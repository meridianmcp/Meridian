"""2ce5bc76 — tests for:
  (1) code-graph staleness-fingerprint detection (fires when the source has
      changed since indexing, silent when unchanged), and
  (2) the prospect_symbol fallback chain tool (each rung: search_graph
      succeeds -> stops there; search_graph empty -> tries Serena;
      Serena also empty -> falls back to grep-style semantic search;
      all via stubbed tunnel calls, mirroring test_searchgraph_projectid_hint.py).

These tests are pure unit tests — no real DB, no real tunnel.
"""
from __future__ import annotations

import json
import pytest
from meridian.routes import tunnel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text_result(obj: object) -> dict:
    """Wrap a Python object as an MCP tools/call text-content result."""
    return {"content": [{"type": "text", "text": json.dumps(obj)}]}


# ---------------------------------------------------------------------------
# Part 1 — staleness fingerprint helpers
# ---------------------------------------------------------------------------

class TestExtractGraphFingerprint:
    def test_extracts_git_commit(self):
        result = _text_result({"git_commit": "abc123", "other": "x"})
        fp = tunnel._extract_graph_fingerprint(result)
        assert fp == "abc123"

    def test_extracts_indexed_at(self):
        result = _text_result({"indexed_at": "2026-07-13T00:00:00Z"})
        fp = tunnel._extract_graph_fingerprint(result)
        assert fp == "2026-07-13T00:00:00Z"

    def test_extracts_from_nested_status(self):
        result = _text_result({"status": {"git_commit": "def456"}})
        fp = tunnel._extract_graph_fingerprint(result)
        assert fp == "def456"

    def test_returns_none_when_absent(self):
        result = _text_result({"results": [{"file": "foo.py"}]})
        fp = tunnel._extract_graph_fingerprint(result)
        assert fp is None

    def test_returns_none_for_non_dict(self):
        assert tunnel._extract_graph_fingerprint(None) is None
        assert tunnel._extract_graph_fingerprint("bare string") is None
        assert tunnel._extract_graph_fingerprint([]) is None

    def test_prefers_git_commit_over_indexed_at(self):
        # git_commit appears first in the key preference list
        result = _text_result({"git_commit": "COMMIT", "indexed_at": "TS"})
        fp = tunnel._extract_graph_fingerprint(result)
        assert fp == "COMMIT"


class TestGraphFingerprintKey:
    def test_uses_project_id_when_present(self):
        key = tunnel._graph_fingerprint_key("tenant-1", {"project_id": "repo-slug"})
        assert key == "tenant-1:repo-slug"

    def test_falls_back_to_wildcard_when_absent(self):
        key = tunnel._graph_fingerprint_key("tenant-1", {})
        assert key == "tenant-1:*"

    def test_falls_back_to_wildcard_when_no_args(self):
        key = tunnel._graph_fingerprint_key("tenant-1", None)
        assert key == "tenant-1:*"

    def test_uses_project_arg_when_no_project_id(self):
        key = tunnel._graph_fingerprint_key("t", {"project": "some-repo"})
        assert key == "t:some-repo"


