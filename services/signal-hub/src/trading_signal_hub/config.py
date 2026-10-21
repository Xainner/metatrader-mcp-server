"""Runtime configuration outside the source checkout."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    home: Path
    source_id: str
    enabled: bool
    allowed_chat_ids: frozenset[int]
    poll_timeout_seconds: int
    token: str = field(default="", repr=False)

    @property
    def database(self) -> Path:
        return self.home / "data" / "signals.sqlite3"

    def blockers(self) -> list[str]:
        result = []
        if not self.enabled:
            result.append("SOURCE_DISABLED")
        if not self.allowed_chat_ids:
            result.append("CHAT_ALLOWLIST_EMPTY")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", self.token):
            result.append("BOT_TOKEN_MISSING_OR_INVALID")
        return result


def load_config(home: Path | None = None) -> Config:
    home = (home or Path(os.environ.get("TRADING_SIGNAL_HOME", "~/trading/signal-hub"))).expanduser().resolve()
    try:
        raw = json.loads((home / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ConfigurationError("Cannot read valid config.json in the signal home") from None
    keys = {"source_id", "enabled", "allowed_chat_ids", "poll_timeout_seconds"}
    if not isinstance(raw, dict) or set(raw) - keys:
        raise ConfigurationError("Unknown configuration fields")
    source = raw.get("source_id")
    ids = raw.get("allowed_chat_ids")
    timeout = raw.get("poll_timeout_seconds", 25)
    if not isinstance(source, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", source):
        raise ConfigurationError("Invalid source_id")
    if not isinstance(raw.get("enabled"), bool):
        raise ConfigurationError("enabled must be a boolean")
    if not isinstance(ids, list) or any(type(item) is not int or item == 0 for item in ids):
        raise ConfigurationError("allowed_chat_ids must contain nonzero integer chat IDs")
    if type(timeout) is not int or not 1 <= timeout <= 50:
        raise ConfigurationError("poll_timeout_seconds must be 1..50")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token and (home / ".env").exists():
        # Parse this one literal key. Never source or execute a shell file.
        for line in (home / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip()
    return Config(home, source, raw["enabled"], frozenset(ids), timeout, token)
