"""Deterministic enumeration of interactive-target dimensions.

Every interactive element on the page has a measured bounding rect from
the Playwright capture. This module turns those rects into a single
canonical list of target measurements -- one entry per target with its
selector, width, height, and centre coordinates.

Two consumers share this one source of truth:
  * checks/base.py:_format_target_size_measurements renders the SC 2.5.8
    judge-prompt block (with nearest-neighbour spacing + PASS/FAIL).
  * functions/claim_validator.py verifies that a finding's cited
    target_width_px / target_height_px matches a real measurement
    (SC 2.5.8 Target Size Minimum and SC 2.5.5 Target Size Enhanced).

Keeping the enumeration in one place means the prompt block and the
claim validator can never disagree about what was measured.
"""
from __future__ import annotations

from typing import Any


def _rect_dims(rect: Any) -> tuple[float, float, float, float] | None:
    """Return (width, height, x, y) as floats, or None if unusable."""
    if not isinstance(rect, dict):
        return None
    try:
        w = float(rect.get("width") or 0)
        h = float(rect.get("height") or 0)
        x = float(rect.get("x") or 0)
        y = float(rect.get("y") or 0)
    except (ValueError, TypeError):
        return None
    # Skip sr-only / visually-hidden-focusable (1x1 clip boxes) and
    # zero-area elements -- they are not real pointer targets.
    if (w <= 2 and h <= 2) or w <= 0 or h <= 0:
        return None
    return w, h, x, y


def compute_target_size_measurements(capture_data: Any) -> list[dict[str, Any]]:
    """Enumerate every interactive target with its measured dimensions.

    Pulls from form fields, links, and the tab-walk (which catches
    native <button> elements and custom interactive controls the first
    two miss). Each entry: selector, kind, width, height, cx, cy, name,
    is_inline. The list is de-duplicated by selector.
    """
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()

    for field in getattr(capture_data, "form_fields", None) or []:
        if not isinstance(field, dict):
            continue
        if (field.get("type") or "").lower() == "hidden":
            continue
        dims = _rect_dims(field.get("rect"))
        if dims is None:
            continue
        w, h, x, y = dims
        sel = field.get("selector", "input")
        if sel in seen:
            continue
        seen.add(sel)
        targets.append({
            "selector": sel, "kind": "form_field",
            "width": w, "height": h, "cx": x + w / 2, "cy": y + h / 2,
            "is_inline": False,
            "name": (field.get("text") or field.get("aria_label")
                     or field.get("aria-label") or ""),
        })

    for link in getattr(capture_data, "links", None) or []:
        if not isinstance(link, dict):
            continue
        dims = _rect_dims(link.get("rect"))
        if dims is None:
            continue
        w, h, x, y = dims
        sel = link.get("selector", "a")
        if sel in seen:
            continue
        seen.add(sel)
        is_inline = bool(
            link.get("in_paragraph")
            or (link.get("context") and link.get("text")
                and link.get("text") in (link.get("context") or ""))
        )
        targets.append({
            "selector": sel, "kind": "link",
            "width": w, "height": h, "cx": x + w / 2, "cy": y + h / 2,
            "is_inline": is_inline,
            "name": (link.get("text") or link.get("aria_label")
                     or link.get("aria-label") or ""),
        })

    # Tab-walk supplemental: native <button> and custom interactive
    # controls that form_fields / links do not enumerate.
    for entry in getattr(capture_data, "tab_walk", None) or []:
        if not isinstance(entry, dict):
            continue
        tag = (entry.get("tag") or "").lower()
        if tag in ("a", "input", "select", "textarea"):
            continue  # already covered by links / form_fields
        sel = entry.get("selector")
        if not sel or sel in seen:
            continue
        dims = _rect_dims(entry.get("rect"))
        if dims is None:
            continue
        w, h, x, y = dims
        seen.add(sel)
        targets.append({
            "selector": sel,
            "kind": "button" if tag == "button" else (tag or "interactive"),
            "width": w, "height": h, "cx": x + w / 2, "cy": y + h / 2,
            "is_inline": False,
            "name": entry.get("text") or sel,
        })

    return targets
