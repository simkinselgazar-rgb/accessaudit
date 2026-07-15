"""Prompt builders for the WCAG test pipeline.

All prompt construction lives here. Check files and capture phases import
from this module instead of writing their own prompt glue.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

from models import CaptureData


# ── Criterion -> relevant CaptureData fields ─────────────────────────────────

_CRITERION_ELEMENT_MAP: dict[str, list[str]] = {
    # 1.1.1 Non-text Content
    "1.1.1": ["images", "background_images", "media", "captchas", "iframes"],
    # 1.2.x Time-based media
    "1.2.1": ["media", "transcript_buttons", "transcript_verifications"],
    "1.2.2": ["media", "transcript_buttons", "transcript_verifications"],
    "1.2.3": ["media", "transcript_buttons", "transcript_verifications"],
    "1.2.4": ["media"],
    "1.2.5": ["media"],
    # 1.3.x Info and Relationships
    "1.3.1": ["headings", "landmarks", "tables", "lists", "form_fields", "pseudo_elements", "aria_issues"],
    "1.3.2": ["headings", "landmarks", "tables", "lists", "tab_walk"],
    "1.3.3": ["form_fields", "links", "images"],
    "1.3.4": ["viewport_meta"],
    "1.3.5": ["form_fields"],
    # 1.4.x Distinguishable
    "1.4.1": ["images", "links", "colors"],
    "1.4.2": ["media"],
    "1.4.3": ["colors", "computed_styles", "pixel_contrast"],
    "1.4.4": ["viewport_meta", "overflow_200pct"],
    "1.4.5": ["images", "pseudo_elements"],
    "1.4.10": ["overflow_200pct", "overflow_320px", "viewport_meta"],
    "1.4.11": ["colors", "form_fields", "focus_indicators", "nontext_contrast", "focus_contrast"],
    "1.4.12": ["text_spacing_overflow"],
    "1.4.13": ["hover_content"],
    # 2.1.x Keyboard
    "2.1.1": ["tab_walk", "tab_coverage", "focus_indicators", "links", "form_fields", "widget_keyboard"],
    "2.1.2": ["keyboard_traps"],
    "2.1.4": ["tab_walk", "form_fields"],
    # 2.2.x Enough Time
    "2.2.1": ["media", "context_changes", "dynamic_content"],
    "2.2.2": ["media", "context_changes", "dynamic_content"],
    # 2.3.x Seizures
    "2.3.1": ["flash_analysis", "dynamic_content"],
    "2.3.3": ["reduced_motion", "dynamic_content"],
    # 2.4.x Navigable
    "2.4.1": ["skip_links", "skip_link_results", "iframes"],
    "2.4.2": [],
    "2.4.3": ["tab_walk", "tab_coverage", "focus_indicators"],
    "2.4.4": ["links"],
    "2.4.5": ["links", "landmarks"],
    "2.4.6": ["headings", "form_fields"],
    "2.4.7": ["focus_indicators", "tab_walk", "focus_contrast"],
    "2.4.11": ["focus_indicators", "tab_walk"],
    # 2.5.x Input Modalities
    "2.5.1": ["form_fields"],
    "2.5.2": ["form_fields"],
    "2.5.3": ["form_fields"],
    "2.5.4": ["form_fields"],
    "2.5.7": ["form_fields"],
    "2.5.8": ["form_fields"],
    # 3.1.x Readable
    "3.1.1": ["page_language"],
    "3.1.2": ["page_language"],
    # 3.2.x Predictable
    "3.2.1": ["form_fields", "context_changes"],
    "3.2.2": ["form_fields", "context_changes"],
    "3.2.3": ["landmarks", "links"],
    "3.2.4": ["links"],
    "3.2.6": ["links", "form_fields"],
    # 3.3.x Input Assistance
    "3.3.1": ["form_fields", "form_errors"],
    "3.3.2": ["form_fields"],
    "3.3.3": ["form_fields", "form_errors"],
    "3.3.4": ["form_fields"],
    "3.3.7": ["form_fields"],
    "3.3.8": ["form_fields"],
    # 4.1.x Compatible
    "4.1.2": ["form_fields", "links", "iframes", "ai_removed_elements", "aria_issues"],
    "4.1.3": ["form_fields", "form_errors", "context_changes"],
}

_DEFAULT_ELEMENTS = ["headings", "images", "links", "form_fields"]


# ── Off-scope keywords per SC ────────────────────────────────────────────────
# Adjacent criteria that models routinely confuse. Listed tokens get injected
# into the system prompt's OFF-SCOPE TOPICS block so the model is explicitly
# told to ignore them for THIS SC. Reduces scope-bleed false positives where
# a 2.2.2 verdict contained a 2.3.3 finding, a 2.4.3 verdict contained a
# 2.1.1 finding, etc.
_OFF_SCOPE_KEYWORDS: dict[str, list[str]] = {
    # 1.1.x
    "1.1.1": [
        "video captions", "audio description", "video transcript",
        "synchronized media",
    ],
    # 1.2.x time-based media -- cross-contaminate each other
    "1.2.1": ["missing alt text", "form labels", "heading structure"],
    "1.2.2": ["missing alt text", "form labels", "heading structure"],
    "1.2.3": ["missing alt text", "form labels", "heading structure"],
    # 1.3.x info & relationships -- most commonly bleed captions and audio
    "1.3.1": [
        "video captions", "audio description", "color contrast",
        "focus visible", "focus indicator",
    ],
    "1.3.2": [
        "video captions", "audio description", "color contrast",
        "focus visible",
    ],
    "1.3.3": ["color contrast", "focus visible", "form labels"],
    "1.3.4": ["color contrast", "focus visible"],
    "1.3.5": ["color contrast"],
    # 1.4.x distinguishable
    "1.4.1": ["focus visible", "focus indicator", "color contrast ratio"],
    "1.4.2": [
        "video captions", "audio description", "animation",
        "prefers-reduced-motion",
    ],
    "1.4.3": ["focus visible", "focus indicator"],
    "1.4.4": ["color contrast", "focus visible"],
    "1.4.5": ["color contrast"],
    "1.4.10": ["color contrast", "focus visible"],
    "1.4.11": ["text contrast ratio 4.5:1", "focus visible"],
    "1.4.12": ["color contrast", "focus visible"],
    "1.4.13": ["color contrast"],
    # 2.1.x keyboard
    "2.1.1": ["focus visible", "focus indicator", "focus order"],
    "2.1.2": ["focus visible", "focus order", "keyboard shortcut"],
    "2.1.4": ["focus visible", "focus order"],
    # 2.2.x enough time
    "2.2.1": [
        "video captions", "audio description", "animation",
        "prefers-reduced-motion", "keyboard access", "focus visible",
    ],
    "2.2.2": [
        # Most commonly confused with 2.3.3
        "prefers-reduced-motion", "CSS animation", "@keyframes",
        "scroll-linked animation", "parallax",
        # And with 2.1.1 (keyboard)
        "keyboard accessibility of pause button",
        "keyboard accessible controls",
        # And with 1.4.2 (audio control)
        "audio control mechanism",
    ],
    # 2.3.x seizures
    "2.3.1": ["autoplay audio", "autoplay video", "captions"],
    "2.3.3": ["autoplay audio", "autoplay video"],
    # 2.4.x navigable
    "2.4.1": ["focus visible", "color contrast"],
    "2.4.2": ["focus visible", "color contrast"],
    "2.4.3": [
        # 2.4.3 is ORDER, not reachability
        "unreachable via keyboard", "keyboard trap",
        "missing focus indicator",
    ],
    "2.4.4": ["focus visible", "color contrast"],
    "2.4.5": ["focus visible"],
    "2.4.6": ["focus visible", "color contrast"],
    "2.4.7": [
        # 2.4.7 is VISIBILITY, not color contrast of the indicator -- that's 1.4.11
        "contrast ratio of focus indicator",
        "keyboard trap", "focus order",
    ],
    "2.4.11": ["keyboard trap", "focus order"],
    # 2.5.x input modalities
    "2.5.1": ["keyboard access", "focus visible"],
    "2.5.2": ["focus visible"],
    "2.5.3": ["color contrast"],
    # 3.x understandable
    "3.1.1": ["color contrast", "focus visible"],
    "3.1.2": ["color contrast", "focus visible"],
    "3.2.1": ["color contrast", "focus visible"],
    "3.2.2": ["color contrast", "focus visible"],
    "3.2.3": ["color contrast", "focus visible"],
    "3.2.4": ["color contrast", "focus visible"],
    "3.3.1": ["color contrast", "focus visible"],
    "3.3.2": ["color contrast", "focus visible"],
    "3.3.3": ["color contrast", "focus visible"],
    "3.3.4": ["color contrast", "focus visible"],
    # 4.1.x compatible
    "4.1.1": ["color contrast", "focus visible", "missing alt text"],
    "4.1.2": ["color contrast", "focus visible"],
    "4.1.3": ["color contrast", "focus visible", "focus order"],
}


def get_off_scope_keywords(criterion_id: str) -> list[str]:
    """Return the off-scope keyword list for a criterion.

    Used by ``build_system_prompt`` to auto-populate the OFF-SCOPE TOPICS
    block without callers having to pass ``off_scope_keywords`` explicitly.
    """
    return list(_OFF_SCOPE_KEYWORDS.get(criterion_id, []))


# ── Universal element-evaluation rules ──────────────────────────────────────
# Applied to every SC's visual/code/judge system prompt. These are truly
# universal -- they describe when an element is EXEMPT from "missing
# accessible name / alt / label" findings. Without them, models flag every
# FontAwesome / Bootstrap icon (aria-hidden SVGs), every SVG with a
# <title> element, and every icon link with a parent aria-label -- all
# false positives, because those elements already have accessible names
# or are hidden from the accessibility tree by design.
#
# Rule 1: element is hidden from AT -> no content rules apply at all.
# Rule 2: element already has a W3C-standard accessible name -> do NOT
#         flag it for "missing name / missing alt / missing label".
HIDDEN_FROM_AT_RULE = (
    "ELEMENT EVALUATION RULES (UNIVERSAL, APPLY TO EVERY SC)\n"
    "\n"
    "RULE 1 -- HIDDEN FROM ASSISTIVE TECH.\n"
    "Do NOT report findings against elements that are intentionally removed "
    "from the accessibility tree. An element is removed from the tree (and "
    "therefore EXEMPT from WCAG content rules like alt text, accessible name, "
    "label, role) when ANY of the following are true:\n"
    "  - It has aria-hidden=\"true\" on itself or any ancestor.\n"
    "  - It has the hidden attribute, display:none, or visibility:hidden "
    "(on itself or any ancestor).\n"
    "  - It is inside a <template> tag.\n"
    "  - It is an SVG/icon marked aria-hidden=\"true\" (the standard "
    "FontAwesome and Bootstrap icon pattern). These intentionally have "
    "role=\"img\" for legacy reasons but are hidden from AT on purpose -- "
    "they are decorative twins of adjacent text labels and must NOT be "
    "flagged for missing aria-label or alt.\n"
    "  - It is an <input type=\"hidden\">.\n"
    "Before flagging an element, mentally check: \"Would a screen reader "
    "actually announce this element?\" If no, do not flag it.\n"
    "\n"
    "RULE 2 -- ELEMENT ALREADY HAS AN ACCESSIBLE NAME.\n"
    "Do NOT report \"missing accessible name / missing alt / missing label\" "
    "when the element ALREADY has a name via ANY of these W3C-standard "
    "mechanisms. A name from ANY of these counts as a valid accessible name:\n"
    "  - The element has a non-empty ``alt`` attribute (for <img>, <area>, "
    "<input type=\"image\">).\n"
    "  - The element has a non-empty ``aria-label`` attribute.\n"
    "  - The element has ``aria-labelledby`` pointing to one or more elements "
    "whose text content forms the name.\n"
    "  - The element is an inline <svg> containing a child <title> element. "
    "The <title> text IS the accessible name -- screen readers announce it. "
    "Pattern: ``<svg role=\"img\"><title>Alert</title>...</svg>`` and "
    "``<svg role=\"img\" aria-labelledby=\"Icon-foo-:R1:\"><title "
    "id=\"Icon-foo-:R1:\">Close dialog</title>...</svg>`` are BOTH compliant.\n"
    "  - The element is a <button>, <a>, or other interactive element whose "
    "visible text content (or the text content of its descendants) provides "
    "the name.\n"
    "  - The element is an icon inside a <button> or <a> that already has an "
    "accessible name (alt, aria-label, or text content). The parent's name "
    "covers the child icon -- do NOT flag both.\n"
    "  - For <input> and <select>, the element has a <label for=\"...\"> "
    "pointing to it, or is wrapped in a <label>, or has aria-labelledby "
    "pointing to a label-bearing element.\n"
    "  - The element has a ``title`` attribute (weakest form but still a "
    "name per ARIA 1.2 accessible-name computation).\n"
    "If you find yourself typing \"X has no accessible name\" in a finding, "
    "STOP and walk through every mechanism in this list against the actual "
    "DOM shown in the user prompt. If any mechanism applies, the element "
    "has a name -- drop the finding. A low-quality name (e.g. alt=\"image\" "
    "or aria-label=\"search-button\") is still a NAME for the purposes of "
    "\"missing name\" rules -- flag it under label-quality criteria instead, "
    "not under \"missing alt\" or \"missing accessible name\".\n"
    "\n"
    "RULE 3 -- REPORTING EXEMPT ELEMENTS DESTROYS REPORT CREDIBILITY. "
    "Every false positive in this class makes the auditor look incompetent. "
    "Do NOT guess. If you cannot positively confirm from the DOM that an "
    "element both (a) is in the accessibility tree AND (b) has no accessible "
    "name via ANY of the Rule 2 mechanisms, drop the finding."
)


# ── Universal "SC rules are law" directive ─────────────────────────────────
# Added to every system prompt. Tells the model to treat the SC-specific
# pass_conditions / fail_conditions / na_conditions / auditor_anti_patterns
# / off_scope_topics shown in the user prompt as authoritative for this
# evaluation, overriding its general training. Forces Flash Lite and
# similar to give the per-SC rules the weight they need.
SC_RULES_ARE_LAW = (
    "PER-CRITERION RULES ARE LAW\n"
    "The user prompt contains a CRITERION GUIDANCE block with the rules "
    "for the specific WCAG Success Criterion under test. It lists when the "
    "criterion PASSES, when it FAILS, when it is NOT APPLICABLE, and a set "
    "of AUDITOR ANTI-PATTERNS and OFF-SCOPE TOPICS specific to this SC. "
    "TREAT THOSE RULES AS LAW for this evaluation:\n"
    "  - A finding is only valid if it matches one of the CRITERION "
    "GUIDANCE fail conditions. If it doesn't match any fail condition, "
    "drop it.\n"
    "  - If a finding matches an AUDITOR ANTI-PATTERN, drop it -- those "
    "are explicitly documented false positives.\n"
    "  - If a finding matches an OFF-SCOPE TOPIC, drop it and name the "
    "correct criterion in the rejection reason -- those belong to a "
    "different SC.\n"
    "  - When your general training conflicts with the CRITERION GUIDANCE, "
    "defer to the CRITERION GUIDANCE. The guidance was written by a "
    "certified Trusted Tester for this exact SC on this exact codebase.\n"
    "Ignoring the per-SC rules produces the hallucinated, scope-bled, "
    "false-positive findings that destroy audit report credibility."
)


# ── Prompt template loader (per-criterion JSON) ──────────────────────────────

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_criterion_prompt(criterion_id: str) -> dict | None:
    """Load the per-criterion prompt template from ``prompts/<id>.json``.

    Returns a dict with keys like ``pass_conditions``, ``fail_conditions``,
    ``na_conditions``, ``examples``, or None if no file exists.
    """
    filename = criterion_id.replace(".", "_") + ".json"
    filepath = _PROMPTS_DIR / filename
    if filepath.exists():
        return json.loads(filepath.read_text(encoding="utf-8"))
    return None


# ── System prompt for the visual/code AI ─────────────────────────────────────

def build_system_prompt(
    criterion_id: str,
    criterion_name: str,
    level: str,
    normative_text: str,
    ict_baseline: str = "",
    off_scope_keywords: list[str] | None = None,
    wcag_version: str = "2.2",
    page_context_hint: str = "",
    product_context: Any = None,
) -> str:
    """Return the system prompt for a single WCAG criterion evaluation.

    The system prompt is **minimal and universal**. It states the role,
    the criterion under test, what a finding must contain for an ACR,
    the response format, the SELECTOR EVIDENCE rule, and a directive
    that the per-criterion rules in the user prompt are LAW.

    Everything SC-specific -- pass/fail conditions, auditor anti-patterns,
    off-scope topics, accessible-name exemptions, etc. -- lives in
    ``prompts/<id>.json`` and renders into the USER prompt via
    CRITERION GUIDANCE. This keeps the system prompt stable across every
    call and makes SC behavior a JSON edit, not a code edit.

    Deprecated parameters kept for caller compatibility: ``off_scope_keywords``,
    ``ict_baseline``, ``page_context_hint`` -- all now flow through the user
    prompt's CRITERION GUIDANCE or PAGE CONTEXT blocks, not here.
    """
    # product_context may carry a tiny identifier line; append to role.
    product_line = ""
    if product_context and hasattr(product_context, "to_prompt"):
        ctx_text = product_context.to_prompt()
        if ctx_text:
            product_line = f"\nProduct under test: {ctx_text}"

    return textwrap.dedent(f"""\
        <role>
        You are a WCAG {wcag_version} Level {level} accessibility auditor
        producing findings for a VPAT 2.5 Section 508 Accessibility
        Conformance Report (ACR).{product_line}
        </role>

        <criterion_under_test>
          <id>{criterion_id}</id>
          <name>{criterion_name}</name>
          <level>{level}</level>
          <normative_text>{normative_text}</normative_text>
        </criterion_under_test>

        <task>
        Evaluate the page ONLY against WCAG {criterion_id}. Work through
        the input in this order, then call the report_wcag_assessment tool
        exactly once:

          1. Read the CRITERION GUIDANCE block in the user prompt to
             internalise pass_conditions, fail_conditions,
             auditor_anti_patterns, and off_scope_topics for this SC.
          2. Walk the captured ground-truth blocks (VERIFIED DOM FACTS,
             ANDI CONTRAST, ANDI LANG, ANDI HIDDEN, ANDI GRAPHICS, ANDI
             TABLES, ANDI INTERACTIVE, axe results, tab_walk, etc.).
             These are deterministic — they win over any visual hunch.
          3. Identify candidate findings that match a fail_condition AND
             agree with the captured data.
          4. Drop candidates that match an auditor_anti_pattern, an
             off_scope_topic, or that are exempt under ELEMENT
             EVALUATION RULES (hidden from AT, or already has an
             accessible name via ARIA name computation).
          5. Set conformance_level. If you cannot evaluate at all
             (data conflicts, missing capture), use Not Evaluated and
             fill conflicting_information / insufficient_evidence_reason.
        </task>

        <rules>
        <per_criterion_rules_are_law>
        The CRITERION GUIDANCE block in the user prompt is law for this
        evaluation:
          - A finding is valid ONLY if it matches a fail_condition.
          - Match against an auditor_anti_pattern → DROP.
          - Match against an off_scope_topic → DROP. Do not redirect to
            the other SC; the other SC has its own evaluation pass.
          - When your general training conflicts with the guidance, obey
            the guidance.
        </per_criterion_rules_are_law>

        <finding_requirements>
        Every finding needs all of these fields. Adopt a "Human Expert" tone — be direct, 
        factual, and descriptive.
          - element: a real CSS selector copied from the source, OR a
            precise spatial description (e.g. "the third link in the top
            navigation bar").
          - issue: WHAT is wrong. Use plain language (e.g. "The search button 
            is invisible to screen readers because it is an icon with no text").
            Always cite the specific WCAG {criterion_id} requirement unmet.
          - impact: WHO is affected and HOW. Be specific about the disability 
            group and assistive technology (e.g. "A blind user using NVDA 
            will hear 'button' but won't know this button opens the main menu").
          - recommendation: The WCAG conformance requirement unmet, stated 
            as what "Success" looks like. Keep this focused on the USER 
            experience. (e.g. "Interactive controls must provide a clear 
            visual and programmatic state"). 
          - severity: high (blocks access) | medium (significant barrier) | 
            low (minor) | info (best-practice).
          - internal_remediation_note: MANDATORY technical fix for the 
            development team. Provide the exact HTML/CSS/ARIA change needed 
            to resolve the issue (e.g. "Add aria-expanded='false' to the 
            button and toggle it to 'true' via JS on click"). This field 
            is internal-only and will not be shown to clients.
        </finding_requirements>

        <selector_evidence>
        Every css_selector, class name, or ID in a finding MUST appear
        verbatim in the HTML, programmatic data, or ground-truth blocks
        in the user prompt. Do not invent class names or IDs. If you
        cannot copy the selector, describe the element by its surrounding
        context instead.
        </selector_evidence>

        <valid_css_syntax>
        css_selector MUST be valid CSS (parseable by
        document.querySelector).
        BANNED:
          - jQuery pseudo-classes: :contains(), :has-text(), :visible
          - XPath expressions: //div[contains(text(), "...")]
          - Compound text predicates: a:contains("Read")
        ALLOWED text-containment patterns:
          - [aria-label="exact text"]
          - [aria-labelledby="id-of-label"]
          - [title="exact text"]
          - Combine with element selectors:
            button[aria-label="Apply Now"]
        If the element is identified by visible text and you cannot find
        a valid attribute selector, use its id or a positional selector
        (e.g. nav > ul > li:nth-of-type(3) > a) copied from the source.
        Findings with invalid CSS will be rejected by the judge.
        </valid_css_syntax>

        <evidence_grounding>
        Every numerical or string-literal claim in a finding MUST come
        from the captured data in the user prompt. A wrong number
        misleads developers about what to fix.

          - Contrast ratios → use values from pixel_contrast,
            computed_styles, focus_contrast, or ANDI CONTRAST. Never
            estimate from a screenshot. Captured 21:1 means do NOT write
            "1.00:1".
          - CSS values (class names, inline styles, custom properties
            like --grid-column-count, computed widths) → quote verbatim
            from dom.html or computed_styles. If dom.html shows
            style="--grid-column-count:1", do not report
            "--grid-column-count:2".
          - Keyboard reachability: before claiming "not reachable by
            keyboard", search [TAB ORDER] / tab_walk. If the selector
            appears, the element WAS reached.
          - SC 2.1.1 / 2.4.3 violation buckets (hard rule): the ONLY
            elements that qualify as "not keyboard operable" are those
            in tab_coverage.focusable_but_skipped or
            tab_coverage.not_focusable_at_all. Do NOT compute your own
            "not reached" count by subtracting tab_walk length from
            total_interactive. Do NOT cite
            tab_coverage.roving_tabindex_valid or
            tab_coverage.custom_arrow_navigable — those are correctly
            reachable via arrow keys through a tab-focusable sibling.
            If both buckets are empty, there is NO SC 2.1.1 violation
            from the tab walk, regardless of how many DOM elements you
            see. Phrases like "N of M are not reachable" where you
            derived N yourself are hallucinations — delete them.
          - Counts and percentages: use exact values
            (tab_coverage.coverage_percent, axe violation counts, etc.).
            Do not derive your own ratio.
          - Accessible name computation: before flagging "missing name",
            apply the ARIA 1.2 cascade in order:
              1. aria-labelledby (resolve and concatenate targets)
              2. aria-label
              3. native host language (label[for], button text, link
                 text, th headers, legend)
              4. title attribute
              5. for elements wrapping an <img>: the wrapped img's alt
                 attribute provides the wrapper's accessible name
            Only flag if EVERY step yields nothing.
          - Element location phrasing (mandatory): every element in
            IMAGES, HEADINGS, LINKS, FORM FIELDS, IFRAMES carries a
            LOCATION: line. Paraphrase from the LOCATION line in the
            element + issue fields, NEVER from the structural selector.
            Write "the image under the section heading inside the
            <main> landmark", not "the ninth section". Structural paths
            belong only in css_selector. If an element has no LOCATION
            line, say "unlabelled" rather than invent a position count.

        If you have visual evidence of a real issue but the precise
        measurement is not in the captured data, report it with
        severity=info and describe what you observe. An info-flagged
        true positive beats a fabricated precise number AND beats
        dropping the finding.
        </evidence_grounding>

        <data_conflicts_and_insufficient_evidence>
        The user prompt has programmatic data AND screenshots. Always
        trust programmatic data over your visual interpretation —
        programmatic data is deterministic ground truth.

        If programmatic data and visual observations CONFLICT:
          - Set conformance_level to "Not Evaluated".
          - Fill conflicting_information with EXACTLY what conflicts
            (e.g. "The element inventory lists alt='...' on this image
            but I see no image content in the screenshot").
          - Do NOT guess or hallucinate findings.

        If you lack sufficient evidence (e.g. screenshots didn't
        capture the right state, no programmatic data for this SC):
          - Set conformance_level to "Not Evaluated".
          - Fill insufficient_evidence_reason with the specific missing
            data.
          - Do NOT fabricate findings to fill a gap.
        </data_conflicts_and_insufficient_evidence>
        </rules>

        <example>
        <scenario>Compliant link wrapping an alt-bearing image</scenario>
        <input_excerpt>
        IMAGES:
          - <a href="/"><img alt="The University"></a>
            LOCATION: header navigation, "Home" link
        </input_excerpt>
        <wrong_output>
        {{ "issue": "Link has no accessible name", "severity": "high" }}
        </wrong_output>
        <correct_output>
        Drop this finding entirely. The wrapping <a> inherits the
        accessible name "The University" from its child <img alt="...">
        per ARIA 1.2 step 5. The element is COMPLIANT for SC 4.1.2 /
        2.4.4. Do not emit a finding for it.
        </correct_output>
        </example>

        <example>
        <scenario>Numerical claim contradicted by ground truth</scenario>
        <input_excerpt>
        ANDI CONTRAST: p.lead ratio=21.00 required=4.5 passes=True
                       fg=rgb(0,0,0) bg=rgb(255,255,255)
        Visual observation: dark text on white background looks fine.
        </input_excerpt>
        <wrong_output>
        {{ "issue": "Insufficient contrast 1.00:1", "severity": "high" }}
        </wrong_output>
        <correct_output>
        No finding emitted. The captured ratio is 21:1 (passes 4.5:1
        threshold). Visual judgment must defer to the deterministic
        ANDI / pixel_contrast measurement.
        </correct_output>
        </example>

        <output_format>
        Call the report_wcag_assessment tool exactly once. No prose, no
        markdown, no commentary outside the tool call. If you find no
        issues, set conformance_level to "Supports" and briefly state
        why in the summary.
        </output_format>
    """).strip()


# ── User prompt ──────────────────────────────────────────────────────────────

def build_user_prompt(
    page_context: dict[str, Any],
    programmatic_data: dict[str, Any] | None = None,
    elements: list[dict[str, Any]] | None = None,
    user_context: str | None = None,
    a11y_tree_summary: str | None = None,
) -> str:
    """Return the user-level prompt with page data for the AI."""

    sections: list[str] = []

    url = page_context.get("url", "(unknown)")
    title = page_context.get("title", "(untitled)")
    file_type = page_context.get("file_type")

    review_type = page_context.get("review_type", "single")
    ctx_header = f"PAGE UNDER TEST\n- URL: {url}\n- Title: {title}"
    if file_type:
        ctx_header += f"\n- File type: {file_type}"
    if review_type == "single":
        ctx_header += (
            "\n- Review scope: SINGLE PAGE ONLY — no other pages from this "
            "site were tested. The following cross-page criteria cannot be "
            "evaluated from a single page and should be marked Not Applicable, "
            "with a summary that says so explicitly (e.g. \"This criterion "
            "requires comparison across multiple pages; only one page was "
            "tested, so this criterion is Not Applicable for the present "
            "review.\"). Do NOT write summaries that claim consistency was "
            "evaluated \"across the page\" or imply cross-page verification "
            "was performed — that phrasing misleads the auditor:\n"
            "  * 3.2.3 Consistent Navigation\n"
            "  * 3.2.4 Consistent Identification\n"
            "  * 3.2.5 Change on Request (when about cross-page consistency)\n"
            "  * 3.2.6 Consistent Help (WCAG 2.2)\n"
            "  * 3.3.7 Redundant Entry (when about multi-step process across pages)"
        )
    sections.append(ctx_header)

    if programmatic_data:
        prog_lines = ["[PROGRAMMATIC DATA]"]
        prog_lines.append(
            "The following results were obtained by deterministic automated "
            "checks. They accurately report what EXISTS in the code, but "
            "cannot judge semantic quality or meaning. You should still "
            "report issues about content quality even if programmatic "
            "checks passed."
        )

        conformance = programmatic_data.get("conformance_level")
        if conformance:
            prog_lines.append(f"- Programmatic conformance: {conformance}")

        confidence = programmatic_data.get("confidence")
        if confidence is not None:
            prog_lines.append(f"- Programmatic confidence: {confidence}")

        findings = programmatic_data.get("findings", [])
        if findings:
            prog_lines.append(f"- Findings ({len(findings)}):")
            for i, f in enumerate(findings, 1):
                element = f.get("element", "?")
                issue = f.get("issue", "?")
                severity = f.get("severity", "?")
                location = f.get("location", "")
                prog_lines.append(f"  {i}. [{severity}] {element} -- {issue}")
                if location:
                    # Indent two extra spaces so the judge can scan
                    # findings vertically and see location alongside.
                    prog_lines.append(f"     Location: {location}")
            prog_lines.append(
                "- SEVERITY SEMANTICS: [high] = confirmed WCAG failure. "
                "[medium] = likely failure pending review. [low] = minor "
                "concern. [info] = MANUAL-REVIEW NOTICE only -- the upstream "
                "tool (HTML_CodeSniffer / IBM Equal Access / etc.) flagged "
                "this for human inspection but did NOT assert a violation. "
                "Treat [info] findings as evidence the page contains "
                "patterns that warrant a closer look, NOT as a basis for a "
                "Does Not Support / Partially Supports verdict on their "
                "own. A verdict worse than Supports requires at least one "
                "[low], [medium], or [high] finding."
            )
        else:
            prog_lines.append("- No programmatic findings.")

        sections.append("\n".join(prog_lines))

    if elements:
        elem_lines = [
            f"RELEVANT ELEMENTS ({len(elements)} items)",
            "(NOTE: The leading number on each line is a list index, NOT the "
            "element's position in the source document. Items are grouped by "
            "category — headings first, then landmarks, then interactive "
            "controls — so a high index does NOT mean 'late in the DOM'. "
            "Do not infer source-document order or visual position from this "
            "numbering. For DOM-vs-visual ordering questions, rely on the "
            "[PROGRAMMATIC DATA] block above, which carries the deterministic "
            "landmark-order analysis when relevant.)",
        ]
        for i, elem in enumerate(elements, 1):
            tag = elem.get("tag", elem.get("selector", "(unknown)"))
            details = {
                k: v
                for k, v in elem.items()
                if k not in ("tag", "selector", "outerHTML", "_bb_label")
                and v not in (None, "", [], {})
            }
            detail_str = ", ".join(f"{k}={v}" for k, v in details.items())
            bb_label = elem.get("_bb_label")
            box_prefix = f"[Box {bb_label}] " if bb_label is not None else ""
            line = f"  {i}. {box_prefix}<{tag}>"
            if detail_str:
                line += f"  {detail_str}"
            outer = elem.get("outerHTML", "")
            if outer:
                line += f"\n     HTML: {outer}"
            elem_lines.append(line)
        sections.append("\n".join(elem_lines))

    if a11y_tree_summary:
        sections.append(
            "[ACCESSIBILITY TREE]\n"
            "The following is a summary of the browser's computed accessibility "
            "tree. This shows the actual ARIA roles, names, and states that "
            "assistive technologies will encounter.\n"
            + a11y_tree_summary
        )

    if user_context:
        sections.append(f"OPERATOR NOTES\n{user_context}")

    sections.append(
        "ANALYSIS REQUEST\n"
        "Carefully examine ALL attached screenshots and/or video alongside "
        "the data above. For each issue you find:\n"
        "  1. Identify the EXACT element and WHERE it is on the page\n"
        "  2. Explain WHAT is wrong in plain language (reference the WCAG requirement)\n"
        "  3. Explain WHO is affected and HOW (specific disability groups + assistive tech)\n"
        "  4. State the WCAG conformance requirement that is not met -- do NOT provide code or implementation fixes\n"
        "  5. Assign severity (high/medium/low/info)\n\n"
        "Your findings will appear in a Section 508 Accessibility Conformance "
        "Report (VPAT format). They must be clear enough for a developer to "
        "locate each issue without additional context.\n\n"
        "Call the report_wcag_assessment tool with your complete assessment."
    )

    return "\n\n".join(sections)


# ── Element selection ────────────────────────────────────────────────────────

def format_elements_for_prompt(
    capture_data: CaptureData,
    criterion_id: str,
) -> list[dict[str, Any]]:
    """Select the CaptureData fields relevant to a given criterion."""
    field_names = _CRITERION_ELEMENT_MAP.get(criterion_id, _DEFAULT_ELEMENTS)

    elements: list[dict[str, Any]] = []
    for name in field_names:
        value = getattr(capture_data, name, None)
        if value is None:
            continue

        if isinstance(value, dict):
            if value:
                elements.append({"tag": name, **value})
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    elements.append(item)
                else:
                    elements.append({"tag": name, "value": item})

    return elements


# ── Page context hint ────────────────────────────────────────────────────────

def build_page_context_hint(capture_data: CaptureData) -> str:
    """Describe the page's primary content profile for dynamic prompt enhancement."""
    parts: list[str] = []

    purpose = (capture_data.user_context or {}).get("page_purpose", "")
    if purpose:
        parts.append(f"Page purpose: {purpose}")

    n_forms = len(capture_data.form_fields)
    n_images = len(capture_data.images)
    n_media = len(capture_data.media)
    n_tables = len(capture_data.tables)
    n_links = len(capture_data.links)

    profile_parts: list[str] = []
    if n_forms > 5:
        profile_parts.append(f"form-heavy ({n_forms} fields)")
    elif n_forms > 0:
        profile_parts.append(f"{n_forms} form fields")

    if n_images > 10:
        profile_parts.append(f"image-heavy ({n_images} images)")
    elif n_images > 0:
        profile_parts.append(f"{n_images} images")

    if n_media > 0:
        profile_parts.append(f"{n_media} media elements")

    if n_tables > 3:
        profile_parts.append(f"data-heavy ({n_tables} tables)")
    elif n_tables > 0:
        profile_parts.append(f"{n_tables} tables")

    if n_links > 50:
        profile_parts.append(f"navigation-heavy ({n_links} links)")

    if profile_parts:
        parts.append("Content profile: " + ", ".join(profile_parts))

    if capture_data.file_type:
        parts.append(f"Document type: {capture_data.file_type}")

    return "\n".join(parts) if parts else ""


# ── Accessibility-tree summarizer ────────────────────────────────────────────

_ROLE_RELEVANCE: dict[str, set[str]] = {
    "1.1": {"img", "image", "figure", "graphics-document", "graphics-symbol"},
    "1.3": {
        "heading", "list", "listitem", "table", "row", "cell", "columnheader",
        "rowheader", "form", "landmark", "region", "navigation", "main",
        "complementary", "banner", "contentinfo", "search",
    },
    "2.1": {
        "button", "link", "textbox", "checkbox", "radio", "combobox",
        "slider", "spinbutton", "tab", "menuitem", "switch",
    },
    "2.4": {
        "heading", "link", "navigation", "main", "banner", "contentinfo",
        "search", "complementary", "region",
    },
    "3.3": {
        "textbox", "combobox", "listbox", "spinbutton", "checkbox", "radio",
        "switch", "alert", "status",
    },
    "4.1": {
        "button", "link", "textbox", "checkbox", "radio", "combobox",
        "slider", "spinbutton", "tab", "menuitem", "switch", "dialog",
        "alertdialog", "progressbar", "status",
    },
}


def summarize_a11y_tree(
    a11y_tree: dict,
    criterion_id: str,
) -> str:
    """Summarize the full accessibility tree for the AI, focusing on
    criterion-relevant roles. No truncation -- every relevant node is
    emitted. Callers that can't fit the result into one prompt must
    chunk it via ``functions.chunker.chunk_text``.
    """
    nodes = a11y_tree.get("nodes", [])
    if not nodes:
        return ""

    prefix = ".".join(criterion_id.split(".")[:2]) if "." in criterion_id else criterion_id
    relevant_roles = _ROLE_RELEVANCE.get(prefix, set())

    lines: list[str] = []
    for node in nodes:
        role = node.get("role", {}).get("value", "")
        name = node.get("name", {}).get("value", "")
        properties = node.get("properties", [])

        if relevant_roles and role not in relevant_roles and role not in {"generic", "none", "presentation"}:
            has_interesting = any(
                p.get("name") in ("required", "invalid", "disabled", "checked", "expanded", "selected")
                for p in properties
            )
            if not has_interesting:
                continue

        if role in ("generic", "none", "presentation", "LineBreak", "InlineTextBox"):
            continue

        prop_strs = []
        for p in properties:
            pname = p.get("name", "")
            pval = p.get("value", {})
            if isinstance(pval, dict):
                pval = pval.get("value", "")
            if pname in (
                "required", "invalid", "disabled", "checked", "expanded",
                "selected", "level", "describedby", "labelledby", "errormessage",
            ):
                prop_strs.append(f"{pname}={pval}")

        line = f"  [{role}]"
        if name:
            line += f' "{name}"'
        if prop_strs:
            line += f" ({', '.join(prop_strs)})"
        lines.append(line)

    return "\n".join(lines)
