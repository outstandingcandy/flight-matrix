"""Tests for `src.core.deploy_target`.

The resolution matrix is asserted cell by cell against the table in
`docs/deployment.md`. This is deliberately exhaustive rather than
representative: the whole point of centralising resolution is that a change to
one cell must be a deliberate, visible edit here.
"""

from __future__ import annotations

import pytest

from src.core.deploy_target import (
    ENV_VAR,
    DeployTarget,
    current_target,
    default_auth_provider,
    default_email_provider,
    default_llm_provider,
    default_scaler_provider,
    default_storage_provider,
    resolve_provider,
)
from src.core.exceptions import ConfigurationError

_LLM_KEYS = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove target and LLM-key variables so tests never inherit a developer env."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    for key in _LLM_KEYS:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# current_target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["aws", "gcp", "local"])
def test_current_target_reads_env(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(ENV_VAR, raw)
    assert current_target() is DeployTarget(raw)


@pytest.mark.parametrize("raw", ["AWS", " gcp ", "Local"])
def test_current_target_normalises_case_and_whitespace(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(ENV_VAR, raw)
    assert current_target() is DeployTarget(raw.strip().lower())


@pytest.mark.parametrize("raw", ["", "   "])
def test_current_target_defaults_to_local(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(ENV_VAR, raw)
    assert current_target() is DeployTarget.LOCAL


def test_current_target_defaults_to_local_when_unset() -> None:
    assert current_target() is DeployTarget.LOCAL


@pytest.mark.parametrize("raw", ["azure", "AWS-prod", "gcloud"])
def test_current_target_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """An unknown target must fail loudly rather than silently downgrade to local."""
    monkeypatch.setenv(ENV_VAR, raw)
    with pytest.raises(ConfigurationError) as excinfo:
        current_target()

    message = str(excinfo.value)
    assert raw.lower() in message
    for target in DeployTarget:
        assert target.value in message


# ---------------------------------------------------------------------------
# Resolution matrix
# ---------------------------------------------------------------------------

# target -> (storage, auth, llm, email, scaler)
_MATRIX = {
    "aws": ("s3", "cognito", "aws_bedrock", "aws_ses", "asg"),
    "gcp": ("gcs", "google", "gemini", "smtp", "noop"),
    "local": ("local", "none", "aws_bedrock", "smtp", "noop"),
}


@pytest.mark.parametrize("target", sorted(_MATRIX))
def test_resolution_matrix(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    monkeypatch.setenv(ENV_VAR, target)
    storage, auth, llm, email, scaler = _MATRIX[target]

    assert default_storage_provider() == storage
    assert default_auth_provider() == auth
    assert default_llm_provider() == llm
    assert default_email_provider() == email
    assert default_scaler_provider() == scaler


def test_matrix_covers_every_target() -> None:
    """Guards against a new DeployTarget member with no asserted row."""
    assert set(_MATRIX) == {t.value for t in DeployTarget}


# ---------------------------------------------------------------------------
# Local LLM provider selection
# ---------------------------------------------------------------------------


def test_local_llm_provider_uses_gemini_when_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_VAR, "local")
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    assert default_llm_provider() == "gemini"


def test_local_llm_provider_defaults_to_bedrock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local development used Bedrock before targets existed; keep it that way."""
    monkeypatch.setenv(ENV_VAR, "local")
    assert default_llm_provider() == "aws_bedrock"


def test_local_llm_provider_ignores_unimplemented_provider_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-selecting 'anthropic'/'openai' would resolve to a provider the
    analysis call sites cannot build; only an explicit override may pick them."""
    monkeypatch.setenv(ENV_VAR, "local")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert default_llm_provider() == "aws_bedrock"


def test_local_llm_provider_ignores_blank_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "local")
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    assert default_llm_provider() == "aws_bedrock"


def test_cloud_targets_ignore_local_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray key in the environment must not override a cloud target's provider."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    monkeypatch.setenv(ENV_VAR, "aws")
    assert default_llm_provider() == "aws_bedrock"

    monkeypatch.setenv(ENV_VAR, "gcp")
    assert default_llm_provider() == "gemini"


# ---------------------------------------------------------------------------
# resolve_provider
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("configured", [None, "", "   "])
def test_resolve_provider_falls_back_to_default(configured: str | None) -> None:
    assert resolve_provider(configured, "s3") == "s3"


def test_resolve_provider_honours_explicit_override() -> None:
    """The override is a supported path: debugging Gemini locally, Bedrock on GCP."""
    assert resolve_provider("gemini", "aws_bedrock") == "gemini"


def test_resolve_provider_strips_whitespace() -> None:
    assert resolve_provider("  gcs  ", "s3") == "gcs"
