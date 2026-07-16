"""HTML_CodeSniffer (HCS) finding extraction.

Companion to ``functions/axe_extract.py``. HCS is Squiz Labs' BSD-3
WCAG conformance checker. Its rule engine is independent of axe, so
running both gives multi-tool corroboration: when HCS and axe both
flag a finding, the judge has very strong deterministic evidence.

HCS message codes encode the WCAG criterion. Examples:
  WCAG2AA.Principle1.Guideline1_1.1_1_1.H37  -> SC 1.1.1
  WCAG2AA.Principle1.Guideline1_3.1_3_1.H42  -> SC 1.3.1
  WCAG2AA.Principle2.Guideline2_4.2_4_4.H77  -> SC 2.4.4

The extractor parses each message's code, extracts the SC id from the
``X_Y_Z`` segment, and emits a Finding only when it matches the
criterion under test.

Severity mapping mirrors HCS's three-level scheme:
  type 1 (ERROR)   -> Severity.HIGH    (definite WCAG failure)
  type 2 (WARNING) -> Severity.MEDIUM  (likely failure, needs review)
  type 3 (NOTICE)  -> Severity.INFO    (informational / manual check)
"""
from __future__ import annotations

import re

from models import CaptureData, Finding, Severity
from functions.finding_utils import _make_finding_id


# HCS encodes the SC inside the message code as ``..._X_Y_Z.<technique>``.
# The integer triple is the criterion id with dots replaced by underscores.
# A few HCS rules apply to multiple SCs and have no embedded triple
# (general best-practice warnings). Those are skipped — they can't be
# attributed to a specific criterion deterministically.
_SC_FROM_CODE_RE = re.compile(r"\b(\d)_(\d{1,2})_(\d{1,2})\b")


def _sc_id_from_code(code: str) -> str:
    """Return the dotted SC id (e.g. "1.1.1") from an HCS message code,
    or empty string if no triple is embedded.
    """
    m = _SC_FROM_CODE_RE.search(code or "")
    if not m:
        return ""
    return ".".join(m.groups())


_TYPE_TO_SEVERITY = {
    1: Severity.HIGH,    # ERROR — HCS asserts a definite WCAG failure
    2: Severity.MEDIUM,  # WARNING — likely failure; HCS is uncertain
    3: Severity.INFO,    # NOTICE — informational, manual check needed
}


