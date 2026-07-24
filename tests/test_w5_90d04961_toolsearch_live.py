"""90d04961 — tool_search / list_plugins must surface LIVE tunnel-bridged tools.

Bug: a tunnel slot that only became active MID-session (e.g. the ``word`` slot
connecting after session start and now serving real tools via its live
``tools/list``) was not reliably reflected by the plugin/tool-search surface.

The fix keeps ``list_plugins`` (the tool that "reports 42 real tools") re-querying
each slot's LIVE ``tools/list`` on every call — never a session-start snapshot —
and flags a slot ``active``/``invocable`` ONLY when it actually returns ≥1 live
tool this fetch. It also surfaces the slot's live, slot-prefixed tool names so a
tool-search consumer can match a specific tunnel-bridged tool that appeared after
the initial snapshot.

Unit-level with mocks ONLY — the tunnel module's ``has_active_tunnel`` and
``_fetch_slot_tools`` are patched, so nothing touches a real socket/port/network.
"""

from __future__ import annotations

import pytest

# Import the full server package first so meridian.mcp.handler is loaded through
# its normal path (importing meridian.mcp.handler as the very first import trips a
# circular import with meridian.server). _dispatch_mcp_tool is imported per-test.
import meridian.server  # noqa: F401
from meridian.mcp.handler import _dispatch_mcp_tool
from meridian.routes import tunnel as tunnel_mod


_TENANT = {"id": "tenant-90d04961", "plan": "pro"}


def _install_live_tunnel(monkeypatch, slot_tools):
    """Patch the tunnel module so ``list_plugins`` sees a LIVE tunnel whose slots
    return exactly ``slot_tools`` (a ``{label: [tool-dict, ...]}`` mapping).

    A label absent from ``slot_tools`` returns no tools (simulating a slot whose
    inner server is not serving). No real socket/network is touched.
    """

    monkeypatch.setattr(tunnel_mod, "has_active_tunnel", lambda _tid: True)

    async def _fake_fetch_slot_tools(tenant_id, label, *, budget=None):
        return label, list(slot_tools.get(label, []))

    monkeypatch.setattr(tunnel_mod, "_fetch_slot_tools", _fake_fetch_slot_tools)


@pytest.mark.asyncio
async def test_mid_session_slot_tool_is_found(monkeypatch, db, tmp_path):
    """A tool that appears only AFTER the initial snapshot (the word slot connecting
    mid-session with real tools) is surfaced by list_plugins on the next call."""
    # Word slot came up mid-session and now serves real docx tools; the fs slot
    # was live from the start. This mapping is the "live tools/list" state.
    _install_live_tunnel(
        monkeypatch,
        {
            "fs": [{"name": "read_file"}, {"name": "write_file"}],
            "word": [
                {"name": "create_document"},
                {"name": "add_paragraph"},
                {"name": "get_text"},
            ],
        },
    )

    result = await _dispatch_mcp_tool(
        "list_plugins", {}, db, str(tmp_path), tenant=_TENANT
    )

    assert result["tunnel_active"] is True
    by_slot = {p["slot"]: p for p in result["plugins"]}

    # The mid-session word slot is now surfaced as active/invocable with its tools.
    word = by_slot["word"]
    assert word["active"] is True
    assert word["invocable"] is True
    assert word["tool_count"] == 3

    # The specific tool that appeared after the initial snapshot is now findable,
    # by its live slot-prefixed name (as the connector advertises it).
    all_tool_names = {t for p in result["plugins"] for t in p.get("tools", [])}
    assert "word__create_document" in all_tool_names
    assert "word__add_paragraph" in all_tool_names
    assert "word__get_text" in all_tool_names


@pytest.mark.asyncio
async def test_dead_slot_not_flagged_active(monkeypatch, db, tmp_path):
    """A slot that returns 0 live tools (never connected / dead inner server) must
    NOT be flagged active/invocable — calling its tools would 503."""
    _install_live_tunnel(
        monkeypatch,
        {
            "fs": [{"name": "read_file"}],
            # 'word' intentionally absent → 0 live tools this fetch.
        },
    )

    result = await _dispatch_mcp_tool(
        "list_plugins", {}, db, str(tmp_path), tenant=_TENANT
    )
    by_slot = {p["slot"]: p for p in result["plugins"]}

    word = by_slot["word"]
    assert word["active"] is False
    assert word["invocable"] is False
    assert word["tool_count"] == 0
    assert word["tools"] == []

    # The live fs slot is still correctly surfaced.
    fs = by_slot["fs"]
    assert fs["active"] is True
    assert fs["invocable"] is True
    assert "filesystem__read_file" in fs["tools"]


@pytest.mark.asyncio
async def test_requery_reflects_slot_appearing_after_first_call(monkeypatch, db, tmp_path):
    """Two successive list_plugins calls: the word slot is dark on the first and
    live on the second. The second call reflects it — proving the surface
    re-queries live tools/list rather than caching a session-start snapshot."""
    live_state = {"fs": [{"name": "read_file"}]}
    _install_live_tunnel(monkeypatch, live_state)

    first = await _dispatch_mcp_tool(
        "list_plugins", {}, db, str(tmp_path), tenant=_TENANT
    )
    first_word = next(p for p in first["plugins"] if p["slot"] == "word")
    assert first_word["active"] is False
    assert first_word["tools"] == []

    # Word slot connects mid-session and starts serving tools.
    live_state["word"] = [{"name": "create_document"}, {"name": "get_text"}]

    second = await _dispatch_mcp_tool(
        "list_plugins", {}, db, str(tmp_path), tenant=_TENANT
    )
    second_word = next(p for p in second["plugins"] if p["slot"] == "word")
    assert second_word["active"] is True
    assert second_word["tool_count"] == 2
    assert "word__create_document" in second_word["tools"]


@pytest.mark.asyncio
async def test_no_tunnel_no_active_plugins(monkeypatch, db, tmp_path):
    """Regression guard: with no active tunnel, no slot is active/invocable and no
    live tools are surfaced (mirrors the pre-existing no-tunnel contract)."""
    monkeypatch.setattr(tunnel_mod, "has_active_tunnel", lambda _tid: False)

    result = await _dispatch_mcp_tool(
        "list_plugins", {}, db, str(tmp_path), tenant=_TENANT
    )
    assert result["tunnel_active"] is False
    for p in result["plugins"]:
        assert p["active"] is False
        assert p["invocable"] is False
        assert p["tool_count"] == 0
        assert p["tools"] == []
