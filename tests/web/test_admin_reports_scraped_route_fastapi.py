"""Semantic + regression coverage for ``/api/v1/admin/reports*`` and
``/api/v1/admin/scraped-data/*`` (FastAPI).

10 endpoints across four dashboards (reports, xiaohongshu, fr24,
jetphotos), all router-level ``Depends(require_admin)`` gated. Smoke
covers not-5xx already; this layer adds:

- Non-admin 403 regression on every endpoint.
- Response body shape for the four "*/stats" dashboards the admin UI
  indexes into. Empty DB → zeros in every count, but the *keys* must
  remain present.
- 404 on an unknown ``aircraft_hex`` for the detail endpoint (empty
  DB, so any hex is unknown).
"""

from __future__ import annotations

from typing import Any

import pytest

ROUTES = [
    ("GET", "/api/v1/admin/reports"),
    ("GET", "/api/v1/admin/reports/stats"),
    ("GET", "/api/v1/admin/reports/deadbeef/detail"),
    ("GET", "/api/v1/admin/scraped-data/xiaohongshu/stats"),
    ("GET", "/api/v1/admin/scraped-data/xiaohongshu/notes"),
    ("GET", "/api/v1/admin/scraped-data/xiaohongshu/notes/nonexistent-id"),
    ("GET", "/api/v1/admin/scraped-data/fr24/stats"),
    ("GET", "/api/v1/admin/scraped-data/fr24/flights"),
    ("GET", "/api/v1/admin/scraped-data/jetphotos/stats"),
    ("GET", "/api/v1/admin/scraped-data/jetphotos/images"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_non_admin_gets_403(
    app_client_fastapi: Any,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
) -> None:
    monkeypatch.setenv("LOCAL_DEV_GROUPS", "")
    r = app_client_fastapi.request(method, path, follow_redirects=False)
    assert r.status_code == 403, f"{method} {path} → {r.status_code}"


class TestDashboardStatsShape:
    """The four /stats endpoints all follow the same envelope. Assert
    the specific stat keys the admin UI reads."""

    def test_report_stats(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.get("/api/v1/admin/reports/stats")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        assert "multi_user_mode" in body
        stats = body["stats"]
        for key in ("total_tracked", "reports_today", "total_sent", "unique_aircraft"):
            assert key in stats, f"missing stats key {key!r}"
            assert isinstance(stats[key], int)

    def test_xhs_stats(self, app_client_fastapi: Any) -> None:
        """XHS stats are flat on the body (not nested under ``stats``) —
        the handler returns ``{success, notes_count, authors_count,
        images_count, latest_scrape}`` directly."""
        r = app_client_fastapi.get("/api/v1/admin/scraped-data/xiaohongshu/stats")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        for key in ("notes_count", "authors_count", "images_count", "latest_scrape"):
            assert key in body, f"missing key {key!r}"

    def test_fr24_stats(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.get("/api/v1/admin/scraped-data/fr24/stats")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        for key in ("flights_count", "airports_count", "today_count", "latest_scrape"):
            assert key in body

    def test_jetphotos_stats(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.get("/api/v1/admin/scraped-data/jetphotos/stats")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        for key in ("images_count", "aircraft_count", "photographers_count", "latest_scrape"):
            assert key in body


class TestListEnvelopes:
    """The four /list endpoints all return pagination envelopes. Frontend
    reads {list_key, total, page, pages}."""

    def test_report_list(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.get("/api/v1/admin/reports")
        assert r.status_code == 200, r.text
        body = r.json()
        for key in ("total", "page"):
            assert key in body

    def test_xhs_notes(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.get("/api/v1/admin/scraped-data/xiaohongshu/notes")
        assert r.status_code == 200, r.text
        assert isinstance(r.json().get("notes", []), list)

    def test_fr24_flights(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.get("/api/v1/admin/scraped-data/fr24/flights")
        assert r.status_code == 200, r.text
        assert isinstance(r.json().get("flights", []), list)

    def test_jetphotos_images(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.get("/api/v1/admin/scraped-data/jetphotos/images")
        assert r.status_code == 200, r.text
        assert isinstance(r.json().get("images", []), list)


class TestDetailFor404:
    def test_report_detail_missing_aircraft_returns_404(self, app_client_fastapi: Any) -> None:
        """Empty DB has no cooldown rows; the handler documents 404 for
        an unknown hex. Guard the not-found path."""
        r = app_client_fastapi.get("/api/v1/admin/reports/deadbeef/detail")
        assert r.status_code == 404, r.text

    def test_xhs_note_detail_missing_returns_404(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.get("/api/v1/admin/scraped-data/xiaohongshu/notes/nonexistent-id")
        assert r.status_code == 404
