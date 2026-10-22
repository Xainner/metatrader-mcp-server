"""
Local supervisor of positions: explicit, persistent rules evaluated by a loop
that does not depend on the AI agent answering.

A rule watches one position (``ticket``) or the positions of a strategy
(``strategy_id``, optionally one ``symbol``) and closes them when the
floating result crosses a threshold chosen by the user:

- ``metric``: ``pnl`` (profit + swap) or ``profit``, in the account currency.
- ``operator``: ``>=`` or ``<=``; ``threshold``: a number.
- ``action``: ``close``.

States
======

=======================  ======================================================
active                   Evaluated on every cycle.
paused                   Kept, not evaluated (``pause_position_rule``).
triggered                The condition was met; the action is about to start.
executing                The close requests are being sent.
fulfilled                Every target position was closed and verified.
failed                   The close was rejected; nothing is retried.
pending_reconciliation   The outcome is uncertain (no response, partial close,
                         interrupted by a restart); nothing is retried.
target_closed            The watched position closed outside the rule.
removed                  Deleted by the user (kept for the audit trail).
=======================  ======================================================

Guarantees and limits:

- Rules exist only after an explicit ``create_position_rule``; installing or
  starting the server activates nothing.
- One process evaluates at a time (a lease in the shared database), and each
  rule moves from ``active`` to ``triggered`` with a compare-and-set, so two
  cycles or processes cannot execute it twice. Each close uses the request
  ID ``rule:<rule_id>:<ticket>``: repeating it never sends a second close.
- After a restart, rules found ``triggered`` or ``executing`` become
  ``pending_reconciliation``; they are never retried blindly.
- A P&L that cannot be read, or a quote that has not changed for
  ``MT5_SUPERVISOR_MAX_STALE_SECONDS`` (default 120), skips the evaluation; no
  external price is used instead.
- The loop depends on this process and its connection: it does not
  guarantee an exact price or result. Broker-side SL/TP remain the
  independent protection.

Settings: ``MT5_SUPERVISOR_ENABLED`` (true), ``MT5_SUPERVISOR_INTERVAL_SECONDS`` (5),
``MT5_SUPERVISOR_MAX_STALE_SECONDS`` (120).
"""

import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

import MetaTrader5 as mt5

from .serialization import to_plain
from .store import Store, get_store
from .strategy import strategy_magic, validate_strategy_id
from .timebase import iso_utc

logger = logging.getLogger(__name__)

METRICS = ("pnl", "profit")
OPERATORS = (">=", "<=")
ACTIONS = ("close",)
LEASE_NAME = "position_supervisor"
DEAL_ENTRY_OUT = 1

ACTIVE, PAUSED, TRIGGERED, EXECUTING = "active", "paused", "triggered", "executing"
FULFILLED, FAILED, PENDING_RECONCILIATION = "fulfilled", "failed", "pending_reconciliation"
TARGET_CLOSED, REMOVED = "target_closed", "removed"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _row(row) -> Dict[str, Any]:
    record = dict(row)
    if record.get("result"):
        record["result"] = json.loads(record["result"])
    for key in ("created_at", "updated_at", "last_checked_at", "triggered_at"):
        if record.get(key):
            record[f"{key}_iso"] = iso_utc(record[key])
    return record


