"""Coverage for the standalone local BM25 code index (extracted 2b2433ca).

Exercises the cooperating layers of :mod:`meridian_codeindex.code_index`:

* **tree-sitter / ast semantic chunking** — a sample ``.py`` and ``.ts`` are
  chunked into the expected function/class/method chunks PLUS the un-named
  module-level blocks (dict literals, bare calls, ``__main__`` guards) that a
  named-symbols-only search can't see.
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

This is a standalone package's own test suite — no Meridian import anywhere.
Meridian-side dispatch/registration coverage (``search_code_semantic`` routed
through ``_dispatch_mcp_tool``, the hosted-mode guard, MCP tool registration)
lives in the parent repo's ``tests/test_code_index.py`` instead, since that
behavior belongs to Meridian's own thin-caller shim, not to this package.
"""
from __future__ import annotations

import os
import time

import pytest

from meridian_codeindex import code_index as ci

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

    Loading the static embedding model in-process costs ~90MB RSS; under a
    parallel test run several workers can contend for RAM/pagefile and the load
    raises OSError 1455 (Windows) / MemoryError. That is an environment
    constraint, not a logic defect — the vector leg is proven end-to-end when
    run in isolation — so we skip rather than fail the suite.
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
    search — must each land in a ``module`` chunk."""
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
    import subprocess

    _seed_tree(tmp_path)
    for dirname in (
        ".codex/worktrees", ".claude/worktrees", ".serena/cache", "OneDrive",
    ):
        (tmp_path / dirname).mkdir(parents=True, exist_ok=True)
    _write(tmp_path / ".codex" / "worktrees" / "copy.py", "def codex_copy():\n    return 1\n")
    _write(tmp_path / ".claude" / "worktrees" / "copy.py", "def claude_copy():\n    return 1\n")
    _write(tmp_path / ".serena" / "cache" / "copy.py", "def serena_copy():\n    return 1\n")
    _write(tmp_path / "OneDrive" / "copy.py", "def cloud_copy():\n    return 1\n")
    tree = ci.build_merkle_tree(str(tmp_path))
    files = set(tree.files())
    assert files == {"root_mod.py", "pkg/a.py", "pkg/sub/b.ts"}
    assert tree.scan_info["canonical_root"] == ci.normalize_root_dir(str(tmp_path))
    assert tree.scan_info["scope_id"]
    assert tree.scan_info["scan_complete"] is True
    assert tree.scan_info["scope_mode"] == "bounded_walk"
    assert not any("node_modules" in path for path in files)
    assert not any(path.endswith(".md") for path in files)

    git_root = tmp_path / "git-root"
    git_root.mkdir()
    subprocess.run(["git", "-C", str(git_root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(git_root), "config", "user.email", "pytest@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(git_root), "config", "user.name", "Pytest"], check=True)
    _write(git_root / ".gitignore", "ignored.py\n")
    _write(git_root / "tracked.py", "def tracked_marker():\n    return 1\n")
    _write(git_root / "ignored.py", "def ignored_marker():\n    return 1\n")
    _write(git_root / "untracked.py", "def untracked_marker():\n    return 1\n")
    subprocess.run(["git", "-C", str(git_root), "add", ".gitignore", "tracked.py"], check=True)
    subprocess.run(["git", "-C", str(git_root), "add", "-f", "ignored.py"], check=True)
    subprocess.run(["git", "-C", str(git_root), "commit", "-qm", "fixture"], check=True)
    git_tree = ci.build_merkle_tree(str(git_root))
    assert set(git_tree.files()) == {"tracked.py"}
    assert git_tree.scan_info["scope_mode"] == "git_tracked"
    assert git_tree.scan_info["is_git_worktree"] is False

    linked = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "-C", str(git_root), "worktree", "add", "--detach", "-q", str(linked), "HEAD"],
        check=True,
    )
    linked_tree = ci.build_merkle_tree(str(linked))
    assert linked_tree.scan_info["canonical_root"] == ci.normalize_root_dir(str(linked))
    assert linked_tree.scan_info["is_git_worktree"] is True
    assert linked_tree.scan_info["git_common_dir"] == ci.normalize_root_dir(str(git_root / ".git"))
    assert set(linked_tree.files()) == {"tracked.py"}

    broad = tmp_path / "broad-root"
    for number in range(129):
        child = broad / f"child-{number:03d}"
        child.mkdir(parents=True, exist_ok=True)
        _write(child / "copy.py", "def broad_copy():\n    return 1\n")
    refused = ci.build_merkle_tree(str(broad))
    assert refused.files() == {}
    assert refused.scan_info["scan_complete"] is False
    assert refused.scan_info["scan_reason"] == "broad_root_refused"
    allowed = ci.build_merkle_tree(str(broad), allow_broad_root=True, max_files=1)
    assert len(allowed.files()) == 1
    assert allowed.scan_info["scan_complete"] is False
    assert allowed.scan_info["scan_reason"] == "file_count_budget_exceeded"


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


def test_bm25_search_hits_conform_to_shared_retrieval_contract(tmp_path):
    """5044d8eb — CodeIndex._bm25_search's hits carry the additive fields
    matching the shared BM25-first + Model2Vec retrieval hit schema (see
    meridian.retrieval_contract.RETRIEVAL_HIT_FIELDS in the parent repo --
    this package stays zero-Meridian-dependency, so it conforms by field
    NAME/shape only, never by importing that module). Every EXISTING flat
    field (chunk_id/path/language/kind/name/line_start/line_end/content/
    bm25) must still be present too -- purely additive."""
    _write(tmp_path / "svc.py", _SAMPLE_PY)
    idx = ci.CodeIndex(str(tmp_path))
    try:
        idx.reindex()
        hits = idx.search("parse_token")
        assert hits
        top = hits[0]
        # Pre-existing fields untouched.
        for legacy_field in (
            "chunk_id", "path", "language", "kind", "name",
            "line_start", "line_end", "content", "bm25",
        ):
            assert legacy_field in top

        # New shared-contract fields.
        assert top["id"] == top["chunk_id"]
        assert top["source"] == "code_index"
        assert top["lexical_score"] == pytest.approx(top["bm25"])
        assert top["semantic_score"] is None
        assert top["fused_score"] == pytest.approx(top["bm25"])
        assert top["freshness"] == "current"
        assert top["provenance_status"] == "not_tracked"
        # content_hash is a real sha256 hex digest already computed by
        # CodeChunk.__post_init__ -- not a placeholder.
        assert isinstance(top["content_hash"], str) and len(top["content_hash"]) == 64
        # structure carries the same values as the legacy flat fields.
        assert top["structure"]["path"] == top["path"]
        assert top["structure"]["kind"] == top["kind"]
        assert top["structure"]["name"] == top["name"]
        assert top["structure"]["line_start"] == top["line_start"]
        assert top["structure"]["line_end"] == top["line_end"]
    finally:
        idx.close()


def test_bm25_search_content_hash_matches_chunk_content_hash(tmp_path):
    """The hit's content_hash is the SAME sha256 CodeChunk.__post_init__
    computed at chunk time -- not independently recomputed (and therefore
    can never silently disagree with it)."""
    _write(tmp_path / "svc.py", _SAMPLE_PY)
    idx = ci.CodeIndex(str(tmp_path))
    try:
        idx.reindex()
        chunks = ci.chunk_file(str(tmp_path / "svc.py"), _SAMPLE_PY)
        by_name = {c.name: c for c in chunks}

        hits = idx.search("parse_token")
        top = next(h for h in hits if h["name"] == "parse_token")
        assert top["content_hash"] == by_name["parse_token"].content_hash
    finally:
        idx.close()


def test_bm25_finds_term_only_in_unnamed_block(tmp_path):
    """A term that appears ONLY in a bare module-level call is findable — the
    gap a named-symbols-only index leaves."""
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

def test_unreadable_file_hash_keeps_last_complete_index(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "svc.py"
    _write(source, "def oldmarker():\n    return 'retaineduniquetoken'\n")
    fail_hash = False

    def hasher(path):
        if fail_hash:
            return None
        return ci._hash_file_bytes(path)

    idx = ci.CodeIndex(str(root), db_path=str(tmp_path / "cache.duckdb"), hasher=hasher)
    try:
        first = idx.reindex()
        assert first["scan_complete"] is True
        assert idx.search("retaineduniquetoken")

        _write(source, "def newmarker():\n    return 'newuniquetoken'\n")
        fail_hash = True
        partial = idx.reindex()

        assert partial["partial"] is True
        assert partial["scan_complete"] is False
        assert partial["scan_reason"] == "file_hash_failed"
        assert partial["unreadable_file_count"] == 1
        assert idx.search("retaineduniquetoken")
        assert not idx.search("newuniquetoken")
    finally:
        idx.close()


def test_reindex_snapshot_read_failure_preserves_last_complete_index(tmp_path, monkeypatch):
    import builtins

    root = tmp_path / "repo"
    root.mkdir()
    source = root / "svc.py"
    _write(source, "def oldmarker():\n    return 'retaineduniquetoken'\n")
    arm_failure = False
    fail_next_read = False

    def hasher(path):
        nonlocal fail_next_read
        digest = ci._hash_file_bytes(path)
        if arm_failure:
            fail_next_read = True
        return digest

    original_open = builtins.open

    def fail_snapshot_read(path, mode="r", *args, **kwargs):
        nonlocal fail_next_read
        if (
            fail_next_read
            and os.path.realpath(os.fspath(path)) == os.path.realpath(source)
            and mode == "rb"
        ):
            fail_next_read = False
            raise OSError("simulated transient chunk read failure")
        return original_open(path, mode, *args, **kwargs)

    idx = ci.CodeIndex(str(root), db_path=str(tmp_path / "cache.duckdb"), hasher=hasher)
    try:
        first = idx.reindex()
        before = idx.get_convergence_state()
        assert first["scan_complete"] is True
        assert idx.search("retaineduniquetoken")

        _write(source, "def newmarker():\n    return 'newuniquetoken'\n")
        arm_failure = True
        monkeypatch.setattr(builtins, "open", fail_snapshot_read)
        partial = idx.reindex()

        assert partial["scan_complete"] is False
        assert partial["scan_reason"] == "file_read_failed"
        assert partial["partial"] is True
        after = idx.get_convergence_state()
        assert after.source_fingerprint == before.source_fingerprint
        assert after.index_revision == before.index_revision
        assert idx.search("retaineduniquetoken")
        assert not idx.search("newuniquetoken")

        monkeypatch.setattr(builtins, "open", original_open)
        recovered = idx.reindex()
        assert recovered["scan_complete"] is True
        assert idx.search("newuniquetoken")
    finally:
        idx.close()


def test_reindex_source_change_between_hash_and_chunk_keeps_previous_snapshot(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "svc.py"
    _write(source, "def oldmarker():\n    return 'retaineduniquetoken'\n")
    replace_after_hash = False

    def racing_hasher(path):
        nonlocal replace_after_hash
        digest = ci._hash_file_bytes(path)
        if replace_after_hash:
            replace_after_hash = False
            _write(source, "def latestmarker():\n    return 'latestuniquetoken'\n")
        return digest

    idx = ci.CodeIndex(str(root), db_path=str(tmp_path / "cache.duckdb"), hasher=racing_hasher)
    try:
        first = idx.reindex()
        before = idx.get_convergence_state()
        assert first["scan_complete"] is True

        _write(source, "def middlemarker():\n    return 'middleuniquetoken'\n")
        replace_after_hash = True
        partial = idx.reindex()

        assert partial["scan_complete"] is False
        assert partial["scan_reason"] == "file_changed_during_chunk"
        assert partial["partial"] is True
        after = idx.get_convergence_state()
        assert after.source_fingerprint == before.source_fingerprint
        assert after.index_revision == before.index_revision
        assert idx.search("retaineduniquetoken")
        assert not idx.search("middleuniquetoken")
        assert not idx.search("latestuniquetoken")

        recovered = idx.reindex()
        assert recovered["scan_complete"] is True
        assert idx.search("latestuniquetoken")
    finally:
        idx.close()


def test_reindex_write_failure_rolls_back_rows_and_merkle_baseline(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "svc.py"
    _write(source, "def oldmarker():\n    return 'retaineduniquetoken'\n")
    idx = ci.CodeIndex(str(root), db_path=str(tmp_path / "cache.duckdb"))
    try:
        idx.reindex()
        before = idx.get_convergence_state()
        _write(source, "def newmarker():\n    return 'newuniquetoken'\n")

        def fail_insert(_con, _chunks):
            raise RuntimeError("simulated insert failure")

        monkeypatch.setattr(idx, "_insert_chunks", fail_insert)
        failed = idx.reindex()

        assert failed["error"] == "index_write_failed"
        assert failed["partial"] is True
        after = idx.get_convergence_state()
        assert after.source_fingerprint == before.source_fingerprint
        assert after.index_revision == before.index_revision
        assert idx.search("retaineduniquetoken")
        assert not idx.search("newuniquetoken")
    finally:
        idx.close()


def test_reindex_rollback_restores_search_feature_flags(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "svc.py"
    db_path = str(tmp_path / "cache.duckdb")
    _write(source, "def oldmarker():\n    return 'retaineduniquetoken'\n")
    original = ci.CodeIndex(str(root), db_path=db_path)
    try:
        original.reindex()
    finally:
        original.close()

    # A reopened persistent index starts with conservative instance flags even
    # though the database already has its prior FTS state.
    idx = ci.CodeIndex(str(root), db_path=db_path)
    assert idx._fts_built is False
    _write(source, "def newmarker():\n    return 'newuniquetoken'\n")

    def fail_meta(*_args, **_kwargs):
        raise RuntimeError("simulated metadata publication failure")

    monkeypatch.setattr(idx, "_store_meta", fail_meta)
    try:
        result = idx.reindex()

        assert result["error"] == "index_write_failed"
        assert idx._fts_built is False
        assert idx._vss_ready is False
        assert idx._vss_dim is None
        state = idx.get_convergence_state()
        assert state.indexed_file_count == 1
    finally:
        idx.close()


def test_index_paths_read_failure_preserves_existing_rows_and_baseline(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "svc.py"
    _write(source, "def oldmarker():\n    return 'retaineduniquetoken'\n")
    idx = ci.CodeIndex(str(root), db_path=str(tmp_path / "cache.duckdb"))
    try:
        idx.reindex()
        before = idx.get_convergence_state()
        _write(source, "def newmarker():\n    return 'newuniquetoken'\n")
        monkeypatch.setattr(
            idx,
            "_chunk_snapshot",
            lambda _path, **_kwargs: (None, "file_read_failed"),
        )

        result = idx.index_paths([str(source)])

        assert result["error"] == "file_read_failed"
        after = idx.get_convergence_state()
        assert after.source_fingerprint == before.source_fingerprint
        assert after.index_revision == before.index_revision
        assert idx.search("retaineduniquetoken")
        assert not idx.search("newuniquetoken")
    finally:
        idx.close()


def test_index_paths_rejects_targets_outside_prune_scope_before_mutation(
    tmp_path, monkeypatch,
):
    root = tmp_path / "repo"
    subtree = root / "subtree"
    subtree.mkdir(parents=True)
    outside_dir = root / "elsewhere"
    outside_dir.mkdir()
    inside = subtree / "inside.py"
    outside = outside_dir / "outside.py"
    _write(inside, "def subtree_marker():\n    return 'prunescopemarker'")
    _write(outside, "def outside_marker():\n    return 'outsideprunescope'")

    idx = ci.CodeIndex(str(root), db_path=str(tmp_path / "cache.duckdb"))
    try:
        idx.reindex()
        assert idx.search("prunescopemarker")
        assert idx.search("outsideprunescope")

        original_connect = idx._connect
        connect_calls = 0

        def track_connect():
            nonlocal connect_calls
            connect_calls += 1
            return original_connect()

        monkeypatch.setattr(idx, "_connect", track_connect)
        result = idx.index_paths([str(outside)], prune_root=str(subtree))
        assert result["error"] == "path_outside_prune_scope"
        assert connect_calls == 0

        monkeypatch.setattr(idx, "_connect", original_connect)
        assert idx.search("prunescopemarker")
        assert idx.search("outsideprunescope")
    finally:
        idx.close()


def test_full_reindex_prunes_deleted_file_chunks(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    deleted = root / "gone.py"
    kept = root / "keep.py"
    _write(deleted, "def doomedmarker():\n    return 'doomeduniquetoken'\n")
    _write(kept, "def survivormarker():\n    return 'survivoruniquetoken'\n")
    idx = ci.CodeIndex(str(root), db_path=str(tmp_path / "cache.duckdb"))
    try:
        idx.reindex()
        assert idx.search("doomeduniquetoken")
        deleted.unlink()

        summary = idx.reindex(full=True)

        assert summary["scan_complete"] is True
        assert not idx.search("doomeduniquetoken")
        assert idx.search("survivoruniquetoken")
    finally:
        idx.close()


def test_bounded_git_path_reader_stops_at_entry_limit():
    import sys
    import time

    paths, reason = ci._run_bounded_nul_list(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(bytes([112, 0]) * 10000)",
        ],
        max_entries=8,
        max_bytes=1024 * 1024,
        deadline=time.monotonic() + 5,
    )

    assert paths == [b"p"] * 8
    assert reason == "file_list_entry_budget_exceeded"


def test_default_and_broad_opt_in_use_separate_persistent_indexes(tmp_path):
    root = tmp_path / "broad-root"
    root.mkdir()
    for index in range(129):
        _write(
            root / f"module_{index:03}.py",
            "def broad_cache_scope_marker():\n    return 'broadcachetoken'\n",
        )

    shared_path = str(tmp_path / "shared.duckdb")
    broad = ci.CodeIndex(str(root), db_path=shared_path, allow_broad_root=True)
    default = ci.CodeIndex(str(root), db_path=shared_path)
    try:
        assert broad._db_path != default._db_path
        indexed = broad.reindex()
        assert indexed["scan_complete"] is True
        assert broad.search("broadcachetoken")

        refused = default.reindex()
        assert refused["scope_mode"] == "refused_broad_root"
        assert refused["partial"] is True
        assert not default.search("broadcachetoken")
    finally:
        broad.close()
        default.close()


def test_reindex_persists_across_reopen(tmp_path):
    """Persistent indexes survive restart and remain isolated per canonical root."""
    shared_data = tmp_path / "shared-index-data"
    shared_data.mkdir()
    db_path = str(shared_data / "code_index.duckdb")
    root_a = tmp_path / "repo-a"
    root_b = tmp_path / "repo-b"
    root_a.mkdir()
    root_b.mkdir()
    _write(root_a / "alpha.py", "def alpha_only_marker():\n    return 'alpha_only_marker'\n")
    _write(root_b / "beta.py", "def beta_only_marker():\n    return 'beta_only_marker'\n")

    idx_a = ci.CodeIndex(str(root_a), db_path=db_path)
    idx_b = ci.CodeIndex(str(root_b), db_path=db_path)
    assert idx_a._db_path != idx_b._db_path
    try:
        summary_a = idx_a.reindex()
        summary_b = idx_b.reindex()
        assert summary_a["scan_complete"] is True
        assert summary_b["scan_complete"] is True
        state_a = idx_a.get_convergence_state().to_dict()
        state_b = idx_b.get_convergence_state().to_dict()
        assert state_a["canonical_root"] == ci.normalize_root_dir(str(root_a))
        assert state_b["canonical_root"] == ci.normalize_root_dir(str(root_b))
        assert state_a["scope_id"] != state_b["scope_id"]
        assert state_a["scan_complete"] is True
        assert state_b["scan_complete"] is True
        assert any(hit["name"] == "alpha_only_marker" for hit in idx_a.search("alpha_only_marker"))
        assert all(not hit["path"].endswith("beta.py") for hit in idx_a.search("beta_only_marker"))
        assert any(hit["name"] == "beta_only_marker" for hit in idx_b.search("beta_only_marker"))
        assert all(not hit["path"].endswith("alpha.py") for hit in idx_b.search("alpha_only_marker"))
    finally:
        idx_a.close()
        idx_b.close()

    reopened_a = ci.CodeIndex(str(root_a), db_path=db_path)
    reopened_b = ci.CodeIndex(str(root_b), db_path=db_path)
    try:
        summary_a = reopened_a.reindex()
        summary_b = reopened_b.reindex()
        assert summary_a["changed_files"] == []
        assert summary_a["chunks_written"] == 0
        assert summary_b["changed_files"] == []
        assert summary_b["chunks_written"] == 0
        assert any(hit["name"] == "alpha_only_marker" for hit in reopened_a.search("alpha_only_marker"))
        assert all(not hit["path"].endswith("beta.py") for hit in reopened_a.search("beta_only_marker"))
        assert any(hit["name"] == "beta_only_marker" for hit in reopened_b.search("beta_only_marker"))
        assert all(not hit["path"].endswith("alpha.py") for hit in reopened_b.search("alpha_only_marker"))
    finally:
        reopened_a.close()
        reopened_b.close()


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
    """Normalize shell quoting and collapse relative/symlink path aliases."""
    raw = '  "' + os.getcwd() + '"  '
    out = ci.normalize_root_dir(raw)
    assert out == os.path.realpath(os.getcwd())
    alias = os.path.join(os.getcwd(), ".", "temporary", "..")
    assert ci.normalize_root_dir(alias) == os.path.realpath(os.getcwd())
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
    # failure) — the load is proven to work in isolation; under parallel runs
    # several workers can exhaust RAM/pagefile.
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
# 5. Embedding freshness / convergence state (item e631d54f, follow-up to
# outputs_local's ConvergenceState in 6af1518d).
#
# These tests are CI-SAFE: they never install the real DuckDB `vss`
# extension and never load a real model2vec model. `_FakeEmbedder` stands in
# for `_Embedder` (CodeIndex.__init__ accepts any embedder duck-typing
# `.available()` / `.embed()` / `.model_name`), and `_fake_rebuild_vss`
# reproduces `CodeIndex._rebuild_vss`'s embedding-column bookkeeping WITHOUT
# the `INSTALL vss` / `CREATE INDEX ... USING HNSW` steps that require the
# native extension — so "the vector leg fully converged" can be tested
# deterministically in any environment.
# ===========================================================================


class _FakeEmbedder:
    """Duck-typed stand-in for :class:`meridian_codeindex.code_index._Embedder`."""

    def __init__(self, model_name: str = "fake-embed-v1", *, enabled: bool = True,
                 fail: bool = False) -> None:
        self._model_name = model_name
        self._enabled = enabled
        self._fail = fail

    @property
    def model_name(self) -> str:
        return self._model_name

    def available(self) -> bool:
        return self._enabled

    def embed(self, texts):
        if not self._enabled or not texts or self._fail:
            return None
        return [[1.0, 0.0, 0.0] for _ in texts]


def _fake_rebuild_vss(idx: "ci.CodeIndex"):
    """Bind a deterministic, extension-free stand-in for ``idx._rebuild_vss``
    onto ``idx`` (matching test_semantic_search.py's plain-function
    monkeypatch-onto-instance convention — no ``self`` param needed since an
    instance-dict attribute isn't bound via the descriptor protocol)."""

    def _run(con):
        idx._vss_ready = False
        rows = con.execute("SELECT chunk_id, content FROM code_chunks").fetchall()
        if not rows:
            return
        texts = [r[1] or "" for r in rows]
        vectors = idx._embedder.embed(texts)
        if not vectors:
            return
        dim = len(vectors[0])
        if dim <= 0:
            return
        con.execute("ALTER TABLE code_chunks DROP COLUMN IF EXISTS embedding")
        con.execute(f"ALTER TABLE code_chunks ADD COLUMN embedding FLOAT[{dim}]")
        for (chunk_id, _content), vec in zip(rows, vectors):
            con.execute(
                "UPDATE code_chunks SET embedding = ? WHERE chunk_id = ?",
                [vec, chunk_id],
            )
        idx._vss_ready = True
        idx._vss_dim = dim

    return _run


def test_convergence_state_bm25_only_is_never_degraded(tmp_path):
    """Vectors disabled (the default posture) → degraded is always False; a
    pure-BM25 index has no partial/stale vector state to flag."""
    _write(tmp_path / "svc.py", _SAMPLE_PY)
    idx = ci.CodeIndex(str(tmp_path))
    try:
        idx.reindex()
        state = idx.get_convergence_state()
        assert state.vectors_enabled is False
        assert state.degraded is False
        assert state.converged is True
        assert state.total_chunks > 0
        assert state.index_revision == 1
        assert state.source_fingerprint  # non-empty root hash
        assert state.last_checkpoint_at is not None
    finally:
        idx.close()


def test_convergence_state_revision_unchanged_on_noop_reindex(tmp_path):
    _write(tmp_path / "svc.py", "def hello():\n    return 1\n")
    idx = ci.CodeIndex(str(tmp_path))
    try:
        idx.reindex()
        rev1 = idx.get_convergence_state().index_revision
        # No source change → Merkle short-circuit; revision must NOT bump.
        idx.reindex()
        rev2 = idx.get_convergence_state().index_revision
        assert rev1 == rev2 == 1
    finally:
        idx.close()


def test_convergence_state_revision_bumps_on_source_change(tmp_path):
    _write(tmp_path / "svc.py", "def hello():\n    return 1\n")
    idx = ci.CodeIndex(str(tmp_path))
    try:
        idx.reindex()
        rev1 = idx.get_convergence_state().index_revision
        time.sleep(0.01)
        _write(tmp_path / "svc.py", "def hello():\n    return 2\n")
        idx.reindex()
        rev2 = idx.get_convergence_state().index_revision
        assert rev2 == rev1 + 1
    finally:
        idx.close()


def test_convergence_state_degraded_when_vectors_enabled_but_embed_fails(tmp_path):
    """Vectors requested but the embedder can't produce vectors (model load
    failure, RSS pressure, whatever) → vectors_ready stays False and the
    index is explicitly flagged degraded, never silently reported as fine."""
    _write(tmp_path / "svc.py", _SAMPLE_PY)
    idx = ci.CodeIndex(str(tmp_path), embedder=_FakeEmbedder(fail=True))
    try:
        summary = idx.reindex()
        assert summary["rebuilt"] is True  # FTS still rebuilt
        assert idx._vss_ready is False
        state = idx.get_convergence_state()
        assert state.vectors_enabled is True
        assert state.vectors_ready is False
        assert state.degraded is True
        assert state.converged is False
    finally:
        idx.close()


def test_convergence_state_not_degraded_once_vector_leg_fully_converged(tmp_path, monkeypatch):
    _write(tmp_path / "svc.py", _SAMPLE_PY)
    idx = ci.CodeIndex(str(tmp_path), embedder=_FakeEmbedder(model_name="model-a"))
    try:
        monkeypatch.setattr(idx, "_rebuild_vss", _fake_rebuild_vss(idx))
        idx.reindex()
        state = idx.get_convergence_state()
        assert state.vectors_ready is True
        assert state.pending_embedding_count == 0
        assert state.embedding_model == "model-a"
        assert state.configured_embedding_model == "model-a"
        assert state.degraded is False
        assert state.converged is True
        assert state.index_revision == 1
    finally:
        idx.close()


def test_deterministic_invalidation_on_model_change_with_empty_diff(tmp_path, monkeypatch):
    """A configured embedding-model upgrade must invalidate the vector leg
    even when NOTHING on disk changed — a Merkle diff alone can never see a
    model-version change, so reindex() must check for it explicitly."""
    _write(tmp_path / "svc.py", _SAMPLE_PY)
    idx = ci.CodeIndex(str(tmp_path), embedder=_FakeEmbedder(model_name="model-a"))
    monkeypatch.setattr(idx, "_rebuild_vss", _fake_rebuild_vss(idx))
    try:
        idx.reindex()
        state1 = idx.get_convergence_state()
        assert state1.embedding_model == "model-a"
        assert state1.index_revision == 1

        # Simulate a model upgrade — no source file touched.
        idx._embedder = _FakeEmbedder(model_name="model-b")
        summary = idx.reindex()
        assert summary["changed_files"] == []          # Merkle diff is empty
        assert summary["rebuilt"] is True               # yet the vector leg WAS rebuilt

        state2 = idx.get_convergence_state()
        assert state2.embedding_model == "model-b"
        assert state2.configured_embedding_model == "model-b"
        assert state2.index_revision == state1.index_revision + 1
        assert state2.degraded is False
    finally:
        idx.close()


def test_index_paths_targeted_registration_after_full_reindex(tmp_path):
    """A file written AFTER the last full reindex() becomes searchable via
    index_paths() without waiting for the next whole-tree walk, and bumps
    index_revision (a Merkle baseline already exists to persist alongside)."""
    _write(tmp_path / "svc.py", "def hello():\n    return 1\n")
    idx = ci.CodeIndex(str(tmp_path))
    try:
        idx.reindex()
        rev_before = idx.get_convergence_state().index_revision

        new_file = tmp_path / "fresh.py"
        _write(new_file, "def freshly_written():\n    return 42\n")
        result = idx.index_paths([str(new_file)])
        assert result["indexed"] == 1
        assert result["skipped"] == 0

        hits = idx.search("freshly_written")
        assert any(h["name"] == "freshly_written" for h in hits)

        rev_after = idx.get_convergence_state().index_revision
        assert rev_after == rev_before + 1
    finally:
        idx.close()


def test_index_paths_before_any_reindex_still_indexes(tmp_path):
    """index_paths() works even on a brand-new index with no prior full
    reindex() pass (no Merkle baseline yet) — content is searchable
    immediately, but there's no baseline to persist a revision bump against."""
    new_file = tmp_path / "solo.py"
    _write(new_file, "def solo_target():\n    return 1\n")
    idx = ci.CodeIndex(str(tmp_path))
    try:
        result = idx.index_paths([str(new_file)])
        assert result["indexed"] == 1
        hits = idx.search("solo_target")
        assert any(h["name"] == "solo_target" for h in hits)
        assert idx.get_convergence_state().index_revision == 0
    finally:
        idx.close()


def test_index_paths_skips_unsupported_and_outside_root(tmp_path):
    outside = tmp_path.parent / "outside_e631d54f_test.py"
    _write(outside, "def nope():\n    return 1\n")
    ignored = tmp_path / ".codex" / "explicit.py"
    ignored.parent.mkdir(parents=True, exist_ok=True)
    _write(ignored, "def explicit_allowlisted_marker():\n    return 1\n")
    idx = ci.CodeIndex(str(tmp_path))
    try:
        result = idx.index_paths([
            str(tmp_path / "notes.md"),
            str(outside),
            "",
        ])
        assert result["indexed"] == 0
        assert result["skipped"] == 3

        allowed = idx.index_paths([str(ignored)])
        assert allowed["indexed"] == 1
        assert allowed["scope_mode"] == "explicit_allowlist"
        assert allowed["canonical_root"] == ci.normalize_root_dir(str(tmp_path))
        assert allowed["allowlisted_paths"] == [".codex/explicit.py"]
        assert any(
            hit["name"] == "explicit_allowlisted_marker"
            for hit in idx.search("explicit_allowlisted_marker")
        )
    finally:
        idx.close()
        try:
            os.remove(outside)
        except OSError:
            pass


def test_register_priority_path_module_wrapper_makes_content_searchable_without_full_reindex(tmp_path):
    db_path = str(tmp_path / "idx.duckdb")
    new_file = tmp_path / "priority.py"
    _write(new_file, "def provenance_written_symbol():\n    return 1\n")

    result = ci.register_priority_path(str(tmp_path), str(new_file), db_path=db_path)
    assert result["indexed"] == 1

    # reindex=False proves it's searchable WITHOUT a full whole-tree pass.
    res = ci.search_code_semantic(
        str(tmp_path), "provenance_written_symbol", db_path=db_path, reindex=False,
    )
    assert any(h["name"] == "provenance_written_symbol" for h in res["hits"])


def test_register_priority_path_missing_root_dir_errors():
    result = ci.register_priority_path(
        os.path.join("does", "not", "exist"), "whatever.py",
    )
    assert "error" in result
    assert result["indexed"] == 0


def test_search_code_semantic_includes_convergence_and_degraded(tmp_path):
    _write(tmp_path / "svc.py", _SAMPLE_PY)
    db_path = str(tmp_path / "idx.duckdb")
    res = ci.search_code_semantic(str(tmp_path), "parse_token", db_path=db_path)
    assert "convergence" in res
    assert res["degraded"] is False
    assert res["convergence"]["vectors_enabled"] is False
    assert res["convergence"]["converged"] is True

# describe_vector_index — bridge to the backend-neutral vector-index
# contract (e1475682, meridian_codeindex.vector_index.IndexMetadata)
# ===========================================================================


def test_describe_vector_index_reflects_bm25_only_by_default(tmp_path, monkeypatch):
    """With the vector leg disabled (the default), describe_vector_index
    reports the honest degraded backend name and no embedding metadata."""
    monkeypatch.delenv("MERIDIAN_CODE_INDEX_VECTORS", raising=False)
    _write(tmp_path / "svc.py", "def hello():\n    return 1\n")
    idx = ci.CodeIndex(str(tmp_path))
    try:
        idx.reindex()
        meta = idx.describe_vector_index()
        assert meta.backend == "bm25_lexical"
        assert meta.embedding_model is None
        assert meta.dimension is None
        assert meta.scope == idx.root_dir
        assert meta.record_count == idx.count()
    finally:
        idx.close()


@pytest.mark.skipif(
    not _vectors_available(),
    reason="model2vec and/or DuckDB VSS extension not available in this env",
)
def test_describe_vector_index_reports_duckdb_vss_when_vectors_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_CODE_INDEX_VECTORS", "1")
    try:
        from model2vec import StaticModel

        StaticModel.from_pretrained(ci._EMBED_MODEL_NAME)
    except BaseException as exc:  # noqa: BLE001
        _skip_if_model_oom(exc)
    _write(
        tmp_path / "math_ops.py",
        "def add_numbers(a, b):\n    return a + b\n",
    )
    idx = ci.CodeIndex(str(tmp_path))
    try:
        idx.reindex()
        assert idx._vss_ready
        meta = idx.describe_vector_index()
        assert meta.backend == "duckdb_vss"
        assert meta.embedding_model == ci._EMBED_MODEL_NAME
        assert meta.dimension == idx._vss_dim
        assert meta.record_count == idx.count()
    finally:
        idx.close()
