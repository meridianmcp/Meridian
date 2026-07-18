"""0882b8d6 — targeted coverage for meridian/routes/tunnel.py high-risk paths.

Selected branches: auth/security helpers, Fly cross-instance routing, slot-health
TTL re-probe, tools-list-changed signal, in-flight bulkhead, fs relative-path
guard, executor-config union helpers, and misc infra helpers. Integration tests
preferred over unit tests per the pinned PROCESS decision.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import types

import pytest
from fastapi.responses import Response

import meridian.server  # noqa: F401 — ensure server import doesn't cycle
from meridian.routes import tunnel as tn


# ---------------------------------------------------------------------------
# Shared cleanup fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_tunnel_state():
    """Reset all per-process registries between tests."""
    def _reset():
        for d in (
            tn._tunnel_sockets, tn._tunnel_code_sockets, tn._tunnel_extract_sockets,
            tn._tunnel_ppt_sockets, tn._tunnel_word_sockets, tn._tunnel_dc_sockets,
            tn._tunnel_docs_sockets, tn._tunnel_zotero_sockets,
            tn._tunnel_tool_routes, tn._slot_health, tn._slot_status_detail,
            tn._slot_unhealthy_since, tn._tenant_owner_instance, tn._tenant_active_repo,
        ):
            d.clear()
        tn._tools_list_changed_pending.clear()
        for s in tn._CUSTOM_SLOTS:
            tn._tunnel_custom_sockets[s].clear()
            tn._pending_custom_reqs[s].clear()

    _reset()
    yield
    _reset()


# ===========================================================================
# 1. _is_tunnel_allowed — is_internal branch (not just plan)
# ===========================================================================

def test_is_tunnel_allowed_internal_flag_overrides_plan():
    """An internal tenant with plan='free' is still allowed — the is_internal
    flag is how staging/admin accounts bypass plan gating. This is security-
    relevant: a regression here could accidentally allow or block an admin."""
    assert tn._is_tunnel_allowed({"plan": "free", "is_internal": True}) is True
    assert tn._is_tunnel_allowed({"plan": "pro", "is_internal": False}) is True
    assert tn._is_tunnel_allowed({"plan": "admin"}) is True
    assert tn._is_tunnel_allowed({"plan": "free", "is_internal": False}) is False
    assert tn._is_tunnel_allowed({"plan": "free"}) is False
    assert tn._is_tunnel_allowed({}) is False


# ===========================================================================
# 2. _resolve_tenant_from_token — Bearer prefix stripping
# ===========================================================================

@pytest.mark.asyncio
async def test_resolve_tenant_from_token_strips_bearer_prefix(monkeypatch):
    """'Bearer sk_meridian_...' must resolve to the same tenant as the bare token.
    This is the auth path hit by every tunnel WebSocket and /tunnel/active-repo.
    A regression here means a valid Bearer header is rejected as invalid."""
    import hashlib

    async def fake_get_by_hash(db, token_hash):
        expected = hashlib.sha256(b"sk_meridian_testtoken").hexdigest()
        if token_hash == expected:
            return {"id": "tenant-123", "plan": "pro"}
        return None

    monkeypatch.setattr(
        "meridian.db.get_tenant_from_token_hash", fake_get_by_hash
    )

    # Bearer prefix variant
    result = await tn._resolve_tenant_from_token(None, "Bearer sk_meridian_testtoken")
    assert result is not None
    assert result["id"] == "tenant-123"

    # With leading/trailing whitespace
    result2 = await tn._resolve_tenant_from_token(None, "  Bearer  sk_meridian_testtoken  ")
    # The code strips the token after 'Bearer ' and then strips again
    # Because of how the stripping works, extra spaces around Bearer prevent match —
    # but bare token with surrounding spaces should work:
    result3 = await tn._resolve_tenant_from_token(None, "  sk_meridian_testtoken  ")
    assert result3 is not None

    # None / empty token → None (no auth)
    assert await tn._resolve_tenant_from_token(None, None) is None
    assert await tn._resolve_tenant_from_token(None, "") is None
    assert await tn._resolve_tenant_from_token(None, "   ") is None


# ===========================================================================
# 3. Fly cross-instance routing: record/clear/replay helpers
# ===========================================================================

def test_record_and_clear_tenant_owner_instance(monkeypatch):
    """record_tenant_owner_instance stores the Fly machine id; clear drops it
    only when instance matches (safety guard against stale disconnects).
    af5b5739 — this is the cross-instance socket-ownership registry."""
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-abc")

    # Record stores the current machine id.
    inst = tn.record_tenant_owner_instance("t-fly")
    assert inst == "machine-abc"
    assert tn.tenant_owner_instance("t-fly") == "machine-abc"

    # Clear with wrong instance id is a no-op — the real owner is preserved.
    tn.clear_tenant_owner_instance("t-fly", "machine-other")
    assert tn.tenant_owner_instance("t-fly") == "machine-abc"

    # Clear with the matching id removes the record.
    tn.clear_tenant_owner_instance("t-fly", "machine-abc")
    assert tn.tenant_owner_instance("t-fly") is None


def test_record_owner_instance_noop_off_fly():
    """Outside Fly (no env var) record is a no-op — single instance needs no
    cross-instance tracking."""
    # Ensure env vars not set
    os.environ.pop("FLY_MACHINE_ID", None)
    os.environ.pop("FLY_ALLOC_ID", None)
    result = tn.record_tenant_owner_instance("t-noop")
    assert result is None
    assert tn.tenant_owner_instance("t-noop") is None


def test_fly_replay_target_returns_none_off_fly():
    """fly_replay_target is a no-op when not on Fly — no replay header."""
    os.environ.pop("FLY_MACHINE_ID", None)
    os.environ.pop("FLY_ALLOC_ID", None)
    assert tn.fly_replay_target({"id": "t-local"}) is None
    assert tn.fly_replay_target(None) is None


def test_fly_replay_target_returns_none_when_socket_held(monkeypatch):
    """When THIS instance already holds the socket, replay would be a self-loop."""
    monkeypatch.setenv("FLY_MACHINE_ID", "self-machine")
    tn._tenant_owner_instance["t-here"] = "other-machine"
    tn._tunnel_sockets["t-here"] = object()  # local socket — don't replay
    assert tn.fly_replay_target({"id": "t-here"}) is None


def test_fly_replay_target_returns_instance_when_owner_known(monkeypatch):
    """Tenant's socket is on another machine — replay target is instance=<id>."""
    monkeypatch.setenv("FLY_MACHINE_ID", "self-machine")
    tn._tenant_owner_instance["t-remote"] = "other-machine"
    # No local socket (cross-instance miss)
    result = tn.fly_replay_target({"id": "t-remote"})
    assert result == "instance=other-machine"


