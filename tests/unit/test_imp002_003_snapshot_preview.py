"""IMP-002 (trading snapshot) and IMP-003 (monetary calculations and pre-trade check)."""

import time

import pytest

from metatrader_client.market import get_symbol_specifications
from metatrader_client.order import (
    calculate_required_margin,
    calculate_trade_outcome,
    get_trading_snapshot,
    place_market_order,
    place_pending_order,
    preview_order,
)
from metatrader_client.order._preview_store import PreviewStore
from .fakes import make_order, make_position, make_symbol, make_tick, with_risk


# ---------------------------------------------------------------------------
# IMP-002: snapshot
# ---------------------------------------------------------------------------

def test_snapshot_reports_quote_account_positions_orders_and_limits(fake_mt5, connection):
    fake_mt5.positions = [make_position(1, profit=12.5, sl=1.09), make_position(2, symbol="GBPUSD", profit=-3)]
    fake_mt5.orders = [make_order(9)]
    with_risk(connection, max_total_volume=1.0, max_open_positions=5, duplicate_window_seconds=0)

    result = get_trading_snapshot(connection, symbol="EURUSD")

    assert result["error"] is False
    data = result["data"]
    assert data["quote"]["status"] == "ok"
    assert data["quote"]["spread_points"] == 10.0
    assert data["quote"]["time_basis"] == "server"
    assert data["quote"]["age_seconds"] is None  # server offset unknown: not invented
    assert data["account"]["currency"] == "USD" and data["account"]["scope"] == "account"
    assert data["positions"]["floating_pnl"] == 12.5
    assert data["positions"]["currency"] == "USD"
    assert [p["ticket"] for p in data["positions"]["items"]] == [1]
    assert data["pending_orders"]["count"] == 1
    usage = data["limits"]["usage"]
    assert usage["total_volume"] == pytest.approx(0.3)
    assert usage["volume_available"] == pytest.approx(0.7)
    assert usage["slots_available"] == 2
    for component in ("quote", "account", "positions", "pending_orders"):
        assert data[component]["source"] == "mt5" and data[component]["captured_at"]
    assert data["capture"]["atomic"] is False
    assert data["capture"]["terminal_calls"] >= 6


def test_snapshot_read_error_is_explicit_never_an_empty_list_or_zero_pnl(fake_mt5, connection, monkeypatch):
    monkeypatch.setattr(fake_mt5, "positions_get", lambda **kwargs: None)
    fake_mt5.error = (-10004, "No IPC connection")

    result = get_trading_snapshot(connection, symbol="EURUSD")

    assert result["error"] is True
    positions = result["data"]["positions"]
    assert positions["status"] == "error"
    assert positions["items"] is None and positions["floating_pnl"] is None
    assert "-10004" in positions["error"]
    assert result["data"]["limits"]["usage"]["status"] == "unknown"
    assert result["data"]["quote"]["status"] == "ok"  # other components are still returned
    assert {e["component"] for e in result["data"]["errors"]} >= {"positions"}


def test_snapshot_quote_age_uses_the_configured_server_offset(fake_mt5, connection, monkeypatch):
    monkeypatch.setenv("MT5_SERVER_UTC_OFFSET_SECONDS", "10800")
    now = time.time()
    fake_mt5.ticks["EURUSD"] = make_tick(1.1, 1.1001, time=int(now) + 10800 - 5)

    quote = get_trading_snapshot(connection, symbol="EURUSD")["data"]["quote"]

    assert quote["time_basis"] == "utc"
    assert 4 <= quote["age_seconds"] <= 7


def test_snapshot_missing_tick_is_an_error_component(fake_mt5, connection):
    del fake_mt5.ticks["EURUSD"]
    result = get_trading_snapshot(connection, symbol="EURUSD")
    assert result["error"] is True
    assert result["data"]["quote"]["status"] == "error"


# ---------------------------------------------------------------------------
# IMP-003: specifications and calculations
# ---------------------------------------------------------------------------

