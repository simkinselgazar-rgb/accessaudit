"""Executive summary synthesis -- generates VPAT overview from all test results.

Every synthesis call goes through ``LLMClient.call_with_tools`` so the
universal 3-attempt retry + prose-restructure cascade handles malformed
model output.

Large reviews are chunked by WCAG principle group (1.x Perceivable, 2.x
Operable, 3.x Understandable, 4.x Robust) so no finding is ever dropped
from the synthesis prompt.
"""
from __future__ import annotations

import logging
from typing import Any

from config import AI_FALLBACK_MODEL, AI_FALLBACK_URL
from functions.llm import LLMClient, LLMError
from functions.tools import SYNTHESIS_TOOL

logger = logging.getLogger(__name__)


# Char budget per synthesis call. When the total prompt would exceed this,
# results are chunked by WCAG principle and the outputs merged. Sized at
# 400K chars (~100K tokens) so the local 128K-context stack has room for
# the system prompt, tool schema, and generated response without pushing
# the model into extended-thinking mode.
_SYNTHESIS_PROMPT_CHUNK_CHARS = 400_000


SYNTHESIS_SYSTEM_PROMPT = """\
<role>
You are a senior accessibility auditor writing the executive summary
for a VPAT 2.5 Accessibility Conformance Report (ACR). Your summary
sits at the top of the report and is read by legal, procurement, and
engineering stakeholders. Tone is professional, evidence-based, and
grounded in the actual findings — never speculative.
</role>

<task>
Call the report_synthesis tool exactly once with four outputs:

  1. executive_summary — four paragraphs following the template below.
  2. systemic_issues   — patterns repeating across criteria/elements.
  3. priority_order    — criterion IDs ordered by remediation impact.
  4. vpat_remarks      — per-criterion 1-3 sentence Remarks text.
</task>

<executive_summary_template>
Paragraph 1 — Overall conformance posture.
  - State how many criteria were evaluated and how many fall into each
    bucket (Supports, Partially Supports, Does Not Support, Not
    Applicable).
  - Name the 2-3 most significant gaps at the criterion level.

Paragraph 2 — Most impacted user groups and the specific barriers.
  - Name disability groups (blind/low-vision, motor-impaired, deaf,
    cognitive) AND specific assistive technologies (JAWS, NVDA,
    VoiceOver, keyboard-only, voice control, switch devices).
  - Describe the concrete barriers they encounter on this site/doc.

Paragraph 3 — Systemic patterns cutting across criteria.
  - Identify issue patterns repeating site-wide (e.g. "Contrast
    ratios below 4.5:1 on 12 text elements", "Missing form labels on
    all checkout inputs"). Each systemic issue maps to one entry in
    the systemic_issues array.

Paragraph 4 — Section 508 risk summary.
  - State whether the product currently meets Section 508
    requirements (yes / not fully / no) and name the high-level gaps
    driving non-conformance.
  - Describe RISK, not fixes. Do not recommend specific code changes.
</executive_summary_template>

<systemic_issues_format>
Each entry identifies a pattern repeating across multiple criteria
or many elements. Fields:
  - pattern: short label.
  - affected_criteria: list of criterion IDs.
  - severity: high | medium | low | info.
  - description: 1-2 sentences.
</systemic_issues_format>

<priority_order_format>
Criterion IDs ordered by remediation impact (highest first). Base the
order on severity + breadth of user impact — not on how easy the fix
would be.
</priority_order_format>

<vpat_remarks_format>
Dict mapping criterion_id → 1-3 sentence "Remarks and Explanations"
text. Use professional VPAT language. Describe conformance status and
user impact. NEVER include code or remediation steps.
</vpat_remarks_format>

<rules>
  - Document current conformance STATUS, not remediation steps.
  - Be evidence-based. Make only claims the underlying findings
    support.
  - Do NOT include code, CSS, HTML, or fix suggestions.
  - Do NOT invent criteria or findings not present in the input.
  - Audit-grade factual tone — no marketing language.
</rules>

<example>
<scenario>Hypothetical illustrative review: 50 SCs evaluated, with
two systemic patterns (low-contrast secondary text and image-only
links without alt). Use the SHAPE and TONE of this example, not its
content — derive your own numbers, criteria, patterns, and
disability-group impacts strictly from the findings you receive.
</scenario>
<output_executive_summary>
Of the 50 WCAG 2.2 Level AA success criteria evaluated, 29 Support,
14 Partially Support, and 7 Do Not Support. The most significant
conformance gaps are in Non-text Content (1.1.1), Contrast (Minimum)
(1.4.3), and Labels or Instructions (3.3.2).

Screen reader users relying on JAWS, NVDA, or VoiceOver cannot
access six informational images that lack text alternatives. Users
with low vision encounter twelve text elements that fall below the
4.5:1 contrast minimum. Keyboard-only users cannot escape one modal
because focus is not trapped correctly and Escape does not dismiss
it.

Two patterns repeat site-wide: low-contrast secondary text appears
across multiple page templates, and image-only navigation links
reuse the same alt-less icon component. These two patterns account
for 19 of the 21 non-conforming findings.

The product does not fully meet Section 508 requirements. The Non-
text Content and Contrast (Minimum) gaps directly affect blind,
low-vision, and keyboard-only users, raising both procurement-
eligibility and legal-exposure risk until they are remediated.
</output_executive_summary>
<output_systemic_issues>
[
  {{
    "pattern": "Low-contrast secondary text repeated across templates",
    "affected_criteria": ["1.4.3", "1.4.6"],
    "severity": "high",
    "description": "Twelve text elements (captions, helper text, byline metadata) sit at roughly 3.5:1 against their background, below the 4.5:1 AA threshold."
  }},
  {{
    "pattern": "Image-only navigation links missing alt",
    "affected_criteria": ["1.1.1", "2.4.4", "4.1.2"],
    "severity": "high",
    "description": "Header navigation reuses the same icon component; none of the seven instances provide aria-label or wrap an alt-bearing element."
  }}
]
</output_systemic_issues>
<output_vpat_remarks_excerpt>
{{
  "1.1.1": "Six informational images lack text alternatives, including the hero banner and three thumbnail tiles. Blind users relying on JAWS, NVDA, or VoiceOver receive no description of this visual content."
}}
</output_vpat_remarks_excerpt>
</example>

<output_format>
Call the report_synthesis tool exactly once. No prose or markdown
outside the tool call.
</output_format>"""


