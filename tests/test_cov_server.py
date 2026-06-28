"""Coverage-focused tests for meridian/server.py route handlers.

These exercise FastAPI route handlers and their error/edge paths: public
pages, config endpoints, status badges, anonymous vs tenant branches,
hosted-only 404 guards (self-hosted client), auth-required 401s, hosted
happy paths (bearer token), hooks, the tunnel device-code flow, admin login,
changelog admin gating, workspace, settings, feedback, and the MCP endpoints.

Reuses the existing test patterns:
  * the ``client`` fixture from conftest.py (self-hosted, in-memory SQLite)
  * ``_make_hosted_client`` + ``_run`` from test_v2_hosted.py
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib

import pytest


# ---------------------------------------------------------------------------
# Shared helpers (mirrors tests/test_v2_hosted.py)
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine in a fresh event loop (pytest-safe)."""
    return asyncio.run(coro)


def _make_hosted_client(monkeypatch, tmp_path):
    """Hosted-mode TestClient backed by an in-memory auth DB."""
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))

    from fastapi.testclient import TestClient
    import meridian.server as server_module

    server_module = importlib.reload(server_module)
    return TestClient(server_module.app)


async def _new_tenant_token(db, email, label="t"):
    """Create a tenant + bearer token; return the raw token string."""
    from meridian import db as db_module

    tenant = await db_module.upsert_tenant(db, email)
    raw, _row = await db_module.create_api_token(db, tenant["id"], label=label)
    return tenant, raw


def _hdr(raw_token):
    return {"Authorization": f"Bearer {raw_token}"}


def _login(client, email, seed_project_db=False):
    """Create a tenant + signed session cookie (the browser-auth flow).

    Routes guarded by ``get_current_tenant`` require a signed session cookie,
    not a Bearer token. Returns the tenant dict; the cookie is set on the
    client so subsequent requests authenticate.

    When ``seed_project_db=True`` the per-tenant DB cache is pre-seeded with
    the in-memory auth DB, so routes that resolve ``_db(request)`` (the
    tenant's project DB) work without a provisioned Neon database — the
    real provisioning path is untestable without live Postgres.
    """
    from datetime import datetime, timezone, timedelta
    from meridian import db as db_module
    from meridian.hosted import _make_session_cookie, _SESSION_COOKIE
    from meridian import _deps

    async def _inner():
        tenant = await db_module.upsert_tenant(client.app.state.db, email)
        # get_user_session compares expires_at lexically against
        # datetime.now(...).isoformat(), so store in the same ISO format.
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        sess = await db_module.create_user_session(
            client.app.state.db, tenant["id"], expires
        )
        return tenant, sess["id"]

    tenant, sid = _run(_inner())
    if seed_project_db:
        _deps._tenant_db_cache[tenant["id"]] = client.app.state.db
    client.cookies.set(_SESSION_COOKIE, _make_session_cookie(sid))
    return tenant


# ===========================================================================
# Public pages + static assets (self-hosted client)
# ===========================================================================

