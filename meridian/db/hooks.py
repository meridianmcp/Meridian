"""User-creatable Claude Code hooks — persistence layer.

273287cb — generalizes past sprint_guard.sh/.ps1 (the only hook Meridian
auto-writes, see ``handoff._write_sprint_guard_hooks``) so a project can
define its own arbitrary PreToolUse/PostToolUse/Stop hooks that get written
into ``.claude/hooks/`` by the same ``generate_handoff`` mechanism.

Each row is one hook: a POSIX shell body (required) and an optional
PowerShell body, an event, an optional tool-name ``matcher`` (ignored for
Stop hooks), and a ``blocking`` flag:

* ``blocking=True``  — the script is written byte-for-byte. Its own exit code
  drives Claude Code's real exit-code-blocking semantics (exit 2 blocks a
  PreToolUse call / a Stop / feeds PostToolUse output back to the model).
* ``blocking=False``  — the script is wrapped so an exit code of 2 is
  downgraded to 1 before Meridian writes the hook file. The hook still runs
  and its stderr/stdout are still surfaced, but it can never hard-block —
  "strong suggestion power" without determinism.

Imported back into ``meridian.db`` via an explicit named re-export at the
bottom of db/__init__.py, matching the workspace.py / sprint_items.py
extraction pattern.
"""
from __future__ import annotations

import re
from typing import Any

import aiosqlite

from meridian.db import (  # noqa: PLC0415
    _new_id,
    _row_to_dict,
)

VALID_HOOK_EVENTS: frozenset[str] = frozenset({"PreToolUse", "PostToolUse", "Stop"})

# c0d2356d's auto-managed sprint_guard files must never be shadowed / clobbered
# by a user-defined hook of the same slug.
_RESERVED_HOOK_SLUGS: frozenset[str] = frozenset({"sprint_guard"})


def _sanitize_hook_slug(name: str) -> str:
    """Derive a filesystem-safe, lowercase slug from a user-supplied hook name.

    Mirrors the conservative sanitization used elsewhere for generated
    filenames: lowercase, non ``[a-z0-9_-]`` runs collapse to a single
    underscore, leading/trailing underscores stripped. Never returns an
    empty string for non-empty input (falls back to "hook").
    """
    slug = re.sub(r"[^a-z0-9_-]+", "_", (name or "").strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_-")
    return slug or "hook"


async def add_custom_hook(
    db: aiosqlite.Connection,
    project_id: str,
    name: str,
    event: str,
    script_sh: str,
    script_ps1: str | None = None,
    matcher: str | None = None,
    blocking: bool = True,
    enabled: bool = True,
) -> dict[str, Any]:
    """Create a user-defined hook for ``project_id``.

    Raises ``ValueError`` for an unknown ``event``, an empty ``name``/
    ``script_sh``, a slug colliding with the reserved ``sprint_guard`` name,
    or a slug already used by another hook on this project (use
    ``update_custom_hook`` to edit an existing one).
    """
    if event not in VALID_HOOK_EVENTS:
        raise ValueError(
            f"event must be one of {sorted(VALID_HOOK_EVENTS)}, got {event!r}"
        )
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    script_sh = script_sh or ""
    if not script_sh.strip():
        raise ValueError("script_sh is required")
    slug = _sanitize_hook_slug(name)
    if slug in _RESERVED_HOOK_SLUGS:
        raise ValueError(
            f"'{slug}' is reserved for Meridian's own sprint_guard hook — choose a different name"
        )
    async with db.execute(
        "SELECT id FROM custom_hooks WHERE project_id = ? AND slug = ?",
        (project_id, slug),
    ) as cur:
        if await cur.fetchone() is not None:
            raise ValueError(f"a hook named '{slug}' already exists on this project")
    hid = _new_id()
    await db.execute(
        "INSERT INTO custom_hooks "
        "(id, project_id, name, slug, event, matcher, script_sh, script_ps1, "
        " blocking, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            hid, project_id, name, slug, event, matcher, script_sh, script_ps1,
            1 if blocking else 0, 1 if enabled else 0,
        ),
    )
    await db.commit()
    return await get_custom_hook(db, project_id, hid)  # type: ignore[return-value]


