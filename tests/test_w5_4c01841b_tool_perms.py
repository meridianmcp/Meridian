"""4c01841b — claude.ai Tool Permissions screen hardening for tunnel tools.

The permission screen misbehaved (tools indistinguishable / looked duplicated /
missing their connector source) because ``list_tunnel_tools`` only namespaced the
top-level ``title`` — but claude.ai resolves a tool's display title as
``annotations.title`` -> top-level ``title`` -> humanized name (MCP title
precedence). Inner servers (filesystem, desktop-commander, serena) that carry
their human label in ``annotations.title`` therefore rendered the *bare* title,
so two slots exposing the same bare title (e.g. two "Read File"s) collapsed into
look-alike rows.

These tests pin the server-side hardening: every surfaced tool gets a unique,
slot-prefixed name AND both title carriers namespaced with the connector source,
without mutating the inner server's advertised objects.

The bridge helpers are exercised directly with ``_do_proxy`` stubbed, mirroring
tests/test_tunnel_bridge.py — no real WebSocket needed.
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
    """Reset per-process tunnel registries between tests."""
    def _reset():
        tn._tunnel_sockets.clear()
        tn._tunnel_code_sockets.clear()
        tn._tunnel_extract_sockets.clear()
        tn._tunnel_ppt_sockets.clear()
        tn._tunnel_word_sockets.clear()
        tn._tunnel_dc_sockets.clear()
        for s in tn._tunnel_custom_sockets.values():
            s.clear()
        tn._tunnel_tool_routes.clear()
        tn._slot_health.clear()
        tn._slot_status_detail.clear()
    _reset()
    yield
    _reset()


def _stub_proxy(monkeypatch, responder):
    """Patch tunnel._do_proxy with responder(label, method, params) -> dict|Response."""
    async def fake_do_proxy(tenant_id, method, path, query, headers, body, sockets, pending, label):
        req = json.loads(body.decode())
        result = responder(label, req["method"], req.get("params") or {})
        if isinstance(result, Response):
            return result
        return Response(content=json.dumps(result).encode(), status_code=200,
                        media_type="application/json")
    monkeypatch.setattr(tn, "_do_proxy", fake_do_proxy)


# ---------------------------------------------------------------------------
# _namespace_source_title — the shared title-namespacing helper
# ---------------------------------------------------------------------------

def test_namespace_source_title_prefixes_bare_title():
    assert tn._namespace_source_title("Read File", "Filesystem") == "Filesystem: Read File"


def test_namespace_source_title_idempotent_on_already_prefixed():
    # Re-listing must not double-prefix.
    assert tn._namespace_source_title("Filesystem: Read File", "Filesystem") is None


def test_namespace_source_title_prefixes_title_that_merely_starts_with_source_word():
    # 4c01841b — the exact "{src}: " guard (not a loose startswith(src)) means a
    # legitimate title that only *begins with* the source word is still namespaced.
    assert tn._namespace_source_title("Word count", "Word") == "Word: Word count"


def test_namespace_source_title_ignores_blank_and_non_string():
    assert tn._namespace_source_title(None, "Filesystem") is None
    assert tn._namespace_source_title("", "Filesystem") is None
    assert tn._namespace_source_title("   ", "Filesystem") is None
    assert tn._namespace_source_title(123, "Filesystem") is None


# ---------------------------------------------------------------------------
# list_tunnel_tools — annotations.title namespacing (the primary defect)
# ---------------------------------------------------------------------------

def test_annotations_title_is_namespaced_with_source(monkeypatch):
    """Inner server advertises its human label via annotations.title (the older
    ToolAnnotations convention). The permission screen reads that first, so it
    must be namespaced too — not just the top-level title."""
    tn._tunnel_sockets["t1"] = object()

    def responder(label, method, params):
        if label == "fs":
            return {"result": {"tools": [
                {"name": "read_file",
                 "annotations": {"title": "Read File", "readOnlyHint": True}},
            ]}}
        return {"result": {"tools": []}}

    _stub_proxy(monkeypatch, responder)
    tools = asyncio.run(tn.list_tunnel_tools("t1"))
    by_name = {t["name"]: t for t in tools}
    tool = by_name["filesystem__read_file"]
    assert tool["annotations"]["title"] == "Filesystem: Read File"
    # Other annotation fields are preserved unchanged.
    assert tool["annotations"]["readOnlyHint"] is True


def test_annotations_copy_does_not_mutate_inner_server_object(monkeypatch):
    """The inner server's advertised annotations dict must not be mutated in place
    (dict(tool) is shallow). We copy annotations before editing."""
    tn._tunnel_sockets["t1"] = object()
    original_annot = {"title": "Read File"}

    def responder(label, method, params):
        if label == "fs":
            return {"result": {"tools": [
                {"name": "read_file", "annotations": original_annot},
            ]}}
        return {"result": {"tools": []}}

    _stub_proxy(monkeypatch, responder)
    tools = asyncio.run(tn.list_tunnel_tools("t1"))
    tool = {t["name"]: t for t in tools}["filesystem__read_file"]
    assert tool["annotations"]["title"] == "Filesystem: Read File"
    # The object the responder handed back is untouched — it is NOT the same dict.
    assert original_annot == {"title": "Read File"}
    assert tool["annotations"] is not original_annot


def test_annotations_title_already_namespaced_not_double_prefixed(monkeypatch):
    tn._tunnel_sockets["t1"] = object()

    def responder(label, method, params):
        if label == "fs":
            return {"result": {"tools": [
                {"name": "read_file",
                 "annotations": {"title": "Filesystem: Read File"}},
            ]}}
        return {"result": {"tools": []}}

    _stub_proxy(monkeypatch, responder)
    tools = asyncio.run(tn.list_tunnel_tools("t1"))
    tool = {t["name"]: t for t in tools}["filesystem__read_file"]
    assert tool["annotations"]["title"] == "Filesystem: Read File"


def test_annotations_without_title_left_alone(monkeypatch):
    """An annotations dict with hints but no title keeps its shape (no title key
    injected — the prefixed NAME already carries the source)."""
    tn._tunnel_sockets["t1"] = object()

    def responder(label, method, params):
        if label == "fs":
            return {"result": {"tools": [
                {"name": "write_file", "annotations": {"readOnlyHint": False}},
            ]}}
        return {"result": {"tools": []}}

    _stub_proxy(monkeypatch, responder)
    tools = asyncio.run(tn.list_tunnel_tools("t1"))
    tool = {t["name"]: t for t in tools}["filesystem__write_file"]
    assert tool["annotations"] == {"readOnlyHint": False}
    assert "title" not in tool["annotations"]


def test_top_level_and_annotations_title_both_namespaced(monkeypatch):
    """A tool may carry both a top-level title and an annotations.title; both are
    namespaced independently."""
    tn._tunnel_sockets["t1"] = object()

    def responder(label, method, params):
        if label == "fs":
            return {"result": {"tools": [
                {"name": "read_file", "title": "Read File",
                 "annotations": {"title": "Read a File"}},
            ]}}
        return {"result": {"tools": []}}

    _stub_proxy(monkeypatch, responder)
    tools = asyncio.run(tn.list_tunnel_tools("t1"))
    tool = {t["name"]: t for t in tools}["filesystem__read_file"]
    assert tool["title"] == "Filesystem: Read File"
    assert tool["annotations"]["title"] == "Filesystem: Read a File"


# ---------------------------------------------------------------------------
# Uniqueness / no collapse — the "looks duplicated / not individually listed"
# symptom. Two slots each exposing a bare "Read File" must render as two
# distinct, source-labelled rows (unique names + unique display titles).
# ---------------------------------------------------------------------------

def test_same_bare_title_across_slots_yields_distinct_rows(monkeypatch):
    tn._tunnel_sockets["t1"] = object()          # filesystem
    tn._tunnel_dc_sockets["t1"] = object()       # desktop-commander

    def responder(label, method, params):
        if label in ("fs", "dc"):
            return {"result": {"tools": [
                {"name": "read_file", "annotations": {"title": "Read File"}},
            ]}}
        return {"result": {"tools": []}}

    _stub_proxy(monkeypatch, responder)
    tools = asyncio.run(tn.list_tunnel_tools("t1"))
    names = sorted(t["name"] for t in tools)
    # Unique, slot-prefixed names — no collapse.
    assert names == ["desktop-commander__read_file", "filesystem__read_file"]
    titles = sorted(t["annotations"]["title"] for t in tools)
    # Distinct, source-labelled display titles on the permission screen.
    assert titles == ["Desktop Commander: Read File", "Filesystem: Read File"]


def test_every_surfaced_tool_name_is_unique(monkeypatch):
    """Uniqueness invariant across a realistic multi-slot fetch — no duplicate
    names reach the client (duplicates collapse permission rows)."""
    tn._tunnel_sockets["t1"] = object()
    tn._tunnel_code_sockets["t1"] = object()
    tn._tunnel_extract_sockets["t1"] = object()

    def responder(label, method, params):
        if label == "fs":
            return {"result": {"tools": [
                {"name": "read_file"}, {"name": "list_directory"},
            ]}}
        if label == "code":
            return {"result": {"tools": [{"name": "search_graph"}]}}
        if label == "extract":
            return {"result": {"tools": [{"name": "get_symbols_tool"}]}}
        return {"result": {"tools": []}}

    _stub_proxy(monkeypatch, responder)
    tools = asyncio.run(tn.list_tunnel_tools("t1"))
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names))  # all unique
    assert set(names) == {
        "filesystem__read_file", "filesystem__list_directory",
        "codebase__search_graph", "extractor__get_symbols_tool",
    }


def test_duplicate_bare_name_within_one_slot_does_not_collapse_route(monkeypatch):
    """If an inner server pathologically advertises the same bare name twice, the
    first wins the route and the second is dropped (never overwrites the route to
    a different tool) — the prefixed name stays unique in the output."""
    tn._tunnel_sockets["t1"] = object()

    def responder(label, method, params):
        if label == "fs":
            return {"result": {"tools": [
                {"name": "read_file", "title": "First"},
                {"name": "read_file", "title": "Second"},
            ]}}
        return {"result": {"tools": []}}

    _stub_proxy(monkeypatch, responder)
    tools = asyncio.run(tn.list_tunnel_tools("t1"))
    fs_reads = [t for t in tools if t["name"] == "filesystem__read_file"]
    assert len(fs_reads) == 1
    assert fs_reads[0]["title"] == "Filesystem: First"
    assert tn._tunnel_tool_routes["t1"]["filesystem__read_file"] == "fs"
