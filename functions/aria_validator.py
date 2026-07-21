"""ARIA attribute validation for WCAG 1.3.1 and 4.1.2.

Validates that ARIA references point to real elements,
role+attribute combinations follow the WAI-ARIA spec,
and required attributes are present for each role.
"""
from __future__ import annotations

import re
import logging

logger = logging.getLogger(__name__)

# Attributes whose values are space-separated lists of ID references.
_IDREF_ATTRIBUTES = frozenset({
    "aria-labelledby",
    "aria-describedby",
    "aria-controls",
    "aria-owns",
    "aria-activedescendant",
    "aria-errormessage",
    "aria-flowto",
    "aria-details",
})

# WAI-ARIA 1.2 required owned elements and required attributes per role.
# Only roles that mandate specific aria-* attributes are listed.
_ROLE_REQUIRED_ATTRS: dict[str, list[str]] = {
    "checkbox": ["aria-checked"],
    "combobox": ["aria-expanded"],
    "heading": ["aria-level"],
    "meter": ["aria-valuenow"],
    "radio": ["aria-checked"],
    "scrollbar": ["aria-controls", "aria-valuenow"],
    "separator": ["aria-valuenow"],  # only when focusable
    "slider": ["aria-valuenow"],
    "spinbutton": ["aria-valuenow"],
    "switch": ["aria-checked"],
}

# Native HTML elements that implicitly carry a role, so explicitly
# setting the same role is redundant.
_IMPLICIT_ROLES: dict[str, str] = {
    "a": "link",                  # with href
    "article": "article",
    "aside": "complementary",
    "button": "button",
    "datalist": "listbox",
    "details": "group",
    "dialog": "dialog",
    "fieldset": "group",
    "figure": "figure",
    "footer": "contentinfo",      # when scoped to body
    "form": "form",
    "h1": "heading",
    "h2": "heading",
    "h3": "heading",
    "h4": "heading",
    "h5": "heading",
    "h6": "heading",
    "header": "banner",           # when scoped to body
    "hr": "separator",
    "img": "img",
    "input": "textbox",           # type=text default
    "li": "listitem",
    "main": "main",
    "math": "math",
    "menu": "list",
    "nav": "navigation",
    "ol": "list",
    "optgroup": "group",
    "option": "option",
    "output": "status",
    "progress": "progressbar",
    "search": "search",
    "section": "region",          # when labelled
    "select": "listbox",          # or combobox depending on size
    "summary": "button",
    "table": "table",
    "tbody": "rowgroup",
    "td": "cell",
    "textarea": "textbox",
    "tfoot": "rowgroup",
    "th": "columnheader",
    "thead": "rowgroup",
    "tr": "row",
    "ul": "list",
}

# Roles for which role="presentation" / role="none" creates a conflict
# when the element is focusable.
_FOCUSABLE_TAGS = frozenset({
    "a", "button", "input", "select", "textarea", "summary",
})


