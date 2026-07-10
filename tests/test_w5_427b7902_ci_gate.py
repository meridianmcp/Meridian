"""HARD CI gate on complete_sprint_item (427b7902).

The b121348e work computed an independent GitHub Actions ``ci_verification`` for the
commit named in a completion's notes, but only WARNED — a "done" was recorded even
when CI was genuinely red. 427b7902 upgrades that to a real gate, mirroring the
EVIDENCE_REQUIRED refusal:

  * A GENUINELY FAILING CI state (``state == "failure"``) REFUSES completion:
    the tool returns ``{"error": "CI_FAILING", ...}`` and the item is NOT flipped
    to ``done``.
  * ``unknown`` (no repo configured, no check-runs yet, self-hosted / no-GitHub)
    and ``pending`` (CI still running — the normal push-then-complete race) are
    ALWAYS allowed through. The gate never blocks on absent/unknown CI.
  * ``override_ci=true`` is the escape hatch (consistent with existing ``force=``
    patterns): it completes anyway and records the failing CI on the item.

The GitHub HTTP seam is never touched — ``github_ci.verify_commit_ci`` is
monkeypatched, exactly as tests/test_github_ci.py does.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import github_ci
from meridian import server as srv


def _fake_ci(state, *, total=3, failed=0):
    """Return an async ``verify_commit_ci`` stub yielding a fixed ``state``."""
    async def _verify(repo, sha, *, token=None, **_kw):
        return {"sha": sha, "repo": repo, "state": state, "total": total,
                "failed": failed}
    return _verify


async def _project_with_repo(db, name):
    p = await db_module.create_project(db, name)
    await db_module.update_project_settings(
        db, p["id"], github_repo="meridianmcp/Meridian"
    )
    return p


async def _status(db, item_id):
    it = await db_module.get_sprint_item(db, item_id)
    return (it or {}).get("status")


# ---------------------------------------------------------------------------
# GREEN CI is allowed — completion succeeds, no warning.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_green_ci_completes(db, monkeypatch):
    p = await _project_with_repo(db, "gate-green")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "ship widget")
    monkeypatch.setattr(github_ci, "verify_commit_ci", _fake_ci("success"))

    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"],
         "notes": "done; committed abc1234 to main"},
        db, "/tmp")

    assert res.get("error") is None
    assert res["status"] == "done"
    assert res["ci_verification"]["state"] == "success"
    assert "ci_warning" not in res
    assert await _status(db, item["id"]) == "done"


# ---------------------------------------------------------------------------
# FAILING CI is REFUSED — clean error, item NOT completed.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failing_ci_is_refused(db, monkeypatch):
    p = await _project_with_repo(db, "gate-fail")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "ship widget")
    monkeypatch.setattr(github_ci, "verify_commit_ci", _fake_ci("failure", failed=2))

    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"],
         "notes": "done; committed abc1234 to main"},
        db, "/tmp")

    assert res["error"] == "CI_FAILING"
    assert res["item_id"] == item["id"]
    assert res["ci_verification"]["state"] == "failure"
    assert "FAILING" in res["message"]
    assert "override_ci=true" in res["message"]
    # The gate refused BEFORE marking done — the item is untouched.
    assert await _status(db, item["id"]) != "done"


# ---------------------------------------------------------------------------
# UNKNOWN / PENDING / no-repo / no-sha are ALWAYS allowed (never block).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_ci_is_allowed(db, monkeypatch):
    p = await _project_with_repo(db, "gate-unknown")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "ship widget")
    monkeypatch.setattr(github_ci, "verify_commit_ci", _fake_ci("unknown"))

    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"],
         "notes": "done; committed abc1234"},
        db, "/tmp")

    assert res.get("error") is None
    assert res["status"] == "done"
    assert res["ci_verification"]["state"] == "unknown"
    assert await _status(db, item["id"]) == "done"


@pytest.mark.asyncio
async def test_pending_ci_is_allowed(db, monkeypatch):
    """CI still running (the normal push-then-complete race) must not block."""
    p = await _project_with_repo(db, "gate-pending")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "ship widget")
    monkeypatch.setattr(github_ci, "verify_commit_ci", _fake_ci("pending"))

    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"],
         "notes": "done; committed abc1234"},
        db, "/tmp")

    assert res.get("error") is None
    assert res["status"] == "done"
    assert res["ci_verification"]["state"] == "pending"
    assert await _status(db, item["id"]) == "done"


@pytest.mark.asyncio
async def test_no_repo_never_blocks(db, monkeypatch):
    """No github_repo configured → CI is never even checked → never blocks."""
    p = await db_module.create_project(db, "gate-norepo")  # no github_repo
    item = await db_module.add_sprint_item(db, p["id"], "v1", "ship widget")
    called = {"v": False}

    async def _should_not_call(*a, **k):
        called["v"] = True
        return {"state": "failure"}
    monkeypatch.setattr(github_ci, "verify_commit_ci", _should_not_call)

    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"],
         "notes": "done; committed abc1234"},
        db, "/tmp")

    assert res.get("error") is None
    assert res["status"] == "done"
    assert "ci_verification" not in res
    assert called["v"] is False


@pytest.mark.asyncio
async def test_no_sha_in_notes_never_blocks(db, monkeypatch):
    """No commit SHA referenced anywhere → nothing to verify → never blocks."""
    p = await _project_with_repo(db, "gate-nosha")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "ship widget")
    called = {"v": False}

    async def _should_not_call(*a, **k):
        called["v"] = True
        return {"state": "failure"}
    monkeypatch.setattr(github_ci, "verify_commit_ci", _should_not_call)

    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"],
         "notes": "finished the work, all green"},  # no commit keyword+sha
        db, "/tmp")

    assert res.get("error") is None
    assert res["status"] == "done"
    assert called["v"] is False


# ---------------------------------------------------------------------------
# OVERRIDE escape hatch — override_ci=true completes on red, records the warning.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_override_ci_completes_on_failure(db, monkeypatch):
    p = await _project_with_repo(db, "gate-override")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "ship widget")
    monkeypatch.setattr(github_ci, "verify_commit_ci", _fake_ci("failure", failed=1))

    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"],
         "notes": "done; committed abc1234 to main", "override_ci": True},
        db, "/tmp")

    # Completed despite red CI — but the failing CI is recorded on the item.
    assert res.get("error") is None
    assert res["status"] == "done"
    assert res["ci_verification"]["state"] == "failure"
    assert "ci_warning" in res and "FAILING" in res["ci_warning"]
    assert "override_ci=true" in res["ci_warning"]
    assert await _status(db, item["id"]) == "done"


# ---------------------------------------------------------------------------
# The CI gate reads the SHA from the ITEM's stored notes too (not just this call).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_uses_stored_item_notes(db, monkeypatch):
    """A SHA recorded on a PRIOR call (e.g. provisional completion) still gates a
    later bare complete_sprint_item with no notes."""
    p = await _project_with_repo(db, "gate-storednotes")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "ship widget")
    # Persist a commit reference on the item without completing it.
    await srv._dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": p["id"], "item_id": item["id"],
         "notes": "progress; committed abc1234"},
        db, "/tmp")
    monkeypatch.setattr(github_ci, "verify_commit_ci", _fake_ci("failure", failed=1))

    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"]},  # no notes on THIS call
        db, "/tmp")

    assert res["error"] == "CI_FAILING"
    assert await _status(db, item["id"]) != "done"


# ---------------------------------------------------------------------------
# The gate never crashes completion if CI verification itself raises.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ci_verify_exception_never_blocks(db, monkeypatch):
    p = await _project_with_repo(db, "gate-boom")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "ship widget")

    async def _boom(*a, **k):
        raise RuntimeError("github down")
    monkeypatch.setattr(github_ci, "verify_commit_ci", _boom)

    res = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"],
         "notes": "done; committed abc1234"},
        db, "/tmp")

    # A verifier error degrades to "allowed" (unknown), never a refusal or crash.
    assert res.get("error") is None
    assert res["status"] == "done"
