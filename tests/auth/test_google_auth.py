"""Tests for `src.auth.google_auth`.

These cover the parts that differ from Cognito and would fail silently if
written the Cognito way: the `access_type=offline` login parameters, the
body-based client credentials at the token endpoint, the config-driven group
lookup, and — most importantly — that `get_user_from_token()` returns exactly
the same field set as `CognitoAuth.get_user_from_token()`. Signature
verification itself is not re-tested; it is the same jose call as Cognito's.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from src.auth.cognito_auth import CognitoAuth
from src.auth.google_auth import AUTHORIZE_ENDPOINT, TOKEN_ENDPOINT, GoogleAuth

_GROUP_MAP = {
    "admins": ["Owner@Example.com"],
    "flight-schedules-viewers": ["viewer@example.com", "owner@example.com"],
}


def _auth(**overrides: Any) -> GoogleAuth:
    kwargs: dict[str, Any] = {
        "client_id": "client-123.apps.googleusercontent.com",
        "client_secret": "secret-456",
        "callback_url": "https://app.example.com/auth/callback",
        "group_map": _GROUP_MAP,
    }
    kwargs.update(overrides)
    return GoogleAuth(**kwargs)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.text = str(payload)
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


# ---------------------------------------------------------------------------
# Login / logout URLs
# ---------------------------------------------------------------------------


def test_login_url_requests_offline_access() -> None:
    """Without offline access + consent Google issues no refresh token."""
    url = _auth().get_login_url(state="abc")
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == AUTHORIZE_ENDPOINT
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["response_type"] == ["code"]
    assert params["state"] == ["abc"]
    assert params["scope"] == ["openid email profile"]
    assert params["redirect_uri"] == ["https://app.example.com/auth/callback"]


def test_login_url_omits_state_when_not_given() -> None:
    params = parse_qs(urlparse(_auth().get_login_url()).query)
    assert "state" not in params


def test_logout_url_is_empty_without_explicit_config() -> None:
    """Google has no RP-initiated logout; callers must treat '' as 'go home'."""
    assert _auth().get_logout_url() == ""


def test_logout_url_passes_through_when_configured() -> None:
    assert _auth(logout_url="https://app.example.com/").get_logout_url() == (
        "https://app.example.com/"
    )


# ---------------------------------------------------------------------------
# Domain allow-list
# ---------------------------------------------------------------------------


def test_empty_allowed_domains_accepts_any_account() -> None:
    auth = _auth()
    assert auth.is_domain_allowed("anyone@gmail.com") is True


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("owner@example.com", True),
        ("Owner@EXAMPLE.com", True),
        ("owner@other.com", False),
        ("not-an-email", False),
        (None, False),
    ],
)
def test_allowed_domains_filter(email: str | None, expected: bool) -> None:
    auth = _auth(allowed_domains=["Example.com"])
    assert auth.is_domain_allowed(email) is expected


# ---------------------------------------------------------------------------
# Group lookup (the claim Google does not provide)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("owner@example.com", ["admins", "flight-schedules-viewers"]),
        ("OWNER@EXAMPLE.COM", ["admins", "flight-schedules-viewers"]),
        ("viewer@example.com", ["flight-schedules-viewers"]),
        ("nobody@example.com", []),
        (None, []),
    ],
)
def test_groups_for_email(email: str | None, expected: list[str]) -> None:
    assert _auth()._groups_for_email(email) == expected


def test_unlisted_user_authenticates_but_gets_no_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Denial is the group decorators' job, not the provider's."""
    auth = _auth()
    monkeypatch.setattr(
        auth,
        "verify_token",
        lambda _token: {"sub": "1", "email": "nobody@example.com", "email_verified": True},
    )

    user = auth.get_user_from_token("t")
    assert user is not None
    assert user["groups"] == []
    assert user["role"] == "user"


# ---------------------------------------------------------------------------
# get_user_from_token — field parity with Cognito
# ---------------------------------------------------------------------------


