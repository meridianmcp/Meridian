"""Profile-layer settings routes — REST surface over the layered
hosted_default/workspace/user/project/session profile contract (PROFILE-5,
0bec79a7). See ``meridian.profile_contract`` for the pure contract and
``meridian.db.profile_layers`` for persistence/resolution — this module adds
no new business logic, only HTTP plumbing over that existing layer,
mirroring the MCP tools registered in ``meridian/mcp_tools.py``
(list/get/save/clone/activate/reset_profile_layer, get_effective_profile).

Top-level (NOT nested under ``/projects/{project_id}/...``) because profile
layers aren't uniformly project-scoped: only the ``project`` and ``session``
scope types are tied to one project_id — ``hosted_default``/``workspace``/
``user`` are not. The one genuinely project-anchored operation
(``GET /projects/{project_id}/effective-profile``) stays nested, matching
its MCP counterpart ``get_effective_profile``'s own project_id-centric shape.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import _deps
from .._deps import _db
from .. import db as db_module
from .. import profile_contract as profile_contract_module

router = APIRouter()


def _scope_type_is(scope_type: Any, name: str) -> bool:
    """True when ``scope_type`` names the ``name`` layer.

    Normalised exactly like ``profile_contract.normalize_scope_type`` (strip +
    lowercase) so ``"PROJECT"`` or ``" session "`` cannot slip past the scope
    check and still be stored as the project / session layer.
    """
    return isinstance(scope_type, str) and scope_type.strip().lower() == name


def _is_project_layer(scope_type: Any) -> bool:
    """True when ``scope_type`` names the ``project`` layer (keyed by a project id)."""
    return _scope_type_is(scope_type, "project")


def _is_session_layer(scope_type: Any) -> bool:
    """True when ``scope_type`` names the ``session`` layer (keyed by a session id)."""
    return _scope_type_is(scope_type, "session")


async def _require_layer_scopes_in_scope(
    request: Request, *layers: "tuple[Any, Any]"
) -> None:
    """RT-TI-005 (wave 2) — ``project`` and ``session`` profile layers are keyed by an id
    that belongs to exactly one project.

    ``/profile-layers/...`` is top-level (outside the /projects/{uuid}
    middleware), so a project-scoped workspace member could otherwise read,
    overwrite, reset or clone another project's executor/capability profile by
    naming its id (pinned decision 6fe5210c). Every ``(scope_type, scope_id)``
    pair that is a project layer must name a project inside the caller's scope;
    every session layer must name a session whose project (``sessions.project_id``)
    is inside it (403, same message as the middleware; an unknown session id gets
    the same 403 as a foreign one, so existence is not leaked). The other scope
    types (hosted_default, workspace, user) are not project-keyed and are left
    unchanged -- whether a project-scoped member may touch them is an open
    product question.

    The scope is only resolved when a project or session layer is involved, and
    the session lookup only for a project-scoped caller, so the other layer types
    and every unscoped caller pay nothing extra.
    """
    project_ids = [
        str(sid).strip() for stype, sid in layers if _is_project_layer(stype) and str(sid).strip()
    ]
    session_ids = [
        str(sid).strip() for stype, sid in layers if _is_session_layer(stype) and str(sid).strip()
    ]
    if not project_ids and not session_ids:
        return
    scoped = await _deps._scoped_project_ids_for_request(request)
    if scoped is None:
        return
    for pid in project_ids:
        _deps._deny_unless_in_scope(scoped, pid)
    for sid in session_ids:
        _deps._deny_unless_in_scope(scoped, await _deps._session_project_id(request, sid))


async def _in_scope_session_ids(
    request: Request, scoped: "list[str]", session_ids: "list[str]"
) -> "set[str]":
    """The subset of ``session_ids`` whose session belongs to a project in ``scoped``.

    One ``IN (...)`` lookup per chunk instead of a query per layer row; an id that
    names no session is simply absent from the result (so it is not in scope).
    """
    allowed = set(scoped)
    found: set[str] = set()
    ids = list(dict.fromkeys(session_ids))
    db = await _db(request)
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        placeholders = ", ".join("?" for _ in chunk)
        async with db.execute(
            f"SELECT id, project_id FROM sessions WHERE id IN ({placeholders})",
            tuple(chunk),
        ) as cur:
            for row in await cur.fetchall():
                if row["project_id"] in allowed:
                    found.add(row["id"])
    return found


@router.get("/profile-layers")
async def list_profile_layers_route(
    request: Request, scope_type: str | None = None
) -> list[dict[str, Any]]:
    """List every persisted profile_layers row, optionally filtered by
    ``scope_type``. Mirrors the ``list_profile_layers`` MCP tool.

    RT-TI-005 (wave 2) — for a project-scoped caller the ``project`` layers of
    projects outside their scope and the ``session`` layers of sessions of other
    projects are dropped (the other layer types are not project-keyed and are
    returned as before). The scope is resolved only when the listing can contain
    a project or session layer, i.e. never for ``?scope_type=workspace`` /
    ``user`` / ``hosted_default``.
    """
    db = await _db(request)
    try:
        rows = await db_module.list_profile_layers(db, scope_type)
    except (profile_contract_module.ProfileContractError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if scope_type is not None and not (
        _is_project_layer(scope_type) or _is_session_layer(scope_type)
    ):
        return rows
    scoped = await _deps._scoped_project_ids_for_request(request)
    if scoped is not None:
        allowed = set(scoped)
        session_ids = [
            str(r.get("scope_id")) for r in rows if _is_session_layer(r.get("scope_type"))
        ]
        in_scope_sessions = (
            await _in_scope_session_ids(request, scoped, session_ids) if session_ids else set()
        )

        def _visible(row: dict[str, Any]) -> bool:
            if _is_project_layer(row.get("scope_type")):
                return row.get("scope_id") in allowed
            if _is_session_layer(row.get("scope_type")):
                return row.get("scope_id") in in_scope_sessions
            return True  # hosted_default / workspace / user: not project-keyed

        rows = [r for r in rows if _visible(r)]
    return rows


@router.get("/profile-layers/{scope_id}/revisions")
async def get_profile_layer_revisions_route(
    scope_id: str, request: Request, limit: int = 50
) -> list[dict[str, Any]]:
    """Return the hosted_default revision/audit history for one scope_id,
    newest first. Mirrors the ``get_profile_layer_revisions`` MCP tool —
    a non-hosted_default scope_id always returns [].

    Registered BEFORE ``get_profile_layer_route`` below: both are GET routes
    with the same 2-segment shape (``/profile-layers/<a>/<b>``), and
    Starlette matches routes in registration order — the literal-suffixed
    ``.../revisions`` route must be tried first or the fully-wildcarded
    ``{scope_type}/{scope_id}`` route below would always shadow it (e.g.
    ``GET /profile-layers/global/revisions`` would resolve as
    ``scope_type="global", scope_id="revisions"`` instead).
    """
    if not scope_id.strip():
        raise HTTPException(status_code=400, detail="scope_id is required")
    db = await _db(request)
    try:
        return await db_module.get_profile_layer_revisions(db, scope_id, limit=limit)
    except (profile_contract_module.ProfileContractError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/profile-layers/{scope_type}/{scope_id}")
async def get_profile_layer_route(
    scope_type: str, scope_id: str, request: Request
) -> dict[str, Any]:
    """Return the raw, single-layer profile for one (scope_type, scope_id).
    A scope with no persisted row gets an empty profile back, never a 404 —
    mirrors the ``get_profile_layer`` MCP tool."""
    if not scope_type.strip():
        raise HTTPException(status_code=400, detail="scope_type is required")
    if not scope_id.strip():
        raise HTTPException(status_code=400, detail="scope_id is required")
    await _require_layer_scopes_in_scope(request, (scope_type, scope_id))  # RT-TI-005
    db = await _db(request)
    try:
        return await db_module.get_profile_layer(db, scope_type, scope_id)
    except (profile_contract_module.ProfileContractError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/profile-layers/{scope_type}/{scope_id}")
async def save_profile_layer_route(
    scope_type: str, scope_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Validate and persist one layer's profile — wholesale-replaces this
    scope's stored fields/reset_fields (not a merge). Body:
    ``{fields, reset_fields, provenance, expected_revision, actor}``, all
    optional. Mirrors the ``save_profile_layer`` MCP tool. Returns 409 (not
    500) when ``expected_revision`` is stale.
    """
    if not scope_type.strip():
        raise HTTPException(status_code=400, detail="scope_type is required")
    if not scope_id.strip():
        raise HTTPException(status_code=400, detail="scope_id is required")
    await _require_layer_scopes_in_scope(request, (scope_type, scope_id))  # RT-TI-005
    db = await _db(request)
    try:
        return await db_module.set_profile_layer(
            db,
            scope_type,
            scope_id,
            fields=body.get("fields"),
            reset_fields=body.get("reset_fields"),
            provenance=body.get("provenance"),
            expected_revision=body.get("expected_revision"),
            actor=body.get("actor"),
        )
    except profile_contract_module.ProfileStaleRevisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (profile_contract_module.ProfileContractError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/profile-layers/{scope_type}/{scope_id}")
async def reset_profile_layer_route(
    scope_type: str, scope_id: str, request: Request
) -> dict[str, Any]:
    """Delete a scope's entire profile-layer row. Returns 200 with the
    post-reset empty-layer dict (not 204) — matches the
    ``reset_profile_layer`` MCP tool's return shape so REST and MCP never
    disagree about it. Idempotent: resetting an already-empty scope is a
    no-op, not an error.
    """
    if not scope_type.strip():
        raise HTTPException(status_code=400, detail="scope_type is required")
    if not scope_id.strip():
        raise HTTPException(status_code=400, detail="scope_id is required")
    await _require_layer_scopes_in_scope(request, (scope_type, scope_id))  # RT-TI-005
    db = await _db(request)
    try:
        return await db_module.reset_profile_layer(db, scope_type, scope_id)
    except (profile_contract_module.ProfileContractError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/profile-layers/{scope_type}/{scope_id}/clone")
async def clone_profile_layer_route(
    scope_type: str, scope_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Copy one layer's fields/reset_fields/provenance onto another scope.
    Body: ``{target_scope_type, target_scope_id, actor}`` — the first two
    required. Mirrors the ``clone_profile_layer`` MCP tool, including its
    "source layer must exist" rejection.
    """
    if not scope_type.strip():
        raise HTTPException(status_code=400, detail="scope_type is required")
    if not scope_id.strip():
        raise HTTPException(status_code=400, detail="scope_id is required")
    target_scope_type = str(body.get("target_scope_type") or "").strip()
    target_scope_id = str(body.get("target_scope_id") or "").strip()
    if not target_scope_type:
        raise HTTPException(status_code=400, detail="target_scope_type is required")
    if not target_scope_id:
        raise HTTPException(status_code=400, detail="target_scope_id is required")
    # RT-TI-005 — both ends of a clone: reading another project's layer (source)
    # and overwriting another project's layer (target) are each refused.
    await _require_layer_scopes_in_scope(
        request, (scope_type, scope_id), (target_scope_type, target_scope_id)
    )
    db = await _db(request)
    try:
        return await db_module.clone_profile_layer(
            db, scope_type, scope_id, target_scope_type, target_scope_id,
            actor=body.get("actor"),
        )
    except (profile_contract_module.ProfileContractError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/profile-layers/{scope_id}/activate")
async def activate_profile_layer_route(
    scope_id: str, request: Request, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Advance a hosted_default layer's lifecycle to 'active'. hosted_default
    only — no scope_type in the path, matching the ``activate_profile_layer``
    MCP tool's signature (it doesn't take scope_type either, since it's
    hardcoded to hosted_default). Body: ``{actor}`` optional.
    """
    if not scope_id.strip():
        raise HTTPException(status_code=400, detail="scope_id is required")
    db = await _db(request)
    try:
        return await db_module.transition_hosted_default_lifecycle(
            db, scope_id, "active", actor=(body or {}).get("actor"),
        )
    except (profile_contract_module.ProfileContractError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/projects/{project_id}/effective-profile")
async def get_effective_profile_route(
    project_id: str,
    request: Request,
    session_id: str | None = None,
    user_scope_id: str | None = None,
    workspace_scope_id: str = "singleton",
    hosted_default_scope_id: str = "global",
) -> dict[str, Any]:
    """Resolve and return the merged profile for a project across every
    applicable layer — hosted_default -> workspace -> user -> project ->
    session. Mirrors the ``get_effective_profile`` MCP tool. This is the
    one project-anchored route in this module — see the module docstring.

    RT-TI-005 (wave 2) — ``project_id`` is vetted by the middleware, but
    ``session_id`` merges that session's layer into the result and
    ``db.get_effective_profile`` never checks the session belongs to the project.
    A project-scoped caller therefore gets a 403 for a session of any other
    project (or an unknown one); unscoped callers are unchanged.
    """
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if session_id and session_id.strip():
        scoped = await _deps._scoped_project_ids_for_request(request)
        if scoped is not None and await _deps._session_project_id(
            request, session_id.strip()
        ) != project_id:
            raise HTTPException(status_code=403, detail="Project is outside your access scope.")
    try:
        return await db_module.get_effective_profile(
            db, project_id,
            session_id=session_id,
            user_scope_id=user_scope_id,
            workspace_scope_id=workspace_scope_id,
            hosted_default_scope_id=hosted_default_scope_id,
        )
    except (profile_contract_module.ProfileContractError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
