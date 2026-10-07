"""Wave 2, pass 2 of pinned decision 6fe5210c -- what an independent verifier found
after the first MCP scope guard (tests/test_scope_guard_mcp.py).

* F-M1  ``prompts/get`` is a project-reading method too (the ``executor-goal``
        prompt renders a project's pending sprint items), and every other
        ``_handle_mcp_request`` method was swept for the same bug class.
* F-M2  lock / claim / lease results name the HOLDER session, and the holder may
        belong to a project outside the caller's scope (``file_locks`` is keyed by
        file path, not project).
* F-M3  a tool's OWN ``project_id`` does not bind a SECOND object id: probe each,
        guard the ones that leaked or mutated, exempt the ones the handler already
        binds (with a proof), and make the completeness test cover every
        (tool, argument) pair instead of only tools without a project_id.
* F-M4  session keys / the comma-separated ``session_ids`` string the first pass
        never exercised, and the one control that needs a stubbed tunnel.
* F-C1  (pass 3) "bound" must hold on EVERY code path: update_sprint_item's no-op
        patch and its in_progress pre-check read the item by id alone, behind a probe
        that only ever sent one call shape. Every bound pair is now probed in its
        minimal shape and with each optional argument alone, against a foreign object
        in each lifecycle state, with an own-project control per probe.
* F-C2  (pass 3) the caption-link store primitives bind to a document; the docx
        conflict element lists keep the ids in-scope sessions hold.

Owners, workspace-wide members and self-hosted callers pass
``scoped_project_ids=None`` and must see no change at all.
"""
from __future__ import annotations

import ast
import contextlib
import json
import os
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch

import pytest

import meridian.server  # noqa: F401 -- load the server before handler to avoid its import cycle
from meridian import db as db_module
from meridian.mcp import handler as mcp_handler
from meridian.mcp import scope_guard
from meridian.mcp_tools import _MCP_TOOLS_LIST

SCOPE_ERROR = "outside your access scope"
SECRET = "ZZ-SECRET-PENDING-ITEM"


# ---------------------------------------------------------------------------
# A world: two in-scope sessions, one foreign project with its own session
# ---------------------------------------------------------------------------

@dataclass
class W:
    mine: str
    theirs: str
    s_mine: str       # the caller's own session
    s_mine2: str      # a second session of the caller's own (in-scope) project
    s_theirs: str     # a session of the foreign project
    name_mine2: str
    name_theirs: str


async def _world(db: Any, tag: str = "") -> W:
    mine = (await db_module.create_project(db, f"p2-mine{tag}"))["id"]
    theirs = (await db_module.create_project(db, f"p2-theirs{tag}"))["id"]
    name_mine2, name_theirs = "mine-worker-two", "foreign-worker-xyz"
    return W(
        mine=mine, theirs=theirs,
        s_mine=(await db_module.register_session(db, mine, "mine-worker"))["id"],
        s_mine2=(await db_module.register_session(db, mine, name_mine2))["id"],
        s_theirs=(await db_module.register_session(db, theirs, name_theirs))["id"],
        name_mine2=name_mine2, name_theirs=name_theirs,
    )


async def _call(db: Any, tmp_path: Any, name: str, args: "dict[str, Any]", scoped: "list[str] | None", *, tenant: Any = None) -> Any:
    return await mcp_handler._dispatch_mcp_tool(
        name, args, db, str(tmp_path), tenant=tenant, scoped_project_ids=scoped,
    )


async def _refused(db: Any, tmp_path: Any, name: str, args: "dict[str, Any]", scoped: "list[str] | None") -> None:
    with pytest.raises(ValueError, match=SCOPE_ERROR):
        await _call(db, tmp_path, name, args, scoped)


async def _rpc(db: Any, tmp_path: Any, method: str, params: "dict[str, Any] | None", scoped: "list[str] | None", **kw: Any) -> "dict[str, Any]":
    body: "dict[str, Any]" = {"jsonrpc": "2.0", "id": 11, "method": method}
    if params is not None:
        body["params"] = params
    return await mcp_handler._handle_mcp_request(
        body, db, str(tmp_path), scoped_project_ids=scoped, **kw,
    )


class _ExplodingDb:
    """Any attribute access means the guard touched the database."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unscoped call touched the database ({name})")


# ===========================================================================
# F-M1. prompts/get and the other JSON-RPC methods
# ===========================================================================

_PROMPTS = ("executor-goal", "start-executor", "daily-standup", "planning-session-start", "hotfix-loop")


async def _prompt(db: Any, tmp_path: Any, name: str, arguments: Any, scoped: "list[str] | None") -> "dict[str, Any]":
    return await _rpc(db, tmp_path, "prompts/get", {"name": name, "arguments": arguments}, scoped)


async def test_prompts_get_executor_goal_does_not_render_a_foreign_projects_items(db, tmp_path):
    """The verifier's reproduction: a scoped caller asked for the executor-goal
    prompt of ANOTHER project and got that project's pending item titles."""
    w = await _world(db)
    await db_module.add_sprint_item(db, w.theirs, "v1", f"{SECRET} refactor the billing gateway")
    await db_module.add_sprint_item(db, w.mine, "v1", "own pending item about parsers")
    scoped = [w.mine]

    leaked = await _prompt(db, tmp_path, "executor-goal", {"project_id": w.theirs}, None)
    assert SECRET in json.dumps(leaked)  # what an owner (unscoped) may read, and what used to leak

    for arguments in ({"project_id": w.theirs}, {"project_name": "p2-theirs"}):
        refused = await _prompt(db, tmp_path, "executor-goal", arguments, scoped)
        assert refused["error"]["message"] == "project is outside your access scope"
        assert "result" not in refused and SECRET not in json.dumps(refused)

    own = await _prompt(db, tmp_path, "executor-goal", {"project_id": w.mine}, scoped)
    assert "own pending item about parsers" in json.dumps(own["result"])
    own_by_name = await _prompt(db, tmp_path, "executor-goal", {"project_name": "p2-mine"}, scoped)
    assert "own pending item about parsers" in json.dumps(own_by_name["result"])


@pytest.mark.parametrize("prompt", _PROMPTS)
async def test_every_prompt_refuses_a_foreign_project_argument(db, tmp_path, prompt):
    w = await _world(db)
    scoped = [w.mine]
    for arguments in (
        {"project_id": w.theirs},
        {"project_name": "p2-theirs"},
        {"project_id": w.mine, "project_name": "p2-theirs"},      # in-scope id next to a foreign name
        {"project_id": w.theirs, "project_name": "p2-mine"},      # foreign id next to an in-scope name
        {"other_project_id": w.theirs},                           # any *_project_id argument
        {"other_project_name": "p2-theirs"},                      # any *_project_name argument
    ):
        refused = await _prompt(db, tmp_path, prompt, arguments, scoped)
        assert refused["error"]["message"] == "project is outside your access scope", (prompt, arguments)
    # An EMPTY scope is still a scoped caller.
    assert "error" in await _prompt(db, tmp_path, prompt, {"project_id": w.mine}, [])
    # Own project, no project at all, and unscoped callers are unaffected.
    assert "result" in await _prompt(db, tmp_path, prompt, {"project_id": w.mine}, scoped)
    assert "result" in await _prompt(db, tmp_path, prompt, {}, scoped)
    assert "result" in await _prompt(db, tmp_path, prompt, {"project_id": w.theirs}, None)
    assert "result" in await _prompt(db, tmp_path, prompt, {"project_name": "p2-theirs"}, None)


async def test_prompts_get_with_a_malformed_arguments_object_is_refused_for_a_scoped_caller(db, tmp_path):
    w = await _world(db)
    for arguments in (["project_id", w.theirs], "project_id", 7):
        refused = await _prompt(db, tmp_path, "executor-goal", arguments, [w.mine])
        assert refused["error"]["message"] == "project is outside your access scope"


async def test_an_unknown_prompt_name_still_reports_unknown_prompt_to_a_scoped_caller(db, tmp_path):
    w = await _world(db)
    resp = await _prompt(db, tmp_path, "no-such-prompt", {"project_id": w.mine}, [w.mine])
    assert resp["error"]["code"] == -32602 and "unknown prompt" in resp["error"]["message"]


async def test_the_prompt_refusal_has_the_exact_error_shape_the_tool_call_gate_produces(db, tmp_path):
    """Same JSON-RPC code, message and (absent) data as the tools/call pre-gate,
    so a caller cannot tell which method's gate refused it."""
    w = await _world(db)
    prompt_err = (await _prompt(db, tmp_path, "executor-goal", {"project_id": w.theirs}, [w.mine]))["error"]
    tool_err = (await _rpc(
        db, tmp_path, "tools/call", {"name": "get_notes", "arguments": {"project_id": w.theirs}}, [w.mine],
    ))["error"]
    assert prompt_err == tool_err == {"code": -32603, "message": "project is outside your access scope"}


async def test_prompts_get_costs_an_unscoped_caller_no_database_access_in_the_guard():
    assert await scope_guard.enforce_scoped_prompt("executor-goal", {"project_id": "p"}, _ExplodingDb(), None) is None
    assert await scope_guard.enforce_scoped_prompt("executor-goal", ["not", "a", "dict"], _ExplodingDb(), None) is None


async def test_a_failing_scope_lookup_fails_closed_instead_of_rendering_the_prompt(db, tmp_path, monkeypatch):
    w = await _world(db)

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("auth database unavailable")

    monkeypatch.setattr(db_module, "get_project_by_name", _boom)
    resp = await _prompt(db, tmp_path, "executor-goal", {"project_name": "p2-mine"}, [w.mine])
    assert resp["error"]["code"] == -32603 and "result" not in resp


# --- every other method of _handle_mcp_request --------------------------------

#: Answered without reading any project (static manifests / protocol handshakes).
_PROJECT_FREE_METHODS = (
    ("initialize", {}), ("notifications/initialized", {}), ("ping", {}),
    ("tools/list", {}), ("prompts/list", {}),
)
#: MCP methods this server does NOT implement. A scoped caller must get the same
#: "method not found" an owner gets, with no project data. If you implement one of
#: these, gate its project-bearing arguments / URI through scope_guard (a
#: resources/read of meridian://project/<id>/... is exactly the bug class) and move
#: it out of this list in the same change.
_UNIMPLEMENTED_METHODS = (
    "resources/list", "resources/read", "resources/templates/list", "resources/subscribe",
    "resources/unsubscribe", "completion/complete", "logging/setLevel", "roots/list",
    "sampling/createMessage", "elicitation/create", "tasks/list", "tasks/get", "tasks/result",
)


@pytest.mark.parametrize("method", [m for m, _ in _PROJECT_FREE_METHODS])
async def test_project_free_methods_return_no_project_data_to_a_scoped_caller(db, tmp_path, method):
    w = await _world(db)
    await db_module.add_sprint_item(db, w.theirs, "v1", f"{SECRET} item")
    resp = await _rpc(db, tmp_path, method, {}, [w.mine])
    blob = json.dumps(resp)
    assert "error" not in resp
    assert w.theirs not in blob and "p2-theirs" not in blob and SECRET not in blob


@pytest.mark.parametrize("method", _UNIMPLEMENTED_METHODS)
async def test_unimplemented_methods_give_a_scoped_caller_nothing_but_method_not_found(db, tmp_path, method):
    w = await _world(db)
    await db_module.add_sprint_item(db, w.theirs, "v1", f"{SECRET} item")
    params = {
        "uri": f"meridian://project/{w.theirs}/sprint-items", "name": "executor-goal",
        "ref": {"type": "ref/prompt", "name": "executor-goal"},
        "argument": {"name": "project_id", "value": w.theirs},
        "arguments": {"project_id": w.theirs},
    }
    for scoped in ([w.mine], None):
        resp = await _rpc(db, tmp_path, method, params, scoped)
        assert resp["error"]["code"] == -32601, (method, scoped)
        assert "result" not in resp and SECRET not in json.dumps(resp)


async def test_github_tools_refuse_a_foreign_project_at_the_pre_dispatch_gate(db, tmp_path):
    """tools/call for a GitHub tool (the tenant branch) never reaches
    _dispatch_mcp_tool; the pre-dispatch gate on project_id is its only guard."""
    w = await _world(db)
    tenant = {"id": "tenant-1"}
    for tool in ("read_file", "patch_file", "search_code", "get_commits"):
        resp = await _rpc(
            db, tmp_path, "tools/call",
            {"name": tool, "arguments": {"project_id": w.theirs, "path": "README.md"}}, [w.mine], tenant=tenant,
        )
        assert resp["error"]["message"] == "project is outside your access scope", tool
    # An in-scope project is let through to the tool (no GitHub PAT -> its own error).
    own = await _rpc(
        db, tmp_path, "tools/call",
        {"name": "read_file", "arguments": {"project_id": w.mine, "path": "README.md"}}, [w.mine], tenant=tenant,
    )
    assert "error" not in own or own["error"]["message"] != "project is outside your access scope"


# ===========================================================================
# F-M2. lock / claim / lease holders that belong to another project
# ===========================================================================

_PY_SRC = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"


def _blob(result: Any) -> str:
    return json.dumps(result, default=str)


def _assert_no_identity(result: Any, w: W) -> None:
    blob = _blob(result)
    assert w.s_theirs not in blob, blob
    assert w.name_theirs not in blob, blob


async def _hold(db: Any, tmp_path: Any, session: str, tool: str, **args: Any) -> Any:
    """Take a lock/claim as ``session`` the way its owner would (unscoped)."""
    return await _call(db, tmp_path, tool, {"session_id": session, **args}, None)


async def test_get_file_claims_does_not_name_a_foreign_lock_holder(db, tmp_path):
    """The verifier's reproduction: file_lock came back with the foreign
    session_id AND session_name (the lock table is keyed by path, not project)."""
    w = await _world(db)
    scoped = [w.mine]
    await _hold(db, tmp_path, w.s_theirs, "claim_file", file_path="shared/x.py")
    await _hold(db, tmp_path, w.s_theirs, "claim_file", file_path="shared/r.py", mode="read")
    await _hold(db, tmp_path, w.s_theirs, "claim_file", file_path="shared/s.py", symbol="alpha", content=_PY_SRC)

    # What the owner (unscoped) sees -- and what used to leak.
    raw = await _call(db, tmp_path, "get_file_claims", {"project_id": w.mine, "file_path": "shared/x.py"}, None)
    assert raw["file_lock"]["session_id"] == w.s_theirs and raw["file_lock"]["session_name"] == w.name_theirs

    redacted = await _call(db, tmp_path, "get_file_claims", {"project_id": w.mine, "file_path": "shared/x.py"}, scoped)
    _assert_no_identity(redacted, w)
    # The caller still learns that the file IS locked, and until when.
    assert redacted["file_lock"] and redacted["file_lock"]["expires_at"] == raw["file_lock"]["expires_at"]
    assert redacted["file_lock"]["session_id"] is None and redacted["file_lock"]["session_name"] is None
    assert redacted["holder_redacted"] is True

    readers = await _call(db, tmp_path, "get_file_claims", {"project_id": w.mine, "file_path": "shared/r.py"}, scoped)
    _assert_no_identity(readers, w)
    assert len(readers["read_claims"]) == 1 and readers["read_claims"][0]["session_id"] is None

    symbols = await _call(db, tmp_path, "get_file_claims", {"project_id": w.mine, "file_path": "shared/s.py"}, scoped)
    _assert_no_identity(symbols, w)
    assert len(symbols["symbol_claims"]) == 1
    # What the foreign session is editing is as private as who it is.
    assert symbols["symbol_claims"][0]["symbol_name"] is None and "alpha" not in _blob(symbols["symbol_claims"])


async def test_an_in_scope_holder_is_not_redacted_and_an_unscoped_caller_sees_everything(db, tmp_path):
    w = await _world(db)
    await _hold(db, tmp_path, w.s_mine2, "claim_file", file_path="shared/own.py")
    scoped = await _call(db, tmp_path, "get_file_claims", {"project_id": w.mine, "file_path": "shared/own.py"}, [w.mine])
    assert scoped["file_lock"]["session_id"] == w.s_mine2 and scoped["file_lock"]["session_name"] == w.name_mine2
    assert "holder_redacted" not in scoped
    conflict = await _call(db, tmp_path, "claim_file", {"session_id": w.s_mine, "file_path": "shared/own.py"}, [w.mine])
    assert conflict["claimed"] is False and conflict["holder_session_id"] == w.s_mine2
    assert "holder_redacted" not in conflict

    await _hold(db, tmp_path, w.s_theirs, "claim_file", file_path="shared/their.py")
    owner_view = await _call(db, tmp_path, "claim_file", {"session_id": w.s_mine, "file_path": "shared/their.py"}, None)
    assert owner_view["holder_session_id"] == w.s_theirs and "holder_redacted" not in owner_view


