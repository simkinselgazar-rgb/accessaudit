"""WCAG Guideline 2.4 - Navigable (A/AA) checks."""
from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin, urlparse

from checks.base import BaseCheck, _make_finding_id


def _normalize_href(href: str, page_url: str = "") -> str:
    """Resolve a link href to a comparable canonical form.

    /news, https://www.example.edu/news, www.example.edu/news, and #/news (when
    page_url is example.edu) all collapse to the same value so that 2.4.4's
    duplicate-text check doesn't flag links that go to the same place.
    Drops fragment, normalises trailing slash on the path, lowercases
    the host (paths + queries are case-sensitive on the server side).
    """
    if not href:
        return ""
    h = href.strip()
    # Skip unhelpful scheme-only / synthetic hrefs
    if h.startswith(("javascript:", "mailto:", "tel:", "data:")):
        return h
    if h in ("#", ""):
        return ""
    if h.startswith("route:"):
        return h
    # Resolve relative against page URL
    try:
        absolute = urljoin(page_url or "", h) if page_url else h
        absolute, _frag = urldefrag(absolute)
        parsed = urlparse(absolute)
        host = (parsed.netloc or "").lower()
        path = parsed.path or ""
        if path.endswith("/"):
            path = path[:-1]
        if not parsed.scheme and not host and not path:
            return absolute
        scheme = parsed.scheme or "https"
        result = f"{scheme}://{host}{path}"
        if parsed.query:
            result += f"?{parsed.query}"
        return result
    except Exception:
        return h
from models import (
    CaptureData,
    ConformanceLevel,
    Finding,
    Severity,
    TTResult,
    TTSubTestResult,
)

# Objectively non-descriptive link texts — these are NEVER meaningful
# regardless of context.  Borderline cases like "details", "learn more",
# "continue" are left to the AI to evaluate in context (e.g., "Learn
# More" in a card with a visible heading may be acceptable per SC 2.4.4
# when programmatic context provides the link purpose).
_VAGUE_LINK_TEXTS = {
    "click here", "here", "click", "link", "this", "go",
}