def test_symbol_specifications_are_the_broker_values(fake_mt5, connection):
    fake_mt5.symbols["XAUUSDm"] = make_symbol(name="XAUUSDm", digits=3, point=0.001, trade_tick_size=0.001,
                                              trade_contract_size=100.0, volume_min=0.01, volume_step=0.01)
    del fake_mt5.symbols["XAUUSDm"].trade_tick_value

    data = get_symbol_specifications(connection, "XAUUSDm")["data"]

    assert data["trade_contract_size"] == 100.0 and data["digits"] == 3
    assert data["filling_modes"] == ["FOK"]
    assert data["default_filling_market"] == "FOK" and data["default_filling_pending"] == "RETURN"
    assert data["execution_mode_name"] == "MARKET" and data["trade_mode_name"] == "FULL"
    assert "trade_tick_value" in data["unavailable"]


@pytest.mark.parametrize("side, exit_price, expected", [("BUY", 1.1010, 10.0), ("SELL", 1.1010, -10.0), ("BUY", 1.0990, -10.0)])
def test_trade_outcome_sign_and_currency(fake_mt5, connection, side, exit_price, expected):
    fake_mt5.account.currency = "EUR"
    data = calculate_trade_outcome(connection, symbol="EURUSD", type=side, volume=0.1, entry_price=1.1, exit_price=exit_price)["data"]
    assert data["profit"] == pytest.approx(expected)
    assert data["currency"] == "EUR"
    assert "commission" in data["excludes"]


def test_outcome_uses_the_symbol_contract_size(fake_mt5, connection):
    fake_mt5.symbols["XAUUSDm"] = make_symbol(name="XAUUSDm", trade_contract_size=100.0)
    data = calculate_trade_outcome(connection, symbol="XAUUSDm", type="BUY", volume=1, entry_price=2000, exit_price=2001)["data"]
    assert data["profit"] == pytest.approx(100.0)


def test_calculation_failure_is_an_error(fake_mt5, connection):
    assert calculate_trade_outcome(connection, symbol="UNKNOWN", type="BUY", volume=1, entry_price=1, exit_price=2)["error"] is True
    assert calculate_required_margin(connection, symbol="UNKNOWN", type="BUY", volume=1, price=1)["error"] is True


def test_required_margin_uses_the_current_quote(fake_mt5, connection):
    data = calculate_required_margin(connection, symbol="EURUSD", type="BUY", volume=0.1)["data"]
    assert data["price"] == 1.1001
    assert data["margin"] == pytest.approx(110.01)
    assert data["free_margin_after"] == pytest.approx(10_000 - 110.01)


# ---------------------------------------------------------------------------
# IMP-003: preview
# ---------------------------------------------------------------------------

def test_preview_with_volume_is_ready_and_checked(fake_mt5, connection):
    result = preview_order(connection, symbol="EURUSD", type="BUY", volume=0.1, stop_loss=1.0990, take_profit=1.1031)

    assert result["error"] is False
    data = result["data"]
    assert data["ready"] and data["preview_id"]
    assert data["loss_at_stop"] == pytest.approx(11.0)
    assert data["profit_at_target"] == pytest.approx(30.0)
    assert data["reward_risk"] == pytest.approx(30 / 11, rel=1e-3)
    assert data["order_check"]["status"] == "ok"
    assert data["costs"]["known"]["spread_round_trip"] == pytest.approx(1.0)
    assert data["send_with"]["tool"] == "place_market_order"
    assert fake_mt5.sent == []  # nothing is sent by a preview


@pytest.mark.parametrize("side, sl", [("BUY", 1.0951), ("SELL", 1.1050)])
def test_risk_sizing_rounds_down_and_never_exceeds_the_amount(fake_mt5, connection, side, sl):
    result = preview_order(connection, symbol="EURUSD", type=side, risk_amount=100, stop_loss=sl)

    data = result["data"]
    assert data["ready"]
    assert data["loss_at_stop"] <= 100
    # One more step would exceed the requested risk.
    entry = data["entry_price"]
    assert abs(sl - entry) * (data["volume"] + 0.01) * 100_000 > 100


