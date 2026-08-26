"""FastAPI port of the remaining HTML page shells + ``/data/{path}``.

Stage 0 lift-and-shift. Seventeen thin handlers, all of which just
render a template (or in one case redirect / serve a file). Two auth
gates in play — ``require_login`` for user-facing pages, ``require_admin``
for admin-only pages. The Flask originals were inconsistent (some admin
pages had only ``@login_required``); this port preserves that exactly
rather than tightening — same-batch tightening is out of scope, and the
missing-admin-gate pages are UI shells that render only from JSON APIs
that themselves check admin.

User pages
----------

- GET /                       — home_required → home.html
- GET /dashboard              — login_required → index.html
- GET /airport-board          — login_required → airport_board.html
- GET /search-track           — login_required → search_track.html
- GET /flight-schedules       — 302 to /  (legacy URL)
- GET /user/{email}/dashboard — login_required → user_dashboard.html
- GET /user/{email}/filters   — login_required → user_filters.html

Admin pages
-----------

- GET /admin                  — admin_required → admin_dashboard.html
- GET /admin/users            — login_required → admin_users.html
- GET /admin/reports          — login_required → admin_reports.html
- GET /admin/track            — admin_required → search_track.html
- GET /admin/dashboard        — admin_required → user_dashboard.html
- GET /admin/filters          — admin_required → user_filters.html
- GET /admin/aircraft-query   — admin_required → admin_aircraft_query.html
- GET /admin/scraped-data     — admin_required → admin_scraped_data.html
- GET /admin/scraper-status   — admin_required → admin_scraper_status.html

File serving
------------

- GET /data/{filepath}        — cloud: 302 to CDN base_url; local: send file
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from starlette.responses import Response

from src.auth.dependencies import require_admin, require_login

logger = logging.getLogger("web.pages")

router = APIRouter(tags=["pages"])


def _render(request: Request, template: str, ctx: dict[str, Any] | None = None) -> Response:
    """Shared shortcut — request-first ``TemplateResponse`` with the
    module-scoped templates env populated in :mod:`app`.
    """
    templates = request.app.state.templates
    return templates.TemplateResponse(request, template, ctx or {})


# ---------------------------------------------------------------------------
# User pages
# ---------------------------------------------------------------------------


@router.get("/", name="home")
async def home(request: Request, _user: dict[str, Any] = Depends(require_login)) -> Response:
    """Google-style search home. Same as ``web_app.py:574``."""
    return _render(request, "home.html")


@router.get("/dashboard", name="dashboard")
async def dashboard(request: Request, _user: dict[str, Any] = Depends(require_login)) -> Response:
    """Original dashboard. Same as ``web_app.py:581``."""
    return _render(request, "index.html")


@router.get("/airport-board", name="airport_board")
async def airport_board(
    request: Request, _user: dict[str, Any] = Depends(require_login)
) -> Response:
    """Airport intel board. Same as ``web_app.py:1506``."""
    return _render(request, "airport_board.html")


@router.get("/search-track", name="search_track")
async def search_track(
    request: Request, _user: dict[str, Any] = Depends(require_login)
) -> Response:
    """Search + flight track page. Same as ``web_app.py:1513``."""
    return _render(request, "search_track.html")


@router.get("/flight-schedules", name="flight_schedules_page")
async def flight_schedules_page() -> RedirectResponse:
    """Legacy URL — redirects to ``/``. Same as ``web_app.py:5181``.

    No auth gate on the redirect itself; the destination (``/``) will
    require login.
    """
    return RedirectResponse("/", status_code=status.HTTP_302_FOUND)


@router.get("/user/{email}/dashboard", name="user_dashboard_page")
async def user_dashboard_page(
    request: Request,
    email: str,
    _user: dict[str, Any] = Depends(require_login),
) -> Response:
    """User dashboard shell. Same as ``web_app.py:3166``."""
    return _render(request, "user_dashboard.html", {"email": email})


@router.get("/user/{email}/filters", name="user_filters_page")
async def user_filters_page(
    request: Request,
    email: str,
    _user: dict[str, Any] = Depends(require_login),
) -> Response:
    """User filter management shell. Same as ``web_app.py:3173``."""
    return _render(request, "user_filters.html", {"email": email})


# ---------------------------------------------------------------------------
# Admin pages
# ---------------------------------------------------------------------------


@router.get("/admin", name="admin_main_page")
async def admin_main_page(
    request: Request, _user: dict[str, Any] = Depends(require_admin)
) -> Response:
    """Admin main dashboard. Same as ``web_app.py:2705``."""
    return _render(request, "admin_dashboard.html")


@router.get("/admin/users", name="admin_users_page")
async def admin_users_page(
    request: Request, _user: dict[str, Any] = Depends(require_login)
) -> Response:
    """Admin user management page. Same as ``web_app.py:2712``.

    Note: Flask uses ``@login_required`` here, not ``@admin_required``.
    Preserved for stage 0 fidelity — the underlying
    ``/api/admin/users*`` endpoints do enforce admin (see batch 6a).
    """
    return _render(request, "admin_users.html")


@router.get("/admin/reports", name="admin_reports_page")
async def admin_reports_page(
    request: Request, _user: dict[str, Any] = Depends(require_login)
) -> Response:
    """Admin report history page. Same as ``web_app.py:2719``.

    Also ``@login_required`` on the Flask side, not ``@admin_required``.
    """
    return _render(request, "admin_reports.html")


@router.get("/admin/track", name="admin_track_page")
async def admin_track_page(
    request: Request, _user: dict[str, Any] = Depends(require_admin)
) -> Response:
    """Admin flight-track query. Same as ``web_app.py:2726``."""
    return _render(request, "search_track.html")


@router.get("/admin/dashboard", name="admin_dashboard_page")
async def admin_dashboard_page(
    request: Request, _user: dict[str, Any] = Depends(require_admin)
) -> Response:
    """Admin view of user dashboard. Same as ``web_app.py:2733``."""
    return _render(request, "user_dashboard.html")


@router.get("/admin/filters", name="admin_filters_page")
async def admin_filters_page(
    request: Request, _user: dict[str, Any] = Depends(require_admin)
) -> Response:
    """Admin view of filter management. Same as ``web_app.py:2740``."""
    return _render(request, "user_filters.html")


@router.get("/admin/aircraft-query", name="admin_aircraft_query_page")
async def admin_aircraft_query_page(
    request: Request, _user: dict[str, Any] = Depends(require_admin)
) -> Response:
    """Admin aircraft list + per-registration detail. Same as
    ``web_app.py:2747``. Which view shows is carried in the URL hash
    (``#aircraft=<reg>``), so a detail view can be linked to."""
    return _render(request, "admin_aircraft_query.html")


@router.get("/admin/scraped-data", name="admin_scraped_data_page")
async def admin_scraped_data_page(
    request: Request, _user: dict[str, Any] = Depends(require_admin)
) -> Response:
    """Admin scraped-data viewer. Same as ``web_app.py:4345``."""
    return _render(request, "admin_scraped_data.html")


@router.get("/admin/scraper-status", name="admin_scraper_status_page")
async def admin_scraper_status_page(
    request: Request, _user: dict[str, Any] = Depends(require_admin)
) -> Response:
    """Scraper status / queue monitor. Same as ``web_app.py:5749``."""
    return _render(request, "admin_scraper_status.html")


# ---------------------------------------------------------------------------
# Data file serving
# ---------------------------------------------------------------------------


@router.get("/data/{filepath:path}", name="serve_data_file")
async def serve_data_file(filepath: str) -> Response:
    """Serve aircraft images.

    Same as ``web_app.py:588``:
    - Cloud deployment (``resolve_media_base_url`` returns non-empty):
      302 to the CDN / public bucket at ``{base_url}/data/{filepath}``.
    - Local: read from ``./data/{filepath}`` off disk.

    Path parameter uses the ``{filepath:path}`` converter so slashes
    inside the tail don't split the parameter.
    """
    from src.storage import resolve_media_base_url

    base_url = resolve_media_base_url()

    if base_url:
        redirect_url = f"{base_url}/data/{filepath}"
        logger.debug("Redirecting to object storage: %s", redirect_url)
        return RedirectResponse(redirect_url, status_code=status.HTTP_302_FOUND)

    # Local development — resolve against the repo's data/ dir.
    # `Path.resolve()` + is_relative_to() prevents directory traversal
    # via `../` tail; matches Flask's send_from_directory guarantees.
    project_root = Path(__file__).resolve().parents[3]
    data_dir = (project_root / "data").resolve()
    candidate = (data_dir / filepath).resolve()
    if not candidate.is_relative_to(data_dir) or not candidate.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(candidate)
