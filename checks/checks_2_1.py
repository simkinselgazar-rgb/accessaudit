"""WCAG Guideline 2.1 - Keyboard Accessible (A) checks."""
from __future__ import annotations

import logging
import re

from checks.base import BaseCheck, _make_finding_id
from functions.keyboard_probe import widget_probe_errored

logger = logging.getLogger(__name__)
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
    TTResult,
    TTSubTestResult,
)

# Elements that should be natively keyboard focusable
_INTERACTIVE_TAGS = {
    "a", "button", "input", "select", "textarea", "details", "summary",
}

# Event handlers that indicate interactivity
_CLICK_HANDLERS = {"onclick", "onmousedown", "onmouseup", "ondblclick"}


class Check_2_1_1(BaseCheck):
    """SC 2.1.1 Keyboard (Level A)."""

    criterion_id = "2.1.1"
    criterion_name = "Keyboard"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.1 Keyboard Accessible"
    principle = "2. Operable"
    ict_baseline = "1"
    tt_tests = ["1.A", "1.B"]
    normative_text = (
        "All functionality of the content is operable through a keyboard "
        "interface without requiring specific timings for individual "
        "keystrokes, except where the underlying function requires input "
        "that depends on the path of the user's movement and not just the "
        "endpoints."
    )
    off_scope_keywords = {
        "focus_visible": ["focus indicator", "focus visible", "focus ring"],
        "focus_order": ["tab order", "focus order"],
    }
    web_only = True

    def get_image_context(self, capture_data: CaptureData) -> str:
        """Tell the AI which elements the tab walk definitively reached."""
        if not capture_data.tab_walk:
            return ""
        lines = [
            "TAB WALK RESULTS (deterministic — these elements ARE keyboard reachable):",
            f"Total tab stops reached: {len(capture_data.tab_walk)}",
            "",
        ]
        for i, stop in enumerate(capture_data.tab_walk):
            tag = stop.get("tag", "?")
            sel = stop.get("selector", "")
            text = stop.get("text", "")
            lines.append(f"  {i+1}. <{tag}> \"{text}\" ({sel})")
        lines.append("")
        lines.append(
            "IMPORTANT: Do NOT report any of these elements as 'not keyboard "
            "reachable'. They were reached during a real browser tab walk. "
            "Only report elements that are NOT in this list as unreachable."
        )
        return "\n".join(lines)

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send the keyboard walkthrough video to the AI so it can watch
        focus indicators moving, elements being activated, dropdowns
        opening/closing, and traps occurring."""
        if capture_data.keyboard_walkthrough_video:
            return capture_data.keyboard_walkthrough_video
        return None

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(
            capture_data.links
            or capture_data.form_fields
            or capture_data.tab_walk
            or capture_data.html
        )

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Track elements found in tab walk for keyboard accessibility
        tab_walked_selectors = set()
        for t in capture_data.tab_walk:
            sel = t.get("selector", "")
            if sel:
                tab_walked_selectors.add(sel)

        # Check interactive elements are keyboard accessible
        # Links with href should be focusable
        for link in capture_data.links:
            selector = link.get("selector", "a")
            tag = link.get("tag", link.get("tagName", "a")).lower()
            href = link.get("href", "")
            tabindex = link.get("tabindex")
            role = link.get("role", "")

            # Anchor without href is not keyboard accessible unless tabindex set
            if tag == "a" and not href and not role:
                if tabindex is None or str(tabindex) == "-1":
                    has_click = link.get("has_onclick", False) or link.get("onclick", "")
                    if has_click:
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=selector,
                            issue="Anchor element with click handler has no href and is not keyboard focusable",
                            impact=(
                                "Keyboard users cannot activate this interactive element."
                            ),
                            recommendation=(
                                "Add href attribute, or add tabindex=\"0\" and "
                                "keydown handler, or use a <button> element."
                            ),
                            severity=Severity.HIGH,
                        ))

        # Check for elements with click handlers but no keyboard support
        for field in capture_data.form_fields:
            selector = field.get("selector", "element")
            tag = (field.get("tag") or field.get("tagName") or "").lower()
            role = field.get("role", "")
            tabindex = field.get("tabindex")
            has_click = field.get("has_onclick", False)
            has_keydown = field.get("has_onkeydown", False) or field.get("has_onkeypress", False)

            # Non-interactive elements with click handlers
            if tag not in _INTERACTIVE_TAGS and has_click:
                if tabindex is None or str(tabindex) == "-1":
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Non-interactive element <{tag}> has click handler "
                            f"but is not keyboard focusable"
                        ),
                        impact="Keyboard-only users cannot access this interactive element.",
                        recommendation=(
                            "Add tabindex=\"0\" and a keydown handler for "
                            "Enter/Space, or use a native interactive element."
                        ),
                        severity=Severity.HIGH,
                    ))
                elif str(tabindex) != "-1" and not has_keydown and not role:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"<{tag}> has tabindex and click handler but no "
                            f"keyboard event handler"
                        ),
                        impact="Element is focusable but cannot be activated via keyboard.",
                        recommendation=(
                            "Add onkeydown handler that responds to Enter "
                            "and/or Space key, or use a native <button>."
                        ),
                        severity=Severity.HIGH,
                    ))

        # Check for elements with role but missing keyboard support
        for field in capture_data.form_fields:
            selector = field.get("selector", "element")
            tag = (field.get("tag") or field.get("tagName") or "").lower()
            role = (field.get("role") or "").lower()
            tabindex = field.get("tabindex")

            interactive_roles = {
                "button", "link", "checkbox", "radio", "tab", "menuitem",
                "menuitemcheckbox", "menuitemradio", "option", "switch",
                "slider", "spinbutton", "combobox", "listbox", "textbox",
                "searchbox", "treeitem",
            }

            if role in interactive_roles and tag not in _INTERACTIVE_TAGS:
                if tabindex is None:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Element with role=\"{role}\" is not keyboard "
                            f"focusable (missing tabindex)"
                        ),
                        impact="Keyboard users cannot reach or activate this control.",
                        recommendation=(
                            f"Add tabindex=\"0\" to make the element focusable, "
                            f"or use a native HTML element that matches the "
                            f"\"{role}\" role."
                        ),
                        severity=Severity.HIGH,
                    ))

        # Check script content for mouse-only event listeners
        script = capture_data.script_content or ""
        mouse_only_patterns = [
            (r"addEventListener\s*\(\s*['\"]mousedown['\"]", "mousedown"),
            (r"addEventListener\s*\(\s*['\"]mouseover['\"]", "mouseover"),
            (r"addEventListener\s*\(\s*['\"]mouseenter['\"]", "mouseenter"),
            (r"addEventListener\s*\(\s*['\"]dblclick['\"]", "dblclick"),
        ]
        for pattern, event in mouse_only_patterns:
            if re.search(pattern, script):
                # Check for corresponding keyboard handler
                keyboard_map = {
                    "mousedown": "keydown",
                    "mouseover": "focus",
                    "mouseenter": "focus",
                    "dblclick": "keydown",
                }
                kb_event = keyboard_map.get(event, "keydown")
                kb_pattern = rf"addEventListener\s*\(\s*['\"]({kb_event}|keypress|keyup)['\"]"
                if not re.search(kb_pattern, script):
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element="<script>",
                        issue=(
                            f"JavaScript uses {event} event listener without "
                            f"corresponding {kb_event} handler"
                        ),
                        impact="Functionality triggered by mouse may not be available via keyboard.",
                        recommendation=(
                            f"Add a corresponding {kb_event} event listener "
                            f"to provide keyboard access."
                        ),
                        severity=Severity.MEDIUM,
                    ))

        # --- Tab walk coverage analysis (selector-level matching) ---
        # Only count VISIBLE interactive elements — hidden/collapsed menu
        # items, display:none elements, etc. are not expected to be in the
        # tab order and should not count as "unreachable".
        if capture_data.tab_walk:
            unreachable_links: list[str] = []
            visible_link_count = 0
            for link in capture_data.links:
                link_sel = link.get("selector", "")
                if not link_sel:
                    continue
                # Skip links that are hidden/collapsed (in hamburger menus, etc.)
                if link.get("visible") is False:
                    continue
                rect = link.get("rect", {})
                if rect and (rect.get("width", 0) <= 1 or rect.get("height", 0) <= 1):
                    continue
                visible_link_count += 1
                if link_sel not in tab_walked_selectors:
                    unreachable_links.append(link_sel)

            unreachable_fields: list[str] = []
            visible_field_count = 0
            for f in capture_data.form_fields:
                if (f.get("tag") or "").lower() in ("input", "select", "textarea", "button"):
                    f_sel = f.get("selector", "")
                    if not f_sel:
                        continue
                    # Skip hidden form fields
                    f_type = (f.get("type") or "").lower()
                    if f_type == "hidden":
                        continue
                    rect = f.get("rect", {})
                    if rect and (rect.get("width", 0) <= 0 or rect.get("height", 0) <= 0):
                        continue
                    visible_field_count += 1
                    if f_sel not in tab_walked_selectors:
                        unreachable_fields.append(f_sel)

            unreachable = unreachable_links + unreachable_fields
            total_interactive = visible_link_count + visible_field_count

            # Elements reachable inside expanded menus/dropdowns (via
            # arrow keys after Enter) should not count as unreachable.
            # This data comes from _recorded_keyboard_walkthrough which
            # already opens menus and records what gets focus.
            expanded_selectors: set[str] = set()
            if hasattr(capture_data, 'expanded_tab_walks') and capture_data.expanded_tab_walks:
                for trigger, items in capture_data.expanded_tab_walks.items():
                    for item in items:
                        sel = item.get("selector", "")
                        if sel:
                            expanded_selectors.add(sel)
                # Remove expanded-reachable items from unreachable list
                unreachable = [u for u in unreachable if u not in expanded_selectors]

            # Suppress this finding entirely when the tab walk was halted
            # by a keyboard trap. The "unreachable" list in that case
            # is contaminated -- it contains every link/field downstream
            # of the trap, none of which the test could physically
            # visit. The trap itself is the real issue and is reported
            # under SC 2.1.2; once it is fixed, a re-run will produce
            # accurate Tab coverage data and any remaining unreachable
            # elements will surface here legitimately.
            walk_halted_by_trap = bool(
                (capture_data.tab_coverage or {}).get("halted_by_trap")
            ) or bool(
                (capture_data.tab_walk_truncated or {}).get("forward_halted_by_trap")
            ) or bool(
                (capture_data.tab_walk_truncated or {}).get("backward_halted_by_trap")
            )

            if unreachable and not walk_halted_by_trap:
                # Report ALL unreachable elements
                detail = ", ".join(unreachable)
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="<page>",
                    issue=(
                        f"Tab walk did not reach {len(unreachable)} of "
                        f"~{total_interactive} interactive elements. "
                        f"Unreachable: {detail}"
                    ),
                    impact=(
                        "Keyboard-only users cannot access these interactive "
                        "elements via the Tab key."
                    ),
                    recommendation=(
                        "Ensure all interactive elements (links, buttons, form fields) "
                        "are keyboard-focusable. Use native HTML elements or add "
                        "tabindex='0' with keyboard event handlers."
                    ),
                    severity=Severity.HIGH,
                ))
            elif unreachable and walk_halted_by_trap:
                logger.info(
                    "SC 2.1.1: tab walk halted by trap -- suppressing "
                    "%d-element unreachability finding (artifact of "
                    "trap, not a genuine SC 2.1.1 violation; the trap "
                    "is reported under SC 2.1.2)",
                    len(unreachable),
                )

        # --- tabindex="0" + onclick without keyboard handler on non-native elements ---
        for field in capture_data.form_fields:
            selector = field.get("selector", "element")
            tag = (field.get("tag") or field.get("tagName") or "").lower()
            tabindex = field.get("tabindex")
            has_click = field.get("has_onclick", False)
            has_keydown = field.get("has_onkeydown", False) or field.get("has_onkeypress", False)

            if (
                tag not in _INTERACTIVE_TAGS
                and str(tabindex) == "0"
                and has_click
                and not has_keydown
            ):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"<{tag}> has tabindex=\"0\" and onclick but no "
                        f"onkeydown/onkeypress handler"
                    ),
                    impact=(
                        "The element is focusable via keyboard but cannot be "
                        "activated with Enter or Space. Keyboard users can reach "
                        "it but not use it."
                    ),
                    recommendation=(
                        "Add an onkeydown or onkeypress handler that activates "
                        "the element on Enter and/or Space, or use a native "
                        "<button> element instead."
                    ),
                    severity=Severity.HIGH,
                ))

        # --- Enhanced coverage from tab_coverage data (Option A) ------
        # The richer tab_coverage now distinguishes two root causes:
        #   focusable_but_skipped: element.focus() works, Tab skips it.
        #                          → keyboard-only users can't reach it,
        #                            but mouse/AT users can. Strong SC
        #                            2.1.1 finding with a clear fix
        #                            (add tabindex="0" or fix Tab order).
        #   not_focusable_at_all:  element.focus() does nothing. The
        #                          element is interactive semantically
        #                          (has onclick/role/etc.) but is inert.
        #                          Different root cause -- disabled,
        #                          hidden ancestor, negative tabindex
        #                          without a programmatic path.
        # Each bucket gets its OWN finding with a specific fix.
        cov = capture_data.tab_coverage
        if cov and cov.get("total_interactive", 0) > 0:
            total = cov["total_interactive"]
            reached = cov["reached_by_tab"]
            pct = cov.get("coverage_percent", 0)
            focusable_skipped = cov.get("focusable_but_skipped", []) or []
            not_focusable = cov.get("not_focusable_at_all", []) or []
            # When a keyboard trap halted the tab walk, the
            # focusable_but_skipped list is contaminated -- it includes
            # every element the walk would have reached AFTER the trap,
            # which the test physically could not visit. Those are not
            # SC 2.1.1 violations; they are artifacts of the SC 2.1.2
            # trap. Suppress the focusable_but_skipped finding entirely
            # when this happens. The trap itself is reported under SC
            # 2.1.2, and once the trap is fixed, a re-run will produce
            # accurate Tab coverage data.
            if cov.get("halted_by_trap"):
                logger.info(
                    "SC 2.1.1: tab walk halted by detected keyboard "
                    "trap -- suppressing focusable_but_skipped (%d) and "
                    "low-coverage findings; the trap is the real issue "
                    "and is reported under SC 2.1.2",
                    len(focusable_skipped),
                )
                focusable_skipped = []
                # not_focusable_at_all is computed by direct
                # element.focus() probing, not by walking, so it is
                # still valid even when the walk was halted -- keep it.

            # A: elements that CAN be focused programmatically but Tab
            #    never visits them -- a Tab-order fix
            if focusable_skipped:
                preview = ", ".join(
                    e.get("selector", "?") for e in focusable_skipped
                )
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="<page>",
                    issue=(
                        f"{len(focusable_skipped)} interactive element(s) "
                        f"are focusable programmatically but the Tab key "
                        f"does not reach them: {preview}"
                    ),
                    impact=(
                        "Keyboard-only users cannot reach these elements "
                        "using Tab, even though they accept focus when "
                        "addressed by other means (screen reader, click). "
                        "This blocks SC 2.1.1 keyboard access."
                    ),
                    recommendation=(
                        "Add these elements to the Tab order: either set "
                        "tabindex=\"0\" (for custom widgets) or verify the "
                        "element's ancestor is not using display/visibility "
                        "rules that exclude it from the focus tree. Native "
                        "controls (<button>, <a href>, <input>) should "
                        "need no tabindex at all."
                    ),
                    severity=Severity.HIGH,
                ))

            # B: elements that appear interactive (role/onclick/etc.) but
            #    cannot be focused at all -- a focus-path fix
            if not_focusable:
                preview = ", ".join(
                    e.get("selector", "?") for e in not_focusable
                )
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="<page>",
                    issue=(
                        f"{len(not_focusable)} element(s) look interactive "
                        f"(role, onclick, etc.) but cannot receive focus: "
                        f"{preview}"
                    ),
                    impact=(
                        "These elements have interactive semantics but no "
                        "keyboard focus path. Keyboard and screen-reader "
                        "users cannot activate them."
                    ),
                    recommendation=(
                        "Replace custom interactive elements with native "
                        "controls (<button>, <a href>), OR add tabindex="
                        "\"0\" and a keyboard event handler (Enter/Space) "
                        "to make them keyboard-operable."
                    ),
                    severity=Severity.HIGH,
                ))

            # C: coverage percent summary when coverage is low but nothing
            #    above was categorised. Skipped entirely when the walk
            #    was truncated -- on a single-page app whose Tab walk
            #    hit MAX_TAB_ITERATIONS, reached_by_tab is a lower
            #    bound and the missing percentage is a walk artefact,
            #    not a real SC 2.1.1 violation. The deterministic
            #    focusable_but_skipped / not_focusable_at_all buckets
            #    already carry the real findings for such sites.
            walk_truncated = bool(cov.get("walk_truncated"))
            if (
                not focusable_skipped
                and not not_focusable
                and pct < 100
                and not walk_truncated
            ):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="<page>",
                    issue=(
                        f"Tab coverage incomplete: {reached} of {total} "
                        f"interactive elements reached ({pct:.0f}%)"
                    ),
                    impact=(
                        f"{total - reached} interactive element(s) not "
                        f"reached by Tab during the automated walk. Root "
                        f"cause uncategorised -- manual inspection needed."
                    ),
                    recommendation=(
                        "Review the Tab walk transcript and confirm "
                        "whether missed elements are keyboard-operable "
                        "through an alternate mechanism (arrow keys, "
                        "Space, Enter)."
                    ),
                    severity=Severity.MEDIUM,
                ))

        # --- Backward tab walk trap detection ---
        if capture_data.backward_tab_walk:
            backward_traps = [
                t for t in capture_data.keyboard_traps
                if t.get("direction") == "backward"
            ]
            for trap in backward_traps:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=trap.get("selector", trap.get("selectors", ["<element>"])[0] if isinstance(trap.get("selectors"), list) else "<element>"),
                    issue=(
                        f"Backward keyboard trap (Shift+Tab): "
                        f"{trap.get('description', 'focus stuck')}"
                    ),
                    impact=(
                        "Users pressing Shift+Tab to navigate backward get "
                        "trapped and cannot move past this element."
                    ),
                    recommendation=(
                        "Ensure backward keyboard navigation is not trapped. "
                        "Shift+Tab must move focus to the previous element."
                    ),
                    severity=Severity.HIGH,
                ))

        # --- Modal interactions: did triggers open via keyboard? ------
        # A modal trigger that only responds to a mouse click and NOT
        # to Enter/Space is a SC 2.1.1 failure -- keyboard users
        # can't reach the content the trigger gates.
        for mi in getattr(capture_data, "modal_interactions", []) or []:
            opened_enter = mi.get("opened_by_enter", False)
            opened_space = mi.get("opened_by_space", False)
            if opened_enter or opened_space:
                continue  # fine
            trig_text = mi.get("trigger_text", "") or "(no text)"
            trig_sel = mi.get("trigger_selector", "")
            findings.append(Finding(
                id=_make_finding_id(),
                element=trig_sel or "<modal trigger>",
                issue=(
                    f"Modal trigger \"{trig_text}\" did not open its "
                    f"dialog via Enter or Space. Keyboard users cannot "
                    f"activate this control."
                ),
                impact=(
                    "Keyboard-only users cannot open the modal, so the "
                    "content and actions inside are unreachable."
                ),
                recommendation=(
                    "Add a keyboard handler (Enter and Space) on the "
                    "trigger that opens the dialog. If the trigger is "
                    "a <button>, verify the click listener is also "
                    "bound to keydown Enter/Space or migrate to a "
                    "native <button> element."
                ),
                severity=Severity.HIGH,
            ))

        # --- Widget keyboard: keys that should respond but don't -----
        # capture/_capture_widget_keyboard records per-widget per-key
        # response data. A widget where NO key responded is a SC 2.1.1
        # failure; a widget where arrow keys work but the full walk
        # didn't reach every item is a SC 2.4.3 order failure, flagged
        # separately (milder severity).
        for wk in getattr(capture_data, "widget_keyboard", []) or []:
            # A crashed probe (every key_result errored) is inconclusive,
            # not a measured failure — never emit a finding from it.
            if widget_probe_errored(wk):
                continue
            any_responded = wk.get("any_key_responded", True)
            all_items_reached = wk.get("all_items_reached")
            wtype = wk.get("type", "widget")
            wsel = wk.get("selector", "")
            items_count = wk.get("items_count", 0)
            if any_responded is False:
                keys_tested = wk.get("keys_tested") or []
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=wsel or f"<{wtype}>",
                    issue=(
                        f"{wtype.capitalize()} widget did not respond "
                        f"to any of its expected keys "
                        f"({', '.join(keys_tested)})."
                    ),
                    impact=(
                        f"Keyboard users cannot navigate within this "
                        f"{wtype} as defined by the WAI-ARIA authoring "
                        f"practices."
                    ),
                    recommendation=(
                        f"Implement the WAI-ARIA keyboard pattern for "
                        f"{wtype}: handle arrow keys to move between "
                        f"items, Enter/Space to activate, Escape to "
                        f"close (where applicable)."
                    ),
                    severity=Severity.HIGH,
                ))
            elif all_items_reached is False and items_count > 1:
                distinct = wk.get("distinct_items_reached", 0)
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=wsel or f"<{wtype}>",
                    issue=(
                        f"{wtype.capitalize()} widget has {items_count} "
                        f"items but the arrow-key walk only reached "
                        f"{distinct} distinct item(s)."
                    ),
                    impact=(
                        "Keyboard users cannot navigate to every item "
                        "in the widget; some are unreachable by "
                        "keyboard alone."
                    ),
                    recommendation=(
                        f"Verify every {wtype} item responds to the "
                        f"primary navigation key (ArrowDown or "
                        f"ArrowRight depending on widget type) so "
                        f"focus walks through the whole list."
                    ),
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.85 if capture_data.tab_coverage else 0.75
        return conformance, confidence, findings

    def _generate_tt_results(
        self, findings: list[Finding], capture_data: CaptureData
    ) -> list[TTSubTestResult]:
        not_app = not self.is_applicable(capture_data)
        focusable_fail = any(
            "not keyboard focusable" in f.issue.lower() or "not focusable" in f.issue.lower()
            for f in findings if f.severity in (Severity.HIGH, Severity.MEDIUM)
        )
        operable_fail = any(
            "keyboard event" in f.issue.lower() or "cannot be activated" in f.issue.lower()
            or "did not open" in f.issue.lower() or "did not respond" in f.issue.lower()
            for f in findings if f.severity in (Severity.HIGH, Severity.MEDIUM)
        )
        return [
            TTSubTestResult(
                tt_id="1.A",
                name="Interactive elements are keyboard focusable",
                result=TTResult.DNA if not_app else TTResult.FAIL if focusable_fail else TTResult.PASS,
            ),
            TTSubTestResult(
                tt_id="1.B",
                name="Interactive elements are keyboard operable",
                result=TTResult.DNA if not_app else TTResult.FAIL if operable_fail else TTResult.PASS,
            ),
        ]


class Check_2_1_2(BaseCheck):
    """SC 2.1.2 No Keyboard Trap (Level A)."""

    criterion_id = "2.1.2"
    criterion_name = "No Keyboard Trap"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.1 Keyboard Accessible"
    principle = "2. Operable"
    ict_baseline = "1"
    tt_tests = ["1.C"]
    normative_text = (
        "If keyboard focus can be moved to a component of the page using "
        "a keyboard interface, then focus can be moved away from that "
        "component using only a keyboard interface, and, if it requires "
        "more than unmodified arrow or tab keys or other standard exit "
        "methods, the user is advised of the method for moving focus away."
    )
    web_only = True

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send the keyboard walkthrough video — the AI can see if focus
        gets stuck, if Escape doesn't close popups, etc."""
        if capture_data.keyboard_walkthrough_video:
            return capture_data.keyboard_walkthrough_video
        return None

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.tab_walk or capture_data.keyboard_traps)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Collect skip link selectors/text to filter false positives
        _skip_selectors = set()
        _skip_texts = set()
        for sl in (capture_data.skip_links or []):
            s = sl.get("selector", "")
            t = (sl.get("text") or "").lower()
            if s:
                _skip_selectors.add(s)
            if t:
                _skip_texts.add(t)
        for sl in (capture_data.skip_link_results or []):
            s = sl.get("selector", "")
            t = (sl.get("text") or "").lower()
            if s:
                _skip_selectors.add(s)
            if t:
                _skip_texts.add(t)

        # Direct keyboard trap detection
        for trap in capture_data.keyboard_traps:
            trap_type = trap.get("type", "unknown")
            # Resolve selector — traps may use "selector" (single) or
            # "selectors" (list, e.g. for cycle traps between two elements).
            selector = trap.get("selector", "")
            if not selector:
                sel_list = trap.get("selectors")
                if isinstance(sel_list, list) and sel_list:
                    selector = sel_list[0]
                else:
                    selector = "element"

            can_exit = trap.get("can_exit", False)
            exit_instructions = trap.get("exit_instructions", "")
            description = trap.get("description", "")

            # Skip link focus redirection is not a keyboard trap
            trap_text = (trap.get("text") or description).lower()
            if (selector in _skip_selectors
                    or "skip" in trap_text
                    or any(st in trap_text for st in _skip_texts if st)):
                continue

            # Trap types from the tab-walk capture: "consecutive",
            # "cycle", "frequency_cycle" — these are always real traps
            # detected by the automated walk.
            if trap_type in ("consecutive", "cycle", "frequency_cycle"):
                detail = description or f"{trap_type} trap"
                if trap_type == "cycle":
                    cycle_sels = trap.get("selectors", [])
                    if cycle_sels:
                        detail = f"Focus cycles between: {', '.join(cycle_sels)}"
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"Keyboard trap detected during tab walk ({trap_type}): "
                        f"{detail}"
                    ),
                    impact=(
                        "Keyboard users become stuck and cannot navigate "
                        "to other parts of the page."
                    ),
                    recommendation=(
                        "Ensure Tab and Shift+Tab allow the user to move "
                        "focus through all page content without getting trapped."
                    ),
                    severity=Severity.HIGH,
                ))
            elif not can_exit:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=f"Keyboard trap detected ({trap_type}): focus cannot be moved away",
                    impact=(
                        "Keyboard users become stuck and cannot navigate "
                        "to other parts of the page."
                    ),
                    recommendation=(
                        "Ensure Tab, Shift+Tab, or Escape allows the user "
                        "to move focus out of the component."
                    ),
                    severity=Severity.HIGH,
                ))
            elif not exit_instructions:
                # can_exit is True here: focus CAN be moved away, so SC
                # 2.1.2's core requirement (not trapped) is already met
                # -- this is NOT a keyboard-trap failure. The capture
                # probe adjudicates these itself; when its description
                # says the component passes 2.1.2, do not emit a finding
                # that contradicts the capture's own verdict. Otherwise
                # record an INFO advisory: a missing exit advisement is a
                # minor enhancement, never a 2.1.2 fail. Verified bug
                # (a county-government site's SC 2.1.2 run): a trap entry whose description
                # said "Passes 2.1.2" was emitted as a hard finding.
                if "passes 2.1.2" in description.lower():
                    continue
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"Focus can be moved away from this {trap_type} "
                        f"component, but the exit method is not advertised "
                        f"to the user"
                    ),
                    impact=(
                        "Users can still exit the component; advertising "
                        "the exit method would improve discoverability."
                    ),
                    recommendation=(
                        "Optionally provide visible instructions on how to "
                        "move focus out of this component."
                    ),
                    severity=Severity.INFO,
                ))

        # Deterministic trap detection already happened in the capture:
        # ``capture_data.keyboard_traps`` holds entries of type
        # ``consecutive`` (same element focused N times in a row),
        # ``cycle`` (A-B-A-B pattern), and ``frequency_cycle`` (same
        # element dominates a sliding window). Those are the only
        # signals that actually describe a focus trap -- a component
        # that captures focus and will not release it on Tab.
        #
        # A pure "element appears >= 3 times in the full walk" count is
        # NOT a trap detector: on any long single-page-app whose Tab
        # walk hits MAX_TAB_ITERATIONS, every element cycles many
        # times. That heuristic generated tens of false-positive medium
        # findings per truncated walk (see tab_walk_truncated). It has
        # been removed; the deterministic trap types above cover the
        # real case.

        # --- Modal escape + focus return (Option A + modal roundtrip)
        # SC 2.1.2 is specifically about being able to MOVE AWAY from
        # a focused component via keyboard. Modal dialogs are a focus
        # trap by design -- that's required behaviour per WAI-ARIA --
        # but the trap must be EXITABLE via Escape. An open modal that
        # Escape does not close IS a keyboard trap under 2.1.2.
        for mi in getattr(capture_data, "modal_interactions", []) or []:
            # Only evaluate modals that actually opened (otherwise the
            # trap/escape data is None and the issue belongs to 2.1.1).
            if not (mi.get("opened_by_enter") or mi.get("opened_by_space")):
                continue
            trig_text = mi.get("trigger_text", "") or "(no text)"
            trig_sel = mi.get("trigger_selector", "")
            modal_sel = mi.get("modal_selector_found", "")
            escape_closes = mi.get("escape_closes")
            focus_returned = mi.get("focus_returned_to_trigger")
            focus_trap_ok = mi.get("focus_trap_ok")

            if escape_closes is False:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=modal_sel or trig_sel or "<modal>",
                    issue=(
                        f"Modal dialog \"{trig_text}\" does not close "
                        f"when Escape is pressed."
                    ),
                    impact=(
                        "Keyboard users are trapped inside the modal "
                        "with no standard exit. Escape is the WAI-ARIA "
                        "dialog pattern's required dismiss mechanism."
                    ),
                    recommendation=(
                        "Handle the Escape keydown event on the modal "
                        "dialog and close it (remove focus trap, hide "
                        "the dialog, and return focus to the trigger)."
                    ),
                    severity=Severity.HIGH,
                ))
            # Focus-return after close is a SC 2.4.3 (focus order) issue
            # rather than 2.1.2, but report it here alongside the modal
            # data since that is where the roundtrip lives. Marked
            # MEDIUM because the user CAN still operate the page --
            # focus lands somewhere reasonable most of the time -- but
            # the correct behaviour is to return to the trigger.
            if escape_closes is True and focus_returned is False:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=trig_sel or "<modal trigger>",
                    issue=(
                        f"After closing modal \"{trig_text}\" with "
                        f"Escape, focus did NOT return to the trigger "
                        f"button. This is a SC 2.4.3 focus-order issue."
                    ),
                    impact=(
                        "Keyboard user loses their place in the page "
                        "after the modal closes, must re-orient."
                    ),
                    recommendation=(
                        "After dismissing the modal (Escape or "
                        "close button), call .focus() on the trigger "
                        "element so keyboard users resume where they "
                        "started."
                    ),
                    severity=Severity.MEDIUM,
                ))
            # Focus-trap failure: modal opened but Tab escaped it.
            # This is NOT a 2.1.2 issue (opposite direction), it's
            # 4.1.2 / 2.4.3 -- a dialog should trap focus by spec.
            # Still valuable to surface here because the trap is a
            # pre-condition for the Escape-dismiss test.
            if focus_trap_ok is False:
                outside = mi.get("tabstops_outside_modal", 0)
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=modal_sel or "<modal>",
                    issue=(
                        f"Modal dialog \"{trig_text}\" does not trap "
                        f"focus: {outside} Tab press(es) moved focus "
                        f"outside the dialog."
                    ),
                    impact=(
                        "Keyboard users can Tab out of the modal while "
                        "it is still open, breaking the WAI-ARIA "
                        "dialog pattern and potentially interacting "
                        "with elements the user cannot see."
                    ),
                    recommendation=(
                        "Implement a focus trap: on Tab from the last "
                        "focusable element in the modal, move focus to "
                        "the first; on Shift+Tab from the first, move "
                        "to the last."
                    ),
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.8 if capture_data.keyboard_traps else 0.6
        return conformance, confidence, findings


