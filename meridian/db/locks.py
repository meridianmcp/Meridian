"""File/symbol/resource/docx-region claim and lock functions — extracted from meridian/db/__init__.py.

This module contains all functions whose primary subject is the claim-locking
system: file locks (whole-file + shared-read), symbol-level claims, typed
resource locks, docx element-region claims, and their shared TTL/expiry
infrastructure.

Imported back into meridian.db via an explicit named re-export at the bottom of
db/__init__.py so all existing ``db_module.claim_file(...)``-style call sites are
unaffected.
"""
from __future__ import annotations

from typing import Any

import aiosqlite

# Shared helpers from the parent db package.  These are imported at the BOTTOM
# of db/__init__.py (after the parent module defines them), then re-used here.
# Using a lazy module-attribute import via the package __init__ avoids the
# circular-import that would occur if we imported at module top-level while
# db/__init__.py is still being initialised.
from meridian.db import (  # noqa: PLC0415
    _new_id,
    _row_to_dict,
    get_code_notes_for_file,
    get_decisions_for_file,
)

# ---------------------------------------------------------------------------
# 356d6ac8 — structural-degradation patch threshold.
#
# When a session write-claims a file >= this many times without the executor
# flagging a deliberate refactor (refactor_flagged), get_structural_degradation_warnings
# surfaces the file as a degradation risk. The heuristic is intentionally blunt:
# many small patches on the same file within one session = likely symptom-chasing.
# A higher count means fewer false positives but later detection.
# ---------------------------------------------------------------------------
_PATCH_DEGRADATION_THRESHOLD = 3


# ---------------------------------------------------------------------------
# v3.1 — file lock coordination
# ---------------------------------------------------------------------------

_FILE_LOCK_TTL_HOURS = 2

# 39544099 — shared staleness constant so file_locks and file_symbol_claims use the
# same TTL. Both mechanisms now expire via heartbeat (session.last_seen > TTL) in
# addition to the explicit expires_at column.
_CLAIM_LIVE_HOURS = _FILE_LOCK_TTL_HOURS


def _cutoff_dt(hours: int) -> str:
    """Return an ISO-8601 datetime string ``hours`` ago (UTC).

    Used as the shared staleness cutoff for file_locks and file_symbol_claims.
    """
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_file_path(file_path: str | None) -> str:
    """Normalize a file path the same way ``claim_file`` stores it.

    claim_file stores ``(file_path or "").strip()`` verbatim — no separator
    rewriting — so code-anchored notes (771c00d7) must apply the *identical*
    rule for their ``file_path`` anchor to match a claim. Centralized here so
    the anchor and the lock can never drift apart.
    """
    return (file_path or "").strip()