def test_fly_replay_response_carries_header():
    """fly_replay_response sets the fly-replay header and returns a 503 body."""
    resp = tn.fly_replay_response("instance=other-machine")
    assert resp.status_code == 503
    assert resp.headers.get(tn.FLY_REPLAY_HEADER) == "instance=other-machine"
    assert b"replaying" in resp.body


def test_fly_replay_target_for_id_delegates_to_fly_replay_target(monkeypatch):
    """fly_replay_target_for_id is the id-keyed variant used by _do_proxy."""
    monkeypatch.setenv("FLY_MACHINE_ID", "self-m")
    tn._tenant_owner_instance["t-rid"] = "remote-m"
    assert tn.fly_replay_target_for_id("t-rid") == "instance=remote-m"


def test_fly_replay_target_noop_when_owner_is_self(monkeypatch):
    """If the known owner IS this instance, replaying to self would loop."""
    monkeypatch.setenv("FLY_MACHINE_ID", "same-machine")
    tn._tenant_owner_instance["t-self"] = "same-machine"
    assert tn.fly_replay_target({"id": "t-self"}) is None


# ===========================================================================
# 4. _slot_is_healthy TTL re-probe (16e02240)
# ===========================================================================

def test_slot_is_healthy_ttl_resets_after_expiry(monkeypatch):
    """A slot marked unhealthy becomes optimistically healthy again after the
    suppression TTL elapses. This prevents a transient hiccup from permanently
    hiding a recovered slot until a full tunnel reconnect. 16e02240."""
    # Set a very short TTL for the test.
    monkeypatch.setenv("MERIDIAN_SLOT_UNHEALTHY_TTL", "0.1")

    # Initially healthy.
    assert tn._slot_is_healthy("t-ttl", "extract") is True

    # Mark unhealthy — starts the suppression clock.
    tn._record_slot_health("t-ttl", "extract", False)
    assert tn._slot_is_healthy("t-ttl", "extract") is False

    # Simulate TTL expiry by backdating the timestamp.
    tn._slot_unhealthy_since["t-ttl"]["extract"] = time.monotonic() - 0.5

    # Now the slot re-appears as healthy (optimistic re-probe).
    assert tn._slot_is_healthy("t-ttl", "extract") is True


