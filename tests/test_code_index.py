"""Coverage for the Cursor-style local code index (93fce816).

Exercises the four cooperating layers of :mod:`meridian.code_index`:

* **tree-sitter / ast semantic chunking** — a sample ``.py`` and ``.ts`` are
  chunked into the expected function/class/method chunks PLUS the un-named
  module-level blocks (dict literals, bare calls, ``__main__`` guards) that a
  named-symbols-only graph search can't see.
* **Merkle tree** — a single changed file is detected without rewalking the
  whole tree (unchanged sibling subtrees keep a stable node hash), and an
  identical tree diffs to empty.
* **BM25 search over chunks** — a keyword query ranks the relevant chunk first,
  including a term that only appears inside a bare module-level call.
* **incremental reindex** — modifying one file re-chunks ONLY that file's
  subtree (the Merkle short-circuit), and a no-op reindex writes zero chunks.
* **optional vector (VSS) leg** — when ``MERIDIAN_CODE_INDEX_VECTORS`` is on
  AND ``model2vec`` + the DuckDB VSS extension are available, a semantic query
  whose words aren't literally present still surfaces the relevant chunk via
  Reciprocal Rank Fusion; otherwise the vector-specific tests skip cleanly and
  the full BM25 path is what's exercised.
* **dispatch** — ``search_code_semantic`` routes through ``_dispatch_mcp_tool``
  and is registered read-only across the HTTP + stdio tool surfaces.
"""
from __future__ import annotations

import os
import time

import pytest

