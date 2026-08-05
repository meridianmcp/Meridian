"""meridian-codeindex — standalone local BM25 code index.

Extracted from Meridian (2b2433ca): a self-contained, standalone-installable
code search index (``pip install -e ./extensions/meridian-codeindex``) that
lives inside the Meridian monorepo but has **no dependency on the rest of
Meridian** — no Serena, no codebase-memory-mcp, no LSP, no external binary.
Tree-sitter/ast semantic chunking + a Merkle-incremental reindex + DuckDB FTS
(Okapi BM25) hybrid search, runnable as a library or via the
``meridian-codeindex`` CLI.

    from meridian_codeindex import CodeIndex, search_code_semantic

    result = search_code_semantic("/path/to/repo", "parse the auth token")
    for hit in result["hits"]:
        print(hit["path"], hit["line_start"], hit["name"])

Or from the command line::

    meridian-codeindex /path/to/repo "parse the auth token"

Meridian itself is just ONE caller of this package (see
``meridian/code_index.py``, a thin compatibility shim) — the same relationship
Meridian has with codebase-memory-mcp: a real independent tool it happens to
call, not a Meridian feature wearing a native-sounding name.
"""
from __future__ import annotations

from .code_index import (
    CodeChunk,
    CodeIndex,
    MerkleDiff,
    MerkleNode,
    MerkleTree,
    build_merkle_tree,
    chunk_file,
    detect_language,
    get_code_index,
    is_indexable,
    normalize_root_dir,
    reindex_at_checkpoint,
    search_code_semantic,
)
from .vector_index import (
    BenchmarkResult,
    DuckDBVSSBackend,
    IndexMetadata,
    LexicalBM25Backend,
    PgVectorBackend,
    VectorBackendUnavailable,
    VectorIndexBackend,
    VectorMatch,
    VectorRecord,
    compare_candidates,
    content_fingerprint,
    run_benchmark,
    run_lexical_benchmark,
    should_enable_pgvector,
)

__version__ = "0.1.0"

__all__ = [
    "CodeChunk",
    "CodeIndex",
    "MerkleDiff",
    "MerkleNode",
    "MerkleTree",
    "build_merkle_tree",
    "chunk_file",
    "detect_language",
    "get_code_index",
    "is_indexable",
    "normalize_root_dir",
    "reindex_at_checkpoint",
    "search_code_semantic",
    # e1475682 — backend-neutral vector-index contract
    "BenchmarkResult",
    "DuckDBVSSBackend",
    "IndexMetadata",
    "LexicalBM25Backend",
    "PgVectorBackend",
    "VectorBackendUnavailable",
    "VectorIndexBackend",
    "VectorMatch",
    "VectorRecord",
    "compare_candidates",
    "content_fingerprint",
    "run_benchmark",
    "run_lexical_benchmark",
    "should_enable_pgvector",
]
