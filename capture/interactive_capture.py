"""Interactive capture tests for WCAG compliance.

Performs keyboard navigation, focus indicator analysis, hover content detection,
text spacing overflow, media playback checks, and more.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections import Counter
from typing import Any
from urllib.parse import urlparse


async def _safe_goto(page, url, timeout=60000):
    """Navigate to URL with fallback strategies for slow sites."""
    for strategy in ["networkidle", "domcontentloaded", "load"]:
        try:
            await page.goto(url, wait_until=strategy, timeout=timeout)
            return
        except Exception:
            if strategy == "load":
                raise
            continue

from playwright.async_api import Page

from functions.js_helpers import GET_SELECTOR_JS
from functions.selectors import selector_within
from models import CaptureData

logger = logging.getLogger(__name__)

# No hard cap — tab until we cycle back to body or hit a trap.
# Tab walks run until a natural termination condition fires:
# - 3 consecutive focus returns to <body>
# - TRAP_CONSECUTIVE_THRESHOLD same-selector focuses in a row (real trap)
# - A-B-A-B cycle detection (real trap)
# - Frequency-cycle detection (4+ repeats of same selector in last 8 steps)
# There is no count-based cap -- accuracy over artificial limits. A page
# with N distinct focusable elements terminates in O(N) Tab presses because
# every distinct focus is recorded and repeats trigger the trap detectors.
TRAP_CONSECUTIVE_THRESHOLD = 5

# Hard cap on how many Tab presses the forward/backward walks will
# attempt. A legitimate site has 50-200 tab stops; 500 is safely
# above that. Without this cap, pages that re-render focusable
# elements on every focus change (SPAs with live carousels, dynamic
# iframes) can keep producing *new* selectors every iteration, which
# evades the trap detectors and burns the full 1800s outer timeout.
# Observed on a university site 2026-04-23: tab_walk ran 30 min without tripping
# any existing trap detector. Iteration cap is the backstop.
MAX_TAB_ITERATIONS = 1000
TRAP_CYCLE_LENGTH = 2
TRAP_CYCLE_REPEATS = 5

# Native inputs whose internal segments/spinners each consume a Tab
# press while resolving to the SAME host selector, so they legitimately
# appear several times in a row in the tab walk. That is normal
# browser behavior (continued Tab DOES leave the field), NOT a keyboard
# trap, so they are exempt from the frequency-cycle trap detector.
# Verified: a native <input type="date"> was falsely reported as a
# 2.1.2 keyboard trap ("received focus 4 times") on the Trattoria
# fixture because month/day/year segments each register on #rdate.
_SEGMENTED_INPUT_TYPES = frozenset({
    "date", "time", "datetime-local", "week", "month", "number", "range",
})

# WCAG 1.4.12 text spacing CSS overrides
TEXT_SPACING_CSS = """
* {
    line-height: 1.5em !important;
    letter-spacing: 0.12em !important;
    word-spacing: 0.16em !important;
}
p {
    margin-bottom: 2em !important;
}
"""

# Overlay detection shared between pre/post activation snapshots, post-Escape
# closure check, and the Tab-out retry. A single selector list keeps the three
# checks consistent — a popup that we counted as "opened" is the same shape we
# look for when verifying closure and when asking whether focus escaped.
_OVERLAY_SELECTOR_JS = (
    "'[aria-expanded=\"true\"], '"
    " + '[role=\"menu\"]:not([hidden]), '"
    " + '[role=\"listbox\"]:not([hidden]), '"
    " + '[role=\"dialog\"]:not([hidden]), '"
    " + '[role=\"tooltip\"]:not([hidden]), '"
    " + '[role=\"menubar\"]:not([hidden]), '"
    " + '.dropdown.open, .dropdown.show, '"
    " + '.menu.open, .menu.show, '"
    " + '.is-open, .is-expanded, .is-active, '"
    " + 'details[open]'"
)

_OVERLAY_SNAPSHOT_JS = r"""
() => {
    const sel = """ + _OVERLAY_SELECTOR_JS + r""";
    const nodes = document.querySelectorAll(sel);
    const visible = Array.from(nodes).filter(el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0
            && s.visibility !== 'hidden' && s.display !== 'none';
    });
    // Classify the topmost overlay so the walker picks the right key
    // sequence: dialogs use Tab, dropdowns/menus/listboxes use arrow
    // keys. ``kind`` is either ``modal`` (dialog / alertdialog /
    // <dialog>) or ``dropdown`` (menu, menubar, listbox, tree,
    // combobox, tooltip, class-based open states).
    function classify(el) {
        if (!el) return '';
        const tag = (el.tagName || '').toLowerCase();
        const role = (el.getAttribute('role') || '').toLowerCase();
        if (tag === 'dialog') return 'modal';
        if (role === 'dialog' || role === 'alertdialog') return 'modal';
        if (role === 'menu' || role === 'menubar') return 'dropdown';
        if (role === 'listbox' || role === 'tree' || role === 'combobox') return 'dropdown';
        if (role === 'tooltip') return 'tooltip';
        return 'dropdown'; // class-based patterns default to dropdown
    }
    return {
        open_count: visible.length,
        open_ids: visible.map(el => el.id || el.tagName.toLowerCase()),
        kinds: visible.map(classify),
        // Top overlay for the walker to pick a strategy.
        top_kind: visible.length ? classify(visible[visible.length - 1]) : '',
    };
}
"""

_FOCUS_IN_OVERLAY_JS = r"""
() => {
    const el = document.activeElement;
    const sel = """ + _OVERLAY_SELECTOR_JS + r""";
    const overlays = Array.from(document.querySelectorAll(sel)).filter(o => {
        const r = o.getBoundingClientRect();
        const s = getComputedStyle(o);
        return r.width > 0 && r.height > 0
            && s.visibility !== 'hidden' && s.display !== 'none';
    });
    let focus_in_overlay = false;
    if (el) {
        focus_in_overlay = overlays.some(o => o.contains(el));
    }
    let focused_sel = 'body';
    if (el) {
        focused_sel = el.tagName.toLowerCase();
        if (el.id) focused_sel = '#' + el.id;
    }
    return {
        focus_in_overlay: focus_in_overlay,
        overlay_count: overlays.length,
        focused: focused_sel,
    };
}
"""

_CLOSE_CHECK_JS = r"""
(triggerSel) => {
    const el = document.activeElement;

    // Build a precise selector for the focused element so SC 2.4.3
    // can compare against the trigger by IDENTITY, not by tag name.
    // Previously this returned just "button" for everything, leading
    // to false positives whenever the trigger was also a <button>
    // and focus did stay on it. Now we return tag + classes (or #id
    // when available) so the comparison is meaningful.
    let focusedSel = null;
    let focusedIsTrigger = false;
    if (el && el !== document.body) {
        if (el.id) {
            focusedSel = '#' + el.id;
        } else {
            const tag = el.tagName.toLowerCase();
            const cls = (el.className && typeof el.className === 'string'
                ? el.className.trim().split(/\s+/).filter(Boolean).join('.')
                : '');
            focusedSel = cls ? tag + '.' + cls : tag;
        }
        // Identity check: is the focused element the same DOM node as
        // the trigger? This is the AUTHORITATIVE answer SC 2.4.3 needs.
        try {
            const trigEl = document.querySelector(triggerSel);
            focusedIsTrigger = !!(trigEl && trigEl === el);
        } catch (e) {
            focusedIsTrigger = false;
        }
    }

    const trigger = document.querySelector(triggerSel) || el;
    const expanded = trigger ? trigger.getAttribute('aria-expanded') : null;

    const openSel = """ + _OVERLAY_SELECTOR_JS + r""";
    const openMenus = Array.from(document.querySelectorAll(openSel)).filter(o => {
        const r = o.getBoundingClientRect();
        const s = getComputedStyle(o);
        return r.width > 0 && r.height > 0
            && s.visibility !== 'hidden' && s.display !== 'none';
    });

    return {
        focus_returned_to: focusedSel,
        focus_is_trigger: focusedIsTrigger,
        aria_expanded_after: expanded,
        still_open: openMenus.length > 0,
        open_selectors: openMenus.map(m => {
            if (m.id) return '#' + m.id;
            const cls = (m.className && typeof m.className === 'string')
                ? '.' + m.className.trim().split(/\s+/)[0] : '';
            return m.tagName.toLowerCase() + cls;
        }),
    };
}
"""

# Selector builder shared by the four focus-walk probes (_tab_walk,
# _backward_tab_walk, _tab_coverage_comparison, and the arrow-key
# probe's _active_selector). Deliberately NOT
# functions.js_helpers.GET_SELECTOR_JS: these walks must see focus
# inside shadow roots, so the path hops through ShadowRoot.host and
# marks crossings with ' >>> ' — a format the canonical getSelector
# doesn't produce. The four call sites diff each other's selectors,
# so they must share ONE implementation (a drifted copy in
# _tab_coverage_comparison used to emit ' > >>> > ' instead of
# ' >>> ' and broke the apples-to-apples diff against tab_walk).
_UNIQUE_SELECTOR_SHADOW_JS = r"""
function uniqueSelector(node) {
    if (!node || node === document.body) return 'body';
    if (node.id) return '#' + node.id;
    const parts = [];
    let cur = node;
    while (cur && cur !== document.body && cur !== document.documentElement) {
        if (cur.id) { parts.unshift('#' + cur.id); break; }
        const tag = cur.tagName ? cur.tagName.toLowerCase() : '';
        if (!tag) break;
        const parent = cur.parentNode;
        const sibs = parent && parent.children ? Array.from(parent.children) : [];
        const sameTag = sibs.filter(n => n.tagName === cur.tagName);
        const idx = sameTag.indexOf(cur) + 1;
        parts.unshift(sameTag.length > 1 ? tag + ':nth-of-type(' + idx + ')' : tag);
        if (parent instanceof ShadowRoot) {
            // Hop to the host element and prepend a shadow crossing
            // marker so the selector is readable.
            parts.unshift('>>>');
            cur = parent.host;
        } else {
            cur = parent;
        }
        if (!cur) break;
    }
    return parts.join(' > ').replace(/ > >>> > /g, ' >>> ');
}
"""


async def run_interactive_tests(
    page: Page,
    capture_data: CaptureData,
    review_dir: str,
) -> None:
    """Run all interactive tests on an already-loaded page.

    Modifies *capture_data* in place with results for:
    tab_walk, backward_tab_walk, tab_coverage, keyboard_traps,
    focus_indicators, hover_content, text_spacing_overflow,
    skip_link_results, form_errors, context_changes,
    focus_contrast, widget_keyboard, reduced_motion.

    Args:
        page: A Playwright page with the target URL already loaded.
        capture_data: The CaptureData object to populate.
        review_dir: Path to the review output directory.
    """
    captures_dir = os.path.join(review_dir, "captures")
    os.makedirs(captures_dir, exist_ok=True)

    # Track completion status for each test so downstream checks can
    # distinguish "no issues found" from "test didn't run".
    completions = getattr(capture_data, "capture_completions", {})

    # Incremental checkpoint path -- after each interactive test
    # completes (or fails), persist the full CaptureData to disk so a
    # subsequent restart's resume path reloads the already-done work
    # instead of repeating ~40 minutes of tab walks, hover detection,
    # focus indicators, etc. Tab walks alone cost 6+ minutes on an SPA
    # that hits the MAX_TAB_ITERATIONS cap; re-running the whole
    # interactive phase after a server restart wastes that time.
    checkpoint_path = os.path.join(captures_dir, "capture_data.json")

    def _save_checkpoint(after_name: str) -> None:
        try:
            capture_data.capture_completions = completions
            import json as _json
            payload = _json.dumps(
                capture_data.to_serializable_dict(), indent=2, default=str,
            )
            # Atomic-ish write: write to temp then rename, so a
            # server kill mid-write doesn't leave a half-flushed file
            # that the resume loader can't parse.
            tmp = checkpoint_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, checkpoint_path)
            logger.debug(
                "Interactive checkpoint saved after '%s' (%d completions recorded)",
                after_name, len(completions),
            )
        except Exception:
            # Checkpoint failures are non-fatal -- if disk is full or
            # serialization fails, we keep going. The end-of-capture
            # write in app.py is the authoritative save.
            logger.exception(
                "Checkpoint save failed after '%s' (non-fatal -- "
                "resume will fall back to re-capturing)", after_name,
            )

    def _norm_url(u: str) -> str:
        u = (u or "").split("#")[0].strip()
        return u[:-1] if u.endswith("/") else u

    _base_url = _norm_url(getattr(capture_data, "url", "") or "")

    async def _restore_page_if_drifted(after_name: str) -> None:
        """Re-navigate to the page under review if a step drifted away.

        An interactive step can complete "ok" yet still leave the page on
        a different URL -- activating a skip link, submitting a form, or
        following a link as part of its probe. Every later capture
        (focus_contrast, widget_keyboard, focus_content, ...) would then
        read the WRONG page. Verified bug (a university-site run 20260519):
        focus_contrast captured a different site's DOM after a
        mid-sequence navigation. _verify_or_recover_page only re-navigates
        on a dead bridge, not on URL drift -- this closes that gap.
        """
        if not _base_url:
            return
        try:
            current = _norm_url(page.url)
        except Exception:
            return
        if current == _base_url:
            return
        logger.warning(
            "Page drifted from %s to %s after '%s' -- re-navigating so "
            "later interactive captures read the page under review.",
            _base_url, current, after_name,
        )
        try:
            await asyncio.wait_for(
                page.goto(_base_url, wait_until="domcontentloaded",
                          timeout=15000),
                timeout=20,
            )
        except (asyncio.TimeoutError, Exception) as exc:
            logger.error(
                "Re-navigation to %s after '%s' failed (%s: %s); "
                "subsequent interactive tests skipped to avoid "
                "wrong-page capture.",
                _base_url, after_name, type(exc).__name__, exc,
            )
            completions["__browser_unrecoverable"] = "yes"

    async def _verify_or_recover_page(after_name: str) -> None:
        """Health-check the browser bridge after a failed/timed-out test.

        A hung Playwright call inside one test (e.g. a click that
        triggers an infinite-loop JS handler) can leave the browser's
        IPC wedged so every SUBSEQUENT call hangs too. Once asyncio
        cancels the coroutine, the Python side returns but the C-level
        I/O may stay blocked. Without recovery, the next test inherits
        the wedged bridge and either hangs again or appears to "run"
        but produces empty results (because page.evaluate returns
        immediately with no payload before the bridge gives up).

        Strategy:
          1. Probe the bridge with `page.evaluate("() => 1")` under a
             5-second budget.
          2. If it responds, return -- bridge is healthy.
          3. If it doesn't, re-navigate the page to its source URL
             (15s budget). This forces Playwright to open a fresh
             IPC connection.
          4. After navigation, probe again.
          5. If recovery fails too, set a sentinel completion so all
             remaining interactive tests skip cleanly instead of each
             timing out for its full budget.
        """
        try:
            await asyncio.wait_for(page.evaluate("() => 1"), timeout=5)
            return  # bridge is alive
        except (asyncio.TimeoutError, Exception):
            pass

        logger.warning(
            "Browser bridge unresponsive after '%s' (page.evaluate "
            "didn't return in 5s). Attempting page recovery via "
            "re-navigation.", after_name,
        )

        url = getattr(capture_data, "url", "") or ""
        if not url:
            try:
                url = page.url or ""
            except Exception:
                url = ""

        if not url:
            logger.error(
                "Cannot recover page after '%s': no URL recorded on "
                "capture_data and page.url is unreadable. Subsequent "
                "interactive tests will be SKIPPED to prevent cascading "
                "timeouts.", after_name,
            )
            completions["__browser_unrecoverable"] = "yes"
            return

        try:
            await asyncio.wait_for(
                page.goto(url, wait_until="domcontentloaded", timeout=15000),
                timeout=20,
            )
            await asyncio.wait_for(page.evaluate("() => 1"), timeout=5)
            logger.info(
                "Page recovered after '%s' via re-navigation to %s",
                after_name, url,
            )
        except (asyncio.TimeoutError, Exception) as exc:
            logger.error(
                "Page recovery failed after '%s' (%s: %s). Subsequent "
                "interactive tests will be SKIPPED to prevent cascading "
                "timeouts.",
                after_name, type(exc).__name__, exc,
            )
            completions["__browser_unrecoverable"] = "yes"

    async def _run_with_timeout(coro, name: str, timeout: float = 60.0):
        # Skip tests whose completion status is already recorded from
        # a previous run (resume path). A checkpoint from an earlier
        # process wrote the step's data; don't overwrite it.
        if completions.get(name) == "ok":
            logger.info(
                "Interactive test '%s': skipping -- already completed "
                "in an earlier run (resume)", name,
            )
            # Close the unused coroutine so asyncio doesn't warn.
            coro.close()
            return
        # Skip if a prior test wedged the browser bridge unrecoverably.
        # Each subsequent test would either time out for its full
        # budget or run against a dead page producing empty results,
        # so bail fast.
        if completions.get("__browser_unrecoverable") == "yes":
            logger.warning(
                "Interactive test '%s': SKIPPED -- browser bridge "
                "unrecoverable from an earlier failure. Remaining "
                "interactive tests skipped to prevent cascading "
                "timeouts.", name,
            )
            completions[name] = "skipped_browser_dead"
            coro.close()
            _save_checkpoint(name)
            return
        try:
            await asyncio.wait_for(coro, timeout=timeout)
            completions[name] = "ok"
        except asyncio.TimeoutError:
            logger.warning("Interactive test '%s' timed out after %.0fs", name, timeout)
            completions[name] = "timeout"
        except Exception:
            logger.exception("Interactive test '%s' failed", name)
            completions[name] = "error"
        finally:
            _save_checkpoint(name)

        # Verify Playwright is still responsive. If not, try to recover
        # before the next test starts.
        if completions[name] != "ok":
            await _verify_or_recover_page(name)

        # A step can complete "ok" yet leave the page navigated away;
        # restore the page under review so later captures do not read
        # the wrong page.
        await _restore_page_if_drifted(name)

    # These timeouts are deliberately generous — per-page capture should
    # never lose evidence to a time cap on a large real-world site. The
    # cap is a safety net against a truly hung Playwright call, not a
    # budget. A university mega-menu with 500 tab stops, a hero carousel
    # with 20 interactive slides, or a YouTube embed with 5-minute
    # captions all need to run to completion; truncation produces false
    # 2.1.1 / 2.4.3 / 1.2.2 findings that the judge then ships into
    # the ACR.
    # Tab walks: 5-minute cap. Both walks also carry an internal
    # MAX_TAB_ITERATIONS cap (500 tabs) -- whichever fires first.
    # A real site completes in ~2-3 min and < 200 iterations; the cap
    # exists to bound misbehaving SPAs that dodge trap detection.
    await _run_with_timeout(_tab_walk(page, capture_data, captures_dir), "tab_walk", 300)
    # Retry once if the walk reached nothing (focus never left <body>) while
    # the DOM clearly has focusable elements -- the walk likely ran before the
    # page settled or while a focus-capturing interstitial (e.g. a bot
    # challenge) was up. A second pass after a short settle usually succeeds
    # and prevents keyboard SCs from resting on a 0%-coverage walk (verified on
    # a university site 2026-05-29: a Cloudflare interstitial yielded a 0-real-stop walk).
    _non_body = [t for t in (capture_data.tab_walk or []) if t.get("tag") != "body"]
    if not _non_body:
        try:
            _focusable = await page.evaluate(
                "() => document.querySelectorAll("
                "'a[href],button,input,select,textarea,[tabindex],[contenteditable=\"true\"]'"
                ").length"
            )
        except Exception:
            logger.warning("Tab-walk retry: focusable-count probe failed", exc_info=True)
            _focusable = 0
        if _focusable and _focusable > 0:
            logger.warning(
                "Tab walk reached 0 focusable elements but the DOM has %d -- "
                "page likely not ready / interstitial up; re-running tab walk once",
                _focusable,
            )
            await page.wait_for_timeout(1500)
            await _run_with_timeout(
                _tab_walk(page, capture_data, captures_dir), "tab_walk_retry", 300
            )
    await _run_with_timeout(_backward_tab_walk(page, capture_data), "backward_tab", 300)
    await _run_with_timeout(_tab_coverage_comparison(page, capture_data), "tab_coverage", 600)
    await _run_with_timeout(_recorded_keyboard_walkthrough(page, capture_data, captures_dir), "keyboard_walkthrough", 1800)
    await _run_with_timeout(_focus_indicator_screenshots(page, capture_data, captures_dir), "focus_indicators", 1800)
    await _run_with_timeout(_hover_content_detection(page, capture_data, captures_dir), "hover_detection", 1800)
    await _run_with_timeout(_text_spacing_overflow(page, capture_data, captures_dir), "text_spacing", 600)
    await _run_with_timeout(_media_playback(page, capture_data), "media_playback", 900)
    await _run_with_timeout(_record_media_with_captions(page, capture_data, captures_dir), "media_recording", 1800)
    await _run_with_timeout(_record_caption_toggle(page, capture_data, captures_dir), "caption_toggle_recording", 1200)
    await _run_with_timeout(_transcript_verification(page, capture_data, captures_dir), "transcript_verification", 900)
    await _run_with_timeout(_skip_link_verification(page, capture_data), "skip_links", 600)
    await _run_with_timeout(_form_submission_test(page, capture_data, captures_dir), "form_submission", 1200)
    await _run_with_timeout(_context_change_detection(page, capture_data), "context_changes", 600)
    await _run_with_timeout(_probe_autoplay_media(page, capture_data), "audio_detection", 300)
    await _run_with_timeout(_capture_focus_contrast(page, capture_data, captures_dir), "focus_contrast", 1200)
    await _run_with_timeout(_capture_form_errors(page, capture_data, captures_dir), "form_error_capture", 1200)
    await _run_with_timeout(_capture_focus_content(page, capture_data, captures_dir), "focus_content", 1200)
    await _run_with_timeout(_capture_widget_keyboard(page, capture_data, captures_dir), "widget_keyboard", 1200)
    await _run_with_timeout(_capture_modal_interactions(page, capture_data), "modal_interactions", 1200)
    # Generic per-trigger Enter/Escape/Tab/Shift+Tab roundtrip — broader
    # trigger inventory than the modal probe, catches custom dropdowns,
    # drawers, search overlays without aria-controls/aria-haspopup.
    # Outer cap is generous: complex pages can have 50+ candidate
    # triggers, each with 90s per-probe budget plus reset overhead.
    await _run_with_timeout(_capture_keyboard_roundtrip(page, capture_data), "keyboard_roundtrip", 3600)
    await _run_with_timeout(_capture_reduced_motion(page, capture_data, captures_dir), "reduced_motion", 60)

    # Save completion status
    capture_data.capture_completions = completions
    ok = sum(1 for v in completions.values() if v == "ok")
    failed = sum(1 for v in completions.values() if v != "ok")
    logger.info(
        "Interactive tests: %d/%d ok, %d failed%s",
        ok, len(completions), failed,
        " — " + ", ".join(f"{k}={v}" for k, v in completions.items() if v != "ok") if failed else "",
    )


# ─── Autoplay media detection via deterministic Playwright probe ────────────

_AUTOPLAY_PROBE_JS = r"""
() => {
    const out = {media: [], iframes: [], audio_context_running: false};

    const collect = (el, kind) => {
        const rect = el.getBoundingClientRect();
        return {
            kind,
            tag: el.tagName.toLowerCase(),
            src: el.currentSrc || el.src || '',
            autoplay_attr: el.hasAttribute('autoplay'),
            muted: !!el.muted,
            paused: el.paused !== false ? el.paused : true,
            ended: !!el.ended,
            current_time: el.currentTime || 0,
            duration: isFinite(el.duration) ? el.duration : null,
            loop: !!el.loop,
            controls: !!el.controls,
            volume: el.volume,
            visible: rect.width > 0 && rect.height > 0,
            x: rect.x, y: rect.y, width: rect.width, height: rect.height,
        };
    };

    document.querySelectorAll('audio').forEach(el => out.media.push(collect(el, 'audio')));
    document.querySelectorAll('video').forEach(el => out.media.push(collect(el, 'video')));

    document.querySelectorAll('iframe').forEach(el => {
        const src = el.src || '';
        const isMedia = /youtube\.com|youtu\.be|vimeo\.com|soundcloud\.com|spotify\.com|wistia/i.test(src);
        if (isMedia) {
            const rect = el.getBoundingClientRect();
            out.iframes.push({
                src,
                autoplay_param: /autoplay=1|autoplay=true/i.test(src),
                muted_param: /mute=1|muted=true/i.test(src),
                visible: rect.width > 0 && rect.height > 0,
            });
        }
    });

    if (window.AudioContext || window.webkitAudioContext) {
        try {
            const ctxClass = window.AudioContext || window.webkitAudioContext;
            // Don't construct a new one (costs perf and may auto-suspend);
            // probe whether any existing context is running by checking the
            // window for known audio-using libraries' singletons.
            const candidates = [window.audioContext, window.AudioContext_singleton];
            for (const c of candidates) {
                if (c && c.state === 'running') { out.audio_context_running = true; break; }
            }
        } catch (e) {}
    }

    return out;
}
"""


async def _probe_autoplay_media(page: Page, capture_data) -> None:
    """Deterministic DOM probe for autoplay audio/video.

    Replaces the prior Gemma E4B audio-LLM call (which never actually
    received audio -- some local multimodal servers strip the audio track from
    video uploads, so the model only saw frames; combined with Playwright's
    video recording producing webm with no audio stream at all, the LLM
    call was an architectural no-op and frequently got cancelled).

    This probe queries every <audio> and <video> element on the page plus
    common embedded-player <iframe>s (YouTube, Vimeo, SoundCloud, Spotify,
    Wistia), records each element's runtime state (autoplay attr, muted,
    paused, currentTime, duration, controls, visibility), and writes a
    summary onto capture_data.audio_detection in the same shape the SC
    1.4.2 / SC 2.2.2 checks already consume -- so downstream checks need
    no changes for the deterministic signal.

    The per-SC check pipelines layer an AI corroboration call on top of
    this when AI_VIDEO_API_URL points to a model that can actually hear
    audio (e.g. Gemini Flash), but only for those two SCs.
    """
    try:
        probe = await page.evaluate(_AUTOPLAY_PROBE_JS)
    except asyncio.CancelledError:
        logger.warning("Autoplay probe CANCELLED before completion")
        raise
    except Exception as exc:
        logger.warning("Autoplay probe failed: %s", exc)
        return

    media = probe.get("media", [])
    iframes = probe.get("iframes", [])

    # An element is "auto-playing audibly" when it autoplays without being
    # muted AND is either currently playing or has the autoplay attr set.
    autoplaying_audible = [
        m for m in media
        if m.get("autoplay_attr")
        and not m.get("muted")
        and (not m.get("paused") or m.get("autoplay_attr"))
    ]

    audible_iframes = [
        f for f in iframes
        if f.get("autoplay_param") and not f.get("muted_param")
    ]

    has_autoplay = bool(autoplaying_audible) or bool(audible_iframes)

    # Duration > 3s when any detected element reports duration > 3s, OR
    # when an embedded player is involved (YouTube/Vimeo are virtually
    # always > 3s and we can't probe their inner duration cross-origin).
    duration_over_3s = (
        any((m.get("duration") or 0) > 3 for m in autoplaying_audible)
        or bool(audible_iframes)
    )

    # has_pause_button: any audible element has controls=true OR there's
    # any visible button/control near it. The SC check itself does the
    # finer verification; this is just the fast signal.
    has_pause = any(m.get("controls") for m in autoplaying_audible)

    # Determine type for downstream messaging
    if any(m["kind"] == "video" for m in autoplaying_audible):
        audio_type = "video soundtrack"
    elif any(m["kind"] == "audio" for m in autoplaying_audible):
        audio_type = "audio"
    elif audible_iframes:
        audio_type = "embedded media"
    else:
        audio_type = "silence"

    description_parts = []
    for m in autoplaying_audible:
        d = m.get("duration")
        d_str = f"{d:.1f}s" if d else "unknown duration"
        description_parts.append(
            f"{m['kind']} src={m.get('src','')} "
            f"autoplay muted={m.get('muted')} duration={d_str}"
        )
    for f in audible_iframes:
        description_parts.append(f"embedded player src={f.get('src','')} autoplay")

    capture_data.audio_detection = {
        "has_autoplay_audio": has_autoplay,
        "duration_over_3s": duration_over_3s,
        "audio_type": audio_type,
        "has_pause_button": has_pause,
        "description": "; ".join(description_parts) if description_parts else "no autoplay media detected",
        "_source": "deterministic_dom_probe",
        "_raw_media": media,
        "_raw_iframes": iframes,
    }

    if not capture_data.dynamic_content:
        capture_data.dynamic_content = {}
    if has_autoplay:
        capture_data.dynamic_content["hasAutoplayAudio_detected"] = True
        capture_data.dynamic_content["autoplay_audio_type"] = audio_type
        capture_data.dynamic_content["autoplay_audio_over_3s"] = duration_over_3s

    logger.info(
        "Autoplay probe: media=%d iframes=%d audible=%d -> has_autoplay=%s, over_3s=%s",
        len(media), len(iframes), len(autoplaying_audible) + len(audible_iframes),
        has_autoplay, duration_over_3s,
    )


# ─── Tab walk ────────────────────────────────────────────────────────────────

async def _tab_walk(
    page: Page,
    capture_data: CaptureData,
    captures_dir: str,
) -> None:
    """Press Tab repeatedly and record the focus order.

    Trap detection:
      - Same element focused TRAP_CONSECUTIVE_THRESHOLD consecutive times.
      - Cycling pattern A-B-A-B-A detected.
    Stops when focus returns to <body>.
    """
    try:
        # Reset page state before measuring: a prior phase (Phase 2
        # visual explorer, hover detection) may have left a modal
        # open, a dropdown expanded, or a focus trap active. Starting
        # the Tab walk inside a trapped state produces garbage data --
        # focus cycles through the trap's handful of elements forever
        # and hits MAX_TAB_ITERATIONS. Three Escape presses close any
        # lingering overlays that respond to Escape (modal, dialog,
        # listbox popup, combobox popup, tooltip). Idempotent on a
        # clean page (Escape at <body> does nothing). This is the
        # Phase-2-to-interactive-capture equivalent of "fresh sample"
        # on the LLM side.
        for _ in range(3):
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(100)
        # Start from the top of the page by focusing body
        await page.evaluate("document.body.focus()")
        await page.wait_for_timeout(100)

        tab_order: list[dict] = []
        traps: list[dict] = []
        recent_selectors: list[str] = []
        # Selectors of native segmented/spinner inputs seen in the walk;
        # the frequency-cycle detector exempts these (their internal
        # segments repeat the host selector but aren't a trap).
        segmented_input_selectors: set[str] = set()
        # Separate window that DOES include body hits — used only by the
        # density-based chrome-cycle stop below. Keeping body out of
        # recent_selectors means the trap detectors can never emit a
        # spurious trap naming <body> (matches the backward walk).
        recent_tab_outcomes: list[str] = []
        consecutive_body = 0

        i = 0
        truncated = False
        halted_by_trap = False
        while True:
            i += 1
            if i > MAX_TAB_ITERATIONS:
                truncated = True
                # Diagnostic dump: what was the Tab walk actually seeing?
                # Without this the cap message is just a guess. The
                # dump answers:
                #   - Did selectors repeat (trap/cycle) or stay unique
                #     (SPA re-render producing fresh nth-of-type paths)?
                #   - Which element was most frequently focused -- that
                #     is usually the root of the trap.
                #   - Did we ever see <body> at all?
                from collections import Counter as _Cnt
                all_sels = [(t.get("selector") or "?") for t in tab_order]
                unique_sels = set(all_sels)
                body_hits = sum(1 for s in all_sels if s == "body")
                top = _Cnt(all_sels).most_common(5)
                top_str = ", ".join(
                    f'"{s}"x{c}' for s, c in top
                )
                last10 = list(all_sels[-10:])
                logger.warning(
                    "_tab_walk: reached MAX_TAB_ITERATIONS=%d without "
                    "returning to <body>. Diagnostic: %d tab stops "
                    "recorded, %d unique selectors, body observed %d "
                    "times. Top frequencies: [%s]. Last 10 selectors: "
                    "%s. If unique_count << recorded, focus is cycling "
                    "through a small set (likely a modal trap or open "
                    "dropdown from a prior phase). If unique_count is "
                    "~recorded, the page re-renders focusable elements "
                    "on every Tab (SPA pattern).",
                    MAX_TAB_ITERATIONS,
                    len(all_sels), len(unique_sels), body_hits,
                    top_str, last10,
                )
                break
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(100)

            info = await page.evaluate("() => {" + _UNIQUE_SELECTOR_SHADOW_JS + """
                // Descend through every shadow root so focus inside a
                // web component (Canvas/Instructure-UI, SIDEARM, Lit)
                // is seen, not reported as <body>.
                function deepActiveElement() {
                    let el = document.activeElement;
                    while (el && el.shadowRoot && el.shadowRoot.activeElement) {
                        el = el.shadowRoot.activeElement;
                    }
                    return el;
                }
                const el = deepActiveElement();
                if (!el) return null;
                const rect = el.getBoundingClientRect();
                const cs = window.getComputedStyle(el);
                const outline = cs.outline || '';
                const outlineWidth = cs.outlineWidth || '0px';
                const outlineStyle = cs.outlineStyle || 'none';
                const boxShadow = cs.boxShadow || '';
                const borderColor = cs.borderColor || '';
                const borderWidth = cs.borderWidth || '';
                const backgroundColor = cs.backgroundColor || '';
                // Check multiple focus indicator CSS properties
                const hasOutline = outlineStyle !== 'none' && outlineWidth !== '0px';
                const hasShadow = boxShadow !== 'none' && boxShadow !== '';
                const hasBorder = borderWidth !== '0px' && borderColor !== '';
                const isVisible = hasOutline || hasShadow || hasBorder;
                const selector = uniqueSelector(el);
                // Detect whether focus is currently inside a shadow root
                // (caller uses this to flag SCs that evaluate shadow-DOM
                // components).
                let rootNode = el.getRootNode ? el.getRootNode() : document;
                const inShadow = rootNode && rootNode.host !== undefined;
                return {
                    tag: el.tagName.toLowerCase(),
                    type: (el.getAttribute('type') || '').toLowerCase(),
                    role: el.getAttribute('role') || '',
                    text: (el.textContent || '').trim(),
                    selector: selector,
                    has_visible_indicator: isVisible,
                    indicator_type: hasOutline ? 'outline' : hasShadow ? 'box-shadow' : hasBorder ? 'border' : 'none',
                    rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                    in_shadow_dom: !!inShadow,
                };
            }""")
            if not info:
                continue

            # Stop if focus returned to body — require 3 consecutive body
            # hits to avoid false stops. SPAs, cookie dialogs, and iframe
            # boundaries often pass through body momentarily.
            #
            # ALSO stop when body shows up too often in the recent window.
            # A page that cycles focus through browser chrome will produce
            # an alternating pattern ([elem, body, elem, body, …]) in which
            # consecutive_body never reaches 3 yet the walk clearly has
            # finished enumerating focusable elements and is now looping.
            # A density-based stop (body >= 30% of last 20 tabs) catches
            # this pattern immediately. Observed on a university site 2026-04-23:
            # tab_walk ran 7000+ iterations in 30 min without tripping any
            # existing detector.
            if info["tag"] == "body":
                consecutive_body += 1
                recent_tab_outcomes.append("body")
                if consecutive_body >= 3:
                    break
                # Density stop: body dominates the recent window.
                if len(recent_tab_outcomes) >= 20:
                    window = recent_tab_outcomes[-20:]
                    body_hits = sum(1 for s in window if s == "body")
                    if body_hits >= 6:  # 30% of last 20 tabs
                        logger.info(
                            "_tab_walk: stop after %d iterations -- body "
                            "hit %d times in last 20 tabs (chrome cycle)",
                            i, body_hits,
                        )
                        break
                # Reset the trap-detection window — passing through body
                # is normal browser behavior, not a trap. Same rule as
                # the backward walk.
                recent_selectors = []
                continue
            consecutive_body = 0

            tab_order.append(info)
            recent_selectors.append(info["selector"])
            recent_tab_outcomes.append(info["selector"])
            if info.get("tag") == "input" and info.get("type") in _SEGMENTED_INPUT_TYPES:
                segmented_input_selectors.add(info["selector"])

            # --- Full-wrap detection ---
            # When focus wraps from the last focusable element back to
            # the first WITHOUT passing through <body> (some sites
            # install JS that keeps focus inside the page chrome), the
            # existing body-return check never fires and the walk spins
            # until MAX_TAB_ITERATIONS. The data is already collected
            # after one full pass -- each additional pass is 100% noise.
            #
            # Detection: track how many consecutive Tabs produce a
            # selector we've seen before (no new additions to the
            # unique set). After 30 consecutive tabs with zero new
            # selectors AND at least 20 unique selectors observed, we
            # have clearly enumerated every focusable element and the
            # current iteration is pure repetition.
            #
            # 30-tab tolerance window: some pages open a dropdown that
            # reveals 5-10 new focusable elements mid-walk, which
            # temporarily resumes growth of the unique set; the
            # tolerance keeps us from aborting during those legitimate
            # mid-walk discoveries.
            if not hasattr(capture_data, "_tw_unique"):
                # Local cache on the CaptureData instance; per-walk
                # state that doesn't need to persist on disk.
                capture_data._tw_unique = set()
                capture_data._tw_repeats = 0
            sel = info["selector"]
            if sel in capture_data._tw_unique:
                capture_data._tw_repeats += 1
            else:
                capture_data._tw_unique.add(sel)
                capture_data._tw_repeats = 0
            if (
                capture_data._tw_repeats >= 30
                and len(capture_data._tw_unique) >= 20
            ):
                logger.info(
                    "_tab_walk: stop at iter %d -- full cycle detected. "
                    "%d unique selectors seen, last 30 tabs added no new "
                    "elements (page wraps focus without passing through "
                    "<body>). Additional iterations would be pure "
                    "repetition.",
                    i, len(capture_data._tw_unique),
                )
                # Clean up the transient cache so subsequent walks
                # start fresh.
                try:
                    del capture_data._tw_unique
                    del capture_data._tw_repeats
                except AttributeError:
                    pass
                break

            # Native segmented/spinner inputs (date/time/number/…) focus
            # their internal segments on successive Tabs, all resolving to
            # the host selector — so they repeat both consecutively AND by
            # frequency. Continued Tab DOES leave the field, so this is not
            # a keyboard trap. Skip ALL trap detectors for this iteration
            # (covers the consecutive, cycle, and frequency detectors below
            # in one place). i is incremented at the top of the loop, so a
            # bare continue is correct.
            if info.get("tag") == "input" and info.get("type") in _SEGMENTED_INPUT_TYPES:
                continue

            # --- Trap detection ---
            # Consecutive same element (only across a body-free run).
            if len(recent_selectors) >= TRAP_CONSECUTIVE_THRESHOLD:
                last_n = recent_selectors[-TRAP_CONSECUTIVE_THRESHOLD:]
                if len(set(last_n)) == 1:
                    traps.append({
                        "type": "consecutive",
                        "selector": info["selector"],
                        "tab_index": i,
                        "description": f"Element {info['selector']} received focus "
                                        f"{TRAP_CONSECUTIVE_THRESHOLD} consecutive times",
                    })
                    break

            # Cycling pattern A-B-A-B-A (only across a body-free run).
            if len(recent_selectors) >= TRAP_CYCLE_REPEATS:
                last_cycle = recent_selectors[-TRAP_CYCLE_REPEATS:]
                a, b = last_cycle[0], last_cycle[1]
                if a != b:
                    expected = [a, b] * (TRAP_CYCLE_REPEATS // 2)
                    if TRAP_CYCLE_REPEATS % 2:
                        expected.append(a)
                    if last_cycle == expected:
                        traps.append({
                            "type": "cycle",
                            "selectors": [a, b],
                            "tab_index": i,
                            "description": f"Focus cycling between {a} and {b}",
                        })
                        break

            # Frequency-based cycle detection — if the same element
            # appears 4+ times in the last 8 steps, it's a trap.
            # Using 4/8 instead of 3/5 to avoid false positives on
            # pages with repeated navigation controls (e.g., frame wrappers).
            if len(recent_selectors) >= 8:
                last_n = recent_selectors[-8:]
                counts = Counter(last_n)
                most_common_sel, most_common_count = counts.most_common(1)[0]
                if most_common_count >= 4 and most_common_sel in segmented_input_selectors:
                    # Native segmented input (date/time/number/…): the
                    # internal segments repeat the host selector but
                    # continued Tab leaves the field — not a trap.
                    logger.debug(
                        "Tab walk: %s repeated %d× but is a native segmented "
                        "input — not a keyboard trap, skipping",
                        most_common_sel, most_common_count,
                    )
                elif most_common_count >= 4:
                    trap_record = {
                        "type": "frequency_cycle",
                        "selector": most_common_sel,
                        "tab_index": i,
                        "description": (
                            f"Element {most_common_sel} received focus "
                            f"{most_common_count} times in the last 8 steps"
                        ),
                    }
                    traps.append(trap_record)
                    # ATTEMPT RECOVERY: a frequency cycle is the test
                    # spinning on a small set of elements; it does NOT
                    # always mean the cycle is non-escapable. Try the
                    # standard recovery sequence: Escape (collapse any
                    # open dropdown), click <body> (force-reset focus
                    # to the page root), then resume forward Tab from
                    # there. If recovery succeeds (focus moves to a
                    # selector we have not seen recently), continue the
                    # walk. If it fails after one attempt, only THEN
                    # mark the walk halted-by-trap. This produces real
                    # tab-coverage data on pages that have a single
                    # cycle followed by reachable content (a university site's
                    # Lorem-Ipsum-ID cycle is the canonical case).
                    pre_recovery_count = len(tab_order)
                    recovery_succeeded = False
                    try:
                        for _ in range(3):
                            await page.keyboard.press("Escape")
                            await page.wait_for_timeout(80)
                        await page.evaluate("() => document.body.focus()")
                        await page.wait_for_timeout(80)
                        # One forward Tab from body should land on a
                        # NEW element if recovery worked
                        await page.keyboard.press("Tab")
                        await page.wait_for_timeout(120)
                        post_recovery_sel = await page.evaluate("""
                            () => {
                                const a = document.activeElement;
                                if (!a || a === document.body) return '';
                                return a.id ? '#' + a.id
                                    : a.tagName.toLowerCase()
                                      + (a.className && typeof a.className === 'string'
                                         ? '.' + a.className.split(' ').filter(Boolean).join('.')
                                         : '');
                            }
                        """)
                        if (post_recovery_sel
                                and post_recovery_sel != most_common_sel
                                and post_recovery_sel not in last_n):
                            recovery_succeeded = True
                            trap_record["recovery_succeeded"] = True
                            recent_selectors = []  # reset cycle history
                    except Exception as exc:
                        logger.debug("Trap recovery failed: %s", exc)

                    if recovery_succeeded:
                        logger.info(
                            "Tab walk: recovered from frequency cycle on "
                            "%s; resuming walk from %s",
                            most_common_sel, post_recovery_sel,
                        )
                        continue  # resume the while-loop
                    # Recovery failed -- mark the walk truncated.
                    truncated = True
                    halted_by_trap = True
                    trap_record["recovery_succeeded"] = False
                    break

        capture_data.tab_walk = tab_order
        capture_data.keyboard_traps = traps
        capture_data.tab_walk_truncated = {
            **(capture_data.tab_walk_truncated or {}),
            "forward": truncated,
            "forward_halted_by_trap": halted_by_trap,
        }
        # Clean up transient full-wrap detection state so the backward
        # walk (and any future probe) starts fresh.
        for attr in ("_tw_unique", "_tw_repeats"):
            try:
                delattr(capture_data, attr)
            except AttributeError:
                pass
    except Exception:
        logger.exception("Tab walk failed")


# ─── Backward tab walk ──────────────────────────────────────────────────────

async def _backward_tab_walk(
    page: Page,
    capture_data: CaptureData,
) -> None:
    """Press Shift+Tab repeatedly from current position and record focus order.

    Runs after the forward tab walk completes, so focus starts wherever
    the forward walk left off.  Uses the same trap detection logic.
    Stops when focus returns to <body> or a trap is detected.
    """
    try:
        # Same reset-before-measure discipline as _tab_walk: if the
        # forward walk ended inside an opened dropdown/modal (last
        # element activated something), Shift+Tab from there would
        # cycle inside the widget forever.
        for _ in range(3):
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(100)

        tab_order: list[dict] = []
        traps: list[dict] = []
        recent_selectors: list[str] = []
        segmented_input_selectors: set[str] = set()
        consecutive_body = 0

        i = 0
        truncated = False
        halted_by_trap = False
        while True:
            i += 1
            if i > MAX_TAB_ITERATIONS:
                truncated = True
                from collections import Counter as _Cnt
                all_sels = [(t.get("selector") or "?") for t in tab_order]
                unique_sels = set(all_sels)
                body_hits = sum(1 for s in all_sels if s == "body")
                top = _Cnt(all_sels).most_common(5)
                top_str = ", ".join(
                    f'"{s}"x{c}' for s, c in top
                )
                last10 = list(all_sels[-10:])
                logger.warning(
                    "_backward_tab_walk: reached MAX_TAB_ITERATIONS=%d "
                    "without returning to <body>. Diagnostic: %d Shift+"
                    "Tab stops recorded, %d unique selectors, body "
                    "observed %d times. Top frequencies: [%s]. Last 10 "
                    "selectors: %s.",
                    MAX_TAB_ITERATIONS,
                    len(all_sels), len(unique_sels), body_hits,
                    top_str, last10,
                )
                break
            await page.keyboard.press("Shift+Tab")
            await page.wait_for_timeout(100)

            info = await page.evaluate("() => {" + _UNIQUE_SELECTOR_SHADOW_JS + """
                function deepActiveElement() {
                    let el = document.activeElement;
                    while (el && el.shadowRoot && el.shadowRoot.activeElement) {
                        el = el.shadowRoot.activeElement;
                    }
                    return el;
                }
                const el = deepActiveElement();
                if (!el) return null;
                const rect = el.getBoundingClientRect();
                const cs = window.getComputedStyle(el);
                const outlineWidth = cs.outlineWidth || '0px';
                const outlineStyle = cs.outlineStyle || 'none';
                const boxShadow = cs.boxShadow || '';
                const borderWidth = cs.borderWidth || '';
                const hasOutline = outlineStyle !== 'none' && outlineWidth !== '0px';
                const hasShadow = boxShadow !== 'none' && boxShadow !== '';
                const hasBorder = borderWidth !== '0px';
                const isVisible = hasOutline || hasShadow || hasBorder;
                const selector = uniqueSelector(el);
                let rootNode = el.getRootNode ? el.getRootNode() : document;
                const inShadow = rootNode && rootNode.host !== undefined;
                return {
                    tag: el.tagName.toLowerCase(),
                    type: (el.getAttribute('type') || '').toLowerCase(),
                    role: el.getAttribute('role') || '',
                    text: (el.textContent || '').trim(),
                    selector: selector,
                    has_visible_indicator: isVisible,
                    indicator_type: hasOutline ? 'outline' : hasShadow ? 'box-shadow' : hasBorder ? 'border' : 'none',
                    rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                    in_shadow_dom: !!inShadow,
                };
            }""")
            if not info:
                continue

            if info["tag"] == "body":
                consecutive_body += 1
                if consecutive_body >= 3:
                    break
                # Reset trap-detection window — cycling through body is
                # normal browser behavior, not a trap. See the forward
                # _tab_walk for the full rationale.
                recent_selectors.clear()
                continue
            consecutive_body = 0

            tab_order.append(info)
            recent_selectors.append(info["selector"])
            if info.get("tag") == "input" and info.get("type") in _SEGMENTED_INPUT_TYPES:
                segmented_input_selectors.add(info["selector"])

            # --- Full-wrap detection ---
            # Same rationale as the forward walk: pages that wrap focus
            # from first to last without passing through <body> would
            # spin the walk until MAX_TAB_ITERATIONS otherwise. Use a
            # DIFFERENT attribute name from the forward walk so their
            # state never collides.
            if not hasattr(capture_data, "_btw_unique"):
                capture_data._btw_unique = set()
                capture_data._btw_repeats = 0
            bsel = info["selector"]
            if bsel in capture_data._btw_unique:
                capture_data._btw_repeats += 1
            else:
                capture_data._btw_unique.add(bsel)
                capture_data._btw_repeats = 0
            if (
                capture_data._btw_repeats >= 30
                and len(capture_data._btw_unique) >= 20
            ):
                logger.info(
                    "_backward_tab_walk: stop at iter %d -- full cycle "
                    "detected. %d unique selectors seen, last 30 "
                    "Shift+Tabs added no new elements.",
                    i, len(capture_data._btw_unique),
                )
                for attr in ("_btw_unique", "_btw_repeats"):
                    try:
                        delattr(capture_data, attr)
                    except AttributeError:
                        pass
                break

            # Native segmented/spinner inputs (date/time/number/…) focus
            # their internal segments on successive Tabs, all resolving to
            # the host selector — so they repeat both consecutively AND by
            # frequency. Continued Tab DOES leave the field, so this is not
            # a keyboard trap. Skip ALL trap detectors for this iteration
            # (covers the consecutive, cycle, and frequency detectors below
            # in one place). i is incremented at the top of the loop, so a
            # bare continue is correct.
            if info.get("tag") == "input" and info.get("type") in _SEGMENTED_INPUT_TYPES:
                continue

            # --- Trap detection ---
            # Consecutive same element (only across a body-free run).
            if len(recent_selectors) >= TRAP_CONSECUTIVE_THRESHOLD:
                last_n = recent_selectors[-TRAP_CONSECUTIVE_THRESHOLD:]
                if len(set(last_n)) == 1:
                    traps.append({
                        "type": "consecutive",
                        "selector": info["selector"],
                        "tab_index": i,
                        "description": f"Element {info['selector']} received focus "
                                        f"{TRAP_CONSECUTIVE_THRESHOLD} consecutive times (backward)",
                    })
                    break

            # Cycling pattern A-B-A-B-A
            if len(recent_selectors) >= TRAP_CYCLE_REPEATS:
                last_cycle = recent_selectors[-TRAP_CYCLE_REPEATS:]
                a, b = last_cycle[0], last_cycle[1]
                if a != b:
                    expected = [a, b] * (TRAP_CYCLE_REPEATS // 2)
                    if TRAP_CYCLE_REPEATS % 2:
                        expected.append(a)
                    if last_cycle == expected:
                        traps.append({
                            "type": "cycle",
                            "selectors": [a, b],
                            "tab_index": i,
                            "description": f"Focus cycling between {a} and {b} (backward)",
                        })
                        break

            # Frequency-based cycle detection — if the same element
            # appears 4+ times in the last 8 steps, it's a trap.
            if len(recent_selectors) >= 8:
                last_n = recent_selectors[-8:]
                counts = Counter(last_n)
                most_common_sel, most_common_count = counts.most_common(1)[0]
                if most_common_count >= 4 and most_common_sel in segmented_input_selectors:
                    # Native segmented input — internal segments repeat the
                    # host selector but continued Tab leaves it; not a trap.
                    logger.debug(
                        "Backward walk: %s repeated %d× but is a native "
                        "segmented input — skipping trap",
                        most_common_sel, most_common_count,
                    )
                elif most_common_count >= 4:
                    traps.append({
                        "type": "frequency_cycle",
                        "selector": most_common_sel,
                        "tab_index": i,
                        "description": (
                            f"Element {most_common_sel} received focus "
                            f"{most_common_count} times in the last 8 steps (backward)"
                        ),
                    })
                    truncated = True
                    halted_by_trap = True
                    break

        capture_data.backward_tab_walk = tab_order
        # Append any backward traps to the shared keyboard_traps list
        capture_data.keyboard_traps.extend(traps)
        capture_data.tab_walk_truncated = {
            **(capture_data.tab_walk_truncated or {}),
            "backward": truncated,
            "backward_halted_by_trap": halted_by_trap,
        }
        for attr in ("_btw_unique", "_btw_repeats"):
            try:
                delattr(capture_data, attr)
            except AttributeError:
                pass
    except Exception:
        logger.exception("Backward tab walk failed")


# ─── Tab coverage comparison ────────────────────────────────────────────────

async def _tab_coverage_comparison(
    page: Page,
    capture_data: CaptureData,
) -> None:
    """Option-A inventory crosscheck: verify every interactive element is
    Tab-reachable, and for each one that is NOT, test whether direct
    ``element.focus()`` works anyway.

    Produces ``capture_data.tab_coverage`` with three sharper buckets:

      reached_by_tab
          Selectors the forward Tab walk actually visited.
      focusable_but_skipped
          Elements that ``element.focus()`` accepts, BUT the Tab walk
          never visited -- a real SC 2.1.1 / 2.4.3 violation. The user
          CAN focus them with other means (click, ARIA, screen reader)
          but a keyboard-only user can't reach them by tabbing.
      not_focusable_at_all
          Elements present in the DOM that match an interactive
          selector, yet ``document.activeElement`` refuses to settle
          on them after ``el.focus()``. These are either inert
          (disabled, hidden by parent), display:none ancestors, or
          have tabindex="-1" with no programmatic focus path -- still
          meaningful for SC 2.1.1 because the author likely intended
          them to be operable.

    The classification replaces the prior ``unreached_selectors`` list
    that only told SC 2.1.1 "tab walk missed something" without
    distinguishing "skipped" from "can't focus at all."

    Uses the SAME selector format as ``_tab_walk`` so the set diff is
    reliable -- earlier the two functions built slightly different
    selector strings for the same element, producing false-positive
    "unreached" entries.
    """
    try:
        result = await page.evaluate("() => {" + _UNIQUE_SELECTOR_SHADOW_JS + """
            // Selector builder MUST match the one in _tab_walk so the
            // diff against capture_data.tab_walk is apples-to-apples.
            // (Structural nth-of-type path, ID shortcut, shadow DOM
            // crossing markers.)

            function isVisible(el) {
                const style = getComputedStyle(el);
                if (style.visibility === 'hidden') return false;
                if (style.display === 'none') return false;
                if (el.offsetParent === null && style.position !== 'fixed') return false;
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) return false;
                return true;
            }

            // Enumerate every element that *should* be interactive. We
            // cast a wide net -- the distinction between "should be
            // focusable" and "actually is focusable" is exactly what
            // we're measuring.
            const selectors = [
                'a[href]', 'button', 'input', 'select', 'textarea',
                '[role="button"]', '[role="link"]', '[role="checkbox"]',
                '[role="radio"]', '[role="tab"]', '[role="menuitem"]',
                '[role="menuitemcheckbox"]', '[role="menuitemradio"]',
                '[role="option"]', '[role="switch"]', '[role="slider"]',
                '[role="spinbutton"]', '[role="combobox"]', '[role="textbox"]',
                '[role="searchbox"]', '[role="treeitem"]',
                '[role="row"]', '[role="gridcell"]',
                '[role="columnheader"]', '[role="rowheader"]',
                '[tabindex]',  // any tabindex, including negative for the focusable test
                '[onclick]', '[onkeydown]', '[onkeyup]', '[onkeypress]',
                'details > summary',
            ];

            // ARIA composite widgets use a "roving tabindex": only ONE
            // member of the group is Tab-focusable at a time; the
            // others are reached via arrow keys. An element that
            // element.focus() accepts but Tab skips is NOT a SC 2.1.1
            // violation if it is inside one of these widgets AND at
            // least one sibling in the same widget IS in the Tab
            // order -- that sibling is the user's entry point, arrow
            // keys handle the rest.
            //
            // Map: child-role -> list of container-role(s) the child
            // must be nested under for this pattern to apply.
            const ROVING_PARENTS = {
                'radio':             ['radiogroup'],
                'tab':               ['tablist'],
                'menuitem':          ['menu', 'menubar'],
                'menuitemcheckbox':  ['menu', 'menubar'],
                'menuitemradio':     ['menu', 'menubar'],
                'treeitem':          ['tree', 'group'],
                'option':            ['listbox'],
                'row':               ['grid', 'treegrid', 'rowgroup'],
                'gridcell':          ['row', 'grid', 'treegrid'],
                'columnheader':      ['row', 'grid', 'treegrid'],
                'rowheader':         ['row', 'grid', 'treegrid'],
            };

            function rovingGroupMembers(el) {
                // Returns an array of sibling selectors (excluding el
                // itself) that share el's roving-tabindex widget. An
                // empty array means el is NOT in a roving pattern.
                if (!el) return [];
                // 1. Native radio buttons with a shared name attribute
                //    form a browser-managed radio group -- same arrow-
                //    keys-within-Tab-order semantics as role="radio".
                if (
                    el.tagName === 'INPUT'
                    && (el.type === 'radio' || el.getAttribute('type') === 'radio')
                    && el.name
                ) {
                    const scope = el.form || document;
                    const name = el.name;
                    const siblings = [];
                    scope.querySelectorAll(
                        'input[type="radio"]'
                    ).forEach(other => {
                        if (other !== el && other.name === name) {
                            siblings.push(uniqueSelector(other));
                        }
                    });
                    if (siblings.length > 0) return siblings;
                }
                // 2. ARIA composite widgets: child role nested under a
                //    matching container role.
                const role = (el.getAttribute('role') || '').toLowerCase();
                const parentRoles = ROVING_PARENTS[role];
                if (parentRoles) {
                    let cur = el.parentElement;
                    while (cur) {
                        const pr = (cur.getAttribute('role') || '').toLowerCase();
                        if (parentRoles.indexOf(pr) >= 0) {
                            // Collect every member in the container with
                            // the same child role. querySelectorAll on
                            // the container scope only catches direct
                            // descendants we care about for most
                            // widgets; for nested trees that's fine too
                            // (treeitem children are still treeitems).
                            const members = [];
                            cur.querySelectorAll(
                                '[role="' + role + '"]'
                            ).forEach(other => {
                                if (other !== el) {
                                    members.push(uniqueSelector(other));
                                }
                            });
                            return members;
                        }
                        cur = cur.parentElement;
                    }
                }
                // 3. role="toolbar" groups every focusable descendant
                //    under a shared roving tabindex. Ancestor walk so
                //    deeply-nested buttons inside a toolbar still count.
                const toolbar = el.closest ? el.closest('[role="toolbar"]') : null;
                if (toolbar && toolbar !== el) {
                    const members = [];
                    toolbar.querySelectorAll(
                        'a[href], button, input, select, textarea, '
                        + '[role="button"], [role="link"], [tabindex]'
                    ).forEach(other => {
                        if (other !== el) members.push(uniqueSelector(other));
                    });
                    return members;
                }
                return [];
            }

            const seen = new Set();
            const candidates = [];
            for (const s of selectors) {
                const els = document.querySelectorAll(s);
                for (const el of els) {
                    if (seen.has(el)) continue;
                    seen.add(el);
                    if (!isVisible(el)) continue;
                    candidates.push(el);
                }
            }

            // For each candidate: build its selector AND test whether
            // el.focus() actually lands focus on it. We save/restore
            // the previous activeElement so the page state is
            // unchanged after the probe.
            const savedActive = document.activeElement;
            const probe = [];
            for (const el of candidates) {
                const selector = uniqueSelector(el);
                let focus_works = false;
                try {
                    el.focus({preventScroll: true});
                    // Some elements briefly accept focus then defer; wait
                    // a microtask via the synchronous active-element read.
                    focus_works = (document.activeElement === el);
                } catch (_) {
                    focus_works = false;
                }
                // aria-hidden walks up the tree -- a child of an
                // aria-hidden=true subtree is itself hidden from AT.
                let aria_hidden = false;
                for (let a = el; a; a = a.parentElement) {
                    if (a.getAttribute && a.getAttribute('aria-hidden') === 'true') {
                        aria_hidden = true;
                        break;
                    }
                }
                const inert = !!(el.closest && el.closest('[inert]'));
                probe.push({
                    selector,
                    tag: el.tagName ? el.tagName.toLowerCase() : '?',
                    role: el.getAttribute('role') || '',
                    tabindex: el.getAttribute('tabindex'),
                    disabled: !!el.disabled,
                    aria_hidden,
                    inert,
                    focus_works,
                    roving_group_members: rovingGroupMembers(el),
                });
            }
            // Restore prior focus (best-effort).
            try {
                if (savedActive && savedActive !== document.body) {
                    savedActive.focus({preventScroll: true});
                } else {
                    document.body.focus();
                }
            } catch (_) {}

            return probe;
        }""")

        # Selectors the Tab walk actually visited (forward walk only;
        # backward walk is also relevant but SC 2.1.1 focuses on forward).
        reached_selectors: set[str] = set()
        for item in capture_data.tab_walk:
            sel = item.get("selector", "")
            if sel:
                reached_selectors.add(sel)
        reached_by_tab = len(reached_selectors)

        # Classify every candidate.
        #
        # ARIA composite widgets (radiogroup, tablist, menu, tree,
        # listbox, grid, toolbar) intentionally keep only ONE member
        # of the group in the Tab order -- the "roving tabindex"
        # pattern from the ARIA Authoring Practices. The other members
        # are reached via arrow keys within the widget. If the element
        # was NOT visited by Tab but (a) element.focus() works and
        # (b) at least one group member WAS visited by Tab, the user
        # can reach the widget through that entry-point member and
        # arrow-key to this element -- SC 2.1.1 is satisfied.
        #
        # Counting those as "focusable but skipped" would generate
        # false positives on every radio group, tab strip, menu, and
        # carousel in existence. The roving_group_members list comes
        # from the JS rovingGroupMembers() helper and covers every
        # composite widget the authoring practices list.
        focusable_but_skipped: list[dict] = []
        not_focusable_at_all: list[dict] = []
        roving_valid: list[dict] = []
        total_interactive = len(result) if isinstance(result, list) else 0
        for entry in result or []:
            if not isinstance(entry, dict):
                continue
            sel = entry.get("selector", "")
            if sel and sel in reached_selectors:
                continue
            members = entry.get("roving_group_members") or []
            group_has_tab_entry = any(
                m in reached_selectors for m in members if isinstance(m, str)
            )
            if members and group_has_tab_entry and entry.get("focus_works"):
                # Valid roving tabindex: Tab reaches the group through
                # a sibling, arrow keys reach this element. Keep for
                # telemetry but do NOT report as SC 2.1.1 violation.
                roving_valid.append({
                    "selector": sel,
                    "tag": entry.get("tag", ""),
                    "role": entry.get("role", ""),
                    "tabindex": entry.get("tabindex"),
                    "group_size": len(members) + 1,
                })
                continue
            # An element intentionally removed from the operable
            # interface — aria-hidden=true subtree, or inert ancestor —
            # is NOT a 2.1.1 failure. The author has explicitly excluded
            # it; classifying it as "focusable but skipped" produces a
            # false positive (verified on a university site: a decorative
            # <video tabindex=-1 aria-hidden=true muted> was being
            # surfaced as a 2.1.1 keyboard violation).
            if entry.get("aria_hidden") or entry.get("inert"):
                continue
            bucket = focusable_but_skipped if entry.get("focus_works") else not_focusable_at_all
            bucket.append({
                "selector": sel,
                "tag": entry.get("tag", ""),
                "role": entry.get("role", ""),
                "tabindex": entry.get("tabindex"),
                "disabled": entry.get("disabled", False),
                "aria_hidden": entry.get("aria_hidden", False),
                "inert": entry.get("inert", False),
            })

        coverage_percent = (
            round((reached_by_tab / total_interactive) * 100, 2)
            if total_interactive > 0
            else 0.0
        )

        truncation = capture_data.tab_walk_truncated or {}
        forward_truncated = bool(truncation.get("forward"))
        backward_truncated = bool(truncation.get("backward"))
        forward_halted_by_trap = bool(truncation.get("forward_halted_by_trap"))
        backward_halted_by_trap = bool(truncation.get("backward_halted_by_trap"))
        capture_data.tab_coverage = {
            "total_interactive": total_interactive,
            "reached_by_tab": reached_by_tab,
            "coverage_percent": coverage_percent,
            "focusable_but_skipped": focusable_but_skipped,
            "not_focusable_at_all": not_focusable_at_all,
            # Elements correctly excluded from Tab order by the ARIA
            # roving-tabindex pattern (radio groups, tablists, menus,
            # trees, listboxes, grids, toolbars). The user reaches them
            # via arrow keys through a tab-focusable sibling. Kept as
            # telemetry so the judge can see the count but NEVER fed
            # as SC 2.1.1 candidates.
            "roving_tabindex_valid": roving_valid,
            # Back-compat: old consumers that look at unreached_selectors
            # still get a flat list of everything Tab missed. Does NOT
            # include roving-valid entries (those are not violations).
            "unreached_selectors": sorted(
                [e["selector"] for e in focusable_but_skipped]
                + [e["selector"] for e in not_focusable_at_all]
            ),
            # When truncated, the Tab walk hit MAX_TAB_ITERATIONS before
            # returning to <body>, so reached_by_tab / coverage_percent
            # are a lower bound only. Consumers (judge prompt builder,
            # SC checks) must rely on focusable_but_skipped /
            # not_focusable_at_all -- those are deterministic because
            # they probe element.focus() directly.
            "forward_walk_truncated": forward_truncated,
            "backward_walk_truncated": backward_truncated,
            "walk_truncated": forward_truncated or backward_truncated,
            # Distinguish "halted by detected keyboard trap" from "hit
            # MAX_TAB_ITERATIONS." When a trap halts the walk, every
            # downstream "focusable_but_skipped" entry is an artifact of
            # the trap (the walk physically could not reach those
            # elements), NOT a genuine SC 2.1.1 violation. SC 2.1.1's
            # finding logic must suppress the focusable_but_skipped
            # finding in this case. The real failure is the trap, which
            # SC 2.1.2 reports independently.
            "forward_halted_by_trap": forward_halted_by_trap,
            "backward_halted_by_trap": backward_halted_by_trap,
            "halted_by_trap": forward_halted_by_trap or backward_halted_by_trap,
        }

        # ARIA-role check above recognizes standards-compliant composite
        # widgets. It misses custom widgets (div-based carousels, in-
        # house dropdowns) that implement arrow-key navigation without
        # declaring role="tablist" / "listbox" / etc. For every survivor
        # in focusable_but_skipped, do a real keyboard probe: press
        # arrows/Home/End/Escape/Tab and see if the element is in fact
        # operable. This measures behavior rather than markup and works
        # for novel frameworks or custom widgets.
        still_candidates = list(focusable_but_skipped)
        custom_arrow_navigable: list[dict] = []
        trap_findings: list[dict] = []
        if still_candidates:
            logger.info(
                "Tab coverage: probing keyboard behavior of %d focusable-"
                "but-skipped element(s) to detect custom arrow-navigable "
                "widgets and traps",
                len(still_candidates),
            )
            for cand in still_candidates:
                sel = cand.get("selector", "")
                if not sel:
                    continue
                probe = await _probe_widget_keyboard_behavior(page, sel)
                cand["keyboard_probe"] = probe
                if probe.get("arrow_navigable") or probe.get("items_reached", 0) > 1:
                    custom_arrow_navigable.append({
                        "selector": sel,
                        "tag": cand.get("tag", ""),
                        "role": cand.get("role", ""),
                        "tabindex": cand.get("tabindex"),
                        "items_reached": probe.get("items_reached", 0),
                        "bidirectional_ok": probe.get("bidirectional_ok", False),
                    })
                if probe.get("is_trap"):
                    # Surface as a SC 2.1.2 finding -- element cannot be
                    # exited via Escape, Tab, or Shift+Tab.
                    trap_findings.append({
                        "type": "custom_widget_trap",
                        "selector": sel,
                        "can_exit": False,
                        "description": (
                            f"Custom widget at {sel} traps keyboard focus: "
                            f"Escape={probe.get('escape_exits')}, "
                            f"Tab={probe.get('tab_exits')}, "
                            f"Shift+Tab={probe.get('shift_tab_exits')}"
                        ),
                    })

            reclass = {c["selector"] for c in custom_arrow_navigable}
            focusable_but_skipped = [
                c for c in focusable_but_skipped
                if c.get("selector") not in reclass
            ]

        if custom_arrow_navigable:
            capture_data.tab_coverage["custom_arrow_navigable"] = custom_arrow_navigable
            capture_data.tab_coverage["focusable_but_skipped"] = focusable_but_skipped
        if trap_findings:
            capture_data.keyboard_traps.extend(trap_findings)

        logger.info(
            "Tab coverage: %d interactive, %d reached by Tab (%.1f%%), "
            "%d focusable-but-skipped (SC 2.1.1 candidates), "
            "%d not-focusable-at-all, "
            "%d roving-tabindex-valid (ARIA, arrow-key reachable), "
            "%d custom-arrow-navigable (behaviour-verified), "
            "%d trap(s) discovered by keyboard probe",
            total_interactive, reached_by_tab, coverage_percent,
            len(focusable_but_skipped), len(not_focusable_at_all),
            len(roving_valid), len(custom_arrow_navigable), len(trap_findings),
        )
    except Exception:
        logger.exception("Tab coverage comparison failed")


# ─── Keyboard behaviour probe ──────────────────────────────────────────────

async def _probe_widget_keyboard_behavior(
    page: Page,
    target_selector: str,
) -> dict:
    """Press real keyboard events from an element and record what happens.

    The probe measures what a keyboard user would observe when they land
    on ``target_selector``. It answers four questions for SC 2.1.1 /
    2.1.2 classification:

      1. Does ANY arrow key move focus off the element? (arrow_navigable)
      2. Is the navigation bidirectional -- does the reverse key return
         to origin? (bidirectional_ok)
      3. How many distinct items are reachable by walking the dominant
         arrow key forward? (items_reached, items_visited)
      4. Can the user EXIT the widget via Escape, Tab, or Shift+Tab?
         (escape_exits, tab_exits, shift_tab_exits, is_trap)

    Intentionally does NOT press Enter or Space. Activation on a link
    navigates away (destroys the capture state), and activation on a
    submit button submits the form (same problem). Arrow navigation and
    the exit tests together cover what SC 2.1.1/2.1.2 need.

    The probe saves and restores document.activeElement and the page
    URL; if the URL changes mid-probe (e.g. an arrow key triggered
    JS that reloads the page), the remaining tests are skipped and the
    probe reports ``error="url changed"`` so the caller knows the data
    is incomplete.

    Return shape (never None):
      {
        arrow_navigable: bool,
        bidirectional_ok: bool,           # all tested reversals restored origin
        directions_tested: list[str],     # keys attempted
        moves: {key: target_selector},    # where each arrow landed
        items_reached: int,               # distinct selectors visited in coverage walk
        items_visited: list[str],
        wraps: bool,                      # coverage walk looped back to start
        max_iterations_hit: bool,         # coverage walk hit the 30-iter cap
        escape_exits: bool,
        tab_exits: bool,
        shift_tab_exits: bool,
        is_trap: bool,                    # NONE of the three exits worked
        error: str,                       # '' on success
      }

    A best-effort probe: any exception at any stage falls through to a
    safe restore so a misbehaving widget can't crash the capture.
    """
    import asyncio as _asyncio

    COVERAGE_MAX = 30
    PROBE_TIMEOUT_S = 25.0

    result: dict = {
        "arrow_navigable": False,
        "bidirectional_ok": True,
        "directions_tested": [],
        "moves": {},
        "items_reached": 0,
        "items_visited": [],
        "wraps": False,
        "max_iterations_hit": False,
        "escape_exits": False,
        "tab_exits": False,
        "shift_tab_exits": False,
        "is_trap": False,
        "error": "",
    }

    async def _active_selector() -> str | None:
        try:
            return await page.evaluate(
                "() => {" + _UNIQUE_SELECTOR_SHADOW_JS + """
                    let el = document.activeElement;
                    while (el && el.shadowRoot && el.shadowRoot.activeElement) {
                        el = el.shadowRoot.activeElement;
                    }
                    if (!el || el === document.body) return null;
                    // Same selector builder as _tab_walk so the probe's
                    // before/after comparisons stay aligned with the
                    // existing reached_selectors set.
                    return uniqueSelector(el);
                }"""
            )
        except Exception:
            logger.warning(
                "Arrow-key probe: active-element selector read failed for "
                "target %s — treating focus position as unknown",
                target_selector, exc_info=True,
            )
            return None

    async def _press(key: str) -> None:
        try:
            await page.keyboard.press(key)
            await page.wait_for_timeout(80)
        except Exception:
            pass  # best-effort — key press may fail if page navigated/closed mid-press

    async def _focus_target() -> bool:
        try:
            ok = await page.evaluate(
                """sel => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    try { el.focus({preventScroll: true}); } catch (_) { return false; }
                    return document.activeElement === el;
                }""",
                target_selector,
            )
            await page.wait_for_timeout(50)
            return bool(ok)
        except Exception:
            logger.warning(
                "Arrow-key probe: focus() attempt failed for %s — probe "
                "will report 'could not focus target'",
                target_selector, exc_info=True,
            )
            return False

    async def _run_probe() -> None:
        initial_url = page.url
        saved_active = await _active_selector()

        if not await _focus_target():
            result["error"] = "could not focus target"
            return

        start = await _active_selector()
        if start != target_selector:
            # Some widgets redirect focus (e.g. to a child) -- that's OK,
            # treat the landed selector as the probe origin.
            start = await _active_selector()
        if not start:
            result["error"] = "no active element after focus"
            return

        pairs = [
            ("ArrowRight", "ArrowLeft"),
            ("ArrowDown",  "ArrowUp"),
            ("End",        "Home"),
        ]
        for forward, reverse in pairs:
            if page.url != initial_url:
                result["error"] = "url changed"
                return
            # Re-anchor focus at start before each pair so one pair's
            # side-effects don't bleed into the next.
            await _focus_target()
            before = await _active_selector()
            result["directions_tested"].append(forward)
            await _press(forward)
            after_forward = await _active_selector()
            moved_forward = bool(after_forward and after_forward != before)
            result["moves"][forward] = after_forward or ""
            if moved_forward:
                result["arrow_navigable"] = True
                result["directions_tested"].append(reverse)
                await _press(reverse)
                after_reverse = await _active_selector()
                result["moves"][reverse] = after_reverse or ""
                if after_reverse != before:
                    result["bidirectional_ok"] = False

        # Coverage walk with the most successful forward key.
        forward_candidates = [
            k for k in ("ArrowRight", "ArrowDown", "End")
            if result["moves"].get(k) and result["moves"][k] != target_selector
        ]
        if forward_candidates:
            walk_key = forward_candidates[0]
            await _focus_target()
            visited: list[str] = []
            cur = await _active_selector()
            if cur:
                visited.append(cur)
            for i in range(COVERAGE_MAX):
                if page.url != initial_url:
                    result["error"] = "url changed during coverage walk"
                    break
                await _press(walk_key)
                nxt = await _active_selector()
                if not nxt or nxt in visited:
                    if nxt and visited and nxt == visited[0]:
                        result["wraps"] = True
                    break
                visited.append(nxt)
            else:
                result["max_iterations_hit"] = True
            result["items_visited"] = visited
            result["items_reached"] = len(visited)

        # Exit tests. For each, re-anchor and verify the key causes focus
        # to leave the widget's member set. "Left the widget" = the
        # landed selector is NOT in any member set we've seen so far.
        widget_members = set(result["items_visited"]) | {target_selector}
        widget_members.update(v for v in result["moves"].values() if v)
        widget_members.discard("")

        async def _test_exit(keys: list[str]) -> bool:
            await _focus_target()
            if page.url != initial_url:
                return False
            for k in keys:
                await _press(k)
                if page.url != initial_url:
                    return False
            after = await _active_selector()
            return bool(after and after not in widget_members)

        result["escape_exits"]   = await _test_exit(["Escape"])
        result["tab_exits"]      = await _test_exit(["Tab"])
        result["shift_tab_exits"] = await _test_exit(["Shift+Tab"])
        result["is_trap"] = not (
            result["escape_exits"] or result["tab_exits"] or result["shift_tab_exits"]
        )

        # Restore focus (best-effort).
        if saved_active:
            try:
                await page.evaluate(
                    """sel => {
                        const el = document.querySelector(sel);
                        if (el) try { el.focus({preventScroll: true}); } catch (_) {}
                    }""",
                    saved_active,
                )
            except Exception:
                pass  # cleanup — best-effort restore of saved focus, page state may be unrecoverable
        else:
            try:
                await page.evaluate("document.body.focus()")
            except Exception:
                pass  # cleanup — best-effort fallback focus on body, page state may be unrecoverable

    try:
        await _asyncio.wait_for(_run_probe(), timeout=PROBE_TIMEOUT_S)
    except _asyncio.TimeoutError:
        result["error"] = f"probe timed out after {PROBE_TIMEOUT_S}s"
    except Exception as exc:
        result["error"] = f"probe exception: {exc}"
        logger.exception("Keyboard probe failed for %s", target_selector)

    logger.info(
        "Keyboard probe: %s -- arrow_navigable=%s, items_reached=%d, "
        "bidirectional=%s, escape=%s, tab=%s, shift_tab=%s, trap=%s, err=%r",
        target_selector, result["arrow_navigable"], result["items_reached"],
        result["bidirectional_ok"], result["escape_exits"],
        result["tab_exits"], result["shift_tab_exits"], result["is_trap"],
        result["error"] or "none",
    )
    return result


# ─── Recorded keyboard walkthrough ──────────────────────────────────────────

async def _recorded_keyboard_walkthrough(
    page: Page,
    capture_data: CaptureData,
    captures_dir: str,
) -> None:
    """Record a video of a comprehensive keyboard walkthrough.

    Opens a new browser context with Playwright video recording, navigates
    to the page, and performs:
      1. Forward Tab through all elements (with visible focus)
      2. Enter/Space on buttons, links, dropdowns
      3. Arrow keys through menus/dropdowns
      4. Escape to close popups
      5. Shift+Tab backward

    The resulting video is sent to the AI VL model so it can evaluate
    keyboard accessibility with full visual context — focus indicators
    moving, dropdowns opening, traps occurring, etc.
    """
    from playwright.async_api import async_playwright

    try:
        video_dir = os.path.join(captures_dir, "keyboard_walkthrough")
        os.makedirs(video_dir, exist_ok=True)

        current_url = page.url
        walkthrough_log: list[dict] = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                record_video_dir=video_dir,
                record_video_size={"width": 1280, "height": 720},
            )
            rec_page = await context.new_page()
            await _safe_goto(rec_page, current_url, timeout=60000)
            await rec_page.wait_for_timeout(2000)  # Let page fully settle

            # Start from body
            await rec_page.evaluate("document.body.focus()")
            await rec_page.wait_for_timeout(500)

            # ── Phase 1: Forward Tab with activation ────────────────
            # Pacing: 700ms between every action so each state is clearly
            # visible in the recording for AI analysis.
            ACTION_WAIT = 700  # ms between every action — human-like pace
            seen_selectors: set[str] = set()
            # Unbounded walkthrough. Natural break conditions live inside
            # the loop: focus returns to body (None/tag==body) and we stop,
            # or consecutive duplicates indicate the tab order has cycled.
            walk_consec_body = 0
            walk_consec_dup = 0
            # Duplicate budget granted after a trap recovery: the outer
            # loop must re-traverse already-seen tab stops (from the top
            # of the page after a reload / click-outside) without the
            # cycle detector reading that re-traversal as a focus trap.
            walk_dup_grace = 0
            while True:
                await rec_page.keyboard.press("Tab")
                await rec_page.wait_for_timeout(ACTION_WAIT)

                info = await rec_page.evaluate("""() => {
                    const el = document.activeElement;
                    if (!el || el.tagName.toLowerCase() === 'body') return null;
                    let selector = el.tagName.toLowerCase();
                    if (el.id) selector = '#' + el.id;
                    else if (el.className && typeof el.className === 'string')
                        selector = el.tagName.toLowerCase() + '.' + el.className.trim().split(/\\s+/)[0];
                    return {
                        tag: el.tagName.toLowerCase(),
                        role: el.getAttribute('role') || '',
                        text: (el.textContent || '').trim(),
                        selector: selector,
                        ariaHasPopup: el.getAttribute('aria-haspopup') || '',
                        ariaExpanded: el.getAttribute('aria-expanded') || '',
                        type: el.getAttribute('type') || '',
                    };
                }""")

                # Focus landed on <body> (or nothing) -- this happens
                # naturally when focus wraps past the last tabstop, and
                # ALSO transiently when Escape closes a widget and
                # leaves focus momentarily lost. Treating a single
                # body-hit as end-of-walkthrough terminates the walk
                # prematurely after exploring one widget (observed on
                # a university site 2026-04-24: walkthrough stopped at 15 actions
                # right after entering a widget, Arrow-Down-exploring,
                # and Escape-ing out). Give focus several chances to
                # recover; only call the walk done when we've been on
                # body for multiple consecutive Tabs.
                if not info or info.get("tag") == "body":
                    walk_consec_body += 1
                    walkthrough_log.append({
                        "action": "tab_landed_body",
                        "consec_body": walk_consec_body,
                    })
                    if walk_consec_body >= 4:
                        # Four consecutive body-hits with no new element
                        # reached: the walk has either truly finished or
                        # focus is stuck. Stop.
                        break
                    # Keep pressing Tab -- the page may have more
                    # focusable content after a widget side-effect
                    # kicked focus to body.
                    continue
                walk_consec_body = 0

                sel = info["selector"]
                walkthrough_log.append({"action": "tab", "element": sel, **info})

                # Cycle detection for the walkthrough loop. Without it, a
                # focus trap that never passes through <body> would loop
                # forever (e.g. [A, B, C, D, A, B, C, D, ...]). If every
                # selector in a long consecutive run has already been seen,
                # the tab order has cycled and further Tab presses will
                # only revisit covered elements.
                if sel in seen_selectors:
                    if walk_dup_grace > 0:
                        # Post-recovery re-traversal of known tab stops —
                        # not a cycle.
                        walk_dup_grace -= 1
                        continue
                    walk_consec_dup += 1
                    # 20 consecutive already-seen focuses = confident cycle
                    if walk_consec_dup >= 20:
                        walkthrough_log.append({
                            "action": "tab_cycle_complete",
                            "last_selector": sel,
                            "distinct_selectors_visited": len(seen_selectors),
                        })
                        break
                    continue
                walk_consec_dup = 0
                walk_dup_grace = 0
                seen_selectors.add(sel)

                # ── Activate interactive elements ────────────
                tag = info["tag"]
                role = info["role"]
                has_popup = info["ariaHasPopup"]
                expanded = info["ariaExpanded"]

                # Unified candidate path: any element that *might* open an
                # overlay — advertised popups, menu/combobox/listbox roles,
                # plain <button>, <details>. We press Enter, then *measure*
                # whether an overlay opened (aria-expanded flip, or new visible
                # overlay node appeared). If something did open, run the full
                # dropdown-verify flow: Arrow down → Escape → if still open,
                # retry with Tab as the alternate escape path (WCAG 2.1.2
                # permits any standard exit, not specifically Escape).
                is_dropdown_candidate = (
                    has_popup
                    or role in ("menu", "menuitem", "combobox", "listbox", "button")
                    or tag in ("button", "details")
                )

                if is_dropdown_candidate:
                    overlay_before = await rec_page.evaluate(_OVERLAY_SNAPSHOT_JS)
                    url_before_activate = rec_page.url

                    await rec_page.keyboard.press("Enter")
                    await rec_page.wait_for_timeout(ACTION_WAIT)
                    walkthrough_log.append({"action": "enter", "element": sel})

                    if rec_page.url != url_before_activate:
                        continue

                    new_expanded = await rec_page.evaluate("""() => {
                        const el = document.activeElement;
                        return el ? el.getAttribute('aria-expanded') : null;
                    }""")
                    overlay_after = await rec_page.evaluate(_OVERLAY_SNAPSHOT_JS)

                    opened_overlay = (
                        new_expanded == "true"
                        or bool(has_popup)
                        or overlay_after.get("open_count", 0) > overlay_before.get("open_count", 0)
                    )

                    if opened_overlay:
                        # Dispatch by overlay kind: dialogs/modals navigate
                        # with Tab (each focusable widget in the dialog is
                        # a Tab stop); menus/listboxes/trees/comboboxes
                        # navigate with ArrowDown/Up. Using the wrong keys
                        # silently misses every item after the first, so
                        # we branch on top_kind returned by the overlay
                        # snapshot.
                        overlay_kind = overlay_after.get("top_kind", "dropdown")
                        walkthrough_log.append({
                            "action": "overlay_classify",
                            "element": sel,
                            "kind": overlay_kind,
                        })

                    if opened_overlay and overlay_kind == "modal":
                        # ── Modal / dialog: Tab through every item ──────
                        # A well-built dialog traps focus inside itself;
                        # walking Tab until we see a selector repeat
                        # proves every focusable dialog widget was
                        # reachable. Also measures whether focus is
                        # trapped in the dialog (would be a SC 2.1.2
                        # violation if Escape fails later).
                        modal_seen: set[str] = set()
                        modal_prev: str | None = None
                        modal_same_count = 0
                        modal_left = False  # focus exited dialog via Tab
                        modal_step = 0
                        MODAL_MAX_TABS = 40
                        while modal_step < MODAL_MAX_TABS:
                            modal_step += 1
                            await rec_page.keyboard.press("Tab")
                            await rec_page.wait_for_timeout(ACTION_WAIT)
                            modal_state = await rec_page.evaluate(_FOCUS_IN_OVERLAY_JS)
                            walkthrough_log.append({
                                "action": "modal_tab",
                                "trigger": sel,
                                "step": modal_step,
                                "focused": modal_state.get("focused", ""),
                                "focus_in_overlay": modal_state.get("focus_in_overlay", False),
                            })
                            if not modal_state.get("focus_in_overlay"):
                                modal_left = True
                                break
                            cur_m = modal_state.get("focused", "") or ""
                            if cur_m == modal_prev:
                                modal_same_count += 1
                                if modal_same_count >= 2:
                                    # Focus pinned at same element 3 in a row
                                    break
                            else:
                                modal_same_count = 0
                            if cur_m in modal_seen:
                                # Cycled back to first item -- focus trap
                                # working correctly.
                                break
                            modal_seen.add(cur_m)
                            modal_prev = cur_m
                        walkthrough_log.append({
                            "action": "modal_tab_complete",
                            "element": sel,
                            "items_reached": len(modal_seen),
                            "focus_left_via_tab": modal_left,
                            "items": sorted(modal_seen),
                        })
                        # Alias to arrow_seen so the reverse-navigation
                        # pass below can compare item sets uniformly.
                        arrow_seen = modal_seen
                    elif opened_overlay:
                        # ── Dropdown / menu: ArrowDown until cycle ─────
                        # Press ArrowDown until focus stops changing. Covers
                        # the whole menu no matter how many items, and
                        # naturally terminates at the last item (no cycle)
                        # or when the menu wraps to an already-seen item.
                        arrow_seen: set[str] = set()
                        arrow_prev: str | None = None
                        arrow_same_count = 0
                        arrow_step = 0
                        while True:
                            arrow_step += 1
                            await rec_page.keyboard.press("ArrowDown")
                            await rec_page.wait_for_timeout(ACTION_WAIT)
                            menu_item_info = await rec_page.evaluate("""() => {
                                const el = document.activeElement;
                                if (!el || el.tagName.toLowerCase() === 'body') return null;
                                let s = el.tagName.toLowerCase();
                                if (el.id) s = '#' + el.id;
                                else if (el.className && typeof el.className === 'string')
                                    s = el.tagName.toLowerCase() + '.' + el.className.trim().split(/\\s+/)[0];
                                return {
                                    tag: el.tagName.toLowerCase(),
                                    role: el.getAttribute('role') || '',
                                    text: (el.textContent || '').trim(),
                                    selector: s,
                                };
                            }""")
                            walkthrough_log.append({
                                "action": "arrow_down",
                                "trigger": sel,
                                "focused_element": menu_item_info,
                            })
                            if not menu_item_info:
                                break
                            cur_sel = menu_item_info.get("selector", "")
                            if cur_sel == arrow_prev:
                                arrow_same_count += 1
                                if arrow_same_count >= 2:
                                    # Same selector three presses in a row =
                                    # we've hit the last item and focus is
                                    # pinned (no wrap).
                                    break
                            else:
                                arrow_same_count = 0
                            if cur_sel in arrow_seen:
                                # We've wrapped back to a previously focused
                                # item — full menu explored.
                                break
                            arrow_seen.add(cur_sel)
                            arrow_prev = cur_sel

                    if opened_overlay:
                        # Shared dismiss verification for BOTH overlay
                        # kinds. Modals used to skip this entirely, so a
                        # modal that ignored Escape never produced an
                        # escape_result entry and could never be reported
                        # as a trap (the walkthrough_traps synthesis keys
                        # off escape_result).
                        await rec_page.keyboard.press("Escape")
                        await rec_page.wait_for_timeout(ACTION_WAIT)
                        walkthrough_log.append({"action": "escape", "element": sel})

                        close_check = await rec_page.evaluate(_CLOSE_CHECK_JS, sel)
                        focus_returned = close_check.get("focus_returned_to") if close_check else None
                        focus_is_trigger = close_check.get("focus_is_trigger", False) if close_check else False
                        expanded_after = close_check.get("aria_expanded_after") if close_check else None
                        still_open = close_check.get("still_open", False) if close_check else False

                        exit_via_tab = False
                        tab_presses_used = 0
                        if still_open or expanded_after == "true":
                            # Unbounded retry: if Tab can exit the overlay
                            # we'll know in 1-3 presses on well-built sites
                            # and the loop terminates naturally via focus
                            # leaving the overlay. If focus never escapes
                            # and keeps pointing at overlay elements, we
                            # stop when the SAME element is focused two
                            # presses in a row (confirmed trap).
                            tab_out_prev = ""
                            tab_out_same = 0
                            while True:
                                tab_presses_used += 1
                                await rec_page.keyboard.press("Tab")
                                await rec_page.wait_for_timeout(ACTION_WAIT)
                                tab_state = await rec_page.evaluate(_FOCUS_IN_OVERLAY_JS)
                                if not tab_state.get("focus_in_overlay"):
                                    exit_via_tab = True
                                    break
                                cur_focus = tab_state.get("focused", "") or ""
                                if cur_focus and cur_focus == tab_out_prev:
                                    tab_out_same += 1
                                    if tab_out_same >= 2:
                                        # Focus stuck on same element three
                                        # presses running -- this overlay
                                        # traps keyboard Tab exit too.
                                        break
                                else:
                                    tab_out_same = 0
                                tab_out_prev = cur_focus
                            walkthrough_log.append({
                                "action": "tab_out_retry",
                                "element": sel,
                                "exit_via_tab": exit_via_tab,
                                "tab_presses": tab_presses_used,
                            })

                        walkthrough_log.append({
                            "action": "escape_result",
                            "element": sel,
                            "overlay_kind": overlay_kind,
                            "focus_after_escape": focus_returned,
                            # focus_is_trigger is the AUTHORITATIVE answer
                            # SC 2.4.3 needs -- did focus stay on the same
                            # DOM node as the trigger? Computed in JS via
                            # node identity comparison; not derivable from
                            # the tag-name alone.
                            "focus_is_trigger": focus_is_trigger,
                            "aria_expanded_after_escape": expanded_after,
                            "dropdown_still_open": still_open,
                            "open_selectors": close_check.get("open_selectors", []) if close_check else [],
                            "exit_via_tab": exit_via_tab,
                            "tab_presses_to_exit": tab_presses_used,
                        })

                        # ── Reverse-navigation verification ───────────
                        # SC 2.1.1 requires a keyboard-operable widget
                        # to work in BOTH directions. The ArrowDown
                        # pass above proved items 1..N are reachable
                        # from item 0 going forward. Now re-open the
                        # widget and press ArrowUp to prove the user
                        # can also walk items N..1 going backward --
                        # some custom widgets bind ArrowDown but forget
                        # ArrowUp, and a purely forward-only widget is
                        # still a 2.1.1 failure.
                        #
                        # Only run this when Escape successfully closed
                        # the widget AND we actually visited at least
                        # two distinct items going forward (one-item
                        # widgets have no reverse to test). A failing
                        # close is already flagged below as a trap --
                        # no value in re-opening to test reverse.
                        close_ok = not still_open and expanded_after != "true"
                        if close_ok and len(arrow_seen) >= 2:
                            await rec_page.keyboard.press("Enter")
                            await rec_page.wait_for_timeout(ACTION_WAIT)
                            walkthrough_log.append({
                                "action": "reenter",
                                "element": sel,
                                "purpose": "reverse navigation check",
                            })

                            reopen_state = await rec_page.evaluate(_OVERLAY_SNAPSHOT_JS)
                            reopened = reopen_state.get(
                                "open_count", 0
                            ) > overlay_before.get("open_count", 0)

                            up_seen: set[str] = set()
                            up_prev: str | None = None
                            up_same_count = 0
                            # Reverse key depends on overlay kind:
                            #   modal/dialog  -> Shift+Tab
                            #   dropdown/menu -> ArrowUp
                            # Using the wrong key is a no-op inside most
                            # widgets, so the reverse pass would silently
                            # return zero items and the bidirectional
                            # check would flag every modal as broken.
                            reverse_key = (
                                "Shift+Tab" if overlay_kind == "modal"
                                else "ArrowUp"
                            )
                            reverse_action = (
                                "modal_shift_tab" if overlay_kind == "modal"
                                else "arrow_up"
                            )
                            if reopened:
                                while True:
                                    await rec_page.keyboard.press(reverse_key)
                                    await rec_page.wait_for_timeout(ACTION_WAIT)
                                    up_info = await rec_page.evaluate("""() => {
                                        const el = document.activeElement;
                                        if (!el || el.tagName.toLowerCase() === 'body') return null;
                                        let s = el.tagName.toLowerCase();
                                        if (el.id) s = '#' + el.id;
                                        else if (el.className && typeof el.className === 'string')
                                            s = el.tagName.toLowerCase() + '.' + el.className.trim().split(/\\s+/)[0];
                                        return {
                                            tag: el.tagName.toLowerCase(),
                                            role: el.getAttribute('role') || '',
                                            text: (el.textContent || '').trim(),
                                            selector: s,
                                        };
                                    }""")
                                    walkthrough_log.append({
                                        "action": reverse_action,
                                        "trigger": sel,
                                        "focused_element": up_info,
                                    })
                                    if not up_info:
                                        break
                                    cur_up = up_info.get("selector", "")
                                    if cur_up == up_prev:
                                        up_same_count += 1
                                        if up_same_count >= 2:
                                            break
                                    else:
                                        up_same_count = 0
                                    if cur_up in up_seen:
                                        break
                                    up_seen.add(cur_up)
                                    up_prev = cur_up
                                    if len(up_seen) >= 40:
                                        # Same MODAL_MAX_TABS-equivalent cap
                                        # as the forward modal walk so a
                                        # runaway reverse doesn't spin
                                        # forever inside a badly-built
                                        # dialog.
                                        break

                                await rec_page.keyboard.press("Escape")
                                await rec_page.wait_for_timeout(ACTION_WAIT)
                                walkthrough_log.append({
                                    "action": "escape_after_reverse",
                                    "element": sel,
                                })

                            # Bidirectional health = reverse reopened AND
                            # reached at least as many distinct items as
                            # forward (every forward-reachable item was
                            # also reverse-reachable). Subset check
                            # tolerates widget ordering differences.
                            bidirectional_ok = (
                                reopened
                                and len(up_seen) >= 1
                                and arrow_seen.issubset(up_seen | {sel})
                            )
                            walkthrough_log.append({
                                "action": "bidirectional_check",
                                "element": sel,
                                "overlay_kind": overlay_kind,
                                "forward_key": (
                                    "Tab" if overlay_kind == "modal" else "ArrowDown"
                                ),
                                "reverse_key": reverse_key,
                                "reopened_on_reenter": reopened,
                                "down_items_count": len(arrow_seen),
                                "up_items_count": len(up_seen),
                                "down_items": sorted(arrow_seen),
                                "up_items": sorted(up_seen),
                                "bidirectional_ok": bidirectional_ok,
                            })
                            if not bidirectional_ok:
                                logger.warning(
                                    "Widget '%s' reverse navigation incomplete: "
                                    "ArrowDown reached %d items, ArrowUp reached "
                                    "%d items (reopened=%s). May be SC 2.1.1 failure "
                                    "-- widget does not support bidirectional "
                                    "keyboard navigation.",
                                    sel, len(arrow_seen), len(up_seen), reopened,
                                )

                        if (still_open or expanded_after == "true") and not exit_via_tab:
                            logger.warning(
                                "Keyboard TRAP: '%s' (%s) did not close after Escape "
                                "AND focus could not exit via Tab "
                                "(aria-expanded=%s, still_open=%s)",
                                sel, overlay_kind, expanded_after, still_open,
                            )
                            # The overlay is still open AND focus cannot
                            # leave it. Without recovery, the outer Tab
                            # loop would cycle inside the overlay until
                            # the 20-duplicate detector terminated the
                            # WHOLE walkthrough — the trap is already
                            # recorded via escape_result above, so now
                            # recover and keep walking. Escape already
                            # failed: try clicking outside the overlay
                            # (mirrors the reset_state pattern in
                            # _capture_keyboard_roundtrip), then reload
                            # as the last resort (mirrors the _safe_goto
                            # recovery used by the media walkthroughs).
                            recovery_method = "click_outside"
                            recovered = False
                            try:
                                await rec_page.mouse.click(2, 2)
                                await rec_page.wait_for_timeout(ACTION_WAIT)
                                after_click = await rec_page.evaluate(_OVERLAY_SNAPSHOT_JS)
                                recovered = (
                                    after_click.get("open_count", 0)
                                    <= overlay_before.get("open_count", 0)
                                )
                            except Exception:
                                logger.warning(
                                    "Walkthrough trap recovery: click outside "
                                    "'%s' failed", sel, exc_info=True,
                                )
                            if not recovered:
                                recovery_method = "reload"
                                try:
                                    await _safe_goto(rec_page, current_url, timeout=60000)
                                    await rec_page.wait_for_timeout(2000)
                                    await rec_page.evaluate("document.body.focus()")
                                    recovered = True
                                except Exception:
                                    logger.warning(
                                        "Walkthrough trap recovery: reload after "
                                        "trap on '%s' failed", sel, exc_info=True,
                                    )
                            walkthrough_log.append({
                                "action": "trap_recovery",
                                "element": sel,
                                "overlay_kind": overlay_kind,
                                "method": recovery_method,
                                "recovered": recovered,
                            })
                            if recovered:
                                # Let the outer loop re-traverse the
                                # already-seen tab stops (focus restarts
                                # at the top after click-outside/reload)
                                # without tripping the cycle detector.
                                walk_consec_dup = 0
                                walk_dup_grace = len(seen_selectors) + 10
                        elif (still_open or expanded_after == "true") and exit_via_tab:
                            logger.info(
                                "Non-standard exit: '%s' ignored Escape but focus "
                                "escaped via Tab after %d press(es) — passes 2.1.2 "
                                "but may fail user expectations",
                                sel, tab_presses_used,
                            )

                elif tag == "select":
                    await rec_page.keyboard.press("Space")
                    await rec_page.wait_for_timeout(ACTION_WAIT)
                    for _ in range(3):
                        await rec_page.keyboard.press("ArrowDown")
                        await rec_page.wait_for_timeout(ACTION_WAIT)
                    await rec_page.keyboard.press("Escape")
                    await rec_page.wait_for_timeout(ACTION_WAIT)
                    walkthrough_log.append({"action": "select_navigate", "element": sel})

            # ── Phase 2: Reverse tab ────────────────────────────────
            await rec_page.wait_for_timeout(1000)  # Clear pause before reversing
            walkthrough_log.append({"action": "reverse_start"})

            # Unbounded reverse walk. Terminates when focus reaches body
            # (top of page reached) or we've cycled back through all
            # already-seen selectors (trap or closed loop).
            rev_seen: set[str] = set()
            rev_consec_dup = 0
            rev_consec_body = 0
            while True:
                await rec_page.keyboard.press("Shift+Tab")
                await rec_page.wait_for_timeout(ACTION_WAIT)

                info = await rec_page.evaluate("""() => {
                    const el = document.activeElement;
                    if (!el || el.tagName.toLowerCase() === 'body') return null;
                    let s = el.tagName.toLowerCase();
                    if (el.id) s = '#' + el.id;
                    return {tag: el.tagName.toLowerCase(), selector: s};
                }""")
                # Same rule as the forward walk: a transient body-hit
                # after widget activation (Enter → ArrowDown → Escape)
                # sometimes leaves focus briefly on body, and Shift+Tab
                # from body lands at the last element. Tolerate several
                # body-hits before calling the walk complete. Without
                # this, the reverse phase terminates the first time
                # focus momentarily lands on body.
                if not info or info.get("tag") == "body":
                    rev_consec_body += 1
                    walkthrough_log.append({
                        "action": "shift_tab_landed_body",
                        "consec_body": rev_consec_body,
                    })
                    if rev_consec_body >= 4:
                        break
                    continue
                rev_consec_body = 0
                rev_sel = info["selector"]
                walkthrough_log.append({"action": "shift_tab", "element": rev_sel})
                if rev_sel in rev_seen:
                    rev_consec_dup += 1
                    if rev_consec_dup >= 20:
                        walkthrough_log.append({
                            "action": "reverse_cycle_complete",
                            "distinct_selectors_visited": len(rev_seen),
                        })
                        break
                    continue
                rev_consec_dup = 0
                rev_seen.add(rev_sel)

            # ── Phase 3: Final pause so AI can see end state ────────
            await rec_page.wait_for_timeout(1000)

            # Get video handle before closing
            video_file = rec_page.video

            # Close page, then context (finalizes video), then browser
            await rec_page.close()
            await context.close()
            await browser.close()

            # Now the video file is released — rename it
            if video_file:
                saved = await video_file.path()
                if saved and os.path.exists(str(saved)):
                    final_path = os.path.join(
                        video_dir, "keyboard_walkthrough.webm"
                    )
                    try:
                        os.rename(str(saved), final_path)
                    except OSError:
                        # Fallback: copy instead of rename
                        import shutil
                        shutil.copy2(str(saved), final_path)
                    capture_data.keyboard_walkthrough_video = final_path
                    logger.info(
                        "Keyboard walkthrough video recorded: %s (%d actions)",
                        final_path, len(walkthrough_log),
                    )

        capture_data.keyboard_walkthrough_log = walkthrough_log

        # Extract structured expanded-state tab data from the walkthrough log.
        # This lets 2.1.1 and 2.4.3 checks know which elements are reachable
        # inside opened menus/dropdowns without duplicating the work.
        expanded_walks: dict[str, list[dict]] = {}
        current_trigger: str | None = None
        for entry in walkthrough_log:
            action = entry.get("action", "")
            if action == "enter":
                current_trigger = entry.get("element")
                if current_trigger and current_trigger not in expanded_walks:
                    expanded_walks[current_trigger] = []
            elif action == "arrow_down" and current_trigger:
                focused = entry.get("focused_element")
                if focused and focused.get("selector"):
                    # Avoid duplicates
                    existing_sels = {e.get("selector") for e in expanded_walks[current_trigger]}
                    if focused["selector"] not in existing_sels:
                        expanded_walks[current_trigger].append(focused)
            elif action in ("escape", "escape_result"):
                current_trigger = None

        capture_data.expanded_tab_walks = expanded_walks
        if expanded_walks:
            total_items = sum(len(v) for v in expanded_walks.values())
            logger.info(
                "Expanded widget tab data: %d widgets, %d items reachable",
                len(expanded_walks), total_items,
            )

        # Synthesize keyboard_traps entries from walkthrough escape_result
        # records where the overlay would not close on Escape. If Tab also
        # couldn't move focus out → WCAG 2.1.2 failure. If Tab did escape →
        # passes 2.1.2 but user has no discoverable exit instructions, which
        # the existing check flags as MEDIUM via the exit_instructions path.
        walkthrough_traps: list[dict] = []
        for entry in walkthrough_log:
            if entry.get("action") != "escape_result":
                continue
            still_open = entry.get("dropdown_still_open")
            expanded_after = entry.get("aria_expanded_after_escape")
            exit_via_tab = entry.get("exit_via_tab", False)
            if not (still_open or expanded_after == "true"):
                continue
            sel_trap = entry.get("element") or "element"
            overlay_kind = entry.get("overlay_kind") or "dropdown"
            kind_word = "Modal" if overlay_kind == "modal" else "Dropdown"
            if exit_via_tab:
                walkthrough_traps.append({
                    "type": "non_standard_exit",
                    "selector": sel_trap,
                    "overlay_kind": overlay_kind,
                    "can_exit": True,
                    "exit_instructions": "",
                    "description": (
                        f"{kind_word} '{sel_trap}' did not close on Escape; "
                        f"focus escaped via Tab after "
                        f"{entry.get('tab_presses_to_exit', 0)} press(es). "
                        f"Passes 2.1.2 but the non-standard exit path is "
                        f"not advertised to the user."
                    ),
                })
            else:
                walkthrough_traps.append({
                    "type": (
                        "modal_escape_failed" if overlay_kind == "modal"
                        else "dropdown_escape_failed"
                    ),
                    "selector": sel_trap,
                    "overlay_kind": overlay_kind,
                    "can_exit": False,
                    "description": (
                        f"{kind_word} '{sel_trap}' did not close after Escape "
                        f"AND focus could not exit via Tab. "
                        f"Keyboard-only users are stuck inside this widget."
                    ),
                })
        if walkthrough_traps:
            capture_data.keyboard_traps.extend(walkthrough_traps)
            logger.info(
                "Walkthrough synthesized %d keyboard trap(s) from escape/Tab failures",
                len(walkthrough_traps),
            )

    except Exception:
        logger.exception("Recorded keyboard walkthrough failed")


# ─── Focus indicator screenshots ─────────────────────────────────────────────

async def _focus_indicator_screenshots(
    page: Page,
    capture_data: CaptureData,
    captures_dir: str,
) -> None:
    """For each unique focusable element, screenshot unfocused and focused states.

    Outline-based focus indicators are the most common pattern (e.g.
    `:focus { outline: 3px solid blue; outline-offset: 1px; }`), and the
    outline + offset paint OUTSIDE the element's bounding rect. The
    previous implementation used `el_handle.screenshot()` which clips
    to the element's bounding box -- so the outline was cropped out of
    both screenshots, producing byte-identical pairs. The downstream
    visual AI was then asked "is the focus visible?" looking at two
    copies of the same image and would hallucinate "yes" most of the
    time (observed: 34/38 pairs identical on a large public university site, 26 of which had a
    real outline that was cropped, 8 were genuine SC 2.4.7 failures).

    Fix:
    - Use `page.screenshot(clip=padded_bbox)` so the outline + offset
      + box-shadow are captured. Padding of FOCUS_PAD_PX is enough to
      cover any reasonable focus ring (browser defaults max out around
      3-4px, page authors rarely exceed 8-10px).
    - Increase the wait between blur/focus and screenshot to 500ms
      (was 300ms) -- some CSS focus transitions run 300-400ms.
    - Hash both screenshots after capture. If byte-identical, no
      visible focus state change exists in the captured pixels, which
      is the authoritative pixel-level signal for SC 2.4.7. Set
      `has_visible_indicator` from this comparison so Check_2_4_7 has
      ground truth, not a CSS-property guess that misses
      color-matching / contrast-too-low cases.
    """
    FOCUS_PAD_PX = 16  # Padding around bbox to capture outline + offset
    FOCUS_WAIT_MS = 500  # Time for CSS transitions / paint to settle

    try:
        focus_dir = os.path.join(captures_dir, "focus_indicators")
        os.makedirs(focus_dir, exist_ok=True)

        # Gather unique selectors from the tab walk
        seen: set[str] = set()
        unique_elements: list[dict] = []
        for item in capture_data.tab_walk:
            sel = item.get("selector", "")
            if sel and sel not in seen:
                seen.add(sel)
                unique_elements.append(item)

        viewport = page.viewport_size or {"width": 1280, "height": 720}
        v_w = viewport["width"]
        v_h = viewport["height"]

        results: list[dict] = []
        identical_count = 0
        for idx, elem in enumerate(unique_elements):
            sel = elem["selector"]
            try:
                el_handle = await page.query_selector(sel)
                if not el_handle:
                    continue

                # Scroll into view so bounding_box returns viewport
                # coordinates that are actually on screen. Without this,
                # offset elements can produce clip rects that fall outside
                # the viewport and Playwright's screenshot() rejects them.
                try:
                    await el_handle.scroll_into_view_if_needed(timeout=2000)
                    await page.wait_for_timeout(150)
                except Exception:
                    pass  # element might be invisible / 0-dim; bbox check handles below

                bbox = await el_handle.bounding_box()
                if not bbox or bbox["width"] <= 0 or bbox["height"] <= 0:
                    # element has no rendered area -- focus indicator is
                    # not perceivable regardless of styles
                    continue

                # Pad and clamp clip to viewport. A focus indicator can
                # never extend more than the padding off the element box,
                # so 16px is a comfortable margin.
                clip_x = max(0.0, bbox["x"] - FOCUS_PAD_PX)
                clip_y = max(0.0, bbox["y"] - FOCUS_PAD_PX)
                clip_w = min(v_w - clip_x, bbox["width"] + 2 * FOCUS_PAD_PX)
                clip_h = min(v_h - clip_y, bbox["height"] + 2 * FOCUS_PAD_PX)
                if clip_w <= 0 or clip_h <= 0:
                    continue
                clip = {"x": clip_x, "y": clip_y, "width": clip_w, "height": clip_h}

                # Blur first, screenshot the unfocused state
                await page.evaluate("(el) => el.blur()", el_handle)
                await page.wait_for_timeout(FOCUS_WAIT_MS)
                unfocused_path = os.path.join(focus_dir, f"unfocused_{idx}.png")
                await page.screenshot(path=unfocused_path, clip=clip)

                # Focus and screenshot the focused state
                await page.evaluate("(el) => el.focus()", el_handle)
                await page.wait_for_timeout(FOCUS_WAIT_MS)
                focused_path = os.path.join(focus_dir, f"focused_{idx}.png")
                await page.screenshot(path=focused_path, clip=clip)

                # Pixel-level ground truth for SC 2.4.7: when the bytes
                # match between blur and focus snapshots, there's no
                # rendered visual change, so no perceivable focus
                # indicator -- regardless of what the computed styles
                # claim. This catches the common color-matching anti-
                # pattern (`outline: 3px solid white` on a white
                # background) and the contrast-too-low edge cases.
                with open(unfocused_path, "rb") as f1, open(focused_path, "rb") as f2:
                    unfocused_hash = hashlib.md5(f1.read()).hexdigest()
                    focused_hash = hashlib.md5(f2.read()).hexdigest()
                pixels_changed = unfocused_hash != focused_hash
                if not pixels_changed:
                    identical_count += 1

                # Record outline / box-shadow / border style metadata too.
                # The CSS check is no longer authoritative for visibility
                # (the pixel diff above is), but the style fields stay
                # useful for explaining WHY a focus indicator is missing.
                styles = await page.evaluate("""(el) => {
                    const cs = window.getComputedStyle(el);
                    return {
                        outline: cs.outline || '',
                        outlineColor: cs.outlineColor || '',
                        outlineWidth: cs.outlineWidth || '',
                        outlineStyle: cs.outlineStyle || '',
                        outlineOffset: cs.outlineOffset || '',
                        boxShadow: cs.boxShadow || '',
                    };
                }""", el_handle)

                results.append({
                    "selector": sel,
                    "tag": elem.get("tag", ""),
                    "unfocused_screenshot": unfocused_path,
                    "focused_screenshot": focused_path,
                    "outline": styles.get("outline", ""),
                    "outline_color": styles.get("outlineColor", ""),
                    "outline_width": styles.get("outlineWidth", ""),
                    "outline_style": styles.get("outlineStyle", ""),
                    "outline_offset": styles.get("outlineOffset", ""),
                    "box_shadow": styles.get("boxShadow", ""),
                    "has_visible_indicator": pixels_changed,
                    "screenshots_byte_identical": not pixels_changed,
                })
            except Exception:
                logger.debug("Focus indicator screenshot failed for %s", sel)

        capture_data.focus_indicators = results
        if results:
            logger.info(
                "Focus indicators: %d elements captured, %d had no visible "
                "state change (byte-identical screenshots = no perceivable "
                "focus indicator).",
                len(results), identical_count,
            )
    except Exception:
        logger.exception("Focus indicator capture failed")


# ─── Hover content detection ─────────────────────────────────────────────────

async def _hover_content_detection(
    page: Page,
    capture_data: CaptureData,
    captures_dir: str,
) -> None:
    """Hover over links and buttons to detect tooltips, popovers, dropdowns."""
    try:
        hover_dir = os.path.join(captures_dir, "hover_content")
        os.makedirs(hover_dir, exist_ok=True)

        from functions.js_helpers import GET_SELECTOR_JS as _SEL
        targets = await page.evaluate(r"""() => {""" + _SEL + r"""
            const els = document.querySelectorAll('a, button, [role="button"], [role="link"]');
            return Array.from(els).map((el, i) => {
                return {
                    selector: getSelector(el),
                    text: (el.textContent || '').trim(),
                    index: i,
                    title: el.getAttribute('title') || '',
                    // unique path for re-locating siblings/nearby elements
                    _path: (() => {
                        const parts = [];
                        let node = el;
                        while (node && node !== document.body) {
                            let s = node.tagName.toLowerCase();
                            if (node.id) { parts.unshift('#' + node.id); break; }
                            const idx = Array.from(node.parentNode?.children || []).indexOf(node);
                            parts.unshift(s + ':nth-child(' + (idx + 1) + ')');
                            node = node.parentNode;
                        }
                        return 'body > ' + parts.join(' > ');
                    })(),
                };
            });
        }""")

        results: list[dict] = []
        for target in targets:
            try:
                # Wrap entire per-element interaction in a 5s timeout
                # to prevent hangs from navigation or slow JS
                async def _check_hover(t=target):
                    el_handle = await page.query_selector(t["selector"])
                    if not el_handle:
                        return None

                    # Skip elements that are hidden (inside closed menus,
                    # collapsed accordions, off-screen nav, etc.)
                    is_visible = await el_handle.is_visible()
                    if not is_visible:
                        return None

                    # Scroll element into view before hovering
                    try:
                        await el_handle.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        return None
                    await page.wait_for_timeout(200)

                    # Capture pre-hover computed styles & bounding rects for
                    # siblings and nearby elements so we can detect CSS-driven
                    # visibility changes after hovering.
                    before_styles = await page.evaluate("""(parentPath) => {
                        const PROPS = ['opacity','visibility','display','transform','height','maxHeight'];
                        const parent = document.querySelector(parentPath);
                        if (!parent) return [];
                        // Collect siblings + their first-level children
                        const candidates = [];
                        for (const child of parent.children) {
                            candidates.push(child);
                            for (const gc of child.children) candidates.push(gc);
                        }
                        // Also check next/prev siblings of the parent itself
                        if (parent.nextElementSibling) {
                            candidates.push(parent.nextElementSibling);
                            for (const c of parent.nextElementSibling.children) candidates.push(c);
                        }
                        if (parent.previousElementSibling) {
                            candidates.push(parent.previousElementSibling);
                            for (const c of parent.previousElementSibling.children) candidates.push(c);
                        }
                        return candidates.map((el, idx) => {
                            const cs = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            const styles = {};
                            for (const p of PROPS) styles[p] = cs.getPropertyValue(p);
                            return {
                                idx,
                                tag: el.tagName.toLowerCase(),
                                styles,
                                rect: { width: rect.width, height: rect.height },
                            };
                        });
                    }""", t.get("_path", "body"))

                    # Take "before" screenshot for WCAG 1.4.13 comparison
                    normal_path = os.path.join(hover_dir, f"normal_{t['index']}.png")
                    await page.screenshot(path=normal_path, full_page=False)

                    before_count = await page.evaluate("document.querySelectorAll('*').length")
                    await el_handle.hover(force=True)
                    await page.wait_for_timeout(2000)  # Let tooltips/dropdowns appear
                    return el_handle, before_count, before_styles, normal_path

                try:
                    result_or_none = await asyncio.wait_for(_check_hover(), timeout=8.0)
                except asyncio.TimeoutError:
                    logger.debug("Hover timed out for %s", target.get("selector"))
                    continue
                if result_or_none is None:
                    continue
                el_handle, before_count, before_styles, normal_path = result_or_none

                # Check for new elements (tooltips / popovers / dropdowns)
                after_count = await page.evaluate("document.querySelectorAll('*').length")
                new_elements = after_count - before_count

                # Check for tooltip / popover / dropdown visibility
                hover_info = await page.evaluate("""() => {
                    const tooltips = document.querySelectorAll(
                        '[role="tooltip"], [class*="tooltip"], [class*="popover"], ' +
                        '[class*="dropdown"][class*="show"], [class*="dropdown"][class*="open"]'
                    );
                    const visible = [];
                    for (const t of tooltips) {
                        const cs = window.getComputedStyle(t);
                        if (cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0') {
                            visible.push({
                                tag: t.tagName.toLowerCase(),
                                role: t.getAttribute('role') || '',
                                text: (t.textContent || '').trim(),
                                className: t.className || '',
                            });
                        }
                    }
                    return visible;
                }""")

                # Detect CSS-driven visibility changes (opacity, display,
                # transform, etc.) and bounding-rect dimension changes by
                # comparing post-hover styles against the pre-hover snapshot.
                css_changes: list[dict] = []
                if before_styles:
                    after_styles = await page.evaluate("""(parentPath) => {
                        const PROPS = ['opacity','visibility','display','transform','height','maxHeight'];
                        const parent = document.querySelector(parentPath);
                        if (!parent) return [];
                        const candidates = [];
                        for (const child of parent.children) {
                            candidates.push(child);
                            for (const gc of child.children) candidates.push(gc);
                        }
                        if (parent.nextElementSibling) {
                            candidates.push(parent.nextElementSibling);
                            for (const c of parent.nextElementSibling.children) candidates.push(c);
                        }
                        if (parent.previousElementSibling) {
                            candidates.push(parent.previousElementSibling);
                            for (const c of parent.previousElementSibling.children) candidates.push(c);
                        }
                        return candidates.map((el, idx) => {
                            const cs = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            const styles = {};
                            for (const p of PROPS) styles[p] = cs.getPropertyValue(p);
                            return {
                                idx,
                                tag: el.tagName.toLowerCase(),
                                styles,
                                rect: { width: rect.width, height: rect.height },
                            };
                        });
                    }""", target.get("_path", "body"))

                    after_map = {s["idx"]: s for s in (after_styles or [])}
                    for bs in before_styles:
                        a = after_map.get(bs["idx"])
                        if not a:
                            continue
                        changed_props: list[str] = []
                        for prop in bs["styles"]:
                            if bs["styles"][prop] != a["styles"].get(prop, bs["styles"][prop]):
                                changed_props.append(
                                    f"{prop}: {bs['styles'][prop]} -> {a['styles'][prop]}"
                                )
                        # Detect significant dimension changes (> 5px)
                        dw = abs(a["rect"]["width"] - bs["rect"]["width"])
                        dh = abs(a["rect"]["height"] - bs["rect"]["height"])
                        if dw > 5:
                            changed_props.append(
                                f"rect.width: {bs['rect']['width']:.1f} -> {a['rect']['width']:.1f}"
                            )
                        if dh > 5:
                            changed_props.append(
                                f"rect.height: {bs['rect']['height']:.1f} -> {a['rect']['height']:.1f}"
                            )
                        if changed_props:
                            css_changes.append({
                                "element": f"{a['tag']}[{a['idx']}]",
                                "changes": changed_props,
                            })

                if new_elements > 0 or hover_info or target["title"] or css_changes:
                    screenshot_path = os.path.join(hover_dir, f"hover_{target['index']}.png")
                    await page.screenshot(path=screenshot_path, full_page=False)

                    # --- WCAG 1.4.13 requirement tests ---

                    # Persistent: wait 2s without moving, check content still present
                    await page.wait_for_timeout(2000)
                    persistent_count = await page.evaluate("document.querySelectorAll('*').length")
                    is_persistent = (persistent_count - before_count) >= new_elements

                    # Hoverable: if hover content appeared, try moving mouse to it
                    is_hoverable = True
                    if hover_info:
                        try:
                            tooltip_el = await page.query_selector(
                                '[role="tooltip"], [class*="tooltip"], [class*="popover"], '
                                '[class*="dropdown"][class*="show"], [class*="dropdown"][class*="open"]'
                            )
                            if tooltip_el:
                                await tooltip_el.hover(force=True)
                                await page.wait_for_timeout(500)
                                still_visible = await tooltip_el.is_visible()
                                is_hoverable = still_visible
                        except Exception:
                            pass  # default to True if we can't test

                    # Dismissible: press Escape and check if content disappears
                    is_dismissible = False
                    try:
                        await page.keyboard.press("Escape")
                        await page.wait_for_timeout(500)
                        dismiss_count = await page.evaluate("document.querySelectorAll('*').length")
                        is_dismissible = dismiss_count <= before_count
                        if not is_dismissible and css_changes:
                            # Check if CSS changes reverted
                            dismiss_styles = await page.evaluate("""(parentPath) => {
                                const PROPS = ['opacity','visibility','display','transform','height','maxHeight'];
                                const parent = document.querySelector(parentPath);
                                if (!parent) return [];
                                const candidates = [];
                                for (const child of parent.children) {
                                    candidates.push(child);
                                    for (const gc of child.children) candidates.push(gc);
                                }
                                if (parent.nextElementSibling) {
                                    candidates.push(parent.nextElementSibling);
                                    for (const c of parent.nextElementSibling.children) candidates.push(c);
                                }
                                if (parent.previousElementSibling) {
                                    candidates.push(parent.previousElementSibling);
                                    for (const c of parent.previousElementSibling.children) candidates.push(c);
                                }
                                return candidates.map((el, idx) => {
                                    const cs = window.getComputedStyle(el);
                                    const styles = {};
                                    for (const p of PROPS) styles[p] = cs.getPropertyValue(p);
                                    return { idx, styles };
                                });
                            }""", target.get("_path", "body"))
                            dismiss_map = {s["idx"]: s for s in (dismiss_styles or [])}
                            reverted = 0
                            for bs in before_styles:
                                ds = dismiss_map.get(bs["idx"])
                                if ds and ds["styles"] == bs["styles"]:
                                    reverted += 1
                            if reverted >= len(before_styles):
                                is_dismissible = True
                    except Exception:
                        pass  # default to False

                    results.append({
                        "selector": target["selector"],
                        "text": target["text"],
                        "title_attr": target["title"],
                        "new_elements_count": new_elements,
                        "hover_content": hover_info,
                        "css_changes": css_changes,
                        "screenshot_path": screenshot_path,
                        "normal_path": normal_path,
                        "hover_path": screenshot_path,
                        "trigger": "hover",
                        "dismissible": is_dismissible,
                        "hoverable": is_hoverable,
                        "persistent": is_persistent,
                    })

                # Move mouse away
                await page.mouse.move(0, 0)
                await page.wait_for_timeout(200)
            except Exception:
                logger.debug("Hover check failed for %s", target.get("selector"))

        if not hasattr(capture_data, 'hover_content') or not capture_data.hover_content:
            capture_data.hover_content = results
        else:
            capture_data.hover_content.extend(results)
    except Exception:
        logger.exception("Hover content detection failed")


# ─── Text spacing overflow ───────────────────────────────────────────────────

async def _text_spacing_overflow(
    page: Page,
    capture_data: CaptureData,
    captures_dir: str = "",
) -> None:
    """Inject WCAG 1.4.12 text spacing CSS and detect overflow."""
    try:
        # Inject text spacing CSS
        await page.evaluate("""(css) => {
            const style = document.createElement('style');
            style.id = '__wcag_text_spacing__';
            style.textContent = css;
            document.head.appendChild(style);
        }""", TEXT_SPACING_CSS)
        await page.wait_for_timeout(500)

        # Screenshot the page with text spacing overrides applied
        if captures_dir:
            try:
                ts_path = os.path.join(captures_dir, "text_spacing_override.png")
                await page.screenshot(path=ts_path, full_page=True)
                capture_data.text_spacing_screenshot = ts_path
            except Exception:
                logger.warning("Failed to capture text spacing screenshot")

        from functions.js_helpers import GET_SELECTOR_JS as _SEL
        overflow_elements = await page.evaluate(r"""() => {""" + _SEL + r"""
            const results = [];
            // Detect "intentional carousel / paginated scroller" containers.
            // These have an enormous scrollWidth (multiple viewport widths)
            // by DESIGN — content is paginated by JS and the user swipes
            // through. WCAG 1.4.12 only fails when text-spacing causes
            // "loss of content or functionality"; carousels still scroll
            // and still display the same content with text spacing applied,
            // so flagging them produces FALSE POSITIVES (observed on a
            // university site's glide.js main-slider sW=3156 cW=526 and the campus
            // carousel sW=9808 cW=1280).
            // We climb up looking for ARIA carousel signals or the
            // glide/swiper/slick class names; if any ancestor matches,
            // the inner overflow is intended and we skip the entry.
            const isInsideCarousel = (el) => {
                let cur = el;
                let depth = 0;
                while (cur && cur !== document.body && depth < 8) {
                    if (cur.getAttribute &&
                        cur.getAttribute('aria-roledescription') === 'carousel') {
                        return true;
                    }
                    const cls = (cur.className && typeof cur.className === 'string')
                        ? cur.className : '';
                    if (/\bglide(?:--|__|\b)|\bswiper(?:-|\b)|\bslick(?:-|\b)|\bcarousel\b/.test(cls)) {
                        return true;
                    }
                    cur = cur.parentElement;
                    depth++;
                }
                return false;
            };

            const elements = document.querySelectorAll('body *');
            for (const el of elements) {
                if (el.scrollHeight > el.clientHeight + 1 ||
                    el.scrollWidth > el.clientWidth + 1) {
                    const cs = window.getComputedStyle(el);
                    const overflow = cs.overflow;
                    // Only flag if content is clipped
                    if (overflow === 'hidden' || cs.overflowX === 'hidden' ||
                        cs.overflowY === 'hidden' || cs.textOverflow === 'ellipsis') {

                        // Skip sr-only / visually-hidden elements — their 1x1px
                        // clip box makes overflow detection meaningless since no
                        // rendered text flow exists to be affected by spacing.
                        const rect = el.getBoundingClientRect();
                        if (rect.width <= 2 && rect.height <= 2) continue;
                        if (cs.clip && cs.clip !== 'auto' && cs.clip.includes('rect')) continue;
                        const clipPath = cs.clipPath || cs.webkitClipPath || '';
                        if (clipPath.includes('inset(50%)') || clipPath.includes('rect(')) continue;
                        if (cs.position === 'absolute' &&
                            (parseInt(cs.left) < -9000 || parseInt(cs.top) < -9000)) continue;

                        // Skip carousel-internal overflow (intended pagination).
                        if (isInsideCarousel(el)) continue;

                        results.push({
                            selector: getSelector(el),
                            tag: el.tagName.toLowerCase(),
                            text: (el.textContent || '').trim(),
                            scrollWidth: el.scrollWidth,
                            clientWidth: el.clientWidth,
                            scrollHeight: el.scrollHeight,
                            clientHeight: el.clientHeight,
                            overflow: overflow,
                        });
                    }
                }
            }
            return results;
        }""")

        capture_data.text_spacing_overflow = overflow_elements

        # Remove injected CSS
        await page.evaluate("""() => {
            const style = document.getElementById('__wcag_text_spacing__');
            if (style) style.remove();
        }""")
    except Exception:
        logger.exception("Text spacing overflow detection failed")
        # Try to clean up
        try:
            await page.evaluate("""() => {
                const style = document.getElementById('__wcag_text_spacing__');
                if (style) style.remove();
            }""")
        except Exception:
            pass  # cleanup — best-effort removal of injected style, page state may be unrecoverable


# ─── Media playback ──────────────────────────────────────────────────────────

async def _media_playback(
    page: Page,
    capture_data: CaptureData,
) -> None:
    """Mute media, play for 2s, check text tracks, attempt to enable captions.

    Also extracts media source URLs, caption/subtitle track URLs, and
    searches the full page for transcript/caption buttons or links.
    """
    try:
        media_results = await page.evaluate("""async () => {
            const results = [];
            const mediaEls = document.querySelectorAll('video, audio');
            for (const el of mediaEls) {
                const info = {
                    tag: el.tagName.toLowerCase(),
                    src: el.currentSrc || el.src || '',
                    media_url: '',
                    hasTextTracks: el.textTracks ? el.textTracks.length > 0 : false,
                    textTrackCount: el.textTracks ? el.textTracks.length : 0,
                    textTrackKinds: [],
                    caption_urls: [],
                    captionsEnabled: false,
                    playSucceeded: false,
                };

                // Resolve media URL from src attribute or <source> children
                if (el.currentSrc) {
                    info.media_url = el.currentSrc;
                } else if (el.src) {
                    info.media_url = el.src;
                } else {
                    const sourceEl = el.querySelector('source');
                    if (sourceEl && sourceEl.src) {
                        info.media_url = sourceEl.src;
                    }
                }

                // Enumerate text tracks and collect caption/subtitle URLs
                if (el.textTracks) {
                    for (let i = 0; i < el.textTracks.length; i++) {
                        info.textTrackKinds.push({
                            kind: el.textTracks[i].kind,
                            label: el.textTracks[i].label,
                            language: el.textTracks[i].language,
                            mode: el.textTracks[i].mode,
                        });
                    }
                }

                // Collect caption/subtitle track URLs from <track> elements
                const trackEls = el.querySelectorAll('track');
                for (const track of trackEls) {
                    const kind = (track.kind || '').toLowerCase();
                    if (kind === 'captions' || kind === 'subtitles') {
                        const trackSrc = track.src || '';
                        if (trackSrc) {
                            info.caption_urls.push(trackSrc);
                        }
                    }
                }

                // Mute and attempt playback for 2 seconds. Race el.play()
                // against a 25s timer — a misbehaving custom player that
                // never resolves play() would otherwise hang the whole
                // evaluate() call (seen on a university homepage hero video).
                el.muted = true;
                el.volume = 0;
                try {
                    await Promise.race([
                        el.play(),
                        new Promise((_, rej) => setTimeout(
                            () => rej(new Error('play timeout 25s')), 25000)),
                    ]);
                    info.playSucceeded = true;
                    await new Promise(r => setTimeout(r, 2000));
                    try { el.pause(); } catch (pe) {}
                } catch (e) {
                    info.playError = e.message;
                }

                // Try enabling captions
                if (el.textTracks) {
                    for (let i = 0; i < el.textTracks.length; i++) {
                        const tt = el.textTracks[i];
                        if (tt.kind === 'captions' || tt.kind === 'subtitles') {
                            try {
                                tt.mode = 'showing';
                                info.captionsEnabled = true;
                            } catch (e) {}
                        }
                    }
                }

                results.push(info);
            }
            return results;
        }""")

        # Merge into existing media data
        for i, result in enumerate(media_results):
            if i < len(capture_data.media):
                capture_data.media[i].update(result)
            else:
                capture_data.media.append(result)

        # Search the entire page for transcript / caption buttons and links.
        # Word-boundary regex required: the previous substring match on
        # 'cc' fired on every "Accept" / "access" / "ACcessibility" link
        # (Cassie cookie-consent buttons matched 6×6 on a university site). 'cc' is the
        # canonical "closed caption" abbreviation, but only as a whole
        # token. Other keywords stay as substring matches because they're
        # long enough to be safe (transcript / caption / subtitle / etc.).
        from functions.js_helpers import GET_SELECTOR_JS as _SEL_TS
        transcript_buttons = await page.evaluate(r"""() => {""" + _SEL_TS + r"""
            const longKeywords = ['transcript', 'caption', 'closed caption', 'subtitles', 'subtitle'];
            // 'cc' matched as a whole word OR delimited by punctuation —
            // captures "[CC]", "(cc)", "CC button", etc. without firing
            // on "ACcept", "access", "ACcessibility", "occurs", or any
            // other word that incidentally contains the bigram.
            const ccRe = /(^|[^a-z])cc([^a-z]|$)/i;
            const candidates = document.querySelectorAll('a, button, [role="button"], [role="link"]');
            const found = [];
            for (const el of candidates) {
                const text = (el.textContent || '').trim().toLowerCase();
                const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
                const title = (el.getAttribute('title') || '').toLowerCase();
                const combined = text + ' ' + ariaLabel + ' ' + title;
                let matched = '';
                for (const kw of longKeywords) {
                    if (combined.includes(kw)) { matched = kw; break; }
                }
                if (!matched && ccRe.test(combined)) matched = 'cc';
                if (matched) {
                    found.push({
                        selector: getSelector(el),
                        text: (el.textContent || '').trim(),
                        href: el.href || el.getAttribute('href') || '',
                        matched_keyword: matched,
                    });
                }
            }
            return found;
        }""")

        # Attach transcript button info to each media entry so 1.2.x checks
        # can reference them (also stored at capture_data level).
        if transcript_buttons:
            for m in capture_data.media:
                m.setdefault("transcript_buttons", [])
                m["transcript_buttons"] = transcript_buttons
            capture_data.transcript_buttons = transcript_buttons
    except Exception:
        logger.exception("Media playback test failed")


# ─── Record media playback as actual video for VL model ──────────────────────

async def _record_media_with_captions(
    page: Page,
    capture_data: CaptureData,
    captures_dir: str,
) -> None:
    """For each video on the page, record SEPARATE video clips using
    Playwright's video recording for direct VL model analysis.

    Records three separate videos per media element:
      1. Captions ON   — video playing with captions enabled
      2. Captions OFF   — same video without captions (comparison)
      3. Full player     — the entire player area showing controls

    Each recording is a real .webm video file sent to the VL model,
    NOT screenshots. The VL model (Qwen3-VL-32B) natively accepts
    video_url content for temporal/visual analysis.
    """
    from playwright.async_api import async_playwright

    try:
        media_dir = os.path.join(captures_dir, "media_recordings")
        os.makedirs(media_dir, exist_ok=True)

        # Get the current page URL to re-navigate in recording contexts
        current_url = page.url

        video_selectors = await page.evaluate("""() => {
            const videos = document.querySelectorAll('video');
            const selectors = [];
            for (let i = 0; i < videos.length; i++) {
                const v = videos[i];
                let sel = 'video';
                if (v.id) sel = '#' + v.id;
                else if (v.className && typeof v.className === 'string')
                    sel = 'video.' + v.className.trim().split(/\\s+/)[0];
                else sel = `video:nth-of-type(${i + 1})`;
                selectors.push(sel);
            }
            return selectors;
        }""")

        if not video_selectors:
            return

        for idx, selector in enumerate(video_selectors):
            try:
                recording_data = {
                    "captions_on_video": "",
                    "captions_off_video": "",
                    "sign_language_detected": False,
                    "audio_description_button_found": False,
                }

                # ── Recording 1: Video with captions ON ──────────────
                captions_on_path = os.path.join(media_dir, f"video_{idx}_captions_on.webm")
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch()
                    rec_context = await browser.new_context(
                        viewport={"width": 1280, "height": 720},
                        record_video_dir=media_dir,
                        record_video_size={"width": 1280, "height": 720},
                    )
                    rec_page = await rec_context.new_page()
                    await _safe_goto(rec_page, current_url, timeout=60000)
                    await rec_page.wait_for_timeout(1000)

                    # Enable captions and play
                    await rec_page.evaluate("""(sel) => {
                        const v = document.querySelector(sel);
                        if (!v) return;
                        if (v.textTracks) {
                            for (let i = 0; i < v.textTracks.length; i++) {
                                const tt = v.textTracks[i];
                                if (tt.kind === 'captions' || tt.kind === 'subtitles')
                                    tt.mode = 'showing';
                            }
                        }
                        v.scrollIntoView({block: 'center'});
                        v.muted = true;
                        v.currentTime = 0;
                        v.play().catch(() => {});
                    }""", selector)

                    # Record 15 seconds of playback with captions
                    await rec_page.wait_for_timeout(15000)

                    # Pause
                    await rec_page.evaluate("""(sel) => {
                        const v = document.querySelector(sel);
                        if (v) v.pause();
                    }""", selector)

                    # Check for sign language and audio description while here
                    sl_info = await rec_page.evaluate("""(sel) => {
                        const v = document.querySelector(sel);
                        if (!v) return null;
                        const parent = v.closest('.video-container, .player, .media-wrapper, [class*="player"]') || v.parentElement;
                        if (!parent) return null;
                        const videos = parent.querySelectorAll('video');
                        if (videos.length > 1) return 'multiple_videos';
                        const signEls = parent.querySelectorAll('[class*="sign"], [aria-label*="sign"], [title*="sign"]');
                        if (signEls.length > 0) return 'sign_element_found';
                        return null;
                    }""", selector)
                    recording_data["sign_language_detected"] = bool(sl_info)

                    ad_found = await rec_page.evaluate("""(sel) => {
                        const v = document.querySelector(sel);
                        if (!v) return false;
                        const parent = v.closest('.video-container, .player, .media-wrapper, [class*="player"]') || v.parentElement;
                        if (!parent) return false;
                        const buttons = parent.querySelectorAll('button, [role="button"]');
                        for (const b of buttons) {
                            const text = (b.textContent || '').toLowerCase() + ' ' + (b.getAttribute('aria-label') || '').toLowerCase();
                            if (text.includes('audio desc') || text.includes('description'))
                                return true;
                        }
                        return false;
                    }""", selector)
                    recording_data["audio_description_button_found"] = bool(ad_found)

                    video_file = rec_page.video
                    await rec_page.close()
                    await rec_context.close()
                    await browser.close()
                    if video_file:
                        saved = await video_file.path()
                        if saved and os.path.exists(str(saved)):
                            try:
                                os.rename(str(saved), captions_on_path)
                            except OSError:
                                import shutil
                                shutil.copy2(str(saved), captions_on_path)
                            recording_data["captions_on_video"] = captions_on_path

                # ── Recording 2: Video with captions OFF ─────────────
                captions_off_path = os.path.join(media_dir, f"video_{idx}_captions_off.webm")
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch()
                    rec_context = await browser.new_context(
                        viewport={"width": 1280, "height": 720},
                        record_video_dir=media_dir,
                        record_video_size={"width": 1280, "height": 720},
                    )
                    rec_page = await rec_context.new_page()
                    await _safe_goto(rec_page, current_url, timeout=60000)
                    await rec_page.wait_for_timeout(1000)

                    # Disable captions and play
                    await rec_page.evaluate("""(sel) => {
                        const v = document.querySelector(sel);
                        if (!v) return;
                        if (v.textTracks) {
                            for (let i = 0; i < v.textTracks.length; i++)
                                v.textTracks[i].mode = 'disabled';
                        }
                        v.scrollIntoView({block: 'center'});
                        v.muted = true;
                        v.currentTime = 0;
                        v.play().catch(() => {});
                    }""", selector)

                    # Record 15 seconds without captions
                    await rec_page.wait_for_timeout(15000)

                    await rec_page.evaluate("""(sel) => {
                        const v = document.querySelector(sel);
                        if (v) v.pause();
                    }""", selector)

                    video_file = rec_page.video
                    await rec_page.close()
                    await rec_context.close()
                    await browser.close()
                    if video_file:
                        saved = await video_file.path()
                        if saved and os.path.exists(str(saved)):
                            try:
                                os.rename(str(saved), captions_off_path)
                            except OSError:
                                import shutil
                                shutil.copy2(str(saved), captions_off_path)
                            recording_data["captions_off_video"] = captions_off_path

                # Store on media entry
                if idx < len(capture_data.media):
                    capture_data.media[idx]["recording"] = recording_data
                else:
                    capture_data.media.append({"recording": recording_data})

                logger.info(
                    "Recorded video %d: captions_on=%s captions_off=%s AD=%s SL=%s",
                    idx,
                    bool(recording_data["captions_on_video"]),
                    bool(recording_data.get("captions_off_video")),
                    recording_data["audio_description_button_found"],
                    recording_data["sign_language_detected"],
                )

            except Exception:
                logger.exception("Failed to record video %d (%s)", idx, selector)

    except Exception:
        logger.exception("Media recording failed")


# ─── Caption toggle recording (VL model watches CC get turned on) ────────────

async def _record_caption_toggle(
    page: Page,
    capture_data: CaptureData,
    captures_dir: str,
) -> None:
    """Record a video of the user clicking the CC/caption button on each
    video player.  The VL model watches this to verify captions actually
    appear on screen when activated.

    Also attempts to find and click native CC buttons in popular players
    (HTML5 default, YouTube, Vimeo, etc.) and records the before/after.
    """
    from playwright.async_api import async_playwright

    try:
        media_dir = os.path.join(captures_dir, "media_recordings")
        os.makedirs(media_dir, exist_ok=True)
        current_url = page.url

        video_selectors = await page.evaluate("""() => {
            const videos = document.querySelectorAll('video');
            return Array.from(videos).map((v, i) => {
                let sel = 'video';
                if (v.id) sel = '#' + v.id;
                else if (v.className && typeof v.className === 'string')
                    sel = 'video.' + v.className.trim().split(/\\s+/)[0];
                else sel = `video:nth-of-type(${i + 1})`;
                return sel;
            });
        }""")

        if not video_selectors:
            return

        for idx, selector in enumerate(video_selectors):
            try:
                toggle_path = os.path.join(media_dir, f"video_{idx}_caption_toggle.webm")

                async with async_playwright() as pw:
                    browser = await pw.chromium.launch()
                    rec_context = await browser.new_context(
                        viewport={"width": 1280, "height": 720},
                        record_video_dir=media_dir,
                        record_video_size={"width": 1280, "height": 720},
                    )
                    rec_page = await rec_context.new_page()
                    await _safe_goto(rec_page, current_url, timeout=60000)
                    await rec_page.wait_for_timeout(1000)

                    # Scroll video into view and start playing (muted)
                    await rec_page.evaluate("""(sel) => {
                        const v = document.querySelector(sel);
                        if (!v) return;
                        v.scrollIntoView({block: 'center'});
                        v.muted = true;
                        v.currentTime = 0;
                        v.play().catch(() => {});
                    }""", selector)
                    await rec_page.wait_for_timeout(3000)

                    # Screenshot BEFORE captions
                    before_path = os.path.join(media_dir, f"video_{idx}_captions_before.png")
                    await rec_page.screenshot(path=before_path, full_page=False)

                    # Try clicking CC button in the player UI
                    cc_clicked = await rec_page.evaluate("""(sel) => {
                        const v = document.querySelector(sel);
                        if (!v) return 'no_video';

                        // Find CC/caption button in parent container
                        const container = v.closest(
                            '.video-container, .player, .media-wrapper, ' +
                            '[class*="player"], [class*="video"]'
                        ) || v.parentElement;
                        if (!container) return 'no_container';

                        const buttons = container.querySelectorAll(
                            'button, [role="button"], [class*="cc"], ' +
                            '[class*="caption"], [class*="subtitle"], ' +
                            '[aria-label*="caption"], [aria-label*="subtitle"], ' +
                            '[aria-label*="CC"], [title*="caption"]'
                        );

                        for (const btn of buttons) {
                            const text = (btn.textContent || '').toLowerCase();
                            const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
                            const title = (btn.getAttribute('title') || '').toLowerCase();
                            const cls = (typeof btn.className === 'string' ? btn.className : '').toLowerCase();
                            const combined = text + ' ' + ariaLabel + ' ' + title + ' ' + cls;

                            if (combined.match(/caption|subtitle|\\bcc\\b|closed.?cap/)) {
                                btn.click();
                                return 'clicked_ui_button';
                            }
                        }

                        // Fallback: enable via textTracks API
                        if (v.textTracks && v.textTracks.length > 0) {
                            for (let i = 0; i < v.textTracks.length; i++) {
                                const tt = v.textTracks[i];
                                if (tt.kind === 'captions' || tt.kind === 'subtitles') {
                                    tt.mode = 'showing';
                                    return 'enabled_via_api';
                                }
                            }
                        }
                        return 'no_cc_found';
                    }""", selector)

                    # Wait for captions to render
                    await rec_page.wait_for_timeout(5000)

                    # Screenshot AFTER captions
                    after_path = os.path.join(media_dir, f"video_{idx}_captions_after.png")
                    await rec_page.screenshot(path=after_path, full_page=False)

                    # Let it play a bit more with captions visible for the recording
                    await rec_page.wait_for_timeout(5000)

                    video_file = rec_page.video
                    await rec_page.close()
                    await rec_context.close()
                    await browser.close()

                    if video_file:
                        saved = await video_file.path()
                        if saved and os.path.exists(str(saved)):
                            try:
                                os.rename(str(saved), toggle_path)
                            except OSError:
                                import shutil
                                shutil.copy2(str(saved), toggle_path)

                # Store results on the media entry
                toggle_data = {
                    "caption_toggle_video": toggle_path if os.path.exists(toggle_path) else "",
                    "captions_before_screenshot": before_path if os.path.exists(before_path) else "",
                    "captions_after_screenshot": after_path if os.path.exists(after_path) else "",
                    "cc_click_result": cc_clicked,
                }

                if idx < len(capture_data.media):
                    capture_data.media[idx].setdefault("recording", {})
                    capture_data.media[idx]["recording"].update(toggle_data)
                else:
                    capture_data.media.append({"recording": toggle_data})

                logger.info(
                    "Caption toggle video %d: %s (cc_click=%s)",
                    idx, bool(toggle_data["caption_toggle_video"]), cc_clicked,
                )

            except Exception:
                logger.exception("Failed to record caption toggle for video %d", idx)

    except Exception:
        logger.exception("Caption toggle recording failed")


# ─── Transcript verification (click and verify content) ─────────────────────

async def _transcript_verification(
    page: Page,
    capture_data: CaptureData,
    captures_dir: str,
) -> None:
    """Click transcript buttons/links and verify transcript content appears.

    For each transcript button found during media_playback:
    1. Screenshot before clicking
    2. Click the button/link
    3. Wait for content to appear (expand, navigate, modal)
    4. Screenshot after clicking
    5. Check if substantial text content appeared
    6. If it's a link, check the destination has text content
    """
    try:
        transcript_dir = os.path.join(captures_dir, "transcripts")
        os.makedirs(transcript_dir, exist_ok=True)

        buttons = getattr(capture_data, "transcript_buttons", [])
        if not buttons:
            return

        results: list[dict] = []

        for idx, btn_info in enumerate(buttons):
            btn_selector = btn_info.get("selector", "")
            btn_text = btn_info.get("text", "")
            btn_href = btn_info.get("href", "")

            if not btn_selector:
                continue

            result = {
                "selector": btn_selector,
                "text": btn_text,
                "href": btn_href,
                "clicked": False,
                "transcript_found": False,
                "transcript_text_length": 0,
                "before_screenshot": "",
                "after_screenshot": "",
                "destination_screenshot": "",
                "method": "",
                "captured_text": "",
                "looks_like_transcript": False,
            }

            try:
                # Screenshot before clicking
                before_path = os.path.join(transcript_dir, f"transcript_{idx}_before.png")
                await page.screenshot(path=before_path, full_page=False)
                result["before_screenshot"] = before_path

                # Save the current URL to detect navigation
                url_before = page.url

                # Try to find and click the element
                el = None
                try:
                    el = page.locator(btn_selector).first
                    if not await el.is_visible():
                        # Try by text content
                        el = page.get_by_text(btn_text).first
                except Exception:
                    # fallback — selector lookup failed (invalid/stale
                    # selector); retry by visible text, click below
                    # reports failure if that also misses
                    el = page.get_by_text(btn_text).first

                if el:
                    try:
                        await el.scroll_into_view_if_needed(timeout=5000)
                    except Exception:
                        pass  # best-effort — element may be off-screen or unscrollable; click below will retry

                    await el.click(timeout=10000)
                    result["clicked"] = True
                    await page.wait_for_timeout(2000)

                    # Check if we navigated to a new page
                    url_after = page.url
                    if url_after != url_before:
                        result["method"] = "navigated"
                        # Wait for the destination page to load
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass  # best-effort — networkidle may never settle on long-poll pages
                        await page.wait_for_timeout(1000)

                        # Screenshot the destination
                        dest_path = os.path.join(transcript_dir, f"transcript_{idx}_destination.png")
                        await page.screenshot(path=dest_path, full_page=True)
                        result["destination_screenshot"] = dest_path

                        # Check for substantial text content on the destination
                        text_content = await page.evaluate("""() => {
                            const body = document.body;
                            if (!body) return '';
                            // Get visible text, skip scripts/styles
                            const clone = body.cloneNode(true);
                            for (const el of clone.querySelectorAll('script, style, noscript, nav, header, footer'))
                                el.remove();
                            return (clone.textContent || '').trim();
                        }""")

                        result["transcript_text_length"] = len(text_content)
                        # A real transcript should have substantial text (>200 chars)
                        result["transcript_found"] = len(text_content) > 200

                        # Verify the appeared content looks like an actual transcript
                        captured_text = ""
                        try:
                            new_content_check = await page.evaluate("""() => {
                                const blocks = document.querySelectorAll(
                                    '[class*="transcript"], [id*="transcript"], ' +
                                    '[role="region"], [aria-live], dialog, ' +
                                    '[class*="caption"], [id*="caption"]'
                                );
                                const texts = [];
                                for (const b of blocks) {
                                    const text = (b.textContent || '').trim();
                                    if (text.length > 20) texts.push(text);
                                }
                                if (texts.length === 0) {
                                    // Fallback: use the full page text
                                    const body = document.body;
                                    if (body) {
                                        const clone = body.cloneNode(true);
                                        for (const el of clone.querySelectorAll('script, style, noscript, nav, header, footer'))
                                            el.remove();
                                        const t = (clone.textContent || '').trim();
                                        if (t.length > 20) texts.push(t);
                                    }
                                }
                                return texts.join('\\n---\\n');
                            }""")
                            captured_text = new_content_check if new_content_check else ""
                        except Exception:
                            pass  # best-effort — fall back to empty captured_text if in-page eval fails

                        has_timestamps = bool(re.search(r'\d{1,2}:\d{2}', captured_text))
                        has_speaker_labels = bool(re.search(r'(?:Speaker|Narrator|Host|\w+)\s*:', captured_text))
                        has_sentence_blocks = len(captured_text.split('.')) > 3
                        looks_like_transcript = has_timestamps or has_speaker_labels or (has_sentence_blocks and len(captured_text) > 100)
                        result["captured_text"] = captured_text
                        result["looks_like_transcript"] = looks_like_transcript

                        # Navigate back to the original page
                        try:
                            await _safe_goto(page, url_before, timeout=30000)
                            await page.wait_for_timeout(1000)
                        except Exception:
                            logger.warning("Could not navigate back after transcript check")

                    else:
                        # Content may have expanded in place (accordion, modal, etc.)
                        result["method"] = "in_page"
                        await page.wait_for_timeout(1000)

                        # Screenshot after clicking (may show expanded transcript)
                        after_path = os.path.join(transcript_dir, f"transcript_{idx}_after.png")
                        await page.screenshot(path=after_path, full_page=False)
                        result["after_screenshot"] = after_path

                        # Check if new visible text content appeared
                        new_content = await page.evaluate("""() => {
                            // Look for newly visible elements that might be transcripts
                            const candidates = document.querySelectorAll(
                                '[class*="transcript"], [id*="transcript"], ' +
                                '[class*="caption-text"], [class*="accordion-body"], ' +
                                '[role="region"][aria-expanded="true"], ' +
                                'dialog[open], [class*="modal"][style*="display: block"], ' +
                                '[class*="modal"][style*="visibility: visible"], ' +
                                '[class*="modal"].show, [class*="modal"].active, ' +
                                '[aria-hidden="false"], details[open]'
                            );

                            let maxLen = 0;
                            let bestText = '';
                            for (const el of candidates) {
                                const text = (el.textContent || '').trim();
                                if (text.length > maxLen) {
                                    maxLen = text.length;
                                    bestText = text;
                                }
                            }

                            // Also check if any element near the button expanded
                            if (maxLen < 100) {
                                const visible = document.querySelectorAll(
                                    'p, div, section, article'
                                );
                                for (const el of visible) {
                                    const style = window.getComputedStyle(el);
                                    if (style.display !== 'none' && style.visibility !== 'hidden') {
                                        const text = (el.textContent || '').trim();
                                        if (text.length > 500 && text.length > maxLen) {
                                            maxLen = text.length;
                                            bestText = text;
                                        }
                                    }
                                }
                            }

                            return {length: maxLen, sample: bestText};
                        }""")

                        result["transcript_text_length"] = new_content.get("length", 0)
                        result["transcript_found"] = new_content.get("length", 0) > 200

                        # Verify the appeared content looks like an actual transcript
                        captured_text = ""
                        try:
                            in_page_text = await page.evaluate("""() => {
                                const blocks = document.querySelectorAll(
                                    '[class*="transcript"], [id*="transcript"], ' +
                                    '[role="region"], [aria-live], dialog, ' +
                                    '[class*="caption"], [id*="caption"]'
                                );
                                const texts = [];
                                for (const b of blocks) {
                                    const text = (b.textContent || '').trim();
                                    if (text.length > 20) texts.push(text);
                                }
                                return texts.join('\\n---\\n');
                            }""")
                            captured_text = in_page_text if in_page_text else ""
                        except Exception:
                            pass  # best-effort — fall back to empty captured_text if in-page eval fails

                        # Fall back to the sample already captured by the in-page check
                        if not captured_text:
                            captured_text = new_content.get("sample", "") or ""

                        has_timestamps = bool(re.search(r'\d{1,2}:\d{2}', captured_text))
                        has_speaker_labels = bool(re.search(r'(?:Speaker|Narrator|Host|\w+)\s*:', captured_text))
                        has_sentence_blocks = len(captured_text.split('.')) > 3
                        looks_like_transcript = has_timestamps or has_speaker_labels or (has_sentence_blocks and len(captured_text) > 100)
                        result["captured_text"] = captured_text
                        result["looks_like_transcript"] = looks_like_transcript

                elif btn_href and btn_href.startswith("http"):
                    # Can't click the element — try navigating directly to the href
                    result["method"] = "direct_navigation"
                    try:
                        await _safe_goto(page, btn_href, timeout=30000)
                        await page.wait_for_timeout(1000)

                        dest_path = os.path.join(transcript_dir, f"transcript_{idx}_destination.png")
                        await page.screenshot(path=dest_path, full_page=True)
                        result["destination_screenshot"] = dest_path

                        text_content = await page.evaluate("""() => {
                            const body = document.body;
                            if (!body) return '';
                            const clone = body.cloneNode(true);
                            for (const el of clone.querySelectorAll('script, style, noscript, nav, header, footer'))
                                el.remove();
                            return (clone.textContent || '').trim();
                        }""")

                        result["transcript_text_length"] = len(text_content)
                        result["transcript_found"] = len(text_content) > 200
                        result["clicked"] = True

                        # Verify the appeared content looks like an actual transcript
                        captured_text = ""
                        try:
                            direct_nav_text = await page.evaluate("""() => {
                                const blocks = document.querySelectorAll(
                                    '[class*="transcript"], [id*="transcript"], ' +
                                    '[role="region"], [aria-live], dialog, ' +
                                    '[class*="caption"], [id*="caption"]'
                                );
                                const texts = [];
                                for (const b of blocks) {
                                    const text = (b.textContent || '').trim();
                                    if (text.length > 20) texts.push(text);
                                }
                                if (texts.length === 0) {
                                    const body = document.body;
                                    if (body) {
                                        const clone = body.cloneNode(true);
                                        for (const el of clone.querySelectorAll('script, style, noscript, nav, header, footer'))
                                            el.remove();
                                        const t = (clone.textContent || '').trim();
                                        if (t.length > 20) texts.push(t);
                                    }
                                }
                                return texts.join('\\n---\\n');
                            }""")
                            captured_text = direct_nav_text if direct_nav_text else ""
                        except Exception:
                            pass  # best-effort — fall back to empty captured_text if in-page eval fails

                        has_timestamps = bool(re.search(r'\d{1,2}:\d{2}', captured_text))
                        has_speaker_labels = bool(re.search(r'(?:Speaker|Narrator|Host|\w+)\s*:', captured_text))
                        has_sentence_blocks = len(captured_text.split('.')) > 3
                        looks_like_transcript = has_timestamps or has_speaker_labels or (has_sentence_blocks and len(captured_text) > 100)
                        result["captured_text"] = captured_text
                        result["looks_like_transcript"] = looks_like_transcript

                        # Navigate back
                        await _safe_goto(page, url_before, timeout=30000)
                    except Exception:
                        logger.debug("Direct navigation to transcript href failed: %s", btn_href)

            except Exception:
                logger.debug("Transcript verification failed for button %d: %s", idx, btn_selector)

            results.append(result)

        capture_data.transcript_verifications = results

        found = sum(1 for r in results if r["transcript_found"])
        clicked = sum(1 for r in results if r["clicked"])
        logger.info(
            "Transcript verification: %d buttons, %d clicked, %d transcripts found",
            len(results), clicked, found,
        )

    except Exception:
        logger.exception("Transcript verification failed")


# ─── Skip link verification ──────────────────────────────────────────────────

_SKIP_LINK_PROBE_JS = r"""(args) => {
    const {targetId, baseline} = args;
    const target = targetId ? document.getElementById(targetId) : null;
    const out = {
        url_hash: window.location.hash || '',
        scroll_y: window.scrollY || window.pageYOffset || 0,
    };
    const el = document.activeElement;
    out.focused_tag = el && el.tagName ? el.tagName.toLowerCase() : '';
    out.focused_id = el && el.id ? el.id : '';
    let walker = el;
    let focus_inside_target = false;
    while (walker && walker !== document.body) {
        if (walker.id === targetId) { focus_inside_target = true; break; }
        if (walker.tagName && walker.tagName.toLowerCase() === 'main') {
            focus_inside_target = true; break;
        }
        if (walker.getAttribute && walker.getAttribute('role') === 'main') {
            focus_inside_target = true; break;
        }
        walker = walker.parentNode;
    }
    out.focus_inside_target = focus_inside_target;
    if (target) {
        const rect = target.getBoundingClientRect();
        out.target_visible_or_above = (
            rect.bottom > 0 && rect.top < window.innerHeight
        ) || rect.top <= 5;
    } else {
        out.target_visible_or_above = false;
    }
    if (baseline) {
        out.hash_changed = out.url_hash !== baseline.url_hash;
        out.scrolled = Math.abs(out.scroll_y - baseline.scroll_y) > 20;
    }
    return out;
}"""


async def _probe_one_skip_link(
    page: Page,
    sel: str,
    href: str,
    text: str,
    cand: dict,
    first_tab_selector: str,
    results: list[dict],
) -> None:
    """Probe a single skip-link candidate and append the result.

    Wrapped in `asyncio.wait_for` by the caller with a per-candidate
    timeout so a hung Playwright call cannot stall the rest of the
    pipeline. Every Playwright interaction inside carries its own
    short timeout as a second line of defence.
    """
    click_ok = False
    keyboard_ok = False
    focus_after_click: dict | None = None
    focus_after_keyboard: dict | None = None
    error: str | None = None

    try:
        el_handle = await page.query_selector(sel)
        if el_handle is None:
            error = "selector not found in DOM"
        else:
            target_id = href.lstrip("#")

            baseline_click = await page.evaluate(
                _SKIP_LINK_PROBE_JS,
                {"targetId": target_id, "baseline": None},
            )

            # -- Mouse click path ---------------------------------------
            try:
                await el_handle.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass  # best-effort scroll; click below will retry
            try:
                # 3-second click cap. Playwright defaults to 30s on
                # element.click() which is far too long for a per-
                # candidate probe -- a single click hanging on a
                # broken JS handler used to wedge the whole pipeline.
                await el_handle.click(timeout=3000)
                await page.wait_for_timeout(500)
                post_click = await page.evaluate(
                    _SKIP_LINK_PROBE_JS,
                    {"targetId": target_id, "baseline": baseline_click},
                )
                await page.keyboard.press("Tab")
                await page.wait_for_timeout(250)
                post_click_tab = await page.evaluate(
                    _SKIP_LINK_PROBE_JS,
                    {"targetId": target_id, "baseline": baseline_click},
                )
                focus_after_click = {
                    "url_hash": post_click.get("url_hash"),
                    "hash_changed": bool(post_click.get("hash_changed")),
                    "scrolled": bool(post_click.get("scrolled")),
                    "target_visible_or_above": bool(
                        post_click.get("target_visible_or_above")
                    ),
                    "next_tab_focus_inside_target": bool(
                        post_click_tab.get("focus_inside_target")
                    ),
                    "focused_after_click": post_click.get("focused_tag", ""),
                    "focused_after_tab": post_click_tab.get("focused_tag", ""),
                }
                click_ok = (
                    focus_after_click["hash_changed"]
                    or focus_after_click["scrolled"]
                    or focus_after_click["target_visible_or_above"]
                    or focus_after_click["next_tab_focus_inside_target"]
                )
            except Exception as exc:
                error = f"click path: {type(exc).__name__}"

            # Reset hash + scroll before keyboard path
            try:
                await page.evaluate(
                    "window.history.replaceState(null, '', "
                    "window.location.pathname + window.location.search); "
                    "window.scrollTo(0, 0); document.body.focus();"
                )
                await page.wait_for_timeout(200)
            except Exception:
                pass  # cleanup; best-effort URL/scroll reset

            # -- Keyboard path (focus + Enter) -------------------------
            try:
                baseline_kb = await page.evaluate(
                    _SKIP_LINK_PROBE_JS,
                    {"targetId": target_id, "baseline": None},
                )
                await page.evaluate(
                    "(el) => el.focus({preventScroll: true})",
                    el_handle,
                )
                await page.wait_for_timeout(200)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(500)
                post_kb = await page.evaluate(
                    _SKIP_LINK_PROBE_JS,
                    {"targetId": target_id, "baseline": baseline_kb},
                )
                await page.keyboard.press("Tab")
                await page.wait_for_timeout(250)
                post_kb_tab = await page.evaluate(
                    _SKIP_LINK_PROBE_JS,
                    {"targetId": target_id, "baseline": baseline_kb},
                )
                focus_after_keyboard = {
                    "url_hash": post_kb.get("url_hash"),
                    "hash_changed": bool(post_kb.get("hash_changed")),
                    "scrolled": bool(post_kb.get("scrolled")),
                    "target_visible_or_above": bool(
                        post_kb.get("target_visible_or_above")
                    ),
                    "next_tab_focus_inside_target": bool(
                        post_kb_tab.get("focus_inside_target")
                    ),
                    "focused_after_enter": post_kb.get("focused_tag", ""),
                    "focused_after_tab": post_kb_tab.get("focused_tag", ""),
                }
                keyboard_ok = (
                    focus_after_keyboard["hash_changed"]
                    or focus_after_keyboard["scrolled"]
                    or focus_after_keyboard["target_visible_or_above"]
                    or focus_after_keyboard["next_tab_focus_inside_target"]
                )
            except Exception as exc:
                kb_err = f"keyboard path: {type(exc).__name__}"
                error = f"{error}; {kb_err}" if error else kb_err

    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    results.append({
        "skip_link_selector": sel,
        "skip_link_text": text,
        "target_href": href,
        "source": cand.get("source", "unknown"),
        "click_activates": click_ok,
        "keyboard_activates": keyboard_ok,
        "focus_after_click": focus_after_click,
        "focus_after_keyboard": focus_after_keyboard,
        # SC 2.4.1 is specifically a KEYBOARD requirement.
        # A skip link that works with mouse but not keyboard
        # does not satisfy 2.4.1.
        "focus_landed_on_target": keyboard_ok,
        "is_first_tabstop": (sel == first_tab_selector),
        "error": error,
    })


async def _skip_link_verification(
    page: Page,
    capture_data: CaptureData,
) -> None:
    """Verify skip links work for keyboard users (SC 2.4.1 Bypass Blocks).

    WCAG 2.4.1 Level A requires a mechanism for users to bypass blocks
    of content repeated on multiple pages. The dominant pattern is a
    "Skip to main content" link as the first (or near-first) tab stop
    that moves focus past the global navigation to the main landmark.

    For each skip link candidate:

      1. Discover candidates via TWO signals:
         a. ``capture_data.skip_links`` populated during deterministic
            capture (href starts with ``#`` and link text/class hints
            at "skip").
         b. A fresh DOM scan for additional candidates the capture may
            have missed (class hints: ``skip-link``, ``sr-only``,
            ``visually-hidden-focusable``; text hints: "skip to",
            "jump to", "go to main").
      2. First-tabstop check: from body, press Tab once. Is the first
         focused element the skip link (or within its container)?
         This is the usability requirement -- a skip link 10 tabs in
         is effectively useless.
      3. Click-activation: verify the link works via MOUSE.
      4. Keyboard-activation: verify the link works via KEYBOARD
         (focus the link, press Enter). SC 2.4.1 is satisfied only
         when the keyboard path works.
      5. After activation, verify focus actually moved into or near
         the named landmark (``<main>``, ``[role="main"]``, or the
         specific target element by id).

    Results feed SC 2.4.1 (Bypass Blocks) directly and SC 2.4.3
    (Focus Order) when the skip link is present but mis-ordered.
    """
    try:
        results: list[dict] = []

        # --- Source A: skip_links that deterministic capture found ---
        candidates: list[dict] = []
        seen_selectors: set[str] = set()
        for sl in capture_data.skip_links or []:
            sel = sl.get("selector", "")
            if sel and sel not in seen_selectors:
                candidates.append({
                    "selector": sel,
                    "href": sl.get("href", ""),
                    "text": sl.get("text", ""),
                    "source": "capture",
                })
                seen_selectors.add(sel)

        # --- Source B: fresh DOM scan for additional candidates ------
        # Catches pages where the capture phase did not detect a skip
        # link (unusual class names, dynamic visibility). Uses both
        # text and class hints.
        extras = await page.evaluate(r"""() => {
            function isCandidate(el) {
                if (!el) return false;
                const href = el.getAttribute('href') || '';
                if (!href.startsWith('#')) return false;
                const text = (el.textContent || '').trim().toLowerCase();
                const cls = (el.className && typeof el.className === 'string' ? el.className : '').toLowerCase();
                const textHint = /(skip|jump|go)\s+(to|past|over)\b/.test(text);
                const classHint = /\b(skip[-_]?link|skip[-_]?nav|visually-hidden-focusable|sr-only-focusable)\b/.test(cls);
                return textHint || classHint;
            }
            function makeSel(el) {
                if (el.id) return '#' + el.id;
                return el.tagName.toLowerCase()
                    + (el.className && typeof el.className === 'string'
                       ? '.' + el.className.trim().split(/\s+/)[0]
                       : '');
            }
            const hits = [];
            for (const a of document.querySelectorAll('a[href^="#"]')) {
                if (!isCandidate(a)) continue;
                hits.push({
                    selector: makeSel(a),
                    href: a.getAttribute('href') || '',
                    text: (a.textContent || '').trim(),
                });
            }
            return hits;
        }""")
        for e in extras or []:
            sel = e.get("selector", "")
            if sel and sel not in seen_selectors:
                candidates.append({
                    "selector": sel,
                    "href": e.get("href", ""),
                    "text": e.get("text", ""),
                    "source": "dom_scan",
                })
                seen_selectors.add(sel)

        # --- First-tabstop check: do this ONCE regardless of count --
        first_tab_is_skip = False
        first_tab_selector = ""
        first_tab_text = ""
        try:
            await page.evaluate("document.body.focus()")
            await page.wait_for_timeout(200)
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(200)
            first_stop = await page.evaluate(r"""() => {
                const el = document.activeElement;
                if (!el || el === document.body) return null;
                const href = el.getAttribute('href') || '';
                const text = (el.textContent || '').trim();
                let sel = el.tagName.toLowerCase();
                if (el.id) sel = '#' + el.id;
                return {
                    selector: sel,
                    tag: el.tagName.toLowerCase(),
                    href, text,
                    classes: el.className || '',
                };
            }""")
            if first_stop:
                first_tab_selector = first_stop.get("selector", "")
                first_tab_text = first_stop.get("text", "")
                # Is the first-tab element itself a skip link?
                text_lower = first_tab_text.lower()
                cls_lower = str(first_stop.get("classes", "")).lower()
                href = first_stop.get("href", "")
                first_tab_is_skip = bool(
                    href.startswith("#") and (
                        any(w in text_lower for w in ("skip", "jump", "go to main"))
                        or "skip" in cls_lower
                        or "visually-hidden-focusable" in cls_lower
                        or "sr-only" in cls_lower
                    )
                )
        except Exception:
            logger.debug("First-tabstop skip-link check failed")

        # --- Per-candidate click + keyboard verification -------------
        # Each candidate runs under a strict 30-second budget. Without
        # this, a single bad click handler (infinite-loop JS, blocked
        # navigation, modal that swallows focus) hangs Playwright's
        # bridge and poisons every subsequent interactive test.
        # Observed on a municipal government site: a single skip_links candidate
        # wedged the bridge for 49 minutes until the outer timeout
        # fired, then all downstream tests inherited a dead page.
        # Every candidate is probed — no count cap. The per-candidate
        # timeout above already bounds the cost of pathological pages
        # with 50+ "skip" hits, and dropping candidates hides real
        # SC 2.4.1 evidence.
        PER_CANDIDATE_TIMEOUT = 30
        probed_selectors: list[str] = []
        bridge_abort = False
        for cand in candidates:
            sel = cand.get("selector", "")
            href = cand.get("href", "")
            text = cand.get("text", "")
            if not sel or not href:
                continue

            probed_selectors.append(sel)
            try:
                await asyncio.wait_for(
                    _probe_one_skip_link(
                        page, sel, href, text, cand, first_tab_selector, results,
                    ),
                    timeout=PER_CANDIDATE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Skip link probe timed out after %ds on selector %s -- "
                    "browser may be wedged, recording timeout and moving on",
                    PER_CANDIDATE_TIMEOUT, sel,
                )
                results.append({
                    "skip_link_selector": sel,
                    "skip_link_text": text,
                    "target_href": href,
                    "source": cand.get("source", "unknown"),
                    "click_activates": False,
                    "keyboard_activates": False,
                    "focus_after_click": None,
                    "focus_after_keyboard": None,
                    "focus_landed_on_target": False,
                    "is_first_tabstop": (sel == first_tab_selector),
                    "error": f"per-candidate timeout after {PER_CANDIDATE_TIMEOUT}s",
                })
                # If a candidate hung, the bridge may be wedged.
                # Probe it cheaply; if dead, break out so the outer
                # health check can recover the page.
                try:
                    await asyncio.wait_for(
                        page.evaluate("() => 1"), timeout=3,
                    )
                except (asyncio.TimeoutError, Exception):
                    logger.warning(
                        "Skip link verification: browser bridge "
                        "unresponsive after candidate timeout, "
                        "aborting remaining candidates"
                    )
                    bridge_abort = True
                    break

        capture_data.skip_link_results = results
        capture_data.skip_link_meta = {
            "candidates_found": len(candidates),
            "candidates_probed": len(probed_selectors),
            "probed_selectors": probed_selectors,
            "aborted_bridge_unresponsive": bridge_abort,
        }

        # Record first-tabstop context at the capture_data level so
        # SC 2.4.1's check can detect the "no skip link at all" case
        # (candidates == [] and first_tab_is_skip == False) vs "skip
        # link exists but not first" (candidates != [] and
        # first_tab_is_skip == False).
        capture_data.skip_link_first_tabstop = {
            "first_tab_is_skip": first_tab_is_skip,
            "first_tab_selector": first_tab_selector,
            "first_tab_text": first_tab_text,
            "any_skip_link_found": bool(candidates),
        }

        n = len(results)
        kb_ok = sum(1 for r in results if r.get("keyboard_activates"))
        click_ok_cnt = sum(1 for r in results if r.get("click_activates"))
        logger.info(
            "Skip links: %d candidate(s), %d click-activates, %d "
            "keyboard-activates; first tabstop is skip link: %s",
            n, click_ok_cnt, kb_ok, first_tab_is_skip,
        )
    except Exception:
        logger.exception("Skip link verification failed")


# ─── Form submission test ────────────────────────────────────────────────────

# Post-submit accessibility probe for a form's error state. Measures the
# fields the SC 3.3.x readers (checks/checks_3_3.py, checks/base.py form-error
# renderer) consume: live-region presence, programmatic error association,
# aria-invalid usage, and any visible error text on the page.
_FORM_ERROR_A11Y_PROBE_JS = """(sel) => {
    const form = document.querySelector(sel);
    if (!form) return null;
    const isVisible = (el) => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0
            && s.display !== 'none' && s.visibility !== 'hidden';
    };
    let hasAriaLive = false, hasRoleAlert = false;
    const visibleErrorText = [];
    for (const el of document.querySelectorAll('[role="alert"]')) {
        const t = (el.textContent || '').trim();
        if (t) { hasRoleAlert = true; visibleErrorText.push(t); }
    }
    for (const el of document.querySelectorAll('[aria-live]:not([aria-live="off"])')) {
        const t = (el.textContent || '').trim();
        if (t) { hasAriaLive = true; visibleErrorText.push(t); }
    }
    for (const el of form.querySelectorAll('.error, .invalid, .is-invalid, .field-error')) {
        const t = (el.textContent || '').trim();
        if (t && isVisible(el)) visibleErrorText.push(t);
    }
    let programmaticAssociation = false;
    let hasAriaInvalid = false;
    for (const el of form.querySelectorAll('input, select, textarea')) {
        if (el.getAttribute('aria-invalid') === 'true') hasAriaInvalid = true;
        const refIds = ((el.getAttribute('aria-describedby') || '') + ' '
            + (el.getAttribute('aria-errormessage') || '')).trim().split(/\\s+/).filter(Boolean);
        for (const id of refIds) {
            const target = document.getElementById(id);
            if (target && (target.textContent || '').trim()) {
                programmaticAssociation = true;
                break;
            }
        }
    }
    return {
        has_aria_live: hasAriaLive,
        has_role_alert: hasRoleAlert,
        programmatic_association: programmaticAssociation,
        has_aria_invalid: hasAriaInvalid,
        visible_error_text: visibleErrorText,
    };
}"""


async def _probe_form_error_a11y(page: Page, sel: str) -> dict:
    """Run the post-submit error-state probe; {} when the form vanished
    or the evaluate failed (missing keys mean "not measured" downstream).
    """
    try:
        return await page.evaluate(_FORM_ERROR_A11Y_PROBE_JS, sel) or {}
    except Exception:
        logger.warning("Form error a11y probe failed for %s", sel, exc_info=True)
        return {}


async def _form_submission_test(
    page: Page,
    capture_data: CaptureData,
    captures_dir: str,
) -> None:
    """Submit forms with empty required fields and screenshot error state."""
    try:
        forms_dir = os.path.join(captures_dir, "form_errors")
        os.makedirs(forms_dir, exist_ok=True)

        forms = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('form')).map((form, i) => {
                const hasRequired = form.querySelector('[required]') !== null;
                let selector = 'form';
                if (form.id) selector = '#' + form.id;
                else if (form.name) selector = 'form[name="' + form.name + '"]';
                const fields = Array.from(form.querySelectorAll('input, select, textarea')).map(el => ({
                    type: (el.getAttribute('type') || el.tagName.toLowerCase()).toLowerCase(),
                }));
                return {
                    selector,
                    hasRequired,
                    index: i,
                    method: (form.method || 'GET').toUpperCase(),
                    action: form.action || '',
                    fields,
                };
            });
        }""")

        # Safety filter — only submit forms that are safe to test.
        # Skip login/password forms, file upload forms, and POST forms
        # targeting external URLs to avoid side effects.
        page_origin = urlparse(page.url).netloc
        safe_forms = []
        for form_info in forms:
            fields = form_info.get("fields", [])
            # Skip forms containing password fields (likely login forms)
            if any(f.get("type") == "password" for f in fields):
                logger.debug("Skipping login/password form: %s", form_info.get("selector"))
                continue
            # Skip forms containing file upload fields
            if any(f.get("type") == "file" for f in fields):
                logger.debug("Skipping file upload form: %s", form_info.get("selector"))
                continue
            # Skip POST forms targeting external URLs
            action = form_info.get("action", "")
            method = form_info.get("method", "GET").upper()
            if method == "POST" and action:
                parsed_action = urlparse(action)
                action_host = parsed_action.netloc
                # If the action has a host and it differs from the page origin, skip
                if action_host and action_host != page_origin:
                    logger.debug(
                        "Skipping external POST form: %s -> %s",
                        form_info.get("selector"),
                        action,
                    )
                    continue
            safe_forms.append(form_info)

        results: list[dict] = []
        for form_info in safe_forms:
            if not form_info.get("hasRequired"):
                continue
            try:
                sel = form_info["selector"]
                idx = form_info["index"]

                # Clear required fields to ensure they're empty
                await page.evaluate("""(sel) => {
                    const form = document.querySelector(sel);
                    if (!form) return;
                    const required = form.querySelectorAll('[required]');
                    for (const el of required) {
                        if (el.tagName === 'SELECT') continue;
                        el.value = '';
                    }
                }""", sel)

                # Attempt to submit
                await page.evaluate("""(sel) => {
                    const form = document.querySelector(sel);
                    if (!form) return;
                    // Try the submit button first, fall back to form.submit()
                    const btn = form.querySelector('[type="submit"], button:not([type])');
                    if (btn) {
                        btn.click();
                    } else {
                        try { form.requestSubmit(); } catch(e) { form.submit(); }
                    }
                }""", sel)
                await page.wait_for_timeout(500)

                # Screenshot the error state
                error_path = os.path.join(forms_dir, f"form_error_{idx}.png")
                await page.screenshot(path=error_path, full_page=False)

                # Collect validation messages
                validation = await page.evaluate("""(sel) => {
                    const form = document.querySelector(sel);
                    if (!form) return [];
                    const required = form.querySelectorAll('[required]');
                    return Array.from(required).map(el => ({
                        tag: el.tagName.toLowerCase(),
                        name: el.name || '',
                        validationMessage: el.validationMessage || '',
                        ariaInvalid: el.getAttribute('aria-invalid') || '',
                        ariaErrormessage: el.getAttribute('aria-errormessage') || '',
                    }));
                }""", sel)

                # Probe the error state BEFORE the recovery fill mutates it —
                # these are the fields the SC 3.3.x readers consume.
                a11y_probe = await _probe_form_error_a11y(page, sel)

                # Error recovery test (3.3.1/3.3.3/3.3.4): fill valid values
                # and check if error state clears properly
                recovery_data = {}
                try:
                    await page.evaluate("""(sel) => {
                        const form = document.querySelector(sel);
                        if (!form) return;
                        for (const el of form.querySelectorAll('[required]')) {
                            const type = (el.getAttribute('type') || el.tagName).toLowerCase();
                            if (type === 'email') el.value = 'test@example.com';
                            else if (type === 'tel') el.value = '555-0100';
                            else if (type === 'number') el.value = '42';
                            else if (type === 'url') el.value = 'https://example.com';
                            else if (el.tagName === 'SELECT') { if (el.options.length > 1) el.selectedIndex = 1; }
                            else el.value = 'Test input';
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                    }""", sel)
                    await page.wait_for_timeout(300)
                    recovery_data = await page.evaluate("""(sel) => {
                        const form = document.querySelector(sel);
                        if (!form) return {};
                        const still_invalid = [];
                        for (const el of form.querySelectorAll('[required]')) {
                            if (el.getAttribute('aria-invalid') === 'true')
                                still_invalid.push({name: el.name || el.id || el.tagName});
                        }
                        return {
                            still_invalid: still_invalid,
                            visible_errors: document.querySelectorAll('[role="alert"], .error, .invalid').length,
                        };
                    }""", sel) or {}
                except Exception:
                    logger.debug("Form recovery test failed for %s", sel)

                entry = {
                    "form_selector": sel,
                    "selector": sel,
                    "error_screenshot": error_path,
                    "validation_messages": validation,
                    "recovery_state": recovery_data,
                }
                native_text = any(
                    (vm.get("validationMessage") or "").strip()
                    for vm in (validation or [])
                )
                if native_text:
                    entry["has_text_description"] = True
                elif a11y_probe:
                    entry["has_text_description"] = bool(
                        a11y_probe.get("visible_error_text"),
                    )
                # else: probe failed and no native text — leave the key
                # absent so readers treat it as "not measured".
                # Probe fields are only recorded when measured; readers
                # treat a missing key as "not measured", never as False.
                for key in ("has_aria_live", "has_role_alert",
                            "programmatic_association", "has_aria_invalid"):
                    if key in a11y_probe:
                        entry[key] = bool(a11y_probe[key])
                results.append(entry)
            except Exception:
                logger.debug("Form submission test failed for form %s", form_info.get("selector"))

        capture_data.form_errors = results
    except Exception:
        logger.exception("Form submission test failed")


