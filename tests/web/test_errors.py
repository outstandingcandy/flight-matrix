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

from src.web.errors import GENERIC_ERROR_MESSAGE, api_error

# Shaped like the psycopg2 / sqlite3 messages these handlers actually raise:
# the driver embeds the failing statement, so `str(exc)` carries the schema.
LEAKY_MESSAGE = (
    'relation "aircraft_static_info" does not exist\n'
    "[SQL: SELECT secret_col FROM aircraft_static_info WHERE api_key = 'sk-live-1234']"
)


class TestApiError:
    def test_body_carries_no_exception_detail(self, app_client: Any) -> None:
        with app_client.application.test_request_context():
            response, status = api_error(RuntimeError(LEAKY_MESSAGE), "Error doing the thing")

        assert status == 500
        body = response.get_json()
        assert body == {"success": False, "error": GENERIC_ERROR_MESSAGE}
        assert "secret_col" not in response.get_data(as_text=True)
        assert "sk-live-1234" not in response.get_data(as_text=True)

    def test_logs_the_context_and_a_traceback(
        self, app_client: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        with app_client.application.test_request_context(), caplog.at_level(logging.ERROR):
            try:
                raise RuntimeError(LEAKY_MESSAGE)
            except RuntimeError as exc:
                api_error(exc, "Error doing the thing")

        assert "Error doing the thing" in caplog.text
        assert "secret_col" in caplog.text, "the detail must survive in the log"
        assert "Traceback (most recent call last)" in caplog.text

    def test_status_is_overridable(self, app_client: Any) -> None:
        with app_client.application.test_request_context():
            _, status = api_error(ValueError("nope"), "Error validating", status=400)
        assert status == 400


class TestHandlerLeak:
    """End-to-end through a real route, since that is where the leak lived."""

    def test_route_failure_logs_the_detail_but_does_not_return_it(
        self, app_client: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        web_app = app_client.application_module

        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError(LEAKY_MESSAGE)

        monkeypatch.setattr(web_app.db_manager, "get_session", boom)

        with caplog.at_level(logging.ERROR):
            response = app_client.get("/api/admin/reports")

        assert response.status_code == 500
        body = response.get_data(as_text=True)
        assert "secret_col" not in body
        assert "aircraft_static_info" not in body
        assert response.get_json()["error"] == GENERIC_ERROR_MESSAGE
        assert "secret_col" in caplog.text

    def test_no_handler_still_serialises_an_exception(self) -> None:
        """A grep-level guard: `str(e)` in a response body must not come back."""
        from pathlib import Path

        source = Path(__file__).resolve().parents[2] / "web_app.py"
        text = source.read_text(encoding="utf-8")
        assert '"error": str(e)' not in text
        assert '"error": str(exc)' not in text
