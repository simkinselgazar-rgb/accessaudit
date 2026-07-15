"""Judge AI -- final arbiter that reviews all source verdicts.

After all 4 sources (Programmatic, Visual AI, Code AI, AT Simulation)
produce their findings, the Judge reviews everything and decides:
1. Which findings are relevant to THIS criterion
2. Which findings belong to a different criterion (reject them)
3. The final conformance level with reasoning
4. Confidence in the decision

Every judge call goes through ``LLMClient.call_with_tools``, which supplies
the full 3-attempt retry + LLM-based prose restructure cascade that every
other structured-output call in the system uses.
"""
from __future__ import annotations

import logging
from typing import Any

from config import (
    AI_JUDGE_API_KEY, AI_JUDGE_API_URL, AI_JUDGE_MODEL,
)
from functions.finding_dedup import _llm_dedupe_findings
from functions.llm import LLMClient, LLMError
from functions.parser import normalize_conformance_level, normalize_severity
from functions.tools import JUDGE_TOOL
from models import ConformanceLevel

logger = logging.getLogger(__name__)


# Chunking threshold for oversized judge prompts. When the assembled
# user prompt would exceed this many characters, findings are batched
# by source and the judge runs once per batch. Results merge: all
# final_findings union, all rejected_findings union, worst conformance
# wins, confidence = min across batches. Sized at 400K chars (~100K
# tokens) so a 128K-context model retains room for the tool schema,
# system prompt, and generated response.
_JUDGE_PROMPT_CHUNK_CHARS = 400_000


# ── Per-criterion source expertise ───────────────────────────────────────────
# Tells the judge which source is best positioned to evaluate each criterion.

SOURCE_EXPERTISE: dict[str, str] = {
    # Programmatic is the expert (mathematical/structural)
    "2.3.1": "Programmatic is the expert: flash rate is computed mathematically from frame analysis.",
    "4.1.1": "Programmatic is the expert: duplicate IDs and parsing errors are detected by exact DOM analysis.",
    "1.4.3": "Programmatic is the expert for computed contrast ratios. AI may catch issues with text over images/gradients.",
    "3.1.1": "Programmatic is the expert: the lang attribute is either present and valid or not.",
    "3.1.2": "Programmatic is the expert: lang attributes on elements are either present or not.",
    # Visual AI is the expert
    "1.3.2": "Visual AI is the expert: meaningful sequence requires comparing visual layout against DOM order.",
    "1.3.3": "Visual AI is the expert: sensory characteristics require seeing whether instructions rely solely on shape/size/position/color.",
    "1.4.1": "Visual AI is the expert: detecting color-only distinctions requires visual inspection.",
    "1.4.4": "Visual AI is the expert: it can SEE whether content is clipped or just reflowed at 200% zoom.",
    "1.4.5": "Visual AI is the expert: it can SEE whether text is rendered as an image.",
    "2.4.7": "Visual AI is the expert: it can SEE whether focus indicators are visible in the keyboard walkthrough video.",
    "1.1.1": "Visual AI is the expert for alt text QUALITY: it can see what an image shows and compare against the alt text.",
    "2.4.6": "Visual AI is the expert: it can judge whether headings are descriptive of their content sections.",
    # Code AI is the expert
    "2.1.4": "Code AI is the expert: character key shortcuts require analyzing JavaScript event handlers.",
    "3.2.1": "Code AI is the expert: detecting context changes on focus requires understanding JS handlers.",
    "3.2.2": "Code AI is the expert: detecting context changes on input requires understanding form handlers.",
    # AT Simulation is the expert
    "4.1.2": "AT Simulation is the expert: it reports exactly what a screen reader announces.",
    "2.5.3": "AT Simulation is the expert: it compares computed accessible name against visible label.",
    "1.3.1": "AT Simulation and Programmatic share expertise: one for structure, one for screen reader behavior.",
    # Keyboard walkthrough video
    "2.1.1": (
        "CRITICAL: Tab walk data is DETERMINISTIC PROOF of keyboard reachability. "
        "REJECT any AI finding claiming an element is 'not reachable' if it appears in tab walk data."
    ),
    "2.1.2": "The keyboard walkthrough video is the expert: it shows whether focus gets trapped.",
    "2.4.3": "The keyboard walkthrough video is the expert: it shows whether focus order matches visual layout.",
}


# ── Judge system prompt ──────────────────────────────────────────────────────

