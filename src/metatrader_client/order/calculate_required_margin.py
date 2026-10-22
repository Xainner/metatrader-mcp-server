from typing import Any, Dict, Optional, Union

import MetaTrader5 as mt5

from ..types import OrderType
from ._build import side_of, to_code


def calculate_required_margin(
    connection,
    *,
    symbol: str,
    type: Union[str, int],
    volume: float,
    price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Estimate the margin needed to open a position, in the account currency.

    Uses ``mt5.order_calc_margin`` with the broker's symbol and account
    settings. When ``price`` is omitted the current ask (BUY) or bid (SELL)
    is used. The margin the server finally takes may differ (hedged volume,
    rate changes, margin calls).

    Args:
        connection: MetaTrader 5 connection object.
        symbol: Broker symbol.
        type: BUY or SELL (pending types use their direction).
        volume: Volume in lots.
        price: Open price (default: current quote).

    Returns:
        dict: {"error", "message", "data": {"margin", "currency", "price", "free_margin", ...}}
    """
    order_type = to_code(OrderType, type)
    if order_type is None:
        return {"error": True, "message": f"Invalid order type {type!r}", "data": None}
    side = side_of(order_type)
    mt5.symbol_select(symbol, True)
    if price is None:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None or not tick.bid or not tick.ask:
            return {"error": True, "message": f"No current quote for {symbol}", "data": None}
        price = tick.ask if side == 0 else tick.bid
    margin = mt5.order_calc_margin(side, symbol, float(volume), float(price))
    if margin is None:
        code, description = mt5.last_error()
        return {"error": True, "message": f"Cannot calculate the margin for {symbol} (error {code}: {description})", "data": None}
    account = mt5.account_info()
    free = getattr(account, "margin_free", None) if account is not None else None
    currency = getattr(account, "currency", None) if account is not None else None
    return {
        "error": False,
        "message": f"Estimated margin {margin} {currency or '(currency unknown)'}",
        "data": {
            "symbol": symbol,
            "side": "BUY" if side == 0 else "SELL",
            "volume": float(volume),
            "price": float(price),
            "margin": float(margin),
            "currency": currency,
            "free_margin": free,
            "free_margin_after": None if free is None else round(float(free) - float(margin), 2),
            "method": "mt5.order_calc_margin",
            "scope": "single order (estimate)",
        },
    }
