"""Analyser behaviour against a real OpenSearch cluster.

The tests in `test_aircraft_index.py` run against a fake and can only assert the
*shape* of the query and the settings — which is how a broken analyser reached
production: `identity_index` was built on an n-gram tokenizer, so it split
`B-1234` on the hyphen, dropped the leading `B` for being shorter than min_gram,
and indexed grams of `1234` alone. Every assertion about the mapping still
passed, and `B-12` matched nothing.

Only a cluster can settle what the analysers actually do, so these are marked
`integration` (deselected by default) and skip unless `OPENSEARCH_URL` is set:

    OPENSEARCH_URL=http://127.0.0.1:9201 uv run pytest -m integration tests/search/

They build and drop their own index, never the deployed one.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime

import pytest

from src.search.aircraft_index import AircraftSearchIndex, build_document
from src.search.opensearch_client import OpenSearchSettings, build_client

pytestmark = pytest.mark.integration

# Real-fleet registrations: B-1234 is the aircraft being looked for, B-18305
# shares the `B-1` prefix and a `1` but not the substring `B-12`, and N4105G
# shares nothing. The sortable and aggregatable fields differ per row so that a
# page, an order and a bucket count are all distinguishable from one another.
FLEET = (
    {
        "registration": "B-1234",
        "operator": "Air China",
        "hex_code": "780abc",
        "aircraft_type": "A350",
        "livery_name": "Star Alliance",
        "last_updated": datetime(2026, 8, 17, 10, 0, 0),
        "photographer_count": 5,
        "images_downloaded": True,
        "attention_level": "高",
    },
    {
        "registration": "B-18305",
        "operator": "China Airlines",
        "hex_code": "899123",
        "aircraft_type": "A350",
        "livery_name": "Retro",
        "last_updated": datetime(2026, 8, 16, 10, 0, 0),
        "photographer_count": 12,
        "images_downloaded": False,
    },
    {
        "registration": "N4105G",
        "operator": "Pan Am",
        "ai_analysis": "Boeing 737-12 seen at KJFK",
        "aircraft_type": "B738",
        "last_updated": datetime(2026, 8, 15, 10, 0, 0),
        "photographer_count": 0,
        "images_downloaded": True,
    },
)


@pytest.fixture(scope="module")
def index() -> Iterator[AircraftSearchIndex]:
    url = os.environ.get("OPENSEARCH_URL", "").strip()
    if not url:
        pytest.skip("OPENSEARCH_URL is not set")

    settings = OpenSearchSettings(url=url, index="aircraft-test-analysers")
    live = AircraftSearchIndex(build_client(settings), index=settings.index)
    live.client.indices.delete(index=live.index, ignore_unavailable=True)
    live.ensure_index()
    live.index_documents([build_document(row) for row in FLEET], refresh=True)
    yield live
    live.client.indices.delete(index=live.index, ignore_unavailable=True)


class TestIdentitySubstring:
    def test_a_registration_fragment_finds_the_aircraft(self, index: AircraftSearchIndex) -> None:
        """The property the SQL `LIKE '%…%'` had and the first analyser lost."""
        assert index.search_registrations("B-12") == ["B-1234"]

    def test_a_fragment_that_is_not_a_substring_does_not_match(
        self, index: AircraftSearchIndex
    ) -> None:
        """`B-18305` contains `B-1` and a `2`-adjacent digit run, but not `B-12`.
        Matching it is the failure mode that made the first version return 241
        aircraft for a query with 40 real answers."""
        assert "B-18305" not in index.search_registrations("B-12")

    def test_the_separator_is_optional(self, index: AircraftSearchIndex) -> None:
        assert index.search_registrations("b1234") == ["B-1234"]

    def test_a_full_registration_finds_exactly_one_aircraft(
        self, index: AircraftSearchIndex
    ) -> None:
        assert index.search_registrations("B-1234")[0] == "B-1234"

    def test_a_hex_code_fragment_matches(self, index: AircraftSearchIndex) -> None:
        assert index.search_registrations("780ab") == ["B-1234"]

    def test_a_hyphenated_query_does_not_reach_into_hex_codes(
        self, index: AircraftSearchIndex
    ) -> None:
        """`B-18305` has hex `899123`, which contains `9912`. Query `99-12` is a
        registration shape, so it must not match it — that conflation added 261
        aircraft to a 124-aircraft answer on the real fleet."""
        assert index.search_registrations("99-12") == []
        assert index.search_registrations("9912") == ["B-18305"]


class TestFreeText:
    def test_an_operator_nothing_else_identifies_is_found(self, index: AircraftSearchIndex) -> None:
        assert index.search_registrations("pan am") == ["N4105G"]

    def test_all_words_must_match(self, index: AircraftSearchIndex) -> None:
        """ "air china cargo" is not Air China."""
        assert index.search_registrations("air china cargo") == []

    def test_a_number_in_an_ai_report_does_not_answer_an_identity_query(
        self, index: AircraftSearchIndex
    ) -> None:
        """N4105G's AI report says "737-12". Fuzzy free-text matching pulled
        aircraft like it into every short identity query."""
        assert "N4105G" not in index.search_registrations("B-12")

    def test_a_phrase_from_an_ai_report_still_finds_the_aircraft(
        self, index: AircraftSearchIndex
    ) -> None:
        """The report is searched as a phrase rather than as a bag of words, but
        it is still searched — that is the field's whole purpose."""
        assert index.search_registrations("seen at KJFK") == ["N4105G"]

    def test_words_scattered_across_a_report_are_not_a_phrase(
        self, index: AircraftSearchIndex
    ) -> None:
        """ "Boeing … KJFK" appear in N4105G's report but not adjacently. On the
        real fleet this distinction is what dropped `B-12` from 360 hits to 127."""
        assert index.search_registrations("boeing KJFK") == []


