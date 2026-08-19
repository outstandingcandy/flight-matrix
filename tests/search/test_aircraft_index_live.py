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

import pytest

from src.search.aircraft_index import AircraftSearchIndex, build_document
from src.search.opensearch_client import OpenSearchSettings, build_client

pytestmark = pytest.mark.integration

# Real-fleet registrations: B-1234 is the aircraft being looked for, B-18305
# shares the `B-1` prefix and a `1` but not the substring `B-12`, and N4105G
# shares nothing.
FLEET = (
    {"registration": "B-1234", "operator": "Air China", "hex_code": "780abc"},
    {"registration": "B-18305", "operator": "China Airlines", "hex_code": "899123"},
    {"registration": "N4105G", "operator": "Pan Am", "ai_analysis": "Boeing 737-12 seen at KJFK"},
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
