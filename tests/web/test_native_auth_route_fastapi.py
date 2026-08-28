"""Integration coverage for the stage 1d native login endpoints.

Exercises the five endpoints mounted at ``/api/auth/*``, ``/api/me`` and
``/api/me/api-key/rotate`` under the FastAPI ``app_client_fastapi``
fixture. The identity providers themselves are exercised in
``tests/auth/`` — this file tests the wire-up (request shape, DB row
creation, api_key rotation) with the providers monkey-patched.

Patterns:

- The factory singletons ``factory._{apple,wechat,google}_auth`` are
  monkey-patched to fake objects. This short-circuits the YAML load
  path so no ``config/auth.yaml`` values are required to test the
  endpoint. ``factory.reset_auth_provider()`` is called at teardown to
  keep sibling tests clean.
- ``SKIP_AUTH`` is left at the fixture default (``true``) except in the
  bearer / 401 tests, which override it — see
  :func:`test_json_401_for_api_but_302_for_html`.
- The ``users`` table already exists (``init_app`` creates it), so
  ``UserService`` can be driven directly to precondition rows.
"""

from __future__ import annotations

from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Fakes


class _FakeAppleAuth:
    """Stand-in for :class:`AppleAuth` — accepts one hard-coded token."""

    def __init__(self, sub: str = "apple-sub-1", email: str | None = None) -> None:
        self.sub = sub
        self.email = email

    def get_user_from_token(self, token: str) -> dict | None:
        if token == "good-apple":
            return {
                "sub": self.sub,
                "email": self.email,
                "email_verified": bool(self.email),
                "name": None,
                "role": "user",
                "groups": [],
            }
        return None


class _FakeGoogleAuth:
    """Stand-in for :class:`GoogleAuth` — accepts one hard-coded id_token."""

    def __init__(self, sub: str = "google-sub-1", email: str = "g@example.com") -> None:
        self.sub = sub
        self.email = email

    def get_user_from_token(self, token: str) -> dict | None:
        if token == "good-google":
            return {
                "sub": self.sub,
                "email": self.email,
                "email_verified": True,
                "name": "Google User",
                "role": "user",
                "groups": [],
            }
        return None


class _FakeWechatAuth:
    """Stand-in for :class:`WechatAuth` — returns a fixed openid for one code."""

    def __init__(self, openid: str = "wx-open-1", unionid: str | None = None) -> None:
        self.openid = openid
        self.unionid = unionid

    def code_to_session(self, code: str, platform: str) -> dict | None:
        if code == "good-code":
            return {"openid": self.openid, "unionid": self.unionid}
        return None


@pytest.fixture(autouse=True)
def _reset_factory_singletons(monkeypatch: pytest.MonkeyPatch):
    """Wipe the auth factory singletons between tests so a monkey-patch
    in one test doesn't leak into the next.
    """
    from src.auth import factory

    yield
    factory.reset_auth_provider()


# ---------------------------------------------------------------------------
# Wechat login


