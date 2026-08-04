"""Tests for the runtime configuration generation feature (meridian/routes/tunnel.py).

Sprint item 02dbd8b4: a monotonically increasing project/tenant-scoped runtime
configuration generation, with hot-reload-vs-restart-required detection for
tunnel/executor settings changes.

Two layers are covered:

  * Direct unit tests of the module-level generation registry (bump/get/all +
    the in-flight counters) against ``meridian.routes.tunnel`` — fast, and lets
    concurrency/isolation/staleness scenarios be driven precisely.
  * End-to-end tests of the actual HTTP endpoints (GET/PUT /tunnel/plugins,
    POST/DELETE /tunnel/plugins/custom, GET /tunnel/status) via a real hosted
    FastAPI TestClient, confirming the generation record is genuinely wired
    into what a dashboard/tunnel client would receive over the wire.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian.routes import tunnel as tn
from meridian import tunnel_plugins as tp


def _tid() -> str:
    """A fresh, guaranteed-unique tenant id per test (avoids cross-test bleed
    through the module-level dicts without needing to reset global state)."""
    return f"t-{uuid.uuid4().hex}"


@pytest.fixture(autouse=True)
def _clean_generation_state():
    """Reset the runtime-config-generation + in-flight registries and any
    tunnel sockets a test pokes directly, so a crashed/aborted test can't leak
    state into the next one."""
    def _reset():
        tn._config_generations.clear()
        tn._config_generation_locks.clear()
        tn._inflight_count.clear()
        tn._tunnel_sockets.clear()
    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# config_fingerprint (meridian/tunnel_plugins.py) — the hash half of the
# generation record. Exercised here (not test_tunnel_plugins.py) since it is
# only actually consumed by the routes/tunnel.py generation machinery this
# file otherwise covers.
# ---------------------------------------------------------------------------

def test_config_fingerprint_identical_for_equivalent_shapes():
    """List-form vs dict-form input that normalize to the same config must
    fingerprint identically — the whole point of hashing the NORMALIZED form."""
    list_form = [{"name": "code-intel", "command": "codegraph --stdio", "enabled": True}]
    dict_form = {"code-intel": {"command": "codegraph --stdio", "enabled": True}}
    assert tp.config_fingerprint(list_form) == tp.config_fingerprint(dict_form)


def test_config_fingerprint_differs_for_different_config():
    a = tp.config_fingerprint([{"name": "code-intel", "command": "codegraph --stdio"}])
    b = tp.config_fingerprint([{"name": "code-intel", "command": "other --stdio"}])
    assert a != b


def test_config_fingerprint_stable_and_deterministic():
    raw = [{"name": "fs", "port": 8808}]
    assert tp.config_fingerprint(raw) == tp.config_fingerprint(raw)


def test_config_fingerprint_empty_config_is_consistent():
    assert tp.config_fingerprint(None) == tp.config_fingerprint({}) == tp.config_fingerprint([])


# ---------------------------------------------------------------------------
# get_runtime_config_generation — read-only, seeds generation 1 on first read
# ---------------------------------------------------------------------------

def test_get_runtime_config_generation_seeds_generation_one():
    tid = _tid()
    record = tn.get_runtime_config_generation(tid, None, {})
    assert record["generation"] == 1
    assert record["restart_required"] is False
    assert isinstance(record["config_hash"], str) and record["config_hash"]
    assert isinstance(record["source_timestamp"], float)


def test_get_runtime_config_generation_does_not_reseed_on_second_read():
    tid = _tid()
    first = tn.get_runtime_config_generation(tid, None, {})
    second = tn.get_runtime_config_generation(tid, None, {})
    assert first == second
    assert second["generation"] == 1  # not bumped by reading again


def test_get_runtime_config_generation_never_sets_restart_required():
    """A read is pure discovery — it must never claim a restart is needed,
    even if (hypothetically) a tunnel happens to be connected right now."""
    tid = _tid()
    tn._tunnel_sockets[tid] = object()
    record = tn.get_runtime_config_generation(tid, None, {})
    assert record["restart_required"] is False


# ---------------------------------------------------------------------------
# bump_runtime_config_generation — the write path
# ---------------------------------------------------------------------------

def test_bump_advances_generation_on_real_change():
    tid = _tid()
    first = asyncio.run(tn.bump_runtime_config_generation(tid, None, {"fs": {"port": 8808}}))
    assert first["generation"] == 1
    second = asyncio.run(tn.bump_runtime_config_generation(tid, None, {"fs": {"port": 9999}}))
    assert second["generation"] == 2
    assert second["config_hash"] != first["config_hash"]


def test_bump_is_a_noop_for_identical_config():
    """Resaving the exact same effective config must not manufacture a new
    generation or flip restart_required — 02dbd8b4's explicit requirement."""
    tid = _tid()
    cfg = {"fs": {"port": 8808}}
    first = asyncio.run(tn.bump_runtime_config_generation(tid, None, cfg))
    second = asyncio.run(tn.bump_runtime_config_generation(tid, None, dict(cfg)))
    assert second == first
    assert second["generation"] == 1


