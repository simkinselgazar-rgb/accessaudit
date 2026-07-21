"""WebSocket connection registry and broadcast helper.

A single module-level dict holds the live WebSocket clients keyed by
review_id. Every long-running orchestrator and route imports
``broadcast`` (and the dict itself when it needs to mutate it) from
here so there is exactly one source of truth.
"""
from __future__ import annotations

import threading

from fastapi import WebSocket


_active_websockets: dict[str, list[WebSocket]] = {}
_active_websockets_lock = threading.Lock()


async def broadcast(review_id: str, message: dict) -> None:
    """Broadcast a message to all WebSocket clients for a review.

    Snapshot the client list under a lock so the websocket_endpoint
    .remove() call cannot mutate it mid-iteration (which would either
    crash with RuntimeError or silently skip clients).
    """
    with _active_websockets_lock:
        clients = list(_active_websockets.get(review_id, []))
    dead = []
    for ws in clients:
        try:
            await ws.send_json(message)
        except Exception:
            # Send failure means the client disconnected — collect the
            # socket for cleanup; nothing to log per-message.
            dead.append(ws)
    if dead:
        with _active_websockets_lock:
            live = _active_websockets.get(review_id, [])
            for ws in dead:
                if ws in live:
                    live.remove(ws)
