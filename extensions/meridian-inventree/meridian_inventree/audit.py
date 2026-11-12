"""Append-only audit log for InvenTree plan events, one JSON object per line.

append() opens the file with O_APPEND, writes one complete line, and fsyncs.
Nothing here truncates, rewrites or deletes the file. Two guards run on every
event before it is written:

- Keys that name a credential (token, secret, password, authorization, ...)
  have their values replaced with "[redacted]". Strings that look like a
  bearer token or an sk_meridian_ key are scrubbed the same way.
- The ts field is always set by this module, so a caller cannot forge it.

Writes from threads in one process are serialized by a lock. Across processes
the file relies on O_APPEND. On Windows that does not guarantee ordering for
concurrent writers, so keep one writer per audit file where possible.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

REDACTED: Final[str] = "[redacted]"
# Event names for one operation. Only OP_DONE marks a client_key as succeeded.
# OP_UNKNOWN marks a write whose request may have reached InvenTree with no usable answer. It is
# resolved only by a later OP_DONE, or by an OP_FAILED that the owner recorded by hand ("manual": true).
OP_STARTED: Final[str] = "op_started"
OP_DONE: Final[str] = "op_done"
OP_FAILED: Final[str] = "op_failed"
OP_SKIPPED: Final[str] = "op_skipped"
OP_UNKNOWN: Final[str] = "op_unknown"
_SECRET_KEY_MARKERS: Final[tuple[str, ...]] = (
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "cookie",
)
_SECRET_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"""(?i)\b(?:token|bearer)\s+[^\s"',;]+"""),
    re.compile(r"sk_meridian_[A-Za-z0-9_\-]+"),
)


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_KEY_MARKERS)


def scrub(value: Any, key: str | None = None) -> Any:
    """Return a copy of value with credential-looking data replaced by REDACTED."""
    if key is not None and _is_secret_key(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(name): scrub(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        cleaned = value
        for pattern in _SECRET_VALUE_PATTERNS:
            cleaned = pattern.sub(REDACTED, cleaned)
        return cleaned
    return value


class AuditLog:
    """A JSON-lines file that only ever grows. append() is the only write path."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        """Write one event as one line and return the record that was written."""
        if not isinstance(event, dict):
            raise TypeError("audit event must be a dict")
        record: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat()}
        for name, value in scrub(event).items():
            if name != "ts":
                record[name] = value
        # ensure_ascii keeps the line free of characters that some readers treat as line breaks.
        line = json.dumps(record, ensure_ascii=True, allow_nan=False, default=str)
        data = (line + "\n").encode("ascii")
        with self._lock:
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                offset = 0
                while offset < len(data):
                    offset += os.write(fd, data[offset:])
                os.fsync(fd)
            finally:
                os.close(fd)
        return record

    def read_events(self) -> list[dict[str, Any]]:
        """Parse every line. Used by tests and by callers that review the log."""
        if not self._path.exists():
            return []
        with self._path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def succeeded_keys(self) -> set[str]:
        """Return every client_key that has an op_done event.

        Raises if the log cannot be read or a line is not JSON. Callers must treat that as
        a refusal, so an unreadable log can never let a write run a second time.
        """
        keys: set[str] = set()
        for event in self.read_events():
            if isinstance(event, dict) and event.get("event") == OP_DONE:
                key = event.get("client_key")
                if isinstance(key, str):
                    keys.add(key)
        return keys

    def unresolved_keys(self) -> set[str]:
        """Return every client_key whose latest settling event is OP_UNKNOWN.

        Events are read in file order, so "later" means later in the log. An OP_UNKNOWN is
        resolved by a later OP_DONE, or by a later OP_FAILED that carries "manual": true, which
        is how the owner records a write that never reached InvenTree. An automatic OP_FAILED
        does not resolve it, because the earlier request may still have been applied.

        Raises if the log cannot be read or a line is not JSON, the same as succeeded_keys.
        """
        pending: dict[str, bool] = {}
        unknown: set[str] = set()
        for event in self.read_events():
            if not isinstance(event, dict):
                continue
            key = event.get("client_key")
            if not isinstance(key, str):
                continue
            name = event.get("event")
            if name == OP_STARTED:
                # A start with no settling event means the final write could not be recorded, so the
                # request may have gone out. Until a later event settles it, the key stays unresolved.
                pending[key] = True
            elif name == OP_UNKNOWN:
                pending[key] = True
                unknown.add(key)
            elif name == OP_DONE:
                pending[key] = False
                unknown.discard(key)
            elif name == OP_FAILED:
                if event.get("manual") is True:
                    pending[key] = False
                    unknown.discard(key)
                elif key not in unknown:
                    # An automatic failure after a definite start clears it. After an unknown
                    # outcome it cannot, because the earlier request may still have been applied.
                    pending[key] = False
        return {key for key, is_pending in pending.items() if is_pending}
