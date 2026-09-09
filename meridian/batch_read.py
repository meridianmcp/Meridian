"""133bfff6 -- generalized, domain-aware CONCURRENT batch-read engine.

This is the read-side counterpart to ``meridian.db.batch_management``
(86e4ae44, the transactional/idempotent management-WRITE engine) and
``meridian.batch_ops``/``meridian.db.batch_management.execute_mixed_mutation_batch``
(the mutation side wired up by this same item as ``batch_mutate``). This
module is exclusively about READS -- it never writes to the database.

Request shape
--------------
``batch_read`` takes a list of typed request dicts::

    {
        "request_id": "r1",            # required, unique within the batch
        "adapter": "sprint_board",     # required, a registered adapter name
        "operation": "get_sprint_items",  # required, an operation the adapter exposes
        "args": {...},                 # optional, defaults to {}
        "depends_on": ["r0"],          # optional, list of request_id prerequisites
        "timeout_ms": 5000,            # optional, per-request timeout (default 10_000)
        "cache_policy": "default",     # optional, see "Coalescing" below
    }

Concurrency model
-------------------
Every request with no unresolved dependency is scheduled as its own
``asyncio`` task and they all run CONCURRENTLY via ``asyncio.gather`` -- this
is pure in-process concurrent dispatch, never subagents/worktrees/processes.
A request with ``depends_on`` waits ONLY on an ``asyncio.Event`` per declared
prerequisite id, not on the whole batch, so independent branches of a
dependency graph still overlap. If a prerequisite fails, the dependent is
never executed -- it resolves immediately with ``error_code="DEPENDENCY_FAILED"``.

**Honest caveat on real parallelism**: this engine's own scheduling/ordering
guarantees (concurrent dispatch, dependency-scoped waiting, coalescing) hold
regardless of backend. Whether that concurrency also gets you real wall-clock
speedup at the database layer depends on the backend -- a Postgres pool
(``self._pool.connection()`` per call) hands out genuinely separate
connections so concurrent adapter calls can execute in parallel on the
server; a single shared ``aiosqlite.Connection`` (SQLite, this repo's
dev/test default) serializes actual statement execution through its own
worker thread even though the Python-level scheduling above is still
correct. Tests in this item prove the SCHEDULING claim (independent requests
overlap in wall-clock time; a dependent genuinely waits only on its own
prerequisite) using INJECTED test adapters with ``asyncio.sleep`` rather than
relying on real DB-level parallelism, which is backend-dependent and not
this engine's own contract to prove.

**Bounded, not unbounded (d17a437a).** This engine has never had a
per-adapter concurrency limiter of its own, and still doesn't -- it bounds
fan-out the same two ways it always has: ``max_requests`` caps the size of
any one batch (default :data:`DEFAULT_MAX_BATCH_REQUESTS`, hard call-level
rejection above that), and ``timeout_ms`` bounds each individual request.
The ``tunnel_research`` adapter (below) does not add a THIRD, adapter-local
limiter either -- for a tunnel-routed ``call``, the real in-flight cap
already lives one layer down, in ``meridian.routes.tunnel``'s own
per-(slot, tenant) ``asyncio.Semaphore`` (``_slot_semaphore`` /
``_max_slot_inflight``) that every ``call_tunnel_tool`` invocation already
goes through -- reused automatically because this adapter calls into that
same path, never bypassed or re-implemented here.

Coalescing
-----------
Two requests with the SAME ``adapter``, ``operation``, normalized ``args``
(``json.dumps(args, sort_keys=True)``), and the SAME ``depends_on`` SET
coalesce to one execution: the first occurrence (input order) is the
canonical request and actually runs; the rest await its result and copy it
in with ``cache_hit=True`` and ``coalesced_with=<canonical request_id>``.
Requiring the same ``depends_on`` set (not just adapter/operation/args) is a
deliberate scope decision -- two requests that are otherwise identical but
wait on different prerequisites have an ambiguous "when should this actually
run" semantics if merged, so this engine does not attempt to merge them.
Passing a non-default ``cache_policy`` (anything other than
``None``/``""``/``"default"``) opts a request OUT of coalescing entirely --
it always executes fresh and is never treated as a duplicate of, or a
canonical target for, any other request.

Adapters implemented vs deferred
-----------------------------------
* **sprint_board** (implemented) -- read-only wraps of existing
  ``meridian.db`` functions, never reimplemented: ``get_sprint_items`` ->
  :func:`meridian.db.get_sprint_items`, ``get_sprint_item_pointers`` ->
  :func:`meridian.db.get_sprint_item_pointers` (with an added project-
  ownership check the raw DB function itself does not have, so a
  ``sprint_item_id`` from a different project can never leak pointers
  through this read surface -- mirrors the isolation
  ``_validate_sprint_item_entry`` already enforces on the write side).
* **profile** (implemented, PROFILE-7 77369699) -- read-only wraps of the
  PROFILE-1/PROFILE-2 layered-profile persistence
  (:mod:`meridian.db.profile_layers`), reusing every function AS-IS:
  ``get_profile_layer`` -> :func:`meridian.db.get_profile_layer`,
  ``list_profile_layers`` -> :func:`meridian.db.list_profile_layers`,
  ``get_effective_profile`` -> :func:`meridian.db.get_effective_profile`
  (the flagship op -- returns the fully merged, generation-keyed effective
  profile), ``get_profile_layer_revisions`` ->
  :func:`meridian.db.get_profile_layer_revisions`. ``get_profile_layer``/
  ``get_profile_layer_revisions`` deliberately do NOT add a project-ownership
  check on top of the raw DB functions -- both require the CALLER to already
  know the exact target ``scope_id``, so ``(scope_type, scope_id)`` not being
  inherently project-scoped for ``hosted_default``/``workspace``/``user``
  scopes is not a new exposure (PROFILE-5's own ``get_profile_layer`` MCP
  tool has no such gate either; this adapter exposes the identical read
  surface through a different transport, not new authorization semantics).
  ``list_profile_layers`` is different: it is a bulk-enumeration primitive
  that needs no prior knowledge of any other project's identifiers, so
  ``project``/``session`` scope_type rows (the two scope types that ARE
  tied to one project each) ARE filtered to the calling ``project_id`` --
  see :func:`_op_list_profile_layers` -- mirroring ``sprint_board``'s own
  project-ownership gate on ``get_sprint_item_pointers``.
  ``hosted_default``/``workspace``/``user`` rows are not project-scoped and
  are never filtered.
* **tunnel_research** (implemented, d17a437a) -- bounded, READ-ONLY fan-out to
  the code-intel / meridian-docs / meridian-outputs MCP surfaces, closing (in
  part) the gap the "code / codebase-memory / Serena-style reads" bullet
  above used to describe as deferred. The premise of that old bullet --
  "building a 'code' adapter here would mean embedding an MCP CLIENT inside
  this engine ... a materially larger, separate piece of work" -- predates
  1365e01a, which gave the ``meridian`` server process itself a generic,
  already-live way to forward a bare tool call to whatever MCP server a
  caller's ``meridian --tunnel`` client has wired onto ANY connected slot
  (``meridian.mcp.handler._tunnel_proxy_outputs_tool``, originally written
  for ``search_outputs``/``annotate_outputs`` but generic in every line of
  its own implementation -- it keys purely off the bare tool name, never
  anything outputs-specific). This adapter REUSES that exact function
  (imported, never re-implemented) plus ``meridian.routes.tunnel``'s existing
  ``has_active_tunnel``/``_label_maps``/``_tunnel_tool_routes`` readiness
  primitives -- the same ones ``get_tunnel_diagnostics`` itself is built on
  -- rather than inventing a parallel connectivity check or a real embedded
  MCP client. Two operations:

  * ``diagnostics`` -- read-only tunnel/slot readiness snapshot for the
    ``code``/``docs``/``outputs`` slots: which are connected
    (``surfaces.<name>.slot_connected``, via ``_label_maps``) and which
    specific tool names are currently routable on them
    (``routed_tools.<name>``, via ``_tunnel_tool_routes``). Pass
    ``{"refresh": true}`` to force a fresh ``list_tunnel_tools`` discovery
    pass first (bounded and timeout-guarded by that function itself, not
    reimplemented here). Never raises for a missing tenant/session context --
    degrades to an honest all-disconnected snapshot instead.
  * ``call`` -- dispatches ONE tool call (``{"tool": ..., "arguments": {...}}``)
    through whichever connected slot currently serves it. ``tool`` MUST be on
    this module's own fixed READ-ONLY allowlist
    (:data:`_TUNNEL_RESEARCH_TOOL_SURFACE`) -- an unrecognized OR known-
    mutating name is rejected as ``VALIDATION_ERROR`` before any dispatch is
    attempted, never forwarded blind. Checks ``has_active_tunnel`` itself
    first (for an actionable message) even though the reused proxy function
    already no-ops on a dead tunnel -- both paths resolve to ``NOT_FOUND``
    when the target surface genuinely isn't reachable right now, never a
    silent/successful-looking empty result.

  Still explicitly DEFERRED, per this same read of the ground truth: a
  literal embedded MCP client (its own handshake/session/auth lifecycle
  independent of Meridian's own tunnel) remains out of scope -- this adapter
  can only ever reach a surface the CALLER has already wired onto their own
  ``meridian --tunnel`` connection, never an MCP server the calling agent
  connects to directly and separately from Meridian (this session's own
  ``meridian-outputs``/``meridian-docs``/``meridian-extract`` connections,
  for instance, are exactly that -- invisible to this adapter on purpose).
  Model2Vec reranking remains out of scope too, unrelated to this item.

Response shape
---------------
``{"results": [...], "elapsed_ms": <float>}`` -- ``results`` is ALWAYS in
INPUT order (mirrors ``batch_management.BatchResult.results``'s ordering
contract). Each entry is::

    {
        "request_id": ..., "status": "ok" | "error",
        "adapter": ..., "operation": ...,
        "result": <adapter return value> | None,
        "error_code": None | "VALIDATION_ERROR" | "ADAPTER_NOT_FOUND" |
            "OPERATION_NOT_FOUND" | "DEPENDENCY_NOT_FOUND" |
            "DEPENDENCY_CYCLE" | "DEPENDENCY_FAILED" | "NOT_FOUND" |
            "TIMEOUT" | "INTERNAL_ERROR",
        "error_message": None | str,
        "elapsed_ms": <float>,   # this request's OWN execution time (0 for
                                  # a coalesced duplicate or a pre-resolved
                                  # structural error)
        "cache_hit": bool,
        "coalesced_with": None | <canonical request_id>,
    }
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from . import db as db_module

#: Signature every adapter operation must implement:
#: ``async def op(db, project_id, args: dict) -> Any``.
#: Raise ``ValueError`` for a bad/malformed ``args`` shape (reported as
#: ``VALIDATION_ERROR``) or ``LookupError`` for a not-found target (reported
#: as ``NOT_FOUND``); any other exception is reported as ``INTERNAL_ERROR``.
#:
#: d17a437a -- an operation MAY additionally declare a ``tenant_id`` keyword
#: parameter (``async def op(db, project_id, args, *, tenant_id=None)``) to
#: receive the AUTHENTICATED tenant_id ``batch_read()`` itself was called
#: with (see :func:`_accepts_tenant_id`). This is additive and fully
#: backward-compatible: every pre-existing 3-arg operation (``sprint_board``,
#: ``profile``) is dispatched exactly as before, byte-for-byte -- only an
#: operation that opts in by declaring the parameter receives it. Needed
#: because ``tenant_id`` is a call-level, caller-authenticated value (never
#: something a REQUEST's own ``args`` should be trusted to carry -- that
#: would let a batch_read caller impersonate an arbitrary tenant), so it must
#: be threaded from the engine's own dispatch loop, not smuggled through
#: ``args``.
AdapterOperation = Callable[..., Awaitable[Any]]


def _accepts_tenant_id(fn: "AdapterOperation") -> bool:
    """True when *fn* declares a ``tenant_id`` parameter (by name, or via
    ``**kwargs``) -- see :data:`AdapterOperation`'s docstring. Never raises:
    a signature that can't be introspected (e.g. some C-implemented
    callables) is treated as NOT accepting it, matching every adapter
    operation's own pre-existing 3-arg calling convention."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "tenant_id" in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )

