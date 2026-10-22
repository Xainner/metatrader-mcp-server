import MetaTrader5 as mt5
from typing import Optional, Union
from ..requests_registry import get_request_registry
from ..types import TradeRequestActions
from .send_order import send_order
from ._confirm import Checks
from ._ownership import ownership_error
from ._result import to_client_response, replayed, STATUS_NO_RESPONSE

def close_position(
    connection,
    id: Union[str, int],
    *,
    request_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
):
    """
    Close a position by its ID.

    After the request the terminal is queried again. ``verification`` tells
    what that check found: ``confirmed`` (the position is gone), ``remaining``
    (still open, with ``remaining_volume``), ``unknown`` (the state could not be
    read, or the request got no response) or ``not_closed`` (rejected). Only
    ``confirmed`` returns ``error: False``. ``checks`` lists what was verified,
    when, and what remains unknown.

    Args:
        connection: MetaTrader 5 connection object.
        id: The unique identifier of the position to close.
        request_id: Optional stable ID; repeating it returns the recorded
            state instead of sending another close.
        strategy_id: When given, the position must belong to this strategy.

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

    if request_id is not None:
        record = get_request_registry(connection).get(request_id)
        if record is not None and record["kind"] != "close":
            return {
                "error": True,
                "message": f"request_id {request_id} was already used for a different request; use a new request_id",
                "data": None,
                "request_id": request_id,
                "request_state": record["state"],
            }
        if record is not None:
            # Repeated close: report the recorded state, never send again
            # (the position may already be gone, so this is checked first).
            result = to_client_response(replayed(record))
            result["position_id"] = position_id
            result["verification"] = (record.get("result") or {}).get("verification", "unknown")
            return result

    positions = mt5.positions_get(ticket=position_id)
    if not positions:
        return {
            "error": True,
            "message": f"Invalid position ID '{id}'",
            "data": None,
        }
    position = positions[0]
    error = ownership_error(position, strategy_id, label=f"Position {position_id}")
    if error:
        return {"error": True, "message": error, "data": None, "position_id": position_id, "verification": "not_closed"}

    response = send_order(
        connection,
        action=TradeRequestActions.DEAL,
        position=position_id,
        order_type="SELL" if position.type == 0 else "BUY",
        symbol=position.symbol,
        volume=position.volume,
        magic=int(getattr(position, "magic", 0) or 0),
        request_id=request_id,
    )
    if response.get("replayed"):
        result = to_client_response(response)
        result["position_id"] = position_id
        result["verification"] = "unknown"
        return result
    if response["success"] is False:
        result = to_client_response(response)
        result["position_id"] = position_id
        # A request without response may have been executed: its final state is unknown.
        result["verification"] = "unknown" if response["status"] == STATUS_NO_RESPONSE else "not_closed"
        return _finish(connection, result)

    data = response["data"]
    result = to_client_response(response, f"Closed position {position_id} at price {getattr(data, 'price', None)} ({response['retcode_name']})")
    result["position_id"] = position_id
    checks = Checks()

    # Only a valid listing without the ticket confirms the close; None means the check itself failed.
    remaining = mt5.positions_get(ticket=position_id)
    if remaining is None:
        code, description = mt5.last_error()
        result["error"] = True
        result["verification"] = "unknown"
        checks.missing("position_closed", f"error {code}: {description}")
        result["message"] = (
            f"Close request for position {position_id} was accepted ({response['retcode_name']}), "
            f"but its final state could not be verified (error {code}: {description}). "
            "Check the open positions before sending another close request."
        )
    elif remaining:
        result["error"] = True
        result["verification"] = "remaining"
        result["remaining_volume"] = remaining[0].volume
        checks.ok("position_listed", remaining_volume=remaining[0].volume)
        result["message"] = (
            f"Position {position_id} is still open with {remaining[0].volume} of {position.volume} lots "
            f"after the close request ({response['retcode_name']})"
        )
    else:
        result["verification"] = "confirmed"
        checks.ok("position_closed")
    result["checks"] = checks.as_dict()
    return _finish(connection, result)


def _finish(connection, result):
    if result.get("request_id"):
        get_request_registry(connection).attach_result(result["request_id"], result)
    return result
