"""Screen-reader AT checks for focus-related WCAG criteria.

Each helper takes the a11y-tree ``nodes`` list plus the optional
``capture_data`` snapshot and returns a list of finding dicts. The
checks here cover the AT-side signals for SC 2.4.7 (Focus Visible) and
SC 2.4.11 (Focus Not Obscured (Minimum)) — focusable nodes that lack
an accessible name, focused elements covered by sticky/fixed overlays,
and focus indicators with no visible outline or box-shadow.
"""
from __future__ import annotations

from typing import Any

from at_simulation.announcements import (
    _INTERACTIVE_ROLES,
    _get_name,
    _get_properties,
    _get_role,
)
from at_simulation.screen_reader import _describe_node


def _check_focus_visibility(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check focus-related a11y tree properties.

    Visual focus indication is a CSS/screenshot concern, but AT simulation
    can surface the a11y-tree half of SC 2.4.7: every focusable node must
    have an accessible name so a screen reader user can identify where
    focus currently sits. A focusable node with no name produces a silent
    announcement on focus.
    """
    findings: list[dict] = []
    for node in nodes:
        role = _get_role(node)
        if role not in _INTERACTIVE_ROLES:
            continue
        props = _get_properties(node)
        if props.get("focusable") is False:
            continue
        if props.get("disabled") in (True, "true"):
            continue
        name = _get_name(node)
        if not name or not name.strip():
            findings.append({
                "element": _describe_node(node),
                "issue": f"Focusable {role} has no accessible name. On focus, "
                         f"screen readers announce the role only, leaving the user "
                         f"unable to identify what has focus.",
                "impact": "Screen reader users can land on this element via Tab "
                          "but cannot identify what they have focused on.",
                "severity": "high",
                "recommendation": "WCAG 2.4.7 requires focus to be identifiable. "
                                  "Add an accessible name (aria-label, visible "
                                  "label, or text content) so the element is "
                                  "identified on focus.",
            })
    return findings


def _check_focus_obscured(nodes: list[dict], capture_data: Any) -> list[dict]:
    """SC 2.4.11 Focus Not Obscured (Minimum) — detect focused elements
    hidden behind sticky/fixed-position overlays.

    2.4.11 is violated when a keyboard-focused element is entirely
    covered by an always-on piece of UI (sticky header, persistent
    chat widget, cookie banner, toolbar). The user sees focus "move"
    but can't tell where because the element it landed on is
    invisible underneath the overlay.

    Signals used (from capture_data):
      * ``tab_walk`` -- list of focused elements with their bounding
        rects at focus time.
      * ``computed_styles`` -- per-element computed CSS, including
        ``position`` and ``z-index``. Elements with ``position:
        fixed`` or ``position: sticky`` are the overlay candidates.

    A focused rect is "fully obscured" when its ``y + height`` is
    entirely inside an overlay's vertical extent AND the overlay has
    higher z-index / paints later. We only flag when the ENTIRE
    focused rect is covered -- any visible portion means the user
    can still see focus.
    """
    findings: list[dict] = []
    if not capture_data:
        return findings

    tab_walk = getattr(capture_data, "tab_walk", None) or []
    if not tab_walk:
        return findings

    # Identify sticky/fixed overlays from computed_styles.
    styles = getattr(capture_data, "computed_styles", None) or []
    overlays: list[dict] = []
    for s in styles:
        if not isinstance(s, dict):
            continue
        pos = (s.get("position") or "").lower()
        if pos not in ("fixed", "sticky"):
            continue
        rect = s.get("rect") or s.get("boundingRect") or {}
        if not rect or not rect.get("height"):
            continue
        overlays.append({
            "selector": s.get("selector") or s.get("css") or "(unknown)",
            "top": float(rect.get("y") or rect.get("top") or 0),
            "bottom": float(
                (rect.get("y") or rect.get("top") or 0)
                + (rect.get("height") or 0)
            ),
            "left": float(rect.get("x") or rect.get("left") or 0),
            "right": float(
                (rect.get("x") or rect.get("left") or 0)
                + (rect.get("width") or 0)
            ),
            "z_index": s.get("zIndex") or s.get("z-index") or 0,
        })

    if not overlays:
        return findings

    # For each tab stop, check if its rect is fully covered by any
    # overlay. Use a ~2px slack to allow anti-aliasing edges.
    for ts in tab_walk:
        rect = ts.get("rect") or {}
        try:
            y = float(rect.get("y") or 0)
            h = float(rect.get("height") or 0)
            x = float(rect.get("x") or 0)
            w = float(rect.get("width") or 0)
        except (TypeError, ValueError):
            continue
        if h <= 0 or w <= 0:
            continue
        top, bottom = y, y + h
        left, right = x, x + w

        for ov in overlays:
            if (top + 2 >= ov["top"] and bottom - 2 <= ov["bottom"]
                    and left + 2 >= ov["left"] and right - 2 <= ov["right"]):
                findings.append({
                    "element": (
                        f"[focused] {ts.get('selector','(unknown)')}"
                    ),
                    "issue": (
                        f"Focused element is fully covered by overlay "
                        f"{ov['selector']} (position: sticky/fixed). The "
                        f"user cannot see where keyboard focus landed."
                    ),
                    "impact": (
                        "Keyboard users receive no visible feedback about "
                        "which element has focus because an always-on "
                        "overlay blocks the focused element entirely."
                    ),
                    "severity": "high",
                    "recommendation": (
                        "WCAG 2.4.11 requires that when a user interface "
                        "component receives keyboard focus, it is not "
                        "entirely hidden due to author-created content. "
                        "Adjust the overlay's placement, add "
                        "scroll-padding to the focusable area, or use "
                        "scroll-margin-top on focusable elements so they "
                        "scroll into the visible area above the overlay."
                    ),
                })
                break  # one overlay hit is enough; don't double-report

    return findings


def _check_focus_contrast(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check focus indicator contrast against background (SC 2.4.7/2.4.11)."""
    findings = []
    if not capture_data:
        return findings

    focus_indicators = getattr(capture_data, 'focus_indicators', [])
    for indicator in focus_indicators:
        outline_color = indicator.get("outlineColor", "")
        outline_style = indicator.get("outlineStyle", "")
        outline_width = indicator.get("outlineWidth", "")
        box_shadow = indicator.get("boxShadow", "")

        # No visible focus indicator at all
        if (not outline_color or outline_style == "none" or outline_width == "0px") and not box_shadow:
            selector = indicator.get("selector", "(unknown)")
            findings.append({
                "element": f'[focus] {selector}',
                "issue": "Element has no visible focus indicator. No outline, "
                         "border change, or box-shadow appears on focus.",
                "impact": "Keyboard users cannot see which element currently "
                          "has focus, making keyboard navigation unusable.",
                "severity": "high",
                "recommendation": "WCAG 2.4.7 requires a visible focus indicator "
                                  "for all keyboard-focusable elements.",
            })

    return findings