def _collect_all_ids(html: str) -> set[str]:
    """Return every id value present in *html*."""
    return set(re.findall(r'\bid\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE))


def _build_selector(tag: str, attrs: dict[str, str]) -> str:
    """Build a minimal CSS-ish selector from tag + attributes.

    Handles two edge cases that crashed an earlier version when the
    page-under-test had author markup like ``class=""`` (empty
    class attribute, common in CMS-generated HTML):

      - ``attrs["class"]`` may be present-but-empty/whitespace-only,
        in which case ``"".split()`` returns ``[]`` and indexing
        ``[0]`` raised IndexError. Now we split first and only use
        the token if at least one survived.
      - ``attrs["id"]`` may be present-but-empty (``id=""``); we
        skip empty ids the same way to avoid emitting a selector
        like ``div#`` that no querySelector would accept.

    Verified crash on a municipal-government site run 20260512_103712 -- empty
    class attributes raised IndexError and silently killed the
    whole ARIA validation pass for the page, leaving aria_issues
    empty for downstream judge calls.
    """
    sel = tag
    id_value = (attrs.get("id") or "").strip()
    if id_value:
        sel += f"#{id_value}"
        return sel
    class_value = (attrs.get("class") or "").strip()
    if class_value:
        tokens = class_value.split()
        if tokens:
            sel += f".{tokens[0]}"
    return sel


def validate_id_references(html: str) -> list[dict]:
    """Check that every ARIA ID reference points to an existing element.

    Scans the HTML for these attributes:
    - aria-labelledby
    - aria-describedby
    - aria-controls
    - aria-owns
    - aria-activedescendant
    - aria-errormessage
    - aria-flowto
    - aria-details
    - for (on label elements)

    For each, extracts the referenced ID(s) and checks if an element
    with that ID exists anywhere in the document.

    Returns list of dicts:
    [{
        "attribute": "aria-describedby",
        "element_selector": "input#email",
        "referenced_id": "email-help",
        "exists": False,
        "severity": "high",
        "issue": "aria-describedby references ID 'email-help' which does not exist"
    }]
    """
    if not html:
        return []

    known_ids = _collect_all_ids(html)
    issues: list[dict] = []

    # Match opening tags with their full attribute string.
    # The regex is intentionally broad to handle malformed HTML gracefully.
    tag_re = re.compile(r'<(\w+)\b([^>]*)/?>', re.DOTALL)

    for tag_match in tag_re.finditer(html):
        tag = tag_match.group(1).lower()
        attr_str = tag_match.group(2)
        if not attr_str:
            continue

        # Collect attributes into a dict for selector building.
        attr_dict: dict[str, str] = {}
        for a_match in re.finditer(r'([\w-]+)\s*=\s*["\']([^"\']*)["\']', attr_str):
            attr_dict[a_match.group(1).lower()] = a_match.group(2)

        selector = _build_selector(tag, attr_dict)

        # Check ARIA idref attributes. Consolidate per (selector, attribute):
        # one finding per attribute on a given element, listing every missing
        # token. Earlier code emitted one finding per token, so a markup bug
        # like ``aria-describedby="We use cookies and similar technologies"``
        # produced N findings (one per word) instead of one consolidated
        # "non-IDREF tokens" finding for the broken attribute.
        for aria_attr in _IDREF_ATTRIBUTES:
            value = attr_dict.get(aria_attr, "")
            if not value:
                continue
            ref_ids = value.split()
            missing = [r for r in ref_ids if r not in known_ids]
            if not missing:
                continue
            if len(missing) == 1 and len(ref_ids) == 1:
                issue_text = (
                    f"{aria_attr} references ID '{missing[0]}' "
                    f"which does not exist"
                )
            elif len(missing) == len(ref_ids):
                issue_text = (
                    f"{aria_attr} value contains "
                    f"{len(missing)} token(s), none of which match an "
                    f"element ID: {', '.join(repr(t) for t in missing)}. "
                    f"This usually means the attribute holds prose instead "
                    f"of space-separated IDREFs."
                )
            else:
                issue_text = (
                    f"{aria_attr} references {len(missing)} ID(s) that do "
                    f"not exist: {', '.join(repr(t) for t in missing)} "
                    f"(of {len(ref_ids)} token(s) total)."
                )
            issues.append({
                "attribute": aria_attr,
                "element_selector": selector,
                "referenced_id": missing[0] if len(missing) == 1 else "",
                "missing_ids": missing,
                "all_tokens": ref_ids,
                "exists": False,
                "severity": "high",
                "issue": issue_text,
            })

        # Check label[for].
        if tag == "label":
            for_val = attr_dict.get("for", "")
            if for_val and for_val not in known_ids:
                issues.append({
                    "attribute": "for",
                    "element_selector": selector,
                    "referenced_id": for_val,
                    "exists": False,
                    "severity": "high",
                    "issue": (
                        f"label[for] references ID '{for_val}' "
                        f"which does not exist"
                    ),
                })

    return issues


def validate_role_required_attributes(elements: list[dict]) -> list[dict]:
    """Check that elements with ARIA roles have their required attributes.

    WAI-ARIA spec defines required attributes per role:
    - checkbox: aria-checked
    - combobox: aria-expanded
    - heading: aria-level (when not using h1-h6)
    - meter: aria-valuenow
    - radio: aria-checked
    - scrollbar: aria-controls, aria-valuenow
    - separator (focusable): aria-valuenow
    - slider: aria-valuenow
    - spinbutton: aria-valuenow
    - switch: aria-checked

    Args:
        elements: list of dicts with keys: selector, tag, role,
                  and any aria-* attributes

    Returns list of issues found.
    """
    issues: list[dict] = []

    for el in elements:
        role = (el.get("role") or "").lower().strip()
        if not role:
            continue
        tag = (el.get("tag") or el.get("tagName") or "").lower()
        selector = el.get("selector", tag or role)

        required = _ROLE_REQUIRED_ATTRS.get(role)
        if not required:
            continue

        # Native h1-h6 headings already expose their level via the tag;
        # aria-level is only required when the role is set explicitly on
        # a non-heading element.
        if role == "heading" and re.match(r"^h[1-6]$", tag):
            continue

        # separator only requires aria-valuenow when focusable.
        if role == "separator":
            tabindex = el.get("tabindex", el.get("tabIndex"))
            if tabindex is None:
                continue

        for attr in required:
            # Normalize lookup: check both dashed and underscored forms
            # because capture data may use either.
            dashed = attr                           # e.g. aria-checked
            underscored = attr.replace("-", "_")    # e.g. aria_checked

            has_attr = (
                el.get(dashed) is not None
                or el.get(underscored) is not None
            )
            if not has_attr:
                issues.append({
                    "attribute": attr,
                    "element_selector": selector,
                    "role": role,
                    "severity": "high",
                    "issue": (
                        f"Element with role=\"{role}\" is missing "
                        f"required attribute {attr}"
                    ),
                })

    return issues


def validate_role_usage(elements: list[dict]) -> list[dict]:
    """Check for common ARIA role misuse patterns.

    Checks:
    - role="button" on div/span without tabindex (not keyboard accessible)
    - role="presentation" or role="none" on focusable elements (conflict)
    - aria-hidden="true" on focusable elements (creates invisible tab stop)
    - role on element that already has that implicit role (redundant)
    - aria-label on non-interactive element without a widget role
    - aria-expanded without a mechanism to expand (no click handler indicator)

    Args:
        elements: list of dicts from capture data

    Returns list of issues found.
    """
    _WIDGET_ROLES = frozenset({
        "button", "checkbox", "combobox", "gridcell", "link",
        "listbox", "menu", "menubar", "menuitem", "menuitemcheckbox",
        "menuitemradio", "option", "progressbar", "radio",
        "scrollbar", "searchbox", "slider", "spinbutton", "switch",
        "tab", "tabpanel", "textbox", "treeitem",
    })
    _INTERACTIVE_TAGS = frozenset({
        "a", "button", "input", "select", "textarea", "summary", "details",
    })

    issues: list[dict] = []

    for el in elements:
        tag = (el.get("tag") or el.get("tagName") or "").lower()
        role = (el.get("role") or "").lower().strip()
        selector = el.get("selector", tag or role or "element")
        tabindex = el.get("tabindex", el.get("tabIndex"))
        aria_hidden = (
            el.get("aria-hidden") or el.get("aria_hidden") or ""
        ).lower()
        aria_label = el.get("aria-label") or el.get("aria_label") or ""
        aria_expanded = el.get("aria-expanded") or el.get("aria_expanded")

        # 1. role="button" on div/span without tabindex
        if role == "button" and tag in ("div", "span"):
            if tabindex is None or str(tabindex) == "":
                issues.append({
                    "attribute": "role",
                    "element_selector": selector,
                    "severity": "high",
                    "issue": (
                        f"<{tag}> with role=\"button\" has no tabindex "
                        f"and is not keyboard accessible"
                    ),
                })

        # 2. role="presentation" / role="none" on focusable elements
        if role in ("presentation", "none"):
            is_focusable = (
                tag in _FOCUSABLE_TAGS
                or (tabindex is not None and str(tabindex) != "")
            )
            if is_focusable:
                issues.append({
                    "attribute": "role",
                    "element_selector": selector,
                    "severity": "high",
                    "issue": (
                        f"Focusable element <{tag}> has role=\"{role}\" "
                        f"which conflicts with its focusability; browsers "
                        f"ignore the role but the conflict indicates a "
                        f"design error"
                    ),
                })

        # 3. aria-hidden="true" on focusable elements
        if aria_hidden == "true":
            is_focusable = (
                tag in _FOCUSABLE_TAGS
                or (tabindex is not None and str(tabindex).lstrip("-").isdigit()
                    and int(str(tabindex)) >= 0)
            )
            if is_focusable:
                issues.append({
                    "attribute": "aria-hidden",
                    "element_selector": selector,
                    "severity": "high",
                    "issue": (
                        f"Focusable element <{tag}> has aria-hidden=\"true\", "
                        f"creating an invisible tab stop that traps assistive "
                        f"technology users"
                    ),
                })

        # 4. Redundant explicit role matching the implicit role
        if role and tag in _IMPLICIT_ROLES:
            implicit = _IMPLICIT_ROLES[tag]
            if role == implicit:
                issues.append({
                    "attribute": "role",
                    "element_selector": selector,
                    "severity": "low",
                    "issue": (
                        f"<{tag}> has explicit role=\"{role}\" which "
                        f"matches its implicit role and is redundant"
                    ),
                })

        # 5. aria-label on non-interactive element without a widget role
        if aria_label and tag not in _INTERACTIVE_TAGS:
            if role not in _WIDGET_ROLES and role not in (
                "img", "region", "navigation", "complementary",
                "banner", "contentinfo", "main", "form", "search",
                "dialog", "alertdialog", "alert", "log", "marquee",
                "status", "timer", "tooltip", "group", "tree",
                "treegrid", "grid", "table", "tablist", "toolbar",
                "application", "document", "feed", "figure", "list",
                "listitem", "math", "note", "presentation", "none",
                "definition", "term", "directory",
            ):
                issues.append({
                    "attribute": "aria-label",
                    "element_selector": selector,
                    "severity": "medium",
                    "issue": (
                        f"<{tag}> has aria-label but is not interactive "
                        f"and has no widget/landmark role; assistive "
                        f"technology may ignore the label"
                    ),
                })

        # 6. aria-expanded without interactive semantics
        if aria_expanded is not None and str(aria_expanded) != "":
            has_interactive = (
                tag in _INTERACTIVE_TAGS
                or role in _WIDGET_ROLES
                or (tabindex is not None and str(tabindex).lstrip("-").isdigit())
            )
            if not has_interactive:
                issues.append({
                    "attribute": "aria-expanded",
                    "element_selector": selector,
                    "severity": "medium",
                    "issue": (
                        f"<{tag}> has aria-expanded but is not interactive "
                        f"and has no widget role; the expanded state cannot "
                        f"be toggled by keyboard"
                    ),
                })

    return issues


def run_all_validations(html: str, elements: list[dict]) -> list[dict]:
    """Run all ARIA validations and return combined results.

    Convenience function that calls all three validators
    and returns a single list of issues.
    """
    results: list[dict] = []
    results.extend(validate_id_references(html))
    results.extend(validate_role_required_attributes(elements))
    results.extend(validate_role_usage(elements))
    return results
