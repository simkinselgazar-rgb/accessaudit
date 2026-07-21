"""Capture-state reload helper for resume support.

`reload_capture_data` rebuilds a `CaptureData` object from the artifacts
saved on disk during a prior capture run. Used when a review is resumed
mid-pipeline (e.g. after a crash, after auth, or by `audit_run.py`).

Tries the fast path first (`capture_data.json` — complete state, every
field) and falls back to piecemeal artifact loading for older reviews
or when the JSON looks empty but disk artifacts are intact.
"""
from __future__ import annotations

import json
import logging
import os

from functions.file_io import load_json_or
from models import CaptureData

logger = logging.getLogger(__name__)


def save_capture_data(
    capture_data: CaptureData,
    captures_dir: str,
    *,
    after_label: str = "",
) -> bool:
    """Atomically write the full CaptureData to captures_dir/capture_data.json.

    Uses a temp file + os.replace so a crash mid-write cannot leave a
    half-flushed file the resume loader cannot parse. Safe to call after
    each capture phase boundary; the symmetric loader is
    reload_capture_data.

    Previously, capture_data.json was first written only inside the
    interactive-tests phase. A crash between end-of-Phase-D and start-of-
    interactive-tests left all the deterministic JSONs (axe / htmlcs /
    ibm_eac / the 6 ANDI files) on disk, but reload_capture_data did NOT
    repopulate those fields from the per-step files. The downstream
    SCs would then evaluate against empty inputs with no warning. This
    helper closes that gap: the v2 orchestrator calls it after every
    phase (D, 1, 2, 3, 4), so a crash at any phase boundary leaves a
    fully reloadable capture_data.json on disk.

    Returns True on success, False on failure. Failures are logged at
    WARNING level with traceback; the caller may continue (resume falls
    back to piecemeal artifact loading).
    """
    cd_path = os.path.join(captures_dir, "capture_data.json")
    tmp_path = cd_path + ".tmp"
    try:
        payload = json.dumps(
            capture_data.to_serializable_dict(), indent=2, default=str,
        )
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, cd_path)
        if after_label:
            logger.info(
                "capture_data.json saved after %s (%d bytes)",
                after_label, len(payload),
            )
        return True
    except Exception:
        logger.warning(
            "capture_data.json save after '%s' FAILED -- a subsequent "
            "crash would lose this phase's state. Continuing.",
            after_label or "?", exc_info=True,
        )
        # Best-effort cleanup of the temp file.
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            logger.debug("Could not remove orphan tmp file %s", tmp_path)
        return False


