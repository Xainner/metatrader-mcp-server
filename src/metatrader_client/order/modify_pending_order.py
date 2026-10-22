import MetaTrader5 as mt5
from typing import Optional, Union
from ..risk import get_risk_guard
from ..types import TradeRequestActions, OrderType
from .send_order import send_order
from ._confirm import confirm_levels
from ._ownership import ownership_error
from ._result import to_client_response
from ._validation import is_buy, normalize_price, check_stops, check_pending_price

def modify_pending_order(
    connection,
    *,
    id: Union[int, str],
    price: Optional[Union[int, float]] = None,
    stop_loss: Optional[Union[int, float]] = None,
    take_profit: Optional[Union[int, float]] = None,
    strategy_id: Optional[str] = None,
):
    """
    Modify the price, stop loss or take profit of a pending order.

    Parameters left as None keep their current value, including the order
    lifetime and expiration. Pass 0 for ``stop_loss`` or ``take_profit`` to
    remove that level explicitly.

    Args:
        connection: MetaTrader 5 connection object.
        id: Pending order ticket.
        price: New order price (None keeps the current price).
        stop_loss: New stop loss (None keeps the current one, 0 removes it).
        take_profit: New take profit (None keeps the current one, 0 removes it).
        strategy_id: When given, the order must belong to this strategy (same magic number).

    Returns:
        dict: ``error`` flag, ``message``, ``data``, ``status``, ``retcode`` and,
        after an accepted request, ``verification`` (``confirmed``, ``mismatch``
        or ``unknown``) and ``checks``; only ``confirmed`` returns ``error: False``.
    """

    try:
        order_id = int(id)
    except (TypeError, ValueError):
        return {
            "error": True,
            "message": f"Invalid order ID {id}",
            "data": None,
        }

    orders = mt5.orders_get(ticket=order_id)
    if not orders:
        return {
            "error": True,
            "message": f"Invalid order ID {id}",
            "data": None,
        }
    order = orders[0]
    error = ownership_error(order, strategy_id, label=f"Order {order_id}")
    if error:
        return { "error": True, "message": error, "data": None }

    symbol_info = mt5.symbol_info(order.symbol)
    if symbol_info is None:
        return { "error": True, "message": f"Failed to get symbol info for {order.symbol}", "data": None }

    new_price = order.price_open
    if price is not None:
        new_price, error = normalize_price(symbol_info, price)
        if error:
            return { "error": True, "message": error, "data": None }
        tick = mt5.symbol_info_tick(order.symbol)
        if tick is None:
            return { "error": True, "message": f"No current quote for {order.symbol}", "data": None }
        error = check_pending_price(symbol_info, order.type, price=new_price, bid=tick.bid, ask=tick.ask)
        if error:
            return { "error": True, "message": error, "data": None }

    levels = {}
    for key, label, requested, current in (
        ("sl", "Stop loss", stop_loss, order.sl),
        ("tp", "Take profit", take_profit, order.tp),
    ):
        if requested is None:
            levels[key] = current
        elif float(requested) == 0:
            levels[key] = 0.0
        else:
            levels[key], error = normalize_price(symbol_info, requested, label)
            if error:
                return { "error": True, "message": error, "data": None }

    # Levels are checked against the (possibly new) order price.
    error = check_stops(
        symbol_info,
        order.type,
        reference_price=new_price,
        stop_loss=levels["sl"],
        take_profit=levels["tp"],
    )
    if error:
        return { "error": True, "message": error, "data": None }

    if stop_loss is not None or price is not None:
        side = OrderType.BUY.value if is_buy(order.type) else OrderType.SELL.value
        error = get_risk_guard(connection).check_stop_loss_risk(
            mt5,
            symbol=order.symbol,
            side=side,
            volume=order.volume_current,
            entry_price=new_price,
            stop_loss=levels["sl"],
        )
        if error:
            return { "error": True, "message": error, "data": None }

    response = send_order(
        connection,
        action=TradeRequestActions.MODIFY,
        order=order_id,
        symbol=order.symbol,
        price=new_price,
        stop_loss=levels["sl"],
        take_profit=levels["tp"],
        type_time=order.type_time,
        expiration=order.time_expiration or None,
        stoplimit=order.price_stoplimit or None,
    )

    result = to_client_response(
        response,
        f"Modified pending order {order_id}: price {new_price}, SL {levels['sl']}, TP {levels['tp']} ({response['retcode_name']})",
    )
    if not response["success"]:
        result["verification"] = "unknown" if response["status"] == "no_response" else "not_applied"
        return result
    result.update(confirm_levels(
        mt5, mt5.orders_get(ticket=order_id), symbol_info=symbol_info,
        expected={"price": new_price, "sl": levels["sl"], "tp": levels["tp"]}, label="order_listed",
    ))
    if result["verification"] != "confirmed":
        result["error"] = True
        result["message"] += (
            f"; the final values could not be confirmed ({result['verification']}, see checks). "
            "Read the order before sending another modification."
        )
    return result
