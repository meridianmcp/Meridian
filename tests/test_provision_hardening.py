"""4c559d4e — onboarding hardening: durable provision queue + retry/backoff +
deep health probe."""
from __future__ import annotations

import asyncio

from meridian import db as db_module
from meridian import hosted


def _hosted_client(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    from fastapi.testclient import TestClient
    import importlib
    import meridian.server as server_module
    server_module = importlib.reload(server_module)
    monkeypatch.setenv("MERIDIAN_AUTH_DB", "")
    monkeypatch.setenv("MERIDIAN_STANDARD_KEY", "")
    return TestClient(server_module.app)


# ---------------------------------------------------------------------------
# provision_queue persistence
# ---------------------------------------------------------------------------

def test_provision_queue_enqueue_bump_and_count():
    async def _run():
        db = await db_module.init_db(":memory:")
        await db_module.enqueue_provision(db, "t1", last_error="boom")
        await db_module.enqueue_provision(db, "t1", last_error="boom2")  # bumps attempts
        await db_module.enqueue_provision(db, "t2")
        pend = await db_module.get_pending_provisions(db)
        c = await db_module.count_pending_provisions(db)
        await db_module.mark_provision_done(db, "t1")
        c2 = await db_module.count_pending_provisions(db)
        t1 = next(p for p in pend if p["tenant_id"] == "t1")
        return c, c2, t1["attempts"], t1["last_error"]

    c, c2, attempts, err = asyncio.run(_run())
    assert c == 2
    assert c2 == 1               # t1 marked done
    assert attempts == 2         # second enqueue bumped it
    assert err == "boom2"


# ---------------------------------------------------------------------------
# provision_with_retry
# ---------------------------------------------------------------------------

def test_provision_with_retry_succeeds_first_try(monkeypatch):
    async def _run():
        db = await db_module.init_db(":memory:")
        calls = {"n": 0}

        async def fake_provision(tid, d):
            calls["n"] += 1
            return {"ok": True}

        monkeypatch.setattr(hosted, "provision_neon_db", fake_provision)
        res = await hosted.provision_with_retry("t1", db)
        return res, calls["n"], await db_module.count_pending_provisions(db)

    res, n, pend = asyncio.run(_run())
    assert res == {"ok": True}
    assert n == 1
    assert pend == 0             # nothing queued on success


def test_provision_with_retry_backs_off_then_succeeds(monkeypatch):
    async def _run():
        db = await db_module.init_db(":memory:")
        calls = {"n": 0}
        delays: list[float] = []

        async def flaky(tid, d):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("neon 423")
            return {"ok": True}

        async def fake_sleep(s):
            delays.append(s)

        monkeypatch.setattr(hosted, "provision_neon_db", flaky)
        res = await hosted.provision_with_retry(
            "t1", db, attempts=3, base_delay=0.5, _sleep=fake_sleep
        )
        return res, calls["n"], delays

    res, n, delays = asyncio.run(_run())
    assert res == {"ok": True}
    assert n == 3
    assert delays == [0.5, 1.0]  # exponential: 0.5*2^0, 0.5*2^1


def test_provision_with_retry_exhausts_and_enqueues(monkeypatch):
    async def _run():
        db = await db_module.init_db(":memory:")

        async def always_fail(tid, d):
            raise RuntimeError("neon down")

        async def fake_sleep(s):
            return None

        monkeypatch.setattr(hosted, "provision_neon_db", always_fail)
        raised = None
        try:
            await hosted.provision_with_retry("t9", db, attempts=2, _sleep=fake_sleep)
        except RuntimeError as e:
            raised = str(e)
        return raised, await db_module.get_pending_provisions(db)

    raised, pend = asyncio.run(_run())
    assert raised == "neon down"
    assert len(pend) == 1 and pend[0]["tenant_id"] == "t9"
    assert "neon down" in (pend[0]["last_error"] or "")


# ---------------------------------------------------------------------------
# /health/deep
# ---------------------------------------------------------------------------

def test_health_deep_endpoint(monkeypatch, tmp_path):
    with _hosted_client(monkeypatch, tmp_path) as c:
        r = c.get("/health/deep")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["db"] is True
        assert "pending_provisions" in body
        assert "version" in body
