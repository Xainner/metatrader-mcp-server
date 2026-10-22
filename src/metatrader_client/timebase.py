"""
Time conversions for MetaTrader 5 timestamps.

MetaTrader 5 returns tick and bar times as seconds since 1970 in the trade
server clock, which is usually not UTC (many servers run at UTC+2/UTC+3). The
offset is not exposed by the Python API, so it must be configured:

``MT5_SERVER_UTC_OFFSET_SECONDS``: server clock minus UTC, in seconds (for
example ``10800`` for UTC+3). When it is not set, times are reported with
``time_basis: "server"`` and ages relative to the local clock are not computed.
"""

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def server_utc_offset(env=None) -> Optional[int]:
    """Configured offset of the trade server clock in seconds, or None when unknown."""
    env = os.environ if env is None else env
    raw = env.get("MT5_SERVER_UTC_OFFSET_SECONDS")
    if raw in (None, ""):
        return None
    return int(float(raw))


def iso_utc(epoch: Optional[float]) -> Optional[str]:
    """ISO 8601 UTC representation of a Unix timestamp."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def describe_server_time(server_epoch: Optional[float], *, offset: Optional[int] = None) -> Dict[str, Any]:
    """
    Describe a server timestamp: the raw value, its UTC equivalent when the
    offset is known, and the basis used.
    """
    if server_epoch is None:
        return {"server_time": None, "time_utc": None, "time_basis": None}
    result = {"server_time": int(server_epoch), "time_utc": None, "time_basis": "server"}
    if offset is not None:
        result["time_utc"] = iso_utc(float(server_epoch) - offset)
        result["time_basis"] = "utc"
    return result


def age_seconds(server_epoch: Optional[float], *, offset: Optional[int] = None, now: Optional[float] = None) -> Optional[float]:
    """Age of a server timestamp against the local clock; None when the offset is unknown."""
    if server_epoch is None or offset is None:
        return None
    now = time.time() if now is None else now
    return round(now - (float(server_epoch) - offset), 3)
