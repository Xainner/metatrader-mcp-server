"""
Risk controls enforced in code before any trade request reaches MetaTrader 5.

The limits do not depend on the judgement of the caller (human or AI agent):
``send_order`` checks them on every request that opens exposure.

Configuration comes from the ``risk`` entry of the client config dictionary
and, for keys not set there, from environment variables:

==============================  ==================================  ========
Config key                      Environment variable                Default
==============================  ==================================  ========
allow_live_trading              MT5_ALLOW_LIVE_TRADING              False
max_volume_per_order            MT5_MAX_VOLUME_PER_ORDER            None
max_total_volume                MT5_MAX_TOTAL_VOLUME                None
max_open_positions              MT5_MAX_OPEN_POSITIONS              None
max_risk_per_trade_pct          MT5_MAX_RISK_PER_TRADE_PCT          None
require_stop_loss               MT5_REQUIRE_STOP_LOSS               False
max_daily_loss                  MT5_MAX_DAILY_LOSS                  None
duplicate_window_seconds        MT5_DUPLICATE_WINDOW_SECONDS        10
allow_new_entries               MT5_ALLOW_NEW_ENTRIES               True
block_on_unresolved             MT5_BLOCK_ON_UNRESOLVED             True
max_entries_per_day             MT5_MAX_ENTRIES_PER_DAY             None
max_aggregate_risk_pct          MT5_MAX_AGGREGATE_RISK_PCT          None
strategy_limits                 MT5_STRATEGY_LIMITS (JSON)          None
missing_protection_policy       MT5_MISSING_PROTECTION_POLICY       report
==============================  ==================================  ========

``None`` disables a limit. Trading is only allowed on demo accounts unless
``allow_live_trading`` is explicitly enabled. The values are chosen by the
user; the defaults do not recommend a size or an acceptable loss.

``strategy_limits`` maps a ``strategy_id`` to its own ``max_volume_per_order``,
``max_total_volume``, ``max_open_positions``, ``max_daily_loss``,
``max_entries_per_day`` and ``max_aggregate_risk_pct``. They are applied in
addition to the account limits, to the positions, orders and reservations
with the strategy magic number (see ``metatrader_client.strategy``).

Aggregate risk rule (``max_aggregate_risk_pct``): the losses at the stop loss
of every position (from its current price), pending order (from its price,
as if it triggered), unlisted reservation and the new order are added
without netting opposite exposure; a stop loss in the profitable direction
contributes 0. Any item without stop loss makes the total unknown and the
new order is blocked. Values are in the account currency, as computed by
``order_calc_profit``; commissions and swaps are not included.

Entries (``max_entries_per_day``): deals of the server day that opened or
reversed a position, plus pending orders that may trigger, plus orders sent
and not listed yet.

Netting accounts hold one net position per symbol: the rules above are applied
to that position as listed (its volume, current price and stop loss), without
splitting it back into the orders that built it.

When a limit needs terminal data that cannot be read (``positions_get``,
``orders_get`` or ``history_deals_get`` returning ``None``), new exposure is
blocked instead of assuming that there is none.

Concurrency: the checks, the request and the reservation of an opening order
run under one lock per :class:`RiskGuard` (one per connection). Reservations
are kept in the shared SQLite store (``metatrader_client.store``), keyed by
account and server, so every connection and process using the same store
sees them. Before ``order_send`` the reservation-dependent limits (open
positions, total volume, daily entries, aggregate risk) are evaluated again
and the reservation is inserted in one ``BEGIN IMMEDIATE`` transaction, which
serializes competing openings across connections and processes; the second
one sees the first one's reservation and is rejected if the budget is spent.
The listings and stop-loss losses used by that final check are the ones read
by :meth:`RiskGuard.check_new_order` just before. An order that
was sent is reserved until the terminal lists it or ``RESERVATION_TTL_SECONDS``
pass. The reservation is reconciled by volume: the committed volume (executed
plus still working) minus what the listings show for its ticket (position
volume plus the current volume of the residual order) stays reserved, so a
listing that lags behind another never releases unpublished exposure. A
reduction that the positions do not explain (for example a cancelled
remainder) stays reserved until the TTL. A partial fill with RETURN filling
keeps its remainder committed; with IOC or FOK the remainder is cancelled by
the server and is not reserved. A rejected order
reserves nothing; an order with an unknown outcome stays reserved for the full
TTL. The protection covers every connection and process sharing the state
database. It does not coordinate processes with another database, manual
trading or other robots using the same account.
"""

import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Any, Dict, Hashable, Iterator, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

