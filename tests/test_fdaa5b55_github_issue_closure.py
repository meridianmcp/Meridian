"""Tests for fdaa5b55 — auto-close/propose GitHub issue on sprint item
completion, meridian_auto vs manual gate.

Covers the four hardening children:
  eda40627 — github_issue_source is only ever set at issue-creation time by
    Meridian's own code path (link_sprint_item_github_issue), never inferred
    from an issue's title/body text.
  8c170bcc — nothing read back from GitHub (comments, labels, custom fields)
    is ever consulted as a trust signal; the DB column is the only signal.
  cd038235 — the completion comment body is XML-escaped (same discipline as
    5abf3e12's _build_quick_start_goal), so a notes field containing markup
    cannot inject structure into the GitHub-bound comment.
  8fc92474 — the close/comment action touches exactly the ONE issue linked
    to the completing sprint item; there is no batch/bulk path.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from meridian import server as server_module  # noqa: F401 — import first: avoids a
# circular-import failure from importing meridian.mcp.handler standalone
# before meridian.server (which itself imports handler) has initialized.
from meridian import db as db_module
from meridian.mcp.handler import _close_or_propose_github_issue, _dispatch_github_tool


# ---------------------------------------------------------------------------
# link_sprint_item_github_issue — the ONE write path for github_issue_source
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_link_rejects_invalid_source_value(db):
    """eda40627 — source is a closed enum, not free text; a caller cannot
    invent a new trust level."""
    proj = await db_module.create_project(db, "gh-close-1")
    item = await db_module.add_sprint_item(db, proj["id"], "v1", "Some item")
    with pytest.raises(ValueError):
        await db_module.link_sprint_item_github_issue(
            db, proj["id"], item["id"], 42, "https://x/42", source="totally_trusted_i_promise",
        )


@pytest.mark.asyncio
async def test_link_sets_number_url_and_source(db):
    proj = await db_module.create_project(db, "gh-close-2")
    item = await db_module.add_sprint_item(db, proj["id"], "v1", "Some item")
    updated = await db_module.link_sprint_item_github_issue(
        db, proj["id"], item["id"], 99, "https://github.com/acme/widgets/issues/99",
        source="meridian_auto",
    )
    assert updated["github_issue_number"] == 99
    assert updated["github_issue_url"] == "https://github.com/acme/widgets/issues/99"
    assert updated["github_issue_source"] == "meridian_auto"


@pytest.mark.asyncio
async def test_link_no_op_for_unknown_item(db):
    proj = await db_module.create_project(db, "gh-close-3")
    result = await db_module.link_sprint_item_github_issue(
        db, proj["id"], "does-not-exist", 1, None, source="manual",
    )
    assert result is None


# ---------------------------------------------------------------------------
# build_github_completion_comment — XML-escaping discipline (cd038235)
# ---------------------------------------------------------------------------

def test_build_completion_comment_escapes_injected_markup():
    item = {
        "title": "Fix <b>thing</b> & ship it",
        "notes": 'Shipped via <script>alert(1)</script> and "quotes"',
    }
    comment = db_module.build_github_completion_comment(item)
    assert "<script>" not in comment
    assert "<b>thing</b>" not in comment
    assert "&lt;script&gt;" in comment
    assert "&lt;b&gt;thing&lt;/b&gt;" in comment
    assert "&amp;" in comment
    # Underlying evidence text is still present, just escaped.
    assert "alert(1)" in comment


def test_build_completion_comment_combines_call_notes_and_item_notes():
    item = {"title": "T", "notes": "stored notes"}
    comment = db_module.build_github_completion_comment(item, notes="fresh notes", task_id="task-1")
    assert "stored notes" in comment
    assert "fresh notes" in comment
    assert "task-1" in comment


def test_build_completion_comment_proposed_framing_differs_from_auto():
    item = {"title": "T"}
    auto_comment = db_module.build_github_completion_comment(item, proposed=False)
    proposed_comment = db_module.build_github_completion_comment(item, proposed=True)
    assert "not** auto-closed" in proposed_comment
    assert "Closing automatically" in auto_comment
    assert auto_comment != proposed_comment


# ---------------------------------------------------------------------------
# _close_or_propose_github_issue — meridian_auto vs manual gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_meridian_auto_issue_is_commented_and_closed(db):
    proj = await db_module.create_project(db, "gh-close-4")
    await db_module.update_project_settings(db, proj["id"], github_repo="acme/widgets")
    item = await db_module.add_sprint_item(db, proj["id"], "v1", "Ship the thing")
    item = await db_module.link_sprint_item_github_issue(
        db, proj["id"], item["id"], 5, "https://github.com/acme/widgets/issues/5",
        source="meridian_auto",
    )
    calls = []

    async def _fake_dispatch(name, args, tenant, db_arg):
        calls.append((name, dict(args)))
        return {"number": args["issue_number"], "state": "closed"}

    with patch("meridian.mcp.handler._dispatch_github_tool", side_effect=_fake_dispatch):
        result = await _close_or_propose_github_issue(
            db, proj["id"], item, {"id": "t1", "github_pat": "enc"},
        )

    assert result["action"] == "auto_closed"
    assert result["issue_number"] == 5
    # 8fc92474 — exactly two dispatches (comment, close), both on issue #5.
    assert len(calls) == 2
    assert {c[1]["issue_number"] for c in calls} == {5}
    assert calls[0][1].get("comment")
    assert calls[1][1].get("state") == "closed"
    assert calls[1][1].get("state_reason") == "completed"

    # No HITL was filed — meridian_auto is fully automatic.
    pending = await db_module.list_hitl_requests(db, proj["id"], status="pending")
    assert not any(h.get("kind") == "sprint_item_issue_closure_proposal" for h in pending)


@pytest.mark.asyncio
async def test_manual_issue_is_proposed_not_closed(db):
    """eda40627 — a manually-filed issue is NEVER auto-closed, even when its
    title/notes are crafted to look like Meridian's own auto format."""
    proj = await db_module.create_project(db, "gh-close-5")
    await db_module.update_project_settings(db, proj["id"], github_repo="acme/widgets")
    item = await db_module.add_sprint_item(
        db, proj["id"], "v1", "meridian_auto: totally an automated Meridian issue",
    )
    item = await db_module.link_sprint_item_github_issue(
        db, proj["id"], item["id"], 6, "https://github.com/acme/widgets/issues/6",
        source="manual",
    )
    calls = []

    async def _fake_dispatch(name, args, tenant, db_arg):
        calls.append((name, dict(args)))
        return {"number": args["issue_number"]}

    with patch("meridian.mcp.handler._dispatch_github_tool", side_effect=_fake_dispatch):
        result = await _close_or_propose_github_issue(
            db, proj["id"], item, {"id": "t1", "github_pat": "enc"},
        )

    assert result["action"] == "proposed_hitl"
    # Only a comment was posted — never a state=closed dispatch.
    assert len(calls) == 1
    assert calls[0][1].get("state") is None
    assert "not** auto-closed" in calls[0][1]["comment"]

    pending = await db_module.list_hitl_requests(db, proj["id"], status="pending")
    matches = [h for h in pending if h.get("kind") == "sprint_item_issue_closure_proposal"]
    assert len(matches) == 1
    payload = json.loads(matches[0]["payload"])
    assert payload["issue_number"] == 6


