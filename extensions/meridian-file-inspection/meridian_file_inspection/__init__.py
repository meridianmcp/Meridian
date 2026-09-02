"""meridian-file-inspection -- standalone local MCP server for a
tunnel-independent, bounded single-file XML/JSON inspector (item 2ffd763d).

See :mod:`meridian_file_inspection.inspector` for the implementation and
``README.md`` for the tool contract and security notes.
"""
from __future__ import annotations

from .inspector import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_ITEMS,
    DEFAULT_PREVIEW_CHARS,
    DEFAULT_TIMEOUT_SECONDS,
    ERROR_CODES,
    SCHEMA_VERSION,
    inspect_file,
    is_secret_path,
)

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_PREVIEW_CHARS",
    "DEFAULT_TIMEOUT_SECONDS",
    "ERROR_CODES",
    "SCHEMA_VERSION",
    "inspect_file",
    "is_secret_path",
]

PACKAGE_VERSION = "0.1.0"