class Check_2_4_1(BaseCheck):
    """SC 2.4.1 Bypass Blocks (Level A)."""

    criterion_id = "2.4.1"
    criterion_name = "Bypass Blocks"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    ict_baseline = "4"
    tt_tests = ["4.A", "4.B"]
    normative_text = (
        "A mechanism is available to bypass blocks of content that are "
        "repeated on multiple Web pages."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        has_skip_link = bool(capture_data.skip_links)
        has_main_landmark = any(
            (lm.get("role") or "").lower() == "main"
            or (lm.get("tag") or lm.get("tagName") or "").lower() == "main"
            for lm in capture_data.landmarks
        )
        has_nav_landmark = any(
            (lm.get("role") or "").lower() == "navigation"
            or (lm.get("tag") or lm.get("tagName") or "").lower() == "nav"
            for lm in capture_data.landmarks
        )
        has_headings = bool(capture_data.headings)

        # At least one bypass mechanism should exist
        if not has_skip_link and not has_main_landmark and not has_headings:
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue=(
                    "No bypass mechanism found: no skip link, no main "
                    "landmark, and no heading structure"
                ),
                impact=(
                    "Keyboard users must tab through repeated navigation "
                    "on every page, significantly slowing navigation."
                ),
                recommendation=(
                    "Add a skip navigation link (e.g., 'Skip to main content'), "
                    "use a <main> landmark, or provide heading-based navigation."
                ),
                severity=Severity.HIGH,
            ))

        # Check skip link functionality. Schema (produced by
        # _skip_link_verification): keyboard_activates,
        # click_activates, focus_after_keyboard, focus_after_click,
        # focus_landed_on_target, is_first_tabstop, error.
        if capture_data.skip_link_results:
            for sl in capture_data.skip_link_results:
                text = sl.get("skip_link_text", "") or sl.get("text", "")
                selector = sl.get("skip_link_selector", "") or sl.get("selector", "")
                target_href = sl.get("target_href", "")
                kb_ok = sl.get("keyboard_activates", None)
                click_ok = sl.get("click_activates", None)
                err = sl.get("error")

                # Keyboard is the SC 2.4.1 requirement. A skip link
                # that only works with a mouse click is not compliant.
                if kb_ok is False:
                    if click_ok is True:
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=selector or "skip link",
                            issue=(
                                f"Skip link \"{text}\" works with a mouse click "
                                f"but does NOT work via keyboard (Enter key)."
                            ),
                            impact=(
                                "Keyboard-only users cannot bypass the block of "
                                "repeated content -- defeating the purpose of "
                                "the skip link."
                            ),
                            recommendation=(
                                f"Ensure the target ({target_href}) receives "
                                f"focus when the skip link is activated via "
                                f"keyboard. Typically this means adding "
                                f"tabindex=\"-1\" to the target element so it "
                                f"can accept programmatic focus."
                            ),
                            severity=Severity.HIGH,
                        ))
                    else:
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=selector or "skip link",
                            issue=(
                                f"Skip link \"{text}\" does not move focus to "
                                f"its target ({target_href}) via either mouse "
                                f"or keyboard activation."
                            ),
                            impact=(
                                "Skip link is non-functional. Keyboard users "
                                "cannot bypass repeated navigation."
                            ),
                            recommendation=(
                                "Verify the target element id exists and is "
                                "keyboard-focusable. Add tabindex=\"-1\" to "
                                "the target if it is not natively focusable."
                            ),
                            severity=Severity.HIGH,
                        ))
                elif err:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector or "skip link",
                        issue=(
                            f"Skip link \"{text}\" verification raised an "
                            f"error: {err}"
                        ),
                        impact=(
                            "Skip link behaviour could not be verified -- "
                            "manual testing required."
                        ),
                        recommendation=(
                            "Confirm the skip link works end-to-end using a "
                            "keyboard (Tab to focus, Enter to activate, "
                            "verify focus lands in main content)."
                        ),
                        severity=Severity.LOW,
                    ))

        # First-tabstop check using the new skip_link_first_tabstop
        # context. Prefer this over the older tab_walk[0] comparison --
        # the new context already knows whether the first tab stop
        # qualifies as a skip link by text + class hints.
        # CROSS-CHECK with tab_walk[0]: if the actual tab walk shows
        # the skip link IS the first stop (its text contains
        # 'skip'/'jump to'/'main content'), trust that over the probe.
        # On 2026-04-28 university-site run, the probe reported first_tab_is_skip=
        # False with empty first_tab_selector while tab_walk[0] clearly
        # showed text="Skip to main content" — produced a false-positive
        # MEDIUM finding. Also: WCAG 2.4.1 does not strictly require the
        # skip link to be the FIRST tab stop, only that it is reachable
        # by keyboard before the main content. Demote to INFO and skip
        # entirely when the probe is inconsistent with tab_walk.
        first_tab_ctx = getattr(capture_data, "skip_link_first_tabstop", None) or {}
        has_any_skip = first_tab_ctx.get("any_skip_link_found", False)
        first_is_skip = first_tab_ctx.get("first_tab_is_skip", False)
        first_sel = first_tab_ctx.get("first_tab_selector", "")
        first_text = first_tab_ctx.get("first_tab_text", "")

        tab_walk = getattr(capture_data, "tab_walk", None) or []
        tw_first_text = ""
        if tab_walk and isinstance(tab_walk[0], dict):
            tw_first_text = (tab_walk[0].get("text") or "").strip().lower()
        SKIP_HINTS = ("skip to main", "skip to content", "skip navigation",
                      "skip nav", "jump to main", "jump to content")
        tw_first_is_skip = any(h in tw_first_text for h in SKIP_HINTS)

        # If the probe contradicts tab_walk (probe says no, walk says yes),
        # trust tab_walk and DON'T fire this finding.
        probe_unreliable = (not first_sel and not first_text)
        if has_skip_link and has_any_skip and not first_is_skip \
                and not tw_first_is_skip and not probe_unreliable:
            findings.append(Finding(
                id=_make_finding_id(),
                element=first_sel or "(first tab stop)",
                issue=(
                    f"Skip link exists on the page but is NOT the first "
                    f"tab stop. First focusable element is {first_sel} "
                    f"(\"{first_text}\"). Note: WCAG 2.4.1 does not "
                    f"strictly require first-tab-stop placement; this is "
                    f"a usability recommendation."
                ),
                impact=(
                    "Keyboard users must tab through other elements "
                    "before reaching the skip link, reducing its value."
                ),
                recommendation=(
                    "Place the skip link so it is the first focusable "
                    "element in the DOM -- typically right after <body>."
                ),
                severity=Severity.LOW,
            ))

        # Check iframes have titles
        for iframe in capture_data.iframes:
            selector = iframe.get("selector", "iframe")
            title = iframe.get("title", "")
            if not title:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Iframe has no title attribute",
                    impact="Screen reader users cannot identify the iframe content.",
                    recommendation="Add a descriptive title attribute to the iframe.",
                    severity=Severity.MEDIUM,
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.8
        return conformance, confidence, findings

    def _generate_tt_results(
        self, findings: list[Finding], capture_data: CaptureData
    ) -> list[TTSubTestResult]:
        no_bypass = any("no bypass" in f.issue.lower() for f in findings if f.severity == Severity.HIGH)
        iframe_fail = any("iframe" in f.issue.lower() for f in findings if f.severity in (Severity.HIGH, Severity.MEDIUM))
        return [
            TTSubTestResult(
                tt_id="4.A",
                name="Bypass mechanism exists",
                result=TTResult.FAIL if no_bypass else TTResult.PASS,
            ),
            TTSubTestResult(
                tt_id="4.B",
                name="Iframes have descriptive titles",
                result=(
                    TTResult.DNA if not capture_data.iframes
                    else TTResult.FAIL if iframe_fail
                    else TTResult.PASS
                ),
            ),
        ]


