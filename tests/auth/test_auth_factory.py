"""Tests for `src.auth.factory`.

The two behaviours asserted hardest here are the ones a future refactor is
most likely to "fix" and break:

- an incompletely configured provider resolves to **None**, because
  `src/auth/decorators.py` uses None to choose between a login redirect and a
  403 page;
- an *unrecognised* `auth.provider` raises, because silently falling back would
  authenticate users against the wrong identity provider.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from src.auth import factory
from src.core.deploy_target import ENV_VAR
from src.core.exceptions import ConfigurationError

_COGNITO_ENV = {
    "COGNITO_USER_POOL_ID": "us-west-2_abc",
    "COGNITO_CLIENT_ID": "cid",
    "COGNITO_DOMAIN": "d.auth.us-west-2.amazoncognito.com",
    "COGNITO_CALLBACK_URL": "https://app.example.com/auth/callback",
}


class _FakeConfig:
    """Stands in for `YAMLConfig`, which would read the real config tree."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def get(self, key_path: str, default: Any = None) -> Any:
        return self._values.get(key_path, default)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset factory caches and strip provider env vars between tests."""
    factory.reset_auth_provider()
    monkeypatch.delenv(ENV_VAR, raising=False)
    for key in (*_COGNITO_ENV, "COGNITO_CLIENT_SECRET", "COGNITO_LOGOUT_URL"):
        monkeypatch.delenv(key, raising=False)
    # cognito_auth keeps its own singleton, which would leak across tests.
    monkeypatch.setattr("src.auth.cognito_auth._cognito_auth", None)
    yield
    factory.reset_auth_provider()


def _use_config(monkeypatch: pytest.MonkeyPatch, values: dict[str, Any]) -> None:
    monkeypatch.setattr(factory, "_get_yaml_config", lambda: _FakeConfig(values))


# ---------------------------------------------------------------------------
# Provider name resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [("aws", "cognito"), ("gcp", "google"), ("local", "none")],
)
def test_blank_provider_resolves_from_target(
    monkeypatch: pytest.MonkeyPatch, target: str, expected: str
) -> None:
    monkeypatch.setenv(ENV_VAR, target)
    _use_config(monkeypatch, {"auth.provider": ""})
    assert factory.resolve_auth_provider_name() == expected


def test_explicit_provider_overrides_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Debugging Google auth on a laptop is a supported path."""
    monkeypatch.setenv(ENV_VAR, "local")
    _use_config(monkeypatch, {"auth.provider": "google"})
    assert factory.resolve_auth_provider_name() == "google"


def test_unloadable_config_falls_back_to_target_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken config file must not take Cognito login down with it."""
    monkeypatch.setenv(ENV_VAR, "aws")
    monkeypatch.setattr(factory, "_get_yaml_config", lambda: None)
    assert factory.resolve_auth_provider_name() == "cognito"


def test_unknown_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "aws")
    _use_config(monkeypatch, {"auth.provider": "okta"})

    with pytest.raises(ConfigurationError) as excinfo:
        factory.resolve_auth_provider_name()

    message = str(excinfo.value)
    assert "okta" in message
    for supported in factory.SUPPORTED_PROVIDERS:
        assert supported in message


@pytest.mark.parametrize(
    ("target", "expected"),
    [("aws", "claims"), ("gcp", "config"), ("local", "env")],
)
def test_groups_source_per_target(
    monkeypatch: pytest.MonkeyPatch, target: str, expected: str
) -> None:
    monkeypatch.setenv(ENV_VAR, target)
    _use_config(monkeypatch, {"auth.provider": ""})
    assert factory.groups_source() == expected


def test_groups_source_covers_every_provider() -> None:
    assert set(factory._GROUPS_SOURCE) == set(factory.SUPPORTED_PROVIDERS)


# ---------------------------------------------------------------------------
# Provider construction
# ---------------------------------------------------------------------------


def test_aws_target_builds_cognito(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.auth.cognito_auth import CognitoAuth

    monkeypatch.setenv(ENV_VAR, "aws")
    for key, value in _COGNITO_ENV.items():
        monkeypatch.setenv(key, value)
    _use_config(monkeypatch, {"auth.provider": ""})

    provider = factory.get_auth_provider()
    assert isinstance(provider, CognitoAuth)


def test_incomplete_cognito_env_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """decorators.py needs None here, not an exception."""
    monkeypatch.setenv(ENV_VAR, "aws")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "cid")  # everything else missing
    _use_config(monkeypatch, {"auth.provider": ""})

    assert factory.get_auth_provider() is None


def test_gcp_target_builds_google(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.auth.google_auth import GoogleAuth

    monkeypatch.setenv(ENV_VAR, "gcp")
    _use_config(
        monkeypatch,
        {
            "auth.provider": "",
            "auth.google.client_id": "cid.apps.googleusercontent.com",
            "auth.google.client_secret": "secret",
            "auth.google.callback_url": "https://app.example.com/auth/callback",
            "auth.google.allowed_domains": ["example.com"],
            "auth.google.groups": {"admins": ["owner@example.com"]},
        },
    )

    provider = factory.get_auth_provider()
    assert isinstance(provider, GoogleAuth)
    assert provider.allowed_domains == ["example.com"]
    assert provider._groups_for_email("owner@example.com") == ["admins"]


def test_incomplete_google_config_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "gcp")
    _use_config(
        monkeypatch,
        {"auth.provider": "", "auth.google.client_id": "cid.apps.googleusercontent.com"},
    )

    assert factory.get_auth_provider() is None


def test_google_provider_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "gcp")
    _use_config(
        monkeypatch,
        {
            "auth.provider": "",
            "auth.google.client_id": "cid.apps.googleusercontent.com",
            "auth.google.client_secret": "secret",
            "auth.google.callback_url": "https://app.example.com/auth/callback",
        },
    )

    assert factory.get_auth_provider() is factory.get_auth_provider()


def test_local_target_has_no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "local")
    _use_config(monkeypatch, {"auth.provider": ""})
    assert factory.get_auth_provider() is None


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------


def test_convenience_wrappers_return_none_without_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_VAR, "local")
    _use_config(monkeypatch, {"auth.provider": ""})

    assert factory.verify_token("t") is None
    assert factory.get_user_from_token("t") is None
    assert factory.exchange_code_for_tokens("c") is None


def test_convenience_wrappers_delegate_to_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Stub:
        def verify_token(self, token: str) -> dict:
            return {"claim": token}

        def get_user_from_token(self, token: str) -> dict:
            return {"email": token}

        def exchange_code_for_tokens(self, code: str) -> dict:
            return {"id_token": code}

    monkeypatch.setattr(factory, "get_auth_provider", lambda: _Stub())

    assert factory.verify_token("t") == {"claim": "t"}
    assert factory.get_user_from_token("t") == {"email": "t"}
    assert factory.exchange_code_for_tokens("c") == {"id_token": "c"}
