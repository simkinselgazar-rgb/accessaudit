"""WCAG Guideline 3.1 - Readable AAA checks."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_3_1_3(BaseCheck):
    """SC 3.1.3 Unusual Words (Level AAA)."""

    criterion_id = "3.1.3"
    criterion_name = "Unusual Words"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.1 Readable"
    principle = "3. Understandable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "A mechanism is available for identifying specific definitions "
        "of words or phrases used in an unusual or restricted way, "
        "including idioms and jargon."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html_lower = (capture_data.html or "").lower()

        # Check for glossary / definition mechanisms
        has_glossary = bool(
            re.search(r"(?:glossary|definitions|terms)", html_lower)
        )
        has_dfn = "<dfn" in html_lower
        has_abbr = "<abbr" in html_lower
        has_dl = "<dl" in html_lower  # definition list

        if not has_glossary and not has_dfn and not has_abbr and not has_dl:
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue=(
                    "No mechanism for defining unusual words found "
                    "(no glossary, <dfn>, <abbr>, or <dl> elements)"
                ),
                impact="Users may not understand jargon or technical terms.",
                recommendation=(
                    "Provide a glossary, use <dfn> for first occurrences of "
                    "unusual terms, or link to definitions."
                ),
                severity=Severity.LOW,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.35
        return conformance, confidence, findings


class Check_3_1_4(BaseCheck):
    """SC 3.1.4 Abbreviations (Level AAA)."""

    criterion_id = "3.1.4"
    criterion_name = "Abbreviations"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.1 Readable"
    principle = "3. Understandable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "A mechanism for identifying the expanded form or meaning of "
        "abbreviations is available."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html = capture_data.html or ""

        # Find uppercase abbreviations in text (2+ capital letters)
        # Rough heuristic: words of 2-6 uppercase letters
        # Remove HTML tags first for text scanning
        text_only = re.sub(r"<[^>]+>", " ", html)
        abbreviations = set(re.findall(r"\b([A-Z]{2,6})\b", text_only))

        # Check if <abbr> tags are used for these
        abbr_tags = re.findall(r"<abbr[^>]*>([^<]+)</abbr>", html, re.IGNORECASE)
        abbr_texts = {a.strip().upper() for a in abbr_tags}

        # Filter common non-abbreviations
        common_words = {"THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL",
                        "CAN", "HER", "WAS", "ONE", "OUR", "OUT", "HAS", "HIS",
                        "HOW", "ITS", "LET", "MAY", "NEW", "NOW", "OLD", "SEE",
                        "WAY", "WHO", "DID", "GET", "PUT", "SAY", "SHE", "TOO",
                        "USE", "CSS", "HTML", "HTTP", "JSON", "XML", "API"}

        uncovered = abbreviations - abbr_texts - common_words
        if uncovered and len(uncovered) > 2:
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue=(
                    f"Abbreviations without <abbr> markup: "
                    f"{', '.join(sorted(uncovered))}"
                ),
                impact="Users may not understand abbreviations without expanded forms.",
                recommendation=(
                    "Wrap abbreviations in <abbr title=\"Expanded Form\"> "
                    "or provide a glossary."
                ),
                severity=Severity.LOW,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.4
        return conformance, confidence, findings


class Check_3_1_5(BaseCheck):
    """SC 3.1.5 Reading Level (Level AAA)."""

    criterion_id = "3.1.5"
    criterion_name = "Reading Level"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.1 Readable"
    principle = "3. Understandable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "When text requires reading ability more advanced than the lower "
        "secondary education level after removal of proper names and "
        "titles, supplemental content is available, or a version that "
        "does not require reading ability more advanced than the lower "
        "secondary education level."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        # Reading level analysis is primarily an AI task
        findings: list[Finding] = []
        html = capture_data.html or ""

        # Check if a simplified version is available
        html_lower = html.lower()
        has_easy_read = bool(
            re.search(r"(?:easy.read|simple.language|plain.language|"
                      r"simplified.version|easy.to.read)", html_lower)
        )

        if not has_easy_read:
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue="No simplified/plain language version detected",
                impact="Users with cognitive disabilities may struggle with complex text.",
                recommendation=(
                    "Provide a plain language summary or alternative version "
                    "for content above lower secondary education reading level."
                ),
                severity=Severity.INFO,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.25  # AI-driven check
        return conformance, confidence, findings


class Check_3_1_6(BaseCheck):
    """SC 3.1.6 Pronunciation (Level AAA)."""

    criterion_id = "3.1.6"
    criterion_name = "Pronunciation"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "3.1 Readable"
    principle = "3. Understandable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "A mechanism is available for identifying specific pronunciation "
        "of words where meaning of the words, in context, is ambiguous "
        "without knowing the pronunciation."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html_lower = (capture_data.html or "").lower()

        # Check for pronunciation aids
        has_ruby = "<ruby" in html_lower
        has_pronunciation = bool(
            re.search(r"(?:pronunciation|phonetic|ipa[:\s])", html_lower)
        )

        if not has_ruby and not has_pronunciation:
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue="No pronunciation mechanism found (no <ruby> or phonetic guides)",
                impact="Users may mispronounce ambiguous words, changing their meaning.",
                recommendation=(
                    "Use <ruby> elements for pronunciation guides, or provide "
                    "phonetic spellings for ambiguous words."
                ),
                severity=Severity.INFO,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.3
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_3_1_3(),
        Check_3_1_4(),
        Check_3_1_5(),
        Check_3_1_6(),
    ]