# ─── Context change detection ────────────────────────────────────────────────

async def _context_change_detection(
    page: Page,
    capture_data: CaptureData,
) -> None:
    """Select the second option in <select> elements and check for URL changes."""
    try:
        selects = await page.evaluate("() => {" + GET_SELECTOR_JS + """
            return Array.from(document.querySelectorAll('select'))
                .filter(el => !el.disabled && !el.closest('fieldset[disabled]'))
                .map((el, i) => {
                    const options = Array.from(el.options).map(o => ({value: o.value, text: o.text}));
                    return {selector: getSelector(el), options, index: i};
                });
        }""")

        results: list[dict] = []
        for sel_info in selects:
            options = sel_info.get("options", [])
            if len(options) < 2:
                continue
            sel = sel_info["selector"]
            try:
                locator = page.locator(sel).first
                if not await locator.is_enabled(timeout=2000):
                    continue
                url_before = page.url

                second_value = options[1]["value"]
                await page.select_option(sel, value=second_value, timeout=10000)
                await page.wait_for_timeout(1000)

                url_after = page.url
                url_changed = url_before != url_after

                results.append({
                    "selector": sel,
                    "option_selected": options[1]["text"],
                    "url_before": url_before,
                    "url_after": url_after,
                    "url_changed": url_changed,
                    "context_change_detected": url_changed,
                })

                if url_changed:
                    await _safe_goto(page, url_before, timeout=30000)
                else:
                    try:
                        await page.select_option(sel, index=0, timeout=10000)
                    except Exception:
                        pass  # cleanup — best-effort restore of select to first option, page may be unrecoverable
            except Exception:
                logger.debug("Context change check failed for %s", sel)

        capture_data.context_changes = results
    except Exception:
        logger.exception("Context change detection failed")