async def test_claim_file_conflict_is_still_reported_without_naming_the_holder(db, tmp_path):
    w = await _world(db)
    scoped = [w.mine]
    await _hold(db, tmp_path, w.s_theirs, "claim_file", file_path="shared/x.py")
    conflict = await _call(db, tmp_path, "claim_file", {"session_id": w.s_mine, "file_path": "shared/x.py"}, scoped)
    assert conflict["claimed"] is False                       # still a conflict ...
    assert conflict["expires_at"] and conflict["file_path"] == "shared/x.py"   # ... with its timing ...
    assert conflict["holder_session_id"] is None and conflict["holder_redacted"] is True   # ... minus the holder
    assert conflict["session_id"] == w.s_mine                  # the caller keeps its OWN identity
    _assert_no_identity(conflict, w)
    # The same path as a read claim: write-locked by a foreign writer.
    read = await _call(db, tmp_path, "claim_file", {"session_id": w.s_mine, "file_path": "shared/x.py", "mode": "read"}, scoped)
    assert read["claimed"] is False and read["reason"] == "write_locked" and read["holder_session_id"] is None
    _assert_no_identity(read, w)


async def test_claim_file_read_conflicts_and_reader_lists_do_not_name_foreign_sessions(db, tmp_path):
    w = await _world(db)
    scoped = [w.mine]
    await _hold(db, tmp_path, w.s_theirs, "claim_file", file_path="shared/r.py", mode="read")
    blocked = await _call(db, tmp_path, "claim_file", {"session_id": w.s_mine, "file_path": "shared/r.py"}, scoped)
    assert blocked["claimed"] is False and blocked["reason"] == "read_locked"
    assert blocked["read_claims"] == [] and "1 reader(s)" in blocked["message"]
    _assert_no_identity(blocked, w)
    # A successful read claim lists the other readers: the caller itself stays, the foreign one goes.
    joined = await _call(db, tmp_path, "claim_file", {"session_id": w.s_mine, "file_path": "shared/r.py", "mode": "read"}, scoped)
    assert joined["claimed"] is True and joined["readers"] == [w.s_mine] and joined["reader_count"] == 2
    _assert_no_identity(joined, w)


async def test_claim_file_symbol_conflicts_do_not_name_foreign_sessions_or_their_symbols(db, tmp_path):
    w = await _world(db)
    scoped = [w.mine]
    await _hold(db, tmp_path, w.s_theirs, "claim_file", file_path="shared/s.py", symbol="alpha", content=_PY_SRC)
    # whole-file claim over a foreign symbol claim
    whole = await _call(db, tmp_path, "claim_file", {"session_id": w.s_mine, "file_path": "shared/s.py"}, scoped)
    assert whole["claimed"] is False and whole["reason"] == "symbol_locked" and whole["holder_session_id"] is None
    assert len(whole["symbol_claims"]) == 1 and whole["symbol_claims"][0]["symbol_name"] is None
    _assert_no_identity(whole, w)
    assert "alpha" not in _blob(whole["symbol_claims"])
    # symbol claim over the same symbol: the message used to say "claimed by session <name>"
    sym = await _call(
        db, tmp_path, "claim_file",
        {"session_id": w.s_mine, "file_path": "shared/s.py", "symbol": "alpha", "content": _PY_SRC}, scoped,
    )
    assert sym["claimed"] is False and sym["reason"] == "symbol_conflict" and sym["holder_session_id"] is None
    assert sym["conflicts"][0]["holder_session_id"] is None and sym["conflicts"][0]["symbol_name"] is None
    assert "claimed by session <redacted>" in sym["message"]
    _assert_no_identity(sym, w)
    assert sym["safe_to_claim"] == ["beta"]    # derived from the caller's own content: kept
    # a symbol claim under a foreign whole-file lock
    await _hold(db, tmp_path, w.s_theirs, "claim_file", file_path="shared/w.py")
    locked = await _call(
        db, tmp_path, "claim_file",
        {"session_id": w.s_mine, "file_path": "shared/w.py", "symbol": "alpha", "content": _PY_SRC}, scoped,
    )
    assert locked["claimed"] is False and locked["reason"] == "file_locked" and locked["holder_session_id"] is None
    _assert_no_identity(locked, w)


async def test_docx_region_and_lease_conflicts_do_not_name_foreign_sessions(db, tmp_path):
    w = await _world(db)
    scoped = [w.mine]
    path = "shared/a.docx"
    await _hold(db, tmp_path, w.s_theirs, "claim_docx_region", file_path=path, element_id="e1")
    await _hold(db, tmp_path, w.s_theirs, "claim_docx_region", file_path=path, element_id="e9")

    element = await _call(db, tmp_path, "claim_docx_region", {"session_id": w.s_mine, "file_path": path, "element_id": "e1"}, scoped)
    assert element["claimed"] is False and element["reason"] == "element_conflict"
    assert element["conflicts"][0]["holder_session_id"] is None and element["conflicts"][0]["element_id"] is None
    assert "other_claimed_elements" not in element           # element ids merged across every other holder
    _assert_no_identity(element, w)
    assert "e9" not in _blob(element)

    lease = await _call(db, tmp_path, "acquire_docx_document_lease", {"session_id": w.s_mine, "file_path": path}, scoped)
    assert lease["leased"] is False and lease["reason"] == "region_claims_active"
    assert lease["holder_session_id"] is None and lease["holder_session_name"] is None
    assert "conflicting_elements" not in lease
    _assert_no_identity(lease, w)

    rows = await _call(db, tmp_path, "get_docx_region_claims", {"file_path": path}, scoped)
    assert len(rows["claims"]) == 2                          # the claims are still there ...
    assert all(c["session_id"] is None and c["session_name"] is None and c["element_id"] is None for c in rows["claims"])
    _assert_no_identity(rows, w)
    owner_rows = await _call(db, tmp_path, "get_docx_region_claims", {"file_path": path}, None)
    assert {c["session_id"] for c in owner_rows["claims"]} == {w.s_theirs}

    # whole-file lock, then a whole-document lease held by a foreign session
    await _hold(db, tmp_path, w.s_theirs, "claim_file", file_path="shared/b.docx")
    file_locked = await _call(db, tmp_path, "claim_docx_region", {"session_id": w.s_mine, "file_path": "shared/b.docx", "element_id": "e1"}, scoped)
    assert file_locked["reason"] == "file_locked" and file_locked["holder_session_id"] is None
    _assert_no_identity(file_locked, w)
    await _hold(db, tmp_path, w.s_theirs, "acquire_docx_document_lease", file_path="shared/c.docx")
    leased = await _call(db, tmp_path, "claim_docx_region", {"session_id": w.s_mine, "file_path": "shared/c.docx", "element_id": "e1"}, scoped)
    assert leased["claimed"] is False and leased["reason"] == "document_leased" and leased["holder_session_id"] is None
    _assert_no_identity(leased, w)
    held = await _call(db, tmp_path, "get_docx_document_lease", {"file_path": "shared/c.docx"}, scoped)
    assert held["lease"] and held["lease"]["session_id"] is None and held["lease"]["session_name"] is None
    _assert_no_identity(held, w)
    owner_lease = await _call(db, tmp_path, "get_docx_document_lease", {"file_path": "shared/c.docx"}, None)
    assert owner_lease["lease"]["session_id"] == w.s_theirs


async def test_claim_sprint_item_and_parallelizable_groups_do_not_name_the_holder_of_a_resource(db, tmp_path):
    """Found while sweeping the same bug class: a sprint item whose declared file
    is locked by a foreign session reported that session's id in its conflict row
    and message (claim_sprint_item) and in resource_blocked (get_parallelizable_groups)."""
    w = await _world(db)
    scoped = [w.mine]
    await _hold(db, tmp_path, w.s_theirs, "claim_file", file_path="shared/x.py")
    item = await db_module.add_sprint_item(db, w.mine, "v1", "needs the shared file", touches_resources=["file:shared/x.py"])

    claim = await _call(db, tmp_path, "claim_sprint_item", {"project_id": w.mine, "session_id": w.s_mine, "item_id": item["id"]}, scoped)
    assert claim["ok"] is False and claim["error"] == "RESOURCE_LOCKED"       # still blocked, still says why
    assert claim["conflicts"][0]["resource"] == "file:shared/x.py"
    assert claim["conflicts"][0]["conflict"]["holder_session_id"] is None
    assert "<redacted>" in claim["message"]
    _assert_no_identity(claim, w)

    groups = await _call(db, tmp_path, "get_parallelizable_groups", {"project_id": w.mine}, scoped)
    assert groups["resource_blocked"] and groups["resource_blocked"][0]["holder_session_id"] is None
    _assert_no_identity(groups, w)
    owner_groups = await _call(db, tmp_path, "get_parallelizable_groups", {"project_id": w.mine}, None)
    assert owner_groups["resource_blocked"][0]["holder_session_id"] == w.s_theirs


async def test_update_paragraph_write_conflicts_do_not_name_the_blocking_foreign_session(db, tmp_path):
    """The docx write gate answers {"holder": <session id>, "message": "... session <id> ..."}
    before the document is even looked up, so any path a foreign session holds leaks."""
    w = await _world(db)
    scoped = [w.mine]
    write = {"project_id": w.mine, "session_id": w.s_mine, "new_text": "edited", "para_id": "e1"}

    await _hold(db, tmp_path, w.s_theirs, "claim_docx_region", file_path="shared/a.docx", element_id="e1")
    element = await _call(db, tmp_path, "update_paragraph", {**write, "doc": "shared/a.docx"}, scoped)
    assert element["error"] == "docx_region_conflict" and element["reason"] == "element_locked"   # still blocked, still says why
    assert element["holder"] is None and element["holder_redacted"] is True
    _assert_no_identity(element, w)

    await _hold(db, tmp_path, w.s_theirs, "claim_file", file_path="shared/b.docx")
    locked = await _call(db, tmp_path, "update_paragraph", {**write, "doc": "shared/b.docx"}, scoped)
    assert locked["reason"] == "file_locked" and locked["holder"] is None
    _assert_no_identity(locked, w)

    await _hold(db, tmp_path, w.s_theirs, "acquire_docx_document_lease", file_path="shared/c.docx")
    leased = await _call(db, tmp_path, "update_paragraph", {**write, "doc": "shared/c.docx"}, scoped)
    assert leased["reason"] == "document_leased" and leased["holder"] is None
    _assert_no_identity(leased, w)

    # An owner still sees who holds it, and an in-scope holder is not hidden.
    owner = await _call(db, tmp_path, "update_paragraph", {**write, "doc": "shared/b.docx"}, None)
    assert owner["holder"] == w.s_theirs
    await _hold(db, tmp_path, w.s_mine2, "claim_file", file_path="shared/d.docx")
    own_project = await _call(db, tmp_path, "update_paragraph", {**write, "doc": "shared/d.docx"}, scoped)
    assert own_project["holder"] == w.s_mine2 and "holder_redacted" not in own_project


async def test_claim_parallel_batch_conflict_does_not_name_the_holder_of_a_resource(db, tmp_path):
    """The batch claim answers BATCH_RESOURCE_CONFLICT with the holder at the top level
    and in its message when a declared file is locked by a foreign session."""
    w = await _world(db)
    await _hold(db, tmp_path, w.s_theirs, "claim_file", file_path="shared/x.py")
    item = await db_module.add_sprint_item(db, w.mine, "v1", "needs the shared file", touches_resources=["file:shared/x.py"])
    await _call(
        db, tmp_path, "add_sprint_item_pointer",
        {"project_id": w.mine, "sprint_item_id": item["id"], "source_type": "doc",
         "targets": [{"uri": "file:shared/x.py", "selector": {"type": "range", "start_line": 1, "end_line": 2}}]}, None,
    )
    batch = {"project_id": w.mine, "session_id": w.s_mine, "item_ids": [item["id"]]}
    conflict = await _call(db, tmp_path, "claim_parallel_batch", batch, [w.mine])
    assert conflict["ok"] is False and conflict["error"] == "BATCH_RESOURCE_CONFLICT"   # still blocked, still says why
    assert conflict["resource"] == "file:shared/x.py" and conflict["holder_session_id"] is None
    assert "locked by another live session (<redacted>)" in conflict["message"]
    _assert_no_identity(conflict, w)


async def test_the_handler_keeps_the_callers_own_session_visible_in_a_redacted_result(db, tmp_path, monkeypatch):
    """The dispatcher hands the call's args to the redactor, so a session id the caller
    minted itself (unknown to the db, but named and scope-checked) is not blanked."""
    w = await _world(db)

    async def _fake_sprint_tools(name: str, args: "dict[str, Any]", *_rest: Any) -> Any:
        if name != "claim_sprint_item":
            return mcp_handler._MISS
        return {"ok": False, "error": "RESOURCE_LOCKED", "session_id": args["session_id"], "holder_session_id": w.s_theirs}

    monkeypatch.setattr(mcp_handler, "_handle_sprint_tools", _fake_sprint_tools)
    own_item = (await db_module.add_sprint_item(db, w.mine, "v1", "claim target for the redaction check"))["id"]
    result = await _call(
        db, tmp_path, "claim_sprint_item",
        {"project_id": w.mine, "session_id": "caller-minted-session", "item_id": own_item}, [w.mine],
    )
    assert result["session_id"] == "caller-minted-session"
    assert result["holder_session_id"] is None and result["holder_redacted"] is True


# --- the redactor itself: shapes that are awkward to reproduce end to end -------

async def test_the_redactor_keeps_the_callers_own_session_even_when_the_db_does_not_know_it(db):
    w = await _world(db)
    result = {"session_id": "caller-minted-id", "holder_session_id": w.s_theirs, "claimed": False}
    out = await scope_guard.filter_scoped_result(
        "claim_sprint_item", result, db, [w.mine], args={"session_id": "caller-minted-id"},
    )
    assert out["session_id"] == "caller-minted-id" and out["holder_session_id"] is None
    # Without having named it, an unknown session is treated as foreign (fail closed).
    out = await scope_guard.filter_scoped_result("claim_sprint_item", result, db, [w.mine], args={})
    assert out["session_id"] is None


async def test_the_redactor_scrubs_names_only_from_free_text_and_ids_from_everywhere(db):
    w = await _world(db)
    result = {
        "claimed": False,
        "holder_session_id": w.s_theirs,
        "holder_session_name": "backend",
        "title": "refactor the backend service",                       # an ordinary word: left alone
        "message": f"locked by backend ({w.s_theirs}); backend-2 and mybackend are other things; "
                   f"a nameless holder shows as {w.s_theirs[:8]}",
        "details": [{"note": f"see {w.s_theirs}"}],
    }
    out = await scope_guard.filter_scoped_result("claim_parallel_batch", result, db, [w.mine], args={})
    assert out["title"] == "refactor the backend service"
    assert out["message"] == (
        "locked by <redacted> (<redacted>); backend-2 and mybackend are other things; "
        "a nameless holder shows as <redacted>"
    )
    assert out["details"] == [{"note": "see <redacted>"}]
    assert out["holder_redacted"] is True and out["holder_session_name"] is None


async def test_the_redactor_is_a_no_op_for_other_tools_unscoped_callers_and_clean_results(db):
    w = await _world(db)
    result = {"holder_session_id": w.s_theirs}
    assert await scope_guard.filter_scoped_result("claim_file", result, _ExplodingDb(), None, args={}) is result
    assert await scope_guard.filter_scoped_result("get_notes", result, _ExplodingDb(), [w.mine], args={}) is result
    clean = {"claimed": True, "session_id": w.s_mine, "file_path": "x.py"}
    assert await scope_guard.filter_scoped_result("claim_file", clean, db, [w.mine], args={"session_id": w.s_mine}) is clean
    # Non-container results pass through untouched.
    assert await scope_guard.filter_scoped_result("claim_file", None, db, [w.mine], args={}) is None
    assert await scope_guard.filter_scoped_result("claim_file", "text", db, [w.mine], args={}) == "text"


# ===========================================================================
# F-M3. a tool's own project_id does not bind a SECOND object id
#
# Every test below is a hole that was PROBED first (a scratch battery of ~90
# tool/argument pairs run as a scoped caller with a foreign id under an in-scope
# project_id): the call either returned the foreign object or mutated it.
# ===========================================================================

