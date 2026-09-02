"""Thin MCP stdio server exposing local outputs indexing as tools.

Run with ``uvx --from <path> meridian-outputs-mcp`` (console entry point) or
``python -m meridian_outputs.server``.  Every tool delegates to a fully-local
module in this package -- NO hosted call is made by any tool here.  Most
tools delegate straight to :mod:`meridian_outputs.outputs_local`; a handful
(``search_outputs``, ``classify_outputs``, ``resolve_figure_output``,
``find_outputs_by_source``, ``tag_output``, ``check_staleness``,
``find_stale_by_script``, ``script_content_hash``, ``get_provenance_status``,
``get_provenance_status_envelope``, ``serialize_provenance_envelope``,
``parse_provenance_envelope``)
delegate instead to the additive sibling modules built alongside it
(:mod:`meridian_outputs.search`, :mod:`meridian_outputs.classify`,
:mod:`meridian_outputs.provenance`, :mod:`meridian_outputs.fingerprint`,
:mod:`meridian_outputs.provenance_status`, :mod:`meridian_outputs.research_evidence`)
that each layer a drop-in-superset fix on top of ``outputs_local``'s public
API without touching it directly (item a26ad8da wired the first batch of
these in; item bd5b8d79 added ``provenance_status``; item 0ea8fd3c added
``research_evidence`` plus the ``get_provenance_status_envelope``/
``serialize_provenance_envelope``/``parse_provenance_envelope`` bridge; see
each sibling module's docstring for the gap it closes).

Item e1c979e3 added the artifact registry that research_evidence.py's own
docstring (item 0ea8fd3c) had explicitly flagged as NOT YET BUILT: a
durable, relocation-safe artifact identity store, exposed here as
``register_artifact``/``resolve_artifact``/``verify_artifact_hash``/
``bind_artifact_source_edge``/``get_artifact_sources``/
``get_source_artifacts``/``list_registered_artifacts``/
``reconcile_legacy_artifact_outputs`` -- see
:mod:`meridian_outputs.artifact_registry` for the full contract. Output-
semantic validation (``validate_output_semantics``) remains a separate,
not-yet-built capability with its own sprint item; nothing below should be
read as a promise that it exists.

Item a4cb12bf added ``inspect_local_file``, a single bounded local
inspect/read router for ONE file (XML/JSON/CSV/XLSX/DOCX) that dispatches to
the ``meridian-file-inspection`` and ``meridian-docs`` sibling packages over
real (subprocess-spawned) MCP client sessions rather than importing them
directly -- see :mod:`meridian_outputs.file_inspector` for the full routing
table, the two-tier launch strategy, and the ``local_only``/``unavailable``
convention it introduces. This tool never touches ``search_outputs``/
``register_output_paths``, which remain exactly as they were.

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

from . import (
    annotate,
    artifact_registry,
    classify,
    file_inspector,
    fingerprint,
    outputs_local,
    provenance,
    provenance_status,
    research_evidence,
    run_manifest,
    search,
)

mcp = FastMCP("meridian-outputs")


@mcp.tool()
def search_outputs(
    outputs_dir: str,
    query: str,
    limit: int = 10,
    include_archival: bool = True,
    max_seconds: float | None = outputs_local.DEFAULT_REBUILD_BUDGET_SECONDS,
    subtree: str | None = None,
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
      subtree:          6af1518d -- optional sub-path of ``outputs_dir`` to
                        scope indexing/searching to, WITHOUT requiring a
                        full re-walk of the root. Uses a separate,
                        independently-converging index for the subtree,
                        seeded from a slice of the root's own cached index
                        when available (so files the root already indexed
                        aren't re-hashed). The response's convergence/
                        partial fields then describe the SUBTREE's own
                        convergence, not the whole root's -- fixes the real
                        inconsistency this item was opened to close: root
                        vs. narrow-subdirectory searches giving different
                        zero-hit answers with no way to tell why.

    Returns:
      {outputs_dir, query, hits, total_indexed} plus optional {subtree,
      partial, pending_stale_count, fts_pending, tantivy_lock_warning,
      index_lock_warning, db_write_error, zero_hits_warning, error,
      convergence}.
      ``pending_stale_count`` (only present when ``partial`` is True) is the
      number of confirmed-stale files still queued for analysis+write --
      distinguishes a zero-hit result on a mid-pass index (more indexing
      queued) from a genuine miss on a fully-converged index (81a0b23d).
      ``index_lock_warning`` (a52216e2) is present whenever the index's
      single-writer lock itself could not be acquired this call (real
      cross-process contention against an ACTIVE owner, or an unexpected
      acquisition failure) -- mirrors the ``tantivy_lock_warning`` precedent:
      the call still degraded gracefully (never raised), but got fewer/no
      hits because a write it needed didn't happen. Safe to re-invoke.
      ``zero_hits_warning`` is present whenever ``hits`` is empty AND the
      index isn't fully converged (partial/fts_pending/db_write_error set,
      OR ``convergence.converged`` is False) -- treat its presence as "do
      not conclude not-found yet, re-invoke this tool instead."
      ``convergence`` (6af1518d) is the explicit, structured convergence
      snapshot: {outputs_dir, subtree, converged, walk_complete,
      scan_boundary, pending_count, indexed_count, expected_count,
      last_error, fts_pending, partial} -- the single authoritative object
      the other ad hoc fields are derived from.
      ``discovery`` (b85394bd) is a planner/diagnostic dict surfacing this
      call's own phase timings and the resolved discovery-vs-analysis
      capacity split: {walk_seconds, discovered_this_call, discovered_total,
      walk_complete, pending_stale_count, walk_batch_limit,
      analysis_batch_limit, analysis_batch_source, analysis_backlog_deferred,
      analysis_seconds, classification_seconds, write_seconds,
      fts_seconds, rebuild_seconds, ...}. ``walk_batch_limit`` is the raw
      filesystem-discovery cap this call used (independent of analysis
      capacity, effectively unbounded by default); ``analysis_batch_limit``
      is the memory-/commit-pressure-adaptive cap on how many stale files
      Phase 1/2 took on THIS call (never the whole backlog at once);
      ``analysis_batch_source`` is ``"adaptive"`` or ``"override"`` (an
      explicit ``max_batch``/``MERIDIAN_OUTPUTS_MAX_BATCH`` was set);
      ``analysis_backlog_deferred`` is how many confirmed-stale files were
      left queued past this call's own analysis intake cap (0 when the
      whole backlog fit). A non-zero ``analysis_backlog_deferred`` also
      implies ``partial``/``pending_stale_count`` above, for the same
      "re-invoke rather than conclude not-found" reason.

      ``index_lock`` (a52216e2) is a read-only snapshot of who currently
      holds this index's write lock -- {held, pid, hostname, session_id,
      started_at, heartbeat_at, age_seconds, lock_mode, pid_alive, is_stale,
      stale_reason} -- or ``None`` for an in-memory index. Never acquired
      just by calling this tool, and never used to terminate anything: a
      ``is_stale=True`` reading only means a leftover lock FILE from a
      crashed owner is safe to reclaim, not that any process should be
      killed.
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
        subtree=subtree,
    )


@mcp.tool()
def register_output_paths(
    outputs_dir: str,
    paths: list[str],
) -> dict[str, Any]:
    """Directly register a small, EXPLICIT list of exactly-known output
    file paths so they're searchable right away (item b85394bd), instead of
    waiting for the ambient full-root walk to discover them on its own
    schedule -- the direct MCP-level counterpart to
    ``outputs_local.index_paths``/``register_priority_path``.

    The natural caller: a build/pipeline step (or an agent) that just
    produced or otherwise already knows a specific set of output files and
    wants them indexed NOW, without triggering (or waiting on) a full
    ``search_outputs`` rebuild pass over ``outputs_dir``. Cost is bounded by
    ``len(paths)``, not the size of ``outputs_dir`` -- safe to call
    synchronously even against a huge, still-converging tree.

    A path that doesn't exist on disk YET is queued (``queued`` in the
    response) rather than treated as an error -- a later call to this tool,
    ``search_outputs``, or the ambient walk reaching it will pick it up
    automatically once it's actually written.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      paths:        Exact output file paths to register/index now.

    Returns:
      {registered, indexed, queued, paths} on success (``indexed`` -- rows
      written this call; ``paths`` -- the paths actually indexed), or
      {registered: False, reason: ...} if ``outputs_dir``/``paths`` are
      missing. Best effort, never raises.
    """
    return outputs_local.register_output_paths(outputs_dir, paths)


@mcp.tool()
def get_convergence_state(
    outputs_dir: str, subtree: str | None = None,
) -> dict[str, Any]:
    """Explicit convergence-state snapshot for a local outputs index (item
    6af1518d requirement 1) -- read-only, does NOT trigger any indexing.

    Answers, without guessing from a search result's shape: how far has the
    index walk gotten (``scan_boundary``), how much work is still queued
    (``pending_count``), did the walk hit anything it couldn't read
    (``last_error``), and how does the indexed count compare to the best-
    known expected total (``indexed_count``/``expected_count``)? Call this
    BEFORE trusting a zero-hit ``search_outputs`` result as a genuine miss,
    or poll it while a large tree is still converging across multiple
    ``search_outputs`` calls.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      subtree:      Optional sub-path of ``outputs_dir``. When given, scopes
                    the answer to that subtree specifically (has THIS
                    sub-path been fully covered by the walk so far), using
                    the SAME cached root index -- does not spin up a
                    separate subtree index (see ``search_outputs``'s
                    ``subtree`` param for that).

    Returns:
      {outputs_dir, subtree, converged, walk_complete, scan_boundary,
      pending_count, indexed_count, expected_count, last_error, fts_pending,
      partial, index_lock, never_walked}, or {error: ...} if ``outputs_dir``
      doesn't exist.
      ``index_lock`` (a52216e2): a read-only snapshot of this index's
      single-writer lock/lease -- {held, pid, hostname, session_id,
      started_at, heartbeat_at, age_seconds, lock_mode, pid_alive, is_stale,
      stale_reason} -- or ``None`` for an in-memory index. Distinguishes an
      ACTIVE owner (a live, recently-heartbeating writer -- never stolen,
      never touched) from a STALE one (a leftover lock file from a crashed
      owner, safe to reclaim on the next acquire). This call never acquires
      the lock itself and never disturbs whatever process currently holds it.
      ``never_walked`` (3f758063): ``True`` only when this index has ZERO
      recorded evidence a walk has ever touched ``outputs_dir`` -- no scan
      boundary, no indexed rows, no pending backlog, no confirmed expected
      count. ``converged`` is always ``False`` whenever this is ``True``:
      fail closed rather than reporting a genuinely never-examined tree as
      "confirmed converged, nothing here" (the exact hosted/local mismatch
      this item exists to close). Never ``True`` for an in-progress walk, a
      durably-persisted interrupted walk, or a real completed pass -- even
      one that confirmed a genuinely empty directory.
    """
    return outputs_local.get_convergence_state(outputs_dir, subtree=subtree)


@mcp.tool()
def inspect_local_file(
    path: str,
    operation: str = "shape",
    format: str = "auto",
    allowed_root: str | None = None,
    allow_symlinks: bool = False,
    selector: str | None = None,
    max_bytes: int = file_inspector.DEFAULT_MAX_BYTES,
    timeout_seconds: float = file_inspector.DEFAULT_TIMEOUT_SECONDS,
    preview_chars: int | None = None,
    max_sample_rows: int | None = None,
) -> dict[str, Any]:
    """One bounded local inspect/read workflow for a single file -- XML,
    JSON, CSV, XLSX, or DOCX -- without a tunnel and without a duplicate
    parser (item a4cb12bf).

    Routes ``path`` to whichever existing capability already understands
    its format -- ``extensions/meridian-file-inspection``'s ``inspect_file``
    (raw XML, generic JSON) or ``inspect_tabular_file`` (CSV, XLSX,
    row-shaped JSON), or ``extensions/meridian-docs``'s ``document_outline``/
    ``read_document_snapshot`` (DOCX/DOTX/DOCM/DOTM) -- each spawned as its
    own short-lived local MCP stdio server (never a direct cross-package
    import; see ``file_inspector`` module docstring for why) and normalizes
    every answer into ONE canonical envelope. ``search_outputs``/
    ``register_output_paths`` are unaffected by this tool -- output-tree
    indexing/search stays exactly as it was.

    This tool never makes a network or tunnel call of its own -- every
    response carries ``"local_only": true``. When the required sibling
    process cannot even be reached (missing package directory, no
    compatible Python/``uvx`` on PATH, a crash before it can answer, or the
    whole attempt exceeding its wall-clock budget), ``state`` is
    ``"unavailable"`` (distinct from ``"failed"``, which means a sibling DID
    run and reported a real parse/policy error) and ``errors`` carries a
    stable ``"unavailable"`` code -- never a raised exception, never a hang.

    Args:
      path:              Path to the single file to inspect.
      operation:         ``"metadata"`` (identity/size/hash/state only, no
                          content), ``"shape"`` (default -- structure
                          without content previews/samples), or
                          ``"preview"`` (the full underlying bounded
                          response, content included). For DOCX, metadata/
                          shape genuinely request a smaller bounded page of
                          headings (cheaper, not just post-filtered);
                          preview reads paragraph content via
                          ``read_document_snapshot``.
      format:            ``"auto"`` (default, sniffed -- never from
                          extension alone except to disambiguate DOCX from
                          XLSX, which share the same ZIP magic bytes),
                          ``"xml"``, ``"json"``, ``"csv"``, ``"xlsx"``, or
                          ``"docx"``.
      allowed_root:       Optional directory the resolved ``path`` must
                          fall under -- a path escaping it is refused as
                          ``denied``/``outside_allowed_root``.
      allow_symlinks:     Set True to permit inspecting a symlink target
                          (default False).
      selector:           Optional bounded dotted/bracket JSON selector,
                          forwarded to ``inspect_file`` when the path
                          routes there; ignored otherwise.
      max_bytes:          Maximum source file size in bytes (default
                          10 MiB) -- checked before any sibling is spawned.
      timeout_seconds:    Wall-clock budget forwarded to the sibling's own
                          parse-time bound (default 5.0s) -- the subprocess
                          spawn/handshake itself gets its own separate,
                          fixed overhead allowance on top of this.
      preview_chars:      Optional override forwarded to the underlying
                          tool's own ``preview_chars``.
      max_sample_rows:    Optional override forwarded to
                          ``inspect_tabular_file``'s ``max_sample_rows``
                          (tabular routes only).

    Returns:
      ``{schema_version, source_ref, format, mime, size_bytes,
      source_sha256, parser_id, parser_version, result_hash, state, shape,
      bounds, warnings, errors, provenance_ref, local_only, operation,
      route}``. ``source_ref`` is a REDACTED portable reference, never the
      raw machine-local absolute path. ``state`` is one of ``"complete"``/
      ``"partial"``/``"failed"``/``"unavailable"``. ``route`` is one of
      ``"generic"``/``"tabular"``/``"docs"`` -- which sibling answered.
    """
    return file_inspector.inspect_local_file(
        path,
        operation=operation,
        format=format,
        allowed_root=allowed_root,
        allow_symlinks=allow_symlinks,
        selector=selector,
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
        preview_chars=preview_chars,
        max_sample_rows=max_sample_rows,
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
    without reopening the output itself. Stored as a single JSON ledger
    (upsert by normalized path) at
    ``<outputs_dir>/.meridian-outputs-cache/provenance_ledger.json`` -- fully
    local, no hosted call, no DB engine. Calling this again for the same
    path OVERWRITES the previous record in place; this is NOT an
    append-only log and no history is kept (corrected bd5b8d79 -- an earlier
    version of this docstring incorrectly described an append-only JSONL
    sidecar, which was never the actual implementation).

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
      decision_id, note, recorded_at, recorded_at_iso, content_hash}, or
      {error: ...}. ``content_hash`` (bd5b8d79) is a best-effort SHA-256 of
      ``path``'s on-disk bytes at record time (``None`` if unreadable) --
      used by ``get_provenance_status`` to later detect relocation/drift.
      This is a pure exact-record write; call ``get_provenance_status`` (not
      this tool) for a richer answer that also covers "known but
      unregistered" and directory-level fallback cases.
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
    """Exact-match lookup for one output file's reproducibility record.

    Queryable WITHOUT opening/parsing the output file itself -- reads only
    the lightweight JSON ledger written by ``record_provenance`` (corrected
    bd5b8d79 -- this was never a JSONL sidecar log; see ``record_provenance``
    for the storage-shape correction).

    A bare ``None`` here is AMBIGUOUS -- it does not distinguish "this path
    is completely unknown to the outputs tree" from "this path is a real,
    indexed output that just never had ``record_provenance`` called on it"
    from "no exact record, but a directory-level MERIDIAN_NOTES.md note
    covers it". Prefer ``get_provenance_status`` when that distinction
    matters; use this tool only when a bare exact-or-nothing answer is
    genuinely what's needed.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      path:         The output file to look up.

    Returns:
      {path, generating_script, params, sprint_item_id, decision_id, note,
      recorded_at, recorded_at_iso, content_hash}, or None if nothing has
      been recorded for this path under this outputs_dir.
    """
    return annotate.get_provenance(outputs_dir, path)


@mcp.tool()
def get_provenance_status(outputs_dir: str, path: str) -> dict[str, Any]:
    """Richer, authoritative per-file provenance answer (item bd5b8d79).

    Composes ``get_provenance`` (exact machine-recorded provenance) with
    ``outputs_local``'s index-membership and directory-level
    ``MERIDIAN_NOTES.md`` annotation systems into ONE ranked answer, so a
    bare ``None`` from ``get_provenance`` never has to be treated as "this
    file is unknown" when it might just mean "known, but unregistered" or
    "covered only by a directory-level note".

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      path:         The output file to look up.

    Returns:
      {error: ...} if outputs_dir/path are missing. Otherwise always
      {path, provenance_type, record, directory_note, staleness} where
      ``provenance_type`` is one of:

        - "exact" -- ``record`` is the exact ``get_provenance`` record;
          ``staleness`` reports {exists_on_disk, recorded_content_hash,
          current_content_hash, stale, reason} -- a best-effort check
          (reusing the SAME hasher ``fingerprint.py`` already computes
          elsewhere, no new hash scheme) for whether the file has been
          relocated, deleted, or changed since the record was made.
        - "directory_fallback" -- no exact record, but ``directory_note`` is
          a covering ``MERIDIAN_NOTES.md`` annotation. Explicitly weaker
          than "exact" -- never the same shape, never silently upgraded.
        - "unregistered" -- no exact record, no directory note, but this
          exact path IS indexed/known to ``outputs_local`` (a real output
          that simply never had ``record_provenance`` called on it).
        - "unknown" -- this path has never been discovered by the outputs
          walker at all.

      ``record``/``directory_note``/``staleness`` are ``None`` whenever not
      applicable to the returned ``provenance_type`` -- always branch on
      ``provenance_type``, never infer status from field presence alone.
    """
    return provenance_status.get_provenance_status(outputs_dir, path)


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
def get_provenance_status_envelope(
    outputs_dir: str,
    paths: list[str],
) -> dict[str, Any]:
    """Typed, lossless research-evidence provenance envelope for a batch of
    output paths (item 0ea8fd3c bridge over ``get_provenance_status``).

    One :class:`research_evidence.EvidenceRecord` (kind ``output``) per
    path, built via :func:`provenance_status.build_provenance_envelope`
    from that SAME path's :func:`get_provenance_status` answer -- never a
    second, independent provenance lookup. See that module's own docstring
    ("Typed research-evidence bridge") for the exact
    ``provenance_type`` -> resolver-status mapping (verified/stale/held/
    ambiguous/unavailable/degraded) and the partial-record rule.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      paths:        Output file paths to build typed records for. ``[]``
                    returns a valid, empty (non-partial) envelope.

    Returns:
      The canonical envelope dict (``research_evidence.envelope_to_dict()``'s
      shape: ``envelope_id``, ``generated_at``, ``records``, ``links``,
      ``version``, ``partial``, ``partial_reason``) -- pass this straight to
      ``serialize_provenance_envelope``/``parse_provenance_envelope`` for a
      round-trippable JSON/XML form, or to ``generate_handoff``'s
      ``research_evidence_envelope`` parameter to render it into a handoff.
      ``partial`` is ``True`` whenever ANY contained record is itself
      partial (e.g. a path that was never discovered, or is indexed but has
      no provenance record yet) -- never silently reported as fully
      authoritative just because SOME paths resolved cleanly.

    Raises:
      Nothing caught here escapes: an empty/missing ``outputs_dir`` or a
      malformed ``path`` entry raises ``research_evidence
      .EnvelopeValidationError`` (fails closed, matching every other
      construction-time failure in that module), rather than silently
      dropping the bad path from the envelope.
    """
    envelope = provenance_status.build_provenance_envelope(outputs_dir, paths)
    return research_evidence.envelope_to_dict(envelope)


@mcp.tool()
def serialize_provenance_envelope(
    envelope: dict[str, Any], format: str = "json",
) -> str:
    """Serialize a canonical research-evidence provenance envelope dict
    (item 0ea8fd3c) to a JSON or XML string.

    ``envelope`` is the canonical dict shape produced by this server's own
    ``get_provenance_status_envelope`` (or
    ``research_evidence.envelope_to_dict``) -- ``envelope_id``,
    ``generated_at``, ``records``, ``links``, ``version``, ``partial``,
    ``partial_reason``. Both formats are two projections of the exact same
    data (see ``research_evidence.envelope_to_dict``'s own docstring) -- they
    can never drift apart in what they're able to express, and either
    round-trips losslessly back through ``parse_provenance_envelope``.

    Args:
      envelope:  The canonical envelope dict to serialize.
      format:    ``"json"`` (default) or ``"xml"``.

    Returns:
      The serialized string. JSON output is key-sorted with stable
      indentation; the same envelope always serializes to byte-identical
      output (deterministic).

    Raises:
      research_evidence.EnvelopeValidationError: ``envelope`` is malformed
      (missing required keys, invalid enum values, etc.) or ``format`` is
      neither ``"json"`` nor ``"xml"`` -- never a raw ``KeyError``/
      ``TypeError`` escaping to the caller.
    """
    env = research_evidence.envelope_from_dict(envelope)
    return research_evidence.serialize_provenance_envelope(env, format=format)


@mcp.tool()
def parse_provenance_envelope(
    payload: str, format: str = "json",
) -> dict[str, Any]:
    """Inverse of ``serialize_provenance_envelope`` (item 0ea8fd3c): parse a
    JSON or XML provenance envelope payload back into its canonical dict
    shape.

    Args:
      payload:  The serialized envelope string (as produced by
                ``serialize_provenance_envelope``, or hand-built by another
                system to the same schema).
      format:   ``"json"`` (default) or ``"xml"`` -- must match the format
                ``payload`` was serialized in.

    Returns:
      The canonical envelope dict (``envelope_id``, ``generated_at``,
      ``records``, ``links``, ``version``, ``partial``, ``partial_reason``)
      -- the exact same shape ``get_provenance_status_envelope`` returns, so
      the two tools compose in either direction.

    Raises:
      research_evidence.EnvelopeValidationError: ``payload`` is malformed
      JSON/XML, structurally invalid (missing required keys, invalid enum
      values), or ``format`` is neither ``"json"`` nor ``"xml"`` -- never a
      raw ``json.JSONDecodeError``/``xml.etree.ElementTree.ParseError``
      escaping to the caller.
    """
    env = research_evidence.parse_provenance_envelope(payload, format=format)
    return research_evidence.envelope_to_dict(env)


@mcp.tool()
def get_evidence_status_and_trusted_pointers(
    envelope: dict[str, Any], limit: int | None = None,
) -> dict[str, Any]:
    """MDE-5 -- the small, BOUNDED projection a handoff embeds instead of the
    full envelope: a machine-readable evidence status summary plus the
    subset of records safe to treat as already-verified ("trusted pointers")
    without re-resolving anything.

    ``envelope`` is the canonical dict shape (same as every other tool on
    this surface -- ``get_provenance_status_envelope``'s return value, or
    ``research_evidence.envelope_to_dict``'s). This is the SAME data
    ``meridian.handoff.generate_handoff(research_evidence_envelope=...,
    emit_manifest=True)`` embeds into a goal-mode ``<handoff_manifest>``'s
    ``<evidence_status>``/``<trusted_pointers>`` elements when a caller
    passes an envelope through -- exposed here directly too, so a caller
    that only has this server (not meridian core) can get the same
    projection without round-tripping through a handoff.

    Args:
      envelope:  The canonical envelope dict to summarize.
      limit:     Optional cap on the number of trusted pointers returned
                 (never silent -- compare ``len(trusted_pointers)`` against
                 ``status["authoritative_record_count"]`` to detect capping).

    Returns:
      ``{"status": <evidence_status_summary() dict>, "trusted_pointers":
      [<id, kind, locator, label>, ...]}``.

    Raises:
      research_evidence.EnvelopeValidationError: ``envelope`` is malformed.
    """
    env = research_evidence.envelope_from_dict(envelope)
    return {
        "status": research_evidence.evidence_status_summary(env),
        "trusted_pointers": research_evidence.trusted_pointers(env, limit=limit),
    }


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
def bind_artifact_provenance(
    outputs_dir: str,
    artifacts: "list[dict[str, Any]]",
    fuzzy_limit: int = 25,
) -> dict[str, Any]:
    """Join structural document artifacts (figures/tables/equations) to
    authoritative per-file provenance, fail-closed (item 6d02f343).

    Given a document's own list of structural artifacts -- one entry per
    figure/table/equation it currently embeds, each carrying whatever
    ``canonical_path``/``expected_sha256`` the document-writing tool already
    knows -- resolves each against meridian-outputs' per-file provenance
    (:func:`resolve_figure_output`'s exact + basename tiers, authoritative;
    :func:`get_provenance_status`'s directory-level note, fallback evidence
    only) and classifies it so a caller can reject or quarantine a write
    instead of silently promoting an orphaned or hash-mismatched artifact.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      artifacts:    One dict per structural artifact: ``{"artifact_id": <str>,
                    "kind": <"figure"|"table"|"equation">,
                    "canonical_path": <str|None>, "expected_sha256":
                    <str|None>}``. ``artifact_id``/``kind`` are carried
                    through unchanged for the caller's own bookkeeping.
      fuzzy_limit:  Forwarded to the basename-fallback tier (default 25).

    Returns:
      ``{"bindings": [...], "counts": {...}, "all_clear": bool}`` -- see
      :func:`meridian_outputs.provenance.bind_artifact_provenance` for the
      full per-binding shape and status semantics (``resolved``/
      ``hash_mismatch``/``orphaned``/``unresolved``). ``all_clear`` is
      ``True`` only when every artifact is ``resolved``.
    """
    return provenance.bind_artifact_provenance(
        outputs_dir, artifacts, fuzzy_limit=fuzzy_limit,
    )


@mcp.tool()
def register_artifact(
    outputs_dir: str,
    kind: str,
    canonical_path: str | None = None,
    expected_sha256: str | None = None,
    generator: str | None = None,
    run_id: str | None = None,
    source_locator: str | None = None,
    role: str | None = None,
    lifecycle_state: str = artifact_registry.ACTIVE,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind (create or update) a stable, relocation-safe public artifact
    identity (item e1c979e3).

    The public ``artifact_id`` is minted deterministically from PORTABLE
    signals only (``kind`` + content hash + ``generator`` +
    ``source_locator``) -- never from ``canonical_path`` -- so re-registering
    the same logical artifact after it has been moved/copied/renamed yields
    the identical id. ``canonical_path`` is stored only as redacted, local
    metadata (see ``strip_local_metadata`` for a shareable projection).

    Args:
      outputs_dir:      Absolute path to the outputs directory.
      kind:              Artifact kind, e.g. "figure"/"table"/"equation"/
                        "output"/"document". Required.
      canonical_path:    Current on-disk location, if known -- hashed
                        best-effort to derive the identity's content hash.
      expected_sha256:  Optional caller-asserted hash, verified against the
                        freshly-computed hash and against any hash already on
                        file for this id; a mismatch against either refuses
                        the write.
      generator:         The script/tool that produced this artifact.
      run_id:            Optional run/session identifier.
      source_locator:    Portable source locator (relative path, dataset
                        name, DOI, sprint-item id, ...).
      role:              "canonical" or "archival", if known.
      lifecycle_state:   One of "active"/"quarantined"/"deprecated"/
                        "deleted" (default "active").
      metadata:          Opaque extra fields, shallow-merged on update.

    Returns:
      The stored record dict plus ``created`` (True on first registration).

    Raises:
      ValueError (artifact_registry.RegistryError): missing required
      arguments, an invalid lifecycle_state, no portable identity signal to
      anchor the id to, or a contradicting hash -- fail-closed, never a
      silently-wrong registration.
    """
    return artifact_registry.register_artifact(
        outputs_dir, kind, canonical_path=canonical_path,
        expected_sha256=expected_sha256, generator=generator, run_id=run_id,
        source_locator=source_locator, role=role, lifecycle_state=lifecycle_state,
        metadata=metadata,
    )


@mcp.tool()
def resolve_artifact(
    outputs_dir: str,
    artifact_id: str | None = None,
    canonical_path: str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Resolve an artifact by public id or by local path, with an explicit
    resolved/ambiguous/unresolved/orphaned/hash_mismatch outcome -- never a
    silent basename/fuzzy guess (item e1c979e3).

    Args:
      outputs_dir:      Absolute path to the outputs directory.
      artifact_id:      Direct lookup by public id, when known.
      canonical_path:    Resolve by content hash (strongest) or exact prior
                        local-path sighting (weaker) when ``artifact_id``
                        isn't known.
      expected_sha256:  Optional hash to verify the resolved record against.

    Returns:
      ``{status, artifact_id, record, evidence, candidates, reason}`` -- see
      ``artifact_registry.resolve_artifact`` for the full status semantics.
      ``status="ambiguous"`` always carries every candidate id in
      ``candidates`` and a ``None`` record -- never narrowed to a guess.
    """
    return artifact_registry.resolve_artifact(
        outputs_dir, artifact_id=artifact_id, canonical_path=canonical_path,
        expected_sha256=expected_sha256,
    )


@mcp.tool()
def verify_artifact_hash(
    outputs_dir: str, artifact_id: str, path: str | None = None,
) -> dict[str, Any]:
    """Recompute a registered artifact's content hash from disk and compare
    it to what is on file (item e1c979e3).

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      artifact_id:  The registered artifact to verify.
      path:         Explicit path to hash; defaults to the artifact's most
                    recently seen local path sighting.

    Returns:
      ``{artifact_id, verified, current_hash, registered_hash, path,
      reason}``. ``verified`` is True only when both hashes are present and
      equal -- a missing registered hash or unreadable path is never
      reported as verified.
    """
    return artifact_registry.verify_artifact_hash(outputs_dir, artifact_id, path=path)


@mcp.tool()
def bind_artifact_source_edge(
    outputs_dir: str,
    artifact_id: str,
    source_locator: str,
    relation: str = "produced_by",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a typed source<->artifact edge for a registered artifact
    (item e1c979e3). Idempotent for the same (artifact_id, source_locator,
    relation) triple.

    Args:
      outputs_dir:      Absolute path to the outputs directory.
      artifact_id:      Must already be registered (``register_artifact``).
      source_locator:    Portable locator for the source side of the edge.
      relation:          Free-text edge verb (default "produced_by").
      metadata:          Opaque extra fields, shallow-merged on update.

    Returns:
      The stored edge dict {edge_id, artifact_id, source_locator, relation,
      created_at, metadata}.

    Raises:
      ValueError (artifact_registry.RegistryError): ``artifact_id`` was
      never registered, or ``source_locator``/``relation`` is empty.
    """
    return artifact_registry.bind_source_edge(
        outputs_dir, artifact_id, source_locator, relation=relation, metadata=metadata,
    )


@mcp.tool()
def get_artifact_sources(outputs_dir: str, artifact_id: str) -> list[dict[str, Any]]:
    """Artifact -> its bound sources (item e1c979e3). Sorted, deterministic."""
    return artifact_registry.get_artifact_sources(outputs_dir, artifact_id)


@mcp.tool()
def get_source_artifacts(outputs_dir: str, source_locator: str) -> list[dict[str, Any]]:
    """Source -> the artifacts it produced (item e1c979e3). Sorted, deterministic."""
    return artifact_registry.get_source_artifacts(outputs_dir, source_locator)


@mcp.tool()
def list_registered_artifacts(
    outputs_dir: str, kind: str | None = None, lifecycle_state: str | None = None,
) -> list[dict[str, Any]]:
    """All registered artifacts, optionally filtered by kind/lifecycle_state
    (item e1c979e3). Sorted by artifact_id."""
    return artifact_registry.list_artifacts(outputs_dir, kind=kind, lifecycle_state=lifecycle_state)


@mcp.tool()
def reconcile_legacy_artifact_outputs(
    outputs_dir: str,
    legacy_entries: list[dict[str, Any]] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Migration/reconciliation report for legacy outputs against the
    artifact registry (item e1c979e3).

    Defaults to reconciling everything already known to
    ``annotate.list_provenance`` (the common "predates the registry" case)
    when ``legacy_entries`` is omitted.

    Args:
      outputs_dir:      Absolute path to the outputs directory.
      legacy_entries:   Explicit ``{kind, canonical_path, expected_sha256,
                        generator, source_locator, role}`` dicts to
                        reconcile, or omit to use the provenance ledger.
      dry_run:          Preview only (default True) -- nothing is written;
                        entries that would be newly registered are listed
                        under ``would_register`` with their would-be id.

    Returns:
      ``{outputs_dir, dry_run, scanned, already_registered,
      registered|would_register, ambiguous, errors, skipped_unanchored}`` --
      see ``artifact_registry.reconcile_legacy_outputs`` for full semantics.
      An ambiguous or unanchored legacy entry is never silently registered.
    """
    return artifact_registry.reconcile_legacy_outputs(
        outputs_dir, legacy_entries, dry_run=dry_run,
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


@mcp.tool()
def start_run_manifest(
    outputs_dir: str,
    run_id: str,
    command_name: str,
    command_args: dict[str, Any] | None = None,
    project_id: str | None = None,
    version: str | None = None,
    sprint_item_id: str | None = None,
    repo_dir: str | None = None,
    input_paths: list[str] | None = None,
    expected_counts: dict[str, int] | None = None,
    allow_partial: bool = False,
    external_manifest_hash: str | None = None,
) -> dict[str, Any]:
    """Persist an in-progress canonical run manifest for ``run_id`` (item
    37ce5537) -- one durable receipt binding project/repo identity,
    tool/package version, command identity, input hashes, the indexing
    bounds actually in effect, every sibling ledger's on-disk location, and
    a convergence-state snapshot, all in ONE place.

    Composes this package's existing modules rather than duplicating them:
    hashes via ``fingerprint.script_content_hash``, git state via a local
    best-effort ``run_manifest.capture_git_state`` (this package cannot
    import ``meridian.executor_contract`` across the package boundary --
    see ``run_manifest``'s module docstring), and a convergence snapshot via
    ``outputs_local.get_convergence_state``. No per-path provenance data or
    artifact record is copied into this ledger -- only references.

    Idempotent: calling this again with identical identity inputs (same
    ``run_id`` plus everything else that feeds the identity hash) returns
    the ALREADY-PERSISTED record unchanged, including a prior
    ``finalize_run_manifest`` outcome if one already ran, rather than
    clobbering it with a fresh ``in_progress`` skeleton -- this is what
    makes an interrupted run's receipt resumable.

    Args:
      outputs_dir:            Absolute path to the outputs directory.
      run_id:                  Unique id for this run. Required.
      command_name:            The tool/script/command that is running.
                              Required.
      command_args:            Opaque caller-supplied args for that command.
      project_id/version/
      sprint_item_id:          Optional project/lineage identity.
      repo_dir:                Optional local repo path to best-effort
                              capture ``git_state`` (HEAD + dirty files) for.
      input_paths:             Input file paths to hash now.
      expected_counts:         Optional ``{label: non-negative int}`` dict.
      allow_partial:           Whether a partial verdict is an acceptable
                              outcome for this run.
      external_manifest_hash:  Optional cross-reference to an externally
                              (e.g. ``meridian.executor_contract``) built
                              execution-manifest hash -- stored, never
                              re-derived.

    Returns:
      The persisted manifest record (``phase="in_progress"`` on a fresh
      start).

    Raises:
      ValueError (run_manifest.RunManifestError): missing required
      arguments, an invalid ``expected_counts`` value, or a manifest already
      exists for this ``run_id`` with a DIFFERENT identity hash -- fails
      closed rather than silently overwriting a different run's identity.
    """
    return run_manifest.start_run_manifest(
        outputs_dir, run_id=run_id, command_name=command_name,
        command_args=command_args, project_id=project_id, version=version,
        sprint_item_id=sprint_item_id, repo_dir=repo_dir,
        input_paths=input_paths, expected_counts=expected_counts,
        allow_partial=allow_partial, external_manifest_hash=external_manifest_hash,
    )


@mcp.tool()
def finalize_run_manifest(
    outputs_dir: str,
    run_id: str,
    output_paths: list[str] | None = None,
    artifact_ids: list[str] | None = None,
    status: str = "complete",
    reason: str | None = None,
) -> dict[str, Any]:
    """Bind exact output hashes and artifact-id references to an
    already-started run manifest, and mark its final phase (item 37ce5537).

    Fail-closed exact output binding: every path in ``output_paths`` is
    RE-HASHED right now -- never trusts a caller-declared hash. A missing/
    unreadable output, or an ``artifact_id`` that doesn't resolve via
    ``resolve_artifact``'s own registry, automatically downgrades an
    attempted ``status="complete"`` to ``"partial"`` -- a finalize call is
    never silently reported as a clean, fully-verified run when something
    it claimed doesn't actually check out.

    Args:
      outputs_dir:    Absolute path to the outputs directory.
      run_id:          The run started via ``start_run_manifest``.
      output_paths:    Exact output file paths this run produced.
      artifact_ids:    Public artifact ids (from ``register_artifact``) this
                      run is claiming credit for.
      status:          "complete" (default), "failed", or "partial".
      reason:          Optional human-readable explanation.

    Returns:
      The updated manifest record. Its ``manifest_hash`` is unchanged from
      ``start_run_manifest`` -- finalize only ever touches outcome fields.

    Raises:
      ValueError (run_manifest.RunManifestError): ``outputs_dir``/``run_id``
      missing, an invalid ``status``, or no manifest was ever started for
      this ``run_id``.
    """
    return run_manifest.finalize_run_manifest(
        outputs_dir, run_id, output_paths=output_paths,
        artifact_ids=artifact_ids, status=status, reason=reason,
    )


@mcp.tool()
def get_run_manifest(outputs_dir: str, run_id: str) -> dict[str, Any] | None:
    """Look up the current run-manifest record for ``run_id`` (item
    37ce5537), whatever phase it's currently in.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      run_id:        The run to look up.

    Returns:
      The manifest record dict, or ``None`` if nothing was ever started for
      this ``run_id``.
    """
    return run_manifest.get_run_manifest(outputs_dir, run_id)


@mcp.tool()
def list_run_manifests(outputs_dir: str) -> list[dict[str, Any]]:
    """List every run-manifest record ever started under ``outputs_dir``
    (item 37ce5537), sorted by ``run_id``.

    Args:
      outputs_dir:  Absolute path to the outputs directory.

    Returns:
      A list of manifest record dicts; ``[]`` if none have been started yet.
    """
    return run_manifest.list_run_manifests(outputs_dir)


@mcp.tool()
def get_run_manifest_envelope(outputs_dir: str, run_id: str) -> dict[str, Any]:
    """Typed, lossless research-evidence envelope for one run manifest (item
    37ce5537) -- a single ``RUN``-kind ``EvidenceRecord`` (the
    previously-unused ``EvidenceKind.RUN`` slot ``research_evidence.py``
    already reserved) wrapping the manifest's full structured fields under
    ``attributes``.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      run_id:        The run to build an envelope for.

    Returns:
      The canonical envelope dict (same shape
      ``get_provenance_status_envelope`` returns) -- pass to
      ``serialize_provenance_envelope``/``parse_provenance_envelope`` for a
      round-trippable JSON/XML form.

    Raises:
      ValueError (run_manifest.RunManifestError): no manifest exists for
      ``run_id`` under ``outputs_dir``.
    """
    envelope = run_manifest.build_run_manifest_envelope(outputs_dir, run_id)
    return research_evidence.envelope_to_dict(envelope)


def main() -> None:
    """Console entry point (``uvx --from <path> meridian-outputs-mcp``)."""
    mcp.run()


if __name__ == "__main__":
    main()
