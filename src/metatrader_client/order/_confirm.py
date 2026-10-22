"""
Verification of trading results against the terminal state.

After ``order_send`` the terminal is queried again to check what actually
happened. Results carry:

- ``verification``: one word summarizing the check (see each function).
- ``checks``: ``checked_at`` (UTC), the facts that were ``verified``, the ones
  that remain ``unknown`` and the observed ``details``.
- ``execution`` (openings): requested and filled volume, requested and
  executed price, slippage in points (positive = against the order),
  known costs from the deal and the costs that are not known, and timings.
- ``protection`` (openings): requested and effective SL/TP and a status:
  ``not_requested``, ``protected``, ``missing``, ``mismatch`` or ``unknown``.

A listing that is not published yet is reported as unknown, never as absent.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List

from ..metrics import metrics, timed
from ._validation import _point, _tick_size, is_buy

EPS = 1e-9


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def level_matches(symbol_info, expected: float, actual: float) -> bool:
    expected, actual = float(expected or 0), float(actual or 0)
    if expected == 0 or actual == 0:
        return expected == actual
    tolerance = _tick_size(symbol_info) / 2 if symbol_info is not None else 1e-9
    return abs(expected - actual) <= tolerance + EPS


class Checks:
    """Collects what was checked, what was found and what is still unknown."""

    def __init__(self):
        self.verified: List[str] = []
        self.unknown: List[str] = []
        self.details: Dict[str, Any] = {}

    def ok(self, name: str, **details) -> None:
        self.verified.append(name)
        self.details.update(details)

    def missing(self, name: str, reason: str) -> None:
        self.unknown.append(name)
        self.details[f"{name}_reason"] = reason

    def as_dict(self) -> Dict[str, Any]:
        return {"checked_at": now_iso(), "verified": self.verified, "unknown": self.unknown, "details": self.details}


def _error_text(mt5) -> str:
    code, description = mt5.last_error()
    return f"error {code}: {description}"


def protection_status(symbol_info, *, requested_sl: float, requested_tp: float, effective_sl, effective_tp) -> str:
    requested = [(requested_sl, effective_sl), (requested_tp, effective_tp)]
    if not requested_sl and not requested_tp:
        return "not_requested"
    if effective_sl is None and effective_tp is None:
        return "unknown"
    for wanted, actual in requested:
        if wanted and not actual:
            return "missing"
    for wanted, actual in requested:
        if wanted and not level_matches(symbol_info, wanted, actual):
            return "mismatch"
    return "protected"


def confirm_market_opening(mt5, response: Dict[str, Any], *, symbol_info) -> Dict[str, Any]:
    """
    Reconcile a market order: deal in the history, position, volume and SL/TP.

    ``verification``: ``confirmed`` (position found with the expected volume
    and protection), ``partial`` (confirmed, but only part of the volume was
    filled), ``pending`` (accepted, not filled yet), ``protection_missing``,
    ``mismatch`` or ``unknown``.
    """
    request = response.get("request") or {}
    data = response.get("data")
    checks = Checks()
    buy = is_buy(request.get("type"))
    requested_sl, requested_tp = float(request.get("sl") or 0), float(request.get("tp") or 0)
    execution: Dict[str, Any] = {
        "requested_volume": request.get("volume"),
        "filled_volume": getattr(data, "volume", None),
        "requested_price": request.get("price"),
        "executed_price": getattr(data, "price", None) or None,
        "slippage_points": None,
        "slippage_basis": "points of the symbol; positive = worse than the requested price",
        "commission": None,
        "fee": None,
        "costs_unknown": ["swap (accrues while open)", "commission on exit"],
        "send_ms": (response.get("timings") or {}).get("send_ms"),
        "confirm_ms": None,
    }
    position = None
    position_id = None

    with timed() as t:
        if response.get("status") == "placed":
            order_ticket = getattr(data, "order", None)
            orders = mt5.orders_get(ticket=order_ticket) if order_ticket else None
            if orders:
                checks.ok("order_listed", order=order_ticket)
            else:
                checks.missing("order_listed", "the accepted order is not listed (yet)" if orders == () else _error_text(mt5))
            verification = "pending"
        else:
            deal_ticket = getattr(data, "deal", None)
            deals = mt5.history_deals_get(ticket=deal_ticket) if deal_ticket else None
            if deals:
                deal = deals[0]
                position_id = getattr(deal, "position_id", None)
                execution["executed_price"] = getattr(deal, "price", None) or execution["executed_price"]
                execution["commission"] = getattr(deal, "commission", None)
                execution["fee"] = getattr(deal, "fee", None)
                checks.ok("deal_in_history", deal=deal_ticket, position_id=position_id)
            else:
                checks.missing("deal_in_history", "the deal is not in the history yet" if deals == () else _error_text(mt5) if deal_ticket else "no deal ticket in the result")

            if position_id:
                positions = mt5.positions_get(ticket=position_id)
                if positions:
                    position = positions[0]
                    checks.ok("position_listed", position_volume=position.volume, position_sl=position.sl, position_tp=position.tp)
                elif positions is None:
                    checks.missing("position_listed", _error_text(mt5))
                else:
                    checks.missing("position_listed", "the position is not listed (not published yet, or already closed)")

            filled = float(getattr(data, "volume", 0) or 0)
            if position is not None:
                # On netting accounts the position may include earlier volume, so only a smaller volume is a mismatch.
                if float(position.volume) + EPS < filled:
                    verification = "mismatch"
                else:
                    checks.ok("volume", filled_volume=filled)
                    verification = "partial" if response.get("status") == "partial" else "confirmed"
            else:
                verification = "unknown"

    status = protection_status(
        symbol_info,
        requested_sl=requested_sl,
        requested_tp=requested_tp,
        effective_sl=None if position is None else position.sl,
        effective_tp=None if position is None else position.tp,
    )
    if position is not None and status in ("protected", "not_requested"):
        checks.ok("protection")
    elif status != "not_requested":
        checks.missing("protection", status)
    if position is not None and status in ("missing", "mismatch") and verification in ("confirmed", "partial"):
        verification = "protection_missing" if status == "missing" else "mismatch"

    executed = execution["executed_price"]
    requested_price = execution["requested_price"]
    if executed and requested_price:
        diff = (float(executed) - float(requested_price)) / _point(symbol_info)
        execution["slippage_points"] = round(diff if buy else -diff, 2)
    execution["confirm_ms"] = round(t.ms, 3)
    metrics.record("confirm", t.ms, ok=verification not in ("unknown",))

    return {
        "verification": verification,
        "checks": checks.as_dict(),
        "execution": execution,
        "protection": {
            "requested_sl": requested_sl or None,
            "requested_tp": requested_tp or None,
            "effective_sl": None if position is None else (position.sl or None),
            "effective_tp": None if position is None else (position.tp or None),
            "status": status,
        },
        "position_id": position_id,
        "position": position,
    }


def confirm_pending_opening(mt5, response: Dict[str, Any], *, symbol_info) -> Dict[str, Any]:
    """
    Check that a placed pending order is listed with the requested price, volume and SL/TP.

    ``verification``: ``confirmed``, ``mismatch`` or ``unknown`` (not listed:
    not published yet, already triggered, or the listing failed).
    """
    request = response.get("request") or {}
    data = response.get("data")
    checks = Checks()
    ticket = getattr(data, "order", None)
    with timed() as t:
        orders = mt5.orders_get(ticket=ticket) if ticket else None
    order = orders[0] if orders else None
    if order is None:
        checks.missing("order_listed", _error_text(mt5) if orders is None else "the order is not listed (not published yet, or already triggered)")
        verification = "unknown"
    else:
        checks.ok("order_listed", order=ticket, price=order.price_open, volume=order.volume_current, sl=order.sl, tp=order.tp)
        same = (
            level_matches(symbol_info, request.get("price"), order.price_open)
            and level_matches(symbol_info, request.get("sl"), order.sl)
            and level_matches(symbol_info, request.get("tp"), order.tp)
            and abs(float(order.volume_current) - float(request.get("volume") or 0)) <= EPS
        )
        verification = "confirmed" if same else "mismatch"
    metrics.record("confirm", t.ms, ok=verification != "unknown")
    status = protection_status(
        symbol_info,
        requested_sl=float(request.get("sl") or 0),
        requested_tp=float(request.get("tp") or 0),
        effective_sl=None if order is None else order.sl,
        effective_tp=None if order is None else order.tp,
    )
    return {
        "verification": verification,
        "checks": checks.as_dict(),
        "execution": {"send_ms": (response.get("timings") or {}).get("send_ms"), "confirm_ms": round(t.ms, 3)},
        "protection": {
            "requested_sl": float(request.get("sl") or 0) or None,
            "requested_tp": float(request.get("tp") or 0) or None,
            "effective_sl": None if order is None else (order.sl or None),
            "effective_tp": None if order is None else (order.tp or None),
            "status": status,
        },
    }


def confirm_levels(mt5, items, *, symbol_info, expected: Dict[str, float], label: str) -> Dict[str, Any]:
    """
    Compare the levels of a re-read position or order with the expected ones.

    ``verification``: ``confirmed``, ``mismatch`` or ``unknown``.
    """
    checks = Checks()
    if items is None:
        checks.missing(label, _error_text(mt5))
        return {"verification": "unknown", "checks": checks.as_dict()}
    if not items:
        checks.missing(label, f"the {label.split('_')[0]} is no longer listed")
        return {"verification": "unknown", "checks": checks.as_dict()}
    item = items[0]
    observed = {}
    mismatched = []
    for key, value in expected.items():
        actual = getattr(item, "price_open" if key == "price" else key, None)
        observed[key] = actual
        if not level_matches(symbol_info, value, actual):
            mismatched.append(key)
    checks.ok(label, observed=observed)
    if mismatched:
        checks.missing("levels", f"different from the request: {', '.join(mismatched)}")
        return {"verification": "mismatch", "checks": checks.as_dict()}
    checks.ok("levels")
    return {"verification": "confirmed", "checks": checks.as_dict()}
