"""
MetaTrader 5 order sending function.

This module provides functionality to send trading orders to MetaTrader 5.
It supports various trading operations including market orders, pending orders,
modification of positions and orders, and position closing.
"""

import MetaTrader5 as mt5
from typing import Optional, Union, Dict, Any
from datetime import datetime

from ..metrics import metrics, timed
from ..requests_registry import (
	fingerprint,
	get_request_registry,
	new_request_id,
	request_tag,
	validate_request_id,
)
from ..risk import entries_block_reason, get_risk_guard
from ..store import get_store
from ..strategy import resolve_magic, validate_strategy_id
from ..types import (
	TradeRequestActions,
	OrderType,
	OrderFilling,
	OrderTime
)
from ._build import (
	build_comment,
	build_opening_request,
	opening_params,
	to_code as _to_code,
)
from ._preview_store import get_preview_store
from ._result import interpret_order_result, failure, replayed, STATUS_NO_RESPONSE
from ._validation import select_filling_mode, normalize_price, validate_volume


def _send(request: Dict) -> Dict:
	"""Send a request and interpret its result. An exception from the terminal means an unknown outcome."""
	try:
		result = mt5.order_send(request)
	except Exception as exc:  # the request may or may not have reached the server
		response = interpret_order_result(mt5, None, request)
		response["message"] = (
			f"order_send raised {type(exc).__name__}: {exc}. "
			"The final state is unknown: check positions and orders before retrying."
		)
		return response
	return interpret_order_result(mt5, result, request)


def _account_identity():
	account = mt5.account_info()
	if account is None:
		return None, None
	return getattr(account, "login", None), getattr(account, "server", None)


def _begin(registry, request_id: Optional[str], request: Dict, *, kind: str, strategy_id, magic):
	"""Register the intention; returns (request_id, early_response or None)."""
	error = validate_request_id(request_id)
	if error:
		return request_id, failure(error)
	login, server = _account_identity()
	params = opening_params(request, strategy_id) if kind == "open" else {
		"action": request["action"], "position": request.get("position"), "volume": request.get("volume"),
		"type": request.get("type"), "symbol": request.get("symbol"),
	}
	begun = registry.begin(
		request_id, params=params, kind=kind, account=login, server=server,
		strategy_id=strategy_id, magic=magic, symbol=request.get("symbol"),
	)
	if begun["status"] == "conflict":
		response = failure(
			f"request_id {request_id} was already used for a different request "
			f"(state {begun['record']['state']}); use a new request_id for a new operation"
		)
		response["request_id"] = request_id
		response["request_state"] = begun["record"]["state"]
		return request_id, response
	if begun["status"] == "existing":
		return request_id, replayed(begun["record"])
	return request_id, None


def _send_tracked(registry, request_id: str, request: Dict) -> Dict:
	"""Mark the request as sending, send it and store the interpreted result."""
	registry.mark(request_id, "sending")
	with timed() as t:
		response = _send(request)
	metrics.record("broker_send", t.ms, ok=response["status"] != STATUS_NO_RESPONSE, error=None if response["status"] != STATUS_NO_RESPONSE else response["message"])
	state = registry.record_send_result(request_id, response)
	response["request_id"] = request_id
	response["request_state"] = state
	response["timings"] = {"send_ms": round(t.ms, 3)}
	return response


def _reject_locally(registry, request_id: str, error: str) -> Dict:
	registry.mark(request_id, "rejected", detail=error, outcome="blocked_locally", message=error)
	response = failure(error)
	response["request_id"] = request_id
	response["request_state"] = "rejected"
	return response


def _send_opening(
	connection,
	guard,
	request: Dict,
	*,
	info: Dict[str, Any],
	request_id: Optional[str],
	strategy_id: Optional[str],
	preview_id: Optional[str],
	duplicate_key,
) -> Dict:
	"""
	Register, check the risk limits, send an order that opens exposure and reserve it.

	The guard lock is held from the first check until the reservation is stored,
	so concurrent requests on the same connection cannot pass the checks with
	the same budget or the same duplicate key. The request identity is stored
	before any check (see ``metatrader_client.requests_registry``).
	"""
	registry = get_request_registry(connection)
	request_id = request_id if request_id is not None else new_request_id()
	request["comment"] = build_comment(request.get("comment"), request_tag(str(request_id)))
	request_id, early = _begin(registry, request_id, request, kind="open", strategy_id=strategy_id, magic=request["magic"])
	if early:
		return early

	symbol, side, stop_loss = request["symbol"], info["side"], info["stop_loss"]
	with guard.opening():
		error = entries_block_reason(guard.config, get_store(connection))
		if not error and guard.config.block_on_unresolved:
			login, server = _account_identity()
			pending = registry.unresolved(account=login, server=server, symbol=symbol, exclude=request_id)
			if pending:
				ids = ", ".join(r["request_id"] for r in pending)
				error = (
					f"Requests with an unknown outcome on {symbol}: {ids}. They may have been executed; "
					"reconcile them (reconcile_request) or resolve them explicitly (resolve_request) before opening new exposure"
				)
		if not error:
			error = get_preview_store(connection).consume(
				preview_id, fingerprint=registry_fingerprint(request, strategy_id), entry_price=request["price"]
			)
		if not error:
			error = guard.check_duplicate(duplicate_key) or guard.check_new_order(
				mt5, symbol=symbol, side=side, volume=request["volume"], entry_price=request["price"],
				stop_loss=stop_loss, magic=request["magic"], strategy_id=strategy_id,
			)
		if error:
			return _reject_locally(registry, request_id, error)
		risk, _ = guard.loss_at_stop(
			mt5, symbol=symbol, side=side, volume=request["volume"], entry_price=request["price"], stop_loss=stop_loss
		) if stop_loss else (None, None)
		response = _send_tracked(registry, request_id, request)
		guard.after_send(response, duplicate_key, request["volume"], magic=request["magic"], risk=risk)
		return response


