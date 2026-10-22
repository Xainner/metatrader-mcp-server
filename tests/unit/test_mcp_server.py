"""MCP server startup, tool schemas and output serialization, through an in-memory MCP session."""

import json
from collections import namedtuple
from unittest.mock import ANY, MagicMock

import pandas as pd
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import metatrader_mcp.server as server

pytestmark = pytest.mark.anyio

Result = namedtuple("Result", "retcode order price request")
Request = namedtuple("Request", "symbol sl tp")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(server, "init", lambda *args, **kwargs: client)
    return client


async def call(tool, arguments=None):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
        return await session.call_tool(tool, arguments or {})


async def test_server_starts_without_credentials_and_lists_tools():
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}

    assert len(tools) == 45
    assert "get_candles_by_date" in tools
    assert {"get_bridge_health", "get_trading_snapshot", "preview_order", "get_request_status", "get_candles"} <= set(tools)
    market = tools["place_market_order"].inputSchema["properties"]
    assert {"symbol", "volume", "type", "stop_loss", "take_profit"} <= set(market)
    assert set(tools["place_market_order"].inputSchema["required"]) == {"symbol", "volume", "type"}
    assert {"request_id", "strategy_id", "preview_id"} <= set(market)
    # Bulk tools require an explicit scope (IMP-006).
    assert "scope" in tools["close_all_positions"].inputSchema["required"]


async def test_tool_without_credentials_reports_configuration_error():
    result = await call("get_all_positions")

    assert result.isError
    assert "not configured" in result.content[0].text


async def test_trading_tool_returns_json_serializable_result(fake_client):
    fake_client.order.place_market_order.return_value = {
        "error": False,
        "message": "Filled",
        "status": "done",
        "retcode": 10009,
        "data": Result(10009, 42, 1.1, Request("EURUSD", 1.09, 1.12)),
    }

    result = await call("place_market_order", {"symbol": "EURUSD", "volume": 0.1, "type": "BUY", "stop_loss": 1.09})

    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert payload["data"] == {"retcode": 10009, "order": 42, "price": 1.1, "request": {"symbol": "EURUSD", "sl": 1.09, "tp": 1.12}}
    fake_client.order.place_market_order.assert_called_once_with(
        symbol="EURUSD", volume=0.1, type="BUY", stop_loss=1.09, take_profit=None,
        request_id=ANY, strategy_id=None, preview_id=None,
    )
    fake_client.disconnect.assert_called_once()


async def test_csv_tools_return_text(fake_client):
    fake_client.order.get_all_positions.return_value = pd.DataFrame([{"id": 1, "symbol": "EURUSD"}])

    result = await call("get_all_positions")

    assert not result.isError
    assert "EURUSD" in result.content[0].text
