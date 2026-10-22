"""Deployment contracts; no terminal connection or broker send is performed."""
import asyncio
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch, Mock


@unittest.skipUnless(os.name == 'nt', 'Windows deployment')
class DemoAdapterTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('demo_adapter_test', Path(__file__).resolve().parents[1]/'demo_server.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.account = SimpleNamespace(login=1, server='demo', trade_mode=0, trade_allowed=True, trade_expert=True)
        self.terminal = SimpleNamespace(connected=True, trade_allowed=True, tradeapi_disabled=False)

    def test_native_schemas_are_preserved_and_context_is_hidden(self):
        async def check():
            tools = {t.name: t for t in await self.module.mcp.list_tools()}
            self.assertEqual(len(tools), 50)
            self.assertIn('type', tools['preview_order'].inputSchema['required'])
            self.assertNotIn('direction', tools['preview_order'].inputSchema['properties'])
            self.assertIn('symbol', tools['get_trading_snapshot'].inputSchema['required'])
            for tool in tools.values():
                self.assertNotIn('ctx', tool.inputSchema['properties'])
            for name in self.module.RESEARCH_TOOLS:
                self.assertTrue(tools[name].annotations.readOnlyHint)
        asyncio.run(check())

    def test_last_send_boundary_rejects_live_switched_and_disabled_accounts(self):
        mod = self.module
        send = Mock(return_value='mock-only')
        with patch.object(mod.mt5, 'account_info', return_value=self.account), patch.object(mod.mt5, 'terminal_info', return_value=self.terminal), patch.object(mod, 'REAL_ORDER_SEND', send):
            mod.BOUND_ACCOUNT = (1, 'demo')
            self.account.trade_mode = mod.mt5.ACCOUNT_TRADE_MODE_REAL
            with self.assertRaises(RuntimeError): mod.checked_order_send({})
            self.account.trade_mode = mod.mt5.ACCOUNT_TRADE_MODE_DEMO
            self.account.login = 2
            with self.assertRaises(RuntimeError): mod.checked_order_send({})
            self.account.login = 1
            self.terminal.tradeapi_disabled = True
            with self.assertRaises(RuntimeError): mod.checked_order_send({})
            send.assert_not_called()
            self.terminal.tradeapi_disabled = False
            self.assertEqual(mod.checked_order_send({}), 'mock-only')
            send.assert_called_once()

    def test_research_network_does_not_occupy_mt5_worker(self):
        async def check():
            mod = self.module
            entered, release = threading.Event(), threading.Event()
            bridge = mod.DemoBridge(None)
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(bridge=bridge)))
            def analyze(context):
                entered.set()
                if not release.wait(3): raise RuntimeError('test timed out')
                return {'status': 'ok'}
            with patch.object(mod, 'ensure_demo'), patch.dict(mod.legacy, get_market_context=lambda **kw: {}), patch.object(mod.research, 'analyze_market_context', analyze):
                task = asyncio.create_task(mod.get_gold_research(ctx))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    self.assertEqual(await asyncio.wait_for(bridge.run('read_probe', 'read', lambda: 7), .5), 7)
                finally:
                    release.set()
                    await task
                    bridge.shutdown()
        asyncio.run(check())
