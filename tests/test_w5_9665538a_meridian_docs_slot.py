"""9665538a — meridian-docs bundled tunnel slot.

meridian-docs is the extracted, stdlib-only OOXML (DOCX) doc parser packaged as
its own ``uvx meridian-docs`` MCP server (``extensions/meridian-docs``). This
item wires it as a first-class *bundled* tunnel slot ("docs") alongside the
Office slots (ppt/word/dc): a ``BUILTIN_PLUGINS`` entry in
``meridian/tunnel_plugins.py`` plus the per-slot server routing in
``meridian/routes/tunnel.py`` (a WebSocket relay + an HTTP proxy, mirroring the
word/docx wiring exactly).

Everything here is unit-level with fakes — no real servers/ports/network/sleeps.
"""
from __future__ import annotations

import asyncio
import base64
import types

import pytest

from meridian import tunnel_plugins as tp

# Import server first to avoid the handler/server import cycle, then the router.
import meridian.server  # noqa: F401
from meridian.routes import tunnel as tn


# ---------------------------------------------------------------------------
# Registry — tunnel_plugins.py
# ---------------------------------------------------------------------------

def test_docs_slot_registered_in_builtin_plugins():
    """The docs slot exists in BUILTIN_PLUGINS with the expected shape, mirroring
    the word/office entries (opt-in plugin, uvx launcher, stateless)."""
    by_name = {p["name"]: p for p in tp.BUILTIN_PLUGINS}
    assert "meridian-docs" in by_name, "meridian-docs must be a bundled builtin slot"
    docs = by_name["meridian-docs"]

    assert docs["slot"] == "docs"
    assert docs["url_prefix"] == "/docs"
    assert docs["port"] == tp.DEFAULT_DOCS_PORT == 8818
    # Launched via `uvx meridian-docs` (the extensions/meridian-docs console entry).
    assert docs["command"] == ["uvx", "meridian-docs"]
    # Opt-in like the Office slots: off by default, not a core tool.
    assert docs["enabled"] is False
    assert docs["core"] is False
    assert docs["builtin"] is True
    # meridian-docs exposes bare tool names → no client prefix (the bridge
    # namespaces via SLOT_DISPLAY_NAMES). Stateless one-shot relay.
    assert docs["prefix"] is None
    assert docs["session_mode"] == "stateless"
    assert isinstance(docs["description"], str) and docs["description"]
    # Same override-carrier shape as the office slots.
    assert docs["description_overrides"] == {}
    assert docs.get("env") == {}


def test_docs_slot_in_slots_tuple_and_ports():
    assert "docs" in tp.SLOTS
    # Its default port is registered as a built-in default (so a custom plugin
    # can't reuse it and collide with the slot's local proxy).
    assert tp.DEFAULT_DOCS_PORT in tp._BUILTIN_DEFAULT_PORTS
    # No collision with any other built-in default port.
    ports = [p["port"] for p in tp.BUILTIN_PLUGINS]
    assert len(ports) == len(set(ports)), "built-in slot ports must be unique"
    # Below the custom auto-assign start, above the pre-allocated custom slots.
    assert max(tp.CUSTOM_SLOT_PORTS.values()) < tp.DEFAULT_DOCS_PORT < tp._CUSTOM_PORT_START


def test_docs_slot_resolves_via_resolve_plugins_and_by_slot():
    """resolve_plugins carries the docs slot with defaults; plugin_by_slot finds it."""
    resolved = tp.resolve_plugins(None)
    by_slot = {p["slot"]: p for p in resolved}
    assert "docs" in by_slot
    assert by_slot["docs"]["name"] == "meridian-docs"
    assert by_slot["docs"]["command"] == ["uvx", "meridian-docs"]

    got = tp.plugin_by_slot(None, "docs")
    assert got is not None and got["name"] == "meridian-docs"


def test_docs_slot_disabled_by_default_but_enableable():
    # Off by default → not in active_plugins.
    active = [p["name"] for p in tp.active_plugins(None)]
    assert "meridian-docs" not in active

    # A tenant override enables it (and can swap the command / port), and the
    # immutable slot/url_prefix survive the merge.
    cfg = {"meridian-docs": {"enabled": True, "port": 9099,
                             "command": "uvx meridian-docs --verbose"}}
    active = tp.active_plugins(cfg)
    docs = {p["slot"]: p for p in active}.get("docs")
    assert docs is not None
    assert docs["enabled"] is True
    assert docs["port"] == 9099
    assert docs["command"] == ["uvx", "meridian-docs", "--verbose"]
    assert docs["url_prefix"] == "/docs"  # immutable