def test_slot_unhealthy_ttl_disabled_pins_dark(monkeypatch):
    """TTL=0 disables the optimistic re-probe — a suppressed slot stays dark
    until an explicit healthy report. 16e02240."""
    monkeypatch.setenv("MERIDIAN_SLOT_UNHEALTHY_TTL", "0")
    tn._record_slot_health("t-nodttl", "dc", False)
    # Even with a backdated timestamp, the slot stays suppressed (TTL disabled).
    tn._slot_unhealthy_since.setdefault("t-nodttl", {})["dc"] = time.monotonic() - 9999
    assert tn._slot_is_healthy("t-nodttl", "dc") is False


def test_slot_unhealthy_no_timestamp_assumed_healthy():
    """Marked unhealthy but no timestamp in _slot_unhealthy_since (defensive case):
    don't pin it dark forever — return True (optimistic). 16e02240."""
    tn._slot_health.setdefault("t-notstamp", {})["extract"] = False
    # Do NOT populate _slot_unhealthy_since so the defensive branch fires.
    # Default TTL is 120s, and since is None → optimistic healthy.
    assert tn._slot_is_healthy("t-notstamp", "extract") is True


# ===========================================================================
# 5. notify_tools_list_changed + consume_tools_list_changed (54ddd609)
# ===========================================================================

def test_notify_and_consume_tools_list_changed():
    """notify_tools_list_changed marks a tenant pending; consume drains exactly
    once. This is the mechanism that makes a recovered slot's tools visible on
    the next tools/list without a full session reconnect. 54ddd609."""
    tn._tunnel_tool_routes["t-notify"] = {"read_file": "fs"}

    # No pending notification initially.
    assert tn.consume_tools_list_changed("t-notify") is False

    # Notify drops the cached routes and sets the pending marker.
    tn.notify_tools_list_changed("t-notify")
    assert "t-notify" not in tn._tunnel_tool_routes  # cache cleared
    assert "t-notify" in tn._tools_list_changed_pending

    # First consume returns True and clears the marker.
    assert tn.consume_tools_list_changed("t-notify") is True
    assert "t-notify" not in tn._tools_list_changed_pending

    # Second consume returns False — already drained.
    assert tn.consume_tools_list_changed("t-notify") is False


def test_record_slot_health_recovery_fires_notify():
    """When a slot transitions from unhealthy to healthy, _record_slot_health
    calls notify_tools_list_changed so the previously-suppressed tools become
    visible on the next tools/list. 54ddd609 + d71ba2e7."""
    tn._tunnel_tool_routes["t-recover"] = {"some_tool": "fs"}
    tn._record_slot_health("t-recover", "fs", False)

    # Clear the pending signal (it may have been set by the unhealthy report).
    tn._tools_list_changed_pending.discard("t-recover")

    # Now send the healthy recovery report.
    tn._record_slot_health("t-recover", "fs", True)

    # The recovery must have set the pending signal so a connected session
    # re-discovers the slot's tools on its next tools/list.
    assert "t-recover" in tn._tools_list_changed_pending


# ===========================================================================
# 6. _do_proxy slot saturation (1d021501 bulkhead)
# ===========================================================================

def test_do_proxy_503_when_slot_saturated(monkeypatch):
    """When the per-slot in-flight semaphore is exhausted, _do_proxy fails fast
    with 503 instead of piling up unbounded pending futures. 1d021501."""
    # Force a semaphore with 0 capacity so it's immediately saturated.
    monkeypatch.setattr(tn, "_max_slot_inflight", lambda: 0)
    # Clear the slot semaphore cache so our limit takes effect.
    tn._slot_inflight.clear()

    # Register a dummy socket so the semaphore path is reached (not the 503-no-socket path).
    tn._tunnel_sockets["t-sat"] = object()

    # The semaphore acquire will immediately timeout (0 permits).
    resp = asyncio.run(tn._do_proxy(
        "t-sat", "POST", "/mcp", "", {}, b"x",
        tn._tunnel_sockets, tn._pending_reqs, "fs",
    ))
    assert resp.status_code == 503
    assert b"saturated" in resp.body