# ─── Focus contrast (computed styles for ALL interactive elements) ────────────

_FOCUS_CONTRAST_JS = r"""
() => {
    const selectors = [
        'a[href]', 'button', 'input', 'select', 'textarea',
        '[role="button"]', '[role="link"]', '[role="checkbox"]',
        '[role="radio"]', '[role="tab"]', '[role="menuitem"]',
        '[tabindex="0"]',
    ];

    function isVisible(el) {
        const style = getComputedStyle(el);
        if (style.visibility === 'hidden') return false;
        if (el.offsetParent === null && style.position !== 'fixed') return false;
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return false;
        return true;
    }

    function readStyles(el) {
        const cs = window.getComputedStyle(el);
        return {
            outline: cs.outline || '',
            outlineColor: cs.outlineColor || '',
            outlineWidth: cs.outlineWidth || '',
            outlineStyle: cs.outlineStyle || '',
            outlineOffset: cs.outlineOffset || '',
            border: cs.border || '',
            borderColor: cs.borderColor || '',
            borderWidth: cs.borderWidth || '',
            borderStyle: cs.borderStyle || '',
            boxShadow: cs.boxShadow || '',
            backgroundColor: cs.backgroundColor || '',
            color: cs.color || '',
        };
    }
    // effectiveBg helper is injected from functions.js_helpers.EFFECTIVE_BG_JS

    // Canonical getSelector — same algorithm as the inventory + v2.
    // Fixes the 22-collisions-on-"a" bug in focus_contrast that made
    // the AI prompt's per-element focus-indicator lines indistinguishable.
    function makeSelector(el) {
        if (el.id) return '#' + CSS.escape(el.id);
        const parts = [];
        let current = el;
        while (current && current !== document.body && current !== document.documentElement) {
            let seg = current.tagName.toLowerCase();
            if (current.id) {
                parts.unshift('#' + CSS.escape(current.id));
                break;
            }
            if (current.parentElement) {
                const sibs = Array.from(current.parentElement.children)
                    .filter(s => s.tagName === current.tagName);
                if (sibs.length > 1) {
                    seg += ':nth-of-type(' + (sibs.indexOf(current) + 1) + ')';
                }
            }
            parts.unshift(seg);
            current = current.parentElement;
            if (parts.length >= 4) break;
        }
        const selector = parts.join(' > ');
        try {
            if (document.querySelectorAll(selector).length === 1) return selector;
            const fullParts = [];
            let node = el;
            while (node && node !== document.body && node !== document.documentElement) {
                let s = node.tagName.toLowerCase();
                if (node.id) { fullParts.unshift('#' + CSS.escape(node.id)); break; }
                if (node.parentElement) {
                    const sibs = Array.from(node.parentElement.children)
                        .filter(c => c.tagName === node.tagName);
                    if (sibs.length > 1) {
                        s += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
                    }
                }
                fullParts.unshift(s);
                node = node.parentElement;
            }
            return fullParts.join(' > ');
        } catch (e) { return selector; }
    }

    const seen = new Set();
    const results = [];

    for (const s of selectors) {
        for (const el of document.querySelectorAll(s)) {
            if (seen.has(el)) continue;
            seen.add(el);
            if (!isVisible(el)) continue;

            const rect = el.getBoundingClientRect();
            const selector = makeSelector(el);
            const text = (el.textContent || '').trim();

            // Read unfocused styles
            const unfocused = readStyles(el);

            // Focus the element
            el.focus({preventScroll: true});
            // Force style recomputation
            void el.offsetHeight;

            // Read focused styles
            const focused = readStyles(el);

            // Blur immediately
            el.blur();

            // Detect changes
            const changes = [];
            let indicatorType = 'none';
            let indicatorColor = '';

            if (unfocused.outlineStyle !== focused.outlineStyle ||
                unfocused.outlineWidth !== focused.outlineWidth ||
                unfocused.outlineColor !== focused.outlineColor) {
                changes.push('outline');
                indicatorType = 'outline';
                indicatorColor = focused.outlineColor;
            }
            if (unfocused.borderColor !== focused.borderColor ||
                unfocused.borderWidth !== focused.borderWidth ||
                unfocused.borderStyle !== focused.borderStyle) {
                changes.push('border');
                if (indicatorType === 'none') {
                    indicatorType = 'border';
                    indicatorColor = focused.borderColor;
                }
            }
            if (unfocused.boxShadow !== focused.boxShadow) {
                changes.push('boxShadow');
                if (indicatorType === 'none') {
                    indicatorType = 'box-shadow';
                    indicatorColor = focused.boxShadow;
                }
            }
            if (unfocused.backgroundColor !== focused.backgroundColor) {
                changes.push('backgroundColor');
            }

            results.push({
                selector: selector,
                text: text,
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute('role') || '',
                rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                unfocused_styles: unfocused,
                focused_styles: focused,
                has_change: changes.length > 0,
                changed_properties: changes,
                indicator_type: indicatorType,
                indicator_color: indicatorColor,
                // Effective background (walks ancestors; falls back to white)
                // -- the element's own backgroundColor is usually transparent.
                bg_color: effectiveBg(el),
                // Keep the raw element bg too for debugging.
                element_bg_color: focused.backgroundColor,
            });
        }
    }
    return results;
}
"""


