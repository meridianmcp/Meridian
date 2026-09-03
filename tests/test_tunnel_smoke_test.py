"""Unit tests for scripts/tunnel_smoke_test.py (sprint item 3dac9efb).

Covers the harness's own pure logic (JSON-RPC message construction,
result/finding formatting, failure-category classification, the "pass twice
consecutively" convergence tracker, the AV-exclusion hard-stop detection, the
client-wiring-gap + port-collision static regression checks, and
StdioMcpClient's I/O framing) via mocks/fixtures. None of these tests spawn a
real tunnel process or any real MCP server binary -- StdioMcpClient's happy-
path / timeout / early-exit tests spawn a tiny synthetic ``python -c ...``
stdio peer script instead, purely to exercise the framing/timeout code with a
real (but trivial, network-free, instantaneous) subprocess.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import textwrap
from pathlib import Path

import pytest

# scripts/ is not a package -- load the module directly by path (mirrors
# tests/test_deploy_drift.py's pattern). Registering the module in
# sys.modules BEFORE exec_module is required here (unlike deploy_drift.py):
# tunnel_smoke_test.py uses `from __future__ import annotations` + dataclasses,
# and dataclasses resolves stringified annotations via sys.modules[cls.__module__].
_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "tunnel_smoke_test.py"
_spec = importlib.util.spec_from_file_location("tunnel_smoke_test", _SCRIPT_PATH)
tst = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tst
_spec.loader.exec_module(tst)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# JSON-RPC message builders
# ---------------------------------------------------------------------------

def test_build_initialize_request_shape():
    req = tst.build_initialize_request(7)
    assert req["jsonrpc"] == "2.0"
    assert req["id"] == 7
    assert req["method"] == "initialize"
    assert req["params"]["protocolVersion"] == tst.MCP_PROTOCOL_VERSION
    assert req["params"]["clientInfo"]["name"] == "meridian-tunnel-smoke-test"


def test_build_initialized_notification_has_no_id():
    note = tst.build_initialized_notification()
    assert note["method"] == "notifications/initialized"
    assert "id" not in note


def test_build_tools_list_request_shape():
    req = tst.build_tools_list_request(3)
    assert req == {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}


def test_build_tools_call_request_shape():
    req = tst.build_tools_call_request("list_directory", {"path": "/x"}, req_id=9)
    assert req["method"] == "tools/call"
    assert req["id"] == 9
    assert req["params"] == {"name": "list_directory", "arguments": {"path": "/x"}}


def test_build_tools_call_request_defaults_arguments_to_empty_dict():
    req = tst.build_tools_call_request("ping", None, req_id=1)
    assert req["params"]["arguments"] == {}


# ---------------------------------------------------------------------------
# parse_jsonrpc_message
# ---------------------------------------------------------------------------

def test_parse_jsonrpc_message_valid_object():
    msg = tst.parse_jsonrpc_message('{"jsonrpc": "2.0", "id": 1, "result": {}}\n')
    assert msg == {"jsonrpc": "2.0", "id": 1, "result": {}}


@pytest.mark.parametrize("line", ["", "   ", "\n"])
def test_parse_jsonrpc_message_blank_returns_none(line):
    assert tst.parse_jsonrpc_message(line) is None


def test_parse_jsonrpc_message_non_json_returns_none():
    assert tst.parse_jsonrpc_message("not json at all") is None


def test_parse_jsonrpc_message_json_array_returns_none():
    # Valid JSON, but not an object -- callers only accept dicts.
    assert tst.parse_jsonrpc_message("[1, 2, 3]") is None


def test_parse_jsonrpc_message_json_scalar_returns_none():
    assert tst.parse_jsonrpc_message("42") is None


# ---------------------------------------------------------------------------
# select_functional_tool
# ---------------------------------------------------------------------------

def test_select_functional_tool_prefers_named_candidate_when_present():
    tools = [
        {"name": "read_file", "inputSchema": {"required": ["path"]}},
        {"name": "list_directory", "inputSchema": {"required": ["path"]}},
    ]
    picked = tst.select_functional_tool(tools, preferred=[("list_directory", {"path": "/repo"})])
    assert picked == ("list_directory", {"path": "/repo"})


def test_select_functional_tool_falls_back_to_zero_required_tool():
    tools = [
        {"name": "read_file", "inputSchema": {"required": ["path"]}},
        {"name": "get_status", "inputSchema": {}},
    ]
    picked = tst.select_functional_tool(tools, preferred=[("nonexistent", {})])
    assert picked == ("get_status", {})


def test_select_functional_tool_no_safe_candidate_returns_none():
    tools = [
        {"name": "read_file", "inputSchema": {"required": ["path"]}},
        {"name": "write_file", "inputSchema": {"required": ["path", "content"]}},
    ]
    assert tst.select_functional_tool(tools) is None


def test_select_functional_tool_empty_tools_returns_none():
    assert tst.select_functional_tool([]) is None


def test_select_functional_tool_ignores_malformed_entries():
    tools = [None, "not-a-dict", {"no_name": True}, {"name": "ok", "inputSchema": {}}]
    assert tst.select_functional_tool(tools) == ("ok", {})


def test_select_functional_tool_missing_input_schema_counts_as_zero_required():
    tools = [{"name": "ping"}]
    assert tst.select_functional_tool(tools) == ("ping", {})


# ---------------------------------------------------------------------------
# classify_captured_output — failure-category signature detection
# ---------------------------------------------------------------------------

def test_classify_av_interference():
    text = "npm WARN tar TAR_ENTRY_ERROR EACCES: permission denied, open '...'"
    assert tst.classify_captured_output(text) == "av_interference"


def test_classify_registry_resolution():
    text = "error: meridian-docs was not found in the package registry"
    assert tst.classify_captured_output(text) == "registry_resolution"


def test_classify_browser_auth_required():
    text = "No API token found. Opening browser to authorize tunnel access."
    assert tst.classify_captured_output(text) == "browser_auth_required"


def test_classify_clean_output_returns_none():
    assert tst.classify_captured_output("tunnel:fs: connected (lazy mode)") is None


@pytest.mark.parametrize("text", [None, ""])
def test_classify_falsy_input_returns_none(text):
    assert tst.classify_captured_output(text) is None


def test_classify_av_takes_precedence_over_registry():
    # Never treat an AV-caused failure as a retriable registry problem.
    text = "TAR_ENTRY_ERROR while extracting; also X was not found in the package registry"
    assert tst.classify_captured_output(text) == "av_interference"


# ---------------------------------------------------------------------------
# check_client_wires_all_catalog_slots — static regression check
# ---------------------------------------------------------------------------

def test_check_client_wires_all_catalog_slots_all_present():
    source = 'if slot == "fs": ...\nfor s in ("ppt", "word", "dc", "docs", "zotero"): ...'
    missing = tst.check_client_wires_all_catalog_slots(
        source, ["fs", "code", "extract", "ppt", "word", "dc", "docs", "zotero"]
    )
    assert missing == []


def test_check_client_wires_all_catalog_slots_detects_gap():
    source = 'if slot == "fs": ...\nfor s in ("ppt", "word", "dc", "docs", "zotero"): ...'
    missing = tst.check_client_wires_all_catalog_slots(
        source, ["fs", "code", "extract", "ppt", "word", "dc", "docs", "zotero", "outputs", "debug"]
    )
    assert missing == ["outputs", "debug"]


def test_check_client_wires_all_catalog_slots_core_slots_always_exempt():
    # fs/code/extract are structurally always wired (they're core, non-optional
    # slots with dedicated always-on branches) -- never flagged even if their
    # literal quoted slot code happens to be absent from a synthetic fixture.
    missing = tst.check_client_wires_all_catalog_slots("no slot literals here at all", ["fs", "code", "extract"])
    assert missing == []


def test_check_client_wires_all_catalog_slots_real_source_matches_known_gap():
    """Live check against the actual tunnel_client.py source: this harness's
    own construction turned up a real 2026-07-19 finding ('outputs'/'debug'
    were declared in the plugin catalog but never wired into run_tunnel).
    FIXED by 12afe021 -- both slots now join the office-family SlotProxy +
    reconnect-loop loop, so the real source has zero missing catalog slots.
    This is now a permanent regression guard: any future slot added to
    BUILTIN_PLUGINS without matching client wiring will flip this back to
    non-empty."""
    real_source = Path(tst.__file__).resolve().parent.parent.joinpath(
        "meridian", "tunnel_client.py"
    ).read_text(encoding="utf-8")
    missing = tst.check_client_wires_all_catalog_slots(real_source, tst.SLOTS)
    assert missing == []


# ---------------------------------------------------------------------------
# check_port_collisions
# ---------------------------------------------------------------------------

def test_check_port_collisions_clean_on_real_current_source():
    """a1a870d5 (2026-07-19) fixed the real finding this check exists for:
    SERENA_POOL_BASE_PORT used to collide with DEFAULT_OUTPUTS_PORT (both
    8820). SERENA_POOL_BASE_PORT is now 8700, below every fixed port the
    tunnel-plugin catalog declares, so the actual current source has zero
    collisions across every declared port."""
    findings = tst.check_port_collisions()
    assert findings == []


def test_check_port_collisions_detects_synthetic_collision_matching_historical_bug(monkeypatch):
    """Proves the general checker would have caught the exact a1a870d5 bug:
    reproduce SERENA_POOL_BASE_PORT == DEFAULT_OUTPUTS_PORT via monkeypatch
    and confirm it is flagged."""
    from meridian.tunnel_plugins import DEFAULT_OUTPUTS_PORT

    monkeypatch.setattr(tst, "SERENA_POOL_BASE_PORT", DEFAULT_OUTPUTS_PORT)
    findings = tst.check_port_collisions()
    assert len(findings) == 1
    assert str(DEFAULT_OUTPUTS_PORT) in findings[0]
    assert "serena_pool.SERENA_POOL_BASE_PORT" in findings[0]
    assert "tunnel_plugins.DEFAULT_OUTPUTS_PORT" in findings[0]


def test_check_port_collisions_no_collision_when_ports_differ(monkeypatch):
    monkeypatch.setattr(tst, "SERENA_POOL_BASE_PORT", 9500)
    findings = tst.check_port_collisions()
    assert findings == []


def test_check_port_collisions_detects_any_pairwise_collision_among_fixed_ports(monkeypatch):
    """The checker is general, not hardcoded to the Serena/outputs pair --
    any two fixed constants sharing a port number get flagged."""
    import meridian.tunnel_plugins as tp

    monkeypatch.setattr(tp, "DEFAULT_PPT_PORT", tp.DEFAULT_WORD_PORT)
    findings = tst.check_port_collisions()
    assert len(findings) == 1
    assert str(tp.DEFAULT_WORD_PORT) in findings[0]
    assert "DEFAULT_PPT_PORT" in findings[0]
    assert "DEFAULT_WORD_PORT" in findings[0]


def test_check_port_collisions_flags_when_ports_match(monkeypatch):
    import meridian.tunnel_plugins as tp
    monkeypatch.setattr(tst, "SERENA_POOL_BASE_PORT", 12345)
    monkeypatch.setattr(tp, "DEFAULT_OUTPUTS_PORT", 12345)
    findings = tst.check_port_collisions()
    assert len(findings) == 1
    assert "12345" in findings[0]


# ---------------------------------------------------------------------------
# detect_cascading_disconnect
# ---------------------------------------------------------------------------

def test_detect_cascading_disconnect_flags_close_cross_slot_event():
    lines = [
        (10.0, "tunnel:docs: spawning proxy (first request / after idle kill) on port 8818"),
        (12.0, "tunnel:fs: disconnected (some error); reconnecting in 1s"),
    ]
    findings = tst.detect_cascading_disconnect(lines, window_s=20.0)
    assert len(findings) == 1
    assert "fs" in findings[0] and "docs" in findings[0]


def test_detect_cascading_disconnect_ignores_same_slot_events():
    lines = [
        (10.0, "tunnel:docs: spawning proxy (first request / after idle kill) on port 8818"),
        (12.0, "tunnel:docs: disconnected (timeout); reconnecting in 1s"),
    ]
    assert tst.detect_cascading_disconnect(lines) == []


def test_detect_cascading_disconnect_ignores_events_outside_window():
    lines = [
        (0.0, "tunnel:docs: spawning proxy (first request / after idle kill) on port 8818"),
        (500.0, "tunnel:fs: disconnected (unrelated); reconnecting in 1s"),
    ]
    assert tst.detect_cascading_disconnect(lines, window_s=20.0) == []


def test_detect_cascading_disconnect_empty_lines_returns_empty():
    assert tst.detect_cascading_disconnect([]) == []


# ---------------------------------------------------------------------------
# predict_needs_browser_auth (failure category 7, pre-flight)
# ---------------------------------------------------------------------------

def test_predict_needs_browser_auth_true_when_no_cached_token(monkeypatch):
    monkeypatch.setattr(tst, "_read_cached_token", lambda base_url: None)
    assert tst.predict_needs_browser_auth("https://usemeridian.us") is True


def test_predict_needs_browser_auth_false_when_token_cached(monkeypatch):
    monkeypatch.setattr(tst, "_read_cached_token", lambda base_url: "sk_meridian_xxx")
    assert tst.predict_needs_browser_auth("https://usemeridian.us") is False


# ---------------------------------------------------------------------------
# ConvergenceTracker — "passes once" != "fixed"
# ---------------------------------------------------------------------------

def test_convergence_tracker_requires_consecutive_passes():
    t = tst.ConvergenceTracker(required=2)
    t.record("fs", True)
    assert t.is_solid("fs") is False  # only 1 so far
    t.record("fs", True)
    assert t.is_solid("fs") is True  # 2 in a row


def test_convergence_tracker_failure_resets_streak():
    t = tst.ConvergenceTracker(required=2)
    t.record("fs", True)
    t.record("fs", False)
    t.record("fs", True)
    assert t.is_solid("fs") is False  # streak reset by the failure, only 1 since
    t.record("fs", True)
    assert t.is_solid("fs") is True


def test_convergence_tracker_all_solid_requires_every_slot():
    t = tst.ConvergenceTracker(required=1)
    t.record("fs", True)
    t.record("code", True)
    assert t.all_solid(["fs", "code"]) is True
    assert t.all_solid(["fs", "code", "extract"]) is False  # extract never recorded


def test_convergence_tracker_all_solid_empty_slots_is_false():
    t = tst.ConvergenceTracker()
    assert t.all_solid([]) is False


def test_convergence_tracker_unsolved_lists_only_non_solid_slots():
    t = tst.ConvergenceTracker(required=2)
    t.record("fs", True)
    t.record("fs", True)
    t.record("code", True)
    assert t.unsolved(["fs", "code"]) == ["code"]


def test_convergence_tracker_streak_defaults_to_zero_for_unseen_slot():
    t = tst.ConvergenceTracker()
    assert t.streak("never-seen") == 0
    assert t.is_solid("never-seen") is False


# ---------------------------------------------------------------------------
# Result / finding dataclasses -- JSON-serializability + basic shape
# ---------------------------------------------------------------------------

def test_slot_result_to_dict_round_trips_through_json():
    r = tst.SlotResult("fs", "filesystem", passed=True, spawn_ms=123.4, tools_count=11,
                        functional_tool="list_directory", functional_ok=True,
                        repeat_consistent=True, notes=["ok"])
    payload = json.loads(json.dumps(r.to_dict()))
    assert payload["slot"] == "fs"
    assert payload["passed"] is True
    assert payload["notes"] == ["ok"]


def test_finding_to_dict_shape():
    f = tst.Finding(category="av_interference", severity="hard-stop", slot="dc",
                     summary="boom", detail="details here")
    assert f.to_dict() == {
        "category": "av_interference", "severity": "hard-stop", "slot": "dc",
        "summary": "boom", "detail": "details here",
    }


def test_cycle_result_any_hard_stop_true_when_present():
    cycle = tst.CycleResult(
        cycle=1, started_at="t0", ended_at="t1", slot_results=[],
        findings=[tst.Finding("av_interference", "hard-stop", "boom")],
    )
    assert cycle.any_hard_stop() is True


def test_cycle_result_any_hard_stop_false_when_absent():
    cycle = tst.CycleResult(
        cycle=1, started_at="t0", ended_at="t1", slot_results=[],
        findings=[tst.Finding("registry_resolution", "action-needed", "meh")],
    )
    assert cycle.any_hard_stop() is False


def test_cycle_result_to_dict_serializes_nested_results():
    cycle = tst.CycleResult(
        cycle=2, started_at="t0", ended_at="t1",
        slot_results=[tst.SlotResult("fs", "filesystem", passed=True)],
        findings=[tst.Finding("info", "info", "hi")],
        tunnel_boot_ok=True,
    )
    payload = json.loads(json.dumps(cycle.to_dict()))
    assert payload["cycle"] == 2
    assert payload["tunnel_boot_ok"] is True
    assert payload["slot_results"][0]["slot"] == "fs"
    assert payload["findings"][0]["category"] == "info"


def test_slot_spec_to_dict_shape():
    spec = tst.SlotSpec("fs", "filesystem", 8808, ["npx", "-y", "pkg"], None, "stateless", False)
    d = spec.to_dict()
    assert d["slot"] == "fs"
    assert d["port"] == 8808
    assert d["wired_in_client"] is True


# ---------------------------------------------------------------------------
# _resolve_launcher
# ---------------------------------------------------------------------------

def test_resolve_launcher_replaces_bare_npx(monkeypatch):
    monkeypatch.setattr(tst, "_find_npx", lambda: r"C:\npm\npx.cmd")
    out = tst._resolve_launcher(["npx", "-y", "@modelcontextprotocol/server-filesystem", "/repo"])
    assert out[0] == r"C:\npm\npx.cmd"
    assert out[1:] == ["-y", "@modelcontextprotocol/server-filesystem", "/repo"]


def test_resolve_launcher_replaces_bare_uvx_when_found(monkeypatch):
    monkeypatch.setattr(tst, "_find_uvx", lambda: "/home/me/.local/bin/uvx")
    out = tst._resolve_launcher(["uvx", "zotero-mcp"])
    assert out[0] == "/home/me/.local/bin/uvx"


def test_resolve_launcher_leaves_uvx_bare_when_not_found(monkeypatch):
    monkeypatch.setattr(tst, "_find_uvx", lambda: None)
    out = tst._resolve_launcher(["uvx", "zotero-mcp"])
    assert out[0] == "uvx"  # unchanged -- lets the caller's spawn surface a clear ENOENT


def test_resolve_launcher_leaves_other_commands_untouched():
    out = tst._resolve_launcher(["cmd", "/c", "npx", "-y", "@wonderwhy-er/desktop-commander@0.2.46"])
    assert out == ["cmd", "/c", "npx", "-y", "@wonderwhy-er/desktop-commander@0.2.46"]


def test_resolve_launcher_empty_command_returns_empty():
    assert tst._resolve_launcher([]) == []


# ---------------------------------------------------------------------------
# Auto-fixer registry
# ---------------------------------------------------------------------------

def test_apply_client_side_cache_clear_fix_delegates_to_scoped_cache_clear(monkeypatch):
    calls = []
    monkeypatch.setattr(tst, "_scoped_cache_clear", lambda cmd, label: calls.append((cmd, label)) or True)
    spec = tst.SlotSpec("docs", "meridian-docs", 8818, ["uvx", "--from", "/x", "meridian-docs-mcp"],
                         None, "stateless", True)
    assert tst.apply_client_side_cache_clear_fix(spec) is True
    assert calls == [(spec.cmd, "docs")]


def test_auto_fixers_only_registered_for_registry_resolution():
    # ABSOLUTE HARD BOUNDARY: never auto-fix AV interference or browser-auth.
    assert "av_interference" not in tst.AUTO_FIXERS
    assert "browser_auth_required" not in tst.AUTO_FIXERS
    assert "registry_resolution" in tst.AUTO_FIXERS


# ---------------------------------------------------------------------------
# detect_live_peer_ports / kill_stale_ports safety guard
# ---------------------------------------------------------------------------

def test_detect_live_peer_ports_skips_closed_ports(monkeypatch):
    monkeypatch.setattr(tst, "_port_is_open", lambda port: False)
    assert tst.detect_live_peer_ports([8808, 8809]) == []


def test_detect_live_peer_ports_flags_open_port_with_live_claim(monkeypatch):
    monkeypatch.setattr(tst, "_port_is_open", lambda port: True)
    monkeypatch.setattr(tst, "_is_slot_claimed_by_live_client", lambda port, cid: True)
    assert tst.detect_live_peer_ports([8808]) == [8808]


def test_detect_live_peer_ports_conservatively_flags_open_port_with_no_claim(monkeypatch):
    # Open but unclaimed (e.g. a pre-claim-file-era manual tunnel invocation)
    # -- treated as possibly live, never silently assumed safe.
    monkeypatch.setattr(tst, "_port_is_open", lambda port: True)
    monkeypatch.setattr(tst, "_is_slot_claimed_by_live_client", lambda port, cid: False)
    assert tst.detect_live_peer_ports([8808]) == [8808]


def test_kill_stale_ports_delegates_with_fresh_client_id(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tst, "_kill_stale_port_occupant",
        lambda port, label, current_client_id: calls.append((port, label, current_client_id)),
    )
    tst.kill_stale_ports([8808, 8809], client_id="my-run-id")
    assert calls == [
        (8808, "smoke-test:8808", "my-run-id"),
        (8809, "smoke-test:8809", "my-run-id"),
    ]


def test_kill_stale_ports_never_raises_on_individual_failure(monkeypatch):
    def _boom(port, label, current_client_id):
        raise RuntimeError("nope")
    monkeypatch.setattr(tst, "_kill_stale_port_occupant", _boom)
    tst.kill_stale_ports([8808], client_id="x")  # must not raise


# ---------------------------------------------------------------------------
# StdioMcpClient -- real (synthetic, non-tunnel) subprocess I/O framing
# ---------------------------------------------------------------------------

_FAKE_SERVER_SRC = textwrap.dedent(r"""
    import json, sys

    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        req = json.loads(raw)
        method = req.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": req["id"], "result": {"serverInfo": {"name": "fake"}}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": req["id"], "result": {"tools": [
                {"name": "get_status", "inputSchema": {}},
                {"name": "read_file", "inputSchema": {"required": ["path"]}},
            ]}})
        elif method == "tools/call":
            name = req["params"]["name"]
            send({"jsonrpc": "2.0", "id": req["id"], "result": {"content": [
                {"type": "text", "text": f"called {name}"}
            ]}})
