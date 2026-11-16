"""The desktop's own log: a rotating, bounded file with secret-shaped text redacted. Pure Python: no Qt import.

The file is %LOCALAPPDATA%\\meridian-desktop\\logs\\desktop.log, or
~/.meridian-desktop/logs/desktop.log when LOCALAPPDATA is unset or blank. That is the same folder
family as settings.json (settings_store.py). The environment mapping and the home directory are
passed in, so tests use a temp folder and never the real one.

start_desktop_log attaches one RotatingFileHandler to the "meridian_desktop" logger and writes a
start line. The file rolls over at max_bytes (1 MiB) and keeps backup_count (3) older files, so
the folder stays under about 4 MiB. The file is opened on the first record, so nothing is created
until something is logged. Starting again replaces the handler, so there is never more than one.

Every formatted record passes redact_text, which is redact_line from log_tail plus a pass for a
value after password, passwd, secret, token or api_key (with ``=`` or ``:``, quoted or not). The
pass runs on the whole formatted record, so a traceback is redacted too.

A folder or file that cannot be created or opened never raises. start_desktop_log returns None
and the desktop runs without a file log.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path

from meridian_desktop.log_tail import (
    DEFAULT_MAX_BYTES as READ_MAX_BYTES,
    REDACTED,
    LogTail,
    read_log_tail,
    redact_line,
)

APP_DIR_NAME = "meridian-desktop"
FALLBACK_DIR_NAME = ".meridian-desktop"
LOG_DIR_NAME = "logs"
LOG_NAME = "desktop.log"
LOGGER_NAME = "meridian_desktop"
HANDLER_NAME = "meridian-desktop-file"
STARTED_MESSAGE = "Meridian Desktop started."
# The file rolls over at this size. The reader's budget is READ_MAX_BYTES, the bounded tail size.
ROTATE_MAX_BYTES = 1024 * 1024
DEFAULT_BACKUP_COUNT = 3
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

# A password, secret, token or API-key assignment. The key keeps its name and the value is replaced.
# A quoted value is taken whole, so no part of it survives. Only spaces may sit between the key and
# ``=`` or ``:``, so words such as "tokens" or "secret_store" do not match.
_VALUE = r"""(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&"'<>]+)"""
_ASSIGNMENT_RE = re.compile(
    r"((?:password|passwd|secret|token|api[_-]?key)[ \t]*[=:][ \t]*)" + _VALUE,
    re.IGNORECASE,
)


def redact_text(text: str) -> str:
    """``text`` with each secret-shaped value replaced by REDACTED. Other text is unchanged.

    The three shapes of log_tail.redact_line run first. Then an assignment pass covers the keys
    listed in the module docstring. The assignment pass never reads past the end of its line, so
    a whole multi-line string can be passed in.
    """
    return _ASSIGNMENT_RE.sub(r"\g<1>" + REDACTED, redact_line(text))


class RedactingFormatter(logging.Formatter):
    """A formatter that redacts the whole rendered record, including any traceback."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def desktop_log_path(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """Where the desktop writes its log. ``env`` and ``home`` can be passed in, so tests never read
    the real environment or home directory."""
    values = os.environ if env is None else env
    local_app_data = values.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        base = Path(local_app_data) / APP_DIR_NAME
    else:
        root = home if home is not None else Path.home()
        base = root / FALLBACK_DIR_NAME
    return base / LOG_DIR_NAME / LOG_NAME


def _valid_budget(value: object, *, minimum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def detach_desktop_log(logger: logging.Logger | None = None) -> None:
    """Remove and close the desktop's file handler, if one is attached. Safe to call when none is."""
    target = logging.getLogger(LOGGER_NAME) if logger is None else logger
    for handler in list(target.handlers):
        if handler.get_name() == HANDLER_NAME:
            target.removeHandler(handler)
            handler.close()


def start_desktop_log(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    *,
    max_bytes: int = ROTATE_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> Path | None:
    """Attach the rotating, redacting file handler and write the start line.

    Returns the log's path, or None when the folder or file cannot be set up. Raises ValueError
    for a bad ``max_bytes`` or ``backup_count``, because that is a bug in the caller.
    """
    max_bytes = _valid_budget(max_bytes, minimum=1, name="max_bytes")
    backup_count = _valid_budget(backup_count, minimum=0, name="backup_count")
    path = desktop_log_path(env, home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
    except OSError:
        return None
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, DATE_FORMAT))
    logger = logging.getLogger(LOGGER_NAME)
    detach_desktop_log(logger)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.info(STARTED_MESSAGE)
    return path


def read_desktop_log_tail(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    *,
    max_bytes: int = READ_MAX_BYTES,
) -> LogTail:
    """The redacted tail of the desktop log, at most ``max_bytes`` read. Read only, with the same
    bounded reader as the tunnel log."""
    return read_log_tail(desktop_log_path(env, home), max_bytes=max_bytes)


__all__ = [
    "DEFAULT_BACKUP_COUNT",
    "HANDLER_NAME",
    "LOGGER_NAME",
    "LOG_NAME",
    "READ_MAX_BYTES",
    "ROTATE_MAX_BYTES",
    "STARTED_MESSAGE",
    "RedactingFormatter",
    "desktop_log_path",
    "detach_desktop_log",
    "read_desktop_log_tail",
    "redact_text",
    "start_desktop_log",
]
