"""Project-scope enforcement for MCP tool calls (pinned decision 6fe5210c, wave 2).

Decision 6fe5210c ("Option A"): a project-scoped workspace member must be
refused on any project outside their scope, even by direct id. The MCP
pre-dispatch gate in ``_handle_mcp_request`` only compares ``project_id`` /
``project_name``, so every tool that reaches a project through some OTHER
argument slips past it: an object id (``request_id``, ``proposal_id``,
``wave_run_id``, ``session_id`` ...), a differently-named project argument
(``source_project_id``, ``parent_project_id`` ...), a profile ``scope_id``, or
no project argument at all (a listing that spans every project when
``project_id`` is omitted).

This module closes that class with ONE entry point,
:func:`enforce_scoped_call`, driven by explicit tables:

* :data:`OBJECT_ARGS` -- per tool, which argument carries which kind of object
  id. Each kind has a resolver that loads the object and returns the project it
  belongs to (``_RESOLVERS``); that project must be in scope.
* :data:`REQUIRE_PROJECT` -- tools that list across every project when
  ``project_id`` is omitted; a scoped caller must name an in-scope project.
* :data:`DENY` -- tools whose answer is cross-project by construction (keyed by
  ``file_path`` only) and cannot be narrowed to a project.
* :data:`PROFILE_SCOPE_ARGS` -- profile-layer / capability-profile tools keyed
  by a ``(scope_type, scope_id)`` pair; for the project-owned scope types the
  ``scope_id`` is a project (or an object of one) and is checked the same way.
* :data:`BATCH_ENVELOPES` -- ``execute_batch`` / ``batch_mutate`` /
  ``batch_read`` carry per-entry ``session_id`` / ``scope_type`` + ``scope_id``
  that the engines honour without re-checking scope, so each entry is walked.
* generic rules that need no table: every ``project_id`` / ``*_project_id``
  argument must be in scope, every ``*_project_name`` argument must resolve to
  an in-scope project, and every session-pointer argument (``session_id``,
  ``to_session_id`` ...) must name a session of an in-scope project.
* :data:`DOC_OBJECT_ARGS` -- document-store objects (a figure or table id) that
  the handler looks up WITHOUT binding to the document; checked against the
  caller's own stored document (pass 2, F-M3).
* :data:`REDACT_HOLDER_TOOLS` -- results that name the holder of a file lock,
  symbol / docx-region claim or resource lease. ``file_locks`` and friends are
  keyed by file path only, so a foreign project's session can be the holder;
  :func:`filter_scoped_result` blanks every holder identity that belongs to an
  out-of-scope session while still reporting the conflict (pass 2, F-M2). The
  merged element lists of a docx conflict keep the ids in-scope sessions hold
  (pass 3, F-C2).

Every refusal raises ``ValueError("project is outside your access scope")``,
the same opaque message the pre-dispatch gate uses, so a caller cannot tell
which layer (or which argument) refused it. ``prompts/get`` is a project-reading
method too (the ``executor-goal`` prompt renders a project's pending sprint
items), so :func:`enforce_scoped_prompt` applies the generic project rules to its
arguments (pass 2, F-M1).

Open product questions (recorded, deliberately NOT changed here)
----------------------------------------------------------------
The rule of decision 6fe5210c is "a project-scoped member is refused on any
PROJECT outside their scope". The tools below act on TENANT-level state, which
belongs to no project, so this module leaves them alone. Whether a project-scoped
member should reach them at all is a product decision for the owner:

* tenant-level profile scopes: ``workspace`` / ``user`` / ``hosted_default``
  profile layers and capability profiles (``save_profile_layer``,
  ``reset_profile_layer``, ``clone_profile_layer``, ``set_capability_profile``,
  ``clear_capability_profile``, ``activate_profile_layer``) and the read-only
  ``get_effective_profile`` / ``get_effective_capability_profile`` scope ids;
* ``update_workspace_settings`` and ``get_workspace_settings``, and
  ``request_manual_issue_screening_toggle`` (an independent verifier saw it return
  ``applied: true`` and change the workspace settings for a scoped caller);
* ``pin_workspace_decision`` / ``get_workspace_decisions``, workspace notes
  (``add_workspace_note``, ``get_workspace_notes``, ``move_workspace_note_to_project``
  for a workspace note), workspace proposals (``add_workspace_proposal``), workspace
  sprint items (``add_workspace_sprint_item`` and friends), ``save_blog_post``, and the
  WORKSPACE section ``get_context_block`` renders from the notes and decisions above;
* ``create_project`` without a parent still creates a project that is not on the
  caller's scope list;
* ``get_server_logs`` / ``search_server_logs`` / ``get_connection_log`` /
  ``get_server_log_checkpoint`` read server-wide logs;
* the tunnel-forward branch of ``_handle_mcp_request`` (it runs before this
  guard and hands the call to a tunnel plugin tool) and the ``batch_read``
  ``tunnel_research`` adapter, which reads from the tenant's own workstation;
* ``find_orphaned_docx_staged_files``, ``check_embedded_staleness``,
  ``audit_figure_table_provenance`` and ``get_latex_structure`` read server-side file
  paths named by the caller (host level, not project data; the verifier read a file
  outside the data directory through ``get_latex_structure``, which is tenant-wide
  rather than project-scoped and worth a look on hosted servers).

Invariants worth keeping when you edit this file:

* ``scoped_project_ids is None`` (owner, self-hosted, demo, stdio) returns
  immediately -- no DB call, no behaviour change.
* An EMPTY list is still a scoped caller (refuses everything project-owned);
  never test the list for truthiness.
* An object that does not exist is refused too (unless the entry sets
  ``missing_ok``), so an out-of-scope id and an unknown id look alike.
* Everything fails closed: a malformed argument or a resolver error raises
  instead of being skipped.
* A proposal with no project (workspace-global) belongs to no project, so it
  is out of scope for a scoped caller.

* A result is only ever NARROWED (a holder blanked, a row dropped), never rewritten
  into something else, and the conflict a caller is entitled to learn about
  ("this path is locked, until T") survives the redaction.

A "bound" exemption in the pass-2 completeness test means the HANDLER binds the id to
the call's own project -- on EVERY code path, not only the one a probe happened to
take. Pass 3 found ``update_sprint_item`` exempted as bound although a call with no
editable field took an unbound early return (and its in_progress pre-check read the
row unbound); the probes now send every bound pair in its minimal shape and with each
optional argument alone, against a foreign object in each lifecycle state, and prove
the probe reached the id by running the same call against an object of the caller's
own project.

``tests/test_scope_guard_mcp.py`` enumerates these tables: every tool and
argument named here must exist in the real tool schemas, and every listed tool is
refused out of scope and still works in scope.
``tests/test_scope_guard_mcp_pass2.py`` proves the other direction: EVERY
id-bearing argument of EVERY tool (with or without a project_id of its own) is
guarded here, covered by a generic rule, or exempted with a reason and, for the
ones a handler already binds to the project, a proof; and every tool the
dispatcher routes but tools/list does not advertise has been reviewed. So the
tables cannot silently rot.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, NoReturn

from .. import db as db_module

#: The one refusal message. Identical to the pre-dispatch gate in
#: ``_handle_mcp_request`` and to ``_resolve_project_reference`` so no layer is
#: distinguishable from another.
OUT_OF_SCOPE = "project is outside your access scope"

#: Resolver result for "no such object" (distinct from "an object that belongs
#: to no project", which resolves to ``""``).
_MISSING: Any = object()


# ---------------------------------------------------------------------------
# Resolvers: object id -> owning project id ("" when the object has none)
# ---------------------------------------------------------------------------

def _owner(row: "dict[str, Any] | None") -> Any:
    """Project id of a loaded row, ``_MISSING`` when the row does not exist."""
    if row is None:
        return _MISSING
    return str(row.get("project_id") or "")


async def _note_project(db: Any, object_id: str) -> Any:
    return _owner(await db_module.get_project_note(db, object_id))


async def _decision_project(db: Any, object_id: str) -> Any:
    return _owner(await db_module.get_pinned_decision(db, object_id))


async def _hitl_project(db: Any, object_id: str) -> Any:
    return _owner(await db_module.get_hitl_request(db, object_id))


async def _wave_run_project(db: Any, object_id: str) -> Any:
    return _owner(await db_module.get_wave_run(db, object_id))


async def _worktree_project(db: Any, object_id: str) -> Any:
    return _owner(await db_module.get_worktree(db, object_id))


async def _sprint_item_project(db: Any, object_id: str) -> Any:
    return _owner(await db_module.get_sprint_item(db, object_id))


async def _session_project(db: Any, object_id: str) -> Any:
    # No db getter returns a bare session row; this is the same single-column
    # lookup routes/sessions.py uses for the HTTP twin.
    async with db.execute(
        "SELECT project_id FROM sessions WHERE id = ?", (object_id,)
    ) as cur:
        row = await cur.fetchone()
    return _owner(db_module._row_to_dict(row) if row is not None else None)


async def _proposal_project(db: Any, object_id: str) -> Any:
    # workspace_proposals.project_id is NULL for a workspace-global proposal:
    # that resolves to "" (a real row owned by no project), never in scope.
    async with db.execute(
        "SELECT project_id FROM workspace_proposals WHERE id = ?", (object_id,)
    ) as cur:
        row = await cur.fetchone()
    return _owner(db_module._row_to_dict(row) if row is not None else None)


async def _task_project(db: Any, object_id: str) -> Any:
    # task_log rows carry their project; a linked task is read back as completion
    # evidence and joined into session summaries, so a foreign one is refused.
    return _owner(await db_module.get_task(db, object_id))


async def _handoff_project(db: Any, object_id: str) -> Any:
    # record_handoff_correction(regenerate=true) INVALIDATES the source handoff,
    # so the handoff must belong to an in-scope project.
    async with db.execute(
        "SELECT project_id FROM handoffs WHERE id = ?", (object_id,)
    ) as cur:
        row = await cur.fetchone()
    return _owner(db_module._row_to_dict(row) if row is not None else None)


_RESOLVERS: "dict[str, Callable[[Any, str], Awaitable[Any]]]" = {
    "note": _note_project,
    "decision": _decision_project,
    "hitl": _hitl_project,
    "wave_run": _wave_run_project,
    "worktree": _worktree_project,
    "sprint_item": _sprint_item_project,
    "session": _session_project,
    "proposal": _proposal_project,
    "task": _task_project,
    "handoff": _handoff_project,
}


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ObjectArg:
    """One argument of one tool that carries the id of a project-owned object.

    ``kind`` selects the resolver in ``_RESOLVERS``. ``many`` marks an argument
    that holds several ids (a list, or a comma-separated string).
    ``missing_ok`` lets an id that resolves to no row through to the handler,
    for the tools whose own contract answers an unknown id normally
    (``delete_note`` -> ``deleted: false``; the ``idle_until_*`` barriers treat
    a missing session as done). Every other entry refuses an unknown id so an
    out-of-scope id and a made-up one are indistinguishable.
    """

    arg: str
    kind: str
    many: bool = False
    missing_ok: bool = False


def _sess(arg: str = "session_id", **kw: Any) -> ObjectArg:
    return ObjectArg(arg, "session", **kw)


#: tool -> the object-id arguments that must belong to an in-scope project.
#: Argument names are checked against the real tool schemas by the tests.
OBJECT_ARGS: "dict[str, tuple[ObjectArg, ...]]" = {
    # --- wave 1 (RT-TI-003/004): notes and decisions by their own id --------
    "delete_note": (ObjectArg("note_id", "note", missing_ok=True),),
    "update_decision": (ObjectArg("decision_id", "decision"),),
    "archive_decision": (ObjectArg("decision_id", "decision"),),
    # --- decisions referenced from other tools ------------------------------
    "validate_assumption": (ObjectArg("decision_id", "decision"),),
    "save_finding": (ObjectArg("decision_id", "decision"),),
    "capture_research_finding": (ObjectArg("related_decision_id", "decision"),),
    # --- HITL requests by request_id ----------------------------------------
    "get_hitl_request": (ObjectArg("request_id", "hitl"),),
    "answer_hitl": (ObjectArg("request_id", "hitl"),),
    "dismiss_hitl": (ObjectArg("request_id", "hitl"),),
    # --- session-keyed tools (no project argument in their schema) ----------
    "get_session_log": (_sess(),),
    "get_session_activity": (_sess(),),
    "get_sprint_notes": (_sess(),),
    "add_sprint_note": (_sess(),),
    "receive_messages": (_sess(),),
    "claim_file": (_sess(),),
    "release_file": (_sess(),),
    "claim_docx_region": (_sess(),),
    "release_docx_region_claims": (_sess(),),
    "acquire_docx_document_lease": (_sess(),),
    "release_docx_document_lease": (_sess(),),
    "get_graph_diff": (_sess("session_a"), _sess("session_b")),
    # The recipient must be a real session of an in-scope project; send_message
    # additionally binds it to the named project (see _check_special).
    "send_message": (_sess("to_session_id"),),
    # Barriers: an unknown session is a normal "done" answer, not an error.
    "idle_until_session_done": (_sess("watching_session_id", missing_ok=True),),
    "idle_until_all_done": (_sess("session_ids", many=True, missing_ok=True),),
    # --- wave runs by wave_run_id -------------------------------------------
    "finalize_wave_run": (ObjectArg("wave_run_id", "wave_run"),),
    "resume_wave": (ObjectArg("wave_run_id", "wave_run"),),
    # --- proposals by proposal id (NULL-project proposals are out of scope) -
    "advance_proposal_status": (ObjectArg("proposal_id", "proposal"),),
    "create_proposal_successor": (ObjectArg("proposal_id", "proposal"),),
    "get_proposal_lineage": (ObjectArg("proposal_id", "proposal"),),
    "link_proposal_lineage": (
        ObjectArg("from_proposal_id", "proposal"),
        ObjectArg("to_proposal_id", "proposal"),
    ),
    "compare_proposal_versions": (
        ObjectArg("from_proposal_id", "proposal"),
        ObjectArg("to_proposal_id", "proposal"),
    ),
    "promote_proposal": (ObjectArg("proposal_id", "proposal"),),
    "preview_proposal_promotion": (ObjectArg("proposal_id", "proposal"),),
    "commit_proposal_promotion": (ObjectArg("proposal_id", "proposal"),),
    # --- worktrees by id -----------------------------------------------------
    "set_active_repo": (ObjectArg("worktree_id", "worktree"),),
    # --- second object id next to the tool's OWN project_id (pass 2, F-M3) ---
    # Each tool in this block was probed with a foreign id under an in-scope
    # project_id. The ones the handler already binds to the project are NOT here:
    # they are exempted, with a proof, in tests/test_scope_guard_mcp_pass2.py.
    #
    # start_wave_run wrote a wave-run child row pointing at a foreign item.
    "start_wave_run": (ObjectArg("item_ids", "sprint_item", many=True),),
    # Stored on the caller's own row and never dereferenced today; refused so it
    # cannot become a cross-project link (an own item is the only valid value).
    "start_remote_task": (ObjectArg("sprint_item_id", "sprint_item"),),
    # regenerate=true INVALIDATES the SOURCE handoff and rewrote a foreign one.
    "record_handoff_correction": (ObjectArg("source_handoff_id", "handoff"),),
    # get_task() is not project-bound: a foreign task id counted as completion
    # evidence and linked an own item into the foreign session's summary.
    "complete_sprint_item": (ObjectArg("task_id", "task"),),
    # Stored only (get_findings never joins it); same rule as above.
    "store_finding": (ObjectArg("task_id", "task"),),
    # Stored, never validated: refuse a foreign worktree, leave an unknown id to
    # the handler (it accepts any label).
    "start_experiment_run": (ObjectArg("worktree_id", "worktree", missing_ok=True),),
    # --- sprint items whose handler reads the item BEFORE it binds it (third pass) --
    # Both tools take the caller's own project_id plus an item_id, and the db
    # functions behind them are bound to the project -- but the handler first reads
    # the row by id alone: update_sprint_item's in_progress pre-check answered
    # IN_PROGRESS with a FOREIGN item's claimed_at, and claim_sprint_item's
    # installer-script pre-check answered PROTECTED for a foreign item. (The no-op
    # patch_sprint_item early return that handed back the whole foreign row is
    # fixed at its root in meridian/db/sprint_items.py.) The handlers live outside
    # this module's remit, so the id is checked here, before they run; a foreign and
    # an unknown id are refused alike.
    "update_sprint_item": (ObjectArg("item_id", "sprint_item"),),
    "claim_sprint_item": (ObjectArg("item_id", "sprint_item"),),
    # --- dispatchable but NOT in tools/list (so the schema scan cannot see them)
    # proposal_to_handoff loaded the proposal by tenant only and wrote update
    # rows and pointers against it; claim_parallel_batch's item_sessions values
    # are handled in _Checker.check_special.
    "proposal_to_handoff": (ObjectArg("proposal_id", "proposal"),),
}

#: Tools that list across EVERY project when ``project_id`` is omitted. A
#: scoped caller must name one (in-scope, by the generic project rule).
REQUIRE_PROJECT: "frozenset[str]" = frozenset({
    "list_hitl_requests",
    "get_file_claims",
    "list_worktrees_pending_cleanup",
    "get_workspace_proposals",
})

#: Tools keyed by ``file_path`` alone whose result spans every project and
#: cannot be narrowed to one. Refused outright for a scoped caller.
DENY: "frozenset[str]" = frozenset({
    "get_symbol_claims",
    "get_symbol_hotspots",
})

#: tool -> ``(scope_type argument, scope_id argument)`` pairs. For the
#: project-owned scope types (see :func:`_profile_scope_owner`) the id must be in
#: scope. ``workspace`` / ``user`` / ``hosted_default`` layers are tenant-level
#: policy, not project data, and are deliberately left unchanged (an open
#: product question: should a project-scoped member write tenant-wide policy?).
PROFILE_SCOPE_ARGS: "dict[str, tuple[tuple[str, str], ...]]" = {
    "get_profile_layer": (("scope_type", "scope_id"),),
    "save_profile_layer": (("scope_type", "scope_id"),),
    "reset_profile_layer": (("scope_type", "scope_id"),),
    "set_capability_profile": (("scope_type", "scope_id"),),
    "clear_capability_profile": (("scope_type", "scope_id"),),
    "clone_profile_layer": (
        ("source_scope_type", "source_scope_id"),
        ("target_scope_type", "target_scope_id"),
    ),
}

#: Tools whose RESULT is a cross-project listing that cannot be narrowed by an
#: argument; :func:`filter_scoped_result` drops the rows outside the scope.
FILTER_RESULT_TOOLS: "frozenset[str]" = frozenset({"list_profile_layers"})

#: Tools the dispatcher routes by name that are NOT advertised in tools/list.
#: ``tools/call`` does not check the advertised list, so they are reachable, but
#: the schema scan in the tests cannot see their arguments; they are named here
#: so a table entry for one is not mistaken for a stale name.
UNLISTED_TOOLS: "frozenset[str]" = frozenset({
    "proposal_to_handoff",
    "claim_parallel_batch",
})

#: tool -> ``(document argument, object-id argument, DocStructureStore method that
#: lists the document's objects)``. The handler resolves ``doc`` against the
#: caller's own ``project_id`` and then calls a store primitive keyed by the bare
#: figure/table id (``set_figure_caption_link`` / ``set_table_caption_link``),
#: which is bound to neither the document nor the project -- so an own document
#: plus a foreign figure id rewrote and returned the foreign row. The object must
#: be one of the named document's own.
DOC_OBJECT_ARGS: "dict[str, tuple[str, str, str]]" = {
    "link_figure_caption": ("doc", "figure_id", "get_figures"),
    "link_table_caption": ("doc", "table_id", "get_tables"),
}

#: Tools whose RESULT can name the holder of a file lock, a symbol / docx-region
#: claim or a resource lease. Those tables are keyed by path (not project), so
#: the holder may be a session of a project outside the caller's scope;
#: :func:`filter_scoped_result` blanks that identity and keeps the conflict.
REDACT_HOLDER_TOOLS: "frozenset[str]" = frozenset({
    "claim_file",
    "get_file_claims",
    "claim_docx_region",
    "get_docx_region_claims",
    "acquire_docx_document_lease",
    "get_docx_document_lease",
    # They run the same claim primitives (or read the same lock tables) and
    # returned the holder in a resource-conflict row / message (probed).
    # transfer_sprint_item_claim is deliberately NOT here: it re-locks the item's
    # resources that the caller's own session already holds, and a path-keyed lock
    # cannot have a second (foreign) holder at the same time.
    "claim_sprint_item",
    "claim_parallel_batch",
    "get_parallelizable_groups",
    # The docx write gate (check_docx_region_write_conflict) answers with the
    # blocking session as ``holder`` (an id) and spells it out in ``message``.
    "update_paragraph",
})

#: Batch tools -> the argument holding their per-entry list. The engines honour
#: an entry's own ``session_id`` / ``scope_type`` + ``scope_id`` (``batch_read``
#: requests nest them under ``args``) without re-checking scope.
BATCH_ENVELOPES: "dict[str, str]" = {
    "execute_batch": "entries",
    "batch_mutate": "entries",
    "batch_read": "requests",
}

#: Session-pointer arguments checked on EVERY tool (an unknown session is left
#: to the handler: many tools auto-register a caller-minted session id).
_SESSION_ARG_KEYS: "tuple[str, ...]" = (
    "session_id", "from_session_id", "to_session_id", "watching_session_id",
    "session_a", "session_b", "verifier_session_id",
)
_SESSION_LIST_KEYS: "tuple[str, ...]" = ("session_ids",)

#: Profile scope types whose ``scope_id`` IS a project id.
_PROJECT_SCOPE_TYPES = frozenset({"project"})


def guarded_tool_names() -> "frozenset[str]":
    """Every tool name any table in this module refers to (for the rot test)."""
    return frozenset(
        set(OBJECT_ARGS) | set(REQUIRE_PROJECT) | set(DENY)
        | set(PROFILE_SCOPE_ARGS) | set(FILTER_RESULT_TOOLS) | set(BATCH_ENVELOPES)
        | set(DOC_OBJECT_ARGS) | set(REDACT_HOLDER_TOOLS) | set(UNLISTED_TOOLS)
    )


def schema_argument_refs() -> "list[tuple[str, str]]":
    """``(tool, argument)`` pairs the tables expect to exist in the tool schemas.

    Tools in :data:`UNLISTED_TOOLS` have no advertised schema, so their pairs are
    left out (the tests pin that they really are unlisted instead).
    """
    refs: "list[tuple[str, str]]" = []
    for tool, args in OBJECT_ARGS.items():
        refs.extend((tool, a.arg) for a in args)
    for tool, pairs in PROFILE_SCOPE_ARGS.items():
        for type_arg, id_arg in pairs:
            refs.extend(((tool, type_arg), (tool, id_arg)))
    for tool, arg in BATCH_ENVELOPES.items():
        refs.append((tool, arg))
    for tool, (doc_arg, id_arg, _method) in DOC_OBJECT_ARGS.items():
        refs.extend(((tool, doc_arg), (tool, id_arg), (tool, "project_id")))
    refs.extend((tool, "project_id") for tool in REQUIRE_PROJECT)
    refs.append(("promote_proposal", "allow_project_transfer"))
    refs.append(("send_message", "project_id"))
    return [(t, a) for t, a in refs if t not in UNLISTED_TOOLS]


def covered_argument_pairs() -> "frozenset[tuple[str, str]]":
    """``(tool, argument)`` pairs the guard actually checks beyond the generic
    project / session rules: every object-table argument, profile scope pair,
    batch envelope and document-object argument. The tests compare this against
    the id-bearing arguments in the real schemas."""
    pairs: "set[tuple[str, str]]" = set()
    for tool, args in OBJECT_ARGS.items():
        pairs.update((tool, a.arg) for a in args)
    for tool, scope_pairs in PROFILE_SCOPE_ARGS.items():
        for type_arg, id_arg in scope_pairs:
            pairs.update(((tool, type_arg), (tool, id_arg)))
    for tool, arg in BATCH_ENVELOPES.items():
        pairs.add((tool, arg))
    for tool, (_doc_arg, id_arg, _method) in DOC_OBJECT_ARGS.items():
        pairs.add((tool, id_arg))
    pairs.add(("claim_parallel_batch", "item_sessions"))
    pairs.add(("promote_proposal", "allow_project_transfer"))
    return frozenset(pairs)


def generic_rule_keys() -> "frozenset[str]":
    """Argument names covered on EVERY tool by the generic rules (the project
    rule is ``project_id`` / ``*_project_id`` / ``*_project_name``, not listed
    here because it is a pattern)."""
    return frozenset(_SESSION_ARG_KEYS) | frozenset(_SESSION_LIST_KEYS)


# ---------------------------------------------------------------------------
# The checker
# ---------------------------------------------------------------------------

class _Checker:
    """One scoped call's checks: scope list, db handle and a per-call memo so an
    id named twice (or in several batch entries) is resolved once."""

    def __init__(self, db: Any, scoped_project_ids: "list[str]") -> None:
        self.db = db
        # Membership only; an empty set still means "scoped to nothing".
        self.scope = frozenset(str(p) for p in scoped_project_ids)
        self._memo: "dict[tuple[str, str], Any]" = {}

    @staticmethod
    def deny() -> NoReturn:
        raise ValueError(OUT_OF_SCOPE)

    def _text(self, value: Any) -> str:
        """A trimmed argument string; ids are always strings, so any other
        non-empty type is malformed and refused rather than coerced."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if not value:  # False, 0, [], {} -- "not given"
            return ""
        self.deny()

    def _ids(self, value: Any, many: bool) -> "list[str]":
        if not many:
            one = self._text(value)
            return [one] if one else []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, (list, tuple)):
            return [i for i in (self._text(v) for v in value) if i]
        if not value:
            return []
        self.deny()

    async def owner_of(self, kind: str, object_id: str) -> Any:
        key = (kind, object_id)
        if key not in self._memo:
            self._memo[key] = await _RESOLVERS[kind](self.db, object_id)
        return self._memo[key]

    async def check_object(self, arg: ObjectArg, value: Any) -> None:
        for object_id in self._ids(value, arg.many):
            owner = await self.owner_of(arg.kind, object_id)
            if owner is _MISSING:
                if arg.missing_ok:
                    continue
                self.deny()
            if owner not in self.scope:
                self.deny()

    # -- generic rules ------------------------------------------------------

    async def check_project_args(self, args: "dict[str, Any]") -> None:
        """M1: every ``project_id`` / ``*_project_id`` argument, and every
        ``*_project_name`` argument that resolves, must be in scope.

        (``project_name`` itself is settled into ``project_id`` by
        ``_resolve_project_reference`` before this runs.) An unresolvable name is
        left to the handler's own error, as before.
        """
        for key, value in args.items():
            if not isinstance(key, str):
                continue
            if key == "project_id" or key.endswith("_project_id"):
                pid = self._text(value)
                if pid and pid not in self.scope:
                    self.deny()
            elif key.endswith("_project_name"):
                pname = self._text(value)
                if not pname:
                    continue
                project = await db_module.get_project_by_name(self.db, pname)
                if project and str(project["id"]) not in self.scope:
                    self.deny()

    async def check_session_args(self, args: "dict[str, Any]") -> None:
        """Any session pointer must name a session of an in-scope project; an
        unknown session id is left to the handler."""
        for key in _SESSION_ARG_KEYS:
            await self.check_object(_sess(key, missing_ok=True), args.get(key))
        for key in _SESSION_LIST_KEYS:
            await self.check_object(_sess(key, many=True, missing_ok=True), args.get(key))

    # -- profile layers -------------------------------------------------------

    async def _profile_scope_owner(self, scope_type: Any, scope_id: Any) -> Any:
        """Project that owns a profile ``(scope_type, scope_id)``, ``None`` when
        the scope type is tenant-level policy (workspace/user/hosted_default),
        ``_MISSING`` when it names an object that does not exist.

        ``scope_type`` is normalised exactly as the profile contract does
        (``strip().lower()``), so ``" Project "`` cannot dodge the check.
        """
        stype = scope_type.strip().lower() if isinstance(scope_type, str) else ""
        sid = self._text(scope_id)
        if not sid:
            return None
        if stype in _PROJECT_SCOPE_TYPES:
            return sid
        if stype == "sprint_version":
            # capability profiles key a sprint version as "<project_id>:<version>"
            return sid.split(":", 1)[0]
        if stype == "item":
            return await self.owner_of("sprint_item", sid)
        if stype == "session":
            return await self.owner_of("session", sid)
        return None

    async def check_profile_scope(self, scope_type: Any, scope_id: Any) -> None:
        owner = await self._profile_scope_owner(scope_type, scope_id)
        if owner is None:
            return
        if owner is _MISSING or owner not in self.scope:
            self.deny()

    async def profile_row_visible(self, row: Any) -> bool:
        if not isinstance(row, dict):
            return False
        owner = await self._profile_scope_owner(row.get("scope_type"), row.get("scope_id"))
        return owner is None or (owner is not _MISSING and owner in self.scope)

    # -- batch envelopes ------------------------------------------------------

    async def check_batch(self, container: Any) -> None:
        """Walk the per-entry dicts of a batch tool (``batch_read`` nests its
        operation arguments under ``args``)."""
        if not isinstance(container, list):
            return  # the engine rejects it as malformed
        for element in container:
            if not isinstance(element, dict):
                continue
            nested = element.get("args")
            for candidate in (element, nested if isinstance(nested, dict) else None):
                if candidate is None:
                    continue
                await self.check_project_args(candidate)
                await self.check_session_args(candidate)
                await self.check_profile_scope(
                    candidate.get("scope_type"), candidate.get("scope_id"),
                )

    # -- per-tool special rules ----------------------------------------------

    async def check_special(self, name: str, args: "dict[str, Any]") -> None:
        if name == "send_message":
            # The row is stamped with the named project, so the recipient must
            # belong to that same project, not merely to another in-scope one.
            project_id = self._text(args.get("project_id"))
            recipient = self._text(args.get("to_session_id"))
            if project_id and recipient:
                owner = await self.owner_of("session", recipient)
                if owner is not _MISSING and owner != project_id:
                    self.deny()
        elif name == "promote_proposal":
            # The override pulls a proposal out of the project it was created
            # under; a scoped caller never gets to do that.
            if args.get("allow_project_transfer"):
                self.deny()
        elif name == "claim_parallel_batch":
            # item_sessions maps {item_id: worker session}: the VALUES are
            # session pointers. A foreign worker session would end up holding
            # the claim (and the file locks) of an item of the caller's project.
            mapping = args.get("item_sessions")
            if isinstance(mapping, dict):
                await self.check_object(
                    _sess("item_sessions", many=True, missing_ok=True),
                    list(mapping.values()),
                )
            elif mapping:
                self.deny()

    # -- document-store objects -------------------------------------------------

    async def check_doc_object(
        self,
        name: str,
        args: "dict[str, Any]",
        doc_store_factory: "Callable[[], Awaitable[Any]] | None",
    ) -> None:
        """The figure / table id must be one of the named document's own.

        ``doc`` is resolved against ``project_id`` (already in scope by the
        generic rule) exactly as the handler does; an unknown document or a blank
        id is left to the handler (it stops there without touching an object).
        Fails closed when the document store cannot be opened.
        """
        spec = DOC_OBJECT_ARGS.get(name)
        if spec is None:
            return
        doc_arg, id_arg, lister = spec
        object_id = self._text(args.get(id_arg))
        project_id = self._text(args.get("project_id"))
        doc_source = args.get(doc_arg)
        if not object_id or not project_id or not isinstance(doc_source, str) or not doc_source.strip():
            return
        store = await doc_store_factory() if doc_store_factory is not None else None
        if store is None:
            self.deny()
        doc_row = await store.get_document(project_id, doc_source)
        if doc_row is None:
            return
        owned = await getattr(store, lister)(doc_row["id"])
        if object_id not in {str(o.get("id")) for o in owned or [] if isinstance(o, dict)}:
            self.deny()

    # -- holder redaction (F-M2) ----------------------------------------------

    async def session_visible(self, session_id: str, own: "frozenset[str]") -> bool:
        """True when a session named in a RESULT may be shown to this caller: it
        is one the caller itself named (and passed the scope check), or it belongs
        to an in-scope project. An unknown session is not visible (fail closed)."""
        if session_id in own:
            return True
        owner = await self.owner_of("session", session_id)
        return owner is not _MISSING and owner in self.scope

    async def _note_foreign_session(self, session_id: str, state: "_Redaction") -> None:
        """Remember a foreign session's id, short id and NAME so free text that
        spells them out ("claimed by session <name>", or the first 8 characters of
        the id when the session has no name) can be scrubbed too."""
        state.ids.add(session_id)
        state.names.add(session_id[:8])
        key = ("session_name", session_id)
        if key not in self._memo:
            async with self.db.execute(
                "SELECT name FROM sessions WHERE id = ?", (session_id,)
            ) as cur:
                row = await cur.fetchone()
            self._memo[key] = (db_module._row_to_dict(row) or {}).get("name") if row is not None else None
        if self._memo[key]:
            state.names.add(str(self._memo[key]))

    async def _elements_held_in_scope(
        self, file_path: Any, elements: Any, own: "frozenset[str]",
    ) -> "list[str] | None":
        """The ids in ``elements`` that only in-scope sessions hold a live docx
        claim on, in their original order; ``None`` when the list cannot be
        attributed (not a list of strings, no file path, or the claims cannot be
        read).

        ``elements`` is a list a docx claim / lease conflict built from every OTHER
        session's live claims on ``file_path`` (``other_claimed_elements`` /
        ``conflicting_elements``). The claims are read again, with the same
        liveness rule, to see who holds each id: an id with no live holder any
        more, or with any foreign holder, is not kept, so the result can only
        shrink.
        """
        if not isinstance(elements, list) or not all(isinstance(e, str) for e in elements):
            return None
        if not isinstance(file_path, str) or not file_path.strip():
            return None
        key = ("docx_holders", file_path)
        if key not in self._memo:
            try:
                # exclude_session_id="" excludes nobody: the caller's own session
                # is never a holder of an element in these lists, and if it were
                # it is visible anyway.
                claims = await db_module._live_docx_region_claims_for_file(self.db, file_path, "")
            except Exception:  # noqa: BLE001 -- unreadable claims -> unattributable -> dropped
                self._memo[key] = None
            else:
                holders: "dict[str, list[str]]" = {}
                for claim in claims:
                    holders.setdefault(str(claim.get("element_id")), []).append(str(claim.get("session_id") or ""))
                self._memo[key] = holders
        holders = self._memo[key]
        if holders is None:
            return None
        kept: "list[str]" = []
        for element in elements:
            sessions = holders.get(element)
            if not sessions:
                continue
            if all([await self.session_visible(session, own) for session in sessions]):
                kept.append(element)
        return kept

    async def redact_foreign_holders(self, result: Any, own: "frozenset[str]") -> Any:
        """Blank every lock / claim holder that belongs to an out-of-scope session.

        The conflict itself is kept (``claimed: false``, the reason, the file, the
        timestamps) so the caller still learns the path is held; only WHO holds it
        and WHAT they are editing is removed. See :data:`REDACT_HOLDER_TOOLS`.
        """
        if not isinstance(result, (dict, list)):
            return result
        state = _Redaction()
        redacted = await self._redact_node(result, own, state)
        if not state.hit:
            return result
        if isinstance(redacted, dict):
            redacted["holder_redacted"] = True
        return _scrub_text(redacted, state)

    async def _redact_node(self, node: Any, own: "frozenset[str]", state: "_Redaction") -> Any:
        if isinstance(node, list):
            return [await self._redact_node(item, own, state) for item in node]
        if not isinstance(node, dict):
            return node
        out = dict(node)
        foreign = [
            out[key].strip()
            for key in _HOLDER_ID_KEYS
            if isinstance(out.get(key), str) and out[key].strip()
            and not await self.session_visible(out[key].strip(), own)
        ]
        if foreign:
            state.hit = True
            for foreign_id in foreign:
                await self._note_foreign_session(foreign_id, state)
            for key in _HOLDER_ID_KEYS:
                if isinstance(out.get(key), str) and out[key].strip() in foreign:
                    out[key] = None
            for key in _HOLDER_NAME_KEYS:
                if key in out:
                    if isinstance(out[key], str) and out[key]:
                        state.names.add(out[key])
                    out[key] = None
            if any(key in out for key in _CLAIM_DETAIL_MARKERS):
                # A symbol / docx-element claim row: what the foreign session is
                # editing is as private as who it is.
                for key in _CLAIM_DETAIL_KEYS:
                    if key in out:
                        out[key] = None
            out["holder_redacted"] = True
        for key in list(out):
            value = out[key]
            if key in _UNATTRIBUTABLE_KEYS:
                # Element ids merged across EVERY other live claimant of the file,
                # so the list itself says nothing about who holds which. Re-read the
                # live claims to attribute each element: the ones held only by
                # in-scope sessions stay (an in-scope-only conflict keeps its whole
                # list), the ones a foreign session holds go, and a list that cannot
                # be attributed at all is dropped (fail closed).
                kept = await self._elements_held_in_scope(out.get("file_path"), value, own)
                if kept is None or len(kept) != len(value):
                    state.hit = True
                    if kept:
                        out[key] = kept
                    else:
                        del out[key]
            elif key in _SESSION_ID_LIST_KEYS and isinstance(value, list) and all(
                isinstance(v, str) for v in value
            ):
                kept = [v for v in value if await self.session_visible(v.strip(), own)]
                if len(kept) != len(value):
                    state.hit = True
                    for removed in (v.strip() for v in value if v not in kept):
                        await self._note_foreign_session(removed, state)
                out[key] = kept
            elif isinstance(value, (dict, list)):
                out[key] = await self._redact_node(value, own, state)
        return out


