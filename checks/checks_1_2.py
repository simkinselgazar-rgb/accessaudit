"""WCAG Guideline 1.2 - Time-based Media (A/AA) checks."""
from __future__ import annotations

import logging
import re

import httpx

from checks.base import BaseCheck, _make_finding_id
from config import WHISPER_API_URL
from functions.media import has_track as _has_track, media_type as _media_type
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
    TTResult,
    TTSubTestResult,
)

logger = logging.getLogger(__name__)

# Known video embed platform patterns (domain substrings)
_VIDEO_EMBED_DOMAINS = [
    ("youtube.com", "YouTube"),
    ("youtube-nocookie.com", "YouTube"),
    ("youtu.be", "YouTube"),
    ("vimeo.com", "Vimeo"),
    ("dailymotion.com", "Dailymotion"),
    ("wistia.com", "Wistia"),
    ("wistia.net", "Wistia"),
    ("brightcove.com", "Brightcove"),
    ("brightcove.net", "Brightcove"),
    ("vidyard.com", "Vidyard"),
    ("kaltura.com", "Kaltura"),
    ("jwplatform.com", "JW Player"),
    ("jwplayer.com", "JW Player"),
    ("twitch.tv", "Twitch"),
    ("facebook.com/plugins/video", "Facebook Video"),
    ("loom.com", "Loom"),
]


def _video_has_audio(media: dict, capture_data: CaptureData) -> bool:
    """Return True only when the video element actually has an audio track.

    WCAG 1.2.2 (Captions), 1.2.3 (Audio Description or Media Alternative),
    and 1.2.5 (Audio Description) all apply to "synchronized media" —
    defined as audio combined with video. A silent video (muted attribute
    AND no audible audio detected by the deterministic probe) is
    "video-only" content covered ONLY by SC 1.2.1, not by 1.2.2/1.2.3/1.2.5.

    The bug being fixed: the prior is_applicable() for these three SCs
    only checked "is there a <video>?" and flagged the silent university hero
    video as missing captions / audio descriptions. The hero has
    muted="" and the audio_detection probe reports audio_type="silence",
    so unmuting plays no audio either — there is nothing to caption,
    no narration to describe.

    Returns True (so the SC IS applicable) when EITHER:
      - the media is not muted (audio is intended to play), OR
      - audio_detection found audible content (probe heard sound).
    Returns False only when both signals agree the media is silent.
    """
    is_muted = bool(media.get("muted"))
    if not is_muted:
        return True  # not muted → audio is intended to play
    ad = getattr(capture_data, "audio_detection", None) or {}
    audio_type = (ad.get("audio_type") or "").lower()
    has_autoplay_audio = bool(ad.get("has_autoplay_audio"))
    if has_autoplay_audio:
        return True
    if audio_type and audio_type not in ("silence", "none", ""):
        return True  # probe heard speech / music / other
    return False


def _detect_video_embeds(capture_data: CaptureData) -> list[dict]:
    """Check iframes for known video embed platforms.

    Returns a list of dicts with 'selector', 'src', 'platform', and
    optionally 'caption_info' keys. YouTube caption data is pulled from
    capture_data.video_embed_captions if available.
    """
    detected: list[dict] = []
    for iframe in capture_data.iframes:
        src = iframe.get("src", "") or iframe.get("data-src", "") or ""
        src_lower = (src or "").lower()
        for domain, platform in _VIDEO_EMBED_DOMAINS:
            if domain in src_lower:
                embed = {
                    "selector": iframe.get("selector", "iframe"),
                    "src": src,
                    "platform": platform,
                }
                # Attach cached YouTube caption data if available
                if platform == "YouTube":
                    vid_id = _extract_youtube_video_id(src)
                    if vid_id and hasattr(capture_data, "video_embed_captions"):
                        embed["caption_info"] = capture_data.video_embed_captions.get(vid_id)
                        embed["video_id"] = vid_id
                detected.append(embed)
                break
    return detected


# Iframe `allow`-attribute tokens a browser only grants to media players,
# and words in an iframe's title/name/src that indicate time-based media.
_MEDIA_ALLOW_TOKENS = ("encrypted-media", "picture-in-picture", "autoplay", "fullscreen")
_MEDIA_NAME_TOKENS = (
    "video", "player", "audio", "webinar", "livestream", "live stream",
    "podcast", "youtube", "vimeo",
)


def _iframe_is_media_candidate(iframe: dict) -> bool:
    """True when an iframe plausibly embeds time-based media.

    This is a pre-judge inclusiveness check, NOT a verdict — the judge
    makes the real applicability call from full-page evidence. It must
    err toward INCLUDING an iframe: a missed media iframe silently skips
    the SC entirely (verified bug — the embed.repd.us video iframe on
    loudoun.gov was auto-marked Not Applicable because repd.us is not a
    hardcoded video host, so 7 media SCs were never judged). Any one
    signal qualifies:
      - src on a known video-embed host (_VIDEO_EMBED_DOMAINS)
      - an `allow` attribute granting media-playback permissions, or
        `allowfullscreen` set — browsers grant these to players
      - a media word in the title / name / aria-label / src
    A non-media iframe that trips this only costs one judge call, which
    then correctly rules the SC Not Applicable.
    """
    src = (iframe.get("src") or iframe.get("data-src") or "").lower()
    for domain, _ in _VIDEO_EMBED_DOMAINS:
        if domain in src:
            return True
    allow = (iframe.get("allow") or "").lower()
    if any(tok in allow for tok in _MEDIA_ALLOW_TOKENS):
        return True
    if str(iframe.get("allowfullscreen") or "").strip().lower() in ("true", "allowfullscreen", "1"):
        return True
    haystack = " ".join(
        str(iframe.get(k) or "")
        for k in ("title", "name", "aria_label", "aria-label", "ariaLabel")
    ).lower() + " " + src
    return any(tok in haystack for tok in _MEDIA_NAME_TOKENS)