ACCOUNT_TRADE_MODE_DEMO = 0
RESERVATION_TTL_SECONDS = 60.0
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
_EPS = 1e-9
DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1
DEAL_ENTRY_IN = 0
DEAL_ENTRY_INOUT = 2

_ENV_PREFIX = "MT5_"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"Invalid boolean value {value!r}")


_BOOL_SETTINGS = {"allow_live_trading", "require_stop_loss", "allow_new_entries", "block_on_unresolved"}
_INT_SETTINGS = {"max_open_positions", "max_entries_per_day"}
STRATEGY_LIMIT_KEYS = (
    "max_volume_per_order",
    "max_total_volume",
    "max_open_positions",
    "max_daily_loss",
    "max_entries_per_day",
    "max_aggregate_risk_pct",
)
PROTECTION_POLICIES = ("report", "retry_sltp", "close")


def _parse_limits(name: str, raw: Mapping[str, Any]) -> Dict[str, Any]:
    unknown = set(raw) - set(STRATEGY_LIMIT_KEYS)
    if unknown:
        raise ValueError(f"Unknown {name} settings: {', '.join(sorted(unknown))}")
    parsed = {}
    for key, value in raw.items():
        if value is None:
            continue
        value = int(value) if key in _INT_SETTINGS else float(value)
        if value < 0:
            raise ValueError(f"{name}.{key} must not be negative")
        parsed[key] = value
    return parsed


@dataclass(frozen=True)
class RiskConfig:
    allow_live_trading: bool = False
    max_volume_per_order: Optional[float] = None
    max_total_volume: Optional[float] = None
    max_open_positions: Optional[int] = None
    max_risk_per_trade_pct: Optional[float] = None
    require_stop_loss: bool = False
    max_daily_loss: Optional[float] = None
    duplicate_window_seconds: float = 10.0
    allow_new_entries: bool = True
    block_on_unresolved: bool = True
    max_entries_per_day: Optional[int] = None
    max_aggregate_risk_pct: Optional[float] = None
    strategy_limits: Optional[Mapping[str, Mapping[str, Any]]] = None
    missing_protection_policy: str = "report"

    @classmethod
    def from_sources(cls, config: Optional[Mapping[str, Any]] = None, env: Optional[Mapping[str, str]] = None) -> "RiskConfig":
        """Build the configuration from a mapping, falling back to environment variables."""
        config = dict(config or {})
        env = os.environ if env is None else env
        unknown = set(config) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown risk settings: {', '.join(sorted(unknown))}")

        values: Dict[str, Any] = {}
        for field in fields(cls):
            raw = config.get(field.name)
            if raw is None:
                env_value = env.get(_ENV_PREFIX + field.name.upper())
                raw = env_value if env_value not in (None, "") else None
            if raw is None:
                continue
            if field.name in _BOOL_SETTINGS:
                values[field.name] = _parse_bool(raw)
            elif field.name in _INT_SETTINGS:
                values[field.name] = int(raw)
            elif field.name == "strategy_limits":
                raw = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(raw, Mapping):
                    raise ValueError("strategy_limits must be a mapping of strategy_id to limits")
                values[field.name] = {str(k): _parse_limits(f"strategy_limits.{k}", v) for k, v in raw.items()}
            elif field.name == "missing_protection_policy":
                policy = str(raw).strip().lower()
                if policy not in PROTECTION_POLICIES:
                    raise ValueError(f"missing_protection_policy must be one of {', '.join(PROTECTION_POLICIES)}")
                values[field.name] = policy
            else:
                values[field.name] = float(raw)

        for name, value in values.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
                raise ValueError(f"Risk setting {name} must not be negative")
        return cls(**values)

    def as_dict(self) -> Dict[str, Any]:
        """Effective settings as a JSON-friendly dict (no secrets are part of the risk settings)."""
        result = {f.name: getattr(self, f.name) for f in fields(self)}
        result["strategy_limits"] = {k: dict(v) for k, v in (self.strategy_limits or {}).items()}
        return result

    def limits_for(self, strategy_id: Optional[str]) -> Dict[str, Any]:
        """Limits configured for one strategy (empty when none)."""
        if not strategy_id or not self.strategy_limits:
            return {}
        return dict(self.strategy_limits.get(strategy_id) or {})


