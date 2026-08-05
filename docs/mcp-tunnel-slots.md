# MCP Tool Surface (Tunnel Slots)

`meridian --tunnel` runs a fixed **3-slot model**: three local proxy ports,
each fronting one code-intelligence backend, relayed to the hosted server so
Claude Desktop, Claude Code, Cursor, and Codex all reach the same tools no
matter which client connects.

| Slot | Local connector name | Default backend | Server route |
|------|----------------------|------------------|---------------|
| `fs` | `filesystem` | built-in filesystem server | `/fs` |
| `code` | `codebase-memory` | `codebase-memory-mcp` | `/code` |
| `extract` | `serena` | **Serena** (`serena-agent` via `uvx`) | `/extract` |

Each slot's command is a plain data value (`meridian/tunnel_plugins.py`'s
`BUILTIN_PLUGINS`), overridable per tenant from the dashboard — swapping a
backend is a config change, not a redeploy.

## Code-extractor slot: Serena is the current default

The `extract` slot's symbol-level code-intelligence backend (`find_symbol`,
`replace_symbol_body`, `find_referencing_symbols`, …) is **Serena**, launched
headless via:

```
uvx --from serena-agent serena start-mcp-server \
    --context claude-code --open-web-dashboard false --project {repo_path}
```

This replaced the older `mcp-server-code-extractor` PyPI package (commit
`1428f1b`). `--open-web-dashboard false` suppresses Serena's own browser
dashboard popup on every tunnel (re)start — see `serena_pool.py`'s
`ensure_serena_headless`, the canonical enforcement point for every place a
Serena command is built or resolved (default, tenant override, or custom
slot), not just the extract slot's exact default string.

### Runtime configuration resolution (`run_tunnel`)

`meridian/tunnel_client.py`'s `_resolve_extract_slot_command(command,
repo_path)` deterministically classifies the extract slot's configured
`command` into what actually launches:

- **No command configured**, an explicit copy of the current default, or a
  configured command that can't be coerced into a runnable list at all
  (malformed/stale config) → the **current default**, Serena, via a
  per-`repo_path` `SerenaDaemonPool`. Previously a missing/empty command
  silently fell back to `_resolve_extractor_inner_cmd()`, which launches the
  now-obsolete `mcp-server-code-extractor` package — correct before the
  built-in default moved to Serena, stale ever since. This is the defect
  fixed by `9d9a92cc`.
- **An explicit, valid override** (including, deliberately, a tenant who
  chose to keep running the legacy extractor package by hand) → that command,
  wrapped in `mcp-proxy` like any other custom slot command.
  `tunnel_plugins.resolve_plugins()` already flags a command matching a
  *superseded* default as `stale_override` (the dashboard's "newer default
  available" badge); `run_tunnel` now also prints that warning at the CLI so
  it's visible without opening the dashboard.

### Diagnostics

`_extract_slot_diagnostics(kind, override, repo_path)` reports, at tunnel
startup, exactly what will run for this slot — **package name**, the Python
runtime executing the tunnel process, the resolved `cwd` (`--project`), and
whether `uvx` is resolvable on this machine (dependency preflight) — without
ever printing the raw command or argv, since a custom slot override can embed
a secret (mirrors `serena_pool._command_hash`'s non-reversible-diagnostics
rationale for the same reason).

## Claude Desktop

Claude Desktop does not read a project-local `.mcp.json`; it reads
`claude_desktop_config.json` directly. Meridian's local config writer
(`_install_mcp_json` in `tunnel_client.py`) only ever emits
`http://127.0.0.1:<port>/mcp` proxy URLs for the tunnel slots — **no
generated config path emits a raw Serena launch command** to Claude Desktop
or any other client. Point Claude Desktop's `mcpServers` entry at the local
proxy port (or the hosted relay URL, for a Pro tunnel) the same way Claude
Code's `.mcp.json` does; the actual backend behind that port is resolved by
`run_tunnel` using the logic above, so a config generated for Desktop is
correct by construction rather than needing its own separate code path.

## Codex / Cursor

Cursor's `.cursor/mcp.json` is updated by the same `_install_mcp_json` writer
as Claude Code's `.mcp.json`, with the identical connector-key/URL shape (see
`_tunnel_mcp_entries`) — the extract-slot fix above applies uniformly, since
it lives in `run_tunnel`'s command resolution, upstream of any per-client
config file. Codex connects to the same tunnel proxy ports via its own
`mcpServers`-style config (see `AGENTS.md`'s "Connect to Meridian" section);
no client-specific code path exists for the extract slot's command
resolution.
