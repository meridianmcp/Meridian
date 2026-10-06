"""Wave 2 of pinned decision 6fe5210c -- MCP tools that reach a project through a
route the pre-dispatch gate cannot see.

The gate in ``_handle_mcp_request`` only compares ``project_id`` /
``project_name``. ``meridian/mcp/scope_guard.py`` closes everything else in one
table-driven function; these tests enumerate those tables against the real tool
schemas and the real handlers:

* every tool the guard lists is refused for a project-scoped caller when its
  object belongs to another project, and still works for an in-scope object;
* ``merge_project`` / ``set_parent_project`` / ``create_project`` (other
  ``*_project_id`` / ``*_project_name`` arguments);
* listings that span every project when ``project_id`` is omitted, and the
  cross-project ``file_path``-only tools;
* profile layers and capability profiles (``scope_type == "project"`` and the
  other project-owned scope types), batch entries, and the list filter;
* the tables cannot rot: every tool/argument they name exists in the tool
  schemas, and every tool with an unbound id argument is listed or exempted.

Owners, workspace-wide members and self-hosted callers pass
``scoped_project_ids=None`` and must see no change at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

import pytest

import meridian.server  # noqa: F401 -- load the server before handler to avoid its import cycle
from meridian import db as db_module
from meridian.mcp import handler as mcp_handler
from meridian.mcp import scope_guard
from meridian.mcp_tools import _MCP_TOOLS_LIST

SCOPE_ERROR = "outside your access scope"


# ---------------------------------------------------------------------------
# A world with two in-scope projects and one foreign project
# ---------------------------------------------------------------------------

@dataclass
class _World:
    mine: str
    mine2: str
    theirs: str
    own: "dict[str, str]"
    own2: "dict[str, str]"
    foreign: "dict[str, str]"
    global_proposal: str  # a workspace-global proposal: owned by no project


async def _objects(db: Any, pid: str, label: str) -> "dict[str, str]":
    session = await db_module.register_session(db, pid, f"sess-{label}")
    session_b = await db_module.register_session(db, pid, f"sess-b-{label}")
    return {
        "note": (await db_module.add_project_note(db, pid, f"note-{label}", "body"))["id"],
        "decision": (await db_module.pin_decision(db, pid, f"dec-{label}", "body"))["id"],
        "hitl": (await db_module.request_hitl(db, pid, f"question {label}?"))["id"],
        "session": session["id"],
        "session_b": session_b["id"],
        "wave_run": (await db_module.create_wave_run(db, pid))["id"],
        "proposal": (await db_module.add_workspace_proposal(db, f"prop-{label}", "body", project_id=pid))["id"],
        "proposal_b": (await db_module.add_workspace_proposal(db, f"prop-b-{label}", "body", project_id=pid))["id"],
        "worktree": (await db_module.register_worktree(db, session["id"], pid, f"branch-{label}", f"wt-{label}"))["id"],
        "sprint_item": (await db_module.add_sprint_item(db, pid, "v1", f"item-{label}"))["id"],
    }


async def _world(db: Any) -> _World:
    mine = (await db_module.create_project(db, "guard-mine"))["id"]
    mine2 = (await db_module.create_project(db, "guard-mine-2"))["id"]
    theirs = (await db_module.create_project(db, "guard-theirs"))["id"]
    return _World(
        mine=mine, mine2=mine2, theirs=theirs,
        own=await _objects(db, mine, "own"),
        own2=await _objects(db, mine2, "own2"),
        foreign=await _objects(db, theirs, "foreign"),
        global_proposal=(await db_module.add_workspace_proposal(db, "global", "body"))["id"],
    )


async def _call(db: Any, tmp_path: Any, name: str, args: "dict[str, Any]", scoped: "list[str] | None") -> Any:
    return await mcp_handler._dispatch_mcp_tool(
        name, args, db, str(tmp_path), tenant=None, scoped_project_ids=scoped,
    )


async def _refused(db: Any, tmp_path: Any, name: str, args: "dict[str, Any]", scoped: "list[str] | None") -> None:
    with pytest.raises(ValueError, match=SCOPE_ERROR):
        await _call(db, tmp_path, name, args, scoped)


# ---------------------------------------------------------------------------
# 1. Every tool in the object table: refused out of scope, works in scope
# ---------------------------------------------------------------------------

#: Arguments each table tool needs besides its object ids. Adding a tool to
#: scope_guard.OBJECT_ARGS without an entry here fails test_the_table_test_covers_every_listed_tool.
_EXTRA: "dict[str, Callable[[_World], dict[str, Any]]]" = {
    "delete_note": lambda w: {},
    "update_decision": lambda w: {"body": "edited"},
    "archive_decision": lambda w: {},
    "validate_assumption": lambda w: {"finding": "checked", "confirmed": True},
    "save_finding": lambda w: {"project_id": w.mine, "summary": "found it"},
    "capture_research_finding": lambda w: {
        "project_id": w.mine, "url": "https://example.test/paper", "summary": "found it",
    },
    "get_hitl_request": lambda w: {},
    "answer_hitl": lambda w: {"answer": "yes"},
    "dismiss_hitl": lambda w: {},
    "get_session_log": lambda w: {},
    "get_session_activity": lambda w: {},
    "get_sprint_notes": lambda w: {},
    "add_sprint_note": lambda w: {"title": "t", "body": "b"},
    "receive_messages": lambda w: {},
    "claim_file": lambda w: {"file_path": "scope/a.py"},
    "release_file": lambda w: {"file_path": "scope/a.py"},
    "claim_docx_region": lambda w: {"file_path": "scope/a.docx", "element_id": "e1"},
    "release_docx_region_claims": lambda w: {},
    "acquire_docx_document_lease": lambda w: {"file_path": "scope/a.docx"},
    "release_docx_document_lease": lambda w: {"file_path": "scope/a.docx"},
    "get_graph_diff": lambda w: {},
    "send_message": lambda w: {"project_id": w.mine, "payload": "hello"},
    "idle_until_session_done": lambda w: {"timeout_seconds": 0},
    "idle_until_all_done": lambda w: {},
    "finalize_wave_run": lambda w: {},
    "resume_wave": lambda w: {},
    "advance_proposal_status": lambda w: {"status": "investigating"},
    "create_proposal_successor": lambda w: {"title": "next", "body": "b", "relation_type": "refines"},
    "get_proposal_lineage": lambda w: {},
    "link_proposal_lineage": lambda w: {"relation_type": "refines"},
    "compare_proposal_versions": lambda w: {},
    "promote_proposal": lambda w: {"project_id": w.mine},
    "preview_proposal_promotion": lambda w: {"project_id": w.mine, "depth": "proposal"},
    "commit_proposal_promotion": lambda w: {
        "project_id": w.mine, "depth": "proposal", "preview_hash": "stale",
    },
    "set_active_repo": lambda w: {},
}

#: Tools whose handler legitimately raises for an in-scope object in this test
#: setup (no tunnel tenant). The message proves the call got PAST the guard.
_IN_SCOPE_RAISES = {
    "set_active_repo": "requires an authenticated tenant",
}


def _args_for(tool: str, world: _World, foreign_index: "int | None" = None) -> "dict[str, Any]":
    """Arguments for ``tool``: in-scope objects everywhere, except the ref at
    ``foreign_index`` which points at the foreign project's object."""
    args = dict(_EXTRA[tool](world))
    seen: "dict[str, int]" = {}
    for i, ref in enumerate(scope_guard.OBJECT_ARGS[tool]):
        n = seen.get(ref.kind, 0)
        seen[ref.kind] = n + 1
        key = ref.kind if n == 0 else f"{ref.kind}_b"
        pool = world.foreign if i == foreign_index else world.own
        args[ref.arg] = [pool[key]] if ref.many else pool[key]
    return args


