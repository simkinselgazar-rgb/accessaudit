"""Form pause mechanism — pauses the pipeline for user interaction.

When a form requires submission for testing (e.g., to test error
handling), the system:
1. Opens a headed (visible) browser at the page
2. Broadcasts a WebSocket message to the frontend
3. Waits for the user to interact and click "Resume"
4. Captures the post-interaction state and continues
"""
from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

# Keyed by review_id — the recording coroutine waits on these events
_resume_events: dict[str, asyncio.Event] = {}


async def wait_for_user_resume(review_id: str, timeout: float = 300.0) -> bool:
    """Block until the user signals resume (or timeout).

    Returns True if resumed, False if timed out.
    """
    event = asyncio.Event()
    _resume_events[review_id] = event
    logger.info("FORM PAUSE: Waiting for user resume (timeout=%ds)...", timeout)
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        logger.info("FORM PAUSE: User resumed")
        return True
    except asyncio.TimeoutError:
        logger.warning("FORM PAUSE: Timed out after %ds", timeout)
        return False
    finally:
        _resume_events.pop(review_id, None)


def signal_resume(review_id: str) -> bool:
    """Called by the WebSocket handler when the user clicks Resume."""
    event = _resume_events.get(review_id)
    if event:
        event.set()
        return True
    return False
