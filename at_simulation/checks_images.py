"""Screen-reader AT checks for image-related WCAG criteria.

Each helper takes the a11y-tree ``nodes`` list plus the optional
``capture_data`` snapshot and returns a list of finding dicts. The
checks here cover SC 1.1.1 (Non-text Content) — both meaningful images
that need accessible names and decorative images that must be properly
hidden from assistive tech.
"""
from __future__ import annotations

from typing import Any

from at_simulation.announcements import (
    _get_name,
    _get_properties,
    _get_role,
    is_meaningless_name,
    render_announcement,
)
from at_simulation.screen_reader import _describe_node


def _check_image_names(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check all images have meaningful accessible names."""
    findings = []
    for node in nodes:
        role = _get_role(node)
        if role not in ("img", "image"):
            continue
        name = _get_name(node)
        props = _get_properties(node)

        # Skip decorative images (role=none/presentation or empty alt)
        if props.get("hidden") in (True, "true"):
            continue

        if not name:
            findings.append({
                "element": _describe_node(node),
                "issue": "Image has no accessible name. Screen readers will either "
                         "skip it or announce the filename/URL.",
                "impact": "Screen reader users (JAWS, NVDA, VoiceOver) receive no "
                          "description of this image's content.",
                "severity": "high",
                "recommendation": "WCAG 1.1.1 requires non-text content to have a "
                                  "text alternative that serves the equivalent purpose.",
            })
        elif is_meaningless_name(name):
            announcement = render_announcement(node)
            findings.append({
                "element": _describe_node(node),
                "issue": f"Image accessible name '{name}' is meaningless. "
                         f"Screen readers announce: '{announcement}'.",
                "impact": "Screen reader users hear a filename or placeholder "
                          "instead of a description of the image content.",
                "severity": "high",
                "recommendation": "WCAG 1.1.1 requires the text alternative to convey "
                                  "the same information the image communicates visually.",
            })
    return findings


def _check_decorative_images(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check that decorative images are properly hidden."""
    findings = []
    for node in nodes:
        role = _get_role(node)
        name = _get_name(node)
        # An image with role=presentation/none but with a name is suspicious
        if role in ("none", "presentation") and name and not is_meaningless_name(name):
            findings.append({
                "element": _describe_node(node),
                "issue": f"Image marked as decorative (role='{role}') but has "
                         f"meaningful alt text '{name}'. If the image conveys "
                         f"information, it should not be decorative.",
                "impact": "Screen readers skip this image entirely, so users "
                          "miss the information the alt text describes.",
                "severity": "medium",
                "recommendation": "If this image conveys information, remove "
                                  "role='presentation' so screen readers announce it.",
            })
    return findings