async def _code_notes_for_session_file(
    db: aiosqlite.Connection,
    session_id: str,
    file_path: str,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve a session's project, then return its code-anchored notes for a file.

    ``claim_file`` is keyed by session_id (not project_id), so we look up the
    owning project here before delegating to :func:`get_code_notes_for_file`.
    Best-effort: an unknown session id yields ``[]`` rather than raising, so the
    file-lock path is never broken by the additive code-notes surface.
    """
    async with db.execute(
        "SELECT project_id FROM sessions WHERE id = ?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
    sess = _row_to_dict(row)
    if not sess or not sess.get("project_id"):
        return []
    return await get_code_notes_for_file(
        db, sess["project_id"], file_path, symbol
    )


async def _decision_notes_for_session_file(
    db: aiosqlite.Connection,
    session_id: str,
    file_path: str,
) -> list[dict[str, Any]]:
    """Return code-anchored decisions for the file, resolved via session's project.

    777f26b0 — companion to ``_code_notes_for_session_file``: fetches active
    decisions whose ``code_anchor`` matches ``file_path`` so they can be injected
    into the ``claim_file`` response as ``decision_notes``. Best-effort: unknown
    session ids or pre-migration DBs yield ``[]`` rather than raising.
    """
    async with db.execute(
        "SELECT project_id FROM sessions WHERE id = ?", (session_id,)
    ) as cur:
        row = await cur.fetchone()
    sess = _row_to_dict(row)
    if not sess or not sess.get("project_id"):
        return []
    return await get_decisions_for_file(db, sess["project_id"], file_path)


async def expire_file_locks(db: aiosqlite.Connection) -> int:
    """Delete expired file locks and return how many rows were cleared.

    39544099 — two expiry paths (unified with file_symbol_claims):
    1. Explicit TTL: expires_at column <= now (original).
    2. Heartbeat: owning session's last_seen is older than _CLAIM_LIVE_HOURS
       (handles crashed/orphaned sessions whose lock was never explicitly released).
    """
    stale_cutoff = _cutoff_dt(_CLAIM_LIVE_HOURS)
    cursor = await db.execute(
        "DELETE FROM file_locks WHERE expires_at <= datetime('now') "
        "OR session_id IN ("
        "    SELECT id FROM sessions "
        "    WHERE last_seen IS NOT NULL AND last_seen < ?"
        ")",
        (stale_cutoff,),
    )
    await db.commit()
    return cursor.rowcount


async def expire_stale_symbol_claims(db: aiosqlite.Connection) -> int:
    """Soft-release symbol claims whose owning session's heartbeat has gone stale.

    39544099 — parallel to expire_file_locks but for file_symbol_claims. Uses the
    same _CLAIM_LIVE_HOURS cutoff so both expiry mechanisms share one constant.
    Marks claims as released (sets released_at) rather than deleting so the hotspot
    history (session_count aggregation) is preserved.
    """
    stale_cutoff = _cutoff_dt(_CLAIM_LIVE_HOURS)
    cursor = await db.execute(
        "UPDATE file_symbol_claims SET released_at = datetime('now') "
        "WHERE released_at IS NULL "
        "AND session_id IN ("
        "    SELECT id FROM sessions "
        "    WHERE last_seen IS NOT NULL AND last_seen < ?"
        ")",
        (stale_cutoff,),
    )
    await db.commit()
    return cursor.rowcount


async def _amend_sprint_item_resources_for_session(
    db: aiosqlite.Connection,
    session_id: str,
    new_resource_id: str,
) -> dict[str, Any] | None:
    """2593a5fe — append a newly-claimed resource to the active sprint item's
    touches_resources if it is not already declared, and set resources_amended=1.

    Called as a best-effort side-effect of claim_file and claim_symbol when the
    claim succeeds. Finds the sprint item this session currently holds in_progress
    (via sprint_items.actor = session_id) and checks whether ``new_resource_id``
    is already in its touches_resources declaration.

    If the resource IS already declared — no-op (returns None).
    If the resource is NEW:
      - Appends it to touches_resources (GROW, never replace — the original
        declaration may still be partially accurate).
      - Sets resources_amended = 1, marking that the item's resource footprint
        changed after its wave label was computed (so a human / planner can see
        the drift and decide whether to re-run assign_sprint_waves).

    Returns a dict ``{"wave_assignment_hint": ..., "amended_resource": ...,
    "item_id": ..., "item_wave": ...}`` when an amendment was made AND the item
    has an existing wave label, so the caller can include this as a visible signal
    in the claim response. Returns None when no amendment was needed (resource
    already present) or when there is no active sprint item for this session.

    Never raises — all errors are swallowed so this side-effect never blocks
    a file/symbol claim.
    """
    try:
        from meridian.db import (  # noqa: PLC0415
            parse_touches_resources,
            serialize_touches_resources,
        )
        # Find the sprint item this session currently holds in_progress.
        async with db.execute(
            "SELECT id, touches_resources, wave FROM sprint_items "
            "WHERE actor = ? AND status = 'in_progress' "
            "ORDER BY claimed_at DESC LIMIT 1",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        item = _row_to_dict(row)
        if not item or not item.get("id"):
            return None
        item_id = item["id"]
        current_wave = item.get("wave")
        # Normalize the incoming resource id so the comparison is canonical.
        try:
            from meridian.db import normalize_resource_id  # noqa: PLC0415
            canonical = normalize_resource_id(new_resource_id)
        except (ValueError, ImportError):
            return None  # malformed resource id — don't amend
        # Check whether the resource is already declared.
        existing = parse_touches_resources(item.get("touches_resources"))
        # Compare against canonical forms of existing resources, stripping
        # any "inferred:" prefix.
        existing_canonical = set()
        for r in existing:
            body = r[len("inferred:"):].strip() if r.lower().startswith("inferred:") else r
            try:
                existing_canonical.add(normalize_resource_id(body))
            except ValueError:
                existing_canonical.add(body)
        if canonical in existing_canonical:
            return None  # already declared — no amendment needed
        # Resource is new: append it (grow, don't replace).
        amended = existing + [canonical]
        new_json = serialize_touches_resources(amended)
        await db.execute(
            "UPDATE sprint_items SET touches_resources = ?, resources_amended = 1 "
            "WHERE id = ?",
            (new_json, item_id),
        )
        await db.commit()
        hint: dict[str, Any] | None = None
        if current_wave:
            hint = {
                "wave_assignment_hint": (
                    f"WAVE_STALE: sprint item {item_id[:8]} (wave {current_wave!r}) "
                    f"claimed resource {canonical!r} which was NOT in its original "
                    "touches_resources declaration. The stored wave label may no "
                    "longer reflect the item's true resource footprint. Consider "
                    "re-running assign_sprint_waves after this item completes."
                ),
                "amended_resource": canonical,
                "item_id": item_id,
                "item_wave": current_wave,
            }
        else:
            hint = {
                "wave_assignment_hint": None,
                "amended_resource": canonical,
                "item_id": item_id,
                "item_wave": None,
            }
        return hint
    except Exception:  # noqa: BLE001 — never wedge a claim on an amendment error
        return None


async def claim_file(
    db: aiosqlite.Connection,
    file_path: str,
    session_id: str,
    *,
    symbol: str | None = None,
    ttl_hours: int = _FILE_LOCK_TTL_HOURS,
    mode: str = "write",
) -> dict[str, Any]:
    """Claim a file path for a session, auto-releasing expired locks first.

    771c00d7 — the returned dict carries a ``code_notes`` list: project notes
    anchored to this file path (note_kind='code'), so the executor sees relevant
    warnings/context before editing. When ``symbol`` is given, symbol-scoped
    anchors for that symbol are preferred but file-level anchors (no symbol)
    are always included. Empty when none. Additive — existing callers are
    unaffected.

    ffa03655 — ``mode`` selects the claim grain. ``write`` (default, legacy) is
    an EXCLUSIVE lock: it blocks other writers and is itself blocked by any other
    session's live read claim ("no lock on an open door" for reads, exclusion for
    writes). ``read`` is a SHARED claim: many sessions can hold a read claim on
    the same file concurrently (zero false contention for parallel reader agents),
    blocked only by another session's exclusive write lock.
    """
    normalized = _normalize_file_path(file_path)
    if not normalized:
        raise ValueError("file_path is required")
    await expire_file_locks(db)
    await expire_file_read_claims(db)
    _mode = "read" if str(mode or "write").lower() == "read" else "write"
    if _mode == "read":
        return await _claim_file_read(db, normalized, session_id, ttl_hours, symbol)
    # ffa03655 — exclusive write waits for readers: another session's live read
    # claim blocks a write claim.
    _readers = await _other_read_claims(db, normalized, session_id)
    if _readers:
        return {
            "claimed": False,
            "reason": "read_locked",
            "claim_mode": "write",
            "file_path": normalized,
            "session_id": session_id,
            "read_claims": [r.get("session_id") for r in _readers],
            "message": (
                f"Cannot write-claim {normalized}: {len(_readers)} reader(s) hold a "
                "shared read claim. Wait for readers to release, or read-claim instead."
            ),
        }
    # 63b030a6 — file ⊃ symbol hierarchy: a whole-file lock conflicts with any
    # live symbol claim on the file held by another session. Block here so the
    # coarser grain can't silently stomp a finer one.
    _other_symbols = await _live_symbol_claims_for_file(db, normalized, session_id)
    if _other_symbols:
        _holder = _other_symbols[0]
        return {
            "claimed": False,
            "reason": "symbol_locked",
            "file_path": normalized,
            "session_id": session_id,
            "holder_session_id": _holder.get("session_id"),
            "symbol_claims": _other_symbols,
            "message": (
                f"Cannot whole-file claim {normalized}: "
                f"{len(_other_symbols)} symbol(s) on it are claimed by another live "
                "session. Claim a specific free symbol with claim_symbol, or wait."
            ),
        }
    async with db.execute(
        "SELECT * FROM file_locks WHERE file_path = ?",
        (normalized,),
    ) as cur:
        existing_row = await cur.fetchone()
    existing = _row_to_dict(existing_row)
    if existing and existing.get("session_id") != session_id:
        return {
            "claimed": False,
            "file_path": normalized,
            "session_id": session_id,
            "holder_session_id": existing.get("session_id"),
            "claimed_at": existing.get("claimed_at"),
            "expires_at": existing.get("expires_at"),
            "code_notes": await _code_notes_for_session_file(
                db, session_id, normalized, symbol
            ),
            "decision_notes": await _decision_notes_for_session_file(
                db, session_id, normalized
            ),
        }

    if existing and existing.get("session_id") == session_id:
        await db.execute(
            "UPDATE file_locks SET claimed_at = datetime('now'), "
            "expires_at = datetime('now', ? || ' hours') "
            "WHERE id = ?",
            (str(ttl_hours), existing["id"]),
        )
    else:
        # Atomic INSERT — ON CONFLICT DO NOTHING races another concurrent INSERT
        # on the same file_path. Re-select to check who won. Safe on both SQLite
        # (single-writer) and Postgres (UNIQUE constraint is atomic).
        await db.execute(
            "INSERT INTO file_locks (id, file_path, session_id, claimed_at, expires_at) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now', ? || ' hours')) "
            "ON CONFLICT (file_path) DO NOTHING",
            (_new_id(), normalized, session_id, str(ttl_hours)),
        )
    await db.commit()
    async with db.execute(
        "SELECT * FROM file_locks WHERE file_path = ?",
        (normalized,),
    ) as cur:
        row = await cur.fetchone()
    lock = _row_to_dict(row) or {}
    # b033c10f — re-check after INSERT to detect concurrent claim by another session.
    # The ON CONFLICT DO NOTHING is a no-op when another session raced us; the
    # re-SELECT reveals the actual winner. UPDATE path is already idempotent.
    if lock.get("session_id") != session_id:
        return {
            "claimed": False,
            "file_path": normalized,
            "session_id": session_id,
            "holder_session_id": lock.get("session_id"),
            "claimed_at": lock.get("claimed_at"),
            "expires_at": lock.get("expires_at"),
            "code_notes": await _code_notes_for_session_file(
                db, session_id, normalized, symbol
            ),
            "decision_notes": await _decision_notes_for_session_file(
                db, session_id, normalized
            ),
        }
    # 356d6ac8 — increment the structural-degradation patch counter for this
    # write claim (best-effort; counter errors never block the claim response).
    await _increment_file_patch_counter(db, session_id, normalized)
    # 2593a5fe — amend the active sprint item's touches_resources if this file
    # was not in the original declaration (mid-execution pivot detection).
    # Best-effort: errors never block the claim. Use "file:<path>" as resource id.
    _resource_hint = await _amend_sprint_item_resources_for_session(
        db, session_id, f"file:{normalized}"
    )
    result: dict[str, Any] = {
        "claimed": True,
        "claim_mode": "write",
        "file_path": normalized,
        "session_id": lock.get("session_id"),
        "claimed_at": lock.get("claimed_at"),
        "expires_at": lock.get("expires_at"),
        "code_notes": await _code_notes_for_session_file(
            db, session_id, normalized, symbol
        ),
        # 777f26b0 — decisions with code_anchor matching this file path.
        "decision_notes": await _decision_notes_for_session_file(
            db, session_id, normalized
        ),
    }
    if _resource_hint and _resource_hint.get("wave_assignment_hint"):
        result["wave_assignment_hint"] = _resource_hint["wave_assignment_hint"]
    return result


async def expire_file_read_claims(db: aiosqlite.Connection) -> None:
    """ffa03655 — drop read claims whose TTL has lapsed (mirrors expire_file_locks).

    949cf1e5 — dialect-split the cutoff comparison. Unlike every sibling lock table
    (file_locks / resource_locks / file_symbol_claims), whose ``expires_at`` is TEXT
    on Postgres, ``file_read_claims.expires_at`` is a TIMESTAMPTZ (pg_adapter). The
    shared ``datetime('now')`` form is rewritten by the adapter to a ``to_char(...)``
    *text* expression, so on Postgres ``expires_at < datetime('now')`` became
    ``timestamptz < text`` → ``operator does not exist: timestamp with time zone <
    text`` (a hard crash surfaced through get_file_claims / claim_file). SQLite is
    loosely typed so it hid the mismatch, and CI is SQLite-only so it never caught it.
    On Postgres compare against a real ``now()`` timestamp; keep the text-comparing
    ``datetime('now')`` on SQLite (where the column is TEXT).
    """
    if hasattr(db, "_pool"):  # Postgres — TIMESTAMPTZ column, compare to a timestamp
        await db.execute(
            "DELETE FROM file_read_claims WHERE expires_at < now()"
        )
    else:  # SQLite — TEXT column, lexical ISO comparison against datetime('now')
        await db.execute(
            "DELETE FROM file_read_claims WHERE expires_at < datetime('now')"
        )
    await db.commit()


async def _other_read_claims(
    db: aiosqlite.Connection, file_path: str, session_id: str
) -> list[dict[str, Any]]:
    """Live read claims on ``file_path`` held by sessions other than this one."""
    async with db.execute(
        "SELECT * FROM file_read_claims WHERE file_path = ? AND session_id != ?",
        (file_path, session_id),
    ) as cur:
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def _all_read_claims(
    db: aiosqlite.Connection, file_path: str
) -> list[dict[str, Any]]:
    async with db.execute(
        "SELECT * FROM file_read_claims WHERE file_path = ?", (file_path,)
    ) as cur:
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def _claim_file_read(
    db: aiosqlite.Connection,
    normalized: str,
    session_id: str,
    ttl_hours: int,
    symbol: str | None,
) -> dict[str, Any]:
    """ffa03655 — acquire (or refresh) a SHARED read claim on ``normalized``.

    Blocked only by another session's exclusive write lock; multiple sessions may
    hold a read claim on the same file at once.
    """
    async with db.execute(
        "SELECT * FROM file_locks WHERE file_path = ?", (normalized,)
    ) as cur:
        wrow = _row_to_dict(await cur.fetchone())
    if wrow and wrow.get("session_id") != session_id:
        return {
            "claimed": False,
            "reason": "write_locked",
            "claim_mode": "read",
            "file_path": normalized,
            "session_id": session_id,
            "holder_session_id": wrow.get("session_id"),
            "message": (
                f"Cannot read-claim {normalized}: it is write-locked by another "
                "live session. Wait for the writer to release."
            ),
        }
    async with db.execute(
        "SELECT id FROM file_read_claims WHERE file_path = ? AND session_id = ?",
        (normalized, session_id),
    ) as cur:
        existing = _row_to_dict(await cur.fetchone())
    # 949cf1e5 — file_read_claims.claimed_at/expires_at are TIMESTAMPTZ on
    # Postgres (unlike every sibling lock table's TEXT columns), so the shared
    # datetime('now', ...) form -- adapter-rewritten to a to_char(...) *text*
    # expression -- fails with "column ... is of type timestamp with time zone
    # but expression is of type text". Dialect-split like expire_file_read_claims.
    if hasattr(db, "_pool"):  # Postgres — TIMESTAMPTZ columns
        if existing:
            await db.execute(
                "UPDATE file_read_claims SET claimed_at = now(), "
                "expires_at = now() + (? || ' hours')::interval WHERE id = ?",
                (str(ttl_hours), existing["id"]),
            )
        else:
            await db.execute(
                "INSERT INTO file_read_claims (id, file_path, session_id, claimed_at, expires_at) "
                "VALUES (?, ?, ?, now(), now() + (? || ' hours')::interval)",
                (_new_id(), normalized, session_id, str(ttl_hours)),
            )
    else:  # SQLite — TEXT columns
        if existing:
            await db.execute(
                "UPDATE file_read_claims SET claimed_at = datetime('now'), "
                "expires_at = datetime('now', ? || ' hours') WHERE id = ?",
                (str(ttl_hours), existing["id"]),
            )
        else:
            await db.execute(
                "INSERT INTO file_read_claims (id, file_path, session_id, claimed_at, expires_at) "
                "VALUES (?, ?, ?, datetime('now'), datetime('now', ? || ' hours'))",
                (_new_id(), normalized, session_id, str(ttl_hours)),
            )
    await db.commit()
    readers = await _all_read_claims(db, normalized)
    return {
        "claimed": True,
        "claim_mode": "read",
        "file_path": normalized,
        "session_id": session_id,
        "readers": [r.get("session_id") for r in readers],
        "reader_count": len(readers),
        "code_notes": await _code_notes_for_session_file(
            db, session_id, normalized, symbol
        ),
    }


async def release_file(
    db: aiosqlite.Connection,
    file_path: str,
    session_id: str,
) -> bool:
    """Release a file lock only when it is owned by ``session_id``."""
    normalized = (file_path or "").strip()
    if not normalized:
        return False
    cursor = await db.execute(
        "DELETE FROM file_locks WHERE file_path = ? AND session_id = ?",
        (normalized, session_id),
    )
    # Also soft-release any symbol-level claims this session holds on the file
    # (4bac57ff) — keeps hotspot history while freeing the symbols.
    sym_cursor = await db.execute(
        "UPDATE file_symbol_claims SET released_at = datetime('now') "
        "WHERE file_path = ? AND session_id = ? AND released_at IS NULL",
        (normalized, session_id),
    )
    # ffa03655 — also drop any shared read claim this session holds on the file.
    read_cursor = await db.execute(
        "DELETE FROM file_read_claims WHERE file_path = ? AND session_id = ?",
        (normalized, session_id),
    )
    await db.commit()
    return cursor.rowcount > 0 or sym_cursor.rowcount > 0 or read_cursor.rowcount > 0


async def release_file_locks_for_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> int:
    """Release every file lock held by a session (write locks + read claims)."""
    cursor = await db.execute(
        "DELETE FROM file_locks WHERE session_id = ?",
        (session_id,),
    )
    # ffa03655 — also drop the session's shared read claims on cleanup.
    await db.execute(
        "DELETE FROM file_read_claims WHERE session_id = ?",
        (session_id,),
    )
    await db.commit()
    return cursor.rowcount


async def get_file_conflict_warnings(
    db: aiosqlite.Connection,
    project_id: str,
    exclude_session_id: str,
) -> list[str]:
    """Return warning strings for files claimed by other recently-active sessions.

    Checks the file_locks table for locks held by sessions other than
    ``exclude_session_id`` whose owning session is still live (status active/live
    and last_seen within the last 10 minutes). Returns human-readable strings
    like ``"dashboard.js claimed by session pre-launch-final (2h ago)"``.
    """
    from datetime import datetime, timezone, timedelta
    cutoff_10m = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    warnings: list[str] = []
    try:
        async with db.execute(
            "SELECT fl.file_path, s.name AS session_name, s.id AS session_id, s.last_seen "
            "FROM file_locks fl "
            "JOIN sessions s ON s.id = fl.session_id "
            "WHERE fl.session_id != ? "
            "AND s.project_id = ? "
            "AND s.status IN ('active', 'live') "
            "AND (s.last_seen IS NULL OR s.last_seen > ?)",
            (exclude_session_id, project_id, cutoff_10m),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            r = _row_to_dict(row)
            if not r:
                continue
            name = r.get("session_name") or (r.get("session_id") or "unknown")[:8]
            last_seen = r.get("last_seen") or ""
            if last_seen:
                warnings.append(
                    f"{r['file_path']} claimed by session {name} (last_seen {last_seen})"
                )
            else:
                warnings.append(f"{r['file_path']} claimed by session {name}")
    except Exception:  # noqa: BLE001
        pass
    return warnings


async def get_file_claims(
    db: aiosqlite.Connection,
    file_path: str,
    project_id: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Return active claims on a file: the whole-file lock plus symbol claims.

    Read-only. Expires stale whole-file locks first so callers never see a
    lock past its TTL. ``file_lock`` is the active ``file_locks`` row (with the
    holder's ``session_name``) or ``None``; ``symbol_claims`` is the list from
    :func:`get_symbol_claims`.

    771c00d7 — when ``project_id`` is supplied, the result also carries a
    ``code_notes`` list of that project's code-anchored notes for this path
    (symbol-scoped when ``symbol`` is given). ``project_id=None`` keeps the
    legacy two-key result for callers that don't track a project.
    """
    normalized = _normalize_file_path(file_path)
    await expire_file_locks(db)
    await expire_file_read_claims(db)
    async with db.execute(
        "SELECT fl.*, s.name AS session_name FROM file_locks fl "
        "LEFT JOIN sessions s ON s.id = fl.session_id "
        "WHERE fl.file_path = ?",
        (normalized,),
    ) as cur:
        row = await cur.fetchone()
    result: dict[str, Any] = {
        "file_path": normalized,
        "file_lock": _row_to_dict(row),
        "symbol_claims": await get_symbol_claims(db, normalized),
        # ffa03655 — shared read claims held on this file (many concurrent).
        "read_claims": await _all_read_claims(db, normalized),
    }
    if project_id:
        result["code_notes"] = await get_code_notes_for_file(
            db, project_id, normalized, symbol
        )
    return result


