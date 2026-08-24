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

import itertools
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


# ===========================================================================
# MDE-3 — canonical change-set / cross-store release manifest and crash
# recovery state machine.
#
# Distinct from the wave-scoped MERGE manifest above (multi-session anchor
# coordination against one canonical .docx): this is the lifecycle of ONE
# release TRANSACTION — filesystem staging -> DOCX/package verification ->
# provenance/Outputs registration -> render evidence -> DB commit — for a
# single change-set landing on a single file_path. The gap this closes is
# documented in this project's own C84-W1 gap-matrix note (category 8,
# "CRASH RECOVERY"): a process that crashes mid-promotion today leaves an
# orphaned staged file with NO transaction journal recording that a
# promotion was even in flight, and nothing scans for or resolves it on the
# next startup.
#
# Explicit states, PREPARED -> STAGED -> PROMOTED -> VERIFIED ->
# DB_COMMITTED -> RELEASED, plus RECOVERY_REQUIRED (reachable from any
# non-terminal state) and the two terminal outcomes RELEASED/ABORTED:
#
#   PREPARED       — transaction opened; nothing on disk/DB touched yet.
#   STAGED         — content written to a disposable staged path (never the
#                    canonical file_path itself — mirrors the merge-draft
#                    isolation rule above).
#   PROMOTED       — the staged content has been atomically swapped into
#                    file_path (e.g. via os.replace).
#   VERIFIED       — the promoted file_path was independently re-verified
#                    (structural/hash check) after promotion.
#   DB_COMMITTED   — the corresponding DB-side commit (sprint item, output
#                    registration, provenance record, ...) has landed.
#   RELEASED       — terminal success: every step above is durably recorded.
#   RECOVERY_REQUIRED — a crash/failure was detected; :func:`resolve_release
#                    _recovery` must run before the transaction can proceed.
#   ABORTED        — terminal: recovery determined the promotion never
#                    landed and the transaction was safely abandoned.
#
# Storage: reuses the EXISTING, already-migrated ``action_audit_log`` table
# (same pattern ``meridian.code_intel_receipt`` already established — no new
# table, no new SQLite/Postgres migration, no db/__init__.py or
# pg_adapter.py changes needed). Each state transition writes ONE new
# append-only event carrying the transaction's FULL current snapshot (not a
# delta), so reconstructing "what is this transaction's state right now" is
# just "read the newest event for this transaction_id" — an authoritative,
# durable, cross-process journal a recovering session can read on restart
# without trusting anything about the crashed process's own in-memory state.
# This journal itself IS the "cross-store receipt": it is written
# independently of the filesystem promotion it describes, and records
# whatever evidence the caller supplies about the OTHER stores it touched
# (provenance_registered, render_evidence, db_commit_ref) without this
# module reaching into those stores directly — coordinating those stores is
# the caller's job (e.g. docs_intel.py's promotion path); this module is the
# durable ledger they report into.
# ===========================================================================

RELEASE_STATE_PREPARED = "PREPARED"
RELEASE_STATE_STAGED = "STAGED"
RELEASE_STATE_PROMOTED = "PROMOTED"
RELEASE_STATE_VERIFIED = "VERIFIED"
RELEASE_STATE_DB_COMMITTED = "DB_COMMITTED"
RELEASE_STATE_RELEASED = "RELEASED"
RELEASE_STATE_RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
RELEASE_STATE_ABORTED = "ABORTED"

#: Forward order of the "happy path" states — used to validate that a
#: transition only ever advances exactly one step (or re-asserts the current
#: step, for idempotent resume), never skips ahead.
_RELEASE_STATE_ORDER = [
    RELEASE_STATE_PREPARED,
    RELEASE_STATE_STAGED,
    RELEASE_STATE_PROMOTED,
    RELEASE_STATE_VERIFIED,
    RELEASE_STATE_DB_COMMITTED,
    RELEASE_STATE_RELEASED,
]
_TERMINAL_RELEASE_STATES = frozenset({RELEASE_STATE_RELEASED, RELEASE_STATE_ABORTED})
_ALL_RELEASE_STATES = frozenset(_RELEASE_STATE_ORDER) | {
    RELEASE_STATE_RECOVERY_REQUIRED, RELEASE_STATE_ABORTED,
}

