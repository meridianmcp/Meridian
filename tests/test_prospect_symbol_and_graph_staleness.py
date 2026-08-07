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


# ---------------------------------------------------------------------------
# Part 3 — 9033914e: graph_empty after fresh index_repository
# ---------------------------------------------------------------------------
#
# Regression tests for the confirmed bug: prospect_symbol reported
# "graph_empty" immediately after index_repository returned a populated
# graph.  Root causes:
#
#  (A) When search_graph with a project_id returns zero results but the
#      SAME graph has results WITHOUT a project_id (project_id slug mismatch
#      between caller and what index_repository auto-assigned), the system
#      should retry without project_id and surface those results instead of
#      silently reporting graph_empty.
#
#  (B) When search_graph returns an application-level error object inside the
#      MCP content envelope (e.g. {"error": "unknown project"}) rather than a
#      JSON-RPC error, prospect_symbol must NOT treat it as zero results and
#      report "graph_empty" — it should report "graph_error: ...".
#
#  (C) _annotate_graph_result_staleness must fall back to the wildcard
#      "{tenant_id}:*" fingerprint key when no project-specific key exists,
#      so that a fingerprint stored by index_repository (which has no
#      project_id argument) is visible to a later search_graph call that
#      carries a project_id.

class TestGraphEmptyAfterFreshIndex:
    """9033914e — regression tests for graph_empty immediately after index."""

    @pytest.mark.asyncio
    async def test_project_id_mismatch_falls_back_to_broad_search(self, monkeypatch):
        """When search_graph with project_id returns zero hits but the same
        graph has hits WITHOUT project_id, the broad fallback succeeds and
        the rung is 'graph' (not 'none') with an informative fallback_reason."""
        import meridian.routes.tunnel as _tunnel_mod

        hits_payload = {"results": [
            {"file": "analysis.py", "line": 42, "name": "dominant_segments_from_group"},
        ]}

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "codebase__search_graph":
                # With project_id → empty (slug mismatch).
                if args.get("project_id"):
                    return _text_result({"results": []})
                # Without project_id → real hits.
                return _text_result(hits_payload)
            raise AssertionError(f"unexpected call: {name}")

        fake_tenant = {"id": "tenant-mismatch"}
        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="dominant_segments_from_group",
            project_id="C-Users-13144-Documents-Masters_Thesis-CURRENT_PROJECT_CODE",
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=fake_tenant,
            data_dir="",
        )
        # The broad fallback must succeed and land on graph rung.
        assert result["rung"] == "graph", f"expected graph rung, got {result['rung']!r}"
        assert len(result["hits"]) == 1
        assert result["hits"][0]["name"] == "dominant_segments_from_group"
        # fallback_reason must describe the project_id mismatch (not "graph_empty").
        assert result.get("fallback_reason") is not None
        assert "project_id" in str(result["fallback_reason"]).lower() or \
               "mismatch" in str(result["fallback_reason"]).lower() or \
               "broad" in str(result["fallback_reason"]).lower(), \
               f"fallback_reason should describe mismatch: {result.get('fallback_reason')!r}"

    @pytest.mark.asyncio
    async def test_app_level_error_in_content_sets_graph_error_reason(self, monkeypatch):
        """When search_graph returns an application-level error dict inside the
        MCP content envelope (not a JSON-RPC error), prospect_symbol must NOT
        report 'graph_empty' — it must report 'graph_error: ...' so the caller
        knows the real cause."""
        import meridian.routes.tunnel as _tunnel_mod

        # Application-level error returned as text content (not JSON-RPC error).
        error_result = _text_result({"error": "unknown project", "code": 404})

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "codebase__search_graph":
                return error_result
            raise AssertionError(f"unexpected call: {name}")

        fake_tenant = {"id": "tenant-app-err"}
        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="dominant_segments_from_group",
            project_id="some-project",
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=fake_tenant,
            data_dir="",
        )
        # Must NOT be "graph_empty" — the real cause is an app-level error.
        assert result["rung"] == "none"
        fr = str(result.get("fallback_reason") or "")
        assert "graph_error" in fr, (
            f"expected fallback_reason to contain 'graph_error', got {fr!r}"
        )
        assert "unknown project" in fr, (
            f"expected error message in fallback_reason, got {fr!r}"
        )

    @pytest.mark.asyncio
    async def test_non_json_text_error_sets_graph_error_reason(self, monkeypatch):
        """When search_graph returns a non-JSON text block (e.g. a raw error
        string), prospect_symbol must report 'graph_error: ...' not 'graph_empty'."""
        import meridian.routes.tunnel as _tunnel_mod

        # Non-JSON text response (raw error string from the server).
        raw_error_result = {"content": [{"type": "text", "text": "Error: project not found"}]}

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "codebase__search_graph":
                return raw_error_result
            raise AssertionError(f"unexpected call: {name}")

        fake_tenant = {"id": "tenant-raw-err"}
        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="dominant_segments_from_group",
            project_id="some-project",
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=fake_tenant,
            data_dir="",
        )
        assert result["rung"] == "none"
        fr = str(result.get("fallback_reason") or "")
        # Raw text error must surface as graph_error, not graph_empty.
        assert "graph_error" in fr, (
            f"expected 'graph_error' in fallback_reason, got {fr!r}"
        )

    @pytest.mark.asyncio
    async def test_both_broad_searches_empty_falls_through(self, monkeypatch):
        """When search_graph returns empty both with AND without project_id,
        fall through to Serena and report the right fallback_reason."""
        import meridian.routes.tunnel as _tunnel_mod

        serena_result = _text_result([
            {"file": "analysis.py", "line": 10, "name": "dominant_segments_from_group"},
        ])

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "codebase__search_graph":
                return _text_result({"results": []})
            if name == "extractor__find_symbol":
                return serena_result
            raise AssertionError(f"unexpected call: {name}")

        fake_tenant = {"id": "tenant-both-empty"}
        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="dominant_segments_from_group",
            project_id="C-Users-13144-Documents-Masters_Thesis-CURRENT_PROJECT_CODE",
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=fake_tenant,
            data_dir="",
        )
        # Fell through to Serena (graph was empty for both attempts).
        assert result["rung"] == "serena"
        assert len(result["hits"]) == 1

    @pytest.mark.asyncio
    async def test_no_project_id_no_broad_retry(self, monkeypatch):
        """When no project_id is passed, there is no second search_graph call
        (nothing to retry without), and graph_empty is reported correctly."""
        import meridian.routes.tunnel as _tunnel_mod

        call_count = [0]

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "codebase__search_graph":
                call_count[0] += 1
                return _text_result({"results": []})
            if name in ("extractor__find_symbol", "extractor__find_declaration"):
                return _text_result([])
            raise AssertionError(f"unexpected call: {name}")

        fake_tenant = {"id": "tenant-nopid"}
        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="something",
            project_id="",  # no project_id
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=fake_tenant,
            data_dir="",
        )
        assert result["rung"] == "none"
        # Only ONE search_graph call when no project_id — no broad retry.
        assert call_count[0] == 1, f"expected 1 call, got {call_count[0]}"
        assert result.get("fallback_reason") == "graph_empty"

    @pytest.mark.asyncio
    async def test_app_level_project_not_found_error_retries_broad_and_succeeds(
        self, monkeypatch,
    ):
        """1579bc1e — confirmed report: search_graph returns an app-level
        "project not found"/"not indexed" error (not just zero hits) for a
        project_id that doesn't match the repo-path slug index_repository
        auto-assigned. The graph rung must retry WITHOUT project_id and,
        when that succeeds, report hits instead of giving up with
        graph_error — the exact same slug-mismatch fix as the zero-hits
        case, just triggered by an explicit error payload."""
        import meridian.routes.tunnel as _tunnel_mod

        error_result = _text_result(
            {"error": "project not found or not indexed", "code": 404}
        )
        hits_payload = _text_result({"results": [
            {"file": "analysis.py", "line": 42, "name": "dominant_segments_from_group"},
        ]})

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "codebase__search_graph":
                if args.get("project_id"):
                    return error_result
                return hits_payload
            raise AssertionError(f"unexpected call: {name}")

        fake_tenant = {"id": "tenant-app-err-mismatch"}
        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="dominant_segments_from_group",
            project_id="meridian-build",
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=fake_tenant,
            data_dir="",
        )
        assert result["rung"] == "graph"
        assert len(result["hits"]) == 1
        assert result["hits"][0]["name"] == "dominant_segments_from_group"
        fr = str(result.get("fallback_reason") or "")
        assert "mismatch" in fr.lower()
        assert "meridian-build" in fr

    @pytest.mark.asyncio
    async def test_app_level_unrelated_error_does_not_trigger_broad_retry(
        self, monkeypatch,
    ):
        """An app-level error that is NOT project-related (e.g. a query
        syntax error) must not trigger the broad no-project_id retry — only
        genuine project-not-found-shaped errors should."""
        import meridian.routes.tunnel as _tunnel_mod

        error_result = _text_result({"error": "query syntax error near '('"})
        call_count = [0]

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "codebase__search_graph":
                call_count[0] += 1
                return error_result
            raise AssertionError(f"unexpected call: {name}")

        fake_tenant = {"id": "tenant-unrelated-err"}
        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="dominant_segments_from_group",
            project_id="meridian-build",
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=fake_tenant,
            data_dir="",
        )
        assert result["rung"] == "none"
        assert call_count[0] == 1, "unrelated error must not trigger a broad retry"
        fr = str(result.get("fallback_reason") or "")
        assert "graph_error" in fr
        assert "query syntax error" in fr

    @pytest.mark.asyncio
    async def test_raised_project_not_found_exception_retries_broad_and_succeeds(
        self, monkeypatch,
    ):
        """1579bc1e — the same slug mismatch can surface as a raised
        exception from call_tunnel_tool (e.g. a JSON-RPC-level error,
        possibly hint-enriched by _enrich_code_intel_project_error) rather
        than a result payload at all. The graph rung must still retry
        without project_id and succeed instead of falling straight to
        Serena/semantic with a bare graph_error."""
        import meridian.routes.tunnel as _tunnel_mod

        hits_payload = _text_result({"results": [
            {"file": "analysis.py", "line": 7, "name": "dominant_segments_from_group"},
        ]})

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "codebase__search_graph":
                if args.get("project_id"):
                    raise RuntimeError(
                        "project 'meridian-build' not found\n\n"
                        "Note: code-intel graph tools identify a project by "
                        "its LOCAL REPO-PATH slug..."
                    )
                return hits_payload
            raise AssertionError(f"unexpected call: {name}")

        fake_tenant = {"id": "tenant-raised-mismatch"}
        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="dominant_segments_from_group",
            project_id="meridian-build",
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=fake_tenant,
            data_dir="",
        )
        assert result["rung"] == "graph"
        assert len(result["hits"]) == 1
        assert result["hits"][0]["name"] == "dominant_segments_from_group"
        fr = str(result.get("fallback_reason") or "")
        assert "mismatch" in fr.lower()

    @pytest.mark.asyncio
    async def test_raised_unrelated_exception_does_not_trigger_broad_retry(
        self, monkeypatch,
    ):
        """A raised exception unrelated to project lookup (e.g. a transport
        failure) must not trigger the broad retry and must fall through to
        Serena exactly as before."""
        import meridian.routes.tunnel as _tunnel_mod

        serena_result = _text_result([
            {"file": "analysis.py", "line": 10, "name": "dominant_segments_from_group"},
        ])
        call_count = [0]

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "codebase__search_graph":
                call_count[0] += 1
                raise RuntimeError("tunnel transport error: connection reset")
            if name == "extractor__find_symbol":
                return serena_result
            raise AssertionError(f"unexpected call: {name}")

        fake_tenant = {"id": "tenant-raised-unrelated"}
        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="dominant_segments_from_group",
            project_id="meridian-build",
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=fake_tenant,
            data_dir="",
        )
        # Only one search_graph call — unrelated errors don't trigger retry.
        assert call_count[0] == 1
        assert result["rung"] == "serena"
        assert result["hits"][0]["name"] == "dominant_segments_from_group"

    @pytest.mark.asyncio
    async def test_nested_error_object_in_payload_still_triggers_broad_retry(
        self, monkeypatch,
    ):
        """1579bc1e — regression for a real gap in _payload_is_error: the
        original implementation built its message via
        ``payload.get("error") or payload.get("message") or ...`` and
        returned the FIRST truthy value regardless of type. When
        codebase-memory-mcp nests the text one level down (e.g.
        ``{"error": {"message": "project not found or not indexed"}}``
        instead of a bare string), the old code hit the dict, saw it was
        truthy, and returned None (since ``isinstance(msg, str)`` failed) —
        silently discarding the error and reporting graph_empty. The fix
        probes a nested dict for its own message/error/detail field."""
        import meridian.routes.tunnel as _tunnel_mod

        nested_error_result = _text_result({
            "error": {"message": "project not found or not indexed", "code": 404},
        })
        hits_payload = _text_result({"results": [
            {"file": "analysis.py", "line": 42, "name": "dominant_segments_from_group"},
        ]})

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "codebase__search_graph":
                if args.get("project_id"):
                    return nested_error_result
                return hits_payload
            raise AssertionError(f"unexpected call: {name}")

        fake_tenant = {"id": "tenant-nested-err"}
        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="dominant_segments_from_group",
            project_id="meridian-build",
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=fake_tenant,
            data_dir="",
        )
        assert result["rung"] == "graph", (
            f"expected the nested error to be recognised and the broad retry "
            f"to succeed, got rung={result['rung']!r} "
            f"fallback_reason={result.get('fallback_reason')!r}"
        )
        assert len(result["hits"]) == 1
        assert result["hits"][0]["name"] == "dominant_segments_from_group"

    @pytest.mark.asyncio
    async def test_full_pipeline_message_less_key_error_still_retries(
        self, monkeypatch,
    ):
        """End-to-end regression combining BOTH fixes through the REAL (not
        stubbed) call_tunnel_tool: the tunnel's JSON-RPC error object has no
        "message" key (text lives under "data" instead — a real gap in the
        old ``err.get("message")``-only extraction), so this exercises the
        actual production code path: _tunnel_jsonrpc -> call_tunnel_tool's
        error-message construction -> the raised exception ->
        prospect_symbol_impl's rung-1 except clause ->
        _is_project_not_found_error -> _broad_retry_without_project_id. Only
        _tunnel_jsonrpc (the lowest-level transport seam) is stubbed."""
        import meridian.routes.tunnel as _tunnel_mod

        tenant_id = "tenant-full-pipeline"
        tool = "codebase__search_graph"
        _tunnel_mod._tunnel_tool_routes[tenant_id] = {tool: "code"}
        _tunnel_mod._tunnel_code_sockets[tenant_id] = object()

        async def _fake_jsonrpc(tid, label, method, params, repo_path=None):
            call_args = params.get("arguments") or {}
            if call_args.get("project_id"):
                # No "message" key — only "data" carries the real text, the
                # exact shape that used to collapse to the string "None".
                return {"error": {"code": 404, "data": "project not found or not indexed"}}
            return {"result": _text_result({"results": [
                {"file": "analysis.py", "line": 7, "name": "dominant_segments_from_group"},
            ]})}

        async def _fake_list_projects(tid):
            return []

        monkeypatch.setattr(_tunnel_mod, "_tunnel_jsonrpc", _fake_jsonrpc)
        monkeypatch.setattr(_tunnel_mod, "_list_indexed_project_ids", _fake_list_projects)

        try:
            result = await _prospect_symbol_impl(
                symbol="dominant_segments_from_group",
                project_id="meridian-build",
                root_dir="",
                limit=5,
                kind=None,
                stale_graph=False,
                tenant={"id": tenant_id},
                data_dir="",
            )
        finally:
            _tunnel_mod._tunnel_tool_routes.pop(tenant_id, None)
            _tunnel_mod._tunnel_code_sockets.pop(tenant_id, None)

        assert result["rung"] == "graph", (
            f"expected the broad retry to succeed end-to-end through the "
            f"real call_tunnel_tool, got rung={result['rung']!r} "
            f"fallback_reason={result.get('fallback_reason')!r}"
        )
        assert len(result["hits"]) == 1
        assert result["hits"][0]["name"] == "dominant_segments_from_group"


