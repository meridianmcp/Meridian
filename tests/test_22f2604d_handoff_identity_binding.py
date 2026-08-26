"""Tests for 22f2604d — handoff identity binding and trusted-vs-pasted
delivery separation.

Confirmed incident: a receiving session was handed a pasted /loop /goal
block where a genuine goal_token returned body_mismatch, and the block's
own <project_start_config> tag pointed at a DIFFERENT project (the parent
Meridian checkout) than the one the actual task belonged to (a separate
sibling paper project). The receiver correctly refused execution — but
nothing in the server's own contract made that refusal MECHANICAL rather
than incidental: verify_handoff_token's body_hash check only fires when a
body_hash was recorded at mint time, and accept_handoff_envelope's own
docstring previously documented "no independent project/tenant identity
check" beyond the token's own wrong_project result.

This file covers:
  - meridian.handoff.check_project_start_config_identity (the new pure
    self-consistency check) directly.
  - accept_handoff_envelope's new identity-binding step (2), including the
    exact incident shape: a genuine, correctly-scoped token PLUS a body
    whose own <project_start_config> tag disagrees with that same
    project_id — FOREIGN_PROJECT_CONFIG, distinct from wrong_project and
    from BODY_HASH_MISMATCH.
  - the trusted-vs-pasted delivery_source/is_trusted_channel markers on
    load_handoff (trusted) vs accept_handoff_envelope (never trusted).
  - two projects with different repo roots (requirement 7's fixture list).
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
from meridian.mcp import handler as mcp_handler


def _tag(project_id="proj-x", repo_path="/repo/x", **overrides):
    attrs = {
        "project_id": project_id,
        "project_name": "unset",
        "version": "unscoped",
        "repo_path": repo_path,
        "test_cmd": "pixi run test",
        "shell": "unset",
    }
    attrs.update(overrides)
    order = ["project_id", "project_name", "version", "repo_path", "test_cmd", "shell"]
    attr_str = " ".join(f'{k}="{attrs[k]}"' for k in order)
    return f"<project_start_config {attr_str} />"


# ---------------------------------------------------------------------------
# check_project_start_config_identity — pure unit behavior
# ---------------------------------------------------------------------------


def test_identity_check_skipped_when_no_tag_present():
    result = handoff_module.check_project_start_config_identity(
        "no tag here at all", "proj-a",
    )
    assert result == {"checked": False, "consistent": True, "found": None, "reasons": []}


def test_identity_check_skipped_when_body_is_none_or_empty():
    assert handoff_module.check_project_start_config_identity(None, "proj-a")["checked"] is False
    assert handoff_module.check_project_start_config_identity("", "proj-a")["checked"] is False


def test_identity_check_consistent_when_project_id_matches():
    body = "some goal text\n" + _tag(project_id="proj-a")
    result = handoff_module.check_project_start_config_identity(body, "proj-a")
    assert result["checked"] is True
    assert result["consistent"] is True
    assert result["reasons"] == []
    assert result["found"]["project_id"] == "proj-a"


def test_identity_check_flags_foreign_project_id():
    body = "some goal text\n" + _tag(project_id="parent-meridian-checkout")
    result = handoff_module.check_project_start_config_identity(body, "sibling-paper-project")
    assert result["checked"] is True
    assert result["consistent"] is False
    assert len(result["reasons"]) == 1
    assert "parent-meridian-checkout" in result["reasons"][0]
    assert "sibling-paper-project" in result["reasons"][0]


def test_identity_check_flags_foreign_repo_path_when_expected_given():
    body = "goal text\n" + _tag(project_id="proj-a", repo_path="C:/repo/meridian")
    result = handoff_module.check_project_start_config_identity(
        body, "proj-a", expected_repo_path="C:/repo/paper-project",
    )
    assert result["checked"] is True
    assert result["consistent"] is False
    assert any("repo_path" in r for r in result["reasons"])


def test_identity_check_repo_path_not_checked_when_expected_repo_path_omitted():
    # Same disagreeing repo_path as above, but the caller never supplied its
    # own expected_repo_path — nothing to compare against, so no failure.
    body = "goal text\n" + _tag(project_id="proj-a", repo_path="C:/repo/meridian")
    result = handoff_module.check_project_start_config_identity(body, "proj-a")
    assert result["consistent"] is True


def test_identity_check_ignores_unset_repo_path_sentinel():
    # _build_project_start_config_clause emits repo_path="unset" when no
    # repo_path was configured — never treat that as a real disagreement.
    body = "goal text\n" + _tag(project_id="proj-a", repo_path="unset")
    result = handoff_module.check_project_start_config_identity(
        body, "proj-a", expected_repo_path="C:/repo/anything",
    )
    assert result["consistent"] is True


# ---------------------------------------------------------------------------
# accept_handoff_envelope — the exact incident shape:
# genuine, correctly-scoped token + foreign project_start_config.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_flags_foreign_project_config_even_with_valid_token(db):
    """The core 22f2604d scenario: goal_token IS genuine and IS scoped to
    THIS project_id (would pass verify_handoff_token outright), but the
    presented body's own <project_start_config> tag names a DIFFERENT
    project — must be rejected as FOREIGN_PROJECT_CONFIG, not silently
    accepted just because the token checked out."""
    p = await db_module.create_project(db, "identity-incident-real-project")
    sibling = await db_module.create_project(db, "identity-incident-sibling-paper")
    body = "some /goal content\n" + _tag(project_id=sibling["id"])
    # Token minted correctly for p (no body_hash recorded — mirrors a token
    # minted independently of this particular reassembled body, e.g. a
    # /loop-style wrapper that spliced in a stale sibling's config text).
    token = await handoff_module.mint_handoff_token(db, p["id"])

    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], goal_token=token, presented_body=body,
    )
    assert result["accepted"] is False
    assert result["result"] == handoff_module.ACCEPT_RESULT_FOREIGN_PROJECT_CONFIG
    # The token itself was genuine and correctly scoped — proving this is a
    # DISTINCT failure mode from wrong_project/not_found/body_mismatch.
    assert result["token_check"]["valid"] is True
    assert result["identity_check"]["checked"] is True
    assert result["identity_check"]["consistent"] is False
    assert sibling["id"] in result["reasons"][0]


@pytest.mark.asyncio
async def test_accept_flags_foreign_project_config_without_any_token():
    """The identity check runs even when no goal_token is supplied at all —
    a caller checking board/tools state with a foreign body must still be
    protected, not just a caller who happened to also pass a token."""
    db = None  # not needed: no DB-backed calls occur when goal_token is absent
    body = "goal content\n" + _tag(project_id="foreign-project")
    result = await handoff_module.accept_handoff_envelope(
        object(), "my-real-project", presented_body=body,
    )
    assert result["accepted"] is False
    assert result["result"] == handoff_module.ACCEPT_RESULT_FOREIGN_PROJECT_CONFIG
    assert result["token_check"] is None


@pytest.mark.asyncio
async def test_accept_passes_identity_check_when_project_start_config_matches(db):
    p = await db_module.create_project(db, "identity-matching-project")
    token = await handoff_module.mint_handoff_token(db, p["id"])
    body = "goal content\n" + _tag(project_id=p["id"])

    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], goal_token=token, presented_body=body,
    )
    assert result["accepted"] is True
    assert result["identity_check"]["checked"] is True
    assert result["identity_check"]["consistent"] is True


@pytest.mark.asyncio
async def test_accept_identity_check_none_when_body_has_no_project_start_config(db):
    """A handoff mode that never emits the tag (full/starter) must not be
    treated as suspicious merely for lacking it."""
    p = await db_module.create_project(db, "identity-no-tag-project")
    token = await handoff_module.mint_handoff_token(db, p["id"])
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], goal_token=token, presented_body="plain goal text, no tag",
    )
    assert result["accepted"] is True
    assert result["identity_check"]["checked"] is False


@pytest.mark.asyncio
async def test_identity_check_runs_before_capability_and_board_checks(db):
    """Precedence: identity binding fires ahead of capability/tool-manifest/
    board checks, mirroring the existing token-check-first ordering."""
    p = await db_module.create_project(db, "identity-precedence-project")
    sibling = await db_module.create_project(db, "identity-precedence-sibling")
    body = "goal\n" + _tag(project_id=sibling["id"])
    token = await handoff_module.mint_handoff_token(db, p["id"])

    result = await handoff_module.accept_handoff_envelope(
        db, p["id"],
        goal_token=token,
        presented_body=body,
        required_tools=["meridian"],
        available_tools=[],  # would fail CAPABILITY_UNAVAILABLE if reached
    )
    assert result["result"] == handoff_module.ACCEPT_RESULT_FOREIGN_PROJECT_CONFIG
    assert result["capability_check"] is None  # never reached


# ---------------------------------------------------------------------------
# Two projects with different repo roots (requirement 7 fixture)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_flags_foreign_repo_root_across_two_projects(db):
    p_a = await db_module.create_project(db, "repo-root-project-a")
    p_b = await db_module.create_project(db, "repo-root-project-b")
    token = await handoff_module.mint_handoff_token(db, p_a["id"])
    body = "goal\n" + _tag(project_id=p_a["id"], repo_path="C:/repos/project-a")

    # Receiver's OWN independently-known repo root disagrees with the body's
    # declared repo_path, even though project_id itself matches.
    result = await handoff_module.accept_handoff_envelope(
        db, p_a["id"], goal_token=token, presented_body=body,
        expected_repo_path="C:/repos/project-b",
    )
    assert result["accepted"] is False
    assert result["result"] == handoff_module.ACCEPT_RESULT_FOREIGN_PROJECT_CONFIG
    assert any("repo_path" in r for r in result["reasons"])


# ---------------------------------------------------------------------------
# Wrong-project token and body_mismatch — regression coverage per this
# item's minimum fixture list (already covered elsewhere; re-asserted here
# alongside FOREIGN_PROJECT_CONFIG so the three distinct failure categories
# are visibly side-by-side and don't collapse into each other).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_project_token_is_stale_handoff_not_foreign_project_config(db):
    p1 = await db_module.create_project(db, "distinct-categories-proj-a")
    p2 = await db_module.create_project(db, "distinct-categories-proj-b")
    token = await handoff_module.mint_handoff_token(db, p1["id"])

    result = await handoff_module.accept_handoff_envelope(
        db, p2["id"], goal_token=token,
    )
    assert result["result"] == handoff_module.ACCEPT_RESULT_STALE_HANDOFF
    assert result["token_check"]["reason"] == "wrong_project"
    assert result["identity_check"] is None  # never reached — token check short-circuited


@pytest.mark.asyncio
async def test_body_mismatch_still_takes_precedence_over_identity_check(db):
    """A tampered body_hash (efaa918a) is caught at step 1 (token check)
    before step 2 (identity binding) ever runs — token genuineness comes
    first, matching the fixed precedence order."""
    p = await db_module.create_project(db, "distinct-categories-body-mismatch")
    token = await handoff_module.mint_handoff_token(db, p["id"], body="original text")
    tampered = "tampered text\n" + _tag(project_id="some-other-project")

    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], goal_token=token, presented_body=tampered,
    )
    assert result["result"] == handoff_module.ACCEPT_RESULT_BODY_HASH_MISMATCH
    assert result["identity_check"] is None  # never reached


@pytest.mark.asyncio
async def test_tampered_project_start_config_alone_no_body_hash_recorded(db):
    """Requirement 7 fixture: 'tampered project_start_config'. No body_hash
    was recorded at mint time (mirrors most real call sites — body_hash is
    opt-in via mint_handoff_token's `body` kwarg), so step 1's body_mismatch
    path is not even available; the identity-binding step is the ONLY thing
    standing between this tampered body and silent acceptance."""
    p = await db_module.create_project(db, "tampered-config-no-hash")
    token = await handoff_module.mint_handoff_token(db, p["id"])  # no body= given
    tampered_body = "legit-looking goal text\n" + _tag(project_id="attacker-controlled-project")

    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], goal_token=token, presented_body=tampered_body,
    )
    assert result["accepted"] is False
    assert result["result"] == handoff_module.ACCEPT_RESULT_FOREIGN_PROJECT_CONFIG


# ---------------------------------------------------------------------------
# Trusted-vs-pasted delivery separation (requirement 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_handoff_envelope_always_reports_untrusted_channel(db):
    p = await db_module.create_project(db, "trust-marker-accept")
    result = await handoff_module.accept_handoff_envelope(db, p["id"])
    assert result["is_trusted_channel"] is False
    assert result["delivery_source"] == "chat_paste"


@pytest.mark.asyncio
async def test_accept_handoff_envelope_delivery_source_is_overridable(db):
    p = await db_module.create_project(db, "trust-marker-accept-override")
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], delivery_source="task_notification",
    )
    assert result["is_trusted_channel"] is False
    assert result["delivery_source"] == "task_notification"


@pytest.mark.asyncio
async def test_load_handoff_reports_trusted_channel(db, tmp_path):
    p = await db_module.create_project(db, "trust-marker-load-handoff")
    result = await mcp_handler._handle_task_tools(
        "load_handoff", {"project_id": p["id"]}, db, str(tmp_path),
        tenant=None, _mcp_tenant_id=None,
    )
    assert result["is_trusted_channel"] is True
    assert result["delivery_source"] == "mcp_load_handoff"


@pytest.mark.asyncio
async def test_mcp_accept_handoff_dispatch_passes_through_identity_params(db, tmp_path):
    """The MCP dispatch layer (mcp/handler.py) must actually forward
    expected_repo_path/delivery_source to accept_handoff_envelope, not just
    the underlying function support them."""
    p = await db_module.create_project(db, "trust-marker-mcp-dispatch")
    sibling = await db_module.create_project(db, "trust-marker-mcp-sibling")
    token = await handoff_module.mint_handoff_token(db, p["id"])
    body = "goal\n" + _tag(project_id=sibling["id"])

    result = await mcp_handler._handle_task_tools(
        "accept_handoff",
        {
            "project_id": p["id"],
            "goal_token": token,
            "presented_body": body,
            "delivery_source": "task_notification",
        },
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert result["result"] == handoff_module.ACCEPT_RESULT_FOREIGN_PROJECT_CONFIG
    assert result["delivery_source"] == "task_notification"
    assert result["is_trusted_channel"] is False
