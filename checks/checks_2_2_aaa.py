"""WCAG Guideline 2.2 - Enough Time AAA checks."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_2_2_3(BaseCheck):
    """SC 2.2.3 No Timing (Level AAA)."""

    criterion_id = "2.2.3"
    criterion_name = "No Timing"
    # Applicability is a meaning judgment — keyword scan is advisory only.
    ai_judged_applicability = True
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.2 Enough Time"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "Timing is not an essential part of the event or activity "
        "presented by the content, except for non-interactive "
        "synchronized media and real-time events."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        script = capture_data.script_content or ""
        html = capture_data.html or ""
        return bool(
            "settimeout" in script.lower()
            or "setinterval" in script.lower()
            or "meta http-equiv" in html.lower()
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        script = capture_data.script_content or ""

        # Any timing mechanism is a potential issue at AAA
        if re.search(r"setTimeout|setInterval", script, re.IGNORECASE):
            findings.append(Finding(
                id=_make_finding_id(),
                element="<script>",
                issue="JavaScript timing functions detected (AAA requires no timing)",
                impact="Users with disabilities may need unlimited time.",
                recommendation="Remove timing constraints from all interactive content.",
                severity=Severity.LOW,
            ))

        html = capture_data.html or ""
        if re.search(r'<meta\s+http-equiv\s*=\s*["\']refresh["\']', html, re.IGNORECASE):
            findings.append(Finding(
                id=_make_finding_id(),
                element="<meta http-equiv='refresh'>",
                issue="Meta refresh detected (AAA requires no timing)",
                impact="Users may not complete reading before redirect.",
                recommendation="Remove meta refresh entirely at AAA level.",
                severity=Severity.MEDIUM,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.5
        return conformance, confidence, findings


class Check_2_2_4(BaseCheck):
    """SC 2.2.4 Interruptions (Level AAA)."""

    criterion_id = "2.2.4"
    criterion_name = "Interruptions"
    # Applicability is a meaning judgment — keyword scan is advisory only.
    ai_judged_applicability = True
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.2 Enough Time"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "Interruptions can be postponed or suppressed by the user, "
        "except interruptions involving an emergency."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        script = capture_data.script_content or ""
        return bool(
            "alert(" in script
            or "confirm(" in script
            or "notification" in script.lower()
            or "modal" in script.lower()
            or "popup" in script.lower()
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        script = capture_data.script_content or ""

        interruption_patterns = [
            (r"alert\s*\(", "JavaScript alert dialog"),
            (r"confirm\s*\(", "JavaScript confirm dialog"),
            (r"(?:new\s+)?Notification\s*\(", "Browser notification"),
        ]

        for pattern, desc in interruption_patterns:
            if re.search(pattern, script):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="<script>",
                    issue=f"{desc} detected - potential user interruption",
                    impact="Users may be disrupted by unexpected interruptions.",
                    recommendation="Allow users to postpone or suppress all non-emergency interruptions.",
                    severity=Severity.LOW,
                ))

        # Check for auto-opening modals/popups
        auto_modal = re.search(
            r"(?:DOMContentLoaded|window\.onload|document\.ready)[^}]*"
            r"(?:modal|popup|dialog|overlay)",
            script, re.IGNORECASE | re.DOTALL,
        )
        if auto_modal:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<script>",
                issue="Auto-opening modal/popup detected on page load",
                impact="Users are interrupted immediately upon arriving at the page.",
                recommendation="Do not auto-open modals. Let users choose to open them.",
                severity=Severity.MEDIUM,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.4
        return conformance, confidence, findings


class Check_2_2_5(BaseCheck):
    """SC 2.2.5 Re-authenticating (Level AAA)."""

    criterion_id = "2.2.5"
    criterion_name = "Re-authenticating"
    # Applicability is a meaning judgment — keyword scan is advisory only.
    ai_judged_applicability = True
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.2 Enough Time"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "When an authenticated session expires, the user can continue "
        "the activity without loss of data after re-authenticating."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        script = capture_data.script_content or ""
        html = capture_data.html or ""
        return bool(
            "session" in script.lower()
            or "login" in html.lower()
            or "signin" in html.lower()
            or "authenticate" in script.lower()
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        script = capture_data.script_content or ""

        if re.search(r"session\s*(?:timeout|expir)", script, re.IGNORECASE):
            findings.append(Finding(
                id=_make_finding_id(),
                element="<script>",
                issue="Session expiration detected; verify data preservation on re-authentication",
                impact="Users may lose form data or progress if session expires.",
                recommendation=(
                    "Preserve all user data and restore it after "
                    "re-authentication so no work is lost."
                ),
                severity=Severity.INFO,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.3
        return conformance, confidence, findings


class Check_2_2_6(BaseCheck):
    """SC 2.2.6 Timeouts (Level AAA, WCAG 2.1/2.2)."""

    criterion_id = "2.2.6"
    criterion_name = "Timeouts"
    # Applicability is a meaning judgment — keyword scan is advisory only.
    ai_judged_applicability = True
    level = "AAA"
    wcag_versions = ["2.1", "2.2"]
    guideline = "2.2 Enough Time"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "Users are warned of the duration of any user inactivity that "
        "could cause data loss, unless the data is preserved for more "
        "than 20 hours when the user does not take any actions."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        script = capture_data.script_content or ""
        return bool(
            "timeout" in script.lower()
            or "inactiv" in script.lower()
            or "idle" in script.lower()
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        script = capture_data.script_content or ""

        timeout_detected = re.search(
            r"(?:inactiv|idle|timeout)\s*(?:timer|check|detect)",
            script, re.IGNORECASE,
        )
        if timeout_detected:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<script>",
                issue="Inactivity timeout detected; verify user is warned",
                impact="Users may lose data without warning due to inactivity timeout.",
                recommendation=(
                    "Warn users about the timeout duration at the start of "
                    "the activity, or preserve data for at least 20 hours."
                ),
                severity=Severity.INFO,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.3
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_2_2_3(),
        Check_2_2_4(),
        Check_2_2_5(),
        Check_2_2_6(),
    ]
