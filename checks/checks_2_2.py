"""WCAG Guideline 2.2 - Enough Time (A) checks."""
from __future__ import annotations

import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
)


class Check_2_2_1(BaseCheck):
    """SC 2.2.1 Timing Adjustable (Level A)."""

    criterion_id = "2.2.1"
    criterion_name = "Timing Adjustable"
    # Applicability (does the page impose a time limit?) is a meaning
    # judgment — a keyword scan of JS must not hard-gate it.
    ai_judged_applicability = True
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.2 Enough Time"
    principle = "2. Operable"
    ict_baseline = "21"
    tt_tests = ["21.C", "21.D"]
    normative_text = (
        "For each time limit that is set by the content, at least one of "
        "the following is true: Turn off, Adjust, Extend, Real-time "
        "Exception, Essential Exception, 20 Hour Exception."
    )
    web_only = True

    def is_applicable(self, capture_data: CaptureData) -> bool:
        html = capture_data.html or ""
        script = capture_data.script_content or ""
        dynamic = capture_data.dynamic_content or {}
        # Applicable if there's meta refresh, setTimeout, session timeout indicators,
        # or dynamic_content detected an auto-refresh
        return bool(
            "meta http-equiv" in html.lower()
            or "settimeout" in script.lower()
            or "setinterval" in script.lower()
            or "session" in script.lower()
            or dynamic.get("hasAutoRefresh", False)
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html = capture_data.html or ""
        script = capture_data.script_content or ""

        # -- Check dynamic_content for auto-refresh --
        dynamic = capture_data.dynamic_content or {}
        if dynamic.get("hasAutoRefresh", False):
            findings.append(Finding(
                id=_make_finding_id(),
                element="<meta http-equiv='refresh'>",
                issue=(
                    "Page contains a meta refresh tag detected at runtime "
                    "(dynamic content analysis)"
                ),
                impact=(
                    "The page auto-refreshes without user control, which "
                    "can disorient users and cause loss of focus position "
                    "for assistive technology users."
                ),
                recommendation=(
                    "Remove the auto-refresh or provide a mechanism to "
                    "turn off, adjust, or extend the time limit before "
                    "the refresh occurs."
                ),
                severity=Severity.HIGH,
            ))

        # Check for meta refresh with redirect
        meta_refresh = re.findall(
            r'<meta\s+http-equiv\s*=\s*["\']refresh["\']\s+'
            r'content\s*=\s*["\'](\d+)',
            html, re.IGNORECASE
        )
        for timeout in meta_refresh:
            timeout_val = int(timeout)
            if timeout_val > 0:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="<meta http-equiv='refresh'>",
                    issue=f"Meta refresh redirects after {timeout_val} seconds",
                    impact=(
                        "Users who read slowly or use assistive technology "
                        "may not finish reading the page before being redirected."
                    ),
                    recommendation=(
                        "Remove the meta refresh or provide a mechanism to "
                        "turn off, adjust, or extend the time limit."
                    ),
                    severity=Severity.HIGH,
                ))

        # Check for setTimeout/setInterval in scripts
        timeout_patterns = [
            (r"setTimeout\s*\(\s*[^,]+,\s*(\d+)\s*\)", "setTimeout"),
            (r"setInterval\s*\(\s*[^,]+,\s*(\d+)\s*\)", "setInterval"),
        ]

        for pattern, fn_name in timeout_patterns:
            matches = re.findall(pattern, script)
            for ms_str in matches:
                ms = int(ms_str)
                # Only flag significant timeouts (>= 5 seconds)
                if ms >= 5000:
                    # Check if there's a mechanism to extend/disable
                    has_extend = bool(
                        re.search(r"extend|reset|renew|keepalive", script, re.IGNORECASE)
                    )
                    if not has_extend:
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element="<script>",
                            issue=(
                                f"{fn_name} with {ms}ms ({ms/1000:.0f}s) delay "
                                f"may impose a time limit"
                            ),
                            impact="Users may lose content or progress if a time limit expires.",
                            recommendation=(
                                "Ensure users can turn off, adjust, or extend "
                                "the time limit before it expires."
                            ),
                            severity=Severity.MEDIUM,
                        ))

        # Check for session timeout indicators — keyword detection only.
        # Many third-party scripts (analytics, consent, ads) contain these
        # keywords without implementing user-facing timeouts, so this is
        # flagged as INFO severity for AI review, not a hard finding.
        session_patterns = [
            r"session\s*(?:timeout|expire|expir)",
            r"idle\s*(?:timeout|timer)",
            r"auto\s*(?:logout|signout|log.?out)",
        ]
        for pattern in session_patterns:
            if re.search(pattern, script, re.IGNORECASE):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="<script>",
                    issue=(
                        "Session timeout/auto-logout keywords detected in "
                        "JavaScript — cannot verify from static testing whether "
                        "this implements an actual user-facing timeout"
                    ),
                    impact=(
                        "If a real session timeout exists, users who need more "
                        "time may lose their session and unsaved data."
                    ),
                    recommendation=(
                        "Verify whether a session timeout fires during use. "
                        "If it does, ensure users are warned at least 20 seconds "
                        "before timeout and can extend the session."
                    ),
                    severity=Severity.INFO,
                ))
                break

        conformance = self._determine_conformance(findings)
        confidence = 0.6
        return conformance, confidence, findings