# ---------------------------------------------------------------------------
# Generalized typed-resource locks (501ec93f)
# ---------------------------------------------------------------------------

async def expire_resource_locks(db: aiosqlite.Connection) -> int:
    """Delete expired resource locks and return how many rows were cleared.

    Mirrors :func:`expire_file_locks` exactly — two expiry paths: explicit TTL
    (expires_at <= now) and owning-session heartbeat (last_seen older than
    _CLAIM_LIVE_HOURS, for crashed sessions that never released).
    """
    stale_cutoff = _cutoff_dt(_CLAIM_LIVE_HOURS)
    cursor = await db.execute(
        "DELETE FROM resource_locks WHERE expires_at <= datetime('now') "
        "OR session_id IN ("
        "    SELECT id FROM sessions "
        "    WHERE last_seen IS NOT NULL AND last_seen < ?"
        ")",
        (stale_cutoff,),
    )
    await db.commit()
    return cursor.rowcount


async def claim_resource(
    db: aiosqlite.Connection,
    resource_id: str,
    session_id: str,
    *,
    ttl_hours: int = _FILE_LOCK_TTL_HOURS,
) -> dict[str, Any]:
    """Claim a typed resource for a session, auto-releasing expired locks first.

    Same primitive as :func:`claim_file` but for any typed resource id. Returns
    ``{"claimed": bool, "resource_id", "resource_type", "session_id",
    "claimed_at", "expires_at", ...}``. When another live session already holds
    the resource, ``claimed`` is False and ``holder_session_id`` names the owner.
    Re-claiming a resource you already hold refreshes the TTL (idempotent).
    """
    # Import normalize/parse helpers from parent — they live in __init__ because
    # sprint_items.py also imports them (ARCH 1A) and they are not exclusive to locks.
    from meridian.db import normalize_resource_id, parse_resource_identifier  # noqa: PLC0415
    normalized = normalize_resource_id(resource_id)  # raises on bad input
    rtype, _ = parse_resource_identifier(normalized)
    await expire_resource_locks(db)
    async with db.execute(
        "SELECT * FROM resource_locks WHERE resource_id = ?",
        (normalized,),
    ) as cur:
        existing_row = await cur.fetchone()
    existing = _row_to_dict(existing_row)
    if existing and existing.get("session_id") != session_id:
        return {
            "claimed": False,
            "resource_id": normalized,
            "resource_type": rtype,
            "session_id": session_id,
            "holder_session_id": existing.get("session_id"),
            "claimed_at": existing.get("claimed_at"),
            "expires_at": existing.get("expires_at"),
        }
    if existing and existing.get("session_id") == session_id:
        await db.execute(
            "UPDATE resource_locks SET claimed_at = datetime('now'), "
            "expires_at = datetime('now', ? || ' hours') WHERE id = ?",
            (str(ttl_hours), existing["id"]),
        )
    else:
        await db.execute(
            "INSERT INTO resource_locks "
            "(id, resource_id, resource_type, session_id, claimed_at, expires_at) "
            "VALUES (?, ?, ?, ?, datetime('now'), datetime('now', ? || ' hours')) "
            "ON CONFLICT (resource_id) DO NOTHING",
            (_new_id(), normalized, rtype, session_id, str(ttl_hours)),
        )
    await db.commit()
    async with db.execute(
        "SELECT * FROM resource_locks WHERE resource_id = ?",
        (normalized,),
    ) as cur:
        row = await cur.fetchone()
    lock = _row_to_dict(row) or {}
    if lock.get("session_id") != session_id:
        # Another session raced us (ON CONFLICT DO NOTHING was a no-op).
        return {
            "claimed": False,
            "resource_id": normalized,
            "resource_type": rtype,
            "session_id": session_id,
            "holder_session_id": lock.get("session_id"),
            "claimed_at": lock.get("claimed_at"),
            "expires_at": lock.get("expires_at"),
        }
    return {
        "claimed": True,
        "resource_id": normalized,
        "resource_type": rtype,
        "session_id": lock.get("session_id"),
        "claimed_at": lock.get("claimed_at"),
        "expires_at": lock.get("expires_at"),
    }