class Check_2_1_4(BaseCheck):
    """SC 2.1.4 Character Key Shortcuts (Level A, WCAG 2.1/2.2)."""

    criterion_id = "2.1.4"
    criterion_name = "Character Key Shortcuts"
    # Applicability depends on whether the page implements key shortcuts —
    # a meaning judgment, not mechanical element existence.
    ai_judged_applicability = True
    level = "A"
    wcag_versions = ["2.1", "2.2"]
    guideline = "2.1 Keyboard Accessible"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "If a keyboard shortcut is implemented in content using only "
        "letter (including upper- and lower-case letters), punctuation, "
        "number, or symbol characters, then at least one of the following "
        "is true: Turn off, Remap, Active only on focus."
    )
    web_only = True

    def is_applicable(self, capture_data: CaptureData) -> bool:
        # Check for single-character shortcut patterns in scripts
        return bool(capture_data.script_content)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        script = capture_data.script_content or ""

        # Look for single-character keyboard shortcut patterns
        # Pattern: checking for key === "x" (single character)
        single_char_patterns = [
            r"(?:key|char|keyCode)\s*===?\s*['\"]([a-zA-Z0-9])['\"]",
            r"(?:key|code)\s*===?\s*['\"]Key([A-Z])['\"]",
            r"accesskey\s*=\s*['\"]([a-zA-Z0-9])['\"]",
        ]

        html = capture_data.html or ""

        for pattern in single_char_patterns:
            matches = re.findall(pattern, script + html, re.IGNORECASE)
            for m in matches:
                char = m if isinstance(m, str) else m[0]
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="<script>",
                    issue=(
                        f"Possible single-character keyboard shortcut "
                        f"detected: '{char}'"
                    ),
                    impact=(
                        "Speech input users may accidentally trigger shortcuts. "
                        "Users with motor disabilities may hit single keys "
                        "unintentionally."
                    ),
                    recommendation=(
                        "Ensure single-character shortcuts can be turned off, "
                        "remapped, or are only active when the component has focus."
                    ),
                    severity=Severity.MEDIUM,
                ))

        # Check for accesskey attributes in HTML
        accesskey_pattern = r"accesskey\s*=\s*['\"]([^'\"]+)['\"]"
        accesskeys = re.findall(accesskey_pattern, html, re.IGNORECASE)
        for ak in accesskeys:
            findings.append(Finding(
                id=_make_finding_id(),
                element=f"[accesskey=\"{ak}\"]",
                issue=f"Element uses accesskey=\"{ak}\" which is a character key shortcut",
                impact=(
                    "Accesskeys can conflict with screen reader or browser "
                    "shortcuts and may be triggered unintentionally."
                ),
                recommendation=(
                    "Ensure the shortcut can be turned off or remapped, "
                    "or remove the accesskey attribute."
                ),
                severity=Severity.LOW,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.5  # Script analysis is heuristic
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_2_1_1(),
        Check_2_1_2(),
        Check_2_1_4(),
    ]