def _every_ref() -> "list[tuple[str, int]]":
    return [
        (tool, i) for tool, refs in sorted(scope_guard.OBJECT_ARGS.items()) for i in range(len(refs))
    ]


def test_the_table_test_covers_every_listed_tool():
    assert set(_EXTRA) == set(scope_guard.OBJECT_ARGS)


@pytest.mark.parametrize("tool,ref_index", _every_ref())
async def test_listed_tool_is_refused_for_an_out_of_scope_object(db, tmp_path, tool, ref_index):
    w = await _world(db)
    await _refused(db, tmp_path, tool, _args_for(tool, w, foreign_index=ref_index), [w.mine])


@pytest.mark.parametrize("tool", sorted(scope_guard.OBJECT_ARGS))
async def test_listed_tool_still_works_for_an_in_scope_object(db, tmp_path, tool):
    w = await _world(db)
    args = _args_for(tool, w)
    expected_error = _IN_SCOPE_RAISES.get(tool)
    if expected_error is not None:
        with pytest.raises(ValueError, match=expected_error):
            await _call(db, tmp_path, tool, args, [w.mine])
    else:
        await _call(db, tmp_path, tool, args, [w.mine])  # must not raise at all


@pytest.mark.parametrize("tool", sorted(scope_guard.OBJECT_ARGS))
async def test_listed_tool_is_unchanged_for_an_unscoped_caller(db, tmp_path, tool):
    """Owners and self-hosted callers (scoped_project_ids=None) reach another
    project's object exactly as before: the guard never runs."""
    w = await _world(db)
    args = _args_for(tool, w, foreign_index=0)
    try:
        await _call(db, tmp_path, tool, args, None)
    except ValueError as exc:
        assert SCOPE_ERROR not in str(exc)


@pytest.mark.parametrize("tool", sorted(scope_guard.OBJECT_ARGS))
async def test_listed_tool_with_an_unknown_object_is_refused_for_a_scoped_caller(db, tmp_path, tool):
    """An unknown id looks exactly like another project's id (no existence
    oracle) -- except for the tools whose own contract answers an unknown id."""
    w = await _world(db)
    args = _args_for(tool, w)
    ref = scope_guard.OBJECT_ARGS[tool][0]
    args[ref.arg] = ["no-such-object"] if ref.many else "no-such-object"
    if ref.missing_ok:
        # delete_note -> {"deleted": False}; idle_until_* -> done/missing.
        await _call(db, tmp_path, tool, args, [w.mine])
    else:
        await _refused(db, tmp_path, tool, args, [w.mine])


# --- the refusals leave the foreign project untouched ----------------------

async def test_refused_hitl_calls_change_nothing(db, tmp_path):
    w = await _world(db)
    hitl = w.foreign["hitl"]
    await _refused(db, tmp_path, "answer_hitl", {"request_id": hitl, "answer": "approved"}, [w.mine])
    await _refused(db, tmp_path, "dismiss_hitl", {"request_id": hitl}, [w.mine])
    await _refused(db, tmp_path, "get_hitl_request", {"request_id": hitl}, [w.mine])
    assert (await db_module.get_hitl_request(db, hitl))["status"] == "pending"
    # The owner of the project can still answer it.
    answered = await _call(db, tmp_path, "answer_hitl", {"request_id": hitl, "answer": "approved"}, [w.theirs])
    assert answered["status"] == "answered"


async def test_refused_session_tools_change_nothing(db, tmp_path):
    w = await _world(db)
    sid = w.foreign["session"]
    await _refused(db, tmp_path, "add_sprint_note", {"session_id": sid, "title": "planted", "body": "x"}, [w.mine])
    await _refused(db, tmp_path, "claim_file", {"session_id": sid, "file_path": "scope/a.py"}, [w.mine])
    assert await db_module.get_session_notes(db, sid) == []
    assert (await db_module.get_file_claims(db, "scope/a.py"))["file_lock"] is None


