"""
Construction of the requests that open exposure.

``send_order`` and ``preview_order`` build their requests here, so the request
checked with ``order_check`` in a preview is the same request that is sent.
"""

import math
from typing import Any, Dict, Optional, Tuple

from ..types import OrderType, OrderTime, OrderFilling, TradeRequestActions
from ._validation import (
    _decimals,
    check_pending_price,
    check_stops,
    is_buy,
    normalize_price,
    select_filling_mode,
    validate_volume,
)

PENDING_TYPES = (
    OrderType.BUY_LIMIT.value,
    OrderType.SELL_LIMIT.value,
    OrderType.BUY_STOP.value,
    OrderType.SELL_STOP.value,
)

COMMENT_MAX = 31


def to_code(enum_cls, value) -> Optional[int]:
    """Convert an enum member, name or numeric code to its numeric code."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value if enum_cls.exists(value) else None
    return enum_cls.validate(value)


def optional_level(value) -> Optional[float]:
    """Convert an optional SL/TP level, where None and 0 mean no level."""
    if value is None:
        return None
    value = float(value)
    return value if value != 0 else None


def load_symbol(mt5, symbol: Optional[str]):
    """Select a symbol and return (symbol_info, tick, error)."""
    if not symbol:
        return None, None, "Parameter `symbol` is required for this operation"
    if not mt5.symbol_select(symbol, True):
        return None, None, f"Invalid symbol or failed to select {symbol}"
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return None, None, f"Failed to get symbol info for {symbol}"
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or not tick.bid or not tick.ask:
        return symbol_info, None, f"No current quote for {symbol}"
    return symbol_info, tick, None


def normalize_levels(symbol_info, stop_loss, take_profit):
    """Normalize optional SL/TP to the symbol precision. Returns (sl, tp, error)."""
    levels = []
    for label, value in (("Stop loss", stop_loss), ("Take profit", take_profit)):
        value = optional_level(value)
        if value is not None:
            value, error = normalize_price(symbol_info, value, label)
            if error:
                return None, None, error
        levels.append(value)
    return levels[0], levels[1], None


def side_of(order_type: int) -> int:
    """Direction (BUY=0 / SELL=1) of any order type."""
    return OrderType.BUY.value if is_buy(order_type) else OrderType.SELL.value


def pending_type_for(side: str, price: float, bid: float, ask: float) -> str:
    """LIMIT or STOP type for a BUY/SELL pending order at ``price``."""
    if side == "BUY":
        return "BUY_LIMIT" if ask > price else "BUY_STOP"
    return "SELL_LIMIT" if bid < price else "SELL_STOP"


def build_comment(comment: Optional[str], tag: Optional[str]) -> str:
    """Order comment carrying the request tag, within the 31 characters MetaTrader 5 keeps."""
    base = (comment or "MCP").strip() or "MCP"
    if not tag:
        return base[:COMMENT_MAX]
    room = COMMENT_MAX - len(tag) - 1
    return f"{base[:room]} {tag}"


def build_opening_request(
    mt5,
    *,
    action: int,
    symbol: Optional[str],
    volume,
    order_type: Optional[int],
    price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    deviation: Optional[int] = 20,
    magic: int = 0,
    comment: str = "MCP",
    expiration=None,
    type_filling=None,
    type_time=None,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], Optional[str]]:
    """
    Validate and build a request that opens exposure (market DEAL or PENDING).

    Returns ``(request, info, error)``. ``info`` holds ``symbol_info``,
    ``tick``, ``side`` and the normalized ``stop_loss`` / ``take_profit``.
    """
    info: Dict[str, Any] = {}
    if action == TradeRequestActions.DEAL.value:
        if order_type not in (OrderType.BUY.value, OrderType.SELL.value):
            return None, info, "Invalid order type, must be BUY or SELL"
    elif action == TradeRequestActions.PENDING.value:
        if order_type not in PENDING_TYPES:
            return None, info, "Invalid order type, must be BUY_LIMIT, SELL_LIMIT, BUY_STOP, or SELL_STOP"
    else:
        return None, info, "Only DEAL and PENDING requests open exposure"

    symbol_info, tick, error = load_symbol(mt5, symbol)
    if error:
        return None, info, error
    info.update(symbol_info=symbol_info, tick=tick, side=side_of(order_type))

    volume, error = validate_volume(symbol_info, volume)
    if error:
        return None, info, error

    pending = action == TradeRequestActions.PENDING.value
    if pending:
        price, error = normalize_price(symbol_info, price)
        if error:
            return None, info, error
        error = check_pending_price(symbol_info, order_type, price=price, bid=tick.bid, ask=tick.ask)
        if error:
            return None, info, error
    elif price:
        price, error = normalize_price(symbol_info, price)
        if error:
            return None, info, error
    else:
        # The quote is read before validating SL/TP, so they are compared with the real price.
        price = tick.ask if order_type == OrderType.BUY.value else tick.bid

    stop_loss, take_profit, error = normalize_levels(symbol_info, stop_loss, take_profit)
    if error:
        return None, info, error
    if pending:
        reference = price
    else:
        # A new position is closed at bid (buy) or ask (sell): stops are measured from that price.
        reference = tick.bid if order_type == OrderType.BUY.value else tick.ask
    error = check_stops(symbol_info, order_type, reference_price=reference, stop_loss=stop_loss, take_profit=take_profit)
    if error:
        return None, info, error
    info.update(stop_loss=stop_loss, take_profit=take_profit)

    filling = to_code(OrderFilling, type_filling)
    if filling is None:
        filling, error = select_filling_mode(symbol_info, pending=pending)
        if error:
            return None, info, error

    if pending:
        lifetime = to_code(OrderTime, type_time)
        if lifetime is None:
            lifetime = OrderTime.SPECIFIED.value if expiration else OrderTime.GTC.value
    else:
        lifetime = OrderTime.GTC.value

    request = {
        "action": action,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": stop_loss or 0.0,
        "tp": take_profit or 0.0,
        "deviation": int(deviation if deviation is not None else 20),
        "magic": int(magic or 0),
        "comment": comment,
        "type_time": lifetime,
        "type_filling": filling,
    }
    if pending and expiration:
        request["expiration"] = expiration
    return request, info, None


def opening_params(request: Dict[str, Any], strategy_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Parameters that identify an opening intention: everything but the market
    price, the comment and the deviation. Used for request_id fingerprints and
    to detect material changes after a preview.
    """
    pending = request["action"] == TradeRequestActions.PENDING.value
    return {
        "action": request["action"],
        "symbol": request["symbol"],
        "type": request["type"],
        "volume": request["volume"],
        "price": request["price"] if pending else None,
        "sl": request["sl"],
        "tp": request["tp"],
        "magic": request["magic"],
        "strategy_id": strategy_id,
        "type_time": request.get("type_time"),
        "expiration": str(request.get("expiration")) if request.get("expiration") else None,
    }


def floor_volume(symbol_info, raw_volume: float) -> float:
    """Largest valid volume step not above ``raw_volume``."""
    step = float(getattr(symbol_info, "volume_step", 0) or 0)
    if step <= 0:
        return raw_volume
    steps = math.floor(raw_volume / step + 1e-9)
    return round(steps * step, _decimals(step))