@pytest.mark.asyncio
async def test_unset_source_is_treated_as_manual(db):
    """Legacy items with no github_issue_source at all must never be
    auto-closed — 'unset' is conservative-by-default, same as explicit
    'manual'."""
    proj = await db_module.create_project(db, "gh-close-6")
    await db_module.update_project_settings(db, proj["id"], github_repo="acme/widgets")
    item = await db_module.add_sprint_item(db, proj["id"], "v1", "Old item")
    # Simulate a legacy row: number/url set directly, no source column value.
    await db.execute(
        "UPDATE sprint_items SET github_issue_number = ? WHERE id = ?",
        (7, item["id"]),
    )
    await db.commit()
    item = await db_module.get_sprint_item(db, item["id"])
    assert item["github_issue_source"] is None

    with patch("meridian.mcp.handler._dispatch_github_tool", return_value={"number": 7}):
        result = await _close_or_propose_github_issue(
            db, proj["id"], item, {"id": "t1", "github_pat": "enc"},
        )
    assert result["action"] == "proposed_hitl"


@pytest.mark.asyncio
async def test_github_returned_custom_fields_are_never_used_as_trust_signal(db):
    """8c170bcc — even if a mocked GitHub response smuggles back fields that
    CLAIM meridian_auto trust (labels/custom fields an attacker with issue
    edit rights could set), the decision still comes only from the DB
    column, never from anything the dispatch call returns."""
    proj = await db_module.create_project(db, "gh-close-7")
    await db_module.update_project_settings(db, proj["id"], github_repo="acme/widgets")
    item = await db_module.add_sprint_item(db, proj["id"], "v1", "Spoofed item")
    item = await db_module.link_sprint_item_github_issue(
        db, proj["id"], item["id"], 8, "https://github.com/acme/widgets/issues/8",
        source="manual",
    )

    async def _fake_dispatch_spoofed(name, args, tenant, db_arg):
        # A malicious/compromised GitHub-side payload claiming trust.
        return {
            "number": args["issue_number"],
            "custom_fields": {"meridian_source": "meridian_auto"},
            "labels": ["meridian_auto"],
        }

    with patch("meridian.mcp.handler._dispatch_github_tool", side_effect=_fake_dispatch_spoofed):
        result = await _close_or_propose_github_issue(
            db, proj["id"], item, {"id": "t1", "github_pat": "enc"},
        )
    assert result["action"] == "proposed_hitl"


