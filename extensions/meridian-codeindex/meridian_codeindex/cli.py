"""Command-line entry point for the standalone ``meridian-codeindex`` package.

    meridian-codeindex <repo_path> <query>

Runs a one-shot BM25 (+ optional local-vector) search over a source tree and
prints ranked hits — the exact same ``search_code_semantic`` surface Meridian
calls internally, runnable standalone with zero Meridian involvement. Useful
as a fallback layer when Serena and/or codebase-memory-mcp are unavailable or
misbehaving, and equally useful on its own as a plain grep-replacement.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Sequence

from .code_index import search_code_semantic


def _format_hit(rank: int, hit: dict[str, Any]) -> str:
    path = hit.get("path", "?")
    line_start = hit.get("line_start", "?")
    line_end = hit.get("line_end", "?")
    kind = hit.get("kind", "?")
    name = hit.get("name", "?")
    score = hit.get("score")
    score_str = f"{score:.4f}" if isinstance(score, (int, float)) else "?"
    return f"{rank:>3}. [{score_str}] {path}:{line_start}-{line_end}  {kind} {name}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meridian-codeindex",
        description=(
            "Local BM25 (+ optional vector) code search over a source tree — "
            "tree-sitter/ast semantic chunking, Merkle-incremental reindex, "
            "DuckDB FTS. Zero external services, zero cloud round-trip."
        ),
    )
    parser.add_argument(
        "repo_path",
        help="Path to the repository / source tree root to index and search.",
    )
    parser.add_argument("query", help="Search query.")
    parser.add_argument(
        "--limit", type=int, default=10,
        help="Maximum number of ranked hits to print (default: 10).",
    )
    parser.add_argument(
        "--kind", default=None,
        help="Filter to one chunk kind (function/class/method/module/...).",
    )
    parser.add_argument(
        "--db-path", default=":memory:", dest="db_path",
        help=(
            "DuckDB sidecar path to persist the chunk store + Merkle tree "
            "across runs (default: in-memory, no persistence)."
        ),
    )
    parser.add_argument(
        "--no-reindex", action="store_true", dest="no_reindex",
        help="Skip the incremental reindex pass; search the existing sidecar as-is.",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv``, run the search, print ranked hits. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    result = search_code_semantic(
        args.repo_path,
        args.query,
        limit=args.limit,
        kind=args.kind,
        db_path=args.db_path,
        reindex=not args.no_reindex,
    )
    if result.get("error"):
        print(f"error: {result['error']}", file=sys.stderr)
        return 1
    hits = result.get("hits") or []
    print(
        f"query: {result.get('query')!r}  root_dir: {result.get('root_dir')}  "
        f"indexed: {result.get('total_indexed')} chunks"
    )
    if not hits:
        print("no hits")
        return 0
    for i, hit in enumerate(hits, start=1):
        print(_format_hit(i, hit))
    return 0


def main() -> None:
    """Console-script entry point (``project.scripts`` target)."""
    raise SystemExit(run())


if __name__ == "__main__":
    main()
