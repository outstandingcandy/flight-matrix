"""Tests for the OpenSearch-backed `search` parameter of `/api/admin/aircraft`.

The endpoint used to match `search` against registration and hex code only, with
`LIKE '%…%'`. It now asks OpenSearch, which widens the match to operator, owner,
manufacturer, livery, serial and the AI analysis — but the rows still come from
PostgreSQL, and the index is optional infrastructure.

So the properties under test are the seams, not relevance (which belongs to
OpenSearch and is covered against a fake in `tests/search/`):

* a match the old SQL could never have found is returned;
* the index deciding nothing matches returns an empty page, not the whole fleet;
* a registration the index still holds but the database has dropped is skipped
  rather than 500-ing;
* an unreachable or unconfigured cluster silently falls back to the old filter.

The fake client is injected by replacing `web_app.get_client`, so everything from
settings resolution through query construction to the `IN` clause runs for real.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from tests.search.fake_opensearch import FakeOpenSearch

LIST_PATH = "/api/admin/aircraft"

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
    """Point the app's client factory at `cluster` (None = nothing configured)."""
    web_app = client.application_module
    web_app.get_client = lambda settings=None: cluster  # type: ignore[assignment]


def _registrations(payload: dict[str, Any]) -> list[str]:
    return [aircraft["registration"] for aircraft in payload["aircraft"]]


@pytest.fixture
def cluster() -> FakeOpenSearch:
    return FakeOpenSearch()


class TestOpenSearchPath:
    def test_a_match_the_old_sql_filter_could_not_find_is_returned(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        """ "Pan Am" appears in no registration and no hex code."""
        _seed(app_client)
        _use(app_client, cluster)
        cluster.queue_hits("N703PA")

        payload = app_client.get(f"{LIST_PATH}?search=pan+am").get_json()

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

    def test_no_matches_returns_an_empty_page_not_the_whole_fleet(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        _seed(app_client)
        _use(app_client, cluster)
        cluster.queue_hits()

        payload = app_client.get(f"{LIST_PATH}?search=qantas").get_json()

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
        assert _registrations(response.get_json()) == ["B-1234"]

    def test_the_other_filters_still_apply_to_the_matches(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        """OpenSearch narrows by text; SQL still owns type / livery / category."""
        _seed(app_client)
        _use(app_client, cluster)
        cluster.queue_hits("B-1234", "N703PA")

        payload = app_client.get(f"{LIST_PATH}?search=airline&aircraft_type=A380").get_json()

        assert payload["aircraft"] == []

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
        assert _registrations(response.get_json()) == ["B-1234"]
        assert app_client.get(LIST_PATH).get_json()["total"] == 2

    def test_pagination_still_applies_to_a_search(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        _seed(app_client)
        _use(app_client, cluster)
        cluster.queue_hits("B-1234", "N703PA")

        first = app_client.get(f"{LIST_PATH}?search=a&limit=1&page=1").get_json()
        second = app_client.get(f"{LIST_PATH}?search=a&limit=1&page=2").get_json()

        assert first["total"] == 2
        assert first["pages"] == 2
        assert _registrations(first) != _registrations(second)


class TestFallback:
    def test_an_unconfigured_cluster_keeps_the_old_registration_filter(
        self, app_client: Any
    ) -> None:
        _seed(app_client)
        _use(app_client, None)

        payload = app_client.get(f"{LIST_PATH}?search=B-12").get_json()

        assert payload["search_backend"] == "sql"
        assert _registrations(payload) == ["B-1234"]

    def test_an_unreachable_cluster_falls_back_instead_of_failing(self, app_client: Any) -> None:
        """An admin page is not allowed to break because search is down."""
        _seed(app_client)
        _use(app_client, FakeOpenSearch(fail_with=ConnectionError("connection refused")))

        response = app_client.get(f"{LIST_PATH}?search=780abc")
        payload = response.get_json()

        assert response.status_code == 200
        assert payload["search_backend"] == "sql"
        assert _registrations(payload) == ["B-1234"]

    def test_an_unfiltered_list_never_consults_the_cluster(
        self, app_client: Any, cluster: FakeOpenSearch
    ) -> None:
        """Every admin page load would otherwise pay for a search returning
        everything."""
        _seed(app_client)
        _use(app_client, cluster)

        payload = app_client.get(LIST_PATH).get_json()

        assert cluster.searches == []
        assert payload["total"] == 2
        assert payload["search_backend"] == "sql"
