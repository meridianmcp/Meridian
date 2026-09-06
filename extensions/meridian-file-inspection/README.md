# meridian-file-inspection

Standalone local MCP server exposing a tunnel-independent, bounded
single-file XML/JSON/CSV/XLSX inspector.

Runs **fully locally** against one file on your machine. No hosted Meridian
call is made, no tunnel or Serena dependency, no writes, no database/cache
persistence. See
`docs/meridian-storage-and-file-inspector-contract-2026-08-31.md` in the
main repo for the design contract this package implements (Wave 0 + Wave 1).
The only network access either tool can ever make is a one-time,
explicitly-opted-in fetch of DuckDB's `excel` extension for `.xlsx` files
(see `inspect_tabular_file` below) — everything else is fully offline.

## Scope

This is a **read-only inspection facade over one file**, not a second
parser/index/database:

- **XML** — secure, hardened streaming parse and bounded structural summary
  (`inspect_file`).
- **JSON (generic)** — bounded structural summary and capped preview
  (`inspect_file`).
- **CSV / JSON (tabular) / XLSX** — bounded schema/sample/row-count
  inspection through DuckDB (`inspect_tabular_file`, item 28ef2710, Wave 1).
- **Out of scope here** (see the design doc): DOCX/OOXML (delegates to
  `meridian-docs` — this package never parses a DOCX ZIP itself) and output
  indexing/search (stays in `meridian-outputs`). XLS/XLSB/ODS are also out
  of scope — only `.xlsx` is supported, pending a real benchmark corpus
  justifying a Calamine/fastexcel compatibility adapter.

## Install

```bash
uvx --from /path/to/meridian-file-inspection meridian-file-inspection-mcp
```

Or add to your MCP client config:

