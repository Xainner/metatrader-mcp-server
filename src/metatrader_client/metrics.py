"""
Latency and error statistics kept in memory, bounded in size.

Stages are free-form names (``read``, ``preview``, ``send``, ``broker_send``,
``confirm``, ...). The MCP bridge records the time of whole tool calls; the
order functions record the time of ``order_send`` and of the confirmation
reads, so the broker and confirmation times can be told apart from the total.
"""

import math
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

MAX_SAMPLES = 512
MAX_ERRORS = 50


def _percentile(sorted_values: List[float], pct: float) -> Optional[float]:
    if not sorted_values:
        return None
    index = max(0, math.ceil(pct / 100 * len(sorted_values)) - 1)
    return round(sorted_values[index], 3)


class LatencyStats:
    """Bounded latency samples (milliseconds) and recent errors per stage."""

    def __init__(self, max_samples: int = MAX_SAMPLES, max_errors: int = MAX_ERRORS):
        self._lock = threading.Lock()
        self._samples: Dict[str, Deque[float]] = {}
        self._counts: Dict[str, Dict[str, int]] = {}
        self._last_success: Dict[str, float] = {}
        self._errors: Deque[Dict[str, Any]] = deque(maxlen=max_errors)
        self._max_samples = max_samples

    def record(self, stage: str, duration_ms: float, *, ok: bool = True, error: Optional[str] = None) -> None:
        """Record one sample; ``error`` is kept (truncated) in the recent errors list."""
        with self._lock:
            samples = self._samples.setdefault(stage, deque(maxlen=self._max_samples))
            samples.append(float(duration_ms))
            counts = self._counts.setdefault(stage, {"ok": 0, "error": 0})
            counts["ok" if ok else "error"] += 1
            if ok:
                self._last_success[stage] = time.time()
            elif error:
                self._errors.append({"stage": stage, "time": time.time(), "error": str(error)[:300]})

    def record_error(self, stage: str, error: str) -> None:
        """Record an error that has no meaningful duration."""
        with self._lock:
            counts = self._counts.setdefault(stage, {"ok": 0, "error": 0})
            counts["error"] += 1
            self._errors.append({"stage": stage, "time": time.time(), "error": str(error)[:300]})

    def last_success(self, stage: str) -> Optional[float]:
        with self._lock:
            return self._last_success.get(stage)

    def snapshot(self) -> Dict[str, Any]:
        """Percentiles per stage (over the retained samples) and the recent errors."""
        with self._lock:
            stages = {}
            for stage, samples in self._samples.items():
                values = sorted(samples)
                stages[stage] = {
                    "count_ok": self._counts[stage]["ok"],
                    "count_error": self._counts[stage]["error"],
                    "samples": len(values),
                    "p50_ms": _percentile(values, 50),
                    "p95_ms": _percentile(values, 95),
                    "p99_ms": _percentile(values, 99),
                    "max_ms": round(values[-1], 3) if values else None,
                    "last_success": self._last_success.get(stage),
                }
            for stage, counts in self._counts.items():
                stages.setdefault(stage, {"count_ok": counts["ok"], "count_error": counts["error"], "samples": 0})
            return {"stages": stages, "recent_errors": list(self._errors)}

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._counts.clear()
            self._last_success.clear()
            self._errors.clear()


# Process-wide statistics used by the client functions and the MCP bridge.
metrics = LatencyStats()


class timed:
    """Context manager measuring a block in milliseconds: ``with timed() as t: ...; t.ms``."""

    def __enter__(self):
        self._start = time.perf_counter()
        self.ms = 0.0
        return self

    def __exit__(self, *exc):
        self.ms = (time.perf_counter() - self._start) * 1000
        return False
