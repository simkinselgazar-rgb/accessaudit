"""IBM Equal Access Accessibility Checker finding extraction.

Companion to ``functions/axe_extract.py`` and ``functions/htmlcs_extract.py``.
IBM EAC is Apache-2.0 licensed and contributes the strongest open-source
ARIA validity coverage: aria-controls/aria-owns reference resolution,
required-children-for-role checks, role-conflict detection, custom-
widget keyboard expectations.

Each result entry has ``ruleId`` (e.g. ``aria_role_redundant``) and
``value`` of [level, judgment] where judgment is one of:
  PASS / FAIL / POTENTIAL / MANUAL

We map FAIL → HIGH, POTENTIAL → MEDIUM (engine sees signal but cannot
confirm without context), MANUAL → INFO. PASS results are skipped --
they're confirmation that nothing is wrong.

Each rule maps to one or more WCAG SCs via the ``IBM_RULE_TO_SC`` table
below. The table covers IBM's most-flagged rules; rules not in the
table are emitted only when their ``message`` mentions the criterion
under test by id (a defensive fallback so new IBM rules still flow
through, just less precisely tagged).
"""
from __future__ import annotations

from models import CaptureData, Finding, Severity
from functions.finding_utils import _make_finding_id


# Rule-id → list of SC ids the rule is evidence for. Sourced from
# https://github.com/IBMa/equal-access/tree/master/accessibility-checker-engine/help-v4
# (the engine's documentation maps each rule to its WCAG criteria).
# Most rules tag a single SC; a few legitimately implicate multiple
# (e.g. missing form label is 1.3.1, 3.3.2, AND 4.1.2 simultaneously).
IBM_RULE_TO_SC: dict[str, list[str]] = {
    # 1.1.1 Non-text Content
    "img_alt_valid": ["1.1.1"],
    "img_alt_misuse": ["1.1.1"],
    "img_alt_redundant": ["1.1.1"],
    "img_alt_decorative": ["1.1.1"],
    "img_alt_null": ["1.1.1"],
    "img_alt_background": ["1.1.1"],
    "object_text_exists": ["1.1.1"],
    "applet_alt_exists": ["1.1.1"],
    "area_alt_exists": ["1.1.1"],
    "input_image_alt": ["1.1.1"],
    "svg_graphics_labelled": ["1.1.1"],
    # 1.2.x Time-based media (mostly manual, IBM flags structural cues)
    "media_track_available": ["1.2.2", "1.2.3"],
    "media_alt_exists": ["1.2.1"],
    # 1.3.1 Info and Relationships
    "fieldset_legend_valid": ["1.3.1"],
    "form_label_unique": ["1.3.1"],
    "input_label_visible": ["1.3.1", "3.3.2"],
    "input_label_exists": ["1.3.1", "3.3.2", "4.1.2"],
    "input_label_after": ["1.3.1"],
    "input_label_before": ["1.3.1"],
    "label_ref_valid": ["1.3.1"],
    "table_caption_empty": ["1.3.1"],
    "table_caption_nested": ["1.3.1"],
    "table_headers_related": ["1.3.1"],
    "table_headers_ref_valid": ["1.3.1"],
    "table_layout_linearized": ["1.3.2"],
    "list_structure_proper": ["1.3.1"],
    "heading_content_exists": ["1.3.1", "2.4.6"],
    "heading_markup_misuse": ["1.3.1"],
    "heading_skip": ["1.3.1"],
    "frame_title_exists": ["2.4.1", "4.1.2"],
    "iframe_title_exists": ["2.4.1", "4.1.2"],
    "page_title_exists": ["2.4.2"],
    # 1.3.2 Meaningful Sequence
    "element_tabbable_visible": ["1.3.2", "2.4.3"],
    # 1.3.5 Identify Input Purpose
    "input_autocomplete_valid": ["1.3.5"],
    # 1.4.1 Use of Color
    "style_color_misuse": ["1.4.1"],
    # 1.4.3 Contrast (Minimum)
    "text_contrast_sufficient": ["1.4.3"],
    "element_textwithin_color_sufficient": ["1.4.3"],
    # 1.4.4 Resize Text
    "meta_viewport_zoom": ["1.4.4", "1.4.10"],
    # 1.4.10 Reflow
    "element_orientation_unlocked": ["1.3.4"],
    # 2.1.1 Keyboard
    "element_mouseevent_keyboard": ["2.1.1"],
    "script_onclick_misuse": ["2.1.1"],
    "script_focus_blur_review": ["2.1.1", "3.2.1"],
    # 2.1.2 No Keyboard Trap
    "keyboard_trap_present": ["2.1.2"],
    # 2.1.4 Character Key Shortcuts
    "key_shortcut_avoid": ["2.1.4"],
    # 2.4.1 Bypass Blocks
    "skip_main_exists": ["2.4.1"],
    "skip_main_described": ["2.4.1"],
    # 2.4.4 Link Purpose
    "a_text_purpose": ["2.4.4"],
    # 2.4.6 Headings and Labels
    "label_content_exists": ["2.4.6"],
    # 2.4.7 Focus Visible
    "style_focus_visible": ["2.4.7"],
    # 3.1.1 Language of Page
    "html_lang_exists": ["3.1.1"],
    "html_lang_valid": ["3.1.1"],
    # 3.1.2 Language of Parts
    "element_lang_valid": ["3.1.2"],
    # 3.2.2 On Input
    "select_options_grouped": ["3.2.2"],
    # 3.3.1 Error Identification (limited rules)
    "input_aria_invalid_valid": ["3.3.1", "4.1.2"],
    # 3.3.2 Labels or Instructions
    "form_submit_button_exists": ["3.3.2"],
    # 4.1.1 Parsing — duplicate ids
    "element_id_unique": ["4.1.1"],
    "html_id_unique": ["4.1.1"],
    # 4.1.2 Name, Role, Value (HUGE for ARIA -- IBM's strength)
    "aria_role_valid": ["4.1.2"],
    "aria_role_redundant": ["4.1.2"],
    "aria_attribute_valid": ["4.1.2"],
    "aria_attribute_required": ["4.1.2"],
    "aria_attribute_value_valid": ["4.1.2"],
    "aria_attribute_conflict": ["4.1.2"],
    "aria_attribute_deprecated": ["4.1.2"],
    "aria_role_required_owned": ["4.1.2"],
    "aria_owns_reference_valid": ["4.1.2"],
    "aria_controls_reference_valid": ["4.1.2"],
    "aria_describedby_reference_valid": ["4.1.2"],
    "aria_labelledby_reference_valid": ["4.1.2"],
    "aria_role_allowed": ["4.1.2"],
    "aria_widget_labelled": ["4.1.2"],
    "aria_application_labelled": ["4.1.2"],
    "combobox_design_valid": ["4.1.2"],
    "combobox_haspopup_valid": ["4.1.2"],
    "dialog_role_valid": ["4.1.2"],
    "menu_child_valid": ["4.1.2"],
    "list_children_valid": ["4.1.2", "1.3.1"],
    "tabpanel_aria_labelled": ["4.1.2"],
    # 4.1.3 Status Messages
    "aria_live_appropriate": ["4.1.3"],
}


