"""Screen-reader AT checks for semantic-structure WCAG criteria.

Each helper takes the a11y-tree ``nodes`` list plus the optional
``capture_data`` snapshot and returns a list of finding dicts. The
checks here cover WCAG criteria whose AT-side signal is structural —
heading hierarchy (1.3.1, 2.4.6), landmark organisation (1.3.1, 2.4.1),
list / table semantics (1.3.1), reading order (1.3.2), form labels
(1.3.1, 3.3.2), name-role-value (4.1.2), live regions (4.1.3), error
identification (3.3.1, 3.3.3), label-in-name (2.5.3), and the various
aria-* idref / state validations.
"""
from __future__ import annotations

from typing import Any

from at_simulation.announcements import (
    _INTERACTIVE_ROLES,
    _LANDMARK_ROLES,
    _SILENT_ROLES,
    _get_name,
    _get_properties,
    _get_role,
    is_meaningless_name,
    render_announcement,
)
from at_simulation.screen_reader import _describe_node
from functions.aria_validator import (
    validate_id_references,
    validate_role_required_attributes,
    validate_role_usage,
)


def _check_heading_structure(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check heading hierarchy from the a11y tree perspective."""
    findings = []
    headings: list[tuple[int, str, dict]] = []

    for node in nodes:
        role = _get_role(node)
        if role != "heading":
            continue
        props = _get_properties(node)
        level = props.get("level", 0)
        name = _get_name(node)
        try:
            level = int(level)
        except (ValueError, TypeError):
            level = 0
        if level > 0:
            headings.append((level, name, node))

    if not headings:
        return findings

    # Check for missing h1
    levels = [h[0] for h in headings]
    if 1 not in levels:
        findings.append({
            "element": "<page>",
            "issue": "No heading level 1 found. Screen reader users navigating "
                     "by headings cannot find the page's main topic.",
            "impact": "Users who navigate by headings (H key in JAWS/NVDA) "
                      "expect an h1 to identify the page's primary content.",
            "severity": "medium",
            "recommendation": "WCAG 1.3.1 requires information structure to be "
                              "programmatically determinable. Add an h1 for the "
                              "page's main topic.",
        })

    # Check for empty headings
    for level, name, node in headings:
        if not name or not name.strip():
            findings.append({
                "element": _describe_node(node),
                "issue": f"Empty heading level {level}. Screen readers announce "
                         f"'heading level {level}' with no text.",
                "impact": "Screen reader users navigating by headings encounter "
                          "a blank heading, disrupting page structure comprehension.",
                "severity": "medium",
                "recommendation": "Headings must have descriptive text content.",
            })

    return findings


def _check_table_structure(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check table accessibility from the a11y tree."""
    findings = []
    for node in nodes:
        role = _get_role(node)
        if role != "table":
            continue
        name = _get_name(node)
        if not name:
            findings.append({
                "element": _describe_node(node),
                "issue": "Data table has no accessible name (caption or aria-label). "
                         "Screen readers announce 'table' with no description.",
                "impact": "Screen reader users cannot identify the table's purpose "
                          "before navigating into it.",
                "severity": "medium",
                "recommendation": "WCAG 1.3.1 requires data tables to have a caption "
                                  "or aria-label identifying their content.",
            })
    return findings


def _check_form_labels(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check all form fields have accessible labels."""
    findings = []
    form_roles = {"textbox", "combobox", "listbox", "spinbutton",
                  "checkbox", "radio", "switch", "searchbox", "slider"}
    for node in nodes:
        role = _get_role(node)
        if role not in form_roles:
            continue
        name = _get_name(node)
        props = _get_properties(node)

        if props.get("hidden") in (True, "true"):
            continue

        if not name:
            announcement = render_announcement(node)
            findings.append({
                "element": _describe_node(node),
                "issue": f"Form field has no accessible label. Screen readers "
                         f"announce: '{announcement}' — users cannot determine "
                         f"what information to enter.",
                "impact": "Screen reader users in forms mode hear the field type "
                          "but no label, making the form unusable without sighted help.",
                "severity": "high",
                "recommendation": "WCAG 1.3.1/3.3.2 require form fields to have "
                                  "programmatically associated labels.",
            })
        elif is_meaningless_name(name):
            findings.append({
                "element": _describe_node(node),
                "issue": f"Form field label '{name}' is not descriptive. "
                         f"Screen readers announce: '{render_announcement(node)}'.",
                "impact": "Screen reader users cannot determine the field's purpose "
                          "from the generic label.",
                "severity": "medium",
                "recommendation": "Form labels should clearly describe what "
                                  "information is expected.",
            })
    return findings


def _check_landmark_structure(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check landmark regions for proper labeling."""
    findings = []
    landmark_counts: dict[str, int] = {}

    for node in nodes:
        role = _get_role(node)
        if role not in _LANDMARK_ROLES:
            continue
        landmark_counts[role] = landmark_counts.get(role, 0) + 1

    # Check for multiple landmarks of same type without labels
    for node in nodes:
        role = _get_role(node)
        if role not in _LANDMARK_ROLES:
            continue
        name = _get_name(node)
        count = landmark_counts.get(role, 0)

        if count > 1 and not name:
            findings.append({
                "element": _describe_node(node),
                "issue": f"Multiple '{role}' landmarks exist but this one has "
                         f"no accessible label. Screen reader users navigating "
                         f"by landmarks cannot distinguish between them.",
                "impact": f"When there are {count} '{role}' regions, screen "
                          f"reader users need unique labels to navigate efficiently.",
                "severity": "medium",
                "recommendation": "When multiple landmarks of the same type exist, "
                                  "each should have a unique aria-label.",
            })
    return findings


def _check_landmark_navigation(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check that landmark navigation provides bypass to main content."""
    findings = []
    has_main = False
    has_nav = False
    has_banner = False

    for node in nodes:
        role = _get_role(node)
        if role == "main":
            has_main = True
        elif role == "navigation":
            has_nav = True
        elif role == "banner":
            has_banner = True

    if not has_main:
        findings.append({
            "element": "<page>",
            "issue": "No 'main' landmark region found. Screen reader users "
                     "cannot jump directly to the primary content.",
            "impact": "Screen reader users must tab through all navigation "
                      "elements to reach the main content area.",
            "severity": "medium",
            "recommendation": "WCAG 2.4.1 requires a mechanism to bypass "
                              "repeated blocks. A main landmark is the standard approach.",
        })

    return findings


def _check_link_purpose(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check that links have descriptive accessible names."""
    findings = []
    generic_link_names = {
        "click here", "here", "read more", "more", "learn more",
        "link", "this", "go", "details", "continue",
    }
    for node in nodes:
        role = _get_role(node)
        if role != "link":
            continue
        name = _get_name(node)
        if not name:
            findings.append({
                "element": _describe_node(node),
                "issue": "Link has no accessible name. Screen readers announce "
                         "'link' with no description of the destination.",
                "impact": "Screen reader users cannot determine where this link "
                          "leads without visual context.",
                "severity": "high",
                "recommendation": "WCAG 2.4.4 requires link text to describe the "
                                  "link's purpose, either from the link text alone "
                                  "or from context.",
            })
        elif name.strip().lower() in generic_link_names:
            findings.append({
                "element": _describe_node(node),
                "issue": f"Link text '{name}' is generic. Screen readers announce: "
                         f"'{render_announcement(node)}'. Users navigating by links "
                         f"(Insert+F7 in JAWS) see a list of '{name}' links.",
                "impact": "Screen reader users who pull up the links list see "
                          "multiple entries all saying the same generic text, "
                          "making it impossible to distinguish destinations.",
                "severity": "medium",
                "recommendation": "WCAG 2.4.4 requires link purpose to be determinable "
                                  "from the link text.",
            })
    return findings


def _check_name_role_value(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check that all interactive elements expose name, role, and value."""
    findings = []
    for node in nodes:
        role = _get_role(node)
        if role not in _INTERACTIVE_ROLES:
            continue
        name = _get_name(node)
        props = _get_properties(node)

        if props.get("hidden") in (True, "true"):
            continue

        # Check: does the node have a proper role?
        if not role or role in _SILENT_ROLES:
            findings.append({
                "element": _describe_node(node),
                "issue": "Interactive element has no ARIA role. Screen readers "
                         "may not announce it as an interactive control.",
                "impact": "Screen reader users may not realize this element is "
                          "interactive, preventing them from activating it.",
                "severity": "high",
                "recommendation": "WCAG 4.1.2 requires all user interface components "
                                  "to expose their name, role, and value to AT.",
            })

        # Check: does it have a name?
        if not name:
            announcement = render_announcement(node)
            findings.append({
                "element": _describe_node(node),
                "issue": f"Interactive {role} has no accessible name. "
                         f"Screen readers announce: '{announcement}'.",
                "impact": "Screen reader users cannot identify this control's purpose.",
                "severity": "high",
                "recommendation": "WCAG 4.1.2 requires interactive elements to have "
                                  "an accessible name.",
            })

    # Augment with deterministic ARIA spec validation over every captured
    # element — including shadow DOM content that may not reach the a11y
    # tree. functions.aria_validator carries the WAI-ARIA role rules
    # (required attributes per role, conflicts like aria-hidden on focusable
    # elements, redundant implicit roles, etc.).
    captured_elements: list[dict] = []
    if capture_data is not None:
        for field_name in ("form_fields", "links", "images", "shadow_elements"):
            value = getattr(capture_data, field_name, None) or []
            if isinstance(value, list):
                captured_elements.extend(value)

    seen_nrv: set[tuple[str, str]] = set()

    def _record_aria_issue(issue: dict) -> None:
        sel = issue.get("element_selector", "element")
        msg = issue.get("issue", "")
        key = (sel, msg)
        if key in seen_nrv:
            return
        seen_nrv.add(key)
        findings.append({
            "element": sel,
            "issue": msg,
            "impact": "Screen readers cannot fully convey this component's "
                      "role, state, or value to assistive technology users.",
            "severity": issue.get("severity", "high"),
            "recommendation": "WCAG 4.1.2 requires user interface components "
                              "to expose name, role, and value to AT.",
        })

    if captured_elements:
        for issue in validate_role_required_attributes(captured_elements):
            _record_aria_issue(issue)
        for issue in validate_role_usage(captured_elements):
            _record_aria_issue(issue)

    return findings


def _check_list_structure(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check list semantics from the a11y tree.

    A list node with zero listitem descendants is either empty-by-mistake or
    mis-structured (a div pretending to be a list). A listitem appearing
    outside a list container is orphaned and will not be announced as part
    of a list.
    """
    findings: list[dict] = []

    # Build parent map by walking children, since a11y tree nodes may not
    # carry explicit parent pointers.
    parent_of: dict[int, dict] = {}
    for node in nodes:
        for child in node.get("children", []) or []:
            cid = id(child)
            parent_of[cid] = node

    for node in nodes:
        role = _get_role(node)
        if role in ("list", "directory"):
            listitem_count = sum(
                1 for child in (node.get("children") or [])
                if _get_role(child) == "listitem"
            )
            if listitem_count == 0:
                findings.append({
                    "element": _describe_node(node),
                    "issue": f"List has no listitem children. Screen readers "
                             f"announce: '{render_announcement(node)}' with no items.",
                    "impact": "Screen reader users hear a list with zero items, "
                              "or the list is structured with non-listitem elements "
                              "that will not be announced as list content.",
                    "severity": "medium",
                    "recommendation": "WCAG 1.3.1 requires list semantics to reflect "
                                      "the visual list. Use <li> elements inside "
                                      "<ul>/<ol>, or role='listitem' inside "
                                      "role='list'.",
                })
        elif role == "listitem":
            parent = parent_of.get(id(node))
            parent_role = _get_role(parent) if parent else ""
            if parent_role not in ("list", "directory"):
                findings.append({
                    "element": _describe_node(node),
                    "issue": f"Listitem appears outside of a list container "
                             f"(parent role='{parent_role or 'unknown'}'). Screen "
                             f"readers will not announce this item as part of a list.",
                    "impact": "The list/item relationship is broken. Assistive "
                              "technology users lose navigation by list and list-count "
                              "announcements.",
                    "severity": "medium",
                    "recommendation": "WCAG 1.3.1 requires list items to be contained "
                                      "within a list element so the relationship is "
                                      "exposed to assistive technology.",
                })
    return findings


def _check_reading_order(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check if reading order from a11y tree is logical (SC 1.3.2)."""
    findings = []
    if not capture_data:
        return findings

    # Compare heading order in a11y tree vs capture data
    tree_headings = []
    for node in nodes:
        if _get_role(node) == "heading":
            tree_headings.append(_get_name(node))

    capture_headings = [h.get("text", "") for h in getattr(capture_data, 'headings', [])]

    # If headings appear in different order, flag it
    if tree_headings and capture_headings:
        # Check for significant ordering differences
        # (minor reordering from CSS can be legitimate)
        if len(tree_headings) >= 2 and len(capture_headings) >= 2:
            if tree_headings[0] != capture_headings[0]:
                findings.append({
                    "element": "<page>",
                    "issue": f"First heading in accessibility tree ('{tree_headings[0]}') "
                             f"differs from first heading in DOM order ('{capture_headings[0]}'). "
                             f"CSS positioning may be altering the visual reading order.",
                    "impact": "Screen reader users experience content in a different "
                              "order than visual users, which may cause confusion.",
                    "severity": "medium",
                    "recommendation": "WCAG 1.3.2 requires meaningful reading sequence "
                                      "to be programmatically determinable.",
                })

    # Check tab order vs DOM order using tab_walk data
    tab_walk = getattr(capture_data, 'tab_walk', [])
    if tab_walk and len(tab_walk) >= 3:
        # Check for tabindex > 0 which disrupts natural order
        for step in tab_walk:
            tabindex = step.get("tabindex", 0)
            if isinstance(tabindex, int) and tabindex > 0:
                findings.append({
                    "element": step.get("selector", "(unknown)"),
                    "issue": f"Element has tabindex={tabindex} which overrides natural "
                             f"tab order. This can confuse keyboard users.",
                    "impact": "Keyboard users encounter this element out of its "
                              "visual position, disrupting expected navigation flow.",
                    "severity": "low",
                    "recommendation": "Avoid positive tabindex values. Use tabindex='0' "
                                      "and rely on DOM order for tab sequence.",
                })
                break  # Only report once

    return findings


def _check_page_title(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check page has a meaningful title (announced on page load).

    The title is the first thing a screen reader announces on every new page
    load. Missing, default, or non-descriptive titles leave AT users unable
    to identify the page they just navigated to.
    """
    findings: list[dict] = []
    if capture_data is None:
        return findings

    title = (getattr(capture_data, "title", None) or "").strip()
    if not title:
        findings.append({
            "element": "<title>",
            "issue": "Page has no title. Screen readers announce the URL or "
                     "nothing when the page loads.",
            "impact": "Users cannot identify the page by its announced title, "
                      "and history/bookmark lists show raw URLs instead of "
                      "meaningful names.",
            "severity": "high",
            "recommendation": "WCAG 2.4.2 requires every page to have a <title> "
                              "that describes topic or purpose.",
        })
        return findings

    lowered = title.lower().strip()
    generic_titles = {
        "untitled", "untitled document", "new page", "document",
        "home", "welcome", "index", "page", "title", "default",
    }
    if lowered in generic_titles or lowered.startswith("untitled "):
        findings.append({
            "element": f"<title>{title}</title>",
            "issue": f"Page title '{title}' is generic and does not describe "
                     f"the page topic or purpose.",
            "impact": "Users navigating by title (history, open tabs, screen "
                      "reader announcement) cannot distinguish this page from "
                      "others.",
            "severity": "medium",
            "recommendation": "WCAG 2.4.2 requires titles that describe the "
                              "page's topic or purpose.",
        })

    # URL-ish titles (contain the hostname or look like a path) are also
    # not meaningful to AT users.
    if "/" in title or ("." in title and " " not in title and len(title) > 12):
        findings.append({
            "element": f"<title>{title}</title>",
            "issue": f"Page title '{title}' looks like a URL or path rather "
                     f"than a descriptive title.",
            "impact": "Screen readers read URL-like strings character by character, "
                      "which is unhelpful to users.",
            "severity": "low",
            "recommendation": "WCAG 2.4.2 requires titles that describe the "
                              "page's topic or purpose in prose.",
        })
    return findings


def _check_heading_labels(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check headings are descriptive (not just 'Section 1')."""
    findings = []
    generic_headings = {
        "section", "untitled", "heading", "title", "header",
        "section 1", "section 2", "section 3",
    }
    for node in nodes:
        role = _get_role(node)
        if role != "heading":
            continue
        name = _get_name(node)
        if name and name.strip().lower() in generic_headings:
            props = _get_properties(node)
            level = props.get("level", "?")
            findings.append({
                "element": _describe_node(node),
                "issue": f"Heading level {level} text '{name}' is generic. "
                         f"Screen readers announce: '{render_announcement(node)}'.",
                "impact": "Screen reader users navigating by headings cannot "
                          "determine the section's content from a generic label.",
                "severity": "low",
                "recommendation": "WCAG 2.4.6 requires headings to describe the "
                                  "topic or purpose of the content section.",
            })
    return findings


def _check_error_identification(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check that error states are announced by screen readers."""
    findings = []
    for node in nodes:
        role = _get_role(node)
        props = _get_properties(node)
        invalid = props.get("invalid", "")
        if invalid and invalid not in ("false", False):
            # Has invalid state — check if there's a description
            name = _get_name(node)
            errormessage = props.get("errormessage", "")
            describedby = props.get("describedby", "")
            if not errormessage and not describedby:
                findings.append({
                    "element": _describe_node(node),
                    "issue": f"Form field '{name or role}' has invalid state but "
                             f"no error message linked via aria-errormessage or "
                             f"aria-describedby. Screen readers announce 'invalid entry' "
                             f"but cannot tell the user what is wrong.",
                    "impact": "Screen reader users know something is wrong but "
                              "have no information about how to fix the error.",
                    "severity": "high",
                    "recommendation": "WCAG 3.3.1 requires errors to be identified "
                                      "and described to the user in text.",
                })
    return findings


def _check_label_in_name(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check that accessible names contain the visible label text (SC 2.5.3)."""
    findings = []
    interactive_roles = {"button", "link", "tab", "menuitem", "switch"}
    for node in nodes:
        role = _get_role(node)
        if role not in interactive_roles:
            continue
        name = _get_name(node)
        props = _get_properties(node)
        if props.get("hidden") in (True, "true"):
            continue
        # The "name" from a11y tree is the accessible name.
        # We need to check if the visible text is contained within it.
        # The a11y tree node may have children with StaticText roles
        # that represent the visible text.
        # For now, check if there's a name source mismatch
        name_source = ""
        for p in node.get("properties", []):
            pname = p.get("name", "")
            if pname == "labelledby" or pname == "describedby":
                name_source = "aria"

        # If name comes from aria-label and there's visible text,
        # the aria-label must contain the visible text
        if not name:
            continue
        # Can only reliably check when we have capture_data with links/form_fields
        if not capture_data:
            continue
        # Match node against captured elements by name
        visible_text = ""
        for link in getattr(capture_data, 'links', []):
            link_text = link.get("text", "").strip()
            aria_label = link.get("ariaLabel", "").strip()
            if aria_label and link_text and aria_label != link_text:
                if link_text.lower() not in aria_label.lower():
                    findings.append({
                        "element": f'[link] "{link_text}"',
                        "issue": f"Visible label '{link_text}' is not contained in "
                                 f"accessible name '{aria_label}'. Voice control users "
                                 f"saying '{link_text}' cannot activate this control.",
                        "impact": "Voice control users (Dragon NaturallySpeaking) who "
                                  "speak the visible label text cannot activate this control "
                                  "because the accessible name doesn't match.",
                        "severity": "high",
                        "recommendation": "WCAG 2.5.3 requires the accessible name to "
                                          "contain the visible label text as a substring.",
                    })
        # Check form fields too
        for field in getattr(capture_data, 'form_fields', []):
            label = field.get("label", "").strip()
            aria_label = field.get("ariaLabel", "").strip()
            if aria_label and label and aria_label != label:
                if label.lower() not in aria_label.lower():
                    field_id = field.get("id", field.get("name", "form field"))
                    findings.append({
                        "element": f'[{field.get("tag", "input")}] "{label}" (id={field_id})',
                        "issue": f"Visible label '{label}' is not contained in "
                                 f"accessible name '{aria_label}'.",
                        "impact": "Voice control users who say the visible label "
                                  "cannot activate this form field.",
                        "severity": "high",
                        "recommendation": "WCAG 2.5.3 requires the accessible name to "
                                          "contain the visible label text.",
                    })
        break  # Only run once (not per node)
    return findings


def _check_color_only_info(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Detect patterns where color alone may convey information (SC 1.4.1)."""
    findings = []
    if not capture_data:
        return findings

    # Check for required fields indicated only by color (no asterisk, no "required" text)
    for field in getattr(capture_data, 'form_fields', []):
        is_required = field.get("required", False)
        label = field.get("label", "").strip()
        aria_label = field.get("ariaLabel", "").strip()
        placeholder = field.get("placeholder", "").strip()
        combined_text = f"{label} {aria_label} {placeholder}"

        if is_required and "*" not in combined_text and "required" not in combined_text.lower():
            field_id = field.get("id", field.get("name", "form field"))
            findings.append({
                "element": f'[{field.get("tag", "input")}] "{label or aria_label}" (id={field_id})',
                "issue": "Required field has the HTML required attribute but no "
                         "visible text indicator (no asterisk *, no 'required' text). "
                         "If the required status is indicated only by color, this "
                         "fails SC 1.4.1.",
                "impact": "Color-blind users and screen magnifier users who cannot "
                          "see the color distinction may not realize this field is required.",
                "severity": "medium",
                "recommendation": "WCAG 1.4.1 requires color not be used as the only "
                                  "visual means of conveying information. Add a text "
                                  "indicator like * or '(required)'.",
            })

    # Check for links that have no underline (might rely on color alone)
    for link in getattr(capture_data, 'links', []):
        # This is primarily for AI to verify visually — just flag when
        # links have no distinguishing feature beyond color
        pass  # Leave to visual AI

    return findings


def _check_live_regions(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check ARIA live region configuration (SC 4.1.3)."""
    findings = []
    has_status = False
    has_alert = False

    for node in nodes:
        role = _get_role(node)
        props = _get_properties(node)
        live = props.get("live", "")

        if role == "status" or live == "polite":
            has_status = True
        if role == "alert" or live == "assertive":
            has_alert = True

        # Check for live regions with no content
        if role in ("status", "alert", "log", "timer", "marquee"):
            name = _get_name(node)
            # These are structural — just note their presence

    # Check form errors — if there are form_errors in capture_data,
    # verify there's a live region to announce them
    if capture_data:
        form_errors = getattr(capture_data, 'form_errors', [])
        if form_errors and not has_status and not has_alert:
            findings.append({
                "element": "<page>",
                "issue": "Form validation errors exist but no ARIA live region "
                         "(role='status' or role='alert') is present to announce "
                         "them to screen reader users.",
                "impact": "Screen reader users may not be notified when form "
                          "errors appear after submission. They must manually "
                          "search for error messages.",
                "severity": "medium",
                "recommendation": "WCAG 4.1.3 requires status messages to be "
                                  "programmatically determinable without receiving focus.",
            })

    return findings


def _check_error_quality(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check form error message quality (SC 3.3.3)."""
    findings = []
    if not capture_data:
        return findings

    form_errors = getattr(capture_data, 'form_errors', [])
    for error in form_errors:
        message = error.get("validationMessage", "") or error.get("error_text", "")
        field_name = error.get("name", "") or error.get("id", "")
        field_label = error.get("label", "")

        if not message:
            findings.append({
                "element": f'[form field] "{field_label or field_name}"',
                "issue": "Form field triggers validation error but no error "
                         "message text is provided to the user.",
                "impact": "Screen reader users hear 'invalid entry' but receive "
                          "no guidance about what is wrong or how to fix it.",
                "severity": "high",
                "recommendation": "WCAG 3.3.3 requires error messages that suggest "
                                  "corrections when input errors are detected.",
            })
        elif len(message) < 10:
            findings.append({
                "element": f'[form field] "{field_label or field_name}"',
                "issue": f"Error message '{message}' is too brief to be helpful. "
                         f"It does not describe what is wrong or how to fix it.",
                "impact": "Users may not understand the error or know how to "
                          "correct their input.",
                "severity": "medium",
                "recommendation": "WCAG 3.3.3 requires suggestions for correction "
                                  "when errors are detected.",
            })

    return findings


def _check_reading_order_validation(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Alias for reading_order — runs the same logic (SC 1.3.2)."""
    return _check_reading_order(nodes, capture_data)


def _check_navigation_consistency(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Flag that navigation consistency requires multi-page comparison (SC 3.2.3)."""
    findings = []
    # Count navigation landmarks
    nav_count = sum(1 for n in nodes if _get_role(n) == "navigation")
    if nav_count == 0:
        findings.append({
            "element": "<page>",
            "issue": "No navigation landmark found on this page. If other pages "
                     "in the site have navigation regions, this is inconsistent.",
            "impact": "Screen reader users who rely on landmark navigation "
                      "expect consistent navigation across pages.",
            "severity": "low",
            "recommendation": "WCAG 3.2.3 requires navigation mechanisms that are "
                              "repeated on multiple pages to occur in the same "
                              "relative order each time.",
        })
    return findings


def _check_identification_consistency(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check for consistent identification patterns (SC 3.2.4)."""
    # This is primarily a cross-page check — on a single page,
    # we can check if similar elements have consistent labeling patterns
    findings = []
    # Check form fields: do they use a consistent labeling strategy?
    form_roles = {"textbox", "combobox", "checkbox", "radio"}
    has_label_for = False
    has_placeholder_only = False

    for node in nodes:
        role = _get_role(node)
        if role not in form_roles:
            continue
        props = _get_properties(node)
        if props.get("hidden") in (True, "true"):
            continue
        name = _get_name(node)
        # Can't easily determine the source of the name from the a11y tree
        # Just check if all fields have names (consistency)
        if name:
            has_label_for = True
        else:
            has_placeholder_only = True

    if has_label_for and has_placeholder_only:
        findings.append({
            "element": "<form>",
            "issue": "Form fields use inconsistent labeling — some have accessible "
                     "names while others do not.",
            "impact": "Screen reader users experience inconsistent form interaction "
                      "patterns, which increases cognitive load.",
            "severity": "low",
            "recommendation": "WCAG 3.2.4 requires components with the same "
                              "functionality to be identified consistently.",
        })

    return findings


def _check_aria_describedby_targets(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Validate ARIA idref attributes (aria-describedby, aria-labelledby,
    aria-controls, aria-owns, aria-activedescendant, aria-errormessage,
    aria-flowto, aria-details, and label[for]) all point to existing IDs.

    Combines two data sources so nothing is left behind:
      1. The a11y tree (catches tree-level relationships).
      2. The captured HTML via functions.aria_validator.validate_id_references
         (catches references in the raw markup that the browser may not expose
         in the tree, including references inside shadow DOM content that
         shadow_dom merges into capture_data.html).
    """
    findings: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def _record(element: str, attribute: str, ref_id: str, severity: str) -> None:
        key = (element, attribute, ref_id)
        if key in seen:
            return
        seen.add(key)
        findings.append({
            "element": element,
            "issue": f"{attribute} references ID '{ref_id}' which does not exist in the document.",
            "impact": "Screen readers cannot announce the intended description, label, or relationship.",
            "severity": severity,
            "recommendation": "Ensure the referenced id exists, or remove the stale reference (WCAG 1.3.1, 4.1.2).",
        })

    # Source 1: a11y tree — cheap, catches computed relationships
    tree_ids = {_get_properties(n).get("id", "") for n in nodes} - {""}
    for node in nodes:
        described_by = _get_properties(node).get("describedby", "")
        if not described_by:
            continue
        for ref_id in str(described_by).split():
            ref_id = ref_id.strip()
            if ref_id and ref_id not in tree_ids:
                _record(_describe_node(node), "aria-describedby", ref_id, "medium")

    # Source 2: raw HTML — full ARIA idref coverage across every attribute
    html = getattr(capture_data, "html", "") or ""
    for issue in validate_id_references(html):
        _record(
            issue.get("element_selector", "(unknown)"),
            issue.get("attribute", "aria-*"),
            issue.get("referenced_id", ""),
            issue.get("severity", "high"),
        )

    return findings


def _check_aria_invalid_errormessage(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Validate aria-invalid fields have associated error messages."""
    findings = []
    for node in nodes:
        props = _get_properties(node)
        if props.get("invalid") not in (True, "true", "spelling", "grammar"):
            continue
        if not (props.get("errormessage") or props.get("describedby")):
            findings.append({
                "element": _describe_node(node),
                "issue": "Field marked aria-invalid but has no aria-errormessage.",
                "impact": "Screen readers announce invalid state without explaining the error.",
                "severity": "medium",
                "recommendation": "Add aria-errormessage pointing to error text (WCAG 3.3.1).",
            })
    return findings


def _check_aria_current_navigation(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Check navigation links use aria-current for the active page."""
    findings = []
    has_nav = any(_get_role(n) == "navigation" for n in nodes)
    if not has_nav:
        return findings
    link_count = sum(1 for n in nodes if _get_role(n) == "link")
    has_current = any(_get_properties(n).get("current") for n in nodes if _get_role(n) == "link")
    if link_count > 0 and not has_current:
        findings.append({
            "element": "[navigation landmark]",
            "issue": "No link uses aria-current to indicate the current page.",
            "impact": "Screen reader users can't tell which nav item is active.",
            "severity": "low",
            "recommendation": "Add aria-current='page' to the active link (WCAG 2.4.8).",
        })
    return findings


def _check_combobox_pattern(nodes: list[dict], capture_data: Any) -> list[dict]:
    """Validate combobox ARIA pattern completeness."""
    findings = []
    for node in nodes:
        if _get_role(node) != "combobox":
            continue
        if not _get_name(node):
            findings.append({
                "element": _describe_node(node),
                "issue": "Combobox has no accessible name.",
                "impact": "Screen readers announce 'combobox' with no label.",
                "severity": "high",
                "recommendation": "Add aria-label or linked <label> (WCAG 4.1.2).",
            })
        if _get_properties(node).get("expanded") is None:
            findings.append({
                "element": _describe_node(node),
                "issue": "Combobox missing aria-expanded state.",
                "impact": "Screen readers can't convey open/closed state.",
                "severity": "medium",
                "recommendation": "Add aria-expanded='true'/'false' (WCAG 4.1.2).",
            })
    return findings
