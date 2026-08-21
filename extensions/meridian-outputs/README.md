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
| `search_outputs` | BM25 full-text search over CSV/JSON/NPY files, with a literal-filename-match boost and an optional `subtree` scope |
| `register_output_paths` | Directly register a known list of output paths so they're searchable immediately, without waiting for the ambient walk |
| `get_convergence_state` | Read-only snapshot of how far the index walk has progressed (never triggers indexing) |
| `search_logs` | Disposable regex search over a log directory tree (ripgrep tier 0, pure-Python fallback, timestamp/JSON sniffing tier 1) |
| `classify_outputs` | Classify paths as canonical or archival (filename heuristics + two-stage SHA-256 byte-identity check) |
| `npy_metadata` | Read a `.npy` header (shape/dtype/size) without loading the array |
| `file_fingerprint` | Cheap content signature (CSV columns, JSON keys, generating-script hint) |

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

`meridian/handoff.py` (meridian core) can also render an envelope built this
way as an additive "## Research Evidence" section of a `generate_handoff(...,
research_evidence_envelope=...)` call (`mode` in `{"full", "delta"}`) — that
integration is duck-typed (meridian core never imports this package; see
`_render_research_evidence_block`'s own docstring in `meridian/handoff.py`).

**Planned, not yet implemented:** `validate_output_semantics`,
`write_artifact_registry`, and `resolve_artifact_registry` are a separate,
not-yet-built capability — a cross-envelope artifact *registry*
(persistence/lookup across many envelopes) and output-semantic validation —
tracked under their own sprint item(s), distinct from this item's typed
evidence model + envelope. Nothing in this README, or in
`research_evidence.py`'s own module docstring, should be read as a promise
that those three exist anywhere in this codebase yet.

## Security

- Secret files are excluded before indexing (`.env*`, `*.key`, `*secret*`,
  `*credential*`, `config.*`, `*.pem`, and many others).
- The local index cache is auto-added to `.gitignore` on first use.
- Concurrent index writes are serialised through `IndexFileLock`
  (threading + optional cross-process portalocker).
- Same inputs always produce the same results (deterministic path ordering,
  stable sort, no non-reproducible set/dict iteration in output paths).