def test_bump_restart_required_true_when_tunnel_actively_connected():
    """Do not silently restart a tunnel while it is actively serving requests:
    a config change while a tunnel is connected must come back
    restart_required=True, never silently 'applied'."""
    tid = _tid()
    tn._tunnel_sockets[tid] = object()  # simulate a live tunnel connection
    record = asyncio.run(tn.bump_runtime_config_generation(tid, None, {"fs": {"port": 8808}}))
    assert record["restart_required"] is True


def test_bump_restart_required_false_when_no_active_tunnel():
    tid = _tid()
    assert tid not in tn._tunnel_sockets
    record = asyncio.run(tn.bump_runtime_config_generation(tid, None, {"fs": {"port": 8808}}))
    assert record["restart_required"] is False


def test_bump_stale_process_signal_persists_until_a_fresh_write():
    """'Stale process' scenario: a config write happens while a process is
    connected (restart_required flips True); a plain read afterwards must
    keep reporting that same stale signal rather than silently clearing it
    (only a NEW write recomputes restart_required — see the module docstring
    note in routes/tunnel.py for why this doesn't auto-clear on disconnect)."""
    tid = _tid()
    tn._tunnel_sockets[tid] = object()
    written = asyncio.run(tn.bump_runtime_config_generation(tid, None, {"fs": {"port": 8808}}))
    assert written["restart_required"] is True

    # The process disconnects (e.g. crashed, or the user is about to restart
    # it) — a read-only status poll must still show the SAME stale record,
    # not silently reset to "current".
    tn._tunnel_sockets.pop(tid, None)
    polled = tn.get_runtime_config_generation(tid, None, {"fs": {"port": 8808}})
    assert polled["restart_required"] is True
    assert polled["generation"] == written["generation"]

    # Only a fresh write (the operator actually restarting) recomputes the
    # signal — now correctly False since nothing is connected at write time.
    fresh = asyncio.run(tn.bump_runtime_config_generation(tid, None, {"fs": {"port": 7777}}))
    assert fresh["restart_required"] is False
    assert fresh["generation"] == written["generation"] + 1


def test_bump_reconnect_after_no_op_save_keeps_generation_stable():
    """Reconnect scenario: a tunnel connects, an identical (no-op) settings
    save happens while it's connected — must NOT flip restart_required, since
    nothing actually changed for the running process to be stale against."""
    tid = _tid()
    cfg = {"fs": {"port": 8808}}
    baseline = asyncio.run(tn.bump_runtime_config_generation(tid, None, cfg))
    assert baseline["restart_required"] is False

    tn._tunnel_sockets[tid] = object()  # tunnel connects, loading generation 1
    resaved = asyncio.run(tn.bump_runtime_config_generation(tid, None, dict(cfg)))
    assert resaved["generation"] == baseline["generation"]
    assert resaved["restart_required"] is False


def test_bump_project_tenant_isolation():
    """Two different tenants' generations must never interact."""
    tid_a, tid_b = _tid(), _tid()
    asyncio.run(tn.bump_runtime_config_generation(tid_a, None, {"fs": {"port": 1}}))
    asyncio.run(tn.bump_runtime_config_generation(tid_a, None, {"fs": {"port": 2}}))
    asyncio.run(tn.bump_runtime_config_generation(tid_b, None, {"fs": {"port": 3}}))

    rec_a = tn.get_runtime_config_generation(tid_a, None, {"fs": {"port": 2}})
    rec_b = tn.get_runtime_config_generation(tid_b, None, {"fs": {"port": 3}})
    assert rec_a["generation"] == 2
    assert rec_b["generation"] == 1
    assert rec_a["config_hash"] != rec_b["config_hash"]


