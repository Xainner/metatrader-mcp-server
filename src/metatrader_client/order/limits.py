"""
Visibility of the risk limits and the new entries switch.
"""

from typing import Any, Dict

from ..risk import ENTRIES_SETTING, entries_block_reason, get_risk_guard, set_new_entries
from ..store import get_store
from ..timebase import iso_utc


def get_risk_limits(connection) -> Dict[str, Any]:
    """
    Effective risk limits, the new entries switch and their change history.

    Limits come from the client configuration and ``MT5_*`` environment
    variables; every distinct set of effective limits is stored with its time
    when the connection is created, so changes leave a record.
    """
    guard = get_risk_guard(connection)
    store = get_store(connection)
    history = store.limits_history()
    for item in history:
        item["time_iso"] = iso_utc(item["time"])
    entries = store.get_setting(ENTRIES_SETTING)
    entries_history = store.setting_history(ENTRIES_SETTING)
    for item in entries_history:
        item["time_iso"] = iso_utc(item["time"])
    return {
        "error": False,
        "message": "Effective risk limits",
        "data": {
            "effective": guard.config.as_dict(),
            "new_entries_block": entries_block_reason(guard.config, store),
            "new_entries_switch": entries["value"] if entries else {"enabled": True, "reason": None, "source": "default"},
            "limits_history": history,
            "new_entries_history": entries_history,
            "note": "Values are chosen by the user or an explicit policy; null disables a limit.",
        },
    }


def disable_new_entries(connection, *, reason: str, source: str = "client") -> Dict[str, Any]:
    """
    Block every new opening (market or pending) until entries are enabled again.

    Closing positions, cancelling orders and modifying stops remain available.
    The change is stored durably with its reason.
    """
    try:
        value = set_new_entries(get_store(connection), enabled=False, reason=reason, source=source)
    except ValueError as exc:
        return {"error": True, "message": str(exc), "data": None}
    return {"error": False, "message": f"New entries disabled: {value['reason']}", "data": value}


def enable_new_entries(connection, *, reason: str, source: str = "client") -> Dict[str, Any]:
    """
    Allow new openings again (the configuration switch ``MT5_ALLOW_NEW_ENTRIES``
    still applies). Not exposed as an MCP tool: re-enabling is left to the
    operator, so an agent cannot undo a stop it was given.
    """
    try:
        value = set_new_entries(get_store(connection), enabled=True, reason=reason, source=source)
    except ValueError as exc:
        return {"error": True, "message": str(exc), "data": None}
    return {"error": False, "message": f"New entries enabled: {value['reason']}", "data": value}
