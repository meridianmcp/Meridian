"""Tests for 3999d90f — conditional proposal-to-GitHub-issue workflow via HITL.

When a code-related workspace proposal is promoted under a project with a
connected GitHub repo, promote_workspace_proposal fires a HITL asking whether
to also file a GitHub issue. When that HITL is answered "yes", server.py's
_on_hitl_answered files the issue via the GitHub tool and stores the number/
URL back on the proposal via db.set_proposal_github_issue.

Covers:
1. promote_workspace_proposal fires the HITL only when BOTH code-related
   (inferred touches_resources non-empty) AND github_repo is connected.
2. No HITL when either condition is missing.
3. set_proposal_github_issue persists issue_number/issue_url on the proposal.
4. _on_hitl_answered('proposal_github_issue') end-to-end: approved 'yes' +
   tenant -> creates the issue (mocked GitHub dispatch) and writes it back.
5. Declined answer / missing tenant / GitHub error -> no proposal mutation,
   never raises.
"""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from meridian import db as db_module
from meridian import server as server_module


def _mock_git_result(stdout: str):
    m = MagicMock()
    m.stdout = stdout
    return m


# ---------------------------------------------------------------------------
# promote_workspace_proposal — conditional HITL firing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_promote_fires_hitl_when_code_related_and_repo_connected(db):
    proj = await db_module.create_project(db, "gh-issue-proj-1")
    await db_module.update_project_settings(db, proj["id"], github_repo="acme/widgets")
    proposal = await db_module.add_workspace_proposal(
        db, "Dashboard rendering fix", "Fix the dashboard layout rendering"
    )

    with patch(
        "meridian.db.workspace.subprocess.run",
        return_value=_mock_git_result("meridian/static/dashboard.js\n"),
    ):
        result = await db_module.promote_workspace_proposal(
            db, proposal["id"], proj["id"],
            infer_touches_resources=True, file_github_issue=True,
        )

    hitl = result.get("github_issue_hitl")
    assert hitl is not None, "HITL should be filed for a code-related proposal + connected repo"
    assert hitl["kind"] == "proposal_github_issue"
    payload = json.loads(hitl["payload"])
    assert payload["proposal_id"] == proposal["id"]
    assert payload["sprint_item_id"] == result["sprint_item_id"]
    assert payload["project_id"] == proj["id"]
    assert payload["github_repo"] == "acme/widgets"
    assert payload["options"] == ["Yes — file a GitHub issue", "No — skip"]
    assert payload["recommended"] == "Yes — file a GitHub issue"

    # It shows up in the pending HITL queue for the project.
    pending = await db_module.list_hitl_requests(db, proj["id"], status="pending")
    assert any(h["id"] == hitl["id"] for h in pending)


@pytest.mark.asyncio
async def test_promote_skips_hitl_when_not_code_related(db):
    """Repo connected but nothing inferred from the proposal -> no HITL."""
    proj = await db_module.create_project(db, "gh-issue-proj-2")
    await db_module.update_project_settings(db, proj["id"], github_repo="acme/widgets")
    proposal = await db_module.add_workspace_proposal(
        db, "Strategic planning proposal", "Consider new revenue streams"
    )

    with patch(
        "meridian.db.workspace.subprocess.run",
        return_value=_mock_git_result(""),
    ):
        result = await db_module.promote_workspace_proposal(
            db, proposal["id"], proj["id"], infer_touches_resources=True
        )

    assert result.get("github_issue_hitl") is None
    pending = await db_module.list_hitl_requests(db, proj["id"], status="pending")
    assert not any(h.get("kind") == "proposal_github_issue" for h in pending)


@pytest.mark.asyncio
async def test_promote_skips_hitl_when_repo_not_connected(db):
    """Code-related but no github_repo on the project -> no HITL."""
    proj = await db_module.create_project(db, "gh-issue-proj-3")
    proposal = await db_module.add_workspace_proposal(
        db, "Dashboard rendering fix", "Fix the dashboard layout rendering"
    )

    with patch(
        "meridian.db.workspace.subprocess.run",
        return_value=_mock_git_result("meridian/static/dashboard.js\n"),
    ):
        result = await db_module.promote_workspace_proposal(
            db, proposal["id"], proj["id"],
            infer_touches_resources=True, file_github_issue=True,
        )

    assert result.get("github_issue_hitl") is None


# ---------------------------------------------------------------------------
# set_proposal_github_issue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_proposal_github_issue_persists_number_and_url(db):
    proposal = await db_module.add_workspace_proposal(db, "Some idea", "Body text")
    updated = await db_module.set_proposal_github_issue(
        db, proposal["id"], 42, "https://github.com/acme/widgets/issues/42"
    )
    assert updated is not None
    assert updated["github_issue_number"] == 42
    assert updated["github_issue_url"] == "https://github.com/acme/widgets/issues/42"

    # Persisted — a fresh read shows the same values.
    again = await db_module.get_workspace_proposals(db)
    row = next(p for p in again if p["id"] == proposal["id"])
    assert row["github_issue_number"] == 42
    assert row["github_issue_url"] == "https://github.com/acme/widgets/issues/42"


@pytest.mark.asyncio
async def test_set_proposal_github_issue_returns_none_for_missing_proposal(db):
    result = await db_module.set_proposal_github_issue(db, "does-not-exist", 1, "https://x")
    assert result is None


