"""WCAG Guideline 1.3 - Adaptable AAA checks."""
from __future__ import annotations

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_1_3_6(BaseCheck):
    """SC 1.3.6 Identify Purpose (Level AAA, WCAG 2.1/2.2)."""

    criterion_id = "1.3.6"
    criterion_name = "Identify Purpose"
    level = "AAA"
    wcag_versions = ["2.1", "2.2"]
    guideline = "1.3 Adaptable"
    principle = "1. Perceivable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "In content implemented using markup languages, the purpose of "
        "User Interface Components, icons, and regions can be "
        "programmatically determined."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(
            capture_data.landmarks
            or capture_data.links
            or capture_data.form_fields
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Check landmarks for explicit roles
        for lm in capture_data.landmarks:
            role = lm.get("role", "")
            tag = (lm.get("tag") or lm.get("tagName") or "").lower()
            if not role and tag not in ("main", "nav", "header", "footer",
                                        "aside", "form", "section"):
                selector = lm.get("selector", "landmark")
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Region lacks explicit landmark role",
                    impact="Assistive technologies may not identify the region purpose.",
                    recommendation="Add an appropriate ARIA role to identify the region purpose.",
                    severity=Severity.LOW,
                ))

        # Check for icons without role="img" and accessible names
        for img in capture_data.images:
            is_icon = img.get("is_icon", False)
            role = img.get("role", "")
            aria_label = img.get("aria_label", img.get("aria-label", ""))
            if is_icon and not role and not aria_label:
                selector = img.get("selector", "icon")
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Icon lacks programmatically determinable purpose",
                    impact="Users relying on assistive technology cannot determine icon purpose.",
                    recommendation="Add role=\"img\" and aria-label describing the icon purpose.",
                    severity=Severity.LOW,
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.45
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [Check_1_3_6()]
