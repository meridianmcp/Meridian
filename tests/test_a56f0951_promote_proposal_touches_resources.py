"""Tests for a56f0951 — promote_workspace_proposal populates touches_resources.

Covers:
1. When git diff returns files that match proposal keywords, touches_resources
   is populated with inferred:file:<path> identifiers on the promoted sprint item.
2. When no files match (or git is unavailable), the sprint item gets a note
   flagging that resource-scoping is unset — not a silent empty touches_resources.
3. The inference helper (_infer_touches_resources_from_proposal) handles edge
   cases: empty body, too-few keywords, no changed files.
4. Return dict from promote_workspace_proposal includes sprint_item_touches_resources
   and sprint_item_notes.
"""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from meridian import db as db_module
from meridian.db.workspace import (
    _infer_touches_resources_from_proposal,
    _proposal_keywords,
)


# ---------------------------------------------------------------------------
# Unit tests for the inference helpers (no DB needed)
# ---------------------------------------------------------------------------

def test_proposal_keywords_basic():
    kws = _proposal_keywords("dashboard TypeScript migration fix")
    assert "dashboard" in kws
    assert "typescript" in kws
    assert "migration" in kws
    # stop-words excluded
    assert "fix" not in kws


def test_proposal_keywords_strips_stop_words():
    kws = _proposal_keywords("add and the a")
    # "add", "and", "the", "a" are all stop-words
    assert not kws


def test_proposal_keywords_min_length():
    # tokens shorter than 3 chars are dropped by the regex
    kws = _proposal_keywords("ok go it as")
    assert not kws


def test_infer_returns_empty_on_git_error():
    """When git fails/unavailable, inference returns empty list silently."""
    with patch("meridian.db.workspace.subprocess.run", side_effect=OSError("no git")):
        result = _infer_touches_resources_from_proposal("dashboard UI fix", "fix the layout")
    assert result == []


def test_infer_returns_empty_when_no_changed_files():
    """Empty git diff output → empty inference."""
    mock_result = MagicMock()
    mock_result.stdout = ""
    with patch("meridian.db.workspace.subprocess.run", return_value=mock_result):
        result = _infer_touches_resources_from_proposal("dashboard UI fix", "")
    assert result == []


def test_infer_returns_empty_when_too_few_keywords():
    """Title+body with < 2 keywords after stop-word removal → no inference."""
    mock_result = MagicMock()
    mock_result.stdout = "meridian/db/__init__.py\n"
    with patch("meridian.db.workspace.subprocess.run", return_value=mock_result):
        # "fix" is a stop-word; only one meaningful keyword would remain
        result = _infer_touches_resources_from_proposal("fix it", "")
    assert result == []


def test_infer_matches_by_filename_stem():
    """A file whose stem appears in the proposal title triggers a match."""
    mock_result = MagicMock()
    mock_result.stdout = "meridian/static/dashboard.js\nmeridian/server.py\n"
    with patch("meridian.db.workspace.subprocess.run", return_value=mock_result):
        result = _infer_touches_resources_from_proposal(
            "dashboard layout improvements",
            "update the dashboard rendering",
        )
    # dashboard.js stem 'dashboard' is in the keyword set
    assert any("dashboard" in r for r in result)
    assert all(r.startswith("inferred:file:") for r in result)


def test_infer_matches_by_path_keyword_overlap():
    """Two+ keywords from proposal overlap with path segments → match.

    Uses a file path with enough 3+-char segments to guarantee >= 2 keyword
    overlap: meridian/hosted/oauth.py → path_kws includes 'meridian', 'hosted',
    'oauth'. A proposal mentioning 'meridian' and 'hosted' picks those two up."""
    mock_result = MagicMock()
    mock_result.stdout = "meridian/hosted/oauth.py\n"
    with patch("meridian.db.workspace.subprocess.run", return_value=mock_result):
        result = _infer_touches_resources_from_proposal(
            "meridian hosted authentication flow",
            "",
        )
    # "meridian" and "hosted" and "authentication" should overlap with path kws
    # At minimum "meridian" + "hosted" = 2 matches (both in path + title)
    assert len(result) >= 1
    assert result[0] == "inferred:file:meridian/hosted/oauth.py"


