"""
MetaTrader 5 order sending function.

This module provides functionality to send trading orders to MetaTrader 5.
It supports various trading operations including market orders, pending orders,
modification of positions and orders, and position closing.
"""

import MetaTrader5 as mt5
from typing import Optional, Union, Dict
from datetime import datetime

from ..risk import get_risk_guard
from ..types import (
	TradeRequestActions,
	OrderType,
	OrderFilling,
	OrderTime
)
from ._result import interpret_order_result, failure
from ._validation import (
	is_buy,
	select_filling_mode,
	validate_volume,
	normalize_price,
	check_stops,
	check_pending_price,
)

PENDING_TYPES = (
	OrderType.BUY_LIMIT.value,
	OrderType.SELL_LIMIT.value,
	OrderType.BUY_STOP.value,
	OrderType.SELL_STOP.value,
)


def _to_code(enum_cls, value) -> Optional[int]:
	"""Convert an enum member, name or numeric code to its numeric code."""
	if value is None:
		return None
	if isinstance(value, int) and not isinstance(value, bool):
		return value if enum_cls.exists(value) else None
	return enum_cls.validate(value)


def _optional_level(value) -> Optional[float]:
	"""Convert an optional SL/TP level, where None and 0 mean no level."""
	if value is None:
		return None
	value = float(value)
	return value if value != 0 else None


def _load_symbol(symbol: Optional[str]):
	"""Select a symbol and return (symbol_info, tick, error)."""
	if not symbol:
		return None, None, "Parameter `symbol` is required for this operation"
	if not mt5.symbol_select(symbol, True):
		return None, None, f"Invalid symbol or failed to select {symbol}"
	symbol_info = mt5.symbol_info(symbol)
	if symbol_info is None:
		return None, None, f"Failed to get symbol info for {symbol}"
	tick = mt5.symbol_info_tick(symbol)
	if tick is None or not tick.bid or not tick.ask:
		return symbol_info, None, f"No current quote for {symbol}"
	return symbol_info, tick, None


def _normalize_levels(symbol_info, stop_loss, take_profit):
	"""Normalize optional SL/TP to the symbol precision. Returns (sl, tp, error)."""
	levels = []
	for label, value in (("Stop loss", stop_loss), ("Take profit", take_profit)):
		value = _optional_level(value)
		if value is not None:
			value, error = normalize_price(symbol_info, value, label)
			if error:
				return None, None, error
		levels.append(value)
	return levels[0], levels[1], None


def _send(request: Dict) -> Dict:
	"""Send a request and interpret its result."""
	return interpret_order_result(mt5, mt5.order_send(request), request)


def _send_opening(guard, request: Dict, *, symbol: str, side: int, stop_loss, duplicate_key) -> Dict:
	"""
	Check the risk limits, send an order that opens exposure and reserve it, atomically.

	The guard lock is held from the first check until the reservation is stored,
	so concurrent requests on the same connection cannot pass the checks with
	the same budget or the same duplicate key.
	"""
	with guard.opening():
		error = guard.check_duplicate(duplicate_key) or guard.check_new_order(
			mt5, symbol=symbol, side=side, volume=request["volume"], entry_price=request["price"], stop_loss=stop_loss
		)
		if error:
			return failure(error)
		response = _send(request)
		guard.after_send(response, duplicate_key, request["volume"])
		return response