RELEASE_EVENT_TYPE = "release_transaction_state"

#: Strictly-increasing, in-process ordering counter for release-transaction
#: snapshots. ``action_audit_log.created_at`` has only whole-second
#: precision (see db/workspace.py's own "Ordering note" on this exact
#: table); an observed real gotcha on this codebase's own CI/dev machines is
#: that even ``time.monotonic_ns()`` can return the SAME value for two
#: calls microseconds apart (coarse OS timer-tick resolution), so a plain
#: itertools.count() is used instead — guaranteed strictly increasing and
#: unique per call, with zero dependency on clock resolution. Same
#: within-process-only caveat as every other such tiebreaker in this
#: codebase: never compared across processes, never persisted as a
#: wall-clock claim.
_release_seq_counter = itertools.count()


def _next_release_seq() -> int:
    return next(_release_seq_counter)

#: Batch size for reconstructing transaction state from action_audit_log.
#: Best-effort/advisory posture, matching every other action_audit_log-backed
#: lookup in this codebase (e.g. code_intel_receipt.find_recent_prospect_
#: receipt): a change-set/file_path pair with more than this many recorded
#: transitions in the lookback window is a real, if unlikely, scaling edge —
#: documented here rather than silently mishandled.
_RELEASE_SCAN_LIMIT = 500


def valid_release_transition(current: "str | None", target: str) -> bool:
    """True iff *target* is a legal next state from *current*.

    Idempotent resume: re-asserting the SAME state the transaction is
    already in is always valid (a crashed/retried caller re-running its own
    last successful step must never be rejected as an invalid transition).
    ``RECOVERY_REQUIRED`` and ``ABORTED`` are BOTH reachable directly from
    any non-terminal state (a failure can be flagged, or an in-flight
    transaction explicitly abandoned, at any point before RELEASED). From
    ``RECOVERY_REQUIRED`` the two forward outcomes
    :func:`resolve_release_recovery` can reach are resuming into
    ``DB_COMMITTED``/``RELEASED`` (current hash matched the expected
    post-promotion content — the file-level work already succeeded, only
    the DB-side bookkeeping needs finishing) — the "current hash matches
    base, so abort" outcome goes straight to ``ABORTED`` (also legal
    directly from ``RECOVERY_REQUIRED``, covered by the blanket
    non-terminal-state rule above). No state ever transitions OUT of a
    terminal state (RELEASED/ABORTED).
    """
    if current is None:
        return target == RELEASE_STATE_PREPARED
    if current == target:
        return True
    if current in _TERMINAL_RELEASE_STATES:
        return False
    if target in (RELEASE_STATE_RECOVERY_REQUIRED, RELEASE_STATE_ABORTED):
        # A failure can be flagged, or an in-flight transaction explicitly
        # abandoned, from ANY non-terminal state -- not only after first
        # passing through RECOVERY_REQUIRED (e.g. an explicit cancellation
        # of a PREPARED transaction that never touched anything).
        return True
    if current == RELEASE_STATE_RECOVERY_REQUIRED:
        return target in (RELEASE_STATE_DB_COMMITTED, RELEASE_STATE_RELEASED)
    if current not in _RELEASE_STATE_ORDER or target not in _RELEASE_STATE_ORDER:
        return False
    return _RELEASE_STATE_ORDER.index(target) == _RELEASE_STATE_ORDER.index(current) + 1


