"""Per-tool handlers extracted from _handle_notes_decisions (ac4df52f).

Each function corresponds to exactly one MCP tool from the
``_handle_notes_decisions`` dispatch group in ``meridian/mcp/handler.py``.
The extraction is PURELY MECHANICAL: zero behaviour change, same tool names,
same arguments, same return values.  All comments and citations are
preserved verbatim from the original.

Callers (handler.py) assemble these into a dispatch table
(dict[str, Callable]) and invoke the matched function instead of walking
the original if/elif chain.

Helper functions that are shared by multiple handlers
(``_resolve_ingest_doc_store``, ``_persist_ingest_structure``,
``_workspace_scope_warning``) are imported from ``meridian.mcp.handler``
itself (where they live and are not part of the extracted set) via a
localised import inside each handler that needs them, keeping the import
graph acyclic and matching the ``# noqa: PLC0415`` pattern already
established by the project_tools extraction.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import meridian.server as _server
from meridian import db as db_module
from meridian._deps import validate_input_size, _MANUAL_NOTE_LINT
# 867317f6 — not re-exported through meridian.db's explicit named import
# list (meridian/db/__init__.py is a high-contention file outside this
# sprint item's declared scope), so import the new proposal error type
# directly from the submodule that defines it.
from meridian.db.workspace import ProposalSchemaError


def _hosted_mode() -> bool:
    """Deferred proxy to meridian.mcp.handler._hosted_mode.

    Tests monkeypatch ``meridian.mcp.handler._hosted_mode`` (that was the
    binding these tools used before the ac4df52f extraction). A direct
    ``from meridian._deps import _hosted_mode`` here would bind a separate,
    un-patchable reference, silently breaking hosted-mode guards that tests
    rely on. Importing the handler module lazily at call time (not at
    module load) keeps this correct without introducing a circular import.
    """
    from meridian.mcp import handler as _handler_mod  # noqa: PLC0415
    return _handler_mod._hosted_mode()


# ---------------------------------------------------------------------------
# Section 1: Decisions
# ---------------------------------------------------------------------------

async def handle_pin_decision(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: pin_decision.

    9149e132 — optional ``evidence`` param: when given, atomically attaches
    ONE typed, code-linked decision_evidence row to the just-created decision
    (see meridian.db.decision_evidence). Shape:
    ``{pointer: {source_type, targets:[...], label?}, text: str,
    assumptions?: str, applicability_scope?: str, confidence?: float,
    version?: str}`` — ``pointer`` and ``text`` are required for the evidence
    to be created; ``pointer`` is validated via the SAME generic pointer
    primitive (:mod:`meridian.pointers`) sprint_item_pointers already uses.
    Omitting ``evidence`` entirely is a complete no-op change from before
    this item — pin_decision's pre-existing behavior and return shape are
    unchanged. A malformed pointer degrades to an ``evidence_error`` field on
    the result rather than failing the whole pin_decision call — the
    decision itself is never lost because its evidence was malformed.
    """
    validate_input_size(args.get("title"), "decision title", 500)
    validate_input_size(args.get("body"), "decision body", 100_000)
    category = args.get("category", "TECHNICAL")
    result = await db_module.pin_decision(
        db, args["project_id"], args["title"], args["body"], category,
        priority=args.get("priority", "normal"),
        assumption=args.get("assumption"),
    )
    await _server._append_decision_to_md(args["title"], args["body"], category)
    evidence = args.get("evidence")
    if isinstance(evidence, dict) and isinstance(result, dict) and result.get("id"):
        pointer = evidence.get("pointer")
        evidence_text = evidence.get("text") or evidence.get("evidence")
        if pointer and evidence_text:
            validate_input_size(evidence_text, "decision evidence text", 100_000)
            validate_input_size(evidence.get("assumptions"), "decision evidence assumptions", 100_000)
            validate_input_size(
                evidence.get("applicability_scope"), "decision evidence applicability_scope", 100_000,
            )
            try:
                ev_row = await db_module.create_decision_evidence(
                    db, args["project_id"], result["id"], pointer, evidence_text,
                    assumptions=evidence.get("assumptions"),
                    applicability_scope=evidence.get("applicability_scope"),
                    confidence=evidence.get("confidence"),
                    version=evidence.get("version"),
                )
                result = {**result, "evidence": ev_row}
            except (ValueError, TypeError) as exc:
                # PointerValidationError is a ValueError subclass — a
                # malformed pointer/evidence shape never fails the whole
                # pin_decision call; the decision itself already committed.
                result = {**result, "evidence_error": str(exc)}
    return result