async def generate_synthesis(
    results: list[dict],
    meta: dict,
    ai_client: Any = None,
) -> dict | None:
    """Generate executive synthesis from all test results.

    Args:
        results: List of TestResult dicts (from to_dict()).
        meta: Review metadata dict.
        ai_client: Optional AIClient (unused -- uses LLMClient directly).

    Returns:
        Dict with executive_summary, systemic_issues, priority_order,
        vpat_remarks -- or None if synthesis fails.
    """
    # Chunk results by guideline group if the combined prompt would
    # overflow the reliable output window. Findings from all chunks
    # are merged; no data is dropped.
    full_prompt = _format_results_for_synthesis(results, meta)
    budget = _SYNTHESIS_PROMPT_CHUNK_CHARS - len(SYNTHESIS_SYSTEM_PROMPT)
    if len(full_prompt) <= budget:
        return await _call_synthesis(full_prompt)

    logger.info(
        "Synthesis: %d chars exceeds %d-char budget -- chunking by principle",
        len(full_prompt), budget,
    )
    chunks = _split_results_by_principle(results, meta)
    merged_summary_parts: list[str] = []
    merged_systemic: list[dict] = []
    merged_priority: list = []
    merged_remarks: dict[str, str] = {}
    for chunk_idx, (principle_label, chunk_prompt) in enumerate(chunks):
        header = (
            f"SYNTHESIS CHUNK {chunk_idx + 1} OF {len(chunks)} "
            f"-- WCAG PRINCIPLE {principle_label}\n"
            "Full site synthesis is split across chunks because of prompt "
            "size. Produce the executive_summary paragraphs ONLY for the "
            "criteria shown in this chunk; the orchestrator merges all "
            "chunks into one final report.\n\n"
        )
        result = await _call_synthesis(header + chunk_prompt)
        if not result:
            continue
        if result.get("executive_summary"):
            merged_summary_parts.append(
                f"[{principle_label}] {result['executive_summary']}"
            )
        merged_systemic.extend(result.get("systemic_issues", []) or [])
        merged_priority.extend(result.get("priority_order", []) or [])
        for k, v in (result.get("vpat_remarks") or {}).items():
            merged_remarks[k] = v

    if not merged_summary_parts and not merged_systemic and not merged_remarks:
        return None

    return {
        "executive_summary": "\n\n".join(merged_summary_parts).strip(),
        "systemic_issues": merged_systemic,
        "priority_order": merged_priority,
        "vpat_remarks": merged_remarks,
    }


