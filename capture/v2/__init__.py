"""AccessAudit v2 capture — AI-Driven Capture Pipeline (package re-export shim).

This package's public API is exactly two functions:

  - `capture_web_page_v2(url, review_dir, ...)` — main capture orchestrator
  - `reload_capture_data(review_dir)` — rebuild CaptureData from saved files

Their implementations live in sibling modules so this package's
`__init__` stays a thin shim:

  - capture.v2.orchestrator      -> capture_web_page_v2
  - capture.v2.state             -> reload_capture_data
  - capture.v2.html_extraction   -> _capture_html_media_and_form_attrs
  - capture.v2.v1_compat         -> _run_v1_extractions
  - capture.v2.element_inventory -> ElementInventory + mapping helpers
  - capture.v2.dom_chunker       -> HTML safe-splitting
  - capture.v2.form_pause        -> form-pause coordination
  - capture.v2.phase1_code_analysis  -> Phase 1 sub-pipeline
  - capture.v2.phase2_visual_explorer -> Phase 2 sub-pipeline
  - capture.v2.phase3_video_segments  -> Phase 3 sub-pipeline
  - capture.v2.phase4_at_simulation   -> Phase 4 sub-pipeline

The five capture phases are documented in `docs/ARCHITECTURE.md` §9.
"""
from __future__ import annotations

# Public API
from capture.v2.orchestrator import capture_web_page_v2
from capture.v2.state import reload_capture_data

# Back-compat re-exports for any callers that historically imported the
# underscore-prefixed helpers from this package directly.
from capture.v2.html_extraction import _capture_html_media_and_form_attrs  # noqa: F401
from capture.v2.v1_compat import _run_v1_extractions  # noqa: F401

__all__ = ["capture_web_page_v2", "reload_capture_data"]