""")

_HANGING_SERVER_SRC = "import sys, time\nsys.stdin.readline()\ntime.sleep(60)\n"
_EXIT_IMMEDIATELY_SRC = "import sys\nsys.exit(0)\n"

# 2026-07-20 finding: modeled directly on a real, reproduced live capture of
# @wonderwhy-er/desktop-commander@0.2.46 -- it emits several
# `notifications/message` (no "id") log lines on stdout in between the real
# initialize response and the real tools/list response. Any spec-compliant
# MCP server may do this (server-initiated notifications are explicitly
# allowed at any time); this fake reproduces exactly that interleaving.
_CHATTY_SERVER_SRC = textwrap.dedent(r"""
    import json, sys

    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()

    def notify(text):
        send({"jsonrpc": "2.0", "method": "notifications/message",
              "params": {"level": "info", "logger": "chatty", "data": text}})

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        req = json.loads(raw)
        method = req.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": req["id"], "result": {"serverInfo": {"name": "chatty"}}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            notify("loading configuration")
            notify("connecting server")
            notify("setting up request handlers")
            send({"jsonrpc": "2.0", "id": req["id"], "result": {"tools": [
                {"name": "get_status", "inputSchema": {}},
            ]}})
        elif method == "tools/call":
            notify("dispatching call")
            name = req["params"]["name"]
            send({"jsonrpc": "2.0", "id": req["id"], "result": {"content": [
                {"type": "text", "text": f"called {name}"}
            ]}})
