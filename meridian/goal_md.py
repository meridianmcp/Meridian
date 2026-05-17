"""Bidirectional GOAL.md ↔ SQLite sync (v0.6.3).

GOAL.md is the human-editable surface for the project goal in the free
/ OSS tier. Lives at repo root next to ROADMAP.md. The file is the
source of truth for keyboard-driven workflows; the DB is the source of
truth for MCP / dashboard workflows. We keep them in agreement:

* On server startup, if GOAL.md exists, parse and upsert the values
  into the latest goal row of the matching project. DB wins when the
  DB has a more recent ``updated_at`` — i.e. it was edited via MCP /
  dashboard since the file was last touched.
* On every ``set_goal`` / ``set_north_star`` / ``set_sprint`` call,
  the dashboard wrapper writes GOAL.md back to disk so a human editor
  reading the file sees the latest state.
* When ``watchfiles`` is installed, a background task in
  :func:`watch_goal_md` re-syncs the DB whenever the file changes on
  disk — saves from the human's editor land in the next dashboard
  refresh without a server restart.

Format (parse by ``##`` header levels):

```markdown
# project-name

## North Star
...prose / bullets...

## Version Goal
...prose / bullets...

## Sprint
...prose / bullets...
```

The first ``#`` heading becomes the project name. Missing sections are
treated as empty strings — partial files don't cause crashes.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import aiosqlite

from . import db as db_module

# Repo root: parent of this package dir. Overridable via env so tests
# can redirect to a tmp path and dev environments can move the file.
_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "GOAL.md"


def default_goal_md_path() -> Path:
    """Return the configured GOAL.md path (env override → repo root)."""
    override = os.environ.get("MERIDIAN_GOAL_MD")
    if override:
        return Path(override)
    return _DEFAULT_PATH


# Recognised section headers (case-insensitive, leading whitespace ok).
_SECTION_RE = re.compile(r"^\s*##\s+([^\n]+?)\s*$", re.MULTILINE)
# Friendly aliases → canonical key.
_SECTION_ALIASES = {
    "north star": "north_star",
    "northstar": "north_star",
    "version goal": "version_goal",
    "version": "version_goal",
    "goal": "version_goal",
    "sprint": "sprint",
    # v1.1.2 — append-only decisions section. Parsed but file-watch
    # treats it as separate from the three goal fields (decisions
    # never trigger conflict detection or attribution log_tasks).
    "decisions": "decisions",
}


def parse_goal_md(text: str) -> dict[str, str | None]:
    """Parse a GOAL.md string into a structured dict.

    Returns ``{project_name, north_star, version_goal, sprint}`` —
    missing sections are ``None``. The project name is the first
    ``#`` heading; if absent it's ``None`` too so the caller can
    decide whether to create / look up a project.
    """
    result: dict[str, str | None] = {
        "project_name": None,
        "north_star": None,
        "version_goal": None,
        "sprint": None,
        "decisions": None,
    }

    # Strip leading whitespace before the title scan so a BOM /
    # blank line at the top doesn't hide it.
    stripped = text.lstrip()
    title_match = re.match(r"#\s+([^\n]+)", stripped)
    if title_match:
        name = title_match.group(1).strip()
        if name:
            result["project_name"] = name

    # Walk every ## header and capture the body up to the next header.
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        header = m.group(1).strip().lower()
        key = _SECTION_ALIASES.get(header)
        if key is None:
            continue
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        result[key] = body or None
    return result


def format_goal_md(
    project_name: str,
    north_star: str | None,
    version_goal: str | None,
    sprint: str | None,
    decisions: str | None = None,
) -> str:
    """Render the structured fields as GOAL.md text. Missing fields
    become empty sections so the file always shows the full layout.

    ``decisions`` (v1.1.2) is appended below ``Sprint`` when provided.
    """
    parts = [f"# {project_name}", ""]
    parts.append("## North Star")
    parts.append("")
    parts.append((north_star or "").rstrip())
    parts.append("")
    parts.append("## Version Goal")
    parts.append("")
    parts.append((version_goal or "").rstrip())
    parts.append("")
    parts.append("## Sprint")
    parts.append("")
    parts.append((sprint or "").rstrip())
    parts.append("")
    if decisions is not None:
        parts.append("## Decisions")
        parts.append("")
        parts.append(decisions.rstrip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def read_goal_md(path: Path | None = None) -> dict[str, str | None] | None:
    """Read and parse GOAL.md. Returns ``None`` when the file is absent."""
    path = path or default_goal_md_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return parse_goal_md(text)


def write_goal_md(
    project_name: str,
    north_star: str | None,
    version_goal: str | None,
    sprint: str | None,
    path: Path | None = None,
    decisions: str | None = None,
) -> Path:
    """Render and write GOAL.md atomically (write tmp → rename).

    Returns the path that was written so the caller can log it.
    """
    path = path or default_goal_md_path()
    content = format_goal_md(
        project_name, north_star, version_goal, sprint, decisions
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# DB ↔ file glue
# ---------------------------------------------------------------------------


_FILE_WATCH_SESSION_NAME = "human/file-watch"


async def _file_watch_session_id(
    db: aiosqlite.Connection, project_id: str
) -> str:
    """Return (creating if needed) the session row used for file-watch
    attribution log_task entries (v1.1.2). One row per project."""
    async with db.execute(
        "SELECT id FROM sessions WHERE project_id = ? AND name = ? "
        "ORDER BY created_at ASC LIMIT 1",
        (project_id, _FILE_WATCH_SESSION_NAME),
    ) as cur:
        row = await cur.fetchone()
    if row is not None:
        return row[0]
    sess = await db_module.register_session(
        db, project_id, _FILE_WATCH_SESSION_NAME, human_id="human"
    )
    return sess["id"]


def _diff_goal_fields(
    new: dict[str, Any], old: dict[str, Any] | None
) -> list[str]:
    """Return the list of fields (north_star / version_goal / sprint)
    that differ between the parsed file and the DB row. Empty when
    the file says the same thing."""
    if old is None:
        old = {}
    changed: list[str] = []
    pairs = [
        ("north_star", new.get("north_star"), old.get("north_star")),
        ("version_goal", new.get("version_goal"), (
            old.get("content")
            if isinstance(old.get("content"), str)
            else (None if old.get("content") is None else str(old.get("content")))
        )),
        ("sprint", new.get("sprint"), old.get("sprint")),
    ]
    for field, n, o in pairs:
        nv = (n or "").strip()
        ov = (o or "").strip()
        if nv != ov:
            changed.append(field)
    return changed


async def sync_goal_md_to_db(
    db: aiosqlite.Connection,
    path: Path | None = None,
    *,
    via_watch: bool = False,
) -> dict[str, Any] | None:
    """If GOAL.md exists and names a known project, upsert its fields.

    v1.1.2 — when the DB row is newer than the file's mtime (someone
    edited via MCP / dashboard since the file was last saved) we
    *skip* the upsert AND write a conflict log_task entry so the
    drift is visible. When ``via_watch=True`` and a real change
    landed, we also write an attribution log_task per changed field
    so timelines show which fields the human edited and when.

    Returns the resulting goal dict, ``None`` when nothing changed,
    or a ``{conflict: True, ...}`` marker on conflict so callers can
    surface it (timeline rerender, dashboard toast).
    """
    path = path or default_goal_md_path()
    parsed = read_goal_md(path)
    if parsed is None or not parsed.get("project_name"):
        return None
    project = await db_module.get_project_by_name(db, parsed["project_name"])
    if project is None:
        return None

    existing = await db_module.get_goal(db, project["id"])

    # ── Conflict detection (v1.1.2) ────────────────────────────────────
    if existing is not None:
        try:
            file_mtime = path.stat().st_mtime
            from datetime import datetime, timezone
            db_updated = datetime.fromisoformat(
                existing["updated_at"].replace(" ", "T")
            ).replace(tzinfo=timezone.utc)
            if db_updated.timestamp() > file_mtime + 1:
                # DB is newer. Log a conflict task once so the user
                # sees it in the timeline; return the conflict marker.
                try:
                    sess_id = await _file_watch_session_id(db, project["id"])
                    await db_module.log_task(
                        db,
                        sess_id,
                        project["id"],
                        (
                            "GOAL.md conflict — file is older than DB "
                            "(file_mtime < db_updated_at); skipped sync"
                        ),
                        status="failed",
                    )
                except Exception:  # noqa: BLE001 — never break sync on log fail
                    pass
                return {
                    "conflict": True,
                    "reason": "db_newer_than_file",
                    "goal": existing,
                }
        except (OSError, ValueError, KeyError):
            pass  # If we can't compare cleanly, fall through to upsert.

    changed_fields = _diff_goal_fields(parsed, existing) if existing else [
        f for f in ("north_star", "version_goal", "sprint")
        if (parsed.get(f) or "").strip()
    ]
    if not changed_fields and existing is not None:
        # Nothing actually changed in the goal fields. Decisions
        # alone may have changed but we don't sync that field here
        # (v1.1.4 handles the decisions column).
        return existing

    result = await db_module.set_goal(
        db,
        project["id"],
        parsed.get("version_goal") or (existing or {}).get("content") or "",
        north_star=parsed.get("north_star"),
        sprint=parsed.get("sprint"),
    )

    # ── Attribution (v1.1.2) ──────────────────────────────────────────
    if via_watch and changed_fields:
        try:
            sess_id = await _file_watch_session_id(db, project["id"])
            for field in changed_fields:
                await db_module.log_task(
                    db,
                    sess_id,
                    project["id"],
                    f"GOAL.md edit — {field} updated by human (file watch)",
                    status="done",
                )
        except Exception:  # noqa: BLE001
            pass

    return result


async def sync_db_to_goal_md(
    db: aiosqlite.Connection,
    project_id: str,
    path: Path | None = None,
) -> Path | None:
    """Write the project's current goal state to GOAL.md.

    Returns the path written, or ``None`` when the project / goal is
    missing. Idempotent — safe to call after every set_goal /
    set_north_star / set_sprint.
    """
    project = await db_module.get_project(db, project_id)
    if project is None:
        return None
    goal = await db_module.get_goal(db, project_id)
    if goal is None:
        return None
    content = goal.get("content")
    version_goal = content if isinstance(content, str) else (
        None if content is None else str(content)
    )
    # v1.1.4 — surface the append-only decisions log in GOAL.md too
    # so the human editor sees the running log alongside the goal
    # fields. Decisions are read-only on the file side: edits to
    # this section are NOT synced back into the DB.
    decisions = await db_module.get_decisions(db, project_id)
    return write_goal_md(
        project["name"],
        goal.get("north_star"),
        version_goal,
        goal.get("sprint"),
        path=path,
        decisions=decisions,
    )


# ---------------------------------------------------------------------------
# Optional live file-watch (watchfiles)
# ---------------------------------------------------------------------------


async def watch_goal_md(
    db: aiosqlite.Connection,
    path: Path | None = None,
) -> None:
    """Watch GOAL.md for human edits and re-sync to the DB live.

    No-op (graceful) when ``watchfiles`` is not installed. Designed to
    be launched once at server startup via ``asyncio.create_task``.
    """
    path = path or default_goal_md_path()
    try:
        from watchfiles import awatch
    except ImportError:
        return
    try:
        async for _ in awatch(str(path.parent)):
            # We watch the parent dir so the file's eventual creation
            # also triggers a sync. Filter to events on our target.
            if path.exists():
                try:
                    # via_watch=True triggers attribution log_tasks for
                    # any field that actually changed (v1.1.2).
                    await sync_goal_md_to_db(db, path, via_watch=True)
                except Exception:  # noqa: BLE001 — never crash the loop
                    continue
    except Exception:  # noqa: BLE001 — graceful exit if watchfiles dies
        return
