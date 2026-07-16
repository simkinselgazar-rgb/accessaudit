"""Video-to-text pre-processing — describe videos once, reuse as text everywhere.

Instead of sending raw video to every SC check (which blows up context windows
and fails on models that can't do tool calls + video simultaneously), this
module sends each video to the vision model ONCE with SC-targeted questions,
captures the prose description, and stores it on CaptureData.video_descriptions.

Checks then receive the text description instead of the raw video, which:
- Fits in any context window
- Works with any model (no multimodal requirement for the assessment call)
- Can be reused across multiple SCs (keyboard walkthrough feeds 2.1.1, 2.1.2, 2.4.3, etc.)
- Lets the vision model focus purely on describing what it sees
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from models import CaptureData

logger = logging.getLogger(__name__)

# Maximum video duration sent to the AI in a single call. Longer videos are
# split into chunks of this length before description; the chunk descriptions
# are then concatenated with time-range headers so the AI's continuity is
# preserved without ever truncating the recording.
CHUNK_SECONDS = 60

# ── Per-video-type question sets ─────────────────────────────────────────────
# Each video type gets specific questions so the model knows what to look for.
# The description is then tagged and stored for the relevant SCs.

VIDEO_QUESTIONS: dict[str, dict[str, Any]] = {
    "keyboard_walkthrough": {
        "description": "Keyboard navigation walkthrough of the website",
        "questions": (
            "Watch this keyboard walkthrough video carefully and describe:\n\n"
            "1. FOCUS INDICATORS: For each element that receives focus, is there "
            "a visible outline, highlight, or other indicator? Describe its "
            "appearance (color, thickness, style). Are any elements focused "
            "WITHOUT a visible indicator?\n\n"
            "2. TAB ORDER: Does focus move in a logical reading order "
            "(left-to-right, top-to-bottom)? Are there any jumps where focus "
            "skips to an unexpected location?\n\n"
            "3. KEYBOARD TRAPS: Does focus ever get stuck on one element or "
            "cycle between the same few elements repeatedly?\n\n"
            "4. DROPDOWNS AND MENUS: When menus/dropdowns are opened via "
            "keyboard, do they close when Escape is pressed? Does focus "
            "return to the trigger element after closing?\n\n"
            "5. INTERACTIVE ELEMENTS: Are there any visible buttons, links, "
            "or controls that NEVER receive keyboard focus during the "
            "walkthrough?\n\n"
            "6. MODALS/DIALOGS: If any modal opens, is focus trapped inside "
            "it? Does it close with Escape?\n\n"
            "Be specific — name the actual elements you see (e.g., 'the "
            "Search button in the top navigation', 'the logo link')."
        ),
        # SCs that consume this description
        "serves_criteria": [
            "2.1.1", "2.1.2", "2.1.4", "2.4.3", "2.4.7", "2.4.11",
            "3.2.1", "3.2.2",
        ],
    },
    "observation": {
        "description": "Page observation recording (watching for dynamic content)",
        "questions": (
            "Watch this recording of a web page and describe:\n\n"
            "1. AUTO-PLAYING MEDIA: Does any video or audio start playing "
            "automatically? If so, is there a visible pause/stop button?\n\n"
            "2. MOVING/BLINKING CONTENT: Is there any content that moves, "
            "scrolls, blinks, or auto-updates? Carousels, tickers, "
            "animations, progress bars? Do they have pause controls?\n\n"
            "3. FLASHING: Is there any content that flashes or rapidly "
            "changes brightness? How frequently?\n\n"
            "4. TIMING: Are there any countdown timers, session warnings, "
            "or auto-refreshing content visible?\n\n"
            "5. CONTENT CHANGES: Does any content appear, disappear, or "
            "change without user interaction?\n\n"
            "Be specific about what you see and where on the page it appears."
        ),
        "serves_criteria": [
            "1.4.2", "2.2.1", "2.2.2", "2.3.1", "2.3.2", "2.3.3",
        ],
    },
    "FORM_INTERACTION": {
        "description": "Form interaction recording",
        "questions": (
            "Watch this form interaction and describe:\n\n"
            "1. ERROR MESSAGES: When fields are submitted incorrectly, "
            "are error messages shown? Are they adjacent to the field?\n\n"
            "2. LABELS: Does each field have a visible label? Are any "
            "fields using placeholder text as the only label?\n\n"
            "3. REQUIRED FIELDS: Are required fields indicated before "
            "submission (asterisk, 'required' text)?\n\n"
            "4. INPUT PURPOSE: Do fields for name, email, phone, address "
            "have appropriate autocomplete suggestions?\n\n"
            "Be specific about which fields you see and their behavior."
        ),
        "serves_criteria": [
            "1.3.5", "3.3.1", "3.3.2", "3.3.3", "3.3.4", "3.3.7", "3.3.8",
        ],
    },
    "MENU_NAVIGATION": {
        "description": "Menu/navigation keyboard interaction recording",
        "questions": (
            "Watch this menu navigation and describe:\n\n"
            "1. Does the menu open when activated by keyboard?\n"
            "2. Can arrow keys navigate between menu items?\n"
            "3. Does Escape close the menu?\n"
            "4. Does focus return to the trigger after closing?\n"
            "5. Are all menu items reachable by keyboard?\n"
            "6. Is the focus indicator visible on each menu item?"
        ),
        "serves_criteria": ["2.1.1", "2.4.3", "2.4.7", "2.4.11"],
    },
    "MODAL_INTERACTION": {
        "description": "Modal/dialog interaction recording",
        "questions": (
            "Watch this modal interaction and describe:\n\n"
            "1. Is focus moved into the modal when it opens?\n"
            "2. Is focus trapped inside the modal (can't Tab out)?\n"
            "3. Does Escape close the modal?\n"
            "4. Does focus return to the trigger element after closing?\n"
            "5. Are all controls inside the modal keyboard accessible?"
        ),
        "serves_criteria": ["2.1.2", "2.4.3", "2.4.7"],
    },
    "MEDIA_PLAYBACK": {
        "description": "Media playback recording",
        "questions": (
            "Watch this media playback and describe:\n\n"
            "1. CAPTIONS: Are captions/subtitles displayed? Are they "
            "accurate and synchronized with the audio?\n"
            "2. AUDIO DESCRIPTION: Is there an audio description track "
            "describing visual content for blind users?\n"
            "3. CONTROLS: Are play/pause/volume controls visible and "
            "keyboard accessible?\n"
            "4. TRANSCRIPT: Is there a link to a text transcript?\n"
            "5. AUTO-PLAY: Does the media start automatically?"
        ),
        "serves_criteria": [
            "1.2.1", "1.2.2", "1.2.3", "1.2.4", "1.2.5",
        ],
    },
}


async def describe_all_videos(
    capture_data: CaptureData,
    ai_client: Any,
) -> None:
    """Pre-process all captured videos into text descriptions.

    Runs once after capture completes. For each video type that has a
    recorded file, sends it to the vision model with targeted questions
    and stores the text description in capture_data.video_descriptions.

    This is called from app.py between capture and testing phases.
    """
    descriptions: dict[str, str] = {}
    start = time.time()

    # Each video to describe carries optional ground-truth context that
    # will be passed to the AI alongside the questions. For the keyboard
    # walkthrough we attach the deterministic tab-walk record so the AI
    # can cross-check its visual observations against the actual focus
    # sequence and explicitly call out skipped or misordered elements.
    videos_to_describe: list[tuple[str, str, str, str]] = []

    if capture_data.keyboard_walkthrough_video:
        videos_to_describe.append((
            "keyboard_walkthrough",
            capture_data.keyboard_walkthrough_video,
            VIDEO_QUESTIONS["keyboard_walkthrough"]["questions"],
            _format_tab_walk_context(capture_data),
        ))

    if capture_data.observation_video_path:
        videos_to_describe.append((
            "observation",
            capture_data.observation_video_path,
            VIDEO_QUESTIONS["observation"]["questions"],
            "",
        ))

    for seg in getattr(capture_data, "video_segments", []) or []:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type", "")
        video_path = seg.get("video_path", "")
        if not video_path or not seg.get("completed"):
            continue
        questions_config = VIDEO_QUESTIONS.get(seg_type)
        if questions_config:
            key = f"segment_{seg_type}_{os.path.basename(video_path)}"
            seg_context = (
                _format_tab_walk_context(capture_data)
                if seg_type == "TAB_WALKTHROUGH"
                else ""
            )
            videos_to_describe.append((
                key, video_path, questions_config["questions"], seg_context,
            ))

    if not videos_to_describe:
        logger.info("Video describer: no videos to process")
        return

    logger.info(
        "Video describer: processing %d video(s)...", len(videos_to_describe),
    )

    from functions.bypass_log import (
        CATEGORY_SKIPPED_DATA, SEVERITY_HIGH, SEVERITY_MEDIUM, log_bypass,
    )

    for video_key, video_path, questions, ground_truth in videos_to_describe:
        if not os.path.exists(video_path):
            logger.warning("Video describer: file not found: %s", video_path)
            log_bypass(
                category=CATEGORY_SKIPPED_DATA,
                severity=SEVERITY_HIGH,
                source="capture/video_describer.py:describe_all_videos",
                event="video_file_missing",
                details={
                    "video_key": video_key,
                    "video_path": video_path,
                },
                outcome="video skipped; SCs that consume this video's description will miss evidence",
                data_lost=True,
            )
            continue

        try:
            description = await _describe_single_video(
                ai_client, video_path, questions, ground_truth,
            )
            if description:
                descriptions[video_key] = description
                logger.info(
                    "Video describer: '%s' -> %d chars",
                    video_key, len(description),
                )
            else:
                log_bypass(
                    category=CATEGORY_SKIPPED_DATA,
                    severity=SEVERITY_MEDIUM,
                    source="capture/video_describer.py:describe_all_videos",
                    event="video_description_empty",
                    details={
                        "video_key": video_key,
                        "video_path": video_path,
                    },
                    outcome="model returned empty description; SCs consuming this video have no prose",
                    data_lost=True,
                )
        except Exception as exc:
            logger.warning(
                "Video describer: failed for '%s': %s", video_key, exc,
            )
            log_bypass(
                category=CATEGORY_SKIPPED_DATA,
                severity=SEVERITY_HIGH,
                source="capture/video_describer.py:describe_all_videos",
                event="video_description_exception",
                details={
                    "video_key": video_key,
                    "video_path": video_path,
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                },
                outcome="video description raised; continuing to next video; SCs that consume this video miss evidence",
                data_lost=True,
            )

    capture_data.video_descriptions = descriptions
    elapsed = time.time() - start

    # Save to disk for debugging
    if capture_data.captures_dir:
        try:
            desc_path = os.path.join(
                capture_data.captures_dir, "video_descriptions.json",
            )
            with open(desc_path, "w", encoding="utf-8") as f:
                json.dump(descriptions, f, indent=2)
        except Exception as exc:
            log_bypass(
                category=CATEGORY_SKIPPED_DATA,
                severity=SEVERITY_MEDIUM,
                source="capture/video_describer.py:describe_all_videos",
                event="video_descriptions_save_failed",
                details={
                    "target_path": desc_path,
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                },
                outcome="video_descriptions.json not persisted; in-memory dict still available",
                data_lost=False,
            )

    logger.info(
        "Video describer: completed %d/%d videos in %.1fs",
        len(descriptions), len(videos_to_describe), elapsed,
    )


_VIDEO_SYSTEM_PROMPT = (
    "You are an expert accessibility auditor reviewing a video recording of a "
    "website. Describe EXACTLY what you observe. Be specific about element "
    "names, positions, colors, and behaviors. Your description will be used "
    "by another system to evaluate WCAG conformance, so accuracy is critical."
)


def _ffprobe_duration(video_path: str) -> float:
    """Return video duration in seconds via ffprobe. 0.0 if ffprobe is unavailable."""
    if not shutil.which("ffprobe"):
        return 0.0
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True, text=True, timeout=15,
        )
        return float((proc.stdout or "0").strip() or 0)
    except Exception as exc:
        logger.debug("ffprobe failed for %s: %s", video_path, exc)
        return 0.0


def _split_video_to_chunks(video_path: str, chunk_seconds: int) -> list[tuple[str, float, float]]:
    """Split a video into chunks of at most ``chunk_seconds`` long.

    Returns a list of ``(chunk_path, start_seconds, end_seconds)``. The
    returned chunk files live in a per-call tempdir; the caller is
    responsible for cleaning them up via ``_cleanup_chunks``.

    Falls back to ``[(video_path, 0, duration)]`` if ffmpeg is missing
    or the split fails -- callers must always handle the original-video case.
    """
    duration = _ffprobe_duration(video_path)
    if duration <= chunk_seconds + 0.5 or not shutil.which("ffmpeg"):
        return [(video_path, 0.0, duration or 0.0)]

    tmpdir = tempfile.mkdtemp(prefix="vid_chunks_")
    suffix = Path(video_path).suffix or ".webm"
    chunks: list[tuple[str, float, float]] = []
    idx = 0
    start = 0.0
    while start < duration - 0.1:
        end = min(start + chunk_seconds, duration)
        out_path = os.path.join(tmpdir, f"chunk_{idx:03d}{suffix}")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-ss", f"{start:.2f}",
                    "-i", video_path,
                    "-t", f"{end - start:.2f}",
                    "-c", "copy",
                    out_path,
                ],
                capture_output=True, timeout=120, check=True,
            )
        except Exception as exc:
            logger.warning(
                "ffmpeg split failed at %.1fs for %s: %s -- falling back to original video",
                start, video_path, exc,
            )
            _cleanup_chunks(chunks, tmpdir)
            return [(video_path, 0.0, duration)]

        chunks.append((out_path, start, end))
        start = end
        idx += 1

    return chunks


def _cleanup_chunks(chunks: list[tuple[str, float, float]], tmpdir: str | None = None) -> None:
    """Remove temporary chunk files. Safe to call on the no-split fallback."""
    for path, _, _ in chunks:
        # Don't delete the original file
        if "vid_chunks_" not in path:
            continue
        try:
            os.remove(path)
        except OSError:
            pass
    if tmpdir and os.path.isdir(tmpdir) and "vid_chunks_" in tmpdir:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except OSError:
            pass


def _format_timestamp(seconds: float) -> str:
    """Render a duration as ``M:SS``."""
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


async def _describe_one_clip(
    video_path: str,
    user_prompt: str,
) -> str:
    """Send a single video file to the configured video model and return prose.

    Routing priority (ALL local first, cloud as last-resort):
      1. Gemma 4 E4B at ``AI_EXPLORER_*`` (port 11804) -- handles both
         video frames AND audio locally. Verified on 2026-04-22 that
         the mlx-vlm server processes audio tracks.
      2. Cloud ``AI_VIDEO_*`` if configured (Gemini Flash etc.).
      3. ``AI_VISION_*`` as final fallback (vision-only, ignores audio).
    """
    from config import (
        AI_API_BASE_URL, AI_API_KEY, AI_EXPLORER_MODEL, AI_EXPLORER_URL,
        AI_VIDEO_API_KEY, AI_VIDEO_API_URL, AI_VIDEO_MODEL,
        AI_VISION_API_KEY, AI_VISION_API_URL, AI_VISION_MODEL,
    )
    from functions.llm import LLMClient
    from functions.parser import get_content_text

    # Prefer a DISTINCT local explorer endpoint (multimodal E4B that handles
    # the audio track) -- but only when it is NOT the plain text endpoint. A
    # text-only explorer (e.g. deepseek on OpenRouter) cannot process video
    # and would 401/error (mirrors functions/llm.py:_select_model; verified
    # the audio_probe variant of this bug on a university run, 2026-05-28). Otherwise use
    # the dedicated cloud video model, then vision as a last resort.
    if (AI_EXPLORER_URL and AI_EXPLORER_MODEL
            and AI_EXPLORER_URL.rstrip("/") != (AI_API_BASE_URL or "").rstrip("/")):
        vid_url, vid_model, vid_key = AI_EXPLORER_URL, AI_EXPLORER_MODEL, AI_API_KEY
    elif AI_VIDEO_API_URL and AI_VIDEO_MODEL:
        vid_url = AI_VIDEO_API_URL
        vid_model = AI_VIDEO_MODEL
        vid_key = AI_VIDEO_API_KEY or AI_API_KEY
    else:
        vid_url = AI_VISION_API_URL
        vid_model = AI_VISION_MODEL
        vid_key = AI_VISION_API_KEY or AI_API_KEY

    vision_llm = LLMClient(
        base_url=vid_url,
        model=vid_model,
        api_key=vid_key,
    )

    raw = await vision_llm.call(
        system_prompt=_VIDEO_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        video=video_path,
        tools=None,
        tool_choice=None,
        temperature=0.3,
        label="video_description",
    )
    return get_content_text(raw).strip()


def _format_tab_walk_context(capture_data: CaptureData) -> str:
    """Render the deterministic tab walk as ground-truth context for the AI.

    The video model uses this to cross-reference what it sees against the
    actual focus sequence the browser recorded. Lets it explicitly call out
    elements that should have been focused but weren't, and keeps the chunk
    descriptions anchored even when the AI loses visual continuity.
    """
    tab_walk = getattr(capture_data, "tab_walk", None) or []
    if not tab_walk:
        return ""

    lines = ["GROUND TRUTH TAB ORDER (recorded deterministically by the browser):"]
    lines.append(
        f"The full walkthrough pressed Tab {len(tab_walk)} times. The focused "
        "element after each Tab press is listed below. Use this to verify "
        "what you see in the video and to flag any mismatches."
    )
    for i, tw in enumerate(tab_walk, 1):
        tag = tw.get("tag", "?")
        text = (tw.get("text") or "").strip()
        sel = tw.get("selector") or "?"
        visible_indicator = tw.get("has_visible_indicator")
        indicator_type = tw.get("indicator_type", "?")
        flag = "VISIBLE" if visible_indicator else "NO VISIBLE FOCUS INDICATOR"
        text_str = f' "{text}"' if text else ""
        lines.append(
            f"  Tab {i}: <{tag}>{text_str} [{flag} / {indicator_type}] -- {sel}"
        )

    keyboard_traps = getattr(capture_data, "keyboard_traps", None) or []
    if keyboard_traps:
        lines.append("")
        lines.append(f"DETECTED KEYBOARD TRAPS ({len(keyboard_traps)}):")
        for trap in keyboard_traps:
            lines.append(
                f"  - {trap.get('type', '?')} at {trap.get('selector', '?')}"
            )

    return "\n".join(lines)




async def _describe_single_video(
    ai_client: Any,
    video_path: str,
    questions: str,
    ground_truth: str = "",
) -> str:
    """Describe a video, chunking into <=60s pieces with rolling context.

    Pipeline:
    1. ffprobe the duration. If <= CHUNK_SECONDS, send as a single clip.
    2. Otherwise split with ffmpeg -c copy into 60s chunks (lossless, fast).
    3. Describe chunks SEQUENTIALLY. Each chunk receives:
       - The original questions (so the AI knows what to look for)
       - Optional ground-truth context (e.g. deterministic tab order for
         the keyboard walkthrough -- lets the AI cross-check what it sees)
       - A continuity hand-off built from the previous chunks' descriptions,
         so the AI knows where the previous chunk ended and can pick up
         the narrative without restarting
       - The chunk's exact time range and position (N of M)
    4. The chunk descriptions are concatenated with [CHUNK N/M -- M:SS-M:SS]
       headers so downstream consumers read it as one continuous prose blob
       with temporal anchors.

    Nothing is ever truncated -- the full video is always described, even
    if that produces a multi-page prose output.
    """
    if not os.path.exists(video_path):
        return ""

    chunks = _split_video_to_chunks(video_path, CHUNK_SECONDS)
    is_chunked = len(chunks) > 1
    tmpdir = os.path.dirname(chunks[0][0]) if is_chunked else None

    if is_chunked:
        logger.info(
            "Video describer: %s split into %d chunks of <=%ds",
            os.path.basename(video_path), len(chunks), CHUNK_SECONDS,
        )

    pieces: list[str] = []
    rolling_context: list[str] = []  # prior chunk descriptions in order

    try:
        for i, (clip_path, start, end) in enumerate(chunks):
            range_label = f"{_format_timestamp(start)}-{_format_timestamp(end)}"
            prompt_parts: list[str] = []

            if is_chunked:
                prompt_parts.append(
                    f"You are describing CHUNK {i + 1} of {len(chunks)} from "
                    f"a longer browser recording. This clip covers the time "
                    f"range {range_label} of the full video. Describe ONLY "
                    f"what happens in this clip -- the other chunks are "
                    f"described separately. Use the context below to keep "
                    f"the narrative consistent across chunks."
                )

            if ground_truth:
                prompt_parts.append(ground_truth)

            if rolling_context:
                ctx_blocks = []
                for j, prev in enumerate(rolling_context):
                    ctx_blocks.append(
                        f"[Previously, in chunk {j + 1}/{len(chunks)}]\n{prev}"
                    )
                prompt_parts.append(
                    "CONTEXT FROM PREVIOUS CHUNKS (do not re-describe these "
                    "events; pick up where they ended):\n\n"
                    + "\n\n".join(ctx_blocks)
                )

            prompt_parts.append(questions)

            if is_chunked and i + 1 < len(chunks):
                prompt_parts.append(
                    "End your description by stating which element is "
                    "currently focused at the end of this clip, so the next "
                    "chunk can continue from there."
                )

            clip_prompt = "\n\n".join(prompt_parts)

            description = ""
            last_exc: Exception | None = None
            for attempt in range(1, 4):
                try:
                    description = await _describe_one_clip(clip_path, clip_prompt)
                    if description:
                        last_exc = None
                        break
                    last_exc = RuntimeError("model returned empty description")
                except Exception as exc:
                    last_exc = exc
                logger.warning(
                    "Video describer: chunk %d/%d (%s) attempt %d/3 failed: %s",
                    i + 1, len(chunks), range_label, attempt, last_exc,
                )
                if attempt < 3:
                    await asyncio.sleep(2 ** attempt)

            if last_exc is not None:
                raise RuntimeError(
                    f"Video chunk {i + 1}/{len(chunks)} ({range_label}) failed "
                    f"after 3 attempts: {last_exc}. Refusing to drop video data "
                    f"-- no gaps allowed."
                )

            rolling_context.append(description)
            if is_chunked:
                pieces.append(
                    f"[CHUNK {i + 1}/{len(chunks)} -- {range_label}]\n{description}"
                )
            else:
                pieces.append(description)
    finally:
        if is_chunked:
            _cleanup_chunks(chunks, tmpdir)

    return "\n\n".join(pieces)
