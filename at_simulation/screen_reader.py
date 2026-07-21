"""Screen reader simulation — walks the accessibility tree like JAWS/NVDA.

Traverses the a11y tree in reading order, generates the announcement
for each node, and detects issues that would affect screen reader users.
Maps findings to specific WCAG criteria.
"""
from __future__ import annotations

import logging
from typing import Any

from at_simulation.announcements import (
    _INTERACTIVE_ROLES,
    _get_name,
    _get_properties,
    _get_role,
    render_announcement_issues,
)

logger = logging.getLogger(__name__)

# Maps WCAG criterion IDs to the AT checks that are relevant.
# Each criterion only runs the checks that apply to it.
_CRITERION_AT_CHECKS: dict[str, list[str]] = {
    "1.1.1": ["image_names", "decorative_images"],
    "1.3.1": ["heading_structure", "table_structure", "form_labels",
              "landmark_structure", "list_structure", "aria_describedby_targets"],
    "1.3.2": ["reading_order", "reading_order_validation"],
    "1.4.1": ["color_only_info"],
    "2.4.1": ["landmark_navigation"],
    "2.4.2": ["page_title"],
    "2.4.4": ["link_purpose"],
    "2.4.6": ["heading_labels", "form_labels"],
    "2.4.7": ["focus_visibility", "focus_contrast"],
    # SC 2.4.11 is "Focus Not Obscured (Minimum)" — about whether a
    # focused element is visually hidden behind sticky headers,
    # overlays, or other always-on content. It is NOT about whether
    # a focus indicator is visible (that's 2.4.7). Previously this
    # entry pointed at ``focus_contrast`` which produced 2.4.7-style
    # findings, then shipped them under 2.4.11's bucket -- a
    # cross-SC mislabel observed on a university-site run 2026-04-23 (34 findings
    # showed up under 2.4.11 that should have been under 2.4.7).
    # The correct 2.4.11 check compares focused-element rects against
    # sticky/fixed-position overlay rects; implemented in
    # ``_check_focus_obscured``.
    "2.4.11": ["focus_obscured"],
    "2.5.3": ["label_in_name"],
    "3.2.3": ["navigation_consistency"],
    "3.2.4": ["identification_consistency"],
    "2.4.8": ["aria_current_navigation"],
    "3.3.1": ["error_identification", "aria_invalid_errormessage"],
    "3.3.2": ["form_labels"],
    "3.3.3": ["error_quality"],
    "4.1.2": ["name_role_value", "combobox_pattern"],
    "4.1.3": ["live_regions"],
}


# ---------------------------------------------------------------------------
# Helpers (defined before the check-module re-imports so the moved
# _check_* helpers can pull `_describe_node` back from this module
# without triggering a circular-import error at load time).
# ---------------------------------------------------------------------------

def _describe_node(node: dict[str, Any]) -> str:
    """Build a human-readable description of an a11y tree node."""
    role = _get_role(node)
    name = _get_name(node)
    props = _get_properties(node)

    parts = []
    if role:
        parts.append(f"[{role}]")
    if name:
        parts.append(f'"{name}"')

    # Include key identifying properties
    level = props.get("level", "")
    if level:
        parts.append(f"level={level}")

    # Node ID from the a11y tree (for cross-referencing)
    node_id = node.get("nodeId", node.get("backendDOMNodeId", ""))
    if node_id:
        parts.append(f"(nodeId={node_id})")

    return " ".join(parts) if parts else "(unnamed node)"


