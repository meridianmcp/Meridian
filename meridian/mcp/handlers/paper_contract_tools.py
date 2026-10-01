"""7c96d41b — paper_contract MCP handlers: the versioned editorial-intent
document (working title, audience, scope in/out, required sections,
style_guide, citation_style, word_limit, constraints) for Meridian's
manuscript/paper editorial tooling line.

Six handlers, mirroring :mod:`meridian.mcp.handlers.docx_derivative_tools`'s
established convention exactly (read that module first): each calls straight
through to :mod:`meridian.db.paper_contract` (already re-exported on
``meridian.db``, so this module reaches it the same way
:mod:`meridian.mcp.handlers.project_tools` reaches ``profile_layers`` --
via ``db_module``, not a submodule import) and catches ``ValueError`` from
that validation/persistence layer, returning ``{"error": ...}`` rather than
letting it propagate. ``content`` is passed through as a plain dict, exactly
as :func:`meridian.db.paper_contract.create_paper_contract_revision`'s own
docstring specifies -- shape checking is that function's minimal non-empty
check, not a Pydantic pass in this layer (no handler in this codebase
constructs :mod:`meridian.models` classes; those are wire-format
documentation, not a runtime validation step).

``get_current_paper_contract_content`` is the one handler here with no 1:1
db-layer function: a convenience read composing
``get_paper_contract``/``get_paper_contract_by_key`` with
``get_paper_contract_revision`` to resolve straight to "what is this paper's
CURRENTLY APPROVED editorial content" in one call -- the thing an editorial
session actually wants most often, rather than making every caller
re-derive it from a bare ``current_revision_id`` pointer. Mirrors
``get_profile_layer``'s "missing scope returns empty, never an error"
contract: an unset contract or a contract with no approved revision yet
returns ``content: None``, not an error.
"""
from __future__ import annotations

from typing import Any

from meridian import db as db_module


async def handle_create_paper_contract(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: create_paper_contract (7c96d41b).

    Creates the stable ``(project_id, paper_key)`` identity a revision
    ledger hangs off of -- contains no editorial content itself; see
    create_paper_contract_revision for that. Not idempotent on paper_key --
    a repeat call raises, matching :func:`meridian.db.paper_contract.create_paper_contract`.
    """
    project_id = args["project_id"]
    try:
        contract = await db_module.create_paper_contract(
            db, project_id, args["paper_key"], args["title"],
            created_by_human_id=args.get("created_by_human_id"),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"contract": contract}


async def handle_get_paper_contract(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_paper_contract (7c96d41b).

    Read-only: look up by ``contract_id`` or by its natural key
    ``paper_key`` (exactly one of the two, alongside the always-required
    ``project_id``). A nonexistent contract returns ``{"contract": None}``,
    never an error.
    """
    project_id = args["project_id"]
    contract_id = (args.get("contract_id") or "").strip()
    paper_key = (args.get("paper_key") or "").strip()
    if not contract_id and not paper_key:
        return {"error": "either contract_id or paper_key is required"}
    try:
        if contract_id:
            contract = await db_module.get_paper_contract(db, project_id, contract_id)
        else:
            contract = await db_module.get_paper_contract_by_key(db, project_id, paper_key)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"contract": contract}


async def handle_create_paper_contract_revision(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: create_paper_contract_revision (7c96d41b).

    Proposes a new PENDING revision of the editorial-intent content (working
    title, audience, scope, style_guide, etc.) -- never binding until a
    human calls approve_paper_contract_revision. See
    :func:`meridian.db.paper_contract.create_paper_contract_revision`.
    """
    project_id = args["project_id"]
    try:
        revision = await db_module.create_paper_contract_revision(
            db, project_id, args["contract_id"], args["content"],
            change_summary=args.get("change_summary"),
            created_by=args.get("created_by"),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"revision": revision}


async def handle_list_paper_contract_revisions(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: list_paper_contract_revisions (7c96d41b).

    Read-only: the full editorial-revision history for one contract, oldest
    first -- pending, approved, and rejected revisions alike.
    """
    project_id = args["project_id"]
    try:
        revisions = await db_module.list_paper_contract_revisions(db, project_id, args["contract_id"])
    except ValueError as exc:
        return {"error": str(exc)}
    return {"revisions": revisions}


async def handle_approve_paper_contract_revision(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: approve_paper_contract_revision (7c96d41b).

    The human-approval gate: pins ``revision_id`` as its contract's binding
    ``current_revision_id``. Requires a non-empty ``approved_by_human_id`` --
    an unattributed approval would defeat the point of the gate. Idempotent
    when already approved; rejects a ``rejected`` revision (propose a new
    one instead). See
    :func:`meridian.db.paper_contract.approve_paper_contract_revision`.
    """
    project_id = args["project_id"]
    try:
        revision = await db_module.approve_paper_contract_revision(
            db, project_id, args["revision_id"], args["approved_by_human_id"],
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"revision": revision}


async def handle_get_current_paper_contract_content(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_current_paper_contract_content (7c96d41b).

    Read-only convenience: resolve straight to the content of the
    CURRENTLY APPROVED revision (the one snapshot an editorial session
    should treat as authoritative right now), by either ``contract_id`` or
    ``paper_key``. A contract that doesn't exist, or exists but has never
    had a revision approved (still ``status="draft"``), returns
    ``content: None`` -- never an error, mirroring get_profile_layer's
    "missing scope returns empty" contract.
    """
    project_id = args["project_id"]
    contract_id = (args.get("contract_id") or "").strip()
    paper_key = (args.get("paper_key") or "").strip()
    if not contract_id and not paper_key:
        return {"error": "either contract_id or paper_key is required"}
    try:
        if contract_id:
            contract = await db_module.get_paper_contract(db, project_id, contract_id)
        else:
            contract = await db_module.get_paper_contract_by_key(db, project_id, paper_key)
    except ValueError as exc:
        return {"error": str(exc)}
    if contract is None:
        return {"contract": None, "revision": None, "content": None}
    current_revision_id = contract.get("current_revision_id")
    if not current_revision_id:
        return {"contract": contract, "revision": None, "content": None}
    revision = await db_module.get_paper_contract_revision(db, project_id, current_revision_id)
    return {
        "contract": contract,
        "revision": revision,
        "content": revision["content"] if revision else None,
    }
