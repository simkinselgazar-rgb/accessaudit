"""WCAG 2.2 new criteria for Guideline 3.3 - Input Assistance."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_3_3_7(BaseCheck):
    """SC 3.3.7 Redundant Entry (Level A, WCAG 2.2)."""

    criterion_id = "3.3.7"
    criterion_name = "Redundant Entry"
    level = "A"
    wcag_versions = ["2.2"]
    guideline = "3.3 Input Assistance"
    principle = "3. Understandable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "Information previously entered by or provided to the user that "
        "is required to be entered again in the same process is either: "
        "auto-populated, or available for the user to select."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.form_fields)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Detect potential redundant entry patterns
        # Look for "confirm" fields that duplicate previous entry
        confirm_patterns = [
            (r"confirm.*(?:email|password|phone)", "confirmation field"),
            (r"re.?enter.*(?:email|password|phone)", "re-entry field"),
            (r"verify.*(?:email|password|phone)", "verification field"),
        ]

        for field in capture_data.form_fields:
            selector = field.get("selector", "input")
            name = (field.get("name") or "").lower()
            label = (field.get("label") or "").lower()
            field_type = (field.get("type") or "").lower()
            autocomplete = (field.get("autocomplete") or "").lower()

            text = f"{name} {label}"

            # Confirm password is acceptable for security
            if field_type == "password":
                continue

            for pattern, desc in confirm_patterns:
                if re.search(pattern, text):
                    # Check if auto-populated
                    has_value = bool(field.get("value", ""))
                    has_autofill = autocomplete and autocomplete != "off"
                    if not has_value and not has_autofill:
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=selector,
                            issue=(
                                f"Possible redundant entry: {desc} "
                                f"(\"{label or name}\") is not auto-populated"
                            ),
                            impact=(
                                "Users with cognitive disabilities may struggle "
                                "to re-enter information they already provided."
                            ),
                            recommendation=(
                                "Auto-populate the field with previously entered "
                                "information, or provide a selection mechanism."
                            ),
                            severity=Severity.MEDIUM,
                        ))
                    break

        # Multi-step forms: check for address re-entry patterns
        shipping_fields = [
            f for f in capture_data.form_fields
            if "shipping" in ((f.get("name") or "") + (f.get("label") or "")).lower()
        ]
        billing_fields = [
            f for f in capture_data.form_fields
            if "billing" in ((f.get("name") or "") + (f.get("label") or "")).lower()
        ]
        if shipping_fields and billing_fields:
            # Check for "same as shipping" checkbox
            html_lower = (capture_data.html or "").lower()
            has_same_as = bool(
                re.search(r"same\s+as\s+(?:shipping|delivery|above)", html_lower)
            )
            if not has_same_as:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="form",
                    issue=(
                        "Billing and shipping address fields present without "
                        "'same as shipping' option"
                    ),
                    impact="Users must re-enter address information redundantly.",
                    recommendation=(
                        "Add a 'Same as shipping address' checkbox to "
                        "auto-populate billing fields."
                    ),
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.45
        return conformance, confidence, findings


class Check_3_3_8(BaseCheck):
    """SC 3.3.8 Accessible Authentication (Minimum) (Level AA, WCAG 2.2)."""

    criterion_id = "3.3.8"
    criterion_name = "Accessible Authentication (Minimum)"
    # Applicability (does the page have an authentication step?) is a
    # meaning judgment — a keyword scan must not hard-gate it.
    ai_judged_applicability = True
    level = "AA"
    wcag_versions = ["2.2"]
    guideline = "3.3 Input Assistance"
    principle = "3. Understandable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "A cognitive function test (such as remembering a password or "
        "solving a puzzle) is not required for any step in an authentication "
        "process unless that step provides at least one of the following: "
        "Alternative, Mechanism, Object Recognition, Personal Content."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        html_lower = (capture_data.html or "").lower()
        return bool(
            "login" in html_lower
            or "log in" in html_lower
            or "sign in" in html_lower
            or "signin" in html_lower
            or "authenticate" in html_lower
            or "password" in html_lower
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html_lower = (capture_data.html or "").lower()

        # Check for CAPTCHA on login
        has_captcha = bool(capture_data.captchas) or "captcha" in html_lower
        has_puzzle = bool(
            re.search(r"(?:puzzle|drag.*image|select.*image|identify)", html_lower)
        )

        # Check password field allows paste
        password_fields = [
            f for f in capture_data.form_fields
            if (f.get("type") or "").lower() == "password"
        ]

        for pf in password_fields:
            selector = pf.get("selector", "input[type=password]")
            autocomplete = (pf.get("autocomplete") or "").lower()
            paste_blocked = pf.get("paste_blocked", False)

            if paste_blocked:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Password field blocks paste (prevents password manager use)",
                    impact=(
                        "Users who rely on password managers cannot paste "
                        "passwords, forcing them to type from memory."
                    ),
                    recommendation=(
                        "Allow paste in password fields to support password "
                        "managers and reduce cognitive burden."
                    ),
                    severity=Severity.HIGH,
                ))

            if autocomplete == "off":
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Password field has autocomplete=\"off\"",
                    impact="Password managers may not fill this field automatically.",
                    recommendation=(
                        "Use autocomplete=\"current-password\" or "
                        "autocomplete=\"new-password\" to support password managers."
                    ),
                    severity=Severity.MEDIUM,
                ))

        if has_captcha:
            # Check for accessible alternatives
            has_alt_auth = bool(
                re.search(r"(?:magic.link|email.link|passkey|webauthn|sso|"
                          r"social.login|sign.in.with|oauth)", html_lower)
            )
            if not has_alt_auth:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="CAPTCHA",
                    issue="CAPTCHA present on authentication without accessible alternative",
                    impact=(
                        "Users with cognitive disabilities may not be able "
                        "to complete the CAPTCHA to authenticate."
                    ),
                    recommendation=(
                        "Provide an alternative authentication path that does "
                        "not require a cognitive function test."
                    ),
                    severity=Severity.HIGH,
                ))

        if has_puzzle:
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue="Puzzle-based authentication detected",
                impact="Cognitive function test barriers for users with cognitive disabilities.",
                recommendation=(
                    "Provide alternative authentication methods that do "
                    "not require cognitive function tests."
                ),
                severity=Severity.HIGH,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.55
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_3_3_7(),
        Check_3_3_8(),
    ]
