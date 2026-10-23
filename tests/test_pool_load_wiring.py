"""1b2fbebe -- the request-path hook and the admin read endpoint for pool telemetry.

The telemetry module itself is covered by test_pool_telemetry.py and the noisy-tenant drill by
test_tenant_pool_isolation.py; this file pins the two places it touches the server:
``_deps._db`` counting a request against its tenant's Neon pool project, and
``GET /admin/pool-load`` (admin + admin-password gated like /admin/health, redacted,
per-process)."""

from __future__ import annotations

import asyncio
import json

import pytest

from meridian import _deps
from meridian.pool_telemetry import METER, pool_label, tenant_label


def _run(coro):
    return asyncio.run(coro)


def _make_hosted_client(monkeypatch, tmp_path):
    """Hosted-mode TestClient backed by an in-memory auth DB (same recipe as test_cov_misc)."""
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


def _sign_in(client, email):
    """Create a tenant plus a signed session cookie; route its project DB to the auth DB."""
    from meridian import db as db_module
    from meridian import hosted as hosted_module

    db = client.app.state.db

    async def _setup():
        tenant = await db_module.upsert_tenant(db, email)
        session = await db_module.create_user_session(db, tenant["id"], "2099-01-01T00:00:00+00:00")
        return tenant, session

    tenant, session = _run(_setup())
    _deps._tenant_db_cache[tenant["id"]] = db
    client.cookies.set(hosted_module._SESSION_COOKIE, hosted_module._make_session_cookie(session["id"]))
    return tenant


@pytest.fixture(autouse=True)
def _clean_meter():
    METER.reset()
    yield
    METER.reset()
    _deps._tenant_pool_ids.clear()


class _Req:
    """The part of a Starlette Request that _note_pool_tenant touches."""

    def __init__(self):
        import types

        self.state = types.SimpleNamespace()


def test_note_pool_tenant_stashes_the_tenants_pool_project_on_the_request():
    _deps._tenant_pool_ids["tenant-1"] = "green-glitter-12345678"
    req = _Req()
    _deps._note_pool_tenant(req, "tenant-1")
    assert req.state._pool_attr == ("tenant-1", "green-glitter-12345678")


def test_a_tenant_without_a_pool_project_is_stashed_with_none():
    req = _Req()
    _deps._note_pool_tenant(req, "admin-tenant")  # never opened through _open_tenant_db_by_id
    assert req.state._pool_attr == ("admin-tenant", None)


def test_note_pool_tenant_never_raises():
    class Hostile:
        @property
        def state(self):
            raise RuntimeError("no state for you")

    _deps._note_pool_tenant(Hostile(), "tenant-1")  # must not propagate


