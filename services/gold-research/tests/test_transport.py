import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_digest_is_truly_empty_until_new_signal_and_after_explicit_ack(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from gold_research.inbox import Inbox

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home/'config.json').write_text(json.dumps({'upstream_path': os.environ['GOLD_FOREX_REPO'], 'raw_database': str(home/'unused.sqlite3'), 'source_id': 'test'}))
            inbox = Inbox(home/'data/research.sqlite3')
            inbox.initialize()
            params = StdioServerParameters(command=sys.executable, args=['-m', 'gold_research', '--home', directory, 'mcp'], env=dict(os.environ))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    self.assertEqual(len(tools), 7)
                    empty = await session.call_tool('peek_signal_digest', {})
                    self.assertEqual(empty.content[0].text, '')
                    self.assertFalse(empty.structuredContent)
                    inbox.save({'sequence': 1, 'source_id': 'test', 'chat_id': 1, 'message_id': 1, 'revision': 1,
                                'emitted_at': '2026-10-21T12:00:00Z'}, {'state': 'parsed', 'signal': {}, 'execution_authorized': False})
                    fresh = await session.call_tool('peek_signal_digest', {})
                    self.assertTrue(fresh.content[0].text)
                    ack = await session.call_tool('ack_signal_events', {'event_sequences': [1]})
                    self.assertFalse(ack.isError)
                    empty = await session.call_tool('peek_signal_digest', {})
                    self.assertEqual(empty.content[0].text, '')
                    failed = await session.call_tool('ack_signal_events', {'event_sequences': [999]})
                    self.assertTrue(failed.isError)


if __name__ == '__main__':
    unittest.main()