@dataclass
class _Reservation:
    """
    Exposure sent to the terminal but possibly not listed yet.

    ``executed`` is the filled volume, expected as positions whose ticket or
    identifier is the order ticket. ``working`` is the volume still active as
    an order with that ticket (pending orders, RETURN remainders, accepted but
    unfilled market orders).
    """
    ticket: Optional[int]
    executed: float
    working: float
    created: float
    magic: int = 0
    risk: Optional[float] = None
    id: Optional[str] = None
    status: str = "sent"

    def unlisted(self, positions, orders) -> Tuple[float, int]:
        """
        Return the volume and the number of entries that the listings do not show yet.

        The committed volume (executed + working) is compared with what the
        listings show for this ticket: the volume of positions whose ticket or
        identifier matches, plus the current volume of the residual order. The
        presence of the ticket alone does not release anything.
        """
        if self.ticket is None:
            return self.executed + self.working, int(self.executed > _EPS) + int(self.working > _EPS)
        in_positions = sum(
            float(p.volume) for p in positions
            if self.ticket in (getattr(p, "ticket", None), getattr(p, "identifier", None))
        )
        in_orders = [float(getattr(o, "volume_current", 0) or 0) for o in orders if getattr(o, "ticket", None) == self.ticket]
        committed = self.executed + self.working
        volume = max(0.0, committed - in_positions - sum(in_orders))
        # Entries the terminal will list once it catches up: a position for the
        # executed part and an order for the part still working.
        slots = int(self.executed > _EPS and in_positions <= _EPS)
        slots += int(self.working > _EPS and not in_orders and in_positions < committed - _EPS)
        return volume, slots


def _read(mt5, function: str, **kwargs) -> Tuple[Optional[tuple], Optional[str]]:
    """Call a listing function; return (items, None) or (None, error) when the terminal returns None."""
    items = getattr(mt5, function)(**kwargs)
    if items is None:
        code, description = mt5.last_error()
        return None, f"{function} failed (error {code}: {description})"
    return tuple(items), None


