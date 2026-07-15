"""WCAG Guideline 3.2 - Predictable (A/AA) checks."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_3_2_1(BaseCheck):
    """SC 3.2.1 On Focus (Level A)."""

    criterion_id = "3.2.1"
    criterion_name = "On Focus"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.2 Predictable"
    principle = "3. Understandable"
    ict_baseline = "19"
    tt_tests = ["19.A"]
    normative_text = (
        "When any user interface component receives focus, it does not "
        "initiate a change of context."
    )
    off_scope_keywords = {
        "on_input": [
            "onchange", "on change", "when the value changes",
            "when an option is selected", "selecting an option",
            "changing the selection", "input event",
        ],
    }

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(
            capture_data.form_fields
            or capture_data.context_changes
            or capture_data.script_content
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Check for onfocus-triggered context changes
        for cc in capture_data.context_changes:
            trigger = (cc.get("trigger") or "").lower()
            if trigger in ("focus", "onfocus"):
                selector = cc.get("selector", "element")
                change_type = cc.get("type", "context change")
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=f"Focus triggers a context change: {change_type}",
                    impact=(
                        "Keyboard users will experience unexpected navigation "
                        "changes when tabbing through the page."
                    ),
                    recommendation=(
                        "Do not change context on focus. Use explicit user "
                        "actions (button clicks, form submissions) instead."
                    ),
                    severity=Severity.HIGH,
                ))

        # Check for onfocus in HTML attributes
        html = capture_data.html or ""
        onfocus_patterns = [
            (r'onfocus\s*=\s*["\'][^"\']*(?:window\.location|location\.href|'
             r'submit\s*\(|\.submit|navigate|redirect)', "onfocus navigation/submit"),
            (r'onfocus\s*=\s*["\'][^"\']*(?:window\.open|popup|modal)',
             "onfocus popup/window"),
        ]

        for pattern, desc in onfocus_patterns:
            matches = re.findall(pattern, html, re.IGNORECASE)
            if matches:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="element with onfocus",
                    issue=f"On-focus context change detected: {desc}",
                    impact="Focus triggers unexpected page navigation or new window.",
                    recommendation="Remove onfocus context changes; require explicit user action.",
                    severity=Severity.HIGH,
                ))

        # Check script for focus-based redirects
        script = capture_data.script_content or ""
        focus_redirect = re.search(
            r"addEventListener\s*\(\s*['\"]focus['\"][^)]*\)\s*[;{]\s*"
            r"(?:[^}]*(?:location|navigate|redirect|submit))",
            script, re.IGNORECASE | re.DOTALL,
        )
        if focus_redirect:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<script>",
                issue="JavaScript focus event handler triggers navigation or submission",
                impact="Users experience unexpected context changes on focus.",
                recommendation="Move the action to a click/submit handler.",
                severity=Severity.HIGH,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.7
        return conformance, confidence, findings


class Check_3_2_2(BaseCheck):
    """SC 3.2.2 On Input (Level A)."""

    criterion_id = "3.2.2"
    criterion_name = "On Input"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.2 Predictable"
    principle = "3. Understandable"
    ict_baseline = "19"
    tt_tests = ["19.B"]
    normative_text = (
        "Changing the setting of any user interface component does not "
        "automatically cause a change of context unless the user has "
        "been advised of the behavior before using the component."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(
            capture_data.form_fields
            or capture_data.context_changes
            or capture_data.script_content
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Check for onchange-triggered context changes
        for cc in capture_data.context_changes:
            trigger = (cc.get("trigger") or "").lower()
            if trigger in ("change", "onchange", "input", "oninput"):
                selector = cc.get("selector", "element")
                change_type = cc.get("type", "context change")
                warned = cc.get("warned", False)
                if not warned:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=f"Input change triggers context change: {change_type}",
                        impact="Users experience unexpected navigation when changing form values.",
                        recommendation=(
                            "Use a submit button instead, or warn users before "
                            "the control that changing it will navigate."
                        ),
                        severity=Severity.HIGH,
                    ))

        # Check for onchange form submission in HTML
        html = capture_data.html or ""
        onchange_submit = re.findall(
            r'onchange\s*=\s*["\'][^"\']*(?:submit|location|navigate|redirect)',
            html, re.IGNORECASE,
        )
        if onchange_submit:
            findings.append(Finding(
                id=_make_finding_id(),
                element="form element with onchange",
                issue="onchange triggers form submission or navigation",
                impact="Changing a dropdown or checkbox causes unexpected navigation.",
                recommendation="Provide a submit button and remove auto-submit on change.",
                severity=Severity.HIGH,
            ))

        # Check for select elements that auto-navigate
        select_nav = re.findall(
            r"<select[^>]*onchange\s*=\s*['\"][^'\"]*"
            r"(?:location|window|navigate|go|href)",
            html, re.IGNORECASE,
        )
        if select_nav:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<select>",
                issue="Select dropdown auto-navigates on change",
                impact="Keyboard users navigating options will be taken to unexpected pages.",
                recommendation="Add a 'Go' button instead of auto-navigating on selection change.",
                severity=Severity.HIGH,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.7
        return conformance, confidence, findings


class Check_3_2_3(BaseCheck):
    """SC 3.2.3 Consistent Navigation (Level AA)."""

    criterion_id = "3.2.3"
    criterion_name = "Consistent Navigation"
    level = "AA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.2 Predictable"
    principle = "3. Understandable"
    ict_baseline = ""
    tt_tests = []
    # Consistency is defined ACROSS pages -- not evaluable from one page.
    requires_multipage = True
    normative_text = (
        "Navigational mechanisms that are repeated on multiple Web pages "
        "within a set of Web pages occur in the same relative order each "
        "time they are repeated, unless a change is initiated by the user."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.landmarks or capture_data.links)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Consistent navigation can only be verified across multiple pages.
        # If we only have a single page capture, we cannot evaluate this SC.
        multi_page = getattr(capture_data, "crawl_pages", None) or []
        if len(multi_page) <= 1:
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue=(
                    "Consistent navigation cannot be verified on a single page. "
                    "This SC requires comparing navigation across multiple pages "
                    "within a set of Web pages."
                ),
                impact="N/A - cross-page comparison required.",
                recommendation=(
                    "Perform a multi-page review to verify navigation consistency. "
                    "Navigation appears present on this page."
                ),
                severity=Severity.INFO,
            ))
            return ConformanceLevel.SUPPORTS, 0.2, findings

        # This check requires multi-page comparison.
        # On a single page, we can verify that nav landmarks exist
        # and are consistently structured.
        nav_landmarks = [
            lm for lm in capture_data.landmarks
            if (lm.get("role") or "").lower() == "navigation"
            or (lm.get("tag") or lm.get("tagName") or "").lower() == "nav"
        ]

        if not nav_landmarks:
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue="No navigation landmarks found for consistency checking",
                impact="Cannot verify navigation consistency without nav regions.",
                recommendation="Use <nav> landmarks to identify repeated navigation blocks.",
                severity=Severity.INFO,
            ))

        # Check for multiple nav elements without labels (makes consistency hard)
        if len(nav_landmarks) > 1:
            unlabeled = [
                lm for lm in nav_landmarks
                if not lm.get("aria_label", lm.get("aria-label", ""))
                and not lm.get("aria_labelledby", lm.get("aria-labelledby", ""))
            ]
            if unlabeled:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="nav",
                    issue=f"{len(unlabeled)} navigation landmarks lack labels for identification",
                    impact="Unlabeled navigations are hard to identify for consistency.",
                    recommendation="Add aria-label to each <nav> to distinguish them.",
                    severity=Severity.LOW,
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.35  # Multi-page check; single page is limited
        return conformance, confidence, findings


class Check_3_2_4(BaseCheck):
    """SC 3.2.4 Consistent Identification (Level AA)."""

    criterion_id = "3.2.4"
    criterion_name = "Consistent Identification"
    level = "AA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.2 Predictable"
    principle = "3. Understandable"
    ict_baseline = ""
    tt_tests = []
    # Consistent identification is defined ACROSS pages.
    requires_multipage = True
    normative_text = (
        "Components that have the same functionality within a set of "
        "Web pages are identified consistently."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.links or capture_data.form_fields)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # On a single page, check that search fields/icons are consistently labeled
        search_fields = [
            f for f in capture_data.form_fields
            if "search" in ((f.get("type") or "") + (f.get("name") or "") + (f.get("label") or "")).lower()
        ]

        if len(search_fields) > 1:
            labels = set()
            for sf in search_fields:
                label = sf.get("label", sf.get("aria_label", sf.get("aria-label", ""))).strip()
                if label:
                    labels.add(label.lower())
            if len(labels) > 1:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="search fields",
                    issue=(
                        f"Multiple search fields with inconsistent labels: "
                        f"{', '.join(labels)}"
                    ),
                    impact="Inconsistent labeling may confuse users.",
                    recommendation="Use the same label for components with the same function.",
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.35  # Multi-page check; single page is limited
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_3_2_1(),
        Check_3_2_2(),
        Check_3_2_3(),
        Check_3_2_4(),
    ]
