"""0527f636 -- a sibling start_session must not steal another session's handoff.

Root cause: ``projects.pending_goal`` is ONE read-once slot per project and
every ``start_session`` popped it, so with parallel sessions whichever session
started first consumed a handoff written for a DIFFERENT session, leaving the
intended receiver with nothing.

Fix under test:

* ``generate_handoff(pending_goal_receiver=...)`` (MCP arg ``receiver``) can
  ADDRESS the persisted goal to its receiver (any subset of ``session_name`` /
  ``role`` / ``worktree``).  An addressed goal is delivered -- and cleared --
  only to a ``start_session`` matching every field; any other session neither
  receives nor consumes it, and it stays readable via ``load_handoff``.
* An UNADDRESSED handoff keeps today's behaviour (first ``start_session`` wins).
* The pop is a compare-and-clear, so two racing start_sessions cannot both be
  handed the same goal.
* The address rides in the existing ``projects.pending_goal`` cell (no schema
  change), is stripped by every reader, and survives amend/correction.
"""
from __future__ import annotations

import asyncio

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import server as mh
from meridian.mcp.handlers.project_tools import handle_start_session


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _start(db, pid: str, tmp_path, name: str, **extra):
    """Drive the real start_session handler exactly as an MCP call would."""
    args = {"project_id": pid, "session_name": name}
    args.update(extra)
    result = await handle_start_session(
        args,
        db=db,
        data_dir=str(tmp_path),
        tenant=None,
        _mcp_tenant_id=None,
        executor_sessions=set(),
    )
    assert "error" not in result, result
    return result


async def _seed_project(db, name: str):
    p = await db_module.create_project(db, name)
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    return p


async def _raw_cell(db, pid: str):
    async with db.execute(
        "SELECT pending_goal FROM projects WHERE id = ?", (pid,)
    ) as cur:
        row = await cur.fetchone()
    return row[0] if not isinstance(row, dict) else row["pending_goal"]


# ---------------------------------------------------------------------------
# the required scenario: two start_sessions after ONE generate_handoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "goal"])
async def test_sibling_start_session_cannot_steal_addressed_handoff(db, tmp_path, mode):
    """The sibling starts FIRST (the exact steal order) -- it must not consume
    the goal; the intended receiver, starting second, still gets it."""
    p = await _seed_project(db, f"recv-steal-{mode}")
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode=mode,
        pending_goal_receiver={"session_name": "exec-a", "role": "executor"},
    )
    stored = await db_module.get_pending_goal(db, p["id"])
    assert stored and "/goal" in stored

    sibling = await _start(db, p["id"], tmp_path, "verifier-b", role="verifier")
    assert "pending_goal" not in sibling
    withheld = sibling["pending_goal_withheld"]
    assert withheld["reason"] == "addressed_to_other_receiver"
    assert withheld["consumed"] is False
    assert withheld["receiver"] == {"session_name": "exec-a", "role": "executor"}
    assert set(withheld["mismatched_fields"]) == {"session_name", "role"}
    # ... and the goal is still pending for its real receiver.
    assert await db_module.get_pending_goal(db, p["id"]) == stored

    receiver = await _start(db, p["id"], tmp_path, "exec-a", role="executor")
    assert receiver["pending_goal"] == stored
    assert receiver["pending_goal_trusted"] is True
    assert "pending_goal_withheld" not in receiver

    # Read-once for the receiver: consumed now, nobody gets it again.
    assert await db_module.get_pending_goal(db, p["id"]) is None
    again = await _start(db, p["id"], tmp_path, "late-c", role="executor")
    assert "pending_goal" not in again
    assert "pending_goal_withheld" not in again


@pytest.mark.asyncio
async def test_receiver_first_then_sibling_gets_nothing(db, tmp_path):
    p = await _seed_project(db, "recv-order")
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True,
        pending_goal_receiver={"session_name": "exec-a"}, mode="full",
    )
    receiver = await _start(db, p["id"], tmp_path, "EXEC-A")  # case-insensitive
    assert receiver["pending_goal"]
    sibling = await _start(db, p["id"], tmp_path, "sibling")
    assert "pending_goal" not in sibling


