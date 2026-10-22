"""
Interpretation of ``mt5.order_send`` results.

``order_send`` returns ``None`` when the request never reached the trade
server, and otherwise a result whose ``retcode`` tells whether the request was
completed, partially completed, placed or rejected. ``last_error`` only
describes terminal/IPC failures, so it cannot be used to detect rejections.
"""

from typing import Any, Dict, Optional, Tuple

from ..types import TradeReturnCodes, TradeRequestActions

# Statuses returned in the "status" field of send_order results.
STATUS_DONE = "done"              # Request completed (market order filled, modification applied, ...)
STATUS_PARTIAL = "partial"        # Market order filled only for part of the requested volume
STATUS_PLACED = "placed"          # Order accepted and placed, not executed yet
STATUS_NO_CHANGES = "no_changes"  # Request matched the current state, nothing was changed
STATUS_REJECTED = "rejected"      # Request rejected by the terminal or the trade server
STATUS_NO_RESPONSE = "no_response"  # order_send returned None, final state unknown

SUCCESS_STATUSES = (STATUS_DONE, STATUS_PARTIAL, STATUS_PLACED)


def _last_error(mt5) -> Tuple[Any, Any]:
    try:
        return mt5.last_error()
    except Exception:  # pragma: no cover - defensive, last_error should not raise
        return None, "unknown"


def interpret_order_result(mt5, result, request: Dict[str, Any]) -> Dict[str, Any]:
    """
    Classify an ``order_send`` result.

    Args:
        mt5: MetaTrader5 module (injected for testability).
        result: Value returned by ``mt5.order_send``.
        request: The request that was sent.

    Returns:
        dict with keys ``success``, ``status``, ``retcode``, ``retcode_name``,
        ``message``, ``data`` (the raw result) and ``request``.
    """
    if result is None:
        code, description = _last_error(mt5)
        return {
            "success": False,
            "status": STATUS_NO_RESPONSE,
            "retcode": None,
            "retcode_name": None,
            "message": (
                f"No response from the terminal (error {code}: {description}). "
                "The final state is unknown: check positions and orders before retrying."
            ),
            "data": None,
            "request": request,
        }

    retcode = getattr(result, "retcode", None)
    name = TradeReturnCodes.to_string(retcode)
    comment = getattr(result, "comment", "") or ""
    detail = f"{name} ({retcode})" + (f": {comment}" if comment else "")

    if retcode == TradeReturnCodes.DONE.value:
        status = STATUS_DONE
        if request.get("action") == TradeRequestActions.DEAL.value:
            requested = float(request.get("volume") or 0)
            filled = float(getattr(result, "volume", 0) or 0)
            if 0 < filled < requested - 1e-9:
                status = STATUS_PARTIAL
    elif retcode == TradeReturnCodes.DONE_PARTIAL.value:
        status = STATUS_PARTIAL
    elif retcode == TradeReturnCodes.PLACED.value:
        status = STATUS_PLACED
    elif retcode == TradeReturnCodes.NO_CHANGES.value:
        status = STATUS_NO_CHANGES
    else:
        status = STATUS_REJECTED

    success = status in SUCCESS_STATUSES
    if status == STATUS_PARTIAL:
        filled = getattr(result, "volume", None)
        message = f"Partially filled {filled} of {request.get('volume')} lots, {detail}"
    elif success:
        message = f"Request completed, {detail}" if status == STATUS_DONE else f"Order placed, {detail}"
    elif status == STATUS_NO_CHANGES:
        message = f"Nothing changed, {detail}"
    else:
        message = f"Request rejected, {detail}"

    return {
        "success": success,
        "status": status,
        "retcode": retcode,
        "retcode_name": name,
        "message": message,
        "data": result,
        "request": request,
    }


def failure(message: str, *, status: Optional[str] = STATUS_REJECTED) -> Dict[str, Any]:
    """Result for requests rejected locally, before reaching the terminal."""
    return {
        "success": False,
        "status": status,
        "retcode": None,
        "retcode_name": None,
        "message": message,
        "data": None,
        "request": None,
    }


# Registry state -> status reported when a request_id is repeated.
_STATE_STATUS = {
    "filled": STATUS_DONE,
    "partially_filled": STATUS_PARTIAL,
    "accepted": STATUS_PLACED,
    "rejected": STATUS_REJECTED,
    "unknown": STATUS_NO_RESPONSE,
    "sending": STATUS_NO_RESPONSE,
    "prepared": "in_progress",
    "reconciled": "reconciled",
}
_EXECUTED_STATES = ("filled", "partially_filled", "accepted")


def replayed(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Result for a request_id that is already recorded: nothing is sent again.

    ``previous_result`` holds the client response stored for the first
    request, when it finished; ``request_state`` the current registry state.
    """
    state = record["state"]
    in_flight = state in ("prepared", "sending")
    message = (
        f"request_id {record['request_id']} is already recorded with state '{state}'"
        + (f" (outcome {record['outcome']})" if record.get("outcome") else "")
        + ("; it is still in progress" if in_flight else "")
        + "; nothing was sent again. Use a new request_id for a new operation."
    )
    return {
        "success": state in _EXECUTED_STATES,
        "status": _STATE_STATUS.get(state, state),
        "retcode": record.get("retcode"),
        "retcode_name": TradeReturnCodes.to_string(record["retcode"]) if record.get("retcode") is not None else None,
        "message": message,
        "data": None,
        "request": None,
        "request_id": record["request_id"],
        "request_state": state,
        "replayed": True,
        "previous_result": record.get("result"),
    }


_PASS_THROUGH = ("request_id", "request_state", "replayed", "previous_result", "timings")


def to_client_response(response: Dict[str, Any], message: Optional[str] = None) -> Dict[str, Any]:
    """Convert a send_order result into the public ``{"error", "message", "data"}`` format."""
    result = {
        "error": not response["success"],
        "message": message if (message and response["success"] and not response.get("replayed")) else response["message"],
        "data": response["data"] if response["success"] else None,
        "status": response.get("status"),
        "retcode": response.get("retcode"),
    }
    for key in _PASS_THROUGH:
        if key in response:
            result[key] = response[key]
    return result
