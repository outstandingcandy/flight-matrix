"""Not-5xx smoke sweep across every migrated FastAPI GET endpoint.

Companion to :mod:`tests.web.test_route_smoke` which runs the same
sweep against the Flask app. Once the migration finishes the Flask
version comes out; keeping both in parallel while the co-existence
window is open catches divergence early — if a route is up on Flask
but 5xx on FastAPI the diff shows exactly one failing case here.

**What this checks.** The empty tmp-file SQLite the fixture ships
with means most handlers return an empty list, a "not found" 404, or
similar — that is *fine*. The assertion is only "no crashed
handler". Any 5xx means the ASGI app raised something the exception
handler flattened to 500, and that almost always means:

- A helper in ``web_app.py`` the migrated handler still delegates to
  went missing / changed signature.
- A template referenced by an HTML shell can't find its context
  variables.
- A dependency import broke (StaticFiles, template globals, etc.).

**What this doesn't check.** Response body shape or business logic.
Targeted tests (``test_admin_users_route_fastapi.py``,
``test_native_auth_route_fastapi.py``, etc.) cover semantics.

**HTML shells expect a real DB.** ``/aircraft/{registration}`` and
similar HTML routes query for the row, and rendering the 404 template
also touches the DB. Same tmp SQLite from ``conftest.py`` gives them
enough to render — the template renders "Aircraft not found" and the
handler returns 200 with the shell HTML. That's the intended behaviour
for SPA-style pages.
"""

from __future__ import annotations

from typing import Any

import pytest


def _assert_not_5xx(response: Any, label: str) -> None:
    """Fail with a useful message on 5xx; every other status is fine."""
    assert response.status_code < 500, (
        f"{label} → HTTP {response.status_code}: {response.text[:400]}"
    )


# ---------------------------------------------------------------------------
# GET endpoints.


AIRCRAFT_GET = [
    "/api/aircraft/search",
    "/api/aircraft/search?q=test",
    "/api/aircraft/tracks/N703PA",
    "/api/aircraft/recent",
    "/api/aircraft/types",
    "/api/aircraft/types/A380",
    "/api/aircraft/types/A380/instances",
    "/api/aircraft/unique",
    "/api/aircraft/static",
    "/api/aircraft/static/N703PA",
    "/api/aircraft/static/stats",
    "/api/aircraft/N703PA/live",
    "/api/aircraft/N703PA/details",
    "/api/aircraft/N703PA/history",
    "/api/aircraft/N703PA/flight-dates",
    "/api/aircraft/N703PA/recent-flights",
    "/api/aircraft/N703PA/images",
    "/api/aircraft/N703PA/static-info",
]

AIRPORT_GET = [
    "/api/statistics",
    "/api/airports/search?q=JFK",
    "/api/airports/JFK",
    "/api/airports/JFK/nearby",
    "/api/airports/popular",
]

SEARCH_GET = [
    "/api/search/unified?q=test",
    "/api/search/suggestions?q=test",
    "/api/search/aircraft?q=test",
]

USER_GET = [
    "/api/user/test@example.com/profile",
    "/api/user/test@example.com/usage",
    "/api/user/test@example.com/cooldowns",
    "/api/user/test@example.com/filters",
]

FLIGHT_SCHEDULES_GET = [
    "/api/flight-schedules",
    "/api/flight-schedules/filter-options",
]

# All admin_* routes are router-level `Depends(require_admin)`-gated.
# The fixture ships with a mock admin (LOCAL_DEV_GROUPS=admins,...) so
# these come through under SKIP_AUTH.
ADMIN_AIRCRAFT_GET = [
    "/api/admin/aircraft",
    "/api/admin/aircraft/stats",
    "/api/admin/aircraft/types",
    "/api/admin/aircraft/liveries",
    "/api/admin/aircraft/registrations",
    "/api/admin/aircraft-query/N703PA",
]

ADMIN_REPORTS_SCRAPED_GET = [
    "/api/admin/reports",
    "/api/admin/reports/stats",
    "/api/admin/reports/deadbeef/detail",
    "/api/admin/scraped-data/xiaohongshu/stats",
    "/api/admin/scraped-data/xiaohongshu/notes",
    "/api/admin/scraped-data/xiaohongshu/notes/nonexistent-id",
    "/api/admin/scraped-data/fr24/stats",
    "/api/admin/scraped-data/fr24/flights",
    "/api/admin/scraped-data/jetphotos/stats",
    "/api/admin/scraped-data/jetphotos/images",
]

ADMIN_SCRAPER_GET = [
    "/api/admin/scraper/stats",
    "/api/admin/scraper/workers",
    "/api/admin/scraper/recent-tasks",
]

# HTML shells — 17 pages migrated in batch 7. Empty DB is enough because
# the pages are SPA shells: their JS fetches from /api/* after render.
PAGE_GET = [
    "/",
    "/home",
    "/dashboard",
    "/flight-schedules",
    "/aircraft/N703PA",
    "/aircraft-type/A380",
    "/search-track",
    "/user/test@example.com/dashboard",
    "/user/test@example.com/filters",
]


