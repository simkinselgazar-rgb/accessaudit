"""WCAG Guideline 4.1 - Compatible checks."""
from __future__ import annotations

import html.parser as _html_parser
import logging
import re

from checks.base import BaseCheck, _make_finding_id
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
    TTResult,
    TTSubTestResult,
)


class _TagNestingValidator(_html_parser.HTMLParser):
    """Lightweight HTML tag-stack validator for WCAG 4.1.1."""

    _VOID = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })
    _OPTIONAL_CLOSE = frozenset({
        "p", "li", "dt", "dd", "tr", "td", "th", "thead", "tbody",
        "tfoot", "colgroup", "caption", "option", "optgroup",
        "rt", "rp", "head", "body", "html",
    })

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.issues: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag in self._VOID:
            return
        # Auto-close optional-close tags when a sibling of the same
        # type starts (e.g. <p>...<p> implicitly closes the first).
        if tag in self._OPTIONAL_CLOSE and self.stack and self.stack[-1] == tag:
            self.stack.pop()
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i] == tag:
                for j in range(len(self.stack) - 1, i, -1):
                    skipped = self.stack[j]
                    if skipped not in self._OPTIONAL_CLOSE:
                        self.issues.append(("misnested", skipped, tag))
                self.stack = self.stack[:i]
                return
        if tag not in self._OPTIONAL_CLOSE:
            self.issues.append(("unmatched_end", tag, ""))


