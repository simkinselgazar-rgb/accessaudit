"""Final reviewer pass — Pro-tier holistic review of the completed ACR.

Six focused calls, one decision each, run after synthesis and before
report generation. Each call has its own tool schema (functions.tools)
so the model is locked to a single decision shape per call.

Phase A (parallel): structural, calibration, contradiction, citation, tone.
Phase B (sequential, after A): synthesis — consumes A's outputs.

The orchestrator returns the raw reviewer report and a list of mutations
(verdict recalibrations + tone rewrites) that the caller applies to
result.json files before the report templates render. Every call goes
through functions.llm.LLMClient so it lands in llm_transcripts/ for audit.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from config import (
    AI_REVIEWER_API_KEY,
    AI_REVIEWER_API_URL,
    AI_REVIEWER_MODEL,
)
from functions.llm import LLMClient
from functions.tools import (
    REVIEWER_CALIBRATION_TOOL,
    REVIEWER_CITATION_TOOL,
    REVIEWER_CONTRADICTION_TOOL,
    REVIEWER_STRUCTURAL_TOOL,
    REVIEWER_SYNTHESIS_TOOL,
    REVIEWER_TONE_TOOL,
)

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
<role>
You are the senior reviewer for a Section 508 / VPAT 2.5 / WCAG 2.x
Accessibility Conformance Report (ACR). The per-criterion pipeline
has already evaluated every success criterion against captured page
data. You perform ONE specific quality check on the completed report
and return your decision via the named tool call.
</role>

<task>
You receive the full ACR on every call but you are asked to make
only ONE decision type per call. The user message tells you which
decision to make (structural, calibration, contradiction, citation,
tone, or synthesis). Stay in scope:

  - Asked for citation errors? Do NOT return tone rewrites.
  - Asked for tone rewrites? Do NOT flag conformance miscalibration.
  - Asked for structural? Do NOT critique individual finding wording.

The orchestrator runs the other reviews in parallel calls — those
decisions are not yours on this call.
</task>

<conservative_bias>
Only flag an SC/finding when you are confident it is wrong. The
per-criterion pipeline is reasonably accurate; you catch the
residual mistakes, not second-guess every judgement. If a finding is
plausible and the prose is acceptable, leave it alone. False
positives at this layer corrupt the final report — be sure before
you mutate.
</conservative_bias>

<decision_types>
<structural>
Whole-report shape. Catch missing sections, broken cross-references
between SCs, missing executive summary, empty VPAT remarks, malformed
priority order, contradictory totals (e.g. "29 Support" but only 27
SCs flagged Support).
</structural>

<calibration>
Per-SC verdict miscalibration. Severity vs conformance_level
inconsistency. The hard rules: any HIGH-severity accepted finding →
Does Not Support; any MEDIUM accepted → Partially Supports at worst;
zero accepted findings → Supports. Flag the verdicts that violate
these.
</calibration>

<contradiction>
Cross-SC contradictions. SC A says "all images have alt"; SC B says
"6 images missing alt". Same evidence quoted with conflicting
conclusions. Same selector cited as a violation in one SC and as
compliant in another.
</contradiction>

<citation>
Selector and number citations. CSS selectors that contain banned
syntax (`:contains()`, `//xpath`), IDs that don't appear in captured
DOM, contrast ratios that contradict the captured pixel_contrast or
ANDI CONTRAST values, "not keyboard reachable" claims that the
tab_walk contradicts.
</citation>

<tone>
VPAT prose quality. Marketing language ("seamless", "world-class"),
remediation instructions where Status text should be ("To fix this,
add ..."), code/CSS/HTML embedded in user-facing text, vague impact
("users may have difficulty") without naming the disability group +
AT.
</tone>

<synthesis>
After the parallel reviewers finish, you receive their outputs and
write the final cross-cutting recommendation set. Same conservative
bias: only escalate the residual mistakes that the parallel passes
flagged with high confidence.
</synthesis>
</decision_types>

<examples>
<example>
<scenario>Calibration — HIGH finding accepted but verdict says Partially Supports</scenario>
<input_excerpt>
SC 1.1.1: conformance_level="Partially Supports"
  findings: [
    { "severity": "high", "issue": "Hero image missing alt", "decision": "accepted" },
    { "severity": "medium", "issue": "Decorative thumbnail uses redundant alt", "decision": "accepted" }
  ]
</input_excerpt>
<correct_output>
Flag SC 1.1.1 for verdict recalibration: should be "Does Not Support"
because at least one accepted finding has severity=high. Cite the
hero-image finding as the trigger.
</correct_output>
</example>

<example>
<scenario>Citation — invalid CSS selector slipped through</scenario>
<input_excerpt>
SC 2.4.4 finding: css_selector = "a:contains('Read more')"
</input_excerpt>
<correct_output>
Flag the finding for citation error: jQuery `:contains()` is not
valid CSS. Recommend rewrite to a positional or attribute selector
(e.g. nav > ul > li:nth-of-type(3) > a or
a[aria-label="Read more about Q4 results"]) copied from the source.
</correct_output>
</example>

<example>
<scenario>Tone — remediation instructions in the Status text</scenario>
<input_excerpt>
SC 1.4.3 vpat_summary:
  "To fix this, change the text colour from #888 to #595959 and add
   a CSS variable --text-secondary."
</input_excerpt>
<correct_output>
Flag SC 1.4.3 for tone rewrite: VPAT Status text describes the
current state, not the fix. Suggested rewrite:
  "Twelve text elements on the product detail pages fall below the
   4.5:1 contrast minimum, including secondary captions and helper
   text. Users with low vision cannot read these elements reliably."
</correct_output>
</example>

<example>
<scenario>Contradiction — two SCs disagree on the same evidence</scenario>
<input_excerpt>
SC 4.1.2: "All buttons have accessible names via aria-label or
            visible text. Supports."
SC 2.4.4: "Three buttons (.cta-secondary) have no accessible name;
            screen readers announce them as 'button' only.
            Does Not Support."
</input_excerpt>
<correct_output>
Flag cross-SC contradiction. The same .cta-secondary buttons cannot
both have valid accessible names (per 4.1.2) and lack accessible
names (per 2.4.4). One verdict is wrong. Recommend re-judging both
against the captured ANDI INTERACTIVE / VERIFIED DOM FACTS to
determine the actual state.
</correct_output>
</example>

<example>
<scenario>Structural — executive summary references a count that
doesn't match the per-SC tallies</scenario>
<input_excerpt>
executive_summary paragraph 1: "Of the 50 SCs evaluated, 29 Support
                                 and 14 Partially Support..."
audit.json: { Supports: 27, Partially Supports: 14, Does Not Support: 7,
              Not Applicable: 2 } = 50 total
</input_excerpt>
<correct_output>
Flag structural mismatch. Executive summary claims 29 Support but
the per-SC tallies show 27. Recommend regenerating the executive
summary from the canonical totals.
</correct_output>
</example>
</examples>

<output_format>
Call the named tool exactly once with the decision shape that tool
expects. No prose, no markdown outside the tool call.
</output_format>"""