async def _count(db: Any, table: str) -> int:
    async with db.execute(f"SELECT COUNT(*) AS n FROM {table}") as cur:
        return (await cur.fetchone())["n"]


async def _changes(db: Any) -> "int | None":
    """Rows written since the connection opened (SQLite); None where unsupported."""
    if hasattr(db, "_pool"):  # Postgres: no total_changes(); the leak asserts still apply
        return None
    async with db.execute("SELECT total_changes() AS n") as cur:
        return (await cur.fetchone())["n"]


async def _item(db: Any, project_id: str, title: str) -> str:
    return (await db_module.add_sprint_item(db, project_id, "v1", title))["id"]


async def test_start_wave_run_refuses_a_foreign_item_and_writes_nothing(db, tmp_path):
    """Verifier mcp#3: a foreign id in item_ids wrote a wave-run child row pointing
    at the foreign sprint item."""
    w = await _world(db)
    foreign = await _item(db, w.theirs, "refactor the foreign billing gateway")
    own = await _item(db, w.mine, "tidy the parser tests")
    scoped = [w.mine]
    before = (await _count(db, "wave_runs"), await _count(db, "wave_run_children"), await _changes(db))
    for item_ids in ([foreign], [own, foreign], f"{own},{foreign}"):
        await _refused(db, tmp_path, "start_wave_run", {"project_id": w.mine, "item_ids": item_ids}, scoped)
    assert (await _count(db, "wave_runs"), await _count(db, "wave_run_children"), await _changes(db)) == before
    ok = await _call(db, tmp_path, "start_wave_run", {"project_id": w.mine, "item_ids": [own]}, scoped)
    assert ok["wave_run_id"] and await _count(db, "wave_run_children") == before[1] + 1
    # Unscoped callers are unchanged.
    anything = await _call(db, tmp_path, "start_wave_run", {"project_id": w.theirs, "item_ids": [foreign]}, None)
    assert anything["wave_run_id"]


async def test_record_handoff_correction_cannot_invalidate_another_projects_handoff(db, tmp_path):
    """regenerate=true INVALIDATES the source handoff: with a foreign source_handoff_id
    it rewrote the foreign project's handoff rows."""
    w = await _world(db)
    foreign = await db_module.record_handoff(db, w.theirs, "full", "foreign handoff body", w.s_theirs)
    before = (await db_module.get_handoff(db, foreign["id"]), await _count(db, "handoffs"), await _changes(db))
    for regenerate in (False, True):
        await _refused(
            db, tmp_path, "record_handoff_correction",
            {"project_id": w.mine, "session_id": w.s_mine, "source_handoff_id": foreign["id"],
             "blocker_classification": "other", "regenerate": regenerate}, [w.mine],
        )
    assert (await db_module.get_handoff(db, foreign["id"]), await _count(db, "handoffs"), await _changes(db)) == before
    own = await db_module.record_handoff(db, w.mine, "full", "own handoff body", w.s_mine)
    ok = await _call(
        db, tmp_path, "record_handoff_correction",
        {"project_id": w.mine, "source_handoff_id": own["id"], "blocker_classification": "other"}, [w.mine],
    )
    assert ok["correction"]["source_handoff_id"] == own["id"]


async def test_proposal_to_handoff_is_guarded_although_tools_list_does_not_advertise_it(db, tmp_path):
    """proposal_to_handoff (dispatchable, NOT in tools/list) loaded a proposal by
    tenant only and wrote update rows and pointers against it."""
    w = await _world(db)
    foreign = (await db_module.add_workspace_proposal(db, "foreign proposal", "body", project_id=w.theirs))["id"]
    own = (await db_module.add_workspace_proposal(db, "own proposal", "body", project_id=w.mine))["id"]
    args = {"project_id": w.mine, "session_id": w.s_mine, "skip_handoff": True, "items": [{"title": "decomposed item one"}]}
    before = (await _count(db, "sprint_items"), await _changes(db))
    # Through the real JSON-RPC entry point: tools/call does not check tools/list.
    resp = await _rpc(db, tmp_path, "tools/call", {"name": "proposal_to_handoff", "arguments": {**args, "proposal_id": foreign}}, [w.mine])
    assert resp["error"]["message"] == "project is outside your access scope"
    assert (await _count(db, "sprint_items"), await _changes(db)) == before
    ok = await _rpc(db, tmp_path, "tools/call", {"name": "proposal_to_handoff", "arguments": {**args, "proposal_id": own}}, [w.mine])
    assert "error" not in ok and json.loads(ok["result"]["content"][0]["text"])["created_item_ids"]


async def test_claim_parallel_batch_cannot_assign_an_item_to_a_foreign_worker_session(db, tmp_path):
    """claim_parallel_batch (not advertised either): item_sessions maps item -> worker
    session; a foreign session would end up holding the caller's claim and its locks."""
    w = await _world(db)
    own = await _item(db, w.mine, "wire up the importer")
    scoped = [w.mine]
    for mapping in ({own: w.s_theirs}, {own: w.s_mine, "other": w.s_theirs}, ["not", "a", "mapping"]):
        await _refused(
            db, tmp_path, "claim_parallel_batch",
            {"project_id": w.mine, "session_id": w.s_mine, "item_ids": [own], "item_sessions": mapping}, scoped,
        )
    assert (await db_module.get_sprint_item(db, own))["status"] == "pending"
    # Own sessions (and an empty mapping) are let through to the db function.
    for mapping in ({own: w.s_mine2}, {}, None):
        result = await _call(
            db, tmp_path, "claim_parallel_batch",
            {"project_id": w.mine, "session_id": w.s_mine, "item_ids": [own], "item_sessions": mapping}, scoped,
        )
        assert isinstance(result, dict) and result.get("error") != "ITEM_NOT_FOUND"
        if result.get("ok"):
            break
    # The same call by an unscoped owner still accepts a foreign worker.
    await db_module.add_sprint_item(db, w.mine, "v1", "second distinct task about dashboards")
    other = await _item(db, w.mine, "polish the changelog wording")
    unscoped = await _call(
        db, tmp_path, "claim_parallel_batch",
        {"project_id": w.mine, "session_id": w.s_mine, "item_ids": [other], "item_sessions": {other: w.s_theirs}}, None,
    )
    assert isinstance(unscoped, dict)


async def test_a_foreign_task_cannot_be_linked_as_evidence_or_to_a_finding(db, tmp_path):
    """get_task() is not project-bound: a foreign task id counted as completion
    evidence for an own item (and, via the checkpoint summary join, put the item's
    title into the FOREIGN session's summary)."""
    w = await _world(db)
    foreign_task = (await db_module.log_task(db, w.s_theirs, w.theirs, "foreign work"))["id"]
    own_task = (await db_module.log_task(db, w.s_mine, w.mine, "own work"))["id"]
    item = await _item(db, w.mine, "finish the importer")
    scoped = [w.mine]
    before = (await _count(db, "session_findings"), await _changes(db))
    await _refused(
        db, tmp_path, "complete_sprint_item",
        {"project_id": w.mine, "session_id": w.s_mine, "item_id": item, "task_id": foreign_task, "notes": "done"}, scoped,
    )
    await _refused(db, tmp_path, "store_finding", {"project_id": w.mine, "content": "c", "task_id": foreign_task}, scoped)
    assert (await db_module.get_sprint_item(db, item))["status"] == "pending"
    assert (await _count(db, "session_findings"), await _changes(db)) == (before[0], before[1])
    done = await _call(
        db, tmp_path, "complete_sprint_item",
        {"project_id": w.mine, "session_id": w.s_mine, "item_id": item, "task_id": own_task, "notes": "done"}, scoped,
    )
    assert done["status"] == "done"


async def test_a_foreign_task_link_is_what_wrote_into_the_foreign_sessions_summary(db, tmp_path):
    """Why the task link matters: checkpoint builds a session's "Shipped: ..." summary
    from every done item whose task_id is one of THAT session's tasks, whatever
    project the item is in. The unscoped control shows the effect; a scoped caller
    can no longer cause it."""
    w = await _world(db)
    foreign_task = (await db_module.log_task(db, w.s_theirs, w.theirs, "foreign work"))["id"]
    planted = await _item(db, w.mine, "PLANTED-TITLE shows up elsewhere")
    done = await _call(
        db, tmp_path, "complete_sprint_item",
        {"project_id": w.mine, "session_id": w.s_mine, "item_id": planted, "task_id": foreign_task, "notes": "n"}, None,
    )
    assert done["status"] == "done"            # unscoped control: allowed, as before
    await _call(db, tmp_path, "checkpoint", {"project_id": w.theirs, "session_id": w.s_theirs}, None)
    async with db.execute("SELECT session_summary FROM sessions WHERE id = ?", (w.s_theirs,)) as cur:
        assert "PLANTED-TITLE" in ((await cur.fetchone())["session_summary"] or "")

    other = await _item(db, w.mine, "second planted attempt at another place")
    await _refused(
        db, tmp_path, "complete_sprint_item",
        {"project_id": w.mine, "session_id": w.s_mine, "item_id": other, "task_id": foreign_task, "notes": "n"}, [w.mine],
    )


async def test_start_remote_task_refuses_a_foreign_sprint_item(db, tmp_path):
    w = await _world(db)
    foreign = await _item(db, w.theirs, "foreign remote benchmark")
    before = (await _count(db, "remote_tasks"), await _changes(db))
    await _refused(
        db, tmp_path, "start_remote_task",
        {"project_id": w.mine, "session_id": w.s_mine, "host": "h", "command": "echo hi", "sprint_item_id": foreign}, [w.mine],
    )
    assert (await _count(db, "remote_tasks"), await _changes(db)) == before


async def test_start_experiment_run_refuses_a_foreign_worktree_but_accepts_an_unknown_label(db, tmp_path):
    from meridian.db import experiments as experiments_db

    w = await _world(db)
    experiment = (await experiments_db.create_experiment(db, w.mine, w.s_mine, name="own experiment"))["id"]
    foreign_wt = (await db_module.register_worktree(db, w.s_theirs, w.theirs, "branch-t", "wt-t"))["id"]
    own_wt = (await db_module.register_worktree(db, w.s_mine, w.mine, "branch-m", "wt-m"))["id"]
    base = {"project_id": w.mine, "session_id": w.s_mine, "experiment_id": experiment}
    before = (await _count(db, "experiment_runs"), await _changes(db))
    await _refused(db, tmp_path, "start_experiment_run", {**base, "worktree_id": foreign_wt}, [w.mine])
    assert (await _count(db, "experiment_runs"), await _changes(db)) == before
    for label in (own_wt, "a-label-nobody-registered"):
        run = await _call(db, tmp_path, "start_experiment_run", {**base, "worktree_id": label}, [w.mine])
        assert run["run"]["worktree_id"] == label


# --- document-store objects: the figure / table id must be the document's own -----

async def _doc_world(db: Any, tmp_path: Any, monkeypatch: Any) -> "tuple[W, Any, dict[str, Any]]":
    """Two projects with stored documents in the (separate) document-structure store."""
    from meridian import doc_store

    sidecar = str(tmp_path / "pass2_doc_structure.db")
    monkeypatch.setenv("MERIDIAN_DOC_STORE_URL", sidecar)
    doc_store._reset_doc_store_cache()
    w = await _world(db)
    store = await doc_store.open_doc_store_for(
        plan=None, hosted=False, data_dir=str(tmp_path), tenant_pg_url=None, override_url=sidecar,
    )
    docs: "dict[str, Any]" = {}
    for key, project, source in (
        ("mine", w.mine, "mine.docx"), ("mine2", w.mine, "mine-appendix.docx"), ("theirs", w.theirs, "theirs.docx"),
    ):
        doc = await store.put_document(project, "docx", [], source=source)
        docs[key] = {
            "id": doc["id"],
            "source": source,
            "figure": (await store.add_figure(doc["id"], None, caption=f"Figure 1: {key} setup"))["figure"]["id"],
            "table": (await store.add_table(doc["id"], None, caption=f"Table 1: {key} results"))["table"]["id"],
        }
    return w, store, docs


@pytest.mark.parametrize("tool,id_arg,object_key,getter", [
    ("link_figure_caption", "figure_id", "figure", "get_figures"),
    ("link_table_caption", "table_id", "table", "get_tables"),
])
async def test_caption_links_are_bound_to_the_callers_own_document(db, tmp_path, monkeypatch, tool, id_arg, object_key, getter):
    """set_figure_caption_link / set_table_caption_link take a bare id and update
    ANY stored row: an own document plus a foreign figure id rewrote and returned
    the foreign row."""
    from meridian import doc_store

    try:
        w, store, docs = await _doc_world(db, tmp_path, monkeypatch)
        scoped = [w.mine]

        async def _caption(doc_key: str) -> "str | None":
            rows = await getattr(store, getter)(docs[doc_key]["id"])
            return rows[0].get("caption_element_id")

        def _args(doc_key: str, object_id: str, link: str) -> "dict[str, Any]":
            return {"project_id": w.mine, "doc": docs[doc_key]["source"], id_arg: object_id, "caption_element_id": link}

        # a foreign figure under the caller's own document ...
        await _refused(db, tmp_path, tool, _args("mine", docs["theirs"][object_key], "el-hijack"), scoped)
        # ... and another document of the SAME (in-scope) project: not that document's object either
        await _refused(db, tmp_path, tool, _args("mine", docs["mine2"][object_key], "el-wrong-doc"), scoped)
        assert await _caption("theirs") is None and await _caption("mine2") is None
        # The caller's own object works, and is returned.
        ok = await _call(db, tmp_path, tool, _args("mine", docs["mine"][object_key], "el-own"), scoped)
        assert ok[object_key]["caption_element_id"] == "el-own" and await _caption("mine") == "el-own"
        # A document the project does not have is the handler's own error; nothing is touched.
        missing = await _call(db, tmp_path, tool, {**_args("mine", docs["theirs"][object_key], "el-x"), "doc": "theirs.docx"}, scoped)
        assert "error" in missing and await _caption("theirs") is None
        # An unscoped caller still links its own document's objects ...
        owner = await _call(db, tmp_path, tool, _args("mine", docs["mine"][object_key], "el-owner"), None)
        assert owner[object_key]["caption_element_id"] == "el-owner" and await _caption("mine") == "el-owner"
        # ... but is bound to the named document too since pass 3 (F-C2): the store primitive no
        # longer takes a bare id, so another document's object is "not found" for everyone.
        for other in ("theirs", "mine2"):
            stray = await _call(db, tmp_path, tool, _args("mine", docs[other][object_key], "el-stray"), None)
            assert "error" in stray and "no doc_" in stray["error"] and await _caption(other) is None
    finally:
        await doc_store.close_all_doc_stores()


async def test_a_doc_object_check_fails_closed_without_a_document_store(db):
    w = await _world(db)
    args = {"project_id": w.mine, "doc": "mine.docx", "figure_id": "f1", "caption_element_id": "c"}
    with pytest.raises(ValueError, match=SCOPE_ERROR):       # no factory at all
        await scope_guard.enforce_scoped_call("link_figure_caption", args, db, [w.mine])

    async def _no_store() -> Any:
        return None

    with pytest.raises(ValueError, match=SCOPE_ERROR):       # the store could not be opened
        await scope_guard.enforce_scoped_call("link_figure_caption", args, db, [w.mine], doc_store_factory=_no_store)
    # Blank ids / missing doc arguments are left to the handler and never open a store.
    for blank in ({**args, "figure_id": ""}, {**args, "doc": ""}, {"project_id": w.mine}):
        await scope_guard.enforce_scoped_call("link_figure_caption", blank, db, [w.mine])
    # An unscoped caller never needs one.
    await scope_guard.enforce_scoped_call("link_figure_caption", args, _ExplodingDb(), None)


async def test_citation_edges_of_a_foreign_document_id_are_empty_for_a_scoped_caller(db, tmp_path, monkeypatch):
    """get_citation_edges(document_id=...) joins doc_documents.project_id = ?, so a
    foreign document id under the caller's own project_id matches nothing (this is
    why the argument is exempted rather than guarded)."""
    from meridian import doc_store

    try:
        w, store, docs = await _doc_world(db, tmp_path, monkeypatch)
        citation = {"ordinal": 1, "level": 0, "kind": "citation", "text": "[1]", "ref": "smith2020"}
        theirs = await store.put_document(w.theirs, "docx", [citation], source="cited.docx")
        owner = await _call(db, tmp_path, "get_citation_edges", {"project_id": w.theirs, "document_id": theirs["id"]}, None)
        assert owner["markers"], "control: the owner of the project sees the marker"
        scoped = await _call(db, tmp_path, "get_citation_edges", {"project_id": w.mine, "document_id": theirs["id"]}, [w.mine])
        assert scoped["markers"] == []
    finally:
        await doc_store.close_all_doc_stores()


