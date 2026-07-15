"""WCAG Guideline 3.2 - Predictable AAA checks."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_3_2_5(BaseCheck):
    """SC 3.2.5 Change on Request (Level AAA)."""

    criterion_id = "3.2.5"
    criterion_name = "Change on Request"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.2 Predictable"
    principle = "3. Understandable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "Changes of context are initiated only by user request, or a "
        "mechanism is available to turn off such changes."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(
            capture_data.context_changes
            or capture_data.script_content
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # At AAA, ALL context changes must be user-initiated
        for cc in capture_data.context_changes:
            trigger = (cc.get("trigger") or "").lower()
            user_initiated = cc.get("user_initiated", False)
            if not user_initiated:
                selector = cc.get("selector", "element")
                change_type = cc.get("type", "context change")
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"Non-user-initiated context change: {change_type} "
                        f"(trigger: {trigger})"
                    ),
                    impact="Context changes should only happen by explicit user request at AAA.",
                    recommendation="Ensure all context changes are triggered by explicit user action.",
                    severity=Severity.MEDIUM,
                ))

        # Check for auto-redirect/refresh
        html = capture_data.html or ""
        if re.search(r'<meta\s+http-equiv\s*=\s*["\']refresh["\']', html, re.IGNORECASE):
            findings.append(Finding(
                id=_make_finding_id(),
                element="<meta>",
                issue="Meta refresh causes non-user-initiated context change",
                impact="Page redirects without user request.",
                recommendation="Remove meta refresh; let users navigate explicitly.",
                severity=Severity.MEDIUM,
            ))

        # Check for target="_blank" without warning
        blank_links = re.findall(r'target\s*=\s*["\']_blank["\']', html, re.IGNORECASE)
        if blank_links:
            # Check if there is a warning mechanism
            has_new_window_warning = bool(
                re.search(r"(?:opens?\s+in\s+(?:a\s+)?new|external\s+link|new\s+window)", html.lower())
            )
            if not has_new_window_warning:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="links with target=_blank",
                    issue=f"{len(blank_links)} link(s) open new windows without warning users",
                    impact="Users are surprised by new windows opening.",
                    recommendation=(
                        "Warn users that links open in new windows "
                        "(via icon, text, or aria-label)."
                    ),
                    severity=Severity.LOW,
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.5
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [Check_3_2_5()]