def test_landing_page_ok(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_robots_txt(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "User-agent" in r.text


def test_favicon(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200


def test_sitemap_xml(client):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "<urlset" in r.text or "<?xml" in r.text


def test_terms_page(client):
    r = client.get("/terms")
    assert r.status_code == 200


def test_privacy_page(client):
    r = client.get("/privacy")
    assert r.status_code == 200


def test_pricing_page(client):
    r = client.get("/pricing")
    assert r.status_code == 200


def test_install_mcp_page(client):
    r = client.get("/install-mcp")
    assert r.status_code == 200


def test_changelog_page(client):
    r = client.get("/changelog")
    assert r.status_code == 200


def test_onboarding_page(client):
    r = client.get("/onboarding")
    assert r.status_code == 200


def test_setup_redirects_to_dashboard(client):
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"


def test_demo_dashboard_sets_demo_cookie(client):
    r = client.get("/demo")
    assert r.status_code == 200
    # demo cookie is set so subsequent calls route to demo DB
    assert any("meridian_demo" in v for v in r.headers.get_list("set-cookie"))


def test_dashboard_self_hosted_ok(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert r.headers["Cache-Control"].startswith("no-store")


# ===========================================================================
# Health / status / config
# ===========================================================================

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "meridian"}


def test_failover_status_default_false(client, monkeypatch):
    monkeypatch.delenv("MERIDIAN_IS_FAILOVER", raising=False)
    r = client.get("/failover-status")
    assert r.status_code == 200
    assert r.json() == {"is_failover": False}


def test_config_self_hosted_memory_db(client):
    r = client.get("/config")
    assert r.status_code == 200
    body = r.json()
    assert body["db"] == "memory"
    assert body["demo_mode"] is False
    assert "server_url" in body
    assert "version" in body


def test_config_demo_mode_via_cookie(client):
    # Visiting /demo sets the demo cookie; /config must then report demo mode.
    client.get("/demo")
    r = client.get("/config")
    assert r.status_code == 200
    body = r.json()
    assert body["demo_mode"] is True
    assert body["db"] == "demo"


def test_config_api_key_status(client):
    r = client.get("/config/api-key")
    assert r.status_code == 200
    body = r.json()
    assert "configured" in body
    assert "method" in body


def test_status_server_badge(client):
    r = client.get("/status/server")
    assert r.status_code == 200
    body = r.json()
    assert body["schemaVersion"] == 1
    assert body["message"] == "online"


def test_status_tools_badge(client):
    r = client.get("/status/tools")
    assert r.status_code == 200
    assert "tools" in r.json()["message"]


def test_status_sessions_badge(client):
    r = client.get("/status/sessions")
    assert r.status_code == 200
    assert "live" in r.json()["message"]


def test_list_tools_endpoint(client):
    r = client.get("/tools")
    assert r.status_code == 200
    tools = r.json()
    assert isinstance(tools, list)
    assert len(tools) > 0
    assert all("name" in t for t in tools)


def test_mcp_quickstart(client):
    r = client.get("/mcp/quickstart")
    assert r.status_code == 200
    assert "start_session" in r.text


def test_mcp_tools_doc(client):
    r = client.get("/mcp/tools-doc")
    assert r.status_code == 200
    assert len(r.text) > 100


# ===========================================================================
# /admin/__error_test gate
# ===========================================================================

def test_error_test_disabled_returns_404(client, monkeypatch):
    monkeypatch.delenv("MERIDIAN_ENABLE_ERROR_TEST", raising=False)
    r = client.get("/admin/__error_test")
    assert r.status_code == 404


# ===========================================================================
# /me — anonymous (self-hosted) vs tenant (hosted)
# ===========================================================================

def test_me_anonymous_returns_empty(client):
    r = client.get("/me")
    assert r.status_code == 200
    assert r.json() == {}


def test_me_with_tenant(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        tenant, raw = _run(_new_tenant_token(client.app.state.db, "me-cov@example.com"))
        r = client.get("/me", headers=_hdr(raw))
        assert r.status_code == 200
        body = r.json()
        assert body["email"] == "me-cov@example.com"
        assert body["tenant_id"] == tenant["id"]
        assert "tunnel_plugins" in body
        assert body["expired"] is False


# ===========================================================================
# context-block / context / devlog routes (project not found + happy path)
# ===========================================================================

def _create_project(client, name="cov-proj"):
    from meridian import db as db_module

    async def _inner():
        return await db_module.create_project(client.app.state.db, name)

    return _run(_inner())


def test_context_block_project_not_found(client):
    r = client.get("/projects/does-not-exist/context-block")
    assert r.status_code == 404


def test_context_block_bad_mode(client):
    p = _create_project(client, "ctxblock-badmode")
    r = client.get(f"/projects/{p['id']}/context-block?mode=bogus")
    assert r.status_code == 400


def test_context_block_happy(client):
    p = _create_project(client, "ctxblock-ok")
    r = client.get(f"/projects/{p['id']}/context-block")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]


def test_context_chat_mode(client):
    p = _create_project(client, "ctxblock-chat")
    r = client.get(f"/projects/{p['id']}/context-block?mode=chat")
    assert r.status_code == 200


def test_context_json_not_found(client):
    r = client.get("/projects/nope/context")
    assert r.status_code == 404


def test_context_json_happy(client):
    p = _create_project(client, "ctxjson-ok")
    r = client.get(f"/projects/{p['id']}/context")
    assert r.status_code == 200
    body = r.json()
    assert body["project"]["id"] == p["id"]
    assert "file_map" in body
    assert "sprint_items" in body


def test_devlog_project_not_found(client):
    r = client.post("/projects/nope/devlog", json={"text": "hi"})
    assert r.status_code == 404


def test_devlog_missing_text(client):
    p = _create_project(client, "devlog-notext")
    r = client.post(f"/projects/{p['id']}/devlog", json={"text": "   "})
    assert r.status_code == 400


def test_devlog_happy(client):
    p = _create_project(client, "devlog-ok")
    r = client.post(f"/projects/{p['id']}/devlog", json={"text": "did a thing"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ===========================================================================
# queued-session round trip
# ===========================================================================

def test_queue_and_get_queued_session(client):
    p = _create_project(client, "queue-proj")
    q = client.post(f"/projects/{p['id']}/queue-session", json={"goal": "next: ship"})
    assert q.status_code == 200
    assert q.json() == {"queued": True, "goal": "next: ship"}
    g = client.get(f"/projects/{p['id']}/queued-session")
    assert g.status_code == 200
    assert g.json() == {"goal": "next: ship"}
    # Empty body clears the queue.
    clr = client.post(f"/projects/{p['id']}/queue-session", json={"goal": ""})
    assert clr.json() == {"queued": False, "goal": None}


# ===========================================================================
# start-session endpoint
# ===========================================================================

def test_start_session_project_not_found(client):
    r = client.post("/projects/nope/start-session", json={"session_name": "x"})
    assert r.status_code == 404


def test_start_session_happy(client):
    p = _create_project(client, "ss-proj")
    r = client.post(
        f"/projects/{p['id']}/start-session",
        json={"session_name": "feature-x"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "session_id" in body or "session" in body


# ===========================================================================
# tasks/enqueue
# ===========================================================================

def test_enqueue_task_project_not_found(client):
    r = client.post(
        "/tasks/enqueue",
        json={"project_id": "nope", "session_id": "s", "prompt": "do it"},
    )
    assert r.status_code == 404


def test_enqueue_task_session_not_found(client):
    p = _create_project(client, "enq-proj")
    r = client.post(
        "/tasks/enqueue",
        json={"project_id": p["id"], "session_id": "no-such-session", "prompt": "go"},
    )
    assert r.status_code == 404


# ===========================================================================
# hooks/session-start + hooks/stop
# ===========================================================================

def test_hooks_stop_missing_project(client):
    r = client.post("/hooks/stop", json={})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_hooks_stop_happy(client):
    p = _create_project(client, "hooks-stop-proj")
    r = client.post("/hooks/stop", json={"project_id": p["id"]})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_hooks_stop_transcript_narrative(client, tmp_path):
    # 571b8b60 — Stop hook reads transcript_path and folds the assistant work
    # narrative into the delta handoff body.
    import json as _json
    from pathlib import Path as _Path

    p = _create_project(client, "hooks-stop-transcript")
    sess = client.post(
        "/sessions/register", json={"project_id": p["id"], "name": "exec-h"}
    ).json()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        _json.dumps({"message": {"role": "assistant",
                     "content": "Refactored the auth module and added 3 tests."}})
        + "\n",
        encoding="utf-8",
    )
    r = client.post("/hooks/stop", json={
        "project_id": p["id"], "session_id": sess["id"],
        "transcript_path": str(transcript),
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["handoff"]["transcript_narrative"] is True
    handoff_text = _Path(body["handoff"]["path"]).read_text(encoding="utf-8")
    assert "Session narrative (from transcript)" in handoff_text
    assert "Refactored the auth module" in handoff_text


def test_hooks_session_start_no_project_no_projects(client):
    # No project_id and no projects in DB -> 400.
    r = client.post("/hooks/session-start", json={})
    assert r.status_code == 400
    assert "no projects" in r.json()["error"].lower()


def test_hooks_session_start_explicit_project_not_found(client):
    r = client.post("/hooks/session-start", json={"project_id": "missing"})
    assert r.status_code == 404
    assert r.json()["error"] == "project not found"


def test_hooks_session_start_explicit_project_happy(client):
    p = _create_project(client, "hooks-ss-proj")
    r = client.post(
        "/hooks/session-start",
        json={"project_id": p["id"], "session_name": "hook1"},
    )
    assert r.status_code == 200
    ctx = r.json()["hookSpecificOutput"]
    assert ctx["hookEventName"] == "SessionStart"
    assert p["id"] in ctx["additionalContext"]


def test_hooks_session_start_autoroute_single_project(client):
    # Exactly one project, no project_id -> auto-route to it.
    p = _create_project(client, "hooks-ss-only")
    r = client.post(
        "/hooks/session-start",
        json={"session_name": "auto", "cwd": "/mnt/c/work/repo", "hostname": "mybox"},
    )
    assert r.status_code == 200
    ctx = r.json()["hookSpecificOutput"]
    assert p["id"] in ctx["additionalContext"]


def test_hooks_stop_bad_bearer_token(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        r = client.post(
            "/hooks/stop",
            json={"project_id": "p"},
            headers={"Authorization": "Bearer sk_meridian_invalid"},
        )
        assert r.status_code == 401


# ===========================================================================
# auth tokens (self-hosted: _get_authenticated_tenant -> 401)
# ===========================================================================

def test_create_token_requires_auth(client):
    r = client.post("/auth/tokens", json={})
    assert r.status_code == 401


def test_list_tokens_requires_auth(client):
    r = client.get("/auth/tokens")
    assert r.status_code == 401


def test_delete_token_requires_auth(client):
    r = client.delete("/auth/tokens/some-id")
    assert r.status_code == 401


def test_get_me_requires_auth(client):
    r = client.get("/auth/me")
    assert r.status_code == 401


def test_orphaned_keys_requires_auth(client):
    r = client.delete("/api/keys/orphaned")
    assert r.status_code == 401


# ===========================================================================
# auth tokens — hosted happy paths
# ===========================================================================

def test_create_token_hosted(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _t, raw = _run(_new_tenant_token(client.app.state.db, "tok-create@example.com"))
        r = client.post(
            "/auth/tokens",
            headers=_hdr(raw),
            json={"label": "ci", "token_type": "readonly"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["token"].startswith("sk_meridian_")
        assert body["label"] == "ci"
        assert body["token_type"] == "readonly"


def test_create_token_bad_type_defaults_readwrite(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _t, raw = _run(_new_tenant_token(client.app.state.db, "tok-badtype@example.com"))
        r = client.post(
            "/auth/tokens",
            headers=_hdr(raw),
            json={"token_type": "garbage"},
        )
        assert r.status_code == 201
        assert r.json()["token_type"] == "readwrite"


def test_get_me_hosted(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        tenant, raw = _run(_new_tenant_token(client.app.state.db, "authme@example.com"))
        r = client.get("/auth/me", headers=_hdr(raw))
        assert r.status_code == 200
        body = r.json()
        assert body["email"] == "authme@example.com"
        assert "neon_db_url" not in body
        assert "github_pat" not in body
        assert body["github_connected"] is False
        assert isinstance(body["projects"], list)


def test_orphaned_keys_hosted(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _t, raw = _run(_new_tenant_token(client.app.state.db, "orphan-cov@example.com"))
        r = client.delete("/api/keys/orphaned", headers=_hdr(raw))
        assert r.status_code == 200
        assert "deleted" in r.json()


def test_delete_token_not_found_hosted(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _t, raw = _run(_new_tenant_token(client.app.state.db, "del-tok@example.com"))
        r = client.delete("/auth/tokens/nonexistent-id", headers=_hdr(raw))
        assert r.status_code == 404


# ===========================================================================
# auth/install — hosted only
# ===========================================================================

def test_auth_install_self_hosted_404(client):
    r = client.get("/auth/install")
    assert r.status_code == 404


def test_auth_install_hosted_requires_auth(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        r = client.get("/auth/install")
        assert r.status_code == 401


def test_auth_install_hosted_happy(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _t, raw = _run(_new_tenant_token(client.app.state.db, "install@example.com"))
        r = client.get("/auth/install", headers=_hdr(raw))
        assert r.status_code == 200
        assert "Meridian Connect" in r.text


# ===========================================================================
# tunnel device-code flow (hosted only)
# ===========================================================================

def test_tunnel_connect_self_hosted_404(client):
    r = client.get("/auth/tunnel-connect?device_code=abc")
    assert r.status_code == 404


def test_tunnel_connect_missing_device_code(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        r = client.get("/auth/tunnel-connect")
        assert r.status_code == 400


def test_tunnel_connect_unauth_redirects_to_login(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        r = client.get(
            "/auth/tunnel-connect?device_code=dev123",
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/auth/login" in r.headers["location"]


def test_tunnel_connect_authorize_and_poll(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _t, raw = _run(_new_tenant_token(client.app.state.db, "tunnel-flow@example.com"))
        # Poll before authorize -> pending
        pending = client.get("/auth/tunnel-poll?device_code=devcode-1")
        assert pending.status_code == 200
        assert pending.json()["status"] == "pending"
        # Authorize
        auth = client.post(
            "/auth/tunnel-connect",
            headers=_hdr(raw),
            json={"device_code": "devcode-1"},
        )
        assert auth.status_code == 200
        assert auth.json()["status"] == "ok"
        # Idempotent re-authorize
        auth2 = client.post(
            "/auth/tunnel-connect",
            headers=_hdr(raw),
            json={"device_code": "devcode-1"},
        )
        assert auth2.json()["status"] == "ok"
        # Poll now completes and returns the token (consumed once)
        done = client.get("/auth/tunnel-poll?device_code=devcode-1")
        assert done.status_code == 200
        assert done.json()["status"] == "complete"
        assert done.json()["token"].startswith("sk_meridian_")
        # Second poll -> pending again (consumed)
        again = client.get("/auth/tunnel-poll?device_code=devcode-1")
        assert again.json()["status"] == "pending"


def test_tunnel_authorize_missing_device_code(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _t, raw = _run(_new_tenant_token(client.app.state.db, "tunnel-nodc@example.com"))
        r = client.post("/auth/tunnel-connect", headers=_hdr(raw), json={})
        assert r.status_code == 400


def test_tunnel_poll_missing_device_code(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        r = client.get("/auth/tunnel-poll")
        assert r.status_code == 400


# ===========================================================================
# settings/notifications + usage (hosted only)
# ===========================================================================

def test_notifications_self_hosted_404(client):
    assert client.get("/settings/notifications").status_code == 404
    assert client.patch("/settings/notifications", json={}).status_code == 404


def test_notifications_round_trip(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _login(client, "notif@example.com")
        get0 = client.get("/settings/notifications")
        assert get0.status_code == 200
        assert "prefs" in get0.json()
        patched = client.patch(
            "/settings/notifications",
            json={"hitl": False},
        )
        assert patched.status_code == 200
        assert patched.json()["status"] == "ok"


def test_usage_self_hosted_404(client):
    assert client.get("/settings/usage").status_code == 404
    assert client.patch("/settings/usage", json={}).status_code == 404


def test_usage_round_trip(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _login(client, "usage@example.com")
        got = client.get("/settings/usage")
        assert got.status_code == 200
        body = got.json()
        assert "compute" in body and "storage" in body
        patched = client.patch(
            "/settings/usage",
            json={"compute_cap": 5, "storage_cap": 2},
        )
        assert patched.status_code == 200
        assert patched.json()["compute_cap"] == 5.0


def test_mcp_config_self_hosted_404(client):
    assert client.get("/settings/mcp-config").status_code == 404


def test_mcp_config_hosted(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _login(client, "mcpcfg@example.com", seed_project_db=True)
        r = client.get("/settings/mcp-config")
        assert r.status_code == 200
        body = r.json()
        assert "projects" in body
        assert "base_url" in body


# ===========================================================================
# export/my-data (hosted only)
# ===========================================================================

def test_export_self_hosted_404(client):
    r = client.get("/export/my-data")
    assert r.status_code == 404


def test_export_hosted_happy(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _login(client, "export@example.com", seed_project_db=True)
        r = client.get("/export/my-data")
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]
        assert r.headers["content-type"].startswith("application/json")


# ===========================================================================
# workspace endpoints (hosted only)
# ===========================================================================

def test_workspace_invite_self_hosted_404(client):
    r = client.post("/workspace/invite", json={"email": "x@y.com"})
    assert r.status_code == 404


def test_workspace_members_self_hosted_404(client):
    assert client.get("/workspace/members").status_code == 404


def test_workspace_members_hosted_lists(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _login(client, "ws-list@example.com")
        r = client.get("/workspace/members")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


def test_workspace_invite_bad_email(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _login(client, "ws-invite@example.com")
        r = client.post("/workspace/invite", json={"email": "not-an-email"})
        assert r.status_code == 422


def test_workspace_invite_bad_role(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _login(client, "ws-invite2@example.com")
        r = client.post(
            "/workspace/invite",
            json={"email": "new@example.com", "role": "wizard"},
        )
        assert r.status_code == 422


def test_workspace_invite_happy(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _login(client, "ws-owner@example.com")
        r = client.post(
            "/workspace/invite",
            json={"email": "member@example.com", "role": "member"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["email"] == "member@example.com"
        assert body["pending"] is True


def test_workspace_update_member_requires_fields(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _login(client, "ws-upd@example.com")
        r = client.patch("/workspace/members/some-id", json={})
        assert r.status_code == 422


def test_workspace_update_member_not_found(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _login(client, "ws-upd2@example.com")
        r = client.patch("/workspace/members/no-such-member", json={"role": "member"})
        assert r.status_code == 404


def test_workspace_remove_member_hosted(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _login(client, "ws-rm@example.com")
        r = client.delete("/workspace/members/whatever")
        assert r.status_code == 204


def test_workspace_invite_resend_not_found(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _login(client, "ws-resend@example.com")
        r = client.post("/workspace/invite/no-member/resend")
        assert r.status_code == 404


def test_workspace_accept_self_hosted_404(client):
    r = client.get("/workspace/accept?token=abc")
    assert r.status_code == 404


def test_workspace_accept_missing_token(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        r = client.get("/workspace/accept")
        assert r.status_code == 400


def test_workspace_accept_unauth_redirects(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        r = client.get("/workspace/accept?token=sometoken", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["location"]


def test_workspace_connect_db_self_hosted_404(client):
    r = client.post("/workspace/connect-db", json={"url": "postgresql://x"})
    assert r.status_code == 404


def test_workspace_connect_db_bad_url(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _login(client, "ws-db@example.com")
        r = client.post("/workspace/connect-db", json={"url": "mysql://nope"})
        assert r.status_code == 422


# ===========================================================================
# changelog admin API (hosted gating)
# ===========================================================================

def test_changelog_create_self_hosted_403(client):
    r = client.post("/api/admin/changelog-entries", json={"title": "x"})
    assert r.status_code == 403


def test_changelog_update_self_hosted_403(client):
    r = client.patch("/api/admin/changelog-entries/x", json={})
    assert r.status_code == 403


def test_changelog_delete_self_hosted_403(client):
    r = client.delete("/api/admin/changelog-entries/x")
    assert r.status_code == 403


def test_changelog_create_non_admin_403(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        # email not in MERIDIAN_ADMIN_EMAILS → is_admin_db False → 403.
        monkeypatch.delenv("MERIDIAN_ADMIN_EMAILS", raising=False)
        _login(client, "notadmin@example.com", seed_project_db=True)
        r = client.post("/api/admin/changelog-entries", json={"title": "v1"})
        assert r.status_code == 403


def test_changelog_entries_json(client):
    r = client.get("/api/changelog-entries")
    assert r.status_code == 200
    assert "entries" in r.json()


# ===========================================================================
# admin login
# ===========================================================================

def test_admin_login_page(client):
    r = client.get("/admin/login")
    assert r.status_code == 200
    assert "Admin" in r.text


def test_admin_login_no_password_configured_redirects(client, monkeypatch):
    monkeypatch.delenv("MERIDIAN_ADMIN_PASSWORD", raising=False)
    r = client.post("/admin/login", data={"password": "x"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin"


def test_admin_login_wrong_password(client, monkeypatch):
    monkeypatch.setenv("MERIDIAN_ADMIN_PASSWORD", "supersecret")
    r = client.post("/admin/login", data={"password": "wrong"}, follow_redirects=False)
    assert r.status_code == 401
    assert "Incorrect password" in r.text


def test_admin_login_correct_password(client, monkeypatch):
    monkeypatch.setenv("MERIDIAN_ADMIN_PASSWORD", "letmein")
    r = client.post("/admin/login", data={"password": "letmein"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin"
    assert any("meridian_admin" in v for v in r.headers.get_list("set-cookie"))


# ===========================================================================
# registered machines (hooks OAuth) — require auth
# ===========================================================================

def test_list_registered_machines_requires_auth(client):
    r = client.get("/projects/p/registered-machines")
    assert r.status_code == 401


def test_revoke_machine_requires_auth(client):
    r = client.delete("/projects/p/registered-machines/m")
    assert r.status_code == 401


def test_hooks_status_requires_auth(client):
    r = client.get("/auth/hooks-status")
    assert r.status_code == 401


def test_hooks_connect_unauth_redirects(client):
    r = client.get("/auth/hooks-connect?hostname=box", follow_redirects=False)
    assert r.status_code == 303
    assert "/auth/login" in r.headers["location"]


def test_registered_machines_hosted_empty(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _t, raw = _run(_new_tenant_token(client.app.state.db, "machines@example.com"))
        r = client.get("/projects/p/registered-machines", headers=_hdr(raw))
        assert r.status_code == 200
        assert isinstance(r.json(), list)


def test_revoke_machine_not_found_hosted(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _t, raw = _run(_new_tenant_token(client.app.state.db, "machines2@example.com"))
        r = client.delete("/projects/p/registered-machines/nope", headers=_hdr(raw))
        assert r.status_code == 404


def test_hooks_connect_missing_hostname_hosted(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _t, raw = _run(_new_tenant_token(client.app.state.db, "hc@example.com"))
        r = client.get("/auth/hooks-connect", headers=_hdr(raw))
        assert r.status_code == 400


def test_hooks_connect_happy_hosted(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _t, raw = _run(_new_tenant_token(client.app.state.db, "hc2@example.com"))
        r = client.get("/auth/hooks-connect?hostname=mybox", headers=_hdr(raw))
        assert r.status_code == 200
        assert "connected" in r.text


# ===========================================================================
# feedback (hosted only)
# ===========================================================================

def test_feedback_self_hosted_404(client):
    r = client.post("/feedback", json={"message": "hi"})
    assert r.status_code == 404


def test_demo_read_only_middleware_blocks_mutations(monkeypatch, tmp_path):
    """The demo cookie makes the read-only middleware reject mutating verbs."""
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        r = client.post(
            "/feedback",
            json={"message": "great"},
            cookies={"meridian_demo": "1"},
        )
        assert r.status_code == 403
        assert r.json()["error"] == "demo_readonly"


def test_feedback_empty_message(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _login(client, "fb@example.com")
        r = client.post("/feedback", json={"message": "   "})
        assert r.status_code == 400


def test_feedback_bad_email(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _login(client, "fb2@example.com")
        r = client.post("/feedback", json={"message": "ok", "email": "bademail"})
        assert r.status_code == 400


def test_feedback_happy(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _login(client, "fb3@example.com")
        r = client.post("/feedback", json={"type": "bug", "message": "found a bug"})
        assert r.status_code == 201
        assert "id" in r.json()


# ===========================================================================
# MCP endpoints
# ===========================================================================

def test_mcp_get_json(client):
    r = client.get("/mcp")
    assert r.status_code == 200
    assert r.json()["name"] == "meridian"


def test_mcp_sse_options(client):
    r = client.options("/mcp/sse")
    assert r.status_code == 204
    assert r.headers["Access-Control-Allow-Origin"] == "*"


def test_mcp_sse_post_initialize(client):
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
    }
    r = client.post("/mcp/sse", json=payload)
    assert r.status_code == 200
    assert r.json()["jsonrpc"] == "2.0"


def test_mcp_sse_post_tools_list(client):
    payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    r = client.post("/mcp/sse", json=payload)
    assert r.status_code == 200
    result = r.json()["result"]
    assert "tools" in result


def test_mcp_sse_post_batch(client):
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    r = client.post("/mcp/sse", json=payload)
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert len(r.json()) == 2


def test_mcp_sse_post_parse_error(client):
    r = client.post(
        "/mcp/sse",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32700


def test_remote_mcp_requires_token(client):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    r = client.post("/mcp", json=payload)
    # No bearer token → 401 with OAuth discovery header.
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_remote_mcp_with_token(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _t, raw = _run(_new_tenant_token(client.app.state.db, "remote-mcp@example.com"))
        payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        r = client.post("/mcp", headers=_hdr(raw), json=payload)
        assert r.status_code == 200
        assert r.json()["jsonrpc"] == "2.0"


# ===========================================================================
# helper functions (direct unit coverage)
# ===========================================================================

def test_jsonrpc_helpers():
    import meridian.server as server_module

    ok = server_module._jsonrpc_ok(7, {"a": 1})
    assert ok == {"jsonrpc": "2.0", "id": 7, "result": {"a": 1}}
    err = server_module._jsonrpc_err(7, -32601, "no method")
    assert err["error"]["code"] == -32601
    assert err["error"]["message"] == "no method"


def test_resolved_tunnel_plugins_defaults():
    import meridian.server as server_module

    plugins = server_module._resolved_tunnel_plugins(None)
    assert isinstance(plugins, list)
    names = [p["name"] for p in plugins]
    assert {"filesystem", "code-intel", "code-extractor"}.issubset(set(names))


def test_resolved_tunnel_plugins_from_json_string():
    import meridian.server as server_module

    plugins = server_module._resolved_tunnel_plugins('{"code-intel": {"enabled": false}}')
    assert isinstance(plugins, list)


def test_notification_prefs_from_raw():
    import meridian.server as server_module

    prefs = server_module._notification_prefs_from_raw(None)
    assert isinstance(prefs, dict)
    # round-trip a JSON string — toggling a known pref key is reflected back
    prefs2 = server_module._notification_prefs_from_raw('{"hitl": false}')
    assert prefs2.get("hitl") is False


def test_mcp_rate_check_window():
    import meridian.server as server_module

    th = "ratecheck-" + hashlib.sha256(b"x").hexdigest()
    server_module._mcp_rate_counters.pop(th, None)
    # First call within limit -> not limited
    assert server_module._mcp_rate_check(th, limit=2) is False
    assert server_module._mcp_rate_check(th, limit=2) is False
    # Third exceeds limit of 2 -> limited
    assert server_module._mcp_rate_check(th, limit=2) is True


def test_notification_pref_enabled():
    import meridian.server as server_module

    # No pref key / no tenant -> enabled.
    assert server_module._notification_pref_enabled(None, None) is True
    assert server_module._notification_pref_enabled(None, "hitl") is True
    # Disabled pref in tenant.
    tenant = {"notification_prefs": '{"hitl": false}'}
    assert server_module._notification_pref_enabled(tenant, "hitl") is False
    # Unset pref defaults to enabled.
    assert server_module._notification_pref_enabled(tenant, "sprint") is True


def test_response_error_detail_variants():
    import meridian.server as server_module

    class _Resp:
        def __init__(self, payload=None, text="", status_code=400):
            self._payload = payload
            self.text = text
            self.status_code = status_code

        def json(self):
            if self._payload is None:
                raise ValueError("no json")
            return self._payload

    assert server_module._response_error_detail(_Resp({"message": "bad"})) == "bad"
    assert server_module._response_error_detail(_Resp({"error": {"message": "nested"}})) == "nested"
    assert server_module._response_error_detail(_Resp(text="plain text err")) == "plain text err"
    assert "400" in server_module._response_error_detail(_Resp(status_code=400))


def test_send_email_notification_skips_without_key(monkeypatch):
    import meridian.server as server_module

    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    # Returns immediately (None) without touching the network.
    _run(server_module._send_email_notification("a@b.com", "subj", "body"))


def test_dispatch_notification_email_route(monkeypatch):
    """Email-style target routes through _send_email_notification."""
    import meridian.server as server_module

    called = {}

    async def _fake_send(to_email, subject, body_text):
        called["to"] = to_email
        called["subject"] = subject

    monkeypatch.setattr(server_module, "_send_email_notification", _fake_send)
    _run(server_module._dispatch_notification("user@example.com", "Title", "Body"))
    assert called["to"] == "user@example.com"
    assert called["subject"] == "[Meridian] Title"


def test_dispatch_notification_empty_url_noop():
    import meridian.server as server_module

    # Empty URL just returns — no exception.
    _run(server_module._dispatch_notification("   ", "t", "b"))


def test_github_tools_for_tenant_no_pat():
    import meridian.server as server_module

    assert server_module._github_tools_for_tenant({}) == []
    assert server_module._github_tools_for_tenant({"github_pat": None}) == []


def test_github_tools_for_tenant_with_pat():
    import meridian.server as server_module
    from meridian import db as db_module

    encrypted = db_module.encrypt_field("ghp_faketoken123")
    tools = server_module._github_tools_for_tenant({"github_pat": encrypted})
    assert isinstance(tools, list)
    assert len(tools) >= 5
    names = {t["name"] for t in tools}
    assert "read_file" in names
    assert "list_files" in names
    assert all("inputSchema" in t for t in tools)


def test_hook_is_executor():
    import meridian.server as server_module

    assert server_module._hook_is_executor({"permission_mode": "bypassPermissions"}) is True
    assert server_module._hook_is_executor({"session_role": "executor"}) is True
    assert server_module._hook_is_executor({"executor": True}) is True
    assert server_module._hook_is_executor({}) is False


def test_is_demo_request_env(monkeypatch, tmp_path):
    import meridian.server as server_module
    from starlette.requests import Request

    monkeypatch.setenv("MERIDIAN_DEMO", "true")
    scope = {"type": "http", "headers": [], "method": "GET", "path": "/"}
    req = Request(scope)
    assert server_module._is_demo_request(req) is True
