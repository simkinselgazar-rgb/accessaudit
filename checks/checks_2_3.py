"""WCAG Guideline 2.3 - Seizures and Physical Reactions (A) checks."""
from __future__ import annotations

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
    TTResult,
    TTSubTestResult,
)


class Check_2_3_1(BaseCheck):
    """SC 2.3.1 Three Flashes or Below Threshold (Level A)."""

    criterion_id = "2.3.1"
    criterion_name = "Three Flashes or Below Threshold"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.3 Seizures and Physical Reactions"
    principle = "2. Operable"
    ict_baseline = "9"
    tt_tests = ["9.A"]
    normative_text = (
        "Web pages do not contain anything that flashes more than three "
        "times in any one second period, or the flash is below the "
        "general flash and red flash thresholds."
    )
    web_only = True

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send the observation video so the VL model can detect flashing."""
        return capture_data.observation_video_path or None

    def is_applicable(self, capture_data: CaptureData) -> bool:
        dynamic = capture_data.dynamic_content or {}
        return bool(
            capture_data.flash_analysis
            or capture_data.observation_video_path
            or capture_data.media
            or dynamic.get("hasAnimations", False)
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        flash = capture_data.flash_analysis
        if flash:
            # Use the actual keys produced by frame_extractor.analyze_flash_rate:
            #   has_violation, max_flashes_per_second, general_flash_violations,
            #   max_luminance_delta, red_flash_events, flash_events
            max_flashes = flash.get("max_flashes_per_second", 0)
            has_violation = flash.get("has_violation", False)
            general_flash_violations = flash.get("general_flash_violations", 0)
            max_luminance_delta = flash.get("max_luminance_delta", 0.0)
            red_flash_events = flash.get("red_flash_events", [])
            has_red_flash = len(red_flash_events) > 0

            # Also honour the legacy keys if present (forward-compat)
            if not has_violation:
                has_violation = flash.get("threshold_exceeded", False)
            flash_regions = flash.get("flash_regions", [])

            # WCAG small area exception: flash is acceptable if the
            # combined area of simultaneous flashes is <= 341x256 px
            # (about 21,824 sq px ~ 25% of 1024x768).
            flash_area_px = flash.get("flash_area_px", 0)
            small_area_exempt = 0 < flash_area_px <= 21824

            if (has_violation or max_flashes > 3) and not small_area_exempt:
                # -- PRIMARY FAILURE: flash_analysis.passed == False equivalent --
                issue_parts = [
                    f"Content flashes more than 3 times per second "
                    f"(detected {max_flashes:.1f} flashes/sec)"
                ]
                if has_red_flash:
                    issue_parts.append(
                        f"RED FLASH detected ({len(red_flash_events)} event(s))"
                    )
                if general_flash_violations:
                    issue_parts.append(
                        f"{general_flash_violations} general-flash luminance "
                        f"violations (max delta {max_luminance_delta:.4f})"
                    )
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="page (observation video)",
                    issue=". ".join(issue_parts),
                    impact=(
                        "Content that flashes can cause seizures in users "
                        "with photosensitive epilepsy."
                    ),
                    recommendation=(
                        "Reduce flash rate to 3 or fewer per second, or "
                        "ensure flashes are below the general flash and "
                        "red flash thresholds, or keep the flashing area "
                        "below 341x256 pixels."
                    ),
                    severity=Severity.HIGH,
                ))
            elif (has_violation or max_flashes > 3) and small_area_exempt:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="page (observation video)",
                    issue=(
                        f"Content flashes > 3 times/sec but flashing area "
                        f"({flash_area_px}px\u00b2) is below the small area "
                        f"threshold (\u2264 341\u00d7256 px = 21,824px\u00b2)"
                    ),
                    impact="Flash is below the small area threshold per WCAG 2.3.1.",
                    recommendation="Continue to monitor; area must stay below threshold.",
                    severity=Severity.INFO,
                ))

                for region in flash_regions:
                    area = region.get("area_percent", 0)
                    location = region.get("location", "")
                    if area > 0:
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=f"page region ({location})" if location else "page region",
                            issue=(
                                f"Flash region covers {area:.1f}% of viewport "
                                f"(>25% triggers seizure risk)"
                            ),
                            impact="Large flashing areas increase seizure risk.",
                            recommendation="Reduce the flashing area or flash rate.",
                            severity=Severity.HIGH if area > 25 else Severity.MEDIUM,
                        ))

            elif max_flashes > 0:
                # Below threshold -- informational
                detail_parts = [f"{max_flashes:.1f} flashes/sec"]
                if general_flash_violations:
                    detail_parts.append(
                        f"{general_flash_violations} general-flash delta(s)"
                    )
                if max_luminance_delta:
                    detail_parts.append(
                        f"max luminance delta {max_luminance_delta:.4f}"
                    )
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="page (observation video)",
                    issue=(
                        f"Some flashing detected ({', '.join(detail_parts)}) "
                        f"but below threshold"
                    ),
                    impact="Flash rate is within acceptable limits.",
                    recommendation="Continue to monitor flash rates with content changes.",
                    severity=Severity.INFO,
                ))

            # Report red flash events even when overall rate is below 3/sec
            if red_flash_events and max_flashes <= 3 and not has_violation:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="page (observation video)",
                    issue=(
                        f"{len(red_flash_events)} red flash transition(s) "
                        f"detected (flash rate {max_flashes:.1f}/sec is "
                        f"below threshold, but saturated red is present)"
                    ),
                    impact=(
                        "Red flashes pose an elevated seizure risk even at "
                        "lower flash rates."
                    ),
                    recommendation=(
                        "Reduce or eliminate saturated red colour transitions. "
                        "Red flashes have a lower safe threshold."
                    ),
                    severity=Severity.MEDIUM,
                ))

        # -- Enhance with dynamic_content.hasAnimations --
        dynamic = capture_data.dynamic_content or {}
        if dynamic.get("hasAnimations", False):
            # If flash_analysis didn't run or didn't find a violation,
            # flag that active animations exist which could potentially flash
            has_flash_data = bool(flash)
            has_flash_violation = any(
                f.severity == Severity.HIGH for f in findings
            )
            if not has_flash_data:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="page",
                    issue=(
                        "Active CSS/JS animations detected at runtime but "
                        "no flash analysis data is available"
                    ),
                    impact=(
                        "Animations may contain flashing content that could "
                        "trigger seizures; without flash analysis this cannot "
                        "be confirmed."
                    ),
                    recommendation=(
                        "Run a flash analysis on the page recording, or "
                        "manually verify that animations do not flash more "
                        "than 3 times per second."
                    ),
                    severity=Severity.MEDIUM,
                ))
            elif not has_flash_violation:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="page",
                    issue=(
                        "Active animations detected at runtime; flash "
                        "analysis did not find a violation, but animations "
                        "may change over time"
                    ),
                    impact=(
                        "Dynamic animations can vary in behaviour; a "
                        "snapshot analysis may not capture all states."
                    ),
                    recommendation=(
                        "Re-test if animation content changes. Consider "
                        "honouring prefers-reduced-motion to disable "
                        "animations for sensitive users."
                    ),
                    severity=Severity.INFO,
                ))

        # Check for CSS animations that might flash. The previous logic
        # produced false positives on every page that uses FontAwesome
        # (which defines @keyframes fa-beat, fa-bounce, fa-fade with
        # short durations as decorative icon animations -- NOT flashing).
        # Two guards now apply:
        #   (1) The flash analyzer's actual capture is ground truth. If
        #       it observed 0 flashes/s during the observation window,
        #       a static CSS rule cannot override that. Skip the
        #       CSS-pattern check entirely.
        #   (2) Only flag keyframe rules that actually toggle
        #       opacity/visibility/display from 0 -> 1 (or display:none
        #       -> block) at sub-frame intervals. Scale/transform
        #       animations (the FontAwesome pattern) do not flash.
        flash_observed = False
        if capture_data.flash_analysis:
            flash_observed = (
                (capture_data.flash_analysis.get("flashes_per_second") or 0) > 0
                or (capture_data.flash_analysis.get("general_flashes") or 0) > 0
                or (capture_data.flash_analysis.get("red_flashes") or 0) > 0
            )

        if flash_observed:
            html_lower = (capture_data.html or "").lower()
            if "animation" in html_lower and ("opacity" in html_lower or "visibility" in html_lower):
                import re
                # Look for keyframes that actually toggle visibility/
                # opacity between 0 and 1 (or none/block). Scale/
                # transform-only animations (icon bounce, beat, etc.)
                # do not constitute flashing.
                keyframe_blocks = re.findall(
                    r"@keyframes\s+[\w-]+\s*\{[^}]*\}", html_lower
                )
                for kf in keyframe_blocks:
                    # Crude but effective: keyframe must contain BOTH
                    # opacity:0 (or visibility:hidden) AND opacity:1
                    # (or visibility:visible) to be a flashing pattern.
                    has_off = (
                        "opacity:0" in kf.replace(" ", "")
                        or "opacity: 0" in kf
                        or "visibility:hidden" in kf.replace(" ", "")
                    )
                    has_on = (
                        "opacity:1" in kf.replace(" ", "")
                        or "opacity: 1" in kf
                        or "visibility:visible" in kf.replace(" ", "")
                    )
                    if not (has_off and has_on):
                        continue
                    # Find an animation rule referencing a short duration
                    rapid_anim = re.search(
                        r"animation[^;}]*?(\d*\.\d+|\d+)s",
                        html_lower,
                    )
                    if not rapid_anim:
                        continue
                    try:
                        duration = float(rapid_anim.group(1))
                    except ValueError:
                        continue
                    if duration <= 0 or duration >= 0.33:
                        continue
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element="<style>",
                        issue=(
                            f"CSS animation with duration {duration}s "
                            f"toggles opacity/visibility, which combined "
                            f"with the observed flashing in the capture "
                            f"video may exceed three flashes per second."
                        ),
                        impact=(
                            "Rapid opacity/visibility changes may "
                            "trigger seizures in users with photosensitive "
                            "epilepsy."
                        ),
                        recommendation=(
                            "Ensure animation duration is at least 0.33s "
                            "and does not produce more than 3 flashes/sec."
                        ),
                        severity=Severity.MEDIUM,
                    ))
                    break  # one finding per page is enough

        conformance = self._determine_conformance(findings)
        confidence = 0.7 if capture_data.flash_analysis else 0.4
        return conformance, confidence, findings

    def _generate_tt_results(
        self, findings: list[Finding], capture_data: CaptureData
    ) -> list[TTSubTestResult]:
        not_app = not self.is_applicable(capture_data)
        has_fail = any(
            "more than 3" in f.issue.lower() or "threshold" in f.issue.lower()
            for f in findings if f.severity == Severity.HIGH
        )
        return [
            TTSubTestResult(
                tt_id="9.A",
                name="Content does not flash more than 3 times per second",
                result=TTResult.DNA if not_app else TTResult.FAIL if has_fail else TTResult.PASS,
            ),
        ]


def get_checks() -> list[BaseCheck]:
    return [Check_2_3_1()]
