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
* **code / codebase-memory / Serena-style reads** (explicitly DEFERRED, per
  this item's own scoping note: "or document that this adapter proxies to
  an external MCP call"). No local, in-process code-search/graph capability
  exists anywhere in the ``meridian`` package today -- ``search_graph``,
  ``find_symbol``, ``trace_path``, etc. are separate MCP servers
  (``meridian-code``, ``codebase-memory-mcp``, Serena) that a calling agent
  invokes directly over its OWN MCP connection, never proxied through
  Meridian's own server process. Building a "code" adapter here would mean
  embedding an MCP CLIENT inside this engine to fan out to those servers --
  a materially larger, separate piece of work (a real client, connection
  lifecycle, auth/tunnel plumbing) rather than a thin DB wrapper like
  ``sprint_board``. Flagged as explicit follow-up work, not implemented.
* **meridian-docs / meridian-outputs adapters, Model2Vec reranking** --
  explicitly OUT of scope per this item's own acceptance criteria (listed as
  "preferred", not "required"). Not implemented; no shallow/fake stand-in
  added either.

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
AdapterOperation = Callable[[Any, str, "dict[str, Any]"], Awaitable[Any]]

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
            value = await asyncio.wait_for(fn(db, project_id, args), timeout=timeout_ms / 1000.0)
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