class TestProspectSymbolTruthfulDiagnostics:
    """d5e60791 — prospect_symbol_impl must never silently collapse an
    exception into rung="none" with no diagnostic. Every rung now carries a
    structured entry in result["rungs"] (status/attempted_tool/selected_tool/
    reason/error/error_kind), and the top-level fallback_reason is always
    populated when every rung misses.

    The first test below is a direct unit-test encoding of the live bug
    reproduction: an already-running MCP connector reported
    ``search_code_semantic`` failing with "No module named
    'meridian_codeindex'" while prospect_symbol silently returned
    ``{"rung": "none", "fallback_reason": None}`` with zero trace of why.
    """

    @pytest.mark.asyncio
    async def test_semantic_dependency_error_is_not_silently_swallowed(self, monkeypatch):
        """The exact live reproduction: meridian_codeindex not importable in
        this runtime must surface as a real, structured diagnostic -- never
        as a bare rung="none" with fallback_reason=None."""
        from meridian import hardening as _hardening

        async def _fake_bulkhead_raises(fn, *a, **kw):
            raise ModuleNotFoundError(
                "No module named 'meridian_codeindex'", name="meridian_codeindex",
            )

        monkeypatch.setattr(_hardening, "run_in_bulkhead", _fake_bulkhead_raises)

        result = await _prospect_symbol_impl(
            symbol="prospect_symbol_impl",
            project_id="",
            root_dir="/repo",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=None,  # no tunnel context at all -> graph/serena both skip cleanly
            data_dir="",
        )
        assert result["rung"] == "none"
        # The bug this closes: fallback_reason must NEVER be None here.
        assert result.get("fallback_reason") is not None
        fr = str(result["fallback_reason"])
        assert "semantic_error" in fr
        assert "meridian_codeindex" in fr

        rungs = result["rungs"]
        assert rungs["graph"]["status"] == "skipped"
        assert rungs["graph"]["reason"] == "no_tenant_id"
        assert rungs["serena"]["status"] == "skipped"
        assert rungs["serena"]["reason"] == "no_tenant_id"
        assert rungs["semantic"]["status"] == "error"
        assert rungs["semantic"]["attempted_tool"] == "search_code_semantic"
        assert "meridian_codeindex" in rungs["semantic"]["error"]
        # A missing/broken import must be classified as a dependency error,
        # not lumped in with generic runtime failures.
        assert rungs["semantic"]["error_kind"] == "dependency_error"

    @pytest.mark.asyncio
    async def test_semantic_rung_error_dict_result_is_not_treated_as_empty(self, monkeypatch):
        """search_code_semantic can degrade to {"error": "..."} instead of
        raising (e.g. hosted-mode guard, bad root_dir). That must be treated
        as a real error too, not silently read as "zero hits, nothing wrong"."""
        from meridian import hardening as _hardening

        async def _fake_bulkhead_error_dict(fn, *a, **kw):
            return {
                "root_dir": "/repo", "query": "x", "hits": [],
                "total_indexed": 0,
                "error": "root_dir does not exist: /repo",
            }

        monkeypatch.setattr(_hardening, "run_in_bulkhead", _fake_bulkhead_error_dict)

        result = await _prospect_symbol_impl(
            symbol="x",
            project_id="",
            root_dir="/repo",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=None,
            data_dir="",
        )
        assert result["rung"] == "none"
        fr = str(result.get("fallback_reason") or "")
        assert "semantic_error" in fr
        assert "does not exist" in fr
        assert result["rungs"]["semantic"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_serena_rung_exceptions_recorded_not_silently_swallowed(self, monkeypatch):
        """Both extractor__find_symbol and extractor__find_declaration
        raising must be visible in rungs["serena"], not a bare `pass`."""
        import meridian.routes.tunnel as _tunnel_mod

        async def _fake_call_tunnel(tid, name, args, **kw):
            raise RuntimeError(f"{name} exploded")

        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="whatever",
            project_id="",
            root_dir="",  # semantic rung skipped -> serena's error is decisive
            limit=5,
            kind=None,
            stale_graph=True,  # graph rung skipped cleanly, isolates serena
            tenant={"id": "tenant-serena-error"},
            data_dir="",
        )
        assert result["rung"] == "none"
        serena_entry = result["rungs"]["serena"]
        assert serena_entry["status"] == "error"
        assert "extractor__find_symbol exploded" in serena_entry["error"]
        assert "extractor__find_declaration exploded" in serena_entry["error"]
        assert serena_entry["error_kind"] == "runtime_error"

    @pytest.mark.asyncio
    async def test_graph_rung_no_active_tunnel_is_recorded_not_silent(self, monkeypatch):
        """Previously: has_active_tunnel()=False left rungs untouched with no
        explanation at all. Must now record an explicit skip reason."""
        import meridian.routes.tunnel as _tunnel_mod

        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: False)

        result = await _prospect_symbol_impl(
            symbol="whatever",
            project_id="",
            root_dir="",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant={"id": "tenant-no-tunnel"},
            data_dir="",
        )
        assert result["rung"] == "none"
        assert result["rungs"]["graph"]["status"] == "skipped"
        assert result["rungs"]["graph"]["reason"] == "no_active_tunnel"
        assert result["rungs"]["serena"]["status"] == "skipped"
        assert result["rungs"]["serena"]["reason"] == "no_active_tunnel"
        # Every rung accounted for -> synthesized fallback_reason, never None.
        assert result.get("fallback_reason") is not None

    @pytest.mark.asyncio
    async def test_successful_semantic_rung_marks_rungs_succeeded(self, monkeypatch):
        """Sanity check: the new diagnostics don't break the success path."""
        from meridian import hardening as _hardening

        fake_semantic_result = {
            "root_dir": "/repo", "query": "put_figures",
            "hits": [{"path": "meridian/doc_store.py", "name": "put_figures"}],
            "total_indexed": 5,
        }

        async def _fake_bulkhead(fn, *a, **kw):
            return fake_semantic_result

        monkeypatch.setattr(_hardening, "run_in_bulkhead", _fake_bulkhead)

        result = await _prospect_symbol_impl(
            symbol="put_figures",
            project_id="",
            root_dir="/repo",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant=None,
            data_dir="",
        )
        assert result["rung"] == "semantic"
        assert result["rungs"]["semantic"]["status"] == "succeeded"
        assert result["rungs"]["semantic"]["selected_tool"] == "search_code_semantic"


