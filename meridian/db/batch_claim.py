"""22cad9b8 — immutable batch-claim manifests for atomic parallel sprint-item claims.

``get_parallelizable_groups`` (meridian/db/sprint_items.py) can tell an
orchestrator "these N items are safe to run in parallel because their
declared ``touches_resources`` are disjoint" — but nothing atomically
RESERVES an entire selected batch's resources before workers actually
launch. Between "compute the safe batch" and "each worker claims its item,"
another session could sneak in and claim one of those same resources, or the
batch composition itself could go stale.

This module is the durable, auditable half of the fix — it mirrors
eb2e44f8's immutable wave-base-manifest pattern (``meridian/db/
worktree_manifest.py``) exactly: persist once, supersede explicitly (never
silently overwrite). The atomic claim ATTEMPT itself (acquiring every item's
status transition and every declared resource, all-or-nothing, with full
rollback on any conflict) lives in :func:`meridian.db.sprint_items.
claim_parallel_batch`, which calls into this module to record the batch
before attempting anything and to stamp the outcome afterward.

Immutability contract: :func:`persist_batch_claim_manifest` refuses to
create a second ACTIVE manifest for the same ``(project_id, batch_key)`` — a
second call without ``force=True`` raises ``ValueError``. ``batch_key`` is a
deterministic function of the (deduped, sorted) item id set, so re-submitting
the exact same batch of items is what collides; different batches (even ones
that share SOME items) get distinct keys and never collide. Passing
``force=True`` performs an explicit, AUDITED replacement: the prior row is
marked ``superseded_at``/``superseded_reason`` (never deleted) and a fresh
row becomes the new active manifest. Enforced twice, exactly like
worktree_manifest.py — once in Python (check-then-act above) and once at the
schema level via a partial unique index (``idx_sprint_batch_claims_active``,
``WHERE superseded_at IS NULL``) so even a caller that bypasses this
module's Python API cannot silently create two simultaneously-active
manifests for the same batch.
"""
from __future__ import annotations

import json
from typing import Any

import aiosqlite

from meridian.db import _new_id, _row_to_dict  # noqa: PLC0415

_VALID_BATCH_CLAIM_STATUSES = ("pending", "claimed", "failed")


