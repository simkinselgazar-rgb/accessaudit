"""WCAG Guideline 3.3 - Input Assistance AAA checks."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_3_3_5(BaseCheck):
    """SC 3.3.5 Help (Level AAA)."""

    criterion_id = "3.3.5"
    criterion_name = "Help"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.3 Input Assistance"
    principle = "3. Understandable"
    ict_baseline = ""
    tt_tests = []
    normative_text = "Context-sensitive help is available."

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.form_fields)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html_lower = (capture_data.html or "").lower()

        has_help = bool(
            re.search(r"(?:help|tooltip|hint|info.icon|popover)", html_lower)
        )
        has_inline_help = bool(
            re.search(r'aria-describedby|class\s*=\s*["\'][^"\']*help', html_lower)
        )

        if not has_help and not has_inline_help and capture_data.form_fields:
            findings.append(Finding(
                id=_make_finding_id(),
                element="form",
                issue="Form has no context-sensitive help mechanism",
                impact="Users who need guidance have no way to get help.",
                recommendation=(
                    "Provide context-sensitive help via tooltips, inline "
                    "hints, help links, or aria-describedby on form fields."
                ),
                severity=Severity.LOW,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.4
        return conformance, confidence, findings


class Check_3_3_6(BaseCheck):
    """SC 3.3.6 Error Prevention (All) (Level AAA)."""

    criterion_id = "3.3.6"
    criterion_name = "Error Prevention (All)"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.3 Input Assistance"
    principle = "3. Understandable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "For Web pages that require the user to submit information, at "
        "least one of the following is true: Reversible, Checked, Confirmed."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.form_fields)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html_lower = (capture_data.html or "").lower()

        has_forms = any(
            f.get("type", "") not in ("hidden", "submit", "button", "reset")
            for f in capture_data.form_fields
        )

        if has_forms:
            has_confirmation = bool(
                re.search(r"(?:confirm|review|preview|check|verify)", html_lower)
            )
            has_undo = bool(
                re.search(r"(?:undo|revert|cancel|go.back|edit)", html_lower)
            )
            has_validation = bool(
                re.search(r"(?:validate|error|required)", html_lower)
            )

            if not has_confirmation and not has_undo and not has_validation:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="form",
                    issue=(
                        "Form submission has no apparent error prevention: "
                        "no confirmation, review, or undo mechanism"
                    ),
                    impact="Users may submit incorrect data with no way to correct.",
                    recommendation=(
                        "Add review step, validation before submission, "
                        "or undo capability after submission."
                    ),
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.4
        return conformance, confidence, findings


class Check_3_3_9(BaseCheck):
    """SC 3.3.9 Accessible Authentication (Enhanced) (Level AAA, WCAG 2.2)."""

    criterion_id = "3.3.9"
    criterion_name = "Accessible Authentication (Enhanced)"
    # Applicability (does the page have an authentication step?) is a
    # meaning judgment — a keyword scan must not hard-gate it.
    ai_judged_applicability = True
    level = "AAA"
    wcag_versions = ["2.2"]
    guideline = "3.3 Input Assistance"
    principle = "3. Understandable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "A cognitive function test is not required for any step in an "
        "authentication process unless that step provides at least one "
        "of: Alternative, Mechanism."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        html_lower = (capture_data.html or "").lower()
        return bool(
            "login" in html_lower
            or "sign in" in html_lower
            or "password" in html_lower
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html_lower = (capture_data.html or "").lower()

        # At AAA Enhanced, even object recognition and personal content
        # tests are not allowed (stricter than 3.3.8)
        has_captcha = bool(capture_data.captchas) or "captcha" in html_lower
        has_puzzle = bool(re.search(r"(?:puzzle|select.*image)", html_lower))
        has_image_recognition = bool(
            re.search(r"(?:which.*image|select.*picture|identify.*object)", html_lower)
        )

        password_fields = [
            f for f in capture_data.form_fields
            if (f.get("type") or "").lower() == "password"
        ]

        for pf in password_fields:
            selector = pf.get("selector", "input[type=password]")
            paste_blocked = pf.get("paste_blocked", False)
            autocomplete = (pf.get("autocomplete") or "").lower()

            # Password entry is a cognitive test (remembering)
            # Must allow paste and password managers at AAA
            if paste_blocked:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Password field blocks paste (AAA: must support mechanisms)",
                    impact="Users relying on password managers are blocked.",
                    recommendation="Allow paste in password fields.",
                    severity=Severity.HIGH,
                ))

        if has_captcha or has_puzzle or has_image_recognition:
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue=(
                    "Cognitive function test detected in authentication "
                    "(AAA: no object recognition or personal content tests allowed)"
                ),
                impact="All cognitive function tests fail at AAA enhanced level.",
                recommendation=(
                    "Remove all cognitive tests from authentication. "
                    "Provide passkey, magic link, or SSO alternatives."
                ),
                severity=Severity.HIGH,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.5
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_3_3_5(),
        Check_3_3_6(),
        Check_3_3_9(),
    ]
