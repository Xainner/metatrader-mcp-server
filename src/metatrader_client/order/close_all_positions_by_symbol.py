import MetaTrader5 as mt5
from typing import Optional
from .close_position import close_position
from ._bulk import run_bulk

def close_all_positions_by_symbol(connection, symbol: str, *, scope: Optional[str] = None, strategy_id: Optional[str] = None, dry_run: bool = False):
    """
    Close all open positions for a given symbol.

    Every item is processed and reported individually, then the terminal is
    queried again to detect items that are still present.

    Args:
        connection: The connection object to the MetaTrader platform.
        symbol: The symbol of the positions to close.
        scope: 'strategy', 'manual' or 'all' (None: all items, as before).
        strategy_id: Strategy whose items are processed with scope 'strategy'.
        dry_run: Only list the affected items; nothing is sent.

    Returns:
        A dictionary with ``error`` (True if any item failed or remains),
        ``message`` and ``data`` with the per-item results.
    """
    items = mt5.positions_get(symbol=symbol)
    return run_bulk(
        items,
        lambda ticket: close_position(connection, ticket),
        lambda: mt5.positions_get(symbol=symbol),
        verb="Closed",
        noun=f"{symbol} positions",
        error=mt5.last_error,
        scope=scope,
        strategy_id=strategy_id,
        dry_run=dry_run,
    )