class RuleStore:
    """Position rules kept in the shared SQLite store."""

    def __init__(self, store: Store):
        self.store = store

    def create(self, **fields) -> Dict[str, Any]:
        now = time.time()
        rule_id = uuid.uuid4().hex[:16]
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO position_rules(rule_id, account, server, ticket, strategy_id, magic, symbol, metric, operator, "
                "threshold, currency, action, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rule_id, fields.get("account"), fields.get("server"), fields.get("ticket"), fields.get("strategy_id"),
                    fields.get("magic"), fields.get("symbol"), fields["metric"], fields["operator"], fields["threshold"],
                    fields.get("currency"), fields["action"], ACTIVE, now, now,
                ),
            )
            db.execute("INSERT INTO rule_events(rule_id, time, state, detail) VALUES (?, ?, ?, ?)", (rule_id, now, ACTIVE, "created"))
        return self.get(rule_id)

    def get(self, rule_id: str) -> Optional[Dict[str, Any]]:
        rows = self.store.query("SELECT * FROM position_rules WHERE rule_id = ?", (rule_id,))
        return _row(rows[0]) if rows else None

    def list(self, *, states: Optional[List[str]] = None, account: Optional[str] = None) -> List[Dict[str, Any]]:
        sql, params = "SELECT * FROM position_rules WHERE 1 = 1", []
        if states:
            sql += f" AND state IN ({', '.join('?' for _ in states)})"
            params.extend(states)
        if account is not None:
            sql += " AND account = ?"
            params.append(str(account))
        sql += " ORDER BY created_at"
        return [_row(r) for r in self.store.query(sql, params)]

    def events(self, rule_id: str) -> List[Dict[str, Any]]:
        rows = self.store.query("SELECT time, state, detail FROM rule_events WHERE rule_id = ? ORDER BY id", (rule_id,))
        return [{**dict(r), "time_iso": iso_utc(r["time"])} for r in rows]

    def transition(self, rule_id: str, from_states, to_state: str, *, detail: Optional[str] = None, **fields) -> bool:
        """Compare-and-set: change the state only if the rule is in one of ``from_states``."""
        from_states = list(from_states)
        now = time.time()
        columns, values = ["state = ?", "updated_at = ?"], [to_state, now]
        for key, value in fields.items():
            if key == "result":
                value = json.dumps(to_plain(value), sort_keys=True, default=str)
            columns.append(f"{key} = ?")
            values.append(value)
        with self.store.transaction() as db:
            changed = db.execute(
                f"UPDATE position_rules SET {', '.join(columns)} WHERE rule_id = ? AND state IN ({', '.join('?' for _ in from_states)})",
                (*values, rule_id, *from_states),
            ).rowcount
            if changed:
                db.execute("INSERT INTO rule_events(rule_id, time, state, detail) VALUES (?, ?, ?, ?)", (rule_id, now, to_state, detail))
        return bool(changed)

    def observe(self, rule_id: str, *, value: Optional[float], error: Optional[str]) -> None:
        self.store.execute(
            "UPDATE position_rules SET last_value = ?, last_checked_at = ?, last_error = ? WHERE rule_id = ?",
            (value, time.time(), error, rule_id),
        )

    def acquire_lease(self, owner: str, ttl: float) -> bool:
        """Take or renew the supervisor lease; False while another live owner holds it."""
        now = time.time()
        with self.store.transaction() as db:
            row = db.execute("SELECT owner, expires_at FROM leases WHERE name = ?", (LEASE_NAME,)).fetchone()
            if row is not None and row["owner"] != owner and row["expires_at"] > now:
                return False
            db.execute(
                "INSERT INTO leases(name, owner, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET owner = excluded.owner, expires_at = excluded.expires_at",
                (LEASE_NAME, owner, now + ttl),
            )
        return True

    def release_lease(self, owner: str) -> None:
        self.store.execute("DELETE FROM leases WHERE name = ? AND owner = ?", (LEASE_NAME, owner))

    def recover_interrupted(self) -> List[str]:
        """Rules left triggered/executing by a stopped process become pending_reconciliation."""
        ids = [r["rule_id"] for r in self.list(states=[TRIGGERED, EXECUTING])]
        for rule_id in ids:
            self.transition(rule_id, [TRIGGERED, EXECUTING], PENDING_RECONCILIATION,
                            detail="interrupted (process stopped during the action); not retried")
        return ids


def get_rule_store(connection) -> RuleStore:
    return RuleStore(get_store(connection))