# ── Per-SC impact prose (disability groups + AT) ───────────────────────
# VPAT 2.5 ACR convention requires every finding's impact field to name
# specific disability groups AND specific assistive technologies. HCS
# message codes encode the SC, so we map by SC. The judge rewrites prose
# in the report but cannot invent AT/disability mappings the extractor
# didn't supply.
_SC_IMPACT_PROSE: dict[str, str] = {
    "1.1.1": (
        "Blind and low-vision users on screen readers (JAWS, NVDA, "
        "VoiceOver) cannot perceive the content because no text "
        "alternative is provided."
    ),
    "1.2.1": (
        "Deaf and hard-of-hearing users cannot access audio-only "
        "content; blind users cannot access video-only content."
    ),
    "1.2.2": (
        "Deaf and hard-of-hearing users cannot follow the audio track "
        "of synchronized media without captions."
    ),
    "1.2.3": (
        "Blind and low-vision users miss visual information that is "
        "not described in the audio track."
    ),
    "1.3.1": (
        "Screen reader users on JAWS, NVDA, and VoiceOver cannot "
        "perceive structural relationships (heading hierarchy, list "
        "grouping, table headers, form-control associations) that "
        "sighted users see visually."
    ),
    "1.3.2": (
        "Screen reader users follow content in the order programmatic "
        "structure defines, which differs here from the visual reading "
        "order."
    ),
    "1.3.3": (
        "Users with cognitive disabilities and screen-reader users "
        "cannot follow instructions that rely solely on shape, color, "
        "size, or position."
    ),
    "1.3.5": (
        "Users with cognitive disabilities and users on autofill-"
        "assistive software cannot rely on programmatic identification "
        "of common input fields."
    ),
    "1.4.1": (
        "Users with color blindness or low vision cannot distinguish "
        "information conveyed only by color."
    ),
    "1.4.3": (
        "Low-vision users and users with color blindness or contrast "
        "sensitivity cannot read text at the measured ratio. Users on "
        "screen magnifiers (ZoomText, Windows Magnifier) face the "
        "same legibility issue."
    ),
    "1.4.4": (
        "Low-vision users who rely on browser zoom cannot increase "
        "text size to a usable level without horizontal scrolling or "
        "content clipping."
    ),
    "1.4.5": (
        "Users who customize text rendering (low vision, dyslexia, "
        "translation) cannot adjust this content because it is "
        "rendered as an image."
    ),
    "1.4.10": (
        "Low vision users on screen magnifiers (ZoomText, Windows "
        "Magnifier) and users on small viewports cannot read content "
        "without horizontal scrolling at 320 CSS px width."
    ),
    "1.4.11": (
        "Low-vision users and users with contrast sensitivity cannot "
        "perceive the boundary or state of this UI component."
    ),
    "1.4.12": (
        "Users who customize text spacing (low vision, dyslexia) lose "
        "content because it overflows or clips when spacing changes."
    ),
    "1.4.13": (
        "Screen reader users and keyboard users cannot dismiss or "
        "interact with content that appears on hover/focus."
    ),
    "2.1.1": (
        "Keyboard-only users (motor-impaired users, screen reader "
        "users on JAWS / NVDA / VoiceOver, switch users) cannot "
        "operate this functionality without a pointer device."
    ),
    "2.1.2": (
        "Keyboard-only users get trapped in this section and cannot "
        "leave using only the keyboard."
    ),
    "2.1.4": (
        "Speech-input users (Dragon, Voice Control) and motor-impaired "
        "users trigger this keyboard shortcut accidentally during "
        "dictation."
    ),
    "2.4.1": (
        "Screen reader users and keyboard users cannot bypass blocks "
        "of repeated content (navigation, banners) and must traverse "
        "them on every page."
    ),
    "2.4.2": (
        "Screen reader users cannot distinguish this page from others "
        "in their browsing session because the title is missing or "
        "uninformative."
    ),
    "2.4.3": (
        "Screen reader users and keyboard users encounter focus in an "
        "order that does not match the visual layout, breaking their "
        "ability to follow content."
    ),
    "2.4.4": (
        "Screen reader users on JAWS, NVDA, and VoiceOver who "
        "navigate by link list cannot determine each link's purpose "
        "from the link text alone."
    ),
    "2.4.5": (
        "Users with cognitive disabilities and screen-reader users "
        "lack alternative navigation paths to this page."
    ),
    "2.4.6": (
        "Screen reader users navigating by headings (h key) cannot "
        "predict section content from the heading text."
    ),
    "2.4.7": (
        "Sighted keyboard users (motor-impaired users, dexterity "
        "limitations) cannot tell which element currently has focus."
    ),
    "2.4.11": (
        "Keyboard users may have the focused element fully or "
        "partially obscured by sticky / fixed content, losing track "
        "of position."
    ),
    "2.5.1": (
        "Users with motor impairments who cannot perform multi-pointer "
        "or path-based gestures cannot operate this functionality."
    ),
    "2.5.2": (
        "Users with motor impairments who depend on pointer-down "
        "cancellation may trigger the action accidentally."
    ),
    "2.5.3": (
        "Voice control users (Dragon, macOS / Windows Voice Control) "
        "cannot target this control by its visible label because the "
        "accessible name does not contain the label. Screen reader "
        "users on JAWS / NVDA / VoiceOver hear a name that doesn't "
        "match what sighted users read."
    ),
    "2.5.4": (
        "Users with motor impairments who cannot perform device "
        "motion gestures cannot operate this functionality."
    ),
    "2.5.7": (
        "Users with motor impairments who cannot perform drag "
        "gestures cannot operate this functionality without an "
        "alternative."
    ),
    "2.5.8": (
        "Users with motor impairments cannot reliably activate small "
        "targets without spacing or alternative."
    ),
    "3.1.1": (
        "Screen reader users on JAWS, NVDA, and VoiceOver receive "
        "incorrect pronunciation because the document language is not "
        "programmatically determined."
    ),
    "3.1.2": (
        "Screen reader users on JAWS, NVDA, and VoiceOver receive "
        "incorrect pronunciation for content in a different language "
        "than the document default."
    ),
    "3.2.1": (
        "Screen reader users and keyboard users experience "
        "disorienting context changes when an element receives focus."
    ),
    "3.2.2": (
        "Screen reader users and keyboard users experience "
        "disorienting context changes when they change a control's "
        "value."
    ),
    "3.2.6": (
        "Users with cognitive disabilities cannot find help "
        "consistently across the site."
    ),
    "3.3.1": (
        "Users with cognitive disabilities and screen-reader users "
        "cannot identify which form field caused a submission error."
    ),
    "3.3.2": (
        "Screen reader users on JAWS, NVDA, and VoiceOver cannot "
        "determine the purpose of this form control because no label "
        "or instruction is provided."
    ),
    "3.3.3": (
        "Users with cognitive disabilities cannot recover from input "
        "errors without specific suggestions for correction."
    ),
    "3.3.4": (
        "Users with cognitive or motor impairments may submit "
        "consequential data (legal, financial) without an opportunity "
        "to review or undo the submission."
    ),
    "3.3.7": (
        "Users with cognitive or motor impairments cannot complete "
        "multi-step processes that require re-entering the same "
        "information."
    ),
    "3.3.8": (
        "Users with cognitive disabilities cannot complete the "
        "authentication process because it requires recall, "
        "transcription, or visual puzzle-solving."
    ),
    "4.1.1": (
        "Screen reader users on JAWS, NVDA, and VoiceOver may receive "
        "ambiguous or broken references when the underlying markup "
        "has duplicate IDs or parsing errors."
    ),
    "4.1.2": (
        "Screen reader users on JAWS, NVDA, and VoiceOver receive "
        "incorrect or missing name / role / value information for this "
        "interactive component."
    ),
    "4.1.3": (
        "Screen reader users do not hear status messages because they "
        "are not exposed via ARIA live regions or alert roles."
    ),
}


