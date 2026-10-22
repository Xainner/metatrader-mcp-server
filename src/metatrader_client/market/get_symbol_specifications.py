from typing import Any, Dict

import MetaTrader5 as mt5

from ..order._validation import min_stop_distance, select_filling_mode

TRADE_MODES = {0: "DISABLED", 1: "LONG_ONLY", 2: "SHORT_ONLY", 3: "CLOSE_ONLY", 4: "FULL"}
EXECUTION_MODES = {0: "REQUEST", 1: "INSTANT", 2: "MARKET", 3: "EXCHANGE"}
FILLING_FLAGS = {1: "FOK", 2: "IOC", 4: "BOC"}
ORDER_FLAGS = {1: "MARKET", 2: "LIMIT", 4: "STOP", 8: "STOP_LIMIT", 16: "SL", 32: "TP", 64: "CLOSE_BY"}
EXPIRATION_FLAGS = {1: "GTC", 2: "DAY", 4: "SPECIFIED", 8: "SPECIFIED_DAY"}
FILLING_NAMES = {0: "FOK", 1: "IOC", 2: "RETURN"}

FIELDS = (
    "name", "description", "path", "currency_base", "currency_profit", "currency_margin",
    "digits", "point", "trade_tick_size", "trade_tick_value", "trade_tick_value_profit",
    "trade_tick_value_loss", "trade_contract_size", "volume_min", "volume_max", "volume_step",
    "volume_limit", "trade_stops_level", "trade_freeze_level", "spread", "spread_float",
    "swap_mode", "swap_long", "swap_short", "margin_initial", "margin_maintenance",
    "trade_mode", "trade_exemode", "filling_mode", "order_mode", "expiration_mode", "visible", "select",
)


def _flags(mask, names) -> list:
    mask = int(mask or 0)
    return [name for bit, name in names.items() if mask & bit]


def get_symbol_specifications(connection, symbol_name: str) -> Dict[str, Any]:
    """
    Trading specification of a broker symbol, as reported by the terminal.

    The values belong to this broker symbol: ``XAUUSDm`` may differ from
    ``XAUUSD`` on another server, so nothing is assumed from generic contract
    specifications. Attributes the terminal does not report are ``None`` and
    listed in ``unavailable``.

    Args:
        connection: MetaTrader 5 connection object.
        symbol_name: Broker symbol (e.g. "EURUSD", "XAUUSDm").

    Returns:
        dict: {"error": bool, "message": str, "data": {...}} with the raw
        attributes, decoded modes and derived values (minimal stop distance in
        price units, default filling policies for market and pending orders).
    """
    if not symbol_name:
        return {"error": True, "message": "symbol_name is required", "data": None}
    mt5.symbol_select(symbol_name, True)
    info = mt5.symbol_info(symbol_name)
    if info is None:
        code, description = mt5.last_error()
        return {"error": True, "message": f"Cannot read symbol {symbol_name} (error {code}: {description})", "data": None}

    data = {name: getattr(info, name, None) for name in FIELDS}
    unavailable = [name for name, value in data.items() if value is None]
    market_filling, market_error = select_filling_mode(info, pending=False)
    pending_filling, _ = select_filling_mode(info, pending=True)
    data.update({
        "trade_mode_name": TRADE_MODES.get(data["trade_mode"]),
        "execution_mode_name": EXECUTION_MODES.get(data["trade_exemode"]),
        "filling_modes": _flags(data["filling_mode"], FILLING_FLAGS),
        "order_modes": _flags(data["order_mode"], ORDER_FLAGS),
        "expiration_modes": _flags(data["expiration_mode"], EXPIRATION_FLAGS),
        "min_stop_distance_price": min_stop_distance(info),
        "freeze_distance_price": float(data["trade_freeze_level"] or 0) * float(data["point"] or 0) if data["point"] else None,
        "default_filling_market": FILLING_NAMES.get(market_filling) if market_filling is not None else None,
        "default_filling_market_error": market_error,
        "default_filling_pending": FILLING_NAMES.get(pending_filling),
        "trading_allowed": data["trade_mode"] not in (None, 0),
        "unavailable": unavailable,
        "source": "mt5.symbol_info",
    })
    return {"error": False, "message": f"Specifications of {symbol_name}", "data": data}