async def _migrate_sprint_batch_claims(db: aiosqlite.Connection) -> None:
    """22cad9b8 — create ``sprint_batch_claims`` on existing SQLite DBs.

    Guarded migration (no inline CREATE INDEX in the unguarded base schema
    literals — 2026-07-04 outage rule): the table + its indexes are created
    here so existing DBs pick them up on the first startup after deploy.
    Mirrors ``worktree_manifest._migrate_wave_base_manifests`` exactly.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS sprint_batch_claims (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            session_id TEXT NOT NULL,
            batch_key TEXT NOT NULL,
            item_ids TEXT NOT NULL,
            item_resource_map TEXT NOT NULL,
            resources TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            failure_detail TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT,
            superseded_at TEXT,
            superseded_reason TEXT
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_sprint_batch_claims_project "
        "ON sprint_batch_claims(project_id)"
    )
    # Partial unique index — the schema-level half of the immutability
    # contract described in the module docstring. Only one row per
    # (project_id, batch_key) may have superseded_at IS NULL at a time.
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sprint_batch_claims_active "
        "ON sprint_batch_claims(project_id, batch_key) WHERE superseded_at IS NULL"
    )
    await db.commit()


def compute_batch_key(item_ids: list[str]) -> str:
    """Deterministic identity for a batch: sorted, deduped, comma-joined ids.

    Two calls that name the exact same SET of item ids (regardless of input
    order or duplicates) collapse to the same key, so the immutability
    contract above applies to "this exact item set", not "this exact call".
    """
    return ",".join(sorted({str(i) for i in item_ids if i}))


def _decode_manifest_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for field, default in (
        ("item_ids", []), ("item_resource_map", {}), ("resources", []),
    ):
        raw = out.get(field)
        if isinstance(raw, str):
            try:
                out[field] = json.loads(raw)
            except (ValueError, TypeError):
                out[field] = default
        elif raw is None:
            out[field] = default
    # failure_detail is nullable (only set once the attempt resolves to
    # 'failed') — decode when present, leave None untouched.
    raw_failure = out.get("failure_detail")
    if isinstance(raw_failure, str):
        try:
            out["failure_detail"] = json.loads(raw_failure)
        except (ValueError, TypeError):
            pass
    return out


async def persist_batch_claim_manifest(
    db: aiosqlite.Connection,
    project_id: str,
    session_id: str,
    item_ids: list[str],
    item_resource_map: dict[str, list[str]],
    resources: list[str],
    *,
    force: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """Persist the immutable "what batch was decided" manifest.

    Raises ``ValueError`` if an active (non-superseded) manifest already
    exists for this exact item-id set in this project and ``force`` is not
    True — see the module docstring for the immutability contract this
    enforces. When ``force=True`` is passed deliberately, the existing
    manifest is marked superseded (with ``reason``, defaulting to a generic
    note) rather than deleted, so the replacement is auditable via
    :func:`get_batch_claim_manifest_history`.

    Recorded with ``status='pending'`` — the caller (``claim_parallel_batch``)
    stamps the real outcome via :func:`mark_batch_claim_outcome` once the
    actual atomic claim attempt resolves (claimed or failed). This means the
    manifest is a durable record of what was DECIDED even when the
    subsequent attempt fails — the audit trail this item's acceptance
    criteria calls for.
    """
    batch_key = compute_batch_key(item_ids)
    existing = await get_batch_claim_manifest(db, project_id, batch_key)
    if existing is not None:
        if not force:
            raise ValueError(
                f"an active batch-claim manifest already exists for this "
                f"item set in project {project_id!r} (id={existing['id']}, "
                f"batch_key={batch_key!r}); pass force=True with a reason "
                "to explicitly supersede it instead of silently overwriting."
            )
        await db.execute(
            "UPDATE sprint_batch_claims SET superseded_at = datetime('now'), "
            "superseded_reason = ? WHERE id = ?",
            (reason or "force-replaced", existing["id"]),
        )
    mid = _new_id()
    await db.execute(
        "INSERT INTO sprint_batch_claims "
        "(id, project_id, session_id, batch_key, item_ids, item_resource_map, "
        "resources, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
        (
            mid, project_id, session_id, batch_key,
            json.dumps(list(item_ids)),
            json.dumps({k: list(v) for k, v in item_resource_map.items()}),
            json.dumps(list(resources)),
        ),
    )
    await db.commit()
    result = await get_batch_claim_manifest_by_id(db, mid)
    assert result is not None  # just inserted; must be findable
    return result


async def get_batch_claim_manifest(
    db: aiosqlite.Connection,
    project_id: str,
    batch_key: str,
) -> dict[str, Any] | None:
    """Return the ACTIVE (non-superseded) manifest for a batch key, or None."""
    async with db.execute(
        "SELECT * FROM sprint_batch_claims "
        "WHERE project_id = ? AND batch_key = ? AND superseded_at IS NULL",
        (project_id, batch_key),
    ) as cur:
        row = await cur.fetchone()
    return _decode_manifest_row(_row_to_dict(row))


async def get_batch_claim_manifest_by_id(
    db: aiosqlite.Connection,
    manifest_id: str,
) -> dict[str, Any] | None:
    """Return a manifest by its own id, active or superseded."""
    async with db.execute(
        "SELECT * FROM sprint_batch_claims WHERE id = ?",
        (manifest_id,),
    ) as cur:
        row = await cur.fetchone()
    return _decode_manifest_row(_row_to_dict(row))


async def get_batch_claim_manifest_history(
    db: aiosqlite.Connection,
    project_id: str,
    batch_key: str,
) -> list[dict[str, Any]]:
    """Every manifest row (including superseded ones) for a batch key, newest
    first — the audit trail behind an explicit ``force=True`` replacement."""
    async with db.execute(
        "SELECT * FROM sprint_batch_claims WHERE project_id = ? AND batch_key = ? "
        "ORDER BY created_at DESC",
        (project_id, batch_key),
    ) as cur:
        rows = await cur.fetchall()
    return [_decode_manifest_row(_row_to_dict(r)) for r in rows if r is not None]  # type: ignore[misc]


async def mark_batch_claim_outcome(
    db: aiosqlite.Connection,
    manifest_id: str,
    status: str,
    *,
    failure_detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stamp the resolved outcome (``'claimed'`` or ``'failed'``) on a manifest
    this SAME call's :func:`persist_batch_claim_manifest` just created.

    This is NOT a supersede — it completes the record of the same attempt
    (the manifest's identity, item set, and resource set are unchanged), so
    it updates the existing row in place rather than creating a new one.
    """
    if status not in ("claimed", "failed"):
        raise ValueError(f"status must be 'claimed' or 'failed', got {status!r}")
    detail_json = json.dumps(failure_detail) if failure_detail is not None else None
    await db.execute(
        "UPDATE sprint_batch_claims SET status = ?, failure_detail = ?, "
        "resolved_at = datetime('now') WHERE id = ?",
        (status, detail_json, manifest_id),
    )
    await db.commit()
    result = await get_batch_claim_manifest_by_id(db, manifest_id)
    assert result is not None
    return result