class TestWechatLogin:
    def test_creates_user_on_first_call_and_reuses_on_second(
        self, app_client_fastapi: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.auth import factory

        monkeypatch.setattr(factory, "_wechat_auth", _FakeWechatAuth(openid="wx-1"))

        r1 = app_client_fastapi.post(
            "/api/auth/wechat/login",
            json={"code": "good-code", "platform": "mp"},
        )
        assert r1.status_code == 200, r1.text
        body1 = r1.json()
        assert body1["success"] is True
        assert body1["api_key"]
        assert body1["user"]["email"].startswith("wechat:wx-1")
        # api_key MUST NOT be duplicated inside the nested user dict.
        assert "api_key" not in body1["user"]

        # Second login with the same openid reuses the row (same id, same key).
        r2 = app_client_fastapi.post(
            "/api/auth/wechat/login",
            json={"code": "good-code", "platform": "mp"},
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["user"]["id"] == body1["user"]["id"]
        assert body2["api_key"] == body1["api_key"]

    def test_bad_code_returns_401(
        self, app_client_fastapi: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.auth import factory

        monkeypatch.setattr(factory, "_wechat_auth", _FakeWechatAuth())

        r = app_client_fastapi.post(
            "/api/auth/wechat/login",
            json={"code": "wrong-code", "platform": "mp"},
        )
        assert r.status_code == 401
        assert r.json() == {"success": False, "error": "wechat code exchange failed"}

    def test_unconfigured_returns_503(self, app_client_fastapi: Any) -> None:
        # `_wechat_auth` is None from teardown; `get_wechat_auth` falls
        # through to the YAML path which finds no AppID in the test config.
        r = app_client_fastapi.post(
            "/api/auth/wechat/login",
            json={"code": "any", "platform": "mp"},
        )
        assert r.status_code == 503
        assert r.json() == {"success": False, "error": "wechat auth is not configured"}


# ---------------------------------------------------------------------------
# Google native


class TestGoogleNative:
    def test_valid_token_creates_user(
        self, app_client_fastapi: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.auth import factory

        monkeypatch.setattr(
            factory,
            "_google_auth",
            _FakeGoogleAuth(sub="gsub-1", email="alice@example.com"),
        )

        r = app_client_fastapi.post(
            "/api/auth/google/native",
            json={"id_token": "good-google"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        assert body["api_key"]
        assert body["user"]["email"] == "alice@example.com"

    def test_invalid_token_returns_401(
        self, app_client_fastapi: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.auth import factory

        monkeypatch.setattr(factory, "_google_auth", _FakeGoogleAuth())

        r = app_client_fastapi.post(
            "/api/auth/google/native",
            json={"id_token": "bogus"},
        )
        assert r.status_code == 401
        assert r.json()["error"] == "invalid google id_token"


# ---------------------------------------------------------------------------
# Apple native


class TestAppleNative:
    def test_valid_token_first_login_persists_email(
        self, app_client_fastapi: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.auth import factory

        monkeypatch.setattr(
            factory,
            "_apple_auth",
            _FakeAppleAuth(sub="asub-1", email="bob@example.com"),
        )

        r = app_client_fastapi.post(
            "/api/auth/apple/native",
            json={"identity_token": "good-apple", "name": "Bob"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user"]["email"] == "bob@example.com"
        assert body["user"]["name"] == "Bob"

    def test_missing_email_after_first_login_reuses_row(
        self, app_client_fastapi: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Apple omits ``email`` from every sign-in after the first.

        The endpoint MUST still resolve the user by ``apple_sub`` and
        return the row already created on the first login.
        """
        from src.auth import factory

        # First login — email present, row created.
        monkeypatch.setattr(
            factory,
            "_apple_auth",
            _FakeAppleAuth(sub="asub-2", email="first@example.com"),
        )
        r1 = app_client_fastapi.post(
            "/api/auth/apple/native", json={"identity_token": "good-apple"}
        )
        assert r1.status_code == 200
        first_id = r1.json()["user"]["id"]

        # Second login — email absent, same sub. Should hit apple_sub lookup.
        monkeypatch.setattr(factory, "_apple_auth", _FakeAppleAuth(sub="asub-2", email=None))
        r2 = app_client_fastapi.post(
            "/api/auth/apple/native", json={"identity_token": "good-apple"}
        )
        assert r2.status_code == 200
        assert r2.json()["user"]["id"] == first_id


# ---------------------------------------------------------------------------
# /api/me + rotate under bearer auth


class TestMeAndRotate:
    def _seed_bearer_user(
        self,
        app_client_fastapi: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[str, dict]:
        """Create a user via the apple native endpoint and return
        (api_key, user_body).

        Convenient because the endpoint gives us a properly-provisioned
        user (row + free-tier subscription + api_key) in one call.
        """
        from src.auth import factory

        monkeypatch.setattr(
            factory,
            "_apple_auth",
            _FakeAppleAuth(sub="asub-me-1", email="me@example.com"),
        )
        r = app_client_fastapi.post("/api/auth/apple/native", json={"identity_token": "good-apple"})
        assert r.status_code == 200, r.text
        body = r.json()
        return body["api_key"], body["user"]

    def test_get_me_with_bearer(
        self, app_client_fastapi: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Turn off skip-auth so the bearer path is exercised. `_skip_auth`
        # reads the env var at call time, so this takes effect immediately.
        monkeypatch.setenv("SKIP_AUTH", "false")

        # Re-enable to seed, then flip back off.
        monkeypatch.setenv("SKIP_AUTH", "true")
        api_key, user = self._seed_bearer_user(app_client_fastapi, monkeypatch)
        monkeypatch.setenv("SKIP_AUTH", "false")

        r = app_client_fastapi.get(
            "/api/me",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        assert body["user"]["email"] == user["email"]

    def test_rotate_api_key_invalidates_old(
        self, app_client_fastapi: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SKIP_AUTH", "true")
        old_key, _ = self._seed_bearer_user(app_client_fastapi, monkeypatch)
        monkeypatch.setenv("SKIP_AUTH", "false")

        r = app_client_fastapi.post(
            "/api/me/api-key/rotate",
            headers={"Authorization": f"Bearer {old_key}"},
        )
        assert r.status_code == 200, r.text
        new_key = r.json()["api_key"]
        assert new_key
        assert new_key != old_key

        # Old key is dead — hitting /api/me with it now 401s.
        r_old = app_client_fastapi.get(
            "/api/me",
            headers={"Authorization": f"Bearer {old_key}"},
        )
        assert r_old.status_code == 401

        # New key still works.
        r_new = app_client_fastapi.get(
            "/api/me",
            headers={"Authorization": f"Bearer {new_key}"},
        )
        assert r_new.status_code == 200


# ---------------------------------------------------------------------------
# JSON 401 vs HTML 302 split (stage 1a behaviour, sanity-checked here so
# regressions inside /api/auth-shaped paths break loudly).


class TestJsonVsHtmlUnauthenticated:
    def test_api_path_returns_json_401(
        self, app_client_fastapi: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SKIP_AUTH", "false")

        r = app_client_fastapi.get("/api/me", follow_redirects=False)
        assert r.status_code == 401
        assert r.json() == {"success": False, "error": "unauthenticated"}

    def test_bad_bearer_returns_json_401(
        self, app_client_fastapi: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SKIP_AUTH", "false")

        r = app_client_fastapi.get(
            "/api/me",
            headers={"Authorization": "Bearer " + ("f" * 40)},  # syntactically valid hex, not in DB
            follow_redirects=False,
        )
        assert r.status_code == 401
