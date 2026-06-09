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
    assert t["trial_started_at"] is not None
    assert t["inactivity_expires_at"] is not None


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


def test_hooks_accept_valid_bearer_for_internal_tenant(monkeypatch, tmp_path):
    """Valid Bearer auth should allow hosted hooks to resolve the tenant DB."""
    from meridian import db as db_module
    import meridian.server as server_module

    async def _setup(db):
        tenant = await db_module.upsert_tenant(db, "hooks-hosted@example.com")
        raw_token, _ = await db_module.create_api_token(db, tenant["id"], label="hooks-test")
        project = await db_module.create_project(db, "hosted-hooks-valid")
        return raw_token, project

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        async def _open_same_db(_request, _tenant_id):
            return client.app.state.db

        monkeypatch.setattr(server_module, "_open_tenant_db_by_id", _open_same_db)
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
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {},
            },
        },
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["protocolVersion"] == "2024-11-05"


def test_remote_mcp_initialize_with_valid_oauth_token_from_db(client):
    """POST /mcp accepts OAuth bearer tokens reloaded from the auth DB."""
    from meridian import server as server_module

    async def _setup():
        db = client.app.state.db
        raw_token = "oauth-db-token"
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        await db.execute(
            "INSERT INTO oauth_tokens (token_hash, tenant_id, client_id, exp) VALUES (?, ?, ?, ?)",
            (token_hash, None, "claude-ai", int(time.time()) + 3600),
        )
        await db.commit()
        server_module._oa_tokens.clear()
        return raw_token, token_hash

    raw_token, token_hash = _run(_setup())

    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {},
            },
        },
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["protocolVersion"] == "2024-11-05"
    assert token_hash in server_module._oa_tokens


def test_remote_mcp_tools_list_returns_8_tools(client):
    """POST /mcp tools/list returns at least 8 tools."""
    from meridian import db as db_module

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
    assert len(tools) >= 8
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
    """dashboard.js wires the sidebar Send-feedback button + modal."""
    import pathlib
    js = pathlib.Path("meridian/static/dashboard.js").read_text(encoding="utf-8")
    assert "ensureFeedbackButton" in js
    assert "showFeedbackModal" in js
    assert "/feedback" in js
