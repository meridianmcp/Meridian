"""Thin MCP stdio server exposing local outputs indexing as tools.

Run with ``uvx --from <path> meridian-outputs-mcp`` (console entry point) or
``python -m meridian_outputs.server``.  Every tool delegates to a fully-local
module in this package -- NO hosted call is made by any tool here.  Most
tools delegate straight to :mod:`meridian_outputs.outputs_local`; a handful
(``search_outputs``, ``classify_outputs``, ``resolve_figure_output``,
``find_outputs_by_source``, ``tag_output``, ``check_staleness``,
``find_stale_by_script``, ``script_content_hash``) delegate instead to the
additive sibling modules built alongside it (:mod:`meridian_outputs.search`,
:mod:`meridian_outputs.classify`, :mod:`meridian_outputs.provenance`,
:mod:`meridian_outputs.fingerprint`) that each layer a drop-in-superset fix
on top of ``outputs_local``'s public API without touching it directly (item
a26ad8da wired these in; see each sibling module's docstring for the gap it
closes).

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

from . import annotate, classify, fingerprint, outputs_local, provenance, search

mcp = FastMCP("meridian-outputs")


@mcp.tool()
def search_outputs(
    outputs_dir: str,
    query: str,
    limit: int = 10,
    include_archival: bool = True,
    max_seconds: float | None = outputs_local.DEFAULT_REBUILD_BUDGET_SECONDS,
) -> dict[str, Any]:
    """BM25 full-text search over a local outputs directory tree, with a
    literal-filename-match boost (item c6236ef4).

    IMPORTANT -- a 0-hit result does NOT mean the file/term doesn't exist.
    On a cold or large ``outputs_dir``, indexing this tool depends on can
    take longer than one call's budget: check ``partial``, ``fts_pending``,
    ``pending_stale_count``, and ``db_write_error`` on the response before
    concluding "not found" -- any of them present means the index is still
    catching up (or a write failed and will retry), and you should
    re-invoke this same tool with the same ``outputs_dir``/``query`` to get
    a progressively more complete answer. A ``zero_hits_warning`` string is
    also included on exactly this "0 hits + still indexing" response shape
    as an unmissable flag -- if you see it, re-run rather than report a miss.

    Indexes CSV, JSON, and NPY files under ``outputs_dir`` (recursive) and
    returns ranked hits.  The index is built and cached locally -- no network
    call is made.  Secret files matching patterns like .env*, *.key, *secret*,
    etc. are excluded from the index.

    The index cache directory (if a persistent db_path is used) is
    automatically added to .gitignore so it is never accidentally committed.

    Delegates to :func:`outputs_local.search_outputs` for all indexing and
    BM25 scoring, then re-ranks so any hit whose basename literally contains
    the (separator-normalized) query string precedes hits that only matched
    via loose BM25 token overlap -- fixes a real case where a longer,
    decoy-suffixed archival file BM25-outranked its exact-match canonical
    twin. Purely additive: same hits, same count, same every-other-field,
    plus one new ``literal_match: bool`` field per hit.

    Args:
      outputs_dir:      Absolute path to the outputs directory to index.
      query:            BM25 search query string.
      limit:            Maximum number of hits to return (default 10).
      include_archival: Include archival-flagged (e.g. ``*_old.csv``) files in
                        results.  They are deprioritised (score halved) but
                        not excluded unless this is False (default True).
      max_seconds:      3535b9ad -- the "indexing slider": how long a single
                        call may spend on rebuild()'s incremental indexing
                        before returning, on a cold or large tree. Omit for
                        the library default (DEFAULT_REBUILD_BUDGET_SECONDS,
                        130s). Lower it for a faster first response on a huge
                        tree (partial=True signals more indexing remains --
                        call again to continue); raise it to converge in
                        fewer calls on a tree too large for the default budget.

    Returns:
      {outputs_dir, query, hits, total_indexed} plus optional {partial,
      pending_stale_count, fts_pending, tantivy_lock_warning, db_write_error,
      zero_hits_warning, error}. ``pending_stale_count`` (only present when
      ``partial`` is True) is the number of confirmed-stale files still
      queued for analysis+write -- distinguishes a zero-hit result on a
      mid-pass index (more indexing queued) from a genuine miss on a
      fully-converged index (81a0b23d). ``zero_hits_warning`` is present
      whenever ``hits`` is empty AND the index isn't fully converged
      (partial/fts_pending/db_write_error set) -- treat its presence as "do
      not conclude not-found yet, re-invoke this tool instead."
      Each hit has: path, score, bm25, is_archival, canonical_path, kind,
      generating_script, csv_columns, json_keys, size, mtime, annotations,
      literal_match.
    """
    return search.search_outputs(
        outputs_dir,
        query,
        limit=limit,
        include_archival=include_archival,
        max_seconds=max_seconds,
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
def record_provenance(
    outputs_dir: str,
    path: str,
    generating_script: str | None = None,
    params: dict[str, Any] | None = None,
    sprint_item_id: str | None = None,
    decision_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Attach lightweight reproducibility metadata to one output file.

    Distinct from ``annotate_outputs`` (free-form human notes): this records
    the machine-oriented facts needed to reconstruct "what produced this
    file, with what parameters, when, and under which sprint-item/decision"
    without reopening the output itself. Stored as an append-only JSONL
    sidecar log at ``<outputs_dir>/.meridian-outputs-cache/provenance.jsonl``
    -- fully local, no hosted call, no DB engine. Calling this again for the
    same path supersedes the previous record (newest wins on read).

    Args:
      outputs_dir:        Absolute path to the outputs directory.
      path:                The output file this record describes.
      generating_script:  Path/name of the script that produced `path`. If
                          omitted, falls back to a best-effort inference via
                          ``file_fingerprint``.
      params:              Key parameters used for this run, e.g.
                          {"radius_scale": 4.0, "use_pca": False}.
      sprint_item_id:      Optional linked Meridian sprint-item id.
      decision_id:         Optional linked Meridian decision id.
      note:                Optional short note (kept separate from
                          ``annotate_outputs``'s longer free-form notes).

    Returns:
      The stored record as {path, generating_script, params, sprint_item_id,
      decision_id, note, recorded_at, recorded_at_iso}, or {error: ...}.
    """
    return annotate.record_provenance(
        outputs_dir,
        path,
        generating_script=generating_script,
        params=params,
        sprint_item_id=sprint_item_id,
        decision_id=decision_id,
        note=note,
    )


