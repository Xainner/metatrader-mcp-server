"""Private configuration and observe-only processing of the existing receiver."""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import threading

from .analysis import Research
from .inbox import Inbox


def load(home=None):
    home = (home or Path(os.environ.get('GOLD_RESEARCH_HOME', '~/trading/gold-research'))).expanduser().resolve()
    config = json.loads((home / 'config.json').read_text(encoding='utf-8'))
    research = Research(Path(config['upstream_path']))
    inbox = Inbox(home / 'data' / 'research.sqlite3')
    return home, config, research, inbox


def process_batch(config, research, inbox):
    sources = config.get('sources') or [{'raw_database': config['raw_database'], 'source_id': config['source_id']}]
    return sum(process_source(source, research, inbox) for source in sources)


def process_source(config, research, inbox):
    path = Path(config['raw_database']).resolve()
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=10)
    try:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT 100', (inbox.source_sequence(config['source_id']),)).fetchall()
    finally:
        db.close()
    for row in rows:
        event = dict(row)
        if event['source_id'] != config['source_id']:
            raise ValueError('Raw source identity changed')
        try:
            result = research.parse(event['text']) if event['content_kind'] == 'text' else {
                'state': 'quarantined', 'errors': ['Media requires separate review'], 'signal': None, 'execution_authorized': False}
            if event['content_kind'] == 'revoked':
                result = {'state': 'management_instruction', 'errors': ['Source message revoked; review correlated trade, never automatically close'], 'signal': None, 'execution_authorized': False}
        except ValueError:
            result = {'state': 'quarantined', 'errors': ['Input exceeds parser contract'], 'signal': None, 'execution_authorized': False}
        inbox.save(event, result)
    return len(rows)


def status(config, inbox):
    with inbox.db() as db:
        states = dict(db.execute('SELECT state, COUNT(*) FROM evaluations GROUP BY state'))
        latest = db.execute('SELECT MAX(evaluated_at) FROM evaluations').fetchone()[0]
    receivers = []
    for source in config.get('sources', []):
        if source.get('status_file'):
            try:
                receiver = json.loads(Path(source['status_file']).read_text())
                age = (datetime.now(timezone.utc)-datetime.fromisoformat(receiver['as_of'].replace('Z','+00:00'))).total_seconds()
                receivers.append({**receiver, 'heartbeat_stale': age > 30 or age < -5})
            except (OSError, ValueError, KeyError):
                receivers.append({'source_id': source['source_id'], 'state': 'unavailable', 'heartbeat_stale': True})
    return {'mode': 'research_only', 'forex_gold_execution': False, 'telegram_sender': False,
            'receivers': receivers,
            'execution_route': 'metatrader-demo in Windows', 'source_id': config['source_id'],
            'sources': [s['source_id'] for s in config.get('sources', [{'source_id': config['source_id']}])],
            'evaluations_by_state': states, 'last_evaluated_at': latest,
            'upstream_commit': __import__('gold_research.analysis', fromlist=['UPSTREAM_COMMIT']).UPSTREAM_COMMIT,
            'capabilities': ['telegram_and_whatsapp_spanish_parse_and_validation', 'durable_digest_and_ack', 'signal_search',
                             'h1_h4_bias', 'atr_adx_macd', 'key_levels', 'breakout_go', 'breakout_retest', 'liquidity_sweep'],
            'not_enabled': ['forex_gold_execution', 'remote_administration', 'ml_prediction', 'news_feed'],
            'as_of': datetime.now(timezone.utc).isoformat()}


def run_processor(home=None):
    home, config, research, inbox = load(home)
    inbox.initialize()
    import fcntl
    stop = threading.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, lambda *_: stop.set())
    with (home / 'data' / 'processor.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while not stop.is_set():
            try:
                process_batch(config, research, inbox)
            except (sqlite3.Error, OSError):
                logging.warning('Raw event storage unavailable; retrying without advancing cursor')
            stop.wait(1)