JUDGE_SYSTEM_PROMPT = """\
<role>
You are a WCAG accessibility auditor acting as the final arbiter for a
Section 508 conformance evaluation. Three upstream analyzers
(programmatic DOM check, code AI, visual AI) have already produced
findings against ONE specific WCAG success criterion. You produce the
final verdict and VPAT ACR text for that criterion.
</role>

<task>
Work through the input in this order and call the report_judgment tool
exactly once at the end:

  1. Read the CRITERION GUIDANCE block in the user prompt to internalise
     this criterion's pass / fail / NA / anti-pattern / off-scope rules.
  2. For every incoming finding, decide ACCEPT or REJECT against those
     rules and against the VERIFIED DOM FACTS / GROUND TRUTH blocks.
     A finding is only ACCEPT if it matches a fail_condition AND its
     evidence agrees with the captured ground truth.
  3. Cluster surviving findings: when two or more describe the SAME
     root issue on the SAME element, merge them into one entry (worst
     severity, clearest wording, comma-joined source list).
  4. Pick ONE conformance_level: Supports, Partially Supports,
     Does Not Support, or Not Applicable. You are the final answer —
     never return Not Evaluated.
  5. Write a 1-3 sentence vpat_summary in professional ACR language.
</task>

<rules>
<per_criterion_rules_are_law>
The user prompt's CRITERION GUIDANCE block lists pass_conditions,
fail_conditions, auditor_anti_patterns, and off_scope_topics. Treat
those as authoritative law:
  - ACCEPT a finding only if it matches a fail_condition.
  - REJECT (auditor_anti_pattern) → list in rejected_findings.
  - REJECT (off_scope_topic) → finding belongs to another SC.
  - When your training conflicts with the guidance, obey the guidance.
</per_criterion_rules_are_law>

<verdict_rules>
  - 0 accepted findings → Supports.
  - Only low/info severity accepted → Supports.
  - Any medium severity accepted → Partially Supports.
  - Any high severity accepted → Does Not Support.
  - Criterion does not apply to this page → Not Applicable.
</verdict_rules>

<selector_evidence>
Every css_selector, class name, or ID in final_findings MUST appear
verbatim in the user prompt (ALL FINDINGS block, VERIFIED DOM FACTS,
or a GROUND TRUTH block). Do not invent selectors.

HALLUCINATED-ID RULE: if a source finding's selector contains an ID
literal and that exact id="..." attribute does not appear anywhere
in the captured DOM, REJECT with reason='invented_selector'. Code
AI occasionally fabricates plausible-sounding IDs.
</selector_evidence>

<measurement_evidence>
Every finding carries a structured `cited_measurements` array. For
EACH concrete measured value your `issue` prose states -- a contrast
ratio, a pixel dimension, a computed CSS value -- add one entry
{selector, metric, value} recording the value you read from a
deterministic measurement block in the prompt.

- Record ONLY measured values. A WCAG threshold/requirement (the
  "4.5:1" in "below the 4.5:1 minimum") is NOT a measurement -- never
  put a threshold in cited_measurements.
- If a finding states no measured number, use an empty array [].
- The system verifies every entry against the captured measurements.
  An entry with no matching measurement is flagged as an unverified
  inference and the finding is demoted. So: if you cannot find a
  measured value in the prompt for the element, do NOT state a
  number in the prose either -- describe the issue qualitatively.
</measurement_evidence>

<cross_source_dedup>
When 2+ incoming findings reference the same element with the same
root issue, MERGE them into a single final_findings entry. Two
findings are the same issue when their selectors target the same DOM
node (different path representations are still a merge: '#hero > img',
'img.hero', and '#main > article > img' may all be the same node) AND
their issue text describes the same root cause. Different problems on
the same element (e.g. missing alt AND wrong contrast) are SEPARATE
entries.
</cross_source_dedup>

<evidence_grounding>
Reject findings whose numbers are invented. Before accepting any
numerical claim, verify it against the user prompt's ground-truth
blocks:
  - Contrast ratio claims → check pixel_contrast / focus_contrast /
    ANDI CONTRAST / computed_styles for that selector. Captured value
    wins (e.g. captured 21:1 beats claimed 1.00:1).
  - CSS value claims (--grid-column-count, width, class name) → must
    appear verbatim in dom.html or computed_styles.
  - "Not keyboard reachable" → verify the selector does NOT appear in
    the [TAB ORDER] / tab_walk block. If the tab walk reached it, the
    claim is false.
  - "Empty link / no accessible name" → apply ARIA 1.2 name
    computation: aria-labelledby > aria-label > native content >
    title. A link wrapping <img alt="X"> has accessible name "X".

When rejecting on these grounds, set
rejected_findings[].reason='evidence_contradicted' and quote the
captured value in the reason text.
</evidence_grounding>

<unsupported_categorical_claims>
A finding that asserts a SPECIFIC FEATURE, COMPONENT, OR PATTERN
EXISTS on the page (a CAPTCHA, a modal dialog, a drag handle, an
audio track, an autoplaying carousel, a video element, a form,
etc.) MUST be supported by captured ground truth that confirms the
feature's presence.

Verify against the relevant capture_data block:
  - "CAPTCHA / reCAPTCHA / hCaptcha" → check capture_data.captchas
    and the DOM context for captcha/recaptcha/hcaptcha/turnstile
    markers. If captchas=[] AND no marker appears in the DOM, the
    feature does NOT exist on this page.
  - "modal dialog" → check capture_data.modal_interactions and the
    keyboard_roundtrip block. If both are empty for the cited
    selector, no modal exists.
  - "video / audio / media" → check capture_data.media. If empty,
    no media exists.
  - "form / input / control" → check capture_data.form_fields.
  - "carousel / slider auto-cycle" → check
    capture_data.dynamic_content for hasMarquee / hasAnimations
    OR capture_data.exploration_results for response=carousel_change.

When the AI cites a feature that no captured block confirms, REJECT
with reason='unsupported_categorical_claim' and quote which
ground-truth block was checked and found empty. The model
hallucinated the feature.

Example: a visual AI finding says "The authentication process
uses a CAPTCHA without an alternative" with empty evidence.
capture_data.captchas is []. The captured DOM has no
captcha/recaptcha/hcaptcha/turnstile string anywhere. → REJECT,
reason='unsupported_categorical_claim'.
</unsupported_categorical_claims>

<unsupported_numerical_claims>
A finding that cites a SPECIFIC numerical value (contrast ratio
like "2.36:1" or "1.4:1", a percentage, a count, a CSS property
value, a pixel measurement) MUST cite captured evidence in either
its ``evidence`` field or by referencing a ground-truth block
(ANDI CONTRAST, pixel_contrast, axe_results, computed_styles,
tab_coverage, etc.).

If the finding cites a number AND the ``evidence`` field is empty
AND no captured ground-truth block contains that number for that
selector, REJECT the finding with
reason='unsupported_numerical_claim'. The model fabricated the
measurement.

This rule exists specifically because vision models trained on
screenshots tend to "estimate" contrast ratios that look plausible
but are not measured (e.g. "2.36:1 between the button border and
its background") even when the prompt says not to. We trust
captured measurements; we do NOT trust unsupported AI numbers.

Example: a visual AI finding says "The border of the 'Visit ASU'
button has a contrast ratio of 2.36:1" with empty evidence.
ANDI CONTRAST has no entry for this selector (it's a UI border,
not text). pixel_contrast and nontext_contrast also have no
matching entry. → REJECT, reason='unsupported_numerical_claim'.

If the finding describes a real visual problem but lacks a
specific measurement, it can still be ACCEPTED at severity=info
with the numerical claim removed from the issue text — record
the qualitative observation, drop the fabricated number.
</unsupported_numerical_claims>

<consolidation_preserves_source_truth>
When you MERGE multiple incoming findings into a single
final_findings entry (per <cross_source_dedup>), the merged
issue text MUST preserve the EXACT directional framing and
counts from the source findings. Do NOT introduce a new number
that does not appear in any source finding. Do NOT change the
direction of the underlying issue.

Common directional flips to AVOID:
  - source says "focusable element has aria-hidden=true on
    ancestor (element IS in tab order but invisible to AT)" →
    do NOT write "element is missing from tab order" or
    "interactive element is not reachable via tab". The element
    IS reachable; the problem is that it is reached while
    hidden from AT. Those are opposite framings.
  - source data shows tab_coverage = X/Y reached → do NOT
    invent a different reached count. Use X and Y verbatim.
    The "unreached" count is Y - X minus any
    roving_tabindex_valid entries.
  - source shows widget_keyboard tested keys=[ArrowRight,
    ArrowLeft] → do NOT claim "Enter or Space do not respond".
    Those keys were not tested. Cite only the keys actually
    in keys_tested or key_results.

When consolidating, the merged issue text should READ as a
faithful summary of the source claims, not as a rewritten
version with new framing. If you cannot summarise without
inventing a number or flipping a direction, prefer keeping
the per-element findings separate.
</consolidation_preserves_source_truth>

<source_attribution_integrity>
Every final_findings entry MUST be tagged with the source it
actually came from. The allowed values are:
  programmatic, axe, andi, htmlcs, ibm_eac,
  visual_ai, code_ai, at_sim, judge_inference

Rules — load-bearing for verdict trust, audited automatically:

1. When you SYNTHESIZE / REWORD / CONSOLIDATE an existing
   input finding, the output's `source` MUST be the input's
   source tag. If multiple inputs from different sources merge
   into one output, list ALL of them comma-separated: e.g.
   "axe, htmlcs, ibm_eac". Multi-source corroboration is the
   strongest possible signal -- preserve it.

2. When you ADD a finding that NO input source produced — i.e.
   you noticed something in the DOM context, screenshots, or
   evidence that none of programmatic / axe / andi / htmlcs /
   ibm_eac / visual_ai / code_ai / at_sim flagged — the source
   MUST be "judge_inference". This is a legitimate use; you are
   allowed to spot what the upstream sources missed. You are
   NOT allowed to disguise that as a measured signal.

3. NEVER label your own additions as "programmatic", "axe",
   "andi", "htmlcs", "ibm_eac", "visual_ai", "code_ai", or
   "at_sim" unless one of those sources flagged the same
   element. Those tags are claims about which subsystem found
   the issue; an auditor reading the report relies on them to
   mean a measurement was taken or a tool fired.

4. The system runs an automatic source-attribution validator
   on your output. Any source claim that does not trace back
   to a matching input finding is downgraded to
   "judge_inference" automatically and logged. So the only
   thing you accomplish by mislabeling is making the audit
   trail noisier; the verdict still gets recorded as inference.
   Be honest up front.
</source_attribution_integrity>

<deterministic_tool_consensus>
Three independent open-source rule engines run on every page:
axe-core (Deque), HTML_CodeSniffer (Squiz Labs), and IBM
Equal Access (IBM). Each implements WCAG conformance rules
with different logic and catches different patterns. ANDI
(SSA Section 508 office) adds per-text-node deterministic
measurements as a fourth source.

How to weight cross-tool agreement when consolidating:

  - When 2+ deterministic tools (any combination of axe / htmlcs /
    ibm_eac / andi) flag the SAME element for the SAME root issue:
    treat as VERY STRONG evidence. Accept the finding, merge the
    sources comma-separated, set severity to the WORST severity
    among the inputs, and write VPAT prose. Do NOT downgrade or
    drop multi-tool-agreement findings without explicit
    contradicting ground truth elsewhere in the prompt.

  - When ONLY ONE deterministic tool flags a finding: that's a
    candidate, not a confirmed failure. Cross-check against the
    VERIFIED DOM FACTS, the screenshots (if attached), and any
    measurement-evidence block for the SC. Tools occasionally
    flag false positives (axe on valid aria-hidden focusable
    patterns, htmlcs on some heading-skip cases that are
    semantically correct, ibm_eac on decorative SVGs with
    role="img"). Keep the finding when ground truth corroborates;
    drop with reason='evidence_contradicted' when ground truth
    refutes; keep at lowered severity when ground truth is silent.

  - When deterministic tools DISAGREE (one flags, one passes the
    same element on the same rule): rare but happens because the
    tools cover overlapping but non-identical rule sets. Trust
    the more specific, more measurement-grounded source. ANDI's
    ratio measurement beats axe's "incomplete contrast" flag
    because ANDI did the math.
</deterministic_tool_consensus>
</rules>

<examples>
<example>
<scenario>Accept and merge cross-source duplicates</scenario>
<input>
CRITERION GUIDANCE for SC 1.1.1: fail_condition = "<img> with no alt attribute and no aria-label/aria-labelledby".
ALL FINDINGS:
  [programmatic] selector="img#hero" issue="Image missing alt attribute" severity=high
  [ai]           selector="#hero img" issue="Hero banner has no text alternative" severity=high
  [code_ai]      selector="img#hero" issue="<img src='/banner.jpg'> lacks alt text" severity=high
VERIFIED DOM FACTS contain: <img id="hero" src="/banner.jpg">
</input>
<output_reasoning>
All three findings target the same DOM node (#hero img == img#hero).
Same root issue: missing alt. Selector verified in DOM. Merge into
one entry, source = "programmatic, ai, code_ai", severity = high
(worst). Verdict: Does Not Support.
</output_reasoning>
<output_tool_call>
final_findings: [{
  "element": "img#hero",
  "css_selector": "img#hero",
  "issue": "The hero banner image (<img src=\\"/banner.jpg\\">) has no alt attribute and no aria-label.",
  "impact": "Screen reader users on JAWS, NVDA, and VoiceOver receive no description of this image.",
  "recommendation": "Add a meaningful alt attribute describing the image content.",
  "severity": "high",
  "source": "programmatic, ai, code_ai"
}]
conformance_level: "Does Not Support"
rejected_findings: []
</output_tool_call>
</example>

<example>
<scenario>Reject for hallucinated selector</scenario>
<input>
ALL FINDINGS:
  [code_ai] selector="#fake-debug-overlay" issue="Debug overlay traps keyboard focus" severity=high
VERIFIED DOM FACTS contain no element with id="fake-debug-overlay".
</input>
<output_reasoning>
The selector references an ID that does not appear anywhere in the
captured DOM. This is the hallucinated-ID failure mode — code AI
fabricated a plausible-sounding ID. REJECT.
</output_reasoning>
<output_tool_call>
final_findings: []
rejected_findings: [{
  "css_selector": "#fake-debug-overlay",
  "reason": "invented_selector",
  "explanation": "id='fake-debug-overlay' does not appear in the captured DOM."
}]
conformance_level: "Supports"   (assuming no other findings remained)
</output_tool_call>
</example>

<example>
<scenario>Reject for evidence contradicted by ground truth</scenario>
<input>
CRITERION GUIDANCE for SC 1.4.3: fail_condition = "ratio < 4.5:1 normal, < 3:1 large".
ALL FINDINGS:
  [ai] selector="p.lead" issue="Contrast 1.00:1 between text and background" severity=high
ANDI CONTRAST shows: p.lead ratio=21.00 required=4.5 passes=True fg=rgb(0,0,0) bg=rgb(255,255,255)
</input>
<output_reasoning>
The visual AI claimed 1.00:1 but the deterministic ANDI measurement
records 21.00:1 for the same selector. Captured value wins. REJECT.
</output_reasoning>
<output_tool_call>
final_findings: []
rejected_findings: [{
  "css_selector": "p.lead",
  "reason": "evidence_contradicted",
  "explanation": "ANDI CONTRAST measured 21.00:1 for p.lead; the claimed 1.00:1 is fabricated."
}]
conformance_level: "Supports"
</output_tool_call>
</example>
</examples>

<output_format>
Call the report_judgment tool exactly once. Emit no prose and no
markdown outside the tool call.
</output_format>"""


