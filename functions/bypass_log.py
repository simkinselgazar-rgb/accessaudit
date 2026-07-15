"""Per-review bypass telemetry.

Every place in the system that catches a failure and keeps running
(instead of letting the review die) should call ``log_bypass()``. The
events land in ``<review_dir>/bypass_log.jsonl`` -- one JSON object per
line -- so each review has its OWN audit trail. No mixing across runs,
no grep-archaeology in a shared log.

The design target: after any review finishes, a human can open
``bypass_log.jsonl`` and answer "did this run stay clean?" or "which
step fell back and why?" in a few seconds, without touching the
global ``accessaudit.log``.

Strict mode (``WCAG_STRICT_MODE=1`` / ``STRICT_MODE=1`` env var)
converts every ``log_bypass()`` call into a raised exception so the
review aborts on the first bypass. Useful for qualifying runs where
"anything less than perfect is a failure" is the desired policy.
Default behaviour (strict off) preserves the "log and keep running"
contract the user asked for.

Nothing here raises unless strict mode is on -- a telemetry helper
that blows up is worse than one that silently drops.
"""
from __future__ import annotations

import contextvars
import datetime as _dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


BYPASS_LOG_FILENAME = "bypass_log.jsonl"


# Ambient review-dir binding. The review orchestrator in ``app.py``
# calls ``bind_current_review_dir(review_dir)`` once when the review
# starts; every ``log_bypass()`` call anywhere in the downstream call
# stack then picks up the path automatically. Using a ContextVar keeps
# concurrent reviews isolated from each other (asyncio tasks inherit
# the ambient context at spawn time).
_current_review_dir: contextvars.ContextVar[str] = contextvars.ContextVar(
    "wcag_current_review_dir", default="",
)


def bind_current_review_dir(review_dir: str | Path) -> contextvars.Token:
    """Bind ``review_dir`` as the default for subsequent ``log_bypass`` calls.

    Returns a token the caller can pass to ``unbind_current_review_dir``
    when the review ends. Using the token pattern keeps nested reviews
    correct (unbind pops only the bind it made).
    """
    return _current_review_dir.set(str(review_dir) if review_dir else "")


def unbind_current_review_dir(token: contextvars.Token) -> None:
    """Reset the ambient review dir to whatever it was before
    ``bind_current_review_dir`` set it."""
    try:
        _current_review_dir.reset(token)
    except Exception:
        # cleanup -- best-effort; ContextVar.reset can raise if the token is from a different context
        pass


def current_review_dir() -> str:
    """Return the ambient review dir, or ``""`` if nothing is bound."""
    return _current_review_dir.get() or ""

# Canonical category vocabulary. Keeps the audit summary tidy -- each
# row in the 54-site audit maps to one of these. Use string constants
# at call sites so a typo is a single-spot fix.
CATEGORY_FALLBACK_ENDPOINT = "fallback_endpoint"     # primary failed, secondary used
CATEGORY_FALLBACK_MODEL = "fallback_model"           # prose-restructure, etc.
CATEGORY_RETRY_EXHAUSTED = "retry_exhausted"          # all retries failed
CATEGORY_SILENT_EXCEPT = "silent_except"              # except Exception: pass
CATEGORY_SKIPPED_DATA = "skipped_data"                # empty result, zero-vector, etc.
CATEGORY_RESUME_REUSE = "resume_reuse"                # cached result reused
CATEGORY_HTTP_ERROR = "http_error"                    # 4xx/5xx response
CATEGORY_PARSE_FAIL = "parse_fail"                    # prose couldn't be parsed
CATEGORY_CONFIG_FALLBACK = "config_fallback"          # missing config, degraded path


# Severity tags. Downstream summary code uses these to prioritize
# what the operator should look at first. Reserve ``high`` for
# bypasses that silently lose grounding evidence the judge relied on.
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"


class StrictModeAbort(RuntimeError):
    """Raised by ``log_bypass()`` when strict mode is on.

    The review orchestrator in ``app.py`` should catch this only at the
    outermost review scope. Inside checks/captures/helpers, letting it
    propagate is correct -- that's the whole point of strict mode.
    """


_write_lock = threading.Lock()


def _is_strict() -> bool:
    for key in ("WCAG_STRICT_MODE", "STRICT_MODE"):
        val = os.environ.get(key, "").strip().lower()
        if val in ("1", "true", "yes", "on"):
            return True
    return False


def _log_path(review_dir: str | Path | None) -> Path | None:
    if not review_dir:
        return None
    p = Path(review_dir)
    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
    return p / BYPASS_LOG_FILENAME


