"""LLM: ClaudeLLM over the Anthropic SDK, and FakeLLM; structured output now, answers in Phase 7."""

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


# ── Claude ────────────────────────────────────────────────────────────────────


class ClaudeLLM:
    """Anthropic Messages API: one attempt per call, explicit timeout, errors as ProviderError."""

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
        """One call constrained to a JSON schema; returns the parsed object (no sampling knobs)."""
        started = time.monotonic()
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,  # type: ignore[arg-type]
                output_config={"format": {"type": "json_schema", "schema": schema}},
                timeout=timeout,
            )
        except anthropic.APIStatusError as exc:
            log.warning("llm_call_failed", kind="structured", status=exc.status_code)
            raise ProviderError(f"Claude rejected the request (HTTP {exc.status_code}).") from None
        except anthropic.APIError as exc:  # timeouts and connection errors
            log.warning("llm_call_failed", kind="structured", error_type=type(exc).__name__)
            raise ProviderError("Claude timed out or is unreachable.") from None
        usage = _usage(message.usage)
        log.info(
            "llm_call_done",
            kind="structured",
            input_tokens=usage.input,
            output_tokens=usage.output,
            stop_reason=message.stop_reason,
            ms=round((time.monotonic() - started) * 1000),
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        return Structured(_json_object(text, message.stop_reason), usage)

    def close(self) -> None:
        """Close the SDK client."""
        self._client.close()


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


# ── Fake ──────────────────────────────────────────────────────────────────────


class FakeLLM:
    """Test and dev double: scripted replies in order, then a canned plan; records each request."""

    model = "fake"

    def __init__(self, replies: Sequence[dict[str, Any] | Exception] = ()) -> None:
        """Keep the scripted replies; an exception among them is raised when its turn comes."""
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def structured(
        self,
        system: str,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        max_tokens: int,
        timeout: float,
    ) -> Structured:
        """The next scripted reply, else a plan whose query is the last user message."""
        request = {
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
        else:
            reply = {"query": messages[-1]["content"], "identifiers": [], "intent": "explain"}
        usage = Usage(
            input=estimate_tokens(json.dumps([system, messages])),
            output=estimate_tokens(json.dumps(reply)),
        )
        return Structured(reply, usage)

    def close(self) -> None:
        """Nothing to close."""
