"""ClaudeLLM: the structured-output request contract and its errors, over a MockTransport."""

import json

import httpx2
import pytest

from app.errors import ProviderError
from app.llm import ClaudeLLM, Structured, Usage

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
MESSAGES = [{"role": "user", "content": "where is the cart total computed?"}]


def _message(text: str, stop_reason: str = "end_turn") -> httpx2.Response:
    """A Messages API reply with one text block."""
    return httpx2.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 50,
                "output_tokens": 20,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": None,
            },
        },
    )


def _claude(*replies: httpx2.Response | Exception) -> tuple[ClaudeLLM, list[httpx2.Request]]:
    """A ClaudeLLM whose requests get the replies in turn; and the requests it sent."""
    seen: list[httpx2.Request] = []

    def _handler(request: httpx2.Request) -> httpx2.Response:
        """Record the request, then return or raise the next reply."""
        seen.append(request)
        reply = replies[min(len(seen), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    return ClaudeLLM("test-key", "claude-sonnet-5", transport=httpx2.MockTransport(_handler)), seen


def _call(llm: ClaudeLLM) -> Structured:
    """One structured call with the test schema."""
    return llm.structured("Plan the search.", MESSAGES, SCHEMA, max_tokens=200, timeout=3.0)


def test_structured_call_sends_the_schema_without_sampling_settings_and_parses_the_json():
    llm, seen = _claude(_message('{"a": 1}'))
    assert _call(llm) == Structured({"a": 1}, Usage(input=50, output=20, cache_read=3))
    (request,) = seen
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == "test-key"
    assert json.loads(request.content) == {  # no temperature: the SDK has no sampling settings
        "model": "claude-sonnet-5",
        "max_tokens": 200,
        "system": "Plan the search.",
        "messages": MESSAGES,
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
    }


def test_server_error_is_not_retried():
    llm, seen = _claude(httpx2.Response(500, json={"type": "error"}))
    with pytest.raises(ProviderError, match="HTTP 500"):
        _call(llm)
    assert len(seen) == 1


def test_timeout_is_a_provider_error():
    llm, seen = _claude(httpx2.ReadTimeout("slow"))
    with pytest.raises(ProviderError, match="timed out"):
        _call(llm)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "reply",
    [
        _message("not json"),
        _message("[1, 2]"),  # JSON, but not an object
        _message('{"a": ', stop_reason="max_tokens"),  # cut off
        _message('{"a": 1}', stop_reason="refusal"),
    ],
)
def test_unusable_reply_is_a_provider_error(reply: httpx2.Response):
    llm, _ = _claude(reply)
    with pytest.raises(ProviderError, match="malformed"):
        _call(llm)
