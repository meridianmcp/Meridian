"""Read-only reader for the Meridian project list. Pure Python: stdlib sqlite3 only, no Qt import.

The desktop shows projects from the Meridian database file, and this module only reads it.
Each call opens the file with the SQLite URI flag ``mode=ro``, runs one SELECT, and closes the
connection. The reader never calls the core ``init_db``, so it never creates tables, migrates a
schema, or creates a missing file.

A missing file raises NoDatabase before any connection is opened, and the error names the path.
A WAL-mode database is read through its -wal and -shm files, so it lists the rows that a running
Meridian has committed. An INSERT or other write on the reader connection is refused by SQLite
(sqlite3.OperationalError). The reader writes no rows. When the -wal and -shm files are absent,
SQLite creates them beside the database while the reader is open. The database file itself is
never created.

Only the columns in PROJECT_COLUMNS are selected. The core table also holds executor_config and
agent_instructions. This reader never selects them, so they never reach a caller.

The list SQL is copied from ``list_projects`` in the core (meridian/db/__init__.py). The core
runs ``SELECT *``, so this reader names the eight columns it needs instead. The placeholder is
``?`` because the sqlite3 module takes qmark parameters.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

PROJECT_COLUMNS = (
    "id",
    "name",
    "creator_human_id",
    "icon",
    "status",
    "priority",
    "parent_project_id",
    "created_at",
)

# The column list is built from the constant above, so it never carries user input.
_SELECT = "SELECT " + ", ".join(PROJECT_COLUMNS) + " FROM projects"
_LIST_SQL = _SELECT + " ORDER BY created_at DESC"
_GET_SQL = _SELECT + " WHERE id = ?"


class NoDatabase(Exception):
    """No database file is at ``path``. The reader raises this and never creates the file."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        super().__init__(f"no Meridian database file at {self.path}")


def _uri(path: Path | str) -> str:
    """Return the SQLite file URI for ``path``, with mode=ro. Path.as_uri percent-encodes the path."""
    return Path(path).resolve().as_uri() + "?mode=ro"


def _connect(path: Path | str) -> sqlite3.Connection:
    """Open ``path`` read-only. Raise NoDatabase first when no file is there, so SQLite never creates one."""
    target = Path(path)
    if not target.is_file():
        raise NoDatabase(path)
    try:
        return sqlite3.connect(_uri(target), uri=True)
    except sqlite3.OperationalError:
        # The file can disappear between the check and the open. That is still a missing file.
        if not target.is_file():
            raise NoDatabase(path) from None
        raise


def _select(path: Path | str, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = _connect(path)
    try:
        fetched = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(zip(PROJECT_COLUMNS, row)) for row in fetched]


def list_projects(path: Path | str) -> list[dict[str, Any]]:
    """Return every project row, newest first by created_at. Each row is keyed by PROJECT_COLUMNS."""
    return _select(path, _LIST_SQL)


def get_project(path: Path | str, project_id: str) -> dict[str, Any] | None:
    """Return the project row with this id, keyed by PROJECT_COLUMNS, or None when there is none."""
    found = _select(path, _GET_SQL, (project_id,))
    return found[0] if found else None


__all__ = ["PROJECT_COLUMNS", "NoDatabase", "get_project", "list_projects"]