def test_bump_hostname_scoped_isolation_within_one_tenant():
    """Per-machine (?hostname=) config changes must not bump a DIFFERENT
    machine's (or the tenant-default's) generation counter for the same tenant."""
    tid = _tid()
    default_rec = asyncio.run(tn.bump_runtime_config_generation(tid, None, {"fs": {"port": 1}}))
    host_a_rec = asyncio.run(tn.bump_runtime_config_generation(tid, "laptop-a", {"fs": {"port": 2}}))
    host_a_rec2 = asyncio.run(tn.bump_runtime_config_generation(tid, "laptop-a", {"fs": {"port": 3}}))

    assert default_rec["generation"] == 1
    assert host_a_rec["generation"] == 1  # independent counter, not 2
    assert host_a_rec2["generation"] == 2

    # The default scope is untouched by laptop-a's writes.
    assert tn.get_runtime_config_generation(tid, None, {"fs": {"port": 1}})["generation"] == 1


def test_bump_concurrent_updates_never_duplicate_or_lose_a_generation():
    """Two overlapping writes for the same tenant must serialize cleanly: the
    final generation is exactly 2, and no generation number is assigned twice."""
    tid = _tid()

    async def _drive():
        return await asyncio.gather(
            tn.bump_runtime_config_generation(tid, None, {"fs": {"port": 111}}),
            tn.bump_runtime_config_generation(tid, None, {"fs": {"port": 222}}),
        )

    results = asyncio.run(_drive())
    generations = sorted(r["generation"] for r in results)
    assert generations == [1, 2]
    final = tn.get_runtime_config_generation(tid, None, {"fs": {"port": 222}})
    assert final["generation"] == 2


# ---------------------------------------------------------------------------
# all_runtime_config_generations / in-flight tracking
# ---------------------------------------------------------------------------

def test_all_runtime_config_generations_uses_default_key_and_only_known_hosts():
    tid = _tid()
    asyncio.run(tn.bump_runtime_config_generation(tid, None, {"fs": {"port": 1}}))
    asyncio.run(tn.bump_runtime_config_generation(tid, "desktop-2", {"fs": {"port": 2}}))

    all_gens = tn.all_runtime_config_generations(tid)
    assert set(all_gens) == {"default", "desktop-2"}
    assert all_gens["default"]["generation"] == 1
    assert all_gens["desktop-2"]["generation"] == 1


def test_all_runtime_config_generations_empty_for_unknown_tenant():
    assert tn.all_runtime_config_generations(_tid()) == {}


def test_mark_inflight_increments_and_prunes_to_empty():
    tid = _tid()
    tn._mark_inflight(tid, "fs", 1)
    tn._mark_inflight(tid, "fs", 1)
    assert tn.tenant_inflight_counts(tid) == {"fs": 2}
    tn._mark_inflight(tid, "fs", -1)
    assert tn.tenant_inflight_counts(tid) == {"fs": 1}
    tn._mark_inflight(tid, "fs", -1)
    # Fully drained → pruned away entirely, not left as {"fs": 0}.
    assert tn.tenant_inflight_counts(tid) == {}
    assert tid not in tn._inflight_count


def test_mark_inflight_isolated_per_slot():
    tid = _tid()
    tn._mark_inflight(tid, "fs", 1)
    tn._mark_inflight(tid, "code", 1)
    tn._mark_inflight(tid, "code", 1)
    counts = tn.tenant_inflight_counts(tid)
    assert counts == {"fs": 1, "code": 2}


# ---------------------------------------------------------------------------
# End-to-end HTTP endpoint tests (real hosted TestClient)
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_hosted_client(monkeypatch, tmp_path):
    """Hosted-mode TestClient backed by an in-memory auth DB (mirrors
    test_v2_hosted.py's helper of the same name/shape)."""
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


async def _new_tenant_token(db, email: str) -> str:
    from meridian import db as db_module
    tenant = await db_module.upsert_tenant(db, email)
    raw, _row = await db_module.create_api_token(db, tenant["id"], label="t")
    return raw


def test_get_tunnel_plugins_reports_config_generation(monkeypatch, tmp_path):
    """A fresh tenant's first GET /tunnel/plugins seeds and reports generation 1."""
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        raw_token = _run(_new_tenant_token(client.app.state.db, "cg-get@example.com"))
        r = client.get("/tunnel/plugins", headers={"Authorization": f"Bearer {raw_token}"})
        assert r.status_code == 200
        gen = r.json()["config_generation"]
        assert gen["generation"] == 1
        assert gen["restart_required"] is False
        assert isinstance(gen["config_hash"], str) and gen["config_hash"]


