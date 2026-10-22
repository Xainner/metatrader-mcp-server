"""IMP-007 (local position supervisor) and IMP-008 (structured, incremental candle data)."""

from types import SimpleNamespace

import MetaTrader5 as real_mt5
import pytest

from metatrader_client.market import candles_to_csv, get_candles, get_candles_multi
from metatrader_client.requests_registry import RequestRegistry
from metatrader_client.risk import RiskConfig, RiskGuard
from metatrader_client.store import Store
from metatrader_client.strategy import strategy_magic
from metatrader_client.supervisor import (
    PositionSupervisor,
    create_position_rule,
    list_position_rules,
    pause_position_rule,
    remove_position_rule,
    resume_position_rule,
)
from .fakes import make_position, make_rates, make_tick, simulate_trading

M1, M5, H4 = real_mt5.TIMEFRAME_M1, real_mt5.TIMEFRAME_M5, real_mt5.TIMEFRAME_H4


@pytest.fixture
def conn():
    store = Store(":memory:")
    return SimpleNamespace(
        risk_guard=RiskGuard(RiskConfig(duplicate_window_seconds=0)),
        store=store,
        request_registry=RequestRegistry(store),
    )


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def supervisor(conn, clock=None, owner="one", **kwargs):
    return PositionSupervisor(conn, owner=owner, interval=1, max_stale_seconds=60, clock=clock or Clock(), **kwargs)


def bump_tick(fake, symbol="EURUSD"):
    tick = fake.ticks[symbol]
    fake.ticks[symbol] = make_tick(tick.bid, tick.ask, time=tick.time + 1)


def primed(conn, fake, clock=None, owner="one"):
    """A supervisor that has already seen the EURUSD tick change, so its quote counts as fresh."""
    sup = supervisor(conn, clock=clock, owner=owner)
    sup._quote_age("EURUSD")
    bump_tick(fake)
    sup._quote_age("EURUSD")
    return sup


# ---------------------------------------------------------------------------
# IMP-007
# ---------------------------------------------------------------------------

def test_without_rules_the_supervisor_does_nothing(fake_mt5, conn):
    simulate_trading(fake_mt5)
    fake_mt5.positions = [make_position(1, profit=500)]
    summary = supervisor(conn).run_cycle()
    assert summary["skipped"] == "no active rules"
    assert fake_mt5.sent == []


def test_rule_closes_the_position_once_and_records_the_result(fake_mt5, conn):
    simulate_trading(fake_mt5)
    fake_mt5.positions = [make_position(1, profit=10)]
    rule = create_position_rule(conn, ticket=1, operator=">=", threshold=50)["data"]
    assert rule["state"] == "active" and rule["currency"] == "USD"
    sup = primed(conn, fake_mt5)

    sup.run_cycle()
    assert list_position_rules(conn)["data"][0]["last_value"] == 10
    assert fake_mt5.sent == []

    fake_mt5.positions[0].profit = 55
    bump_tick(fake_mt5)
    sup.run_cycle()
    sup.run_cycle()

    final = list_position_rules(conn, include_events=True)["data"][0]
    assert final["state"] == "fulfilled"
    assert final["triggered_value"] == 55
    assert final["result"]["observed"]["kind"] == "floating"
    assert final["result"]["results"][0]["verification"] == "confirmed"
    assert [e["state"] for e in final["events"]] == ["active", "triggered", "executing", "fulfilled"]
    assert len(fake_mt5.sent) == 1
    assert fake_mt5.sent[0]["comment"].startswith("MCP r")
    assert conn.request_registry.get(f"rule:{rule['rule_id']}:1")["state"] == "filled"


def test_two_supervisors_never_close_twice(fake_mt5, conn):
    fake_mt5.positions = [make_position(1, profit=100)]  # the close is not reflected: the position stays
    rule_id = create_position_rule(conn, ticket=1, operator=">=", threshold=50)["data"]["rule_id"]
    first, second = primed(conn, fake_mt5, owner="one"), primed(conn, fake_mt5, owner="two")

    first.run_cycle()
    conn.store.execute("UPDATE leases SET expires_at = 0")  # the first owner's lease lapses
    second.run_cycle()

    assert len(fake_mt5.sent) == 1
    from metatrader_client.supervisor import get_rule_store
    rules = get_rule_store(conn)
    assert rules.transition(rule_id, ["active"], "triggered") is False  # compare-and-set: no longer active


def test_second_owner_waits_for_the_lease(fake_mt5, conn):
    fake_mt5.positions = [make_position(1, profit=0)]
    create_position_rule(conn, ticket=1, operator=">=", threshold=50)
    supervisor(conn, owner="one").run_cycle()
    assert supervisor(conn, owner="two").run_cycle()["skipped"] == "another supervisor holds the lease"


