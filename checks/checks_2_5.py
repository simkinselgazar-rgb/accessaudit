"""WCAG Guideline 2.5 - Input Modalities (A) checks (WCAG 2.1/2.2)."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_2_5_1(BaseCheck):
    """SC 2.5.1 Pointer Gestures (Level A, WCAG 2.1/2.2)."""

    criterion_id = "2.5.1"
    criterion_name = "Pointer Gestures"
    # Applicability (does the page use multipoint / path-based gestures?)
    # is a meaning judgment — a keyword scan must not hard-gate it.
    ai_judged_applicability = True
    level = "A"
    wcag_versions = ["2.1", "2.2"]
    guideline = "2.5 Input Modalities"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "All functionality that uses multipoint or path-based gestures "
        "for operation can be operated with a single pointer without a "
        "path-based gesture, unless a multipoint or path-based gesture "
        "is essential."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        script = capture_data.script_content or ""
        html = capture_data.html or ""
        return bool(
            "touch" in script.lower()
            or "gesture" in script.lower()
            or "pinch" in script.lower()
            or "swipe" in script.lower()
            or "touchstart" in html.lower()
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        script = capture_data.script_content or ""
        html = capture_data.html or ""
        combined = script + html

        # Detect multipoint gesture patterns
        multipoint_patterns = [
            (r"(?:touches|targetTouches)\.length\s*(?:>=?|===?)\s*2", "two-finger gesture"),
            (r"pinch(?:zoom|start|move|end|In|Out)", "pinch gesture"),
            (r"(?:two|multi)\s*(?:finger|touch|point)", "multi-finger gesture"),
        ]

        # Detect path-based gesture patterns
        path_patterns = [
            (r"swipe(?:Left|Right|Up|Down|Start|End|Move)", "swipe gesture"),
            (r"drag(?:start|end|over|enter|leave)", "drag gesture"),
            (r"(?:touch|pointer)move", "path-based pointer tracking"),
        ]

        gesture_found = False
        for pattern, desc in multipoint_patterns:
            if re.search(pattern, combined, re.IGNORECASE):
                gesture_found = True
                # Check for single-pointer alternative
                has_alternative = bool(
                    re.search(r"(?:click|tap|button|single)", combined, re.IGNORECASE)
                )
                if not has_alternative:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element="<script>",
                        issue=f"Multipoint gesture detected ({desc}) without apparent single-pointer alternative",
                        impact="Users who cannot perform multi-finger gestures cannot operate this functionality.",
                        recommendation="Provide a single-pointer alternative (e.g., buttons for zoom in/out).",
                        severity=Severity.MEDIUM,
                    ))

        for pattern, desc in path_patterns:
            if re.search(pattern, combined, re.IGNORECASE):
                gesture_found = True
                has_button_alt = bool(
                    re.search(r"(?:button|click|tap|arrow)", combined, re.IGNORECASE)
                )
                if not has_button_alt:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element="<script>",
                        issue=f"Path-based gesture detected ({desc}) without apparent alternative",
                        impact="Users who cannot follow a specific path cannot operate this functionality.",
                        recommendation="Provide a single-click or single-tap alternative.",
                        severity=Severity.MEDIUM,
                    ))

        conformance = self._determine_conformance(findings)
        confidence = 0.45  # Script analysis is heuristic
        return conformance, confidence, findings


class Check_2_5_2(BaseCheck):
    """SC 2.5.2 Pointer Cancellation (Level A, WCAG 2.1/2.2)."""

    criterion_id = "2.5.2"
    criterion_name = "Pointer Cancellation"
    level = "A"
    wcag_versions = ["2.1", "2.2"]
    guideline = "2.5 Input Modalities"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "For functionality that can be operated using a single pointer, "
        "at least one of the following is true: No Down-Event, Abort or "
        "Undo, Up Reversal, Essential."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        script = capture_data.script_content or ""
        html = capture_data.html or ""
        return "mousedown" in (script + html).lower() or "touchstart" in (script + html).lower()

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        script = capture_data.script_content or ""
        html = capture_data.html or ""
        combined = script + html

        # Check for actions triggered on mousedown/touchstart (down event)
        # rather than click/mouseup/touchend (up event)
        down_patterns = [
            (r"addEventListener\s*\(\s*['\"]mousedown['\"]", "mousedown"),
            (r"addEventListener\s*\(\s*['\"]touchstart['\"]", "touchstart"),
            (r"onmousedown\s*=", "onmousedown attribute"),
            (r"ontouchstart\s*=", "ontouchstart attribute"),
        ]

        for pattern, desc in down_patterns:
            matches = re.findall(pattern, combined, re.IGNORECASE)
            if matches:
                # Check if there's also a click/mouseup handler (indicating proper handling)
                has_up_handler = bool(
                    re.search(r"addEventListener\s*\(\s*['\"](?:click|mouseup|touchend)['\"]",
                             combined, re.IGNORECASE)
                    or re.search(r"on(?:click|mouseup|touchend)\s*=", combined, re.IGNORECASE)
                )

                if not has_up_handler:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element="<script>",
                        issue=(
                            f"Action triggered on {desc} without "
                            f"corresponding up-event handler"
                        ),
                        impact=(
                            "Users cannot abort an accidental click by moving "
                            "the pointer off the target before releasing."
                        ),
                        recommendation=(
                            "Use click event (which fires on pointer release) "
                            "instead of mousedown/touchstart, or implement "
                            "abort/undo mechanisms."
                        ),
                        severity=Severity.MEDIUM,
                    ))

        conformance = self._determine_conformance(findings)
        confidence = 0.5
        return conformance, confidence, findings


class Check_2_5_3(BaseCheck):
    """SC 2.5.3 Label in Name (Level A, WCAG 2.1/2.2)."""

    criterion_id = "2.5.3"
    criterion_name = "Label in Name"
    # A finding asserting whether an element's accessible name includes
    # its visible text is checked against the ANDI interactive audit.
    measurement_sources = {
        "name_inc_visible": ("andi_interactive_results", "name_includes_visible"),
    }
    level = "A"
    wcag_versions = ["2.1", "2.2"]
    guideline = "2.5 Input Modalities"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "For user interface components with labels that include text or "
        "images of text, the name contains the text that is presented "
        "visually."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.form_fields or capture_data.links)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Check form fields
        for field in capture_data.form_fields:
            selector = field.get("selector", "input")
            visible_text = (field.get("visible_label") or field.get("label") or "").strip()
            accessible_name = (field.get("accessible_name") or "").strip()
            aria_label = (field.get("aria_label") or field.get("aria-label") or "").strip()

            # Use aria-label as accessible name if present
            if aria_label:
                accessible_name = aria_label

            if visible_text and accessible_name:
                # The accessible name must contain the visible text
                if visible_text.lower() not in accessible_name.lower():
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Visible label \"{visible_text}\" is not contained "
                            f"in the accessible name \"{accessible_name}\""
                        ),
                        impact=(
                            "Speech input users saying the visible label "
                            "cannot activate this control."
                        ),
                        recommendation=(
                            "Ensure the accessible name (aria-label, "
                            "aria-labelledby) starts with or contains the "
                            "visible label text."
                        ),
                        severity=Severity.HIGH,
                    ))

        # Check links with aria-label vs visible text
        for link in capture_data.links:
            selector = link.get("selector", "a")
            visible_text = (link.get("text") or "").strip()
            aria_label = (link.get("aria_label") or link.get("aria-label") or "").strip()

            if visible_text and aria_label:
                if visible_text.lower() not in aria_label.lower():
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Link visible text \"{visible_text}\" is not "
                            f"in aria-label \"{aria_label}\""
                        ),
                        impact="Speech input users cannot activate this link by saying its visible text.",
                        recommendation=(
                            f"Include \"{visible_text}\" in the aria-label value."
                        ),
                        severity=Severity.HIGH,
                    ))

        conformance = self._determine_conformance(findings)
        confidence = 0.8
        return conformance, confidence, findings


class Check_2_5_4(BaseCheck):
    """SC 2.5.4 Motion Actuation (Level A, WCAG 2.1/2.2)."""

    criterion_id = "2.5.4"
    criterion_name = "Motion Actuation"
    level = "A"
    wcag_versions = ["2.1", "2.2"]
    guideline = "2.5 Input Modalities"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "Functionality that can be operated by device motion or user "
        "motion can also be operated by user interface components and "
        "responding to the motion can be disabled to prevent accidental "
        "actuation."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        script = capture_data.script_content or ""
        html = capture_data.html or ""
        combined = (script + html).lower()
        return bool(
            "devicemotion" in combined
            or "deviceorientation" in combined
            or "accelerometer" in combined
            or "gyroscope" in combined
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        script = capture_data.script_content or ""
        html = capture_data.html or ""
        combined = script + html

        motion_patterns = [
            (r"addEventListener\s*\(\s*['\"]devicemotion['\"]", "device motion"),
            (r"addEventListener\s*\(\s*['\"]deviceorientation['\"]", "device orientation"),
            (r"(?:new\s+)?Accelerometer\s*\(", "Accelerometer API"),
            (r"(?:new\s+)?Gyroscope\s*\(", "Gyroscope API"),
        ]

        for pattern, desc in motion_patterns:
            if re.search(pattern, combined, re.IGNORECASE):
                # Check for alternative UI control
                has_button_alt = bool(
                    re.search(r"(?:button|click|toggle|disable.*motion)", combined, re.IGNORECASE)
                )
                if not has_button_alt:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element="<script>",
                        issue=f"{desc} used without apparent UI control alternative",
                        impact=(
                            "Users who cannot perform device motion (e.g., "
                            "mounted devices, motor impairments) cannot use "
                            "this functionality."
                        ),
                        recommendation=(
                            "Provide a UI button or control as an alternative "
                            "to the motion gesture, and allow disabling motion "
                            "actuation."
                        ),
                        severity=Severity.MEDIUM,
                    ))

        conformance = self._determine_conformance(findings)
        # SC 2.5.4 applicability is a deterministic script scan: only
        # pages that register devicemotion/deviceorientation handlers,
        # or instantiate Accelerometer/Gyroscope, can fail this SC.
        # The AI cannot read scripts more reliably than the regex.
        # When applicability is False (most pages), Supports is
        # certain. When applicability is True and an alternative
        # button is detected, Supports is high-confidence. When
        # applicability is True and no alternative is found, the
        # finding is correctly emitted at MEDIUM. Promoted to
        # PROGRAMMATIC_DEFINITIVE 2026-04-29.
        confidence = 0.9 if not findings else 0.7
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_2_5_1(),
        Check_2_5_2(),
        Check_2_5_3(),
        Check_2_5_4(),
    ]