def test_docs_names_are_reserved_for_custom_plugins():
    """The slot name ('docs') and the plugin display name ('meridian-docs') are
    reserved — a custom plugin may not shadow either (it would be a slot override,
    not a stand-alone custom plugin)."""
    assert tp.is_reserved_custom_name("docs") is True
    assert tp.is_reserved_custom_name("meridian-docs") is True
    assert tp.is_reserved_custom_name("MERIDIAN-DOCS") is True  # case-insensitive

    # validate_custom_plugin rejects both as reserved.
    _entry, err = tp.validate_custom_plugin("meridian-docs", "uvx x")
    assert err is not None and "built-in" in err

    # And a custom plugin may not reuse the docs default port.
    _entry, err = tp.validate_custom_plugin(
        "mydocs", "uvx x", tp.DEFAULT_DOCS_PORT)
    assert err is not None and "built-in" in err


# ---------------------------------------------------------------------------
# Server routing — routes/tunnel.py
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_docs_state():
    """Reset the per-process docs registries around each test."""
    def _reset():
        tn._tunnel_docs_sockets.clear()
        tn._pending_docs_reqs.clear()
        tn._tunnel_tool_routes.clear()
    _reset()
    yield
    _reset()


def test_docs_slot_has_socket_and_pending_registries():
    assert isinstance(tn._tunnel_docs_sockets, dict)
    assert isinstance(tn._pending_docs_reqs, dict)


def test_docs_label_maps_to_own_registries():
    sockets, pending = tn._label_maps("docs")
    assert sockets is tn._tunnel_docs_sockets
    assert pending is tn._pending_docs_reqs


def test_docs_in_tunnel_labels_and_display_names():
    assert "docs" in tn._TUNNEL_LABELS
    # Bridge namespaces docs-slot tools as "meridian-docs__<tool>".
    assert tn.SLOT_DISPLAY_NAMES["docs"] == "meridian-docs"


def test_docs_socket_counts_toward_active_tunnel():
    assert tn.has_active_tunnel("t-docs") is False
    tn._tunnel_docs_sockets["t-docs"] = object()
    assert tn.has_active_tunnel("t-docs") is True
    assert "t-docs" in tn.active_tunnel_tenant_ids()


def test_tunnel_status_reports_docs_active():
    status = asyncio.run(tn.tunnel_status("t-docs"))
    assert status["docs_active"] is False
    tn._tunnel_docs_sockets["t-docs"] = object()
    status = asyncio.run(tn.tunnel_status("t-docs"))
    assert status["docs_active"] is True


class _FakeReq:
    """Minimal Starlette Request stand-in for the proxy route wrappers."""

    def __init__(self, path, query="", method="POST", headers=None, body=b""):
        self.method = method
        self.headers = headers or {}
        self.url = types.SimpleNamespace(path=path, query=query)
        self._body = body

    async def body(self):
        return self._body


class _FakeWS:
    """Resolves the pending future inline when the server sends a request."""

    def __init__(self, pending, response):
        self._pending = pending
        self._response = response

    async def send_json(self, payload):
        fut = self._pending.get(payload["id"])
        if fut is not None and not fut.done():
            fut.set_result({**self._response, "id": payload["id"]})


def test_docs_mcp_proxy_503_when_not_hosted(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: False)
    resp = asyncio.run(tn.docs_mcp_proxy("t1", _FakeReq("/docs/mcp/t1")))
    assert resp.status_code == 503
    assert b"hosted mode" in resp.body


def test_docs_mcp_proxy_503_when_no_socket(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: True)
    resp = asyncio.run(tn.docs_mcp_proxy("t1", _FakeReq("/docs/mcp/t1")))
    assert resp.status_code == 503
    assert b"docs tunnel not connected" in resp.body


def test_docs_mcp_proxy_roundtrip_via_fake_socket(monkeypatch):
    """A connected docs socket relays a request and returns the inner response —
    proving the /docs/mcp route is wired to the docs socket/pending registries."""
    monkeypatch.setattr(tn, "_hosted_mode", lambda: True)
    response = {"status": 200, "headers": {"content-type": "application/json"},
                "body": base64.b64encode(b'{"ok":1}').decode()}
    tn._tunnel_docs_sockets["t1"] = _FakeWS(tn._pending_docs_reqs, response)
    resp = asyncio.run(tn.docs_mcp_proxy("t1", _FakeReq("/docs/mcp/t1")))
    assert resp.status_code == 200
    assert resp.body == b'{"ok":1}'


def test_docs_mcp_proxy_subpath_roundtrip(monkeypatch):
    monkeypatch.setattr(tn, "_hosted_mode", lambda: True)
    response = {"status": 200, "headers": {"content-type": "application/json"},
                "body": base64.b64encode(b'{"ok":2}').decode()}
    tn._tunnel_docs_sockets["t1"] = _FakeWS(tn._pending_docs_reqs, response)
    resp = asyncio.run(
        tn.docs_mcp_proxy_subpath("t1", "mcp", _FakeReq("/docs/mcp/t1/mcp")))
    assert resp.status_code == 200
    assert resp.body == b'{"ok":2}'