# --- tools the dispatcher routes but tools/list does not advertise -------------------

_SOURCE_NAME = re.compile(r'name == "([a-z][a-z0-9_]+)"')
_SOURCE_NAME_GROUP = re.compile(r"name in \(([^)]*)\)")
_SOURCE_NAME_QUOTED = re.compile(r'"([a-z][a-z0-9_]+)"')
_SOURCE_DISPATCH_TABLE = re.compile(r'^\s+"([a-z][a-z0-9_]+)": handle_[a-z0-9_]+,', re.M)


def _dispatchable_tool_names() -> "set[str]":
    """Every tool name the dispatcher routes, scraped from the handler sources
    (``if name == "x"``, ``name in ("x", ...)`` and the ``"x": handle_x`` tables).
    tools/call does not check tools/list, so an unadvertised name is callable."""
    import glob
    import os

    root = os.path.dirname(os.path.abspath(mcp_handler.__file__))
    names: "set[str]" = set()
    for path in [os.path.join(root, "handler.py")] + sorted(glob.glob(os.path.join(root, "handlers", "*.py"))):
        with open(path, encoding="utf-8", errors="ignore") as fh:
            source = fh.read()
        names |= set(_SOURCE_NAME.findall(source))
        names |= set(_SOURCE_DISPATCH_TABLE.findall(source))
        for group in _SOURCE_NAME_GROUP.findall(source):
            names |= set(_SOURCE_NAME_QUOTED.findall(group))
    return names


#: Names the scraper finds that tools/list does not advertise, and the review
#: verdict for each. A NEW name here fails the test below until someone reviews it.
_REVIEWED_UNLISTED: "dict[str, str]" = {
    "claim_parallel_batch": "GUARDED: scope_guard.UNLISTED_TOOLS (item_sessions values are session pointers)",
    "proposal_to_handoff": "GUARDED: scope_guard.OBJECT_ARGS (proposal_id)",
    "check_embedded_staleness": "project_id + doc; binds figure_id / table_id to the document itself (store.get_figures)",
    "audit_figure_table_provenance": "project_id + doc only; walks that document's own figures and tables",
    **{name: "GitHub-repo tool: takes project_id only (generic project rule + pre-dispatch gate)" for name in (
        "create_issue", "get_commit", "get_commits", "get_issue", "get_workflow_run_logs", "get_workflow_runs",
        "git_diff", "issue_write", "list_branches", "list_files", "list_issues", "patch_file", "read_file",
        "search_code", "search_commits", "trigger_workflow",
    )},
    **{name: "not a tool: an advisory label inside complete_sprint_item's result" for name in (
        "board_change", "ci", "completion_support", "github_issue", "unclaimed_files",
    )},
}


def test_every_dispatchable_tool_that_tools_list_hides_has_been_reviewed():
    listed = {t["name"] for t in _MCP_TOOLS_LIST}
    scraped = _dispatchable_tool_names()
    # The scraper really sees the dispatcher (a negative control for the check below).
    assert {"claim_file", "proposal_to_handoff", "claim_parallel_batch", "get_hitl_request"} <= scraped
    unreviewed = sorted((scraped - listed) - set(_REVIEWED_UNLISTED))
    assert not unreviewed, (
        f"{unreviewed} are routed by the dispatcher but not advertised in tools/list, so the schema scan "
        "cannot see their arguments. Review them (project_id? a second object id? a session pointer?), "
        "guard them in meridian/mcp/scope_guard.py and list them in scope_guard.UNLISTED_TOOLS, or record "
        "why they are safe in _REVIEWED_UNLISTED."
    )
    stale = sorted(set(_REVIEWED_UNLISTED) - (scraped - listed))
    assert not stale, f"{stale} are now advertised (or gone): drop them from _REVIEWED_UNLISTED"


def test_the_unlisted_tools_the_guard_names_are_really_unlisted_and_really_dispatchable():
    listed = {t["name"] for t in _MCP_TOOLS_LIST}
    assert not (scope_guard.UNLISTED_TOOLS & listed), "advertised now: move the entries into the normal tables"
    assert scope_guard.UNLISTED_TOOLS <= _dispatchable_tool_names()


# ===========================================================================
# F-M3. the completeness test, generalised
#
# Before: only tools WITHOUT a project_id/project_name in their schema were checked,
# so a tool with its own project_id plus a second object id (start_wave_run.item_ids,
# get_research_run.run_id, the paper-contract tools ...) was invisible to it. Now every
# (tool, argument) pair that looks like an id must be guarded by a table entry, covered
# by a generic rule, or exempted below WITH A REASON and a category:
#
#   bound   the handler passes the call's own project_id into a project-bound lookup;
#           a foreign id finds nothing. Backed by a proof (the tests further down).
#   inert   a label / filter / stored value that nothing dereferences across projects.
#   label   an identity label of a person, not an object of a project.
#   tenant  tenant-level state that belongs to no project (open product question,
#           see the scope_guard module docstring).
# ===========================================================================

_TOOLS = {t["name"]: t for t in _MCP_TOOLS_LIST}
_ID_ARG = re.compile(r"(_id$|_ids$|^id$|^scope_|^session_[ab]$|worktree|^depends_on$|^affected$)")


def _schema_args(tool: str) -> "set[str]":
    return set(((_TOOLS[tool].get("inputSchema") or {}).get("properties") or {}).keys())


def _is_generic_key(arg: str) -> bool:
    """Covered on every tool by a generic rule: any project id argument or any
    session pointer."""
    return arg == "project_id" or arg.endswith("_project_id") or arg in scope_guard.generic_rule_keys()


def _id_pairs() -> "set[tuple[str, str]]":
    """Every (tool, argument) that looks like an id and is not covered generically
    -- for ALL tools, whether or not they also take a project_id."""
    return {
        (tool, arg)
        for tool in _TOOLS
        for arg in _schema_args(tool)
        if _ID_ARG.search(arg) and not _is_generic_key(arg)
    }


def _old_unbound_id_tools() -> "set[str]":
    """The first pass's definition, kept only to show what it missed."""
    return {
        tool for tool in _TOOLS
        if not (_schema_args(tool) & {"project_id", "project_name"})
        and any(_ID_ARG.search(arg) for arg in _schema_args(tool))
    }


_EXEMPT_ID_ARGS: "dict[tuple[str, str], tuple[str, str]]" = {}


def _exempt(category: str, reason: str, *pairs: "tuple[str, str]") -> None:
    for pair in pairs:
        assert pair not in _EXEMPT_ID_ARGS, f"{pair} is exempted twice"
        _EXEMPT_ID_ARGS[pair] = (category, reason)


_exempt(
    "bound", "run id / experiment id looked up with the call's project_id (db.experiments / db.research_runs: 'not found in this project')",
    ("get_research_run", "run_id"), ("complete_research_run", "run_id"), ("promote_research_run", "run_id"),
    ("get_experiment", "experiment_id"), ("get_experiment_events", "experiment_id"), ("get_experiment_events", "run_id"),
    ("get_experiment_run", "run_id"), ("list_experiment_runs", "experiment_id"), ("start_experiment_run", "experiment_id"),
    ("start_experiment_run", "pivot_parent_run_id"), ("complete_experiment_run", "run_id"),
    ("promote_experiment_run", "run_id"), ("record_experiment_event", "experiment_id"),
    ("record_experiment_event", "run_id"), ("register_run_artifact", "run_id"),
)
_exempt(
    "bound", "external job / remote task id looked up with the call's project_id ('not found in this project')",
    ("get_external_job", "job_id"), ("update_external_job", "job_id"), ("complete_external_job", "job_id"),
    ("get_remote_task_status", "job_id"),
)
_exempt(
    "bound", "paper contract / revision / derivative / recovery id looked up with the call's project_id (db.paper_contract, db.docx_derivatives, db.session_recovery)",
    ("get_paper_contract", "contract_id"), ("get_current_paper_contract_content", "contract_id"),
    ("list_paper_contract_revisions", "contract_id"), ("create_paper_contract_revision", "contract_id"),
    ("approve_paper_contract_revision", "revision_id"), ("verify_docx_diff", "derivative_id"),
    ("promote_docx_candidate", "derivative_id"), ("get_session_recovery", "recovery_id"),
)
_exempt(
    "bound", "watchlist query / custom hook id looked up inside the call's project (project notes / project hooks)",
    ("run_watchlist_query", "watchlist_id"), ("delete_watchlist_query", "watchlist_id"),
    ("update_custom_hook", "hook_id"), ("delete_custom_hook", "hook_id"),
)
_exempt(
    "bound", "sprint item / pointer id resolved within the call's project (sprint-item db functions take project_id: 'sprint item not found')",
    ("get_sprint_item_pointers", "sprint_item_id"), ("add_sprint_item_pointer", "sprint_item_id"),
    ("delete_sprint_item_pointer", "pointer_id"), ("relocate_sprint_item_pointer", "pointer_id"),
    ("resolve_sprint_item_pointers", "sprint_item_id"),
    ("complete_sprint_item", "item_id"), ("release_sprint_item_claim", "item_id"),
    ("transfer_sprint_item_claim", "item_id"), ("split_sprint_item", "item_id"), ("merge_sprint_items", "item_ids"),
    ("add_subtask", "parent_id"), ("link_manual_github_issue", "item_id"), ("reconcile_stale_claims", "item_ids"),
    ("get_proposal_gates", "sprint_item_id"), ("get_effective_capability_profile", "sprint_item_id"),
)
_exempt(
    "bound", "proposal gate id looked up with the call's project_id ('not found in project')",
    ("resolve_proposal_gate", "gate_id"), ("reopen_proposal_gate", "gate_id"),
)
_exempt(
    "bound", "generate_handoff validates both id lists against the project and REJECTS a foreign id (force_include_rejected / HANDOFF_SELECTION_BLOCKED); only the caller's own input is echoed",
    ("generate_handoff", "selected_item_ids"), ("generate_handoff", "force_include_ids"),
)
_exempt(
    "bound", "an override approval is spent through gate_override.consume_gate_override_approval(project_id, ...), which answers 'not found in this project' for another project's HITL",
    ("complete_sprint_item", "override_hitl_id"), ("complete_sprint_item", "completion_override_hitl_id"),
    ("complete_sprint_item", "foreign_claim_override_hitl_id"), ("update_sprint_item", "override_hitl_id"),
    ("complete_wave_gate", "override_hitl_id"),
)
_exempt(
    "bound", "_wave_gate_evidence_from_run rejects a verification run that belongs to a different project",
    ("complete_wave_gate", "verification_run_id"),
)
_exempt(
    "bound", "ai_log events are read with 'WHERE project_id = ?' AND-ed with these filters (db.ai_log.search_events / export_events)",
    ("export_ai_log", "correlation_id"), ("export_ai_log", "parent_event_id"), ("search_ai_log", "tenant_id"),
    ("search_ai_log", "correlation_id"), ("search_ai_log", "parent_event_id"), ("search_ai_log", "actor_id"),
)
_exempt(
    "bound", "doc_documents.project_id = ? joins the document id (doc store); a foreign document id matches nothing",
    ("get_citation_edges", "document_id"),
)
_exempt(
    "inert", "a stored value on the caller's own row that nothing dereferences across projects (read paths treat a foreign id as missing, efea329f)",
    ("add_sprint_item", "depends_on"), ("update_sprint_item", "depends_on"),
)
_exempt(
    "inert", "stored on the caller's own gate row; blocking_gates_for_sprint_item reads only the gates of the project it is asked about",
    ("add_proposal_gate", "affected"),
)
_exempt(
    "inert", "an opaque grouping label on the caller's own proposal; lineage tools take proposal ids, which ARE guarded",
    ("add_proposal", "family_id"),
)
_exempt(
    "inert", "resolved only among the in_progress items held under the caller's own (scope-checked) session",
    ("claim_file", "item_id"),
)
_exempt(
    "inert", "a label for a region of a document path (the claim tables are keyed by path; conflicts on it are the feature)",
    ("claim_docx_region", "element_id"), ("release_docx_region_claims", "element_id"),
)
_exempt(
    "inert", "stored as a label on the caller's own document's figure / table, after the document binding above",
    ("link_figure_caption", "caption_element_id"), ("link_table_caption", "caption_element_id"),
    ("index_table", "paired_figure_id"),
)
_exempt(
    "inert", "an id inside the caller's own stored document (the doc store resolves it through project_id + doc)",
    ("insert_equation", "para_id"), ("update_paragraph", "para_id"), ("find_symbol_usages", "symbol_or_equation_id"),
    ("link_flag_to_section", "element_id"), ("get_flag_drift", "element_id"),
)
_exempt(
    "inert", "an opaque merge-manifest key (wave_id, file_path); db.docx_merge never dereferences the wave_runs table",
    ("update_paragraph", "wave_run_id"),
)
_exempt(
    "inert", "a provider's or client's own opaque reference, stored on the caller's own row",
    ("register_external_job", "external_id"), ("register_session_recovery", "local_ref_id"),
    ("start_experiment_run", "repository_id"), ("start_research_run", "repository_id"),
)
_exempt(
    "inert", "opaque strings stored in the event row's JSON; never dereferenced",
    ("record_experiment_event", "artifact_ids"),
)
_exempt(
    "inert", "not an id: a boolean flag / a list of capability manifest labels",
    ("start_research_run", "is_isolated_worktree"), ("set_capability_profile", "disabled_capability_ids"),
)
_exempt(
    "label", "an identity label of a person, not an object of a project",
    ("add_sprint_item", "human_id"), ("update_sprint_item", "human_id"), ("register_session", "human_id"),
    ("start_session", "human_id"), ("create_paper_contract", "created_by_human_id"),
    ("approve_paper_contract_revision", "approved_by_human_id"),
)
_exempt(
    "tenant", "tenant-level profile layer (hosted_default / workspace / user): no project-typed id exists",
    ("activate_profile_layer", "scope_id"), ("get_profile_layer_revisions", "scope_id"),
    ("get_effective_profile", "user_scope_id"), ("get_effective_profile", "workspace_scope_id"),
    ("get_effective_profile", "hosted_default_scope_id"), ("get_effective_capability_profile", "user_scope_id"),
    ("get_effective_capability_profile", "workspace_scope_id"),
)
_exempt(
    "tenant", "the listing is narrowed by scope_guard.filter_scoped_result (list_profile_layers)",
    ("list_profile_layers", "scope_type"),
)
_exempt(
    "tenant", "workspace-level state (notes, sprint items, blog posts) or an external library; a PROJECT note id is refused by the handler",
    ("move_workspace_note_to_project", "note_id"), ("update_workspace_sprint_item", "item_id"),
    ("complete_workspace_sprint_item", "item_id"), ("add_workspace_sprint_item", "human_id"),
    ("update_workspace_sprint_item", "human_id"), ("save_blog_post", "id"), ("zotero_search", "library_id"),
)


def test_every_id_argument_of_every_tool_is_guarded_or_exempted():
    covered = scope_guard.covered_argument_pairs()
    unhandled = sorted(_id_pairs() - covered - set(_EXEMPT_ID_ARGS))
    assert not unhandled, (
        f"{unhandled} look like ids and are neither checked by meridian/mcp/scope_guard.py nor exempted. "
        "Probe each with a foreign id under an in-scope project_id: if it leaks or mutates, add an "
        "OBJECT_ARGS entry; if the handler binds it to the project, exempt it here as 'bound' with a proof."
    )


def test_no_exemption_has_gone_stale_or_is_also_guarded():
    pairs = _id_pairs()
    covered = scope_guard.covered_argument_pairs()
    assert set(_EXEMPT_ID_ARGS) <= pairs, sorted(set(_EXEMPT_ID_ARGS) - pairs)
    assert not (set(_EXEMPT_ID_ARGS) & covered), sorted(set(_EXEMPT_ID_ARGS) & covered)
    assert {category for category, _ in _EXEMPT_ID_ARGS.values()} == {"bound", "inert", "label", "tenant"}
    assert all(reason.strip() for _, reason in _EXEMPT_ID_ARGS.values())


