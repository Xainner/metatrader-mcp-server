"""
Helpers for operations applied to several positions or orders at once.
"""

from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ..strategy import SCOPES, filter_scope, ownership, strategy_magic, validate_strategy_id


def resolve_scope(scope: Optional[str], strategy_id: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    """Magic number for a bulk scope; returns (magic, error)."""
    if scope is not None and scope not in SCOPES:
        return None, f"Invalid scope {scope!r}, must be one of {', '.join(SCOPES)}"
    error = validate_strategy_id(strategy_id)
    if error:
        return None, error
    if scope == "strategy" and not strategy_id:
        return None, "scope 'strategy' requires strategy_id"
    if strategy_id and scope not in (None, "strategy"):
        return None, "strategy_id can only be combined with scope 'strategy'"
    return (strategy_magic(strategy_id) if strategy_id else None), None


def describe(item, magic: Optional[int]) -> Dict:
    """Summary of a position or order for bulk previews."""
    return {
        "id": int(item.ticket),
        "symbol": getattr(item, "symbol", None),
        "type": getattr(item, "type", None),
        "volume": getattr(item, "volume", None) if hasattr(item, "volume") else getattr(item, "volume_current", None),
        "magic": int(getattr(item, "magic", 0) or 0),
        "ownership": ownership(item, magic),
        "profit": getattr(item, "profit", None),
    }


def run_bulk(
    items: Optional[Iterable],
    operation: Callable[[int], Dict],
    list_present: Callable[[], Optional[Iterable]],
    *,
    verb: str,
    noun: str,
    error: Callable[[], object] = lambda: None,
    scope: Optional[str] = None,
    strategy_id: Optional[str] = None,
    dry_run: bool = False,
) -> Dict:
    """
    Apply ``operation`` to every item, then check which ones are still present.

    Args:
        items: Positions or orders to process, as returned by ``mt5.positions_get`` /
            ``mt5.orders_get``. None means the terminal could not be read.
        operation: Function that processes one ticket and returns a client response dict.
        list_present: Function returning the items currently present in the terminal (None on error).
        verb: Past-tense verb for messages (e.g. "Closed").
        noun: Plural noun for messages (e.g. "positions").
        error: Function returning the last terminal error, used in messages.
        scope: ``strategy`` (items with the strategy magic), ``manual`` (magic 0)
            or ``all``. None keeps the library behaviour (all items).
        strategy_id: Strategy for ``scope='strategy'``.
        dry_run: Only list the items that would be processed; nothing is sent.

    Returns:
        dict: ``error`` is True when any item failed or is still present. ``data``
        contains ``results`` (one entry per ticket), ``succeeded``, ``failed``,
        ``unverified`` (final state unknown), ``remaining`` (None if the final
        listing failed) and ``total``.
    """
    magic, scope_error = resolve_scope(scope, strategy_id)
    if scope_error:
        return {"error": True, "message": scope_error, "data": None}
    if items is None:
        return {"error": True, "message": f"Cannot read {noun} from the terminal: {error()}", "data": None}
    items = filter_scope(items, scope=scope, magic=magic)
    targets = tickets(items)
    scope_label = scope or "all"
    if dry_run:
        return {
            "error": False,
            "message": f"Dry run: {len(targets)} {noun} in scope '{scope_label}' would be processed; nothing was sent",
            "data": {"dry_run": True, "scope": scope_label, "strategy_id": strategy_id, "total": len(targets),
                     "items": [describe(item, magic) for item in items]},
        }
    present_all = list_present
    list_present = lambda: filter_scope(present_all(), scope=scope, magic=magic)
    results: List[Dict] = []
    for ticket in targets:
        try:
            response = operation(ticket)
        except Exception as exc:  # keep processing the other tickets
            response = {"error": True, "message": f"Exception: {exc}", "status": None, "retcode": None}
        results.append({
            "id": ticket,
            "error": bool(response.get("error")),
            "message": response.get("message"),
            "status": response.get("status"),
            "retcode": response.get("retcode"),
            "verification": response.get("verification"),
        })

    present = list_present()
    verified = present is not None
    still_present = set(tickets(present)) if verified else set()
    remaining = [t for t in targets if t in still_present]
    for item in results:
        if item["id"] in still_present and not item["error"]:
            item["error"] = True
            item["message"] = f"{item['message']}; still present after the request"
        elif verified and item["id"] not in still_present and item["verification"] == "unknown":
            # The final listing is valid and no longer contains the ticket: the close is confirmed.
            item["error"] = False
            item["verification"] = "confirmed"
            item["message"] = f"{item['message']}; confirmed closed by the final listing"

    failed = [r["id"] for r in results if r["error"]]
    unverified = [r["id"] for r in results if r["error"] and r["verification"] == "unknown"]
    succeeded = [r["id"] for r in results if not r["error"]]
    has_error = bool(failed or remaining) or (bool(targets) and not verified)

    if not targets:
        message = f"No {noun} to process"
    elif has_error:
        message = f"{verb} {len(succeeded)} of {len(targets)} {noun}; failed: {failed}; still present: {remaining}"
        if unverified:
            message += f"; final state unknown for {unverified}, check before retrying"
        if not verified:
            message += f"; could not verify the remaining {noun}: {error()}"
    else:
        message = f"{verb} {len(succeeded)} of {len(targets)} {noun}"

    return {
        "error": has_error,
        "message": message,
        "data": {
            "scope": scope_label,
            "total": len(targets),
            "succeeded": succeeded,
            "failed": failed,
            "unverified": unverified,
            "remaining": remaining if verified else None,
            "results": results,
        },
    }


def tickets(items) -> List[int]:
    """Tickets of the tuples returned by ``mt5.positions_get`` / ``mt5.orders_get``."""
    return [int(item.ticket) for item in (items or ())]
