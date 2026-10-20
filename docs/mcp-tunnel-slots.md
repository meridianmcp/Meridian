# MCP Tool Surface (Tunnel Slots)

`meridian --tunnel` runs **eleven built-in slots**. Each slot is one local proxy
port that fronts one MCP backend and is relayed to the hosted server, so Claude
Desktop, Claude Code, Cursor, and Codex all reach the same tools no matter which
client connects. Four pre-allocated custom slots (`p0`-`p3`, ports 8814-8817)
carry user-defined plugins and are not built-in.

| Slot | Built-in plugin | Tool prefix | Local port | Server route | Default state | Default backend |
|------|-----------------|-------------|------------|--------------|---------------|-----------------|
| `fs` | `filesystem` | `filesystem` | 8808 | `/fs` | on (core) | built-in filesystem server (`@modelcontextprotocol/server-filesystem`) |
| `code` | `code-intel` | `codebase` | 8809 | `/code` | on (core) | `codebase-memory-mcp` (auto-installed) |
| `extract` | `code-extractor` | `extractor` | 8810 | `/extract` | on (core) | **Serena** (`serena-agent` via `uvx`) |
| `ppt` | `powerpoint` | `powerpoint` | 8811 | `/ppt` | off (opt-in) | `powerpoint-mcp` via `uvx` |
| `word` | `word` | `word` | 8812 | `/word` | off, retired | `docx-mcp` via `uvx` |
| `dc` | `desktop-commander` | `desktop-commander` | 8813 | `/dc` | off (opt-in) | Desktop Commander |
| `docs` | `meridian-docs` | `meridian-docs` | 8818 | `/docs` | off (opt-in) | `meridian-docs-mcp` via `uvx` |
| `zotero` | `zotero-mcp` | `zotero-mcp` | 8819 | `/zotero` | off (opt-in) | `zotero-mcp` via `uvx` |
| `outputs` | `meridian-outputs` | `meridian-outputs` | 8820 | `/outputs` | off (opt-in) | `meridian-outputs-mcp` via `uvx` |
| `debug` | `mcp-debugger` | `mcp-debugger` | 8821 | `/debug` | off (opt-in) | `@debugmcp/mcp-debugger` via `npx` |
| `latex` | `meridian-latex` | `meridian-latex` | 8822 | `/latex` | off (opt-in) | `meridian-latex` (`@meridianmcp/mcp`) via `npx` |

"Tool prefix" is the `SLOT_DISPLAY_NAMES` value in `meridian/routes/tunnel.py`
that namespaces each slot's tools (for example, `filesystem__read_file`). The
built-in list is `BUILTIN_PLUGINS` in `meridian/tunnel_plugins.py`; `fs`, `code`,
and `extract` are core and on by default, and `word` is retired and forced off.

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

### Remote shell and verification: pinned locally

The server never sends a shell string to the tunnel client. `run_cmd` is refused
(`status: "refused"`), and nothing is spawned for it.

`run_verification` sends only the project id. The client runs the command pinned
for that project in its own pins file, and reports the command it ran back to the
server. The server's `test_cmd` value is informational and is not executed.

The pins file is `verification_commands.json`, in the local-runner state folder
(`~/.meridian/local_runner/`, or `MERIDIAN_LOCAL_RUNNER_STATE_DIR` when set). It
lives only on the machine that runs the tunnel and is never synced.

```json
{
  "5787cc92-ba7d-4788-b17c-28ab7938b839": {
    "cmd": "pixi run test",
    "cwd": "C:\\Users\\you\\Documents\\Meridian\\repository"
  }
}
```

`cmd` is required, non-empty, and at most 2000 characters. `cwd` is optional.
A missing project, a malformed file, or an invalid entry pins nothing, and
`run_verification` then returns `not_configured` without running anything.

### Local policy: this machine decides what runs and what writes

The hosted side chooses which plugins run and on which ports. It never supplies a
command, an environment, or a switch that starts a shell or a write path. Those
come from `local_policy.json` in the local-runner state folder, which is never
synced and missing or invalid means the locked-down defaults:

```json
{
  "allow_tunnel_writes": false,
  "enable_shell_slot": false,
  "builtin_commands": {"code-extractor": {"command": ["uvx", "..."], "env": {}}},
  "custom_plugins": {"my-plugin": {"command": ["node", "server.js"], "env": {}}}
}
```

- `allow_tunnel_writes` (default `false`): tools that change files or stored state
  are refused for tunnel requests on every slot. That covers the filesystem write
  tools (`write_file`, `edit_file`, `create_directory`, `move_file`), Serena's
  edit tools, the word, docs, and outputs writers, and codebase-memory's index
  writers. A refused call returns HTTP 403 with a JSON-RPC error. Set it to `true`
  to allow them.
