"""ece2ac0a — P0: the hosted service must fail CLOSED for unauthenticated REST.

Regression suite for the anonymous-access bug: in hosted mode
``_deps._db(request)`` used to fall back to the shared control-plane DB
(``app.state.db``) when no credential resolved a tenant, so an anonymous
``GET /projects`` listed the operator's real projects, ``GET
/projects/{id}/notes`` returned note bodies, every write route accepted
anonymous writes, and a handful of routes that never touched ``_db()``
(``POST /tunnel/plugins/install``, ``POST /admin/shutdown`` ...) let anyone act
on the server process itself.

What this file pins down:

* The app-wide gate (``meridian/hosted_route_gate.py``) is attached to EVERY
  route, and its two allowlists are exactly the reviewed sets below.
* Every non-allowlisted route, generated from the LIVE route table (so a route
  added later is covered automatically), returns 401 + ``WWW-Authenticate:
  Bearer`` to an anonymous hosted caller and to a bogus Bearer token.
* Every self-authenticated route still refuses anonymous callers on its own.
* Public routes are not gated; the key public pages still serve anonymously.
* A valid session cookie or API token reaches ONLY that tenant's own DB; an
  unprovisioned tenant still gets 503 (not 401); the admin tenant still
  reaches the control-plane DB it lives in.
* Self-hosted mode and the demo cookie are unchanged.
* The non-``_db()`` routes (server process, server filesystem, SSE sessions,
  hooks, document peeks) each refuse correctly.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import re
import subprocess
import uuid

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute

from meridian import _deps
from meridian import db as db_module
from meridian import hosted_route_gate as gate
import meridian.server as _server_at_collection

DUMMY_ID = "00000000-0000-4000-8000-000000000000"
_GATE_401 = {"detail": "authentication required"}

# ---------------------------------------------------------------------------
# The intended allowlists, spelled out independently of the module so that
# widening the gate is a deliberate two-place edit.
# ---------------------------------------------------------------------------

EXPECTED_PUBLIC = {
    ("GET", "/"), ("GET", "/robots.txt"), ("GET", "/favicon.ico"), ("GET", "/sw.js"),
    ("GET", "/manifest.webmanifest"), ("GET", "/sitemap.xml"), ("GET", "/terms"),
    ("GET", "/privacy"), ("GET", "/pricing"), ("GET", "/onboarding"),
    ("GET", "/install-mcp"), ("GET", "/tools"), ("GET", "/setup"), ("GET", "/blog"),
    ("GET", "/blog/{slug}"), ("GET", "/waitlist-pending"), ("GET", "/changelog"),
    ("GET", "/api/changelog-entries"), ("POST", "/waitlist"),
    ("GET", "/.well-known/oauth-authorization-server"),
    ("GET", "/.well-known/openid-configuration"),
    ("GET", "/.well-known/oauth-protected-resource"),
    ("GET", "/.well-known/agent.json"), ("POST", "/.well-known/agent.json"),
    ("POST", "/oauth/register"), ("POST", "/oauth/device"), ("POST", "/oauth/token"),
    ("GET", "/oauth/device-callback"),
    ("GET", "/auth/login"), ("GET", "/auth/callback"), ("GET", "/auth/google/login"),
    ("GET", "/auth/github/login"), ("GET", "/auth/github/callback"),
    ("GET", "/auth/microsoft/login"), ("GET", "/auth/microsoft/callback"),
    ("GET", "/auth/email-required"), ("GET", "/auth/logout"), ("POST", "/auth/magic"),
    ("GET", "/auth/magic/verify"), ("GET", "/auth/tunnel-poll"),
    ("GET", "/admin/login"), ("POST", "/admin/login"), ("POST", "/__gate__"),
    ("GET", "/demo"), ("POST", "/demo-auth"),
    ("GET", "/health"), ("GET", "/health/deep"), ("GET", "/failover-status"),
    ("GET", "/status/server"), ("GET", "/status/tools"), ("GET", "/status/sessions"),
    ("GET", "/setup/health"), ("GET", "/admin/__error_test"),
    ("GET", "/mcp"), ("OPTIONS", "/mcp/sse"), ("GET", "/mcp/quickstart"),
    ("GET", "/mcp/tools-doc"), ("GET", "/api-reference-doc"), ("GET", "/config"),
    ("GET", "/config/api-key"),
    ("GET", "/install.sh"), ("GET", "/install.ps1"), ("GET", "/install-windows.ps1"),
    ("GET", "/install_tunnel.sh"), ("GET", "/install_tunnel.ps1"),
    ("GET", "/install_watcher.sh"), ("GET", "/install_watcher.ps1"),
    ("GET", "/hooks.sh"), ("GET", "/hooks.ps1"), ("GET", "/hooks_install.ps1"),
    ("GET", "/hooks/diagnostics"), ("POST", "/pkg-guard/check"),
    ("GET", "/tunnel/registry"), ("GET", "/tunnel/plugins/check"),
    ("POST", "/tunnel/openai/diagnostics/{tenant_id}"),
    ("GET", "/me"), ("GET", "/me/workspaces"), ("GET", "/tunnel/plugins"),
    ("GET", "/tunnel/filesystem-roots"),
    ("GET", "/control-plane/artifacts"),
    ("GET", "/projects/{project_id}/agent-instructions/default"),
}

EXPECTED_SELF_AUTHENTICATED = {
    ("POST", "/webhooks/stripe"), ("POST", "/webhooks/github-marketplace"),
    ("POST", "/projects/{project_id}/events"),
    ("POST", "/mcp"), ("POST", "/mcp/openai"),
    ("POST", "/hooks/session-start"), ("POST", "/hooks/stop"),
    ("GET", "/dashboard"), ("GET", "/admin"), ("GET", "/activate"), ("POST", "/activate"),
    ("GET", "/oauth/authorize"), ("GET", "/checkout"), ("GET", "/billing/portal"),
    ("POST", "/billing/portal"), ("GET", "/auth/github/repo-connect"),
    ("GET", "/auth/github/repo-callback"), ("GET", "/auth/hooks-connect"),
    ("GET", "/auth/tunnel-connect"), ("POST", "/auth/tunnel-connect"),
    ("GET", "/auth/install"), ("GET", "/workspace/accept"),
    ("GET", "/auth/me"), ("GET", "/auth/hooks-status"), ("GET", "/auth/tokens"),
    ("POST", "/auth/tokens"), ("DELETE", "/auth/tokens/{token_id}"),
    ("DELETE", "/api/keys/orphaned"), ("GET", "/account/sessions"),
    ("POST", "/account/sessions/{session_id}/revoke"), ("POST", "/account/delete"),
    ("GET", "/export/my-data"), ("POST", "/feedback"), ("GET", "/settings/mcp-config"),
    ("GET", "/settings/notifications"), ("PATCH", "/settings/notifications"),
    ("GET", "/settings/usage"), ("PATCH", "/settings/usage"),
    ("GET", "/projects/{project_id}/registered-machines"),
    ("DELETE", "/projects/{project_id}/registered-machines/{machine_id}"),
    ("POST", "/workspace/connect-db"), ("POST", "/workspace/invite"),
    ("POST", "/workspace/invite/{member_id}/resend"), ("GET", "/workspace/members"),
    ("PATCH", "/workspace/members/{member_id}"), ("DELETE", "/workspace/members/{member_id}"),
    ("GET", "/github/connections"), ("DELETE", "/github/connections/{account_login}"),
    ("GET", "/projects/{project_id}/github/status"),
    ("GET", "/projects/{project_id}/github/repos"),
    ("GET", "/projects/{project_id}/github/branches"),
    ("POST", "/projects/{project_id}/github/connect"),
    ("DELETE", "/projects/{project_id}/github/disconnect"),
    ("PATCH", "/projects/{project_id}/github/account"),
    ("POST", "/projects/{project_id}/github/push-mcp-template"),
    ("GET", "/projects/{project_id}/repo-image"),
    ("GET", "/admin/health"), ("GET", "/admin/stats"), ("GET", "/admin/waitlist"),
    ("DELETE", "/admin/waitlist/{entry_id}"),
    ("POST", "/admin/tenants/{tenant_id}/reset-provisioning"),
    ("GET", "/admin/blog/posts"), ("POST", "/admin/blog/posts"),
    ("GET", "/admin/blog/posts/{post_id}"), ("DELETE", "/admin/blog/posts/{post_id}"),
    ("POST", "/admin/blog/posts/{post_id}/publish"),
    ("POST", "/admin/blog/posts/{post_id}/unpublish"),
    ("POST", "/admin/blog/generate-draft"),
    ("POST", "/api/admin/changelog-entries"),
    ("PATCH", "/api/admin/changelog-entries/{entry_id}"),
    ("DELETE", "/api/admin/changelog-entries/{entry_id}"),
    ("POST", "/config/connections"), ("DELETE", "/config/connections/{name}"),
    ("GET", "/tunnel/manifest"), ("POST", "/tunnel/active-repo"), ("POST", "/tunnel/refresh"),
    ("GET", "/tunnel/status/{tenant_id}"), ("PUT", "/tunnel/plugins"),
    ("POST", "/tunnel/plugins/custom"), ("DELETE", "/tunnel/plugins/custom"),
    ("POST", "/tunnel/filesystem-roots"), ("DELETE", "/tunnel/filesystem-roots"),
} | {
    (method, f"/{slot}/mcp/{{tenant_id}}{suffix}")
    for slot in ("fs", "code", "extract", "ppt", "word", "dc", "docs", "zotero", "outputs", "debug")
    for suffix in ("", "/{rest:path}")
    for method in ("GET", "POST", "OPTIONS")
}


def _route_keys(app) -> set[tuple[str, str]]:
    return {
        (m, r.path)
        for r in app.routes
        if isinstance(r, APIRoute)
        for m in r.methods
    }


_ALL_KEYS = sorted(_route_keys(_server_at_collection.app))
_GATED_KEYS = [k for k in _ALL_KEYS if not gate.is_gate_exempt(*k)]
_PUBLIC_KEYS = sorted(k for k in _ALL_KEYS if k in gate.HOSTED_PUBLIC_ROUTES)
_SELF_AUTH_KEYS = sorted(k for k in _ALL_KEYS if k in gate.HOSTED_SELF_AUTHENTICATED_ROUTES)


def _kid(key: tuple[str, str]) -> str:
    return f"{key[0]} {key[1]}"


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", DUMMY_ID, path)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _refuse(*_a, **_k):
    raise RuntimeError("ece2ac0a test guard: server-side side effect attempted")


async def _arefuse(*_a, **_k):
    raise RuntimeError("ece2ac0a test guard: server-side subprocess attempted")


@pytest.fixture(scope="module")
def hosted(tmp_path_factory):
    """One hosted-mode app for the whole module (the sweep is ~500 requests).

    ``os.kill`` is disabled for the module's lifetime so that a regression
    re-opening /admin/shutdown or /admin/restart can never take down the test
    worker via their delayed kill task.
    """
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("MERIDIAN_HOSTED", "1")
        mp.setenv("MERIDIAN_SESSION_SECRET", "test-secret")
        mp.setenv("MERIDIAN_DB", ":memory:")
        mp.setenv("MERIDIAN_DB_URL", "")
        mp.setenv("MERIDIAN_DEMO_DB_URL", "")
        mp.setenv("MERIDIAN_SKIP_DEMO", "1")
        mp.setenv("MERIDIAN_AUTH_DB", "")
        mp.setenv("MERIDIAN_ADMIN_PASSWORD", "")
        mp.setenv("MERIDIAN_ADMIN_EMAILS", "operator@example.com")
        mp.setenv("SITE_PASSWORD", "")
        mp.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_only")
        mp.setenv("GITHUB_MARKETPLACE_WEBHOOK_SECRET", "gh_test_only")
        data_dir = str(tmp_path_factory.mktemp("ece2ac0a"))
        mp.setenv("MERIDIAN_DATA_DIR", data_dir)
        mp.setenv("MERIDIAN_GOAL_MD", os.path.join(data_dir, "GOAL.md"))
        mp.setenv("MERIDIAN_MD_ROOT", data_dir)
        mp.setattr(os, "kill", _refuse)

        from fastapi.testclient import TestClient
        import meridian.server as server_module
        server_module = importlib.reload(server_module)
        _deps._reset_limiter_counts()
        with TestClient(server_module.app) as c:
            # The operator's real data lives in the control-plane DB.
            op = asyncio.run(db_module.create_project(c.app.state.db, "operator-secret-proj"))
            asyncio.run(db_module.add_project_note(
                c.app.state.db, op["id"], "operator note", "operator-secret-note-body",
            ))
            yield c, server_module, op
        _deps._reset_limiter_counts()
    finally:
        mp.undo()


@pytest.fixture(autouse=True)
def _fresh_request_state(hosted):
    c = hosted[0]
    c.cookies.clear()
    _deps._reset_limiter_counts()
    yield
    c.cookies.clear()


@pytest.fixture
def no_side_effects(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _refuse)
    monkeypatch.setattr(subprocess, "run", _refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _arefuse)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _arefuse)


def _seed_tenant(c, email: str, *, plan: str = "free") -> dict:
    db = c.app.state.db
    t = asyncio.run(db_module.upsert_tenant(db, email))
    asyncio.run(db_module.update_tenant(db, t["id"], plan=plan))
    return asyncio.run(db_module.get_tenant_by_id(db, t["id"]))


def _cookie_header(c, tenant_id: str) -> dict[str, str]:
    from meridian.hosted import _make_session_cookie
    session = asyncio.run(
        db_module.create_user_session(c.app.state.db, tenant_id, "2099-01-01 00:00:00")
    )
    return {"Cookie": f"meridian_session={_make_session_cookie(session['id'])}"}


def _bearer_header(c, tenant_id: str) -> dict[str, str]:
    raw, _row = asyncio.run(db_module.create_api_token(c.app.state.db, tenant_id, label="t"))
    return {"Authorization": f"Bearer {raw}"}


@pytest.fixture
def own_db_tenant(hosted):
    """A non-admin tenant with its OWN database (injected into the per-tenant
    DB cache, exactly where _open_tenant_db_by_id would put a Neon pool)."""
    c = hosted[0]
    tenant = _seed_tenant(c, f"tenant-{uuid.uuid4().hex[:8]}@example.com", plan="pro")
    conn = asyncio.run(db_module.init_db(":memory:"))
    proj = asyncio.run(db_module.create_project(conn, "tenant-own-proj"))
    _deps._tenant_db_cache[tenant["id"]] = conn
    try:
        yield tenant, conn, proj
    finally:
        _deps._tenant_db_cache.pop(tenant["id"], None)
        asyncio.run(conn.close())


def _is_gate_401(r) -> bool:
    if r.status_code != 401:
        return False
    try:
        return r.json() == _GATE_401
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 1. Wiring and allowlists
# ---------------------------------------------------------------------------

def test_every_route_carries_the_gate(hosted):
    app = hosted[0].app
    missing = []
    for r in app.routes:
        if isinstance(r, (APIRoute, APIWebSocketRoute)):
            calls = [getattr(d, "call", None) for d in r.dependant.dependencies]
            if gate.hosted_route_gate not in calls:
                missing.append(r.path)
    assert missing == []


def test_allowlists_are_exactly_as_intended(hosted):
    assert set(gate.HOSTED_PUBLIC_ROUTES) == EXPECTED_PUBLIC
    assert set(gate.HOSTED_SELF_AUTHENTICATED_ROUTES) == EXPECTED_SELF_AUTHENTICATED
    assert not (gate.HOSTED_PUBLIC_ROUTES & gate.HOSTED_SELF_AUTHENTICATED_ROUTES)
    live = _route_keys(hosted[0].app)
    stale = (EXPECTED_PUBLIC | EXPECTED_SELF_AUTHENTICATED) - live
    assert stale == set(), f"allowlist entries with no live route: {sorted(stale)}"


def test_named_leak_routes_are_gated():
    """The routes confirmed leaking live on prod are all on the gated side."""
    for key in [
        ("GET", "/projects"), ("GET", "/projects/{project_id}"),
        ("GET", "/projects/{project_id}/notes"), ("GET", "/projects/{project_id}/sprint-items"),
        ("GET", "/projects/{project_id}/settings"), ("PATCH", "/projects/{project_id}/settings"),
        ("POST", "/tunnel/plugins/install"), ("POST", "/admin/shutdown"),
        ("POST", "/admin/restart"), ("GET", "/waitlist"), ("GET", "/mcp/sse"),
        ("POST", "/mcp/sse"), ("POST", "/tasks/enqueue"), ("GET", "/admin/snapshot"),
    ]:
        assert key in _GATED_KEYS, key


# ---------------------------------------------------------------------------
# 2. Route-table sweep: anonymous + bogus credential
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", _GATED_KEYS, ids=_kid)
def test_gated_route_rejects_anonymous(hosted, key, no_side_effects):
    c = hosted[0]
    method, path = key
    r = c.request(method, _concrete(path), json={} if method in ("POST", "PUT", "PATCH", "DELETE") else None,
                  follow_redirects=False)
    assert r.status_code == 401, (key, r.status_code, r.text[:200])
    assert r.json() == _GATE_401
    assert r.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize("key", _GATED_KEYS, ids=_kid)
def test_gated_route_rejects_bogus_credentials(hosted, key, no_side_effects):
    c = hosted[0]
    method, path = key
    body = {} if method in ("POST", "PUT", "PATCH", "DELETE") else None
    r = c.request(method, _concrete(path), json=body, follow_redirects=False,
                  headers={"Authorization": "Bearer sk_meridian_not_a_real_token"})
    assert _is_gate_401(r), (key, r.status_code, r.text[:200])
    r = c.request(method, _concrete(path), json=body, follow_redirects=False,
                  headers={"Cookie": "meridian_session=forged.value"})
    assert _is_gate_401(r), (key, r.status_code, r.text[:200])


# Routes whose own anonymous refusal is not a bare 401/403.
_SELF_AUTH_QUERY = {
    "/auth/github/repo-connect": {"project_id": DUMMY_ID},
    "/workspace/accept": {"token": "not-a-real-invite"},
    "/auth/tunnel-connect": {"device_code": "not-a-real-code"},
}


def _self_auth_refused(key, r) -> bool:
    method, path = key
    if key == ("POST", "/hooks/session-start"):
        return r.status_code == 200 and r.json()["hookSpecificOutput"]["additionalContext"] == ""
    if key == ("POST", "/hooks/stop"):
        return r.status_code == 200 and r.json() == {
            "ok": True, "handoff": None, "reason": "unauthenticated",
        }
    if key == ("POST", "/webhooks/stripe"):
        return r.status_code == 400  # signature required (secret configured)
    if r.status_code in (401, 403):
        return True
    if 300 <= r.status_code < 400:
        return "/auth/login" in r.headers.get("location", "") + r.text
    return False


@pytest.mark.parametrize("key", _SELF_AUTH_KEYS, ids=_kid)
def test_self_authenticated_route_refuses_anonymous(hosted, key, no_side_effects):
    c = hosted[0]
    method, path = key
    r = c.request(
        method, _concrete(path),
        json={} if method in ("POST", "PUT", "PATCH", "DELETE") else None,
        params=_SELF_AUTH_QUERY.get(path), follow_redirects=False,
    )
    assert _self_auth_refused(key, r), (key, r.status_code, r.text[:200])


@pytest.mark.parametrize("key", _PUBLIC_KEYS, ids=_kid)
def test_public_route_is_not_gated(hosted, key, no_side_effects):
    c = hosted[0]
    method, path = key
    r = c.request(method, _concrete(path),
                  json={} if method in ("POST", "PUT", "PATCH", "DELETE") else None,
                  follow_redirects=False)
    assert not _is_gate_401(r), key
    assert r.status_code != 500, (key, r.text[:200])


@pytest.mark.parametrize("method,path,expected", [
    ("GET", "/", 200), ("GET", "/health", 200), ("GET", "/changelog", 200),
    ("GET", "/api/changelog-entries", 200), ("GET", "/pricing", 200),
    ("GET", "/onboarding", 200), ("GET", "/auth/login", 200), ("GET", "/mcp", 200),
    ("GET", "/.well-known/oauth-protected-resource", 200),
    ("GET", "/.well-known/oauth-authorization-server", 200),
    ("GET", "/status/server", 200), ("GET", "/install.sh", 200), ("GET", "/me", 200),
])
def test_public_pages_serve_anonymously(hosted, method, path, expected):
    r = hosted[0].request(method, path, follow_redirects=False)
    assert r.status_code == expected, (path, r.text[:200])


def test_anonymous_sees_no_control_plane_data(hosted):
    c, _server, op = hosted
    for path in ("/projects", f"/projects/{op['id']}", f"/projects/{op['id']}/notes",
                 f"/projects/{op['id']}/sprint-items", f"/projects/{op['id']}/settings"):
        r = c.get(path)
        assert r.status_code == 401, path
        assert "operator-secret" not in r.text
    r = c.patch(f"/projects/{op['id']}/settings", json={"auto_handoff": False})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 3. Public routes that used to go through _db()
# ---------------------------------------------------------------------------

def test_public_changelog_reads_control_plane_for_everyone(hosted, own_db_tenant):
    c = hosted[0]
    entry = asyncio.run(db_module.create_changelog_entry(
        c.app.state.db, f"release-{uuid.uuid4().hex[:6]}", "body",
    ))
    tenant, _conn, _proj = own_db_tenant
    for headers in ({}, _cookie_header(c, tenant["id"])):
        r = c.get("/api/changelog-entries", headers=headers)
        assert r.status_code == 200
        assert entry["title"] in [e["title"] for e in r.json()["entries"]]
        page = c.get("/changelog", headers=headers)
        assert page.status_code == 200 and entry["title"] in page.text


def test_public_waitlist_signup_lands_in_control_plane(hosted, own_db_tenant):
    c = hosted[0]
    email = f"wl-{uuid.uuid4().hex[:8]}@example.com"
    r = c.post("/waitlist", json={"email": email})
    assert r.status_code == 201
    emails = [e["email"] for e in asyncio.run(db_module.get_waitlist(c.app.state.db))]
    assert email in emails


def test_pricing_prefills_email_from_auth_db_session(hosted, own_db_tenant):
    c = hosted[0]
    tenant, _conn, _proj = own_db_tenant
    r = c.get("/onboarding", headers=_cookie_header(c, tenant["id"]))
    assert r.status_code == 200
    assert tenant["email"] in r.text


# ---------------------------------------------------------------------------
# 4. Authenticated callers: scoped to their own tenant
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cred", ["cookie", "bearer"])
def test_valid_credential_is_scoped_to_own_tenant_db(hosted, own_db_tenant, cred):
    c = hosted[0]
    tenant, _conn, proj = own_db_tenant
    headers = (_cookie_header if cred == "cookie" else _bearer_header)(c, tenant["id"])
    r = c.get("/projects", headers=headers)
    assert r.status_code == 200, r.text
    names = {p["name"] for p in r.json()}
    assert "tenant-own-proj" in names
    assert "operator-secret-proj" not in names
    r = c.get(f"/projects/{proj['id']}/notes", headers=headers)
    assert r.status_code == 200
    r = c.get(f"/projects/{proj['id']}/settings", headers=headers)
    assert r.status_code == 200
    r = c.patch(f"/projects/{proj['id']}/settings", json={}, headers=headers)
    assert r.status_code == 200, r.text
    r = c.get(f"/projects/{hosted[2]['id']}", headers=headers)
    assert r.status_code == 404  # the operator's project is not in this tenant's DB


def test_unprovisioned_tenant_gets_503_not_401(hosted):
    c = hosted[0]
    tenant = _seed_tenant(c, f"unprov-{uuid.uuid4().hex[:8]}@example.com", plan="pro")
    r = c.get("/projects", headers=_cookie_header(c, tenant["id"]))
    assert r.status_code == 503, r.text


def test_admin_tenant_still_reaches_control_plane(hosted):
    """The owner's dashboard: an admin-plan tenant with no dedicated DB keeps
    resolving to the control-plane DB through its session cookie."""
    c, _server, op = hosted
    admin = _seed_tenant(c, f"admin-{uuid.uuid4().hex[:8]}@example.com", plan="admin")
    try:
        r = c.get("/projects", headers=_cookie_header(c, admin["id"]))
        assert r.status_code == 200
        assert "operator-secret-proj" in {p["name"] for p in r.json()}
    finally:
        _deps._tenant_db_cache.pop(admin["id"], None)


# ---------------------------------------------------------------------------
# 5. Self-hosted and demo: unchanged
# ---------------------------------------------------------------------------

def test_self_hosted_anonymous_access_unchanged(hosted, monkeypatch):
    c, _server, op = hosted
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    r = c.get("/projects")
    assert r.status_code == 200
    assert "operator-secret-proj" in {p["name"] for p in r.json()}
    assert c.get(f"/projects/{op['id']}/notes").status_code == 200
    assert c.get(f"/projects/{op['id']}/settings").status_code == 200
    assert c.patch(f"/projects/{op['id']}/settings", json={}).status_code == 200
    r = c.post("/projects", json={"name": f"selfhost-{uuid.uuid4().hex[:6]}"})
    assert r.status_code == 201, r.text


def test_demo_cookie_without_demo_db_fails_closed(hosted, monkeypatch):
    c = hosted[0]
    monkeypatch.setattr(c.app.state, "demo_db", None, raising=False)
    r = c.get("/projects", headers={"Cookie": "meridian_demo=1"})
    assert r.status_code == 503
    assert "operator-secret" not in r.text


def test_demo_cookie_routes_to_demo_db(hosted, monkeypatch):
    c = hosted[0]
    demo = asyncio.run(db_module.init_db(":memory:"))
    try:
        asyncio.run(db_module.create_project(demo, "demo-proj"))
        monkeypatch.setattr(c.app.state, "demo_db", demo, raising=False)
        r = c.get("/projects", headers={"Cookie": "meridian_demo=1"})
        assert r.status_code == 200
        assert {p["name"] for p in r.json()} == {"demo-proj"}
    finally:
        asyncio.run(demo.close())


# ---------------------------------------------------------------------------
# 6. Routes that never went through _db()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", [
    ("POST", "/admin/shutdown", {}),
    ("POST", "/admin/restart", {"confirm": True}),
    ("GET", "/admin/git-status", None),
    ("GET", "/waitlist", None),
    ("POST", "/tasks/enqueue", {"session_id": "s", "project_id": "p", "prompt": "x"}),
])
def test_server_process_routes_are_operator_only(hosted, own_db_tenant, no_side_effects,
                                                 method, path, body):
    c = hosted[0]
    tenant, _conn, _proj = own_db_tenant
    for headers in (_cookie_header(c, tenant["id"]), _bearer_header(c, tenant["id"]),
                    {"Cookie": "meridian_demo=1"}):
        r = c.request(method, path, json=body, headers=headers)
        assert r.status_code in (401, 403), (path, headers.keys(), r.status_code, r.text[:200])


def test_operator_can_still_read_waitlist(hosted):
    c = hosted[0]
    op_tenant = _seed_tenant(c, "operator@example.com", plan="admin")
    try:
        r = c.get("/waitlist", headers=_cookie_header(c, op_tenant["id"]))
        assert r.status_code == 200
        assert isinstance(r.json(), list)
    finally:
        _deps._tenant_db_cache.pop(op_tenant["id"], None)


def test_plugin_install_refused_in_hosted_even_for_operator(hosted, no_side_effects):
    c = hosted[0]
    op_tenant = _seed_tenant(c, "operator@example.com", plan="admin")
    try:
        r = c.post("/tunnel/plugins/install", json={"command": "npx -y some-package"},
                   headers=_cookie_header(c, op_tenant["id"]))
        assert r.status_code == 403
        assert r.json()["ok"] is False
    finally:
        _deps._tenant_db_cache.pop(op_tenant["id"], None)


def test_server_instruction_files_refused_in_hosted(hosted, own_db_tenant):
    c = hosted[0]
    tenant, _conn, proj = own_db_tenant
    headers = _cookie_header(c, tenant["id"])
    r = c.get(f"/projects/{proj['id']}/files/CLAUDE.md", headers=headers)
    assert r.status_code == 403
    r = c.put(f"/projects/{proj['id']}/files/CLAUDE.md", json={"content": "pwned"},
              headers=headers)
    assert r.status_code == 403


def test_tunnel_diagnostics_require_a_tenant(hosted):
    c = hosted[0]
    for path in (f"/tunnel/diagnostics/{DUMMY_ID}", f"/tunnel/launch-matrix/{DUMMY_ID}"):
        r = c.get(path, headers={"Cookie": "meridian_demo=1"})
        assert r.status_code == 401, path


def test_document_peeks_never_expose_shared_bucket(hosted):
    from meridian import doc_peeks
    c = hosted[0]
    doc_peeks.record_peek(None, "C:/server/local-secret.docx")
    try:
        r = c.get("/document-peeks", headers={"Cookie": "meridian_demo=1"})
        assert r.status_code == 200
        assert r.json() == {"peeks": []}
    finally:
        doc_peeks.clear(None)


def test_hooks_contract_for_anonymous_and_bogus_bearer(hosted):
    c = hosted[0]
    r = c.post("/hooks/session-start", json={"project_id": hosted[2]["id"]})
    assert r.status_code == 200
    assert r.json()["hookSpecificOutput"]["additionalContext"] == ""
    r = c.post("/hooks/stop", json={"project_id": hosted[2]["id"]})
    assert r.json() == {"ok": True, "handoff": None, "reason": "unauthenticated"}
    for path in ("/hooks/session-start", "/hooks/stop"):
        r = c.post(path, json={"project_id": hosted[2]["id"]},
                   headers={"Authorization": "Bearer sk_meridian_bogus"})
        assert r.status_code == 401, path


def test_sse_session_is_bound_to_its_tenant(hosted, own_db_tenant, monkeypatch):
    c, server_module, _op = hosted
    tenant_a, conn_a, _proj = own_db_tenant
    tenant_b = _seed_tenant(c, f"b-{uuid.uuid4().hex[:8]}@example.com", plan="pro")
    conn_b = asyncio.run(db_module.init_db(":memory:"))
    _deps._tenant_db_cache[tenant_b["id"]] = conn_b
    seen = []

    async def _capture(body, db, data_dir, **_kw):
        seen.append(db)
        return {"jsonrpc": "2.0", "id": body.get("id"), "result": {}}

    monkeypatch.setattr(server_module, "_handle_mcp_request", _capture)
    sid = str(uuid.uuid4())
    server_module._SSE_SESSIONS[sid] = {"db": conn_a, "data_dir": ".", "tenant_id": tenant_a["id"]}
    try:
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        # Tenant B presenting tenant A's session id gets its OWN db, never A's.
        r = c.post(f"/mcp/sse?session_id={sid}", json=rpc, headers=_bearer_header(c, tenant_b["id"]))
        assert r.status_code == 200
        assert seen[-1] is conn_b
        # Tenant A reuses its own session.
        r = c.post(f"/mcp/sse?session_id={sid}", json=rpc, headers=_bearer_header(c, tenant_a["id"]))
        assert r.status_code == 200
        assert seen[-1] is conn_a
        # Anonymous: refused outright.
        r = c.post(f"/mcp/sse?session_id={sid}", json=rpc)
        assert r.status_code == 401
        assert len(seen) == 2
    finally:
        server_module._SSE_SESSIONS.pop(sid, None)
        _deps._tenant_db_cache.pop(tenant_b["id"], None)
        asyncio.run(conn_b.close())