def test_infer_caps_at_ten_results():
    """Inference caps matched files at 10."""
    files = "\n".join(f"meridian/module{i}/dashboard.py" for i in range(20))
    mock_result = MagicMock()
    mock_result.stdout = files + "\n"
    with patch("meridian.db.workspace.subprocess.run", return_value=mock_result):
        result = _infer_touches_resources_from_proposal(
            "dashboard module improvements", "module dashboard update patch",
        )
    assert len(result) <= 10


# ---------------------------------------------------------------------------
# Integration tests — promote_workspace_proposal writes touches_resources / notes
# (uses the shared db fixture + aiosqlite in-memory)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_promote_proposal_sets_touches_resources_when_files_match(db):
    """When git inference finds matching files, sprint item gets touches_resources."""
    proj = await db_module.create_project(db, "test-project-promote-tr")
    proposal = await db_module.add_workspace_proposal(
        db, "Dashboard rendering fix", "Fix the dashboard layout rendering"
    )

    mock_result = MagicMock()
    mock_result.stdout = "meridian/static/dashboard.js\n"

    with patch("meridian.db.workspace.subprocess.run", return_value=mock_result):
        result = await db_module.promote_workspace_proposal(
            db, proposal["id"], proj["id"], infer_touches_resources=True
        )

    si_id = result["sprint_item_id"]
    assert si_id

    # touches_resources should be set
    tr = result.get("sprint_item_touches_resources")
    assert tr is not None, "sprint_item_touches_resources should be set when files match"
    parsed = json.loads(tr)
    assert any("dashboard" in r for r in parsed)
    assert all(r.startswith("inferred:file:") for r in parsed)

    # notes should NOT contain the unset flag
    notes = result.get("sprint_item_notes")
    assert notes is None or "[resource-scope:unset]" not in (notes or "")


@pytest.mark.asyncio
async def test_promote_proposal_sets_notes_flag_when_no_files_match(db):
    """When git inference produces no matches, sprint item notes flagging resource gap."""
    proj = await db_module.create_project(db, "test-project-promote-no-tr")
    proposal = await db_module.add_workspace_proposal(
        db, "Strategic planning proposal", "Consider new revenue streams"
    )

    # No files changed — no inference possible
    mock_result = MagicMock()
    mock_result.stdout = ""

    with patch("meridian.db.workspace.subprocess.run", return_value=mock_result):
        result = await db_module.promote_workspace_proposal(
            db, proposal["id"], proj["id"], infer_touches_resources=True
        )

    # touches_resources should be None/empty
    tr = result.get("sprint_item_touches_resources")
    assert tr is None, "touches_resources should be None when inference fails"

    # notes must contain the [resource-scope:unset] flag
    notes = result.get("sprint_item_notes")
    assert notes is not None, "notes should be set with the resource-scope flag"
    assert "[resource-scope:unset]" in notes


@pytest.mark.asyncio
async def test_promote_proposal_sets_notes_when_git_unavailable(db):
    """When git is unavailable, sprint item notes contain the resource-scope flag."""
    proj = await db_module.create_project(db, "test-project-promote-git-err")
    proposal = await db_module.add_workspace_proposal(
        db, "Something important", "This needs resources"
    )

    with patch("meridian.db.workspace.subprocess.run", side_effect=OSError("no git")):
        result = await db_module.promote_workspace_proposal(
            db, proposal["id"], proj["id"], infer_touches_resources=True
        )

    notes = result.get("sprint_item_notes")
    assert notes is not None
    assert "[resource-scope:unset]" in notes
    assert result.get("sprint_item_touches_resources") is None


