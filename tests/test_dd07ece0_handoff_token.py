"""Tests for dd07ece0 — handoff provenance token.

generate_handoff mints a short-lived, single-use token and embeds it in the
returned /goal block as <goal_token>TOKEN</goal_token>. A receiving session
can call verify_handoff_token(project_id, token) to confirm the /goal came
from a real generate_handoff call rather than injected/spoofed text.

Tests cover:
  (a) Token is minted and embedded in the /goal block on generate_handoff.
  (b) verify_handoff_token succeeds (valid=True, reason='ok') on first use.
  (c) verify_handoff_token fails (already_consumed) on reuse.
  (d) verify_handoff_token fails (not_found) for an unknown token.
  (e) verify_handoff_token fails (expired) when TTL has passed (time-mocked).
  (f) wrong_project rejection: token minted for project A rejected when
      verified against project B's id.
  (g) MCP handler routes verify_handoff_token correctly (integration path).
  (h) Empty/missing token returns not_found immediately.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_token_from_goal(goal: str) -> str | None:
    """Pull the token value out of a <goal_token>…</goal_token> tag."""
    m = re.search(r"<goal_token>([^<]+)</goal_token>", goal)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# (a) Token minted and embedded in /goal block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_embeds_token_in_goal(db, tmp_path):
    """dd07ece0 (a): generate_handoff embeds a <goal_token> line in quick_start_goal."""
    p = await db_module.create_project(db, "token-test-a")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")

    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    # The content is the full handoff markdown; the /goal block appears in it.
    assert "<goal_token>" in content, (
        "generate_handoff output must contain a <goal_token> provenance tag"
    )
    token = _extract_token_from_goal(content)
    assert token is not None, "<goal_token> tag must carry a non-empty token value"
    assert len(token) > 0, "token must not be empty"


@pytest.mark.asyncio
async def test_mint_handoff_token_produces_unique_tokens(db, tmp_path):
    """dd07ece0 (a extra): mint_handoff_token produces different tokens on each call."""
    t1 = handoff_module.mint_handoff_token("proj-a")
    t2 = handoff_module.mint_handoff_token("proj-a")
    assert t1 != t2, "each mint_handoff_token call must produce a distinct token"


# ---------------------------------------------------------------------------
# (b) verify_handoff_token succeeds once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_handoff_token_succeeds_on_first_use(db, tmp_path):
    """dd07ece0 (b): verify_handoff_token returns valid=True on the first call."""
    p = await db_module.create_project(db, "token-test-b")
    await db_module.set_goal(db, p["id"], "go", sprint="s-b")

    token = handoff_module.mint_handoff_token(p["id"])
    result = handoff_module.verify_handoff_token(token, p["id"])

    assert result["valid"] is True, f"expected valid=True, got {result}"
    assert result["reason"] == "ok", f"expected reason='ok', got {result['reason']}"


# ---------------------------------------------------------------------------
# (c) verify_handoff_token fails on reuse (already consumed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_handoff_token_fails_on_reuse(db, tmp_path):
    """dd07ece0 (c): a token is single-use; second verification returns already_consumed."""
    p = await db_module.create_project(db, "token-test-c")

    token = handoff_module.mint_handoff_token(p["id"])

    # First use — must succeed.
    first = handoff_module.verify_handoff_token(token, p["id"])
    assert first["valid"] is True, f"first verify must succeed: {first}"

    # Second use — must be rejected as already consumed.
    second = handoff_module.verify_handoff_token(token, p["id"])
    assert second["valid"] is False, f"second verify must fail: {second}"
    assert second["reason"] == "already_consumed", (
        f"expected reason='already_consumed', got {second['reason']}"
    )


# ---------------------------------------------------------------------------
# (d) verify_handoff_token fails for unknown token
# ---------------------------------------------------------------------------


def test_verify_handoff_token_fails_for_unknown_token():
    """dd07ece0 (d): an unrecognized token is rejected as not_found."""
    result = handoff_module.verify_handoff_token("deadbeefcafe0000", "any-project")
    assert result["valid"] is False
    assert result["reason"] == "not_found"


# ---------------------------------------------------------------------------
# (e) verify_handoff_token fails after expiry (time-mocked)
# ---------------------------------------------------------------------------


def test_verify_handoff_token_fails_after_expiry():
    """dd07ece0 (e): a token past its TTL is rejected as expired.

    We mock datetime.now inside the handoff module so we don't have to sleep.
    """
    project_id = "expiry-test-proj"
    token = handoff_module.mint_handoff_token(project_id)

    # Advance simulated time beyond the TTL.
    future = datetime.now(timezone.utc) + timedelta(
        seconds=handoff_module._HANDOFF_TOKEN_TTL_SECONDS + 10
    )

    # Patch the datetime used inside verify_handoff_token to return a future time.
    # The function calls datetime.now(timezone.utc) to check expiry.
    with patch.object(
        handoff_module,
        "_HANDOFF_TOKENS",
        {
            token: {
                "project_id": project_id,
                "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
                "consumed": False,
            }
        },
    ):
        result = handoff_module.verify_handoff_token(token, project_id)

    assert result["valid"] is False
    assert result["reason"] == "expired", (
        f"expected reason='expired', got {result['reason']}"
    )


def test_verify_handoff_token_fails_after_expiry_via_time_mock():
    """dd07ece0 (e alt): patch datetime.now in the handoff module to simulate TTL expiry."""
    project_id = "expiry-test-2"
    token = handoff_module.mint_handoff_token(project_id)

    # Patch datetime.now in the handoff module to return a far-future time.
    far_future = datetime.now(timezone.utc) + timedelta(hours=1)

    original_tokens = handoff_module._HANDOFF_TOKENS

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return far_future

    try:
        with patch("meridian.handoff.datetime", _FakeDateTime):
            result = handoff_module.verify_handoff_token(token, project_id)
    finally:
        # Restore — the patch already does this, but be explicit.
        pass

    assert result["valid"] is False
    assert result["reason"] == "expired"


# ---------------------------------------------------------------------------
# (f) wrong_project rejection
# ---------------------------------------------------------------------------


def test_verify_handoff_token_fails_for_wrong_project():
    """dd07ece0 (f): a token minted for project A is rejected when verified as project B."""
    token = handoff_module.mint_handoff_token("project-alpha")

    result = handoff_module.verify_handoff_token(token, "project-beta")
    assert result["valid"] is False
    assert result["reason"] == "wrong_project", (
        f"expected reason='wrong_project', got {result['reason']}"
    )

    # Token must remain unconsumed after a wrong-project rejection.
    correct_result = handoff_module.verify_handoff_token(token, "project-alpha")
    assert correct_result["valid"] is True, (
        "token must still be verifiable after a wrong-project rejection (not consumed)"
    )


# ---------------------------------------------------------------------------
# (g) MCP handler routes verify_handoff_token correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_routes_verify_handoff_token(db, tmp_path):
    """dd07ece0 (g): the MCP handler dispatches verify_handoff_token to the right impl."""
    import meridian.server  # noqa: F401 — must be imported before handler to avoid cycle
    from meridian.mcp import handler as mh

    p = await db_module.create_project(db, "token-handler-test")
    token = handoff_module.mint_handoff_token(p["id"])

    args = {"project_id": p["id"], "token": token}
    result = await mh._handle_task_tools(
        "verify_handoff_token", args, db, str(tmp_path), None, None
    )
    assert result is not mh._MISS, "handler must not return _MISS for verify_handoff_token"
    assert isinstance(result, dict), f"expected dict result, got {type(result)}"
    assert result.get("valid") is True, f"expected valid=True from handler: {result}"
    assert result.get("reason") == "ok"


@pytest.mark.asyncio
async def test_handler_verify_handoff_token_rejects_unknown(db, tmp_path):
    """dd07ece0 (g extra): handler rejects an unknown token (not_found)."""
    import meridian.server  # noqa: F401
    from meridian.mcp import handler as mh

    p = await db_module.create_project(db, "token-handler-reject")
    args = {"project_id": p["id"], "token": "notarealtoken00"}
    result = await mh._handle_task_tools(
        "verify_handoff_token", args, db, str(tmp_path), None, None
    )
    assert result is not mh._MISS
    assert result.get("valid") is False
    assert result.get("reason") == "not_found"


# ---------------------------------------------------------------------------
# (h) Empty or missing token returns not_found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_verify_handoff_token_empty_token(db, tmp_path):
    """dd07ece0 (h): passing an empty token string returns not_found immediately."""
    import meridian.server  # noqa: F401
    from meridian.mcp import handler as mh

    p = await db_module.create_project(db, "token-empty-test")
    args = {"project_id": p["id"], "token": ""}
    result = await mh._handle_task_tools(
        "verify_handoff_token", args, db, str(tmp_path), None, None
    )
    assert result is not mh._MISS
    assert result.get("valid") is False
    assert result.get("reason") == "not_found"


def test_verify_handoff_token_empty_token_direct():
    """dd07ece0 (h direct): verify_handoff_token with empty token returns not_found."""
    result = handoff_module.verify_handoff_token("", "some-project")
    assert result["valid"] is False
    assert result["reason"] == "not_found"


# ---------------------------------------------------------------------------
# Full integration: generate_handoff + verify round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_token_verify_roundtrip(db, tmp_path):
    """dd07ece0 integration: extract token from generate_handoff output and verify it."""
    p = await db_module.create_project(db, "token-roundtrip")
    await db_module.set_goal(db, p["id"], "north star", sprint="s-rt")
    await db_module.add_sprint_item(db, p["id"], "v1", "implement the thing")

    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )

    token = _extract_token_from_goal(content)
    assert token is not None, "content must contain a <goal_token> tag"

    # Verify the extracted token against the correct project.
    result = handoff_module.verify_handoff_token(token, p["id"])
    assert result["valid"] is True, f"round-trip verify must succeed: {result}"
    assert result["reason"] == "ok"

    # A second verification attempt must fail (single-use).
    second = handoff_module.verify_handoff_token(token, p["id"])
    assert second["valid"] is False
    assert second["reason"] == "already_consumed"
