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
) -> str:
    """Render the structured fields as GOAL.md text. Missing fields
    become empty sections so the file always shows the full layout."""
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
) -> Path:
    """Render and write GOAL.md atomically (write tmp → rename).

    Returns the path that was written so the caller can log it.
    """
    path = path or default_goal_md_path()
    content = format_goal_md(project_name, north_star, version_goal, sprint)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# DB ↔ file glue
# ---------------------------------------------------------------------------


async def sync_goal_md_to_db(
    db: aiosqlite.Connection, path: Path | None = None
) -> dict[str, Any] | None:
    """If GOAL.md exists and names a known project, upsert its fields
    into that project's latest goal row.

    Returns the resulting goal dict, or ``None`` when nothing changed
    (file missing / no project name / project not in DB). DB wins
    when its ``updated_at`` is later than the file's mtime — the file
    has presumably gone stale waiting for the next disk write.
    """
    path = path or default_goal_md_path()
    parsed = read_goal_md(path)
    if parsed is None or not parsed.get("project_name"):
        return None
    project = await db_module.get_project_by_name(db, parsed["project_name"])
    if project is None:
        return None

    existing = await db_module.get_goal(db, project["id"])
    if existing is not None:
        # If the DB row is newer than the file's mtime, treat the DB
        # as authoritative — happens when MCP / dashboard wrote since
        # the human last saved the file.
        try:
            file_mtime = path.stat().st_mtime
            # SQLite timestamp is naïve UTC text.
            from datetime import datetime, timezone
            db_updated = datetime.fromisoformat(
                existing["updated_at"].replace(" ", "T")
            ).replace(tzinfo=timezone.utc)
            if db_updated.timestamp() > file_mtime + 1:
                return existing
        except (OSError, ValueError, KeyError):
            pass  # If we can't compare cleanly, fall through to upsert.

    return await db_module.set_goal(
        db,
        project["id"],
        parsed.get("version_goal") or (existing or {}).get("content") or "",
        north_star=parsed.get("north_star"),
        sprint=parsed.get("sprint"),
    )


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
    return write_goal_md(
        project["name"],
        goal.get("north_star"),
        version_goal,
        goal.get("sprint"),
        path=path,
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
                    await sync_goal_md_to_db(db, path)
                except Exception:  # noqa: BLE001 — never crash the loop
                    continue
    except Exception:  # noqa: BLE001 — graceful exit if watchfiles dies
        return
