"""Regressions from the PR #3 review (four reproductions by the reviewer, then complementary cases)."""

from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

from metatrader_client.order import place_market_order
from metatrader_client.requests_registry import RequestRegistry
from metatrader_client.risk import RiskConfig, RiskGuard
from metatrader_client.store import Store
from metatrader_client.supervisor import PositionSupervisor, create_position_rule, get_rule_store
from .fakes import make_position, make_tick


def conn(store=None, limit=None, aggregate=None):
    store=store or Store(':memory:')
    return SimpleNamespace(store=store,request_registry=RequestRegistry(store),
        risk_guard=RiskGuard(RiskConfig(duplicate_window_seconds=0,max_open_positions=limit,max_aggregate_risk_pct=aggregate)))


def test_first_old_tick_must_not_trigger_close(fake_mt5):
    connection=conn()
    fake_mt5.positions=[make_position(1,profit=100)]
    fake_mt5.ticks['EURUSD']=make_tick(1.1,1.1002,time=1_700_000_000)
    create_position_rule(connection,ticket=1,operator='>=',threshold=50)
    sup=PositionSupervisor(connection,owner='one',max_stale_seconds=60)
    sup.run_cycle()
    assert fake_mt5.sent==[], 'Old cached tick was accepted as fresh on first observation'


def test_starting_second_supervisor_must_not_recover_a_live_lease(fake_mt5):
    connection=conn()
    fake_mt5.positions=[make_position(1,profit=100)]
    rule=create_position_rule(connection,ticket=1,operator='>=',threshold=50)['data']
    first=PositionSupervisor(connection,owner='one')
    assert first.rules.acquire_lease('one',ttl=600)
    first.rules.transition(rule['rule_id'],['active'],'executing',owner='one')
    PositionSupervisor(connection,owner='two')
    assert first.rules.get(rule['rule_id'])['state']=='executing'


def test_realized_net_must_include_entry_commission(fake_mt5,monkeypatch):
    deals=[SimpleNamespace(entry=0,profit=0,commission=-5,swap=0,fee=0),
           SimpleNamespace(entry=1,profit=100,commission=0,swap=0,fee=0)]
    monkeypatch.setattr(fake_mt5,'history_deals_get',lambda **kwargs:deals)
    assert PositionSupervisor._realized(1)==95


def test_two_guards_sharing_store_must_not_exceed_new_aggregate_risk_limit(fake_mt5,monkeypatch):
    store=Store(':memory:')
    connections=[conn(store,aggregate=.15),conn(store,aggregate=.15)]
    barrier=threading.Barrier(2)
    for connection in connections:
        original=connection.risk_guard.check_new_order
        def synchronize(*args,_original=original,**kwargs):
            error=_original(*args,**kwargs)
            barrier.wait(timeout=3)
            return error
        monkeypatch.setattr(connection.risk_guard,'check_new_order',synchronize)
    def send(index):
        return place_market_order(connections[index],type='BUY',symbol='EURUSD',volume=.1,stop_loss=1.099,request_id=f'agent-{index}')
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(send,[0,1]))
    # Each opening risks $11 on $10,000 equity; both together risk $22 > $15 cap.
    assert len(fake_mt5.sent)<=1, 'Aggregate risk limit 0.15% allowed simultaneous exposure of 0.22%'



# ---------------------------------------------------------------------------
# Complementary cases for the PR #3 review findings
# ---------------------------------------------------------------------------

def test_two_processes_sharing_the_state_file_respect_the_aggregate_limit(fake_mt5, tmp_path):
    path = str(tmp_path / "state.sqlite3")
    first, second = conn(Store(path), aggregate=.15), conn(Store(path), aggregate=.15)  # separate sqlite connections

    a = place_market_order(first, type='BUY', symbol='EURUSD', volume=.1, stop_loss=1.099, request_id='p1')
    b = place_market_order(second, type='BUY', symbol='EURUSD', volume=.1, stop_loss=1.099, request_id='p2')

    assert a['error'] is False
    assert b['error'] is True and 'Aggregate loss at stop losses' in b['message']
    assert len(fake_mt5.sent) == 1


def test_uncertain_send_stays_reserved_for_other_connections(fake_mt5):
    store = Store(':memory:')
    first, second = conn(store, limit=1), conn(store, limit=1)
    for connection in (first, second):
        connection.risk_guard.config = RiskConfig(duplicate_window_seconds=0, max_open_positions=1, block_on_unresolved=False)
    fake_mt5.results = [None]

    assert place_market_order(first, type='BUY', symbol='EURUSD', volume=.1)['status'] == 'no_response'
    other = place_market_order(second, type='SELL', symbol='EURUSD', volume=.2)

    assert other['error'] is True and 'recently sent and not listed yet' in other['message']
    assert len(fake_mt5.sent) == 1


def test_old_tick_with_configured_offset_is_stale(fake_mt5, monkeypatch):
    monkeypatch.setenv('MT5_SERVER_UTC_OFFSET_SECONDS', '0')
    connection = conn()
    fake_mt5.positions = [make_position(1, profit=100)]
    fake_mt5.ticks['EURUSD'] = make_tick(1.1, 1.1002, time=1_700_000_000)
    rule = create_position_rule(connection, ticket=1, operator='>=', threshold=50)['data']

    PositionSupervisor(connection, owner='one', max_stale_seconds=60).run_cycle()

    assert fake_mt5.sent == []
    assert 'stale' in get_rule_store(connection).get(rule['rule_id'])['last_error']


def test_rule_of_a_lapsed_owner_is_recovered_by_the_next_lease_holder(fake_mt5):
    connection = conn()
    fake_mt5.positions = [make_position(1, profit=100)]
    rule = create_position_rule(connection, ticket=1, operator='>=', threshold=50)['data']
    rules = get_rule_store(connection)
    assert rules.acquire_lease('one', ttl=600)
    rules.transition(rule['rule_id'], ['active'], 'executing', owner='one')

    second = PositionSupervisor(connection, owner='two')
    assert second.run_cycle()['skipped'] == 'another supervisor holds the lease'
    assert rules.get(rule['rule_id'])['state'] == 'executing'

    connection.store.execute('UPDATE leases SET expires_at = 0')  # the first owner stopped renewing
    summary = second.run_cycle()
    assert summary['recovered'] == [rule['rule_id']]
    assert rules.get(rule['rule_id'])['state'] == 'pending_reconciliation'
    assert fake_mt5.sent == []


def test_realized_close_excludes_entry_costs_and_position_total_includes_them(fake_mt5, monkeypatch):
    deals = [SimpleNamespace(ticket=1, entry=0, profit=0, commission=-5, swap=0, fee=0),
             SimpleNamespace(ticket=2, entry=1, profit=40, commission=-1, swap=0, fee=0),
             SimpleNamespace(ticket=3, entry=1, profit=60, commission=-1, swap=-2, fee=0)]
    monkeypatch.setattr(fake_mt5, 'history_deals_get',
                        lambda ticket=None, position=None, **kwargs: [d for d in deals if ticket in (None, d.ticket)])
    assert PositionSupervisor._realized(1) == 91           # whole position: entry costs and both partial closes
    assert PositionSupervisor._realized_deal(3) == 57      # only the deal of this close
