"""WCAG Guideline 1.4 - Distinguishable (A/AA) checks."""
from __future__ import annotations

import logging
import re

from checks.base import BaseCheck, _make_finding_id
from functions.overflow import classify_overflow_loss
from functions.contrast import (
    parse_rgb,
    composite_alpha,
    relative_luminance,
    contrast_ratio,
    is_large_text,
)
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
    TTResult,
    TTSubTestResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Colour / contrast helpers — delegated to functions/contrast.py
# Thin wrappers preserve the underscore-prefixed names used throughout
# this file so callers don't need updating.
# ---------------------------------------------------------------------------

_parse_rgb = parse_rgb
_composite_alpha = composite_alpha
_relative_luminance = relative_luminance
_contrast_ratio = contrast_ratio


def _nontext_contrast_location(ntc: dict) -> str:
    """Build an ACR-quality location string from nontext_contrast data."""
    parts = []
    name = ntc.get("accessible_name") or ntc.get("text", "")
    if name:
        parts.append(f"'{name}'")
    role = ntc.get("role", "")
    tag = ntc.get("tag", "element")
    parts.append(role if role else tag)
    landmark = ntc.get("landmark", "")
    if landmark:
        parts.append(f"in the {landmark} region")
    rect = ntc.get("rect", {})
    y = rect.get("y", 0)
    if y < 200:
        parts.append("near the top of the page")
    elif y > 2000:
        parts.append("near the bottom of the page")
    sel = ntc.get("selector", "")
    if sel:
        parts.append(f"({sel})")
    return " ".join(parts)


_is_large_text = is_large_text


class Check_1_4_1(BaseCheck):
    """SC 1.4.1 Use of Color (Level A)."""

    criterion_id = "1.4.1"
    criterion_name = "Use of Color"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.4 Distinguishable"
    principle = "1. Perceivable"
    ict_baseline = "7"
    tt_tests = ["7.B"]
    normative_text = (
        "Color is not used as the only visual means of conveying "
        "information, indicating an action, prompting a response, or "
        "distinguishing a visual element."
    )
    off_scope_keywords = {
        "contrast": ["contrast ratio", "4.5:1", "3:1"],
    }

    # Per-image screenshots -- charts/graphs often encode data via color alone,
    # so the AI needs to see every image. Zoom/320px views come from the base.
    _SCREENSHOT_FIELDS = [("images", "screenshot_path")]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.links or capture_data.form_fields or capture_data.colors)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Check links distinguished only by color (no underline or other visual cue).
        #
        # The capture extracts has_underline, has_border, has_icon,
        # font_weight, font_style, color, and surrounding_color for
        # every <a>. A link inline in a paragraph is a 1.4.1 risk when
        # NONE of {underline, border, icon, bold, italic} differs from
        # its parent — i.e. only colour distinguishes it. We then check
        # the colour contrast between the link and surrounding text:
        # if it's < 3:1, the colour distinction is also not perceivable
        # to many colour-blind users -> MEDIUM. If >= 3:1, distinction
        # exists but relies entirely on colour -> LOW (best-practice).
        for link in capture_data.links:
            if link.get("visible") is False:
                continue

            selector = link.get("selector", "a")
            has_underline = link.get("has_underline", True)
            has_border = link.get("has_border", False)
            has_icon = link.get("has_icon", False)
            in_text = link.get("in_paragraph", False)
            # Bold or italic on the link (vs surrounding paragraph)
            # also counts as a non-colour visual cue.
            try:
                font_weight = int(str(link.get("font_weight", "400")).strip() or "400")
            except (TypeError, ValueError):
                font_weight = 400
            font_style = (link.get("font_style", "") or "").lower()
            has_weight_or_style = font_weight >= 600 or font_style in ("italic", "oblique")
            has_other_visual = has_border or has_icon or has_weight_or_style

            if in_text and not has_underline and not has_other_visual:
                # Check if link color differs from surrounding text
                link_color = link.get("color", "")
                text_color = link.get("surrounding_color", "")
                if link_color and text_color and link_color != text_color:
                    # Color-only distinction for in-text links
                    link_rgb = _parse_rgb(link_color, text_color)
                    text_rgb = _parse_rgb(text_color)
                    if link_rgb and text_rgb:
                        lum_link = _relative_luminance(*link_rgb)
                        lum_text = _relative_luminance(*text_rgb)
                        ratio = _contrast_ratio(lum_link, lum_text)
                        if ratio < 3.0:
                            findings.append(Finding(
                                id=_make_finding_id(),
                                element=selector,
                                issue=(
                                    f"In-text link distinguished only by color "
                                    f"(no underline). Link-to-text contrast "
                                    f"ratio: {ratio:.2f}:1 (needs 3:1 minimum)"
                                ),
                                impact=(
                                    "Color-blind users may not be able to "
                                    "distinguish links from surrounding text."
                                ),
                                recommendation=(
                                    "Add underline, border, icon, or other "
                                    "non-color visual indicator to links."
                                ),
                                severity=Severity.MEDIUM,
                            ))
                        else:
                            findings.append(Finding(
                                id=_make_finding_id(),
                                element=selector,
                                issue=(
                                    f"In-text link has no underline but color "
                                    f"contrast with text is {ratio:.2f}:1"
                                ),
                                impact=(
                                    "Some users may miss that this is a link."
                                ),
                                recommendation=(
                                    "Consider adding underline or other non-color "
                                    "indicator for best accessibility."
                                ),
                                severity=Severity.LOW,
                            ))

        # Check form error states indicated by color only
        for field in capture_data.form_fields:
            selector = field.get("selector", "input")
            error_state = field.get("has_error", False)
            error_indicator = field.get("error_indicator", "")
            aria_invalid = field.get("aria_invalid", field.get("aria-invalid", ""))

            if error_state and not aria_invalid:
                if error_indicator == "color_only":
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue="Form field error indicated by color only",
                        impact=(
                            "Color-blind users may not perceive the error state."
                        ),
                        recommendation=(
                            "Add text, icon, or aria-invalid attribute to "
                            "indicate the error in addition to color."
                        ),
                        severity=Severity.MEDIUM,
                    ))

        conformance = self._determine_conformance(findings)
        confidence = 0.6  # Color-only detection is heuristic
        return conformance, confidence, findings