# ----------------------------------------------------------------------
# Client operations (exposed as MCP tools)
# ----------------------------------------------------------------------
def create_position_rule(
    connection,
    *,
    operator: str,
    threshold: float,
    ticket: Optional[int] = None,
    strategy_id: Optional[str] = None,
    symbol: Optional[str] = None,
    metric: str = "pnl",
    action: str = "close",
) -> Dict[str, Any]:
    """
    Create an active rule; see the module documentation for its semantics.

    Exactly one of ``ticket`` or ``strategy_id`` is required. The threshold is
    in the account currency and is chosen by the user; no default is assumed.
    """
    if (ticket is None) == (strategy_id is None):
        return {"error": True, "message": "Pass exactly one of ticket or strategy_id", "data": None}
    if metric not in METRICS:
        return {"error": True, "message": f"metric must be one of {', '.join(METRICS)}", "data": None}
    if operator not in OPERATORS:
        return {"error": True, "message": f"operator must be one of {', '.join(OPERATORS)}", "data": None}
    if action not in ACTIONS:
        return {"error": True, "message": f"action must be one of {', '.join(ACTIONS)}", "data": None}
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        return {"error": True, "message": "threshold must be a number", "data": None}
    error = validate_strategy_id(strategy_id)
    if error:
        return {"error": True, "message": error, "data": None}

    account = mt5.account_info()
    if account is None:
        return {"error": True, "message": "Cannot read the account; the rule was not created", "data": None}
    magic = strategy_magic(strategy_id) if strategy_id else None
    if ticket is not None:
        try:
            ticket = int(ticket)
        except (TypeError, ValueError):
            return {"error": True, "message": f"Invalid ticket {ticket!r}", "data": None}
        positions = mt5.positions_get(ticket=ticket)
        if positions is None:
            return {"error": True, "message": "Cannot read positions; the rule was not created", "data": None}
        if not positions:
            return {"error": True, "message": f"Position {ticket} is not open", "data": None}
        symbol = positions[0].symbol

    rule = get_rule_store(connection).create(
        account=str(getattr(account, "login", "")), server=getattr(account, "server", None), ticket=ticket,
        strategy_id=strategy_id, magic=magic, symbol=symbol, metric=metric, operator=operator,
        threshold=threshold, currency=getattr(account, "currency", None), action=action,
    )
    scope = f"position {ticket}" if ticket is not None else f"strategy {strategy_id}" + (f" on {symbol}" if symbol else "")
    return {
        "error": False,
        "message": f"Rule {rule['rule_id']} active: {action} {scope} when {metric} {operator} {threshold} {rule['currency']}",
        "data": rule,
    }


def list_position_rules(connection, *, state: Optional[str] = None, include_events: bool = False) -> Dict[str, Any]:
    rules_store = get_rule_store(connection)
    rules = rules_store.list(states=[state] if state else None)
    if include_events:
        for rule in rules:
            rule["events"] = rules_store.events(rule["rule_id"])
    return {"error": False, "message": f"{len(rules)} rules", "data": rules}


def _change(connection, rule_id: str, from_states, to_state: str, detail: str) -> Dict[str, Any]:
    rules_store = get_rule_store(connection)
    rule = rules_store.get(rule_id)
    if rule is None:
        return {"error": True, "message": f"Unknown rule {rule_id}", "data": None}
    if not rules_store.transition(rule_id, from_states, to_state, detail=detail):
        return {"error": True, "message": f"Rule {rule_id} is {rule['state']}; it cannot become {to_state}", "data": rule}
    return {"error": False, "message": f"Rule {rule_id} is {to_state}", "data": rules_store.get(rule_id)}


def pause_position_rule(connection, rule_id: str) -> Dict[str, Any]:
    return _change(connection, rule_id, [ACTIVE], PAUSED, "paused by request")


def resume_position_rule(connection, rule_id: str) -> Dict[str, Any]:
    return _change(connection, rule_id, [PAUSED], ACTIVE, "resumed by request")


def remove_position_rule(connection, rule_id: str) -> Dict[str, Any]:
    """Remove a rule (kept as ``removed`` for the audit trail). A rule being executed cannot be removed."""
    removable = [ACTIVE, PAUSED, FULFILLED, FAILED, PENDING_RECONCILIATION, TARGET_CLOSED]
    return _change(connection, rule_id, removable, REMOVED, "removed by request")


