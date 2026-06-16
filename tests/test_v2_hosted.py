"""v2.0 hosted-tier tests: tenant tables, token, webhook sig, landing, MCP auth, docker-compose."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import aiosqlite
import pytest


def _run(coro):
    """Run a coroutine in a fresh event loop (pytest-safe)."""
    return asyncio.run(coro)


def _make_test_db():
    """Coroutine that returns an in-memory DB with full schema applied."""
    from meridian.db import CREATE_TABLES

    async def _inner():
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await db.executescript(CREATE_TABLES)
        await db.commit()
        return db

    return _inner


def _make_hosted_client(monkeypatch, tmp_path):
    """Hosted-mode TestClient backed by an in-memory auth DB."""
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))

    import importlib
    from fastapi.testclient import TestClient
    import meridian.server as server_module

    server_module = importlib.reload(server_module)
    return TestClient(server_module.app)


# ---------------------------------------------------------------------------
# Tenant tables
# ---------------------------------------------------------------------------

def test_tenant_tables_exist_in_schema():
    """CREATE_TABLES must define tenants, user_sessions, token tables."""
    from meridian.db import CREATE_TABLES

    for table in ("tenants", "user_sessions", "api_tokens", "oauth_tokens"):
        assert table in CREATE_TABLES, f"CREATE_TABLES missing {table!r}"


def test_upsert_tenant_creates_row():
    """upsert_tenant creates a tenant row and assigns an id."""
    from meridian import db as db_module

    async def _run_inner():
        db = await _make_test_db()()
        t = await db_module.upsert_tenant(db, "alice@example.com")
        await db.close()
        return t

    t = _run(_run_inner())
    assert t["email"] == "alice@example.com"
    assert t["id"] is not None
    assert t["plan"] == "free"
    # c3e91df4 — trial clock starts at first project creation, not signup
    assert t["trial_started_at"] is None
    assert t["inactivity_expires_at"] is None


def test_upsert_tenant_idempotent():
    """Calling upsert_tenant twice with same email returns same id."""
    from meridian import db as db_module

    async def _run_inner():
        db = await _make_test_db()()
        t1 = await db_module.upsert_tenant(db, "alice@example.com")
        t2 = await db_module.upsert_tenant(db, "alice@example.com")
        await db.close()
        return t1, t2

    t1, t2 = _run(_run_inner())
    assert t1["id"] == t2["id"]


def test_upsert_tenant_updates_google_sub():
    """upsert_tenant patches google_sub when provided on second call."""
    from meridian import db as db_module

    async def _run_inner():
        db = await _make_test_db()()
        await db_module.upsert_tenant(db, "bob@example.com")
        t = await db_module.upsert_tenant(db, "bob@example.com", google_sub="sub_xyz")
        await db.close()
        return t

    t = _run(_run_inner())
    assert t["google_sub"] == "sub_xyz"


# ---------------------------------------------------------------------------
# API token round-trip
# ---------------------------------------------------------------------------

def test_api_token_create_and_lookup():
    """create_api_token stores hash; get_tenant_from_token_hash finds tenant."""
    from meridian import db as db_module

    async def _run_inner():
        db = await _make_test_db()()
        tenant = await db_module.upsert_tenant(db, "carol@example.com")
        raw_token, _row = await db_module.create_api_token(db, tenant["id"], label="test")
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        found = await db_module.get_tenant_from_token_hash(db, token_hash)
        bad = await db_module.get_tenant_from_token_hash(db, "badhash000")
        await db.close()
        return raw_token, found, bad

    raw_token, found, bad = _run(_run_inner())
    assert raw_token.startswith("sk_meridian_")
    assert found is not None
    assert found["email"] == "carol@example.com"
    assert bad is None


def test_bearer_auth_endpoint_returns_401_without_token(client):
    """POST /auth/tokens returns 401 when no auth provided."""
    r = client.post("/auth/tokens", json={})
    assert r.status_code == 401


def test_bearer_auth_me_returns_401_without_token(client):
    """GET /auth/me returns 401 when unauthenticated."""
    r = client.get("/auth/me")
    assert r.status_code == 401


def test_bearer_auth_tokens_list_and_revoke_with_valid_token(monkeypatch, tmp_path):
    """GET/DELETE /auth/tokens lists masked keys and revokes only the target key."""
    from meridian import db as db_module

    async def _setup(db):
        tenant = await db_module.upsert_tenant(db, "tokens-list@example.com")
        raw_a, row_a = await db_module.create_api_token(db, tenant["id"], label="first")
        raw_b, row_b = await db_module.create_api_token(db, tenant["id"], label="second")
        return raw_a, raw_b, row_a, row_b

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _raw_a, raw_token, row_a, row_b = _run(_setup(client.app.state.db))

        listed = client.get(
            "/auth/tokens",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert listed.status_code == 200
        body = listed.json()
        assert {item["id"] for item in body} == {row_a["id"], row_b["id"]}
        assert {item["label"] for item in body} == {"first", "second"}
        assert all(item["masked_token"].startswith("sk_meridian_") for item in body)
        assert all("token_hash" not in item for item in body)

        deleted = client.delete(
            f"/auth/tokens/{row_a['id']}",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert deleted.status_code == 204

        listed_again = client.get(
            "/auth/tokens",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert listed_again.status_code == 200
        remaining_ids = [item["id"] for item in listed_again.json()]
        assert remaining_ids == [row_b["id"]]

        missing = client.delete(
            f"/auth/tokens/{row_a['id']}",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert missing.status_code == 404


def test_me_returns_tenant_id_for_authenticated_user(monkeypatch, tmp_path):
    """GET /me exposes tenant_id so `meridian --tunnel` can build its URLs."""
    from meridian import db as db_module

    async def _setup(db):
        tenant = await db_module.upsert_tenant(db, "tunnel-me@example.com")
        raw, _row = await db_module.create_api_token(db, tenant["id"], label="t")
        return tenant["id"], raw

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        tenant_id, raw_token = _run(_setup(client.app.state.db))
        r = client.get("/me", headers={"Authorization": f"Bearer {raw_token}"})
        assert r.status_code == 200
        assert r.json()["tenant_id"] == tenant_id


def test_delete_orphaned_oauth_keys_endpoint(monkeypatch, tmp_path):
    """DELETE /api/keys/orphaned purges only 'oauth' tokens older than 24h."""
    from meridian import db as db_module

    from datetime import datetime, timezone, timedelta

    async def _setup(db):
        tenant = await db_module.upsert_tenant(db, "orphan@example.com")
        tid = tenant["id"]
        # Auth token for the request (recent, label 'session' — must survive).
        raw_session, _ = await db_module.create_api_token(db, tid, label="session")
        old_ts = (
            datetime.now(timezone.utc) - timedelta(hours=48)
        ).strftime("%Y-%m-%d %H:%M:%S")
        # Stale orphaned oauth token (48h old) — must be purged.
        await db.execute(
            "INSERT INTO api_tokens (id, tenant_id, token_hash, label, token_type, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("orph-old", tid, "h-old", "oauth", "readwrite", old_ts),
        )
        # Fresh oauth token (just minted) — must survive (may be in use).
        await db.execute(
            "INSERT INTO api_tokens (id, tenant_id, token_hash, label, token_type)"
            " VALUES (?, ?, ?, ?, ?)",
            ("orph-new", tid, "h-new", "oauth", "readwrite"),
        )
        # Old token with a different label — must survive (not an oauth orphan).
        await db.execute(
            "INSERT INTO api_tokens (id, tenant_id, token_hash, label, token_type, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("keep-old", tid, "h-keep", "install", "readwrite", old_ts),
        )
        await db.commit()
        return raw_session

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        raw_session = _run(_setup(client.app.state.db))

        r = client.delete(
            "/api/keys/orphaned",
            headers={"Authorization": f"Bearer {raw_session}"},
        )
        assert r.status_code == 200
        assert r.json()["deleted"] == 1

        async def _remaining(db):
            async with db.execute("SELECT id FROM api_tokens ORDER BY id") as cur:
                return [row[0] for row in await cur.fetchall()]

        ids = _run(_remaining(client.app.state.db))
        assert "orph-old" not in ids
        assert "orph-new" in ids   # fresh oauth token kept
        assert "keep-old" in ids   # non-oauth label kept


def test_delete_orphaned_oauth_keys_requires_auth(client):
    """DELETE /api/keys/orphaned without a token returns 401."""
    r = client.delete("/api/keys/orphaned")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Stripe webhook signature verification
# ---------------------------------------------------------------------------

def _make_stripe_sig(payload: bytes, secret: str, ts: int | None = None) -> str:
    ts_val = ts if ts is not None else int(time.time())
    msg = f"{ts_val}.{payload.decode()}"
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"t={ts_val},v1={sig}"


def test_stripe_sig_rejects_bad_sig(client):
    """POST /webhooks/stripe returns 400 for wrong signature."""
    os.environ["STRIPE_WEBHOOK_SECRET"] = "testsecret123"
    try:
        r = client.post(
            "/webhooks/stripe",
            content=b'{"type":"checkout.session.completed","data":{"object":{}}}',
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "t=12345,v1=badsig",
            },
        )
        assert r.status_code == 400
    finally:
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)


def test_stripe_sig_accepts_valid_sig(client):
    """POST /webhooks/stripe accepts a correctly signed request (sig != 400)."""
    secret = "wh_test_valid"
    os.environ["STRIPE_WEBHOOK_SECRET"] = secret
    try:
        payload = json.dumps({
            "type": "checkout.session.completed",
            "data": {"object": {
                "customer_email": "dave@example.com",
                "customer": "cus_456",
            }},
        }).encode()
        sig_header = _make_stripe_sig(payload, secret)
        r = client.post(
            "/webhooks/stripe",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": sig_header,
            },
        )
        # 400 means signature check failed (wrong). Anything else means sig was ok.
        assert r.status_code != 400, f"signature check rejected valid sig: {r.text}"
    finally:
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)


def test_stripe_webhook_ignores_unknown_events(client):
    """POST /webhooks/stripe returns ignored for unhandled event types."""
    os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
    r = client.post(
        "/webhooks/stripe",
        json={"type": "customer.created", "data": {"object": {}}},
    )
    assert r.status_code == 200
    assert r.json().get("status") == "ignored"


def test_stripe_webhook_stale_timestamp_rejected(client):
    """POST /webhooks/stripe rejects a request with a timestamp > 5 min old."""
    secret = "staletstest"
    os.environ["STRIPE_WEBHOOK_SECRET"] = secret
    try:
        payload = b'{"type":"checkout.session.completed","data":{"object":{}}}'
        old_ts = int(time.time()) - 400  # 6+ minutes ago
        sig_header = _make_stripe_sig(payload, secret, ts=old_ts)
        r = client.post(
            "/webhooks/stripe",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": sig_header,
            },
        )
        assert r.status_code == 400
    finally:
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)


def _make_github_sig(payload: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def test_github_marketplace_webhook_signature(client):
    """POST /webhooks/github-marketplace: 200 on valid X-Hub-Signature-256, 401 on bad."""
    secret = "gh_mp_secret"
    os.environ["GITHUB_MARKETPLACE_WEBHOOK_SECRET"] = secret
    try:
        payload = json.dumps({
            "action": "purchased",
            "marketplace_purchase": {"account": {"login": "octocat"}},
        }).encode()
        good = client.post(
            "/webhooks/github-marketplace",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _make_github_sig(payload, secret),
                "X-GitHub-Delivery": "delivery-1",
            },
        )
        assert good.status_code == 200
        assert good.json()["action"] == "purchased"

        bad = client.post(
            "/webhooks/github-marketplace",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=" + "0" * 64,
            },
        )
        assert bad.status_code == 401
    finally:
        os.environ.pop("GITHUB_MARKETPLACE_WEBHOOK_SECRET", None)


# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------

def test_landing_page_returns_200(client):
    """GET / returns 200 HTML."""
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


def test_landing_page_has_headline_and_ctas(client):
    """Landing page contains Meridian branding, CTA links, and waitlist form."""
    r = client.get("/")
    html = r.text
    assert "Meridian" in html
    assert "/auth/login" in html
    assert "github.com" in html
    assert "waitlist" in html.lower()


def test_landing_page_has_pricing_section(client):
    """Landing page has a pricing section with Standard and Pro plans (v2.2+)."""
    r = client.get("/")
    html = r.text
    # Pricing cards for both tiers
    assert "Standard" in html
    assert "$20" in html
    assert "Pro" in html
    assert "$49" in html
    # Pro plan should show waitlist CTA
    assert "Join waitlist" in html or "waitlist" in html.lower()


def test_waitlist_join_from_landing(client):
    """POST /waitlist accepts a new email."""
    r = client.post("/waitlist", json={"email": "landing_test_v2@example.com"})
    assert r.status_code in (201, 409)  # 201 new, 409 duplicate


# ---------------------------------------------------------------------------
# Remote MCP endpoint auth
# ---------------------------------------------------------------------------

def test_remote_mcp_requires_bearer_auth(client):
    """POST /mcp without bearer token returns 401."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401