async def release_resource(
    db: aiosqlite.Connection,
    resource_id: str,
    session_id: str,
) -> bool:
    """Release a resource lock only when it is owned by ``session_id``."""
    from meridian.db import normalize_resource_id  # noqa: PLC0415
    try:
        normalized = normalize_resource_id(resource_id)
    except ValueError:
        return False
    cursor = await db.execute(
        "DELETE FROM resource_locks WHERE resource_id = ? AND session_id = ?",
        (normalized, session_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def release_resource_locks_for_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> int:
    """Release every resource lock held by a session."""
    cursor = await db.execute(
        "DELETE FROM resource_locks WHERE session_id = ?",
        (session_id,),
    )
    await db.commit()
    return cursor.rowcount


async def get_resource_claims(
    db: aiosqlite.Connection,
    resource_id: str,
) -> dict[str, Any]:
    """Return the active lock on a resource (with holder session_name) or None.

    Read-only. Expires stale locks first so callers never see a lock past TTL.
    """
    from meridian.db import normalize_resource_id  # noqa: PLC0415
    normalized = normalize_resource_id(resource_id)
    await expire_resource_locks(db)
    async with db.execute(
        "SELECT rl.*, s.name AS session_name FROM resource_locks rl "
        "LEFT JOIN sessions s ON s.id = rl.session_id "
        "WHERE rl.resource_id = ?",
        (normalized,),
    ) as cur:
        row = await cur.fetchone()
    return {
        "resource_id": normalized,
        "resource_lock": _row_to_dict(row),
    }


async def get_resource_conflicts(
    db: aiosqlite.Connection,
    project_id: str,
    resources: list[str],
    *,
    exclude_session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return active resource locks (held by other live sessions) overlapping ``resources``.

    Used for pre-claim / pre-fanout conflict detection: given the resource ids a
    unit of work wants to touch, surface any that another still-live session in
    the project already holds. Stale locks are expired first.
    """
    from meridian.db import normalize_resource_id  # noqa: PLC0415
    wanted: set[str] = set()
    for r in resources or []:
        try:
            wanted.add(normalize_resource_id(r))
        except ValueError:
            continue
    if not wanted:
        return []
    await expire_resource_locks(db)
    params: list[Any] = [project_id]
    exclude_clause = ""
    if exclude_session_id:
        exclude_clause = "AND rl.session_id != ? "
        params.append(exclude_session_id)
    async with db.execute(
        "SELECT rl.resource_id, rl.resource_type, rl.session_id, "
        "       s.name AS session_name, s.last_seen "
        "FROM resource_locks rl "
        "JOIN sessions s ON s.id = rl.session_id "
        "WHERE s.project_id = ? "
        f"{exclude_clause}"
        "AND s.status IN ('active', 'live') "
        "AND (s.last_seen IS NULL OR s.last_seen > datetime('now', '-10 minutes'))",
        tuple(params),
    ) as cur:
        rows = await cur.fetchall()
    conflicts: list[dict[str, Any]] = []
    for row in rows:
        r = _row_to_dict(row) or {}
        rid = str(r.get("resource_id") or "")
        if rid not in wanted:
            continue
        conflicts.append({
            "resource_id": rid,
            "resource_type": r.get("resource_type"),
            "session_id": r.get("session_id"),
            "session_name": r.get("session_name"),
            "last_seen": r.get("last_seen"),
        })
    return conflicts


# ---------------------------------------------------------------------------
# Symbol-level parallel protection (4bac57ff)
# ---------------------------------------------------------------------------


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Inclusive line-range overlap test."""
    return a_start <= b_end and b_start <= a_end


async def _live_symbol_claims_for_file(
    db: aiosqlite.Connection,
    file_path: str,
    exclude_session_id: str,
) -> list[dict[str, Any]]:
    """Symbol claims on ``file_path`` held by *other* still-live sessions.

    39544099 — uses _CLAIM_LIVE_HOURS (unified with file_locks TTL) as the
    staleness cutoff instead of a hardcoded 10-minute window, so both expiry
    mechanisms share the same constant. A crashed session's claims time out after
    _CLAIM_LIVE_HOURS just like a whole-file lock does.
    """
    cutoff = _cutoff_dt(_CLAIM_LIVE_HOURS)
    async with db.execute(
        "SELECT fsc.symbol_name, fsc.symbol_type, fsc.line_start, fsc.line_end, "
        "       fsc.session_id, s.name AS session_name "
        "FROM file_symbol_claims fsc "
        "JOIN sessions s ON s.id = fsc.session_id "
        "WHERE fsc.file_path = ? AND fsc.session_id != ? "
        "AND fsc.released_at IS NULL "
        "AND s.status IN ('active', 'live') "
        "AND (s.last_seen IS NULL OR s.last_seen > ?)",
        (file_path, exclude_session_id, cutoff),
    ) as cur:
        rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r]


async def claim_symbol(
    db: aiosqlite.Connection,
    session_id: str,
    file_path: str,
    symbol: str,
    content: str,
) -> dict[str, Any]:
    """Claim a single class/function/method by line range within a file.

    Parses ``content`` to locate ``symbol``'s line span, then hard-blocks if any
    *other* live session already claims an overlapping span. On a block it
    returns the conflicting claims plus ``safe_to_claim`` — symbols in the file
    whose ranges are free — so the caller can immediately pick a non-colliding
    symbol. Returns ``reason='unparseable'`` (unsupported/syntax-error/missing
    grammar) so callers can fall back to whole-file ``claim_file``.
    """
    from ..symbols import extract_symbols

    normalized = (file_path or "").strip()
    symbol = (symbol or "").strip()
    if not normalized:
        raise ValueError("file_path is required")
    if not symbol:
        raise ValueError("symbol is required")

    # 63b030a6 — file ⊃ symbol hierarchy: if another live session holds a
    # WHOLE-FILE lock on this file, no symbol-level claim is allowed (the file
    # owner may touch any symbol). Mirror the inverse block in claim_file.
    await expire_file_locks(db)
    async with db.execute(
        "SELECT session_id, claimed_at, expires_at FROM file_locks WHERE file_path = ?",
        (_normalize_file_path(normalized),),
    ) as cur:
        _fl_row = await cur.fetchone()
    _fl = _row_to_dict(_fl_row)
    if _fl and _fl.get("session_id") and _fl.get("session_id") != session_id:
        return {
            "claimed": False,
            "reason": "file_locked",
            "file_path": normalized,
            "holder_session_id": _fl.get("session_id"),
            "message": (
                f"Cannot claim symbol in {normalized}: another live session holds a "
                "whole-file lock on it. Wait for it to release, or coordinate."
            ),
        }

    symbols = extract_symbols(normalized, content or "")
    if not symbols:
        return {
            "claimed": False,
            "reason": "unparseable",
            "file_path": normalized,
            "message": (
                f"Could not extract symbols from {normalized} "
                "(unsupported language, syntax error, or missing grammar). "
                "Use whole-file claim_file instead."
            ),
        }

    target = next((s for s in symbols if s["name"] == symbol), None)
    if target is None:
        return {
            "claimed": False,
            "reason": "symbol_not_found",
            "file_path": normalized,
            "available_symbols": [s["name"] for s in symbols],
            "message": (
                f"Symbol '{symbol}' not found in {normalized}. "
                f"Available: {', '.join(s['name'] for s in symbols) or '(none)'}"
            ),
        }

    others = await _live_symbol_claims_for_file(db, normalized, session_id)
    conflicts = [
        c for c in others
        if _ranges_overlap(target["line_start"], target["line_end"], c["line_start"], c["line_end"])
    ]
    if conflicts:
        claimed_ranges = [(c["line_start"], c["line_end"]) for c in others]
        safe = [
            s["name"] for s in symbols
            if s["name"] != symbol
            and not any(_ranges_overlap(s["line_start"], s["line_end"], cs, ce) for cs, ce in claimed_ranges)
        ]
        holder = conflicts[0]
        holder_name = holder.get("session_name") or (holder.get("session_id") or "unknown")[:8]
        safe_hint = f" — you can safely claim {', '.join(safe)}" if safe else " — no other symbols are free"
        return {
            "claimed": False,
            "reason": "symbol_conflict",
            "file_path": normalized,
            "symbol": symbol,
            "conflicts": [
                {
                    "symbol_name": c["symbol_name"],
                    "line_start": c["line_start"],
                    "line_end": c["line_end"],
                    "holder_session_id": c["session_id"],
                    "holder_session_name": c.get("session_name"),
                }
                for c in conflicts
            ],
            "safe_to_claim": safe,
            "message": (
                f"⚠️ {conflicts[0]['symbol_name']} "
                f"(lines {conflicts[0]['line_start']}-{conflicts[0]['line_end']}) "
                f"claimed by session {holder_name}{safe_hint}"
            ),
        }

    # No conflict — (re)claim this symbol for the session (idempotent per symbol).
    # Drop any prior row for this exact (session, file, symbol), active or
    # released, so a re-claim is a single fresh active row.
    await db.execute(
        "DELETE FROM file_symbol_claims WHERE session_id = ? AND file_path = ? AND symbol_name = ?",
        (session_id, normalized, symbol),
    )
    await db.execute(
        "INSERT INTO file_symbol_claims "
        "(id, session_id, file_path, symbol_name, symbol_type, line_start, line_end) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_new_id(), session_id, normalized, symbol, target["type"],
         target["line_start"], target["line_end"]),
    )
    await db.commit()
    # 2593a5fe — amend the active sprint item's touches_resources if this symbol
    # was not in the original declaration (mid-execution pivot detection).
    # Use "symbol:<path>::<symbol>" as resource id for symbol-level precision.
    _sym_resource_hint = await _amend_sprint_item_resources_for_session(
        db, session_id, f"symbol:{normalized}::{symbol}"
    )
    sym_result: dict[str, Any] = {
        "claimed": True,
        "file_path": normalized,
        "session_id": session_id,
        "symbol": symbol,
        "symbol_type": target["type"],
        "line_start": target["line_start"],
        "line_end": target["line_end"],
    }
    if _sym_resource_hint and _sym_resource_hint.get("wave_assignment_hint"):
        sym_result["wave_assignment_hint"] = _sym_resource_hint["wave_assignment_hint"]
    return sym_result


async def get_symbol_claims(
    db: aiosqlite.Connection,
    file_path: str,
) -> list[dict[str, Any]]:
    """Return active symbol claims on a file (released_at IS NULL), newest first."""
    async with db.execute(
        "SELECT fsc.*, s.name AS session_name FROM file_symbol_claims fsc "
        "LEFT JOIN sessions s ON s.id = fsc.session_id "
        "WHERE fsc.file_path = ? AND fsc.released_at IS NULL "
        "ORDER BY fsc.claimed_at DESC",
        ((file_path or "").strip(),),
    ) as cur:
        rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r]