def test_user_shape_matches_cognito(monkeypatch: pytest.MonkeyPatch) -> None:
    """decorators.py and /auth/debug read these names; they must not drift."""
    google = _auth()
    monkeypatch.setattr(
        google,
        "verify_token",
        lambda _token: {
            "sub": "google-sub",
            "email": "owner@example.com",
            "email_verified": True,
            "name": "Owner",
        },
    )

    cognito = CognitoAuth(
        user_pool_id="us-west-2_abc",
        client_id="cid",
        client_secret=None,
        domain="d.example.com",
        callback_url="https://app.example.com/auth/callback",
        logout_url="https://app.example.com/",
    )
    monkeypatch.setattr(
        cognito,
        "verify_token",
        lambda _token: {
            "sub": "cognito-sub",
            "email": "owner@example.com",
            "email_verified": True,
            "name": "Owner",
            "cognito:groups": ["admins"],
        },
    )

    google_user = google.get_user_from_token("t")
    cognito_user = cognito.get_user_from_token("t")

    assert google_user is not None and cognito_user is not None
    assert google_user.keys() == cognito_user.keys()
    assert google_user["role"] == "admin"
    assert google_user["groups"] == ["admins", "flight-schedules-viewers"]


def test_admin_role_only_for_admins_group(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = _auth()
    monkeypatch.setattr(
        auth,
        "verify_token",
        lambda _token: {"sub": "1", "email": "viewer@example.com", "email_verified": True},
    )
    user = auth.get_user_from_token("t")
    assert user is not None
    assert user["role"] == "user"


def test_unverified_email_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = _auth()
    monkeypatch.setattr(
        auth,
        "verify_token",
        lambda _token: {"sub": "1", "email": "owner@example.com", "email_verified": False},
    )
    assert auth.get_user_from_token("t") is None


def test_disallowed_domain_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = _auth(allowed_domains=["example.com"])
    monkeypatch.setattr(
        auth,
        "verify_token",
        lambda _token: {"sub": "1", "email": "outsider@other.com", "email_verified": True},
    )
    assert auth.get_user_from_token("t") is None


def test_invalid_token_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = _auth()
    monkeypatch.setattr(auth, "verify_token", lambda _token: None)
    assert auth.get_user_from_token("t") is None


# ---------------------------------------------------------------------------
# Token endpoint — credentials in the body, not an HTTP Basic header
# ---------------------------------------------------------------------------


def test_code_exchange_sends_credentials_in_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sending them as HTTP Basic (the Cognito way) fails with invalid_client."""
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return _FakeResponse(200, {"id_token": "id", "refresh_token": "r"})

    monkeypatch.setattr("src.auth.google_auth.requests.post", fake_post)

    tokens = _auth().exchange_code_for_tokens("the-code")

    assert tokens == {"id_token": "id", "refresh_token": "r"}
    assert captured["url"] == TOKEN_ENDPOINT
    assert captured["data"] == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "client_id": "client-123.apps.googleusercontent.com",
        "redirect_uri": "https://app.example.com/auth/callback",
        "client_secret": "secret-456",
    }
    assert "Authorization" not in captured["headers"]


def test_code_exchange_returns_none_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.auth.google_auth.requests.post",
        lambda *_a, **_k: _FakeResponse(400, {"error": "invalid_grant"}),
    )
    assert _auth().exchange_code_for_tokens("bad") is None


def test_refresh_sends_credentials_in_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return _FakeResponse(200, {"id_token": "fresh"})

    monkeypatch.setattr("src.auth.google_auth.requests.post", fake_post)

    assert _auth().refresh_tokens("the-refresh-token") == {"id_token": "fresh"}
    assert captured["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "the-refresh-token",
        "client_id": "client-123.apps.googleusercontent.com",
        "client_secret": "secret-456",
    }


def test_refresh_returns_none_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.auth.google_auth.requests.post",
        lambda *_a, **_k: _FakeResponse(400, {"error": "invalid_grant"}),
    )
    assert _auth().refresh_tokens("stale") is None