# ---------------------------------------------------------------------------
# _on_hitl_answered('proposal_github_issue') — the write-back side effect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_hitl_answered_files_issue_and_stores_it_back(db):
    proj = await db_module.create_project(db, "gh-issue-proj-4")
    await db_module.update_project_settings(db, proj["id"], github_repo="acme/widgets")
    proposal = await db_module.add_workspace_proposal(db, "Fix thing", "Body")

    hitl = await db_module.request_hitl(
        db, proj["id"], "Also file a GitHub issue?",
        kind="proposal_github_issue",
        options=["Yes — file a GitHub issue", "No — skip"],
        payload=json.dumps({
            "proposal_id": proposal["id"],
            "sprint_item_id": "si-1",
            "project_id": proj["id"],
            "github_repo": "acme/widgets",
            "issue_title": "Fix thing",
            "issue_body": "Body",
        }),
    )
    row = await db_module.answer_hitl_request(
        db, hitl["id"], "Yes — file a GitHub issue", answered_by="human"
    )

    fake_gh_result = {
        "number": 7,
        "title": "Fix thing",
        "state": "open",
        "html_url": "https://github.com/acme/widgets/issues/7",
    }
    with patch(
        "meridian.mcp.handler._dispatch_github_tool",
        return_value=fake_gh_result,
    ) as mock_dispatch:
        # AsyncMock behaviour: patch with a coroutine-returning side effect.
        async def _fake_dispatch(name, args, tenant, db_arg):
            return fake_gh_result
        mock_dispatch.side_effect = _fake_dispatch

        extra = await server_module._on_hitl_answered(
            db, row, approved=True, tenant={"id": "tenant-1", "github_pat": "enc"},
        )

    assert extra["applied"] is True
    assert extra["github_issue_number"] == 7
    assert extra["github_issue_url"] == "https://github.com/acme/widgets/issues/7"

    updated_proposal = await db_module.get_workspace_proposals(db)
    row2 = next(p for p in updated_proposal if p["id"] == proposal["id"])
    assert row2["github_issue_number"] == 7
    assert row2["github_issue_url"] == "https://github.com/acme/widgets/issues/7"


@pytest.mark.asyncio
async def test_on_hitl_answered_declined_does_not_file_issue(db):
    proj = await db_module.create_project(db, "gh-issue-proj-5")
    proposal = await db_module.add_workspace_proposal(db, "Fix thing", "Body")
    hitl = await db_module.request_hitl(
        db, proj["id"], "Also file a GitHub issue?",
        kind="proposal_github_issue",
        payload=json.dumps({
            "proposal_id": proposal["id"], "project_id": proj["id"],
            "issue_title": "Fix thing", "issue_body": "Body",
        }),
    )
    row = await db_module.answer_hitl_request(db, hitl["id"], "No — skip")

    with patch("meridian.mcp.handler._dispatch_github_tool") as mock_dispatch:
        extra = await server_module._on_hitl_answered(
            db, row, approved=True, tenant={"id": "t1", "github_pat": "enc"},
        )
    mock_dispatch.assert_not_called()
    assert extra["applied"] is False
    assert extra["reason"] == "declined"


@pytest.mark.asyncio
async def test_on_hitl_answered_rejected_dismissal_is_noop():
    extra = await server_module._on_hitl_answered(
        None, {"kind": "proposal_github_issue", "answer": "Yes — file a GitHub issue"},
        approved=False,
    )
    assert extra == {"applied": False, "reason": "rejected"}


@pytest.mark.asyncio
async def test_on_hitl_answered_missing_tenant_is_safe_noop(db):
    proj = await db_module.create_project(db, "gh-issue-proj-6")
    proposal = await db_module.add_workspace_proposal(db, "Fix thing", "Body")
    hitl = await db_module.request_hitl(
        db, proj["id"], "Also file a GitHub issue?",
        kind="proposal_github_issue",
        payload=json.dumps({
            "proposal_id": proposal["id"], "project_id": proj["id"],
            "issue_title": "Fix thing", "issue_body": "Body",
        }),
    )
    row = await db_module.answer_hitl_request(db, hitl["id"], "Yes — file a GitHub issue")

    extra = await server_module._on_hitl_answered(db, row, approved=True, tenant=None)
    assert extra == {"applied": False, "reason": "no_tenant_context"}


@pytest.mark.asyncio
async def test_on_hitl_answered_github_error_surfaces_apply_error(db):
    proj = await db_module.create_project(db, "gh-issue-proj-7")
    proposal = await db_module.add_workspace_proposal(db, "Fix thing", "Body")
    hitl = await db_module.request_hitl(
        db, proj["id"], "Also file a GitHub issue?",
        kind="proposal_github_issue",
        payload=json.dumps({
            "proposal_id": proposal["id"], "project_id": proj["id"],
            "issue_title": "Fix thing", "issue_body": "Body",
        }),
    )
    row = await db_module.answer_hitl_request(db, hitl["id"], "Yes — file a GitHub issue")

    async def _fake_dispatch_error(name, args, tenant, db_arg):
        return {"error": "no_github_repo", "message": "No GitHub repo connected"}

    with patch(
        "meridian.mcp.handler._dispatch_github_tool", side_effect=_fake_dispatch_error,
    ):
        extra = await server_module._on_hitl_answered(
            db, row, approved=True, tenant={"id": "t1", "github_pat": "enc"},
        )
    assert extra["applied"] is False
    assert "no_github_repo" in extra["apply_error"]

    # Proposal is untouched.
    rows = await db_module.get_workspace_proposals(db)
    p = next(p for p in rows if p["id"] == proposal["id"])
    assert p.get("github_issue_number") is None
