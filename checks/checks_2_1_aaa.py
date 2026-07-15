"""WCAG Guideline 2.1 - Keyboard Accessible AAA checks."""
from __future__ import annotations

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_2_1_3(BaseCheck):
    """SC 2.1.3 Keyboard (No Exception) (Level AAA)."""

    criterion_id = "2.1.3"
    criterion_name = "Keyboard (No Exception)"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.1 Keyboard Accessible"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "All functionality of the content is operable through a keyboard "
        "interface without requiring specific timings for individual "
        "keystrokes."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(
            capture_data.links
            or capture_data.form_fields
            or capture_data.tab_walk
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        # Same as 2.1.1 but no path-based exceptions
        findings: list[Finding] = []

        _INTERACTIVE_TAGS = {"a", "button", "input", "select", "textarea", "details", "summary"}

        for link in capture_data.links:
            selector = link.get("selector", "a")
            tag = link.get("tag", link.get("tagName", "a")).lower()
            href = link.get("href", "")
            tabindex = link.get("tabindex")
            has_click = link.get("has_onclick", False)

            if tag == "a" and not href and has_click:
                if tabindex is None or str(tabindex) == "-1":
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue="Interactive anchor is not keyboard accessible (AAA: no exceptions)",
                        impact="Keyboard users cannot activate this element.",
                        recommendation="Add href, tabindex='0' + keydown handler, or use <button>.",
                        severity=Severity.HIGH,
                    ))

        for field in capture_data.form_fields:
            selector = field.get("selector", "element")
            tag = (field.get("tag") or field.get("tagName") or "").lower()
            has_click = field.get("has_onclick", False)
            tabindex = field.get("tabindex")

            if tag not in _INTERACTIVE_TAGS and has_click:
                if tabindex is None or str(tabindex) == "-1":
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=f"Non-native interactive <{tag}> not keyboard accessible (AAA: no exceptions)",
                        impact="All functionality must be keyboard operable without exception.",
                        recommendation="Use a native interactive element or add full keyboard support.",
                        severity=Severity.HIGH,
                    ))

        conformance = self._determine_conformance(findings)
        confidence = 0.7
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [Check_2_1_3()]