- `enable_shell_slot` (default `false`): the exec slots run programs on this
  machine, so they start only when this is `true`. Those are `desktop-commander`,
  `mcp-debugger`, and `meridian-latex`. Serena's `execute_shell_command` is refused
  too. Enabling any of them from the hosted dashboard has no effect here.
- `builtin_commands`: an explicit local command for a built-in slot. The hosted
  config's command is ignored.
- `custom_plugins`: a custom plugin runs only when its name has a command here. A
  custom plugin defined only in the hosted config is logged and not started.

Only a literal `true` turns a switch on. The packages are pinned so `npx` cannot
silently upgrade them: `@modelcontextprotocol/server-filesystem@2.0.0` and
`mcp-proxy@6.7.20`.

### Key and credential files, and secret values, never leave through the tunnel

Two layers protect the tunnel, and neither depends on the hosted side behaving:

- **Refused by name.** A request whose arguments name a key or credential file is
  refused with a 403 before it reaches the local proxy. Names covered: `.env` and
  `.env.*` (but not `.env.example`, `.env.sample`, `.env.template`, `.env.dist`),
  `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519`, `*.pem`, `*.key`, `*.p12`, `*.pfx`,
  `*.jks`, `*.keystore`, `*.kdbx`, `*.ppk`, `.npmrc`, `.netrc`, `.pypirc`,
  `.git-credentials`, `.envrc`, `secrets.json`, `credentials.json`, the Meridian
  token and policy files, and `.git/config`. This applies to read tools, to
  `resources/read` URIs, and to every path argument.
- **Resolved on the filesystem slot.** A symlink or junction whose target is a key
  file is refused even when its own name is innocent.
- **Redacted by value.** Anything the tunnel sends back has credential-looking
  values replaced with `[redacted by the Meridian tunnel]`. That covers PEM private
  key blocks, Meridian tokens (`sk_meridian_…`), AWS, GitHub, Slack, and OpenAI-style
  keys, and `password`, `secret`, `api_key`, and `token` assignments. JSON, SSE, and
  plain-text bodies are all handled.

Limits: refusal is by name, so a key pasted into an ordinary file is caught only if
its value matches one of the redaction patterns. Symlinks are resolved on the
filesystem slot only. Search results from Serena or the filesystem server can still
contain text from a file with an innocent name.

### Served roots: credential and token folders are refused

The filesystem slot never serves a root that is, contains, or sits inside a
credential or token folder. This covers `filesystem_roots` from the server, the
local root list, and the repo-path fallback. The refused folders are:

- `~/.meridian` and the local-runner state folder
- `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.azure`, `~/.kube`, `~/.docker`
- `~/.config/gh`, `~/.config/gcloud`, `~/.config/op`
- `%APPDATA%\Zotero`, the Zotero profile folder that holds the bridge's preference lines
- `%APPDATA%\GitHub CLI`, `%APPDATA%\gcloud`

Containment is checked in both directions. A root that contains one of these
folders, such as the home folder itself, is refused too. A refused root is
logged and dropped. If the check cannot resolve a path, it refuses that root.

### Connection auth, keepalive, and request handling

- The client sends its bearer token in the `Authorization` header. A WebSocket that
  puts the token in the URL is refused, and its token is never logged. Release
  ordering matters: ship the client that sends the header before the server change
  goes live, or older clients cannot connect.
- The hosted keepalive refreshes the session of every connected tunnel, including one
  that has made no tool call for a long run (4b698ea5). It touches only tenants whose
  project database is already open, so it never opens a new connection.
- Each request on a socket is served on its own task, so a slow tool call no longer
  holds up the others. A socket close gives in-flight work 5 seconds before it is
  cancelled.

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

Cursor's user-global MCP config is `~/.cursor/mcp.json`. The `meridian` entry
that `scripts/meridian_connect.py` merges (`_configure_cursor`) goes there, and
the dashboard's MCP client list (`meridian/static/dashboard-settings.ts`) names
the same path.

The tunnel client's `_install_mcp_json` writer (`_mcp_json_paths` in
`meridian/tunnel_client.py`) injects the same connector entries as Claude Code's
`.mcp.json`, with the identical connector-key/URL shape (see
`_tunnel_mcp_entries`). It writes `<cwd>/.mcp.json` (created if absent) and
touches `<cwd>/.cursor/mcp.json` only when that project-root file already
exists; it never creates one. The extract-slot fix above applies uniformly,
since it lives in `run_tunnel`'s command resolution, upstream of any per-client
config file. Codex connects to the same tunnel proxy ports via its own
`mcpServers`-style config (see `AGENTS.md`'s "Connect to Meridian" section);
no client-specific code path exists for the extract slot's command
resolution.
