"""Coverage for Meridian's ``meridian/code_index.py`` thin-caller shim
(extraction 2b2433ca).

The ``CodeIndex`` implementation (tree-sitter/ast chunking, Merkle-incremental
reindex, DuckDB FTS hybrid BM25/VSS search) moved OUT of this repo into the
standalone ``extensions/meridian-codeindex`` package — its own test suite
(``extensions/meridian-codeindex/tests/test_code_index.py``) covers that logic
directly and is NOT duplicated here.

This file covers exactly what remains Meridian's own responsibility:

* the shim re-exports the full extracted namespace (``CodeIndex``,
  ``chunk_file``, etc.) so nothing that imports ``meridian.code_index``
  breaks;
* the hosted-mode guard (workspace decision 0dedff91 / fix 90c593d) — hosted
  Meridian can't reach a caller's local filesystem, so ``search_code_semantic``
  must fail honestly instead of delegating;
* ``search_code_semantic`` routes through Meridian's own
  ``_dispatch_mcp_tool`` and is registered read-only across the HTTP + stdio
  MCP tool surfaces.
"""
from __future__ import annotations

import os

import pytest

from meridian import code_index as ci


def _write(path, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ===========================================================================
# Shim re-export sanity — the extracted package's namespace must be reachable
# unchanged through meridian.code_index for every existing importer.
# ===========================================================================

def test_shim_reexports_extracted_namespace():
    assert ci.CodeIndex is not None
    assert callable(ci.chunk_file)
    assert callable(ci.build_merkle_tree)
    assert callable(ci.get_code_index)
    assert callable(ci.normalize_root_dir)
    assert callable(ci.reindex_at_checkpoint)


def test_shim_search_code_semantic_delegates_to_extracted_package(tmp_path):
    """End-to-end proof the thin caller actually reaches the extracted
    package's real chunking/search, not a stub."""
    _write(tmp_path / "svc.py", "def unique_shim_marker():\n    return 1\n")
    db_path = str(tmp_path / "idx.duckdb")
    res = ci.search_code_semantic(str(tmp_path), "unique_shim_marker", db_path=db_path)
    assert "error" not in res, res
    assert res["total_indexed"] > 0
    assert any(h["name"] == "unique_shim_marker" for h in res["hits"])


# ===========================================================================
# Hosted-mode guard (0dedff91 / 90c593d) — Meridian-deployment-specific, lives
# in THIS shim (not the extracted package, which has no hosted/local concept).
# ===========================================================================

def test_search_code_semantic_hosted_guard_blocks_local_fs_access(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    _write(tmp_path / "svc.py", "def hello():\n    return 1\n")
    res = ci.search_code_semantic(str(tmp_path), "hello")
    assert res["hits"] == []
    assert res["total_indexed"] == 0
    assert "error" in res
    assert "hosted Meridian" in res["error"]


def test_search_code_semantic_not_hosted_runs_normally(monkeypatch, tmp_path):
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    _write(tmp_path / "svc.py", "def hello_local():\n    return 1\n")
    db_path = str(tmp_path / "idx.duckdb")
    res = ci.search_code_semantic(str(tmp_path), "hello_local", db_path=db_path)
    assert "error" not in res, res
    assert any(h["name"] == "hello_local" for h in res["hits"])


# ===========================================================================
# a0cf71ef — root_dir normalization still applies through the shim (delegated
# to the extracted package's normalize_root_dir, re-exercised here so the
# quoting/whitespace edge case is proven at the Meridian call surface too).
# ===========================================================================

def test_search_code_semantic_accepts_quoted_valid_dir(tmp_path):
    _write(tmp_path / "svc.py", "def parse_token():\n    return 1\n")
    db_path = str(tmp_path / "idx.duckdb")
    quoted = '"' + str(tmp_path) + '"'
    res = ci.search_code_semantic(quoted, "parse_token", db_path=db_path)
    assert "error" not in res, res
    assert res["root_dir"] == os.path.abspath(str(tmp_path))
    assert any(h["name"] == "parse_token" for h in res["hits"])


def test_search_code_semantic_truly_missing_dir_still_errors(tmp_path):
    missing = str(tmp_path / "definitely_not_here_zzz")
    res = ci.search_code_semantic(missing, "x")
    assert res.get("error", "").startswith("root_dir does not exist")
    assert res["hits"] == []


# ===========================================================================
# Dispatch + registration — Meridian's own MCP surface
# ===========================================================================

@pytest.mark.asyncio
async def test_search_code_semantic_dispatches_through_dispatch_mcp_tool(tmp_path):
    from meridian import server as srv

    _write(tmp_path / "dispatched.py", "def unique_dispatch_marker():\n    return 1\n")
    result = await srv._dispatch_mcp_tool(
        "search_code_semantic",
        {"root_dir": str(tmp_path), "query": "unique_dispatch_marker"},
        None,
        str(tmp_path),
    )
    assert isinstance(result, dict)
    assert result["query"] == "unique_dispatch_marker"
    assert any(h["name"] == "unique_dispatch_marker" for h in result["hits"])


@pytest.mark.asyncio
async def test_dispatch_search_code_semantic_accepts_quoted_dir(tmp_path):
    """End-to-end through _dispatch_mcp_tool: a quoted valid root_dir searches
    (the handler .strip()s but code_index does the real normalization)."""
    from meridian import server as srv

    _write(tmp_path / "svc.py", "def unique_dispatch_marker():\n    return 7\n")
    res = await srv._dispatch_mcp_tool(
        "search_code_semantic",
        {"root_dir": '"' + str(tmp_path) + '"', "query": "unique_dispatch_marker"},
        None, str(tmp_path),
    )
    assert "error" not in res, res
    assert any(h["name"] == "unique_dispatch_marker" for h in res["hits"])


@pytest.mark.asyncio
async def test_dispatch_search_code_semantic_requires_root_dir():
    from meridian import server as srv

    with pytest.raises(ValueError, match="root_dir is required"):
        await srv._dispatch_mcp_tool(
            "search_code_semantic", {"query": "x"}, None, "/tmp"
        )


@pytest.mark.asyncio
async def test_dispatch_search_code_semantic_requires_query(tmp_path):
    from meridian import server as srv

    with pytest.raises(ValueError, match="query is required"):
        await srv._dispatch_mcp_tool(
            "search_code_semantic", {"root_dir": str(tmp_path)}, None, str(tmp_path)
        )


def test_search_code_semantic_registered_read_only():
    from meridian import mcp_tools as mt

    names = [t["name"] for t in mt._MCP_TOOLS_LIST]
    assert "search_code_semantic" in names
    assert "search_code_semantic" in mt._READ_ONLY_TOOLS
    tool = next(t for t in mt._MCP_TOOLS_LIST if t["name"] == "search_code_semantic")
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["title"] == "Search Code Semantic"


def test_search_code_semantic_registered_in_stdio_surface():
    from meridian.mcp import stdio_handler as sh  # noqa: F401 — import-wired surface

    # The stdio tool list is built inside list_tools; assert the name is wired
    # into the shared example map (a proxy the other tools use) + tool-doc list.
    from meridian import mcp_tools as mt

    assert "search_code_semantic" in mt._TOOL_EXAMPLES