class Check_2_2_2(BaseCheck):
    """SC 2.2.2 Pause, Stop, Hide (Level A)."""

    criterion_id = "2.2.2"
    criterion_name = "Pause, Stop, Hide"
    # Applicability (is there moving / auto-updating content?) is a
    # meaning judgment — a keyword scan must not hard-gate it.
    ai_judged_applicability = True
    # A finding asserting the page has animation / auto-refresh / marquee
    # / autoplay is checked against the deterministic dynamic-content
    # probe (a finding claiming animation when the probe measured none
    # is demoted to judge_inference).
    measurement_sources = {
        "hasAnimations": ("dynamic_content", "hasAnimations"),
        "hasAutoRefresh": ("dynamic_content", "hasAutoRefresh"),
        "hasMarquee": ("dynamic_content", "hasMarquee"),
        "hasAutoplayVideo": ("dynamic_content", "hasAutoplayVideo"),
        "hasAutoplayAudio": ("dynamic_content", "hasAutoplayAudio"),
    }
    level = "A"
    needs_audio = True  # Must HEAR auto-playing audio/video
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.2 Enough Time"
    principle = "2. Operable"
    ict_baseline = "21"
    tt_tests = ["21.E", "21.F"]
    normative_text = (
        "For moving, blinking, scrolling, or auto-updating information, "
        "all of the following are true: Moving/blinking/scrolling content "
        "that starts automatically, lasts more than 5 seconds, and is "
        "presented in parallel with other content, can be paused, stopped, "
        "or hidden. Auto-updating content that starts automatically and is "
        "presented in parallel with other content, can be paused, stopped, "
        "or hidden or the frequency of the update can be controlled."
    )
    web_only = True

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send the observation video so the VL model can evaluate motion."""
        return capture_data.observation_video_path or None

    def _has_video_embeds(self, capture_data: CaptureData) -> list[dict]:
        """Detect video embeds in iframes (YouTube, Vimeo, etc.)."""
        from checks.checks_1_2 import _detect_video_embeds
        return _detect_video_embeds(capture_data)

    def is_applicable(self, capture_data: CaptureData) -> bool:
        html = capture_data.html or ""
        html_lower = html.lower()
        dynamic = capture_data.dynamic_content or {}
        return bool(
            capture_data.media
            or "marquee" in html_lower
            or "blink" in html_lower
            or "animation" in html_lower
            or "carousel" in html_lower
            or "slider" in html_lower
            or "@keyframes" in html_lower
            or dynamic.get("hasAutoplayVideo", False)
            or dynamic.get("hasAutoplayAudio", False)
            or dynamic.get("hasAnimations", False)
            or dynamic.get("hasMarquee", False)
            or self._has_video_embeds(capture_data)
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html = capture_data.html or ""
        html_lower = html.lower()

        # -- Check dynamic_content for auto-playing / animated content --
        dynamic = capture_data.dynamic_content or {}

        if dynamic.get("hasAutoplayVideo", False):
            # Check whether any media element has controls
            has_pause = any(
                m.get("controls", False) for m in capture_data.media
                if (m.get("tag") or m.get("tagName") or "").lower() == "video"
                and m.get("autoplay", False)
            )
            if not has_pause:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="video[autoplay]",
                    issue=(
                        "Auto-playing video detected at runtime without a "
                        "visible pause/stop mechanism"
                    ),
                    impact=(
                        "Moving video content that starts automatically can "
                        "distract users and is inaccessible to those who "
                        "cannot perceive rapid visual changes."
                    ),
                    recommendation=(
                        "Provide a clearly visible pause or stop button, or "
                        "add the controls attribute to the video element."
                    ),
                    severity=Severity.HIGH,
                ))

        if dynamic.get("hasAutoplayAudio", False):
            has_pause = any(
                m.get("controls", False) for m in capture_data.media
                if (m.get("tag") or m.get("tagName") or "").lower() == "audio"
                and m.get("autoplay", False)
            )
            if not has_pause:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="audio[autoplay]",
                    issue=(
                        "Auto-playing audio detected at runtime without a "
                        "visible pause/stop mechanism"
                    ),
                    impact=(
                        "Auto-playing audio can interfere with screen readers "
                        "and disorient users. Users must be able to stop it."
                    ),
                    recommendation=(
                        "Provide a pause/stop control for the audio, or add "
                        "the controls attribute to the audio element."
                    ),
                    severity=Severity.HIGH,
                ))

        if dynamic.get("hasAnimations", False):
            has_reduced_motion = "prefers-reduced-motion" in html_lower
            if not has_reduced_motion:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="page",
                    issue=(
                        "Active CSS/JS animations detected at runtime without "
                        "a prefers-reduced-motion media query"
                    ),
                    impact=(
                        "Animated content that starts automatically and lasts "
                        "more than 5 seconds must have a pause mechanism. "
                        "Users with vestibular disorders may experience "
                        "discomfort."
                    ),
                    recommendation=(
                        "Add a pause/stop control for animations, or honour "
                        "@media (prefers-reduced-motion: reduce) to disable "
                        "them when the user has requested reduced motion."
                    ),
                    severity=Severity.MEDIUM,
                ))

        if dynamic.get("hasMarquee", False):
            # The static HTML <marquee> check below may also fire; this
            # catches carousel/slider/rotate classes detected at runtime
            if "<marquee" not in html_lower:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="page",
                    issue=(
                        "Moving/scrolling content detected at runtime "
                        "(carousel, slider, or rotating element)"
                    ),
                    impact=(
                        "Auto-moving content can distract users and may be "
                        "inaccessible to those using assistive technology."
                    ),
                    recommendation=(
                        "Provide a visible pause or stop mechanism for "
                        "carousels, sliders, and other moving content."
                    ),
                    severity=Severity.MEDIUM,
                ))

        # Check for <marquee> elements
        if "<marquee" in html_lower:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<marquee>",
                issue="Page uses deprecated <marquee> element (moving text)",
                impact=(
                    "Users with attention or reading disabilities may have "
                    "difficulty reading moving text. Some users may experience "
                    "distraction or discomfort."
                ),
                recommendation=(
                    "Remove the <marquee> element. If the content must scroll, "
                    "provide a pause mechanism."
                ),
                severity=Severity.HIGH,
            ))

        # Check for <blink> elements
        if "<blink" in html_lower:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<blink>",
                issue="Page uses deprecated <blink> element",
                impact="Blinking content can be distracting and may cause seizures.",
                recommendation="Remove the <blink> element entirely.",
                severity=Severity.HIGH,
            ))

        # Check for CSS blink animation
        if "text-decoration" in html_lower and "blink" in html_lower:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<style>",
                issue="CSS text-decoration: blink detected",
                impact="Blinking text is distracting and may cause seizures.",
                recommendation="Remove text-decoration: blink.",
                severity=Severity.HIGH,
            ))

        # Check for auto-playing media without controls
        for m in capture_data.media:
            selector = m.get("selector", "media element")
            autoplay = m.get("autoplay", False)
            has_controls = m.get("controls", False)
            loop = m.get("loop", False)

            if autoplay and not has_controls:
                severity = Severity.HIGH if loop else Severity.MEDIUM
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"Auto-playing media without pause/stop controls"
                        + (" (loops)" if loop else "")
                    ),
                    impact="Users cannot pause or stop the moving content.",
                    recommendation="Add controls attribute to the media element.",
                    severity=severity,
                ))

        # Check for CSS animations without prefers-reduced-motion
        keyframes = re.findall(r"@keyframes\s+(\w+)", html, re.IGNORECASE)
        has_reduced_motion = "prefers-reduced-motion" in html_lower
        if keyframes and not has_reduced_motion:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<style>",
                issue=(
                    f"CSS animations detected ({', '.join(keyframes)}) "
                    f"without prefers-reduced-motion media query"
                ),
                impact="Users sensitive to motion cannot disable animations.",
                recommendation=(
                    "Add @media (prefers-reduced-motion: reduce) to disable "
                    "or minimize animations when the user has requested it."
                ),
                severity=Severity.MEDIUM,
            ))

        # Check for auto-updating content (AJAX polling, live regions)
        script = capture_data.script_content or ""
        auto_update_patterns = [
            r"setInterval\s*\(\s*(?:function|[^,]+fetch|[^,]+ajax|[^,]+XMLHttp)",
            r"auto[_-]?refresh",
            r"live[_-]?update",
        ]
        for pattern in auto_update_patterns:
            if re.search(pattern, script, re.IGNORECASE):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="<script>",
                    issue="Auto-updating content mechanism detected",
                    impact=(
                        "Users may be disoriented by content that changes "
                        "without their action."
                    ),
                    recommendation=(
                        "Provide a mechanism to pause, stop, or control the "
                        "frequency of auto-updates."
                    ),
                    severity=Severity.MEDIUM,
                ))
                break

        # Check for embedded video platforms (YouTube, Vimeo, etc.)
        # These auto-play by default and the page has no control over them
        video_embeds = self._has_video_embeds(capture_data)
        for embed in video_embeds:
            src = embed.get("src", "")
            # YouTube embeds autoplay if the URL contains autoplay=1,
            # but many also autoplay via JS. Flag if no pause mechanism
            # is provided ON THE PAGE (the embed's own controls don't count
            # because they require entering the iframe).
            autoplay_param = "autoplay=1" in src.lower()
            findings.append(Finding(
                id=_make_finding_id(),
                element=embed["selector"],
                issue=(
                    f"Embedded {embed['platform']} video"
                    + (" with autoplay=1" if autoplay_param else "")
                    + " — page does not provide a pause/stop mechanism "
                    "outside the iframe"
                ),
                impact=(
                    "Auto-playing or user-initiated video in an iframe "
                    "cannot be paused from the main page. Users must "
                    "enter the iframe to access controls."
                ),
                recommendation=(
                    f"Provide a pause/stop button on the page that controls "
                    f"the {embed['platform']} embed, or ensure the embed "
                    f"does not auto-play."
                ),
                severity=Severity.MEDIUM if not autoplay_param else Severity.HIGH,
            ))

        # Audio detection: deterministic DOM probe (always) + optional AI
        # corroboration via cloud video model -- same helper SC 1.4.2 uses.
        # Only these two SCs import audio_probe.
        from functions.audio_probe import corroborate_autoplay_audio, merge_audio_signals
        deterministic = getattr(capture_data, "audio_detection", {}) or {}
        video_path = getattr(capture_data, "observation_video_path", None)
        ai_signal = await corroborate_autoplay_audio(video_path)
        audio = merge_audio_signals(deterministic, ai_signal)
        if audio.get("has_autoplay_audio") and audio.get("duration_over_3s"):
            audio_type = audio.get("audio_type", "audio")
            has_pause = audio.get("has_pause_button", False)
            if not has_pause:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="page (detected via audio analysis)",
                    issue=(
                        f"Auto-playing {audio_type} detected on page load, "
                        f"lasting longer than 5 seconds, with no mechanism to "
                        f"pause, stop, or hide the content"
                    ),
                    impact=(
                        "Moving or auto-updating content that starts "
                        "automatically can distract users with cognitive "
                        "disabilities and interfere with assistive technologies."
                    ),
                    recommendation=(
                        "Provide a visible pause or stop mechanism for "
                        "auto-playing audio/video content."
                    ),
                    severity=Severity.HIGH,
                    evidence=f"Audio analysis: type={audio_type}, has_pause={has_pause}",
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.65
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_2_2_1(),
        Check_2_2_2(),
    ]
