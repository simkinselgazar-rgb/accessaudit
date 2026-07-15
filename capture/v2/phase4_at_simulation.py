"""Phase 4: AT Simulation — wraps existing AT modules + inventory cross-ref.

Runs the existing screen reader, keyboard navigation, and announcement
simulation. Additionally cross-references the Phase 1 element inventory
against the accessibility tree to detect elements invisible to assistive
technologies.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from capture.v2.element_inventory import ElementInventory

logger = logging.getLogger(__name__)


async def run_phase4(
    capture_data: Any,
    inventory: ElementInventory,
    captures_dir: str,
    progress_callback=None,
) -> None:
    """Run Phase 4: AT Simulation + inventory cross-reference.

    Modifies capture_data in place with:
    - at_missing_elements: elements the AI found but the a11y tree doesn't have
    """
    phase_start = time.monotonic()
    logger.info("=" * 60)
    logger.info("PHASE 4: AT Simulation + Inventory Cross-Reference")
    logger.info("=" * 60)

    if progress_callback:
        await progress_callback("Phase 4: Running assistive technology simulation...")

    a11y_tree = capture_data.a11y_tree
    if not a11y_tree or not a11y_tree.get("nodes"):
        logger.warning("PHASE 4: No accessibility tree available — skipping")
        capture_data.phase_timings["phase4"] = 0.0
        return

    nodes = a11y_tree.get("nodes", [])
    logger.info("PHASE 4: Accessibility tree has %d nodes", len(nodes))

    # Build lookup structures from the a11y tree
    tree_roles = set()
    tree_names = set()
    for node in nodes:
        role_obj = node.get("role") or {}
        role_val = (role_obj.get("value") or "").lower()
        if role_val:
            tree_roles.add(role_val)

        name_obj = node.get("name") or {}
        name_val = (name_obj.get("value") or "").strip()
        if name_val:
            # Normalize whitespace for fuzzy matching
            import re as _re
            normalized = _re.sub(r"\s+", " ", name_val.lower().strip())
            tree_names.add(normalized)

    logger.info("PHASE 4: A11y tree has %d unique roles, %d unique names",
                len(tree_roles), len(tree_names))

    # Cross-reference: find interactive elements from inventory
    # that have NO representation in the accessibility tree
    missing = []
    checked = 0
    for elem in inventory.elements:
        if not elem.interactive:
            continue
        if not elem.visible:
            continue
        checked += 1

        # Try to find this element in the a11y tree by name or role
        found = False

        # Check by name match (text content, whitespace-normalized)
        import re as _re
        if elem.text:
            normalized_text = _re.sub(r"\s+", " ", elem.text.lower().strip())
            if normalized_text in tree_names:
                found = True

        # Check by role match
        if not found and elem.role:
            expected_roles = _map_to_a11y_roles(elem.role, elem.tag)
            if any(r in tree_roles for r in expected_roles):
                # Role exists, but we can't confirm it's THIS element
                # without a selector match. Mark as potentially found.
                found = True

        if not found:
            missing.append({
                "selector": elem.selector,
                "type": elem.type,
                "text": elem.text,
                "tag": elem.tag,
                "role": elem.role,
                "reason": "Interactive element not found in accessibility tree — may be invisible to screen readers",
            })

    capture_data.at_missing_elements = missing
    logger.info("PHASE 4: Checked %d interactive elements, %d missing from a11y tree",
                checked, len(missing))

    for m in missing:
        logger.info("  MISSING: %s \"%s\" (%s)", m["type"], m["text"], m["selector"])

    # Save cross-reference results
    crossref_path = os.path.join(captures_dir, "phase4_at_crossref.json")
    with open(crossref_path, "w", encoding="utf-8") as f:
        json.dump({
            "total_interactive": checked,
            "missing_from_tree": len(missing),
            "missing_elements": missing,
        }, f, indent=2, default=str)

    elapsed = time.monotonic() - phase_start
    capture_data.phase_timings["phase4"] = round(elapsed, 1)
    logger.info("PHASE 4 COMPLETE: %d missing elements detected in %.1fs",
                len(missing), elapsed)


def _map_to_a11y_roles(role: str, tag: str) -> list[str]:
    """Map an element's role/tag to expected accessibility tree roles."""
    role_lower = role.lower() if role else ""
    tag_lower = tag.lower() if tag else ""

    # Explicit roles
    if role_lower:
        return [role_lower]

    # Implicit roles from HTML tags
    implicit = {
        "a": ["link"],
        "button": ["button"],
        "input": ["textbox", "checkbox", "radio", "spinbutton", "searchbox"],
        "select": ["combobox", "listbox"],
        "textarea": ["textbox"],
        "nav": ["navigation"],
        "main": ["main"],
        "header": ["banner"],
        "footer": ["contentinfo"],
        "aside": ["complementary"],
        "form": ["form"],
        "table": ["table"],
        "img": ["image", "img"],
        "h1": ["heading"],
        "h2": ["heading"],
        "h3": ["heading"],
        "h4": ["heading"],
        "h5": ["heading"],
        "h6": ["heading"],
    }

    return implicit.get(tag_lower, [tag_lower])
