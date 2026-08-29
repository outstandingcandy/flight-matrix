"""Front-end parity smoke tests.

`test_route_smoke.py` exercises every route the Flask app registers.
This module is narrower: it only hits the endpoints that the HTML + JS
in `web_templates/` and `web_static/js/` actually `fetch()`, and it
uses **the exact query-string shapes the frontend sends**.

Why this exists: an endpoint can be route-smoke green but still broken
for the frontend if it requires a specific query param that the generic
smoke test doesn't provide. e.g. a page issues
`GET /api/aircraft/recent?hours=1&limit=100` and the handler 500s
when `hours` is missing. The route smoke test uses bare
`/api/aircraft/recent` and wouldn't catch it.

All fetch() URLs were enumerated from web_templates/ and web_static/js/
on 2026-05-09 and checked in below. If you add a new fetch() call,
add it here too.

Test policy: same as route_smoke — status_code < 500 is pass; 4xx is
fine (the endpoint may require more setup than the test provides).
5xx means the handler crashed.
"""

from __future__ import annotations

from typing import Any

import pytest

OK = {200, 201, 204, 301, 302, 303, 307, 308, 400, 401, 403, 404, 405, 409, 422}


def _assert_not_5xx(r: Any, method: str, path: str) -> None:
    assert r.status_code < 500, (
        f"{method} {path} returned {r.status_code} — body: {r.text[:300]}"
    )
    assert r.status_code in OK, (
        f"{method} {path} returned unexpected {r.status_code} — "
        f"body: {r.text[:200]}"
    )


EMAIL = "test@example.com"


# Endpoints the frontend uses with their real query parameters.
# Shape: (method, path, expected_query_params_or_body).
FRONTEND_GET_CALLS = [
    # Page data — hit from home.html, dashboard, airport-board etc.
    "/api/statistics",
    "/api/aircraft/recent?hours=1&limit=100",
    "/api/aircraft/search?is_military=true&limit=200",
    "/api/aircraft/unique?days=7",
    "/api/aircraft/tracks/N703PA",
    "/api/search/unified?q=test&limit=10",
    "/api/search/suggestions?q=test",
    # Airport pages
    "/api/airports/search?q=JFK&limit=10",
    "/api/airports/JFK",
    "/api/airports/JFK/realtime-aircraft?radius_km=50",
    # Aircraft detail page
    "/api/aircraft/N703PA/details",
    "/api/aircraft/N703PA/history?limit=500",
    "/api/aircraft/N703PA/images",
    "/api/aircraft/N703PA/recent-flights",
    "/api/aircraft/static/N703PA",
    # Aircraft-type page
    "/api/aircraft/types/A380",
    "/api/aircraft/types/A380/instances?offset=0&limit=20",
    # Flight schedules page
    "/api/flight-schedules",
    "/api/flight-schedules/filter-options?search=test",
    # `/api/flight/trail/<fr24_id>` intentionally omitted — proxy to
    # FR24's clickhandler, whose 403 for bogus IDs the handler correctly
    # returns as 502. See the identical note in test_route_smoke.py.
    # User dashboard + filter pages
    f"/api/user/{EMAIL}/profile",
    f"/api/user/{EMAIL}/usage",
    f"/api/user/{EMAIL}/cooldowns",
    f"/api/user/{EMAIL}/filters",
    f"/api/user/{EMAIL}/filters/1",
    # Admin dashboard + subpages
    "/api/admin/users",
    "/api/admin/users/stats",
    "/api/admin/users/999",
    "/api/admin/users?limit=20&offset=0",
    "/api/admin/aircraft",
    "/api/admin/aircraft/stats",
    "/api/admin/aircraft/types?search=A",
    "/api/admin/aircraft/liveries?search=test",
    "/api/admin/aircraft/registrations?search=N",
    "/api/admin/aircraft-query/N703PA",
    "/api/admin/reports/stats",
    "/api/admin/reports/abc123/detail",
    "/api/admin/scraped-data/xiaohongshu/stats",
    "/api/admin/scraped-data/xiaohongshu/notes",
    "/api/admin/scraped-data/xiaohongshu/notes/note123",
    "/api/admin/scraped-data/fr24/stats",
    "/api/admin/scraped-data/fr24/flights",
    "/api/admin/scraped-data/jetphotos/stats",
    "/api/admin/scraped-data/jetphotos/images",
    "/api/admin/scraper/stats",
    "/api/admin/scraper/workers",
]


