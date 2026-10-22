"""Explicit composition of upstream pure functions. Never import its app."""

import hashlib
import importlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from types import FunctionType, SimpleNamespace
from .spanish import parse_spanish

UPSTREAM_COMMIT = '83d7faf8d7e131a9ed353c7920a2c456cc088807'
PERIODS = {'M1': 60, 'M5': 300, 'M15': 900, 'H1': 3600, 'H4': 14400}


def rebound(function, **globals_override):
    """Use upstream code with an explicitly isolated dependency environment."""
    function_copy = FunctionType(function.__code__, {**function.__globals__, **globals_override},
                                 function.__name__, function.__defaults__, function.__closure__)
    function_copy.__kwdefaults__ = function.__kwdefaults__
    return function_copy


class Research:
    def __init__(self, checkout: Path):
        checkout = checkout.resolve()
        revision = subprocess.run(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(['git', '-C', str(checkout), 'diff', '--name-only', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
        if revision != UPSTREAM_COMMIT or dirty:
            raise ValueError('forex-gold checkout differs from the reviewed revision')
        sys.path.insert(0, str(checkout))
        self.parser = importlib.import_module('backend.src.services.signals.parser')
        self.indicators = importlib.import_module('backend.src.services.market.indicators')
        self.levels = importlib.import_module('backend.src.services.market.levels')
        self.clock = importlib.import_module('backend.src.services.market.bar_clock')
        self.dpm = importlib.import_module('backend.src.services.dpm.engine')
        breakout = importlib.import_module('backend.src.services.breakout_signal.signal_generator')
        # No adaptive DB/owner settings: research uses the published parameter
        # defaults, explicitly identified below. No financial sizing is imported.
        params = importlib.import_module('backend.src.services.breakout_signal.adaptive_params')
        readonly_params = SimpleNamespace(get=lambda key: float(params.PARAMS[key]['default']))
        self.generators = {name: rebound(getattr(breakout, name), ap=readonly_params) for name in
                           ('check_breakout_go', 'check_breakout_retest', 'check_liquidity_sweep', 'calculate_breakout_risk_levels')}
        # Upstream optionally repairs/extrapolates TPs. Preserve provider input
        # for this integration: malformed levels must surface as validation errors.
        identity = lambda direction, low, high, raw: dict(raw)
        self.parsers = [rebound(getattr(self.parser, name), _autocorrect_tps=identity) for name in
                        ('parse_gold_signal', 'parse_gd2_signal', 'parse_limit_order_signal')]

    def parse(self, text: str) -> dict:
        if not isinstance(text, str) or len(text) > 12000:
            raise ValueError('Signal text must contain at most 12000 characters')
        result = {'state': 'noise', 'signal': None, 'errors': [], 'execution_authorized': False,
                  'parser': 'forex-gold', 'upstream_commit': UPSTREAM_COMMIT, 'tp_autocorrection': False}
        if re.search(r'\b(CLOSE|CERRAR|CIERRA|CERRAMOS|CIERRE|ASEGURAR|BREAKEVEN|BREAK EVEN|RISK FREE|SIN RIESGO|PUNTO DE ENTRADA|MOVER SL|SUBIR SL|BAJAR SL)\b|\bSL\s*(?:A|TO)?\s*BE\b', text, re.I):
            result.update(state='management_instruction', signal={'action_text': text, 'requires_correlated_trade': True},
                          errors=['Management requires an explicit correlated trade and current MT5 state'])
            return result
        if re.search(r'^\s*(SL|STOP LOSS|TP\d*|OBJETIVO\s*\d*)\s*[:=]?\s*\d', text, re.I):
            result.update(state='awaiting_correlation', errors=['Levels-only update requires an explicit reply or signal identity'])
            return result
        if not re.search(r'\b(ORO|GOLD|XAU\s*/?\s*USD(?:m)?)\b', text, re.I):
            return result
        # Broker suffix is an explicit alias; upstream's canonical instrument is XAUUSD.
        normalized = re.sub(r'\bXAUUSDm\b', 'XAUUSD', text, flags=re.I)
        spanish, candidate, spanish_errors = parse_spanish(text)
        if spanish:
            result['parser'] = 'spanish-explicit-v1 + forex-gold-validation'
            if not candidate:
                return {**result, 'state': 'quarantined', 'errors': spanish_errors}
        for parser in ([lambda _: candidate] if spanish else self.parsers):
            try:
                signal = parser(normalized)
            except (ValueError, TypeError, OverflowError):
                result.update(state='quarantined', errors=['Malformed numeric fields'])
                return result
            if signal:
                fields = [signal.get(key) for key in ('entry_low', 'entry_high', 'stop_loss')]
                tps = [signal.get(f'tp{n}') for n in range(1, 9)]
                numbers = [value for value in fields + tps if value is not None]
                errors = list(spanish_errors) if spanish else []
                if any(not math.isfinite(float(value)) or float(value) <= 0 for value in numbers):
                    errors.append('Prices must be positive finite numbers')
                if any(value is None for value in fields):
                    errors.append('Entry and stop loss are required')
                if not any(value is not None for value in tps):
                    errors.append('At least one explicit take profit is required')
                if not errors:
                    errors.extend(self.parser.validate_signal(signal['direction'], *fields, *tps))
                signal.update(canonical_symbol='XAUUSD', broker_symbol='XAUUSDm')
                result.update(state='quarantined' if errors else 'parsed', signal=signal, errors=errors)
                return result
        if re.search(r'\b(BUY|SELL|COMPRA|VENTA)\b', normalized, re.I):
            result.update(state='awaiting_fields', errors=['No complete supported entry/SL/TP layout'])
        return result

    def analyze(self, context: dict, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        if not isinstance(context, dict) or context.get('symbol') != 'XAUUSDm':
            raise ValueError('Only an explicit XAUUSDm context is accepted')
        result = {'status': 'data_unknown', 'symbol': 'XAUUSDm', 'execution_authorized': False,
                  'upstream_commit': UPSTREAM_COMMIT, 'candidates': [], 'as_of': now,
                  'source': context.get('source', 'caller_supplied_unverified'),
                  'news_status': 'not_configured', 'ml_model_status': 'not_used',
                  'parameter_basis': 'upstream_published_defaults_not_account_calibrated'}
        if 'captured_at' not in context:
            raise ValueError('Missing context.captured_at: provide the actual capture time as numeric Unix seconds UTC. For live MT5 use metatrader-demo.get_gold_research(); no internal feed is required.')
        captured = context['captured_at']
        if isinstance(captured, bool) or not isinstance(captured, (int, float)) or not math.isfinite(captured) or captured <= 0:
            raise ValueError('context.captured_at must be positive finite numeric Unix seconds UTC, not an ISO string')
        if captured >= 100_000_000_000:
            raise ValueError('context.captured_at uses milliseconds; numeric Unix seconds UTC are required. Do not change the original capture time to bypass freshness.')
        if captured > now + 5:
            raise ValueError('context.captured_at is in the future; check caller/server clocks and timestamp units')
        result['data_quality'] = {'capture_age_seconds': now-captured, 'max_capture_age_seconds': 60,
                                  'max_tick_age_seconds': 30, 'timestamp_unit': 'unix_seconds'}
        if now - captured > 60:
            return {**result, 'status': 'data_stale', 'reason': 'capture_too_old',
                    'next_step': 'Recapture MT5 data with metatrader-demo.get_gold_research(); do not restamp old data'}
        raw = context.get('candles', {})
        bars = {}
        for tf, values in raw.items():
            if tf not in PERIODS or not isinstance(values, list) or len(values) > 1000:
                raise ValueError('Unsupported timeframe or candle count')
            previous = -1
            bars[tf] = []
            for row in values:
                item = {key: float(row[key]) for key in ('ts', 'open', 'high', 'low', 'close')}
                if any(not math.isfinite(value) for value in item.values()):
                    raise ValueError('Nonfinite candle')
                if item['ts'] <= previous or min(item[key] for key in ('open', 'high', 'low', 'close')) <= 0:
                    raise ValueError('Candles must be ordered, unique and positive')
                if item['high'] < max(item['open'], item['close'], item['low']) or item['low'] > min(item['open'], item['close'], item['high']):
                    raise ValueError('Invalid OHLC bounds')
                previous = item['ts']
                bars[tf].append(item)
        quote = context.get('tick', {})
        bid, ask, stamp = (float(quote.get(key, 0)) for key in ('bid', 'ask', 'time'))
        if not all(math.isfinite(value) for value in (bid, ask, stamp)) or bid <= 0 or ask < bid:
            raise ValueError('Invalid quote')
        if not math.isfinite(stamp) or stamp <= 0 or stamp >= 100_000_000_000:
            raise ValueError('context.tick.time must be the original tick timestamp in numeric Unix seconds, not milliseconds or the caller capture time')
        offset = self.clock.measured_offset_s(SimpleNamespace(timestamp=stamp), bars.get('M1', []), now)
        if offset is None:
            return {**result, 'status': 'clock_unknown', 'reason': 'broker_clock_unresolved_or_inconsistent',
                    'next_step': 'Provide original MT5 tick.time and M1 candle ts from the same capture; do not mix clock conventions'}
        tick_age = now - (stamp - offset)
        if tick_age > 30 or tick_age < -5:
            return {**result, 'status': 'data_stale', 'reason': 'quote_too_old' if tick_age > 30 else 'quote_in_future',
                    'tick_age_seconds': tick_age,
                    'data_quality': {**result['data_quality'], 'tick_age_seconds': tick_age, 'broker_offset_seconds': offset},
                    'next_step': 'Read a new actual MT5 tick and candles; check terminal connectivity/session and clock if the tick does not advance'}
        closed = {tf: self.clock.utc_closed(rows, PERIODS[tf], offset, now) for tf, rows in bars.items()}
        quality = {'broker_offset_seconds': offset, 'tick_age_seconds': tick_age,
                   'closed_bar_counts': {tf: len(rows) for tf, rows in closed.items()},
                   'closed_only': True, 'capture_age_seconds': now-captured,
                   'gap_counts': {tf: sum(b['ts']-a['ts'] > PERIODS[tf]*1.5 for a, b in zip(rows, rows[1:])) for tf, rows in closed.items()}}
        result['data_quality'] = quality
        minimums = {'M1': 15, 'M5': 35, 'M15': 30, 'H1': 60, 'H4': 60}
        if any(len(closed.get(tf, [])) < minimum for tf, minimum in minimums.items()):
            return {**result, 'status': 'insufficient_data'}
        h1 = self.levels.compute_htf_bias(closed['H1'])
        h4 = self.indicators.compute_h4_bias(closed['H4'])
        atr = self.dpm.compute_atr(closed['M5'])
        adx = self.indicators.compute_adx(closed['M5'])
        macd_line, hist = self.indicators.compute_macd_hist([row['close'] for row in closed['M5']])
        price = (bid+ask)/2
        levels = self.levels.identify_key_levels(closed['H1'], price, closed['M15'])
        result['analysis'] = {'h1_bias': h1, 'h4_bias': h4, 'atr_m5': atr, 'adx_m5': adx,
                              'macd_hist_m5': hist, 'macd_line_m5': macd_line,
                              'regime': self.indicators.detect_regime(adx, h1, h4),
                              'key_levels': levels[:30], 'bid': bid, 'ask': ask, 'spread_price': ask-bid}
        for name in ('check_breakout_go', 'check_breakout_retest', 'check_liquidity_sweep'):
            frame = 'M5' if name.endswith('sweep') else 'M15'
            candidate = self.generators[name](closed[frame], levels, h1, h4, price, atr, adx, hist)
            if candidate:
                risk = self.generators['calculate_breakout_risk_levels'](candidate, price, atr, adx)
                if risk:
                    identity = json.dumps([name, closed[frame][-1]['ts'], candidate, risk], sort_keys=True)
                    result['candidates'].append({'candidate_id': hashlib.sha256(identity.encode()).hexdigest()[:24],
                                                 'generator': name, 'candidate': candidate, 'proposed_levels': risk,
                                                 'last_closed_bar': closed[frame][-1]['ts'], 'execution_authorized': False})
        return {**result, 'status': 'ok'}