#: Default cap on requests per call -- same precedent as
#: ``batch_management.DEFAULT_MAX_BATCH_ENTRIES``.
DEFAULT_MAX_BATCH_REQUESTS = 100

#: Default per-request timeout when a request omits ``timeout_ms``.
DEFAULT_TIMEOUT_MS = 10_000


class BatchReadRequestError(ValueError):
    """A CALL-level contract violation (bad/empty/oversized ``requests``,
    a non-object request, or a duplicate ``request_id``). Distinct from a
    per-request failure, which is NEVER raised -- it comes back as an
    ``"error"``-status entry in the normally-returned response.
    """


# ---------------------------------------------------------------------------
# sprint_board adapter -- thin, read-only wraps of existing meridian.db reads.
# ---------------------------------------------------------------------------

async def _op_get_sprint_items(db: Any, project_id: str, args: "dict[str, Any]") -> Any:
    kwargs: dict[str, Any] = {}
    for key in (
        "status", "show_blocked", "include_human", "version",
        "include_manual_blocker", "include_deferred",
    ):
        if key in args:
            kwargs[key] = args[key]
    return await db_module.get_sprint_items(db, project_id, **kwargs)


async def _op_get_sprint_item_pointers(db: Any, project_id: str, args: "dict[str, Any]") -> Any:
    sprint_item_id = args.get("sprint_item_id")
    if not isinstance(sprint_item_id, str) or not sprint_item_id.strip():
        raise ValueError("get_sprint_item_pointers requires a non-empty 'sprint_item_id'")
    # Project isolation: get_sprint_item_pointers(sprint_item_id) itself has
    # no project scoping (it never did on the write side either -- see
    # sprint_items.py), so this adapter enforces it explicitly: a
    # sprint_item_id belonging to a DIFFERENT project must never leak its
    # pointers through this read surface.
    item = await db_module.get_sprint_item(db, sprint_item_id)
    if item is None or item.get("project_id") != project_id:
        raise LookupError(f"sprint item not found in project: {sprint_item_id}")
    return await db_module.get_sprint_item_pointers(db, sprint_item_id)


