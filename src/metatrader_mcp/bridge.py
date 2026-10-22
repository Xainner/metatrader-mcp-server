"""
Execution of MetaTrader 5 calls for the MCP tools.

Every tool call that reaches MetaTrader 5 runs on one dedicated worker
thread, so the event loop keeps answering (``get_bridge_health`` included)
while a terminal call is slow, and calls to the terminal are serialized.

Deadlines: a call that exceeds its stage deadline returns an error to the
client, but the native call cannot be interrupted: it keeps running on the
worker. Until it finishes, new calls fail immediately with a "bridge busy"
error that names the blocked call. A timed-out send is reported as an
unknown outcome, never as a cancellation; the request registry keeps it as
``sending``/``unknown`` until it is reconciled.

Recovery: before a call, the terminal connection is checked; when it is
down, reconnection is attempted under a bounded policy (attempts per
window, exponential backoff). Reconnecting never repeats a trade request.

Settings: ``MCP_READ_TIMEOUT_SECONDS`` (30), ``MCP_SEND_TIMEOUT_SECONDS`` (60),
``MCP_AUTO_RECONNECT`` (true), ``MCP_RECONNECT_MAX_ATTEMPTS`` (3 per window),
``MCP_RECONNECT_WINDOW_SECONDS`` (300).
"""

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from metatrader_client.metrics import metrics

logger = logging.getLogger(__name__)

STAGES = ("read", "preview", "send")


class BridgeError(RuntimeError):
    """Base class of errors raised before or around a terminal call."""

    def __init__(self, message: str, *, stage: str, tool: str, state: str):
        super().__init__(message)
        self.stage = stage
        self.tool = tool
        self.state = state


class BridgeTimeout(BridgeError):
    pass


class BridgeBusy(BridgeError):
    pass


class BridgeUnavailable(BridgeError):
    pass


def _env_float(env, name: str, default: float) -> float:
    raw = env.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_bool(env, name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


class ReconnectPolicy:
    """Bounded reconnection attempts: at most ``max_attempts`` per ``window`` seconds, with backoff."""

    def __init__(self, *, max_attempts: int = 3, window: float = 300.0, base_delay: float = 2.0):
        self.max_attempts = max_attempts
        self.window = window
        self.base_delay = base_delay
        self.attempts: List[float] = []
        self.last_error: Optional[str] = None
        self.last_success: Optional[float] = None

    def allowed(self, now: Optional[float] = None) -> Optional[str]:
        """None when an attempt is allowed now, else the reason it is not."""
        now = time.time() if now is None else now
        self.attempts = [t for t in self.attempts if now - t < self.window]
        if len(self.attempts) >= self.max_attempts:
            return f"{len(self.attempts)} reconnection attempts in the last {self.window:g}s (limit {self.max_attempts})"
        if self.attempts:
            delay = self.base_delay * (2 ** (len(self.attempts) - 1))
            wait = self.attempts[-1] + delay - now
            if wait > 0:
                return f"next reconnection attempt allowed in {wait:.1f}s"
        return None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "recent_attempts": len(self.attempts),
            "max_attempts": self.max_attempts,
            "window_seconds": self.window,
            "last_error": self.last_error,
            "last_success": self.last_success,
        }