def test_remote_mcp_invalid_token_returns_401(client):
    """POST /mcp with wrong bearer token returns 401."""
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Authorization": "Bearer sk_meridian_totallyinvalid"},
    )
    assert r.status_code == 401


def test_hooks_invalid_bearer_returns_401_in_hosted_mode(monkeypatch, tmp_path):
    """Hook endpoints must reject invalid Bearer tokens instead of falling back."""
    from meridian import db as db_module

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _run(db_module.create_project(client.app.state.db, "hosted-hooks-invalid"))
        r = client.post(
            "/hooks/session-start",
            json={"project_id": "does-not-matter"},
            headers={"Authorization": "Bearer sk_meridian_totallyinvalid"},
        )
        assert r.status_code == 401
        assert "invalid API token" in r.text

        r = client.post(
            "/hooks/stop",
            json={"project_id": "does-not-matter"},
            headers={"Authorization": "Bearer sk_meridian_totallyinvalid"},
        )
        assert r.status_code == 401
        assert "invalid API token" in r.text


def test_hooks_accept_valid_bearer_for_admin_tenant(monkeypatch, tmp_path):
    """Valid Bearer auth resolves the tenant DB through the REAL routing path.

    Exercises the production ``_open_tenant_db_by_id`` resolver rather than
    monkeypatching it away. An admin-plan tenant with no dedicated neon_db_url
    legitimately falls back to the auth DB (see _deps.py), so the project we
    create there is visible to the hook. A non-admin tenant on this same path
    would 503 — that isolation guarantee is covered by
    test_non_admin_tenant_with_null_db_gets_503 in test_hosted.py.
    """
    from meridian import db as db_module
    import meridian._deps as deps_module

    async def _setup(db):
        tenant = await db_module.upsert_tenant(db, "hooks-hosted@example.com")
        # admin plan → _open_tenant_db_by_id falls back to the auth DB when no
        # dedicated neon_db_url is provisioned (the real resolver path).
        await db_module.update_tenant(db, tenant["id"], plan="admin")
        raw_token, _ = await db_module.create_api_token(db, tenant["id"], label="hooks-test")
        project = await db_module.create_project(db, "hosted-hooks-valid")
        return raw_token, project

    # With no MERIDIAN_AUTH_DB secret set, the admin-plan tenant resolves to the
    # shared auth DB (the documented fallback) instead of dialing a Neon URL.
    # Empty (not unset) so the server reload's load_dotenv doesn't repopulate it.
    monkeypatch.setenv("MERIDIAN_AUTH_DB", "")

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        # Clear the module-level tenant DB cache so this test resolves fresh.
        deps_module._tenant_db_cache.clear()
        raw_token, project = _run(_setup(client.app.state.db))
        start_resp = client.post(
            "/hooks/session-start",
            json={"project_id": project["id"], "session_name": "hook-test"},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert start_resp.status_code == 200
        body = start_resp.json()
        assert "hookSpecificOutput" in body
        assert project["name"] in body["hookSpecificOutput"]["additionalContext"]

        stop_resp = client.post(
            "/hooks/stop",
            json={"project_id": project["id"]},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert stop_resp.status_code == 200
        assert stop_resp.json()["ok"] is True


def test_remote_mcp_initialize_with_valid_token(client):
    """POST /mcp initialize returns protocolVersion with valid bearer token."""
    from meridian import db as db_module

    async def _setup():
        db = client.app.state.db
        tenant = await db_module.upsert_tenant(db, "mcp_init@example.com")
        raw, _ = await db_module.create_api_token(db, tenant["id"], label="mcp-init")
        return raw

    raw_token = _run(_setup())

    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {},
            },
        },
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["protocolVersion"] == "2025-03-26"


