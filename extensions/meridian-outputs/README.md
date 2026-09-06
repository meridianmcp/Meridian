# meridian-outputs

Standalone local MCP server for outputs indexing (CSV/JSON/NPY) — wave-1 stopgap.

Runs **fully locally** against a real `outputs_dir` on your machine.  No hosted
Meridian call is made.  The hosted-aware smart-routing layer is a separate later
item.

## Install

```bash
uvx --from /path/to/meridian-outputs meridian-outputs-mcp
```

Or add to your MCP client config:

```json
{
  "mcpServers": {
    "meridian-outputs": {
      "command": "uvx",
      "args": ["--from", "/path/to/extensions/meridian-outputs", "meridian-outputs-mcp"]
    }
  }
}
```

## Tools

### Search & indexing

| Tool | Description |
|------|-------------|
| `search_outputs` | BM25 full-text search over CSV/JSON/text/PDF files (body content) plus filename-only search over everything else (NPY etc.), with a literal-filename-match boost and an optional `subtree` scope |
| `register_output_paths` | Directly register a known list of output paths so they're searchable immediately, without waiting for the ambient walk |
| `get_convergence_state` | Read-only snapshot of how far the index walk has progressed (never triggers indexing) |
| `search_logs` | Disposable regex search over a log directory tree (ripgrep tier 0, pure-Python fallback, timestamp/JSON sniffing tier 1) |
| `classify_outputs` | Classify paths as canonical or archival (filename heuristics + two-stage SHA-256 byte-identity check) |
| `npy_metadata` | Read a `.npy` header (shape/dtype/size) without loading the array |
| `file_fingerprint` | Cheap content signature (CSV columns, JSON keys, generating-script hint) |

### Local file inspection router (item a4cb12bf)

