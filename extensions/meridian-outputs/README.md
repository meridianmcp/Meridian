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
| `annotate_outputs` | Add/update human annotation for a file or directory |
| `classify_outputs` | Classify paths as canonical or archival (two-stage SHA-256) |
| `resolve_figure_output` | Exact-path lookup of a figure in the index |
| `npy_metadata` | Read .npy header (shape/dtype/size) without loading the array |
| `file_fingerprint` | Cheap content signature (CSV columns, JSON keys, generating script) |

## Security

- Secret files are excluded before indexing (`.env*`, `*.key`, `*secret*`,
  `*credential*`, `config.*`, `*.pem`, and many others).
- The local index cache is auto-added to `.gitignore` on first use.
- Concurrent index writes are serialised through `IndexFileLock`
  (threading + optional cross-process portalocker).
- Same inputs always produce the same results (deterministic path ordering,
  stable sort, no non-reproducible set/dict iteration in output paths).
