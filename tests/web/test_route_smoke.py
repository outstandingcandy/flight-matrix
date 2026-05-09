"""HTTP smoke tests for every route in web_app.py.

Goal: confirm every registered route actually responds — doesn't 500 on
empty-DB input, doesn't raise a Jinja error, doesn't reference a missing
service. The tests deliberately assert **status codes and response shape
only**, not business logic. Business-logic coverage is a separate layer.

Every test runs against an in-memory SQLite DB. No network calls, no
Cognito, no FR24, no Bedrock. Tests are ordered by blueprint / URL
prefix to make diffs easy to read.

What "OK" means here:
  - 2xx if the route serves content without inputs that require setup.
  - 3xx if the route legitimately redirects (e.g. /logout).
  - 4xx if the route requires parameters or a body we aren't supplying.
  - Never 5xx. A 5xx is what we're trying to catch.
"""

from __future__ import annotations

from typing import Any

import pytest

# Allowed status classes for a smoke-test probe. 5xx always fails.
OK = {200, 201, 204, 301, 302, 303, 307, 308, 400, 401, 403, 404, 405, 409, 422}


def _assert_not_5xx(r: Any, method: str, path: str) -> None:
    assert r.status_code < 500, (
        f"{method} {path} returned {r.status_code} — body: {r.get_data(as_text=True)[:300]}"
    )
    assert r.status_code in OK, (
        f"{method} {path} returned unexpected {r.status_code} — "
        f"body: {r.get_data(as_text=True)[:200]}"
    )


# ---------------------------------------------------------------------------
# Page routes (HTML)
# ---------------------------------------------------------------------------


PAGE_ROUTES = [
    "/",
    "/dashboard",
    "/airport-board",
    "/search-track",
    "/flight-schedules",
    "/admin",
    "/admin/dashboard",
    "/admin/users",
    "/admin/reports",
    "/admin/track",
    "/admin/filters",
    "/admin/aircraft-query",
    "/admin/scraped-data",
    "/admin/scraper-status",
    "/aircraft/N703PA",
    "/aircraft-type/A380",
    "/airport/JFK",
    "/user/test@example.com/dashboard",
    "/user/test@example.com/filters",
]


@pytest.mark.parametrize("path", PAGE_ROUTES)
def test_page_route_renders(app_client, path: str) -> None:
    r = app_client.get(path, follow_redirects=False)
    _assert_not_5xx(r, "GET", path)


# ---------------------------------------------------------------------------
# /api/aircraft/* (15 routes)
# ---------------------------------------------------------------------------


# Routes marked `xfail` emit Postgres-specific SQL or reference columns that
# don't live in the SQLAlchemy ORM models (only in Postgres migrations).
# They pass against real Postgres in production; SQLite 500s. Tracked as
# portability debt — remove the xfail once the SQL is dialect-agnostic.
API_AIRCRAFT_GET_ROUTES = [
    "/api/aircraft/search",
    "/api/aircraft/search?q=test",
    "/api/aircraft/tracks/N703PA",
    "/api/aircraft/recent",
    "/api/aircraft/types",
    "/api/aircraft/types/A380",
    pytest.param(
        "/api/aircraft/types/A380/instances",
        marks=pytest.mark.xfail(reason="Postgres-only SQL"),
    ),
    "/api/aircraft/unique",
    "/api/aircraft/static",
    "/api/aircraft/static/N703PA",
    pytest.param(
        "/api/aircraft/static/stats",
        marks=pytest.mark.xfail(reason="aircraft_static_info.is_military column not in ORM"),
    ),
    "/api/aircraft/N703PA/live",
    "/api/aircraft/N703PA/details",
    "/api/aircraft/N703PA/history",
    "/api/aircraft/N703PA/flight-dates",
    "/api/aircraft/N703PA/recent-flights",
    "/api/aircraft/N703PA/images",
    pytest.param(
        "/api/aircraft/N703PA/static-info",
        marks=pytest.mark.xfail(reason="Postgres-only SQL or missing columns"),
    ),
]


@pytest.mark.parametrize("path", API_AIRCRAFT_GET_ROUTES)
def test_api_aircraft_get(app_client, path: str) -> None:
    r = app_client.get(path, follow_redirects=False)
    _assert_not_5xx(r, "GET", path)


