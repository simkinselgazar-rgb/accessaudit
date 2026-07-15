"""Screen reader announcement rendering.

Builds the exact text strings that JAWS, NVDA, and VoiceOver would
announce for each node in the accessibility tree.  This enables
testing what users actually HEAR, not just what the DOM contains.
"""
from __future__ import annotations

import re
from typing import Any


# ARIA role → screen reader announcement text
_ROLE_ANNOUNCEMENTS: dict[str, str] = {
    "button": "button",
    "link": "link",
    "heading": "heading",
    "img": "image",
    "image": "image",
    "textbox": "edit",
    "checkbox": "check box",
    "radio": "radio button",
    "combobox": "combo box",
    "listbox": "list box",
    "slider": "slider",
    "spinbutton": "spin button",
    "switch": "switch",
    "tab": "tab",
    "tabpanel": "tab panel",
    "tablist": "tab list",
    "menuitem": "menu item",
    "menu": "menu",
    "menubar": "menu bar",
    "menuitemcheckbox": "menu item check box",
    "menuitemradio": "menu item radio button",
    "dialog": "dialog",
    "alertdialog": "alert dialog",
    "alert": "alert",
    "status": "status",
    "progressbar": "progress bar",
    "tree": "tree view",
    "treeitem": "tree item",
    "grid": "grid",
    "gridcell": "cell",
    "row": "row",
    "rowheader": "row header",
    "columnheader": "column header",
    "cell": "cell",
    "table": "table",
    "list": "list",
    "listitem": "list item",
    "navigation": "navigation",
    "main": "main",
    "banner": "banner",
    "contentinfo": "content information",
    "complementary": "complementary",
    "search": "search",
    "form": "form",
    "region": "region",
    "article": "article",
    "figure": "figure",
    "separator": "separator",
    "toolbar": "tool bar",
    "tooltip": "tooltip",
    "document": "document",
    "application": "application",
    "group": "group",
    "log": "log",
    "marquee": "marquee",
    "math": "math",
    "note": "note",
    "timer": "timer",
    "definition": "definition",
    "term": "term",
    "directory": "directory",
    "feed": "feed",
}

# Roles that are "interactive" — they MUST have an accessible name
_INTERACTIVE_ROLES: set[str] = {
    "button", "link", "textbox", "checkbox", "radio", "combobox",
    "listbox", "slider", "spinbutton", "switch", "tab", "menuitem",
    "menuitemcheckbox", "menuitemradio", "treeitem", "option",
    "searchbox", "gridcell",
}

# Roles that are "landmarks" — they SHOULD have labels when multiple
_LANDMARK_ROLES: set[str] = {
    "navigation", "main", "banner", "contentinfo", "complementary",
    "search", "form", "region",
}

# Roles that are structural/invisible — no announcement needed
_SILENT_ROLES: set[str] = {
    "generic", "none", "presentation", "InlineTextBox", "LineBreak",
    "StaticText", "paragraph", "Section",
}

# Patterns that indicate a meaningless/bad accessible name
_BAD_NAME_PATTERNS: list[re.Pattern] = [
    re.compile(r"^(img|image|photo|picture|graphic|icon|logo)\s*\d*$", re.I),
    re.compile(r"^DSC[_\-]?\d+", re.I),              # Camera filenames
    re.compile(r"^IMG[_\-]?\d+", re.I),               # Camera filenames
    re.compile(r"^\d+\.(jpe?g|png|gif|svg|webp|bmp)$", re.I),  # Numbered files
    re.compile(r"^[a-f0-9]{8,}\.(jpe?g|png|gif|svg)$", re.I),  # Hash filenames
    re.compile(r"^untitled", re.I),
    re.compile(r"^https?://", re.I),                   # URL as name
    re.compile(r"^\s*$"),                               # Whitespace only
    re.compile(r"^(click here|read more|learn more|here|more|link|button)$", re.I),
    re.compile(r"^\.{2,}$"),                            # Just dots
    re.compile(r"^\*+$"),                               # Just asterisks
    re.compile(r"^(edit|input|field|text|enter)\s*$", re.I),  # Generic form labels
    re.compile(r"^spacer", re.I),                       # Spacer images
    re.compile(r"^(banner|advertisement|ad)\s*\d*$", re.I),  # Ad images
]