_JUDGMENT_TO_SEVERITY = {
    "FAIL": Severity.HIGH,
    "POTENTIAL": Severity.MEDIUM,
    "MANUAL": Severity.INFO,
    "RECOMMENDATION": Severity.INFO,
}


# ── Per-rule impact prose (disability groups + AT) ───────────────────────
# VPAT 2.5 ACR convention: every finding's impact field must name specific
# disability groups AND specific assistive technologies. The judge rewrites
# prose for the report but it cannot invent AT/disability mappings the
# extractor didn't supply. Naming the AT here means the judge has the
# right vocabulary to preserve in its rewrite.
_RULE_IMPACT_PROSE: dict[str, str] = {
    # Image / non-text content
    "img_alt_valid": (
        "Blind and low-vision users on screen readers (JAWS, NVDA, "
        "VoiceOver) receive no description of this image and cannot "
        "perceive its informational content."
    ),
    "img_alt_misuse": (
        "Screen reader users on JAWS, NVDA, and VoiceOver hear "
        "redundant or misleading text alternatives that interfere with "
        "comprehension."
    ),
    "img_alt_decorative": (
        "Screen reader users may have decorative imagery announced "
        "incorrectly, or meaningful imagery hidden, depending on the "
        "specific misuse."
    ),
    "object_text_exists": (
        "Blind users on screen readers cannot access the embedded "
        "object's content because no text alternative is provided."
    ),
    "input_image_alt": (
        "Screen reader users on JAWS / NVDA / VoiceOver hear no name "
        "for this image-button and cannot determine its purpose."
    ),
    # Form labels
    "input_label_exists": (
        "Screen reader users on JAWS, NVDA, and VoiceOver cannot "
        "determine the purpose of this form control because it has no "
        "programmatic label. Voice control software (Dragon, Voice "
        "Control) cannot target it by visible name."
    ),
    "input_label_visible": (
        "Voice control users (Dragon, macOS / Windows Voice Control) "
        "cannot target this control because its programmatic name "
        "differs from its visible label. Screen reader users on "
        "JAWS / NVDA / VoiceOver hear a name that doesn't match what "
        "sighted users see."
    ),
    "label_ref_valid": (
        "Screen reader users hear no association between this control "
        "and its intended label because the label-for reference is "
        "broken."
    ),
    "fieldset_legend_valid": (
        "Screen reader users on JAWS, NVDA, and VoiceOver hear the "
        "individual controls but no group label, losing the "
        "relationship between related radios / checkboxes."
    ),
    # Headings + structure
    "heading_content_exists": (
        "Screen reader users navigating by headings (h key in JAWS / "
        "NVDA / VoiceOver) skip past or hear an empty announcement, "
        "losing structural context."
    ),
    "heading_skip": (
        "Screen reader users navigating by heading level lose the "
        "document outline; assistive technology relies on a logical "
        "hierarchy to convey relative importance."
    ),
    "frame_title_exists": (
        "Screen reader users on JAWS, NVDA, and VoiceOver hear no name "
        "for this frame and cannot decide whether to enter it."
    ),
    "iframe_title_exists": (
        "Screen reader users hear an unnamed iframe in their virtual "
        "buffer and cannot determine its content type before entering."
    ),
    "page_title_exists": (
        "Screen reader users hear an unnamed tab/window and cannot "
        "distinguish this page from others in their browsing session."
    ),
    # Tables + lists
    "table_caption_empty": (
        "Screen reader users on JAWS, NVDA, and VoiceOver hear no "
        "summary of the table's purpose, losing the high-level "
        "context that aids navigation through cells."
    ),
    "table_headers_related": (
        "Screen reader users navigating cell-by-cell hear data values "
        "without their column or row context, breaking comprehension."
    ),
    "list_children_valid": (
        "Screen reader users hear an incorrect count of list items, "
        "or list semantics are broken entirely, losing the grouping "
        "relationship."
    ),
    # Contrast
    "text_contrast_sufficient": (
        "Low-vision users and users with color blindness or contrast "
        "sensitivity cannot read text at the measured ratio. Users on "
        "screen magnifiers (ZoomText, Windows Magnifier) face the "
        "same legibility issue."
    ),
    "element_textwithin_color_sufficient": (
        "Low-vision users and users with contrast sensitivity cannot "
        "perceive text content embedded in this element."
    ),
    # ARIA
    "aria_role_valid": (
        "Screen reader users on JAWS, NVDA, and VoiceOver hear an "
        "incorrect or undefined role announcement, breaking the "
        "user's mental model of the widget."
    ),
    "aria_attribute_valid": (
        "Screen reader users hear inconsistent or missing state and "
        "property information about this widget, preventing them from "
        "understanding its current state."
    ),
    "aria_attribute_required": (
        "Screen reader users on JAWS / NVDA / VoiceOver receive an "
        "incomplete announcement because the widget is missing a "
        "required ARIA attribute (typically state or property)."
    ),
    "aria_owns_reference_valid": (
        "Screen reader users perceive a broken parent-child "
        "relationship; the widget's claimed children are not actually "
        "reachable through the accessibility tree."
    ),
    "aria_controls_reference_valid": (
        "Screen reader users hear a control claiming to manipulate "
        "another element that does not exist or is not exposed, "
        "breaking the trigger-target relationship."
    ),
    "aria_labelledby_reference_valid": (
        "Screen reader users on JAWS, NVDA, and VoiceOver hear an "
        "empty or wrong name for this element because its "
        "aria-labelledby points to a missing ID."
    ),
    "combobox_design_valid": (
        "Screen reader users navigating combobox widgets receive "
        "inconsistent ARIA state announcements, breaking expected "
        "AT keyboard interaction."
    ),
    "dialog_role_valid": (
        "Screen reader users on JAWS / NVDA / VoiceOver hear a "
        "dialog announced incorrectly or without proper boundary "
        "demarcation, leading to focus disorientation."
    ),
    # Keyboard / focus
    "element_mouseevent_keyboard": (
        "Keyboard-only users (motor-impaired users, screen reader "
        "users on JAWS / NVDA / VoiceOver, users using switches) "
        "cannot trigger this element because it has mouse handlers "
        "without keyboard equivalents."
    ),
    "script_onclick_misuse": (
        "Keyboard-only users and switch users cannot activate this "
        "control through the keyboard."
    ),
    "style_focus_visible": (
        "Sighted keyboard users (motor-impaired users, dexterity "
        "limitations) cannot tell which element currently has focus "
        "because the visible focus indicator is suppressed."
    ),
    # Language
    "html_lang_exists": (
        "Screen reader users on JAWS, NVDA, and VoiceOver receive "
        "incorrect pronunciation because the synthesizer cannot "
        "determine the document language."
    ),
    "html_lang_valid": (
        "Screen reader users on JAWS / NVDA / VoiceOver receive "
        "incorrect pronunciation because the document's lang attribute "
        "is not a valid BCP 47 tag."
    ),
    "element_lang_valid": (
        "Screen reader users on JAWS, NVDA, and VoiceOver receive "
        "incorrect pronunciation for this content because the lang "
        "attribute is invalid or missing."
    ),
    # Parsing
    "element_id_unique": (
        "Screen reader users on JAWS, NVDA, and VoiceOver may receive "
        "ambiguous targets for label-for / aria-* references when "
        "multiple elements share the same id."
    ),
    "html_id_unique": (
        "Screen reader users may experience broken label / "
        "description / control associations when duplicate IDs cause "
        "ambiguous accessibility tree references."
    ),
}