class RiskGuard:
    """
    Applies a :class:`RiskConfig` to trade requests.

    Every ``check_*`` method returns ``None`` when the request is allowed and
    an error message otherwise.
    """

    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()
        self._recent: Dict[Hashable, float] = {}
        self._lock = threading.Lock()
        self._opening_lock = threading.Lock()
        self._store = None
        self._account_key = "unknown"
        self._plans = threading.local()

    # ------------------------------------------------------------------
    # Shared reservation book
    # ------------------------------------------------------------------
    def bind(self, store, account_key: Optional[str]) -> None:
        """
        Keep reservations in ``store`` under ``account_key`` (``login@server``).

        Guards bound to the same store and account see each other's
        reservations. An unbound guard uses a private in-memory store.
        """
        self._store = store
        self._account_key = account_key or "unknown"

    def _book(self):
        if self._store is None:
            from .store import Store
            self._store = Store(":memory:")
        return self._store

    def _rows(self, db, now: float) -> List[_Reservation]:
        """Live reservations of the bound account; expired ones are deleted."""
        db.execute(
            "DELETE FROM reservations WHERE account_key = ? AND created_at <= ?",
            (self._account_key, now - RESERVATION_TTL_SECONDS),
        )
        rows = db.execute(
            "SELECT * FROM reservations WHERE account_key = ? ORDER BY created_at", (self._account_key,)
        ).fetchall()
        return [
            _Reservation(
                ticket=r["ticket"], executed=r["executed"], working=r["working"], created=r["created_at"],
                magic=r["magic"], risk=r["risk"], id=r["id"], status=r["status"],
            )
            for r in rows
        ]

    def _reserved(self, db, positions, orders, *, magic: Optional[int], now: float) -> List[Tuple[float, int, Optional[float]]]:
        unlisted: List[Tuple[float, int, Optional[float]]] = []
        for reservation in self._rows(db, now):
            volume, slots = reservation.unlisted(positions, orders)
            if volume <= _EPS and slots == 0:
                if reservation.status == "sent":
                    db.execute("DELETE FROM reservations WHERE id = ?", (reservation.id,))
                continue
            if magic is not None and reservation.magic != magic:
                continue
            committed = reservation.executed + reservation.working
            risk = None
            if reservation.risk is not None and committed > _EPS:
                risk = reservation.risk * min(1.0, volume / committed)
            unlisted.append((volume, slots, risk))
        return unlisted

    @contextmanager
    def opening(self) -> Iterator[None]:
        """
        Serialize the check, send and reservation of orders that open exposure.

        Hold it from the first limit check until :meth:`after_send` has run,
        so concurrent requests on this guard see each other's reservations.
        """
        with self._opening_lock:
            yield

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------
    def check_account(self, mt5) -> Optional[str]:
        """Block trading on non-demo accounts unless live trading is explicitly allowed."""
        if self.config.allow_live_trading:
            return None
        account = mt5.account_info()
        if account is None:
            return "Cannot read account information to verify that the account is a demo account"
        if getattr(account, "trade_mode", None) != ACCOUNT_TRADE_MODE_DEMO:
            return (
                "Trading is blocked: the account is not a demo account. "
                "Set MT5_ALLOW_LIVE_TRADING=true only after validating the setup on a demo account."
            )
        return None

    # ------------------------------------------------------------------
    # Exposure for new orders
    # ------------------------------------------------------------------
    def check_new_order(
        self,
        mt5,
        *,
        symbol: str,
        side: int,
        volume: float,
        entry_price: float,
        stop_loss: Optional[float],
        magic: Optional[int] = None,
        strategy_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Check the limits that apply to an order that opens new exposure.

        Account limits always apply. When ``strategy_id`` has entries in
        ``strategy_limits``, the same kinds of limits are applied again to the
        positions, pending orders and reservations whose magic is ``magic``.

        Args:
            side: ``mt5.ORDER_TYPE_BUY`` (0) or ``mt5.ORDER_TYPE_SELL`` (1) direction of the order.
        """
        cfg = self.config
        account_limits = {
            "max_volume_per_order": cfg.max_volume_per_order,
            "max_total_volume": cfg.max_total_volume,
            "max_open_positions": cfg.max_open_positions,
            "max_daily_loss": cfg.max_daily_loss,
            "max_entries_per_day": cfg.max_entries_per_day,
            "max_aggregate_risk_pct": cfg.max_aggregate_risk_pct,
        }
        scopes = [("account", account_limits, None)]
        strategy_limits = cfg.limits_for(strategy_id)
        if strategy_limits:
            scopes.append((f"strategy {strategy_id}", strategy_limits, int(magic or 0)))

        new_order = {"symbol": symbol, "side": side, "volume": volume, "entry_price": entry_price, "stop_loss": stop_loss}
        plan: Dict[str, Any] = {"positions": None, "orders": None, "scopes": [], "equity": None, "currency": None, "new_risk": None}
        self._plans.current = None
        for label, limits, scope_magic in scopes:
            error = self._check_scope(mt5, label=label, limits=limits, magic=scope_magic, new_order=new_order, plan=plan)
            if error:
                return error
        self._plans.current = plan

        return self.check_stop_loss_risk(
            mt5, symbol=symbol, side=side, volume=volume, entry_price=entry_price, stop_loss=stop_loss
        )

    def _check_scope(self, mt5, *, label: str, limits: Mapping[str, Any], magic: Optional[int],
                     new_order: Dict[str, Any], plan: Dict[str, Any]) -> Optional[str]:
        """
        Apply one set of limits to the items of a scope (``magic`` None means the whole account).

        The values that do not depend on reservations (listed positions and
        orders, today's entries, losses at the stop losses) are kept in
        ``plan`` so :meth:`commit_reservation` can evaluate the limits again
        against the shared reservations without reading the terminal.
        """
        volume = new_order["volume"]
        prefix = "" if magic is None else f"[{label}] "

        max_volume = limits.get("max_volume_per_order")
        if max_volume is not None and volume > max_volume + 1e-9:
            return f"{prefix}Volume {volume} exceeds the per-order limit of {max_volume} lots"

        needs_listings = any(
            limits.get(k) is not None
            for k in ("max_total_volume", "max_open_positions", "max_entries_per_day", "max_aggregate_risk_pct")
        )
        if needs_listings:
            if plan["positions"] is None:
                positions, error = _read(mt5, "positions_get")
                if error:
                    return f"{prefix}Cannot verify exposure limits, new orders are blocked: {error}"
                orders, error = _read(mt5, "orders_get")
                if error:
                    return f"{prefix}Cannot verify exposure limits, new orders are blocked: {error}"
                plan["positions"], plan["orders"] = positions, orders
            positions, orders = plan["positions"], plan["orders"]
            if magic is not None:
                positions = tuple(p for p in positions if int(getattr(p, "magic", 0) or 0) == magic)
                orders = tuple(o for o in orders if int(getattr(o, "magic", 0) or 0) == magic)
            scope = {
                "prefix": prefix,
                "magic": magic,
                "limits": dict(limits),
                "positions": len(positions),
                "orders": len(orders),
                "volume": sum(float(p.volume) for p in positions) + sum(float(o.volume_current) for o in orders),
                "opened": 0,
                "base_risk": None,
            }
            if limits.get("max_entries_per_day") is not None:
                opened, error = self._opened_today(mt5, magic=magic, symbol=new_order["symbol"])
                if error:
                    return f"{prefix}Cannot verify the daily entries limit, new orders are blocked: {error}"
                scope["opened"] = opened
            if limits.get("max_aggregate_risk_pct") is not None:
                error = self._prepare_aggregate_risk(mt5, scope=scope, positions=positions, orders=orders,
                                                     new_order=new_order, plan=plan, prefix=prefix)
                if error:
                    return error
            reserved = self._unlisted_reservations(plan["positions"], plan["orders"], magic=magic)
            error = self._evaluate_scope(scope, reserved, volume=volume, plan=plan)
            if error:
                return error
            plan["scopes"].append(scope)

        max_daily = limits.get("max_daily_loss")
        if max_daily is not None:
            error = self.check_daily_loss(mt5, symbol=new_order["symbol"], limit=max_daily, magic=magic)
            if error:
                return f"{prefix}{error}"
        return None

    def _evaluate_scope(self, scope: Dict[str, Any], reserved, *, volume: float, plan: Dict[str, Any]) -> Optional[str]:
        """Limits that depend on reservations, from the values prepared in ``scope``."""
        limits, prefix = scope["limits"], scope["prefix"]

        max_open = limits.get("max_open_positions")
        if max_open is not None:
            reserved_slots = sum(slots for _, slots, _ in reserved)
            open_count = scope["positions"] + scope["orders"] + reserved_slots
            if open_count + 1 > max_open:
                return (
                    f"{prefix}Opening this order would exceed the limit of {max_open} "
                    f"open positions and pending orders ({scope['positions']} positions, {scope['orders']} orders, "
                    f"{reserved_slots} recently sent and not listed yet)"
                )

        max_total = limits.get("max_total_volume")
        if max_total is not None:
            current = scope["volume"] + sum(v for v, _, _ in reserved)
            if current + volume > max_total + 1e-9:
                return (
                    f"{prefix}Total exposure would be {round(current + volume, 8)} lots, "
                    f"above the limit of {max_total} lots"
                )

        max_entries = limits.get("max_entries_per_day")
        if max_entries is not None:
            unlisted = sum(1 for v, _, _ in reserved if v > _EPS)
            total = scope["opened"] + scope["orders"] + unlisted
            if total + 1 > max_entries:
                return (
                    f"{prefix}Daily entries limit of {max_entries} reached ({scope['opened']} entries today, "
                    f"{scope['orders']} pending orders that may trigger, {unlisted} sent and not listed yet)"
                )

        max_aggregate = limits.get("max_aggregate_risk_pct")
        if max_aggregate is not None:
            total = scope["base_risk"]
            for unlisted_volume, _, risk in reserved:
                if unlisted_volume <= _EPS:
                    continue
                if risk is None:
                    return f"{prefix}Aggregate risk cannot be evaluated: an order sent without stop loss is not listed yet"
                total += risk
            total += plan["new_risk"]
            pct = total / plan["equity"] * 100
            if pct > max_aggregate + 1e-9:
                return (
                    f"{prefix}Aggregate loss at stop losses would be {total:.2f} {plan['currency']} "
                    f"({pct:.2f}% of equity), above the limit of {max_aggregate}%"
                )
        return None

    def _opened_today(self, mt5, *, magic: Optional[int], symbol: str) -> Tuple[Optional[int], Optional[str]]:
        """
        Entries of the server day: deals that opened or reversed a position
        today. Pending orders that may still trigger and orders sent and not
        listed yet are added by :meth:`_evaluate_scope`; the new order counts
        as one more entry.
        """
        deals, error = self._today_deals(mt5, symbol)
        if error:
            return None, error
        opened = [
            d for d in deals
            if int(getattr(d, "entry", DEAL_ENTRY_IN)) in (DEAL_ENTRY_IN, DEAL_ENTRY_INOUT)
            and d.type in (DEAL_TYPE_BUY, DEAL_TYPE_SELL)
            and (magic is None or int(getattr(d, "magic", 0) or 0) == magic)
        ]
        return len(opened), None

    def _prepare_aggregate_risk(self, mt5, *, scope, positions, orders, new_order, plan, prefix: str) -> Optional[str]:
        """
        Losses at the stop loss of the listed positions and pending orders of
        the scope, and of the new order, for the aggregate risk limit.

        Rules: each item contributes the loss from its current price (positions)
        or order price (pending orders) to its stop loss; a stop loss beyond the
        entry in the profitable direction contributes 0; opposite positions are
        not netted; an item without stop loss makes the total unknown, which
        blocks the order. Unlisted reservations are added with the loss stored
        when they were sent.
        """
        if plan["equity"] is None:
            account = mt5.account_info()
            if account is None:
                return f"{prefix}Cannot read account information to check the aggregate risk"
            plan["equity"], plan["currency"] = float(account.equity), account.currency
        if plan["equity"] <= 0:
            return f"{prefix}Account equity is not positive"

        total = 0.0
        for p in positions:
            if not p.sl:
                return f"{prefix}Aggregate risk cannot be evaluated: position {p.ticket} has no stop loss"
            price = float(getattr(p, "price_current", 0) or p.price_open)
            loss, error = self.loss_at_stop(mt5, symbol=p.symbol, side=int(p.type), volume=float(p.volume), entry_price=price, stop_loss=p.sl)
            if error:
                return f"{prefix}Aggregate risk cannot be evaluated: {error}"
            total += loss
        for o in orders:
            if not o.sl:
                return f"{prefix}Aggregate risk cannot be evaluated: pending order {o.ticket} has no stop loss"
            side = DEAL_TYPE_BUY if int(o.type) in (0, 2, 4, 6) else DEAL_TYPE_SELL
            loss, error = self.loss_at_stop(mt5, symbol=o.symbol, side=side, volume=float(o.volume_current), entry_price=float(o.price_open), stop_loss=o.sl)
            if error:
                return f"{prefix}Aggregate risk cannot be evaluated: {error}"
            total += loss
        if plan["new_risk"] is None:
            if not new_order["stop_loss"]:
                return f"{prefix}Aggregate risk cannot be evaluated: the new order has no stop loss"
            loss, error = self.loss_at_stop(mt5, **new_order)
            if error:
                return f"{prefix}Aggregate risk cannot be evaluated: {error}"
            plan["new_risk"] = loss
        scope["base_risk"] = total
        return None

    @staticmethod
    def loss_at_stop(mt5, *, symbol: str, side: int, volume: float, entry_price: float, stop_loss: Optional[float]) -> Tuple[Optional[float], Optional[str]]:
        """Loss (positive, account currency) if the price goes from ``entry_price`` to ``stop_loss``."""
        if not stop_loss:
            return None, "no stop loss"
        loss = mt5.order_calc_profit(side, symbol, volume, entry_price, stop_loss)
        if loss is None:
            return None, f"cannot calculate the loss at the stop loss for {symbol}"
        return abs(min(float(loss), 0.0)), None

    def check_stop_loss_risk(
        self,
        mt5,
        *,
        symbol: str,
        side: int,
        volume: float,
        entry_price: float,
        stop_loss: Optional[float],
    ) -> Optional[str]:
        """Require a stop loss when configured and limit the loss it allows."""
        cfg = self.config
        needs_sl = cfg.require_stop_loss or cfg.max_risk_per_trade_pct is not None
        if not stop_loss:
            return "A stop loss is required by the risk settings" if needs_sl else None
        if cfg.max_risk_per_trade_pct is None:
            return None

        account = mt5.account_info()
        if account is None:
            return "Cannot read account information to check the risk per trade"
        loss = mt5.order_calc_profit(side, symbol, volume, entry_price, stop_loss)
        if loss is None:
            return f"Cannot calculate the loss at the stop loss for {symbol}"
        loss = abs(min(float(loss), 0.0))
        equity = float(account.equity)
        if equity <= 0:
            return "Account equity is not positive"
        risk_pct = loss / equity * 100
        if risk_pct > cfg.max_risk_per_trade_pct + 1e-9:
            return (
                f"Loss at stop loss would be {loss:.2f} {account.currency} ({risk_pct:.2f}% of equity), "
                f"above the limit of {cfg.max_risk_per_trade_pct}%"
            )
        return None

    @staticmethod
    def _server_day(mt5, symbol: Optional[str]) -> Tuple[int, int]:
        """(start of the server day, server now). The server clock is read from the last tick."""
        server_now = None
        if symbol:
            tick = mt5.symbol_info_tick(symbol)
            server_now = getattr(tick, "time", None) if tick is not None else None
        if not server_now:
            server_now = int(time.time())
        return int(server_now) - int(server_now) % 86400, int(server_now)

    def _today_deals(self, mt5, symbol: Optional[str]) -> Tuple[Optional[List[Any]], Optional[str]]:
        """Deals of the current server day, or (None, error) when the history cannot be read."""
        day_start, server_now = self._server_day(mt5, symbol)
        deals = mt5.history_deals_get(
            datetime.fromtimestamp(day_start - 86400, tz=timezone.utc),
            datetime.fromtimestamp(server_now + 86400, tz=timezone.utc),
        )
        if deals is None:
            code, description = mt5.last_error()
            return None, f"history_deals_get failed (error {code}: {description})"
        return [d for d in deals if d.time >= day_start], None

    def check_daily_loss(self, mt5, *, symbol: Optional[str] = None, limit: Optional[float] = None,
                         magic: Optional[int] = None) -> Optional[str]:
        """
        Block new exposure once the loss of the current trading day reaches the limit.

        The day starts at 00:00 of the trade server clock. The daily result is the
        realized profit of today's deals (including commission, swap and fees)
        plus the floating profit of open positions. With ``magic``, only deals
        and positions with that magic number count.
        """
        if limit is None:
            limit = self.config.max_daily_loss
        if limit is None:
            return None

        deals, error = self._today_deals(mt5, symbol)
        if error:
            return f"Cannot verify the daily loss limit, new orders are blocked: {error}"
        realized = sum(
            float(d.profit) + float(d.commission) + float(d.swap) + float(getattr(d, "fee", 0.0))
            for d in deals
            if d.type in (DEAL_TYPE_BUY, DEAL_TYPE_SELL)
            and (magic is None or int(getattr(d, "magic", 0) or 0) == magic)
        )
        positions, error = _read(mt5, "positions_get")
        if error:
            return f"Cannot verify the daily loss limit, new orders are blocked: {error}"
        floating = sum(
            float(p.profit) + float(p.swap) for p in positions
            if magic is None or int(getattr(p, "magic", 0) or 0) == magic
        )
        daily = realized + floating
        if daily <= -limit:
            return f"Daily loss limit reached: today's result is {daily:.2f}, limit is -{limit}"
        return None

    # ------------------------------------------------------------------
    # Duplicate requests
    # ------------------------------------------------------------------
    def check_duplicate(self, key: Hashable) -> Optional[str]:
        """Reject a request identical to one sent within the duplicate window."""
        window = self.config.duplicate_window_seconds
        if not window:
            return None
        now = time.monotonic()
        with self._lock:
            self._recent = {k: t for k, t in self._recent.items() if now - t < window}
            if key in self._recent:
                return (
                    f"Identical request sent {now - self._recent[key]:.1f}s ago; "
                    f"repeat it after {window:g}s if it is intended"
                )
        return None

    def record(self, key: Hashable) -> None:
        """Remember a request that was sent, so identical requests are detected."""
        if self.config.duplicate_window_seconds:
            with self._lock:
                self._recent[key] = time.monotonic()

    # ------------------------------------------------------------------
    # Reservations of exposure sent but not yet listed by the terminal
    # ------------------------------------------------------------------
    def commit_reservation(self, *, volume: float, magic: int = 0, risk: Optional[float] = None,
                           request_id: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """
        Evaluate the reservation-dependent limits again and reserve the order, atomically.

        Runs in one ``BEGIN IMMEDIATE`` transaction of the shared store, so two
        connections or processes cannot both use the last part of a budget:
        the second one sees the first one's reservation. It uses the listings
        and losses prepared by the last :meth:`check_new_order` of this thread.

        Returns ``(reservation_id, None)`` or ``(None, error)``.
        """
        plan = getattr(self._plans, "current", None)
        self._plans.current = None
        now = time.time()
        with self._book().transaction() as db:
            if plan is not None:
                for scope in plan["scopes"]:
                    reserved = self._reserved(db, plan["positions"], plan["orders"], magic=scope["magic"], now=now)
                    error = self._evaluate_scope(scope, reserved, volume=float(volume), plan=plan)
                    if error:
                        return None, error
            reservation_id = uuid.uuid4().hex
            # Until the outcome is known the whole volume is reserved without ticket (counted as unlisted).
            db.execute(
                "INSERT INTO reservations(id, account_key, request_id, ticket, executed, working, magic, risk, status, created_at) "
                "VALUES (?, ?, ?, NULL, ?, 0, ?, ?, 'pending', ?)",
                (reservation_id, self._account_key, request_id, float(volume), int(magic or 0), risk, now),
            )
        return reservation_id, None

    def after_send(
        self,
        response: Dict[str, Any],
        duplicate_key: Optional[Hashable],
        volume: float,
        *,
        magic: int = 0,
        risk: Optional[float] = None,
        reservation_id: Optional[str] = None,
    ) -> None:
        """
        Apply the reservation policy to the outcome of an opening order.

        - done: the requested volume is reserved as executed.
        - partial: the filled volume is reserved as executed. The remainder is
          reserved as working unless the request used IOC or FOK filling, whose
          remainder the server cancels; with RETURN or an unknown policy it stays.
        - placed: the requested volume is reserved as working (pending orders,
          market orders accepted but not filled yet).
        - no_response: the requested volume is reserved for the full TTL and the
          request is remembered, because it may have been executed.
        - rejected or anything else: nothing is reserved.

        ``reservation_id`` is the pending reservation of :meth:`commit_reservation`;
        it is updated with the outcome (or deleted). Without it a new one is stored.
        """
        status = response.get("status")
        book = self._book()
        if status not in ("done", "placed", "partial", "no_response"):
            if reservation_id:
                book.execute("DELETE FROM reservations WHERE id = ?", (reservation_id,))
            return
        if duplicate_key is not None:
            self.record(duplicate_key)
        result = response.get("data")
        request = response.get("request") or {}
        ticket = getattr(result, "order", None) or None
        volume = float(volume)

        if status == "done":
            executed, working = volume, 0.0
        elif status == "partial":
            executed = min(float(getattr(result, "volume", 0) or 0), volume)
            remainder = volume - executed
            cancelled = request.get("type_filling") in (ORDER_FILLING_IOC, ORDER_FILLING_FOK)
            working = 0.0 if cancelled else remainder
        elif status == "placed":
            executed, working = 0.0, volume
        else:  # no_response: no ticket, kept for the full TTL
            executed, working = volume, 0.0

        if reservation_id:
            book.execute(
                "UPDATE reservations SET ticket = ?, executed = ?, working = ?, status = 'sent' WHERE id = ?",
                (ticket, executed, working, reservation_id),
            )
        else:
            book.execute(
                "INSERT INTO reservations(id, account_key, request_id, ticket, executed, working, magic, risk, status, created_at) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, 'sent', ?)",
                (uuid.uuid4().hex, self._account_key, ticket, executed, working, int(magic or 0), risk, time.time()),
            )

    def reservations(self) -> List[_Reservation]:
        """Live reservations of the bound account (expired ones are dropped)."""
        with self._book().transaction() as db:
            return self._rows(db, time.time())

    def _unlisted_reservations(self, positions, orders, *, magic: Optional[int] = None) -> List[Tuple[float, int, Optional[float]]]:
        """
        Reconcile reservations with the listings.

        Returns the unlisted (volume, entries, risk) of each reservation, and
        drops reservations that expired or are fully listed. ``risk`` is the
        loss at the stop loss of the unlisted part (None without stop loss).
        With ``magic``, only reservations of that magic number are returned.
        """
        with self._book().transaction() as db:
            return self._reserved(db, positions, orders, magic=magic, now=time.time())


_default_guard: Optional[RiskGuard] = None
_default_lock = threading.Lock()


def get_risk_guard(connection) -> RiskGuard:
    """Return the guard attached to a connection, or a process-wide guard built from the environment."""
    guard = getattr(connection, "risk_guard", None)
    if isinstance(guard, RiskGuard):
        return guard
    global _default_guard
    with _default_lock:
        if _default_guard is None:
            _default_guard = RiskGuard(RiskConfig.from_sources())
        return _default_guard


def account_key(mt5) -> str:
    """``login@server`` of the connected account, used to share reservations."""
    account = mt5.account_info()
    if account is None:
        return "unknown"
    return f"{getattr(account, 'login', '')}@{getattr(account, 'server', '')}"


def bound_risk_guard(connection, mt5) -> RiskGuard:
    """The connection's guard, bound to the connection's store and the connected account."""
    from .store import get_store

    guard = get_risk_guard(connection)
    guard.bind(get_store(connection), account_key(mt5))
    return guard


# ----------------------------------------------------------------------
# New entries switch
# ----------------------------------------------------------------------
ENTRIES_SETTING = "new_entries"


def entries_block_reason(config: RiskConfig, store) -> Optional[str]:
    """
    Reason why new entries are disabled, or None.

    New entries can be disabled by configuration (``allow_new_entries``) or at
    runtime (:func:`set_new_entries`, stored durably). Closing positions,
    cancelling orders and modifying stops stay available either way.
    """
    if not config.allow_new_entries:
        return "New entries are disabled by configuration (MT5_ALLOW_NEW_ENTRIES=false); reducing exposure is still allowed"
    setting = store.get_setting(ENTRIES_SETTING) if store is not None else None
    if setting and not setting["value"].get("enabled", True):
        reason = setting["value"].get("reason") or "no reason given"
        return f"New entries are disabled ({reason}); reducing exposure is still allowed"
    return None


def set_new_entries(store, *, enabled: bool, reason: str, source: str) -> Dict[str, Any]:
    """Enable or disable new entries at runtime; the change is kept in the settings log."""
    if not reason or not str(reason).strip():
        raise ValueError("A reason is required to change the new entries switch")
    value = {"enabled": bool(enabled), "reason": str(reason).strip(), "source": source, "time": time.time()}
    store.set_setting(ENTRIES_SETTING, value)
    return value