class Check_4_1_1(BaseCheck):
    """SC 4.1.1 Parsing (Level A, WCAG 2.0/2.1 only).

    Removed in WCAG 2.2 but still evaluated when the configured WCAG
    version is 2.0 or 2.1.  Checks for complete start/end tags, correct
    nesting, no duplicate attributes, and unique IDs.
    """

    criterion_id = "4.1.1"
    criterion_name = "Parsing"
    level = "A"
    wcag_versions = ["2.0", "2.1"]
    guideline = "4.1 Compatible"
    principle = "4. Robust"
    ict_baseline = "24"
    tt_tests = ["24.A"]
    normative_text = (
        "In content implemented using markup languages, elements have "
        "complete start and end tags, elements are nested according to "
        "their specifications, elements do not contain duplicate "
        "attributes, and any IDs are unique, except where the "
        "specifications allow these features."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html = capture_data.html or ""

        # ── 1. Duplicate IDs ──────────────────────────────────────────
        id_pattern = re.findall(r'\bid\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
        seen_ids: dict[str, int] = {}
        for id_val in id_pattern:
            seen_ids[id_val] = seen_ids.get(id_val, 0) + 1
        for id_val, count in seen_ids.items():
            if count > 1:
                # Populate BOTH element (auditor-readable form) and
                # css_selector (the canonical `#id` form). The judge
                # rewrites attribute-form selectors `[id="X"]` to id-form
                # `#X` in its output, and the source-attribution
                # validator's selector match needs the input to use the
                # same form, otherwise it demotes the legitimate finding
                # to judge_inference -> FAST-PATH ENFORCEMENT drops it
                # (observed on ASU run f8765656).
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=f"[id=\"{id_val}\"]",
                    css_selector=f"#{id_val}",
                    issue=f"Duplicate ID \"{id_val}\" found {count} times in the document",
                    impact=(
                        "Duplicate IDs cause accessibility failures: "
                        "aria-labelledby, aria-describedby, and label[for] "
                        "references may resolve to the wrong element."
                    ),
                    recommendation=f"Ensure each id value is unique. \"{id_val}\" is used {count} times.",
                    severity=Severity.HIGH,
                ))

        # ── 2. Deprecated / obsolete elements ─────────────────────────
        obsolete_elements = re.findall(
            r'<(font|center|marquee|blink|strike|big|tt)\b',
            html, re.IGNORECASE,
        )
        if obsolete_elements:
            from collections import Counter
            elem_counts = Counter(e.lower() for e in obsolete_elements)
            for elem, count in elem_counts.items():
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=f"<{elem}>",
                    issue=f"Obsolete element <{elem}> used {count} time(s)",
                    impact="Obsolete elements may not be parsed consistently across user agents.",
                    recommendation=f"Replace <{elem}> with modern CSS/HTML equivalents.",
                    severity=Severity.MEDIUM,
                ))

        # ── 3. Duplicate attributes on the same element ───────────────
        dup_attr_reported: set[str] = set()
        for m in re.finditer(r'<(\w+)\b((?:\s+[^>]*?)?)/?>', html):
            tag_name = m.group(1).lower()
            attr_str = m.group(2)
            if not attr_str:
                continue
            attr_names = re.findall(r'\s([\w-]+)\s*=', attr_str)
            seen: dict[str, int] = {}
            for attr in attr_names:
                a_lower = attr.lower()
                seen[a_lower] = seen.get(a_lower, 0) + 1
            for attr, count in seen.items():
                if count > 1:
                    key = f"{tag_name}:{attr}"
                    if key not in dup_attr_reported:
                        dup_attr_reported.add(key)
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=f"<{tag_name}>",
                            issue=f"Duplicate attribute \"{attr}\" on <{tag_name}> element",
                            impact=(
                                "User agents may use the first or last value "
                                "unpredictably, leading to incorrect behavior "
                                "or accessibility failures."
                            ),
                            recommendation=(
                                f"Remove the duplicate \"{attr}\" attribute so each "
                                f"attribute appears only once per element."
                            ),
                            severity=Severity.MEDIUM,
                        ))

        # ── 4. Tag nesting / unclosed tag violations ──────────────────
        validator = _TagNestingValidator()
        try:
            validator.feed(html)
        except Exception:
            pass
        # Remaining stack = unclosed at document end
        for tag in reversed(validator.stack):
            if tag not in _TagNestingValidator._OPTIONAL_CLOSE:
                validator.issues.append(("unclosed", tag, ""))

        # Group issues by type and emit one finding per type (avoids noise)
        issue_tags: dict[str, list[str]] = {}
        for issue_type, tag, _ctx in validator.issues:
            issue_tags.setdefault(issue_type, []).append(tag)

        if "misnested" in issue_tags:
            tags = sorted(set(issue_tags["misnested"]))
            sample = ", ".join(f"<{t}>" for t in tags)
            findings.append(Finding(
                id=_make_finding_id(),
                element="document",
                issue=(
                    f"Incorrectly nested elements detected: {sample}"
                ),
                impact=(
                    "Incorrectly nested elements produce an accessibility tree "
                    "that may not match the intended document structure, causing "
                    "assistive technologies to misrepresent content."
                ),
                recommendation=(
                    "Close inner elements before closing outer elements to "
                    "ensure correct nesting."
                ),
                severity=Severity.HIGH,
            ))

        if "unclosed" in issue_tags:
            tags = sorted(set(issue_tags["unclosed"]))
            sample = ", ".join(f"<{t}>" for t in tags)
            findings.append(Finding(
                id=_make_finding_id(),
                element="document",
                issue=(
                    f"Unclosed elements detected: {sample}"
                ),
                impact=(
                    "Unclosed elements force the browser to infer structure "
                    "that may differ from the author's intent, potentially "
                    "breaking the accessible structure of the document."
                ),
                recommendation=(
                    "Add the missing closing tags to make the document "
                    "structure unambiguous."
                ),
                severity=Severity.HIGH,
            ))

        if "unmatched_end" in issue_tags:
            tags = sorted(set(issue_tags["unmatched_end"]))
            sample = ", ".join(f"</{t}>" for t in tags)
            findings.append(Finding(
                id=_make_finding_id(),
                element="document",
                issue=(
                    f"Orphaned closing tags with no matching start tag: {sample}"
                ),
                impact=(
                    "Orphaned closing tags indicate broken markup that may "
                    "cause inconsistent parsing across user agents."
                ),
                recommendation=(
                    "Remove orphaned closing tags or add the corresponding "
                    "start tags."
                ),
                severity=Severity.MEDIUM,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.85
        return conformance, confidence, findings

    def _generate_tt_results(
        self, findings: list[Finding], capture_data: CaptureData
    ) -> list[TTSubTestResult]:
        has_fail = any(
            f.severity in (Severity.HIGH, Severity.MEDIUM)
            for f in findings
        )
        return [
            TTSubTestResult(
                tt_id="24.A",
                name="Parsing (WCAG 2.0/2.1)",
                result=TTResult.FAIL if has_fail else TTResult.PASS,
            ),
        ]


class Check_4_1_2(BaseCheck):
    """SC 4.1.2 Name, Role, Value (Level A)."""

    criterion_id = "4.1.2"
    criterion_name = "Name, Role, Value"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "4.1 Compatible"
    principle = "4. Robust"
    ict_baseline = "5"
    tt_tests = ["5.A", "5.B", "5.C"]
    normative_text = (
        "For all user interface components (including but not limited to: "
        "form elements, links, and components generated by scripts), the "
        "name and role can be programmatically determined; states, "
        "properties, and values that can be set by the user can be "
        "programmatically set; and notification of changes to these items "
        "is available to user agents, including assistive technologies."
    )
    off_scope_keywords = {
        "contrast": ["contrast ratio"],
        "alt_text": ["alt attribute quality"],
    }

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(
            capture_data.form_fields
            or capture_data.links
            or capture_data.iframes
            or getattr(capture_data, "ai_removed_elements", None)
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Check form fields for accessible names
        for field in capture_data.form_fields:
            selector = field.get("selector", "input")
            tag = (field.get("tag") or field.get("tagName") or "").lower()
            field_type = (field.get("type") or "").lower()
            role = field.get("role", "")
            label = field.get("label", "")
            aria_label = field.get("aria_label", field.get("aria-label", ""))
            aria_labelledby = field.get("aria_labelledby", field.get("aria-labelledby", ""))
            title = field.get("title", "")

            if field_type in ("hidden",):
                continue

            # Name: must have accessible name
            has_name = bool(label or aria_label or aria_labelledby or title)
            if not has_name and field_type not in ("submit", "button", "reset", "image"):
                # Submit buttons can use value attribute as name
                value = field.get("value", "")
                if field_type in ("submit", "button", "reset") and value:
                    continue
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=f"UI component ({field_type or tag}) has no accessible name",
                    impact="Assistive technologies cannot identify this component.",
                    recommendation="Add aria-label, aria-labelledby, or <label> to provide a name.",
                    severity=Severity.HIGH,
                ))

            # Role: custom components should have explicit roles
            interactive_tags = {"a", "button", "input", "select", "textarea"}
            if tag not in interactive_tags and not role:
                has_click = field.get("has_onclick", False)
                tabindex = field.get("tabindex")
                if has_click or (tabindex is not None and str(tabindex) != "-1"):
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=f"Interactive <{tag}> element has no explicit ARIA role",
                        impact="Assistive technologies cannot determine the element type.",
                        recommendation=(
                            "Add an appropriate role attribute (e.g., "
                            "role=\"button\", role=\"link\") or use a "
                            "native HTML element."
                        ),
                        severity=Severity.HIGH,
                    ))

            # Value/state: check aria-checked, aria-expanded, aria-selected
            # for components that should have them
            role_lower = role.lower()
            if role_lower in ("checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"):
                aria_checked = field.get("aria_checked", field.get("aria-checked"))
                if aria_checked is None and tag not in ("input",):
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=f"Custom {role} missing aria-checked state",
                        impact="Screen reader users cannot determine the checked state.",
                        recommendation=f"Add aria-checked='true' or 'false' to the {role} element.",
                        severity=Severity.HIGH,
                    ))

            if role_lower in ("combobox", "listbox", "tree", "grid"):
                aria_expanded = field.get("aria_expanded", field.get("aria-expanded"))
                if aria_expanded is None:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=f"Custom {role} missing aria-expanded state",
                        impact="Users cannot determine if the component is expanded/collapsed.",
                        recommendation=f"Add aria-expanded='true' or 'false' to the {role} element.",
                        severity=Severity.MEDIUM,
                    ))

        # Check links for accessible names (visible links only)
        for link in capture_data.links:
            if link.get("visible") is False:
                continue
            rect = link.get("rect", {})
            if rect and (rect.get("width", 0) <= 1 or rect.get("height", 0) <= 1):
                continue

            selector = link.get("selector", "a")
            text = (link.get("text") or "").strip()
            aria_label = (link.get("aria_label") or link.get("aria-label") or "").strip()
            aria_labelledby = link.get("aria_labelledby", link.get("aria-labelledby", ""))
            title = (link.get("title") or "").strip()
            has_img = link.get("has_image", False)
            img_alt = (link.get("image_alt") or "").strip()

            effective_name = aria_label or text or title
            if not effective_name and not aria_labelledby:
                if has_img and img_alt:
                    continue  # Image alt provides the name
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Link has no accessible name (empty text, no aria-label)",
                    impact="Screen readers will announce 'link' with no description.",
                    recommendation="Add text content, aria-label, or ensure image has alt text.",
                    severity=Severity.HIGH,
                ))

        # Check iframes for titles
        for iframe in capture_data.iframes:
            selector = iframe.get("selector", "iframe")
            title = iframe.get("title", "")
            aria_label = iframe.get("aria_label", iframe.get("aria-label", ""))
            if not title and not aria_label:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Iframe has no title or aria-label",
                    impact="Screen reader users cannot identify the iframe purpose.",
                    recommendation="Add a descriptive title attribute to the iframe.",
                    severity=Severity.MEDIUM,
                ))

        # Check for invalid ARIA attribute usage in HTML
        html = capture_data.html or ""

        # --- aria-hidden="true" on focusable elements ---
        focusable_hidden_pattern = re.finditer(
            r'<(\w+)\b([^>]*?)aria-hidden\s*=\s*["\']true["\']([^>]*?)>',
            html, re.IGNORECASE,
        )
        _native_focusable = {"a", "button", "input", "select", "textarea", "summary"}
        for match in focusable_hidden_pattern:
            tag_name = match.group(1).lower()
            attrs = match.group(2) + match.group(3)
            is_focusable = False
            if tag_name in _native_focusable:
                # <a> needs href to be natively focusable
                if tag_name == "a":
                    is_focusable = bool(re.search(r'\bhref\s*=', attrs, re.IGNORECASE))
                else:
                    is_focusable = True
            # Any element with tabindex >= 0 is focusable
            ti_match = re.search(r'tabindex\s*=\s*["\']?\s*(-?\d+)', attrs, re.IGNORECASE)
            if ti_match and int(ti_match.group(1)) >= 0:
                is_focusable = True
            # Disabled elements are not focusable
            if re.search(r'\bdisabled\b', attrs, re.IGNORECASE):
                is_focusable = False
            if is_focusable:
                snippet = match.group(0)
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=snippet,
                    issue="Focusable element has aria-hidden=\"true\"",
                    impact=(
                        "Assistive technology users can focus the element but it is "
                        "hidden from them, creating a confusing experience."
                    ),
                    recommendation=(
                        "Remove aria-hidden=\"true\" from focusable elements, or "
                        "add tabindex=\"-1\" to remove it from the tab order."
                    ),
                    severity=Severity.HIGH,
                ))

        # --- role="presentation"/"none" on elements with focusable children ---
        pres_pattern = re.finditer(
            r'<(\w+)\b([^>]*?)role\s*=\s*["\'](?:presentation|none)["\']([^>]*?)>(.*?)</\1>',
            html, re.IGNORECASE | re.DOTALL,
        )
        for match in pres_pattern:
            inner_html = match.group(4)
            # Check if inner HTML contains focusable children
            has_focusable_child = bool(re.search(
                r'<(?:a\b[^>]*href|button|input|select|textarea)\b[^>]*>',
                inner_html, re.IGNORECASE,
            )) or bool(re.search(
                r'tabindex\s*=\s*["\']?\s*(?:[0-9])',
                inner_html, re.IGNORECASE,
            ))
            if has_focusable_child:
                snippet = match.group(0)
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=snippet,
                    issue="Element with role=\"presentation\" or role=\"none\" contains focusable children",
                    impact=(
                        "The presentation role removes the element's semantics from "
                        "the accessibility tree, but focusable children remain "
                        "reachable, causing confusion for AT users."
                    ),
                    recommendation=(
                        "Remove role=\"presentation\"/\"none\" from elements that "
                        "contain interactive children, or move the interactive "
                        "children outside this container."
                    ),
                    severity=Severity.HIGH,
                ))

        # --- ARIA role-to-required-properties mapping ---
        _role_required_props = {
            "slider": ["aria-valuenow", "aria-valuemin", "aria-valuemax"],
            "checkbox": ["aria-checked"],
            "combobox": ["aria-expanded"],
            "tab": ["aria-selected"],
            "progressbar": ["aria-valuenow"],
        }
        role_instances = re.finditer(
            r'<(\w+)\b([^>]*?)role\s*=\s*["\'](\w+)["\']([^>]*?)(?:/?>)',
            html, re.IGNORECASE,
        )
        for match in role_instances:
            tag_name = match.group(1).lower()
            role_val = match.group(3).lower()
            full_attrs = match.group(2) + match.group(4)
            if role_val in _role_required_props:
                # Native elements that already expose correct semantics can be skipped
                if role_val == "checkbox" and tag_name == "input":
                    continue
                missing = []
                for prop in _role_required_props[role_val]:
                    if not re.search(rf'{re.escape(prop)}\s*=', full_attrs, re.IGNORECASE):
                        missing.append(prop)
                if missing:
                    snippet = match.group(0)
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=snippet,
                        issue=(
                            f"Element with role=\"{role_val}\" is missing required "
                            f"ARIA properties: {', '.join(missing)}"
                        ),
                        impact=(
                            "Assistive technologies cannot convey the full state of "
                            "this component without the required properties."
                        ),
                        recommendation=(
                            f"Add the missing ARIA properties ({', '.join(missing)}) "
                            f"to the element with role=\"{role_val}\"."
                        ),
                        severity=Severity.HIGH,
                    ))

        # --- aria-labelledby / aria-describedby referencing non-existent IDs ---
        all_ids = set(re.findall(r'\bid\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE))
        for attr_name in ("aria-labelledby", "aria-describedby"):
            ref_pattern = re.finditer(
                rf'{re.escape(attr_name)}\s*=\s*["\']([^"\']+)["\']',
                html, re.IGNORECASE,
            )
            for ref_match in ref_pattern:
                value = ref_match.group(1)
                ref_ids = value.split()
                missing = [r for r in ref_ids if r not in all_ids]
                if not missing:
                    continue
                # Detect prose-as-value: when most/all ref tokens look
                # like words from a sentence rather than ID strings.
                # Observed on ASU's homepage where aria-describedby was
                # set to "We use cookies to improve your experience and
                # our services." -- the splitter then produced 19+
                # findings for "We", "use", "cookies", etc. Emit ONE
                # finding describing the underlying authoring error.
                looks_like_prose = (
                    len(ref_ids) >= 5
                    and len(missing) >= len(ref_ids) * 0.8
                    and any(t.endswith(('.', ',', '!', '?')) for t in ref_ids)
                )
                if looks_like_prose:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=f"[{attr_name}]",
                        issue=(
                            f"{attr_name} value appears to be prose rather "
                            f"than a list of element IDs: \"{value}\""
                        ),
                        impact=(
                            "Browsers split the value on whitespace and "
                            "look up each token as an element id; with "
                            "a sentence as the value, all lookups fail "
                            "and the accessible description is empty."
                        ),
                        recommendation=(
                            "Either move the descriptive text into an "
                            f"element with an id and reference that id "
                            f"via {attr_name}, or replace {attr_name} "
                            "with aria-label / aria-description for "
                            "literal text content."
                        ),
                        severity=Severity.HIGH,
                    ))
                    continue
                for ref_id in missing:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=f"[{attr_name}] references \"{ref_id}\"",
                        issue=(
                            f"{attr_name} references ID \"{ref_id}\" which "
                            f"does not exist in the document"
                        ),
                        impact=(
                            "The accessible name or description will be empty or "
                            "incomplete because the referenced element is missing."
                        ),
                        recommendation=(
                            f"Ensure an element with id=\"{ref_id}\" exists in "
                            f"the document, or correct the {attr_name} value."
                        ),
                        severity=Severity.HIGH,
                    ))

        # --- Button elements must have accessible names ---
        button_pattern = re.finditer(
            r'<button\b([^>]*)>(.*?)</button>',
            html, re.IGNORECASE | re.DOTALL,
        )
        for match in button_pattern:
            btn_attrs = match.group(1)
            btn_content = match.group(2).strip()
            btn_aria_label = re.search(r'aria-label\s*=\s*["\']([^"\']+)["\']', btn_attrs, re.IGNORECASE)
            btn_aria_labelledby = re.search(r'aria-labelledby\s*=\s*["\']([^"\']+)["\']', btn_attrs, re.IGNORECASE)
            btn_title = re.search(r'title\s*=\s*["\']([^"\']+)["\']', btn_attrs, re.IGNORECASE)
            btn_value = re.search(r'value\s*=\s*["\']([^"\']+)["\']', btn_attrs, re.IGNORECASE)
            # Strip HTML tags from content to get text
            text_content = re.sub(r'<[^>]+>', '', btn_content).strip()
            # Check for img alt as accessible name
            img_alt = re.search(r'<img\b[^>]*alt\s*=\s*["\']([^"\']+)["\']', btn_content, re.IGNORECASE)
            has_name = bool(
                text_content
                or (btn_aria_label and btn_aria_label.group(1).strip())
                or (btn_aria_labelledby and btn_aria_labelledby.group(1).strip())
                or (btn_title and btn_title.group(1).strip())
                or (btn_value and btn_value.group(1).strip())
                or (img_alt and img_alt.group(1).strip())
            )
            if not has_name:
                snippet = match.group(0)
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=snippet,
                    issue="Button element has no accessible name",
                    impact="Screen readers announce 'button' with no label, leaving users unable to identify its purpose.",
                    recommendation=(
                        "Add text content, aria-label, aria-labelledby, title, "
                        "or ensure contained images have alt text."
                    ),
                    severity=Severity.HIGH,
                ))
        # Also check <input type="button|submit|reset|image"> for names
        input_btn_pattern = re.finditer(
            r'<input\b([^>]*type\s*=\s*["\'](?:button|submit|reset|image)["\'][^>]*)(?:/?>)',
            html, re.IGNORECASE,
        )
        for match in input_btn_pattern:
            btn_attrs = match.group(1)
            btn_value = re.search(r'value\s*=\s*["\']([^"\']+)["\']', btn_attrs, re.IGNORECASE)
            btn_aria_label = re.search(r'aria-label\s*=\s*["\']([^"\']+)["\']', btn_attrs, re.IGNORECASE)
            btn_aria_labelledby = re.search(r'aria-labelledby\s*=\s*["\']([^"\']+)["\']', btn_attrs, re.IGNORECASE)
            btn_title = re.search(r'title\s*=\s*["\']([^"\']+)["\']', btn_attrs, re.IGNORECASE)
            # For type="image", alt attribute provides the name
            btn_alt = re.search(r'alt\s*=\s*["\']([^"\']+)["\']', btn_attrs, re.IGNORECASE)
            has_name = bool(
                (btn_value and btn_value.group(1).strip())
                or (btn_aria_label and btn_aria_label.group(1).strip())
                or (btn_aria_labelledby and btn_aria_labelledby.group(1).strip())
                or (btn_title and btn_title.group(1).strip())
                or (btn_alt and btn_alt.group(1).strip())
            )
            if not has_name:
                snippet = match.group(0)
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=snippet,
                    issue="Button-type input element has no accessible name",
                    impact="Screen readers cannot identify this button's purpose.",
                    recommendation=(
                        "Add a value attribute, aria-label, aria-labelledby, "
                        "title, or alt (for type=\"image\")."
                    ),
                    severity=Severity.HIGH,
                ))

        # Check for invalid role values
        invalid_roles = re.findall(
            r'role\s*=\s*["\']([^"\']+)["\']', html
        )
        valid_roles = {
            "alert", "alertdialog", "application", "article", "banner",
            "button", "cell", "checkbox", "columnheader", "combobox",
            "complementary", "contentinfo", "definition", "dialog",
            "directory", "document", "feed", "figure", "form", "grid",
            "gridcell", "group", "heading", "img", "link", "list",
            "listbox", "listitem", "log", "main", "marquee", "math",
            "menu", "menubar", "menuitem", "menuitemcheckbox",
            "menuitemradio", "navigation", "none", "note", "option",
            "presentation", "progressbar", "radio", "radiogroup",
            "region", "row", "rowgroup", "rowheader", "scrollbar",
            "search", "searchbox", "separator", "slider", "spinbutton",
            "status", "switch", "tab", "table", "tablist", "tabpanel",
            "term", "textbox", "timer", "toolbar", "tooltip", "tree",
            "treegrid", "treeitem",
        }
        for role_val in invalid_roles:
            for r in role_val.split():
                if r.lower() not in valid_roles:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=f"[role=\"{role_val}\"]",
                        issue=f"Invalid ARIA role value: \"{r}\"",
                        impact="Assistive technologies may ignore or misinterpret the element.",
                        recommendation=f"Use a valid ARIA role value. \"{r}\" is not a recognized role.",
                        severity=Severity.MEDIUM,
                    ))

        # --- AI-removed elements: custom elements without proper role/name ---
        # These are elements that the AI flagged during capture as "not real
        # accessible elements" — e.g. divs acting as buttons without an ARIA
        # role.  This is a direct 4.1.2 violation (Name, Role, Value).
        ai_removed = getattr(capture_data, "ai_removed_elements", None) or []
        for elem in ai_removed:
            selector = elem.get("selector", "element")
            tag = elem.get("tag", "")
            elem_type = elem.get("type", "")
            text = elem.get("text") or ""
            reason = elem.get("reason", "")

            element_desc = selector
            if tag and text:
                element_desc = f"{selector} (<{tag}> \"{text}\")"

            findings.append(Finding(
                id=_make_finding_id(),
                element=element_desc,
                issue=(
                    f"Custom element <{tag}> acts as interactive control "
                    f"({elem_type}) but lacks proper ARIA role and accessible name"
                    + (f" — {reason}" if reason else "")
                ),
                impact=(
                    "Assistive technologies cannot determine the element's "
                    "role or name. Screen reader users will not know this "
                    "element is interactive or what it does."
                ),
                recommendation=(
                    f"Add an appropriate ARIA role (e.g., role=\"button\") "
                    f"and an accessible name (aria-label or visible text) to "
                    f"the <{tag}> element, or replace it with a native HTML "
                    f"element (e.g., <button>)."
                ),
                severity=Severity.HIGH,
            ))

        # --- Accessibility tree analysis (CDP Accessibility.getFullAXTree) ---
        try:
            a11y_tree = capture_data.a11y_tree
            ax_nodes = (a11y_tree or {}).get("nodes") or []
            if ax_nodes:
                _interactive_roles = {
                    "button", "link", "textbox", "combobox", "checkbox",
                    "radio", "slider", "switch", "tab", "menuitem",
                    "treeitem",
                }

                # Build a quick lookup: nodeId -> node
                node_by_id: dict[str, dict] = {}
                for node in ax_nodes:
                    nid = node.get("nodeId")
                    if nid is not None:
                        node_by_id[str(nid)] = node

                for node in ax_nodes:
                    node_role_obj = node.get("role") or {}
                    node_role = (node_role_obj.get("value") or "").lower()

                    node_name_obj = node.get("name") or {}
                    node_name = (node_name_obj.get("value") or "").strip()

                    # 1) Missing accessible names on interactive elements
                    if node_role in _interactive_roles and not node_name:
                        backend_id = node.get("backendDOMNodeId", "?")
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=f"AXNode role={node_role} (backendDOMNodeId={backend_id})",
                            issue=(
                                f"Interactive element with role \"{node_role}\" has no "
                                f"accessible name in the accessibility tree"
                            ),
                            impact=(
                                "Screen readers will announce the role but not a "
                                "label, making it impossible for AT users to "
                                "identify the control's purpose."
                            ),
                            recommendation=(
                                "Provide an accessible name via aria-label, "
                                "aria-labelledby, a visible <label>, or text content."
                            ),
                            severity=Severity.HIGH,
                        ))

                    # 2) Generic/unnamed wrappers with interactive children
                    if node_role == "generic":
                        child_ids = node.get("childIds") or []
                        for cid in child_ids:
                            child = node_by_id.get(str(cid))
                            if not child:
                                continue
                            child_role = (
                                (child.get("role") or {}).get("value") or ""
                            ).lower()
                            child_name = (
                                (child.get("name") or {}).get("value") or ""
                            ).strip()
                            if child_role in _interactive_roles and not child_name:
                                backend_id = node.get("backendDOMNodeId", "?")
                                findings.append(Finding(
                                    id=_make_finding_id(),
                                    element=(
                                        f"AXNode role=generic "
                                        f"(backendDOMNodeId={backend_id})"
                                    ),
                                    issue=(
                                        f"Generic wrapper contains unnamed interactive "
                                        f"child (role=\"{child_role}\") with no "
                                        f"accessible name"
                                    ),
                                    impact=(
                                        "The wrapper provides no grouping semantics "
                                        "and the interactive child is unlabelled, "
                                        "leaving AT users unable to determine its "
                                        "purpose."
                                    ),
                                    recommendation=(
                                        "Give the interactive child an accessible "
                                        "name, or replace the generic wrapper with "
                                        "a semantically meaningful element."
                                    ),
                                    severity=Severity.HIGH,
                                ))

                    # 3) State conflicts: aria-checked / aria-expanded
                    node_props = node.get("properties") or []
                    props_dict: dict[str, str] = {}
                    for prop in node_props:
                        pname = (prop.get("name") or "").lower()
                        pval = prop.get("value")
                        if isinstance(pval, dict):
                            pval = pval.get("value")
                        if pname and pval is not None:
                            props_dict[pname] = str(pval).lower()

                    # Check name sources for HTML-attribute values
                    name_sources = node_name_obj.get("sources") or []
                    html_attrs: dict[str, str] = {}
                    for src in name_sources:
                        attr_name = (src.get("attribute") or "").lower()
                        attr_val = src.get("attributeValue")
                        if isinstance(attr_val, dict):
                            attr_val = attr_val.get("value")
                        if attr_name and attr_val is not None:
                            html_attrs[attr_name] = str(attr_val).lower()

                    # Also look through properties for HTML attribute info
                    # (CDP sometimes puts the HTML attribute value separately)
                    for state_attr in ("checked", "expanded"):
                        tree_val = props_dict.get(state_attr)
                        html_val = html_attrs.get(f"aria-{state_attr}")
                        if tree_val is None or html_val is None:
                            continue
                        # Normalise: "true"/"false"/"mixed"
                        if tree_val != html_val:
                            backend_id = node.get("backendDOMNodeId", "?")
                            findings.append(Finding(
                                id=_make_finding_id(),
                                element=(
                                    f"AXNode role={node_role} "
                                    f"(backendDOMNodeId={backend_id})"
                                ),
                                issue=(
                                    f"State conflict: aria-{state_attr} HTML "
                                    f"attribute is \"{html_val}\" but the "
                                    f"accessibility tree reports "
                                    f"\"{state_attr}={tree_val}\""
                                ),
                                impact=(
                                    "The mismatch between the HTML attribute and "
                                    "the computed accessibility tree state may "
                                    "cause assistive technologies to report "
                                    "incorrect information."
                                ),
                                recommendation=(
                                    f"Ensure the aria-{state_attr} attribute "
                                    f"value matches the actual component state, "
                                    f"and update it dynamically when the state "
                                    f"changes."
                                ),
                                severity=Severity.MEDIUM,
                            ))
        except Exception:
            logging.getLogger(__name__).debug(
                "Accessibility tree analysis skipped due to unexpected format",
                exc_info=True,
            )

        conformance = self._determine_conformance(
            findings,
            len(capture_data.form_fields) + len(capture_data.links),
        )
        confidence = 0.8
        return conformance, confidence, findings

    def _generate_tt_results(
        self, findings: list[Finding], capture_data: CaptureData
    ) -> list[TTSubTestResult]:
        not_app = not self.is_applicable(capture_data)
        name_fail = any(
            "no accessible name" in f.issue.lower()
            for f in findings if f.severity in (Severity.HIGH, Severity.MEDIUM)
        )
        role_fail = any(
            "no explicit aria role" in f.issue.lower() or "invalid aria role" in f.issue.lower()
            for f in findings if f.severity in (Severity.HIGH, Severity.MEDIUM)
        )
        state_fail = any(
            "missing aria-" in f.issue.lower()
            for f in findings if f.severity in (Severity.HIGH, Severity.MEDIUM)
        )
        return [
            TTSubTestResult(
                tt_id="5.A",
                name="UI components have programmatically determinable names",
                result=TTResult.DNA if not_app else TTResult.FAIL if name_fail else TTResult.PASS,
            ),
            TTSubTestResult(
                tt_id="5.B",
                name="UI components have programmatically determinable roles",
                result=TTResult.DNA if not_app else TTResult.FAIL if role_fail else TTResult.PASS,
            ),
            TTSubTestResult(
                tt_id="5.C",
                name="UI component states/values are programmatically set",
                result=TTResult.DNA if not_app else TTResult.FAIL if state_fail else TTResult.PASS,
            ),
        ]


