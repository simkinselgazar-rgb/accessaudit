"""WCAG Guideline 1.4 - Distinguishable AAA checks."""
from __future__ import annotations

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_1_4_6(BaseCheck):
    """SC 1.4.6 Contrast (Enhanced) (Level AAA)."""

    criterion_id = "1.4.6"
    criterion_name = "Contrast (Enhanced)"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.4 Distinguishable"
    principle = "1. Perceivable"
    ict_baseline = ""
    tt_tests = []
    # Text-on-background contrast is measured per text node by ANDI.
    measurement_sources = {"contrast_ratio": ("andi_contrast_results", "ratio")}
    normative_text = (
        "The visual presentation of text and images of text has a "
        "contrast ratio of at least 7:1, except for: Large Text "
        "(at least 4.5:1), Incidental, Logotypes."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.colors)

    def get_image_context(self, capture_data: CaptureData) -> str:
        """Pass ANDI per-text-node ratios as ground truth to the visual AI.

        SC 1.4.6 (AAA) thresholds: 7:1 normal, 4.5:1 large.
        """
        lines: list[str] = []
        andi_block = self._format_andi_image_context(capture_data, is_aaa=True)
        if andi_block:
            lines.append(andi_block)

        base_context = super().get_image_context(capture_data)
        if base_context:
            if lines:
                lines.append("")
            lines.append(base_context)

        return "\n".join(lines)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        # Authoritative source for SC 1.4.6 (AAA) contrast findings is
        # ANDI cANDI via BaseCheck._extract_andi_contrast_findings, which
        # recomputes pass/fail at the AAA threshold (4.5:1 large /
        # 7.0:1 normal) using the same recorded ratios. The legacy
        # element-walker is removed for the same bg-image-detection bug
        # described in checks_1_4.py:Check_1_4_3.run_programmatic.
        return ConformanceLevel.SUPPORTS, 0.95, []


class Check_1_4_7(BaseCheck):
    """SC 1.4.7 Low or No Background Audio (Level AAA)."""

    criterion_id = "1.4.7"
    criterion_name = "Low or No Background Audio"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.4 Distinguishable"
    principle = "1. Perceivable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "For prerecorded audio-only content that (1) contains primarily "
        "speech in the foreground, (2) is not an audio CAPTCHA or audio "
        "logo, and (3) is not vocalization intended to be primarily "
        "musical expression, at least one of the following is true: "
        "No Background, Turn Off, 20 dB."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return any(
            (m.get("tag") or m.get("tagName") or "").lower() == "audio"
            for m in capture_data.media
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        # Cannot determine background audio levels programmatically
        findings: list[Finding] = []
        for m in capture_data.media:
            if (m.get("tag") or m.get("tagName") or "").lower() != "audio":
                continue
            selector = m.get("selector", "audio element")
            findings.append(Finding(
                id=_make_finding_id(),
                element=selector,
                issue="Audio content detected; background audio level cannot be verified programmatically",
                impact="Users with hearing difficulties may struggle with background noise.",
                recommendation=(
                    "Ensure background sounds are at least 20 dB lower than "
                    "foreground speech, or provide a mechanism to turn off background audio."
                ),
                severity=Severity.INFO,
            ))
        conformance = ConformanceLevel.NOT_EVALUATED if findings else ConformanceLevel.NOT_APPLICABLE
        return conformance, 0.3, findings


class Check_1_4_8(BaseCheck):
    """SC 1.4.8 Visual Presentation (Level AAA)."""

    criterion_id = "1.4.8"
    criterion_name = "Visual Presentation"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.4 Distinguishable"
    principle = "1. Perceivable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "For the visual presentation of blocks of text, a mechanism is "
        "available to achieve the following: foreground and background "
        "colors can be selected, width is no more than 80 characters, "
        "text is not justified, line spacing is at least space-and-a-half, "
        "text can be resized without assistive technology up to 200%."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        for style in capture_data.computed_styles:
            selector = style.get("selector", "")
            has_text = style.get("has_text_content", False)
            if not has_text:
                continue

            # Check text-align: justify
            text_align = style.get("text_align", style.get("text-align", ""))
            if text_align == "justify":
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Text block uses text-align: justify",
                    impact="Justified text creates uneven spacing that impairs readability for some users.",
                    recommendation="Use text-align: left (or start) instead of justify.",
                    severity=Severity.LOW,
                ))

            # Check line-height < 1.5
            line_height = style.get("line_height", style.get("line-height", ""))
            if line_height:
                try:
                    lh_val = float(str(line_height).replace("px", "").strip())
                    font_size = style.get("font_size", style.get("font-size", "16"))
                    fs_val = float(str(font_size).replace("px", "").strip())
                    if fs_val > 0 and lh_val / fs_val < 1.5:
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=selector,
                            issue=f"Line height {lh_val/fs_val:.2f}x is less than 1.5x",
                            impact="Tight line spacing reduces readability.",
                            recommendation="Set line-height to at least 1.5.",
                            severity=Severity.LOW,
                        ))
                except (ValueError, ZeroDivisionError):
                    pass

            # Check max-width for text blocks (>80ch)
            max_width = style.get("max_width", style.get("max-width", ""))
            width = style.get("width", "")
            if not max_width or max_width == "none":
                # If width is very large in pixels, flag it
                try:
                    w_val = float(str(width).replace("px", "").strip())
                    if w_val > 700:  # Roughly 80ch at default font size
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=selector,
                            issue=f"Text block width ({w_val}px) may exceed 80 characters",
                            impact="Very wide text blocks are hard to read.",
                            recommendation="Set max-width to 80ch or roughly 700px.",
                            severity=Severity.LOW,
                        ))
                except (ValueError, TypeError):
                    pass

        conformance = self._determine_conformance(findings)
        confidence = 0.5
        return conformance, confidence, findings


class Check_1_4_9(BaseCheck):
    """SC 1.4.9 Images of Text (No Exception) (Level AAA)."""

    criterion_id = "1.4.9"
    criterion_name = "Images of Text (No Exception)"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.4 Distinguishable"
    principle = "1. Perceivable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "Images of text are only used for pure decoration or where a "
        "particular presentation of text is essential to the information "
        "being conveyed."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.images)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        for img in capture_data.images:
            selector = img.get("selector", "img")
            has_text = img.get("contains_text", False)
            if has_text:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Image contains text (no exception at AAA level)",
                    impact="Text in images cannot be customized by users.",
                    recommendation=(
                        "Replace with real HTML text unless the specific "
                        "presentation is essential (e.g., logotype)."
                    ),
                    severity=Severity.MEDIUM,
                ))
        conformance = self._determine_conformance(findings)
        confidence = 0.3
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_1_4_6(),
        Check_1_4_7(),
        Check_1_4_8(),
        Check_1_4_9(),
    ]
