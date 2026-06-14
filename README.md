<!-- mcp-name: io.github.meridianmcp/meridian -->
<p align="center">
  <img src="meridian/static/logo.svg" width="64" height="64" alt="Meridian">
</p>

# Meridian

**Claude Code has no memory between sessions. Meridian fixes that.**

Open-source MCP server for persistent AI session memory — shared task log,
pinned decisions, human-in-the-loop queue, and tiered handoffs. Works with
Claude Code, Cursor, Cline, Claude Desktop, or any MCP client.

[![GitHub Stars](https://img.shields.io/github/stars/meridianmcp/Meridian?style=social)](https://github.com/meridianmcp/Meridian)
[![License: MSL-1.0](https://img.shields.io/badge/license-MSL--1.0-blue)](LICENSE)
[![Tests](https://github.com/meridianmcp/Meridian/actions/workflows/test.yml/badge.svg)](https://github.com/meridianmcp/Meridian/actions/workflows/test.yml)
[![Docs](https://img.shields.io/badge/docs-docs.usemeridian.us-6c8fff)](https://docs.usemeridian.us)
[![Hosted](https://img.shields.io/badge/hosted-usemeridian.us-a78bfa)](https://usemeridian.us)
[![Neon](https://img.shields.io/badge/db-neon%20postgres-00e599)](https://neon.tech)
[![Server](https://img.shields.io/endpoint?url=https://usemeridian.us/status/server)](https://usemeridian.us)
[![MCP Tools](https://img.shields.io/endpoint?url=https://usemeridian.us/status/tools)](https://docs.usemeridian.us/mcp-tools)
[![Live Sessions](https://img.shields.io/endpoint?url=https://usemeridian.us/status/sessions)](https://usemeridian.us)

## Why Meridian

Every AI coding session boots blind. You re-explain the architecture, re-describe
the constraints, re-list what's been tried. When context fills up mid-task,
everything is lost. This is context debt — and it compounds.

Meridian gives your sessions shared memory. They see the same task log, the same
pinned decisions, the same goal state. When context fills up, a new session resumes
from a compressed handoff in seconds. No copy-paste, no re-explaining from scratch.

[![Meridian dashboard](docs/screenshots/01_dashboard.png)](https://usemeridian.us)

## What it is, in 30 seconds

A local MCP server every AI session connects to. They share goal state, see each
other's task log, and resume from a compressed handoff when context fills up.

**Two ways to run Meridian:**
- **Self-host** — free forever, any team size. Clone and run in 2 commands.
- **Hosted** at [usemeridian.us](https://usemeridian.us) — 30 days free (no card), then $20/mo Standard.

<!-- MERIDIAN:ANCHOR:START quickstart -->
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
<!-- MERIDIAN:ANCHOR:END quickstart -->

## Wire it into your AI client

### Claude Code

Drop a `.mcp.json` at your project root.

**Hosted (no install)** — generate an API key at [usemeridian.us/settings](https://usemeridian.us/settings):
```json
{
  "mcpServers": {
    "meridian": {
      "type": "http",
      "url": "https://usemeridian.us/mcp",
      "headers": { "Authorization": "Bearer sk_meridian_YOUR_KEY_HERE" }
    }
  }
}
```

**Self-host (from source):**
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

### claude.ai web (recommended for planning chat)

Use [dnakov/claude-mcp](https://github.com/dnakov/claude-mcp) — included as a submodule — to bridge claude.ai to your local Meridian server:

```bash
git clone --recurse-submodules https://github.com/meridianmcp/Meridian
```

1. Open `chrome://extensions` and enable **Developer mode**
2. Click **Load unpacked** and select `extensions/claude-mcp`
3. Click the extension icon and set the URL to `http://localhost:7878/mcp`

All 54+ Meridian tools (`checkpoint`, `log_task`, `pin_decision`, etc.) are now available directly in claude.ai planning chat. No copy-pasting session output.

### Hosted tier (no install)

Sign in at [usemeridian.us](https://usemeridian.us) → Settings → MCP client setup → Generate API key → Copy config.

Or manually:
```json
{"mcpServers":{"meridian":{"type":"http","url":"https://usemeridian.us/mcp","headers":{"Authorization":"Bearer sk_meridian_YOUR_KEY_HERE"}}}}
```

**claude.ai (browser)** users: install the [dnakov/claude-mcp](https://github.com/dnakov/claude-mcp) Chrome extension, then visit [usemeridian.us/install-mcp](https://usemeridian.us/install-mcp) for a step-by-step setup guide with one-click copy buttons.

Get your API key at [usemeridian.us/settings](https://usemeridian.us/settings) after sign-in. Free tier: 30 days, no card, full features.

<!-- MERIDIAN:ANCHOR:START features-list -->
## What you get

- **Dashboard** at `http://localhost:7878` — sessions, tasks, sprint board,
  swimlane timeline, HITL queue, pinned decisions.
- **MCP tools** — `start_session`, `log_task`, `claim_task`, `set_decision`,
  `pin_decision`, `request_hitl`, `generate_handoff`, and ~50 more.
- **Symbol-level parallel safety** — `claim_file` can claim a single class or
  function (parsed with `ast` / tree-sitter) so two sessions edit the same file
  safely; an overlapping claim is blocked with the free symbols listed.
- **Live work queue** — planners inject sprint items mid-run; executors pick them
  up at the next item boundary via a `board_change` signal, no interruption.
- **HITL recommended option** — `request_hitl` can flag a safe-default option the
  dashboard highlights; Enter submits it, number keys pick others.
- **GitHub hub** (hosted) — connect your repo once in Settings; sessions get `read_file`,
  `list_files`, `search_code`, `git_log`, `get_commit` injected automatically. No extra install.
- **Tiered handoffs** — L0/L1/L2 compression so a fresh session can resume in seconds.
- **Webhook intake** — push events from LangGraph / Autogen / custom agents into the same dashboard.
- **Works everywhere** — Claude Code, Claude Desktop, Cursor, Windsurf, LangGraph, custom.
<!-- MERIDIAN:ANCHOR:END features-list -->

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

Prompts for your Meridian server URL (default `http://localhost:7878`), then opens
your browser to connect this machine — no project ID or API token to paste. Writes
to `~/.claude/settings.json` (Claude Code) or `~/.codex/config.toml` (Codex). After
setup, every session automatically:

1. **On start** — calls `POST /hooks/session-start` → injects goal, sprint items,
   recent tasks, and pinned decisions into the session context via `additionalContext`.
2. **On stop** — calls `POST /hooks/stop` → runs `auto_capture` and writes a delta
   handoff so the next session resumes from where this one ended.

No more manual `start_session()` calls. No lost work when context fills.

<!-- MERIDIAN:ANCHOR:START pricing-table -->
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

30-day free trial · no card required
<!-- MERIDIAN:ANCHOR:END pricing-table -->

## License

[MSL-1.0](LICENSE) — free for local and internal use at any team size. Paid
license required if you host Meridian as a service for others. Converts to
MIT after 6 years.

For licensing questions: [hello@usemeridian.us](mailto:hello@usemeridian.us)

## Contributors

Built by [@ajc3xc](https://github.com/ajc3xc)
