# MCP tool-surface freshness

Meridian does not delete or rewrite a client’s private cache. Cache locations
and cache formats belong to the host application and differ across operating
systems and releases. The portable contract is protocol-level:

1. Meridian computes a deterministic tool-schema revision from the canonical
   tool names, descriptions, input schemas, and annotations.
2. HTTP `initialize` responses advertise that revision and
   `capabilities.tools.listChanged: true`.
3. HTTP `tools/list` responses include the revision and tool count in
   `result._meta["meridian/toolManifest"]`.
4. Local stdio initialization advertises the same `listChanged` capability.
5. A client that receives `notifications/tools/list_changed` must re-run
   `tools/list`; a client that cannot receive notifications must reconnect or
   poll and compare the revision.

This deliberately avoids vendor-specific cache paths. A cache purge is a
last-resort host operation, not something the Meridian server can safely do.

## Client behavior

| Client | Transport | Expected refresh path | Important limitation |
|---|---|---|---|
| Claude Code | HTTP or stdio | Honors MCP `list_changed` and refreshes capabilities; otherwise use `/mcp` or reconnect | A failed refresh should retain the previous known-good surface |
| Claude Desktop | Usually stdio | Restart/reconnect the MCP server or the desktop app if the host does not process the notification | Its local cache is host-owned; do not assume a Windows path on Linux/macOS |
| Cursor | HTTP or stdio | Reconnect/reload the MCP server, then verify with a fresh `tools/list` | Client cache behavior can vary by Cursor release |
| Codex app connector | Hosted connector catalog | Fully exit/reopen Codex so its connector catalog is rebuilt; then verify the live tool surface | Codex’s app catalog is separate from Meridian’s MCP server and cannot be invalidated by Meridian code |

## Deterministic diagnosis

Use the repository diagnostic without printing credentials:

```powershell
pixi run python scripts/diagnose_tool_surface.py
```

The diagnostic compares the production health/version and authoritative
`/tools` manifest. It does not claim that a client has refreshed merely because
the server is healthy. After reconnecting a client, verify that the expected
tools are callable in that client; a server-side manifest is not proof of the
client’s in-memory registry.

For a stale Codex app catalog, close the Codex application completely and
reopen it. Do not hand-edit or delete unknown cache files as part of normal
Meridian operation. If a local cache must be quarantined during diagnosis,
move it to a timestamped backup so it can be restored.

## Scope and security

The revision contains tool schemas, not credentials or project data. It is a
freshness signal, not an authorization mechanism. Tool calls must still pass
the normal Meridian authentication, project-scope, capability, and write-lock
checks.
