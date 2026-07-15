"""Embedding helpers for cross-page consistency and finding dedup.

Wraps the Ollama bge-m3 endpoint at ``EMBEDDINGS_API_URL``. Used by the
cross-page aggregator for SC 3.2.3 (Consistent Navigation) and SC 3.2.4
(Consistent Identification), and by the finding deduplicator to cluster
semantically equivalent findings across crawled pages.

Every embedding call goes through this module. No other file makes raw
HTTP requests to the embedding host.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Sequence

import httpx

from config import (
    EMBEDDINGS_API_KEY,
    EMBEDDINGS_API_URL,
    EMBEDDINGS_DIM,
    EMBEDDINGS_FORMAT,
    EMBEDDINGS_MODEL,
)

logger = logging.getLogger(__name__)


# Global serialization lock for the embedding endpoint.
#
# Ollama's embedding runner is single-threaded per model: it processes
# one request at a time and queues the rest. Anything more than one
# in-flight request from our side just clogs that queue and turns
# into ``ReadTimeout`` failures (empirically 50%+ loss at concurrency=4
# on a 148-pattern run). Since concurrent calls offer no real speedup
# against a single-threaded backend, we serialize EVERY embedding call
# through this lock: one request starts only after the previous one
# has returned. Predictable, no queueing pressure, no retry storms.
#
# The lock is module-global so independent callers (sc_retrieval,
# finding_deduper, cross-page aggregator, etc.) also cannot overlap --
# not just calls within a single embed_batch invocation.
_embed_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Lazy-init the lock so it binds to whichever event loop is running.

    ``asyncio.Lock()`` captures the current running loop at construction.
    Constructing at import time would bind to the wrong loop (or no
    loop) in test contexts and in uvicorn workers that start their own
    loop after import. Building it on first use means the first caller
    in each loop gets a lock tied to THAT loop; subsequent callers in
    the same loop reuse it.
    """
    global _embed_lock
    if _embed_lock is None:
        _embed_lock = asyncio.Lock()
    return _embed_lock


class EmbeddingError(RuntimeError):
    """Raised when the embedding host is unreachable or returns garbage."""


async def embed(
    text: str,
    *,
    model: str | None = None,
    timeout_s: float = 30.0,
    chunk_chars: int = 7000,
) -> list[float]:
    """Return the embedding vector for ``text``.

    Uses Ollama's ``/api/embeddings`` endpoint. Returns a list of floats
    (1024-dim for bge-m3). Raises ``EmbeddingError`` if the host is
    unreachable or the response is malformed -- callers should catch and
    fall back to plain text matching when embeddings are unavailable.

    CHUNKING over truncation: bge-m3 accepts ~8192 tokens per call.
    Texts exceeding ``chunk_chars`` (default 7000 English chars, a
    conservative cap that stays under the token limit for code-dense
    payloads) are split into chunks along sentence / whitespace
    boundaries and each chunk is embedded independently. The final
    vector is the mean of the chunk vectors -- mean pooling is the
    standard aggregation for bge-m3 chunks and preserves the full
    input's signal. No truncation, no data loss (per the project's
    NEVER-TRUNCATE rule from CLAUDE.md).
    """
    if not text or not text.strip():
        return [0.0] * EMBEDDINGS_DIM

    # Single-chunk happy path.
    if len(text) <= chunk_chars:
        return await _embed_single(text, model=model, timeout_s=timeout_s)

    # Multi-chunk path: split, embed each, mean-pool.
    from functions.chunker import chunk_text
    chunks = chunk_text(text, max_chars=chunk_chars)
    logger.info(
        "embed: text %d chars > chunk_chars %d -> %d chunks, mean-pooling "
        "the resulting vectors (no truncation)",
        len(text), chunk_chars, len(chunks),
    )
    vectors: list[list[float]] = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        v = await _embed_single(chunk, model=model, timeout_s=timeout_s)
        vectors.append(v)
    if not vectors:
        return [0.0] * EMBEDDINGS_DIM
    # Mean pool: element-wise average of all chunk vectors.
    dim = len(vectors[0])
    out = [0.0] * dim
    for v in vectors:
        for i in range(dim):
            out[i] += v[i]
    n = float(len(vectors))
    return [x / n for x in out]


