"""W1-K — Derivative-document (DOCX) provenance tooling MCP handlers.

Three handlers, mirroring :mod:`meridian.mcp.handlers.research_tools`'s
established convention exactly (read that module first): each catches
``ValueError`` from the validation/persistence layer
(:mod:`meridian.db.docx_derivatives`) and returns ``{"error": ...}`` rather
than letting it propagate.
"""
from __future__ import annotations

from typing import Any


async def handle_register_docx_derivative(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: register_docx_derivative (W1-K).

    Always creates a NEW ``status='candidate'`` row -- see
    :func:`meridian.db.docx_derivatives.register_docx_derivative`'s docstring.
    """
    from meridian.db import docx_derivatives as derivative_db  # noqa: PLC0415

    project_id = args["project_id"]
    try:
        derivative = await derivative_db.register_docx_derivative(
            db, project_id, args["session_id"],
            source_path=args.get("source_path"),
            derivative_path=args.get("derivative_path"),
            source_content_hash=args.get("source_content_hash"),
            derivative_content_hash=args.get("derivative_content_hash"),
            generating_tool=args.get("generating_tool"),
            generated_at=args.get("generated_at"),
            notes=args.get("notes"),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"derivative": derivative}


async def handle_verify_docx_diff(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: verify_docx_diff (W1-K). Read-side comparison, but persists
    the verdict onto the derivative row as an audit trail -- see
    :func:`meridian.db.docx_derivatives.verify_docx_diff`'s docstring.
    """
    from meridian.db import docx_derivatives as derivative_db  # noqa: PLC0415

    project_id = args["project_id"]
    try:
        result = await derivative_db.verify_docx_diff(
            db, project_id,
            derivative_id=args["derivative_id"],
            current_source_content_hash=args.get("current_source_content_hash"),
            current_derivative_content_hash=args.get("current_derivative_content_hash"),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return result


async def handle_promote_docx_candidate(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: promote_docx_candidate (W1-K).

    Requires the derivative's stored status to be 'candidate' (idempotent,
    never an error, when it is already 'accepted'). See
    :func:`meridian.db.docx_derivatives.promote_docx_candidate`'s docstring
    for the full state-machine contract.
    """
    from meridian.db import docx_derivatives as derivative_db  # noqa: PLC0415

    project_id = args["project_id"]
    try:
        result = await derivative_db.promote_docx_candidate(
            db, project_id, args.get("session_id"),
            derivative_id=args["derivative_id"],
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return result