def registry_fingerprint(request: Dict, strategy_id: Optional[str]) -> str:
	"""Fingerprint of an opening request, shared with ``preview_order``."""
	return fingerprint(opening_params(request, strategy_id))


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
	magic: Optional[int] = None,
	comment: Optional[str] = "",
	position: Optional[int] = None,
	position_by: Optional[int] = None,
	order: Optional[int] = None,
	expiration: Optional[datetime] = None,
	type_filling: Optional[Union[str, int, OrderFilling]] = None,
	type_time: Optional[Union[str, int, OrderTime]] = None,
	stoplimit: Optional[float] = None,
	request_id: Optional[str] = None,
	strategy_id: Optional[str] = None,
	preview_id: Optional[str] = None,
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
		magic: Magic number; derived from ``strategy_id`` when omitted (0 without strategy)
		comment: Order comment (the request tag is appended for openings)
		position: Position ticket (required for SLTP, and to close a position with DEAL)
		position_by: Opposite position ticket (for CLOSE_BY operations)
		order: Order ticket (required for MODIFY and REMOVE)
		expiration: Order expiration time (for pending orders)
		type_filling: Order filling type; selected from the symbol settings when omitted
		type_time: Order lifetime type (GTC, DAY, SPECIFIED, SPECIFIED_DAY)
		stoplimit: Stop limit price (for STOP_LIMIT orders)
		request_id: Stable ID of this trading intention (generated when omitted for openings).
			Repeating it returns the recorded state instead of sending again.
		strategy_id: Strategy that owns the order (sets the magic number and the strategy limits)
		preview_id: ID returned by ``preview_order``; the order must match the preview

	Returns:
		Dictionary containing:
		- 'success': True when the request was completed, partially completed or placed
		- 'status': 'done', 'partial', 'placed', 'no_changes', 'rejected' or 'no_response'
		- 'retcode' / 'retcode_name': Trade server return code, None if the request was not sent
		- 'message': Human-readable message describing the result
		- 'data': Raw order_send result, None if the request was not sent
		- 'request': The request that was sent
		- 'request_id' / 'request_state': Request identity and registry state (tracked requests)
		- 'replayed': True when the request_id was already recorded and nothing was sent

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

	error = validate_strategy_id(strategy_id)
	if error:
		return failure(error)
	resolved_magic = resolve_magic(strategy_id, magic)
	if resolved_magic is None:
		return failure(f"magic {magic} does not match the magic number of strategy {strategy_id}")

	error = guard.check_account(mt5)
	if error:
		return failure(error)

	closing = action == TradeRequestActions.DEAL.value and bool(position)

	# ------------------------------------------------------------
	# Orders that open exposure: market (BUY/SELL) and pending
	# ------------------------------------------------------------
	if action in (TradeRequestActions.DEAL.value, TradeRequestActions.PENDING.value) and not closing:
		request, info, error = build_opening_request(
			mt5,
			action=action,
			symbol=symbol,
			volume=volume,
			order_type=order_type,
			price=price,
			stop_loss=stop_loss,
			take_profit=take_profit,
			deviation=deviation,
			magic=resolved_magic,
			comment=comment,
			expiration=expiration,
			type_filling=type_filling,
			type_time=type_time,
		)
		if error:
			return failure(error)
		if action == TradeRequestActions.DEAL.value:
			duplicate_key = ("DEAL", symbol, order_type, request["volume"], info["stop_loss"], info["take_profit"], resolved_magic)
		else:
			duplicate_key = ("PENDING", symbol, order_type, request["volume"], request["price"], info["stop_loss"], info["take_profit"], resolved_magic)
		return _send_opening(
			connection, guard, request, info=info, request_id=request_id,
			strategy_id=strategy_id, preview_id=preview_id, duplicate_key=duplicate_key,
		)

	# ---------------------------------
	# Close a position with a DEAL order
	# ---------------------------------
	if closing:
		if order_type not in (OrderType.BUY.value, OrderType.SELL.value):
			return failure("Invalid order type, must be BUY or SELL")
		if not symbol:
			return failure("Parameter `symbol` is required for this operation")
		if not mt5.symbol_select(symbol, True):
			return failure(f"Invalid symbol or failed to select {symbol}")
		symbol_info = mt5.symbol_info(symbol)
		if symbol_info is None:
			return failure(f"Failed to get symbol info for {symbol}")
		tick = mt5.symbol_info_tick(symbol)
		if tick is None or not tick.bid or not tick.ask:
			return failure(f"No current quote for {symbol}")
		volume, error = validate_volume(symbol_info, volume)
		if error:
			return failure(error)
		if price:
			price, error = normalize_price(symbol_info, price)
			if error:
				return failure(error)
		else:
			price = tick.ask if order_type == OrderType.BUY.value else tick.bid
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
			"sl": 0.0,
			"tp": 0.0,
			"deviation": int(deviation if deviation is not None else 20),
			"magic": int(resolved_magic or 0),
			"comment": comment,
			"type_time": OrderTime.GTC.value,
			"type_filling": filling,
			"position": int(position),
		}
		if request_id is None:
			return _send(request)
		registry = get_request_registry(connection)
		request["comment"] = build_comment(comment, request_tag(request_id))
		request_id, early = _begin(registry, request_id, request, kind="close", strategy_id=strategy_id, magic=request["magic"])
		if early:
			return early
		return _send_tracked(registry, request_id, request)

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
