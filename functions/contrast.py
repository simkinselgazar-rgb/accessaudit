"""Contrast and colour utilities for WCAG testing.

Consolidates all contrast-related helpers: CSS colour parsing, WCAG luminance
and contrast-ratio math, large-text classification, and pixel-sampling
functions for screenshot-based contrast measurement.
"""
from __future__ import annotations

import re

import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# CSS colour parsing
# ---------------------------------------------------------------------------

def parse_rgb(
    color_str: str, bg_str: str | None = None,
) -> tuple[int, int, int] | None:
    """Parse an rgb/rgba/hex colour string into (R, G, B).

    If *color_str* contains an alpha channel (rgba or 8-digit hex) and
    *bg_str* is provided, the foreground is alpha-composited onto the
    background before returning the resulting opaque (R, G, B).
    """
    if not color_str:
        return None
    color_str = color_str.strip()

    alpha: float = 1.0

    # Hex
    m = re.match(r"^#([0-9a-fA-F]{3,8})$", color_str)
    if m:
        h = m.group(1)
        if len(h) == 3:
            r, g, b = int(h[0]*2, 16), int(h[1]*2, 16), int(h[2]*2, 16)
        elif len(h) == 6:
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        elif len(h) == 8:
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            alpha = int(h[6:8], 16) / 255.0
        else:
            return None

        if alpha < 1.0 and bg_str:
            return composite_alpha(r, g, b, alpha, bg_str)
        return r, g, b

    # rgb()/rgba()
    m = re.match(
        r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+))?\s*\)",
        color_str,
    )
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if m.group(4) is not None:
            alpha = float(m.group(4))
            # Normalise percentage-style alpha (values > 1 treated as /255)
            if alpha > 1.0:
                alpha = alpha / 255.0
        if alpha < 1.0 and bg_str:
            return composite_alpha(r, g, b, alpha, bg_str)
        return r, g, b

    return None


def composite_alpha(
    fg_r: int, fg_g: int, fg_b: int, alpha: float, bg_str: str,
) -> tuple[int, int, int] | None:
    """Alpha-composite foreground onto background.

    Formula per channel: composited = fg * alpha + bg * (1 - alpha)
    """
    bg = parse_rgb(bg_str)  # recursive call without bg, so no infinite loop
    if bg is None:
        return fg_r, fg_g, fg_b
    bg_r, bg_g, bg_b = bg
    comp_r = round(fg_r * alpha + bg_r * (1 - alpha))
    comp_g = round(fg_g * alpha + bg_g * (1 - alpha))
    comp_b = round(fg_b * alpha + bg_b * (1 - alpha))
    return comp_r, comp_g, comp_b


# ---------------------------------------------------------------------------
# WCAG luminance & contrast
# ---------------------------------------------------------------------------