def _release_detail(row: "dict[str, Any]") -> "dict[str, Any]":
    try:
        parsed = json.loads(row.get("detail") or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def _scan_release_events(
    db: aiosqlite.Connection,
    *,
    project_id: "str | None",
    tenant_id: "str | None",
    limit: int = _RELEASE_SCAN_LIMIT,
) -> "list[dict[str, Any]]":
    """Newest-first raw action_audit_log rows for RELEASE_EVENT_TYPE.

    action_audit_log's ``created_at`` has only whole-second precision (see
    ``db/workspace.py``'s own "Ordering note" a few hundred lines up from
    ``record_action_audit_event`` — the SAME table, the SAME documented gap)
    — nowhere near fine-grained enough to order two transitions of the same
    transaction recorded within one second of each other, a routine
    occurrence in both tests and real usage. Re-sorted here by
    ``(created_at, _seq)`` descending, mirroring that module's own
    established tiebreaker convention (in spirit, not literally — see
    ``_next_release_seq``'s own docstring for why a plain counter is used
    here instead of ``time.monotonic_ns()``): every snapshot this module
    writes embeds a strictly-increasing ``"_seq"``, used ONLY to break
    same-second ties — never compared across processes or trusted as a
    wall-clock claim.
    """
    from . import workspace as _workspace  # noqa: PLC0415 — defer to avoid the

    # db/__init__.py <-> workspace.py circular-import ordering issue, same
    # pattern _migrate_docx_merge_manifests's own callers use elsewhere in
    # this package.
    try:
        rows = await _workspace.get_action_audit_log(
            db, project_id=project_id, tenant_id=tenant_id,
            event_type=RELEASE_EVENT_TYPE, limit=limit,
        )
    except Exception:  # noqa: BLE001 — an unverifiable read must never raise
        return []
    return sorted(
        rows,
        key=lambda r: (r.get("created_at") or "", _release_detail(r).get("_seq", 0)),
        reverse=True,
    )


def _latest_snapshot_per_transaction(
    rows: "list[dict[str, Any]]",
) -> "dict[str, dict[str, Any]]":
    """Reduce newest-first audit rows to ONE latest snapshot per transaction_id.

    Each event carries the FULL current snapshot (see module docstring), so
    the first (newest, since ``rows`` is newest-first) row seen for a given
    ``transaction_id`` is authoritative for that transaction.
    """
    latest: "dict[str, dict[str, Any]]" = {}
    for row in rows:
        detail = _release_detail(row)
        tid = detail.get("transaction_id")
        if not tid or tid in latest:
            continue
        detail["_audit_row_id"] = row.get("id")
        detail["_recorded_at"] = row.get("created_at")
        latest[tid] = detail
    return latest


async def open_release_transaction(
    db: aiosqlite.Connection,
    change_set_id: str,
    file_path: str,
    *,
    session_id: "str | None" = None,
    base_hash: "str | None" = None,
    project_id: "str | None" = None,
    tenant_id: "str | None" = None,
) -> "dict[str, Any]":
    """Open (or resume) a release transaction for *change_set_id*/*file_path*.

    Idempotent/resumable: if a NON-terminal transaction already exists for
    this exact (change_set_id, file_path) pair, it is returned as-is
    (``resumed: True``) rather than opening a duplicate — a crashed or
    retried caller re-entering this function picks up its own prior
    transaction_id instead of starting a parallel, conflicting one. Returns
    a fresh ``PREPARED`` transaction (``resumed: False``) otherwise.
    """
    change_set_id = (change_set_id or "").strip()
    normalized = _normalize_file_path(file_path)
    if not change_set_id or not normalized:
        return {
            "opened": False, "reason": "invalid",
            "message": "change_set_id and file_path are both required",
        }

    existing = await find_open_release_transaction(
        db, change_set_id, normalized, project_id=project_id, tenant_id=tenant_id,
    )
    if existing is not None:
        return {**existing, "opened": True, "resumed": True}

    from . import workspace as _workspace  # noqa: PLC0415

    transaction_id = _new_id()
    snapshot = {
        "transaction_id": transaction_id,
        "change_set_id": change_set_id,
        "file_path": normalized,
        "session_id": session_id,
        "state": RELEASE_STATE_PREPARED,
        "base_hash": base_hash,
        "staged_path": None,
        "staged_hash": None,
        "post_hash": None,
        "provenance_registered": False,
        "render_evidence": None,
        "db_commit_ref": None,
        "error": None,
        "recovery_action": None,
        "history": [RELEASE_STATE_PREPARED],
        "_seq": _next_release_seq(),
    }
    await _workspace.record_action_audit_event(
        db, RELEASE_EVENT_TYPE, tenant_id=tenant_id, project_id=project_id,
        actor=session_id, detail=json.dumps(snapshot),
    )
    return {**snapshot, "opened": True, "resumed": False}


async def find_open_release_transaction(
    db: aiosqlite.Connection,
    change_set_id: str,
    file_path: str,
    *,
    project_id: "str | None" = None,
    tenant_id: "str | None" = None,
) -> "dict[str, Any] | None":
    """The latest NON-terminal transaction for (change_set_id, file_path),
    or ``None``. Used by :func:`open_release_transaction` for resumability
    and by a recovering caller to rediscover in-flight work after a crash."""
    change_set_id = (change_set_id or "").strip()
    normalized = _normalize_file_path(file_path)
    rows = await _scan_release_events(db, project_id=project_id, tenant_id=tenant_id)
    latest = _latest_snapshot_per_transaction(rows)
    for snapshot in latest.values():
        if (
            snapshot.get("change_set_id") == change_set_id
            and snapshot.get("file_path") == normalized
            and snapshot.get("state") not in _TERMINAL_RELEASE_STATES
        ):
            return snapshot
    return None


async def get_release_transaction(
    db: aiosqlite.Connection,
    transaction_id: str,
    *,
    project_id: "str | None" = None,
    tenant_id: "str | None" = None,
) -> "dict[str, Any] | None":
    """The latest recorded snapshot for *transaction_id*, or ``None`` if no
    such transaction has ever been recorded (in the scanned lookback
    window — see ``_RELEASE_SCAN_LIMIT``)."""
    rows = await _scan_release_events(db, project_id=project_id, tenant_id=tenant_id)
    for row in rows:
        detail = _release_detail(row)
        if detail.get("transaction_id") == transaction_id:
            detail["_audit_row_id"] = row.get("id")
            detail["_recorded_at"] = row.get("created_at")
            return detail
    return None


async def advance_release_state(
    db: aiosqlite.Connection,
    transaction_id: str,
    target_state: str,
    *,
    session_id: "str | None" = None,
    project_id: "str | None" = None,
    tenant_id: "str | None" = None,
    staged_path: "str | None" = None,
    staged_hash: "str | None" = None,
    post_hash: "str | None" = None,
    provenance_registered: "bool | None" = None,
    render_evidence: "Any" = None,
    db_commit_ref: "str | None" = None,
    error: "str | None" = None,
) -> "dict[str, Any]":
    """Advance *transaction_id* to *target_state*, fail-closed on an invalid
    transition.

    Never silently skips a step or resurrects a terminal transaction: an
    illegal transition (skipping a state, moving out of RELEASED/ABORTED, an
    unrecognized target) is refused (``advanced: False, reason:
    "invalid_transition"``) rather than recorded — the single mechanism this
    module relies on for "no partial release is ever presented as
    complete." Idempotent resume: calling with the transaction's OWN current
    state is always accepted and simply re-records the (possibly updated)
    field values without complaint. Any of the optional per-field kwargs
    left at their default (``None``) preserve the transaction's PRIOR value
    for that field — only fields the caller actually passes are updated.
    """
    if target_state not in _ALL_RELEASE_STATES:
        return {
            "advanced": False, "reason": "unknown_state",
            "message": f"{target_state!r} is not a recognized release state.",
        }
    current = await get_release_transaction(
        db, transaction_id, project_id=project_id, tenant_id=tenant_id,
    )
    if current is None:
        return {
            "advanced": False, "reason": "no_such_transaction",
            "message": f"No release transaction found for id {transaction_id!r}.",
        }
    current_state = current.get("state")
    if not valid_release_transition(current_state, target_state):
        return {
            "advanced": False, "reason": "invalid_transition",
            "current_state": current_state, "target_state": target_state,
            "message": (
                f"{current_state!r} -> {target_state!r} is not a legal release "
                "transition — refusing to record it. This is the fail-closed "
                "guard against ever presenting a partial release as complete."
            ),
        }

    from . import workspace as _workspace  # noqa: PLC0415

    history = list(current.get("history") or [])
    if not history or history[-1] != target_state:
        history.append(target_state)
    snapshot = {
        "transaction_id": transaction_id,
        "change_set_id": current.get("change_set_id"),
        "file_path": current.get("file_path"),
        "session_id": session_id or current.get("session_id"),
        "state": target_state,
        "base_hash": current.get("base_hash"),
        "staged_path": staged_path if staged_path is not None else current.get("staged_path"),
        "staged_hash": staged_hash if staged_hash is not None else current.get("staged_hash"),
        "post_hash": post_hash if post_hash is not None else current.get("post_hash"),
        "provenance_registered": (
            provenance_registered if provenance_registered is not None
            else current.get("provenance_registered", False)
        ),
        "render_evidence": render_evidence if render_evidence is not None else current.get("render_evidence"),
        "db_commit_ref": db_commit_ref if db_commit_ref is not None else current.get("db_commit_ref"),
        "error": error if error is not None else (
            None if target_state not in (RELEASE_STATE_RECOVERY_REQUIRED,) else current.get("error")
        ),
        "recovery_action": current.get("recovery_action"),
        "history": history,
        "_seq": _next_release_seq(),
    }
    await _workspace.record_action_audit_event(
        db, RELEASE_EVENT_TYPE, tenant_id=tenant_id, project_id=project_id,
        actor=session_id or current.get("session_id"),
        detail=json.dumps(snapshot),
    )
    return {**snapshot, "advanced": True, "previous_state": current_state}


async def resolve_release_recovery(
    db: aiosqlite.Connection,
    transaction_id: str,
    current_hash: "str | None",
    *,
    session_id: "str | None" = None,
    project_id: "str | None" = None,
    tenant_id: "str | None" = None,
) -> "dict[str, Any]":
    """The crash-recovery DECISION function: compare *current_hash* (the
    caller's freshly-computed hash of the transaction's ``file_path`` as it
    actually exists on disk right now) against the transaction's own
    recorded ``base_hash``/``post_hash``.

    * ``current_hash == base_hash`` -> ``action: "abort"``. The promotion
      never actually landed (or was cleanly rolled back before the crash) —
      the canonical file is exactly as it was before this transaction ever
      touched it, so it is safe to declare the transaction ABORTED. No file
      write happens here; the CALLER is responsible for reclaiming any
      orphaned staged file at ``staged_path``.
    * ``current_hash == post_hash`` -> ``action: "finish_db_commit"``. The
      promotion genuinely succeeded (the file already holds the expected
      post-transaction content) but the crash happened before DB_COMMITTED/
      RELEASED was recorded — safe to resume forward and finish the
      remaining DB-side bookkeeping; nothing filesystem-side needs redoing.
    * Neither matches (including when either recorded hash is ``None``, or
      *current_hash* itself couldn't be computed) -> ``action:
      "require_human"``. The file is in a state this function cannot prove
      is safe — it NEVER guesses, and NEVER restores a stale backup
      automatically. The transaction is recorded as ``RECOVERY_REQUIRED``
      either way (if not already) so the ambiguity itself is durably
      journaled, not silently retried forever.

    Always returns a dict with ``action`` set to one of the three values
    above (or ``"no_such_transaction"`` if *transaction_id* is unknown), and
    records the decision via :func:`advance_release_state` before returning
    (idempotent: re-resolving an already-decided ``RECOVERY_REQUIRED``
    transaction with the SAME current_hash reproduces the SAME decision).
    """
    current = await get_release_transaction(
        db, transaction_id, project_id=project_id, tenant_id=tenant_id,
    )
    if current is None:
        return {"action": "no_such_transaction", "transaction_id": transaction_id}

    state = current.get("state")
    if state in _TERMINAL_RELEASE_STATES:
        return {
            "action": "already_terminal", "state": state,
            "transaction_id": transaction_id,
            "message": f"Transaction is already terminal ({state}) — nothing to recover.",
        }

    base_hash = current.get("base_hash")
    post_hash = current.get("post_hash")
    if current_hash is not None and base_hash is not None and current_hash == base_hash:
        action = "abort"
        target_state = RELEASE_STATE_ABORTED
    elif current_hash is not None and post_hash is not None and current_hash == post_hash:
        action = "finish_db_commit"
        target_state = (
            RELEASE_STATE_RELEASED if state == RELEASE_STATE_DB_COMMITTED
            else RELEASE_STATE_DB_COMMITTED
        )
    else:
        action = "require_human"
        target_state = RELEASE_STATE_RECOVERY_REQUIRED

    # Ensure the transaction is AT LEAST flagged RECOVERY_REQUIRED before
    # attempting the resolving transition (a transition out of
    # RECOVERY_REQUIRED is only legal FROM that state — see
    # valid_release_transition) — a no-op if it's already there.
    if state != RELEASE_STATE_RECOVERY_REQUIRED and target_state != RELEASE_STATE_RECOVERY_REQUIRED:
        await advance_release_state(
            db, transaction_id, RELEASE_STATE_RECOVERY_REQUIRED,
            session_id=session_id, project_id=project_id, tenant_id=tenant_id,
            error=(
                f"crash recovery invoked: current_hash={current_hash!r} vs "
                f"base_hash={base_hash!r}/post_hash={post_hash!r}"
            ),
        )

    result = await advance_release_state(
        db, transaction_id, target_state,
        session_id=session_id, project_id=project_id, tenant_id=tenant_id,
        error=None if action != "require_human" else (
            f"UNRESOLVED: current_hash={current_hash!r} matches neither "
            f"base_hash={base_hash!r} nor post_hash={post_hash!r} — human "
            "recovery required, refusing to guess or restore a stale backup."
        ),
    )
    return {
        "action": action,
        "transaction_id": transaction_id,
        "state": result.get("state", target_state),
        "advanced": result.get("advanced", False),
        "current_hash": current_hash,
        "base_hash": base_hash,
        "post_hash": post_hash,
    }


async def list_release_transactions(
    db: aiosqlite.Connection,
    *,
    project_id: "str | None" = None,
    tenant_id: "str | None" = None,
    state: "str | None" = None,
    limit: int = _RELEASE_SCAN_LIMIT,
) -> "list[dict[str, Any]]":
    """Read-only: every distinct transaction's latest snapshot (optionally
    filtered to one ``state``), newest-recorded first. Used both for
    ``RECOVERY_REQUIRED`` startup scanning and for handoff evidence
    (:func:`summarize_release_transactions`)."""
    rows = await _scan_release_events(db, project_id=project_id, tenant_id=tenant_id, limit=limit)
    latest = _latest_snapshot_per_transaction(rows)
    transactions = list(latest.values())
    if state is not None:
        transactions = [t for t in transactions if t.get("state") == state]
    transactions.sort(key=lambda t: t.get("_recorded_at") or "", reverse=True)
    return transactions


def summarize_release_transactions(transactions: "list[dict[str, Any]]") -> "dict[str, Any]":
    """Pure, DB-free reduction of :func:`list_release_transactions`'s output
    into the small, bounded evidence summary a handoff embeds (MDE-3: "exact
    evidence in handoff"): counts by state, and the transaction_ids of any
    that need attention (RECOVERY_REQUIRED)."""
    counts: "dict[str, int]" = {}
    recovery_needed: "list[dict[str, Any]]" = []
    for t in transactions:
        state = t.get("state") or "UNKNOWN"
        counts[state] = counts.get(state, 0) + 1
        if state == RELEASE_STATE_RECOVERY_REQUIRED:
            recovery_needed.append({
                "transaction_id": t.get("transaction_id"),
                "change_set_id": t.get("change_set_id"),
                "file_path": t.get("file_path"),
                "error": t.get("error"),
            })
    return {
        "transaction_count": len(transactions),
        "state_counts": counts,
        "recovery_required": recovery_needed,
        "all_released": bool(transactions) and all(
            t.get("state") in (RELEASE_STATE_RELEASED, RELEASE_STATE_ABORTED) for t in transactions
        ),
    }
