"""
MetaTrader 5 connection module.

This module provides functionality to connect to a MetaTrader 5 terminal.
"""
import os
import time
import datetime
import logging
import random
from typing import Dict, List, Tuple, Union, Optional

try:
    import MetaTrader5 as mt5
except ImportError:
    raise ImportError("MetaTrader5 package is not installed. Please install it with: pip install MetaTrader5")

from metatrader_client.exceptions import ConnectionError, InitializationError, LoginError, DisconnectionError
from .risk import RiskConfig, RiskGuard
from .connection import (
    _find_terminal_path,
    _ensure_cooldown,
    _initialize_terminal,
    _login,
    _get_last_error,
    connect,
    disconnect,
    is_connected,
    get_terminal_info,
    get_version,
)

# Set up logger
logger = logging.getLogger("MT5Connection")


class MT5Connection:
    """
    MetaTrader 5 connection class.
    
    This class provides functionality to connect to a MetaTrader 5 terminal.
    """
    
    def __init__(self, config: Dict):
        """
        Initialize the MetaTrader 5 connection.
        
        Args:
            config: A dictionary containing the connection configuration.
                - path (str): Path to the MetaTrader 5 terminal executable (default: None).
                - login (int): Login ID (default: None).
                - password (str): Password (default: None).
                - server (str): Server name (default: None).
                - timeout (int): Timeout in milliseconds (default: 60000).
                - portable (bool): Whether to use portable mode (default: False).
                - debug (bool): Whether to enable debug logging (default: False).
                - max_retries (int): Maximum number of connection retries (default: 3).
                - backoff_factor (float): Backoff factor for retry delays (default: 1.5).
                - cooldown_time (float): Cooldown time between connections in seconds (default: 2.0).
                - risk (dict): Risk limits, see ``metatrader_client.risk`` (default: environment variables).
        """
        self.config = config
        self.path = config.get("path")
        self.login = config.get("login")
        self.password = config.get("password")
        self.server = config.get("server")
        self.timeout = config.get("timeout", 60000)
        self.portable = config.get("portable", False)
        self.debug = config.get("debug", False)
        self.max_retries = config.get("max_retries", 3)
        self.backoff_factor = config.get("backoff_factor", 1.5)
        self.cooldown_time = config.get("cooldown_time", 2.0)
        self.risk_guard = RiskGuard(RiskConfig.from_sources(config.get("risk")))
        self._init_state(config)
        self._connected = False
        self._last_connection_time = 0
        
        # Set up logging level
        if self.debug:
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)
        
        # Standard paths to look for MetaTrader 5 terminal
        self.standard_paths = [
            "C:\\Program Files\\MetaTrader 5\\terminal64.exe",
            "C:\\Program Files (x86)\\MetaTrader 5\\terminal.exe",
            os.path.expanduser("~\\AppData\\Roaming\\MetaQuotes\\Terminal\\*\\terminal64.exe"),
        ]
    
    def _init_state(self, config: Dict) -> None:
        """
        Durable state (request registry, limits log, supervisor rules) and previews.

        The database path comes from ``state_db`` in the config, else
        ``MT5_STATE_DB``, else ``~/.metatrader_mcp/state.sqlite3``. The
        effective risk limits are logged when they changed since the last start,
        and old final requests are purged (``MT5_REQUEST_RETENTION_DAYS``).
        """
        from .store import Store, default_path
        from .requests_registry import RequestRegistry
        from .order._preview_store import PreviewStore

        self.store = Store(default_path(config.get("state_db")))
        self.request_registry = RequestRegistry(self.store)
        self.preview_store = PreviewStore()
        self.store.record_limits(self.risk_guard.config.as_dict(), source="startup")
        purged = self.request_registry.purge()
        if purged:
            logger.info("Purged %s final trade requests past the retention period", purged)

    def _find_terminal_path(self) -> str:
        return _find_terminal_path(self)

    def _ensure_cooldown(self):
        return _ensure_cooldown(self)

    def _initialize_terminal(self) -> bool:
        return _initialize_terminal(self)

    def _login(self) -> bool:
        return _login(self)

    def _get_last_error(self) -> Tuple[int, str]:
        return _get_last_error(self)

    def connect(self) -> bool:
        return connect(self)

    def disconnect(self) -> bool:
        return disconnect(self)

    def is_connected(self) -> bool:
        return is_connected(self)

    def get_terminal_info(self) -> Dict:
        return get_terminal_info(self)

    def get_version(self) -> Tuple[int, int, int, int]:
        return get_version(self)
