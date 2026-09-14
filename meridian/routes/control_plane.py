"""07f753b2 — CONTROL-PLANE-UI-IMPLEMENT: permission-aware relationship,
proposal, plaintext artifact/manifest, and provenance views.

Implements the accepted CONTROL-PLANE-UI-PLAN (workspace note 8e06a475,
attached to sprint item ba61cc39). Three READ-ONLY endpoints, deliberately
reusing existing data paths rather than building a second ledger:

* ``GET /control-plane/relationships`` — workspace -> project -> one-level
  subproject, reusing ``db.list_projects`` + the SAME listing-only scope
  filter ``GET /projects`` already applies (``_scoped_project_ids_for_request``).
  Tenant isolation is inherited for free: ``_db(request)`` already resolves
  to the caller's own per-tenant database connection (hosted mode is one
  dedicated Postgres DB per tenant; self-host is the single local DB) — there
  is no ``tenant_id`` column to filter on ``projects`` because isolation is
  physical, not row-level. See ``meridian/_deps.py::_db`` /
  ``_scoped_project_ids_for_request`` and ``meridian/server.py``'s
  ``project_scope_enforcement`` middleware, both confirmed by inspection
  during this item's prospecting pass (not assumed).

* ``GET /control-plane/proposals`` — reuses ``db.get_workspace_proposals``
  (already paginated and already supports an optional ``project_id`` filter,
  a8afd8f9). Design note 8e06a475 flagged a confirmed, still-open gap: that
  function's ``project_id`` filter is optional, so a project-scoped member
  calling it with no ``project_id`` sees every proposal in the workspace.
  This endpoint does not wait on that product decision — see
  ``_require_scoped_project_id`` below for the conservative mitigation it
  applies on its own read surface only.

* ``GET /control-plane/artifacts`` — run-manifest / provenance / artifact
  registry records from the ``meridian-outputs`` extension's local, per-machine
  JSON ledgers (``<outputs_dir>/.meridian-outputs-cache/``). Two real
  constraints drove this endpoint's shape, both confirmed by inspection
  (not assumed) during this item's prospecting pass:

  1. ``meridian_outputs`` is a genuinely separate, optionally-installed
     package (``extensions/meridian-outputs/pyproject.toml``, not a
     dependency of the core ``meridian`` pixi environment) — ``import
     meridian_outputs`` raises ``ModuleNotFoundError`` in a plain core
     checkout. This endpoint therefore imports it lazily, inside the request
     handler, and degrades to an honest ``available: false`` envelope
     instead of ever hard-failing the whole dashboard process over an
     optional extension.
  2. Design note 8e06a475's SECURITY FINDING: in hosted mode, the only path
     from the cloud-hosted dashboard process to a user's local
     ``.meridian-outputs-cache/`` is the per-tenant WebSocket tunnel proxy
     (``meridian/routes/tunnel.py``), which the note found takes its
     ``tenant_id`` purely from the URL path with no caller-tenant binding
     check — a confirmed, separate security gap the note explicitly says
     "must be fixed before any hosted-mode version ships" of exactly this
     kind of browser. This endpoint honors that: hosted mode gets a clear,
     documented "not available yet" response, never a silent proxy call
     through the flagged path. Self-hosted mode needs no tunnel at all — the
     dashboard process already runs on the same machine as the repo — so it
     is fully functional today.

Every record returned here also goes through ``_redact_local_paths`` (and,
for registry records, the extension's own ``strip_local_metadata``) so a
machine-local absolute path is never forwarded to a caller — the same
"never a secret, never a bare local path" rule this repo's
``capability_manifest.py`` already enforces for manifest fields.

No new mutation actions are added here (READ-ONLY by design, per the sprint
item's explicit instruction) — moving/promoting/deleting a record still goes
through the existing, already role-gated routes/tools
(``POST /projects/{id}/parent``, ``promote_proposal``, the existing delete
routes). c4c74141 (the planned local-first temporary-artifact registry) had
not landed as of this item's prospecting pass (confirmed via
``get_sprint_items`` — still ``pending``), so the artifacts endpoint is built
against the CURRENT ``meridian_outputs`` ledger shape; that integration point
is called out explicitly above and in
``docs/control-plane-relationship-and-artifact-view.md`` rather than blocked
on.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .._deps import (
    _db,
    _enforcement_context,
    _get_tenant_from_request,
    _hosted_mode,
    _scoped_project_ids_for_request,
)
from .. import db as db_module
from ..db.workspace import ProposalSchemaError
from ..repo_scope import RepoScopeError, validate_repo_scope
from ..roles import PERM_READ, has_perm
from ..secret_redaction import redact

router = APIRouter()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


async def _require_read(request: Request) -> "dict[str, Any] | None":
    """Resolve the workspace this request is actually scoped to and enforce
    PERM_READ against the caller's role IN THAT WORKSPACE.

    18c488b6/prospecting note — this deliberately uses ``_enforcement_context``
    rather than ``_get_tenant_from_request`` + ``_require_workspace_perm``
    (the pattern ``meridian/routes/export.py::delete_account`` uses for a
    self-scoped action). ``_get_tenant_from_request`` only ever answers "who
    is the caller", never "which workspace are they viewing" — a caller who
    is a project-scoped VIEWER of someone else's workspace (via
    ``X-Workspace-Tenant-Id``) is still the OWNER of their own tiny personal
    workspace. Checking ``has_perm`` against the caller's OWN tenant id would
    therefore ALWAYS pass (an owner can always read their own data), silently
    defeating this endpoint's cross-workspace scope enforcement — confirmed
    by reading ``_deps.py::_db``/``_enforcement_context``/
    ``_scoped_project_ids_for_request`` together, which is the SAME
    resolution ``_db(request)`` itself uses to decide which physical/tenant
    DB to open. ``_enforcement_context`` returns ``None`` for the "no gate
    needed" cases (self-hosted, demo, unauthenticated, or the caller's own
    workspace — all implicitly "owner" of what ``_db(request)`` will open),
    matching every other read path in this codebase.
    """
    ctx = await _enforcement_context(request)
    if ctx is not None:
        _caller_email, active_tenant_id, role = ctx
        if not has_perm(role, PERM_READ):
            raise HTTPException(
                status_code=403,
                detail=f"Workspace role '{role}' lacks permission '{PERM_READ}'",
            )
        return {"id": active_tenant_id}
    return await _get_tenant_from_request(request)


def _clamp_page(limit: Any, offset: Any, *, max_limit: int = 100) -> "tuple[int, int]":
    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        limit_i = 20
    try:
        offset_i = int(offset)
    except (TypeError, ValueError):
        offset_i = 0
    return max(1, min(limit_i, max_limit)), max(0, offset_i)


# ---------------------------------------------------------------------------
# Relationship explorer — workspace -> project -> one-level subproject
# ---------------------------------------------------------------------------

def _public_project(p: "dict[str, Any]") -> "dict[str, Any]":
    """Explicit field allowlist — never forward a raw DB row. Mirrors the
    ``Project`` pydantic model's own field set (``meridian/models.py``) plus
    one derived, display-only field."""
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "status": p.get("status") or "active",
        "priority": p.get("priority") or "P2",
        "parent_project_id": p.get("parent_project_id"),
        "created_at": p.get("created_at"),
        "relationship_scope": "subproject" if p.get("parent_project_id") else "project",
    }


@router.get("/control-plane/relationships")
async def get_control_plane_relationships(request: Request) -> "dict[str, Any]":
    """Permission-aware workspace -> project -> one-level-subproject tree.

    Returns exactly the projects ``GET /projects`` would show this caller
    (same scoping helper, same tenant-isolated db connection), annotated with
    an explicit ``relationship_scope`` label per row and an overall
    ``scope`` label ("workspace" for an owner/workspace-wide member, "project"
    for a project-scoped member) so the UI never has to guess what it is
    looking at. Nesting subprojects under their parent is left to the
    dashboard's existing, already-unit-tested ``flattenHierarchy`` helper
    (``dashboard-subprojects.ts``) — deliberately not re-implemented here,
    per the design note's reuse recommendation.
    """
    tenant = await _require_read(request)
    db = await _db(request)
    projects = await db_module.list_projects(db)
    scoped = await _scoped_project_ids_for_request(request)
    scope = "workspace" if scoped is None else "project"
    if scoped is not None:
        allowed = set(scoped)
        projects = [p for p in projects if p.get("id") in allowed]
    return {
        "scope": scope,
        "scoped_project_ids": sorted(scoped) if scoped is not None else None,
        "tenant_id": tenant["id"] if tenant else None,
        "generated_at": _utc_now_iso(),
        "projects": [_public_project(p) for p in projects],
    }


# ---------------------------------------------------------------------------
# Proposals — paginated, redacted, read-only
# ---------------------------------------------------------------------------

def _public_proposal(row: "dict[str, Any]") -> "dict[str, Any]":
    """Explicit field allowlist + defensive secret redaction on the two
    free-text fields (a pasted proposal body is human-authored and could
    contain an accidentally-pasted credential)."""
    return {
        "id": row.get("id"),
        "title": redact(str(row.get("title") or "")),
        "body": redact(str(row.get("body") or "")),
        "status": row.get("status"),
        "scope_type": row.get("scope_type") or "workspace",
        "project_id": row.get("project_id"),
        "tags": row.get("tags"),
        "slug": row.get("slug"),
        "nickname": row.get("nickname"),
        "created_at": row.get("created_at"),
        "last_activity_at": row.get("last_activity_at"),
    }


@router.get("/control-plane/proposals")
async def get_control_plane_proposals(
    request: Request,
    project_id: "str | None" = None,
    status: "str | None" = None,
    tag: "str | None" = None,
    limit: int = 20,
    offset: int = 0,
) -> "dict[str, Any]":
    """Paginated, read-only, redacted view of workspace/project proposals.

    8e06a475 — ``db.get_workspace_proposals`` treats ``project_id`` as purely
    optional and applies no additional scope check of its own; calling it
    with ``project_id`` omitted returns every proposal regardless of caller
    scope. That is a confirmed, still-open product question (see this
    module's docstring), not something this item's own read surface should
    duplicate. So: a caller who is themselves project-scoped
    (``_scoped_project_ids_for_request`` returns a non-``None`` list) MUST
    name one of their own scoped ``project_id`` values here — a 403, not a
    silent workspace-wide fallback.
    """
    tenant = await _require_read(request)
    db = await _db(request)
    scoped = await _scoped_project_ids_for_request(request)
    if scoped is not None and (not project_id or project_id not in set(scoped)):
        raise HTTPException(
            status_code=403,
            detail=(
                "Project-scoped members must request a specific in-scope "
                "project_id; workspace-wide proposal listing is not "
                "available to this role."
            ),
        )
    limit_i, offset_i = _clamp_page(limit, offset)
    tenant_id = tenant["id"] if tenant else None
    try:
        rows = await db_module.get_workspace_proposals(
            db,
            status=status,
            tag=tag,
            tenant_id=tenant_id,
            limit=limit_i + 1,
            offset=offset_i,
            project_id=project_id,
        )
    except ProposalSchemaError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    has_more = len(rows) > limit_i
    rows = rows[:limit_i]
    return {
        "scope": "project" if project_id else "workspace",
        "project_id": project_id,
        "items": [_public_proposal(r) for r in rows],
        "limit": limit_i,
        "offset": offset_i,
        "has_more": has_more,
    }


# ---------------------------------------------------------------------------
# Artifacts / run manifests / provenance — self-hosted only (see module docstring)
# ---------------------------------------------------------------------------

_ARTIFACT_RECORD_KINDS = ("run_manifest", "provenance", "registry")

#: Field names that carry a local filesystem path in a meridian-outputs
#: ledger record. ``artifact_registry.strip_local_metadata`` already handles
#: registry records' ``local_paths`` entries; this covers the simpler
#: top-level path-ish fields run-manifest/provenance records carry (their
#: ledgers are keyed by a caller-supplied path/id that is not guaranteed to
#: be relative).
_LOCAL_PATH_LIKE_KEYS = (
    "path", "local_path", "abs_path", "absolute_path", "script_path", "repo_path",
)


def _redact_local_paths(record: "dict[str, Any]") -> "dict[str, Any]":
    """Defense in depth: an absolute-looking string under a path-shaped key
    is replaced with just its basename. Never mutates ``record``."""
    out = dict(record)
    for key in _LOCAL_PATH_LIKE_KEYS:
        val = out.get(key)
        if isinstance(val, str) and (
            val.startswith("/") or val.startswith("\\\\") or (len(val) > 1 and val[1] == ":")
        ):
            out[key] = os.path.basename(val.replace("\\", "/")) or "(redacted path)"
    return out


def _hosted_artifacts_unavailable(limit: int, offset: int) -> "dict[str, Any]":
    return {
        "available": False,
        "mode": "hosted",
        "reason": (
            "Hosted-mode artifact/manifest browsing is deferred pending a "
            "security fix for the tunnel proxy's tenant binding (design note "
            "8e06a475 attached to sprint item ba61cc39). Self-hosted mode is "
            "fully supported without a tunnel."
        ),
        "items": [], "total": 0, "limit": limit, "offset": offset, "has_more": False,
    }


def _extension_unavailable(limit: int, offset: int) -> "dict[str, Any]":
    return {
        "available": False,
        "mode": "self_hosted",
        "reason": "The meridian-outputs extension is not installed in this environment.",
        "items": [], "total": 0, "limit": limit, "offset": offset, "has_more": False,
    }


@router.get("/control-plane/artifacts")
async def get_control_plane_artifacts(
    request: Request,
    outputs_dir: "str | None" = None,
    kind: "str | None" = None,
    limit: int = 50,
    offset: int = 0,
) -> "dict[str, Any]":
    """Paginated, read-only, redacted view of run manifests, provenance
    status, and artifact-registry records. See module docstring for why this
    is self-hosted-only in this pass, and for the lazy-import rationale.
    """
    await _require_read(request)
    limit_i, offset_i = _clamp_page(limit, offset, max_limit=200)
    if kind is not None and kind not in _ARTIFACT_RECORD_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {_ARTIFACT_RECORD_KINDS} or omitted",
        )

    if _hosted_mode():
        return _hosted_artifacts_unavailable(limit_i, offset_i)

    try:
        from meridian_outputs import annotate as _annotate  # noqa: PLC0415
        from meridian_outputs import artifact_registry as _artifact_registry  # noqa: PLC0415
        from meridian_outputs import run_manifest as _run_manifest  # noqa: PLC0415
    except ImportError:
        return _extension_unavailable(limit_i, offset_i)

    resolved_dir = outputs_dir or os.getcwd()
    try:
        validated = validate_repo_scope(resolved_dir, cwd=os.getcwd())
    except RepoScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    outputs_dir_str = str(validated)

    records: "list[dict[str, Any]]" = []
    if kind in (None, "run_manifest"):
        for rec in _run_manifest.list_run_manifests(outputs_dir_str):
            records.append({"record_kind": "run_manifest", **_redact_local_paths(rec)})
    if kind in (None, "provenance"):
        for rec in _annotate.list_provenance(outputs_dir_str):
            records.append({"record_kind": "provenance", **_redact_local_paths(rec)})
    if kind in (None, "registry"):
        for rec in _artifact_registry.list_artifacts(outputs_dir_str):
            stripped = _artifact_registry.strip_local_metadata(rec)
            records.append({"record_kind": "registry", **_redact_local_paths(stripped)})

    def _sort_key(r: "dict[str, Any]") -> "tuple[str, str]":
        ident = r.get("path") or r.get("run_id") or r.get("artifact_id") or ""
        return (str(r.get("record_kind", "")), str(ident))

    records.sort(key=_sort_key)
    total = len(records)
    page = records[offset_i: offset_i + limit_i]
    return {
        "available": True,
        "mode": "self_hosted",
        "outputs_dir_basename": os.path.basename(outputs_dir_str) or outputs_dir_str,
        "items": page,
        "total": total,
        "limit": limit_i,
        "offset": offset_i,
        "has_more": offset_i + limit_i < total,
    }