def test_remote_mcp_initialize_with_valid_oauth_token_from_db(client):
    """POST /mcp accepts OAuth bearer tokens reloaded from the auth DB."""
    from meridian.routes import oauth as oauth_module

    async def _setup():
        db = client.app.state.db
        raw_token = "oauth-db-token"
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        await db.execute(
            "INSERT INTO oauth_tokens (token_hash, tenant_id, client_id, exp) VALUES (?, ?, ?, ?)",
            (token_hash, None, "claude-ai", int(time.time()) + 3600),
        )
        await db.commit()
        oauth_module._oa_tokens.clear()
        return raw_token, token_hash

    raw_token, token_hash = _run(_setup())

    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {},
            },
        },
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["protocolVersion"] == "2025-03-26"
    assert token_hash in oauth_module._oa_tokens


def test_remote_mcp_tools_list_returns_full_tool_surface(client):
    """POST /mcp tools/list returns the full base MCP tool surface.

    A tenant with no GitHub connection gets exactly _MCP_TOOLS_LIST (no GitHub
    tools appended), so the count is pinned to the source list rather than a
    stale magic number — adding a tool keeps this assertion correct.
    """
    from meridian import db as db_module
    from meridian.mcp_tools import _MCP_TOOLS_LIST

    async def _setup():
        db = client.app.state.db
        tenant = await db_module.upsert_tenant(db, "mcp_tools2@example.com")
        raw, _ = await db_module.create_api_token(db, tenant["id"])
        return raw

    raw_token = _run(_setup())

    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert r.status_code == 200
    tools = r.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    # No GitHub connected for this tenant → exactly the base tool list.
    assert len(tools) == len(_MCP_TOOLS_LIST)
    for expected in ("create_project", "register_session", "log_task", "generate_handoff", "list_projects", "get_project_by_name"):
        assert expected in names