class Check_1_4_2(BaseCheck):
    """SC 1.4.2 Audio Control (Level A)."""

    criterion_id = "1.4.2"
    criterion_name = "Audio Control"
    level = "A"
    needs_audio = True  # Must HEAR if audio autoplays
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.4 Distinguishable"
    principle = "1. Perceivable"
    ict_baseline = "21"
    tt_tests = ["21.A", "21.B"]
    normative_text = (
        "If any audio on a Web page plays automatically for more than 3 "
        "seconds, either a mechanism is available to pause or stop the "
        "audio, or a mechanism is available to control audio volume "
        "independently from the overall system volume level."
    )

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send page observation video — VL model watches for
        auto-playing audio and checks for pause/stop/volume controls."""
        if capture_data.observation_video_path:
            return capture_data.observation_video_path
        return None

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.media)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        for m in capture_data.media:
            selector = m.get("selector", "media element")
            autoplay = m.get("autoplay", False)
            has_audio = m.get("has_audio", True)
            muted = m.get("muted", False)
            duration = m.get("duration", 0)
            has_controls = m.get("controls", False)
            tag = (m.get("tag") or m.get("tagName") or "").lower()

            if not autoplay:
                continue

            # Autoplay + muted is generally acceptable (no audible output)
            if muted:
                continue

            # Autoplay with audio (not muted) -- potential issue
            # For elements without has_audio field, assume audio is present
            # unless it is a <video> with no audio track indication
            if not has_audio:
                continue

            if duration > 3 or duration == 0:
                if not has_controls:
                    # Autoplay, not muted, no controls -- worst case
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            "Audio/video autoplays (not muted) with no "
                            "mechanism to pause, stop, or control volume"
                        ),
                        impact=(
                            "Screen reader users may not be able to hear "
                            "their screen reader over the autoplaying audio. "
                            "Users have no way to stop or mute the audio."
                        ),
                        recommendation=(
                            "Remove autoplay, add the muted attribute, or "
                            "provide visible controls to pause/stop/mute."
                        ),
                        severity=Severity.HIGH,
                    ))
                else:
                    # Autoplay, not muted, but controls are present --
                    # user can stop it, so this is a lesser issue
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            "Audio/video autoplays (not muted) but native "
                            "controls are present"
                        ),
                        impact=(
                            "Users can stop or mute the audio via controls, "
                            "but may be briefly disrupted before they locate them."
                        ),
                        recommendation=(
                            "Consider adding the muted attribute or removing "
                            "autoplay to avoid disrupting screen reader users."
                        ),
                        severity=Severity.MEDIUM,
                    ))
            elif duration > 0:
                # Short autoplay (<=3s) with audio, not muted
                # WCAG 1.4.2 only applies to audio >3s, but flag for awareness
                if not has_controls:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Audio/video autoplays (not muted) for "
                            f"{duration}s (under 3s threshold) with no controls"
                        ),
                        impact=(
                            "Short autoplay audio is not a WCAG 1.4.2 failure "
                            "but may still briefly disrupt screen reader users."
                        ),
                        recommendation=(
                            "Consider adding the muted attribute or providing "
                            "controls for user convenience."
                        ),
                        severity=Severity.LOW,
                    ))

        # Check for autoplay in HTML attributes
        html_lower = (capture_data.html or "").lower()
        if "autoplay" in html_lower and not capture_data.media:
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue="Autoplay attribute detected in page HTML (media element not captured)",
                impact="Audio may autoplay without user control.",
                recommendation="Review and ensure autoplay media has controls.",
                severity=Severity.LOW,
            ))

        # Audio detection: deterministic DOM probe (always) + optional AI
        # corroboration via cloud video model (Gemini Flash). The AI call
        # only happens for SC 1.4.2 / SC 2.2.2 -- those are the only two
        # SCs that import audio_probe -- and only when AI_VIDEO_* is a
        # cloud endpoint that can actually process audio.
        from functions.audio_probe import corroborate_autoplay_audio, merge_audio_signals
        deterministic = getattr(capture_data, "audio_detection", {}) or {}
        video_path = getattr(capture_data, "observation_video_path", None)
        ai_signal = await corroborate_autoplay_audio(video_path)
        audio = merge_audio_signals(deterministic, ai_signal)
        if audio.get("has_autoplay_audio") and audio.get("duration_over_3s"):
            audio_type = audio.get("audio_type", "audio")
            desc = audio.get("description", "")
            has_pause = audio.get("has_pause_button", False)
            severity = Severity.MEDIUM if has_pause else Severity.HIGH
            findings.append(Finding(
                id=_make_finding_id(),
                element="page (detected via audio analysis)",
                issue=(
                    f"Auto-playing {audio_type} detected on page load, lasting "
                    f"longer than 3 seconds"
                    + (f". {desc}" if desc else "")
                ),
                impact=(
                    "Screen reader users may not be able to hear their "
                    "assistive technology over the auto-playing audio."
                ),
                recommendation=(
                    "Provide a mechanism to pause, stop, or control the "
                    "volume of auto-playing audio, or ensure audio does "
                    "not play for more than 3 seconds."
                ),
                severity=severity,
                evidence=f"Audio analysis: type={audio_type}, has_pause={has_pause}",
            ))

        conformance = self._determine_conformance(findings, len(capture_data.media))
        confidence = 0.85
        return conformance, confidence, findings


class Check_1_4_3(BaseCheck):
    """SC 1.4.3 Contrast (Minimum) (Level AA)."""

    criterion_id = "1.4.3"
    criterion_name = "Contrast (Minimum)"
    level = "AA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.4 Distinguishable"
    principle = "1. Perceivable"
    ict_baseline = "8"
    # Text-on-background contrast is measured per text node by ANDI.
    measurement_sources = {"contrast_ratio": ("andi_contrast_results", "ratio")}
    tt_tests = ["8.A", "8.B"]
    normative_text = (
        "The visual presentation of text and images of text has a "
        "contrast ratio of at least 4.5:1, except for: Large Text "
        "(at least 3:1), Incidental, Logotypes."
    )
    off_scope_keywords = {
        "non_text": ["non-text contrast", "UI component contrast"],
    }

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.colors)

    def get_image_context(self, capture_data: CaptureData) -> str:
        """Pass computed contrast ratios as ground truth to the visual AI."""
        lines = [
            "COMPUTED CONTRAST RATIOS (ground truth from programmatic analysis):",
            "These are mathematically computed — do NOT estimate contrast from screenshots.",
            "Verify visually that the colors described match what you see.",
            "",
        ]
        for color_info in capture_data.colors:
            fg = color_info.get("color", color_info.get("foreground", ""))
            bg = color_info.get("background_color", color_info.get("background", ""))
            ratio = color_info.get("contrast_ratio")
            font_size = color_info.get("font_size", color_info.get("fontSize", ""))
            font_weight = color_info.get("font_weight", color_info.get("fontWeight", ""))
            text = color_info.get("text", color_info.get("sampleText", ""))
            tag = color_info.get("tag", "")

            if ratio:
                status = "PASS" if ratio >= 4.5 else ("PASS (large)" if ratio >= 3.0 else "FAIL")
                lines.append(f"  <{tag}> \"{text}\" — {fg} on {bg} = {ratio:.2f}:1 [{status}]")
            else:
                lines.append(f"  <{tag}> \"{text}\" — {fg} on {bg} (size: {font_size}, weight: {font_weight})")

        # ANDI per-text-node ratios (denser, includes SVG text and per-
        # text-node colour overrides that the element-level walk above
        # may dedup away).
        andi_block = self._format_andi_image_context(capture_data, is_aaa=False)
        if andi_block:
            lines.append("")
            lines.append(andi_block)

        # Also include parent exploration context
        base_context = super().get_image_context(capture_data)
        if base_context:
            lines.append("")
            lines.append(base_context)

        return "\n".join(lines)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        # Authoritative source for SC 1.4.3 contrast findings is ANDI cANDI
        # (BaseCheck._extract_andi_contrast_findings). The legacy element-
        # walker over capture_data.colors and capture_data.computed_styles
        # was removed because:
        #   - its bg-image detection halted on the first opaque ancestor
        #     and missed images one layer deeper, producing 1.23:1 K-means
        #     uniform-region false positives on hero banners;
        #   - it duplicated the per-text-node walk ANDI now performs
        #     correctly with the bg_image_present flag and walk_depth.
        # Keep is_applicable / get_image_context driven by capture_data.colors
        # so the visual AI still receives the computed-styles ground truth.
        # The base class will run _extract_axe_findings (axe color-contrast,
        # incomplete entries already INFO) and _extract_andi_contrast_findings
        # after this returns, populating findings + bumping conformance.
        return ConformanceLevel.SUPPORTS, 0.95, []

    def _generate_tt_results(
        self, findings: list[Finding], capture_data: CaptureData
    ) -> list[TTSubTestResult]:
        not_app = not self.is_applicable(capture_data)
        normal_fail = any("normal text" in f.issue for f in findings if f.severity in (Severity.HIGH, Severity.MEDIUM))
        large_fail = any("large text" in f.issue for f in findings if f.severity in (Severity.HIGH, Severity.MEDIUM))
        return [
            TTSubTestResult(
                tt_id="8.A",
                name="Normal text contrast >= 4.5:1",
                result=TTResult.DNA if not_app else TTResult.FAIL if normal_fail else TTResult.PASS,
            ),
            TTSubTestResult(
                tt_id="8.B",
                name="Large text contrast >= 3:1",
                result=TTResult.DNA if not_app else TTResult.FAIL if large_fail else TTResult.PASS,
            ),
        ]


class Check_1_4_4(BaseCheck):
    """SC 1.4.4 Resize Text (Level AA)."""

    criterion_id = "1.4.4"
    criterion_name = "Resize Text"
    level = "AA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.4 Distinguishable"
    principle = "1. Perceivable"
    ict_baseline = "22"
    tt_tests = ["22.A"]
    normative_text = (
        "Except for captions and images of text, text can be resized "
        "without assistive technology up to 200 percent without loss "
        "of content or functionality."
    )
    off_scope_keywords = {
        "reflow": ["reflow", "320px", "narrow viewport", "horizontal scroll"],
        "speculation": ["may overflow", "will overflow", "could overflow",
                        "might be clipped", "appears to use fixed"],
    }

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    def get_image_context(self, capture_data: CaptureData) -> str:
        """Tell the AI exactly which screenshot is which."""
        lines = [
            "[ATTACHED IMAGE GUIDE]",
            "The following screenshots are attached in order:",
            "  1. FULL PAGE at 100% zoom — the entire page scrolled "
            "top to bottom at normal size",
            "  2. VIEWPORT at 100% zoom — only the visible above-the-fold "
            "area at normal browser zoom",
            "  3. VIEWPORT at 200% zoom — the same viewport area after "
            "applying 200% browser zoom",
            "  4. FULL PAGE at 200% zoom — the entire page at 200% zoom "
            "(if available) — shows how the full page reflows at zoom",
            "",
            "Compare images 1+2 (normal) against 3+4 (zoomed) to identify "
            "text that is clipped, truncated, overlapped, or hidden when "
            "zoomed to 200%.  Content moving off-screen that is still "
            "accessible via scrolling is NOT a 1.4.4 failure — only "
            "content that is permanently lost (clipped by overflow:hidden, "
            "obscured by overlapping elements, or otherwise unreachable) "
            "is a failure.",
        ]
        return "\n".join(lines)

    def get_extra_images(self, capture_data: CaptureData) -> list[str]:
        """Send the 200% zoom screenshots for AI review."""
        paths = []
        if capture_data.viewport_200pct_path:
            paths.append(capture_data.viewport_200pct_path)
        if getattr(capture_data, "full_page_200pct_path", ""):
            paths.append(capture_data.full_page_200pct_path)
        return paths

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Check viewport meta for user-scalable=no
        vp = capture_data.viewport_meta
        if vp:
            content = (vp.get("content") or "").lower()
            if "user-scalable=no" in content or "user-scalable=0" in content:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="<meta name=\"viewport\">",
                    issue="Viewport meta disables user scaling (user-scalable=no)",
                    impact="Users cannot zoom in to enlarge text, blocking low-vision users.",
                    recommendation="Remove user-scalable=no from the viewport meta tag.",
                    severity=Severity.HIGH,
                ))

            # Check maximum-scale < 2
            ms_match = re.search(r"maximum-scale\s*=\s*([\d.]+)", content)
            if ms_match:
                max_scale = float(ms_match.group(1))
                if max_scale < 2.0:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element="<meta name=\"viewport\">",
                        issue=f"Viewport meta restricts zoom: maximum-scale={max_scale} (< 2.0)",
                        impact="Users cannot zoom to 200%, limiting text enlargement.",
                        recommendation="Set maximum-scale=5.0 or remove the maximum-scale restriction.",
                        severity=Severity.HIGH,
                    ))

        # Check for overflow issues at 200% zoom. SC 1.4.4 is about TEXT
        # content being LOST at 200%. Geometric overflow alone is not loss:
        # carousels/sliders position slides off-screen by design, decorative
        # image grids crop tiles with object-fit:cover, and zero-area / non-
        # self-clipping boxes lose nothing. Filter those out before flagging
        # (verified on a university run 2026-05-28: all 6 programmatic 1.4.4 findings
        # were FPs -- off-screen carousel slides, the full-width carousel
        # viewport, a h=0 wrapper, and uniform 304x304 gallery tiles).
        _ovs = capture_data.overflow_200pct or []
        kept_overflow, skipped_reasons = classify_overflow_loss(_ovs)
        skipped_overflow = sum(skipped_reasons.values())

        for overflow in kept_overflow:
            selector = overflow.get("selector", "element")
            tag = overflow.get("tag", "")
            rect = overflow.get("rect", {})
            ov_x = overflow.get("overflowX", False)
            ov_y = overflow.get("overflowY", False)

            directions: list[str] = []
            if ov_x:
                directions.append("horizontally")
            if ov_y:
                directions.append("vertically")
            dir_desc = " and ".join(directions)

            rect_desc = ""
            if rect:
                rect_desc = (
                    f" (element rect: {rect.get('width', '?')}x"
                    f"{rect.get('height', '?')} at "
                    f"{rect.get('x', '?')},{rect.get('y', '?')})"
                )

            findings.append(Finding(
                id=_make_finding_id(),
                element=selector,
                issue=(
                    f"<{tag}> overflows its container {dir_desc} at "
                    f"200% zoom{rect_desc}"
                ),
                impact=(
                    "Content that overflows at 200% zoom may be clipped "
                    "by overflow:hidden ancestors, causing loss of "
                    "information for users who rely on zoom."
                ),
                recommendation=(
                    "Use relative units (em, rem, %) and ensure containers "
                    "expand to fit zoomed content. Avoid fixed widths and "
                    "overflow: hidden on ancestors of text content."
                ),
                severity=Severity.HIGH,
            ))

        if skipped_overflow:
            logger.info(
                "SC 1.4.4: %d of %d 200%% overflow entries skipped as non-loss "
                "(off-screen carousel slides, decorative tile grids, zero-area, "
                "or full-width carousel viewports); %d real overflow finding(s) kept",
                skipped_overflow, len(_ovs), len(findings),
            )
        conformance = self._determine_conformance(findings)
        # The deterministic check covers all SC 1.4.4 mechanisms:
        # viewport meta scaling restrictions + overflow:hidden ancestors
        # at 200% zoom. AI input has only added duplicate findings or
        # carousel false positives across both a university and a community college. SC promoted
        # to PROGRAMMATIC_DEFINITIVE 2026-04-29.
        confidence = 0.9
        return conformance, confidence, findings


class Check_1_4_5(BaseCheck):
    """SC 1.4.5 Images of Text (Level AA)."""

    criterion_id = "1.4.5"
    criterion_name = "Images of Text"
    level = "AA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.4 Distinguishable"
    principle = "1. Perceivable"
    ict_baseline = "7"
    tt_tests = ["7.C"]
    normative_text = (
        "If the technologies being used can achieve the visual "
        "presentation, text is used to convey information rather than "
        "images of text except for: Customizable, Essential."
    )

    # Per-image screenshots -- VL model checks each image for text that could
    # be rendered as HTML instead. Zoom/320px views come from the base.
    _SCREENSHOT_FIELDS = [("images", "screenshot_path")]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.images)

    # Alt-text snippets and filename stems that look like wordmarks/logos
    # and should NOT be flagged as "images of text" even when the VLM
    # correctly transcribes text from them.
    _WORDMARK_HINTS: tuple[str, ...] = (
        "logo", "wordmark", "brand", "emblem", "seal", "crest", "shield",
        "banner", "masthead",
    )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        """Deterministic SC 1.4.5 evaluation.

        For every image the capture pipeline ran through the VLM
        (stored as ``vlm_extracted_text`` on the image dict), a
        non-empty transcription is a strong signal that the image
        carries text which could have been rendered as HTML.

        Logos, wordmarks, and decorative brand graphics are exempt:
        SC 1.4.5's "logotype" exception allows text-in-image for
        those. We detect them by filename / alt-text hints.
        """
        findings: list[Finding] = []
        analyzed_count = 0

        for img in capture_data.images:
            selector = img.get("selector", "img")
            src = img.get("src", "")
            alt = (img.get("alt") or "").strip()
            is_svg = (src or "").lower().endswith(".svg")

            # Inline SVGs are not "images of text" in the WCAG sense --
            # their text IS programmatically available to AT.
            if is_svg:
                continue

            # If the image is explicitly decorative, WCAG 1.4.5 exempts
            # it (no meaningful text is being conveyed via the image).
            role = (img.get("role") or "").lower()
            if role in ("presentation", "none"):
                continue

            extracted = (img.get("vlm_extracted_text") or "").strip()
            if not extracted:
                continue
            analyzed_count += 1

            # Logotype exception: logos are allowed to be images of text.
            if _looks_like_wordmark(src, alt):
                continue

            # Filter out trivial OCR hits (single word, single number) --
            # those rarely indicate content-bearing text.
            meaningful = len(extracted) >= 12 or len(extracted.split()) >= 3
            if not meaningful:
                continue

            findings.append(Finding(
                id=_make_finding_id(),
                element=selector,
                issue=(
                    f"Image contains rendered text that could have been "
                    f"HTML: \"{extracted}\""
                ),
                impact=(
                    "Text rendered inside an image cannot be resized by the "
                    "user, cannot be re-themed by browser or user stylesheets, "
                    "cannot be translated inline by browser tools, and is "
                    "pixelated when zoomed. Users with low vision, users who "
                    "need high-contrast modes, and users who rely on machine "
                    "translation are all blocked."
                ),
                recommendation=(
                    "Replace the image with actual HTML text styled with CSS "
                    "so it can be resized, reflowed, and customized. WCAG 1.4.5 "
                    "exempts logotypes and text where a specific presentation "
                    "is essential."
                ),
                severity=Severity.MEDIUM,
                evidence=f"VLM transcription: {extracted}",
            ))

        # NB: a previous "background-image text overlay vs HTML corpus"
        # check lived here. It compared the first line of the bg-image
        # element's `textContent` (the JS-side `text_overlay_text` field
        # IS just `el.textContent`) against a corpus of headings + links
        # + heading visible_text. If the first line wasn't in that
        # corpus, the check flagged the element as "text likely baked
        # into the image."
        #
        # Verified false positive on a university run 20260506_112508_bb25cbfd:
        # the SELECT#edit-interestarea element has option text in
        # textContent ("Select one...Architecture & Construction..."),
        # the bg-image is a decorative chevron icon, and the OPTIONS
        # are HTML. The corpus didn't include `<option>` text so the
        # check fired falsely. Same pattern for div.bg.topo-white (text
        # in `<p>`) and div.block.block-layout-builder (text in `<h3>`
        # plus body copy).
        #
        # Conceptually the check is unsound: the element's textContent
        # IS HTML by definition. There is no programmatic way to know
        # what text is RASTERIZED INTO the bg image without OCR or AI
        # vision. Visual-AI / VLM image analysis is the right place to
        # decide whether a bg-image actually contains baked-in text;
        # the judge's prompt now also surfaces every bg-image's
        # `inner_text` and crop so it can verify visual-AI claims.
        # Removed the false-positive heuristic. Real image-of-text
        # findings still come through visual_ai / VLM transcription
        # path above (lines 700-727).

        conformance = self._determine_conformance(findings)
        # High confidence when the VLM actually analyzed images or we
        # cross-checked bg-image overlays; low when we had nothing to
        # work with.
        confidence = 0.9 if analyzed_count else 0.3
        return conformance, confidence, findings


def _looks_like_wordmark(src: str, alt: str) -> bool:
    """Identify images that are logos / wordmarks (SC 1.4.5 exemption)."""
    haystack = f"{src} {alt}".lower()
    return any(
        hint in haystack
        for hint in Check_1_4_5._WORDMARK_HINTS
    )


class Check_1_4_10(BaseCheck):
    """SC 1.4.10 Reflow (Level AA, WCAG 2.1/2.2)."""

    criterion_id = "1.4.10"
    criterion_name = "Reflow"
    level = "AA"
    wcag_versions = ["2.1", "2.2"]
    guideline = "1.4 Distinguishable"
    principle = "1. Perceivable"
    ict_baseline = ""
    tt_tests = []
    # A finding's `position: fixed/sticky` claim is verified against the
    # deterministic full-page positioned-element scan.
    measurement_sources = {"position": ("positioned_elements", "position")}
    normative_text = (
        "Content can be presented without loss of information or "
        "functionality, and without requiring scrolling in two dimensions "
        "for: Vertical scrolling content at a width equivalent to 320 CSS "
        "pixels; Horizontal scrolling content at a height equivalent to "
        "256 CSS pixels."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    def get_extra_images(self, capture_data: CaptureData) -> list[str]:
        """Send the 320px viewport screenshot for AI review."""
        if capture_data.viewport_320px_path:
            return [capture_data.viewport_320px_path]
        return []

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Check if page has horizontal scrolling at 320px viewport
        if capture_data.horizontal_scroll_320:
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue="Page requires horizontal scrolling at 320px viewport width",
                impact=(
                    "Users on mobile devices or at 400% zoom must scroll "
                    "horizontally to read content, which is difficult for "
                    "users with motor disabilities."
                ),
                recommendation=(
                    "Ensure content reflows to fit within a 320px viewport "
                    "without horizontal scrolling. Use responsive CSS."
                ),
                severity=Severity.HIGH,
            ))

        # Check specific overflow elements at 320px
        for overflow in (capture_data.overflow_320px or []):
            selector = overflow.get("selector", "element")
            tag = overflow.get("tag", "")
            rect = overflow.get("rect", {})
            ov_x = overflow.get("overflowX", False)
            ov_y = overflow.get("overflowY", False)

            if not ov_x and not ov_y:
                continue

            # Horizontal overflow is the primary Reflow concern
            severity = Severity.HIGH if ov_x else Severity.MEDIUM

            directions: list[str] = []
            if ov_x:
                directions.append("horizontally")
            if ov_y:
                directions.append("vertically")
            dir_desc = " and ".join(directions)

            el_width = rect.get("width", "?")
            rect_desc = ""
            if rect:
                rect_desc = (
                    f" (element size: {el_width}x"
                    f"{rect.get('height', '?')}px)"
                )

            findings.append(Finding(
                id=_make_finding_id(),
                element=selector,
                issue=(
                    f"<{tag}> overflows its container {dir_desc} at "
                    f"320px viewport width{rect_desc}"
                ),
                impact=(
                    "Content requires horizontal scrolling at 320px "
                    "viewport width, making it difficult for users on "
                    "mobile devices or at high zoom levels."
                ),
                recommendation=(
                    "Use max-width: 100%, overflow-wrap: break-word, or "
                    "flexible layout to ensure content reflows within "
                    "the viewport."
                ),
                severity=severity,
            ))

        # Check viewport meta for fixed width
        vp = capture_data.viewport_meta
        if vp:
            content = vp.get("content", "")
            width_match = re.search(r"width\s*=\s*(\d+)", content)
            if width_match:
                fixed_width = int(width_match.group(1))
                if fixed_width > 320:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element="<meta name=\"viewport\">",
                        issue=f"Viewport meta sets fixed width={fixed_width}px (> 320px)",
                        impact="Content may not reflow properly at narrow viewports.",
                        recommendation="Use width=device-width instead of a fixed pixel width.",
                        severity=Severity.MEDIUM,
                    ))

        conformance = self._determine_conformance(findings)
        # horizontal_scroll_320 directly answers SC 1.4.10 ("page must
        # not require horizontal scrolling at 320px"). overflow_320px
        # adds per-element granularity. The AI cannot improve on these
        # boolean+rect measurements.  Promoted to PROGRAMMATIC_DEFINITIVE
        # 2026-04-29.
        confidence = 0.9
        return conformance, confidence, findings


class Check_1_4_11(BaseCheck):
    """SC 1.4.11 Non-text Contrast (Level AA, WCAG 2.1/2.2)."""

    criterion_id = "1.4.11"
    criterion_name = "Non-text Contrast"
    level = "AA"
    wcag_versions = ["2.1", "2.2"]
    guideline = "1.4 Distinguishable"
    principle = "1. Perceivable"
    ict_baseline = ""
    tt_tests = []
    # UI-component / graphical-object contrast.
    measurement_sources = {"contrast_ratio": ("nontext_contrast", "contrast_ratio")}
    normative_text = (
        "The visual presentation of the following have a contrast ratio "
        "of at least 3:1 against adjacent color(s): User Interface "
        "Components, Graphical Objects."
    )

    # Per-image screenshots -- VL model evaluates contrast of UI components,
    # icons, graphical objects, chart elements.
    _SCREENSHOT_FIELDS = [("images", "screenshot_path")]

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.form_fields or capture_data.colors)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Check form field borders/outlines for contrast
        for field in capture_data.form_fields:
            selector = field.get("selector", "input")
            border_color = field.get("border_color", "")
            bg_color = field.get("background_color", field.get("background", ""))
            page_bg = field.get("page_background", "")

            # Check border against background
            if border_color and bg_color:
                border_rgb = _parse_rgb(border_color, bg_color)
                bg_rgb = _parse_rgb(bg_color)
                if border_rgb and bg_rgb:
                    b_lum = _relative_luminance(*border_rgb)
                    bg_lum = _relative_luminance(*bg_rgb)
                    ratio = _contrast_ratio(b_lum, bg_lum)
                    if ratio < 3.0:
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=selector,
                            issue=(
                                f"UI component border contrast too low: "
                                f"{ratio:.2f}:1 (required: 3:1). "
                                f"Border: {border_color}, Background: {bg_color}"
                            ),
                            impact=(
                                "Users with low vision may not be able to "
                                "identify the form field boundaries."
                            ),
                            recommendation=(
                                "Increase the border contrast to at least "
                                "3:1 against the adjacent background color."
                            ),
                            severity=Severity.MEDIUM,
                        ))

        # Check custom UI component colors
        checked_selectors: set[str] = set()
        for color_info in capture_data.colors:
            is_ui_component = color_info.get("is_ui_component", False)
            if not is_ui_component:
                continue

            selector = color_info.get("selector", "element")
            checked_selectors.add(selector)
            fg = color_info.get("color", color_info.get("foreground", ""))
            bg = color_info.get("background_color", color_info.get("background", ""))

            fg_rgb = _parse_rgb(fg, bg)
            bg_rgb = _parse_rgb(bg)
            if fg_rgb and bg_rgb:
                fg_lum = _relative_luminance(*fg_rgb)
                bg_lum = _relative_luminance(*bg_rgb)
                ratio = _contrast_ratio(fg_lum, bg_lum)
                if ratio < 3.0:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Non-text UI component contrast too low: "
                            f"{ratio:.2f}:1 (required: 3:1)"
                        ),
                        impact="Users with low vision may not perceive the UI component.",
                        recommendation="Ensure non-text elements have at least 3:1 contrast ratio.",
                        severity=Severity.MEDIUM,
                    ))

        # Check non-text contrast data (borders, outlines, focus indicators)
        for ntc in (capture_data.nontext_contrast or []):
            selector = ntc.get("selector", "")
            if selector in checked_selectors:
                continue

            location = _nontext_contrast_location(ntc)

            bc = ntc.get("border_contrast")
            border_color = (ntc.get("border_color") or "").strip()
            bg_color = (ntc.get("background") or "").strip()
            # WCAG 1.4.11 (G174/G195): a UI component is sufficiently
            # distinguishable when EITHER its border has 3:1 contrast
            # against its own bg, OR its bg has 3:1 contrast against
            # the surrounding page color, OR another visual indicator
            # marks the boundary. When border_color == background, the
            # border is decorative -- the component is delimited by
            # its bg vs the page, not by the border. Flagging
            # border_contrast < 3:1 in that case is a FALSE POSITIVE
            # (observed on a university's maroon submit/radio buttons and the
            # "Aprender más en Español" dark button — bg vs white
            # page is 18+:1, the border is purely styling). We DEMOTE
            # these to INFO so the auditor still sees them for manual
            # bg-vs-surrounding verification, but the SC verdict is
            # not driven by them.
            border_is_decorative = (
                bc is not None
                and bc < 1.05  # essentially 1:1 (border == bg)
                and border_color
                and bg_color
                and border_color == bg_color
            )
            if bc is not None and bc < 3.0:
                checked_selectors.add(selector)
                if border_is_decorative:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=location,
                        css_selector=selector,
                        issue=(
                            f"UI component has a decorative border "
                            f"(border colour matches background, "
                            f"contrast {bc:.2f}:1). The component must "
                            f"still be distinguishable from surrounding "
                            f"content per WCAG 1.4.11 — verify the "
                            f"component's background ({bg_color}) has "
                            f"3:1 contrast against the page colour."
                        ),
                        impact=(
                            "Cannot determine programmatically without "
                            "the surrounding page colour; manual review "
                            "needed."
                        ),
                        recommendation=(
                            "Confirm the component's bg colour has at "
                            "least 3:1 contrast against the page bg, "
                            "or add a visible border / other indicator."
                        ),
                        severity=Severity.INFO,
                    ))
                else:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=location,
                        css_selector=selector,
                        issue=(
                            f"UI component border contrast {bc:.2f}:1 (required: 3:1). "
                            f"Border: {ntc.get('border_color','?')}, "
                            f"Background: {ntc.get('background','?')}"
                        ),
                        impact="Users with low vision may not perceive the component boundary.",
                        recommendation="Ensure the component border has at least 3:1 contrast ratio.",
                        severity=Severity.MEDIUM,
                    ))

            foc = ntc.get("focus_outline_contrast")
            if foc is not None and foc < 3.0:
                if selector not in checked_selectors:
                    checked_selectors.add(selector)
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=location,
                    css_selector=selector,
                    issue=(
                        f"Focus indicator contrast {foc:.2f}:1 (required: 3:1). "
                        f"Focus outline: {ntc.get('focus_outline_color','?')}, "
                        f"Background: {ntc.get('background','?')}"
                    ),
                    impact=(
                        "Keyboard users with low vision may not see which "
                        "element currently has focus."
                    ),
                    recommendation="Ensure focus indicators have at least 3:1 contrast.",
                    severity=Severity.MEDIUM,
                ))

            fbc = ntc.get("focus_border_contrast")
            if fbc is not None and fbc < 3.0:
                if selector not in checked_selectors:
                    checked_selectors.add(selector)
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=location,
                    css_selector=selector,
                    issue=(
                        f"Focus border contrast {fbc:.2f}:1 (required: 3:1). "
                        f"Focus border: {ntc.get('focus_border_color','?')}, "
                        f"Background: {ntc.get('background','?')}"
                    ),
                    impact="Keyboard users may not perceive the focus state change.",
                    recommendation="Ensure focus indicators have at least 3:1 contrast.",
                    severity=Severity.MEDIUM,
                ))

        # Supplement: check computed_styles for UI-component tags not yet covered
        _ui_tags = {"input", "select", "textarea", "button", "a", "summary"}
        for style in (capture_data.computed_styles or []):
            tag = style.get("tag", "").lower()
            if tag not in _ui_tags:
                continue
            selector = style.get("selector", "")
            if not selector or selector in checked_selectors:
                continue
            fg = style.get("color", "")
            bg = style.get("backgroundColor", "")
            has_bg_image = style.get("hasBgImage", False)
            effective_opacity = style.get("effectiveOpacity", 1.0)

            if has_bg_image:
                findings.append(Finding(
                    id=_make_finding_id(), element=selector, css_selector=selector,
                    issue="Non-text contrast cannot be verified — UI component has background image.",
                    impact="Component boundary may be invisible against varying background.",
                    recommendation="Visually verify component has 3:1 contrast against background.",
                    severity=Severity.INFO,
                    evidence=f"UI component ({tag}) has background image/gradient",
                ))
                continue
            fg_rgb = _parse_rgb(fg, bg)
            bg_rgb = _parse_rgb(bg)
            if fg_rgb is None or bg_rgb is None:
                continue

            fg_lum = _relative_luminance(*fg_rgb)
            bg_lum = _relative_luminance(*bg_rgb)
            ratio = _contrast_ratio(fg_lum, bg_lum)
            if ratio < 3.0:
                opacity_note = ""
                if effective_opacity is not None and effective_opacity < 0.99:
                    opacity_note = (
                        f" (element opacity: {effective_opacity:.2f})"
                    )
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"Non-text UI component contrast too low: "
                        f"{ratio:.2f}:1 (required: 3:1). "
                        f"Colors: {fg} on {bg}{opacity_note}"
                    ),
                    impact="Users with low vision may not perceive the UI component.",
                    recommendation="Ensure non-text elements have at least 3:1 contrast ratio.",
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.7
        return conformance, confidence, findings


class Check_1_4_12(BaseCheck):
    """SC 1.4.12 Text Spacing (Level AA, WCAG 2.1/2.2)."""

    criterion_id = "1.4.12"
    criterion_name = "Text Spacing"
    level = "AA"
    wcag_versions = ["2.1", "2.2"]
    guideline = "1.4 Distinguishable"
    principle = "1. Perceivable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "In content implemented using markup languages that support the "
        "following text style properties, no loss of content or "
        "functionality occurs by setting all of the following and by "
        "changing no other style property: Line height to at least 1.5 "
        "times the font size; Spacing following paragraphs to at least "
        "2 times the font size; Letter spacing to at least 0.12 times "
        "the font size; Word spacing to at least 0.16 times the font size."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Check overflow elements after text spacing override
        for overflow in (capture_data.text_spacing_overflow or []):
            selector = overflow.get("selector", "element")
            tag = overflow.get("tag", "")
            text = overflow.get("text", "")
            scroll_w = overflow.get("scrollWidth", 0)
            client_w = overflow.get("clientWidth", 0)
            scroll_h = overflow.get("scrollHeight", 0)
            client_h = overflow.get("clientHeight", 0)
            ov_css = overflow.get("overflow", "")

            # Determine which axis overflows
            clips_x = scroll_w > client_w and client_w > 0
            clips_y = scroll_h > client_h and client_h > 0

            if not clips_x and not clips_y:
                continue

            clip_details: list[str] = []
            if clips_x:
                clip_details.append(
                    f"horizontally (scrollWidth {scroll_w}px > "
                    f"clientWidth {client_w}px)"
                )
            if clips_y:
                clip_details.append(
                    f"vertically (scrollHeight {scroll_h}px > "
                    f"clientHeight {client_h}px)"
                )
            clip_desc = " and ".join(clip_details)

            text_preview = ""
            if text:
                text_preview = f' Text: "{text}"'

            ov_note = ""
            if ov_css and ov_css in ("hidden", "clip"):
                ov_note = (
                    f" Container has overflow: {ov_css}, so overflowing "
                    f"content is clipped and inaccessible."
                )

            findings.append(Finding(
                id=_make_finding_id(),
                element=selector,
                issue=(
                    f"<{tag}> content overflows {clip_desc} when WCAG "
                    f"text spacing is applied.{text_preview}"
                ),
                impact=(
                    f"Users who increase text spacing for readability "
                    f"lose access to content.{ov_note}"
                ),
                recommendation=(
                    "Use flexible containers that expand with content. "
                    "Avoid fixed heights, overflow: hidden, and "
                    "text-overflow: ellipsis on text containers."
                ),
                severity=Severity.HIGH,
            ))

        # Check for fixed-height containers with overflow hidden
        for style in capture_data.computed_styles:
            selector = style.get("selector", "")
            overflow = style.get("overflow", "")
            height = style.get("height", "")
            has_text = style.get("has_text_content", False)
            line_height = style.get("line_height", style.get("line-height", ""))

            if has_text and overflow in ("hidden", "clip"):
                if height and "px" in str(height):
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Fixed-height container ({height}) with "
                            f"overflow: {overflow} may clip text when spacing increases"
                        ),
                        impact="Text may be cut off when users increase spacing.",
                        recommendation=(
                            "Use min-height instead of height, or remove "
                            "overflow: hidden on text containers."
                        ),
                        severity=Severity.MEDIUM,
                    ))

        conformance = self._determine_conformance(findings)
        confidence = 0.7
        return conformance, confidence, findings

    def get_extra_images(self, capture_data: CaptureData) -> list[str]:
        """Send text-spacing-override screenshot so the Visual AI can see
        whether applying WCAG text spacing causes clipping or overlap."""
        paths: list[str] = []
        sp = getattr(capture_data, "text_spacing_screenshot", "")
        if sp:
            paths.append(sp)
        return paths


class Check_1_4_13(BaseCheck):
    """SC 1.4.13 Content on Hover or Focus (Level AA, WCAG 2.1/2.2)."""

    criterion_id = "1.4.13"
    criterion_name = "Content on Hover or Focus"
    level = "AA"
    wcag_versions = ["2.1", "2.2"]
    guideline = "1.4 Distinguishable"
    principle = "1. Perceivable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "Where receiving and then removing pointer hover or keyboard "
        "focus triggers additional content to become visible and then "
        "hidden, the following are true: Dismissible, Hoverable, Persistent."
    )
    web_only = True

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.hover_content)

    def get_extra_images(self, capture_data: CaptureData) -> list[str]:
        """Send hover content screenshot pairs (normal/hover)."""
        paths: list[str] = []
        for hc in capture_data.hover_content:
            normal = hc.get("normal_path", "")
            hover = hc.get("hover_path", hc.get("screenshot_path", ""))
            if normal:
                paths.append(normal)
            if hover:
                paths.append(hover)
        return paths

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        tested = 0

        for hc in capture_data.hover_content:
            selector = hc.get("selector", "element")
            trigger = hc.get("trigger", "hover")
            is_dismissible = hc.get("dismissible")
            is_hoverable = hc.get("hoverable")
            is_persistent = hc.get("persistent")

            # SC 1.4.13 only applies when hover/focus actually reveals
            # new content. Without this guard every focusable element on
            # the page gets flagged when its dismissible/persistent
            # fields are None (untested) or empty.
            #
            # Two capture shapes exist:
            #   - hover entries: have hover_content (text list),
            #     new_elements_count, css_changes, hover_path
            #   - focus entries: have content_appeared (bool),
            #     focus_content (text list), before/after_screenshot
            new_elements = hc.get("new_elements_count") or 0
            content_text = hc.get("hover_content") or hc.get("focus_content") or []
            css_changes = hc.get("css_changes") or []
            content_appeared_flag = hc.get("content_appeared")
            content_appeared = (
                bool(new_elements)
                or bool(content_text)
                or bool(css_changes)
                or content_appeared_flag is True
            )
            if not content_appeared:
                continue

            probe_ran = (
                is_dismissible is not None
                or is_persistent is not None
                or is_hoverable is not None
            )
            if not probe_ran:
                continue
            tested += 1

            issues: list[str] = []
            if is_dismissible is False:
                issues.append("not dismissible (Escape key does not hide it)")
            if trigger == "hover" and is_hoverable is False:
                issues.append("not hoverable (pointer cannot move to content)")
            if is_persistent is False:
                issues.append("not persistent (disappears prematurely)")

            if issues:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"Content on {trigger} fails: "
                        + "; ".join(issues)
                    ),
                    impact=(
                        "Users may not be able to read or interact with "
                        "additional content that appears on hover/focus."
                    ),
                    recommendation=(
                        "Ensure hover/focus content is: (1) dismissible via "
                        "Escape key, (2) hoverable by pointer, (3) persistent "
                        "until dismissed or trigger removed."
                    ),
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings, tested)
        confidence = 0.7
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_1_4_1(),
        Check_1_4_2(),
        Check_1_4_3(),
        Check_1_4_4(),
        Check_1_4_5(),
        Check_1_4_10(),
        Check_1_4_11(),
        Check_1_4_12(),
        Check_1_4_13(),
    ]
