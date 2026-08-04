"""32ba4125 — validated, worktree-aware code-intelligence context.

Covers meridian/worktree_code_intel_context.py end to end:

- normalize_context_path / build_context_fingerprint (pure helpers)
- find_registered_worktree / resolve_worktree_code_intel_context (fail-closed
  lookup + validation against the active_worktrees table)
- activate_worktree_code_intel_context (atomic activation: fail-closed on
  missing/removed worktree, fail-closed when the backend push doesn't
  actually land, success returns fingerprint metadata, reuse/idempotency)
- clear_stale_active_repo_cache (cleanup on worktree removal)
- the set_active_repo MCP-tool dispatch wiring (worktree_id branch, and a
  regression guard that the pre-existing repo_path-only behavior/error
  messages are completely unchanged)
- the DELETE /projects/{id}/worktrees/{id} HTTP route now clearing any
  stale _tenant_active_repo cache entry pointing at the removed worktree
"""
from __future__ import annotations

import asyncio

import pytest

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
import meridian.db as db_module
from meridian.mcp import handler as mh
from meridian.routes import tunnel as tn
from meridian.worktree_code_intel_context import (
    WorktreeCodeIntelContextError,
    activate_worktree_code_intel_context,
    build_context_fingerprint,
    clear_stale_active_repo_cache,
    find_registered_worktree,
    normalize_context_path,
    resolve_worktree_code_intel_context,
)


def _run(coro):
    return asyncio.run(coro)


class _FakeExtractWS:
    """Minimal WebSocket stub, mirrors tests/test_tunnel_bridge.py's fixture."""

    def __init__(self, raise_on_send=False):
        self.sent = []
        self._raise = raise_on_send

    async def send_json(self, obj):
        if self._raise:
            raise RuntimeError("ws broken")
        self.sent.append(obj)


@pytest.fixture(autouse=True)
def _clean_tunnel_caches():
    """Every test starts and ends with clean tunnel-side module dicts so
    tests never leak sockets/cache entries into each other."""
    tn._tunnel_extract_sockets.clear()
    tn._tunnel_sockets.clear()
    tn._tenant_active_repo.clear()
    yield
    tn._tunnel_extract_sockets.clear()
    tn._tunnel_sockets.clear()
    tn._tenant_active_repo.clear()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_normalize_context_path_is_stable():
    a = normalize_context_path("./meridian/../meridian")
    b = normalize_context_path("meridian")
    assert a == b


def test_normalize_context_path_handles_blank():
    # Mirrors SerenaDaemonPool._normalize: Path("").resolve() is the CWD, not
    # an error, so "" normalizes to a real (non-empty) path — same as the
    # pool it deliberately parallels. Only a genuinely non-path-like value
    # (None) falls back to the empty-string sentinel.
    assert normalize_context_path("") != ""
    assert normalize_context_path(None) == ""  # type: ignore[arg-type]


def test_build_context_fingerprint_maps_fields():
    row = {
        "id": "wt-1", "project_id": "proj-1", "session_id": "sess-1",
        "item_id": "item-1", "branch": "worktree/abc", "path": "/x/y",
        "created_at": "2026-08-04 00:00:00",
    }
    fp = build_context_fingerprint(row)
    assert fp == {
        "worktree_id": "wt-1",
        "project_id": "proj-1",
        "session_id": "sess-1",
        "item_id": "item-1",
        "branch": "worktree/abc",
        "path": "/x/y",
        "registered_at": "2026-08-04 00:00:00",
    }


# ---------------------------------------------------------------------------
# find_registered_worktree / resolve_worktree_code_intel_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_registered_worktree_matches_registered_path(db):
    p = await db_module.create_project(db, "wcic-find")
    session = await db_module.register_session(db, p["id"], "wcic-sess")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/f1", "meridian",
    )
    found = await find_registered_worktree(db, p["id"], "meridian")
    assert found is not None
    assert found["id"] == wt["id"]


@pytest.mark.asyncio
async def test_find_registered_worktree_returns_none_for_unregistered_path(db):
    p = await db_module.create_project(db, "wcic-find-none")
    found = await find_registered_worktree(db, p["id"], "/totally/unregistered/path")
    assert found is None


