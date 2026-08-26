"""Handoff generation route — extracted from server.py."""
from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .._deps import _db, _data_dir
from .. import db as db_module
from .. import handoff as handoff_module
from ..db import ai_log as ai_log_module
from ..models import HandoffResult

router = APIRouter()


async def _log_correction_event(
    db: Any,
    project_id: str,
    event_type: str,
    *,
    session_id: "str | None",
    correlation_id: "str | None",
    payload: dict[str, Any],
    payload_schema: str,
) -> None:
    """79491e26 — best-effort durable trace for a planner/executor
    corrective handoff, so :func:`meridian.handoff.build_run_timeline_for_handoff`
    can reconstruct it alongside every other execution event this project
    has captured, instead of a correction being visible ONLY via a separate
    ``handoff_corrections`` lookup.

    Fire-and-forget by design, matching every other enrichment step in the
    handoff pipeline: an ai_log write problem (a pre-9e83be4a DB missing the
    ``ai_log_events`` table, a transient secret-shaped-payload rejection
    from :func:`meridian.secret_redaction.check_for_secrets`, anything else)
    must never make a legitimate correction unrecordable — this endpoint's
    own failure contract stays exactly what it was before this function
    existed (``HandoffCorrectionError`` only). ``correlation_id`` is set to
    the correction's ``source_handoff_id`` so every event tied to one source
    handoff's correction lifecycle (recorded, then regenerated) groups under
    one correlation id on the reconstructed timeline.
    """
    try:
        await ai_log_module.AiLogStore(db, project_id).append(
            event_type,
            "session" if session_id else "system",
            actor_id=session_id,
            session_id=session_id,
            correlation_id=correlation_id,
            source="routes.handoff",
            payload=payload,
            payload_schema=payload_schema,
        )
    except Exception:  # noqa: BLE001 — best-effort, never blocks the correction
        pass