def _is_issue_relevant(issue: dict, criterion_id: str) -> bool:
    """Check if an announcement issue is relevant to the current criterion.

    Only returns True for criteria where general announcement issues
    (missing names, meaningless names) are directly relevant.
    For criteria not in the map, returns False — those criteria have
    their own specific AT checks registered in _CRITERION_AT_CHECKS.
    """
    role = issue.get("role", "")

    # Strict map: only include announcement issues for criteria where
    # the element type is directly relevant to that criterion.
    if criterion_id == "1.1.1":
        return role in ("img", "image")
    elif criterion_id == "1.3.1":
        return role in ("heading", "textbox", "combobox", "listbox",
                        "checkbox", "radio", "switch", "table")
    elif criterion_id == "2.4.4":
        return role == "link"
    elif criterion_id == "2.4.6":
        return role == "heading"
    elif criterion_id == "3.3.2":
        return role in ("textbox", "combobox", "listbox", "spinbutton",
                        "checkbox", "radio", "switch")
    elif criterion_id == "4.1.2":
        return role in _INTERACTIVE_ROLES
    # For all other criteria, do NOT include general announcement issues.
    # They have their own specific checks in _CRITERION_AT_CHECKS.
    return False


def _get_recommendation(issue: dict, criterion_id: str) -> str:
    """Get a WCAG-specific recommendation for the issue."""
    role = issue.get("role", "")
    _CRITERION_RECS = {
        "1.1.1": "WCAG 1.1.1 requires non-text content to have a text "
                 "alternative that serves the equivalent purpose.",
        "1.3.1": "WCAG 1.3.1 requires information and relationships conveyed "
                 "through presentation to be programmatically determinable.",
        "2.4.4": "WCAG 2.4.4 requires the purpose of each link to be "
                 "determinable from the link text alone or from context.",
        "2.4.6": "WCAG 2.4.6 requires headings and labels to describe "
                 "the topic or purpose.",
        "3.3.2": "WCAG 3.3.2 requires labels or instructions when content "
                 "requires user input.",
        "4.1.2": "WCAG 4.1.2 requires user interface components to expose "
                 "their name, role, and value to assistive technologies.",
    }
    return _CRITERION_RECS.get(criterion_id, issue.get("impact", ""))


# ---------------------------------------------------------------------------
# Re-import the per-domain _check_* helpers so that bare-name calls
# inside simulate_screen_reader (via _CHECK_REGISTRY) resolve, and so
# external callers that historically reached for these names through
# screen_reader still find them.
# ---------------------------------------------------------------------------

from at_simulation.checks_images import (  # noqa: E402, F401
    _check_decorative_images,
    _check_image_names,
)
from at_simulation.checks_focus import (  # noqa: E402, F401
    _check_focus_contrast,
    _check_focus_obscured,
    _check_focus_visibility,
)
from at_simulation.checks_semantics import (  # noqa: E402, F401
    _check_aria_current_navigation,
    _check_aria_describedby_targets,
    _check_aria_invalid_errormessage,
    _check_color_only_info,
    _check_combobox_pattern,
    _check_error_identification,
    _check_error_quality,
    _check_form_labels,
    _check_heading_labels,
    _check_heading_structure,
    _check_identification_consistency,
    _check_label_in_name,
    _check_landmark_navigation,
    _check_landmark_structure,
    _check_link_purpose,
    _check_list_structure,
    _check_live_regions,
    _check_name_role_value,
    _check_navigation_consistency,
    _check_page_title,
    _check_reading_order,
    _check_reading_order_validation,
    _check_table_structure,
)


_CHECK_REGISTRY: dict[str, Any] = {
    "image_names": _check_image_names,
    "decorative_images": _check_decorative_images,
    "heading_structure": _check_heading_structure,
    "table_structure": _check_table_structure,
    "form_labels": _check_form_labels,
    "landmark_structure": _check_landmark_structure,
    "landmark_navigation": _check_landmark_navigation,
    "link_purpose": _check_link_purpose,
    "name_role_value": _check_name_role_value,
    "list_structure": _check_list_structure,
    "reading_order": _check_reading_order,
    "reading_order_validation": _check_reading_order_validation,
    "page_title": _check_page_title,
    "heading_labels": _check_heading_labels,
    "error_identification": _check_error_identification,
    "focus_visibility": _check_focus_visibility,
    "label_in_name": _check_label_in_name,
    "color_only_info": _check_color_only_info,
    "focus_contrast": _check_focus_contrast,
    "focus_obscured": _check_focus_obscured,
    "live_regions": _check_live_regions,
    "error_quality": _check_error_quality,
    "navigation_consistency": _check_navigation_consistency,
    "identification_consistency": _check_identification_consistency,
    "aria_describedby_targets": _check_aria_describedby_targets,
    "aria_invalid_errormessage": _check_aria_invalid_errormessage,
    "aria_current_navigation": _check_aria_current_navigation,
    "combobox_pattern": _check_combobox_pattern,
}


