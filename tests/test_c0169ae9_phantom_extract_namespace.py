"""Regression test for c0169ae9.

Sprint item: "Fix phantom 'meridian-extract:' tool namespace — real slot is
code-extractor / extractor__ prefix, no such registered plugin exists."

Investigation confirmed the narrow premise: `tunnel_plugins.BUILTIN_PLUGINS`
has never registered any plugin named "meridian-extract" (or "meridian-code").
The real built-in on the "extract" slot is named "code-extractor" (default
launcher: Serena), and the server-side bridge (routes/tunnel.py) namespaces
its tools as `extractor__*` per `SLOT_DISPLAY_NAMES` (the "code" slot's
plugin, "code-intel", is namespaced `codebase__*`).

BUT the investigation also found this was not *purely* a client-side stale
config issue, as an earlier pass had concluded: this repo's own code contained
two live, in-repo sources of the exact phantom name:

1. ``meridian/tunnel_client.py``'s ``_index_code_dir`` printed a real stderr
   message on a partial/failed code-intel index telling the user to
   "Use meridian-extract (Serena) for reliable code-intel" -- a name that has
   never existed in the registry.
2. ``meridian/agent_defaults.py``'s ``DEFAULT_AGENT_INSTRUCTIONS`` (injected
   into every project and returned by ``start_session``) named
   `meridian-code` / `meridian-extractor` as the local STDIO tool identifiers
   in its d659200c STDIO-unreachable section -- inconsistent with the same
   file's own (correct) b2d312b1 section, which already used `codebase__*`
   and `extractor__*`.

Both were genuine in-repo documentation bugs that would send any future
session looking for a tool/process under a name that was never real. Fixed
by swapping the stale names for the real, currently-registered ones
everywhere they appeared as live (non-historical) text.
"""
from __future__ import annotations

import asyncio

import meridian.server  # noqa: F401 -- import first to avoid handler/server import cycle
from meridian import tunnel_client as tc
from meridian import tunnel_plugins
from meridian.agent_defaults import DEFAULT_AGENT_INSTRUCTIONS
from meridian.routes import tunnel as tn


def test_no_plugin_named_meridian_extract_in_registry():
    """Ground truth: no plugin has ever been registered under 'meridian-extract'
    (or 'meridian-code') in BUILTIN_PLUGINS."""
    names = {p["name"] for p in tunnel_plugins.BUILTIN_PLUGINS}
    assert "meridian-extract" not in names
    assert "meridian-extractor" not in names
    assert "meridian-code" not in names
    assert "code-extractor" in names


def test_extract_slot_plugin_is_code_extractor():
    """The 'extract' slot's registered plugin is named 'code-extractor'."""
    by_slot = {p["slot"]: p for p in tunnel_plugins.BUILTIN_PLUGINS}
    assert by_slot["extract"]["name"] == "code-extractor"


def test_slot_display_names_confirm_extractor_and_codebase_prefixes():
    """The server bridge's real tool-name prefixes: 'extract' -> 'extractor',
    'code' -> 'codebase' (not 'meridian-extract' / 'meridian-code')."""
    assert tn.SLOT_DISPLAY_NAMES["extract"] == "extractor"
    assert tn.SLOT_DISPLAY_NAMES["code"] == "codebase"
    assert "meridian-extract" not in tn.SLOT_DISPLAY_NAMES.values()
    assert "meridian-code" not in tn.SLOT_DISPLAY_NAMES.values()


def test_index_code_dir_stderr_message_does_not_reference_phantom_slot(monkeypatch, capsys):
    """_index_code_dir's real, user-facing stderr message on a partial/failed
    index must not tell the user to use a slot name ('meridian-extract') that
    was never registered -- it must name the real code-extractor slot /
    extractor__* tool prefix instead."""
    import httpx as _httpx

    _call_count = [0]

    class FakeResp:
        def __init__(self, body: bytes):
            self.status_code = 200
            self.content = body

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, **kw):
            _call_count[0] += 1
            if _call_count[0] == 1:
                return FakeResp(b'{"jsonrpc":"2.0","id":"probe","result":{"tools":[]}}')
            return FakeResp(
                b'{"jsonrpc":"2.0","id":"idx","error":{"code":-32603,"message":"scan partial"}}'
            )

    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)
    asyncio.run(tc._index_code_dir(8809, "/repo"))

    captured = capsys.readouterr()
    assert "scan partial" in captured.err
    assert "meridian-extract" not in captured.err
    # The corrected message names the real, registered slot/tool prefix.
    assert "code-extractor" in captured.err
    assert "extractor__" in captured.err


def test_agent_instructions_have_no_phantom_tool_names():
    """The live, injected DEFAULT_AGENT_INSTRUCTIONS text must not reference
    'meridian-code' / 'meridian-extractor' / 'meridian-extract' as tool or
    process identifiers -- those names were never registered."""
    text = DEFAULT_AGENT_INSTRUCTIONS
    assert "meridian-code" not in text
    assert "meridian-extractor" not in text
    assert "meridian-extract" not in text
    # The real identifiers must be present instead.
    assert "codebase__" in text
    assert "extractor__" in text