# ─── Helpers ────────────────────────────────────────────────────────────────


def _load_review(review_dir: Path) -> dict[str, Any]:
    """Load every tests/<sc>/result.json plus audit.json into one dict."""
    bundle: dict[str, Any] = {"results": [], "audit": None}
    tests_dir = review_dir / "tests"
    if tests_dir.exists():
        for sc_dir in sorted(tests_dir.iterdir()):
            if not sc_dir.is_dir():
                continue
            r_path = sc_dir / "result.json"
            if not r_path.exists():
                continue
            try:
                bundle["results"].append(json.loads(r_path.read_text(encoding="utf-8")))
            except Exception as exc:
                logger.warning("Reviewer skipped unreadable %s: %s", r_path, exc)
    audit_path = review_dir / "audit.json"
    if audit_path.exists():
        try:
            bundle["audit"] = json.loads(audit_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Reviewer could not read audit.json: %s", exc)
    return bundle


def _client() -> LLMClient:
    return LLMClient(
        base_url=AI_REVIEWER_API_URL,
        model=AI_REVIEWER_MODEL,
        api_key=AI_REVIEWER_API_KEY,
    )


def _compact(obj: Any) -> str:
    """Compact JSON for prompt embedding (no whitespace bloat)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _build_results_block(results: list[dict]) -> str:
    """Strip results down to the fields the reviewer actually needs.

    We do NOT send full screenshots, base64, or capture data — the
    reviewer is reasoning over the textual ACR, not re-evaluating the
    page. Keeping the prompt lean keeps every call within Pro's
    context budget even on very large multi-page reviews.
    """
    slim: list[dict] = []
    for r in results:
        findings = []
        for i, f in enumerate(r.get("findings") or []):
            findings.append({
                "i": i,
                "element": f.get("element", ""),
                "css_selector": f.get("css_selector", ""),
                "issue": f.get("issue", ""),
                "impact": f.get("impact", ""),
                "recommendation": f.get("recommendation", ""),
                "internal_remediation_note": f.get("internal_remediation_note", ""),
                "severity": _stringify(f.get("severity")),
                "source": f.get("source", ""),
            })
        slim.append({
            "criterion_id": r.get("criterion_id", "?"),
            "criterion_name": r.get("criterion_name", ""),
            "level": r.get("level", ""),
            "verdict": _stringify(r.get("conformance_level")),
            "confidence": r.get("confidence"),
            "summary": r.get("summary", ""),
            "needs_review": r.get("needs_review", False),
            "needs_review_reasons": r.get("needs_review_reasons") or [],
            "source_counts": {
                "programmatic": r.get("programmatic_findings_count", 0),
                "ai": r.get("ai_findings_count", 0),
                "code_ai": r.get("code_ai_findings_count", 0),
                "at_sim": r.get("at_sim_findings_count", 0),
            },
            "findings": findings,
        })
    return _compact(slim)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


async def _call(
    client: LLMClient,
    *,
    label: str,
    user_prompt: str,
    tool: dict,
) -> dict[str, Any] | None:
    """Run one reviewer call. Returns tool args dict or None on failure."""
    tool_name = tool["function"]["name"]
    try:
        return await client.call_with_tools(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tool_name=tool_name,
            tool_schema=tool,
            temperature=0.1,
            max_tool_attempts=2,
            restructure_on_failure=True,
        )
    except Exception as exc:
        logger.exception("Final reviewer call %s failed: %s", label, exc)
        return None


# ─── The six calls ──────────────────────────────────────────────────────────


async def _call_structural(client: LLMClient, results_block: str) -> dict[str, Any] | None:
    user = (
        "TASK: Structural completeness review.\n\n"
        "Inspect every SC result below. Flag SCs that:\n"
        "  - Have no verdict (conformance_level missing or empty).\n"
        "  - Have findings but an empty or missing summary.\n"
        "  - Have schema problems (e.g. a finding missing element/issue/severity, "
        "or severity outside the {high, medium, low, info} enum).\n"
        "Do NOT recalibrate verdicts here -- a different call handles that.\n\n"
        f"ACR DATA:\n{results_block}\n\n"
        "Call report_structural_review with what you find. Empty arrays are fine."
    )
    return await _call(client, label="reviewer_structural", user_prompt=user, tool=REVIEWER_STRUCTURAL_TOOL)


async def _call_calibration(client: LLMClient, results_block: str) -> dict[str, Any] | None:
    user = (
        "TASK: Severity-to-conformance calibration review.\n\n"
        "For each SC below, decide whether the verdict matches the severity "
        "distribution of its findings. Apply these rules:\n"
        "  - 0 findings, or only severity=info, or only severity=low -> Supports\n"
        "  - any severity=medium present (and no high) -> Partially Supports\n"
        "  - any severity=high present -> Does Not Support\n"
        "  - the criterion does not apply to the page content -> Not Applicable\n"
        "Only emit a recalibration when the CURRENT verdict is clearly wrong "
        "given the rules above. Do NOT recalibrate borderline cases.\n\n"
        f"ACR DATA:\n{results_block}\n\n"
        "Call report_calibration_review with the recalibrations. Empty array is fine."
    )
    return await _call(client, label="reviewer_calibration", user_prompt=user, tool=REVIEWER_CALIBRATION_TOOL)


async def _call_contradiction(client: LLMClient, results_block: str) -> dict[str, Any] | None:
    user = (
        "TASK: Cross-SC contradiction scan.\n\n"
        "Look for elements that appear in multiple findings (matched by "
        "css_selector or unambiguous element description). Identify cases "
        "where two findings make CONTRADICTORY claims about the same "
        "element -- e.g. one says 'the link is keyboard-reachable' and "
        "another says 'the link is not keyboard-reachable'. Different SCs "
        "flagging the same element for different reasons is NORMAL and not "
        "a contradiction (a logo-link can fail 4.1.2 for missing alt and "
        "fail 2.4.4 for unclear purpose simultaneously).\n\n"
        f"ACR DATA:\n{results_block}\n\n"
        "Call report_contradiction_review. Empty array is fine."
    )
    return await _call(client, label="reviewer_contradiction", user_prompt=user, tool=REVIEWER_CONTRADICTION_TOOL)


async def _call_citation(client: LLMClient, results_block: str) -> dict[str, Any] | None:
    user = (
        "TASK: WCAG citation accuracy review.\n\n"
        "Every finding's issue and recommendation may cite WCAG criterion "
        "numbers, normative thresholds, and requirements. Flag every case "
        "where a citation is FACTUALLY WRONG (e.g. 'WCAG 1.4.3 requires "
        "3:1 for normal text' -- it requires 4.5:1; or 'WCAG 1.4.10 "
        "requires reflow at 256 CSS pixels' -- it requires 320). Use your "
        "knowledge of WCAG 2.0/2.1/2.2 normative text. Do NOT flag stylistic "
        "differences or paraphrasing. Only flag clear factual errors.\n\n"
        f"ACR DATA:\n{results_block}\n\n"
        "Call report_citation_review. Empty array is fine."
    )
    return await _call(client, label="reviewer_citation", user_prompt=user, tool=REVIEWER_CITATION_TOOL)


async def _call_tone(client: LLMClient, results_block: str) -> dict[str, Any] | None:
    user = (
        "TASK: VPAT 2.5 ACR prose tone review.\n\n"
        "Inspect each SC's vpat_summary and finding text. Suggest verbatim "
        "rewrites only where the prose violates VPAT 2.5 conventions:\n"
        "  - third-person voice (no 'you', no 'your site')\n"
        "  - factual claims (no hedging, no 'might', no 'could')\n"
        "  - no implementation suggestions in issue/recommendation "
        "(WCAG conformance language only -- no code, no CSS fixes)\n"
        "  - no colloquialisms, contractions sparingly\n"
        "Provide the original text verbatim alongside the suggested rewrite "
        "so the orchestrator can apply the change with a string replace. "
        "Do NOT rewrite text that is already compliant.\n\n"
        f"ACR DATA:\n{results_block}\n\n"
        "Call report_tone_review. Empty array is fine."
    )
    return await _call(client, label="reviewer_tone", user_prompt=user, tool=REVIEWER_TONE_TOOL)


async def _call_synthesis(
    client: LLMClient,
    results_block: str,
    audit: dict | None,
    phase_a: dict[str, dict | None],
) -> dict[str, Any] | None:
    user = (
        "TASK: Final synthesis. Write the executive summary, identify "
        "systemic issues spanning multiple SCs, and recommend a remediation "
        "priority order.\n\n"
        "You may incorporate the prior reviewer pass results as known facts "
        "when writing the summary -- e.g. miscalibrated verdicts, "
        "contradictions, citation errors, prose issues. The orchestrator "
        "applies recalibrations and rewrites separately; your job is to "
        "produce the prose that opens the ACR and the systemic-issue list "
        "the operator should action first.\n\n"
        f"ACR DATA:\n{results_block}\n\n"
        f"STRUCTURAL AUDIT:\n{_compact(audit) if audit else '{}'}\n\n"
        f"PRIOR REVIEWER OUTPUTS:\n{_compact(phase_a)}\n\n"
        "Call report_final_synthesis. The executive_summary should be a "
        "2-4 paragraph opener suitable for the ACR. The systemic_issues "
        "should each name a root cause that, if fixed, would resolve "
        "multiple criteria at once. The priority_order should list the "
        "criterion_ids in remediation order with one-sentence rationales."
    )
    return await _call(client, label="reviewer_synthesis", user_prompt=user, tool=REVIEWER_SYNTHESIS_TOOL)


# ─── Orchestrator ───────────────────────────────────────────────────────────


async def run_all(review_dir: Path | str) -> dict[str, Any]:
    """Run the six reviewer calls and return the full report.

    Phase A is launched in parallel (5 independent calls). Phase B
    (synthesis) runs after Phase A and consumes its outputs. The full
    report is saved to ``<review_dir>/reviewer_report.json`` and also
    returned so the caller can apply recalibrations and tone rewrites
    to result.json files before report generation.
    """
    review_dir = Path(review_dir)
    bundle = _load_review(review_dir)
    if not bundle["results"]:
        logger.info("Final reviewer skipped: no SC results in %s", review_dir)
        return {"status": "skipped", "reason": "no SC results"}

    results_block = _build_results_block(bundle["results"])
    client = _client()

    logger.info(
        "Final reviewer: starting Phase A (5 parallel calls) on %d SCs, %d total findings",
        len(bundle["results"]),
        sum(len(r.get("findings") or []) for r in bundle["results"]),
    )

    phase_a_results = await asyncio.gather(
        _call_structural(client, results_block),
        _call_calibration(client, results_block),
        _call_contradiction(client, results_block),
        _call_citation(client, results_block),
        _call_tone(client, results_block),
        return_exceptions=False,
    )
    structural, calibration, contradiction, citation, tone = phase_a_results

    phase_a = {
        "structural": structural,
        "calibration": calibration,
        "contradiction": contradiction,
        "citation": citation,
        "tone": tone,
    }
    logger.info(
        "Final reviewer: Phase A complete — structural=%s calibration=%s "
        "contradiction=%s citation=%s tone=%s",
        bool(structural), bool(calibration), bool(contradiction),
        bool(citation), bool(tone),
    )

    synthesis = await _call_synthesis(client, results_block, bundle["audit"], phase_a)
    logger.info("Final reviewer: synthesis %s", "OK" if synthesis else "FAILED")

    report: dict[str, Any] = {
        "status": "ok",
        "model": AI_REVIEWER_MODEL,
        "total_sc_reviewed": len(bundle["results"]),
        "structural": structural or {"missing_verdicts": [], "missing_summaries": [], "schema_violations": []},
        "calibration": calibration or {"recalibrations": []},
        "contradiction": contradiction or {"contradictions": []},
        "citation": citation or {"citation_errors": []},
        "tone": tone or {"rewrites": []},
        "synthesis": synthesis or {"executive_summary": "", "systemic_issues": [], "priority_order": []},
    }

    out_path = review_dir / "reviewer_report.json"
    try:
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Final reviewer report saved to %s", out_path)
    except Exception as exc:
        logger.warning("Could not save reviewer_report.json: %s", exc)

    return report


# ─── Mutation application ───────────────────────────────────────────────────


def apply_mutations(review_dir: Path | str, report: dict[str, Any]) -> dict[str, int]:
    """Apply reviewer-suggested verdict recalibrations and tone rewrites.

    Modifies tests/<sc>/result.json files in place. Returns a counts dict
    so the caller can log how many changes landed. Idempotent — applying
    twice is harmless because the second pass would find nothing to change.
    """
    review_dir = Path(review_dir)
    counts = {"recalibrated": 0, "rewritten": 0, "skipped": 0}
    if report.get("status") != "ok":
        return counts

    recalibrations = (report.get("calibration") or {}).get("recalibrations") or []
    rewrites = (report.get("tone") or {}).get("rewrites") or []

    for recal in recalibrations:
        sc = (recal.get("criterion_id") or "").strip()
        suggested = recal.get("suggested_verdict") or ""
        if not sc or not suggested:
            counts["skipped"] += 1
            continue
        sc_path = _sc_result_path(review_dir, sc)
        if not sc_path.exists():
            counts["skipped"] += 1
            continue
        try:
            data = json.loads(sc_path.read_text(encoding="utf-8"))
            if _stringify(data.get("conformance_level")) == suggested:
                continue  # already correct
            data["conformance_level"] = suggested
            data.setdefault("reviewer_notes", []).append({
                "type": "recalibration",
                "from": _stringify(recal.get("current_verdict")),
                "to": suggested,
                "reason": recal.get("reason", ""),
            })
            sc_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            counts["recalibrated"] += 1
        except Exception as exc:
            logger.warning("Could not apply recalibration for %s: %s", sc, exc)
            counts["skipped"] += 1

    for rw in rewrites:
        sc = (rw.get("criterion_id") or "").strip()
        field = rw.get("field") or ""
        original = rw.get("original") or ""
        suggested = rw.get("suggested") or ""
        if not (sc and field and original and suggested):
            counts["skipped"] += 1
            continue
        sc_path = _sc_result_path(review_dir, sc)
        if not sc_path.exists():
            counts["skipped"] += 1
            continue
        try:
            data = json.loads(sc_path.read_text(encoding="utf-8"))
            applied = _apply_rewrite(data, field, rw.get("finding_index"), original, suggested)
            if applied:
                data.setdefault("reviewer_notes", []).append({
                    "type": "tone_rewrite",
                    "field": field,
                    "finding_index": rw.get("finding_index"),
                    "reason": rw.get("reason", ""),
                })
                sc_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                counts["rewritten"] += 1
            else:
                counts["skipped"] += 1
        except Exception as exc:
            logger.warning("Could not apply tone rewrite for %s.%s: %s", sc, field, exc)
            counts["skipped"] += 1

    return counts


def _sc_result_path(review_dir: Path, criterion_id: str) -> Path:
    """tests/<sc-with-underscores>/result.json"""
    sc_dir = criterion_id.strip().replace(".", "_")
    return review_dir / "tests" / sc_dir / "result.json"


def _apply_rewrite(
    data: dict,
    field: str,
    finding_index: Any,
    original: str,
    suggested: str,
) -> bool:
    """Apply a single tone rewrite to a result.json dict in place."""
    if field == "summary":
        if data.get("summary") == original:
            data["summary"] = suggested
            return True
        return False

    if field not in ("issue", "impact", "recommendation", "internal_remediation_note"):
        return False
    if not isinstance(finding_index, int):
        return False
    findings = data.get("findings") or []
    if finding_index < 0 or finding_index >= len(findings):
        return False
    f = findings[finding_index]
    if f.get(field) == original:
        f[field] = suggested
        return True
    return False
