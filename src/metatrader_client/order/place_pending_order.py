import MetaTrader5 as mt5
from typing import Optional, Union
from ..requests_registry import get_request_registry
from ..types import TradeRequestActions
from .send_order import send_order
from ._build import pending_type_for
from ._confirm import confirm_pending_opening
from ._result import to_client_response
from ..client_market import MT5Market

def place_pending_order(
	connection,
	*,
	type: str,
	symbol: str,
	volume: Union[float, int],
	price: Union[float, int],
	stop_loss: Optional[Union[float, int]] = None,
	take_profit: Optional[Union[float, int]] = None,
	request_id: Optional[str] = None,
	strategy_id: Optional[str] = None,
	preview_id: Optional[str] = None,
):
	"""
	Places a pending order on the MetaTrader 5 platform.

	This function validates the order type and retrieves the current market
	price for the specified symbol. It then determines whether to place a 
	BUY_LIMIT, BUY_STOP, SELL_LIMIT, or SELL_STOP order based on the current 
	price and the desired order price. The order is sent to the MetaTrader 5 
	server for execution.

	Args:
		connection: MetaTrader 5 connection object.
		type: The type of order, either 'BUY' or 'SELL'.
		symbol: The trading instrument name (e.g., "EURUSD").
		volume: The trade volume in lots.
		price: The price at which to place the pending order.
		stop_loss: Optional stop loss level.
		take_profit: Optional take profit level.
		request_id: Stable ID of this intention; repeating it never sends a second order.
		strategy_id: Strategy owning the order (magic number and strategy limits).
		preview_id: ID returned by ``preview_order``; the order must match it.

	Returns:
		A dictionary with the result of the order placement, the request
		identity and, when the order was placed, ``verification`` and
		``checks`` from re-reading the order in the terminal.
	"""
	
	type = str(type).upper()
	accepted_types  = ["BUY", "SELL"]
	if type not in accepted_types:
		return { "error": True, "message": f"Invalid type, should be BUY or SELL.", "data": None }
	
	market = MT5Market(connection)
	current_price = market.get_symbol_price(symbol_name=symbol)
	if current_price is None:
		return { "error": True, "message": f"Cannot get latest market price for {symbol}", "data": None }
	
	price = float(price)
	order_type = pending_type_for(type, price, current_price["bid"], current_price["ask"])

	response = send_order(
		connection,
		action = TradeRequestActions.PENDING,
		order_type = order_type,
		symbol = symbol,
		volume = volume,
		price = price,
		stop_loss = stop_loss,
		take_profit = take_profit,
		request_id = request_id,
		strategy_id = strategy_id,
		preview_id = preview_id,
	)

	if response["success"] is False or response.get("replayed"):
		result = to_client_response(response)
		if response.get("status") == "no_response":
			result["verification"] = "unknown"
		return result

	result = to_client_response(
		response,
		f"Placed pending order {order_type} {symbol} {volume} LOT at {price} (Order ID: {response['data'].order}, {response['retcode_name']})",
	)
	result.update(confirm_pending_opening(mt5, response, symbol_info=mt5.symbol_info(symbol)))
	if result["verification"] != "confirmed":
		result["message"] += f"; verification {result['verification']} (see checks)"
	if result.get("request_id"):
		get_request_registry(connection).attach_result(result["request_id"], result)
	return result