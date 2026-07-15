"""WCAG Guideline 3.1 - Readable (A/AA) checks."""
from __future__ import annotations

import re

from checks.base import BaseCheck
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
    TTResult,
    TTSubTestResult,
)

# Valid BCP 47 language tag pattern (simplified)
_LANG_RE = re.compile(
    r"^[a-zA-Z]{2,3}"        # primary language subtag
    r"(?:-[a-zA-Z]{4})?"     # optional script subtag
    r"(?:-[a-zA-Z]{2}|\d{3})?",  # optional region subtag
)


def _is_valid_lang(lang: str) -> bool:
    """Return True if the string looks like a valid BCP 47 language tag."""
    if not lang or not lang.strip():
        return False
    return bool(_LANG_RE.match(lang.strip()))


class Check_3_1_1(BaseCheck):
    """SC 3.1.1 Language of Page (Level A)."""

    criterion_id = "3.1.1"
    criterion_name = "Language of Page"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.1 Readable"
    principle = "3. Understandable"
    ict_baseline = "15"
    tt_tests = ["15.B"]
    normative_text = (
        "The default human language of each Web page can be "
        "programmatically determined."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return True

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        # Authoritative source for SC 3.1.1 is ANDI sANDI via
        # BaseCheck._extract_andi_lang_findings: validates html lang +
        # xml:lang against BCP 47 with full per-segment data. The legacy
        # capture_data.page_language path and the regex-from-raw-HTML
        # fallback are removed because the new ANDI extractor populates
        # capture_data.andi_lang_results unconditionally and produces the
        # same findings at higher fidelity (selectors, validity flags,
        # redundant detection, hidden segments).
        return ConformanceLevel.SUPPORTS, 0.95, []

    def _generate_tt_results(
        self, findings: list[Finding], capture_data: CaptureData
    ) -> list[TTSubTestResult]:
        has_fail = any(f.severity in (Severity.HIGH, Severity.MEDIUM) for f in findings)
        return [
            TTSubTestResult(
                tt_id="15.B",
                name="Page language is programmatically determined",
                result=TTResult.FAIL if has_fail else TTResult.PASS,
            ),
        ]


class Check_3_1_2(BaseCheck):
    """SC 3.1.2 Language of Parts (Level AA)."""

    criterion_id = "3.1.2"
    criterion_name = "Language of Parts"
    level = "AA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.1 Readable"
    principle = "3. Understandable"
    ict_baseline = "15"
    tt_tests = ["15.C"]
    normative_text = (
        "The human language of each passage or phrase in the content can "
        "be programmatically determined except for proper names, technical "
        "terms, words of indeterminate language, and words or phrases that "
        "have become part of the vernacular of the immediately surrounding "
        "text."
    )
    off_scope_keywords = {
        "page_language": [
            "html element", "<html>", "html lang attribute",
            "default human language", "page language",
            "missing lang attribute",
        ],
    }

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        # Authoritative source for SC 3.1.2 is ANDI sANDI via
        # BaseCheck._extract_andi_lang_findings: per-segment selector +
        # BCP 47 validity + xml:lang mismatch + redundant lang detection
        # + hidden-segment flag. Returns 0.45 confidence so the AI/judge
        # pipeline still runs to catch missing-lang on foreign passages
        # (which deterministic checks cannot detect — needs language ID).
        return ConformanceLevel.SUPPORTS, 0.45, []


def get_checks() -> list[BaseCheck]:
    return [
        Check_3_1_1(),
        Check_3_1_2(),
    ]