_GENERIC_HCS_IMPACT = (
    "Users of assistive technology (screen reader users on JAWS / "
    "NVDA / VoiceOver, keyboard-only users, voice-control users) may "
    "receive incorrect or missing information for this element, "
    "preventing equivalent access."
)


# ── HCS rule corroboration helpers ─────────────────────────────────────
# Two HCS rules over-fire in ways that produce known false positives on
# real pages. Both checks corroborate the HCS claim against the actual
# captured DOM before letting the finding flow into the judge prompt --
# same structural pattern as the IBM EAC bg-image / focus-visible
# filters in functions/ibm_eac_extract.py.

# H48 = "If a list of links is intended as a navigation section, mark
# it up as a list." HCS's message is conditional ("IF this element
# contains a navigation section..."), but our extractor previously
# flowed it through at MEDIUM severity and the judge then rewrote the
# conditional as assertive ("the navigation items are marked up as
# paragraphs, violating WCAG"). Verified failure on A11Y Project run
# 20260511: 3 false positives where the cited <p> elements were
# author-bio paragraphs and copyright lines, NOT navigation. Filter:
# only emit if the cited element's class actually appears inside a
# <nav> ancestor or role=navigation container in capture_data.html.
_HCS_H48_CODE_SUFFIX = "1_3_1.H48"