@router.get("/projects/{project_id}/handoff/planner")
async def planner_handoff_endpoint(
    project_id: str, request: Request
) -> dict[str, Any]:
    """GET the planner-optimised handoff for a project.

    Returns strategic context (north star, decisions, notes, open HITLs,
    pending sprint items, recent tasks) as plain markdown. Intended for
    pasting into a claude.ai planning chat — excludes mechanical executor
    details like file paths and test commands.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    db = await _db(request)
    data_dir = _data_dir(request)
    try:
        path, content, _ = await asyncio.wait_for(
            handoff_module.generate_handoff(
                db, project_id, data_dir, mode="planner"
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="planner handoff timed out")
    # 98aaccf4 — machine-readable effective capability contract; best-effort,
    # never breaks the planner handoff.
    capability_contract = await handoff_module.build_effective_capability_contract(
        db, project_id,
    )
    # 89a06e40 — machine-readable effective profile identity/generation;
    # best-effort, never breaks the planner handoff. See pinned decision
    # ee7bccc9 for why the tunnel/connector routes deliberately do NOT call
    # this same helper.
    profile_binding = await handoff_module.build_effective_profile_binding(
        db, project_id,
    )
    # 6cdc5df3 — machine-readable proposal-to-evidence linkage; best-effort,
    # never breaks the planner handoff.
    proposal_evidence = await handoff_module.build_proposal_evidence_for_handoff(
        db, project_id,
    )
    # d09c29fe — machine-readable DOCX-integrity gate; best-effort, never
    # breaks the planner handoff. Tied to the proposal evidence above so a
    # proposal-linked .docx artifact is gated too (6cdc5df3).
    docx_integrity = await handoff_module.build_docx_integrity_gate_for_handoff(
        db, project_id, proposal_evidence=proposal_evidence,
    )
    # 9154aa9a — machine-readable pointer to the latest durable executor
    # report, mirroring the capability_contract/proposal_evidence pattern
    # above; best-effort, never breaks the planner handoff. The prose
    # rendering of this same data already lives in `content` (see
    # handoff_module._generate_planner_handoff's "Latest executor report"
    # section) — this is the structured projection for a machine reader.
    try:
        _reports = await db_module.list_executor_reports(db, project_id, limit=1)
        latest_executor_report = _reports[0] if _reports else None
    except Exception:  # noqa: BLE001
        latest_executor_report = None
    return {
        # a5e8aa74 — route through the same shared helper the MCP handler/stdio
        # transports use so all transports share one raw-text contract (this
        # endpoint was already raw; this just makes that guarantee explicit and
        # keeps the three transports from being able to drift independently).
        "path": path, "content": handoff_module.format_handoff_mcp_content(content),
        "mode": "planner",
        "capability_contract": capability_contract,
        "profile_binding": profile_binding,
        "proposal_evidence": proposal_evidence,
        "docx_integrity": docx_integrity,
        "latest_executor_report": latest_executor_report,
    }


@router.post("/projects/{project_id}/handoff", response_model=HandoffResult)
async def generate_handoff_endpoint(
    project_id: str, request: Request
) -> dict[str, Any]:
    """Render and write the handoff file for a project."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    session_id = body.get("session_id")
    _session_id = session_id if isinstance(session_id, str) else None
    # aec043cb — same in-memory role registry the MCP transports use (lazy
    # import: meridian.mcp.handler imports meridian.server, which imports
    # this module at startup, so a module-level import here would be
    # circular — mirrors the existing lazy-import pattern this module's
    # own generate_handoff dispatch already uses elsewhere, e.g. handler.py's
    # `from ..routes import tunnel as _tunnel_mod`). Best-effort: a failed
    # import must never break this endpoint, it only means the role hint
    # falls back to None and resolve_handoff_mode uses its own safe default.
    _session_role: "str | None" = None
    try:
        from ..mcp import handler as _mcp_handler  # noqa: PLC0415
        _session_role = _mcp_handler._session_role_hint(_session_id)
    except Exception:  # noqa: BLE001 — role hint is best-effort only
        _session_role = None
    mode = handoff_module.resolve_handoff_mode(
        body.get("mode"),
        _session_id,
        session_role=_session_role,
    )
    # b8f89491 — optional explicit sprint-version scope, same contract as the
    # MCP path: wins over the session's own stored sprint_version; None (no
    # version, no session, or a session with no stored scope) is unchanged
    # unscoped behaviour.
    _version = body.get("version")
    if isinstance(_version, str) and not _version.strip():
        _version = None
    # 45f519a0/8a883f60/eb8b6894 — same gap as the stdio transport had: this
    # REST body was only ever read for session_id/mode/version, silently
    # dropping force_include_ids/strict_evidence/strict_pointer_evidence even
    # though the MCP HTTP dispatch (handler.py) already threads all of these.
    _force_include_ids: list[str] | None = None
    _raw_fii = body.get("force_include_ids")
    if isinstance(_raw_fii, list):
        _force_include_ids = [str(x) for x in _raw_fii if x]
    # 94f48e4d — same transport-parity gap: thread selected_item_ids through
    # the REST body too. See handoff_module.generate_handoff's own docstring
    # for the fail-closed contract (differs from force_include_ids above).
    _selected_item_ids: list[str] | None = None
    _raw_sel = body.get("selected_item_ids")
    if isinstance(_raw_sel, list):
        _selected_item_ids = [str(x) for x in _raw_sel if x]
    _strict_evidence = bool(body.get("strict_evidence"))
    _strict_pointer_evidence = bool(body.get("strict_pointer_evidence"))
    # 3cab355a — mirror handler.py's out-param: one entry per requested
    # force_include_ids id that failed validation (unknown/cross-project/
    # cross-version/not-pending). See handoff.generate_handoff's
    # force_include_rejected docstring.
    _force_include_rejected: list[dict[str, Any]] = []
    # ecc8b280 — same gap as force_include_ids/strict_evidence above: thread
    # the continuation-gate args through the REST body too.
    _checkpoint = bool(body.get("checkpoint"))
    _strict_continuation = bool(body.get("strict_continuation"))
    _continuation_status: dict[str, Any] = {}
    skip_summary = not os.environ.get("ANTHROPIC_API_KEY")
    db = await _db(request)
    data_dir = _data_dir(request)
    _board_stale = False
    try:
        path, content, _ = await asyncio.wait_for(
            handoff_module.generate_handoff(
                db, project_id, data_dir,
                skip_ai_summary=skip_summary,
                mode=mode,
                session_id=session_id if isinstance(session_id, str) else None,
                version=_version if isinstance(_version, str) else None,
                force_include_ids=_force_include_ids,
                selected_item_ids=_selected_item_ids,
                strict_evidence=_strict_evidence,
                strict_pointer_evidence=_strict_pointer_evidence,
                force_include_rejected=_force_include_rejected,
                checkpoint=_checkpoint,
                strict_continuation=_strict_continuation,
                continuation_status=_continuation_status,
            ),
            timeout=90.0,
        )
    except asyncio.TimeoutError:
        path, content = await handoff_module._generate_handoff_l0(db, project_id, data_dir)
        mode = "full"
        # 98aaccf4 — the L0 emergency fallback means this handoff's own
        # board/profile snapshot is known incomplete; a contract built
        # alongside it must not silently report executable=true.
        _board_stale = True
    except handoff_module.HandoffEvidenceRequired as exc:
        # 8a883f60 — strict_evidence=True and a best-effort capability was
        # failed/degraded: fail CLOSED, mirroring the MCP HTTP dispatch's
        # structured refusal instead of a generic 500.
        raise HTTPException(
            status_code=422,
            detail={
                "error": "HANDOFF_EVIDENCE_BLOCKED",
                "project_id": project_id,
                "evidence_status": exc.evidence_status,
                "evidence_errors": exc.errors,
                "message": str(exc),
            },
        ) from exc
    except handoff_module.HandoffSelectionError as exc:
        # 94f48e4d — selected_item_ids given and at least one requested id
        # failed validation: fail CLOSED, mirroring the MCP HTTP dispatch's
        # structured refusal instead of a generic 500.
        raise HTTPException(
            status_code=422,
            detail={
                "error": "HANDOFF_SELECTION_BLOCKED",
                "project_id": project_id,
                "selection_rejected": exc.rejected,
                "message": str(exc),
            },
        ) from exc
    except handoff_module.HandoffScopeNonExecutable as exc:
        # fb82e51f — selected_item_ids validated cleanly but every requested
        # id was independently excluded from the claimable batch (no
        # prospecting evidence, backburner, manual, or wave-gate pending):
        # fail CLOSED rather than persist a handoff that renders successfully
        # yet has zero executable items for its own declared scope.
        raise HTTPException(
            status_code=422,
            detail={
                "error": "HANDOFF_SCOPE_NON_EXECUTABLE",
                "project_id": project_id,
                "requested_ids": exc.requested_ids,
                "excluded_requested": exc.excluded,
                "message": str(exc),
            },
        ) from exc
    except handoff_module.HandoffContinuationRequired as exc:
        # ecc8b280 — strict_continuation=True, not checkpoint=True, and
        # actionable work remains: fail CLOSED, mirroring the MCP HTTP
        # dispatch's structured refusal instead of a generic 500.
        raise HTTPException(
            status_code=422,
            detail={
                "error": "HANDOFF_CONTINUATION_BLOCKED",
                "project_id": project_id,
                "continuation_status": exc.continuation_state,
                "message": str(exc),
            },
        ) from exc
    except handoff_module.HandoffStaleReferenceError as exc:
        # ee8a6af1 — unconditional fail-closed: a depends_on edge on the live
        # board points at an id that doesn't resolve for this project/version
        # scope. Mirrors the MCP HTTP dispatch's structured refusal instead
        # of a generic 500.
        raise HTTPException(
            status_code=422,
            detail={
                "error": "STALE_REFERENCE",
                "project_id": exc.project_id,
                "version": exc.version,
                "stale_references": exc.stale_references,
                "message": str(exc),
            },
        ) from exc
    # 98aaccf4 — machine-readable effective capability contract; best-effort,
    # never breaks the mandatory handoff.
    capability_contract = await handoff_module.build_effective_capability_contract(
        db, project_id, board_stale=_board_stale,
    )
    # 89a06e40 — machine-readable effective profile identity/generation;
    # best-effort, never breaks the mandatory handoff. See pinned decision
    # ee7bccc9 for why the tunnel/connector routes deliberately do NOT call
    # this same helper. session_id is threaded through the same way it is
    # into generate_handoff() above.
    profile_binding = await handoff_module.build_effective_profile_binding(
        db, project_id, session_id=session_id if isinstance(session_id, str) else None,
    )
    # 6cdc5df3 — machine-readable proposal-to-evidence linkage; best-effort,
    # never breaks the mandatory handoff.
    proposal_evidence = await handoff_module.build_proposal_evidence_for_handoff(
        db, project_id,
    )
    # d09c29fe — machine-readable DOCX-integrity gate; best-effort, never
    # breaks the mandatory handoff. Tied to the proposal evidence above so a
    # proposal-linked .docx artifact is gated too (6cdc5df3).
    docx_integrity = await handoff_module.build_docx_integrity_gate_for_handoff(
        db, project_id, proposal_evidence=proposal_evidence,
    )
    return {
        # a5e8aa74 — same shared helper as the planner endpoint above and both
        # MCP transports; see format_handoff_mcp_content's docstring.
        "path": path, "content": handoff_module.format_handoff_mcp_content(content),
        "mode": mode,
        "capability_contract": capability_contract,
        "profile_binding": profile_binding,
        "proposal_evidence": proposal_evidence,
        "docx_integrity": docx_integrity,
        "force_include_rejected": _force_include_rejected,
        "continuation_status": _continuation_status,
    }