@pytest.mark.asyncio
async def test_unaddressed_handoff_keeps_todays_first_start_wins(db, tmp_path):
    """No receiver -> exactly the pre-existing behaviour: the first
    start_session (whoever it is) gets it and clears it; the stored cell is the
    bare body with no header."""
    p = await _seed_project(db, "recv-unaddressed")
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="full",
    )
    body = await db_module.get_pending_goal(db, p["id"])
    assert await _raw_cell(db, p["id"]) == body  # nothing wrapped around it
    assert await db_module.get_pending_goal_receiver(db, p["id"]) is None

    first = await _start(db, p["id"], tmp_path, "anyone", role="verifier")
    assert first["pending_goal"] == body
    assert "pending_goal_withheld" not in first
    second = await _start(db, p["id"], tmp_path, "someone-else")
    assert "pending_goal" not in second
    assert "pending_goal_withheld" not in second


# ---------------------------------------------------------------------------
# matching rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worktree_receiver_matches_cwd_inside_worktree_with_path_boundary(db, tmp_path):
    p = await _seed_project(db, "recv-worktree")
    await db_module.set_pending_goal(
        db, p["id"], "/goal WT work",
        receiver={"worktree": "C:\\repo\\.claude\\worktrees\\wt-1"},
    )
    # No cwd supplied -> cannot be verified -> never consumes.
    r0 = await _start(db, p["id"], tmp_path, "s0")
    assert "pending_goal" not in r0
    assert r0["pending_goal_withheld"]["mismatched_fields"] == ["worktree"]
    # A sibling worktree that merely shares a name PREFIX is not inside it.
    r1 = await _start(db, p["id"], tmp_path, "s1", cwd="C:/repo/.claude/worktrees/wt-10")
    assert "pending_goal" not in r1
    assert await db_module.get_pending_goal(db, p["id"]) == "/goal WT work"
    # A subdirectory of the worktree (any slash style / drive-letter case) matches.
    r2 = await _start(
        db, p["id"], tmp_path, "s2", cwd="c:/repo/.claude/worktrees/wt-1/meridian",
    )
    assert r2["pending_goal"] == "/goal WT work"
    assert await db_module.get_pending_goal(db, p["id"]) is None


@pytest.mark.asyncio
async def test_every_addressed_field_must_match_and_missing_claimant_field_is_mismatch(db, tmp_path):
    p = await _seed_project(db, "recv-and")
    await db_module.set_pending_goal(
        db, p["id"], "/goal AND work",
        receiver={"session_name": "exec-a", "role": "executor"},
    )
    # Right name, role omitted -> mismatch (fail-closed), not consumed.
    r1 = await _start(db, p["id"], tmp_path, "exec-a")
    assert "pending_goal" not in r1
    assert r1["pending_goal_withheld"]["mismatched_fields"] == ["role"]
    # Right role, wrong name -> mismatch.
    r2 = await _start(db, p["id"], tmp_path, "exec-z", role="executor")
    assert "pending_goal" not in r2
    assert r2["pending_goal_withheld"]["mismatched_fields"] == ["session_name"]
    assert await db_module.get_pending_goal(db, p["id"]) == "/goal AND work"
    # Both right -> delivered. Retrying under the SAME name (the mismatched
    # attempt above registered it) resumes that session and still receives it.
    r3 = await _start(db, p["id"], tmp_path, "exec-a", role="EXECUTOR")
    assert r3["pending_goal"] == "/goal AND work"


@pytest.mark.asyncio
async def test_legacy_delivery_pop_without_claimant_never_consumes_addressed_goal(db):
    p = await db_module.create_project(db, "recv-noclaimant")
    await db_module.set_pending_goal(db, p["id"], "/goal X", receiver={"role": "executor"})
    assert await db_module.pop_pending_goal_with_meta(db, p["id"]) is None
    assert await db_module.get_pending_goal(db, p["id"]) == "/goal X"
    meta = await db_module.pop_pending_goal_with_meta(
        db, p["id"], claimant={"role": "executor"},
    )
    assert meta["goal"] == "/goal X"
    assert meta["receiver"] == {"role": "executor"}


# ---------------------------------------------------------------------------
# the read-once race itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_start_sessions_deliver_an_unaddressed_goal_at_most_once(db):
    p = await db_module.create_project(db, "recv-race")
    await db_module.set_pending_goal(db, p["id"], "/goal RACE")
    results = await asyncio.gather(*[
        db_module.pop_pending_goal_with_meta(db, p["id"]) for _ in range(6)
    ])
    delivered = [r for r in results if r]
    assert len(delivered) == 1
    assert delivered[0]["goal"] == "/goal RACE"


