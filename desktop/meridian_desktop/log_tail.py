"""Bounded, redacted tail of the tunnel log, for the Logs tab. Pure Python: no Qt import.

The core writes tunnel diagnostics to one log file. Its path is the file named by the
MERIDIAN_TUNNEL_LOG environment variable (a file path), or ~/.meridian/tunnel.log when that
variable is unset or blank. This matches _tunnel_log_path in meridian/tunnel_client.py. When
the log is over 5 MB at tunnel start, the core moves it to tunnel.log.1 and starts a new log.

read_log_tail reads the end of any log file (the desktop's and the tunnel's both use it). When
that file is shorter than the budget, it also reads the end of its ``.1`` backup to fill the
rest. The budget is max_bytes (default 65536) in total, counted in bytes read from disk. The
older backup lines come first. A window that starts in the middle of a line drops that partial
first line, so no fragment is shown. Every returned line then passes redact_line.

read_tunnel_log_tail is the tunnel's entry point: it resolves the tunnel log's path and then
calls read_log_tail.

The module never creates, writes, truncates, renames or deletes a file. A missing log is not an
error: the result has status STATUS_NO_LOG_YET and no lines. A log that exists but cannot be
read gives STATUS_UNREADABLE, with fixed text and no exception. The Logs tab shows its own text
for each source, so the fixed messages here are generic.

Redaction is defense in depth. The core stops the secret reaching the file in the first place
(core-tunnel-log-tee-redaction). The pass here covers three shapes: a value after token= (in a
query string or anywhere else), the value after a Bearer header word, and a Meridian-kind sk_ token.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

TUNNEL_LOG_ENV = "MERIDIAN_TUNNEL_LOG"
LOG_NAME = "tunnel.log"
BACKUP_SUFFIX = ".1"
DEFAULT_MAX_BYTES = 65536

REDACTED = "[redacted]"

# (name, pattern, replacement). The names are pinned by the test, so a change here is deliberate.
REDACTION_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("token-param", re.compile(r"(token=)[^\s&#\"'<>]+", re.IGNORECASE), r"\g<1>" + REDACTED),
    ("bearer", re.compile(r"(\bBearer\s+)\S+", re.IGNORECASE), r"\g<1>" + REDACTED),
    ("meridian-token", re.compile(r"sk_(?:meridian)_[A-Za-z0-9_-]*", re.IGNORECASE), REDACTED),
)
PATTERN_NAMES = tuple(name for name, _pattern, _replacement in REDACTION_PATTERNS)

STATUS_OK = "ok"
STATUS_NO_LOG_YET = "no_log_yet"
STATUS_UNREADABLE = "unreadable"

NO_LOG_YET_TEXT = "No log yet. The log file does not exist."
UNREADABLE_TEXT = "The log file exists but could not be read."


@dataclass(frozen=True)
class LogTail:
    """One read of the tunnel log. ``lines`` are redacted, and hold at most max_bytes of file text.

    ``message`` is fixed text for a status other than STATUS_OK, and is empty for STATUS_OK.
    """

    status: str
    path: Path
    lines: tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class _Window:
    """The bytes read from one file. ``starts_mid_file`` is True when the window begins inside a line."""

    data: bytes
    starts_mid_file: bool


def redact_line(line: str) -> str:
    """Return ``line`` with each secret-shaped value replaced by REDACTED. Other text is unchanged."""
    for _name, pattern, replacement in REDACTION_PATTERNS:
        line = pattern.sub(replacement, line)
    return line


def tunnel_log_path(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """The path the core writes its tunnel log to.

    MERIDIAN_TUNNEL_LOG is used when it is set and not blank. Otherwise the path is
    ~/.meridian/tunnel.log. ``env`` and ``home`` can be passed in, so tests never read the real
    environment or home directory.
    """
    values = os.environ if env is None else env
    override = values.get(TUNNEL_LOG_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    base = home if home is not None else Path.home()
    return base / ".meridian" / LOG_NAME


def _open_for_read(path: Path) -> BinaryIO:
    """Open ``path`` read-only, in binary mode. This is the only open call in the module."""
    return open(path, "rb")


def _window(path: Path, limit: int) -> _Window | None:
    """The last ``limit`` bytes of ``path``, or None when the file does not exist.

    At most ``limit`` bytes are read. A file smaller than ``limit`` gives the whole file.
    FileNotFoundError is a missing file. Any other OSError is raised to the caller.
    """
    try:
        fh = _open_for_read(path)
    except FileNotFoundError:
        return None
    with fh:
        size = os.fstat(fh.fileno()).st_size
        start = max(size - limit, 0)
        fh.seek(start)
        data = fh.read(limit)
    return _Window(data, start > 0)


def _complete_lines(window: _Window) -> list[str]:
    """The lines in a window, with line endings removed.

    A window that starts mid-file begins with a partial line. That line is dropped, through the
    first newline. When the window holds no newline at all, it is one partial line and nothing
    from it is returned.
    """
    data = window.data
    if window.starts_mid_file:
        cut = data.find(b"\n")
        if cut == -1:
            return []
        data = data[cut + 1 :]
    lines = data.decode("utf-8", errors="replace").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def read_log_tail(path: Path | str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> LogTail:
    """Return the redacted tail of the log at ``path``, reading at most ``max_bytes`` from disk.

    The backup, ``<path>.1``, is read only when the current log is shorter than the budget.
    Raise ValueError when ``max_bytes`` is not a positive integer.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    current = Path(path)
    backup = current.with_name(current.name + BACKUP_SUFFIX)
    try:
        newest = _window(current, max_bytes)
        budget = max_bytes - (len(newest.data) if newest is not None else 0)
        older = _window(backup, budget) if budget > 0 else None
    except OSError:
        return LogTail(STATUS_UNREADABLE, current, (), UNREADABLE_TEXT)
    if newest is None and older is None:
        return LogTail(STATUS_NO_LOG_YET, current, (), NO_LOG_YET_TEXT)
    lines: list[str] = []
    for window in (older, newest):
        if window is not None:
            lines.extend(_complete_lines(window))
    return LogTail(STATUS_OK, current, tuple(redact_line(line) for line in lines))


def read_tunnel_log_tail(
    path: Path | str | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> LogTail:
    """Return the redacted tail of the tunnel log, reading at most ``max_bytes`` from disk.

    ``path`` names the current log. When it is None, tunnel_log_path(env, home) is used. Rules
    are those of read_log_tail, including the ``.1`` backup and the ValueError for a bad budget.
    """
    current = Path(path) if path is not None else tunnel_log_path(env, home)
    return read_log_tail(current, max_bytes=max_bytes)


__all__ = [
    "BACKUP_SUFFIX",
    "DEFAULT_MAX_BYTES",
    "LOG_NAME",
    "NO_LOG_YET_TEXT",
    "PATTERN_NAMES",
    "REDACTED",
    "REDACTION_PATTERNS",
    "STATUS_NO_LOG_YET",
    "STATUS_OK",
    "STATUS_UNREADABLE",
    "TUNNEL_LOG_ENV",
    "UNREADABLE_TEXT",
    "LogTail",
    "read_log_tail",
    "read_tunnel_log_tail",
    "redact_line",
    "tunnel_log_path",
]
