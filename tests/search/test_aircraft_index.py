"""Tests for the aircraft OpenSearch index.

The index is an accelerator in front of PostgreSQL, so the properties worth
pinning are the ones that decide whether an admin's search finds the aircraft:
the document carries the fields, the id is the registration (so a re-sync
overwrites rather than duplicates), the query looks for substrings as well as
words, and every cluster-side failure arrives as `SearchError` — the one
exception `web_app` catches to fall back to SQL.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.core.exceptions import SearchError
from src.search.aircraft_index import (
    AIRCRAFT_INDEX,
    INDEX_SETTINGS,
    MAPPINGS,
    PROSE_FIELD,
    AircraftSearchIndex,
    build_document,
)
from tests.search.fake_opensearch import FakeOpenSearch

NOW = datetime(2026, 8, 17, 12, 30, 0)


@pytest.fixture
def client() -> FakeOpenSearch:
    return FakeOpenSearch()


@pytest.fixture
def index(client: FakeOpenSearch) -> AircraftSearchIndex:
    cluster_index = AircraftSearchIndex(client, max_results=50)
    cluster_index.ensure_index()
    return cluster_index


class TestBuildDocument:
    def test_the_registration_is_required(self) -> None:
        """Without one the document has no id, so a re-sync would append a
        second copy instead of replacing the first."""
        with pytest.raises(ValueError, match="registration"):
            build_document({"registration": "  ", "operator": "Air China"})

    def test_searchable_fields_are_carried_over(self) -> None:
        document = build_document(
            {
                "registration": "B-1234",
                "operator": "Air China",
                "livery_name": "80th Anniversary",
                "serial_number": "44521",
                "last_updated": NOW,
            }
        )

        assert document["operator"] == "Air China"
        assert document["livery_name"] == "80th Anniversary"
        assert document["serial_number"] == "44521"
        assert document["last_updated"] == NOW

    def test_empty_and_missing_values_are_dropped(self) -> None:
        """Indexing them as null would make every document carry every column
        that was ever added."""
        document = build_document({"registration": "B-1234", "operator": "", "owner": None})

        assert "operator" not in document
        assert "owner" not in document

    def test_whitespace_is_trimmed(self) -> None:
        document = build_document({"registration": " B-1234 ", "operator": " Air China "})

        assert document["registration"] == "B-1234"
        assert document["operator"] == "Air China"

    def test_sqlite_integer_booleans_become_booleans(self) -> None:
        """SQLite hands back 0/1 where Postgres hands back False/True; the
        mapping declares `boolean` and would reject the integers."""
        document = build_document(
            {"registration": "B-1234", "is_military": 1, "images_downloaded": 0}
        )

        assert document["is_military"] is True
        assert document["images_downloaded"] is False

    def test_an_unparseable_year_is_skipped_rather_than_indexed(self) -> None:
        document = build_document({"registration": "B-1234", "year_built": "unknown"})

        assert "year_built" not in document

    def test_columns_the_index_does_not_declare_are_ignored(self) -> None:
        document = build_document({"registration": "B-1234", "hit_count": 12})

        assert "hit_count" not in document


class TestEnsureIndex:
    def test_a_missing_index_is_created_with_analysers_and_mappings(
        self, client: FakeOpenSearch
    ) -> None:
        created = AircraftSearchIndex(client).ensure_index()

        assert created is True
        body = client.created[AIRCRAFT_INDEX]
        assert body["settings"] == INDEX_SETTINGS
        assert body["mappings"] == MAPPINGS

    def test_an_existing_index_gets_an_additive_mapping_update(
        self, client: FakeOpenSearch
    ) -> None:
        """New fields can be added in place; nothing is recreated, so the
        documents already there survive a deploy that adds a column."""
        cluster_index = AircraftSearchIndex(client)
        cluster_index.ensure_index()

        created = cluster_index.ensure_index()

        assert created is False
        assert client.mapping_updates == [(AIRCRAFT_INDEX, MAPPINGS)]

    def test_registration_is_matched_both_exactly_and_by_substring(self) -> None:
        """`B-12` has to find `B-1234`, which is what the SQL `LIKE '%…%'` this
        replaces did. Only the n-gram analyser gives that."""
        properties = MAPPINGS["properties"]

        assert properties["registration"]["analyzer"] == "identity_index"
        assert properties["registration"]["search_analyzer"] == "identity_search"
        assert properties["registration"]["fields"]["keyword"]["type"] == "keyword"
        assert "identity_ngram" in INDEX_SETTINGS["analysis"]["filter"]

    def test_the_ngrams_are_cut_from_the_whole_registration(self) -> None:
        """An n-gram *tokenizer* would split on the hyphen and then drop the
        leading `B` for being shorter than min_gram, so `B-1234` would be
        indexed as grams of `1234` only and `B-12` would match nothing at all.
        A keyword tokenizer feeding an n-gram filter keeps `b1234` whole."""
        analysis = INDEX_SETTINGS["analysis"]

        assert analysis["analyzer"]["identity_index"]["tokenizer"] == "keyword"
        assert analysis["analyzer"]["identity_index"]["filter"][-1] == "identity_ngram"
        assert analysis["filter"]["identity_ngram"]["min_gram"] == 2

    def test_the_separator_is_folded_away_on_both_sides(self) -> None:
        """So `B1234` finds `B-1234`, and the query fragment is compared against
        grams cut from a value whose hyphen is already gone."""
        analysis = INDEX_SETTINGS["analysis"]

        for analyzer in ("identity_index", "identity_search"):
            assert analysis["analyzer"][analyzer]["char_filter"] == ["identity_strip"]
        assert analysis["char_filter"]["identity_strip"]["pattern"] == "[^A-Za-z0-9]"

    def test_a_hex_query_keeps_its_separators(self) -> None:
        """Hex codes contain none, so a hyphen in the query says "registration".
        Folding it away made `B-12` match the 261 aircraft whose hex contains
        `b12` — none of which the SQL filter this replaces would have returned."""
        properties = MAPPINGS["properties"]
        analysis = INDEX_SETTINGS["analysis"]

        assert properties["hex_code"]["search_analyzer"] == "hex_search"
        assert "char_filter" not in analysis["analyzer"]["hex_search"]
        # Still n-grammed on the way in, or a fragment could not match at all.
        assert properties["hex_code"]["analyzer"] == "identity_index"

    def test_a_single_node_index_asks_for_no_replicas(self) -> None:
        """A replica that can never be assigned leaves the cluster yellow and
        the container health check flapping."""
        assert INDEX_SETTINGS["index"]["number_of_replicas"] == 0

    def test_a_cluster_error_becomes_a_search_error(self) -> None:
        broken = FakeOpenSearch(fail_with=ConnectionError("connection refused"))
        broken.created[AIRCRAFT_INDEX] = {}

        with pytest.raises(SearchError, match="Failed to ensure index"):
            AircraftSearchIndex(broken).ensure_index()


class TestIndexDocuments:
    def test_the_registration_is_the_document_id(self, index: AircraftSearchIndex) -> None:
        """So a second sync of the same aircraft overwrites it."""
        index.index_documents([build_document({"registration": "B-1234", "owner": "First"})])
        index.index_documents([build_document({"registration": "B-1234", "owner": "Second"})])

        assert list(index.client.documents) == ["B-1234"]
        assert index.client.documents["B-1234"]["owner"] == "Second"

    def test_a_batch_is_one_bulk_request(self, index: AircraftSearchIndex) -> None:
        sent = index.index_documents(
            build_document({"registration": reg}) for reg in ("B-1", "B-2", "B-3")
        )

        assert sent == 3
        assert len(index.client.bulk_calls) == 1

    def test_nothing_is_sent_for_an_empty_batch(self, index: AircraftSearchIndex) -> None:
        assert index.index_documents([]) == 0
        assert index.client.bulk_calls == []

    def test_a_reindex_does_not_refresh_per_batch(self, index: AircraftSearchIndex) -> None:
        """One refresh at the end of a full pass instead of one per 500 rows."""
        index.index_documents([build_document({"registration": "B-1234"})])

        assert index.client.bulk_calls[0]["refresh"] is False

    def test_a_rejected_item_raises(self, index: AircraftSearchIndex) -> None:
        """A bulk request returns HTTP 200 with per-item errors inside, so not
        looking at them is how a reindex silently indexes nothing."""
        index.client.bulk_response = {
            "errors": True,
            "items": [
                {
                    "index": {
                        "_id": "B-1234",
                        "status": 400,
                        "error": {"type": "mapper_parsing_exception"},
                    }
                }
            ],
        }

        with pytest.raises(SearchError, match="mapper_parsing_exception"):
            index.index_documents([build_document({"registration": "B-1234"})])

    def test_a_transport_failure_becomes_a_search_error(self) -> None:
        broken = FakeOpenSearch(fail_with=ConnectionError("connection refused"))

        with pytest.raises(SearchError, match="Bulk index"):
            AircraftSearchIndex(broken).index_documents([{"registration": "B-1234"}])


class TestDelete:
    def test_a_registration_is_removed(self, index: AircraftSearchIndex) -> None:
        index.index_documents([build_document({"registration": "B-1234"})])

        assert index.delete_registrations(["B-1234"]) == 1
        assert index.client.documents == {}

    def test_deleting_something_absent_is_not_an_error(self, index: AircraftSearchIndex) -> None:
        """The caller is reconciling; "already gone" is the desired state."""
        assert index.delete_registrations(["B-9999"]) == 1


class TestSearch:
    def test_the_matching_registrations_come_back_in_order(
        self, index: AircraftSearchIndex
    ) -> None:
        index.client.queue_hits("B-1234", "B-5678")

        assert index.search_registrations("air china") == ["B-1234", "B-5678"]

    def test_only_ids_are_requested(self, index: AircraftSearchIndex) -> None:
        """PostgreSQL supplies every rendered field, so `_source` would double
        the response for nothing."""
        index.search_registrations("air china")

        assert index.client.last_search_body["_source"] is False

    def test_an_empty_query_asks_the_cluster_nothing(self, index: AircraftSearchIndex) -> None:
        assert index.search_registrations("   ") == []
        assert index.client.searches == []

    def test_the_result_cap_is_enforced_even_when_a_larger_limit_is_asked_for(
        self, index: AircraftSearchIndex
    ) -> None:
        """The endpoint pages through these in SQL; an uncapped query could pull
        the entire fleet into one response."""
        index.search_registrations("boeing", limit=5000)

        assert index.client.last_search_body["size"] == 50

    def test_a_full_registration_outranks_every_other_clause(
        self, index: AircraftSearchIndex
    ) -> None:
        index.search_registrations("b-1234")

        clauses = index.client.last_search_body["query"]["bool"]["should"]
        exact = next(c for c in clauses if "term" in c and "registration.keyword" in c["term"])
        assert exact["term"]["registration.keyword"]["value"] == "B-1234"
        assert exact["term"]["registration.keyword"]["boost"] == max(
            clause[kind][field]["boost"]
            for clause in clauses
            for kind in ("term", "match")
            if kind in clause
            for field in clause[kind]
        )

    def test_every_word_of_a_multi_word_query_has_to_match(
        self, index: AircraftSearchIndex
    ) -> None:
        """ "air china cargo" must not return the whole of Air China."""
        index.search_registrations("air china cargo")

        clauses = index.client.last_search_body["query"]["bool"]["should"]
        multi_match = next(c["multi_match"] for c in clauses if "multi_match" in c)
        assert multi_match["operator"] == "and"

    def test_the_free_text_clause_is_not_fuzzy(self, index: AircraftSearchIndex) -> None:
        """The caller re-sorts these registrations by its own column, so a loose
        match is not a low-ranked row, it is an equal one. On the real fleet
        `fuzziness: AUTO` turned 40 matches for `B-12` into 241."""
        index.search_registrations("b-12")

        clauses = index.client.last_search_body["query"]["bool"]["should"]
        multi_match = next(c["multi_match"] for c in clauses if "multi_match" in c)
        assert "fuzziness" not in multi_match

    def test_the_ai_report_is_searched_as_a_phrase(self, index: AircraftSearchIndex) -> None:
        """Paragraphs of prose are where an AND-of-words clause goes wrong: on the
        real fleet `B-12` matched 239 reports containing a `B` and a `12`
        somewhere, against 1 containing the phrase — while the queries the field
        exists to serve lose nothing ("special livery" 3 either way)."""
        index.search_registrations("b-12")

        clauses = index.client.last_search_body["query"]["bool"]["should"]
        multi_match = next(c["multi_match"] for c in clauses if "multi_match" in c)
        assert PROSE_FIELD not in multi_match["fields"]
        phrase = next(c["match_phrase"] for c in clauses if "match_phrase" in c)
        assert phrase[PROSE_FIELD]["query"] == "b-12"

    def test_a_cluster_failure_becomes_a_search_error(self) -> None:
        """`web_app` catches exactly this to fall back to the SQL filter."""
        broken = FakeOpenSearch(fail_with=ConnectionError("connection refused"))

        with pytest.raises(SearchError, match="Search on"):
            AircraftSearchIndex(broken).search_registrations("air china")


class TestQueryPage:
    """The list query: the index filters, sorts, pages and counts in one request."""

    def test_an_unfiltered_page_matches_everything(self, index: AircraftSearchIndex) -> None:
        """The admin list's first load. `search_registrations` returns nothing for
        an empty query; this has to return the fleet instead."""
        index.query_page()

        assert index.client.last_search_body["query"] == {"match_all": {}}

    def test_the_filters_are_exact_and_unscored(self, index: AircraftSearchIndex) -> None:
        """`Air China` is one livery, not three words, and a yes/no filter has no
        business influencing relevance."""
        index.query_page(aircraft_type="A350", livery="Air China", attention_levels=["高"])

        query = index.client.last_search_body["query"]["bool"]
        assert query["filter"] == [
            {"term": {"aircraft_type.keyword": "A350"}},
            {"term": {"livery_name.keyword": "Air China"}},
            {"terms": {"attention_level": ["高"]}},
        ]
        assert "must" not in query

    def test_text_and_filters_combine(self, index: AircraftSearchIndex) -> None:
        index.query_page(text="pan am", aircraft_type="B747")

        query = index.client.last_search_body["query"]["bool"]
        assert query["filter"] == [{"term": {"aircraft_type.keyword": "B747"}}]
        assert query["must"], "the typed text was dropped"

    def test_every_sort_ends_with_the_registration(self, index: AircraftSearchIndex) -> None:
        """Thousands of rows share one `last_updated` after a batch update, and an
        ambiguous order over `from`/`size` shows a row on two pages or on none."""
        index.query_page(sort="last_updated", order="desc")

        assert index.client.last_search_body["sort"] == [
            {"last_updated": {"order": "desc", "missing": "_last"}},
            {"registration.keyword": {"order": "asc"}},
        ]

    def test_sorting_by_the_tiebreak_does_not_repeat_it(self, index: AircraftSearchIndex) -> None:
        index.query_page(sort="registration", order="asc")

        assert index.client.last_search_body["sort"] == [
            {"registration.keyword": {"order": "asc", "missing": "_last"}}
        ]

    def test_an_unknown_sort_key_falls_back_rather_than_failing(
        self, index: AircraftSearchIndex
    ) -> None:
        """A stale bookmark still has to render a list."""
        index.query_page(sort="phase_of_moon")

        assert index.client.last_search_body["sort"][0] == {
            "last_updated": {"order": "desc", "missing": "_last"}
        }

    def test_the_window_is_asked_for_as_from_and_size(self, index: AircraftSearchIndex) -> None:
        index.query_page(offset=40, limit=20)

        body = index.client.last_search_body
        assert (body["from"], body["size"]) == (40, 20)
        assert body["_source"] is False

    def test_the_whole_match_count_is_asked_for(self, index: AircraftSearchIndex) -> None:
        """Left to itself the cluster stops counting at 10,000 and reports a lower
        bound, which would cap the pager at 500 pages."""
        index.query_page()

        assert index.client.last_search_body["track_total_hits"] is True

    def test_the_page_and_the_total_are_returned(self, index: AircraftSearchIndex) -> None:
        index.client.queue_hits("B-1234", "N703PA", total=1250)

        page = index.query_page(limit=2)

        assert page.registrations == ["B-1234", "N703PA"]
        assert page.total == 1250

    def test_a_total_reported_as_a_bare_number_is_understood(
        self, index: AircraftSearchIndex
    ) -> None:
        """Clusters configured with `rest_total_hits_as_int` answer this way, and
        reading it as a mapping would silently page over a total of zero."""

        class OldStyle(FakeOpenSearch):
            def search(self, index: str, body: dict[str, object]) -> dict[str, object]:
                response = super().search(index, body)
                hits = response["hits"]
                assert isinstance(hits, dict)
                hits["total"] = 7
                return response

        index.client = OldStyle()

        assert index.query_page().total == 7

    def test_a_failure_becomes_a_search_error(self) -> None:
        """Including a window past `max_result_window`, which `web_app` avoids
        asking for but the cluster would reject."""
        broken = FakeOpenSearch(fail_with=ConnectionError("connection refused"))

        with pytest.raises(SearchError, match="List query on"):
            AircraftSearchIndex(broken).query_page()


class TestFieldCounts:
    """What fills the type and livery dropdowns."""

    @staticmethod
    def _seed(index: AircraftSearchIndex) -> None:
        index.index_documents(
            [
                build_document({"registration": "B-1", "livery_name": "Star Alliance"}),
                build_document({"registration": "B-2", "livery_name": "Star Alliance"}),
                build_document({"registration": "B-3", "livery_name": "Retro"}),
            ]
        )

    def test_values_come_back_most_common_first(self, index: AircraftSearchIndex) -> None:
        self._seed(index)

        assert index.field_counts("livery_name") == [("Star Alliance", 2), ("Retro", 1)]

    def test_the_whole_value_is_counted_not_its_words(self, index: AircraftSearchIndex) -> None:
        """`Star Alliance` is one dropdown entry; the analysed field would make
        it two."""
        self._seed(index)
        index.field_counts("livery_name")

        aggregation = index.client.last_search_body["aggs"]["values"]["terms"]
        assert aggregation["field"] == "livery_name.keyword"

    def test_a_fragment_matches_inside_the_value(self, index: AircraftSearchIndex) -> None:
        """The SQL this replaces was `LIKE '%…%'` on the column."""
        self._seed(index)

        assert index.field_counts("livery_name", contains="alli") == [("Star Alliance", 2)]

    def test_a_fragments_wildcards_are_escaped(self, index: AircraftSearchIndex) -> None:
        """Otherwise a typed `*` would match every livery."""
        index.field_counts("livery_name", contains="a*b")

        value = index.client.last_search_body["query"]["wildcard"]["livery_name.keyword"]["value"]
        assert value == r"*a\*b*"

    def test_the_limit_is_passed_through(self, index: AircraftSearchIndex) -> None:
        index.field_counts("aircraft_type", limit=20)

        assert index.client.last_search_body["aggs"]["values"]["terms"]["size"] == 20

    def test_a_field_with_no_keyword_is_refused(self, index: AircraftSearchIndex) -> None:
        """Aggregating the analysed field would return n-grams as dropdown
        entries, which is worse than an error."""
        with pytest.raises(SearchError, match="aggregatable"):
            index.field_counts("year_built")


class TestSummaryCounts:
    def test_the_header_counts_are_one_request(self, index: AircraftSearchIndex) -> None:
        index.index_documents(
            [
                build_document(
                    {"registration": "B-1", "images_downloaded": True, "attention_level": "高"}
                ),
                build_document({"registration": "B-2", "images_downloaded": False}),
            ]
        )

        counts = index.summary_counts(("高", "极高"))

        assert counts == {"total": 2, "with_images": 1, "special": 1}
        assert len(index.client.searches) == 1

    def test_no_special_levels_means_none_are_special(self, index: AircraftSearchIndex) -> None:
        index.index_documents([build_document({"registration": "B-1", "attention_level": "高"})])

        assert index.summary_counts()["special"] == 0

    def test_a_failure_becomes_a_search_error(self) -> None:
        broken = FakeOpenSearch(fail_with=ConnectionError("connection refused"))

        with pytest.raises(SearchError, match="Summary aggregation"):
            AircraftSearchIndex(broken).summary_counts()


class TestSuggestRegistrations:
    def test_a_prefix_match_is_boosted_above_a_substring_one(
        self, index: AircraftSearchIndex
    ) -> None:
        """Typing `B-12` should offer `B-1234` before `N9B-12`."""
        index.suggest_registrations("B-12")

        clauses = index.client.last_search_body["query"]["bool"]["should"]
        prefix = next(c["prefix"] for c in clauses if "prefix" in c)
        assert prefix["registration.keyword"]["boost"] == 10
        assert prefix["registration.keyword"]["case_insensitive"] is True

    def test_relevance_leads_and_the_registration_breaks_ties(
        self, index: AircraftSearchIndex
    ) -> None:
        """Which is the order the SQL `CASE WHEN … THEN 0` produced."""
        index.suggest_registrations("B-12")

        assert index.client.last_search_body["sort"] == [
            "_score",
            {"registration.keyword": {"order": "asc"}},
        ]

    def test_the_hex_code_is_searched_too(self, index: AircraftSearchIndex) -> None:
        """An admin pasting a hex from an ADS-B feed gets the registration back."""
        index.suggest_registrations("780abc")

        should = index.client.last_search_body["query"]["bool"]["should"]
        assert any("hex_code" in clause.get("match", {}) for clause in should)

    def test_an_empty_fragment_asks_the_cluster_nothing(self, index: AircraftSearchIndex) -> None:
        assert index.suggest_registrations("  ") == []
        assert index.client.searches == []

    def test_a_failure_becomes_a_search_error(self) -> None:
        broken = FakeOpenSearch(fail_with=ConnectionError("connection refused"))

        with pytest.raises(SearchError, match="Suggest on"):
            AircraftSearchIndex(broken).suggest_registrations("B-12")


class TestWatermark:
    def test_the_newest_indexed_timestamp_is_returned(self, index: AircraftSearchIndex) -> None:
        index.index_documents(
            [
                build_document({"registration": "B-1", "last_updated": NOW}),
                build_document(
                    {"registration": "B-2", "last_updated": NOW.replace(hour=9)},
                ),
            ]
        )

        assert index.max_last_updated() == NOW

    def test_an_empty_index_has_no_watermark(self, index: AircraftSearchIndex) -> None:
        """Which is how `--incremental` decides to run a full pass instead."""
        assert index.max_last_updated() is None

    def test_a_missing_index_has_no_watermark(self, client: FakeOpenSearch) -> None:
        assert AircraftSearchIndex(client).max_last_updated() is None
        assert client.searches == []


class TestDocumentCount:
    def test_a_missing_index_counts_zero_rather_than_raising(self, client: FakeOpenSearch) -> None:
        assert AircraftSearchIndex(client).document_count() == 0

    def test_indexed_documents_are_counted(self, index: AircraftSearchIndex) -> None:
        index.index_documents([build_document({"registration": "B-1234"})])

        assert index.document_count() == 1