class TestFingerprintWildcardFallback:
    """9033914e — staleness fingerprint wildcard fallback for project-specific
    search_graph calls after a project_id-less index_repository run."""

    @pytest.mark.asyncio
    async def test_wildcard_key_used_when_project_specific_absent(self, monkeypatch):
        """When a search_graph call has a project_id but only a wildcard
        fingerprint exists (stored by index_repository), the wildcard is used
        as the staleness baseline so the comparison fires correctly."""
        tenant_id = "tenant-wildcard-fb"
        wildcard_key = f"{tenant_id}:*"
        project_key = f"{tenant_id}:C-Users-13144-Documents-Masters_Thesis"

        # Simulate: index_repository ran and stored under wildcard.
        tunnel._code_graph_fingerprints[wildcard_key] = "INDEX-COMMIT"
        # No project-specific key.
        tunnel._code_graph_fingerprints.pop(project_key, None)

        async def _fake_fetch(tid, args):
            return "NEWER-COMMIT"  # Graph has changed since index.

        monkeypatch.setattr(tunnel, "_fetch_graph_current_fingerprint", _fake_fetch)

        result = _text_result({"results": [{"file": "foo.py", "line": 1}]})
        enriched = await tunnel._annotate_graph_result_staleness(
            tenant_id,
            "codebase__search_graph",
            {"project_id": "C-Users-13144-Documents-Masters_Thesis", "query": "foo"},
            result,
        )
        # Must have detected staleness via wildcard fallback.
        stale = enriched.get("_graph_staleness")
        assert stale is not None, (
            "expected staleness annotation when wildcard key exists and "
            "fingerprints differ, but got none"
        )
        assert stale["stale"] is True
        assert "INDEX-COMMIT" in stale["warning"]
        assert "NEWER-COMMIT" in stale["warning"]

        # Cleanup.
        tunnel._code_graph_fingerprints.pop(wildcard_key, None)
        tunnel._code_graph_fingerprints.pop(project_key, None)

    @pytest.mark.asyncio
    async def test_project_specific_key_takes_precedence_over_wildcard(self, monkeypatch):
        """When BOTH a project-specific and a wildcard key exist, the
        project-specific key is used (it takes precedence)."""
        tenant_id = "tenant-proj-precedence"
        wildcard_key = f"{tenant_id}:*"
        project_key = f"{tenant_id}:myrepo"

        tunnel._code_graph_fingerprints[wildcard_key] = "WILDCARD-COMMIT"
        tunnel._code_graph_fingerprints[project_key] = "PROJECT-COMMIT"

        fetch_args_seen: list = []

        async def _fake_fetch(tid, args):
            fetch_args_seen.append(args)
            return "PROJECT-COMMIT"  # Same → no staleness.

        monkeypatch.setattr(tunnel, "_fetch_graph_current_fingerprint", _fake_fetch)

        result = _text_result({"results": []})
        out = await tunnel._annotate_graph_result_staleness(
            tenant_id,
            "codebase__search_graph",
            {"project_id": "myrepo", "query": "x"},
            result,
        )
        # Fingerprints match → no staleness annotation.
        assert "_graph_staleness" not in out
        # Must have fetched ONCE (for the project-specific key comparison).
        assert len(fetch_args_seen) == 1

        tunnel._code_graph_fingerprints.pop(wildcard_key, None)
        tunnel._code_graph_fingerprints.pop(project_key, None)

    @pytest.mark.asyncio
    async def test_no_wildcard_and_no_project_key_skips_fetch(self, monkeypatch):
        """When neither a wildcard nor a project-specific fingerprint exists,
        no fetch is performed and the result passes through unchanged."""
        tenant_id = "tenant-no-keys"
        tunnel._code_graph_fingerprints.pop(f"{tenant_id}:*", None)
        tunnel._code_graph_fingerprints.pop(f"{tenant_id}:myrepo", None)

        fetch_called = [False]

        async def _fake_fetch(tid, args):
            fetch_called[0] = True
            return "SOME-COMMIT"

        monkeypatch.setattr(tunnel, "_fetch_graph_current_fingerprint", _fake_fetch)

        result = _text_result({"results": [{"file": "x.py"}]})
        out = await tunnel._annotate_graph_result_staleness(
            tenant_id,
            "codebase__search_graph",
            {"project_id": "myrepo"},
            result,
        )
        assert "_graph_staleness" not in out
        assert not fetch_called[0], "fetch should not be called when no baseline exists"


