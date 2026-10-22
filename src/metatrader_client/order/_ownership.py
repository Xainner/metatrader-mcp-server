"""Ownership checks for operations on a single position or order."""

from typing import Optional

from ..strategy import strategy_magic, validate_strategy_id


def ownership_error(item, strategy_id: Optional[str], *, label: str) -> Optional[str]:
    """
    Error when ``strategy_id`` is given and ``item`` does not carry its magic number.

    Without ``strategy_id`` the operation is not restricted (manual use).
    """
    if strategy_id is None:
        return None
    error = validate_strategy_id(strategy_id)
    if error:
        return error
    magic = strategy_magic(strategy_id)
    item_magic = int(getattr(item, "magic", 0) or 0)
    if item_magic != magic:
        owner = "manual trading" if item_magic == 0 else f"magic {item_magic}"
        return f"{label} belongs to {owner}, not to strategy {strategy_id} (magic {magic}); it was not changed"
    return None
