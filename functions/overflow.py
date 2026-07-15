"""Overflow-as-content-loss filtering for resize/reflow/spacing checks.

A geometric overflow at 200% zoom (or under text-spacing overrides) is NOT
the same as WCAG content loss. Carousels/sliders position slides off-screen
by design, decorative image grids crop tiles with object-fit:cover, zero-area
and non-self-clipping boxes lose nothing, and a full-width carousel viewport
clips its track on purpose. This module separates real content-loss overflow
from those benign patterns so SC 1.4.4 / 1.4.10 / 1.4.12 stop emitting
carousel/gallery false positives (verified umich.edu 2026-05-28: all 6
programmatic 1.4.4 findings were such FPs).
"""
from __future__ import annotations

from collections import Counter


def _x(o: dict) -> float:
    return float((o.get("rect") or {}).get("x") or 0)


def _w(o: dict) -> float:
    return float((o.get("rect") or {}).get("width") or 0)


def _h(o: dict) -> float:
    return float((o.get("rect") or {}).get("height") or 0)


def classify_overflow_loss(entries: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Split overflow entries into real content-loss vs benign.

    ``entries`` are overflow records with ``rect`` (x/width/height),
    ``overflowX``/``overflowY``, and ``clippedBySelf``. Returns
    ``(kept, skipped_by_reason)`` where ``kept`` is the entries that represent
    real, on-screen, self-clipping content loss, and ``skipped_by_reason``
    maps a reason string to the count dropped. Entries with neither overflow
    flag set are ignored entirely (not counted as skipped).
    """
    overflowing = [o for o in entries if o.get("overflowX") or o.get("overflowY")]
    # Visible content width = widest left-anchored (x~0) block.
    viewport_w = max((_w(o) for o in overflowing if abs(_x(o)) < 1.0), default=0.0)
    # A (w,h) size shared by >=3 self-clipping elements is a decorative tile
    # grid (gallery / object-fit:cover crops), not text-content loss.
    size_counts = Counter(
        (round(_w(o)), round(_h(o)))
        for o in overflowing
        if o.get("clippedBySelf") and _w(o) > 0 and _h(o) > 0
    )
    grid_sizes = {s for s, c in size_counts.items() if c >= 3}

    kept: list[dict] = []
    skipped: dict[str, int] = {}

    def _skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for o in overflowing:
        x, w, h = _x(o), _w(o), _h(o)
        ov_x, ov_y = o.get("overflowX"), o.get("overflowY")
        if not o.get("clippedBySelf"):
            _skip("does not clip its own content (no loss)")
        elif w <= 0 or h <= 0:
            _skip("zero-area box")
        elif x < 0 or (viewport_w > 0 and x >= viewport_w):
            _skip("off-screen slider/carousel slide")
        elif (round(w), round(h)) in grid_sizes:
            _skip("decorative uniform tile grid (object-fit:cover crop)")
        elif (viewport_w > 0 and abs(x) < 1.0 and abs(w - viewport_w) < 1.0
              and ov_x and not ov_y):
            _skip("full-width carousel viewport (track clipping by design)")
        else:
            kept.append(o)
    return kept, skipped