_GENERIC_AT_IMPACT = (
    "Users of assistive technology — screen reader users on JAWS / "
    "NVDA / VoiceOver, voice-control users on Dragon, and "
    "keyboard-only users — may receive incorrect or missing "
    "information for this element, preventing equivalent access."
)


def _judgment_of(result: dict) -> str:
    """Pull the judgment string from the [level, judgment] value array."""
    val = result.get("value") or []
    if isinstance(val, list) and val:
        return str(val[-1]).upper()
    return ""


def _scs_for_rule(rule_id: str, message: str, criterion_id: str) -> list[str]:
    """Return the SC ids this rule attaches to.

    Strategy:
    1. Use the curated ``IBM_RULE_TO_SC`` table when the rule is known.
    2. Otherwise, search the rule's message text for the criterion id
       under test (e.g. "WCAG 1.4.3" or "Success Criterion 1.4.3"). If
       it appears, attribute the finding to that criterion only. This
       is a defensive fallback for rules added by IBM after this table
       was last refreshed -- they still flow through, just attributed
       only when the message itself confirms the SC.
    """
    if rule_id in IBM_RULE_TO_SC:
        return IBM_RULE_TO_SC[rule_id]
    if criterion_id and criterion_id in (message or ""):
        return [criterion_id]
    return []


