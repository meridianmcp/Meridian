"""Durable, project-scoped, cross-client session recovery registry (RESCUE-D).

See :mod:`meridian.session_recovery` for the full identity-separation
rationale (local transcript id / RC bridge id / environment id / Meridian
session id) and the host-local-vs-hosted-metadata security contract this
table enforces. This module is the DB-facing half: CRUD over
``session_recovery_registry`` plus :func:`build_recovery_continuation`, which
re-derives the LIVE sprint board (via ``meridian.db.board_snapshot``) and
this session's own still-active file claims rather than replaying a stored
``/goal`` body -- so a recovering agent sees ground truth, and never
duplicates or force-completes work another session already owns.

Every column here is, by construction, hosted-safe: :mod:`meridian.session_recovery`
validates every string field (``client_type``, refs, metadata) through
``secret_redaction.check_for_secrets`` plus the same absolute-path/secret-shaped
rejection ``capability_manifest.py`` uses, and rejects any of the four known
host-local identity keys (``local_session_id``/``bridge_id``/``environment_id``/
``argv``) if a caller tries to smuggle one into ``metadata``. The table itself
declares no column for any of them -- there is nothing here to redact after
the fact because the sensitive values are never accepted into this module at
all; they stay in the caller's own host-local snapshot
(``meridian.session_recovery.write_local_recovery_snapshot``).
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from meridian import session_recovery as model

from . import board_snapshot as board_snapshot_module
from . import locks as locks_module

_RECOVERY_COLUMNS = (
    "id", "project_id", "meridian_session_id", "local_ref_id", "client_type",
    "transport", "lifecycle_status", "verified_resumable", "last_heartbeat_at",
    "last_checkpoint_ref", "last_handoff_ref", "sprint_version", "metadata_json",
    "registered_by_session_id", "created_at", "updated_at",
)


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(zip(_RECOVERY_COLUMNS, row))


def _decode(
    row: Any,
    *,
    stale_after_seconds: int = model.DEFAULT_STALE_AFTER_SECONDS,
    dead_after_seconds: int = model.DEFAULT_DEAD_AFTER_SECONDS,
) -> dict[str, Any] | None:
    result = _row_to_dict(row)
    if result is None:
        return None
    raw = result.pop("metadata_json", None)
    if isinstance(raw, dict):
        result["metadata"] = raw
    else:
        try:
            result["metadata"] = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            result["metadata"] = {}
    result["verified_resumable"] = bool(result.get("verified_resumable"))
    result["liveness"] = model.classify_liveness(
        result.get("last_heartbeat_at"), result.get("lifecycle_status"),
        stale_after_seconds=stale_after_seconds, dead_after_seconds=dead_after_seconds,
    )
    return result


async def _require_session(db: Any, project_id: str, session_id: str) -> None:
    if not session_id:
        raise ValueError("session_id is required for session-recovery writes")
    async with db.execute(
        "SELECT id FROM sessions WHERE id = ? AND project_id = ?",
        (session_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise ValueError(f"session {session_id!r} does not belong to project {project_id!r}")


async def _find(
    db: Any, project_id: str, *,
    recovery_id: str | None = None, meridian_session_id: str | None = None,
) -> dict[str, Any] | None:
    if bool(recovery_id) == bool(meridian_session_id):
        raise ValueError("pass exactly one of recovery_id or session_id")
    column, value = (
        ("id", recovery_id) if recovery_id else ("meridian_session_id", meridian_session_id)
    )
    async with db.execute(
        f"SELECT {', '.join(_RECOVERY_COLUMNS)} FROM session_recovery_registry "
        f"WHERE project_id = ? AND {column} = ?",
        (project_id, value),
    ) as cur:
        return _decode(await cur.fetchone())


async def register_session_recovery(
    db: Any,
    project_id: str,
    session_id: str,
    *,
    transport: str,
    client_type: str | None = None,
    lifecycle_status: str = "active",
    verified_resumable: bool = False,
    local_ref_id: str | None = None,
    last_checkpoint_ref: str | None = None,
    last_handoff_ref: str | None = None,
    sprint_version: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or reaffirm (upsert + heartbeat) one session's recovery record.

    ``session_id`` is the Meridian ``sessions.id`` this record describes --
    NOT a local transcript id, RC bridge id, or environment id; those never
    reach this function's arguments at all. A second call for the same
    ``(project_id, session_id)`` pair updates in place and refreshes
    ``last_heartbeat_at`` (the same "register == reaffirm" upsert semantics
    ``meridian.db.external_jobs.register_external_job`` already established
    for ``job_key``), so a client can call this on every heartbeat tick
    without needing a separate heartbeat tool.

    ``verified_resumable`` is a caller-computed boolean (see
    ``meridian.session_recovery.build_resume_recipe`` -- run LOCALLY by the
    caller against its own host-local identity, never against anything this
    function sees) -- a plain, non-identifying bool is safe to persist
    hosted-side even though the identity that produced it is not.
    """
    await _require_session(db, project_id, session_id)
    transport_v = model.validate_transport(transport)
    status_v = model.validate_lifecycle_status(lifecycle_status)
    client_type_v = model.validate_hosted_text(client_type, field="client_type", max_chars=64)
    local_ref_id_v = (
        model.validate_hosted_text(local_ref_id, field="local_ref_id", max_chars=128)
        or model.new_local_ref_id()
    )
    last_checkpoint_ref_v = model.validate_hosted_text(
        last_checkpoint_ref, field="last_checkpoint_ref", max_chars=200
    )
    last_handoff_ref_v = model.validate_hosted_text(
        last_handoff_ref, field="last_handoff_ref", max_chars=200
    )
    sprint_version_v = model.validate_hosted_text(
        sprint_version, field="sprint_version", max_chars=100
    )
    metadata_v = model.validate_hosted_metadata(metadata)
    now = model.utcnow_iso()

    existing = await _find(db, project_id, meridian_session_id=session_id)
    if existing is not None:
        updates: dict[str, Any] = {
            "local_ref_id": local_ref_id_v,
            "client_type": client_type_v,
            "transport": transport_v,
            "lifecycle_status": status_v,
            "verified_resumable": 1 if verified_resumable else 0,
            "last_heartbeat_at": now,
            "sprint_version": (
                sprint_version_v if sprint_version is not None else existing.get("sprint_version")
            ),
            "last_checkpoint_ref": (
                last_checkpoint_ref_v if last_checkpoint_ref is not None
                else existing.get("last_checkpoint_ref")
            ),
            "last_handoff_ref": (
                last_handoff_ref_v if last_handoff_ref is not None
                else existing.get("last_handoff_ref")
            ),
            "metadata_json": json.dumps(
                metadata_v if metadata is not None else (existing.get("metadata") or {}),
                ensure_ascii=False, sort_keys=True,
            ),
            "updated_at": now,
        }
        assignments = ", ".join(f"{column} = ?" for column in updates)
        await db.execute(
            f"UPDATE session_recovery_registry SET {assignments} WHERE project_id = ? AND id = ?",
            [*updates.values(), project_id, existing["id"]],
        )
        row = await _find(db, project_id, recovery_id=existing["id"])
        assert row is not None
        await db.commit()
        return row

    rid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO session_recovery_registry "
        "(id, project_id, meridian_session_id, local_ref_id, client_type, transport, "
        "lifecycle_status, verified_resumable, last_heartbeat_at, last_checkpoint_ref, "
        "last_handoff_ref, sprint_version, metadata_json, registered_by_session_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rid, project_id, session_id, local_ref_id_v, client_type_v, transport_v,
            status_v, 1 if verified_resumable else 0, now, last_checkpoint_ref_v,
            last_handoff_ref_v, sprint_version_v,
            json.dumps(metadata_v, ensure_ascii=False, sort_keys=True), session_id,
        ),
    )
    row = await _find(db, project_id, recovery_id=rid)
    assert row is not None
    await db.commit()
    return row


