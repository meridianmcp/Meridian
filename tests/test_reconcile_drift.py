"""Tests for reconcile-drift commit-hash reporting.

Regression: the MCP `reconcile_sprint_drift` tool and the checkpoint drift
warnings reported an empty commit hash ("matches commit ") because
`_fetch_recent_commits` dropped the SHA and callers hardcoded sha="".
"""
from __future__ import annotations

import asyncio

import meridian.server  # noqa: F401  — import first to avoid handler/server circular import
from meridian import handoff
from meridian.mcp.handler import _fetch_recent_commits


def test_fetch_recent_commits_returns_sha_and_message():
    """_fetch_recent_commits yields {sha, message} dicts with real SHAs.

    Runs against the local git repo (the test suite's own checkout).
    """
    commits = asyncio.run(_fetch_recent_commits({}, None))
    assert commits, "expected at least one commit from local git history"
    assert all(set(c) >= {"sha", "message"} for c in commits)
    # At least one real (non-empty) SHA — the bug was every sha == "".
    assert any(c["sha"] for c in commits)


def test_reconcile_sprint_items_propagates_commit_sha():
    """reconcile_sprint_items surfaces the matching commit's SHA, not ''."""
    pending = [{"id": "item-1", "title": "Fix tunnel client permanent url endpoint"}]
    commits = [
        {"sha": "abc123def456", "message": "fix tunnel client permanent url endpoint bug"},
    ]
    matches = handoff.reconcile_sprint_items(pending, commits)
    assert matches, "expected a keyword match"
    first = matches[0]["matching_commits"][0]
    assert first["sha"] == "abc123def456"
    assert first["sha"]  # explicitly: not the old empty-string regression