def test_remote_mcp_tools_call_create_project(client):
    """POST /mcp tools/call create_project creates a project."""
    from meridian import db as db_module

    async def _setup():
        db = client.app.state.db
        tenant = await db_module.upsert_tenant(db, "mcp_call2@example.com")
        raw, _ = await db_module.create_api_token(db, tenant["id"])
        return raw

    raw_token = _run(_setup())

    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {
                "name": "create_project",
                "arguments": {"name": "mcp-remote-proj-v2"},
            },
        },
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert r.status_code == 200
    result = r.json()["result"]
    content = json.loads(result["content"][0]["text"])
    assert content["name"] == "mcp-remote-proj-v2"


def test_remote_mcp_project_discovery_tools_return_sprint(client):
    """list_projects and get_project_by_name expose project discovery fields."""
    from meridian import db as db_module

    async def _setup():
        db = client.app.state.db
        tenant = await db_module.upsert_tenant(db, "mcp_discovery@example.com")
        raw, _ = await db_module.create_api_token(db, tenant["id"], label="mcp-discovery")
        project = await db_module.create_project(db, "meridian-build")
        await db_module.set_goal(db, project["id"], "browser hardening", sprint="v1.0.4")
        return raw, project

    raw_token, project = _run(_setup())

    list_resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 10, "method": "tools/call",
            "params": {"name": "list_projects", "arguments": {}},
        },
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert list_resp.status_code == 200
    list_payload = json.loads(list_resp.json()["result"]["content"][0]["text"])
    assert any(
        item["id"] == project["id"]
        and item["name"] == "meridian-build"
        and item["sprint"] == "v1.0.4"
        for item in list_payload
    )

    by_name_resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 11, "method": "tools/call",
            "params": {"name": "get_project_by_name", "arguments": {"name": "meridian-build"}},
        },
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert by_name_resp.status_code == 200
    by_name_payload = json.loads(by_name_resp.json()["result"]["content"][0]["text"])
    assert by_name_payload == {
        "id": project["id"],
        "name": "meridian-build",
        "sprint": "v1.0.4",
    }