def test_interrupted_rule_is_not_retried_after_a_restart(fake_mt5, conn):
    fake_mt5.positions = [make_position(1, profit=100)]
    rule = create_position_rule(conn, ticket=1, operator=">=", threshold=50)["data"]
    from metatrader_client.supervisor import get_rule_store
    get_rule_store(conn).transition(rule["rule_id"], ["active"], "executing")  # the process stopped here

    sup = supervisor(conn)  # restart
    sup.run_cycle()

    assert list_position_rules(conn)["data"][0]["state"] == "pending_reconciliation"
    assert fake_mt5.sent == []


@pytest.mark.parametrize("make_result, state", [
    (lambda fake: (lambda request: None), "pending_reconciliation"),
    (lambda fake: (lambda request: fake.result_for(request, 10019)), "failed"),
])
def test_uncertain_or_rejected_close_is_not_retried(fake_mt5, conn, make_result, state):
    fake_mt5.positions = [make_position(1, profit=100)]
    create_position_rule(conn, ticket=1, operator=">=", threshold=50)
    fake_mt5.results = [make_result(fake_mt5)]
    sup = primed(conn, fake_mt5)

    sup.run_cycle()
    sup.run_cycle()

    assert list_position_rules(conn)["data"][0]["state"] == state
    assert len(fake_mt5.sent) == 1


def test_partial_close_is_pending_reconciliation(fake_mt5, conn):
    fake_mt5.positions = [make_position(1, profit=100)]  # close accepted but the position stays listed
    create_position_rule(conn, ticket=1, operator=">=", threshold=50)
    primed(conn, fake_mt5).run_cycle()
    assert list_position_rules(conn)["data"][0]["state"] == "pending_reconciliation"


def test_stale_or_unreadable_pnl_is_not_evaluated(fake_mt5, conn, monkeypatch):
    fake_mt5.positions = [make_position(1, profit=100)]
    create_position_rule(conn, ticket=1, operator=">=", threshold=50)
    clock = Clock()
    sup = primed(conn, fake_mt5, clock=clock)  # the tick changed at clock 0
    clock.now = 120  # and has not changed for two minutes

    sup.run_cycle()
    rule = list_position_rules(conn)["data"][0]
    assert rule["state"] == "active" and "stale" in rule["last_error"]

    monkeypatch.setattr(fake_mt5, "positions_get", lambda **kwargs: None)
    bump_tick(fake_mt5)
    assert "unreadable" in sup.run_cycle()["skipped"]
    assert fake_mt5.sent == []


def test_strategy_rule_closes_only_its_positions(fake_mt5, conn):
    simulate_trading(fake_mt5)
    magic = strategy_magic("alpha")
    fake_mt5.positions = [make_position(1, magic=magic, profit=-30), make_position(2, magic=magic, profit=-25), make_position(3, profit=-500)]
    create_position_rule(conn, strategy_id="alpha", operator="<=", threshold=-50)

    primed(conn, fake_mt5).run_cycle()

    rule = list_position_rules(conn)["data"][0]
    assert rule["state"] == "fulfilled" and rule["triggered_value"] == -55
    assert [p.ticket for p in fake_mt5.positions] == [3]


def test_rule_lifecycle_requires_explicit_requests(fake_mt5, conn):
    fake_mt5.positions = [make_position(1)]
    assert create_position_rule(conn, operator=">=", threshold=1)["error"] is True  # no target
    rule_id = create_position_rule(conn, ticket=1, operator=">=", threshold=1)["data"]["rule_id"]

    assert pause_position_rule(conn, rule_id)["data"]["state"] == "paused"
    assert pause_position_rule(conn, rule_id)["error"] is True
    assert resume_position_rule(conn, rule_id)["data"]["state"] == "active"
    assert remove_position_rule(conn, rule_id)["data"]["state"] == "removed"


def test_position_closed_elsewhere_ends_the_rule(fake_mt5, conn):
    fake_mt5.positions = [make_position(1)]
    create_position_rule(conn, ticket=1, operator=">=", threshold=50)
    fake_mt5.positions = []
    supervisor(conn).run_cycle()
    assert list_position_rules(conn)["data"][0]["state"] == "target_closed"


# ---------------------------------------------------------------------------
# IMP-008
# ---------------------------------------------------------------------------

START = 1_700_000_040  # a multiple of 60


@pytest.fixture
def market(fake_mt5):
    fake_mt5.rates[("EURUSD", M1)] = make_rates(START, 10, 60)
    fake_mt5.rates[("EURUSD", M5)] = make_rates(START - 1200, 6, 300)
    # The last M1 bar starts at START + 540 and is still forming.
    fake_mt5.ticks["EURUSD"] = make_tick(1.1, 1.1001, time=START + 560)
    return fake_mt5