async def release_symbol_claims_for_session(
    db: aiosqlite.Connection,
    session_id: str,
    file_path: str | None = None,
) -> int:
    """Soft-release a session's active symbol claims (all, or just one file).

    Sets released_at instead of deleting so hotspot scoring retains the history.
    Returns the number of claims released.
    """
    if file_path:
        cur = await db.execute(
            "UPDATE file_symbol_claims SET released_at = datetime('now') "
            "WHERE session_id = ? AND file_path = ? AND released_at IS NULL",
            (session_id, (file_path or "").strip()),
        )
    else:
        cur = await db.execute(
            "UPDATE file_symbol_claims SET released_at = datetime('now') "
            "WHERE session_id = ? AND released_at IS NULL",
            (session_id,),
        )
    await db.commit()
    return cur.rowcount


async def get_symbol_hotspots(
    db: aiosqlite.Connection,
    file_path: str | None = None,
    *,
    min_sessions: int = 3,
    days: int = 14,
) -> list[dict[str, Any]]:
    """Symbols claimed by ``min_sessions``+ distinct sessions within ``days``.

    A hotspot is a symbol many sessions keep touching — a refactor/ownership
    smell. Computed over recent rows in file_symbol_claims (active + not-yet-
    released claims within the window).
    """
    params: list[Any] = [f"-{max(0, int(days))} days"]
    where = "WHERE claimed_at > datetime('now', ?)"
    if file_path:
        where += " AND file_path = ?"
        params.append((file_path or "").strip())
    sql = (
        "SELECT file_path, symbol_name, symbol_type, "
        "COUNT(DISTINCT session_id) AS session_count "
        f"FROM file_symbol_claims {where} "
        "GROUP BY file_path, symbol_name, symbol_type "
        "HAVING COUNT(DISTINCT session_id) >= ? "
        "ORDER BY session_count DESC, file_path"
    )
    params.append(int(min_sessions))
    async with db.execute(sql, tuple(params)) as cur:
        rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r]