@mcp.tool()
def get_provenance(outputs_dir: str, path: str) -> dict[str, Any] | None:
    """Look up the most recent reproducibility record for one output file.

    Queryable WITHOUT opening/parsing the output file itself -- reads only
    the lightweight JSONL sidecar log written by ``record_provenance``.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      path:         The output file to look up.

    Returns:
      {path, generating_script, params, sprint_item_id, decision_id, note,
      recorded_at, recorded_at_iso}, or None if nothing has been recorded for
      this path under this outputs_dir.
    """
    return annotate.get_provenance(outputs_dir, path)


@mcp.tool()
def list_provenance(outputs_dir: str) -> list[dict[str, Any]]:
    """List the latest reproducibility record for every path recorded under
    an outputs directory (sorted by path, deterministic).

    Args:
      outputs_dir:  Absolute path to the outputs directory.

    Returns:
      A list of record dicts (same shape as ``get_provenance``'s return
      value); ``[]`` if nothing has been recorded yet.
    """
    return annotate.list_provenance(outputs_dir)


@mcp.tool()
def classify_outputs(
    paths: list[str],
) -> dict[str, Any]:
    """Classify a list of output file paths as canonical or archival (item
    2820ab1f's broader superset).

    Delegates to outputs_local's two-stage classification for everything it
    already resolves:
      Stage 1 (cheap): filename heuristic (``*_old.csv``, ``_results.csv`` etc.)
      Stage 2 (SHA-256): byte-identity check against the canonical twin.

    ...then adds a broader stage-1b naming heuristic (``_backup``/``_bak``/
    ``_deprecated``/``_mislabeled``/``_wip``/``_copy``/``_stale``/``_archived``
    suffixes, plus a whole extra suffix appended after the real extension like
    ``.bak_41img_mislabeled`` or a trailing ``~``) for paths outputs_local's
    own ``_old``/``_old_N`` convention doesn't recognise, and surfaces
    ``size``/``mtime``/``mtime_iso`` directly on every record.

    Returns {total, classifications} where each classification has:
      path, is_archival, canonical_path, reason, size, mtime, mtime_iso.
    Results are in stable sorted order (sorted by path).
    """
    return classify.classify_outputs(paths)


