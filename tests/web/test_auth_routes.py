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


# The former TestAuthShimNoops block was removed alongside the Flask
# decorators. ``src.web.auth_shim`` no longer installs no-op
# decorators — the FastAPI half uses ``src.auth.dependencies``
# instead, and the Flask half doesn't exist anymore. The remaining
# TestAuthBlueprintRegistered class still covers /login, /auth/callback,
# /auth/set-session, and /logout — those are FastAPI routes now.