# ── Main judge function ──────────────────────────────────────────────────────

async def judge_criterion(
    criterion_id: str,
    criterion_name: str,
    level: str,
    normative_text: str,
    source_verdicts: dict[str, dict],
    all_findings: list[dict],
    wcag_version: str = "2.2",
    dom_context: str = "",
    product_context: Any = None,
    programmatic_only: bool = False,
    code_findings: list[dict] | None = None,
    code_findings_embeddings: list[list[float]] | None = None,
    images: list[str] | None = None,
) -> dict[str, Any] | None:
    """Run the Judge AI on one criterion's results.

    Args:
        programmatic_only: VPAT synthesis mode — accept all programmatic
            findings as definitive truth, only rewrite in VPAT language.
        code_findings: Layer 1 per-page pattern cache (from
            ``capture_data.code_findings``). When given alongside
            ``code_findings_embeddings`` the judge performs Layer 3
            semantic retrieval and appends the top-K most SC-similar
            snippets to its user prompt as grounding evidence.
        code_findings_embeddings: bge-m3 vectors aligned with
            ``code_findings``, built once per review by
            ``functions.code_analyzer.analyze_page_code``.
        images: optional list of screenshot paths the judge should see
            alongside the DOM facts. Pass the SAME images that were sent
            to the visual_ai run so the judge can independently verify or
            reject visual_ai's findings against the actual pixels. When
            None or empty, the judge runs text-only (used by the
            programmatic fast path where no AI source ran on images).
            ``LLMClient._select_model`` automatically routes image-bearing
            calls to ``AI_LOCAL_JUDGE_MODEL`` (CLAUDE.md "Accuracy over
            speed").

    Returns dict with conformance_level, confidence, reasoning, final_findings,
    rejected_findings, vpat_summary -- or None if the judge call fails.
    """
    # Load criterion-specific guidance
    criterion_guidance = _load_criterion_guidance(criterion_id)

    # VPAT synthesis mode for programmatic-definitive criteria
    if programmatic_only:
        logger.info("Judge: VPAT synthesis mode for SC %s (%d findings)",
                     criterion_id, len(all_findings))
        return await _judge_vpat_synthesis(
            criterion_id, criterion_name, level, normative_text,
            source_verdicts, all_findings, wcag_version,
            dom_context, product_context, criterion_guidance,
        )

    # Add per-criterion source expertise
    expertise = SOURCE_EXPERTISE.get(criterion_id, "")
    if expertise and "SOURCE EXPERTISE" not in criterion_guidance:
        criterion_guidance += f"\n\nSOURCE EXPERTISE FOR THIS CRITERION:\n{expertise}"

    # Load per-SC guidance from the criterion JSON and append to
    # criterion_guidance so it reaches the user prompt. The judge obeys
    # the universal rules in its system prompt AND the per-SC rules shown
    # here. This is where SC-specific logic lives -- never in the system
    # prompt itself, which stays universal.
    try:
        from prompts import load_criterion_prompt
        from functions.prompt import get_off_scope_keywords
        _tmpl = load_criterion_prompt(criterion_id) or {}
        _anti = _tmpl.get("auditor_anti_patterns", [])
        if _anti and "AUDITOR ANTI-PATTERNS" not in criterion_guidance:
            criterion_guidance += (
                "\n\nAUDITOR ANTI-PATTERNS (do NOT report these false positives):\n"
                + "\n".join(f"  - {a}" for a in _anti)
            )
        # off_scope_topics: prefer per-JSON field if present, fall back
        # to the central _OFF_SCOPE_KEYWORDS map until every JSON has been
        # migrated. Both sources are SC-specific and belong in the user
        # prompt via criterion_guidance, not in the system prompt.
        _off_scope = _tmpl.get("off_scope_topics") or get_off_scope_keywords(criterion_id)
        if _off_scope and "OFF-SCOPE TOPICS" not in criterion_guidance:
            criterion_guidance += (
                f"\n\nOFF-SCOPE TOPICS FOR {criterion_id} (REJECT findings about these -- they belong to a different SC):\n"
                + "\n".join(f"  - {t}" for t in _off_scope)
            )
    except Exception:
        logger.debug("off-scope topics load failed", exc_info=True)

    # JUDGE_SYSTEM_PROMPT is self-contained and already includes the
    # SELECTOR EVIDENCE and PER-CRITERION RULES ARE LAW directives. Do
    # not append rule constants here -- doing so produces duplicated
    # sections in the final prompt sent to the model.
    system_prompt = JUDGE_SYSTEM_PROMPT
    if product_context and hasattr(product_context, "to_prompt"):
        ctx = product_context.to_prompt()
        if ctx:
            system_prompt += f"\n\n{ctx}"

    # Layer 3 semantic retrieval (top-K cached patterns most similar to
    # this SC). Results append to dom_context as an extra grounding
    # block so the batching logic counts them in its budget and every
    # batch sees the retrieved code. Failures are non-fatal: when
    # embeddings are missing or the retrieval raises we proceed with
    # the original dom_context.
    retrieved_block = ""
    if code_findings and code_findings_embeddings:
        try:
            from functions.sc_retrieval import (
                format_retrieved_patterns,
                retrieve_for_sc,
            )
            retrieved = await retrieve_for_sc(
                criterion_id=criterion_id,
                criterion_name=criterion_name,
                criterion_guidance=criterion_guidance,
                code_findings=code_findings,
                pattern_embeddings=code_findings_embeddings,
            )
            retrieved_block = format_retrieved_patterns(retrieved, criterion_id)
        except Exception as exc:
            logger.warning(
                "Judge: Layer 3 retrieval failed for SC %s (%s) -- "
                "continuing without retrieved code evidence",
                criterion_id, exc,
            )
            try:
                from functions.bypass_log import (
                    CATEGORY_SKIPPED_DATA, SEVERITY_HIGH, log_bypass,
                )
                log_bypass(
                    category=CATEGORY_SKIPPED_DATA,
                    severity=SEVERITY_HIGH,
                    source="analysis/judge.py:judge_criterion",
                    event="layer3_retrieval_exception",
                    details={
                        "criterion_id": criterion_id,
                        "exception_type": type(exc).__name__,
                        "exception": str(exc),
                    },
                    outcome="judge runs without retrieved code evidence; may under-detect patterns Phase 1 mis-tagged",
                    data_lost=True,
                )
            except Exception:
                logger.debug("bypass_log emission failed", exc_info=True)
            retrieved_block = ""
    if retrieved_block:
        dom_context = (
            (dom_context + "\n\n" + retrieved_block)
            if dom_context else retrieved_block
        )

    # Chunk findings if the combined prompt would overflow the judge's
    # reliable output window. All findings across all batches are kept;
    # no data is dropped. Worst conformance across batches wins.
    batches = _batch_findings_for_prompt_budget(
        criterion_id=criterion_id,
        criterion_name=criterion_name,
        level=level,
        normative_text=normative_text,
        source_verdicts=source_verdicts,
        all_findings=all_findings,
        criterion_guidance=criterion_guidance,
        dom_context=dom_context,
        system_chars=len(system_prompt),
    )

    if len(batches) == 1:
        return await _call_judge(
            system_prompt=system_prompt,
            user_prompt=batches[0],
            images=images,
        )

    # Multi-batch merge. Each batch gets its own call; results merge.
    logger.info(
        "Judge: SC %s chunked into %d batches (%d findings total)",
        criterion_id, len(batches), len(all_findings),
    )
    merged_final: list[dict] = []
    merged_rejected: list[dict] = []
    reasonings: list[str] = []
    summaries: list[str] = []
    worst_conf: ConformanceLevel | None = None
    min_confidence = 1.0

    for batch_idx, batch_prompt in enumerate(batches):
        batch_header = (
            f"JUDGE BATCH {batch_idx + 1} OF {len(batches)}\n"
            f"The findings for this criterion are split across multiple "
            f"judge calls because of prompt size. Evaluate the findings "
            f"shown below ONLY. Other batches are judged separately; the "
            f"orchestrator will merge the final_findings and rejected_findings "
            f"into a single ACR entry. Do not speculate about findings "
            f"not shown here.\n\n"
        )
        # Send images on every batch — splitting findings across batches
        # doesn't change the visual evidence the judge needs to verify
        # claims, and a "batch 2" call without images would silently
        # regress the multimodal verification this fix introduces.
        result = await _call_judge(
            system_prompt=system_prompt,
            user_prompt=batch_header + batch_prompt,
            images=images,
        )
        if not result:
            continue
        merged_final.extend(result.get("final_findings", []) or [])
        merged_rejected.extend(result.get("rejected_findings", []) or [])
        if result.get("reasoning"):
            reasonings.append(f"[batch {batch_idx + 1}] {result['reasoning']}")
        if result.get("vpat_summary"):
            summaries.append(result["vpat_summary"])
        batch_conf = result.get("conformance_level")
        if isinstance(batch_conf, ConformanceLevel):
            worst_conf = _worse_conformance(worst_conf, batch_conf)
        min_confidence = min(min_confidence, float(result.get("confidence", 1.0)))

    if worst_conf is None:
        return None

    # Cross-source dedup. The fuzzy version (selector + issue-prefix
    # match) failed on ASU's SC 1.1.1 -- 3 sources flagged the same
    # hero image with 3 different selector paths and 3 different
    # wordings. Replaced with a focused LLM call that decides the
    # grouping semantically; merging is then mechanical. Failure
    # leaves findings unchanged so a flaky LLM call doesn't lose data.
    deduped = await _llm_dedupe_findings(merged_final, criterion_id)

    return {
        "conformance_level": worst_conf,
        "confidence": min_confidence,
        "reasoning": " ".join(reasonings),
        "final_findings": deduped,
        "rejected_findings": merged_rejected,
        "vpat_summary": " ".join(summaries).strip(),
    }


