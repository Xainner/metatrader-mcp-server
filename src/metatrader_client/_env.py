"""Helpers to read typed settings from environment mappings."""

from typing import Mapping, Optional

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"Invalid boolean value {raw!r} for {name}")


def env_float(env: Mapping[str, str], name: str, default: Optional[float]) -> Optional[float]:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    return float(raw)