def relative_luminance(r: int, g: int, b: int) -> float:
    """WCAG relative luminance from sRGB values 0-255."""
    rs, gs, bs = r / 255.0, g / 255.0, b / 255.0

    def linearize(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * linearize(rs) + 0.7152 * linearize(gs) + 0.0722 * linearize(bs)


def contrast_ratio(lum1: float, lum2: float) -> float:
    """Return the WCAG contrast ratio between two luminances."""
    lighter = max(lum1, lum2)
    darker = min(lum1, lum2)
    return (lighter + 0.05) / (darker + 0.05)


def contrast_ratio_rgb(
    color1: tuple[int, int, int], color2: tuple[int, int, int],
) -> float:
    """Convenience: contrast ratio directly from two (R, G, B) tuples."""
    return contrast_ratio(
        relative_luminance(*color1),
        relative_luminance(*color2),
    )


# ---------------------------------------------------------------------------
# Large-text classification
# ---------------------------------------------------------------------------

def is_large_text(font_size: float | None, font_weight: str | int | None) -> bool:
    """Large text: >= 18pt (24px) or >= 14pt (18.66px) bold."""
    if font_size is None:
        return False
    bold = False
    if font_weight is not None:
        try:
            bold = int(font_weight) >= 700
        except (ValueError, TypeError):
            bold = str(font_weight).lower() in ("bold", "bolder")
    if font_size >= 24:
        return True
    if font_size >= 18.66 and bold:
        return True
    return False


# ---------------------------------------------------------------------------
# Pixel-sampling helpers
# ---------------------------------------------------------------------------

def _load_image(image_path: str) -> Image.Image | None:
    """Open an image file and convert to RGB, returning None on failure."""
    try:
        img = Image.open(image_path).convert("RGB")
        if img.size[0] == 0 or img.size[1] == 0:
            return None
        return img
    except Exception:
        return None


def _clamp(val: int, lo: int, hi: int) -> int:
    """Clamp an integer to [lo, hi]."""
    return max(lo, min(val, hi))


def _median_color(pixels: np.ndarray) -> tuple[int, int, int] | None:
    """Return the per-channel median of an (N, 3) uint8 array.

    Returns None if the array is empty.
    """
    if pixels.size == 0:
        return None
    med = np.median(pixels, axis=0).astype(int)
    return int(med[0]), int(med[1]), int(med[2])


def sample_pixel_color(
    image_path: str, x: int, y: int, sample_size: int = 5,
) -> tuple[int, int, int] | None:
    """Sample the median color in a small region around (x, y).

    Uses median instead of mean to handle anti-aliased text edges.
    sample_size is the radius -- samples a (2*sample_size+1) square.
    Returns (R, G, B) or None if out of bounds or image can't be loaded.
    """
    img = _load_image(image_path)
    if img is None:
        return None
    w, h = img.size
    if x < 0 or x >= w or y < 0 or y >= h:
        return None

    x0 = _clamp(x - sample_size, 0, w - 1)
    y0 = _clamp(y - sample_size, 0, h - 1)
    x1 = _clamp(x + sample_size, 0, w - 1)
    y1 = _clamp(y + sample_size, 0, h - 1)

    region = img.crop((x0, y0, x1 + 1, y1 + 1))
    pixels = np.array(region).reshape(-1, 3)
    return _median_color(pixels)


def sample_element_colors(image_path: str, rect: dict) -> dict:
    """Sample foreground and background colors for an element from a screenshot.

    Uses k-means colour clustering (k=2) on the cropped element region
    to separate text pixels from background pixels. This is the same
    approach professional contrast analysers use and works correctly
    regardless of background complexity (gradients, images, overlays,
    transparency — the browser already composited everything).

    Algorithm:
    1. Crop the element region from the screenshot.
    2. Flatten all pixels into an Nx3 array.
    3. Run k-means with k=2 to find the two dominant colours.
    4. The larger cluster is background (text occupies less area).
    5. The smaller cluster is foreground (the text colour).
    6. For anti-aliased text, the smaller cluster captures the core
       text colour even though individual edge pixels are blended.
    7. If only one meaningful cluster exists (uniform colour), the
       element has no text contrast to measure.

    For elements smaller than 4x4 pixels or with fewer than 16 pixels,
    falls back to a simple most-different-from-median approach.

    *rect* must contain keys ``x``, ``y``, ``width``, ``height``
    (all numeric, in CSS pixels matching the screenshot scale).

    Returns: {
        "fg_color": (R, G, B) or None,
        "bg_color": (R, G, B) or None,
        "contrast_ratio": float or None,
        "method": "kmeans_cluster" | "fallback_max_diff"
    }
    """
    empty: dict = {
        "fg_color": None,
        "bg_color": None,
        "contrast_ratio": None,
        "method": "pixel_sample",
    }

    img = _load_image(image_path)
    if img is None:
        return empty

    ex = int(rect.get("x", 0))
    ey = int(rect.get("y", 0))
    ew = int(rect.get("width", 0))
    eh = int(rect.get("height", 0))
    if ew <= 0 or eh <= 0:
        return empty

    img_w, img_h = img.size
    arr = np.array(img)

    # Crop the element region (clamped to image bounds)
    x0 = _clamp(ex, 0, img_w - 1)
    y0 = _clamp(ey, 0, img_h - 1)
    x1 = _clamp(ex + ew, 1, img_w)
    y1 = _clamp(ey + eh, 1, img_h)
    crop = arr[y0:y1, x0:x1]

    if crop.size == 0:
        return empty

    # Only use RGB channels (drop alpha if present)
    if crop.ndim == 3 and crop.shape[2] >= 3:
        crop = crop[:, :, :3]
    else:
        return empty

    pixels = crop.reshape(-1, 3).astype(np.float64)
    n_pixels = pixels.shape[0]

    if n_pixels < 2:
        return empty

    # ── K-means clustering (k=2) ──────────────────────────────────
    # Manual implementation to avoid sklearn dependency. Simple Lloyd's
    # algorithm converges fast on 2-colour problems (typically 3-5 iters).

    # Initialise centroids: pick the two most different pixels
    # (faster and more robust than random for a 2-cluster problem)
    sample_indices = np.linspace(0, n_pixels - 1, min(n_pixels, 200), dtype=int)
    sample = pixels[sample_indices]

    # First centroid: median of all pixels (likely the background)
    c0 = np.median(sample, axis=0)

    # Second centroid: the sampled pixel furthest from c0
    dists_from_c0 = np.sqrt(np.sum((sample - c0) ** 2, axis=1))
    c1 = sample[int(np.argmax(dists_from_c0))].copy()

    # If the two initial centroids are nearly identical, the element
    # is a uniform colour — no text/background separation possible
    if np.sqrt(np.sum((c0 - c1) ** 2)) < 12:
        colour = _median_color(pixels.astype(np.uint8))
        return {
            "fg_color": colour,
            "bg_color": colour,
            "contrast_ratio": 1.0,
            "method": "uniform_region",
        }

    # Run Lloyd's algorithm for up to 10 iterations
    for _ in range(10):
        d0 = np.sum((pixels - c0) ** 2, axis=1)
        d1 = np.sum((pixels - c1) ** 2, axis=1)
        labels = (d1 < d0).astype(int)  # 0 = closer to c0, 1 = closer to c1

        mask0 = labels == 0
        mask1 = labels == 1
        count0 = int(mask0.sum())
        count1 = int(mask1.sum())

        if count0 == 0 or count1 == 0:
            break

        new_c0 = pixels[mask0].mean(axis=0)
        new_c1 = pixels[mask1].mean(axis=0)

        # Check convergence (centroids stopped moving)
        shift = max(
            np.sqrt(np.sum((new_c0 - c0) ** 2)),
            np.sqrt(np.sum((new_c1 - c1) ** 2)),
        )
        c0 = new_c0
        c1 = new_c1
        if shift < 1.0:
            break

    # Recompute final cluster sizes
    d0 = np.sum((pixels - c0) ** 2, axis=1)
    d1 = np.sum((pixels - c1) ** 2, axis=1)
    labels = (d1 < d0).astype(int)
    count0 = int((labels == 0).sum())
    count1 = int((labels == 1).sum())

    # Post-convergence uniformity check: if the final centroids are very
    # close in RGB space, the crop is effectively a single-colour region
    # and K-means just split its natural variance into two buckets. Any
    # ratio we compute would be garbage (e.g. (232,232,232) vs (255,255,255)
    # = 1.23:1 on a uniform white crop, or (25,25,25) vs (30,30,30) on a
    # uniform dark footer with SR-only text). Return 1.0 so the caller can
    # decide to skip this element rather than emit a misleading finding.
    if np.sqrt(np.sum((c0 - c1) ** 2)) < 30:
        colour = _median_color(pixels.astype(np.uint8))
        return {
            "fg_color": colour,
            "bg_color": colour,
            "contrast_ratio": 1.0,
            "method": "uniform_region_post_kmeans",
        }

    # Larger cluster = background, smaller = foreground (text)
    if count0 >= count1:
        bg_color = (int(round(c0[0])), int(round(c0[1])), int(round(c0[2])))
        fg_color = (int(round(c1[0])), int(round(c1[1])), int(round(c1[2])))
        fg_pct = count1 / n_pixels
    else:
        bg_color = (int(round(c1[0])), int(round(c1[1])), int(round(c1[2])))
        fg_color = (int(round(c0[0])), int(round(c0[1])), int(round(c0[2])))
        fg_pct = count0 / n_pixels

    # Sanity check: if the "foreground" cluster is > 70% of pixels,
    # it's probably not text — both clusters are background variants
    # (e.g. gradient). In that case compute contrast between the two
    # background shades (which is the worst-case contrast on the gradient).
    if fg_pct > 0.70:
        method = "gradient_extremes"
    else:
        method = "kmeans_cluster"

    # Clamp to valid RGB range
    bg_color = tuple(_clamp(c, 0, 255) for c in bg_color)
    fg_color = tuple(_clamp(c, 0, 255) for c in fg_color)

    cr = contrast_ratio_rgb(fg_color, bg_color)

    return {
        "fg_color": fg_color,
        "bg_color": bg_color,
        "contrast_ratio": round(cr, 2),
        "method": method,
    }


def measure_focus_change(
    before_path: str, after_path: str, rect: dict,
) -> dict:
    """Measure the visual change between unfocused and focused element screenshots.

    Crops both screenshots to the element rect + margin, computes pixel diff,
    extracts the dominant NEW color (the focus indicator color), and computes
    its contrast against the background.

    *rect* must contain keys ``x``, ``y``, ``width``, ``height``.

    Returns: {
        "visible": bool,           # did anything visually change?
        "changed_pixel_ratio": float,  # fraction of pixels that changed
        "indicator_color": (R, G, B) or None,  # the new color (focus ring/outline)
        "bg_color": (R, G, B) or None,  # background behind the indicator
        "indicator_contrast": float or None,  # contrast of indicator vs background
        "method": "pixel_diff"
    }
    """
    empty: dict = {
        "visible": False,
        "changed_pixel_ratio": 0.0,
        "indicator_color": None,
        "bg_color": None,
        "indicator_contrast": None,
        "method": "pixel_diff",
    }

    before_img = _load_image(before_path)
    after_img = _load_image(after_path)
    if before_img is None or after_img is None:
        return empty

    ex = int(rect.get("x", 0))
    ey = int(rect.get("y", 0))
    ew = int(rect.get("width", 0))
    eh = int(rect.get("height", 0))
    if ew <= 0 or eh <= 0:
        return empty

    # Expand rect by a margin so we capture focus rings drawn outside the element
    margin = max(8, int(max(ew, eh) * 0.15))
    bw = min(before_img.size[0], after_img.size[0])
    bh = min(before_img.size[1], after_img.size[1])

    crop_x0 = _clamp(ex - margin, 0, bw - 1)
    crop_y0 = _clamp(ey - margin, 0, bh - 1)
    crop_x1 = _clamp(ex + ew + margin, 0, bw)
    crop_y1 = _clamp(ey + eh + margin, 0, bh)
    if crop_x1 <= crop_x0 or crop_y1 <= crop_y0:
        return empty

    before_crop = np.array(before_img.crop((crop_x0, crop_y0, crop_x1, crop_y1)))
    after_crop = np.array(after_img.crop((crop_x0, crop_y0, crop_x1, crop_y1)))

    if before_crop.shape != after_crop.shape:
        return empty

    # Per-pixel Euclidean distance in RGB space
    diff = np.abs(after_crop.astype(np.int16) - before_crop.astype(np.int16))
    pixel_dist = np.sqrt(np.sum(diff.astype(np.float64) ** 2, axis=2))

    # Threshold: a pixel is "changed" if its RGB distance > 30
    change_threshold = 30
    changed_mask = pixel_dist > change_threshold
    total_pixels = changed_mask.size
    changed_count = int(np.sum(changed_mask))
    changed_ratio = changed_count / total_pixels if total_pixels > 0 else 0.0

    # Need at least 0.5% of pixels changed to count as visible
    visible = changed_ratio >= 0.005

    if not visible:
        return {
            **empty,
            "changed_pixel_ratio": round(changed_ratio, 4),
        }

    # Extract the dominant NEW colour (the focus indicator)
    changed_pixels_after = after_crop[changed_mask]
    indicator_color = _median_color(changed_pixels_after)

    # Background: the unchanged pixels surrounding the changed area
    unchanged_mask = ~changed_mask
    unchanged_pixels = before_crop[unchanged_mask]
    bg_color = _median_color(unchanged_pixels)

    indicator_contrast: float | None = None
    if indicator_color is not None and bg_color is not None:
        indicator_contrast = round(
            contrast_ratio_rgb(indicator_color, bg_color), 2,
        )

    return {
        "visible": True,
        "changed_pixel_ratio": round(changed_ratio, 4),
        "indicator_color": indicator_color,
        "bg_color": bg_color,
        "indicator_contrast": indicator_contrast,
        "method": "pixel_diff",
    }
