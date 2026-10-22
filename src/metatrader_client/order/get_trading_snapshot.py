"""
Account and market snapshot for one symbol, in a single call.
"""

import time
from typing import Any, Callable, Dict, Optional

import MetaTrader5 as mt5

from ..risk import bound_risk_guard, entries_block_reason
from ..store import get_store
from ..strategy import ownership, strategy_magic, validate_strategy_id
from ..timebase import age_seconds, describe_server_time, iso_utc, server_utc_offset
from ._validation import _point

ACCOUNT_TRADE_MODES = {0: "demo", 1: "contest", 2: "real"}


class _Recorder:
    """Times each component read and counts the terminal calls."""

    def __init__(self):
        self.calls = 0
        self.errors = []

    def read(self, name: str, function: Callable[[], Any]):
        started = time.time()
        self.calls += 1
        try:
            value = function()
            error = None
        except Exception as exc:  # a component failing must not hide the others
            value, error = None, f"{type(exc).__name__}: {exc}"
        if value is None and error is None:
            code, description = mt5.last_error()
            error = f"error {code}: {description}"
        meta = {"captured_at": iso_utc(started), "duration_ms": round((time.time() - started) * 1000, 3), "source": "mt5"}
        if error:
            self.errors.append({"component": name, "error": error})
        return value, error, meta


def _position(p, magic) -> Dict[str, Any]:
    return {
        "ticket": p.ticket,
        "type": "BUY" if p.type == 0 else "SELL",
        "volume": p.volume,
        "price_open": p.price_open,
        "price_current": getattr(p, "price_current", None),
        "sl": p.sl or None,
        "tp": p.tp or None,
        "profit": p.profit,
        "swap": getattr(p, "swap", None),
        "magic": int(getattr(p, "magic", 0) or 0),
        "ownership": ownership(p, magic),
        "comment": getattr(p, "comment", None),
    }


def _order(o, magic) -> Dict[str, Any]:
    from ..types import OrderType
    return {
        "ticket": o.ticket,
        "type": OrderType.to_string(o.type),
        "volume": o.volume_current,
        "price": o.price_open,
        "sl": o.sl or None,
        "tp": o.tp or None,
        "magic": int(getattr(o, "magic", 0) or 0),
        "ownership": ownership(o, magic),
        "comment": getattr(o, "comment", None),
    }