# ── Known false-positive patterns: rules to filter at extractor level ──
# Each entry has its own filter logic that consults other deterministic
# capture data to suppress findings the engine produces unreliably. The
# alternative (letting the judge filter them) doesn't work in fast-path
# mode where the judge is told to accept all deterministic findings as
# ground truth — the filter has to happen BEFORE findings reach the
# judge prompt.

_CONTRAST_RULES = frozenset({
    "text_contrast_sufficient",
    "element_textwithin_color_sufficient",
})


def _selector_likely_over_bg_image(
    selector: str, snippet: str, capture_data: CaptureData,
) -> bool:
    """Return True when an IBM EAC contrast finding's element likely
    sits over a background image / video / gradient where the engine's
    bg-color resolver falls back to an unreliable color (typically
    white, producing the famous 1.23:1 fallback ratio).

    Mirrors the existing ANDI handling: ``andi_contrast_results``
    already marks per-text-node entries with ``bg_image_present=True``
    when the bg-color walk hits a fallback. If ANY ANDI entry whose
    selector overlaps the IBM EAC finding's selector / snippet is
    marked bg_image_present, the IBM EAC finding's ratio is equally
    unreliable and we filter it.

    Conservative: only filters when ANDI explicitly confirms the
    element is over a bg-image. If ANDI didn't sample the element
    (e.g. it's an SVG text node ANDI couldn't resolve), the IBM EAC
    finding is kept and the judge can evaluate.
    """
    andi = getattr(capture_data, "andi_contrast_results", None) or []
    if not andi:
        return False

    sel_lower = (selector or "").lower()
    snip_lower = (snippet or "").lower()

    for entry in andi:
        if not isinstance(entry, dict):
            continue
        if not entry.get("bg_image_present"):
            continue
        andi_sel = str(entry.get("selector") or "").lower()
        if not andi_sel:
            continue
        # Match if the ANDI selector appears in the IBM EAC selector
        # or snippet, OR vice versa. ANDI selectors are typically
        # short (.foo, #bar, p.lead); IBM EAC selectors are XPath-
        # style (/html[1]/body[1]/...). Selector-text overlap covers
        # both directions.
        andi_tail = andi_sel.split(">")[-1].strip()
        if andi_tail and (
            andi_tail in sel_lower or andi_tail in snip_lower
        ):
            return True
        # ANDI text-node text content sometimes appears in IBM EAC
        # snippets (engines pick up the same text). Use the text as
        # a corroborating signal.
        andi_text = str(entry.get("text") or "").strip()
        if andi_text and len(andi_text) >= 8 and andi_text.lower() in snip_lower:
            return True
    return False


