from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from gold_research.analysis import Research
from gold_research.inbox import Inbox
from gold_research.runtime import process_batch


class SpanishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.research = Research(Path(os.environ['GOLD_FOREX_REPO']))

    def test_spanish_buy_sell_and_decimal_comma(self):
        for direction, entry, sl, tp in [('COMPRA', '2500,50 - 2502,00', '2495,00', '2510,50'),
                                         ('VENTA', '2500 - 2502', '2510', '2490')]:
            result = self.research.parse(f'{direction} ORO XAUUSDm\nEntrada: {entry}\nStop loss: {sl}\nObjetivo 1: {tp}')
            self.assertEqual(result['state'], 'parsed', result)
            self.assertEqual(result['signal']['direction'], 'BUY' if direction == 'COMPRA' else 'SELL')
            self.assertFalse(result['execution_authorized'])

    def test_no_invented_levels_or_ambiguous_prices(self):
        for text in ['COMPRA ORO ahora\nSL: 2495\nTP1: 2510',
                     'COMPRA ORO\nEntrada: 2,500\nSL: 2495\nTP1: 2510',
                     'COMPRA ORO\nEntrada: 2500\nSL: 2495\nTP1: 2510\nTP1: 2520',
                     'Si llega a entrada compra ORO\nEntrada: 2500\nSL: 2495\nTP1: 2510',
                     'NO COMPRAR ORO\nEntrada: 2500\nSL: 2495\nTP1: 2510']:
            self.assertEqual(self.research.parse(text)['state'], 'quarantined', text)
        self.assertEqual(self.research.parse('MOVER SL A BE')['state'], 'management_instruction')
        bad = self.research.parse('COMPRA ORO\nEntrada: 2500\nSL: 2495\nTP1: 2490')
        self.assertEqual(bad['state'], 'quarantined')
        self.assertEqual(bad['signal']['tp1'], 2490)


class MultiSourceTests(unittest.TestCase):
    def test_colliding_raw_ids_restart_and_old_receipts_survive_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = Inbox(Path(directory)/'inbox.sqlite3')
            inbox.initialize()
            paths = []
            for source in ('telegram-main', 'whatsapp-main'):
                path = Path(directory)/(source+'.sqlite3')
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute('CREATE TABLE events(sequence INTEGER,source_id TEXT,chat_id TEXT,message_id TEXT,revision INTEGER,emitted_at TEXT,received_at TEXT,content_kind TEXT,text TEXT)')
                    db.execute('INSERT INTO events VALUES (1,?,?,?,?,?,?,?,?)', (source, '123@g.us', 'msg', 1, '2026-10-21T00:00:00Z','2026-10-21T00:00:00Z','text','COMPRA ORO'))
                paths.append({'source_id':source,'raw_database':str(path)})
            class FakeResearch:
                def parse(self, text):
                    return {'state':'quarantined','execution_authorized':False}
            self.assertEqual(process_batch({'sources': paths},FakeResearch(),inbox),2)
            inbox.ack('rinari',[1])
            self.assertEqual([item['event_sequence'] for item in inbox.pending('rinari')],[2])
            self.assertEqual(process_batch({'sources': paths},FakeResearch(),inbox),0)
            # Simulate a legacy database without the new import identity table.
            with inbox.db() as db:
                db.execute('DROP TABLE imports')
            inbox.initialize()
            self.assertEqual(process_batch({'sources': paths},FakeResearch(),inbox),0)
            self.assertEqual([item['event_sequence'] for item in inbox.pending('rinari')],[2])
            self.assertEqual(inbox.source_sequence('whatsapp-main'),1)