# ---------------------------------------------------------------------------
# 653579c5 — resolve_sprint_item_pointers's symbol resolver now reaches the
# same live three-rung prospect_symbol chain, not just the (production-empty)
# cached codebase_graph_entities snapshot.
# ---------------------------------------------------------------------------

from meridian.prospect import build_symbol_resolver, _normalize_prospect_hit  # noqa: E402


class TestNormalizeProspectHit:
    def test_passes_through_existing_qualified_name_and_file(self):
        hit = {"qualified_name": "pkg.mod.fn", "file": "pkg/mod.py", "line": 10}
        out = _normalize_prospect_hit(hit, "fn")
        assert out["qualified_name"] == "pkg.mod.fn"
        assert out["file"] == "pkg/mod.py"
        assert out["line"] == 10

    def test_backfills_qualified_name_from_name(self):
        # Serena-style hit: has "name", not "qualified_name".
        hit = {"name": "add_figure", "file": "meridian/doc_store.py"}
        out = _normalize_prospect_hit(hit, "add_figure")
        assert out["qualified_name"] == "add_figure"
        assert out["file"] == "meridian/doc_store.py"

    def test_backfills_file_from_path(self):
        # search_code_semantic-style hit: has "path", not "file".
        hit = {"name": "my_fn", "path": "foo.py"}
        out = _normalize_prospect_hit(hit, "my_fn")
        assert out["file"] == "foo.py"
        assert out["qualified_name"] == "my_fn"

    def test_non_dict_hit_degrades_to_qualified_name_only(self):
        out = _normalize_prospect_hit("not-a-dict", "my_fn")
        assert out == {"qualified_name": "my_fn"}