# ---------------------------------------------------------------------------
# profile adapter -- thin, read-only wraps of meridian.db.profile_layers
# (PROFILE-7 77369699). See the module docstring's "profile" bullet for the
# deliberate absence of a project-ownership gate on the scope-keyed ops.
# ---------------------------------------------------------------------------

async def _op_get_profile_layer(db: Any, project_id: str, args: "dict[str, Any]") -> Any:
    scope_type = args.get("scope_type")
    scope_id = args.get("scope_id")
    if not isinstance(scope_type, str) or not scope_type.strip():
        raise ValueError("get_profile_layer requires a non-empty 'scope_type'")
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise ValueError("get_profile_layer requires a non-empty 'scope_id'")
    return await db_module.get_profile_layer(db, scope_type, scope_id)


async def _op_list_profile_layers(db: Any, project_id: str, args: "dict[str, Any]") -> Any:
    scope_type = args.get("scope_type")
    if scope_type is not None and (not isinstance(scope_type, str) or not scope_type.strip()):
        raise ValueError("list_profile_layers 'scope_type', when given, must be a non-empty string")
    rows = await db_module.list_profile_layers(db, scope_type)
    # Project isolation (security fix, PROFILE-7 77369699 review): unlike
    # get_profile_layer/get_profile_layer_revisions (which require already
    # knowing the exact target scope_id -- see this module's docstring),
    # list_profile_layers is a bulk-enumeration primitive that needs no
    # prior knowledge of any other project's identifiers. 'project' and
    # 'session' scope_type rows ARE tied to one project each, so left
    # unfiltered this op would let any caller enumerate every OTHER
    # project's project-scoped/session-scoped profile-layer rows just by
    # passing scope_type='project' (or nothing at all). hosted_default/
    # workspace/user rows are NOT project-scoped and pass through
    # unfiltered -- mirrors _op_get_sprint_item_pointers's own
    # project-ownership gate above.
    session_ids: "set[str] | None" = None
    if any(row.get("scope_type") == "session" for row in rows):
        sessions = await db_module.get_sessions(db, project_id, active_only=False)
        session_ids = {s["id"] for s in sessions if s.get("id")}
    filtered: "list[dict[str, Any]]" = []
    for row in rows:
        row_scope_type = row.get("scope_type")
        if row_scope_type == "project" and row.get("scope_id") != project_id:
            continue
        if row_scope_type == "session" and row.get("scope_id") not in (session_ids or set()):
            continue
        filtered.append(row)
    return filtered


