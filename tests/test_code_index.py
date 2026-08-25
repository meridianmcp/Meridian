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
# ec91e311 -- the shim must forward the extracted package's own explicit
# convergence/degraded state (e631d54f), not hand-roll a subset of its
# result dict. Before this item, ``ci.search_code_semantic`` built its OWN
# result (total_indexed / vectors_active / hits only) instead of delegating
# to ``meridian_codeindex.code_index.search_code_semantic``, so every MCP
# caller of the ``search_code_semantic`` tool -- and prospect_symbol's Rung 3
# -- silently never saw ``convergence``/``degraded`` even though the
# underlying CodeIndex computed it on every call.
# ===========================================================================

def test_search_code_semantic_forwards_convergence_bm25_only_not_degraded(
    monkeypatch, tmp_path,
):
    monkeypatch.delenv("MERIDIAN_CODE_INDEX_VECTORS", raising=False)
    _write(tmp_path / "svc.py", "def convergence_marker_zzqq():\n    return 1\n")
    db_path = str(tmp_path / "idx.duckdb")
    res = ci.search_code_semantic(
        str(tmp_path), "convergence_marker_zzqq", db_path=db_path,
    )
    assert "error" not in res, res
    assert "convergence" in res
    assert res["degraded"] is False
    assert res["convergence"]["degraded"] is False
    assert res["convergence"]["vectors_enabled"] is False
    assert res["convergence"]["index_revision"] >= 1
    assert res["convergence"]["root_dir"] == os.path.abspath(str(tmp_path))


def test_search_code_semantic_forwards_degraded_when_vector_leg_cant_embed(
    monkeypatch, tmp_path,
):
    """Vectors enabled but the embedder can't actually produce vectors (e.g.
    ``model2vec`` missing/broken) -- the shim's result must surface
    ``degraded=True`` rather than silently reporting a clean BM25 result.
    """
    from meridian_codeindex import code_index as impl

    monkeypatch.setattr(impl._Embedder, "available", lambda self: True)
    monkeypatch.setattr(impl._Embedder, "embed", lambda self, texts: None)
    _write(tmp_path / "svc.py", "def degraded_marker_zzqq():\n    return 1\n")
    db_path = str(tmp_path / "idx.duckdb")
    res = ci.search_code_semantic(
        str(tmp_path), "degraded_marker_zzqq", db_path=db_path,
    )
    assert "error" not in res, res
    assert res["degraded"] is True
    assert res["convergence"]["vectors_enabled"] is True
    assert res["convergence"]["vectors_ready"] is False


def test_search_code_semantic_hosted_guard_response_omits_convergence(
    monkeypatch, tmp_path,
):
    """The hosted-mode guard returns BEFORE any delegation -- it must never
    claim a convergence state for an index it never touched."""
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    res = ci.search_code_semantic(str(tmp_path), "anything")
    assert "convergence" not in res
    assert "degraded" not in res
    assert "error" in res


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
async def test_dispatch_search_code_semantic_includes_convergence_and_degraded(
    tmp_path,
):
    """ec91e311 -- routing-contract regression at the actual MCP tool-call
    boundary (not just the shim function): a client calling the
    ``search_code_semantic`` tool must see the same explicit
    convergence/degraded state a direct library caller gets."""
    from meridian import server as srv

    _write(tmp_path / "dispatched.py", "def dispatch_convergence_marker():\n    return 1\n")
    result = await srv._dispatch_mcp_tool(
        "search_code_semantic",
        {"root_dir": str(tmp_path), "query": "dispatch_convergence_marker"},
        None,
        str(tmp_path),
    )
    assert "error" not in result, result
    assert "convergence" in result
    assert result["degraded"] is False


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


def test_prospect_symbol_registered_in_stdio_surface():
    """d5e60791 — prospect_symbol was entirely absent from the stdio
    transport's tool list AND its call_tool dispatch (unlike
    search_code_semantic, which was already wired): a stdio-connected client
    (e.g. the documented self-hosted `pixi run python -m meridian --mcp`
    connection) got "unknown tool: prospect_symbol" no matter what. Assert
    the Tool() is registered and the dispatch elif chain routes it through
    the same shared _dispatch_mcp_tool as search_code_semantic."""
    import inspect

    from meridian.mcp import stdio_handler as sh

    source = inspect.getsource(sh)
    assert 'name="prospect_symbol"' in source
    # Dispatched via the same shared tuple search_code_semantic already uses.
    assert '"prospect_symbol",' in source


