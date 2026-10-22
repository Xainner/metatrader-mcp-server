"""
Durable identity of trade requests (``request_id``).

A ``request_id`` names one trading intention. The registry stores it with a
fingerprint of the normalized parameters before anything is sent, so:

- Repeating the same ID returns the known state; nothing is sent again.
- Reusing an ID with other parameters is a conflict.
- A new intentional operation needs a new ID.

States
======

==================  ==========================================================
prepared            Intention recorded; ``order_send`` has not been called.
sending             Marked immediately before ``order_send``; no result yet.
accepted            The broker placed the order (pending order, or a market
                    order accepted and not filled yet).
partially_filled    Part of the volume was executed.
filled              The requested volume was executed (or the request was
                    completed, for closes).
rejected            Rejected locally or by the broker; nothing was executed.
unknown             The outcome could not be determined (no response,
                    timeout, or a process stop during the send).
reconciled          A previously uncertain request was resolved from the
                    terminal history or by an explicit, recorded decision;
                    ``outcome`` says what was found.
==================  ==========================================================

``prepared`` and ``sending`` rows that are not updated for
``stale_after`` seconds (``MT5_REQUEST_STALE_SECONDS``, default 120) belong to
a process that stopped: ``prepared`` becomes ``rejected`` (outcome
``not_sent``, the send mark is committed before ``order_send`` is called) and
``sending`` becomes ``unknown``.

This does not provide exactly-once execution: the trade server offers no
idempotency key, and a request whose result was lost may stay ``unknown``
until it is reconciled or explicitly resolved. Each order carries a comment
tag derived from the ID (``tag``) so it can be found in the history; brokers
may replace comments, in which case reconciliation falls back to tickets.

Retention: rows in a final state older than ``MT5_REQUEST_RETENTION_DAYS``
(default 90) are removed by :meth:`RequestRegistry.purge`; ``unknown``,
``sending`` and ``prepared`` rows are never purged.
"""

import hashlib
import json
import os
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

from .serialization import to_plain
from .store import Store

STATE_PREPARED = "prepared"
STATE_SENDING = "sending"
STATE_ACCEPTED = "accepted"
STATE_PARTIAL = "partially_filled"
STATE_FILLED = "filled"
STATE_REJECTED = "rejected"
STATE_UNKNOWN = "unknown"
STATE_RECONCILED = "reconciled"

IN_FLIGHT_STATES = (STATE_PREPARED, STATE_SENDING)
UNRESOLVED_STATES = (STATE_SENDING, STATE_UNKNOWN)
FINAL_STATES = (STATE_FILLED, STATE_REJECTED, STATE_RECONCILED)

# send_order status -> registry state
STATUS_TO_STATE = {
    "done": STATE_FILLED,
    "partial": STATE_PARTIAL,
    "placed": STATE_ACCEPTED,
    "no_changes": STATE_REJECTED,
    "rejected": STATE_REJECTED,
    "no_response": STATE_UNKNOWN,
}

RESOLVE_DECISIONS = ("not_executed", "accept_unknown")


def new_request_id() -> str:
    return uuid.uuid4().hex