async def test_receive_messages_does_not_mark_a_foreign_inbox_read(db, tmp_path):
    w = await _world(db)
    sid = w.foreign["session"]
    await db_module.send_message(db, w.theirs, sid, "for you only")
    await _refused(db, tmp_path, "receive_messages", {"session_id": sid}, [w.mine])
    # Still unread: the owner reads it.
    got = await _call(db, tmp_path, "receive_messages", {"session_id": sid}, [w.theirs])
    assert [m["payload"] for m in got] == ["for you only"]


async def test_send_message_cannot_inject_into_another_projects_session(db, tmp_path):
    w = await _world(db)
    await _refused(
        db, tmp_path, "send_message",
        {"project_id": w.mine, "to_session_id": w.foreign["session"], "payload": "injected"}, [w.mine],
    )
    assert await db_module.receive_messages(db, w.foreign["session"], mark_read=False) == []


async def test_send_message_recipient_must_belong_to_the_named_project(db, tmp_path):
    """Both projects are in scope, but the message row is stamped with the
    named project, so the recipient has to live in that same project."""
    w = await _world(db)
    scoped = [w.mine, w.mine2]
    await _refused(
        db, tmp_path, "send_message",
        {"project_id": w.mine, "to_session_id": w.own2["session"], "payload": "cross"}, scoped,
    )
    sent = await _call(
        db, tmp_path, "send_message",
        {"project_id": w.mine, "to_session_id": w.own["session"], "payload": "same project"}, scoped,
    )
    assert sent["to_session_id"] == w.own["session"]


async def test_refused_wave_and_proposal_calls_change_nothing(db, tmp_path):
    w = await _world(db)
    before = (await db_module.get_wave_run(db, w.foreign["wave_run"]))["status"]
    await _refused(db, tmp_path, "finalize_wave_run", {"wave_run_id": w.foreign["wave_run"]}, [w.mine])
    assert (await db_module.get_wave_run(db, w.foreign["wave_run"]))["status"] == before

    prop = w.foreign["proposal"]
    await _refused(db, tmp_path, "advance_proposal_status", {"proposal_id": prop, "status": "rejected"}, [w.mine])
    await _refused(
        db, tmp_path, "create_proposal_successor",
        {"proposal_id": prop, "title": "planted", "body": "x", "relation_type": "refines"}, [w.mine],
    )
    rows = await db_module.get_workspace_proposals(db, status="all", project_id=w.theirs)
    assert {r["id"]: r["status"] for r in rows}[prop] == "raw"
    assert not any(r["title"] == "planted" for r in rows)


async def test_validate_assumption_cannot_stamp_or_file_a_blocking_hitl_in_another_project(db, tmp_path):
    w = await _world(db)
    dec = w.foreign["decision"]
    hitls_before = len(await db_module.list_hitl_requests(db, w.theirs, status=None))
    await _refused(
        db, tmp_path, "validate_assumption",
        {"decision_id": dec, "finding": "it is false", "confirmed": False}, [w.mine],
    )
    assert (await db_module.get_pinned_decision(db, dec)).get("assumption_status") in (None, "", "unverified")
    assert len(await db_module.list_hitl_requests(db, w.theirs, status=None)) == hitls_before


async def test_wave1_note_and_decision_tools_are_refused_without_touching_the_object(db, tmp_path):
    w = await _world(db)
    await _refused(db, tmp_path, "delete_note", {"note_id": w.foreign["note"]}, [w.mine])
    await _refused(db, tmp_path, "update_decision", {"decision_id": w.foreign["decision"], "body": "x"}, [w.mine])
    await _refused(db, tmp_path, "archive_decision", {"decision_id": w.foreign["decision"]}, [w.mine])
    assert await db_module.get_project_note(db, w.foreign["note"]) is not None
    assert (await db_module.get_pinned_decision(db, w.foreign["decision"]))["body"] == "body"


# --- proposals ---------------------------------------------------------------

async def test_a_workspace_global_proposal_is_out_of_scope_for_a_scoped_caller(db, tmp_path):
    w = await _world(db)
    gid = w.global_proposal
    for tool, args in (
        ("advance_proposal_status", {"proposal_id": gid, "status": "investigating"}),
        ("get_proposal_lineage", {"proposal_id": gid}),
        ("promote_proposal", {"proposal_id": gid, "project_id": w.mine}),
        ("compare_proposal_versions", {"from_proposal_id": gid, "to_proposal_id": w.own["proposal"]}),
    ):
        await _refused(db, tmp_path, tool, args, [w.mine])
    # The unscoped owner still manages it.
    advanced = await _call(db, tmp_path, "advance_proposal_status", {"proposal_id": gid, "status": "investigating"}, None)
    assert advanced["status"] == "investigating"


@pytest.mark.parametrize("flag", [True, "true", 1, "false"])
async def test_promote_proposal_refuses_allow_project_transfer_for_a_scoped_caller(db, tmp_path, flag):
    """The override moves a proposal out of the project it was created under:
    even between two in-scope projects a scoped caller never gets it (and the
    db treats ANY truthy value, including the string "false", as set)."""
    w = await _world(db)
    await _refused(
        db, tmp_path, "promote_proposal",
        {"proposal_id": w.own2["proposal"], "project_id": w.mine,
         "allow_project_transfer": flag, "transfer_reason": "r"},
        [w.mine, w.mine2],
    )
    rows = await db_module.get_workspace_proposals(db, status="all", project_id=w.mine2)
    assert {r["status"] for r in rows} == {"raw"}
    # Unscoped callers keep the override.
    moved = await _call(
        db, tmp_path, "promote_proposal",
        {"proposal_id": w.own2["proposal"], "project_id": w.mine,
         "allow_project_transfer": True, "transfer_reason": "r"}, None,
    )
    assert moved.get("error") is None


