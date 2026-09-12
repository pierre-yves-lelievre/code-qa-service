"""LLM: ClaudeLLM over the Anthropic SDK, and FakeLLM; structured output and cited answers."""

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import anthropic

from app.chunking import estimate_tokens
from app.errors import ProviderError
from app.logging_setup import get_logger

log = get_logger(__name__)

ANSWER_RETRIES = 1  # one immediate retry on a 5xx; backoff lives only in embeddings.py


@dataclass(frozen=True)
class Usage:
    """Tokens one call used, as the provider counted them."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass(frozen=True)
class Structured:
    """A structured-output reply: the parsed JSON object and the tokens the call used."""

    data: dict[str, Any]
    usage: Usage


@dataclass(frozen=True)
class Citation:
    """One search-result citation: the result's global index, its source, the blocks cited."""

    search_result_index: int
    source: str
    title: str | None
    cited_text: str
    start_block: int
    end_block: int  # exclusive


@dataclass(frozen=True)
class Completion:
    """An answer: its text, its search-result citations, the tokens used, whether it was cut."""

    text: str
    citations: tuple[Citation, ...]
    usage: Usage
    truncated: bool = False


# ── Claude ────────────────────────────────────────────────────────────────────


class ClaudeLLM:
    """Anthropic Messages API: explicit timeouts, no SDK retries, errors as ProviderError."""

    def __init__(self, api_key: str, model: str, *, transport: Any = None) -> None:
        """Build the SDK client without its own retries; tests pass an httpx2 MockTransport."""
        self.model = model
        http = anthropic.DefaultHttpxClient(transport=transport) if transport is not None else None
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=0, http_client=http)

    def structured(
        self,
        system: str,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        max_tokens: int,
        timeout: float,
    ) -> Structured:
        """One call constrained to a JSON schema; returns the parsed object (no sampling knobs).

        Reasoning is off: the model thinks by default, and on the planner that was ~290 hidden
        output tokens and 4-6 s for ~40 tokens of JSON, enough to hit the timeout.

        A dropped connection or a 5xx is retried once inside the same timeout budget, since one
        blip otherwise costs the planner its identifiers. A timeout or a 4xx is final.
        """
        message, usage = self._create(
            "structured",
            1,
            retry_connection=True,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            thinking={"type": "disabled"},
            timeout=timeout,
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        return Structured(_json_object(text, message.stop_reason), usage)

    def complete(
        self,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int,
        timeout: float,
    ) -> Completion:
        """One answer call over search-result blocks; a 5xx is retried once, immediately."""
        message, usage = self._create(
            "complete",
            ANSWER_RETRIES,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            timeout=timeout,
        )
        if message.stop_reason not in ("end_turn", "max_tokens"):
            raise ProviderError("Claude returned a malformed response.")
        return _completion(message.content, usage, message.stop_reason == "max_tokens")

    def _create(
        self, kind: str, retries: int, *, retry_connection: bool = False, **request: Any
    ) -> tuple[anthropic.types.Message, Usage]:
        """Send one request, retrying a 5xx (and a dropped connection when asked) up to `retries`
        times within the original timeout; log shapes, never content."""
        started = time.monotonic()
        budget = float(request.get("timeout") or 0.0)
        attempt = 0
        while True:
            try:
                message = self._client.messages.create(model=self.model, **request)
            except anthropic.APIStatusError as exc:
                left = _left(started, budget)
                if exc.status_code >= 500 and attempt < retries and left:
                    attempt += 1
                    request["timeout"] = left
                    log.warning("llm_call_retried", kind=kind, status=exc.status_code)
                    continue
                log.warning("llm_call_failed", kind=kind, status=exc.status_code)
                raise ProviderError(
                    f"Claude rejected the request (HTTP {exc.status_code})."
                ) from None
            except anthropic.APITimeoutError:  # the budget is spent: never retried
                log.warning("llm_call_failed", kind=kind, error_type="APITimeoutError")
                raise ProviderError("Claude timed out or is unreachable.") from None
            except anthropic.APIConnectionError as exc:
                left = _left(started, budget)
                if retry_connection and attempt < retries and left:
                    attempt += 1
                    request["timeout"] = left
                    log.warning("llm_call_retried", kind=kind, error_type=type(exc).__name__)
                    continue
                log.warning("llm_call_failed", kind=kind, error_type=type(exc).__name__)
                raise ProviderError("Claude timed out or is unreachable.") from None
            except anthropic.APIError as exc:
                log.warning("llm_call_failed", kind=kind, error_type=type(exc).__name__)
                raise ProviderError("Claude timed out or is unreachable.") from None
            break
        usage = _usage(message.usage)
        log.info(
            "llm_call_done",
            kind=kind,
            input_tokens=usage.input,
            output_tokens=usage.output,
            cache_read_tokens=usage.cache_read,
            cache_write_tokens=usage.cache_write,
            stop_reason=message.stop_reason,
            attempts=attempt + 1,
            ms=round((time.monotonic() - started) * 1000),
        )
        return message, usage

    def close(self) -> None:
        """Close the SDK client."""
        self._client.close()


def _left(started: float, budget: float) -> float:
    """What is left of the timeout budget; 0.0 when it is spent, so no retry is attempted."""
    return max(0.0, budget - (time.monotonic() - started))


def _usage(usage: anthropic.types.Usage) -> Usage:
    """Our token counts from the SDK's."""
    return Usage(
        input=usage.input_tokens,
        output=usage.output_tokens,
        cache_read=usage.cache_read_input_tokens or 0,
        cache_write=usage.cache_creation_input_tokens or 0,
    )


def _json_object(text: str, stop_reason: str | None) -> dict[str, Any]:
    """The reply as a JSON object; a cut-off, refused or non-object reply is a ProviderError."""
    try:
        data = json.loads(text) if stop_reason == "end_turn" else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ProviderError("Claude returned a malformed response.")
    return data


def _completion(content: Sequence[Any], usage: Usage, truncated: bool) -> Completion:
    """The text blocks joined, with their search-result citations; other kinds are counted."""
    text: list[str] = []
    citations: list[Citation] = []
    ignored = 0
    for block in content:
        if block.type != "text":
            continue
        text.append(block.text)
        for cited in block.citations or []:
            if cited.type != "search_result_location":
                ignored += 1
                continue
            citations.append(
                Citation(
                    search_result_index=cited.search_result_index,
                    source=cited.source,
                    title=cited.title,
                    cited_text=cited.cited_text,
                    start_block=cited.start_block_index,
                    end_block=cited.end_block_index,
                )
            )
    if ignored:
        log.info("llm_citations_ignored", count=ignored)
    return Completion("".join(text), tuple(citations), usage, truncated)


# ── Fake ──────────────────────────────────────────────────────────────────────


class FakeLLM:
    """Test and dev double: scripted replies in order, then canned ones; records each request."""

    model = "fake"

    def __init__(
        self,
        replies: Sequence[dict[str, Any] | Exception] = (),
        completions: Sequence[Completion | Exception] = (),
    ) -> None:
        """Keep the scripted replies; an exception among them is raised when its turn comes."""
        self._replies = list(replies)
        self._completions = list(completions)
        self.requests: list[dict[str, Any]] = []

    def structured(
        self,
        system: str,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        max_tokens: int,
        timeout: float,
    ) -> Structured:
        """The next scripted reply, else a canned summary or a plan of the last user message."""
        request = {
            "kind": "structured",
            "system": system,
            "messages": messages,
            "schema": schema,
            "max_tokens": max_tokens,
            "timeout": timeout,
        }
        self.requests.append(request)
        if self._replies:
            reply = self._replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
        elif "summary" in schema.get("properties", {}):
            reply = {
                "summary": "A fake summary of the repository.",
                "questions": [f"Fake suggested question {i}?" for i in range(1, 5)],
            }
        else:
            reply = {"query": messages[-1]["content"], "identifiers": [], "intent": "explain"}
        usage = Usage(
            input=estimate_tokens(json.dumps([system, messages])),
            output=estimate_tokens(json.dumps(reply)),
        )
        return Structured(reply, usage)

    def complete(
        self,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int,
        timeout: float,
    ) -> Completion:
        """The next scripted completion, else one citing the first search result of this turn."""
        request = {
            "kind": "complete",
            "system": system,
            "messages": messages,
            "max_tokens": max_tokens,
            "timeout": timeout,
        }
        self.requests.append(request)
        if self._completions:
            completion = self._completions.pop(0)
            if isinstance(completion, Exception):
                raise completion
            return completion
        earlier = sum(len(_search_results(m)) for m in messages[:-1])
        current = _search_results(messages[-1])
        if current:
            first = current[0]
            text = f"See {first['title']}."
            citations = (
                Citation(
                    earlier, first["source"], first["title"], first["content"][0]["text"], 0, 1
                ),
            )
        else:
            text, citations = "Not found in the indexed code.", ()
        usage = Usage(
            input=estimate_tokens(json.dumps([system, messages])), output=estimate_tokens(text)
        )
        return Completion(text, citations, usage)

    def close(self) -> None:
        """Nothing to close."""


def _search_results(message: dict[str, Any]) -> list[dict[str, Any]]:
    """The search-result blocks of one message."""
    content = message["content"]
    return [b for b in content if b["type"] == "search_result"] if isinstance(content, list) else []
