"""Regression from the third PR #3 review (reviewer reproduction), then complementary cases."""

from metatrader_client.order import place_market_order, get_trading_snapshot
from metatrader_client.store import Store
from .fakes import make_position
from .test_review_regressions import conn


def test_book_version_must_cover_the_terminal_read_not_only_later_reconciliation(fake_mt5,monkeypatch):
    store=Store(':memory:')
    first,second=conn(store,aggregate=.15),conn(store,aggregate=.15)
    original=fake_mt5.positions_get
    armed=True
    def interleaved_read(**kwargs):
        nonlocal armed
        old=original(**kwargs)
        if armed and not kwargs:
            armed=False
            # A's read saw no positions. B sends, publishes and reconciles
            # before A receives that read and samples its first book version.
            sent=place_market_order(second,type='BUY',symbol='EURUSD',volume=.1,
                                    stop_loss=1.099,request_id='second')
            assert not sent['error'],sent
            fake_mt5.positions=[make_position(sent['data'].order,volume=.1,sl=1.099)]
            snapshot=get_trading_snapshot(second,symbol='EURUSD')
            assert snapshot['data']['positions']['count']==1
            assert second.risk_guard.reservations()==[]
        return old
    monkeypatch.setattr(fake_mt5,'positions_get',interleaved_read)
    result=place_market_order(first,type='BUY',symbol='EURUSD',volume=.1,
                              stop_loss=1.099,request_id='first')
    assert len(fake_mt5.sent)<=1,(result,len(fake_mt5.sent))



def _second_agent_publishes(fake_mt5, second):
    sent = place_market_order(second, type='BUY', symbol='EURUSD', volume=.1, stop_loss=1.099, request_id='second')
    fake_mt5.positions = [make_position(sent['data'].order, volume=.1, sl=1.099)]
    get_trading_snapshot(second, symbol='EURUSD')  # reconciles and releases the reservation


def test_change_during_the_orders_read_forces_a_re_read(fake_mt5, monkeypatch):
    store = Store(':memory:')
    first, second = conn(store, aggregate=.15), conn(store, aggregate=.15)
    original = fake_mt5.orders_get
    armed = [True]

    def interleaved_orders(**kwargs):
        orders = original(**kwargs)
        if armed[0] and not kwargs:
            armed[0] = False
            _second_agent_publishes(fake_mt5, second)
        return orders

    monkeypatch.setattr(fake_mt5, 'orders_get', interleaved_orders)
    result = place_market_order(first, type='BUY', symbol='EURUSD', volume=.1, stop_loss=1.099, request_id='first')

    assert result['error'] is True and 'would be 21.00' in result['message']
    assert len(fake_mt5.sent) == 1


def test_change_during_the_revalidation_read_is_detected_again(fake_mt5, monkeypatch):
    store = Store(':memory:')
    first, second = conn(store, aggregate=.15), conn(store, aggregate=.15)
    original_check = first.risk_guard.check_new_order
    original_read = fake_mt5.positions_get
    calls = {'reads': 0}

    def check_then_change(*args, **kwargs):
        error = original_check(*args, **kwargs)
        with first.risk_guard._book().transaction() as db:  # an unrelated change: forces a revalidation
            first.risk_guard._bump(db)
        return error

    def read(**kwargs):
        items = original_read(**kwargs)
        if not kwargs:
            calls['reads'] += 1
            if calls['reads'] == 2:  # first revalidation read: B publishes meanwhile
                _second_agent_publishes(fake_mt5, second)
        return items

    monkeypatch.setattr(first.risk_guard, 'check_new_order', check_then_change)
    monkeypatch.setattr(fake_mt5, 'positions_get', read)
    result = place_market_order(first, type='BUY', symbol='EURUSD', volume=.1, stop_loss=1.099, request_id='first')

    assert result['error'] is True and 'would be 21.00' in result['message']
    assert len(fake_mt5.sent) == 1


def test_own_reconciliation_does_not_force_a_re_read(fake_mt5, monkeypatch):
    connection = conn(aggregate=1.0)
    first = place_market_order(connection, type='BUY', symbol='EURUSD', volume=.1, stop_loss=1.099)
    fake_mt5.positions = [make_position(first['data'].order, volume=.1, sl=1.099)]  # published: the next check releases it
    original = fake_mt5.positions_get
    reads = []
    monkeypatch.setattr(fake_mt5, 'positions_get', lambda **kwargs: (reads.append(kwargs), original(**kwargs))[1])

    second = place_market_order(connection, type='BUY', symbol='EURUSD', volume=.1, stop_loss=1.099)

    assert second['error'] is False
    assert connection.risk_guard.reservations()[0].ticket == second['data'].order or len(connection.risk_guard.reservations()) == 1
    assert sum(1 for k in reads if not k) == 1  # one listing read for the check, no revalidation
