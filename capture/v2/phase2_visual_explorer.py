"""Phase 2: Visual AI Explorer — 3-screenshot interaction cycle.

For each interactive element identified by Phase 1:
1. Screenshot INITIAL state (before any interaction)
2. Screenshot HOVER state (after hovering)
3. Screenshot ACTIVATED state (after clicking/focusing)

The AI analyzes the 3 screenshots and decides:
- Did new content appear? (dropdown, submenu, modal, tooltip)
- Should we explore DEEPER into the new content?
- What accessibility observations can we make?

Exploration goes as deep as possible (submenus, nested accordions)
without leaving the page. If a click navigates away, we go back.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Page

from capture.v2.element_inventory import ElementInventory, InventoryElement
from functions.pixel_diff import screenshots_differ as _screenshots_differ
from functions.tools import EXPLORATION_TOOL

logger = logging.getLogger(__name__)

# Exploration limits
MAX_ELEMENTS = 5000       # Total elements to explore
MAX_DEPTH = 5             # Maximum recursion depth
PHASE2_TIMEOUT = 7200     # 120 minutes wall clock (scaled with element limit)
HOVER_WAIT_MS = 700       # Time to wait after hover
CLICK_WAIT_MS = 1000      # Time to wait after click
SCROLL_TIMEOUT_MS = 3000  # Timeout for scroll into view


@dataclass
class StateScreenshot:
    """A single screenshot capturing one visual state of an element."""
    path: str = ""
    state: str = ""       # "initial", "hover", "click_1", "submenu_open", etc.
    description: str = "" # What this state shows
    action_taken: str = ""  # What action produced this state


@dataclass
class ExplorationResult:
    """Result of exploring a single element.

    Screenshots are a DYNAMIC list — one per visual state change.
    A simple link might have 2 (initial + hover). A mega-menu with
    3 levels of submenus might have 8+ screenshots.
    """
    selector: str
    text: str = ""
    depth: int = 0
    # Dynamic screenshot list — every state change gets captured
    screenshots: list = field(default_factory=list)  # list[StateScreenshot]
    interaction_response: str = "none"  # none, tooltip, dropdown, modal, etc.
    new_elements: list = field(default_factory=list)  # Newly appeared elements
    focus_indicator_visible: bool = False
    state_change_detected: bool = False
    accessibility_observations: list = field(default_factory=list)
    error: str = ""

    # Legacy accessors for backward compatibility
    @property
    def initial_screenshot(self) -> str:
        for s in self.screenshots:
            if s.state == "initial":
                return s.path
        return self.screenshots[0].path if self.screenshots else ""

    @property
    def hover_screenshot(self) -> str:
        for s in self.screenshots:
            if s.state == "hover":
                return s.path
        return ""

    @property
    def activated_screenshot(self) -> str:
        for s in self.screenshots:
            if "click" in s.state or "activated" in s.state:
                return s.path
        return ""


# Schema lives in functions.tools as the canonical EXPLORATION_TOOL --
# imported above so this file and any other caller share one definition.


async def run_phase2(
    page: Page,
    inventory: ElementInventory,
    capture_data: Any,
    ai_client: Any,
    captures_dir: str,
    progress_callback=None,
) -> list[ExplorationResult]:
    """Run Phase 2: Visual AI Explorer.

    Explores every interactive element with the 3-screenshot cycle.
    Returns a list of ExplorationResults.
    """
    phase_start = time.monotonic()
    logger.info("=" * 60)
    logger.info("PHASE 2: Visual AI Explorer")
    logger.info("=" * 60)

    explore_dir = os.path.join(captures_dir, "exploration")
    os.makedirs(explore_dir, exist_ok=True)

    # Get elements to explore, sorted by priority
    explorable = inventory.get_explorable()
    has_rect = sum(1 for e in explorable if e.rect)
    logger.info("PHASE 2: %d elements to explore (from %d total, %d have rects)",
                len(explorable), len(inventory.elements), has_rect)

    if progress_callback:
        await progress_callback(f"Phase 2: Exploring {len(explorable)} interactive elements...")

    results: list[ExplorationResult] = []
    explored_count = 0
    original_url = page.url
    explored_selectors: set[str] = set()

    hit_limit = False

    for i, elem in enumerate(explorable):
        # Check limits
        if explored_count >= MAX_ELEMENTS:
            skipped = [e.selector for e in explorable[i:]]
            logger.warning("PHASE 2: Hit element limit (%d) — %d elements skipped",
                           MAX_ELEMENTS, len(skipped))
            hit_limit = True
            break
        if time.monotonic() - phase_start > PHASE2_TIMEOUT:
            skipped = [e.selector for e in explorable[i:]]
            logger.warning("PHASE 2: Hit time limit (%.0fs) — %d elements skipped",
                           PHASE2_TIMEOUT, len(skipped))
            hit_limit = True
            break

        if progress_callback and i % 5 == 0:
            await progress_callback(
                f"Phase 2: Exploring element {i + 1}/{len(explorable)}: "
                f"{elem.type} \"{elem.text}\"..."
            )

        logger.info("PHASE 2 [%d/%d] Exploring: %s \"%s\" (priority=%s, actions=%s)",
                     i + 1, len(explorable), elem.type, elem.text,
                     elem.exploration_priority, elem.exploration_actions)

        try:
            result = await asyncio.wait_for(
                _explore_element(
                    page, elem, explore_dir, ai_client,
                    depth=0, explored_count=explored_count, original_url=original_url,
                ),
                timeout=120,
            )
        except asyncio.TimeoutError:
            logger.error(
                "PHASE 2: TIMEOUT exploring element %d/%d: %s \"%s\" "
                "(stuck >120s — likely a Playwright interaction hung on a "
                "closed dropdown or unresponsive element). Skipping.",
                i + 1, len(explorable), elem.type if hasattr(elem, 'type') else '?',
                (elem.text if hasattr(elem, 'text') else str(elem)),
            )
            result = ExplorationResult(
                selector=elem.selector if hasattr(elem, 'selector') else str(elem),
                text=elem.text if hasattr(elem, 'text') else '',
                depth=0,
                error="Timeout after 120s — element exploration hung",
            )
            # Probe the page connection. If Playwright lost its
            # bridge to the browser (e.g. hung carousel button froze
            # the renderer), asyncio.wait_for can raise TimeoutError
            # but the underlying I/O may stay blocked, freezing every
            # subsequent _explore_element call. The cheap evaluate()
            # round-trip below has its own 5s budget; if it also
            # hangs we abort Phase 2 cleanly with a partial result
            # rather than letting the whole capture stall for the
            # full PHASE2_TIMEOUT window (60 minutes). Observed on
            # a community-college carousel elem_0065 — Phase 2 hung 28+ minutes
            # past the 120s per-element timeout because Playwright
            # itself was unresponsive.
            try:
                await asyncio.wait_for(
                    page.evaluate("() => 1"), timeout=5,
                )
            except (asyncio.TimeoutError, Exception) as conn_exc:
                logger.error(
                    "PHASE 2: Playwright connection unresponsive after "
                    "element timeout (page.evaluate '() => 1' failed: "
                    "%s). Aborting Phase 2 with partial results to "
                    "prevent capture stalling.",
                    type(conn_exc).__name__,
                )
                results.append(result)
                explored_count += 1
                hit_limit = True
                break
        except Exception as exc:
            # Any other exception during exploration: log and skip
            # the element. The earlier path only caught TimeoutError,
            # so a Playwright TargetClosedError or ConnectionError
            # would crash the whole loop.
            logger.error(
                "PHASE 2: ERROR exploring element %d/%d (%s): %s — "
                "skipping element, continuing loop.",
                i + 1, len(explorable), type(exc).__name__, exc,
            )
            result = ExplorationResult(
                selector=elem.selector if hasattr(elem, 'selector') else str(elem),
                text=elem.text if hasattr(elem, 'text') else '',
                depth=0,
                error=f"{type(exc).__name__}: {exc}",
            )
        results.append(result)
        explored_count += 1
        sel = elem.selector if hasattr(elem, 'selector') else str(elem)
        explored_selectors.add(sel)

        # Recursive depth exploration
        if result.new_elements and explored_count < MAX_ELEMENTS:
            try:
                depth_results = await asyncio.wait_for(
                    _explore_depth(
                        page, result.new_elements, explore_dir, ai_client,
                        depth=1, explored_count=explored_count,
                        original_url=original_url, progress_callback=progress_callback,
                        explored_selectors=explored_selectors,
                    ),
                    timeout=300,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "PHASE 2: TIMEOUT in depth exploration for %s "
                    "(stuck >300s exploring submenu elements). Skipping depth.",
                    (elem.text if hasattr(elem, 'text') else str(elem)),
                )
                depth_results = []
            results.extend(depth_results)
            explored_count += len(depth_results)

    # Record exploration completeness
    completions = getattr(capture_data, "capture_completions", {})
    if hit_limit:
        completions["phase2_exploration"] = f"incomplete ({explored_count}/{len(explorable)})"
    else:
        completions["phase2_exploration"] = "ok"
    capture_data.capture_completions = completions

    # Map results to CaptureData
    capture_data.exploration_results = [_result_to_dict(r) for r in results]

    # Build hover_content from exploration (legacy field)
    hover_content = []
    for r in results:
        if r.interaction_response in ("tooltip", "dropdown", "submenu", "overlay"):
            hover_content.append({
                "selector": r.selector,
                "text": r.text,
                "title_attr": "",
                "new_elements_count": len(r.new_elements),
                "hover_content": r.interaction_response,
                "screenshot_path": r.hover_screenshot,
            })
    capture_data.hover_content = hover_content

    # Build exploration_screenshots mapping — ALL screenshots per element
    screenshots = {}
    for r in results:
        paths = [s.path for s in r.screenshots if s.path]
        if paths:
            screenshots[r.selector] = paths
    capture_data.exploration_screenshots = screenshots

    # Save exploration log
    log_path = os.path.join(captures_dir, "phase2_exploration.json")
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump([_result_to_dict(r) for r in results], f, indent=2, default=str)

    elapsed = time.monotonic() - phase_start
    capture_data.phase_timings["phase2"] = round(elapsed, 1)

    # Summary breakdown for debugging
    response_counts: dict[str, int] = {}
    error_count = 0
    for r in results:
        response_counts[r.interaction_response] = response_counts.get(r.interaction_response, 0) + 1
        if r.error:
            error_count += 1
    logger.info("PHASE 2 COMPLETE: %d elements explored, %d hover content, %.1fs",
                explored_count, len(hover_content), elapsed)
    logger.info("PHASE 2 RESULTS: %s", ", ".join(
        f"{k}={v}" for k, v in sorted(response_counts.items(), key=lambda x: -x[1])))
    if error_count:
        logger.info("PHASE 2 ERRORS: %d elements had errors (not found/not visible/scroll failed)",
                     error_count)

    return results


async def _explore_element(
    page: Page,
    elem: InventoryElement | dict,
    explore_dir: str,
    ai_client: Any,
    depth: int = 0,
    explored_count: int = 0,
    original_url: str = "",
) -> ExplorationResult:
    """Execute the 3-screenshot cycle for one element."""
    selector = elem.selector if hasattr(elem, "selector") else elem.get("selector", "")
    text = elem.text if hasattr(elem, "text") else elem.get("text", "")
    actions = elem.exploration_actions if hasattr(elem, "exploration_actions") else elem.get("exploration_actions", ["hover", "click"])

    result = ExplorationResult(selector=selector, text=text, depth=depth)
    screenshot_count = 0

    def _ss_path(state_name: str) -> str:
        nonlocal screenshot_count
        screenshot_count += 1
        return os.path.join(elem_dir, f"{screenshot_count:02d}_{state_name}.png")

    async def _capture_state(state: str, description: str, action: str = "") -> str:
        """Capture a screenshot and add it to the result's screenshot list."""
        path = _ss_path(state)
        await page.screenshot(path=path)
        result.screenshots.append(StateScreenshot(
            path=path, state=state, description=description, action_taken=action,
        ))
        logger.debug("PHASE 2: Screenshot %d: %s — %s", screenshot_count, state, description)
        return path

    # Create directory for this element's screenshots
    safe_name = re.sub(r"[^\w\-.]", "_", selector)[:60]
    elem_dir = os.path.join(explore_dir, f"elem_{explored_count:04d}_{safe_name}")
    os.makedirs(elem_dir, exist_ok=True)

    try:
        el = await page.query_selector(selector)
        if not el:
            result.error = f"Element not found: {selector}"
            return result

        is_visible = await el.is_visible()
        if not is_visible:
            result.error = "Element not visible"
            return result

        # Scroll into view
        try:
            await el.scroll_into_view_if_needed(timeout=SCROLL_TIMEOUT_MS)
        except Exception:
            result.error = "Could not scroll to element"
            return result
        await page.wait_for_timeout(200)

        # Re-query the element after scroll — the reference can go stale
        # if scrolling triggered lazy loading or DOM mutations
        el_fresh = await page.query_selector(selector)
        if el_fresh:
            el = el_fresh

        # Screenshot: INITIAL state (always)
        await _capture_state("initial", f"Before any interaction with {text}")

        # Screenshot: HOVER state
        if "hover" in actions:
            try:
                await el.hover(timeout=3000)
                await page.wait_for_timeout(HOVER_WAIT_MS)
                await _capture_state("hover", f"After hovering over {text}", "hover")
            except Exception as e:
                logger.debug("PHASE 2: Hover failed for %s: %s", selector, e)
                # Try re-querying and hovering again — element may have moved
                try:
                    el = await page.query_selector(selector)
                    if el:
                        await el.hover(timeout=3000)
                        await page.wait_for_timeout(HOVER_WAIT_MS)
                        await _capture_state("hover", f"After hovering over {text} (retry)", "hover")
                except Exception:
                    pass  # best-effort hover retry — element may have detached or moved

        # Execute ALL actions and screenshot after EACH one
        last_action = "none"
        action_index = 0
        for action_name in actions:
            if action_name == "hover":
                continue  # Already done above

            action_index += 1
            url_before = page.url

            try:
                if action_name == "click":
                    await el.click(timeout=5000)
                    last_action = "click"
                elif action_name == "focus":
                    await el.focus()
                    last_action = "focus"
                elif action_name == "enter":
                    await el.focus()
                    await page.keyboard.press("Enter")
                    last_action = "enter"
                elif action_name == "space":
                    await el.focus()
                    await page.keyboard.press(" ")
                    last_action = "space"
                elif action_name == "expand":
                    await el.click(timeout=5000)
                    last_action = "expand"
                elif action_name == "arrow_keys":
                    await el.focus()
                    for key in ["ArrowDown", "ArrowDown", "ArrowDown"]:
                        await page.keyboard.press(key)
                        await page.wait_for_timeout(300)
                    last_action = "arrow_keys"
                elif action_name == "escape":
                    await page.keyboard.press("Escape")
                    last_action = "escape"
                else:
                    continue

                await page.wait_for_timeout(CLICK_WAIT_MS)

                # Check if we navigated away (handle both full nav and SPA routing)
                url_changed = page.url != url_before
                if url_changed:
                    # Check if this is an SPA route change (same origin, DOM root intact)
                    # vs a full page navigation
                    try:
                        same_origin = (
                            page.url.split("/")[2] == url_before.split("/")[2]
                        )
                        # If same origin, it might be SPA — capture new state
                        # but only go back if it's truly a different page
                        has_same_root = await page.evaluate("""
                            () => document.querySelector('main, [role="main"], #content, .content') !== null
                        """)
                    except Exception:
                        same_origin = False
                        has_same_root = False

                    if same_origin and has_same_root:
                        # SPA route change — capture but don't go back
                        logger.info("PHASE 2: SPA route change to %s — capturing new state",
                                    page.url)
                        await _capture_state(
                            f"spa_route_{action_index}",
                            f"SPA navigated to {page.url} after {action_name}",
                            action_name,
                        )
                        result.interaction_response = "state_change"
                        # Navigate back for next element
                        try:
                            await page.go_back(wait_until="domcontentloaded", timeout=10000)
                            await page.wait_for_timeout(500)
                        except Exception:
                            pass  # cleanup — best-effort go_back after state change, page state may be unrecoverable
                    else:
                        # Full page navigation — capture and go back
                        logger.info("PHASE 2: Full navigation to %s — capturing then going back",
                                    page.url)
                        await _capture_state(
                            f"navigated_{action_index}",
                            f"Page navigated to {page.url} after {action_name}",
                            action_name,
                        )
                        result.interaction_response = "navigation"
                        try:
                            await page.go_back(wait_until="domcontentloaded", timeout=10000)
                            await page.wait_for_timeout(500)
                        except Exception:
                            pass  # cleanup — best-effort go_back after navigation, page state may be unrecoverable
                    break  # Stop exploring this element either way

                # Screenshot after EVERY action that might change the visual state
                await _capture_state(
                    f"{action_name}_{action_index}",
                    f"After {action_name} on {text}",
                    action_name,
                )

            except Exception as e:
                logger.debug("PHASE 2: Action '%s' failed for %s: %s", action_name, selector, e)

        # Pixel-diff gate: only call the LLM if screenshots show a
        # visual change. This saves an LLM call per element when hover/
        # click produced no visible effect (the common case for simple
        # links and buttons). Identical screenshots are deleted to save
        # disk space.
        screenshot_paths = [s.path for s in result.screenshots]
        visual_change_detected = False

        if len(screenshot_paths) >= 2:
            initial_path = screenshot_paths[0]
            kept_screenshots = [result.screenshots[0]]

            for ss in result.screenshots[1:]:
                if _screenshots_differ(initial_path, ss.path):
                    visual_change_detected = True
                    kept_screenshots.append(ss)
                else:
                    try:
                        os.remove(ss.path)
                    except OSError:
                        pass
                    logger.debug(
                        "PHASE 2: %s screenshot identical to initial — deleted %s",
                        ss.state, os.path.basename(ss.path),
                    )

            result.screenshots = kept_screenshots
            screenshot_paths = [s.path for s in result.screenshots]

        if visual_change_detected and len(screenshot_paths) >= 2 and ai_client:
            elem_rect = None
            if hasattr(elem, 'rect'):
                elem_rect = elem.rect
            elif isinstance(elem, dict):
                elem_rect = elem.get('rect')

            enhanced_paths = []
            try:
                from analysis.image_annotator import annotate_screenshot as _annotate
                from PIL import Image as _PILImage

                import re as _re
                safe_id = _re.sub(r'[^\w\-]', '_', selector[:20])

                for ss_idx, ss_path in enumerate(screenshot_paths):
                    if elem_rect and elem_rect.get('width', 0) > 0:
                        annotated = _annotate(
                            ss_path,
                            [{"rect": elem_rect, "_bb_label": 1}],
                            f"p2_{safe_id}_{ss_idx}",
                            elem_dir,
                        )
                        if annotated:
                            enhanced_paths.append(annotated)

                        try:
                            img = _PILImage.open(ss_path)
                            x = max(0, int(elem_rect['x']) - 50)
                            y = max(0, int(elem_rect['y']) - 50)
                            x2 = min(img.width, int(elem_rect['x'] + elem_rect['width']) + 50)
                            y2 = min(img.height, int(elem_rect['y'] + elem_rect['height']) + 50)
                            if x2 > x and y2 > y:
                                cropped = img.crop((x, y, x2, y2))
                                crop_path = ss_path.replace('.png', '_crop.png')
                                cropped.save(crop_path)
                                enhanced_paths.append(crop_path)
                        except Exception:
                            pass  # best-effort — fall through to original full screenshot if crop fails
                    else:
                        enhanced_paths.append(ss_path)
            except ImportError:
                logger.debug("PHASE 2: image_annotator not available, using raw screenshots")
                enhanced_paths = screenshot_paths
            except Exception as ann_err:
                logger.warning("PHASE 2: Annotation failed for %s: %s", selector, ann_err)
                enhanced_paths = screenshot_paths

            if not enhanced_paths:
                enhanced_paths = screenshot_paths

            ai_screenshot_paths = enhanced_paths

            labels = []
            for s in result.screenshots:
                labels.append(f"{s.state}: {s.description}")
                if elem_rect and elem_rect.get('width', 0) > 0:
                    labels.append(f"{s.state} (close-up crop of element)")
            labels = labels[:len(ai_screenshot_paths)]

            ai_result = await _ai_analyze_exploration(
                ai_client, selector, text, last_action, ai_screenshot_paths, labels,
            )
            if not ai_result:
                import asyncio as _aio
                logger.info("PHASE 2: Retrying AI for %s after 5s...", selector)
                await _aio.sleep(5)
                ai_result = await _ai_analyze_exploration(
                    ai_client, selector, text, last_action, ai_screenshot_paths, labels,
                )

            if ai_result:
                result.interaction_response = ai_result.get("interaction_response", result.interaction_response)
                result.new_elements = ai_result.get("new_elements_found", [])
                result.focus_indicator_visible = ai_result.get("focus_indicator_visible", False)
                result.state_change_detected = ai_result.get("state_change_detected", False)
                result.accessibility_observations = ai_result.get("accessibility_observations", [])

                # Reconcile contradiction: observed 2026-04-23 on a university site
                # that the AI sometimes picks ``interaction_response=
                # "focus_visible"`` (categorical) but sets
                # ``focus_indicator_visible=false`` (boolean) in the
                # same response -- two fields answering the same
                # question with opposite answers. The categorical is
                # the stronger signal (it's one of 13 enum values the
                # AI chose ONE of), so promote the boolean to match
                # when the categorical claims focus visibility. Also
                # log so we can measure how often this fires.
                if (
                    result.interaction_response == "focus_visible"
                    and not result.focus_indicator_visible
                ):
                    logger.info(
                        "PHASE 2: reconciling contradiction on %s -- "
                        "interaction_response=focus_visible but "
                        "focus_indicator_visible was false; setting "
                        "boolean to true to match categorical.",
                        selector,
                    )
                    result.focus_indicator_visible = True
                # Converse: if categorical is "none" and action was
                # focus, there definitively was no visible indicator.
                if (
                    result.interaction_response == "none"
                    and last_action == "focus"
                    and result.focus_indicator_visible
                ):
                    logger.info(
                        "PHASE 2: reconciling contradiction on %s -- "
                        "interaction_response=none but "
                        "focus_indicator_visible was true on a focus "
                        "action; setting boolean to false.",
                        selector,
                    )
                    result.focus_indicator_visible = False

                if result.accessibility_observations:
                    logger.info("PHASE 2: Observations for %s: %s",
                                selector, "; ".join(result.accessibility_observations))
            else:
                logger.warning("PHASE 2: AI returned no result for %s after retry (%d screenshots)",
                               selector, len(screenshot_paths))
        elif not visual_change_detected and len(screenshot_paths) >= 1:
            logger.info(
                "PHASE 2: No visual change for %s — skipped LLM call (saved %d identical screenshots)",
                selector,
                len([s.path for s in result.screenshots]) - 1 if len(result.screenshots) > 1 else 0,
            )
        elif len(screenshot_paths) < 2:
            if not result.error:
                result.error = "All interactions failed — only initial screenshot captured"
            logger.info("PHASE 2: Skipped AI analysis for %s — only %d screenshot(s)",
                        selector, len(screenshot_paths))

        # Try to close any opened menus/modals
        if result.interaction_response in ("dropdown", "modal", "submenu", "overlay"):
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
                # Screenshot the closed state too
                await _capture_state("after_escape", "After pressing Escape to close", "escape")
            except Exception:
                pass  # cleanup — best-effort Escape press to dismiss overlay, page state may be unrecoverable

    except Exception as e:
        result.error = str(e)
        logger.warning("PHASE 2: Error exploring %s: %s", selector, e)

    logger.info("PHASE 2: Element %s — %d screenshots captured, response=%s",
                selector, len(result.screenshots), result.interaction_response)

    return result


