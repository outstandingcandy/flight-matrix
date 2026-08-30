"""Coverage for the ``POST /api/admin/import-data`` bulk-import route.

Two things matter here and neither is exercised by the smoke sweep:

1. **The X-Admin-Secret gate.** This endpoint is a machine-to-machine
   write path used by data-migration scripts. If the header check ever
   slips off, anyone on the public internet can bulk-insert rows into
   ``aircraft_snapshots``, ``airports``, and ``aircraft_static_info``.
   The three ``test_reject_*`` cases guard the gate: missing header,
   empty header, wrong header — each MUST return 401.
2. **Insertion actually works.** The handler commits after every 500
   rows and swallows row-level errors. Assert that a well-formed
   airport payload lands in the DB and shows up on ``GET
   /api/airports/{code}``.
"""

from __future__ import annotations

from typing import Any

import pytest

# The default value ``verify_admin_secret`` compares against when the
# ``ADMIN_SECRET`` env var isn't set — kept in sync with the handler.
LEGACY_SECRET = "flight-matrix-admin-2026"


# ---------------------------------------------------------------------------
# Auth gate


class TestAdminSecretGate:
    def test_reject_missing_header(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.post("/api/v1/admin/import-data", json={"airports": []})
        assert r.status_code == 401
        assert r.json() == {"success": False, "error": "Unauthorized"}

    def test_reject_empty_header(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.post(
            "/api/v1/admin/import-data",
            headers={"X-Admin-Secret": ""},
            json={"airports": []},
        )
        assert r.status_code == 401

    def test_reject_wrong_secret(self, app_client_fastapi: Any) -> None:
        r = app_client_fastapi.post(
            "/api/v1/admin/import-data",
            headers={"X-Admin-Secret": "obviously-not-the-secret"},
            json={"airports": []},
        )
        assert r.status_code == 401

    def test_reject_with_admin_session_but_no_header(self, app_client_fastapi: Any) -> None:
        """A signed-in admin (SKIP_AUTH mock user with 'admins' group)
        still cannot post here without the header — this is a
        different auth gate than ``require_admin``.
        """
        # The fixture already provides an admin session.
        r = app_client_fastapi.post("/api/v1/admin/import-data", json={"airports": []})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Happy path


class TestImportHappyPath:
    @pytest.fixture
    def secret(self, monkeypatch: pytest.MonkeyPatch) -> str:
        """Pin the secret explicitly so the test isn't dependent on
        whichever value the CI env has picked up.
        """
        monkeypatch.setenv("ADMIN_SECRET", LEGACY_SECRET)
        return LEGACY_SECRET

    def test_import_airports_persists_and_reads_back(
        self, app_client_fastapi: Any, secret: str
    ) -> None:
        payload = {
            "airports": [
                {
                    "iata_code": "ZZZ",
                    "icao_code": "ZZZZ",
                    "name": "Test Field",
                    "city": "Testville",
                    "country": "Testland",
                    "latitude": 12.34,
                    "longitude": 56.78,
                }
            ]
        }
        r_import = app_client_fastapi.post(
            "/api/v1/admin/import-data",
            headers={"X-Admin-Secret": secret},
            json=payload,
        )
        assert r_import.status_code == 200, r_import.text
        body = r_import.json()
        assert body == {
            "success": True,
            "imported": 1,
            "errors": 0,
            "total": 1,
        }

        # And it survives — the read-side endpoint should now find it.
        r_read = app_client_fastapi.get("/api/v1/airports/ZZZ")
        assert r_read.status_code == 200

    def test_unknown_table_is_skipped_not_errored(
        self, app_client_fastapi: Any, secret: str
    ) -> None:
        """Rows for a table not in the allow-list get skipped with a
        log line; the request still returns 200 and doesn't count them
        as errors.
        """
        r = app_client_fastapi.post(
            "/api/v1/admin/import-data",
            headers={"X-Admin-Secret": secret},
            json={"not_a_real_table": [{"x": 1}]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        # Nothing imported, no rows added to the total.
        assert body["imported"] == 0
        assert body["total"] == 0

    def test_empty_body_rejected(self, app_client_fastapi: Any, secret: str) -> None:
        r = app_client_fastapi.post(
            "/api/v1/admin/import-data",
            headers={"X-Admin-Secret": secret},
            json={},
        )
        assert r.status_code == 400
        assert r.json() == {"success": False, "error": "Missing request body"}

    def test_row_level_errors_counted_not_thrown(
        self, app_client_fastapi: Any, secret: str
    ) -> None:
        """A row that fails to construct MUST be counted (``errors +=
        1``) and MUST NOT abort the request — the whole batch is a
        best-effort import, and the migration script relies on the
        response body to know how many rows landed.

        Note: the handler ``session.rollback()`` on a per-row failure
        wipes any pending-but-uncommitted rows in the same batch too,
        so we don't assert that earlier rows survived — the docstring
        on the handler advertises 500-row commit granularity, not
        1-row.
        """
        payload = {
            "airports": [
                {
                    "iata_code": "BBB",
                    "icao_code": "BBBB",
                    "name": "Bad",
                    "latitude": 3.0,
                    "longitude": 4.0,
                    "no_such_column": "boom",  # TypeError at __init__
                },
            ]
        }
        r = app_client_fastapi.post(
            "/api/v1/admin/import-data",
            headers={"X-Admin-Secret": secret},
            json=payload,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        assert body["errors"] == 1
        assert body["total"] == 1
