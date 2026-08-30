"""Semantic coverage for the ``/api/v1/flight-schedules*`` routes.

The main endpoint is the heaviest handler in the app (airport board
polled every 30 s by the frontend). The smoke sweep already asserts
these routes don't 5xx; this file goes one level deeper on the surface
that clients actually depend on:

- Required-airport 400
- Response body shape (top-level keys the frontend reads)
- Query-parameter validation (``limit`` clamp)
- ``filter-options`` shape + the "no search → empty airports list"
  fast-path documented on the handler
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# /api/flight-schedules


class TestFlightSchedules:
    def test_missing_airport_returns_400(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.get("/api/v1/flight-schedules")
        assert r.status_code == 400
        assert r.json() == {"success": False, "error": "Airport code is required"}

    def test_valid_airport_returns_schedule_shape(self, app_client_fastapi: Any) -> None:
        """Empty DB → empty schedules list, but the *shape* is the contract
        the frontend depends on. Assert the top-level keys, not the
        counts.
        """
        r = app_client_fastapi.get("/api/v1/flight-schedules", params={"airport": "PEK"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        assert isinstance(body.get("schedules"), list)
        # `total_count` / `arrival_count` / `departure_count` are what
        # the tab counter in the UI reads — Number-ish types, never null.
        for key in ("total_count", "arrival_count", "departure_count"):
            assert key in body, f"missing key {key!r}"
            assert isinstance(body[key], int)

    def test_limit_over_500_rejected(self, app_client_fastapi: Any) -> None:
        """``limit`` is declared ``Query(200, ge=1, le=500)``. Anything
        above 500 must 422 (Pydantic validation) rather than silently
        clamping or ignoring — a client asking for 1000 rows is a bug
        we want visible.
        """
        r = app_client_fastapi.get(
            "/api/v1/flight-schedules",
            params={"airport": "PEK", "limit": 1000},
        )
        assert r.status_code == 422

    def test_limit_below_one_rejected(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.get(
            "/api/v1/flight-schedules",
            params={"airport": "PEK", "limit": 0},
        )
        assert r.status_code == 422

    def test_flight_type_filter_accepted(self, app_client_fastapi: Any) -> None:
        for ft in ("arrival", "departure", ""):
            r = app_client_fastapi.get(
                "/api/v1/flight-schedules",
                params={"airport": "PEK", "flight_type": ft},
            )
            assert r.status_code == 200, (ft, r.text)

    def test_date_query_accepts_iso_and_recent(self, app_client_fastapi: Any) -> None:
        for date in ("2026-01-01", "recent", ""):
            r = app_client_fastapi.get(
                "/api/v1/flight-schedules",
                params={"airport": "PEK", "date": date},
            )
            assert r.status_code == 200, (date, r.text)


# ---------------------------------------------------------------------------
# /api/flight-schedules/filter-options


class TestFilterOptions:
    def test_default_response_shape(self, app_client_fastapi: Any) -> None:
        """Empty query — the "no search" fast path returns an empty
        airports list along with the standard four top-level keys.
        """
        r = app_client_fastapi.get("/api/v1/flight-schedules/filter-options")
        assert r.status_code == 200, r.text
        body = r.json()
        for key in ("airports", "aircraft_types", "liveries", "available_dates"):
            assert key in body, f"expected key {key!r} in body, got {list(body)}"
        # No search → the airports list is empty by design (see the
        # handler's "no search → empty airports list" fast path).
        assert body["airports"] == []

    def test_airport_narrows_types_and_dates(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.get(
            "/api/v1/flight-schedules/filter-options",
            params={"airport": "PEK"},
        )
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["aircraft_types"], list)
        assert isinstance(body["liveries"], list)
        assert isinstance(body["available_dates"], list)

    def test_search_populates_airports_key(self, app_client_fastapi: Any) -> None:
        """When ``search`` is set the handler runs the airport-match
        query. Empty DB → empty list, but the key itself remains a
        list (never null / missing) — the frontend indexes into it.
        """
        r = app_client_fastapi.get(
            "/api/v1/flight-schedules/filter-options",
            params={"search": "PEK"},
        )
        assert r.status_code == 200
        assert isinstance(r.json()["airports"], list)
