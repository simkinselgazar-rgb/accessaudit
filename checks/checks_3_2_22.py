"""WCAG 2.2 new criteria for Guideline 3.2 - Predictable."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_3_2_6(BaseCheck):
    """SC 3.2.6 Consistent Help (Level A, WCAG 2.2)."""

    criterion_id = "3.2.6"
    criterion_name = "Consistent Help"
    level = "A"
    wcag_versions = ["2.2"]
    guideline = "3.2 Predictable"
    principle = "3. Understandable"
    ict_baseline = ""
    tt_tests = []
    # "Consistent" help is defined across a set of pages -- the criterion
    # is about the help mechanism's relative order ACROSS pages.
    requires_multipage = True
    normative_text = (
        "If a Web page within a set of Web pages contains any of the "
        "following help mechanisms, and those mechanisms are repeated on "
        "multiple Web pages within the set, they occur in the same "
        "relative order to other page content, unless a change is "
        "initiated by the user: Human contact details, Human contact "
        "mechanism, Self-help option, A fully automated contact mechanism."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html_lower = (capture_data.html or "").lower()

        # Detect help mechanisms on the page
        help_mechanisms: list[str] = []

        help_patterns = {
            "contact_info": r"(?:contact\s+us|get\s+in\s+touch|reach\s+us|phone|email\s+us)",
            "chat": r"(?:live\s+chat|chat\s+with\s+us|support\s+chat|help\s+chat)",
            "faq": r"(?:faq|frequently\s+asked|help\s+center|help\s+centre|knowledge\s+base)",
            "support_form": r"(?:support\s+form|contact\s+form|help\s+form|submit\s+a\s+ticket)",
        }

        for mechanism, pattern in help_patterns.items():
            if re.search(pattern, html_lower):
                help_mechanisms.append(mechanism)

        # Check for phone/email links
        has_tel = bool(re.search(r'href\s*=\s*["\']tel:', html_lower))
        has_mailto = bool(re.search(r'href\s*=\s*["\']mailto:', html_lower))
        if has_tel:
            help_mechanisms.append("phone_link")
        if has_mailto:
            help_mechanisms.append("email_link")

        if not help_mechanisms:
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue="No help mechanism detected on this page",
                impact=(
                    "Users who need help cannot find consistent support "
                    "across the site."
                ),
                recommendation=(
                    "Add a consistent help mechanism (contact info, chat, "
                    "FAQ, or support form) that appears in the same location "
                    "on every page."
                ),
                severity=Severity.INFO,
            ))

        # Multi-page consistency cannot be verified on a single page
        conformance = self._determine_conformance(findings)
        confidence = 0.3  # Multi-page check; very limited on single page
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [Check_3_2_6()]
