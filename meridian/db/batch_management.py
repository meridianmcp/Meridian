"""86e4ae44 -- shared transactional batch-management write engine.

A single, reusable primitive for processing a **homogeneous** list of
management-write entries -- sprint-item create/update, sprint-item pointer
create, or sprint-note create -- with two selectable semantics:

* ``mode="all_or_nothing"`` -- every entry is structurally validated BEFORE
  any mutation runs; if validation finds a problem anywhere in the batch,
  nothing is written. If a mutation still fails partway through (a
  validation rule that only the underlying single-entry function itself
  enforces, e.g. an enum check inside :func:`meridian.db.add_sprint_item`),
  every entry this batch call already wrote is rolled back via a
  compensating action and the overall result is ``"failed"``.
* ``mode="best_effort"`` -- entries are processed independently; one
  entry's failure never prevents the others from succeeding. The overall
  result is ``"ok"`` / ``"partial"`` / ``"failed"`` depending on how many
  entries succeeded.

Both modes return a :class:`BatchResult` carrying an overall ``status`` and
a **deterministic, input-order** list of :class:`BatchEntryResult` --
one per input entry, each reporting its ``id`` (when created/updated),
``error_code`` / ``error_message`` / ``retryable`` (when it failed), and a
free-form ``outcome`` payload -- so a caller can reconcile every entry it
submitted, including duplicates and rolled-back entries, without needing
to fall back to global exception handling.

Worker CLAIM batches (``claim_parallel_batch`` in
:mod:`meridian.db.batch_claim`) are a completely different concept --
"grab N claimable sprint items for an executor" -- and are intentionally
**not** touched or unified with this module. This engine is exclusively
about MANAGEMENT writes: creating/updating sprint items, attaching
pointers, and filing notes.

-------------------------------------------------------------------------
Why this reuses existing single-entry functions instead of re-implementing
their validation
-------------------------------------------------------------------------

Each entry kind's mutation step is a direct call into the SAME function a
single-item MCP tool already uses and already tests:

* ``sprint_item`` (action ``"create"``) -> :func:`meridian.db.add_sprint_item`
  -- the fully validated path (duplicate-title guard, priority/blocker_kind
  enums, touches_resources/tool_requirements/artifact_* normalization).
* ``sprint_item`` (action ``"update"``) -> :func:`meridian.db.patch_sprint_item`
  -- the same ``_UNSET``-sentinel field editor ``update_sprint_item`` uses.
* ``sprint_item_pointer`` -> :func:`meridian.db.add_sprint_item_pointer`
  -- validates via :func:`meridian.pointers.validate_pointer` before writing.
* ``sprint_note`` -> :func:`meridian.db.add_session_note`.

This is a deliberate design choice, not an oversight: duplicating those
functions' business rules here would create a second copy that silently
drifts out of sync every time one of them gains a new field or a new
validation rule (as sprint_items.py very often does -- see its own long
history of ``_UNSET``-sentinel additions). Routing through the real
functions means every existing single-entry validation rule is honored
automatically, with zero duplication.

Both ``add_sprint_item`` and ``patch_sprint_item`` (and
``add_sprint_item_pointer``) already validate BEFORE they issue their
``INSERT``/``UPDATE`` statement -- confirmed by direct reading of their
bodies. That means even a "deep" semantic error only detectable inside the
underlying function (e.g. a bad ``priority`` enum) still guarantees NO row
is written for that specific entry. Combined with this engine's own
compensating rollback of every entry that already succeeded earlier in
the same call, ``all_or_nothing`` delivers a genuine "nothing persisted"
guarantee on failure, without a real multi-statement DB transaction.

-------------------------------------------------------------------------
Why NOT a real DB transaction
-------------------------------------------------------------------------

It is structurally impossible to wrap this batch in a single DB
transaction: every one of the underlying write functions this engine
calls (``add_sprint_item``, ``patch_sprint_item``, ``add_sprint_item_pointer``,
``add_session_note``) already calls ``await db.commit()`` itself. On
Postgres (``autocommit=True``, see ``meridian/pg_adapter.py``) each
statement is independently committed the instant it runs, and
``db.commit()``/``db.rollback()`` are documented no-ops there -- so there
is no pending transaction to roll back even on the very first entry.
SQLite has the same practical effect here because each helper commits
its own implicit transaction before this engine ever gets control back.

This mirrors the exact same conclusion the workspace-proposal hardening
item (867317f6, see ``meridian/db/workspace.py``'s ``_undo_proposal_writes``
and its docstring) reached for the identical problem: on a shared
connection, "rollback" must mean per-row **compensating actions** (a
targeted ``DELETE``/reverting ``UPDATE`` keyed by the row's own id), never
``db.rollback()`` -- a shared aiosqlite connection has exactly ONE implicit
transaction, and blindly rolling it back can discard a different,
still-in-flight caller's uncommitted work. Every ``compensate()`` step in
this module follows that same rule and is fully guarded (never raises --
a failure while undoing a partial write must never mask the original
failure that triggered the rollback).

-------------------------------------------------------------------------
Idempotency, honestly
-------------------------------------------------------------------------

``idempotency_key`` makes a retried call (same key, same ``project_id``,
same ``entry_kind``) return the FIRST call's stored :class:`BatchResult`
verbatim instead of re-executing -- the item's own acceptance criteria ask
for this, citing 867317f6's ``workspace_proposals.idempotency_key`` +
UNIQUE partial index as "the established convention". This module's
declared scope explicitly excludes ``meridian/db/migrations.py`` and
``meridian/db/pg_adapter.py`` (schema files), so adding a brand-new
dedicated table/index -- the literal 867317f6 approach -- is out of reach
here. Instead this reuses the existing, already-migrated
``action_audit_log`` table (no schema change at all -- the same table
``meridian/code_intel_receipt.py`` reuses for prospecting receipts), with
a twist that recovers a REAL uniqueness guarantee without a new index:

The receipt row's ``id`` is not a random uuid -- it is a deterministic
``sha256(tenant_id | project_id | entry_kind | idempotency_key)`` digest.
``action_audit_log.id`` is already a ``TEXT PRIMARY KEY``, so two
concurrent callers racing to insert a receipt with the SAME deterministic
id collide on that existing PRIMARY KEY constraint exactly the way two
racing ``add_workspace_proposal`` calls collide on the 867317f6 UNIQUE
index -- the loser's insert fails, and it re-fetches and returns the
winner's stored result instead (see ``_write_batch_receipt`` /
``_is_unique_violation``, mirroring ``workspace.py``'s
``_is_proposal_unique_violation`` winner-refetch pattern). This makes
idempotency race-safe for TRUE concurrent duplicate calls, not merely
sequential retries -- the one property a naive "SELECT then INSERT"
pre-check cannot provide on its own.

**Known, documented limitation**: unlike a real dedicated table, a stored
receipt in ``action_audit_log`` is never pruned by this module and the
lookup is an exact point lookup by the deterministic id (O(1), not a
scan) -- so this scales fine, but a caller wanting an idempotency window
shorter than "forever" (e.g. TTL-based key reuse) gets no such feature
here. Should that ever be needed, a dedicated ``batch_receipts`` table
with a real TTL/expiry column is the natural follow-up migration -- flagged
here rather than worked around.

-------------------------------------------------------------------------
Never holds a transaction across an external call
-------------------------------------------------------------------------

Every adapter's ``apply``/``compensate`` step in this module is a plain DB
write -- nothing here calls prospecting, web-archive, or a tunnel/connector
tool. This is intentional and structural, not incidental: a caller wanting
to enrich entries with prospecting/web-archive results (the way
``handle_add_sprint_item_pointer`` archives a cited web passage before
calling ``add_sprint_item_pointer``, or ``handle_add_sprint_item`` computes
prospecting hints after creating the item) must do that enrichment OUTSIDE
``execute_batch`` -- before building ``entries`` or after inspecting
``BatchResult`` -- exactly as today's handlers already do around their own
single-entry calls.

-------------------------------------------------------------------------
Compatibility: why fan_out_sprint_items / add_sprint_item_pointer are NOT
rerouted through this engine BY DEFAULT
-------------------------------------------------------------------------

:func:`meridian.db.sprint_items.fan_out_sprint_items` has a DELIBERATELY
different DEFAULT contract from this engine's ``sprint_item`` entry kind:
its own docstring says "the duplicate guard is **not** applied here -- the
orchestrator is assumed to have already deduped". This engine's
``sprint_item`` create path calls ``add_sprint_item``, which DOES enforce
the 60%-word-overlap duplicate guard. Unconditionally rerouting
``fan_out_sprint_items`` through this engine would silently reject
near-duplicate titles that succeed today -- a real behavior change for
every existing caller (the orchestrator fan-out flow, tested in
``tests/test_ba4f879b_sprint_tools_dispatch.py`` and others). Per this
item's own acceptance criteria ("if full rerouting risks behavior changes
for existing callers, it's fine to keep the new engine as an ADDITIVE new
code path"), ``fan_out_sprint_items``'s DEFAULT (``strict=False``) is left
untouched.

468ab67d (a later, focused follow-up) added an explicit, OPT-IN
``strict=True`` parameter to ``fan_out_sprint_items`` itself that DOES
reroute through this exact engine (``execute_batch`` with
``entry_kind="sprint_item"``) -- giving a caller who explicitly asks for it
the duplicate guard, idempotency-key replay, and best_effort/all_or_nothing
semantics documented above, reusing this module's implementation rather
than a second title-overlap heuristic. This is still "additive" in the
sense the acceptance criteria above intended: nothing about the DEFAULT
call shape (no ``strict=`` kwarg passed) changed at all; the new behavior
is reached only through a parameter that did not previously exist. See
``fan_out_sprint_items``'s own docstring for the strict-mode contract.
Separately, this module remains the new, additive, atomic/idempotent path
for callers who want validated + duplicate-guarded + rollback-safe bulk
sprint-item writes without going through ``fan_out_sprint_items`` at all
(e.g. via ``execute_batch``/``batch_ops.execute_batch_operation`` directly).

:func:`meridian.db.sprint_items.add_sprint_item_pointer` needed no changes
at all in the other direction: it is already a clean validate-then-insert
single-entry function with no duplicate-guard complexity, so this engine's
``sprint_item_pointer`` adapter calls it directly, as-is, as its atomic
mutation step -- the "routing" the item's acceptance criteria asks for
happens with the engine consuming the existing function, not the other
way around.

Multi-transport exposure of this engine (new MCP tool schemas across
``meridian/mcp_tools.py``, ``mcp/handler.py``, ``mcp/stdio_handler.py``, HTTP
routes, and docs) is explicitly OUT OF SCOPE for this item -- that is
sprint item 627187b8's job, built on top of :func:`execute_batch`'s public
signature below.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from meridian import db as db_module
from meridian.pointers import validate_pointer

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Entry kinds this engine knows how to process. Every call to
#: :func:`execute_batch` processes ONE homogeneous kind -- mixing kinds in a
#: single call is not supported (matches "homogeneous" in the item title).
BATCH_ENTRY_KINDS: tuple[str, ...] = ("sprint_item", "sprint_item_pointer", "sprint_note")

#: Supported batch modes -- see the module docstring for the semantics of each.
BATCH_MODES: tuple[str, ...] = ("all_or_nothing", "best_effort")

#: Entry kinds accepted by :func:`execute_mixed_mutation_batch` (133bfff6's
#: ``batch_mutate`` engine) -- a deliberately narrow, MIXED-kind sibling of
#: :func:`execute_batch`. Excludes plain sprint-item CREATE and sprint_note
#: entirely; only pointer-attach and sprint-item UPDATE are exposed here. See
#: :func:`execute_mixed_mutation_batch`'s own docstring for why this is a
#: separate engine rather than a mode of :func:`execute_batch`.
MIXED_MUTATION_ENTRY_KINDS: tuple[str, ...] = ("sprint_item_pointer", "sprint_item_update")

#: ``entry_kind`` label :func:`execute_mixed_mutation_batch` stamps on its own
#: idempotency receipts -- distinct from :func:`execute_batch`'s own
#: entry_kind strings ("sprint_item", "sprint_item_pointer", "sprint_note")
#: so an ``idempotency_key`` reused across the homogeneous and mixed engines
#: can never collide on the same ``action_audit_log`` receipt row.
MIXED_MUTATION_RECEIPT_KIND = "batch_mutate_mixed"

#: Default cap on entries per call (mirrors ``handoff.py``'s
#: ``_MAX_ENRICHED_ITEMS = 100`` -- the established "how big is a reasonable
#: bulk operation" precedent elsewhere in this codebase). Callers may pass a
#: different ``max_entries`` to :func:`execute_batch`.
DEFAULT_MAX_BATCH_ENTRIES = 100

#: Per-entry error codes.
ERROR_VALIDATION = "VALIDATION_ERROR"
ERROR_DUPLICATE = "DUPLICATE_TITLE"
ERROR_NOT_FOUND = "NOT_FOUND"
ERROR_INTERNAL = "INTERNAL_ERROR"

#: event_type recorded in action_audit_log for a durable idempotency receipt.
BATCH_RECEIPT_EVENT_TYPE = "batch_management_write"


class BatchEngineError(ValueError):
    """Raised for a CALL-level contract violation.

    Distinct from a per-entry failure (which is captured as a
    :class:`BatchEntryResult` inside a normally-returned :class:`BatchResult`,
    never raised): a bad ``entry_kind``/``mode``, an empty/non-list
    ``entries``, or exceeding ``max_entries`` means the call itself is
    malformed and nothing was attempted for ANY entry.
    """


class _EntryError(Exception):
    """Internal control-flow exception used by entry-kind adapters.

    Carries the same shape a :class:`BatchEntryResult` failure needs
    (``code``, ``message``, ``retryable``, optional ``payload``) so
    :func:`execute_batch` can translate it into a result without any
    adapter needing to know about the result dataclasses.
    """

    def __init__(
        self, code: str, message: str, *, retryable: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.payload = payload


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class BatchEntryResult:
    """The outcome of ONE input entry, at its original input ``index``.

    ``status`` is one of:
      * ``"ok"`` -- mutated successfully; ``id``/``outcome`` are populated.
      * ``"error"`` -- failed (validation or mutation); ``error_code`` /
        ``error_message`` / ``retryable`` are populated.
      * ``"rolled_back"`` -- succeeded, then undone because a LATER entry in
        the same ``all_or_nothing`` call failed (``outcome`` still carries
        the original mutation outcome, plus ``rolled_back: True``).
      * ``"not_attempted"`` -- never attempted, because an earlier entry in
        the same ``all_or_nothing`` call aborted the batch (or pre-mutation
        validation rejected the whole call) before this entry's turn came.
    """

    index: int
    correlation_key: str | None
    status: str
    id: str | None = None
    outcome: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "correlation_key": self.correlation_key,
            "status": self.status,
            "id": self.id,
            "outcome": self.outcome,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BatchEntryResult":
        return cls(
            index=int(data.get("index", 0)),
            correlation_key=data.get("correlation_key"),
            status=str(data.get("status") or "error"),
            id=data.get("id"),
            outcome=data.get("outcome"),
            error_code=data.get("error_code"),
            error_message=data.get("error_message"),
            retryable=bool(data.get("retryable", False)),
        )


@dataclass
class BatchResult:
    """The overall outcome of one :func:`execute_batch` call.

    ``status`` is one of:
      * ``"ok"`` -- every entry succeeded.
      * ``"partial"`` -- (``best_effort`` only) some entries succeeded, some
        failed.
      * ``"failed"`` -- every entry failed (``best_effort``), or a mutation
        failed partway through and everything was rolled back
        (``all_or_nothing``).
      * ``"rejected"`` -- (``all_or_nothing`` only) pre-mutation structural
        validation found a problem; NOTHING was ever written.

    ``results`` is always in the SAME order as the input ``entries`` list
    (deterministic by input index, regardless of processing/completion
    order -- this engine processes sequentially in input order anyway, but
    the ordering guarantee is a documented contract, not an implementation
    accident).
    """

    status: str
    mode: str
    entry_kind: str
    project_id: str
    idempotency_key: str | None
    results: list[BatchEntryResult]
    idempotent_replay: bool = False

    @property
    def created_count(self) -> int:
        return sum(1 for r in self.results if r.status == "ok")

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if r.status != "ok")

    def ordered_ids(self) -> list[str | None]:
        """IDs in input order; ``None`` at the index of any non-``"ok"`` entry."""
        return [r.id if r.status == "ok" else None for r in self.results]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "mode": self.mode,
            "entry_kind": self.entry_kind,
            "project_id": self.project_id,
            "idempotency_key": self.idempotency_key,
            "idempotent_replay": self.idempotent_replay,
            "created_count": self.created_count,
            "error_count": self.error_count,
            "results": [r.to_dict() for r in self.results],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BatchResult":
        return cls(
            status=str(data.get("status") or "failed"),
            mode=str(data.get("mode") or "all_or_nothing"),
            entry_kind=str(data.get("entry_kind") or ""),
            project_id=str(data.get("project_id") or ""),
            idempotency_key=data.get("idempotency_key"),
            results=[BatchEntryResult.from_dict(r) for r in (data.get("results") or [])],
            idempotent_replay=True,
        )


# ---------------------------------------------------------------------------
# Entry context -- batch-level defaults threaded through every adapter call.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _EntryContext:
    actor: str | None = None
    session_id: str | None = None


def _correlation_key(raw: Any, index: int) -> str | None:
    if isinstance(raw, dict):
        ck = raw.get("correlation_key")
        if isinstance(ck, str) and ck.strip():
            return ck
    return None


def _pre_error_result(index: int, raw: Any, err: _EntryError) -> BatchEntryResult:
    return BatchEntryResult(
        index=index, correlation_key=_correlation_key(raw, index), status="error",
        error_code=err.code, error_message=err.message, retryable=err.retryable,
        outcome={"payload": err.payload} if err.payload else None,
    )


def _not_attempted_result(index: int, raw: Any) -> BatchEntryResult:
    return BatchEntryResult(
        index=index, correlation_key=_correlation_key(raw, index), status="not_attempted",
    )


# ---------------------------------------------------------------------------
# Entry-kind adapter: sprint_item (create via add_sprint_item, update via
# patch_sprint_item)
# ---------------------------------------------------------------------------

#: Kwargs forwarded to add_sprint_item (title/version handled separately --
#: title is required+positional-by-name, version is positional).
_SPRINT_ITEM_CREATE_FIELDS: tuple[str, ...] = (
    "version", "group", "human_id", "depends_on", "failure_mode",
    "milestone_type", "touches_resources", "force", "slug", "deferred_until",
    "track", "priority", "blocker_kind", "wave", "sprint_name", "prospect_bypass",
    "required_tool", "tool_requirements", "artifact_kind", "planned_output",
    "artifact_policy", "notes",
)

#: Kwargs forwarded to patch_sprint_item -- same names patch_sprint_item's own
#: signature uses, so an update entry maps 1:1 onto it with no translation.
_SPRINT_ITEM_PATCH_FIELDS: tuple[str, ...] = (
    "title", "version", "status", "feedback_thumb", "feedback_note", "notes",
    "human_id", "item_group", "touches_resources", "required_notes",
    "deferred_until", "track", "priority", "blocker_kind", "wave", "sprint_name",
    "prospect_bypass", "depends_on", "require_verification", "require_strict_evidence",
    "required_tool", "tool_requirements", "artifact_kind", "planned_output",
    "artifact_policy", "github_channel",
)


async def _validate_sprint_item_entry(
    db: Any, project_id: str, raw: Any, ctx: _EntryContext,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _EntryError(ERROR_VALIDATION, "entry must be an object")
    action = raw.get("action") or "create"
    if action not in ("create", "update"):
        raise _EntryError(
            ERROR_VALIDATION, f"action must be 'create' or 'update', got {action!r}"
        )
    if action == "create":
        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            raise _EntryError(ERROR_VALIDATION, "create entry requires a non-empty 'title'")
        kwargs = {k: raw[k] for k in _SPRINT_ITEM_CREATE_FIELDS if k in raw}
        return {"action": "create", "title": title, "kwargs": kwargs}
    # update
    item_id = raw.get("item_id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise _EntryError(ERROR_VALIDATION, "update entry requires a non-empty 'item_id'")
    snapshot = await db_module.get_sprint_item(db, item_id)
    if snapshot is None or snapshot.get("project_id") != project_id:
        raise _EntryError(
            ERROR_NOT_FOUND, f"sprint item not found in project: {item_id}"
        )
    changed_fields = [k for k in _SPRINT_ITEM_PATCH_FIELDS if k in raw]
    if not changed_fields:
        raise _EntryError(ERROR_VALIDATION, "update entry supplies no patchable fields")
    kwargs = {k: raw[k] for k in changed_fields}
    return {
        "action": "update", "item_id": item_id, "kwargs": kwargs,
        "changed_fields": changed_fields, "snapshot": snapshot,
    }


async def _apply_sprint_item_entry(
    db: Any, project_id: str, normalized: dict[str, Any], ctx: _EntryContext,
) -> tuple[str, dict[str, Any], tuple[Any, ...]]:
    if normalized["action"] == "create":
        kwargs = dict(normalized["kwargs"])
        version = kwargs.pop("version", "") or ""
        try:
            result = await db_module.add_sprint_item(
                db, project_id, version, normalized["title"], **kwargs
            )
        except ValueError as exc:
            raise _EntryError(ERROR_VALIDATION, str(exc)) from exc
        if isinstance(result, dict) and result.get("error") == "duplicate":
            raise _EntryError(
                ERROR_DUPLICATE, result.get("message") or "duplicate sprint item title",
                payload=result,
            )
        item_id = result["id"]
        return item_id, {"action": "create", "item": result}, ("create", item_id)

    item_id = normalized["item_id"]
    try:
        result = await db_module.patch_sprint_item(
            db, project_id, item_id, **normalized["kwargs"]
        )
    except ValueError as exc:
        raise _EntryError(ERROR_VALIDATION, str(exc)) from exc
    if result is None:
        raise _EntryError(
            ERROR_NOT_FOUND, f"sprint item not found or already changed: {item_id}"
        )
    comp_state = ("update", item_id, normalized["changed_fields"], normalized["snapshot"])
    return item_id, {"action": "update", "item": result}, comp_state


async def _compensate_sprint_item_entry(
    db: Any, project_id: str, comp_state: tuple[Any, ...], ctx: _EntryContext,
) -> None:
    try:
        kind = comp_state[0]
        if kind == "create":
            item_id = comp_state[1]
            await db.execute(
                "DELETE FROM sprint_items WHERE id = ? AND project_id = ?",
                (item_id, project_id),
            )
            await db.commit()
            db_module._invalidate_sprint_items_cache(project_id)
        elif kind == "update":
            _, item_id, changed_fields, snapshot = comp_state
            revert_kwargs: dict[str, Any] = {}
            for field_name in changed_fields:
                if field_name == "status":
                    old_status = snapshot.get("status")
                    # patch_sprint_item only accepts administrative-reset
                    # statuses (_PATCH_SPRINT_ITEM_ALLOWED_STATUSES); reverting
                    # THROUGH that same gate is best-effort -- if the old
                    # status isn't one of those, this one field cannot be
                    # reverted here (a documented, narrow limitation, not a
                    # silent failure: every other changed field still reverts).
                    if old_status not in db_module._PATCH_SPRINT_ITEM_ALLOWED_STATUSES:
                        continue
                    revert_kwargs["status"] = old_status
                else:
                    revert_kwargs[field_name] = snapshot.get(field_name)
            if revert_kwargs:
                await db_module.patch_sprint_item(db, project_id, item_id, **revert_kwargs)
    except Exception:  # noqa: BLE001 -- compensation must never mask the original abort
        pass


# ---------------------------------------------------------------------------
# Entry-kind adapter: sprint_item_pointer (add_sprint_item_pointer as-is)
# ---------------------------------------------------------------------------

async def _validate_pointer_entry(
    db: Any, project_id: str, raw: Any, ctx: _EntryContext,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _EntryError(ERROR_VALIDATION, "entry must be an object")
    sprint_item_id = raw.get("sprint_item_id")
    if not isinstance(sprint_item_id, str) or not sprint_item_id.strip():
        raise _EntryError(
            ERROR_VALIDATION, "pointer entry requires a non-empty 'sprint_item_id'"
        )
    source_type = raw.get("source_type")
    targets = raw.get("targets")
    label = raw.get("label")
    try:
        validate_pointer({"source_type": source_type, "targets": targets, "label": label})
    except ValueError as exc:
        raise _EntryError(ERROR_VALIDATION, str(exc)) from exc
    return {
        "sprint_item_id": sprint_item_id, "source_type": source_type,
        "targets": targets, "label": label,
    }


async def _apply_pointer_entry(
    db: Any, project_id: str, normalized: dict[str, Any], ctx: _EntryContext,
) -> tuple[str, dict[str, Any], tuple[Any, ...]]:
    try:
        result = await db_module.add_sprint_item_pointer(
            db, project_id, normalized["sprint_item_id"], normalized["source_type"],
            normalized["targets"], label=normalized.get("label"),
        )
    except ValueError as exc:
        raise _EntryError(ERROR_VALIDATION, str(exc)) from exc
    pointer_id = result["id"]
    return pointer_id, {"pointer": result}, ("pointer", pointer_id)


async def _compensate_pointer_entry(
    db: Any, project_id: str, comp_state: tuple[Any, ...], ctx: _EntryContext,
) -> None:
    try:
        _, pointer_id = comp_state
        await db_module.delete_sprint_item_pointer(db, pointer_id)
    except Exception:  # noqa: BLE001 -- compensation must never mask the original abort
        pass


# ---------------------------------------------------------------------------
# Entry-kind adapter: sprint_note (add_session_note as-is)
# ---------------------------------------------------------------------------

async def _validate_note_entry(
    db: Any, project_id: str, raw: Any, ctx: _EntryContext,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _EntryError(ERROR_VALIDATION, "entry must be an object")
    session_id = raw.get("session_id") or ctx.session_id
    if not isinstance(session_id, str) or not session_id.strip():
        raise _EntryError(
            ERROR_VALIDATION,
            "note entry requires a non-empty 'session_id' "
            "(or a batch-level session_id default)",
        )
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise _EntryError(ERROR_VALIDATION, "note entry requires a non-empty 'title'")
    body = raw.get("body")
    if not isinstance(body, str):
        raise _EntryError(ERROR_VALIDATION, "note entry requires a string 'body'")
    note_kind = raw.get("note_kind")
    return {"session_id": session_id, "title": title, "body": body, "note_kind": note_kind}


async def _apply_note_entry(
    db: Any, project_id: str, normalized: dict[str, Any], ctx: _EntryContext,
) -> tuple[str, dict[str, Any], tuple[Any, ...]]:
    result = await db_module.add_session_note(
        db, normalized["session_id"], normalized["title"], normalized["body"],
        note_kind=normalized.get("note_kind"),
    )
    note_id = result["id"]
    return note_id, {"note": result}, ("note", note_id)


async def _compensate_note_entry(
    db: Any, project_id: str, comp_state: tuple[Any, ...], ctx: _EntryContext,
) -> None:
    try:
        _, note_id = comp_state
        await db.execute("DELETE FROM session_notes WHERE id = ?", (note_id,))
        await db.commit()
    except Exception:  # noqa: BLE001 -- compensation must never mask the original abort
        pass


@dataclass(frozen=True)
class _EntryAdapter:
    validate: Callable[[Any, str, Any, _EntryContext], Awaitable[dict[str, Any]]]
    apply: Callable[
        [Any, str, dict[str, Any], _EntryContext],
        Awaitable[tuple[str, dict[str, Any], tuple[Any, ...]]],
    ]
    compensate: Callable[[Any, str, tuple[Any, ...], _EntryContext], Awaitable[None]]


_ADAPTERS: dict[str, _EntryAdapter] = {
    "sprint_item": _EntryAdapter(
        _validate_sprint_item_entry, _apply_sprint_item_entry, _compensate_sprint_item_entry,
    ),
    "sprint_item_pointer": _EntryAdapter(
        _validate_pointer_entry, _apply_pointer_entry, _compensate_pointer_entry,
    ),
    "sprint_note": _EntryAdapter(
        _validate_note_entry, _apply_note_entry, _compensate_note_entry,
    ),
}


# ---------------------------------------------------------------------------
# Idempotency: durable receipts stored in the existing action_audit_log table
# ---------------------------------------------------------------------------

def _receipt_id(
    *, tenant_id: str | None, project_id: str, entry_kind: str, idempotency_key: str,
) -> str:
    """Deterministic action_audit_log.id for this (scope, key) pair.

    Reusing the table's own TEXT PRIMARY KEY as the uniqueness guarantee --
    see the module docstring's "Idempotency, honestly" section.
    """
    raw = "|".join(["batchmgmt", tenant_id or "", project_id, entry_kind, idempotency_key])
    return "batchmgmt_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_unique_violation(exc: BaseException) -> bool:
    """Heuristic: does *exc* look like a PRIMARY KEY / UNIQUE collision?

    Matches sqlite3's ``UNIQUE constraint failed`` and psycopg3's
    ``UniqueViolation`` (``duplicate key value violates unique constraint``)
    -- identical heuristic to ``meridian.db.workspace._is_proposal_unique_violation``,
    duplicated here (rather than imported) because it is a two-line string
    check, not business logic, and importing a `_`-private helper across
    unrelated modules is worse than a one-line duplicate.
    """
    msg = str(exc).lower()
    return "unique" in msg or "duplicate key" in msg


async def _load_batch_receipt(
    db: Any, *, tenant_id: str | None, project_id: str, entry_kind: str,
    idempotency_key: str,
) -> BatchResult | None:
    rid = _receipt_id(
        tenant_id=tenant_id, project_id=project_id, entry_kind=entry_kind,
        idempotency_key=idempotency_key,
    )
    async with db.execute(
        "SELECT * FROM action_audit_log WHERE id = ?", (rid,)
    ) as cur:
        row = await cur.fetchone()
    row_dict = db_module._row_to_dict(row)
    if row_dict is None:
        return None
    try:
        detail = json.loads(row_dict.get("detail") or "{}")
    except (TypeError, ValueError):
        return None
    stored = detail.get("result") if isinstance(detail, dict) else None
    if not isinstance(stored, dict):
        return None
    result = BatchResult.from_dict(stored)
    result.idempotent_replay = True
    return result


async def _write_batch_receipt(
    db: Any, *, tenant_id: str | None, project_id: str, entry_kind: str,
    idempotency_key: str, actor: str | None, result: BatchResult,
) -> None:
    rid = _receipt_id(
        tenant_id=tenant_id, project_id=project_id, entry_kind=entry_kind,
        idempotency_key=idempotency_key,
    )
    detail = json.dumps(
        {"idempotency_key": idempotency_key, "result": result.to_dict()}, default=str,
    )
    try:
        await db.execute(
            "INSERT INTO action_audit_log "
            "(id, tenant_id, project_id, event_type, actor, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rid, tenant_id, project_id, BATCH_RECEIPT_EVENT_TYPE, actor, detail),
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001 -- classified below
        if _is_unique_violation(exc):
            # Lost a race against a concurrent call using the identical
            # (tenant_id, project_id, entry_kind, idempotency_key) tuple. The
            # winner's receipt is already durable; THIS call's own result
            # (computed and returned to its own caller above) is unaffected --
            # only a SUBSEQUENT retry with the same key will observe the
            # winner's stored result, matching add_workspace_proposal's
            # documented idempotency-key race behavior.
            return
        raise


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def execute_batch(
    db: Any,
    *,
    project_id: str,
    entry_kind: str,
    entries: list[dict[str, Any]],
    mode: str = "all_or_nothing",
    idempotency_key: str | None = None,
    tenant_id: str | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    max_entries: int = DEFAULT_MAX_BATCH_ENTRIES,
) -> BatchResult:
    """Process a homogeneous batch of management-write entries atomically
    (``mode="all_or_nothing"``) or independently (``mode="best_effort"``).

    Parameters
    ----------
    db:
        The shared aiosqlite/Postgres connection (same object every other
        ``meridian.db`` function takes).
    project_id:
        Scopes every entry exactly the way the underlying single-entry
        functions already do (``sprint_item``/``sprint_item_pointer`` entries
        are written with this ``project_id``; ``sprint_note`` entries are
        scoped by their own ``session_id`` instead, matching
        ``add_session_note``'s existing, unmodified contract -- this function
        does not add a project check ``add_session_note`` never had).
    entry_kind:
        One of :data:`BATCH_ENTRY_KINDS`. Every entry in ``entries`` is
        processed by the SAME adapter -- mixed kinds in one call are not
        supported.
    entries:
        Non-empty list of entry dicts (shape depends on ``entry_kind`` --
        see the module docstring). Each entry MAY carry a ``correlation_key``
        (any non-empty string) that is echoed back on its result; when
        omitted, callers can still identify a result by its ``index``
        (0-based position in ``entries``, always present).
    mode:
        One of :data:`BATCH_MODES`.
    idempotency_key:
        When given and truthy, a retried call with the identical
        ``(tenant_id, project_id, entry_kind, idempotency_key)`` returns the
        FIRST call's stored :class:`BatchResult` (``idempotent_replay=True``)
        instead of re-executing. See the module docstring's "Idempotency,
        honestly" section for exactly what guarantee this does and does not
        provide.
    tenant_id:
        Used only for idempotency-receipt scoping/attribution (mirrors
        ``action_audit_log.tenant_id``) -- NOT an authorization check; the
        caller (MCP dispatch layer) is responsible for verifying the tenant
        owns ``project_id`` before calling this function, same as for every
        other ``meridian.db`` write.
    actor:
        Attribution string stored on the idempotency receipt (when one is
        written); has no other effect.
    session_id:
        Batch-level default ``session_id`` used by ``sprint_note`` entries
        that omit their own ``session_id``.
    max_entries:
        Hard cap on ``len(entries)``; exceeding it raises
        :class:`BatchEngineError` before anything is attempted.

    Raises
    ------
    BatchEngineError:
        For a call-level contract violation (bad ``entry_kind``/``mode``,
        empty ``entries``, or exceeding ``max_entries``). Per-entry problems
        are NEVER raised -- they are reported as ``"error"``-status
        :class:`BatchEntryResult` entries inside the normally-returned
        :class:`BatchResult`.
    """
    if not project_id or not isinstance(project_id, str):
        raise BatchEngineError("project_id is required")
    if entry_kind not in BATCH_ENTRY_KINDS:
        raise BatchEngineError(
            f"entry_kind must be one of {BATCH_ENTRY_KINDS}, got {entry_kind!r}"
        )
    if mode not in BATCH_MODES:
        raise BatchEngineError(f"mode must be one of {BATCH_MODES}, got {mode!r}")
    if not isinstance(entries, list) or not entries:
        raise BatchEngineError("entries must be a non-empty list")
    if len(entries) > max_entries:
        raise BatchEngineError(
            f"entries has {len(entries)} items, exceeding max_entries={max_entries}; "
            "split into smaller batches"
        )

    if idempotency_key:
        replay = await _load_batch_receipt(
            db, tenant_id=tenant_id, project_id=project_id,
            entry_kind=entry_kind, idempotency_key=idempotency_key,
        )
        if replay is not None:
            return replay

    ctx = _EntryContext(actor=actor, session_id=session_id)
    adapter = _ADAPTERS[entry_kind]

    # ---- Phase 1: structural, pre-mutation validation (read-only). --------
    normalized: list[Any] = [None] * len(entries)
    entry_errors: dict[int, _EntryError] = {}
    for i, raw in enumerate(entries):
        try:
            normalized[i] = await adapter.validate(db, project_id, raw, ctx)
        except _EntryError as exc:
            entry_errors[i] = exc
        except ValueError as exc:
            entry_errors[i] = _EntryError(ERROR_VALIDATION, str(exc))

    if mode == "all_or_nothing" and entry_errors:
        # Nothing is mutated: every entry failed BEFORE any write was attempted.
        results = [
            _pre_error_result(i, entries[i], entry_errors[i]) if i in entry_errors
            else _not_attempted_result(i, entries[i])
            for i in range(len(entries))
        ]
        batch_result = BatchResult(
            status="rejected", mode=mode, entry_kind=entry_kind, project_id=project_id,
            idempotency_key=idempotency_key, results=results,
        )
        if idempotency_key:
            await _write_batch_receipt(
                db, tenant_id=tenant_id, project_id=project_id, entry_kind=entry_kind,
                idempotency_key=idempotency_key, actor=actor, result=batch_result,
            )
        return batch_result

    # ---- Phase 2: mutate, strictly in input order. -------------------------
    # Never spans an external call -- only the DB writes the adapters below
    # perform (see the module docstring).
    results: list[BatchEntryResult | None] = [None] * len(entries)
    compensations: list[tuple[Any, ...]] = []  # comp_state values, in apply order
    aborted = False
    for i, raw in enumerate(entries):
        ckey = _correlation_key(raw, i)
        if i in entry_errors:
            results[i] = _pre_error_result(i, raw, entry_errors[i])
            if mode == "all_or_nothing":
                aborted = True
                break
            continue
        try:
            entry_id, outcome, comp_state = await adapter.apply(
                db, project_id, normalized[i], ctx
            )
            results[i] = BatchEntryResult(
                index=i, correlation_key=ckey, status="ok", id=entry_id, outcome=outcome,
            )
            compensations.append(comp_state)
        except _EntryError as exc:
            results[i] = BatchEntryResult(
                index=i, correlation_key=ckey, status="error", error_code=exc.code,
                error_message=exc.message, retryable=exc.retryable,
                outcome={"payload": exc.payload} if exc.payload else None,
            )
            if mode == "all_or_nothing":
                aborted = True
                break
        except Exception as exc:  # noqa: BLE001 -- unexpected DB/driver error
            results[i] = BatchEntryResult(
                index=i, correlation_key=ckey, status="error", error_code=ERROR_INTERNAL,
                error_message=str(exc), retryable=True,
            )
            if mode == "all_or_nothing":
                aborted = True
                break

    if mode == "all_or_nothing" and aborted:
        # Undo every entry this call already wrote, most-recent first.
        for comp_state in reversed(compensations):
            await adapter.compensate(db, project_id, comp_state, ctx)
        for j in range(len(results)):
            if results[j] is None:
                results[j] = _not_attempted_result(j, entries[j])
            elif results[j].status == "ok":
                results[j] = replace(
                    results[j], status="rolled_back",
                    outcome={**(results[j].outcome or {}), "rolled_back": True},
                )
        overall_status = "failed"
    else:
        for j in range(len(results)):
            if results[j] is None:
                results[j] = _not_attempted_result(j, entries[j])
        ok_n = sum(1 for r in results if r.status == "ok")
        overall_status = "ok" if ok_n == len(results) else ("failed" if ok_n == 0 else "partial")

    batch_result = BatchResult(
        status=overall_status, mode=mode, entry_kind=entry_kind, project_id=project_id,
        idempotency_key=idempotency_key, results=results,  # type: ignore[arg-type]
    )
    if idempotency_key:
        await _write_batch_receipt(
            db, tenant_id=tenant_id, project_id=project_id, entry_kind=entry_kind,
            idempotency_key=idempotency_key, actor=actor, result=batch_result,
        )
    return batch_result


# ---------------------------------------------------------------------------
# 133bfff6 -- batch_mutate's core engine: a MIXED-kind sibling of
# execute_batch, restricted to sprint_item_pointer + sprint_item UPDATE.
# ---------------------------------------------------------------------------

async def execute_mixed_mutation_batch(
    db: Any,
    *,
    project_id: str,
    entries: list[dict[str, Any]],
    mode: str = "all_or_nothing",
    idempotency_key: str | None = None,
    tenant_id: str | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    max_entries: int = DEFAULT_MAX_BATCH_ENTRIES,
) -> BatchResult:
    """133bfff6 -- ``batch_mutate``'s core engine.

    A MIXED-kind sibling of :func:`execute_batch`: a single call may combine
    ``sprint_item_pointer`` entries (attach a pointer) and
    ``sprint_item_update`` entries (patch an EXISTING sprint item --
    creation is deliberately not offered here; only ``execute_batch``'s
    ``entry_kind="sprint_item"`` with ``action="create"`` creates) in ONE
    call, each entry selecting its own adapter via a required ``"kind"``
    field. That per-entry kind selection is the one structural difference
    from :func:`execute_batch`, which requires every entry in a call to
    share the SAME ``entry_kind`` (see that function's own "homogeneous"
    framing). Everything else -- validate-before-mutate, all_or_nothing
    compensation, best_effort partial commit, deterministic input-order
    results, idempotency-replay receipts -- is the identical contract, and
    this function reuses :func:`execute_batch`'s own adapters
    (``_ADAPTERS["sprint_item_pointer"]`` -> :func:`_apply_pointer_entry` /
    :func:`_validate_pointer_entry` / :func:`_compensate_pointer_entry`, and
    ``_ADAPTERS["sprint_item"]`` -> :func:`_apply_sprint_item_entry` /
    :func:`_validate_sprint_item_entry` / :func:`_compensate_sprint_item_entry`)
    AS-IS -- no duplicated validation/mutation/compensation logic, per this
    item's acceptance criteria.

    A ``sprint_item_update`` entry's ``action`` is force-set to ``"update"``
    before validation (mirroring ``meridian.batch_ops``'s
    ``_normalize_entries_for_operation`` forced-action pattern for its
    ``item_updates`` operation, including its default: an entry that omits
    ``action`` entirely -- the common case, callers pass ``item_id`` + fields,
    never ``action`` -- defaults to ``"update"`` here, NOT
    ``_validate_sprint_item_entry``'s own bare default of ``"create"``,
    which would otherwise misfire for every caller who (correctly) omits
    ``action`` on an update entry). An entry that explicitly names
    ``action="create"`` is rejected with a clear, actionable message rather
    than silently creating a sprint item through what is documented as an
    update-only surface.

    Project/tenant isolation: an entry MAY optionally carry its own
    ``project_id`` field (e.g. a caller that copy-pasted an entry from
    another context) -- if present, it MUST match this call's own
    *project_id* exactly, or the entry is rejected before any mutation is
    attempted, never silently ignored. This is on top of (not instead of)
    the isolation the underlying adapters already provide:
    ``_apply_pointer_entry``/``_apply_sprint_item_entry`` both write/patch
    scoped to *project_id*, and ``_validate_sprint_item_entry``'s own
    ``snapshot.get("project_id") != project_id`` check already 404s an
    ``item_id`` that resolves to a DIFFERENT project -- so a cross-project
    ``item_id`` guess is already impossible even without this extra guard;
    this guard's job is rejecting an explicit conflicting ``project_id``
    field outright.

    See :func:`execute_batch` for the full parameter/return/raise contract
    (identical here except ``entry_kind`` does not exist as a parameter --
    it is chosen per-entry via ``"kind"``) -- this docstring only calls out
    what is DIFFERENT.
    """
    if not project_id or not isinstance(project_id, str):
        raise BatchEngineError("project_id is required")
    if mode not in BATCH_MODES:
        raise BatchEngineError(f"mode must be one of {BATCH_MODES}, got {mode!r}")
    if not isinstance(entries, list) or not entries:
        raise BatchEngineError("entries must be a non-empty list")
    if len(entries) > max_entries:
        raise BatchEngineError(
            f"entries has {len(entries)} items, exceeding max_entries={max_entries}; "
            "split into smaller batches"
        )

    if idempotency_key:
        replay = await _load_batch_receipt(
            db, tenant_id=tenant_id, project_id=project_id,
            entry_kind=MIXED_MUTATION_RECEIPT_KIND, idempotency_key=idempotency_key,
        )
        if replay is not None:
            return replay

    ctx = _EntryContext(actor=actor, session_id=session_id)

    # ---- Phase 0: resolve each entry's adapter from its own 'kind', force
    # the update-only action, and enforce project isolation. Pure structural
    # checks -- no DB access yet. --------------------------------------------
    resolved_entries: list[Any] = [None] * len(entries)
    resolved_adapter: list[_EntryAdapter | None] = [None] * len(entries)
    entry_errors: dict[int, _EntryError] = {}
    for i, raw in enumerate(entries):
        if not isinstance(raw, dict):
            entry_errors[i] = _EntryError(ERROR_VALIDATION, "entry must be an object")
            continue
        kind = raw.get("kind")
        if kind not in MIXED_MUTATION_ENTRY_KINDS:
            entry_errors[i] = _EntryError(
                ERROR_VALIDATION,
                f"entry 'kind' must be one of {MIXED_MUTATION_ENTRY_KINDS}, got {kind!r}",
            )
            continue
        entry_pid = raw.get("project_id")
        if entry_pid is not None and str(entry_pid) != str(project_id):
            entry_errors[i] = _EntryError(
                ERROR_VALIDATION,
                f"entry project_id {entry_pid!r} does not match this batch's own "
                f"project_id {project_id!r} -- a mutation entry cannot target a "
                "different project",
            )
            continue
        if kind == "sprint_item_pointer":
            resolved_adapter[i] = _ADAPTERS["sprint_item_pointer"]
            resolved_entries[i] = raw
        else:  # "sprint_item_update"
            action = raw.get("action") or "update"
            if action != "update":
                entry_errors[i] = _EntryError(
                    ERROR_VALIDATION,
                    "kind='sprint_item_update' only supports action='update' "
                    "(sprint-item creation is not exposed through batch_mutate) "
                    f"-- got action={action!r}",
                )
                continue
            entry = dict(raw)
            entry["action"] = "update"
            resolved_adapter[i] = _ADAPTERS["sprint_item"]
            resolved_entries[i] = entry

    # ---- Phase 1: structural, pre-mutation validation (read-only). --------
    normalized: list[Any] = [None] * len(entries)
    for i in range(len(entries)):
        if i in entry_errors:
            continue
        adapter = resolved_adapter[i]
        assert adapter is not None
        try:
            normalized[i] = await adapter.validate(db, project_id, resolved_entries[i], ctx)
        except _EntryError as exc:
            entry_errors[i] = exc
        except ValueError as exc:
            entry_errors[i] = _EntryError(ERROR_VALIDATION, str(exc))

    if mode == "all_or_nothing" and entry_errors:
        results = [
            _pre_error_result(i, entries[i], entry_errors[i]) if i in entry_errors
            else _not_attempted_result(i, entries[i])
            for i in range(len(entries))
        ]
        batch_result = BatchResult(
            status="rejected", mode=mode, entry_kind=MIXED_MUTATION_RECEIPT_KIND,
            project_id=project_id, idempotency_key=idempotency_key, results=results,
        )
        if idempotency_key:
            await _write_batch_receipt(
                db, tenant_id=tenant_id, project_id=project_id,
                entry_kind=MIXED_MUTATION_RECEIPT_KIND,
                idempotency_key=idempotency_key, actor=actor, result=batch_result,
            )
        return batch_result

    # ---- Phase 2: mutate, strictly in input order, each entry through its
    # own resolved adapter. Never spans an external call (see execute_batch's
    # module docstring -- identical rule here). -----------------------------
    results: list[BatchEntryResult | None] = [None] * len(entries)
    compensations: list[tuple[_EntryAdapter, tuple[Any, ...]]] = []
    aborted = False
    for i, raw in enumerate(entries):
        ckey = _correlation_key(raw, i)
        if i in entry_errors:
            results[i] = _pre_error_result(i, raw, entry_errors[i])
            if mode == "all_or_nothing":
                aborted = True
                break
            continue
        adapter = resolved_adapter[i]
        assert adapter is not None
        try:
            entry_id, outcome, comp_state = await adapter.apply(
                db, project_id, normalized[i], ctx
            )
            results[i] = BatchEntryResult(
                index=i, correlation_key=ckey, status="ok", id=entry_id, outcome=outcome,
            )
            compensations.append((adapter, comp_state))
        except _EntryError as exc:
            results[i] = BatchEntryResult(
                index=i, correlation_key=ckey, status="error", error_code=exc.code,
                error_message=exc.message, retryable=exc.retryable,
                outcome={"payload": exc.payload} if exc.payload else None,
            )
            if mode == "all_or_nothing":
                aborted = True
                break
        except Exception as exc:  # noqa: BLE001 -- unexpected DB/driver error
            results[i] = BatchEntryResult(
                index=i, correlation_key=ckey, status="error", error_code=ERROR_INTERNAL,
                error_message=str(exc), retryable=True,
            )
            if mode == "all_or_nothing":
                aborted = True
                break

    if mode == "all_or_nothing" and aborted:
        # Undo every entry this call already wrote, most-recent first, each
        # through the SAME adapter it was applied with.
        for adapter, comp_state in reversed(compensations):
            await adapter.compensate(db, project_id, comp_state, ctx)
        for j in range(len(results)):
            if results[j] is None:
                results[j] = _not_attempted_result(j, entries[j])
            elif results[j].status == "ok":
                results[j] = replace(
                    results[j], status="rolled_back",
                    outcome={**(results[j].outcome or {}), "rolled_back": True},
                )
        overall_status = "failed"
    else:
        for j in range(len(results)):
            if results[j] is None:
                results[j] = _not_attempted_result(j, entries[j])
        ok_n = sum(1 for r in results if r.status == "ok")
        overall_status = "ok" if ok_n == len(results) else ("failed" if ok_n == 0 else "partial")

    batch_result = BatchResult(
        status=overall_status, mode=mode, entry_kind=MIXED_MUTATION_RECEIPT_KIND,
        project_id=project_id, idempotency_key=idempotency_key, results=results,  # type: ignore[arg-type]
    )
    if idempotency_key:
        await _write_batch_receipt(
            db, tenant_id=tenant_id, project_id=project_id,
            entry_kind=MIXED_MUTATION_RECEIPT_KIND,
            idempotency_key=idempotency_key, actor=actor, result=batch_result,
        )
    return batch_result
