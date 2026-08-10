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

| Tool | Description |
|------|-------------|
| `search_outputs` | BM25 full-text search over CSV/JSON/NPY files, with a literal-filename-match boost |
| `register_output_paths` | Directly index an explicit list of exactly-known output paths, no full-root walk wait |
| `get_convergence_state` | Read-only snapshot of a local outputs index's walk progress (scan boundary, pending count, index lock) |
| `annotate_outputs` | Add/update human annotation for a file or directory |
| `classify_outputs` | Classify paths as canonical or archival (two-stage SHA-256) |
| `resolve_figure_output` | Forward resolution: a figure's file path -> its generating source (exact-path, then basename-fallback) |
| `npy_metadata` | Read .npy header (shape/dtype/size) without loading the array |
| `file_fingerprint` | Cheap content signature (CSV columns, JSON keys, generating script) |
| `search_logs` | Disposable regex log search: Tier 0 ripgrep scan + Tier 1 JSON/timestamp ranking, no persistent index |

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

## Security

- Secret files are excluded before indexing (`.env*`, `*.key`, `*secret*`,
  `*credential*`, `config.*`, `*.pem`, and many others).
- The local index cache is auto-added to `.gitignore` on first use.
- Concurrent index writes are serialised through `IndexFileLock`
  (threading + optional cross-process portalocker).
- Same inputs always produce the same results (deterministic path ordering,
  stable sort, no non-reproducible set/dict iteration in output paths).