@pytest.mark.parametrize("path", AIRCRAFT_GET)
def test_aircraft_endpoints_not_5xx(app_client_fastapi: Any, path: str) -> None:
    r = app_client_fastapi.get(path, follow_redirects=False)
    _assert_not_5xx(r, f"GET {path}")


@pytest.mark.parametrize("path", AIRPORT_GET)
def test_airport_endpoints_not_5xx(app_client_fastapi: Any, path: str) -> None:
    r = app_client_fastapi.get(path, follow_redirects=False)
    _assert_not_5xx(r, f"GET {path}")


@pytest.mark.parametrize("path", SEARCH_GET)
def test_search_endpoints_not_5xx(app_client_fastapi: Any, path: str) -> None:
    r = app_client_fastapi.get(path, follow_redirects=False)
    _assert_not_5xx(r, f"GET {path}")


@pytest.mark.parametrize("path", USER_GET)
def test_user_endpoints_not_5xx(app_client_fastapi: Any, path: str) -> None:
    r = app_client_fastapi.get(path, follow_redirects=False)
    _assert_not_5xx(r, f"GET {path}")


@pytest.mark.parametrize("path", PAGE_GET)
def test_html_shells_not_5xx(app_client_fastapi: Any, path: str) -> None:
    r = app_client_fastapi.get(path, follow_redirects=False)
    _assert_not_5xx(r, f"GET {path}")


@pytest.mark.parametrize("path", FLIGHT_SCHEDULES_GET)
def test_flight_schedules_endpoints_not_5xx(app_client_fastapi: Any, path: str) -> None:
    r = app_client_fastapi.get(path, follow_redirects=False)
    _assert_not_5xx(r, f"GET {path}")


@pytest.mark.parametrize("path", ADMIN_AIRCRAFT_GET)
def test_admin_aircraft_endpoints_not_5xx(app_client_fastapi: Any, path: str) -> None:
    r = app_client_fastapi.get(path, follow_redirects=False)
    _assert_not_5xx(r, f"GET {path}")


@pytest.mark.parametrize("path", ADMIN_REPORTS_SCRAPED_GET)
def test_admin_reports_scraped_endpoints_not_5xx(app_client_fastapi: Any, path: str) -> None:
    r = app_client_fastapi.get(path, follow_redirects=False)
    _assert_not_5xx(r, f"GET {path}")


@pytest.mark.parametrize("path", ADMIN_SCRAPER_GET)
def test_admin_scraper_endpoints_not_5xx(app_client_fastapi: Any, path: str) -> None:
    r = app_client_fastapi.get(path, follow_redirects=False)
    _assert_not_5xx(r, f"GET {path}")


# ---------------------------------------------------------------------------
# Admin-only guard: verify the require_admin dependency covers each router.
# One shot per router — one endpoint each is enough to catch a
# router-level dependency slipping off.


ADMIN_ROUTER_SENTINELS = [
    ("GET", "/api/admin/aircraft"),
    ("GET", "/api/admin/reports"),
    ("GET", "/api/admin/scraper/stats"),
]


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTER_SENTINELS)
def test_admin_routers_deny_non_admin(
    app_client_fastapi: Any,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
) -> None:
    """Router-level ``Depends(require_admin)`` sentinel — one endpoint per
    admin router. If the dependency drops off any of the three admin
    routers, the corresponding case here fires."""
    monkeypatch.setenv("LOCAL_DEV_GROUPS", "")
    r = app_client_fastapi.request(method, path, follow_redirects=False)
    assert r.status_code == 403, f"{method} {path} → {r.status_code} (expected 403)"


# ---------------------------------------------------------------------------
# POST endpoints — targeted, since arbitrary POSTs would be validation-heavy.


def test_aircraft_static_batch_empty_body(app_client_fastapi: Any) -> None:
    """POST /api/aircraft/static/batch with empty registrations list."""
    r = app_client_fastapi.post(
        "/api/aircraft/static/batch",
        json={"registrations": []},
        follow_redirects=False,
    )
    _assert_not_5xx(r, "POST /api/aircraft/static/batch")


def test_aircraft_static_batch_with_value(app_client_fastapi: Any) -> None:
    r = app_client_fastapi.post(
        "/api/aircraft/static/batch",
        json={"registrations": ["N703PA"]},
        follow_redirects=False,
    )
    _assert_not_5xx(r, "POST /api/aircraft/static/batch")


# ---------------------------------------------------------------------------
# /data/{path} — batch 7's redirect helper.


def test_data_path_redirects_when_configured(
    app_client_fastapi: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/data/{filepath}`` is expected to 302 to the CDN when
    ``MEDIA_BASE_URL`` is set, and either 404 or 200-from-filesystem
    when it isn't. Only asserts not-5xx — the exact behaviour depends
    on env, and semantic tests live elsewhere.
    """
    r = app_client_fastapi.get("/data/anything.jpg", follow_redirects=False)
    _assert_not_5xx(r, "GET /data/anything.jpg")
