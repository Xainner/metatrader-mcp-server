"""
Short-lived previews of opening orders.

``preview_order`` stores the fingerprint of the request it checked, the quote
it used and an expiry. An order sent with that ``preview_id`` is accepted only
if it is the same request (no material change), the preview has not expired
and the market has not moved more than the allowed distance. A preview does
not reserve a price and does not guarantee execution; the risk limits are
checked again when the order is sent. Each preview can be used once.

Settings: ``MT5_PREVIEW_TTL_SECONDS`` (default 30),
``MT5_PREVIEW_MAX_PRICE_MOVE_POINTS`` (default: the order deviation, 20 points),
``MT5_REQUIRE_PREVIEW`` (default false: orders without a preview are allowed).
"""

import os
import threading
import time
import uuid
from typing import Any, Dict, Optional

from .._env import env_bool, env_float


class PreviewStore:
    def __init__(self, *, ttl: Optional[float] = None, max_move_points: Optional[float] = None,
                 require_preview: Optional[bool] = None, env=None):
        env = os.environ if env is None else env
        self.ttl = ttl if ttl is not None else env_float(env, "MT5_PREVIEW_TTL_SECONDS", 30.0)
        self.max_move_points = (
            max_move_points if max_move_points is not None else env_float(env, "MT5_PREVIEW_MAX_PRICE_MOVE_POINTS", None)
        )
        self.require_preview = (
            require_preview if require_preview is not None else env_bool(env, "MT5_REQUIRE_PREVIEW", False)
        )
        self._items: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def save(self, *, fingerprint: str, entry_price: float, point: float, market: bool, deviation: int) -> Dict[str, Any]:
        now = time.time()
        preview_id = uuid.uuid4().hex
        item = {
            "fingerprint": fingerprint,
            "entry_price": float(entry_price),
            "point": float(point) or 1e-5,
            "market": market,
            "max_move_points": self.max_move_points if self.max_move_points is not None else float(deviation),
            "created_at": now,
            "expires_at": now + self.ttl,
        }
        with self._lock:
            self._items = {k: v for k, v in self._items.items() if v["expires_at"] > now}
            self._items[preview_id] = item
        return {"preview_id": preview_id, "expires_at": item["expires_at"], "max_price_move_points": item["max_move_points"]}

    def consume(self, preview_id: Optional[str], *, fingerprint: str, entry_price: float) -> Optional[str]:
        """Validate and consume a preview; returns an error message or None."""
        if not preview_id:
            return "A preview is required before sending orders (MT5_REQUIRE_PREVIEW): call preview_order first" if self.require_preview else None
        now = time.time()
        with self._lock:
            item = self._items.get(preview_id)
            if item is None:
                return f"Unknown or already used preview_id {preview_id}; create a new preview"
            if item["expires_at"] <= now:
                del self._items[preview_id]
                return f"Preview {preview_id} expired {now - item['expires_at']:.1f}s ago; create a new preview"
            if item["fingerprint"] != fingerprint:
                return (
                    f"The order differs from preview {preview_id} (symbol, type, volume, price, SL/TP or strategy "
                    "changed); create a new preview"
                )
            if item["market"]:
                moved = abs(float(entry_price) - item["entry_price"]) / item["point"]
                if moved > item["max_move_points"] + 1e-6:
                    del self._items[preview_id]
                    return (
                        f"The price moved {moved:.1f} points since preview {preview_id} "
                        f"(limit {item['max_move_points']:g}); create a new preview"
                    )
            del self._items[preview_id]
        return None


_default_store: Optional[PreviewStore] = None
_default_lock = threading.Lock()


def get_preview_store(connection) -> PreviewStore:
    store = getattr(connection, "preview_store", None)
    if isinstance(store, PreviewStore):
        return store
    global _default_store
    with _default_lock:
        if _default_store is None:
            _default_store = PreviewStore()
        return _default_store


def reset_default_preview_store() -> None:
    global _default_store
    with _default_lock:
        _default_store = None