def _page_has_media_iframe(capture_data: CaptureData) -> bool:
    """True when any iframe on the page plausibly embeds time-based media."""
    return any(
        _iframe_is_media_candidate(f) for f in (capture_data.iframes or [])
    )


def _extract_youtube_video_id(src: str) -> str | None:
    """Extract the video ID from a YouTube embed URL."""
    import re
    # Match /embed/VIDEO_ID, /v/VIDEO_ID, or ?v=VIDEO_ID
    patterns = [
        r"youtube\.com/embed/([a-zA-Z0-9_-]{11})",
        r"youtube-nocookie\.com/embed/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/v/([a-zA-Z0-9_-]{11})",
        r"youtu\.be/([a-zA-Z0-9_-]{11})",
        r"[?&]v=([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        m = re.search(pattern, src)
        if m:
            return m.group(1)
    return None


async def _check_youtube_captions(video_id: str) -> dict:
    """Check if a YouTube video has captions available.

    Uses YouTube's timedtext list API (no API key required).
    Returns dict with has_captions, caption_languages, etc.
    """
    import httpx
    result = {"video_id": video_id, "has_captions": False, "caption_languages": []}
    try:
        url = f"https://www.youtube.com/api/timedtext?type=list&v={video_id}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                text = resp.text
                # The response is XML with <track> elements
                import re
                tracks = re.findall(r'lang_code="([^"]+)"', text)
                if tracks:
                    result["has_captions"] = True
                    result["caption_languages"] = tracks
    except Exception:
        pass  # Non-critical — if API fails, we just don't know
    return result


class Check_1_2_1(BaseCheck):
    """SC 1.2.1 Audio-only and Video-only (Prerecorded) (Level A)."""

    criterion_id = "1.2.1"
    criterion_name = "Audio-only and Video-only (Prerecorded)"
    needs_audio = True  # Must hear audio content
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.2 Time-based Media"
    principle = "1. Perceivable"
    ict_baseline = "16"
    tt_tests = ["16.A", "16.B"]
    normative_text = (
        "For prerecorded audio-only and prerecorded video-only media, "
        "the following are true: Prerecorded Audio-only: An alternative "
        "for time-based media is provided. Prerecorded Video-only: "
        "Either an alternative for time-based media or an audio track "
        "is provided."
    )
    off_scope_keywords = {
        "captions": ["caption track", "closed caption"],
        "audio_description": ["audio description", "described video"],
    }

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send recorded media playback video to VL model."""
        for m in capture_data.media:
            rec = m.get("recording", {})
            v = rec.get("captions_on_video", "")
            if v:
                return v
        return None

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(
            capture_data.media
            or _detect_video_embeds(capture_data)
            or _page_has_media_iframe(capture_data)
        )

    def get_extra_images(self, capture_data: CaptureData) -> list[str]:
        """Send transcript verification screenshots to AI."""
        paths: list[str] = []
        for v in getattr(capture_data, "transcript_verifications", []):
            for key in ("before_screenshot", "after_screenshot", "destination_screenshot"):
                p = v.get(key, "")
                if p:
                    paths.append(p)
        for m in capture_data.media:
            rec = m.get("recording", {})
            for p in rec.get("playback_screenshots", []):
                if p:
                    paths.append(p)
        return paths

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Build a lookup of transcript verification results
        transcript_verified = {}
        for tv in getattr(capture_data, "transcript_verifications", []):
            transcript_verified[tv.get("selector", "")] = tv

        for m in capture_data.media:
            selector = m.get("selector", "media element")
            mt = _media_type(m)
            is_live = m.get("live", False)
            if is_live:
                continue  # This SC applies to prerecorded only

            # Skip embeds on known video platforms (YouTube, Vimeo,
            # etc.). These are virtually always synchronized media —
            # audio + video. SC 1.2.1 governs audio-only and video-only
            # content; synchronized media is covered by 1.2.2 / 1.2.3 /
            # 1.2.5 instead. Without this guard every YouTube embed
            # gets flagged as "video-only no audio track" because the
            # capture cannot reach into the iframe to inspect the
            # audio stream.
            tag = (m.get("tag") or m.get("tagName") or "").lower()
            src_lower = (m.get("src") or "").lower()
            if tag == "iframe" and any(
                d in src_lower for d, _ in _VIDEO_EMBED_DOMAINS
            ):
                continue

            has_transcript_link = m.get("has_transcript_link", False)
            has_text_alternative = m.get("has_text_alternative", False)

            # Check if transcript was actually verified by clicking
            transcript_actually_works = False
            for tv in getattr(capture_data, "transcript_verifications", []):
                if tv.get("transcript_found"):
                    transcript_actually_works = True
                    break

            if mt == "audio":
                # Audio-only needs a transcript
                if not has_transcript_link and not has_text_alternative:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue="Prerecorded audio has no transcript or text alternative",
                        impact=(
                            "Deaf and hard-of-hearing users cannot access "
                            "the audio content."
                        ),
                        recommendation=(
                            "Provide a text transcript that conveys all spoken "
                            "content and relevant sound effects."
                        ),
                        severity=Severity.HIGH,
                    ))
                elif has_transcript_link and not transcript_actually_works:
                    # Button exists but transcript didn't load
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            "Transcript link/button found but clicking it did not "
                            "reveal transcript content"
                        ),
                        impact=(
                            "Users who need a transcript cannot access one even "
                            "though the UI suggests one exists."
                        ),
                        recommendation=(
                            "Ensure the transcript link leads to actual transcript "
                            "content or that the expand/toggle mechanism works."
                        ),
                        severity=Severity.HIGH,
                    ))
            elif mt == "video":
                # Video-only needs a text alternative or audio track
                has_audio_track = m.get("has_audio", False)
                if not has_transcript_link and not has_text_alternative and not has_audio_track:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            "Prerecorded video-only has no text alternative "
                            "and no audio track"
                        ),
                        impact=(
                            "Blind users cannot access the visual information "
                            "in the video."
                        ),
                        recommendation=(
                            "Provide a text alternative describing the video "
                            "content, or add an audio track narrating the "
                            "visual information."
                        ),
                        severity=Severity.HIGH,
                    ))
                elif has_transcript_link and not transcript_actually_works:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            "Transcript link/button found but clicking it did not "
                            "reveal transcript content"
                        ),
                        impact=(
                            "Users who need a transcript cannot access one even "
                            "though the UI suggests one exists."
                        ),
                        recommendation=(
                            "Ensure the transcript link leads to actual transcript "
                            "content or that the expand/toggle mechanism works."
                        ),
                        severity=Severity.HIGH,
                    ))

        # Detect embedded video platforms in iframes
        video_embeds = _detect_video_embeds(capture_data)
        for embed in video_embeds:
            platform = embed["platform"]
            caption_info = embed.get("caption_info")

            if caption_info and caption_info.get("has_captions"):
                langs = ", ".join(caption_info["caption_languages"])
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=embed["selector"],
                    issue=(
                        f"Embedded {platform} video has captions available "
                        f"(languages: {langs}) but audio description and "
                        f"transcript availability could not be verified"
                    ),
                    impact=(
                        "Captions are available on the platform, but users "
                        "depend on the embed settings to enable them. Audio "
                        "descriptions and transcripts may still be missing."
                    ),
                    recommendation=(
                        f"Verify that this {platform} embed: "
                        f"(1) has captions enabled by default or easily toggled, "
                        f"(2) provides audio description or text alternative "
                        f"(SC 1.2.3/1.2.5), and (3) provides a transcript on the "
                        f"page if applicable (SC 1.2.1)."
                    ),
                    severity=Severity.LOW,
                ))
            else:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=embed["selector"],
                    issue=(
                        f"Embedded {platform} video detected"
                        + (" — no captions found on the platform"
                           if caption_info and not caption_info.get("has_captions")
                           else " — SC 1.2.x criteria require evaluation")
                    ),
                    impact=(
                        "Embedded video content may lack captions, audio descriptions, "
                        "or transcripts. These cannot be fully verified programmatically "
                        "for third-party embeds."
                    ),
                    recommendation=(
                        f"Verify that this {platform} embed has: "
                        f"(1) captions/subtitles (SC 1.2.2), "
                        f"(2) audio description or text alternative (SC 1.2.3/1.2.5), "
                        f"and (3) a transcript if applicable (SC 1.2.1). "
                        f"Do not mark 1.2.x criteria as N/A when embedded video is present."
                    ),
                    severity=Severity.MEDIUM if not caption_info else Severity.HIGH,
                ))

        total_media = len(capture_data.media) + len(video_embeds)
        conformance = self._determine_conformance(findings, total_media)
        # Higher confidence when we actually clicked and verified transcripts
        has_verifications = bool(getattr(capture_data, "transcript_verifications", []))
        confidence = 0.85 if has_verifications else 0.7
        return conformance, confidence, findings

    def _generate_tt_results(
        self, findings: list[Finding], capture_data: CaptureData
    ) -> list[TTSubTestResult]:
        not_app = not self.is_applicable(capture_data)
        has_fail = any(f.severity in (Severity.HIGH, Severity.MEDIUM) for f in findings)
        audio_fail = any("audio" in f.issue.lower() for f in findings if f.severity == Severity.HIGH)
        video_fail = any("video" in f.issue.lower() for f in findings if f.severity == Severity.HIGH)
        return [
            TTSubTestResult(
                tt_id="16.A",
                name="Audio-only has transcript",
                result=TTResult.DNA if not_app else TTResult.FAIL if audio_fail else TTResult.PASS,
            ),
            TTSubTestResult(
                tt_id="16.B",
                name="Video-only has alternative or audio track",
                result=TTResult.DNA if not_app else TTResult.FAIL if video_fail else TTResult.PASS,
            ),
        ]


class Check_1_2_2(BaseCheck):
    """SC 1.2.2 Captions (Prerecorded) (Level A)."""

    criterion_id = "1.2.2"
    criterion_name = "Captions (Prerecorded)"
    level = "A"
    needs_audio = True  # Must hear to verify captions match audio
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.2 Time-based Media"
    principle = "1. Perceivable"
    ict_baseline = "17"
    tt_tests = ["17.A", "17.B", "17.C", "17.D"]
    normative_text = (
        "Captions are provided for all prerecorded audio content in "
        "synchronized media, except when the media is a media alternative "
        "for text and is clearly labeled as such."
    )
    off_scope_keywords = {
        "audio_description": ["audio description", "described video"],
    }

    def is_applicable(self, capture_data: CaptureData) -> bool:
        # SC 1.2.2 / 1.2.3 / 1.2.5 apply to "synchronized media" — audio
        # combined with video. A silent muted video (e.g. a university's hero) is
        # video-only content; SC 1.2.1 covers it, these three do not.
        # _video_has_audio returns True for video embeds (we cannot probe
        # remote players' audio reliably) so YouTube/Vimeo iframes still
        # count.
        has_audible_video = any(
            _media_type(m) == "video"
            and not m.get("live", False)
            and _video_has_audio(m, capture_data)
            for m in capture_data.media
        )
        return (
            has_audible_video
            or bool(_detect_video_embeds(capture_data))
            or _page_has_media_iframe(capture_data)
        )

    def get_extra_images(self, capture_data: CaptureData) -> list[str]:
        """Send caption before/after screenshots + playback screenshots.

        The AI sees:
        - Screenshot BEFORE captions enabled (no text overlay)
        - Screenshot AFTER captions enabled (text overlay visible)
        - Any playback screenshots from the recording phase
        This lets the VL model visually confirm captions actually render.
        """
        paths: list[str] = []
        for m in capture_data.media:
            rec = m.get("recording", {})
            # Caption toggle before/after screenshots
            for key in ("captions_before_screenshot", "captions_after_screenshot"):
                p = rec.get(key, "")
                if p:
                    paths.append(p)
            # Playback screenshots
            for p in rec.get("playback_screenshots", []):
                if p:
                    paths.append(p)
        return paths

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send the caption toggle video to VL model (preferred) or
        the captions-on recording.

        Priority:
        1. Caption toggle video — shows CC button being clicked and
           captions appearing, so VL model watches the actual activation
        2. Captions-on video — 15s of playback with captions enabled
        """
        for m in capture_data.media:
            rec = m.get("recording", {})
            # Prefer the toggle video (shows the CC activation)
            v = rec.get("caption_toggle_video", "")
            if v:
                return v
            # Fallback to the captions-on recording
            v = rec.get("captions_on_video", "")
            if v:
                return v
        return None

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        for m in capture_data.media:
            if _media_type(m) != "video" or m.get("live", False):
                continue

            selector = m.get("selector", "video element")
            has_captions = _has_track(m, "captions") or _has_track(m, "subtitles")
            has_embedded = m.get("has_embedded_captions", False)
            aria_label = m.get("ariaLabel", "") or ""
            duration = m.get("duration", None)

            if not has_captions and not has_embedded:
                # Build a descriptive issue string with available metadata
                detail_parts = ["Prerecorded video has no caption track"]
                if aria_label:
                    detail_parts.append(f"(aria-label: \"{aria_label}\")")
                if duration is not None:
                    try:
                        dur_s = float(duration)
                        if dur_s > 0:
                            mins, secs = divmod(int(dur_s), 60)
                            detail_parts.append(
                                f"[duration: {mins}m {secs}s]"
                            )
                    except (ValueError, TypeError):
                        pass
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=" ".join(detail_parts),
                    impact=(
                        "Deaf and hard-of-hearing users cannot access the "
                        "audio content of the video."
                    ),
                    recommendation=(
                        "Add a <track kind=\"captions\"> element with "
                        "synchronized captions in WebVTT or SRT format."
                    ),
                    severity=Severity.HIGH,
                ))
            elif has_captions:
                # Captions exist -- check if any track has an empty src
                tracks = m.get("tracks", [])
                for track in tracks:
                    kind = (track.get("kind") or "").lower()
                    if kind in ("captions", "subtitles"):
                        src = track.get("src", "") or ""
                        label = track.get("label", "") or ""
                        if not src:
                            findings.append(Finding(
                                id=_make_finding_id(),
                                element=selector,
                                issue=(
                                    f"Caption track"
                                    + (f" \"{label}\"" if label else "")
                                    + " has no src attribute"
                                ),
                                impact=(
                                    "The caption track element exists but "
                                    "has no source file, so no captions "
                                    "will be displayed."
                                ),
                                recommendation=(
                                    "Set the src attribute to a valid "
                                    "WebVTT or SRT caption file URL."
                                ),
                                severity=Severity.HIGH,
                            ))

        # -- Check transcript_buttons for caption-related controls --
        for btn in getattr(capture_data, "transcript_buttons", []):
            btn_label = btn.get("label", "") or btn.get("text", "") or ""
            btn_selector = btn.get("selector", "button")
            # If a button looks like it toggles captions, note it as
            # supporting evidence (reduces severity of missing <track>)
            if any(
                kw in btn_label.lower()
                for kw in ("caption", "cc", "subtitle", "closed caption")
            ):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=btn_selector,
                    issue=(
                        f"Caption toggle button found (\"{btn_label}\") -- "
                        f"verify it activates synchronised captions"
                    ),
                    impact=(
                        "A caption button suggests captions may be available "
                        "through the player UI even if no <track> element is "
                        "present in the DOM."
                    ),
                    recommendation=(
                        "Manually verify that activating this button "
                        "displays accurate, synchronised captions."
                    ),
                    severity=Severity.INFO,
                ))

        # -- Use transcript_verifications to validate caption availability --
        for tv in getattr(capture_data, "transcript_verifications", []):
            tv_selector = tv.get("selector", "")
            transcript_found = tv.get("transcript_found", False)
            captured_text = tv.get("captured_text", "") or ""
            if not transcript_found:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=tv_selector or "transcript area",
                    issue=(
                        "Transcript/caption verification attempted but no "
                        "transcript content was found after activation"
                    ),
                    impact=(
                        "The page may advertise captions or a transcript "
                        "but the mechanism does not produce visible text."
                    ),
                    recommendation=(
                        "Ensure the caption/transcript mechanism works "
                        "and displays content when activated."
                    ),
                    severity=Severity.HIGH,
                ))
            elif len(captured_text.strip()) < 20:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=tv_selector or "transcript area",
                    issue=(
                        "Transcript content found but very short "
                        f"({len(captured_text.strip())} chars) -- may be "
                        "incomplete"
                    ),
                    impact=(
                        "An extremely short transcript is unlikely to "
                        "convey the full audio content."
                    ),
                    recommendation=(
                        "Verify that the full transcript or caption track "
                        "is complete and covers all spoken content."
                    ),
                    severity=Severity.MEDIUM,
                ))

        # -- Whisper transcription comparison ---------------------------------
        # For videos that have both a media URL and a caption track URL,
        # call the Whisper server to compare the audio against the captions.
        whisper_ran = False
        for m in capture_data.media:
            if _media_type(m) != "video" or m.get("live", False):
                continue
            media_url = m.get("media_url", "")
            caption_urls = m.get("caption_urls", [])
            if not media_url or not caption_urls:
                continue

            selector = m.get("selector", "video element")
            caption_url = caption_urls[0]  # compare against first track
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(
                        f"{WHISPER_API_URL}/compare",
                        json={
                            "media_url": media_url,
                            "caption_url": caption_url,
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()

                overall_accuracy = data.get("overall_accuracy", 0.0)
                missing_segments = data.get("missing_segments", [])
                inaccurate_segments = data.get("inaccurate_segments", [])
                whisper_ran = True

                if overall_accuracy >= 80:
                    # Captions are good -- optionally note minor gaps
                    if missing_segments or inaccurate_segments:
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=selector,
                            issue=(
                                f"Caption accuracy is {overall_accuracy:.0f}% "
                                f"({len(missing_segments)} missing, "
                                f"{len(inaccurate_segments)} inaccurate segments)"
                            ),
                            impact="Minor caption gaps may affect comprehension.",
                            recommendation=(
                                "Review flagged segments and correct captions."
                            ),
                            severity=Severity.LOW,
                        ))
                elif overall_accuracy >= 50:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Caption accuracy is only {overall_accuracy:.0f}% "
                            f"(partially supports). "
                            f"{len(missing_segments)} missing segments, "
                            f"{len(inaccurate_segments)} inaccurate segments"
                        ),
                        impact=(
                            "Deaf and hard-of-hearing users will miss "
                            "significant portions of the audio content."
                        ),
                        recommendation=(
                            "Improve caption accuracy: fix inaccurate segments "
                            "and add missing ones."
                        ),
                        severity=Severity.MEDIUM,
                    ))
                else:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Caption accuracy is {overall_accuracy:.0f}% "
                            f"(does not support). "
                            f"{len(missing_segments)} missing segments, "
                            f"{len(inaccurate_segments)} inaccurate segments"
                        ),
                        impact=(
                            "Captions are largely inaccurate or incomplete, "
                            "making the video inaccessible to deaf and "
                            "hard-of-hearing users."
                        ),
                        recommendation=(
                            "Rewrite captions to accurately reflect all "
                            "spoken content and relevant sound effects."
                        ),
                        severity=Severity.HIGH,
                    ))
            except Exception as exc:
                logger.debug(
                    "Whisper comparison unavailable for %s: %s",
                    selector, exc,
                )
                # Whisper unavailable -- skip gracefully, don't fail the check

        # -- Fallback: use caption_verifier with recorded media files --------
        # If the /compare endpoint wasn't available but we have recorded
        # media files and transcript text, use our local pipeline.
        if not whisper_ran:
            try:
                from analysis.caption_verifier import verify_caption_accuracy

                for m in capture_data.media:
                    if _media_type(m) != "video" or m.get("live", False):
                        continue
                    rec = m.get("recording", {})
                    # Use the captions-off recording (raw audio without overlay)
                    video_file = rec.get("captions_off_video", "") or rec.get("captions_on_video", "")
                    if not video_file:
                        continue

                    # Get displayed caption/transcript text
                    displayed_text = ""
                    for tv in getattr(capture_data, "transcript_verifications", []):
                        ct = tv.get("captured_text", "")
                        if ct:
                            displayed_text += ct + "\n"
                    # Also check track src content if available
                    for track in m.get("tracks", []):
                        track_text = track.get("content", "")
                        if track_text:
                            displayed_text += track_text + "\n"

                    if not displayed_text.strip():
                        continue

                    work_dir = getattr(capture_data, "captures_dir", "") or ""
                    if not work_dir:
                        continue

                    result = await verify_caption_accuracy(
                        video_file, displayed_text, work_dir,
                    )
                    if result and result.total_segments > 0:
                        whisper_ran = True
                        selector = m.get("selector", "video element")
                        accuracy_pct = result.overall_accuracy * 100
                        if accuracy_pct >= 80:
                            if result.missing_segments or result.inaccurate_segments:
                                findings.append(Finding(
                                    id=_make_finding_id(),
                                    element=selector,
                                    issue=(
                                        f"Caption accuracy is {accuracy_pct:.0f}% "
                                        f"(verified via Whisper transcription). "
                                        f"{len(result.missing_segments)} missing, "
                                        f"{len(result.inaccurate_segments)} inaccurate segments"
                                    ),
                                    impact="Minor caption gaps may affect comprehension.",
                                    recommendation="Review flagged segments for accuracy.",
                                    severity=Severity.LOW,
                                ))
                        elif accuracy_pct >= 50:
                            findings.append(Finding(
                                id=_make_finding_id(),
                                element=selector,
                                issue=(
                                    f"Caption accuracy is only {accuracy_pct:.0f}% "
                                    f"(verified via Whisper). {len(result.missing_segments)} "
                                    f"missing, {len(result.inaccurate_segments)} inaccurate"
                                ),
                                impact="Deaf and hard-of-hearing users will miss "
                                       "significant portions of the audio content.",
                                recommendation="Improve caption accuracy.",
                                severity=Severity.MEDIUM,
                            ))
                        else:
                            findings.append(Finding(
                                id=_make_finding_id(),
                                element=selector,
                                issue=(
                                    f"Caption accuracy is {accuracy_pct:.0f}% "
                                    f"(verified via Whisper). Captions are largely "
                                    f"inaccurate or incomplete."
                                ),
                                impact="Captions are unusable for deaf and "
                                       "hard-of-hearing users.",
                                recommendation="Captions need significant correction.",
                                severity=Severity.HIGH,
                            ))
            except Exception as exc:
                logger.debug("Caption verifier fallback failed: %s", exc)

        # -- Check embedded video platforms for captions --
        video_embeds = _detect_video_embeds(capture_data)
        for embed in video_embeds:
            platform = embed["platform"]
            caption_info = embed.get("caption_info")
            if caption_info and caption_info.get("has_captions"):
                langs = ", ".join(caption_info["caption_languages"])
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=embed["selector"],
                    issue=(
                        f"Embedded {platform} video has captions available "
                        f"on the platform (languages: {langs})"
                    ),
                    impact=(
                        "Captions exist on the platform but may not be "
                        "enabled by default in the embed."
                    ),
                    recommendation=(
                        f"Verify that captions are enabled or easily toggled "
                        f"in the {platform} embed player."
                    ),
                    severity=Severity.LOW,
                ))
            elif caption_info and not caption_info.get("has_captions"):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=embed["selector"],
                    issue=(
                        f"Embedded {platform} video has no captions "
                        f"available on the platform"
                    ),
                    impact=(
                        "Deaf and hard-of-hearing users cannot access the "
                        "audio content of the video."
                    ),
                    recommendation=(
                        f"Add captions to the {platform} video or provide "
                        f"a synchronized caption track on the page."
                    ),
                    severity=Severity.HIGH,
                ))
            else:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=embed["selector"],
                    issue=(
                        f"Embedded {platform} video — caption availability "
                        f"could not be verified"
                    ),
                    impact=(
                        "Deaf and hard-of-hearing users may not be able to "
                        "access the audio content."
                    ),
                    recommendation=(
                        f"Verify that this {platform} embed has captions."
                    ),
                    severity=Severity.MEDIUM,
                ))

        total_media = len(capture_data.media) + len(video_embeds)
        conformance = self._determine_conformance(findings, total_media)
        confidence = 0.90 if whisper_ran else 0.85
        return conformance, confidence, findings

    def _generate_tt_results(
        self, findings: list[Finding], capture_data: CaptureData
    ) -> list[TTSubTestResult]:
        not_app = not self.is_applicable(capture_data)
        has_fail = any(f.severity == Severity.HIGH for f in findings)
        has_accuracy = any("accuracy" in f.issue.lower() for f in findings)
        return [
            TTSubTestResult(
                tt_id="17.A",
                name="Media player provides captions",
                result=TTResult.DNA if not_app else TTResult.FAIL if has_fail else TTResult.PASS,
            ),
            TTSubTestResult(
                tt_id="17.B",
                name="Captions are synchronized",
                result=TTResult.DNA if not_app else TTResult.NOT_TESTED,
            ),
            TTSubTestResult(
                tt_id="17.C",
                name="Captions are accurate",
                result=(
                    TTResult.DNA if not_app
                    else TTResult.FAIL if has_fail and has_accuracy
                    else TTResult.PASS if has_accuracy
                    else TTResult.NOT_TESTED
                ),
            ),
            TTSubTestResult(
                tt_id="17.D",
                name="Captions do not obscure content",
                result=TTResult.DNA if not_app else TTResult.NOT_TESTED,
            ),
        ]