def test_the_completeness_test_is_not_vacuous():
    pairs = _id_pairs()
    # Tools with their OWN project_id plus a second object id are inside the net now ...
    assert {("start_wave_run", "item_ids"), ("get_research_run", "run_id"), ("get_experiment", "experiment_id"),
            ("get_paper_contract", "contract_id"), ("run_watchlist_query", "watchlist_id"),
            ("verify_docx_diff", "derivative_id"), ("add_sprint_item_pointer", "sprint_item_id"),
            ("get_hitl_request", "request_id")} <= pairs
    # ... and the first pass's definition could not see most of them.
    old = _old_unbound_id_tools()
    assert "start_wave_run" not in old and "get_research_run" not in old and "get_hitl_request" in old
    # Remove a guard and the check notices it.
    covered = scope_guard.covered_argument_pairs() - {("start_wave_run", "item_ids")}
    assert ("start_wave_run", "item_ids") in (pairs - covered - set(_EXEMPT_ID_ARGS))
    # A brand-new unguarded id argument is flagged too.
    assert ("some_new_tool", "other_thing_id") in ((pairs | {("some_new_tool", "other_thing_id")}) - covered - set(_EXEMPT_ID_ARGS))


# ===========================================================================
# F-M3. proof for every exemption that says "bound"
#
# Each probe calls the tool as a scoped caller with the caller's OWN project_id and
# a foreign object id, and asserts that nothing of the foreign object comes back and
# nothing is written. Without these, "the handler already binds it" would be a claim
# in a comment; the guard's table deliberately does not check these arguments.
# ===========================================================================

async def _foreign(db: Any, tmp_path: Any, w: W, *, marker: str = SECRET) -> SimpleNamespace:
    """One foreign project's objects of every kind the 'bound' tools take. ``marker`` is the text every
    one of them carries (the control world of the shape probes uses another one, so a tool that
    legitimately answers with the CALLER's own data is not mistaken for a leak)."""
    async def mk(tool: str, **args: Any) -> Any:
        return await _call(db, tmp_path, tool, args, None)

    b = SimpleNamespace()
    b.item = await _item(db, w.theirs, f"{marker} rotate the production keys")
    # (a similar title would be deduplicated into the first item; the marker rides in the notes)
    b.item2 = (await db_module.add_sprint_item(db, w.theirs, "v1", "migrate the billing tables", notes=marker))["id"]
    b.own_item = await _item(db, w.mine, "own tidy-up of the parser tests")
    exp = await mk("create_experiment", project_id=w.theirs, session_id=w.s_theirs, name=f"{marker} experiment", hypothesis=marker)
    b.exp = exp["experiment"]["id"]
    b.own_exp = (await mk("create_experiment", project_id=w.mine, session_id=w.s_mine, name="own experiment"))["experiment"]["id"]
    b.run = (await mk("start_experiment_run", project_id=w.theirs, session_id=w.s_theirs, experiment_id=b.exp))["run"]["id"]
    b.rrun = (await mk(
        "start_research_run", project_id=w.theirs, session_id=w.s_theirs, mode="read_only", repository_id="repo-x", turn_budget=5,
    ))["run"]["id"]
    b.job = (await mk(
        "register_external_job", project_id=w.theirs, session_id=w.s_theirs, job_key="jk", provider="prov",
        external_id="ext-1", detail=marker,
    ))["job"]["id"]
    b.rtask = (await mk("start_remote_task", project_id=w.theirs, session_id=w.s_theirs, host="h", command=f"echo {marker}"))["job"]["id"]
    b.contract = (await mk("create_paper_contract", project_id=w.theirs, paper_key="pk", title=f"{marker} paper"))["contract"]["id"]
    b.rev = (await mk(
        "create_paper_contract_revision", project_id=w.theirs, contract_id=b.contract, content={"working_title": marker},
    ))["revision"]["id"]
    b.deriv = (await mk(
        "register_docx_derivative", project_id=w.theirs, session_id=w.s_theirs, source_path="a.docx", derivative_path="b.docx",
        source_content_hash="h1", derivative_content_hash="h2", generating_tool="gt", notes=marker,
    ))["derivative"]["id"]
    b.recovery = (await mk("register_session_recovery", project_id=w.theirs, session_id=w.s_theirs, transport="tunnel"))["recovery"]["id"]
    b.gate = (await mk(
        "add_proposal_gate", project_id=w.theirs, category="product_scope", question=f"{marker}?", affected=[b.item],
        evidence="e", created_by="x",
    ))["id"]
    b.watch = (await mk("save_watchlist_query", project_id=w.theirs, source_type="arxiv", query=f"{marker} query"))["watchlist_id"]
    b.hook = (await mk("add_custom_hook", project_id=w.theirs, name=f"{marker}hook", event="PreToolUse", script_sh="echo hi"))["id"]
    b.pointer = (await mk(
        "add_sprint_item_pointer", project_id=w.theirs, sprint_item_id=b.item, source_type="doc", label=marker,
        targets=[{"uri": "file:x.py", "selector": {"type": "range", "start_line": 1, "end_line": 2}}],
    ))["id"]
    from meridian.db import ai_log  # noqa: PLC0415

    tag = w.theirs[:8]
    b.corr, b.actor, b.tenant = f"corr-{tag}", f"actor-{tag}", f"tenant-{tag}"
    root = await ai_log.append_event(
        db, w.theirs, "tool.invoked", "session", actor_id=b.actor, session_id=w.s_theirs, tenant_id=b.tenant,
        correlation_id=b.corr, payload={"detail": marker},
    )
    b.parent = root["id"]
    await ai_log.append_event(
        db, w.theirs, "tool.invoked", "session", actor_id=b.actor, session_id=w.s_theirs, tenant_id=b.tenant,
        correlation_id=b.corr, parent_event_id=b.parent, payload={"detail": marker},
    )
    return b


_RANGE = {"uri": "file:evil.py", "selector": {"type": "range", "start_line": 1, "end_line": 2}}

#: pair -> (w, b) -> (tool, arguments): the call a scoped caller would make.
_BOUND_PROBES: "dict[tuple[str, str], Callable[[W, SimpleNamespace], tuple[str, dict[str, Any]]]]" = {
    ("get_research_run", "run_id"): lambda w, b: ("get_research_run", {"project_id": w.mine, "run_id": b.rrun}),
    ("complete_research_run", "run_id"): lambda w, b: (
        "complete_research_run", {"project_id": w.mine, "session_id": w.s_mine, "run_id": b.rrun, "receipt": {"x": 1}, "disposition": "keep"}),
    ("promote_research_run", "run_id"): lambda w, b: (
        "promote_research_run", {"project_id": w.mine, "session_id": w.s_mine, "run_id": b.rrun}),
    ("get_experiment", "experiment_id"): lambda w, b: ("get_experiment", {"project_id": w.mine, "experiment_id": b.exp}),
    ("get_experiment_events", "experiment_id"): lambda w, b: (
        "get_experiment_events", {"project_id": w.mine, "experiment_id": b.exp}),
    ("get_experiment_events", "run_id"): lambda w, b: (
        "get_experiment_events", {"project_id": w.mine, "experiment_id": b.own_exp, "run_id": b.run}),
    ("get_experiment_run", "run_id"): lambda w, b: ("get_experiment_run", {"project_id": w.mine, "run_id": b.run}),
    ("list_experiment_runs", "experiment_id"): lambda w, b: (
        "list_experiment_runs", {"project_id": w.mine, "experiment_id": b.exp}),
    ("start_experiment_run", "experiment_id"): lambda w, b: (
        "start_experiment_run", {"project_id": w.mine, "session_id": w.s_mine, "experiment_id": b.exp}),
    ("start_experiment_run", "pivot_parent_run_id"): lambda w, b: (
        "start_experiment_run", {"project_id": w.mine, "session_id": w.s_mine, "experiment_id": b.own_exp, "pivot_parent_run_id": b.run}),
    ("complete_experiment_run", "run_id"): lambda w, b: (
        "complete_experiment_run", {"project_id": w.mine, "session_id": w.s_mine, "run_id": b.run, "outcome_summary": "o", "disposition": "discard"}),
    ("promote_experiment_run", "run_id"): lambda w, b: (
        "promote_experiment_run", {"project_id": w.mine, "session_id": w.s_mine, "run_id": b.run}),
    ("record_experiment_event", "experiment_id"): lambda w, b: (
        "record_experiment_event", {"project_id": w.mine, "session_id": w.s_mine, "experiment_id": b.exp, "event_type": "note", "label": "l"}),
    ("record_experiment_event", "run_id"): lambda w, b: (
        "record_experiment_event", {"project_id": w.mine, "session_id": w.s_mine, "experiment_id": b.own_exp, "run_id": b.run, "event_type": "note", "label": "l"}),
    ("register_run_artifact", "run_id"): lambda w, b: (
        "register_run_artifact", {"project_id": w.mine, "session_id": w.s_mine, "run_id": b.run, "logical_path": "a/b.txt",
                                  "content_hash": "sha256:" + "a" * 64, "artifact_role": "output"}),
    ("get_external_job", "job_id"): lambda w, b: ("get_external_job", {"project_id": w.mine, "job_id": b.job}),
    ("update_external_job", "job_id"): lambda w, b: (
        "update_external_job", {"project_id": w.mine, "session_id": w.s_mine, "job_id": b.job, "status": "failed"}),
    ("complete_external_job", "job_id"): lambda w, b: (
        "complete_external_job", {"project_id": w.mine, "session_id": w.s_mine, "job_id": b.job, "status": "succeeded"}),
    ("get_remote_task_status", "job_id"): lambda w, b: ("get_remote_task_status", {"project_id": w.mine, "job_id": b.rtask}),
    ("get_paper_contract", "contract_id"): lambda w, b: ("get_paper_contract", {"project_id": w.mine, "contract_id": b.contract}),
    ("get_current_paper_contract_content", "contract_id"): lambda w, b: (
        "get_current_paper_contract_content", {"project_id": w.mine, "contract_id": b.contract}),
    ("list_paper_contract_revisions", "contract_id"): lambda w, b: (
        "list_paper_contract_revisions", {"project_id": w.mine, "contract_id": b.contract}),
    ("create_paper_contract_revision", "contract_id"): lambda w, b: (
        "create_paper_contract_revision", {"project_id": w.mine, "contract_id": b.contract, "content": {"working_title": "hijack"}}),
    ("approve_paper_contract_revision", "revision_id"): lambda w, b: (
        "approve_paper_contract_revision", {"project_id": w.mine, "revision_id": b.rev, "approved_by_human_id": "h"}),
    ("verify_docx_diff", "derivative_id"): lambda w, b: (
        "verify_docx_diff", {"project_id": w.mine, "derivative_id": b.deriv, "current_source_content_hash": "zz"}),
    ("promote_docx_candidate", "derivative_id"): lambda w, b: (
        "promote_docx_candidate", {"project_id": w.mine, "session_id": w.s_mine, "derivative_id": b.deriv}),
    ("get_session_recovery", "recovery_id"): lambda w, b: ("get_session_recovery", {"project_id": w.mine, "recovery_id": b.recovery}),
    ("run_watchlist_query", "watchlist_id"): lambda w, b: ("run_watchlist_query", {"project_id": w.mine, "watchlist_id": b.watch}),
    ("delete_watchlist_query", "watchlist_id"): lambda w, b: ("delete_watchlist_query", {"project_id": w.mine, "watchlist_id": b.watch}),
    ("update_custom_hook", "hook_id"): lambda w, b: ("update_custom_hook", {"project_id": w.mine, "hook_id": b.hook, "name": "renamed"}),
    ("delete_custom_hook", "hook_id"): lambda w, b: ("delete_custom_hook", {"project_id": w.mine, "hook_id": b.hook}),
    ("get_sprint_item_pointers", "sprint_item_id"): lambda w, b: (
        "get_sprint_item_pointers", {"project_id": w.mine, "sprint_item_id": b.item}),
    ("add_sprint_item_pointer", "sprint_item_id"): lambda w, b: (
        "add_sprint_item_pointer", {"project_id": w.mine, "sprint_item_id": b.item, "source_type": "doc", "targets": [_RANGE]}),
    ("delete_sprint_item_pointer", "pointer_id"): lambda w, b: (
        "delete_sprint_item_pointer", {"project_id": w.mine, "pointer_id": b.pointer}),
    ("relocate_sprint_item_pointer", "pointer_id"): lambda w, b: (
        "relocate_sprint_item_pointer", {"project_id": w.mine, "pointer_id": b.pointer, "targets": [_RANGE]}),
    ("resolve_sprint_item_pointers", "sprint_item_id"): lambda w, b: (
        "resolve_sprint_item_pointers", {"project_id": w.mine, "sprint_item_id": b.item}),
    # (update_sprint_item.item_id and claim_sprint_item.item_id were exempted as bound
    # in pass 2 and are guarded in scope_guard.OBJECT_ARGS since pass 3 -- see F-C1.)
    ("complete_sprint_item", "item_id"): lambda w, b: (
        "complete_sprint_item", {"project_id": w.mine, "session_id": w.s_mine, "item_id": b.item, "notes": "n"}),
    ("release_sprint_item_claim", "item_id"): lambda w, b: (
        "release_sprint_item_claim", {"project_id": w.mine, "session_id": w.s_mine, "item_id": b.item, "force": True}),
    ("transfer_sprint_item_claim", "item_id"): lambda w, b: (
        "transfer_sprint_item_claim", {"project_id": w.mine, "session_id": w.s_mine, "item_id": b.item, "to_actor": "x", "force": True}),
    ("split_sprint_item", "item_id"): lambda w, b: (
        "split_sprint_item", {"project_id": w.mine, "item_id": b.item, "titles": ["alpha part", "omega part"]}),
    ("merge_sprint_items", "item_ids"): lambda w, b: (
        "merge_sprint_items", {"project_id": w.mine, "item_ids": [b.item, b.item2], "new_title": "merged"}),
    ("add_subtask", "parent_id"): lambda w, b: ("add_subtask", {"project_id": w.mine, "parent_id": b.item, "title": "planted"}),
    ("link_manual_github_issue", "item_id"): lambda w, b: (
        "link_manual_github_issue", {"project_id": w.mine, "session_id": w.s_mine, "item_id": b.item, "issue_number": 5}),
    ("reconcile_stale_claims", "item_ids"): lambda w, b: (
        "reconcile_stale_claims", {"project_id": w.mine, "item_ids": [b.item], "dry_run": True}),
    ("get_proposal_gates", "sprint_item_id"): lambda w, b: ("get_proposal_gates", {"project_id": w.mine, "sprint_item_id": b.item}),
    ("get_effective_capability_profile", "sprint_item_id"): lambda w, b: (
        "get_effective_capability_profile", {"project_id": w.mine, "sprint_item_id": b.item}),
    ("generate_handoff", "selected_item_ids"): lambda w, b: (
        "generate_handoff", {"project_id": w.mine, "selected_item_ids": [b.item]}),
    ("generate_handoff", "force_include_ids"): lambda w, b: (
        "generate_handoff", {"project_id": w.mine, "force_include_ids": [b.item]}),
    ("export_ai_log", "correlation_id"): lambda w, b: ("export_ai_log", {"project_id": w.mine, "correlation_id": b.corr}),
    ("export_ai_log", "parent_event_id"): lambda w, b: ("export_ai_log", {"project_id": w.mine, "parent_event_id": b.parent}),
    ("search_ai_log", "tenant_id"): lambda w, b: ("search_ai_log", {"project_id": w.mine, "tenant_id": b.tenant}),
    ("search_ai_log", "correlation_id"): lambda w, b: ("search_ai_log", {"project_id": w.mine, "correlation_id": b.corr}),
    ("search_ai_log", "parent_event_id"): lambda w, b: ("search_ai_log", {"project_id": w.mine, "parent_event_id": b.parent}),
    ("search_ai_log", "actor_id"): lambda w, b: ("search_ai_log", {"project_id": w.mine, "actor_id": b.actor}),
    ("resolve_proposal_gate", "gate_id"): lambda w, b: (
        "resolve_proposal_gate", {"project_id": w.mine, "gate_id": b.gate, "state": "allowed", "decision": "ok", "actor": "a"}),
    ("reopen_proposal_gate", "gate_id"): lambda w, b: (
        "reopen_proposal_gate", {"project_id": w.mine, "gate_id": b.gate, "actor": "a", "reason": "r"}),
}

