"""Durable, transactionally acknowledged Bot API events. Private local data."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .config import Config

SCHEMA_VERSION = "1"
MESSAGE_TYPES = ("message", "edited_message", "channel_post", "edited_channel_post")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(path: Path, *, readonly: bool = False):
    target = f"{path.resolve().as_uri()}?mode=ro" if readonly else str(path)
    db = sqlite3.connect(target, uri=readonly, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        if readonly:
            db.execute("PRAGMA query_only=ON")
        else:
            db.execute("PRAGMA synchronous=FULL")
        yield db
    finally:
        db.close()


def initialize(config: Config) -> None:
    config.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with connect(config.database) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if version and version[0] != SCHEMA_VERSION:
                raise ValueError("Unsupported database schema")
            source = db.execute("SELECT value FROM metadata WHERE key='source_id'").fetchone()
            if source and source[0] != config.source_id:
                raise ValueError("Database belongs to a different source_id")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('source_id', ?)", (config.source_id,))
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL, update_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                event_type TEXT NOT NULL, revision INTEGER NOT NULL,
                emitted_at TEXT NOT NULL, received_at TEXT NOT NULL,
                reply_to_message_id INTEGER, text TEXT NOT NULL,
                content_hash TEXT NOT NULL, content_kind TEXT NOT NULL,
                UNIQUE(source_id, update_id)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS event_message ON events(source_id, chat_id, message_id)")


def set_metadata(config: Config, **values: str) -> None:
    with connect(config.database) as db, db:
        db.executemany("INSERT OR REPLACE INTO metadata VALUES (?, ?)", values.items())


def metadata(config: Config) -> dict[str, str]:
    if not config.database.exists():
        return {}
    with connect(config.database, readonly=True) as db:
        return dict(db.execute("SELECT key, value FROM metadata"))


def bind_bot(config: Config, bot_id: int) -> None:
    with connect(config.database) as db, db:
        old = db.execute("SELECT value FROM metadata WHERE key='bot_id'").fetchone()
        if old and old[0] != str(bot_id):
            raise ValueError("Bot identity changed; use a new source home/database")
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('bot_id', ?)", (str(bot_id),))


def ingest_updates(config: Config, updates: list[dict]) -> dict:
    """Commit accepted events AND next offset together, before the next poll."""
    if not isinstance(updates, list) or any(not isinstance(u, dict) or type(u.get("update_id")) is not int for u in updates):
        raise ValueError("Invalid update batch")
    inserted = ignored = 0
    with connect(config.database) as db, db:
        for update in sorted(updates, key=lambda item: item["update_id"]):
            key = next((key for key in MESSAGE_TYPES if key in update), None)
            msg = update.get(key) if key else None
            chat = msg.get("chat") if isinstance(msg, dict) else None
            chat_id = chat.get("id") if isinstance(chat, dict) else None
            if type(chat_id) is not int or chat_id not in config.allowed_chat_ids:
                ignored += 1
                continue
            mid, date = msg.get("message_id"), msg.get("edit_date", msg.get("date"))
            if type(mid) is not int or type(date) is not int:
                raise ValueError("Malformed allowed-chat event; batch not acknowledged")
            text = msg.get("text", msg.get("caption", ""))
            if not isinstance(text, str) or len(text) > 32768:
                raise ValueError("Invalid message text; batch not acknowledged")
            revision = db.execute("SELECT COALESCE(MAX(revision), 0) + 1 FROM events WHERE source_id=? AND chat_id=? AND message_id=?", (config.source_id, chat_id, mid)).fetchone()[0]
            reply = msg.get("reply_to_message")
            reply_id = reply.get("message_id") if isinstance(reply, dict) else None
            if type(reply_id) is not int:
                reply_id = None
            kind = "text" if "text" in msg else "media_or_service"
            canonical = json.dumps({"text": text, "kind": kind, "reply_id": reply_id}, sort_keys=True, ensure_ascii=False)
            cur = db.execute("""INSERT OR IGNORE INTO events
                (source_id, update_id, chat_id, message_id, event_type, revision,
                 emitted_at, received_at, reply_to_message_id, text, content_hash, content_kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (config.source_id, update["update_id"], chat_id, mid,
                 "edit" if key.startswith("edited_") else "new", revision,
                 datetime.fromtimestamp(date, timezone.utc).isoformat(), utc_now(), reply_id,
                 text, hashlib.sha256(canonical.encode()).hexdigest(), kind))
            inserted += cur.rowcount
        if updates:
            next_offset = max(update["update_id"] for update in updates) + 1
            prior = db.execute("SELECT value FROM metadata WHERE key='next_offset'").fetchone()
            next_offset = max(next_offset, int(prior[0]) if prior else 0)
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('next_offset', ?)", (str(next_offset),))
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('last_poll_at', ?)", (utc_now(),))
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('last_error', '')")
    return {"inserted": inserted, "ignored": ignored}


def list_events(config: Config, after_sequence: int = 0, limit: int = 20) -> dict:
    if type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid cursor or limit (1..100)")
    if not config.database.exists():
        return {"items": [], "next_cursor": after_sequence, "mode": "observe"}
    with connect(config.database, readonly=True) as db:
        rows = db.execute("SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT ?", (after_sequence, limit)).fetchall()
    items = [dict(row, state="observed", trusted_instructions=False) for row in rows]
    return {"items": items, "next_cursor": items[-1]["sequence"] if items else after_sequence, "mode": "observe"}


def get_status(config: Config) -> dict:
    info = metadata(config)
    count = 0
    if config.database.exists():
        with connect(config.database, readonly=True) as db:
            count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return {"mode": "observe", "execution_supported": False, "source_id": config.source_id,
            "configured": not config.blockers(), "blockers": config.blockers(),
            "allowed_chat_count": len(config.allowed_chat_ids), "event_count": count,
            "receiver_state": info.get("receiver_state", "not_started"),
            "last_poll_at": info.get("last_poll_at"), "last_error": info.get("last_error"),
            "next_offset": info.get("next_offset"), "as_of": utc_now()}