class Check_1_2_3(BaseCheck):
    """SC 1.2.3 Audio Description or Media Alternative (Prerecorded) (Level A)."""

    criterion_id = "1.2.3"
    criterion_name = "Audio Description or Media Alternative (Prerecorded)"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.2 Time-based Media"
    principle = "1. Perceivable"
    ict_baseline = "18"
    tt_tests = ["18.A"]
    normative_text = (
        "An alternative for time-based media or audio description of the "
        "prerecorded video content is provided for synchronized media, "
        "except when the media is a media alternative for text."
    )

    _SCREENSHOT_FIELDS = [("media", "recording", "playback_screenshots")]

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send recorded video — VL model watches it to evaluate
        whether audio description or media alternative is present."""
        for m in capture_data.media:
            rec = m.get("recording", {})
            v = rec.get("captions_on_video", "")
            if v:
                return v
        return None

    def is_applicable(self, capture_data: CaptureData) -> bool:
        # SC 1.2.2 / 1.2.3 / 1.2.5 apply to "synchronized media" — audio
        # combined with video. A silent muted video (e.g. a university's hero) is
        # video-only content; SC 1.2.1 covers it, these three do not.
        # _video_has_audio returns True for video embeds (we cannot probe
        # remote players' audio reliably) so YouTube/Vimeo iframes still
        # count.
        has_audible_video = any(
            _media_type(m) == "video"
            and not m.get("live", False)
            and _video_has_audio(m, capture_data)
            for m in capture_data.media
        )
        return (
            has_audible_video
            or bool(_detect_video_embeds(capture_data))
            or _page_has_media_iframe(capture_data)
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Build a lookup of transcript verification results
        transcript_verified_any = False
        for tv in getattr(capture_data, "transcript_verifications", []):
            if tv.get("transcript_found", False):
                transcript_verified_any = True
                break

        for m in capture_data.media:
            if _media_type(m) != "video" or m.get("live", False):
                continue

            selector = m.get("selector", "video element")
            has_descriptions = _has_track(m, "descriptions")
            has_text_alt = m.get("has_text_alternative", False)
            has_transcript = m.get("has_transcript_link", False)
            aria_label = m.get("ariaLabel", "") or ""
            duration = m.get("duration", None)

            if not has_descriptions and not has_text_alt and not has_transcript:
                detail_parts = [
                    "Prerecorded video has no audio description track "
                    "and no media alternative"
                ]
                if aria_label:
                    detail_parts.append(f"(aria-label: \"{aria_label}\")")
                if duration is not None:
                    try:
                        dur_s = float(duration)
                        if dur_s > 0:
                            mins, secs = divmod(int(dur_s), 60)
                            detail_parts.append(
                                f"[duration: {mins}m {secs}s]"
                            )
                    except (ValueError, TypeError):
                        pass
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=" ".join(detail_parts),
                    impact=(
                        "Blind users cannot access visual information that "
                        "is not conveyed through the existing audio."
                    ),
                    recommendation=(
                        "Add a <track kind=\"descriptions\"> with audio "
                        "descriptions, or provide a full text alternative "
                        "describing the visual content."
                    ),
                    severity=Severity.HIGH,
                ))
            elif has_descriptions:
                # Descriptions track exists -- check it has a valid src
                tracks = m.get("tracks", [])
                for track in tracks:
                    kind = (track.get("kind") or "").lower()
                    if kind == "descriptions":
                        src = track.get("src", "") or ""
                        if not src:
                            findings.append(Finding(
                                id=_make_finding_id(),
                                element=selector,
                                issue=(
                                    "Audio description track element exists "
                                    "but has no src attribute"
                                ),
                                impact=(
                                    "The descriptions track is present in "
                                    "the DOM but cannot load content."
                                ),
                                recommendation=(
                                    "Set the src attribute to a valid "
                                    "audio description file."
                                ),
                                severity=Severity.HIGH,
                            ))
            elif has_transcript and not transcript_verified_any:
                # A transcript link exists but verification didn't find content
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        "Video relies on a transcript link as its media "
                        "alternative, but transcript verification did not "
                        "confirm visible transcript content"
                    ),
                    impact=(
                        "If the transcript does not work, blind users have "
                        "no way to access the visual information."
                    ),
                    recommendation=(
                        "Ensure the transcript link leads to a full text "
                        "alternative describing all visual content, or add "
                        "a <track kind=\"descriptions\"> element."
                    ),
                    severity=Severity.MEDIUM,
                ))

        # -- Check transcript_buttons for audio-description controls --
        for btn in getattr(capture_data, "transcript_buttons", []):
            btn_label = btn.get("label", "") or btn.get("text", "") or ""
            btn_selector = btn.get("selector", "button")
            if any(
                kw in btn_label.lower()
                for kw in ("audio desc", "described", "description")
            ):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=btn_selector,
                    issue=(
                        f"Audio description toggle button found "
                        f"(\"{btn_label}\") -- verify it activates an "
                        f"audio description track"
                    ),
                    impact=(
                        "A description button suggests audio descriptions "
                        "may be available through the player UI."
                    ),
                    recommendation=(
                        "Manually verify that activating this button plays "
                        "an audio description of visual content."
                    ),
                    severity=Severity.INFO,
                ))

        # -- Check embedded video platforms for audio description --
        video_embeds = _detect_video_embeds(capture_data)
        for embed in video_embeds:
            findings.append(Finding(
                id=_make_finding_id(),
                element=embed["selector"],
                issue=(
                    f"Embedded {embed['platform']} video — audio description "
                    f"or media alternative cannot be verified programmatically"
                ),
                impact=(
                    "Blind users cannot access visual information that "
                    "is not conveyed through the existing audio."
                ),
                recommendation=(
                    f"Verify that this {embed['platform']} embed has audio "
                    f"descriptions for significant visual content, or provide "
                    f"a full text alternative on the page describing the "
                    f"visual information."
                ),
                severity=Severity.MEDIUM,
            ))

        total_media = len(capture_data.media) + len(video_embeds)
        conformance = self._determine_conformance(findings, total_media)
        # Higher confidence when transcript verifications were performed
        has_verifications = bool(getattr(capture_data, "transcript_verifications", []))
        confidence = 0.75 if has_verifications else 0.7
        return conformance, confidence, findings


class Check_1_2_4(BaseCheck):
    """SC 1.2.4 Captions (Live) (Level AA)."""

    criterion_id = "1.2.4"
    criterion_name = "Captions (Live)"
    level = "AA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.2 Time-based Media"
    principle = "1. Perceivable"
    ict_baseline = "17"
    tt_tests = ["17.E"]
    normative_text = (
        "Captions are provided for all live audio content in synchronized media."
    )

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send recorded media playback — VL model checks if live
        captioning mechanism is visible during playback."""
        for m in capture_data.media:
            rec = m.get("recording", {})
            v = rec.get("captions_on_video", "")
            if v:
                return v
        return None

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return (
            any(
                m.get("live", False) and _media_type(m) == "video"
                for m in capture_data.media
            )
            or bool(_detect_video_embeds(capture_data))
            or _page_has_media_iframe(capture_data)
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        for m in capture_data.media:
            if not m.get("live", False) or _media_type(m) != "video":
                continue

            selector = m.get("selector", "live video element")
            has_captions = _has_track(m, "captions") or _has_track(m, "subtitles")
            has_live_captions = m.get("has_live_captions", False)

            if not has_captions and not has_live_captions:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Live video has no caption support",
                    impact=(
                        "Deaf and hard-of-hearing users cannot access "
                        "live audio content."
                    ),
                    recommendation=(
                        "Provide real-time captions using CART services, "
                        "auto-captioning, or WebVTT live captions."
                    ),
                    severity=Severity.HIGH,
                ))

        conformance = self._determine_conformance(findings, len(capture_data.media))
        confidence = 0.65
        return conformance, confidence, findings