async def _capture_focus_contrast(
    page: Page,
    capture_data: CaptureData,
    captures_dir: str,
) -> None:
    """Capture focus indicator styles for every interactive element.

    Focuses each element briefly via JS, reads the focused computed
    styles (outline, border, boxShadow), then blurs. Computes contrast
    of each focus indicator against its background.
    """
    import time
    from functions.contrast import parse_rgb, contrast_ratio_rgb
    from functions.js_helpers import EFFECTIVE_BG_JS

    # Reset first so a failure or wrong-page run never leaves stale data.
    capture_data.focus_contrast = []
    try:
        t0 = time.monotonic()
        # Inject the shared effectiveBg helper so the contrast denominator
        # is the rendered ancestor background, not the element's own
        # transparent backgroundColor.
        js = _FOCUS_CONTRAST_JS.replace(
            "// effectiveBg helper is injected from functions.js_helpers.EFFECTIVE_BG_JS",
            EFFECTIVE_BG_JS,
        )
        raw_results = await page.evaluate(js)

        if not raw_results:
            logger.info("Focus contrast: no interactive elements found")
            capture_data.focus_contrast = []
            return

        for entry in raw_results:
            indicator_color_str = entry.get("indicator_color", "")
            bg_color_str = entry.get("bg_color", "")

            indicator_rgb = parse_rgb(indicator_color_str) if indicator_color_str else None
            bg_rgb = parse_rgb(bg_color_str) if bg_color_str else None

            if indicator_rgb and bg_rgb:
                entry["contrast_ratio"] = round(contrast_ratio_rgb(indicator_rgb, bg_rgb), 2)
            else:
                entry["contrast_ratio"] = None

        capture_data.focus_contrast = raw_results
        has_change = sum(1 for r in raw_results if r.get("has_change"))
        elapsed = time.monotonic() - t0
        logger.info(
            "Focus contrast: %d elements, %d with visible change (%.1fs)",
            len(raw_results), has_change, elapsed,
        )
    except Exception:
        logger.exception("Focus contrast capture failed")


