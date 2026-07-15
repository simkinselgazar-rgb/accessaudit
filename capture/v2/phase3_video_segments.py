"""Phase 3: AI-planned video segment recordings.

The AI examines the element inventory and Phase 2 results, then plans
which video segments to record:
- Tab walkthrough (always)
- Form interaction (fill without submit)
- Media playback (captions on/off)
- Menu navigation (keyboard through menus)
- Modal interaction (open/close/focus trap)
- Custom segments as needed

Each segment is recorded as a separate video file.
If a form requires submission for testing, the system pauses and
lets the user interact via a headed browser.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import async_playwright, Page, BrowserContext

from capture.v2.element_inventory import ElementInventory

logger = logging.getLogger(__name__)

SEGMENT_MAX_DURATION = 120  # Max seconds per segment (2 minutes)
ACTION_WAIT_MS = 700       # Wait between actions


@dataclass
class SegmentAction:
    action: str = ""       # "tab", "enter", "click", "type", "arrow_down", etc.
    selector: str = ""     # Target element (empty for keyboard-only)
    value: str = ""        # For "type" actions
    wait_ms: int = 700     # Wait after action
    description: str = ""  # What this action does


@dataclass
class VideoSegment:
    type: str = ""                  # TAB_WALKTHROUGH, FORM_INTERACTION, etc.
    name: str = ""                  # Human-readable name
    target_selectors: list = field(default_factory=list)
    actions: list = field(default_factory=list)  # list of SegmentAction dicts
    duration_estimate: int = 30     # Seconds
    requires_user_pause: bool = False
    pause_reason: str = ""
    # After recording:
    video_path: str = ""
    completed: bool = False
    error: str = ""


# Tool for AI to signal planning is complete
_PLAN_DONE_TOOL = {
    "type": "function",
    "function": {
        "name": "planning_complete",
        "description": (
            "Call this ONLY when you have planned ALL necessary video segments. "
            "Do NOT call this until every interaction type on the page has been covered."
        ),
        "parameters": {
            "type": "object",
            "required": ["total_segments"],
            "properties": {
                "total_segments": {"type": "integer"},
                "summary": {
                    "type": "string",
                    "description": "Brief summary of what was planned and why.",
                },
            },
        },
    },
}

# Tool schema for AI video planner
_VIDEO_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "report_video_plan",
        "description": "Plan which video segments to record for accessibility testing.",
        "parameters": {
            "type": "object",
            "required": ["segments"],
            "properties": {
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["type", "name", "actions"],
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["TAB_WALKTHROUGH", "FORM_INTERACTION",
                                         "MEDIA_PLAYBACK", "MENU_NAVIGATION",
                                         "MODAL_INTERACTION", "ACCORDION_INTERACTION",
                                         "CAROUSEL_INTERACTION", "CUSTOM"],
                            },
                            "name": {"type": "string"},
                            "target_selectors": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "actions": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "action": {
                                            "type": "string",
                                            "enum": ["tab", "shift_tab", "enter", "space",
                                                     "escape", "arrow_up", "arrow_down",
                                                     "arrow_left", "arrow_right",
                                                     "click", "type", "focus", "wait"],
                                        },
                                        "selector": {"type": "string"},
                                        "value": {"type": "string"},
                                        "wait_ms": {"type": "integer"},
                                        "description": {"type": "string"},
                                    },
                                },
                            },
                            "duration_estimate": {"type": "integer"},
                            "requires_user_pause": {"type": "boolean"},
                            "pause_reason": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


async def run_phase3(
    url: str,
    inventory: ElementInventory,
    exploration_results: list[dict],
    capture_data: Any,
    ai_client: Any,
    captures_dir: str,
    progress_callback=None,
    form_pause_callback=None,
) -> list[VideoSegment]:
    """Run Phase 3: AI-planned video segment recordings.

    Args:
        url: Page URL to record
        inventory: Element inventory from Phase 1
        exploration_results: Results from Phase 2
        capture_data: CaptureData being populated
        ai_client: AIClient for planning
        captures_dir: Output directory
        progress_callback: Progress updates
        form_pause_callback: Called when user interaction needed for forms

    Returns:
        List of recorded VideoSegments
    """
    phase_start = time.monotonic()
    logger.info("=" * 60)
    logger.info("PHASE 3: AI-Planned Video Segments")
    logger.info("=" * 60)

    video_dir = os.path.join(captures_dir, "video_segments")
    os.makedirs(video_dir, exist_ok=True)

    # Step 1: AI plans which segments to record
    if progress_callback:
        await progress_callback("Phase 3: AI planning video segments...")

    segments = await _plan_segments(ai_client, inventory, exploration_results)
    logger.info("PHASE 3: AI planned %d video segments", len(segments))
    for s in segments:
        logger.info("  Segment: %s (%s) — %d actions, ~%ds, pause=%s",
                     s.name, s.type, len(s.actions), s.duration_estimate, s.requires_user_pause)

    # Always ensure tab walkthrough exists
    has_tab = any(s.type == "TAB_WALKTHROUGH" for s in segments)
    if not has_tab and any(e.interactive for e in inventory.elements):
        logger.info("PHASE 3: Adding mandatory TAB_WALKTHROUGH segment")
        tab_segment = _create_default_tab_walkthrough(inventory)
        segments.insert(0, tab_segment)

    # Step 2: Record each segment
    recorded = []
    for i, segment in enumerate(segments):
        if progress_callback:
            await progress_callback(
                f"Phase 3: Recording segment {i + 1}/{len(segments)}: {segment.name}..."
            )

        logger.info("PHASE 3 RECORDING [%d/%d]: %s (%s)",
                     i + 1, len(segments), segment.name, segment.type)

        if segment.requires_user_pause and form_pause_callback:
            logger.info("PHASE 3: Pausing for user interaction: %s", segment.pause_reason)
            await form_pause_callback(segment.name, segment.pause_reason, url)

        segment = await _record_segment(url, segment, video_dir, i, captures_dir)
        recorded.append(segment)

        if segment.completed:
            logger.info("PHASE 3 RECORDED: %s → %s", segment.name, segment.video_path)
        else:
            logger.warning("PHASE 3 FAILED: %s — %s", segment.name, segment.error)

    # Step 3: Map to CaptureData
    capture_data.video_segments = [_segment_to_dict(s) for s in recorded]

    # Map tab walkthrough to legacy field
    tab_segments = [s for s in recorded if s.type == "TAB_WALKTHROUGH" and s.completed]
    if tab_segments:
        capture_data.keyboard_walkthrough_video = tab_segments[0].video_path

    # Save plan log
    plan_path = os.path.join(captures_dir, "phase3_video_plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump([_segment_to_dict(s) for s in recorded], f, indent=2, default=str)

    elapsed = time.monotonic() - phase_start
    capture_data.phase_timings["phase3"] = round(elapsed, 1)
    completed = sum(1 for s in recorded if s.completed)
    logger.info("PHASE 3 COMPLETE: %d/%d segments recorded in %.1fs",
                completed, len(recorded), elapsed)

    return recorded


async def _plan_segments(
    ai_client: Any,
    inventory: ElementInventory,
    exploration_results: list[dict],
) -> list[VideoSegment]:
    """Deterministic segment detection + single AI call for action details.

    Step 1: Code determines WHICH segment types are needed based on inventory.
    Step 2: One AI call plans the specific actions for each segment.
    No loop — small context, best quality.
    """
    if not ai_client:
        return [_create_default_tab_walkthrough(inventory)]

    # Step 1: Deterministic — decide which segments are needed
    needed_types = []
    has_interactive = any(e.interactive for e in inventory.elements)
    has_forms = any(e.type == "form_field" for e in inventory.elements)
    has_media = any(e.type == "media" for e in inventory.elements)
    has_menus = any(e.type in ("menu", "dropdown") for e in inventory.elements)
    has_modals = any(e.type == "modal_trigger" for e in inventory.elements)
    has_accordions = any(e.type == "accordion" for e in inventory.elements)
    has_carousels = any(e.type == "carousel" for e in inventory.elements)

    if has_interactive:
        needed_types.append("TAB_WALKTHROUGH")
    if has_forms:
        needed_types.append("FORM_INTERACTION")
    if has_media:
        needed_types.append("MEDIA_PLAYBACK")
    if has_menus:
        needed_types.append("MENU_NAVIGATION")
    if has_modals:
        needed_types.append("MODAL_INTERACTION")
    if has_accordions:
        needed_types.append("ACCORDION_INTERACTION")
    if has_carousels:
        needed_types.append("CAROUSEL_INTERACTION")

    if not needed_types:
        needed_types = ["TAB_WALKTHROUGH"]

    logger.info("PHASE 3: Deterministic planning: %d segment types needed: %s",
                len(needed_types), ", ".join(needed_types))

    # Step 2: Build segments deterministically from inventory
    # No AI dependency — actions derived from element types and selectors
    segments = _build_deterministic_segments(inventory, needed_types)
    if segments:
        logger.info("PHASE 3: Built %d segments deterministically", len(segments))
        return segments

    # Fallback: AI planning if deterministic produced nothing
    logger.info("PHASE 3: Deterministic produced no segments, trying AI planning")
    # Build context
    elem_summary = []
    for e in inventory.elements:
        if e.interactive:
            elem_summary.append(f"  {e.type}: \"{e.text}\" ({e.selector})")

    exploration_summary = []
    for r in exploration_results:
        if r.get("interaction_response", "none") != "none":
            new_elems = r.get("new_elements", [])
            new_elems_str = ", ".join(str(el) for el in new_elems) if new_elems else "none"
            exploration_summary.append(
                f"  {r.get('selector', '?')}: {r.get('interaction_response')} "
                f"(new elements: {new_elems_str})"
            )

    interactive_count = len([e for e in inventory.elements if e.interactive])

    # Build single prompt with ONLY the needed segment types
    initial_prompt = (
        "═══════════════════════════════════════════════════════\n"
        "  TASK: Plan video recordings for accessibility testing\n"
        "═══════════════════════════════════════════════════════\n\n"
        "Call report_video_plan with ALL segments in ONE response.\n\n"

        "═══════════════════════════════════════════════════════\n"
        "  PAGE INFORMATION\n"
        "═══════════════════════════════════════════════════════\n\n"
        f"Page type: {inventory.page_type}\n"
        f"Summary: {inventory.page_summary}\n\n"

        "═══════════════════════════════════════════════════════\n"
        f"  INTERACTIVE ELEMENTS ({interactive_count} total)\n"
        "═══════════════════════════════════════════════════════\n\n"
        + "\n".join(elem_summary) + "\n\n"
    )

    if exploration_summary:
        initial_prompt += (
            "═══════════════════════════════════════════════════════\n"
            "  EXPLORATION RESULTS\n"
            "═══════════════════════════════════════════════════════\n\n"
            + "\n".join(exploration_summary) + "\n\n"
        )

    initial_prompt += (
        "═══════════════════════════════════════════════════════\n"
        f"  SEGMENTS TO PLAN ({len(needed_types)} types)\n"
        "═══════════════════════════════════════════════════════\n\n"
    )
    for st in needed_types:
        desc = {
            "TAB_WALKTHROUGH": "Tab through ALL interactive elements showing focus order",
            "FORM_INTERACTION": "Fill form fields with test data (do NOT submit). Set requires_user_pause=true if submission needed",
            "MEDIA_PLAYBACK": "Play video/audio with captions on and off",
            "MENU_NAVIGATION": "Keyboard through dropdown/mega menus (arrows + enter + escape)",
            "MODAL_INTERACTION": "Open modal, test focus trap, Escape to close",
            "ACCORDION_INTERACTION": "Expand/collapse accordion panels with keyboard",
            "CAROUSEL_INTERACTION": "Navigate carousel/slider with keyboard",
        }.get(st, st)
        initial_prompt += f"  - {st}: {desc}\n"

    initial_prompt += (
        "\n═══════════════════════════════════════════════════════\n"
        "  ACTION\n"
        "═══════════════════════════════════════════════════════\n\n"
        "Call report_video_plan with ALL segments in a single response.\n"
        "For each segment, list the specific keyboard/mouse actions to perform.\n"
    )

    from functions.llm import LLMClient

    system_prompt = (
        "You are planning video recordings for WCAG accessibility testing. "
        "Plan ALL segments in a single report_video_plan call. "
        "Each segment needs specific actions (tab, click, type, arrow keys, etc)."
    )

    all_segments: list[VideoSegment] = []

    try:
        llm = _resolve_llm(ai_client)
        parsed = await llm.call_with_tools(
            system_prompt=system_prompt,
            user_prompt=initial_prompt,
            tool_name="report_video_plan",
            tool_schema=_VIDEO_PLAN_TOOL,
            temperature=0.2,
        )
        if isinstance(parsed, dict):
            batch = _parse_segments_from_dict(parsed)
            all_segments.extend(batch)
            logger.info("PHASE 3 PLANNING: AI planned %d segments", len(batch))
    except Exception as e:
        logger.warning("PHASE 3 PLANNING: AI call failed: %s", e)

    if not all_segments:
        logger.warning("PHASE 3: No segments planned — using default tab walkthrough")
        return [_create_default_tab_walkthrough(inventory)]

    return all_segments


def _resolve_llm(ai_client):
    from functions.llm import LLMClient

    if isinstance(ai_client, LLMClient):
        return ai_client
    inner = getattr(ai_client, "_llm", None)
    if isinstance(inner, LLMClient):
        return inner
    return LLMClient()


def _parse_segments_from_dict(parsed: dict) -> list[VideoSegment]:
    """Convert a parsed tool call dict to VideoSegment objects."""
    segments = []
    for s in parsed.get("segments", []):
        actions = []
        for a in s.get("actions", []):
            actions.append(SegmentAction(
                action=a.get("action", "wait"),
                selector=a.get("selector", ""),
                value=a.get("value", ""),
                wait_ms=a.get("wait_ms", ACTION_WAIT_MS),
                description=a.get("description", ""),
            ))
        segments.append(VideoSegment(
            type=s.get("type", "CUSTOM"),
            name=s.get("name", "Unnamed segment"),
            target_selectors=s.get("target_selectors", []),
            actions=actions,
            duration_estimate=s.get("duration_estimate", 30),
            requires_user_pause=s.get("requires_user_pause", False),
            pause_reason=s.get("pause_reason", ""),
        ))
    return segments


def _create_default_tab_walkthrough(inventory: ElementInventory) -> VideoSegment:
    """Create a default tab walkthrough segment."""
    interactive_count = sum(1 for e in inventory.elements if e.interactive)
    actions = []
    for _ in range(min(interactive_count + 20, 200)):
        actions.append(SegmentAction(action="tab", wait_ms=ACTION_WAIT_MS, description="Tab to next element"))

    return VideoSegment(
        type="TAB_WALKTHROUGH",
        name="Full keyboard tab walkthrough",
        actions=actions,
        duration_estimate=min(len(actions) * ACTION_WAIT_MS // 1000 + 10, SEGMENT_MAX_DURATION),
    )


def _create_menu_segment(inventory: ElementInventory) -> VideoSegment:
    """Create a menu/dropdown navigation segment from inventory."""
    menus = [e for e in inventory.elements
             if e.type in ("menu", "dropdown") and e.visible and e.selector]
    actions = []
    for menu in menus:
        actions.append(SegmentAction(action="click", selector=menu.selector,
                                     wait_ms=ACTION_WAIT_MS, description=f"Open {menu.text or menu.selector}"))
        actions.append(SegmentAction(action="arrow_down", wait_ms=500, description="Navigate down"))
        actions.append(SegmentAction(action="arrow_down", wait_ms=500, description="Navigate down"))
        actions.append(SegmentAction(action="enter", wait_ms=ACTION_WAIT_MS, description="Select item"))
        actions.append(SegmentAction(action="escape", wait_ms=ACTION_WAIT_MS, description="Close menu"))
    return VideoSegment(
        type="MENU_NAVIGATION", name="Dropdown Menu Navigation",
        actions=actions, duration_estimate=min(len(actions) * ACTION_WAIT_MS // 1000 + 5, SEGMENT_MAX_DURATION),
    )


def _create_form_segment(inventory: ElementInventory) -> VideoSegment:
    """Create a form interaction segment from inventory."""
    fields = [e for e in inventory.elements
              if e.type == "form_field" and e.visible and e.selector]
    actions = []
    for field in fields:
        actions.append(SegmentAction(action="focus", selector=field.selector,
                                     wait_ms=500, description=f"Focus {field.label or field.name or field.selector}"))
        test_value = "Test input"
        if field.input_type == "email":
            test_value = "test@example.com"
        elif field.input_type == "tel":
            test_value = "555-0100"
        elif field.input_type == "number":
            test_value = "42"
        actions.append(SegmentAction(action="type", selector=field.selector, value=test_value,
                                     wait_ms=ACTION_WAIT_MS, description=f"Type into {field.label or field.selector}"))
    return VideoSegment(
        type="FORM_INTERACTION", name="Form Field Testing",
        actions=actions, duration_estimate=min(len(actions) * ACTION_WAIT_MS // 1000 + 5, SEGMENT_MAX_DURATION),
    )


def _create_modal_segment(inventory: ElementInventory) -> VideoSegment:
    """Create a modal interaction segment from inventory."""
    triggers = [e for e in inventory.elements
                if e.type == "modal_trigger" and e.visible and e.selector]
    actions = []
    for trigger in triggers:
        actions.append(SegmentAction(action="click", selector=trigger.selector,
                                     wait_ms=1000, description=f"Open modal: {trigger.text or trigger.selector}"))
        # Tab inside modal to test focus trap
        for _ in range(5):
            actions.append(SegmentAction(action="tab", wait_ms=500, description="Tab inside modal"))
        actions.append(SegmentAction(action="escape", wait_ms=ACTION_WAIT_MS, description="Close modal with Escape"))
    return VideoSegment(
        type="MODAL_INTERACTION", name="Modal Focus Trap Testing",
        actions=actions, duration_estimate=min(len(actions) * ACTION_WAIT_MS // 1000 + 5, SEGMENT_MAX_DURATION),
    )


def _create_carousel_segment(inventory: ElementInventory) -> VideoSegment:
    """Create a carousel interaction segment from inventory."""
    carousels = [e for e in inventory.elements
                 if e.type == "carousel" and e.visible and e.selector]
    actions = []
    for car in carousels:
        actions.append(SegmentAction(action="focus", selector=car.selector,
                                     wait_ms=ACTION_WAIT_MS, description=f"Focus carousel: {car.selector}"))
        for _ in range(4):
            actions.append(SegmentAction(action="arrow_right", wait_ms=1000, description="Next slide"))
        actions.append(SegmentAction(action="arrow_left", wait_ms=1000, description="Previous slide"))
    return VideoSegment(
        type="CAROUSEL_INTERACTION", name="Carousel Navigation",
        actions=actions, duration_estimate=min(len(actions) * 1000 // 1000 + 5, SEGMENT_MAX_DURATION),
    )


def _build_deterministic_segments(inventory: ElementInventory, needed_types: list[str]) -> list[VideoSegment]:
    """Build all video segments deterministically from inventory data.

    No AI needed — actions are derived from element types and attributes.
    """
    segments = []
    builders = {
        "TAB_WALKTHROUGH": lambda: _create_default_tab_walkthrough(inventory),
        "FORM_INTERACTION": lambda: _create_form_segment(inventory),
        "MENU_NAVIGATION": lambda: _create_menu_segment(inventory),
        "MODAL_INTERACTION": lambda: _create_modal_segment(inventory),
        "CAROUSEL_INTERACTION": lambda: _create_carousel_segment(inventory),
    }
    for seg_type in needed_types:
        builder = builders.get(seg_type)
        if builder:
            seg = builder()
            if seg.actions:  # Only add if there are actual actions
                segments.append(seg)
    return segments


async def _record_segment(
    url: str,
    segment: VideoSegment,
    video_dir: str,
    index: int,
    captures_dir: str,
) -> VideoSegment:
    """Record a single video segment."""
    safe_name = re.sub(r"[^\w\-]", "_", segment.name)[:40]
    video_filename = f"{index:02d}_{segment.type.lower()}_{safe_name}.webm"

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                record_video_dir=video_dir,
                record_video_size={"width": 1280, "height": 720},
                ignore_https_errors=True,
            )

            # Check for auth state
            from capture.auth import get_auth_state_path
            auth_state = get_auth_state_path(os.path.dirname(captures_dir), url=url)
            if auth_state:
                # Re-create context with auth
                await context.close()
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    record_video_dir=video_dir,
                    record_video_size={"width": 1280, "height": 720},
                    ignore_https_errors=True,
                    storage_state=auth_state,
                )

            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1000)

            # Execute ALL actions -- no wall-clock cap. A long walkthrough
            # produces a long video, which video_describer.py then chunks
            # into 60s pieces before sending to the AI. We never want to
            # truncate the walkthrough itself; that would lose tab order
            # information for elements past the cut.
            actions_executed = 0

            for action in segment.actions:
                try:
                    await _execute_action(page, action)
                    actions_executed += 1
                except Exception as e:
                    logger.debug("PHASE 3: Action %s failed: %s", action.action, e)
                    continue

            logger.info("PHASE 3: Executed %d/%d actions for %s",
                         actions_executed, len(segment.actions), segment.name)

            # Extract tab walk data BEFORE closing the page
            if segment.type == "TAB_WALKTHROUGH":
                # Re-navigate and extract tab walk cleanly (the recording
                # already captured the visual, now we need the data)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    await page.wait_for_timeout(1000)
                    result = await _extract_tab_walk_data(page)
                    tab_walk_list = result.get("tab_walk", [])
                    keyboard_traps = result.get("keyboard_traps", [])
                    # Save tab walk elements (list) for SC 2.4.7 etc.
                    tab_path = os.path.join(captures_dir, "tab_walk.json")
                    with open(tab_path, "w", encoding="utf-8") as f:
                        json.dump(tab_walk_list, f, indent=2, default=str)
                    # Save keyboard traps (list) for SC 2.1.2
                    if keyboard_traps:
                        traps_path = os.path.join(captures_dir, "keyboard_traps.json")
                        with open(traps_path, "w", encoding="utf-8") as f:
                            json.dump(keyboard_traps, f, indent=2, default=str)
                        logger.info("PHASE 3: %d keyboard traps detected", len(keyboard_traps))
                except Exception as e:
                    logger.warning("PHASE 3: Tab walk extraction failed: %s", e)

            # Close and get video path
            video_path_obj = page.video
            await page.close()
            await context.close()
            await browser.close()

            if video_path_obj:
                raw_path = await video_path_obj.path()
                # Rename to our preferred filename
                final_path = os.path.join(video_dir, video_filename)
                try:
                    os.rename(raw_path, final_path)
                except Exception:
                    final_path = raw_path

                segment.video_path = str(final_path)
                segment.completed = True
            else:
                segment.error = "No video recorded"

    except Exception as e:
        segment.error = str(e)
        logger.warning("PHASE 3: Recording failed for %s: %s", segment.name, e)

    return segment


async def _execute_action(page: Page, action: SegmentAction) -> None:
    """Execute a single action during recording."""
    key_map = {
        "tab": "Tab",
        "shift_tab": "Shift+Tab",
        "enter": "Enter",
        "space": " ",
        "escape": "Escape",
        "arrow_up": "ArrowUp",
        "arrow_down": "ArrowDown",
        "arrow_left": "ArrowLeft",
        "arrow_right": "ArrowRight",
    }

    if action.action == "click" and action.selector:
        el = page.locator(action.selector).first
        if await el.is_visible():
            await el.click(timeout=5000)
    elif action.action == "type" and action.selector:
        el = page.locator(action.selector).first
        if await el.is_visible():
            await el.fill(action.value or "Test input", timeout=5000)
    elif action.action == "focus" and action.selector:
        el = page.locator(action.selector).first
        await el.focus(timeout=5000)
    elif action.action == "wait":
        pass  # Just wait
    elif action.action in key_map:
        await page.keyboard.press(key_map[action.action])
    else:
        logger.debug("PHASE 3: Unknown action: %s", action.action)

    await page.wait_for_timeout(action.wait_ms)


async def _extract_tab_walk_data(page: Page) -> dict:
    """Extract tab walk data during the tab walkthrough recording.

    This populates the legacy capture_data.tab_walk field that
    keyboard accessibility checks depend on.

    For each focused element, checks CSS properties (outline, box-shadow,
    border, background-color) to determine whether a visible focus
    indicator is present — matching the v1 interactive_capture logic.

    Also detects keyboard traps:
      - Consecutive: same element focused 5+ times in a row
      - Cycling: A-B-A-B-A pattern detected
      - Frequency: same element 4+ times in last 8 steps

    Returns a dict with keys "tab_walk" (list) and "keyboard_traps" (list).
    """
    TRAP_CONSECUTIVE_THRESHOLD = 5
    TRAP_CYCLE_REPEATS = 5

    try:
        # Focus body first
        await page.evaluate("document.body.focus()")
        await page.wait_for_timeout(500)

        tab_walk = []
        traps = []
        recent_selectors = []
        seen_selectors = set()
        max_tabs = 300

        for i in range(max_tabs):
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(500)

            info = await page.evaluate("""() => {
                const el = document.activeElement;
                if (!el || el === document.body) return null;
                const rect = el.getBoundingClientRect();
                const cs = window.getComputedStyle(el);
                const outline = cs.outline || '';
                const outlineWidth = cs.outlineWidth || '0px';
                const outlineStyle = cs.outlineStyle || 'none';
                const outlineColor = cs.outlineColor || '';
                const boxShadow = cs.boxShadow || '';
                const borderColor = cs.borderColor || '';
                const borderWidth = cs.borderWidth || '';
                const backgroundColor = cs.backgroundColor || '';
                // Check multiple focus indicator CSS properties
                const hasOutline = outlineStyle !== 'none' && outlineWidth !== '0px';
                const hasShadow = boxShadow !== 'none' && boxShadow !== '';
                const hasBorder = borderWidth !== '0px' && borderColor !== '';
                const isVisible = hasOutline || hasShadow || hasBorder;
                // Build unique selector by walking up DOM
                let selector = '';
                if (el.id) {
                    selector = '#' + CSS.escape(el.id);
                } else {
                    const parts = [];
                    let cur = el;
                    while (cur && cur !== document.body && parts.length < 4) {
                        let seg = cur.tagName.toLowerCase();
                        if (cur.id) { parts.unshift('#' + CSS.escape(cur.id)); break; }
                        if (cur.parentElement) {
                            const sibs = Array.from(cur.parentElement.children).filter(s => s.tagName === cur.tagName);
                            if (sibs.length > 1) seg += ':nth-of-type(' + (sibs.indexOf(cur) + 1) + ')';
                        }
                        parts.unshift(seg);
                        cur = cur.parentElement;
                    }
                    selector = parts.join(' > ');
                }
                const tabindex = el.getAttribute('tabindex');
                return {
                    tag: el.tagName.toLowerCase(),
                    role: el.getAttribute('role') || '',
                    text: (el.textContent || '').trim(),
                    selector: selector,
                    tabindex: tabindex !== null ? tabindex : undefined,
                    has_visible_indicator: isVisible,
                    indicator_type: hasOutline ? 'outline' : hasShadow ? 'box-shadow' : hasBorder ? 'border' : 'none',
                    css: {
                        outline: outline,
                        outlineWidth: outlineWidth,
                        outlineStyle: outlineStyle,
                        outlineColor: outlineColor,
                        boxShadow: boxShadow,
                        borderColor: borderColor,
                        borderWidth: borderWidth,
                        backgroundColor: backgroundColor,
                    },
                    rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                };
            }""")

            if not info:
                break  # Focus returned to body

            # Stop if focus returned to body
            if info["tag"] == "body":
                break

            tab_walk.append(info)
            recent_selectors.append(info["selector"])

            # --- Trap detection (ported from v1 interactive_capture) ---

            # Consecutive same element
            if len(recent_selectors) >= TRAP_CONSECUTIVE_THRESHOLD:
                last_n = recent_selectors[-TRAP_CONSECUTIVE_THRESHOLD:]
                if len(set(last_n)) == 1:
                    traps.append({
                        "type": "consecutive",
                        "selector": info["selector"],
                        "tab_index": i,
                        "description": (
                            f"Element {info['selector']} received focus "
                            f"{TRAP_CONSECUTIVE_THRESHOLD} consecutive times"
                        ),
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
                            "description": f"Focus cycling between {a} and {b}",
                        })
                        break

            # Frequency-based cycle detection — same element at the
            # SAME POSITION 4+ times in last 8 steps.
            # Uses selector + bounding box to distinguish repeated DOM
            # structures (e.g., news article links) from actual traps.
            if len(tab_walk) >= 8:
                last_8 = tab_walk[-8:]
                # Build position key: selector + rounded position
                pos_keys = []
                for tw_entry in last_8:
                    r = tw_entry.get("rect", {})
                    # Round to nearest 10px to handle minor shifts
                    px = round(r.get("x", 0) / 10) * 10
                    py = round(r.get("y", 0) / 10) * 10
                    pos_keys.append(f"{tw_entry.get('selector', '')}@{px},{py}")
                counts = Counter(pos_keys)
                most_common_key, most_common_count = counts.most_common(1)[0]
                if most_common_count >= 4:
                    traps.append({
                        "type": "frequency_cycle",
                        "selector": most_common_key.split("@")[0],
                        "tab_index": i,
                        "description": (
                            f"Element {most_common_key.split('@')[0]} at position "
                            f"{most_common_key.split('@')[1]} received focus "
                            f"{most_common_count} times in the last 8 steps"
                        ),
                    })
                    break

            seen_selectors.add(info["selector"])

        logger.info("PHASE 3: Tab walk extracted %d elements, %d traps",
                     len(tab_walk), len(traps))
        return {"tab_walk": tab_walk, "keyboard_traps": traps}

    except Exception as e:
        logger.warning("PHASE 3: Tab walk extraction failed: %s", e)
        return {"tab_walk": [], "keyboard_traps": []}


def _segment_to_dict(s: VideoSegment) -> dict:
    """Convert VideoSegment to dict for JSON serialization."""
    return {
        "type": s.type,
        "name": s.name,
        "target_selectors": s.target_selectors,
        "action_count": len(s.actions),
        "duration_estimate": s.duration_estimate,
        "requires_user_pause": s.requires_user_pause,
        "pause_reason": s.pause_reason,
        "video_path": s.video_path,
        "completed": s.completed,
        "error": s.error,
    }