# ── Internal helpers ─────────────────────────────────────────────────────────

def _load_criterion_guidance(criterion_id: str) -> str:
    """Load conceptual scaffolding + pass/fail/NA from the SC JSON.

    Order of sections in the output is deliberate: the model reads
    plain meaning + scope boundaries FIRST so it has the rule's intent
    in mind before it starts matching conditions.
    """
    try:
        from prompts import load_criterion_prompt
        template = load_criterion_prompt(criterion_id)
        if not template:
            return ""

        parts = []

        # Conceptual scaffolding (new in 2026-04-15 refactor). Renders
        # before pass/fail so the model understands WHAT the rule means
        # and WHAT it covers before evaluating conditions.
        plain_meaning = template.get("plain_meaning")
        if plain_meaning:
            parts.append(f"PLAIN MEANING\n{plain_meaning}")
        scope_in = template.get("scope_applies_to", [])
        if scope_in:
            parts.append(
                "APPLIES TO (this criterion covers):\n"
                + "\n".join(f"  - {s}" for s in scope_in)
            )
        scope_out = template.get("scope_does_not_apply_to", [])
        if scope_out:
            parts.append(
                "DOES NOT APPLY TO (out of scope for this criterion):\n"
                + "\n".join(f"  - {s}" for s in scope_out)
            )
        borderline = template.get("borderline_calibration", [])
        if borderline:
            parts.append(
                "BORDERLINE CALIBRATION (how strict to be):\n"
                + "\n".join(f"  - {b}" for b in borderline)
            )

        for key, label in [
            ("pass_conditions", "SUPPORTS (passes)"),
            ("fail_conditions", "DOES NOT SUPPORT (fails)"),
            ("na_conditions", "NOT APPLICABLE"),
        ]:
            conditions = template.get(key, [])
            if conditions:
                parts.append(f"This criterion {label} when:\n" +
                             "\n".join(f"  - {c}" for c in conditions))

        src_guide = template.get("source_guidance", "")
        if src_guide:
            parts.append(f"SOURCE EXPERTISE FOR THIS CRITERION:\n{src_guide}")

        return "\n\n".join(parts)
    except Exception:
        return ""