class TestBuildSymbolResolver:
    """build_symbol_resolver: prospect_symbol_impl first, snapshot fallback."""

    @pytest.mark.asyncio
    async def test_uses_live_graph_hit_when_tunnel_active(self, monkeypatch):
        """653579c5 regression: a tenant with an active code tunnel resolves
        via the LIVE graph, even though the local snapshot has nothing for it
        (search_graph_entities is never even called — the prospect hit wins)."""
        import meridian.routes.tunnel as _tunnel_mod
        from meridian import db as db_module

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "codebase__search_graph":
                return _text_result({
                    "results": [{"qualified_name": "meridian.prospect.prospect_symbol_impl",
                                 "file": "meridian/prospect.py"}],
                })
            raise AssertionError(f"unexpected: {name}")

        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        async def _snapshot_should_not_be_called(*a, **kw):
            raise AssertionError("snapshot fallback must not run when graph hit")

        monkeypatch.setattr(db_module, "search_graph_entities", _snapshot_should_not_be_called)

        resolver = build_symbol_resolver(tenant={"id": "tenant-live"}, root_dir="", data_dir="")
        matches = await resolver(object(), "myrepo", "prospect_symbol_impl", 5)
        assert len(matches) == 1
        assert matches[0]["qualified_name"] == "meridian.prospect.prospect_symbol_impl"
        assert matches[0]["file"] == "meridian/prospect.py"

    @pytest.mark.asyncio
    async def test_falls_back_to_snapshot_when_no_tenant(self, monkeypatch):
        """No tenant/tunnel (e.g. self-hosted, no --tunnel running): degrades
        to the old cached-snapshot behaviour instead of returning nothing."""
        from meridian import db as db_module

        async def _fake_snapshot(_db, _pid, query, limit=10):
            return [{"qualified_name": "meridian.server.mcp_tools_doc",
                      "file": "meridian/server.py"}]

        monkeypatch.setattr(db_module, "search_graph_entities", _fake_snapshot)

        resolver = build_symbol_resolver(tenant=None, root_dir="", data_dir="")
        matches = await resolver(object(), "p1", "mcp_tools_doc", 5)
        assert len(matches) == 1
        assert matches[0]["file"] == "meridian/server.py"

    @pytest.mark.asyncio
    async def test_falls_back_to_snapshot_when_tunnel_active_but_zero_hits(self, monkeypatch):
        """Tunnel active but the graph genuinely has zero hits: still falls
        through to the snapshot rather than dead-ending."""
        import meridian.routes.tunnel as _tunnel_mod
        from meridian import db as db_module

        async def _fake_call_tunnel(tid, name, args, **kw):
            return _text_result({"results": []})

        monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

        async def _fake_snapshot(_db, _pid, query, limit=10):
            return [{"qualified_name": "snapshot.hit", "file": "snap.py"}]

        monkeypatch.setattr(db_module, "search_graph_entities", _fake_snapshot)

        resolver = build_symbol_resolver(tenant={"id": "tenant-empty"}, root_dir="", data_dir="")
        matches = await resolver(object(), "p1", "nonexistent_fn", 5)
        assert len(matches) == 1
        assert matches[0]["qualified_name"] == "snapshot.hit"

    @pytest.mark.asyncio
    async def test_no_qualified_name_skips_straight_to_snapshot(self, monkeypatch):
        from meridian import db as db_module

        async def _fake_snapshot(_db, _pid, query, limit=10):
            return []

        monkeypatch.setattr(db_module, "search_graph_entities", _fake_snapshot)

        resolver = build_symbol_resolver(tenant={"id": "t"}, root_dir="", data_dir="")
        matches = await resolver(object(), "p1", None, 5)
        assert matches == []

    @pytest.mark.asyncio
    async def test_prospect_exception_falls_back_to_snapshot(self, monkeypatch):
        """A prospect_symbol_impl-side exception must never break resolution —
        degrade to the snapshot fallback instead of raising."""
        import meridian.prospect as _prospect_mod
        from meridian import db as db_module

        async def _boom(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(_prospect_mod, "prospect_symbol_impl", _boom)

        async def _fake_snapshot(_db, _pid, query, limit=10):
            return [{"qualified_name": "snapshot.hit", "file": "snap.py"}]

        monkeypatch.setattr(db_module, "search_graph_entities", _fake_snapshot)

        resolver = build_symbol_resolver(tenant={"id": "t"}, root_dir="", data_dir="")
        matches = await resolver(object(), "p1", "some_fn", 5)
        assert len(matches) == 1
        assert matches[0]["qualified_name"] == "snapshot.hit"
# Part 4 — 4b8f083f: local git-commit-drift staleness probe
#
# The 2ce5bc76 fingerprint mechanism above only detects drift when a
# DIFFERENT process re-indexes and index_status echoes back a newer
# fingerprint than the one this process cached — it never fires for the far
# more common case where real commits land in the repo and nobody re-runs
# index_repository at all (index_status just keeps echoing the same stale
# fingerprint forever). These tests cover the local git-based probe that
# closes that gap.
# ---------------------------------------------------------------------------

import os  # noqa: E402
import subprocess  # noqa: E402

from meridian.prospect import (  # noqa: E402
    _looks_like_git_commit,
    _git_commit_drift_sync,
    _detect_graph_commit_drift,
)
import meridian.routes.tunnel as _tunnel_mod  # noqa: E402
import meridian.hardening as _hardening_mod  # noqa: E402
import meridian.prospect as _prospect_mod  # noqa: E402


class TestLooksLikeGitCommit:
    def test_accepts_full_sha(self):
        assert _looks_like_git_commit("a" * 40) is True

    def test_accepts_short_sha(self):
        assert _looks_like_git_commit("abc1234") is True

    def test_rejects_iso_timestamp(self):
        assert _looks_like_git_commit("2026-07-13T00:00:00Z") is False

    def test_rejects_too_short(self):
        assert _looks_like_git_commit("abc12") is False

    def test_rejects_none_and_empty(self):
        assert _looks_like_git_commit(None) is False
        assert _looks_like_git_commit("") is False


def _run_git(args: list, cwd: str) -> None:
    subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"},
    )


