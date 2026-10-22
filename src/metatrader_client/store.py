"""
Durable local state kept in SQLite: trade request identities, the audit log
of risk limits, the new-entries switch and position supervisor rules.

Location: the ``state_db`` entry of the client config, else ``MT5_STATE_DB``,
else ``~/.metatrader_mcp/state.sqlite3``. ``:memory:`` keeps everything in
memory (tests). No credentials are written: rows hold account login and
server name, symbols, volumes, prices, tickets and broker messages.

Concurrency: one connection per :class:`Store`, guarded by a lock; writes that
must be atomic use ``BEGIN IMMEDIATE`` so several processes sharing the file
are serialized by SQLite. Schema changes are applied in order using
``PRAGMA user_version`` (see ``_MIGRATIONS``).
"""

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

DEFAULT_PATH = Path.home() / ".metatrader_mcp" / "state.sqlite3"

_MIGRATIONS = [
    # 1: request registry, limits audit log, settings
    """
    CREATE TABLE requests (
        request_id TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        account TEXT,
        server TEXT,
        strategy_id TEXT,
        magic INTEGER,
        symbol TEXT,
        kind TEXT NOT NULL,
        params TEXT NOT NULL,
        tag TEXT NOT NULL,
        state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        order_ticket INTEGER,
        deal_ticket INTEGER,
        position_ticket INTEGER,
        retcode INTEGER,
        message TEXT,
        outcome TEXT,
        result TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX requests_state ON requests(state, account, symbol);
    CREATE TABLE request_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL,
        time REAL NOT NULL,
        state TEXT NOT NULL,
        detail TEXT
    );
    CREATE INDEX request_events_id ON request_events(request_id);
    CREATE TABLE limits_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time REAL NOT NULL,
        source TEXT NOT NULL,
        limits TEXT NOT NULL
    );
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE settings_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time REAL NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL
    );
    """,
    # 2: position supervisor
    """
    CREATE TABLE position_rules (
        rule_id TEXT PRIMARY KEY,
        account TEXT,
        server TEXT,
        ticket INTEGER,
        strategy_id TEXT,
        magic INTEGER,
        symbol TEXT,
        metric TEXT NOT NULL,
        operator TEXT NOT NULL,
        threshold REAL NOT NULL,
        currency TEXT,
        action TEXT NOT NULL,
        state TEXT NOT NULL,
        owner TEXT,
        last_value REAL,
        last_checked_at REAL,
        last_error TEXT,
        triggered_value REAL,
        triggered_at REAL,
        result TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE rule_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_id TEXT NOT NULL,
        time REAL NOT NULL,
        state TEXT NOT NULL,
        detail TEXT
    );
    CREATE TABLE leases (
        name TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        expires_at REAL NOT NULL
    );
    """,
]


def default_path(config_value: Optional[str] = None, env=None) -> str:
    env = os.environ if env is None else env
    return config_value or env.get("MT5_STATE_DB") or str(DEFAULT_PATH)


class Store:
    """SQLite database with the migrations of this package applied."""

    def __init__(self, path: str = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            for index, script in enumerate(_MIGRATIONS[version:], start=version + 1):
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    for statement in script.split(";"):
                        if statement.strip():
                            self._db.execute(statement)
                    self._db.execute(f"PRAGMA user_version = {index}")
                    self._db.execute("COMMIT")
                except Exception:
                    self._db.execute("ROLLBACK")
                    raise

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._db.execute("PRAGMA user_version").fetchone()[0]

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Exclusive write transaction (``BEGIN IMMEDIATE``), also across processes."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def query(self, sql: str, params=()) -> List[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def execute(self, sql: str, params=()) -> int:
        """Run one autocommitted statement and return the number of changed rows."""
        with self._lock:
            return self._db.execute(sql, params).rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------
    # Settings with an audit trail
    # ------------------------------------------------------------------
    def get_setting(self, key: str) -> Optional[Dict[str, Any]]:
        rows = self.query("SELECT value, updated_at FROM settings WHERE key = ?", (key,))
        if not rows:
            return None
        return {"value": json.loads(rows[0]["value"]), "updated_at": rows[0]["updated_at"]}

    def set_setting(self, key: str, value: Any) -> None:
        now = time.time()
        encoded = json.dumps(value, sort_keys=True)
        with self.transaction() as db:
            db.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, encoded, now),
            )
            db.execute("INSERT INTO settings_log(time, key, value) VALUES (?, ?, ?)", (now, key, encoded))

    def setting_history(self, key: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.query(
            "SELECT time, value FROM settings_log WHERE key = ? ORDER BY id DESC LIMIT ?", (key, int(limit))
        )
        return [{"time": r["time"], "value": json.loads(r["value"])} for r in rows]

    # ------------------------------------------------------------------
    # Risk limits audit log
    # ------------------------------------------------------------------
    def record_limits(self, limits: Dict[str, Any], *, source: str) -> bool:
        """Store the effective limits when they differ from the last stored ones. Returns True if stored."""
        encoded = json.dumps(limits, sort_keys=True)
        with self.transaction() as db:
            last = db.execute("SELECT limits FROM limits_log ORDER BY id DESC LIMIT 1").fetchone()
            if last is not None and last["limits"] == encoded:
                return False
            db.execute("INSERT INTO limits_log(time, source, limits) VALUES (?, ?, ?)", (time.time(), source, encoded))
            return True

    def limits_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.query("SELECT time, source, limits FROM limits_log ORDER BY id DESC LIMIT ?", (int(limit),))
        return [{"time": r["time"], "source": r["source"], "limits": json.loads(r["limits"])} for r in rows]


_default_store: Optional[Store] = None
_default_lock = threading.Lock()


def get_store(connection) -> Store:
    """Return the store attached to a connection, or a process-wide in-memory store."""
    store = getattr(connection, "store", None)
    if isinstance(store, Store):
        return store
    global _default_store
    with _default_lock:
        if _default_store is None:
            _default_store = Store(":memory:")
        return _default_store


def reset_default_store() -> None:
    """Drop the process-wide fallback store (used by tests)."""
    global _default_store
    with _default_lock:
        _default_store = None