#: Pairs whose proof is a dedicated test below (an extra setup, or a unit-level
#: proof of the function that holds the project binding).
_CUSTOM_PROOFS = {
    ("complete_sprint_item", "override_hitl_id"), ("complete_sprint_item", "completion_override_hitl_id"),
    ("complete_sprint_item", "foreign_claim_override_hitl_id"), ("update_sprint_item", "override_hitl_id"),
    ("complete_wave_gate", "override_hitl_id"), ("complete_wave_gate", "verification_run_id"),
    ("get_citation_edges", "document_id"),
}

#: Probes whose call is allowed to write ONE kind of row, and why. None of them may touch the FOREIGN
#: object: the shape test below proves that independently with row snapshots.
_ALLOWED_WRITES: "dict[tuple[str, str], str]" = {
    ("generate_handoff", "selected_item_ids"): "a generated handoff is stored under the CALLER's own project",
    ("generate_handoff", "force_include_ids"): "a generated handoff is stored under the CALLER's own project",
    ("link_manual_github_issue", "item_id"): (
        "the raw-content log and audit rows of the CALLER's own project (the handler reports 'linked' even when "
        "the UPDATE bound to the project matched nothing); needs the screening toggle, which only the shape test enables"
    ),
}


def test_every_bound_exemption_has_a_proof_and_every_proof_is_for_a_bound_exemption():
    bound = {pair for pair, (category, _) in _EXEMPT_ID_ARGS.items() if category == "bound"}
    assert bound == set(_BOUND_PROBES) | _CUSTOM_PROOFS, sorted(bound ^ (set(_BOUND_PROBES) | _CUSTOM_PROOFS))
    assert not (set(_BOUND_PROBES) & _CUSTOM_PROOFS)


async def test_handler_bound_second_ids_find_nothing_for_a_foreign_object(db, tmp_path):
    """One world, every 'bound' probe: the foreign object is neither returned nor
    touched. (A single test: the world is the expensive part.)"""
    w = await _world(db)
    b = await _foreign(db, tmp_path, w)
    failures: "list[tuple[Any, list[str], str]]" = []
    for pair, build in _BOUND_PROBES.items():
        tool, args = build(w, b)
        before = await _changes(db)
        try:
            outcome = _blob(await _call(db, tmp_path, tool, args, [w.mine]))
        except Exception as exc:  # noqa: BLE001 -- a refusal may be an exception; its text is what we inspect
            outcome = f"{type(exc).__name__}: {exc}"
        after = await _changes(db)
        problems = []
        if SECRET in outcome or w.theirs in outcome:
            problems.append("returned foreign data")
        if before is not None and after != before and pair not in _ALLOWED_WRITES:
            problems.append(f"wrote {after - before} row(s)")
        if problems:
            failures.append((pair, problems, outcome[:200]))
    assert not failures, failures


async def test_the_bound_probes_are_not_vacuous(db, tmp_path):
    """Control: with the foreign id replaced by an id of the caller's OWN project the
    same tool answers normally, so a refusal above means the binding, not a typo."""
    w = await _world(db)
    own_exp = (await _call(db, tmp_path, "create_experiment", {"project_id": w.mine, "session_id": w.s_mine, "name": "ctl"}, None))["experiment"]["id"]
    got = await _call(db, tmp_path, "get_experiment", {"project_id": w.mine, "experiment_id": own_exp}, [w.mine])
    assert got["experiment"]["id"] == own_exp
    b = await _foreign(db, tmp_path, w)
    owner = await _call(db, tmp_path, "get_experiment", {"project_id": w.theirs, "experiment_id": b.exp}, [w.theirs])
    assert owner["experiment"]["id"] == b.exp          # the foreign object really exists
    refused = await _call(db, tmp_path, "get_experiment", {"project_id": w.mine, "experiment_id": b.exp}, [w.mine])
    assert refused == {"error": "experiment not found in this project"}
    # The write detector used above does fire for a real write.
    before = await _changes(db)
    await _call(db, tmp_path, "store_finding", {"project_id": w.mine, "content": "a real write"}, [w.mine])
    assert before is None or await _changes(db) > before


async def test_generate_handoff_rejects_a_foreign_item_in_both_id_lists(db, tmp_path):
    w = await _world(db)
    foreign = await _item(db, w.theirs, f"{SECRET} rotate the production keys")
    scoped = [w.mine]
    forced = await _call(
        db, tmp_path, "generate_handoff",
        {"project_id": w.mine, "session_id": w.s_mine, "mode": "full", "force_include_ids": [foreign]}, scoped,
    )
    assert forced["force_include_rejected"][0]["id"] == foreign       # only the caller's own input is echoed
    assert SECRET not in _blob(forced) and w.theirs not in _blob(forced)
    with open(forced["file_path"], encoding="utf-8") as fh:
        assert SECRET not in fh.read()
    selected = await _call(
        db, tmp_path, "generate_handoff",
        {"project_id": w.mine, "session_id": w.s_mine, "mode": "full", "selected_item_ids": [foreign]}, scoped,
    )
    assert selected["error"] == "HANDOFF_SELECTION_BLOCKED" and SECRET not in _blob(selected)


async def test_override_approvals_and_verification_runs_are_bound_to_their_own_project(db):
    """The binding lives in gate_override.consume_gate_override_approval and
    sprint_items._wave_gate_evidence_from_run, which every override_hitl_id /
    verification_run_id argument above is passed through with the CALL's project_id."""
    from meridian import gate_override
    from meridian.db import sprint_items as sprint_items_db
    from meridian.db import verification_runs

    w = await _world(db)
    hitl = await gate_override.request_gate_override_hitl(
        db, w.theirs, gate=gate_override.GATE_WAVE_GATE_UNBOUND_PAYLOAD, subject_id="w1|v1", reason="r", description="d",
    )
    await db_module.answer_hitl_request(db, hitl["id"], "Yes — approve this override", "a-human")
    with pytest.raises(gate_override.GateOverrideError) as excinfo:
        await gate_override.consume_gate_override_approval(
            db, w.mine, hitl["id"], gate=gate_override.GATE_WAVE_GATE_UNBOUND_PAYLOAD, subject_id="w1|v1",
        )
    assert excinfo.value.code == "HITL_NOT_FOUND"
    # ... and the foreign approval was not spent by the attempt.
    spent = await gate_override.consume_gate_override_approval(
        db, w.theirs, hitl["id"], gate=gate_override.GATE_WAVE_GATE_UNBOUND_PAYLOAD, subject_id="w1|v1",
    )
    assert spent is not None

    run = await verification_runs.create_verification_run(db, w.theirs, "pytest")
    await verification_runs.complete_verification_run(db, run["id"], status="ok", exit_code=0, passed=3, failed=0)
    with pytest.raises(ValueError, match="belongs to a different project"):
        await sprint_items_db._wave_gate_evidence_from_run(db, w.mine, run["id"])
    assert (await sprint_items_db._wave_gate_evidence_from_run(db, w.theirs, run["id"]))["exit_code"] == 0


async def test_ai_log_filters_are_anded_with_the_project(db, tmp_path):
    from meridian.db import ai_log

    w = await _world(db)
    await ai_log.append_event(
        db, w.theirs, "tool.invoked", "session", actor_id="actor-x", session_id=w.s_theirs, tenant_id="tenant-x",
        correlation_id="corr-x", parent_event_id=None, payload={"secret": SECRET},
    )
    owner = await _call(db, tmp_path, "search_ai_log", {"project_id": w.theirs, "correlation_id": "corr-x"}, None)
    assert len(owner["events"]) == 1, "control: the event exists"
    for tool in ("search_ai_log", "export_ai_log"):
        for key, value in (("correlation_id", "corr-x"), ("parent_event_id", "corr-x")):
            result = await _call(db, tmp_path, tool, {"project_id": w.mine, key: value}, [w.mine])
            assert not result.get("events") and SECRET not in _blob(result), (tool, key)
    for key, value in (("tenant_id", "tenant-x"), ("actor_id", "actor-x"), ("correlation_id", "corr-x")):
        result = await _call(db, tmp_path, "search_ai_log", {"project_id": w.mine, key: value}, [w.mine])
        assert result["events"] == [] and result["total_count"] == 0, key


async def test_the_inert_depends_on_pointer_is_treated_as_a_missing_predecessor(db):
    """efea329f: an item that depends_on a foreign item is never blocked by, nor shown,
    that item (this is why depends_on is exempted rather than guarded)."""
    w = await _world(db)
    foreign = await _item(db, w.theirs, f"{SECRET} the foreign predecessor")
    dependent = (await db_module.add_sprint_item(db, w.mine, "v1", "needs a predecessor", depends_on=foreign))["id"]
    blocking = await db_module.get_blocking_dependency_for_sprint_item(db, dependent)
    assert blocking == {"id": foreign, "title": "(missing sprint item)", "status": "missing"}
    assert SECRET not in _blob(blocking)


# ===========================================================================
# F-M4. session pointers the first pass never exercised, and the tunnel control
# ===========================================================================

@pytest.mark.parametrize("key", scope_guard._SESSION_ARG_KEYS)
async def test_every_session_pointer_key_is_checked_on_every_tool(db, key):
    """The first pass's mutation run could drop from_session_id and verifier_session_id
    from the key list without a test noticing; each key is pinned here on a tool that
    has no session argument of its own."""
    w = await _world(db)
    scoped = [w.mine]
    base = {"project_id": w.mine}
    for foreign in (w.s_theirs, f"  {w.s_theirs}  "):
        with pytest.raises(ValueError, match=SCOPE_ERROR):
            await scope_guard.enforce_scoped_call("get_notes", {**base, key: foreign}, db, scoped)
    await scope_guard.enforce_scoped_call("get_notes", {**base, key: w.s_mine}, db, scoped)
    # An unknown id is left to the handler (many tools auto-register a caller-minted id) ...
    await scope_guard.enforce_scoped_call("get_notes", {**base, key: "caller-minted"}, db, scoped)
    # ... a malformed one is not.
    with pytest.raises(ValueError, match=SCOPE_ERROR):
        await scope_guard.enforce_scoped_call("get_notes", {**base, key: [w.s_mine]}, db, scoped)
    # And an unscoped caller is never asked.
    await scope_guard.enforce_scoped_call("get_notes", {**base, key: w.s_theirs}, _ExplodingDb(), None)


async def test_from_session_id_and_verifier_session_id_are_refused_on_the_tools_that_take_them(db, tmp_path):
    w = await _world(db)
    scoped = [w.mine]
    item = await _item(db, w.mine, "needs an independent verifier")
    before = await _changes(db)
    await _refused(
        db, tmp_path, "send_message",
        {"project_id": w.mine, "to_session_id": w.s_mine, "from_session_id": w.s_theirs, "payload": "spoofed sender"}, scoped,
    )
    await _refused(
        db, tmp_path, "complete_sprint_item",
        {"project_id": w.mine, "session_id": w.s_mine, "item_id": item, "verifier_session_id": w.s_theirs,
         "verification_verdict": "PASS", "notes": "n"}, scoped,
    )
    assert await _changes(db) == before and (await db_module.get_sprint_item(db, item))["status"] == "pending"
    sent = await _call(
        db, tmp_path, "send_message",
        {"project_id": w.mine, "to_session_id": w.s_mine, "from_session_id": w.s_mine2, "payload": "from a colleague"}, scoped,
    )
    assert sent["from_session_id"] == w.s_mine2


async def test_session_ids_as_a_comma_separated_string_is_checked_like_the_list_form(db, tmp_path):
    """idle_until_all_done documents BOTH shapes; the string form was parsed by a line
    no test reached (a mutation that broke it survived)."""
    w = await _world(db)
    scoped = [w.mine]
    for value in (
        f"{w.s_mine},{w.s_theirs}", f"{w.s_theirs}", f" {w.s_mine} , {w.s_theirs} ,", f"{w.s_theirs},{w.s_mine}",
        [w.s_mine, w.s_theirs], (w.s_theirs,),
    ):
        await _refused(db, tmp_path, "idle_until_all_done", {"session_ids": value}, scoped)
    # Own sessions, blanks and "not given" are all fine.
    for value in (f"{w.s_mine},{w.s_mine2}", f" {w.s_mine} , ,{w.s_mine2},", [w.s_mine, w.s_mine2], "", [], None, 0):
        await _call(db, tmp_path, "idle_until_all_done", {"session_ids": value}, scoped)
    # Anything that is neither a string nor a list is malformed and refused.
    for value in (5, {"id": w.s_mine}, True):
        await _refused(db, tmp_path, "idle_until_all_done", {"session_ids": value}, scoped)
    # An unknown id is a normal "done" answer (the barrier contract), not an error.
    await _call(db, tmp_path, "idle_until_all_done", {"session_ids": f"{w.s_mine},not-a-session"}, scoped)


async def test_set_active_repo_for_an_in_scope_worktree_really_reaches_the_tunnel(db, tmp_path, monkeypatch):
    """The first pass's control for this tool only proved 'raises requires a tenant'.
    With a tenant and a stubbed tunnel push the call succeeds for an own worktree and
    is refused (before any push) for a foreign one."""
    from meridian.routes import tunnel as tunnel_routes

    pushed: "list[tuple[str, str]]" = []

    async def _active(tenant_id: str, repo_path: str) -> "dict[str, str]":
        pushed.append((tenant_id, repo_path))
        return {"status": "ok"}

    async def _roots(tenant_id: str, roots: Any) -> "dict[str, str]":
        return {"status": "ok"}

    monkeypatch.setattr(tunnel_routes, "send_active_repo_control", _active)
    monkeypatch.setattr(tunnel_routes, "send_add_fs_roots_control", _roots)
    w = await _world(db)
    own = await db_module.register_worktree(db, w.s_mine, w.mine, "branch-m", "wt-m")
    foreign = await db_module.register_worktree(db, w.s_theirs, w.theirs, "branch-t", "wt-t")
    tenant = {"id": "tenant-1"}

    result = await _call(db, tmp_path, "set_active_repo", {"worktree_id": own["id"]}, [w.mine], tenant=tenant)
    assert result["status"] == "ok" and result["repo_path"] == own["path"]
    assert pushed == [("tenant-1", own["path"])]
    with pytest.raises(ValueError, match=SCOPE_ERROR):
        await _call(db, tmp_path, "set_active_repo", {"worktree_id": foreign["id"]}, [w.mine], tenant=tenant)
    assert pushed == [("tenant-1", own["path"])]                  # nothing was pushed for the foreign one
    # The owner (unscoped) can still activate any worktree of the tenant.
    await _call(db, tmp_path, "set_active_repo", {"worktree_id": foreign["id"]}, None, tenant=tenant)
    assert pushed[-1] == ("tenant-1", foreign["path"])


async def test_malformed_argument_shapes_are_handled_without_crashing_or_leaking(db):
    """Branches the first pass left uncovered (the verifier measured 96%): 'not given'
    values of the wrong type, non-string keys, blank names, odd batch containers and
    non-row entries in a filtered listing."""
    w = await _world(db)
    scoped = [w.mine]
    # falsy non-strings and blank names mean "not given"; a non-string KEY is ignored
    await scope_guard.enforce_scoped_call(
        "get_notes", {"project_id": w.mine, "x_project_id": 0, "y_project_id": [], "z_project_name": "", 7: "seven"}, db, scoped,
    )
    # a batch whose entries container is not a list, or holds non-objects, is the engine's to reject
    await scope_guard.enforce_scoped_call("execute_batch", {"project_id": w.mine, "entries": "oops"}, db, scoped)
    await scope_guard.enforce_scoped_call(
        "execute_batch", {"project_id": w.mine, "entries": ["x", 5, None, {"title": "t"}]}, db, scoped,
    )
    # ... but an object entry inside it is still walked
    with pytest.raises(ValueError, match=SCOPE_ERROR):
        await scope_guard.enforce_scoped_call(
            "execute_batch", {"project_id": w.mine, "entries": ["x", {"session_id": w.s_theirs}]}, db, scoped,
        )
    # a filtered listing drops anything that is not a row
    rows = [None, "x", {"scope_type": "project", "scope_id": w.mine}, {"scope_type": "project", "scope_id": w.theirs}]
    kept = await scope_guard.filter_scoped_result("list_profile_layers", rows, db, scoped)
    assert kept == [{"scope_type": "project", "scope_id": w.mine}]


