import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


@unittest.skipUnless(importlib.util.find_spec("mcp"), "Install the mcp extra for transport tests")
class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_tools_are_readonly_and_queries_leave_database_unchanged(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from trading_signal_hub.config import load_config
        from trading_signal_hub.store import initialize, metadata

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.json").write_text(json.dumps({"source_id": "test", "enabled": False, "allowed_chat_ids": []}))
            config = load_config(home)
            initialize(config)
            before = metadata(config)
            environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            params = StdioServerParameters(command=sys.executable, args=["-m", "trading_signal_hub", "--home", directory, "mcp"], env=environment)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    self.assertEqual({tool.name for tool in tools}, {"get_signal_hub_status", "list_signal_events", "get_signal_event"})
                    self.assertTrue(all(tool.annotations.readOnlyHint and not tool.annotations.openWorldHint for tool in tools))
                    for _ in range(5):
                        status = await session.call_tool("get_signal_hub_status", {})
                        self.assertFalse(status.isError)
                    events = await session.call_tool("list_signal_events", {"after_sequence": 0, "limit": 10})
                    self.assertFalse(events.isError)
                    self.assertFalse(json.loads(status.content[0].text)["execution_supported"])
            self.assertEqual(metadata(config), before)


if __name__ == "__main__":
    unittest.main()
