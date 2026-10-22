"""
MetaTrader 5 order operations module.

This module handles trade execution, modification, and management.
"""

from pandas import DataFrame
from typing import Optional, Union

from .order import get_all_positions, get_positions_by_symbol, get_positions_by_currency, get_positions_by_id
from .order import get_all_pending_orders, get_pending_orders_by_symbol, get_pending_orders_by_currency, get_pending_orders_by_id
from .order import place_market_order, place_pending_order, modify_position, modify_pending_order
from .order import close_position, close_all_positions, close_all_positions_by_symbol, close_all_profitable_positions, close_all_losing_positions
from .order import cancel_pending_order, cancel_all_pending_orders, cancel_pending_orders_by_symbol
from .order import calculate_trade_outcome, calculate_required_margin, preview_order, get_trading_snapshot
from .order import get_request_status, list_requests, reconcile_request, resolve_request
from .order import get_risk_limits, disable_new_entries, enable_new_entries


class MT5Order:
    """
    Handles MetaTrader 5 order operations.
    Provides methods to execute, modify, and manage trading orders.
    """


    def __init__(self, connection):
        self._connection = connection
    

    def get_all_positions(self) -> DataFrame:
        return get_all_positions(self._connection)


    def get_positions_by_symbol(self, symbol: str) -> DataFrame:
        return get_positions_by_symbol(self._connection, symbol)


    def get_positions_by_currency(self, currency: str) -> DataFrame:
        return get_positions_by_currency(self._connection, currency)


    def get_positions_by_id(self, id: Union[int, str]) -> DataFrame:
        return get_positions_by_id(self._connection, id)


    def get_all_pending_orders(self) -> DataFrame:
        return get_all_pending_orders(self._connection)


    def get_pending_orders_by_symbol(self, symbol: str) -> DataFrame:
        return get_pending_orders_by_symbol(self._connection, symbol)


    def get_pending_orders_by_currency(self, currency: str) -> DataFrame:
        return get_pending_orders_by_currency(self._connection, currency)


    def get_pending_orders_by_id(self, id: Union[int, str]) -> DataFrame:
        return get_pending_orders_by_id(self._connection, id)


    def place_market_order(self, *, type: str, symbol: str, volume: Union[float, int], stop_loss: Optional[Union[float, int]] = None, take_profit: Optional[Union[float, int]] = None,
                           request_id: Optional[str] = None, strategy_id: Optional[str] = None, preview_id: Optional[str] = None):
        return place_market_order(self._connection, type=type, symbol=symbol, volume=volume, stop_loss=stop_loss, take_profit=take_profit,
                                  request_id=request_id, strategy_id=strategy_id, preview_id=preview_id)


    def place_pending_order(self, *, type: str, symbol: str, volume: Union[float, int], price: Union[float, int], stop_loss: Optional[Union[float, int]] = None, take_profit: Optional[Union[float, int]] = None,
                            request_id: Optional[str] = None, strategy_id: Optional[str] = None, preview_id: Optional[str] = None):
        return place_pending_order(self._connection, type=type, symbol=symbol, volume=volume, price=price, stop_loss=stop_loss, take_profit=take_profit,
                                   request_id=request_id, strategy_id=strategy_id, preview_id=preview_id)


    def modify_position(self, id: Union[str, int], *, stop_loss: Optional[Union[int, float]] = None, take_profit: Optional[Union[int, float]] = None, strategy_id: Optional[str] = None):
        return modify_position(self._connection, id, stop_loss=stop_loss, take_profit=take_profit, strategy_id=strategy_id)


    def modify_pending_order(self, *, id: Union[int, str], price: Optional[Union[int, float]] = None, stop_loss: Optional[Union[int, float]] = None, take_profit: Optional[Union[int, float]] = None, strategy_id: Optional[str] = None):
        return modify_pending_order(self._connection, id=id, price=price, stop_loss=stop_loss, take_profit=take_profit, strategy_id=strategy_id)


    def close_position(self, id: Union[str, int], *, request_id: Optional[str] = None, strategy_id: Optional[str] = None):
        return close_position(self._connection, id, request_id=request_id, strategy_id=strategy_id)


    def cancel_pending_order(self, id: Union[int, str], *, strategy_id: Optional[str] = None):
        return cancel_pending_order(self._connection, id, strategy_id=strategy_id)


    def close_all_positions(self, *, scope: Optional[str] = None, strategy_id: Optional[str] = None, dry_run: bool = False):
        return close_all_positions(self._connection, scope=scope, strategy_id=strategy_id, dry_run=dry_run)


    def close_all_positions_by_symbol(self, symbol: str, *, scope: Optional[str] = None, strategy_id: Optional[str] = None, dry_run: bool = False):
        return close_all_positions_by_symbol(self._connection, symbol, scope=scope, strategy_id=strategy_id, dry_run=dry_run)


    def close_all_profitable_positions(self, *, scope: Optional[str] = None, strategy_id: Optional[str] = None, dry_run: bool = False):
        return close_all_profitable_positions(self._connection, scope=scope, strategy_id=strategy_id, dry_run=dry_run)

    def close_all_losing_positions(self, *, scope: Optional[str] = None, strategy_id: Optional[str] = None, dry_run: bool = False):
        return close_all_losing_positions(self._connection, scope=scope, strategy_id=strategy_id, dry_run=dry_run)


    def cancel_all_pending_orders(self, *, scope: Optional[str] = None, strategy_id: Optional[str] = None, dry_run: bool = False):
        return cancel_all_pending_orders(self._connection, scope=scope, strategy_id=strategy_id, dry_run=dry_run)


    def cancel_pending_orders_by_symbol(self, symbol: str, *, scope: Optional[str] = None, strategy_id: Optional[str] = None, dry_run: bool = False):
        return cancel_pending_orders_by_symbol(self._connection, symbol, scope=scope, strategy_id=strategy_id, dry_run=dry_run)


    def get_trading_snapshot(self, symbol: str, *, strategy_id: Optional[str] = None):
        return get_trading_snapshot(self._connection, symbol=symbol, strategy_id=strategy_id)


    def calculate_trade_outcome(self, *, symbol: str, type: str, volume: float, entry_price: float, exit_price: float):
        return calculate_trade_outcome(self._connection, symbol=symbol, type=type, volume=volume, entry_price=entry_price, exit_price=exit_price)


    def calculate_required_margin(self, *, symbol: str, type: str, volume: float, price: Optional[float] = None):
        return calculate_required_margin(self._connection, symbol=symbol, type=type, volume=volume, price=price)


    def preview_order(self, *, symbol: str, type: str, volume: Optional[float] = None, risk_amount: Optional[float] = None, price: Optional[float] = None,
                      stop_loss: Optional[float] = None, take_profit: Optional[float] = None, strategy_id: Optional[str] = None):
        return preview_order(self._connection, symbol=symbol, type=type, volume=volume, risk_amount=risk_amount, price=price,
                             stop_loss=stop_loss, take_profit=take_profit, strategy_id=strategy_id)


    def get_request_status(self, request_id: str):
        return get_request_status(self._connection, request_id)


    def list_requests(self, *, state: Optional[str] = None, limit: int = 50):
        return list_requests(self._connection, state=state, limit=limit)


    def reconcile_request(self, request_id: str):
        return reconcile_request(self._connection, request_id)


    def resolve_request(self, request_id: str, *, decision: str, reason: str):
        return resolve_request(self._connection, request_id, decision=decision, reason=reason)


    def get_risk_limits(self):
        return get_risk_limits(self._connection)


    def disable_new_entries(self, *, reason: str, source: str = "client"):
        return disable_new_entries(self._connection, reason=reason, source=source)


    def enable_new_entries(self, *, reason: str, source: str = "client"):
        return enable_new_entries(self._connection, reason=reason, source=source)
