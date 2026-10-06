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

Every refusal raises ``ValueError("project is outside your access scope")``,
the same opaque message the pre-dispatch gate uses, so a caller cannot tell
which layer (or which argument) refused it.

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

``tests/test_scope_guard_mcp.py`` enumerates these tables: every tool and
argument named here must exist in the real tool schemas, and every tool with an
unbound id argument must be listed here or explicitly exempted with a reason,
so the tables cannot silently rot.
"""
from __future__ import annotations

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


_RESOLVERS: "dict[str, Callable[[Any, str], Awaitable[Any]]]" = {
    "note": _note_project,
    "decision": _decision_project,
    "hitl": _hitl_project,
    "wave_run": _wave_run_project,
    "worktree": _worktree_project,
    "sprint_item": _sprint_item_project,
    "session": _session_project,
    "proposal": _proposal_project,
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
    )


def schema_argument_refs() -> "list[tuple[str, str]]":
    """``(tool, argument)`` pairs the tables expect to exist in the tool schemas."""
    refs: "list[tuple[str, str]]" = []
    for tool, args in OBJECT_ARGS.items():
        refs.extend((tool, a.arg) for a in args)
    for tool, pairs in PROFILE_SCOPE_ARGS.items():
        for type_arg, id_arg in pairs:
            refs.extend(((tool, type_arg), (tool, id_arg)))
    for tool, arg in BATCH_ENVELOPES.items():
        refs.append((tool, arg))
    refs.extend((tool, "project_id") for tool in REQUIRE_PROJECT)
    refs.append(("promote_proposal", "allow_project_transfer"))
    refs.append(("send_message", "project_id"))
    return refs


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


async def enforce_scoped_call(
    name: str,
    args: "dict[str, Any]",
    db: Any,
    scoped_project_ids: "list[str] | None",
) -> None:
    """Refuse a tool call that reaches outside a project-scoped caller's scope.

    Raises ``ValueError("project is outside your access scope")``; returns
    ``None`` otherwise. ``scoped_project_ids`` of ``None`` (owner, self-hosted,
    demo, stdio) returns immediately with no DB access.

    Runs AFTER ``_resolve_project_reference`` has folded ``project_name`` into
    ``project_id``, so ``args["project_id"]`` is the final target project.
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
    for type_arg, id_arg in PROFILE_SCOPE_ARGS.get(name, ()):
        await chk.check_profile_scope(args.get(type_arg), args.get(id_arg))
    envelope = BATCH_ENVELOPES.get(name)
    if envelope is not None:
        await chk.check_batch(args.get(envelope))


async def filter_scoped_result(
    name: str,
    result: Any,
    db: Any,
    scoped_project_ids: "list[str] | None",
) -> Any:
    """Drop the rows of a cross-project listing that lie outside the scope.

    Only :data:`FILTER_RESULT_TOOLS` are touched, and only for a scoped caller;
    anything else is returned unchanged with no DB access.
    """
    if (
        scoped_project_ids is None
        or name not in FILTER_RESULT_TOOLS
        or not isinstance(result, list)
    ):
        return result
    chk = _Checker(db, scoped_project_ids)
    return [row for row in result if await chk.profile_row_visible(row)]


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