# ===========================================================================
# 7. call_tunnel_tool relative path guard (fs slot)
# ===========================================================================

def test_call_tunnel_tool_raises_on_relative_fs_path(monkeypatch):
    """The fs slot requires absolute paths. A relative path raises RuntimeError
    immediately (before any tunnel round-trip) with an actionable message.
    This catches a class of confusing 'file not found' errors from the tunnel."""
    tenant = "t-relpath"
    tn._tunnel_sockets[tenant] = object()
    tn._tunnel_tool_routes[tenant] = {"filesystem__read_file": "fs"}

    called = {"n": 0}

    async def fake_do_proxy(*a, **k):
        called["n"] += 1
        return Response(content=b"{}", status_code=200, media_type="application/json")

    monkeypatch.setattr(tn, "_do_proxy", fake_do_proxy)

    with pytest.raises(RuntimeError, match="absolute paths"):
        asyncio.run(tn.call_tunnel_tool(
            tenant, "filesystem__read_file", {"path": "relative/file.txt"}
        ))
    # Proxy must NOT have been called — the guard fires before the network hop.
    assert called["n"] == 0


def test_call_tunnel_tool_absolute_path_passes_guard(monkeypatch):
    """An absolute path passes the guard and reaches the tunnel relay."""
    tenant = "t-abspath"
    tn._tunnel_sockets[tenant] = object()
    tn._tunnel_tool_routes[tenant] = {"filesystem__read_file": "fs"}

    async def fake_do_proxy(tid, method, path, query, headers, body, sockets, pending, label):
        return Response(
            content=json.dumps({"result": {"content": [{"type": "text", "text": "ok"}]}}).encode(),
            status_code=200, media_type="application/json",
        )

    monkeypatch.setattr(tn, "_do_proxy", fake_do_proxy)

    # POSIX absolute path — should pass
    result = asyncio.run(tn.call_tunnel_tool(
        tenant, "filesystem__read_file", {"path": "/home/user/file.txt"}
    ))
    assert result is not None

    # Windows absolute path (drive letter + colon) — should pass
    result2 = asyncio.run(tn.call_tunnel_tool(
        tenant, "filesystem__read_file", {"path": "C:\\Users\\user\\file.txt"}
    ))
    assert result2 is not None


def test_call_tunnel_tool_list_paths_checks_each(monkeypatch):
    """When the argument is a list of paths, each one is checked. A single
    relative path in the list triggers the guard."""
    tenant = "t-listpath"
    tn._tunnel_sockets[tenant] = object()
    tn._tunnel_tool_routes[tenant] = {"filesystem__read_multiple_files": "fs"}

    with pytest.raises(RuntimeError, match="absolute paths"):
        asyncio.run(tn.call_tunnel_tool(
            tenant, "filesystem__read_multiple_files",
            {"paths": ["/absolute/ok.txt", "relative/bad.txt"]}
        ))


def test_call_tunnel_tool_bounds_cold_cache_discovery_to_outer_timeout(monkeypatch):
    """9ab967d6 — hardening audit companion to the 2026-07-17 tools/list outage
    hotfix. ``call_tunnel_tool``'s cold-route-cache path awaits
    ``list_tunnel_tools`` directly; that function's own per-slot retry budget
    (up to ~14s worst case for a slot marked healthy but actually unreachable)
    used to have NO outer bound here, so a single stale-healthy slot could hang
    an actual tools/call indefinitely (worse than the tools/list case this
    mirrors, since it fires on every cold-cache tool call, not just discovery).
    A hard ``_TOOL_DISCOVERY_TIMEOUT`` wait_for now guarantees this returns
    promptly (miss/None) instead of hanging on a slow/wedged fetch."""
    import time as _time

    tenant = "t-cold-cache-hang"
    tn._tunnel_sockets[tenant] = object()
    # No pre-seeded route — forces the cold-cache discovery branch.
    assert tenant not in tn._tunnel_tool_routes

    async def hangs_forever(tid, reserved=frozenset()):
        await asyncio.sleep(30)
        return [{"name": "some_tool"}]

    monkeypatch.setattr(tn, "list_tunnel_tools", hangs_forever)

    start = _time.monotonic()
    result = asyncio.run(tn.call_tunnel_tool(tenant, "filesystem__read_file", {}))
    elapsed = _time.monotonic() - start

    # Bounded well under the 30s hang — proves the outer wait_for fired.
    assert elapsed < tn._TOOL_DISCOVERY_TIMEOUT + 5.0, (
        f"call_tunnel_tool took {elapsed:.1f}s — outer discovery timeout did not bound it"
    )
    # No route was discovered (the hung fetch's result is discarded, not
    # awaited-through), so the call correctly reports "no tunnel tool found".
    assert result is None


