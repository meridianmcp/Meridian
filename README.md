<p align="center">
  <img src="meridian/static/logo.svg" width="64" height="64" alt="Meridian">
</p>

# Meridian

**Claude Code has no memory between sessions. Meridian fixes that.**

Open-source MCP server for persistent AI session memory — shared task log,
pinned decisions, human-in-the-loop queue, and tiered handoffs. Works with
Claude Code, Cursor, Cline, Claude Desktop, or any MCP client.

[![License: MSL-1.0](https://img.shields.io/badge/license-MSL--1.0-blue)](LICENSE)
[![Tests](https://github.com/meridianmcp/Meridian/actions/workflows/test.yml/badge.svg)](https://github.com/meridianmcp/Meridian/actions/workflows/test.yml)
[![Docs](https://img.shields.io/badge/docs-docs.usemeridian.us-6c8fff)](https://docs.usemeridian.us)
[![Hosted](https://img.shields.io/badge/hosted-usemeridian.us-a78bfa)](https://usemeridian.us)
[![Neon](https://img.shields.io/badge/db-neon%20postgres-00e599)](https://neon.tech)

## Why Meridian

Every AI coding session boots blind. You re-explain the architecture, re-describe
the constraints, re-list what's been tried. When context fills up mid-task,
everything is lost. This is context debt — and it compounds.

Meridian gives your sessions shared memory. They see the same task log, the same
pinned decisions, the same goal state. When context fills up, a new session resumes
from a compressed handoff in seconds. No copy-paste, no re-explaining from scratch.

---

---

## What it is, in 30 seconds

A local MCP server every AI session connects to. They share goal state, see each
other's task log, and resume from a compressed handoff when context fills up.

Hosted tier at **[usemeridian.us](https://usemeridian.us)** — $20/mo, 7-day free trial.
Self-host the same product for free.

## Quickstart — binary (no Python required)

Download the single-file binary for your platform, double-click (or run from terminal), and the dashboard opens at `http://localhost:7878`.

| Platform | Download |
|---|---|
| Windows | [meridian.exe](https://github.com/meridianmcp/Meridian/releases/latest/download/meridian.exe) |
| macOS (Apple Silicon) | [meridian-mac-arm64](https://github.com/meridianmcp/Meridian/releases/latest/download/meridian-mac-arm64) |
| macOS (Intel) | [meridian-mac-x86](https://github.com/meridianmcp/Meridian/releases/latest/download/meridian-mac-x86) |

## Quickstart — from source

**Linux / macOS:**
```bash
git clone https://github.com/meridianmcp/Meridian
cd Meridian
./install.sh
pixi run start
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/meridianmcp/Meridian
cd Meridian
.\install.ps1
pixi run start
```

Dashboard opens at **http://localhost:7878**. Data persists in `./data/meridian.db`.

## Wire it into your AI client

### Claude Code

Drop a `.mcp.json` at your project root:
```json
{
  "mcpServers": {
    "meridian": {
      "command": "pixi",
      "args": ["run", "python", "-m", "meridian", "--mcp"],
      "cwd": "/absolute/path/to/Meridian"
    }
  }
}
```

### Cursor / Windsurf

Same JSON snippet — both clients read `.mcp.json` from the project root.

### Claude Desktop

Add the same `mcpServers` block to:
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

Restart Claude Desktop. New chats have Meridian tools.

### Hosted tier (no install)

```json
{
  "mcpServers": {
    "meridian": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://usemeridian.us/mcp"],
      "env": {"BEARER_TOKEN": "sk_meridian_..."}
    }
  }
}
```

Get your bearer token at [usemeridian.us/dashboard](https://usemeridian.us/dashboard) after sign-in.

## What you get

- **Dashboard** at `http://localhost:7878` — sessions, tasks, sprint board,
  swimlane timeline, HITL queue, pinned decisions.
- **MCP tools** — `start_session`, `log_task`, `claim_task`, `set_decision`,
  `pin_decision`, `request_hitl`, `generate_handoff`, plus 10 more.
- **Tiered handoffs** — L0/L1/L2 compression so a fresh session can resume in seconds.
- **Webhook intake** — push events from LangGraph / Autogen / custom agents into the same dashboard.
- **Works everywhere** — Claude Code, Claude Desktop, Cursor, Windsurf, LangGraph, custom.

## How it works

```
> start_session(project_id="meridian", session_name="feature-x")
  ✓ session registered · sprint loaded · 12 active tasks

> get_tasks(project_id="meridian", limit=5)
  [DONE]    backend / wire decisions_pinned table
  [PENDING] frontend / add notes vtab (claimed by session-2)

> claim_task(task_id="a1f3...")
  ✓ claimed — other sessions skip this one
```

State lives in `data/meridian.db` (SQLite) or a Postgres URL via `MERIDIAN_DB_URL`.
No cloud required for local use.

## Team coordination

Point `MERIDIAN_DB_URL` at a shared Postgres (Neon free tier works great). Every
teammate runs their own local Meridian against the same DB — instant shared
sessions, no Meridian server in the cloud.

## Auto-checkpoint with hooks

One command wires Claude Code and Codex to Meridian. Every session start injects
your project context automatically. Every session end snapshots completed work and
writes a delta handoff.

**Mac/Linux:**
```bash
curl -fsSL https://usemeridian.us/hooks.sh | bash
```

**Windows:**
```powershell
irm https://usemeridian.us/hooks.ps1 | iex
```

Prompts for your Meridian server URL (default `http://localhost:7878`) and your
project ID. Writes to `~/.claude/settings.json` (Claude Code) or
`~/.codex/config.toml` (Codex). After setup, every session automatically:

1. **On start** — calls `POST /hooks/session-start` → injects goal, sprint items,
   recent tasks, and pinned decisions into the session context via `additionalContext`.
2. **On stop** — calls `POST /hooks/stop` → runs `auto_capture` and writes a delta
   handoff so the next session resumes from where this one ended.

No more manual `start_session()` calls. No lost work when context fills.

## Hosted tier

| | Standard | Pro |
|---|---|---|
| **Price** | $20/mo | $49/mo (waitlist) |
| **Storage** | 1 GB included | 10 GB included |
| **Compute** | 2 CU · 100 hrs/mo | 4 CU · 300 hrs/mo |
| **Environments** | 1 | prod / staging / dev |
| **Bring your own Postgres** | ✓ | ✓ |
| **OAuth + email magic link** | ✓ | ✓ |
| **Extra storage** | $0.50 / GB-month | $0.50 / GB-month |
| **Support** | Email | Priority |

7-day free trial on Standard. Card required, no charge until day 8.

## License

[MSL-1.0](LICENSE) — free for local and internal use at any team size. Paid
license required if you host Meridian as a service for others. Converts to
MIT after 6 years.

For licensing questions: [hello@usemeridian.us](mailto:hello@usemeridian.us)

## Contributors

[![Contributors](https://contrib.rocks/image?repo=meridianmcp/Meridian)](https://github.com/meridianmcp/Meridian/graphs/contributors)