class Check_2_4_2(BaseCheck):
    """SC 2.4.2 Page Titled (Level A)."""

    criterion_id = "2.4.2"
    criterion_name = "Page Titled"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    ict_baseline = "11"
    tt_tests = ["11.A", "11.B"]
    normative_text = "Web pages have titles that describe topic or purpose."

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return True

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        """Programmatic check: verify the title EXISTS and is not empty.

        Title *quality* (is it descriptive? does it identify the page?)
        is a judgment call — the AI evaluates that.  Programmatic only
        catches the objective, binary failures.
        """
        findings: list[Finding] = []
        title = capture_data.title.strip() if capture_data.title else ""

        if not title:
            findings.append(Finding(
                id=_make_finding_id(),
                element="<head>",
                issue="Page has no <title> element or title is empty",
                impact=(
                    "Users cannot identify the page purpose from browser "
                    "tabs, bookmarks, or screen reader page lists."
                ),
                recommendation="Add a descriptive <title> element to the page.",
                severity=Severity.HIGH,
            ))
            return self._determine_conformance(findings), 0.95, findings

        # Title exists — programmatic can confirm that, but whether
        # the title is DESCRIPTIVE is a judgment call for the AI.
        # Set low confidence so the AI's evaluation carries weight.
        return ConformanceLevel.SUPPORTS, 0.5, findings

    def _generate_tt_results(
        self, findings: list[Finding], capture_data: CaptureData
    ) -> list[TTSubTestResult]:
        no_title = any("no <title>" in f.issue for f in findings)
        bad_title = any("generic" in f.issue.lower() for f in findings)
        return [
            TTSubTestResult(
                tt_id="11.A",
                name="Page has a title",
                result=TTResult.FAIL if no_title else TTResult.PASS,
            ),
            TTSubTestResult(
                tt_id="11.B",
                name="Title describes the page purpose",
                result=TTResult.FAIL if bad_title else TTResult.PASS,
            ),
        ]


