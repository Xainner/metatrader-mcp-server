"""Additional read/calculation tools for the existing Windows demo MCP.

AGPL-3.0-only, part of the gold-research integration. No order_send calls.
"""

from decimal import Decimal, ROUND_FLOOR
import json
import math
import subprocess
import time


def register(mcp, mt5, lock, ensure_demo):
    from mcp.types import ToolAnnotations
    from metatrader_client.order._validation import select_filling_mode, validate_volume, check_stops, normalize_price

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    def symbol_info(symbol):
        if symbol != 'XAUUSDm':
            raise ValueError('This integration is scoped to XAUUSDm')
        if not mt5.symbol_select(symbol, True):
            raise ValueError('XAUUSDm cannot be selected in this terminal')
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError('Symbol specifications unavailable')
        return info

    def quote(symbol):
        value = mt5.symbol_info_tick(symbol)
        if value is None or value.bid <= 0 or value.ask < value.bid:
            raise RuntimeError('Current MT5 quote unavailable')
        return value

    @mcp.tool(annotations=read)
    def get_symbol_specs(symbol: str = 'XAUUSDm') -> dict:
        """Read actual symbol contract, tick, lot, stops/freeze and filling specifications."""
        with lock:
            account, _ = ensure_demo()
            info = symbol_info(symbol)
            fields = ('name', 'digits', 'point', 'trade_tick_size', 'trade_tick_value',
                      'trade_contract_size', 'volume_min', 'volume_max', 'volume_step', 'volume_limit',
                      'trade_stops_level', 'trade_freeze_level', 'trade_mode', 'trade_exemode',
                      'filling_mode', 'currency_base', 'currency_profit', 'currency_margin')
            return {'symbol': symbol, 'as_of': time.time(), 'account_currency': account.currency,
                    'specifications': {key: getattr(info, key, None) for key in fields}}

    @mcp.tool(annotations=read)
    def get_trading_snapshot(symbol: str = 'XAUUSDm') -> dict:
        """Read account/quote/positions/orders with explicit component failures, never fabricated zeros."""
        with lock:
            account, terminal = ensure_demo()
            symbol_info(symbol)
            started = time.time()
            tick = mt5.symbol_info_tick(symbol)
            positions = mt5.positions_get(symbol=symbol)
            orders = mt5.orders_get(symbol=symbol)
            errors = [name for name, value in [('tick', tick), ('positions', positions), ('pending_orders', orders)] if value is None]
            return {'status': 'partial' if errors else 'ok', 'symbol': symbol,
                    'capture_started': started, 'capture_finished': time.time(), 'errors': errors,
                    'account': {key: getattr(account, key, None) for key in ('balance', 'equity', 'profit', 'margin', 'margin_free', 'currency')},
                    'connected': terminal.connected, 'tick': tick._asdict() if tick else None,
                    'positions': [item._asdict() for item in positions] if positions is not None else None,
                    'pending_orders': [item._asdict() for item in orders] if orders is not None else None}

    @mcp.tool(annotations=read)
    def get_market_context(symbol: str = 'XAUUSDm', count: int = 160) -> dict:
        """Capture real OHLC M1/M5/M15/H1/H4 including forming bars for clock-aware research."""
        if type(count) is not int or not 80 <= count <= 500:
            raise ValueError('count must be 80..500')
        with lock:
            ensure_demo()
            symbol_info(symbol)
            tick = quote(symbol)
            context = {'symbol': symbol, 'source': 'MT5_terminal', 'captured_at': time.time(),
                       'tick': tick._asdict(), 'candles': {}, 'bars_include_forming': True}
            for name in ('M1', 'M5', 'M15', 'H1', 'H4'):
                rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, 'TIMEFRAME_'+name), 0, count)
                if rates is None:
                    raise RuntimeError('MT5 candles unavailable for '+name)
                context['candles'][name] = [{'ts': int(row['time']), **{key: float(row[key]) for key in ('open', 'high', 'low', 'close')}} for row in rates]
            return context

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
    def get_gold_research(count: int = 160) -> dict:
        """Capture MT5 data and compute forex-gold analysis/candidates on owner-authorized server2."""
        context = get_market_context(count=count)
        # SSH executes outside the MT5 lock: network analysis must not hold up a close tool.
        result = subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', 'server2',
                                 '/home/xainner/trading/gold-research/.venv/bin/python', '-m', 'gold_research', 'analyze'],
                                input=json.dumps(context, allow_nan=False), capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError('Remote research unavailable; no broker action was requested')
        return json.loads(result.stdout)

    @mcp.tool(annotations=read)
    def calculate_trade_risk(direction: str, stop_loss: float, risk_usd: float | None = None, volume: float | None = None) -> dict:
        """Calculate stop loss exposure and margin from MT5, not a hardcoded gold pip value. No authorization."""
        direction = direction.upper()
        if direction not in ('BUY', 'SELL') or not math.isfinite(stop_loss) or stop_loss <= 0:
            raise ValueError('Valid BUY/SELL and positive finite stop_loss required')
        if (risk_usd is None) == (volume is None):
            raise ValueError('Provide exactly one of risk_usd or volume')
        with lock:
            account, _ = ensure_demo()
            info = symbol_info('XAUUSDm')
            tick = quote('XAUUSDm')
            side = mt5.ORDER_TYPE_BUY if direction == 'BUY' else mt5.ORDER_TYPE_SELL
            entry = tick.ask if direction == 'BUY' else tick.bid
            if (direction == 'BUY' and stop_loss >= entry) or (direction == 'SELL' and stop_loss <= entry):
                raise ValueError('Stop loss is on the wrong side of the current entry')
            one_lot = mt5.order_calc_profit(side, 'XAUUSDm', 1.0, entry, stop_loss)
            if one_lot is None or not math.isfinite(one_lot) or one_lot >= 0:
                raise RuntimeError('MT5 loss calculation unavailable')
            if risk_usd is not None:
                if account.currency != 'USD' or not math.isfinite(risk_usd) or risk_usd <= 0:
                    raise ValueError('risk_usd requires a USD account and positive finite budget')
                steps = (Decimal(str(risk_usd))/Decimal(str(abs(one_lot)))/Decimal(str(info.volume_step))).to_integral_value(rounding=ROUND_FLOOR)
                volume = float(steps*Decimal(str(info.volume_step)))
            volume, error = validate_volume(info, volume)
            if error:
                return {'status': 'volume_not_feasible', 'reason': error, 'execution_authorized': False}
            loss = mt5.order_calc_profit(side, 'XAUUSDm', volume, entry, stop_loss)
            margin = mt5.order_calc_margin(side, 'XAUUSDm', volume, entry)
            if loss is None or margin is None:
                raise RuntimeError('MT5 risk or margin calculation unavailable')
            return {'status': 'calculated', 'symbol': 'XAUUSDm', 'direction': direction, 'volume': volume,
                    'entry': entry, 'stop_loss': stop_loss, 'estimated_loss': abs(loss), 'estimated_margin': margin,
                    'currency': account.currency, 'execution_authorized': False,
                    'excludes': ['unknown_commissions', 'future_slippage', 'gaps']}

    @mcp.tool(annotations=read)
    def preview_order(direction: str, volume: float, stop_loss: float, take_profit: float) -> dict:
        """Validate an explicit market request and call MT5 order_check without sending an order."""
        direction = direction.upper()
        if direction not in ('BUY', 'SELL'):
            raise ValueError('direction must be BUY or SELL')
        if not all(math.isfinite(float(value)) and float(value) > 0 for value in (volume, stop_loss, take_profit)):
            raise ValueError('Volume, SL and TP must be positive finite numbers')
        with lock:
            ensure_demo()
            info = symbol_info('XAUUSDm')
            tick = quote('XAUUSDm')
            side = mt5.ORDER_TYPE_BUY if direction == 'BUY' else mt5.ORDER_TYPE_SELL
            normalized, error = validate_volume(info, volume)
            filling, filling_error = select_filling_mode(info, pending=False)
            sl, sl_error = normalize_price(info, stop_loss, 'Stop loss')
            tp, tp_error = normalize_price(info, take_profit, 'Take profit')
            error = error or filling_error or sl_error or tp_error
            if not error:
                error = check_stops(info, side, reference_price=tick.bid if direction == 'BUY' else tick.ask, stop_loss=sl, take_profit=tp)
            if error:
                return {'status': 'invalid', 'reason': error, 'execution_authorized': False}
            request = {'action': mt5.TRADE_ACTION_DEAL, 'symbol': 'XAUUSDm', 'volume': normalized,
                       'type': side, 'price': tick.ask if side == mt5.ORDER_TYPE_BUY else tick.bid,
                       'sl': sl, 'tp': tp, 'type_filling': filling, 'type_time': mt5.ORDER_TIME_GTC}
            check = mt5.order_check(request)
            state = 'check_unknown' if check is None else ('check_passed' if check.retcode == 0 else 'check_rejected')
            return {'status': state, 'check': check._asdict() if check is not None else None,
                    'request': request, 'execution_authorized': False, 'note': 'A favorable check does not guarantee execution'}

    return {'get_symbol_specs': get_symbol_specs, 'get_trading_snapshot': get_trading_snapshot,
            'get_market_context': get_market_context, 'get_gold_research': get_gold_research,
            'calculate_trade_risk': calculate_trade_risk, 'preview_order': preview_order}