```json
{
  "mcpServers": {
    "meridian-file-inspection": {
      "command": "uvx",
      "args": ["--from", "/path/to/extensions/meridian-file-inspection", "meridian-file-inspection-mcp"]
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `inspect_file` | Inspect exactly one local XML or JSON file and return a bounded, deterministic structural summary — never full file content |
| `inspect_tabular_file` | Inspect exactly one local CSV, JSON, or XLSX file's TABULAR shape (schema, bounded row sample, row count) through DuckDB — never full file content |

## Response envelope

```json
{
  "schema_version": "1.0.0",
  "source_ref": "redacted/portable/ref.xml",
  "format": "xml",
  "mime": "application/xml",
  "size_bytes": 1234,
  "source_sha256": "...",
  "parser_id": "lxml-xml-secure",
  "parser_version": "lxml-...",
  "result_hash": "...",
  "state": "complete",
  "shape": {},
  "bounds": {},
  "warnings": [],
  "errors": [],
  "provenance_ref": null
}
```

`state` is one of `complete` / `partial` / `failed` — a `partial` result is
still useful (it means a bound was hit mid-inspection) but must never be
treated as complete. Every failure mode reports a structured entry under
`errors`/`warnings` using one of six stable codes — this tool **never
raises** for a bad input file:

| Code | Meaning |
|------|---------|
| `unsupported` | Format not recognized (neither XML nor JSON magic bytes) |
| `limit_exceeded` | A bound (`max_bytes`/`max_depth`/`max_items`/timeout prescan) was hit |
| `malformed` | Content isn't valid XML/JSON, or isn't valid UTF-8 |
| `denied` | Path-policy violation (directory, symlink, outside `allowed_root`, secret-named file, not found/unreadable) **or** a DTD/entity was found in XML input |
| `timeout` | The soft wall-clock budget was exceeded mid-parse |
| `partial` | (as a `state`, not an error code) — some bound was hit but a partial summary is still returned |

`source_ref` is a **redacted, portable reference** — basename plus up to two
parent directory names — never the raw machine-local absolute path. Nothing
this tool returns is persisted by it; `provenance_ref` is always `null` — a
caller that wants to bind an inspection to a run/artifact passes this
envelope's `result_hash`/`source_sha256` to `meridian-outputs`
(`record_provenance` / `bind_artifact_provenance`) itself.

### `inspect_tabular_file` shape (CSV/JSON/XLSX)

Same envelope as above, with `format` one of `"csv"`/`"json"`/`"xlsx"`,
`parser_id` one of `"duckdb-csv"`/`"duckdb-json"`/`"duckdb-excel"`, and
`shape` containing:

```json
{
  "row_count": {"value": 1234, "exact": true},
  "column_count": 5,
  "columns": [{"name": "id", "type": "BIGINT"}],
  "truncated_columns": false,
  "sample_rows": [{"id": 1}],
  "truncated_sample": false
}
```

`row_count.exact` is `false` only when the count query itself timed out or
errored (`row_count.value` is then `null`) — an inexact count is never
presented as exact. Two additional error-code reasons apply only to
`inspect_tabular_file`:

| Reason | Meaning |
|--------|---------|
| `max_decompressed_bytes_exceeded` (`limit_exceeded`) | An `.xlsx` file's ZIP central directory declares a total uncompressed member size over `max_decompressed_bytes` — refused before any decompression, the zip-bomb guard |
| `xlsx_extension_unavailable` (`denied`) | DuckDB's `excel` core extension isn't cached locally and `allow_extension_network_install` wasn't set — refused rather than making an implicit network call |

DuckDB's `.xlsx` support lives in a separate extension that DuckDB will
otherwise fetch over the network on first use — the ONLY network access
either tool in this package can ever make, and only when explicitly opted
into via `allow_extension_network_install=True`. Pre-cache it once instead
(e.g. `pixi run python -c "import duckdb; duckdb.connect().execute('INSTALL excel')"`)
to keep `inspect_tabular_file` fully offline afterward.

## Security

This package exists specifically to make it safe to point an untrusted XML
or JSON file at an AI-driven tool. Non-negotiable guarantees:

1. **XXE / DTD hardening (see `xml_safe.py` for the full threat model).**
   Any document containing a DOCTYPE declaration anywhere in its bytes is
   rejected outright (`denied` / `dtd_disallowed`) before it reaches a real
   parser — the simplest, most conservative interpretation of "reject
   DTD/entities/external resolution" as one rule. As defense in depth, the
   underlying `lxml.etree.XMLParser` is ALSO configured with
   `resolve_entities=False`, `no_network=True`, `load_dtd=False`,
   `dtd_validation=False`, and `huge_tree=False` (never disable libxml2's
   own built-in hardening limits), and the parsed document's `docinfo` is
   checked again after parsing as a third, independent layer. XInclude and
   XSLT processing are never invoked anywhere in this package — those
   attack classes are structurally absent, not merely disabled.
   `lxml` is used instead of `defusedxml` deliberately: `defusedxml` is only
   ever a *transitive* dependency of `fpdf2` in the core repo's
   `pixi.lock` and is **not actually importable** in that environment
   (verified empirically before this choice was made), whereas `lxml` is
   already a direct dependency there and in `extensions/meridian-docs` —
   this package adds zero new dependency surface to the project.
2. **No secret-file reads.** A file whose basename matches a secret-shaped
   pattern (`.env*`, `*.key`, `*.pem`, `*secret*`, `*credential*`,
   `config.*`, etc. — see `inspector.py`'s `is_secret_path`, ported from
   `extensions/meridian-outputs/meridian_outputs/outputs_local.py`'s
   function of the same name) is refused (`denied` / `secret_path_excluded`)
   before the file is ever opened.
3. **Path policy.** Directories, symlinks (unless `allow_symlinks=True`),
   paths resolving outside an optional `allowed_root`, and missing/unreadable
   paths are all refused as `denied` before any content is read.
4. **Every bound is enforced, never advisory.** `max_bytes` is checked via
   `os.stat` before the file is opened. JSON nesting depth and container
   count are bounded by a single-pass bracket scan **before** the real
   `json.loads` parser ever runs (defends against a pathologically deep
   document overflowing Python's recursion limit or the C stack). XML
   depth/item/text bounds are enforced during a streaming `iterparse` walk
   that clears completed subtrees as it goes, so a huge-but-shallow document
   never fully materializes in memory. A soft wall-clock `timeout_seconds`
   budget is checked periodically during both parses.
5. **No writes, no network, no shell, no imports/includes, no directory
   walk, no database/cache persistence.** This package only ever
   `os.stat`s and opens-for-read the single path it was asked to inspect.
6. **Deterministic output.** Dict/set summaries are always emitted in sorted
   key order; `result_hash` is a SHA-256 over the canonical (sorted-key,
   no-timestamp) JSON encoding of `shape` — the same file inspected twice
   with the same bounds always produces the same `result_hash`.

## Capability registration

This is a **local-only, optional/degraded_ok** capability — it has no
tunnel, network, or Serena dependency, so its unavailability must never
block a session; a project's capability manifest (see
`meridian/capability_manifest.py` in the core repo, and
`tools/meridian_fallbacks/capability_manifest.json`'s
`fallback_chain_example` for a worked schema-valid entry) can declare it
with `availability_policy: "degraded_ok"`, e.g.:

```json
{
  "id": "local_file_inspection",
  "purpose": "Inspect one local XML/JSON file's bounded structural shape, without a tunnel or Serena dependency.",
  "required_tools": ["meridian-file-inspection:inspect_file"],
  "fallback_chain": [],
  "availability_policy": "degraded_ok"
}
```

The same `degraded_ok` posture applies to `inspect_tabular_file` — a
missing/unavailable DuckDB `excel` extension for `.xlsx` degrades that one
format (reported via `denied`/`xlsx_extension_unavailable`), never blocks
CSV/JSON tabular inspection or any other capability.

## Tests

```bash
pixi run python -m pytest tests/test_local_file_inspection.py tests/test_local_file_inspection_tabular.py -p no:xdist -q --timeout=60
```

Both test files live in the main repo's `tests/` directory (not under this
package) and import `meridian_file_inspection` directly via a `sys.path`
insertion rather than as a declared `pixi.toml` dependency — this package's
dependencies (`mcp`, `lxml`, `duckdb`) are already direct dependencies of
the root `meridian` pixi environment, so the real hardened parser (and, for
the tabular tests, the real DuckDB engine) can be exercised (including a
genuine malicious-shaped XXE/DTD fixture and a genuine zip-bomb-shaped XLSX
fixture) without adding anything to `pixi.toml`/`pixi.lock`.