class Check_2_4_3(BaseCheck):
    """SC 2.4.3 Focus Order (Level A)."""

    criterion_id = "2.4.3"
    criterion_name = "Focus Order"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    ict_baseline = "3"
    tt_tests = ["3.A"]
    normative_text = (
        "If a Web page can be navigated sequentially and the navigation "
        "sequences affect meaning or operation, focusable components "
        "receive focus in an order that preserves meaning and operability."
    )
    off_scope_keywords = {
        "focus_visible": [
            "onfocus", "blur()", "focus indicator", "focus visible",
            "outline:none", "outline: none",
        ],
    }
    web_only = True

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send the keyboard walkthrough video so the AI can watch
        focus moving through the page and verify the order is logical."""
        if capture_data.keyboard_walkthrough_video:
            return capture_data.keyboard_walkthrough_video
        return None

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.tab_walk)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        if not capture_data.tab_walk:
            return ConformanceLevel.NOT_EVALUATED, 0.3, findings

        # Check for positive tabindex values (override natural order)
        for t in capture_data.tab_walk:
            tabindex = t.get("tabindex")
            if tabindex is not None:
                try:
                    ti_val = int(tabindex)
                    if ti_val > 0:
                        findings.append(Finding(
                            id=_make_finding_id(),
                            element=t.get("selector", "element"),
                            issue=(
                                f"Element has positive tabindex={ti_val}, "
                                f"disrupting natural focus order"
                            ),
                            impact="Focus order may not match visual or logical order.",
                            recommendation="Remove positive tabindex; use DOM order instead.",
                            severity=Severity.MEDIUM,
                        ))
                except (ValueError, TypeError):
                    pass

        # Check if focus order roughly matches visual layout (top-to-bottom)
        prev_y = -1
        out_of_order_count = 0
        for t in capture_data.tab_walk:
            y = t.get("y", t.get("top", -1))
            if y < 0:
                continue
            if prev_y >= 0 and y < prev_y - 50:
                out_of_order_count += 1
            prev_y = y

        if out_of_order_count > 3:
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue=(
                    f"Focus order deviates significantly from visual layout "
                    f"({out_of_order_count} elements receive focus out of "
                    f"visual order)"
                ),
                impact=(
                    "Users navigating by keyboard may be confused by "
                    "unexpected focus movement."
                ),
                recommendation=(
                    "Ensure the DOM order matches the visual presentation "
                    "order, or use CSS to reorder visually without changing DOM."
                ),
                severity=Severity.MEDIUM,
            ))

        # Check expanded menu focus management: after Escape,
        # focus should return to the trigger element.
        #
        # The walkthrough log now records ``focus_is_trigger`` -- a
        # boolean computed in the browser by comparing the focused DOM
        # node directly to the trigger node (identity check). This is
        # the authoritative signal. We only fall back to the tag-name
        # heuristic for old captures that pre-date the
        # focus_is_trigger flag.
        import re as _re
        for entry in getattr(capture_data, "keyboard_walkthrough_log", []):
            if entry.get("action") != "escape_result":
                continue
            trigger = entry.get("element", "")
            focus_after = entry.get("focus_after_escape", "")
            focus_is_trigger = entry.get("focus_is_trigger")  # tri-state: True/False/None
            still_open = entry.get("dropdown_still_open", False)
            if not (trigger and focus_after):
                continue
            # If escape did not close the popup, that is a SC 1.4.13
            # dismissibility issue, not a SC 2.4.3 focus-order issue.
            if still_open:
                continue
            # Authoritative path: the JS close-check resolved both
            # selectors to DOM nodes and compared by identity.
            if focus_is_trigger is True:
                continue
            # Backward-compat path for captures predating focus_is_trigger.
            # Extract trigger tag and compare to focus_after tag.
            if focus_is_trigger is None:
                last_segment = trigger.split(">")[-1].strip()
                m = _re.match(r"^([a-zA-Z][a-zA-Z0-9]*)", last_segment)
                trigger_tag = (m.group(1) if m else "").lower()
                focus_tag = focus_after.strip().lower()
                if focus_tag and trigger_tag and focus_tag == trigger_tag:
                    continue
            findings.append(Finding(
                id=_make_finding_id(),
                element=trigger,
                issue=(
                    f"After closing an expanded widget with Escape, "
                    f"focus moved to '{focus_after}' instead of "
                    f"returning to the trigger element '{trigger}'"
                ),
                impact=(
                    "Keyboard users lose their place on the page "
                    "when focus does not return to the trigger."
                ),
                recommendation=(
                    "When a popup/menu is closed via Escape, return "
                    "focus to the element that opened it."
                ),
                severity=Severity.MEDIUM,
            ))

        conformance = self._determine_conformance(findings)
        confidence = 0.6
        return conformance, confidence, findings


class Check_2_4_4(BaseCheck):
    """SC 2.4.4 Link Purpose (In Context) (Level A)."""

    criterion_id = "2.4.4"
    criterion_name = "Link Purpose (In Context)"
    level = "A"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    ict_baseline = "14"
    tt_tests = ["14.A"]
    normative_text = (
        "The purpose of each link can be determined from the link text "
        "alone or from the link text together with its programmatically "
        "determined link context, except where the purpose of the link "
        "would be ambiguous to users in general."
    )
    off_scope_keywords = {
        "focus": ["focus indicator", "focus visible"],
        "keyboard": ["keyboard accessible"],
    }

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.links)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        for link in capture_data.links:
            # Skip hidden links (inside collapsed menus, etc.)
            if link.get("visible") is False:
                continue
            rect = link.get("rect", {})
            if rect and (rect.get("width", 0) <= 1 or rect.get("height", 0) <= 1):
                continue

            selector = link.get("selector", "a")
            text = (link.get("text") or "").strip()
            aria_label = (link.get("aria_label") or link.get("aria-label") or "").strip()
            aria_labelledby = (
                link.get("ariaLabelledby")
                or link.get("aria_labelledby")
                or link.get("aria-labelledby")
                or ""
            ).strip()
            title = (link.get("title") or "").strip()
            href = link.get("href", "")
            has_img = link.get("has_image", False)
            img_alt = (link.get("imgAlt") or link.get("image_alt") or "").strip()
            context = (link.get("context") or "").strip()

            # Determine effective link text (accessible name)
            effective_text = aria_label or text or title
            if not effective_text and has_img:
                effective_text = img_alt

            # --- Image-only links: check alt text quality ---
            if has_img and not text and not aria_label:
                if not img_alt and not aria_labelledby:
                    # Image link with no alt text at all
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Image link has no alt text"
                            + (f" (href=\"{href}\")" if href else "")
                        ),
                        impact=(
                            "Screen reader users will hear the link but "
                            "cannot determine its purpose."
                        ),
                        recommendation=(
                            "Add meaningful alt text to the image that "
                            "describes the link destination or purpose."
                        ),
                        severity=Severity.HIGH,
                    ))
                    continue
                elif img_alt and img_alt.lower() in _VAGUE_LINK_TEXTS:
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Image link has vague alt text: \"{img_alt}\""
                            + (f" (href=\"{href}\")" if href else "")
                        ),
                        impact=(
                            "Screen reader users hear a non-descriptive name "
                            "for this image link."
                        ),
                        recommendation=(
                            f"Replace alt=\"{img_alt}\" with text that "
                            f"describes the link destination or purpose."
                        ),
                        severity=Severity.MEDIUM,
                    ))
                    continue

            # --- Empty link text (no accessible name at all) ---
            if not effective_text and not aria_labelledby:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=(
                        f"Link has no accessible text"
                        + (f" (href=\"{href}\")" if href else "")
                    ),
                    impact="Screen reader users will not know the link purpose.",
                    recommendation=(
                        "Add descriptive text content, aria-label, or ensure "
                        "the image within has meaningful alt text."
                    ),
                    severity=Severity.HIGH,
                ))
                continue

            # --- Vague link text detection ---
            if effective_text.lower() in _VAGUE_LINK_TEXTS:
                # Determine whether supplementary accessible name or
                # programmatic context rescues the vague text.
                # SC 2.4.4 allows purpose to be determined from link text
                # *together with* its programmatically determined context.
                has_meaningful_title = bool(
                    title and title.lower() not in _VAGUE_LINK_TEXTS
                )
                has_meaningful_aria_label = bool(
                    aria_label and aria_label.lower() not in _VAGUE_LINK_TEXTS
                )
                has_meaningful_labelledby = bool(aria_labelledby)
                has_meaningful_context = bool(
                    context
                    and context.lower() not in _VAGUE_LINK_TEXTS
                    and len(context) > len(effective_text)
                )

                if has_meaningful_aria_label:
                    # aria-label overrides visible text for AT users --
                    # the accessible name is adequate. Flag as low because
                    # sighted users still see vague visible text.
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Link has vague visible text \"{text}\" but "
                            f"aria-label provides purpose: \"{aria_label}\""
                        ),
                        impact=(
                            "Sighted users see vague text; AT users hear the "
                            "descriptive aria-label. Visible text could be improved."
                        ),
                        recommendation=(
                            f"Update visible link text to be descriptive so "
                            f"all users benefit, not just AT users."
                        ),
                        severity=Severity.LOW,
                    ))
                elif has_meaningful_title:
                    # title attribute provides supplementary information
                    # (tooltip). Downgrade severity but still flag because
                    # title is not exposed by all AT consistently.
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Link text is vague: \"{effective_text}\" "
                            f"but title attribute provides context: \"{title}\""
                        ),
                        impact=(
                            "The title attribute may clarify the link purpose, "
                            "but it is not reliably exposed to all assistive "
                            "technology users (e.g., mobile screen readers)."
                        ),
                        recommendation=(
                            f"Replace \"{effective_text}\" with descriptive text, "
                            f"or use aria-label instead of title for broader AT support."
                        ),
                        severity=Severity.LOW,
                    ))
                elif has_meaningful_labelledby:
                    # aria-labelledby references another element that
                    # provides the accessible name. Downgrade.
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Link text is vague: \"{effective_text}\" "
                            f"but aria-labelledby references additional context"
                        ),
                        impact=(
                            "The aria-labelledby reference may provide adequate "
                            "link purpose for AT users."
                        ),
                        recommendation=(
                            f"Verify the referenced element provides a clear "
                            f"description. Consider making visible text "
                            f"descriptive as well."
                        ),
                        severity=Severity.LOW,
                    ))
                elif has_meaningful_context:
                    # Surrounding text (parent p/li/td) gives context.
                    # SC 2.4.4 allows this, but link lists strip context.
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Link text is vague: \"{effective_text}\" "
                            f"but surrounding content provides context"
                        ),
                        impact=(
                            "Users reading in context can infer the purpose, "
                            "but users navigating via a links list cannot."
                        ),
                        recommendation=(
                            f"Replace \"{effective_text}\" with descriptive "
                            f"text, or add aria-label describing the link purpose."
                        ),
                        severity=Severity.LOW,
                    ))
                else:
                    # Vague text with NO rescue from context, title, or ARIA
                    findings.append(Finding(
                        id=_make_finding_id(),
                        element=selector,
                        issue=(
                            f"Link text is vague: \"{effective_text}\" "
                            f"with no programmatic context, title, or "
                            f"aria-label to clarify purpose"
                        ),
                        impact=(
                            "Users navigating by links list cannot determine "
                            "where this link goes. No supplementary mechanism "
                            "provides the link purpose."
                        ),
                        recommendation=(
                            f"Replace \"{effective_text}\" with descriptive "
                            f"text, or add aria-label describing the link purpose."
                        ),
                        severity=Severity.HIGH,
                    ))

            # Duplicate link text going to different destinations
            # (tracked across all links)

        # Check for multiple links with same text but different targets.
        # Normalise hrefs first so that "/news" and
        # "https://www.example.edu/news" — which point to the same page on
        # example.edu — don't get counted as different destinations.
        # Track each matching link's selector so the finding can point
        # at the actual offending elements -- earlier this dropped the
        # selector entirely, leaving the judge unable to reference the
        # specific links and the audit script flagging "missing
        # css_selector". Verified gap on a university-site run f8d46924 SC 2.4.4 where
        # 10 duplicate-text findings all had empty selectors.
        page_url = getattr(capture_data, "url", "") or ""
        text_to_links: dict[str, dict[str, list[str]]] = {}
        for link in capture_data.links:
            text = (
                link.get("aria_label") or link.get("aria-label")
                or link.get("text") or ""
            ).strip().lower()
            href = link.get("href", "")
            sel = link.get("selector") or ""
            if text and href:
                normalized = _normalize_href(href, page_url)
                if normalized:
                    bucket = text_to_links.setdefault(
                        text, {"hrefs": set(), "selectors": []}
                    )
                    bucket["hrefs"].add(normalized)
                    if sel and sel not in bucket["selectors"]:
                        bucket["selectors"].append(sel)

        for text, bucket in text_to_links.items():
            hrefs = bucket["hrefs"]
            if len(hrefs) > 1 and text not in _VAGUE_LINK_TEXTS:
                # Build a comma-separated selector list so the finding
                # references every offending link. Cap at first 8
                # selectors to keep the finding readable; the count in
                # the issue text reflects the true total.
                sels = bucket["selectors"]
                shown = sels[:8]
                more = len(sels) - len(shown)
                sel_str = ", ".join(shown)
                if more > 0:
                    sel_str += f" (+{more} more)"
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=f"links with text \"{text}\" ({len(sels)} matched)",
                    issue=(
                        f"Multiple links with identical text \"{text}\" "
                        f"go to {len(hrefs)} different destinations, "
                        f"violating WCAG 2.4.4 Link Purpose (In Context)."
                    ),
                    impact=(
                        "Screen reader users on JAWS, NVDA, and VoiceOver "
                        "who navigate by link list (Insert+F7 / NVDA+F7 / "
                        "VO+U) hear identical link text and cannot "
                        "predict which destination each link leads to "
                        "without activating it."
                    ),
                    recommendation=(
                        "WCAG 2.4.4 requires the purpose of each link "
                        "to be determinable from the link text alone, "
                        "or from the link text together with its "
                        "programmatically-determined link context. "
                        "Differentiate the text or add aria-label / "
                        "aria-labelledby that distinguishes the targets."
                    ),
                    severity=Severity.LOW,
                    css_selector=sel_str,
                ))

        conformance = self._determine_conformance(findings, len(capture_data.links))
        confidence = 0.75
        return conformance, confidence, findings


class Check_2_4_5(BaseCheck):
    """SC 2.4.5 Multiple Ways (Level AA)."""

    criterion_id = "2.4.5"
    criterion_name = "Multiple Ways"
    level = "AA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    ict_baseline = ""
    tt_tests = []
    normative_text = (
        "More than one way is available to locate a Web page within a "
        "set of Web pages except where the Web Page is the result of, "
        "or a step in, a process."
    )

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.html)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []
        html_lower = (capture_data.html or "").lower()

        # Check for common navigation mechanisms
        has_nav = any(
            (lm.get("role") or "").lower() == "navigation"
            or (lm.get("tag") or lm.get("tagName") or "").lower() == "nav"
            for lm in capture_data.landmarks
        )
        # Also count a group of internal links as navigation even without <nav>
        if not has_nav and len(capture_data.links) >= 3:
            internal_links = [
                lnk for lnk in capture_data.links
                if (lnk.get("href") or "").startswith(("/", "#", "./"))
                or (lnk.get("href") or "").startswith(
                    (capture_data.url or "x://no-match").rsplit("/", 1)[0]
                )
            ]
            if len(internal_links) >= 3:
                has_nav = True

        has_search = bool(
            re.search(r'role\s*=\s*["\']search["\']', html_lower)
            or re.search(r'type\s*=\s*["\']search["\']', html_lower)
            or re.search(r'<form[^>]*search', html_lower)
            or re.search(r'<input[^>]*search', html_lower)
        )

        has_sitemap_link = bool(
            re.search(r'href\s*=\s*["\'][^"\']*site.?map', html_lower)
        )

        has_toc = bool(
            re.search(r'(?:table.of.contents|toc|page.index)', html_lower)
        )

        mechanisms = []
        if has_nav:
            mechanisms.append("navigation")
        if has_search:
            mechanisms.append("search")
        if has_sitemap_link:
            mechanisms.append("sitemap")
        if has_toc:
            mechanisms.append("table of contents")

        if len(mechanisms) < 2:
            found_str = ", ".join(mechanisms) if mechanisms else "none"
            findings.append(Finding(
                id=_make_finding_id(),
                element="body",
                issue=(
                    f"Only {len(mechanisms)} navigation mechanism(s) detected "
                    f"on this page: {found_str}. SC 2.4.5 requires at least 2 "
                    f"ways to locate a page within a set of pages. Note: this "
                    f"check requires a site crawl for full verification — "
                    f"search, sitemap, or A-Z index may exist on other pages."
                ),
                impact=(
                    "Users with different abilities may prefer different "
                    "navigation strategies."
                ),
                recommendation=(
                    "Provide at least two of: site navigation, search, "
                    "sitemap, table of contents, or A-Z index."
                ),
                # LOW severity on single page — can't fully verify
                severity=Severity.LOW,
            ))

        conformance = self._determine_conformance(findings)
        # SC 2.4.5 fundamentally requires a multi-page crawl to verify
        # ("more than one way is available to locate a Web page within
        # a set of Web pages"). On a single-page audit we can confirm
        # the page has navigation/search/sitemap mechanisms, but we
        # cannot confirm site-wide consistency. The AI sees the same
        # single page and can't answer either. We accept the partial
        # answer with clear caveat — AI input would only duplicate
        # what the deterministic check already says, so promoted to
        # PROGRAMMATIC_DEFINITIVE 2026-04-29.
        # Confidence is high when ≥2 mechanisms are present (Supports
        # is robust); lower when fewer (the LOW finding's caveat text
        # already indicates manual verification is needed).
        confidence = 0.85 if len(mechanisms) >= 2 else 0.5
        return conformance, confidence, findings


class Check_2_4_6(BaseCheck):
    """SC 2.4.6 Headings and Labels (Level AA)."""

    criterion_id = "2.4.6"
    criterion_name = "Headings and Labels"
    level = "AA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    ict_baseline = "10"
    tt_tests = ["10.E"]
    normative_text = "Headings and labels describe topic or purpose."

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.headings or capture_data.form_fields)

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Check heading descriptiveness
        for h in capture_data.headings:
            selector = h.get("selector", "heading")
            text = (h.get("text") or "").strip()

            if not text:
                continue  # Empty headings covered by 1.3.1

            # Very short, possibly non-descriptive headings
            if len(text) == 1 or text.lower() in (".", "-", "*", "section"):
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=f"Heading has non-descriptive text: \"{text}\"",
                    impact="Users navigating by headings cannot determine content topic.",
                    recommendation="Use descriptive heading text that identifies the section content.",
                    severity=Severity.MEDIUM,
                ))

        # Check form labels are descriptive
        for field in capture_data.form_fields:
            selector = field.get("selector", "input")
            label = (field.get("label") or "").strip()
            field_type = (field.get("type") or "").lower()

            if field_type in ("hidden", "submit", "button", "reset"):
                continue

            if label and len(label) == 1:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue=f"Form label is not descriptive: \"{label}\"",
                    impact="Users cannot determine what information to enter.",
                    recommendation="Provide a descriptive label that explains the field purpose.",
                    severity=Severity.MEDIUM,
                ))

        # Check for duplicate headings at same level
        heading_texts: dict[str, int] = {}
        for h in capture_data.headings:
            tag = (h.get("tag") or h.get("tagName") or "").lower()
            text = (h.get("text") or "").strip().lower()
            if text:
                key = f"{tag}:{text}"
                heading_texts[key] = heading_texts.get(key, 0) + 1

        for key, count in heading_texts.items():
            if count > 1:
                tag, text = key.split(":", 1)
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=f"<{tag}>",
                    issue=f"Duplicate heading \"{text}\" appears {count} times at same level",
                    impact="Users navigating by headings may be confused by identical headings.",
                    recommendation="Differentiate headings to describe unique content sections.",
                    severity=Severity.LOW,
                ))

        conformance = self._determine_conformance(findings)
        confidence = 0.6  # Descriptiveness is partly subjective
        return conformance, confidence, findings


class Check_2_4_7(BaseCheck):
    """SC 2.4.7 Focus Visible (Level AA)."""

    criterion_id = "2.4.7"
    criterion_name = "Focus Visible"
    level = "AA"
    wcag_versions = ["2.0", "2.1", "2.2"]
    guideline = "2.4 Navigable"
    principle = "2. Operable"
    ict_baseline = "2"
    tt_tests = ["2.A"]
    # Focus-indicator contrast is measured per element by the focus probe.
    measurement_sources = {"contrast_ratio": ("focus_contrast", "contrast_ratio")}
    normative_text = (
        "Any keyboard operable user interface has a mode of operation "
        "where the keyboard focus indicator is visible."
    )

    off_scope_keywords = {
        "alt_text": ["alt text", "alternative text", "missing alt"],
        "contrast": ["contrast ratio", "4.5:1", "3:1", "color contrast"],
    }
    web_only = True

    def get_video_path(self, capture_data: CaptureData) -> str | None:
        """Send the keyboard walkthrough video — AI can watch focus
        indicators appearing (or not) as the user tabs through."""
        if capture_data.keyboard_walkthrough_video:
            return capture_data.keyboard_walkthrough_video
        return None

    def is_applicable(self, capture_data: CaptureData) -> bool:
        return bool(capture_data.tab_walk or capture_data.focus_indicators)

    def get_extra_images(self, capture_data: CaptureData) -> list[str]:
        """Send ALL focus indicator screenshot pairs (focused/unfocused)."""
        paths: list[str] = []
        for fi in capture_data.focus_indicators:
            unfocused = fi.get("unfocused_path", "")
            focused = fi.get("focused_path", "")
            if unfocused:
                paths.append(unfocused)
            if focused:
                paths.append(focused)
        return paths

    async def run_programmatic(
        self, capture_data: CaptureData
    ) -> tuple[ConformanceLevel, float, list[Finding]]:
        findings: list[Finding] = []

        # Build a lookup of which selectors have a *border-based* focus
        # indicator. The focus_indicators capture only inspects outline +
        # box-shadow, missing border changes; the nontext_contrast
        # capture (driven by SC 1.4.11) collects focus_border_color when
        # the element changes its border on :focus. Without this guard,
        # any element styled with `:focus { border-color: ... }` and no
        # outline gets flagged here as "no visible focus indicator" even
        # though the border IS the indicator (jQuery UI tabs are the
        # canonical case — see ui-id-1..ui-id-13 on a university site).
        border_focus_selectors: set[str] = set()
        for nctx in (getattr(capture_data, "nontext_contrast", None) or []):
            sel = nctx.get("selector") or ""
            border_color = nctx.get("focus_border_color") or ""
            if sel and border_color:
                border_focus_selectors.add(sel)

        # Check focus indicators from capture.
        # The focus_indicators list may come from two capture paths:
        #   1) Screenshot capture: fields include outline_style, box_shadow
        #   2) Tab-walk capture: fields include has_visible_indicator, indicator_type
        # We check both field naming conventions.
        _reported_selectors: set[str] = set()
        for fi in capture_data.focus_indicators:
            selector = fi.get("selector", "element")
            tag = fi.get("tag", "")
            text = fi.get("text") or ""

            # Determine visibility from whichever fields are present
            outline_style = fi.get("outline_style", fi.get("outline-style", ""))
            box_shadow = fi.get("box_shadow", fi.get("box-shadow", fi.get("boxShadow", "")))
            has_visible = fi.get("has_visible_indicator")  # from tab-walk
            visible = fi.get("visible")  # legacy field name

            # Compute effective visibility
            if has_visible is not None:
                is_visible = bool(has_visible)
            elif visible is not None:
                is_visible = bool(visible)
            else:
                # Infer from CSS properties (outline OR box-shadow OR
                # border change recorded by nontext_contrast)
                outline_ok = outline_style and outline_style != "none"
                shadow_ok = box_shadow and box_shadow != "none"
                border_ok = selector in border_focus_selectors
                is_visible = bool(outline_ok or shadow_ok or border_ok)

            if not is_visible:
                indicator_type = fi.get("indicator_type", "none")
                element_desc = selector
                if tag and text:
                    element_desc = f"{selector} (<{tag}> \"{text}\")"
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=element_desc,
                    issue=(
                        f"Element has no visible focus indicator"
                        f" (indicator_type: {indicator_type})"
                        if indicator_type and indicator_type != "none"
                        else "Element has no visible focus indicator"
                    ),
                    impact="Keyboard users cannot see which element has focus.",
                    recommendation=(
                        "Ensure a visible focus indicator (outline, border, "
                        "or box-shadow) is present on focus."
                    ),
                    severity=Severity.HIGH,
                ))
                _reported_selectors.add(selector)
            elif outline_style == "none" and not box_shadow:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=selector,
                    issue="Focus indicator suppressed with outline: none and no alternative",
                    impact="Keyboard users cannot see which element has focus.",
                    recommendation=(
                        "Replace outline: none with a visible alternative "
                        "using outline, border, or box-shadow on :focus."
                    ),
                    severity=Severity.HIGH,
                ))
                _reported_selectors.add(selector)

        # Cross-reference tab_walk data for elements with
        # has_visible_indicator=False that were NOT already reported from
        # the focus_indicators list above.
        for tw in capture_data.tab_walk:
            sel = tw.get("selector", "")
            if not sel or sel in _reported_selectors:
                continue
            if tw.get("has_visible_indicator") is False:
                tag = tw.get("tag", "")
                text = tw.get("text") or ""
                element_desc = sel
                if tag and text:
                    element_desc = f"{sel} (<{tag}> \"{text}\")"
                findings.append(Finding(
                    id=_make_finding_id(),
                    element=element_desc,
                    issue="Element has no visible focus indicator during tab walk",
                    impact="Keyboard users cannot see which element has focus.",
                    recommendation=(
                        "Add a visible focus style (outline, border, or "
                        "box-shadow) for keyboard focus on this element."
                    ),
                    severity=Severity.HIGH,
                ))
                _reported_selectors.add(sel)

        # Check CSS for any :focus rule that sets outline to none/0
        html = capture_data.html or ""
        if re.search(r":focus[^{]*\{[^}]*outline\s*:\s*(?:none|0)", html):
            # There is a :focus rule that suppresses outline -- check if an
            # alternative focus style is also defined (box-shadow / border)
            has_alt_focus_style = bool(
                re.search(r":focus[^{]*\{[^}]*(?:box-shadow|border)", html)
            )
            if not has_alt_focus_style:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="<style>",
                    issue="CSS :focus rule sets outline:none/0 with no alternative focus style",
                    impact="Focus indicators are removed, blocking keyboard navigation.",
                    recommendation=(
                        "Add an alternative visual indicator (box-shadow, border) "
                        "alongside outline:none in :focus rules."
                    ),
                    severity=Severity.HIGH,
                ))

        # Check for onfocus="blur()" which programmatically removes focus
        # This is a JavaScript-based focus killer that defeats keyboard
        # navigation — the element receives focus then immediately loses it
        if 'onfocus' in html.lower() and 'blur()' in html.lower():
            import re as _re
            blur_matches = _re.findall(
                r'onfocus\s*=\s*["\'][^"\']*blur\(\)[^"\']*["\']',
                html, _re.IGNORECASE,
            )
            if blur_matches:
                findings.append(Finding(
                    id=_make_finding_id(),
                    element="elements with onfocus=\"blur()\"",
                    issue=(
                        f"Found {len(blur_matches)} element(s) with "
                        f"onfocus=\"blur()\" which programmatically removes "
                        f"focus when the element receives it. This defeats "
                        f"keyboard navigation entirely — the user can never "
                        f"see where focus is."
                    ),
                    impact=(
                        "Keyboard users (JAWS, NVDA, VoiceOver) cannot see "
                        "or maintain focus on these elements. The focus "
                        "indicator is removed before it can be perceived, "
                        "making keyboard navigation impossible."
                    ),
                    recommendation=(
                        "Remove all onfocus=\"blur()\" handlers. Focus must "
                        "remain on elements when they receive it so keyboard "
                        "users can see where they are on the page."
                    ),
                    severity=Severity.HIGH,
                ))

        conformance = self._determine_conformance(findings)
        confidence = self._compute_confidence(
            findings, capture_data,
            total_elements=len(capture_data.tab_walk),
        )
        return conformance, confidence, findings


def get_checks() -> list[BaseCheck]:
    return [
        Check_2_4_1(),
        Check_2_4_2(),
        Check_2_4_3(),
        Check_2_4_4(),
        Check_2_4_5(),
        Check_2_4_6(),
        Check_2_4_7(),
    ]