# ---------------------------------------------------------------------------
# 2. M1: other *_project_id / *_project_name arguments
# ---------------------------------------------------------------------------

async def _project_state(db: Any, pid: str) -> "tuple[str, str]":
    project = await db_module.get_project(db, pid)
    return project["name"], project.get("status") or "active"


async def test_merge_project_refuses_an_out_of_scope_side_and_changes_nothing(db, tmp_path):
    w = await _world(db)
    theirs_before = await _project_state(db, w.theirs)
    mine_before = await _project_state(db, w.mine)
    scoped = [w.mine]
    cases = (
        {"source_project_id": w.theirs, "target_project_id": w.mine},   # pull their project in
        {"source_project_id": w.mine, "target_project_id": w.theirs},   # push mine into theirs
        {"source_project_id": w.theirs, "target_project_id": w.theirs + "x"},
        {"source_project_name": "guard-theirs", "target_project_id": w.mine},
        {"source_project_id": w.mine, "target_project_name": "guard-theirs"},
        # An in-scope id next to an out-of-scope name is refused too.
        {"source_project_id": w.mine, "source_project_name": "guard-theirs", "target_project_id": w.mine2},
    )
    for args in cases:
        await _refused(db, tmp_path, "merge_project", args, scoped)
    assert await _project_state(db, w.theirs) == theirs_before
    assert await _project_state(db, w.mine) == mine_before
    # Their child rows were never re-parented.
    assert await db_module.get_project_note(db, w.foreign["note"], project_id=w.theirs) is not None


async def test_merge_project_between_two_in_scope_projects_still_works(db, tmp_path):
    w = await _world(db)
    merged = await _call(
        db, tmp_path, "merge_project",
        {"source_project_id": w.mine2, "target_project_id": w.mine}, [w.mine, w.mine2],
    )
    assert merged["source_archived"] is True
    assert await db_module.get_project_note(db, w.own2["note"], project_id=w.mine) is not None


async def test_merge_project_with_an_unresolvable_name_is_left_to_the_handler(db, tmp_path):
    w = await _world(db)
    result = await _call(
        db, tmp_path, "merge_project",
        {"source_project_name": "no-such-project", "target_project_id": w.mine}, [w.mine],
    )
    assert "error" in result


async def test_merge_project_is_unchanged_for_an_unscoped_caller(db, tmp_path):
    w = await _world(db)
    merged = await _call(
        db, tmp_path, "merge_project", {"source_project_id": w.theirs, "target_project_id": w.mine}, None,
    )
    assert merged["source_archived"] is True


async def test_set_parent_project_refuses_an_out_of_scope_parent(db, tmp_path):
    w = await _world(db)
    scoped = [w.mine]
    await _refused(db, tmp_path, "set_parent_project", {"project_id": w.mine, "parent_project_id": w.theirs}, scoped)
    await _refused(db, tmp_path, "set_parent_project", {"project_id": w.mine, "parent_project_name": "guard-theirs"}, scoped)
    assert (await db_module.get_project(db, w.mine)).get("parent_project_id") in (None, "")
    # An in-scope parent is fine, and so is detaching (empty parent).
    attached = await _call(
        db, tmp_path, "set_parent_project", {"project_id": w.mine, "parent_project_id": w.mine2}, [w.mine, w.mine2],
    )
    assert attached.get("parent_project_id") == w.mine2
    detached = await _call(db, tmp_path, "set_parent_project", {"project_id": w.mine, "parent_project_id": ""}, [w.mine, w.mine2])
    assert detached.get("parent_project_id") in (None, "")


async def test_set_parent_project_refuses_to_reparent_an_out_of_scope_project(db, tmp_path):
    w = await _world(db)
    await _refused(db, tmp_path, "set_parent_project", {"project_id": w.theirs, "parent_project_id": w.mine}, [w.mine])


async def test_create_project_refuses_an_out_of_scope_parent(db, tmp_path):
    w = await _world(db)
    await _refused(db, tmp_path, "create_project", {"name": "planted-child", "parent_project_id": w.theirs}, [w.mine])
    assert await db_module.get_project_by_name(db, "planted-child") is None
    created = await _call(db, tmp_path, "create_project", {"name": "legit-child", "parent_project_id": w.mine}, [w.mine])
    assert created["parent_project_id"] == w.mine


async def test_any_star_project_id_argument_must_be_in_scope(db):
    w = await _world(db)
    await scope_guard.enforce_scoped_call("get_notes", {"project_id": w.mine}, db, [w.mine])
    for key in ("other_project_id", "target_project_id", "x_project_id"):
        with pytest.raises(ValueError, match=SCOPE_ERROR):
            await scope_guard.enforce_scoped_call("get_notes", {"project_id": w.mine, key: w.theirs}, db, [w.mine])
    with pytest.raises(ValueError, match=SCOPE_ERROR):
        await scope_guard.enforce_scoped_call("get_notes", {"other_project_name": "guard-theirs"}, db, [w.mine])
    # A malformed (non-string) id is refused rather than coerced.
    with pytest.raises(ValueError, match=SCOPE_ERROR):
        await scope_guard.enforce_scoped_call("get_notes", {"parent_project_id": [w.mine]}, db, [w.mine])


# ---------------------------------------------------------------------------
# 3. M3: listings that span every project, and cross-project file_path tools
# ---------------------------------------------------------------------------