@router.post("/projects/{project_id}/handoff/corrections")
async def record_handoff_correction_endpoint(
    project_id: str, request: Request
) -> dict[str, Any]:
    """3af86d28 — record a corrective handoff for a blocked executor session.

    REST mirror of the MCP ``record_handoff_correction`` tool — see
    ``meridian.handoff.record_handoff_correction`` /
    ``regenerate_handoff_correction`` for the full contract. Pass
    ``regenerate: true`` in the body to also repair pointers, invalidate the
    source handoff, and produce a new deterministic revision in this same
    call.
    """
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    source_handoff_id = body.get("source_handoff_id")
    blocker_classification = body.get("blocker_classification")
    if not source_handoff_id or not blocker_classification:
        raise HTTPException(
            status_code=422,
            detail="source_handoff_id and blocker_classification are required",
        )
    try:
        correction = await handoff_module.record_handoff_correction(
            db, project_id,
            source_handoff_id=source_handoff_id,
            blocker_classification=blocker_classification,
            session_id=body.get("session_id"),
            investigation_evidence=body.get("investigation_evidence"),
            added_pointers=body.get("added_pointers"),
            removed_pointers=body.get("removed_pointers"),
            superseded_pointers=body.get("superseded_pointers"),
            changed_resources=body.get("changed_resources"),
            requested_scope=body.get("requested_scope"),
            version=body.get("version"),
            source_token=body.get("source_token"),
            idempotency_key=body.get("idempotency_key"),
            status=body.get("status") or "draft",
        )
    except handoff_module.HandoffCorrectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # 79491e26 — see _log_correction_event's docstring: best-effort, never
    # blocks a legitimate correction on an ai_log write problem.
    await _log_correction_event(
        db, project_id, "handoff.correction_recorded",
        session_id=correction.get("session_id"),
        correlation_id=correction.get("source_handoff_id"),
        payload={
            "correction_id": correction.get("id"),
            "source_handoff_id": correction.get("source_handoff_id"),
            "blocker_classification": correction.get("blocker_classification"),
            "status": correction.get("status"),
            "version": correction.get("version"),
        },
        payload_schema="handoff_correction_recorded@1",
    )
    if not body.get("regenerate"):
        return {"correction": correction, "regenerated": False}
    data_dir = _data_dir(request)
    try:
        result = await handoff_module.regenerate_handoff_correction(
            db, project_id, correction["id"], body.get("output_dir") or data_dir,
            session_id=body.get("session_id"),
            mode=body.get("mode") or "full",
        )
    except handoff_module.HandoffCorrectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # 79491e26 — same best-effort durable trace for the regeneration outcome.
    await _log_correction_event(
        db, project_id, "handoff.correction_regenerated",
        session_id=correction.get("session_id"),
        correlation_id=correction.get("source_handoff_id"),
        payload={
            "correction_id": correction.get("id"),
            "new_handoff_id": result.get("new_handoff_id"),
            "amended": result.get("amended"),
            "invalidated_source": bool(result.get("invalidated_source")),
        },
        payload_schema="handoff_correction_regenerated@1",
    )
    return result