# F92,ARIA4 = "role=presentation used on element with semantic
# children." HCS's rule fires on any SVG with role=presentation that
# has children, even when the children are pure visual primitives
# (<path>, <circle>, <rect>) with no ARIA / labels / titles.
# Verified failure on A11Y Project run 20260511: hero illustration
# SVG with role=presentation and only <path> primitives was flagged.
# Filter: only emit if the matching SVG actually has children that
# carry explicit semantic meaning.
_HCS_F92_CODE_SUFFIX = "1_3_1.F92,ARIA4"


def _selector_class_inside_nav_ancestor(selector: str, html: str) -> bool:
    """Return True when a class-based selector's class name appears
    inside ANY <nav> element (or role=navigation container) in the
    captured HTML.

    Used to corroborate HCS H48 ("nav-as-paragraph") findings: the rule
    fires conditionally on any <p> that might be a navigation list, but
    the conditional is "IF this element contains a navigation section".
    If the cited <p> class never appears inside a <nav>, the conditional
    fails and the HCS warning is a false positive.

    Conservative: when no <nav> ancestor information is available
    (e.g. ``capture_data.html`` was empty or the selector has no
    class component), return True so the finding is KEPT for the judge
    to evaluate. The filter only suppresses findings we can affirmatively
    rule out.
    """
    if not selector or not html:
        return True
    import re
    # Pull every class token from the selector (handles 'p.foo',
    # '.foo', 'p.foo.bar', 'div.bg > p.lead', etc.)
    classes = re.findall(r"\.([a-zA-Z_][\w-]*)", selector)
    if not classes:
        # Selector has no class component; we can't class-match against
        # nav regions. Keep the finding -- conservative default.
        return True
    # Find all <nav>...</nav> regions. Non-greedy capture covers nested
    # navs imperfectly but is good enough for ancestor-class detection
    # (a class inside an inner nav is still inside SOME nav).
    nav_regions = re.findall(
        r"<nav\b[^>]*>([\s\S]*?)</nav>", html, re.IGNORECASE,
    )
    # Plus role=navigation containers on non-<nav> elements.
    nav_regions.extend(re.findall(
        r"<(?!nav\b)([a-zA-Z][\w-]*)\b[^>]*\brole\s*=\s*[\"']navigation[\"'][^>]*>"
        r"([\s\S]*?)</\1>",
        html, re.IGNORECASE,
    ))
    # Build a set of every class token that appears in any nav region.
    # Real HTML class matching treats `class="..."` as a
    # whitespace-separated token list, NOT a substring match. Class
    # "foo" must match `class="foo"`, `class="foo bar"`, `class="bar foo"`
    # but NOT `class="foo-bar"` -- in CSS, foo and foo-bar are
    # different selectors. A naive `\bfoo\b` regex would match
    # `foo-bar` because hyphen is a non-word char (creates a word
    # boundary). The token-list approach mirrors how browsers parse
    # the class attribute.
    class_tokens_in_navs: set[str] = set()
    class_attr_re = re.compile(
        r"""\bclass\s*=\s*(?:"([^"]*)"|'([^']*)')""",
        re.IGNORECASE,
    )
    for region in nav_regions:
        # findall returns tuples for the second pattern (group 1 = tag,
        # group 2 = content). Normalize to content-only.
        body = region[1] if isinstance(region, tuple) else region
        for m in class_attr_re.finditer(body):
            cls_value = m.group(1) or m.group(2) or ""
            for tok in cls_value.split():
                class_tokens_in_navs.add(tok)
    return any(c in class_tokens_in_navs for c in classes)