""")


@pytest.mark.asyncio
async def test_stdio_mcp_client_full_happy_path():
    client = tst.StdioMcpClient([sys.executable, "-c", _FAKE_SERVER_SRC], label="fake")
    await client.start()
    try:
        init_resp = await client.initialize(timeout=10.0)
        assert init_resp["result"]["serverInfo"]["name"] == "fake"

        tools = await client.list_tools(timeout=10.0)
        assert {t["name"] for t in tools} == {"get_status", "read_file"}

        picked = tst.select_functional_tool(tools)
        assert picked == ("get_status", {})
        call_resp = await client.call_tool(*picked, timeout=10.0)
        assert "called get_status" in call_resp["result"]["content"][0]["text"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_mcp_client_times_out_on_unresponsive_server():
    client = tst.StdioMcpClient([sys.executable, "-c", _HANGING_SERVER_SRC], label="hang")
    await client.start()
    try:
        with pytest.raises(tst.McpStdioError, match="timed out"):
            await client.initialize(timeout=0.5)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_mcp_client_close_does_not_raise_when_stderr_task_still_pending(monkeypatch):
    """2026-07-20 finding: contextlib.suppress(Exception) does NOT catch
    asyncio.CancelledError -- a BaseException, not an Exception, since Python
    3.8. close() cancels self._stderr_task then awaits it; if the drain loop
    was still genuinely blocked on a read() (true whenever the real child
    process/tree is slower to die than _terminate_proc_tree_async, e.g. a
    Serena/language-server process tree observed live), the resulting
    CancelledError escaped close() uncaught and crashed the whole harness
    (run_cycle -> run_loop -> _amain) on an otherwise-ordinary slot timeout.
    Reproduced by stubbing _terminate_proc_tree_async as a no-op so the
    stderr task is still pending -- not already EOF-completed -- when
    close() cancels it."""
    async def _no_op_terminate(proc):
        return None

    monkeypatch.setattr(tst, "_terminate_proc_tree_async", _no_op_terminate)
    client = tst.StdioMcpClient([sys.executable, "-c", _EXIT_IMMEDIATELY_SRC], label="race")
    await client.start()
    # Force the stderr-drain task into a state that's still pending (blocked)
    # regardless of how fast the real trivial subprocess's own stderr pipe
    # closed, so close()'s .cancel() genuinely interrupts a live await.
    client._stderr_task.cancel()
    client._stderr_task = asyncio.ensure_future(asyncio.sleep(60))
    await client.close()  # must not raise


@pytest.mark.asyncio
async def test_stdio_mcp_client_raises_when_process_exits_early():
    client = tst.StdioMcpClient([sys.executable, "-c", _EXIT_IMMEDIATELY_SRC], label="exit")
    await client.start()
    try:
        with pytest.raises(tst.McpStdioError, match="closed stdout"):
            await client.initialize(timeout=5.0)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_mcp_client_skips_notifications_interleaved_before_response():
    """2026-07-20 finding: _recv() used to return the FIRST parseable JSON-RPC
    line, with no check that it was actually the response to the request just
    sent rather than a server-initiated notification (no "id", legal at any
    time per the MCP spec). Live-reproduced against
    @wonderwhy-er/desktop-commander@0.2.46: its startup notifications got
    returned as if they WERE the tools/list response, silently yielding
    `tools=0, error=None` instead of the real (non-empty) tool list. This
    exercises the exact same interleaving via _CHATTY_SERVER_SRC and asserts
    list_tools() correctly skips the notifications and returns the real
    tools, not an empty list."""
    client = tst.StdioMcpClient([sys.executable, "-c", _CHATTY_SERVER_SRC], label="chatty")
    await client.start()
    try:
        init_resp = await client.initialize(timeout=10.0)
        assert init_resp["result"]["serverInfo"]["name"] == "chatty"

        tools = await client.list_tools(timeout=10.0)
        assert [t["name"] for t in tools] == ["get_status"]

        call_resp = await client.call_tool("get_status", {}, timeout=10.0)
        assert "called get_status" in call_resp["result"]["content"][0]["text"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_mcp_client_raises_mcpstdioerror_on_missing_binary():
    client = tst.StdioMcpClient(["this-binary-does-not-exist-anywhere-xyz"], label="missing")
    with pytest.raises(tst.McpStdioError, match="spawn failed"):
        await client.start()


# ---------------------------------------------------------------------------
# ba31dedf -- close() returns a REAL shutdown receipt (the "shutdown" leg of
# the cold/warm/shutdown smoke test), and SlotResult carries it through.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_returns_true_when_well_behaved_process_exits_cleanly():
    client = tst.StdioMcpClient([sys.executable, "-c", _FAKE_SERVER_SRC], label="fake")
    await client.start()
    await client.initialize(timeout=10.0)
    shutdown_confirmed = await client.close()
    assert shutdown_confirmed is True
    assert client._proc.returncode is not None


@pytest.mark.asyncio
async def test_close_returns_true_after_force_killing_unresponsive_process():
    """The hard case: a process that never reads stdin/exits on its own still
    gets force-killed by _terminate_proc_tree_async -- close() must confirm
    that kill actually landed (a real receipt), not just assume it did."""
    client = tst.StdioMcpClient([sys.executable, "-c", _HANGING_SERVER_SRC], label="hang")
    await client.start()
    shutdown_confirmed = await client.close()
    assert shutdown_confirmed is True
    assert client._proc.returncode is not None


@pytest.mark.asyncio
async def test_close_returns_false_when_process_never_spawned():
    client = tst.StdioMcpClient([sys.executable, "-c", "pass"], label="unspawned")
    shutdown_confirmed = await client.close()
    assert shutdown_confirmed is False


def test_slot_result_carries_shutdown_confirmed_field():
    r = tst.SlotResult("fs", "filesystem", passed=True, shutdown_confirmed=True)
    assert r.shutdown_confirmed is True
    payload = json.loads(json.dumps(r.to_dict()))
    assert payload["shutdown_confirmed"] is True


def test_slot_result_shutdown_confirmed_defaults_to_none():
    """A SlotResult built before any spawn was attempted (skip_reason /
    no-runnable-command early returns in test_slot) has no shutdown to report."""
    r = tst.SlotResult("fs", "filesystem", passed=False, error="skipped")
    assert r.shutdown_confirmed is None
    assert r.to_dict()["shutdown_confirmed"] is None


@pytest.mark.asyncio
async def test_test_slot_populates_shutdown_confirmed_on_success(tmp_path):
    """End-to-end through test_slot() itself (not just StdioMcpClient directly)
    -- the success path must thread the real shutdown_confirmed receipt onto
    the returned SlotResult (companion to test_test_slot_full_pass_via_fake_
    stdio_server's pre-existing coverage of the rest of that same result)."""
    spec = tst.SlotSpec("fake", "fake-slot", 9999, [sys.executable, "-c", _FAKE_SERVER_SRC],
                         None, "stateless", False)
    result = await tst.test_slot(spec, str(tmp_path), repeat_check=False)
    assert result.passed is True
    assert result.shutdown_confirmed is True


@pytest.mark.asyncio
async def test_test_slot_populates_shutdown_confirmed_on_early_protocol_failure(tmp_path):
    """The error-path branch (spawn/initialize/list_tools failure) must also
    report a real shutdown receipt, not leave it None once a process WAS
    spawned."""
    spec = tst.SlotSpec("hang", "hang-slot", 9998, [sys.executable, "-c", _HANGING_SERVER_SRC],
                         None, "stateless", False)
    result = await tst.test_slot(spec, str(tmp_path), spawn_timeout=0.5)
    assert result.passed is False
    assert result.shutdown_confirmed is True


# ---------------------------------------------------------------------------
# build_slot_specs -- code-intel cache-dir parity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_slot_specs_code_slot_uses_dedicated_harness_cache_dir(tmp_path, monkeypatch):
    """2026-07-20 finding: the "code" SlotSpec used to pass env=None, so its
    codebase-memory-mcp spawn used the default (unset) CBM_CACHE_DIR instead
    of a warm, dedicated one -- forcing a full cold reindex (and a false 30s
    timeout) on every harness run. Confirms build_slot_specs now gives it a
    real, harness-only CBM_CACHE_DIR distinct from both the unset default and
    production's own dedicated dir (tunnel_client._code_intel_cache_dir), and
    is now treated as a cold-fetch slot (gets the larger spawn budget +
    warm-repeat check on cycle 1)."""
    async def _fake_ensure(*a, **kw):
        return str(tmp_path / "fake-codebase-memory-mcp")

    monkeypatch.delenv("CBM_CACHE_DIR", raising=False)
    monkeypatch.setattr(tst, "_ensure_codebase_memory_mcp", _fake_ensure)
    specs = await tst.build_slot_specs(str(tmp_path), slots=["code"])
    assert len(specs) == 1
    spec = specs[0]
    assert spec.cold_fetch is True
    assert "code" in tst.COLD_FETCH_SLOTS
    assert spec.env is not None
    assert spec.env.get("CBM_CACHE_DIR")
    assert "code-intel-cache-smoketest" in spec.env["CBM_CACHE_DIR"]

    from meridian.tunnel_client import _code_intel_cache_dir
    assert spec.env["CBM_CACHE_DIR"] != str(_code_intel_cache_dir())


# ---------------------------------------------------------------------------
# _terminate_proc_tree_async
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_terminate_proc_tree_async_bounds_stalled_taskkill(monkeypatch):
    """A stalled ``taskkill /F /T /PID`` (observed under AV/Defender
    interference, or an unkillable target process) must not hang
    _terminate_proc_tree_async forever. The very next await in the same
    function (``proc.wait()``) is already wrapped in
    ``asyncio.wait_for(..., timeout=5.0)``; the taskkill wait must be bounded
    the same way so a stall there still reaches the ``proc.terminate()``
    fallback instead of blocking every caller (StdioMcpClient.close(),
    TunnelSubprocess.stop()) indefinitely.

    Reproduced by making the spawned taskkill helper process's own ``wait()``
    never resolve, then bounding the whole call with an outer watchdog that
    is longer than the function's internal 5s timeout but far shorter than
    "forever" -- before the fix this outer watchdog is what fires (proving
    the function itself has no bound); after the fix the function returns on
    its own well within the outer watchdog.
    """
    monkeypatch.setattr(tst.sys, "platform", "win32")

    class _HangingKillProc:
        def __init__(self):
            self.pid = 999

        async def wait(self):
            await asyncio.Event().wait()  # never resolves

    async def _fake_create_subprocess_exec(*args, **kwargs):
        return _HangingKillProc()

    monkeypatch.setattr(tst.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    class _FakeTargetProc:
        def __init__(self):
            self.pid = 123
            self.terminate_called = False

        def terminate(self):
            self.terminate_called = True

        async def wait(self):
            return 0

    target = _FakeTargetProc()
    await asyncio.wait_for(tst._terminate_proc_tree_async(target), timeout=8.0)
    assert target.terminate_called is True


# ---------------------------------------------------------------------------
# test_slot orchestration (mocked StdioMcpClient -- no real spawn)
# ---------------------------------------------------------------------------

def test_test_slot_returns_failure_immediately_for_skip_reason():
    spec = tst.SlotSpec("code", "code-intel", None, [], None, "stateless", False,
                         skip_reason="codebase-memory-mcp could not be installed")
    result = asyncio.run(tst.test_slot(spec, "/repo"))
    assert result.passed is False
    assert "could not be installed" in result.error


def test_test_slot_returns_failure_for_empty_command():
    spec = tst.SlotSpec("ppt", "powerpoint", 8811, [], None, "stateless", True)
    result = asyncio.run(tst.test_slot(spec, "/repo"))
    assert result.passed is False
    assert result.error == "no runnable command resolved"


@pytest.mark.asyncio
async def test_test_slot_full_pass_via_fake_stdio_server(tmp_path):
    spec = tst.SlotSpec("fake", "fake-slot", 9999, [sys.executable, "-c", _FAKE_SERVER_SRC],
                         None, "stateless", False)
    result = await tst.test_slot(spec, str(tmp_path), repeat_check=False)
    assert result.passed is True
    assert result.tools_count == 2
    assert result.functional_ok is True
    assert result.functional_tool == "get_status"


@pytest.mark.asyncio
async def test_test_slot_classifies_av_signature_on_spawn_failure(tmp_path):
    src = (
        "import sys\n"
        "sys.stderr.write('npm WARN tar TAR_ENTRY_ERROR boom')\n"
        "sys.stderr.flush()\n"
        "sys.exit(1)\n"
    )
    spec = tst.SlotSpec("dc", "desktop-commander", 8813, [sys.executable, "-c", src],
                         None, "persistent", True)
    result = await tst.test_slot(spec, str(tmp_path), spawn_timeout=5.0)
    assert result.passed is False
    assert result.classification == "av_interference"


@pytest.mark.asyncio
async def test_test_slot_times_out_on_hung_process_spawn(tmp_path, monkeypatch):
    """2026-07-20 finding: client.start() (a bare asyncio.create_subprocess_exec)
    was the one await in test_slot with no timeout wrapper -- an OS-level stall
    before the child process ever talks on stdio (AV on-access scan, a first-run
    GUI/elevation prompt from an Office-family slot, etc.) hung the whole harness
    indefinitely. Simulates that stall by making StdioMcpClient.start() never
    return, and asserts test_slot still resolves within spawn_timeout instead of
    hanging -- the outer asyncio.wait_for is this test's own regression guard."""
    async def _hang_forever(self):
        await asyncio.sleep(999)

    monkeypatch.setattr(tst.StdioMcpClient, "start", _hang_forever)
    spec = tst.SlotSpec("dc", "desktop-commander", 8813, [sys.executable, "-c", _EXIT_IMMEDIATELY_SRC],
                         None, "persistent", True)
    result = await asyncio.wait_for(
        tst.test_slot(spec, str(tmp_path), spawn_timeout=0.5), timeout=5.0,
    )
    assert result.passed is False


# ---------------------------------------------------------------------------
# static_findings — integration of the two static checks
# ---------------------------------------------------------------------------

def test_static_findings_clean_after_12afe021_and_a1a870d5():
    """Both static findings this checker exists to catch are fixed against the
    real repo: 12afe021 wired outputs/debug into run_tunnel (no more
    client_wiring_gap), and a1a870d5 moved SERENA_POOL_BASE_PORT off
    DEFAULT_OUTPUTS_PORT (no more port_collision)."""
    findings = tst.static_findings()
    categories = {f.category for f in findings}
    assert "client_wiring_gap" not in categories
    assert "port_collision" not in categories


def test_static_findings_reports_port_collision_when_reintroduced(monkeypatch):
    """If SERENA_POOL_BASE_PORT ever regresses back onto a declared port,
    static_findings must surface it as an action-needed port_collision."""
    from meridian.tunnel_plugins import DEFAULT_OUTPUTS_PORT

    monkeypatch.setattr(tst, "SERENA_POOL_BASE_PORT", DEFAULT_OUTPUTS_PORT)
    findings = tst.static_findings()
    port_findings = [f for f in findings if f.category == "port_collision"]
    assert len(port_findings) == 1
    assert port_findings[0].severity == "action-needed"


# ---------------------------------------------------------------------------
# TunnelSubprocess.start() unbounded-await regression (matches the sibling
# StdioMcpClient.start() bug already fixed in test_slot()): a stall inside
# asyncio.create_subprocess_exec itself must not hang run_cycle /
# check_auth_reuse forever -- both call sites now bound it with
# asyncio.wait_for(..., timeout=STARTUP_HARD_TIMEOUT_S).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_auth_reuse_bounds_hung_subprocess_spawn(tmp_path, monkeypatch):
    """Before the fix, ``await boot.start()`` in check_auth_reuse was never
    wrapped in asyncio.wait_for, so a stall inside create_subprocess_exec
    itself (simulated here) hung the coroutine forever. After the fix it must
    return a boot_failure Finding within STARTUP_HARD_TIMEOUT_S instead."""
    monkeypatch.setattr(tst, "detect_live_peer_ports", lambda ports: [])
    monkeypatch.setattr(tst, "kill_stale_ports", lambda ports, client_id=None: None)
    monkeypatch.setattr(tst, "STARTUP_HARD_TIMEOUT_S", 0.2)

    never_set = asyncio.Event()

    async def _hanging_create_subprocess_exec(*args, **kwargs):
        await never_set.wait()  # simulates an AV scan / pixi resolution stall
        raise AssertionError("should never get here")  # pragma: no cover

    monkeypatch.setattr(tst.asyncio, "create_subprocess_exec", _hanging_create_subprocess_exec)

    ctx = tst.RunContext(repo_path=str(tmp_path), log_dir=tmp_path)

    # Outer bound is a generous safety net only -- if the fix regresses, this
    # call hangs until the outer wait_for cancels it and the test fails with
    # asyncio.TimeoutError instead of the expected Finding.
    finding = await asyncio.wait_for(tst.check_auth_reuse(ctx), timeout=5.0)
    assert finding.category == "boot_failure"
    assert "spawn" in finding.summary.lower()


@pytest.mark.asyncio
async def test_run_cycle_bounds_hung_subprocess_spawn(tmp_path, monkeypatch):
    """Same regression as above, but for run_cycle's boot leg."""
    monkeypatch.setattr(tst, "detect_live_peer_ports", lambda ports: [])
    monkeypatch.setattr(tst, "kill_stale_ports", lambda ports, client_id=None: None)
    monkeypatch.setattr(tst, "STARTUP_HARD_TIMEOUT_S", 0.2)

    never_set = asyncio.Event()

    async def _hanging_create_subprocess_exec(*args, **kwargs):
        await never_set.wait()
        raise AssertionError("should never get here")  # pragma: no cover

    monkeypatch.setattr(tst.asyncio, "create_subprocess_exec", _hanging_create_subprocess_exec)

    ctx = tst.RunContext(repo_path=str(tmp_path), slots=(), log_dir=tmp_path, with_tunnel_boot=True)
    tracker = tst.ConvergenceTracker()

    cycle = await asyncio.wait_for(tst.run_cycle(ctx, 1, tracker), timeout=5.0)
    assert cycle.tunnel_boot_ok is False
    boot_findings = [f for f in cycle.findings if f.category == "boot_failure"]
    assert boot_findings and "spawn" in boot_findings[0].summary.lower()