# ---------------------------------------------------------------------------
# docker-compose.yml
# ---------------------------------------------------------------------------

def test_docker_compose_exists():
    """docker-compose.yml exists at repo root."""
    p = Path(__file__).parent.parent / "docker-compose.yml"
    assert p.exists(), "docker-compose.yml not found at repo root"


def test_docker_compose_port_mapping():
    """docker-compose.yml maps port 7878 on host to 8000 in container."""
    content = (Path(__file__).parent.parent / "docker-compose.yml").read_text()
    assert "7878" in content
    assert "8000" in content


def test_docker_compose_volume_mount():
    """docker-compose.yml mounts ./data:/app/data."""
    content = (Path(__file__).parent.parent / "docker-compose.yml").read_text()
    assert "./data:/app/data" in content


def test_pyproject_toml_has_hosted_deps():
    """pyproject.toml lists authlib, itsdangerous, resend, bcrypt, slowapi."""
    content = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    for dep in ("authlib", "itsdangerous", "resend", "bcrypt", "slowapi"):
        assert dep in content, f"pyproject.toml missing {dep}"


# ---------------------------------------------------------------------------
# Free-tier project limit (the 2nd-project-403 guarantee from QA item 95ed26d2)
# ---------------------------------------------------------------------------

def test_free_tier_second_project_returns_403(monkeypatch, tmp_path):
    """A free-plan tenant may create one project; the second returns 403.

    Exercises the real POST /projects route end-to-end for an authenticated
    free tenant. The tenant's project DB is the in-memory auth DB, injected
    via the production _tenant_db_cache seam so no Neon project is needed.
    """
    from meridian import db as db_module
    from meridian import _deps
    from meridian import hosted as hosted_module

    client = _make_hosted_client(monkeypatch, tmp_path)

    with client:
        db = client.app.state.db

        async def _setup():
            tenant = await db_module.upsert_tenant(db, "qa-free@meridian-test.invalid")
            session = await db_module.create_user_session(
                db, tenant["id"], "2099-01-01T00:00:00+00:00"
            )
            return tenant, session

        tenant, session = _run(_setup())
        assert tenant["plan"] == "free"

        # Route the tenant's project DB to the in-memory auth DB.
        _deps._tenant_db_cache[tenant["id"]] = db
        try:
            client.cookies.set(
                hosted_module._SESSION_COOKIE,
                hosted_module._make_session_cookie(session["id"]),
            )

            r1 = client.post("/projects", json={"name": "first-project"})
            assert r1.status_code == 201, r1.text

            r2 = client.post("/projects", json={"name": "second-project"})
            assert r2.status_code == 403, r2.text
            assert "Free tier" in r2.json()["detail"]
        finally:
            _deps._tenant_db_cache.pop(tenant["id"], None)


