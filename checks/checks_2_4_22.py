"""WCAG 2.2 new criteria for Guideline 2.4 - Navigable (AA)."""
from __future__ import annotations

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_2_4_11(BaseCheck):
    """SC 2.4.11 Focus Not Obscured (Minimum) (Level AA, WCAG 2.2)."""

    criterion_id = "2.4.11"
    criterion_name = "Focus Not Obscured (Minimum)"
    level = "AA"
    wcag_versions = ["2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "When a user interface component receives keyboard focus, the "
        "component is not entirely hidden due to author-created content."
    )
    web_only = True

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.tab_walk or capture_data.focus_indicators)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Check for fixed/sticky elements that might obscure focus
        fixed_elements = [
            s for s in capture_data.computed_styles
            if s.get("position") in ("fixed", "sticky")
        ]

        for fi in capture_data.focus_indicators:
            selector = fi.get("selector", "element")
            obscured = fi.get("obscured", False)
            obscured_by = fi.get("obscured_by", "")
            visible_area = fi.get("visible_area_percent", 100)

            if obscured or visible_area < 1:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"Focused element is entirely hidden"
                        + (f" by {obscured_by}" if obscured_by else "")
                    ),
                    impact="Keyboard users cannot see which element has focus.",
                    recommendation=(
                        "Ensure focused elements scroll into view and are "
                        "not hidden behind fixed headers, footers, or overlays."
                    ),
                    severity=Severity.HIGH,
                ))
            elif visible_area < 50:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"Focused element is mostly obscured "
                        f"({100 - visible_area:.0f}% hidden)"
                        + (f" by {obscured_by}" if obscured_by else "")
                    ),
                    impact="Keyboard users may have difficulty locating focus.",
                    recommendation="Adjust scroll position or z-index to reveal focused element.",
                    severity=Severity.MEDIUM,
                ))

        # Check for common patterns: sticky headers over content
        if fixed_elements:
            for el in fixed_elements:
                pos = el.get("position", "")
                tag = (el.get("tag") or el.get("tagName") or "").lower()
                height = el.get("height", 0)
                try:
                    h_val = float(str(height).replace("px", ""))
                except (ValueError, TypeError):
                    h_val = 0

                if pos == "fixed" and h_val > 60:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=el.get("selector", f"fixed {tag}"),
                        issue=(
                            f"Fixed element ({tag}, height: {h_val}px) "
                            f"may obscure focused elements when scrolling"
                        ),
                        impact="Focused elements near the top of viewport may be hidden.",
                        recommendation=(
                            "Use scroll-padding-top to account for fixed "
                            "elements, or use scroll-into-view with offset."
                        ),
                        severity=Severity.LOW,
                    ))

        # Cross-reference focus_indicators for elements that have no
        # visible indicator at all — if the focus indicator itself is
        # invisible, the focused component is effectively "not perceivable"
        # even if not technically obscured by another element.  This is a
        # weaker signal than full obscuring, so report at MEDIUM.
        _obscured_selectors = {
            fi.get("selector", "")
            for fi in capture_data.focus_indicators
            if fi.get("obscured")
        }
        for fi in capture_data.focus_indicators:
            selector = fi.get("selector", "element")
            if selector in _obscured_selectors:
                continue  # Already reported above

            has_visible = fi.get("has_visible_indicator")
            if has_visible is False:
                tag = fi.get("tag", "")
                text = fi.get("text") or ""
                element_desc = selector
                if tag and text:
                    element_desc = f"{selector} (<{tag}> \"{text}\")"
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=element_desc,
                    issue=(
                        "Focused element has no visible focus indicator, "
                        "making it impossible to determine if focus is obscured"
                    ),
                    impact=(
                        "Without a visible indicator, keyboard users cannot "
                        "tell whether the element has focus or if it is hidden "
                        "behind other content."
                    ),
                    recommendation=(
                        "Add a visible focus indicator (outline, border, or "
                        "box-shadow) so users can perceive the focused element."
                    ),
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings)
        # SC 2.4.11 is answered by deterministic data:
        #   - focus_indicators[*].obscured / visible_area_percent (per-
        #     element measurement of whether a sticky overlay covers
        #     the focused element)
        #   - computed_styles entries with position:fixed/sticky
        # The AI guesses from screenshots and adds noise. Promoted to
        # PROGRAMMATIC_DEFINITIVE 2026-04-29.
        confidence = 0.9
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [Check_2_4_11()]
