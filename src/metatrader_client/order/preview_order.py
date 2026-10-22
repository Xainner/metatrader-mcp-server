"""
Pre-trade check of an opening order without sending it.
"""

import time
from typing import Any, Dict, Optional, Union

import MetaTrader5 as mt5

from ..risk import get_risk_guard
from ..strategy import resolve_magic, validate_strategy_id
from ..timebase import iso_utc
from ..types import OrderType, TradeRequestActions
from ..serialization import to_plain
from ._build import (
    PENDING_TYPES,
    build_opening_request,
    floor_volume,
    load_symbol,
    normalize_levels,
    opening_params,
    pending_type_for,
    side_of,
    to_code,
)
from ._preview_store import get_preview_store
from ._validation import _point
from ..requests_registry import fingerprint


def _fail(message: str) -> Dict[str, Any]:
    return {"error": True, "message": message, "data": None}


def _calc_profit(side: int, symbol: str, volume: float, price_open: float, price_close: float) -> Optional[float]:
    value = mt5.order_calc_profit(side, symbol, volume, price_open, price_close)
    return None if value is None else float(value)


def _size_for_risk(symbol_info, *, side: int, symbol: str, entry: float, stop_loss: Optional[float], risk_amount: float):
    """Largest valid volume whose loss at the stop loss does not exceed ``risk_amount``. Returns (volume, error)."""
    if not stop_loss:
        return None, "risk_amount needs a stop_loss to size the volume"
    if risk_amount <= 0:
        return None, "risk_amount must be greater than 0"
    loss_per_lot = _calc_profit(side, symbol, 1.0, entry, stop_loss)
    if loss_per_lot is None:
        return None, f"Cannot calculate the loss at the stop loss for {symbol}; the volume cannot be sized"
    loss_per_lot = abs(min(loss_per_lot, 0.0))
    if loss_per_lot <= 0:
        return None, "The stop loss does not produce a loss from the entry price; check its side"
    vmax = float(getattr(symbol_info, "volume_max", 0) or 0)
    raw = risk_amount / loss_per_lot
    if vmax > 0:
        raw = min(raw, vmax)
    volume = floor_volume(symbol_info, raw)
    step = float(getattr(symbol_info, "volume_step", 0) or 0)
    vmin = float(getattr(symbol_info, "volume_min", 0) or 0)
    # The loss is recomputed for the rounded volume; never round the risk up.
    while volume > 0:
        loss = _calc_profit(side, symbol, volume, entry, stop_loss)
        if loss is None:
            return None, f"Cannot calculate the loss at the stop loss for {symbol}"
        if abs(min(loss, 0.0)) <= risk_amount + 1e-9:
            break
        volume = floor_volume(symbol_info, volume - (step or volume))
    if volume <= 0 or (vmin > 0 and volume < vmin - 1e-9):
        min_loss = _calc_profit(side, symbol, vmin, entry, stop_loss) if vmin > 0 else None
        detail = f" (the minimum volume {vmin} would lose {abs(min_loss):.2f})" if min_loss is not None else ""
        return None, f"risk_amount {risk_amount} is too small for the minimum volume of {symbol}{detail}"
    return volume, None


