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
import json
import types
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


# ---------------------------------------------------------------------------
# PROFILE-6 (89a06e40) — profile_binding: workspace-only (hosted_default +
# workspace layers, no project/session layers) compact profile identity/
# generation attached to the tunnel/connector-refresh contract. See pinned
# decision ee7bccc9 (project 5787cc92-ba7d-4788-b17c-28ab7938b839): this
# surface is scoped by tenant_id only, so it deliberately does NOT call
# db.get_effective_profile (which hard-requires a project_id) — it calls
# db.get_workspace_effective_profile instead (see tests/test_profile_layers.py
# for that function's own coverage). This item's declared touches_resources
# named tests/test_tunnel_client.py for this half, but that file covers only
# the LOCAL tunnel *client* (meridian/tunnel_client.py), not these
# server-side HTTP routes — this file is the actual existing test file for
# routes/tunnel.py's HTTP routes (see its own module docstring), so these
# tests live here instead.
# ---------------------------------------------------------------------------

_PROFILE_BINDING_KEYS = {
    "generation_key", "executable", "degraded", "restart_required", "restart_report",
}


def test_get_tunnel_plugins_includes_profile_binding(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        raw_token = _run(_new_tenant_token(client.app.state.db, "profile-binding-plugins@example.com"))
        r = client.get("/tunnel/plugins", headers={"Authorization": f"Bearer {raw_token}"})
        assert r.status_code == 200
        binding = r.json()["profile_binding"]
        assert binding is not None
        assert set(binding.keys()) == _PROFILE_BINDING_KEYS
        assert binding["executable"] is True


def test_tunnel_status_includes_profile_binding(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        raw_token = _run(_new_tenant_token(client.app.state.db, "profile-binding-status@example.com"))
        hdr = {"Authorization": f"Bearer {raw_token}"}
        me = client.get("/me", headers=hdr)
        tenant_id = me.json()["tenant_id"]

        status = client.get(f"/tunnel/status/{tenant_id}")
        assert status.status_code == 200
        binding = status.json()["profile_binding"]
        assert binding is not None
        assert set(binding.keys()) == _PROFILE_BINDING_KEYS


def test_tunnel_status_profile_binding_reflects_configured_workspace_layer(monkeypatch, tmp_path):
    with _make_hosted_client(monkeypatch, tmp_path) as client:
        raw_token = _run(_new_tenant_token(client.app.state.db, "profile-binding-configured@example.com"))
        hdr = {"Authorization": f"Bearer {raw_token}"}
        me = client.get("/me", headers=hdr)
        tenant_id = me.json()["tenant_id"]

        before = client.get(f"/tunnel/status/{tenant_id}").json()["profile_binding"]

        from meridian import db as db_module
        _run(db_module.set_profile_layer(
            client.app.state.db, "workspace", "singleton", fields={"auto_worktrees": 0},
        ))

        after = client.get(f"/tunnel/status/{tenant_id}").json()["profile_binding"]
        assert before["generation_key"] != after["generation_key"]


def test_tunnel_status_direct_call_without_request_degrades_profile_binding_to_none():
    """Backward compat: existing direct-call tests (test_slot_reprobe.py,
    test_tunnel_bridge.py, test_w5_9665538a_meridian_docs_slot.py) invoke
    tunnel_status(tid) as a plain coroutine with no Request object. That must
    keep working — profile_binding degrades to None rather than raising."""
    result = asyncio.run(tn.tunnel_status("direct-call-no-request"))
    assert result["profile_binding"] is None
    assert result["tenant_id"] == "direct-call-no-request"


# ---------------------------------------------------------------------------
# Generation-aware tools/list manifest — sprint item 49d8244d
#
# Builds on the config-generation registry above (02dbd8b4). Covers:
#   * ``_invalidate_tunnel_manifest`` — atomic routing-cache + manifest-
#     timestamp invalidation, the single chokepoint every reconnect/recovery
#     path now uses.
#   * ``_tunnel_manifest_hash`` / ``tunnel_manifest_snapshot`` — deterministic
#     content hash + combined generation/health/freshness snapshot.
#   * ``refresh_tunnel_manifest`` — the explicit refresh/re-list operation,
#     and its two HTTP surfaces: ``POST /tunnel/refresh`` (forces a rebuild)
#     and ``GET /tunnel/manifest`` (read-only).
#   * ``list_tunnel_tools`` stamping ``_tunnel_manifest_generated_at`` so
#     ``age_seconds`` reflects real cache staleness.
#
# Pure in-process unit tests — no real WebSocket, no network, no sleeps
# (clock is monkeypatched where "age" needs to move).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_manifest_state():
    """Reset the manifest-specific registries this section exercises."""
    def _reset():
        tn._tunnel_code_sockets.clear()
        tn._slot_health.clear()
        tn._slot_status_detail.clear()
        tn._tunnel_tool_routes.clear()
        tn._tunnel_manifest_generated_at.clear()
        tn._tools_list_changed_pending.clear()
    _reset()
    yield
    _reset()


def _fake_fetch_slot_tools(per_slot: dict[str, list[dict]]):
    """Build a fake ``_fetch_slot_tools(tenant_id, label)`` returning canned tools."""
    async def _fetch(tenant_id: str, label: str):
        return label, list(per_slot.get(label, []))
    return _fetch


# ---------------------------------------------------------------------------
# _invalidate_tunnel_manifest — atomic cache invalidation
# ---------------------------------------------------------------------------

def test_invalidate_tunnel_manifest_clears_both_caches():
    tid = _tid()
    tn._tunnel_tool_routes[tid] = {"filesystem__read_file": "fs"}
    tn._tunnel_manifest_generated_at[tid] = 12345.0

    tn._invalidate_tunnel_manifest(tid)

    assert tid not in tn._tunnel_tool_routes
    assert tid not in tn._tunnel_manifest_generated_at


def test_invalidate_tunnel_manifest_noop_when_nothing_cached():
    """Invalidating a tenant with no cache entries must not raise."""
    tn._invalidate_tunnel_manifest(_tid())


def test_notify_tools_list_changed_invalidates_atomically():
    """54ddd609's recovery signal now routes through the atomic helper (49d8244d)."""
    tid = _tid()
    tn._tunnel_tool_routes[tid] = {"filesystem__read_file": "fs"}
    tn._tunnel_manifest_generated_at[tid] = 999.0

    tn.notify_tools_list_changed(tid)

    assert tid not in tn._tunnel_tool_routes
    assert tid not in tn._tunnel_manifest_generated_at
    assert tn.consume_tools_list_changed(tid) is True


# ---------------------------------------------------------------------------
# _tunnel_manifest_hash — deterministic, order-independent
# ---------------------------------------------------------------------------

def test_manifest_hash_none_when_no_cache():
    assert tn._tunnel_manifest_hash(_tid()) is None


def test_manifest_hash_deterministic_and_order_independent():
    tid = _tid()
    tn._tunnel_tool_routes[tid] = {"b__tool": "code", "a__tool": "fs"}
    h1 = tn._tunnel_manifest_hash(tid)

    tn._tunnel_tool_routes[tid] = {"a__tool": "fs", "b__tool": "code"}
    h2 = tn._tunnel_manifest_hash(tid)

    assert h1 == h2
    assert len(h1) == 64  # sha256 hex digest


def test_manifest_hash_changes_when_routes_change():
    tid = _tid()
    tn._tunnel_tool_routes[tid] = {"a__tool": "fs"}
    h1 = tn._tunnel_manifest_hash(tid)

    tn._tunnel_tool_routes[tid] = {"a__tool": "fs", "b__tool": "code"}
    h2 = tn._tunnel_manifest_hash(tid)

    assert h1 != h2


def test_manifest_hash_empty_dict_is_not_none():
    """An explicitly-empty (but present) route table is a REAL empty manifest,
    distinct from 'no manifest built yet' (None)."""
    tid = _tid()
    tn._tunnel_tool_routes[tid] = {}
    assert tn._tunnel_manifest_hash(tid) is not None


# ---------------------------------------------------------------------------
# tunnel_manifest_snapshot — combined shape
# ---------------------------------------------------------------------------

def test_snapshot_shape_when_no_tunnel():
    snap = tn.tunnel_manifest_snapshot(_tid())
    assert snap["manifest_hash"] is None
    assert snap["tool_count"] == 0
    assert snap["slot_health"] == {}
    assert snap["generated_at"] is None
    assert snap["age_seconds"] is None
    assert snap["list_changed_pending"] is False
    assert snap["has_active_tunnel"] is False
    assert isinstance(snap["config_generation"], dict)


def test_snapshot_reflects_live_slot_health_without_a_stamped_timestamp():
    """slot_health is always LIVE — present even with no manifest generation yet."""
    tid = _tid()
    tn._record_slot_health(tid, "fs", False, reason="boom")
    snap = tn.tunnel_manifest_snapshot(tid)
    assert snap["slot_health"] == {"fs": False}
    # No routing-cache build has happened — freshness fields stay null.
    assert snap["generated_at"] is None
    assert snap["age_seconds"] is None


def test_snapshot_age_seconds_grows_with_clock(monkeypatch):
    tid = _tid()
    fake_now = [1_000_000.0]
    monkeypatch.setattr(tn.time, "time", lambda: fake_now[0])

    tn._tunnel_tool_routes[tid] = {"a__tool": "fs"}
    tn._tunnel_manifest_generated_at[tid] = fake_now[0]

    snap0 = tn.tunnel_manifest_snapshot(tid)
    assert snap0["age_seconds"] == 0

    fake_now[0] += 42.5
    snap1 = tn.tunnel_manifest_snapshot(tid)
    assert snap1["age_seconds"] == pytest.approx(42.5)
    # generated_at itself does not move — only age relative to "now" does.
    assert snap1["generated_at"] == snap0["generated_at"]


# ---------------------------------------------------------------------------
# list_tunnel_tools — stamps generated_at, handles partial-slot aggregation
# ---------------------------------------------------------------------------

def test_list_tunnel_tools_stamps_generated_at(monkeypatch):
    tid = _tid()
    monkeypatch.setattr(
        tn, "_fetch_slot_tools",
        _fake_fetch_slot_tools({"fs": [{"name": "read_file", "description": "d"}]}),
    )
    assert tid not in tn._tunnel_manifest_generated_at

    asyncio.run(tn.list_tunnel_tools(tid))

    assert tid in tn._tunnel_manifest_generated_at
    snap = tn.tunnel_manifest_snapshot(tid)
    assert snap["manifest_hash"] is not None
    assert snap["tool_count"] == 1
    assert snap["age_seconds"] == pytest.approx(0, abs=1.0)


def test_list_tunnel_tools_partial_slot_aggregation(monkeypatch):
    """One healthy slot returns tools, another returns none — both still
    aggregate correctly and the manifest reflects only the real tools."""
    tid = _tid()
    monkeypatch.setattr(
        tn, "_fetch_slot_tools",
        _fake_fetch_slot_tools({
            "fs": [{"name": "read_file", "description": "d"}],
            "code": [],  # slot healthy but advertises nothing this pass
        }),
    )
    tools = asyncio.run(tn.list_tunnel_tools(tid))
    names = {t["name"] for t in tools}
    assert names == {"filesystem__read_file"}
    snap = tn.tunnel_manifest_snapshot(tid)
    assert snap["tool_count"] == 1


def test_list_tunnel_tools_partial_slot_skips_unhealthy(monkeypatch):
    """An unhealthy slot is excluded from aggregation entirely (d71ba2e7),
    and never even gets fetched (let alone counted in the manifest)."""
    tid = _tid()
    tn._record_slot_health(tid, "code", False, reason="down")
    fetch_calls: list[str] = []

    async def _fetch(tenant_id, label):
        fetch_calls.append(label)
        if label == "code":
            return label, [{"name": "trace_path", "description": "d"}]
        return label, []

    monkeypatch.setattr(tn, "_fetch_slot_tools", _fetch)
    asyncio.run(tn.list_tunnel_tools(tid))
    assert "code" not in fetch_calls  # never fetched — filtered before the fan-out


def test_list_tunnel_tools_zero_tools_active_tunnel_still_stamps_timestamp(monkeypatch):
    """Active tunnel, but every slot returned nothing this pass — the existing
    routing cache (if any) is preserved (existing behavior) while the manifest
    timestamp is still refreshed so age_seconds reflects a real, recent attempt."""
    tid = _tid()
    tn._tunnel_sockets[tid] = object()  # has_active_tunnel() -> True
    tn._tunnel_tool_routes[tid] = {"stale__tool": "fs"}  # pre-existing cache
    monkeypatch.setattr(tn, "_fetch_slot_tools", _fake_fetch_slot_tools({}))

    asyncio.run(tn.list_tunnel_tools(tid))

    # Old routing cache untouched (existing semantics — call_tunnel_tool can
    # still route by it) ...
    assert tn._tunnel_tool_routes.get(tid) == {"stale__tool": "fs"}
    # ...but the manifest timestamp WAS refreshed (49d8244d).
    assert tid in tn._tunnel_manifest_generated_at


# ---------------------------------------------------------------------------
# refresh_tunnel_manifest — the explicit refresh/re-list operation
# ---------------------------------------------------------------------------

def test_refresh_tunnel_manifest_forces_rebuild(monkeypatch):
    tid = _tid()
    calls = {"n": 0}

    async def _fetch(tenant_id, label):
        calls["n"] += 1
        if label == "fs":
            return label, [{"name": "read_file", "description": "d"}]
        return label, []

    monkeypatch.setattr(tn, "_fetch_slot_tools", _fetch)

    snap = asyncio.run(tn.refresh_tunnel_manifest(tid))
    first_call_count = calls["n"]

    assert snap["tool_count"] == 1
    assert snap["manifest_hash"] is not None
    assert first_call_count == len(tn._TUNNEL_LABELS)

    # Calling again re-fetches EVERY slot again (a real rebuild, not a cache hit).
    snap2 = asyncio.run(tn.refresh_tunnel_manifest(tid))
    assert calls["n"] == first_call_count * 2
    assert snap2["manifest_hash"] == snap["manifest_hash"]


def test_refresh_tunnel_manifest_invalidates_before_rebuilding(monkeypatch):
    """A stale cache from a DIFFERENT (now-gone) tool set must never leak into
    the refreshed snapshot even transiently."""
    tid = _tid()
    tn._tunnel_tool_routes[tid] = {"removed__tool": "fs"}
    tn._tunnel_manifest_generated_at[tid] = 1.0

    monkeypatch.setattr(
        tn, "_fetch_slot_tools",
        _fake_fetch_slot_tools({"fs": [{"name": "new_tool", "description": "d"}]}),
    )
    snap = asyncio.run(tn.refresh_tunnel_manifest(tid))
    assert tn._tunnel_tool_routes[tid] == {"filesystem__new_tool": "fs"}
    assert "removed__tool" not in tn._tunnel_tool_routes[tid]
    assert snap["tool_count"] == 1


def test_refresh_tunnel_manifest_no_tunnel_returns_empty_snapshot(monkeypatch):
    tid = _tid()
    monkeypatch.setattr(tn, "_fetch_slot_tools", _fake_fetch_slot_tools({}))
    snap = asyncio.run(tn.refresh_tunnel_manifest(tid))
    assert snap["has_active_tunnel"] is False
    assert snap["manifest_hash"] is None
    assert snap["tool_count"] == 0


# ---------------------------------------------------------------------------
# Reconnect — a fresh connect invalidates the manifest (simulated via the same
# atomic helper the real WS handlers call on connect/disconnect).
# ---------------------------------------------------------------------------

def test_reconnect_invalidates_stale_manifest(monkeypatch):
    tid = _tid()
    monkeypatch.setattr(
        tn, "_fetch_slot_tools",
        _fake_fetch_slot_tools({"fs": [{"name": "read_file", "description": "d"}]}),
    )
    asyncio.run(tn.list_tunnel_tools(tid))
    assert tn.tunnel_manifest_snapshot(tid)["manifest_hash"] is not None

    # Simulate a (re)connect — every real WS handler does exactly this on
    # connect (see tunnel_ws / _serve_tunnel_ws).
    tn._invalidate_tunnel_manifest(tid)

    snap = tn.tunnel_manifest_snapshot(tid)
    assert snap["manifest_hash"] is None
    assert snap["generated_at"] is None

    # The very next aggregation (what a real tools/list triggers) rebuilds it.
    asyncio.run(tn.list_tunnel_tools(tid))
    assert tn.tunnel_manifest_snapshot(tid)["manifest_hash"] is not None


# ---------------------------------------------------------------------------
# Concurrent tools/list — two overlapping aggregations for the same tenant
# must not corrupt shared state.
# ---------------------------------------------------------------------------

def test_concurrent_list_tunnel_tools_stays_consistent(monkeypatch):
    tid = _tid()

    async def _fetch(tenant_id, label):
        await asyncio.sleep(0)  # yield, encouraging interleaving
        if label == "fs":
            return label, [{"name": "read_file", "description": "d"}]
        return label, []

    monkeypatch.setattr(tn, "_fetch_slot_tools", _fetch)

    async def _run_concurrent():
        return await asyncio.gather(
            tn.list_tunnel_tools(tid),
            tn.list_tunnel_tools(tid),
        )

    results = asyncio.run(_run_concurrent())
    for tools in results:
        assert {t["name"] for t in tools} == {"filesystem__read_file"}
    # Final shared state is the (identical, deterministic) aggregation from
    # whichever call wrote last — never a half-updated/mixed table.
    assert tn._tunnel_tool_routes[tid] == {"filesystem__read_file": "fs"}
    assert tn.tunnel_manifest_snapshot(tid)["manifest_hash"] is not None


# ---------------------------------------------------------------------------
# HTTP routes — POST /tunnel/refresh and GET /tunnel/manifest
# ---------------------------------------------------------------------------

class _FakeManifestRequest:
    """Minimal Starlette-Request stand-in (mirrors _FakeActiveRepoReq in
    test_tunnel_bridge.py)."""

    def __init__(self, token="sk_meridian_manifest_token"):
        self.headers = {"authorization": f"Bearer {token}"} if token else {}
        self.query_params = {}
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(db=None))


def _patch_manifest_auth(monkeypatch, tenant_id=None, plan="pro"):
    tenant_id = tenant_id or _tid()
    monkeypatch.setattr(tn, "_hosted_mode", lambda: True)

    async def fake_resolve(auth_db, token):
        return {"id": tenant_id, "plan": plan} if token else None

    monkeypatch.setattr(tn, "_resolve_tenant_from_token", fake_resolve)
    return tenant_id


def test_refresh_route_requires_hosted_mode(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: False)
    resp = asyncio.run(tn.refresh_tunnel_tools_route(_FakeManifestRequest()))
    assert resp.status_code == 503


def test_refresh_route_requires_auth(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: True)

    async def fake_resolve(auth_db, token):
        return None

    monkeypatch.setattr(tn, "_resolve_tenant_from_token", fake_resolve)
    resp = asyncio.run(tn.refresh_tunnel_tools_route(_FakeManifestRequest()))
    assert resp.status_code == 401


def test_refresh_route_requires_pro_plan(monkeypatch):
    _patch_manifest_auth(monkeypatch, plan="free")
    resp = asyncio.run(tn.refresh_tunnel_tools_route(_FakeManifestRequest()))
    assert resp.status_code == 403


def test_refresh_route_success_no_tunnel(monkeypatch):
    tenant_id = _patch_manifest_auth(monkeypatch)
    monkeypatch.setattr(tn, "_fetch_slot_tools", _fake_fetch_slot_tools({}))

    resp = asyncio.run(tn.refresh_tunnel_tools_route(_FakeManifestRequest()))

    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert body["tenant_id"] == tenant_id
    assert body["has_active_tunnel"] is False
    assert body["manifest_hash"] is None


def test_refresh_route_success_with_tunnel(monkeypatch):
    tenant_id = _patch_manifest_auth(monkeypatch)
    tn._tunnel_sockets[tenant_id] = object()
    monkeypatch.setattr(
        tn, "_fetch_slot_tools",
        _fake_fetch_slot_tools({"fs": [{"name": "read_file", "description": "d"}]}),
    )

    resp = asyncio.run(tn.refresh_tunnel_tools_route(_FakeManifestRequest()))

    body = json.loads(resp.body)
    assert resp.status_code == 200
    assert body["tool_count"] == 1
    assert body["manifest_hash"] is not None
    assert body["has_active_tunnel"] is True


def test_manifest_route_is_read_only(monkeypatch):
    """GET /tunnel/manifest never triggers a slot fetch — only reports whatever
    was last built (distinguishing it from POST /tunnel/refresh)."""
    tenant_id = _patch_manifest_auth(monkeypatch)
    fetch_called = {"n": 0}

    async def _fetch(tenant_id_, label):
        fetch_called["n"] += 1
        return label, []

    monkeypatch.setattr(tn, "_fetch_slot_tools", _fetch)

    resp = asyncio.run(tn.get_tunnel_manifest_route(_FakeManifestRequest()))

    assert resp.status_code == 200
    assert fetch_called["n"] == 0

    body = json.loads(resp.body)
    assert body["manifest_hash"] is None
    assert body["tenant_id"] == tenant_id


def test_manifest_route_reports_previously_built_manifest(monkeypatch):
    tenant_id = _patch_manifest_auth(monkeypatch)
    tn._tunnel_tool_routes[tenant_id] = {"filesystem__read_file": "fs"}
    tn._tunnel_manifest_generated_at[tenant_id] = tn.time.time()

    resp = asyncio.run(tn.get_tunnel_manifest_route(_FakeManifestRequest()))
    body = json.loads(resp.body)
    assert body["tool_count"] == 1
    assert body["manifest_hash"] == tn._tunnel_manifest_hash(tenant_id)


def test_manifest_route_requires_hosted_mode(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: False)
    resp = asyncio.run(tn.get_tunnel_manifest_route(_FakeManifestRequest()))
    assert resp.status_code == 503


def test_manifest_route_requires_auth(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: True)

    async def fake_resolve(auth_db, token):
        return None

    monkeypatch.setattr(tn, "_resolve_tenant_from_token", fake_resolve)
    resp = asyncio.run(tn.get_tunnel_manifest_route(_FakeManifestRequest()))
    assert resp.status_code == 401
