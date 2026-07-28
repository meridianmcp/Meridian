"""fe989980 — wave-scoped DOCX merge manifests and serialized canonical merge gate.

Depends on the scoped docx-region claim primitives in ``meridian.db.locks``
(f7ee1ba7: ``claim_docx_region`` / ``check_docx_region_write_conflict`` /
``release_docx_region_claims``), which already give a session exclusive
element-level ownership of a paragraph/element within a single .docx. This
module adds a coordination layer ON TOP of that primitive for multi-session
"wave" work against ONE canonical .docx:

* During a wave, each session works against its own ISOLATED draft artifact
  (``draft_path`` — never the canonical ``file_path`` itself). Registering a
  draft (:func:`open_merge_manifest`) and declaring the anchors it touches
  (:func:`declare_merge_anchors`) reuses ``claim_docx_region`` verbatim for
  the actual exclusivity check — this module never reimplements or weakens
  that logic, only wraps it with wave/manifest bookkeeping.
* Only ONE session may hold the serialized "merge owner" role for a given
  (wave_id, file_path) manifest at a time (:func:`claim_merge_owner` /
  :func:`release_merge_owner`) — the owner is the only session allowed to
  actually write drafted anchors into the canonical file.
* Anchors are addressed by the same durable ``element_id`` (``w14:paraId``)
  the region-claim layer already uses — merges are keyed on stable anchors,
  never on line/byte offsets that shift as the canonical file is edited.
* :func:`check_merge_stale_or_overlap` is the pre-write gate: it rejects a
  merge attempt whose draft was opened against an outdated canonical
  revision (``stale_revision``), whose anchor was already merged by a
  DIFFERENT session (``anchor_already_merged``), or that isn't held by the
  current merge owner (``not_merge_owner``).
* :func:`record_merge_result` persists the per-anchor merge ledger row.
  Keyed uniquely on (manifest_id, element_id) so re-recording the same
  session's own already-merged anchor is a safe idempotent no-op, while a
  genuine race for the same anchor by two different sessions/drafts can
  only ever have one winner (UNIQUE-constraint-backed, mirrors the
  ON CONFLICT DO NOTHING race pattern used throughout ``locks.py``).
* :func:`finalize_merge_manifest` is the verification-gated completion step:
  it refuses to mark a manifest ``complete`` without a truthy verification
  payload, is idempotent (repeat calls after completion just report the
  stored result), and fails closed for a non-owner or an already-aborted
  manifest.
"""
from __future__ import annotations

import json
from typing import Any

import aiosqlite

from meridian.db import _new_id, _row_to_dict  # noqa: PLC0415

from .locks import _normalize_file_path, claim_docx_region

_MERGE_OWNER_TTL_MINUTES = 30


