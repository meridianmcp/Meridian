"""Coverage-raising tests for billing, admin, decisions, error_alerting, demo_seed.

Self-contained: reuses the in-memory hosted-client / _run patterns from
test_v2_hosted.py and the `client`/`db` fixtures from conftest.py. All external
services (Stripe, Resend email, httpx, ntfy) are mocked — no network I/O.
"""
from __future__ import annotations

import asyncio
import json
import os

import aiosqlite
import pytest


def _run(coro):
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

    import importlib
    from fastapi.testclient import TestClient
    import meridian.server as server_module

    server_module = importlib.reload(server_module)
    return TestClient(server_module.app)


def _auth_admin_session(client, monkeypatch, email):
    """Create an admin tenant + signed session cookie; return tenant dict.

    Uses the MERIDIAN_ADMIN_EMAILS env fallback for is_admin_db, and routes
    the tenant's project DB to the in-memory auth DB via the production seam.
    """
    from meridian import db as db_module
    from meridian import _deps
    from meridian import hosted as hosted_module

    monkeypatch.setenv("MERIDIAN_ADMIN_EMAILS", email)
    db = client.app.state.db

    async def _setup():
        tenant = await db_module.upsert_tenant(db, email)
        session = await db_module.create_user_session(
            db, tenant["id"], "2099-01-01T00:00:00+00:00"
        )
        return tenant, session

    tenant, session = _run(_setup())
    _deps._tenant_db_cache[tenant["id"]] = db
    client.cookies.set(
        hosted_module._SESSION_COOKIE,
        hosted_module._make_session_cookie(session["id"]),
    )
    return tenant


# ===========================================================================
# billing.py
# ===========================================================================

def test_waitlist_duplicate_returns_409(client):
    """Second POST /waitlist with the same email returns 409."""
    email = "dup-wl@example.com"
    r1 = client.post("/waitlist", json={"email": email, "note": "hi", "plan": "pro"})
    assert r1.status_code in (201, 409)
    r2 = client.post("/waitlist", json={"email": email})
    assert r2.status_code == 409


def test_waitlist_invalid_email_returns_422(client):
    """POST /waitlist with no '@' returns 422."""
    r = client.post("/waitlist", json={"email": "not-an-email"})
    assert r.status_code == 422


def test_waitlist_list_endpoint(client):
    """GET /waitlist returns the list of entries."""
    client.post("/waitlist", json={"email": "wl-list@example.com"})
    r = client.get("/waitlist")
    assert r.status_code == 200
    assert any(e.get("email") == "wl-list@example.com" for e in r.json())


def test_waitlist_pending_page_default(client):
    """GET /waitlist-pending returns the default 'on the list' page."""
    r = client.get("/waitlist-pending")
    assert r.status_code == 200
    assert "waitlist" in r.text.lower()
    assert "on the list" in r.text.lower()


def test_waitlist_pending_page_with_message(client):
    """GET /waitlist-pending?message=... renders the 'early access full' badge."""
    r = client.get("/waitlist-pending", params={"message": "Early access is full"})
    assert r.status_code == 200
    assert "Early access is full" in r.text


def test_admin_waitlist_page_403_when_not_authenticated(client):
    """GET /admin/waitlist returns 403 HTML when no session."""
    r = client.get("/admin/waitlist")
    assert r.status_code == 403
    assert "403" in r.text


def test_admin_waitlist_page_403_when_not_admin(monkeypatch, tmp_path):
    """A signed-in non-admin tenant gets 403 from /admin/waitlist."""
    from meridian import db as db_module
    from meridian import hosted as hosted_module

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        monkeypatch.setenv("MERIDIAN_ADMIN_EMAILS", "")
        db = client.app.state.db

        async def _setup():
            tenant = await db_module.upsert_tenant(db, "plain@example.com")
            session = await db_module.create_user_session(
                db, tenant["id"], "2099-01-01T00:00:00+00:00"
            )
            return session

        session = _run(_setup())
        client.cookies.set(
            hosted_module._SESSION_COOKIE,
            hosted_module._make_session_cookie(session["id"]),
        )
        r = client.get("/admin/waitlist")
        assert r.status_code == 403
        assert "Admin only" in r.text


def test_admin_waitlist_page_renders_for_admin(monkeypatch, tmp_path):
    """An admin tenant sees the waitlist management page with stats."""
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _auth_admin_session(client, monkeypatch, "admin@example.com")
        client.post("/waitlist", json={"email": "seed-wl@example.com", "note": "n"})
        r = client.get("/admin/waitlist")
        assert r.status_code == 200
        assert "Waitlist Management" in r.text
        assert "Total Tenants" in r.text
        assert "seed-wl@example.com" in r.text


def test_admin_delete_waitlist_403_when_unauthenticated(client):
    """DELETE /admin/waitlist/{id} without auth returns 403."""
    r = client.delete("/admin/waitlist/some-id")
    assert r.status_code == 403


def test_admin_delete_waitlist_403_when_not_admin(monkeypatch, tmp_path):
    """Non-admin tenant gets 403 from DELETE /admin/waitlist/{id}."""
    from meridian import db as db_module
    from meridian import hosted as hosted_module

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        monkeypatch.setenv("MERIDIAN_ADMIN_EMAILS", "")
        db = client.app.state.db

        async def _setup():
            tenant = await db_module.upsert_tenant(db, "plain2@example.com")
            session = await db_module.create_user_session(
                db, tenant["id"], "2099-01-01T00:00:00+00:00"
            )
            return session

        session = _run(_setup())
        client.cookies.set(
            hosted_module._SESSION_COOKIE,
            hosted_module._make_session_cookie(session["id"]),
        )
        r = client.delete("/admin/waitlist/x")
        assert r.status_code == 403