#: Result keys that carry the session that holds a lock / claim / lease.
_HOLDER_ID_KEYS: "tuple[str, ...]" = ("session_id", "holder_session_id", "holder")
_HOLDER_NAME_KEYS: "tuple[str, ...]" = ("session_name", "holder_session_name")
#: A row that has one of these is a symbol / docx-element claim; its detail keys
#: are blanked next to the holder's identity.
_CLAIM_DETAIL_MARKERS: "tuple[str, ...]" = ("symbol_name", "element_id")
_CLAIM_DETAIL_KEYS: "tuple[str, ...]" = (
    "id", "symbol_name", "symbol_type", "symbol", "line_start", "line_end",
    "element_id", "item_id",
)
#: Lists of BARE session ids (claim_file's read_claims / readers).
_SESSION_ID_LIST_KEYS: "tuple[str, ...]" = ("read_claims", "readers")
#: Lists of element ids merged across every other holder of a document; the holder
#: of each id is looked up again (``_Checker._elements_held_in_scope``) so the ids
#: in-scope sessions hold survive.
_UNATTRIBUTABLE_KEYS: "tuple[str, ...]" = ("other_claimed_elements", "conflicting_elements")
#: Free-text keys whose strings may embed a holder's session NAME ("claimed by
#: session X"). Names are arbitrary words, so they are only scrubbed from these.
_TEXT_KEYS: "tuple[str, ...]" = ("message", "error", "reason", "hint", "detail", "warning")