def _build_judge_user_prompt(
    criterion_id: str,
    criterion_name: str,
    level: str,
    normative_text: str,
    source_verdicts: dict[str, dict],
    findings_slice: list[dict],
    total_findings: int,
    finding_offset: int,
    criterion_guidance: str,
    dom_context: str,
) -> str:
    """Build the user prompt for the judge with all evidence."""
    sections = [
        f"CRITERION: {criterion_id} -- {criterion_name} (Level {level})\n"
        f"Normative text: {normative_text}",
    ]

    if criterion_guidance:
        sections.append(f"CRITERION GUIDANCE\n{criterion_guidance}")

    # Source verdicts
    verdict_lines = ["SOURCE VERDICTS"]
    for source, verdict in source_verdicts.items():
        conf = verdict.get("conformance", "Not Evaluated")
        confidence = verdict.get("confidence", 0)
        count = verdict.get("findings_count", 0)
        verdict_lines.append(f"  {source}: {conf} (confidence={confidence:.2f}, findings={count})")
    sections.append("\n".join(verdict_lines))

    # All findings in this batch
    if findings_slice:
        label = (
            f"ALL FINDINGS ({len(findings_slice)} shown; full criterion total = {total_findings})"
            if len(findings_slice) != total_findings
            else f"ALL FINDINGS ({total_findings} total)"
        )
        finding_lines = [label]
        for local_i, f in enumerate(findings_slice):
            global_i = finding_offset + local_i
            source = f.get("source", "?")
            severity = f.get("severity", "?")
            element = f.get("element", "?")
            issue = f.get("issue", "?")
            css = f.get("css_selector", "")
            finding_lines.append(
                f"  [{global_i}] ({source}, {severity}) {element}"
                + (f" [{css}]" if css else "")
                + f"\n      Issue: {issue}"
            )
        sections.append("\n".join(finding_lines))
    else:
        sections.append("ALL FINDINGS: None")

    # DOM context for fact-checking
    if dom_context:
        sections.append(f"[VERIFIED DOM FACTS]\n{dom_context}")

    sections.append("Make your judgment. Call the report_judgment tool.")
    return "\n\n".join(sections)