def test_bars_are_ascending_and_the_forming_bar_is_flagged(market, connection):
    data = get_candles(connection, symbol="EURUSD", timeframe="M1", count=5)["data"]

    times = [b["time"] for b in data["bars"]]
    assert times == sorted(times) and len(times) == 5
    assert [b["closed"] for b in data["bars"]] == [True, True, True, True, False]
    assert data["forming_bar_included"] is True
    assert data["schema"] == "candles/v1" and data["time_basis"] == "server"
    assert data["next_since"] == START + 480


def test_closed_only_excludes_the_forming_bar(market, connection):
    data = get_candles(connection, symbol="EURUSD", timeframe="M1", count=5, closed_only=True)["data"]
    assert all(b["closed"] for b in data["bars"]) and data["forming_bar_included"] is False


def test_utc_times_when_the_server_offset_is_configured(market, connection, monkeypatch):
    monkeypatch.setenv("MT5_SERVER_UTC_OFFSET_SECONDS", "7200")
    bar = get_candles(connection, symbol="EURUSD", timeframe="M1", count=1)["data"]["bars"][0]
    assert bar["time_iso"].endswith("Z")


def test_incremental_query_matches_the_full_query(market, connection):
    first = get_candles(connection, symbol="EURUSD", timeframe="M1", count=6, closed_only=True)["data"]
    # Three more minutes pass: new bars are published and the previous forming bar closes.
    market.rates[("EURUSD", M1)] = make_rates(START, 13, 60)
    market.ticks["EURUSD"] = make_tick(1.1, 1.1001, time=START + 740)
    newer = get_candles(connection, symbol="EURUSD", timeframe="M1", since=first["next_since"], closed_only=True)["data"]
    full = get_candles(connection, symbol="EURUSD", timeframe="M1", count=13, closed_only=True, use_cache=False)["data"]

    combined = first["bars"] + newer["bars"]
    expected = [b for b in full["bars"] if b["time"] >= first["bars"][0]["time"]]
    assert len(newer["bars"]) == 3
    assert [(b["time"], b["close"]) for b in combined] == [(b["time"], b["close"]) for b in expected]


def test_since_paging_reports_truncation(market, connection):
    data = get_candles(connection, symbol="EURUSD", timeframe="M1", since=START - 1, limit=3)["data"]
    assert data["truncated"] is True and len(data["bars"]) == 3
    assert data["next_since"] == data["bars"][-1]["time"]


def test_gaps_and_duplicates_are_explicit(market, connection):
    import numpy as np
    rates = market.rates[("EURUSD", M1)]
    market.rates[("EURUSD", M1)] = np.concatenate([rates[:3], rates[3:4], rates[3:4], rates[6:]])

    data = get_candles(connection, symbol="EURUSD", timeframe="M1", count=10, use_cache=False)["data"]

    assert data["duplicates_removed"] == 1
    assert data["gaps"] == [{"after": START + 180, "before": START + 360, "missing_periods": 2}]


def test_closed_history_is_cached_and_verified(market, connection):
    get_candles(connection, symbol="EURUSD", timeframe="M1", count=5)
    second = get_candles(connection, symbol="EURUSD", timeframe="M1", count=5)["data"]
    assert second["cache"]["used"] is True
    assert second["cache"]["fetched_bars"] < 5 + 2

    # The broker rewrites the last cached bar: the cache is invalidated and the data re-read.
    rates = market.rates[("EURUSD", M1)]
    rates["close"][-2] = 9.9
    third = get_candles(connection, symbol="EURUSD", timeframe="M1", count=5)["data"]
    assert third["cache"]["invalidated"] is True
    assert third["bars"][-2]["close"] == 9.9


def test_multi_timeframe_reports_errors_per_timeframe(market, connection):
    result = get_candles_multi(connection, symbol="EURUSD", timeframes=["M1", "M5", "H4"], count=3)
    assert result["error"] is True
    assert set(result["data"]["results"]) == {"M1", "M5"}
    assert "H4" in result["data"]["errors"]
    assert "failed: H4" in result["message"]


def test_csv_export_keeps_the_closed_flag(market, connection):
    data = get_candles(connection, symbol="EURUSD", timeframe="M1", count=2)["data"]
    lines = candles_to_csv(data).strip().splitlines()
    assert lines[0].startswith("time,time_iso,open") and lines[0].endswith("closed")
    assert lines[-1].endswith("False")


def test_invalid_arguments_are_errors(market, connection):
    assert get_candles(connection, symbol="EURUSD", timeframe="X9")["error"] is True
    assert get_candles(connection, symbol="EURUSD", timeframe="M1", count=0)["error"] is True
