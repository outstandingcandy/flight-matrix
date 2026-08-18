"""Tests for `src.llm.gemini`.

The Converse translation is asserted against the real ``google-genai`` types
rather than hand-rolled stubs, so a field rename in the SDK fails here instead
of at runtime on the gcp target. Only the transport is faked.

The load-bearing case is `test_history_round_trip`: `flight_agent` feeds the
assistant message it got back straight into the next request, so a tool call has
to survive being translated out of Gemini and back into it — including the
function *name*, which Gemini needs on the response part but the Converse
`toolResult` block does not carry.
"""

from __future__ import annotations

from typing import Any

import pytest
from google.genai import errors, types

from src.core.exceptions import AnalysisError, ConfigurationError
from src.llm.gemini import GeminiConverseClient

_TOOL_SPEC = {
    "toolSpec": {
        "name": "search_web",
        "description": "Search the web",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            }
        },
    }
}


class _FakeModels:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
        self.calls.append({"model": model, "contents": contents, "config": config})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeGenaiClient:
    """Stands in for ``google.genai.Client`` — only ``models`` is used."""

    def __init__(self, responses: list[Any]) -> None:
        self.models = _FakeModels(responses)


def _response(*parts: types.Part, prompt_tokens: int = 11, output_tokens: int = 7) -> Any:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens, candidates_token_count=output_tokens
        ),
    )


def _client(*responses: Any) -> tuple[GeminiConverseClient, _FakeGenaiClient]:
    fake = _FakeGenaiClient(list(responses))
    return GeminiConverseClient(client=fake), fake


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_missing_api_key_raises() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        GeminiConverseClient()
    assert "GEMINI_API_KEY" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Response translation
# ---------------------------------------------------------------------------


def test_text_response_has_converse_shape() -> None:
    client, _ = _client(_response(types.Part(text="An analysis.")))

    result = client.converse(
        modelId="gemini-3.7-flash",
        messages=[{"role": "user", "content": [{"text": "Analyse N703PA"}]}],
    )

    assert result["output"]["message"] == {
        "role": "assistant",
        "content": [{"text": "An analysis."}],
    }
    assert result["stopReason"] == "end_turn"
    assert result["usage"] == {"inputTokens": 11, "outputTokens": 7}


def test_function_call_becomes_tool_use() -> None:
    client, _ = _client(
        _response(
            types.Part(
                function_call=types.FunctionCall(name="search_web", args={"query": "N703PA owner"})
            )
        )
    )

    result = client.converse(messages=[{"role": "user", "content": [{"text": "go"}]}])

    assert result["stopReason"] == "tool_use"
    assert result["output"]["message"]["content"] == [
        {
            "toolUse": {
                "toolUseId": "search_web#0",
                "name": "search_web",
                "input": {"query": "N703PA owner"},
            }
        }
    ]


def test_function_call_id_is_kept_when_gemini_supplies_one() -> None:
    client, _ = _client(
        _response(
            types.Part(function_call=types.FunctionCall(id="fc-1", name="search_web", args={}))
        )
    )

    result = client.converse(messages=[{"role": "user", "content": [{"text": "go"}]}])

    assert result["output"]["message"]["content"][0]["toolUse"]["toolUseId"] == "fc-1"


def test_text_and_tool_calls_can_coexist() -> None:
    client, _ = _client(
        _response(
            types.Part(text="Looking that up."),
            types.Part(function_call=types.FunctionCall(name="search_web", args={"query": "x"})),
        )
    )

    blocks = client.converse(messages=[])["output"]["message"]["content"]

    assert [sorted(block) for block in blocks] == [["text"], ["toolUse"]]


def test_missing_usage_metadata_counts_as_zero() -> None:
    """A response without usage must not break TokenUsage accumulation."""
    response = types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[]))]
    )
    client, _ = _client(response)

    result = client.converse(messages=[])

    assert result["usage"] == {"inputTokens": 0, "outputTokens": 0}
    assert result["output"]["message"]["content"] == []


# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------


def test_system_prompt_and_inference_config_are_translated() -> None:
    client, fake = _client(_response(types.Part(text="ok")))

    client.converse(
        modelId="gemini-2.5-pro",
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        system=[{"text": "You are an analyst."}],
        inferenceConfig={"maxTokens": 4096, "temperature": 0.7},
    )

    call = fake.models.calls[0]
    assert call["model"] == "gemini-2.5-pro"
    assert call["config"].system_instruction == "You are an analyst."
    assert call["config"].max_output_tokens == 4096
    assert call["config"].temperature == 0.7