class Bridge:
    """Single worker thread with deadlines, busy detection and reconnection."""

    def __init__(self, client: Optional[Any], *, env=None):
        env = os.environ if env is None else env
        self.client = client
        self.timeouts = {
            "read": _env_float(env, "MCP_READ_TIMEOUT_SECONDS", 30.0),
            "preview": _env_float(env, "MCP_READ_TIMEOUT_SECONDS", 30.0),
            "send": _env_float(env, "MCP_SEND_TIMEOUT_SECONDS", 60.0),
        }
        self.auto_reconnect = _env_bool(env, "MCP_AUTO_RECONNECT", True)
        self.reconnect = ReconnectPolicy(
            max_attempts=int(_env_float(env, "MCP_RECONNECT_MAX_ATTEMPTS", 3)),
            window=_env_float(env, "MCP_RECONNECT_WINDOW_SECONDS", 300.0),
        )
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5-bridge")
        self._lock = threading.Lock()
        self._running: Optional[Dict[str, Any]] = None
        self._stuck: Optional[Dict[str, Any]] = None
        self.connectivity: Dict[str, Any] = {"connected": None, "checked_at": None, "error": None}
        self.late_completions = 0

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------
    async def run(self, tool: str, stage: str, function: Callable[[], Any]) -> Any:
        """Run ``function`` on the worker with the deadline of ``stage``."""
        with self._lock:
            stuck = self._stuck
        if stuck is not None:
            age = time.time() - stuck["started"]
            metrics.record_error(stage, f"{tool}: bridge busy")
            raise BridgeBusy(
                f"The MetaTrader 5 bridge is still blocked by {stuck['tool']} ({stuck['stage']} stage) "
                f"running for {age:.1f}s past its deadline. {tool} was not started; retry later or check "
                "get_bridge_health. The blocked call may still complete.",
                stage=stage, tool=tool, state="not_started",
            )
        timeout = self.timeouts.get(stage, self.timeouts["read"])
        started = time.perf_counter()
        ticket = {"started": False, "abandoned": False}
        future = self._executor.submit(self._call, tool, stage, function, ticket)
        try:
            result = await asyncio.wait_for(asyncio.wrap_future(future), timeout)
        except asyncio.TimeoutError:
            elapsed = (time.perf_counter() - started) * 1000
            with self._lock:
                never_started = not ticket["started"]
                if never_started:
                    # Still queued behind another call: it will be skipped, so nothing is sent.
                    ticket["abandoned"] = True
                elif self._running is not None:
                    self._stuck = dict(self._running)
            metrics.record(stage, elapsed, ok=False, error=f"{tool}: timeout after {timeout:g}s")
            if never_started:
                raise BridgeTimeout(
                    f"{tool} waited {timeout:g}s for an earlier MetaTrader 5 call and was not started; nothing was sent.",
                    stage=stage, tool=tool, state="not_started",
                ) from None
            state = "unknown" if stage == "send" else "not_completed"
            raise BridgeTimeout(
                f"{tool} exceeded the {stage} deadline of {timeout:g}s. The MetaTrader 5 call may still be "
                "running and its outcome is unknown; this is not a confirmed cancellation."
                + (" Check the request status and the positions before any retry." if stage == "send" else ""),
                stage=stage, tool=tool, state=state,
            ) from None
        except BridgeError:
            metrics.record(stage, (time.perf_counter() - started) * 1000, ok=False, error=f"{tool}: bridge error")
            raise
        except Exception as exc:
            metrics.record(stage, (time.perf_counter() - started) * 1000, ok=False, error=f"{tool}: {type(exc).__name__}: {exc}")
            raise
        metrics.record(stage, (time.perf_counter() - started) * 1000, ok=True)
        return result

    def _call(self, tool: str, stage: str, function: Callable[[], Any], ticket: Dict[str, bool]) -> Any:
        with self._lock:
            if ticket["abandoned"]:
                return None
            ticket["started"] = True
            self._running = {"tool": tool, "stage": stage, "started": time.time()}
        try:
            self._ensure_connected(tool, stage)
            return function()
        finally:
            with self._lock:
                if self._stuck is not None:
                    self.late_completions += 1
                    logger.warning("%s (%s) finished %.1fs after it started, past its deadline",
                                   tool, stage, time.time() - self._stuck["started"])
                self._running = None
                self._stuck = None

    def _ensure_connected(self, tool: str, stage: str) -> None:
        """Check the terminal and reconnect within the policy. Never repeats a request."""
        if self.client is None:
            return
        now = time.time()
        try:
            connected = bool(self.client.is_connected())
            error = None
        except Exception as exc:
            connected, error = False, f"{type(exc).__name__}: {exc}"
        self.connectivity = {"connected": connected, "checked_at": now, "error": error}
        if connected:
            return
        if not self.auto_reconnect:
            raise BridgeUnavailable(
                f"The terminal is not connected; {tool} was not started (automatic reconnection is disabled)",
                stage=stage, tool=tool, state="not_started",
            )
        reason = self.reconnect.allowed(now)
        if reason:
            raise BridgeUnavailable(
                f"The terminal is not connected and no reconnection is attempted now ({reason}); {tool} was not started",
                stage=stage, tool=tool, state="not_started",
            )
        self.reconnect.attempts.append(now)
        try:
            self.client.connect()
            connected = bool(self.client.is_connected())
        except Exception as exc:
            self.reconnect.last_error = f"{type(exc).__name__}: {exc}"[:300]
            connected = False
        self.connectivity = {"connected": connected, "checked_at": time.time(), "error": None if connected else self.reconnect.last_error}
        if not connected:
            metrics.record_error("connect", self.reconnect.last_error or "reconnection failed")
            raise BridgeUnavailable(
                f"The terminal is not connected and reconnection failed ({self.reconnect.last_error}); {tool} was not started",
                stage=stage, tool=tool, state="not_started",
            )
        self.reconnect.last_success = time.time()
        logger.info("Reconnected to the MetaTrader 5 terminal before %s", tool)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    def state(self) -> Dict[str, Any]:
        with self._lock:
            running = dict(self._running) if self._running else None
            stuck = dict(self._stuck) if self._stuck else None
        now = time.time()
        for item in (running, stuck):
            if item:
                item["running_seconds"] = round(now - item["started"], 3)
        return {
            "running": running,
            "blocked": stuck,
            "late_completions": self.late_completions,
            "timeouts_seconds": dict(self.timeouts),
            "auto_reconnect": self.auto_reconnect,
            "reconnect": self.reconnect.snapshot(),
            "connectivity": dict(self.connectivity),
        }

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