_LISTING_EXTRA = {
    "list_hitl_requests": {},
    "get_file_claims": {"file_path": "scope/a.py"},
    "list_worktrees_pending_cleanup": {},
    "get_workspace_proposals": {},
}


def test_the_listing_test_covers_every_required_project_tool():
    assert set(_LISTING_EXTRA) == set(scope_guard.REQUIRE_PROJECT)


@pytest.mark.parametrize("tool", sorted(scope_guard.REQUIRE_PROJECT))
async def test_a_listing_needs_an_in_scope_project_for_a_scoped_caller(db, tmp_path, tool):
    w = await _world(db)
    base = _LISTING_EXTRA[tool]
    await _refused(db, tmp_path, tool, dict(base), [w.mine])                          # project omitted
    await _refused(db, tmp_path, tool, {**base, "project_id": ""}, [w.mine])          # blank
    await _refused(db, tmp_path, tool, {**base, "project_id": w.theirs}, [w.mine])    # out of scope
    await _call(db, tmp_path, tool, {**base, "project_id": w.mine}, [w.mine])         # fine
    await _call(db, tmp_path, tool, {**base, "project_name": "guard-mine"}, [w.mine])  # by name too


@pytest.mark.parametrize("tool", sorted(scope_guard.REQUIRE_PROJECT))
async def test_a_listing_without_a_project_is_unchanged_for_an_unscoped_caller(db, tmp_path, tool):
    w = await _world(db)
    await _call(db, tmp_path, tool, dict(_LISTING_EXTRA[tool]), None)


async def test_the_hitl_listing_only_shows_the_named_project(db, tmp_path):
    w = await _world(db)
    rows = await _call(db, tmp_path, "list_hitl_requests", {"project_id": w.mine, "status": "all"}, [w.mine])
    assert {r["id"] for r in rows} == {w.own["hitl"]}


@pytest.mark.parametrize("tool", sorted(scope_guard.DENY))
async def test_cross_project_file_path_tools_are_refused_for_a_scoped_caller(db, tmp_path, tool):
    """Keyed by file_path alone, so naming an in-scope project cannot narrow them."""
    w = await _world(db)
    args = {"file_path": "scope/a.py"}
    await _refused(db, tmp_path, tool, args, [w.mine])
    await _refused(db, tmp_path, tool, {**args, "project_id": w.mine}, [w.mine])
    await _call(db, tmp_path, tool, args, None)  # unchanged for an unscoped caller


# ---------------------------------------------------------------------------
# 4. M4: profile layers and capability profiles
# ---------------------------------------------------------------------------

async def _set_layer(db: Any, scope_type: str, scope_id: str, mode: str = "strict") -> None:
    await db_module.set_profile_layer(db, scope_type, scope_id, fields={"claim_verification_mode": mode})


async def test_get_profile_layer_for_a_project_needs_that_project_in_scope(db, tmp_path):
    w = await _world(db)
    await _set_layer(db, "project", w.theirs)
    await _set_layer(db, "project", w.mine)
    for scope_type in ("project", "Project", " PROJECT "):  # the contract lowercases and strips
        await _refused(db, tmp_path, "get_profile_layer", {"scope_type": scope_type, "scope_id": w.theirs}, [w.mine])
        await _refused(db, tmp_path, "get_profile_layer", {"scope_type": scope_type, "scope_id": f" {w.theirs} "}, [w.mine])
    own = await _call(db, tmp_path, "get_profile_layer", {"scope_type": "project", "scope_id": w.mine}, [w.mine])
    assert own["fields"]["claim_verification_mode"] == "strict"


async def test_save_and_reset_profile_layer_cannot_write_another_projects_layer(db, tmp_path):
    w = await _world(db)
    await _set_layer(db, "project", w.theirs, "strict")
    revision = (await db_module.get_profile_layer(db, "project", w.theirs))["revision"]
    await _refused(
        db, tmp_path, "save_profile_layer",
        {"scope_type": "project", "scope_id": w.theirs, "fields": {"claim_verification_mode": "off"}}, [w.mine],
    )
    await _refused(db, tmp_path, "reset_profile_layer", {"scope_type": "project", "scope_id": w.theirs}, [w.mine])
    layer = await db_module.get_profile_layer(db, "project", w.theirs)
    assert layer["revision"] == revision and layer["fields"]["claim_verification_mode"] == "strict"
    # Their own project still takes writes and resets.
    saved = await _call(
        db, tmp_path, "save_profile_layer",
        {"scope_type": "project", "scope_id": w.mine, "fields": {"claim_verification_mode": "strict"}}, [w.mine],
    )
    assert saved["fields"]["claim_verification_mode"] == "strict"
    await _call(db, tmp_path, "reset_profile_layer", {"scope_type": "project", "scope_id": w.mine}, [w.mine])


async def test_clone_profile_layer_checks_both_the_source_and_the_target(db, tmp_path):
    w = await _world(db)
    await _set_layer(db, "project", w.theirs)
    await _set_layer(db, "project", w.mine)
    scoped = [w.mine, w.mine2]
    base = {"source_scope_type": "project", "target_scope_type": "project"}
    await _refused(db, tmp_path, "clone_profile_layer", {**base, "source_scope_id": w.theirs, "target_scope_id": w.mine}, scoped)
    await _refused(db, tmp_path, "clone_profile_layer", {**base, "source_scope_id": w.mine, "target_scope_id": w.theirs}, scoped)
    assert (await db_module.get_profile_layer(db, "project", w.theirs))["revision"] == 1
    cloned = await _call(db, tmp_path, "clone_profile_layer", {**base, "source_scope_id": w.mine, "target_scope_id": w.mine2}, scoped)
    assert cloned["fields"]["claim_verification_mode"] == "strict"


