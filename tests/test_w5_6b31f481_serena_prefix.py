"""6b31f481 — serena / code-extractor (the extract slot) connector-prefix regression.

Follow-up to 4c01841b / 6f63145 (`fix(connector-source)`), which namespaced
tunneled tool NAMES and TITLES so every slot shows its plugin source in
claude.ai's Tool Permissions UI. The original regression test
(`test_list_tunnel_tools_namespaces_titles_for_source`) only exercised the
**filesystem** slot. The reported bug was that Serena / code-extractor tools
("Find Declaration", "Find Symbol", "Read Memory") appeared with ZERO connector
prefix, sitting unlabeled.

Re-verified against current code: the extract slot is NOT missing from
``SLOT_DISPLAY_NAMES`` (it maps ``extract`` → ``extractor``), and
``list_tunnel_tools`` resolves the display via ``SLOT_DISPLAY_NAMES.get(label,
label)`` — so no slot can ever fall through unprefixed. These tests pin that
already-correct behaviour for the extract/serena slot specifically so a future
edit to ``SLOT_DISPLAY_NAMES`` (e.g. dropping ``extract``) or the title helper
cannot silently regress serena tools back to unlabeled.

Unit-level only: ``_do_proxy`` is stubbed, so no real WebSocket / port / network
is touched.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.responses import Response

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian.routes import tunnel as tn


@pytest.fixture(autouse=True)
def _clean_bridge_state():
    """Reset the per-process tunnel registries this test touches, before & after."""
    def _reset():
        tn._tunnel_sockets.clear()
        tn._tunnel_code_sockets.clear()
        tn._tunnel_extract_sockets.clear()
        tn._tunnel_ppt_sockets.clear()
        tn._tunnel_word_sockets.clear()
        tn._tunnel_dc_sockets.clear()
        tn._tunnel_tool_routes.clear()
    _reset()
    yield
    _reset()


def _stub_proxy(monkeypatch, responder):
    """Patch tunnel._do_proxy with responder(label, method, params) -> dict|Response."""
    async def fake_do_proxy(tenant_id, method, path, query, headers, body,
                            sockets, pending, label):
        req = json.loads(body.decode())
        result = responder(label, req["method"], req.get("params") or {})
        if isinstance(result, Response):
            return result
        return Response(content=json.dumps(result).encode(), status_code=200,
                        media_type="application/json")
    monkeypatch.setattr(tn, "_do_proxy", fake_do_proxy)


# ---------------------------------------------------------------------------
# Static: the extract slot is registered and mapped (no unlabeled slot)
# ---------------------------------------------------------------------------

def test_extract_slot_has_display_name():
    """The extract slot (serena / mcp-server-code-extractor) is namespaced —
    it is in _TUNNEL_LABELS and mapped in SLOT_DISPLAY_NAMES, so its tools can
    never fall through as a raw/unlabeled prefix."""
    assert "extract" in tn._TUNNEL_LABELS
    assert tn.SLOT_DISPLAY_NAMES["extract"] == "extractor"
    # Every tunnel slot (including extract) has a display name — this is the
    # invariant that guarantees no slot leaks tools without a connector source.
    for label in tn._TUNNEL_LABELS:
        assert label in tn.SLOT_DISPLAY_NAMES, f"slot {label!r} missing display name"
    # The humanized title prefix the extract slot uses in tool titles.
    assert tn._display_pretty(tn.SLOT_DISPLAY_NAMES["extract"]) == "Extractor"


# ---------------------------------------------------------------------------
# The reported bug: serena / code-extractor tools show a connector prefix
# ---------------------------------------------------------------------------

def test_serena_extract_tools_get_namespaced_name_and_title(monkeypatch):
    """A serena / code-extractor tool advertising a bare ``title`` ("Find Symbol",
    "Find Declaration", "Read Memory") gets BOTH:
      * a connector-prefixed NAME   (extractor__find_symbol), and
      * a source-namespaced TITLE   ("Extractor: Find Symbol"),
    so claude.ai's Tool Permissions UI shows the plugin source instead of an
    unlabeled bare title. Mirrors the 4c01841b filesystem test for the extract
    slot. A serena tool with NO title is unchanged (its prefixed name carries the
    source); nested inputSchema param titles are left untouched."""
    tn._tunnel_extract_sockets["t1"] = object()

    def responder(label, method, params):
        assert method == "tools/list"
        if label == "extract":
            return {"result": {"tools": [
                {"name": "find_symbol", "title": "Find Symbol",
                 "inputSchema": {"properties": {"name_path": {"title": "Name Path"}}}},
                {"name": "find_referencing_symbols", "title": "Find Declaration"},
                {"name": "read_memory", "title": "Read Memory"},
                # A serena tool with no top-level title — its prefixed name alone
                # carries the source, so no title should be synthesized.
                {"name": "get_symbols_overview"},
            ]}}
        return {"result": {"tools": []}}

    _stub_proxy(monkeypatch, responder)
    tools = asyncio.run(tn.list_tunnel_tools("t1"))
    by_name = {t["name"]: t for t in tools}

    # Names are connector-namespaced with the extract slot's display name.
    assert "extractor__find_symbol" in by_name
    assert "extractor__find_referencing_symbols" in by_name
    assert "extractor__read_memory" in by_name
    assert "extractor__get_symbols_overview" in by_name
    # The bare (unprefixed) names must NOT be advertised — that was the bug.
    assert "find_symbol" not in by_name
    assert "read_memory" not in by_name

    # Titles are source-namespaced so the connector shows in the permissions UI.
    assert by_name["extractor__find_symbol"]["title"] == "Extractor: Find Symbol"
    assert by_name["extractor__find_referencing_symbols"]["title"] == "Extractor: Find Declaration"
    assert by_name["extractor__read_memory"]["title"] == "Extractor: Read Memory"

    # Nested inputSchema param titles are left alone (only the top-level tool
    # title is namespaced).
    assert (by_name["extractor__find_symbol"]["inputSchema"]["properties"]
            ["name_path"]["title"] == "Name Path")

    # A serena tool with no title gets none synthesized (prefixed name suffices).
    assert "title" not in by_name["extractor__get_symbols_overview"]

    # Routing cache keys the prefixed name back to the internal 'extract' label.
    routes = tn._tunnel_tool_routes["t1"]
    assert routes["extractor__find_symbol"] == "extract"
    assert routes["extractor__read_memory"] == "extract"


def test_serena_extract_title_not_double_prefixed(monkeypatch):
    """An already-namespaced serena title ("Extractor: ...") is not prefixed
    twice — the guard checks for the existing source prefix first."""
    tn._tunnel_extract_sockets["t1"] = object()

    def responder(label, method, params):
        if label == "extract":
            return {"result": {"tools": [
                {"name": "find_symbol", "title": "Extractor: Find Symbol"},
            ]}}
        return {"result": {"tools": []}}

    _stub_proxy(monkeypatch, responder)
    tools = asyncio.run(tn.list_tunnel_tools("t1"))
    by_name = {t["name"]: t for t in tools}
    assert by_name["extractor__find_symbol"]["title"] == "Extractor: Find Symbol"
