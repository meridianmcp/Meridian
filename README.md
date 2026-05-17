# Meridian — Multi-Session Claude Coordinator

A local MCP server that gives multiple Claude sessions a shared persistent
brain. Goal state, task log, session registry, and context handoff files —
so you can run parallel sessions without losing context or repeating yourself.

## What it solves

- Claude sessions are isolated. Each tab or terminal knows nothing about the others.
- Context fills up and dies. You lose everything mid-project and start over.
- Parallel sessions on the same project means manually syncing state.
- Switching between projects means re-loading context from scratch every time.

Meridian fixes all four. Every session connects to the same local server.
They share goal state, see each other's task logs, and generate compressed
handoff files so new sessions resume with full context instantly.

**Architecture principle:** The chat interface is stateless. Meridian is the
source of truth. Any session — Desktop, Code, terminal, or browser tab —
hydrates from Meridian on connect. No manual context pasting.

## Current version: v1.0.0 (166 tests passing)

- MCP tools: `create_project`, `register_session`, `get_goal`, `set_goal`,
  `set_north_star`, `set_sprint`, `log_task`, `get_tasks`, `get_sessions`,
  `generate_handoff`, `enqueue_claude_task`, `claim_task`, `release_task`,
  `list_projects`, `get_project_by_name`, `start_session`
- FastAPI HTTP server on port 7878
- SQLite at `~/.meridian/meridian.db`
- XML-tagged goal output with prompt caching hints
- GOAL.md bidirectional file sync
- First-run setup wizard, project switcher
- IP attribution PDF export (SHA-256 tamper-evident)
- PyInstaller single-file exe (run `pixi run build-exe`)
- License: MSL-2.0 (free for individual use, paid for team/shared use)

## Quick start

```bash
pixi run start        # FastAPI on port 7878
pixi run test         # 25 tests
pixi run demo         # two-session coordination demo
```

## Claude Desktop / Code MCP config

```json
{
  "mcpServers": {
    "meridian": {
      "command": "pixi",
      "args": ["run", "python", "-m", "meridian", "--mcp"],
      "cwd": "C:/Users/13144/Documents/Meridian/repository"
    }
  }
}
```

## Privacy

**Free tier:** runs entirely on your machine. No server, no telemetry, no accounts. We physically cannot see your data because it never leaves your computer.

**Paid/hosted tier:** your data lives on our server. We do not sell it. We do not train models on it — Meridian calls AI APIs, it is not an AI model, and task logs are coordination data not training data. Full export and deletion available at any time. Your SQLite database is an open file format you can take anywhere.

**Enterprise/self-hosted:** runs on your infrastructure. We never have access.

Meridian shows managers what shipped, not how many hours someone worked. No productivity scores, no per-developer rankings, no surveillance features. The developer is the customer.

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the full version plan and free/paid split.

- **v0.2.0** — Dashboard + project-scoped chat, HITL queue, WebSocket push
- **v0.3.0** — claim/release task locking, session health, watch_goal
- **v0.4.0** — Named project configs, multi-project workspace
- **v1.0.0** — Tauri desktop app, system tray, worker dispatch panel

## License

Meridian Source License 1.0 — free for individual personal use.
Commercial use requires a license. See LICENSE for details.
