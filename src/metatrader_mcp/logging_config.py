"""
Logging for the MCP server.

With the STDIO transport, stdout carries JSON-RPC and stderr is a pipe that
the client may not read: a full pipe blocks every write and, with it, the
server. Logs therefore go to a rotating file by default and nothing is
written to stdout or stderr unless ``MCP_LOG_STDERR_LEVEL`` asks for it.
Importing FastMCP installs a Rich handler on the root logger; it is removed
here so the deployment's configuration is the only one in effect.

Settings:

=========================  ===========================================  ==========================
Variable                   Meaning                                      Default
=========================  ===========================================  ==========================
MCP_LOG_FILE               Log file path                                ~/.metatrader_mcp/logs/metatrader_mcp.log
MCP_LOG_LEVEL              Level of the file log                        INFO
MCP_LOG_MAX_BYTES          Size that triggers rotation                  5000000
MCP_LOG_BACKUPS            Rotated files kept                           3
MCP_LOG_STDERR_LEVEL       Also log to stderr at this level ("off")     off
=========================  ===========================================  ==========================

A failure to write a log record never propagates to the caller: it is
counted and the last error is kept (bounded) for ``get_bridge_health``.
Configured secrets (password, auth tokens) are masked in every record.
"""

import logging
import logging.handlers
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, Optional

DEFAULT_LOG_FILE = Path.home() / ".metatrader_mcp" / "logs" / "metatrader_mcp.log"


class LoggingState:
    """Degradation counters of the log handlers."""

    def __init__(self):
        self._lock = threading.Lock()
        self.failures = 0
        self.last_error: Optional[str] = None
        self.last_error_at: Optional[float] = None
        self.path: Optional[str] = None
        self.stderr_level: Optional[str] = None

    def failed(self, error: BaseException) -> None:
        with self._lock:
            self.failures += 1
            self.last_error = f"{type(error).__name__}: {error}"[:300]
            self.last_error_at = time.time()

    def snapshot(self) -> Dict:
        with self._lock:
            size = None
            if self.path:
                try:
                    size = os.path.getsize(self.path)
                except OSError:
                    size = None
            return {
                "file": self.path,
                "file_bytes": size,
                "stderr_level": self.stderr_level,
                "write_failures": self.failures,
                "last_error": self.last_error,
                "last_error_at": self.last_error_at,
            }


logging_state = LoggingState()


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating file handler that records its failures instead of writing them to stderr."""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 (logging API)
        logging_state.failed(sys.exc_info()[1] or RuntimeError("log write failed"))


class SafeStreamHandler(logging.StreamHandler):
    """Stream handler that never raises (a blocked or closed stderr must not stop trading)."""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        logging_state.failed(sys.exc_info()[1] or RuntimeError("log write failed"))


class RedactSecrets(logging.Filter):
    """Mask configured secret values in log messages."""

    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 4]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        masked = message
        for secret in self.secrets:
            masked = masked.replace(secret, "***")
        if masked != message:
            record.msg, record.args = masked, None
        return True


def _secrets(env) -> list:
    return [env.get(name, "") for name in ("PASSWORD", "password", "MCP_AUTH_TOKEN", "OPENAPI_AUTH_TOKEN")]


def configure_logging(env=None) -> Dict:
    """Install the file (and optional stderr) handlers on the root logger; returns the effective settings."""
    env = os.environ if env is None else env
    root = logging.getLogger()
    for handler in list(root.handlers):
        # Rich/stream handlers installed by imports write to the terminal streams.
        root.removeHandler(handler)
    logging.raiseExceptions = False

    level = getattr(logging, str(env.get("MCP_LOG_LEVEL", "INFO")).upper(), logging.INFO)
    root.setLevel(level)
    redact = RedactSecrets(_secrets(env))
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    path = Path(env.get("MCP_LOG_FILE") or DEFAULT_LOG_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = SafeRotatingFileHandler(
            path,
            maxBytes=int(env.get("MCP_LOG_MAX_BYTES") or 5_000_000),
            backupCount=int(env.get("MCP_LOG_BACKUPS") or 3),
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(formatter)
        handler.addFilter(redact)
        root.addHandler(handler)
        logging_state.path = str(path)
    except OSError as exc:
        logging_state.failed(exc)
        logging_state.path = None

    stderr_level = str(env.get("MCP_LOG_STDERR_LEVEL", "off")).upper()
    if stderr_level != "OFF":
        stream = SafeStreamHandler(sys.stderr)
        stream.setLevel(getattr(logging, stderr_level, logging.WARNING))
        stream.setFormatter(formatter)
        stream.addFilter(redact)
        root.addHandler(stream)
        logging_state.stderr_level = stderr_level
    else:
        logging_state.stderr_level = None
    return logging_state.snapshot()