def get_trading_snapshot(connection, *, symbol: str, strategy_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Quote, account, positions, pending orders and risk limits for one symbol.

    The components are successive reads, not an atomic broker transaction:
    each one has its own ``captured_at`` and the snapshot reports when the
    capture started and finished. A component that cannot be read is reported
    with ``status: "error"`` and ``None`` values; it is never replaced by an
    empty list or a zero P&L. Account values come only from MT5.

    Args:
        connection: MetaTrader 5 connection object.
        symbol: Broker symbol.
        strategy_id: Optional strategy, to classify ownership and show its limits.

    Returns:
        dict: {"error": bool, "message": str, "data": {...}}. ``error`` is True
        when any component failed (``data.errors`` lists them); the readable
        components are still returned. Use the snapshot for analysis; orders
        revalidate the quote and the risk state when they are sent.
    """
    error = validate_strategy_id(strategy_id)
    if error:
        return {"error": True, "message": error, "data": None}
    magic = strategy_magic(strategy_id) if strategy_id else None
    offset = server_utc_offset()
    started = time.time()
    rec = _Recorder()

    # Quote
    mt5.symbol_select(symbol, True)
    info, info_error, info_meta = rec.read("symbol_info", lambda: mt5.symbol_info(symbol))
    tick, tick_error, tick_meta = rec.read("tick", lambda: mt5.symbol_info_tick(symbol))
    if tick is not None and (not tick.bid or not tick.ask):
        tick_error = "the tick has no bid/ask"
        rec.errors.append({"component": "tick", "error": tick_error})
    if tick_error or tick is None:
        quote = {"status": "error", "error": tick_error, **tick_meta}
    else:
        point = _point(info) if info is not None else None
        quote = {
            "status": "ok",
            "bid": tick.bid,
            "ask": tick.ask,
            "spread_price": round(tick.ask - tick.bid, 10),
            "spread_points": round((tick.ask - tick.bid) / point, 1) if point else None,
            **describe_server_time(getattr(tick, "time", None), offset=offset),
            "age_seconds": age_seconds(getattr(tick, "time", None), offset=offset),
            "age_basis": "local clock vs tick time (MT5_SERVER_UTC_OFFSET_SECONDS)" if offset is not None else "unknown: set MT5_SERVER_UTC_OFFSET_SECONDS to compute it",
            **tick_meta,
        }

    # Account
    account, account_error, account_meta = rec.read("account", lambda: mt5.account_info())
    terminal, terminal_error, terminal_meta = rec.read("terminal", lambda: mt5.terminal_info())
    if account is None:
        account_data = {"status": "error", "error": account_error, **account_meta}
    else:
        account_data = {
            "status": "ok",
            "login": getattr(account, "login", None),
            "server": getattr(account, "server", None),
            "trade_mode": ACCOUNT_TRADE_MODES.get(getattr(account, "trade_mode", None), getattr(account, "trade_mode", None)),
            "currency": account.currency,
            "balance": getattr(account, "balance", None),
            "equity": account.equity,
            "margin": getattr(account, "margin", None),
            "margin_free": getattr(account, "margin_free", None),
            "margin_level": getattr(account, "margin_level", None),
            "leverage": getattr(account, "leverage", None),
            "account_trade_allowed": getattr(account, "trade_allowed", None),
            "expert_trade_allowed": getattr(account, "trade_expert", None),
            "terminal_trade_allowed": None if terminal is None else getattr(terminal, "trade_allowed", None),
            "terminal_connected": None if terminal is None else getattr(terminal, "connected", None),
            "scope": "account",
            **account_meta,
        }
    currency = getattr(account, "currency", None) if account is not None else None

    # Positions and pending orders of the symbol
    positions, positions_error, positions_meta = rec.read("positions", lambda: mt5.positions_get(symbol=symbol))
    if positions is None:
        positions_data = {"status": "error", "error": positions_error, "items": None, "floating_pnl": None, **positions_meta}
    else:
        floating = sum(float(p.profit) + float(getattr(p, "swap", 0) or 0) for p in positions)
        positions_data = {
            "status": "ok",
            "items": [_position(p, magic) for p in positions],
            "count": len(positions),
            "volume": round(sum(p.volume for p in positions), 8),
            "floating_pnl": round(floating, 2),
            "floating_pnl_includes": ["profit", "swap"],
            "currency": currency,
            "scope": f"open positions of {symbol}",
            **positions_meta,
        }
        if magic:
            own = [p for p in positions if int(getattr(p, "magic", 0) or 0) == magic]
            positions_data["strategy_floating_pnl"] = round(sum(float(p.profit) + float(getattr(p, "swap", 0) or 0) for p in own), 2)
    orders, orders_error, orders_meta = rec.read("orders", lambda: mt5.orders_get(symbol=symbol))
    if orders is None:
        orders_data = {"status": "error", "error": orders_error, "items": None, **orders_meta}
    else:
        orders_data = {
            "status": "ok",
            "items": [_order(o, magic) for o in orders],
            "count": len(orders),
            "volume": round(sum(o.volume_current for o in orders), 8),
            "scope": f"pending orders of {symbol}",
            **orders_meta,
        }

    # Limits and current use
    guard = bound_risk_guard(connection, mt5)
    cfg = guard.config
    limits = {
        "status": "ok",
        "effective": cfg.as_dict(),
        "strategy": cfg.limits_for(strategy_id) if strategy_id else None,
        "new_entries_block": entries_block_reason(cfg, get_store(connection)),
    }
    all_positions, all_pos_error, _ = rec.read("all_positions", lambda: mt5.positions_get())
    all_orders, all_ord_error, _ = rec.read("all_orders", lambda: mt5.orders_get())
    if all_positions is None or all_orders is None:
        limits["usage"] = {"status": "unknown", "error": all_pos_error or all_ord_error}
    else:
        reserved = guard._unlisted_reservations(all_positions, all_orders)
        total_volume = sum(p.volume for p in all_positions) + sum(o.volume_current for o in all_orders) + sum(v for v, _, _ in reserved)
        usage = {
            "status": "ok",
            "open_positions": len(all_positions),
            "pending_orders": len(all_orders),
            "unlisted_reservations": len(reserved),
            "total_volume": round(total_volume, 8),
        }
        if cfg.max_total_volume is not None:
            usage["volume_available"] = round(max(0.0, cfg.max_total_volume - total_volume), 8)
        if cfg.max_open_positions is not None:
            usage["slots_available"] = max(0, cfg.max_open_positions - len(all_positions) - len(all_orders) - sum(s for _, s, _ in reserved))
        if cfg.max_risk_per_trade_pct is not None:
            usage["max_loss_per_trade"] = (
                round(float(account.equity) * cfg.max_risk_per_trade_pct / 100, 2) if account is not None else None
            )
            usage["max_loss_per_trade_currency"] = currency
        limits["usage"] = usage

    finished = time.time()
    data = {
        "symbol": symbol,
        "strategy_id": strategy_id,
        "quote": quote,
        "account": account_data,
        "positions": positions_data,
        "pending_orders": orders_data,
        "limits": limits,
        "capture": {
            "started_at": iso_utc(started),
            "finished_at": iso_utc(finished),
            "duration_ms": round((finished - started) * 1000, 3),
            "max_skew_ms": round((finished - started) * 1000, 3),
            "terminal_calls": rec.calls,
            "atomic": False,
            "note": "Components are successive reads; compare captured_at values before relying on them together.",
        },
        "errors": rec.errors,
    }
    message = f"Snapshot of {symbol}" + (f" with {len(rec.errors)} component errors" if rec.errors else "")
    return {"error": bool(rec.errors), "message": message, "data": data}