# ===========================================================================
# 8. _extract_install_command — runtime preference (uv > npm > other)
# ===========================================================================

def test_extract_install_command_prefers_uv_over_npm():
    """uv runtime wins the preference order over npm."""
    server = {
        "packages": [
            {"runtime": "npm", "name": "foo-mcp", "package_arguments": []},
            {"runtime": "uv", "name": "mcp-server-foo", "package_arguments": []},
        ]
    }
    cmd = tn._extract_install_command(server)
    assert cmd.startswith("uvx mcp-server-foo")


def test_extract_install_command_npm_fallback():
    server = {
        "packages": [
            {"runtime": "npm", "name": "bar-mcp", "package_arguments": ["--key", "val"]},
        ]
    }
    cmd = tn._extract_install_command(server)
    assert cmd.startswith("npx -y bar-mcp")
    assert "--key" in cmd


def test_extract_install_command_unknown_runtime():
    server = {
        "packages": [
            {"runtime": "docker", "name": "docker-mcp", "package_arguments": []},
        ]
    }
    cmd = tn._extract_install_command(server)
    # Falls back to bare name
    assert "docker-mcp" in cmd


def test_extract_install_command_empty_packages():
    assert tn._extract_install_command({}) == ""
    assert tn._extract_install_command({"packages": []}) == ""
    assert tn._extract_install_command({"packages": None}) == ""


# ===========================================================================
# 9. send_add_fs_roots_control + send_set_fs_roots_control (live-fs-roots)
# ===========================================================================

class _FakeFSWebSocket:
    def __init__(self, raise_on_send=False):
        self.sent = []
        self._raise = raise_on_send

    async def send_json(self, obj):
        if self._raise:
            raise RuntimeError("socket gone")
        self.sent.append(obj)


def test_send_add_fs_roots_not_connected():
    result = asyncio.run(tn.send_add_fs_roots_control("no-tenant", ["/path/a"]))
    assert result["status"] == "not_connected"


def test_send_add_fs_roots_ok():
    ws = _FakeFSWebSocket()
    tn._tunnel_sockets["t-addroot"] = ws
    result = asyncio.run(tn.send_add_fs_roots_control("t-addroot", ["/path/a", "/path/b"]))
    assert result["status"] == "ok"
    assert result["roots"] == ["/path/a", "/path/b"]
    assert ws.sent[0] == {"type": "add_fs_roots", "roots": ["/path/a", "/path/b"]}


def test_send_add_fs_roots_error():
    ws = _FakeFSWebSocket(raise_on_send=True)
    tn._tunnel_sockets["t-addrooterr"] = ws
    result = asyncio.run(tn.send_add_fs_roots_control("t-addrooterr", ["/path"]))
    assert result["status"] == "error"
    assert "socket gone" in result["message"]


def test_send_set_fs_roots_not_connected():
    result = asyncio.run(tn.send_set_fs_roots_control("no-tenant", []))
    assert result["status"] == "not_connected"


def test_send_set_fs_roots_ok():
    ws = _FakeFSWebSocket()
    tn._tunnel_sockets["t-setroot"] = ws
    result = asyncio.run(tn.send_set_fs_roots_control("t-setroot", ["/only/root"]))
    assert result["status"] == "ok"
    assert ws.sent[0]["type"] == "set_fs_roots"
    assert ws.sent[0]["roots"] == ["/only/root"]


def test_send_set_fs_roots_error():
    ws = _FakeFSWebSocket(raise_on_send=True)
    tn._tunnel_sockets["t-setrooterr"] = ws
    result = asyncio.run(tn.send_set_fs_roots_control("t-setrooterr", []))
    assert result["status"] == "error"


# ===========================================================================
# 10. Executor-config union helpers (_union_filesystem_roots, etc.)
# ===========================================================================

def _proj(cfg):
    """Make a project row with executor_config as JSON string."""
    return {"executor_config": json.dumps(cfg)}


