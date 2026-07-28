"""efaa918a — resume_wave: stale-manifest gating + body-bound handoff tokens.

Depends on 2a654cb0 (meridian.db.wave_runs) and ef665ef8
(meridian.db.board_snapshot), both merged. Split from proposal
e27d3453-438c-4849-9f63-78174128c007.

Coverage (acceptance criteria first, then normal registration/dispatch):
  1.  Happy path: an unchanged manifest is resumable, resume_delta.changed=False.
  2.  ACCEPTANCE: stale board (revision_hash mismatch via an added item) fails
      closed with an actionable reason and a resume_delta.
  3.  ACCEPTANCE: a superseded item (blocker_kind='superseded') fails closed,
      even though it does NOT move the tracked-field revision hash.
  4.  ACCEPTANCE: a missing pointer (evidence regression) fails closed, backed
      by diff_board_snapshots' own tracked 'pointers' field.
  5.  ACCEPTANCE: changed wave membership fails closed, even though 'wave' is
      NOT part of board_snapshot's tracked-field hash.
  6.  ACCEPTANCE: an edited body fails closed via the body-hash-bound handoff
      token (mint_handoff_token(body=...) / verify_handoff_token(body=...)),
      while the pre-existing four token outcomes (not_found/expired/
      already_consumed/wrong_project) are preserved unchanged.
  7.  Terminal run (merged/aborted) cannot be resumed.
  8.  A run created with no board snapshot pinned refuses (can't verify
      staleness against nothing).
  9.  Unknown wave_run_id raises "not found".
 10.  MCP registration: schema/category/role/title/example present.
 11.  MCP dispatch end-to-end: start_wave_run -> resume_wave happy path ->
      mutate board -> resume_wave fails closed with reasons + resume_delta.
 12.  MCP dispatch: goal_token + presented_body wired through resume_wave,
      including a successful match and a body_mismatch rejection.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import server as srv
from meridian.db.wave_resume import WaveResumeStale
from meridian.mcp_tools import (
    _MCP_TOOLS_LIST,
    _TOOL_CATEGORY,
    _TOOL_EXAMPLES,
    _TOOL_ROLE_RELEVANCE,
    _TITLE_OVERRIDES,
)


_GOOD_EVIDENCE = {
    "status": "ok",
    "exit_code": 0,
    "passed": 42,
    "failed": 0,
}


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


async def _run(db, project_id: str, **kwargs):
    """Create a wave run with a real board snapshot pinned (mirrors test_2a654cb0)."""
    snapshot = await db_module.build_board_snapshot(db, project_id)
    return await db_module.create_wave_run(
        db, project_id, snapshot=snapshot, **kwargs
    )


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unchanged_manifest_is_resumable(db):
    pid = await _project(db, "rw-happy")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: something")
    run = await _run(db, pid, item_ids=[item["id"]])

    result = await db_module.check_wave_resume(db, run["id"])
    assert result["resumable"] is True
    assert result["resume_delta"]["changed"] is False
    assert result["pinned_revision_hash"] == result["live_revision_hash"]
    assert result["status"] == "planned"


# ---------------------------------------------------------------------------
# 2. ACCEPTANCE: stale board (revision_hash mismatch)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_board_revision_hash_mismatch_fails_closed(db):
    pid = await _project(db, "rw-stale-board")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: original item")
    run = await _run(db, pid, item_ids=[item["id"]])

    # The board moves: a new item is added after the wave was planned.
    new_item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: injected mid-wave")

    with pytest.raises(WaveResumeStale) as excinfo:
        await db_module.check_wave_resume(db, run["id"])

    assert new_item["id"] in " ".join(excinfo.value.reasons)
    assert excinfo.value.resume_delta["changed"] is True
    assert any(a["id"] == new_item["id"] for a in excinfo.value.resume_delta["added"])
    # The exception message itself names the specifics — not a generic "invalid".
    assert "stale" in str(excinfo.value).lower()
    assert new_item["id"] in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3. ACCEPTANCE: superseded item
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_superseded_item_fails_closed(db):
    pid = await _project(db, "rw-superseded")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: gets superseded")
    run = await _run(db, pid, item_ids=[item["id"]])

    # Mark the item superseded AFTER the wave was planned. blocker_kind is
    # deliberately NOT one of board_snapshot's tracked fields, so this must
    # NOT show up as a revision_hash mismatch — resume_wave must catch it via
    # its own dedicated check.
    before = await db_module.build_board_snapshot(db, pid)
    await db_module.patch_sprint_item(db, pid, item["id"], blocker_kind="superseded")
    after = await db_module.build_board_snapshot(db, pid)
    assert before["revision_hash"] == after["revision_hash"], (
        "sanity: blocker_kind must NOT move the tracked-field revision hash"
    )

    with pytest.raises(WaveResumeStale) as excinfo:
        await db_module.check_wave_resume(db, run["id"])

    assert any("superseded" in r for r in excinfo.value.reasons)
    assert any(item["id"] in r for r in excinfo.value.reasons)


# ---------------------------------------------------------------------------
# 4. ACCEPTANCE: missing pointer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_pointer_fails_closed(db):
    pid = await _project(db, "rw-missing-pointer")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: prospected item")
    pointer = await db_module.add_sprint_item_pointer(
        db, pid, item["id"], "code",
        [
            {
                "uri": "file:meridian/db/wave_resume.py",
                "selector": {"type": "range", "start_line": 1, "end_line": 20},
            }
        ],
        label="impl site",
    )
    run = await _run(db, pid, item_ids=[item["id"]])

    # The pointer evidence is deleted after the wave was planned.
    await db_module.delete_sprint_item_pointer(db, pointer["id"])

    with pytest.raises(WaveResumeStale) as excinfo:
        await db_module.check_wave_resume(db, run["id"])

    assert any("lost pointer evidence" in r for r in excinfo.value.reasons)
    assert any(item["id"] in r for r in excinfo.value.reasons)
    # Reused diff_board_snapshots shape verbatim: pointers field changed.
    changed = {c["id"]: c["changes"] for c in excinfo.value.resume_delta["changed_items"]}
    assert "pointers" in changed[item["id"]]


# ---------------------------------------------------------------------------
# 5. ACCEPTANCE: changed wave membership
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_changed_wave_membership_fails_closed(db):
    pid = await _project(db, "rw-wave-membership")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "FEAT: wave-scoped item", wave="wave-1",
    )
    run = await _run(db, pid, item_ids=[item["id"]])

    before = await db_module.build_board_snapshot(db, pid)
    await db_module.patch_sprint_item(db, pid, item["id"], wave="wave-2")
    after = await db_module.build_board_snapshot(db, pid)
    assert before["revision_hash"] == after["revision_hash"], (
        "sanity: 'wave' must NOT move the tracked-field revision hash"
    )

    with pytest.raises(WaveResumeStale) as excinfo:
        await db_module.check_wave_resume(db, run["id"])

    assert any("changed wave membership" in r for r in excinfo.value.reasons)
    assert any(item["id"] in r for r in excinfo.value.reasons)


# ---------------------------------------------------------------------------
# 6. ACCEPTANCE: edited body (body-hash-bound handoff token)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mint_and_verify_with_matching_body_succeeds(db):
    pid = await _project(db, "rw-body-match")
    token = await handoff_module.mint_handoff_token(db, pid, body="the real /goal body")
    result = await handoff_module.verify_handoff_token(
        db, token, pid, body="the real /goal body",
    )
    assert result == {"valid": True, "reason": "ok"}


@pytest.mark.asyncio
async def test_edited_body_fails_closed_with_body_mismatch(db):
    pid = await _project(db, "rw-body-edited")
    token = await handoff_module.mint_handoff_token(db, pid, body="the real /goal body")

    result = await handoff_module.verify_handoff_token(
        db, token, pid, body="a DIFFERENT, attacker-edited body",
    )
    assert result["valid"] is False
    assert result["reason"] == "body_mismatch"


@pytest.mark.asyncio
async def test_body_mismatch_does_not_consume_token(db):
    """A body_mismatch must NOT burn the token — the legitimate holder of the
    CORRECT body must still be able to verify successfully afterward (mirrors
    wrong_project's own non-consuming behavior)."""
    pid = await _project(db, "rw-body-retry")
    token = await handoff_module.mint_handoff_token(db, pid, body="the real body")

    bad = await handoff_module.verify_handoff_token(db, token, pid, body="edited")
    assert bad["reason"] == "body_mismatch"

    good = await handoff_module.verify_handoff_token(db, token, pid, body="the real body")
    assert good == {"valid": True, "reason": "ok"}


@pytest.mark.asyncio
async def test_token_minted_without_body_skips_body_check(db):
    """Backward compatibility: a token minted with no body (every pre-efaa918a
    caller, and every caller that still omits it) must behave exactly as
    before — verify_handoff_token(body=...) has nothing to check against."""
    pid = await _project(db, "rw-body-none")
    token = await handoff_module.mint_handoff_token(db, pid)  # no body=

    result = await handoff_module.verify_handoff_token(
        db, token, pid, body="anything at all",
    )
    assert result == {"valid": True, "reason": "ok"}


@pytest.mark.asyncio
async def test_body_bound_token_verified_without_presenting_body_still_ok(db):
    """Caller supplies no `body=` to verify at all -> the check is skipped
    (opt-in on the VERIFY side too), same as the pre-existing 4-outcome path."""
    pid = await _project(db, "rw-body-omit-verify")
    token = await handoff_module.mint_handoff_token(db, pid, body="the real body")

    result = await handoff_module.verify_handoff_token(db, token, pid)
    assert result == {"valid": True, "reason": "ok"}


@pytest.mark.asyncio
async def test_four_preexisting_token_outcomes_unchanged_with_body_param_present(db):
    """The pre-existing not_found/expired/already_consumed/wrong_project
    distinctions must survive the body-hash extension untouched."""
    pid = await _project(db, "rw-four-outcomes")
    other_pid = await _project(db, "rw-four-outcomes-other")

    # not_found
    result = await handoff_module.verify_handoff_token(db, "never-issued-token", pid)
    assert result == {"valid": False, "reason": "not_found"}

    # wrong_project
    token = await handoff_module.mint_handoff_token(db, pid, body="body")
    result = await handoff_module.verify_handoff_token(db, token, other_pid, body="body")
    assert result == {"valid": False, "reason": "wrong_project"}

    # already_consumed — the SAME token, now verified for the right project.
    result = await handoff_module.verify_handoff_token(db, token, pid, body="body")
    assert result == {"valid": True, "reason": "ok"}
    result = await handoff_module.verify_handoff_token(db, token, pid, body="body")
    assert result == {"valid": False, "reason": "already_consumed"}


# ---------------------------------------------------------------------------
# 7-9. Terminal run / no snapshot / unknown run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_terminal_run_cannot_be_resumed(db):
    pid = await _project(db, "rw-terminal")
    run = await _run(db, pid)
    await db_module.advance_wave_run_status(db, run["id"], "aborted")

    with pytest.raises(ValueError, match="terminal"):
        await db_module.check_wave_resume(db, run["id"])


@pytest.mark.asyncio
async def test_no_pinned_snapshot_refuses(db):
    pid = await _project(db, "rw-no-snapshot")
    run = await db_module.create_wave_run(db, pid)  # no snapshot=

    with pytest.raises(ValueError, match="no board snapshot pinned"):
        await db_module.check_wave_resume(db, run["id"])


@pytest.mark.asyncio
async def test_unknown_wave_run_raises(db):
    with pytest.raises(ValueError, match="not found"):
        await db_module.check_wave_resume(db, "no-such-run")


# ---------------------------------------------------------------------------
# 10. MCP registration
# ---------------------------------------------------------------------------

def test_resume_wave_registered_with_full_metadata():
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    assert "resume_wave" in by_name

    tool = by_name["resume_wave"]
    assert tool["description"]
    assert tool["inputSchema"]["type"] == "object"
    assert tool["inputSchema"]["required"] == ["wave_run_id"]
    assert "goal_token" in tool["inputSchema"]["properties"]
    assert "presented_body" in tool["inputSchema"]["properties"]
    assert "resume_wave" in _TOOL_EXAMPLES
    assert "resume_wave(" in _TOOL_EXAMPLES["resume_wave"]
    assert _TOOL_CATEGORY.get("resume_wave") == "sprint-management"
    assert _TOOL_ROLE_RELEVANCE.get("resume_wave") == "executor"
    assert _TITLE_OVERRIDES.get("resume_wave") == "Resume Wave"


# ---------------------------------------------------------------------------
# 11. MCP dispatch end-to-end: happy path then stale-board failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_resume_wave_happy_then_stale(db):
    pid = await _project(db, "rw-mcp-roundtrip")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: mcp item")

    started = await srv._dispatch_mcp_tool(
        "start_wave_run",
        {"project_id": pid, "item_ids": [item["id"]]},
        db, "/tmp",
    )
    assert "error" not in started
    wave_run_id = started["wave_run_id"]

    ok = await srv._dispatch_mcp_tool(
        "resume_wave", {"wave_run_id": wave_run_id}, db, "/tmp",
    )
    assert ok["resumable"] is True
    assert ok["resume_delta"]["changed"] is False

    # Board moves: a planner injects a new item mid-wave.
    await db_module.add_sprint_item(db, pid, "v1", "FEAT: injected after start")

    stale = await srv._dispatch_mcp_tool(
        "resume_wave", {"wave_run_id": wave_run_id}, db, "/tmp",
    )
    assert stale["resumable"] is False
    assert "error" in stale
    assert stale["reasons"]
    assert stale["resume_delta"]["changed"] is True


@pytest.mark.asyncio
async def test_mcp_resume_wave_requires_wave_run_id(db):
    result = await srv._dispatch_mcp_tool("resume_wave", {}, db, "/tmp")
    assert "wave_run_id" in result.get("error", "")


@pytest.mark.asyncio
async def test_mcp_resume_wave_unknown_run(db):
    result = await srv._dispatch_mcp_tool(
        "resume_wave", {"wave_run_id": "no-such-run"}, db, "/tmp",
    )
    assert result["resumable"] is False
    assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# 12. MCP dispatch: goal_token + presented_body wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_resume_wave_token_body_match_succeeds(db):
    pid = await _project(db, "rw-mcp-token-ok")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: token item")
    started = await srv._dispatch_mcp_tool(
        "start_wave_run", {"project_id": pid, "item_ids": [item["id"]]}, db, "/tmp",
    )
    wave_run_id = started["wave_run_id"]

    token = await handoff_module.mint_handoff_token(db, pid, body="/goal the real body")

    result = await srv._dispatch_mcp_tool(
        "resume_wave",
        {
            "wave_run_id": wave_run_id,
            "goal_token": token,
            "presented_body": "/goal the real body",
        },
        db, "/tmp",
    )
    assert result["resumable"] is True
    assert result["token_check"] == {"valid": True, "reason": "ok"}


@pytest.mark.asyncio
async def test_mcp_resume_wave_token_body_mismatch_refuses(db):
    pid = await _project(db, "rw-mcp-token-bad")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: token item")
    started = await srv._dispatch_mcp_tool(
        "start_wave_run", {"project_id": pid, "item_ids": [item["id"]]}, db, "/tmp",
    )
    wave_run_id = started["wave_run_id"]

    token = await handoff_module.mint_handoff_token(db, pid, body="/goal the real body")

    result = await srv._dispatch_mcp_tool(
        "resume_wave",
        {
            "wave_run_id": wave_run_id,
            "goal_token": token,
            "presented_body": "/goal an EDITED body",
        },
        db, "/tmp",
    )
    assert result["resumable"] is False
    assert result["token_check"]["reason"] == "body_mismatch"
    assert "body_mismatch" in result["error"] or "edited" in result["error"].lower()


@pytest.mark.asyncio
async def test_mcp_resume_wave_token_wrong_project_refuses(db):
    pid = await _project(db, "rw-mcp-token-wrongproj")
    other_pid = await _project(db, "rw-mcp-token-wrongproj-other")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: token item")
    started = await srv._dispatch_mcp_tool(
        "start_wave_run", {"project_id": pid, "item_ids": [item["id"]]}, db, "/tmp",
    )
    wave_run_id = started["wave_run_id"]

    token = await handoff_module.mint_handoff_token(db, other_pid)

    result = await srv._dispatch_mcp_tool(
        "resume_wave",
        {"wave_run_id": wave_run_id, "goal_token": token},
        db, "/tmp",
    )
    assert result["resumable"] is False
    assert result["token_check"]["reason"] == "wrong_project"


@pytest.mark.asyncio
async def test_mcp_resume_wave_token_already_consumed_hint(db):
    pid = await _project(db, "rw-mcp-token-consumed")
    item = await db_module.add_sprint_item(db, pid, "v1", "FEAT: token item")
    started = await srv._dispatch_mcp_tool(
        "start_wave_run", {"project_id": pid, "item_ids": [item["id"]]}, db, "/tmp",
    )
    wave_run_id = started["wave_run_id"]

    token = await handoff_module.mint_handoff_token(db, pid)
    # Consume it once directly.
    first = await handoff_module.verify_handoff_token(db, token, pid)
    assert first["valid"] is True

    result = await srv._dispatch_mcp_tool(
        "resume_wave",
        {"wave_run_id": wave_run_id, "goal_token": token},
        db, "/tmp",
    )
    assert result["resumable"] is False
    assert result["token_check"]["reason"] == "already_consumed"
    # b763d2ba framing: not an automatic spoofing verdict.
    assert "sibling" in result["error"].lower()