def _svg_with_role_presentation_has_semantic_children(html: str) -> bool:
    """Return True when ANY SVG element with role=presentation in the
    captured HTML has children that carry explicit ARIA semantics.

    Used to corroborate HCS F92,ARIA4 findings. The HCS rule fires on
    every SVG with role=presentation that has any children, but the
    real WCAG concern is only when those children are semantically
    meaningful (<title>, <desc>, <text>, or elements with role /
    aria-label / aria-labelledby / aria-describedby). Pure visual
    primitives (<path>, <rect>, <circle>, <ellipse>, <polygon>,
    <polyline>, <line>, <g>) are decoration regardless of role.

    Conservative: when no captured HTML is available, return True so
    the finding flows through to the judge for manual evaluation.
    """
    if not html:
        return True
    import re
    # Find every SVG with role=presentation
    pattern = re.compile(
        r"<svg\b[^>]*\brole\s*=\s*[\"']presentation[\"'][^>]*>([\s\S]*?)</svg>",
        re.IGNORECASE,
    )
    bodies = pattern.findall(html)
    if not bodies:
        # No matching SVG -- HCS finding doesn't match captured DOM at
        # all. Drop by returning False (no semantic children found
        # because the element itself can't be located).
        return False
    semantic_child_pattern = re.compile(
        r"<(title|desc|text)\b"
        r"|\b(role|aria-label|aria-labelledby|aria-describedby)\s*=",
        re.IGNORECASE,
    )
    for body in bodies:
        if semantic_child_pattern.search(body):
            return True
    return False


