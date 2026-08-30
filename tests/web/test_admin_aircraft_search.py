"""Tests for the OpenSearch-backed admin aircraft endpoints.

`/api/v1/admin/aircraft` used to be pure SQL, with OpenSearch bolted on to widen the
`search` parameter. It now asks the index for the whole query — text, the type /
livery / category filters, the sort, the page window and the total — including the
first page load, where nothing is filtered at all. Its header counts and its two
filter dropdowns come from aggregations, and the registration autocomplete from a
prefix query. Rows are still hydrated from PostgreSQL, and the index remains
optional infrastructure.

So the properties under test are the seams, not relevance (which belongs to
OpenSearch and is covered against a fake in `tests/search/`):

* the index answers even an unfiltered first page, and the filters, sort and page
  window it is asked for are the ones the request carried;
* the order and total it returns are the ones rendered, not SQL's;
* a registration the index still holds but the database has dropped is skipped
  rather than 500-ing;
* an unreachable cluster, an unconfigured one, or a page deeper than the index's
  window falls back to the old SQL, which still filters and counts correctly.

The fake client is injected by replacing `web_app.get_client`, so everything from
settings resolution through query construction to the `IN` clause runs for real.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from tests.search.fake_opensearch import FakeOpenSearch

LIST_PATH = "/api/v1/admin/aircraft"
STATS_PATH = "/api/v1/admin/aircraft/stats"
TYPES_PATH = "/api/v1/admin/aircraft/types"
LIVERIES_PATH = "/api/v1/admin/aircraft/liveries"
REGISTRATIONS_PATH = "/api/v1/admin/aircraft/registrations"

# Naive UTC, which is what every writer in this project stores.
NOW = datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def _seed(client: Any) -> None:
    """Two aircraft whose operators are the only searchable difference."""
    session = client.application_module.db_manager.get_session()
    try:
        for registration, hex_code, operator in (
            ("B-1234", "780abc", "Air China"),
            ("N703PA", "a1b2c3", "Pan Am"),
        ):
            session.execute(
                text("""
                INSERT INTO aircraft_static_info
                    (registration, hex_code, operator, last_updated)
                VALUES (:registration, :hex_code, :operator, :ts)
                """),
                {
                    "registration": registration,
                    "hex_code": hex_code,
                    "operator": operator,
                    "ts": NOW,
                },
            )
        session.commit()
    finally:
        session.close()


def _use(client: Any, cluster: FakeOpenSearch | None) -> None:
    """Point the app's client factory at `cluster` (None = nothing configured).

    ``aircraft_search_index`` moved out of ``web_app`` into
    :mod:`src.web.search_index` (which imports ``get_client`` directly
    from ``src.search.opensearch_client``). Patch there — patching
    ``web_app.get_client`` no longer reaches the call site.
    """
    from src.web import search_index

    search_index.get_client = lambda settings=None: cluster  # type: ignore[assignment]


def _registrations(payload: dict[str, Any]) -> list[str]:
    return [aircraft["registration"] for aircraft in payload["aircraft"]]


def _filters(cluster: FakeOpenSearch) -> list[dict[str, Any]]:
    """The exact-match filter clauses of the last list query."""
    query = cluster.last_search_body["query"]
    return list(query.get("bool", {}).get("filter", []))


@pytest.fixture
def cluster() -> FakeOpenSearch:
    return FakeOpenSearch()


class TestTheIndexAnswersTheWholeList:
    def test_the_first_unfiltered_page_load_goes_to_the_index(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        """Not just the search box: opening the page is a `match_all` on the index."""
        _seed(app_client)
        _use(app_client, cluster)
        cluster.queue_hits("B-1234", "N703PA")

        payload = app_client.get(LIST_PATH).json()

        assert payload["search_backend"] == "opensearch"
        assert cluster.last_search_body["query"] == {"match_all": {}}
        assert _registrations(payload) == ["B-1234", "N703PA"]

    def test_the_order_rendered_is_the_index_order_not_the_databases(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        """`IN` returns rows in the database's own order; sorting is the index's
        job now, so the hydrated rows have to be put back into its sequence."""
        _seed(app_client)
        _use(app_client, cluster)
        cluster.queue_hits("N703PA", "B-1234")

        payload = app_client.get(LIST_PATH).json()

        assert _registrations(payload) == ["N703PA", "B-1234"]

    def test_the_total_comes_from_the_index_not_from_a_count_query(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        """The page window holds one row; the pager still has to show 63 pages."""
        _seed(app_client)
        _use(app_client, cluster)
        cluster.queue_hits("B-1234", total=1250)

        payload = app_client.get(LIST_PATH).json()

        assert (payload["total"], payload["pages"]) == (1250, 63)

    def test_the_requested_page_window_is_asked_of_the_index(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        _seed(app_client)
        _use(app_client, cluster)

        app_client.get(f"{LIST_PATH}?page=3&limit=10")

        body = cluster.last_search_body
        assert (body["from"], body["size"]) == (20, 10)

    def test_the_requested_sort_is_asked_of_the_index_with_a_tiebreak(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        """Thousands of aircraft share a photographer count of 0, and an
        ambiguous order over `from`/`size` shows a row twice or not at all."""
        _seed(app_client)
        _use(app_client, cluster)

        app_client.get(f"{LIST_PATH}?sort=photographers&order=asc")

        assert cluster.last_search_body["sort"] == [
            {"photographer_count": {"order": "asc", "missing": "_last"}},
            {"registration.keyword": {"order": "asc"}},
        ]

    def test_type_livery_and_category_are_filtered_by_the_index(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        """These three used to be SQL `WHERE` clauses; the index owns them now,
        or paging and totals would be computed over the wrong match set."""
        _seed(app_client)
        _use(app_client, cluster)

        app_client.get(f"{LIST_PATH}?aircraft_type=A350&livery=Star+Alliance&category=special")

        clauses = _filters(cluster)
        assert {"term": {"aircraft_type.keyword": "A350"}} in clauses
        assert {"term": {"livery_name.keyword": "Star Alliance"}} in clauses
        assert any("terms" in clause and "attention_level" in clause["terms"] for clause in clauses)

    def test_an_unknown_category_filters_nothing(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        """`widebody` and friends are not derivable from the current data. The SQL
        it replaces ignored them, and a stale bookmark still has to render."""
        _seed(app_client)
        _use(app_client, cluster)
        cluster.queue_hits("B-1234", "N703PA")

        payload = app_client.get(f"{LIST_PATH}?category=widebody").json()

        assert cluster.last_search_body["query"] == {"match_all": {}}
        assert len(payload["aircraft"]) == 2

    def test_a_match_the_old_sql_filter_could_not_find_is_returned(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        """ "Pan Am" appears in no registration and no hex code."""
        _seed(app_client)
        _use(app_client, cluster)
        cluster.queue_hits("N703PA")

        payload = app_client.get(f"{LIST_PATH}?search=pan+am").json()

        assert payload["search_backend"] == "opensearch"
        assert _registrations(payload) == ["N703PA"]
        assert payload["total"] == 1

    def test_the_typed_text_reaches_the_cluster(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        _seed(app_client)
        _use(app_client, cluster)

        app_client.get(f"{LIST_PATH}?search=lufthansa")

        assert cluster.searches, "the endpoint never queried OpenSearch"
        assert "lufthansa" in str(cluster.last_search_body["query"])

    def test_no_matches_returns_an_empty_page_not_the_whole_fleet(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        _seed(app_client)
        _use(app_client, cluster)
        cluster.queue_hits()

        payload = app_client.get(f"{LIST_PATH}?search=qantas").json()

        assert payload["success"] is True
        assert payload["aircraft"] == []
        assert (payload["total"], payload["pages"]) == (0, 0)
        assert payload["search_backend"] == "opensearch"

    def test_a_stale_index_entry_is_skipped_rather_than_rendered(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        """The index is rebuilt from the database, never the other way round, so
        a document for a deleted row is expected and must not 500."""
        _seed(app_client)
        _use(app_client, cluster)
        cluster.queue_hits("B-1234", "DELETED-1")

        response = app_client.get(f"{LIST_PATH}?search=air+china")

        assert response.status_code == 200
        assert _registrations(response.json()) == ["B-1234"]

    def test_matched_registrations_are_bound_not_interpolated(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        """A document id is data. If it were spliced into the SQL text, this one
        would end the statement early."""
        _seed(app_client)
        _use(app_client, cluster)
        cluster.queue_hits("B-1234", "'); DROP TABLE aircraft_static_info; --")

        response = app_client.get(f"{LIST_PATH}?search=air+china")

        assert response.status_code == 200
        assert _registrations(response.json()) == ["B-1234"]
        _use(app_client, None)
        assert app_client.get(LIST_PATH).json()["total"] == 2


class TestHeaderCountsAndDropdowns:
    """The rest of the page: nothing here waits on a `COUNT(*)` scan either."""

    @staticmethod
    def _index(cluster: FakeOpenSearch) -> None:
        cluster.documents.update(
            {
                "B-1234": {
                    "registration": "B-1234",
                    "aircraft_type": "A350",
                    "livery_name": "Star Alliance",
                    "images_downloaded": True,
                    "attention_level": "高",
                },
                "N703PA": {
                    "registration": "N703PA",
                    "aircraft_type": "A350",
                    "livery_name": "Retro",
                    "images_downloaded": False,
                },
            }
        )

    def test_the_header_counts_come_from_one_aggregation(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        _seed(app_client)
        _use(app_client, cluster)
        self._index(cluster)

        stats = app_client.get(STATS_PATH).json()["stats"]

        assert (stats["total"], stats["with_images"], stats["special"]) == (2, 1, 1)
        # Not derivable from the current data by either backend.
        assert (stats["widebody"], stats["cargo"], stats["military"]) == (0, 0, 0)

    def test_the_type_dropdown_counts_aircraft_per_type(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        _seed(app_client)
        _use(app_client, cluster)
        self._index(cluster)

        types = app_client.get(TYPES_PATH).json()["types"]

        assert types == [{"code": "A350", "full_name": "A350", "count": 2}]

    def test_the_livery_dropdown_narrows_on_a_substring(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        """The SQL it replaces was `LIKE '%…%'` on the whole column, so "alli"
        has to match in the middle of "Star Alliance"."""
        _seed(app_client)
        _use(app_client, cluster)
        self._index(cluster)

        liveries = app_client.get(f"{LIVERIES_PATH}?search=alli").json()["liveries"]

        assert liveries == [{"name": "Star Alliance", "count": 1}]
        assert cluster.last_search_body["query"]["wildcard"]["livery_name.keyword"]["value"] == (
            "*alli*"
        )

    def test_autocomplete_hydrates_the_suggested_registrations(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        """The index ranks; the hex code and type beside each row come from SQL."""
        _seed(app_client)
        _use(app_client, cluster)
        cluster.queue_hits("N703PA", "B-1234")

        payload = app_client.get(f"{REGISTRATIONS_PATH}?search=70").json()

        assert payload["registrations"] == [
            {"registration": "N703PA", "hex_code": "a1b2c3", "aircraft_type": None},
            {"registration": "B-1234", "hex_code": "780abc", "aircraft_type": None},
        ]

    def test_autocomplete_skips_a_suggestion_the_table_has_dropped(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        _seed(app_client)
        _use(app_client, cluster)
        cluster.queue_hits("DELETED-1", "B-1234")

        payload = app_client.get(f"{REGISTRATIONS_PATH}?search=B-").json()

        assert [row["registration"] for row in payload["registrations"]] == ["B-1234"]

    def test_a_dead_cluster_leaves_every_dropdown_working(self, app_client: Any) -> None:
        _seed(app_client)
        _use(app_client, FakeOpenSearch(fail_with=ConnectionError("connection refused")))

        stats = app_client.get(STATS_PATH).json()
        types = app_client.get(TYPES_PATH).json()
        liveries = app_client.get(LIVERIES_PATH).json()
        suggestions = app_client.get(f"{REGISTRATIONS_PATH}?search=B-").json()

        assert stats["stats"]["total"] == 2
        assert types["success"] is True and liveries["success"] is True
        assert [row["registration"] for row in suggestions["registrations"]] == ["B-1234"]


class TestFallback:
    def test_an_unconfigured_cluster_keeps_the_old_registration_filter(
        self, app_client: Any
    ) -> None:
        _seed(app_client)
        _use(app_client, None)

        payload = app_client.get(f"{LIST_PATH}?search=B-12").json()

        assert payload["search_backend"] == "sql"
        assert _registrations(payload) == ["B-1234"]

    def test_an_unreachable_cluster_falls_back_instead_of_failing(self, app_client: Any) -> None:
        """An admin page is not allowed to break because search is down."""
        _seed(app_client)
        _use(app_client, FakeOpenSearch(fail_with=ConnectionError("connection refused")))

        response = app_client.get(f"{LIST_PATH}?search=780abc")
        payload = response.json()

        assert response.status_code == 200
        assert payload["search_backend"] == "sql"
        assert _registrations(payload) == ["B-1234"]

    def test_an_unfiltered_first_page_still_renders_without_a_cluster(
        self, app_client: Any
    ) -> None:
        """The whole list depends on the index now, so this is the case that
        would leave the page blank if the fallback were lost."""
        _seed(app_client)
        _use(app_client, None)

        payload = app_client.get(LIST_PATH).json()

        assert payload["search_backend"] == "sql"
        assert (payload["total"], len(payload["aircraft"])) == (2, 2)

    def test_the_sql_category_filter_binds_its_attention_levels(self, app_client: Any) -> None:
        """An `IN :levels` that is not declared expanding raises rather than
        filtering, and neither seeded aircraft is special."""
        _seed(app_client)
        _use(app_client, None)

        response = app_client.get(f"{LIST_PATH}?category=special")

        assert response.status_code == 200
        assert response.json()["total"] == 0

    def test_a_page_deeper_than_the_index_window_is_served_by_sql(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        """`from + size` past `index.max_result_window` is rejected outright, so
        the query must not be sent at all."""
        _seed(app_client)
        _use(app_client, cluster)

        payload = app_client.get(f"{LIST_PATH}?page=501&limit=20").json()

        assert cluster.searches == []
        assert payload["search_backend"] == "sql"
        assert (payload["total"], payload["aircraft"]) == (2, [])