# ---------------------------------------------------------------------------
# storage: no schema change, header never leaks, body untouched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_address_is_stripped_by_every_reader_and_body_is_untouched(db):
    p = await db_module.create_project(db, "recv-storage")
    body = "/goal line one\n<goal_token>abc</goal_token>\nline three"
    await db_module.set_pending_goal(
        db, p["id"], body, receiver={"session_name": "a-->b", "role": "executor"},
    )
    assert await db_module.get_pending_goal(db, p["id"]) == body
    assert await db_module.get_pending_goal_receiver(db, p["id"]) == {
        "session_name": "a-->b", "role": "executor",
    }
    raw = await _raw_cell(db, p["id"])
    assert raw != body and raw.endswith("\n" + body)
    # Unconditional administrative pop returns just the body and clears both.
    assert await db_module.pop_pending_goal(db, p["id"]) == body
    assert await db_module.get_pending_goal(db, p["id"]) is None
    assert await db_module.get_pending_goal_receiver(db, p["id"]) is None


@pytest.mark.asyncio
async def test_body_that_looks_like_a_header_round_trips_unaddressed(db):
    p = await db_module.create_project(db, "recv-lookalike")
    body = db_module._PENDING_GOAL_ENVELOPE_PREFIX + '{"role": "x"}-->\n/goal sneaky'
    await db_module.set_pending_goal(db, p["id"], body)
    assert await db_module.get_pending_goal(db, p["id"]) == body
    assert await db_module.get_pending_goal_receiver(db, p["id"]) is None


@pytest.mark.asyncio
async def test_new_handoff_replaces_the_address_and_amend_detection_still_works(db, tmp_path):
    p = await _seed_project(db, "recv-amend")
    _, _, am1 = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True,
        pending_goal_receiver={"session_name": "exec-a"}, mode="full",
    )
    assert am1 is False
    # Unconsumed addressed handoff is still detected as "unconsumed" -> amend.
    _, _, am2 = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True,
        pending_goal_receiver={"session_name": "exec-b"}, mode="full",
    )
    assert am2 is True
    assert await db_module.get_pending_goal_receiver(db, p["id"]) == {"session_name": "exec-b"}
    # The newest handoff wins wholesale, including dropping the address.
    _, _, am3 = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="full",
    )
    assert am3 is True
    assert await db_module.get_pending_goal_receiver(db, p["id"]) is None


@pytest.mark.asyncio
async def test_correction_regeneration_keeps_the_receiver_address(db, tmp_path):
    p = await _seed_project(db, "recv-correction")
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True,
        pending_goal_receiver={"session_name": "exec-a", "role": "executor"}, mode="full",
    )
    source = (await db_module.get_handoffs(db, p["id"], limit=1))[0]
    corr = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=source["id"], blocker_classification="scope_stale",
    )
    await handoff_module.regenerate_handoff_correction(
        db, p["id"], corr["id"], str(tmp_path), mode="full", skip_ai_summary=True,
    )
    assert await db_module.get_pending_goal(db, p["id"])
    assert await db_module.get_pending_goal_receiver(db, p["id"]) == {
        "session_name": "exec-a", "role": "executor",
    }


# ---------------------------------------------------------------------------
# validation: a mistyped address must never silently become "unaddressed"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "exec-a",                                   # not an object
        ["exec-a"],
        {"session": "exec-a"},                      # typo'd key
        {"session_name": 5},                        # non-string field
        {"role": "x" * 600},                        # over-long
    ],
)
async def test_invalid_receiver_is_rejected_before_anything_is_persisted(db, tmp_path, bad):
    p = await _seed_project(db, "recv-invalid")
    with pytest.raises(ValueError):
        await handoff_module.generate_handoff(
            db, p["id"], str(tmp_path), skip_ai_summary=True, pending_goal_receiver=bad, mode="full",
        )
    assert await db_module.get_pending_goal(db, p["id"]) is None
    assert await db_module.get_handoffs(db, p["id"], limit=5) == []


def test_blank_receiver_normalises_to_unaddressed():
    norm = db_module.normalize_pending_goal_receiver
    assert norm(None) is None
    assert norm({}) is None
    assert norm({"session_name": "  ", "role": None}) is None
    assert norm({"session_name": " a ", "role": ""}) == {"session_name": "a"}


