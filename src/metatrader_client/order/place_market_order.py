import MetaTrader5 as mt5
from typing import Union, Optional
from ..requests_registry import get_request_registry
from ..risk import get_risk_guard
from ..types import TradeRequestActions
from .send_order import send_order
from ._confirm import confirm_market_opening, confirm_levels
from ._result import to_client_response, STATUS_PARTIAL, STATUS_PLACED

def place_market_order(
    connection,
    *,
    type: str,
    symbol: str,
    volume: Union[float, int],
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    request_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    preview_id: Optional[str] = None,
):
    """
    Places a market order for a specified financial instrument.

    This function sends a market order request to the MetaTrader 5 platform. It supports
    BUY and SELL order types, then reconciles the result with the terminal: the
    deal in the history, the position, its volume and its effective SL/TP.

    Args:
        connection: The MetaTrader 5 connection object.
        type: The type of market order, either "BUY" or "SELL".
        symbol: The trading instrument symbol (e.g., "EURUSD").
        volume: The volume of the trade operation in lots.
        stop_loss: Optional stop loss price. None or 0 means no stop loss.
        take_profit: Optional take profit price. None or 0 means no take profit.
        request_id: Stable ID of this intention; repeating it never sends a second order.
        strategy_id: Strategy owning the order (magic number and strategy limits).
        preview_id: ID returned by ``preview_order``; the order must match it.

    Returns:
        dict: ``error`` flag (False when the broker executed or accepted the
              order), ``message``, ``data`` (order_send result), ``status``
              ('done', 'partial', 'placed', 'rejected', 'no_response', ...),
              ``retcode``, ``request_id``, ``request_state`` and, when the
              request was sent, ``verification``, ``checks``, ``execution``,
              ``protection`` and ``position_id`` (see ``order._confirm``).
    """

    type = str(type).upper()

    if type not in ["BUY", "SELL"]:
        return { "error": True, "message": f"Invalid type, should be BUY or SELL.", "data": None }

    response = send_order(
        connection,
        action=TradeRequestActions.DEAL,
        order_type=type,
        symbol=symbol,
        volume=volume,
        stop_loss=stop_loss,
        take_profit=take_profit,
        request_id=request_id,
        strategy_id=strategy_id,
        preview_id=preview_id,
    )

    if response["success"] is False or response.get("replayed"):
        result = to_client_response(response)
        if response.get("status") == "no_response":
            result["verification"] = "unknown"
        return result

    data = response["data"]
    request = response["request"]
    symbol_info = mt5.symbol_info(symbol)
    confirmation = confirm_market_opening(mt5, response, symbol_info=symbol_info)
    confirmation.pop("position", None)
    position_id = confirmation.pop("position_id", None)

    if response["status"] == STATUS_PLACED:
        headline = f"Accepted {type} {symbol} {request['volume']} LOT, not filled yet (order {data.order}, {response['retcode_name']})"
    else:
        filled = "Partially filled" if response["status"] == STATUS_PARTIAL else "Filled"
        headline = (
            f"{filled} {type} {symbol} {data.volume} of {request['volume']} LOT at {data.price} "
            f"(order {data.order}, deal {data.deal}, {response['retcode_name']})"
        )
    result = to_client_response(response, headline)
    result.update(confirmation)
    if position_id:
        result["position_id"] = position_id

    protection = confirmation["protection"]["status"]
    if protection in ("missing", "mismatch") and position_id:
        result["message"] += f"; WARNING: the position is NOT protected as requested (protection {protection})"
        _apply_protection_policy(connection, result, position_id=position_id, symbol_info=symbol_info, request=request)
    elif confirmation["verification"] == "unknown":
        result["warning"] = "The execution could not be verified yet (see checks.unknown); verify the open positions before retrying."
        result["message"] += "; not verified yet"
    elif confirmation["verification"] == "pending":
        result["message"] += "; check the order state before sending another order"

    if result.get("request_id"):
        get_request_registry(connection).attach_result(result["request_id"], result, position_ticket=position_id)
    return result


def _apply_protection_policy(connection, result, *, position_id, symbol_info, request):
    """
    Follow ``missing_protection_policy`` for a position without the requested SL/TP.

    - ``report`` (default): only report it.
    - ``retry_sltp``: send the requested SL/TP once and verify them.
    - ``close``: close the position and verify the close.
    """
    from .close_position import close_position

    policy = get_risk_guard(connection).config.missing_protection_policy
    action = {"policy": policy}
    if policy == "retry_sltp":
        response = send_order(
            connection,
            action=TradeRequestActions.SLTP,
            position=position_id,
            symbol=request["symbol"],
            stop_loss=request["sl"],
            take_profit=request["tp"],
        )
        check = confirm_levels(
            mt5, mt5.positions_get(ticket=position_id), symbol_info=symbol_info,
            expected={"sl": request["sl"], "tp": request["tp"]}, label="position_listed",
        )
        action.update(status=response["status"], retcode=response["retcode"], verification=check["verification"], checks=check["checks"])
        if check["verification"] == "confirmed":
            result["protection"]["status"] = "protected"
            result["protection"]["effective_sl"] = request["sl"] or None
            result["protection"]["effective_tp"] = request["tp"] or None
            result["verification"] = "partial" if result.get("status") == STATUS_PARTIAL else "confirmed"
            result["message"] += "; SL/TP set again and verified"
    elif policy == "close":
        closing_id = f"{result['request_id']}:protection-close" if result.get("request_id") else None
        closed = close_position(connection, position_id, request_id=closing_id)
        action.update(status=closed.get("status"), retcode=closed.get("retcode"), verification=closed.get("verification"), message=closed.get("message"))
        result["message"] += f"; position closed by policy ({closed.get('verification')})"
    result["protection_action"] = action
