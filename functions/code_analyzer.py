"""Per-page code analysis cache (Layer 1 of the cached-code-AI architecture).

Reads the page's HTML and readable JavaScript ONCE, produces a unified
list of accessibility-relevant code patterns, and tags each pattern with
the WCAG SC IDs it may be evidence of. The cached result is consumed by
every SC's ``run_code_analysis`` in ``checks/base.py`` instead of each
SC re-reading all 100+ code chunks from scratch.

Routing:
- Phase 1 calls are pure text (no screenshots) so they go to the text
  model at ``AI_FALLBACK_URL`` (Qwen 3.5 35B by default). Qwen is
  stronger at code reasoning than the local Gemma vision models and
  avoids the output-truncation pattern Gemma hits on 25K JS chunks.
- Each chunk is saved to the LLM transcript directory via the standard
  ``LLMClient.call_with_tools`` cascade, so every call is auditable.

Cache:
- When ``review_dir`` is provided, the final findings list is persisted
  to ``<review>/code_findings.json``. Subsequent calls within the same
  review read from that file instead of re-running the LLM pass.

Chunking:
- HTML goes through ``chunk_html_by_landmarks`` (lossless, landmark-aware).
- JavaScript goes through ``chunk_text`` at sentence boundaries with a
  hard-slice fallback (lossless). Every chunk is analyzed; nothing is
  dropped.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Default size of each text chunk sent to the code model. Matches the
# Gemini context window budget (1M). We use 300K characters as a safe 
# baseline that fits comfortably within 1M tokens including system
# overhead and output budget.
DEFAULT_CHUNK_MAX_CHARS = 300_000


# ── System prompt for the unified code-pattern pass ─────────────────────────

_SYSTEM_PROMPT = (
    "ROLE\n"
    "You are a WCAG code auditor doing a SINGLE read of a page's source "
    "code. Your task is to enumerate every accessibility-relevant pattern "
    "in this code chunk. You are NOT judging a specific criterion -- you "
    "are building a neutral inventory that per-SC judges will consume.\n\n"

    "TAGGING RULE (sc_ids)\n"
    "Every pattern you report must carry a non-empty sc_ids list naming "
    "every WCAG 2.2 success criterion the pattern is evidence for. A "
    "single pattern can map to multiple SCs -- tag all of them. Prefer "
    "OVER-tagging to under-tagging: the per-SC judge filters false "
    "positives but cannot recover missed evidence.\n"
    "Examples of good tagging:\n"
    "  - <img> with no alt attribute -> sc_ids: ['1.1.1']\n"
    "  - <input> with no associated <label> -> sc_ids: ['1.3.1','3.3.2','4.1.2']\n"
    "  - <a> without href + onclick handler -> sc_ids: ['2.1.1','4.1.2']\n"
    "  - <video> with no <track> -> sc_ids: ['1.2.1','1.2.2','1.2.3','1.2.5']\n"
    "  - outline:none on :focus -> sc_ids: ['2.4.7']\n"
    "  - <button> with no accessible name -> sc_ids: ['1.1.1','4.1.2']\n"
    "  - keydown handler checking single-character code -> sc_ids: ['2.1.4']\n"
    "  - CSS animation without prefers-reduced-motion gate -> sc_ids: ['2.3.3']\n"
    "  - onclick on <div>/<span> with no tabindex -> sc_ids: ['2.1.1','4.1.2']\n"
    "  - aria-labelledby pointing to nonexistent id -> sc_ids: ['1.3.1','4.1.2']\n"
    "  - <iframe> with no title -> sc_ids: ['2.4.1','4.1.2']\n"
    "  - form field with aria-required but no visible required indicator -> sc_ids: ['1.4.1','3.3.2']\n"

    "RAW EVIDENCE\n"
    "Every pattern must include raw_evidence: the EXACT code snippet "
    "(3-20 lines) from the chunk that triggered the finding. The per-SC "
    "judge uses raw_evidence to verify the pattern and reject false "
    "positives. Do NOT paraphrase -- quote the code verbatim.\n\n"

    "SELECTOR\n"
    "When the pattern points at a specific DOM element whose selector is "
    "derivable from the code (ID, class, or unique tag path), populate "
    "css_selector. When the pattern is JS behavior with no single DOM "
    "element, leave css_selector empty.\n\n"

    "EMPTY CHUNKS\n"
    "If the chunk contains no accessibility-relevant patterns (pure "
    "analytics, webpack boilerplate, minified library code, CSS resets), "
    "return patterns: []. Empty is a valid answer.\n\n"

    "RESPONSE\n"
    "Call report_code_patterns exactly once. No prose, no markdown."
)


async def analyze_page_code(
    html: str,
    script_content: str,
    *,
    review_dir: str = "",
    force: bool = False,
    chunk_max_chars: int = DEFAULT_CHUNK_MAX_CHARS,
) -> list[dict[str, Any]]:
    """Run the Phase 1 code-pattern pass over a page's full source.

    Args:
        html: The rendered page HTML (from ``CaptureData.html``).
        script_content: Concatenated readable JavaScript from inline
            ``<script>`` blocks + extracted event handlers. Usually the
            output of ``_extract_readable_scripts`` in ``checks/base.py``.
        review_dir: Optional path to the review directory. When provided,
            results are cached to ``<review_dir>/code_findings.json`` so
            subsequent runs within the review skip the LLM pass.
        force: When True, ignore any cached file and re-run the pass.
        chunk_max_chars: Max chars per chunk sent to the code model.
            Chunking is lossless -- every byte ends up in exactly one
            chunk, no truncation. Defaults to
            ``DEFAULT_CHUNK_MAX_CHARS`` (25K, tuned for Qwen 35B).

    Returns:
        A list of pattern dicts. Each pattern carries sc_ids (list[str]),
        element (str), css_selector (str), issue (str), raw_evidence (str),
        severity (str), and source_chunk (str) for traceability.
    """
    # ── Cache hit ────────────────────────────────────────────────────
    cache_path: Path | None = None
    if review_dir:
        cache_path = Path(review_dir) / "code_findings.json"
        if cache_path.exists() and not force:
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    logger.info(
                        "code_analyzer: loaded %d cached patterns from %s",
                        len(data), cache_path,
                    )
                    return data
            except Exception as exc:
                logger.warning(
                    "code_analyzer: cache read failed (%s) -- regenerating",
                    exc,
                )

    # ── Layer 2: AST / regex prefilter (JS only) ────────────────────
    # HTML is already small and structurally dense; filtering it hurts
    # more than it helps (stripping "irrelevant" divs risks dropping the
    # element the judge needs to look at). JS, on the other hand, is
    # dominated by framework and analytics noise; stripping it before
    # chunking compounds the downstream savings.
    filtered_script_content = script_content
    if script_content:
        from functions.js_ast_filter import filter_accessibility_code
        filtered_script_content, filter_stats = filter_accessibility_code(script_content)
        logger.info(
            "code_analyzer: Layer 2 JS prefilter -- %d -> %d chars (%.1f%% kept, path=%s)",
            filter_stats["input_chars"], filter_stats["output_chars"],
            100 * filter_stats["ratio"], filter_stats["path"],
        )

    # ── Chunk ───────────────────────────────────────────────────────
    from functions.chunker import chunk_html_by_landmarks, chunk_text

    html_chunks: list[tuple[str, str]] = []
    if html:
        html_chunks = chunk_html_by_landmarks(html, max_chars=chunk_max_chars)
    js_chunks: list[str] = []
    if filtered_script_content:
        js_chunks = chunk_text(filtered_script_content, max_chars=chunk_max_chars)

    total_chunks = len(html_chunks) + len(js_chunks)
    if total_chunks == 0:
        logger.info("code_analyzer: no code to analyze")
        if cache_path:
            try:
                cache_path.write_text("[]", encoding="utf-8")
            except Exception:
                logger.debug("code_analyzer: failed to write empty cache file %s", cache_path, exc_info=True)
        return []

    logger.info(
        "code_analyzer: Phase 1 pass starting -- %d HTML chunks + %d JS chunks = %d total",
        len(html_chunks), len(js_chunks), total_chunks,
    )

    # ── Pick a model for Phase 1 code-pattern calls ──────────────────
    # Route to the project's PRIMARY text model (Gemma 26B at 11805 by
    # default) rather than the fallback text model (Qwen 3.5 35B at
    # 11801). Observed on a university run 2026-04-22: Qwen 3.5 returns
    # prose or double-encoded ``patterns`` strings on the
    # ``report_code_patterns`` schema, exhausting the cascade. Gemma
    # 26B emits clean tool calls for every other tool in the system
    # (report_wcag_assessment / report_judgment / ...), so it's the
    # right target for this one too.
    #
    # Output-truncation note: 25K code prompts occasionally push Gemma
    # into extended-thinking mode. Layer 2 already halves JS bytes
    # before chunking, and the cascade's retry+restructure path
    # catches truncations when they happen. Net win: far fewer
    # cascade exhausts.
    from config import AI_API_BASE_URL, AI_MODEL
    from functions.llm import LLMClient
    from functions.tools import CODE_PATTERN_INVENTORY_TOOL

    client_kwargs: dict[str, Any] = {}
    if AI_API_BASE_URL:
        client_kwargs["base_url"] = AI_API_BASE_URL
    if AI_MODEL:
        client_kwargs["model"] = AI_MODEL
    client = LLMClient(**client_kwargs)

    # Run all chunks concurrently. Concurrency cap is enforced by
    # LLMClient's process-wide AI_MAX_CONCURRENT semaphore, so we can
    # gather() the full chunk list without overwhelming the backend.
    # Sequential await-in-a-for-loop here was the dominant wall-clock
    # cost on real sites: 142 chunks * ~25s each = ~60 min for the
    # analyzer pass alone. With concurrency=10 this drops to ~6-8 min.
    html_tasks = [
        _run_chunk(
            client,
            chunk,
            source_label=f"html:{label}",
            index=idx + 1,
            total=len(html_chunks),
            kind="HTML",
            tool_schema=CODE_PATTERN_INVENTORY_TOOL,
        )
        for idx, (label, chunk) in enumerate(html_chunks)
    ]
    js_tasks = [
        _run_chunk(
            client,
            chunk,
            source_label=f"js:chunk_{idx + 1}_of_{len(js_chunks)}",
            index=idx + 1,
            total=len(js_chunks),
            kind="JavaScript",
            tool_schema=CODE_PATTERN_INVENTORY_TOOL,
        )
        for idx, chunk in enumerate(js_chunks)
    ]
    chunk_results = await asyncio.gather(*html_tasks, *js_tasks)

    all_patterns: list[dict[str, Any]] = []
    for patterns in chunk_results:
        all_patterns.extend(patterns)

    logger.info(
        "code_analyzer: Phase 1 complete -- %d patterns across %d chunks",
        len(all_patterns), total_chunks,
    )

    # ── Persist cache ───────────────────────────────────────────────
    if cache_path:
        try:
            cache_path.write_text(
                json.dumps(all_patterns, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("code_analyzer: cache written to %s", cache_path)
        except Exception as exc:
            logger.warning("code_analyzer: cache write failed: %s", exc)

    # ── Build pattern embeddings (Layer 3 prep) ─────────────────────
    # Embed every cached pattern ONCE here so the per-SC judge-time
    # retrieval only does cosine ops. Failures are tolerated: the
    # judge's retrieval helper returns an empty list when embeddings
    # are missing, so a flaky bge-m3 host never breaks the run.
    if all_patterns:
        try:
            from functions.sc_retrieval import build_pattern_embeddings
            await build_pattern_embeddings(all_patterns, review_dir=review_dir)
        except Exception as exc:
            logger.warning(
                "code_analyzer: pattern embedding build failed (%s) -- judge "
                "retrieval will degrade but run continues",
                exc,
            )
            try:
                from functions.bypass_log import (
                    CATEGORY_SKIPPED_DATA, SEVERITY_HIGH, log_bypass,
                )
                log_bypass(
                    category=CATEGORY_SKIPPED_DATA,
                    severity=SEVERITY_HIGH,
                    source="functions/code_analyzer.py:analyze_page_code",
                    event="pattern_embedding_build_failed",
                    details={
                        "pattern_count": len(all_patterns),
                        "exception_type": type(exc).__name__,
                        "exception": str(exc),
                    },
                    outcome="Layer 3 disabled for entire review; every SC's judge runs without retrieved code evidence",
                    data_lost=True,
                )
            except Exception:
                logger.debug("code_analyzer: log_bypass call failed for pattern_embedding_build_failed", exc_info=True)

    return all_patterns


# Floor for the recursive split-on-empty path in _run_chunk. Chunks
# at or below this size still get retried, but a final empty result is
# accepted (and logged). Set well below typical chunk sizes -- any
# further halving below this point is unlikely to change Gemini's
# grammar-filter decision.
_SPLIT_FLOOR_CHARS = 1000

# Delay between the first call and its single retry, mirroring the
# 5-second pause used by capture/v2/phase2_visual_explorer.py.
_RETRY_DELAY_SECONDS = 5


def _build_chunk_prompt(
    chunk: str, source_label: str, kind: str, index: int, total: int,
) -> str:
    return (
        f"SOURCE: {source_label}\n"
        f"KIND: {kind} chunk {index} of {total}\n"
        f"LENGTH: {len(chunk)} chars\n\n"
        f"CODE TO ANALYZE:\n"
        f"```\n{chunk}\n```\n\n"
        f"Enumerate every accessibility-relevant pattern you find in the "
        f"code above. Tag each pattern with every WCAG SC ID it could be "
        f"evidence for. Include the exact code snippet as raw_evidence. "
        f"Call report_code_patterns exactly once."
    )


async def _call_with_retry(
    client, user_prompt: str, tool_schema: dict, source_label: str,
) -> tuple[dict | None, Exception | None]:
    """One call + one 5s-delay retry on empty result. Returns (payload, exc).

    A `(None, None)` return means both attempts came back empty without
    raising -- typically Gemini's MALFORMED_FUNCTION_CALL grammar gate.
    The caller decides whether to split-and-recurse or accept the loss.
    """
    async def attempt() -> tuple[dict | None, Exception | None]:
        try:
            return await client.call_with_tools(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                tool_name="report_code_patterns",
                tool_schema=tool_schema,
                temperature=0.1,
            ), None
        except Exception as exc:
            return None, exc

    payload, exc = await attempt()
    if payload is None and exc is None:
        logger.info(
            "code_analyzer chunk %s: empty result on first try -- retrying after %ds",
            source_label, _RETRY_DELAY_SECONDS,
        )
        await asyncio.sleep(_RETRY_DELAY_SECONDS)
        payload, exc = await attempt()
    return payload, exc


def _log_chunk_bypass(
    *, event: str, source_label: str, kind: str, index: int, total: int,
    chunk_chars: int, outcome: str, extra: dict[str, Any] | None = None,
) -> None:
    """Record a chunk-level data-loss event. Failures here are best-effort."""
    try:
        from functions.bypass_log import (
            CATEGORY_SKIPPED_DATA, SEVERITY_HIGH, log_bypass,
        )
        details: dict[str, Any] = {
            "source_chunk": source_label,
            "chunk_kind": kind,
            "chunk_index": index,
            "chunk_total": total,
            "chunk_chars": chunk_chars,
        }
        if extra:
            details.update(extra)
        log_bypass(
            category=CATEGORY_SKIPPED_DATA,
            severity=SEVERITY_HIGH,
            source="functions/code_analyzer.py:_run_chunk",
            event=event,
            details=details,
            outcome=outcome,
            data_lost=True,
        )
    except Exception:
        logger.debug("code_analyzer: log_bypass call failed for %s", event, exc_info=True)


async def _split_and_recurse(
    client, chunk: str, *,
    source_label: str, index: int, total: int,
    kind: str, tool_schema: dict, min_split_chars: int,
) -> list[dict[str, Any]]:
    """Halve the chunk on a byte boundary and recurse on each half.

    Splitting is lossless: ``chunk[:mid] + chunk[mid:] == chunk`` byte for
    byte. Each half re-enters ``_run_chunk`` with the same retry+split
    machinery, so the recursion terminates when either (a) a sub-chunk
    fits Gemini's grammar gate and returns a payload, or (b) the chunk
    is at/below ``min_split_chars`` and the loss is recorded.
    """
    if len(chunk) <= min_split_chars:
        _log_chunk_bypass(
            event="chunk_llm_empty_at_split_floor",
            source_label=source_label, kind=kind,
            index=index, total=total, chunk_chars=len(chunk),
            extra={"min_split_chars": min_split_chars},
            outcome=(
                "LLM returned no payload on first call + 5s-delay retry "
                "AND chunk is at/below split floor; this leaf produces no "
                "patterns (full prompt+response still saved in llm_transcripts/)"
            ),
        )
        return []

    mid = len(chunk) // 2
    halves = [chunk[:mid], chunk[mid:]]
    logger.info(
        "code_analyzer chunk %s: empty after retry -- splitting %d chars "
        "into 2 halves (%d + %d) and recursing",
        source_label, len(chunk), len(halves[0]), len(halves[1]),
    )
    results: list[dict[str, Any]] = []
    for half_idx, half in enumerate(halves):
        results.extend(await _run_chunk(
            client, half,
            source_label=f"{source_label}_split{half_idx + 1}of2",
            index=index, total=total,
            kind=kind, tool_schema=tool_schema,
            min_split_chars=min_split_chars,
        ))
    return results


def _decode_patterns_field(
    raw: Any, source_label: str, kind: str,
) -> list[Any]:
    """Coerce the tool-call ``patterns`` field to a list.

    Some models (notably Qwen 35B) return ``patterns`` as a JSON-encoded
    string instead of a native array -- the tool call is structurally
    valid but the field is double-encoded. This unwraps it once. If the
    inner decode fails the chunk is dropped and a bypass event is
    recorded with the full offending string (no truncation per CLAUDE.md).
    """
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str):
        return []
    try:
        decoded = json.loads(raw)
    except Exception as exc:
        try:
            from functions.bypass_log import (
                CATEGORY_PARSE_FAIL, SEVERITY_HIGH, log_bypass,
            )
            log_bypass(
                category=CATEGORY_PARSE_FAIL,
                severity=SEVERITY_HIGH,
                source="functions/code_analyzer.py:_decode_patterns_field",
                event="patterns_string_json_decode_failed",
                details={
                    "source_chunk": source_label,
                    "chunk_kind": kind,
                    "patterns_string_chars": len(raw),
                    "patterns_content": raw,
                    "error": str(exc),
                },
                outcome="double-encoded patterns string could not be unwrapped; chunk produces no patterns",
                data_lost=True,
            )
        except Exception:
            logger.debug(
                "code_analyzer: log_bypass call failed for patterns_string_json_decode_failed",
                exc_info=True,
            )
        return []
    if not isinstance(decoded, list):
        return []
    logger.info(
        "code_analyzer chunk %s: unwrapped double-encoded patterns string "
        "(%d chars -> %d items)",
        source_label, len(raw), len(decoded),
    )
    return decoded


def _normalize_pattern(p: Any, source_label: str) -> dict[str, Any] | None:
    """Coerce a single raw pattern dict into the canonical schema.

    Returns None when the pattern has no SC tags -- such patterns are
    useless to per-SC judges downstream.
    """
    if not isinstance(p, dict):
        return None
    sc_ids = p.get("sc_ids") or []
    if isinstance(sc_ids, str):
        sc_ids = [sc_ids]
    sc_ids = [str(s).strip() for s in sc_ids if s]
    if not sc_ids:
        return None
    return {
        "pattern_type": str(p.get("pattern_type", "")),
        "sc_ids": sc_ids,
        "element": str(p.get("element", "")),
        "css_selector": str(p.get("css_selector", "")),
        "issue": str(p.get("issue", "")),
        "raw_evidence": str(p.get("raw_evidence", "")),
        "severity": str(p.get("severity", "medium")).lower(),
        "source_chunk": source_label,
    }


async def _run_chunk(
    client,
    chunk: str,
    *,
    source_label: str,
    index: int,
    total: int,
    kind: str,
    tool_schema: dict,
    min_split_chars: int = _SPLIT_FLOOR_CHARS,
) -> list[dict[str, Any]]:
    """Analyse one code chunk and return its accessibility-pattern list.

    Two-tier recovery on empty/MALFORMED responses (no information loss):
      1. First call -> if empty, sleep 5s and retry once with fresh sampling.
      2. If still empty, halve the chunk on a byte boundary and recurse on
         each half. Bottoms out at ``min_split_chars``.

    A genuine LLM exception (network, auth, schema error) is logged as a
    bypass event and the chunk is dropped -- those are not transient.
    """
    user_prompt = _build_chunk_prompt(chunk, source_label, kind, index, total)
    payload, exc = await _call_with_retry(client, user_prompt, tool_schema, source_label)

    if exc is not None:
        logger.warning(
            "code_analyzer chunk %s failed: %s -- returning empty patterns",
            source_label, exc,
        )
        _log_chunk_bypass(
            event="chunk_llm_call_exception",
            source_label=source_label, kind=kind,
            index=index, total=total, chunk_chars=len(chunk),
            extra={"exception_type": type(exc).__name__, "exception": str(exc)},
            outcome="this chunk produced no patterns; per-SC judges get no code evidence from this region",
        )
        return []

    if not payload:
        return await _split_and_recurse(
            client, chunk,
            source_label=source_label, index=index, total=total,
            kind=kind, tool_schema=tool_schema,
            min_split_chars=min_split_chars,
        )

    raw_patterns = _decode_patterns_field(payload.get("patterns") or [], source_label, kind)
    normalized = [_normalize_pattern(p, source_label) for p in raw_patterns]
    return [n for n in normalized if n is not None]


def findings_for_sc(
    findings: list[dict[str, Any]],
    criterion_id: str,
) -> list[dict[str, Any]]:
    """Filter the cached findings list down to patterns tagged for an SC."""
    return [
        f for f in findings
        if criterion_id in (f.get("sc_ids") or [])
    ]
