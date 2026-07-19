"""469d89b4 — meridian-outputs wired as a first-class bundled tunnel slot (same
shape as the 9665538a meridian-docs 'docs' slot and 39c117b1 zotero slot).
Server-side registry + routing."""
from __future__ import annotations

from meridian import tunnel_plugins as tp
from meridian.routes import tunnel as tn


# ---------------------------------------------------------------------------
# tunnel_plugins.py — registry
# ---------------------------------------------------------------------------

def test_outputs_slot_registered_in_slots_and_ports():
    assert "outputs" in tp.SLOTS
    assert tp.DEFAULT_OUTPUTS_PORT == 8820
    # Reserved so a custom plugin can't grab the port.
    assert tp.DEFAULT_OUTPUTS_PORT in tp._BUILTIN_DEFAULT_PORTS


def test_outputs_port_no_collision_with_other_builtins():
    ports = [p["port"] for p in tp.BUILTIN_PLUGINS]
    assert len(ports) == len(set(ports)), "built-in slot ports must be unique"


def test_outputs_custom_port_start_above_outputs_port():
    # _CUSTOM_PORT_START was bumped from 8820 to 8821 to make room for outputs,
    # then to 8822 (121e6a27) to make room for the debug slot.
    assert tp._CUSTOM_PORT_START == 8822
    assert tp.DEFAULT_OUTPUTS_PORT < tp._CUSTOM_PORT_START


def test_outputs_builtin_plugin_entry_shape():
    by_name = {p["name"]: p for p in tp.BUILTIN_PLUGINS}
    assert "meridian-outputs" in by_name, "meridian-outputs must be a bundled builtin slot"
    o = by_name["meridian-outputs"]

    assert o["slot"] == "outputs"
    assert o["url_prefix"] == "/outputs"
    assert o["port"] == tp.DEFAULT_OUTPUTS_PORT
    # 469d89b4 — launched via `uvx --from <local-path> meridian-outputs-mcp` (NOT bare
    # `uvx meridian-outputs`; the package is not on PyPI, so the local extensions/
    # meridian-outputs source dir must be supplied via --from).
    # The entry-point is "meridian-outputs-mcp" (not "meridian-outputs") to prevent
    # uvx from treating the trailing command as a PyPI registry lookup (same as 58a044c7).
    cmd = o["command"]
    assert cmd[0] == "uvx"
    assert cmd[1] == "--from"
    assert cmd[3] == "meridian-outputs-mcp"
    # The --from path points at the local extensions/meridian-outputs directory.
    assert "extensions" in cmd[2] and "meridian-outputs" in cmd[2]
    # Opt-in like the other non-core slots: off by default, not a core tool.
    assert o["enabled"] is False
    assert o["core"] is False
    assert o["builtin"] is True
    # meridian-outputs exposes bare tool names → no client prefix (the bridge
    # namespaces via SLOT_DISPLAY_NAMES). Stateless one-shot relay.
    assert o["prefix"] is None
    assert o["session_mode"] == "stateless"
    assert isinstance(o["description"], str) and o["description"]
    assert o["description_overrides"] == {}
    assert o.get("env") == {}


def test_outputs_name_in_builtin_names():
    assert "meridian-outputs" in tp.builtin_names()


def test_outputs_is_a_reserved_builtin_name():
    # A custom plugin cannot reuse the reserved slot/name.
    assert tp.is_reserved_custom_name("outputs") is True
    assert tp.is_reserved_custom_name("meridian-outputs") is True


def test_outputs_in_bundled_catalog():
    # 469d89b4 ships meridian-outputs as a first-class built-in; catalog must
    # reflect bundled=True so unbundled_plugin_tools() does not list it as a gap.
    by_name = {t["name"]: t for t in tp.known_plugin_tools()}
    assert "meridian-outputs" in by_name, "meridian-outputs must appear in KNOWN_PLUGIN_TOOLS"
    o = by_name["meridian-outputs"]
    assert o["bundled"] is True
    assert o["slot"] == "outputs"
    assert o["owner_item"] is None


def test_outputs_resolves_via_resolve_plugins():
    resolved = tp.resolve_plugins(None)
    by_slot = {p["slot"]: p for p in resolved}
    assert "outputs" in by_slot
    o = by_slot["outputs"]
    assert o["name"] == "meridian-outputs"
    assert o["enabled"] is False  # opt-in default


def test_outputs_plugin_by_slot():
    p = tp.plugin_by_slot(None, "outputs")
    assert p is not None
    assert p["name"] == "meridian-outputs"


# ---------------------------------------------------------------------------
# routes/tunnel.py — routing
# ---------------------------------------------------------------------------

def test_outputs_label_and_display_name():
    assert "outputs" in tn._TUNNEL_LABELS
    assert tn.SLOT_DISPLAY_NAMES["outputs"] == "meridian-outputs"


def test_outputs_label_maps_to_its_own_registries():
    sockets, pending = tn._label_maps("outputs")
    assert sockets is tn._tunnel_outputs_sockets
    assert pending is tn._pending_outputs_reqs


def test_outputs_socket_detected_by_has_active_tunnel():
    tid = "t-outputs-test-469d89b4"
    assert tn.has_active_tunnel(tid) is False
    tn._tunnel_outputs_sockets[tid] = object()
    try:
        assert tn.has_active_tunnel(tid) is True
        assert tid in tn.active_tunnel_tenant_ids()
    finally:
        tn._tunnel_outputs_sockets.pop(tid, None)


def test_outputs_ws_route_registered():
    import meridian.server as _srv
    paths = [getattr(r, "path", "") for r in _srv.app.routes]
    assert any("/tunnel-outputs/" in p for p in paths)
    assert any("/outputs/mcp/" in p for p in paths)


def test_slot_display_names_cover_outputs():
    """Every tunnel slot (including outputs) has a display name — invariant check."""
    for label in tn._TUNNEL_LABELS:
        assert label in tn.SLOT_DISPLAY_NAMES, f"slot {label!r} missing display name"
