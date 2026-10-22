"""
Fixtures for unit tests: ``fake_mt5`` replaces the ``mt5`` module used by the
order functions, so the trading logic runs end to end without a terminal.
"""

import importlib
from types import SimpleNamespace

import pytest

from metatrader_client.risk import RiskConfig, RiskGuard
from .fakes import FakeMT5, PATCHED_MODULES


@pytest.fixture
def fake_mt5(monkeypatch):
    fake = FakeMT5()
    for name in PATCHED_MODULES:
        monkeypatch.setattr(importlib.import_module(name), "mt5", fake)
    return fake


@pytest.fixture
def connection():
    """Connection stand-in carrying a risk guard with live trading blocked and no other limits."""
    return SimpleNamespace(risk_guard=RiskGuard(RiskConfig(duplicate_window_seconds=0)))


@pytest.fixture(autouse=True)
def _fresh_process_state():
    """Process-wide fallbacks (store, previews, candle cache, metrics) start empty in every test."""
    from metatrader_client.store import reset_default_store
    from metatrader_client.order._preview_store import reset_default_preview_store
    from metatrader_client.market.get_candles import candle_cache
    from metatrader_client.metrics import metrics

    reset_default_store()
    reset_default_preview_store()
    candle_cache.clear()
    metrics.reset()
    yield