def _batch_findings_for_prompt_budget(
    *,
    criterion_id: str,
    criterion_name: str,
    level: str,
    normative_text: str,
    source_verdicts: dict[str, dict],
    all_findings: list[dict],
    criterion_guidance: str,
    dom_context: str,
    system_chars: int,
) -> list[str]:
    """Split the judge user prompt into batches when it would overflow.

    Always returns at least one user prompt. When the total would fit in
    ``_JUDGE_PROMPT_CHUNK_CHARS`` minus the system prompt, returns one
    prompt containing every finding. Otherwise splits ``all_findings``
    greedily so each batch's user prompt stays under budget. No finding
    is dropped; each appears in exactly one batch.
    """
    # Fixed overhead per batch (criterion header + guidance + verdicts +
    # dom_context + tail prompt). Compute by calling the builder once
    # with an empty findings slice.
    overhead_prompt = _build_judge_user_prompt(
        criterion_id, criterion_name, level, normative_text,
        source_verdicts, [], len(all_findings), 0,
        criterion_guidance, dom_context,
    )
    overhead_chars = len(overhead_prompt)

    budget = _JUDGE_PROMPT_CHUNK_CHARS - system_chars

    # Single-batch happy path
    full_prompt = _build_judge_user_prompt(
        criterion_id, criterion_name, level, normative_text,
        source_verdicts, all_findings, len(all_findings), 0,
        criterion_guidance, dom_context,
    )
    if len(full_prompt) <= budget:
        return [full_prompt]

    # Compute per-finding-bytes budget after fixed overhead
    per_batch_budget = max(1, budget - overhead_chars)

    # Greedy pack findings into batches respecting per_batch_budget
    batches_idx: list[list[int]] = []
    current: list[int] = []
    current_len = 0
    for i, f in enumerate(all_findings):
        finding_chars = _finding_chars(f, i)
        if current and current_len + finding_chars > per_batch_budget:
            batches_idx.append(current)
            current = []
            current_len = 0
        current.append(i)
        current_len += finding_chars
    if current:
        batches_idx.append(current)

    if not batches_idx:
        return [full_prompt]

    return [
        _build_judge_user_prompt(
            criterion_id, criterion_name, level, normative_text,
            source_verdicts,
            [all_findings[i] for i in idx_list],
            len(all_findings),
            idx_list[0] if idx_list else 0,
            criterion_guidance,
            dom_context,
        )
        for idx_list in batches_idx
    ]


def _finding_chars(f: dict, index: int) -> int:
    """Approximate the rendered char length of one finding entry."""
    return (
        len(str(f.get("source", "?")))
        + len(str(f.get("severity", "?")))
        + len(str(f.get("element", "?")))
        + len(str(f.get("issue", "?")))
        + len(str(f.get("css_selector", "")))
        + len(str(index))
        + 40  # structural overhead
    )


async def _call_judge(
    *,
    system_prompt: str,
    user_prompt: str,
    images: list[str] | None = None,
) -> dict[str, Any] | None:
    """Issue a single judge call via LLMClient.call_with_tools and return
    the normalized result dict, or None if the call failed.

    Uses the universal 3-attempt retry + prose-restructure cascade, so
    a malformed Gemma reply recovers instead of being silently dropped.

    When ``images`` is non-empty, the judge call goes multimodal:
    ``LLMClient._select_model`` routes image-bearing calls to
    ``AI_LOCAL_JUDGE_MODEL`` (per CLAUDE.md "Accuracy over speed"), so the
    judge sees the same pixels visual_ai saw and can independently verify
    its findings instead of trusting visual_ai's prose blindly.
    """
    try:
        client = LLMClient(
            base_url=AI_JUDGE_API_URL,
            model=AI_JUDGE_MODEL,
            api_key=AI_JUDGE_API_KEY,
        )
        payload = await client.call_with_tools(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tool_name="report_judgment",
            tool_schema=JUDGE_TOOL,
            temperature=0.2,
            images=images or None,
        )
    except LLMError as exc:
        logger.warning("Judge LLM call failed: %s", exc)
        return None
    except Exception:
        logger.exception("Judge call raised an unexpected error (code bug, not a model failure)")
        return None

    if payload is None:
        return None

    return _normalize_judge_payload(payload)


