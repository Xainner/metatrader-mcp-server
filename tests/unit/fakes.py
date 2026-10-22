"""
In-memory stand-in for the MetaTrader5 package, used by the unit tests.
"""

from collections import namedtuple
from types import SimpleNamespace
from typing import Callable, List, Optional

from metatrader_client.risk import RiskConfig, RiskGuard

TradeResult = namedtuple(
    "TradeResult",
    "retcode deal order volume price bid ask comment request_id retcode_external request",
)
TradeRequest = namedtuple("TradeRequest", "action symbol volume type price sl tp position order")

PATCHED_MODULES = [
    "metatrader_client.market.get_symbol_price",
    "metatrader_client.market.get_symbol_specifications",
    "metatrader_client.market.get_candles",
    "metatrader_client.order.send_order",
    "metatrader_client.order.place_market_order",
    "metatrader_client.order.place_pending_order",
    "metatrader_client.order.modify_pending_order",
    "metatrader_client.order.modify_position",
    "metatrader_client.order.close_position",
    "metatrader_client.order.cancel_pending_order",
    "metatrader_client.order.close_all_positions",
    "metatrader_client.order.close_all_positions_by_symbol",
    "metatrader_client.order.close_all_profitable_positions",
    "metatrader_client.order.close_all_losing_positions",
    "metatrader_client.order.cancel_all_pending_orders",
    "metatrader_client.order.cancel_pending_orders_by_symbol",
    "metatrader_client.order.calculate_trade_outcome",
    "metatrader_client.order.calculate_required_margin",
    "metatrader_client.order.preview_order",
    "metatrader_client.order.get_trading_snapshot",
    "metatrader_client.order.requests",
    "metatrader_client.supervisor",
]

DONE = 10009


