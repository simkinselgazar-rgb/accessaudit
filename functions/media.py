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


def encode_image(path: str, max_size: int = 768, quality: int = 80) -> str:
    """Load an image, resize to fit within max_size px, return a base64 JPEG data URI.

    Default 768px/80% balances quality with local model memory limits.
    """
    with Image.open(path) as img:
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")

        w, h = img.size
        if w > max_size or h > max_size:
            scale = max_size / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

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
