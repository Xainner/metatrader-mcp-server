"""Regression from the second PR #3 review (reviewer reproduction), then complementary cases."""

from metatrader_client.order import place_market_order, get_trading_snapshot
from metatrader_client.risk import bound_risk_guard
from metatrader_client.store import Store
from .fakes import make_position
from .test_review_regressions import conn


def test_reconciling_shared_reservation_cannot_hide_exposure_from_older_plan(fake_mt5,monkeypatch):
    store=Store(':memory:')
    first,second=conn(store,aggregate=.15),conn(store,aggregate=.15)
    original=first.risk_guard.check_new_order
    def interleave(*args,**kwargs):
        error=original(*args,**kwargs)  # first records empty positions in its plan
        assert error is None
        sent=place_market_order(second,type='BUY',symbol='EURUSD',volume=.1,stop_loss=1.099,request_id='second')
        assert not sent['error'], sent
        # Terminal publishes the second agent's position before first commits.
        ticket=sent['data'].order
        fake_mt5.positions=[make_position(ticket,volume=.1,sl=1.099)]
        # A regular snapshot reconciles and deletes the now-listed reservation.
        snapshot=get_trading_snapshot(second,symbol='EURUSD')
        assert snapshot['data']['positions']['count']==1
        assert second.risk_guard.reservations()==[]
        return error
    monkeypatch.setattr(first.risk_guard,'check_new_order',interleave)
    result=place_market_order(first,type='BUY',symbol='EURUSD',volume=.1,stop_loss=1.099,request_id='first')
    assert len(fake_mt5.sent)<=1, (result,len(fake_mt5.sent))



def _interleave_second_order(fake_mt5, first, second, monkeypatch):
    """Make the second agent send, publish and reconcile its order between the first one's check and commit."""
    original = first.risk_guard.check_new_order

    def interleave(*args, **kwargs):
        error = original(*args, **kwargs)
        if not fake_mt5.sent:
            sent = place_market_order(second, type='BUY', symbol='EURUSD', volume=.1, stop_loss=1.099, request_id='second')
            fake_mt5.positions = [make_position(sent['data'].order, volume=.1, sl=1.099)]
            get_trading_snapshot(second, symbol='EURUSD')
        return error

    monkeypatch.setattr(first.risk_guard, 'check_new_order', interleave)


def test_stale_plan_is_revalidated_and_rejected_with_the_real_reason(fake_mt5, monkeypatch):
    store = Store(':memory:')
    first, second = conn(store, aggregate=.15), conn(store, aggregate=.15)
    _interleave_second_order(fake_mt5, first, second, monkeypatch)

    result = place_market_order(first, type='BUY', symbol='EURUSD', volume=.1, stop_loss=1.099, request_id='first')

    assert result['error'] is True
    assert 'Aggregate loss at stop losses would be 21.00' in result['message']
    assert len(fake_mt5.sent) == 1


def test_stale_plan_with_room_left_is_revalidated_and_sent(fake_mt5, monkeypatch):
    store = Store(':memory:')
    first, second = conn(store, aggregate=1.0), conn(store, aggregate=1.0)
    _interleave_second_order(fake_mt5, first, second, monkeypatch)

    result = place_market_order(first, type='BUY', symbol='EURUSD', volume=.1, stop_loss=1.099, request_id='first')

    assert result['error'] is False
    assert len(fake_mt5.sent) == 2


def test_a_book_that_keeps_changing_rejects_after_bounded_attempts(fake_mt5, monkeypatch):
    connection = conn(aggregate=1.0)
    guard = bound_risk_guard(connection, fake_mt5)
    original_plan = guard._plan

    def plan_then_change(*args, **kwargs):
        plan, error = original_plan(*args, **kwargs)
        with guard._book().transaction() as db:
            guard._bump(db)  # another process changes the book every time
        return plan, error

    monkeypatch.setattr(guard, '_plan', plan_then_change)
    result = place_market_order(connection, type='BUY', symbol='EURUSD', volume=.1, stop_loss=1.099)

    assert result['error'] is True and 'kept changing' in result['message']
    assert fake_mt5.sent == []


def test_every_book_change_bumps_the_version(fake_mt5, monkeypatch):
    import metatrader_client.risk as risk
    connection = conn(limit=5)
    guard = bound_risk_guard(connection, fake_mt5)
    version = lambda: guard._version(guard._book()._db)

    place_market_order(connection, type='BUY', symbol='EURUSD', volume=.1)  # stored, then updated after the send
    after_send = version()
    assert after_send >= 2
    monkeypatch.setattr(risk, 'RESERVATION_TTL_SECONDS', 0)
    guard.reservations()  # expiry deletes it
    assert version() == after_send + 1
