"""LLM client abstraction shared by the aws, gcp and local targets.

The three analysis call sites (``src/analysis/flight_agent.py``,
``src/services/note_analysis_service.py``,
``src/services/aircraft_analysis_service.py``) were written against the Bedrock
Converse API, and one of them hand-writes an agent loop around it: it echoes the
assistant message straight back into ``messages``, counts iterations itself, and
drives a progress callback per round.

Rather than rewrite those loops per provider, this package keeps the **Converse
request/response shape as the interface** and translates it inside each
implementation. A provider switch is therefore a factory change, and the loops,
the ``max_iterations`` ceiling, the progress reporting and the JSON-fence
parsing all stay exactly as they are on every target.

``S3``-style note: on the aws target the implementation *is* the boto3
``bedrock-runtime`` client — it already speaks this protocol, so nothing wraps
it and the production path is bit-for-bit unchanged.

Supported request keys (the subset the call sites actually send)::

    modelId: str
    messages: [{"role": "user" | "assistant", "content": [block, ...]}, ...]
    system: [{"text": str}]                                   # optional
    toolConfig: {"tools": [toolSpec, ...],                     # optional
                 "toolChoice": {"auto": {}}}
    inferenceConfig: {"maxTokens": int, "temperature": float}  # optional

Supported content blocks::

    {"text": str}
    {"image": {"format": "jpeg" | "png" | ..., "source": {"bytes": bytes}}}
    {"toolUse": {"toolUseId": str, "name": str, "input": dict}}
    {"toolResult": {"toolUseId": str, "content": [{"text": str}]}}

Response shape::

    {"output": {"message": {"role": "assistant", "content": [block, ...]}},
     "stopReason": "tool_use" | "end_turn",
     "usage": {"inputTokens": int, "outputTokens": int}}
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["ConverseClient"]


@runtime_checkable
class ConverseClient(Protocol):
    """A client that answers Bedrock-Converse-shaped requests.

    Declared as a Protocol rather than an ABC so that the boto3
    ``bedrock-runtime`` client satisfies it without being wrapped or
    subclassed.
    """

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        """Run one model turn.

        Args:
            **kwargs: The request keys documented in this module.

        Returns:
            The response mapping documented in this module.

        Raises:
            AnalysisError: If the provider rejects the request or replies with
                something that cannot be translated.
        """
        ...
