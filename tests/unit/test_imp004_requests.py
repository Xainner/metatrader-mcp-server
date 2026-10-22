"""IMP-004: durable request identity, replays, conflicts, crashes and reconciliation."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from metatrader_client.order import (
    close_position,
    get_request_status,
    place_market_order,
    reconcile_request,
    resolve_request,
)
from metatrader_client.requests_registry import RequestRegistry, request_tag
from metatrader_client.risk import RiskConfig, RiskGuard
from metatrader_client.store import Store
from .fakes import make_order, make_position, make_symbol, make_tick, simulate_trading

TIMEOUT = 5


def connection_with(store, **registry):
    return SimpleNamespace(
        risk_guard=RiskGuard(RiskConfig(duplicate_window_seconds=0)),
        store=store,
        request_registry=RequestRegistry(store, **registry),
    )


@pytest.fixture
def store():
    return Store(":memory:")


def test_repeating_a_request_id_returns_the_known_state_without_sending(fake_mt5, store):
    connection = connection_with(store)
    first = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1, request_id="intent-1")
    again = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1, request_id="intent-1")

    assert first["error"] is False and first["request_state"] == "filled"
    assert again["replayed"] is True
    assert again["request_state"] == "filled" and again["status"] == "done"
    assert again["previous_result"]["request_id"] == "intent-1"
    assert "nothing was sent again" in again["message"]
    assert len(fake_mt5.sent) == 1


def test_reusing_a_request_id_with_other_parameters_is_a_conflict(fake_mt5, store):
    connection = connection_with(store)
    place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1, request_id="intent-1")
    conflict = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.2, request_id="intent-1")

    assert conflict["error"] is True
    assert "already used for a different request" in conflict["message"]
    assert len(fake_mt5.sent) == 1


def test_order_comment_carries_the_request_tag_within_31_characters(fake_mt5, store):
    place_market_order(connection_with(store), type="BUY", symbol="EURUSD", volume=0.1, request_id="intent-1")
    comment = fake_mt5.sent[0]["comment"]
    assert request_tag("intent-1") in comment and len(comment) <= 31


def test_same_request_id_concurrently_sends_once(fake_mt5, store):
    connection = connection_with(store)
    entered, release = threading.Event(), threading.Event()
    original = fake_mt5.order_send

    def slow_send(request):
        entered.set()
        release.wait(TIMEOUT)
        return original(request)

    fake_mt5.order_send = slow_send
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(place_market_order, connection, type="BUY", symbol="EURUSD", volume=0.1, request_id="same")
        assert entered.wait(TIMEOUT)
        second = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1, request_id="same")
        release.set()
        first = first.result(TIMEOUT)

    assert second["replayed"] is True and second["request_state"] == "sending"
    assert "still in progress" in second["message"]
    assert first["request_state"] == "filled"
    assert len(fake_mt5.sent) == 1


def test_same_request_id_after_a_restart_is_not_sent_again(fake_mt5, tmp_path):
    path = str(tmp_path / "state.sqlite3")
    place_market_order(connection_with(Store(path)), type="BUY", symbol="EURUSD", volume=0.1, request_id="intent-1")

    restarted = connection_with(Store(path))  # new process: new store object on the same file
    again = place_market_order(restarted, type="BUY", symbol="EURUSD", volume=0.1, request_id="intent-1")

    assert again["replayed"] is True and again["request_state"] == "filled"
    assert len(fake_mt5.sent) == 1


def test_crash_before_sending_is_reported_as_not_sent(fake_mt5, store):
    registry = RequestRegistry(store, stale_after=0)
    registry.begin("intent-1", params={"x": 1}, kind="open", symbol="EURUSD")  # process stops here

    record = registry.get("intent-1")

    assert record["state"] == "rejected" and record["outcome"] == "not_sent"


def test_crash_during_the_send_blocks_the_symbol_until_resolved(fake_mt5, store):
    connection = connection_with(store, stale_after=0)
    registry = connection.request_registry
    registry.begin("lost", params={"x": 1}, kind="open", account=5_000_001, server="Demo-Server", symbol="EURUSD")
    registry.mark("lost", "sending")  # process stops during order_send

    blocked = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1)
    other_symbol_ok = None
    fake_mt5.symbols["GBPUSD"] = make_symbol(name="GBPUSD")
    fake_mt5.ticks["GBPUSD"] = make_tick(1.25, 1.2501)
    other_symbol_ok = place_market_order(connection, type="BUY", symbol="GBPUSD", volume=0.1)

    assert blocked["error"] is True and "lost" in blocked["message"] and "unknown outcome" in blocked["message"]
    assert other_symbol_ok["error"] is False
    assert get_request_status(connection, "lost")["data"]["record"]["state"] == "unknown"

    not_found = reconcile_request(connection, "lost")
    assert not_found["error"] is True and not_found["data"]["record"]["state"] == "unknown"

    assert resolve_request(connection, "lost", decision="not_executed", reason="")["error"] is True  # a reason is required
    resolved = resolve_request(connection, "lost", decision="not_executed", reason="checked the terminal history")
    assert resolved["data"]["state"] == "reconciled"
    assert place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1)["error"] is False
    events = get_request_status(connection, "lost")["data"]["events"]
    assert any("checked the terminal history" in (e["detail"] or "") for e in events)


def test_crash_after_execution_is_reconciled_from_the_history(fake_mt5, store):
    connection = connection_with(store, stale_after=0)
    simulate_trading(fake_mt5)

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("process stopped before the result was stored")

    connection.request_registry.record_send_result = crash
    with pytest.raises(KeyboardInterrupt):
        place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1, request_id="executed")
    del connection.request_registry.record_send_result

    # After the restart the request is unknown and blocks the symbol; nothing is resent.
    status = get_request_status(connection, "executed")["data"]["record"]
    assert status["state"] == "unknown"
    again = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1, request_id="executed")
    assert again["replayed"] is True
    assert len(fake_mt5.sent) == 1

    reconciled = reconcile_request(connection, "executed")
    assert reconciled["error"] is False
    assert reconciled["data"]["outcome"] == "filled"
    assert reconciled["data"]["record"]["state"] == "reconciled"
    assert reconciled["data"]["evidence"]["filled_volume"] == 0.1
    assert place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1)["error"] is False


def test_no_response_keeps_the_request_unknown(fake_mt5, store):
    connection = connection_with(store)
    fake_mt5.results = [None]
    result = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1, request_id="lost")
    assert result["request_state"] == "unknown" and result["verification"] == "unknown"
    retry = place_market_order(connection, type="BUY", symbol="EURUSD", volume=0.1, request_id="lost")
    assert retry["replayed"] is True and len(fake_mt5.sent) == 1


def test_pending_order_still_working_is_reconciled_as_accepted(fake_mt5, store):
    connection = connection_with(store, stale_after=0)
    registry = connection.request_registry
    registry.begin("pending", params={"volume": 0.1}, kind="open", symbol="EURUSD")
    registry.mark("pending", "sending")
    fake_mt5.orders = [make_order(77, comment=f"MCP {request_tag('pending')}")]

    result = reconcile_request(connection, "pending")

    assert result["data"]["outcome"] == "accepted"
    assert result["data"]["record"]["order_ticket"] == 77


def test_repeated_close_with_the_same_request_id_is_not_sent_again(fake_mt5, store):
    connection = connection_with(store)
    simulate_trading(fake_mt5)
    fake_mt5.positions = [make_position(42)]

    first = close_position(connection, 42, request_id="close-42")
    again = close_position(connection, 42, request_id="close-42")

    assert first["verification"] == "confirmed"
    assert again["replayed"] is True and again["verification"] == "confirmed"
    assert len(fake_mt5.sent) == 1


def test_purge_keeps_unresolved_requests(store):
    registry = RequestRegistry(store, stale_after=3600)
    registry.begin("done", params={}, kind="open")
    registry.mark("done", "filled")
    registry.begin("lost", params={}, kind="open")
    registry.mark("lost", "unknown")
    store.execute("UPDATE requests SET updated_at = ?", (time.time() - 200 * 86400,))

    assert registry.purge(older_than_days=90) == 1
    assert registry.get("done") is None
    assert registry.get("lost")["state"] == "unknown"


def test_store_schema_is_versioned(store):
    assert store.schema_version == 4
