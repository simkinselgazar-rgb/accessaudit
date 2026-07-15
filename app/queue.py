"""Review queue plumbing.

Owns the asyncio queue, the post-review reviewer + audit hooks, and the
worker coroutine that drains the queue one review at a time. The
orchestrators in :mod:`app.orchestrators` are imported lazily inside
``queue_worker`` so the dependency graph stays acyclic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import traceback

from pathlib import Path

from config import REVIEWS_DIR
from storage.review_store import load_meta, save_meta

from app.cancellation import ReviewCancelled
from app.websocket_manager import broadcast


logger = logging.getLogger(__name__)


_review_queue: asyncio.Queue = asyncio.Queue()
_queue_worker_task: asyncio.Task | None = None


async def _run_final_reviewer(review_id: str, review_dir: Path) -> dict | None:
    """Pro-tier holistic review of the completed ACR.

    Six focused calls (5 in parallel + 1 synthesis) check structural
    completeness, severity-conformance calibration, cross-SC contradictions,
    WCAG citation accuracy, and prose tone, then write the executive
    summary. Recalibrations and tone rewrites are applied to result.json
    files so the rendered ACR reflects them. Failure here is non-fatal —
    the unreviewed ACR still ships.
    """
    try:
        from analysis.final_reviewer import apply_mutations, run_all
        report = await run_all(review_dir)
        if not report or report.get("status") != "ok":
            logger.info(
                "FINAL REVIEWER %s skipped or failed: %s",
                review_id, (report or {}).get("status", "no result"),
            )
            return report
        counts = apply_mutations(review_dir, report)
        logger.info(
            "FINAL REVIEWER %s: %d recalibrated, %d rewritten, %d skipped",
            review_id, counts["recalibrated"], counts["rewritten"], counts["skipped"],
        )
        await broadcast(review_id, {
            "type": "reviewer_complete",
            "recalibrations": len((report.get("calibration") or {}).get("recalibrations") or []),
            "contradictions": len((report.get("contradiction") or {}).get("contradictions") or []),
            "citation_errors": len((report.get("citation") or {}).get("citation_errors") or []),
            "tone_rewrites": len((report.get("tone") or {}).get("rewrites") or []),
            "systemic_issues": len((report.get("synthesis") or {}).get("systemic_issues") or []),
            "applied": counts,
        })
        return report
    except Exception as exc:
        logger.exception("Final reviewer failed for %s: %s", review_id, exc)
        return None


def _run_post_review_audit(review_id: str) -> dict | None:
    """Invoke audit_run.audit_review on a finished review.

    The report is saved to ``reviews/<id>/audit.json`` and a one-line
    summary is logged. Bugs are surfaced over the websocket so the UI
    can flag problems to the user (silent finding drops, missing
    element text, parser text-fallback, etc.) instead of letting them
    ship into the ACR unnoticed.
    """
    try:
        from audit_run import audit_review
        report = audit_review(review_id, reviews_root=REVIEWS_DIR)
        review_dir = REVIEWS_DIR / review_id
        try:
            (review_dir / "audit.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Could not write audit.json for %s: %s", review_id, exc)
        bug_count = len(report.get("bugs") or [])
        warn_count = len(report.get("warns") or [])
        if report.get("fatal"):
            logger.warning("AUDIT %s: fatal=%s", review_id, report["fatal"])
        else:
            logger.info(
                "AUDIT %s: %d bugs, %d warnings (completed=%d total_findings=%d)",
                review_id, bug_count, warn_count,
                report["stats"]["completed"], report["stats"]["total_findings"],
            )
        return report
    except Exception as exc:
        logger.exception("Post-review audit crashed for %s: %s", review_id, exc)
        return None


async def queue_worker():
    """Process reviews from the queue one at a time."""
    from app.orchestrators import process_review
    while True:
        review_id = await _review_queue.get()
        review_finished_cleanly = False
        try:
            await process_review(review_id)
            review_finished_cleanly = True
        except ReviewCancelled:
            logger.info("Review %s cancelled by user", review_id)
            try:
                review_dir = REVIEWS_DIR / review_id
                meta = load_meta(review_dir)
                meta.status = "cancelled"
                meta.error = "Cancelled by user"
                save_meta(review_dir, meta)
                await broadcast(review_id, {"type": "cancelled", "message": "Review cancelled"})
            except Exception:
                logger.exception("Failed to record cancellation state for review %s", review_id)
        except Exception as e:
            logger.error(f"Review {review_id} failed: {e}\n{traceback.format_exc()}")
            try:
                review_dir = REVIEWS_DIR / review_id
                meta = load_meta(review_dir)
                meta.status = "error"
                meta.error = str(e)
                save_meta(review_dir, meta)
                await broadcast(review_id, {"type": "error", "message": str(e)})
            except Exception:
                logger.exception("Failed to record error state for review %s", review_id)
        finally:
            # Run the post-review audit whether the review completed
            # cleanly or errored -- partial results are still worth
            # inspecting, and the audit itself is cheap and sandboxed.
            report = _run_post_review_audit(review_id)
            if report and not report.get("fatal"):
                try:
                    await broadcast(review_id, {
                        "type": "audit_complete",
                        "bugs": report["bugs"],
                        "warns": report["warns"],
                        "stats": report["stats"],
                        "review_clean": review_finished_cleanly,
                    })
                except Exception:
                    logger.exception("Failed to broadcast audit_complete for review %s", review_id)
            _review_queue.task_done()