def _make_commit(repo_dir: str, filename: str, content: str) -> str:
    with open(os.path.join(repo_dir, filename), "w") as f:
        f.write(content)
    _run_git(["add", "-A"], repo_dir)
    _run_git(["commit", "-m", f"add {filename}"], repo_dir)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir,
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


class TestGitCommitDriftSync:
    """Real (temp, on-disk) git repos — no mocking of git itself."""

    def test_not_a_directory_returns_none(self):
        assert _git_commit_drift_sync("", "abc1234") is None
        assert _git_commit_drift_sync("/definitely/not/a/real/path/xyz", "abc1234") is None

    def test_not_a_git_repo_returns_none(self, tmp_path):
        d = tmp_path / "not_a_repo"
        d.mkdir()
        assert _git_commit_drift_sync(str(d), "abc1234") is None

    def test_no_drift_when_stored_equals_head(self, tmp_path):
        repo = tmp_path / "repo1"
        repo.mkdir()
        _run_git(["init"], str(repo))
        commit1 = _make_commit(str(repo), "a.txt", "one")
        assert _git_commit_drift_sync(str(repo), commit1) is None

    def test_drift_detected_after_new_commits(self, tmp_path):
        repo = tmp_path / "repo2"
        repo.mkdir()
        _run_git(["init"], str(repo))
        commit1 = _make_commit(str(repo), "a.txt", "one")
        _make_commit(str(repo), "b.txt", "two")
        commit3 = _make_commit(str(repo), "c.txt", "three")

        drift = _git_commit_drift_sync(str(repo), commit1)
        assert drift is not None
        assert drift["stored_commit"] == commit1
        assert drift["head_commit"] == commit3
        assert drift["commits_since_index"] == 2

    def test_unresolvable_stored_commit_returns_none(self, tmp_path):
        """A fingerprint from an unrelated repo/history — fail open, don't guess."""
        repo = tmp_path / "repo3"
        repo.mkdir()
        _run_git(["init"], str(repo))
        _make_commit(str(repo), "a.txt", "one")
        bogus_commit = "f" * 40
        assert _git_commit_drift_sync(str(repo), bogus_commit) is None