@pytest.mark.asyncio
async def test_find_registered_worktree_ignores_removed_worktree(db):
    p = await db_module.create_project(db, "wcic-find-removed")
    session = await db_module.register_session(db, p["id"], "wcic-sess2")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/f2", "meridian",
    )
    await db_module.remove_worktree(db, wt["id"])
    found = await find_registered_worktree(db, p["id"], "meridian")
    assert found is None


@pytest.mark.asyncio
async def test_resolve_worktree_code_intel_context_worktree_kind(db):
    p = await db_module.create_project(db, "wcic-resolve-wt")
    session = await db_module.register_session(db, p["id"], "wcic-sess3")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/r1", "meridian",
    )
    ctx = await resolve_worktree_code_intel_context(db, p["id"], "meridian")
    assert ctx["kind"] == "worktree"
    assert ctx["context"]["worktree_id"] == wt["id"]


@pytest.mark.asyncio
async def test_resolve_worktree_code_intel_context_approved_root_kind(db):
    p = await db_module.create_project(db, "wcic-resolve-root")
    ctx = await resolve_worktree_code_intel_context(
        db, p["id"], "meridian", approved_roots=["meridian"],
    )
    assert ctx["kind"] == "approved_root"
    assert ctx["context"] is None


@pytest.mark.asyncio
async def test_resolve_worktree_code_intel_context_raises_for_arbitrary_path(db):
    p = await db_module.create_project(db, "wcic-resolve-reject")
    with pytest.raises(WorktreeCodeIntelContextError):
        await resolve_worktree_code_intel_context(
            db, p["id"], "/some/random/never/registered/path",
            approved_roots=["meridian"],
        )


# ---------------------------------------------------------------------------
# activate_worktree_code_intel_context — atomic, fail-closed activation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activate_raises_for_unknown_worktree(db):
    with pytest.raises(WorktreeCodeIntelContextError, match="not registered"):
        await activate_worktree_code_intel_context(db, "tenant-x", "does-not-exist")


@pytest.mark.asyncio
async def test_activate_raises_for_removed_worktree(db):
    p = await db_module.create_project(db, "wcic-activate-removed")
    session = await db_module.register_session(db, p["id"], "wcic-sess4")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/a1", "meridian",
    )
    await db_module.remove_worktree(db, wt["id"])
    with pytest.raises(WorktreeCodeIntelContextError, match="removed"):
        await activate_worktree_code_intel_context(db, "tenant-x", wt["id"])


@pytest.mark.asyncio
async def test_activate_fails_closed_when_backend_not_connected(db):
    """No extract WS for the tenant → activation raises instead of reporting
    success (fail-closed — the notes' 'atomic ... or fail closed' contract)."""
    p = await db_module.create_project(db, "wcic-activate-noconn")
    session = await db_module.register_session(db, p["id"], "wcic-sess5")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/a2", "meridian",
    )
    with pytest.raises(WorktreeCodeIntelContextError, match="not_connected"):
        await activate_worktree_code_intel_context(db, "tenant-noconn", wt["id"])


@pytest.mark.asyncio
async def test_activate_fails_closed_on_ws_send_error(db):
    p = await db_module.create_project(db, "wcic-activate-wserr")
    session = await db_module.register_session(db, p["id"], "wcic-sess6")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/a3", "meridian",
    )
    tn._tunnel_extract_sockets["tenant-wserr"] = _FakeExtractWS(raise_on_send=True)
    with pytest.raises(WorktreeCodeIntelContextError, match="ws broken"):
        await activate_worktree_code_intel_context(db, "tenant-wserr", wt["id"])


