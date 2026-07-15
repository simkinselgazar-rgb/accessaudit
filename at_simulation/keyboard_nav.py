"""Keyboard navigation simulation for screen readers.

Simulates the quick-navigation keys that JAWS/NVDA users rely on:
  H — jump between headings
  T — jump between tables
  F — jump between form fields
  K/L — jump between links
  D — jump between landmarks
  Tab — standard tab navigation

Compares these navigation paths against the a11y tree to find
elements that would be skipped or unreachable.
"""
from __future__ import annotations

import logging
from typing import Any

from at_simulation.announcements import (
    _INTERACTIVE_ROLES,
    _LANDMARK_ROLES,
    _get_name,
    _get_properties,
    _get_role,
    render_announcement,
)

logger = logging.getLogger(__name__)


def simulate_heading_navigation(
    nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Simulate pressing 'H' key to navigate between headings.

    Returns a summary of the heading navigation experience, including
    the sequence of announcements and any issues found.
    """
    headings: list[dict] = []
    for node in nodes:
        if _get_role(node) != "heading":
            continue
        props = _get_properties(node)
        name = _get_name(node)
        level = props.get("level", 0)
        try:
            level = int(level)
        except (ValueError, TypeError):
            level = 0
        headings.append({
            "level": level,
            "name": name,
            "announcement": render_announcement(node),
            "node": node,
        })

    return {
        "total_headings": len(headings),
        "sequence": [h["announcement"] for h in headings],
        "levels": [h["level"] for h in headings],
        "has_h1": any(h["level"] == 1 for h in headings),
        "empty_headings": sum(1 for h in headings if not h["name"]),
    }


def simulate_form_navigation(
    nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Simulate pressing 'F' key to navigate between form fields.

    Returns the sequence of form field announcements and issues.
    """
    form_roles = {"textbox", "combobox", "listbox", "spinbutton",
                  "checkbox", "radio", "switch", "searchbox", "slider"}
    fields: list[dict] = []
    for node in nodes:
        role = _get_role(node)
        if role not in form_roles:
            continue
        props = _get_properties(node)
        if props.get("hidden") in (True, "true"):
            continue
        name = _get_name(node)
        fields.append({
            "role": role,
            "name": name,
            "announcement": render_announcement(node),
            "has_label": bool(name),
            "is_required": props.get("required") in ("true", True),
            "node": node,
        })

    unlabeled = sum(1 for f in fields if not f["has_label"])

    return {
        "total_fields": len(fields),
        "sequence": [f["announcement"] for f in fields],
        "unlabeled_count": unlabeled,
        "required_count": sum(1 for f in fields if f["is_required"]),
    }


def simulate_landmark_navigation(
    nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Simulate pressing 'D' key to navigate between landmarks.

    Returns the sequence of landmark announcements and issues.
    """
    landmarks: list[dict] = []
    for node in nodes:
        role = _get_role(node)
        if role not in _LANDMARK_ROLES:
            continue
        name = _get_name(node)
        landmarks.append({
            "role": role,
            "name": name,
            "announcement": render_announcement(node),
            "node": node,
        })

    # Count landmarks by type
    type_counts: dict[str, int] = {}
    for lm in landmarks:
        r = lm["role"]
        type_counts[r] = type_counts.get(r, 0) + 1

    # Find unlabeled duplicates
    unlabeled_duplicates = []
    for lm in landmarks:
        if type_counts.get(lm["role"], 0) > 1 and not lm["name"]:
            unlabeled_duplicates.append(lm["role"])

    return {
        "total_landmarks": len(landmarks),
        "sequence": [lm["announcement"] for lm in landmarks],
        "type_counts": type_counts,
        "has_main": type_counts.get("main", 0) > 0,
        "has_navigation": type_counts.get("navigation", 0) > 0,
        "unlabeled_duplicates": unlabeled_duplicates,
    }


def simulate_link_navigation(
    nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Simulate pulling up the JAWS links list (Insert+F7).

    Returns what a user would see in the links list dialog.
    """
    links: list[dict] = []
    for node in nodes:
        if _get_role(node) != "link":
            continue
        name = _get_name(node)
        props = _get_properties(node)
        if props.get("hidden") in (True, "true"):
            continue
        links.append({
            "name": name,
            "announcement": render_announcement(node),
            "node": node,
        })

    # Check for ambiguous link names (same text, different links)
    name_counts: dict[str, int] = {}
    for link in links:
        n = (link["name"] or "").strip().lower()
        if n:
            name_counts[n] = name_counts.get(n, 0) + 1

    ambiguous = {n: c for n, c in name_counts.items() if c > 1}
    unnamed = sum(1 for l in links if not l["name"])

    return {
        "total_links": len(links),
        "links_list": [l["name"] or "(no text)" for l in links],
        "unnamed_count": unnamed,
        "ambiguous_names": ambiguous,
    }


def simulate_table_navigation(
    nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Simulate pressing 'T' key to navigate between tables.

    Returns table accessibility info as a screen reader user experiences it.
    """
    tables: list[dict] = []
    for node in nodes:
        if _get_role(node) != "table":
            continue
        name = _get_name(node)
        props = _get_properties(node)
        tables.append({
            "name": name,
            "announcement": render_announcement(node),
            "has_name": bool(name),
            "node": node,
        })

    # Count headers in the tree (columnheader, rowheader)
    header_count = sum(
        1 for n in nodes
        if _get_role(n) in ("columnheader", "rowheader")
    )

    return {
        "total_tables": len(tables),
        "sequence": [t["announcement"] for t in tables],
        "unnamed_tables": sum(1 for t in tables if not t["has_name"]),
        "total_headers": header_count,
    }


def simulate_tab_order_comparison(
    nodes: list[dict[str, Any]],
    tab_walk: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare tab navigation with the a11y tree's interactive elements.

    Finds interactive elements in the a11y tree that were NOT reached
    during the tab walk — meaning keyboard users can't access them.
    """
    if not tab_walk:
        return {"skipped": [], "total_interactive": 0, "total_tabbed": 0}

    # Collect selectors/names reached by tab
    tabbed_selectors: set[str] = set()
    tabbed_texts: set[str] = set()
    for step in tab_walk:
        sel = step.get("selector", "")
        text = step.get("text", "").strip().lower()
        if sel:
            tabbed_selectors.add(sel.lower())
        if text:
            tabbed_texts.add(text)

    # Find interactive nodes in a11y tree
    interactive_nodes: list[dict] = []
    for node in nodes:
        role = _get_role(node)
        if role not in _INTERACTIVE_ROLES:
            continue
        props = _get_properties(node)
        if props.get("hidden") in (True, "true"):
            continue
        if props.get("disabled") in (True, "true"):
            continue
        name = _get_name(node)
        interactive_nodes.append({
            "role": role,
            "name": name,
            "announcement": render_announcement(node),
            "node": node,
        })

    # Check which interactive nodes weren't reached by tab
    skipped = []
    for inode in interactive_nodes:
        name = (inode["name"] or "").strip().lower()
        # Heuristic match: check if name appears in tabbed texts
        found = name and name in tabbed_texts
        if not found:
            skipped.append({
                "role": inode["role"],
                "name": inode["name"],
                "announcement": inode["announcement"],
            })

    return {
        "total_interactive": len(interactive_nodes),
        "total_tabbed": len(tab_walk),
        "skipped_count": len(skipped),
        "skipped": skipped,
    }