class Check_1_2_5(BaseCheck):
    """SC 1.2.5 Audio Description (Prerecorded) (Level AA)."""

    criterion_id = "1.2.5"
    criterion_name = "Audio Description (Prerecorded)"
    level = "AA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "1.2 Time-based Media"
    principle = "1. Perceivable"
    ict_baseline = "18"
    tt_tests = ["18.A"]
    normative_text = (
        "Audio description is provided for all prerecorded video content "
        "in synchronized media."
    )

    _SCREENSHOT_FIELDS = [("media", "recording", "playback_screenshots")]

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send recorded video — VL model evaluates audio description
        availability and controls in the player."""
        for m in capture_data.media:
            rec = m.get("recording", {})
            v = rec.get("captions_on_video", "")
            if v:
                return v
        return None

    def is_applicable(self, capture_data: CaptureData) -> bool:
        # SC 1.2.2 / 1.2.3 / 1.2.5 apply to "synchronized media" — audio
        # combined with video. A silent muted video (e.g. a university's hero) is
        # video-only content; SC 1.2.1 covers it, these three do not.
        # _video_has_audio returns True for video embeds (we cannot probe
        # remote players' audio reliably) so YouTube/Vimeo iframes still
        # count.
        has_audible_video = any(
            _media_type(m) == "video"
            and not m.get("live", False)
            and _video_has_audio(m, capture_data)
            for m in capture_data.media
        )
        return (
            has_audible_video
            or bool(_detect_video_embeds(capture_data))
            or _page_has_media_iframe(capture_data)
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        for m in capture_data.media:
            if _media_type(m) != "video" or m.get("live", False):
                continue

            selector = m.get("selector", "video element")
            has_descriptions = _has_track(m, "descriptions")
            aria_label = m.get("ariaLabel", "") or ""
            duration = m.get("duration", None)

            if not has_descriptions:
                detail_parts = [
                    "Prerecorded video has no audio description track"
                ]
                if aria_label:
                    detail_parts.append(f"(aria-label: \"{aria_label}\")")
                if duration is not None:
                    try:
                        dur_s = float(duration)
                        if dur_s > 0:
                            mins, secs = divmod(int(dur_s), 60)
                            detail_parts.append(
                                f"[duration: {mins}m {secs}s]"
                            )
                    except (ValueError, TypeError):
                        pass
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=" ".join(detail_parts),
                    impact=(
                        "Blind users cannot access visual-only information "
                        "presented in the video."
                    ),
                    recommendation=(
                        "Add a <track kind=\"descriptions\"> element with "
                        "audio descriptions of important visual content."
                    ),
                    severity=Severity.HIGH,
                ))
            else:
                # Description track exists -- validate src is set
                tracks = m.get("tracks", [])
                for track in tracks:
                    kind = (track.get("kind") or "").lower()
                    if kind == "descriptions":
                        src = track.get("src", "") or ""
                        if not src:
                            findings.append(Finding(
                                id=_make_finding_id(),
                                element=selector,
                                issue=(
                                    "Audio description track exists but "
                                    "has no src attribute"
                                ),
                                impact=(
                                    "The descriptions track cannot load "
                                    "content without a source URL."
                                ),
                                recommendation=(
                                    "Set the src attribute on the "
                                    "<track kind=\"descriptions\"> element."
                                ),
                                severity=Severity.HIGH,
                            ))

        # -- Check embedded video platforms for audio description --
        video_embeds = _detect_video_embeds(capture_data)
        for embed in video_embeds:
            findings.append(Finding(
                id=_make_finding_id(),
                element=embed["selector"],
                issue=(
                    f"Embedded {embed['platform']} video — audio description "
                    f"cannot be verified programmatically"
                ),
                impact=(
                    "Blind users cannot access visual-only information "
                    "presented in the video."
                ),
                recommendation=(
                    f"Verify that this {embed['platform']} embed provides "
                    f"audio descriptions of important visual content."
                ),
                severity=Severity.MEDIUM,
            ))

        total_media = len(capture_data.media) + len(video_embeds)
        conformance = self._determine_conformance(findings, total_media)
        confidence = 0.8
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_1_2_1(),
        Check_1_2_2(),
        Check_1_2_3(),
        Check_1_2_4(),
        Check_1_2_5(),
    ]
