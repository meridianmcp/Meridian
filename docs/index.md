# Meridian

**Shared memory for your AI sessions.**

[![GitHub](https://img.shields.io/github/stars/meridianmcp/Meridian?style=social)](https://github.com/meridianmcp/Meridian)
[![License](https://img.shields.io/badge/license-MSL--2.0-blue)](https://github.com/meridianmcp/Meridian/blob/main/LICENSE)

<div style="border:2px dashed #4a90d9;border-radius:8px;padding:20px 24px;margin:20px 0;text-align:center;background:rgba(74,144,217,0.05)">
  📹 <strong>What is Meridian? (90 sec)</strong> — video coming soon<br>
  <span style="font-size:0.85em;color:#888">Drop the YouTube URL below when ready</span>
  <!-- REPLACE WITH ACTUAL YOUTUBE URL BEFORE LAUNCH -->
  <!-- <iframe width="560" height="315" src="https://www.youtube.com/embed/VIDEO_ID" title="What is Meridian?" frameborder="0" allowfullscreen style="max-width:100%"></iframe> -->
</div>

---

## The Problem

Every AI coding session starts completely fresh. Open Claude Code in two terminals and they have absolutely no idea what the other is doing. Hit the context limit mid-task and your entire working memory evaporates. Run parallel sessions and they step on each other — duplicate work, conflicting edits, no coordination.

This isn't a Claude problem. It's a fundamental limitation of stateless sessions. And it gets worse with every wasted token.

<img src="screenshots/landing_problems.png" alt="Meridian landing page — the problem" style="max-width:100%;border-radius:8px;margin:12px 0">

## The Solution

Meridian is a local MCP server that gives all your Claude sessions a **shared persistent brain**.

<img src="screenshots/landing_hero.png" alt="Meridian dashboard — hero" style="max-width:100%;border-radius:8px;margin:12px 0">

Every session that connects to Meridian can:

- **Read the current goal** — what the project is trying to accomplish right now
- **Log tasks** — what it's doing, what it finished, what failed
- **See every other session's work** — no duplicate effort
- **Generate instant handoffs** — compressed context files that resume a new session in seconds

When the context window fills up, you can generate a handoff and start fresh. The new session will then read the file and pick up exactly where you left off -- no re-explaining required.

<img src="screenshots/landing_features.png" alt="Meridian features" style="max-width:100%;border-radius:8px;margin:12px 0">

## Key Features

| Feature | What it does |
|---------|-------------|
| `start_session` | Register this session, load full context in one call |
| `get_goal` | Read the shared north star, sprint, and version goal |
| `log_task` | Log progress — all sessions see it instantly |
| `claim_task` | Lock a task so two sessions can't double-work it |
| `generate_handoff` | Compress full context into a resumable file |
| `get_sprint_items` | See the sprint board — what's todo, in progress, done |
| `set_goal` | Update the shared goal — all sessions align instantly |
| `get_sessions` | See all active sessions and their last activity |

## Architecture

Two deployment modes — pick the one that fits you.

### Self-hosted (free)

```
┌─────────────────────────────────────────────────────────────┐
│                        Your machine                         │
│                                                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │ Claude Code │  │   Cursor /  │  │  Claude Desktop     │ │
│  │  (any term) │  │  Windsurf   │  │  (via .dxt install) │ │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘ │
│         │                │                    │            │
│         └────────────────┴────────────────────┘            │
│                          │ MCP stdio (local)               │
│                   ┌──────▼──────┐                          │
│                   │  Meridian   │  ← pixi run start        │
│                   │ MCP Server  │    or binary .exe / mac  │
│                   │   :7878     │                          │
│                   └──────┬──────┘                          │
│                          │                                 │
│            ┌─────────────┴──────────────┐                  │
│            ▼                            ▼                  │
│   ┌────────────────┐        ┌─────────────────────┐        │
│   │   SQLite DB    │   or   │  Postgres (Neon /   │        │
│   │  meridian.db   │        │  any compatible DB) │        │
│   └────────────────┘        └─────────────────────┘        │
│                                                             │
│  Features: persistent memory · task log · sprint board     │
│            HITL queue · handoff · hooks auto-checkpoint     │
└─────────────────────────────────────────────────────────────┘
```

### Hosted tier (usemeridian.us)

```
┌──────────────────────┐         ┌───────────────────────────┐
│     Your machine     │         │    usemeridian.us cloud   │
│                      │         │                           │
│  ┌────────────────┐  │  HTTPS  │  ┌─────────────────────┐  │
│  │  Claude Code   │  │ Bearer  │  │  Meridian Cloud     │  │
│  │  Cursor        ├──┼────────►│  │  MCP Server         │  │
│  │  Claude.ai     │  │  token  │  └──────────┬──────────┘  │
│  └────────────────┘  │         │             │             │
│                      │         │  ┌──────────▼──────────┐  │
│  ┌────────────────┐  │         │  │ Isolated Neon       │  │
│  │  Dashboard     │◄─┼────────►│  │ Postgres DB         │  │
│  │  (browser)     │  │         │  │ (per workspace)     │  │
│  └────────────────┘  │         │  └─────────────────────┘  │
└──────────────────────┘         │                           │
                                 │  Features: same as self-  │
                                 │  hosted + zero install,   │
                                 │  managed DB, team URLs,   │
                                 │  SLA, SSO (enterprise)    │
                                 └───────────────────────────┘
```

Zero local install — sign in at [usemeridian.us](https://usemeridian.us), copy your API token, add to MCP config.

## Dashboard

<img src="screenshots/demo_sessions.png" alt="Meridian dashboard — sessions view" style="max-width:100%;border-radius:8px;margin:8px 0">
<img src="screenshots/demo_queue.png" alt="Meridian dashboard — queue tab" style="max-width:100%;border-radius:8px;margin:8px 0">
<img src="screenshots/demo_hitl.png" alt="Meridian dashboard — HITL queue" style="max-width:100%;border-radius:8px;margin:8px 0">
<img src="screenshots/pricing.png" alt="Meridian pricing" style="max-width:100%;border-radius:8px;margin:8px 0">
<img src="screenshots/install_mcp.png" alt="Meridian MCP install guide" style="max-width:100%;border-radius:8px;margin:8px 0">

## Quick Install

=== "pixi (recommended)"
    ```bash
    git clone https://github.com/meridianmcp/Meridian
    cd Meridian
    pixi install
    pixi run start
    ```

=== "pip"
    ```bash
    git clone https://github.com/meridianmcp/Meridian
    cd Meridian
    pip install -e .
    python -m meridian
    ```

=== "Docker"
    ```bash
    git clone https://github.com/meridianmcp/Meridian
    cd Meridian
    docker compose up
    ```

Then add to your Claude Code `.mcp.json`:

```json
{
  "mcpServers": {
    "meridian": {
      "command": "pixi",
      "args": ["run", "python", "-m", "meridian", "--mcp"],
      "cwd": "/path/to/Meridian"
    }
  }
}
```

→ [Full quickstart guide](quickstart.md)

## Power Tools (Recommended Companions)

These MCP servers pair with Meridian to give your AI agents codebase context and safe file editing.

**Same JSON format everywhere — only the file path changes.**

=== "Claude Code"
    **File:** `.mcp.json` at project root (project-scoped), or `~/.config/claude/mcp.json` (global)

    ```json
    {
      "mcpServers": {
        "meridian": {
          "command": "pixi",
          "args": ["run", "python", "-m", "meridian", "--mcp"],
          "cwd": "/path/to/Meridian"
        },
        "text-editor": { "command": "uvx", "args": ["mcp-text-editor"] },
        "repomix": { "command": "npx", "args": ["-y", "repomix", "--mcp"] }
      }
    }
    ```

=== "Claude Desktop"
    **File:**

    - **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
    - **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

    ```json
    {
      "mcpServers": {
        "meridian": {
          "command": "pixi",
          "args": ["run", "python", "-m", "meridian", "--mcp"],
          "cwd": "/path/to/Meridian"
        },
        "text-editor": { "command": "uvx", "args": ["mcp-text-editor"] },
        "repomix": { "command": "npx", "args": ["-y", "repomix", "--mcp"] }
      }
    }
    ```

=== "Cursor"
    **File:** `.cursor/mcp.json` at project root

    ```json
    {
      "mcpServers": {
        "meridian": {
          "command": "pixi",
          "args": ["run", "python", "-m", "meridian", "--mcp"],
          "cwd": "/path/to/Meridian"
        },
        "text-editor": { "command": "uvx", "args": ["mcp-text-editor"] },
        "repomix": { "command": "npx", "args": ["-y", "repomix", "--mcp"] }
      }
    }
    ```

=== "Windsurf"
    **File:** `~/.codeium/windsurf/mcp_config.json`

    ```json
    {
      "mcpServers": {
        "meridian": {
          "command": "pixi",
          "args": ["run", "python", "-m", "meridian", "--mcp"],
          "cwd": "/path/to/Meridian"
        },
        "text-editor": { "command": "uvx", "args": ["mcp-text-editor"] },
        "repomix": { "command": "npx", "args": ["-y", "repomix", "--mcp"] }
      }
    }
    ```

=== "claude.ai (browser)"
    Uses SSE transport — no JSON config file needed. Install the [dnakov/claude-mcp](https://github.com/dnakov/claude-mcp) Chrome extension, then:

    1. Click the extension icon → **Add server**
    2. **Name:** `meridian`
    3. **URL:** `http://localhost:7878/mcp/sse`

    The extension discovers all Meridian tools via the SSE handshake. For the hosted tier, use `https://usemeridian.us/mcp/sse` with your API token.

    → [Full setup guide](https://usemeridian.us/install-mcp)

**What each adds:**

| Tool | What it does | Install |
|------|-------------|---------|
| [mcp-text-editor](https://github.com/tumf/mcp-text-editor) | Safe line-oriented file patching with conflict detection | `uvx mcp-text-editor` |
| [Repomix](https://repomix.com) | Packs your entire codebase into AI-friendly context | `npx repomix --mcp` |
| [Desktop Commander](https://github.com/wonderwhy-er/DesktopCommanderMCP) | Terminal, process management, file system | Already in submodule |


## Screenshots

The Meridian dashboard runs in your browser at `localhost:7878`. All data stays on your machine (or your dedicated Neon DB on the hosted tier).

<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:16px 0">
  <div>
    <p><strong>Dashboard — project overview</strong></p>
    <img src="screenshots/01_dashboard.png" alt="Meridian dashboard" style="border-radius:6px;border:1px solid #2a2d35;width:100%">
  </div>
  <div>
    <p><strong>Live sessions tab</strong></p>
    <img src="screenshots/02_live_tab.png" alt="Live sessions" style="border-radius:6px;border:1px solid #2a2d35;width:100%">
  </div>
  <div>
    <p><strong>Goal + sprint board</strong></p>
    <img src="screenshots/03_goal_tab.png" alt="Goal tab" style="border-radius:6px;border:1px solid #2a2d35;width:100%">
  </div>
  <div>
    <p><strong>Queue — task log</strong></p>
    <img src="screenshots/04_queue_tab.png" alt="Queue tab" style="border-radius:6px;border:1px solid #2a2d35;width:100%">
  </div>
</div>

Try the live demo at [usemeridian.us/demo](https://usemeridian.us/demo) — no sign-in needed.

## Hosted Tier

Don't want to run your own server? [usemeridian.us](https://usemeridian.us) is a hosted version — sign in with Google or GitHub, get a managed Neon Postgres database, and connect over HTTPS.

**$20/month** — 7-day free trial, no commitment.

→ [Hosted tier guide](hosted.md)