async def _explore_depth(
    page: Page,
    new_elements: list[dict],
    explore_dir: str,
    ai_client: Any,
    depth: int,
    explored_count: int,
    original_url: str,
    progress_callback=None,
    explored_selectors: set[str] | None = None,
) -> list[ExplorationResult]:
    """Recursively explore newly discovered elements."""
    if depth > MAX_DEPTH:
        logger.info("PHASE 2: Max depth %d reached", MAX_DEPTH)
        return []

    if explored_selectors is None:
        explored_selectors = set()

    results = []
    explorable = [e for e in new_elements if isinstance(e, dict) and e.get("should_explore", False)]

    # Filter out already-explored selectors to prevent cycles
    before = len(explorable)
    explorable = [e for e in explorable if e.get("selector", "") not in explored_selectors]
    if before > len(explorable):
        logger.info(
            "PHASE 2 DEPTH %d: skipped %d already-explored selectors (cycle prevention)",
            depth, before - len(explorable),
        )

    logger.info("PHASE 2 DEPTH %d: %d new elements to explore", depth, len(explorable))

    for elem_dict in explorable:
        if explored_count + len(results) >= MAX_ELEMENTS:
            break

        if progress_callback:
            await progress_callback(
                f"Phase 2: Depth {depth} — exploring \"{elem_dict.get('text', '')}\"..."
            )

        # Create a proper InventoryElement for the new element
        elem = InventoryElement(
            selector=elem_dict.get("selector", ""),
            text=elem_dict.get("text", ""),
            type=elem_dict.get("type", "unknown"),
            exploration_actions=["hover", "click"],
            interactive=True,
        )

        try:
            result = await asyncio.wait_for(
                _explore_element(
                    page, elem, explore_dir, ai_client,
                    depth=depth, explored_count=explored_count + len(results),
                    original_url=original_url,
                ),
                timeout=120,
            )
        except asyncio.TimeoutError:
            sel = elem_dict.get("selector", "?")
            txt = elem_dict.get("text", "?")
            logger.error(
                "PHASE 2 DEPTH %d: TIMEOUT exploring \"%s\" (%s) "
                "(stuck >120s). Skipping this submenu element.",
                depth, txt, sel,
            )
            result = ExplorationResult(
                selector=sel, text=txt, depth=depth,
                error=f"Timeout after 120s at depth {depth}",
            )
        results.append(result)
        explored_selectors.add(elem_dict.get("selector", ""))

        # Recurse if more new elements found
        if result.new_elements and depth + 1 <= MAX_DEPTH:
            try:
                sub_results = await asyncio.wait_for(
                    _explore_depth(
                        page, result.new_elements, explore_dir, ai_client,
                        depth=depth + 1, explored_count=explored_count + len(results),
                        original_url=original_url, progress_callback=progress_callback,
                        explored_selectors=explored_selectors,
                    ),
                    timeout=300,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "PHASE 2 DEPTH %d: TIMEOUT in sub-depth exploration "
                    "(stuck >300s). Skipping deeper levels.",
                    depth + 1,
                )
                sub_results = []
            results.extend(sub_results)

    return results


