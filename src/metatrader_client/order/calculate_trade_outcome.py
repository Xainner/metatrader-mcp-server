from typing import Any, Dict, Optional, Union

import MetaTrader5 as mt5

from ..types import OrderType
from ._build import side_of, to_code


def _account_currency() -> Optional[str]:
    account = mt5.account_info()
    return getattr(account, "currency", None) if account is not None else None


def calculate_trade_outcome(
    connection,
    *,
    symbol: str,
    type: Union[str, int],
    volume: float,
    entry_price: float,
    exit_price: float,
) -> Dict[str, Any]:
    """
    Estimate the profit or loss of a trade in the account currency.

    Uses ``mt5.order_calc_profit`` with the broker's own symbol settings and
    conversion rates. It is an estimate: commissions, swaps, fees and
    slippage are not included, and conversion rates change.

    Args:
        connection: MetaTrader 5 connection object.
        symbol: Broker symbol.
        type: BUY or SELL (pending types use their direction).
        volume: Volume in lots.
        entry_price: Open price.
        exit_price: Close price.

    Returns:
        dict: {"error", "message", "data": {"profit", "currency", "scope", "method", "excludes", ...}}
    """
    order_type = to_code(OrderType, type)
    if order_type is None:
        return {"error": True, "message": f"Invalid order type {type!r}", "data": None}
    side = side_of(order_type)
    try:
        volume, entry_price, exit_price = float(volume), float(entry_price), float(exit_price)
    except (TypeError, ValueError):
        return {"error": True, "message": "volume, entry_price and exit_price must be numbers", "data": None}
    mt5.symbol_select(symbol, True)
    profit = mt5.order_calc_profit(side, symbol, volume, entry_price, exit_price)
    if profit is None:
        code, description = mt5.last_error()
        return {"error": True, "message": f"Cannot calculate the profit for {symbol} (error {code}: {description})", "data": None}
    currency = _account_currency()
    return {
        "error": False,
        "message": f"Estimated result {profit} {currency or '(currency unknown)'}",
        "data": {
            "symbol": symbol,
            "side": "BUY" if side == 0 else "SELL",
            "volume": volume,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "profit": float(profit),
            "currency": currency,
            "scope": "single order (estimate)",
            "method": "mt5.order_calc_profit",
            "excludes": ["commission", "swap", "fees", "slippage"],
        },
    }