# ===========================================================================
# d5e60791 — packaging/boot-preflight resilience for the meridian_codeindex
# import. Live reproduction: an already-running MCP connector serving this
# exact checkout raised "No module named 'meridian_codeindex'" even though
# `pixi run python -c "import meridian_codeindex"` succeeded moments later
# from a shell in the SAME checkout -- a stale/never-resynced runtime
# environment, not a genuinely-missing package. meridian.code_index now
# self-heals by falling back to the vendored source tree that ships in every
# checkout (extensions/meridian-codeindex/meridian_codeindex/) before giving
# up, and raises an ACTIONABLE ImportError (not a bare ModuleNotFoundError)
# when even that fails.
# ===========================================================================

def test_ensure_meridian_codeindex_importable_succeeds_when_already_importable():
    """The common case (properly synced env, e.g. CI / this pixi env): no
    path surgery needed, returns None immediately."""
    err = ci._ensure_meridian_codeindex_importable()
    assert err is None


def test_ensure_meridian_codeindex_importable_falls_back_to_vendored_source(
    monkeypatch, tmp_path,
):
    """Simulates the exact live bug: the FIRST `import meridian_codeindex`
    fails (as if this runtime's site-packages never saw the pixi.toml
    editable-install entry). The boot preflight must locate the vendored
    source tree next to meridian/code_index.py, insert it onto sys.path, and
    retry -- succeeding on the second attempt."""
    import builtins
    import sys
    import types

    vendored_root = tmp_path / "extensions" / "meridian-codeindex"
    pkg_dir = vendored_root / "meridian_codeindex"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "meridian").mkdir()

    monkeypatch.setattr(ci, "__file__", str(tmp_path / "meridian" / "code_index.py"))

    call_count = {"n": 0}
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "meridian_codeindex":
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ModuleNotFoundError(
                    "No module named 'meridian_codeindex'", name="meridian_codeindex",
                )
            # Second attempt must only succeed because the vendored dir is
            # now on sys.path -- proves causation, not coincidence.
            assert str(vendored_root) in sys.path
            return types.ModuleType("meridian_codeindex")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    try:
        err = ci._ensure_meridian_codeindex_importable()
    finally:
        if str(vendored_root) in sys.path:
            sys.path.remove(str(vendored_root))

    assert err is None
    assert call_count["n"] == 2


def test_ensure_meridian_codeindex_importable_returns_original_error_without_vendored_source(
    monkeypatch, tmp_path,
):
    """When even the vendored source tree isn't present on disk (e.g. a
    frozen/packaged distribution that never ships extensions/), the ORIGINAL
    ModuleNotFoundError is returned -- never masked, never crashes."""
    import builtins

    (tmp_path / "meridian").mkdir()
    monkeypatch.setattr(ci, "__file__", str(tmp_path / "meridian" / "code_index.py"))
    # Deliberately no extensions/meridian-codeindex/ under tmp_path.

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "meridian_codeindex":
            raise ModuleNotFoundError(
                "No module named 'meridian_codeindex'", name="meridian_codeindex",
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    err = ci._ensure_meridian_codeindex_importable()
    assert isinstance(err, ModuleNotFoundError)
    assert err.name == "meridian_codeindex"


def test_ensure_meridian_codeindex_importable_does_not_mask_transitive_dependency_error(
    monkeypatch, tmp_path,
):
    """meridian_codeindex ITSELF being found but failing to import because
    one of ITS OWN dependencies (e.g. duckdb) is missing is a different
    problem sys.path surgery cannot fix -- must return that real error
    immediately, not silently swallow it into a misleading packaging-gap
    story."""
    import builtins

    vendored_root = tmp_path / "extensions" / "meridian-codeindex"
    pkg_dir = vendored_root / "meridian_codeindex"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "meridian").mkdir()
    monkeypatch.setattr(ci, "__file__", str(tmp_path / "meridian" / "code_index.py"))

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "meridian_codeindex":
            raise ModuleNotFoundError("No module named 'duckdb'", name="duckdb")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    err = ci._ensure_meridian_codeindex_importable()
    assert isinstance(err, ModuleNotFoundError)
    assert err.name == "duckdb"