async def _embed_single(
    text: str,
    *,
    model: str | None,
    timeout_s: float,
    max_attempts: int = 3,
    backoff_s: float = 0.75,
) -> list[float]:
    """One embedding per call with retry + full diagnostic on failure.

    Ollama serializes embedding requests internally; under concurrent
    fan-out (embed_batch runs N parallel workers) individual calls
    occasionally time out or return a transient error. Retrying after
    a short backoff clears that almost every time. Three attempts is
    the sweet spot -- more doesn't help, fewer lets flakiness through.

    Exception reporting: ``httpx.ReadTimeout`` and some transport errors
    carry an empty ``str(exc)`` (no message passed to __init__). Printing
    just ``{exc}`` yields "Embedding call failed: " with a blank suffix,
    which is what buried the real problem on the 23% zero-vector run.
    The error string now always includes the type name so a reader sees
    ``ReadTimeout`` / ``ConnectError`` / ``RemoteProtocolError`` even
    when the message is empty.
    """
    # Build payload + headers per configured API format.
    #
    # Two formats supported (controlled by EMBEDDINGS_FORMAT):
    #   - "ollama" (default): {"model","prompt"} -> body["embedding"]
    #   - "openai":           {"model","input"}  -> body["data"][0]["embedding"]
    # The "openai" path also adds "Authorization: Bearer <key>" so it works
    # against Gemini's OpenAI-compatible endpoint, OpenAI itself, and any
    # other OpenAI-compatible embedding host without further code changes.
    if EMBEDDINGS_FORMAT == "openai":
        payload: dict = {
            "model": model or EMBEDDINGS_MODEL,
            "input": text,
        }
        headers: dict = {"Content-Type": "application/json"}
        if EMBEDDINGS_API_KEY:
            headers["Authorization"] = f"Bearer {EMBEDDINGS_API_KEY}"
    else:  # ollama (default)
        payload = {
            "model": model or EMBEDDINGS_MODEL,
            "prompt": text,
        }
        headers = {}

    lock = _get_lock()
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            # Global serialization: one HTTP call to the embedding endpoint
            # in flight at a time. Ollama is single-threaded server-side;
            # cloud providers are concurrent but rate-limited, so the lock
            # also keeps us under per-minute caps without separate logic.
            async with lock:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    resp = await client.post(EMBEDDINGS_API_URL, json=payload, headers=headers)
                    resp.raise_for_status()
                    body = resp.json()
            # Parse the response per the configured format.
            if EMBEDDINGS_FORMAT == "openai":
                # OpenAI shape: {"data": [{"embedding": [...], "index": 0}], ...}
                data_arr = body.get("data") or []
                if not data_arr or not isinstance(data_arr, list):
                    raise EmbeddingError(
                        f"Embedding response missing 'data[0].embedding': {body!r}"
                    )
                first = data_arr[0]
                vector = first.get("embedding") if isinstance(first, dict) else None
            else:
                # Ollama shape: {"embedding": [...]}
                vector = body.get("embedding") or body.get("vector")
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError(
                    f"Embedding response missing vector field: {body!r}"
                )
            return [float(v) for v in vector]
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt < max_attempts:
                delay = backoff_s * (2 ** (attempt - 1))
                logger.warning(
                    "embed: attempt %d/%d failed (%s: %s); retrying in %.2fs",
                    attempt, max_attempts, type(exc).__name__,
                    str(exc) or "(no message)", delay,
                )
                await asyncio.sleep(delay)
                continue
            break
        except EmbeddingError:
            # Malformed response -- don't retry, that's a server bug.
            raise
        except Exception as exc:
            last_exc = exc
            break

    exc_type = type(last_exc).__name__ if last_exc else "UnknownError"
    exc_msg = str(last_exc) if last_exc else ""
    raise EmbeddingError(
        f"Embedding call failed after {max_attempts} attempt(s): "
        f"{exc_type}: {exc_msg or '(no message)'}"
    ) from last_exc