@pytest.mark.asyncio
async def test_activate_succeeds_and_returns_fingerprint(db):
    p = await db_module.create_project(db, "wcic-activate-ok")
    session = await db_module.register_session(db, p["id"], "wcic-sess7")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/ok1", "meridian",
    )
    ws = _FakeExtractWS()
    tn._tunnel_extract_sockets["tenant-ok"] = ws

    result = await activate_worktree_code_intel_context(db, "tenant-ok", wt["id"])

    assert result["status"] == "ok"
    assert result["repo_path"] == "meridian"
    assert result["worktree"]["worktree_id"] == wt["id"]
    assert result["worktree"]["branch"] == "worktree/ok1"
    assert ws.sent == [{"type": "set_active_repo", "repo_path": "meridian"}]
    # 4d9ad87b cache contract is untouched by this module — still updated.
    assert tn._tenant_active_repo["tenant-ok"] == "meridian"


@pytest.mark.asyncio
async def test_activate_succeeds_even_when_fs_roots_push_fails(db, monkeypatch):
    """fs-roots expansion is best-effort — its failure must never undo an
    already-successful Serena activation."""
    p = await db_module.create_project(db, "wcic-activate-fsfail")
    session = await db_module.register_session(db, p["id"], "wcic-sess8")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/fs1", "meridian",
    )
    ws = _FakeExtractWS()
    tn._tunnel_extract_sockets["tenant-fsfail"] = ws

    async def _boom(*_a, **_k):
        raise RuntimeError("fs roots down")

    monkeypatch.setattr(tn, "send_add_fs_roots_control", _boom)

    result = await activate_worktree_code_intel_context(db, "tenant-fsfail", wt["id"])
    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# clear_stale_active_repo_cache
# ---------------------------------------------------------------------------


def test_clear_stale_active_repo_cache_clears_matching_tenants():
    target = normalize_context_path("meridian")
    tn._tenant_active_repo["t1"] = "meridian"
    tn._tenant_active_repo["t2"] = target
    tn._tenant_active_repo["t3"] = "/somewhere/else"

    cleared = clear_stale_active_repo_cache({"path": "meridian"})

    assert set(cleared) == {"t1", "t2"}
    assert "t1" not in tn._tenant_active_repo
    assert "t2" not in tn._tenant_active_repo
    assert tn._tenant_active_repo["t3"] == "/somewhere/else"


def test_clear_stale_active_repo_cache_noop_for_no_match():
    tn._tenant_active_repo["t1"] = "/unrelated/path"
    cleared = clear_stale_active_repo_cache({"path": "meridian"})
    assert cleared == []
    assert tn._tenant_active_repo["t1"] == "/unrelated/path"


def test_clear_stale_active_repo_cache_blank_path_is_noop():
    tn._tenant_active_repo["t1"] = "meridian"
    cleared = clear_stale_active_repo_cache({"path": ""})
    assert cleared == []
    assert tn._tenant_active_repo["t1"] == "meridian"


# ---------------------------------------------------------------------------
# set_active_repo MCP-tool dispatch — worktree_id branch
# ---------------------------------------------------------------------------


def _make_db():
    return _run(db_module.init_db(":memory:"))


def test_dispatch_set_active_repo_worktree_id_unregistered_raises():
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="not registered"):
            _run(mh._dispatch_mcp_tool(
                "set_active_repo", {"worktree_id": "nope"},
                db, "/tmp", tenant={"id": "t1"},
            ))
    finally:
        _run(db.close())


def test_dispatch_set_active_repo_worktree_id_no_tenant_raises():
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="authenticated tenant"):
            _run(mh._dispatch_mcp_tool(
                "set_active_repo", {"worktree_id": "nope"},
                db, "/tmp", tenant=None,
            ))
    finally:
        _run(db.close())


def test_dispatch_set_active_repo_worktree_id_ok():
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "wcic-dispatch-ok"))
        session = _run(db_module.register_session(db, proj["id"], "wcic-d-sess"))
        wt = _run(db_module.register_worktree(
            db, session["id"], proj["id"], "worktree/d1", "meridian",
        ))
        ws = _FakeExtractWS()
        tn._tunnel_extract_sockets["t-dispatch"] = ws
        try:
            result = _run(mh._dispatch_mcp_tool(
                "set_active_repo", {"worktree_id": wt["id"]},
                db, "/tmp", tenant={"id": "t-dispatch"},
            ))
            assert result["status"] == "ok"
            assert result["repo_path"] == "meridian"
            assert result["worktree"]["worktree_id"] == wt["id"]
        finally:
            tn._tunnel_extract_sockets.pop("t-dispatch", None)
            tn._tenant_active_repo.pop("t-dispatch", None)
    finally:
        _run(db.close())


