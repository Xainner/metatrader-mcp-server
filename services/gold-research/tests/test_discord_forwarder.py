from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from gold_research.inbox import Inbox
from gold_research import discord_forwarder as relay


class DiscordRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)/'relay'
        self.inbox=Inbox(Path(self.temp.name)/'research.sqlite3')
        self.inbox.initialize()
        relay.initialize(self.home,self.inbox.path)

    def event(self,sequence=1,source='telegram-fxtradingvision',**changes):
        stamp=datetime.now(timezone.utc).isoformat()
        return {'sequence':sequence,'source_id':source,'chat_id':-10042,'message_id':10,'revision':1,
                'emitted_at':stamp,'received_at':stamp,'text':'BUY GOLD 2500\nSL 2495\nTP 2510 @everyone ```',
                'event_type':'new','content_kind':'text','reply_to_message_id':None,**changes}

    def save(self,event=None,state='parsed'):
        self.inbox.save(event or self.event(),{'state':state,'execution_authorized':False})
        relay.enqueue(self.home,self.inbox.path)

    def rows(self):
        with relay.database(self.home/'data/deliveries.sqlite3') as db:
            return [dict(row) for row in db.execute('SELECT * FROM deliveries ORDER BY sequence')]

    def test_both_sources_revision_mentions_and_independent_receipts(self):
        self.save()
        self.save(self.event(source='whatsapp-main'))
        self.save(self.event(sequence=2,revision=2,event_type='edit',text='SL 2490'))
        self.assertEqual(len(self.rows()),3)
        body=json.loads(self.rows()[0]['payload'])
        self.assertEqual(body['allowed_mentions'],{'parse':[]})
        self.assertIn('FXTradingVision',body['embeds'][0]['title'])
        relay.enqueue(self.home,self.inbox.path)
        self.assertEqual(len(self.rows()),3)
        self.assertEqual(len(self.inbox.pending('rinari-xau-signals')),3)

    def test_ignores_history_noise_other_sources_and_late_import_of_old_events(self):
        self.save(self.event(received_at='2020-01-01T00:00:00+00:00'))
        self.save(self.event(sequence=2),state='noise')
        self.save(self.event(sequence=3,source='not-authorized'))
        self.assertEqual(self.rows(),[])

    def test_initialization_preserves_cursor_and_skips_existing_history(self):
        self.inbox.save(self.event(),{'state':'parsed'})
        other=Path(self.temp.name)/'other'
        relay.initialize(other,self.inbox.path)
        relay.enqueue(other,self.inbox.path)
        with relay.database(other/'data/deliveries.sqlite3') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0],0)

    def test_confirmed_delivery_not_sent_twice(self):
        self.save()
        calls=[]
        def send(url,body):
            calls.append(body)
            return 200,{}, {'id':'discord-message'}
        self.assertTrue(relay.deliver_one(self.home,'unused',send))
        self.assertFalse(relay.deliver_one(self.home,'unused',send))
        self.assertEqual(len(calls),1)
        self.assertEqual(self.rows()[0]['state'],'sent')

    def test_rate_limit_preserves_queue_and_honors_retry(self):
        self.save()
        relay.deliver_one(self.home,'unused',lambda *_:(429,{}, {'retry_after':30}))
        self.assertEqual(self.rows()[0]['state'],'pending')
        self.assertFalse(relay.deliver_one(self.home,'unused',lambda *_:self.fail('Too early')))

    def test_timeout_and_interrupted_send_are_not_blindly_retried(self):
        self.save()
        relay.deliver_one(self.home,'unused',lambda *_:(0,{},{}))
        self.assertEqual(self.rows()[0]['state'],'uncertain')
        self.assertFalse(relay.deliver_one(self.home,'unused',lambda *_:self.fail('Duplicate')))
        with relay.database(self.home/'data/deliveries.sqlite3') as db:
            db.execute("UPDATE deliveries SET state='sending'")
        relay.recover(self.home)
        self.assertEqual(self.rows()[0]['state'],'uncertain')

    def test_secret_url_restriction(self):
        (self.home/'.env').write_text('DISCORD_WEBHOOK_URL=https://example.com/api/webhooks/1/token')
        with self.assertRaises(ValueError): relay.webhook(self.home)


if __name__=='__main__': unittest.main()
