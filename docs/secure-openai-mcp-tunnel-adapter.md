# OpenAI Secure MCP Tunnel adapter (optional)

Meridian's own permanent tunnel (`meridian --tunnel`) is the primary, hosted
transport that lets Claude, Cursor, and other local clients reach a private
local MCP server through `usemeridian.us`. OpenAI publishes a **separate**
mechanism — the **Secure MCP Tunnel** — that lets ChatGPT, Codex, and
Responses API workflows reach a private local/stdio MCP server through
OpenAI's own infrastructure instead of Meridian's relay.

This adapter is an **optional, additive** layer that lets a project describe
and diagnose an OpenAI Secure MCP Tunnel configuration using the same shape
Meridian already uses for its own tunnel diagnostics — without replacing or
hard-depending on Meridian's existing transport, and without this adapter
ever holding a live connection or credential itself.

!!! note "Scope of this item (45049071)"
    This adapter ships **config validation + diagnostics only**. It does not
    spawn a process, open a socket, or call any OpenAI API. See
    [What this item does NOT do](#what-this-item-does-not-do) below.

## Why a separate adapter

| | Meridian tunnel | OpenAI Secure MCP Tunnel adapter |
|---|---|---|
| Relay | `usemeridian.us` (Meridian-hosted) | OpenAI-hosted |
| Client | `meridian --tunnel` | not spawned by Meridian |
| Consumers | Claude, Cursor, local MCP clients, cross-client routing | ChatGPT, Codex, Responses API workflows |
| Module | `meridian/tunnel_client.py`, `meridian/routes/tunnel.py` | `meridian/openai_tunnel_adapter.py` |
| State enum | `tunnel_client.SlotState` | `openai_tunnel_adapter.OpenAITunnelState` |

The two transports are deliberately kept in **separate enums and separate
diagnostics sections** — see [Diagnostics shape](#diagnostics-shape) below —
so a caller can never conflate "Meridian's tunnel is up" with "the OpenAI
adapter is configured."

## Config schema

`meridian.openai_tunnel_adapter.normalize_config(raw)` validates and
normalizes a raw config dict. `None` or `{}` normalizes to the fully-disabled
default rather than raising.

```json
{
  "enabled": false,
  "tunnel_id": null,
  "transport": null,
  "command": null,
  "url": null,
  "allowed_tools": [],
  "approval_policy": "always_ask",
  "tenant_id": null,
  "project_id": null,
  "env": null
}
```

| Field | Type | Notes |
|---|---|---|
| `enabled` | bool | Default `false`. When `true`, `transport` and the matching `command`/`url` become required. |
| `tunnel_id` | str \| null | An **opaque external reference** configured out-of-band with OpenAI — never a raw credential. Screened for secret-shaped values (see [Secret handling](#secret-handling)). |
| `transport` | `"stdio"` \| `"http"` | Required when `enabled`. |
| `command` | list[str] \| str | stdio only. Coerced the same way `tunnel_plugins._coerce_command` coerces a custom plugin's launch command. Required for `enabled` + `stdio`. |
| `url` | str | http only. Must be `http://` or `https://`. Required for `enabled` + `http`. |
| `allowed_tools` | list[str] | Default `[]` — **secure by default**: an empty list means no tools are allowed, never "all tools." |
| `approval_policy` | `"always_ask"` \| `"auto_approve_allowlisted"` \| `"never"` | Default `"always_ask"`. |
| `tenant_id` / `project_id` | str \| null | Meridian-side scope this adapter instance is associated with, for diagnostics only. |
| `env` | dict[str, str] \| null | stdio spawn environment overrides. |

Any schema or safety violation raises `OpenAITunnelAdapterError` —
deterministic rejection, never a partial/best-guess normalization, mirroring
`meridian.capability_manifest.normalize_capability`'s own contract.

## Secret handling

Every string field is screened for a secret-shaped value (API key / bearer
token / password) using the **same regex** `capability_manifest.py` already
uses — never a weaker duplicate. A `tunnel_id` or credential must be kept
**external**: pass a reference (e.g. an environment variable name your local
process resolves separately), never the literal secret.

This screen is deliberately **narrower** than
`capability_manifest._check_no_secrets_or_local_paths`: this adapter's raw
runtime config is expected to be **local-machine-only** (a local stdio
launcher command, a local/loopback HTTP URL), not `capability_manifest`'s
project-shared, multi-machine DB state — so, unlike a capability manifest
entry, an absolute local command path is **not** rejected here. Only
`default_capability_entry()` (below) touches actual shared, multi-machine
state, and it delegates unchanged to
`capability_manifest.normalize_capability`, which keeps applying its own
full secret-**and**-path screen.

## Opting in via the capability manifest

A project opts into this adapter through the **existing**, generic
capability-manifest mechanism (`set_capability_manifest` /
`get_capability_manifest`) — no new opt-in mechanism was added.

```python
from meridian.openai_tunnel_adapter import default_capability_entry

entry = default_capability_entry()
# {
#   "id": "openai_secure_mcp_tunnel",
#   "purpose": "Optional OpenAI Secure MCP Tunnel transport for ChatGPT/Codex/Responses API clients, alongside Meridian's own tunnel",
#   "required_tools": ["openai_secure_mcp_tunnel"],
#   "fallback_chain": ["meridian_tunnel"],
#   "availability_policy": "optional",
#   "verification_command": None,
#   "provenance": None,
# }
```

`availability_policy` defaults to `"optional"` with `fallback_chain:
["meridian_tunnel"]` — this adapter must never make a project
non-executable when unavailable; Meridian's own tunnel is always the working
fallback. Pass `**overrides` to `default_capability_entry(...)` to adjust
any field before normalization (e.g. a stricter policy for a project that
truly requires it).

`meridian/capability_manifest.py` and `meridian/capability_contract.py`
required **no code changes** for this: both already handle any declared
capability id generically (see `meridian.code_intel_receipt` for the
established precedent of a feature module layering on top of the generic
manifest schema without modifying it).

## Diagnostics shape

`build_diagnostics(config, reported_status=None)` returns an
`OpenAITunnelDiagnostics` (mirrors the shape of
`tunnel_client.SlotDiagnostics.to_dict()`):

```json
{
  "state": "not_configured",
  "detail": "adapter is not configured or is disabled",
  "transport": null,
  "tenant_id": null,
  "project_id": null,
  "allowed_tool_count": 0,
  "approval_policy": null
}
```

`state` is one of `OpenAITunnelState`: `not_configured`, `configured`,
`connecting`, `connected`, `degraded`, `disconnected`, `error`. Without a
`reported_status` argument the result can only ever be `not_configured` or
`configured` — this module never probes anything live. `reported_status`
(`{"state": ..., "detail": ...}`) is the injectable seam a future live-health
integration uses (mirrors `capability_contract`'s own `availability_checker`
pattern); an unrecognized `state` value degrades to `error` rather than
silently passing through.

`combined_diagnostics(tenant_id, openai_config=..., meridian_tunnel_active=...)`
composes **both** transports' state into one payload, explicitly namespaced:

```json
{
  "tenant_id": "t1",
  "meridian_tunnel": {"active": true},
  "openai_tunnel": {"state": "configured", "...": "..."}
}
```

## Integration points

- **`meridian/openai_tunnel_adapter.py`** — the module itself: config
  schema, lifecycle enum, diagnostics builder, capability entry template.
- **`meridian/tunnel_client.py`** — `openai_tunnel_adapter_snapshot()` reads
  local config from the `MERIDIAN_OPENAI_TUNNEL_CONFIG` environment variable
  (JSON object) only — never a committed file, never sent to Meridian's own
  relay — and builds a diagnostics snapshot. Diagnostics only; it does not
  change what `run_tunnel()` actually spawns or connects.
- **`meridian/routes/tunnel.py`** — `POST
  /tunnel/openai/diagnostics/{tenant_id}` composes the OpenAI adapter's
  diagnostics with Meridian's own tunnel-socket state for a tenant. The
  caller supplies `openai_tunnel_config` (and optionally `reported_status`)
  in the request body each call — this endpoint does **not** persist
  anything server-side.
- **`meridian/tunnel_plugins.py`** — `openai`, `openai-tunnel`, and
  `openai_secure_mcp_tunnel` are reserved custom-plugin names
  (`is_reserved_custom_name`), so a user cannot define a local custom plugin
  that collides with this adapter's identity.

## What this item does NOT do

Explicitly out of scope, per the sprint item's own restriction ("do not
change production credentials or connections in this item"):

- No real connection to OpenAI's Secure MCP Tunnel infrastructure.
- No process spawn, no socket, no OpenAI API call.
- No new database column or table — nothing is persisted server-side by the
  diagnostics route. `openai_tunnel_config`/`reported_status` are supplied
  fresh by the caller on every request.
- No live health probing — `reported_status` is an injectable seam for a
  **future** item to wire, not something this module computes itself.
- No CLI flag to actually spawn/launch an OpenAI-side tunnel process.

## Follow-ups (explicitly out of scope for this item)

- Server-side persistence of a tenant's OpenAI tunnel config (would need a
  new migrated DB column/table on both the SQLite and Postgres paths, per
  this repo's existing migration-parity requirements).
- A live health-probing integration that supplies a real `reported_status`
  to `build_diagnostics`/`combined_diagnostics`.
- A `meridian --tunnel --openai-status` (or dashboard card) surface that
  actually renders `tunnel_client.openai_tunnel_adapter_snapshot()`.
- Real process/connection wiring once OpenAI's Secure MCP Tunnel client
  contract is confirmed against a pinned version (mirrors this repo's
  existing "pin the package/version, don't infer the contract from generic
  web results" convention for third-party MCP integrations).
