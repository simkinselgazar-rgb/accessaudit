"""Pixel-level image comparison utilities.

Used by Phase 2 exploration (skip LLM when nothing changed),
focus indicator detection (2.4.7), content-on-focus (1.4.13),
and auto-advancing content detection (2.2.2).
"""
from __future__ import annotations

import logging
from collections import Counter

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


def screenshots_differ(path_a: str, path_b: str, threshold: float = 0.005) -> bool:
    """Fast pixel-diff check between two screenshots.

    Returns True if the images differ by more than `threshold` fraction of
    pixels. Uses raw byte comparison first (instant for identical files),
    then falls back to a numpy pixel diff for near-identical screenshots.

    threshold=0.005 means 0.5% of pixels must change — enough to catch a
    tooltip or outline appearing, but ignores sub-pixel anti-aliasing jitter.
    """
    try:
        with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
            bytes_a = fa.read()
            bytes_b = fb.read()

        if bytes_a == bytes_b:
            return False

        img_a = np.array(Image.open(path_a).convert("RGB"))
        img_b = np.array(Image.open(path_b).convert("RGB"))

        if img_a.shape != img_b.shape:
            return True

        diff_pixels = np.any(np.abs(img_a.astype(int) - img_b.astype(int)) > 10, axis=2)
        diff_ratio = diff_pixels.sum() / diff_pixels.size
        return diff_ratio > threshold
    except Exception as e:
        logger.debug("Pixel diff failed (%s vs %s): %s — assuming different", path_a, path_b, e)
        return True


def _crop_to_rect(img: Image.Image, rect: dict, margin: int) -> Image.Image:
    """Crop an image to the given rect plus margin, clamped to image bounds."""
    w, h = img.size
    left = max(0, rect["x"] - margin)
    top = max(0, rect["y"] - margin)
    right = min(w, rect["x"] + rect["width"] + margin)
    bottom = min(h, rect["y"] + rect["height"] + margin)
    return img.crop((left, top, right, bottom))


def diff_region(path_a: str, path_b: str, rect: dict, margin: int = 20) -> dict:
    """Compare a specific rectangular region between two screenshots.

    Crops both images to rect + margin, then computes pixel diff.
    More targeted than full-screenshot diff — useful for comparing
    just the area around a specific element.

    Args:
        path_a: First screenshot path.
        path_b: Second screenshot path.
        rect: {"x": int, "y": int, "width": int, "height": int}
        margin: Extra pixels around rect to include.

    Returns:
        {
            "differ": bool,
            "changed_ratio": float (0.0-1.0),
            "total_pixels": int,
            "changed_pixels": int,
        }
    """
    empty = {"differ": False, "changed_ratio": 0.0, "total_pixels": 0, "changed_pixels": 0}

    if not rect or rect.get("width", 0) <= 0 or rect.get("height", 0) <= 0:
        return empty

    try:
        img_a = Image.open(path_a).convert("RGB")
        img_b = Image.open(path_b).convert("RGB")
    except Exception as e:
        logger.debug("diff_region: failed to open images (%s, %s): %s", path_a, path_b, e)
        return empty

    crop_a = _crop_to_rect(img_a, rect, margin)
    crop_b = _crop_to_rect(img_b, rect, margin)

    if crop_a.size[0] == 0 or crop_a.size[1] == 0:
        return empty

    if crop_a.size != crop_b.size:
        return {"differ": True, "changed_ratio": 1.0,
                "total_pixels": crop_a.size[0] * crop_a.size[1],
                "changed_pixels": crop_a.size[0] * crop_a.size[1]}

    arr_a = np.array(crop_a)
    arr_b = np.array(crop_b)

    diff_mask = np.any(np.abs(arr_a.astype(int) - arr_b.astype(int)) > 10, axis=2)
    total = diff_mask.size
    changed = int(diff_mask.sum())
    ratio = changed / total if total > 0 else 0.0

    return {
        "differ": changed > 0,
        "changed_ratio": ratio,
        "total_pixels": total,
        "changed_pixels": changed,
    }


def extract_changed_color(
    path_a: str, path_b: str, rect: dict, margin: int = 20
) -> tuple[int, int, int] | None:
    """Extract the dominant NEW color in the changed region between two screenshots.

    Useful for detecting focus indicator color: diff the unfocused and focused
    screenshots, find which pixels changed, and return the most common new color.

    Args:
        path_a: First screenshot path (before state).
        path_b: Second screenshot path (after state).
        rect: {"x": int, "y": int, "width": int, "height": int}
        margin: Extra pixels around rect to include.

    Returns:
        (R, G, B) of the dominant changed color, or None if no change.
    """
    if not rect or rect.get("width", 0) <= 0 or rect.get("height", 0) <= 0:
        return None

    try:
        img_a = Image.open(path_a).convert("RGB")
        img_b = Image.open(path_b).convert("RGB")
    except Exception as e:
        logger.debug("extract_changed_color: failed to open images (%s, %s): %s", path_a, path_b, e)
        return None

    crop_a = _crop_to_rect(img_a, rect, margin)
    crop_b = _crop_to_rect(img_b, rect, margin)

    if crop_a.size[0] == 0 or crop_a.size[1] == 0:
        return None

    if crop_a.size != crop_b.size:
        return None

    arr_a = np.array(crop_a)
    arr_b = np.array(crop_b)

    diff_mask = np.any(np.abs(arr_a.astype(int) - arr_b.astype(int)) > 10, axis=2)
    if not diff_mask.any():
        return None

    new_colors = arr_b[diff_mask]
    counter = Counter(map(tuple, new_colors.tolist()))
    dominant = counter.most_common(1)[0][0]
    return (dominant[0], dominant[1], dominant[2])