@mcp.tool()
def resolve_figure_output(
    outputs_dir: str,
    file_path: str,
    fuzzy_limit: int = 25,
) -> dict[str, Any] | None:
    """Forward resolution: a document figure's ``file_path`` -> its
    generating source (item e422de44's relocation-tolerant superset).

    Two tiers, tried in order:
      1. Exact-path (legacy contract, unchanged): the figure file IS itself
         an indexed output at that same path. Matching is path-normalised
         (handles back-slashes/forward-slashes, case differences on Windows,
         relative vs absolute).
      2. Basename fallback: when the exact path misses -- the figure was
         relocated/copied/renamed relative to when it was indexed -- searches
         the outputs index for files sharing the same basename and returns
         the best-scoring candidate. Catches a figure whose docx-embedded
         copy no longer lives where it was generated, which the old
         exact-path-only lookup silently missed (returned None with no
         further signal).

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      file_path:    The figure's file path to resolve.
      fuzzy_limit:  Max search hits considered for the basename tier
                    (default 25).

    Returns:
      ``None`` only when NEITHER tier finds anything. Otherwise the resolved
      row (path, generating_script, is_archival, canonical_path, sha256*,
      kind, size, mtime, csv_columns, json_keys -- *sha256 only present on an
      exact match) plus ``match_type`` (``"exact"`` or ``"basename"``),
      ``queried_path``, and (basename tier only) ``candidate_count`` -- more
      than 1 means the match is ambiguous and ``generating_script`` should be
      treated as a best guess, not a certainty.
    """
    return provenance.resolve_figure_output(
        outputs_dir, file_path, fuzzy_limit=fuzzy_limit,
    )


@mcp.tool()
def find_outputs_by_source(
    outputs_dir: str,
    source_path: str,
    limit: int = 25,
    search_limit: int = 200,
) -> dict[str, Any]:
    """Reverse resolution: a script/data ``source_path`` -> the outputs it
    produced (item e422de44's new direction -- did not exist before this).

    Given the generating script or data file, scans the outputs index for
    rows whose recorded ``generating_script`` traces back to it (exact-string
    or basename match) -- i.e. "what did this thing produce?". This is the
    direction needed to catch a docx figure quietly citing STALE data: walk
    the source's outputs forward, newest first, and compare against what the
    docx actually shows.

    Args:
      outputs_dir:   Absolute path to the outputs directory.
      source_path:   The script or data file to trace forward from.
      limit:         Max number of matched outputs to return (default 25).
      search_limit:  How many underlying search hits to scan before filtering
                    (default 200; generous, since only a subset will match).

    Returns:
      {source_path, outputs, total} where each output row has the same
      fields as a search hit (path, generating_script, is_archival,
      canonical_path, kind, size, mtime, csv_columns, json_keys), sorted
      newest-first by mtime. ``total`` is the full match count before
      ``limit`` truncation. ``outputs`` is empty (not an error) when nothing
      in the tree cites this source.
    """
    return provenance.find_outputs_by_source(
        outputs_dir, source_path, limit=limit, search_limit=search_limit,
    )


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


