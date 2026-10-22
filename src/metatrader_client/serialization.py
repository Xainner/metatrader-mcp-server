"""Conversion of MetaTrader 5 results to JSON-friendly values."""

from datetime import date, datetime
from typing import Any


def to_plain(value: Any) -> Any:
    """Convert named tuples, namespaces, mappings and sequences to plain dicts and lists."""
    if hasattr(value, "_asdict"):
        return {k: to_plain(v) for k, v in value._asdict().items()}
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "__dict__") and type(value).__name__ == "SimpleNamespace":
        return {k: to_plain(v) for k, v in vars(value).items()}
    if hasattr(value, "item") and callable(value.item):  # numpy scalars
        try:
            return value.item()
        except (TypeError, ValueError):
            return value
    return value
