#!/usr/bin/env python3
import os
import argparse
import asyncio
import logging
from importlib import metadata
from pathlib import Path
from dotenv import load_dotenv

from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.exceptions import ToolError
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Callable, List, Literal, Optional, Union

from metatrader_client.metrics import metrics
from metatrader_client.requests_registry import new_request_id
from metatrader_client.serialization import to_plain
from metatrader_mcp.bridge import Bridge, BridgeError
from metatrader_mcp.logging_config import logging_state
from metatrader_mcp.utils import init, get_client

logger = logging.getLogger(__name__)


def _to_plain(value: Any) -> Any:
	"""Convert MT5 named tuples (order_send results and requests) to dictionaries for JSON output."""
	return to_plain(value)

# ────────────────────────────────────────────────────────────────────────────────
# 1) Lifespan context definition
# ────────────────────────────────────────────────────────────────────────────────
@dataclass
class AppContext:
	client: Optional[Any]
	bridge: Optional[Bridge] = None
	supervisor: Optional[Any] = None

def _supervisor_enabled() -> bool:
	return os.getenv("MT5_SUPERVISOR_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}

@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:

	client = init(
		os.getenv("login"),
		os.getenv("password"),
		os.getenv("server"),
		os.getenv("MT5_PATH"),
		raise_on_failure=False,
	)
	bridge = Bridge(client)
	supervisor = None
	if client is not None and _supervisor_enabled():
		from metatrader_client.supervisor import PositionSupervisor

		loop = asyncio.get_running_loop()
		supervisor = PositionSupervisor(client.connection)
		supervisor.start(
			lambda cycle: asyncio.run_coroutine_threadsafe(bridge.run("position_supervisor", "send", cycle), loop).result()
		)
	try:
		yield AppContext(client=client, bridge=bridge, supervisor=supervisor)
	finally:
		if supervisor is not None:
			supervisor.stop()
		bridge.shutdown()
		if client is not None:
			client.disconnect()

# ────────────────────────────────────────────────────────────────────────────────
# 2) Instantiate FastMCP as `mcp` (must be named `mcp`, `server`, or `app`)
# ────────────────────────────────────────────────────────────────────────────────
mcp = FastMCP(
	"metatrader",
	instructions=(
		"Trading tools for a MetaTrader 5 account. Every trading tool returns `error`, `message`, "
		"`status` and `retcode`: only `error: false` means the request was executed or accepted. "
		"`status` is 'done', 'partial', 'placed', 'no_changes', 'rejected' or 'no_response'; "
		"`verification` and `checks` say what was confirmed in the terminal afterwards and what is still unknown. "
		"Pass a new `request_id` for every new trading intention and reuse it only to retry the same intention: "
		"a repeated request_id never sends a second order. After 'no_response', a timeout or an unknown verification, "
		"call get_request_status / reconcile_request before anything else. "
		"Use get_trading_snapshot for the account and market state in one call, and preview_order before sending "
		"(send with its preview_id). Bulk tools need an explicit scope; use dry_run to list the affected tickets first. "
		"Risk limits configured on the server can reject requests; do not try to bypass them."
	),
	lifespan=app_lifespan,
	dependencies=[],
)

# ────────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────────

def _app(ctx: Context) -> AppContext:
	return ctx.request_context.lifespan_context

def _bridge(ctx: Context) -> Bridge:
	app = _app(ctx)
	if app.bridge is None:
		app.bridge = Bridge(app.client)
	return app.bridge

async def _read(ctx: Context, tool: str, function: Callable[[Any], Any], stage: str = "read") -> Any:
	"""Run a read on the bridge; bridge errors become tool errors naming the stage and known state."""
	try:
		return await _bridge(ctx).run(tool, stage, lambda: function(get_client(ctx)))
	except BridgeError as exc:
		raise ToolError(f"[stage: {exc.stage}, state: {exc.state}] {exc}") from None

async def _trade(ctx: Context, tool: str, function: Callable[[Any], Any], *, request_id: Optional[str] = None) -> dict:
	"""Run a trading call on the bridge; a deadline or bridge failure is reported in the trading result format."""
	try:
		return _to_plain(await _bridge(ctx).run(tool, "send", lambda: function(get_client(ctx))))
	except BridgeError as exc:
		uncertain = exc.state == "unknown"
		return {
			"error": True,
			"status": "no_response" if uncertain else "not_sent",
			"retcode": None,
			"stage": exc.stage,
			"state": exc.state,
			"verification": "unknown" if uncertain else None,
			"request_id": request_id,
			"message": str(exc),
			"data": None,
		}

def _csv(df: Any) -> str:
	return df.to_csv() if hasattr(df, 'to_csv') else str(df)

def _build_info() -> dict:
	"""Package version and, when running from a git checkout, the commit (no subprocess is started)."""
	try:
		version = metadata.version("metatrader-mcp-server")
	except metadata.PackageNotFoundError:
		version = None
	commit = os.getenv("MCP_BUILD_COMMIT")
	if not commit:
		git = Path(__file__).resolve().parents[2] / ".git"
		try:
			head = (git / "HEAD").read_text().strip()
			if head.startswith("ref: "):
				ref = git / head[5:]
				commit = ref.read_text().strip() if ref.exists() else None
				if commit is None and (git / "packed-refs").exists():
					for line in (git / "packed-refs").read_text().splitlines():
						if line.endswith(head[5:]):
							commit = line.split()[0]
			else:
				commit = head
		except OSError:
			commit = None
	return {"version": version, "commit": commit}

def _mask(login: Any) -> Optional[str]:
	if login in (None, ""):
		return None
	text = str(login)
	return "*" * max(0, len(text) - 3) + text[-3:]

# ────────────────────────────────────────────────────────────────────────────────
# 3) Register tools with @mcp.tool()
# ────────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_bridge_health(ctx: Context, probe: bool = False) -> dict:
	"""
	Health of this MCP server and its MetaTrader 5 bridge, answered without waiting for the terminal:
	version/commit, account reference (masked login and server), connectivity, the call in progress or
	blocked past its deadline, reconnection attempts, latency percentiles per stage (read, preview, send,
	broker_send, confirm), recent errors, logging degradation and the position supervisor state.
	probe=True also asks the terminal for its state (subject to the read deadline). Contains no secrets.
	"""
	app = _app(ctx)
	bridge = _bridge(ctx)
	connection = getattr(app.client, "connection", None) if app.client is not None else None
	health = {
		"build": _build_info(),
		"configured": app.client is not None,
		"account_reference": {
			"login": _mask(getattr(connection, "login", None)),
			"server": getattr(connection, "server", None),
		},
		"bridge": bridge.state(),
		"metrics": metrics.snapshot(),
		"logging": logging_state.snapshot(),
		"supervisor": app.supervisor.state() if app.supervisor is not None else {"running": False},
	}
	if probe and app.client is not None:
		try:
			terminal = await bridge.run("get_bridge_health", "read", lambda: app.client.get_terminal_info())
			health["probe"] = {"ok": True, "terminal": _to_plain(terminal)}
		except Exception as exc:
			health["probe"] = {"ok": False, "error": str(exc)}
	return health

@mcp.tool()
async def get_account_info(ctx: Context) -> dict:
	"""Get account information (balance, equity, profit, margin level, free margin, account type, leverage, currency)."""
	return await _read(ctx, "get_account_info", lambda client: client.account.get_trade_statistics())

@mcp.tool()
async def get_trading_snapshot(ctx: Context, symbol: str, strategy_id: Optional[str] = None) -> dict:
	"""
	One call with the quote (bid, ask, spread, tick time and age), account state and permissions, positions and
	pending orders of the symbol with floating P&L, and the effective risk limits and their current use.
	Each component has captured_at/source; unreadable components have status 'error' (never empty lists or zero P&L).
	The reads are successive, not atomic. Orders revalidate the quote and the limits when sent.
	"""
	return _to_plain(await _read(ctx, "get_trading_snapshot", lambda client: client.order.get_trading_snapshot(symbol, strategy_id=strategy_id)))

@mcp.tool()
async def get_deals(ctx: Context, from_date: Optional[str] = None, to_date: Optional[str] = None, symbol: Optional[str] = None) -> str:
	"""Get historical deals as CSV. Date input in format: 'YYYY-MM-DD'."""
	return _csv(await _read(ctx, "get_deals", lambda client: client.history.get_deals_as_dataframe(from_date=from_date, to_date=to_date, group=symbol)))

@mcp.tool()
async def get_orders(ctx: Context, from_date: Optional[str] = None, to_date: Optional[str] = None, symbol: Optional[str] = None) -> str:
	"""Get historical orders as CSV. Date input in format: 'YYYY-MM-DD'"""
	return _csv(await _read(ctx, "get_orders", lambda client: client.history.get_orders_as_dataframe(from_date=from_date, to_date=to_date, group=symbol)))

@mcp.tool()
async def get_candles_by_date(ctx: Context, symbol_name: str, timeframe: str, from_date: str = None, to_date: str = None) -> str:
	"""Get candle data for a symbol in a given timeframe and date range as CSV. Date input in format: ISO 8601 or 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'."""
	return _csv(await _read(ctx, "get_candles_by_date", lambda client: client.market.get_candles_by_date(symbol_name=symbol_name, timeframe=timeframe, from_date=from_date, to_date=to_date)))

@mcp.tool()
async def get_candles_latest(ctx: Context, symbol_name: str, timeframe: str, count: int = 100) -> str:
	"""Get the latest N candles for a symbol and timeframe as CSV (newest first). Prefer get_candles for analysis."""
	return _csv(await _read(ctx, "get_candles_latest", lambda client: client.market.get_candles_latest(symbol_name=symbol_name, timeframe=timeframe, count=count)))

@mcp.tool()
async def get_candles(
	ctx: Context,
	symbol: str,
	timeframe: str,
	count: int = 100,
	since: Optional[Union[int, str]] = None,
	closed_only: bool = False,
	limit: Optional[int] = None,
	format: Literal["json", "csv"] = "json",
) -> Union[dict, str]:
	"""
	Candles as structured JSON (schema candles/v1): ascending order, `closed` per bar, declared time basis
	(UTC when the server offset is configured), gaps, volume semantics. closed_only=true excludes the forming bar.
	For incremental updates pass since=<next_since of the previous response>: only newer bars are returned and the
	forming bar is returned again each time (replace it). format='csv' returns the bars as CSV.
	"""
	from metatrader_client.market import candles_to_csv

	result = await _read(ctx, "get_candles", lambda client: client.market.get_candles(
		symbol=symbol, timeframe=timeframe, count=count, since=since, closed_only=closed_only, limit=limit,
	))
	if format == "csv" and not result.get("error"):
		return candles_to_csv(result["data"])
	return _to_plain(result)

@mcp.tool()
async def get_candles_multi(ctx: Context, symbol: str, timeframes: List[str], count: int = 100, closed_only: bool = False) -> dict:
	"""
	Candles of several timeframes (e.g. ['M1','M5','H4']) in one call, as get_candles JSON per timeframe.
	`errors` lists the timeframes that failed; `error` is true if any failed, the others are still returned.
	"""
	return _to_plain(await _read(ctx, "get_candles_multi", lambda client: client.market.get_candles_multi(
		symbol=symbol, timeframes=timeframes, count=count, closed_only=closed_only,
	)))

@mcp.tool()
async def get_symbol_price(ctx: Context, symbol_name: str) -> dict:
	"""Get the latest price info for a symbol as a dictionary."""
	return await _read(ctx, "get_symbol_price", lambda client: client.market.get_symbol_price(symbol_name=symbol_name))

@mcp.tool()
async def get_symbol_specifications(ctx: Context, symbol: str) -> dict:
	"""
	Broker specification of a symbol: digits, point and tick size/value, contract size, volume min/max/step,
	stop and freeze distances, trade/execution/filling/order/expiration modes and permissions. Values are the
	broker's for this exact symbol (XAUUSDm may differ from XAUUSD); unreported attributes are listed in `unavailable`.
	"""
	return _to_plain(await _read(ctx, "get_symbol_specifications", lambda client: client.market.get_symbol_specifications(symbol)))

@mcp.tool()
async def get_all_symbols(ctx: Context) -> list:
	"""Get a list of all available market symbols."""
	return await _read(ctx, "get_all_symbols", lambda client: client.market.get_symbols())

@mcp.tool()
async def get_symbols(ctx: Context, group: Optional[str] = None) -> list:
	"""
	Get a list of available market symbols. Filter symbols by group pattern (e.g., '*USD*').
	"""
	return await _read(ctx, "get_symbols", lambda client: client.market.get_symbols(group=group))

# ────────────────────────────────────────────────────────────────────────────────
# Pre-trade calculations
# ────────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def calculate_trade_outcome(ctx: Context, symbol: str, type: str, volume: float, entry_price: float, exit_price: float) -> dict:
	"""Estimated profit/loss in the account currency (order_calc_profit) for a trade from entry_price to exit_price. Excludes commission, swap and slippage."""
	return _to_plain(await _read(ctx, "calculate_trade_outcome", lambda client: client.order.calculate_trade_outcome(
		symbol=symbol, type=type, volume=volume, entry_price=entry_price, exit_price=exit_price,
	), stage="preview"))

@mcp.tool()
async def calculate_required_margin(ctx: Context, symbol: str, type: str, volume: float, price: Optional[float] = None) -> dict:
	"""Estimated margin in the account currency (order_calc_margin); price defaults to the current quote."""
	return _to_plain(await _read(ctx, "calculate_required_margin", lambda client: client.order.calculate_required_margin(
		symbol=symbol, type=type, volume=volume, price=price,
	), stage="preview"))

@mcp.tool()
async def preview_order(
	ctx: Context,
	symbol: str,
	type: str,
	volume: Optional[float] = None,
	risk_amount: Optional[float] = None,
	price: Optional[float] = None,
	stop_loss: Optional[float] = None,
	take_profit: Optional[float] = None,
	strategy_id: Optional[str] = None,
) -> dict:
	"""
	Check an opening order without sending it: normalized request, entry, volume, loss at SL and profit at TP
	(account currency), reward/risk, margin, exposure, risk limits and the broker's order_check.
	Pass volume, or risk_amount (max loss at the SL) to size the volume down to a valid step.
	price makes it a pending order. When ready, send it with send_with.arguments (includes preview_id) before
	expires_at; a material change or a price move beyond max_price_move_points rejects the order.
	A preview reserves no price and does not guarantee execution.
	"""
	return _to_plain(await _read(ctx, "preview_order", lambda client: client.order.preview_order(
		symbol=symbol, type=type, volume=volume, risk_amount=risk_amount, price=price,
		stop_loss=stop_loss, take_profit=take_profit, strategy_id=strategy_id,
	), stage="preview"))

# ────────────────────────────────────────────────────────────────────────────────
# Order module tools
# ────────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_all_positions(ctx: Context) -> str:
	"""Get all open positions as CSV."""
	return _csv(await _read(ctx, "get_all_positions", lambda client: client.order.get_all_positions()))

@mcp.tool()
async def get_positions_by_symbol(ctx: Context, symbol: str) -> str:
	"""Get open positions for a specific symbol."""
	return _csv(await _read(ctx, "get_positions_by_symbol", lambda client: client.order.get_positions_by_symbol(symbol=symbol)))

@mcp.tool()
async def get_positions_by_id(ctx: Context, id: Union[int, str]) -> str:
	"""Get open positions by ID."""
	return _csv(await _read(ctx, "get_positions_by_id", lambda client: client.order.get_positions_by_id(id=id)))

@mcp.tool()
async def get_all_pending_orders(ctx: Context) -> str:
	"""Get all pending orders."""
	return _csv(await _read(ctx, "get_all_pending_orders", lambda client: client.order.get_all_pending_orders()))

@mcp.tool()
async def get_pending_orders_by_symbol(ctx: Context, symbol: str) -> str:
	"""Get pending orders for a specific symbol."""
	return _csv(await _read(ctx, "get_pending_orders_by_symbol", lambda client: client.order.get_pending_orders_by_symbol(symbol=symbol)))

@mcp.tool()
async def get_pending_orders_by_id(ctx: Context, id: Union[int, str]) -> str:
	"""Get pending orders by id."""
	return _csv(await _read(ctx, "get_pending_orders_by_id", lambda client: client.order.get_pending_orders_by_id(id=id)))

@mcp.tool()
async def place_market_order(
	ctx: Context,
	symbol: str,
	volume: float,
	type: str,
	stop_loss: Optional[float] = None,
	take_profit: Optional[float] = None,
	request_id: Optional[str] = None,
	strategy_id: Optional[str] = None,
	preview_id: Optional[str] = None,
) -> dict:
	"""
	Place a market order. Parameters:
		symbol: Symbol name (e.g., 'EURUSD')
		volume: Lot size. (e.g. 1.5)
		type: Order type ('BUY' or 'SELL')
		stop_loss (optional): Stop loss price, sent with the order.
		take_profit (optional): Take profit price, sent with the order.
		request_id (optional): ID of this trading intention. Reuse it only to retry the same intention; a repeated
			ID returns the recorded result and never sends again. Generated (and returned) when omitted.
		strategy_id (optional): Strategy owning the order (sets its magic number and strategy limits).
		preview_id (optional): From preview_order; the order must match the preview.
	The result includes verification ('confirmed', 'partial', 'pending', 'protection_missing', 'mismatch',
	'unknown'), checks, execution (fill, slippage, timings) and protection (requested vs effective SL/TP).
	"""
	request_id = request_id or new_request_id()
	return await _trade(ctx, "place_market_order", lambda client: client.order.place_market_order(
		symbol=symbol, volume=volume, type=type, stop_loss=stop_loss, take_profit=take_profit,
		request_id=request_id, strategy_id=strategy_id, preview_id=preview_id,
	), request_id=request_id)

@mcp.tool()
async def place_pending_order(
	ctx: Context,
	symbol: str,
	volume: float,
	type: str,
	price: float,
	stop_loss: Optional[float] = None,
	take_profit: Optional[float] = None,
	request_id: Optional[str] = None,
	strategy_id: Optional[str] = None,
	preview_id: Optional[str] = None,
) -> dict:
	"""
	Place a pending order. Parameters:
		symbol: Symbol name (e.g., 'EURUSD')
		volume: Lot size. (e.g. 1.5)
		type: Order type ('BUY', 'SELL').
		price: Pending order price.
		stop_loss (optional): Stop loss price.
		take_profit (optional): Take profit price.
		request_id / strategy_id / preview_id (optional): as in place_market_order.
	"""
	request_id = request_id or new_request_id()
	return await _trade(ctx, "place_pending_order", lambda client: client.order.place_pending_order(
		symbol=symbol, volume=volume, type=type, price=price, stop_loss=stop_loss, take_profit=take_profit,
		request_id=request_id, strategy_id=strategy_id, preview_id=preview_id,
	), request_id=request_id)

@mcp.tool()
async def modify_position(ctx: Context, id: Union[int, str], stop_loss: Optional[Union[int, float]] = None, take_profit: Optional[Union[int, float]] = None, strategy_id: Optional[str] = None) -> dict:
	"""
	Modify the stop loss and/or take profit of an open position by ID. Omitted values are kept; 0 removes a level.
	The position is read again: only verification 'confirmed' returns error false. With strategy_id, positions of
	other strategies or manual trading are not changed.
	"""
	return await _trade(ctx, "modify_position", lambda client: client.order.modify_position(id=id, stop_loss=stop_loss, take_profit=take_profit, strategy_id=strategy_id))

@mcp.tool()
async def modify_pending_order(ctx: Context, id: Union[int, str], price: Optional[Union[int, float]] = None, stop_loss: Optional[Union[int, float]] = None, take_profit: Optional[Union[int, float]] = None, strategy_id: Optional[str] = None) -> dict:
	"""Modify the price, stop loss and/or take profit of a pending order by ID. Omitted values are kept; 0 removes a level. Verified like modify_position."""
	return await _trade(ctx, "modify_pending_order", lambda client: client.order.modify_pending_order(id=id, price=price, stop_loss=stop_loss, take_profit=take_profit, strategy_id=strategy_id))

@mcp.tool()
async def close_position(ctx: Context, id: Union[int, str], request_id: Optional[str] = None, strategy_id: Optional[str] = None) -> dict:
	"""
	Close an open position by ID. verification: 'confirmed', 'remaining' (with remaining_volume), 'unknown' or
	'not_closed'; only 'confirmed' returns error false. A repeated request_id never sends another close.
	"""
	return await _trade(ctx, "close_position", lambda client: client.order.close_position(id=id, request_id=request_id, strategy_id=strategy_id), request_id=request_id)

@mcp.tool()
async def cancel_pending_order(ctx: Context, id: Union[int, str], strategy_id: Optional[str] = None) -> dict:
	"""Cancel a pending order by ID. verification: 'confirmed', 'remaining', 'unknown' or 'not_cancelled'."""
	return await _trade(ctx, "cancel_pending_order", lambda client: client.order.cancel_pending_order(id=id, strategy_id=strategy_id))

Scope = Literal["strategy", "manual", "all"]

@mcp.tool()
async def close_all_positions(ctx: Context, scope: Scope, strategy_id: Optional[str] = None, dry_run: bool = False) -> dict:
	"""Close all open positions in the scope. Returns the result of every close and the positions still open. scope is required: 'strategy' (only items with the magic of strategy_id), 'manual' (magic 0) or 'all'. dry_run=true lists the affected tickets without sending anything."""
	return await _trade(ctx, "close_all_positions", lambda client: client.order.close_all_positions(scope=scope, strategy_id=strategy_id, dry_run=dry_run))

@mcp.tool()
async def close_all_positions_by_symbol(ctx: Context, symbol: str, scope: Scope, strategy_id: Optional[str] = None, dry_run: bool = False) -> dict:
	"""Close all open positions for a specific symbol in the scope. scope is required: 'strategy' (only items with the magic of strategy_id), 'manual' (magic 0) or 'all'. dry_run=true lists the affected tickets without sending anything."""
	return await _trade(ctx, "close_all_positions_by_symbol", lambda client: client.order.close_all_positions_by_symbol(symbol=symbol, scope=scope, strategy_id=strategy_id, dry_run=dry_run))

@mcp.tool()
async def close_all_profitable_positions(ctx: Context, scope: Scope, strategy_id: Optional[str] = None, dry_run: bool = False) -> dict:
	"""Close all profitable positions in the scope. scope is required: 'strategy' (only items with the magic of strategy_id), 'manual' (magic 0) or 'all'. dry_run=true lists the affected tickets without sending anything."""
	return await _trade(ctx, "close_all_profitable_positions", lambda client: client.order.close_all_profitable_positions(scope=scope, strategy_id=strategy_id, dry_run=dry_run))

@mcp.tool()
async def close_all_losing_positions(ctx: Context, scope: Scope, strategy_id: Optional[str] = None, dry_run: bool = False) -> dict:
	"""Close all losing positions in the scope. scope is required: 'strategy' (only items with the magic of strategy_id), 'manual' (magic 0) or 'all'. dry_run=true lists the affected tickets without sending anything."""
	return await _trade(ctx, "close_all_losing_positions", lambda client: client.order.close_all_losing_positions(scope=scope, strategy_id=strategy_id, dry_run=dry_run))

@mcp.tool()
async def cancel_all_pending_orders(ctx: Context, scope: Scope, strategy_id: Optional[str] = None, dry_run: bool = False) -> dict:
	"""Cancel all pending orders in the scope. scope is required: 'strategy' (only items with the magic of strategy_id), 'manual' (magic 0) or 'all'. dry_run=true lists the affected tickets without sending anything."""
	return await _trade(ctx, "cancel_all_pending_orders", lambda client: client.order.cancel_all_pending_orders(scope=scope, strategy_id=strategy_id, dry_run=dry_run))

@mcp.tool()
async def cancel_pending_orders_by_symbol(ctx: Context, symbol: str, scope: Scope, strategy_id: Optional[str] = None, dry_run: bool = False) -> dict:
	"""Cancel all pending orders for a specific symbol in the scope. scope is required: 'strategy' (only items with the magic of strategy_id), 'manual' (magic 0) or 'all'. dry_run=true lists the affected tickets without sending anything."""
	return await _trade(ctx, "cancel_pending_orders_by_symbol", lambda client: client.order.cancel_pending_orders_by_symbol(symbol=symbol, scope=scope, strategy_id=strategy_id, dry_run=dry_run))

# ────────────────────────────────────────────────────────────────────────────────
# Request identity and risk limits
# ────────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_request_status(ctx: Context, request_id: str) -> dict:
	"""Recorded state of a request_id (prepared, sending, accepted, partially_filled, filled, rejected, unknown, reconciled) with its events and tickets."""
	return _to_plain(await _read(ctx, "get_request_status", lambda client: client.order.get_request_status(request_id)))

@mcp.tool()
async def list_requests(ctx: Context, state: Optional[str] = None, limit: int = 50) -> dict:
	"""Most recent tracked trade requests, optionally only one state (e.g. 'unknown')."""
	return _to_plain(await _read(ctx, "list_requests", lambda client: client.order.list_requests(state=state, limit=limit)))

@mcp.tool()
async def reconcile_request(ctx: Context, request_id: str) -> dict:
	"""
	Search the terminal (orders, positions, history) for the effects of a request with an unknown or partial outcome
	and update its state. Never sends anything. If nothing is found it stays unknown.
	"""
	return _to_plain(await _read(ctx, "reconcile_request", lambda client: client.order.reconcile_request(request_id)))

@mcp.tool()
async def resolve_request(ctx: Context, request_id: str, decision: Literal["not_executed", "accept_unknown"], reason: str) -> dict:
	"""
	Record an explicit, audited decision for a request whose outcome stays unknown after reconcile_request, which
	unblocks new orders on its symbol. Only use it after checking the account; never sends or cancels anything.
	"""
	return _to_plain(await _read(ctx, "resolve_request", lambda client: client.order.resolve_request(request_id, decision=decision, reason=reason)))

@mcp.tool()
async def get_risk_limits(ctx: Context) -> dict:
	"""Effective risk limits (account and per strategy), the new entries switch, and the history of their changes."""
	return _to_plain(await _read(ctx, "get_risk_limits", lambda client: client.order.get_risk_limits()))

@mcp.tool()
async def disable_new_entries(ctx: Context, reason: str) -> dict:
	"""
	Block every new opening (market or pending) until the operator enables entries again. Closing, cancelling
	and modifying stops remain available. Re-enabling is not available as a tool.
	"""
	return _to_plain(await _read(ctx, "disable_new_entries", lambda client: client.order.disable_new_entries(reason=reason, source="mcp")))

# ────────────────────────────────────────────────────────────────────────────────
# Position supervisor
# ────────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def create_position_rule(
	ctx: Context,
	operator: Literal[">=", "<="],
	threshold: float,
	ticket: Optional[int] = None,
	strategy_id: Optional[str] = None,
	symbol: Optional[str] = None,
	metric: Literal["pnl", "profit"] = "pnl",
) -> dict:
	"""
	Create a persistent rule evaluated locally every few seconds: close the position `ticket` (or every position of
	`strategy_id`, optionally only `symbol`) when its floating `metric` (pnl = profit + swap, account currency)
	is `operator` `threshold`. Only create rules the user asked for. Stale or unreadable P&L is not evaluated; an
	uncertain close becomes pending_reconciliation and is never retried. Broker SL/TP remain the independent protection.
	"""
	return _to_plain(await _read(ctx, "create_position_rule", lambda client: client.supervisor.create_position_rule(
		operator=operator, threshold=threshold, ticket=ticket, strategy_id=strategy_id, symbol=symbol, metric=metric,
	)))