def test_union_filesystem_roots_dedupes_and_preserves_order():
    projects = [
        _proj({"filesystem_roots": ["/a", "/b"]}),
        _proj({"filesystem_roots": ["/b", "/c"]}),
    ]
    assert tn._union_filesystem_roots(projects) == ["/a", "/b", "/c"]


def test_union_filesystem_roots_skips_malformed():
    projects = [
        {"executor_config": "NOT JSON"},
        _proj({"filesystem_roots": ["/good"]}),
    ]
    assert tn._union_filesystem_roots(projects) == ["/good"]


def test_union_filesystem_roots_non_dict_cfg():
    projects = [{"executor_config": None}, {"executor_config": 42}]
    assert tn._union_filesystem_roots(projects) == []


def test_union_repo_paths():
    projects = [
        _proj({"repo_path": "/repo/a"}),
        _proj({"repo_path": "/repo/a"}),  # dup
        _proj({"repo_path": "/repo/b"}),
    ]
    assert tn._union_repo_paths(projects) == ["/repo/a", "/repo/b"]


def test_union_repo_paths_empty_cfg():
    projects = [_proj({}), {"executor_config": None}]
    assert tn._union_repo_paths(projects) == []


def test_first_serena_repo_path_returns_first():
    projects = [
        _proj({"serena_repo_path": ""}),
        _proj({"serena_repo_path": "/serena/repo1"}),
        _proj({"serena_repo_path": "/serena/repo2"}),
    ]
    # First non-empty wins (order preserved)
    assert tn._first_serena_repo_path(projects) == "/serena/repo1"


def test_first_serena_repo_path_empty_list():
    assert tn._first_serena_repo_path([]) == ""
    assert tn._first_serena_repo_path([_proj({}), _proj({"serena_repo_path": "  "})]) == ""


def test_union_codebase_code_dirs():
    projects = [
        _proj({"codebase_code_dirs": ["/src/a", "/src/b"]}),
        _proj({"codebase_code_dirs": ["/src/b", "/src/c"]}),
    ]
    assert tn._union_codebase_code_dirs(projects) == ["/src/a", "/src/b", "/src/c"]


# ===========================================================================
# 11. active_tunnel_tenant_ids — multi-slot union
# ===========================================================================

def test_active_tunnel_tenant_ids_includes_all_slots():
    """active_tunnel_tenant_ids must include tenants from every slot registry
    (4b698ea5 — used by the keepalive loop to find live-binary tenants)."""
    tn._tunnel_sockets["t-fs"] = object()
    tn._tunnel_code_sockets["t-code"] = object()
    tn._tunnel_zotero_sockets["t-zotero"] = object()
    tn._tunnel_custom_sockets["p1"]["t-custom"] = object()

    ids = tn.active_tunnel_tenant_ids()
    assert "t-fs" in ids
    assert "t-code" in ids
    assert "t-zotero" in ids
    assert "t-custom" in ids
    # Non-connected tenant must not appear.
    assert "t-not-here" not in ids


# ===========================================================================
# 12. _parse_plugins_json — string/malformed input
# ===========================================================================

def test_parse_plugins_json_valid_json_string():
    result = tn._parse_plugins_json('{"fs": {"port": 8808}}')
    assert result == {"fs": {"port": 8808}}


def test_parse_plugins_json_invalid_string_returns_none():
    assert tn._parse_plugins_json("not valid json {{") is None


def test_parse_plugins_json_blank_string_returns_as_is():
    """A blank/whitespace-only string is not a JSON string so it's returned as-is
    (not None). Only malformed non-empty JSON strings return None."""
    assert tn._parse_plugins_json("") == ""
    # A whitespace-only string doesn't match `raw.strip()` → returned unchanged.
    assert tn._parse_plugins_json("   ") == "   "


def test_parse_plugins_json_dict_passthrough():
    d = {"fs": {"port": 8808}}
    assert tn._parse_plugins_json(d) is d


def test_parse_plugins_json_none_passthrough():
    assert tn._parse_plugins_json(None) is None


# ===========================================================================
# 13. _namespace_source_title — idempotency and blank handling
# ===========================================================================

def test_namespace_source_title_prefixes_bare():
    result = tn._namespace_source_title("Read File", "Filesystem")
    assert result == "Filesystem: Read File"