async def get_hotspot_suggestions(
    db: aiosqlite.Connection,
    *,
    min_sessions: int = 5,
    days: int = 30,
) -> list[dict[str, Any]]:
    """Return sprint item suggestions based on file-level contention hotspots.

    1b4760a9 — files touched by min_sessions+ distinct sessions within days are
    likely candidates for refactoring, clearer ownership, or better test coverage.
    Returns dicts with: file_path, session_count, suggestion (human-readable
    recommendation text). Computed over file_symbol_claims grouped by file_path
    (not symbol), so a heavily-edited file surfaces even if individual symbols
    each have low session counts.
    """
    params: list[Any] = [f"-{max(0, int(days))} days"]
    sql = (
        "SELECT file_path, COUNT(DISTINCT session_id) AS session_count "
        "FROM file_symbol_claims "
        "WHERE claimed_at > datetime('now', ?) "
        "GROUP BY file_path "
        "HAVING COUNT(DISTINCT session_id) >= ? "
        "ORDER BY session_count DESC, file_path"
    )
    params.append(int(min_sessions))
    async with db.execute(sql, tuple(params)) as cur:
        rows = await cur.fetchall()
    suggestions = []
    for row in rows:
        r = _row_to_dict(row)
        if not r:
            continue
        fp = r.get("file_path", "")
        sc = r.get("session_count", 0)
        suggestions.append({
            "file_path": fp,
            "session_count": sc,
            "suggestion": (
                f"Refactor or add ownership docs for {fp} — "
                f"touched by {sc} distinct sessions in the last {days} days"
            ),
        })
    return suggestions


# ---------------------------------------------------------------------------
# get_session_file_claims — convenience view for session briefing
# ---------------------------------------------------------------------------

