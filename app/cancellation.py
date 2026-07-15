"""Review cancellation primitives.

Holds the global cancellation flag set, the ``ReviewCancelled`` exception
that long-running orchestrators raise when they detect a flag, and
``resolve_review_dir`` for safely turning a review_id from a URL path
into an on-disk path. These are leaf utilities — no other ``app.*``
module may import upstream from here without creating a cycle.
"""
from __future__ import annotations

import threading
from pathlib import Path

from config import REVIEWS_DIR
from functions.security import validate_review_id


_cancelled_reviews: set[str] = set()
_cancelled_reviews_lock = threading.Lock()


class ReviewCancelled(Exception):
    """Raised when a review is cancelled mid-processing."""
    pass


def check_cancelled(review_id: str) -> None:
    """Call this periodically during processing. Raises if cancelled.

    The check+discard pair runs under a lock so concurrent checks don't
    race and drop the cancellation flag before all of them see it.
    """
    with _cancelled_reviews_lock:
        cancelled = review_id in _cancelled_reviews
        if cancelled:
            _cancelled_reviews.discard(review_id)
    if cancelled:
        raise ReviewCancelled(f"Review {review_id} was cancelled")


def resolve_review_dir(review_id: str) -> Path:
    """Validate review_id and return the corresponding directory path.

    Every route that takes review_id as a path parameter should call
    this so an attacker can't smuggle path-traversal sequences through
    a URL. Raises InvalidReviewIdError on bad input; callers should
    translate that to a 400 response.
    """
    validate_review_id(review_id)
    return REVIEWS_DIR / review_id