# ===========================================================================
# F-C1 (pass 3). "bound" has to hold on EVERY code path, not for one call shape
#
# update_sprint_item was exempted above as "bound" because a probe that sets a title is
# refused ("sprint item not found"). The same tool called with NO editable field took
# patch_sprint_item's early return, which read the row by id alone and handed back the
# whole foreign row -- over MCP and over HTTP PATCH /projects/{pid}/sprint-items/{iid} --
# and its in_progress pre-check answered IN_PROGRESS with the foreign item's claimed_at.
# So every pair that is exempted as bound is now sent in its MINIMAL shape (the schema's
# required arguments, the caller's own project_id and the id) and with each optional
# argument ALONE, against a foreign object in every lifecycle state, and each probe is
# shown to have REACHED the id by running the very same call against an object of the
# caller's own project: a probe that answers identically for both proves nothing.
# ===========================================================================

def _swapped(w: W) -> W:
    """The same world seen from the foreign side: building "the foreign objects" for it
    puts them in the caller's OWN project (the control for every probe)."""
    return W(
        mine=w.theirs, theirs=w.mine, s_mine=w.s_theirs, s_mine2=w.s_theirs, s_theirs=w.s_mine,
        name_mine2=w.name_theirs, name_theirs=w.name_mine2,
    )


#: A claimed_at no clock can produce any more, so an echo of it is unmistakable.
_STAMP = "2001-02-03 04:05:06"
_STATES = ("pending", "in_progress", "done")
_SPRINT_ITEM_ID_ARGS = frozenset({"item_id", "item_ids", "parent_id", "sprint_item_id"})
async def _item_states(
    db: Any, tmp_path: Any, project: str, session: str, *, stamp: "str | None" = None, marker: str = SECRET,
) -> SimpleNamespace:
    """One sprint item of ``project`` per lifecycle state: pending, in_progress (claimed by
    ``session``) and done. The marker rides in the notes: a similar title is deduplicated."""
    async def add(title: str) -> str:
        return (await db_module.add_sprint_item(db, project, "v1", title, notes=marker))["id"]

    states = SimpleNamespace(
        pending=await add("reindex the warehouse nightly"),
        in_progress=await add("archive the legacy exports"),
        done=await add("rotate the staging certificates"),
    )
    for item in (states.in_progress, states.done):
        await _call(db, tmp_path, "claim_sprint_item", {"project_id": project, "session_id": session, "item_id": item}, None)
    await _call(
        db, tmp_path, "complete_sprint_item",
        {"project_id": project, "session_id": session, "item_id": states.done, "notes": "finished"}, None,
    )
    if stamp:
        await db.execute("UPDATE sprint_items SET claimed_at = ? WHERE id = ?", (stamp, states.in_progress))
        await db.commit()
    got = [(await db_module.get_sprint_item(db, getattr(states, name)))["status"] for name in _STATES]
    assert got == list(_STATES), f"the world is not what the probes assume: {got}"
    return states


async def _rows(db: Any, ids: "list[str]") -> "dict[str, Any]":
    return {item_id: await db_module.get_sprint_item(db, item_id) for item_id in ids}


def _dummy_values(prop: "dict[str, Any]", key: str, w: W) -> "list[Any]":
    """Values of the schema's type to put in an optional argument (an enum: each of its values)."""
    if key in scope_guard._SESSION_ARG_KEYS:
        return [w.s_mine]
    if "enum" in prop:
        return list(prop["enum"])
    kind = prop.get("type")
    if kind == "boolean":
        return [True, False]
    if kind == "integer":
        return [1]
    if kind == "array":
        return [[]]
    if kind == "object":
        return [{}]
    return ["x"]


def _call_shapes(tool: str, id_arg: str, id_value: Any, probe_args: "dict[str, Any]", w: W) -> "list[tuple[str, dict[str, Any]]]":
    """Every call SHAPE a scoped caller can make with ``id_value`` in ``id_arg``: the
    minimal one, then each optional argument alone added to it (with the single-shape
    probe's own value for it when that probe sets one, else a dummy of the schema's type)."""
    schema = _TOOLS[tool].get("inputSchema") or {}
    required = list(schema.get("required") or [])
    props = schema.get("properties") or {}

    def base() -> "dict[str, Any]":
        args: "dict[str, Any]" = {"project_id": w.mine}
        for key in required:
            args[key] = probe_args[key] if key in probe_args else _dummy_values(props.get(key, {}), key, w)[0]
        args[id_arg] = id_value
        return args

    shapes = [("minimal", base())]
    for key, prop in props.items():
        if key in ("project_id", "project_name", id_arg) or key in required:
            continue
        values = _dummy_values(prop, key, w)
        if key in probe_args:
            values = [probe_args[key]] + [v for v in values if v != probe_args[key]]
        shapes.extend((f"+{key}={str(value)[:24]}", {**base(), key: value}) for value in values)
    return shapes


async def _outcome(db: Any, tmp_path: Any, tool: str, args: "dict[str, Any]", scoped: "list[str] | None", tenant: Any = None) -> str:
    try:
        return _blob(await _call(db, tmp_path, tool, args, scoped, tenant=tenant))
    except Exception as exc:  # noqa: BLE001 -- a refusal may be an exception; its text is what we inspect
        return f"{type(exc).__name__}: {exc}"


def _ids_in(*containers: Any) -> "set[str]":
    found: "set[str]" = set()
    for container in containers:
        if isinstance(container, str):
            found.add(container)
        elif isinstance(container, dict):
            found |= _ids_in(*container.values())
        elif isinstance(container, (list, tuple, set)):
            found |= _ids_in(*container)
    return {i for i in found if len(i) >= 8}


def _squash(text: str, ids: "set[str]") -> str:
    """``text`` with every id replaced, so two answers that differ only in WHICH object they name compare equal."""
    for object_id in sorted(ids, key=len, reverse=True):
        text = text.replace(object_id, "<id>")
    return text


async def _enable_manual_issue_screening(db: Any, project_id: str, tenant_id: str) -> None:
    hitl = await db_module.request_hitl(
        db, project_id, "Enable?", kind="manual_issue_screening_toggle", require_human=True, options=["Yes", "No"],
    )
    await db_module.answer_hitl_request(db, hitl["id"], "Yes", answered_by="human")
    await db_module.set_manual_issue_screening_enabled(db, True, hitl_id=hitl["id"], tenant_id=tenant_id)


async def _benign_issue(name: str, args: "dict[str, Any]", tenant: Any, db_arg: Any) -> "dict[str, Any]":
    return {"number": 7, "title": "Bug: crash on save", "body": "Steps: click save twice", "html_url": "https://x/7", "comments": []}


#: The marker the control world's objects carry: a tool that legitimately answers with the CALLER's own
#: data (a generated handoff renders the own board) must not be mistaken for a leak of the foreign one.
_OWN_MARKER = "OWN-DATA-MARKER"
_BOUND_TOOLS = sorted({tool for tool, _ in _BOUND_PROBES})


def _with_written_file(outcome: str) -> str:
    """``outcome`` plus the text of the file a generate_handoff answer points at (the answer alone does
    not show what was rendered into it)."""
    try:
        path = json.loads(outcome).get("file_path")
    except (ValueError, AttributeError):
        return outcome
    if isinstance(path, str) and os.path.isfile(path):
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return outcome + "\n" + fh.read()
    return outcome


def test_the_shape_generator_really_produces_minimal_and_single_optional_shapes(tmp_path):
    w = W("p-mine", "p-theirs", "s-mine", "s-mine2", "s-theirs", "n2", "nt")
    shapes = dict(_call_shapes("get_experiment_events", "experiment_id", "EXP-ID-0001", {"limit": 7, "run_id": "r"}, w))
    assert shapes["minimal"] == {"project_id": "p-mine", "experiment_id": "EXP-ID-0001"}
    assert shapes["+limit=7"] == {"project_id": "p-mine", "experiment_id": "EXP-ID-0001", "limit": 7}   # the probe's own value
    assert shapes["+run_id=r"]["run_id"] == "r" and "limit" not in shapes["+run_id=r"]                 # each optional ALONE
    update = dict(_call_shapes("update_sprint_item", "item_id", "ITEM-ID-0001", {}, w))
    assert update["minimal"] == {"project_id": "p-mine", "item_id": "ITEM-ID-0001"}
    assert update["+required_notes=True"]["required_notes"] is True and update["+required_notes=False"]["required_notes"] is False
    assert {"+priority=urgent", "+priority=low", "+blocker_kind=manual", "+github_channel=graduated"} <= set(update)  # every enum value
    # a required argument the probe does not set gets a dummy of its type; session pointers are the caller's own
    assert dict(_call_shapes("release_sprint_item_claim", "item_id", "I", {}, w))["minimal"]["session_id"] == "s-mine"
    assert dict(_call_shapes("add_sprint_item_pointer", "sprint_item_id", "I", {}, w))["minimal"]["targets"] == []
    assert len(_BOUND_PROBES) >= 50 and "update_sprint_item" not in _BOUND_TOOLS   # that one is guarded by OBJECT_ARGS, see below
    # a tool called with a LIST of ids gets a list
    assert dict(_call_shapes("merge_sprint_items", "item_ids", ["A", "B"], {}, w))["minimal"]["item_ids"] == ["A", "B"]
    # the file behind a generate_handoff answer is part of what the caller learns
    written = tmp_path / "handoff.md"
    written.write_text("rendered body", encoding="utf-8")
    assert _with_written_file(json.dumps({"file_path": str(written)})).endswith("rendered body")
    assert _with_written_file('{"file_path": "/no/such/file"}') == '{"file_path": "/no/such/file"}'
    assert _with_written_file("not json") == "not json" and _with_written_file("[1]") == "[1]"


async def _shape_run(
    db: Any, tmp_path: Any, pair: "tuple[str, str]",
    build: "Callable[[W, SimpleNamespace], tuple[str, dict[str, Any]]]", *, tag: str = "",
) -> "tuple[list[Any], bool]":
    """Send ``pair`` as a scoped caller in every call shape against a foreign object in every state, each
    next to the same call on an object of the caller's own project. Returns ``(problems, reached)``:
    what a call returned / wrote / changed that it must not have, and whether some shape answered the
    foreign id differently than the own one (i.e. the probe really got as far as looking the id up)."""
    tool, id_arg = pair
    w = await _world(db, tag)
    b = await _foreign(db, tmp_path, w)
    bo = await _foreign(db, tmp_path, _swapped(w), marker=_OWN_MARKER)    # the same kinds of objects in the caller's own project
    scoped = [w.mine]
    tenant: Any = None
    problems: "list[Any]" = []
    reached = False
    with contextlib.ExitStack() as stack:
        if tool == "link_manual_github_issue":
            tenant = {"id": "t-shapes", "github_pat": "enc"}
            await _enable_manual_issue_screening(db, w.mine, "t-shapes")
            stack.enter_context(patch("meridian.mcp.handler._dispatch_github_tool", side_effect=_benign_issue))
        _, foreign_args = build(w, b)
        _, own_args = build(w, bo)
        foreign_item_ids = [b.item, b.item2]
        targets: "list[tuple[str, Any, Any]]" = [("", foreign_args[id_arg], own_args[id_arg])]
        if id_arg in _SPRINT_ITEM_ID_ARGS:
            # "pending" is the probe's own item (the one the pointers and gates hang on); the other two
            # states are fresh items, one claimed (with a claimed_at no clock can produce) and one done.
            theirs = await _item_states(db, tmp_path, w.theirs, w.s_theirs, stamp=_STAMP)
            mine = await _item_states(db, tmp_path, w.mine, w.s_mine, marker=_OWN_MARKER)
            foreign_item_ids += [getattr(theirs, name) for name in _STATES]
            wrap = (lambda v: [v]) if isinstance(foreign_args[id_arg], list) else (lambda v: v)
            targets = [("pending", foreign_args[id_arg], own_args[id_arg])] + [
                (name, wrap(getattr(theirs, name)), wrap(getattr(mine, name))) for name in _STATES[1:]
            ]
        for state, foreign_id, own_id in targets:
            own_shapes = dict(_call_shapes(tool, id_arg, own_id, own_args, w))
            for label, args in _call_shapes(tool, id_arg, foreign_id, foreign_args, w):
                rows_before, writes_before = await _rows(db, foreign_item_ids), await _changes(db)
                outcome = _with_written_file(await _outcome(db, tmp_path, tool, args, scoped, tenant))
                writes_after = await _changes(db)
                found = [m for m in (SECRET, w.theirs, w.s_theirs, _STAMP, "IN_PROGRESS") if m in outcome]
                if found:
                    problems.append((pair, state, label, "returned foreign data", found, outcome[:160]))
                if writes_before is not None and writes_after != writes_before and pair not in _ALLOWED_WRITES:
                    problems.append((pair, state, label, f"wrote {writes_after - writes_before} row(s)", outcome[:160]))
                if await _rows(db, foreign_item_ids) != rows_before:
                    problems.append((pair, state, label, "changed a foreign sprint item", outcome[:160]))
                control = _with_written_file(await _outcome(db, tmp_path, tool, own_shapes[label], scoped, tenant))
                ids = _ids_in(args, own_shapes[label], foreign_args, own_args)
                reached = reached or _squash(outcome, ids) != _squash(control, ids)
    return problems, reached


@pytest.mark.parametrize("pair", sorted(_BOUND_PROBES), ids=lambda pair: ".".join(pair))
async def test_a_bound_pair_answers_nothing_for_a_foreign_object_in_any_call_shape(db, tmp_path, pair):
    """The foreign object is neither returned, echoed, summarised nor touched -- whichever way the
    (own project_id, foreign id) call is shaped, and whatever state the object is in -- and the probe
    reached the id (some shape answers differently than the same call on an object of the caller's own)."""
    problems, reached = await _shape_run(db, tmp_path, pair, _BOUND_PROBES[pair])
    assert not problems, problems
    assert reached, (
        f"{pair}: every call shape answered the foreign id exactly as it answered the caller's own object, so the "
        "probe never got as far as looking the id up and proves nothing; fix the probe's arguments"
    )


async def test_the_shape_probes_catch_the_original_update_sprint_item_bug(db, tmp_path, monkeypatch):
    """Negative control for the whole F-C1 machinery. Put the bug back (guard off, the db read unbound
    again) and the same shapes that pass above report it: the bare call returns the foreign row, and an
    in_progress item answers IN_PROGRESS with the foreign claimed_at. The pass-2 probe sent only a title."""
    from meridian.db import sprint_items as sprint_items_db

    async def _open(*_a: Any, **_k: Any) -> None:
        return None

    async def _unbound(db_arg: Any, project_id: str, item_id: str) -> Any:
        return await db_module.get_sprint_item(db_arg, item_id)

    pair = ("update_sprint_item", "item_id")

    def build(w: W, b: SimpleNamespace) -> "tuple[str, dict[str, Any]]":
        return "update_sprint_item", {"project_id": w.mine, "item_id": b.item, "title": "hijacked"}

    fixed, reached = await _shape_run(db, tmp_path, pair, build)
    assert fixed == [] and reached                          # today: nothing leaks, and the probe got to the id

    monkeypatch.setattr(scope_guard, "enforce_scoped_call", _open)
    monkeypatch.setattr(sprint_items_db, "_get_sprint_item_in_project", _unbound)
    broken, _ = await _shape_run(db, tmp_path, pair, build, tag="-again")
    leaked = {(state, label) for _pair, state, label, what, *_rest in broken if what == "returned foreign data"}
    assert ("pending", "minimal") in leaked                  # the verifier's reproduction: no editable field at all
    assert ("done", "minimal") in leaked
    assert any(state == "in_progress" for state, _label in leaked)   # the pre-check that answered IN_PROGRESS
    # ... while the pass-2 shape (a title) never revealed anything, which is why the exemption survived it
    assert ("pending", "+title=x") not in leaked


def test_every_override_approval_is_spent_against_the_calls_own_project():
    """The 'bound' proof for the *override_hitl_id arguments is function-level
    (consume_gate_override_approval(db, project_id, hitl_id, ...) answers 'not found in this
    project'), not a call shape. It only holds while every call site passes the CALL's own
    project as the second argument."""
    import glob
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(mcp_handler.__file__)))
    sites: "list[tuple[str, int, str]]" = []
    for path in sorted(glob.glob(os.path.join(root, "**", "*.py"), recursive=True)):
        with open(path, encoding="utf-8", errors="ignore") as fh:
            source = fh.read()
        if "consume_gate_override_approval" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            func = getattr(node, "func", None)
            if isinstance(node, ast.Call) and getattr(func, "attr", getattr(func, "id", "")) == "consume_gate_override_approval":
                second = ast.unparse(node.args[1]) if len(node.args) > 1 else "<missing>"
                sites.append((os.path.relpath(path, root), node.lineno, second))
    assert len(sites) >= 5, sites     # the scan really sees the call sites
    assert all("project_id" in second for _, _, second in sites), sites


