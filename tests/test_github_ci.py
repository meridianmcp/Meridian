"""Independent GitHub CI verification at sprint-item completion (b121348e).

complete_sprint_item's required_notes proves evidence TEXT exists, not that it is
TRUE. These tests cover the SHA extraction, the (stubbed) GitHub check-runs
aggregation, and the advisory flag wired into the complete_sprint_item MCP path.
No network is touched — the HTTP seam / verifier are injected or monkeypatched.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import github_ci


# ---------------------------------------------------------------------------
# extract_commit_sha — keyword-anchored so a hex UUID isn't mistaken for a SHA
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Committed 62a3a6c / main 6346a18", "62a3a6c"),
    ("commit abc1234 shipped", "abc1234"),
    ("sha: deadbeefdeadbeef", "deadbeefdeadbeef"),
    ("merged to main 7af7590", "7af7590"),
    ("no commit here", None),
    ("", None),
    (None, None),
    # A bare hex UUID / sprint-item id must NOT be read as a commit SHA.
    ("closed item b121348e-c4c3-4262 done", None),
])
def test_extract_commit_sha(text, expected):
    assert github_ci.extract_commit_sha(text) == expected


# ---------------------------------------------------------------------------
# verify_commit_ci — aggregate check-runs (stubbed http)
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _http(payload):
    async def _get(url, headers=None):
        return _Resp(payload)
    return _get


@pytest.mark.asyncio
async def test_verify_commit_ci_states():
    ok = await github_ci.verify_commit_ci("o/r", "sha1", http_get=_http(
        {"check_runs": [{"status": "completed", "conclusion": "success"},
                        {"status": "completed", "conclusion": "skipped"}]}))
    assert ok["state"] == "success" and ok["total"] == 2 and ok["failed"] == 0

    bad = await github_ci.verify_commit_ci("o/r", "sha2", http_get=_http(
        {"check_runs": [{"status": "completed", "conclusion": "success"},
                        {"status": "completed", "conclusion": "failure"}]}))
    assert bad["state"] == "failure" and bad["failed"] == 1

    pend = await github_ci.verify_commit_ci("o/r", "sha3", http_get=_http(
        {"check_runs": [{"status": "in_progress", "conclusion": None}]}))
    assert pend["state"] == "pending"

    none = await github_ci.verify_commit_ci("o/r", "sha4", http_get=_http({"check_runs": []}))
    assert none["state"] == "unknown"


@pytest.mark.asyncio
async def test_verify_commit_ci_guarded_on_error_and_missing_args():
    async def _boom(url, headers=None):
        raise RuntimeError("network down")
    r = await github_ci.verify_commit_ci("o/r", "sha", http_get=_boom)
    assert r["state"] == "unknown"  # guarded — never raises
    assert (await github_ci.verify_commit_ci("", "sha"))["state"] == "unknown"
    assert (await github_ci.verify_commit_ci("o/r", ""))["state"] == "unknown"


# ---------------------------------------------------------------------------
# Handler integration — the advisory flag on complete_sprint_item
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_sprint_item_flags_failing_ci(db, monkeypatch):
    """427b7902 — a GENUINELY failing CI now REFUSES completion (was advisory-only
    under b121348e). override_ci=true is the escape hatch and records the warning.
    (Full green/unknown/pending/override matrix lives in test_w5_427b7902_ci_gate.)"""
    from meridian import server as srv
    p = await db_module.create_project(db, "ci-proj")
    await db_module.update_project_settings(db, p["id"], github_repo="meridianmcp/Meridian")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "ship the widget")

    async def _fake_verify(repo, sha, *, token=None, **_kw):
        return {"sha": sha, "repo": repo, "state": "failure", "total": 3, "failed": 1}
    monkeypatch.setattr(github_ci, "verify_commit_ci", _fake_verify)

    # Failing CI is now REFUSED, not merely flagged — the item stays open.
    refused = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"],
         "notes": "done; committed abc1234 to main"},
        db, "/tmp")
    assert refused["error"] == "CI_FAILING"
    assert refused["ci_verification"]["state"] == "failure"
    still = await db_module.get_sprint_item(db, item["id"])
    assert still["status"] != "done"

    # override_ci=true completes anyway and records the failing CI as a warning.
    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"],
         "notes": "done; committed abc1234 to main", "override_ci": True},
        db, "/tmp")
    assert res["status"] == "done"
    assert res["ci_verification"]["state"] == "failure"
    assert "ci_warning" in res and "FAILING" in res["ci_warning"]


@pytest.mark.asyncio
async def test_complete_sprint_item_ci_success_no_warning(db, monkeypatch):
    from meridian import server as srv
    p = await db_module.create_project(db, "ci-proj2")
    await db_module.update_project_settings(db, p["id"], github_repo="meridianmcp/Meridian")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "ship another widget")

    async def _fake_verify(repo, sha, *, token=None, **_kw):
        return {"sha": sha, "repo": repo, "state": "success", "total": 3, "failed": 0}
    monkeypatch.setattr(github_ci, "verify_commit_ci", _fake_verify)

    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"], "notes": "done; commit deadbee"},
        db, "/tmp")
    assert res["ci_verification"]["state"] == "success"
    assert "ci_warning" not in res


@pytest.mark.asyncio
async def test_complete_sprint_item_no_repo_or_sha_skips_ci(db, monkeypatch):
    from meridian import server as srv
    # No github_repo on the project → CI verification is skipped entirely.
    p = await db_module.create_project(db, "ci-proj3")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "ship a third widget")
    called = {"v": False}

    async def _should_not_call(*a, **k):
        called["v"] = True
        return {}
    monkeypatch.setattr(github_ci, "verify_commit_ci", _should_not_call)

    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"], "notes": "done; committed abc1234"},
        db, "/tmp")
    assert "ci_verification" not in res  # no repo → skipped
    assert called["v"] is False