async def _op_get_effective_profile(db: Any, project_id: str, args: "dict[str, Any]") -> Any:
    # get_effective_profile raises ValueError for an unknown project_id --
    # this engine's dispatch loop maps ValueError -> VALIDATION_ERROR
    # generically, so no extra try/except is needed here (see batch_read's
    # own docstring: "Raise ValueError for a bad/malformed args shape").
    return await db_module.get_effective_profile(
        db, project_id,
        session_id=args.get("session_id"),
        user_scope_id=args.get("user_scope_id"),
        workspace_scope_id=args.get("workspace_scope_id", "singleton"),
    )


async def _op_get_profile_layer_revisions(db: Any, project_id: str, args: "dict[str, Any]") -> Any:
    scope_id = args.get("scope_id")
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise ValueError("get_profile_layer_revisions requires a non-empty 'scope_id'")
    limit = args.get("limit", 50)
    return await db_module.get_profile_layer_revisions(db, scope_id, limit=limit)


# ---------------------------------------------------------------------------
# tunnel_research adapter -- bounded, READ-ONLY cross-MCP fan-out (d17a437a).
# See the module docstring's "tunnel_research" bullet for the full rationale;
# this section only carries the allowlists and the two operations.
#
# Reuses, never duplicates:
#   * meridian.mcp.handler._tunnel_proxy_outputs_tool -- the actual dispatch
#     primitive (routing-cache scan + call_tunnel_tool + MCP content unwrap).
#   * meridian.routes.tunnel.has_active_tunnel / _label_maps /
#     _tunnel_tool_routes -- the exact same readiness signals
#     get_tunnel_diagnostics itself is built on.
# Both are imported lazily, inside the operation functions below, matching
# this codebase's established convention for avoiding import-time cycles
# with the (large, FastAPI-route-carrying) modules they live in -- see e.g.
# meridian.mcp.handlers.sprint_tools.handle_batch_read's own lazy
# `from ... import batch_read` for the identical reason in the other
# direction.
# ---------------------------------------------------------------------------

#: Known-read-only tool names on the "code" tunnel slot (codebase-memory-mcp
#: / Serena-style servers -- see AGENTS.md's "Code intelligence" section).
#: Deliberately excludes every write-shaped tool on those same servers
#: (replace_symbol_body, insert_after_symbol, insert_before_symbol,
#: rename_symbol, safe_delete_symbol, replace_content, write_memory,
#: edit_memory, delete_memory, rename_memory, index_repository,
#: ingest_traces, manage_adr, delete_project) -- this adapter is READ-ONLY
#: and will never dispatch a tool not on this list, full stop.
_CODE_INTEL_READONLY_TOOLS: "frozenset[str]" = frozenset({
    "search_graph", "trace_path", "get_code_snippet", "query_graph",
    "get_architecture", "search_code", "search_code_semantic", "index_status",
    "get_symbols_overview", "find_symbol", "find_referencing_symbols",
    "find_declaration", "find_implementations", "get_diagnostics_for_file",
    "list_memories", "read_memory", "list_projects",
})

