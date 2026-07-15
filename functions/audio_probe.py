"""LLM corroboration for autoplay audio detection.

Used by SC 1.4.2 (Audio Control) and SC 2.2.2 (Pause, Stop, Hide). The
deterministic Playwright probe in ``capture/interactive_capture.py``
populates ``capture_data.audio_detection`` from DOM state -- that's the
fast, reliable signal that catches every <audio>/<video autoplay> and
common embedded-player iframe (YouTube, Vimeo, etc).

This module layers an OPTIONAL AI corroboration on top, only when the
two SC checks call it. The LLM is routed through ``AI_VIDEO_*`` so the
call lands on a model that can actually process the audio track
(Gemini Flash). The local mlx-vlm/Gemma E4B endpoint was empirically
verified to strip audio from uploaded videos -- it accepts video_url
parts and returns 200 but only sees frames -- so we never make this
call against a local-only stack; the helper short-circuits and returns
None when no cloud video model is configured.

The helper is intentionally narrow: only the two audio-related SCs
import it, which keeps every other criterion off this code path.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "You are an accessibility tester listening to a recording of a web "
    "page loading. Your single job is to report whether any audio plays "
    "automatically and how long it lasts. Listen carefully -- do not "
    "guess from visuals. If the recording is silent, say so."
)

_USER_PROMPT = (
    "Listen to the attached video. Answer:\n"
    "1. Does any audio play automatically when the page loads? (true/false)\n"
    "2. If yes, does the audio last longer than 3 seconds? (true/false)\n"
    "3. What kind of audio is it? (music, speech, video soundtrack, "
    "notification, jingle, silence)\n"
    "4. Does the page show a visible pause or mute control? (true/false)\n\n"
    "Respond with a JSON object exactly like:\n"
    '{"has_autoplay_audio": <bool>, "duration_over_3s": <bool>, '
    '"audio_type": "<string>", "has_pause_button": <bool>, '
    '"description": "<one sentence>"}'
)


async def corroborate_autoplay_audio(
    video_path: str | None,
    *,
    timeout_s: float = 90.0,
) -> dict[str, Any] | None:
    """Send the observation video to an audio-capable model for a second
    opinion on whether autoplay audio is present.

    Returns a dict with the same keys as the deterministic probe, or
    None when:
    - no video is available,
    - no cloud video model is configured (see _has_cloud_video_model),
    - the call fails or is cancelled (logged, not raised).

    Callers should treat None as "no AI signal" and rely on the
    deterministic probe alone.
    """
    if not video_path or not os.path.exists(video_path):
        return None

    if not _has_cloud_video_model():
        logger.info(
            "Audio corroboration skipped: no cloud AI_VIDEO_* configured. "
            "Local mlx-vlm strips audio so a fallback call would be wasted."
        )
        return None

    if os.path.getsize(video_path) > 50 * 1024 * 1024:
        logger.info("Audio corroboration skipped: video too large (>50MB)")
        return None

    from functions.llm import LLMClient
    from functions.parser import extract_json_from_text, get_content_text
    from config import AI_VIDEO_API_KEY, AI_VIDEO_API_URL, AI_VIDEO_MODEL

    llm = LLMClient(
        base_url=AI_VIDEO_API_URL,
        model=AI_VIDEO_MODEL,
        api_key=AI_VIDEO_API_KEY,
    )

    try:
        response = await asyncio.wait_for(
            llm.call(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=_USER_PROMPT,
                video=video_path,
                needs_audio=True,
                temperature=0.1,
                label="audio_probe",
            ),
            timeout=timeout_s,
        )
    except asyncio.CancelledError:
        logger.warning("Audio corroboration CANCELLED by parent task")
        raise
    except asyncio.TimeoutError:
        logger.warning("Audio corroboration timed out after %.0fs", timeout_s)
        return None
    except Exception as exc:
        logger.warning("Audio corroboration failed: %s", exc)
        return None

    content = get_content_text(response)
    if not content:
        return None

    try:
        parsed = extract_json_from_text(content)
    except Exception as exc:
        logger.warning("Audio corroboration: failed to parse JSON: %s", exc)
        return None

    if not isinstance(parsed, dict):
        return None

    parsed["_source"] = "ai_corroboration"
    logger.info("Audio corroboration: %s", parsed)
    return parsed


def merge_audio_signals(
    deterministic: dict[str, Any] | None,
    ai: dict[str, Any] | None,
) -> dict[str, Any]:
    """Combine the deterministic DOM probe with the optional AI signal.

    Rules:
    - If both agree on has_autoplay_audio=True, keep both fields and
      raise confidence (downstream uses it).
    - If only one says True, return that one with a flag noting the
      mismatch so the SC check can adjust severity.
    - If both say False, return False.
    - When the AI signal is None (not configured / failed), pass the
      deterministic signal through unchanged.

    The merged dict always carries ``_signals`` listing which sources
    contributed, so the SC check can attribute the finding correctly.
    """
    det = deterministic or {}
    ai_d = ai or {}

    det_yes = bool(det.get("has_autoplay_audio"))
    ai_yes = bool(ai_d.get("has_autoplay_audio"))

    sources = []
    if det:
        sources.append("dom_probe")
    if ai:
        sources.append("ai_corroboration")

    if not det and not ai:
        return {"has_autoplay_audio": False, "_signals": sources}

    # Agreement → pick the richer description, OR-merge the booleans
    if det_yes and ai_yes:
        return {
            "has_autoplay_audio": True,
            "duration_over_3s": (
                det.get("duration_over_3s") or ai_d.get("duration_over_3s")
            ),
            "audio_type": ai_d.get("audio_type") or det.get("audio_type") or "audio",
            "has_pause_button": (
                det.get("has_pause_button") or ai_d.get("has_pause_button")
            ),
            "description": "; ".join(
                s for s in [det.get("description"), ai_d.get("description")] if s
            ),
            "_signals": sources,
            "_agreement": "both",
        }

    # Single source positive
    if det_yes:
        return {**det, "_signals": sources, "_agreement": "dom_only"}
    if ai_yes:
        return {**ai_d, "_signals": sources, "_agreement": "ai_only"}

    return {"has_autoplay_audio": False, "_signals": sources}


def _has_cloud_video_model() -> bool:
    """Return True when AI_VIDEO_* points to a cloud model that can hear audio."""
    try:
        from config import AI_VIDEO_API_URL, AI_VIDEO_MODEL
    except ImportError:
        return False
    if not AI_VIDEO_API_URL or not AI_VIDEO_MODEL:
        return False
    url = AI_VIDEO_API_URL.lower()
    return any(host in url for host in (
        "googleapis.com", "openai.com", "anthropic.com",
        "openrouter.ai", "deepinfra.com",
    ))
