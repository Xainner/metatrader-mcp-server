"""IMP-001: bridge deadlines, busy detection, reconnection, health and non-blocking logs."""

import asyncio
import json
import logging
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import metatrader_mcp.server as server
from metatrader_mcp.bridge import Bridge, BridgeBusy, BridgeTimeout, BridgeUnavailable, ReconnectPolicy
from metatrader_mcp.logging_config import RedactSecrets, SafeRotatingFileHandler, configure_logging, logging_state

SRC = Path(__file__).resolve().parents[2] / "src"
TIMEOUT = 5  # safety net only; the tests synchronize on events


def run(coro):
    return asyncio.run(coro)


def bridge_with(client=None, **env):
    values = {"MCP_READ_TIMEOUT_SECONDS": "0.2", "MCP_SEND_TIMEOUT_SECONDS": "0.2"}
    values.update(env)
    return Bridge(client, env=values)


# ---------------------------------------------------------------------------
# Deadlines and busy detection
# ---------------------------------------------------------------------------

def test_blocked_read_times_out_then_new_calls_fail_fast_until_it_finishes():
    bridge = bridge_with()
    release = threading.Event()
    calls = []

    async def scenario():
        with pytest.raises(BridgeTimeout) as timeout:
            await bridge.run("get_symbol_price", "read", lambda: release.wait(TIMEOUT))
        assert timeout.value.stage == "read"
        assert "not a confirmed cancellation" in str(timeout.value)
        assert bridge.state()["blocked"]["tool"] == "get_symbol_price"

        with pytest.raises(BridgeBusy) as busy:
            await bridge.run("get_all_positions", "read", lambda: calls.append("ran"))
        assert busy.value.state == "not_started"
        assert "get_symbol_price" in str(busy.value)

        release.set()
        # The blocked call finishes; the bridge accepts calls again.
        for _ in range(100):
            if bridge.state()["blocked"] is None:
                break
            await asyncio.sleep(0.01)
        assert await bridge.run("get_all_positions", "read", lambda: "ok") == "ok"

    run(scenario())
    assert calls == []
    assert bridge.late_completions == 1
    bridge.shutdown()


def test_send_timeout_is_reported_as_unknown_not_cancelled():
    bridge = bridge_with()
    release = threading.Event()

    async def scenario():
        with pytest.raises(BridgeTimeout) as timeout:
            await bridge.run("place_market_order", "send", lambda: release.wait(TIMEOUT))
        return timeout.value

    error = run(scenario())
    release.set()
    assert error.state == "unknown"
    assert "Check the request status" in str(error)
    bridge.shutdown()


def test_queued_call_whose_caller_timed_out_is_never_started():
    bridge = bridge_with(MCP_READ_TIMEOUT_SECONDS="5", MCP_SEND_TIMEOUT_SECONDS="0.2")
    entered, release = threading.Event(), threading.Event()
    sent = []

    def blocking_read():
        entered.set()
        release.wait(TIMEOUT)
        return "read done"

    async def scenario():
        reader = asyncio.ensure_future(bridge.run("slow_read", "read", blocking_read))
        await asyncio.get_running_loop().run_in_executor(None, entered.wait, TIMEOUT)
        with pytest.raises(BridgeTimeout) as timeout:
            await bridge.run("place_market_order", "send", lambda: sent.append("order"))
        release.set()
        assert await reader == "read done"
        return timeout.value

    error = run(scenario())
    assert error.state == "not_started"
    assert "nothing was sent" in str(error)
    bridge._executor.submit(lambda: None).result(TIMEOUT)  # drain the worker queue
    assert sent == []
    bridge.shutdown()


# ---------------------------------------------------------------------------
# Reconnection
# ---------------------------------------------------------------------------

def test_disconnected_terminal_is_reconnected_once_and_the_call_runs():
    client = MagicMock()
    client.is_connected.side_effect = [False, True]
    bridge = bridge_with(client)

    assert run(bridge.run("get_account_info", "read", lambda: "ok")) == "ok"
    client.connect.assert_called_once()
    assert bridge.state()["reconnect"]["last_success"] is not None
    bridge.shutdown()


def test_failed_reconnection_is_bounded_and_never_runs_the_call():
    client = MagicMock()
    client.is_connected.return_value = False
    client.connect.side_effect = RuntimeError("terminal closed")
    bridge = bridge_with(client, MCP_RECONNECT_MAX_ATTEMPTS="1")
    calls = []

    with pytest.raises(BridgeUnavailable) as first:
        run(bridge.run("place_market_order", "send", lambda: calls.append("order")))
    assert "terminal closed" in str(first.value)
    with pytest.raises(BridgeUnavailable) as second:
        run(bridge.run("place_market_order", "send", lambda: calls.append("order")))
    assert "no reconnection is attempted now" in str(second.value)

    assert calls == []
    assert client.connect.call_count == 1
    bridge.shutdown()