#: Known-read-only tool names on the "docs" tunnel slot (extensions/meridian-docs
#: -- see extensions/meridian-docs/meridian_docs/server.py). Excludes every
#: insert_*/edit_*/remove_*/write_*/apply_*/index_*/ingest_*/render_*/
#: relocate_*/move_*/copy_*/renumber_*/sync_* tool on that same server.
_DOCS_READONLY_TOOLS: "frozenset[str]" = frozenset({
    "document_outline", "parse_document", "get_structure", "get_structure_elements",
    "get_paragraph", "search_paragraphs", "search_document", "read_document_snapshot",
    "locate_anchor", "locate_anchors", "get_document_review", "audit_document",
    "check_render_capability", "list_render_receipts", "check_release_render_gate",
    "extract_equations", "get_equations", "audit_equation_style",
    "get_journal_style_preset", "scan_citation_keys", "format_reference",
    "get_section_content", "find_references_to", "scan_stale_notes",
    "list_internal_notes", "audit_equation_integrity", "compare_equation_structures",
    "find_orphaned_docx_staged_files",
})

#: Known-read-only tool names on the "outputs" tunnel slot (extensions/meridian-outputs
#: -- see extensions/meridian-outputs/meridian_outputs/server.py). Excludes
#: register_output_paths, annotate_outputs, record_provenance,
#: bind_artifact_provenance, register_artifact, bind_artifact_source_edge,
#: reconcile_legacy_artifact_outputs, tag_output, start_run_manifest,
#: finalize_run_manifest on that same server.
_OUTPUTS_READONLY_TOOLS: "frozenset[str]" = frozenset({
    "search_outputs", "get_convergence_state", "inspect_local_file",
    "get_provenance", "get_provenance_status", "list_provenance",
    "get_provenance_status_envelope", "get_evidence_status_and_trusted_pointers",
    "classify_outputs", "resolve_figure_output", "find_outputs_by_source",
    "resolve_artifact", "verify_artifact_hash", "get_artifact_sources",
    "get_source_artifacts", "list_registered_artifacts", "npy_metadata",
    "file_fingerprint", "search_logs", "check_staleness", "find_stale_by_script",
    "script_content_hash", "get_run_manifest", "list_run_manifests",
    "get_run_manifest_envelope",
})

#: Tunnel slot label (meridian.routes.tunnel._label_maps's own vocabulary)
#: for each of the three research surfaces this adapter fans out across.
_TUNNEL_RESEARCH_SLOTS: "dict[str, str]" = {"code": "code", "docs": "docs", "outputs": "outputs"}

#: bare tool name -> surface name, the actual allowlist `call` enforces.
_TUNNEL_RESEARCH_TOOL_SURFACE: "dict[str, str]" = {
    **{name: "code" for name in _CODE_INTEL_READONLY_TOOLS},
    **{name: "docs" for name in _DOCS_READONLY_TOOLS},
    **{name: "outputs" for name in _OUTPUTS_READONLY_TOOLS},
}


async def _op_tunnel_diagnostics(
    db: Any, project_id: str, args: "dict[str, Any]", *, tenant_id: "str | None" = None,
) -> Any:
    """``tunnel_research.diagnostics`` -- read-only tunnel/slot readiness.

    Never assumes a slot is up: reports exactly what
    ``meridian.routes.tunnel``'s own live registries say right now, the same
    ones ``get_tunnel_diagnostics`` reads. Never raises for a missing
    tenant/session context -- degrades to an honest all-disconnected snapshot
    (this is a batch_read operation; ValueError/LookupError here would
    surface as a per-request VALIDATION_ERROR/NOT_FOUND, which would be
    misleading for what is simply "no tunnel context available yet").
    """
    from ._deps import _hosted_mode  # noqa: PLC0415 -- avoid a module-load cycle
    from .routes import tunnel as _tunnel_mod  # noqa: PLC0415 -- ditto

    routed_tools: "dict[str, list[str]]" = {name: [] for name in _TUNNEL_RESEARCH_SLOTS}
    if not tenant_id:
        return {
            "tenant_id": None,
            "hosted": _hosted_mode(),
            "tunnel_active": False,
            "surfaces": {name: {"slot_connected": False} for name in _TUNNEL_RESEARCH_SLOTS},
            "routed_tools": routed_tools,
            "reason": "no tenant/session context available for this batch_read call",
        }

    if args.get("refresh"):
        try:
            await _tunnel_mod.list_tunnel_tools(tenant_id)
        except Exception:  # noqa: BLE001 -- diagnostics must never fail on a refresh error
            pass

    surfaces: "dict[str, Any]" = {}
    for name, slot_label in _TUNNEL_RESEARCH_SLOTS.items():
        sockets, _ = _tunnel_mod._label_maps(slot_label)
        surfaces[name] = {"slot_connected": tenant_id in sockets}

    routes = _tunnel_mod._tunnel_tool_routes.get(tenant_id) or {}
    for prefixed_name in routes:
        bare_name = prefixed_name.rsplit("__", 1)[-1] if "__" in prefixed_name else prefixed_name
        surface = _TUNNEL_RESEARCH_TOOL_SURFACE.get(bare_name)
        if surface is not None:
            routed_tools[surface].append(bare_name)

    return {
        "tenant_id": tenant_id,
        "hosted": _hosted_mode(),
        "tunnel_active": _tunnel_mod.has_active_tunnel(tenant_id),
        "surfaces": surfaces,
        "routed_tools": routed_tools,
    }