class TestDetectGraphCommitDrift:
    """The async wrapper: no-baseline / non-git-fingerprint short-circuits,
    and the real drift path goes through hardening.run_in_bulkhead."""

    @pytest.mark.asyncio
    async def test_no_tenant_or_root_dir_returns_none(self):
        assert await _detect_graph_commit_drift("", "proj", "/some/dir") is None
        assert await _detect_graph_commit_drift("tenant", "proj", "") is None

    @pytest.mark.asyncio
    async def test_no_stored_fingerprint_skips_probe_entirely(self, monkeypatch):
        """No baseline → no git subprocess at all (mirrors the fingerprint
        mechanism's own 'no baseline, no fetch' discipline)."""
        called = {"bulkhead": False}

        monkeypatch.setattr(
            _tunnel_mod, "get_cached_graph_fingerprint", lambda tid, pid=None: None,
        )

        async def _fake_bulkhead(fn, *a, **kw):
            called["bulkhead"] = True
            return {"drift": True}

        monkeypatch.setattr(_hardening_mod, "run_in_bulkhead", _fake_bulkhead)

        out = await _detect_graph_commit_drift("tenant-x", "proj", "/some/dir")
        assert out is None
        assert called["bulkhead"] is False

    @pytest.mark.asyncio
    async def test_non_git_fingerprint_skips_probe(self, monkeypatch):
        """An indexed_at ISO timestamp fingerprint must never reach git."""
        called = {"bulkhead": False}

        monkeypatch.setattr(
            _tunnel_mod, "get_cached_graph_fingerprint",
            lambda tid, pid=None: "2026-07-13T00:00:00Z",
        )

        async def _fake_bulkhead(fn, *a, **kw):
            called["bulkhead"] = True
            return {"drift": True}

        monkeypatch.setattr(_hardening_mod, "run_in_bulkhead", _fake_bulkhead)

        out = await _detect_graph_commit_drift("tenant-x", "proj", "/some/dir")
        assert out is None
        assert called["bulkhead"] is False

    @pytest.mark.asyncio
    async def test_git_fingerprint_runs_through_bulkhead(self, monkeypatch):
        """A plausible commit-hash fingerprint dispatches the sync probe via
        run_in_bulkhead (thread pool — never asyncio subprocess, which is
        unsupported on the SelectorEventLoop this project forces on Windows)."""
        seen_args = []

        monkeypatch.setattr(
            _tunnel_mod, "get_cached_graph_fingerprint",
            lambda tid, pid=None: "abc1234",
        )

        async def _fake_bulkhead(fn, *a, **kw):
            seen_args.append((fn, a, kw))
            return {
                "stored_commit": "abc1234", "head_commit": "def5678",
                "commits_since_index": 4,
            }

        monkeypatch.setattr(_hardening_mod, "run_in_bulkhead", _fake_bulkhead)

        out = await _detect_graph_commit_drift("tenant-x", "proj", "/some/dir")
        assert out == {
            "stored_commit": "abc1234", "head_commit": "def5678",
            "commits_since_index": 4,
        }
        assert len(seen_args) == 1
        fn, a, kw = seen_args[0]
        assert fn is _prospect_mod._git_commit_drift_sync
        assert a == ("/some/dir", "abc1234")
        assert kw.get("label") == "prospect_symbol_commit_drift"


class TestGetCachedGraphFingerprint:
    """tunnel.get_cached_graph_fingerprint — public accessor used by the
    commit-drift probe."""

    def test_returns_project_specific_when_present(self):
        tunnel._code_graph_fingerprints["tenant-acc:myrepo"] = "PROJECT-FP"
        try:
            assert tunnel.get_cached_graph_fingerprint("tenant-acc", "myrepo") == "PROJECT-FP"
        finally:
            tunnel._code_graph_fingerprints.pop("tenant-acc:myrepo", None)

    def test_falls_back_to_wildcard(self):
        tunnel._code_graph_fingerprints["tenant-acc2:*"] = "WILDCARD-FP"
        try:
            assert tunnel.get_cached_graph_fingerprint("tenant-acc2", "myrepo") == "WILDCARD-FP"
        finally:
            tunnel._code_graph_fingerprints.pop("tenant-acc2:*", None)

    def test_returns_none_when_absent(self):
        tunnel._code_graph_fingerprints.pop("tenant-acc3:myrepo", None)
        tunnel._code_graph_fingerprints.pop("tenant-acc3:*", None)
        assert tunnel.get_cached_graph_fingerprint("tenant-acc3", "myrepo") is None

    def test_no_project_id_uses_wildcard_key_directly(self):
        tunnel._code_graph_fingerprints["tenant-acc4:*"] = "WILDCARD-ONLY"
        try:
            assert tunnel.get_cached_graph_fingerprint("tenant-acc4") == "WILDCARD-ONLY"
        finally:
            tunnel._code_graph_fingerprints.pop("tenant-acc4:*", None)


