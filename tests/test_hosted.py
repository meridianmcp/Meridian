"""Hosted and tenant-oriented tests for Meridian."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from meridian import db as db_module


def test_file_lock_round_trip(client):
    import json as _json

    def _run(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    async def _setup():
        db = client.app.state.db
        tenant = await db_module.upsert_tenant(db, "filelock@example.com")
        raw, _ = await db_module.create_api_token(db, tenant["id"])
        proj = await db_module.create_project(db, "file-lock-test")
        sess = await db_module.register_session(db, proj["id"], "locker")
        return raw, proj["id"], sess["id"]

    token, _pid, sid = _run(_setup())
    headers = {"Authorization": f"Bearer {token}"}

    def mcp(name, arguments):
        return client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": name, "arguments": arguments}},
            headers=headers,
        )

    r = mcp("claim_file", {"session_id": sid, "file_path": "src/foo.py"})
    assert r.status_code == 200
    result = _json.loads(r.json()["result"]["content"][0]["text"])
    assert result.get("claimed") is True

    r = mcp("release_file", {"session_id": sid, "file_path": "src/foo.py"})
    assert r.status_code == 200
    result = _json.loads(r.json()["result"]["content"][0]["text"])
    assert result.get("released") is True


def test_mcp_hitl_tools_lifecycle(client):
    """MCP tools/call: request_hitl -> list_hitl_requests -> answer_hitl -> get_hitl_request."""
    import json as _json

    async def _setup():
        db = client.app.state.db
        tenant = await db_module.upsert_tenant(db, "hitl_mcp@example.com")
        raw, _ = await db_module.create_api_token(db, tenant["id"])
        proj = await db_module.create_project(db, "mcp-hitl-proj")
        return raw, proj["id"]

    token, pid = asyncio.run(_setup())
    headers = {"Authorization": f"Bearer {token}"}

    def mcp(name, arguments):
        return client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": name, "arguments": arguments}},
            headers=headers,
        )

    r = mcp("request_hitl", {"project_id": pid, "question": "Ready to ship?"})
    assert r.status_code == 200
    hitl = _json.loads(r.json()["result"]["content"][0]["text"])
    hitl_id = hitl["id"]
    assert hitl["status"] == "pending"

    r = mcp("list_hitl_requests", {"project_id": pid, "status": "pending"})
    assert r.status_code == 200
    items = _json.loads(r.json()["result"]["content"][0]["text"])
    assert any(item["id"] == hitl_id for item in items)

    r = mcp("answer_hitl", {"request_id": hitl_id, "answer": "Yes, ship it!", "answered_by": "adam"})
    assert r.status_code == 200
    answered = _json.loads(r.json()["result"]["content"][0]["text"])
    assert answered["status"] == "answered"
    assert answered["answer"] == "Yes, ship it!"

    r = mcp("get_hitl_request", {"request_id": hitl_id})
    assert r.status_code == 200
    final = _json.loads(r.json()["result"]["content"][0]["text"])
    assert final["status"] == "answered"

    r = mcp("request_hitl", {"project_id": pid, "question": "Dismiss me?"})
    second_hitl_id = _json.loads(r.json()["result"]["content"][0]["text"])["id"]
    r = mcp("dismiss_hitl", {"request_id": second_hitl_id})
    assert r.status_code == 200
    assert _json.loads(r.json()["result"]["content"][0]["text"])["status"] == "dismissed"


def test_notification_email_sends_on_hitl(client, monkeypatch):
    """request_hitl should send an email notification when notify_url is an address."""
    import json as _json
    import httpx

    calls: list[dict[str, object]] = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, **kwargs):
            calls.append({"url": url, "kwargs": kwargs})

            class FakeResp:
                status_code = 200

            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())
    monkeypatch.setenv("RESEND_API_KEY", "resend-test-key")

    async def _setup():
        db = client.app.state.db
        tenant = await db_module.upsert_tenant(db, "hitl-email@example.com")
        raw, _ = await db_module.create_api_token(db, tenant["id"])
        proj = await db_module.create_project(db, "hitl-email-proj")
        await db_module.set_project_ntfy_url(db, proj["id"], "notify@example.com")
        return raw, proj["id"]

    token, pid = asyncio.run(_setup())
    headers = {"Authorization": f"Bearer {token}"}

    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "request_hitl",
                "arguments": {"project_id": pid, "question": "Ready to ship?"},
            },
        },
        headers=headers,
    )
    assert r.status_code == 200
    hitl = _json.loads(r.json()["result"]["content"][0]["text"])
    assert hitl["status"] == "pending"

    assert calls, "expected Resend to receive an email request"
    payload = calls[0]["kwargs"]["json"]
    assert calls[0]["url"] == "https://api.resend.com/emails"
    assert payload["to"] == ["notify@example.com"]
    assert payload["subject"] == "[Meridian] Action needed (NORMAL)"
    assert "Ready to ship?" in payload["text"]


@pytest.mark.asyncio
async def test_post_login_redirect_launch_open_provisions_when_capacity_available(
    db, monkeypatch
):
    from meridian import hosted as hosted_module

    tenant = await db_module.upsert_tenant(db, "launch-free@example.com")
    seen: dict[str, str] = {}

    async def fake_provision(tenant_id, _db):
        seen["tenant_id"] = tenant_id
        await db_module.update_tenant(
            _db,
            tenant_id,
            neon_project_id="neon-free-1",
            neon_db_url="encrypted-url",
        )
        return await db_module.get_tenant_by_id(_db, tenant_id)

    monkeypatch.setenv("MERIDIAN_LAUNCH_OPEN", "true")
    monkeypatch.delenv("MERIDIAN_FREE_LAUNCH_CAP", raising=False)
    monkeypatch.setattr(hosted_module, "provision_neon_db", fake_provision)

    dest = await hosted_module._post_login_redirect(tenant, db)
    assert dest == "/dashboard"
    assert seen["tenant_id"] == tenant["id"]


@pytest.mark.asyncio
async def test_post_login_redirect_launch_open_waitlists_when_capacity_full(
    db, monkeypatch
):
    from meridian import hosted as hosted_module

    for i in range(15):
        tenant = await db_module.upsert_tenant(db, f"filled-{i}@example.com")
        await db_module.update_tenant(
            db,
            tenant["id"],
            neon_project_id=f"neon-{i}",
            neon_db_url=f"enc-{i}",
        )
    new_tenant = await db_module.upsert_tenant(db, "latecomer@example.com")

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("provision_neon_db should not run when free launch is full")

    monkeypatch.setenv("MERIDIAN_LAUNCH_OPEN", "true")
    monkeypatch.delenv("MERIDIAN_FREE_LAUNCH_CAP", raising=False)
    monkeypatch.setattr(hosted_module, "provision_neon_db", fail_if_called)

    dest = await hosted_module._post_login_redirect(new_tenant, db)
    assert dest == "/waitlist-pending?message=Early%20access%20is%20full"

    waitlist = await db_module.get_waitlist(db)
    assert any(row["email"] == "latecomer@example.com" for row in waitlist)


@pytest.mark.asyncio
async def test_neon_pool_projects_table_exists(db):
    """neon_pool_projects table must exist after init_db."""
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='neon_pool_projects'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None, "neon_pool_projects table missing"


@pytest.mark.asyncio
async def test_pool_project_register_and_count(db):
    """register_pool_project and get_pool_project_counts work correctly."""
    from meridian.db import get_pool_project_counts, increment_pool_project_count, register_pool_project

    counts = await get_pool_project_counts(db, tier="standard")
    initial = counts["projects"]

    pool = await register_pool_project(db, "neon-test-proj-001", "standard")
    assert pool["neon_project_id"] == "neon-test-proj-001"
    assert pool["tier"] == "standard"
    assert pool["customer_count"] == 0

    await increment_pool_project_count(db, "neon-test-proj-001")
    counts = await get_pool_project_counts(db, tier="standard")
    assert counts["projects"] >= initial + 1
    assert counts["customers"] >= 1


@pytest.mark.asyncio
async def test_claim_pool_project_slot_atomic_under_concurrent_claims(db):
    """Item 38 — race regression: 50 concurrent claims against a pool with
    a single open slot must not overprovision past the cap.

    Before claim_pool_project_slot the signup hot path read get_available
    and later increment as two statements, letting concurrent claims both
    see 7/8 and both bump to 8/8 — leaving a 9/8 row in production. The
    atomic UPDATE with the outer ``customer_count < cap`` recheck ensures
    only one claim wins per slot.
    """
    import asyncio
    import uuid

    from meridian.db import (
        claim_pool_project_slot,
        register_pool_project,
    )

    cap = 8
    pool = await register_pool_project(db, f"neon-race-{uuid.uuid4().hex[:8]}", "standard")
    pool_id = pool["id"]
    # Saturate the pool to cap-1 so only one slot remains.
    await db.execute(
        "UPDATE neon_pool_projects SET customer_count = ? WHERE id = ?",
        (cap - 1, pool_id),
    )
    await db.commit()

    # Fire 50 concurrent claims. Exactly one must succeed.
    results = await asyncio.gather(
        *(claim_pool_project_slot(db, tier="standard", max_customers=cap) for _ in range(50))
    )

    successes = [r for r in results if r is not None]
    failures = [r for r in results if r is None]

    assert len(successes) == 1, (
        f"expected exactly 1 claim to win the last slot, got {len(successes)}"
    )
    assert len(failures) == 49, (
        f"expected 49 claims to lose, got {len(failures)}"
    )

    # The pool customer_count must equal the cap — not cap+N — proving the
    # outer WHERE guard prevented overprovisioning.
    async with db.execute(
        "SELECT customer_count FROM neon_pool_projects WHERE id = ?",
        (pool_id,),
    ) as cur:
        row = await cur.fetchone()
    final_count = row["customer_count"] if hasattr(row, "keys") else row[0]
    assert final_count == cap, (
        f"pool overprovisioned: customer_count={final_count}, cap={cap}"
    )


@pytest.mark.asyncio
async def test_claim_pool_project_slot_returns_none_when_all_full(db):
    """claim returns None when every pool of the tier is at cap, so the
    caller knows to register a fresh pool."""
    import uuid

    from meridian.db import claim_pool_project_slot, register_pool_project

    cap = 4
    for _ in range(3):
        pool = await register_pool_project(db, f"neon-full-{uuid.uuid4().hex[:8]}", "standard")
        await db.execute(
            "UPDATE neon_pool_projects SET customer_count = ? WHERE id = ?",
            (cap, pool["id"]),
        )
    await db.commit()

    result = await claim_pool_project_slot(db, tier="standard", max_customers=cap)
    assert result is None, f"expected None when all pools full, got {result}"


@pytest.mark.asyncio
async def test_get_available_pool_project(db):
    """get_available_pool_project returns project with room or None."""
    import uuid

    from meridian.db import get_available_pool_project, increment_pool_project_count, register_pool_project

    unique_id = str(uuid.uuid4())[:12]
    pool = await register_pool_project(db, f"neon-{unique_id}", "standard")
    neon_id = pool["neon_project_id"]

    found = await get_available_pool_project(db, tier="standard", max_customers=8)
    assert found is not None

    for _ in range(8):
        await increment_pool_project_count(db, neon_id)

    found = await get_available_pool_project(db, tier="standard", max_customers=8)
    if found is not None:
        assert found["neon_project_id"] != neon_id or found["customer_count"] < 8


@pytest.mark.asyncio
async def test_open_tenant_db_admin_falls_back_to_auth_db_when_decrypt_fails(monkeypatch):
    """Admin tenant with bad neon_db_url falls back to MERIDIAN_AUTH_DB."""
    from cryptography.fernet import InvalidToken

    import meridian._deps as deps_module
    import meridian.pg_adapter as pg_adapter

    deps_module._tenant_db_cache.clear()

    async def fake_get_tenant_by_id(_db, tenant_id):
        return {"id": tenant_id, "plan": "admin", "neon_db_url": "enc:not-valid"}

    def fake_decrypt_field(_value):
        raise InvalidToken()

    async def fake_init_pg_db(url):
        return {"opened_url": url}

    monkeypatch.setattr(db_module, "get_tenant_by_id", fake_get_tenant_by_id)
    monkeypatch.setattr(db_module, "decrypt_field", fake_decrypt_field)
    monkeypatch.setattr(pg_adapter, "init_pg_db", fake_init_pg_db)
    monkeypatch.setenv("MERIDIAN_AUTH_DB", "postgresql://fallback-db")

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db="auth-db")),
        state=SimpleNamespace(),
    )

    conn = await deps_module._open_tenant_db_by_id(request, "tenant-1")
    assert conn == {"opened_url": "postgresql://fallback-db"}


@pytest.mark.asyncio
async def test_non_admin_tenant_with_null_db_gets_503(monkeypatch):
    """Non-admin tenant with no neon_db_url must get 503, never the admin DB."""
    from fastapi import HTTPException

    import meridian._deps as deps_module

    deps_module._tenant_db_cache.clear()

    async def fake_get_tenant_by_id(_db, tenant_id):
        return {"id": tenant_id, "plan": "free", "neon_db_url": None}

    monkeypatch.setattr(db_module, "get_tenant_by_id", fake_get_tenant_by_id)
    monkeypatch.setenv("MERIDIAN_AUTH_DB", "postgresql://admin-db-must-not-leak")

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db="auth-db")),
        state=SimpleNamespace(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await deps_module._open_tenant_db_by_id(request, "free-tenant-1")

    assert exc_info.value.status_code == 503
    # Confirm auth DB was NOT returned
    assert deps_module._tenant_db_cache.get("free-tenant-1") is None


def test_strip_unsupported_pg_query_params_preserves_sslmode():
    import meridian.pg_adapter as pg_adapter

    url = (
        "postgresql://user:pass@ep-bitter-art-ajwunt4h-pooler.c-3.us-east-2.aws.neon.tech/"
        "cust_dradamawsome_48a1bd68?channel_binding=require&sslmode=require"
    )

    cleaned = pg_adapter._strip_unsupported_pg_query_params(url)

    assert cleaned == (
        "postgresql://user:pass@ep-bitter-art-ajwunt4h-pooler.c-3.us-east-2.aws.neon.tech/"
        "cust_dradamawsome_48a1bd68?sslmode=require"
    )


@pytest.mark.asyncio
async def test_create_neon_pool_project_omits_suspend_timeout(monkeypatch):
    import httpx

    import meridian.hosted as hosted_module

    seen: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "project": {"id": "neon-proj-1"},
                "connection_uris": [{"connection_uri": "postgresql://tenant-db"}],
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            seen["url"] = url
            seen["json"] = json
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=30: FakeClient())

    neon_project_id, conn_uri = await hosted_module._create_neon_pool_project("key", "free")
    assert neon_project_id == "neon-proj-1"
    assert conn_uri == "postgresql://tenant-db"
    endpoint_settings = seen["json"]["project"]["default_endpoint_settings"]
    assert "suspend_timeout_seconds" not in endpoint_settings


@pytest.mark.asyncio
async def test_create_customer_database_retries_locked_and_uses_role_name(monkeypatch):
    import httpx

    import meridian.hosted as hosted_module

    seen: dict[str, object] = {"post_calls": 0, "uri_url": ""}

    class FakeResponse:
        def __init__(self, status_code=200, payload=None):
            self.status_code = status_code
            self._payload = payload or {}

        def raise_for_status(self):
            if self.status_code >= 400 and self.status_code != 409:
                raise httpx.HTTPStatusError(
                    "error",
                    request=httpx.Request("GET", "https://example.test"),
                    response=httpx.Response(self.status_code),
                )

        def json(self):
            return self._payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers=None):
            if url.endswith("/branches"):
                return FakeResponse(payload={"branches": [{"id": "br-1", "primary": True}]})
            seen["uri_url"] = url
            return FakeResponse(payload={"uri": "postgresql://tenant-db"})

        async def post(self, url, headers=None, json=None):
            seen["post_calls"] += 1
            if seen["post_calls"] == 1:
                return FakeResponse(status_code=423)
            return FakeResponse(status_code=409)

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=15: FakeClient())
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    uri = await hosted_module._create_customer_database("key", "neon-proj-1", "cust_demo")
    assert uri == "postgresql://tenant-db"
    assert seen["post_calls"] == 2
    assert "branch_id=br-1" in seen["uri_url"]
    assert "database_name=cust_demo" in seen["uri_url"]
    assert "role_name=neondb_owner" in seen["uri_url"]


@pytest.mark.asyncio
async def test_provision_creates_metered_item(monkeypatch):
    """create_stripe_checkout_session includes the metered overage price when STRIPE_OVERAGE_PRICE_ID is set."""
    import sys
    from unittest.mock import MagicMock, patch

    import meridian.hosted as hosted_module

    monkeypatch.setattr(hosted_module, "STRIPE_PRICE_ID", "price_flat_test")
    monkeypatch.setattr(hosted_module, "STRIPE_OVERAGE_PRICE_ID", "price_overage_test")
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_fake")

    fake_session = MagicMock()
    fake_session.url = "https://checkout.stripe.com/pay/cs_test"

    mock_stripe = MagicMock()
    mock_stripe.checkout.Session.create.return_value = fake_session

    with patch.dict(sys.modules, {"stripe": mock_stripe}):
        url = await hosted_module.create_stripe_checkout_session({"id": "t1", "email": "a@b.com"}, "standard")

    assert url == fake_session.url
    create_call = mock_stripe.checkout.Session.create.call_args
    kwargs = create_call[1] if create_call[1] else create_call[0][0] if create_call[0] else {}
    line_items = kwargs.get("line_items", [])
    price_ids = [line_item["price"] for line_item in line_items]
    assert "price_flat_test" in price_ids
    assert "price_overage_test" in price_ids
    assert len(line_items) == 2


@pytest.mark.asyncio
async def test_overage_check_reports_usage(monkeypatch):
    """report_stripe_overage calls stripe.billing.MeterEvent.create with the correct payload."""
    import sys
    from unittest.mock import MagicMock, patch

    import meridian.hosted as hosted_module

    captured: dict = {}

    mock_stripe = MagicMock()
    mock_stripe.billing.MeterEvent.create.side_effect = lambda **kwargs: captured.update(kwargs)

    with patch.dict(sys.modules, {"stripe": mock_stripe}):
        await hosted_module.report_stripe_overage(
            stripe_customer_id="cus_abc123",
            overage_gb=2.5,
            stripe_api_key="sk_test_fake",
        )

    assert captured.get("event_name") == "storage_overage_gb"
    payload = captured.get("payload", {})
    assert payload.get("stripe_customer_id") == "cus_abc123"
    assert float(payload.get("value", 0)) == 2.5