def is_meaningless_name(name: str) -> bool:
    """Return True if the accessible name is meaningless to a user."""
    if not name or not name.strip():
        return True
    name = name.strip()
    if len(name) <= 1:
        return True
    return any(p.match(name) for p in _BAD_NAME_PATTERNS)


def render_announcement(node: dict[str, Any]) -> str:
    """Render the screen reader announcement for a single a11y tree node.

    Returns the text a screen reader would speak, e.g.:
      "About Us, heading level 2"
      "Submit, button"
      "Search, edit, required"
      "", (for silent/decorative nodes)
    """
    role = _get_role(node)
    name = _get_name(node)
    properties = _get_properties(node)

    # Silent roles produce no announcement
    if role in _SILENT_ROLES:
        return ""

    # Decorative images (presentation/none role, or empty name on img)
    if role in ("none", "presentation"):
        return ""

    parts: list[str] = []

    # 1. Accessible name
    if name:
        parts.append(name)

    # 2. Role announcement
    role_text = _ROLE_ANNOUNCEMENTS.get(role, "")
    if role == "heading":
        level = properties.get("level", "")
        if level:
            role_text = f"heading level {level}"
    if role_text:
        parts.append(role_text)

    # 3. State announcements
    states = _build_state_announcements(role, properties)
    parts.extend(states)

    # 4. Description (aria-describedby content)
    description = properties.get("description", "")
    if description:
        parts.append(description)

    return ", ".join(parts)


def render_announcement_issues(node: dict[str, Any]) -> list[dict[str, str]]:
    """Check a single node for AT announcement quality issues.

    Returns a list of issue dicts with keys:
      role, name, issue, impact, severity
    """
    role = _get_role(node)
    name = _get_name(node)
    properties = _get_properties(node)
    issues: list[dict[str, str]] = []

    # Skip silent/decorative roles
    if role in _SILENT_ROLES or role in ("none", "presentation"):
        return issues

    # Issue 1: Interactive element with no accessible name
    if role in _INTERACTIVE_ROLES and not name:
        issues.append({
            "role": role,
            "name": "",
            "issue": f"Interactive {_ROLE_ANNOUNCEMENTS.get(role, role)} has no accessible name. "
                     f"Screen readers will announce only the role with no identifying text.",
            "impact": f"Screen reader users (JAWS, NVDA, VoiceOver) will hear "
                      f"'{_ROLE_ANNOUNCEMENTS.get(role, role)}' with no description of "
                      f"what this control does, making it impossible to identify its purpose.",
            "severity": "high",
        })

    # Issue 2: Interactive element with meaningless name
    elif role in _INTERACTIVE_ROLES and is_meaningless_name(name):
        announcement = render_announcement(node)
        issues.append({
            "role": role,
            "name": name,
            "issue": f"Screen readers announce this as '{announcement}' — the name "
                     f"'{name}' does not convey the element's purpose.",
            "impact": f"Screen reader users will hear '{announcement}' which provides "
                      f"no meaningful context about what this {_ROLE_ANNOUNCEMENTS.get(role, role)} "
                      f"does or where it leads.",
            "severity": "medium",
        })

    # Issue 3: Image with filename as alt text
    if role in ("img", "image") and name:
        if re.search(r"\.(jpe?g|png|gif|svg|webp|bmp|tiff?)$", name, re.I):
            issues.append({
                "role": role,
                "name": name,
                "issue": f"Image alt text is a filename '{name}'. Screen readers "
                         f"will announce 'image, {name}' which describes the file, "
                         f"not the visual content.",
                "impact": "Screen reader users receive no description of what the "
                          "image actually shows. They hear a filename instead of "
                          "meaningful content.",
                "severity": "high",
            })

    # Issue 4: Heading with no text
    if role == "heading" and not name:
        level = properties.get("level", "?")
        issues.append({
            "role": role,
            "name": "",
            "issue": f"Empty heading level {level}. Screen readers announce "
                     f"'heading level {level}' with no text, disrupting heading navigation.",
            "impact": "Screen reader users navigating by headings (H key) will "
                      "encounter a blank heading, making it impossible to understand "
                      "the page structure at this point.",
            "severity": "medium",
        })

    # Issue 5: Required field without explicit required state
    if role in ("textbox", "combobox", "listbox", "spinbutton"):
        is_required = properties.get("required", False)
        if is_required and not name:
            issues.append({
                "role": role,
                "name": "",
                "issue": "Required form field has no accessible name. Screen readers "
                         "announce 'edit, required' with no label.",
                "impact": "Screen reader users cannot identify which field is required "
                          "or what information to enter.",
                "severity": "high",
            })

    # Issue 6: Focusable element with aria-hidden="true"
    focusable = properties.get("focusable", False)
    hidden = properties.get("hidden", False)
    if focusable and hidden:
        issues.append({
            "role": role,
            "name": name or "(no name)",
            "issue": "Element is focusable but hidden from screen readers (aria-hidden='true'). "
                     "Keyboard users can reach it but screen reader users cannot perceive it.",
            "impact": "Screen reader users will experience a 'ghost' element — focus "
                      "moves to something invisible, creating confusion about where "
                      "they are on the page.",
            "severity": "high",
        })

    return issues


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_role(node: dict[str, Any]) -> str:
    """Extract the role string from an a11y tree node."""
    role = node.get("role", {})
    if isinstance(role, dict):
        return role.get("value", "")
    return str(role)


