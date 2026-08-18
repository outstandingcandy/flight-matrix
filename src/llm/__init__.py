"""LLM provider abstraction for the aws, gcp and local deployment targets.

The interface is the Bedrock Converse request/response shape (see
:mod:`src.llm.base`), because the analysis call sites — including
``flight_agent``'s hand-written tool loop — were built against it and must keep
behaving identically on every target.

| ``DEPLOY_TARGET`` | Provider     | Client                                     |
|-------------------|--------------|--------------------------------------------|
| ``aws``           | ``aws_bedrock`` | boto3 ``bedrock-runtime``               |
| ``gcp``           | ``gemini``   | :class:`~src.llm.gemini.GeminiConverseClient` |
| ``local``         | ``gemini`` with ``GEMINI_API_KEY``, else ``aws_bedrock`` | either |

Usage::

    from src.llm.factory import LLMClientFactory, resolve_llm_provider_name

    provider = resolve_llm_provider_name(yaml_config.get("llm.provider", ""))
    client = LLMClientFactory.create(yaml_config)
    response = client.converse(modelId=model_id, messages=messages)
"""

from src.llm.base import ConverseClient
from src.llm.factory import (
    AWS_BEDROCK,
    GEMINI,
    IMPLEMENTED_PROVIDERS,
    KNOWN_PROVIDERS,
    LLMClientFactory,
    resolve_llm_provider_name,
    resolve_model_id,
)
from src.llm.gemini import DEFAULT_MODEL, DEFAULT_VISION_MODEL, GeminiConverseClient

__all__ = [
    "AWS_BEDROCK",
    "DEFAULT_MODEL",
    "DEFAULT_VISION_MODEL",
    "GEMINI",
    "IMPLEMENTED_PROVIDERS",
    "KNOWN_PROVIDERS",
    "ConverseClient",
    "GeminiConverseClient",
    "LLMClientFactory",
    "resolve_llm_provider_name",
    "resolve_model_id",
]
