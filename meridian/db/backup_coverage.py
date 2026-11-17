"""Read-only per-project backup coverage.

Where project backups are recorded today (grep "backup" in ``meridian/``):

- Database state (projects, goal_states, sessions, task_log, sprint_items,
  decisions): covered only by Neon point-in-time restore, documented in
  ``docs/backup-runbook.md``. No per-project or per-table backup record exists.
- Stored artifact content: ``meridian/db/object_sync_state.py`` keeps one row
  per ``(project_id, content_hash)`` with ``state``, ``synced_at`` and
  ``last_error``. This is the only per-part backup record. No route, tool or
  worker writes it yet (``docs/object-storage-backend.md``), so every artifact
  part currently reads as not backed up.
- Local artifact export (``artifact_capture.export_project``,
  ``artifact_store.export_artifacts``) writes a file the caller keeps and
  records nothing in the database, so it is not represented here.

This module only reads ``object_sync_state``. It never writes a row.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite

from meridian.db import list_projects
from meridian.db.object_sync_state import list_object_sync_states

#: Scope of the per-part record this module reads. Database tables are not
#: represented by any per-part record, so the boolean never speaks for them.
COVERAGE_SCOPE = "artifact_content"

#: Object-sync state that means a remote copy completed.
BACKED_UP_STATE = "synced"

#: Coordination tables that have no per-part backup record. Their only backup
#: is database-level Neon point-in-time restore (docs/backup-runbook.md).
UNRECORDED_DB_PARTS: tuple[str, ...] = (
    "projects",
    "goal_states",
    "sessions",
    "task_log",
    "sprint_items",
    "decisions",
)


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp to an aware datetime, or ``None``.

    Naive values are read as UTC. Unparseable values are ignored rather than
    raised, so one malformed ``synced_at`` cannot break the whole report.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _coverage_from_rows(project_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build one project's coverage report from its ``object_sync_state`` rows."""
    not_backed_up: list[dict[str, Any]] = []
    synced_count = 0
    latest: tuple[datetime, str] | None = None
    for row in rows:
        state = row.get("state")
        if state == BACKED_UP_STATE:
            synced_count += 1
            stamp = row.get("synced_at")
            parsed = _parse_ts(stamp)
            if parsed is not None and (latest is None or parsed > latest[0]):
                latest = (parsed, str(stamp))
        else:
            not_backed_up.append({
                "part": "artifact",
                "content_hash": row.get("content_hash"),
                "artifact_class": row.get("artifact_class"),
                "state": state,
                "last_error": row.get("last_error"),
                "updated_at": row.get("updated_at"),
            })
    return {
        "project_id": project_id,
        "coverage_scope": COVERAGE_SCOPE,
        "backed_up": synced_count > 0 and not not_backed_up,
        "last_backup_at": latest[1] if latest is not None else None,
        "artifact_parts_recorded": len(rows),
        "parts_not_backed_up": not_backed_up,
        "parts_without_backup_record": list(UNRECORDED_DB_PARTS),
    }


async def get_project_backup_coverage(
    db: aiosqlite.Connection, project_id: str,
) -> dict[str, Any]:
    """Return backup coverage for one project, from the existing sync-state record.

    ``backed_up`` is true only when at least one artifact part is recorded as
    ``synced`` and no recorded artifact part is in any other state. A project
    with no artifact rows is therefore not backed up. ``last_backup_at`` is the
    latest parseable ``synced_at`` among synced parts, or ``None``.
    """
    rows = await list_object_sync_states(db, project_id)
    return _coverage_from_rows(project_id, rows)


async def list_project_backup_coverage(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return backup coverage for every project, in ``list_projects`` order."""
    projects = await list_projects(db)
    return [
        await get_project_backup_coverage(db, str(project["id"]))
        for project in projects
    ]
