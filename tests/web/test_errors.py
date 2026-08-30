"""Tests for `src.web.errors` and its use by the API handlers.

The defect being guarded is a leak, so the assertions are two-sided every time:
the detail must appear in the log *and* be absent from the response body.
Asserting only the second half would pass for a handler that swallowed the
error entirely.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

# The generic-500 body FastAPI's global exception handler returns. Copied
# here as a local constant now that ``src.web.errors`` is gone with the
# Flask side; the assertion is the contract, not the constant source.
GENERIC_ERROR_MESSAGE = "Internal server error"

# Shaped like the psycopg2 / sqlite3 messages these handlers actually raise:
# the driver embeds the failing statement, so `str(exc)` carries the schema.
LEAKY_MESSAGE = (
    'relation "aircraft_static_info" does not exist\n'
    "[SQL: SELECT secret_col FROM aircraft_static_info WHERE api_key = 'sk-live-1234']"
)


# The TestApiError direct-call tests were removed alongside the Flask
# routes. ``src.web.errors.api_error`` was Flask-only (uses jsonify +
# a request context); FastAPI routes rely on the global
# StarletteHTTPException handler in ``app.py`` for the same
# "generic 500, detail only in the log" contract, and the E2E
# ``TestHandlerLeak`` below covers that path.


class TestHandlerLeak:
    """End-to-end through a real route, since that is where the leak lived."""

    def test_route_failure_logs_the_detail_but_does_not_return_it(
        self, app_client: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        runtime_module = app_client.application_module

        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError(LEAKY_MESSAGE)

        monkeypatch.setattr(runtime_module.db_manager, "get_session", boom)

        with caplog.at_level(logging.ERROR):
            response = app_client.get("/api/v1/admin/reports")

        assert response.status_code == 500
        body = response.text
        assert "secret_col" not in body
        assert "aircraft_static_info" not in body
        assert response.json()["error"] == GENERIC_ERROR_MESSAGE
        assert "secret_col" in caplog.text

    def test_no_handler_still_serialises_an_exception(self) -> None:
        """Grep-level guard: ``str(e)`` in a response body must not come
        back. Scans every FastAPI handler under ``src/web/routes/`` +
        the ASGI app factory.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        scanned = [root / "app.py", *root.glob("src/web/routes/*.py")]
        for source in scanned:
            text = source.read_text(encoding="utf-8")
            assert '"error": str(e)' not in text, source
            assert '"error": str(exc)' not in text, source