def test_api_aircraft_static_batch_empty(app_client) -> None:
    # POST /api/aircraft/static/batch expects a JSON body with registrations.
    r = app_client.post(
        "/api/aircraft/static/batch", json={"registrations": []}, follow_redirects=False
    )
    _assert_not_5xx(r, "POST", "/api/aircraft/static/batch")


def test_api_aircraft_static_batch_with_value(app_client) -> None:
    r = app_client.post(
        "/api/aircraft/static/batch",
        json={"registrations": ["N703PA"]},
        follow_redirects=False,
    )
    _assert_not_5xx(r, "POST", "/api/aircraft/static/batch")


# ---------------------------------------------------------------------------
# /api/airports/* (5 routes)
# ---------------------------------------------------------------------------


API_AIRPORT_GET_ROUTES = [
    "/api/airports/search?q=JFK",
    "/api/airports/JFK",
    "/api/airports/JFK/nearby",
    "/api/airports/JFK/realtime-aircraft",
    "/api/airports/popular",
]


@pytest.mark.parametrize("path", API_AIRPORT_GET_ROUTES)
def test_api_airport_get(app_client, path: str) -> None:
    r = app_client.get(path, follow_redirects=False)
    _assert_not_5xx(r, "GET", path)


# ---------------------------------------------------------------------------
# /api/search/* (3 routes)
# ---------------------------------------------------------------------------


API_SEARCH_GET_ROUTES = [
    pytest.param(
        "/api/search/unified?q=test",
        marks=pytest.mark.xfail(reason="Postgres-only SQL"),
    ),
    "/api/search/suggestions?q=test",
    "/api/search/aircraft?q=test",
]


@pytest.mark.parametrize("path", API_SEARCH_GET_ROUTES)
def test_api_search_get(app_client, path: str) -> None:
    r = app_client.get(path, follow_redirects=False)
    _assert_not_5xx(r, "GET", path)


# ---------------------------------------------------------------------------
# /api/flight-schedules, /api/flight/trail, /api/statistics (5 routes)
# ---------------------------------------------------------------------------


API_MISC_GET_ROUTES = [
    "/api/statistics",
    "/api/flight-schedules",
    "/api/flight-schedules?airport_code=JFK",
    "/api/flight-schedules/filter-options",
    "/api/flight/trail/abc123",
]


@pytest.mark.parametrize("path", API_MISC_GET_ROUTES)
def test_api_misc_get(app_client, path: str) -> None:
    r = app_client.get(path, follow_redirects=False)
    _assert_not_5xx(r, "GET", path)


# ---------------------------------------------------------------------------
# /api/user/<email>/* (10 routes)
# ---------------------------------------------------------------------------


EMAIL = "test@example.com"

API_USER_GET_ROUTES = [
    f"/api/user/{EMAIL}/profile",
    f"/api/user/{EMAIL}/usage",
    f"/api/user/{EMAIL}/cooldowns",
    f"/api/user/{EMAIL}/filters",
    f"/api/user/{EMAIL}/filters/1",
]


@pytest.mark.parametrize("path", API_USER_GET_ROUTES)
def test_api_user_get(app_client, path: str) -> None:
    r = app_client.get(path, follow_redirects=False)
    _assert_not_5xx(r, "GET", path)


def test_api_user_settings_put(app_client) -> None:
    r = app_client.put(f"/api/user/{EMAIL}/settings", json={"name": "Test"}, follow_redirects=False)
    _assert_not_5xx(r, "PUT", f"/api/user/{EMAIL}/settings")


def test_api_user_create_filter(app_client) -> None:
    r = app_client.post(
        f"/api/user/{EMAIL}/filters",
        json={"name": "test", "filter_sql": "is_military = 1"},
        follow_redirects=False,
    )
    _assert_not_5xx(r, "POST", f"/api/user/{EMAIL}/filters")


def test_api_user_update_filter(app_client) -> None:
    r = app_client.put(
        f"/api/user/{EMAIL}/filters/999",
        json={"name": "updated"},
        follow_redirects=False,
    )
    _assert_not_5xx(r, "PUT", f"/api/user/{EMAIL}/filters/999")


