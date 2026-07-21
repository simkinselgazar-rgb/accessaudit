"""Bounding-box annotation for screenshots sent to vision models.

Two consumers rely on this module:

- ``checks/base.py`` labels the elements relevant to an SC with
  ``assign_box_labels`` and sends one annotated full-page screenshot so
  the model can tie each "[Box N]" prompt line to a visual location.
- ``capture/v2/phase2_visual_explorer.py`` draws a single box around the
  element being explored so before/after state screenshots are
  unambiguous about which element was interacted with.

Both callers treat annotation as best-effort: any failure returns None
and the caller falls back to the clean screenshot.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

_BOX_COLOR = (230, 30, 30)
_LABEL_BG = (230, 30, 30)
_LABEL_FG = (255, 255, 255)


def assign_box_labels(elements: list) -> None:
    """Assign sequential ``_bb_label`` integers to drawable elements.

    An element is drawable when it is a dict carrying a ``rect`` dict
    with positive width and height. Elements without a usable rect are
    left unlabeled; ``functions/prompt.py`` then renders them without a
    "[Box N]" prefix, so prompt text and annotated image stay in sync.
    """
    n = 0
    for elem in elements or []:
        if not isinstance(elem, dict):
            continue
        rect = elem.get("rect")
        if (
            isinstance(rect, dict)
            and float(rect.get("width", 0) or 0) > 0
            and float(rect.get("height", 0) or 0) > 0
        ):
            n += 1
            elem["_bb_label"] = n


def annotate_screenshot(
    screenshot_path: str,
    elements: list,
    tag: str,
    out_dir: str | None = None,
) -> str | None:
    """Draw numbered bounding boxes on a screenshot.

    Draws one box per element that has both a ``_bb_label`` (see
    ``assign_box_labels``) and a positive-size ``rect`` in page
    coordinates ({x, y, width, height} or {left, top, ...}). The
    annotated copy is written to ``out_dir`` (or next to the source
    screenshot) as ``annotated_<tag>.png``.

    Returns the annotated file path, or None when there is nothing to
    draw or annotation fails — callers fall back to the clean image.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.warning("Pillow not available -- screenshot annotation skipped")
        return None

    boxes: list[tuple[int, dict]] = []
    for elem in elements or []:
        if not isinstance(elem, dict) or elem.get("_bb_label") is None:
            continue
        rect = elem.get("rect")
        if isinstance(rect, dict):
            boxes.append((int(elem["_bb_label"]), rect))
    if not boxes:
        return None

    try:
        img = Image.open(screenshot_path).convert("RGB")
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("arial.ttf", 22)
        except Exception:  # font lookup is platform-dependent; default is fine
            font = ImageFont.load_default()

        drawn = 0
        for label, rect in boxes:
            x = float(rect.get("x", rect.get("left", 0)) or 0)
            y = float(rect.get("y", rect.get("top", 0)) or 0)
            w = float(rect.get("width", 0) or 0)
            h = float(rect.get("height", 0) or 0)
            if w <= 0 or h <= 0:
                continue
            x1 = max(0, min(x, img.width - 1))
            y1 = max(0, min(y, img.height - 1))
            x2 = max(x1 + 1, min(x + w, img.width))
            y2 = max(y1 + 1, min(y + h, img.height))
            draw.rectangle((x1, y1, x2, y2), outline=_BOX_COLOR, width=3)

            text = str(label)
            tb = draw.textbbox((0, 0), text, font=font)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            pad = 4
            # Label above the box when there is room, else inside it.
            ly = y1 - th - 2 * pad if y1 - th - 2 * pad >= 0 else y1
            lx = x1
            draw.rectangle((lx, ly, lx + tw + 2 * pad, ly + th + 2 * pad), fill=_LABEL_BG)
            draw.text((lx + pad, ly + pad), text, fill=_LABEL_FG, font=font)
            drawn += 1

        if not drawn:
            return None

        safe_tag = re.sub(r"[^\w\-]", "_", str(tag))[:60] or "boxes"
        dest_dir = out_dir or os.path.dirname(screenshot_path) or "."
        os.makedirs(dest_dir, exist_ok=True)
        out_path = os.path.join(dest_dir, f"annotated_{safe_tag}.png")
        img.save(out_path)
        return out_path
    except Exception as exc:
        logger.warning(
            "Screenshot annotation failed for %s: %s", screenshot_path, exc,
        )
        return None
