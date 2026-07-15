"""Caption accuracy verification using Whisper transcription.

Extracts audio from media, transcribes with Whisper, and compares
against displayed captions/transcripts to verify accuracy.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path

import httpx

from config import (
    WHISPER_API_KEY,
    WHISPER_API_URL,
    WHISPER_FORMAT,
    WHISPER_GEMINI_MODEL,
    MEDIA_DOWNLOAD_TIMEOUT,
)
from models import CaptionComparisonResult, CaptionSegment

logger = logging.getLogger(__name__)


async def _transcribe_via_gemini(audio_bytes: bytes) -> list[CaptionSegment]:
    """Transcribe audio using Gemini's native multimodal generateContent.

    Gemini's OpenAI-compatible endpoint does not expose
    ``/audio/transcriptions``; audio understanding goes through the native
    REST API with inline_data parts. We use the same API key as the rest
    of the Gemini stack -- a single credential covers chat, vision,
    embeddings, AND transcription.

    Audio is sent as base64 inline_data. The 60-second sample cap in
    ``audio_transcriber.DEFAULT_SAMPLE_SECONDS`` keeps every clip under
    Gemini's ~20MB inline limit, so chunking is not needed here. If we
    later raise the cap, this function should switch to the Files API.
    """
    if not WHISPER_API_KEY:
        raise RuntimeError(
            "WHISPER_API_KEY (or AI_API_KEY) is empty -- Gemini audio "
            "transcription cannot authenticate."
        )
    base = WHISPER_API_URL.rstrip("/")
    if base.endswith("/openai"):
        # User pointed whisper_api_url at the OpenAI-compat endpoint.
        # Gemini audio uses the native API, not OpenAI-compat -- strip
        # the suffix so we hit the right path.
        base = base[: -len("/openai")]
    # Key goes in the x-goog-api-key header, NOT the query string: httpx
    # exception messages embed the request URL, so a ?key= param would leak
    # the credential into logs on any HTTP error.
    url = f"{base}/models/{WHISPER_GEMINI_MODEL}:generateContent"

    payload = {
        "contents": [{
            "parts": [
                {
                    "text": (
                        "Transcribe this audio verbatim. Return JSON only, "
                        "matching exactly the schema: "
                        "{\"segments\":[{\"start\":<seconds_float>,"
                        "\"end\":<seconds_float>,\"text\":<string>}]}. "
                        "Each segment should be one continuous spoken phrase. "
                        "Use real timestamps in seconds from the start of the "
                        "audio. If the audio contains music or no speech, "
                        "return {\"segments\": []}."
                    ),
                },
                {
                    "inline_data": {
                        "mime_type": "audio/wav",
                        "data": base64.b64encode(audio_bytes).decode("ascii"),
                    },
                },
            ],
        }],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": {
                "type": "OBJECT",
                "properties": {
                    "segments": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "start": {"type": "NUMBER"},
                                "end": {"type": "NUMBER"},
                                "text": {"type": "STRING"},
                            },
                            "required": ["start", "end", "text"],
                        },
                    },
                },
                "required": ["segments"],
            },
        },
    }

    async with httpx.AsyncClient(timeout=MEDIA_DOWNLOAD_TIMEOUT * 4) as client:
        resp = await client.post(
            url, json=payload, headers={"x-goog-api-key": WHISPER_API_KEY},
        )
        resp.raise_for_status()
        body = resp.json()

    # Pull JSON text out of the candidates structure
    candidates = body.get("candidates") or []
    if not candidates:
        return []
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text_payload = ""
    for p in parts:
        if isinstance(p, dict) and p.get("text"):
            text_payload += p["text"]
    if not text_payload:
        return []
    try:
        parsed = json.loads(text_payload)
    except json.JSONDecodeError:
        logger.warning(
            "Gemini transcription returned non-JSON content: %s",
            text_payload,
        )
        return []

    out: list[CaptionSegment] = []
    for seg in parsed.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg.get("start", 0))
            end = float(seg.get("end", 0))
        except (TypeError, ValueError):
            continue
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        out.append(CaptionSegment(start=start, end=end, text=text))
    return out


async def extract_audio(video_path: str, output_dir: str) -> str | None:
    """Extract audio from a video file using ffmpeg.

    Returns the path to the extracted .wav file, or None on failure.
    """
    output_path = os.path.join(output_dir, "extracted_audio.wav")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", video_path,
            "-vn",  # no video
            "-acodec", "pcm_s16le",  # PCM 16-bit
            "-ar", "16000",  # 16kHz for Whisper
            "-ac", "1",  # mono
            output_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("ffmpeg audio extraction failed: %s", stderr.decode())
            return None
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return output_path
        return None
    except FileNotFoundError:
        logger.warning("ffmpeg not found — cannot extract audio")
        return None
    except Exception as exc:
        logger.warning("Audio extraction failed: %s", exc)
        return None


async def transcribe_audio(audio_path: str) -> list[CaptionSegment]:
    """Send audio to a Whisper-style API and return timestamped segments.

    Three transcription paths are supported, selected by ``WHISPER_FORMAT``:

    - ``"gemini"``: Gemini native multimodal ``generateContent`` with
      audio sent as inline_data. Same API key as the rest of the
      Gemini stack -- ideal for full-Gemini deployments. Audio clips
      should stay under ~20MB inline (the 60-second sample cap in
      ``audio_transcriber`` already enforces this).
    - ``"openai"``: OpenAI-compatible
      ``{WHISPER_API_URL}/audio/transcriptions`` endpoint. Works
      against OpenAI's Whisper-1 directly or any OpenAI-compatible
      audio host.
    - ``"local"``: faster-whisper HTTP at
      ``{WHISPER_API_URL}/transcribe`` returning
      ``{"segments": [...], "full_text": "..."}``. The 11803 fleet
      (large-v3-turbo) exposes this shape.
    - ``"auto"`` (default for backward compatibility): tries the
      local shape first, then falls back to the OpenAI-compatible
      shape on 404 or empty body.
    """
    audio_bytes = Path(audio_path).read_bytes()
    fmt = (WHISPER_FORMAT or "auto").lower()

    # Gemini native path -- isolated branch, no fallback because if the
    # user picked "gemini" they want Gemini and a silent fallback would
    # hide misconfiguration.
    if fmt == "gemini":
        try:
            return await _transcribe_via_gemini(audio_bytes)
        except Exception as exc:
            logger.warning("Gemini transcription failed: %s", exc)
            return []

    try:
        data = None
        async with httpx.AsyncClient(timeout=MEDIA_DOWNLOAD_TIMEOUT * 4) as client:
            # 1) Local faster-whisper shape (default for fmt="local"/"auto")
            if fmt in ("local", "auto"):
                try:
                    resp = await client.post(
                        f"{WHISPER_API_URL}/transcribe",
                        files={"file": ("audio.wav", audio_bytes, "audio/wav")},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                except Exception:
                    data = None

            # 2) OpenAI-compatible shape
            if data is None and fmt in ("openai", "auto"):
                openai_headers = {}
                if WHISPER_API_KEY:
                    openai_headers["Authorization"] = f"Bearer {WHISPER_API_KEY}"
                resp = await client.post(
                    f"{WHISPER_API_URL}/audio/transcriptions",
                    files={"file": ("audio.wav", audio_bytes, "audio/wav")},
                    data={
                        "model": "whisper-1",
                        "response_format": "verbose_json",
                        "timestamp_granularities[]": "segment",
                    },
                    headers=openai_headers,
                )
                resp.raise_for_status()
                data = resp.json()
            if data is None:
                logger.warning(
                    "transcribe_audio: no path matched WHISPER_FORMAT=%s",
                    fmt,
                )
                return []

        segments: list[CaptionSegment] = []
        raw_segments = data.get("segments") or []
        for seg in raw_segments:
            if not isinstance(seg, dict):
                continue
            segments.append(CaptionSegment(
                start=float(seg.get("start", 0)),
                end=float(seg.get("end", 0)),
                text=(seg.get("text") or "").strip(),
            ))

        # Fallback: if the server only returned plain text (either the
        # local ``full_text`` key or the OpenAI ``text`` key), make a
        # single segment covering the whole clip. Timestamps are zero
        # but compare_captions only uses text for matching anyway.
        if not segments:
            full_text = data.get("full_text") or data.get("text") or ""
            if full_text.strip():
                segments.append(CaptionSegment(
                    start=0.0,
                    end=0.0,
                    text=full_text.strip(),
                ))

        return segments

    except Exception as exc:
        logger.warning("Whisper transcription failed: %s", exc)
        return []


def compare_captions(
    whisper_segments: list[CaptionSegment],
    displayed_text: str,
) -> CaptionComparisonResult:
    """Compare Whisper transcription against displayed captions/transcript.

    Uses word-level overlap to compute accuracy. A segment is "matched"
    if >60% of its words appear in the displayed text. A segment is
    "inaccurate" if <40% of its words match.

    Args:
        whisper_segments: Timestamped segments from Whisper.
        displayed_text: The caption/transcript text shown on the page.

    Returns:
        CaptionComparisonResult with accuracy metrics.
    """
    if not whisper_segments:
        return CaptionComparisonResult(
            overall_accuracy=0.0,
            matched_segments=0,
            total_segments=0,
        )

    if not displayed_text or not displayed_text.strip():
        # No captions/transcript displayed — everything is "missing"
        return CaptionComparisonResult(
            overall_accuracy=0.0,
            matched_segments=0,
            total_segments=len(whisper_segments),
            missing_segments=[
                {
                    "start": seg.start,
                    "end": seg.end,
                    "whisper_text": seg.text,
                    "reason": "No caption/transcript text found on page",
                }
                for seg in whisper_segments if seg.text
            ],
        )

    # Normalize displayed text for comparison
    displayed_words = set(_normalize_text(displayed_text).split())

    matched = 0
    missing: list[dict] = []
    inaccurate: list[dict] = []
    total_word_matches = 0
    total_whisper_words = 0

    for seg in whisper_segments:
        if not seg.text:
            continue

        seg_words = _normalize_text(seg.text).split()
        if not seg_words:
            continue

        total_whisper_words += len(seg_words)

        # Count how many words from this segment appear in displayed text
        matches = sum(1 for w in seg_words if w in displayed_words)
        match_ratio = matches / len(seg_words) if seg_words else 0
        total_word_matches += matches

        if match_ratio >= 0.6:
            matched += 1
        elif match_ratio < 0.4:
            if match_ratio == 0:
                missing.append({
                    "start": seg.start,
                    "end": seg.end,
                    "whisper_text": seg.text,
                    "reason": "Segment not found in displayed captions",
                })
            else:
                inaccurate.append({
                    "start": seg.start,
                    "end": seg.end,
                    "whisper_text": seg.text,
                    "match_ratio": round(match_ratio, 2),
                    "reason": f"Only {match_ratio:.0%} of words matched",
                })

    overall = total_word_matches / total_whisper_words if total_whisper_words > 0 else 0.0

    return CaptionComparisonResult(
        overall_accuracy=round(overall, 3),
        matched_segments=matched,
        total_segments=len([s for s in whisper_segments if s.text]),
        missing_segments=missing,
        inaccurate_segments=inaccurate,
    )


async def verify_caption_accuracy(
    video_path: str,
    displayed_caption_text: str,
    work_dir: str,
) -> CaptionComparisonResult | None:
    """Full pipeline: extract audio -> transcribe -> compare.

    Args:
        video_path: Path to the media file.
        displayed_caption_text: The caption/transcript text from the page.
        work_dir: Directory for temporary files.

    Returns:
        CaptionComparisonResult, or None if the pipeline fails.
    """
    try:
        # Step 1: Extract audio
        audio_path = await extract_audio(video_path, work_dir)
        if not audio_path:
            logger.info("No audio extracted from %s — skipping caption verification", video_path)
            return None

        # Step 2: Transcribe with Whisper
        segments = await transcribe_audio(audio_path)
        if not segments:
            logger.info("Whisper returned no segments for %s", video_path)
            return None

        # Step 3: Compare
        result = compare_captions(segments, displayed_caption_text)

        logger.info(
            "Caption accuracy for %s: %.0f%% (%d/%d segments matched, "
            "%d missing, %d inaccurate)",
            Path(video_path).name,
            result.overall_accuracy * 100,
            result.matched_segments,
            result.total_segments,
            len(result.missing_segments),
            len(result.inaccurate_segments),
        )

        # Clean up temp audio
        try:
            os.remove(audio_path)
        except Exception:
            # best-effort temp cleanup; OS will sweep tmpdir if this fails
            pass

        return result

    except Exception as exc:
        logger.warning("Caption verification failed: %s", exc)
        return None


def _normalize_text(text: str) -> str:
    """Normalize text for word-level comparison."""
    import re
    # Lowercase
    text = text.lower()
    # Remove timestamps like [00:01] or (00:01)
    text = re.sub(r'[\[\(]\d{1,2}:\d{2}(?::\d{2})?[\]\)]', '', text)
    # Remove speaker labels only at line starts ("speaker:", "john:") so we
    # don't delete real words ("note:", "warning:") or corrupt ratios ("4.5:1").
    text = re.sub(r"(?m)^\s*\w[\w .'-]{0,30}:\s", '', text)
    # Remove punctuation except apostrophes
    text = re.sub(r"[^\w\s']", ' ', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text
