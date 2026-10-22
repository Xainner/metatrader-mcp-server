"""
Strategy identity and ownership of positions and orders.

A ``strategy_id`` is mapped to the ``magic`` number placed on its orders.
The mapping comes from ``MT5_STRATEGY_MAGICS`` (JSON object, for example
``{"breakout": 7001}``) or, for IDs not listed there, from a stable hash of the
ID in the range 100000000-999999999.

Ownership of a position or order, relative to a strategy:

- ``own``: its magic equals the strategy magic.
- ``manual``: magic 0 (orders placed from the terminal and, on many setups,
  other tools that do not set a magic number).
- ``other``: any other magic (other strategies or robots).

A magic number identifies orders; it is not an authorization boundary: any
program connected to the account can use any magic number.
"""

import json
import os
import zlib
from typing import Any, Dict, Iterable, List, Optional

OWNERSHIP_OWN = "own"
OWNERSHIP_MANUAL = "manual"
OWNERSHIP_OTHER = "other"

SCOPES = ("strategy", "manual", "all")


def configured_magics(env=None) -> Dict[str, int]:
    env = os.environ if env is None else env
    raw = env.get("MT5_STRATEGY_MAGICS")
    if not raw:
        return {}
    mapping = json.loads(raw)
    if not isinstance(mapping, dict):
        raise ValueError("MT5_STRATEGY_MAGICS must be a JSON object")
    return {str(k): int(v) for k, v in mapping.items()}


def validate_strategy_id(strategy_id: Any) -> Optional[str]:
    if strategy_id is None:
        return None
    if not isinstance(strategy_id, str) or not strategy_id.strip():
        return "strategy_id must be a non-empty string"
    if len(strategy_id) > 64:
        return "strategy_id must be at most 64 characters"
    return None


def strategy_magic(strategy_id: Optional[str], env=None) -> int:
    """Magic number of a strategy; 0 when no strategy is given."""
    if not strategy_id:
        return 0
    mapping = configured_magics(env)
    if strategy_id in mapping:
        return mapping[strategy_id]
    return 100_000_000 + zlib.crc32(strategy_id.encode()) % 900_000_000


def resolve_magic(strategy_id: Optional[str] = None, magic: Optional[int] = None, env=None) -> Optional[int]:
    """Magic to use for a request: explicit ``magic``, else the strategy magic; None if both disagree."""
    derived = strategy_magic(strategy_id, env) if strategy_id else None
    if magic is not None and derived is not None and int(magic) != derived:
        return None
    if magic is not None:
        return int(magic)
    return derived or 0


def ownership(item, magic: Optional[int]) -> str:
    """Classify a position or order relative to a strategy magic."""
    item_magic = int(getattr(item, "magic", 0) or 0)
    if magic and item_magic == int(magic):
        return OWNERSHIP_OWN
    if item_magic == 0:
        return OWNERSHIP_MANUAL
    return OWNERSHIP_OTHER


def filter_scope(items: Optional[Iterable], *, scope: Optional[str], magic: Optional[int] = None) -> Optional[List]:
    """
    Select the items in a scope: ``strategy`` (magic equals ``magic``),
    ``manual`` (magic 0) or ``all``. None in, None out (unreadable listing).
    """
    if items is None:
        return None
    items = list(items)
    if scope in (None, "all"):
        return items
    if scope == "strategy":
        if not magic:
            raise ValueError("scope 'strategy' needs a strategy_id or magic")
        return [i for i in items if int(getattr(i, "magic", 0) or 0) == int(magic)]
    if scope == "manual":
        return [i for i in items if int(getattr(i, "magic", 0) or 0) == 0]
    raise ValueError(f"Invalid scope {scope!r}, must be one of {', '.join(SCOPES)}")
