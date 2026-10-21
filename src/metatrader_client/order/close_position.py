import MetaTrader5 as mt5
from typing import Union
from ..types import TradeRequestActions
from .send_order import send_order
from ._result import to_client_response, STATUS_NO_RESPONSE

def close_position(connection, id: Union[str, int]):
    """
    Close a position by its ID.

    After the request the terminal is queried again. ``verification`` tells
    what that check found: ``confirmed`` (the position is gone), ``remaining``
    (still open, with ``remaining_volume``), ``unknown`` (the state could not be
    read, or the request got no response) or ``not_closed`` (rejected). Only
    ``confirmed`` returns ``error: False``.

    Args:
        connection: MetaTrader 5 connection object.
        id: The unique identifier of the position to close.

    Returns:
        A dictionary containing an error flag, a message, the order_send
        result, the status and the trade server return code.
    """

    try:
        position_id = int(id)
    except (TypeError, ValueError):
        return {
            "error": True,
            "message": f"Invalid position ID '{id}', it should be a valid integer",
            "data": None,
        }

    positions = mt5.positions_get(ticket=position_id)
    if not positions:
        return {
            "error": True,
            "message": f"Invalid position ID '{id}'",
            "data": None,
        }
    position = positions[0]
    response = send_order(
        connection,
        action=TradeRequestActions.DEAL,
        position=position_id,
        order_type="SELL" if position.type == 0 else "BUY",
        symbol=position.symbol,
        volume=position.volume,
    )
    if response["success"] is False:
        result = to_client_response(response)
        result["position_id"] = position_id
        # A request without response may have been executed: its final state is unknown.
        result["verification"] = "unknown" if response["status"] == STATUS_NO_RESPONSE else "not_closed"
        return result

    data = response["data"]
    result = to_client_response(response, f"Closed position {position_id} at price {getattr(data, 'price', None)} ({response['retcode_name']})")
    result["position_id"] = position_id

    # Only a valid listing without the ticket confirms the close; None means the check itself failed.
    remaining = mt5.positions_get(ticket=position_id)
    if remaining is None:
        code, description = mt5.last_error()
        result["error"] = True
        result["verification"] = "unknown"
        result["message"] = (
            f"Close request for position {position_id} was accepted ({response['retcode_name']}), "
            f"but its final state could not be verified (error {code}: {description}). "
            "Check the open positions before sending another close request."
        )
    elif remaining:
        result["error"] = True
        result["verification"] = "remaining"
        result["remaining_volume"] = remaining[0].volume
        result["message"] = (
            f"Position {position_id} is still open with {remaining[0].volume} of {position.volume} lots "
            f"after the close request ({response['retcode_name']})"
        )
    else:
        result["verification"] = "confirmed"
    return result