@router.post("/projects/{project_id}/handoff/accept")
async def accept_handoff_endpoint(
    project_id: str, request: Request
) -> dict[str, Any]:
    """1bd5e810 — REST mirror of the MCP ``accept_handoff`` tool — see
    ``meridian.handoff.accept_handoff_envelope`` for the full contract. Every
    field in the body is optional and independently gated; an omitted check
    is skipped, never failed. Same underlying function as the MCP/stdio
    transports, so all three produce identical results for identical input.
    """
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}

    presented_body = body.get("presented_body")
    body_for_check = (
        handoff_module.strip_goal_token_banner(presented_body)
        if isinstance(presented_body, str)
        else None
    )
    live_items = body.get("live_items")
    return await handoff_module.accept_handoff_envelope(
        db, project_id,
        goal_token=body.get("goal_token"),
        presented_body=body_for_check,
        live_items=live_items if isinstance(live_items, list) else None,
        expected_board_revision=body.get("expected_board_revision"),
        expected_required_tools_hash=body.get("expected_required_tools_hash"),
        required_tools=body.get("required_tools"),
        available_tools=body.get("available_tools"),
        # 22f2604d — same identity-binding params the MCP transport passes
        # through; keeps REST/MCP/stdio producing identical results for
        # identical input (this endpoint's own docstring contract).
        expected_repo_path=body.get("expected_repo_path"),
        delivery_source=body.get("delivery_source") or "chat_paste",
    )


