"""Thin MCP stdio server exposing local outputs indexing as tools.

Run with ``uvx --from <path> meridian-outputs-mcp`` (console entry point) or
``python -m meridian_outputs.server``.  Every tool delegates to the fully-local
:mod:`meridian_outputs.outputs_local` module -- NO hosted call is made by any
tool in this package.

This is the wave-1 stopgap for local outputs indexing.  The hosted-aware
smart-routing layer (item 1365e01a) is deliberately out of scope here.

Security notes:
  - Secret files (.env*, *.key, *secret*, etc.) are excluded from the index
    at walk time -- the exclusion filter is in outputs_local.is_secret_path.
  - The local index cache directory is auto-added to .gitignore on first use
    (via outputs_local.ensure_gitignored, called by the search tool when it
    creates a persistent DB).
  - All index writes are serialised through IndexFileLock (threading + optional
    cross-process portalocker) to prevent cache corruption.
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import outputs_local

mcp = FastMCP("meridian-outputs")


@mcp.tool()
def search_outputs(
    outputs_dir: str,
    query: str,
    limit: int = 10,
    include_archival: bool = True,
) -> dict[str, Any]:
    """BM25 full-text search over a local outputs directory tree.

    Indexes CSV, JSON, and NPY files under ``outputs_dir`` (recursive) and
    returns ranked hits.  The index is built and cached locally -- no network
    call is made.  Secret files matching patterns like .env*, *.key, *secret*,
    etc. are excluded from the index.

    The index cache directory (if a persistent db_path is used) is
    automatically added to .gitignore so it is never accidentally committed.

    Args:
      outputs_dir:      Absolute path to the outputs directory to index.
      query:            BM25 search query string.
      limit:            Maximum number of hits to return (default 10).
      include_archival: Include archival-flagged (e.g. ``*_old.csv``) files in
                        results.  They are deprioritised (score halved) but
                        not excluded unless this is False (default True).

    Returns:
      {outputs_dir, query, hits, total_indexed} plus optional {partial, error}.
      Each hit has: path, score, bm25, is_archival, canonical_path, kind,
      generating_script, csv_columns, json_keys, size, mtime, annotations.
    """
    return outputs_local.search_outputs(
        outputs_dir,
        query,
        limit=limit,
        include_archival=include_archival,
    )


@mcp.tool()
def annotate_outputs(
    outputs_dir: str,
    path: str,
    note: str,
    run_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add or update a human annotation for a file or directory in the outputs tree.

    Stores the note in the local outputs index (DuckDB ``annotations`` table).
    Annotations are automatically surfaced alongside search hits -- no extra
    tool call needed.

    ``path`` may be:
      - The ``outputs_dir`` root (Tier 1 -- "what this entire run tree is for").
      - Any sub-path (file or directory) within the tree (Tier 2 -- per-run or
        per-file context such as "PCA on, BFS off, overwritten 5x").

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      path:         File or directory path to annotate (within outputs_dir).
      note:         Human-authored annotation text.
      run_params:   Optional free-form parameter dict (e.g. {"lr": 0.001}).

    Returns:
      The stored annotation as {path, note, run_params, created_at, updated_at,
      source}, or {error: ...} on failure.
    """
    return outputs_local.annotate_outputs(
        outputs_dir, path, note, run_params=run_params,
    )


@mcp.tool()
def classify_outputs(
    paths: list[str],
) -> dict[str, Any]:
    """Classify a list of output file paths as canonical or archival.

    Uses the two-stage classification from outputs_local:
      Stage 1 (cheap): filename heuristic (``*_old.csv``, ``_results.csv`` etc.)
      Stage 2 (SHA-256): byte-identity check against the canonical twin.

    Returns {total, classifications} where each classification has:
      path, is_archival, canonical_path, reason.
    Results are in stable sorted order (sorted by path).
    """
    return outputs_local.classify_outputs(paths)


@mcp.tool()
def resolve_figure_output(
    outputs_dir: str,
    file_path: str,
) -> dict[str, Any] | None:
    """Exact-path lookup: is this figure already indexed as an output?

    Given a document figure's ``file_path``, returns the outputs_index row for
    the SAME file if it is present in the local index -- or None if no match.
    Matching is path-normalised (handles back-slashes/forward-slashes, case
    differences on Windows, relative vs absolute).

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      file_path:    The figure's file path to resolve.

    Returns:
      {path, generating_script, is_archival, canonical_path, sha256, kind,
      size, mtime, csv_columns, json_keys} or null if no match.
    """
    return outputs_local.resolve_figure_output(outputs_dir, file_path)


@mcp.tool()
def npy_metadata(path: str) -> dict[str, Any]:
    """Read metadata from a .npy file WITHOUT loading the full array.

    Uses numpy.load(mmap_mode='r') to read only the header, never pulling the
    full array into memory.  Safe on large arrays.

    Args:
      path:  Absolute path to the .npy file.

    Returns:
      {path, shape, dtype, size_bytes, modified_at} plus optional {error}.
    """
    return outputs_local.npy_metadata(path).to_dict()


@mcp.tool()
def file_fingerprint(path: str) -> dict[str, Any]:
    """Compute a cheap content-derived fingerprint for one output file.

    For CSV: returns column names (header row) + generating_script hint.
    For JSON: returns top-level keys + generating_script hint.
    For NPY and other binaries: metadata-only (no content read).

    Useful for "does this output already exist / has it changed?" checks
    without re-running the full search index rebuild.

    Args:
      path:  Absolute path to the file.

    Returns:
      {path, kind, csv_columns, json_keys, generating_script}.
    """
    return outputs_local.file_fingerprint(path).to_dict()


def main() -> None:
    """Console entry point (``uvx --from <path> meridian-outputs-mcp``)."""
    mcp.run()


if __name__ == "__main__":
    main()
