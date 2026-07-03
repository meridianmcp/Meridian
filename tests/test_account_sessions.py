"""3c28450d — active web-session management: list + tenant-scoped revoke."""
from __future__ import annotations

import asyncio

from meridian import db as db_module


def _gated_client(monkeypatch, tmp_path):
    """A hosted-mode TestClient over a fresh in-memory DB (no site-password gate)."""
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    monkeypatch.setenv("MERIDIAN_SESSION_SECRET", "test-secret")
    from fastapi.testclient import TestClient
    import importlib
    import meridian.server as server_module
    server_module = importlib.reload(server_module)
    monkeypatch.setenv("MERIDIAN_AUTH_DB", "")
    monkeypatch.setenv("MERIDIAN_STANDARD_KEY", "")
    return TestClient(server_module.app)


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

def test_user_session_metadata_and_list():
    async def _run():
        db = await db_module.init_db(":memory:")
        t = await db_module.upsert_tenant(db, "acct@example.com")
        s1 = await db_module.create_user_session(
            db, t["id"], "2099-01-01 00:00:00", user_agent="UA/1.0", ip="1.2.3.4"
        )
        await db_module.create_user_session(db, t["id"], "2099-01-01 00:00:00")
        return s1, await db_module.get_user_sessions_for_tenant(db, t["id"])

    s1, sessions = asyncio.run(_run())
    assert s1["user_agent"] == "UA/1.0"
    assert s1["ip"] == "1.2.3.4"
    assert s1["last_seen_at"]  # seeded at creation
    assert len(sessions) == 2


def test_revoke_user_session_is_tenant_scoped():
    async def _run():
        db = await db_module.init_db(":memory:")
        t1 = await db_module.upsert_tenant(db, "one@example.com")
        t2 = await db_module.upsert_tenant(db, "two@example.com")
        s1 = await db_module.create_user_session(db, t1["id"], "2099-01-01 00:00:00")
        wrong = await db_module.revoke_user_session(db, s1["id"], t2["id"])
        still = await db_module.get_user_sessions_for_tenant(db, t1["id"])
        right = await db_module.revoke_user_session(db, s1["id"], t1["id"])
        gone = await db_module.get_user_sessions_for_tenant(db, t1["id"])
        return wrong, len(still), right, len(gone)

    wrong, still, right, gone = asyncio.run(_run())
    assert wrong is False and still == 1   # another tenant can't revoke it
    assert right is True and gone == 0     # owner can


def test_expired_sessions_excluded_from_list():
    async def _run():
        db = await db_module.init_db(":memory:")
        t = await db_module.upsert_tenant(db, "exp@example.com")
        await db_module.create_user_session(db, t["id"], "2000-01-01 00:00:00")  # expired
        await db_module.create_user_session(db, t["id"], "2099-01-01 00:00:00")  # live
        return await db_module.get_user_sessions_for_tenant(db, t["id"])

    assert len(asyncio.run(_run())) == 1


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

def test_account_sessions_list_marks_current_and_revokes(monkeypatch, tmp_path):
    with _gated_client(monkeypatch, tmp_path) as c:
        from meridian.hosted import _make_session_cookie
        db = c.app.state.db
        t = asyncio.run(db_module.upsert_tenant(db, "ep@example.com"))
        cur = asyncio.run(
            db_module.create_user_session(db, t["id"], "2099-01-01 00:00:00")
        )
        other = asyncio.run(
            db_module.create_user_session(db, t["id"], "2099-01-01 00:00:00")
        )
        c.cookies.set("meridian_session", _make_session_cookie(cur["id"]))

        r = c.get("/account/sessions")
        assert r.status_code == 200, r.text
        by_id = {s["id"]: s for s in r.json()["sessions"]}
        assert len(by_id) == 2
        assert by_id[cur["id"]]["current"] is True
        assert by_id[other["id"]]["current"] is False

        rr = c.post(f"/account/sessions/{other['id']}/revoke")
        assert rr.status_code == 200, rr.text
        assert rr.json()["revoked"] is True
        assert len(c.get("/account/sessions").json()["sessions"]) == 1


def test_account_sessions_requires_auth(monkeypatch, tmp_path):
    with _gated_client(monkeypatch, tmp_path) as c:
        r = c.get("/account/sessions")
        assert r.status_code in (401, 403)
