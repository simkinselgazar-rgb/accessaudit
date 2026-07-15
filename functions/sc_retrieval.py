"""Layer 3 of the cached-code-AI architecture: semantic retrieval for the judge.

Layer 1 (``functions.code_analyzer``) emits SC-tagged code patterns,
each carrying a ``raw_evidence`` snippet. Layer 1's consumer
(``checks.base.run_code_analysis``) filters the cache to patterns
explicitly tagged for its SC. That filter is great for precision --
every pattern the judge eventually sees was deliberately labeled by
Phase 1 as evidence for this SC -- but it can miss a pattern Phase 1
mis-tagged or forgot to tag.

Layer 3 closes that gap. For each SC the judge is evaluating, it
retrieves the top-K most semantically similar cached patterns using
cosine similarity over bge-m3 embeddings. The retrieved snippets go
into the judge's user prompt as a ``CODE EVIDENCE - retrieved for this
SC`` block. The judge still relies on the accepted findings from
Code AI / Visual AI / AT-sim for its final verdict -- the retrieval
output is GROUNDING evidence, not a new finding source.

Why embeddings and not another LLM call:

1. Layer 1's Phase 1 already IS the "read all code and tag by SC" LLM
   call. Adding a second LLM per-SC tag picker re-introduces the cost
   Layer 1 just eliminated.
2. bge-m3 runs locally at ``EMBEDDINGS_API_URL`` and costs no tokens.
3. The SC's own ``plain_meaning`` + ``fail_conditions`` text from the
   criterion JSON is a high-quality query; no keyword extraction step
   is needed.

Pattern embeddings are built ONCE per review and persisted alongside
the cache. Every SC judge reuses them, so the full-run cost is one
``embed_batch`` call per review plus K=8 cosine ops per SC.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from functions.embeddings import (
    EmbeddingError,
    cosine_similarity,
    embed,
    embed_batch,
)

logger = logging.getLogger(__name__)


# Default policy: send every pattern to the judge, ranked by cosine
# similarity, with the judge's own prompt-chunker deciding how to
# spread them across batches. No hardcoded top-K cap, no minimum
# similarity floor. Callers that want a narrower slice pass
# ``top_k`` or ``min_similarity`` explicitly; both are optional.


async def build_pattern_embeddings(
    code_findings: list[dict[str, Any]],
    *,
    review_dir: str = "",
    force: bool = False,
) -> list[list[float]]:
    """Embed each cached pattern ONCE for the whole review.

    Returns a list aligned with ``code_findings``: index ``i`` is the
    bge-m3 vector for ``code_findings[i]``. A pattern with no embedable
    text gets a zero vector (skipped by the similarity scan).

    When ``review_dir`` is given the vectors are persisted to
    ``<review_dir>/code_findings_embeddings.json`` and loaded on
    subsequent calls. ``force=True`` re-embeds even if the cache file
    exists.
    """
    if not code_findings:
        return []

    cache_path: Path | None = None
    if review_dir:
        cache_path = Path(review_dir) / "code_findings_embeddings.json"
        if cache_path.exists() and not force:
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(data, list) and len(data) == len(code_findings):
                    logger.info(
                        "sc_retrieval: loaded %d cached embeddings from %s",
                        len(data), cache_path,
                    )
                    return [list(v) if isinstance(v, list) else [] for v in data]
                logger.info(
                    "sc_retrieval: embedding cache length mismatch "
                    "(%d cached vs %d patterns) -- rebuilding",
                    len(data) if isinstance(data, list) else -1, len(code_findings),
                )
            except Exception as exc:
                logger.warning(
                    "sc_retrieval: embedding cache read failed (%s) -- rebuilding",
                    exc,
                )

    texts = [_pattern_text(p) for p in code_findings]
    from config import EMBEDDINGS_MODEL
    logger.info(
        "sc_retrieval: embedding %d patterns via %s (one-shot per review)",
        len(texts), EMBEDDINGS_MODEL,
    )
    vectors = await embed_batch(texts)

    if cache_path:
        try:
            cache_path.write_text(
                json.dumps(vectors, separators=(",", ":")),
                encoding="utf-8",
            )
            logger.info(
                "sc_retrieval: embeddings cached to %s", cache_path,
            )
        except Exception as exc:
            logger.warning("sc_retrieval: embedding cache write failed: %s", exc)

    return vectors


async def retrieve_for_sc(
    criterion_id: str,
    criterion_name: str,
    criterion_guidance: str,
    code_findings: list[dict[str, Any]],
    pattern_embeddings: list[list[float]],
    *,
    top_k: int | None = None,
    min_similarity: float | None = None,
) -> list[dict[str, Any]]:
    """Return cached patterns ranked by semantic similarity to this SC.

    Default policy (no arguments): return EVERY pattern in
    ``code_findings`` (that has an embedding) ordered by cosine
    similarity to the SC query, highest first. The judge's own prompt
    chunker then decides how to spread them across batches, so
    nothing is dropped up-front.

    Optional narrowing:

    * ``top_k=N`` trims the returned list to the top N after ranking.
      Pass ``None`` (default) for no cap.
    * ``min_similarity=X`` drops entries with cosine below X. Pass
      ``None`` (default) for no floor.

    Each returned entry is the original pattern dict with a
    ``retrieval_score`` float added. On embedding-host failure the
    function returns an empty list -- retrieval is a grounding aid,
    not a blocker. The judge still has DOM context + Code-AI findings.
    """
    from functions.bypass_log import (
        CATEGORY_SKIPPED_DATA, SEVERITY_HIGH, log_bypass,
    )

    if not code_findings or not pattern_embeddings:
        return []
    if len(pattern_embeddings) != len(code_findings):
        logger.warning(
            "sc_retrieval: mismatched lengths (%d embeddings vs %d patterns) -- skipping",
            len(pattern_embeddings), len(code_findings),
        )
        log_bypass(
            category=CATEGORY_SKIPPED_DATA,
            severity=SEVERITY_HIGH,
            source="functions/sc_retrieval.py:retrieve_for_sc",
            event="embedding_cache_length_mismatch",
            details={
                "criterion_id": criterion_id,
                "embedding_count": len(pattern_embeddings),
                "pattern_count": len(code_findings),
            },
            outcome="Layer 3 retrieval disabled for this SC; judge gets no retrieved code evidence",
            data_lost=True,
        )
        return []

    query = _sc_query_text(criterion_id, criterion_name, criterion_guidance)
    try:
        query_vec = await embed(query)
    except EmbeddingError as exc:
        logger.warning(
            "sc_retrieval: query embedding failed for %s (%s) -- skipping retrieval",
            criterion_id, exc,
        )
        log_bypass(
            category=CATEGORY_SKIPPED_DATA,
            severity=SEVERITY_HIGH,
            source="functions/sc_retrieval.py:retrieve_for_sc",
            event="query_embedding_failed",
            details={
                "criterion_id": criterion_id,
                "error": str(exc),
            },
            outcome="Layer 3 retrieval skipped for this SC; judge gets no retrieved code evidence",
            data_lost=True,
        )
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
    for pattern, vec in zip(code_findings, pattern_embeddings):
        if not vec:
            continue
        sim = cosine_similarity(query_vec, vec)
        if min_similarity is not None and sim < min_similarity:
            continue
        scored.append((sim, pattern))

    scored.sort(key=lambda x: x[0], reverse=True)
    if top_k is not None and top_k > 0:
        scored = scored[:top_k]

    logger.info(
        "sc_retrieval: SC %s -- %d/%d patterns kept (top_k=%s, min_sim=%s, top score %.2f)",
        criterion_id, len(scored), len(code_findings),
        top_k if top_k is not None else "none",
        f"{min_similarity:.2f}" if min_similarity is not None else "none",
        scored[0][0] if scored else 0.0,
    )

    out: list[dict[str, Any]] = []
    for score, pattern in scored:
        entry = dict(pattern)
        entry["retrieval_score"] = round(float(score), 4)
        out.append(entry)
    return out


def format_retrieved_patterns(
    retrieved: list[dict[str, Any]],
    criterion_id: str,
) -> str:
    """Render retrieved patterns as a judge-facing prompt block.

    Returns an empty string when there is nothing to add -- the caller
    can ``if block:`` before appending. Layout mirrors
    ``checks.base._format_patterns_for_judge`` so the judge sees the
    same field vocabulary it already handles from Code AI.
    """
    if not retrieved:
        return ""

    lines: list[str] = [
        f"CODE EVIDENCE (retrieved for SC {criterion_id} via semantic similarity)",
        "These code patterns were NOT necessarily tagged for this SC by the",
        "Phase 1 inventory pass, but are semantically similar to the SC's",
        "description. Treat them as supplementary grounding: use their",
        "raw_evidence to verify findings, but DO NOT create new findings",
        "from them unless Code AI / Visual AI already flagged the same element.",
        "",
    ]
    for i, p in enumerate(retrieved, 1):
        lines.append(
            f"[{i}] score={p.get('retrieval_score', 0):.2f} "
            f"pattern_type={p.get('pattern_type', '')!s}"
        )
        if p.get("element"):
            lines.append(f"    element: {p['element']}")
        if p.get("css_selector"):
            lines.append(f"    selector: {p['css_selector']}")
        if p.get("issue"):
            lines.append(f"    issue: {p['issue']}")
        sc_ids = p.get("sc_ids", []) or []
        if sc_ids:
            lines.append(f"    phase1_sc_tags: {sc_ids}")
        src = p.get("source_chunk", "")
        if src:
            lines.append(f"    source: {src}")
        evidence = (p.get("raw_evidence", "") or "").rstrip()
        if evidence:
            lines.append("    raw_evidence:")
            for ev_line in evidence.split("\n"):
                lines.append(f"      | {ev_line}")
        lines.append("")
    return "\n".join(lines)


# ─── Internal helpers ────────────────────────────────────────────────────────

def _pattern_text(pattern: dict[str, Any]) -> str:
    """Build the text bge-m3 embeds for one cached pattern.

    Includes the pattern's semantic fields AND the raw code evidence.
    bge-m3 handles mixed natural-language + code tokens well; including
    code snippets gives retrieval a strong anchor when Phase 1's prose
    description is thin or generic.
    """
    parts = [
        f"pattern_type: {pattern.get('pattern_type', '')}",
        f"element: {pattern.get('element', '')}",
        f"issue: {pattern.get('issue', '')}",
    ]
    sc_ids = pattern.get("sc_ids", []) or []
    if sc_ids:
        parts.append(f"sc_ids: {', '.join(sc_ids)}")
    raw_evidence = pattern.get("raw_evidence", "") or ""
    if raw_evidence:
        parts.append(f"code:\n{raw_evidence}")
    return "\n".join(parts)


def _sc_query_text(
    criterion_id: str,
    criterion_name: str,
    criterion_guidance: str,
) -> str:
    """Build the SC-side query string that bge-m3 embeds.

    The judge already holds the full ``criterion_guidance`` text. We
    pass it through verbatim so the query captures plain_meaning +
    pass/fail conditions + anti-patterns. The full guidance tends to
    be under 3K chars which is well inside bge-m3's context window.
    """
    return (
        f"WCAG {criterion_id} {criterion_name}\n"
        f"{criterion_guidance or ''}"
    ).strip()
