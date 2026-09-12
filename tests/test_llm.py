"""ClaudeLLM: structured and answer request contracts and their errors, over a MockTransport."""

import json

import httpx2
import pytest

from app.errors import ProviderError
from app.llm import Citation, ClaudeLLM, Completion, FakeLLM, Structured, Usage

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


def test_structured_call_sends_the_schema_with_reasoning_off_and_parses_the_json():
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
        "thinking": {"type": "disabled"},  # JSON extraction: reasoning only costs time
    }


def test_a_server_error_is_retried_once_and_a_client_error_is_not():
    llm, seen = _claude(httpx2.Response(500, json={"type": "error"}))
    with pytest.raises(ProviderError, match="HTTP 500"):
        _call(llm)
    assert len(seen) == 2  # one immediate retry, inside the same timeout budget

    llm, seen = _claude(httpx2.Response(400, json={"type": "error"}))
    with pytest.raises(ProviderError, match="HTTP 400"):
        _call(llm)
    assert len(seen) == 1  # our request was wrong: sending it again would not help


def test_a_dropped_connection_is_retried_once_and_the_retry_stands():
    llm, seen = _claude(httpx2.ConnectError("reset"), _message('{"a": 1}'))
    assert _call(llm) == Structured({"a": 1}, Usage(input=50, output=20, cache_read=3))
    assert len(seen) == 2
    assert json.loads(seen[1].content)["thinking"] == {"type": "disabled"}  # the same request


def test_timeout_is_a_provider_error_and_is_never_retried():
    llm, seen = _claude(httpx2.ReadTimeout("slow"))
    with pytest.raises(ProviderError, match="timed out"):
        _call(llm)
    assert len(seen) == 1  # the budget is spent, however the retry rule is set


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


# ── Answers ───────────────────────────────────────────────────────────────────

SYSTEM = [
    {"type": "text", "text": "Answer from the sources."},
    {"type": "text", "text": "Repository octo/shop.", "cache_control": {"type": "ephemeral"}},
]
RESULT = {
    "type": "search_result",
    "source": "shop/cart.py:1-10",
    "title": "shop.cart.total",
    "content": [{"type": "text", "text": "def total(): ..."}],
    "citations": {"enabled": True},
}
ANSWER_MESSAGES = [
    {"role": "user", "content": [{"type": "text", "text": "where is the total?"}, RESULT]}
]
CITED = {
    "type": "search_result_location",
    "cited_text": "def total(): ...",
    "search_result_index": 0,
    "source": "shop/cart.py:1-10",
    "title": "shop.cart.total",
    "start_block_index": 0,
    "end_block_index": 1,
}
ELSEWHERE = {
    "type": "char_location",
    "cited_text": "x",
    "document_index": 0,
    "document_title": None,
    "start_char_index": 0,
    "end_char_index": 1,
}


def _reply(content: list[dict], stop_reason: str = "end_turn") -> httpx2.Response:
    """A Messages API reply with the given content blocks."""
    response = _message("", stop_reason)
    body = json.loads(response.content) | {"content": content}
    return httpx2.Response(200, json=body)


ANSWER = _reply(
    [
        {"type": "text", "text": "The total is ", "citations": None},
        {"type": "text", "text": "computed in total()", "citations": [CITED, ELSEWHERE]},
        {"type": "text", "text": "."},
    ]
)


def _answer(llm: ClaudeLLM) -> Completion:
    """One answer call over the test briefing."""
    return llm.complete(SYSTEM, ANSWER_MESSAGES, max_tokens=1500, timeout=60.0)


def test_answer_call_sends_system_blocks_and_search_results_without_sampling_settings():
    llm, seen = _claude(ANSWER)
    _answer(llm)
    (request,) = seen
    assert json.loads(request.content) == {
        "model": "claude-sonnet-5",
        "max_tokens": 1500,
        "system": SYSTEM,
        "messages": ANSWER_MESSAGES,
    }


def test_answer_text_is_joined_and_only_search_result_citations_are_kept():
    llm, _ = _claude(ANSWER)
    assert _answer(llm) == Completion(
        text="The total is computed in total().",
        citations=(Citation(0, "shop/cart.py:1-10", "shop.cart.total", "def total(): ...", 0, 1),),
        usage=Usage(input=50, output=20, cache_read=3),
    )


@pytest.mark.parametrize("status", [500, 529])
def test_a_server_error_is_retried_once(status: int):
    llm, seen = _claude(httpx2.Response(status, json={"type": "error"}), ANSWER)
    assert _answer(llm).text == "The total is computed in total()."
    assert len(seen) == 2


def test_a_second_server_error_is_a_provider_error():
    llm, seen = _claude(httpx2.Response(500, json={"type": "error"}))
    with pytest.raises(ProviderError, match="HTTP 500"):
        _answer(llm)
    assert len(seen) == 2


@pytest.mark.parametrize(
    "reply", [httpx2.Response(400, json={"type": "error"}), httpx2.ReadTimeout("slow")]
)
def test_client_errors_and_timeouts_are_not_retried(reply):
    llm, seen = _claude(reply)
    with pytest.raises(ProviderError):
        _answer(llm)
    assert len(seen) == 1


def test_an_answer_cut_at_max_tokens_is_kept_and_marked_truncated():
    llm, _ = _claude(_reply([{"type": "text", "text": "The total"}], stop_reason="max_tokens"))
    completion = _answer(llm)
    assert (completion.text, completion.truncated) == ("The total", True)


def test_a_refused_answer_is_a_provider_error():
    llm, _ = _claude(_reply([{"type": "text", "text": "No."}], stop_reason="refusal"))
    with pytest.raises(ProviderError, match="malformed"):
        _answer(llm)


# ── Fake ──────────────────────────────────────────────────────────────────────


def test_fake_answer_cites_the_first_search_result_of_this_turn_by_global_index():
    earlier = {**RESULT, "source": "old.py:1-5"}
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "q1"}, earlier]},
        {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
        *ANSWER_MESSAGES,
    ]
    llm = FakeLLM()
    completion = llm.complete(SYSTEM, messages, max_tokens=1500, timeout=60.0)
    (citation,) = completion.citations
    assert (citation.search_result_index, citation.source) == (1, "shop/cart.py:1-10")
    assert llm.requests[0]["kind"] == "complete"


def test_fake_answer_without_sources_is_not_found_and_scripts_come_first():
    no_sources = [{"role": "user", "content": [{"type": "text", "text": "q"}]}]
    scripted = Completion("Scripted.", (), Usage())
    llm = FakeLLM(completions=[scripted, ProviderError("down")])
    assert llm.complete(SYSTEM, no_sources, 1500, 60.0) is scripted
    with pytest.raises(ProviderError):
        llm.complete(SYSTEM, no_sources, 1500, 60.0)
    assert llm.complete(SYSTEM, no_sources, 1500, 60.0).text == "Not found in the indexed code."