def test_dispatch_set_active_repo_worktree_id_takes_precedence_over_repo_path():
    """When both are given, worktree_id wins and the raw repo_path is ignored
    entirely (never even validated against approved roots) — this is the
    validated path, so it should never silently fall back to trusting an
    arbitrary caller-supplied path."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "wcic-dispatch-prec"))
        session = _run(db_module.register_session(db, proj["id"], "wcic-d-sess2"))
        wt = _run(db_module.register_worktree(
            db, session["id"], proj["id"], "worktree/d2", "meridian",
        ))
        ws = _FakeExtractWS()
        tn._tunnel_extract_sockets["t-dispatch2"] = ws
        try:
            result = _run(mh._dispatch_mcp_tool(
                "set_active_repo",
                {"worktree_id": wt["id"], "repo_path": "/completely/different/path"},
                db, "/tmp", tenant={"id": "t-dispatch2"},
            ))
            assert result["repo_path"] == "meridian"
            assert ws.sent == [{"type": "set_active_repo", "repo_path": "meridian"}]
        finally:
            tn._tunnel_extract_sockets.pop("t-dispatch2", None)
            tn._tenant_active_repo.pop("t-dispatch2", None)
    finally:
        _run(db.close())


def test_dispatch_set_active_repo_legacy_repo_path_error_unchanged():
    """Regression guard: the ORIGINAL repo_path-only error message/behavior
    (no worktree_id at all) must be byte-for-byte unchanged."""
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="^repo_path is required$"):
            _run(mh._dispatch_mcp_tool(
                "set_active_repo", {"repo_path": ""},
                db, "/tmp", tenant={"id": "t1"},
            ))
    finally:
        _run(db.close())


# ---------------------------------------------------------------------------
# DELETE /projects/{id}/worktrees/{id} — cache cleanup wiring
# ---------------------------------------------------------------------------


def test_delete_worktree_clears_stale_active_repo_cache(client):
    proj = client.post("/projects", json={"name": "wcic-http-cleanup"}).json()
    pid = proj["id"]
    sess = client.post(
        f"/projects/{pid}/start-session", json={"session_name": "wcic-http-sess"},
    ).json()
    sid = sess["session_id"]

    r = client.post(f"/projects/{pid}/worktrees", json={
        "session_id": sid,
        "branch": "worktree/http1",
        "path": "meridian",
    })
    assert r.status_code == 201
    wt = r.json()

    # Simulate some tenant's tunnel having this worktree's path cached as its
    # active repo (as activate_worktree_code_intel_context would leave it).
    tn._tenant_active_repo["http-tenant"] = "meridian"
    try:
        r2 = client.delete(f"/projects/{pid}/worktrees/{wt['id']}")
        assert r2.status_code == 204
        assert "http-tenant" not in tn._tenant_active_repo
    finally:
        tn._tenant_active_repo.pop("http-tenant", None)


def test_delete_worktree_cache_cleanup_does_not_affect_unrelated_tenant(client):
    proj = client.post("/projects", json={"name": "wcic-http-cleanup-2"}).json()
    pid = proj["id"]
    sess = client.post(
        f"/projects/{pid}/start-session", json={"session_name": "wcic-http-sess2"},
    ).json()
    sid = sess["session_id"]

    r = client.post(f"/projects/{pid}/worktrees", json={
        "session_id": sid,
        "branch": "worktree/http2",
        "path": "meridian",
    })
    assert r.status_code == 201
    wt = r.json()

    tn._tenant_active_repo["unrelated-tenant"] = "/some/other/repo"
    try:
        r2 = client.delete(f"/projects/{pid}/worktrees/{wt['id']}")
        assert r2.status_code == 204
        assert tn._tenant_active_repo.get("unrelated-tenant") == "/some/other/repo"
    finally:
        tn._tenant_active_repo.pop("unrelated-tenant", None)