def fingerprint(params: Dict[str, Any]) -> str:
    """Stable hash of the normalized parameters of a request."""
    encoded = json.dumps(to_plain(params), sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def request_tag(request_id: str) -> str:
    """Short tag placed in the order comment to find the order in the history."""
    return "r" + hashlib.sha1(str(request_id).encode()).hexdigest()[:10]


def validate_request_id(request_id: Any) -> Optional[str]:
    """Return an error message for IDs that cannot be stored."""
    if not isinstance(request_id, str) or not request_id.strip():
        return "request_id must be a non-empty string"
    if len(request_id) > 128:
        return "request_id must be at most 128 characters"
    return None


def _row(row) -> Dict[str, Any]:
    record = dict(row)
    for key in ("params", "result"):
        if record.get(key):
            record[key] = json.loads(record[key])
    return record


class RequestRegistry:
    """Request identities stored in a :class:`~metatrader_client.store.Store`."""

    def __init__(self, store: Store, *, stale_after: Optional[float] = None, env=None):
        env = os.environ if env is None else env
        self.store = store
        if stale_after is None:
            stale_after = float(env.get("MT5_REQUEST_STALE_SECONDS") or 120)
        self.stale_after = float(stale_after)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def begin(
        self,
        request_id: str,
        *,
        params: Dict[str, Any],
        kind: str,
        account: Optional[Any] = None,
        server: Optional[str] = None,
        strategy_id: Optional[str] = None,
        magic: Optional[int] = None,
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Record a new intention atomically.

        Returns ``{"status": "new" | "existing" | "conflict", "record": {...}}``.
        Only ``new`` allows the caller to continue towards ``order_send``.
        """
        digest = fingerprint(params)
        now = time.time()
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO requests(request_id, fingerprint, account, server, strategy_id, magic, symbol, kind, "
                    "params, tag, state, attempts, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        request_id, digest, None if account is None else str(account), server, strategy_id,
                        magic, symbol, kind, json.dumps(to_plain(params), sort_keys=True, default=str),
                        request_tag(request_id), STATE_PREPARED, now, now,
                    ),
                )
                db.execute(
                    "INSERT INTO request_events(request_id, time, state, detail) VALUES (?, ?, ?, ?)",
                    (request_id, now, STATE_PREPARED, None),
                )
                status = "new"
            else:
                status = "existing" if row["fingerprint"] == digest else "conflict"
        record = self.get(request_id)
        return {"status": status, "record": record}

    def mark(self, request_id: str, state: str, *, detail: Optional[str] = None, **fields: Any) -> None:
        """Change the state of a request and append an event."""
        allowed = {"order_ticket", "deal_ticket", "position_ticket", "retcode", "message", "outcome", "result"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown request fields: {sorted(unknown)}")
        now = time.time()
        columns = ["state = ?", "updated_at = ?"]
        values: List[Any] = [state, now]
        for key, value in fields.items():
            if key == "result":
                value = json.dumps(to_plain(value), sort_keys=True, default=str)
            columns.append(f"{key} = ?")
            values.append(value)
        if state == STATE_SENDING:
            columns.append("attempts = attempts + 1")
        with self.store.transaction() as db:
            db.execute(f"UPDATE requests SET {', '.join(columns)} WHERE request_id = ?", (*values, request_id))
            db.execute(
                "INSERT INTO request_events(request_id, time, state, detail) VALUES (?, ?, ?, ?)",
                (request_id, now, state, detail),
            )

    def record_send_result(self, request_id: str, response: Dict[str, Any]) -> str:
        """Store the interpreted ``order_send`` result; returns the new state."""
        state = STATUS_TO_STATE.get(response.get("status"), STATE_UNKNOWN)
        data = response.get("data")
        self.mark(
            request_id,
            state,
            detail=response.get("message"),
            order_ticket=getattr(data, "order", None) or None,
            deal_ticket=getattr(data, "deal", None) or None,
            retcode=response.get("retcode"),
            message=response.get("message"),
        )
        return state

    def attach_result(self, request_id: str, result: Dict[str, Any], *, position_ticket: Optional[int] = None) -> None:
        """Keep the final client response (returned again when the ID is repeated)."""
        fields: Dict[str, Any] = {"result": result}
        if position_ticket:
            fields["position_ticket"] = int(position_ticket)
        record = self.get(request_id)
        if record is None:
            return
        self.mark(request_id, record["state"], detail="result stored", **fields)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Return the request, converting stale in-flight rows first."""
        rows = self.store.query("SELECT * FROM requests WHERE request_id = ?", (request_id,))
        if not rows:
            return None
        record = _row(rows[0])
        if self._expire_if_stale(record):
            return self.get(request_id)
        return record

    def events(self, request_id: str) -> List[Dict[str, Any]]:
        rows = self.store.query(
            "SELECT time, state, detail FROM request_events WHERE request_id = ? ORDER BY id", (request_id,)
        )
        return [dict(r) for r in rows]

    def list(self, *, states: Optional[Iterable[str]] = None, limit: int = 50) -> List[Dict[str, Any]]:
        sql, params = "SELECT * FROM requests", []
        if states:
            states = list(states)
            sql += f" WHERE state IN ({', '.join('?' for _ in states)})"
            params.extend(states)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        records = []
        for row in self.store.query(sql, params):
            record = _row(row)
            if self._expire_if_stale(record):
                record = self.get(record["request_id"])
            records.append(record)
        return records

    def unresolved(
        self,
        *,
        account: Optional[Any] = None,
        server: Optional[str] = None,
        symbol: Optional[str] = None,
        exclude: Optional[str] = None,
        kinds: Iterable[str] = ("open",),
    ) -> List[Dict[str, Any]]:
        """Requests that may have executed without a known result (``sending`` or ``unknown``)."""
        kinds = list(kinds)
        sql = (
            f"SELECT * FROM requests WHERE state IN (?, ?, ?) AND kind IN ({', '.join('?' for _ in kinds)})"
        )
        params: List[Any] = [STATE_PREPARED, STATE_SENDING, STATE_UNKNOWN, *kinds]
        for column, value in (("account", None if account is None else str(account)), ("server", server), ("symbol", symbol)):
            if value is not None:
                sql += f" AND {column} = ?"
                params.append(value)
        result = []
        for row in self.store.query(sql, params):
            record = _row(row)
            if record["request_id"] == exclude:
                continue
            if self._expire_if_stale(record):
                record = self.get(record["request_id"])
            if record["state"] in UNRESOLVED_STATES:
                result.append(record)
        return result

    def _expire_if_stale(self, record: Dict[str, Any]) -> bool:
        if record["state"] not in IN_FLIGHT_STATES:
            return False
        if time.time() - float(record["updated_at"]) < self.stale_after:
            return False
        if record["state"] == STATE_PREPARED:
            changed = self.store.execute(
                "UPDATE requests SET state = ?, outcome = ?, updated_at = ? WHERE request_id = ? AND state = ?",
                (STATE_REJECTED, "not_sent", time.time(), record["request_id"], STATE_PREPARED),
            )
            detail = "Abandoned before sending (the send mark was never written); not sent"
            new_state = STATE_REJECTED
        else:
            changed = self.store.execute(
                "UPDATE requests SET state = ?, updated_at = ? WHERE request_id = ? AND state = ?",
                (STATE_UNKNOWN, time.time(), record["request_id"], STATE_SENDING),
            )
            detail = "No result was recorded after the send; the process may have stopped during it"
            new_state = STATE_UNKNOWN
        if changed:
            self.store.execute(
                "INSERT INTO request_events(request_id, time, state, detail) VALUES (?, ?, ?, ?)",
                (record["request_id"], time.time(), new_state, detail),
            )
        return True

    # ------------------------------------------------------------------
    # Explicit resolution and retention
    # ------------------------------------------------------------------
    def resolve(self, request_id: str, *, decision: str, reason: str) -> Dict[str, Any]:
        """
        Record an explicit decision for an uncertain request.

        ``not_executed``: the caller verified that nothing was executed.
        ``accept_unknown``: the caller accepts that the outcome stays unknown
        (for example after checking the account manually) and unblocks new
        requests on the symbol. Both are kept in the audit trail with ``reason``.
        """
        if decision not in RESOLVE_DECISIONS:
            return {"error": True, "message": f"decision must be one of {', '.join(RESOLVE_DECISIONS)}", "data": None}
        if not reason or not str(reason).strip():
            return {"error": True, "message": "A reason is required to resolve a request", "data": None}
        record = self.get(request_id)
        if record is None:
            return {"error": True, "message": f"Unknown request_id {request_id}", "data": None}
        if record["state"] not in UNRESOLVED_STATES:
            return {
                "error": True,
                "message": f"Request {request_id} is {record['state']}; only sending or unknown requests can be resolved",
                "data": record,
            }
        outcome = "not_executed_by_decision" if decision == "not_executed" else "unknown_accepted_by_decision"
        self.mark(request_id, STATE_RECONCILED, detail=f"{decision}: {reason}", outcome=outcome)
        return {"error": False, "message": f"Request {request_id} resolved as {outcome}", "data": self.get(request_id)}

    def purge(self, *, older_than_days: Optional[float] = None, env=None) -> int:
        """Delete final requests older than the retention period; returns the number removed."""
        env = os.environ if env is None else env
        if older_than_days is None:
            older_than_days = float(env.get("MT5_REQUEST_RETENTION_DAYS") or 90)
        cutoff = time.time() - float(older_than_days) * 86400
        placeholders = ", ".join("?" for _ in FINAL_STATES)
        with self.store.transaction() as db:
            ids = [
                r["request_id"] for r in db.execute(
                    f"SELECT request_id FROM requests WHERE state IN ({placeholders}) AND updated_at < ?",
                    (*FINAL_STATES, cutoff),
                ).fetchall()
            ]
            for request_id in ids:
                db.execute("DELETE FROM request_events WHERE request_id = ?", (request_id,))
                db.execute("DELETE FROM requests WHERE request_id = ?", (request_id,))
        return len(ids)


def get_request_registry(connection) -> RequestRegistry:
    """Registry over the connection's store (or the process-wide in-memory store)."""
    from .store import get_store

    registry = getattr(connection, "request_registry", None)
    if isinstance(registry, RequestRegistry):
        return registry
    return RequestRegistry(get_store(connection))
