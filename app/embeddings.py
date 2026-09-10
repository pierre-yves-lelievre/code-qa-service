"""Embeddings: VoyageEmbeddings over httpx, FakeEmbeddings, token-budget batching, backoff."""

import hashlib
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.chunking import estimate_tokens
from app.config import settings
from app.errors import ProviderError
from app.logging_setup import get_logger

log = get_logger(__name__)

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
# Voyage allows 1,000 inputs per request and 120K tokens for its code and large models;
# voyage-code-4 is not listed, so the lowest documented limit is assumed, less estimate slack.
EMBED_BATCH_TOKENS = 100_000
EMBED_BATCH_ITEMS = 1_000
MAX_ATTEMPTS = 5
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
BACKOFF_JITTER_S = 0.5
KEY_CHECK_INPUT = "ping"


@dataclass(frozen=True)
class Embedded:
    """Vectors for one request, in input order, and the tokens the provider counted."""

    vectors: list[list[float]]
    tokens: int


# ── Batching ──────────────────────────────────────────────────────────────────


def token_batches(tokens: list[int], max_tokens: int, max_items: int) -> list[range]:
    """Split items, in order, into ranges under both limits; an oversize item goes alone."""
    batches: list[range] = []
    start = total = 0
    for i, count in enumerate(tokens):
        if i > start and (total + count > max_tokens or i - start == max_items):
            batches.append(range(start, i))
            start, total = i, 0
        total += count
    if start < len(tokens):
        batches.append(range(start, len(tokens)))
    return batches


def _check_batch(texts: list[str], max_tokens: int, max_items: int) -> None:
    """Refuse a batch over the request limits, unless it is a single oversize item."""
    over_tokens = len(texts) > 1 and sum(map(estimate_tokens, texts)) > max_tokens
    if len(texts) > max_items or over_tokens:
        raise ValueError("Embedding batch over the request limits; slice it with token_batches.")


# ── Voyage ────────────────────────────────────────────────────────────────────


class VoyageEmbeddings:
    """Voyage REST embeddings: one request per pre-sliced batch, retried on 429, 5xx, timeouts."""

    batch_tokens = EMBED_BATCH_TOKENS
    batch_items = EMBED_BATCH_ITEMS

    def __init__(
        self,
        api_key: str,
        model: str,
        dims: int,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Build the HTTP client; tests pass a MockTransport and a recording sleep."""
        self.model = model
        self.dims = dims
        self.usd_per_mtok = settings.embed_usd_per_mtok
        self._sleep = sleep
        self._http = httpx.Client(
            timeout=settings.embed_timeout_s,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    def embed_documents(self, texts: list[str]) -> Embedded:
        """Embed one batch of documents, already sliced by `token_batches`, in one request."""
        _check_batch(texts, self.batch_tokens, self.batch_items)
        if not texts:
            return Embedded([], 0)
        return self._post(texts, "document")

    def embed_query(self, text: str) -> Embedded:
        """Embed a search query."""
        return self._post([text], "query")

    def check_key(self) -> None:
        """Fail fast on a rejected key with one tiny request."""
        self._post([KEY_CHECK_INPUT], "query")

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()

    def _post(self, texts: list[str], input_type: str) -> Embedded:
        """One embeddings request, retried with backoff on 429, 5xx and transport errors."""
        body = {
            "input": texts,
            "model": self.model,
            "input_type": input_type,
            "output_dimension": self.dims,
            "truncation": False,
        }
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.monotonic()
            retry_after: str | None = None
            try:
                response = self._http.post(VOYAGE_URL, json=body)
            except httpx.TransportError as exc:
                status: int | str = type(exc).__name__
            else:
                if response.status_code == 200:
                    embedded = self._parse(response, len(texts))
                    log.info(
                        "embed_batch_done",
                        count=len(texts),
                        tokens=embedded.tokens,
                        ms=round((time.monotonic() - started) * 1000),
                    )
                    return embedded
                if response.status_code not in RETRY_STATUSES:
                    raise _rejected(response.status_code)
                status = response.status_code
                retry_after = response.headers.get("retry-after")
            if attempt == MAX_ATTEMPTS:
                break
            delay = _backoff(attempt, retry_after)
            log.warning("embed_retried", attempt=attempt, status=status, delay_s=round(delay, 2))
            self._sleep(delay)
        log.error("embed_failed", attempts=MAX_ATTEMPTS, count=len(texts))
        raise ProviderError(f"Voyage failed after {MAX_ATTEMPTS} attempts.")

    def _parse(self, response: httpx.Response, count: int) -> Embedded:
        """Vectors in input order and billed tokens; any other shape is a ProviderError."""
        try:
            payload: dict[str, Any] = response.json()
            items = sorted(payload["data"], key=lambda item: item["index"])
            indexes = [item["index"] for item in items]
            vectors = [[float(x) for x in item["embedding"]] for item in items]
            tokens = int(payload["usage"]["total_tokens"])
        except (ValueError, KeyError, TypeError):
            raise ProviderError("Voyage returned a malformed response.") from None
        if indexes != list(range(count)) or any(len(v) != self.dims for v in vectors):
            raise ProviderError("Voyage returned a malformed response.")
        return Embedded(vectors, tokens)


def _rejected(status: int) -> ProviderError:
    """The error for a status that retrying cannot fix."""
    if status in (401, 403):
        return ProviderError("Voyage rejected the API key.")
    return ProviderError(f"Voyage rejected the request (HTTP {status}).")


def _backoff(attempt: int, retry_after: str | None) -> float:
    """Seconds before the next attempt: Retry-After when numeric, else exponential with jitter."""
    try:
        wait = float(retry_after) if retry_after is not None else math.nan
    except ValueError:
        wait = math.nan
    if not math.isfinite(wait):
        wait = BACKOFF_BASE_S * 2 ** (attempt - 1) + random.uniform(0, BACKOFF_JITTER_S)
    return min(max(wait, 0.0), BACKOFF_CAP_S)


# ── Fake ──────────────────────────────────────────────────────────────────────


class FakeEmbeddings:
    """Test and dev double: unit-norm vectors seeded from a hash of the text; no network."""

    model = "fake"  # its own key in `embeddings`, so fake vectors never pass for real ones
    usd_per_mtok = 0.0
    batch_tokens = EMBED_BATCH_TOKENS
    batch_items = EMBED_BATCH_ITEMS

    def __init__(self, dims: int) -> None:
        """Keep the vector size."""
        self.dims = dims

    def embed_documents(self, texts: list[str]) -> Embedded:
        """Embed one batch under the same limits as Voyage; tokens are the estimate."""
        _check_batch(texts, self.batch_tokens, self.batch_items)
        vectors = [_fake_vector(text, self.dims) for text in texts]
        return Embedded(vectors, sum(map(estimate_tokens, texts)))

    def embed_query(self, text: str) -> Embedded:
        """Embed a search query."""
        return Embedded([_fake_vector(text, self.dims)], estimate_tokens(text))

    def check_key(self) -> None:
        """There is no key to check."""

    def close(self) -> None:
        """Nothing to close."""


def _fake_vector(text: str, dims: int) -> list[float]:
    """A unit-norm Gaussian vector seeded from the text's SHA-256; identical text, same vector."""
    rng = random.Random(hashlib.sha256(text.encode()).digest())
    values = [rng.gauss(0.0, 1.0) for _ in range(dims)]
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]