# --- update_sprint_item and claim_sprint_item: the two tools the shapes caught --------

async def test_patch_sprint_item_with_nothing_to_edit_is_bound_to_the_callers_project(db, tmp_path):
    """The verifier's reproduction at its root: a patch that edits nothing still READS the
    row, and that read took the id alone."""
    w = await _world(db)
    foreign = await _item_states(db, tmp_path, w.theirs, w.s_theirs)
    own = await _item_states(db, tmp_path, w.mine, w.s_mine)
    for state in _STATES:
        item_id = getattr(foreign, state)
        before = await db_module.get_sprint_item(db, item_id)
        assert await db_module.patch_sprint_item(db, w.mine, item_id) is None             # nothing to edit
        assert await db_module.patch_sprint_item(db, w.mine, item_id, title="hijacked") is None
        assert await db_module.patch_sprint_item(db, w.mine, item_id, status="pending") is None
        assert await db_module.get_sprint_item(db, item_id) == before                     # and nothing changed
        # the owning project still gets its row back
        assert (await db_module.patch_sprint_item(db, w.theirs, item_id))["id"] == item_id
    # An unknown id answers the same way, and the caller's own item is returned.
    assert await db_module.patch_sprint_item(db, w.mine, "no-such-item") is None
    row = await db_module.patch_sprint_item(db, w.mine, own.pending)
    assert row["id"] == own.pending and row["project_id"] == w.mine


def test_the_http_patch_with_an_empty_body_is_bound_to_the_url_project(client):
    """The HTTP twin of the same read: PATCH /projects/{own}/sprint-items/{foreign} with {}
    returned 200 and the foreign row, while the same call with a title was a 404."""
    def project() -> str:
        r = client.post("/projects", json={"name": f"patch-bind-{os.urandom(4).hex()}"})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def add(project_id: str, title: str) -> str:
        r = client.post(f"/projects/{project_id}/sprint-items", json={"version": "v1", "title": title, "notes": SECRET})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    mine, theirs = project(), project()
    foreign, own = add(theirs, f"{SECRET} rotate the production keys"), add(mine, "tidy the parser tests")
    for body in ({}, {"title": "hijacked"}, {"status": "pending"}, {"notes": "n"}):
        r = client.patch(f"/projects/{mine}/sprint-items/{foreign}", json=body)
        assert r.status_code == 404 and SECRET not in r.text, (body, r.status_code, r.text)
    assert client.patch(f"/projects/{mine}/sprint-items/no-such-item", json={}).status_code == 404
    assert client.patch(f"/projects/{theirs}/sprint-items/{foreign}", json={}).json()["id"] == foreign
    assert client.patch(f"/projects/{mine}/sprint-items/{own}", json={}).json()["id"] == own


async def test_update_sprint_item_never_answers_for_a_foreign_item_in_any_state(db, tmp_path, monkeypatch):
    """The verifier's reproduction over MCP, in every state of the foreign item."""
    w = await _world(db)
    scoped = [w.mine]
    foreign = await _item_states(db, tmp_path, w.theirs, w.s_theirs, stamp=_STAMP)
    own = await _item_states(db, tmp_path, w.mine, w.s_mine, stamp="2002-03-04 05:06:07")
    before = await _rows(db, [getattr(foreign, name) for name in _STATES])
    # With the guard: refused in every state, whether or not the call edits anything or forces it.
    for state in _STATES:
        for extra in ({}, {"force": True}, {"override_reason": "r"}, {"title": "hijacked"}, {"status": "pending", "force": True}):
            await _refused(db, tmp_path, "update_sprint_item", {"project_id": w.mine, "item_id": getattr(foreign, state), **extra}, scoped)
    await _refused(db, tmp_path, "update_sprint_item", {"project_id": w.mine, "item_id": "no-such-item"}, scoped)   # an unknown id looks alike
    assert await _rows(db, [getattr(foreign, name) for name in _STATES]) == before
    # The caller's own items still answer: the row for an edit-less call, IN_PROGRESS for a claimed one.
    kept = await _call(db, tmp_path, "update_sprint_item", {"project_id": w.mine, "item_id": own.pending}, scoped)
    assert kept["id"] == own.pending and kept["project_id"] == w.mine
    claimed = await _call(db, tmp_path, "update_sprint_item", {"project_id": w.mine, "item_id": own.in_progress}, scoped)
    assert claimed["error"] == "IN_PROGRESS" and claimed["claimed_at"] == "2002-03-04 05:06:07"
    # The owner (unscoped) is unchanged, foreign ids included.
    owner = await _call(db, tmp_path, "update_sprint_item", {"project_id": w.theirs, "item_id": foreign.pending}, None)
    assert owner["id"] == foreign.pending

    # The handler's own binding, with the guard switched off: the db no longer hands back a foreign
    # row for an edit-less call (pending, done, or a forced in_progress one). The un-forced in_progress
    # pre-check lives in handlers/sprint_tools.py and is covered by the guard alone.
    async def _open(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(scope_guard, "enforce_scoped_call", _open)
    for state, extra in (("pending", {}), ("pending", {"force": True}), ("done", {}), ("done", {"force": True}), ("in_progress", {"force": True})):
        out = await _call(db, tmp_path, "update_sprint_item", {"project_id": w.mine, "item_id": getattr(foreign, state), **extra}, scoped)
        assert out == {"error": "sprint item not found"}, (state, extra, out)
    # ... in every call shape (each optional argument alone), not just the bare one.
    for state in _STATES:
        for label, args in _call_shapes("update_sprint_item", "item_id", getattr(foreign, state), {}, w):
            if state == "in_progress":
                args = {**args, "force": True}
            outcome = await _outcome(db, tmp_path, "update_sprint_item", args, scoped)
            assert not [m for m in (SECRET, w.theirs, _STAMP, "IN_PROGRESS") if m in outcome], (state, label, outcome[:200])
    assert await _rows(db, [getattr(foreign, name) for name in _STATES]) == before


async def test_claim_sprint_item_does_not_answer_protected_for_a_foreign_item(db, tmp_path):
    """claim_sprint_item's installer-script pre-check read the item by id alone, so a foreign
    item whose touches_files names hooks.ps1 / hooks.sh answered PROTECTED, a fact about
    another project's item, instead of 'sprint item not found'."""
    w = await _world(db)
    scoped = [w.mine]
    foreign = await _item(db, w.theirs, "rework the foreign installer")
    own = await _item(db, w.mine, "rework the own installer")
    for item_id in (foreign, own):
        await db.execute("UPDATE sprint_items SET touches_files = ? WHERE id = ?", ('["hooks.ps1"]', item_id))
    await db.commit()
    args = {"project_id": w.mine, "session_id": w.s_mine}
    for extra in ({}, {"force": False}):
        await _refused(db, tmp_path, "claim_sprint_item", {**args, "item_id": foreign, **extra}, scoped)
    await _refused(db, tmp_path, "claim_sprint_item", {**args, "item_id": "no-such-item"}, scoped)
    assert (await db_module.get_sprint_item(db, foreign))["status"] == "pending"
    # An own item that touches the installer is still PROTECTED (unless forced), scoped or not.
    for caller_scope in (scoped, None):
        protected = await _call(db, tmp_path, "claim_sprint_item", {**args, "item_id": own}, caller_scope)
        assert protected["error"] == "PROTECTED" and protected["protected_files"] == ["hooks.ps1"]
    forced = await _call(db, tmp_path, "claim_sprint_item", {**args, "item_id": own, "force": True}, scoped)
    assert forced["status"] == "in_progress"
    # The owner may still claim for any project of the tenant, and sees PROTECTED for the foreign item.
    owner = await _call(db, tmp_path, "claim_sprint_item", {"project_id": w.theirs, "session_id": w.s_theirs, "item_id": foreign}, None)
    assert owner["error"] == "PROTECTED"


async def test_the_manual_issue_velocity_signal_is_never_fed_a_foreign_item(db):
    """discover_and_link_manual_issue read the item by id alone to take its wave label for
    the velocity signal, and that signal (wave label included) lands in an own-project HITL
    and audit row. link_sprint_item_github_issue was already bound to the project."""
    from meridian.mcp.handler import discover_and_link_manual_issue

    w = await _world(db)
    foreign = (await db_module.add_sprint_item(db, w.theirs, "v1", "foreign wave item", wave="WAVE-SECRET"))["id"]
    own = (await db_module.add_sprint_item(db, w.mine, "v1", "own wave item", wave="own-wave"))["id"]
    await _enable_manual_issue_screening(db, w.mine, "t-velocity")
    real = db_module.check_manual_issue_action_velocity
    seen: "list[Any]" = []

    async def _spy(db_arg: Any, project_id: str, *, triggering_item: Any = None, **kwargs: Any) -> "dict[str, Any]":
        seen.append(triggering_item)
        return await real(db_arg, project_id, triggering_item=triggering_item, **kwargs)

    tenant = {"id": "t-velocity", "github_pat": "enc"}
    with patch.object(db_module, "check_manual_issue_action_velocity", _spy), \
            patch("meridian.mcp.handler._dispatch_github_tool", side_effect=_benign_issue):
        stray = await discover_and_link_manual_issue(db, w.mine, foreign, 7, tenant)
        mine = await discover_and_link_manual_issue(db, w.mine, own, 8, tenant)
    assert seen[0] is None                                              # no foreign wave label reaches the signal
    assert seen[1]["id"] == own and seen[1]["wave"] == "own-wave"       # the own item still does
    assert stray["action"] == "linked" and stray["item"] is None        # (the UPDATE bound to the project matched nothing)
    assert mine["item"]["github_issue_number"] == 8
    assert (await db_module.get_sprint_item(db, foreign))["github_issue_number"] is None


# ===========================================================================
# F-C2. the LOW items that were cheap
# ===========================================================================

async def test_the_caption_link_primitives_are_bound_to_a_document_when_given_one(db, tmp_path, monkeypatch):
    """The root cause behind the guard's membership check: the store primitives took a bare
    id and updated any row. With document_id a figure / table of another document is 'not
    found' for every caller; the handlers pass the document they resolved under the project."""
    from meridian import doc_store

    try:
        _, store, docs = await _doc_world(db, tmp_path, monkeypatch)
        for method, lister, key in (
            ("set_figure_caption_link", "get_figures", "figure"), ("set_table_caption_link", "get_tables", "table"),
        ):
            link = getattr(store, method)
            theirs, mine = docs["theirs"], docs["mine"]
            assert await link(theirs[key], "el-x", document_id=mine["id"]) is None
            assert await link(docs["mine2"][key], "el-x", document_id=mine["id"]) is None
            assert (await getattr(store, lister)(theirs["id"]))[0]["caption_element_id"] is None
            ok = await link(mine[key], "el-ok", document_id=mine["id"])
            assert ok["caption_element_id"] == "el-ok" and ok["document_id"] == mine["id"]
            # a direct caller that names no document keeps the bare-id behaviour
            assert (await link(theirs[key], "el-bare"))["caption_element_id"] == "el-bare"
            assert await link("", "el-blank", document_id=mine["id"]) is None
    finally:
        await doc_store.close_all_doc_stores()


async def test_docx_conflict_element_lists_keep_the_ids_in_scope_sessions_hold(db, tmp_path):
    """other_claimed_elements / conflicting_elements merge the element ids of EVERY other live
    holder, so pass 2 dropped them for any scoped caller. Re-reading the claims attributes each
    id: an in-scope-only conflict keeps its whole list, a mixed one keeps the in-scope ids."""
    w = await _world(db)
    scoped = [w.mine]
    path = "shared/lists.docx"
    for element in ("pA", "pB"):
        await _hold(db, tmp_path, w.s_mine2, "claim_docx_region", file_path=path, element_id=element)

    conflict = await _call(db, tmp_path, "claim_docx_region", {"session_id": w.s_mine, "file_path": path, "element_id": "pA"}, scoped)
    assert conflict["reason"] == "element_conflict" and conflict["other_claimed_elements"] == ["pB"]
    assert conflict["conflicts"][0]["holder_session_id"] == w.s_mine2 and "holder_redacted" not in conflict
    lease = await _call(db, tmp_path, "acquire_docx_document_lease", {"session_id": w.s_mine, "file_path": path}, scoped)
    assert lease["reason"] == "region_claims_active" and lease["conflicting_elements"] == ["pA", "pB"]
    assert lease["holder_session_id"] == w.s_mine2 and "holder_redacted" not in lease

    # A foreign session joins the same document: its ids are dropped, the in-scope ones stay.
    await _hold(db, tmp_path, w.s_theirs, "claim_docx_region", file_path=path, element_id="pC")
    mixed = await _call(db, tmp_path, "claim_docx_region", {"session_id": w.s_mine, "file_path": path, "element_id": "pA"}, scoped)
    assert mixed["other_claimed_elements"] == ["pB"] and mixed["holder_redacted"] is True
    assert "pC" not in _blob(mixed)
    _assert_no_identity(mixed, w)
    mixed_lease = await _call(db, tmp_path, "acquire_docx_document_lease", {"session_id": w.s_mine, "file_path": path}, scoped)
    assert mixed_lease["conflicting_elements"] == ["pA", "pB"] and mixed_lease["holder_redacted"] is True
    assert "pC" not in _blob(mixed_lease)
    _assert_no_identity(mixed_lease, w)
    # The owner (unscoped) still sees every id and every holder.
    owner = await _call(db, tmp_path, "claim_docx_region", {"session_id": w.s_mine, "file_path": path, "element_id": "pA"}, None)
    assert sorted(owner["other_claimed_elements"]) == ["pB", "pC"] and "holder_redacted" not in owner


async def test_the_element_list_redactor_fails_closed(db, tmp_path, monkeypatch):
    """Every way the attribution can fail drops the list instead of keeping unattributed ids."""
    w = await _world(db)
    scoped = [w.mine]
    path = "shared/closed.docx"
    await _hold(db, tmp_path, w.s_mine2, "claim_docx_region", file_path=path, element_id="pB")
    await _hold(db, tmp_path, w.s_theirs, "claim_docx_region", file_path=path, element_id="pF")

    def result(**extra: Any) -> "dict[str, Any]":
        return {"claimed": False, "reason": "element_conflict", "file_path": path, "other_claimed_elements": ["pB", "pF"], **extra}

    async def redact(res: "dict[str, Any]") -> "dict[str, Any]":
        return await scope_guard.filter_scoped_result("claim_docx_region", res, db, scoped, args={"session_id": w.s_mine})

    narrowed = await redact(result())
    assert narrowed["other_claimed_elements"] == ["pB"] and narrowed["holder_redacted"] is True
    # an id that an in-scope AND a foreign session both hold (rows the conflict rule never produces, but
    # a race or a hand-edited table can) goes with the foreign holder
    for claim_id, holder in (("dup-1", w.s_mine2), ("dup-2", w.s_theirs)):
        await db.execute(
            "INSERT INTO file_docx_region_claims (id, session_id, file_path, element_id) VALUES (?, ?, ?, ?)",
            (claim_id, holder, path, "pShared"),
        )
    await db.commit()
    assert (await redact(result(other_claimed_elements=["pB", "pShared"])))["other_claimed_elements"] == ["pB"]
    # an id nobody holds any more is not attributable, so it goes too (the result only shrinks)
    assert (await redact(result(other_claimed_elements=["pB", "ghost"])))["other_claimed_elements"] == ["pB"]
    assert "other_claimed_elements" not in await redact(result(other_claimed_elements=["pF"]))
    assert "other_claimed_elements" not in await redact(result(other_claimed_elements=["ghost"]))
    # no file path, a non-list or a list of non-strings cannot be attributed at all
    for broken in ({"file_path": None}, {"file_path": ""}, {"file_path": 7}, {"other_claimed_elements": "pB"},
                   {"other_claimed_elements": None}, {"other_claimed_elements": ["pB", 5]}):
        assert "other_claimed_elements" not in await redact(result(**broken)), broken
    # a failing claims read drops the list too
    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("claims unreadable")

    with monkeypatch.context() as patched:
        patched.setattr(db_module, "_live_docx_region_claims_for_file", _boom)
        assert "other_claimed_elements" not in await redact(result())
    # and an unscoped caller is never touched
    untouched = result()
    assert await scope_guard.filter_scoped_result("claim_docx_region", untouched, _ExplodingDb(), None) is untouched
