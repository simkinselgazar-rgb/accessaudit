"""WCAG Guideline 1.3 - Adaptable (A/AA) checks."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
    TTResult,
    TTSubTestResult,
)


class Check_1_3_1(BaseCheck):
    """SC 1.3.1 Info and Relationships (Level A)."""

    criterion_id = "1.3.1"
    criterion_name = "Info and Relationships"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.3 Adaptable"
    principle = "1. Perceivable"
    ict_baseline = "10"
    tt_tests = ["10.A", "10.B", "10.C", "10.D"]
    normative_text = (
        "Information, structure, and relationships conveyed through "
        "presentation can be programmatically determined or are available "
        "in text."
    )
    off_scope_keywords = {
        "contrast": ["contrast ratio", "color contrast"],
        "alt_text": ["alt attribute", "alternative text"],
    }

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(
            capture_data.headings
            or capture_data.form_fields
            or capture_data.tables
            or capture_data.lists
            or capture_data.landmarks
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # --- Heading hierarchy analysis ---
        if capture_data.headings:
            prev_level = 0
            for h in capture_data.headings:
                tag = (h.get("tag") or h.get("tagName") or "").lower()
                selector = h.get("selector", tag)
                text = (h.get("text") or "").strip()
                # Extract heading level from tag
                match = re.match(r"h(\d)", tag)
                if not match:
                    # ARIA heading
                    level = h.get("aria_level", h.get("aria-level", 0))
                    try:
                        level = int(level)
                    except (ValueError, TypeError):
                        level = 0
                else:
                    level = int(match.group(1))

                if level == 0:
                    continue

                # Check for empty headings
                if not text:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=f"Empty heading element <{tag}>",
                        impact=(
                            "Screen reader users navigating by headings will "
                            "encounter an empty heading that provides no information."
                        ),
                        recommendation="Add descriptive text or remove the empty heading element.",
                        severity=Severity.MEDIUM,
                    ))

                # Check for skipped heading levels (e.g., h1 -> h3)
                if prev_level > 0 and level > prev_level + 1:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Heading level skipped: <h{prev_level}> to <h{level}> "
                            f"(missing <h{prev_level + 1}>)"
                        ),
                        impact=(
                            f"Heading levels skip from h{prev_level} to h{level}, "
                            f"which may confuse screen reader users navigating by "
                            f"headings. Note: this may be valid if the headings are "
                            f"in different HTML5 sectioning elements (section, "
                            f"article, nav, aside)."
                        ),
                        recommendation=(
                            f"Use <h{prev_level + 1}> or restructure the heading "
                            f"hierarchy to avoid skipping levels."
                        ),
                        severity=Severity.LOW,
                    ))

                prev_level = level

            # Check if page has no h1 OR multiple h1s. A well-structured
            # page has exactly one <h1>: missing means screen-reader users
            # have no clear "top of page" anchor; multiple means the page
            # outline is flat and ambiguous. Both violate the spirit of SC
            # 1.3.1 programmatically determinable hierarchy.
            h1_entries = [
                h for h in capture_data.headings
                if ((h.get("tag") or h.get("tagName") or "").lower() == "h1")
            ]
            if not h1_entries and capture_data.headings:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="html",
                    issue="Page has headings but no <h1> element",
                    impact=(
                        "Screen reader users expect an h1 as the primary page heading."
                    ),
                    recommendation="Add an <h1> element as the main page heading.",
                    severity=Severity.MEDIUM,
                ))
            elif len(h1_entries) > 1:
                texts = [
                    (h.get("text") or "").strip()
                    for h in h1_entries
                    if (h.get("text") or "").strip()
                ]
                preview = ", ".join(f'"{t}"' for t in texts)
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="html",
                    issue=(
                        f"Page has {len(h1_entries)} <h1> elements: {preview}. "
                        f"WCAG 1.3.1 does not strictly limit h1 count "
                        f"(HTML5 sectioning permits multiple), but multiple h1s "
                        f"flatten the heading outline; review whether each "
                        f"corresponds to a distinct top-level page section."
                    ),
                    impact=(
                        "Screen-reader users navigating by heading level may "
                        "find it unclear which heading is the page title "
                        "versus a section heading when the document outline "
                        "has many h1s at the same level."
                    ),
                    recommendation=(
                        "Verify each <h1> represents a true top-level section. "
                        "If a heading is content nested within another section, "
                        "demote to <h2>/<h3> based on its nesting depth."
                    ),
                    severity=Severity.MEDIUM,
                ))

        # --- Form field labels ---
        for field in capture_data.form_fields:
            selector = field.get("selector", "form field")
            field_type = (field.get("type") or "").lower()
            tag = (field.get("tag") or field.get("tagName") or "").lower()
            label = field.get("label", "")
            aria_label = field.get("aria_label", field.get("aria-label", ""))
            aria_labelledby = field.get("aria_labelledby", field.get("aria-labelledby", ""))
            title = field.get("title", "")
            placeholder = field.get("placeholder", "")

            # Skip hidden and submit/button types
            if field_type in ("hidden", "submit", "button", "reset", "image"):
                continue

            has_label = bool(label or aria_label or aria_labelledby or title)
            if not has_label:
                sev = Severity.HIGH
                # If there is at least a placeholder, lower severity
                if placeholder:
                    sev = Severity.MEDIUM
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"Form field ({field_type or tag}) has no accessible label"
                        + (f" (has placeholder: \"{placeholder}\")" if placeholder else "")
                    ),
                    impact=(
                        "Screen reader users will not know what information to "
                        "enter in this field."
                    ),
                    recommendation=(
                        "Add a <label> element associated via for/id, or use "
                        "aria-label or aria-labelledby."
                    ),
                    severity=sev,
                ))

            # Check fieldset/legend for radio/checkbox groups.
            # Skip:
            #   - role="switch" toggles. ARIA switches are individual
            #     on/off controls (the same shape as <input type="checkbox">
            #     under the hood), not group-style controls. Each toggle
            #     is independently labeled and does not need a fieldset.
            #     OneTrust / Google's cookie-preference UIs use this
            #     pattern.
            #   - Inputs with their own aria-label / aria-labelledby /
            #     associated <label>: these are individually labeled
            #     standalone controls, not part of an unlabeled group.
            #     A fieldset is only meaningful when it labels a *set* of
            #     related controls; a lone "Subscribe" checkbox doesn't
            #     belong in one.
            if field_type in ("radio", "checkbox"):
                role = (field.get("role") or "").lower()
                if role == "switch":
                    pass  # individual toggle, no group needed
                else:
                    has_fieldset = field.get("in_fieldset", False)
                    has_group_label = field.get("group_label", "")
                    has_individual_name = bool(
                        field.get("aria_label")
                        or field.get("aria-label")
                        or field.get("aria_labelledby")
                        or field.get("aria-labelledby")
                        or field.get("label")
                    )
                    # Only flag radios as needing fieldset (radios are
                    # inherently group controls). Standalone checkboxes
                    # with individual labels are fine without fieldset.
                    needs_group = (
                        field_type == "radio"
                        or (field_type == "checkbox" and not has_individual_name)
                    )
                    if needs_group and not has_fieldset and not has_group_label:
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=selector,
                            issue=f"{field_type.capitalize()} input is not grouped in a <fieldset> with <legend>",
                            impact=(
                                "Screen reader users may not understand the grouping "
                                "relationship of related radio/checkbox controls."
                            ),
                            recommendation=(
                                "Wrap related radio/checkbox inputs in a <fieldset> "
                                "with a descriptive <legend> element."
                            ),
                            severity=Severity.MEDIUM,
                        ))

        # Table structure findings come from ANDI tANDI
        # (BaseCheck._extract_andi_tables_findings). It performs the
        # full data-vs-layout classification, scope/headers validation,
        # caption/summary checks, nested-table detection, and
        # referential-integrity checks — all of which the legacy
        # capture_data.tables-driven block here only partially covered.
        # Removed to keep findings single-sourced.

        # --- List structure ---
        for lst in capture_data.lists:
            selector = lst.get("selector", "list")
            tag = (lst.get("tag") or lst.get("tagName") or "").lower()
            children = lst.get("children", [])
            invalid_children = lst.get("invalid_children", [])

            if invalid_children:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"List <{tag}> contains non-list-item children: "
                        f"{', '.join(invalid_children)}"
                    ),
                    impact="Screen reader users receive incorrect list semantics.",
                    recommendation=f"Ensure all direct children of <{tag}> are <li> elements.",
                    severity=Severity.MEDIUM,
                ))

        # --- Landmarks ---
        if not capture_data.landmarks:
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue="Page has no ARIA landmark regions",
                impact=(
                    "Screen reader users cannot quickly navigate to major "
                    "sections of the page."
                ),
                recommendation=(
                    "Add landmark roles: <main>, <nav>, <header>, <footer>, "
                    "or use role attributes."
                ),
                severity=Severity.LOW,
            ))
        else:
            # Check for main landmark
            has_main = any(
                (l.get("role") or "").lower() == "main"
                or (l.get("tag") or l.get("tagName") or "").lower() == "main"
                for l in capture_data.landmarks
            )
            if not has_main:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="body",
                    issue="Page has no <main> landmark",
                    impact="Screen reader users cannot quickly jump to the main content.",
                    recommendation="Add a <main> element or role=\"main\" to the primary content area.",
                    severity=Severity.MEDIUM,
                ))

            # Check for duplicate landmarks without labels.
            # Only consider the canonical ARIA landmark roles -- a bare
            # tag-name fallback ("header", "footer", "aside") would
            # double-count <header> elements that are NOT banner landmarks
            # because they're nested inside <main>/<article>/<section>.
            # The capture-side extraction filters those out (per HTML5
            # landmark scoping rules), but this guard keeps the rule
            # correct even if upstream data slips a non-landmark tag in.
            LANDMARK_ROLES = {
                "banner", "navigation", "main", "complementary",
                "contentinfo", "search", "form", "region",
            }
            role_counts: dict[str, int] = {}
            for lm in capture_data.landmarks:
                role = (lm.get("role") or "").lower()
                if role not in LANDMARK_ROLES:
                    continue
                role_counts[role] = role_counts.get(role, 0) + 1
            for lm in capture_data.landmarks:
                role = (lm.get("role") or "").lower()
                if role not in LANDMARK_ROLES:
                    continue
                label = lm.get("aria_label", lm.get("aria-label", ""))
                labelledby = lm.get("aria_labelledby", lm.get("aria-labelledby", ""))
                if role_counts.get(role, 0) > 1 and not label and not labelledby:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=lm.get("selector", role),
                        issue=f"Duplicate {role} landmark has no accessible label",
                        impact=(
                            "Screen reader users cannot distinguish between "
                            "multiple landmarks of the same type."
                        ),
                        recommendation=(
                            f"Add aria-label or aria-labelledby to distinguish "
                            f"this {role} landmark from others."
                        ),
                        severity=Severity.MEDIUM,
                    ))

        # --- Pseudo-element content not in accessibility tree ---
        _DECORATIVE_PSEUDO_RE = re.compile(
            r'^[\s\u2022\u2013\u2014\u2018\u2019\u201c\u201d'
            r'\u00ab\u00bb\u25cf\u25cb\u25b6\u25c0\u2192\u2190'
            r'\u2191\u2193\u2606\u2605|/\\:;\-\.\,\!\?\*\#\>\<'
            r'\u00a0\u200b\ufeff]+$'
        )
        for pe in getattr(capture_data, "pseudo_elements", None) or []:
            content = (pe.get("content") or "").strip()
            if not content:
                continue
            # Skip purely decorative / punctuation-only content
            if _DECORATIVE_PSEUDO_RE.match(content):
                continue
            visibility = (pe.get("visibility") or "").lower()
            aria_hidden = (pe.get("ariaHidden") or "").lower()
            if visibility == "hidden" or aria_hidden == "true":
                continue
            selector = pe.get("selector", "element")
            pseudo = pe.get("pseudo", "")
            findings.append(Finding(
                id=_make_finding_id(),
                element=f"{selector}{pseudo}",
                issue=(
                    f"Pseudo-element content \"{content}\" on "
                    f"{selector}{pseudo} may convey information not "
                    f"available to assistive technology"
                ),
                impact=(
                    "Content injected via CSS ::before/::after pseudo-elements "
                    "is not included in the accessibility tree. If this text "
                    "conveys meaningful information, assistive technology users "
                    "will miss it."
                ),
                recommendation=(
                    "Move meaningful text into the DOM so it is available to "
                    "assistive technology, or supplement it with visually-hidden "
                    "text or an aria-label."
                ),
                severity=Severity.MEDIUM,
            ))

        total = (
            len(capture_data.headings) + len(capture_data.form_fields)
            + len(capture_data.tables) + len(capture_data.lists)
        )
        conformance = self._determine_conformance(findings, total)
        confidence = 0.85
        return conformance, confidence, findings

    def _generate_tt_results(
        self, findings: list[Finding], capture_data: CaptureData
    ) -> list[TTSubTestResult]:
        not_app = not self.is_applicable(capture_data)
        heading_fail = any(
            "heading" in f.issue.lower()
            for f in findings if f.severity in (Severity.HIGH, Severity.MEDIUM)
        )
        label_fail = any(
            "label" in f.issue.lower() or "form field" in f.issue.lower()
            for f in findings if f.severity in (Severity.HIGH, Severity.MEDIUM)
        )
        table_fail = any(
            "table" in f.issue.lower()
            for f in findings if f.severity in (Severity.HIGH, Severity.MEDIUM)
        )
        list_fail = any(
            "list" in f.issue.lower()
            for f in findings if f.severity in (Severity.HIGH, Severity.MEDIUM)
        )
        return [
            TTSubTestResult(
                tt_id="10.A",
                name="Heading levels programmatically determined",
                result=TTResult.DNA if not_app else TTResult.FAIL if heading_fail else TTResult.PASS,
            ),
            TTSubTestResult(
                tt_id="10.B",
                name="Form labels programmatically associated",
                result=TTResult.DNA if not_app else TTResult.FAIL if label_fail else TTResult.PASS,
            ),
            TTSubTestResult(
                tt_id="10.C",
                name="Table structure programmatically determined",
                result=TTResult.DNA if not_app else TTResult.FAIL if table_fail else TTResult.PASS,
            ),
            TTSubTestResult(
                tt_id="10.D",
                name="List structure programmatically determined",
                result=TTResult.DNA if not_app else TTResult.FAIL if list_fail else TTResult.PASS,
            ),
        ]


class Check_1_3_2(BaseCheck):
    """SC 1.3.2 Meaningful Sequence (Level A)."""

    criterion_id = "1.3.2"
    criterion_name = "Meaningful Sequence"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.3 Adaptable"
    principle = "1. Perceivable"
    ict_baseline = "15"
    tt_tests = ["15.A"]
    normative_text = (
        "When the sequence in which content is presented affects its "
        "meaning, a correct reading sequence can be programmatically determined."
    )
    off_scope_keywords = {
        "focus_order": ["focus order", "tab order"],
    }

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Check for CSS properties that reorder content visually
        for style in capture_data.computed_styles:
            selector = style.get("selector", "")
            order = style.get("order")
            flex_direction = style.get("flex_direction", style.get("flex-direction", ""))
            float_val = style.get("float", "")
            position = style.get("position", "")
            display = style.get("display", "")

            # Elements with CSS order property (flexbox/grid reordering)
            if order is not None and str(order) != "0":
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=f"Element uses CSS order: {order} which may reorder content visually",
                    impact=(
                        "The visual order may differ from the DOM order, "
                        "causing confusion for screen reader users who follow "
                        "the DOM sequence."
                    ),
                    recommendation=(
                        "Ensure the DOM order matches the intended reading "
                        "sequence, or use CSS order only for decorative reordering."
                    ),
                    severity=Severity.LOW,
                ))

            # flex-direction: row-reverse or column-reverse
            if flex_direction in ("row-reverse", "column-reverse"):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=f"Element uses flex-direction: {flex_direction} which reverses visual order",
                    impact=(
                        "Visual content order is reversed from DOM order, "
                        "which can confuse assistive technology users."
                    ),
                    recommendation=(
                        "Ensure the DOM order reflects the meaningful reading "
                        "sequence regardless of visual presentation."
                    ),
                    severity=Severity.LOW,
                ))

        # Check for tabindex values that override natural order
        tab_walk = capture_data.tab_walk or []
        positive_tabindex = [
            t for t in tab_walk
            if t.get("tabindex", 0) and int(t.get("tabindex", 0)) > 0
        ]
        if positive_tabindex:
            for t in positive_tabindex:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=t.get("selector", "element"),
                    issue=f"Element has positive tabindex={t.get('tabindex')}, overriding natural order",
                    impact=(
                        "Positive tabindex values change the navigation order, "
                        "potentially making the reading sequence confusing."
                    ),
                    recommendation=(
                        "Remove positive tabindex and rely on DOM order for "
                        "a meaningful sequence."
                    ),
                    severity=Severity.MEDIUM,
                ))

        # Landmark visual-order vs DOM-order check.
        #
        # When a page renders the navigation/banner at the top visually
        # but the DOM places those landmarks at the END (a common SPA
        # pattern where main content is mounted first, then chrome is
        # injected via JS), screen-reader users navigating in DOM order
        # hear the page out of sequence -- main content first, banner
        # last -- which is the failure mode SC 1.3.2 protects against.
        # We compare landmark DOM order against landmark Y-coordinate
        # order. A mismatch where a later-DOM landmark renders ABOVE an
        # earlier-DOM one is the diagnostic.
        #
        # We restrict comparison to the canonical structural landmarks
        # (banner, navigation, main, contentinfo) -- decorative "region"
        # / "complementary" containers float around for layout reasons
        # and don't reliably indicate reading-order intent.
        STRUCTURAL = ("banner", "navigation", "main", "contentinfo")
        ordered_landmarks: list[dict] = []
        for i, lm in enumerate(capture_data.landmarks or []):
            role = (lm.get("role") or "").lower()
            if role not in STRUCTURAL:
                continue
            rect = lm.get("rect") or {}
            try:
                y = float(rect.get("y") or 0)
                w = float(rect.get("width") or 0)
                h = float(rect.get("height") or 0)
            except (TypeError, ValueError):
                continue
            if w <= 0 or h <= 0:
                continue
            ordered_landmarks.append({
                "dom_index": i, "role": role,
                "selector": lm.get("selector", role),
                "y": y,
                "label": lm.get("aria_label") or lm.get("aria-label") or "",
            })
        # For each adjacent pair in DOM order, check whether the LATER
        # landmark renders ABOVE the earlier one (Y mismatch).
        for a, b in zip(ordered_landmarks, ordered_landmarks[1:]):
            # Allow a small overlap tolerance — rendered landmarks may
            # share a horizontal stripe (e.g. nav inside banner sharing
            # 0..100 Y range). Only flag when b is at least ~80px above a.
            if b["y"] < a["y"] - 80:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=b["selector"],
                    issue=(
                        f"Landmark <{b['role']}> at DOM position #{b['dom_index']} "
                        f"renders at Y={b['y']:.0f}px, ABOVE the earlier "
                        f"<{a['role']}> at DOM position #{a['dom_index']} "
                        f"(Y={a['y']:.0f}px). The DOM reading order is "
                        f"out of sequence with the visual presentation, "
                        f"violating WCAG 1.3.2."
                    ),
                    impact=(
                        "Screen-reader users following DOM order will "
                        "encounter the main content before the banner / "
                        "navigation, contrary to the visual layout."
                    ),
                    recommendation=(
                        "Move the landmark in the DOM so its position "
                        "matches its visual placement, or use semantic "
                        "ordering (e.g., header before main) consistently."
                    ),
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings)
        # The CSS-order + tabindex sweep covers the easy cases. The
        # landmark visual/DOM-order check above closes the SPA-injection
        # gap. The remaining 1.3.2 cases (table reading order, list
        # vs paragraph nesting) still benefit from AI corroboration —
        # so we keep confidence moderate when this check produces a
        # clean result so the judge isn't overridden too readily.
        confidence = 0.75 if findings else 0.7
        return conformance, confidence, findings


class Check_1_3_3(BaseCheck):
    """SC 1.3.3 Sensory Characteristics (Level A)."""

    criterion_id = "1.3.3"
    criterion_name = "Sensory Characteristics"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.3 Adaptable"
    principle = "1. Perceivable"
    ict_baseline = "7"
    tt_tests = ["7.A"]
    normative_text = (
        "Instructions provided for understanding and operating content "
        "do not rely solely on sensory characteristics of components "
        "such as shape, color, size, visual location, orientation, or sound."
    )
    off_scope_keywords = {
        "alt_text": [
            "missing alt", "alt text", "alternative text",
            "no alt attribute", "text alternative",
        ],
        "images": [
            "image lacks", "icon without text", "image-based",
            "represented solely by an icon", "identified solely by",
            "shape and color of", "shape of the icon",
            "shape and color of the icon", "icon image",
            "navigation relies solely on",
            "navigation menu items are identified solely",
            "represented by images without text",
            "images without text labels",
            "without a text label visible",
            "image without a text label",
        ],
        "color_only": [
            "distinguished by background color",
            "distinguished by color",
            "color alone", "color only",
        ],
    }

    def get_extra_images(self, capture_data: CaptureData) -> list[str]:
        """Send 200% and 320px views — sensory instructions like
        'click the button on the right' are visual-layout dependent.
        Multiple viewport sizes help the AI catch location-dependent
        instructions that break at different widths."""
        paths: list[str] = []
        if capture_data.viewport_200pct_path:
            paths.append(capture_data.viewport_200pct_path)
        if capture_data.viewport_320px_path:
            paths.append(capture_data.viewport_320px_path)
        return paths

    def is_applicable(self, capture_data: CaptureData) -> bool:
        # Always applicable - AI-heavy check for sensory instructions in text
        return bool(capture_data.html)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        # This is primarily an AI check. Programmatic: scan for common
        # sensory-only instruction patterns in the HTML.
        findings: list[Finding] = []
        html_lower = capture_data.html.lower() if capture_data.html else ""

        sensory_patterns = [
            (r"click\s+the\s+(red|green|blue|yellow|orange)\s+(button|link|icon)",
             "color-only instruction"),
            (r"(the|see)\s+(round|square|circular|triangular)\s+(button|icon|element)",
             "shape-only instruction"),
            (r"on\s+the\s+(left|right)\s+side",
             "location-only instruction"),
            (r"the\s+(large|small|big|tiny)\s+(button|icon|text)",
             "size-only instruction"),
        ]

        for pattern, desc in sensory_patterns:
            matches = re.findall(pattern, html_lower)
            if matches:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="body (text content)",
                    issue=f"Possible {desc} found in page content",
                    impact=(
                        "Users who cannot perceive the sensory characteristic "
                        "(color, shape, size, location) will not be able to "
                        "follow the instruction."
                    ),
                    recommendation=(
                        "Supplement sensory instructions with non-sensory "
                        "alternatives (e.g., label text, ARIA labels)."
                    ),
                    severity=Severity.MEDIUM,
                ))

        # Low programmatic confidence - this is primarily an AI check
        conformance = self._determine_conformance(findings)
        confidence = 0.35
        return conformance, confidence, findings


class Check_1_3_4(BaseCheck):
    """SC 1.3.4 Orientation (Level AA, WCAG 2.1/2.2)."""

    criterion_id = "1.3.4"
    criterion_name = "Orientation"
    level = "AA"
    wcag_versions = ["2.1", "2.2"]
    guideline = "1.3 Adaptable"
    principle = "1. Perceivable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "Content does not restrict its view and operation to a single "
        "display orientation, such as portrait or landscape, unless a "
        "specific display orientation is essential."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html or capture_data.computed_styles)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html = capture_data.html or ""

        # Check for orientation media queries in inline styles or style blocks
        orientation_lock_patterns = [
            r"@media\s*\(\s*orientation\s*:\s*(portrait|landscape)\s*\)",
        ]

        for pattern in orientation_lock_patterns:
            matches = re.finditer(pattern, html, re.IGNORECASE)
            for m in matches:
                orientation = m.group(1)
                # Check if the media query hides content (display: none)
                # by scanning the surrounding CSS block
                start = m.start()
                block_end = html.find("}", start)
                if block_end != -1:
                    block = html[start:block_end + 1].lower()
                    if "display: none" in block or "display:none" in block:
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element="<style> block",
                            issue=(
                                f"CSS hides content when orientation is "
                                f"{orientation}, possibly locking orientation"
                            ),
                            impact=(
                                "Users who rely on a specific orientation "
                                "(e.g., mounted devices) may be unable to use "
                                "the content."
                            ),
                            recommendation=(
                                "Remove orientation restrictions unless essential "
                                "for the content's function."
                            ),
                            severity=Severity.MEDIUM,
                        ))

        # Check for transform: rotate INSIDE an orientation media query.
        # Earlier code checked "orientation" and "rotate" anywhere in the
        # html — a rotate animation in one CSS block plus an
        # orientation-aware fix in a totally different block both fired
        # the rule, producing false positives on NVCC and most marketing
        # sites. The correct check: scan each `@media (orientation:...)
        # { ... }` block and confirm a `transform:rotate(...)` lives
        # inside that block. Only such an in-query rotation can lock
        # the page to one orientation.
        for m in re.finditer(
            r"@media\s*[^{]*orientation[^{]*\{",
            html, re.IGNORECASE,
        ):
            depth = 1
            i = m.end()
            while i < len(html) and depth > 0:
                c = html[i]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                i += 1
            block = html[m.end():i - 1]
            if re.search(r"transform\s*:\s*rotate", block, re.IGNORECASE):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="<style> block",
                    issue=(
                        "CSS rotates content via transform: rotate INSIDE an "
                        "@media (orientation: ...) query, which can lock the "
                        "presentation to one orientation."
                    ),
                    impact=(
                        "Users on mounted devices or who prefer the opposite "
                        "orientation may be unable to read the rotated "
                        "content."
                    ),
                    recommendation=(
                        "Remove the rotation, OR confirm rotation is "
                        "essential per WCAG 1.3.4 'Essential' exception."
                    ),
                    severity=Severity.MEDIUM,
                ))
                break

        # Check viewport meta for orientation restriction
        vp = capture_data.viewport_meta
        if vp:
            content = (vp.get("content") or "").lower()
            if "orientation" in content and ("portrait" in content or "landscape" in content):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="<meta name='viewport'>",
                    issue="Viewport meta tag attempts to restrict display orientation",
                    impact="Users who need a specific orientation (e.g., mounted devices) cannot rotate the page.",
                    recommendation="Remove orientation restrictions from the viewport meta tag.",
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings)
        # The deterministic CSS scan covers all author-applied
        # orientation locks; AI cannot meaningfully add to this signal
        # (it can only see the rendered page and guess). This SC is on
        # PROGRAMMATIC_DEFINITIVE so the AI stack is skipped entirely.
        confidence = 0.95
        return conformance, confidence, findings


class Check_1_3_5(BaseCheck):
    """SC 1.3.5 Identify Input Purpose (Level AA, WCAG 2.1/2.2)."""

    criterion_id = "1.3.5"
    criterion_name = "Identify Input Purpose"
    level = "AA"
    wcag_versions = ["2.1", "2.2"]
    guideline = "1.3 Adaptable"
    principle = "1. Perceivable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "The purpose of each input field collecting information about "
        "the user can be programmatically determined when the input "
        "field serves a purpose identified in the Input Purposes for "
        "User Interface Components section."
    )

    # Valid autocomplete tokens per HTML spec
    _PERSONAL_AUTOCOMPLETE = {
        "name", "honorific-prefix", "given-name", "additional-name",
        "family-name", "honorific-suffix", "nickname", "email", "username",
        "new-password", "current-password", "one-time-code",
        "organization-title", "organization", "street-address",
        "address-line1", "address-line2", "address-line3",
        "address-level4", "address-level3", "address-level2",
        "address-level1", "country", "country-name", "postal-code",
        "cc-name", "cc-given-name", "cc-additional-name",
        "cc-family-name", "cc-number", "cc-exp", "cc-exp-month",
        "cc-exp-year", "cc-csc", "cc-type",
        "transaction-currency", "transaction-amount", "language",
        "bday", "bday-day", "bday-month", "bday-year",
        "sex", "tel", "tel-country-code", "tel-national",
        "tel-area-code", "tel-local", "tel-extension",
        "impp", "url", "photo",
    }

    # Map of common label text to expected autocomplete values
    _LABEL_AUTOCOMPLETE_MAP = {
        "first name": "given-name",
        "last name": "family-name",
        "full name": "name",
        "name": "name",
        "email": "email",
        "e-mail": "email",
        "phone": "tel",
        "telephone": "tel",
        "address": "street-address",
        "city": "address-level2",
        "state": "address-level1",
        "zip": "postal-code",
        "zip code": "postal-code",
        "postal code": "postal-code",
        "country": "country-name",
        "username": "username",
        "password": "current-password",
        "new password": "new-password",
        "credit card": "cc-number",
        "card number": "cc-number",
        "expiration": "cc-exp",
        "cvv": "cc-csc",
        "cvc": "cc-csc",
        "birthday": "bday",
        "date of birth": "bday",
    }

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.form_fields)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        for field in capture_data.form_fields:
            selector = field.get("selector", "input")
            field_type = (field.get("type") or "").lower()
            autocomplete = (field.get("autocomplete") or "").lower().strip()
            label = (field.get("label") or "").lower().strip()
            name_attr = (field.get("name") or "").lower().strip()
            placeholder = (field.get("placeholder") or "").lower().strip()

            # Skip non-input types
            if field_type in ("hidden", "submit", "button", "reset", "image",
                              "file", "checkbox", "radio", "range", "color"):
                continue

            # Determine if this is a personal-info field based on label/name
            text_to_check = label or name_attr or placeholder
            expected_autocomplete = None
            for label_text, ac_value in self._LABEL_AUTOCOMPLETE_MAP.items():
                if label_text in text_to_check:
                    expected_autocomplete = ac_value
                    break

            if expected_autocomplete:
                if not autocomplete:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Personal information field \"{text_to_check}\" "
                            f"missing autocomplete attribute (expected: "
                            f"autocomplete=\"{expected_autocomplete}\")"
                        ),
                        impact=(
                            "Users with cognitive disabilities cannot benefit "
                            "from autocomplete features or personalized icons."
                        ),
                        recommendation=(
                            f"Add autocomplete=\"{expected_autocomplete}\" to "
                            f"this input field."
                        ),
                        severity=Severity.MEDIUM,
                    ))
                elif autocomplete == "off":
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Personal information field \"{text_to_check}\" "
                            f"has autocomplete=\"off\""
                        ),
                        impact=(
                            "Disabling autocomplete prevents users from using "
                            "browser autofill, creating barriers for users with "
                            "motor or cognitive disabilities."
                        ),
                        recommendation=(
                            f"Replace autocomplete=\"off\" with "
                            f"autocomplete=\"{expected_autocomplete}\"."
                        ),
                        severity=Severity.MEDIUM,
                    ))
                elif autocomplete not in self._PERSONAL_AUTOCOMPLETE:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Personal information field has invalid "
                            f"autocomplete value: \"{autocomplete}\""
                        ),
                        impact="Browser cannot identify the field purpose for autofill.",
                        recommendation=(
                            f"Use a valid autocomplete token. Suggested: "
                            f"\"{expected_autocomplete}\"."
                        ),
                        severity=Severity.MEDIUM,
                    ))

        conformance = self._determine_conformance(findings, len(capture_data.form_fields))
        confidence = 0.8
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_1_3_1(),
        Check_1_3_2(),
        Check_1_3_3(),
        Check_1_3_4(),
        Check_1_3_5(),
    ]
