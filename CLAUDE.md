# Claude Development Guide - MetaTrader MCP Server

## Project Overview

MCP server enabling AI assistants to trade on MetaTrader 5 via natural language. Provides both MCP (stdio) and HTTP/REST interfaces.

**Stack**: Python 3.10+ • FastMCP • FastAPI • MetaTrader5 SDK • Pandas • Pydantic • pytest
**Version**: 0.7.0 (Beta)
**Repo**: https://github.com/ariadng/metatrader-mcp-server

---

## Architecture (3 Packages)

### 1. `metatrader_client` - Core MT5 Library
```
metatrader_client/
├── account/      # Balance, equity, margin operations
├── connection/   # MT5 connection management with retry/cooldown
├── history/      # Historical deals and orders
├── market/       # Prices, candles, symbols
├── order/        # Trade execution and position management
├── types/        # Enums (OrderType, Timeframe, TradeAction, etc.)
├── risk.py       # Risk limits, reservations, new entries switch
├── store.py      # SQLite state (request registry, limits log, rules)
├── requests_registry.py  # request_id states and reconciliation data
├── strategy.py   # strategy_id -> magic, ownership and bulk scopes
├── supervisor.py # Local position rules and evaluation loop
└── client.py     # Main MT5Client class
```

**Pattern**: Functions receive `connection` as first param, use keyword-only args (`*,`)
```python
def place_market_order(connection, *, type: str, symbol: str, volume: Union[float, int]):
```

### 2. `metatrader_mcp` - MCP Server
- FastMCP with STDIO transport (default); SSE/streamable HTTP via `--transport`, bound to 127.0.0.1 unless `MCP_AUTH_TOKEN` and TLS are configured (`metatrader_mcp/security.py`)
- Tools: `async` functions decorated with `@mcp.tool()`; MT5 work runs through `metatrader_mcp/bridge.py` (one worker thread, deadlines, bounded reconnection): `await _read(ctx, "tool", lambda client: ...)` or `await _trade(...)` for sends
- Logs: `metatrader_mcp/logging_config.py` (rotating file; never stdout, stderr only on request)
- Client access inside the bridge callable: the `client` argument (from `get_client(ctx)`)
- Returns: CSV strings for DataFrames, dicts for objects

### 3. `metatrader_openapi` - REST API
- FastAPI with OpenAPI docs at `/docs`
- Routers: accounts, market, orders, positions, history
- Client: `request.app.state.client`
- CORS enabled, lifespan-managed connection

---

## Key Conventions

### Code Style
```python
# Function pattern
def operation(connection, *, param: type, optional: Optional[type] = None):
    """Brief description.

    Args:
        connection: MT5 connection object
        param: Description
        optional: Description (default: None)

    Returns:
        dict: {"error": bool, "message": str, "data": Any}
    """
```

### Return Formats
- **Client operations**: `{"error": False, "message": "...", "data": {...}}` (dict) or DataFrame
- **Trading operations** add `status` (done|partial|placed|no_changes|rejected|no_response) and `retcode`; `send_order` interprets `retcode`, never `last_error()` alone
- Openings add `request_id`, `request_state`, `verification`, `checks`, `execution`, `protection`; repeated `request_id` → `replayed: true`, nothing sent
- **MCP tools**: `df.to_csv()` for DataFrames, dict/list for objects
- **REST endpoints**: `df.to_dict(orient="records")` or dict (auto JSON)

### Import Order
1. Standard library (`os`, `typing`)
2. Third-party (`pandas`, `fastapi`)
3. Local (`.exceptions`, `.types`)

---

## Common Patterns

### Connection
```python
from metatrader_client import MT5Client

# Minimal configuration
config = {"login": int(login), "password": password, "server": server}

# Full configuration with all available options
config = {
    "login": int(login),         # Required: MT5 account login
    "password": password,         # Required: MT5 account password
    "server": server,             # Required: MT5 server name
    "path": None,                 # Optional: Path to terminal executable (auto-detect if None)
    "timeout": 60000,             # Optional: Connection timeout in ms (default: 60000)
    "portable": False,            # Optional: Use portable mode (default: False)
    "max_retries": 3,             # Optional: Max connection retries (default: 3)
    "backoff_factor": 1.5,        # Optional: Retry delay multiplier (default: 1.5)
    "cooldown_time": 2.0,         # Optional: Seconds between connections (default: 2.0)
    "debug": False                # Optional: Enable debug logging (default: False)
}

client = MT5Client(config)
client.connect()
# operations...
client.disconnect()
```

### Error Handling
```python
# Client level
if type.upper() not in ["BUY", "SELL"]:
    return {"error": True, "message": "Invalid type", "data": None}

# API level
try:
    return client.operation()
except MT5ConnectionError as e:
    raise HTTPException(status_code=503, detail=str(e))
```

### Testing Pattern
```python
@pytest.fixture(scope="module")
def mt5_client():
    client = MT5Client({"login": int(os.getenv("LOGIN")), ...})
    client.connect()
    yield client
    client.disconnect()
```

---

## Adding New Features

### New Client Operation
1. Create `metatrader_client/<module>/new_operation.py`
2. Add to `<module>/__init__.py` and `client_<module>.py`
3. Test in `tests/metatrader_client/test_<module>.py`

### New MCP Tool
1. Add to `metatrader_mcp/server.py`:
```python
@mcp.tool()
async def tool_name(ctx: Context, param: type) -> dict:
    """Description for AI."""
    return _to_plain(await _read(ctx, "tool_name", lambda client: client.module.operation(param)))
```
2. Tools that send orders use `_trade(...)` so a deadline is reported as `no_response`/`unknown`, never as a cancellation.

