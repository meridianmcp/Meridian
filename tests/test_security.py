"""Security tests: repo hygiene, auth gates, endpoint protection."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".pdf", ".zip", ".sqlite", ".db", ".pyc",
    ".woff", ".woff2", ".ttf", ".eot", ".svg",
}
_SKIP_FILES = {"test_security.py", "scan_sensitive.py"}


def _tracked_text_files():
    result = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=ROOT,
    )
    for line in result.stdout.splitlines():
        p = ROOT / line.strip()
        if p.suffix.lower() in _SKIP_EXTENSIONS:
            continue
        if p.name in _SKIP_FILES:
            continue
        if p.is_file():
            yield p


# ---------------------------------------------------------------------------
# (a) No .env file committed
# ---------------------------------------------------------------------------

def test_no_env_file_committed():
    """No .env, secrets.env, or .env.local file may be tracked by git."""
    result = subprocess.run(
        ["git", "ls-files", "--", ".env", ".env.local", "secrets.env"],
        capture_output=True, text=True, cwd=ROOT,
    )
    tracked = [f.strip() for f in result.stdout.splitlines() if f.strip()]
    assert tracked == [], f"Sensitive env files committed to git: {tracked}"


# ---------------------------------------------------------------------------
# (b) No real API key patterns in committed files
# ---------------------------------------------------------------------------

_KEY_PATTERNS = [
    (
        "stripe_live_key",
        re.compile(r"\bsk_live_[A-Za-z0-9]{12,}\b"),
        "Stripe live secret key",
    ),
    (
        "github_pat",
        re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
        "GitHub personal access token",
    ),
    (
        "neon_owner_password",
        # Real Neon passwords follow neondb_owner:<password>@<host>
        # Must have actual host after the password (not just a placeholder)
        re.compile(r"neondb_owner:[A-Za-z0-9_-]{8,}@ep-[a-zA-Z0-9-]+\.[a-zA-Z0-9.-]+"),
        "Neon owner password in connection string",
    ),
    (
        "google_oauth_secret",
        re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{24,}\b"),
        "Google OAuth client secret",
    ),
]


@pytest.mark.parametrize(
    "name,pattern,description",
    _KEY_PATTERNS,
    ids=[k[0] for k in _KEY_PATTERNS],
)
def test_no_api_key_pattern(name, pattern, description):
    """No real credentials matching known secret formats in committed files."""
    hits = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in pattern.finditer(text):
            rel = path.relative_to(ROOT).as_posix()
            # Skip example files and docs — they use safe placeholder forms
            if rel.endswith((".example", ".md")) or "/docs/" in rel:
                continue
            hits.append(f"{rel}: {m.group(0)[:30]}...")
    assert hits == [], f"Found {description} in committed source files:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# (c) /health returns 200 without auth
# ---------------------------------------------------------------------------

def test_health_no_auth(client):
    """/health must respond 200 with no authentication."""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("status") == "ok"


# ---------------------------------------------------------------------------
# (d) /dashboard redirects to /auth/login when unauthenticated in hosted mode
# ---------------------------------------------------------------------------

def test_dashboard_redirects_to_login_hosted(monkeypatch, tmp_path):
    """Unauthenticated GET /dashboard in hosted mode must redirect to /auth/login."""
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))

    import importlib
    import meridian.server as server_module
    server_module = importlib.reload(server_module)

    from fastapi.testclient import TestClient
    with TestClient(server_module.app, follow_redirects=False) as c:
        resp = c.get("/dashboard")

    assert resp.status_code == 302, f"Expected 302, got {resp.status_code}"
    location = resp.headers.get("location", "")
    assert "/auth/login" in location, f"Expected redirect to /auth/login, got: {location}"


# ---------------------------------------------------------------------------
# (e) /mcp POST rejects requests without Bearer token
# ---------------------------------------------------------------------------

def test_mcp_rejects_no_bearer(client):
    """/mcp POST without Authorization header must return 401."""
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 393eed0a — server-side workspace-role enforcement
# ---------------------------------------------------------------------------

def test_required_perm_for_request_mapping():
    """The (method, path) → permission map gates the right things and skips
    reads, self-scoped writes, and public endpoints."""
    from meridian._deps import _required_perm_for_request
    from meridian.roles import PERM_WRITE, PERM_INVITE, PERM_SETTINGS

    # Reads are never gated.
    assert _required_perm_for_request("GET", "/projects/p1/tasks") is None
    # Self-scoped / pre-auth / per-tool-gated paths are skipped.
    for path in ("/billing/portal", "/account/delete", "/auth/tokens",
                 "/oauth/token", "/mcp", "/webhooks/stripe", "/__gate__"):
        assert _required_perm_for_request("POST", path) is None, path
    # Team management.
    assert _required_perm_for_request("POST", "/workspace/invite") == PERM_INVITE
    assert _required_perm_for_request("DELETE", "/workspace/members/m1") == PERM_INVITE
    # Workspace / project configuration.
    assert _required_perm_for_request("PATCH", "/workspace/settings") == PERM_SETTINGS
    assert _required_perm_for_request("PATCH", "/projects/p1/settings") == PERM_SETTINGS
    assert _required_perm_for_request("PATCH", "/projects/p1/ntfy") == PERM_SETTINGS
    assert _required_perm_for_request("POST", "/projects/p1/github/connect") == PERM_SETTINGS
    # Whole-project deletion is elevated; a sub-resource delete is a normal write.
    assert _required_perm_for_request("DELETE", "/projects/p1") == PERM_SETTINGS
    assert _required_perm_for_request("DELETE", "/projects/p1/sprint/s1") == PERM_WRITE
    # Ordinary project-data writes.
    assert _required_perm_for_request("POST", "/tasks") == PERM_WRITE
    assert _required_perm_for_request("POST", "/projects/p1/decisions") == PERM_WRITE


def _boot_hosted_role_client(monkeypatch, tmp_path):
    """Boot a hosted TestClient (fresh server import) for role-enforcement tests."""
    import importlib
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    # Blank any real Neon admin URL from the dev env so the admin-plan owner's
    # workspace resolves to the in-memory auth DB instead of dialing Neon.
    monkeypatch.setenv("MERIDIAN_AUTH_DB", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    import meridian.server as server_module
    server_module = importlib.reload(server_module)
    from fastapi.testclient import TestClient
    return TestClient(server_module.app)


def test_role_enforcement_cross_workspace_http_and_mcp(monkeypatch, tmp_path):
    """A viewer invited to another workspace is read-only there; a member can
    write project data. Enforced on both the HTTP and MCP write paths, and only
    when the request targets the invited workspace (X-Workspace-Tenant-Id)."""
    import asyncio
    from datetime import datetime, timezone
    from meridian import db as db_module

    with _boot_hosted_role_client(monkeypatch, tmp_path) as c:
        db = c.app.state.db

        async def _setup():
            owner = await db_module.upsert_tenant(db, "rl-owner@example.com")
            # admin plan → the cross-workspace switch resolves to the auth DB in
            # tests (no per-tenant Neon DB is provisioned).
            await db.execute("UPDATE tenants SET plan='admin' WHERE id=?", (owner["id"],))
            invitee = await db_module.upsert_tenant(db, "rl-invitee@example.com")
            raw, _ = await db_module.create_api_token(db, invitee["id"])
            proj = await db_module.create_project(db, "rl-owner-proj")
            sess = await db_module.register_session(db, proj["id"], "rl-sess")
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            await db.execute(
                "INSERT INTO workspace_members "
                "(id, tenant_id, email, role, github_access, joined_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("rl-wm", owner["id"], "rl-invitee@example.com", "viewer", "none", now),
            )
            await db.commit()
            return owner["id"], raw, proj["id"], sess["id"]

        owner_id, token, pid, sid = asyncio.run(_setup())
        hdr = {"Authorization": f"Bearer {token}", "X-Workspace-Tenant-Id": owner_id}

        # viewer: reads are allowed in the invited workspace.
        r = c.get(f"/projects/{pid}/tasks", headers=hdr)
        assert r.status_code == 200, r.text

        # viewer: HTTP write is blocked by the role middleware (before the route).
        r = c.post("/tasks", headers=hdr,
                   json={"session_id": sid, "project_id": pid, "description": "nope"})
        assert r.status_code == 403, r.text
        assert "cannot perform" in r.text

        # viewer: MCP write tool is denied by the per-tool gate; a read tool is not.
        def _mcp(name, args):
            return c.post("/mcp", headers=hdr, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": args}})
        mr = _mcp("pin_decision", {"project_id": pid, "title": "t", "body": "b", "category": "X"})
        assert "denied" in mr.text and "read-only" in mr.text, mr.text
        mr = _mcp("get_tasks", {"project_id": pid})
        assert "denied" not in mr.text, mr.text

        # Promote viewer → member: the same HTTP write now passes and succeeds.
        async def _promote():
            await db.execute("UPDATE workspace_members SET role='member' WHERE id='rl-wm'")
            await db.commit()
        asyncio.run(_promote())

        r = c.post("/tasks", headers=hdr,
                   json={"session_id": sid, "project_id": pid, "description": "ok"})
        assert r.status_code == 201, r.text

        # Member still cannot change workspace settings (PERM_SETTINGS).
        r = c.patch("/projects/%s/settings" % pid, headers=hdr,
                    json={"require_merge_approval": True})
        assert r.status_code == 403, r.text
        assert "cannot perform" in r.text


def test_project_scope_enforcement_http_and_mcp(monkeypatch, tmp_path):
    """95499c3e / decision 6fe5210c — Option A airtight per-request enforcement.

    A project-scoped member is 403'd on any project outside their scope — even by
    direct ID (403, not 404, so existence isn't leaked) — on both the HTTP routes
    and the MCP dispatch. Workspace-wide members are unaffected.
    """
    import asyncio
    from datetime import datetime, timezone
    from meridian import db as db_module

    with _boot_hosted_role_client(monkeypatch, tmp_path) as c:
        db = c.app.state.db

        async def _setup():
            owner = await db_module.upsert_tenant(db, "ps-owner@example.com")
            # admin plan → cross-workspace switch resolves to the auth DB in tests.
            await db.execute("UPDATE tenants SET plan='admin' WHERE id=?", (owner["id"],))
            scoped = await db_module.upsert_tenant(db, "ps-scoped@example.com")
            wide = await db_module.upsert_tenant(db, "ps-wide@example.com")
            raw_scoped, _ = await db_module.create_api_token(db, scoped["id"])
            raw_wide, _ = await db_module.create_api_token(db, wide["id"])
            proj_a = await db_module.create_project(db, "ps-proj-a")
            proj_b = await db_module.create_project(db, "ps-proj-b")
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            # Scoped member: invited to owner's workspace, scoped to proj_a only.
            await db.execute(
                "INSERT INTO workspace_members "
                "(id, tenant_id, email, role, github_access, joined_at, project_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("ps-wm-scoped", owner["id"], "ps-scoped@example.com", "member", "none", now, proj_a["id"]),
            )
            # Workspace-wide member: project_id NULL → sees everything.
            await db.execute(
                "INSERT INTO workspace_members "
                "(id, tenant_id, email, role, github_access, joined_at, project_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("ps-wm-wide", owner["id"], "ps-wide@example.com", "member", "none", now, None),
            )
            await db.commit()
            return owner["id"], raw_scoped, raw_wide, proj_a["id"], proj_b["id"]

        owner_id, tok_scoped, tok_wide, pid_a, pid_b = asyncio.run(_setup())
        h_scoped = {"Authorization": f"Bearer {tok_scoped}", "X-Workspace-Tenant-Id": owner_id}
        h_wide = {"Authorization": f"Bearer {tok_wide}", "X-Workspace-Tenant-Id": owner_id}

        # HTTP: scoped member CAN reach their own project by direct ID.
        assert c.get(f"/projects/{pid_a}/tasks", headers=h_scoped).status_code == 200
        # HTTP: scoped member CANNOT reach a sibling project — 403, NOT 404.
        r = c.get(f"/projects/{pid_b}/tasks", headers=h_scoped)
        assert r.status_code == 403, r.text
        assert "scope" in r.text.lower()
        # HTTP: workspace-wide member reaches both projects.
        assert c.get(f"/projects/{pid_a}/tasks", headers=h_wide).status_code == 200
        assert c.get(f"/projects/{pid_b}/tasks", headers=h_wide).status_code == 200

        # MCP dispatch enforcement matches the HTTP enforcement.
        def _mcp(tok, args):
            return c.post(
                "/mcp",
                headers={"Authorization": f"Bearer {tok}", "X-Workspace-Tenant-Id": owner_id},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "get_tasks", "arguments": args}},
            )
        assert "outside your access scope" not in _mcp(tok_scoped, {"project_id": pid_a}).text
        assert "outside your access scope" in _mcp(tok_scoped, {"project_id": pid_b}).text
        # Workspace-wide member: MCP allowed on the sibling project too.
        assert "outside your access scope" not in _mcp(tok_wide, {"project_id": pid_b}).text


# ---------------------------------------------------------------------------
# fdf1120f — magic-link verify renders HTML on failure, never a blank JSON page
# ---------------------------------------------------------------------------

def test_magic_verify_invalid_token_returns_html(client):
    """An expired/invalid magic link (opened in a browser) must render a real
    HTML page with a way back — not a blank JSON 401."""
    r = client.get("/auth/magic/verify?token=bogus-nonexistent", follow_redirects=False)
    assert r.status_code == 401
    assert "text/html" in r.headers.get("content-type", "")
    assert "Request a new link" in r.text
    assert "expired" in r.text.lower()


def test_magic_verify_missing_token_returns_html(client):
    """No token → HTML 400, not a blank JSON error."""
    r = client.get("/auth/magic/verify", follow_redirects=False)
    assert r.status_code == 400
    assert "text/html" in r.headers.get("content-type", "")
    assert "Request a new link" in r.text