class _Redaction:
    """What one result redaction saw: whether anything was blanked and which
    foreign session ids / names to scrub out of free text."""

    def __init__(self) -> None:
        self.hit = False
        self.ids: "set[str]" = set()
        self.names: "set[str]" = set()


def _scrub_text(node: Any, state: "_Redaction", key: "str | None" = None) -> Any:
    """Replace foreign session ids (anywhere) and names (in free-text keys) inside
    the strings of an already structurally redacted result."""
    if isinstance(node, dict):
        return {k: _scrub_text(v, state, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_scrub_text(v, state, key) for v in node]
    if not isinstance(node, str):
        return node
    text = node
    for foreign_id in state.ids:
        if foreign_id:
            text = text.replace(foreign_id, "<redacted>")
    if key in _TEXT_KEYS:
        for foreign_name in state.names:
            if foreign_name:
                text = re.sub(
                    r"(?<![\w-])" + re.escape(foreign_name) + r"(?![\w-])", "<redacted>", text,
                )
    return text


async def enforce_scoped_call(
    name: str,
    args: "dict[str, Any]",
    db: Any,
    scoped_project_ids: "list[str] | None",
    *,
    doc_store_factory: "Callable[[], Awaitable[Any]] | None" = None,
) -> None:
    """Refuse a tool call that reaches outside a project-scoped caller's scope.

    Raises ``ValueError("project is outside your access scope")``; returns
    ``None`` otherwise. ``scoped_project_ids`` of ``None`` (owner, self-hosted,
    demo, stdio) returns immediately with no DB access.

    Runs AFTER ``_resolve_project_reference`` has folded ``project_name`` into
    ``project_id``, so ``args["project_id"]`` is the final target project.
    ``doc_store_factory`` opens the document-structure store for the tools in
    :data:`DOC_OBJECT_ARGS`; without one those tools fail closed.
    """
    if scoped_project_ids is None:
        return
    chk = _Checker(db, scoped_project_ids)
    if name in DENY:
        chk.deny()
    if name in REQUIRE_PROJECT and not chk._text(args.get("project_id")):
        chk.deny()
    await chk.check_project_args(args)
    await chk.check_session_args(args)
    for ref in OBJECT_ARGS.get(name, ()):
        await chk.check_object(ref, args.get(ref.arg))
    await chk.check_special(name, args)
    await chk.check_doc_object(name, args, doc_store_factory)
    for type_arg, id_arg in PROFILE_SCOPE_ARGS.get(name, ()):
        await chk.check_profile_scope(args.get(type_arg), args.get(id_arg))
    envelope = BATCH_ENVELOPES.get(name)
    if envelope is not None:
        await chk.check_batch(args.get(envelope))


async def enforce_scoped_prompt(
    name: str,
    args: Any,
    db: Any,
    scoped_project_ids: "list[str] | None",
) -> None:
    """Refuse a ``prompts/get`` whose arguments name a project outside the scope.

    The ``executor-goal`` prompt renders the named project's live pending sprint
    items (and the other prompts echo the project they are given), so the same
    generic rules as a tool call apply: every ``project_id`` / ``*_project_id``
    argument must be in scope and every ``project_name`` /
    ``*_project_name`` must resolve to an in-scope project (an unresolvable name
    is left to the prompt builder, which degrades to its fill-in template, exactly
    as ``tools/call`` leaves it to the handler). Malformed ``arguments`` (not an
    object) are refused for a scoped caller. ``None`` (owner, self-hosted, demo,
    stdio) returns immediately with no DB access.
    """
    if scoped_project_ids is None:
        return
    chk = _Checker(db, scoped_project_ids)
    if not isinstance(args, dict):
        chk.deny()
    await chk.check_project_args(args)
    pname = chk._text(args.get("project_name"))
    if pname:
        project = await db_module.get_project_by_name(db, pname)
        if project and str(project["id"]) not in chk.scope:
            chk.deny()


async def filter_scoped_result(
    name: str,
    result: Any,
    db: Any,
    scoped_project_ids: "list[str] | None",
    args: "dict[str, Any] | None" = None,
) -> Any:
    """Narrow a result for a scoped caller; a no-op (and no DB access) otherwise.

    * :data:`FILTER_RESULT_TOOLS` -- cross-project listings lose the rows outside
      the scope (``list_profile_layers``).
    * :data:`REDACT_HOLDER_TOOLS` -- lock / claim / lease holders that belong to an
      out-of-scope session are blanked, the conflict itself kept (F-M2).
      ``args`` supplies the session ids the caller named (already scope-checked),
      which stay visible so a caller never loses its own identity from a result.
    """
    if scoped_project_ids is None:
        return result
    if name in FILTER_RESULT_TOOLS:
        if not isinstance(result, list):
            return result
        chk = _Checker(db, scoped_project_ids)
        return [row for row in result if await chk.profile_row_visible(row)]
    if name in REDACT_HOLDER_TOOLS:
        chk = _Checker(db, scoped_project_ids)
        own: "set[str]" = set()
        for key in _SESSION_ARG_KEYS:
            value = (args or {}).get(key)
            if isinstance(value, str) and value.strip():
                own.add(value.strip())
        return await chk.redact_foreign_holders(result, frozenset(own))
    return result


async def session_in_scope(
    db: Any, session_id: str, scoped_project_ids: "list[str] | None",
) -> bool:
    """True when the caller may write to this session's activity feed.

    ``None`` (unscoped) is always True. Used by the dispatcher's error path so a
    refused call that merely NAMES a foreign session never lands a row in that
    session's feed.
    """
    if scoped_project_ids is None:
        return True
    chk = _Checker(db, scoped_project_ids)
    owner = await chk.owner_of("session", str(session_id))
    return owner is not _MISSING and owner in chk.scope