class TestAnnotateGraphResultStaleness:
    """Tests for _annotate_graph_result_staleness."""

    @pytest.mark.asyncio
    async def test_injects_staleness_when_fingerprints_differ(self, monkeypatch):
        tenant_id = "tenant-stale"
        # Stored fingerprint from last index_repository.
        tunnel._code_graph_fingerprints["tenant-stale:myrepo"] = "OLD-COMMIT"

        async def _fake_fetch(tid, args):
            return "NEW-COMMIT"

        monkeypatch.setattr(tunnel, "_fetch_graph_current_fingerprint", _fake_fetch)

        result = _text_result({"results": [{"file": "foo.py", "line": 10}]})
        enriched = await tunnel._annotate_graph_result_staleness(
            tenant_id, "codebase__search_graph",
            {"project_id": "myrepo", "query": "foo"},
            result,
        )
        # Original result intact.
        assert "content" in enriched
        # Staleness annotation present.
        stale = enriched.get("_graph_staleness")
        assert stale is not None
        assert stale["stale"] is True
        assert "OLD-COMMIT" in stale["warning"]
        assert "NEW-COMMIT" in stale["warning"]
        assert "wrong" in stale["warning"].lower() or "stale" in stale["warning"].lower()

        # Cleanup.
        tunnel._code_graph_fingerprints.pop("tenant-stale:myrepo", None)

    @pytest.mark.asyncio
    async def test_no_annotation_when_fingerprints_match(self, monkeypatch):
        tunnel._code_graph_fingerprints["tenant-fresh:proj"] = "SAME-COMMIT"

        async def _fake_fetch(tid, args):
            return "SAME-COMMIT"

        monkeypatch.setattr(tunnel, "_fetch_graph_current_fingerprint", _fake_fetch)

        result = _text_result({"results": []})
        out = await tunnel._annotate_graph_result_staleness(
            "tenant-fresh", "codebase__search_graph",
            {"project_id": "proj"},
            result,
        )
        assert "_graph_staleness" not in out

        tunnel._code_graph_fingerprints.pop("tenant-fresh:proj", None)

    @pytest.mark.asyncio
    async def test_no_annotation_when_stored_fingerprint_absent(self, monkeypatch):
        """When we have never stored a fingerprint for this tenant/project,
        there is nothing to compare against — no false-positive staleness.
        With the no-baseline optimization, we skip _fetch_graph_current_fingerprint
        entirely (no extra round-trip until an index_repository establishes a
        baseline). The result passes through unchanged."""
        tunnel._code_graph_fingerprints.pop("tenant-new:proj", None)

        fetch_called = False

        async def _fake_fetch(tid, args):
            nonlocal fetch_called
            fetch_called = True
            return "SOME-COMMIT"

        monkeypatch.setattr(tunnel, "_fetch_graph_current_fingerprint", _fake_fetch)

        result = _text_result({"results": [{"file": "x.py"}]})
        out = await tunnel._annotate_graph_result_staleness(
            "tenant-new", "codebase__search_graph",
            {"project_id": "proj"},
            result,
        )
        assert "_graph_staleness" not in out
        # With no stored baseline, we skip the fetch — no extra round-trip.
        assert not fetch_called
        # No fingerprint stored (nothing to store without a fetch).
        assert tunnel._code_graph_fingerprints.get("tenant-new:proj") is None

        tunnel._code_graph_fingerprints.pop("tenant-new:proj", None)

    @pytest.mark.asyncio
    async def test_noop_for_non_search_graph_tool(self, monkeypatch):
        async def _fake_fetch(tid, args):
            return "DIFFERENT"

        monkeypatch.setattr(tunnel, "_fetch_graph_current_fingerprint", _fake_fetch)

        result = _text_result({"architecture": "microservices"})
        out = await tunnel._annotate_graph_result_staleness(
            "t", "codebase__get_architecture", {}, result
        )
        # get_architecture is not search_graph — pass through unchanged.
        assert out is result

    @pytest.mark.asyncio
    async def test_noop_for_none_result(self, monkeypatch):
        out = await tunnel._annotate_graph_result_staleness(
            "t", "codebase__search_graph", {}, None
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_survives_fetch_error(self, monkeypatch):
        """If _fetch_graph_current_fingerprint raises, the result passes through."""
        tunnel._code_graph_fingerprints["tenant-err:proj"] = "X"

        async def _boom(tid, args):
            raise RuntimeError("network down")

        monkeypatch.setattr(tunnel, "_fetch_graph_current_fingerprint", _boom)

        result = _text_result({"results": []})
        out = await tunnel._annotate_graph_result_staleness(
            "tenant-err", "codebase__search_graph", {"project_id": "proj"}, result
        )
        # Should return the original result unchanged (best-effort).
        assert out is result

        tunnel._code_graph_fingerprints.pop("tenant-err:proj", None)


class TestIndexRepositoryFingerprintCapture:
    """End-to-end: call_tunnel_tool captures fingerprint after index_repository."""

    @pytest.mark.asyncio
    async def test_stores_fingerprint_after_index_repository(self, monkeypatch):
        tenant = "tenant-idx"
        tool = "codebase__index_repository"

        tunnel._tunnel_tool_routes[tenant] = {tool: "code"}
        tunnel._tunnel_code_sockets[tenant] = object()
        # Clear any prior fingerprint.
        tunnel._code_graph_fingerprints.pop(f"{tenant}:*", None)

        async def _fake_jsonrpc(tid, label, method, params, repo_path=None):
            return {"result": {"content": [{"type": "text", "text": json.dumps({
                "indexed": True, "git_commit": "FRESH-COMMIT",
            })}]}}

        monkeypatch.setattr(tunnel, "_tunnel_jsonrpc", _fake_jsonrpc)

        try:
            result = await tunnel.call_tunnel_tool(tenant, tool, {"path": "/repo"})
            # Fingerprint should now be stored.
            stored = tunnel._code_graph_fingerprints.get(f"{tenant}:*")
            assert stored == "FRESH-COMMIT"
        finally:
            tunnel._tunnel_tool_routes.pop(tenant, None)
            tunnel._tunnel_code_sockets.pop(tenant, None)
            tunnel._code_graph_fingerprints.pop(f"{tenant}:*", None)


# ---------------------------------------------------------------------------
# Part 2 — prospect_symbol fallback chain
# ---------------------------------------------------------------------------

from meridian.prospect import prospect_symbol_impl as _prospect_symbol_impl  # noqa: E402


class TestProspectSymbolFallbackChain:
    """Each rung of the prospect_symbol fallback chain, via stubbed tunnel calls."""

    @pytest.mark.asyncio
    async def test_graph_rung_succeeds_stops_there(self, monkeypatch):
        """When search_graph returns results, stop at the graph rung."""
        import meridian.routes.tunnel as _tunnel_mod

        graph_result = _text_result({"results": [
            {"file": "meridian/server.py", "line": 100, "name": "patch_sprint_item"},
        ]})

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "codebase__search_graph":
                return graph_result
            raise AssertionError(f"should not call {name}")

        fake_tenant = {"id": "tenant-graph"}
        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="patch_sprint_item",
            project_id="meridian-repo",
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=fake_tenant,
            data_dir="",
        )
        assert result["rung"] == "graph"
        assert len(result["hits"]) == 1
        assert result["hits"][0]["name"] == "patch_sprint_item"

    @pytest.mark.asyncio
    async def test_graph_empty_falls_through_to_serena(self, monkeypatch):
        """When search_graph returns zero results, try Serena find_symbol."""
        import meridian.routes.tunnel as _tunnel_mod

        serena_result = _text_result([
            {"file": "meridian/server.py", "line_start": 4847, "name": "patch_sprint_item"},
        ])

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "codebase__search_graph":
                return _text_result({"results": []})
            if name == "extractor__find_symbol":
                return serena_result
            raise AssertionError(f"unexpected call: {name}")

        fake_tenant = {"id": "tenant-serena"}
        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="patch_sprint_item",
            project_id="",
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=fake_tenant,
            data_dir="",
        )
        assert result["rung"] == "serena"
        assert len(result["hits"]) >= 1
        assert result["hits"][0]["name"] == "patch_sprint_item"

    @pytest.mark.asyncio
    async def test_graph_skipped_serena_fallback(self, monkeypatch):
        """When stale_graph=True, skip graph and go straight to Serena."""
        import meridian.routes.tunnel as _tunnel_mod

        serena_result = _text_result([
            {"file": "meridian/doc_store.py", "line_start": 200, "name": "add_figure"},
        ])

        call_log: list[str] = []

        async def _fake_call_tunnel(tid, name, args, **kw):
            call_log.append(name)
            if name == "extractor__find_symbol":
                return serena_result
            if name == "extractor__find_declaration":
                return _text_result([])
            raise AssertionError(f"unexpected call: {name}")

        fake_tenant = {"id": "tenant-skip-graph"}
        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="add_figure",
            project_id="",
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=True,
            tenant=fake_tenant,
            data_dir="",
        )
        # Graph was skipped.
        assert "codebase__search_graph" not in call_log
        assert result["rung"] == "serena"
        assert result["hits"][0]["name"] == "add_figure"

    @pytest.mark.asyncio
    async def test_all_tunnel_rungs_miss_falls_back_to_semantic(self, monkeypatch):
        """When graph and Serena both return empty, fall back to search_code_semantic."""
        import meridian.routes.tunnel as _tunnel_mod
        import meridian.code_index as _code_index
        import meridian.hardening as _hardening

        async def _fake_call_tunnel(tid, name, args, **kw):
            # All tunnel tools return empty.
            if name == "codebase__search_graph":
                return _text_result({"results": []})
            if name in ("extractor__find_symbol", "extractor__find_declaration"):
                return _text_result([])
            raise AssertionError(f"unexpected: {name}")

        fake_semantic_result = {
            "root_dir": "/repo",
            "query": "put_figures",
            "hits": [
                {"path": "meridian/doc_store.py", "line_start": 300, "name": "put_figures"},
            ],
            "total_indexed": 10,
        }

        async def _fake_bulkhead(fn, *a, **kw):
            return fake_semantic_result

        fake_tenant = {"id": "tenant-semantic"}
        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)
        monkeypatch.setattr(_hardening, "run_in_bulkhead", _fake_bulkhead)

        result = await _prospect_symbol_impl(
            symbol="put_figures",
            project_id="",
            root_dir="/repo",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=fake_tenant,
            data_dir="",
        )
        assert result["rung"] == "semantic"
        assert len(result["hits"]) == 1
        assert result["hits"][0]["name"] == "put_figures"

    @pytest.mark.asyncio
    async def test_no_tunnel_no_root_dir_returns_empty(self, monkeypatch):
        """Without a tunnel and without a root_dir, returns empty without raising."""
        import meridian.routes.tunnel as _tunnel_mod

        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: False)

        result = await _prospect_symbol_impl(
            symbol="nonexistent_fn",
            project_id="",
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant={"id": "tenant-notunnel"},
            data_dir="",
        )
        assert result["rung"] == "none"
        assert result["hits"] == []

    @pytest.mark.asyncio
    async def test_graph_staleness_warning_preserved_in_hits(self, monkeypatch):
        """When search_graph returns hits AND a staleness warning, both are
        preserved in the result (the caller can see the warning even with hits)."""
        import meridian.routes.tunnel as _tunnel_mod

        graph_result = {
            "content": [{"type": "text", "text": json.dumps({
                "results": [{"file": "x.py", "line": 4408, "name": "patch_sprint_item"}],
            })}],
            "_graph_staleness": {
                "stale": True,
                "warning": "line numbers may be wrong",
                "stored_fingerprint": "OLD",
                "current_fingerprint": "NEW",
            },
        }

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "codebase__search_graph":
                return graph_result
            raise AssertionError(f"unexpected: {name}")

        fake_tenant = {"id": "tenant-stale-hits"}
        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="patch_sprint_item",
            project_id="myrepo",
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=fake_tenant,
            data_dir="",
        )
        # Hits were present so we stop at graph.
        assert result["rung"] == "graph"
        # Staleness warning is surfaced.
        assert "_graph_staleness" in result
        assert result["_graph_staleness"]["stale"] is True

    @pytest.mark.asyncio
    async def test_serena_find_declaration_fallback(self, monkeypatch):
        """If find_symbol returns empty, try find_declaration before giving up on Serena."""
        import meridian.routes.tunnel as _tunnel_mod

        decl_result = _text_result([
            {"file": "meridian/doc_store.py", "line_start": 50, "name": "add_figure"},
        ])

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "codebase__search_graph":
                return _text_result({"results": []})
            if name == "extractor__find_symbol":
                return _text_result([])  # empty
            if name == "extractor__find_declaration":
                return decl_result
            raise AssertionError(f"unexpected: {name}")

        fake_tenant = {"id": "tenant-decl"}
        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="add_figure",
            project_id="",
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=fake_tenant,
            data_dir="",
        )
        assert result["rung"] == "serena"
        assert result["hits"][0]["name"] == "add_figure"

    @pytest.mark.asyncio
    async def test_no_tenant_uses_semantic_only(self, monkeypatch):
        """No tenant → tunnel rungs are all skipped; semantic (root_dir given)
        is the first and only rung tried."""
        import meridian.hardening as _hardening

        async def _fake_bulkhead(fn, *a, **kw):
            return {"hits": [{"path": "foo.py", "name": "my_fn"}], "total_indexed": 1}

        monkeypatch.setattr(_hardening, "run_in_bulkhead", _fake_bulkhead)

        result = await _prospect_symbol_impl(
            symbol="my_fn",
            project_id="",
            root_dir="/some/root",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=None,
            data_dir="",
        )
        assert result["rung"] == "semantic"
        assert result["hits"][0]["name"] == "my_fn"


# ---------------------------------------------------------------------------
# Integration: prospect_symbol appears in the MCP tools list
# ---------------------------------------------------------------------------

def test_prospect_symbol_in_mcp_tools_list():
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    names = [t["name"] for t in _MCP_TOOLS_LIST]
    assert "prospect_symbol" in names


def test_prospect_symbol_is_read_only():
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    tool = next(t for t in _MCP_TOOLS_LIST if t["name"] == "prospect_symbol")
    assert tool.get("annotations", {}).get("readOnlyHint") is True


def test_prospect_symbol_schema_requires_symbol():
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    tool = next(t for t in _MCP_TOOLS_LIST if t["name"] == "prospect_symbol")
    schema = tool.get("inputSchema", {})
    assert "symbol" in schema.get("required", [])