def _normalize_judge_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce a parsed judge tool-call into our internal shape.

    Handles the "judge returned Not Evaluated" override by inferring the
    verdict from the severity distribution of final_findings. Returns
    final_findings as dicts (not Finding objects) so they serialize
    cleanly and are consumed uniformly by base.py.
    """
    try:
        conformance = normalize_conformance_level(
            str(payload.get("conformance_level", "Supports"))
        )
    except ValueError:
        conformance = ConformanceLevel.SUPPORTS

    # Judge should NEVER return Not Evaluated — override based on findings
    if conformance == ConformanceLevel.NOT_EVALUATED:
        final_raw = payload.get("final_findings", [])
        has_high = any(f.get("severity") == "high" for f in final_raw if isinstance(f, dict))
        has_med = any(f.get("severity") == "medium" for f in final_raw if isinstance(f, dict))
        if has_high:
            conformance = ConformanceLevel.DOES_NOT_SUPPORT
        elif has_med:
            conformance = ConformanceLevel.PARTIALLY_SUPPORTS
        elif final_raw:
            conformance = ConformanceLevel.PARTIALLY_SUPPORTS
        else:
            conformance = ConformanceLevel.SUPPORTS
        logger.info("Judge returned Not Evaluated — overridden to %s based on %d findings",
                     conformance.value, len(final_raw))

    confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.5))))

    final_findings: list[dict] = []
    for f in payload.get("final_findings", []):
        if isinstance(f, dict):
            css = str(f.get("css_selector", ""))
            # Defensive: strip invalid CSS syntax that the model occasionally
            # emits (jQuery :contains(), XPath //, :has-text). The css_selector
            # field is meant to be a real querySelector-compatible string;
            # leaving invalid syntax in misleads developers and breaks any
            # downstream tooling that tries to look the element up. The
            # human-readable element field is kept as-is.
            if css and (
                ":contains(" in css
                or ":has-text(" in css.lower()
                or css.startswith("//")
                or " //" in css
            ):
                logger.warning(
                    "Judge produced invalid CSS selector %r -- blanking field "
                    "(element description retained)", css,
                )
                css = ""
            # Carry the structured cited_measurements through verbatim.
            # The post-judge claim validator (checks/base.py ->
            # functions/claim_validator.py) reads this field to verify
            # every measured value against the captured ground truth.
            # Dropping it here -- which the prior field whitelist did --
            # silently disabled the validator (verified on run
            # 20260515_214502_8f27bb59 SC 1.4.10 / 1.4.11: the judge
            # emitted cited_measurements correctly but it never reached
            # the validator).
            cited = f.get("cited_measurements", [])
            if not isinstance(cited, list):
                cited = []
            final_findings.append({
                "element": str(f.get("element", "")),
                "issue": str(f.get("issue", "")),
                "impact": str(f.get("impact", "")),
                "recommendation": str(f.get("recommendation", "")),
                "severity": normalize_severity(f.get("severity", "medium")).value,
                "source": str(f.get("source", "judge")),
                "css_selector": css,
                "cited_measurements": cited,
            })

    # NOTE: cross-source dedup intentionally NOT done here. This path
    # runs PER BATCH; the LLM-based dedup at the end of judge_criterion
    # operates on the union of all batches, which is the only level at
    # which dedup can correctly catch duplicates that span batches. We
    # do still call the cheap fuzzy merge as a last-resort within-batch
    # exact-match cleanup so obvious one-batch duplicates don't bloat
    # the prompt to the dedup LLM call.
    final_findings = _merge_cross_source_findings(final_findings)

    return {
        "conformance_level": conformance,
        "confidence": confidence,
        "reasoning": str(payload.get("reasoning", "")),
        "final_findings": final_findings,
        "rejected_findings": payload.get("rejected_findings", []),
        "vpat_summary": str(payload.get("vpat_summary", "")),
    }


_CONFORMANCE_ORDER: dict[ConformanceLevel, int] = {
    ConformanceLevel.NOT_APPLICABLE: -1,
    ConformanceLevel.NOT_EVALUATED: -1,
    ConformanceLevel.SUPPORTS: 0,
    ConformanceLevel.PARTIALLY_SUPPORTS: 1,
    ConformanceLevel.DOES_NOT_SUPPORT: 2,
}


def _merge_cross_source_findings(findings: list[dict]) -> list[dict]:
    """Legacy fuzzy dedup -- kept for tests / non-judge call sites.

    Dedup that should ship to the report goes through
    ``_llm_dedupe_findings`` (called from the judge after batch
    merging). This function remains a cheap last-resort that catches
    findings with IDENTICAL css_selector + issue prefix; it should
    never be relied on for cross-source merging.
    """
    if len(findings) <= 1:
        return findings

    severity_rank = {"high": 3, "medium": 2, "low": 1, "info": 0}

    def _key(f: dict) -> tuple[str, str]:
        sel = (f.get("css_selector") or "").strip().lower()
        issue = (f.get("issue") or "").strip().lower()
        shape = " ".join(issue.split())
        return (sel, shape)

    grouped: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for f in findings:
        # Skip entries that lack both selector and issue -- they cannot
        # be meaningfully grouped, keep as-is.
        sel = (f.get("css_selector") or "").strip()
        issue = (f.get("issue") or "").strip()
        if not sel and not issue:
            order.append(("__novel__", str(id(f))))
            grouped[order[-1]] = dict(f)
            continue

        key = _key(f)
        if key not in grouped:
            order.append(key)
            grouped[key] = dict(f)
            grouped[key]["_sources_set"] = set()
            grouped[key]["_sources_set"].add(str(f.get("source", "judge")))
            continue

        # Merge: pick worse severity, union sources
        prev = grouped[key]
        prev_rank = severity_rank.get(str(prev.get("severity", "medium")).lower(), 2)
        new_rank = severity_rank.get(str(f.get("severity", "medium")).lower(), 2)
        if new_rank > prev_rank:
            prev["severity"] = f.get("severity", prev.get("severity"))
        prev["_sources_set"].add(str(f.get("source", "judge")))

    merged: list[dict] = []
    for key in order:
        entry = grouped[key]
        if "_sources_set" in entry:
            sources = sorted(entry.pop("_sources_set"))
            entry["source"] = ", ".join(sources) if len(sources) > 1 else sources[0]
        merged.append(entry)
    return merged


def _worse_conformance(
    a: ConformanceLevel | None, b: ConformanceLevel,
) -> ConformanceLevel:
    """Pick the worse (more severe) of two conformance levels."""
    if a is None:
        return b
    return a if _CONFORMANCE_ORDER.get(a, 0) >= _CONFORMANCE_ORDER.get(b, 0) else b


# ── VPAT Synthesis mode ─────────────────────────────────────────────────────

async def _judge_vpat_synthesis(
    criterion_id: str,
    criterion_name: str,
    level: str,
    normative_text: str,
    source_verdicts: dict[str, dict],
    all_findings: list[dict],
    wcag_version: str,
    dom_context: str,
    product_context: Any,
    criterion_guidance: str,
) -> dict[str, Any] | None:
    """Lightweight judge for programmatic-definitive criteria.

    Accepts all findings as truth. Only rewrites in VPAT language.
    Does NOT override the programmatic conformance verdict.
    """
    prog = source_verdicts.get("Programmatic", {})
    prog_conformance = prog.get("conformance", "Not Evaluated")

    system_prompt = (
        "You are a senior WCAG accessibility auditor producing findings for a "
        "Section 508 Accessibility Conformance Report (VPAT 2.5).\n\n"
        "MODE: VPAT SYNTHESIS\n"
        "Most findings below were VERIFIED by deterministic programmatic "
        "analysis (duplicate-ID detection, frame-rate math, attribute "
        "checks). Treat those as ground truth and rewrite them in VPAT "
        "language without questioning the measurement.\n\n"
        "EXCEPTION — ADVISORY findings: any finding whose issue text begins "
        "with '[ADVISORY' (e.g. '[ADVISORY — HTML_CodeSniffer manual-check "
        "reminder, NOT a detected violation]' or '[ADVISORY — IBM Equal "
        "Access manual-check needed, NOT a definitive violation]') is a "
        "manual-check reminder, NOT a measured defect. You MUST NOT rewrite "
        "an [ADVISORY] finding as a concrete violation. If the only inputs "
        "for this criterion are ADVISORY, the correct output is zero "
        "final_findings (or one info-severity 'manual review recommended' "
        "finding). Do NOT escalate ADVISORY items into definitive failures "
        "— that contradicts the source tools' own classification.\n\n"
        "Your job is to rewrite the non-advisory findings in VPAT "
        "language.\n\n"
        "FINDING REQUIREMENTS (every final_finding needs all of these fields)\n"
        "  - element: spatial description a human can find on the page.\n"
        "  - css_selector: copy VERBATIM from the source finding. Do NOT "
        "invent or simplify. If the source finding has '#UA_BrandBar_SearchBtn', "
        "the output must contain that exact string.\n"
        "  - issue: what is wrong, citing the WCAG clause and the measured "
        "value from the programmatic check.\n"
        "  - impact: which disability group and which assistive technology is "
        "affected, and how.\n"
        "  - recommendation: the WCAG conformance requirement that is unmet. "
        "Do NOT include code, CSS, or HTML fixes.\n"
        "  - severity: high (blocks access), medium (significant barrier with "
        "workaround), low (minor inconvenience), info (best-practice note). "
        "Severity is contextual: a missing alt on a page-central hero image "
        "is high, on a small decorative footer icon is low.\n\n"
        f"CONFORMANCE: Set conformance_level to EXACTLY '{prog_conformance}'. "
        "Do not change it -- the programmatic verdict is mathematically "
        "definitive for this criterion.\n\n"
        "VPAT SUMMARY: Write a 1-3 sentence vpat_summary describing the "
        "finding pattern in professional ACR language (no code, no fixes). "
        "Call the report_judgment tool exactly once."
    )
    # Load per-SC guidance (anti-patterns, off-scope topics) from the
    # criterion JSON and append to criterion_guidance so it reaches the
    # user prompt. The VPAT-synthesis system prompt above is self-
    # contained and does NOT need universal rule blocks appended.
    from functions.prompt import get_off_scope_keywords
    try:
        from prompts import load_criterion_prompt
        _tmpl = load_criterion_prompt(criterion_id) or {}
        _anti = _tmpl.get("auditor_anti_patterns", [])
        if _anti and "AUDITOR ANTI-PATTERNS" not in (criterion_guidance or ""):
            criterion_guidance = (criterion_guidance or "") + (
                "\n\nAUDITOR ANTI-PATTERNS (do NOT report these false positives):\n"
                + "\n".join(f"  - {a}" for a in _anti)
            )
        _off_scope = _tmpl.get("off_scope_topics") or get_off_scope_keywords(criterion_id)
        if _off_scope and "OFF-SCOPE TOPICS" not in (criterion_guidance or ""):
            criterion_guidance = (criterion_guidance or "") + (
                f"\n\nOFF-SCOPE TOPICS FOR {criterion_id} (REJECT findings about these -- they belong to a different SC):\n"
                + "\n".join(f"  - {t}" for t in _off_scope)
            )
    except Exception:
        logger.debug("off-scope topics load failed", exc_info=True)
    if criterion_guidance:
        system_prompt += f"\n\nCRITERION GUIDANCE:\n{criterion_guidance}"
    if product_context and hasattr(product_context, "to_prompt"):
        ctx = product_context.to_prompt()
        if ctx:
            system_prompt += f"\n\n{ctx}"

    # Format findings. VPAT synthesis has no selector-invention concern
    # because we're just rewriting prose; chunk the findings list by
    # size the same way the normal judge does.
    user_prompt_shell = (
        f"CRITERION: {criterion_id} {criterion_name} (Level {level})\n"
        f"REQUIREMENT: {normative_text}\n"
        f"VERDICT: {prog_conformance} ({prog.get('confidence', 0):.0%})\n\n"
    )
    tail = "\n\nRewrite all findings in VPAT language. Call report_judgment."

    def _render_findings(findings: list[dict]) -> str:
        out = f"FINDINGS ({len(findings)}):\n"
        for f in findings:
            out += (
                f"\n  Element: {f.get('element', '')}\n"
                f"    css_selector: {f.get('css_selector', '')}\n"
                f"    Issue: {f.get('issue', '')}\n"
                f"    Severity: {f.get('severity', 'medium')}\n"
            )
            if f.get("evidence"):
                out += f"    Evidence: {f['evidence']}\n"
        return out

    # Batch by char budget
    budget = _JUDGE_PROMPT_CHUNK_CHARS - len(system_prompt) - len(user_prompt_shell) - len(tail)
    batches: list[list[dict]] = []
    cur: list[dict] = []
    cur_len = 0
    for f in all_findings:
        entry_chars = _finding_chars(f, 0)
        if cur and cur_len + entry_chars > budget:
            batches.append(cur)
            cur = []
            cur_len = 0
        cur.append(f)
        cur_len += entry_chars
    if cur:
        batches.append(cur)
    if not batches:
        batches = [[]]

    # Severity ceiling for the fast path. The deterministic input is
    # ground truth; the judge is here to rewrite prose, not to invent
    # severity. If every input finding is INFO, the output cannot
    # legitimately escalate to MEDIUM or HIGH because the judge has no
    # new evidence -- it cannot "see" anything the deterministic check
    # didn't already measure. Past failure: SC 1.4.3 fast path consumed
    # 59 INFO inputs and emitted 3 HIGH outputs citing 1.23:1 ratios
    # (the judge re-quoted unreliable fallback ratios from the prompt
    # at escalated severity). Capping by max-input-severity prevents
    # that regardless of what the model writes.
    _SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}
    _RANK_TO_SEVERITY = {0: "info", 1: "low", 2: "medium", 3: "high"}
    max_input_rank = 0
    for f in all_findings:
        s = (f.get("severity") or "info").lower()
        max_input_rank = max(max_input_rank, _SEVERITY_RANK.get(s, 0))
    severity_ceiling = _RANK_TO_SEVERITY[max_input_rank]

    merged_final: list[dict] = []
    summaries: list[str] = []
    for batch in batches:
        user_prompt = user_prompt_shell + _render_findings(batch) + tail
        result = await _call_judge(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        if not result:
            continue
        merged_final.extend(result.get("final_findings", []) or [])
        if result.get("vpat_summary"):
            summaries.append(result["vpat_summary"])

    if not merged_final and not summaries:
        return None

    # Apply severity ceiling. Any output finding whose severity exceeds
    # the worst input severity gets clamped down with a note.
    capped_count = 0
    for ff in merged_final:
        if not isinstance(ff, dict):
            continue
        out_sev = (ff.get("severity") or "info").lower()
        out_rank = _SEVERITY_RANK.get(out_sev, 0)
        if out_rank > max_input_rank:
            ff["severity"] = severity_ceiling
            capped_count += 1
    if capped_count:
        logger.info(
            "Judge VPAT synthesis: clamped %d finding(s) to max-input "
            "severity '%s' (judge cannot escalate beyond what the "
            "deterministic input established)",
            capped_count, severity_ceiling,
        )

    return {
        "conformance_level": prog_conformance,
        "confidence": prog.get("confidence", 0.9),
        "reasoning": f"VPAT synthesis across {len(batches)} batch(es)",
        "final_findings": merged_final,
        "rejected_findings": [],
        "vpat_summary": " ".join(summaries).strip(),
    }