class TestProspectSymbolCommitDriftIntegration:
    """End-to-end through prospect_symbol_impl: a detected commit drift skips
    the graph rung exactly like an explicit stale_graph=True, but with a
    distinguishing fallback_reason and a _graph_commit_drift diagnostic."""

    @pytest.mark.asyncio
    async def test_drift_detected_skips_graph_goes_to_serena(self, monkeypatch):
        import meridian.routes.tunnel as _tm

        drift_info = {
            "stored_commit": "abc1234", "head_commit": "def5678",
            "commits_since_index": 4,
        }

        async def _fake_detect(tenant_id, project_id, root_dir):
            return drift_info

        monkeypatch.setattr(_prospect_mod, "_detect_graph_commit_drift", _fake_detect)

        serena_result = _text_result([
            {"file": "meridian/server.py", "line_start": 900, "name": "some_fn"},
        ])
        call_log: list[str] = []

        async def _fake_call_tunnel(tid, name, args, **kw):
            call_log.append(name)
            if name == "extractor__find_symbol":
                return serena_result
            raise AssertionError(f"should not call {name}")

        monkeypatch.setattr(_tm, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tm, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="some_fn",
            project_id="myrepo",
            root_dir="/repo",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant={"id": "tenant-drift"},
            data_dir="",
        )
        # Graph rung was skipped entirely — never called.
        assert "codebase__search_graph" not in call_log
        assert result["rung"] == "serena"
        assert result["hits"][0]["name"] == "some_fn"
        assert result["_graph_commit_drift"] == drift_info
        assert "graph_skipped_commit_drift_detected" in result["fallback_reason"]
        assert "4 commit(s)" in result["fallback_reason"]

    @pytest.mark.asyncio
    async def test_no_drift_proceeds_to_graph_normally(self, monkeypatch):
        """When the probe finds nothing (no baseline, unchanged, whatever),
        the graph rung runs exactly as before — no regression."""
        import meridian.routes.tunnel as _tm

        async def _fake_detect(tenant_id, project_id, root_dir):
            return None

        monkeypatch.setattr(_prospect_mod, "_detect_graph_commit_drift", _fake_detect)

        graph_result = _text_result({"results": [
            {"file": "meridian/server.py", "line": 42, "name": "some_fn"},
        ]})

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "codebase__search_graph":
                return graph_result
            raise AssertionError(f"should not call {name}")

        monkeypatch.setattr(_tm, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tm, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="some_fn",
            project_id="myrepo",
            root_dir="/repo",
            limit=5,
            kind=None,
            stale_graph=False,
            tenant={"id": "tenant-nodrift"},
            data_dir="",
        )
        assert result["rung"] == "graph"
        assert "_graph_commit_drift" not in result

    @pytest.mark.asyncio
    async def test_explicit_stale_graph_skips_drift_probe(self, monkeypatch):
        """When the caller already passed stale_graph=True, the drift probe
        must not even run (redundant — avoid an unnecessary git subprocess)."""
        import meridian.routes.tunnel as _tm

        probe_called = {"yes": False}

        async def _fake_detect(tenant_id, project_id, root_dir):
            probe_called["yes"] = True
            return None

        monkeypatch.setattr(_prospect_mod, "_detect_graph_commit_drift", _fake_detect)

        async def _fake_call_tunnel(tid, name, args, **kw):
            if name == "extractor__find_symbol":
                return _text_result([{"file": "x.py", "name": "fn"}])
            raise AssertionError(f"should not call {name}")

        monkeypatch.setattr(_tm, "call_tunnel_tool", _fake_call_tunnel)
        monkeypatch.setattr(_tm, "has_active_tunnel", lambda tid: True)

        result = await _prospect_symbol_impl(
            symbol="fn",
            project_id="",
            root_dir="/repo",
            limit=5,
            kind=None,
            stale_graph=True,
            tenant={"id": "tenant-explicit-stale"},
            data_dir="",
        )
        assert probe_called["yes"] is False
        assert result["fallback_reason"] == "graph_skipped_stale_graph=true"


# ---------------------------------------------------------------------------
# c95d0c12 — bound codebase-memory reindex to the active repository and
# exclude nested worktrees.
# ---------------------------------------------------------------------------

from meridian.code_index import (  # noqa: E402
    compute_bounded_reindex_scope,
    is_index_repository_failure,
)


class TestComputeBoundedReindexScope:
    def test_safe_when_no_worktree_containers(self, tmp_path):
        (tmp_path / "meridian").mkdir()
        result = compute_bounded_reindex_scope(str(tmp_path))
        assert result["safe"] is True
        assert result["nested_worktree_count"] == 0
        assert result["recommended_repo_path"] == str(tmp_path)

    def test_safe_when_worktree_count_at_or_below_threshold(self, tmp_path):
        worktrees = tmp_path / ".claude" / "worktrees"
        worktrees.mkdir(parents=True)
        for i in range(3):
            (worktrees / f"session-{i}").mkdir()

        result = compute_bounded_reindex_scope(str(tmp_path), worktree_threshold=5)
        assert result["nested_worktree_count"] == 3
        assert result["safe"] is True
        assert str(worktrees) in result["excluded_paths"]
        assert result["recommended_repo_path"] == str(tmp_path)

    def test_unsafe_when_worktree_count_exceeds_threshold_falls_back_to_package_dir(
        self, tmp_path
    ):
        claude_worktrees = tmp_path / ".claude" / "worktrees"
        claude_worktrees.mkdir(parents=True)
        codex_worktrees = tmp_path / ".codex" / "worktrees"
        codex_worktrees.mkdir(parents=True)
        for i in range(4):
            (claude_worktrees / f"session-{i}").mkdir()
        for i in range(4):
            (codex_worktrees / f"session-{i}").mkdir()
        (tmp_path / "meridian").mkdir()

        result = compute_bounded_reindex_scope(str(tmp_path), worktree_threshold=5)
        assert result["nested_worktree_count"] == 8
        assert result["safe"] is False
        assert result["recommended_repo_path"] == str(tmp_path / "meridian")
        assert str(claude_worktrees) in result["excluded_paths"]
        assert str(codex_worktrees) in result["excluded_paths"]

    def test_excludes_conventional_cache_and_vcs_dirs(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / ".pixi").mkdir()

        result = compute_bounded_reindex_scope(str(tmp_path))
        assert str(tmp_path / ".git") in result["excluded_paths"]
        assert str(tmp_path / "__pycache__") in result["excluded_paths"]
        assert str(tmp_path / ".pixi") in result["excluded_paths"]

    def test_nonexistent_repo_path_is_safe_with_no_exclusions(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        result = compute_bounded_reindex_scope(str(missing))
        assert result["safe"] is True
        assert result["excluded_paths"] == []
        assert result["nested_worktree_count"] == 0

    def test_unsafe_with_no_package_dir_falls_back_to_repo_root(self, tmp_path):
        worktrees = tmp_path / ".claude" / "worktrees"
        worktrees.mkdir(parents=True)
        for i in range(10):
            (worktrees / f"session-{i}").mkdir()
        # No "meridian" or same-named package directory exists.

        result = compute_bounded_reindex_scope(str(tmp_path), worktree_threshold=5)
        assert result["safe"] is False
        assert result["recommended_repo_path"] == str(tmp_path)


class TestIsIndexRepositoryFailure:
    def test_none_result_is_failure(self):
        assert is_index_repository_failure(None) is True

    def test_empty_dict_is_failure(self):
        assert is_index_repository_failure({}) is True

    def test_non_dict_result_is_failure(self):
        assert is_index_repository_failure("502 Bad Gateway") is True
        assert is_index_repository_failure([]) is True

    def test_dict_with_error_field_is_failure(self):
        assert is_index_repository_failure({"error": "502 Bad Gateway"}) is True
        assert is_index_repository_failure({"indexed": True, "error": "timeout"}) is True

    def test_dict_without_error_field_is_success(self):
        assert is_index_repository_failure({"indexed": True, "git_commit": "abc123"}) is False

    def test_dict_with_falsy_error_field_is_success(self):
        assert is_index_repository_failure({"error": None}) is False
        assert is_index_repository_failure({"error": ""}) is False