def test_reconnect_policy_backoff_and_window():
    policy = ReconnectPolicy(max_attempts=3, window=100, base_delay=2)
    assert policy.allowed(now=0) is None
    policy.attempts.append(0)
    assert "allowed in" in policy.allowed(now=1)
    assert policy.allowed(now=2) is None
    policy.attempts += [2, 6]
    assert "limit 3" in policy.allowed(now=7)
    assert policy.allowed(now=106) is None  # attempts left the window


# ---------------------------------------------------------------------------
# MCP tools on the bridge
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_client(monkeypatch):
    client = MagicMock()
    client.connection = SimpleNamespace(login=12345678, server="Demo-Server")
    monkeypatch.setattr(server, "init", lambda *args, **kwargs: client)
    monkeypatch.setenv("MT5_SUPERVISOR_ENABLED", "false")
    return client


async def call(tool, arguments=None):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
        return await session.call_tool(tool, arguments or {})


@pytest.mark.anyio
async def test_health_masks_the_login_and_reports_bridge_and_metrics(fake_client):
    result = await call("get_bridge_health")

    assert not result.isError
    health = json.loads(result.content[0].text)
    assert health["account_reference"] == {"login": "*****678", "server": "Demo-Server"}
    assert health["build"]["version"]
    assert set(health["bridge"]["timeouts_seconds"]) == {"read", "preview", "send"}
    assert "stages" in health["metrics"]
    text = result.content[0].text.lower()
    assert "password" not in text


@pytest.mark.anyio
async def test_trading_tool_timeout_returns_unknown_with_request_id(fake_client, monkeypatch):
    monkeypatch.setenv("MCP_SEND_TIMEOUT_SECONDS", "0.2")
    release = threading.Event()
    fake_client.order.place_market_order.side_effect = lambda **kwargs: release.wait(TIMEOUT)

    result = await call("place_market_order", {"symbol": "EURUSD", "volume": 0.1, "type": "BUY", "request_id": "intent-1"})
    release.set()

    payload = json.loads(result.content[0].text)
    assert payload["error"] is True
    assert payload["status"] == "no_response"
    assert payload["verification"] == "unknown"
    assert payload["stage"] == "send"
    assert payload["request_id"] == "intent-1"
    assert "not a confirmed cancellation" in payload["message"]


@pytest.mark.anyio
async def test_read_tool_error_names_stage_and_state(fake_client, monkeypatch):
    fake_client.is_connected.return_value = False
    fake_client.connect.side_effect = RuntimeError("terminal closed")

    result = await call("get_symbol_price", {"symbol_name": "EURUSD"})

    assert result.isError
    assert "[stage: read, state: not_started]" in result.content[0].text
    fake_client.market.get_symbol_price.assert_not_called()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def test_logs_go_to_a_bounded_file_and_never_block_on_an_unread_stderr(tmp_path):
    """Long run with stderr and stdout piped and never read: the process must finish and logs stay bounded."""
    log_file = tmp_path / "server.log"
    script = textwrap.dedent(f"""
        import logging, sys
        sys.path.insert(0, {str(SRC)!r})
        import mcp.server.fastmcp  # installs its own handler on import
        from metatrader_mcp.logging_config import configure_logging
        configure_logging({{"MCP_LOG_FILE": {str(log_file)!r}, "MCP_LOG_MAX_BYTES": "20000", "MCP_LOG_BACKUPS": "2"}})
        log = logging.getLogger("stress")
        for i in range(20000):
            log.warning("record %05d %s", i, "x" * 80)
        print("done", flush=True)
    """)
    process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert process.wait(timeout=120) == 0  # a blocked stderr pipe would hang here
    finally:
        if process.poll() is None:
            process.kill()
    files = list(tmp_path.glob("server.log*"))
    assert 1 <= len(files) <= 3
    assert sum(f.stat().st_size for f in files) <= 3 * 20000 + 2000
    assert process.stdout.read().strip() == b"done"


def test_configure_logging_removes_terminal_handlers(tmp_path):
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        root.addHandler(logging.StreamHandler(sys.stdout))
        configure_logging({"MCP_LOG_FILE": str(tmp_path / "a.log")})
        assert all(isinstance(h, SafeRotatingFileHandler) for h in root.handlers)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in saved:
            root.addHandler(handler)


def test_a_failing_log_handler_is_counted_and_does_not_raise(tmp_path):
    handler = SafeRotatingFileHandler(tmp_path / "x.log")
    before = logging_state.failures
    handler.emit = lambda record: handler.handleError(record)
    try:
        raise OSError("disk full")
    except OSError:
        handler.handle(logging.LogRecord("t", logging.ERROR, __file__, 1, "message", None, None))
    assert logging_state.failures == before + 1
    handler.close()


def test_secrets_are_masked_in_log_records():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "login with %s", ("hunter22secret",), None)
    RedactSecrets(["hunter22secret"]).filter(record)
    assert record.getMessage() == "login with ***"