async def get_session_file_claims(
    db: aiosqlite.Connection, session_id: str
) -> list[str]:
    """Return the file paths a session currently holds an active lock on.

    1750dccf — used by get_session_brief(role='executor') to remind an executor
    what it has claimed. Stale locks are expired first so the list is live.
    """
    await expire_file_locks(db)
    async with db.execute(
        "SELECT file_path FROM file_locks WHERE session_id = ? ORDER BY file_path",
        (session_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [r["file_path"] for r in rows if r is not None]


# ---------------------------------------------------------------------------
# 356d6ac8 — structural-degradation patch-counter helpers
# ---------------------------------------------------------------------------


async def _increment_file_patch_counter(
    db: aiosqlite.Connection,
    session_id: str,
    file_path: str,
) -> None:
    """Increment the write-claim count for (session_id, file_path).

    356d6ac8 — called on every successful exclusive write claim. Uses
    INSERT OR IGNORE + UPDATE so the upsert is a single pair of statements
    (SQLite supports ON CONFLICT DO UPDATE but the two-statement form is
    cleaner to read and works on both SQLite and Postgres without dialect
    gymnastics). Best-effort: a DB error is silently swallowed so a counter
    glitch never blocks a file claim.
    """
    try:
        await db.execute(
            "INSERT OR IGNORE INTO file_patch_counters "
            "(id, session_id, file_path, patch_count, refactor_flagged, "
            "first_patched_at, last_patched_at) "
            "VALUES (?, ?, ?, 0, 0, datetime('now'), datetime('now'))",
            (_new_id(), session_id, file_path),
        )
        await db.execute(
            "UPDATE file_patch_counters "
            "SET patch_count = patch_count + 1, last_patched_at = datetime('now') "
            "WHERE session_id = ? AND file_path = ?",
            (session_id, file_path),
        )
        await db.commit()
    except Exception:  # noqa: BLE001 — never wedge a claim on a counter error
        pass


async def get_structural_degradation_warnings(
    db: aiosqlite.Connection,
    session_id: str,
    *,
    threshold: int = _PATCH_DEGRADATION_THRESHOLD,
) -> list[dict[str, Any]]:
    """Return files flagged as structural-degradation risks for a session.

    356d6ac8 — a file is flagged when its write-claim count within the session
    is >= ``threshold`` AND refactor_flagged is 0 (the executor has not
    signalled a deliberate refactor of this file). The heuristic: many small
    patches on the same file within one session, without a refactor checkpoint,
    is the documented AI-agent symptom-chasing pattern.

    Returns a list of dicts::

        {
            "file_path": str,
            "patch_count": int,
            "refactor_flagged": bool,
            "first_patched_at": str,
            "last_patched_at": str,
            "warning": str,           # human-readable explanation
        }

    An empty list means no degradation signals detected. The caller can use
    this in generate_handoff, analyze_sprint, or any pre-commit gate.
    """
    async with db.execute(
        "SELECT file_path, patch_count, refactor_flagged, "
        "first_patched_at, last_patched_at "
        "FROM file_patch_counters "
        "WHERE session_id = ? AND patch_count >= ? AND refactor_flagged = 0 "
        "ORDER BY patch_count DESC, last_patched_at DESC",
        (session_id, int(threshold)),
    ) as cur:
        rows = await cur.fetchall()
    results = []
    for row in rows:
        r = _row_to_dict(row)
        if not r:
            continue
        count = int(r.get("patch_count") or 0)
        results.append({
            "file_path": r.get("file_path", ""),
            "patch_count": count,
            "refactor_flagged": bool(r.get("refactor_flagged")),
            "first_patched_at": r.get("first_patched_at"),
            "last_patched_at": r.get("last_patched_at"),
            "warning": (
                f"Structural-degradation risk: {r.get('file_path', '')} has been "
                f"write-claimed {count} times this session without a refactor checkpoint. "
                "This may indicate symptom-patching rather than root-cause fixes. "
                "Consider a deliberate refactor pass, or call flag_file_refactor to "
                "acknowledge this is intentional incremental work."
            ),
        })
    return results


async def flag_file_refactor(
    db: aiosqlite.Connection,
    session_id: str,
    file_path: str,
) -> dict[str, Any]:
    """Signal that this session intentionally refactored ``file_path``.

    356d6ac8 — sets refactor_flagged=1 on the (session_id, file_path) counter
    row so get_structural_degradation_warnings no longer surfaces it as a risk.
    A refactor flag means: "the repeated writes on this file are deliberate
    structural work, not symptom-patching."

    Creates the row if absent (idempotent). Returns the updated counter row.
    """
    normalized = (file_path or "").strip()
    if not normalized:
        raise ValueError("file_path is required")
    await db.execute(
        "INSERT OR IGNORE INTO file_patch_counters "
        "(id, session_id, file_path, patch_count, refactor_flagged, "
        "first_patched_at, last_patched_at) "
        "VALUES (?, ?, ?, 0, 1, datetime('now'), datetime('now'))",
        (_new_id(), session_id, normalized),
    )
    await db.execute(
        "UPDATE file_patch_counters SET refactor_flagged = 1 "
        "WHERE session_id = ? AND file_path = ?",
        (session_id, normalized),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM file_patch_counters WHERE session_id = ? AND file_path = ?",
        (session_id, normalized),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {
        "session_id": session_id,
        "file_path": normalized,
        "refactor_flagged": True,
    }


# ---------------------------------------------------------------------------
# f7ee1ba7 — Model B: scoped docx-region claims
#
# A .docx is a zip container with no partial write; every mutating tool
# re-saves the whole file (last-save-wins). ``file_locks`` (whole-file) and
# ``file_symbol_claims`` (code line-ranges) already guard code files at two
# granularities. This extends the same pattern to DOCX documents:
#
# * A session may claim a specific paragraph/element by its durable ``para_id``
#   (the ``w14:paraId`` the OOXML layer already surfaces — the same id used by
#   ``update_paragraph`` and ``get_document_structure``).
# * Two sessions can hold NON-OVERLAPPING element claims on the same file
#   concurrently — the precision benefit mirroring symbol claims for code.
# * An edit attempt OUTSIDE the caller's claimed element is REJECTED before
#   touching the filesystem — structural prevention, not advice.
# * A whole-file (unscoped) claim still works as before; scoped claims
#   compose with it (a whole-file lock blocks all scoped writers, exactly as
#   file_locks blocks symbol claims).
# ---------------------------------------------------------------------------

async def _migrate_docx_region_claims(db: aiosqlite.Connection) -> None:
    """f7ee1ba7 — create file_docx_region_claims table if it doesn't exist.

    Guarded migration (no inline CREATE INDEX in CREATE_TABLES — 2026-07-04
    outage rule): the table and its indexes are created here, so existing DBs
    pick them up on first startup after the deploy.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS file_docx_region_claims (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            file_path TEXT NOT NULL,
            element_id TEXT NOT NULL,
            claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
            released_at TEXT
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_docx_region_claims_file "
        "ON file_docx_region_claims (file_path)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_docx_region_claims_session "
        "ON file_docx_region_claims (session_id)"
    )
    await db.commit()


async def _live_docx_region_claims_for_file(
    db: aiosqlite.Connection,
    file_path: str,
    exclude_session_id: str,
) -> list[dict[str, Any]]:
    """Active scoped docx-region claims on ``file_path`` held by OTHER sessions.

    Uses the same _CLAIM_LIVE_HOURS staleness cutoff as file_symbol_claims so
    a crashed session's element claims time out consistently.
    """
    cutoff = _cutoff_dt(_CLAIM_LIVE_HOURS)
    async with db.execute(
        "SELECT drc.id, drc.session_id, drc.file_path, drc.element_id, "
        "       drc.claimed_at, s.name AS session_name "
        "FROM file_docx_region_claims drc "
        "JOIN sessions s ON s.id = drc.session_id "
        "WHERE drc.file_path = ? AND drc.session_id != ? "
        "AND drc.released_at IS NULL "
        "AND s.status IN ('active', 'live') "
        "AND (s.last_seen IS NULL OR s.last_seen > ?)",
        (file_path, exclude_session_id, cutoff),
    ) as cur:
        rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r]


async def claim_docx_region(
    db: aiosqlite.Connection,
    session_id: str,
    file_path: str,
    element_id: str,
) -> dict[str, Any]:
    """Claim a specific paragraph/element of a .docx for exclusive editing.

    f7ee1ba7 — Model B scoped-region claiming. ``element_id`` is the durable
    ``w14:paraId`` (or ``p{index}`` fallback) surfaced by
    ``get_document_structure`` / ``update_paragraph`` — the same stable id that
    round-trips through the OOXML layer.

    Conflict rules (mirror file_symbol_claims / claim_symbol):

    * A whole-file write lock on the document by ANOTHER session blocks this
      claim (the file owner may touch any element).
    * Another session's scoped claim on the SAME ``element_id`` blocks (hard
      conflict — two writers would race on the same paragraph).
    * Another session's claim on a DIFFERENT element is allowed (the real
      precision benefit: parallel editing of non-overlapping regions).
    * The caller's own prior claim on this element is refreshed (idempotent).

    Returns ``{"claimed": True, ...}`` on success, ``{"claimed": False,
    "reason": ..., ...}`` on conflict, never raises.
    """
    normalized = _normalize_file_path(file_path)
    if not normalized:
        return {"claimed": False, "reason": "invalid", "message": "file_path is required"}
    elem = (element_id or "").strip()
    if not elem:
        return {"claimed": False, "reason": "invalid", "message": "element_id is required"}

    # Check for a whole-file write lock held by another session.
    await expire_file_locks(db)
    async with db.execute(
        "SELECT session_id, claimed_at, expires_at FROM file_locks WHERE file_path = ?",
        (normalized,),
    ) as cur:
        _fl_row = await cur.fetchone()
    _fl = _row_to_dict(_fl_row)
    if _fl and _fl.get("session_id") and _fl.get("session_id") != session_id:
        return {
            "claimed": False,
            "reason": "file_locked",
            "file_path": normalized,
            "element_id": elem,
            "holder_session_id": _fl.get("session_id"),
            "message": (
                f"Cannot claim element {elem!r} in {normalized}: another live session "
                "holds a whole-file lock on it. Wait for it to release, or coordinate."
            ),
        }

    # Check for another session's claim on the same element_id.
    others = await _live_docx_region_claims_for_file(db, normalized, session_id)
    conflicts = [c for c in others if c.get("element_id") == elem]
    if conflicts:
        holder = conflicts[0]
        safe_elements = list({c["element_id"] for c in others if c["element_id"] != elem})
        holder_name = holder.get("session_name") or (holder.get("session_id") or "unknown")[:8]
        return {
            "claimed": False,
            "reason": "element_conflict",
            "file_path": normalized,
            "element_id": elem,
            "conflicts": [
                {
                    "element_id": c["element_id"],
                    "holder_session_id": c["session_id"],
                    "holder_session_name": c.get("session_name"),
                }
                for c in conflicts
            ],
            "other_claimed_elements": safe_elements,
            "message": (
                f"Element {elem!r} in {normalized} is already claimed by session "
                f"{holder_name}. Pick a different element or wait for the claim to release."
            ),
        }

    # No conflict — (re)claim this element (idempotent per session+file+element).
    await db.execute(
        "DELETE FROM file_docx_region_claims "
        "WHERE session_id = ? AND file_path = ? AND element_id = ?",
        (session_id, normalized, elem),
    )
    await db.execute(
        "INSERT INTO file_docx_region_claims (id, session_id, file_path, element_id) "
        "VALUES (?, ?, ?, ?)",
        (_new_id(), session_id, normalized, elem),
    )
    await db.commit()
    return {
        "claimed": True,
        "file_path": normalized,
        "session_id": session_id,
        "element_id": elem,
    }


async def get_docx_region_claims(
    db: aiosqlite.Connection,
    file_path: str,
) -> list[dict[str, Any]]:
    """Return active scoped docx-region claims on ``file_path`` (released_at IS NULL).

    f7ee1ba7 — read-only; newest first. Expired claims (sessions gone stale)
    are NOT pruned here — read-only so it never writes. Use claim_docx_region
    to auto-expire on the write path.
    """
    normalized = _normalize_file_path(file_path)
    async with db.execute(
        "SELECT drc.*, s.name AS session_name "
        "FROM file_docx_region_claims drc "
        "LEFT JOIN sessions s ON s.id = drc.session_id "
        "WHERE drc.file_path = ? AND drc.released_at IS NULL "
        "ORDER BY drc.claimed_at DESC",
        (normalized,),
    ) as cur:
        rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r]


async def release_docx_region_claims(
    db: aiosqlite.Connection,
    session_id: str,
    file_path: str | None = None,
    element_id: str | None = None,
) -> int:
    """Soft-release scoped docx-region claims held by ``session_id``.

    f7ee1ba7 — sets released_at (never deletes) so history is retained.
    Scoping: all claims for the session (no args), all claims on one file
    (file_path), or a single element claim (file_path + element_id).
    Returns the number of claims released.
    """
    if file_path and element_id:
        normalized = _normalize_file_path(file_path)
        cur = await db.execute(
            "UPDATE file_docx_region_claims SET released_at = datetime('now') "
            "WHERE session_id = ? AND file_path = ? AND element_id = ? "
            "AND released_at IS NULL",
            (session_id, normalized, element_id.strip()),
        )
    elif file_path:
        normalized = _normalize_file_path(file_path)
        cur = await db.execute(
            "UPDATE file_docx_region_claims SET released_at = datetime('now') "
            "WHERE session_id = ? AND file_path = ? AND released_at IS NULL",
            (session_id, normalized),
        )
    else:
        cur = await db.execute(
            "UPDATE file_docx_region_claims SET released_at = datetime('now') "
            "WHERE session_id = ? AND released_at IS NULL",
            (session_id,),
        )
    await db.commit()
    return cur.rowcount


async def check_docx_region_write_conflict(
    db: "Any",
    session_id: str | None,
    file_path: str,
    element_id: str | None,
) -> "dict[str, Any] | None":
    """Scoped-claim enforcement gate for docx writes (f7ee1ba7 Model B).

    Called by ``update_paragraph`` before writing a paragraph into a .docx.
    Returns a conflict dict ``{"blocked": True, "reason": ..., "message": ...}``
    when the write must be rejected, else ``None`` (clear — proceed).

    Rules (strictly enforced, not advisory):

    1. Whole-file lock: if another session holds a whole-file write lock on
       ``file_path``, BLOCK (regardless of element_id). The file owner controls
       every element.
    2. Scoped-claim enforcement: if ANY session holds a scoped claim on
       ``file_path`` AND ``session_id`` is NOT the holder AND ``element_id``
       is NOT in the caller's own scoped claims for this file → BLOCK.
       - If the write is for an element another session has claimed → BLOCK
         (element owned by someone else).
       - If the write is for an element the caller DOES own → ALLOW (owner
         writes their own region).
       - If no scoped claims exist at all → ALLOW (unscoped/unclaimed).
    3. Fail-open: a DB error, missing db, or unidentifiable element degrades
       to None (no block). The gate surfaces conflicts; claim_docx_region is
       the real primitive.
    """
    if db is None:
        return None
    normalized = _normalize_file_path(file_path)
    if not normalized:
        return None
    _sid = (session_id or "").strip()
    _elem = (element_id or "").strip()

    try:
        # Rule 1: whole-file write lock by another session.
        await expire_file_locks(db)
        async with db.execute(
            "SELECT session_id FROM file_locks WHERE file_path = ?",
            (normalized,),
        ) as cur:
            fl_row = await cur.fetchone()
        fl = _row_to_dict(fl_row)
        if fl and fl.get("session_id") and fl.get("session_id") != _sid:
            holder = fl["session_id"]
            return {
                "blocked": True,
                "reason": "file_locked",
                "file_path": normalized,
                "element_id": _elem or None,
                "holder": holder,
                "message": (
                    f"Cannot write element {_elem!r} in {normalized}: session {holder} "
                    "holds a whole-file write lock. Wait for it to release or coordinate."
                ),
            }

        # Rule 2: scoped-claim enforcement.
        # Any scoped claim on this file means the file is in "region-partitioned"
        # mode — edits without owning the target element are rejected.
        all_claims = await get_docx_region_claims(db, normalized)
        if not all_claims:
            return None  # No scoped claims — unguarded, allow.

        # Check whether the caller owns the target element.
        if _elem:
            caller_owns = any(
                c.get("session_id") == _sid and c.get("element_id") == _elem
                for c in all_claims
            )
            if caller_owns:
                return None  # Caller owns this element — allow.

            # Check if another session claims THIS element.
            other_owns_target = any(
                c.get("session_id") != _sid and c.get("element_id") == _elem
                for c in all_claims
            )
            if other_owns_target:
                holder_row = next(
                    c for c in all_claims
                    if c.get("session_id") != _sid and c.get("element_id") == _elem
                )
                holder = holder_row.get("session_id", "unknown")
                return {
                    "blocked": True,
                    "reason": "element_locked",
                    "file_path": normalized,
                    "element_id": _elem,
                    "holder": holder,
                    "message": (
                        f"Element {_elem!r} in {normalized} is claimed by session "
                        f"{holder}. You do not own this element. Use claim_docx_region "
                        "to acquire a non-conflicting element, or wait for the claim to release."
                    ),
                }

            # The file is in scoped mode but the target element is unclaimed by
            # anyone. Editing an unclaimed element while others are scoped is
            # still risky (last-save-wins), but NOT a conflict per the Model-B
            # spec — block only when a claimed element would be overwritten.
            return None

        # No element_id provided — the caller is trying a whole-element or
        # unscoped write on a file that has scoped claims. Block if the caller
        # has no claims themselves (they should use a scoped claim or wait).
        caller_has_any = any(c.get("session_id") == _sid for c in all_claims)
        if not caller_has_any:
            other_sessions = list({c.get("session_id") for c in all_claims if c.get("session_id") != _sid})
            return {
                "blocked": True,
                "reason": "scoped_mode",
                "file_path": normalized,
                "element_id": None,
                "holder": other_sessions[0] if other_sessions else "unknown",
                "message": (
                    f"{normalized} is in scoped-edit mode: {len(other_sessions)} other "
                    "session(s) hold region claims on it. Provide an element_id and "
                    "use claim_docx_region to acquire your region before writing."
                ),
            }

    except Exception:  # noqa: BLE001 — never wedge a write on a guard error
        return None

    return None