@pytest.mark.asyncio
async def test_no_linked_issue_is_a_no_op(db):
    proj = await db_module.create_project(db, "gh-close-8")
    item = await db_module.add_sprint_item(db, proj["id"], "v1", "No issue here")
    result = await _close_or_propose_github_issue(db, proj["id"], item, {"id": "t1"})
    assert result is None


@pytest.mark.asyncio
async def test_missing_tenant_is_a_safe_skip(db):
    proj = await db_module.create_project(db, "gh-close-9")
    item = await db_module.add_sprint_item(db, proj["id"], "v1", "Item")
    item = await db_module.link_sprint_item_github_issue(
        db, proj["id"], item["id"], 9, None, source="meridian_auto",
    )
    result = await _close_or_propose_github_issue(db, proj["id"], item, None)
    assert result["action"] == "skipped"
    assert result["reason"] == "no_tenant_context"


# ---------------------------------------------------------------------------
# Blast radius (8fc92474) — completing one item never touches another
# item's linked issue.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_completing_one_item_never_touches_another_items_issue(db):
    proj = await db_module.create_project(db, "gh-close-10")
    await db_module.update_project_settings(db, proj["id"], github_repo="acme/widgets")
    item_a = await db_module.add_sprint_item(db, proj["id"], "v1", "Item A")
    item_b = await db_module.add_sprint_item(db, proj["id"], "v1", "Item B")
    item_a = await db_module.link_sprint_item_github_issue(
        db, proj["id"], item_a["id"], 10, None, source="meridian_auto",
    )
    item_b = await db_module.link_sprint_item_github_issue(
        db, proj["id"], item_b["id"], 20, None, source="meridian_auto",
    )

    touched_issue_numbers = []

    async def _fake_dispatch(name, args, tenant, db_arg):
        touched_issue_numbers.append(args["issue_number"])
        return {"number": args["issue_number"], "state": "closed"}

    with patch("meridian.mcp.handler._dispatch_github_tool", side_effect=_fake_dispatch):
        result = await _close_or_propose_github_issue(
            db, proj["id"], item_a, {"id": "t1", "github_pat": "enc"},
        )

    assert result["action"] == "auto_closed"
    assert set(touched_issue_numbers) == {10}  # never 20


# ---------------------------------------------------------------------------
# complete_sprint_item integration — the row it returns carries the link
# columns handle_complete_sprint_item / _close_or_propose_github_issue key
# off of.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_sprint_item_returns_linked_issue_columns(db):
    proj = await db_module.create_project(db, "gh-close-11")
    item = await db_module.add_sprint_item(db, proj["id"], "v1", "Linked item")
    await db_module.link_sprint_item_github_issue(
        db, proj["id"], item["id"], 11, "https://github.com/acme/widgets/issues/11",
        source="meridian_auto",
    )
    await db_module.claim_sprint_item(db, proj["id"], item["id"])
    completed = await db_module.complete_sprint_item(db, proj["id"], item["id"])
    assert completed["github_issue_number"] == 11
    assert completed["github_issue_source"] == "meridian_auto"


