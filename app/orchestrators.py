"""End-to-end review orchestrators.

Three coroutines drive the full audit pipeline:

* :func:`process_review` — single page or single document.
* :func:`process_multi_review` — operator-supplied URL list, no crawl.
* :func:`process_site_review` — full crawl + AI page selection.

All three are dispatched by :func:`process_review`, which is itself
called by :func:`app.queue.queue_worker`. They are pure relocations of
the original functions previously colocated in ``app.py``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import traceback
from pathlib import Path

from config import REVIEWS_DIR
from models import ConformanceLevel, ReviewMeta

from app.cancellation import ReviewCancelled, check_cancelled
from app.summary import _compute_summary, _compute_summary_from_dicts
from app.websocket_manager import broadcast


logger = logging.getLogger(__name__)


async def process_review(review_id: str) -> None:
    """Process a single review end-to-end."""
    review_dir = REVIEWS_DIR / review_id
    from storage.review_store import load_meta, save_meta, save_test_result
    meta = load_meta(review_dir)

    # Import here to avoid circular imports
    from analysis.api_client import AIClient
    from checks.registry import get_checks_for_version
    from report.acr_generator import generate_acr_report
    from verification.verifier import verify_result
    from functions.bypass_log import bind_current_review_dir

    ai_client = AIClient()

    # Bind the review dir for ambient bypass telemetry. Every
    # ``log_bypass()`` call under this review (including deep call
    # paths in llm.py / embeddings.py that don't receive review_dir)
    # will write to ``<review_dir>/bypass_log.jsonl`` automatically.
    # asyncio tasks have their own ContextVar copy, so no unbinding
    # is needed -- the binding dies with this coroutine.
    bind_current_review_dir(review_dir)

    if meta.review_type == "site":
        await process_site_review(review_id, meta, review_dir, ai_client)
        return

    if meta.review_type == "multi":
        await process_multi_review(review_id, meta, review_dir, ai_client)
        return

    # Single page/file review
    capture_data = None

    # Phase: Capturing
    meta.status = "capturing"
    save_meta(review_dir, meta)
    await broadcast(review_id, {"type": "phase", "phase": "capturing"})

    source = meta.source_url or meta.source_file
    file_type = meta.file_type

    logger.info("=" * 70)
    logger.info("REVIEW %s STARTED", review_id)
    logger.info("  Source: %s", source)
    logger.info("  Type: %s | Format: %s | WCAG: %s | Level: %s",
                file_type or "web", meta.report_format, meta.wcag_version, meta.coverage_level)
    logger.info("=" * 70)

    # Resume support: if capture already completed, reload from saved files
    captures_dir = review_dir / "captures"
    dom_path = captures_dir / "dom.html"
    can_resume_capture = (captures_dir / "capture_data.json").exists() or (
        dom_path.exists() and (captures_dir / "a11y_tree.json").exists()
    )

    if can_resume_capture and file_type not in ("pdf", "docx", "xlsx", "pptx"):
        logger.info("RESUMING: Reloading capture data from saved files")
        await broadcast(review_id, {"type": "phase", "phase": "capturing", "message": "Reloading saved capture data..."})
        try:
            from capture.v2 import reload_capture_data
            capture_data = reload_capture_data(str(review_dir))
            logger.info("RESUMING: Capture data reloaded (%d links, %d images)",
                        len(getattr(capture_data, "links", []) or []),
                        len(getattr(capture_data, "images", []) or []))
        except Exception as e:
            logger.warning("RESUMING: Reload failed (%s), re-capturing", e)
            can_resume_capture = False

    # Document captures are cheap local parses, so a resumed document
    # review simply re-captures (capture_data is still None here).
    if capture_data is None:
        try:
            if file_type == "pdf":
                from capture.pdf_capture import capture_pdf
                capture_data = capture_pdf(meta.source_file, str(review_dir))
            elif file_type in ("docx", "xlsx", "pptx"):
                from capture.office_capture import capture_office
                capture_data = capture_office(meta.source_file, str(review_dir))
            else:
                from config import CAPTURE_PIPELINE
                async def _auth_cb(msg):
                    await broadcast(review_id, {"type": "phase", "phase": "authenticating", "message": msg})
                async def _progress_cb(msg):
                    await broadcast(review_id, {"type": "phase", "phase": "capturing", "message": str(msg)})

                if CAPTURE_PIPELINE == "v2":
                    logger.info("Using v2 AI-driven capture pipeline")
                    from capture.v2 import capture_web_page_v2
                    capture_data = await capture_web_page_v2(
                        meta.source_url, str(review_dir), meta.user_context,
                        auth_callback=_auth_cb, progress_callback=_progress_cb,
                        cancel_check=lambda: check_cancelled(review_id),
                    )
                else:
                    from capture.web_capture import capture_web_page
                    capture_data = await capture_web_page(
                        meta.source_url, str(review_dir), meta.user_context,
                        auth_callback=_auth_cb,
                    )
        except ReviewCancelled:
            # A cancel during capture must abort cleanly, not be recorded
            # as a capture error. Re-raise so the queue worker's
            # ReviewCancelled handler marks the review "cancelled".
            raise
        except Exception as e:
            logger.error(f"Capture failed: {e}\n{traceback.format_exc()}")
            meta.status = "error"
            meta.error = f"Capture failed: {e}"
            save_meta(review_dir, meta)
            await broadcast(review_id, {"type": "error", "message": f"Capture failed: {e}"})
            return

    if capture_data is None:
        meta.status = "error"
        meta.error = "Capture returned no data"
        save_meta(review_dir, meta)
        await broadcast(review_id, {"type": "error", "message": "Capture returned no data"})
        return

    # Attach product context to capture_data so every AI call sees it
    if meta.product_context:
        from models import ProductContext
        capture_data.product_context = ProductContext.from_dict(meta.product_context)

    # Attach the review scope so per-SC checks can gate cross-page criteria.
    # A single-page review cannot evaluate criteria that require comparing
    # content across multiple pages (3.2.3 / 3.2.4 / 3.2.6); those checks
    # read this to mark themselves Not Applicable for the review's scope.
    capture_data.review_type = meta.review_type

    # Compute the canonical interactive-target dimension list once. Both
    # the SC 2.5.8 prompt block and the claim validator (SC 2.5.8 / 2.5.5)
    # read this single source so they cannot disagree on what was measured.
    try:
        from functions.target_size import compute_target_size_measurements
        capture_data.target_size_measurements = (
            compute_target_size_measurements(capture_data)
        )
        logger.info(
            "Target-size measurements computed: %d interactive targets",
            len(capture_data.target_size_measurements),
        )
    except Exception:
        logger.warning(
            "Target-size measurement computation failed (non-fatal)",
            exc_info=True,
        )

    # Resume: finish any interactive tests that weren't completed when a
    # prior process exited. Without this step, a server restart during
    # interactive capture would leave tab_walk / tab_coverage / hover /
    # focus_indicators / widget / modal data missing, and every SC that
    # depends on them would evaluate with partial evidence. The
    # run_interactive_tests helper itself skips any step already
    # recorded as "ok" in capture_completions, so re-entering only
    # costs the time of the genuinely unfinished steps.
    if can_resume_capture and file_type not in ("pdf", "docx", "xlsx", "pptx"):
        completions = getattr(capture_data, "capture_completions", {}) or {}
        INTERACTIVE_TESTS = [
            "tab_walk", "backward_tab", "tab_coverage",
            "keyboard_walkthrough", "focus_indicators", "hover_detection",
            "text_spacing", "media_playback", "media_recording",
            "caption_toggle_recording", "transcript_verification",
            "skip_links", "form_submission", "context_changes",
            "audio_detection", "focus_contrast", "form_error_capture",
            "focus_content", "widget_keyboard", "modal_interactions",
            "reduced_motion",
        ]
        done_count = sum(
            1 for t in INTERACTIVE_TESTS if completions.get(t) == "ok"
        )
        missing = [t for t in INTERACTIVE_TESTS if completions.get(t) != "ok"]
        if missing:
            logger.info(
                "RESUMING: interactive capture incomplete -- %d/%d tests "
                "already ok, %d to finish: %s. Re-opening browser to run "
                "remaining tests.",
                done_count, len(INTERACTIVE_TESTS), len(missing),
                ", ".join(missing),
            )
            try:
                from playwright.async_api import async_playwright
                from capture.interactive_capture import run_interactive_tests
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    context = await browser.new_context()
                    page = await context.new_page()
                    await page.goto(meta.source_url, wait_until="domcontentloaded", timeout=60_000)
                    await page.wait_for_timeout(1500)
                    await run_interactive_tests(page, capture_data, str(review_dir))
                    await browser.close()
            except Exception as e:
                logger.exception(
                    "Resume: re-running interactive tests failed (%s). "
                    "SCs that depend on interactive data will evaluate "
                    "with whatever partial data exists.", e,
                )
        else:
            logger.info(
                "RESUMING: all %d interactive tests already completed",
                len(INTERACTIVE_TESTS),
            )

    # Log capture summary
    cap_summary = []
    if capture_data.images: cap_summary.append(f"{len(capture_data.images)} images")
    if capture_data.links: cap_summary.append(f"{len(capture_data.links)} links")
    if capture_data.form_fields: cap_summary.append(f"{len(capture_data.form_fields)} form fields")
    if capture_data.headings: cap_summary.append(f"{len(capture_data.headings)} headings")
    if capture_data.media: cap_summary.append(f"{len(capture_data.media)} media")
    if capture_data.tables: cap_summary.append(f"{len(capture_data.tables)} tables")
    if capture_data.landmarks: cap_summary.append(f"{len(capture_data.landmarks)} landmarks")
    if capture_data.tab_walk: cap_summary.append(f"{len(capture_data.tab_walk)} tab stops")
    logger.info("CAPTURE COMPLETE: %s", ", ".join(cap_summary) or "minimal content")

    # Save complete capture data for resume support — every field, no truncation
    try:
        cd_path = captures_dir / "capture_data.json"
        cd_path.write_text(
            json.dumps(capture_data.to_serializable_dict(), indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("CAPTURE DATA SAVED: %s (%.1f KB)",
                     cd_path, cd_path.stat().st_size / 1024)
    except Exception as e:
        logger.warning("Failed to save capture_data.json (non-fatal): %s", e)
        try:
            from functions.bypass_log import (
                CATEGORY_SKIPPED_DATA, SEVERITY_HIGH, log_bypass,
            )
            log_bypass(
                category=CATEGORY_SKIPPED_DATA,
                severity=SEVERITY_HIGH,
                source="app.py:process_review",
                event="capture_data_save_failed",
                details={
                    "target_path": str(cd_path),
                    "exception_type": type(e).__name__,
                    "exception": str(e),
                },
                outcome="capture_data.json NOT persisted; resume will re-capture the page",
                data_lost=False,
            )
        except Exception:
            logger.exception("bypass_log emit failed after capture_data.json save failure")
    if capture_data.full_page_path:
        logger.info("  Screenshots: full_page=%s", "YES" if capture_data.full_page_path else "NO")
    if capture_data.keyboard_walkthrough_video:
        logger.info("  Keyboard walkthrough video: YES")
    if capture_data.observation_video_path:
        logger.info("  Observation video: YES")
    lang = capture_data.page_language
    if lang:
        logger.info("  Page language: %s (valid=%s)", lang.get("html_lang", "?"), lang.get("lang_valid", "?"))

    # Phase: Video-to-text pre-processing
    # Describe all captured videos ONCE before testing starts.
    # Text descriptions are reused by every check instead of sending raw video.
    #
    # Skip paths (useful when resuming a review whose first try died in the
    # describer's retry loop):
    #   1. Set SKIP_VIDEO_DESCRIBER=1 in the environment.
    #   2. Touch a ``.skip_video_describer`` sentinel file inside the
    #      review directory -- per-review opt-out, no env needed.
    skip_describer_env = str(os.getenv("SKIP_VIDEO_DESCRIBER", "")).lower() in (
        "1", "true", "yes", "on",
    )
    skip_describer_sentinel = (review_dir / ".skip_video_describer").exists()
    skip_describer = skip_describer_env or skip_describer_sentinel
    if skip_describer:
        logger.info(
            "Video describer SKIPPED (env=%s, sentinel=%s) -- SCs will "
            "fall back to raw frame analysis or text-only evidence",
            skip_describer_env, skip_describer_sentinel,
        )
    elif ai_client:
        try:
            from capture.video_describer import describe_all_videos
            await broadcast(review_id, {
                "type": "phase", "phase": "testing",
                "message": "Pre-processing video descriptions...",
            })
            await describe_all_videos(capture_data, ai_client)
            vd = getattr(capture_data, "video_descriptions", {})
            if vd:
                logger.info("Video descriptions ready: %s", ", ".join(vd.keys()))
                # Re-persist the canonical snapshot now that video
                # descriptions have populated. Without this, capture_data.json
                # is missing video_descriptions because it was saved earlier
                # (right after capture) and the describer mutates the in-
                # memory CaptureData afterwards. The standalone
                # captures/video_descriptions.json file still exists either
                # way; this re-save just keeps the canonical bundle complete
                # so a downstream consumer reading only capture_data.json
                # gets every field.
                try:
                    cd_path = review_dir / "captures" / "capture_data.json"
                    cd_path.write_text(
                        json.dumps(capture_data.to_serializable_dict(), indent=2, default=str),
                        encoding="utf-8",
                    )
                except Exception:
                    logger.warning(
                        "Re-persist of capture_data.json after video describer "
                        "failed (non-fatal): video_descriptions still in "
                        "captures/video_descriptions.json"
                    )
        except Exception:
            logger.exception("Video description pre-processing failed (non-fatal)")

    # Phase: Testing
    meta.status = "testing"
    save_meta(review_dir, meta)
    await broadcast(review_id, {"type": "phase", "phase": "testing"})

    checks = get_checks_for_version(meta.wcag_version, meta.coverage_level, file_type=file_type)
    meta.total_criteria = len(checks)
    save_meta(review_dir, meta)

    logger.info("-" * 70)
    logger.info("TESTING PHASE: %d criteria to evaluate", len(checks))
    logger.info("-" * 70)

    import time as _time
    test_phase_start = _time.monotonic()

    results = []

    # Resume support: load already-completed test results
    skipped = 0
    corrupt_result_ids = set()
    for check in checks:
        sc_dir = review_dir / "tests" / check.criterion_id.replace(".", "_")
        result_file = sc_dir / "result.json"
        if result_file.exists():
            try:
                existing = json.loads(result_file.read_text(encoding="utf-8"))
                from models import TestResult as _TR
                r = _TR.from_dict(existing)
                results.append(r)
                skipped += 1
            except Exception:
                corrupt_result_ids.add(check.criterion_id)
                logger.exception(
                    "Resume failed to load existing result %s; will re-run %s",
                    result_file, check.criterion_id,
                )

    if skipped:
        logger.info("RESUMING: %d/%d tests already completed, skipping", skipped, len(checks))
        await broadcast(review_id, {
            "type": "phase", "phase": "testing",
            "message": f"Resuming — {skipped} tests already done, continuing...",
        })

    for idx, check in enumerate(checks):
        # Skip already-completed tests (resume support); a result.json
        # that failed to parse above must still re-run.
        sc_dir = review_dir / "tests" / check.criterion_id.replace(".", "_")
        if (sc_dir / "result.json").exists() and check.criterion_id not in corrupt_result_ids:
            continue

        check_cancelled(review_id)
        check_start = _time.monotonic()
        logger.info(
            "[%d/%d] SC %s %s (Level %s) — starting...",
            idx + 1, len(checks), check.criterion_id, check.criterion_name, check.level,
        )
        await broadcast(review_id, {
            "type": "test_start",
            "index": idx + 1,
            "total": len(checks),
            "criterion_id": check.criterion_id,
            "criterion_name": check.criterion_name,
        })

        try:
            result = await check.run(capture_data, ai_client)

            # Feature E: Confidence-based retry for low-confidence results
            if result.confidence < 0.5 and result.conformance_level != ConformanceLevel.NOT_APPLICABLE:
                logger.info(
                    "Low confidence (%.2f) for %s — retrying with enhanced prompt",
                    result.confidence, check.criterion_id,
                )
                try:
                    retry_result = await check.run(capture_data, ai_client)
                    if retry_result.confidence > result.confidence:
                        result = retry_result
                        logger.info(
                            "Retry improved confidence to %.2f for %s",
                            result.confidence, check.criterion_id,
                        )
                except Exception:
                    logger.debug("Retry failed for %s, keeping original", check.criterion_id)

            # Optional verification (with screenshots for visual confirmation)
            result = await verify_result(result, ai_client, capture_data)

            # Save result
            from storage.review_store import save_test_result
            save_test_result(review_dir, result)
            results.append(result)

            # Detailed logging per check
            elapsed = round(_time.monotonic() - check_start, 1)
            conf_val = result.conformance_level.value if hasattr(result.conformance_level, 'value') else str(result.conformance_level)
            finding_severities = {}
            for f in result.findings:
                sev = f.severity.value if hasattr(f.severity, 'value') else str(f.severity)
                finding_severities[sev] = finding_severities.get(sev, 0) + 1
            sev_str = ", ".join(f"{v}x{k}" for k, v in finding_severities.items()) if finding_severities else "none"

            logger.info(
                "[%d/%d] SC %s → %s (%.0f%% confidence) | %d findings [%s] | "
                "prog=%s ai=%s code_ai=%s | %.1fs",
                idx + 1, len(checks), check.criterion_id,
                conf_val, result.confidence * 100,
                len(result.findings), sev_str,
                result.programmatic_conformance.value if hasattr(result.programmatic_conformance, 'value') else "?",
                result.ai_conformance.value if hasattr(result.ai_conformance, 'value') else "?",
                result.code_ai_conformance.value if hasattr(result.code_ai_conformance, 'value') else "?",
                elapsed,
            )

            await broadcast(review_id, {
                "type": "test_complete",
                "index": idx + 1,
                "total": len(checks),
                "criterion_id": check.criterion_id,
                "criterion_name": check.criterion_name,
                "conformance_level": conf_val,
                "confidence": round(result.confidence, 3),
                "finding_count": len(result.findings),
                "verification_status": result.verification_status,
                "tt_sub_test_count": len(result.tt_results),
            })

        except Exception as e:
            logger.error(f"Check {check.criterion_id} failed: {e}\n{traceback.format_exc()}")
            from models import TestResult as TR
            err_result = TR(
                criterion_id=check.criterion_id,
                criterion_name=check.criterion_name,
                level=check.level,
                wcag_versions=check.wcag_versions,
                conformance_level=ConformanceLevel.NOT_EVALUATED,
                error=str(e),
            )
            from storage.review_store import save_test_result
            save_test_result(review_dir, err_result)
            results.append(err_result)

            await broadcast(review_id, {
                "type": "test_complete",
                "index": idx + 1,
                "total": len(checks),
                "criterion_id": check.criterion_id,
                "criterion_name": check.criterion_name,
                "conformance_level": "Not Evaluated",
                "confidence": 0,
                "finding_count": 0,
                "verification_status": "error",
                "tt_sub_test_count": 0,
            })

    # Cross-criterion verdict consistency: downgrade any stricter SC that
    # out-ranks the easier SC it is a strict superset of (e.g. 2.1.3 cannot
    # pass when 2.1.1 fails). Runs once, after every SC has a verdict.
    try:
        from functions.sc_consistency import reconcile_cross_sc_verdicts
        from storage.review_store import save_test_result as _save_tr
        _pre = {
            getattr(r, "criterion_id", None):
                getattr(r, "conformance_level", None)
            for r in results
        }
        _downgrades = reconcile_cross_sc_verdicts(results)
        if _downgrades:
            for r in results:
                cid = getattr(r, "criterion_id", None)
                if cid and _pre.get(cid) != getattr(r, "conformance_level", None):
                    _save_tr(review_dir, r)
            logger.info(
                "Cross-SC reconciliation: %d verdict(s) downgraded and "
                "re-saved.", _downgrades,
            )
    except Exception:
        logger.warning(
            "Cross-SC verdict reconciliation failed (non-fatal)",
            exc_info=True,
        )

    test_elapsed = round(_time.monotonic() - test_phase_start, 1)
    supports = sum(1 for r in results if hasattr(r, 'conformance_level') and r.conformance_level == ConformanceLevel.SUPPORTS)
    partial = sum(1 for r in results if hasattr(r, 'conformance_level') and r.conformance_level == ConformanceLevel.PARTIALLY_SUPPORTS)
    does_not = sum(1 for r in results if hasattr(r, 'conformance_level') and r.conformance_level == ConformanceLevel.DOES_NOT_SUPPORT)
    na = sum(1 for r in results if hasattr(r, 'conformance_level') and r.conformance_level == ConformanceLevel.NOT_APPLICABLE)
    ne = sum(1 for r in results if hasattr(r, 'conformance_level') and r.conformance_level == ConformanceLevel.NOT_EVALUATED)
    total_findings = sum(len(r.findings) for r in results if hasattr(r, 'findings'))

    logger.info("-" * 70)
    logger.info("TESTING COMPLETE in %.1fs", test_elapsed)
    logger.info("  Supports: %d | Partial: %d | Does Not: %d | N/A: %d | Not Eval: %d",
                supports, partial, does_not, na, ne)
    logger.info("  Total findings: %d", total_findings)
    logger.info("-" * 70)

    # Discover and test linked documents (PDFs, DOCX, etc.)
    # Single-page and multi-page reviews should also test documents
    # found on the page, not just site crawls.
    if capture_data and file_type not in ("pdf", "docx", "xlsx", "pptx"):
        doc_extensions = {".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt"}
        discovered_docs = []
        for link in getattr(capture_data, "links", []):
            href = link.get("href", "")
            if href:
                # Check if URL ends with a document extension
                path_part = href.split("?")[0].split("#")[0].lower()
                if any(path_part.endswith(ext) for ext in doc_extensions):
                    if href not in discovered_docs:
                        discovered_docs.append(href)

        if discovered_docs:
            logger.info("Found %d linked documents on page: %s",
                        len(discovered_docs),
                        ", ".join(d.rsplit("/", 1)[-1] for d in discovered_docs))
            await broadcast(review_id, {
                "type": "phase", "phase": "testing_documents",
                "message": f"Testing {len(discovered_docs)} linked documents...",
            })

            doc_results_list = []
            for doc_num, doc_url in enumerate(discovered_docs, 1):
                check_cancelled(review_id)
                doc_filename = doc_url.rsplit("/", 1)[-1]
                logger.info("Document %d/%d: %s", doc_num, len(discovered_docs), doc_filename)
                doc_dir = review_dir / f"doc_{doc_num:03d}"
                doc_dir.mkdir(parents=True, exist_ok=True)

                try:
                    import httpx as _httpx
                    async with _httpx.AsyncClient(timeout=30, follow_redirects=True) as _client:
                        resp = await _client.get(doc_url)
                        if resp.status_code == 200:
                            doc_path = doc_dir / "captures" / doc_filename
                            doc_path.parent.mkdir(parents=True, exist_ok=True)
                            doc_path.write_bytes(resp.content)

                            ext = doc_path.suffix.lower().lstrip(".")
                            doc_file_type = ext if ext in ("pdf", "docx", "xlsx", "pptx") else "pdf"
                            doc_checks = get_checks_for_version(
                                meta.wcag_version, meta.coverage_level, file_type=doc_file_type,
                            )

                            if doc_file_type == "pdf":
                                from capture.pdf_capture import capture_pdf
                                doc_capture = capture_pdf(str(doc_path), str(doc_dir))
                            else:
                                from capture.office_capture import capture_office
                                doc_capture = capture_office(str(doc_path), str(doc_dir))

                            if doc_capture:
                                if meta.product_context:
                                    doc_capture.product_context = ProductContext.from_dict(meta.product_context)
                                from models import TestResult as _TR
                                doc_results = []
                                for check in doc_checks:
                                    try:
                                        result = await check.run(doc_capture, ai_client)
                                        doc_results.append(result.to_dict())
                                    except Exception as e:
                                        logger.error(
                                            "Doc check %s failed on %s: %s\n%s",
                                            check.criterion_id, doc_url, e, traceback.format_exc(),
                                        )
                                        err = _TR(
                                            criterion_id=check.criterion_id,
                                            criterion_name=check.criterion_name,
                                            level=check.level,
                                            wcag_versions=check.wcag_versions,
                                            conformance_level=ConformanceLevel.NOT_EVALUATED,
                                            error=str(e),
                                        )
                                        doc_results.append(err.to_dict())
                                (doc_dir / "results.json").write_text(
                                    json.dumps(
                                        {"url": doc_url, "results": doc_results},
                                        indent=2,
                                    ),
                                    encoding="utf-8",
                                )
                                doc_results_list.append({"url": doc_url, "results": doc_results})
                                logger.info("Document tested: %s (%d criteria)",
                                            doc_filename, len(doc_results))
                except Exception as e:
                    logger.warning("Document %s failed: %s", doc_url, e)

            if doc_results_list:
                (review_dir / "document_results.json").write_text(
                    json.dumps(doc_results_list, indent=2), encoding="utf-8",
                )
                logger.info(
                    "Linked-document testing complete: %d/%d documents tested; "
                    "results saved to document_results.json",
                    len(doc_results_list), len(discovered_docs),
                )

    # Feature C: AI cross-criterion synthesis — generates executive summary,
    # identifies patterns, and writes VPAT-style remarks for each criterion
    logger.info("SYNTHESIS PHASE: generating VPAT remarks and executive summary...")
    try:
        from analysis.synthesis import generate_synthesis
        meta_dict = meta.to_dict() if hasattr(meta, "to_dict") else meta
        results_dicts = [r.to_dict() if hasattr(r, "to_dict") else r for r in results]
        synthesis = await generate_synthesis(results_dicts, meta_dict, ai_client)
        if synthesis:
            # Store synthesis data for reports
            synthesis_path = review_dir / "synthesis.json"
            synthesis_path.write_text(json.dumps(synthesis, indent=2))
            # Attach executive summary to meta for report use
            if not isinstance(meta.overall_summary, dict):
                meta.overall_summary = {}
            meta.overall_summary["executive_summary"] = synthesis.get("executive_summary", "")
            meta.overall_summary["systemic_issues"] = synthesis.get("systemic_issues", [])
            meta.overall_summary["priority_order"] = synthesis.get("priority_order", [])
            # Feature A: Update each result's summary with AI-generated VPAT remarks.
            # Only apply synthesis remarks where the Judge AI didn't already
            # write a vpat_summary — the Judge's output takes priority.
            vpat_remarks = synthesis.get("vpat_remarks", {})
            remarks_applied = 0
            judge_preserved = 0
            for result in results:
                remark = vpat_remarks.get(result.criterion_id)
                if remark:
                    # Check if the Judge already wrote a summary for this criterion
                    judge_file = review_dir / "tests" / result.criterion_id.replace(".", "_") / "judge_response.json"
                    if judge_file.exists():
                        # Judge wrote this — preserve the Judge's summary
                        judge_preserved += 1
                    else:
                        result.summary = remark
                        save_test_result(review_dir, result)
                        remarks_applied += 1
            logger.info(
                "SYNTHESIS COMPLETE: %d VPAT remarks written, %d judge summaries preserved, "
                "%d systemic issues found",
                remarks_applied, judge_preserved, len(synthesis.get("systemic_issues", []))
            )
        else:
            logger.warning("Synthesis returned no data")
    except Exception as e:
        logger.warning("Synthesis phase failed (non-fatal): %s", e)

    # Phase: Final reviewer (Pro-tier holistic check of the completed ACR).
    meta.status = "reviewing"
    save_meta(review_dir, meta)
    await broadcast(review_id, {"type": "phase", "phase": "reviewing"})
    from app.queue import _run_final_reviewer
    reviewer_report = await _run_final_reviewer(review_id, review_dir)
    if reviewer_report and reviewer_report.get("status") == "ok":
        from storage.review_store import load_all_test_results
        results = load_all_test_results(review_dir)

    # Phase: Report Generation
    meta.status = "generating_report"
    save_meta(review_dir, meta)
    await broadcast(review_id, {"type": "phase", "phase": "generating_report"})

    try:
        await asyncio.to_thread(generate_acr_report, results, meta, str(review_dir))
        await asyncio.to_thread(generate_acr_report, results, meta, str(review_dir), client_mode=True)
    except Exception as e:
        logger.error(f"Report generation failed: {e}\n{traceback.format_exc()}")

    # Complete
    summary = _compute_summary(results)
    meta.status = "complete"
    # Merge, don't clobber: the synthesis phase already stored
    # executive_summary / systemic_issues / priority_order on
    # overall_summary — keep them and add the count keys.
    if isinstance(meta.overall_summary, dict):
        meta.overall_summary = {**meta.overall_summary, **summary}
    else:
        meta.overall_summary = summary
    meta.supports = summary["supports"]
    meta.partially_supports = summary["partially_supports"]
    meta.does_not_support = summary["does_not_support"]
    meta.not_applicable = summary["not_applicable"]
    meta.not_evaluated = summary["not_evaluated"]
    save_meta(review_dir, meta)

    logger.info("=" * 70)
    logger.info("REVIEW %s COMPLETE", review_id)
    logger.info("  Supports: %d | Partial: %d | Does Not: %d | N/A: %d",
                summary.get("supports", 0), summary.get("partially_supports", 0),
                summary.get("does_not_support", 0), summary.get("not_applicable", 0))
    logger.info("  Total findings: %d | Report saved to: %s", summary.get("total_findings", 0), review_dir)
    logger.info("=" * 70)

    await broadcast(review_id, {"type": "complete", "summary": summary})


async def process_multi_review(review_id: str, meta: ReviewMeta, review_dir: Path, ai_client) -> None:
    """Process a multi-page review with user-specified URLs.

    Same as site crawl but skips the crawler — uses URLs from
    meta.user_context["multi_urls"]. Runs cross-page consistency
    checks just like site crawl.
    """
    from crawl.aggregator import aggregate_results, generate_per_page_summary
    from checks.registry import get_checks_for_version
    from report.acr_generator import generate_acr_report
    from verification.verifier import verify_result
    from functions.bypass_log import bind_current_review_dir
    from storage.review_store import load_meta, save_meta

    # Bind review dir for ambient bypass telemetry.
    bind_current_review_dir(review_dir)

    # Get URLs from user context
    pages = meta.user_context.get("multi_urls", [])
    if not pages:
        meta.status = "error"
        meta.error = "No URLs provided for multi-page review"
        save_meta(review_dir, meta)
        return

    meta.pages_discovered = len(pages)
    save_meta(review_dir, meta)

    # Phase: Testing each page (skip crawling — we have the URLs)
    meta.status = "testing"
    save_meta(review_dir, meta)
    await broadcast(review_id, {"type": "phase", "phase": "testing", "total_pages": len(pages)})

    page_results_list = []
    checks = get_checks_for_version(meta.wcag_version, meta.coverage_level, file_type="web")

    for page_num, page_url in enumerate(pages, 1):
        check_cancelled(review_id)
        await broadcast(review_id, {
            "type": "site_page_start",
            "page_url": page_url,
            # Both spellings: legacy consumers read page_num, the site
            # path (and the JS) uses page_number.
            "page_num": page_num,
            "page_number": page_num,
            "total_pages": len(pages),
        })

        page_dir = review_dir / f"page_{page_num}"
        page_dir.mkdir(parents=True, exist_ok=True)

        try:
            from config import CAPTURE_PIPELINE
            async def _auth_cb(msg):
                await broadcast(review_id, {"type": "phase", "phase": "authenticating", "message": msg})
            async def _progress_cb(msg):
                await broadcast(review_id, {"type": "phase", "phase": "capturing", "message": str(msg)})
            if CAPTURE_PIPELINE == "v2":
                from capture.v2 import capture_web_page_v2
                capture_data = await capture_web_page_v2(
                    page_url, str(page_dir), {},
                    auth_callback=_auth_cb, progress_callback=_progress_cb,
                    cancel_check=lambda: check_cancelled(review_id),
                )
            else:
                from capture.web_capture import capture_web_page
                capture_data = await capture_web_page(page_url, str(page_dir), auth_callback=_auth_cb)

            # Attach product context so every AI call sees it
            if meta.product_context:
                from models import ProductContext
                capture_data.product_context = ProductContext.from_dict(meta.product_context)

            # Pre-process videos for this page
            if ai_client:
                try:
                    from capture.video_describer import describe_all_videos
                    await describe_all_videos(capture_data, ai_client)
                except Exception:
                    logger.exception("Video description failed for %s (non-fatal)", page_url)

            # Per-page test results are persisted IMMEDIATELY to
            # page_dir/tests/<sc>/result.json so a crash mid-page does
            # not lose work for already-tested SCs (Gap 2 from the
            # 2026-05-21 audit). Mirrors the single-page behaviour at
            # ~l. 386-477. Each page's tests dir is independent of the
            # others.
            from storage.review_store import save_test_result
            from models import TestResult as _TR
            page_results = []
            for idx, check in enumerate(checks):
                check_cancelled(review_id)
                # Resume support: if a prior partial run already
                # persisted this SC's result.json, reload it instead of
                # re-running the check.
                sc_dirname = check.criterion_id.replace(".", "_")
                existing_path = page_dir / "tests" / sc_dirname / "result.json"
                if existing_path.exists():
                    try:
                        page_results.append(
                            json.loads(existing_path.read_text(encoding="utf-8")),
                        )
                        continue
                    except Exception:
                        logger.warning(
                            "Could not reload prior result for SC %s on %s; "
                            "re-running.", check.criterion_id, page_url,
                            exc_info=True,
                        )
                try:
                    result = await check.run(capture_data, ai_client)
                    result = await verify_result(result, ai_client, capture_data)
                    save_test_result(page_dir, result)
                    page_results.append(result.to_dict())
                except Exception as e:
                    logger.error(f"Check {check.criterion_id} failed on {page_url}: {e}")
                    err = _TR(
                        criterion_id=check.criterion_id,
                        criterion_name=check.criterion_name,
                        level=check.level,
                        wcag_versions=check.wcag_versions,
                        conformance_level=ConformanceLevel.NOT_EVALUATED,
                        error=str(e),
                    )
                    try:
                        save_test_result(page_dir, err)
                    except Exception:
                        logger.warning(
                            "Could not persist error stub for SC %s on %s",
                            check.criterion_id, page_url, exc_info=True,
                        )
                    page_results.append(err.to_dict())

            page_results_list.append({"url": page_url, "results": page_results})

            await broadcast(review_id, {
                "type": "site_page_complete",
                "page_url": page_url,
                # Both spellings: legacy consumers read page_num /
                # results_count, the site path (and the JS) uses
                # page_number / criteria_tested.
                "page_num": page_num,
                "page_number": page_num,
                "total_pages": len(pages),
                "results_count": len(page_results),
                "criteria_tested": len(page_results),
            })

        except ReviewCancelled:
            raise  # cancel aborts the whole review, not just this page
        except Exception as e:
            logger.error(f"Multi-page capture failed for {page_url}: {e}")
            page_results_list.append({"url": page_url, "results": [], "error": str(e)})

    # Discover and test linked documents from all tested pages
    doc_extensions = {".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt"}
    all_doc_urls: list[str] = []
    for page_num, pr in enumerate(page_results_list, 1):
        # Check if capture_data had links with document extensions
        page_dir_check = review_dir / f"page_{page_num}"
        inv_path = page_dir_check / "captures" / "element_inventory.json"
        if inv_path.exists():
            try:
                inv = json.loads(inv_path.read_text(encoding="utf-8"))
                elements = inv.get("elements", inv) if isinstance(inv, dict) else inv
                for el in elements:
                    if el.get("type") == "link":
                        href = el.get("href", "")
                        if href:
                            path_part = href.split("?")[0].split("#")[0].lower()
                            if any(path_part.endswith(ext) for ext in doc_extensions):
                                if href not in all_doc_urls:
                                    all_doc_urls.append(href)
            except Exception:
                logger.exception(
                    "Failed to scan element inventory for linked documents: %s", inv_path,
                )

    if all_doc_urls:
        logger.info("Found %d linked documents across pages", len(all_doc_urls))
        await broadcast(review_id, {
            "type": "phase", "phase": "testing_documents",
            "message": f"Testing {len(all_doc_urls)} linked documents...",
        })
        for doc_num, doc_url in enumerate(all_doc_urls, 1):
            check_cancelled(review_id)
            doc_filename = doc_url.rsplit("/", 1)[-1]
            doc_dir = review_dir / f"doc_{doc_num:03d}"
            doc_dir.mkdir(parents=True, exist_ok=True)
            try:
                import httpx as _httpx
                async with _httpx.AsyncClient(timeout=30, follow_redirects=True) as _client:
                    resp = await _client.get(doc_url)
                    if resp.status_code == 200:
                        doc_path = doc_dir / "captures" / doc_filename
                        doc_path.parent.mkdir(parents=True, exist_ok=True)
                        doc_path.write_bytes(resp.content)
                        ext = doc_path.suffix.lower().lstrip(".")
                        doc_file_type = ext if ext in ("pdf", "docx", "xlsx", "pptx") else "pdf"
                        doc_checks = get_checks_for_version(
                            meta.wcag_version, meta.coverage_level, file_type=doc_file_type,
                        )
                        if doc_file_type == "pdf":
                            from capture.pdf_capture import capture_pdf
                            doc_capture = capture_pdf(str(doc_path), str(doc_dir))
                        else:
                            from capture.office_capture import capture_office
                            doc_capture = capture_office(str(doc_path), str(doc_dir))
                        if doc_capture:
                            if meta.product_context:
                                from models import ProductContext
                                doc_capture.product_context = ProductContext.from_dict(meta.product_context)
                            doc_results = []
                            from models import TestResult as _TR
                            for check in doc_checks:
                                try:
                                    result = await check.run(doc_capture, ai_client)
                                    doc_results.append(result.to_dict())
                                except Exception as e:
                                    logger.error(
                                        "Doc check %s failed: %s\n%s",
                                        check.criterion_id, e, traceback.format_exc(),
                                    )
                                    err = _TR(
                                        criterion_id=check.criterion_id,
                                        criterion_name=check.criterion_name,
                                        level=check.level,
                                        wcag_versions=check.wcag_versions,
                                        conformance_level=ConformanceLevel.NOT_EVALUATED,
                                        error=str(e),
                                    )
                                    doc_results.append(err.to_dict())
                            page_results_list.append({"url": doc_url, "results": doc_results})
                            logger.info("Document tested: %s (%d criteria)", doc_filename, len(doc_results))
            except Exception as e:
                logger.warning("Document %s failed: %s", doc_url, e)

    # Phase: Aggregation
    meta.status = "aggregating"
    save_meta(review_dir, meta)
    await broadcast(review_id, {"type": "phase", "phase": "aggregating"})

    aggregated = await aggregate_results(page_results_list)
    per_page = generate_per_page_summary(page_results_list)

    # Cross-page consistency checks (SC 3.2.3, 3.2.4)
    from crawl.aggregator import check_cross_page_consistency
    cross_page_findings = await check_cross_page_consistency(page_results_list, str(review_dir))
    if cross_page_findings:
        for agg in aggregated:
            cid = agg.get("criterion_id", "")
            if cid in ("3.2.3", "3.2.4"):
                # Route each cross-page finding to ITS criterion only --
                # a nav-order inconsistency (3.2.3) must not downgrade
                # 3.2.4's verdict, and vice versa. Untagged findings
                # (legacy shape) still apply to both.
                matched = [
                    cpf for cpf in cross_page_findings
                    if cpf.get("criterion_id", cid) == cid
                ]
                if not matched:
                    continue
                existing = agg.get("findings", [])
                existing.extend(matched)
                agg["findings"] = existing
                high = sum(1 for f in existing if f.get("severity") == "high")
                med = sum(1 for f in existing if f.get("severity") == "medium")
                if high > 0:
                    agg["conformance_level"] = "Does Not Support"
                elif med > 0 and agg.get("conformance_level") == "Supports":
                    agg["conformance_level"] = "Partially Supports"

    # Save aggregated results
    for r in aggregated:
        result_path = review_dir / "tests" / r.get("criterion_id", "unknown").replace(".", "_")
        result_path.mkdir(parents=True, exist_ok=True)
        (result_path / "result.json").write_text(json.dumps(r, indent=2, default=str))

    # Phase: Final reviewer (Pro-tier holistic check) — runs on the
    # aggregated multi-page result.json files just written above.
    meta.status = "reviewing"
    save_meta(review_dir, meta)
    await broadcast(review_id, {"type": "phase", "phase": "reviewing"})
    from app.queue import _run_final_reviewer
    reviewer_report = await _run_final_reviewer(review_id, review_dir)
    if reviewer_report and reviewer_report.get("status") == "ok":
        # Reload aggregated dicts from disk so report generation picks up
        # any verdict recalibrations and tone rewrites.
        aggregated = []
        for r_orig in sorted((review_dir / "tests").iterdir()):
            rp = r_orig / "result.json"
            if rp.exists():
                try:
                    aggregated.append(json.loads(rp.read_text(encoding="utf-8")))
                except Exception:
                    logger.exception("Skipping malformed test result %s", rp)

    # Phase: Report Generation
    meta.status = "generating_report"
    save_meta(review_dir, meta)
    await broadcast(review_id, {"type": "phase", "phase": "generating_report"})

    try:
        await asyncio.to_thread(generate_acr_report, aggregated, meta, str(review_dir))
        await asyncio.to_thread(generate_acr_report, aggregated, meta, str(review_dir), client_mode=True)
    except Exception as e:
        logger.error("Report generation failed: %s", e)

    # Phase: Complete
    meta.status = "complete"
    summary = {"supports": 0, "partially_supports": 0, "does_not_support": 0,
               "not_applicable": 0, "not_evaluated": 0, "total_findings": 0}
    for r in aggregated:
        level = r.get("conformance_level", "Not Evaluated")
        if level == "Supports": summary["supports"] += 1
        elif level == "Partially Supports": summary["partially_supports"] += 1
        elif level == "Does Not Support": summary["does_not_support"] += 1
        elif level == "Not Applicable": summary["not_applicable"] += 1
        else: summary["not_evaluated"] += 1
        summary["total_findings"] += len(r.get("findings", []))

    meta.supports = summary["supports"]
    meta.partially_supports = summary["partially_supports"]
    meta.does_not_support = summary["does_not_support"]
    meta.not_applicable = summary["not_applicable"]
    meta.not_evaluated = summary["not_evaluated"]
    meta.total_criteria = len(aggregated)
    meta.overall_summary = summary
    meta.pages_tested = len(page_results_list)
    meta.per_page_summary = per_page
    save_meta(review_dir, meta)

    logger.info("=" * 70)
    logger.info("MULTI-PAGE REVIEW %s COMPLETE (%d pages)", review_id, len(pages))
    logger.info("  Supports: %d | Partial: %d | Does Not: %d | N/A: %d",
                summary["supports"], summary["partially_supports"],
                summary["does_not_support"], summary["not_applicable"])
    logger.info("=" * 70)

    await broadcast(review_id, {"type": "complete", "summary": summary})


async def process_site_review(review_id: str, meta: ReviewMeta, review_dir: Path, ai_client) -> None:
    """Process a site crawl review."""
    from crawl.site_crawler import crawl_site
    from crawl.aggregator import aggregate_results, generate_per_page_summary
    from checks.registry import get_checks_for_version
    from report.acr_generator import generate_acr_report
    from verification.verifier import verify_result
    from functions.bypass_log import bind_current_review_dir
    from storage.review_store import load_meta, save_meta

    # Bind review dir for ambient bypass telemetry.
    bind_current_review_dir(review_dir)

    # Phase: Crawling
    logger.info("=" * 70)
    logger.info("SITE CRAWL: %s (max_pages=%d)", meta.source_url, meta.max_pages or 10)
    logger.info("=" * 70)
    meta.status = "crawling"
    save_meta(review_dir, meta)
    await broadcast(review_id, {"type": "phase", "phase": "crawling"})

    async def crawl_progress(discovered, message):
        await broadcast(review_id, {"type": "crawl_progress", "discovered": discovered, "message": message})

    crawl_result = await crawl_site(
        meta.source_url,
        max_pages=meta.max_pages or 10,
        progress_callback=crawl_progress,
    )
    pages = crawl_result["pages"]
    discovered_docs = crawl_result.get("documents", [])

    logger.info("CRAWL COMPLETE: %d pages + %d documents discovered", len(pages), len(discovered_docs))
    for i, p in enumerate(pages):
        logger.info("  Page %d: %s", i + 1, p)
    for i, d in enumerate(discovered_docs):
        logger.info("  Doc  %d: %s", i + 1, d)

    meta.pages_discovered = len(pages)
    save_meta(review_dir, meta)

    if discovered_docs:
        logger.info("Found %d linked documents: %s", len(discovered_docs),
                     ", ".join(d.rsplit("/", 1)[-1] for d in discovered_docs))

    # Phase: AI site analysis + page selection
    # Step 1: Summarize all discovered pages (off-domain redirects dropped)
    # Step 2: AI determines site type (school? gov? commerce?)
    # Step 3: AI picks the pages needed for a credible ACR — the selector
    #         decides the count based on site complexity, not an operator cap.

    from crawl.page_selector import summarize_pages, select_pages, analyze_site

    try:
        meta.status = "selecting"
        save_meta(review_dir, meta)
        await broadcast(review_id, {
            "type": "phase", "phase": "selecting",
            "message": f"AI analyzing {len(pages)} discovered pages...",
        })

        async def select_progress(done, msg):
            await broadcast(review_id, {"type": "crawl_progress", "discovered": done, "message": msg})

        # Summarize every discovered page (lightweight HTTP fetch)
        logger.info("PHASE: Summarizing %d pages...", len(pages))
        summaries = await summarize_pages(pages, progress_callback=select_progress)
        logger.info("PHASE: Summarization complete — %d pages summarized", len(summaries))

        # AI determines what this site IS from the page summaries
        await broadcast(review_id, {
            "type": "crawl_progress",
            "discovered": len(pages),
            "message": "Analyzing site type and user context...",
        })
        logger.info("PHASE: AI analyzing site type...")
        site_context = await analyze_site(summaries, ai_client)
        logger.info("SITE ANALYSIS: sector=%s, type=%s, users=%s, workflows=%s",
                     site_context.get("sector"), site_context.get("client_type"),
                     site_context.get("primary_users"), site_context.get("critical_workflows"))

        # Merge AI-detected context with any user-provided context.
        # User input takes priority — AI fills in the gaps.
        existing = meta.product_context or {}
        for key, value in site_context.items():
            if not existing.get(key):
                existing[key] = value
        # If user provided product_description, add it as additional_context
        if meta.product_description and not existing.get("additional_context"):
            existing["additional_context"] = meta.product_description
        meta.product_context = existing
        if not meta.product_description and existing.get("additional_context"):
            meta.product_description = existing["additional_context"]
        save_meta(review_dir, meta)

        logger.info(
            "Site analyzed: %s (%s) — %d pages discovered",
            site_context.get("client_type", "unknown"),
            site_context.get("sector", "unknown"),
            len(pages),
        )

        # AI selects the right pages — it decides how many are needed
        # for a credible ACR based on the site's complexity and content.
        logger.info("PHASE: AI selecting pages from %d discovered...", len(pages))
        selection = await select_pages(
            summaries, ai_client,
            max_pages=len(pages),
            coverage_level=meta.coverage_level,
            wcag_version=meta.wcag_version,
            site_context=site_context,
        )
        pages = [s["url"] for s in selection["selected"]]
        meta.page_sample = selection["selected"]
        if not meta.page_rationale:
            meta.page_rationale = selection["rationale"]
        logger.info("PAGE SELECTION: AI chose %d pages", len(pages))
        for s in selection["selected"]:
            logger.info("  SELECTED: %s — %s", s["url"], s.get("reason", ""))
        logger.info("  RATIONALE: %s", selection.get("rationale", ""))

        save_meta(review_dir, meta)
        await broadcast(review_id, {
            "type": "phase", "phase": "selected",
            "message": f"Selected {len(pages)} pages for testing",
            "selected": len(pages),
        })

    except Exception as exc:
        import random as _rand
        logger.warning("AI site analysis/selection failed: %s — falling back to random 10", exc)
        discovered_count = meta.pages_discovered or len(pages)
        if len(pages) > 10:
            homepage = pages[0]
            rest = pages[1:]
            _rand.shuffle(rest)
            pages = [homepage] + rest[:9]
        meta.page_rationale = (
            f"AI page selection failed. Testing homepage + {len(pages) - 1} "
            f"random pages from {discovered_count} discovered."
        )
        # Discard any partial AI selection recorded before the failure;
        # page_sample must describe the pages actually being tested.
        meta.page_sample = [
            {"url": u, "reason": "Random fallback after AI page selection failed"}
            for u in pages
        ]
        save_meta(review_dir, meta)

    # Phase: Testing each page
    logger.info("=" * 70)
    logger.info("TESTING PHASE: %d pages, %d criteria each", len(pages), len(get_checks_for_version(meta.wcag_version, meta.coverage_level, file_type="web")))
    logger.info("=" * 70)
    meta.status = "testing"
    save_meta(review_dir, meta)
    await broadcast(review_id, {"type": "phase", "phase": "testing", "total_pages": len(pages)})

    page_results_list = []
    checks = get_checks_for_version(meta.wcag_version, meta.coverage_level, file_type="web")

    for page_num, page_url in enumerate(pages, 1):
        check_cancelled(review_id)
        logger.info("PAGE %d/%d: %s", page_num, len(pages), page_url)
        await broadcast(review_id, {
            "type": "site_page_start",
            "page_url": page_url,
            "page_number": page_num,
            "total_pages": len(pages),
        })

        page_dir = review_dir / f"page_{page_num:03d}"
        page_dir.mkdir(parents=True, exist_ok=True)

        # Capture and test are separate — if capture partially fails
        # (e.g. browser crashes during interactive capture but screenshots
        # and a11y tree were already saved), we still test what we got.
        capture_data = None
        try:
            from config import CAPTURE_PIPELINE
            async def _auth_cb(msg):
                await broadcast(review_id, {"type": "phase", "phase": "authenticating", "message": msg})
            async def _progress_cb(msg):
                await broadcast(review_id, {"type": "phase", "phase": "capturing", "message": str(msg)})
            if CAPTURE_PIPELINE == "v2":
                from capture.v2 import capture_web_page_v2
                capture_data = await capture_web_page_v2(
                    page_url, str(page_dir), {},
                    auth_callback=_auth_cb, progress_callback=_progress_cb,
                    cancel_check=lambda: check_cancelled(review_id),
                )
            else:
                from capture.web_capture import capture_web_page
                capture_data = await capture_web_page(page_url, str(page_dir), auth_callback=_auth_cb)
        except ReviewCancelled:
            raise  # cancel aborts the whole review, not just this page
        except Exception as e:
            logger.warning(f"Capture failed for {page_url}: {e}")
            # Recover from whatever was saved before the crash
            from models import CaptureData as _CD
            dom_path = page_dir / "captures" / "dom.html"
            if dom_path.exists():
                try:
                    capture_data = _CD(
                        url=page_url,
                        html=dom_path.read_text(encoding="utf-8"),
                        review_dir=str(page_dir),
                    )
                    a11y_path = page_dir / "captures" / "a11y_tree.json"
                    if a11y_path.exists():
                        import json as _json
                        capture_data.a11y_tree = _json.loads(a11y_path.read_text(encoding="utf-8"))
                    # Check for screenshots
                    fp = page_dir / "captures" / "full_page.png"
                    if fp.exists():
                        capture_data.full_page_path = str(fp)
                    vp = page_dir / "captures" / "viewport.png"
                    if vp.exists():
                        capture_data.viewport_path = str(vp)
                    logger.info(f"Recovered partial capture for {page_url} (DOM + a11y tree + screenshots)")
                except Exception as recover_err:
                    logger.warning(f"Recovery also failed for {page_url}: {recover_err}")

        if capture_data is None:
            logger.error(f"Page {page_url} has no usable capture data — skipping")
            await broadcast(review_id, {
                "type": "site_page_error",
                "page_url": page_url, "page_number": page_num,
                "total_pages": len(pages), "error": "Capture failed completely",
            })
            continue

        # Attach product context
        if meta.product_context:
            from models import ProductContext
            capture_data.product_context = ProductContext.from_dict(meta.product_context)

        # Pre-process videos for this page
        if ai_client:
            try:
                from capture.video_describer import describe_all_videos
                await describe_all_videos(capture_data, ai_client)
            except Exception:
                logger.exception("Video description failed for %s (non-fatal)", page_url)

        # Test all criteria on whatever capture data we have. Each check
        # runs in isolation -- a single check exception records a
        # Not-Evaluated result for that criterion and moves on, so one
        # bad check can't erase the other 86 from the final ACR.
        #
        # Per-page test results are persisted IMMEDIATELY to
        # page_dir/tests/<sc>/result.json so a crash mid-page (or
        # mid-crawl) does not lose work for already-tested SCs (Gap 2
        # from the 2026-05-21 audit). Mirrors the single-page behaviour.
        from models import TestResult as _TR
        from storage.review_store import save_test_result
        page_results = []
        for idx, check in enumerate(checks):
            check_cancelled(review_id)
            # Resume support: skip SCs whose result.json already exists
            # for this page.
            sc_dirname = check.criterion_id.replace(".", "_")
            existing_path = page_dir / "tests" / sc_dirname / "result.json"
            if existing_path.exists():
                try:
                    page_results.append(
                        json.loads(existing_path.read_text(encoding="utf-8")),
                    )
                    continue
                except Exception:
                    logger.warning(
                        "Could not reload prior result for SC %s on %s; "
                        "re-running.", check.criterion_id, page_url,
                        exc_info=True,
                    )
            try:
                result = await check.run(capture_data, ai_client)
                result = await verify_result(result, ai_client, capture_data)
                save_test_result(page_dir, result)
                page_results.append(result.to_dict())
            except Exception as e:
                logger.error(
                    "Check %s failed on %s: %s\n%s",
                    check.criterion_id, page_url, e, traceback.format_exc(),
                )
                err = _TR(
                    criterion_id=check.criterion_id,
                    criterion_name=check.criterion_name,
                    level=check.level,
                    wcag_versions=check.wcag_versions,
                    conformance_level=ConformanceLevel.NOT_EVALUATED,
                    error=str(e),
                )
                try:
                    save_test_result(page_dir, err)
                except Exception:
                    logger.warning(
                        "Could not persist error stub for SC %s on %s",
                        check.criterion_id, page_url, exc_info=True,
                    )
                page_results.append(err.to_dict())

        page_results_list.append({"url": page_url, "results": page_results})

        await broadcast(review_id, {
            "type": "site_page_complete",
            "page_url": page_url,
            "page_number": page_num,
            "total_pages": len(pages),
            "criteria_tested": len(page_results),
        })

    # Phase: Test linked documents (PDFs, DOCX, etc.)
    if discovered_docs:
        await broadcast(review_id, {
            "type": "phase", "phase": "testing_documents",
            "message": f"Testing {len(discovered_docs)} linked documents...",
        })
        for doc_num, doc_url in enumerate(discovered_docs, 1):
            check_cancelled(review_id)
            doc_filename = doc_url.rsplit("/", 1)[-1]
            await broadcast(review_id, {
                "type": "crawl_progress",
                "discovered": doc_num,
                "message": f"Testing document {doc_num}/{len(discovered_docs)}: {doc_filename}",
            })
            doc_dir = review_dir / f"doc_{doc_num:03d}"
            doc_dir.mkdir(parents=True, exist_ok=True)

            try:
                # Download the document
                import httpx as _httpx
                async with _httpx.AsyncClient(timeout=30, follow_redirects=True) as _client:
                    resp = await _client.get(doc_url)
                    if resp.status_code == 200:
                        doc_path = doc_dir / "captures" / doc_filename
                        doc_path.parent.mkdir(parents=True, exist_ok=True)
                        doc_path.write_bytes(resp.content)

                        # Determine file type and get matching checks
                        ext = doc_path.suffix.lower().lstrip(".")
                        file_type = ext if ext in ("pdf", "docx", "xlsx", "pptx") else "pdf"
                        doc_checks = get_checks_for_version(
                            meta.wcag_version, meta.coverage_level, file_type=file_type,
                        )

                        if file_type == "pdf":
                            from capture.pdf_capture import capture_pdf
                            capture_data = capture_pdf(str(doc_path), str(doc_dir))
                        else:
                            from capture.office_capture import capture_office
                            capture_data = capture_office(str(doc_path), str(doc_dir))

                        if capture_data:
                            if meta.product_context:
                                from models import ProductContext
                                capture_data.product_context = ProductContext.from_dict(meta.product_context)

                            doc_results = []
                            for check in doc_checks:
                                try:
                                    result = await check.run(capture_data, ai_client)
                                    doc_results.append(result.to_dict())
                                except Exception as e:
                                    logger.error(f"Doc check {check.criterion_id} failed on {doc_url}: {e}")

                            page_results_list.append({"url": doc_url, "results": doc_results})
                            logger.info("Document tested: %s (%d criteria)", doc_filename, len(doc_results))
            except Exception as e:
                logger.warning(f"Document {doc_url} failed: {e}")

    # Phase: Aggregating
    meta.status = "aggregating"
    save_meta(review_dir, meta)
    await broadcast(review_id, {"type": "phase", "phase": "aggregating"})

    aggregated = await aggregate_results(page_results_list)
    per_page = generate_per_page_summary(page_results_list)

    # Cross-page consistency checks (SC 3.2.3, 3.2.4)
    from crawl.aggregator import check_cross_page_consistency
    cross_page_findings = await check_cross_page_consistency(page_results_list, str(review_dir))
    if cross_page_findings:
        # Inject cross-page findings into the relevant aggregated criteria
        for agg in aggregated:
            cid = agg.get("criterion_id", "")
            if cid in ("3.2.3", "3.2.4"):
                # Route each cross-page finding to ITS criterion only --
                # a nav-order inconsistency (3.2.3) must not downgrade
                # 3.2.4's verdict, and vice versa. Untagged findings
                # (legacy shape) still apply to both.
                matched = [
                    cpf for cpf in cross_page_findings
                    if cpf.get("criterion_id", cid) == cid
                ]
                if not matched:
                    continue
                existing = agg.get("findings", [])
                existing.extend(matched)
                agg["findings"] = existing
                # Update conformance if cross-page issues found
                high = sum(1 for f in existing if f.get("severity") == "high")
                med = sum(1 for f in existing if f.get("severity") == "medium")
                if high > 0:
                    agg["conformance_level"] = "Does Not Support"
                elif med > 0 and agg.get("conformance_level") == "Supports":
                    agg["conformance_level"] = "Partially Supports"

    # Save aggregated results
    for r in aggregated:
        result_path = review_dir / "tests" / r.get("criterion_id", "unknown").replace(".", "_")
        result_path.mkdir(parents=True, exist_ok=True)
        (result_path / "result.json").write_text(json.dumps(r, indent=2, default=str))

    # Phase: Final reviewer (Pro-tier holistic check) — runs on the
    # aggregated site-crawl result.json files just written above.
    meta.status = "reviewing"
    save_meta(review_dir, meta)
    await broadcast(review_id, {"type": "phase", "phase": "reviewing"})
    from app.queue import _run_final_reviewer
    reviewer_report = await _run_final_reviewer(review_id, review_dir)
    if reviewer_report and reviewer_report.get("status") == "ok":
        aggregated = []
        for r_orig in sorted((review_dir / "tests").iterdir()):
            rp = r_orig / "result.json"
            if rp.exists():
                try:
                    aggregated.append(json.loads(rp.read_text(encoding="utf-8")))
                except Exception:
                    logger.exception("Skipping malformed test result %s", rp)

    # Phase: Report
    meta.status = "generating_report"
    save_meta(review_dir, meta)
    await broadcast(review_id, {"type": "phase", "phase": "generating_report"})

    try:
        await asyncio.to_thread(generate_acr_report, aggregated, meta, str(review_dir))
        await asyncio.to_thread(generate_acr_report, aggregated, meta, str(review_dir), client_mode=True)
    except Exception as e:
        logger.error(f"Report generation failed: {e}")

    # Complete
    summary = _compute_summary_from_dicts(aggregated)
    meta.status = "complete"
    meta.overall_summary = summary
    meta.supports = summary["supports"]
    meta.partially_supports = summary["partially_supports"]
    meta.does_not_support = summary["does_not_support"]
    meta.not_applicable = summary["not_applicable"]
    meta.not_evaluated = summary["not_evaluated"]
    meta.pages_tested = len(page_results_list)
    meta.per_page_summary = per_page
    save_meta(review_dir, meta)

    await broadcast(review_id, {
        "type": "complete",
        "summary": summary,
        "pages_tested": len(page_results_list),
        "per_page_summary": per_page,
    })
