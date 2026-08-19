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
        assert "identity_ngram" in INDEX_SETTINGS["analysis"]["tokenizer"]

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
        assert multi_match["fuzziness"] == "AUTO"

    def test_a_cluster_failure_becomes_a_search_error(self) -> None:
        """`web_app` catches exactly this to fall back to the SQL filter."""
        broken = FakeOpenSearch(fail_with=ConnectionError("connection refused"))

        with pytest.raises(SearchError, match="Search on"):
            AircraftSearchIndex(broken).search_registrations("air china")


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