@pytest.mark.asyncio
async def test_promote_proposal_sprint_item_db_row_has_touches_resources(db):
    """The sprint_items DB row itself carries touches_resources (not just the return dict)."""
    proj = await db_module.create_project(db, "test-project-promote-row")
    proposal = await db_module.add_workspace_proposal(
        db, "Server routing improvements", "Improve the server route handlers"
    )

    mock_result = MagicMock()
    mock_result.stdout = "meridian/server.py\n"

    with patch("meridian.db.workspace.subprocess.run", return_value=mock_result):
        result = await db_module.promote_workspace_proposal(
            db, proposal["id"], proj["id"], infer_touches_resources=True
        )

    si_id = result["sprint_item_id"]
    # Fetch the sprint item from DB directly
    item = await db_module.get_sprint_item(db, si_id)
    assert item is not None

    tr_raw = item.get("touches_resources")
    assert tr_raw is not None, "sprint_items.touches_resources row should be set"
    parsed = json.loads(tr_raw)
    assert any("server" in r for r in parsed)


@pytest.mark.asyncio
async def test_promote_proposal_sprint_item_db_row_has_notes_flag(db):
    """When no inference, the sprint_items DB row itself carries the notes flag."""
    proj = await db_module.create_project(db, "test-project-promote-row-notes")
    proposal = await db_module.add_workspace_proposal(
        db, "General plan for growth", "Something vague without file keywords"
    )

    mock_result = MagicMock()
    mock_result.stdout = ""

    with patch("meridian.db.workspace.subprocess.run", return_value=mock_result):
        result = await db_module.promote_workspace_proposal(
            db, proposal["id"], proj["id"], infer_touches_resources=True
        )

    si_id = result["sprint_item_id"]
    item = await db_module.get_sprint_item(db, si_id)
    assert item is not None

    notes = item.get("notes")
    assert notes is not None, "sprint_items.notes row should be set when inference fails"
    assert "[resource-scope:unset]" in notes


@pytest.mark.asyncio
async def test_promote_proposal_still_sets_status_pending_and_proposal_promoted(db):
    """Core promotion semantics (proposal→promoted, sprint item pending) unaffected."""
    proj = await db_module.create_project(db, "test-project-promote-core")
    proposal = await db_module.add_workspace_proposal(
        db, "Core feature work", "Implement some feature"
    )

    mock_result = MagicMock()
    mock_result.stdout = ""

    with patch("meridian.db.workspace.subprocess.run", return_value=mock_result):
        result = await db_module.promote_workspace_proposal(
            db, proposal["id"], proj["id"]
        )

    # Proposal should be promoted
    assert result["proposal"]["status"] == "promoted"
    # Sprint item should exist and be pending
    si_id = result["sprint_item_id"]
    item = await db_module.get_sprint_item(db, si_id)
    assert item is not None
    assert item["status"] == "pending"


@pytest.mark.asyncio
async def test_promote_proposal_does_not_guess_resources_or_file_github_by_default(db):
    proj = await db_module.create_project(db, "test-project-promote-explicit")
    await db_module.update_project_settings(db, proj["id"], github_repo="acme/widgets")
    proposal = await db_module.add_workspace_proposal(
        db, "Dashboard rendering fix", "Fix the dashboard layout rendering"
    )

    with patch("meridian.db.workspace.subprocess.run") as run:
        result = await db_module.promote_workspace_proposal(
            db, proposal["id"], proj["id"]
        )

    run.assert_not_called()
    assert result["sprint_item_touches_resources"] is None
    assert result["github_issue_hitl"] is None


@pytest.mark.asyncio
async def test_promote_proposal_accepts_explicit_resources_without_inference(db):
    proj = await db_module.create_project(db, "test-project-promote-explicit-resources")
    proposal = await db_module.add_workspace_proposal(
        db, "Workspace persistence", "Keep the proposal history durable"
    )

    with patch("meridian.db.workspace.subprocess.run") as run:
        result = await db_module.promote_workspace_proposal(
            db,
            proposal["id"],
            proj["id"],
            touches_resources=["file:meridian/db/workspace.py"],
        )

    run.assert_not_called()
    assert json.loads(result["sprint_item_touches_resources"]) == [
        "file:meridian/db/workspace.py"
    ]