def test_put_tunnel_plugins_bumps_generation_and_reports_it(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        raw_token = _run(_new_tenant_token(client.app.state.db, "cg-put@example.com"))
        hdr = {"Authorization": f"Bearer {raw_token}"}

        first = client.put("/tunnel/plugins", headers=hdr,
                            json={"config": [{"name": "code-intel", "command": "codegraph --stdio"}]})
        assert first.status_code == 200
        gen1 = first.json()["config_generation"]
        assert gen1["generation"] == 1
        assert gen1["restart_required"] is False

        # A genuinely different config bumps the generation again.
        second = client.put("/tunnel/plugins", headers=hdr,
                             json={"config": [{"name": "code-intel", "command": "other --stdio"}]})
        gen2 = second.json()["config_generation"]
        assert gen2["generation"] == 2
        assert gen2["config_hash"] != gen1["config_hash"]

        # Resaving the SAME config again is a no-op: generation unchanged.
        third = client.put("/tunnel/plugins", headers=hdr,
                            json={"config": [{"name": "code-intel", "command": "other --stdio"}]})
        gen3 = third.json()["config_generation"]
        assert gen3["generation"] == 2


def test_put_tunnel_plugins_restart_required_while_tunnel_connected(monkeypatch, tmp_path):
    """A settings change made while a tunnel is actively connected must come
    back restart_required=True — never silently treated as already-applied."""
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        from meridian import db as db_module
        tenant = _run(db_module.upsert_tenant(client.app.state.db, "cg-restart@example.com"))
        raw_token, _row = _run(db_module.create_api_token(client.app.state.db, tenant["id"], label="t"))
        hdr = {"Authorization": f"Bearer {raw_token}"}

        tn._tunnel_sockets[tenant["id"]] = object()  # simulate a live tunnel
        try:
            r = client.put("/tunnel/plugins", headers=hdr,
                            json={"config": [{"name": "code-intel", "command": "codegraph --stdio"}]})
            assert r.json()["config_generation"]["restart_required"] is True
        finally:
            tn._tunnel_sockets.pop(tenant["id"], None)


def test_custom_plugin_add_and_remove_report_config_generation(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        raw_token = _run(_new_tenant_token(client.app.state.db, "cg-custom@example.com"))
        hdr = {"Authorization": f"Bearer {raw_token}"}

        added = client.post("/tunnel/plugins/custom", headers=hdr,
                             json={"name": "fetch", "command": "uvx mcp-server-fetch"})
        assert added.status_code == 200
        gen_added = added.json()["config_generation"]
        assert gen_added["generation"] == 1

        removed = client.request("DELETE", "/tunnel/plugins/custom?name=fetch", headers=hdr)
        assert removed.status_code == 200
        gen_removed = removed.json()["config_generation"]
        assert gen_removed["generation"] == 2
        assert gen_removed["config_hash"] != gen_added["config_hash"]


def test_tunnel_status_reports_config_generation_and_drain_safety(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        raw_token = _run(_new_tenant_token(client.app.state.db, "cg-status@example.com"))
        hdr = {"Authorization": f"Bearer {raw_token}"}
        put = client.put("/tunnel/plugins", headers=hdr,
                          json={"config": [{"name": "code-intel", "command": "codegraph --stdio"}]})
        tenant_id = None
        # tenant_id is not in the PUT response; fetch it via /me.
        me = client.get("/me", headers=hdr)
        tenant_id = me.json()["tenant_id"]

        status = client.get(f"/tunnel/status/{tenant_id}")
        assert status.status_code == 200
        body = status.json()
        assert body["safe_to_restart"] is True
        assert body["inflight"] == {}
        assert body["config_generation"]["default"]["generation"] == put.json()["config_generation"]["generation"]


def test_tunnel_plugins_config_generation_isolated_per_tenant_over_http(monkeypatch, tmp_path):
    """Two different tenants writing through the real HTTP endpoints must not
    observe or influence each other's generation counter."""
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        token_a = _run(_new_tenant_token(client.app.state.db, "cg-iso-a@example.com"))
        token_b = _run(_new_tenant_token(client.app.state.db, "cg-iso-b@example.com"))
        hdr_a = {"Authorization": f"Bearer {token_a}"}
        hdr_b = {"Authorization": f"Bearer {token_b}"}

        client.put("/tunnel/plugins", headers=hdr_a,
                   json={"config": [{"name": "code-intel", "command": "a --stdio"}]})
        client.put("/tunnel/plugins", headers=hdr_a,
                   json={"config": [{"name": "code-intel", "command": "b --stdio"}]})
        r_b = client.put("/tunnel/plugins", headers=hdr_b,
                          json={"config": [{"name": "code-intel", "command": "c --stdio"}]})

        assert r_b.json()["config_generation"]["generation"] == 1

        r_a_get = client.get("/tunnel/plugins", headers=hdr_a)
        assert r_a_get.json()["config_generation"]["generation"] == 2
