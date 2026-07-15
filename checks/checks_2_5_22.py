"""WCAG 2.2 new criteria for Guideline 2.5 - Input Modalities (AA)."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_2_5_7(BaseCheck):
    """SC 2.5.7 Dragging Movements (Level AA, WCAG 2.2)."""

    criterion_id = "2.5.7"
    criterion_name = "Dragging Movements"
    level = "AA"
    wcag_versions = ["2.2"]
    guideline = "2.5 Input Modalities"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "All functionality that uses a dragging movement for operation "
        "can be achieved by a single pointer without dragging, unless "
        "dragging is essential or the functionality is determined by the "
        "user agent and not modified by the author."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        script = capture_data.script_content or ""
        html = capture_data.html or ""
        combined = (script + html).lower()
        return bool(
            "drag" in combined
            or "draggable" in combined
            or "sortable" in combined
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        script = capture_data.script_content or ""
        html = capture_data.html or ""
        combined = script + html

        # WCAG 2.5.7 only fails when dragging is the SOLE way to operate.
        # Carousels are the most common drag/swipe widget and almost
        # always ship with prev/next button controls — which means
        # 2.5.7 is met regardless of whether the carousel ALSO accepts
        # swipe. Earlier code didn't recognise these alternatives and
        # produced false positives every run on Bootstrap / Glide /
        # Swiper sites (ASU + NVCC both flagged despite obvious
        # carousel-control-prev / carousel-control-next buttons).
        #
        # Detect drag patterns AND carousel-style alternatives. If any
        # alternative exists, return high-confidence Supports.
        drag_patterns = [
            (r"draggable\s*=\s*['\"]true['\"]", "HTML draggable attribute"),
            (r"addEventListener\s*\(\s*['\"]dragstart['\"]", "drag event listener"),
            (r"addEventListener\s*\(\s*['\"]drag['\"]", "drag event listener"),
            (r"(?:\.sortable|Sortable|sortablejs)", "sortable library"),
            (r"(?:\.draggable|ui-draggable)", "jQuery UI draggable"),
        ]
        drag_signals = [
            (pat, desc) for pat, desc in drag_patterns
            if re.search(pat, combined, re.IGNORECASE)
        ]
        if not drag_signals:
            # No drag widgets detected; SC trivially supports.
            return ConformanceLevel.SUPPORTS, 0.95, []

        # Detect single-pointer alternatives. Two strong signals:
        # 1. Carousel control buttons (Bootstrap / Glide / Swiper / Slick / generic)
        # 2. Reorder / move buttons / arrow-key handlers
        carousel_alt_patterns = [
            r"class=['\"][^'\"]*carousel-control-(?:prev|next)",
            r"class=['\"][^'\"]*glide__arrow",
            r"class=['\"][^'\"]*swiper-button-(?:prev|next)",
            r"class=['\"][^'\"]*slick-(?:prev|next)",
            r"aria-label=['\"](?:Previous|Next|Previous slide|Next slide)['\"]",
            r"data-bs-slide=['\"](?:prev|next)['\"]",
        ]
        button_alt_patterns = [
            r"(?:move.*button|up.*down.*button|reorder.*button|select.*position)",
            r"addEventListener\s*\(\s*['\"]keydown['\"]",  # arrow-key support
            r"\bArrowUp|\bArrowDown|\bArrowLeft|\bArrowRight",
        ]
        has_carousel_alt = any(
            re.search(p, combined, re.IGNORECASE)
            for p in carousel_alt_patterns
        )
        has_button_alt = any(
            re.search(p, combined, re.IGNORECASE)
            for p in button_alt_patterns
        )
        has_alt = has_carousel_alt or has_button_alt

        if has_alt:
            # Drag functionality exists but a single-pointer alternative
            # is also present. SC 2.5.7 is met. High confidence —
            # we observed concrete alternative controls in the markup.
            return ConformanceLevel.SUPPORTS, 0.9, []

        # Drag without any detectable alternative — flag.
        for pattern, desc in drag_signals[:1]:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<script>",
                issue=(
                    f"Dragging functionality detected ({desc}) without "
                    f"a detectable single-pointer alternative "
                    f"(carousel prev/next, reorder buttons, or arrow "
                    f"keys all checked)."
                ),
                impact=(
                    "Users who cannot perform dragging movements "
                    "(motor impairments, switch users) cannot operate "
                    "this functionality."
                ),
                recommendation=(
                    "Provide a single-pointer alternative such as "
                    "explicit prev/next buttons, move up/down buttons, "
                    "a dropdown to select position, or keyboard arrow "
                    "support."
                ),
                severity=Severity.MEDIUM,
            ))

        conformance = self._determine_conformance(findings)
        # When alternatives WOULD be expected but weren't detected, we
        # still want to fast-path with this verdict; if the deterministic
        # signal was missed and AI would catch it, the per-SC AI source
        # exclusion can be lifted in EXCLUDED_AI_SOURCES_PER_SC.
        confidence = 0.85
        return conformance, confidence, findings


class Check_2_5_8(BaseCheck):
    """SC 2.5.8 Target Size (Minimum) (Level AA, WCAG 2.2)."""

    criterion_id = "2.5.8"
    criterion_name = "Target Size (Minimum)"
    level = "AA"
    wcag_versions = ["2.2"]
    guideline = "2.5 Input Modalities"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    # Target dimensions are measured per element from captured rects.
    measurement_sources = {
        "target_width_px": ("target_size_measurements", "width"),
        "target_height_px": ("target_size_measurements", "height"),
    }
    normative_text = (
        "The size of the target for pointer inputs is at least 24 by 24 "
        "CSS pixels, except when: Spacing, Equivalent, Inline, User agent "
        "control, Essential."
    )
    web_only = True

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.links or capture_data.form_fields)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        MIN = 24.0

        # Build a list of all interactive targets with their rects + selectors.
        # Each entry: {selector, w, h, cx, cy, kind, type, is_inline}.
        # Then for each undersized target, apply WCAG 2.5.8 exceptions:
        #   - Spacing: 24px-radius circles around two undersized targets
        #     don't intersect (centers ≥ 24px apart, including against
        #     other targets of any size).
        #   - Inline: link inside <p>/<li>/<td>/<dd> sentence — exempt.
        #   - User-agent default form controls — exempt for form_fields
        #     with no author-set width/height (heuristic: w/h match
        #     browser defaults). Skipped here to keep deterministic.
        # Earlier code read field.get("width") which doesn't exist on
        # form_fields (the rect is in field["rect"]); the check produced
        # 0 findings for forms regardless of actual size. Also: the
        # spacing key did not exist either; spacing exception never
        # fired even when a 16x16 radio was clearly 100+px from its
        # nearest neighbour.
        targets: list[dict] = []

        def _add(sel: str, rect: dict, kind: str, type_: str = "", is_inline: bool = False):
            try:
                w = float(rect.get("width") or 0)
                h = float(rect.get("height") or 0)
                x = float(rect.get("x") or 0)
                y = float(rect.get("y") or 0)
            except (ValueError, TypeError):
                return
            if w <= 0 or h <= 0:
                return
            # Skip sr-only / visually-hidden-focusable elements. These have
            # 1x1 / 1x2 / 2x2 rects by design (Bootstrap .visually-hidden-
            # focusable, sr-only pattern). They're not pointer targets —
            # they only become visible when keyboard focus hits them, so
            # WCAG 2.5.8 (which is about pointer targets) does not apply.
            # Without this filter, every "Skip to main content" / "Report
            # an accessibility problem" link is flagged as a sub-24px
            # target even though no mouse user can ever click it.
            if w <= 2 and h <= 2:
                return
            targets.append({
                "selector": sel, "w": w, "h": h,
                "cx": x + w / 2, "cy": y + h / 2,
                "kind": kind, "type": type_, "is_inline": is_inline,
            })

        for field in capture_data.form_fields:
            type_ = (field.get("type") or "").lower()
            if type_ in ("hidden",):
                continue
            sel = field.get("selector", "input")
            _add(sel, field.get("rect") or {}, "form_field", type_=type_)

        for link in capture_data.links:
            sel = link.get("selector", "a")
            is_inline = bool(
                link.get("in_paragraph")
                or (link.get("context") and link.get("text")
                    and link.get("text") in (link.get("context") or ""))
            )
            _add(sel, link.get("rect") or {}, "link", is_inline=is_inline)

        # Helper: minimum centre-to-centre distance to ANY other target
        # (not just other undersized ones — adjacent full-size targets
        # also count against the spacing exception per WCAG note).
        def nearest_other_distance(t: dict) -> float:
            best = float("inf")
            for o in targets:
                if o is t:
                    continue
                d = ((t["cx"] - o["cx"]) ** 2 + (t["cy"] - o["cy"]) ** 2) ** 0.5
                if d < best:
                    best = d
            return best

        for t in targets:
            w, h = t["w"], t["h"]
            sel = t["selector"]
            kind = t["kind"]
            if w >= MIN and h >= MIN:
                continue
            if t["is_inline"]:
                continue

            spacing = nearest_other_distance(t)
            # WCAG spacing exception: 24-px-diameter circles centred on
            # the bounding box don't intersect another target. Two
            # 24-radius circles fail to intersect when centres are at
            # least 24px apart.
            if spacing >= MIN:
                continue

            if kind == "form_field":
                issue = (
                    f"Form control target size ({w:.0f}x{h:.0f}px) is "
                    f"below {MIN:.0f}x{MIN:.0f}px and the centre is only "
                    f"{spacing:.0f}px from the nearest interactive "
                    f"target (spacing exception requires ≥{MIN:.0f}px)."
                )
            else:
                issue = (
                    f"Link target size ({w:.0f}x{h:.0f}px) is below "
                    f"{MIN:.0f}x{MIN:.0f}px and not inline; nearest "
                    f"adjacent target is {spacing:.0f}px away (spacing "
                    f"exception requires ≥{MIN:.0f}px)."
                )
            findings.append(Finding(
                id=_make_finding_id(),
                element=sel,
                css_selector=sel,
                issue=issue,
                impact=(
                    "Users with motor impairments — including those "
                    "with tremor or low precision pointers — may not "
                    "be able to reliably hit a target this small."
                ),
                recommendation=(
                    f"Increase the target size to at least "
                    f"{MIN:.0f}x{MIN:.0f}px, OR space adjacent targets "
                    f"so their centres are ≥{MIN:.0f}px apart."
                ),
                severity=Severity.MEDIUM,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.85
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_2_5_7(),
        Check_2_5_8(),
    ]