async def _op_tunnel_call(
    db: Any, project_id: str, args: "dict[str, Any]", *, tenant_id: "str | None" = None,
) -> Any:
    """``tunnel_research.call`` -- dispatch ONE allowlisted read-only research
    tool call through whichever connected tunnel slot currently serves it.

    Reuses ``meridian.mcp.handler._tunnel_proxy_outputs_tool`` verbatim (see
    this section's module-level comment) -- this function's own job is only
    the allowlist gate (never forward a name that is not on
    :data:`_TUNNEL_RESEARCH_TOOL_SURFACE`) and turning "not reachable right
    now" into an honest ``NOT_FOUND`` instead of dispatching blind.
    """
    tool = args.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise ValueError("tunnel_research.call requires a non-empty 'tool' name")
    surface = _TUNNEL_RESEARCH_TOOL_SURFACE.get(tool)
    if surface is None:
        raise ValueError(
            f"tool {tool!r} is not on the tunnel_research read-only allowlist -- "
            "this adapter is READ-ONLY and will never dispatch an unrecognized "
            "or known-mutating tool name (known surfaces: code, docs, outputs)"
        )
    arguments = args.get("arguments")
    if arguments is None:
        arguments = {}
    elif not isinstance(arguments, dict):
        raise ValueError("tunnel_research.call 'arguments', when given, must be an object")

    if not tenant_id:
        raise LookupError(
            "no tenant/session context is available for this batch_read call -- "
            "tunnel_research.call requires an authenticated tunnel session"
        )

    from .routes import tunnel as _tunnel_mod  # noqa: PLC0415 -- avoid a module-load cycle

    if not _tunnel_mod.has_active_tunnel(tenant_id):
        raise LookupError(
            f"no active tunnel for this session -- start `meridian --tunnel` with "
            f"the {surface} MCP server exposed on a connected slot, then retry"
        )

    from .mcp.handler import _tunnel_proxy_outputs_tool as _dispatch_tunnel_tool  # noqa: PLC0415

    result = await _dispatch_tunnel_tool(tenant_id, tool, arguments)
    if result is None:
        raise LookupError(
            f"tool {tool!r} ({surface}) is not exposed on any currently-connected "
            "tunnel slot for this session"
        )
    return result


#: Domain-aware adapter registry: adapter name -> {operation name -> callable}.
#: See the module docstring's "Adapters implemented vs deferred" section.
DEFAULT_ADAPTERS: "dict[str, dict[str, AdapterOperation]]" = {
    "sprint_board": {
        "get_sprint_items": _op_get_sprint_items,
        "get_sprint_item_pointers": _op_get_sprint_item_pointers,
    },
    "profile": {
        "get_profile_layer": _op_get_profile_layer,
        "list_profile_layers": _op_list_profile_layers,
        "get_effective_profile": _op_get_effective_profile,
        "get_profile_layer_revisions": _op_get_profile_layer_revisions,
    },
    "tunnel_research": {
        "diagnostics": _op_tunnel_diagnostics,
        "call": _op_tunnel_call,
    },
}


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

@dataclass
class _ReadResult:
    request_id: str
    status: str
    adapter: "str | None" = None
    operation: "str | None" = None
    result: Any = None
    error_code: "str | None" = None
    error_message: "str | None" = None
    elapsed_ms: float = 0.0
    cache_hit: bool = False
    coalesced_with: "str | None" = None

    def to_dict(self) -> "dict[str, Any]":
        return {
            "request_id": self.request_id,
            "status": self.status,
            "adapter": self.adapter,
            "operation": self.operation,
            "result": self.result,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "cache_hit": self.cache_hit,
            "coalesced_with": self.coalesced_with,
        }


