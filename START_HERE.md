# Meridian — Start Here
<!-- WEDGE HANDOFF v1 — paste this into any new session to boot context -->
<!-- If context is large: run /compact first, then paste the remainder. -->
<!-- For a minimal cold-start: generate_handoff(project_id="...", mode="starter") -->

**If you are Claude Code or Codex (executor):**
Read `data/meridian-build_handoff.md` in full, then call:
```
start_session(project_id="5787cc92-ba7d-4788-b17c-28ab7938b839", session_name="<describe-what-youre-doing>", human_id="adam", client="claude_code")
```
Then continue from the pending sprint items in L1 of the handoff.

---

**If you are a planning chat (claude.ai / Claude Desktop):**
Call this MCP tool to load strategic context:
```
generate_handoff(project_id="5787cc92-ba7d-4788-b17c-28ab7938b839", mode="planner")
```
That gives you: sprint state, pending items in plain English, open HITLs, key decisions.
No need to read the full handoff file.

---

## Quick Commands

```bash
pixi run start              # local server → localhost:7878/dashboard
pixi run test               # test suite (target: 545+)
pixi run mcp                # start Meridian as MCP server (stdio)
pixi run text-editor-mcp    # start mcp-text-editor (uvx, safe file patching)
pixi run repomix-mcp        # start Repomix MCP (npx, codebase context)
pixi run install-companions # install repomix globally via npm (one-time)
pixi run launch             # launch morning: unset SITE_PASSWORD + health checks
```

## Full MCP Config (all pixi-managed, paste into .mcp.json or claude_desktop_config.json)

```json
{
  "mcpServers": {
    "meridian":    { "command": "pixi", "args": ["run", "mcp"],             "cwd": "/path/to/Meridian" },
    "text-editor": { "command": "pixi", "args": ["run", "text-editor-mcp"], "cwd": "/path/to/Meridian" },
    "repomix":     { "command": "pixi", "args": ["run", "repomix-mcp"],     "cwd": "/path/to/Meridian" }
  }
}
```

Run `pixi install` once. Run `pixi run install-companions` once for repomix.

## Using claude.ai (browser) via dnakov/claude-mcp extension

The extension uses SSE transport (not stdio). Point it at the SSE endpoint:

```
URL: http://localhost:7878/mcp/sse
```

In the extension popup: add server, set URL to `http://localhost:7878/mcp/sse`, leave command/args empty.
The extension auto-discovers all Meridian MCP tools via the SSE handshake.

For hosted tier (usemeridian.us), use: `https://usemeridian.us/mcp/sse` with your API token.

---
*Meridian · meridian-build · 5787cc92 · START_HERE.md is the wedge; data/meridian-build_handoff.md is the full context.*
