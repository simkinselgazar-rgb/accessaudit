"""WCAG Guideline 1.2 - Time-based Media AAA checks."""
from __future__ import annotations

from checks.base import BaseCheck, _make_finding_id
from checks.checks_1_2 import _page_has_media_iframe
from functions.media import has_track as _has_track, media_type as _media_type
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_1_2_6(BaseCheck):
    """SC 1.2.6 Sign Language (Prerecorded) (Level AAA)."""

    criterion_id = "1.2.6"
    criterion_name = "Sign Language (Prerecorded)"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.2 Time-based Media"
    principle = "1. Perceivable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "Sign language interpretation is provided for all prerecorded "
        "audio content in synchronized media."
    )

    # Media playback screenshots captured during the recording phase.
    _SCREENSHOT_FIELDS = [("media", "recording", "playback_screenshots")]

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send recorded video — VL model watches it to identify
        sign language interpreter window (PiP, inset, separate feed)."""
        for m in capture_data.media:
            rec = m.get("recording", {})
            v = rec.get("captions_on_video", "")
            if v:
                return v
        return None

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return any(
            _media_type(m) == "video" and not m.get("live", False)
            for m in capture_data.media
        ) or _page_has_media_iframe(capture_data)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        for m in capture_data.media:
            if _media_type(m) != "video" or m.get("live", False):
                continue
            selector = m.get("selector", "video element")
            # Also check recording data for sign language detection
            rec = m.get("recording", {})
            has_sign = (
                _has_track(m, "sign")
                or m.get("has_sign_language", False)
                or rec.get("sign_language_detected", False)
            )
            if not has_sign:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Prerecorded video has no sign language interpretation",
                    impact="Deaf users who rely on sign language cannot access audio content.",
                    recommendation="Provide sign language interpretation for prerecorded video content.",
                    severity=Severity.MEDIUM,
                ))
        conformance = self._determine_conformance(findings, len(capture_data.media))
        return conformance, 0.5, findings


class Check_1_2_7(BaseCheck):
    """SC 1.2.7 Extended Audio Description (Prerecorded) (Level AAA)."""

    criterion_id = "1.2.7"
    criterion_name = "Extended Audio Description (Prerecorded)"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.2 Time-based Media"
    principle = "1. Perceivable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "Where pauses in foreground audio are insufficient to allow audio "
        "descriptions to convey the sense of the video, extended audio "
        "description is provided for all prerecorded video content in "
        "synchronized media."
    )

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send recorded video — VL model evaluates if extended audio
        description (video pausing for description) is present."""
        for m in capture_data.media:
            rec = m.get("recording", {})
            v = rec.get("captions_on_video", "")
            if v:
                return v
        return None

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return any(
            _media_type(m) == "video" and not m.get("live", False)
            for m in capture_data.media
        ) or _page_has_media_iframe(capture_data)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        for m in capture_data.media:
            if _media_type(m) != "video" or m.get("live", False):
                continue
            selector = m.get("selector", "video element")
            has_extended = m.get("has_extended_audio_description", False)
            has_descriptions = _has_track(m, "descriptions")
            if not has_extended and not has_descriptions:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Prerecorded video has no extended audio description",
                    impact=(
                        "Blind users miss visual information when pauses in "
                        "the audio are insufficient for standard descriptions."
                    ),
                    recommendation=(
                        "Provide extended audio descriptions that pause the "
                        "video when additional description time is needed."
                    ),
                    severity=Severity.MEDIUM,
                ))
        conformance = self._determine_conformance(findings, len(capture_data.media))
        return conformance, 0.4, findings


class Check_1_2_8(BaseCheck):
    """SC 1.2.8 Media Alternative (Prerecorded) (Level AAA)."""

    criterion_id = "1.2.8"
    criterion_name = "Media Alternative (Prerecorded)"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.2 Time-based Media"
    principle = "1. Perceivable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "An alternative for time-based media is provided for all "
        "prerecorded synchronized media and for all prerecorded "
        "video-only media."
    )

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send recorded video — VL model evaluates if a full media
        text alternative (transcript with visual descriptions) exists."""
        for m in capture_data.media:
            rec = m.get("recording", {})
            v = rec.get("captions_on_video", "")
            if v:
                return v
        return None

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return any(
            _media_type(m) == "video" and not m.get("live", False)
            for m in capture_data.media
        ) or _page_has_media_iframe(capture_data)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        for m in capture_data.media:
            if _media_type(m) != "video" or m.get("live", False):
                continue
            selector = m.get("selector", "video element")
            has_alt = m.get("has_text_alternative", False) or m.get("has_transcript_link", False)
            if not has_alt:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Prerecorded video has no full media text alternative",
                    impact=(
                        "Users who cannot perceive the video content lack a "
                        "complete text alternative."
                    ),
                    recommendation=(
                        "Provide a full text alternative (transcript with "
                        "visual descriptions) for the video content."
                    ),
                    severity=Severity.MEDIUM,
                ))
        conformance = self._determine_conformance(findings, len(capture_data.media))
        return conformance, 0.5, findings


class Check_1_2_9(BaseCheck):
    """SC 1.2.9 Audio-only (Live) (Level AAA)."""

    criterion_id = "1.2.9"
    criterion_name = "Audio-only (Live)"
    level = "AAA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.2 Time-based Media"
    principle = "1. Perceivable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "An alternative for time-based media that presents equivalent "
        "information for live audio-only content is provided."
    )

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send recorded media — VL model checks if live audio has
        a real-time text alternative visible."""
        for m in capture_data.media:
            rec = m.get("recording", {})
            v = rec.get("captions_on_video", "")
            if v:
                return v
        return None

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return any(
            m.get("live", False) and _media_type(m) == "audio"
            for m in capture_data.media
        ) or _page_has_media_iframe(capture_data)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        for m in capture_data.media:
            if not m.get("live", False) or _media_type(m) != "audio":
                continue
            selector = m.get("selector", "live audio element")
            has_live_text = m.get("has_live_text_alternative", False)
            has_captions = m.get("has_live_captions", False)
            if not has_live_text and not has_captions:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Live audio has no text alternative",
                    impact="Deaf users cannot access live audio content.",
                    recommendation=(
                        "Provide a real-time text alternative such as CART "
                        "or live transcription for the audio stream."
                    ),
                    severity=Severity.MEDIUM,
                ))
        conformance = self._determine_conformance(findings, len(capture_data.media))
        return conformance, 0.4, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_1_2_6(),
        Check_1_2_7(),
        Check_1_2_8(),
        Check_1_2_9(),
    ]