@pytest.mark.parametrize("path", FRONTEND_GET_CALLS)
def test_frontend_get_endpoint(app_client, path: str) -> None:
    """Every endpoint the frontend fetches via GET returns non-5xx."""
    r = app_client.get(path, follow_redirects=False)
    _assert_not_5xx(r, "GET", path)


# POST endpoints — hit with representative bodies.


def test_frontend_batch_static_info(app_client) -> None:
    r = app_client.post(
        "/api/aircraft/static/batch",
        json={"registrations": ["N703PA"]},
        follow_redirects=False,
    )
    _assert_not_5xx(r, "POST", "/api/aircraft/static/batch")


def test_frontend_set_session(app_client) -> None:
    # Called by auth_callback.html with {id_token, access_token, refresh_token}.
    # With SKIP_AUTH on, this returns 403, which is non-5xx.
    r = app_client.post(
        "/auth/set-session",
        json={"id_token": "test", "access_token": "test", "refresh_token": "test"},
        follow_redirects=False,
    )
    _assert_not_5xx(r, "POST", "/auth/set-session")


def test_frontend_create_user_filter(app_client) -> None:
    r = app_client.post(
        f"/api/user/{EMAIL}/filters",
        json={
            "name": "test-filter",
            "filter_sql": "is_military = 1",
            "description": "from frontend test",
        },
        follow_redirects=False,
    )
    _assert_not_5xx(r, "POST", f"/api/user/{EMAIL}/filters")


def test_frontend_test_user_filter(app_client) -> None:
    r = app_client.post(
        f"/api/user/{EMAIL}/filters/test",
        json={"filter_sql": "is_military = 1"},
        follow_redirects=False,
    )
    _assert_not_5xx(r, "POST", f"/api/user/{EMAIL}/filters/test")


def test_frontend_admin_create_user(app_client) -> None:
    r = app_client.post(
        "/api/admin/users",
        json={"email": "newuser@example.com", "name": "New User"},
        follow_redirects=False,
    )
    _assert_not_5xx(r, "POST", "/api/admin/users")


def test_frontend_admin_regenerate_api_key(app_client) -> None:
    r = app_client.post("/api/admin/users/999/api-key", follow_redirects=False)
    _assert_not_5xx(r, "POST", "/api/admin/users/999/api-key")


# PUT endpoints.


def test_frontend_update_settings(app_client) -> None:
    r = app_client.put(
        f"/api/user/{EMAIL}/settings",
        json={"name": "Test", "email_notifications": True},
        follow_redirects=False,
    )
    _assert_not_5xx(r, "PUT", f"/api/user/{EMAIL}/settings")


def test_frontend_update_user_filter(app_client) -> None:
    r = app_client.put(
        f"/api/user/{EMAIL}/filters/1",
        json={"name": "renamed"},
        follow_redirects=False,
    )
    _assert_not_5xx(r, "PUT", f"/api/user/{EMAIL}/filters/1")


def test_frontend_admin_update_user(app_client) -> None:
    r = app_client.put(
        "/api/admin/users/999",
        json={"name": "Updated"},
        follow_redirects=False,
    )
    _assert_not_5xx(r, "PUT", "/api/admin/users/999")


# DELETE endpoints.


def test_frontend_delete_user_filter(app_client) -> None:
    r = app_client.delete(f"/api/user/{EMAIL}/filters/1", follow_redirects=False)
    _assert_not_5xx(r, "DELETE", f"/api/user/{EMAIL}/filters/1")


def test_frontend_admin_delete_user(app_client) -> None:
    r = app_client.delete("/api/admin/users/999", follow_redirects=False)
    _assert_not_5xx(r, "DELETE", "/api/admin/users/999")
