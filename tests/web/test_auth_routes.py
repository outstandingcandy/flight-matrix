"""Integration tests for the auth blueprint.

Boots the real Flask app in skip-auth mode and hits each auth route
through the test client. Confirms the blueprint registers correctly and
the expected status codes come back without Cognito configured.
"""

from __future__ import annotations

import pytest

# app_client fixture comes from tests/web/conftest.py — it boots the app
# with skip-auth enabled, an in-memory SQLite DB, and init_app() already run.


class TestAuthBlueprintRegistered:
    def test_login_route_exists(self, app_client) -> None:
        # With auth disabled, /login returns 403 ("Authentication is not enabled").
        r = app_client.get("/login", follow_redirects=False)
        assert r.status_code == 403

    def test_callback_route_exists(self, app_client) -> None:
        r = app_client.get("/auth/callback", follow_redirects=False)
        assert r.status_code == 403

    def test_set_session_rejects_without_body(self, app_client) -> None:
        # Auth-disabled returns 403; when enabled it would return 400 on empty body.
        r = app_client.post("/auth/set-session", json={}, follow_redirects=False)
        assert r.status_code == 403

    def test_logout_redirects(self, app_client) -> None:
        # With auth disabled, /logout clears session and 302s to /flight-schedules.
        r = app_client.get("/logout", follow_redirects=False)
        assert r.status_code == 302
        assert "/flight-schedules" in r.headers.get("Location", "")


class TestAuthShimNoops:
    def test_decorators_are_passthrough_in_skip_auth(self) -> None:
        from src.web.auth_shim import (
            admin_required,
            flight_schedules_required,
            group_required,
            login_required,
            optional_login,
        )

        def handler():
            return "ok"

        # All decorators should return the handler unchanged in skip-auth mode.
        assert login_required(handler) is handler
        assert admin_required(handler) is handler
        assert flight_schedules_required(handler) is handler
        assert optional_login(handler) is handler
        # group_required is a factory; its inner decorator is also identity.
        assert group_required(["admins"])(handler) is handler