@mcp.tool()
async def list_position_rules(ctx: Context, state: Optional[str] = None, include_events: bool = False) -> dict:
	"""Position rules with state (active, paused, triggered, executing, fulfilled, failed, pending_reconciliation, target_closed, removed), last observed value and result."""
	return _to_plain(await _read(ctx, "list_position_rules", lambda client: client.supervisor.list_position_rules(state=state, include_events=include_events)))

@mcp.tool()
async def pause_position_rule(ctx: Context, rule_id: str) -> dict:
	"""Pause an active rule (it is kept and can be resumed)."""
	return _to_plain(await _read(ctx, "pause_position_rule", lambda client: client.supervisor.pause_position_rule(rule_id)))

@mcp.tool()
async def resume_position_rule(ctx: Context, rule_id: str) -> dict:
	"""Resume a paused rule."""
	return _to_plain(await _read(ctx, "resume_position_rule", lambda client: client.supervisor.resume_position_rule(rule_id)))

@mcp.tool()
async def remove_position_rule(ctx: Context, rule_id: str) -> dict:
	"""Remove a rule (kept as 'removed' in the audit trail). A rule that is executing cannot be removed."""
	return _to_plain(await _read(ctx, "remove_position_rule", lambda client: client.supervisor.remove_position_rule(rule_id)))

