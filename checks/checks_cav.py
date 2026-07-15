"""Conforming Alternate Version checks (TT tests 1.A through 1.D)."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
    TTResult,
    TTSubTestResult,
)


class Check_CAV(BaseCheck):
    """Conforming Alternate Version (Trusted Tester Baseline).

    Not tied to a specific WCAG SC but part of the Trusted Tester
    baseline. Tests 1.A-1.D check whether a conforming alternate
    version exists and is accessible.
    """

    criterion_id = "CAV"
    criterion_name = "Conforming Alternate Version"
    # Applicability (does a conforming alternate version exist?) is a
    # meaning judgment — a regex scan of the HTML must not hard-gate it.
    ai_judged_applicability = True
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "Conformance Requirement"
    principle = "Conformance"
    ict_baseline = "1"
    tt_tests = ["1.A", "1.B", "1.C", "1.D"]
    normative_text = (
        "When a conforming alternate version of a Web page is provided, "
        "the non-conforming version must have an accessible mechanism to "
        "reach the conforming version. The conforming version must be "
        "up-to-date and contain the same information and functionality."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        # Check if there are links to alternate versions
        html_lower = (capture_data.html or "").lower()
        return bool(
            re.search(r"(?:accessible.version|text.only|accessibility.mode|"
                      r"high.contrast|simplified.version|alternative.version|"
                      r"mobile.version)", html_lower)
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html_lower = (capture_data.html or "").lower()

        # Detect alternate version links
        alt_version_patterns = [
            (r"(?:accessible|accessibility)\s*(?:version|mode|view)", "accessible version"),
            (r"text[\s-]*only\s*(?:version|view|mode|site)", "text-only version"),
            (r"high[\s-]*contrast\s*(?:version|mode|view)", "high contrast version"),
            (r"simplified?\s*(?:version|mode|view)", "simplified version"),
        ]

        alt_versions_found: list[str] = []
        for pattern, desc in alt_version_patterns:
            if re.search(pattern, html_lower):
                alt_versions_found.append(desc)

        if alt_versions_found:
            # If alternate versions exist, check they are accessible
            # This requires checking the alternate page, so we flag for review

            # TT 1.A: Is there an accessible mechanism to reach the conforming version?
            for alt in alt_versions_found:
                # Check if the link to the alternate version is accessible
                link_found = False
                for link in capture_data.links:
                    link_text = (link.get("text") or "").lower()
                    if any(keyword in link_text for keyword in alt.split()):
                        link_found = True
                        # Verify the link itself is accessible
                        has_accessible_name = bool(
                            (link.get("text") or "").strip()
                            or link.get("aria_label", link.get("aria-label", ""))
                        )
                        if not has_accessible_name:
                            findings.append(Finding(
                                id=_make_finding_id(),
                                element=link.get("selector", "a"),
                                issue=(
                                    f"Link to {alt} lacks accessible name"
                                ),
                                impact=(
                                    "Users cannot reach the alternate version "
                                    "if the mechanism itself is not accessible."
                                ),
                                recommendation=(
                                    "Ensure the link to the alternate version "
                                    "has descriptive, accessible text."
                                ),
                                severity=Severity.HIGH,
                            ))
                        break

                if not link_found:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element="body",
                        issue=(
                            f"Reference to {alt} found in content but no "
                            f"accessible link to it detected"
                        ),
                        impact="Users cannot navigate to the alternate version.",
                        recommendation=(
                            "Provide a clear, keyboard-accessible link to "
                            "the alternate version."
                        ),
                        severity=Severity.MEDIUM,
                    ))

        conformance = self._determine_conformance(findings)
        # Low confidence - alternate version testing requires crawling
        confidence = 0.35
        return conformance, confidence, findings

    def _generate_tt_results(
        self, findings: list[Finding], capture_data: CaptureData
    ) -> list[TTSubTestResult]:
        not_app = not self.is_applicable(capture_data)

        has_mechanism_issue = any(
            "no accessible link" in f.issue.lower()
            or "lacks accessible name" in f.issue.lower()
            for f in findings
        )

        return [
            TTSubTestResult(
                tt_id="1.A",
                name="Accessible mechanism to conforming version exists",
                result=(
                    TTResult.DNA if not_app
                    else TTResult.FAIL if has_mechanism_issue
                    else TTResult.PASS
                ),
            ),
            TTSubTestResult(
                tt_id="1.B",
                name="Non-conforming version does not interfere",
                result=TTResult.DNA if not_app else TTResult.NOT_TESTED,
            ),
            TTSubTestResult(
                tt_id="1.C",
                name="Conforming version is up to date",
                result=TTResult.DNA if not_app else TTResult.NOT_TESTED,
            ),
            TTSubTestResult(
                tt_id="1.D",
                name="Conforming version contains same information",
                result=TTResult.DNA if not_app else TTResult.NOT_TESTED,
            ),
        ]


def get_checks() -> list[BaseCheck]:
    return [Check_CAV()]
