"""Tests for `src.llm.factory`.

The behaviour worth pinning down is what happens to the two provider names that
`config/llm.yaml` has always documented but no call site implements: selecting
``anthropic`` or ``openai`` must fail loudly rather than quietly running against
a different model than the operator asked for.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.core.deploy_target import ENV_VAR
from src.core.exceptions import ConfigurationError
from src.llm.base import ConverseClient
from src.llm.factory import (
    IMPLEMENTED_PROVIDERS,
    KNOWN_PROVIDERS,
    LLMClientFactory,
    resolve_llm_provider_name,
    resolve_model_id,
)
from src.llm.gemini import DEFAULT_MODEL, DEFAULT_VISION_MODEL, GeminiConverseClient

_LLM_KEYS = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY")


class _FakeYAMLConfig:
    def __init__(self, llm_config: dict[str, Any]) -> None:
        self._llm_config = llm_config

    def get_llm_config(self) -> dict[str, Any]:
        return self._llm_config


class _StubClient:
    def converse(self, **kwargs: Any) -> dict[str, Any]:
        return {"output": {"message": {"role": "assistant", "content": []}}}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never inherit a developer's target or API keys."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    for key in _LLM_KEYS:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Provider name resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [("aws", "aws_bedrock"), ("gcp", "gemini"), ("local", "aws_bedrock")],
)
def test_blank_provider_resolves_from_target(
    monkeypatch: pytest.MonkeyPatch, target: str, expected: str
) -> None:
    monkeypatch.setenv(ENV_VAR, target)
    assert resolve_llm_provider_name("") == expected


def test_local_target_picks_gemini_when_key_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "local")
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    assert resolve_llm_provider_name("") == "gemini"


def test_explicit_provider_overrides_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Debugging Gemini on a laptop, and falling back to Bedrock on GCP, both
    depend on this."""
    monkeypatch.setenv(ENV_VAR, "aws")
    assert resolve_llm_provider_name("gemini") == "gemini"

    monkeypatch.setenv(ENV_VAR, "gcp")
    assert resolve_llm_provider_name("aws_bedrock") == "aws_bedrock"


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_known_but_unimplemented_provider_raises(provider: str) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        resolve_llm_provider_name(provider)

    message = str(excinfo.value)
    assert provider in message
    assert "not implemented" in message
    for supported in IMPLEMENTED_PROVIDERS:
        assert supported in message


def test_unknown_provider_raises() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        resolve_llm_provider_name("vertex_ai")

    assert "vertex_ai" in str(excinfo.value)


def test_implemented_providers_are_a_subset_of_known() -> None:
    assert set(IMPLEMENTED_PROVIDERS) <= set(KNOWN_PROVIDERS)


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def test_gemini_client_is_built_for_gcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "gcp")

    client = LLMClientFactory.create_from_dict(
        {"provider": "", "gemini_api_key": "", "client": _StubClient()}
    )

    assert isinstance(client, GeminiConverseClient)


def test_gemini_without_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "gcp")

    with pytest.raises(ConfigurationError):
        LLMClientFactory.create_from_dict({"provider": "", "gemini_api_key": ""})


def test_bedrock_client_is_returned_unwrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """boto3 already speaks the Converse protocol; wrapping it would put new
    code in the production aws path for no reason."""
    monkeypatch.setenv(ENV_VAR, "aws")
    stub = _StubClient()

    assert LLMClientFactory.create_from_dict({"provider": "", "client": stub}) is stub


def test_real_bedrock_client_satisfies_the_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "aws")

    client = LLMClientFactory.create_from_dict({"provider": "", "aws_region": "us-west-2"})

    assert isinstance(client, ConverseClient)


def test_create_reads_the_yaml_llm_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "aws")
    yaml_config = _FakeYAMLConfig({"provider": "gemini", "gemini_api_key": "sk-test"})

    client = LLMClientFactory.create(yaml_config)  # type: ignore[arg-type]

    assert isinstance(client, GeminiConverseClient)


# ---------------------------------------------------------------------------
# Model id resolution
# ---------------------------------------------------------------------------


def test_bedrock_model_id_override_wins() -> None:
    config = {"bedrock_model_id": "from-config"}
    assert resolve_model_id(config, "aws_bedrock", bedrock_model_id="from-call-site") == (
        "from-call-site"
    )


def test_bedrock_falls_back_to_config_model_id() -> None:
    assert resolve_model_id({"bedrock_model_id": "from-config"}, "aws_bedrock") == "from-config"


def test_bedrock_ignores_gemini_models() -> None:
    config = {"bedrock_model_id": "claude", "gemini_model": "gemini-x"}
    assert resolve_model_id(config, "aws_bedrock", vision=True) == "claude"


def test_gemini_uses_configured_text_model() -> None:
    config = {"gemini_model": "gemini-x", "bedrock_model_id": "claude"}
    assert resolve_model_id(config, "gemini", bedrock_model_id="claude") == "gemini-x"


def test_gemini_vision_uses_the_vision_model() -> None:
    config = {"gemini_model": "gemini-x", "gemini_vision_model": "gemini-pro-x"}
    assert resolve_model_id(config, "gemini", vision=True) == "gemini-pro-x"


def test_gemini_vision_falls_back_to_the_text_model() -> None:
    assert resolve_model_id({"gemini_model": "gemini-x"}, "gemini", vision=True) == "gemini-x"


def test_gemini_falls_back_to_module_defaults() -> None:
    assert resolve_model_id({}, "gemini") == DEFAULT_MODEL
    assert resolve_model_id({}, "gemini", vision=True) == DEFAULT_VISION_MODEL