def test_trial_starts_on_first_project_not_at_signup(monkeypatch, tmp_path):
    """c3e91df4 — trial_started_at must be NULL after signup and set only when
    the tenant creates their first own project. Invited members who never create
    a project must never consume a trial slot."""
    from meridian import db as db_module
    from meridian import _deps
    from meridian import hosted as hosted_module

    client = _make_hosted_client(monkeypatch, tmp_path)

    with client:
        db = client.app.state.db

        async def _setup():
            return await db_module.upsert_tenant(db, "trial-test@meridian-test.invalid")

        tenant = _run(_setup())
        # c3e91df4: trial clock must NOT start at signup
        assert tenant["trial_started_at"] is None
        assert tenant["inactivity_expires_at"] is None

        _deps._tenant_db_cache[tenant["id"]] = db
        try:
            session = _run(
                db_module.create_user_session(db, tenant["id"], "2099-01-01T00:00:00+00:00")
            )
            client.cookies.set(
                hosted_module._SESSION_COOKIE,
                hosted_module._make_session_cookie(session["id"]),
            )

            r = client.post("/projects", json={"name": "first-project"})
            assert r.status_code == 201, r.text

            # Trial must now be started on the tenant row
            refreshed = _run(db_module.get_tenant_by_id(db, tenant["id"]))
            assert refreshed["trial_started_at"] is not None
            assert refreshed["inactivity_expires_at"] is not None
        finally:
            _deps._tenant_db_cache.pop(tenant["id"], None)