# ----------------------------------------------------------------------
# Evaluation loop
# ----------------------------------------------------------------------
class PositionSupervisor:
    """Evaluates the active rules; ``run_cycle`` is synchronous and must run where MT5 calls are serialized."""

    def __init__(self, connection, *, owner: Optional[str] = None, interval: Optional[float] = None,
                 max_stale_seconds: Optional[float] = None, clock: Callable[[], float] = time.monotonic):
        self.connection = connection
        self.rules = get_rule_store(connection)
        self.owner = owner or f"supervisor-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.interval = interval if interval is not None else _env_float("MT5_SUPERVISOR_INTERVAL_SECONDS", 5.0)
        self.max_stale = max_stale_seconds if max_stale_seconds is not None else _env_float("MT5_SUPERVISOR_MAX_STALE_SECONDS", 120.0)
        self._clock = clock
        self._quotes: Dict[str, Dict[str, float]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_cycle: Optional[Dict[str, Any]] = None
        recovered = self.rules.recover_interrupted()
        if recovered:
            logger.warning("Rules interrupted by a restart moved to pending_reconciliation: %s", recovered)

    # Quote freshness, measured by this process: how long the last tick time has not changed.
    def _quote_age(self, symbol: str) -> Optional[float]:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        tick_time = float(getattr(tick, "time_msc", 0) or getattr(tick, "time", 0) or 0)
        now = self._clock()
        seen = self._quotes.get(symbol)
        if seen is None or seen["tick_time"] != tick_time:
            self._quotes[symbol] = {"tick_time": tick_time, "first_seen": now}
            return 0.0
        return now - seen["first_seen"]

    def run_cycle(self) -> Dict[str, Any]:
        summary: Dict[str, Any] = {"started_at": iso_utc(time.time()), "evaluated": 0, "triggered": [], "skipped": None, "errors": []}
        active = self.rules.list(states=[ACTIVE])
        if not active:
            summary["skipped"] = "no active rules"
            self.last_cycle = summary
            return summary
        if not self.rules.acquire_lease(self.owner, ttl=max(3 * self.interval, 30.0)):
            summary["skipped"] = "another supervisor holds the lease"
            self.last_cycle = summary
            return summary
        account = mt5.account_info()
        positions = mt5.positions_get()
        if account is None or positions is None:
            code, description = mt5.last_error()
            error = f"{'account' if account is None else 'positions'} unreadable (error {code}: {description}); not evaluated"
            for rule in active:
                self.rules.observe(rule["rule_id"], value=None, error=error)
            summary["skipped"] = error
            self.last_cycle = summary
            return summary

        login = str(getattr(account, "login", ""))
        for rule in active:
            if rule["account"] not in (None, login) or rule["server"] not in (None, getattr(account, "server", None)):
                continue
            summary["evaluated"] += 1
            try:
                self._evaluate(rule, positions, summary)
            except Exception as exc:  # one rule must not stop the others
                self.rules.observe(rule["rule_id"], value=None, error=f"{type(exc).__name__}: {exc}")
                summary["errors"].append({"rule_id": rule["rule_id"], "error": str(exc)})
        self.last_cycle = summary
        return summary

    def _targets(self, rule, positions) -> List[Any]:
        if rule["ticket"] is not None:
            return [p for p in positions if p.ticket == rule["ticket"]]
        return [
            p for p in positions
            if int(getattr(p, "magic", 0) or 0) == rule["magic"] and (not rule["symbol"] or p.symbol == rule["symbol"])
        ]

    def _evaluate(self, rule, positions, summary) -> None:
        rule_id = rule["rule_id"]
        targets = self._targets(rule, positions)
        if not targets:
            if rule["ticket"] is not None:
                self.rules.transition(rule_id, [ACTIVE], TARGET_CLOSED, detail="the position is no longer open (closed outside this rule)")
            else:
                self.rules.observe(rule_id, value=None, error="no open positions for the strategy")
            return
        for symbol in {p.symbol for p in targets}:
            age = self._quote_age(symbol)
            if age is None:
                self.rules.observe(rule_id, value=None, error=f"no quote for {symbol}; not evaluated")
                return
            if age > self.max_stale:
                self.rules.observe(rule_id, value=None, error=f"quote of {symbol} unchanged for {age:.0f}s; P&L considered stale, not evaluated")
                return
        value = sum(float(p.profit) + (float(getattr(p, "swap", 0) or 0) if rule["metric"] == "pnl" else 0.0) for p in targets)
        value = round(value, 2)
        self.rules.observe(rule_id, value=value, error=None)
        met = value >= rule["threshold"] if rule["operator"] == ">=" else value <= rule["threshold"]
        if not met:
            return
        observed_at = time.time()
        if not self.rules.transition(rule_id, [ACTIVE], TRIGGERED, detail=f"{rule['metric']} {value} {rule['operator']} {rule['threshold']}",
                                     triggered_value=value, triggered_at=observed_at):
            return  # another cycle or process got it first
        if not self.rules.transition(rule_id, [TRIGGERED], EXECUTING, detail="closing", owner=self.owner):
            return
        summary["triggered"].append(rule_id)
        self._execute(rule, targets, value, observed_at)

    def _execute(self, rule, targets, value, observed_at) -> None:
        from .order.close_position import close_position

        results = []
        for position in targets:
            response = close_position(self.connection, position.ticket, request_id=f"rule:{rule['rule_id']}:{position.ticket}")
            results.append({
                "ticket": position.ticket,
                "error": response.get("error"),
                "status": response.get("status"),
                "verification": response.get("verification"),
                "message": response.get("message"),
                "floating_pnl_at_trigger": round(float(position.profit) + float(getattr(position, "swap", 0) or 0), 2),
                "realized_net": self._realized(position.ticket) if response.get("verification") == "confirmed" else None,
            })
        verifications = {r["verification"] for r in results}
        if verifications == {"confirmed"}:
            state, detail = FULFILLED, "all target positions closed and verified"
        elif verifications <= {"not_closed"}:
            state, detail = FAILED, "the close was rejected; not retried"
        else:
            state, detail = PENDING_RECONCILIATION, "the outcome of at least one close is uncertain or partial; not retried"
        realized = [r["realized_net"] for r in results if r["realized_net"] is not None]
        result = {
            "observed": {"metric": rule["metric"], "value": value, "threshold": rule["threshold"], "operator": rule["operator"],
                         "currency": rule["currency"], "observed_at": iso_utc(observed_at), "kind": "floating"},
            "action": rule["action"],
            "results": results,
            "realized_net_total": round(sum(realized), 2) if len(realized) == len(results) else None,
            "realized_note": "realized_net adds profit, commission, swap and fees of the closing deals; it differs from the floating value at the trigger",
            "finished_at": iso_utc(time.time()),
        }
        self.rules.transition(rule["rule_id"], [EXECUTING], state, detail=detail, result=result)

    @staticmethod
    def _realized(ticket: int) -> Optional[float]:
        deals = mt5.history_deals_get(position=ticket)
        if not deals:
            return None
        exits = [d for d in deals if int(getattr(d, "entry", 0)) == DEAL_ENTRY_OUT]
        if not exits:
            return None
        return round(sum(float(d.profit) + float(d.commission) + float(d.swap) + float(getattr(d, "fee", 0) or 0) for d in exits), 2)

    # Background loop -----------------------------------------------------
    def start(self, submit: Callable[[Callable[[], Dict[str, Any]]], Any]) -> None:
        """Run cycles every ``interval`` seconds; ``submit`` runs a cycle where MT5 calls are serialized."""
        if self._thread is not None:
            return

        def loop():
            while not self._stop.wait(self.interval):
                try:
                    submit(self.run_cycle)
                except Exception as exc:  # keep the loop alive; the error is visible in the health data
                    self.last_cycle = {"error": f"{type(exc).__name__}: {exc}", "at": iso_utc(time.time())}
                    logger.warning("Position supervisor cycle failed: %s", exc)

        self._thread = threading.Thread(target=loop, name="position-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.rules.release_lease(self.owner)

    def state(self) -> Dict[str, Any]:
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "owner": self.owner,
            "interval_seconds": self.interval,
            "max_stale_seconds": self.max_stale,
            "last_cycle": self.last_cycle,
        }
