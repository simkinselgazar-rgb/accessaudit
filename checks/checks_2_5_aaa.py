"""WCAG Guideline 2.5 - Input Modalities AAA checks (WCAG 2.1/2.2)."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_2_5_5(BaseCheck):
    """SC 2.5.5 Target Size (Enhanced) (Level AAA, WCAG 2.1/2.2)."""

    criterion_id = "2.5.5"
    criterion_name = "Target Size (Enhanced)"
    level = "AAA"
    wcag_versions = ["2.1", "2.2"]
    guideline = "2.5 Input Modalities"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    # Target dimensions are measured per element from captured rects.
    measurement_sources = {
        "target_width_px": ("target_size_measurements", "width"),
        "target_height_px": ("target_size_measurements", "height"),
    }
    normative_text = (
        "The size of the target for pointer inputs is at least 44 by 44 "
        "CSS pixels except when: the target is available through an "
        "equivalent link or control on the same page that is at least "
        "44 by 44 CSS pixels; the target is in a sentence or block of "
        "text; the size of the target is determined by the user agent "
        "and is not modified by the author; a particular presentation "
        "of the target is essential to the information being conveyed."
    )
    web_only = True

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(
            capture_data.links
            or capture_data.form_fields
            or capture_data.tab_walk
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Check interactive elements for 44x44 minimum target size
        small_targets = []
        for field in capture_data.form_fields:
            rect = field.get("rect")
            if rect:
                w = rect.get("width", 0)
                h = rect.get("height", 0)
                if w > 0 and h > 0 and (w < 44 or h < 44):
                    selector = field.get("selector", field.get("id", field.get("name", "form element")))
                    small_targets.append((selector, w, h))

        for link in capture_data.links:
            rect = link.get("rect")
            if rect:
                w = rect.get("width", 0)
                h = rect.get("height", 0)
                if w > 0 and h > 0 and (w < 44 or h < 44):
                    text = link.get("text", link.get("href", "link"))
                    small_targets.append((text, w, h))

        for selector, w, h in small_targets:
            findings.append(Finding(
                id=_make_finding_id(),
                element=str(selector),
                issue=f"Target size is {w:.0f}x{h:.0f}px (AAA requires at least 44x44px)",
                impact="Users with motor impairments may have difficulty activating small targets.",
                recommendation="Increase the target size to at least 44x44 CSS pixels.",
                severity=Severity.LOW,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.6
        return conformance, confidence, findings


class Check_2_5_6(BaseCheck):
    """SC 2.5.6 Concurrent Input Mechanisms (Level AAA, WCAG 2.1/2.2)."""

    criterion_id = "2.5.6"
    criterion_name = "Concurrent Input Mechanisms"
    level = "AAA"
    wcag_versions = ["2.1", "2.2"]
    guideline = "2.5 Input Modalities"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "Web content does not restrict use of input modalities available "
        "on a platform except where the restriction is essential, required "
        "to ensure the security of the content, or required to respect "
        "user settings."
    )
    web_only = True

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        script = capture_data.script_content or ""
        html = capture_data.html or ""

        # Check for input restriction patterns
        restriction_patterns = [
            (r"navigator\.maxTouchPoints", "Checks maxTouchPoints to restrict input"),
            (r"ontouchstart\s+in\s+window", "Detects touch capability to restrict input"),
            (r"pointer.*coarse.*mouse.*none", "Media query restricts to touch-only"),
        ]

        for pattern, desc in restriction_patterns:
            if re.search(pattern, script, re.IGNORECASE):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="<script>",
                    issue=f"Potential input mechanism restriction: {desc}",
                    impact=(
                        "Users who switch between input methods (e.g., touch "
                        "and keyboard, mouse and voice) may be blocked."
                    ),
                    recommendation=(
                        "Do not restrict input to a single modality. Allow "
                        "users to use any input mechanism available on their device."
                    ),
                    severity=Severity.INFO,
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.3
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_2_5_5(),
        Check_2_5_6(),
    ]
