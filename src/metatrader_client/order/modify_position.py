import MetaTrader5 as mt5
from typing import Optional, Union
from ..risk import get_risk_guard
from ..types import TradeRequestActions
from .send_order import send_order
from ._confirm import confirm_levels
from ._ownership import ownership_error
from ._result import to_client_response
from ._validation import normalize_price, check_stops

def modify_position(
	connection,
	id: Union[str, int],
	*,
	stop_loss: Optional[Union[int, float]] = None,
	take_profit: Optional[Union[int, float]] = None,
	strategy_id: Optional[str] = None,
):
	"""
	Modify an existing position's stop loss and take profit levels.

	This function attempts to modify the stop loss and take profit of an
	open position identified by the given ID. If the position ID is invalid
	or not found, it returns an error. New levels are validated against the
	symbol precision, the current price and the minimal stop distance.

	Args:
		connection: The connection object to the MetaTrader platform.
		id: The unique identifier of the position to modify.
		stop_loss: The new stop loss level. If None, the current stop loss
			level is retained. 0 removes the stop loss.
		take_profit: The new take profit level. If None, the current take
			profit level is retained. 0 removes the take profit.

	Returns:
		A dictionary containing an error flag, a message, the order_send
		result, the status and the trade server return code.
	"""

	try:
		position_id = int(id)
	except (TypeError, ValueError):
		position_id = None

	positions = mt5.positions_get(ticket=position_id) if position_id is not None else None
	if not positions:
		return {
			"error": True,
			"message": f"Invalid position ID {id}",
			"data": None,
		}
	position = positions[0]
	error = ownership_error(position, strategy_id, label=f"Position {position_id}")
	if error:
		return { "error": True, "message": error, "data": None }

	if stop_loss is None and take_profit is None:
		return { "error": True, "message": "Nothing to modify: pass stop_loss and/or take_profit", "data": None }

	symbol_info = mt5.symbol_info(position.symbol)
	tick = mt5.symbol_info_tick(position.symbol)
	if symbol_info is None or tick is None:
		return { "error": True, "message": f"No symbol information or quote for {position.symbol}", "data": None }

	levels = {"sl": position.sl, "tp": position.tp}
	changed = {}
	for key, label, requested in (("sl", "Stop loss", stop_loss), ("tp", "Take profit", take_profit)):
		if requested is None:
			continue
		if float(requested) == 0:
			levels[key] = 0.0
			continue
		levels[key], error = normalize_price(symbol_info, requested, label)
		if error:
			return { "error": True, "message": error, "data": None }
		changed[key] = levels[key]

	# Only new levels are checked: an unchanged level may legitimately sit inside the stop distance now.
	reference = tick.bid if position.type == 0 else tick.ask
	error = check_stops(
		symbol_info,
		position.type,
		reference_price=reference,
		stop_loss=changed.get("sl"),
		take_profit=changed.get("tp"),
	)
	if error:
		return { "error": True, "message": error, "data": None }

	if stop_loss is not None:
		error = get_risk_guard(connection).check_stop_loss_risk(
			mt5,
			symbol=position.symbol,
			side=position.type,
			volume=position.volume,
			entry_price=position.price_open,
			stop_loss=levels["sl"],
		)
		if error:
			return { "error": True, "message": error, "data": None }

	response = send_order(
		connection,
		action = TradeRequestActions.SLTP,
		position = position_id,
		symbol = position.symbol,
		stop_loss = levels["sl"],
		take_profit = levels["tp"],
	)

	result = to_client_response(
		response,
		f"Modified position {position_id}: SL {levels['sl']}, TP {levels['tp']} ({response['retcode_name']})",
	)
	if not response["success"]:
		result["verification"] = "unknown" if response["status"] == "no_response" else "not_applied"
		return result
	result.update(confirm_levels(
		mt5, mt5.positions_get(ticket=position_id), symbol_info=symbol_info,
		expected={"sl": levels["sl"], "tp": levels["tp"]}, label="position_listed",
	))
	if result["verification"] != "confirmed":
		result["error"] = True
		result["message"] += (
			f"; the final levels could not be confirmed ({result['verification']}, see checks). "
			"Read the position before sending another modification."
		)
	return result
