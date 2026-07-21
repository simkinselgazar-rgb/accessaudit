"""Keyboard roundtrip-probe finding extractor.

Extracted from `checks/base.py:BaseCheck` so the per-source extraction
logic is reusable and testable independently of the BaseCheck instance.
"""
from __future__ import annotations

from models import CaptureData, Finding, Severity
from functions.finding_utils import _make_finding_id


# SCs that consume the generic keyboard roundtrip probe.
_KEYBOARD_ROUNDTRIP_SCS = {"2.1.1", "2.1.2", "2.4.3", "1.4.13"}

# A keyboard walk that reached almost nothing on a page that clearly HAS
# focusable elements is a capture failure (focus never left <body>, the page
# was not ready, or a bot-challenge interstitial captured focus), NOT a real
# keyboard barrier. Below these thresholds the walk is unreliable and must
# not drive a "keyboard inaccessible / low coverage" finding.
_MIN_FOCUSABLE_FOR_JUDGEMENT = 5    # page must have at least this many focusables
_RELIABLE_COVERAGE_PCT = 15.0       # coverage at/above this is trusted
_RELIABLE_MIN_REACHED = 3           # or this many elements actually reached
_CHALLENGE_MARKERS = (
    "cf-", "cloudflare", "turnstile", "recaptcha", "g-recaptcha",
    "hcaptcha", "challenge", "__cf", "px-captcha",
)


