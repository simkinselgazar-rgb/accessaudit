"""Review storage management."""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from config import REVIEWS_DIR
from models import ReviewMeta, TestResult

logger = logging.getLogger(__name__)

RUNNING_STATUSES = (
    "queued", "capturing", "testing", "generating_report", "crawling",
    "aggregating", "reviewing", "selecting", "authenticating",
    "testing_documents",
)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write content to path atomically (temp file + os.replace).

    A concurrent reader never sees a half-written file: it reads either
    the old complete content or the new complete content.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass  # temp file already replaced or never created
        raise


def ensure_reviews_dir() -> Path:
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    return REVIEWS_DIR


def create_review(meta: ReviewMeta) -> Path:
    """Create a new review directory and save initial metadata."""
    review_dir = ensure_reviews_dir() / meta.review_id
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "captures").mkdir(exist_ok=True)
    (review_dir / "captures" / "elements").mkdir(exist_ok=True)
    (review_dir / "captures" / "interactive").mkdir(exist_ok=True)
    (review_dir / "captures" / "interactive" / "tab_walk").mkdir(exist_ok=True)
    (review_dir / "captures" / "interactive" / "focus").mkdir(exist_ok=True)
    (review_dir / "captures" / "interactive" / "hover").mkdir(exist_ok=True)
    (review_dir / "captures" / "interactive" / "error_states").mkdir(exist_ok=True)
    (review_dir / "captures" / "observation_frames").mkdir(exist_ok=True)
    (review_dir / "captures" / "observation_frames" / "ai").mkdir(exist_ok=True)
    (review_dir / "tests").mkdir(exist_ok=True)
    (review_dir / "report").mkdir(exist_ok=True)
    save_meta(review_dir, meta)
    return review_dir


def save_meta(review_dir: Path, meta: ReviewMeta) -> None:
    """Save review metadata."""
    meta_path = review_dir / "meta.json"
    _atomic_write_text(meta_path, json.dumps(meta.to_dict(), indent=2, default=str))


def load_meta(review_dir: Path) -> ReviewMeta:
    """Load review metadata."""
    meta_path = review_dir / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"No meta.json in {review_dir}")
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    meta = ReviewMeta()
    for k, v in data.items():
        if hasattr(meta, k):
            setattr(meta, k, v)
    return meta


def save_test_result(review_dir: Path, result: TestResult) -> None:
    """Save a test result to the tests directory."""
    criterion_dir = review_dir / "tests" / result.criterion_id.replace(".", "_")
    criterion_dir.mkdir(parents=True, exist_ok=True)
    result_path = criterion_dir / "result.json"
    _atomic_write_text(result_path, json.dumps(result.to_dict(), indent=2, default=str))


def load_test_result(review_dir: Path, criterion_id: str) -> dict | None:
    """Load a test result."""
    criterion_dir = review_dir / "tests" / criterion_id.replace(".", "_")
    result_path = criterion_dir / "result.json"
    if not result_path.exists():
        return None
    return json.loads(result_path.read_text(encoding="utf-8"))


def load_all_test_results(review_dir: Path) -> list[dict]:
    """Load all test results for a review."""
    tests_dir = review_dir / "tests"
    if not tests_dir.exists():
        return []
    results = []
    for criterion_dir in sorted(tests_dir.iterdir()):
        if criterion_dir.is_dir():
            result_path = criterion_dir / "result.json"
            if result_path.exists():
                results.append(json.loads(result_path.read_text(encoding="utf-8")))
    return results


def save_prompt(review_dir: Path, criterion_id: str, system_prompt: str, user_prompt: str, prefix: str = "") -> None:
    """Save AI prompts for debugging."""
    criterion_dir = review_dir / "tests" / criterion_id.replace(".", "_")
    criterion_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{prefix}prompt.txt" if prefix else "prompt.txt"
    prompt_path = criterion_dir / filename
    content = f"=== SYSTEM PROMPT ===\n{system_prompt or '(empty)'}\n\n=== USER PROMPT ===\n{user_prompt or '(empty)'}"
    prompt_path.write_text(content, encoding="utf-8")


def save_ai_response(review_dir: Path, criterion_id: str, response: str) -> None:
    """Save raw AI response."""
    criterion_dir = review_dir / "tests" / criterion_id.replace(".", "_")
    criterion_dir.mkdir(parents=True, exist_ok=True)
    (criterion_dir / "ai_response.txt").write_text(response, encoding="utf-8")


def save_programmatic_data(review_dir: Path, criterion_id: str, data: dict) -> None:
    """Save programmatic check data."""
    criterion_dir = review_dir / "tests" / criterion_id.replace(".", "_")
    criterion_dir.mkdir(parents=True, exist_ok=True)
    (criterion_dir / "programmatic_data.json").write_text(
        json.dumps(data, indent=2, default=str), encoding="utf-8"
    )


def save_user_decision(review_dir: Path, criterion_id: str, finding_id: str,
                       status: str, reason: str = "") -> None:
    """Save a user decision on a finding."""
    criterion_dir = review_dir / "tests" / criterion_id.replace(".", "_")
    criterion_dir.mkdir(parents=True, exist_ok=True)
    decisions_path = criterion_dir / "user_decisions.json"
    decisions = {}
    if decisions_path.exists():
        decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    decisions[finding_id] = {
        "status": status,
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    decisions_path.write_text(json.dumps(decisions, indent=2), encoding="utf-8")


def load_user_decisions(review_dir: Path, criterion_id: str) -> dict:
    """Load user decisions for a criterion."""
    criterion_dir = review_dir / "tests" / criterion_id.replace(".", "_")
    decisions_path = criterion_dir / "user_decisions.json"
    if not decisions_path.exists():
        return {}
    return json.loads(decisions_path.read_text(encoding="utf-8"))


def list_reviews() -> list[ReviewMeta]:
    """List all reviews, newest first."""
    reviews = []
    reviews_dir = ensure_reviews_dir()
    for review_dir in sorted(reviews_dir.iterdir(), reverse=True):
        if review_dir.is_dir():
            try:
                meta = load_meta(review_dir)
                reviews.append(meta)
            except Exception:
                logger.warning(
                    "list_reviews: skipping %s -- could not read %s",
                    review_dir, review_dir / "meta.json", exc_info=True,
                )
                continue
    return reviews


def delete_review(review_id: str) -> bool:
    """Delete a review directory."""
    review_dir = REVIEWS_DIR / review_id
    if not review_dir.exists():
        return False
    meta = load_meta(review_dir)
    if meta.status in RUNNING_STATUSES:
        return False
    shutil.rmtree(review_dir, ignore_errors=True)
    return True


def delete_all_reviews() -> tuple[int, int]:
    """Delete all reviews. Returns (deleted, skipped)."""
    deleted = 0
    skipped = 0
    reviews_dir = ensure_reviews_dir()
    for review_dir in list(reviews_dir.iterdir()):
        if review_dir.is_dir():
            try:
                meta = load_meta(review_dir)
                if meta.status in RUNNING_STATUSES:
                    skipped += 1
                    continue
            except Exception:
                logger.warning(
                    "delete_all_reviews: skipping %s -- could not read meta (possible in-progress review)",
                    review_dir,
                    exc_info=True,
                )
                skipped += 1
                continue
            shutil.rmtree(review_dir, ignore_errors=True)
            deleted += 1
    return deleted, skipped
