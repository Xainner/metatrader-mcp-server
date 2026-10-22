"""
Status, reconciliation and explicit resolution of tracked trade requests.
"""

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import MetaTrader5 as mt5

from ..requests_registry import (
    STATE_ACCEPTED,
    STATE_FILLED,
    STATE_PARTIAL,
    STATE_RECONCILED,
    STATE_UNKNOWN,
    UNRESOLVED_STATES,
    get_request_registry,
)
from ..timebase import iso_utc

ORDER_STATE_NAMES = {0: "started", 1: "placed", 2: "canceled", 3: "partial", 4: "filled", 5: "rejected", 6: "expired"}
NOT_EXECUTED_ORDER_STATES = (2, 5, 6)
DEAL_ENTRY_OUT = 1


def _public(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if record is None:
        return None
    record = dict(record)
    for key in ("created_at", "updated_at"):
        record[f"{key}_iso"] = iso_utc(record.get(key))
    return record


def get_request_status(connection, request_id: str, *, include_events: bool = True) -> Dict[str, Any]:
    """
    Recorded state of a request_id (see ``metatrader_client.requests_registry``).

    Returns:
        dict: {"error", "message", "data": {"record", "events"}}.
    """
    registry = get_request_registry(connection)
    record = registry.get(request_id)
    if record is None:
        return {"error": True, "message": f"Unknown request_id {request_id}", "data": None}
    data = {"record": _public(record)}
    if include_events:
        data["events"] = [{**e, "time_iso": iso_utc(e["time"])} for e in registry.events(request_id)]
    blocking = record["state"] in UNRESOLVED_STATES
    message = f"Request {request_id} is {record['state']}"
    if blocking:
        message += "; its outcome is unknown: reconcile_request or resolve_request before opening new exposure on the symbol"
    return {"error": False, "message": message, "data": data}


def list_requests(connection, *, state: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    """Most recent tracked requests, optionally filtered by state."""
    registry = get_request_registry(connection)
    records = registry.list(states=[state] if state else None, limit=limit)
    return {"error": False, "message": f"{len(records)} requests", "data": [_public(r) for r in records]}


def _matches(item, *, tag: str, order_ticket: Optional[int], attributes) -> bool:
    """True when the comment carries the request tag or one of ``attributes`` equals the order ticket."""
    comment = str(getattr(item, "comment", "") or "")
    if tag and tag in comment:
        return True
    if not order_ticket:
        return False
    return any(getattr(item, name, None) == order_ticket for name in attributes)


def reconcile_request(connection, request_id: str) -> Dict[str, Any]:
    """
    Look for the effects of a request in the terminal and update its state.

    Evidence is searched by the order ticket stored for the request and by the
    request tag in order comments: open orders, open positions, history
    orders and history deals around the request time. An uncertain request
    (``sending``/``unknown``) becomes ``reconciled`` with an ``outcome`` when
    evidence is found; when nothing is found it stays ``unknown`` (a broker
    may replace comments), and only an explicit ``resolve_request`` can
    release it. Accepted or partially filled requests are refreshed.

    Returns:
        dict: {"error", "message", "data": {"record", "outcome", "evidence", "complete"}}.
    """
    registry = get_request_registry(connection)
    record = registry.get(request_id)
    if record is None:
        return {"error": True, "message": f"Unknown request_id {request_id}", "data": None}
    state = record["state"]
    if state not in (*UNRESOLVED_STATES, STATE_ACCEPTED, STATE_PARTIAL):
        return {
            "error": False,
            "message": f"Request {request_id} is {state}; nothing to reconcile",
            "data": {"record": _public(record), "outcome": record.get("outcome"), "evidence": None, "complete": True},
        }

    tag, order_ticket = record["tag"], record.get("order_ticket")
    start = datetime.fromtimestamp(record["created_at"] - 86400, tz=timezone.utc)
    end = datetime.fromtimestamp(time.time() + 86400, tz=timezone.utc)
    failures: List[str] = []

    def read(name, function, attributes):
        items = function()
        if items is None:
            code, description = mt5.last_error()
            failures.append(f"{name} failed (error {code}: {description})")
            return []
        return [i for i in items if _matches(i, tag=tag, order_ticket=order_ticket, attributes=attributes)]

    # Orders are matched by ticket, positions by the identifier of their opening order, deals by order.
    open_orders = read("orders_get", lambda: mt5.orders_get(), ("ticket",))
    positions = read("positions_get", lambda: mt5.positions_get(), ("identifier",))
    history_orders = read("history_orders_get", lambda: mt5.history_orders_get(start, end), ("ticket",))
    deals = read("history_deals_get", lambda: mt5.history_deals_get(start, end), ("order",))

    requested = float((record.get("params") or {}).get("volume") or 0)
    kind = record["kind"]
    executing = [d for d in deals if kind == "close" or int(getattr(d, "entry", 0)) != DEAL_ENTRY_OUT]
    filled = round(sum(float(d.volume) for d in executing), 8)
    evidence = {
        "open_orders": [o.ticket for o in open_orders],
        "positions": [p.ticket for p in positions],
        "history_orders": [{"ticket": o.ticket, "state": ORDER_STATE_NAMES.get(getattr(o, "state", None))} for o in history_orders],
        "deals": [{"ticket": d.ticket, "order": getattr(d, "order", None), "volume": d.volume, "position_id": getattr(d, "position_id", None)} for d in executing],
        "filled_volume": filled,
        "requested_volume": requested,
        "search": {"tag": tag, "order_ticket": order_ticket, "from": start.isoformat(), "to": end.isoformat()},
        "failures": failures,
    }
    complete = not failures

    if filled > 0:
        outcome = STATE_FILLED if filled >= requested - 1e-9 else STATE_PARTIAL
    elif open_orders:
        outcome = STATE_ACCEPTED
    elif history_orders and all(getattr(o, "state", None) in NOT_EXECUTED_ORDER_STATES for o in history_orders):
        outcome = "not_executed"
    else:
        outcome = None

    position_ticket = None
    if executing:
        position_ticket = getattr(executing[0], "position_id", None)
    ticket = order_ticket or (history_orders[0].ticket if history_orders else (open_orders[0].ticket if open_orders else None))

    if outcome is None:
        if state in UNRESOLVED_STATES and complete:
            registry.mark(request_id, STATE_UNKNOWN, detail="reconcile: no evidence found (comments may have been replaced)")
        message = (
            f"No evidence of request {request_id} was found"
            + ("" if complete else f" and some listings failed: {'; '.join(failures)}")
            + ". It stays unknown: check the account and use resolve_request with a reason if nothing was executed."
        )
        return {"error": True, "message": message, "data": {"record": _public(registry.get(request_id)), "outcome": None, "evidence": evidence, "complete": complete}}

    fields = {"outcome": outcome}
    if ticket:
        fields["order_ticket"] = int(ticket)
    if position_ticket:
        fields["position_ticket"] = int(position_ticket)
    if state in UNRESOLVED_STATES or outcome == "not_executed":
        new_state = STATE_RECONCILED
    else:
        new_state = outcome
    registry.mark(request_id, new_state, detail=f"reconcile: {outcome} ({filled} of {requested} lots)", **fields)
    return {
        "error": False,
        "message": f"Request {request_id} reconciled: {outcome}" + ("" if complete else f" (incomplete search: {'; '.join(failures)})"),
        "data": {"record": _public(registry.get(request_id)), "outcome": outcome, "evidence": evidence, "complete": complete},
    }


def resolve_request(connection, request_id: str, *, decision: str, reason: str) -> Dict[str, Any]:
    """
    Explicit, audited decision for a request whose outcome stays unknown.

    ``decision``: ``not_executed`` (verified that nothing was executed) or
    ``accept_unknown`` (accept the uncertainty to unblock the symbol). Never
    sends or cancels anything.
    """
    return get_request_registry(connection).resolve(request_id, decision=decision, reason=reason)