async def embed_batch(
    texts: Sequence[str],
    *,
    model: str | None = None,
    timeout_s: float = 30.0,
) -> list[list[float]]:
    """Embed many texts sequentially, preserving input order.

    Every call goes through the module-level ``_embed_lock``, so calls
    from inside embed_batch AND from any other caller in the process
    are serialized: one request at a time, next starts only after the
    previous returns. Ollama's ``/api/embeddings`` is single-threaded
    per model on the server side anyway -- parallelizing on the client
    gained nothing and caused 50%+ timeout-driven zero-vector loss on
    a 148-pattern run. Sequential + retry is faster overall because
    no time is burned on ReadTimeout retries.

    Each item still gets the full 3-attempt retry cascade inside
    ``_embed_single``. If all retries fail (server genuinely down or
    responding with malformed data), that slot logs a bypass_log
    entry and falls back to a zero vector so the caller still gets a
    same-length list. The bypass_log tells the operator exactly which
    pattern lost signal and why.
    """
    if not texts:
        return []

    def _log_zero_vector(idx: int, txt: str, exc: Exception) -> None:
        try:
            from functions.bypass_log import (
                CATEGORY_SKIPPED_DATA, SEVERITY_HIGH, log_bypass,
            )
            log_bypass(
                category=CATEGORY_SKIPPED_DATA,
                severity=SEVERITY_HIGH,
                source="functions/embeddings.py:embed_batch",
                event="embed_zero_vector",
                details={
                    "item_index": idx,
                    "text_chars": len(txt or ""),
                    "embedding_error": str(exc),
                },
                outcome="returned zero vector; downstream cosine is 0.0 for this item",
                data_lost=True,
            )
        except Exception:
            # bypass_log failing must not mask the original embed failure.
            pass

    results: list[list[float]] = []
    total = len(texts)
    fail_count = 0
    for idx, txt in enumerate(texts):
        try:
            vector = await embed(txt, model=model, timeout_s=timeout_s)
            results.append(vector)
        except EmbeddingError as exc:
            fail_count += 1
            logger.warning(
                "embed_batch [%d/%d]: falling back to zero vector: %s",
                idx + 1, total, exc,
            )
            _log_zero_vector(idx, txt, exc)
            results.append([0.0] * EMBEDDINGS_DIM)

    if fail_count:
        logger.warning(
            "embed_batch: %d of %d items fell back to zero vector "
            "after retries exhausted",
            fail_count, total,
        )
    else:
        logger.info("embed_batch: %d/%d items embedded successfully", total, total)
    return results


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1]. Zero vectors return 0.0."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def cluster_by_similarity(
    items: Sequence[tuple[object, list[float]]],
    threshold: float = 0.85,
) -> list[list[object]]:
    """Greedy single-link clustering by cosine similarity.

    ``items`` is a sequence of ``(item, vector)`` pairs. Two items end up
    in the same cluster when their cosine similarity is >= ``threshold``.
    Use this for finding dedup (semantically-identical issues reported
    across multiple pages should become one entry with many affected URLs).

    Deterministic: input order determines cluster assignment when two
    items are equidistant from different existing clusters.
    """
    clusters: list[list[object]] = []
    centroids: list[list[float]] = []
    for item, vec in items:
        # Empty OR all-zero vector = failed/missing embedding (the embed
        # fallback returns a zero vector when the embeddings host is
        # unreachable). Force a singleton with an empty centroid so it is
        # skipped in future comparisons, rather than letting cosine==0.0
        # silently strand it as an un-mergeable cluster.
        if not vec or not any(vec):
            clusters.append([item])
            centroids.append([])
            continue
        best_idx = -1
        best_sim = threshold
        for idx, centroid in enumerate(centroids):
            if not centroid:
                continue
            sim = cosine_similarity(vec, centroid)
            if sim >= best_sim:
                best_sim = sim
                best_idx = idx
        if best_idx == -1:
            clusters.append([item])
            centroids.append(list(vec))
        else:
            clusters[best_idx].append(item)
            # Running mean for centroid update
            existing = centroids[best_idx]
            n = len(clusters[best_idx])
            centroids[best_idx] = [
                ((n - 1) * existing[i] + vec[i]) / n
                for i in range(len(existing))
            ]
    return clusters


async def health_check(timeout_s: float = 180.0) -> dict[str, object]:
    """Ping the embedding host and return a status summary."""
    try:
        vector = await embed("health probe", timeout_s=timeout_s)
        return {
            "status": "ok",
            "endpoint": EMBEDDINGS_API_URL,
            "model": EMBEDDINGS_MODEL,
            "dim": len(vector),
        }
    except Exception as exc:
        return {
            "status": "error",
            "endpoint": EMBEDDINGS_API_URL,
            "model": EMBEDDINGS_MODEL,
            "error": str(exc),
        }