def assess_tab_walk_reliability(capture_data: CaptureData) -> dict:
    """Judge whether the captured keyboard walk is trustworthy.

    Returns ``{"reliable": bool, "reason": str, "total_interactive": int,
    "reached": int, "coverage_percent": float, "challenge_dominated": bool}``.

    A walk is UNRELIABLE when the page clearly has focusable elements
    (``total_interactive >= 5``) but the walk reached almost none of them
    (coverage < 15% and fewer than 3 reached), or when the recorded stops are
    dominated by bot-challenge selectors. In that case the low coverage
    reflects the capture, not a keyboard barrier on the page, so keyboard SCs
    (2.1.1/2.1.2/2.1.3/2.4.3/2.4.7) must not infer a failure from it
    (verified on a university site, 2026-05-29).
    """
    tc = getattr(capture_data, "tab_coverage", None) or {}
    walk = getattr(capture_data, "tab_walk", None) or []
    non_body = [w for w in walk if isinstance(w, dict) and w.get("tag") != "body"]

    total_interactive = tc.get("total_interactive")
    if not isinstance(total_interactive, int):
        # Fall back to counting obvious focusables when coverage is absent.
        total_interactive = (
            len(getattr(capture_data, "links", None) or [])
            + len(getattr(capture_data, "form_fields", None) or [])
            + len(getattr(capture_data, "buttons", None) or [])
        )
    reached_cov = tc.get("reached_by_tab")
    reached = max(
        reached_cov if isinstance(reached_cov, int) else 0,
        len(non_body),
    )
    cov_pct = tc.get("coverage_percent")
    if not isinstance(cov_pct, (int, float)):
        cov_pct = (100.0 * reached / total_interactive) if total_interactive else 0.0

    challenge_hits = sum(
        1 for w in non_body
        if any(m in (w.get("selector", "") or "").lower() for m in _CHALLENGE_MARKERS)
    )
    challenge_dominated = bool(non_body) and challenge_hits >= max(1, len(non_body) // 2)

    reliable = True
    reason = "ok"
    if total_interactive >= _MIN_FOCUSABLE_FOR_JUDGEMENT and (
        cov_pct < _RELIABLE_COVERAGE_PCT and reached < _RELIABLE_MIN_REACHED
    ):
        reliable = False
        reason = (
            f"walk reached only {reached} of {total_interactive} focusable "
            f"elements ({cov_pct:.0f}%) -- capture likely failed (focus stuck "
            f"on <body>, page not ready, or bot-challenge), not a keyboard barrier"
        )
    elif challenge_dominated:
        reliable = False
        reason = (
            f"{challenge_hits} of {len(non_body)} recorded stops are "
            f"bot-challenge elements -- the walk was interrupted by an "
            f"interstitial, not a representative keyboard walk"
        )

    return {
        "reliable": reliable,
        "reason": reason,
        "total_interactive": total_interactive,
        "reached": reached,
        "coverage_percent": round(float(cov_pct), 1),
        "challenge_dominated": challenge_dominated,
    }


def extract_keyboard_roundtrip_findings(capture_data: CaptureData, criterion_id: str) -> list[Finding]:
    """Extract findings from the generic keyboard roundtrip probe.

    Routes to keyboard-behavior SCs:

    - SC 2.1.2 No Keyboard Trap (Level A):
      * Trigger opened content + Tab stays inside + Escape does
        NOT close → HIGH (keyboard trap with no documented escape).
    - SC 1.4.13 Content on Hover or Focus (Level AA):
      * Trigger opened content + Escape does NOT close → MEDIUM
        (dismissibility requirement not met). Skipped when this
        is also a 2.1.2 trap to avoid double-flagging.
    - SC 2.4.3 Focus Order (Level A):
      * Trigger opened content + closes on Escape but focus does
        NOT return to the trigger → MEDIUM (focus order broken
        after dismiss).
      * Shift+Tab from inside does not reach the trigger → INFO.
    - SC 2.1.1 Keyboard (Level A):
      * Trigger that has aria-haspopup / aria-controls / role
        implying it should activate, but neither Enter nor Space
        opens anything → MEDIUM (keyboard activation expected but
        not observed). Plain buttons with no popup affordance
        silently do nothing on Enter for many legitimate reasons
        (form submit blocked, etc.) so they're not flagged here.

    For other SCs returns ``[]``.
    """
    if criterion_id not in _KEYBOARD_ROUNDTRIP_SCS:
        return []

    results = getattr(capture_data, "keyboard_roundtrip_results", None) or []
    if not results:
        return []

    findings: list[Finding] = []
    sc = criterion_id

    for r in results:
        if not isinstance(r, dict):
            continue
        sel = r.get("selector", "?")
        tag = r.get("tag", "")
        role = r.get("role", "")
        text = r.get("text", "") or r.get("accessible_name", "") or ""
        text_preview = text
        opened = bool(r.get("opens_on_enter") or r.get("opens_on_space"))
        esc = r.get("escape_closes")
        ret = r.get("focus_returns_to_trigger")
        shift_ok = r.get("shift_tab_exits_cleanly")
        inside = r.get("tab_stays_inside")
        target = r.get("opened_target_selector", "")
        ah = r.get("aria_haspopup", "")
        ac = r.get("aria_controls", "")

        is_trap = opened and inside is True and esc is False
        is_undismissable = opened and esc is False
        is_focus_lost = opened and esc is True and ret is False
        is_promised_to_open = bool(opened is False and (ah or ac or role in ("menuitem", "tab", "combobox")))

        if sc == "2.1.2" and is_trap:
            findings.append(Finding(
                id=_make_finding_id(),
                element=sel, css_selector=sel,
                issue=(
                    f"Keyboard trap: <{tag}> "
                    f'"{text_preview}" opens content '
                    f"({target or 'unidentified container'}) "
                    f"that traps Tab inside and does NOT close on "
                    f"Escape. Users who tabbed in cannot get out "
                    f"with the keyboard alone."
                ),
                impact=(
                    "Keyboard-only and screen-reader users cannot "
                    "leave this widget without using the mouse. "
                    "Failing SC 2.1.2 is a Level A blocker."
                ),
                recommendation=(
                    "Implement Escape-to-close on the opened "
                    "container, OR provide a visible keyboard-"
                    "operable close affordance, OR remove the "
                    "Tab-trap so focus can leave naturally."
                ),
                severity=Severity.HIGH,
                source="programmatic",
                evidence=(
                    f"keyboard_roundtrip: opens={opened} "
                    f"tab_stays_inside={inside} escape_closes={esc} "
                    f"focus_returns={ret}."
                ),
            ))
            continue

        # SC 1.4.13 only applies when hover/focus actually REVEALS
        # additional content. The keyboard_roundtrip probe records
        # an "open_target" selector when something visibly appeared.
        # If open_target is empty (e.g. a toggle button that just
        # flipped its own text without revealing new UI — pauseHero
        # play/pause, header search expand-inline), 1.4.13 doesn't
        # apply and the missing-Escape behaviour is not a violation.
        if sc == "1.4.13" and is_undismissable and target:
            findings.append(Finding(
                id=_make_finding_id(),
                element=sel, css_selector=sel,
                issue=(
                    f"<{tag}> \"{text_preview}\" opens content "
                    f"({target}) that is NOT dismissible by "
                    f"pressing Escape. SC 1.4.13 requires "
                    f"hover/focus-revealed content to be "
                    f"dismissible without losing pointer focus."
                ),
                impact=(
                    "Users with low vision who magnify the screen, "
                    "or anyone who unintentionally triggered the "
                    "overlay, cannot remove it from the viewport "
                    "without moving the pointer or activating "
                    "another element."
                ),
                recommendation=(
                    "Add an Escape-key handler that dismisses the "
                    "opened content while leaving the trigger "
                    "focused."
                ),
                severity=Severity.MEDIUM,
                source="programmatic",
                evidence=(
                    f"keyboard_roundtrip: opened={opened}, "
                    f"escape_closes=False, "
                    f"target=\"{target}\"."
                ),
            ))

        if sc == "2.4.3" and is_focus_lost:
            findings.append(Finding(
                id=_make_finding_id(),
                element=sel, css_selector=sel,
                issue=(
                    f"<{tag}> \"{text_preview}\" opens and "
                    "closes correctly on Enter/Escape, but focus "
                    "does NOT return to the trigger after "
                    "dismiss. The user lands at an unexpected "
                    "spot in the tab order."
                ),
                impact=(
                    "Screen reader and keyboard users lose their "
                    "place after closing the overlay. They have "
                    "to re-discover where they were on the page, "
                    "often re-traversing the entire tab order."
                ),
                recommendation=(
                    "After Escape closes the content, set "
                    "document focus back to the trigger element."
                ),
                severity=Severity.MEDIUM,
                source="programmatic",
                evidence=(
                    "keyboard_roundtrip: escape_closes=True, "
                    "focus_returns_to_trigger=False."
                ),
            ))

        if sc == "2.4.3" and opened and shift_ok is False and esc is True:
            findings.append(Finding(
                id=_make_finding_id(),
                element=sel, css_selector=sel,
                issue=(
                    f"<{tag}> \"{text_preview}\" opens content "
                    "but Shift+Tab from inside the opened "
                    "content does not lead back to the trigger. "
                    "Reverse-Tab navigation is broken."
                ),
                impact=(
                    "Users who pressed Tab into the opened "
                    "content cannot reverse-Tab back to the "
                    "trigger, only forward through the rest of "
                    "the page."
                ),
                recommendation=(
                    "Ensure the opened content is in DOM order "
                    "after the trigger so Shift+Tab naturally "
                    "returns to it, or set tabindex/programmatic "
                    "focus management to mirror forward Tab."
                ),
                severity=Severity.INFO,
                source="programmatic",
                evidence=(
                    "keyboard_roundtrip: shift_tab_exits_cleanly=False."
                ),
            ))

        if sc == "2.1.1" and is_promised_to_open:
            findings.append(Finding(
                id=_make_finding_id(),
                element=sel, css_selector=sel,
                issue=(
                    f"<{tag}> \"{text_preview}\" advertises a "
                    f"popup or controlled region "
                    f"(aria-haspopup={ah!r}, aria-controls={ac!r}, "
                    f"role={role!r}) but neither Enter nor Space "
                    "opens anything. Either the keyboard handler "
                    "is missing or the ARIA attributes are stale."
                ),
                impact=(
                    "Screen reader users hear that this element "
                    "controls a popup, then activate it and "
                    "nothing happens. Mouse-only behaviour "
                    "indicates the click handler is wired but "
                    "the keyboard equivalent is not."
                ),
                recommendation=(
                    "Bind the same activation logic to keydown "
                    "Enter / Space, OR remove the misleading "
                    "aria-haspopup / aria-controls attributes."
                ),
                severity=Severity.MEDIUM,
                source="programmatic",
                evidence=(
                    f"keyboard_roundtrip: opens_on_enter=False, "
                    f"opens_on_space=False, aria_haspopup={ah!r}, "
                    f"aria_controls={ac!r}."
                ),
            ))

    return findings
