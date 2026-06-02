# Meridian

**Shared memory for your AI sessions.**

[![GitHub](https://img.shields.io/github/stars/meridianmcp/Meridian?style=social)](https://github.com/meridianmcp/Meridian)
[![License](https://img.shields.io/badge/license-MSL--2.0-blue)](https://github.com/meridianmcp/Meridian/blob/main/LICENSE)

---

## The Problem

Every AI coding session starts completely fresh. Open Claude Code in two terminals and they have absolutely no idea what the other is doing. Hit the context limit mid-task and your entire working memory evaporates. Run parallel sessions and they step on each other — duplicate work, conflicting edits, no coordination.

This isn't a Claude problem. It's a fundamental limitation of stateless sessions. And it gets worse with every wasted token.

## The Solution

Meridian is a local MCP server that gives all your Claude sessions a **shared persistent brain**.

Every session that connects to Meridian can:

- **Read the current goal** — what the project is trying to accomplish right now
- **Log tasks** — what it's doing, what it finished, what failed
- **See every other session's work** — no duplicate effort
- **Generate instant handoffs** — compressed context files that resume a new session in seconds

When the context window fills up, you can generate a handoff and start fresh. The new session will then read the file and pick up exactly where you left off -- no re-explaining required.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Your machine                        │
│                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ Claude Code  │  │  Claude      │  │   Cursor /   │  │
│  │  terminal 1  │  │  Desktop     │  │  Windsurf    │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  │
│         │                 │                 │           │
│         └─────────────────┴─────────────────┘           │
│                           │ MCP stdio                    │
│                    ┌──────▼───────┐                     │
│                    │   Meridian   │                     │
│                    │  MCP Server  │                     │
│                    │  :7878       │                     │
│                    └──────┬───────┘                     │
│                           │                             │
│                    ┌──────▼───────┐                     │
│                    │  SQLite DB   │  ← or Postgres      │
│                    │  meridian.db │     (Neon / any)    │
│                    └──────────────┘                     │
└─────────────────────────────────────────────────────────┘
```

For **hosted tier** (usemeridian.us), Meridian runs in the cloud and each workspace gets an isolated Neon Postgres database. Your Claude sessions connect over HTTPS with a bearer token — zero local install required.

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

## Hosted Tier

Don't want to run your own server? [usemeridian.us](https://usemeridian.us) is a hosted version — sign in with Google or GitHub, get a managed Neon Postgres database, and connect over HTTPS.

**$20/month** — 7-day free trial, no commitment.

→ [Hosted tier guide](hosted.md)
