"""Handoff generation route — extracted from server.py."""
from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .._deps import _db, _data_dir
from .. import db as db_module
from .. import handoff as handoff_module
from ..models import HandoffResult

router = APIRouter()


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
    return {
        # a5e8aa74 — route through the same shared helper the MCP handler/stdio
        # transports use so all transports share one raw-text contract (this
        # endpoint was already raw; this just makes that guarantee explicit and
        # keeps the three transports from being able to drift independently).
        "path": path, "content": handoff_module.format_handoff_mcp_content(content),
        "mode": "planner",
        "capability_contract": capability_contract,
        "proposal_evidence": proposal_evidence,
        "docx_integrity": docx_integrity,
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
    mode = handoff_module.resolve_handoff_mode(
        body.get("mode"),
        session_id if isinstance(session_id, str) else None,
    )
    # b8f89491 — optional explicit sprint-version scope, same contract as the
    # MCP path: wins over the session's own stored sprint_version; None (no
    # version, no session, or a session with no stored scope) is unchanged
    # unscoped behaviour.
    _version = body.get("version")
    if isinstance(_version, str) and not _version.strip():
        _version = None
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
    # 98aaccf4 — machine-readable effective capability contract; best-effort,
    # never breaks the mandatory handoff.
    capability_contract = await handoff_module.build_effective_capability_contract(
        db, project_id, board_stale=_board_stale,
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
        "proposal_evidence": proposal_evidence,
        "docx_integrity": docx_integrity,
    }
