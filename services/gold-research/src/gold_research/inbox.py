"""Parsed event ledger and explicit per-consumer receipts, separate from raw input."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3


class Inbox:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA foreign_keys=ON')
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS evaluations (event_sequence INTEGER PRIMARY KEY, signal_id TEXT NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL, event_json TEXT NOT NULL, result_json TEXT NOT NULL, evaluated_at TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS signal_identity ON evaluations(signal_id, revision)')
            db.execute('CREATE TABLE IF NOT EXISTS receipts (consumer TEXT NOT NULL, event_sequence INTEGER NOT NULL REFERENCES evaluations(event_sequence), at TEXT NOT NULL, PRIMARY KEY(consumer, event_sequence))')

    def save(self, event, result):
        identity = f"{event['source_id']}:{event['chat_id']}:{event['message_id']}"
        if event.get('reply_to_message_id'):
            result = {**result, 'reply_signal_id': f"{event['source_id']}:{event['chat_id']}:{event['reply_to_message_id']}",
                      'correlation_requires_verification': True}
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO evaluations VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (event['sequence'], identity, event['revision'], result['state'], json.dumps(event), json.dumps(result), datetime.now(timezone.utc).isoformat()))

    @staticmethod
    def decode(row):
        item = dict(row)
        item['event'] = json.loads(item.pop('event_json'))
        item['result'] = json.loads(item.pop('result_json'))
        item['content_is_external'] = True
        emitted = datetime.fromisoformat(item['event']['emitted_at'])
        item['event_age_seconds'] = (datetime.now(timezone.utc)-emitted).total_seconds()
        return item

    @staticmethod
    def consumer(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
            raise ValueError('Invalid consumer identifier')
        return value

    def pending(self, consumer, limit=20):
        self.consumer(consumer)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be 1..100')
        with self.db() as db:
            rows = db.execute("SELECT * FROM evaluations e WHERE state!='noise' AND NOT EXISTS (SELECT 1 FROM receipts r WHERE r.consumer=? AND r.event_sequence=e.event_sequence) ORDER BY event_sequence LIMIT ?", (consumer, limit)).fetchall()
        return [self.decode(row) for row in rows]

    def ack(self, consumer, sequences):
        self.consumer(consumer)
        if not isinstance(sequences, list) or len(sequences) > 100 or any(type(seq) is not int or seq <= 0 for seq in sequences):
            raise ValueError('Expected at most 100 positive event sequences')
        with self.db() as db:
            for seq in sequences:
                if not db.execute('SELECT 1 FROM evaluations WHERE event_sequence=?', (seq,)).fetchone():
                    raise ValueError('Unknown event sequence')
                db.execute('INSERT OR IGNORE INTO receipts VALUES (?, ?, ?)', (consumer, seq, datetime.now(timezone.utc).isoformat()))
        return {'acknowledged': sorted(set(sequences)), 'consumer_id': consumer, 'execution_authorized': False}

    def signal(self, identity):
        with self.db() as db:
            rows = db.execute('SELECT * FROM evaluations WHERE signal_id=? ORDER BY event_sequence', (identity,)).fetchall()
        return {'signal_id': identity, 'revisions': [self.decode(row) for row in rows]}

    def search(self, query='', state=None, limit=20):
        if not isinstance(query, str) or len(query) > 200 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid search or limit')
        with self.db() as db:
            rows = db.execute("SELECT * FROM evaluations WHERE state!='noise' AND (? IS NULL OR state=?) AND instr(lower(event_json), lower(?))>0 ORDER BY event_sequence DESC LIMIT ?", (state, state, query, limit)).fetchall()
        return [self.decode(row) for row in rows]

    def last_sequence(self):
        with self.db() as db:
            return db.execute('SELECT COALESCE(MAX(event_sequence),0) FROM evaluations').fetchone()[0]