async def get_session_recovery(
    db: Any,
    project_id: str,
    *,
    session_id: str | None = None,
    recovery_id: str | None = None,
    stale_after_seconds: int = model.DEFAULT_STALE_AFTER_SECONDS,
    dead_after_seconds: int = model.DEFAULT_DEAD_AFTER_SECONDS,
) -> dict[str, Any] | None:
    """Read-only: one recovery record, with ``liveness`` freshly computed."""
    row = await _find(db, project_id, recovery_id=recovery_id, meridian_session_id=session_id)
    if row is None:
        return None
    row["liveness"] = model.classify_liveness(
        row.get("last_heartbeat_at"), row.get("lifecycle_status"),
        stale_after_seconds=stale_after_seconds, dead_after_seconds=dead_after_seconds,
    )
    return row


async def list_resumable_sessions(
    db: Any,
    project_id: str,
    *,
    sprint_version: str | None = None,
    include_dead: bool = False,
    stale_after_seconds: int = model.DEFAULT_STALE_AFTER_SECONDS,
    dead_after_seconds: int = model.DEFAULT_DEAD_AFTER_SECONDS,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List this project's recovery registry, newest heartbeat first.

    Classification (``liveness``) is computed fresh on every read rather than
    stored, so a row's classification is always relative to "now" -- never
    goes stale itself. ``include_dead=False`` (default) omits rows that
    classify as ``dead`` so a resuming session's own listing call surfaces
    only genuinely resumable/stale/unknown candidates by default.
    """
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if sprint_version is not None:
        clauses.append("sprint_version = ?")
        params.append(sprint_version)
    limit = max(1, min(int(limit), 500))
    params.append(limit)
    async with db.execute(
        f"SELECT {', '.join(_RECOVERY_COLUMNS)} FROM session_recovery_registry "
        f"WHERE {' AND '.join(clauses)} ORDER BY last_heartbeat_at DESC, id DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        record = _decode(
            row, stale_after_seconds=stale_after_seconds, dead_after_seconds=dead_after_seconds,
        )
        if record is None:
            continue
        if not include_dead and record["liveness"] == "dead":
            continue
        results.append(record)
    return results


async def build_recovery_continuation(
    db: Any,
    project_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Re-derive the LIVE board for a recovering session -- never replays a
    stored ``/goal`` body.

    Reuses the SAME canonical, byte-stable snapshot
    (:func:`meridian.db.board_snapshot.build_board_snapshot`) that
    ``generate_handoff(mode='delta')``/``build_continuation_manifest``
    (862f6522) already build their own resume payload from, scoped to this
    recovery record's own ``sprint_version`` (mirrors
    ``build_continuation_manifest``'s own version-resolution rule: an
    explicit scope wins, ``None`` means unscoped/every version).

    ``active_file_claims`` reports the file paths THIS session (identified
    by its Meridian ``session_id``, not any local/RC/environment id) still
    holds an active write lock on
    (:func:`meridian.db.locks.get_session_file_claims`) -- surfaced so a
    recovering agent can see what it already owns instead of re-claiming or
    duplicating it. This function only reads; it never releases a claim,
    reassigns an item, or completes anything -- preserving active claims and
    avoiding duplicate/force-complete action is the CALLER's responsibility,
    informed by this response, exactly as the sprint item requires.
    """
    recovery = await _find(db, project_id, meridian_session_id=session_id)
    if recovery is None:
        raise ValueError(
            f"no session recovery record for session {session_id!r} in project {project_id!r}"
        )
    version = recovery.get("sprint_version")
    board = await board_snapshot_module.build_board_snapshot(db, project_id, version=version)
    active_file_claims = await locks_module.get_session_file_claims(db, session_id)
    return {
        "recovery": recovery,
        "board": {
            "version_filter": board.get("version_filter"),
            "item_count": board.get("item_count"),
            "items": board.get("items"),
            "revision_hash": board.get("revision_hash"),
            "blocker_summary": board.get("blocker_summary"),
        },
        "active_file_claims": active_file_claims,
        "guidance": (
            "This re-derives the LIVE board -- do not replay a stale /goal body. "
            "active_file_claims lists what THIS session still holds a write lock "
            "on; never reclaim, duplicate, or force-complete an item another "
            "session already owns (status not in pending/todo)."
        ),
    }