async def handle_update_decision(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: update_decision."""
    new_title = args.get("new_title")
    new_body = args.get("new_body")
    if new_title and new_body:
        return await db_module.supersede_pinned_decision(
            db, args["decision_id"], new_title, new_body, args.get("category"),
            priority=args.get("priority"),
        )
    result = await db_module.update_pinned_decision(
        db, args["decision_id"],
        body=args.get("body"),
        title=args.get("title"),
        category=args.get("category"),
        status=args.get("status"),
        superseded_by=args.get("superseded_by"),
        priority=args.get("priority"),
        assumption=args.get("assumption"),
        assumption_status=args.get("assumption_status"),
    )
    if result is None:
        raise ValueError("decision not found")
    return result


async def handle_validate_assumption(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: validate_assumption.

    8ec5493b — one-call assumption validation: stamp the decision's
    assumption_status, save a code-anchored finding note, and fire a
    blocking HITL on invalidation.
    """
    if "confirmed" not in args:
        return {"error": "validate_assumption requires 'confirmed' (bool)"}
    validate_input_size(args.get("finding"), "finding", 100_000)
    validate_input_size(args.get("file_path"), "file_path", 2_000)
    validate_input_size(args.get("symbol"), "symbol", 500)
    try:
        return await db_module.validate_assumption(
            db, args["decision_id"], args.get("finding") or "",
            bool(args.get("confirmed")),
            file_path=args.get("file_path"), symbol=args.get("symbol"),
            session_id=args.get("session_id"),
        )
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_get_pinned_decisions(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_pinned_decisions."""
    return await db_module.get_pinned_decisions(
        db, args["project_id"],
        include_superseded=bool(args.get("include_superseded", False)),
    )


async def handle_archive_decision(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: archive_decision."""
    deleted = await db_module.delete_pinned_decision(db, args["decision_id"])
    if not deleted:
        raise ValueError("decision not found")
    return {"deleted": True, "decision_id": args["decision_id"]}


# ---------------------------------------------------------------------------
# Section 1b: Proposal HITL gates (c6d13571)
#
# Typed, lane-blocking gates for materially ambiguous decisions — legal/IP,
# product scope, destructive operations, production deployment, human
# acceptance of a contradiction, and other materially ambiguous decisions.
# Distinct from decisions_pinned (informational) and decision_evidence (a
# typed pointer backing one decision): a gate BLOCKS its named affected
# items/pointers until a human explicitly resolves it. See
# meridian.proposal_gates for the full schema/state-machine docstring.
# ---------------------------------------------------------------------------

async def handle_add_proposal_gate(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: add_proposal_gate.

    Raises a new HITL gate — always starts ``state='blocked'`` (fail-safe)
    with no decision yet. ``category`` must be one of
    ``meridian.proposal_gates.GATE_CATEGORIES``; ``affected`` is a non-empty
    list of sprint_item_id strings and/or generic pointer objects (see
    ``meridian.proposal_gates.normalize_affected``).
    """
    validate_input_size(args.get("question"), "gate question", 100_000)
    validate_input_size(args.get("evidence"), "gate evidence", 100_000)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    try:
        return await db_module.create_proposal_gate(
            db, args["project_id"], args.get("category"), args.get("question"),
            args.get("affected"), args.get("evidence"),
            created_by=args.get("created_by") or args.get("session_id"),
            expires_at=args.get("expires_at"),
            reopen_policy=args.get("reopen_policy", "manual"),
        )
    except db_module.ProposalGateError as exc:
        return {"error": str(exc)}


async def handle_resolve_proposal_gate(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: resolve_proposal_gate.

    Records a human decision on a gate: the lane's new state
    (blocked/quarantined/allowed), the free-text decision, and the actor who
    decided. Refuses (``{"error": ...}``) an already-decided, unexpired gate
    — call ``reopen_proposal_gate`` first.
    """
    validate_input_size(args.get("decision"), "gate decision", 100_000)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    if not args.get("gate_id"):
        return {"error": "gate_id is required"}
    kwargs: dict[str, Any] = {}
    if "expires_at" in args:
        kwargs["expires_at"] = args.get("expires_at")
    if "reopen_policy" in args:
        kwargs["reopen_policy"] = args.get("reopen_policy")
    try:
        return await db_module.resolve_proposal_gate(
            db, args["project_id"], args["gate_id"], args.get("state"),
            args.get("decision"), args.get("actor"), **kwargs,
        )
    except db_module.ProposalGateError as exc:
        return {"error": str(exc)}


async def handle_reopen_proposal_gate(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: reopen_proposal_gate.

    Invalidates a still-standing decision (e.g. new evidence surfaced) so
    ``resolve_proposal_gate`` can be called again — resets the lane to
    ``blocked`` (fail-safe) and snapshots the prior decision into
    ``previous_*``. Refuses (``{"error": ...}``) a gate that was never
    decided.
    """
    validate_input_size(args.get("reason"), "gate reopen reason", 100_000)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    if not args.get("gate_id"):
        return {"error": "gate_id is required"}
    try:
        return await db_module.reopen_proposal_gate(
            db, args["project_id"], args["gate_id"], args.get("actor"),
            args.get("reason"),
        )
    except db_module.ProposalGateError as exc:
        return {"error": str(exc)}


async def handle_get_proposal_gates(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_proposal_gates.

    Read-only: list gates for a project, optionally filtered by category
    and/or (raw, stored) state. Pass ``sprint_item_id`` to instead list only
    the gates currently blocking/quarantining that one item (via
    ``meridian.proposal_gates.blocking_gates_for_sprint_item`` — an
    effective-state-aware view, unlike the unfiltered list).
    """
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    if args.get("sprint_item_id"):
        return await db_module.blocking_gates_for_sprint_item(
            db, args["project_id"], args["sprint_item_id"],
        )
    return await db_module.list_proposal_gates(
        db, args["project_id"],
        category=args.get("category"), state=args.get("state"),
    )


# ---------------------------------------------------------------------------
# Section 2: Notes
# ---------------------------------------------------------------------------

async def handle_add_note(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: add_note."""
    validate_input_size(args.get("title"), "note title", 500)
    validate_input_size(args.get("body"), "note body", 10_000_000)
    validate_input_size(args.get("file_path"), "note file_path", 2_000)
    validate_input_size(args.get("symbol"), "note symbol", 500)
    validate_input_size(args.get("source"), "note source", 2_000)
    # 41b8a927 — recognise #hashtags in the title/body as tags so a note is
    # searchable by tag without a separate tags argument.
    import re as _re_ht  # noqa: PLC0415
    _ht = _re_ht.findall(
        r"(?<!\w)#([A-Za-z][\w-]{1,40})",
        f"{args.get('title') or ''} {args.get('body') or ''}",
    )
    _tags_arg = args.get("tags")
    if _ht:
        _have = {t.strip().lower() for t in (_tags_arg or "").split(",") if t.strip()}
        _add = [h for h in _ht if h.lower() not in _have]
        if _add:
            _tags_arg = ", ".join(
                [p for p in [(_tags_arg or "").strip()] if p] + _add
            )
    try:
        result = await db_module.add_project_note(
            db, args["project_id"], args["title"], args["body"],
            _tags_arg, kind=args.get("kind"),
            priority=args.get("priority", "normal"),
            file_path=args.get("file_path"), symbol=args.get("symbol"),
            source=args.get("source"),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    await _server._append_note_to_roadmap(
        args["title"], args["body"], args.get("tags"), args.get("category"),
    )
    # e5592013 — lint: "MANUAL" notes are usually human tasks, not wiki.
    if isinstance(result, dict) and "MANUAL" in (args.get("title") or ""):
        result = {**result, "lint": _MANUAL_NOTE_LINT}
    # 6e4e2371 — warn (never block) when a near-duplicate note already exists,
    # so notes don't accumulate repetitive near-copies. Advisory: any failure
    # here must not fail the write.
    if isinstance(result, dict) and not result.get("error"):
        try:
            import difflib as _difflib  # noqa: PLC0415
            _new_title = (args.get("title") or "").strip().lower()
            if _new_title:
                _new_id = result.get("id")
                _new_slug = result.get("slug")
                _existing = await db_module.get_project_notes(
                    db, args["project_id"], limit=200
                )
                _similar = []
                for _n in (_existing or []):
                    if (_new_id and _n.get("id") == _new_id) or (
                        _new_slug and _n.get("slug") == _new_slug
                    ):
                        continue  # skip the note we just created
                    _et = (_n.get("title") or "").strip().lower()
                    if not _et:
                        continue
                    _ratio = _difflib.SequenceMatcher(None, _new_title, _et).ratio()
                    if _ratio >= 0.82:
                        _similar.append({
                            "slug": _n.get("slug"),
                            "title": _n.get("title"),
                            "similarity": round(_ratio, 2),
                        })
                if _similar:
                    _similar.sort(key=lambda s: s["similarity"], reverse=True)
                    result = {
                        **result,
                        "similar_notes": _similar[:3],
                        "similar_notes_warning": (
                            "A similar note already exists — consider updating it "
                            "instead of accumulating near-duplicates."
                        ),
                    }
        except Exception:  # noqa: BLE001 — dedup is advisory
            pass
    return result


async def handle_get_notes(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_notes.

    5a5bba43 — pull model: default to the lightweight list (no bodies) so
    bulk note injection can't overflow context. Agents fetch one body via
    read_note(slug). Pass bodies=true to opt back into full rows.
    9fa119dd — cursor pagination, opt-in (mirrors get_sprint_items, whose
    MCP tool stays a bare list while the HTTP route paginates): pass
    ``cursor`` and/or ``limit`` to get the {notes, has_more, next_cursor}
    envelope, then re-call with cursor=next_cursor for the next page.
    Without either arg the legacy bare list is returned for back-compat.
    98890df1 — relevance sort (reference_count/recency/decision-link) takes
    precedence over cursor/limit paging.
    """
    if args.get("sort") == "relevance":
        return await db_module.get_project_notes_ranked(
            db, args["project_id"], tag=args.get("tag"), query=args.get("query"),
            bodies=bool(args.get("bodies", False)),
            limit=(int(args["limit"]) if "limit" in args else None),
        )
    if "cursor" in args or "limit" in args:
        return await db_module.get_project_notes_page(
            db, args["project_id"], tag=args.get("tag"), query=args.get("query"),
            bodies=bool(args.get("bodies", False)),
            limit=int(args.get("limit", 100)),
            cursor=int(args.get("cursor", 0)),
        )
    return await db_module.get_project_notes(
        db, args["project_id"], tag=args.get("tag"), query=args.get("query"),
        bodies=bool(args.get("bodies", False)),
    )


async def handle_read_note(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: read_note.

    5a5bba43 — the pull half of the list→read model: fetch one note's full
    body by its per-project slug (returned in the get_notes list).
    """
    note = await db_module.get_project_note_by_slug(
        db, args["project_id"], args["slug"],
    )
    if note is None:
        return {"error": f"note '{args['slug']}' not found in project {args['project_id']}"}
    return note


async def handle_delete_note(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: delete_note."""
    ok = await db_module.delete_project_note(db, args["note_id"])
    return {"deleted": ok}


# ---------------------------------------------------------------------------
# Section 3: Document ingestion and structure
# ---------------------------------------------------------------------------

async def handle_ingest_document(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: ingest_document.

    e3f150d0 — extract a Word/PDF/text document into a kind='document'
    note. Extraction (.txt/.md/.docx) is stdlib-only and server-side;
    PDFs/unsupported types must be pre-extracted by the caller and passed
    as `content`. The body cap is applied inside db.ingest_document.
    """
    validate_input_size(args.get("title"), "document title", 500)
    validate_input_size(args.get("file_path"), "document file_path", 2_000)
    validate_input_size(args.get("source"), "document source", 2_000)
    validate_input_size(args.get("content"), "document content", 50_000_000)
    # 832d67af — when only a file_path is given (no inline content) the server
    # extracts the text from its OWN filesystem (doc_ingest.extract_text), so on
    # hosted Meridian (Fly.io) it has ZERO access to a caller's local path and
    # the open fails with a misleading "[Errno 2] No such file or directory".
    # Mirror get_document_structure's honest guard: fail clearly, telling the
    # caller why and what to do. `content` (pre-extracted text) needs no
    # filesystem and DOES work hosted, so only guard the path-only case.
    _fp = args.get("file_path")
    _content = args.get("content")
    _has_content = _content is not None and str(_content).strip() != ""
    if _hosted_mode() and _fp and not _has_content:
        return {
            "error": (
                "ingest_document reads the file from the Meridian server's own "
                "filesystem, so on hosted Meridian it cannot open a path on your "
                "machine (that is what surfaces as a misleading '[Errno 2] No "
                "such file or directory'). Run Meridian self-hosted so the server "
                "shares a filesystem with the file, pass the already-extracted "
                "text as `content` instead of a `file_path`, or read the document "
                "through your tunnel's local document tools, which proxy to your "
                "machine."
            ),
            "hosted": True,
            "file_path": _fp,
        }
    from meridian.doc_ingest import DocExtractionError  # noqa: PLC0415
    try:
        _ingest_result = await db_module.ingest_document(
            db, args["project_id"],
            file_path=args.get("file_path"),
            content=args.get("content"),
            title=args.get("title"),
            source=args.get("source"),
            tags=args.get("tags"),
        )
    except (ValueError, DocExtractionError, FileNotFoundError) as exc:
        return {"error": str(exc)}
    # 9ee6d2ec — best-effort: persist the parsed docx/latex STRUCTURE into the
    # tiered doc-structure store. Fully guarded inside — a persistence failure
    # never touches _ingest_result (no regression to the flat-note ingest).
    from meridian.mcp.handler import _persist_ingest_structure  # noqa: PLC0415
    await _persist_ingest_structure(
        db,
        data_dir,
        tenant,
        args["project_id"],
        args.get("file_path"),
        args.get("source"),
        args.get("title"),
    )
    return _ingest_result


async def handle_get_document_structure(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_document_structure.

    13462df2 — stateless docs_intel: heading outline of a server-side .docx
    (no sidecar index). Same server-side file access as ingest_document
    (self-hosted / tunnel).
    """
    validate_input_size(args.get("file_path"), "document file_path", 2_000)
    fp = args.get("file_path")
    if not fp:
        return {"error": "file_path is required"}
    # 79ee73e8 — record this stateless peek in the tenant-scoped "recently
    # viewed (not saved)" log so the Documents tab can surface it. Peeks were
    # invisible there (only ingested docs showed), silently conflating the two.
    _peek_scope = (tenant or {}).get("id") if tenant else None

    def _record_peek(ok: bool) -> None:
        try:
            from meridian import doc_peeks  # noqa: PLC0415
            doc_peeks.record_peek(_peek_scope, fp, ok=ok)
        except Exception:  # noqa: BLE001 — the recent-peeks log is best-effort
            pass

    # b43bab91 — this reads the .docx from the SERVER's own filesystem
    # (zipfile.ZipFile), so it only works self-hosted, where the server and the
    # files share a machine. On hosted Meridian (Fly.io) the server has ZERO
    # access to a caller's local path, so the read would fail with a misleading
    # "file not found" regardless of tunnel/file state. Fail honestly instead:
    # tell the caller why and what to do (self-host, or read via the tunnel's
    # word-document tools, which proxy to their machine — unlike this native
    # tool, which does not).
    if _hosted_mode():
        _record_peek(ok=False)
        return {
            "error": (
                "get_document_structure reads the .docx from the Meridian "
                "server's own filesystem, so on hosted Meridian it cannot open a "
                "path on your machine (that is what surfaces as a misleading "
                "'file not found'). Run Meridian self-hosted so the server shares "
                "a filesystem with the file, or read the document through your "
                "tunnel's word-document tools, which proxy to your machine."
            ),
            "hosted": True,
            "file_path": fp,
        }
    try:
        from meridian.docs_intel import document_outline  # noqa: PLC0415
        from meridian import hardening as _hardening  # noqa: PLC0415
        # document_outline is a synchronous zipfile/OOXML parse — previously
        # run directly on the event loop with no deadline, so a huge/malformed
        # .docx could block the whole loop (e5f96adf). Run it in the bulkhead
        # under a hard timeout: fail fast + keep the loop responsive.
        _outline = await _hardening.run_in_bulkhead(
            document_outline, fp, label="get_document_structure",
        )
    except _hardening.HeavyToolTimeout as exc:
        _record_peek(ok=False)
        return {"error": str(exc), "timed_out": True, "file_path": fp}
    except FileNotFoundError:
        return {"error": f"file not found: {fp}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not parse document: {exc}"}
    _record_peek(ok=True)
    return _outline


async def handle_get_latex_structure(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_latex_structure.

    106118cd — docs_intel Phase 3: native LaTeX (.tex) structure + biblio,
    no PDF intermediary. Accepts a server-side file_path (like
    get_document_structure) OR raw `source` inline. latex_intel never
    raises — malformed LaTeX yields a partial/empty tree, not a crash.
    """
    validate_input_size(args.get("file_path"), "latex file_path", 2_000)
    validate_input_size(args.get("source"), "latex source", 5_000_000)
    fp = args.get("file_path")
    src = args.get("source")
    if not fp and not src:
        return {"error": "file_path or source is required"}
    # b43bab91 — a file_path is read from the SERVER filesystem and is
    # unreadable on hosted Meridian (same root cause as get_document_structure).
    # But get_latex_structure ALSO accepts inline `source`, which DOES work
    # hosted — so on hosted prefer source, and fail honestly when only an
    # unreadable path was given.
    if _hosted_mode() and fp:
        if src:
            fp = None  # server can't open the caller's path; use inline source
        else:
            return {
                "error": (
                    "get_latex_structure reads the .tex from the Meridian "
                    "server's filesystem, so on hosted Meridian it cannot open a "
                    "path on your machine. Pass the file contents inline via "
                    "`source`, or run Meridian self-hosted."
                ),
                "hosted": True,
                "file_path": fp,
            }
    from meridian.latex_intel import analyze_latex  # noqa: PLC0415
    try:
        if fp:
            if not os.path.isfile(fp):
                return {"error": f"file not found: {fp}"}
            return analyze_latex(fp)
        return analyze_latex(src)
    except Exception as exc:  # noqa: BLE001 — defense in depth; analyze_latex is already safe
        return {"error": f"could not parse latex: {exc}"}


async def handle_ingest_document_structure(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: ingest_document_structure.

    db42acce — receive pre-parsed structural data (headings/figures/tables)
    forwarded from the tunnel-local side (where the real .docx lives) and
    persist it into the doc-structure store so find_similar_figure /
    index_figure / index_table / index_equation all see the right document_id.

    The tunnel-local function ``ingest_local_document_structure`` (in the
    meridian-docs extension) calls document_content_tree on the REAL file
    (which it CAN read locally), serializes the ``blocks`` list from the
    tree as JSON, and forwards it here.  The hosted server receives the raw
    blocks, converts them to structured elements via
    ``elements_from_docx_content_tree`` (which lives server-side in
    meridian.doc_store), and stores them via put_document — keyed on the
    SAME source string that ingest_document(content=...) already used, so
    get_document() resolves the same document_id for both the flat note and
    the structural rows.
    """
    validate_input_size(args.get("source"), "document source", 2_000)
    validate_input_size(args.get("title"), "document title", 500)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    _struct_source = (args.get("source") or "").strip()
    if not _struct_source:
        return {
            "error": (
                "source is required — must match the source used for "
                "ingest_document (usually the local file path)"
            )
        }
    _blocks_raw = args.get("blocks")
    if not _blocks_raw:
        return {
            "error": (
                "blocks is required — JSON-encoded list of body blocks from "
                "document_content_tree (the 'blocks' key of its return value)"
            )
        }
    try:
        if isinstance(_blocks_raw, str):
            _blocks: list[Any] = json.loads(_blocks_raw)
        elif isinstance(_blocks_raw, list):
            _blocks = _blocks_raw
        else:
            return {"error": "blocks must be a JSON array"}
    except (json.JSONDecodeError, TypeError) as exc:
        return {"error": f"blocks is not valid JSON: {exc}"}
    if not isinstance(_blocks, list):
        return {"error": "blocks must be a JSON array"}
    _struct_doc_type = (args.get("doc_type") or "docx").strip() or "docx"
    # Convert raw blocks to structured elements using the server-side mapper.
    try:
        from meridian.doc_store import elements_from_docx_content_tree  # noqa: PLC0415
        _struct_elements: list[Any] = elements_from_docx_content_tree(
            {"blocks": _blocks}
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not convert blocks to elements: {exc}"}
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"error": "document-structure store unavailable"}
    try:
        doc = await store.put_document(
            args["project_id"],
            _struct_doc_type,
            _struct_elements,
            source=_struct_source,
            title=args.get("title"),
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not persist document structure: {exc}"}
    return {
        "project_id": args["project_id"],
        "document_id": doc["id"],
        "source": _struct_source,
        "doc_type": _struct_doc_type,
        "element_count": doc.get("element_count", len(_struct_elements)),
    }


# ---------------------------------------------------------------------------
# Section 4: Citation graph
# ---------------------------------------------------------------------------

async def handle_get_citation_edges(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_citation_edges.

    fefb596a — read the citation graph: every kind='citation' marker in a
    project (optionally scoped to one document via source/document_id) with
    its intra-doc bibentry edges AND cross-doc zotero_item edges. Reads the
    tier-resolved doc-structure store; returns an empty graph (never an
    error) when no structure has been persisted yet.
    """
    validate_input_size(args.get("source"), "citation source", 2_000)
    validate_input_size(args.get("document_id"), "citation document_id", 200)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"project_id": args["project_id"], "markers": []}
    try:
        graph = await store.get_citation_graph(
            args["project_id"],
            source=args.get("source"),
            document_id=args.get("document_id"),
        )
    except Exception as exc:  # noqa: BLE001 — read must not crash the tool call
        return {"error": f"could not read citation graph: {exc}"}
    return {"project_id": args["project_id"], **graph}


async def handle_resolve_citations(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: resolve_citations.

    fefb596a — opt-in cross-document resolve pass: walk unresolved citation
    markers and link each to a canonical Zotero item via Zotero's LOCAL API
    (zotero_client.resolve_citation_ref). NETWORK — deliberately a separate
    tool, never in ingest/put_document. Idempotent: only fills gaps. When
    Zotero is closed / its local API is disabled every marker just stays
    unresolved (the resolver returns None, never raises).
    """
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"error": "document-structure store unavailable"}
    _max = args.get("max_items")
    try:
        _max = int(_max) if _max is not None else None
    except (TypeError, ValueError):
        _max = None
    try:
        summary = await store.resolve_zotero_edges(
            args["project_id"], max_items=_max,
        )
    except Exception as exc:  # noqa: BLE001 — the pass is best-effort
        return {"error": f"could not resolve citations: {exc}"}
    return {"project_id": args["project_id"], **summary}


# ---------------------------------------------------------------------------
# Section 5: Equation index
# ---------------------------------------------------------------------------

async def handle_index_equation(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: index_equation.

    06df6ab3 — index ONE Word equation (OMML) against a document already
    stored in the doc-structure store. Mirrors get_citation_edges' shape
    (resolve the store, then look up the document by its stored source).
    """
    validate_input_size(args.get("doc"), "equation doc", 2_000)
    validate_input_size(args.get("omml_or_latex"), "omml_or_latex", 100_000)
    validate_input_size(args.get("semantic_label"), "semantic_label", 500)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    doc_source = args.get("doc")
    omml_or_latex = args.get("omml_or_latex")
    if not doc_source:
        return {"error": "doc is required"}
    if not omml_or_latex:
        return {"error": "omml_or_latex is required"}
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"error": "document-structure store unavailable"}
    try:
        doc_row = await store.get_document(args["project_id"], doc_source)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not resolve doc: {exc}"}
    if doc_row is None:
        return {
            "error": (
                f"no stored document for doc={doc_source!r} — ingest_document "
                "it first (that MCP tool populates the doc-structure store; "
                "there is no separate reindex_document tool)"
            ),
        }
    try:
        result = await store.add_equation(
            doc_row["id"], omml_or_latex,
            semantic_label=args.get("semantic_label"),
        )
    except Exception as exc:  # noqa: BLE001 — indexing is best-effort
        return {"error": f"could not index equation: {exc}"}
    return {
        "project_id": args["project_id"],
        "document_id": doc_row["id"],
        **result,
    }


async def handle_find_similar_equation(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: find_similar_equation.

    06df6ab3 — fuzzy-match a LaTeX string against a document's already-
    stored equations (read-only counterpart of index_equation).
    """
    validate_input_size(args.get("doc"), "equation doc", 2_000)
    validate_input_size(args.get("latex"), "latex", 100_000)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    doc_source = args.get("doc")
    latex = args.get("latex")
    if not doc_source:
        return {"error": "doc is required"}
    if not latex:
        return {"error": "latex is required"}
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"project_id": args["project_id"], "document_id": None, "matches": []}
    try:
        doc_row = await store.get_document(args["project_id"], doc_source)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not resolve doc: {exc}"}
    if doc_row is None:
        return {"project_id": args["project_id"], "document_id": None, "matches": []}
    _limit = args.get("limit")
    try:
        _limit = int(_limit) if _limit is not None else 5
    except (TypeError, ValueError):
        _limit = 5
    try:
        matches = await store.find_similar_equations(
            doc_row["id"], latex, limit=_limit,
        )
    except Exception as exc:  # noqa: BLE001 — read must not crash the tool call
        return {"error": f"could not find similar equations: {exc}"}
    return {
        "project_id": args["project_id"],
        "document_id": doc_row["id"],
        "matches": matches,
    }


async def handle_insert_equation(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: insert_equation.

    51a595e7 — write an OMML equation straight into a stored document's
    source .docx (direct OOXML write-back), then resync the sidecar
    equation index. Mirrors index_equation's shape (resolve the store, then
    the document by its stored source) but MUTATES the underlying file.
    """
    validate_input_size(args.get("doc"), "equation doc", 2_000)
    validate_input_size(args.get("para_id"), "para_id", 500)
    validate_input_size(
        args.get("equation_id_or_omml"), "equation_id_or_omml", 100_000
    )
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    doc_source = args.get("doc")
    para_id = args.get("para_id")
    equation_id_or_omml = args.get("equation_id_or_omml")
    if not doc_source:
        return {"error": "doc is required"}
    if not para_id:
        return {"error": "para_id is required"}
    if not equation_id_or_omml:
        return {"error": "equation_id_or_omml is required"}
    position = args.get("position") or "append"
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"error": "document-structure store unavailable"}
    try:
        result = await store.insert_equation(
            args["project_id"], doc_source, para_id, equation_id_or_omml,
            position=position,
        )
    except Exception as exc:  # noqa: BLE001 — write-back is best-effort
        return {"error": f"could not insert equation: {exc}"}
    if "error" in result:
        return result
    return {"project_id": args["project_id"], **result}


# ---------------------------------------------------------------------------
# Section 6: Paragraph update
# ---------------------------------------------------------------------------

async def handle_update_paragraph(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: update_paragraph.

    f978e588 — ID-addressable docx WRITE (the write counterpart of the
    get_element_by_id read primitive). Mirrors index_equation's resolution:
    resolve the tier store, look up the stored document by its source, then
    rewrite ONE paragraph in the on-disk .docx by its w14:paraId (never by
    text match) and resync the doc_elements row.
    f7ee1ba7 — Model B scoped-region enforcement: before writing, consult
    docx-region claims and REJECT the write when another session owns the
    target element (or holds a whole-file lock). Fail-open: a claim-lookup
    error degrades to allow so a missing db never wedges a legitimate write.

    5988a5bb — threads three new OPT-IN parameters straight through to
    ``store.update_paragraph`` (see that method's docstring for the fail-
    closed/draft-mode semantics); none of them change behavior when omitted:
    ``expected_content_hash`` (fail-closed staleness precondition),
    ``draft_output_path`` + ``wave_run_id`` (wave-scoped isolated-draft
    write, both-or-neither). The scoped-region claim-check layering above
    is otherwise unchanged.
    """
    validate_input_size(args.get("doc"), "doc", 2_000)
    validate_input_size(args.get("para_id"), "para_id", 500)
    validate_input_size(args.get("expected_content_hash"), "expected_content_hash", 200)
    validate_input_size(args.get("draft_output_path"), "draft_output_path", 4_000)
    validate_input_size(args.get("wave_run_id"), "wave_run_id", 200)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    doc_source = args.get("doc")
    para_id = args.get("para_id")
    if not doc_source:
        return {"error": "doc is required"}
    if not para_id:
        return {"error": "para_id is required"}
    # new_text_or_runs: EITHER a plain string OR a list of runs. Exactly one
    # of new_text / runs must be provided.
    new_text = args.get("new_text")
    runs = args.get("runs")
    if new_text is None and runs is None:
        return {"error": "provide either new_text (string) or runs (list)"}
    if new_text is not None and runs is not None:
        return {"error": "provide only one of new_text or runs, not both"}
    new_text_or_runs: Any = runs if runs is not None else new_text
    if new_text is not None:
        validate_input_size(new_text, "new_text", 1_000_000)
    elif not isinstance(runs, list):
        return {"error": "runs must be a list of strings or run objects"}
    # f7ee1ba7 — scoped-region claim enforcement gate.
    _up_session_id = (args.get("session_id") or "").strip() or None
    if db is not None:
        _region_conflict = await db_module.check_docx_region_write_conflict(
            db, _up_session_id, doc_source, para_id,
        )
        if _region_conflict is not None and _region_conflict.get("blocked"):
            return {
                "error": "docx_region_conflict",
                "blocked": True,
                "reason": _region_conflict.get("reason"),
                "holder": _region_conflict.get("holder"),
                "message": _region_conflict.get("message"),
            }
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"error": "document-structure store unavailable"}
    try:
        result = await store.update_paragraph(
            args["project_id"], doc_source, para_id, new_text_or_runs,
            expected_content_hash=args.get("expected_content_hash") or None,
            draft_output_path=args.get("draft_output_path") or None,
            wave_run_id=args.get("wave_run_id") or None,
            session_id=_up_session_id,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — the write is best-effort
        return {"error": f"could not update paragraph: {exc}"}
    return {"project_id": args["project_id"], **result}


# ---------------------------------------------------------------------------
# Section 7: Symbol usages
# ---------------------------------------------------------------------------

async def handle_find_symbol_usages(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: find_symbol_usages.

    9605edb0 — READ-ONLY cross-reference tracking: resolve a symbol /
    normalized-LaTeX string OR a doc_equations id to one target and return
    every paragraph/equation where it reappears, classified definition vs
    reuse (earliest ordinal = definition). Mirrors find_similar_equation's
    shape (resolve the store, then look up the document by its stored source).
    """
    validate_input_size(args.get("doc"), "symbol usages doc", 2_000)
    validate_input_size(args.get("symbol_or_equation_id"), "symbol_or_equation_id", 100_000)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    doc_source = args.get("doc")
    symbol_or_equation_id = args.get("symbol_or_equation_id")
    if not doc_source:
        return {"error": "doc is required"}
    if not symbol_or_equation_id:
        return {"error": "symbol_or_equation_id is required"}
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"project_id": args["project_id"], "document_id": None, "target": "", "hits": []}
    try:
        doc_row = await store.get_document(args["project_id"], doc_source)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not resolve doc: {exc}"}
    if doc_row is None:
        return {"project_id": args["project_id"], "document_id": None, "target": "", "hits": []}
    try:
        usages = await store.find_symbol_usages(
            doc_row["id"], symbol_or_equation_id,
        )
    except Exception as exc:  # noqa: BLE001 — read must not crash the tool call
        return {"error": f"could not find symbol usages: {exc}"}
    return {
        "project_id": args["project_id"],
        "document_id": doc_row["id"],
        **usages,
    }


# ---------------------------------------------------------------------------
# Section 8: Figure index
# ---------------------------------------------------------------------------

async def handle_index_figure(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: index_figure.

    c623e648 — index ONE figure into the SEMANTIC figure index (dedup +
    similarity on a normalized caption), the direct parallel of
    index_equation. Complementary to the structural kind='figure'
    doc_elements placement, not a duplicate of it.
    """
    validate_input_size(args.get("doc"), "figure doc", 2_000)
    validate_input_size(args.get("file_path"), "file_path", 4_000)
    validate_input_size(args.get("caption"), "caption", 10_000)
    validate_input_size(args.get("semantic_label"), "semantic_label", 500)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    doc_source = args.get("doc")
    file_path = args.get("file_path")
    caption = args.get("caption")
    if not doc_source:
        return {"error": "doc is required"}
    if not file_path and not caption:
        return {"error": "at least one of file_path or caption is required"}
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"error": "document-structure store unavailable"}
    try:
        doc_row = await store.get_document(args["project_id"], doc_source)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not resolve doc: {exc}"}
    if doc_row is None:
        return {
            "error": (
                f"no stored document for doc={doc_source!r} — ingest_document "
                "it first (that MCP tool populates the doc-structure store; "
                "there is no separate reindex_document tool)"
            ),
        }
    try:
        result = await store.add_figure(
            doc_row["id"], file_path,
            caption=caption,
            semantic_label=args.get("semantic_label"),
        )
    except Exception as exc:  # noqa: BLE001 — indexing is best-effort
        return {"error": f"could not index figure: {exc}"}
    return {
        "project_id": args["project_id"],
        "document_id": doc_row["id"],
        **result,
    }


async def handle_find_similar_figure(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: find_similar_figure.

    c623e648 — fuzzy-match a description OR a path against a document's
    already-indexed figures (read-only counterpart of index_figure).
    """
    validate_input_size(args.get("doc"), "figure doc", 2_000)
    validate_input_size(args.get("description_or_path"), "description_or_path", 10_000)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    doc_source = args.get("doc")
    query = args.get("description_or_path")
    if not doc_source:
        return {"error": "doc is required"}
    if not query:
        return {"error": "description_or_path is required"}
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"project_id": args["project_id"], "document_id": None, "matches": []}
    try:
        doc_row = await store.get_document(args["project_id"], doc_source)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not resolve doc: {exc}"}
    if doc_row is None:
        return {"project_id": args["project_id"], "document_id": None, "matches": []}
    _limit = args.get("limit")
    try:
        _limit = int(_limit) if _limit is not None else 5
    except (TypeError, ValueError):
        _limit = 5
    # d2a3537a — cross-store resolve-through: when the caller names an
    # outputs_dir, each matched figure that carries a file_path is resolved
    # THROUGH to its outputs_index row (linked_output). Building the DuckDB
    # index is CPU-bound, so do it once off the event loop and hand the store
    # a resolver closure over the built index; skipped entirely when no
    # outputs_dir is given (the tool stays a pure fuzzy match by default).
    outputs_dir = str(args.get("outputs_dir") or "").strip()
    _resolver = None
    _index = None
    # Workspace decision 0dedff91 — the outputs resolve-through stats/walks
    # `outputs_dir` on THIS process's own filesystem (os.path.isdir + a
    # DuckDB rebuild over the tree). On hosted Meridian that path is on the
    # caller's machine, which the server can never reach, so skip the
    # resolve-through entirely (the figure match itself is DB-only and
    # still works). Never touch a caller's local dir server-side hosted.
    if _hosted_mode():
        outputs_dir = ""
    if outputs_dir and os.path.isdir(outputs_dir):
        from meridian import outputs_indexer as _outputs_indexer  # noqa: PLC0415
        _index = _outputs_indexer.OutputsFtsIndex(outputs_dir)
        try:
            await asyncio.to_thread(_index.rebuild)
            _resolver = _index.resolve_output
        except Exception:  # noqa: BLE001 — resolve-through is advisory glue
            _resolver = None
    try:
        matches = await store.find_similar_figures(
            doc_row["id"], query, limit=_limit, output_resolver=_resolver,
        )
    except Exception as exc:  # noqa: BLE001 — read must not crash the tool call
        return {"error": f"could not find similar figures: {exc}"}
    finally:
        if _index is not None:
            _index.close()
    return {
        "project_id": args["project_id"],
        "document_id": doc_row["id"],
        "matches": matches,
    }


async def handle_link_figure_caption(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: link_figure_caption.

    0ff8b982 — durably link an already-indexed doc_figures row to its
    caption paragraph by stable doc_elements id (not proximity). Confirmation
    primitive for the advisory suggestion from index_figure, and the
    backfill mechanism for figures indexed before caption linkage was added.
    """
    validate_input_size(args.get("doc"), "figure doc", 2_000)
    validate_input_size(args.get("figure_id"), "figure_id", 200)
    validate_input_size(args.get("caption_element_id"), "caption_element_id", 200)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    doc_source = args.get("doc")
    figure_id = (args.get("figure_id") or "").strip()
    caption_element_id = (args.get("caption_element_id") or "").strip()
    if not doc_source:
        return {"error": "doc is required"}
    if not figure_id:
        return {"error": "figure_id is required"}
    if not caption_element_id:
        return {"error": "caption_element_id is required"}
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"error": "document-structure store unavailable"}
    try:
        doc_row = await store.get_document(args["project_id"], doc_source)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not resolve doc: {exc}"}
    if doc_row is None:
        return {
            "error": (
                f"no stored document for doc={doc_source!r} — ingest_document "
                "it first (that MCP tool populates the doc-structure store; "
                "there is no separate reindex_document tool)"
            ),
        }
    try:
        updated = await store.set_figure_caption_link(figure_id, caption_element_id)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not set caption link: {exc}"}
    if updated is None:
        return {
            "error": (
                f"no doc_figures row found for figure_id={figure_id!r} "
                "— use find_similar_figure to locate the correct figure_id"
            ),
        }
    return {
        "project_id": args["project_id"],
        "document_id": doc_row["id"],
        "figure": updated,
    }


# ---------------------------------------------------------------------------
# Section 9: Table index
# ---------------------------------------------------------------------------

async def handle_index_table(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: index_table.

    2622182d — index ONE table into the SEMANTIC table index (dedup +
    similarity on a normalized caption), the direct parallel of
    index_figure. Complementary to the structural kind='table'
    doc_elements placement, not a duplicate of it.
    """
    validate_input_size(args.get("doc"), "table doc", 2_000)
    validate_input_size(args.get("caption"), "caption", 10_000)
    validate_input_size(args.get("semantic_label"), "semantic_label", 500)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    doc_source = args.get("doc")
    caption = args.get("caption")
    table_index_raw = args.get("table_index")
    table_index: int | None = None
    if table_index_raw is not None:
        try:
            table_index = int(table_index_raw)
        except (TypeError, ValueError):
            return {"error": f"table_index must be an integer, got {table_index_raw!r}"}
    if not doc_source:
        return {"error": "doc is required"}
    if table_index is None and not caption:
        return {"error": "at least one of table_index or caption is required"}
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"error": "document-structure store unavailable"}
    try:
        doc_row = await store.get_document(args["project_id"], doc_source)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not resolve doc: {exc}"}
    if doc_row is None:
        return {
            "error": (
                f"no stored document for doc={doc_source!r} — ingest_document "
                "it first (that MCP tool populates the doc-structure store; "
                "there is no separate reindex_document tool)"
            ),
        }
    try:
        result = await store.add_table(
            doc_row["id"], table_index,
            caption=caption,
            semantic_label=args.get("semantic_label"),
            paired_figure_id=args.get("paired_figure_id"),
        )
    except Exception as exc:  # noqa: BLE001 — indexing is best-effort
        return {"error": f"could not index table: {exc}"}
    return {
        "project_id": args["project_id"],
        "document_id": doc_row["id"],
        **result,
    }


async def handle_find_similar_table(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: find_similar_table.

    2622182d — fuzzy-match a description against a document's
    already-indexed tables (read-only counterpart of index_table).
    """
    validate_input_size(args.get("doc"), "table doc", 2_000)
    validate_input_size(args.get("description"), "description", 10_000)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    doc_source = args.get("doc")
    query = args.get("description")
    if not doc_source:
        return {"error": "doc is required"}
    if not query:
        return {"error": "description is required"}
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"project_id": args["project_id"], "document_id": None, "matches": []}
    try:
        doc_row = await store.get_document(args["project_id"], doc_source)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not resolve doc: {exc}"}
    if doc_row is None:
        return {"project_id": args["project_id"], "document_id": None, "matches": []}
    _limit = args.get("limit")
    try:
        _limit = int(_limit) if _limit is not None else 5
    except (TypeError, ValueError):
        _limit = 5
    try:
        matches = await store.find_similar_tables(
            doc_row["id"], query, limit=_limit,
        )
    except Exception as exc:  # noqa: BLE001 — read must not crash the tool call
        return {"error": f"could not find similar tables: {exc}"}
    return {
        "project_id": args["project_id"],
        "document_id": doc_row["id"],
        "matches": matches,
    }


async def handle_link_table_caption(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: link_table_caption.

    42d398a5 — durably link an already-indexed doc_tables row to its
    caption paragraph by stable doc_elements id (not proximity). The table
    analogue of link_figure_caption: confirmation primitive for the advisory
    suggestion from index_table, and the backfill mechanism for tables
    indexed before caption linkage was added.
    """
    validate_input_size(args.get("doc"), "table doc", 2_000)
    validate_input_size(args.get("table_id"), "table_id", 200)
    validate_input_size(args.get("caption_element_id"), "caption_element_id", 200)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    doc_source = args.get("doc")
    table_id = (args.get("table_id") or "").strip()
    caption_element_id = (args.get("caption_element_id") or "").strip()
    if not doc_source:
        return {"error": "doc is required"}
    if not table_id:
        return {"error": "table_id is required"}
    if not caption_element_id:
        return {"error": "caption_element_id is required"}
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"error": "document-structure store unavailable"}
    try:
        doc_row = await store.get_document(args["project_id"], doc_source)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not resolve doc: {exc}"}
    if doc_row is None:
        return {
            "error": (
                f"no stored document for doc={doc_source!r} — ingest_document "
                "it first (that MCP tool populates the doc-structure store; "
                "there is no separate reindex_document tool)"
            ),
        }
    try:
        updated = await store.set_table_caption_link(table_id, caption_element_id)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not set caption link: {exc}"}
    if updated is None:
        return {
            "error": (
                f"no doc_tables row found for table_id={table_id!r} "
                "— use find_similar_table to locate the correct table_id"
            ),
        }
    return {
        "project_id": args["project_id"],
        "document_id": doc_row["id"],
        "table": updated,
    }


# ---------------------------------------------------------------------------
# Section 9b: Embedded-copy-vs-source drift detection (432fcfcb)
# ---------------------------------------------------------------------------

async def handle_check_embedded_staleness(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: check_embedded_staleness.

    432fcfcb — detect whether a figure or table that was EMBEDDED into a .docx
    at some point in time has since drifted from its generating source (a plot
    script output, a CSV, etc.). This is distinct from check_staleness() in
    meridian-docs (which checks if the .docx itself changed since last indexed)
    — this checks if the SOURCE that fed the embedded copy has changed SINCE the
    copy was made.

    The comparison uses the SHA-256 fingerprint recorded in the outputs_index
    (via meridian-outputs / search_outputs / find_similar_figure's
    resolve-through) at embed time vs the CURRENT sha256 of the live source
    file on disk right now.

    Covers figures (have a file_path pointing to the embedded image asset) and
    tables (have a source_path pointing to the CSV/JSON/npy that was copied) via
    one shared mechanism. For figures, the source_path is resolved automatically
    from the stored figure's file_path via the outputs_dir (when given). For
    tables, source_path must be supplied explicitly (since doc_tables stores no
    file_path).

    Three states are returned:
      stale=False  reason="current"            — source unchanged
      stale=True   reason="content-changed"    — sha256 differs (drifted)
      stale=None   reason="source-missing"     — source file gone (distinct)
      stale=None   reason="no-source-provenance" — no source info available

    Args (all optional except project_id + doc + kind):
      project_id   — required
      doc          — the stored document's source (same as index_figure/index_table)
      kind         — "figure" or "table" (required)
      figure_id    — id of the stored doc_figures row (for kind=figure)
      table_id     — id of the stored doc_tables row (for kind=table)
      source_path  — explicit path to the generating source file on disk; for
                     figures, inferred from file_path + outputs_dir when absent
      outputs_dir  — the meridian-outputs directory to resolve the figure's
                     file_path through (figures only; triggers the same
                     OutputsFtsIndex resolve-through used by find_similar_figure)
      embed_sha256 — the SHA-256 recorded at embed time; when absent the tool
                     looks it up from the outputs_index row via outputs_dir
      embed_mtime  — the mtime recorded at embed time (fallback when no sha256)
    """
    validate_input_size(args.get("doc"), "doc", 2_000)
    validate_input_size(args.get("figure_id"), "figure_id", 200)
    validate_input_size(args.get("table_id"), "table_id", 200)
    validate_input_size(args.get("source_path"), "source_path", 4_000)
    validate_input_size(args.get("outputs_dir"), "outputs_dir", 4_000)
    validate_input_size(args.get("embed_sha256"), "embed_sha256", 200)

    if not args.get("project_id"):
        return {"error": "project_id is required"}
    kind = (args.get("kind") or "").strip().lower()
    if kind not in ("figure", "table"):
        return {"error": "kind must be 'figure' or 'table'"}
    doc_source = (args.get("doc") or "").strip()
    if not doc_source:
        return {"error": "doc is required"}

    # Resolve the doc-structure store so we can look up the stored figure/table.
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"error": "document-structure store unavailable"}
    try:
        doc_row = await store.get_document(args["project_id"], doc_source)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not resolve doc: {exc}"}
    if doc_row is None:
        return {
            "error": (
                f"no stored document for doc={doc_source!r} — ingest_document "
                "it first"
            ),
        }

    # ---------- figure path: resolve file_path + outputs_dir resolve-through ---
    source_path: str | None = (args.get("source_path") or "").strip() or None
    embed_sha256: str | None = (args.get("embed_sha256") or "").strip() or None
    embed_mtime: float | None = None
    _em = args.get("embed_mtime")
    if _em is not None:
        try:
            embed_mtime = float(_em)
        except (TypeError, ValueError):
            embed_mtime = None

    if kind == "figure":
        figure_id = (args.get("figure_id") or "").strip() or None
        if figure_id:
            try:
                figs = await store.get_figures(doc_row["id"])
            except Exception as exc:  # noqa: BLE001
                return {"error": f"could not fetch figures: {exc}"}
            fig = next((f for f in figs if str(f.get("id")) == figure_id), None)
            if fig is None:
                return {
                    "error": (
                        f"no doc_figures row for figure_id={figure_id!r} in "
                        f"document {doc_source!r} — use find_similar_figure "
                        "to locate the correct figure_id"
                    ),
                }
            if source_path is None:
                source_path = (fig.get("file_path") or "").strip() or None

        # When outputs_dir is provided, resolve through the outputs index to get
        # the sha256 recorded at index/embed time (same mechanism as
        # find_similar_figure's d2a3537a cross-store resolve-through).
        outputs_dir = str(args.get("outputs_dir") or "").strip()
        # Hosted-mode guard (same as find_similar_figure): the outputs tree lives
        # on the caller's machine; skip the resolve-through when hosted.
        if _hosted_mode():
            outputs_dir = ""
        if outputs_dir and source_path and embed_sha256 is None:
            if os.path.isdir(outputs_dir):
                try:
                    from meridian import outputs_indexer as _oi  # noqa: PLC0415
                    _index = _oi.OutputsFtsIndex(outputs_dir)
                    try:
                        await asyncio.to_thread(_index.rebuild)
                        linked = _index.resolve_output(source_path)
                        if linked is not None:
                            embed_sha256 = linked.get("sha256") or None
                            if embed_mtime is None:
                                _lm = linked.get("mtime")
                                if _lm is not None:
                                    try:
                                        embed_mtime = float(_lm)
                                    except (TypeError, ValueError):
                                        pass
                    finally:
                        _index.close()
                except Exception:  # noqa: BLE001 — resolve-through is advisory
                    pass

    # ---------- table path: source_path must be given explicitly ---------------
    # (doc_tables stores no file_path — the caller knows the source CSV/file)

    # ---------- run the staleness check ----------------------------------------
    from meridian.embedded_staleness import check_embedded_staleness  # noqa: PLC0415
    result = check_embedded_staleness(
        kind,
        source_path=source_path,
        embed_sha256=embed_sha256,
        embed_mtime=embed_mtime,
    )
    return {
        "project_id": args["project_id"],
        "doc": doc_source,
        "document_id": doc_row["id"],
        **result,
    }


# ---------------------------------------------------------------------------
# Section 9b2: Semantic figure/table/media provenance integrity audit (6b657a8b)
# ---------------------------------------------------------------------------

async def handle_audit_figure_table_provenance(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: audit_figure_table_provenance.

    6b657a8b — a batch analogue of :func:`handle_check_embedded_staleness`:
    where that tool checks ONE figure or table given explicit ids/paths, this
    walks EVERY figure and table stored for a document and links each caption
    to its embedded asset, its exact/fallback output match, SHA-256, and
    generating script — so a caller gets one whole-document integrity report
    instead of having to enumerate every figure/table id itself.

    Per-figure resolution (uses the figure's stored ``file_path``):
      - no ``file_path`` recorded at all -> ``status="orphan"``,
        ``reason="no-embedded-asset"``.
      - ``outputs_dir`` not given/not a directory -> ``status="unresolved"``,
        ``reason="no-outputs-dir"`` (nothing to resolve against).
      - :func:`meridian.outputs_indexer.resolve_output_with_fallback` finds
        nothing -> ``status="orphan"``, ``reason="no-output-match"``.
      - an EXACT match, or a basename match with exactly one candidate, is
        AUTHORITATIVE: :func:`meridian.embedded_staleness.check_embedded_staleness`
        then compares the resolved sha256 against the live file at
        ``file_path`` right now -> ``status="mismatch"`` when drifted
        (``stale=True``), else ``status="ok"``.
      - a basename match with 2+ same-basename candidates is AMBIGUOUS
        (non-authoritative — could be the wrong file) -> ``status="ambiguous"``.

    Per-table resolution (``doc_tables`` stores no ``file_path``, so the same
    exact/basename path resolution cannot apply): a generating-script hint is
    extracted from the table's OWN caption text via
    :func:`meridian.outputs_indexer.infer_generating_script_hint`, then traced
    forward with :func:`meridian.outputs_indexer.find_outputs_by_source`:
      - no hint in the caption, or ``outputs_dir`` absent -> ``status="orphan"``,
        ``reason="no-source-hint"`` / ``"no-outputs-dir"``.
      - hint found but it traces to zero outputs -> ``status="orphan"``,
        ``reason="no-output-match"``.
      - hint traces to exactly one output -> ``status="ok"``.
      - hint traces to 2+ outputs -> ``status="ambiguous"`` (non-authoritative:
        which run actually fed this table can't be determined from the
        caption alone).

    Args:
      project_id  — required.
      doc         — the stored document's source (same as ingest_document).
      outputs_dir — the meridian-outputs directory to resolve against. Omitted
                    (or hosted mode, where the outputs tree lives on the
                    caller's machine) yields ``"unresolved"``/``"no-outputs-dir"``
                    for every figure/table rather than a hard error — a
                    document with no local outputs tree is still auditable
                    for structure, just not for provenance.

    Returns ``{project_id, doc, document_id, figures: [...], tables: [...],
    summary: {figure_count, table_count, ok_count, ambiguous_count,
    orphan_count, mismatch_count, unresolved_count}}``.
    """
    validate_input_size(args.get("doc"), "doc", 2_000)
    validate_input_size(args.get("outputs_dir"), "outputs_dir", 4_000)

    if not args.get("project_id"):
        return {"error": "project_id is required"}
    doc_source = (args.get("doc") or "").strip()
    if not doc_source:
        return {"error": "doc is required"}

    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"error": "document-structure store unavailable"}
    try:
        doc_row = await store.get_document(args["project_id"], doc_source)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not resolve doc: {exc}"}
    if doc_row is None:
        return {
            "error": (
                f"no stored document for doc={doc_source!r} — ingest_document "
                "it first"
            ),
        }

    outputs_dir = str(args.get("outputs_dir") or "").strip()
    if _hosted_mode():
        # Hosted-mode guard (same as check_embedded_staleness/find_similar_figure):
        # the outputs tree lives on the caller's machine, never the server's.
        outputs_dir = ""
    outputs_dir_usable = bool(outputs_dir) and os.path.isdir(outputs_dir)

    from meridian import outputs_indexer as _oi  # noqa: PLC0415
    from meridian.embedded_staleness import check_embedded_staleness  # noqa: PLC0415

    counts = {
        "ok_count": 0, "ambiguous_count": 0, "orphan_count": 0,
        "mismatch_count": 0, "unresolved_count": 0,
    }

    figures_out: list[dict[str, Any]] = []
    try:
        figures = await store.get_figures(doc_row["id"])
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not fetch figures: {exc}"}
    for fig in figures:
        entry: dict[str, Any] = {
            "id": fig.get("id"),
            "caption": fig.get("caption"),
            "file_path": fig.get("file_path"),
        }
        file_path = (fig.get("file_path") or "").strip() or None
        if not file_path:
            entry["status"] = "orphan"
            entry["reason"] = "no-embedded-asset"
        elif not outputs_dir_usable:
            entry["status"] = "unresolved"
            entry["reason"] = "no-outputs-dir"
        else:
            resolved = _oi.resolve_output_with_fallback(outputs_dir, file_path)
            if resolved is None:
                entry["status"] = "orphan"
                entry["reason"] = "no-output-match"
            else:
                entry["match_type"] = resolved.get("match_type")
                entry["generating_script"] = resolved.get("generating_script")
                entry["sha256"] = resolved.get("sha256")
                candidate_count = resolved.get("candidate_count", 1)
                if resolved.get("match_type") != "exact":
                    entry["candidate_count"] = candidate_count
                    if candidate_count > 1:
                        entry["status"] = "ambiguous"
                        entry["reason"] = "ambiguous-basename-match"
                    else:
                        entry["status"] = "unresolved"
                        entry["reason"] = "basename-match-not-authoritative"
                else:
                    staleness = check_embedded_staleness(
                        "figure", source_path=file_path, embed_sha256=resolved.get("sha256"),
                    )
                    entry["stale"] = staleness["stale"]
                    if staleness["stale"] is True:
                        entry["status"] = "mismatch"
                        entry["reason"] = "content-changed"
                    else:
                        entry["status"] = "ok"
                        entry["reason"] = staleness["reason"]
        counts[f"{entry['status']}_count"] += 1
        figures_out.append(entry)

    tables_out: list[dict[str, Any]] = []
    try:
        tables = await store.get_tables(doc_row["id"])
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not fetch tables: {exc}"}
    for tbl in tables:
        entry = {
            "id": tbl.get("id"),
            "caption": tbl.get("caption"),
        }
        hint = _oi.infer_generating_script_hint(tbl.get("caption") or "")
        if not hint:
            entry["status"] = "orphan"
            entry["reason"] = "no-source-hint"
        elif not outputs_dir_usable:
            entry["status"] = "unresolved"
            entry["reason"] = "no-outputs-dir"
            entry["generating_script"] = hint
        else:
            traced = _oi.find_outputs_by_source(outputs_dir, hint)
            entry["generating_script"] = hint
            total = traced.get("total", 0)
            if total == 0:
                entry["status"] = "orphan"
                entry["reason"] = "no-output-match"
            elif total > 1:
                entry["status"] = "ambiguous"
                entry["reason"] = "ambiguous-source-match"
                entry["candidate_count"] = total
            else:
                entry["status"] = "ok"
                entry["reason"] = "current"
                matched = traced.get("outputs") or []
                if matched:
                    entry["sha256"] = matched[0].get("sha256")
        counts[f"{entry['status']}_count"] += 1
        tables_out.append(entry)

    return {
        "project_id": args["project_id"],
        "doc": doc_source,
        "document_id": doc_row["id"],
        "figures": figures_out,
        "tables": tables_out,
        "summary": {
            "figure_count": len(figures_out),
            "table_count": len(tables_out),
            **counts,
        },
    }


# ---------------------------------------------------------------------------
# Section 9c: Flag-to-section drift check (8ca89e8f)
# ---------------------------------------------------------------------------

async def handle_link_flag_to_section(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: link_flag_to_section.

    8ca89e8f — durably record that a docx section/paragraph/figure/table
    (identified by its stable ``doc_elements`` id — the SAME id space
    index_figure/index_table/link_figure_caption already anchor to) was
    computed with a config flag set to a particular value. The write
    primitive behind :meth:`DocStructureStore.link_flag_state`; the read/
    drift side is ``get_flag_drift``.

    Typical flow: run ``get_flag_registry`` to find the flag's current
    file/line/default, compute the section, then call this tool with
    ``value`` = the value actually used and ``default`` = the default
    ``get_flag_registry`` reported (so a later drift check has something to
    compare the codebase's CURRENT default against).
    """
    validate_input_size(args.get("doc"), "doc", 2_000)
    validate_input_size(args.get("element_id"), "element_id", 200)
    validate_input_size(args.get("flag_name"), "flag_name", 500)
    validate_input_size(args.get("source_file"), "source_file", 4_000)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    doc_source = args.get("doc")
    element_id = (args.get("element_id") or "").strip()
    flag_name = (args.get("flag_name") or "").strip()
    if not doc_source:
        return {"error": "doc is required"}
    if not element_id:
        return {"error": "element_id is required"}
    if not flag_name:
        return {"error": "flag_name is required"}
    if "value" not in args:
        return {"error": "value is required"}
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {"error": "document-structure store unavailable"}
    try:
        doc_row = await store.get_document(args["project_id"], doc_source)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not resolve doc: {exc}"}
    if doc_row is None:
        return {
            "error": (
                f"no stored document for doc={doc_source!r} — ingest_document "
                "it first (that MCP tool populates the doc-structure store; "
                "there is no separate reindex_document tool)"
            ),
        }
    try:
        link = await store.link_flag_state(
            args["project_id"], doc_row["id"], element_id, flag_name,
            value=args.get("value"),
            default=args.get("default"),
            source_file=(args.get("source_file") or None),
            source_line=args.get("source_line"),
        )
    except Exception as exc:  # noqa: BLE001 — recording is best-effort
        return {"error": f"could not record flag link: {exc}"}
    return {
        "project_id": args["project_id"],
        "document_id": doc_row["id"],
        "link": link,
    }


async def handle_get_flag_drift(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_flag_drift.

    8ca89e8f — read side of the flag-to-section link: for every recorded
    flag link (optionally scoped to one doc / element_id / flag_name — the
    reverse query "flag X changed, which sections does it touch" is just
    ``flag_name`` with no ``doc``), re-scan the CURRENT codebase via
    :func:`meridian.flag_registry.get_flag_registry` and diff
    (:func:`~meridian.flag_registry.diff_flag_links`) against each link's
    recorded default. Only the LATEST link per (element, flag) is diffed
    (:func:`~meridian.flag_registry.dedupe_flag_links`) — a re-verified
    section's older links are history, not live claims.

    Returns ``{project_id, root_dir, links:[{...link, current_default,
    current_call_sites, status}], summary:{ok, drifted, removed}}``. An empty
    link history (nothing recorded yet) returns an empty list, never an
    error — this tool is advisory, not a hard gate.
    """
    validate_input_size(args.get("doc"), "doc", 2_000)
    validate_input_size(args.get("element_id"), "element_id", 200)
    validate_input_size(args.get("flag_name"), "flag_name", 500)
    validate_input_size(args.get("root_dir"), "root_dir", 4_000)
    if not args.get("project_id"):
        return {"error": "project_id is required"}
    from meridian.mcp.handler import _resolve_ingest_doc_store  # noqa: PLC0415
    store = await _resolve_ingest_doc_store(db, data_dir, tenant)
    if store is None:
        return {
            "project_id": args["project_id"], "links": [],
            "summary": {"ok": 0, "drifted": 0, "removed": 0},
        }

    document_id = None
    doc_source = args.get("doc")
    if doc_source:
        try:
            doc_row = await store.get_document(args["project_id"], doc_source)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"could not resolve doc: {exc}"}
        if doc_row is None:
            return {
                "error": (
                    f"no stored document for doc={doc_source!r} — "
                    "ingest_document it first"
                ),
            }
        document_id = doc_row["id"]

    try:
        links = await store.get_flag_links(
            args["project_id"],
            element_id=(args.get("element_id") or None),
            document_id=document_id,
            flag_name=(args.get("flag_name") or None),
        )
    except Exception as exc:  # noqa: BLE001 — read must not crash the tool call
        return {"error": f"could not read flag links: {exc}"}

    from meridian import flag_registry as _flag_registry  # noqa: PLC0415
    latest = _flag_registry.dedupe_flag_links(links)

    root_dir = str(args.get("root_dir") or "").strip()
    if not root_dir:
        root_dir = os.getcwd()
    # Tolerate an accidentally-quoted path, same normalization get_flag_registry
    # already applies.
    if len(root_dir) >= 2 and root_dir[0] == root_dir[-1] and root_dir[0] in ("'", '"'):
        root_dir = root_dir[1:-1]

    try:
        results = await asyncio.to_thread(
            _flag_registry.check_flag_drift, latest, root_dir,
        )
    except Exception as exc:  # noqa: BLE001 — the scan itself is best-effort
        return {"error": f"could not scan flag registry: {exc}"}

    summary = {"ok": 0, "drifted": 0, "removed": 0}
    for r in results:
        status = r.get("status")
        if status in summary:
            summary[status] += 1

    return {
        "project_id": args["project_id"],
        "root_dir": root_dir,
        "links": results,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Section 10: Insights and findings
# ---------------------------------------------------------------------------

async def handle_add_insight(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: add_insight.

    0b711a9d — durable strategic insight (dedicated table, not a note).
    """
    validate_input_size(args.get("title"), "insight title", 500)
    validate_input_size(args.get("body"), "insight body", 1_000_000)
    return await db_module.create_insight(
        db, args["project_id"], args["title"], args.get("body") or "",
        horizon=args.get("horizon", "quarter"),
        tags=args.get("tags"),
    )


async def handle_get_insights(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_insights."""
    return await db_module.get_insights(
        db, args["project_id"], horizon=args.get("horizon")
    )


async def handle_save_finding(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: save_finding.

    e1f43ee7 — phase-agnostic capture primitive (decoupled from search).
    """
    validate_input_size(args.get("summary"), "finding summary", 1_000_000)
    validate_input_size(args.get("source_url"), "source_url", 2_000)
    if not (args.get("summary") or "").strip():
        return {"error": "save_finding requires a non-empty summary"}
    try:
        return await db_module.save_finding(
            db, args["project_id"], args.get("summary") or "",
            source_url=args.get("source_url"),
            source_type=args.get("source_type", "web"),
            decision_id=args.get("decision_id"),
        )
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_capture_research_finding(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: capture_research_finding.

    b1d36e93 — web/paper-shaped wrapper over save_finding; arXiv URLs are
    auto-tagged source_type=arxiv.
    """
    validate_input_size(args.get("summary"), "finding summary", 1_000_000)
    validate_input_size(args.get("url"), "url", 2_000)
    _url = (args.get("url") or "").strip()
    if not _url:
        return {"error": "capture_research_finding requires a url"}
    if not (args.get("summary") or "").strip():
        return {"error": "capture_research_finding requires a non-empty summary"}
    _st = "arxiv" if "arxiv.org" in _url.lower() else "web"
    try:
        return await db_module.save_finding(
            db, args["project_id"], args.get("summary") or "",
            source_url=_url, source_type=_st,
            decision_id=args.get("related_decision_id"),
        )
    except ValueError as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Section 11: Workspace notes, decisions, settings
# ---------------------------------------------------------------------------

async def handle_add_workspace_note(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: add_workspace_note."""
    validate_input_size(args.get("title"), "note title", 500)
    validate_input_size(args.get("body"), "note body", 10_000_000)
    result = await db_module.add_workspace_note(
        db, args["title"], args["body"], args.get("tags"),
        tenant_id=_mcp_tenant_id,
    )
    # 22c274bd — soft scope nudge; never blocks the write.
    from meridian.mcp.handler import _workspace_scope_warning  # noqa: PLC0415
    warning = _workspace_scope_warning(args.get("title"), args.get("body"))
    if warning and isinstance(result, dict):
        result["scope_warning"] = warning
    return result


async def handle_get_workspace_notes(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_workspace_notes."""
    return await db_module.get_workspace_notes(
        db, tag=args.get("tag"), tenant_id=_mcp_tenant_id,
    )


async def handle_move_workspace_note_to_project(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: move_workspace_note_to_project (84f77597).

    Reclassifies a workspace-level note (visible across all projects) into a
    single project's notes. The destination is deliberately named
    ``project_id`` (with a ``project_name`` alternative, resolved to
    ``project_id`` upstream in ``_dispatch_mcp_tool`` before this handler
    runs) so it flows through the generic project-scope gate in
    ``mcp/handler.py`` like every other project-scoped write tool — source
    ownership is enforced by ``tenant_id`` inside
    ``db_module.move_workspace_note_to_project`` itself; see that function's
    docstring for the full tenant-safety and atomicity-in-effect rationale.
    """
    note_id = (args.get("note_id") or "").strip()
    if not note_id:
        return {"error": "note_id is required"}
    project_id = (args.get("project_id") or "").strip()
    if not project_id:
        return {"error": "project_id (or project_name) is required"}
    result = await db_module.move_workspace_note_to_project(
        db, note_id, project_id, tenant_id=_mcp_tenant_id,
    )
    if result is None:
        return {
            "error": (
                "could not move workspace note: note_id not found "
                "(or not owned by this tenant), destination project not "
                "found, or a concurrent move/delete already claimed it"
            )
        }
    return result


async def handle_pin_workspace_decision(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: pin_workspace_decision."""
    validate_input_size(args.get("title"), "decision title", 500)
    validate_input_size(args.get("body"), "decision body", 100_000)
    result = await db_module.pin_workspace_decision(
        db, args["title"], args["body"],
        category=args.get("category", "TECHNICAL"),
        tenant_id=_mcp_tenant_id,
    )
    # 22c274bd — soft scope nudge; never blocks the write.
    from meridian.mcp.handler import _workspace_scope_warning  # noqa: PLC0415
    warning = _workspace_scope_warning(args.get("title"), args.get("body"))
    if warning and isinstance(result, dict):
        result["scope_warning"] = warning
    return result


async def handle_get_workspace_decisions(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_workspace_decisions."""
    return await db_module.get_workspace_decisions(
        db, include_superseded=args.get("include_superseded", False),
        tenant_id=_mcp_tenant_id,
    )


async def handle_get_workspace_settings(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_workspace_settings."""
    return await db_module.get_workspace_settings(db, tenant_id=_mcp_tenant_id)


async def handle_update_workspace_settings(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: update_workspace_settings."""
    return await db_module.update_workspace_settings(
        db,
        hitl_auto_answer_default=args.get("hitl_auto_answer_default"),
        sprint_name_default=args.get("sprint_name_default"),
        handoff_template=args.get("handoff_template"),
        # 0bf67524 — cascade defaults for new projects.
        execution_mode_default=args.get("execution_mode_default"),
        code_intel_enabled_default=args.get("code_intel_enabled_default"),
        # 76cf8bda — /loop auto-continue workspace default.
        loop_enabled_default=args.get("loop_enabled_default"),
        # 36fea6ca — inline resolved sprint-item pointers in the handoff.
        handoff_inline_pointers=args.get("handoff_inline_pointers"),
        # 490e100d — workspace-level default MCP tool priority per semantic
        # task category (hard-enforced in the /goal block).
        tool_priority_map=args.get("tool_priority_map"),
        # 4ef6ce5e — off/advisory/strict: does a PostToolUse hook re-check
        # claim_sprint_item/complete_sprint_item against live DB state?
        claim_verification_mode=args.get("claim_verification_mode"),
        tenant_id=_mcp_tenant_id,
    )


# ---------------------------------------------------------------------------
# Section 12: Blog posts
# ---------------------------------------------------------------------------

async def handle_save_blog_post(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: save_blog_post."""
    validate_input_size(args.get("title"), "blog title", 500)
    validate_input_size(args.get("body"), "blog body", 1_000_000)
    return await db_module.save_blog_post(
        db, args["title"], args.get("body", ""),
        status=args.get("status", "draft"),
        slug=args.get("slug"),
        post_id=args.get("id"),
        tenant_id=_mcp_tenant_id,
    )


async def handle_get_blog_posts(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_blog_posts."""
    return await db_module.get_blog_posts(
        db, tenant_id=_mcp_tenant_id, status=args.get("status"),
    )


# ---------------------------------------------------------------------------
# Section 13: Workspace sprint items
# ---------------------------------------------------------------------------

async def handle_add_workspace_sprint_item(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: add_workspace_sprint_item."""
    validate_input_size(args.get("title"), "sprint item title", 500)
    return await db_module.add_workspace_sprint_item(
        db, args["title"],
        item_group=args.get("group"),
        human_id=args.get("human_id"),
        tenant_id=_mcp_tenant_id,
    )


async def handle_get_workspace_sprint_items(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_workspace_sprint_items."""
    return await db_module.get_workspace_sprint_items(
        db, status=args.get("status"), item_group=args.get("group"),
        tenant_id=_mcp_tenant_id,
    )


async def handle_update_workspace_sprint_item(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: update_workspace_sprint_item."""
    validate_input_size(args.get("title"), "sprint item title", 500)
    item = await db_module.update_workspace_sprint_item(
        db, args["item_id"],
        title=args.get("title"),
        status=args.get("status"),
        item_group=args.get("group"),
        human_id=args.get("human_id"),
        tenant_id=_mcp_tenant_id,
    )
    return item or {"error": "workspace sprint item not found"}


async def handle_complete_workspace_sprint_item(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: complete_workspace_sprint_item."""
    item = await db_module.complete_workspace_sprint_item(
        db, args["item_id"], tenant_id=_mcp_tenant_id,
    )
    return item or {"error": "workspace sprint item not found"}


# ---------------------------------------------------------------------------
# Section 14: Workspace proposals (5c4dcc0f)
# ---------------------------------------------------------------------------

async def handle_add_workspace_proposal(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: add_workspace_proposal (5c4dcc0f — workspace proposals lifecycle).

    867317f6 — accepts an optional ``idempotency_key`` so a caller that
    retries after a network blip (or an ambiguous timeout) doesn't create a
    second proposal for the same intent; a schema-mid-migration failure on
    this backend comes back as a deterministic ``{"error": ...}`` instead of
    an unhandled exception."""
    validate_input_size(args.get("title"), "proposal title", 500)
    validate_input_size(args.get("body"), "proposal body", 100_000)
    try:
        return await db_module.add_workspace_proposal(
            db, args["title"], args["body"],
            tags=args.get("tags"),
            tenant_id=_mcp_tenant_id,
            family_id=args.get("family_id"),
            idempotency_key=args.get("idempotency_key"),
        )
    except ProposalSchemaError as exc:
        return {"error": str(exc)}


async def handle_get_workspace_proposals(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_workspace_proposals.

    a8afd8f9 — optional ``project_id``/``project_name`` (the dispatcher
    already resolved project_name to project_id before this handler runs)
    restricts the listing to that project's proposals only. Omitted (the
    default) preserves the unchanged prior behavior."""
    return await db_module.get_workspace_proposals(
        db, status=args.get("status"), tag=args.get("tag"),
        tenant_id=_mcp_tenant_id,
        limit=int(args.get("limit", 20)),
        offset=int(args.get("offset", 0)),
        family_id=args.get("family_id"),
        sort_by=args.get("sort_by", "activity"),
        project_id=args.get("project_id") or None,
    )


async def handle_advance_proposal_status(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: advance_proposal_status."""
    try:
        result = await db_module.advance_workspace_proposal_status(
            db, args["proposal_id"], args["status"],
            tenant_id=_mcp_tenant_id,
        )
    except (ValueError, ProposalSchemaError) as exc:
        # ValueError covers both an invalid target status and 867317f6's
        # lost-transition-race case; ProposalSchemaError covers a
        # mid-migration schema on this backend. Both are deterministic,
        # actionable {"error": ...} responses rather than a raw exception.
        return {"error": str(exc)}
    return result or {"error": "proposal not found"}


async def handle_promote_proposal(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: promote_proposal.

    a8afd8f9 — optional ``allow_project_transfer`` + ``transfer_reason``
    acknowledge promoting a project-scoped proposal into a DIFFERENT project
    than the one it was created under; omitted, a scope mismatch is rejected
    (see ``promote_workspace_proposal``'s docstring)."""
    _promo_project_id = args.get("project_id") or ""
    if not _promo_project_id:
        return {"error": "project_id (or project_name) is required for promote_proposal"}
    try:
        result = await db_module.promote_workspace_proposal(
            db, args["proposal_id"], _promo_project_id,
            sprint_item_title=args.get("sprint_item_title"),
            sprint_item_version=args.get("sprint_item_version"),
            tenant_id=_mcp_tenant_id,
            touches_resources=args.get("touches_resources"),
            infer_touches_resources=args.get("infer_touches_resources", False),
            file_github_issue=args.get("file_github_issue", False),
            allow_project_transfer=args.get("allow_project_transfer", False),
            transfer_reason=args.get("transfer_reason"),
        )
    except (ValueError, ProposalSchemaError) as exc:
        # ValueError covers not-found/wrong-state, 867317f6's lost
        # double-promotion race, and a8afd8f9's project-scope mismatch;
        # ProposalSchemaError covers a mid-migration schema on this backend.
        # Both come back deterministically instead of an unhandled exception
        # or a silently-duplicated sprint item.
        return {"error": str(exc)}
    return result


# ---------------------------------------------------------------------------
# Section 14b: Project-scoped proposals as the default (a8afd8f9)
# ---------------------------------------------------------------------------

async def handle_add_proposal(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: add_proposal (a8afd8f9 — project-scoped proposals as the
    default entry point; add_workspace_proposal remains the explicit
    workspace-global opt-in, unchanged).

    Requires the caller to be explicit about scope — never inferred from an
    absent id: pass ``project_id`` (or ``project_name``, already resolved to
    project_id by the dispatcher before this handler runs) for a
    project-scoped proposal, XOR pass ``scope='workspace'`` to explicitly
    opt into the original workspace-global behavior. Neither, or both, is a
    hard {"error": ...} — never a guess."""
    validate_input_size(args.get("title"), "proposal title", 500)
    validate_input_size(args.get("body"), "proposal body", 100_000)
    _project_id = args.get("project_id") or ""
    _scope = (args.get("scope") or "").strip().lower()
    if _scope and _scope not in ("project", "workspace"):
        return {"error": f"Invalid scope '{_scope}' for add_proposal. Use 'project' or 'workspace'."}
    if _scope == "workspace":
        if _project_id:
            return {
                "error": "add_proposal got both project_id (or project_name) and "
                "scope='workspace' — pass one or the other, not both."
            }
    elif not _project_id:
        return {
            "error": "add_proposal requires project_id (or project_name) to scope "
            "the proposal to a project, or scope='workspace' to explicitly opt "
            "into a workspace-global proposal instead (like add_workspace_proposal)."
        }
    try:
        return await db_module.add_workspace_proposal(
            db, args["title"], args["body"],
            tags=args.get("tags"),
            tenant_id=_mcp_tenant_id,
            family_id=args.get("family_id"),
            idempotency_key=args.get("idempotency_key"),
            project_id=_project_id or None,
        )
    except (ValueError, ProposalSchemaError) as exc:
        # ValueError: the given project_id doesn't resolve to a real project.
        # ProposalSchemaError: a mid-migration schema on this backend.
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Section 15: Configurable proposal-to-handoff promotion (ce4883f3)
# ---------------------------------------------------------------------------

async def handle_preview_proposal_promotion(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: preview_proposal_promotion (ce4883f3). Read-only."""
    from meridian import proposal_promotion  # noqa: PLC0415

    _project_id = args.get("project_id") or ""
    if not _project_id:
        return {"error": "project_id (or project_name) is required for preview_proposal_promotion"}
    _depth = args.get("depth") or ""
    try:
        return await proposal_promotion.preview_proposal_promotion(
            db, args["proposal_id"], _project_id, _depth,
            tenant_id=_mcp_tenant_id,
            sprint_item_title=args.get("sprint_item_title"),
            sprint_item_version=args.get("sprint_item_version"),
            touches_resources=args.get("touches_resources"),
            infer_touches_resources=args.get("infer_touches_resources", True),
        )
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_commit_proposal_promotion(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: commit_proposal_promotion (ce4883f3).

    Requires a fresh ``preview_hash`` from ``preview_proposal_promotion``
    (called with the SAME arguments) — a stale/mismatched hash is reported
    as ``{"error": ...}`` (StalePreviewError) rather than silently committed.
    """
    from meridian import proposal_promotion  # noqa: PLC0415

    _project_id = args.get("project_id") or ""
    if not _project_id:
        return {"error": "project_id (or project_name) is required for commit_proposal_promotion"}
    _depth = args.get("depth") or ""
    _preview_hash = args.get("preview_hash") or ""
    if not _preview_hash:
        return {"error": "preview_hash is required — call preview_proposal_promotion first"}
    try:
        return await proposal_promotion.commit_proposal_promotion(
            db, args["proposal_id"], _project_id, _depth, _preview_hash,
            tenant_id=_mcp_tenant_id,
            actor=args.get("actor"),
            session_id=args.get("session_id"),
            sprint_item_title=args.get("sprint_item_title"),
            sprint_item_version=args.get("sprint_item_version"),
            touches_resources=args.get("touches_resources"),
            infer_touches_resources=args.get("infer_touches_resources", True),
            investigation_findings=args.get("investigation_findings"),
            pointers=args.get("pointers"),
            data_dir=data_dir,
            override_reason=args.get("override_reason"),
        )
    except (ValueError, proposal_promotion.StalePreviewError) as exc:
        # StalePreviewError is itself a ValueError subclass; listed
        # explicitly for readability. Covers stale hash, unknown depth,
        # not-found proposal/project, and malformed pointer shapes — all
        # deterministic {"error": ...} responses instead of a raw exception.
        return {"error": str(exc)}
