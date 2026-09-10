"""Embedding clients: the Voyage request contract, retries, response checks, and the fake."""

import json
import math
from collections.abc import Callable

import httpx
import pytest

from app.embeddings import (
    EMBED_BATCH_ITEMS,
    MAX_ATTEMPTS,
    VOYAGE_URL,
    FakeEmbeddings,
    VoyageEmbeddings,
)
from app.errors import ProviderError

DIMS = 4
Handler = Callable[[httpx.Request], httpx.Response]


def _ok(request: httpx.Request) -> httpx.Response:
    """A well-formed reply: one vector per input, listed in reverse index order."""
    count = len(json.loads(request.content)["input"])
    data = [
        {"object": "embedding", "embedding": [float(i)] * DIMS, "index": i} for i in range(count)
    ]
    return httpx.Response(
        200,
        json={
            "object": "list",
            "data": data[::-1],
            "model": "voyage-code-4",
            "usage": {"total_tokens": 7 * count},
        },
    )


def _sequence(*replies: httpx.Response | Exception | Handler) -> tuple[Handler, list[int]]:
    """A handler giving each reply in turn (the last one repeats), and a call counter."""
    calls: list[int] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Return or raise the next reply."""
        reply = replies[min(len(calls), len(replies) - 1)]
        calls.append(1)
        if isinstance(reply, Exception):
            raise reply
        return reply(request) if callable(reply) else reply

    return _handler, calls


def _voyage(handler: Handler, sleeps: list[float] | None = None) -> VoyageEmbeddings:
    """A VoyageEmbeddings over a MockTransport, recording its backoff delays."""
    return VoyageEmbeddings(
        "test-key",
        "voyage-code-4",
        DIMS,
        transport=httpx.MockTransport(handler),
        sleep=(sleeps if sleeps is not None else []).append,
    )


# ── Voyage request contract ───────────────────────────────────────────────────


def test_documents_are_sent_with_model_dims_input_type_and_bearer_key():
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Record the request, then reply normally."""
        seen.append(request)
        return _ok(request)

    embedded = _voyage(_handler).embed_documents(["def a(): pass", "def b(): pass"])
    (request,) = seen
    assert str(request.url) == VOYAGE_URL
    assert request.headers["authorization"] == "Bearer test-key"
    assert json.loads(request.content) == {
        "input": ["def a(): pass", "def b(): pass"],
        "model": "voyage-code-4",
        "input_type": "document",
        "output_dimension": DIMS,
        "truncation": False,
    }
    assert embedded.vectors == [[0.0] * DIMS, [1.0] * DIMS]  # back in input order
    assert embedded.tokens == 14  # usage.total_tokens, not the estimate


def test_query_uses_the_query_input_type():
    seen: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Record the body, then reply normally."""
        seen.append(json.loads(request.content))
        return _ok(request)

    _voyage(_handler).embed_query("where is the cart total computed?")
    assert seen[0]["input_type"] == "query"


def test_check_key_sends_one_tiny_request():
    handler, calls = _sequence(_ok)
    _voyage(handler).check_key()
    assert len(calls) == 1


def test_empty_batch_makes_no_request():
    handler, calls = _sequence(_ok)
    assert _voyage(handler).embed_documents([]).vectors == []
    assert calls == []


def test_oversize_batch_is_refused_before_any_request():
    handler, calls = _sequence(_ok)
    with pytest.raises(ValueError):
        _voyage(handler).embed_documents(["x"] * (EMBED_BATCH_ITEMS + 1))
    assert calls == []


# ── Retries ───────────────────────────────────────────────────────────────────


def test_rate_limit_is_retried_after_an_exponential_delay():
    handler, calls = _sequence(httpx.Response(429), _ok)
    sleeps: list[float] = []
    embedded = _voyage(handler, sleeps).embed_documents(["x"])
    assert len(embedded.vectors) == 1
    assert len(calls) == 2
    (delay,) = sleeps
    assert 1.0 <= delay <= 1.5  # base delay plus jitter


def test_numeric_retry_after_sets_the_delay():
    handler, _ = _sequence(httpx.Response(503, headers={"Retry-After": "3"}), _ok)
    sleeps: list[float] = []
    _voyage(handler, sleeps).embed_documents(["x"])
    assert sleeps == [3.0]


def test_timeouts_are_retried():
    handler, calls = _sequence(httpx.ReadTimeout("slow"), _ok)
    _voyage(handler).embed_documents(["x"])
    assert len(calls) == 2


def test_persistent_server_errors_fail_after_five_attempts():
    handler, calls = _sequence(httpx.Response(503))
    sleeps: list[float] = []
    with pytest.raises(ProviderError, match="after 5 attempts"):
        _voyage(handler, sleeps).embed_documents(["x"])
    assert len(calls) == MAX_ATTEMPTS
    assert len(sleeps) == MAX_ATTEMPTS - 1
    assert sleeps == sorted(sleeps)  # the delay grows


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_is_not_retried(status: int):
    handler, calls = _sequence(httpx.Response(status))
    sleeps: list[float] = []
    with pytest.raises(ProviderError, match="rejected the API key"):
        _voyage(handler, sleeps).check_key()
    assert (len(calls), sleeps) == (1, [])


def test_bad_request_is_not_retried():
    handler, calls = _sequence(httpx.Response(400))
    with pytest.raises(ProviderError, match="HTTP 400"):
        _voyage(handler).embed_documents(["x"])
    assert len(calls) == 1


# ── Response checks ───────────────────────────────────────────────────────────


def _reply(data: object, usage: object = None) -> httpx.Response:
    """A 200 with the given `data` and `usage`."""
    return httpx.Response(
        200, json={"data": data, "usage": {"total_tokens": 1} if usage is None else usage}
    )


@pytest.mark.parametrize(
    "reply",
    [
        _reply([]),  # no vector for the input
        _reply([{"embedding": [0.0] * (DIMS + 1), "index": 0}]),  # wrong size
        _reply([{"embedding": [0.0] * DIMS, "index": 3}]),  # unknown index
        _reply([{"embedding": [0.0] * DIMS, "index": 0}], usage={}),  # no token count
        httpx.Response(200, text="not json"),
    ],
)
def test_malformed_response_is_a_provider_error(reply: httpx.Response):
    handler, _ = _sequence(reply)
    with pytest.raises(ProviderError, match="malformed"):
        _voyage(handler).embed_documents(["x"])


# ── Fake ──────────────────────────────────────────────────────────────────────


def test_fake_vectors_are_deterministic_unit_norm_and_distinct():
    fake = FakeEmbeddings(dims=64)
    first = fake.embed_documents(["alpha", "beta"])
    again = FakeEmbeddings(dims=64).embed_documents(["alpha", "beta"])
    assert first == again
    for vector in first.vectors:
        assert len(vector) == 64
        assert math.isclose(math.fsum(v * v for v in vector), 1.0)
    assert first.vectors[0] != first.vectors[1]
    assert fake.embed_query("alpha").vectors[0] == first.vectors[0]
    assert fake.model == "fake"