def test_a_hosted_request_through_the_db_resolver_is_counted(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        tenant = _sign_in(client, "pool-counted@example.com")
        METER.reset()
        _deps._tenant_pool_ids[tenant["id"]] = "pool-free-ABCD1234"
        r = client.get("/projects")
        assert r.status_code == 200, r.text
        windows = [w for w in METER.tenant_windows() if w.label == tenant_label(tenant["id"])]
        assert windows and windows[0].requests >= 1
        assert windows[0].pool == "p_ABCD1234"
        assert windows[0].p95_ms is not None  # the middleware timed it, not just counted it


def test_pool_load_refuses_an_unauthenticated_caller(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        r = client.get("/admin/pool-load")
        # The hosted default-deny gate answers 401 before the route's own 403 is reached.
        assert r.status_code in (401, 403)
        assert "top_tenants" not in r.text


def test_pool_load_403_for_a_non_admin_tenant(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        monkeypatch.setenv("MERIDIAN_ADMIN_EMAILS", "")
        _sign_in(client, "pool-plain@example.com")
        assert client.get("/admin/pool-load").status_code == 403


def test_pool_load_403_when_the_admin_password_is_required(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        monkeypatch.setenv("MERIDIAN_ADMIN_EMAILS", "pool-pw@example.com")
        _sign_in(client, "pool-pw@example.com")
        monkeypatch.setenv("MERIDIAN_ADMIN_PASSWORD", "s3cret")
        r = client.get("/admin/pool-load")
        assert r.status_code == 403
        assert "password" in r.text.lower()


def test_pool_load_returns_a_redacted_per_process_snapshot_to_an_admin(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        monkeypatch.delenv("MERIDIAN_ADMIN_PASSWORD", raising=False)
        monkeypatch.setenv("MERIDIAN_ADMIN_EMAILS", "pool-admin@example.com")
        tenant = _sign_in(client, "pool-admin@example.com")
        _deps._tenant_pool_ids[tenant["id"]] = "pool-free-ZZZZ9999"
        for _ in range(3):
            assert client.get("/projects").status_code == 200

        r = client.get("/admin/pool-load")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {
            "thresholds", "pools", "top_tenants", "recommendations",
            "tracked_tenants", "evicted_tenants", "scope",
        }
        assert body["scope"] == "this server process only"
        assert body["thresholds"]["provisional"] is True
        assert any(p["pool"] == "p_ZZZZ9999" for p in body["pools"])
        text = json.dumps(body)
        assert tenant["id"] not in text
        assert "pool-admin@example.com" not in text
        assert "pool-free-ZZZZ9999" not in text


def test_pool_load_clamps_top_n(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        monkeypatch.delenv("MERIDIAN_ADMIN_PASSWORD", raising=False)
        monkeypatch.setenv("MERIDIAN_ADMIN_EMAILS", "pool-top@example.com")
        _sign_in(client, "pool-top@example.com")
        for i in range(60):
            METER.record(f"tenant-{i}", "proj-aaaaaaaa")
        assert len(client.get("/admin/pool-load?top_n=1000").json()["top_tenants"]) == 50
        assert len(client.get("/admin/pool-load?top_n=0").json()["top_tenants"]) == 1


# ---------------------------------------------------------------------------
# PoolTimingMiddleware (pure ASGI)
# ---------------------------------------------------------------------------


def _drive(app, *, scope_type="http"):
    """Run an ASGI app once; return the messages it sent."""
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": scope_type, "state": {}}
    asyncio.run(app(scope, receive, send))
    return scope, sent


def _app(status=200, attribute=("tenant-1", "pool-free-AAAAAAAA"), extra_starts=0):
    async def app(scope, receive, send):
        if attribute is not None:
            scope["state"]["_pool_attr"] = attribute
        await send({"type": "http.response.start", "status": status, "headers": []})
        for _ in range(extra_starts):
            await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def test_middleware_records_the_attributed_request_with_its_latency():
    from meridian.pool_timing import PoolTimingMiddleware

    _, sent = _drive(PoolTimingMiddleware(_app()))
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]  # forwarded unchanged
    (w,) = METER.tenant_windows()
    assert w.label == tenant_label("tenant-1") and w.pool == "p_AAAAAAAA"
    assert w.requests == 1 and w.errors == 0 and w.p95_ms is not None


def test_middleware_counts_a_server_error_as_an_error():
    from meridian.pool_timing import PoolTimingMiddleware

    _drive(PoolTimingMiddleware(_app(status=503)))
    (w,) = METER.tenant_windows()
    assert w.errors == 1


def test_middleware_ignores_requests_that_never_resolved_a_tenant():
    from meridian.pool_timing import PoolTimingMiddleware

    _drive(PoolTimingMiddleware(_app(attribute=None)))
    assert METER.tenant_windows() == []


def test_middleware_records_a_request_once_even_if_the_app_starts_a_response_twice():
    from meridian.pool_timing import PoolTimingMiddleware

    _drive(PoolTimingMiddleware(_app(extra_starts=2)))
    assert METER.tenant_windows()[0].requests == 1


def test_middleware_passes_websocket_and_lifespan_scopes_straight_through():
    from meridian.pool_timing import PoolTimingMiddleware

    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    for kind in ("websocket", "lifespan"):
        _drive(PoolTimingMiddleware(app), scope_type=kind)
    assert seen == ["websocket", "lifespan"]
    assert METER.tenant_windows() == []


def test_middleware_lets_an_app_exception_propagate_and_records_nothing():
    from meridian.pool_timing import PoolTimingMiddleware

    async def boom(scope, receive, send):
        raise ValueError("handler blew up")

    with pytest.raises(ValueError, match="handler blew up"):
        _drive(PoolTimingMiddleware(boom))
    assert METER.tenant_windows() == []


def test_middleware_still_forwards_the_response_when_telemetry_fails(monkeypatch):
    import meridian.pool_timing as pt
    from meridian.pool_timing import PoolTimingMiddleware

    def boom(*a, **k):
        raise RuntimeError("telemetry is broken")

    monkeypatch.setattr(pt, "record_request", boom)
    _, sent = _drive(PoolTimingMiddleware(_app()))
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]


def test_the_server_registers_the_middleware_outermost():
    import meridian.server as server_module
    from meridian.pool_timing import PoolTimingMiddleware

    assert server_module.app.user_middleware[0].cls is PoolTimingMiddleware