# ---------------------------------------------------------------------------
# Feedback (sidebar "Send feedback" form)
# ---------------------------------------------------------------------------

def test_feedback_requires_auth(monkeypatch, tmp_path):
    """POST /feedback without a session returns 401."""
    client = _make_hosted_client(monkeypatch, tmp_path)
    with client:
        r = client.post("/feedback", json={"type": "bug", "message": "hi"})
        assert r.status_code == 401, r.text


def test_feedback_submit_persists_and_validates(monkeypatch, tmp_path):
    """Authenticated POST /feedback stores a row; empty message is rejected."""
    from meridian import db as db_module
    from meridian import _deps
    from meridian import hosted as hosted_module

    client = _make_hosted_client(monkeypatch, tmp_path)
    with client:
        db = client.app.state.db

        async def _setup():
            tenant = await db_module.upsert_tenant(db, "fb@meridian-test.invalid")
            session = await db_module.create_user_session(
                db, tenant["id"], "2099-01-01T00:00:00+00:00"
            )
            return tenant, session

        tenant, session = _run(_setup())
        # Route the tenant's project DB to the in-memory auth DB (production seam).
        _deps._tenant_db_cache[tenant["id"]] = db
        try:
            client.cookies.set(
                hosted_module._SESSION_COOKIE,
                hosted_module._make_session_cookie(session["id"]),
            )

            # Empty message → 400
            r_bad = client.post("/feedback", json={"type": "bug", "message": "  "})
            assert r_bad.status_code == 400, r_bad.text

            # Valid submission → 201 with an id
            r_ok = client.post(
                "/feedback",
                json={"type": "feature", "message": "Please add dark mode", "email": "fb@x.io"},
            )
            assert r_ok.status_code == 201, r_ok.text
            # The returned id is the freshly-inserted feedback row's uuid.
            assert r_ok.json().get("id")
            assert r_ok.json()["id"] != "demo"
        finally:
            _deps._tenant_db_cache.pop(tenant["id"], None)


def test_dashboard_js_has_feedback_button():
    """dashboard-demo.js wires the sidebar Send-feedback button + modal."""
    import pathlib
    js = pathlib.Path("meridian/static/dashboard-demo.js").read_text(encoding="utf-8")
    assert "ensureFeedbackButton" in js
    assert "showFeedbackModal" in js
    assert "/feedback" in js


def test_mcp_rate_limit_429_after_limit_exceeded(client, monkeypatch):
    """POST /mcp returns 429 with Retry-After after exceeding per-token rate limit."""
    from meridian import db as db_module
    from meridian import server as server_module

    async def _setup():
        db = client.app.state.db
        tenant = await db_module.upsert_tenant(db, "ratelimit@example.com")
        raw, _ = await db_module.create_api_token(db, tenant["id"], label="rl-test")
        return raw

    raw_token = _run(_setup())

    # Inject a mock _mcp_rate_check that rejects after the first call
    call_count = [0]

    def _mock_check(token_hash: str, limit: int) -> bool:
        call_count[0] += 1
        return call_count[0] > 1

    monkeypatch.setattr(server_module, "_mcp_rate_check", _mock_check)

    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {}}}
    headers = {"Authorization": f"Bearer {raw_token}"}

    r1 = client.post("/mcp", json=payload, headers=headers)
    assert r1.status_code == 200

    r2 = client.post("/mcp", json=payload, headers=headers)
    assert r2.status_code == 429
    assert r2.headers.get("Retry-After") == "60"
