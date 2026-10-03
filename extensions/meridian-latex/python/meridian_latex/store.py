"""Python-stage port of meridian-latex's local SQLite store and edit ledger.

The Node engine remains the live owner of the Overleaf AST, authentication,
sync/OT transport, and write dispatch. This module ports the local project and
provenance tables with schema-compatible names so the workstation companion
can migrate those local responsibilities independently. Pass the same
database path as the Node engine to open an existing store; the default is an
in-memory database to avoid silently creating a second persistent store.

All connections use SQLite autocommit. Callers own and close the connection.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

WHOLE_DOCUMENT_LEASE_NODE_ID = "__meridian_latex_whole_document_lease__"
CLAIM_TTL_MINUTES = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  project_id   TEXT PRIMARY KEY,
  last_seen_at TEXT NOT NULL,
  last_outline TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES projects(project_id),
  node_id      TEXT NOT NULL,
  holder_token TEXT NOT NULL,
  claimed_at   TEXT NOT NULL,
  released_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_claims_project ON claims (project_id, node_id);

CREATE TABLE IF NOT EXISTS provenance (
  id                         TEXT PRIMARY KEY,
  project_id                 TEXT NOT NULL REFERENCES projects(project_id),
  node_id                    TEXT NOT NULL,
  kind                       TEXT NOT NULL,
  field                      TEXT NOT NULL,
  old_value                  TEXT,
  new_value                  TEXT NOT NULL,
  holder_token               TEXT NOT NULL,
  recorded_at                TEXT NOT NULL,
  synced_to_meridian_outputs INTEGER NOT NULL DEFAULT 0,
  synced_at                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_provenance_project
  ON provenance (project_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_provenance_unsynced
  ON provenance (synced_to_meridian_outputs);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def open_store(db_path: str = ":memory:") -> sqlite3.Connection:
    """Open a schema-compatible local store with foreign keys and autocommit."""
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(_SCHEMA)
    return connection


def get_project(
    connection: sqlite3.Connection, project_id: str
) -> dict[str, Any] | None:
    """Return a stored project row or None when the project is not registered."""
    row = connection.execute(
        "SELECT * FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def upsert_project(
    connection: sqlite3.Connection, project_id: str, outline_nodes: Any
) -> None:
    """Register a project and replace its last outline snapshot."""
    connection.execute(
        """INSERT INTO projects (project_id, last_seen_at, last_outline)
           VALUES (?, ?, ?)
           ON CONFLICT(project_id) DO UPDATE SET
             last_seen_at = excluded.last_seen_at,
             last_outline = excluded.last_outline""",
        (
            project_id,
            _now_iso(),
            json.dumps(outline_nodes, ensure_ascii=False, separators=(",", ":")),
        ),
    )


def ensure_project_row(connection: sqlite3.Connection, project_id: str) -> None:
    """Register a project only if absent, preserving any existing outline."""
    connection.execute(
        """INSERT INTO projects (project_id, last_seen_at, last_outline)
           VALUES (?, ?, '[]')
           ON CONFLICT(project_id) DO NOTHING""",
        (project_id, _now_iso()),
    )


def record_edit(
    connection: sqlite3.Connection,
    *,
    project_id: Any,
    node_id: Any,
    kind: Any,
    field: Any,
    new_value: Any,
    holder_token: Any,
    old_value: Any = None,
) -> dict[str, Any]:
    """Record one verified local edit. Errors return a result instead of raising."""
    if not all((project_id, node_id, kind, field, holder_token)):
        return {
            "recorded": False,
            "reason": "project_id, node_id, kind, field, and holder_token are all required",
        }
    if not isinstance(new_value, str):
        return {"recorded": False, "reason": "new_value must be a string"}
    try:
        ensure_project_row(connection, project_id)
        record_id = str(uuid4())
        connection.execute(
            """INSERT INTO provenance
               (id, project_id, node_id, kind, field, old_value, new_value,
                holder_token, recorded_at, synced_to_meridian_outputs, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)""",
            (
                record_id,
                project_id,
                node_id,
                kind,
                field,
                old_value,
                new_value,
                holder_token,
                _now_iso(),
            ),
        )
        return {"recorded": True, "id": record_id}
    except Exception as exc:  # noqa: BLE001 - preserve the local-engine result contract
        return {"recorded": False, "reason": f"internal error: {exc}"}


def list_provenance(
    connection: sqlite3.Connection,
    *,
    project_id: Any = None,
    unsynced_only: bool = False,
) -> list[dict[str, Any]]:
    """List a project's edit ledger, newest first; failures degrade to empty."""
    if not project_id:
        return []
    query = "SELECT * FROM provenance WHERE project_id = ?"
    if unsynced_only:
        query += " AND synced_to_meridian_outputs = 0"
    query += " ORDER BY recorded_at DESC"
    try:
        return [dict(row) for row in connection.execute(query, (project_id,))]
    except Exception:  # noqa: BLE001 - match the local engine's best-effort read
        return []


def mark_synced(
    connection: sqlite3.Connection, ids: Any
) -> dict[str, Any]:
    """Mark provenance ids after a caller actually syncs them to Meridian Outputs."""
    if not isinstance(ids, list) or not ids:
        return {"marked": 0}
    marked = 0
    try:
        statement = """UPDATE provenance
                       SET synced_to_meridian_outputs = 1, synced_at = ?
                       WHERE id = ?"""
        for record_id in ids:
            cursor = connection.execute(statement, (_now_iso(), record_id))
            marked += cursor.rowcount
        return {"marked": marked}
    except Exception as exc:  # noqa: BLE001 - preserve the local-engine result contract
        return {"marked": 0, "reason": f"internal error: {exc}"}
