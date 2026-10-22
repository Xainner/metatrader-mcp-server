"""
Test isolation rules.

- Tests in ``tests/metatrader_client`` talk to a real MetaTrader 5 terminal. They are
  marked ``integration`` and only run with ``MT5_RUN_INTEGRATION=1``. Every connection
  they open is checked: if the account is not a demo account the connection is closed
  and the test session stops, because some of these tests open and close positions.
- All other tests must not reach the terminal: MT5 credentials are removed from the
  environment and the MetaTrader5 functions that connect or trade raise an error.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

INTEGRATION_DIR = Path(__file__).resolve().parent / "metatrader_client"
CREDENTIAL_VARS = ("LOGIN", "PASSWORD", "SERVER", "login", "password", "server", "MT5_PATH")
BLOCKED_MT5_FUNCTIONS = ("initialize", "login", "order_send", "order_check")
ACCOUNT_TRADE_MODE_DEMO = 0


def integration_enabled() -> bool:
    return os.getenv("MT5_RUN_INTEGRATION", "").strip().lower() in {"1", "true", "yes"}


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="integration test: set MT5_RUN_INTEGRATION=1 and use a dedicated demo account")
    for item in items:
        if INTEGRATION_DIR in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.integration)
            if not integration_enabled():
                item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def _require_demo_account():
    """Wrap MT5Connection.connect so integration tests can only run against a demo account."""
    if not integration_enabled():
        yield
        return

    import MetaTrader5 as mt5
    from metatrader_client.client_connection import MT5Connection

    original_connect = MT5Connection.connect

    def connect_demo_only(self):
        result = original_connect(self)
        account = mt5.account_info()
        if account is None or account.trade_mode != ACCOUNT_TRADE_MODE_DEMO:
            self.disconnect()
            pytest.exit("Integration tests require a demo account; the connected account is not a demo account.", returncode=2)
        return result

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(MT5Connection, "connect", connect_demo_only)
        # The integration suite repeats identical orders within seconds on purpose.
        mp.setenv("MT5_DUPLICATE_WINDOW_SECONDS", os.getenv("MT5_DUPLICATE_WINDOW_SECONDS") or "0")
        yield


@pytest.fixture(autouse=True)
def _isolate_from_terminal(request, monkeypatch):
    """Unit tests must never connect to a terminal or send orders."""
    if request.node.get_closest_marker("integration"):
        yield
        return

    for name in CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
    # Durable state must not be written to the user's home directory by tests.
    monkeypatch.setenv("MT5_STATE_DB", ":memory:")

    import MetaTrader5 as mt5

    def blocked(*args, **kwargs):
        raise RuntimeError("Unit tests must not call the real MetaTrader 5 terminal; use the fake_mt5 fixture")

    for name in BLOCKED_MT5_FUNCTIONS:
        monkeypatch.setattr(mt5, name, blocked)
    yield


@pytest.fixture
def openapi_client(monkeypatch):
    """MagicMock MT5 client served by the REST API instead of a real connection."""
    import metatrader_openapi.main as main_module

    client = MagicMock()
    monkeypatch.setattr(main_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(main_module, "init", lambda *args, **kwargs: client)
    return client