def test_risk_too_small_for_the_minimum_volume_is_rejected(fake_mt5, connection):
    result = preview_order(connection, symbol="EURUSD", type="BUY", risk_amount=0.5, stop_loss=1.0901)
    assert result["error"] is True
    assert "too small for the minimum volume" in result["message"]


def test_risk_amount_requires_a_stop_loss(fake_mt5, connection):
    assert "needs a stop_loss" in preview_order(connection, symbol="EURUSD", type="BUY", risk_amount=50)["message"]


def test_order_check_rejection_blocks_the_preview(fake_mt5, connection):
    fake_mt5.check_retcode = 10019
    result = preview_order(connection, symbol="EURUSD", type="BUY", volume=0.1)
    assert result["error"] is True
    assert result["data"]["preview_id"] is None
    assert "order_check rejected" in result["message"]


def test_unavailable_loss_calculation_blocks_the_preview(fake_mt5, connection, monkeypatch):
    monkeypatch.setattr(fake_mt5, "order_calc_profit", lambda *args: None)
    result = preview_order(connection, symbol="EURUSD", type="BUY", volume=0.1, stop_loss=1.09)
    assert result["error"] is True and result["data"]["preview_id"] is None


def test_risk_limits_are_part_of_the_preview(fake_mt5, connection):
    with_risk(connection, max_volume_per_order=0.05, duplicate_window_seconds=0)
    result = preview_order(connection, symbol="EURUSD", type="BUY", volume=0.1)
    assert result["data"]["risk_check"]["allowed"] is False
    assert result["error"] is True


def test_order_sent_with_its_preview_is_accepted_once(fake_mt5, connection):
    preview = preview_order(connection, symbol="EURUSD", type="BUY", volume=0.1, stop_loss=1.099)["data"]
    args = preview["send_with"]["arguments"]

    first = place_market_order(connection, **args)
    second = place_market_order(connection, **{**args, "request_id": "another-intent"})

    assert first["error"] is False
    assert "already used" in second["message"]
    assert len(fake_mt5.sent) == 1


def test_material_change_after_preview_is_rejected(fake_mt5, connection):
    preview = preview_order(connection, symbol="EURUSD", type="BUY", volume=0.1, stop_loss=1.099)["data"]
    args = {**preview["send_with"]["arguments"], "volume": 0.2}

    result = place_market_order(connection, **args)

    assert result["error"] is True and "differs from preview" in result["message"]
    assert fake_mt5.sent == []


def test_price_move_after_preview_is_rejected(fake_mt5, connection):
    preview = preview_order(connection, symbol="EURUSD", type="BUY", volume=0.1)["data"]
    fake_mt5.ticks["EURUSD"] = make_tick(1.1003, 1.1004)  # 30 points, the deviation is 20

    result = place_market_order(connection, **preview["send_with"]["arguments"])

    assert result["error"] is True and "price moved 30.0 points" in result["message"]
    assert fake_mt5.sent == []


def test_expired_preview_is_rejected(fake_mt5, connection):
    connection.preview_store = PreviewStore(ttl=-1)
    preview = preview_order(connection, symbol="EURUSD", type="BUY", volume=0.1)["data"]
    result = place_market_order(connection, **preview["send_with"]["arguments"])
    assert "expired" in result["message"] and fake_mt5.sent == []


def test_required_preview_blocks_orders_without_one(fake_mt5, connection):
    connection.preview_store = PreviewStore(require_preview=True)
    result = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1)
    assert "preview is required" in result["message"] and fake_mt5.sent == []


def test_pending_preview_round_trip(fake_mt5, connection):
    preview = preview_order(connection, symbol="EURUSD", type="BUY", volume=0.1, price=1.0950, stop_loss=1.0900)["data"]
    assert preview["order_type"] == "BUY_LIMIT"
    fake_mt5.default_retcode = 10008  # PLACED
    result = place_pending_order(connection, **preview["send_with"]["arguments"])
    assert result["error"] is False and result["status"] == "placed"