def send_order(
	connection,
	*,
	action: Union[str, int, TradeRequestActions],
	symbol: Optional[str] = None,
	volume: Optional[float] = None,
	order_type: Optional[Union[str, int, OrderType]] = None,
	price: Optional[float] = None,
	stop_loss: Optional[float] = None,
	take_profit: Optional[float] = None,
	deviation: Optional[int] = 20,
	magic: Optional[int] = 0,
	comment: Optional[str] = "",
	position: Optional[int] = None,
	position_by: Optional[int] = None,
	order: Optional[int] = None,
	expiration: Optional[datetime] = None,
	type_filling: Optional[Union[str, int, OrderFilling]] = None,
	type_time: Optional[Union[str, int, OrderTime]] = None,
	stoplimit: Optional[float] = None
) -> Dict:
	"""
	Send a trading order to MetaTrader 5.

	The request is validated against the symbol specification (volume limits
	and step, price precision, minimal stop distance) and against the risk
	limits configured for the connection before it is sent. The result of
	``order_send`` is then interpreted from its ``retcode``.

	Args:
		connection: MetaTrader 5 connection object
		action: Trading operation type (DEAL, PENDING, SLTP, MODIFY, REMOVE, CLOSE_BY)
		symbol: Trading instrument name (e.g., "EURUSD")
		volume: Trade volume in lots
		order_type: Order type (BUY, SELL, BUY_LIMIT, etc.)
		price: Order price. Required for PENDING and MODIFY; for DEAL the current quote is used when omitted
		stop_loss: Stop Loss level. None or 0 means no stop loss
		take_profit: Take Profit level. None or 0 means no take profit
		deviation: Maximum acceptable price deviation in points (for market orders)
		magic: Expert Advisor ID (magic number)
		comment: Order comment
		position: Position ticket (required for SLTP, and to close a position with DEAL)
		position_by: Opposite position ticket (for CLOSE_BY operations)
		order: Order ticket (required for MODIFY and REMOVE)
		expiration: Order expiration time (for pending orders)
		type_filling: Order filling type; selected from the symbol settings when omitted
		type_time: Order lifetime type (GTC, DAY, SPECIFIED, SPECIFIED_DAY)
		stoplimit: Stop limit price (for STOP_LIMIT orders)

	Returns:
		Dictionary containing:
		- 'success': True when the request was completed, partially completed or placed
		- 'status': 'done', 'partial', 'placed', 'no_changes', 'rejected' or 'no_response'
		- 'retcode' / 'retcode_name': Trade server return code, None if the request was not sent
		- 'message': Human-readable message describing the result
		- 'data': Raw order_send result, None if the request was not sent
		- 'request': The request that was sent

	Notes:
		Different parameters are required depending on the action:
		- DEAL (market order): symbol, volume, order_type (BUY/SELL); position to close a position
		- PENDING: symbol, volume, price, order_type
		- SLTP: position, sl and tp (both are sent; pass the current value to keep it)
		- MODIFY: order, price, sl and tp (all are sent; pass the current value to keep it)
		- REMOVE: order
		- CLOSE_BY: not supported
	"""
	action = _to_code(TradeRequestActions, action)
	if action is None:
		return failure("Invalid trade action")
	order_type = _to_code(OrderType, order_type)
	comment = comment if comment else "MCP"
	guard = get_risk_guard(connection)

	error = guard.check_account(mt5)
	if error:
		return failure(error)

	# ------------------------------
	# Market execution (BUY or SELL)
	# ------------------------------
	if action == TradeRequestActions.DEAL.value:

		if order_type not in (OrderType.BUY.value, OrderType.SELL.value):
			return failure("Invalid order type, must be BUY or SELL")
		closing = bool(position)

		symbol_info, tick, error = _load_symbol(symbol)
		if error:
			return failure(error)

		volume, error = validate_volume(symbol_info, volume)
		if error:
			return failure(error)

		# The quote is read before validating SL/TP, so they are compared with the real price.
		if price:
			price, error = normalize_price(symbol_info, price)
			if error:
				return failure(error)
		else:
			price = tick.ask if order_type == OrderType.BUY.value else tick.bid

		stop_loss, take_profit, error = _normalize_levels(symbol_info, stop_loss, take_profit)
		if error:
			return failure(error)
		if not closing:
			# A new position is closed at bid (buy) or ask (sell): stops are measured from that price.
			reference = tick.bid if order_type == OrderType.BUY.value else tick.ask
			error = check_stops(symbol_info, order_type, reference_price=reference, stop_loss=stop_loss, take_profit=take_profit)
			if error:
				return failure(error)

		filling = _to_code(OrderFilling, type_filling)
		if filling is None:
			filling, error = select_filling_mode(symbol_info, pending=False)
			if error:
				return failure(error)

		request = {
			"action": action,
			"symbol": symbol,
			"volume": volume,
			"type": order_type,
			"price": price,
			"sl": stop_loss or 0.0,
			"tp": take_profit or 0.0,
			"deviation": int(deviation if deviation is not None else 20),
			"magic": int(magic or 0),
			"comment": comment,
			"type_time": OrderTime.GTC.value,
			"type_filling": filling,
		}
		if closing:
			request["position"] = int(position)
			return _send(request)

		duplicate_key = ("DEAL", symbol, order_type, volume, stop_loss, take_profit)
		return _send_opening(guard, request, symbol=symbol, side=order_type, stop_loss=stop_loss, duplicate_key=duplicate_key)

	# ----------------------------------------------------------
	# Pending order (BUY_LIMIT, SELL_LIMIT, BUY_STOP, SELL_STOP)
	# ----------------------------------------------------------
	if action == TradeRequestActions.PENDING.value:

		if order_type not in PENDING_TYPES:
			return failure("Invalid order type, must be BUY_LIMIT, SELL_LIMIT, BUY_STOP, or SELL_STOP")

		symbol_info, tick, error = _load_symbol(symbol)
		if error:
			return failure(error)

		volume, error = validate_volume(symbol_info, volume)
		if error:
			return failure(error)

		price, error = normalize_price(symbol_info, price)
		if error:
			return failure(error)
		error = check_pending_price(symbol_info, order_type, price=price, bid=tick.bid, ask=tick.ask)
		if error:
			return failure(error)

		stop_loss, take_profit, error = _normalize_levels(symbol_info, stop_loss, take_profit)
		if error:
			return failure(error)
		error = check_stops(symbol_info, order_type, reference_price=price, stop_loss=stop_loss, take_profit=take_profit)
		if error:
			return failure(error)

		filling = _to_code(OrderFilling, type_filling)
		if filling is None:
			filling, _ = select_filling_mode(symbol_info, pending=True)

		lifetime = _to_code(OrderTime, type_time)
		if lifetime is None:
			lifetime = OrderTime.SPECIFIED.value if expiration else OrderTime.GTC.value

		request = {
			"action": action,
			"symbol": symbol,
			"volume": volume,
			"type": order_type,
			"price": price,
			"sl": stop_loss or 0.0,
			"tp": take_profit or 0.0,
			"deviation": int(deviation if deviation is not None else 20),
			"magic": int(magic or 0),
			"comment": comment,
			"type_time": lifetime,
			"type_filling": filling,
		}
		if expiration:
			request["expiration"] = expiration

		side = OrderType.BUY.value if is_buy(order_type) else OrderType.SELL.value
		duplicate_key = ("PENDING", symbol, order_type, volume, price, stop_loss, take_profit)
		return _send_opening(guard, request, symbol=symbol, side=side, stop_loss=stop_loss, duplicate_key=duplicate_key)

	# -----------------------
	# Modify position (SL/TP)
	# -----------------------
	if action == TradeRequestActions.SLTP.value:

		if not position:
			return failure("Parameter `position` is required for this operation")
		if stop_loss is None or take_profit is None:
			return failure("Parameters `stop_loss` and `take_profit` are required; pass the current value to keep it")

		request = {
			"action": action,
			"position": int(position),
			"sl": float(stop_loss),
			"tp": float(take_profit),
		}
		if symbol:
			request["symbol"] = symbol
		return _send(request)

	# --------------------
	# Modify pending order
	# --------------------
	if action == TradeRequestActions.MODIFY.value:

		if not order:
			return failure("Parameter `order` is required for this operation")
		if price is None or stop_loss is None or take_profit is None:
			return failure("Parameters `price`, `stop_loss` and `take_profit` are required; pass the current value to keep it")

		request = {
			"action": action,
			"order": int(order),
			"price": float(price),
			"sl": float(stop_loss),
			"tp": float(take_profit),
		}
		lifetime = _to_code(OrderTime, type_time)
		if lifetime is not None:
			request["type_time"] = lifetime
		if expiration:
			request["expiration"] = expiration
		if stoplimit:
			request["stoplimit"] = float(stoplimit)
		if symbol:
			request["symbol"] = symbol
		return _send(request)

	# --------------------
	# Remove pending order
	# --------------------
	if action == TradeRequestActions.REMOVE.value:

		if not order:
			return failure("Parameter `order` is required for this operation")

		return _send({"action": action, "order": int(order)})

	return failure(f"Unsupported trade action {TradeRequestActions.to_string(action)}")