def _str_or_none(value: Any) -> "str | None":
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def batch_read(
    db: Any,
    *,
    project_id: str,
    requests: "list[dict[str, Any]]",
    tenant_id: "str | None" = None,
    adapters: "dict[str, dict[str, AdapterOperation]] | None" = None,
    default_timeout_ms: int = DEFAULT_TIMEOUT_MS,
    max_requests: int = DEFAULT_MAX_BATCH_REQUESTS,
) -> "dict[str, Any]":
    """Dispatch a batch of typed read requests concurrently.

    Parameters
    ----------
    db:
        The shared aiosqlite/Postgres connection (same object every other
        ``meridian.db`` function takes).
    project_id:
        Every ``sprint_board`` operation is scoped to this project.
    requests:
        Non-empty list of request dicts -- see the module docstring for the
        shape.
    tenant_id:
        Accepted for symmetry with ``batch_management``/``batch_ops`` and
        for future adapters that need tenant scoping; unused by the
        ``sprint_board`` adapter today (``get_sprint_items``/
        ``get_sprint_item_pointers`` are project-scoped, not tenant-scoped).
    adapters:
        Optional override of the adapter registry -- defaults to
        :data:`DEFAULT_ADAPTERS`. Tests use this to inject deterministic,
        timing-controlled fake adapters instead of exercising the real DB,
        which is how this item's concurrency/dependency-ordering tests
        prove real overlap without depending on backend-specific parallelism
        (see the module docstring's "Honest caveat on real parallelism").
    default_timeout_ms:
        Fallback per-request timeout when a request omits ``timeout_ms``.
    max_requests:
        Hard cap on ``len(requests)``; exceeding it raises
        :class:`BatchReadRequestError` before anything is attempted.

    Raises
    ------
    BatchReadRequestError:
        For a call-level contract violation. Per-request problems are NEVER
        raised -- they come back as ``"error"``-status entries in the
        normally-returned response.
    """
    if not project_id or not isinstance(project_id, str):
        raise BatchReadRequestError("project_id is required")
    if not isinstance(requests, list) or not requests:
        raise BatchReadRequestError("requests must be a non-empty list")
    if len(requests) > max_requests:
        raise BatchReadRequestError(
            f"requests has {len(requests)} items, exceeding max_requests={max_requests}; "
            "split into smaller batches"
        )
    registry = adapters if adapters is not None else DEFAULT_ADAPTERS

    # ---- Phase 0: structural validation (request_id uniqueness). ----------
    order: list[str] = []
    by_id: "dict[str, dict[str, Any]]" = {}
    for i, raw in enumerate(requests):
        if not isinstance(raw, dict):
            raise BatchReadRequestError(f"request at index {i} must be an object")
        rid = raw.get("request_id")
        if not isinstance(rid, str) or not rid.strip():
            raise BatchReadRequestError(
                f"request at index {i} requires a non-empty 'request_id'"
            )
        if rid in by_id:
            raise BatchReadRequestError(f"duplicate request_id {rid!r} in batch")
        order.append(rid)
        by_id[rid] = raw

    # ---- Phase 1: per-request structural checks (depends_on/args/adapter/
    # operation) -- these become immediate per-request errors, never a
    # call-level rejection (an unrelated well-formed request must still run).
    pre_errors: "dict[str, tuple[str, str]]" = {}
    edges: "dict[str, list[str]]" = {}
    for rid in order:
        raw = by_id[rid]
        deps = raw.get("depends_on") if raw.get("depends_on") is not None else []
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            pre_errors[rid] = ("VALIDATION_ERROR", "depends_on must be a list of request_id strings")
            edges[rid] = []
            continue
        unknown = [d for d in deps if d not in by_id]
        if unknown:
            pre_errors[rid] = ("DEPENDENCY_NOT_FOUND", f"unknown depends_on id(s): {unknown}")
            edges[rid] = [d for d in deps if d in by_id]
            continue
        edges[rid] = deps
        args = raw.get("args")
        if args is not None and not isinstance(args, dict):
            pre_errors[rid] = ("VALIDATION_ERROR", "'args' must be an object")
            continue
        adapter_name = raw.get("adapter")
        if not isinstance(adapter_name, str) or adapter_name not in registry:
            pre_errors[rid] = ("ADAPTER_NOT_FOUND", f"unknown adapter {adapter_name!r}")
            continue
        operation = raw.get("operation")
        if not isinstance(operation, str) or operation not in registry[adapter_name]:
            pre_errors[rid] = (
                "OPERATION_NOT_FOUND",
                f"unknown operation {operation!r} for adapter {adapter_name!r}",
            )
            continue

    # ---- Phase 2: cycle detection over the (best-effort, unknown-id-free)
    # depends_on graph. Any request participating in a cycle can never run.
    _WHITE, _GRAY, _BLACK = 0, 1, 2
    color = {rid: _WHITE for rid in order}
    cyclic: set[str] = set()

    def _visit(rid: str, stack: list[str]) -> None:
        color[rid] = _GRAY
        stack.append(rid)
        for dep in edges[rid]:
            if color[dep] == _WHITE:
                _visit(dep, stack)
            elif color[dep] == _GRAY:
                start = stack.index(dep)
                cyclic.update(stack[start:])
        stack.pop()
        color[rid] = _BLACK

    for rid in order:
        if color[rid] == _WHITE:
            _visit(rid, [])
    for rid in cyclic:
        pre_errors.setdefault(
            rid, ("DEPENDENCY_CYCLE", "this request participates in a depends_on cycle")
        )

    # ---- Phase 3: coalescing -- normalize (adapter, operation, args,
    # depends_on-set) into a key; first occurrence wins as canonical.
    def _normalize_key(rid: str) -> str:
        raw = by_id[rid]
        cache_policy = raw.get("cache_policy")
        if cache_policy not in (None, "", "default"):
            return f"__nocoalesce__:{rid}"
        args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
        payload = {
            "adapter": raw.get("adapter"),
            "operation": raw.get("operation"),
            "args": args,
            "depends_on": sorted(edges[rid]),
        }
        return json.dumps(payload, sort_keys=True, default=str)

    canonical_for: "dict[str, str]" = {}
    key_to_canonical: "dict[str, str]" = {}
    for rid in order:
        if rid in pre_errors:
            canonical_for[rid] = rid
            continue
        key = _normalize_key(rid)
        if key in key_to_canonical:
            canonical_for[rid] = key_to_canonical[key]
        else:
            key_to_canonical[key] = rid
            canonical_for[rid] = rid

    # ---- Phase 4: concurrent execution. ------------------------------------
    events: "dict[str, asyncio.Event]" = {rid: asyncio.Event() for rid in order}
    result_records: "dict[str, _ReadResult]" = {}

    for rid, (code, message) in pre_errors.items():
        raw = by_id[rid]
        result_records[rid] = _ReadResult(
            request_id=rid, status="error",
            adapter=_str_or_none(raw.get("adapter")), operation=_str_or_none(raw.get("operation")),
            error_code=code, error_message=message, elapsed_ms=0.0,
        )
        events[rid].set()

    async def _run_canonical(rid: str) -> None:
        raw = by_id[rid]
        deps = edges[rid]
        if deps:
            await asyncio.gather(*[events[d].wait() for d in deps])
        failed_dep = next((d for d in deps if result_records[d].status != "ok"), None)
        adapter_name = raw.get("adapter")
        operation = raw.get("operation")
        if failed_dep is not None:
            result_records[rid] = _ReadResult(
                request_id=rid, status="error",
                adapter=_str_or_none(adapter_name), operation=_str_or_none(operation),
                error_code="DEPENDENCY_FAILED",
                error_message=f"prerequisite request {failed_dep!r} did not succeed",
                elapsed_ms=0.0,
            )
            events[rid].set()
            return
        args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
        raw_timeout = raw.get("timeout_ms")
        timeout_ms = raw_timeout if isinstance(raw_timeout, (int, float)) and raw_timeout > 0 else default_timeout_ms
        fn = registry[adapter_name][operation]
        start = time.perf_counter()
        try:
            call = (
                fn(db, project_id, args, tenant_id=tenant_id)
                if _accepts_tenant_id(fn) else fn(db, project_id, args)
            )
            value = await asyncio.wait_for(call, timeout=timeout_ms / 1000.0)
            result_records[rid] = _ReadResult(
                request_id=rid, status="ok", adapter=adapter_name, operation=operation,
                result=value, elapsed_ms=(time.perf_counter() - start) * 1000.0,
            )
        except asyncio.TimeoutError:
            result_records[rid] = _ReadResult(
                request_id=rid, status="error", adapter=adapter_name, operation=operation,
                error_code="TIMEOUT", error_message=f"operation exceeded {timeout_ms}ms",
                elapsed_ms=(time.perf_counter() - start) * 1000.0,
            )
        except LookupError as exc:
            result_records[rid] = _ReadResult(
                request_id=rid, status="error", adapter=adapter_name, operation=operation,
                error_code="NOT_FOUND", error_message=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000.0,
            )
        except ValueError as exc:
            result_records[rid] = _ReadResult(
                request_id=rid, status="error", adapter=adapter_name, operation=operation,
                error_code="VALIDATION_ERROR", error_message=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 -- unexpected adapter/driver error
            result_records[rid] = _ReadResult(
                request_id=rid, status="error", adapter=adapter_name, operation=operation,
                error_code="INTERNAL_ERROR", error_message=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000.0,
            )
        finally:
            events[rid].set()

    async def _run_duplicate(rid: str, canonical_rid: str) -> None:
        await events[canonical_rid].wait()
        canonical_result = result_records[canonical_rid]
        result_records[rid] = replace(
            canonical_result, request_id=rid, cache_hit=True,
            coalesced_with=canonical_rid, elapsed_ms=0.0,
        )
        events[rid].set()

    tasks = []
    for rid in order:
        if rid in pre_errors:
            continue
        if canonical_for[rid] == rid:
            tasks.append(asyncio.create_task(_run_canonical(rid)))
    for rid in order:
        if rid in pre_errors:
            continue
        if canonical_for[rid] != rid:
            tasks.append(asyncio.create_task(_run_duplicate(rid, canonical_for[rid])))

    start_all = time.perf_counter()
    if tasks:
        await asyncio.gather(*tasks)
    total_elapsed_ms = (time.perf_counter() - start_all) * 1000.0

    return {
        "results": [result_records[rid].to_dict() for rid in order],
        "elapsed_ms": round(total_elapsed_ms, 3),
    }