def log_bypass(
    review_dir: str | Path | None = None,
    *,
    category: str,
    source: str,
    event: str,
    severity: str = SEVERITY_MEDIUM,
    details: dict[str, Any] | None = None,
    outcome: str = "",
    data_lost: bool = False,
) -> None:
    """Record a single bypass event.

    Args:
        review_dir: Review directory where the JSONL file lives. Pass
            ``None`` or ``""`` to disable persistence (the event still
            logs to the global Python logger so nothing is silent).
        category: One of the ``CATEGORY_*`` constants. Unknown strings
            are accepted but will show up as ``category="other"`` in the
            summary.
        source: ``file:line`` or ``module:function`` identifier. Helps
            an operator locate the code path without grepping.
        event: Short snake_case label for the specific trigger
            (``http_500_fallback_taken``, ``embed_zero_vector``, etc.).
        severity: ``high`` / ``medium`` / ``low``. Guides the summary
            sort order.
        details: Free-form structured context. Everything relevant --
            target URL, model, status code, response body excerpt,
            counts, etc. No truncation is applied here; callers
            should pass the full context they want preserved.
        outcome: One-line prose describing what happened next
            ("tried AI_FALLBACK_URL which also failed; returned None").
        data_lost: True when the bypass resulted in the caller
            receiving incomplete/missing signal (e.g. Layer 3
            retrieval skipped, one video description missing,
            transcript not saved). Downstream summary uses this to
            separate "recovered cleanly" from "run is partially blind".

    When strict mode is on, this function raises
    :class:`StrictModeAbort` AFTER writing the event so the audit
    trail still captures the cause of the abort.
    """
    # Resolve the target path: explicit arg wins, otherwise the ambient
    # binding set by ``bind_current_review_dir``. This lets deep call
    # paths (LLM client, embeddings, etc.) log without plumbing the
    # review_dir through every signature.
    if not review_dir:
        review_dir = current_review_dir()

    record: dict[str, Any] = {
        "timestamp": _dt.datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
        "category": category,
        "severity": severity,
        "source": source,
        "event": event,
        "outcome": outcome,
        "data_lost": bool(data_lost),
        "details": details or {},
    }

    # 1) Global logger line so stdout/uvicorn still shows it.
    logger.warning(
        "BYPASS[%s/%s] %s  source=%s  data_lost=%s  outcome=%s",
        category, severity, event, source, data_lost, outcome or "(none)",
    )

    # 2) Per-review JSONL append (atomic under the lock so concurrent
    #    checks don't interleave partial lines). Non-fatal on I/O
    #    error: the whole point of this helper is to never worsen an
    #    already-bypassed path.
    path = _log_path(review_dir)
    if path is not None:
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        try:
            with _write_lock:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line)
        except Exception as exc:
            logger.warning(
                "bypass_log: could not persist event to %s (%s)",
                path, exc,
            )

    # 3) Strict mode: raise AFTER the write. The review dies but the
    #    audit trail is complete up to the abort point.
    if _is_strict():
        raise StrictModeAbort(
            f"strict mode aborted review: {category}/{event} at {source} "
            f"(severity={severity}, data_lost={data_lost})"
        )


def summarize_bypasses(review_dir: str | Path) -> dict[str, Any]:
    """Aggregate a review's bypass log into a single summary dict.

    Returns counts by category, severity, source, and data_lost. When
    the log is empty or missing, returns ``{"total": 0, ...}`` so the
    caller can always emit a consistent summary into ``audit.json``.
    """
    summary: dict[str, Any] = {
        "total": 0,
        "data_lost_count": 0,
        "by_category": {},
        "by_severity": {},
        "by_source": {},
        "events": [],
    }
    path = _log_path(review_dir)
    if path is None or not path.exists():
        return summary
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except Exception as exc:
        logger.warning("bypass_log: summary read failed (%s)", exc)
        return summary

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        summary["total"] += 1
        if rec.get("data_lost"):
            summary["data_lost_count"] += 1
        cat = str(rec.get("category", "other"))
        sev = str(rec.get("severity", "medium"))
        src = str(rec.get("source", "?"))
        summary["by_category"][cat] = summary["by_category"].get(cat, 0) + 1
        summary["by_severity"][sev] = summary["by_severity"].get(sev, 0) + 1
        summary["by_source"][src] = summary["by_source"].get(src, 0) + 1
        summary["events"].append(rec)
    return summary
