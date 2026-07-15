"""WCAG Guideline 2.4 - Navigable AAA checks."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)

_VAGUE_LINK_TEXTS = {
    "click here", "here", "more", "read more", "learn more", "link",
    "click", "details", "this", "info", "go", "page", "continue",
}


class Check_2_4_8(BaseCheck):
    """SC 2.4.8 Location (Level AAA)."""

    criterion_id = "2.4.8"
    criterion_name = "Location"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "Information about the user's location within a set of Web pages "
        "is available."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html_lower = (capture_data.html or "").lower()

        # Check for breadcrumbs
        has_breadcrumb = bool(
            re.search(r'(?:class|role)\s*=\s*["\'][^"\']*breadcrumb', html_lower)
            or re.search(r'aria-label\s*=\s*["\']breadcrumb', html_lower)
        )

        # Check for active/current nav item
        has_current = bool(
            re.search(r'aria-current\s*=\s*["\'](?:page|step|location|true)', html_lower)
            or re.search(r'class\s*=\s*["\'][^"\']*(?:active|current|selected)', html_lower)
        )

        has_sitemap = "sitemap" in html_lower

        if not has_breadcrumb and not has_current and not has_sitemap:
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue="No location indicator found (no breadcrumbs, no aria-current, no sitemap link)",
                impact="Users may not know where they are within the site hierarchy.",
                recommendation=(
                    "Add breadcrumb navigation, aria-current='page' on "
                    "active nav items, or a sitemap."
                ),
                severity=Severity.LOW,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.5
        return conformance, confidence, findings


class Check_2_4_9(BaseCheck):
    """SC 2.4.9 Link Purpose (Link Only) (Level AAA)."""

    criterion_id = "2.4.9"
    criterion_name = "Link Purpose (Link Only)"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "A mechanism is available to allow the purpose of each link to "
        "be identified from link text alone, except where the purpose of "
        "the link would be ambiguous to users in general."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.links)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        for link in capture_data.links:
            if link.get("visible") is False:
                continue

            selector = link.get("selector", "a")
            text = (link.get("text") or "").strip()
            aria_label = (link.get("aria_label") or link.get("aria-label") or "").strip()
            effective = aria_label or text

            if effective.lower() in _VAGUE_LINK_TEXTS:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"Link text alone is not descriptive: \"{effective}\" "
                        f"(AAA: link text alone must convey purpose)"
                    ),
                    impact="Users cannot determine link purpose without surrounding context.",
                    recommendation=(
                        "Replace with descriptive text or add aria-label "
                        "that describes the link purpose."
                    ),
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings, len(capture_data.links))
        confidence = 0.7
        return conformance, confidence, findings


class Check_2_4_10(BaseCheck):
    """SC 2.4.10 Section Headings (Level AAA)."""

    criterion_id = "2.4.10"
    criterion_name = "Section Headings"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = "Section headings are used to organize the content."

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        if not capture_data.headings:
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue="Page has no heading elements to organize content",
                impact="Users navigating by headings cannot find content sections.",
                recommendation="Add heading elements (h1-h6) to organize page content into sections.",
                severity=Severity.MEDIUM,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.6
        return conformance, confidence, findings


class Check_2_4_12(BaseCheck):
    """SC 2.4.12 Focus Not Obscured (Enhanced) (Level AAA, WCAG 2.2)."""

    criterion_id = "2.4.12"
    criterion_name = "Focus Not Obscured (Enhanced)"
    level = "AAA"
    wcag_versions = ["2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "When a user interface component receives keyboard focus, no part "
        "of the component is hidden due to author-created content."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.tab_walk or capture_data.focus_indicators)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        for fi in capture_data.focus_indicators:
            selector = fi.get("selector", "element")
            visible_area = fi.get("visible_area_percent", 100)
            obscured_by = fi.get("obscured_by", "")

            # At AAA, NO PART can be hidden
            if visible_area < 100:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"Focused element is partially obscured "
                        f"({100 - visible_area:.0f}% hidden)"
                        + (f" by {obscured_by}" if obscured_by else "")
                        + " (AAA: no part may be hidden)"
                    ),
                    impact="Any hidden portion of a focused element fails AAA.",
                    recommendation="Ensure the entire focused element is fully visible.",
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.55
        return conformance, confidence, findings


class Check_2_4_13(BaseCheck):
    """SC 2.4.13 Focus Appearance (Level AAA, WCAG 2.2)."""

    criterion_id = "2.4.13"
    criterion_name = "Focus Appearance"
    level = "AAA"
    wcag_versions = ["2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "When the keyboard focus indicator is visible, an area of the "
        "focus indicator meets all the following: is at least as large as "
        "the area of a 2 CSS pixel thick perimeter of the unfocused "
        "component, has a contrast ratio of at least 3:1 between the "
        "same pixels in the focused and unfocused states, has a contrast "
        "ratio of at least 3:1 against adjacent non-focus-indicator colors."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.focus_indicators)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        for fi in capture_data.focus_indicators:
            selector = fi.get("selector", "element")
            visible = fi.get("visible", True)
            outline_width = fi.get("outline_width", fi.get("outline-width", ""))
            outline_color = fi.get("outline_color", fi.get("outline-color", ""))
            focus_contrast = fi.get("focus_contrast", None)

            if not visible:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="No visible focus indicator (AAA: strict appearance requirements)",
                    impact="Keyboard users cannot see focus position.",
                    recommendation="Add a visible focus indicator meeting AAA requirements.",
                    severity=Severity.HIGH,
                ))
                continue

            # Check outline thickness >= 2px
            try:
                width_val = float(str(outline_width).replace("px", "").strip())
                if width_val < 2:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=f"Focus indicator width ({width_val}px) is less than 2px",
                        impact="Focus indicator does not meet AAA size requirements.",
                        recommendation="Set outline width to at least 2px.",
                        severity=Severity.MEDIUM,
                    ))
            except (ValueError, TypeError):
                pass

            # Check focus indicator contrast
            if focus_contrast is not None and focus_contrast < 3.0:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"Focus indicator contrast ({focus_contrast:.2f}:1) "
                        f"is below 3:1 minimum"
                    ),
                    impact="Low-vision users may not perceive the focus indicator.",
                    recommendation="Increase focus indicator contrast to at least 3:1.",
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.5
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_2_4_8(),
        Check_2_4_9(),
        Check_2_4_10(),
        Check_2_4_12(),
        Check_2_4_13(),
    ]