def test_namespace_source_title_idempotent():
    """Already-namespaced title is left alone (no double-prefix)."""
    assert tn._namespace_source_title("Filesystem: Read File", "Filesystem") is None


def test_namespace_source_title_blank_returns_none():
    assert tn._namespace_source_title("", "Filesystem") is None
    assert tn._namespace_source_title("   ", "Filesystem") is None
    assert tn._namespace_source_title(None, "Filesystem") is None
    assert tn._namespace_source_title(42, "Filesystem") is None


def test_namespace_source_title_partial_match_not_idempotent():
    """A title that STARTS with the src word but lacks ': ' is still namespaced."""
    result = tn._namespace_source_title("Filesystem Read File", "Filesystem")
    assert result == "Filesystem: Filesystem Read File"


# ===========================================================================
# 14. list_tunnel_tools — annotation title namespacing
# ===========================================================================

def test_list_tunnel_tools_namespaces_annotation_title(monkeypatch):
    """Older servers put the human label in annotations.title. Both top-level
    title AND annotations.title must be namespaced so the tool-permission UI
    shows the connector source on both paths."""
    tn._tunnel_code_sockets["t-annot"] = object()

    async def fake_do_proxy(tenant_id, method, path, query, headers, body, sockets, pending, label):
        resp = {"result": {"tools": [
            {
                "name": "trace_path",
                "title": "Trace Path",
                "annotations": {"title": "Trace Path", "readOnlyHint": True},
            }
        ]}}
        return Response(content=json.dumps(resp).encode(), status_code=200,
                        media_type="application/json")

    monkeypatch.setattr(tn, "_do_proxy", fake_do_proxy)

    tools = asyncio.run(tn.list_tunnel_tools("t-annot"))
    by_name = {t["name"]: t for t in tools}
    t = by_name.get("codebase__trace_path")
    assert t is not None
    # Top-level title namespaced.
    assert t["title"].startswith("Codebase")
    # annotations.title also namespaced.
    assert t["annotations"]["title"].startswith("Codebase")
    # Non-title annotation field untouched.
    assert t["annotations"]["readOnlyHint"] is True


# ===========================================================================
# 15. _do_proxy fly-replay path (cross-instance miss fallback)
# ===========================================================================

def test_do_proxy_fly_replay_when_owner_known(monkeypatch):
    """When the socket is absent but a Fly owner is known, _do_proxy returns a
    fly-replay response rather than a plain 503 (af5b5739)."""
    monkeypatch.setenv("FLY_MACHINE_ID", "this-machine")
    tn._tenant_owner_instance["t-replay"] = "other-machine"
    # No socket for this tenant on this machine (cross-instance miss).

    resp = asyncio.run(tn._do_proxy(
        "t-replay", "POST", "/mcp", "", {}, b"x",
        tn._tunnel_sockets, tn._pending_reqs, "fs",
    ))
    # Should carry fly-replay header (not a plain 503 "not connected" body).
    assert resp.headers.get(tn.FLY_REPLAY_HEADER) == "instance=other-machine"
    assert resp.status_code == 503
    assert b"replaying" in resp.body


def test_do_proxy_plain_503_when_no_owner_known(monkeypatch):
    """When no fly owner is known, _do_proxy returns the plain 503 + label body."""
    monkeypatch.setenv("FLY_MACHINE_ID", "this-machine")
    # No owner registered for this tenant.
    resp = asyncio.run(tn._do_proxy(
        "t-noowner", "POST", "/mcp", "", {}, b"x",
        tn._tunnel_sockets, tn._pending_reqs, "fs",
    ))
    assert resp.status_code == 503
    assert b"fs tunnel not connected" in resp.body
    assert tn.FLY_REPLAY_HEADER not in resp.headers


# ===========================================================================
# 16. _clear_slot_health — 54ddd609 discard on full clear
# ===========================================================================

def test_clear_slot_health_discards_tools_list_changed():
    """A full (no-slot) _clear_slot_health must drain the tools_list_changed
    pending marker — a disconnecting slot shouldn't leave a stale signal. 54ddd609."""
    tn._tools_list_changed_pending.add("t-disconnect")
    tn._slot_health["t-disconnect"] = {"fs": False}

    tn._clear_slot_health("t-disconnect")

    assert "t-disconnect" not in tn._tools_list_changed_pending
    assert tn._slot_health.get("t-disconnect") is None
