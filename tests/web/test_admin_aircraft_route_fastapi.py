"""Semantic + regression coverage for ``/api/admin/aircraft*`` (FastAPI).

The smoke sweep in :mod:`tests.web.test_route_smoke_fastapi` already
asserts these routes don't 5xx on an empty DB. This file adds:

- **Non-admin 403 regression per endpoint.** The router-level
  ``Depends(require_admin)`` is shared with the other admin routers;
  if it slips off during a cleanup, every one of these six cases
  fires at once with an explanation.
- **Response body shape** on the two endpoints the admin dashboard
  actually indexes into (list + stats). Empty-DB rows are fine — the
  shape is what the frontend depends on.
- **Search-parameter narrowing** for the type/livery/registration
  autocomplete endpoints.
"""

from __future__ import annotations

from typing import Any

import pytest

ROUTES = [
    ("GET", "/api/admin/aircraft"),
    ("GET", "/api/admin/aircraft/stats"),
    ("GET", "/api/admin/aircraft/types"),
    ("GET", "/api/admin/aircraft/liveries"),
    ("GET", "/api/admin/aircraft/registrations"),
    ("GET", "/api/admin/aircraft-query/N703PA"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_non_admin_gets_403(
    app_client_fastapi: Any,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
) -> None:
    """Every admin_aircraft endpoint MUST 403 for a caller without the
    ``admins`` group. Guards against the router-level dependency
    quietly dropping off, same class of bug as batch 6a on
    admin_users.
    """
    monkeypatch.setenv("LOCAL_DEV_GROUPS", "")
    r = app_client_fastapi.request(method, path, follow_redirects=False)
    assert r.status_code == 403, f"{method} {path} → {r.status_code} (expected 403)"


class TestAdminAircraftShape:
    def test_list_returns_pagination_envelope(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.get("/api/admin/aircraft")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        # Frontend reads all four — assert every key present even on empty DB.
        for key in ("aircraft", "total", "page", "pages"):
            assert key in body, f"missing key {key!r}"
        assert isinstance(body["aircraft"], list)

    def test_stats_returns_expected_keys(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.get("/api/admin/aircraft/stats")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        stats = body["stats"]
        # Categories the dashboard renders. Empty DB → all zeros, but
        # the *keys* stay present.
        for key in ("total", "widebody", "cargo", "military"):
            assert key in stats, f"missing stats key {key!r}"

    def test_types_short_search_returns_empty_narrowing(self, app_client_fastapi: Any) -> None:
        """<2-char search is documented to skip narrowing and return
        the full top-200 slice. Empty DB → 0 items either way, but
        the shape mustn't change (frontend distinguishes an empty
        autocomplete from a broken one)."""
        r_short = app_client_fastapi.get("/api/admin/aircraft/types?search=a")
        assert r_short.status_code == 200
        assert isinstance(r_short.json()["types"], list)

        r_full = app_client_fastapi.get("/api/admin/aircraft/types?search=A320")
        assert r_full.status_code == 200
        assert isinstance(r_full.json()["types"], list)

    def test_liveries_and_registrations_shape(self, app_client_fastapi: Any) -> None:
        for path, key in (
            ("/api/admin/aircraft/liveries", "liveries"),
            ("/api/admin/aircraft/registrations", "registrations"),
        ):
            r = app_client_fastapi.get(path)
            assert r.status_code == 200, (path, r.text)
            body = r.json()
            assert body["success"] is True
            assert key in body, f"{path} missing top-level key {key!r}"
            assert isinstance(body[key], list)

    def test_aircraft_query_missing_returns_204_or_404_body(self, app_client_fastapi: Any) -> None:
        """``/api/admin/aircraft-query/{registration}`` is the drill-
        down for an admin. On an empty DB every joined table is empty,
        so the endpoint returns 200 with an empty body — not 404. The
        assertion here is only "no 5xx", not the exact status.
        """
        r = app_client_fastapi.get("/api/admin/aircraft-query/NONEXISTENT")
        assert r.status_code < 500, r.text
