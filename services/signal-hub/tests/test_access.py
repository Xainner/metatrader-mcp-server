import tempfile
from pathlib import Path
import unittest
from trading_signal_hub.config import Config
from trading_signal_hub.telegram import ApiError, check_chat_access


class AccessTests(unittest.TestCase):
    def test_checks_membership_privacy_and_denied_chats_without_polling_or_sending(self):
        calls=[]
        class FakeAPI:
            def call(self, method, parameters=None):
                calls.append(method)
                if method=='getMe':
                    return {'id':123,'is_bot':True,'username':'fake_bot','can_read_all_group_messages':False}
                chat=parameters['chat_id']
                if chat == -1003:
                    raise ApiError('TELEGRAM_HTTP_400')
                if method=='getChat':
                    return {'id':chat,'title':'Synthetic','type':'channel' if chat==-1001 else 'supergroup'}
                if method=='getChatMember':
                    return {'status':'member'}
                raise AssertionError('Unexpected method')
        with tempfile.TemporaryDirectory() as directory:
            config=Config(Path(directory),'test',True,frozenset({-1001,-1002,-1003}),25)
            output=check_chat_access(config,[-1001,-1002,-1003],FakeAPI())
        items={item['chat_id']:item for item in output['chats']}
        self.assertTrue(items[-1001]['can_receive_messages'])
        self.assertEqual(items[-1002]['blocker'],'GROUP_PRIVACY_MODE')
        self.assertFalse(items[-1003]['access_verified'])
        self.assertEqual(items[-1003]['blocker'],'TELEGRAM_HTTP_400')
        self.assertNotIn('getUpdates',calls)
        self.assertEqual(output['messages_sent'],0)
