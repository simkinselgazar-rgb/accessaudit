"""Token-bucket rate limiter for LLM calls. Extracted from functions/llm.py so the rate-limiting policy is reusable and testable independently of LLMClient."""
from __future__ import annotations

import asyncio
import random
import time


class _TokenBucket:
    """Thread/task-safe token bucket for AI_RPM enforcement.

    Safe under AI_MAX_CONCURRENT > 1 -- multiple concurrent tasks can
    call ``acquire()`` simultaneously and the bucket's own asyncio.Lock
    serializes the token math. Each task waits only as long as needed
    to take a token, not as long as the slowest waiter.

    rpm=0 (or negative) means unlimited; acquire() is a no-op.
    Otherwise bucket capacity = rpm, refill rate = rpm / 60 per second.
    """

    def __init__(self, rpm: int) -> None:
        self.rpm = max(0, int(rpm))
        self.capacity = float(self.rpm)
        self.tokens = float(self.rpm)
        self.last_refill = time.monotonic()
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        # Lazy init so we don't need a running event loop at construction.
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def acquire(self) -> None:
        if self.rpm <= 0:
            return
        while True:
            async with self._get_lock():
                now = time.monotonic()
                elapsed = now - self.last_refill
                if elapsed > 0:
                    self.tokens = min(
                        self.capacity,
                        self.tokens + elapsed * (self.rpm / 60.0),
                    )
                    self.last_refill = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                # Need to wait for at least 1 token's worth of refill.
                needed = 1.0 - self.tokens
                wait_s = needed * (60.0 / self.rpm)
            # Release lock while sleeping so other waiters can also
            # check/refill. Add small jitter to avoid a thundering
            # herd where N concurrent tasks all wake at the same ms.
            await asyncio.sleep(wait_s + random.uniform(0, 0.05))