class Check_4_1_3(BaseCheck):
    """SC 4.1.3 Status Messages (Level AA, WCAG 2.1/2.2)."""

    criterion_id = "4.1.3"
    criterion_name = "Status Messages"
    level = "AA"
    wcag_versions = ["2.1", "2.2"]
    guideline = "4.1 Compatible"
    principle = "4. Robust"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "In content implemented using markup languages, status messages "
        "can be programmatically determined through role or properties "
        "such that they can be presented to the user by assistive "
        "technologies without receiving focus."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(
            capture_data.form_fields
            or capture_data.form_errors
            or capture_data.context_changes
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html = capture_data.html or ""
        html_lower = html.lower()

        # Check for aria-live regions
        has_live_region = bool(
            re.search(r'aria-live\s*=\s*["\'](?:polite|assertive)["\']', html, re.IGNORECASE)
            or re.search(r'role\s*=\s*["\'](?:alert|status|log|marquee|timer)["\']', html, re.IGNORECASE)
        )

        # SC 4.1.3 status messages appear DYNAMICALLY at runtime; a static
        # HTML keyword scan ("saved" / "updated" / "error message" etc.)
        # cannot distinguish a real status message from ordinary page text
        # and produced verdict-flipping false positives (verified
        # umich.edu 2026-05-28: an "action confirmation detected" finding
        # matched static news copy on a page with NO status mechanism;
        # earlier berkeley.edu: `loading="eager"` matched "loading"). We do
        # NOT infer status messages from a static snapshot. Grounded signals
        # only: real captured form errors (below) and live-region presence.
        # When the page has a form but no live region anywhere, emit a single
        # INFO advisory (manual check) -- never a definitive violation.
        has_form = bool(re.search(r"<form[\s>]", html_lower))
        if has_form and not has_live_region:
            findings.append(Finding(
                id=_make_finding_id(),
                element="document",
                issue=(
                    "[ADVISORY — manual check, NOT a detected violation] The "
                    "page has a form but no ARIA live region (aria-live / "
                    "role=status / role=alert) anywhere. If submitting or "
                    "interacting with the form produces a dynamic status "
                    "message (success/error confirmation, validation feedback, "
                    "result count) without a page reload, verify it is "
                    "announced via a live region. This cannot be confirmed "
                    "from a static capture."
                ),
                impact=(
                    "Screen reader users are not notified of dynamic status "
                    "changes that are not wrapped in a live region."
                ),
                recommendation=(
                    "For any runtime status message, use aria-live=\"polite\" "
                    "for non-urgent updates or role=\"alert\" for urgent ones."
                ),
                severity=Severity.INFO,
            ))

        # Check form error messages for live region
        for error in capture_data.form_errors:
            selector = error.get("selector", "error")
            has_live = error.get("has_aria_live", False)
            has_role_alert = error.get("has_role_alert", False)

            if not has_live and not has_role_alert:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Form error message is not in an aria-live region",
                    impact="Screen reader users will not hear error messages appear.",
                    recommendation=(
                        "Add role=\"alert\" or aria-live=\"assertive\" to the "
                        "error message container."
                    ),
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.55
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_4_1_1(),
        Check_4_1_2(),
        Check_4_1_3(),
    ]