if __name__ == "__main__":
	load_dotenv()
	from metatrader_mcp.logging_config import configure_logging
	from metatrader_mcp.utils import resolve_transport_config, run_mcp

	parser = argparse.ArgumentParser(description="MetaTrader MCP Server")
	parser.add_argument("--login",    type=str, help="MT5 login")
	parser.add_argument("--password", type=str, help="MT5 password")
	parser.add_argument("--server",   type=str, help="MT5 server name")
	parser.add_argument("--path",     type=str, help="Path to MT5 terminal executable (optional)")
	parser.add_argument("--transport", type=str, choices=["sse", "stdio", "streamable-http"], default=None, help="MCP transport type (default: stdio, env: MCP_TRANSPORT)")
	parser.add_argument("--host",     type=str, default=None, help="Host to bind for SSE/HTTP transport (default: 127.0.0.1, env: MCP_HOST)")
	parser.add_argument("--port",     type=int, default=None, help="Port to bind for SSE/HTTP transport (default: 8080, env: MCP_PORT)")

	args = parser.parse_args()

	# inject into lifespan via env vars
	if args.login:    os.environ["login"]    = args.login
	if args.password: os.environ["password"] = args.password
	if args.server:   os.environ["server"]   = args.server
	if args.path:     os.environ["MT5_PATH"] = args.path

	configure_logging()
	transport, host, port = resolve_transport_config(args.transport, args.host, args.port)

	# run the MCP server (must call mcp.run)
	try:
		run_mcp(mcp, transport, host, port)
	except ValueError as exc:
		parser.error(str(exc))