class TestListQuery:
    """The admin list, which the cluster now answers whole.

    The fake can only show that a `from`, a `size` and a `sort` were asked for.
    Whether the cluster then returns disjoint pages in the requested order — the
    thing a missing tie-break silently breaks — only a cluster can say.
    """

    def test_an_unfiltered_query_returns_the_fleet_newest_first(
        self, index: AircraftSearchIndex
    ) -> None:
        """The first load of the admin page, with nothing typed and nothing filtered."""
        page = index.query_page()

        assert page.registrations == ["B-1234", "B-18305", "N4105G"]
        assert page.total == 3

    def test_consecutive_pages_are_disjoint_and_in_order(self, index: AircraftSearchIndex) -> None:
        first = index.query_page(limit=2)
        second = index.query_page(offset=2, limit=2)

        assert first.registrations == ["B-1234", "B-18305"]
        assert second.registrations == ["N4105G"]
        assert (first.total, second.total) == (3, 3)

    def test_sorting_by_photographers_orders_by_the_denormalised_count(
        self, index: AircraftSearchIndex
    ) -> None:
        """The count lives in `aircraft_images`; the index cannot sort on a value
        it does not hold, so `aircraft_sync` copies it in."""
        page = index.query_page(sort="photographers", order="desc")

        assert page.registrations == ["B-18305", "B-1234", "N4105G"]

    def test_sorting_by_registration_is_alphabetical(self, index: AircraftSearchIndex) -> None:
        page = index.query_page(sort="registration", order="asc")

        assert page.registrations == ["B-1234", "B-18305", "N4105G"]

    def test_a_filter_narrows_the_total_not_only_the_page(self, index: AircraftSearchIndex) -> None:
        """The pager would otherwise promise pages that do not exist."""
        page = index.query_page(aircraft_type="A350")

        assert page.registrations == ["B-1234", "B-18305"]
        assert page.total == 2

    def test_the_livery_filter_matches_a_whole_multi_word_value(
        self, index: AircraftSearchIndex
    ) -> None:
        assert index.query_page(livery="Star Alliance").registrations == ["B-1234"]

    def test_text_and_filters_narrow_together(self, index: AircraftSearchIndex) -> None:
        assert index.query_page(text="air china", aircraft_type="A350").registrations == ["B-1234"]
        assert index.query_page(text="air china", aircraft_type="B738").registrations == []

    def test_the_special_category_filters_on_attention_level(
        self, index: AircraftSearchIndex
    ) -> None:
        assert index.query_page(attention_levels=["高", "极高"]).registrations == ["B-1234"]


class TestAggregations:
    """The header counts and the two filter dropdowns."""

    def test_types_are_counted_per_distinct_value(self, index: AircraftSearchIndex) -> None:
        assert index.field_counts("aircraft_type") == [("A350", 2), ("B738", 1)]

    def test_a_multi_word_livery_is_one_bucket_matched_by_a_fragment(
        self, index: AircraftSearchIndex
    ) -> None:
        """`Star Alliance` has to arrive as one dropdown entry, and `alli` has to
        find it in the middle of the value — the `LIKE '%…%'` this replaces did."""
        assert index.field_counts("livery_name", contains="alli") == [("Star Alliance", 1)]

    def test_the_header_counts_are_computed_in_one_request(
        self, index: AircraftSearchIndex
    ) -> None:
        assert index.summary_counts(("高", "极高")) == {
            "total": 3,
            "with_images": 2,
            "special": 1,
        }

    def test_autocomplete_offers_both_prefix_matches(self, index: AircraftSearchIndex) -> None:
        assert set(index.suggest_registrations("B-1")) == {"B-1234", "B-18305"}

    def test_autocomplete_also_finds_a_registration_by_its_middle(
        self, index: AircraftSearchIndex
    ) -> None:
        assert index.suggest_registrations("1234") == ["B-1234"]