@router.get("/projects/{project_id}/handoff/corrections/latest")
async def load_handoff_correction_endpoint(
    project_id: str, request: Request
) -> dict[str, Any]:
    """3af86d28 — load a corrective handoff directly (never reconstruct from notes).

    REST mirror of ``meridian.handoff.load_handoff_correction``. Optional
    query params ``correction_id`` / ``source_handoff_id`` scope the lookup;
    with neither, returns the project's single latest correction (any
    status). ``{correction: null}`` when the project has no corrections.
    """
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    correction_id = request.query_params.get("correction_id")
    source_handoff_id = request.query_params.get("source_handoff_id")
    try:
        correction = await handoff_module.load_handoff_correction(
            db, project_id,
            correction_id=correction_id,
            source_handoff_id=source_handoff_id,
        )
    except handoff_module.HandoffCorrectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"correction": correction}


@router.get("/projects/{project_id}/wave-summary/{wave_id}")
async def get_wave_summary_endpoint(
    project_id: str, wave_id: str, request: Request
) -> dict[str, Any]:
    """bbb447ec — the authoritative, immutable completion record for one wave.

    REST mirror of ``meridian.db.get_wave_summary`` — a read-only projection
    of a wave's persisted completion summary (item outcomes, commits/changed
    resources, test receipts, blockers, exclusions, tool availability,
    handoff status), keyed by ``wave_id`` (the same value as
    ``sprint_items.wave`` / ``wave_gate_results.wave_label``, e.g.
    ``"wave-5"``). Optional ``?version=`` query param scopes to one
    sprint-version bucket (unscoped/'' bucket by default — same convention as
    ``board_snapshot_revisions``); optional ``?wave_run_id=`` narrows to one
    specific run/attempt. Pass ``?include_history=1`` to also return every
    prior (superseded) row in the correction chain, oldest first — the full
    audit trail, mirroring ``get_wave_run_events(include_superseded=True)``.

    ``{"summary": null}`` (never a 404) when no summary has ever been
    recorded for the bucket — a wave with no summary yet is a normal state,
    not an error, matching ``load_handoff_correction``'s own "null body, not
    404" convention just above.
    """
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    version = request.query_params.get("version") or None
    wave_run_id = request.query_params.get("wave_run_id") or None
    include_history = (request.query_params.get("include_history") or "").lower() in (
        "1", "true", "yes",
    )
    summary = await db_module.get_wave_summary(
        db, project_id, wave_id, version=version, wave_run_id=wave_run_id,
    )
    result: dict[str, Any] = {
        "project_id": project_id,
        "wave_id": wave_id,
        "version": version,
        "summary": summary,
    }
    if include_history:
        result["history"] = await db_module.get_wave_summary_history(
            db, project_id, wave_id, version=version, wave_run_id=wave_run_id,
        )
    return result
