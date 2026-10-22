import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest


@unittest.skipUnless(os.name == 'nt', 'Windows MT5 client adapter')
class WindowsToolsTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1]/'client/windows_tools.py'
        spec = importlib.util.spec_from_file_location('windows_research_tools', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        class MCP:
            def tool(self, **kwargs):
                return lambda function: function
        self.mt5 = SimpleNamespace(
            symbol_select=lambda *_: True,
            symbol_info=lambda _: SimpleNamespace(name='XAUUSDm', volume_min=.01, volume_max=100., volume_step=.01,
                                                  filling_mode=1, trade_exemode=2, trade_stops_level=0, point=.01, digits=2, trade_tick_size=.01),
            symbol_info_tick=lambda _: SimpleNamespace(bid=2500., ask=2500.2),
            order_calc_profit=lambda side, symbol, volume, entry, stop: volume*100*(stop-entry)*(1 if side == 0 else -1),
            order_calc_margin=lambda side, symbol, volume, entry: volume*100,
            ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1, TRADE_ACTION_DEAL=1, ORDER_TIME_GTC=0,
            order_check=lambda request: SimpleNamespace(retcode=0, _asdict=lambda: {'retcode': 0}))
        self.tools = module.register(MCP(), self.mt5, threading.RLock(), lambda: (SimpleNamespace(currency='USD'), None))

    def test_risk_sizing_rounds_down_and_never_calls_order_send(self):
        result = self.tools['calculate_trade_risk']('BUY', 2495, risk_usd=10)
        self.assertEqual(result['volume'], .01)
        self.assertLessEqual(result['estimated_loss'], 10)
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(self.tools['calculate_trade_risk']('BUY', 2495, risk_usd=1)['status'], 'volume_not_feasible')

    def test_preview_calls_check_and_rejects_wrong_side_levels(self):
        self.assertEqual(self.tools['preview_order']('BUY', .01, 2495, 2510)['status'], 'check_passed')
        self.assertEqual(self.tools['preview_order']('BUY', .01, 2510, 2515)['status'], 'invalid')

    def test_nonfinite_budget_and_other_symbol_are_rejected(self):
        with self.assertRaises(ValueError):
            self.tools['calculate_trade_risk']('BUY', 2495, risk_usd=float('nan'))
        with self.assertRaises(ValueError):
            self.tools['get_symbol_specs']('EURUSD')


if __name__ == '__main__':
    unittest.main()