def reload_capture_data(review_dir: str) -> CaptureData:
    """Reload CaptureData from saved files for resume support.

    Tries capture_data.json first (complete state from the capture
    phase — every field, no data loss). Falls back to piecemeal
    file loading for reviews created before the complete-state save
    was implemented.
    """
    captures_dir = os.path.join(review_dir, "captures")

    # ── Fast path: complete capture_data.json ────────────────────
    cd_path = os.path.join(captures_dir, "capture_data.json")
    if os.path.exists(cd_path):
        try:
            data = json.loads(open(cd_path, encoding="utf-8").read())
            cd = CaptureData.from_serialized_dict(data, review_dir)
            logger.info(
                "Capture data reloaded from capture_data.json: "
                "%d links, %d images, %d tab stops, %d focus_contrast, "
                "%d pixel_contrast, %d aria_issues, DOM=%d chars",
                len(cd.links or []), len(cd.images or []),
                len(cd.tab_walk or []), len(cd.focus_contrast or []),
                len(cd.pixel_contrast or []), len(cd.aria_issues or []),
                len(cd.html or ""),
            )
            # Sanity gate: if the JSON loaded a shell with an empty
            # inventory but the piecemeal artifact files on disk have
            # real data, DON'T return the shell. A failed previous
            # resume can save an empty capture_data.json that looks
            # fine to from_serialized_dict but contains no links,
            # images, or form fields -- every SC then evaluates Not
            # Applicable (observed on review
            # 20260423_041233_4ae3d812).  Trust the disk artifacts.
            inv_on_disk = os.path.exists(
                os.path.join(captures_dir, "element_inventory.json")
            )
            dom_on_disk = os.path.exists(
                os.path.join(captures_dir, "dom.html")
            )
            looks_empty = (
                not cd.links and not cd.images and not cd.form_fields
                and not cd.headings
            )
            if looks_empty and inv_on_disk and dom_on_disk:
                logger.warning(
                    "capture_data.json loaded but looks empty (0 links, "
                    "0 images, 0 form fields, 0 headings). Falling "
                    "through to piecemeal reload because "
                    "element_inventory.json and dom.html look intact."
                )
            else:
                return cd
        except Exception as e:
            logger.warning("Reload from capture_data.json failed (%s), falling back to piecemeal", e)

    # ── Fallback: piecemeal loading from individual files ────────
    cd = CaptureData(
        url="",
        review_dir=review_dir,
        captures_dir=captures_dir,
        capture_pipeline_version="v2",
    )

    # Piecemeal artifact loads below each log the path at WARNING on
    # failure and continue — one malformed file must not abort the
    # whole resume (the other artifacts are still good).

    # DOM
    dom_path = os.path.join(captures_dir, "dom.html")
    if os.path.exists(dom_path):
        try:
            with open(dom_path, encoding="utf-8", errors="replace") as fh:
                cd.html = fh.read()
        except OSError:
            logger.warning("Reload: dom.html unreadable at %s", dom_path, exc_info=True)

    # A11y tree
    a11y_path = os.path.join(captures_dir, "a11y_tree.json")
    a11y_tree = load_json_or(a11y_path)
    if a11y_tree is not None:
        cd.a11y_tree = a11y_tree

    # Screenshots
    for name, field in [
        ("full_page.png", "full_page_path"),
        ("viewport.png", "viewport_path"),
        ("viewport_200pct.png", "viewport_200pct_path"),
        ("full_page_200pct.png", "full_page_200pct_path"),
        ("viewport_320px.png", "viewport_320px_path"),
        ("text_spacing_override.png", "text_spacing_screenshot"),
    ]:
        path = os.path.join(captures_dir, name)
        if os.path.exists(path):
            setattr(cd, field, path)

    # Element inventory → links, images, form fields, etc.
    # Older code called ElementInventory.from_file which never
    # existed; rebuild via from_dict(json.loads(...)) which is the
    # actual API. Without this, resume paths silently lost every
    # link/image/form field, causing every SC to evaluate as Not
    # Applicable -- observed on review 20260423_041233_4ae3d812.
    inv_path = os.path.join(captures_dir, "element_inventory.json")
    if os.path.exists(inv_path):
        try:
            from capture.v2.element_inventory import ElementInventory, map_inventory_to_capture_data
            with open(inv_path, "r", encoding="utf-8") as fh:
                inv_dict = json.load(fh)
            inv = ElementInventory.from_dict(inv_dict)
            map_inventory_to_capture_data(inv, cd)
        except Exception as e:
            logger.warning("Reload: element inventory failed: %s", e)

    # Tab walk
    tw_path = os.path.join(captures_dir, "tab_walk.json")
    tab_walk = load_json_or(tw_path)
    if tab_walk is not None:
        cd.tab_walk = tab_walk

    # Keyboard traps
    kt_path = os.path.join(captures_dir, "keyboard_traps.json")
    keyboard_traps = load_json_or(kt_path)
    if keyboard_traps is not None:
        cd.keyboard_traps = keyboard_traps

    # Keyboard walkthrough video
    kw_path = os.path.join(captures_dir, "keyboard_walkthrough", "keyboard_walkthrough.webm")
    if os.path.exists(kw_path):
        cd.keyboard_walkthrough_video = kw_path

    # Observation video
    obs_dir = os.path.join(captures_dir, "observation_video")
    if os.path.exists(obs_dir):
        for f in os.listdir(obs_dir):
            if f.endswith(".webm"):
                cd.observation_video_path = os.path.join(obs_dir, f)
                break

    # Meta for URL/title
    meta_path = os.path.join(review_dir, "meta.json")
    meta = load_json_or(meta_path)
    if meta is not None:
        cd.url = meta.get("source_url", "")
        cd.title = meta.get("product_name", "") or cd.url

    # Derive focus_indicators from tab_walk
    if cd.tab_walk and not cd.focus_indicators:
        focus_indicators = []
        for tw in cd.tab_walk:
            focus_indicators.append({
                "selector": tw.get("selector", ""),
                "tag": tw.get("tag", ""),
                "text": tw.get("text", ""),
                "has_visible_indicator": tw.get("has_visible_indicator"),
                "indicator_type": tw.get("indicator_type", ""),
                "css_unfocused": tw.get("css_unfocused", {}),
                "focus_style_delta": tw.get("focus_style_delta", []),
            })
        cd.focus_indicators = focus_indicators

    logger.info("Capture data reloaded: %d links, %d images, %d tab stops, DOM=%d chars",
                len(cd.links or []), len(cd.images or []),
                len(cd.tab_walk or []), len(cd.html or ""))
    return cd