async def _ai_analyze_exploration(
    ai_client,
    selector: str,
    text: str,
    action: str,
    screenshot_paths: list[str],
    screenshot_labels: list[str] | None = None,
) -> dict | None:
    """Send exploration screenshots to AI and return structured analysis.

    Uses LLMClient.call_with_tools() — one call path, one parser, handles
    all model formats (Gemini, local Gemma, Qwen, etc.) automatically.

    For local vLLM: routes to Gemma E4B explorer model. If the explorer
    returns prose instead of a tool call, sends that prose to the text
    model (Qwen 35B) for structuring into a proper tool response.
    """
    from functions.llm import LLMClient

    try:
        from config import (
            AI_API_KEY, AI_BACKEND, AI_EXPLORER_MODEL, AI_EXPLORER_URL,
        )
    except ImportError:
        AI_BACKEND = "gemini"
        AI_EXPLORER_URL = ""
        AI_EXPLORER_MODEL = ""
        AI_API_KEY = ""

    # Select model: local explorer (Gemma E4B) or primary API. The
    # restructure-on-failure path inside call_with_tools routes prose
    # to AI_FALLBACK_URL automatically -- no need to wire it here.
    if AI_BACKEND == "vllm" and AI_EXPLORER_URL:
        model_url = AI_EXPLORER_URL
        model_name = AI_EXPLORER_MODEL
        api_key = ""
    else:
        model_url = None
        model_name = None
        api_key = AI_API_KEY

    labels = screenshot_labels or [f"State {i + 1}" for i in range(len(screenshot_paths))]

    # Pull the ordered list of action states actually represented in the
    # screenshots from the labels. This avoids the prior misleading
    # "ACTION: <last>" framing which led the model to assume the last
    # screenshot reflected the LAST action's outcome (e.g. claiming
    # "escape failed to close the menu" when no post-escape screenshot
    # was attached at all).
    state_labels_only = [
        L.split(":")[0].strip()
        for L in labels[:len(screenshot_paths)]
        if "(close-up crop" not in L
    ]
    actions_attached = ", ".join(state_labels_only) or action

    non_initial_states = [s for s in state_labels_only if s and s != "initial"]
    if not non_initial_states:
        logger.debug(
            "PHASE 2 AI: skipping LLM call for %s — only 'initial' state captured, "
            "no interaction to analyze", selector,
        )
        return None

    user_prompt = (
        f"ELEMENT: {selector}\n"
        f"TEXT: \"{text}\"\n"
        f"ACTIONS PERFORMED IN ORDER (each screenshot shows state AFTER that action): {actions_attached}\n"
        f"SCREENSHOTS: {len(screenshot_paths)} images attached.\n\n"
    )
    for i, label in enumerate(labels[:len(screenshot_paths)]):
        user_prompt += f"  Image {i + 1}: {label}\n"
    user_prompt += (
        "\nCOMPARE the screenshots. Look for: new content (dropdown, tooltip, "
        "modal), focus indicators, color changes, content shifts, overlays, "
        "carousel moves, error messages.\n\n"
        "EVIDENCE RULES (CRITICAL):\n"
        "1. Make claims ONLY about states whose screenshots are attached. "
        "If a state name (e.g. 'escape', 'after_escape') does NOT appear in "
        "the SCREENSHOTS list above, do NOT claim observations about the "
        "outcome of that action -- you have no evidence for it.\n"
        "2. When commenting on dismissibility (e.g. 'Escape did not close X'), "
        "you MUST cite a specific attached screenshot label as evidence. If "
        "no post-Escape screenshot is attached, do not make dismissibility "
        "claims at all.\n"
        "3. Mouse-hover-opened menus (CSS :hover state) and JavaScript-opened "
        "menus (click/Enter triggering aria-expanded) are DIFFERENT code paths. "
        "Phase 2 fires actions in sequence without releasing hover state, so a "
        "menu held open by CSS :hover may not respond to Escape -- this is "
        "normal CSS behavior, NOT a keyboard accessibility failure. When "
        "describing dismissibility issues, state which trigger opened the menu.\n"
        "4. Do NOT cite WCAG SC numbers in observations. The per-SC checks "
        "attribute findings to the right SC themselves; your job is just to "
        "describe what you see.\n\n"
        "If ANY visual difference exists, interaction_response MUST NOT be 'none'.\n"
        "Call report_exploration_result."
    )

    system_prompt = (
        "You are a visual accessibility tester comparing before/after "
        "screenshots of an interaction with a single web element. Your job "
        "is to describe what visibly changed and report any accessibility "
        "issues you notice. Call the report_exploration_result tool with "
        "your observations.\n\n"
        "PROCEDURE\n"
        "1. Compare the attached screenshots pixel-by-pixel for ANY visible "
        "   change: new content, color shift, position change, overlays, "
        "   focus rings, content swap, URL bar change.\n"
        "2. Pick the SINGLE most specific interaction_response value that "
        "   describes what happened.\n"
        "3. Note any new interactive elements that appeared so we can "
        "   explore them next.\n"
        "4. Record accessibility observations when you see problems.\n\n"
        "interaction_response enum -- pick the MOST SPECIFIC that applies:\n"
        "  none            -- screenshots are identical, no visible change\n"
        "  focus_visible   -- only a focus ring/outline appeared on the element\n"
        "  tooltip         -- a small text tooltip appeared near the element\n"
        "  dropdown        -- a <select>-style or custom dropdown menu opened\n"
        "  submenu         -- a navigation submenu slid out from a menu item\n"
        "  modal           -- a dialog, modal, or lightbox appeared over the page\n"
        "  accordion       -- a collapsible panel expanded or collapsed\n"
        "  overlay         -- a general overlay / drawer / off-canvas appeared\n"
        "  tab_panel       -- a tab panel switched its visible content\n"
        "  carousel_change -- a carousel or slider advanced to a different slide\n"
        "  state_change    -- element internal state changed (checked, expanded,\n"
        "                     pressed, color swap, content replaced)\n"
        "  navigation      -- the page navigated to a different URL\n"
        "  error_message   -- a validation or error message appeared\n\n"
        "IMPORTANT: a focus ring appearing on the element IS a change -- "
        "report it as 'focus_visible'. Do NOT report it as 'none'.\n\n"
        "ACCESSIBILITY OBSERVATIONS\n"
        "Record one-line observations for any of the following, citing the\n"
        "specific image label that shows the evidence (e.g. 'Image 3: hover'):\n"
        "- A dropdown opened without aria-expanded going to true\n"
        "- A tooltip appeared without role=tooltip or aria-describedby\n"
        "- A modal appeared without focus moving into it\n"
        "- An element received focus but shows no visible focus indicator\n"
        "  (only when the focus screenshot is attached and looks identical\n"
        "  to the unfocused screenshot — not when no focus screenshot exists)\n"
        "- A keyboard-activated control only responds to mouse\n\n"
        "Avoid subjective adjectives like 'clear', 'thin', 'weak', 'insufficient'\n"
        "in observations -- describe binary observable facts (focus state\n"
        "differs from unfocused state: yes/no) and let downstream SC checks\n"
        "evaluate quality. Keep observations consistent across calls -- the\n"
        "same nav button gets the same characterization.\n\n"
        "explore_deeper -- set to true when new interactive CONTROLS appeared "
        "that toggle further state (e.g. a modal opened a form, an accordion "
        "revealed nested toggles, a tab strip swapped panels). Set to FALSE "
        "when the new elements are plain navigation links (<a href> that "
        "just goes to another page) -- those have nothing further to explore.\n\n"
        "REPORTING new_elements_found CORRECTLY\n"
        "Each entry in new_elements_found describes ONE element. RULES:\n"
        "1. Each entry MUST have its own UNIQUE selector pointing at THAT "
        "   element. Never put the parent button or container selector on "
        "   multiple child entries -- if a submenu opened with 7 links, "
        "   you must produce 7 entries with 7 different selectors, one per "
        "   link (e.g. 'nav li:nth-of-type(1) ul li:nth-of-type(1) a', "
        "   'nav li:nth-of-type(1) ul li:nth-of-type(2) a', ...).\n"
        "2. should_explore is true ONLY for elements that themselves toggle "
        "   more state. Plain <a href> navigation links MUST be should_explore=false.\n"
        "3. type is the actual semantic tag/role: 'a' for links, 'button' "
        "   for buttons, 'input' for fields. Not the visual style."
    )

    # Create LLM client for this call
    explorer = LLMClient(
        base_url=model_url,
        model=model_name,
        api_key=api_key,
    )

    # call_with_tools handles parser recovery + LLM-based prose
    # restructuring internally (it routes restructuring to AI_FALLBACK_URL
    # when configured, so Gemma E4B prose goes to Qwen 35B for structuring).
    try:
        result = await explorer.call_with_tools(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tool_name="report_exploration_result",
            tool_schema=EXPLORATION_TOOL,
            images=screenshot_paths,
            temperature=0.1,
        )
        if result:
            logger.debug("PHASE 2 AI: Got result for %s: response=%s",
                         selector, result.get("interaction_response", "?"))
            return result
        logger.warning("PHASE 2 AI: No result for %s", selector)
    except Exception as e:
        logger.warning("PHASE 2 AI: Analysis failed for %s: %s", selector, e)

    return None


def _result_to_dict(r: ExplorationResult) -> dict:
    """Convert ExplorationResult to dict for JSON serialization."""
    return {
        "selector": r.selector,
        "text": r.text,
        "depth": r.depth,
        "screenshot_count": len(r.screenshots),
        "screenshots": [
            {"path": s.path, "state": s.state, "description": s.description, "action": s.action_taken}
            for s in r.screenshots
        ],
        "interaction_response": r.interaction_response,
        "new_elements": r.new_elements,
        "focus_indicator_visible": r.focus_indicator_visible,
        "state_change_detected": r.state_change_detected,
        "accessibility_observations": r.accessibility_observations,
        "error": r.error,
    }