def simulate_screen_reader(
    a11y_tree: dict[str, Any],
    criterion_id: str,
    capture_data: Any = None,
) -> tuple[str, float, list[dict]]:
    """Simulate a screen reader walkthrough of the page.

    Args:
        a11y_tree: The CDP accessibility tree (from Accessibility.getFullAXTree).
        criterion_id: The WCAG criterion being tested.
        capture_data: Optional CaptureData for supplementary info.

    Returns:
        (conformance_level_str, confidence, findings_list)
    """
    nodes = a11y_tree.get("nodes", [])
    if not nodes:
        return "Not Evaluated", 0.0, []

    # Determine which AT checks to run for this criterion
    checks_to_run = _CRITERION_AT_CHECKS.get(criterion_id, [])
    if not checks_to_run:
        # Not a criterion that benefits from AT simulation
        return "Not Evaluated", 0.0, []

    findings: list[dict] = []

    # Run each applicable check
    for check_name in checks_to_run:
        check_func = _CHECK_REGISTRY.get(check_name)
        if check_func:
            try:
                check_findings = check_func(nodes, capture_data)
                findings.extend(check_findings)
            except Exception as exc:
                logger.debug("AT check %s failed: %s", check_name, exc)

    # Safety net against cross-SC mislabels. If an individual check
    # happens to produce a finding whose issue or recommendation text
    # NAMES a DIFFERENT WCAG criterion (e.g. _check_focus_contrast
    # writes "WCAG 2.4.7 requires..."), drop it from THIS criterion's
    # bucket. The alternative -- shipping a 2.4.7-worded finding
    # under SC 2.4.11 because that's where the check was routed --
    # produced 34 miscategorised findings on a university-site run
    # 2026-04-23. This filter is a belt-and-braces check on top of
    # the fixed _CRITERION_AT_CHECKS mapping.
    import re as _re
    sc_pattern = _re.compile(r"\b(?:WCAG|SC)\s*([0-9]+\.[0-9]+\.[0-9]+)\b", _re.IGNORECASE)
    filtered: list[dict] = []
    for f in findings:
        issue_text = str(f.get("issue", "")) + " " + str(f.get("recommendation", ""))
        cited = set(sc_pattern.findall(issue_text))
        if cited and criterion_id not in cited:
            logger.info(
                "AT sim: dropping finding for SC %s whose text cites "
                "other criteria %s (selector=%s). Belongs under those "
                "SCs instead.",
                criterion_id, sorted(cited),
                f.get("element") or "",
            )
            continue
        filtered.append(f)
    findings = filtered

    # Also run the general announcement quality check on all nodes
    for node in nodes:
        for issue in render_announcement_issues(node):
            # Only include issues relevant to the current criterion
            if _is_issue_relevant(issue, criterion_id):
                findings.append({
                    "element": _describe_node(node),
                    "issue": issue["issue"],
                    "impact": issue["impact"],
                    "severity": issue["severity"],
                    "recommendation": _get_recommendation(issue, criterion_id),
                })

    # Determine conformance
    high_count = sum(1 for f in findings if f.get("severity") == "high")
    med_count = sum(1 for f in findings if f.get("severity") == "medium")

    if high_count >= 3:
        conformance = "Does Not Support"
    elif high_count > 0:
        conformance = "Partially Supports"
    elif med_count > 0:
        conformance = "Partially Supports"
    elif findings:
        conformance = "Supports"  # Only low/info findings
    else:
        conformance = "Supports"

    # Confidence is high because we're reading the actual a11y tree
    confidence = min(0.90, 0.75 + len(nodes) * 0.001)

    return conformance, confidence, findings
