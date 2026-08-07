"""Coverage tests for ``meridian.routes.export`` — GDPR data export + account
deletion routes.

Both routes are hosted-tier only (they 404 in self-host mode). The uncovered
lines were:

* line 26      — the demo-mode 403 short-circuit in ``export_my_data``.
* lines 53-84  — the entire body of ``delete_account`` after the ``_hosted_mode``
                 guard: the demo-mode 403, owner-permission gate, confirmation
                 check, Stripe cancel, Neon drop, tenant-record deletion, the
                 deletion email, and the session-cookie clear on the response.

These are exercised end-to-end through a real hosted-mode ``TestClient`` with an
authenticated tenant session cookie, so the tests assert genuine behaviour of
the uncovered branches (guards, error paths, and the happy path) rather than
poking at internals.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from meridian import db as db_module
from meridian import _deps


@contextmanager
def _hosted_client(monkeypatch, tmp_path):
    """A TestClient booted in hosted mode against in-memory SQLite.

    Mirrors the ``client`` conftest fixture's env isolation but flips
    ``MERIDIAN_HOSTED`` on so ``_hosted_mode()`` returns True and the export /
    delete routes actually run their bodies instead of 404-ing.
    """
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    monkeypatch.setenv("MERIDIAN_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))

    import meridian.server as server_module
    server_module = importlib.reload(server_module)
    # Blank live-DB fallbacks AFTER reload (reload re-reads .env) so nothing
    # points at a real backend during the test.
    monkeypatch.setenv("MERIDIAN_AUTH_DB", "")
    monkeypatch.setenv("MERIDIAN_STANDARD_KEY", "")

    # Rate-limit state is a module-level singleton that survives reloads; flush
    # the counters so the 3/minute cap on /export/my-data doesn't bleed across
    # tests. Keep the route-limit registrations intact: clearing the whole
    # storage would disable rate limiting for every later test in the worker.
    _deps._reset_limiter_counts()

    with TestClient(server_module.app) as c:
        yield c

    _deps._reset_limiter_counts()


def _seed_tenant_session(c, email="export@example.com", **tenant_fields):
    """Create a tenant (+ optional column overrides) and an authed session cookie.

    The tenant is put on the ``admin`` plan so ``_deps._db`` resolves the
    per-request "project DB" to the in-memory auth DB itself (an admin without a
    dedicated Neon URL falls back to ``app.state.db``) instead of raising 503 for
    an unprovisioned tenant DB. That keeps these tests hermetic (no Neon).
    """
    from meridian.hosted import _make_session_cookie

    db = c.app.state.db
    tenant = asyncio.run(db_module.upsert_tenant(db, email))
    fields = {"plan": "admin", **tenant_fields}
    asyncio.run(db_module.update_tenant(db, tenant["id"], **fields))
    tenant = asyncio.run(db_module.get_tenant_by_id(db, tenant["id"]))
    session = asyncio.run(
        db_module.create_user_session(db, tenant["id"], "2099-01-01 00:00:00")
    )
    c.cookies.set("meridian_session", _make_session_cookie(session["id"]))
    return tenant


# ---------------------------------------------------------------------------
# Self-host mode — both routes 404 (the _hosted_mode() guard)
# ---------------------------------------------------------------------------

def test_export_my_data_404_in_self_host(client):
    assert client.get("/export/my-data").status_code == 404


def test_delete_account_404_in_self_host(client):
    r = client.post("/account/delete", json={"confirmation": "DELETE"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# export_my_data — demo mode short-circuit (line 26)
# ---------------------------------------------------------------------------

def test_export_my_data_demo_mode_returns_403(monkeypatch, tmp_path):
    """A demo-cookie request in hosted mode gets the 403 'not in demo' payload."""
    with _hosted_client(monkeypatch, tmp_path) as c:
        c.cookies.set("meridian_demo", "1")
        r = c.get("/export/my-data")
        assert r.status_code == 403
        assert "demo mode" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# export_my_data — happy path (lines 30-45)
# ---------------------------------------------------------------------------

def test_export_my_data_returns_attachment(monkeypatch, tmp_path):
    """Authenticated tenant gets a JSON attachment with a filename from their email."""
    with _hosted_client(monkeypatch, tmp_path) as c:
        _seed_tenant_session(c, email="alice@example.com")
        r = c.get("/export/my-data")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/json")
        # filename slug is derived from the local-part of the email (line 39-40)
        cd = r.headers["content-disposition"]
        assert "attachment" in cd
        assert "meridian-export-alice.json" in cd
        body = r.json()
        assert body["tenant"]["email"] == "alice@example.com"


def test_export_my_data_filename_falls_back_to_user(monkeypatch, tmp_path):
    """When the tenant email is blank, the filename slug falls back to 'user'.

    Exercises the ``(tenant.get("email") or "user")`` fallback on line 39.
    """
    with _hosted_client(monkeypatch, tmp_path) as c:
        tenant = _seed_tenant_session(c, email="blankslug@example.com")
        # Force the email column empty so the ``or "user"`` branch is taken.
        db = c.app.state.db
        asyncio.run(
            db.execute("UPDATE tenants SET email = '' WHERE id = ?", (tenant["id"],))
        )
        asyncio.run(db.commit())
        r = c.get("/export/my-data")
        assert r.status_code == 200, r.text
        assert "meridian-export-user.json" in r.headers["content-disposition"]


# ---------------------------------------------------------------------------
# delete_account — demo mode short-circuit (lines 53-57)
# ---------------------------------------------------------------------------

def test_delete_account_demo_read_only_middleware_blocks_post(monkeypatch, tmp_path):
    """A demo-cookie POST is short-circuited by the app's demo read-only middleware.

    The middleware ('demo_readonly') runs *before* the route, so a mutating
    demo request never reaches ``delete_account``'s own in-body demo guard. This
    documents that outer defense — the route's line-54 guard is defense-in-depth
    behind it (covered directly below).
    """
    with _hosted_client(monkeypatch, tmp_path) as c:
        c.cookies.set("meridian_demo", "1")
        r = c.post("/account/delete", json={"confirmation": "DELETE"})
        assert r.status_code == 403
        assert r.json()["error"] == "demo_readonly"


def test_delete_account_in_body_demo_guard_returns_403():
    """Directly exercise ``delete_account``'s in-body demo guard (line 53-57).

    The demo read-only middleware normally intercepts mutating demo requests
    before the route runs, so the only way to reach the route's own demo branch
    is to invoke the coroutine directly with a request that reads as hosted +
    demo. Asserts the 403 'not available in demo mode' JSON payload.
    """
    from meridian.routes import export as export_mod

    class _FakeURL:
        path = "/account/delete"

    class _FakeRequest:
        # hosted mode is toggled via env in the test; demo via the cookie.
        cookies = {"meridian_demo": "1"}
        headers: dict = {}
        url = _FakeURL()

    import os

    prev = os.environ.get("MERIDIAN_HOSTED")
    os.environ["MERIDIAN_HOSTED"] = "1"
    try:
        resp = asyncio.run(export_mod.delete_account(_FakeRequest()))  # type: ignore[arg-type]
    finally:
        if prev is None:
            os.environ.pop("MERIDIAN_HOSTED", None)
        else:
            os.environ["MERIDIAN_HOSTED"] = prev

    assert resp.status_code == 403
    body = json.loads(bytes(resp.body))
    assert "demo mode" in body["detail"].lower()


# ---------------------------------------------------------------------------
# delete_account — auth / permission gate (line 64)
# ---------------------------------------------------------------------------

def test_delete_account_requires_auth(monkeypatch, tmp_path):
    """No session cookie → get_current_tenant raises 401 before the perm gate (line 61)."""
    with _hosted_client(monkeypatch, tmp_path) as c:
        r = c.post("/account/delete", json={"confirmation": "DELETE"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# delete_account — confirmation guard (lines 65-67)
# ---------------------------------------------------------------------------

def test_delete_account_wrong_confirmation_returns_400(monkeypatch, tmp_path):
    with _hosted_client(monkeypatch, tmp_path) as c:
        _seed_tenant_session(c)
        r = c.post("/account/delete", json={"confirmation": "nope"})
        assert r.status_code == 400
        assert "DELETE" in r.json()["detail"]


def test_delete_account_missing_confirmation_returns_400(monkeypatch, tmp_path):
    """Empty body ⇒ confirmation is None ⇒ 400 (line 66-67)."""
    with _hosted_client(monkeypatch, tmp_path) as c:
        _seed_tenant_session(c)
        r = c.post("/account/delete", json={})
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# delete_account — full happy path (lines 69-84)
# ---------------------------------------------------------------------------

def test_delete_account_happy_path_no_billing(monkeypatch, tmp_path):
    """Owner with no Stripe/Neon: skips those branches, deletes records, clears cookie.

    Covers the falsy sides of the ``stripe_id`` (line 70) and ``neon_project_id``
    (line 73) guards plus the record deletion (77) and cookie clear (82-84).
    """
    with _hosted_client(monkeypatch, tmp_path) as c:
        import meridian.hosted as hosted

        sent: list[str] = []

        async def _fake_send(email):
            sent.append(email)

        monkeypatch.setattr(hosted, "send_account_deleted_email", _fake_send)

        tenant = _seed_tenant_session(c, email="deleteme@example.com")
        r = c.post("/account/delete", json={"confirmation": "DELETE"})
        assert r.status_code == 200, r.text
        assert r.json() == {"deleted": True}
        # The response clears the session cookie (line 83).
        set_cookie = r.headers.get("set-cookie", "")
        assert "meridian_session" in set_cookie

        # The tenant row is actually gone (delete_tenant_records ran — line 77).
        # The email dispatch (line 79-80) fires an asyncio.create_task; the stub
        # keeps it from leaving the process. The response returning 200 confirms
        # that branch was reached without error.
        db = c.app.state.db
        assert asyncio.run(db_module.get_tenant_by_id(db, tenant["id"])) is None


def test_delete_account_happy_path_with_stripe_and_neon(monkeypatch, tmp_path):
    """Owner WITH stripe_customer_id + neon_project_id exercises both branches.

    Covers lines 70-71 (cancel_stripe_subscription) and 73-74
    (_drop_tenant_neon_database create_task) and 79-80 (send email create_task).
    All external side-effects are stubbed so nothing leaves the process.
    """
    with _hosted_client(monkeypatch, tmp_path) as c:
        import meridian.hosted as hosted

        cancelled: list[str] = []
        dropped: list[dict] = []
        emailed: list[str] = []

        async def _fake_cancel(stripe_id):
            cancelled.append(stripe_id)

        async def _fake_drop(tenant):
            dropped.append(tenant)

        async def _fake_email(email):
            emailed.append(email)

        monkeypatch.setattr(hosted, "cancel_stripe_subscription", _fake_cancel)
        monkeypatch.setattr(hosted, "_drop_tenant_neon_database", _fake_drop)
        monkeypatch.setattr(hosted, "send_account_deleted_email", _fake_email)

        tenant = _seed_tenant_session(
            c,
            email="paid@example.com",
            stripe_customer_id="cus_test123",
            neon_project_id="neon-proj-abc",
        )
        r = c.post("/account/delete", json={"confirmation": "DELETE"})
        assert r.status_code == 200, r.text
        assert r.json() == {"deleted": True}

        # Stripe cancellation ran synchronously with the tenant's customer id.
        assert cancelled == ["cus_test123"]

        # The tenant record was deleted.
        db = c.app.state.db
        assert asyncio.run(db_module.get_tenant_by_id(db, tenant["id"])) is None