def preview_order(
    connection,
    *,
    symbol: str,
    type: str,
    volume: Optional[Union[float, int]] = None,
    risk_amount: Optional[float] = None,
    price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    strategy_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Normalize and check an opening order without sending it.

    The order is built exactly as ``place_market_order`` / ``place_pending_order``
    would build it, then evaluated with ``order_calc_profit``,
    ``order_calc_margin``, the configured risk limits and ``order_check``.

    Args:
        connection: MetaTrader 5 connection object.
        symbol: Broker symbol.
        type: BUY or SELL (market, or pending when ``price`` is given: LIMIT/STOP
            is chosen from the current quote), or an explicit pending type.
        volume: Volume in lots. Either ``volume`` or ``risk_amount`` is required.
        risk_amount: Maximum loss at the stop loss in the account currency; the
            volume is the largest valid step whose loss does not exceed it.
        price: Pending order price (omit for a market order).
        stop_loss: Stop loss price.
        take_profit: Take profit price.
        strategy_id: Strategy owning the order (magic number and its limits).

    Returns:
        dict: ``data`` holds the normalized ``request``, ``entry_price``,
        ``volume``, ``loss_at_stop``, ``profit_at_target``, ``reward_risk``,
        ``margin``, ``exposure``, ``risk_check``, ``order_check``, known and
        unknown costs, ``preview_id`` and ``expires_at``. ``ready`` is True only
        when every check passed. A preview does not reserve a price: send the
        order with ``preview_id`` before it expires; it is checked again then.
    """
    started = time.time()
    error = validate_strategy_id(strategy_id)
    if error:
        return _fail(error)
    if (volume is None) == (risk_amount is None):
        return _fail("Pass exactly one of volume or risk_amount")
    magic = resolve_magic(strategy_id)

    requested = str(type).upper()
    symbol_info, tick, error = load_symbol(mt5, symbol)
    if error:
        return _fail(error)

    if price is None:
        if requested not in ("BUY", "SELL"):
            return _fail("A market preview needs type BUY or SELL; pass price for a pending order")
        action, order_type = TradeRequestActions.DEAL.value, to_code(OrderType, requested)
        entry = tick.ask if requested == "BUY" else tick.bid
    else:
        action = TradeRequestActions.PENDING.value
        if requested in ("BUY", "SELL"):
            requested = pending_type_for(requested, float(price), tick.bid, tick.ask)
        order_type = to_code(OrderType, requested)
        if order_type not in PENDING_TYPES:
            return _fail("Invalid pending order type")
        entry = float(price)

    side = side_of(order_type)
    sized = None
    if risk_amount is not None:
        stop_level, _, error = normalize_levels(symbol_info, stop_loss, None)
        if error:
            return _fail(error)
        volume, error = _size_for_risk(symbol_info, side=side, symbol=symbol, entry=entry, stop_loss=stop_level, risk_amount=float(risk_amount))
        if error:
            return _fail(error)
        sized = {"risk_amount": float(risk_amount), "method": "largest volume step whose loss at the stop loss does not exceed risk_amount"}

    request, info, error = build_opening_request(
        mt5, action=action, symbol=symbol, volume=volume, order_type=order_type, price=price,
        stop_loss=stop_loss, take_profit=take_profit, magic=magic, comment="MCP preview",
    )
    if error:
        return _fail(error)
    entry = request["price"]
    account = mt5.account_info()
    currency = getattr(account, "currency", None) if account is not None else None
    problems = []

    loss = profit = None
    if request["sl"]:
        value = _calc_profit(side, symbol, request["volume"], entry, request["sl"])
        if value is None:
            problems.append("loss at the stop loss could not be calculated")
        else:
            loss = abs(min(value, 0.0))
    if request["tp"]:
        value = _calc_profit(side, symbol, request["volume"], entry, request["tp"])
        if value is None:
            problems.append("profit at the take profit could not be calculated")
        else:
            profit = max(value, 0.0)
    reward_risk = round(profit / loss, 3) if (profit is not None and loss) else None
    if sized and loss is not None and loss > sized["risk_amount"] + 1e-9:
        return _fail("Internal sizing check failed: the final volume would exceed risk_amount")

    margin = mt5.order_calc_margin(side, symbol, request["volume"], entry)
    if margin is None:
        problems.append("margin could not be calculated")

    spread_cost = _calc_profit(OrderType.BUY.value, symbol, request["volume"], tick.ask, tick.bid)

    positions = mt5.positions_get(symbol=symbol)
    orders = mt5.orders_get(symbol=symbol)
    if positions is None or orders is None:
        exposure = {"status": "unknown", "error": "positions or orders could not be read"}
        problems.append("current exposure could not be read")
    else:
        signed = sum((p.volume if p.type == 0 else -p.volume) for p in positions)
        new_signed = request["volume"] if side == 0 else -request["volume"]
        exposure = {
            "status": "ok",
            "symbol_positions_volume": round(sum(p.volume for p in positions), 8),
            "symbol_pending_volume": round(sum(o.volume_current for o in orders), 8),
            "symbol_net_volume_before": round(signed, 8),
            "symbol_net_volume_after": round(signed + new_signed, 8),
            "note": "net volume adds buys and subtracts sells of this symbol; it is not a risk measure",
        }

    guard = get_risk_guard(connection)
    risk_error = guard.check_account(mt5) or guard.check_new_order(
        mt5, symbol=symbol, side=side, volume=request["volume"], entry_price=entry,
        stop_loss=info["stop_loss"], magic=magic, strategy_id=strategy_id,
    )

    check = mt5.order_check(request)
    if check is None:
        code, description = mt5.last_error()
        order_check = {"status": "unknown", "error": f"order_check returned no result (error {code}: {description})"}
        problems.append("order_check returned no result")
    else:
        retcode = getattr(check, "retcode", None)
        order_check = {
            "status": "ok" if retcode == 0 else "rejected",
            "retcode": retcode,
            "comment": getattr(check, "comment", None),
            "margin": getattr(check, "margin", None),
            "margin_free": getattr(check, "margin_free", None),
            "margin_level": getattr(check, "margin_level", None),
            "balance": getattr(check, "balance", None),
            "equity": getattr(check, "equity", None),
        }
        if retcode != 0:
            problems.append(f"order_check rejected the request: {order_check['comment']} ({retcode})")

    ready = not problems and risk_error is None
    saved = None
    if ready:
        saved = get_preview_store(connection).save(
            fingerprint=fingerprint(opening_params(request, strategy_id)),
            entry_price=entry,
            point=_point(symbol_info),
            market=action == TradeRequestActions.DEAL.value,
            deviation=request["deviation"],
        )

    tool = "place_market_order" if action == TradeRequestActions.DEAL.value else "place_pending_order"
    arguments = {
        "symbol": symbol,
        "volume": request["volume"],
        "type": "BUY" if side == 0 else "SELL",
        "stop_loss": request["sl"] or None,
        "take_profit": request["tp"] or None,
        "strategy_id": strategy_id,
    }
    if tool == "place_pending_order":
        arguments["price"] = request["price"]
    if saved:
        arguments["preview_id"] = saved["preview_id"]

    data = {
        "ready": ready,
        "problems": problems,
        "request": to_plain({k: v for k, v in request.items() if k != "comment"}),
        "order_type": OrderType.to_string(order_type),
        "entry_price": entry,
        "quote": {"bid": tick.bid, "ask": tick.ask, "server_time": getattr(tick, "time", None)},
        "volume": request["volume"],
        "sizing": sized,
        "currency": currency,
        "loss_at_stop": loss,
        "profit_at_target": profit,
        "reward_risk": reward_risk,
        "margin": None if margin is None else float(margin),
        "exposure": exposure,
        "risk_check": {"allowed": risk_error is None, "reason": risk_error},
        "order_check": order_check,
        "costs": {
            "known": {"spread_round_trip": None if spread_cost is None else abs(min(spread_cost, 0.0))},
            "unknown": ["commission", "swap", "slippage"],
            "currency": currency,
        },
        "estimate_note": "Estimates in the account currency from order_calc_profit/order_calc_margin; the executed result can differ.",
        "preview_id": saved["preview_id"] if saved else None,
        "expires_at": iso_utc(saved["expires_at"]) if saved else None,
        "max_price_move_points": saved["max_price_move_points"] if saved else None,
        "send_with": {"tool": tool, "arguments": arguments} if saved else None,
        "duration_ms": round((time.time() - started) * 1000, 3),
    }
    message = "Preview ready" if ready else "Preview found problems: " + "; ".join(problems + ([risk_error] if risk_error else []))
    return {"error": not ready, "message": message, "data": data}
