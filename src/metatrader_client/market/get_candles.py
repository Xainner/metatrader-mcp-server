"""
Structured candle data for analysis: explicit closed/forming bars, ascending
order, declared time basis, incremental queries and a cache of closed bars.
"""

import csv
import io
import math
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import MetaTrader5 as mt5

from ..timebase import server_utc_offset
from ..types import Timeframe

SCHEMA = "candles/v1"
FIELDS = ("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume")
PERIOD_SECONDS = {
    "M1": 60, "M2": 120, "M3": 180, "M4": 240, "M5": 300, "M6": 360, "M10": 600, "M12": 720,
    "M15": 900, "M20": 1200, "M30": 1800, "H1": 3600, "H2": 7200, "H3": 10800, "H4": 14400,
    "H6": 21600, "H8": 28800, "H12": 43200, "D1": 86400, "W1": 604800, "MN1": None,
}
VOLUME_SEMANTICS = (
    "tick_volume: number of price changes in the bar; real_volume: traded volume "
    "reported by the exchange (0 when the broker does not provide it); spread: in points"
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _rows(rates) -> List[Dict[str, Any]]:
    """Rows of ``copy_rates_*`` (numpy structured array or sequence of mappings) as dicts."""
    if rates is None:
        return []
    names = getattr(getattr(rates, "dtype", None), "names", None)
    rows = []
    for item in rates:
        if names:
            row = {name: item[name].item() if hasattr(item[name], "item") else item[name] for name in names}
        elif isinstance(item, dict):
            row = dict(item)
        else:
            row = {name: getattr(item, name) for name in FIELDS}
        row = {name: row.get(name) for name in FIELDS}
        row["time"] = int(row["time"])
        rows.append(row)
    return rows


def _bar_end(start: int, timeframe: str) -> int:
    period = PERIOD_SECONDS[timeframe]
    if period is not None:
        return start + period
    moment = datetime.fromtimestamp(start, tz=timezone.utc)
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    return int(moment.replace(year=year, month=month, day=1, hour=0, minute=0, second=0).timestamp())


def _parse_since(since: Union[int, float, str, None]) -> Tuple[Optional[int], Optional[str]]:
    """``since`` as a server-clock epoch: an integer from a previous ``next_since``, or ISO 8601."""
    if since is None or since == "":
        return None, None
    if isinstance(since, (int, float)):
        return int(since), None
    text = str(since).strip()
    if text.lstrip("-").isdigit():
        return int(text), None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, f"Invalid since {since!r}: use the next_since value or ISO 8601"
    if moment.tzinfo is not None:
        offset = server_utc_offset()
        if offset is None:
            return None, "since has a time zone but MT5_SERVER_UTC_OFFSET_SECONDS is not set; pass a server-time value"
        return int(moment.timestamp()) + offset, None
    return int(moment.replace(tzinfo=timezone.utc).timestamp()), None


class CandleCache:
    """
    Closed bars per (symbol, timeframe), kept in memory.

    Only closed bars are cached. Every use re-reads the most recent bars and
    compares the last cached bar with the terminal; any difference (history
    rewritten by the broker, gap in coverage) invalidates the entry. Quotes
    and risk data are never served from this cache.
    """

    def __init__(self, max_bars: int = 20000):
        self.max_bars = max_bars
        self._data: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._data.get(key)
            return None if entry is None else {"bars": list(entry["bars"]), "stored_at": entry["stored_at"]}

    def put(self, key, bars: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._data[key] = {"bars": bars[-self.max_bars:], "stored_at": time.time()}

    def invalidate(self, key) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


candle_cache = CandleCache()


def _fetch_latest(symbol: str, tf: int, timeframe: str, count: int, server_now: Optional[int], use_cache: bool):
    """Return (rows ascending, cache info, terminal calls, error)."""
    key = (symbol, timeframe)
    period = PERIOD_SECONDS[timeframe]
    cached = candle_cache.get(key) if use_cache and period else None
    info = {"used": False, "invalidated": False, "fetched_bars": 0}
    calls = 0
    # The newest bar is usually forming and never cached: count - 1 closed bars are enough.
    if cached and len(cached["bars"]) >= count - 1 and server_now:
        last = cached["bars"][-1]
        needed = min(count + 2, math.ceil(max(0, server_now - last["time"]) / period) + 2)
        fresh = _rows(mt5.copy_rates_from_pos(symbol, tf, 0, needed))
        calls += 1
        overlap = next((r for r in fresh if r["time"] == last["time"]), None)
        if overlap is not None and overlap == last:
            newer = [r for r in fresh if r["time"] > last["time"]]
            info.update(used=True, fetched_bars=len(fresh))
            return cached["bars"] + newer, info, calls, None
        candle_cache.invalidate(key)
        info["invalidated"] = True
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    calls += 1
    if rates is None:
        code, description = mt5.last_error()
        return None, info, calls, f"copy_rates_from_pos failed (error {code}: {description})"
    rows = _rows(rates)
    info["fetched_bars"] = len(rows)
    return rows, info, calls, None


def get_candles(
    connection,
    *,
    symbol: str,
    timeframe: str,
    count: int = 100,
    since: Union[int, str, None] = None,
    closed_only: bool = False,
    limit: Optional[int] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """
    Candles as structured data.

    Bars are in ascending time order, de-duplicated by time, with ``closed``
    telling whether each bar is complete. A bar is closed when its end is not
    after the last tick time of the symbol; without a tick only the newest bar
    is treated as forming. During market closures the last bar can be complete
    and still reported as forming (conservative).

    Times are MetaTrader 5 server times (``time``, seconds); ``time_iso`` is
    UTC when ``MT5_SERVER_UTC_OFFSET_SECONDS`` is set (``time_basis: "utc"``)
    and server clock otherwise (``time_basis: "server"``).

    Args:
        connection: MetaTrader 5 connection object.
        symbol: Broker symbol.
        timeframe: M1, M5, M15, M30, H1, H4, D1, W1, MN1, ...
        count: Number of most recent bars (ignored with ``since``).
        since: Return only bars that start after this server time (use the
            ``next_since`` of a previous response). The forming bar is
            returned again on every call; replace it rather than appending.
        closed_only: Exclude the forming bar.
        limit: Maximum bars for ``since`` queries (default and cap:
            ``MT5_MAX_CANDLES``, 5000). ``truncated`` tells if more exist.
        use_cache: Reuse cached closed bars for count queries (verified against the terminal).

    Returns:
        dict: {"error", "message", "data": {"schema", "bars", "gaps", "next_since", ...}}.
    """
    started = time.time()
    tf_name = str(timeframe).upper()
    tf = Timeframe.get(tf_name)
    if tf is None or tf_name not in PERIOD_SECONDS:
        return {"error": True, "message": f"Invalid timeframe: '{timeframe}'", "data": None}
    max_bars = _env_int("MT5_MAX_CANDLES", 5000)
    try:
        count = int(count)
    except (TypeError, ValueError):
        return {"error": True, "message": "count must be an integer", "data": None}
    if count <= 0 or count > max_bars:
        return {"error": True, "message": f"count must be between 1 and {max_bars}", "data": None}
    limit = min(int(limit), max_bars) if limit else max_bars
    since_epoch, error = _parse_since(since)
    if error:
        return {"error": True, "message": error, "data": None}

    if not mt5.symbol_select(symbol, True):
        return {"error": True, "message": f"Symbol '{symbol}' not found or cannot be selected", "data": None}
    tick = mt5.symbol_info_tick(symbol)
    server_now = int(getattr(tick, "time", 0) or 0) or None
    calls = 2
    cache_info = None

    if since_epoch is not None:
        end = (server_now or int(time.time())) + 2 * (PERIOD_SECONDS[tf_name] or 31 * 86400)
        rates = mt5.copy_rates_range(symbol, tf, since_epoch + 1, end)
        calls += 1
        if rates is None:
            code, description = mt5.last_error()
            return {"error": True, "message": f"copy_rates_range failed (error {code}: {description})", "data": None}
        rows = [r for r in _rows(rates) if r["time"] > since_epoch]
    else:
        rows, cache_info, extra_calls, error = _fetch_latest(symbol, tf, tf_name, count, server_now, use_cache)
        calls += extra_calls
        if error:
            return {"error": True, "message": error, "data": None}

    # Ascending order without duplicates (the newest copy of a time wins).
    by_time: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        by_time[row["time"]] = row
    duplicates = len(rows) - len(by_time)
    rows = [by_time[t] for t in sorted(by_time)]

    for index, row in enumerate(rows):
        if server_now:
            row["closed"] = _bar_end(row["time"], tf_name) <= server_now
        else:
            row["closed"] = index < len(rows) - 1

    if since_epoch is None:
        closed_rows = [r for r in rows if r["closed"]]
        if use_cache and PERIOD_SECONDS[tf_name] and closed_rows:
            candle_cache.put((symbol, tf_name), [{k: r[k] for k in FIELDS} for r in closed_rows])
        rows = rows[-count:]

    truncated = False
    if since_epoch is not None and len(rows) > limit:
        rows, truncated = rows[:limit], True
    forming_included = bool(rows) and not rows[-1]["closed"]
    if closed_only:
        rows = [r for r in rows if r["closed"]]
        forming_included = False

    offset = server_utc_offset()
    for row in rows:
        moment = datetime.fromtimestamp(row["time"] - (offset or 0), tz=timezone.utc)
        row["time_iso"] = moment.isoformat().replace("+00:00", "Z") if offset is not None else moment.replace(tzinfo=None).isoformat()

    period = PERIOD_SECONDS[tf_name]
    gaps = []
    for previous, current in zip(rows, rows[1:]):
        expected = _bar_end(previous["time"], tf_name)
        if current["time"] > expected:
            missing = (current["time"] - previous["time"]) // period - 1 if period else None
            gaps.append({"after": previous["time"], "before": current["time"], "missing_periods": missing})

    closed_times = [r["time"] for r in rows if r["closed"]]
    next_since = closed_times[-1] if closed_times else since_epoch
    data = {
        "schema": SCHEMA,
        "symbol": symbol,
        "timeframe": tf_name,
        "period_seconds": period,
        "order": "ascending",
        "time_basis": "utc" if offset is not None else "server",
        "server_utc_offset_seconds": offset,
        "server_time_now": server_now,
        "closed_only": bool(closed_only),
        "forming_bar_included": forming_included,
        "count": len(rows),
        "bars": rows,
        "next_since": next_since,
        "truncated": truncated,
        "gaps": gaps,
        "gaps_note": "Gaps are intervals without bars (market closures, missing history); they are not filled.",
        "duplicates_removed": duplicates,
        "volume_semantics": VOLUME_SEMANTICS,
        "cache": cache_info,
        "terminal_calls": calls,
        "duration_ms": round((time.time() - started) * 1000, 3),
    }
    return {"error": False, "message": f"{len(rows)} {tf_name} bars of {symbol}", "data": data}


def candles_to_csv(data: Dict[str, Any]) -> str:
    """CSV export of a ``get_candles`` result (compatibility format)."""
    buffer = io.StringIO()
    columns = ["time", "time_iso", *FIELDS[1:], "closed"]
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in data["bars"]:
        writer.writerow(row)
    return buffer.getvalue()


def get_candles_multi(
    connection,
    *,
    symbol: str,
    timeframes: Iterable[str],
    count: int = 100,
    closed_only: bool = False,
) -> Dict[str, Any]:
    """
    Candles of several timeframes for one symbol, with a result or an error
    per timeframe. ``error`` is True when any timeframe failed; the others are
    still returned under ``results``.
    """
    timeframes = [str(t).upper() for t in timeframes or []]
    if not timeframes:
        return {"error": True, "message": "timeframes is required", "data": None}
    results, errors = {}, {}
    for tf in dict.fromkeys(timeframes):
        try:
            response = get_candles(connection, symbol=symbol, timeframe=tf, count=count, closed_only=closed_only)
        except Exception as exc:
            response = {"error": True, "message": f"{type(exc).__name__}: {exc}", "data": None}
        if response["error"]:
            errors[tf] = response["message"]
        else:
            results[tf] = response["data"]
    message = f"{len(results)} of {len(results) + len(errors)} timeframes returned"
    if errors:
        message += f"; failed: {', '.join(errors)}"
    return {"error": bool(errors), "message": message, "data": {"symbol": symbol, "results": results, "errors": errors}}
