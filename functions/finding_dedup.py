"""LLM-based finding deduplication.

Extracted from analysis/judge.py so the dedup pipeline can be reused
outside the per-criterion judge call. The function clusters findings
by root issue using the project's standard LLMClient.call_with_tools
cascade and merges each cluster mechanically (worst severity wins,
sources unioned, most-precise selector retained).
"""
from __future__ import annotations

import logging

from config import AI_JUDGE_API_KEY, AI_JUDGE_API_URL, AI_JUDGE_MODEL

logger = logging.getLogger(__name__)


async def _llm_dedupe_findings(
    findings: list[dict], criterion_id: str,
) -> list[dict]:
    """Use a focused LLM call to cluster same-root-issue findings.

    Replaces the prior fuzzy (selector + 80-char issue prefix) match
    that missed obvious duplicates whenever sources phrased the same
    issue differently or pointed at the same element via different
    selector paths (observed on a university SC 1.1.1: 3 sources flagged the
    'grad-ranking-poster.jpg' filename-as-alt issue with 3 different
    selectors and 3 different wordings, none of which collapsed).

    The LLM is asked ONLY for the grouping decision -- the orchestrator
    then merges each cluster mechanically (worst severity wins,
    sources unioned, most-precise selector + clearest issue retained).
    Failure is non-fatal: on any error, return findings unchanged.

    Uses the project's standard LLMClient.call_with_tools cascade and
    routes to AI_JUDGE_MODEL (the model that already arbitrates
    findings, so its judgment about which findings overlap is at
    minimum no worse than its core verdict reasoning).
    """
    if len(findings) < 2:
        return findings

    from functions.llm import LLMClient
    from functions.tools import FINDING_DEDUP_TOOL

    lines = []
    for i, f in enumerate(findings):
        sev = (f.get("severity") or "")
        sel = (f.get("css_selector") or "")
        elem = (f.get("element") or "")
        issue = (f.get("issue") or "")
        src = (f.get("source") or "")
        lines.append(
            f"[{i}] severity={sev} source={src}\n"
            f"    selector: {sel}\n"
            f"    element: {elem}\n"
            f"    issue: {issue}"
        )

    system_prompt = (
        "<role>\n"
        "You are a finding-deduplication helper for a Section 508 ACR.\n"
        "</role>\n\n"
        "<task>\n"
        "Cluster the supplied findings by ROOT ISSUE. The orchestrator "
        "then merges each cluster mechanically (worst severity wins, "
        "sources unioned, clearest selector kept) — you don't decide "
        "severity, wording, or selector. Your only output is the "
        "grouping itself.\n\n"
        "Cover every input index exactly once across all clusters. "
        "Singleton clusters are expected for findings that don't "
        "overlap any other.\n"
        "</task>\n\n"
        "<rules>\n"
        "  - Two findings cluster ONLY if they describe the SAME "
        "problem on the SAME element.\n"
        "  - css_selectors may differ in path representation but still "
        "target the same DOM node — that is a merge "
        "(e.g. '#hero > img', 'img.hero', and '#main article > img' "
        "all reference the same node).\n"
        "  - Different problems on the same element (e.g. missing alt "
        "AND insufficient contrast) are SEPARATE clusters.\n"
        "  - Be CONSERVATIVE: when in doubt, keep findings separate. "
        "False merges hide real issues; false splits only cause minor "
        "report bloat.\n"
        "</rules>\n\n"
        "<output_format>\n"
        "Call report_finding_clusters exactly once. No prose, no "
        "markdown.\n"
        "</output_format>"
    )

    example_block = (
        "<example>\n"
        "<scenario>Three sources flag the same hero image's missing "
        "alt — same root issue, different selector paths and wordings."
        "</scenario>\n"
        "<input_findings>\n"
        "[0] severity=high source=programmatic\n"
        "    selector: img#hero\n"
        "    element: img#hero\n"
        "    issue: Image missing alt attribute\n\n"
        "[1] severity=high source=ai\n"
        "    selector: #hero img\n"
        "    element: hero banner\n"
        "    issue: Hero banner has no text alternative\n\n"
        "[2] severity=high source=code_ai\n"
        "    selector: img#hero\n"
        "    element: <img>\n"
        "    issue: <img src='/banner.jpg'> lacks alt text\n\n"
        "[3] severity=medium source=programmatic\n"
        "    selector: img#hero\n"
        "    element: img#hero\n"
        "    issue: Image filename used as alt text 'banner.jpg'\n"
        "</input_findings>\n"
        "<output_clusters>\n"
        "{ \"clusters\": [\n"
        "    { \"finding_indices\": [0, 1, 2] },   # all three: missing alt\n"
        "    { \"finding_indices\": [3] }           # different issue: alt content quality\n"
        "] }\n"
        "</output_clusters>\n"
        "</example>\n\n"
        "<example>\n"
        "<scenario>Same DOM element written two completely different ways "
        "— positional path vs class-attribute path. The findings are "
        "identical Label-in-Name failures on the SAME <a>; they MUST "
        "merge despite the radically different selector strings. This "
        "is a common failure mode: the orchestrator's two source pipes "
        "(programmatic-style and AI-rewrite) often emit the same node "
        "via different selector vocabularies.\n"
        "</scenario>\n"
        "<input_findings>\n"
        "[0] severity=high source=programmatic\n"
        "    selector: div:nth-of-type(6) > div:nth-of-type(2) > div:nth-of-type(2) > a\n"
        "    element: a\n"
        "    issue: The accessible name (aria-label) 'University named a top producer of prestigious Gilman awards' does not contain the visible text label 'Read more', violating WCAG 2.5.3.\n\n"
        "[1] severity=high source=programmatic\n"
        "    selector: div.bg > div.container > div.row > div.col-12 > div.layout__region > div.spacing-top-72 > div.container > div.col-12 > a.btn-default\n"
        "    element: a.btn-default\n"
        "    issue: The accessible name 'University named a top producer of prestigious Gilman awards' does not contain the visible text label 'Read more', violating WCAG 2.5.3.\n"
        "</input_findings>\n"
        "<output_clusters>\n"
        "{ \"clusters\": [\n"
        "    { \"finding_indices\": [0, 1] }   # SAME <a>, different selector path encodings\n"
        "] }\n"
        "</output_clusters>\n"
        "<reasoning>\n"
        "Both findings cite the identical aria-label string ('University named "
        "a top producer of prestigious Gilman awards') and the identical "
        "visible text ('Read more') and the identical violation (Label "
        "in Name, SC 2.5.3). The selectors LOOK different but encode the "
        "same DOM node — one via positional nth-of-type chain, the "
        "other via class names. When two findings share the same root "
        "issue, the same accessible-name string, AND the same visible "
        "text, treat them as the same element regardless of how the "
        "selectors are written.\n"
        "</reasoning>\n"
        "</example>"
    )

    user_prompt = (
        f"<criterion_under_test>SC {criterion_id}</criterion_under_test>\n\n"
        f"{example_block}\n\n"
        f"<findings_to_cluster count=\"{len(findings)}\">\n"
        + "\n\n".join(lines)
        + "\n</findings_to_cluster>\n\n"
        f"Call report_finding_clusters."
    )

    try:
        client = LLMClient(
            base_url=AI_JUDGE_API_URL,
            model=AI_JUDGE_MODEL,
            api_key=AI_JUDGE_API_KEY,
        )
        result = await client.call_with_tools(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tool_name="report_finding_clusters",
            tool_schema=FINDING_DEDUP_TOOL,
            temperature=0.1,
        )
    except Exception as exc:
        logger.warning(
            "LLM dedup failed for SC %s (%s); keeping all %d findings",
            criterion_id, exc, len(findings),
        )
        return findings

    if not result:
        return findings

    clusters_raw = result.get("clusters") or []

    # Validate cluster indices: every index must be in range and unique
    seen: set[int] = set()
    valid_clusters: list[list[int]] = []
    for c in clusters_raw:
        if not isinstance(c, dict):
            continue
        idxs = c.get("finding_indices") or []
        valid = []
        for i in idxs:
            try:
                ii = int(i)
            except (TypeError, ValueError):
                continue
            if 0 <= ii < len(findings) and ii not in seen:
                valid.append(ii)
                seen.add(ii)
        if valid:
            valid_clusters.append(valid)

    # Findings the model didn't assign land in singleton clusters so
    # nothing is silently dropped.
    for i in range(len(findings)):
        if i not in seen:
            valid_clusters.append([i])

    if not valid_clusters:
        return findings

    # Mechanical merge of each cluster
    severity_rank = {"high": 3, "medium": 2, "low": 1, "info": 0}
    merged: list[dict] = []
    for cluster in valid_clusters:
        if len(cluster) == 1:
            merged.append(findings[cluster[0]])
            continue
        items = [findings[i] for i in cluster]
        # Pick representative entry by worst severity, longest issue text
        items_sorted = sorted(
            items,
            key=lambda x: (
                -severity_rank.get(str(x.get("severity", "medium")).lower(), 2),
                -len(str(x.get("issue", ""))),
            ),
        )
        worst = items_sorted[0]
        # Union sources
        sources: list[str] = []
        for it in items:
            for s in str(it.get("source", "")).split(","):
                s = s.strip()
                if s and s not in sources:
                    sources.append(s)
        # Pick most precise (longest) selector
        sels = sorted(
            (it.get("css_selector", "") for it in items),
            key=lambda s: -len(s or ""),
        )
        best_sel = sels[0] if sels else ""
        merged_entry = dict(worst)
        merged_entry["css_selector"] = best_sel
        merged_entry["source"] = (
            ", ".join(sorted(sources)) if len(sources) > 1
            else (sources[0] if sources else worst.get("source", "judge"))
        )
        merged.append(merged_entry)

    if len(merged) < len(findings):
        logger.info(
            "LLM dedup SC %s: %d findings -> %d clusters (%d merged)",
            criterion_id, len(findings), len(merged),
            len(findings) - len(merged),
        )
    return merged