# ---------------------------------------------------------------------------
# MCP surface: generate_handoff(receiver=...), load_handoff, schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_generate_handoff_receiver_arg_and_load_handoff_reports_it(db, tmp_path):
    out = str(tmp_path)
    proj = await mh._dispatch_mcp_tool("create_project", {"name": "recv-mcp"}, db, out)
    pid = proj["id"]
    await mh._dispatch_mcp_tool(
        "generate_handoff",
        {"project_id": pid, "mode": "full", "receiver": {"session_name": "exec-a"}},
        db, out,
    )
    # A sibling via the real dispatcher: no pending_goal, goal NOT consumed.
    sibling = await mh._dispatch_mcp_tool(
        "start_session", {"project_id": pid, "session_name": "sib"}, db, out,
    )
    assert "pending_goal" not in sibling
    assert sibling["pending_goal_withheld"]["receiver"] == {"session_name": "exec-a"}
    # load_handoff (idempotent, read-only) still shows the goal + its address.
    loaded = await mh._dispatch_mcp_tool("load_handoff", {"project_id": pid}, db, out)
    assert loaded["pending_goal"] and "/goal" in loaded["pending_goal"]
    assert loaded["pending_goal_receiver"] == {"session_name": "exec-a"}
    loaded2 = await mh._dispatch_mcp_tool("load_handoff", {"project_id": pid}, db, out)
    assert loaded2["pending_goal"] == loaded["pending_goal"]
    # The intended receiver gets it through start_session.
    recv = await mh._dispatch_mcp_tool(
        "start_session", {"project_id": pid, "session_name": "exec-a"}, db, out,
    )
    assert recv["pending_goal"] == loaded["pending_goal"]
    after = await mh._dispatch_mcp_tool("load_handoff", {"project_id": pid}, db, out)
    assert after["pending_goal"] is None and after["pending_goal_receiver"] is None


def test_receiver_is_advertised_on_the_shared_tool_schema():
    from meridian import mcp_tools

    tool = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "generate_handoff")
    prop = tool["inputSchema"]["properties"]["receiver"]
    assert prop["type"] == "object"
    assert set(prop["properties"]) == set(db_module.PENDING_GOAL_RECEIVER_FIELDS)


def test_rest_route_rejects_malformed_receiver_with_422(client):
    project = client.post("/projects", json={"name": "recv-rest"}).json()
    resp = client.post(
        f"/projects/{project['id']}/handoff",
        json={"mode": "full", "receiver": {"session": "typo"}},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# defensive decode paths (the server only ever writes well-formed headers)
# ---------------------------------------------------------------------------


def test_decode_defensive_paths_never_leak_a_header_into_a_delivered_body():
    dec = db_module._decode_pending_goal
    prefix = db_module._PENDING_GOAL_ENVELOPE_PREFIX
    # Unusable JSON payload: header stripped, treated as unaddressed.
    assert dec(prefix + "not-json-->\n/goal body") == ("/goal body", None)
    # Valid JSON but an invalid address (unknown key): stripped, unaddressed.
    assert dec(prefix + '{"bogus": "x"}-->\n/goal body') == ("/goal body", None)
    # Structurally broken header line (no newline / no closing marker): whole
    # cell returned as an unaddressed body rather than guessed at.
    assert dec(prefix + '{"role": "x"}-->') == (prefix + '{"role": "x"}-->', None)
    assert dec(prefix + '{"role": "x"}\n/goal body') == (
        prefix + '{"role": "x"}\n/goal body', None,
    )
    assert dec(None) == (None, None)
    assert dec("") == (None, None)
    assert dec("/goal plain") == ("/goal plain", None)


def test_mismatch_helper_is_a_noop_for_unaddressed_goals():
    mism = db_module.pending_goal_receiver_mismatches
    assert mism(None, None) == []
    assert mism({}, {"session_name": "x"}) == []
    assert mism({"worktree": "/r/wt"}, {"cwd": "/r/wt"}) == []
    assert mism({"worktree": "/r/wt"}, {"cwd": "/r/wt/sub"}) == []
    assert mism({"worktree": "/r/wt"}, {"cwd": "/r/wt2"}) == ["worktree"]
    assert mism({"worktree": "/"}, {"cwd": "/anything"}) == []