`inspect_local_file` is one bounded local inspect/read workflow for a single
XML, JSON, CSV, XLSX, or DOCX file — without a tunnel and without a second
parser. It routes to whichever existing capability already understands the
file's format (`meridian-file-inspection`'s `inspect_file`/
`inspect_tabular_file`, or `meridian-docs`'s `document_outline`/
`read_document_snapshot`), spawning each as its own short-lived local MCP
stdio server rather than importing it directly (these are independently
`uvx`-installable packages — see `meridian_outputs/file_inspector.py`'s
module docstring for why a direct import isn't viable in production), and
normalizes every answer into one canonical envelope.

Every response carries `"local_only": true` (this tool never makes a
network/tunnel call of its own) and an `operation` tier — `"metadata"`
(identity/size/hash only), `"shape"` (default — structure without content
previews/samples), or `"preview"` (the full bounded response). When the
required sibling process can't even be reached, `state` is the new
`"unavailable"` value (distinct from `"failed"`, which means a sibling ran
and reported a real parse error) — never a raised exception, never a hang.
`search_outputs`/`register_output_paths` are untouched by this addition.

| Tool | Description |
|------|-------------|
| `inspect_local_file` | Route one file to the matching bounded inspector (XML/JSON/CSV/XLSX/DOCX) and return a single canonical envelope, with explicit `local_only`/`unavailable` state |

### Annotations & provenance

| Tool | Description |
|------|-------------|
| `annotate_outputs` | Add/update a human annotation for a file or directory |
| `record_provenance` | Attach machine-oriented reproducibility metadata (generating script, params, content hash) to one output file |
| `get_provenance` | Exact-match lookup of one file's `record_provenance` record (ambiguous `None` on a miss — see `get_provenance_status`) |
| `get_provenance_status` | Richer, ranked per-file provenance answer: exact / directory-fallback / unregistered / unknown / stale-by-script, plus staleness, archival identity, and index-convergence awareness |
| `list_provenance` | List the latest `record_provenance` record for every path recorded under an outputs directory |
| `resolve_figure_output` | Forward resolution: a document figure's file path → its generating source (exact match, then relocation-tolerant basename fallback) |
| `find_outputs_by_source` | Reverse resolution: a script/data source path → the outputs it produced |
| `bind_artifact_provenance` | Join a document's structural artifacts (figures/tables/equations) to authoritative per-file provenance, fail-closed |
| `tag_output` | Fingerprint an output and record it in a persistent ledger stamped with its generating script's content hash |
| `check_staleness` | Re-hash every ledger-tagged generating script and report which previously-tagged outputs are now stale |
| `find_stale_by_script` | All ledger-tagged output paths whose generating script's current content no longer matches the hash recorded at tag time |
| `script_content_hash` | SHA-256 of a generating script's current on-disk content (the primitive the three tools above use internally) |

### Hash / provenance tools

The tools an "Outputs hash/provenance check" (see `meridian.docx_integrity_gate.RECIPE_CHECK_REGISTRY`
in the core repo) resolves against:

| Tool | Description |
|------|-------------|
| `record_provenance` | Attach reproducibility metadata (generating script, params, sprint item/decision) to one output file |
| `get_provenance` | Exact-match lookup of one output file's recorded provenance; a bare `None` is ambiguous, prefer `get_provenance_status` |
| `get_provenance_status` | Richer, authoritative per-file provenance answer — exact record vs. directory-note fallback vs. unregistered vs. unknown, plus content-hash staleness |
| `list_provenance` | List the latest provenance record for every path recorded under an outputs directory |
| `find_outputs_by_source` | Reverse resolution: a script/data source path -> the outputs it produced |
| `bind_artifact_provenance` | Join a document's structural artifacts (figures/tables/equations) to authoritative provenance, fail-closed |
| `tag_output` | Fingerprint an output file and record it in a ledger, stamped with its generating script's content hash |
| `check_staleness` | Re-hash every ledger-tagged generating script and report which outputs are now stale |
| `find_stale_by_script` | All ledger-tagged outputs whose recorded hash no longer matches `script_path`'s current content |
| `script_content_hash` | SHA-256 of a generating script's current on-disk content |

### Typed research-evidence provenance envelope (item 0ea8fd3c)

`meridian_outputs.research_evidence` defines a canonical, lossless evidence
model — typed `EvidenceRecord`/`EvidenceLink` nodes (one per
claim/source/citation/dataset/code/run/output/figure/table/document/review),
each carrying identity, hashes/fingerprints, timestamps/revisions, and an
explicit six-state resolver (`verified`/`stale`/`held`/`ambiguous`/
`unavailable`/`degraded`) plus a confidence float — packaged into a
`ProvenanceEnvelope` that serializes losslessly to JSON *and* XML. Markdown
(`ProvenanceEnvelope.to_markdown()`) is only ever a read-only projection of
this typed data, never the source of truth, and a partial/unresolved record
or link is never rendered without a visible caveat.

`meridian_outputs.provenance_status` bridges this module's own per-file
`get_provenance_status()` answers into that typed model (see that module's
own docstring, "Typed research-evidence bridge", for the exact
`provenance_type` → resolver-status mapping table).

| Tool | Description |
|------|-------------|
| `get_provenance_status_envelope` | Build one typed, lossless `ProvenanceEnvelope` (canonical dict shape) from a batch of output paths, via `get_provenance_status` + the typed-evidence bridge |
| `serialize_provenance_envelope` | Serialize a canonical envelope dict to a JSON or XML string (both are lossless projections of the same data; either round-trips back through `parse_provenance_envelope`) |
| `parse_provenance_envelope` | Inverse of `serialize_provenance_envelope`: parse a JSON/XML envelope payload back into its canonical dict shape |
| `get_evidence_status_and_trusted_pointers` | MDE-5: the small, bounded `{status, trusted_pointers}` projection a handoff manifest embeds instead of the full envelope -- status counts by resolver state plus the subset of records safe to treat as already-verified without re-resolving |

`meridian/handoff.py` (meridian core) can also render an envelope built this
way as an additive "## Research Evidence" section of a `generate_handoff(...,
research_evidence_envelope=...)` call (`mode` in `{"full", "delta"}`) — that
integration is duck-typed (meridian core never imports this package; see
`_render_research_evidence_block`'s own docstring in `meridian/handoff.py`).

**Planned, not yet implemented:** `validate_output_semantics` is a separate,
not-yet-built output-semantic-validation capability, tracked under its own
sprint item. The artifact-registry gap noted above in earlier revisions of
this README was closed by item e1c979e3 — see the next section.

### Stable artifact registry (item e1c979e3)

`meridian_outputs.artifact_registry` is a durable, atomic JSON-ledger
registry (`<outputs_dir>/.meridian-outputs-cache/artifact_registry.json`)
that mints a **relocation-safe public `artifact_id`** from portable identity
signals only — content hash, generator/tool, and an explicit
`source_locator` — never from the absolute path. Re-registering the same
logical artifact after it has moved yields the identical id. The exact
on-disk path is stored ONLY inside a clearly-separated, redactable
`local_paths` bucket. Every resolution outcome is explicit
(`resolved`/`ambiguous`/`unresolved`/`orphaned`/`hash_mismatch`) — this
module never falls back to a basename/fuzzy guess the way
`resolve_figure_output`'s second tier does; multiple genuine candidates are
always surfaced together, never silently narrowed to one.

| Tool | Description |
|------|-------------|
| `register_artifact` | Bind (create or update) a stable public artifact identity; fails closed with no portable anchor or a contradicting hash |
| `resolve_artifact` | Resolve by public id or by local path (content-hash tier, then exact-path-sighting tier); explicit `ambiguous`/`unresolved`/`orphaned` outcomes, never a basename guess |
| `verify_artifact_hash` | Recompute an artifact's content hash from disk on demand and compare to what is on file; never silently "verified" with nothing to compare |
| `bind_artifact_source_edge` | Record a typed edge between a registered artifact and a source locator (idempotent) |
| `get_artifact_sources` | Artifact → its bound sources |
| `get_source_artifacts` | Source → the artifacts it produced |
| `list_registered_artifacts` | All registered artifacts, optionally filtered by kind/lifecycle_state |
| `reconcile_legacy_artifact_outputs` | Migration/reconciliation report for legacy outputs (defaults to the `record_provenance` ledger); dry-run preview or real registration; ambiguous/unanchored entries are never silently registered |

### Canonical run manifest (item 37ce5537)

`meridian_outputs.run_manifest` composes this package's existing modules
behind one run-scoped receipt — it introduces exactly ONE new ledger
(`<outputs_dir>/.meridian-outputs-cache/run_manifest_ledger.json`, keyed by
`run_id`) and duplicates none of the state the sibling ledgers above already
own: hashes come from `fingerprint.script_content_hash`, artifact references
are validated (never re-derived) against `artifact_registry`, and the
convergence snapshot comes from `outputs_local.get_convergence_state`.

A run manifest binds, in one place: project/repo identity (including a
best-effort local `git_state` capture — this package cannot import
`meridian.executor_contract` across the package boundary, see the module's
own docstring), package/tool version, command identity, input/output file
hashes, the indexing bounds actually in effect (max workers, batch caps,
adaptive thresholds, DuckDB/Tantivy memory), every sibling ledger's on-disk
location, referenced artifact ids, and an explicit `phase`
(`in_progress`/`complete`/`failed`/`partial`).

`start_run_manifest` persists an `in_progress` receipt immediately — an
interrupted run's manifest is resumable, never lost. Its `manifest_hash` is
a deterministic identity fingerprint (excludes wall-clock and
lifecycle/outcome fields): identical identity inputs on unchanged
repo/runtime state hash identically every time, and a same-`run_id` call
with a genuinely different identity is refused rather than silently
overwritten — a new run needs a new `run_id`. `finalize_run_manifest`
re-hashes every declared output path and validates every declared artifact
id RIGHT NOW (fail-closed): anything that doesn't check out downgrades an
attempted `"complete"` to `"partial"` automatically, and `manifest_hash`
never changes across finalize. `get_run_manifest_envelope` represents the
finished (or still-partial) manifest as a `RUN`-kind `EvidenceRecord` — the
`EvidenceKind.RUN` slot `research_evidence.py` already reserved but nothing
populated until this item — so it round-trips through the exact same
lossless JSON/XML envelope machinery as every other provenance answer in
this package, with no second codec.

| Tool | Description |
|------|-------------|
| `start_run_manifest` | Persist an in-progress canonical run manifest: project/repo/package/command identity, input hashes, indexing bounds in effect, sibling-ledger locations, convergence snapshot. Idempotent; refuses a same-`run_id`/different-identity collision |
| `finalize_run_manifest` | Bind exact (re-hashed now) output paths and validated artifact-id references to a started run, and mark its final phase — fails closed to `partial` on anything unverified |
| `get_run_manifest` | Look up the current run-manifest record for a `run_id`, whatever phase it's in |
| `list_run_manifests` | List every run-manifest record started under an outputs directory |
| `get_run_manifest_envelope` | Typed, lossless `RUN`-kind `ProvenanceEnvelope` for one run manifest |

## Security

- Secret files are excluded before indexing (`.env*`, `*.key`, `*secret*`,
  `*credential*`, `config.*`, `*.pem`, and many others).
- The local index cache is auto-added to `.gitignore` on first use.
- Concurrent index writes are serialised through `IndexFileLock`
  (threading + optional cross-process portalocker).
- Same inputs always produce the same results (deterministic path ordering,
  stable sort, no non-reproducible set/dict iteration in output paths).