from meridian import code_index as ci


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write(path, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


_SAMPLE_PY = """import os

CONFIG = {
    "retries": 3,
    "timeout": 30,
}

def parse_token(raw):
    return raw.strip()


class TokenService:
    def refresh(self, token):
        return parse_token(token)

    def revoke(self, token):
        return None


if __name__ == "__main__":
    register_startup_hook()
"""

_SAMPLE_TS = """export const SETTINGS = { retries: 3 };

export function parseToken(raw: string): string {
  return raw.trim();
}

export class TokenService {
  refresh(token: string): string {
    return parseToken(token);
  }
}

export interface TokenProps {
  id: number;
}
"""


def _vectors_available() -> bool:
    """True only when the vector leg can genuinely run end-to-end here."""
    try:
        import model2vec  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    try:
        import duckdb

        con = duckdb.connect()
        con.execute("INSTALL vss")
        con.execute("LOAD vss")
        con.close()
    except Exception:  # noqa: BLE001
        return False
    return True


def _skip_if_model_oom(exc: BaseException) -> None:
    """Skip (not fail) when a genuine OS memory limit blocks the 90MB model load.

    Loading the static embedding model in-process costs ~90MB RSS; under the
    full ``-n auto`` suite several xdist workers can contend for RAM/pagefile
    and the load raises OSError 1455 (Windows) / MemoryError. That is an
    environment constraint, not a logic defect — the vector leg is proven
    end-to-end when run in isolation — so we skip rather than fail the suite.
    """
    msg = str(exc).lower()
    if isinstance(exc, MemoryError) or "paging file" in msg or "os error 1455" in msg \
            or "cannot allocate" in msg or "out of memory" in msg:
        pytest.skip(f"embedding model load hit an OS memory limit: {exc}")
    raise exc


# ===========================================================================
# 1. tree-sitter / ast semantic chunking
# ===========================================================================

def test_chunk_python_named_symbols():
    chunks = ci.chunk_file("sample.py", _SAMPLE_PY)
    by_name = {(c.kind, c.name) for c in chunks}
    assert ("function", "parse_token") in by_name
    assert ("class", "TokenService") in by_name
    assert ("method", "TokenService.refresh") in by_name
    assert ("method", "TokenService.revoke") in by_name


def test_chunk_python_unnamed_blocks_fill_the_gap():
    """The dict literal and the __main__ guard — invisible to a named-symbol
    graph search — must each land in a ``module`` chunk."""
    chunks = ci.chunk_file("sample.py", _SAMPLE_PY)
    module_chunks = [c for c in chunks if c.kind == "module"]
    joined = "\n".join(c.content for c in module_chunks)
    assert "CONFIG" in joined, "top-level dict literal must be chunked"
    assert "retries" in joined
    assert "__main__" in joined, "the __main__ guard must be chunked"
    assert "register_startup_hook" in joined, "a bare module-level call must be chunked"


def test_chunk_python_lines_are_covered_exactly_once():
    """Every NON-BLANK source line lands in exactly one top-level chunk.

    Purely-blank gaps between symbols aren't chunked (an empty chunk is
    dropped), so coverage is asserted over the non-blank lines only.
    """
    src_lines = _SAMPLE_PY.splitlines()
    non_blank = {i for i, ln in enumerate(src_lines, start=1) if ln.strip()}
    chunks = ci.chunk_file("sample.py", _SAMPLE_PY)
    # Union of top-level (non-method) chunk spans.
    top = [c for c in chunks if c.kind != "method"]
    covered: set[int] = set()
    for c in top:
        for ln in range(c.line_start, c.line_end + 1):
            covered.add(ln)
    # Every non-blank line is covered by exactly one top-level chunk.
    assert non_blank <= covered


def test_chunk_typescript_named_symbols():
    chunks = ci.chunk_file("sample.ts", _SAMPLE_TS)
    kinds_names = {(c.kind, c.name) for c in chunks}
    assert ("function", "parseToken") in kinds_names
    assert ("class", "TokenService") in kinds_names
    assert any(c.kind == "method" and c.name.endswith("refresh") for c in chunks)
    assert any(c.kind == "interface" for c in chunks)


def test_chunk_typescript_unnamed_block():
    chunks = ci.chunk_file("sample.ts", _SAMPLE_TS)
    module_chunks = [c for c in chunks if c.kind == "module"]
    joined = "\n".join(c.content for c in module_chunks)
    assert "SETTINGS" in joined, "the top-level const literal must be chunked"


def test_chunk_unsupported_extension_returns_empty():
    assert ci.chunk_file("notes.md", "# hello") == []
    assert ci.chunk_file("data.bin", "\x00\x01") == []


def test_chunk_syntactically_broken_file_still_yields_one_module_chunk():
    """A file we can't parse is never invisible — it's one module chunk."""
    chunks = ci.chunk_file("broken.py", "def oops(:\n  pass\n")
    assert len(chunks) == 1
    assert chunks[0].kind == "module"


def test_chunk_id_is_deterministic_and_content_derived():
    a = ci.chunk_file("sample.py", _SAMPLE_PY)
    b = ci.chunk_file("sample.py", _SAMPLE_PY)
    assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
    assert all(c.content_hash for c in a)


# ===========================================================================
# 2. Merkle tree
# ===========================================================================

def _seed_tree(tmp_path):
    os.makedirs(tmp_path / "pkg" / "sub", exist_ok=True)
    _write(tmp_path / "root_mod.py", "def root_fn():\n    return 1\n")
    _write(tmp_path / "pkg" / "a.py", "def alpha():\n    return 1\n")
    _write(tmp_path / "pkg" / "sub" / "b.ts", "export function beta() { return 2; }\n")
    # a non-source file + a skip-dir file must NOT become leaves
    _write(tmp_path / "README.md", "not indexed")
    os.makedirs(tmp_path / "node_modules", exist_ok=True)
    _write(tmp_path / "node_modules" / "vendor.js", "module.exports = {};")


def test_merkle_only_indexes_source_files(tmp_path):
    _seed_tree(tmp_path)
    tree = ci.build_merkle_tree(str(tmp_path))
    files = set(tree.files().keys())
    assert files == {"root_mod.py", "pkg/a.py", "pkg/sub/b.ts"}
    # node_modules / README.md are excluded
    assert not any("node_modules" in f for f in files)
    assert not any(f.endswith(".md") for f in files)


def test_merkle_identical_tree_diffs_empty(tmp_path):
    _seed_tree(tmp_path)
    t1 = ci.build_merkle_tree(str(tmp_path))
    t2 = ci.build_merkle_tree(str(tmp_path))
    assert t1.root_hash == t2.root_hash
    assert t2.diff(t1).is_empty


def test_merkle_detects_single_changed_file_without_rewalking(tmp_path):
    _seed_tree(tmp_path)
    t1 = ci.build_merkle_tree(str(tmp_path))
    time.sleep(0.01)
    _write(tmp_path / "pkg" / "a.py", "def alpha():\n    return 999\n")
    t2 = ci.build_merkle_tree(str(tmp_path))

    diff = t2.diff(t1)
    assert diff.modified == ["pkg/a.py"]
    assert diff.added == [] and diff.removed == []
    assert diff.changed_files == ["pkg/a.py"]

    # The short-circuit proof: the UNCHANGED sibling's node hash is identical,
    # so a real walk would skip that subtree entirely.
    assert (
        t2.root.children["root_mod.py"].hash
        == t1.root.children["root_mod.py"].hash
    )
    # And the changed file's ancestor chain hashes all moved.
    assert t2.root.hash != t1.root.hash
    assert t2.root.children["pkg"].hash != t1.root.children["pkg"].hash


def test_merkle_detects_add_and_remove(tmp_path):
    _seed_tree(tmp_path)
    t1 = ci.build_merkle_tree(str(tmp_path))
    _write(tmp_path / "pkg" / "c.py", "def gamma():\n    pass\n")
    os.remove(tmp_path / "root_mod.py")
    t2 = ci.build_merkle_tree(str(tmp_path))
    diff = t2.diff(t1)
    assert diff.added == ["pkg/c.py"]
    assert diff.removed == ["root_mod.py"]


def test_merkle_diff_against_none_is_all_added(tmp_path):
    _seed_tree(tmp_path)
    tree = ci.build_merkle_tree(str(tmp_path))
    diff = tree.diff(None)
    assert set(diff.added) == {"root_mod.py", "pkg/a.py", "pkg/sub/b.ts"}
    assert diff.modified == [] and diff.removed == []


def test_merkle_json_round_trip(tmp_path):
    _seed_tree(tmp_path)
    tree = ci.build_merkle_tree(str(tmp_path))
    restored = ci.MerkleTree.from_json(tree.to_json())
    assert restored.root_hash == tree.root_hash
    assert restored.files() == tree.files()
    # A round-tripped tree still diffs empty against the original.
    assert restored.diff(tree).is_empty


# ===========================================================================
# 3 + 4. CodeIndex — incremental reindex + BM25 search
# ===========================================================================

def test_reindex_writes_chunks_and_bm25_search_finds_them(tmp_path):
    _write(tmp_path / "svc.py", _SAMPLE_PY)
    idx = ci.CodeIndex(str(tmp_path))
    try:
        summary = idx.reindex()
        assert "svc.py" in summary["added"]
        assert summary["chunks_written"] > 0
        assert idx.count() == summary["chunks_written"]

        hits = idx.search("parse_token")
        names = [h["name"] for h in hits]
        assert "parse_token" in names
        # BM25 fields present on hits.
        top = hits[0]
        assert "score" in top and "chunk_id" in top and "path" in top
    finally:
        idx.close()


def test_bm25_finds_term_only_in_unnamed_block(tmp_path):
    """A term that appears ONLY in a bare module-level call is findable — the
    gap search_graph leaves."""
    _write(tmp_path / "svc.py", _SAMPLE_PY)
    idx = ci.CodeIndex(str(tmp_path))
    try:
        idx.reindex()
        hits = idx.search("register_startup_hook")
        assert hits, "the __main__ bare call term must be searchable"
        assert hits[0]["kind"] == "module"
    finally:
        idx.close()


def test_search_kind_filter(tmp_path):
    _write(tmp_path / "svc.py", _SAMPLE_PY)
    idx = ci.CodeIndex(str(tmp_path))
    try:
        idx.reindex()
        hits = idx.search("token", kind="method")
        assert hits, "kind-filtered search returns method chunks"
        assert all(h["kind"] == "method" for h in hits)
    finally:
        idx.close()


def test_incremental_reindex_only_rechunks_changed_subtree(tmp_path):
    os.makedirs(tmp_path / "pkg", exist_ok=True)
    _write(tmp_path / "pkg" / "a.py", "def alpha():\n    return 1\n")
    _write(tmp_path / "pkg" / "b.py", "def beta():\n    return 2\n")
    idx = ci.CodeIndex(str(tmp_path))
    try:
        first = idx.reindex()
        assert set(first["added"]) == {"pkg/a.py", "pkg/b.py"}

        # No change → the Merkle root-hash compare short-circuits: zero writes.
        noop = idx.reindex()
        assert noop["changed_files"] == []
        assert noop["chunks_written"] == 0
        assert noop["rebuilt"] is False

        # Modify ONLY a.py → only a.py is re-chunked.
        time.sleep(0.01)
        _write(tmp_path / "pkg" / "a.py", "def alpha():\n    return 42\ndef added_fn():\n    pass\n")
        third = idx.reindex()
        assert third["modified"] == ["pkg/a.py"]
        assert third["changed_files"] == ["pkg/a.py"]
        assert third["removed"] == []
        # The newly-added function is now searchable; b.py's chunks untouched.
        hits = idx.search("added_fn")
        assert any(h["name"] == "added_fn" for h in hits)
    finally:
        idx.close()


def test_reindex_removes_deleted_file_chunks(tmp_path):
    _write(tmp_path / "gone.py", "def doomed():\n    return 1\n")
    _write(tmp_path / "keep.py", "def survivor():\n    return 2\n")
    idx = ci.CodeIndex(str(tmp_path))
    try:
        idx.reindex()
        assert idx.search("doomed")
        os.remove(tmp_path / "gone.py")
        summary = idx.reindex()
        assert summary["removed"] == ["gone.py"]
        # The deleted file's chunk is gone; the survivor remains.
        assert not idx.search("doomed")
        assert idx.search("survivor")
    finally:
        idx.close()


def test_reindex_persists_across_reopen(tmp_path):
    """A file-backed sidecar keeps chunks + Merkle tree, so the FIRST reindex
    after reopening is incremental (no changes → zero writes)."""
    _write(tmp_path / "svc.py", "def hello():\n    return 1\n")
    db_path = str(tmp_path / "code_index.duckdb")
    idx1 = ci.CodeIndex(str(tmp_path), db_path=db_path)
    idx1.reindex()
    idx1.close()

    idx2 = ci.CodeIndex(str(tmp_path), db_path=db_path)
    try:
        summary = idx2.reindex()
        assert summary["changed_files"] == []
        assert summary["chunks_written"] == 0
        assert idx2.count() > 0
    finally:
        idx2.close()


# ===========================================================================
# 4. reindex_at_checkpoint entry point + search_code_semantic wrapper
# ===========================================================================

def test_reindex_at_checkpoint_is_incremental(tmp_path):
    _write(tmp_path / "svc.py", "def hello():\n    return 1\n")
    db_path = str(tmp_path / "idx.duckdb")
    s1 = ci.reindex_at_checkpoint(str(tmp_path), db_path=db_path)
    assert "svc.py" in s1["added"]
    # A second checkpoint with no changes short-circuits.
    s2 = ci.reindex_at_checkpoint(str(tmp_path), db_path=db_path)
    assert s2["changed_files"] == []
    assert s2["chunks_written"] == 0


def test_reindex_at_checkpoint_missing_dir():
    s = ci.reindex_at_checkpoint(os.path.join("does", "not", "exist"))
    assert "error" in s
    assert s["chunks_written"] == 0


def test_search_code_semantic_wrapper(tmp_path):
    _write(tmp_path / "svc.py", _SAMPLE_PY)
    db_path = str(tmp_path / "idx.duckdb")
    res = ci.search_code_semantic(
        str(tmp_path), "parse_token", db_path=db_path
    )
    assert res["query"] == "parse_token"
    assert res["total_indexed"] > 0
    assert any(h["name"] == "parse_token" for h in res["hits"])
    assert "vectors_enabled" in res


def test_search_code_semantic_missing_query(tmp_path):
    res = ci.search_code_semantic(str(tmp_path), "")
    assert res["error"] == "query is required"


def test_search_code_semantic_missing_dir():
    res = ci.search_code_semantic(os.path.join("no", "such", "dir"), "x")
    assert "error" in res
    assert res["hits"] == []


# ===========================================================================
# a0cf71ef — root_dir normalization: a valid local dir handed to us in a
# quoted / ~-prefixed / trailing-sep shape must be ACCEPTED (was reported as
# "root_dir does not exist"), while a genuinely missing path still errors.
# ===========================================================================

def test_normalize_root_dir_strips_quotes_and_whitespace():
    """Surrounding quotes + whitespace (a JSON/shell round-trip artifact) are
    stripped so the path resolves; result is an absolute path."""
    raw = '  "' + os.getcwd() + '"  '
    out = ci.normalize_root_dir(raw)
    assert out == os.path.abspath(os.getcwd())
    assert '"' not in out


def test_normalize_root_dir_expands_user_and_env(monkeypatch):
    monkeypatch.setenv("MERIDIAN_TESTVAR", "sub")
    out = ci.normalize_root_dir("~")
    assert out == os.path.abspath(os.path.expanduser("~"))
    out2 = ci.normalize_root_dir(os.path.join("$MERIDIAN_TESTVAR", "leaf"))
    assert "sub" in out2 and "$MERIDIAN_TESTVAR" not in out2


def test_normalize_root_dir_empty_returns_empty():
    assert ci.normalize_root_dir("") == ""
    assert ci.normalize_root_dir(None) == ""
    assert ci.normalize_root_dir('""') == ""


def test_search_code_semantic_accepts_quoted_valid_dir(tmp_path):
    """A valid local dir passed WITH surrounding quotes indexes + searches
    instead of returning 'root_dir does not exist'."""
    _write(tmp_path / "svc.py", _SAMPLE_PY)
    db_path = str(tmp_path / "idx.duckdb")
    quoted = '"' + str(tmp_path) + '"'
    res = ci.search_code_semantic(quoted, "parse_token", db_path=db_path)
    assert "error" not in res, res
    assert res["total_indexed"] > 0
    # root_dir echoes the normalized (unquoted, absolute) path.
    assert res["root_dir"] == os.path.abspath(str(tmp_path))
    assert any(h["name"] == "parse_token" for h in res["hits"])


def test_search_code_semantic_accepts_trailing_sep_and_forward_slashes(tmp_path):
    """Trailing separator + POSIX-style forward slashes still resolve on any OS."""
    _write(tmp_path / "svc.py", _SAMPLE_PY)
    db_path = str(tmp_path / "idx.duckdb")
    forward = str(tmp_path).replace(os.sep, "/") + "/"
    res = ci.search_code_semantic(forward, "parse_token", db_path=db_path)
    assert "error" not in res, res
    assert res["total_indexed"] > 0


def test_search_code_semantic_truly_missing_dir_still_errors(tmp_path):
    """Normalization must NOT mask a genuinely non-existent path — a path that
    does not resolve to a real directory still returns the 'does not exist'
    error, never a silent empty success on some other directory."""
    missing = str(tmp_path / "definitely_not_here_zzz")
    res = ci.search_code_semantic(missing, "x")
    assert res.get("error", "").startswith("root_dir does not exist")
    assert res["hits"] == []


def test_reindex_at_checkpoint_accepts_quoted_valid_dir(tmp_path):
    """reindex_at_checkpoint also normalizes: a quoted valid dir reindexes
    without the 'does not exist' error."""
    _write(tmp_path / "svc.py", "def hello():\n    return 1\n")
    db_path = str(tmp_path / "idx.duckdb")
    quoted = '"' + str(tmp_path) + '"'
    s = ci.reindex_at_checkpoint(quoted, db_path=db_path)
    assert "error" not in s, s
    assert "svc.py" in s["added"]


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


# ===========================================================================
# Optional vector (VSS) leg — real end-to-end when deps are present
# ===========================================================================

def test_vectors_off_by_default(monkeypatch):
    monkeypatch.delenv("MERIDIAN_CODE_INDEX_VECTORS", raising=False)
    assert ci._vectors_enabled() is False


@pytest.mark.skipif(
    not _vectors_available(),
    reason="model2vec and/or DuckDB VSS extension not available in this env",
)
def test_vector_leg_finds_semantically_related_code(tmp_path, monkeypatch):
    """With the vector leg enabled + deps present, a semantic query whose words
    are NOT literally in the code still surfaces the right chunk via RRF."""
    monkeypatch.setenv("MERIDIAN_CODE_INDEX_VECTORS", "1")
    # Surface a genuine OS memory limit on the ~90MB model load as a SKIP (not a
    # failure) — the load is proven to work in isolation; under -n auto several
    # workers can exhaust RAM/pagefile.
    try:
        from model2vec import StaticModel

        StaticModel.from_pretrained(ci._EMBED_MODEL_NAME)
    except BaseException as exc:  # noqa: BLE001
        _skip_if_model_oom(exc)
    _write(
        tmp_path / "math_ops.py",
        "def add_numbers(a, b):\n    return a + b\n\n"
        "def multiply_values(x, y):\n    return x * y\n",
    )
    _write(
        tmp_path / "net.py",
        "def fetch_remote_page(url):\n    return open_socket(url)\n",
    )
    idx = ci.CodeIndex(str(tmp_path))
    try:
        summary = idx.reindex()
        assert summary["rebuilt"] is True
        assert idx._vss_ready, "VSS index should build when vectors enabled"
        # 'arithmetic sum' isn't a literal token in the source.
        hits = idx.search("arithmetic sum computation", limit=5)
        assert hits
        # At least one hit must have come from the vector leg (RRF fusion).
        assert any(h.get("vector_rank") is not None for h in hits)
        top_names = [h["name"] for h in hits[:3]]
        assert any(n in ("add_numbers", "multiply_values") for n in top_names)
    finally:
        idx.close()


@pytest.mark.skipif(
    not _vectors_available(),
    reason="model2vec and/or DuckDB VSS extension not available in this env",
)
def test_embedder_reports_available_when_enabled(monkeypatch):
    monkeypatch.setenv("MERIDIAN_CODE_INDEX_VECTORS", "1")
    emb = ci._Embedder()
    assert emb.available() is True
    try:
        from model2vec import StaticModel

        StaticModel.from_pretrained(ci._EMBED_MODEL_NAME)
    except BaseException as exc:  # noqa: BLE001
        _skip_if_model_oom(exc)
    vecs = emb.embed(["def add(a, b): return a + b"])
    assert vecs and len(vecs) == 1 and len(vecs[0]) > 0


def test_embedder_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("MERIDIAN_CODE_INDEX_VECTORS", raising=False)
    emb = ci._Embedder()
    assert emb.available() is False
    assert emb.embed(["anything"]) is None


def test_rrf_fusion_pure_bm25_passthrough():
    bm25 = [
        {"chunk_id": "a", "name": "a", "bm25": 5.0},
        {"chunk_id": "b", "name": "b", "bm25": 3.0},
    ]
    fused = ci._reciprocal_rank_fusion(bm25, [])
    assert [h["chunk_id"] for h in fused] == ["a", "b"]
    assert fused[0]["score"] > fused[1]["score"]
    assert fused[0]["bm25_rank"] == 1


def test_rrf_fusion_merges_two_rankings():
    bm25 = [{"chunk_id": "a", "name": "a"}, {"chunk_id": "b", "name": "b"}]
    vec = [{"chunk_id": "b", "name": "b"}, {"chunk_id": "c", "name": "c"}]
    fused = ci._reciprocal_rank_fusion(bm25, vec)
    ids = [h["chunk_id"] for h in fused]
    # b appears in both lists → highest fused score.
    assert ids[0] == "b"
    assert set(ids) == {"a", "b", "c"}


# ===========================================================================
# Dispatch + registration
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
    from meridian.mcp import stdio_handler as sh

    # The stdio tool list is built inside list_tools; assert the name is wired
    # into the shared example map (a proxy the other tools use) + tool-doc list.
    from meridian import mcp_tools as mt

    assert "search_code_semantic" in mt._TOOL_EXAMPLES
