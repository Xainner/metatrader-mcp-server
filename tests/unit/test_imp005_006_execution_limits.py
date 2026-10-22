"""IMP-005 (execution confirmation and protection) and IMP-006 (aggregate limits and strategy ownership)."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from metatrader_client.order import (
    cancel_pending_order,
    close_all_positions,
    close_position,
    disable_new_entries,
    enable_new_entries,
    get_risk_limits,
    modify_position,
    place_market_order,
    place_pending_order,
)
from metatrader_client.risk import RiskConfig
from metatrader_client.store import Store
from metatrader_client.strategy import strategy_magic
from metatrader_client.types import TradeReturnCodes
from .fakes import make_deal, make_order, make_position, simulate_trading, with_risk

TIMEOUT = 5
A, B = "alpha", "beta"


# ---------------------------------------------------------------------------
# IMP-005: confirmation of openings
# ---------------------------------------------------------------------------

def test_filled_order_is_confirmed_with_its_protection(fake_mt5, connection):
    simulate_trading(fake_mt5)
    result = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1, stop_loss=1.09, take_profit=1.12)

    assert result["error"] is False
    assert result["verification"] == "confirmed"
    assert result["protection"] == {"requested_sl": 1.09, "requested_tp": 1.12, "effective_sl": 1.09, "effective_tp": 1.12, "status": "protected"}
    checks = result["checks"]
    assert {"deal_in_history", "position_listed", "volume", "protection"} <= set(checks["verified"])
    assert checks["unknown"] == [] and checks["checked_at"].endswith("Z")
    execution = result["execution"]
    assert execution["filled_volume"] == 0.1 and execution["slippage_points"] == 0.0
    assert execution["send_ms"] is not None and execution["confirm_ms"] is not None
    assert "swap (accrues while open)" in execution["costs_unknown"]


def test_requested_stop_loss_missing_on_the_position_is_never_reported_as_protected(fake_mt5, connection):
    simulate_trading(fake_mt5, keep_sl_tp=False)
    result = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1, stop_loss=1.09)

    assert result["verification"] == "protection_missing"
    assert result["protection"]["status"] == "missing"
    assert "NOT protected" in result["message"]
    assert result["protection_action"] == {"policy": "report"}
    assert len(fake_mt5.sent) == 1  # report only: nothing else is sent


def test_retry_sltp_policy_sets_and_verifies_the_levels(fake_mt5, connection):
    simulate_trading(fake_mt5, keep_sl_tp=False)
    with_risk(connection, duplicate_window_seconds=0, missing_protection_policy="retry_sltp")

    result = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1, stop_loss=1.09)

    assert result["protection"]["status"] == "protected"
    assert result["verification"] == "confirmed"
    assert result["protection_action"]["verification"] == "confirmed"
    assert [r["action"] for r in fake_mt5.sent] == [1, 6]


def test_close_policy_closes_an_unprotected_position(fake_mt5, connection):
    simulate_trading(fake_mt5, keep_sl_tp=False)
    with_risk(connection, duplicate_window_seconds=0, missing_protection_policy="close")

    result = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1, stop_loss=1.09)

    assert result["protection_action"]["verification"] == "confirmed"
    assert fake_mt5.positions == []


def test_accepted_market_order_is_not_reported_as_filled(fake_mt5, connection):
    fake_mt5.default_retcode = TradeReturnCodes.PLACED.value
    result = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1)

    assert result["status"] == "placed"
    assert result["message"].startswith("Accepted") and "Filled" not in result["message"]
    assert result["verification"] == "pending"


def test_partial_fill_and_late_publication_keep_distinct_states(fake_mt5, connection):
    fake_mt5.results = [lambda request: fake_mt5.result_for(request, TradeReturnCodes.DONE_PARTIAL.value, volume=0.04)]
    simulate_trading(fake_mt5)
    partial = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1)
    assert partial["verification"] == "partial" and partial["execution"]["filled_volume"] == 0.04

    fake_mt5.on_send = None  # the position is not published yet
    late = place_market_order(connection, type="SELL", symbol="EURUSD", volume=0.1)
    assert late["error"] is False and late["verification"] == "unknown"
    assert "position_listed" in late["checks"]["unknown"]
    assert "warning" in late


def test_rejection_and_no_response_are_not_confirmed(fake_mt5, connection):
    fake_mt5.results = [lambda request: fake_mt5.result_for(request, TradeReturnCodes.NO_MONEY.value), None]
    rejected = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1)
    lost = place_market_order(connection, type="SELL", symbol="EURUSD", volume=0.2)

    assert rejected["status"] == "rejected" and "verification" not in rejected
    assert lost["status"] == "no_response" and lost["verification"] == "unknown"


def test_slippage_is_measured_against_the_requested_price(fake_mt5, connection):
    def worse_fill(request):
        result = fake_mt5.result_for(request)
        return result._replace(price=round(request["price"] + 0.00003, 5))

    fake_mt5.results = [worse_fill]
    result = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1)
    assert result["execution"]["slippage_points"] == pytest.approx(3.0)


def test_pending_order_is_confirmed_in_the_listing(fake_mt5, connection):
    simulate_trading(fake_mt5)
    fake_mt5.default_retcode = TradeReturnCodes.PLACED.value
    result = place_pending_order(connection, type="BUY", symbol="EURUSD", volume=0.1, price=1.09, stop_loss=1.08)
    assert result["verification"] == "confirmed"
    assert result["protection"]["status"] == "protected"


def test_modification_not_applied_is_reported_as_mismatch(fake_mt5, connection):
    fake_mt5.apply_modifications = False
    fake_mt5.positions = [make_position(7, sl=1.095)]

    result = modify_position(connection, 7, stop_loss=1.09)

    assert result["error"] is True and result["verification"] == "mismatch"
    assert "could not be confirmed" in result["message"]


def test_cancelled_order_still_listed_is_an_error(fake_mt5, connection):
    fake_mt5.orders = [make_order(55)]
    result = cancel_pending_order(connection, 55)
    assert result["error"] is True and result["verification"] == "remaining"

    simulate_trading(fake_mt5)
    assert cancel_pending_order(connection, 55)["verification"] == "confirmed"


# ---------------------------------------------------------------------------
# IMP-006: aggregate limits
# ---------------------------------------------------------------------------

def test_small_entries_and_pending_orders_count_towards_the_daily_entries_limit(fake_mt5, connection):
    with_risk(connection, max_entries_per_day=3, duplicate_window_seconds=0)
    fake_mt5.deals = [make_deal(1, type=0, entry=0), make_deal(2, type=1, entry=0), make_deal(3, type=1, entry=1)]
    fake_mt5.orders = [make_order(9)]

    result = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.01)

    assert result["error"] is True and "Daily entries limit of 3" in result["message"]
    assert fake_mt5.sent == []


def test_aggregate_risk_includes_positions_pending_orders_and_the_new_order(fake_mt5, connection):
    with_risk(connection, max_aggregate_risk_pct=1.0, duplicate_window_seconds=0)
    fake_mt5.positions = [make_position(1, price_open=1.1, sl=1.095)]  # loss 50
    fake_mt5.orders = [make_order(2, price_open=1.09, sl=1.087)]       # loss 30

    allowed = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.01, stop_loss=1.0901)   # ~9.9
    assert allowed["error"] is False

    blocked = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.02, stop_loss=1.0901)  # +~19.8 > 100
    assert blocked["error"] is True and "Aggregate loss at stop losses" in blocked["message"]


def test_position_without_stop_loss_makes_the_aggregate_risk_unknown(fake_mt5, connection):
    with_risk(connection, max_aggregate_risk_pct=5.0, duplicate_window_seconds=0)
    fake_mt5.positions = [make_position(1, sl=0.0)]
    result = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.01, stop_loss=1.09)
    assert result["error"] is True and "position 1 has no stop loss" in result["message"]


def test_unreadable_positions_block_strategy_limits(fake_mt5, connection, monkeypatch):
    connection.risk_guard.config = RiskConfig(duplicate_window_seconds=0, strategy_limits={A: {"max_total_volume": 1.0}})
    monkeypatch.setattr(fake_mt5, "positions_get", lambda **kwargs: None)
    result = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.01, strategy_id=A)
    assert result["error"] is True and f"[strategy {A}]" in result["message"]
    assert fake_mt5.sent == []


def test_strategy_limits_only_count_the_strategy_items(fake_mt5, connection):
    connection.risk_guard.config = RiskConfig(duplicate_window_seconds=0, strategy_limits={A: {"max_open_positions": 1}})
    fake_mt5.positions = [make_position(1, magic=0), make_position(2, magic=strategy_magic(B))]

    first = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.01, strategy_id=A)
    second = place_market_order(connection, type="SELL", symbol="EURUSD", volume=0.01, strategy_id=A)
    other = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.02, strategy_id=B)

    assert first["error"] is False
    assert fake_mt5.sent[0]["magic"] == strategy_magic(A)
    assert second["error"] is True and "recently sent and not listed yet" in second["message"]
    assert other["error"] is False


def test_concurrent_orders_of_a_strategy_cannot_exceed_its_limit(fake_mt5, connection):
    connection.risk_guard.config = RiskConfig(duplicate_window_seconds=0, strategy_limits={A: {"max_total_volume": 0.15}})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda side: place_market_order(connection, type=side, symbol="EURUSD", volume=0.1, strategy_id=A),
            ["BUY", "SELL"],
        ))
    assert sorted(r["error"] for r in results) == [False, True]
    assert len(fake_mt5.sent) == 1


def test_magic_that_does_not_match_the_strategy_is_rejected(fake_mt5, connection):
    from metatrader_client.order import send_order
    result = send_order(connection, action="DEAL", order_type="BUY", symbol="EURUSD", volume=0.1, strategy_id=A, magic=1)
    assert result["success"] is False and "does not match" in result["message"]


# ---------------------------------------------------------------------------
# IMP-006: ownership, bulk scope and the new entries switch
# ---------------------------------------------------------------------------

def test_single_ticket_of_another_strategy_is_not_changed(fake_mt5, connection):
    fake_mt5.positions = [make_position(1, magic=strategy_magic(B), sl=1.095)]

    closed = close_position(connection, 1, strategy_id=A)
    modified = modify_position(connection, 1, stop_loss=1.09, strategy_id=A)

    assert closed["error"] is True and "not to strategy alpha" in closed["message"]
    assert modified["error"] is True
    assert fake_mt5.sent == []


def test_bulk_close_only_touches_the_requested_scope(fake_mt5, connection):
    simulate_trading(fake_mt5)
    fake_mt5.positions = [make_position(1, magic=strategy_magic(A)), make_position(2, magic=0), make_position(3, magic=strategy_magic(B))]

    preview = close_all_positions(connection, scope="strategy", strategy_id=A, dry_run=True)
    assert preview["data"]["dry_run"] is True and [i["id"] for i in preview["data"]["items"]] == [1]
    assert preview["data"]["items"][0]["ownership"] == "own"
    assert fake_mt5.sent == []

    result = close_all_positions(connection, scope="strategy", strategy_id=A)
    assert result["error"] is False and result["data"]["succeeded"] == [1]
    assert sorted(p.ticket for p in fake_mt5.positions) == [2, 3]

    manual = close_all_positions(connection, scope="manual")
    assert manual["data"]["succeeded"] == [2]
    assert [p.ticket for p in fake_mt5.positions] == [3]


@pytest.mark.parametrize("scope, strategy_id, message", [
    ("strategy", None, "requires strategy_id"),
    ("everything", None, "Invalid scope"),
    ("manual", A, "only be combined"),
])
def test_ambiguous_bulk_scope_is_rejected(fake_mt5, connection, scope, strategy_id, message):
    fake_mt5.positions = [make_position(1)]
    result = close_all_positions(connection, scope=scope, strategy_id=strategy_id)
    assert result["error"] is True and message in result["message"]
    assert fake_mt5.sent == []


def test_disabling_new_entries_still_allows_reducing_exposure(fake_mt5, connection):
    connection.store = Store(":memory:")
    simulate_trading(fake_mt5)
    fake_mt5.positions = [make_position(1)]

    assert disable_new_entries(connection, reason="")["error"] is True  # a reason is required
    disable_new_entries(connection, reason="drawdown review")
    blocked = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1)
    closed = close_position(connection, 1)

    assert blocked["error"] is True and "drawdown review" in blocked["message"]
    assert closed["verification"] == "confirmed"

    enable_new_entries(connection, reason="review done")
    assert place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1)["error"] is False
    history = get_risk_limits(connection)["data"]["new_entries_history"]
    assert [h["value"]["enabled"] for h in history] == [True, False]


def test_limit_changes_are_logged(connection):
    store = Store(":memory:")
    assert store.record_limits({"max_total_volume": 1.0}, source="startup") is True
    assert store.record_limits({"max_total_volume": 1.0}, source="startup") is False
    assert store.record_limits({"max_total_volume": 2.0}, source="startup") is True
    assert [h["limits"]["max_total_volume"] for h in store.limits_history()] == [2.0, 1.0]


def test_strategy_limits_from_the_environment(monkeypatch):
    monkeypatch.setenv("MT5_STRATEGY_LIMITS", '{"alpha": {"max_total_volume": 0.5, "max_entries_per_day": 3}}')
    config = RiskConfig.from_sources()
    assert config.limits_for("alpha") == {"max_total_volume": 0.5, "max_entries_per_day": 3}
    monkeypatch.setenv("MT5_STRATEGY_LIMITS", '{"alpha": {"max_lots": 1}}')
    with pytest.raises(ValueError):
        RiskConfig.from_sources()
