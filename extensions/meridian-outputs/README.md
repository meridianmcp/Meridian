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
| `search_outputs` | BM25 full-text search over CSV/JSON/NPY files |
| `register_output_paths` | Directly register a known list of output file paths so they're searchable immediately, without waiting for the ambient full-root walk |
| `get_convergence_state` | Read-only snapshot of how far the local outputs index walk has gotten (pending count, scan boundary, lock lease) -- call before trusting a zero-hit search as a genuine miss |
| `annotate_outputs` | Add/update human annotation for a file or directory |
| `record_provenance` | Attach reproducibility metadata (generating script, params, sprint-item/decision ids) to one output file |
| `get_provenance` | Exact-match lookup of a file's recorded reproducibility record |
| `get_provenance_status` | Richer, authoritative per-file provenance answer: exact / directory_fallback / unregistered / unknown, plus staleness |
| `list_provenance` | List the latest reproducibility record for every path recorded under an outputs directory |
| `classify_outputs` | Classify paths as canonical or archival (two-stage SHA-256) |
| `resolve_figure_output` | Forward resolution: a figure's file path -> its generating source (exact-path, then relocation-tolerant basename fallback) |
| `find_outputs_by_source` | Reverse resolution: a script/data source path -> the outputs it produced |
| `bind_artifact_provenance` | Join a document's structural artifacts (figures/tables/equations) to authoritative per-file provenance, fail-closed |
| `npy_metadata` | Read .npy header (shape/dtype/size) without loading the array |
| `file_fingerprint` | Cheap content signature (CSV columns, JSON keys, generating script) |
| `search_logs` | Disposable regex log search: Tier 0 ripgrep scan + Tier 1 JSON/timestamp ranking, no persistent index |
| `tag_output` | Fingerprint an output file and record it in a ledger stamped with its generating script's content hash at this moment |
| `check_staleness` | Re-hash every ledger-tagged generating script and report which previously-tagged outputs are now stale |
| `find_stale_by_script` | List ledger-tagged output paths produced by a script in a content state other than its current on-disk content |
| `script_content_hash` | SHA-256 of a generating script's current on-disk content |

## Security

- Secret files are excluded before indexing (`.env*`, `*.key`, `*secret*`,
  `*credential*`, `config.*`, `*.pem`, and many others).
- The local index cache is auto-added to `.gitignore` on first use.
- Concurrent index writes are serialised through `IndexFileLock`
  (threading + optional cross-process portalocker).
- Same inputs always produce the same results (deterministic path ordering,
  stable sort, no non-reproducible set/dict iteration in output paths).
