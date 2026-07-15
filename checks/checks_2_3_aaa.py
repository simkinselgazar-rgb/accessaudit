"""WCAG Guideline 2.3 - Seizures and Physical Reactions AAA checks."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_2_3_2(BaseCheck):
    """SC 2.3.2 Three Flashes (Level AAA)."""

    criterion_id = "2.3.2"
    criterion_name = "Three Flashes"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.3 Seizures and Physical Reactions"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "Web pages do not contain anything that flashes more than three "
        "times in any one second period."
    )

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send page observation video — VL model watches for any
        flashing content (absolute 3-per-second threshold, no exceptions)."""
        if capture_data.observation_video_path:
            return capture_data.observation_video_path
        return None

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.flash_analysis or capture_data.observation_video_path)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        flash = capture_data.flash_analysis
        if flash:
            max_flashes = flash.get("max_flashes_per_second", 0)
            if max_flashes > 3:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="page",
                    issue=(
                        f"Content flashes {max_flashes} times/sec "
                        f"(AAA: NO flash above 3/sec, no threshold exception)"
                    ),
                    impact="Any flash above 3/sec is a failure at AAA level.",
                    recommendation="Eliminate all flashing above 3 times per second.",
                    severity=Severity.HIGH,
                ))
            elif max_flashes > 0:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="page",
                    issue=f"Some flashing detected ({max_flashes}/sec), within AAA threshold",
                    impact="Flash rate is acceptable.",
                    recommendation="Continue monitoring.",
                    severity=Severity.INFO,
                ))
        conformance = self._determine_conformance(findings)
        confidence = 0.6 if flash else 0.3
        return conformance, confidence, findings


class Check_2_3_3(BaseCheck):
    """SC 2.3.3 Animation from Interactions (Level AAA, WCAG 2.1/2.2)."""

    criterion_id = "2.3.3"
    criterion_name = "Animation from Interactions"
    # Applicability (does the page animate on interaction?) is a meaning
    # judgment — a keyword scan must not hard-gate it.
    ai_judged_applicability = True
    # A finding asserting the page has animation is checked against the
    # deterministic dynamic-content probe.
    measurement_sources = {
        "hasAnimations": ("dynamic_content", "hasAnimations"),
    }
    level = "AAA"
    wcag_versions = ["2.1", "2.2"]
    guideline = "2.3 Seizures and Physical Reactions"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "Motion animation triggered by interaction can be disabled, "
        "unless the animation is essential to the functionality or the "
        "information being conveyed."
    )

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send page observation video — VL model watches for motion
        animations triggered by interactions and checks if they can
        be disabled (e.g., prefers-reduced-motion support)."""
        if capture_data.observation_video_path:
            return capture_data.observation_video_path
        return None

    def is_applicable(self, capture_data: CaptureData) -> bool:
        html = capture_data.html or ""
        script = capture_data.script_content or ""
        return bool(
            "animation" in html.lower()
            or "transition" in html.lower()
            or "animate" in script.lower()
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html = capture_data.html or ""
        script = capture_data.script_content or ""
        html_lower = html.lower()

        has_animations = bool(
            "transition" in html_lower
            or "@keyframes" in html_lower
            or re.search(r"\.animate\s*\(", script)
        )

        has_reduced_motion = "prefers-reduced-motion" in html_lower

        if has_animations and not has_reduced_motion:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<style>/<script>",
                issue=(
                    "Motion animations detected without "
                    "prefers-reduced-motion support"
                ),
                impact=(
                    "Users with vestibular disorders may experience "
                    "dizziness or nausea from motion animations."
                ),
                recommendation=(
                    "Respect prefers-reduced-motion media query and disable "
                    "non-essential animations when the user has requested reduced motion."
                ),
                severity=Severity.MEDIUM,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.5
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_2_3_2(),
        Check_2_3_3(),
    ]