async def get_custom_hooks(
    db: aiosqlite.Connection,
    project_id: str,
    event: str | None = None,
    enabled_only: bool = False,
) -> list[dict[str, Any]]:
    """List a project's custom hooks, newest first. Optional event filter."""
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if event:
        clauses.append("event = ?")
        params.append(event)
    if enabled_only:
        clauses.append("enabled = 1")
    where = " AND ".join(clauses)
    async with db.execute(
        f"SELECT * FROM custom_hooks WHERE {where} ORDER BY created_at DESC",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def get_custom_hook(
    db: aiosqlite.Connection, project_id: str, hook_id: str
) -> dict[str, Any] | None:
    """Fetch a single custom hook, scoped to ``project_id``."""
    async with db.execute(
        "SELECT * FROM custom_hooks WHERE id = ? AND project_id = ?",
        (hook_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def update_custom_hook(
    db: aiosqlite.Connection,
    project_id: str,
    hook_id: str,
    **fields: Any,
) -> dict[str, Any] | None:
    """Patch a subset of a hook's editable fields. Returns None if not found.

    Editable: name, event, matcher, script_sh, script_ps1, blocking, enabled.
    ``name`` changes re-derive ``slug`` (subject to the same reserved-name /
    uniqueness checks as ``add_custom_hook``).
    """
    existing = await get_custom_hook(db, project_id, hook_id)
    if existing is None:
        return None
    _editable = {
        "name", "event", "matcher", "script_sh", "script_ps1", "blocking", "enabled",
    }
    sets: list[str] = []
    params: list[Any] = []
    if "event" in fields and fields["event"] is not None:
        if fields["event"] not in VALID_HOOK_EVENTS:
            raise ValueError(
                f"event must be one of {sorted(VALID_HOOK_EVENTS)}, got {fields['event']!r}"
            )
    if "name" in fields and fields["name"]:
        new_name = str(fields["name"]).strip()
        new_slug = _sanitize_hook_slug(new_name)
        if new_slug in _RESERVED_HOOK_SLUGS:
            raise ValueError(
                f"'{new_slug}' is reserved for Meridian's own sprint_guard hook"
            )
        if new_slug != existing["slug"]:
            async with db.execute(
                "SELECT id FROM custom_hooks WHERE project_id = ? AND slug = ? AND id != ?",
                (project_id, new_slug, hook_id),
            ) as cur:
                if await cur.fetchone() is not None:
                    raise ValueError(f"a hook named '{new_slug}' already exists on this project")
        sets.append("name = ?")
        params.append(new_name)
        sets.append("slug = ?")
        params.append(new_slug)
    for key in ("event", "matcher", "script_sh", "script_ps1"):
        if key in fields and fields.get(key) is not None:
            sets.append(f"{key} = ?")
            params.append(fields[key])
    if "blocking" in fields and fields["blocking"] is not None:
        sets.append("blocking = ?")
        params.append(1 if fields["blocking"] else 0)
    if "enabled" in fields and fields["enabled"] is not None:
        sets.append("enabled = ?")
        params.append(1 if fields["enabled"] else 0)
    unknown = set(fields) - _editable
    if unknown:
        raise ValueError(f"unknown field(s): {sorted(unknown)}")
    if not sets:
        return existing
    # Dialect-aware "now" expression: psycopg3's ?->%s adapter rewrites
    # placeholders but not SQLite-specific function calls, so PG connections
    # (detected the same way as elsewhere in db/__init__.py: hasattr(db, "_pool"))
    # need now() instead of datetime('now').
    now_expr = "now()" if hasattr(db, "_pool") else "datetime('now')"
    sets.append(f"updated_at = {now_expr}")
    params.extend([hook_id, project_id])
    await db.execute(
        f"UPDATE custom_hooks SET {', '.join(sets)} WHERE id = ? AND project_id = ?",
        params,
    )
    await db.commit()
    return await get_custom_hook(db, project_id, hook_id)


async def delete_custom_hook(
    db: aiosqlite.Connection, project_id: str, hook_id: str
) -> bool:
    """Hard-delete a custom hook. Returns True if a row was removed.

    Does NOT remove any already-written ``.claude/hooks/<slug>.*`` files —
    those are cleaned up on the next ``generate_handoff`` (only currently
    enabled hooks are (re)written; a deleted hook's stale files are left in
    place for the human to remove, same as any other repo file Meridian
    doesn't own outright).
    """
    async with db.execute(
        "DELETE FROM custom_hooks WHERE id = ? AND project_id = ?",
        (hook_id, project_id),
    ) as cur:
        rc = cur.rowcount or 0
    await db.commit()
    return rc > 0