def test_automatic_function_calling_is_always_disabled() -> None:
    """Left on, google-genai would run tools and loop, bypassing the caller's
    iteration ceiling and progress callback."""
    client, fake = _client(_response(types.Part(text="ok")))

    client.converse(messages=[], toolConfig={"tools": [_TOOL_SPEC], "toolChoice": {"auto": {}}})

    config = fake.models.calls[0]["config"]
    assert config.automatic_function_calling.disable is True


def test_tool_specs_become_function_declarations() -> None:
    client, fake = _client(_response(types.Part(text="ok")))

    client.converse(messages=[], toolConfig={"tools": [_TOOL_SPEC], "toolChoice": {"auto": {}}})

    tools = fake.models.calls[0]["config"].tools
    assert len(tools) == 1
    declarations = tools[0].function_declarations
    assert [d.name for d in declarations] == ["search_web"]
    assert declarations[0].description == "Search the web"
    assert declarations[0].parameters_json_schema == _TOOL_SPEC["toolSpec"]["inputSchema"]["json"]


def test_no_tools_means_no_tool_config() -> None:
    client, fake = _client(_response(types.Part(text="ok")))

    client.converse(messages=[])

    assert fake.models.calls[0]["config"].tools is None


def test_non_auto_tool_choice_is_rejected() -> None:
    """Silently downgrading to auto would change model behaviour invisibly."""
    client, _ = _client(_response(types.Part(text="ok")))

    with pytest.raises(ConfigurationError):
        client.converse(messages=[], toolConfig={"tools": [_TOOL_SPEC], "toolChoice": {"any": {}}})


def test_images_are_sent_as_inline_bytes() -> None:
    client, fake = _client(_response(types.Part(text="ok")))

    client.converse(
        messages=[
            {
                "role": "user",
                "content": [
                    {"image": {"format": "png", "source": {"bytes": b"\x89PNG-data"}}},
                    {"text": "What livery is this?"},
                ],
            }
        ]
    )

    parts = fake.models.calls[0]["contents"][0].parts
    assert parts[0].inline_data.mime_type == "image/png"
    assert parts[0].inline_data.data == b"\x89PNG-data"
    assert parts[1].text == "What livery is this?"


def test_history_round_trip() -> None:
    """A tool call must survive Gemini -> Converse -> Gemini.

    `flight_agent` echoes the assistant message back verbatim, so the function
    name has to be recovered from history for the response part: the Converse
    `toolResult` block only carries the id.
    """
    client, fake = _client(_response(types.Part(text="done")))

    client.converse(
        messages=[
            {"role": "user", "content": [{"text": "Analyse N703PA"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "search_web#0",
                            "name": "search_web",
                            "input": {"query": "N703PA"},
                        }
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "search_web#0",
                            "content": [{"text": "Owned by Example Corp"}],
                        }
                    }
                ],
            },
        ]
    )

    contents = fake.models.calls[0]["contents"]
    assert [c.role for c in contents] == ["user", "model", "user"]

    call_part = contents[1].parts[0]
    assert call_part.function_call.name == "search_web"
    assert call_part.function_call.args == {"query": "N703PA"}

    response_part = contents[2].parts[0]
    assert response_part.function_response.name == "search_web"
    assert response_part.function_response.response == {"result": "Owned by Example Corp"}


def test_unknown_content_block_is_rejected() -> None:
    client, _ = _client(_response(types.Part(text="ok")))

    with pytest.raises(ConfigurationError):
        client.converse(messages=[{"role": "user", "content": [{"video": {}}]}])


def test_unknown_role_is_rejected() -> None:
    client, _ = _client(_response(types.Part(text="ok")))

    with pytest.raises(ConfigurationError):
        client.converse(messages=[{"role": "system", "content": [{"text": "hi"}]}])


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_api_error_becomes_analysis_error() -> None:
    api_error = errors.APIError(429, {"error": {"message": "rate limited"}})
    client, _ = _client(api_error)

    with pytest.raises(AnalysisError) as excinfo:
        client.converse(messages=[{"role": "user", "content": [{"text": "hi"}]}])

    message = str(excinfo.value)
    assert "429" in message
    assert "rate limited" in message