async def _migrate_docx_merge_manifests(db: aiosqlite.Connection) -> None:
    """fe989980 — create the wave-scoped merge-manifest tables if absent.

    Guarded migration (no inline CREATE INDEX in the unguarded base schema
    literals — 2026-07-04 outage rule): tables + their indexes are created
    here so existing DBs pick them up on first startup after the deploy.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS docx_merge_manifests (
            id TEXT PRIMARY KEY,
            wave_id TEXT NOT NULL,
            file_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            base_revision TEXT,
            merge_owner_session_id TEXT,
            merge_owner_claimed_at TEXT,
            merge_owner_expires_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT,
            verification TEXT
        )
        """
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_docx_merge_manifests_wave_file "
        "ON docx_merge_manifests (wave_id, file_path)"
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS docx_merge_drafts (
            id TEXT PRIMARY KEY,
            manifest_id TEXT NOT NULL REFERENCES docx_merge_manifests(id) ON DELETE CASCADE,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            draft_path TEXT NOT NULL,
            anchors TEXT NOT NULL DEFAULT '[]',
            declared_at TEXT NOT NULL DEFAULT (datetime('now')),
            merged_at TEXT
        )
        """
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_docx_merge_drafts_manifest_session "
        "ON docx_merge_drafts (manifest_id, session_id)"
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS docx_merge_anchor_locks (
            id TEXT PRIMARY KEY,
            manifest_id TEXT NOT NULL REFERENCES docx_merge_manifests(id) ON DELETE CASCADE,
            element_id TEXT NOT NULL,
            draft_id TEXT,
            session_id TEXT NOT NULL,
            merged_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_docx_merge_anchor_locks_manifest_element "
        "ON docx_merge_anchor_locks (manifest_id, element_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_docx_merge_anchor_locks_session "
        "ON docx_merge_anchor_locks (session_id)"
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Row lookup helpers
# ---------------------------------------------------------------------------

async def _get_manifest_row(
    db: aiosqlite.Connection, wave_id: str, file_path: str
) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT * FROM docx_merge_manifests WHERE wave_id = ? AND file_path = ?",
        (wave_id, file_path),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def _get_draft_row(
    db: aiosqlite.Connection, manifest_id: str, session_id: str
) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT * FROM docx_merge_drafts WHERE manifest_id = ? AND session_id = ?",
        (manifest_id, session_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# Manifest lifecycle: open -> declare anchors -> claim owner -> merge -> finalize
# ---------------------------------------------------------------------------

async def open_merge_manifest(
    db: aiosqlite.Connection,
    wave_id: str,
    file_path: str,
    session_id: str,
    *,
    base_revision: str | None = None,
    draft_path: str,
) -> dict[str, Any]:
    """Register this session's ISOLATED draft within a wave's merge manifest.

    Creates the (wave_id, file_path) manifest on first call (idempotent
    thereafter — ``ON CONFLICT DO NOTHING``), then registers/refreshes this
    session's draft row. ``draft_path`` MUST differ from the canonical
    ``file_path`` — a session must never treat the canonical document as its
    own scratch space during parallel wave work ("isolated draft artifacts
    only during parallel work" is enforced here, structurally, not by
    convention).
    """
    normalized = _normalize_file_path(file_path)
    wave_id = (wave_id or "").strip()
    session_id = (session_id or "").strip()
    if not normalized or not wave_id or not session_id:
        return {
            "opened": False,
            "reason": "invalid",
            "message": "wave_id, file_path, and session_id are all required",
        }
    draft = (draft_path or "").strip()
    if not draft:
        return {
            "opened": False,
            "reason": "invalid",
            "message": (
                "draft_path is required — isolated draft artifacts must not "
                "write to the canonical file_path during parallel work"
            ),
        }
    if draft == normalized:
        return {
            "opened": False,
            "reason": "not_isolated",
            "message": (
                "draft_path must differ from the canonical file_path — drafts "
                "must be isolated artifacts, never the canonical file itself"
            ),
        }

    manifest = await _get_manifest_row(db, wave_id, normalized)
    if manifest and manifest.get("status") in ("complete", "aborted"):
        return {
            "opened": False,
            "reason": "manifest_closed",
            "manifest_status": manifest.get("status"),
            "message": (
                f"Merge manifest for wave {wave_id!r} on {normalized} is already "
                f"{manifest.get('status')} — open a new wave_id."
            ),
        }

    if not manifest:
        await db.execute(
            "INSERT INTO docx_merge_manifests (id, wave_id, file_path, status, base_revision) "
            "VALUES (?, ?, ?, 'open', ?) "
            "ON CONFLICT (wave_id, file_path) DO NOTHING",
            (_new_id(), wave_id, normalized, base_revision),
        )
        await db.commit()
        manifest = await _get_manifest_row(db, wave_id, normalized)
    manifest_id = manifest["id"]

    existing_draft = await _get_draft_row(db, manifest_id, session_id)
    if existing_draft:
        await db.execute(
            "UPDATE docx_merge_drafts SET draft_path = ? WHERE id = ?",
            (draft, existing_draft["id"]),
        )
    else:
        await db.execute(
            "INSERT INTO docx_merge_drafts (id, manifest_id, session_id, draft_path, anchors) "
            "VALUES (?, ?, ?, ?, '[]') "
            "ON CONFLICT (manifest_id, session_id) DO NOTHING",
            (_new_id(), manifest_id, session_id, draft),
        )
    await db.commit()

    manifest = await _get_manifest_row(db, wave_id, normalized)
    draft_row = await _get_draft_row(db, manifest_id, session_id)
    return {
        "opened": True,
        "manifest_id": manifest_id,
        "wave_id": wave_id,
        "file_path": normalized,
        "session_id": session_id,
        "draft_path": draft_row.get("draft_path") if draft_row else draft,
        "base_revision": manifest.get("base_revision"),
        "status": manifest.get("status"),
    }


async def declare_merge_anchors(
    db: aiosqlite.Connection,
    wave_id: str,
    file_path: str,
    session_id: str,
    anchors: list[str],
) -> dict[str, Any]:
    """Declare the durable anchors (``element_id``) this session's draft touches.

    Reuses :func:`meridian.db.locks.claim_docx_region` verbatim for every
    requested anchor — the ONLY exclusivity check performed is the existing
    scoped-region-claim primitive, preserved unmodified. An anchor another
    session already holds surfaces as a conflict here exactly as it would
    via a direct ``claim_docx_region`` call; anchors this session already
    owns (from this wave or any other) refresh idempotently.
    """
    normalized = _normalize_file_path(file_path)
    wave_id = (wave_id or "").strip()
    session_id = (session_id or "").strip()

    manifest = await _get_manifest_row(db, wave_id, normalized)
    if not manifest:
        return {
            "declared": False,
            "reason": "no_manifest",
            "message": (
                f"No merge manifest for wave {wave_id!r} on {normalized}. "
                "Call open_merge_manifest first."
            ),
        }
    if manifest.get("status") not in ("open", "merging"):
        return {
            "declared": False,
            "reason": "manifest_closed",
            "manifest_status": manifest.get("status"),
        }
    draft = await _get_draft_row(db, manifest["id"], session_id)
    if not draft:
        return {
            "declared": False,
            "reason": "no_draft",
            "message": "Call open_merge_manifest to register a draft before declaring anchors.",
        }

    requested = sorted({(a or "").strip() for a in (anchors or []) if (a or "").strip()})
    if not requested:
        return {"declared": False, "reason": "invalid", "message": "anchors must be a non-empty list"}

    claimed: list[str] = []
    conflicts: list[dict[str, Any]] = []
    for elem in requested:
        result = await claim_docx_region(db, session_id, normalized, elem)
        if result.get("claimed"):
            claimed.append(elem)
        else:
            # claim_docx_region reports the holder differently by reason:
            # "file_locked" puts it top-level (holder_session_id); the more
            # common "element_conflict" nests it inside the per-conflict
            # "conflicts" list instead. Check both so callers always get the
            # actual holder, not None.
            holder = result.get("holder_session_id")
            if holder is None:
                nested = result.get("conflicts") or []
                if nested:
                    holder = nested[0].get("holder_session_id")
            conflicts.append({
                "element_id": elem,
                "reason": result.get("reason"),
                "holder_session_id": holder,
                "message": result.get("message"),
            })

    if claimed:
        existing_anchors = set(json.loads(draft.get("anchors") or "[]"))
        await db.execute(
            "UPDATE docx_merge_drafts SET anchors = ? WHERE id = ?",
            (json.dumps(sorted(existing_anchors | set(claimed))), draft["id"]),
        )
        await db.commit()

    return {
        "declared": bool(claimed) and not conflicts,
        "wave_id": wave_id,
        "file_path": normalized,
        "session_id": session_id,
        "claimed_anchors": claimed,
        "conflicts": conflicts,
    }


async def claim_merge_owner(
    db: aiosqlite.Connection,
    wave_id: str,
    file_path: str,
    session_id: str,
    *,
    ttl_minutes: int = _MERGE_OWNER_TTL_MINUTES,
) -> dict[str, Any]:
    """Claim the single serialized merge-owner role for (wave_id, file_path).

    Exactly one session may hold this role at a time (mirrors
    ``locks.claim_resource``'s exclusive-TTL pattern). Only the merge owner
    may write drafted anchors into the canonical file
    (:func:`check_merge_stale_or_overlap` enforces this). Re-claiming by the
    current owner refreshes the TTL (idempotent); an expired claim is
    reclaimable by anyone.
    """
    normalized = _normalize_file_path(file_path)
    wave_id = (wave_id or "").strip()
    session_id = (session_id or "").strip()

    manifest = await _get_manifest_row(db, wave_id, normalized)
    if not manifest:
        return {"claimed": False, "reason": "no_manifest", "message": "Call open_merge_manifest first."}
    if manifest.get("status") in ("complete", "aborted"):
        return {"claimed": False, "reason": "manifest_closed", "manifest_status": manifest.get("status")}

    # Expire a stale ownership claim before evaluating (mirrors file_locks TTL expiry).
    await db.execute(
        "UPDATE docx_merge_manifests SET merge_owner_session_id = NULL, "
        "merge_owner_claimed_at = NULL, merge_owner_expires_at = NULL "
        "WHERE id = ? AND merge_owner_expires_at IS NOT NULL "
        "AND merge_owner_expires_at <= datetime('now')",
        (manifest["id"],),
    )
    await db.commit()
    manifest = await _get_manifest_row(db, wave_id, normalized)

    holder = manifest.get("merge_owner_session_id")
    if holder and holder != session_id:
        return {
            "claimed": False,
            "reason": "owner_locked",
            "holder_session_id": holder,
            "expires_at": manifest.get("merge_owner_expires_at"),
            "message": (
                f"Session {holder} already holds merge ownership for wave "
                f"{wave_id!r} on {normalized}."
            ),
        }

    await db.execute(
        "UPDATE docx_merge_manifests SET merge_owner_session_id = ?, "
        "merge_owner_claimed_at = datetime('now'), "
        "merge_owner_expires_at = datetime('now', ? || ' minutes'), "
        "status = CASE WHEN status = 'open' THEN 'merging' ELSE status END "
        "WHERE id = ?",
        (session_id, str(ttl_minutes), manifest["id"]),
    )
    await db.commit()
    # b033c10f-style re-check: another session may have raced us between the
    # holder check above and this UPDATE. Re-select to find the true winner.
    manifest = await _get_manifest_row(db, wave_id, normalized)
    if manifest.get("merge_owner_session_id") != session_id:
        return {
            "claimed": False,
            "reason": "owner_locked",
            "holder_session_id": manifest.get("merge_owner_session_id"),
            "expires_at": manifest.get("merge_owner_expires_at"),
            "message": "Another session claimed merge ownership concurrently.",
        }
    return {
        "claimed": True,
        "wave_id": wave_id,
        "file_path": normalized,
        "session_id": session_id,
        "expires_at": manifest.get("merge_owner_expires_at"),
        "status": manifest.get("status"),
    }


async def release_merge_owner(
    db: aiosqlite.Connection,
    wave_id: str,
    file_path: str,
    session_id: str,
) -> bool:
    """Release merge ownership held by ``session_id``. No-op (False) otherwise.

    A manifest still mid-merge (status ``merging``) reverts to ``open`` so
    another session may pick up ownership; a manifest already ``complete`` is
    left untouched (ownership is cleared at completion by
    :func:`finalize_merge_manifest`, not here).
    """
    normalized = _normalize_file_path(file_path)
    manifest = await _get_manifest_row(db, (wave_id or "").strip(), normalized)
    if not manifest or manifest.get("merge_owner_session_id") != session_id:
        return False
    new_status = "open" if manifest.get("status") == "merging" else manifest.get("status")
    await db.execute(
        "UPDATE docx_merge_manifests SET merge_owner_session_id = NULL, "
        "merge_owner_claimed_at = NULL, merge_owner_expires_at = NULL, status = ? "
        "WHERE id = ?",
        (new_status, manifest["id"]),
    )
    await db.commit()
    return True


async def check_merge_stale_or_overlap(
    db: aiosqlite.Connection,
    wave_id: str,
    file_path: str,
    session_id: str,
    element_id: str,
    *,
    expected_base_revision: str | None = None,
) -> dict[str, Any] | None:
    """Pre-write gate for merging one anchor into the canonical file.

    Returns a ``{"blocked": True, "reason": ..., ...}`` dict when the merge
    must be rejected, else ``None`` (clear to proceed — including the
    idempotent case where this exact session already merged this anchor;
    :func:`record_merge_result` is what actually short-circuits the
    duplicate write). Rejection reasons:

    * ``no_manifest`` — no manifest open for this wave/file.
    * ``manifest_aborted`` / ``manifest_complete`` — the manifest is closed;
      completed manifests are immutable, so any further merge is stale by
      definition.
    * ``not_merge_owner`` — the caller doesn't hold the serialized merge-owner
      role (:func:`claim_merge_owner`).
    * ``stale_revision`` — the draft's expected base revision no longer
      matches the manifest's recorded canonical revision (someone else's
      merge already advanced it since this draft was opened).
    * ``anchor_already_merged`` — a DIFFERENT session's draft already merged
      this exact anchor — a genuine overlap that must never be double-applied.
    """
    normalized = _normalize_file_path(file_path)
    wave_id = (wave_id or "").strip()
    session_id = (session_id or "").strip()
    elem = (element_id or "").strip()

    manifest = await _get_manifest_row(db, wave_id, normalized)
    if not manifest:
        return {
            "blocked": True,
            "reason": "no_manifest",
            "message": "No merge manifest open for this wave/file.",
        }
    status = manifest.get("status")
    if status == "aborted":
        return {"blocked": True, "reason": "manifest_aborted", "message": "Merge manifest was aborted."}
    if status == "complete":
        return {
            "blocked": True,
            "reason": "manifest_complete",
            "message": "Merge manifest already finalized; no further merges allowed.",
        }

    holder = manifest.get("merge_owner_session_id")
    if holder != session_id:
        return {
            "blocked": True,
            "reason": "not_merge_owner",
            "holder_session_id": holder,
            "message": "Caller does not hold merge ownership for this wave/file. Call claim_merge_owner first.",
        }

    if expected_base_revision is not None and manifest.get("base_revision") != expected_base_revision:
        return {
            "blocked": True,
            "reason": "stale_revision",
            "expected": expected_base_revision,
            "current": manifest.get("base_revision"),
            "message": (
                "The draft's base revision no longer matches the manifest's "
                "canonical revision — re-open against the current baseline."
            ),
        }

    if not elem:
        return {"blocked": True, "reason": "invalid", "message": "element_id is required"}

    async with db.execute(
        "SELECT session_id FROM docx_merge_anchor_locks WHERE manifest_id = ? AND element_id = ?",
        (manifest["id"], elem),
    ) as cur:
        row = await cur.fetchone()
    existing = _row_to_dict(row)
    if existing and existing.get("session_id") != session_id:
        return {
            "blocked": True,
            "reason": "anchor_already_merged",
            "element_id": elem,
            "holder_session_id": existing.get("session_id"),
            "message": (
                f"Element {elem!r} was already merged into the canonical file "
                "by another session/draft."
            ),
        }
    return None


async def record_merge_result(
    db: aiosqlite.Connection,
    wave_id: str,
    file_path: str,
    session_id: str,
    element_id: str,
    *,
    canonical_revision_after: str | None = None,
) -> dict[str, Any]:
    """Persist that ``session_id`` merged ``element_id`` into the canonical file.

    Idempotent: a second call for an anchor this SAME session already merged
    returns ``{"recorded": True, "already_merged": True}`` without creating a
    duplicate row or re-applying anything. A race for the same anchor by two
    different sessions can only ever have one winner — enforced by the
    UNIQUE(manifest_id, element_id) index, with a re-select after the
    ``ON CONFLICT DO NOTHING`` insert to surface the true winner (mirrors the
    ``claim_file`` / ``claim_resource`` race-detection pattern in ``locks.py``).
    """
    normalized = _normalize_file_path(file_path)
    wave_id = (wave_id or "").strip()
    session_id = (session_id or "").strip()
    elem = (element_id or "").strip()

    manifest = await _get_manifest_row(db, wave_id, normalized)
    if not manifest:
        return {"recorded": False, "reason": "no_manifest"}
    if not elem:
        return {"recorded": False, "reason": "invalid"}

    async with db.execute(
        "SELECT session_id FROM docx_merge_anchor_locks WHERE manifest_id = ? AND element_id = ?",
        (manifest["id"], elem),
    ) as cur:
        row = await cur.fetchone()
    existing = _row_to_dict(row)
    if existing:
        if existing.get("session_id") != session_id:
            return {
                "recorded": False,
                "reason": "anchor_already_merged_by_other",
                "holder_session_id": existing.get("session_id"),
            }
        return {"recorded": True, "already_merged": True, "element_id": elem}

    draft = await _get_draft_row(db, manifest["id"], session_id)
    await db.execute(
        "INSERT INTO docx_merge_anchor_locks (id, manifest_id, element_id, draft_id, session_id) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (manifest_id, element_id) DO NOTHING",
        (_new_id(), manifest["id"], elem, draft["id"] if draft else None, session_id),
    )
    await db.commit()

    # Race-safety re-check — another session's insert may have won the UNIQUE
    # index between our SELECT above and this INSERT.
    async with db.execute(
        "SELECT session_id FROM docx_merge_anchor_locks WHERE manifest_id = ? AND element_id = ?",
        (manifest["id"], elem),
    ) as cur:
        row2 = await cur.fetchone()
    winner = _row_to_dict(row2) or {}
    if winner.get("session_id") != session_id:
        return {
            "recorded": False,
            "reason": "anchor_already_merged_by_other",
            "holder_session_id": winner.get("session_id"),
        }

    if draft:
        await db.execute(
            "UPDATE docx_merge_drafts SET merged_at = datetime('now') WHERE id = ?",
            (draft["id"],),
        )
    if canonical_revision_after is not None:
        await db.execute(
            "UPDATE docx_merge_manifests SET base_revision = ? WHERE id = ?",
            (canonical_revision_after, manifest["id"]),
        )
    await db.commit()
    return {"recorded": True, "already_merged": False, "element_id": elem}


async def finalize_merge_manifest(
    db: aiosqlite.Connection,
    wave_id: str,
    file_path: str,
    session_id: str,
    *,
    verification: Any,
) -> dict[str, Any]:
    """Verification-gated, idempotent completion of a wave's merge manifest.

    Refuses to mark the manifest ``complete`` without a truthy
    ``verification`` payload (``verification_required``), without the caller
    holding merge ownership (``not_merge_owner``), or against an aborted
    manifest (``manifest_aborted``). A repeat call after completion returns
    the stored result with ``already_complete: True`` rather than re-running
    anything. On success, merge ownership is released as part of completion.
    """
    normalized = _normalize_file_path(file_path)
    wave_id = (wave_id or "").strip()
    session_id = (session_id or "").strip()

    manifest = await _get_manifest_row(db, wave_id, normalized)
    if not manifest:
        return {"finalized": False, "reason": "no_manifest"}
    if manifest.get("status") == "complete":
        stored_verification = manifest.get("verification")
        try:
            stored_verification = json.loads(stored_verification) if stored_verification else None
        except (TypeError, ValueError):
            pass
        return {
            "finalized": True,
            "already_complete": True,
            "completed_at": manifest.get("completed_at"),
            "verification": stored_verification,
        }
    if manifest.get("status") == "aborted":
        return {"finalized": False, "reason": "manifest_aborted"}
    if manifest.get("merge_owner_session_id") != session_id:
        return {
            "finalized": False,
            "reason": "not_merge_owner",
            "holder_session_id": manifest.get("merge_owner_session_id"),
        }
    if not verification:
        return {
            "finalized": False,
            "reason": "verification_required",
            "message": "A truthy verification payload is required before completion.",
        }

    verification_json = verification if isinstance(verification, str) else json.dumps(verification)
    await db.execute(
        "UPDATE docx_merge_manifests SET status = 'complete', completed_at = datetime('now'), "
        "verification = ?, merge_owner_session_id = NULL, merge_owner_claimed_at = NULL, "
        "merge_owner_expires_at = NULL WHERE id = ?",
        (verification_json, manifest["id"]),
    )
    await db.commit()
    manifest = await _get_manifest_row(db, wave_id, normalized)
    return {
        "finalized": True,
        "already_complete": False,
        "completed_at": manifest.get("completed_at"),
        "verification": verification,
    }


async def get_merge_manifest(
    db: aiosqlite.Connection, wave_id: str, file_path: str
) -> dict[str, Any] | None:
    """Read-only status view: manifest fields + drafts + merged-anchor ledger."""
    normalized = _normalize_file_path(file_path)
    manifest = await _get_manifest_row(db, (wave_id or "").strip(), normalized)
    if not manifest:
        return None

    async with db.execute(
        "SELECT id, session_id, draft_path, anchors, declared_at, merged_at "
        "FROM docx_merge_drafts WHERE manifest_id = ? ORDER BY declared_at",
        (manifest["id"],),
    ) as cur:
        draft_rows = await cur.fetchall()
    drafts: list[dict[str, Any]] = []
    for r in draft_rows:
        d = _row_to_dict(r) or {}
        try:
            d["anchors"] = json.loads(d.get("anchors") or "[]")
        except (TypeError, ValueError):
            d["anchors"] = []
        drafts.append(d)

    async with db.execute(
        "SELECT element_id, session_id, merged_at FROM docx_merge_anchor_locks "
        "WHERE manifest_id = ? ORDER BY merged_at",
        (manifest["id"],),
    ) as cur:
        anchor_rows = await cur.fetchall()
    merged_anchors = [r for r in (_row_to_dict(row) for row in anchor_rows) if r]

    result = dict(manifest)
    if result.get("verification"):
        try:
            result["verification"] = json.loads(result["verification"])
        except (TypeError, ValueError):
            pass
    result["drafts"] = drafts
    result["merged_anchors"] = merged_anchors
    return result
