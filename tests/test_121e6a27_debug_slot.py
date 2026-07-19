"""121e6a27 — @debugmcp/mcp-debugger wired as a first-class bundled tunnel slot
(same shape as the 9665538a meridian-docs / 39c117b1 zotero / 469d89b4 outputs
slots). npm-published + npx-ready (no local clone needed), 7-language DAP
debugger. Server-side registry + routing."""
from __future__ import annotations

from meridian import tunnel_plugins as tp
from meridian.routes import tunnel as tn


# ---------------------------------------------------------------------------
# tunnel_plugins.py — registry
# ---------------------------------------------------------------------------

def test_debug_slot_registered_in_slots_and_ports():
    assert "debug" in tp.SLOTS
    assert tp.DEFAULT_DEBUG_PORT == 8821
    # Reserved so a custom plugin can't grab the port.
    assert tp.DEFAULT_DEBUG_PORT in tp._BUILTIN_DEFAULT_PORTS


def test_debug_port_no_collision_with_other_builtins():
    ports = [p["port"] for p in tp.BUILTIN_PLUGINS]
    assert len(ports) == len(set(ports)), "built-in slot ports must be unique"


def test_debug_custom_port_start_above_debug_port():
    # _CUSTOM_PORT_START was bumped from 8821 to 8822 to make room for debug.
    assert tp._CUSTOM_PORT_START == 8822
    assert tp.DEFAULT_DEBUG_PORT < tp._CUSTOM_PORT_START


def test_debug_builtin_plugin_entry_shape():
    by_name = {p["name"]: p for p in tp.BUILTIN_PLUGINS}
    assert "mcp-debugger" in by_name, "mcp-debugger must be a bundled builtin slot"
    d = by_name["mcp-debugger"]

    assert d["slot"] == "debug"
    assert d["url_prefix"] == "/debug"
    assert d["port"] == tp.DEFAULT_DEBUG_PORT
    # npx-ready — no local clone / extensions checkout, unlike meridian-docs /
    # meridian-outputs which spawn from a local --from path.
    assert d["command"] == ["npx", "-y", "@debugmcp/mcp-debugger"]
    # Opt-in like the other non-core slots: off by default, not a core tool.
    assert d["enabled"] is False
    assert d["core"] is False
    assert d["builtin"] is True
    # mcp-debugger exposes bare tool names → no client prefix (the bridge
    # namespaces via SLOT_DISPLAY_NAMES).
    assert d["prefix"] is None
    # A debug session is stateful across requests (breakpoints/call stack), so
    # this slot is persistent (like Desktop Commander), not one-shot stateless.
    assert d["session_mode"] == "persistent"
    assert isinstance(d["description"], str) and d["description"]
    assert d["description_overrides"] == {}
    assert d.get("env") == {}


def test_debug_name_in_builtin_names():
    assert "mcp-debugger" in tp.builtin_names()


def test_debug_is_a_reserved_builtin_name():
    # A custom plugin cannot reuse the reserved slot/name.
    assert tp.is_reserved_custom_name("debug") is True
    assert tp.is_reserved_custom_name("mcp-debugger") is True


def test_debug_in_bundled_catalog():
    # 121e6a27 ships mcp-debugger as a first-class built-in; catalog must
    # reflect bundled=True so unbundled_plugin_tools() does not list it as a gap.
    by_name = {t["name"]: t for t in tp.known_plugin_tools()}
    assert "mcp-debugger" in by_name, "mcp-debugger must appear in KNOWN_PLUGIN_TOOLS"
    d = by_name["mcp-debugger"]
    assert d["bundled"] is True
    assert d["slot"] == "debug"
    assert d["owner_item"] is None
    assert d["runtime"] == "npx"
    assert d["package"] == "@debugmcp/mcp-debugger"


def test_debug_resolves_via_resolve_plugins():
    resolved = tp.resolve_plugins(None)
    by_slot = {p["slot"]: p for p in resolved}
    assert "debug" in by_slot
    d = by_slot["debug"]
    assert d["name"] == "mcp-debugger"
    assert d["enabled"] is False  # opt-in default


def test_debug_plugin_by_slot():
    p = tp.plugin_by_slot(None, "debug")
    assert p is not None
    assert p["name"] == "mcp-debugger"


# ---------------------------------------------------------------------------
# routes/tunnel.py — routing
# ---------------------------------------------------------------------------

def test_debug_label_and_display_name():
    assert "debug" in tn._TUNNEL_LABELS
    assert tn.SLOT_DISPLAY_NAMES["debug"] == "mcp-debugger"


def test_debug_label_maps_to_its_own_registries():
    sockets, pending = tn._label_maps("debug")
    assert sockets is tn._tunnel_debug_sockets
    assert pending is tn._pending_debug_reqs


def test_debug_socket_detected_by_has_active_tunnel():
    tid = "t-debug-test-121e6a27"
    assert tn.has_active_tunnel(tid) is False
    tn._tunnel_debug_sockets[tid] = object()
    try:
        assert tn.has_active_tunnel(tid) is True
        assert tid in tn.active_tunnel_tenant_ids()
    finally:
        tn._tunnel_debug_sockets.pop(tid, None)


def test_debug_ws_route_registered():
    import meridian.server as _srv
    paths = [getattr(r, "path", "") for r in _srv.app.routes]
    assert any("/tunnel-debug/" in p for p in paths)
    assert any("/debug/mcp/" in p for p in paths)


def test_slot_display_names_cover_debug():
    """Every tunnel slot (including debug) has a display name — invariant check."""
    for label in tn._TUNNEL_LABELS:
        assert label in tn.SLOT_DISPLAY_NAMES, f"slot {label!r} missing display name"