### New REST Endpoint
1. Add to `metatrader_openapi/routers/<module>.py`:
```python
@router.post("/path", response_model=Dict[str, Any])
async def endpoint(request: Request, param: type = Body(...)):
    """Description."""
    client = request.app.state.client
    return client.module.operation(param)
```

---

## Critical Rules

### ✅ DO
- Use type hints and docstrings for all public functions
- Return consistent formats: `{"error": bool, "message": str, "data": Any}`
- Use keyword-only args (`*,`) for clarity
- Handle exceptions with custom exception classes
- Test with demo accounts
- Validate inputs (symbols, volumes, prices)
- Update `__init__.py` when adding modules

### ❌ DON'T
- Hardcode credentials (use env vars)
- Mix business logic with routing
- Skip error handling on trading operations
- Modify connection object in functions
- Use `print()` (use logging)
- Test with live accounts without consent
- Add emojis unless requested

---

## MetaTrader 5 Specifics

**Order Types**: BUY, SELL, BUY_LIMIT, SELL_LIMIT, BUY_STOP, SELL_STOP, BUY_STOP_LIMIT, SELL_STOP_LIMIT
**Timeframes**: M1, M5, M15, M30, H1, H4, D1, W1, MN1
**Filling Modes**: Broker-dependent (FOK, IOC, Return)
**Platform**: Windows-centric (MT5 terminal required)
**Symbol Format**: Broker-dependent (e.g., "EURUSD", "EURUSD.m")

---

## Environment Variables

**Required**: `LOGIN`, `PASSWORD`, `SERVER`
**Optional**: `MT5_PATH` (path to MT5 terminal executable, auto-detected if not set), `OPENAPI_TITLE`, `OPENAPI_VERSION`
**MCP Transport**: `MCP_TRANSPORT` (stdio|sse|streamable-http, default: stdio), `MCP_HOST` (default: 127.0.0.1), `MCP_PORT` (default: 8080), `MCP_AUTH_TOKEN`, `MCP_SSL_CERTFILE`, `MCP_SSL_KEYFILE`, `MCP_ALLOW_INSECURE_REMOTE`, `MCP_ALLOWED_HOSTS`
**Risk limits** (`metatrader_client/risk.py`): `MT5_ALLOW_LIVE_TRADING` (default false: demo only), `MT5_MAX_VOLUME_PER_ORDER`, `MT5_MAX_TOTAL_VOLUME`, `MT5_MAX_OPEN_POSITIONS`, `MT5_MAX_RISK_PER_TRADE_PCT`, `MT5_REQUIRE_STOP_LOSS`, `MT5_MAX_DAILY_LOSS`, `MT5_DUPLICATE_WINDOW_SECONDS`, `MT5_ALLOW_NEW_ENTRIES`, `MT5_BLOCK_ON_UNRESOLVED`, `MT5_MAX_ENTRIES_PER_DAY`, `MT5_MAX_AGGREGATE_RISK_PCT`, `MT5_STRATEGY_LIMITS` (JSON), `MT5_STRATEGY_MAGICS` (JSON), `MT5_MISSING_PROTECTION_POLICY`
**State and data**: `MT5_STATE_DB`, `MT5_REQUEST_STALE_SECONDS`, `MT5_REQUEST_RETENTION_DAYS`, `MT5_PREVIEW_TTL_SECONDS`, `MT5_PREVIEW_MAX_PRICE_MOVE_POINTS`, `MT5_REQUIRE_PREVIEW`, `MT5_SERVER_UTC_OFFSET_SECONDS`, `MT5_MAX_CANDLES`, `MT5_SUPERVISOR_ENABLED`, `MT5_SUPERVISOR_INTERVAL_SECONDS`, `MT5_SUPERVISOR_MAX_STALE_SECONDS`
**MCP bridge and logs**: `MCP_READ_TIMEOUT_SECONDS`, `MCP_SEND_TIMEOUT_SECONDS`, `MCP_AUTO_RECONNECT`, `MCP_RECONNECT_MAX_ATTEMPTS`, `MCP_RECONNECT_WINDOW_SECONDS`, `MCP_LOG_FILE`, `MCP_LOG_LEVEL`, `MCP_LOG_MAX_BYTES`, `MCP_LOG_BACKUPS`, `MCP_LOG_STDERR_LEVEL`
**Debug**: Set `debug: True` in config dict

---

## Testing & Debugging

```bash
# Run tests (unit tests use a fake MT5 in tests/unit/fakes.py and never reach a terminal; Python 3.10-3.13)
pytest                                                  # Unit and route tests; integration tests are skipped
MT5_RUN_INTEGRATION=1 pytest tests/metatrader_client/   # Integration: dedicated demo account only

# Debug
client.is_connected()                   # Check connection
client.get_terminal_info()              # Terminal details
client.last_error()                     # Get last error
```

---

## Project Goals

1. **Accessibility** - Natural language trading for non-programmers
2. **Modularity** - Independent, reusable components
3. **Safety** - Prevent accidental losses through validation
4. **Reliability** - Robust error handling and connection management
5. **Extensibility** - Easy to add operations and integrations

---

## Resources

- **MCP Spec**: https://spec.modelcontextprotocol.io/
- **FastMCP**: https://github.com/jlowin/fastmcp
- **MT5 Python**: https://www.mql5.com/en/docs/python_metatrader5
- **FastAPI**: https://fastapi.tiangolo.com/

---

**Note**: This is financial software. Changes can result in real money loss. Test thoroughly with demo accounts.