def extract_htmlcs_findings(
    capture_data: CaptureData, criterion_id: str,
) -> list[Finding]:
    """Extract HCS findings relevant to a given SC.

    Args:
        capture_data: holds ``htmlcs_results`` dict from
            ``capture/web_capture.py:_capture_htmlcs``. When the capture
            failed (CDN blocked, network issue) the dict carries an
            ``error`` field and an empty ``messages`` list; this
            function returns [] in that case so the SC check just
            doesn't see HCS evidence.
        criterion_id: dotted WCAG SC id ("1.1.1", "2.4.7", etc).

    Returns:
        List of Finding objects with source="htmlcs". Empty when the
        capture didn't run or no HCS message matched this SC.
    """
    if not getattr(capture_data, "htmlcs_results", None):
        return []
    messages = capture_data.htmlcs_results.get("messages") or []
    if not messages:
        return []

    # Load the captured DOM for corroboration filters. capture_data.html
    # is populated in-memory during capture (web_capture.py:445 and
    # capture/v2/orchestrator.py:325/361), but the serialised
    # capture_data.json is empty for the html field on the v2 pipeline,
    # and the field may be cleared between capture and SC-check phases
    # to reduce memory. Fall back to reading dom_path from disk when the
    # in-memory field is empty -- guarantees the H48 / F92 filters see
    # real DOM data regardless of the pipeline's memory-management
    # decisions. Verified gap on A11Y Project run 20260511 SC 1.3.1
    # where the H48 filter returned conservative "keep" because the
    # in-memory html field was empty even though dom.html was on disk
    # with 3 <nav> elements.
    html = getattr(capture_data, "html", "") or ""
    if not html:
        dom_path = getattr(capture_data, "dom_path", "") or ""
        if dom_path:
            try:
                with open(dom_path, "r", encoding="utf-8") as _fh:
                    html = _fh.read()
            except Exception:
                # Soft-no-op: if dom.html isn't readable, filters fall
                # back to conservative "keep" -- no worse than before.
                import logging
                logging.getLogger(__name__).debug(
                    "HTMLCS SC %s: dom_path %r unreadable -- corroboration "
                    "filters running on empty html (conservative keep)",
                    criterion_id, dom_path,
                )
                html = ""

    findings: list[Finding] = []
    dropped_h48 = 0
    dropped_f92 = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        code = str(m.get("code") or "")
        sc = _sc_id_from_code(code)
        if sc != criterion_id:
            continue

        msg_type = m.get("type")
        try:
            msg_type_int = int(msg_type) if msg_type is not None else 3
        except (TypeError, ValueError):
            msg_type_int = 3
        severity = _TYPE_TO_SEVERITY.get(msg_type_int, Severity.INFO)

        text = str(m.get("msg") or "").strip() or "(no description)"
        selector = str(m.get("selector") or "").strip()
        tag = str(m.get("tag_name") or "").strip()

        # Corroboration filters: drop known false-positive patterns
        # BEFORE they reach the judge prompt. Same structural strategy
        # as the IBM EAC bg-image / focus-visible filters.
        if code.endswith(_HCS_H48_CODE_SUFFIX):
            # H48: "if this <p> contains navigation, mark it up as a
            # list." The HCS message is conditional; the body text or
            # author-credit paragraphs that have 2-3 inline links are
            # NOT navigation. Only emit if the cited element actually
            # sits inside a <nav> ancestor in the captured DOM.
            if not _selector_class_inside_nav_ancestor(selector, html):
                dropped_h48 += 1
                continue
        if code.endswith(_HCS_F92_CODE_SUFFIX):
            # F92,ARIA4: "role=presentation contains semantic children."
            # HCS fires on any SVG with role=presentation that has
            # children, but the WCAG concern is only when those
            # children carry explicit ARIA semantics. Pure visual
            # primitives (<path>, <circle>, <rect>) are decoration
            # regardless of role.
            if not _svg_with_role_presentation_has_semantic_children(html):
                dropped_f92 += 1
                continue

        # The element field gets a human-friendly description; the
        # selector field gets the raw CSS so the judge can verify it
        # against the captured DOM. Mirrors axe_extract conventions.
        element_desc = selector if selector else (
            f"<{tag}>" if tag else "(unknown element)"
        )

        # A type-3 message is an ADVISORY manual-check reminder, not a
        # detected violation. Label it unmistakably so the judge cannot
        # escalate it into a concrete failure finding asserting a
        # specific defect (verified on a university SC 4.1.3: a type-3
        # "check that status messages..." notice was rewritten into two
        # fabricated findings about non-existent loading indicators).
        if msg_type_int == 3:
            issue_text = (
                f"[ADVISORY — HTML_CodeSniffer manual-check reminder, NOT "
                f"a detected violation] (WCAG {criterion_id}, code={code}): "
                f"{text}"
            )
            impact_text = (
                "This is a manual-check reminder, not a measured defect — "
                "it is not evidence that a violation exists."
            )
            recommendation_text = (
                "Treat as an advisory note only. Do NOT escalate it into a "
                "concrete failure finding or assert a specific defect from "
                "it — no check detected one."
            )
        else:
            issue_text = (
                f"HTML_CodeSniffer "
                f"{('error' if msg_type_int == 1 else 'warning')} "
                f"(WCAG {criterion_id}, code={code}): {text}"
            )
            impact_text = _SC_IMPACT_PROSE.get(
                criterion_id, _GENERIC_HCS_IMPACT,
            )
            recommendation_text = (
                f"WCAG {criterion_id} requires the condition described "
                "above. Review the flagged element and bring it into "
                "compliance with the success criterion."
            )

        findings.append(Finding(
            id=_make_finding_id(),
            element=element_desc,
            issue=issue_text,
            impact=impact_text,
            recommendation=recommendation_text,
            severity=severity,
            source="htmlcs",
            css_selector=selector,
        ))

    if dropped_h48:
        import logging
        logging.getLogger(__name__).info(
            "HTMLCS SC %s: dropped %d H48 'nav-as-paragraph' finding(s) "
            "where cited element has no <nav> ancestor in captured DOM "
            "(body-text paragraphs with inline links are NOT navigation)",
            criterion_id, dropped_h48,
        )
    if dropped_f92:
        import logging
        logging.getLogger(__name__).info(
            "HTMLCS SC %s: dropped %d F92,ARIA4 'role=presentation with "
            "semantic children' finding(s) where the cited SVG has only "
            "visual primitives (<path>/<circle>/<rect>), no semantic "
            "children",
            criterion_id, dropped_f92,
        )

    return findings
