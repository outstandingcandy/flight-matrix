"""LLM client factory.

Selects the Converse-speaking client for the active deployment target. Mirrors
:class:`~src.storage.factory.StorageFactory` and
:class:`~src.notifications.factory.EmailNotifierFactory` so all three provider
factories read the same way: an explicit ``llm.provider`` wins, an empty one
resolves from ``DEPLOY_TARGET``.

``anthropic`` and ``openai`` are recognised names — they have lived in
``config/llm.yaml`` since before this abstraction — but no analysis call site
implements them, so selecting one raises rather than silently falling back to a
different model than the operator asked for.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.core.deploy_target import default_llm_provider, resolve_provider
from src.core.exceptions import ConfigurationError

if TYPE_CHECKING:
    from src.llm.base import ConverseClient
    from src.utils.yaml_config import YAMLConfig

logger = logging.getLogger("llm.factory")

__all__ = [
    "AWS_BEDROCK",
    "GEMINI",
    "IMPLEMENTED_PROVIDERS",
    "KNOWN_PROVIDERS",
    "LLMClientFactory",
    "resolve_llm_provider_name",
    "resolve_model_id",
]

AWS_BEDROCK = "aws_bedrock"
GEMINI = "gemini"
ANTHROPIC = "anthropic"
OPENAI = "openai"

#: Providers this factory can build a client for.
IMPLEMENTED_PROVIDERS = (AWS_BEDROCK, GEMINI)

#: Every provider name `config/llm.yaml` documents.
KNOWN_PROVIDERS = (AWS_BEDROCK, GEMINI, ANTHROPIC, OPENAI)


def resolve_llm_provider_name(configured: str | None = None) -> str:
    """Resolve which LLM provider this deployment uses.

    Args:
        configured: The value of ``llm.provider``. Empty or None resolves from
            ``DEPLOY_TARGET``.

    Returns:
        One of :data:`IMPLEMENTED_PROVIDERS`.

    Raises:
        ConfigurationError: If the resolved provider is not implemented.
    """
    provider = resolve_provider(configured, default_llm_provider())

    if provider in IMPLEMENTED_PROVIDERS:
        return provider

    if provider in KNOWN_PROVIDERS:
        raise ConfigurationError(
            f"llm.provider={provider!r} is not implemented by the analysis call sites. "
            f"Use one of: {', '.join(IMPLEMENTED_PROVIDERS)}"
        )

    raise ConfigurationError(
        f"Invalid llm.provider={provider!r}. Supported values: {', '.join(IMPLEMENTED_PROVIDERS)}"
    )


class LLMClientFactory:
    """Factory for creating Converse-speaking LLM clients."""

    @staticmethod
    def create_from_dict(config: dict[str, Any]) -> ConverseClient:
        """Create an LLM client from a plain mapping.

        Args:
            config: Mapping with an optional ``provider`` key (empty resolves
                from the deployment target) plus provider-specific settings:
                ``aws_region`` / ``aws_access_key_id`` /
                ``aws_secret_access_key`` for Bedrock, ``gemini_api_key`` for
                Gemini, and ``client`` to inject a pre-built SDK client.

        Returns:
            A client satisfying :class:`~src.llm.base.ConverseClient`.

        Raises:
            ConfigurationError: If the provider is unsupported or its required
                settings are missing.
        """
        provider = resolve_llm_provider_name(config.get("provider"))
        client = config.get("client")

        if provider == GEMINI:
            from src.llm.gemini import GeminiConverseClient

            return GeminiConverseClient(
                api_key=config.get("gemini_api_key", "") or "", client=client
            )

        if client is not None:
            return client  # type: ignore[no-any-return]

        import boto3

        region = config.get("aws_region") or "us-west-2"
        access_key = config.get("aws_access_key_id")
        secret_key = config.get("aws_secret_access_key")

        if access_key and secret_key:
            bedrock: ConverseClient = boto3.client(
                "bedrock-runtime",
                region_name=region,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
            )
        else:
            bedrock = boto3.client("bedrock-runtime", region_name=region)

        logger.info("Bedrock client initialised (region=%s)", region)
        return bedrock

    @staticmethod
    def create(yaml_config: YAMLConfig) -> ConverseClient:
        """Create an LLM client from configuration.

        Args:
            yaml_config: YAML configuration manager.

        Returns:
            A client satisfying :class:`~src.llm.base.ConverseClient`.

        Raises:
            ConfigurationError: If the provider is unsupported or its required
                settings are missing.
        """
        return LLMClientFactory.create_from_dict(yaml_config.get_llm_config())


def resolve_model_id(
    config: dict[str, Any],
    provider: str,
    *,
    bedrock_model_id: str = "",
    vision: bool = False,
) -> str:
    """Resolve the model id for a provider from an LLM config mapping.

    Args:
        config: LLM configuration mapping.
        provider: A resolved provider name from
            :func:`resolve_llm_provider_name`.
        bedrock_model_id: Model id to use on Bedrock. Lets a call site keep its
            own override chain (note analysis has one) instead of always
            reading ``llm.bedrock_model_id``.
        vision: Select the vision model rather than the text model. Only
            Gemini distinguishes them; Bedrock uses one multimodal model.

    Returns:
        The model id to send.
    """
    if provider == GEMINI:
        from src.llm.gemini import DEFAULT_MODEL, DEFAULT_VISION_MODEL

        if vision:
            return (
                config.get("gemini_vision_model")
                or config.get("gemini_model")
                or DEFAULT_VISION_MODEL
            )
        return config.get("gemini_model") or DEFAULT_MODEL

    return bedrock_model_id or config.get("bedrock_model_id") or ""
