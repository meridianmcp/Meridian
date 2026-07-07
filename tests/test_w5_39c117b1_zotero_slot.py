"""39c117b1 — zotero-mcp wired as a first-class bundled tunnel slot (same shape
as the 9665538a meridian-docs 'docs' slot). Server-side registry + routing."""
from __future__ import annotations

from meridian import tunnel_plugins as tp
from meridian.routes import tunnel as tn


# ---------------------------------------------------------------------------
# tunnel_plugins.py — registry
# ---------------------------------------------------------------------------

def test_zotero_slot_registered_in_slots_and_ports():
    assert "zotero" in tp.SLOTS
    assert tp.DEFAULT_ZOTERO_PORT == 8819
    # Reserved so a custom plugin can't grab the port.
    assert tp.DEFAULT_ZOTERO_PORT in tp._BUILTIN_DEFAULT_PORTS


def test_zotero_builtin_plugin_entry_shape():
    by_name = {p["name"]: p for p in tp.BUILTIN_PLUGINS}
    assert "zotero-mcp" in by_name
    z = by_name["zotero-mcp"]
    assert z["slot"] == "zotero"
    assert z["port"] == tp.DEFAULT_ZOTERO_PORT
    assert z["command"] == ["uvx", "zotero-mcp"]
    assert z["env"] == {"ZOTERO_LOCAL": "true"}
    assert z["builtin"] is True
    # Opt-in like the other Office slots (not auto-on).
    assert z["enabled"] is False
    assert "zotero-mcp" in tp.builtin_names()


def test_zotero_is_a_reserved_builtin_name():
    # A custom plugin cannot reuse the reserved slot/name.
    assert tp.is_reserved_custom_name("zotero") is True
    assert tp.is_reserved_custom_name("zotero-mcp") is True


def test_zotero_in_bundled_catalog():
    # a8a54fe9 catalog now marks zotero-mcp bundled (39c117b1 shipped it).
    by_name = {t["name"]: t for t in tp.known_plugin_tools()}
    assert by_name["zotero-mcp"]["bundled"] is True
    assert by_name["zotero-mcp"]["slot"] == "zotero"
    assert by_name["zotero-mcp"]["owner_item"] is None


# ---------------------------------------------------------------------------
# routes/tunnel.py — routing
# ---------------------------------------------------------------------------

def test_zotero_label_and_display_name():
    assert "zotero" in tn._TUNNEL_LABELS
    assert tn.SLOT_DISPLAY_NAMES["zotero"] == "zotero-mcp"


def test_zotero_label_maps_to_its_own_registries():
    sockets, pending = tn._label_maps("zotero")
    assert sockets is tn._tunnel_zotero_sockets
    assert pending is tn._pending_zotero_reqs


def test_zotero_socket_detected_by_has_active_tunnel():
    tid = "t-zotero-test"
    assert tn.has_active_tunnel(tid) is False
    tn._tunnel_zotero_sockets[tid] = object()
    try:
        assert tn.has_active_tunnel(tid) is True
        assert tid in tn.active_tunnel_tenant_ids()
    finally:
        tn._tunnel_zotero_sockets.pop(tid, None)


def test_zotero_ws_route_registered():
    import meridian.server as _srv
    paths = [getattr(r, "path", "") for r in _srv.app.routes]
    assert any("/tunnel-zotero/" in p for p in paths)
    assert any("/zotero/mcp/" in p for p in paths)