async def _call_synthesis(user_prompt: str) -> dict | None:
    """Single synthesis call via the universal call_with_tools cascade."""
    try:
        client = LLMClient()
        payload = await client.call_with_tools(
            system_prompt=SYNTHESIS_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tool_name="report_synthesis",
            tool_schema=SYNTHESIS_TOOL,
            temperature=0.3,
        )
        if payload is not None:
            logger.info(
                "Synthesis complete: %d systemic issues, %d remarks",
                len(payload.get("systemic_issues", [])),
                len(payload.get("vpat_remarks", {})),
            )
            return _normalize_synthesis_payload(payload)
    except LLMError as exc:
        logger.warning("Primary synthesis failed: %s", exc)
        _primary_exc = exc
    except Exception as exc:
        logger.exception("Primary synthesis raised an unexpected error (code bug, not a model failure)")
        _primary_exc = exc
    else:
        _primary_exc = None

    # Try fallback model
    from functions.bypass_log import (
        CATEGORY_FALLBACK_MODEL, SEVERITY_HIGH, SEVERITY_MEDIUM, log_bypass,
    )
    if AI_FALLBACK_URL:
        log_bypass(
            category=CATEGORY_FALLBACK_MODEL,
            severity=SEVERITY_MEDIUM,
            source="analysis/synthesis.py:run_synthesis",
            event="synthesis_primary_failed_trying_fallback",
            details={
                "primary_error": str(_primary_exc) if _primary_exc else "None returned",
                "fallback_url": AI_FALLBACK_URL,
                "fallback_model": AI_FALLBACK_MODEL,
            },
            outcome=f"primary synthesis failed; retrying on {AI_FALLBACK_URL}",
            data_lost=False,
        )
        try:
            fallback = LLMClient(base_url=AI_FALLBACK_URL, model=AI_FALLBACK_MODEL)
            payload = await fallback.call_with_tools(
                system_prompt=SYNTHESIS_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                tool_name="report_synthesis",
                tool_schema=SYNTHESIS_TOOL,
                temperature=0.3,
            )
            if payload is not None:
                return _normalize_synthesis_payload(payload)
        except Exception as exc:
            logger.warning("Fallback synthesis also failed: %s", exc)
            log_bypass(
                category=CATEGORY_FALLBACK_MODEL,
                severity=SEVERITY_HIGH,
                source="analysis/synthesis.py:run_synthesis",
                event="synthesis_fallback_failed",
                details={
                    "fallback_url": AI_FALLBACK_URL,
                    "fallback_model": AI_FALLBACK_MODEL,
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                },
                outcome="both primary and fallback synthesis failed; report will have no executive summary / systemic issues / VPAT remarks",
                data_lost=True,
            )

    log_bypass(
        category=CATEGORY_FALLBACK_MODEL,
        severity=SEVERITY_HIGH,
        source="analysis/synthesis.py:run_synthesis",
        event="synthesis_returned_none",
        details={
            "primary_failed": _primary_exc is not None,
            "fallback_configured": bool(AI_FALLBACK_URL),
        },
        outcome="synthesis produced no payload; ACR ships without executive summary",
        data_lost=True,
    )
    return None


def _normalize_synthesis_payload(payload: dict[str, Any]) -> dict:
    return {
        "executive_summary": str(payload.get("executive_summary", "")),
        "systemic_issues": payload.get("systemic_issues", []),
        "priority_order": payload.get("priority_order", []),
        "vpat_remarks": payload.get("vpat_remarks", {}),
    }


def _format_results_for_synthesis(results: list[dict], meta: dict) -> str:
    """Format test results into a concise text block for the synthesis prompt."""
    lines = [
        f"PRODUCT: {meta.get('product_name', meta.get('source_url', 'Unknown'))}",
        f"WCAG VERSION: {meta.get('wcag_version', '2.2')}",
        f"COVERAGE LEVEL: {meta.get('coverage_level', 'AA')}",
        "",
        f"RESULTS ({len(results)} criteria tested):",
    ]

    for r in results:
        cid = r.get("criterion_id", "?")
        name = r.get("criterion_name", "?")
        level = r.get("level", "?")
        conf = r.get("conformance_level", "Not Evaluated")
        confidence = r.get("confidence", 0)
        findings = r.get("findings", [])
        n_findings = len(findings)

        line = f"  {cid} {name} (Level {level}): {conf} (confidence={confidence:.2f})"
        if n_findings:
            severities = [f.get("severity", "?") for f in findings]
            line += f" -- {n_findings} findings ({', '.join(severities)})"
        lines.append(line)

        # Include ALL findings for context -- the synthesis model needs to see
        # every issue across every criterion to spot systemic patterns.
        for f in findings:
            element = f.get("element", "?")
            issue = f.get("issue", "?")
            lines.append(f"    - [{f.get('severity', '?')}] {element}: {issue}")

    return "\n".join(lines)


def _split_results_by_principle(
    results: list[dict], meta: dict,
) -> list[tuple[str, str]]:
    """Split results into per-principle prompt chunks.

    Returns a list of ``(principle_label, user_prompt)`` tuples. Principle
    buckets: 1.x Perceivable, 2.x Operable, 3.x Understandable, 4.x Robust,
    plus a "Documents" bucket for DOC-* criteria and an "Other" bucket for
    anything that doesn't match.
    """
    buckets: dict[str, list[dict]] = {
        "1.x Perceivable": [],
        "2.x Operable": [],
        "3.x Understandable": [],
        "4.x Robust": [],
        "Documents": [],
        "Other": [],
    }
    for r in results:
        cid = r.get("criterion_id", "")
        if cid.startswith("DOC-"):
            buckets["Documents"].append(r)
        elif cid.startswith("1."):
            buckets["1.x Perceivable"].append(r)
        elif cid.startswith("2."):
            buckets["2.x Operable"].append(r)
        elif cid.startswith("3."):
            buckets["3.x Understandable"].append(r)
        elif cid.startswith("4."):
            buckets["4.x Robust"].append(r)
        else:
            buckets["Other"].append(r)

    return [
        (label, _format_results_for_synthesis(bucket_results, meta))
        for label, bucket_results in buckets.items()
        if bucket_results
    ]