async def test_capability_profile_tools_for_a_project_need_that_project_in_scope(db, tmp_path):
    w = await _world(db)
    await db_module.set_capability_profile(db, "project", w.theirs, disabled_capability_ids=["keep_me"])
    for tool, extra in (
        ("set_capability_profile", {"disabled_capability_ids": ["hijacked"]}),
        ("clear_capability_profile", {}),
    ):
        await _refused(db, tmp_path, tool, {"scope_type": "project", "scope_id": w.theirs, **extra}, [w.mine])
    kept = await db_module.get_capability_profile(db, "project", w.theirs)
    assert kept["disabled_capability_ids"] == ["keep_me"]
    ok = await _call(
        db, tmp_path, "set_capability_profile",
        {"scope_type": "project", "scope_id": w.mine, "disabled_capability_ids": ["mine_only"]}, [w.mine],
    )
    assert ok["disabled_capability_ids"] == ["mine_only"]
    await _call(db, tmp_path, "clear_capability_profile", {"scope_type": "project", "scope_id": w.mine}, [w.mine])


async def test_other_project_owned_profile_scopes_are_checked_through_their_object(db, tmp_path):
    w = await _world(db)
    scoped = [w.mine]
    # profile layers keyed by a session
    await _refused(db, tmp_path, "get_profile_layer", {"scope_type": "session", "scope_id": w.foreign["session"]}, scoped)
    await _call(db, tmp_path, "get_profile_layer", {"scope_type": "session", "scope_id": w.own["session"]}, scoped)
    await _refused(db, tmp_path, "get_profile_layer", {"scope_type": "session", "scope_id": "no-such-session"}, scoped)
    # capability profiles keyed by a sprint item / "<project>:<version>"
    for scope_type, foreign_id, own_id in (
        ("item", w.foreign["sprint_item"], w.own["sprint_item"]),
        ("sprint_version", f"{w.theirs}:v1", f"{w.mine}:v1"),
    ):
        await _refused(db, tmp_path, "clear_capability_profile", {"scope_type": scope_type, "scope_id": foreign_id}, scoped)
        await _call(db, tmp_path, "clear_capability_profile", {"scope_type": scope_type, "scope_id": own_id}, scoped)


async def test_tenant_level_profile_scopes_are_left_unchanged(db, tmp_path):
    """workspace / user / hosted_default layers are tenant policy, not project
    data -- an open product question, deliberately not touched here."""
    w = await _world(db)
    await _set_layer(db, "workspace", "singleton")
    got = await _call(db, tmp_path, "get_profile_layer", {"scope_type": "workspace", "scope_id": "singleton"}, [w.mine])
    assert got["fields"]["claim_verification_mode"] == "strict"
    await _call(db, tmp_path, "save_profile_layer", {"scope_type": "user", "scope_id": "someone", "fields": {}}, [w.mine])


async def test_list_profile_layers_only_returns_the_callers_projects(db, tmp_path):
    w = await _world(db)
    await _set_layer(db, "project", w.mine)
    await _set_layer(db, "project", w.theirs)
    await _set_layer(db, "session", w.own["session"])
    await _set_layer(db, "session", w.foreign["session"])
    await _set_layer(db, "workspace", "singleton")

    everything = await _call(db, tmp_path, "list_profile_layers", {}, None)
    assert {(r["scope_type"], r["scope_id"]) for r in everything} >= {
        ("project", w.mine), ("project", w.theirs),
        ("session", w.own["session"]), ("session", w.foreign["session"]),
    }
    visible = await _call(db, tmp_path, "list_profile_layers", {}, [w.mine])
    assert {(r["scope_type"], r["scope_id"]) for r in visible} == {
        ("project", w.mine), ("session", w.own["session"]), ("workspace", "singleton"),
    }
    only_projects = await _call(db, tmp_path, "list_profile_layers", {"scope_type": "project"}, [w.mine])
    assert [r["scope_id"] for r in only_projects] == [w.mine]
    assert await _call(db, tmp_path, "list_profile_layers", {"scope_type": "project"}, []) == []


async def test_the_result_filter_is_a_no_op_for_every_other_tool_and_for_unscoped_callers(db):
    rows = [{"scope_type": "project", "scope_id": "elsewhere"}]
    assert await scope_guard.filter_scoped_result("list_profile_layers", rows, db, None) is rows
    assert await scope_guard.filter_scoped_result("get_notes", rows, db, ["p"]) is rows
    assert await scope_guard.filter_scoped_result("list_profile_layers", {"error": "x"}, db, ["p"]) == {"error": "x"}


# ---------------------------------------------------------------------------
# 5. Batch entries carry their own session / profile scope
# ---------------------------------------------------------------------------

async def test_execute_batch_refuses_a_note_entry_for_another_projects_session(db, tmp_path):
    w = await _world(db)
    entries = [{"session_id": w.foreign["session"], "title": "planted", "body": "x"}]
    await _refused(
        db, tmp_path, "execute_batch",
        {"project_id": w.mine, "operation": "notes", "entries": entries,
         "mode": "all_or_nothing", "idempotency_key": "k1"}, [w.mine],
    )
    assert await db_module.get_session_notes(db, w.foreign["session"]) == []
    own = [{"session_id": w.own["session"], "title": "mine", "body": "x"}]
    result = await _call(
        db, tmp_path, "execute_batch",
        {"project_id": w.mine, "operation": "notes", "entries": own,
         "mode": "all_or_nothing", "idempotency_key": "k1b"}, [w.mine],
    )
    assert result.get("status") == "ok", result