def _focus_visible_corroborated(
    selector: str, capture_data: CaptureData,
) -> bool:
    """Return True when an IBM EAC ``style_focus_visible`` finding is
    corroborated by the deterministic byte-identical-screenshot probe.

    IBM EAC's ``style_focus_visible`` rule fires on any element with
    ``outline:none``, even when alternative indicators (border,
    box-shadow, background change) provide a clearly visible focus
    state. The byte-identical-screenshot probe in
    ``capture/interactive_capture.py:_capture_focus_indicators``
    actually compares before/after focus screenshots and reports
    ``has_change=False`` only when the rendered pixels are
    indistinguishable. We trust that measurement and require it to
    corroborate before accepting the IBM EAC claim.

    Conservative: when no focus_contrast data exists for the element
    (e.g. focus indicator capture didn't reach it), keep the IBM EAC
    finding — better to over-report than silently drop a real issue.
    """
    fc = getattr(capture_data, "focus_contrast", None) or []
    if not fc:
        # No deterministic focus data → keep IBM EAC finding so the
        # judge sees it and can evaluate against screenshots.
        return True

    sel_lower = (selector or "").lower()
    if not sel_lower:
        return True

    # Look for an entry whose selector overlaps. If found AND it
    # reports has_change=True, drop the IBM EAC claim — pixels say
    # focus IS visible.
    for entry in fc:
        if not isinstance(entry, dict):
            continue
        e_sel = str(entry.get("selector") or "").lower()
        if not e_sel:
            continue
        # Bidirectional overlap: IBM EAC uses XPath, focus_contrast
        # uses CSS selectors. Compare by tail-token containment.
        e_tail = e_sel.split(">")[-1].strip()
        if e_tail and e_tail in sel_lower:
            if entry.get("has_change") is True:
                # Pixels prove focus is visible -- IBM EAC false positive
                return False
            # has_change=False or None → IBM EAC finding stands
            return True
    # No matching focus_contrast entry → keep IBM EAC finding
    return True


def _impact_for_rule(rule_id: str) -> str:
    """Per-rule disability/AT-aware impact prose; falls back to a
    generic AT statement for rules not in the table.
    """
    return _RULE_IMPACT_PROSE.get(rule_id, _GENERIC_AT_IMPACT)


