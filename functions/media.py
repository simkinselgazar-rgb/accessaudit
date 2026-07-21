"""Image and video encoding for LLM API calls, plus small media helpers.

The ONLY place in the codebase that encodes media into base64 data URIs.
Also hosts the tiny predicate helpers that classify <audio>/<video>
elements and detect caption/description <track> elements so every
1.2.x and 1.2.x-AAA check consumes the same logic.
"""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)


# ── Media element predicates (shared across SC 1.2.x checks) ────────────────

def has_track(media: dict, kind: str) -> bool:
    """Return True if *media* has a ``<track>`` entry of the given kind.

    Tracks are extracted from the DOM during capture and stored on each
    media element as ``media["tracks"]`` (list of dicts with a ``kind``
    key). Kind comparison is case-insensitive.
    """
    tracks = media.get("tracks", []) or []
    target = kind.lower()
    return any((t.get("kind") or "").lower() == target for t in tracks)


def media_type(media: dict) -> str:
    """Return ``"audio"`` or ``"video"`` for a captured media element.

    The capture pipeline records the tag name under ``tag`` (or the
    legacy alias ``tagName``). Everything that isn't explicitly ``audio``
    is treated as video -- <video>, YouTube iframes, Vimeo iframes, etc.
    """
    tag = (media.get("tag") or media.get("tagName") or "").lower()
    if tag == "audio":
        return "audio"
    return "video"


# Absolute pixel ceiling per encoded image. Keeps an extremely tall
# full-page screenshot from producing a payload the endpoint rejects
# outright (a single oversized image cannot be batch-split the way a
# multi-image payload can).
_PIXEL_BUDGET = 6_000_000


def encode_image(path: str, max_size: int | None = None, quality: int | None = None) -> str:
    """Load an image, downscale for the vision model, return a base64 JPEG data URI.

    Sizing rules (defaults come from ``AI_IMAGE_MAX_DIM`` / ``AI_IMAGE_QUALITY``):

    - Roughly square images fit their LONG side to ``max_size``.
    - Elongated images (full-page screenshots, where height can be many
      times the width) fit their SHORT side to ``max_size`` instead --
      scaling a 1280x8000 page capture by its long side would crush the
      width to ~120px, destroying the focus rings, small text, and 1px
      outlines that SC 2.4.7 / 1.4.x verdicts rest on.
    - A total-pixel budget caps the worst case either way.
    """
    if max_size is None or quality is None:
        from config import AI_IMAGE_MAX_DIM, AI_IMAGE_QUALITY
        if max_size is None:
            max_size = AI_IMAGE_MAX_DIM
        if quality is None:
            quality = AI_IMAGE_QUALITY

    with Image.open(path) as img:
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")

        w, h = img.size
        long_side, short_side = max(w, h), min(w, h)
        if long_side <= max_size:
            scale = 1.0
        elif long_side <= 2 * short_side:
            scale = max_size / long_side
        else:
            scale = min(1.0, max_size / short_side)
        if (w * scale) * (h * scale) > _PIXEL_BUDGET:
            scale = (_PIXEL_BUDGET / (w * h)) ** 0.5
        if scale < 1.0:
            img = img.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS,
            )

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    return f"data:image/jpeg;base64,{b64}"


def encode_video(path: str) -> str:
    """Read a video file and return a base64 data URI."""
    raw = Path(path).read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")

    suffix = Path(path).suffix.lower()
    mime_map = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".ogg": "video/ogg",
        ".mov": "video/quicktime",
    }
    return f"data:{mime_map.get(suffix, 'video/mp4')};base64,{b64}"