async def test_batch_mutate_refuses_a_profile_layer_entry_for_another_project(db, tmp_path):
    w = await _world(db)
    entries = [{"kind": "profile_layer", "scope_type": "project", "scope_id": w.theirs,
                "fields": {"claim_verification_mode": "off"}}]
    await _refused(
        db, tmp_path, "batch_mutate",
        {"project_id": w.mine, "entries": entries, "mode": "all_or_nothing", "idempotency_key": "k2"}, [w.mine],
    )
    assert (await db_module.get_profile_layer(db, "project", w.theirs))["revision"] == 0
    own = [{"kind": "profile_layer", "scope_type": "project", "scope_id": w.mine,
            "fields": {"claim_verification_mode": "strict"}}]
    result = await _call(
        db, tmp_path, "batch_mutate",
        {"project_id": w.mine, "entries": own, "mode": "all_or_nothing", "idempotency_key": "k3"}, [w.mine],
    )
    assert result.get("status") == "ok", result


async def test_batch_read_refuses_a_profile_request_for_another_project(db, tmp_path):
    w = await _world(db)
    await _set_layer(db, "project", w.theirs)
    requests = [{"request_id": "r1", "adapter": "profile", "operation": "get_profile_layer",
                 "args": {"scope_type": "project", "scope_id": w.theirs}}]
    await _refused(db, tmp_path, "batch_read", {"project_id": w.mine, "requests": requests}, [w.mine])
    own = [{"request_id": "r1", "adapter": "profile", "operation": "get_profile_layer",
            "args": {"scope_type": "project", "scope_id": w.mine}}]
    result = await _call(db, tmp_path, "batch_read", {"project_id": w.mine, "requests": own}, [w.mine])
    assert result["results"][0]["status"] == "ok", result
    # The same request is untouched for an unscoped caller.
    again = await _call(db, tmp_path, "batch_read", {"project_id": w.mine, "requests": requests}, None)
    assert again["results"][0]["status"] == "ok", again


# ---------------------------------------------------------------------------
# 6. Session pointers on EVERY tool, not just the listed ones
# ---------------------------------------------------------------------------

async def test_a_foreign_session_pointer_is_refused_on_tools_outside_the_table(db, tmp_path):
    w = await _world(db)
    sid = w.foreign["session"]
    # snapshot_graph_metrics falls back to the SESSION's project when project_id is omitted.
    await _refused(db, tmp_path, "snapshot_graph_metrics", {"session_id": sid}, [w.mine])
    await _refused(db, tmp_path, "get_findings", {"project_id": w.mine, "session_id": sid}, [w.mine])
    await _refused(db, tmp_path, "request_hitl", {"project_id": w.mine, "session_id": sid, "question": "q"}, [w.mine])
    async with db.execute(
        "SELECT COUNT(*) AS n FROM session_graph_snapshots WHERE session_id = ?", (sid,)
    ) as cur:
        assert (await cur.fetchone())["n"] == 0


async def test_an_unknown_session_pointer_is_left_to_the_handler(db, tmp_path):
    """log_task and friends auto-register a caller-minted session id."""
    w = await _world(db)
    await scope_guard.enforce_scoped_call("log_task", {"project_id": w.mine, "session_id": "brand-new"}, db, [w.mine])


# ---------------------------------------------------------------------------
# 7. Invariants of the guard itself
# ---------------------------------------------------------------------------