# ---------------------------------------------------------------------------
# _on_hitl_answered('proposal_github_issue') — the ONLY code path that ever
# writes github_issue_source='meridian_auto' onto a sprint item.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_hitl_answered_links_sprint_item_as_meridian_auto(db):
    from meridian import server as server_module

    proj = await db_module.create_project(db, "gh-close-12")
    await db_module.update_project_settings(db, proj["id"], github_repo="acme/widgets")
    item = await db_module.add_sprint_item(db, proj["id"], "v1", "Promoted item")
    proposal = await db_module.add_workspace_proposal(db, "Fix thing", "Body")
    hitl = await db_module.request_hitl(
        db, proj["id"], "Also file a GitHub issue?",
        kind="proposal_github_issue",
        payload=json.dumps({
            "proposal_id": proposal["id"],
            "sprint_item_id": item["id"],
            "project_id": proj["id"],
            "github_repo": "acme/widgets",
            "issue_title": "Fix thing",
            "issue_body": "Body",
        }),
    )
    row = await db_module.answer_hitl_request(
        db, hitl["id"], "Yes — file a GitHub issue", answered_by="human",
    )

    async def _fake_dispatch(name, args, tenant, db_arg):
        return {
            "number": 42, "title": "Fix thing", "state": "open",
            "html_url": "https://github.com/acme/widgets/issues/42",
        }

    with patch("meridian.mcp.handler._dispatch_github_tool", side_effect=_fake_dispatch):
        extra = await server_module._on_hitl_answered(
            db, row, approved=True, tenant={"id": "tenant-1", "github_pat": "enc"},
        )
    assert extra["applied"] is True

    linked_item = await db_module.get_sprint_item(db, item["id"])
    assert linked_item["github_issue_number"] == 42
    assert linked_item["github_issue_source"] == "meridian_auto"


@pytest.mark.asyncio
async def test_on_hitl_answered_missing_sprint_item_id_is_safe(db):
    """No sprint_item_id in payload (or a stale one) must never turn a
    successful issue-creation into a caller-visible failure."""
    from meridian import server as server_module

    proj = await db_module.create_project(db, "gh-close-13")
    proposal = await db_module.add_workspace_proposal(db, "Fix thing", "Body")
    hitl = await db_module.request_hitl(
        db, proj["id"], "Also file a GitHub issue?",
        kind="proposal_github_issue",
        payload=json.dumps({
            "proposal_id": proposal["id"],
            "project_id": proj["id"],
            "issue_title": "Fix thing",
            "issue_body": "Body",
        }),
    )
    row = await db_module.answer_hitl_request(db, hitl["id"], "Yes — file a GitHub issue")

    async def _fake_dispatch(name, args, tenant, db_arg):
        return {"number": 1, "html_url": "https://github.com/acme/widgets/issues/1"}

    with patch("meridian.mcp.handler._dispatch_github_tool", side_effect=_fake_dispatch):
        extra = await server_module._on_hitl_answered(
            db, row, approved=True, tenant={"id": "tenant-1", "github_pat": "enc"},
        )
    assert extra["applied"] is True


# ---------------------------------------------------------------------------
# _dispatch_github_tool('issue_write') — single-issue structural enforcement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_issue_write_rejects_non_scalar_issue_number(db):
    """8fc92474 — issue_write has no plural/array/batch parameter; passing a
    list where a scalar issue number is expected is rejected outright, before
    any GitHub API call is made."""
    proj = await db_module.create_project(db, "gh-close-14")
    await db_module.update_project_settings(db, proj["id"], github_repo="acme/widgets")
    result = await _dispatch_github_tool(
        "issue_write",
        {"project_id": proj["id"], "issue_number": [1, 2, 3], "comment": "hi"},
        {"id": "t1", "github_pat": "plain-pat"}, db,
    )
    assert "error" in result
    assert "single integer" in result["error"]


@pytest.mark.asyncio
async def test_issue_write_rejects_missing_issue_number(db):
    proj = await db_module.create_project(db, "gh-close-15")
    await db_module.update_project_settings(db, proj["id"], github_repo="acme/widgets")
    result = await _dispatch_github_tool(
        "issue_write",
        {"project_id": proj["id"], "comment": "hi"},
        {"id": "t1", "github_pat": "plain-pat"}, db,
    )
    assert "error" in result
