"""Gemini implementation of the Converse protocol.

Translates the Bedrock Converse request/response shape described in
:mod:`src.llm.base` onto ``google-genai``, so the analysis call sites and their
hand-written agent loop run unchanged on the gcp target.

Two Gemini behaviours are handled explicitly because getting them wrong fails
quietly rather than loudly:

- **Automatic function calling is disabled.** ``google-genai`` will happily
  execute tools and loop on its own when it is handed Python callables, which
  would bypass ``flight_agent``'s iteration ceiling and progress callback. Tools
  are therefore declared as :class:`~google.genai.types.FunctionDeclaration`
  only, plus an explicit
  :class:`~google.genai.types.AutomaticFunctionCallingConfig` with
  ``disable=True``.
- **The assistant role is called ``model``.** ``flight_agent`` appends the
  response message straight back into its history, so the round trip has to map
  ``assistant`` → ``model`` on the way out and back again on the way in.

Gemini also does not always return an id alongside a function call, while the
Converse shape requires one to pair a ``toolResult`` with its ``toolUse``. Where
the id is missing a deterministic ``<name>#<index>`` id is synthesised; because
the same id then travels through the caller's history, the reverse lookup that
recovers the function *name* for the response part still resolves.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.core.exceptions import AnalysisError, ConfigurationError

if TYPE_CHECKING:
    from google.genai import types

logger = logging.getLogger("llm.gemini")

__all__ = ["DEFAULT_MODEL", "DEFAULT_VISION_MODEL", "GeminiConverseClient"]

# Verified against the models endpoint at implementation time. Gemini iterates
# fast; re-check with
#   curl -s "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY"
DEFAULT_MODEL = "gemini-3.7-flash"
DEFAULT_VISION_MODEL = "gemini-2.5-pro"

# Converse role -> Gemini role. Function results are sent as a user turn, which
# is what the Gemini API accepts; `role` only takes "user" or "model".
_ROLE_MAP = {"user": "user", "assistant": "model", "tool": "user"}


class GeminiConverseClient:
    """Answers Converse-shaped requests using the Gemini Developer API.

    Args:
        api_key: Gemini API key. Ignored when ``client`` is supplied.
        client: Pre-built ``google.genai.Client``. Injected by tests and by
            call sites that build their own client.

    Raises:
        ConfigurationError: If neither ``api_key`` nor ``client`` is given.
    """

    def __init__(self, api_key: str = "", client: Any = None) -> None:
        if client is not None:
            self._client = client
        else:
            if not api_key:
                raise ConfigurationError(
                    "Gemini provider selected but no API key is configured. "
                    "Set GEMINI_API_KEY (llm.gemini_api_key)."
                )
            from google import genai

            self._client = genai.Client(api_key=api_key)

        logger.info("Gemini client initialised")

    # ------------------------------------------------------------------
    # Protocol entry point
    # ------------------------------------------------------------------

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        """Run one Gemini turn from a Converse-shaped request.

        Args:
            **kwargs: See :mod:`src.llm.base` for the accepted keys.

        Returns:
            A Converse-shaped response mapping.

        Raises:
            AnalysisError: If the Gemini API call fails.
            ConfigurationError: If the request uses a Converse feature that has
                no Gemini equivalent.
        """
        from google.genai import errors

        model = kwargs.get("modelId") or DEFAULT_MODEL
        contents = self._to_contents(kwargs.get("messages") or [])
        config = self._to_config(kwargs)

        try:
            response = self._client.models.generate_content(
                model=model, contents=contents, config=config
            )
        except errors.APIError as e:
            code = getattr(e, "code", None) or getattr(e, "status", "")
            raise AnalysisError(f"Gemini API error ({code}): {e.message}") from e

        return self._from_response(response)

    # ------------------------------------------------------------------
    # Request translation
    # ------------------------------------------------------------------

    def _to_contents(self, messages: list[dict[str, Any]]) -> list[types.Content]:
        """Translate Converse messages into Gemini contents.

        Args:
            messages: Converse ``messages`` list.

        Returns:
            The equivalent Gemini contents.

        Raises:
            ConfigurationError: If a message uses an unknown role or an unknown
                content block.
        """
        from google.genai import types

        # toolUseId -> function name, so a later toolResult can name its
        # function response. Gemini pairs responses by name, not by id.
        tool_names: dict[str, str] = {}
        contents: list[types.Content] = []

        for message in messages:
            role = message.get("role", "user")
            if role not in _ROLE_MAP:
                raise ConfigurationError(f"Unsupported message role for Gemini: {role!r}")

            parts = [
                self._to_part(block, tool_names) for block in message.get("content") or [] if block
            ]
            if parts:
                contents.append(types.Content(role=_ROLE_MAP[role], parts=parts))

        return contents

    def _to_part(self, block: dict[str, Any], tool_names: dict[str, str]) -> types.Part:
        """Translate one Converse content block into a Gemini part.

        Args:
            block: A single Converse content block.
            tool_names: Accumulated ``toolUseId`` → function-name mapping. Read
                for ``toolResult`` blocks and written for ``toolUse`` blocks.

        Returns:
            The equivalent Gemini part.

        Raises:
            ConfigurationError: If the block type is not supported.
        """
        from google.genai import types

        if "text" in block:
            return types.Part.from_text(text=block["text"])

        if "image" in block:
            image = block["image"]
            image_format = image.get("format") or "jpeg"
            return types.Part.from_bytes(
                data=image["source"]["bytes"], mime_type=f"image/{image_format}"
            )

        if "toolUse" in block:
            tool_use = block["toolUse"]
            tool_names[tool_use.get("toolUseId", "")] = tool_use["name"]
            return types.Part.from_function_call(
                name=tool_use["name"], args=tool_use.get("input") or {}
            )

        if "toolResult" in block:
            tool_result = block["toolResult"]
            tool_use_id = tool_result.get("toolUseId", "")
            name = tool_names.get(tool_use_id, tool_use_id)
            text = "".join(
                item.get("text", "") for item in tool_result.get("content") or [] if item
            )
            return types.Part.from_function_response(name=name, response={"result": text})

        raise ConfigurationError(f"Unsupported Converse content block for Gemini: {sorted(block)}")

    def _to_config(self, request: dict[str, Any]) -> types.GenerateContentConfig:
        """Build the Gemini generation config from a Converse request.

        Args:
            request: The full Converse request mapping.

        Returns:
            The equivalent generation config, with automatic function calling
            switched off.

        Raises:
            ConfigurationError: If an unsupported ``toolChoice`` is requested.
        """
        from google.genai import types

        system_blocks = request.get("system") or []
        system_instruction = "\n".join(block.get("text", "") for block in system_blocks).strip()

        inference = request.get("inferenceConfig") or {}
        tool_config = request.get("toolConfig") or {}

        choice = tool_config.get("toolChoice") or {}
        if choice and "auto" not in choice:
            raise ConfigurationError(
                f"Only toolChoice 'auto' is supported on Gemini, got {sorted(choice)}"
            )

        tools: list[Any] | None = None
        declarations = [
            self._to_function_declaration(spec) for spec in tool_config.get("tools") or []
        ]
        if declarations:
            tools = [types.Tool(function_declarations=declarations)]

        return types.GenerateContentConfig(
            system_instruction=system_instruction or None,
            max_output_tokens=inference.get("maxTokens"),
            temperature=inference.get("temperature"),
            tools=tools,
            # Never let the SDK run tools and loop by itself: the callers own
            # their loops, their iteration ceilings and their progress reporting.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    @staticmethod
    def _to_function_declaration(spec: dict[str, Any]) -> types.FunctionDeclaration:
        """Translate one Converse ``toolSpec`` into a Gemini declaration.

        Args:
            spec: A Converse tool entry, i.e. ``{"toolSpec": {...}}``.

        Returns:
            The equivalent function declaration.
        """
        from google.genai import types

        tool_spec = spec.get("toolSpec", spec)
        schema = (tool_spec.get("inputSchema") or {}).get("json") or {}

        return types.FunctionDeclaration(
            name=tool_spec["name"],
            description=tool_spec.get("description", ""),
            parameters_json_schema=schema,
        )

    # ------------------------------------------------------------------
    # Response translation
    # ------------------------------------------------------------------

    @staticmethod
    def _from_response(response: Any) -> dict[str, Any]:
        """Translate a Gemini response into the Converse shape.

        Args:
            response: A ``google.genai`` ``GenerateContentResponse``.

        Returns:
            A Converse-shaped response mapping. Parts are read directly rather
            than via ``response.text``, whose behaviour varies once a reply
            mixes text with function calls.
        """
        content_blocks: list[dict[str, Any]] = []
        candidates = getattr(response, "candidates", None) or []
        parts: list[Any] = []
        if candidates:
            candidate_content = getattr(candidates[0], "content", None)
            parts = getattr(candidate_content, "parts", None) or []

        tool_use_count = 0
        for part in parts:
            text = getattr(part, "text", None)
            if text:
                content_blocks.append({"text": text})
                continue

            function_call = getattr(part, "function_call", None)
            if function_call is not None:
                name = function_call.name or ""
                content_blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": function_call.id or f"{name}#{tool_use_count}",
                            "name": name,
                            "input": dict(function_call.args or {}),
                        }
                    }
                )
                tool_use_count += 1

        usage = getattr(response, "usage_metadata", None)
        return {
            "output": {"message": {"role": "assistant", "content": content_blocks}},
            "stopReason": "tool_use" if tool_use_count else "end_turn",
            "usage": {
                "inputTokens": getattr(usage, "prompt_token_count", 0) or 0,
                "outputTokens": getattr(usage, "candidates_token_count", 0) or 0,
            },
        }