class _ExplodingDb:
    """Any attribute access means the guard touched the database."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unscoped call touched the database ({name})")


@pytest.mark.parametrize("tool", sorted(scope_guard.guarded_tool_names()))
async def test_an_unscoped_caller_costs_no_database_access(tool):
    args = {"project_id": "p", "session_id": "s", "request_id": "r", "scope_type": "project", "scope_id": "x"}
    assert await scope_guard.enforce_scoped_call(tool, args, _ExplodingDb(), None) is None
    rows = [{"scope_type": "project", "scope_id": "x"}]
    assert await scope_guard.filter_scoped_result(tool, rows, _ExplodingDb(), None) is rows


async def test_an_empty_scope_is_still_a_scoped_caller(db, tmp_path):
    """[] must refuse everything project-owned, never read as 'unscoped'."""
    w = await _world(db)
    await _refused(db, tmp_path, "get_hitl_request", {"request_id": w.own["hitl"]}, [])
    await _refused(db, tmp_path, "list_hitl_requests", {}, [])
    await _refused(db, tmp_path, "get_profile_layer", {"scope_type": "project", "scope_id": w.mine}, [])
    await _refused(db, tmp_path, "get_notes", {"project_id": w.mine}, [])
    assert await db_module.get_hitl_request(db, w.own["hitl"]) is not None


async def test_a_resolver_error_fails_closed(db, monkeypatch):
    w = await _world(db)

    async def _boom(_db: Any, _id: str) -> Any:
        raise RuntimeError("auth database unavailable")

    monkeypatch.setitem(scope_guard._RESOLVERS, "hitl", _boom)
    with pytest.raises(RuntimeError, match="unavailable"):
        await scope_guard.enforce_scoped_call("get_hitl_request", {"request_id": w.own["hitl"]}, db, [w.mine])


async def test_a_refused_call_by_a_scoped_caller_never_writes_a_foreign_sessions_activity_feed(db, tmp_path, monkeypatch):
    """The dispatcher's error path records 'EXCEPTION ...' into the named
    executor session's feed; a scoped caller naming ANOTHER project's session
    must not be able to land a row there (a planner reads the feed as proof of life)."""
    w = await _world(db)
    own_sid, foreign_sid = w.own["session"], w.foreign["session"]
    monkeypatch.setattr(mcp_handler, "_EXECUTOR_SESSIONS", {own_sid, foreign_sid})

    async def _rpc(sid: str, scoped: "list[str] | None") -> "dict[str, Any]":
        return await mcp_handler._handle_mcp_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "get_hitl_request",
                        "arguments": {"request_id": "no-such-request", "session_id": sid}}},
            db, str(tmp_path), tenant=None, scoped_project_ids=scoped,
        )

    assert "error" in await _rpc(foreign_sid, [w.mine])
    assert await db_module.get_session_activity(db, foreign_sid) == []
    # The caller's OWN session still gets its failures recorded.
    assert "error" in await _rpc(own_sid, [w.mine])
    assert [a["tool_name"] for a in await db_module.get_session_activity(db, own_sid)] == ["get_hitl_request"]
    # And an unscoped caller is exactly as before (it may record on any session).
    assert "error" in await _rpc(foreign_sid, None)
    assert len(await db_module.get_session_activity(db, foreign_sid)) == 1


async def test_the_json_rpc_layer_gives_the_same_opaque_error_whichever_layer_refused(db, tmp_path):
    w = await _world(db)

    async def _rpc(name: str, arguments: "dict[str, Any]", scoped: "list[str] | None") -> "dict[str, Any]":
        return await mcp_handler._handle_mcp_request(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
            db, str(tmp_path), tenant=None, scoped_project_ids=scoped,
        )

    gate = await _rpc("get_notes", {"project_id": w.theirs}, [w.mine])               # pre-dispatch gate
    guard = await _rpc("merge_project", {"source_project_id": w.theirs, "target_project_id": w.mine}, [w.mine])
    by_object = await _rpc("get_hitl_request", {"request_id": w.foreign["hitl"]}, [w.mine])
    unknown = await _rpc("get_hitl_request", {"request_id": "no-such-request"}, [w.mine])
    messages = {r["error"]["message"] for r in (gate, guard, by_object, unknown)}
    assert messages == {"project is outside your access scope"}
    # In-scope calls and unscoped callers are unaffected.
    assert "error" not in await _rpc("get_hitl_request", {"request_id": w.own["hitl"]}, [w.mine])
    assert "error" not in await _rpc("get_hitl_request", {"request_id": w.foreign["hitl"]}, None)


# ---------------------------------------------------------------------------
# 8. The tables cannot rot
# ---------------------------------------------------------------------------

_TOOLS = {t["name"]: t for t in _MCP_TOOLS_LIST}


def _schema_args(tool: str) -> "set[str]":
    return set(((_TOOLS[tool].get("inputSchema") or {}).get("properties") or {}).keys())


def _names_missing_from_the_tool_list(names: "set[str] | frozenset[str]") -> "list[str]":
    return sorted(n for n in names if n not in _TOOLS)


def test_every_tool_named_in_the_tables_exists():
    assert _names_missing_from_the_tool_list(scope_guard.guarded_tool_names()) == []


def test_a_table_entry_for_a_tool_that_does_not_exist_is_caught(monkeypatch):
    """Negative control: the check above is not vacuous."""
    monkeypatch.setitem(scope_guard.OBJECT_ARGS, "no_such_tool", (scope_guard.ObjectArg("x_id", "hitl"),))
    monkeypatch.setattr(scope_guard, "REQUIRE_PROJECT", scope_guard.REQUIRE_PROJECT | {"also_no_such_tool"})
    assert _names_missing_from_the_tool_list(scope_guard.guarded_tool_names()) == ["also_no_such_tool", "no_such_tool"]


def test_every_argument_named_in_the_tables_is_in_that_tools_schema():
    missing = [(t, a) for t, a in scope_guard.schema_argument_refs() if a not in _schema_args(t)]
    assert missing == []


def test_every_object_kind_has_a_resolver():
    kinds = {ref.kind for refs in scope_guard.OBJECT_ARGS.values() for ref in refs}
    assert kinds <= set(scope_guard._RESOLVERS)


#: Tools with an id-bearing argument and no project_id/project_name in their
#: schema that are deliberately NOT object-checked, and why. Anything else with
#: such an argument must be listed in scope_guard or added here after review.
_EXEMPT_UNBOUND_ID_TOOLS = {
    "create_project": "parent_project_id is covered by the generic *_project_id rule (tested above)",
    "merge_project": "source/target_project_id are covered by the generic *_project_id rule (tested above)",
    "activate_profile_layer": "acts on the tenant-wide hosted_default layer only; no project-typed scope_id exists",
    "get_profile_layer_revisions": "reads hosted_default history only; any other scope_id returns []",
    "add_workspace_sprint_item": "workspace-level item; human_id is an identity label, not a project object",
    "update_workspace_sprint_item": "workspace-level sprint item, not a project object",
    "complete_workspace_sprint_item": "workspace-level sprint item, not a project object",
    "save_blog_post": "workspace blog post id, not project data",
    "zotero_search": "external Zotero library id, not a project object",
}

_ID_ARG = re.compile(r"(_id$|_ids$|^id$|^scope_|^session_[ab]$|worktree)")


def _unbound_id_tools() -> "set[str]":
    found = set()
    for name, tool in _TOOLS.items():
        props = _schema_args(name)
        if props & {"project_id", "project_name"}:
            continue
        if any(_ID_ARG.search(p) for p in props):
            found.add(name)
    return found


def test_every_tool_with_an_unbound_id_argument_is_guarded_or_exempted():
    unhandled = _unbound_id_tools() - scope_guard.guarded_tool_names() - set(_EXEMPT_UNBOUND_ID_TOOLS)
    assert not unhandled, (
        f"{sorted(unhandled)} take an id argument but no project_id/project_name; list them in "
        "meridian/mcp/scope_guard.py (so a scoped caller is checked) or exempt them here with a reason"
    )


def test_no_exemption_has_gone_stale():
    assert set(_EXEMPT_UNBOUND_ID_TOOLS) <= set(_TOOLS)
    assert not (set(_EXEMPT_UNBOUND_ID_TOOLS) & scope_guard.guarded_tool_names())
    assert set(_EXEMPT_UNBOUND_ID_TOOLS) <= _unbound_id_tools()