def _get_name(node: dict[str, Any]) -> str:
    """Extract the accessible name from an a11y tree node."""
    name = node.get("name", {})
    if isinstance(name, dict):
        return name.get("value", "")
    return str(name) if name else ""


def _get_properties(node: dict[str, Any]) -> dict[str, Any]:
    """Extract properties as a flat dict from an a11y tree node."""
    props = node.get("properties", [])
    result: dict[str, Any] = {}
    if isinstance(props, list):
        for p in props:
            pname = p.get("name", "")
            pval = p.get("value", {})
            if isinstance(pval, dict):
                result[pname] = pval.get("value", "")
            else:
                result[pname] = pval
    elif isinstance(props, dict):
        result = props
    return result


def _build_state_announcements(role: str, properties: dict[str, Any]) -> list[str]:
    """Build state announcement strings like 'not checked', 'expanded', 'required'."""
    states: list[str] = []

    # Checked state (checkboxes, radio buttons, switches)
    if role in ("checkbox", "menuitemcheckbox", "switch"):
        checked = properties.get("checked", "")
        if checked == "true" or checked is True:
            states.append("checked")
        elif checked == "mixed":
            states.append("partially checked")
        else:
            states.append("not checked")
    elif role in ("radio", "menuitemradio"):
        checked = properties.get("checked", "")
        if checked == "true" or checked is True:
            states.append("selected")
        else:
            states.append("not selected")

    # Expanded/collapsed
    expanded = properties.get("expanded", None)
    if expanded == "true" or expanded is True:
        states.append("expanded")
    elif expanded == "false" or expanded is False:
        states.append("collapsed")

    # Required
    if properties.get("required") in ("true", True):
        states.append("required")

    # Invalid
    invalid = properties.get("invalid", "")
    if invalid and invalid not in ("false", False):
        states.append("invalid entry")

    # Disabled
    if properties.get("disabled") in ("true", True):
        states.append("unavailable")

    # Selected (tabs, options)
    if role in ("tab", "option", "treeitem"):
        selected = properties.get("selected", "")
        if selected == "true" or selected is True:
            states.append("selected")

    # Read-only
    if properties.get("readonly") in ("true", True):
        states.append("read only")

    # Has popup
    haspopup = properties.get("haspopup", "")
    if haspopup and haspopup not in ("false", False, ""):
        states.append("has popup")

    return states
