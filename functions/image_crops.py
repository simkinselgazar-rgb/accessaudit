"""Per-image cropped screenshots for image-bearing SCs (1.1.1, 1.4.5, 4.1.2).

The judge / visual AI receives the full-page screenshot plus a list of
image entries from the DOM (selector, alt, role, etc). Without explicit
"this image entry corresponds to that pixel region" binding, the model
has to visually correlate the DOM list against the screenshot, which:

  - Fails outright for CSS background-images (no <img> to point at —
    just a `background-image:url(...)` style on some <div>).
  - Fails on cluttered pages with multiple alt="" images that look
    similar to the model.
  - Leads to false positives and false negatives on SC 1.1.1
    (decorative vs meaningful) and SC 1.4.5 (background image text).

This module crops each image element's rect out of the full-page
screenshot, saves it as `image_<N>.png`, and writes `crop_id`
("IMG-1", "BG-1", ...) plus `crop_path` back onto the image dict.
The DOM-context prompt builder then binds each crop to its IMG-N /
BG-N label, and the visual-AI call attaches the crops as multimodal
images in the same order. Now the AI has unambiguous per-image
evidence.

CLAUDE.md rule alignment:
  - "Find root causes, don't patch symptoms": instead of telling the
    model "decide better about decorative images," we hand it the
    actual cropped pixels next to each DOM entry.
  - "Reusable code lives in functions/": this module is consumed by
    `capture/v2/v1_compat.py` (Phase D) and `checks/base.py`
    (`_build_dom_context`, `get_extra_images`).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Padding around the image rect when cropping. A bit of surrounding
# context helps the model tell whether the image stands alone or is
# embedded in a card / hero / nav and lets it see adjacent text labels
# that may be the "real content."
CROP_PADDING_PX = 30

# Below this rect area (5×5) we treat the element as a sub-pixel
# artefact — icon-font glyph, sr-only SVG, 1px tracking pixel — and
# skip cropping. Cropping these would just produce noise.
MIN_RECT_AREA = 25


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def crop_images_from_full_page(
    capture_data,
    full_page_path: str,
    output_dir: str | os.PathLike[str],
) -> int:
    """Crop every image element's rect from `full_page_path` and write
    `output_dir/image_<N>.png`. Mutates each image dict on
    `capture_data.images` and `capture_data.background_images` in place
    to add `crop_id` and `crop_path`.

    Numbering scheme:
      - <img> elements: IMG-1, IMG-2, ...
      - background-images: BG-1, BG-2, ...
    Numbers are sequential within each kind, in the order the entries
    already appear on `capture_data`.

    Returns the total number of crops written.

    Failures are logged at WARNING (per CLAUDE.md "log path on file
    failure") and the function continues so a single bad rect can't
    kill the whole pass.
    """
    if not full_page_path or not os.path.exists(full_page_path):
        logger.debug("crop_images: full_page_path missing or not on disk (%r), skipping", full_page_path)
        return 0

    try:
        from PIL import Image
    except ImportError:
        logger.warning("crop_images: Pillow not installed; per-image crops disabled")
        return 0

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    try:
        page_img = Image.open(full_page_path).convert("RGB")
    except Exception:
        logger.exception("crop_images: failed to open %s", full_page_path)
        return 0

    page_w, page_h = page_img.width, page_img.height
    saved = 0

    def _try_crop(item: dict, prefix: str, idx: int) -> bool:
        rect = item.get("rect") or {}
        x = _safe_int(rect.get("x"))
        y = _safe_int(rect.get("y"))
        w = _safe_int(rect.get("width"))
        h = _safe_int(rect.get("height"))
        if w * h < MIN_RECT_AREA:
            return False
        x1 = max(0, x - CROP_PADDING_PX)
        y1 = max(0, y - CROP_PADDING_PX)
        x2 = min(page_w, x + w + CROP_PADDING_PX)
        y2 = min(page_h, y + h + CROP_PADDING_PX)
        if x2 <= x1 or y2 <= y1:
            return False
        crop_id = f"{prefix}-{idx}"
        crop_filename = f"image_{prefix.lower()}_{idx}.png"
        crop_path = output_path / crop_filename
        try:
            page_img.crop((x1, y1, x2, y2)).save(str(crop_path))
        except Exception:
            logger.warning(
                "crop_images: crop save failed for selector=%s rect=%s -> %s",
                item.get("selector"), rect, crop_path,
                exc_info=True,
            )
            return False
        item["crop_id"] = crop_id
        item["crop_path"] = str(crop_path)
        return True

    img_idx = 0
    for img in capture_data.images or []:
        img_idx += 1
        if _try_crop(img, "IMG", img_idx):
            saved += 1
        else:
            # Numbering still advances so IMG-N matches list position;
            # otherwise prompt-text and attached-image order can drift
            # when one entry has a zero-area rect mid-list.
            pass

    bg_idx = 0
    for bg in capture_data.background_images or []:
        bg_idx += 1
        if _try_crop(bg, "BG", bg_idx):
            saved += 1

    logger.info(
        "crop_images: saved %d crops (%d <img> + %d bg-images considered) to %s",
        saved,
        len(capture_data.images or []),
        len(capture_data.background_images or []),
        output_path,
    )
    return saved