def test_api_user_delete_filter(app_client) -> None:
    r = app_client.delete(f"/api/user/{EMAIL}/filters/999", follow_redirects=False)
    _assert_not_5xx(r, "DELETE", f"/api/user/{EMAIL}/filters/999")


def test_api_user_test_filter(app_client) -> None:
    r = app_client.post(
        f"/api/user/{EMAIL}/filters/test",
        json={"filter_sql": "is_military = 1"},
        follow_redirects=False,
    )
    _assert_not_5xx(r, "POST", f"/api/user/{EMAIL}/filters/test")


# ---------------------------------------------------------------------------
# /api/admin/* (24 routes)
# ---------------------------------------------------------------------------


XFAIL_PG_ONLY = pytest.mark.xfail(reason="Postgres-only SQL")
API_ADMIN_GET_ROUTES = [
    pytest.param("/api/admin/aircraft-query/N703PA", marks=XFAIL_PG_ONLY),
    "/api/admin/users",
    "/api/admin/users/stats",
    "/api/admin/users/999",
    pytest.param("/api/admin/aircraft", marks=XFAIL_PG_ONLY),
    pytest.param("/api/admin/aircraft/stats", marks=XFAIL_PG_ONLY),
    "/api/admin/aircraft/types",
    pytest.param("/api/admin/aircraft/liveries", marks=XFAIL_PG_ONLY),
    "/api/admin/aircraft/registrations",
    pytest.param("/api/admin/reports", marks=XFAIL_PG_ONLY),
    "/api/admin/reports/stats",
    "/api/admin/reports/abc123/detail",
    pytest.param("/api/admin/scraped-data/xiaohongshu/stats", marks=XFAIL_PG_ONLY),
    pytest.param("/api/admin/scraped-data/xiaohongshu/notes", marks=XFAIL_PG_ONLY),
    pytest.param("/api/admin/scraped-data/xiaohongshu/notes/note123", marks=XFAIL_PG_ONLY),
    "/api/admin/scraped-data/fr24/stats",
    "/api/admin/scraped-data/fr24/flights",
    "/api/admin/scraped-data/jetphotos/stats",
    "/api/admin/scraped-data/jetphotos/images",
    pytest.param("/api/admin/scraper/stats", marks=XFAIL_PG_ONLY),
    pytest.param("/api/admin/scraper/workers", marks=XFAIL_PG_ONLY),
    pytest.param("/api/admin/scraper/recent-tasks", marks=XFAIL_PG_ONLY),
]


@pytest.mark.parametrize("path", API_ADMIN_GET_ROUTES)
def test_api_admin_get(app_client, path: str) -> None:
    r = app_client.get(path, follow_redirects=False)
    _assert_not_5xx(r, "GET", path)


def test_api_admin_create_user(app_client) -> None:
    r = app_client.post(
        "/api/admin/users",
        json={"email": "new@example.com", "name": "New"},
        follow_redirects=False,
    )
    _assert_not_5xx(r, "POST", "/api/admin/users")


def test_api_admin_update_user(app_client) -> None:
    r = app_client.put("/api/admin/users/999", json={"name": "updated"}, follow_redirects=False)
    _assert_not_5xx(r, "PUT", "/api/admin/users/999")


def test_api_admin_delete_user(app_client) -> None:
    r = app_client.delete("/api/admin/users/999", follow_redirects=False)
    _assert_not_5xx(r, "DELETE", "/api/admin/users/999")


def test_api_admin_regenerate_api_key(app_client) -> None:
    r = app_client.post("/api/admin/users/999/api-key", follow_redirects=False)
    _assert_not_5xx(r, "POST", "/api/admin/users/999/api-key")


def test_api_admin_import_data(app_client) -> None:
    r = app_client.post(
        "/api/admin/import-data",
        json={"snapshots": []},
        follow_redirects=False,
    )
    _assert_not_5xx(r, "POST", "/api/admin/import-data")


# ---------------------------------------------------------------------------
# Static + data serving
# ---------------------------------------------------------------------------


def test_static_file_404_is_not_500(app_client) -> None:
    r = app_client.get("/data/does-not-exist.jpg", follow_redirects=False)
    _assert_not_5xx(r, "GET", "/data/does-not-exist.jpg")