def test_admin_delete_waitlist_succeeds_for_admin(monkeypatch, tmp_path):
    """Admin can delete a waitlist entry by id."""
    from meridian import db as db_module

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _auth_admin_session(client, monkeypatch, "admin@example.com")
        db = client.app.state.db
        entry = _run(db_module.add_waitlist_entry(db, "del-me@example.com", None))
        r = client.delete(f"/admin/waitlist/{entry['id']}")
        assert r.status_code == 200
        assert r.json()["deleted"] is True
        remaining = _run(db_module.get_waitlist(db))
        assert all(e["id"] != entry["id"] for e in remaining)


def test_billing_portal_get_redirects_to_login_when_unauthenticated(client):
    """GET /billing/portal redirects to /auth/login when not signed in."""
    r = client.get("/billing/portal", follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" in r.headers["location"]


def test_billing_portal_get_redirects_to_pricing_without_customer(monkeypatch, tmp_path):
    """Signed-in tenant with no stripe_customer_id is redirected to /pricing."""
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _auth_admin_session(client, monkeypatch, "noportal@example.com")
        r = client.get("/billing/portal", follow_redirects=False)
        assert r.status_code == 302
        assert "/pricing" in r.headers["location"]


def test_billing_portal_get_redirects_to_stripe_with_customer(monkeypatch, tmp_path):
    """With a stripe_customer_id, GET /billing/portal redirects to the Stripe URL."""
    from meridian import db as db_module
    from meridian.routes import billing as billing_module

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        tenant = _auth_admin_session(client, monkeypatch, "hasportal@example.com")
        db = client.app.state.db
        _run(db_module.update_tenant(db, tenant["id"], stripe_customer_id="cus_abc"))

        async def _fake_portal(t):
            return "https://billing.stripe.com/p/session_xyz"

        monkeypatch.setattr(
            billing_module, "create_stripe_billing_portal_session", _fake_portal,
            raising=False,
        )
        # The route imports the symbol lazily from ..hosted, so patch there too.
        import meridian.hosted as hosted_module
        monkeypatch.setattr(
            hosted_module, "create_stripe_billing_portal_session", _fake_portal
        )
        r = client.get("/billing/portal", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "https://billing.stripe.com/p/session_xyz"


def test_billing_portal_post_401_when_unauthenticated(client):
    """POST /billing/portal returns 401 JSON when not signed in."""
    r = client.post("/billing/portal")
    assert r.status_code == 401


def test_billing_portal_post_404_without_customer(monkeypatch, tmp_path):
    """POST /billing/portal returns 404 when tenant has no billing account."""
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _auth_admin_session(client, monkeypatch, "nobill@example.com")
        r = client.post("/billing/portal")
        assert r.status_code == 404


def test_billing_portal_post_returns_url(monkeypatch, tmp_path):
    """POST /billing/portal returns the Stripe URL as JSON for a paying tenant."""
    from meridian import db as db_module
    import meridian.hosted as hosted_module

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        tenant = _auth_admin_session(client, monkeypatch, "paying@example.com")
        db = client.app.state.db
        _run(db_module.update_tenant(db, tenant["id"], stripe_customer_id="cus_pay"))

        async def _fake_portal(t):
            return "https://billing.stripe.com/p/abc"

        monkeypatch.setattr(
            hosted_module, "create_stripe_billing_portal_session", _fake_portal
        )
        r = client.post("/billing/portal")
        assert r.status_code == 200
        assert r.json()["url"] == "https://billing.stripe.com/p/abc"


def test_billing_portal_post_503_when_stripe_unconfigured(monkeypatch, tmp_path):
    """POST /billing/portal surfaces RuntimeError (Stripe unconfigured) as 503."""
    from meridian import db as db_module
    import meridian.hosted as hosted_module

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        tenant = _auth_admin_session(client, monkeypatch, "noconf@example.com")
        db = client.app.state.db
        _run(db_module.update_tenant(db, tenant["id"], stripe_customer_id="cus_x"))

        async def _boom(t):
            raise RuntimeError("STRIPE_API_KEY is not configured")

        monkeypatch.setattr(
            hosted_module, "create_stripe_billing_portal_session", _boom
        )
        r = client.post("/billing/portal")
        assert r.status_code == 503


def test_checkout_invalid_plan_returns_400(client):
    """GET /checkout?plan=bogus returns 400."""
    r = client.get("/checkout", params={"plan": "bogus"}, follow_redirects=False)
    assert r.status_code == 400


def test_checkout_redirects_to_login_when_unauthenticated(client):
    """GET /checkout redirects to /auth/login when not signed in."""
    r = client.get("/checkout", params={"plan": "pro"}, follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" in r.headers["location"]


def test_checkout_fallback_payment_link_when_stripe_unconfigured(monkeypatch, tmp_path):
    """When Stripe raises RuntimeError, /checkout falls back to STRIPE_PAYMENT_LINK."""
    import meridian.hosted as hosted_module

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _auth_admin_session(client, monkeypatch, "checkout@example.com")
        monkeypatch.setenv("STRIPE_PAYMENT_LINK", "https://pay.example.com/link")

        async def _boom(tenant, plan):
            raise RuntimeError("not configured")

        monkeypatch.setattr(
            hosted_module, "create_stripe_checkout_session", _boom
        )
        r = client.get("/checkout", params={"plan": "standard"}, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "https://pay.example.com/link"


def test_checkout_redirects_to_stripe_url(monkeypatch, tmp_path):
    """GET /checkout redirects to the Stripe checkout URL on success."""
    import meridian.hosted as hosted_module

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _auth_admin_session(client, monkeypatch, "checkout2@example.com")

        async def _fake(tenant, plan):
            return f"https://checkout.stripe.com/c/{plan}"

        monkeypatch.setattr(
            hosted_module, "create_stripe_checkout_session", _fake
        )
        r = client.get("/checkout", params={"plan": "pro"}, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "https://checkout.stripe.com/c/pro"


def test_stripe_webhook_dunning_payment_failed(monkeypatch, tmp_path):
    """invoice.payment_failed stamps payment_failed_at on the matching tenant."""
    from meridian import db as db_module

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
        db = client.app.state.db

        async def _setup():
            tenant = await db_module.upsert_tenant(db, "dunning@example.com")
            await db_module.update_tenant(db, tenant["id"], stripe_customer_id="cus_dun")
            return tenant

        tenant = _run(_setup())
        r = client.post(
            "/webhooks/stripe",
            json={
                "type": "invoice.payment_failed",
                "data": {"object": {"customer": "cus_dun"}},
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "dunning_started"
        refreshed = _run(db_module.get_tenant_by_id(db, tenant["id"]))
        assert refreshed["payment_failed_at"] is not None


def test_stripe_webhook_no_email_returns_no_email(client):
    """A handled event with no resolvable email returns status no_email."""
    os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
    r = client.post(
        "/webhooks/stripe",
        json={"type": "checkout.session.completed", "data": {"object": {}}},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "no_email"


def test_stripe_webhook_invalid_json_returns_400(client):
    """Malformed JSON body (no signature secret) returns 400."""
    os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
    r = client.post(
        "/webhooks/stripe",
        content=b"not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_stripe_webhook_capacity_exceeded(monkeypatch, tmp_path):
    """When check_capacity raises, the webhook returns capacity_exceeded."""
    import meridian.hosted as hosted_module

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)

        async def _cap(db):
            raise RuntimeError("at hard limit")

        monkeypatch.setattr(hosted_module, "check_capacity", _cap)
        r = client.post(
            "/webhooks/stripe",
            json={
                "type": "checkout.session.completed",
                "data": {"object": {
                    "customer_email": "cap@example.com",
                    "customer": "cus_cap",
                    "metadata": {"plan": "pro"},
                }},
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "capacity_exceeded"


def test_stripe_webhook_provisioning_queued_on_failure(monkeypatch, tmp_path):
    """When provision_neon_db raises, the webhook returns provisioning_queued."""
    import meridian.hosted as hosted_module

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)

        async def _cap(db):
            return None

        async def _boom(tenant_id, db):
            raise Exception("neon down")

        monkeypatch.setattr(hosted_module, "check_capacity", _cap)
        monkeypatch.setattr(hosted_module, "provision_neon_db", _boom)
        r = client.post(
            "/webhooks/stripe",
            json={
                "type": "invoice.paid",
                "data": {"object": {
                    "customer_email": "queued@example.com",
                    "customer": "cus_q",
                }},
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "provisioning_queued"


def test_stripe_webhook_full_success_path(monkeypatch, tmp_path):
    """Happy path: capacity ok, provisioning ok, token minted, welcome email sent."""
    from meridian import db as db_module
    import meridian.hosted as hosted_module

    sent = {}

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
        db = client.app.state.db

        async def _cap(db_):
            return None

        async def _provision(tenant_id, db_):
            return await db_module.get_tenant_by_id(db_, tenant_id)

        async def _welcome(email, raw_token, tenant):
            sent["email"] = email
            sent["token"] = raw_token

        monkeypatch.setattr(hosted_module, "check_capacity", _cap)
        monkeypatch.setattr(hosted_module, "provision_neon_db", _provision)
        monkeypatch.setattr(hosted_module, "send_welcome_email", _welcome)

        r = client.post(
            "/webhooks/stripe",
            json={
                "type": "checkout.session.completed",
                "data": {"object": {
                    "customer_details": {"email": "success@example.com"},
                    "customer": "cus_ok",
                    "metadata": {"plan": "standard"},
                }},
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert sent["email"] == "success@example.com"
        assert sent["token"].startswith("sk_meridian_")


def test_stripe_webhook_metered_subscription_item(monkeypatch, tmp_path):
    """When an overage price is configured, the webhook stores the metered item id."""
    from meridian import db as db_module
    import meridian.hosted as hosted_module

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
        db = client.app.state.db

        monkeypatch.setattr(hosted_module, "STRIPE_OVERAGE_PRICE_ID", "price_meter")
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test")

        # Fake the `stripe` module Subscription.retrieve call.
        import types
        fake_stripe = types.ModuleType("stripe")

        class _Item:
            def __init__(self, _id, price_id):
                self.id = _id
                self.price = types.SimpleNamespace(id=price_id)

        class _Sub:
            items = types.SimpleNamespace(
                data=[_Item("si_meter", "price_meter"), _Item("si_base", "price_base")]
            )

        fake_stripe.Subscription = types.SimpleNamespace(retrieve=lambda _id: _Sub())
        fake_stripe.api_key = ""
        monkeypatch.setitem(__import__("sys").modules, "stripe", fake_stripe)

        async def _cap(db_):
            return None

        async def _provision(tenant_id, db_):
            return await db_module.get_tenant_by_id(db_, tenant_id)

        async def _welcome(*a, **k):
            return None

        monkeypatch.setattr(hosted_module, "check_capacity", _cap)
        monkeypatch.setattr(hosted_module, "provision_neon_db", _provision)
        monkeypatch.setattr(hosted_module, "send_welcome_email", _welcome)

        r = client.post(
            "/webhooks/stripe",
            json={
                "type": "checkout.session.completed",
                "data": {"object": {
                    "customer_email": "meter@example.com",
                    "customer": "cus_meter",
                    "subscription": "sub_123",
                    "metadata": {"plan": "pro"},
                }},
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        tenant = _run(db_module.get_tenant_by_stripe_customer(db, "cus_meter"))
        assert tenant["stripe_metered_item_id"] == "si_meter"


# ===========================================================================
# admin.py
# ===========================================================================

def test_admin_health_403_when_unauthenticated(client):
    """GET /admin/health returns 403 with no session."""
    r = client.get("/admin/health")
    assert r.status_code == 403


def test_admin_health_403_when_not_admin(monkeypatch, tmp_path):
    """Signed-in non-admin tenant gets 403 from /admin/health."""
    from meridian import db as db_module
    from meridian import hosted as hosted_module

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        monkeypatch.setenv("MERIDIAN_ADMIN_EMAILS", "")
        db = client.app.state.db

        async def _setup():
            tenant = await db_module.upsert_tenant(db, "h-plain@example.com")
            session = await db_module.create_user_session(
                db, tenant["id"], "2099-01-01T00:00:00+00:00"
            )
            return session

        session = _run(_setup())
        client.cookies.set(
            hosted_module._SESSION_COOKIE,
            hosted_module._make_session_cookie(session["id"]),
        )
        r = client.get("/admin/health")
        assert r.status_code == 403


def test_admin_health_json_for_admin(monkeypatch, tmp_path):
    """Admin (no admin password set) gets the health JSON with counters."""
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        monkeypatch.delenv("MERIDIAN_ADMIN_PASSWORD", raising=False)
        _auth_admin_session(client, monkeypatch, "health-admin@example.com")
        r = client.get("/admin/health")
        assert r.status_code == 200
        body = r.json()
        assert body["hosted_mode"] is True
        assert "tenants_total" in body
        assert "sprint_pending" in body
        assert "version" in body


def test_admin_health_403_when_password_required(monkeypatch, tmp_path):
    """With MERIDIAN_ADMIN_PASSWORD set and no cookie, admin health returns 403."""
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        _auth_admin_session(client, monkeypatch, "pw-admin@example.com")
        monkeypatch.setenv("MERIDIAN_ADMIN_PASSWORD", "s3cret")
        r = client.get("/admin/health")
        assert r.status_code == 403
        assert "password" in r.text.lower()


def test_admin_stats_403_when_unauthenticated(client):
    """d1cb1100 — GET /admin/stats returns 403 with no session."""
    r = client.get("/admin/stats")
    assert r.status_code == 403


def test_admin_stats_json_for_admin(monkeypatch, tmp_path):
    """d1cb1100 — admin gets launch/user stats: free_count, cap (+percent),
    total_tenants, provisioned, waitlist."""
    from meridian import db as db_module

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        monkeypatch.delenv("MERIDIAN_ADMIN_PASSWORD", raising=False)
        monkeypatch.setenv("MERIDIAN_FREE_LAUNCH_CAP", "1000")
        _auth_admin_session(client, monkeypatch, "stats-admin@example.com")
        db = client.app.state.db

        async def _seed():
            t = await db_module.upsert_tenant(db, "free-user@example.com")
            await db_module.update_tenant(
                db, t["id"], neon_project_id="neon-x", neon_db_url="enc",
            )
            await db_module.add_waitlist_entry(db, "waiter@example.com", note="test")

        _run(_seed())
        r = client.get("/admin/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["cap"] == 1000
        assert body["free_count"] >= 1
        assert body["provisioned"] >= 1
        assert body["waitlist"] >= 1
        assert body["total_tenants"] >= 1
        assert 0.0 <= body["percent"] <= 100.0


def test_me_endpoint_shows_trial_days_for_free_tenant(monkeypatch, tmp_path):
    """509d9de1 — a free tenant with no inactivity_expires_at still gets a real
    days_remaining (30-day window anchored on created_at), not None, so the
    banner shows an actual number instead of 'limited time'."""
    from meridian import db as db_module
    from meridian import hosted as hosted_module

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        auth_db = client.app.state.db

        async def _setup():
            tenant = await db_module.upsert_tenant(auth_db, "free-days@example.com")
            session = await db_module.create_user_session(
                auth_db, tenant["id"], "2099-01-01T00:00:00+00:00")
            return session

        session = _run(_setup())
        client.cookies.set(
            hosted_module._SESSION_COOKIE,
            hosted_module._make_session_cookie(session["id"]))
        body = client.get("/me").json()
        assert body.get("plan") == "free"
        assert isinstance(body.get("days_remaining"), int)
        assert 0 <= body["days_remaining"] <= 30


def test_open_tenant_db_retries_init_before_503(monkeypatch, tmp_path):
    """894f7645 — _open_tenant_db_by_id retries init_pg_db on a transient failure
    (provisioning race) before returning 503, so a freshly-provisioned tenant's
    first request doesn't hard-fail."""
    import types
    from meridian import _deps
    from meridian import db as db_module
    import meridian.tenant_crypto as _tc
    import meridian.pg_adapter as _pg

    with _make_hosted_client(monkeypatch, tmp_path) as client:
        auth_db = client.app.state.db

        async def _seed():
            t = await db_module.upsert_tenant(auth_db, "retry@example.com")
            await db_module.update_tenant(auth_db, t["id"], neon_db_url="enc-blob")
            return t

        tenant = _run(_seed())
        tid = tenant["id"]
        _deps._tenant_db_cache.pop(tid, None)

        monkeypatch.setattr(_tc, "decrypt_tenant_db_url", lambda _t, _b: "postgres://fake")
        calls = {"n": 0}
        sentinel = object()

        async def flaky_init(_url):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("db not accepting connections yet")
            return sentinel

        monkeypatch.setattr(_pg, "init_pg_db", flaky_init)

        req = types.SimpleNamespace(
            app=types.SimpleNamespace(state=types.SimpleNamespace(db=auth_db)))
        try:
            conn = _run(_deps._open_tenant_db_by_id(req, tid))
            assert conn is sentinel
            assert calls["n"] == 3
        finally:
            _deps._tenant_db_cache.pop(tid, None)


def test_admin_git_status_returns_shape(client):
    """GET /admin/git-status returns a dict with ahead/behind keys (ok either way)."""
    r = client.get("/admin/git-status")
    assert r.status_code == 200
    body = r.json()
    assert "ok" in body
    assert "behind" in body
    assert "ahead" in body


def test_admin_shutdown_blocked_in_demo_mode(client, monkeypatch):
    """POST /admin/shutdown is blocked (403) for demo requests."""
    monkeypatch.setenv("MERIDIAN_DEMO", "1")
    r = client.post("/admin/shutdown")
    assert r.status_code == 403
    assert "demo" in r.text.lower()


def test_admin_restart_blocked_in_demo_mode(client, monkeypatch):
    """POST /admin/restart is blocked (403) for demo requests."""
    monkeypatch.setenv("MERIDIAN_DEMO", "1")
    r = client.post("/admin/restart", json={"confirm": True})
    assert r.status_code == 403


def test_admin_restart_requires_confirm(client):
    """POST /admin/restart without confirm returns a warning, not a restart."""
    r = client.post("/admin/restart", json={})
    assert r.status_code == 200
    body = r.json()
    assert body.get("requires_confirm") is True
    assert "warning" in body


def test_admin_shutdown_schedules_kill(client, monkeypatch):
    """Non-demo POST /admin/shutdown returns ok and schedules a delayed SIGINT."""
    monkeypatch.delenv("MERIDIAN_DEMO", raising=False)
    killed = []

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("meridian.routes.admin.asyncio.sleep", _no_sleep)
    monkeypatch.setattr("meridian.routes.admin.os.kill", lambda *a: killed.append(a))
    r = client.post("/admin/shutdown")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_admin_restart_confirmed_spawns_and_kills(client, monkeypatch):
    """Confirmed non-demo POST /admin/restart spawns a new process then kills self."""
    monkeypatch.delenv("MERIDIAN_DEMO", raising=False)
    spawned = []
    killed = []

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("meridian.routes.admin.asyncio.sleep", _no_sleep)
    import subprocess as _sp
    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr("meridian.routes.admin.os.kill", lambda *a: killed.append(a))
    r = client.post("/admin/restart", json={"confirm": True})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_admin_snapshot_reads_db_file(client, monkeypatch, tmp_path):
    """GET /admin/snapshot streams the on-disk SQLite file when MERIDIAN_DB is a path."""
    db_file = tmp_path / "snap.db"
    db_file.write_bytes(b"SQLite format 3\x00fake-bytes")
    monkeypatch.setenv("MERIDIAN_DB", str(db_file))
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    r = client.get("/admin/snapshot")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-sqlite3")
    assert r.content.startswith(b"SQLite format 3")


def test_admin_snapshot_missing_file_returns_500(client, monkeypatch, tmp_path):
    """A non-existent DB path produces a 500 (could not read DB file)."""
    monkeypatch.setenv("MERIDIAN_DB", str(tmp_path / "nope.db"))
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    r = client.get("/admin/snapshot")
    assert r.status_code == 500


@pytest.mark.sqlite_only  # guard only fires when MERIDIAN_DB_URL is absent; PG run sets it so no 400
def test_admin_snapshot_rejects_memory_db(client):
    """GET /admin/snapshot returns 400 for an in-memory DB (no file to read)."""
    r = client.get("/admin/snapshot")
    # client fixture uses MERIDIAN_DB=:memory: with no MERIDIAN_DB_URL.
    assert r.status_code == 400
    assert "in-memory" in r.json()["detail"].lower()


# ===========================================================================
# decisions.py
# ===========================================================================

def test_list_pinned_decisions_404_for_missing_project(client):
    """GET decisions-pinned for an unknown project returns 404."""
    r = client.get("/projects/does-not-exist/decisions-pinned")
    assert r.status_code == 404


def _make_project(client):
    r = client.post("/projects", json={"name": f"dec-{os.urandom(4).hex()}"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_create_pinned_decision_404_for_missing_project(client):
    """POST decisions-pinned for unknown project returns 404."""
    r = client.post(
        "/projects/nope/decisions-pinned",
        json={"title": "T", "body": "B"},
    )
    assert r.status_code == 404


def test_create_pinned_decision_400_when_missing_fields(client):
    """POST decisions-pinned without title/body returns 400."""
    pid = _make_project(client)
    r = client.post(
        f"/projects/{pid}/decisions-pinned",
        json={"title": "", "body": ""},
    )
    assert r.status_code == 400


def test_create_and_list_pinned_decision(client):
    """Full create → list round-trip for a pinned decision."""
    pid = _make_project(client)
    r = client.post(
        f"/projects/{pid}/decisions-pinned",
        json={"title": "Use psycopg3", "body": "asyncpg DLL issues", "category": "TECHNICAL"},
    )
    assert r.status_code == 201
    created = r.json()
    assert created["title"] == "Use psycopg3"

    listed = client.get(f"/projects/{pid}/decisions-pinned")
    assert listed.status_code == 200
    assert any(d["id"] == created["id"] for d in listed.json())


def test_update_pinned_decision_patch_fields(client):
    """PATCH a decision's title/status returns the updated row."""
    pid = _make_project(client)
    created = client.post(
        f"/projects/{pid}/decisions-pinned",
        json={"title": "Original", "body": "Body"},
    ).json()
    r = client.patch(
        f"/projects/{pid}/decisions-pinned/{created['id']}",
        json={"title": "Renamed"},
    )
    assert r.status_code == 200
    assert r.json()["title"] == "Renamed"


def test_update_pinned_decision_404_for_unknown_id(client):
    """PATCH an unknown decision id returns 404."""
    pid = _make_project(client)
    r = client.patch(
        f"/projects/{pid}/decisions-pinned/ghost",
        json={"title": "x"},
    )
    assert r.status_code == 404


def test_update_pinned_decision_supersede(client):
    """PATCH with new_title + new_body supersedes and creates a fresh decision."""
    pid = _make_project(client)
    created = client.post(
        f"/projects/{pid}/decisions-pinned",
        json={"title": "Old way", "body": "old"},
    ).json()
    r = client.patch(
        f"/projects/{pid}/decisions-pinned/{created['id']}",
        json={"new_title": "New way", "new_body": "new", "category": "TECHNICAL"},
    )
    assert r.status_code == 200
    assert r.json()["title"] == "New way"


def test_delete_pinned_decision(client):
    """DELETE a pinned decision returns 204; deleting again returns 404."""
    pid = _make_project(client)
    created = client.post(
        f"/projects/{pid}/decisions-pinned",
        json={"title": "Doomed", "body": "x"},
    ).json()
    r = client.delete(f"/projects/{pid}/decisions-pinned/{created['id']}")
    assert r.status_code == 204
    again = client.delete(f"/projects/{pid}/decisions-pinned/{created['id']}")
    assert again.status_code == 404


def test_replace_all_pinned_decisions_400_when_empty(client):
    """POST replace-all with no decisions returns 400."""
    pid = _make_project(client)
    r = client.post(
        f"/projects/{pid}/decisions-pinned/replace-all",
        json={"decisions": []},
    )
    assert r.status_code == 400


def test_replace_all_pinned_decisions_supersedes_and_creates(client):
    """replace-all retires existing decisions and creates the supplied set."""
    pid = _make_project(client)
    client.post(
        f"/projects/{pid}/decisions-pinned",
        json={"title": "First", "body": "one"},
    )
    r = client.post(
        f"/projects/{pid}/decisions-pinned/replace-all",
        json={"decisions": [
            {"title": "Merged A", "body": "a", "category": "TECHNICAL"},
            {"title": "Merged B", "body": "b"},
        ]},
    )
    assert r.status_code == 201
    titles = {d["title"] for d in r.json()}
    assert {"Merged A", "Merged B"} <= titles
    # Active list should now reflect the replacement set, not "First".
    active = client.get(f"/projects/{pid}/decisions-pinned").json()
    active_titles = {d["title"] for d in active}
    assert "First" not in active_titles


def test_archive_oldest_with_no_decisions(client):
    """archive-oldest returns {archived: 0} when there are none."""
    pid = _make_project(client)
    r = client.post(
        f"/projects/{pid}/decisions-pinned/archive-oldest",
        json={"count": 2},
    )
    assert r.status_code == 200
    assert r.json()["archived"] == 0


def test_archive_oldest_bad_count_returns_400(client):
    """archive-oldest with a non-integer count returns 400."""
    pid = _make_project(client)
    r = client.post(
        f"/projects/{pid}/decisions-pinned/archive-oldest",
        json={"count": "lots"},
    )
    assert r.status_code == 400


def test_archive_oldest_archives_n(client):
    """archive-oldest retires the N oldest active decisions."""
    pid = _make_project(client)
    for i in range(3):
        client.post(
            f"/projects/{pid}/decisions-pinned",
            json={"title": f"D{i}", "body": f"b{i}"},
        )
    r = client.post(
        f"/projects/{pid}/decisions-pinned/archive-oldest",
        json={"count": 2},
    )
    assert r.status_code == 200
    assert r.json()["archived"] == 2
    active = client.get(f"/projects/{pid}/decisions-pinned").json()
    assert len(active) == 1


def test_consolidate_decisions_400_without_api_key(client):
    """consolidate without api_key returns 400."""
    pid = _make_project(client)
    r = client.post(
        f"/projects/{pid}/decisions/consolidate",
        json={},
    )
    assert r.status_code == 400


def test_consolidate_decisions_400_when_no_decisions(client):
    """consolidate with api_key but no pinned decisions returns 400."""
    pid = _make_project(client)
    r = client.post(
        f"/projects/{pid}/decisions/consolidate",
        json={"api_key": "sk-test"},
    )
    assert r.status_code == 400


def test_consolidate_decisions_success_with_mocked_anthropic(client, monkeypatch):
    """consolidate calls the Anthropic API (mocked) and returns the parsed set."""
    import httpx

    pid = _make_project(client)
    client.post(
        f"/projects/{pid}/decisions-pinned",
        json={"title": "Keep A", "body": "a"},
    )
    client.post(
        f"/projects/{pid}/decisions-pinned",
        json={"title": "Keep B", "body": "b"},
    )

    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"content": [{"text": json.dumps(
                {"decisions": [{"title": "Merged", "category": "TECHNICAL", "body": "m"}]}
            )}]}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    r = client.post(
        f"/projects/{pid}/decisions/consolidate",
        json={"api_key": "sk-test", "model": "claude-haiku-4-5-20251001"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["original_count"] == 2
    assert body["consolidated"][0]["title"] == "Merged"


def test_consolidate_decisions_502_on_http_error(client, monkeypatch):
    """consolidate surfaces upstream HTTP errors as 502."""
    import httpx

    pid = _make_project(client)
    client.post(
        f"/projects/{pid}/decisions-pinned",
        json={"title": "Only", "body": "x"},
    )

    class _FakeResp:
        status_code = 429
        text = "rate limited"

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "429", request=httpx.Request("POST", "https://api.anthropic.com"),
                response=httpx.Response(429, text="rate limited"),
            )

        def json(self):
            return {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    r = client.post(
        f"/projects/{pid}/decisions/consolidate",
        json={"api_key": "sk-test"},
    )
    assert r.status_code == 502


# ===========================================================================
# error_alerting.py
# ===========================================================================

def test_record_5xx_fires_dispatch_hook_at_threshold(monkeypatch):
    """record_5xx invokes the dispatch hook once the threshold is met."""
    from meridian import error_alerting as ea

    monkeypatch.setenv("MERIDIAN_5XX_ALERT_THRESHOLD", "3")
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_WINDOW_SECS", "300")
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_COOLDOWN_SECS", "900")
    ea._reset_for_tests()

    captured = []
    ea._set_dispatch_hook(lambda payload: captured.append(payload))

    async def _drive():
        for _ in range(2):
            await ea.record_5xx("/x", "tenant-1", 500)
        # Threshold not yet met
        assert captured == []
        await ea.record_5xx("/y", "tenant-2", 503)

    _run(_drive())
    assert len(captured) == 1
    assert captured[0]["count"] == 3
    assert captured[0]["last_route"] == "/y"
    assert captured[0]["last_status"] == 503
    ea._reset_for_tests()


def test_record_5xx_cooldown_suppresses_second_alert(monkeypatch):
    """Within the cooldown, a second threshold breach does not re-dispatch."""
    from meridian import error_alerting as ea

    monkeypatch.setenv("MERIDIAN_5XX_ALERT_THRESHOLD", "2")
    monkeypatch.setenv("MERIDIAN_5XX_ALERT_COOLDOWN_SECS", "9999")
    ea._reset_for_tests()

    captured = []
    ea._set_dispatch_hook(lambda payload: captured.append(payload))

    async def _drive():
        for _ in range(5):
            await ea.record_5xx("/x", None, 500)

    _run(_drive())
    assert len(captured) == 1  # cooldown blocks the rest
    ea._reset_for_tests()


def test_record_5xx_hook_exception_is_swallowed(monkeypatch):
    """A raising dispatch hook is caught and does not propagate."""
    from meridian import error_alerting as ea

    monkeypatch.setenv("MERIDIAN_5XX_ALERT_THRESHOLD", "1")
    ea._reset_for_tests()

    def _boom(payload):
        raise RuntimeError("hook failed")

    ea._set_dispatch_hook(_boom)

    async def _drive():
        await ea.record_5xx("/x", None, 500)  # must not raise

    _run(_drive())
    ea._reset_for_tests()


def test_send_alert_no_notifier_configured(monkeypatch):
    """_send_alert with no ntfy/email configured returns without error."""
    from meridian import error_alerting as ea

    monkeypatch.setenv("MERIDIAN_ADMIN_NTFY_URL", "")
    monkeypatch.setenv("ADMIN_EMAIL", "")
    payload = {
        "count": 11, "window_secs": 300,
        "last_route": "/boom", "last_tenant": None, "last_status": 500,
    }
    _run(ea._send_alert(payload))  # no exception → pass


def test_send_alert_dispatches_ntfy_and_email(monkeypatch):
    """_send_alert posts to both ntfy and Resend when both are configured."""
    from meridian import error_alerting as ea

    monkeypatch.setenv("MERIDIAN_ADMIN_NTFY_URL", "meridian-alerts")
    monkeypatch.setenv("ADMIN_EMAIL", "ops@example.com")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")

    posted = []

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, *a, **k):
            posted.append(url)
            return None

    import meridian.error_alerting as ea_mod

    # Both _send_ntfy and _send_email import httpx lazily; patch the module.
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    payload = {
        "count": 12, "window_secs": 600,
        "last_route": "/x", "last_tenant": "t1", "last_status": 500,
    }
    _run(ea_mod._send_alert(payload))
    assert any("ntfy.sh" in u for u in posted)
    assert any("resend.com" in u for u in posted)


def test_send_email_skips_without_api_key(monkeypatch):
    """_send_email returns early (no post) when RESEND_API_KEY is unset."""
    from meridian import error_alerting as ea

    monkeypatch.delenv("RESEND_API_KEY", raising=False)

    posted = []

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, *a, **k):
            posted.append(url)

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    _run(ea._send_email("x@example.com", "subj", "body"))
    assert posted == []


def test_cfg_int_falls_back_on_bad_value(monkeypatch):
    """_cfg_int returns the default when the env var is non-numeric."""
    from meridian import error_alerting as ea

    monkeypatch.setenv("MERIDIAN_5XX_ALERT_THRESHOLD", "not-a-number")
    assert ea._cfg_int("MERIDIAN_5XX_ALERT_THRESHOLD", 10) == 10


# ===========================================================================
# demo_seed.py
# ===========================================================================

def test_seed_demo_data_creates_projects_and_tasks():
    """_seed_demo_data populates two demo projects with sessions/tasks/items."""
    from meridian import db as db_module
    from meridian.demo_seed import _seed_demo_data

    async def _drive():
        db = await db_module.init_db(":memory:")
        try:
            await _seed_demo_data(db)
            projects = await db_module.list_projects(db)
            names = {p["name"] for p in projects}

            async def _count(sql):
                async with db.execute(sql) as cur:
                    row = await cur.fetchone()
                return (row[0] if row else 0) or 0

            tasks = await _count("SELECT COUNT(*) FROM task_log")
            sprint = await _count("SELECT COUNT(*) FROM sprint_items")
            sessions = await _count("SELECT COUNT(*) FROM sessions")
            return names, tasks, sprint, sessions
        finally:
            await db.close()

    names, tasks, sprint, sessions = _run(_drive())
    assert {"backend-api-v2", "data-pipeline"} <= names
    assert tasks > 0
    assert sprint > 0
    assert sessions > 0


def test_seed_demo_data_is_idempotent_wipe_and_reseed():
    """Calling _seed_demo_data twice wipes the old data and re-seeds cleanly."""
    from meridian import db as db_module
    from meridian.demo_seed import _seed_demo_data

    async def _drive():
        db = await db_module.init_db(":memory:")
        try:
            await _seed_demo_data(db)
            await _seed_demo_data(db)  # second pass exercises the wipe branch
            projects = await db_module.list_projects(db)
            # Still exactly the two demo projects — no duplication.
            names = [p["name"] for p in projects]
            return names
        finally:
            await db.close()

    names = _run(_drive())
    assert sorted(names) == ["backend-api-v2", "data-pipeline"]


def test_seed_decisions_from_file_noop_when_already_seeded():
    """_seed_decisions_from_file is a noop when the project already has decisions."""
    from meridian import db as db_module
    from meridian.demo_seed import _seed_decisions_from_file

    async def _drive():
        db = await db_module.init_db(":memory:")
        try:
            project = await db_module.create_project(db, "decproj")
            await db_module.pin_decision(db, project["id"], "Existing", "body", "TECHNICAL")
            before = await db_module.count_decisions(db, project["id"])
            await _seed_decisions_from_file(db, project["id"])
            after = await db_module.count_decisions(db, project["id"])
            return before, after
        finally:
            await db.close()

    before, after = _run(_drive())
    assert before == 1
    assert after == 1  # unchanged — early return because count > 0


def test_seed_decisions_from_file_parses_markdown(tmp_path, monkeypatch):
    """_seed_decisions_from_file parses ## sections of DECISIONS.md into rows."""
    from meridian import db as db_module
    from meridian import demo_seed

    decisions_md = tmp_path / "DECISIONS.md"
    decisions_md.write_text(
        "# Decisions\n\n"
        "## Use Redis for caching\n"
        "Better data structures for rate-limit counters and pub/sub invalidation.\n\n"
        "## Pricing model is per-seat\n"
        "Billing and revenue scale with team size; Stripe handles proration.\n\n"
        "## Dashboard uses a single JS file\n"
        "Product/UI simplicity beats a build step for this feature surface.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(demo_seed, "_REPO_ROOT", tmp_path)

    async def _drive():
        db = await db_module.init_db(":memory:")
        try:
            project = await db_module.create_project(db, "mdproj")
            await demo_seed._seed_decisions_from_file(db, project["id"])
            decisions = await db_module.get_pinned_decisions(db, project["id"])
            return decisions
        finally:
            await db.close()

    decisions = _run(_drive())
    titles = {d["title"] for d in decisions}
    cats = {d["title"]: d["category"] for d in decisions}
    assert "Use Redis for caching" in titles
    assert "Pricing model is per-seat" in titles
    # Category guesser: 'pricing/billing/revenue' → BUSINESS, 'dashboard/ui' → PRODUCT.
    assert cats["Pricing model is per-seat"] == "BUSINESS"
    assert cats["Dashboard uses a single JS file"] == "PRODUCT"
