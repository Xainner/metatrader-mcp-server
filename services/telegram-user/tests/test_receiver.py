from datetime import datetime, timezone, timedelta
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest

SERVICES=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(SERVICES/'signal-hub/src'),str(SERVICES/'gold-research/src')]
spec=importlib.util.spec_from_file_location('telegram_personal_test',SERVICES/'telegram-user/src/trading_telegram_user/__main__.py')
receiver=importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)
        self.started=datetime.now(timezone.utc)-timedelta(minutes=2)
        self.config={'target':'FXTradingVision','source_id':'telegram-fxtradingvision','peer_id':-10042,'capture_after':self.started.isoformat()}
        receiver.initialize(self.home,self.config)

    def message(self,text='BUY GOLD 2500\nSL 2495\nTP 2510',**changes):
        return SimpleNamespace(**{'date':datetime.now(timezone.utc),'message':text,'id':1,'reply_to_msg_id':None,**changes})

    def test_idempotence_revisions_and_original_age(self):
        message=self.message()
        self.assertEqual(receiver.save_message(self.home,self.config,message,-10042),1)
        self.assertEqual(receiver.save_message(self.home,self.config,message,-10042),0)
        edited=self.message(text='BUY GOLD 2500\nSL 2490\nTP 2510',date=message.date)
        self.assertEqual(receiver.save_message(self.home,self.config,edited,-10042),1)
        with closing(sqlite3.connect(self.home/'data/signals.sqlite3')) as db:
            rows=db.execute('SELECT revision,event_type,emitted_at FROM events ORDER BY sequence').fetchall()
        self.assertEqual([r[:2] for r in rows],[(1,'new'),(2,'edit')])
        self.assertTrue(all(r[2]==message.date.isoformat() for r in rows))

    def test_wrong_chat_rejected_and_old_history_not_imported(self):
        with self.assertRaises(ValueError): receiver.save_message(self.home,self.config,self.message(),-10043)
        self.assertEqual(receiver.save_message(self.home,self.config,self.message(date=self.started-timedelta(seconds=1)),-10042),0)

    def test_media_quarantine_and_reply_identity(self):
        receiver.save_message(self.home,self.config,self.message(text='',reply_to_msg_id=7),-10042)
        with closing(sqlite3.connect(self.home/'data/signals.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT content_kind,reply_to_message_id FROM events').fetchone(),('media_or_service',7))

    def test_raw_to_research_digest_with_independent_ack(self):
        from gold_research.inbox import Inbox
        from gold_research.runtime import process_source
        receiver.save_message(self.home,self.config,self.message(),-10042)
        inbox=Inbox(self.home/'research.sqlite3')
        inbox.initialize()
        source={'source_id':self.config['source_id'],'raw_database':str(self.home/'data/signals.sqlite3')}
        parser=SimpleNamespace(parse=lambda text: {'state':'parsed','signal':{},'execution_authorized':False})
        self.assertEqual(process_source(source,parser,inbox),1)
        self.assertEqual(process_source(source,parser,inbox),0)
        rows=inbox.pending('signals')
        self.assertEqual(len(rows),1)
        self.assertIn('telegram-fxtradingvision:',rows[0]['signal_id'])
        inbox.ack('signals',[rows[0]['event_sequence']])
        self.assertEqual(inbox.pending('signals'),[])
        self.assertEqual(len(inbox.pending('other-agent')),1)

    def test_status_distinguishes_login_from_empty_feed(self):
        receiver.status(self.home,self.config,'needs_login')
        data=json.loads((self.home/'data/status.json').read_text())
        self.assertFalse(data['authenticated'])
        self.assertFalse(data['receiver_ready'])
        self.assertEqual(data['stored_events'],0)
        receiver.status(self.home,self.config,'connected')
        self.assertTrue(json.loads((self.home/'data/status.json').read_text())['receiver_ready'])


if __name__=='__main__': unittest.main()