# ─── Form error capture ──────────────────────────────────────────────────────

async def _capture_form_errors(
    page: Page,
    capture_data: CaptureData,
    captures_dir: str,
) -> None:
    """Submit forms with required fields empty to capture error states.

    For each form that has required fields:
    1. Screenshot before submission
    2. Click the submit button
    3. Wait for validation
    4. Screenshot after submission
    5. Extract error elements (aria-invalid, role=alert, .error classes)
    6. Check if errors are associated with fields (aria-describedby)
    """
    import time

    try:
        t0 = time.monotonic()
        forms_dir = os.path.join(captures_dir, "form_error_captures")
        os.makedirs(forms_dir, exist_ok=True)

        forms = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('form')).map((form, i) => {
                const requiredFields = Array.from(form.querySelectorAll('[required]'));
                if (requiredFields.length === 0) return null;
                let selector = 'form';
                if (form.id) selector = '#' + form.id;
                else if (form.name) selector = 'form[name="' + form.name + '"]';
                const fields = requiredFields.map(el => {
                    let fSel = el.tagName.toLowerCase();
                    if (el.id) fSel = '#' + el.id;
                    else if (el.name) fSel = el.tagName.toLowerCase() + '[name="' + el.name + '"]';
                    return {
                        selector: fSel,
                        tag: el.tagName.toLowerCase(),
                        type: (el.getAttribute('type') || el.tagName.toLowerCase()).toLowerCase(),
                        name: el.name || '',
                        id: el.id || '',
                        ariaDescribedby: el.getAttribute('aria-describedby') || '',
                        ariaErrormessage: el.getAttribute('aria-errormessage') || '',
                        label: (() => {
                            if (el.id) {
                                const lbl = document.querySelector('label[for="' + el.id + '"]');
                                if (lbl) return lbl.textContent.trim();
                            }
                            const parent = el.closest('label');
                            if (parent) return parent.textContent.trim();
                            return '';
                        })(),
                    };
                });
                const allFields = Array.from(form.querySelectorAll('input, select, textarea')).map(el => ({
                    type: (el.getAttribute('type') || el.tagName).toLowerCase(),
                }));
                return {
                    selector,
                    index: i,
                    method: (form.method || 'GET').toUpperCase(),
                    action: form.action || '',
                    requiredFields: fields,
                    hasPassword: allFields.some(f => f.type === 'password'),
                    hasFile: allFields.some(f => f.type === 'file'),
                };
            }).filter(Boolean);
        }""")

        page_origin = urlparse(page.url).netloc
        results: list[dict] = []

        for form_info in forms:
            if form_info.get("hasPassword") or form_info.get("hasFile"):
                continue
            action = form_info.get("action", "")
            method = form_info.get("method", "GET").upper()
            if method == "POST" and action:
                parsed = urlparse(action)
                if parsed.netloc and parsed.netloc != page_origin:
                    continue

            sel = form_info["selector"]
            idx = form_info["index"]

            try:
                url_before = page.url

                before_path = os.path.join(forms_dir, f"form_{idx}_before.png")
                await page.screenshot(path=before_path, full_page=False)

                await page.evaluate("""(sel) => {
                    const form = document.querySelector(sel);
                    if (!form) return;
                    for (const el of form.querySelectorAll('[required]')) {
                        if (el.tagName === 'SELECT') continue;
                        el.value = '';
                    }
                }""", sel)

                submitted = await page.evaluate("""(sel) => {
                    const form = document.querySelector(sel);
                    if (!form) return 'no_form';
                    const btn = form.querySelector('[type="submit"], button:not([type])');
                    if (btn) {
                        btn.click();
                        return 'clicked_button';
                    }
                    try { form.requestSubmit(); return 'requestSubmit'; }
                    catch(e) {}
                    try { form.submit(); return 'submit'; }
                    catch(e) { return 'failed'; }
                }""", sel)
                await page.wait_for_timeout(1000)

                after_path = os.path.join(forms_dir, f"form_{idx}_after.png")
                await page.screenshot(path=after_path, full_page=False)

                error_state = await page.evaluate("""(sel) => {
                    const form = document.querySelector(sel);
                    if (!form) return {};
                    const invalidEls = form.querySelectorAll('[aria-invalid="true"]');
                    const alertEls = document.querySelectorAll('[role="alert"]');
                    const errorClassEls = form.querySelectorAll('.error, .invalid, .is-invalid, .field-error');
                    const errorMessages = [];
                    for (const el of alertEls) {
                        const text = (el.textContent || '').trim();
                        if (text) errorMessages.push(text);
                    }
                    for (const el of errorClassEls) {
                        const text = (el.textContent || '').trim();
                        if (text) errorMessages.push(text);
                    }
                    const fieldErrors = [];
                    for (const el of form.querySelectorAll('[required]')) {
                        let fSel = el.tagName.toLowerCase();
                        if (el.id) fSel = '#' + el.id;
                        fieldErrors.push({
                            selector: fSel,
                            ariaInvalid: el.getAttribute('aria-invalid') || '',
                            validationMessage: el.validationMessage || '',
                            ariaDescribedby: el.getAttribute('aria-describedby') || '',
                            ariaErrormessage: el.getAttribute('aria-errormessage') || '',
                            hasErrorAssociation: !!(el.getAttribute('aria-describedby') || el.getAttribute('aria-errormessage')),
                        });
                    }
                    return {
                        invalidCount: invalidEls.length,
                        alertCount: alertEls.length,
                        errorClassCount: errorClassEls.length,
                        errorMessages: errorMessages,
                        fieldErrors: fieldErrors,
                    };
                }""", sel)

                entry = {
                    "form_selector": sel,
                    "selector": sel,
                    "submit_method": submitted,
                    "before_screenshot": before_path,
                    "after_screenshot": after_path,
                    "required_fields": form_info.get("requiredFields", []),
                    "error_state": error_state,
                }
                a11y_probe = await _probe_form_error_a11y(page, sel)
                error_state = error_state or {}
                native_text = any(
                    (fe.get("validationMessage") or "").strip()
                    for fe in (error_state.get("fieldErrors") or [])
                )
                if native_text or error_state.get("errorMessages"):
                    entry["has_text_description"] = True
                elif a11y_probe:
                    entry["has_text_description"] = bool(
                        a11y_probe.get("visible_error_text"),
                    )
                # else: not measured — key stays absent so readers
                # render "unknown" instead of failing the SC.
                for key in ("has_aria_live", "has_role_alert",
                            "programmatic_association", "has_aria_invalid"):
                    if key in a11y_probe:
                        entry[key] = bool(a11y_probe[key])
                results.append(entry)

                url_after = page.url
                if url_after != url_before:
                    try:
                        await _safe_goto(page, url_before, timeout=30000)
                    except Exception:
                        logger.warning("Could not navigate back after form error capture")
                else:
                    await page.reload(timeout=30000)
                    await page.wait_for_timeout(500)

            except Exception:
                logger.debug("Form error capture failed for %s", sel)

        if results:
            capture_data.form_errors.extend(results)

        elapsed = time.monotonic() - t0
        logger.info(
            "Form error capture: %d forms tested (%.1fs)",
            len(results), elapsed,
        )
    except Exception:
        logger.exception("Form error capture failed")


# ─── Focus content detection (keyboard trigger for 1.4.13) ───────────────────

async def _capture_focus_content(
    page: Page,
    capture_data: CaptureData,
    captures_dir: str,
) -> None:
    """Test if content appears when elements receive keyboard focus.

    For each element that showed hover content, also test focus.
    Uses DOM element count diff to detect new content appearing.
    """
    import time

    try:
        t0 = time.monotonic()
        hover_results = getattr(capture_data, "hover_content", [])
        if not hover_results:
            logger.info("Focus content: no hover content entries to test")
            return

        hover_with_content = [h for h in hover_results if h.get("trigger") == "hover"]
        if not hover_with_content:
            logger.info("Focus content: no hover-triggered content to retest with focus")
            return

        focus_dir = os.path.join(captures_dir, "focus_content")
        os.makedirs(focus_dir, exist_ok=True)

        focus_results: list[dict] = []
        for idx, hover_entry in enumerate(hover_with_content):
            sel = hover_entry.get("selector", "")
            if not sel:
                continue

            try:
                el_handle = await page.query_selector(sel)
                if not el_handle:
                    continue
                if not await el_handle.is_visible():
                    continue

                try:
                    await el_handle.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    continue
                await page.wait_for_timeout(200)

                before_path = os.path.join(focus_dir, f"focus_before_{idx}.png")
                await page.screenshot(path=before_path, full_page=False)
                before_count = await page.evaluate("document.querySelectorAll('*').length")

                await page.evaluate("(el) => el.focus({preventScroll: true})", el_handle)
                await page.wait_for_timeout(1500)

                after_count = await page.evaluate("document.querySelectorAll('*').length")
                new_elements = after_count - before_count

                focus_content_info = await page.evaluate("""() => {
                    const tooltips = document.querySelectorAll(
                        '[role="tooltip"], [class*="tooltip"], [class*="popover"], ' +
                        '[class*="dropdown"][class*="show"], [class*="dropdown"][class*="open"]'
                    );
                    const visible = [];
                    for (const t of tooltips) {
                        const cs = window.getComputedStyle(t);
                        if (cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0') {
                            visible.push({
                                tag: t.tagName.toLowerCase(),
                                role: t.getAttribute('role') || '',
                                text: (t.textContent || '').trim(),
                            });
                        }
                    }
                    return visible;
                }""")

                content_appeared = new_elements > 0 or len(focus_content_info) > 0

                is_dismissible = False
                if content_appeared:
                    after_path = os.path.join(focus_dir, f"focus_after_{idx}.png")
                    await page.screenshot(path=after_path, full_page=False)

                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(500)
                    dismiss_count = await page.evaluate("document.querySelectorAll('*').length")
                    is_dismissible = dismiss_count <= before_count

                    focus_results.append({
                        "selector": sel,
                        "text": hover_entry.get("text", ""),
                        "trigger": "focus",
                        "content_appeared": True,
                        "new_elements_count": new_elements,
                        "focus_content": focus_content_info,
                        "dismissible": is_dismissible,
                        "before_screenshot": before_path,
                        "after_screenshot": after_path,
                        "corresponding_hover_index": idx,
                    })
                else:
                    focus_results.append({
                        "selector": sel,
                        "text": hover_entry.get("text", ""),
                        "trigger": "focus",
                        "content_appeared": False,
                        "new_elements_count": 0,
                        "focus_content": [],
                        "dismissible": None,
                        "before_screenshot": before_path,
                        "after_screenshot": "",
                        "corresponding_hover_index": idx,
                    })

                await page.evaluate("(el) => el.blur()", el_handle)
                await page.wait_for_timeout(200)

            except Exception:
                logger.debug("Focus content check failed for %s", sel)

        capture_data.hover_content.extend(focus_results)

        appeared = sum(1 for r in focus_results if r.get("content_appeared"))
        elapsed = time.monotonic() - t0
        logger.info(
            "Focus content: %d elements tested, %d showed content on focus (%.1fs)",
            len(focus_results), appeared, elapsed,
        )
    except Exception:
        logger.exception("Focus content detection failed")


# ─── Modal open/trap/close roundtrip ────────────────────────────────────────

_MODAL_TRIGGER_INVENTORY_JS = r"""
() => {
""" + GET_SELECTOR_JS + r"""
    // Find every element that looks like a modal trigger. We cast a
    // wide net -- the cost of testing a false positive (element that
    // does nothing on Enter) is a recorded "did not open" result,
    // while missing a real trigger leaves the modal entirely untested.
    function isVisible(el) {
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        if (el.offsetParent === null && cs.position !== 'fixed') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }

    const triggerSelectors = [
        '[aria-haspopup="dialog"]',
        '[aria-haspopup="true"]',
        '[data-bs-toggle="modal"]',
        '[data-toggle="modal"]',
        '[data-bs-target]',
        '[data-target][data-toggle]',
        'button[aria-controls]',
    ];
    const triggers = [];
    const seen = new Set();
    for (const s of triggerSelectors) {
        for (const el of document.querySelectorAll(s)) {
            if (seen.has(el)) continue;
            seen.add(el);
            if (!isVisible(el)) continue;
            // If the trigger points at a specific element, resolve
            // that so the test can watch it change visibility.
            const controlsId = el.getAttribute('aria-controls')
                || (el.getAttribute('data-bs-target') || '').replace(/^#/, '')
                || (el.getAttribute('data-target') || '').replace(/^#/, '');
            let targetSel = '';
            if (controlsId) {
                const target = document.getElementById(controlsId);
                if (target) {
                    targetSel = '#' + controlsId;
                    // Only accept if target looks like a dialog/modal.
                    const role = target.getAttribute('role') || '';
                    const tagName = target.tagName.toLowerCase();
                    const classAttr = target.className || '';
                    const isDialog = role === 'dialog' || role === 'alertdialog'
                        || tagName === 'dialog'
                        || /\b(modal|dialog|popup|overlay)\b/i.test(classAttr);
                    if (!isDialog) continue;
                }
            }
            triggers.push({
                selector: getSelector(el),
                text: (el.textContent || '').trim(),
                aria_haspopup: el.getAttribute('aria-haspopup') || '',
                aria_controls: controlsId || '',
                target_selector: targetSel,
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute('role') || '',
            });
        }
    }
    return triggers;
}
"""


async def _capture_modal_interactions(
    page: Page,
    capture_data: CaptureData,
) -> None:
    """Test the full modal keyboard roundtrip for every trigger on the
    page.

    For each candidate trigger:

      1. Focus the trigger and screenshot baseline visibility of the
         target modal (if the trigger names one via ``aria-controls``
         / ``data-bs-target``).
      2. Press Enter, wait 500ms. Did any ``role="dialog"`` /
         ``<dialog>`` element become visible? Record ``opened_by_enter``.
      3. If Enter didn't open it, try Space. Record ``opened_by_space``.
      4. If the modal is open: press Tab up to 20 times, recording
         which element has focus each time. If every tabstop lands
         inside the modal container, ``focus_trap_ok=True``.
      5. Press Escape. Wait 500ms. Modal hidden? ``escape_closes=True``.
      6. Read ``document.activeElement`` -- is it back on the trigger?
         ``focus_returned_to_trigger=True``.

    The collected data feeds SC 2.1.1 (trigger keyboard-operable?),
    SC 2.1.2 (Escape closes -- otherwise the modal is a keyboard
    trap), SC 2.4.3 (focus returns to origin), and SC 4.1.2 (role
    announced).

    Each trigger is tested with a 30s cap; the whole function is
    wrapped in a per-test timeout by the orchestrator.
    """
    import time

    try:
        t0 = time.monotonic()
        triggers = await page.evaluate(_MODAL_TRIGGER_INVENTORY_JS)
        if not triggers:
            logger.info("Modal interactions: no modal triggers found")
            capture_data.modal_interactions = []
            return

        logger.info(
            "Modal interactions: testing %d trigger(s)", len(triggers),
        )
        results: list[dict] = []

        for trigger in triggers:
            trig_sel = trigger.get("selector", "")
            trig_text = trigger.get("text", "")
            target_sel = trigger.get("target_selector", "")
            if not trig_sel:
                continue

            result = {
                "trigger_selector": trig_sel,
                "trigger_text": trig_text,
                "target_selector": target_sel,
                "aria_haspopup": trigger.get("aria_haspopup", ""),
                "opened_by_enter": False,
                "opened_by_space": False,
                "modal_selector_found": "",
                "focus_trap_ok": None,
                "tabstops_inside_modal": 0,
                "tabstops_outside_modal": 0,
                "tabstops_walk": [],
                "escape_closes": None,
                "focus_returned_to_trigger": None,
                "error": None,
            }

            try:
                # Focus the trigger fresh each iteration so state is
                # deterministic between triggers.
                trig_handle = await page.query_selector(trig_sel)
                if trig_handle is None:
                    result["error"] = "trigger selector not found"
                    results.append(result)
                    continue
                if not await trig_handle.is_visible():
                    result["error"] = "trigger not visible"
                    results.append(result)
                    continue
                try:
                    await trig_handle.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass  # best-effort — element may be off-screen or unscrollable; focus below will retry
                await page.evaluate(
                    "(el) => el.focus({preventScroll: true})", trig_handle,
                )
                await page.wait_for_timeout(250)

                # Snapshot all open dialogs before pressing anything so
                # we can diff post-press.
                def _visible_dialogs_js() -> str:
                    return r"""() => {
                        const sels = [
                            '[role="dialog"]', '[role="alertdialog"]',
                            'dialog[open]', '[aria-modal="true"]',
                        ];
                        const seen = new Set();
                        const out = [];
                        for (const s of sels) {
                            for (const el of document.querySelectorAll(s)) {
                                if (seen.has(el)) continue;
                                seen.add(el);
                                const cs = getComputedStyle(el);
                                if (cs.display === 'none') continue;
                                if (cs.visibility === 'hidden') continue;
                                const r = el.getBoundingClientRect();
                                if (r.width <= 0 || r.height <= 0) continue;
                                let sel = el.tagName.toLowerCase();
                                if (el.id) sel = '#' + el.id;
                                out.push({
                                    selector: sel,
                                    role: el.getAttribute('role') || '',
                                    aria_modal: el.getAttribute('aria-modal') || '',
                                });
                            }
                        }
                        return out;
                    }"""

                dialogs_before = await page.evaluate(_visible_dialogs_js())
                before_sels = {d.get("selector", "") for d in dialogs_before}

                # Try Enter first.
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(500)
                dialogs_after_enter = await page.evaluate(_visible_dialogs_js())
                new_after_enter = [
                    d for d in dialogs_after_enter
                    if d.get("selector", "") not in before_sels
                ]
                if new_after_enter:
                    result["opened_by_enter"] = True
                    modal = new_after_enter[0]
                    result["modal_selector_found"] = modal.get("selector", "")
                else:
                    # Refocus trigger and try Space.
                    try:
                        await page.evaluate(
                            "(el) => el.focus({preventScroll: true})", trig_handle,
                        )
                        await page.wait_for_timeout(150)
                    except Exception:
                        pass  # best-effort — refocus may fail if trigger detached; Space press below proceeds anyway
                    await page.keyboard.press("Space")
                    await page.wait_for_timeout(500)
                    dialogs_after_space = await page.evaluate(_visible_dialogs_js())
                    new_after_space = [
                        d for d in dialogs_after_space
                        if d.get("selector", "") not in before_sels
                    ]
                    if new_after_space:
                        result["opened_by_space"] = True
                        modal = new_after_space[0]
                        result["modal_selector_found"] = modal.get("selector", "")

                # If the modal is open, test focus trap + Escape.
                modal_sel = result["modal_selector_found"]
                if modal_sel:
                    # Walk Tab 20 times; at each stop, is activeElement
                    # inside the modal?
                    inside = 0
                    outside = 0
                    walk: list[dict] = []
                    for _ in range(20):
                        await page.keyboard.press("Tab")
                        await page.wait_for_timeout(120)
                        focus_info = await page.evaluate(r"""(modalSel) => {
                            const modal = document.querySelector(modalSel);
                            const el = document.activeElement;
                            if (!el || !modal) return null;
                            const isInside = modal === el || modal.contains(el);
                            let sel = el.tagName ? el.tagName.toLowerCase() : '?';
                            if (el.id) sel = '#' + el.id;
                            return {
                                focused: sel,
                                inside_modal: isInside,
                                text: (el.textContent || '').trim(),
                            };
                        }""", modal_sel)
                        if focus_info:
                            walk.append(focus_info)
                            if focus_info.get("inside_modal"):
                                inside += 1
                            else:
                                outside += 1
                    result["tabstops_inside_modal"] = inside
                    result["tabstops_outside_modal"] = outside
                    result["tabstops_walk"] = walk
                    # Focus-trap passes when ALL tabstops stayed inside
                    # the modal (standard WAI-ARIA dialog pattern).
                    result["focus_trap_ok"] = outside == 0 and inside > 0

                    # Close with Escape.
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(500)
                    dialogs_after_esc = await page.evaluate(_visible_dialogs_js())
                    still_open = any(
                        d.get("selector", "") == modal_sel
                        for d in dialogs_after_esc
                    )
                    result["escape_closes"] = not still_open

                    # After Escape, is focus back on the trigger?
                    focus_back = await page.evaluate(r"""(trigSel) => {
                        const el = document.activeElement;
                        if (!el) return false;
                        if (el.id && trigSel === '#' + el.id) return true;
                        try {
                            const target = document.querySelector(trigSel);
                            return el === target;
                        } catch (_) {
                            return false;
                        }
                    }""", trig_sel)
                    result["focus_returned_to_trigger"] = bool(focus_back)

                    # If Escape did NOT close the modal, force-close it
                    # with a click on the body + blur so the next
                    # trigger test starts clean.
                    if still_open:
                        try:
                            await page.evaluate(r"""(sel) => {
                                const el = document.querySelector(sel);
                                if (el) {
                                    el.style.display = 'none';
                                    el.setAttribute('hidden', '');
                                }
                                if (document.activeElement) {
                                    document.activeElement.blur();
                                }
                            }""", modal_sel)
                        except Exception:
                            pass  # cleanup — best-effort dismiss of modal, page state may be unrecoverable
                        await page.wait_for_timeout(200)

            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"

            results.append(result)

        capture_data.modal_interactions = results

        n_total = len(results)
        n_opened = sum(
            1 for r in results
            if r.get("opened_by_enter") or r.get("opened_by_space")
        )
        n_trap_ok = sum(1 for r in results if r.get("focus_trap_ok"))
        n_esc_ok = sum(1 for r in results if r.get("escape_closes"))
        elapsed = time.monotonic() - t0
        logger.info(
            "Modal interactions: %d triggers, %d opened, %d with focus trap, "
            "%d closed via Escape (%.1fs)",
            n_total, n_opened, n_trap_ok, n_esc_ok, elapsed,
        )
    except Exception:
        logger.exception("Modal interactions test failed")


# ─── Generic keyboard roundtrip probe ────────────────────────────────────────
#
# Behavior-verifies every probable trigger on the page. Focus →
# snapshot → Enter → snapshot. If something changed (DOM mutation,
# new visible content, focus moved into a new container), continue to
# verify Escape closes, Tab inside stays bounded (trap or natural
# next stop), focus returns to the trigger after dismiss, and
# Shift+Tab from inside exits cleanly.
#
# Distinct from _capture_modal_interactions (strict aria-haspopup /
# aria-controls→dialog inventory) and from _capture_widget_keyboard
# (per-ARIA-widget-type tests). Catches custom JS dropdowns, drawers,
# popovers, search overlays, and any other non-ARIA-tagged trigger
# that opens dismissible content.
#
# Time budget: ~3-10s per candidate × ~30-50 candidates on a typical
# page. Skips <a> elements that would navigate (full URL href). Re-
# establishes baseline state between probes by pressing Escape twice
# and clicking the page body.

_KB_ROUNDTRIP_INVENTORY_JS = r"""
() => {
""" + GET_SELECTOR_JS + r"""
    function isVisible(el) {
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        if (parseFloat(cs.opacity) === 0) return false;
        if (el.offsetParent === null && cs.position !== 'fixed') return false;
        const r = el.getBoundingClientRect();
        return r.width > 1 && r.height > 1;
    }

    // Candidate triggers: things that can accept Enter and might open
    // something. Skip <a> with full-URL href (would navigate away).
    const candidates = [];
    const seen = new WeakSet();
    const sels = [
        'button:not([disabled])',
        '[role="button"]',
        'summary',
        '[role="menuitem"]',
        '[role="tab"]',
        'a[aria-haspopup]',
        'a[aria-controls]',
        'a[aria-expanded]',
        '[role="combobox"]',
    ];
    for (const sel of sels) {
        for (const el of document.querySelectorAll(sel)) {
            if (seen.has(el)) continue;
            seen.add(el);
            if (!isVisible(el)) continue;
            if (el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true') continue;

            const tag = el.tagName.toLowerCase();
            // For <a> we also require a same-page hash href OR an
            // explicit aria-controls/haspopup/expanded — anything
            // else might navigate. Other tags accepted as-is.
            if (tag === 'a') {
                const href = el.getAttribute('href') || '';
                const hasAria = el.hasAttribute('aria-controls')
                    || el.hasAttribute('aria-haspopup')
                    || el.hasAttribute('aria-expanded');
                const isHash = href.startsWith('#');
                const isNoHref = href === '';
                if (!hasAria && !isHash && !isNoHref) continue;
            }

            const r = el.getBoundingClientRect();
            const text = (el.textContent || '').trim();
            const ariaLabel = el.getAttribute('aria-label') || '';
            const ariaControls = el.getAttribute('aria-controls') || '';
            const ariaExpanded = el.getAttribute('aria-expanded') || '';
            const ariaHaspopup = el.getAttribute('aria-haspopup') || '';

            candidates.push({
                selector: getSelector(el),
                tag: tag,
                role: el.getAttribute('role') || '',
                href: el.getAttribute('href') || '',
                text: text,
                accessible_name: ariaLabel || text,
                aria_controls: ariaControls,
                aria_expanded: ariaExpanded,
                aria_haspopup: ariaHaspopup,
                rect: { x: r.x, y: r.y, width: r.width, height: r.height },
            });
        }
    }
    return candidates;
}
"""


_KB_DOM_SIGNATURE_JS = r"""
() => {
""" + GET_SELECTOR_JS + r"""
    // Lightweight DOM signature so we can detect whether Enter caused
    // a meaningful change. Counts visible focusables, visible-text
    // length, open-popover/dialog roles, aria-expanded=true count.
    function isVisible(el) {
        if (!el || el.nodeType !== 1) return false;
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        if (parseFloat(cs.opacity) === 0) return false;
        const r = el.getBoundingClientRect();
        return r.width > 1 && r.height > 1;
    }
    let visibleFocusables = 0;
    let visibleTextLen = 0;
    let openDialogs = 0;
    let expandedCount = 0;
    const focSel = 'a[href], button, input, select, textarea, [tabindex], [role="button"], [role="link"]';
    for (const el of document.querySelectorAll(focSel)) {
        if (isVisible(el)) visibleFocusables += 1;
    }
    for (const el of document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog[open], [role="menu"], [role="listbox"], [role="tooltip"]')) {
        if (isVisible(el)) openDialogs += 1;
    }
    for (const el of document.querySelectorAll('[aria-expanded="true"]')) {
        if (isVisible(el)) expandedCount += 1;
    }
    visibleTextLen = (document.body && document.body.innerText || '').length;
    const active = document.activeElement;
    let activeSel = '';
    if (active && active !== document.body) {
        activeSel = getSelector(active);
    }
    return {
        focusables: visibleFocusables,
        text_len: visibleTextLen,
        open_dialogs: openDialogs,
        expanded: expandedCount,
        active_selector: activeSel,
        scroll_y: window.scrollY,
        url: location.href,
    };
}
"""


def _signatures_differ(before: dict, after: dict) -> bool:
    """Return True when the two DOM signatures differ enough to count
    as 'something opened'. Tolerates micro-noise: 1-element focusable
    delta, <50-char text length jitter from animated counters."""
    if not before or not after:
        return False
    if before.get("url") != after.get("url"):
        return True  # navigation
    if abs(int(after.get("focusables", 0)) - int(before.get("focusables", 0))) >= 2:
        return True
    if int(after.get("open_dialogs", 0)) > int(before.get("open_dialogs", 0)):
        return True
    if int(after.get("expanded", 0)) > int(before.get("expanded", 0)):
        return True
    if abs(int(after.get("text_len", 0)) - int(before.get("text_len", 0))) > 50:
        return True
    if before.get("active_selector") != after.get("active_selector"):
        return True
    return False


async def _capture_keyboard_roundtrip(
    page: Page,
    capture_data: CaptureData,
) -> None:
    """Behavior-verify Enter / Escape / Tab / Shift+Tab on every
    probable trigger.

    Saves to ``capture_data.keyboard_roundtrip_results``.

    Per candidate trigger:

      1. Press Escape twice + click body to clear any prior open state.
      2. Focus the trigger (Playwright ``element.focus()``).
      3. Capture DOM signature — visible focusables, expanded count,
         open dialogs, active element.
      4. Press Enter, wait 350ms.
      5. Recapture signature. If unchanged, try Space, recapture.
      6. If signature changed (something opened):
         a. Press Tab up to 8 times. Record where focus lands. If it
            cycles back to the trigger or stays inside the opened
            container, record ``tab_stays_inside=True``.
         b. Press Escape, wait 350ms. Re-snapshot. If signature
            collapsed back to baseline, ``escape_closes=True``.
         c. Inspect ``document.activeElement`` — if it equals the
            trigger, ``focus_returns_to_trigger=True``.
         d. Re-open with Enter and try Shift+Tab to see whether the
            trigger is reachable backward from inside.
      7. Always: press Escape twice and click body to reset state
         before moving to the next candidate.

    Each candidate is wrapped in a 30s timeout so a single misbehaving
    element cannot stall the whole probe.
    """
    import time

    try:
        t0 = time.monotonic()
        candidates = await page.evaluate(_KB_ROUNDTRIP_INVENTORY_JS)
        if not candidates:
            logger.info("Keyboard roundtrip: no probable triggers found")
            capture_data.keyboard_roundtrip_results = []
            return

        logger.info(
            "Keyboard roundtrip: %d probable trigger(s) to probe",
            len(candidates),
        )

        async def reset_state() -> None:
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.25)
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.25)
                await page.locator("body").click(position={"x": 1, "y": 1}, force=True, timeout=8000)
                await asyncio.sleep(0.25)
            except Exception:
                # Best-effort — if click fails the next Escape sequence
                # usually still recovers.
                pass

        results: list[dict] = []

        for i, cand in enumerate(candidates):
            entry: dict = {
                "selector": cand.get("selector", ""),
                "tag": cand.get("tag", ""),
                "role": cand.get("role", ""),
                "text": cand.get("text", ""),
                "accessible_name": cand.get("accessible_name", ""),
                "aria_controls": cand.get("aria_controls", ""),
                "aria_haspopup": cand.get("aria_haspopup", ""),
                "aria_expanded_before": cand.get("aria_expanded", ""),
                "rect": cand.get("rect", {}),
                "opens_on_enter": False,
                "opens_on_space": False,
                "tab_stays_inside": None,
                "tab_steps_inside": 0,
                "escape_closes": None,
                "focus_returns_to_trigger": None,
                "shift_tab_exits_cleanly": None,
                "errors": [],
            }
            try:
                await asyncio.wait_for(
                    _probe_one_keyboard_roundtrip(page, cand, entry, reset_state),
                    timeout=90.0,
                )
            except asyncio.TimeoutError:
                entry["errors"].append("timeout >90s — probe abandoned")
                logger.warning(
                    "Keyboard roundtrip [%d/%d] %s: timeout",
                    i + 1, len(candidates), cand.get("selector", "?"),
                )
                # Aggressive recovery — page might be in a stuck state
                try:
                    await reset_state()
                except Exception:
                    pass  # cleanup — best-effort reset after timeout, page state may be unrecoverable
            except Exception as exc:
                entry["errors"].append(f"{type(exc).__name__}: {exc}")
                logger.warning(
                    "Keyboard roundtrip [%d/%d] %s: %s",
                    i + 1, len(candidates), cand.get("selector", "?"), exc,
                )

            results.append(entry)

        capture_data.keyboard_roundtrip_results = results

        n_open = sum(1 for r in results if r["opens_on_enter"] or r["opens_on_space"])
        n_no_esc = sum(
            1 for r in results
            if (r["opens_on_enter"] or r["opens_on_space"]) and r["escape_closes"] is False
        )
        n_no_return = sum(
            1 for r in results
            if (r["opens_on_enter"] or r["opens_on_space"]) and r["focus_returns_to_trigger"] is False
        )
        n_trap = sum(
            1 for r in results
            if r["tab_stays_inside"] is True and r["escape_closes"] is False
        )
        elapsed = time.monotonic() - t0
        logger.info(
            "Keyboard roundtrip: %d candidates probed, %d opened, "
            "%d Escape-failed, %d focus-return-failed, %d trap-suspect (%.1fs)",
            len(results), n_open, n_no_esc, n_no_return, n_trap, elapsed,
        )
    except Exception:
        logger.exception("Keyboard roundtrip probe failed")


_FOCUS_WITHIN_CONTAINER_JS = r"""
(sel) => {
    let container = null;
    try { container = document.querySelector(sel); } catch (e) { return null; }
    if (!container) return null;
    let a = document.activeElement;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) {
        a = a.shadowRoot.activeElement;
    }
    if (!a || a === document.body || a === document.documentElement) return false;
    return container === a || container.contains(a);
}
"""


async def _focus_within_container(page: Page, container_selector: str) -> bool | None:
    """DOM-truth containment: is the focused element inside the container?

    Returns True/False from ``container.contains(activeElement)``, or
    None when the container selector no longer resolves (DOM mutated,
    invalid selector) so the caller can fall back to structural
    selector matching (``functions.selectors.selector_within``).
    Focus on <body>/<html>/nothing counts as escaped, never inside.
    """
    if not container_selector:
        return None
    try:
        return await page.evaluate(_FOCUS_WITHIN_CONTAINER_JS, container_selector)
    except Exception:
        logger.warning(
            "focus-within-container probe failed for %s",
            container_selector, exc_info=True,
        )
        return None


async def _probe_one_keyboard_roundtrip(
    page: Page,
    cand: dict,
    entry: dict,
    reset_state,
) -> None:
    """Run the per-trigger probe for one candidate. Mutates ``entry``
    in place. Raises on Playwright failure so the caller can record
    the error and move on."""
    sel = cand.get("selector", "")
    if not sel:
        entry["errors"].append("missing_selector")
        return

    # Reset to baseline before this probe
    await reset_state()

    # Resolve the candidate via Playwright. If the selector doesn't
    # match anymore (DOM mutated), record and move on.
    try:
        loc = page.locator(sel).first
        await loc.wait_for(state="attached", timeout=8000)
    except Exception as exc:
        entry["errors"].append(f"selector_not_found: {exc}")
        return

    # Focus the trigger
    try:
        await loc.scroll_into_view_if_needed(timeout=8000)
        await loc.focus(timeout=8000)
        await asyncio.sleep(0.2)
    except Exception as exc:
        entry["errors"].append(f"focus_failed: {exc}")
        return

    sig_before = await page.evaluate(_KB_DOM_SIGNATURE_JS)

    # Press Enter
    try:
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.6)
    except Exception as exc:
        entry["errors"].append(f"enter_press_failed: {exc}")
        return

    sig_after_enter = await page.evaluate(_KB_DOM_SIGNATURE_JS)

    # Navigation guard — if URL changed, skip the rest of the probe
    # to avoid losing the page. The reset_state at top of next probe
    # would still try to recover; we go back here.
    if sig_after_enter.get("url") != sig_before.get("url"):
        entry["opens_on_enter"] = True
        entry["errors"].append("navigated_away_on_enter — going back")
        try:
            await page.go_back(timeout=15000, wait_until="domcontentloaded")
        except Exception:
            pass  # cleanup — best-effort go_back after Enter navigation, page state may be unrecoverable
        return

    opened_via_enter = _signatures_differ(sig_before, sig_after_enter)
    entry["opens_on_enter"] = opened_via_enter

    if not opened_via_enter:
        # Try Space (some buttons only respond to Space)
        try:
            await loc.focus(timeout=8000)
            await asyncio.sleep(0.1)
            await page.keyboard.press("Space")
            await asyncio.sleep(0.6)
        except Exception:
            pass  # best-effort — element may be detached/non-focusable; Space probe is opportunistic
        sig_after_space = await page.evaluate(_KB_DOM_SIGNATURE_JS)
        if sig_after_space.get("url") != sig_before.get("url"):
            entry["opens_on_space"] = True
            entry["errors"].append("navigated_away_on_space — going back")
            try:
                await page.go_back(timeout=15000, wait_until="domcontentloaded")
            except Exception:
                pass  # cleanup — best-effort go_back after Space navigation, page state may be unrecoverable
            return
        opened_via_space = _signatures_differ(sig_before, sig_after_space)
        entry["opens_on_space"] = opened_via_space
        if not opened_via_space:
            return  # Nothing opened; probe complete
        sig_after_open = sig_after_space
    else:
        sig_after_open = sig_after_enter

    # Something opened. Identify the open container if possible.
    open_target = await page.evaluate(
        "() => {" + GET_SELECTOR_JS + r"""
            function isVisible(el) {
                if (!el) return false;
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                const r = el.getBoundingClientRect();
                return r.width > 1 && r.height > 1;
            }
            const cands = document.querySelectorAll(
                '[role="dialog"], [role="alertdialog"], dialog[open], '
                + '[role="menu"], [role="listbox"], [role="tooltip"], '
                + '[aria-expanded="true"]'
            );
            for (const el of cands) {
                if (isVisible(el)) return getSelector(el);
            }
            return '';
        }"""
    )
    entry["opened_target_selector"] = open_target

    # Tab inside up to 8 steps; record whether focus stays in opened area.
    # If no opened container could be identified (open_target=='' — e.g.,
    # toggle buttons that just flip their own text without revealing new
    # content), there is no container to be trapped IN. Mark
    # tab_stays_inside=None so the SC 2.1.2 finding extractor skips
    # this candidate. Earlier behaviour: treated empty open_target as
    # "perpetually inside" and produced HIGH 2.1.2 false positives on
    # a university site's #pauseHeroVid (toggles the video's play state without
    # opening any UI) and the inline header search button.
    if not open_target:
        entry["tab_steps_inside"] = 0
        entry["tab_stays_inside"] = None
    else:
        tab_inside_count = 0
        seen_inside = True
        for _step in range(8):
            try:
                await page.keyboard.press("Tab")
                await asyncio.sleep(0.2)
                sig_step = await page.evaluate(_KB_DOM_SIGNATURE_JS)
                active = sig_step.get("active_selector", "") or ""
                # DOM-truth containment first (container.contains(active)).
                # Falls back to structural selector matching when the
                # container selector no longer resolves. Either way an
                # empty/body active selector counts as ESCAPED — the old
                # raw-substring test ('' in open_target) counted focus on
                # <body> as "inside" and let prefix-sharing selectors
                # (#nav vs #nav2) false-match.
                inside = await _focus_within_container(page, open_target)
                if inside is None:
                    inside = selector_within(active, open_target)
                if not inside:
                    # Focus moved out of the opened thing
                    seen_inside = False
                    break
                tab_inside_count += 1
            except Exception:
                break
        entry["tab_steps_inside"] = tab_inside_count
        entry["tab_stays_inside"] = bool(seen_inside and tab_inside_count > 0)

    # Press Escape, see if open container closes
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.6)
    except Exception as exc:
        entry["errors"].append(f"escape_press_failed: {exc}")

    sig_after_esc = await page.evaluate(_KB_DOM_SIGNATURE_JS)
    closed = (
        int(sig_after_esc.get("open_dialogs", 0)) <= int(sig_before.get("open_dialogs", 0))
        and int(sig_after_esc.get("expanded", 0)) <= int(sig_before.get("expanded", 0))
        and not _signatures_differ(sig_before, sig_after_esc)
    )
    entry["escape_closes"] = closed

    # Where did focus land after Escape?
    active_after = sig_after_esc.get("active_selector", "") or ""
    # Build trigger's expected active selector. Compare loosely — id-
    # based equality wins, otherwise tag-class prefix.
    trigger_id_match = sel.split("#", 1)
    if len(trigger_id_match) == 2:
        trigger_id_token = "#" + trigger_id_match[1].split(" ")[0]
        focus_returned = trigger_id_token in active_after
    else:
        focus_returned = False
        # Fall-back: check via Playwright that the trigger element is
        # the active one
        try:
            is_focused = await loc.evaluate("(el) => el === document.activeElement")
            focus_returned = bool(is_focused)
        except Exception:
            pass  # default to focus_returned=False if probe fails (element detached/cross-origin)
    entry["focus_returns_to_trigger"] = focus_returned

    # Re-open and verify Shift+Tab from inside reaches the trigger
    if closed:
        try:
            await loc.focus(timeout=8000)
            await asyncio.sleep(0.15)
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.5)
            # Tab once into the opened thing, then Shift+Tab back
            await page.keyboard.press("Tab")
            await asyncio.sleep(0.2)
            await page.keyboard.press("Shift+Tab")
            await asyncio.sleep(0.3)
            try:
                back_at_trigger = await loc.evaluate("(el) => el === document.activeElement")
            except Exception:
                back_at_trigger = False
            entry["shift_tab_exits_cleanly"] = bool(back_at_trigger)
            # Close it again
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.4)
        except Exception as exc:
            entry["errors"].append(f"shift_tab_recheck_failed: {exc}")


# ─── Widget keyboard testing ─────────────────────────────────────────────────

_WIDGET_INVENTORY_JS = r"""
() => {
    const widgets = [];

    // Canonical getSelector — same algorithm as the inventory + v2.
    // Fixes the 22-collisions-on-"a" bug in focus_contrast that made
    // the AI prompt's per-element focus-indicator lines indistinguishable.
    function makeSelector(el) {
        if (el.id) return '#' + CSS.escape(el.id);
        const parts = [];
        let current = el;
        while (current && current !== document.body && current !== document.documentElement) {
            let seg = current.tagName.toLowerCase();
            if (current.id) {
                parts.unshift('#' + CSS.escape(current.id));
                break;
            }
            if (current.parentElement) {
                const sibs = Array.from(current.parentElement.children)
                    .filter(s => s.tagName === current.tagName);
                if (sibs.length > 1) {
                    seg += ':nth-of-type(' + (sibs.indexOf(current) + 1) + ')';
                }
            }
            parts.unshift(seg);
            current = current.parentElement;
            if (parts.length >= 4) break;
        }
        const selector = parts.join(' > ');
        try {
            if (document.querySelectorAll(selector).length === 1) return selector;
            const fullParts = [];
            let node = el;
            while (node && node !== document.body && node !== document.documentElement) {
                let s = node.tagName.toLowerCase();
                if (node.id) { fullParts.unshift('#' + CSS.escape(node.id)); break; }
                if (node.parentElement) {
                    const sibs = Array.from(node.parentElement.children)
                        .filter(c => c.tagName === node.tagName);
                    if (sibs.length > 1) {
                        s += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
                    }
                }
                fullParts.unshift(s);
                node = node.parentElement;
            }
            return fullParts.join(' > ');
        } catch (e) { return selector; }
    }

    // Tabs
    for (const tablist of document.querySelectorAll('[role="tablist"]')) {
        const tabs = tablist.querySelectorAll('[role="tab"]');
        if (tabs.length > 1) {
            widgets.push({
                type: 'tablist',
                selector: makeSelector(tablist),
                items: Array.from(tabs).map(t => ({
                    selector: makeSelector(t),
                    text: (t.textContent || '').trim(),
                    ariaSelected: t.getAttribute('aria-selected') || '',
                })),
                keys: ['ArrowRight', 'ArrowLeft'],
                stateAttr: 'aria-selected',
            });
        }
    }

    // Accordions (buttons with aria-expanded)
    const accordionBtns = document.querySelectorAll(
        '[role="button"][aria-expanded], button[aria-expanded], ' +
        '[role="heading"] button[aria-expanded]'
    );
    for (const btn of accordionBtns) {
        widgets.push({
            type: 'accordion',
            selector: makeSelector(btn),
            items: [{
                selector: makeSelector(btn),
                text: (btn.textContent || '').trim(),
                ariaExpanded: btn.getAttribute('aria-expanded') || '',
            }],
            keys: ['Enter', 'Space'],
            stateAttr: 'aria-expanded',
        });
    }

    // Menus
    for (const menu of document.querySelectorAll('[role="menu"], [role="menubar"]')) {
        const items = menu.querySelectorAll('[role="menuitem"]');
        if (items.length > 0) {
            widgets.push({
                type: 'menu',
                selector: makeSelector(menu),
                items: Array.from(items).map(mi => ({
                    selector: makeSelector(mi),
                    text: (mi.textContent || '').trim(),
                })),
                keys: ['ArrowDown', 'ArrowUp', 'Escape'],
                stateAttr: 'aria-expanded',
            });
        }
    }

    // Sliders
    for (const slider of document.querySelectorAll('[role="slider"]')) {
        widgets.push({
            type: 'slider',
            selector: makeSelector(slider),
            items: [{
                selector: makeSelector(slider),
                text: (slider.textContent || '').trim(),
                ariaValuenow: slider.getAttribute('aria-valuenow') || '',
                ariaValuemin: slider.getAttribute('aria-valuemin') || '',
                ariaValuemax: slider.getAttribute('aria-valuemax') || '',
            }],
            keys: ['ArrowRight', 'ArrowLeft', 'ArrowUp', 'ArrowDown'],
            stateAttr: 'aria-valuenow',
        });
    }

    // Comboboxes
    for (const combo of document.querySelectorAll('[role="combobox"]')) {
        widgets.push({
            type: 'combobox',
            selector: makeSelector(combo),
            items: [{
                selector: makeSelector(combo),
                text: (combo.textContent || '').trim(),
                ariaExpanded: combo.getAttribute('aria-expanded') || '',
            }],
            keys: ['ArrowDown', 'Escape'],
            stateAttr: 'aria-expanded',
        });
    }

    // Tree views
    for (const tree of document.querySelectorAll('[role="tree"]')) {
        const items = tree.querySelectorAll('[role="treeitem"]');
        if (items.length > 0) {
            widgets.push({
                type: 'tree',
                selector: makeSelector(tree),
                items: Array.from(items).map(ti => ({
                    selector: makeSelector(ti),
                    text: (ti.textContent || '').trim(),
                    ariaExpanded: ti.getAttribute('aria-expanded') || '',
                    ariaSelected: ti.getAttribute('aria-selected') || '',
                })),
                keys: ['ArrowDown', 'ArrowUp', 'ArrowRight', 'ArrowLeft'],
                stateAttr: 'aria-expanded',
            });
        }
    }

    return widgets;
}
"""


async def _ai_discover_widgets(
    page: Page,
    capture_data: CaptureData,
    known_widgets: list[dict],
    captures_dir: str,
) -> list[dict]:
    """Ask the vision LLM to find composite widgets missed by the JS inventory.

    The JS inventory covers standard ARIA roles. This catches custom widgets,
    widgets with wrong/missing ARIA, and visually-obvious interactive regions
    that still require arrow keys to navigate internally.
    """
    from functions.llm import LLMClient
    from functions.tools import WIDGET_DISCOVERY_TOOL

    # Reuse the already-captured full-page screenshot when available.
    screenshot_path = getattr(capture_data, "full_page_path", "")
    if not screenshot_path or not os.path.exists(screenshot_path):
        if not captures_dir:
            return []
        screenshot_path = os.path.join(captures_dir, "widget_scan.png")
        try:
            await page.screenshot(path=screenshot_path, full_page=True)
        except Exception:
            logger.debug("Widget AI discovery: could not take screenshot, skipping")
            return []

    # Compact DOM snapshot of ARIA widgets and trigger controls on the page.
    try:
        dom_snippet = await page.evaluate("""() => {
            const roles = ['tablist','menu','menubar','tree','listbox',
                           'combobox','slider','grid','treegrid'];
            const roleQ = roles.map(r => '[role="' + r + '"]').join(', ');
            const widgets = [...document.querySelectorAll(roleQ)];
            const triggers = [...document.querySelectorAll(
                'button[aria-expanded], [aria-haspopup]:not([aria-haspopup="false"]), ' +
                'input[type="range"], select[multiple]'
            )];
            return {
                widget_elements: widgets.map(el => ({
                    tag: el.tagName.toLowerCase(),
                    role: el.getAttribute('role') || '',
                    id: el.id || '',
                    class: el.className || '',
                    children: el.children.length,
                    html: el.outerHTML,
                })),
                trigger_elements: triggers.map(el => ({
                    tag: el.tagName.toLowerCase(),
                    type: el.type || '',
                    role: el.getAttribute('role') || '',
                    id: el.id || '',
                    ariaExpanded: el.getAttribute('aria-expanded') || '',
                    ariaHasPopup: el.getAttribute('aria-haspopup') || '',
                    text: (el.textContent || '').trim(),
                })),
            };
        }""")
    except Exception:
        dom_snippet = {}
        logger.warning(
            "Widget DOM snapshot failed — AI arrow-key prompt will lack "
            "widget_elements/trigger_elements evidence for this page",
            exc_info=True,
        )

    known_lines = [
        f"- {w.get('type')} @ {w.get('selector')} "
        f"({w.get('items_count', len(w.get('items', [])))} items, "
        f"keys: {w.get('keys', [])})"
        for w in known_widgets
    ]

    system_prompt = (
        "You are an accessibility engineer verifying WCAG 2.2 keyboard interaction "
        "compliance. Identify interactive composite widgets on a web page that require "
        "arrow-key navigation per WAI-ARIA Authoring Practices.\n\n"
        "EXPECTED KEYBOARD PATTERNS:\n"
        "- tablist: ArrowLeft/ArrowRight between tabs\n"
        "- menu/menubar: ArrowDown/ArrowUp between items, Escape to close\n"
        "- tree: ArrowDown/ArrowUp to move, ArrowRight to expand, ArrowLeft to collapse\n"
        "- listbox: ArrowDown/ArrowUp between options\n"
        "- combobox: ArrowDown to open/navigate, Escape to close\n"
        "- slider: ArrowLeft/ArrowRight or ArrowDown/ArrowUp to change value\n"
        "- grid/treegrid: arrow keys in all 4 directions\n"
        "- accordion: Enter/Space to toggle panels\n"
        "- carousel/slideshow: ArrowLeft/ArrowRight between slides\n"
        "- date picker: arrow keys to navigate calendar cells\n\n"
        "ALREADY DETECTED (DO NOT repeat these):\n"
        + ("\n".join(known_lines) if known_lines else "(none)")
    )

    user_prompt = (
        "Examine the page screenshot and DOM structure below. "
        "Report ONLY composite widgets NOT already in the 'ALREADY DETECTED' list "
        "that need arrow-key testing. Focus on widgets where Tab alone would skip "
        "internal items. Include custom implementations that lack ARIA roles.\n\n"
        f"DOM STRUCTURE:\n{json.dumps(dom_snippet, indent=2)}"
    )

    try:
        client = LLMClient()
        result = await client.call_with_tools(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tool_name="report_widget_discovery",
            tool_schema=WIDGET_DISCOVERY_TOOL,
            images=[screenshot_path],
            temperature=0.0,
        )

        if not result or not result.get("widgets"):
            return []

        additional: list[dict] = []
        for w in result["widgets"]:
            wtype = w.get("type", "custom")
            sel = w.get("selector", "").strip()
            keys = w.get("keys") or []
            first_sel = (w.get("first_item_selector") or sel).strip()
            state_attr = w.get("state_attr", "") or _DEFAULT_STATE_ATTR.get(wtype, "aria-selected")

            if not sel or not keys:
                continue

            # Resolve first item and count items via live DOM.
            try:
                item_sels = await page.evaluate(
                    """(container) => {
                        const c = document.querySelector(container);
                        if (!c) return [];
                        // Child items by role or focusable tag
                        const kids = [...c.querySelectorAll(
                            '[role="tab"],[role="menuitem"],[role="treeitem"],' +
                            '[role="option"],[role="gridcell"],button,a[href]'
                        )];
                        return kids.map(el => {
                            if (el.id) return '#' + el.id;
                            const t = el.tagName.toLowerCase();
                            const p = el.parentElement;
                            if (!p) return t;
                            const sibs = [...p.children].filter(s => s.tagName === el.tagName);
                            return sibs.length > 1
                                ? t + ':nth-of-type(' + (sibs.indexOf(el) + 1) + ')'
                                : t;
                        });
                    }""",
                    sel,
                )
            except Exception as exc:
                logger.warning(
                    "Widget item enumeration failed for %s: %s -- falling back to first item only",
                    sel, exc,
                )
                item_sels = [first_sel] if first_sel else []

            if not item_sels:
                item_sels = [first_sel] if first_sel else [sel]

            additional.append({
                "type": wtype,
                "selector": sel,
                "items": [{"selector": s, "text": ""} for s in item_sels],
                "keys": keys,
                "stateAttr": state_attr,
                "ai_discovered": True,
                "ai_reason": w.get("reason", ""),
            })

        if additional:
            logger.info(
                "Widget keyboard AI discovery: %d additional widget(s) found: %s",
                len(additional),
                ", ".join(f"{a['type']}@{a['selector']}" for a in additional),
            )
        return additional

    except Exception:
        logger.warning("Widget AI discovery failed", exc_info=True)
        return []


_DEFAULT_STATE_ATTR: dict[str, str] = {
    "tablist": "aria-selected",
    "accordion": "aria-expanded",
    "menu": "aria-expanded",
    "combobox": "aria-expanded",
    "tree": "aria-expanded",
    "listbox": "aria-selected",
    "grid": "aria-selected",
    "slider": "aria-valuenow",
    "carousel": "aria-selected",
    "date_picker": "aria-selected",
    "custom": "aria-expanded",
}


# Number of confirmed FAILs (responded=False without error) after which
# a selector is banned from further widget-exploration proposals. A
# single FAIL might be a transient state issue; two in a row means the
# element genuinely has no keyboard handler.
_KEYBOARD_PROBE_BAN_AFTER_FAILS = 2


def _should_ban_selector(probe: dict, all_results: list[dict]) -> bool:
    """Decide whether to add a probe's selector to the AI-exploration ban list.

    Selectors are banned when:
      * Playwright could not find or see the element (the AI is hallucinating
        a CSS path that does not exist), OR
      * The selector responded to no keys after _KEYBOARD_PROBE_BAN_AFTER_FAILS
        confirmed FAILs (the element has no ARIA keyboard support to test).
    """
    err = (probe.get("error") or "").lower()
    if "not found" in err or "not visible" in err:
        return True
    if err or probe.get("any_key_responded") is not False:
        return False
    sel = probe.get("selector")
    confirmed_fails = sum(
        1 for r in all_results
        if r.get("selector") == sel
        and not (r.get("error") or "")
        and r.get("any_key_responded") is False
    )
    return confirmed_fails >= _KEYBOARD_PROBE_BAN_AFTER_FAILS


async def _execute_keyboard_probe(
    page: Page,
    selector: str,
    pre_action: str,
    keys: list[str],
) -> dict:
    """Execute one AI-directed keyboard probe: focus, optional pre-action, key sequence.

    Returns a result dict compatible with the widget_keyboard result schema so it
    slots directly into the same per-SC analysis and DOM context rendering.
    """
    result: dict = {
        "selector": selector,
        "pre_action": pre_action,
        "keys_tested": list(keys),
        "key_results": [],
        "any_key_responded": False,
        "error": "",
    }

    _active_js = """() => {
        const el = document.activeElement;
        if (!el || el === document.body) return null;
        if (el.id) return '#' + el.id;
        const t = el.tagName.toLowerCase();
        const p = el.parentElement;
        if (!p) return t;
        const sibs = [...p.children].filter(s => s.tagName === el.tagName);
        return sibs.length > 1 ? t + ':nth-of-type(' + (sibs.indexOf(el) + 1) + ')' : t;
    }"""

    _state_js = """() => {
        const el = document.activeElement;
        if (!el) return {};
        return {
            ariaSelected: el.getAttribute('aria-selected') || '',
            ariaExpanded: el.getAttribute('aria-expanded') || '',
            ariaValuenow: el.getAttribute('aria-valuenow') || '',
        };
    }"""

    try:
        el = await page.query_selector(selector)
        if not el:
            result["error"] = f"not found: {selector}"
            return result
        if not await el.is_visible():
            result["error"] = f"not visible: {selector}"
            return result

        await el.scroll_into_view_if_needed(timeout=3000)

        if pre_action == "hover":
            await el.hover()
            await page.wait_for_timeout(600)
            # Re-focus after hover so key presses land on the right element
            await page.evaluate("(el) => el.focus({preventScroll: true})", el)
        elif pre_action == "click":
            await el.click()
            await page.wait_for_timeout(600)
            el = await page.query_selector(selector)
            if el:
                await page.evaluate("(el) => el.focus({preventScroll: true})", el)
        elif pre_action in ("Enter", "Space"):
            await page.evaluate("(el) => el.focus({preventScroll: true})", el)
            await page.wait_for_timeout(200)
            await page.keyboard.press(pre_action)
            await page.wait_for_timeout(500)
        else:
            await page.evaluate("(el) => el.focus({preventScroll: true})", el)
            await page.wait_for_timeout(300)

        any_responded = False
        for key in keys:
            before_focus = await page.evaluate(_active_js)
            before_state = await page.evaluate(_state_js)

            await page.keyboard.press(key)
            await page.wait_for_timeout(300)

            after_focus = await page.evaluate(_active_js)
            after_state = await page.evaluate(_state_js)

            focus_moved = before_focus != after_focus
            state_changed = (
                before_state.get("ariaSelected") != after_state.get("ariaSelected")
                or before_state.get("ariaExpanded") != after_state.get("ariaExpanded")
                or before_state.get("ariaValuenow") != after_state.get("ariaValuenow")
            )
            responded = focus_moved or state_changed
            if responded:
                any_responded = True

            result["key_results"].append({
                "key": key,
                "focus_moved": focus_moved,
                "state_changed": state_changed,
                "before": {"focus": before_focus, **before_state},
                "after": {"focus": after_focus, **after_state},
                "responded": responded,
            })

        result["any_key_responded"] = any_responded

        await page.evaluate("() => { const el = document.activeElement; if (el) el.blur(); }")
        await page.wait_for_timeout(200)

    except Exception as exc:
        result["error"] = str(exc)

    return result


async def _ai_keyboard_exploration_loop(
    page: Page,
    capture_data: CaptureData,
    initial_results: list[dict],
    captures_dir: str,
    max_iterations: int = 15,
    max_consecutive_incomplete: int = 2,
) -> list[dict]:
    """AI-driven agentic keyboard exploration loop.

    After the JS-inventory pass, the AI sees the current page screenshot
    plus a full report of what was already tested (and how each widget
    responded). It decides the next action — hover to reveal a menu,
    click to open a dropdown, arrow through a tablist, Enter to expand
    an accordion — executes it live in the browser, then reviews the
    result and decides what to probe next. Loops until the AI signals
    'done' or the iteration cap is hit.
    """
    from functions.llm import LLMClient
    from functions.tools import WIDGET_EXPLORATION_ACTION_TOOL

    additional: list[dict] = []
    all_results = list(initial_results)
    consecutive_incomplete = 0
    corrective_note = ""
    banned_selectors: set[str] = set()
    last_proposal: tuple[str, str, tuple[str, ...]] | None = None
    repeat_count = 0

    for iteration in range(max_iterations):
        shot_path = ""
        if captures_dir:
            shot_path = os.path.join(captures_dir, f"widget_explore_{iteration:02d}.png")
            try:
                await page.screenshot(path=shot_path, full_page=False)
            except Exception:
                shot_path = getattr(capture_data, "full_page_path", "")

        if not shot_path or not os.path.exists(shot_path):
            break

        # Build compact summary — collapse repeated failures per selector
        # so the AI doesn't see the same FAIL entry 10 times.
        from collections import defaultdict
        fail_counts: dict[str, int] = defaultdict(int)
        nonexistent_sels: set[str] = set()  # selectors Playwright couldn't find
        real_sels: set[str] = set()  # selectors that exist on the page
        summary_lines: list[str] = []
        seen_sels: dict[str, dict] = {}  # sel → best result entry

        for r in all_results:
            sel = r.get("selector", "?")
            any_resp = r.get("any_key_responded")
            all_items = r.get("all_items_reached")
            n = r.get("items_count", 0)
            distinct = r.get("distinct_items_reached")
            err = (r.get("error") or "").strip()

            if err and "not found" in err.lower():
                verdict = "SELECTOR NOT FOUND on page"
                nonexistent_sels.add(sel)
            elif err:
                verdict = f"ERROR: {err}"
                real_sels.add(sel)
            elif any_resp is False:
                verdict = "FAIL"
                fail_counts[sel] += 1
                real_sels.add(sel)
            elif all_items is False and n > 1:
                verdict = f"PARTIAL — {distinct}/{n} items reached"
                real_sels.add(sel)
            elif all_items is True:
                verdict = f"PASS — all {n} items reached"
                real_sels.add(sel)
            elif any_resp is True:
                verdict = "PASS (responded)"
                real_sels.add(sel)
            else:
                verdict = "tested"
                real_sels.add(sel)

            # Keep the best (non-FAIL, non-NOT-FOUND) result per selector
            existing = seen_sels.get(sel)
            existing_verdict = existing["_verdict"] if existing else ""
            is_worse = verdict in ("FAIL", "SELECTOR NOT FOUND on page")
            if not existing or not is_worse:
                seen_sels[sel] = {**r, "_verdict": verdict}
            elif existing_verdict in ("FAIL", "SELECTOR NOT FOUND on page"):
                seen_sels[sel] = {**r, "_verdict": verdict}

        for sel, r in seen_sels.items():
            wtype = r.get("type", "?")
            keys = r.get("keys_tested") or r.get("keys", [])
            ai_tag = " [AI]" if r.get("ai_discovered") else ""
            verdict = r["_verdict"]
            fc = fail_counts[sel]
            if verdict == "FAIL" and fc > 1:
                verdict = (
                    f"FAIL (tried {fc}×) — APG pattern keys unresponsive. "
                    "Not by itself an SC 2.1.1 violation: the widget may "
                    "still operate via Enter/Space/Tab, and the widget-type "
                    "label may be a heuristic guess"
                )
            keys_str = ", ".join(str(k) for k in keys) if keys else "none"
            summary_lines.append(
                f"  {wtype}{ai_tag} @ {sel}: [{keys_str}] → {verdict}"
            )

        # Selectors confirmed as non-ARIA after 2+ FAIL attempts
        confirmed_non_aria = {sel for sel, fc in fail_counts.items() if fc >= 2}

        tested_block = "\n".join(summary_lines) or "  (nothing tested yet)"
        skip_block = (
            "\nDO NOT RETRY THESE (confirmed non-ARIA after 2+ attempts):\n"
            + "\n".join(f"  {s}" for s in sorted(confirmed_non_aria))
            if confirmed_non_aria else ""
        )
        nonexistent_block = (
            "\nDO NOT USE THESE SELECTORS (they do not exist on the page — "
            "Playwright could not find them):\n"
            + "\n".join(f"  {s}" for s in sorted(nonexistent_sels))
            if nonexistent_sels else ""
        )
        # Surface every verified selector so the AI re-uses them instead
        # of inventing new CSS paths.
        known_real_block = ""
        if real_sels:
            known_real_block = (
                "\nKNOWN-REAL SELECTORS on this page (use ONLY selectors from "
                "this list or attribute-based selectors visible in the screenshot — "
                "do NOT invent CSS paths):\n"
                + "\n".join(f"  {s}" for s in sorted(real_sels))
            )

        system_prompt = (
            "You are an accessibility engineer performing WCAG 2.2 keyboard interaction "
            "testing. Your job is to build a thorough, hands-on understanding of how every "
            "interactive widget on this page responds to the keyboard — by ACTIVELY "
            "PROBING each one in the live browser, not by inferring from the screenshot.\n\n"
            "MISSION: Be exhaustive. Examine the screenshot and find EVERY interactive "
            "composite widget visible on the page — menus, dropdowns, comboboxes, tablists, "
            "accordions, sliders, carousels, trees, grids, date pickers, custom widgets, "
            "search controls with autocomplete, language switchers, hamburger menus, "
            "expand/collapse panels, anything that opens a popup or moves focus between "
            "child items. For EACH one, drive it with the appropriate keyboard sequence "
            "and record what happens. Your goal is full coverage, not a token probe — "
            "keep going until you have genuinely tested everything you can see.\n\n"
            "KEYBOARD PATTERNS BY WIDGET TYPE:\n"
            "  tablist    → Tab to selected tab, then ArrowLeft/ArrowRight to walk all tabs\n"
            "  menu       → hover or click to open, then ArrowDown/ArrowUp between items, "
            "Escape to close\n"
            "  combobox   → click or focus, ArrowDown to open + navigate options, "
            "Enter to select, Escape to close\n"
            "  tree       → ArrowDown/ArrowUp to move, ArrowRight to expand, ArrowLeft to "
            "collapse\n"
            "  accordion  → Tab to button, Enter or Space to toggle panel\n"
            "  slider     → ArrowLeft/ArrowRight or ArrowDown/ArrowUp to change value\n"
            "  carousel   → ArrowLeft/ArrowRight between slides\n"
            "  grid       → arrow keys in all 4 directions\n"
            "  date picker→ Enter to open, arrow keys to navigate cells\n\n"
            "TESTS COMPLETED SO FAR:\n"
            + tested_block
            + skip_block
            + nonexistent_block
            + known_real_block + "\n\n"
            "Examine the screenshot carefully. Return the NEXT test to run, or 'done'.\n\n"
            "RULES:\n"
            "1. Do NOT re-test anything already marked PASS or PARTIAL with the SAME key "
            "sequence. (Different keys on the same widget is fine — e.g., after testing "
            "arrow nav on a menu, test Enter + Escape on the same menu.)\n"
            "2. Do NOT retry any selector listed in 'DO NOT RETRY THESE' — those are plain "
            "HTML elements with no ARIA keyboard support. Retrying them wastes iterations.\n"
            "3. Plain <ul>/<ol> navigation lists are NOT ARIA menus — they do not respond "
            "to ArrowDown/ArrowUp. Only test arrow keys on elements with role=menu/menuitem "
            "or similar explicit ARIA roles.\n"
            "4. For menus/dropdowns: use pre_action='hover' or 'click' to open them "
            "BEFORE testing arrow navigation inside.\n"
            "5. Include enough key presses to walk through ALL items in the widget "
            "(e.g. 5× ArrowRight for a 6-tab tablist).\n"
            "6. ANY widget marked '[AI]' in the tested-so-far list with keys=[none] was "
            "DISCOVERED but NEVER keyboard-probed. You MUST probe each [AI] widget with "
            "the correct pattern from the list above (open it via pre_action then run the "
            "matching arrow keys) before returning 'done'.\n"
            "7. Before returning 'done', SCAN the screenshot for interactive widgets that "
            "are NOT in the tested-so-far list at all. The deterministic scan only catches "
            "widgets with proper ARIA roles — custom widgets, mis-roled widgets, and "
            "decoration-styled controls won't be there. Find them visually and probe them.\n"
            "8. If you see a widget but are unsure of its type, use widget_type='custom' "
            "and pick the most likely arrow keys.\n"
            "9. NEVER return status='continue' without BOTH a non-empty selector AND a "
            "non-empty keys array. If you have no concrete next probe to run, return "
            "status='done' instead.\n"
            "10. Do NOT invent CSS paths like '#some-id > li:nth-of-type(N) > button' from "
            "the screenshot alone. Prefer selectors from the 'KNOWN-REAL SELECTORS' list "
            "above, or attribute selectors that are actually visible in the page "
            "(e.g. [role='tab'], [aria-controls='X'], button[aria-expanded]).\n"
            "11. Any selector you previously tried that appears under 'DO NOT USE THESE "
            "SELECTORS' was NOT FOUND on the page. Do not retry it. Pick a different "
            "selector that you have evidence actually exists.\n"
            "12. Return 'done' ONLY when (a) every [AI]-discovered widget has been "
            "keyboard-probed, AND (b) every interactive widget you can see in the "
            "screenshot is represented in the tested-so-far list with at least one "
            "tested key sequence. 'Done' is the final state, not an early exit."
        )

        user_prompt = (
            "Current page screenshot attached. "
            "What keyboard interaction should be tested next? "
            "Return one action (or 'done' if everything is covered)."
        )
        if corrective_note:
            user_prompt = corrective_note + "\n\n" + user_prompt

        try:
            client = LLMClient()
            action = await client.call_with_tools(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tool_name="keyboard_exploration_action",
                tool_schema=WIDGET_EXPLORATION_ACTION_TOOL,
                images=[shot_path],
                temperature=0.0,
            )

            if not action:
                consecutive_incomplete += 1
                if consecutive_incomplete >= max_consecutive_incomplete:
                    logger.warning(
                        "Widget AI exploration: iteration %d — no response after "
                        "%d consecutive incomplete attempts, stopping",
                        iteration + 1, consecutive_incomplete,
                    )
                    break
                corrective_note = (
                    "Your previous response was empty. Either return a valid action "
                    "(non-empty selector + non-empty keys array) OR return status='done'."
                )
                logger.info(
                    "Widget AI exploration: iteration %d — empty response, retrying",
                    iteration + 1,
                )
                continue

            if action.get("status") == "done":
                logger.info(
                    "Widget AI exploration: done after %d iteration(s)", iteration + 1
                )
                break

            sel = (action.get("selector") or "").strip()
            keys = action.get("keys") or []
            pre = action.get("pre_action", "none")
            wtype = action.get("widget_type", "custom")
            reason = action.get("reason", "")

            if not sel or not keys:
                consecutive_incomplete += 1
                if consecutive_incomplete >= max_consecutive_incomplete:
                    logger.warning(
                        "Widget AI exploration: iteration %d — incomplete action "
                        "(selector=%r keys=%r) for %d consecutive attempts, stopping",
                        iteration + 1, sel, keys, consecutive_incomplete,
                    )
                    break
                corrective_note = (
                    f"Your previous response was status='continue' with "
                    f"selector={sel!r} and keys={keys!r} — that is INVALID. "
                    "Either: (a) return status='continue' with BOTH a non-empty selector "
                    "AND a non-empty keys array describing a specific widget to probe, "
                    "OR (b) return status='done' if everything visible has been tested."
                )
                logger.info(
                    "Widget AI exploration: iteration %d — incomplete action "
                    "(selector=%r keys=%r), asking for retry",
                    iteration + 1, sel, keys,
                )
                continue

            consecutive_incomplete = 0
            corrective_note = ""

            proposal_key = (sel, pre, tuple(str(k) for k in keys))
            if proposal_key == last_proposal:
                repeat_count += 1
            else:
                repeat_count = 0
            last_proposal = proposal_key

            if repeat_count >= 1:
                logger.warning(
                    "Widget AI exploration: iteration %d — model repeated identical "
                    "proposal (selector=%r pre=%r keys=%r); aborting loop to avoid waste",
                    iteration + 1, sel, pre, keys,
                )
                break

            if sel in banned_selectors:
                logger.info(
                    "Widget AI exploration: iteration %d — model proposed banned "
                    "selector %r again; rejecting without probe", iteration + 1, sel,
                )
                corrective_note = (
                    f"You proposed selector={sel!r} but that selector is listed under "
                    f"'DO NOT USE THESE SELECTORS' / 'DO NOT RETRY THESE'. "
                    f"It either does not exist on the page or has been confirmed "
                    f"non-responsive. Pick a DIFFERENT selector — prefer one from "
                    f"the 'KNOWN-REAL SELECTORS' list, or attribute selectors visible "
                    f"in the screenshot. Do NOT re-propose this selector."
                )
                continue

            logger.info(
                "Widget AI exploration: iteration %d — %s @ %s  pre=%s  keys=%s\n  → %s",
                iteration + 1, wtype, sel, pre, keys, reason,
            )

            probe = await _execute_keyboard_probe(page, sel, pre, keys)
            probe["type"] = wtype
            probe["selector"] = sel
            probe["ai_discovered"] = True
            probe["ai_reason"] = reason

            additional.append(probe)
            all_results.append(probe)

            if _should_ban_selector(probe, all_results):
                banned_selectors.add(sel)

        except Exception:
            logger.warning(
                "Widget AI exploration: iteration %d exception", iteration + 1,
                exc_info=True,
            )
            break

    if additional:
        logger.info(
            "Widget AI exploration: %d additional probe(s) completed over %d iteration(s)",
            len(additional), min(len(additional), max_iterations),
        )
    return additional


async def _capture_widget_keyboard(
    page: Page,
    capture_data: CaptureData,
    captures_dir: str = "",
) -> None:
    """Test keyboard interactions for ARIA widgets.

    Phase 1 — JS inventory: deterministic ARIA-role scan finds standard
    composite widgets (tablist, menu, combobox, tree, slider, accordion).
    Adds AI-discovered widgets that the JS selectors missed.

    Phase 2 — AI exploration loop: the AI sees the page screenshot and
    the full Phase 1 test report, then drives further testing — hover to
    open menus, click to activate dropdowns, arrow through every item —
    until it declares all interactive regions covered.
    """
    import time

    try:
        t0 = time.monotonic()
        widgets = await page.evaluate(_WIDGET_INVENTORY_JS)

        # AI-augmented discovery: single LLM call to catch widgets the JS
        # selectors missed (custom markup, missing ARIA, etc.). Merged with
        # the JS inventory and tested the same way before the agentic loop.
        ai_extra = await _ai_discover_widgets(page, capture_data, widgets, captures_dir)
        if ai_extra:
            existing_sels = {w.get("selector") for w in widgets}
            for aw in ai_extra:
                if aw.get("selector") not in existing_sels:
                    widgets.append(aw)
                    existing_sels.add(aw["selector"])

        results: list[dict] = []

        if not widgets:
            logger.info("Widget keyboard: no ARIA widgets found by JS+AI discovery — running AI exploration only")

        for widget in widgets:
            wtype = widget.get("type", "")
            wselector = widget.get("selector", "")
            keys_to_test = widget.get("keys", [])
            state_attr = widget.get("stateAttr", "")
            items = widget.get("items", [])

            if not items:
                continue

            widget_result = {
                "type": wtype,
                "selector": wselector,
                "items_count": len(items),
                "keys_tested": [],
                "key_results": [],
                "ai_discovered": widget.get("ai_discovered", False),
            }

            try:
                first_item_sel = items[0].get("selector", "")
                if not first_item_sel:
                    continue

                el = await page.query_selector(first_item_sel)
                if not el:
                    continue
                if not await el.is_visible():
                    continue

                try:
                    await el.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    continue

                await page.evaluate("(el) => el.focus({preventScroll: true})", el)
                await page.wait_for_timeout(300)

                # ── Multi-item walk (tablist / menu / tree) ───────────
                # When the widget has >1 item, press the primary
                # navigation key ``items_count - 1`` times and record
                # which item has focus after each press. This tests
                # the full walk, not just the first step. A widget
                # where arrow-1 works but arrow-2 jumps to the end is
                # broken; the old "only first item" test missed that.
                # Bounded by len(items) + 1 to prevent a rogue widget
                # from wrapping forever; real multi-item widgets have
                # 2-20 items.
                walk_keys = {
                    "tablist": "ArrowRight",
                    "menu": "ArrowDown",
                    "tree": "ArrowDown",
                }
                walk_key = walk_keys.get(wtype)
                walked_items: list[dict] = []
                all_items_reached = None
                if walk_key and len(items) > 1:
                    # Record the first item's focus before any arrow
                    # presses so we can diff against it below.
                    first_info = await page.evaluate("""() => {
                        const el = document.activeElement;
                        if (!el) return null;
                        let s = el.tagName.toLowerCase();
                        if (el.id) s = '#' + el.id;
                        return {
                            selector: s,
                            text: (el.textContent || '').trim(),
                        };
                    }""")
                    if first_info:
                        walked_items.append(first_info)

                    # Press the walk key (items_count - 1) times;
                    # record where focus landed each press. Stop
                    # early if focus stops moving (widget boundary).
                    max_steps = min(len(items) - 1, 49)
                    for step in range(max_steps):
                        try:
                            await page.keyboard.press(walk_key)
                            await page.wait_for_timeout(150)
                            info = await page.evaluate("""() => {
                                const el = document.activeElement;
                                if (!el) return null;
                                let s = el.tagName.toLowerCase();
                                if (el.id) s = '#' + el.id;
                                return {
                                    selector: s,
                                    text: (el.textContent || '').trim(),
                                };
                            }""")
                            if info:
                                walked_items.append(info)
                            # If we stopped moving (hit end without
                            # wrapping), bail out rather than waste
                            # presses on a no-op.
                            if (len(walked_items) >= 2
                                    and walked_items[-1] == walked_items[-2]):
                                break
                        except Exception:
                            break

                    distinct_reached = len({w.get("selector", "") for w in walked_items})
                    all_items_reached = distinct_reached >= len(items)
                    widget_result["items_walked"] = walked_items
                    widget_result["distinct_items_reached"] = distinct_reached
                    widget_result["all_items_reached"] = all_items_reached

                    # Reset focus to first item so the
                    # per-key-category loop below starts from the
                    # same baseline every widget.
                    try:
                        el2 = await page.query_selector(first_item_sel)
                        if el2:
                            await page.evaluate(
                                "(el) => el.focus({preventScroll: true})", el2,
                            )
                        await page.wait_for_timeout(200)
                    except Exception:
                        pass  # best-effort — refocus first item, page state may be unrecoverable

                # ── Per-key state/focus change (original behavior) ──
                # Test each declared key once more to record before/
                # after ARIA state. This is the outcome check --
                # a menu that responds to ArrowDown should also
                # respond to Escape by collapsing.
                for key in keys_to_test:
                    try:
                        state_before = await page.evaluate("""(attr) => {
                            const el = document.activeElement;
                            if (!el) return {focused: null, state: null};
                            let s = el.tagName.toLowerCase();
                            if (el.id) s = '#' + el.id;
                            return {
                                focused: s,
                                state: el.getAttribute(attr) || '',
                                text: (el.textContent || '').trim(),
                            };
                        }""", state_attr)

                        await page.keyboard.press(key)
                        await page.wait_for_timeout(400)

                        state_after = await page.evaluate("""(attr) => {
                            const el = document.activeElement;
                            if (!el) return {focused: null, state: null};
                            let s = el.tagName.toLowerCase();
                            if (el.id) s = '#' + el.id;
                            return {
                                focused: s,
                                state: el.getAttribute(attr) || '',
                                text: (el.textContent || '').trim(),
                                role: el.getAttribute('role') || '',
                                ariaExpanded: el.getAttribute('aria-expanded') || '',
                                ariaSelected: el.getAttribute('aria-selected') || '',
                                ariaChecked: el.getAttribute('aria-checked') || '',
                                ariaValuenow: el.getAttribute('aria-valuenow') || '',
                            };
                        }""", state_attr)

                        focus_moved = (
                            state_before.get("focused") != state_after.get("focused")
                        )
                        state_changed = (
                            state_before.get("state") != state_after.get("state")
                        )

                        widget_result["keys_tested"].append(key)
                        widget_result["key_results"].append({
                            "key": key,
                            "focus_moved": focus_moved,
                            "state_changed": state_changed,
                            "before": state_before,
                            "after": state_after,
                            "responded": focus_moved or state_changed,
                        })
                    except Exception as exc:
                        logger.warning(
                            "Widget key probe failed (key=%s, widget=%s): %s",
                            key, widget_result.get("selector", "?"), exc,
                        )
                        widget_result["key_results"].append({
                            "key": key,
                            "error": True,
                            "error_detail": str(exc),
                            "responded": False,
                        })

                responded_count = sum(
                    1 for kr in widget_result["key_results"] if kr.get("responded")
                )
                widget_result["any_key_responded"] = responded_count > 0
                widget_result["all_keys_responded"] = responded_count == len(keys_to_test)

                await page.evaluate("() => { if (document.activeElement) document.activeElement.blur(); }")
                await page.wait_for_timeout(200)

            except Exception:
                logger.debug("Widget keyboard test failed for %s (%s)", wselector, wtype)
                widget_result["error"] = True

            results.append(widget_result)

        # Phase 2: AI-driven exploration loop.
        # The AI reviews what was tested, sees the current page screenshot,
        # and decides what to probe next (hover, click, arrow keys, etc.)
        # until it signals all interactive widgets are covered.
        ai_loop_results = await _ai_keyboard_exploration_loop(
            page, capture_data, results, captures_dir
        )
        results.extend(ai_loop_results)

        capture_data.widget_keyboard = results

        total = len(results)
        responsive = sum(1 for r in results if r.get("any_key_responded"))
        elapsed = time.monotonic() - t0
        logger.info(
            "Widget keyboard: %d widgets tested (%d AI-loop), %d responsive (%.1fs)",
            total, len(ai_loop_results), responsive, elapsed,
        )
    except Exception:
        logger.exception("Widget keyboard testing failed")


# ─── Reduced motion preference ───────────────────────────────────────────────

async def _capture_reduced_motion(
    page: Page,
    capture_data: CaptureData,
    captures_dir: str,
) -> None:
    """Test if the page respects prefers-reduced-motion.

    1. Count animations in normal mode
    2. Set prefers-reduced-motion: reduce
    3. Count animations again
    4. Compare -- if count dropped, page respects preference
    """
    import time

    try:
        t0 = time.monotonic()

        normal_data = await page.evaluate("""() => {
            const animations = document.getAnimations();
            const details = animations.map(a => ({
                name: a.animationName || a.id || '',
                playState: a.playState || '',
                target: (() => {
                    const el = a.effect && a.effect.target;
                    if (!el) return '';
                    let s = el.tagName ? el.tagName.toLowerCase() : '';
                    if (el.id) s = '#' + el.id;
                    return s;
                })(),
            }));
            return {
                count: animations.length,
                running: animations.filter(a => a.playState === 'running').length,
                details: details,
            };
        }""")

        animations_normal = normal_data.get("count", 0)

        await page.emulate_media(reduced_motion="reduce")
        await page.wait_for_timeout(1000)

        reduced_data = await page.evaluate("""() => {
            const animations = document.getAnimations();
            const details = animations.map(a => ({
                name: a.animationName || a.id || '',
                playState: a.playState || '',
                target: (() => {
                    const el = a.effect && a.effect.target;
                    if (!el) return '';
                    let s = el.tagName ? el.tagName.toLowerCase() : '';
                    if (el.id) s = '#' + el.id;
                    return s;
                })(),
            }));
            return {
                count: animations.length,
                running: animations.filter(a => a.playState === 'running').length,
                details: details,
            };
        }""")

        animations_reduced = reduced_data.get("count", 0)

        screenshot_path = ""
        if animations_normal > 0:
            screenshot_path = os.path.join(captures_dir, "reduced_motion.png")
            try:
                await page.screenshot(path=screenshot_path, full_page=False)
            except Exception:
                screenshot_path = ""

        respects = False
        if animations_normal > 0 and animations_reduced < animations_normal:
            respects = True
        elif animations_normal == 0:
            respects = True

        capture_data.reduced_motion = {
            "animations_normal": animations_normal,
            "animations_normal_running": normal_data.get("running", 0),
            "animations_reduced": animations_reduced,
            "animations_reduced_running": reduced_data.get("running", 0),
            "respects_preference": respects,
            "screenshot_path": screenshot_path,
            "normal_details": normal_data.get("details", []),
            "reduced_details": reduced_data.get("details", []),
        }

        # Playwright treats reduced_motion=None as "leave unchanged";
        # "null" is the documented reset back to system default.
        await page.emulate_media(reduced_motion="null")

        elapsed = time.monotonic() - t0
        logger.info(
            "Reduced motion: normal=%d, reduced=%d, respects=%s (%.1fs)",
            animations_normal, animations_reduced, respects, elapsed,
        )
    except Exception:
        logger.exception("Reduced motion capture failed")
        try:
            await page.emulate_media(reduced_motion="null")
        except Exception:
            pass  # cleanup — best-effort restore of media emulation, page state may be unrecoverable
