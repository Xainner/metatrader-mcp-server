import MetaTrader5 as mt5
from typing import Optional, Union
from ..types import TradeRequestActions
from .send_order import send_order
from ._confirm import Checks
from ._ownership import ownership_error
from ._result import to_client_response

def cancel_pending_order(connection, id: Union[int, str], *, strategy_id: Optional[str] = None):
    """
    Cancel a pending order by its ID.

    After an accepted request the order is looked up again: ``verification``
    is ``confirmed`` (no longer listed), ``remaining`` (still listed) or
    ``unknown`` (the listing failed or the request got no response); only
    ``confirmed`` returns ``error: False``.

    Args:
        connection: The connection object to the MetaTrader platform.
        id: The unique identifier of the pending order to cancel.
        strategy_id: When given, the order must belong to this strategy.

    Returns:
        A dictionary containing an error flag, a message, and the order data
        if successful.
    """
    try:
        order_id = int(id)
    except (TypeError, ValueError):
        return {
            "error": True,
            "message": f"Invalid order ID {id}",
            "data": None,
        }
    if strategy_id is not None:
        orders = mt5.orders_get(ticket=order_id)
        if not orders:
            return {"error": True, "message": f"Invalid order ID {id}", "data": None}
        error = ownership_error(orders[0], strategy_id, label=f"Order {order_id}")
        if error:
            return {"error": True, "message": error, "data": None, "verification": "not_cancelled"}
    response = send_order(
        connection,
        action=TradeRequestActions.REMOVE,
        order=order_id,
    )
    result = to_client_response(response, f"Canceled pending order {order_id} ({response['retcode_name']})")
    if not response["success"]:
        result["verification"] = "unknown" if response["status"] == "no_response" else "not_cancelled"
        return result

    checks = Checks()
    remaining = mt5.orders_get(ticket=order_id)
    if remaining is None:
        code, description = mt5.last_error()
        checks.missing("order_removed", f"error {code}: {description}")
        result.update(error=True, verification="unknown")
        result["message"] += f"; the final state could not be verified (error {code}: {description})"
    elif remaining:
        checks.ok("order_listed")
        result.update(error=True, verification="remaining")
        result["message"] += "; the order is still listed after the request"
    else:
        checks.ok("order_removed")
        result["verification"] = "confirmed"
    result["checks"] = checks.as_dict()
    return result