def make_symbol(**overrides):
    values = dict(
        name="EURUSD",
        digits=5,
        point=0.00001,
        trade_tick_size=0.00001,
        volume_min=0.01,
        volume_max=50.0,
        volume_step=0.01,
        trade_stops_level=0,
        filling_mode=1,       # SYMBOL_FILLING_FOK
        trade_exemode=2,      # SYMBOL_TRADE_EXECUTION_MARKET
        trade_contract_size=100_000.0,
        trade_tick_value=1.0,
        currency_base="EUR",
        currency_profit="USD",
        currency_margin="EUR",
        trade_mode=4,
        trade_freeze_level=0,
        order_mode=127,
        expiration_mode=15,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def make_tick(bid, ask, time=1_700_000_000):
    return SimpleNamespace(bid=bid, ask=ask, last=0.0, volume=0, time=time)


def make_position(ticket, *, type=0, volume=0.1, symbol="EURUSD", price_open=1.1, sl=0.0, tp=0.0, profit=0.0,
                  magic=0, comment="", identifier=None, price_current=None):
    return SimpleNamespace(
        ticket=ticket, type=type, volume=volume, symbol=symbol,
        price_open=price_open, sl=sl, tp=tp, profit=profit, swap=0.0,
        magic=magic, comment=comment, identifier=ticket if identifier is None else identifier,
        price_current=price_open if price_current is None else price_current,
    )


def make_order(ticket, *, type=2, volume=0.1, symbol="EURUSD", price_open=1.09, sl=0.0, tp=0.0,
               type_time=0, time_expiration=0, price_stoplimit=0.0, magic=0, comment="", state=1):
    return SimpleNamespace(
        ticket=ticket, type=type, volume_current=volume, symbol=symbol, price_open=price_open,
        sl=sl, tp=tp, type_time=type_time, time_expiration=time_expiration, price_stoplimit=price_stoplimit,
        magic=magic, comment=comment, state=state,
    )


def make_deal(ticket, *, order=0, position_id=0, volume=0.1, type=0, entry=0, price=1.1, time=1_700_000_000,
              profit=0.0, magic=0, comment="", symbol="EURUSD"):
    return SimpleNamespace(
        ticket=ticket, order=order, position_id=position_id, volume=volume, type=type, entry=entry, price=price,
        time=time, profit=profit, commission=0.0, swap=0.0, fee=0.0, magic=magic, comment=comment, symbol=symbol,
    )


CheckResult = namedtuple("CheckResult", "retcode balance equity profit margin margin_free margin_level comment request")


def make_rates(start, count, period=60, base=1.1):
    """Structured array like ``copy_rates_*``: one bar per period from ``start``."""
    import numpy as np

    dtype = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
             ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")]
    rows = [(start + i * period, base + i * 1e-4, base + i * 1e-4 + 5e-4, base + i * 1e-4 - 5e-4,
             base + (i + 1) * 1e-4, 10 + i, 2, 0) for i in range(count)]
    return np.array(rows, dtype=dtype)


class FakeMT5:
    """Minimal MetaTrader5 replacement with configurable state and order_send results."""

    def __init__(self):
        self.symbols = {"EURUSD": make_symbol()}
        self.ticks = {"EURUSD": make_tick(1.1000, 1.1001)}
        self.account = SimpleNamespace(
            trade_mode=0, equity=10_000.0, balance=10_000.0, currency="USD", login=5_000_001, server="Demo-Server",
            margin=0.0, margin_free=10_000.0, margin_level=0.0, leverage=100, trade_allowed=True, trade_expert=True,
        )
        self.terminal = SimpleNamespace(connected=True, trade_allowed=True)
        self.history_orders: List[SimpleNamespace] = []
        self.rates = {}
        self.check_retcode = 0
        self.apply_modifications = True
        self.positions: List[SimpleNamespace] = []
        self.orders: List[SimpleNamespace] = []
        self.deals: List[SimpleNamespace] = []
        self.sent: List[dict] = []
        self.results: List[Optional[TradeResult]] = []
        self.default_retcode = DONE
        self.on_send: Optional[Callable[[dict, Optional[TradeResult]], None]] = None
        self.error = (1, "Success")
        self._next_ticket = 1000

    # Market data
    def symbol_select(self, symbol, enable=True):
        return symbol in self.symbols

    def symbol_info(self, symbol):
        return self.symbols.get(symbol)

    def symbol_info_tick(self, symbol):
        return self.ticks.get(symbol)

    # Account and trading state
    def account_info(self):
        return self.account

    def positions_get(self, ticket=None, symbol=None):
        items = self.positions
        if ticket is not None:
            items = [p for p in items if p.ticket == ticket]
        if symbol is not None:
            items = [p for p in items if p.symbol == symbol]
        return tuple(items)

    def orders_get(self, ticket=None, symbol=None):
        items = self.orders
        if ticket is not None:
            items = [o for o in items if o.ticket == ticket]
        if symbol is not None:
            items = [o for o in items if o.symbol == symbol]
        return tuple(items)

    def history_deals_get(self, *args, ticket=None, position=None, **kwargs):
        if ticket is not None:
            return tuple(d for d in self.deals if d.ticket == ticket)
        if position is not None:
            return tuple(d for d in self.deals if d.position_id == position)
        return tuple(self.deals)

    def history_orders_get(self, *args, ticket=None, **kwargs):
        if ticket is not None:
            return tuple(o for o in self.history_orders if o.ticket == ticket)
        return tuple(self.history_orders)

    def terminal_info(self):
        return self.terminal

    def _contract(self, symbol):
        return getattr(self.symbols.get(symbol), "trade_contract_size", 100_000.0)

    def order_calc_profit(self, action, symbol, volume, price_open, price_close):
        if symbol not in self.symbols:
            return None
        direction = 1 if action == 0 else -1
        return round((price_close - price_open) * direction * volume * self._contract(symbol), 2)

    def order_calc_margin(self, action, symbol, volume, price):
        if symbol not in self.symbols:
            return None
        return round(volume * self._contract(symbol) * price / self.account.leverage, 2)

    def order_check(self, request):
        margin = self.order_calc_margin(request["type"] % 2, request["symbol"], request["volume"], request["price"])
        return CheckResult(
            retcode=self.check_retcode, balance=self.account.balance, equity=self.account.equity, profit=0.0,
            margin=margin, margin_free=self.account.margin_free - margin, margin_level=0.0,
            comment="Done" if self.check_retcode == 0 else "Rejected", request=None,
        )

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        rates = self.rates.get((symbol, timeframe))
        if rates is None:
            return None
        end = len(rates) - start_pos
        return rates[max(0, end - count):end]

    def copy_rates_range(self, symbol, timeframe, date_from, date_to):
        rates = self.rates.get((symbol, timeframe))
        if rates is None:
            return None
        return rates[(rates["time"] >= int(date_from)) & (rates["time"] <= int(date_to))]

    def last_error(self):
        return self.error

    # Order sending
    def result_for(self, request, retcode=DONE, volume=None, comment=""):
        self._next_ticket += 1
        return TradeResult(
            retcode=retcode,
            deal=self._next_ticket if retcode in (DONE, 10010) else 0,  # DONE, DONE_PARTIAL
            order=self._next_ticket,
            volume=request.get("volume", 0.0) if volume is None else volume,
            price=request.get("price", 0.0),
            bid=0.0, ask=0.0, comment=comment, request_id=1, retcode_external=0,
            request=TradeRequest(
                request.get("action"), request.get("symbol"), request.get("volume"), request.get("type"),
                request.get("price"), request.get("sl"), request.get("tp"), request.get("position"), request.get("order"),
            ),
        )

    def order_send(self, request):
        self.sent.append(dict(request))
        if self.results:
            result = self.results.pop(0)
            if callable(result):
                result = result(request)
        else:
            result = self.result_for(request, self.default_retcode)
        if result is not None and result.deal:
            self.deals.append(make_deal(
                result.deal, order=result.order, position_id=request.get("position") or result.order,
                volume=result.volume, type=request.get("type", 0) % 2, entry=1 if request.get("position") else 0,
                price=result.price, magic=request.get("magic", 0), comment=request.get("comment", ""),
                symbol=request.get("symbol"), time=self.ticks.get(request.get("symbol"), make_tick(0, 0)).time,
            ))
        if self.apply_modifications and result is not None and result.retcode == DONE:
            self._apply(request)
        if self.on_send:
            self.on_send(request, result)
        return result

    def _apply(self, request):
        """Apply accepted SL/TP and pending order modifications, like the terminal would."""
        if request.get("action") == 6:  # TRADE_ACTION_SLTP
            for p in self.positions:
                if p.ticket == request.get("position"):
                    p.sl, p.tp = request["sl"], request["tp"]
        elif request.get("action") == 7:  # TRADE_ACTION_MODIFY
            for o in self.orders:
                if o.ticket == request.get("order"):
                    o.price_open, o.sl, o.tp = request["price"], request["sl"], request["tp"]


def with_risk(connection, **settings):
    connection.risk_guard = RiskGuard(RiskConfig(**settings))
    return connection


def simulate_trading(fake: FakeMT5, *, keep_sl_tp: bool = True):
    """
    Make the fake terminal list what accepted requests create or remove:
    market openings become positions (with the request SL/TP unless
    ``keep_sl_tp`` is False), closes remove the position, pending orders are
    listed and removals delete them.
    """
    def on_send(request, result):
        if result is None or result.retcode not in (DONE, 10008, 10010):  # DONE, PLACED, DONE_PARTIAL
            return
        action = request.get("action")
        if action == 1 and request.get("position"):  # close
            fake.positions = [p for p in fake.positions if p.ticket != request["position"]]
        elif action == 1 and result.retcode in (DONE, 10010):
            fake.positions.append(make_position(
                result.order, type=request["type"], volume=result.volume, symbol=request["symbol"],
                price_open=result.price, sl=request["sl"] if keep_sl_tp else 0.0, tp=request["tp"] if keep_sl_tp else 0.0,
                magic=request.get("magic", 0), comment=request.get("comment", ""),
            ))
        elif action == 5:  # pending
            fake.orders.append(make_order(
                result.order, type=request["type"], volume=request["volume"], symbol=request["symbol"],
                price_open=request["price"], sl=request["sl"], tp=request["tp"],
                magic=request.get("magic", 0), comment=request.get("comment", ""),
            ))
        elif action == 8:  # remove
            fake.orders = [o for o in fake.orders if o.ticket != request["order"]]
    fake.on_send = on_send
    return fake