@mcp.tool()
def search_logs(
    logs_dir: str,
    query: str,
    limit: int = 20,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Lightweight, disposable regex search over a local log directory tree.

    Unlike ``search_outputs``, this builds NO persistent index -- logs have no
    guaranteed structure (rotated files, plain text, JSON-lines, syslog, mixed
    formats), so every call re-scans the tree fresh instead of maintaining a
    cache that would drift stale on the next rotation.

    Tier 0 (always on): a sub-second ``rg`` (ripgrep) subprocess scan;
    transparently falls back to an equivalent pure-Python regex scan when
    ``rg`` isn't on PATH. Secret-named files (.env*, *.key, *secret*,
    *credential*, etc.) are excluded, same as outputs indexing.

    Tier 1 (opportunistic, layered on the same scan, not a second pass): each
    matched line is cheaply sniffed for a timestamp and/or a JSON object.
    Matches with a sniffed signal are ranked above plain ones (by severity,
    then recency); anything unsniffable free-falls back to Tier 0's own scan
    order at no extra cost.

    Args:
      logs_dir:         Absolute path to the log directory to search.
      query:            Ripgrep-flavoured regex (case-insensitive); degrades
                        to Python `re`, then a literal match, in the fallback
                        path.
      limit:            Maximum number of hits to return (default 20).
      timeout_seconds:  Wall-clock scan budget in seconds (default 5.0).

    Returns:
      {logs_dir, query, hits, total_matched, engine} plus optional {error}.
      Each hit has: path, line_number, line, tier, timestamp, timestamp_epoch,
      level, json_fields.
    """
    return outputs_local.search_logs(
        logs_dir, query, limit=limit, timeout_seconds=timeout_seconds,
    )


@mcp.tool()
def tag_output(
    output_path: str,
    outputs_dir: str,
    script_path: str | None = None,
    search_root: str | None = None,
) -> dict[str, Any]:
    """Fingerprint one output file and record it in a persistent ledger,
    stamped with its generating script's content hash AT THIS MOMENT (item
    7518bfcd).

    Reuses ``file_fingerprint``'s cheap per-file signature (csv_columns /
    json_keys / generating_script) and adds one more dimension on top: the
    SHA-256 of the generating script's own on-disk content, so two runs of
    the same script (one buggy, one fixed) that happen to produce
    byte-identical-looking outputs can still be told apart later via
    ``check_staleness``/``find_stale_by_script``.

    Stored in ``<outputs_dir>/.meridian-outputs-cache/fingerprint_ledger.json``
    -- fully local, no hosted call. Calling this again for the same
    ``output_path`` supersedes the previous ledger entry.

    Args:
      output_path:  Absolute path to the output file to tag.
      outputs_dir:  Root outputs directory (the ledger lives under its
                    ``.meridian-outputs-cache/`` subdirectory).
      script_path:  Explicit path to the generating script, if already known.
                    Takes priority over the content-derived
                    ``generating_script`` hint.
      search_root:  Optional directory to resolve a bare script-name hint
                    against (e.g. a repo root) when ``script_path`` isn't
                    given explicitly.

    Returns:
      {path, kind, csv_columns, json_keys, generating_script, script_path,
      script_hash, tagged_at} -- the ledger entry that was written.
    """
    return fingerprint.tag_output(
        output_path, outputs_dir, script_path=script_path, search_root=search_root,
    ).to_dict()


@mcp.tool()
def check_staleness(outputs_dir: str) -> list[dict[str, Any]]:
    """Re-hash every generating script referenced in the ``tag_output``
    ledger and report which previously-tagged outputs are now stale (item
    7518bfcd).

    An output is flagged stale when its generating script's CURRENT content
    hash differs from the hash recorded at tag time -- the script has been
    edited (bug fix or otherwise) since that output was produced, so the
    output may still reflect the OLD script behaviour even though the output
    file itself never changed and looks perfectly valid/cached. Also flagged
    stale if the generating script can no longer be read at all
    (deleted/moved).

    Args:
      outputs_dir:  Absolute path to the outputs directory.

    Returns:
      A list of {path, script_path, tagged_script_hash, current_script_hash,
      is_stale, reason} dicts, one per ledger entry, in stable sorted order
      (by output path). ``[]`` if nothing has ever been tagged.
    """
    return [r.to_dict() for r in fingerprint.check_staleness(outputs_dir)]


@mcp.tool()
def find_stale_by_script(outputs_dir: str, script_path: str) -> list[str]:
    """All ledger-tagged output paths produced by ``script_path`` in a
    content state OTHER than its current on-disk content (item 7518bfcd).

    The direct tool for the motivating scenario: "``script_path`` was just
    found buggy and fixed -- which previously-tagged outputs did the buggy
    (pre-fix) version produce?" Every ledger entry tagged against this script
    whose recorded hash doesn't match the script's CURRENT hash was
    generated by a script content-state that no longer exists on disk --
    i.e. potentially the just-fixed bug.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      script_path:  The generating script to check tagged outputs against.

    Returns:
      A list of output paths (stable sorted order); ``[]`` if none are stale
      relative to this script's current content.
    """
    return fingerprint.find_stale_by_script(outputs_dir, script_path)


@mcp.tool()
def script_content_hash(script_path: str) -> str | None:
    """SHA-256 of a generating script's current on-disk content (item
    7518bfcd).

    The same primitive ``tag_output``/``check_staleness``/
    ``find_stale_by_script`` use internally, exposed directly for a quick
    "what would tagging this script right now record?" check without
    needing an outputs_dir or an actual output file at hand.

    Args:
      script_path:  Absolute (or resolvable) path to the script file.

    Returns:
      The hex SHA-256 digest, or None if the file can't be read (missing,
      moved, permissions) -- never raises.
    """
    return fingerprint.script_content_hash(script_path)


def main() -> None:
    """Console entry point (``uvx --from <path> meridian-outputs-mcp``)."""
    mcp.run()


if __name__ == "__main__":
    main()
