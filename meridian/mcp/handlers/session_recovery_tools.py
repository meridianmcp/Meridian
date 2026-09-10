"""MCP tool handlers for the cross-client session recovery registry (cdd0ef6c).

Sibling of ``meridian/mcp/handlers/session_tools.py``'s external-job handlers
(``handle_register_external_job`` et al.) -- same shape (thin wrapper over a
``meridian/db/*.py`` module, refresh a host-local JSON snapshot, return a
bounded task-log description) applied to session recovery instead of
external jobs. See ``meridian/session_recovery.py`` for the identity-
separation rationale this whole feature exists to enforce.
"""
from __future__ import annotations

from typing import Any


async def handle_register_session_recovery(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: register_session_recovery (cdd0ef6c).

    ``local_identity`` (optional) carries HOST-LOCAL identity only --
    ``local_session_id`` / ``bridge_id`` / ``environment_id`` / ``argv`` /
    ``local_transcript_path``. It is used ONLY to (a) compute
    ``verified_resumable`` and a resume recipe locally
    (:func:`meridian.session_recovery.build_resume_recipe`) and (b) refresh
    the host-local snapshot file. None of it is passed to the DB layer --
    ``meridian.db.session_recovery.register_session_recovery`` has no
    parameter for any of it, so there is no code path by which it could
    reach the hosted ``session_recovery_registry`` table even by mistake.
    """
    from meridian.db import session_recovery as recovery_db  # noqa: PLC0415
    from meridian import session_recovery as model  # noqa: PLC0415

    project_id = args["project_id"]
    session_id = args["session_id"]
    transport = args["transport"]
    client_type = args.get("client_type")
    local_identity = args.get("local_identity") or {}

    resume_recipe, blocked_reason = model.build_resume_recipe(
        transport, client_type, local_identity
    )
    verified_resumable = resume_recipe is not None

    record = await recovery_db.register_session_recovery(
        db, project_id, session_id,
        transport=transport, client_type=client_type,
        lifecycle_status=args.get("lifecycle_status", "active"),
        verified_resumable=verified_resumable,
        local_ref_id=args.get("local_ref_id"),
        last_checkpoint_ref=args.get("last_checkpoint_ref"),
        last_handoff_ref=args.get("last_handoff_ref"),
        sprint_version=args.get("sprint_version"),
        metadata=args.get("metadata"),
    )

    # Refresh the host-local snapshot with the sensitive identity + resolved
    # recipe, keyed by the same opaque local_ref_id now stored (safely) on
    # the hosted row. read-modify-write: other sessions' entries in the same
    # project snapshot are preserved.
    snapshot = model.read_local_recovery_snapshot(data_dir, project_id) or {}
    local_records: dict[str, Any] = dict(snapshot.get("records") or {})
    local_records[record["local_ref_id"]] = {
        **local_identity,
        "meridian_session_id": session_id,
        "transport": transport,
        "resume_recipe": resume_recipe,
        "resume_blocked_reason": blocked_reason,
        "updated_at": model.utcnow_iso(),
    }
    snapshot_write = model.write_local_recovery_snapshot(data_dir, project_id, local_records)

    return {
        "recovery": record,
        "resume_recipe": resume_recipe,
        "resume_blocked_reason": blocked_reason,
        "task_log": {"description": model.build_log_description("registered", record)},
        "local_snapshot": snapshot_write,
    }


async def handle_list_resumable_sessions(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: list_resumable_sessions (cdd0ef6c). Read-only.

    Every returned row is hosted-safe as-is (no local-only fields exist on
    this table to begin with) -- classification (``liveness``) is computed
    fresh against "now" on every call, never stored.
    """
    from meridian.db import session_recovery as recovery_db  # noqa: PLC0415

    project_id = args["project_id"]
    rows = await recovery_db.list_resumable_sessions(
        db, project_id,
        sprint_version=args.get("sprint_version"),
        include_dead=args.get("include_dead", False),
        limit=args.get("limit", 100),
    )
    return {"sessions": rows, "count": len(rows)}


async def handle_get_session_recovery(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_session_recovery (cdd0ef6c).

    ``continuation`` (default on) re-derives the LIVE board and this
    session's own active file claims via
    :func:`meridian.db.session_recovery.build_recovery_continuation` --
    never a stored ``/goal`` body. ``resume_recipe`` is resolved from the
    CALLING PROCESS's own host-local snapshot file (never from the hosted
    row) and is ``None`` whenever this machine's snapshot has no matching
    entry -- e.g. a fresh machine that never registered this session locally
    sees the hosted classification/metadata but no recipe, which is the
    correct, safe default rather than a guess.
    """
    from meridian.db import session_recovery as recovery_db  # noqa: PLC0415
    from meridian import session_recovery as model  # noqa: PLC0415

    project_id = args["project_id"]
    session_id = args.get("session_id")
    recovery_id = args.get("recovery_id")
    if not session_id and not recovery_id:
        return {"error": "pass session_id or recovery_id"}

    record = await recovery_db.get_session_recovery(
        db, project_id, session_id=session_id, recovery_id=recovery_id,
    )
    if record is None:
        return {"error": "no session recovery record found in this project"}

    continuation: Any = None
    if args.get("include_continuation", True):
        try:
            continuation = await recovery_db.build_recovery_continuation(
                db, project_id, record["meridian_session_id"],
            )
        except Exception as exc:  # noqa: BLE001 -- continuation is best-effort enrichment
            continuation = {"error": str(exc)}

    resume_recipe = None
    snapshot = model.read_local_recovery_snapshot(data_dir, project_id)
    if snapshot:
        entry = (snapshot.get("records") or {}).get(record.get("local_ref_id"))
        if entry:
            resume_recipe = entry.get("resume_recipe")

    return {
        "recovery": record,
        "resume_recipe": resume_recipe,
        "continuation": continuation,
    }