def extract_ibm_eac_findings(
    capture_data: CaptureData, criterion_id: str,
) -> list[Finding]:
    """Extract IBM Equal Access findings relevant to a given SC.

    Two filter passes happen at extractor level (BEFORE findings reach
    the judge prompt) for known false-positive patterns the engine
    cannot self-correct:

      1. ``text_contrast_sufficient`` / ``element_textwithin_color_sufficient``
         findings are dropped when ANDI's per-text-node walk has marked
         the element ``bg_image_present=True`` -- the IBM EAC contrast
         calculation falls back to white background and reports the
         spurious 1.23:1 ratio. Mirrors the existing ANDI bg-image
         filter pattern.

      2. ``style_focus_visible`` findings are dropped when the
         deterministic byte-identical-screenshot probe (focus_contrast)
         reports ``has_change=True`` for the element -- IBM EAC fires
         on ``outline:none`` even when alternative indicators
         (border, box-shadow, background change) provide a clearly
         visible focus state.

    Both filters are conservative: when corroborating data is missing
    (no ANDI sample, no focus_contrast entry), the IBM EAC finding is
    kept so the judge can evaluate against screenshots.

    Args:
        capture_data: holds ``ibm_eac_results`` dict from
            ``capture/web_capture.py:_capture_ibm_eac``. Soft-no-op when
            the capture didn't run.
        criterion_id: dotted WCAG SC id.

    Returns:
        List of Finding objects with source="ibm_eac".
    """
    if not getattr(capture_data, "ibm_eac_results", None):
        return []
    results = capture_data.ibm_eac_results.get("results") or []
    if not results:
        return []

    findings: list[Finding] = []
    dropped_bgimg = 0
    dropped_focus = 0
    for r in results:
        if not isinstance(r, dict):
            continue
        judgment = _judgment_of(r)
        # Skip PASS — that's affirmative evidence the rule did NOT
        # find an issue. Useful for confidence elsewhere; not a finding.
        if not judgment or judgment == "PASS":
            continue

        rule_id = str(r.get("ruleId") or "")
        message = str(r.get("message") or "")
        scs = _scs_for_rule(rule_id, message, criterion_id)
        if criterion_id not in scs:
            continue

        selector = str(r.get("path_dom") or "").strip()
        snippet = str(r.get("snippet") or "").strip()

        # Filter 1: contrast over background image (1.23:1 fallback)
        if rule_id in _CONTRAST_RULES:
            if _selector_likely_over_bg_image(selector, snippet, capture_data):
                dropped_bgimg += 1
                continue

        # Filter 2: focus-visible flagged but pixels prove visible
        if rule_id == "style_focus_visible":
            if not _focus_visible_corroborated(selector, capture_data):
                dropped_focus += 1
                continue

        severity = _JUDGMENT_TO_SEVERITY.get(judgment, Severity.INFO)
        help_text = str(r.get("help") or "").strip()

        element_desc = selector if selector else (
            snippet if snippet else "(unknown element)"
        )

        # An IBM EAC POTENTIAL judgment is a manual-check reminder — the
        # engine saw a signal but cannot definitively rule. Label it as
        # advisory so the judge cannot promote it into a concrete
        # violation claim (mirrors the htmlcs type-3 advisory marker).
        # Verified berkeley.edu SC 3.3.2: IBM POTENTIAL input_label_visible
        # was escalated into definitive "no programmatic label" findings
        # on an input that actually has <label for>, aria-label, and title.
        is_potential = str(judgment).upper() == "POTENTIAL"
        if is_potential:
            issue_text = (
                f"[ADVISORY — IBM Equal Access manual-check needed, NOT a "
                f"definitive violation] (rule={rule_id}): {message} "
                f"(WCAG {criterion_id})"
            )
            impact_text = (
                "This is a manual-check reminder, not a measured defect — "
                "the engine saw a signal it cannot definitively evaluate. "
                "Not evidence that a violation exists."
            )
            recommendation_text = (
                "Treat as an advisory note only. Do NOT escalate it into "
                "a concrete failure finding or assert a specific defect "
                "from it alone — the engine did not detect one."
            )
        else:
            issue_text = (
                f"IBM Equal Access {judgment.lower()} (rule={rule_id}): "
                f"{message} (WCAG {criterion_id})"
            )
            impact_text = _impact_for_rule(rule_id)
            recommendation_text = (
                f"WCAG {criterion_id} requires this be addressed. "
                + (help_text if help_text else
                   "Bring the flagged element into compliance with "
                   "the success criterion.")
            )

        findings.append(Finding(
            id=_make_finding_id(),
            element=element_desc,
            issue=issue_text,
            impact=impact_text,
            recommendation=recommendation_text,
            severity=severity,
            source="ibm_eac",
            css_selector=selector,
        ))

    if dropped_bgimg:
        import logging
        logging.getLogger(__name__).info(
            "IBM_EAC SC %s: dropped %d contrast finding(s) over background "
            "images (ANDI bg_image_present corroborates fallback ratio)",
            criterion_id, dropped_bgimg,
        )
    if dropped_focus:
        import logging
        logging.getLogger(__name__).info(
            "IBM_EAC SC %s: dropped %d focus-visible finding(s) where "
            "deterministic byte-identical screenshot probe shows "
            "has_change=True (focus IS visible)",
            criterion_id, dropped_focus,
        )

    return findings
