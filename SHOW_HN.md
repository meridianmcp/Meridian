# Show HN: Meridian – open-source MCP coordination layer for AI coding sessions, with auto-checkpoint hooks

**[usemeridian.us](https://usemeridian.us)** · **[github.com/meridianmcp/Meridian](https://github.com/meridianmcp/Meridian)**

---

## The problem

Every AI coding session boots blind. You re-explain the architecture, re-describe the constraints, re-list what's been tried. When context fills up mid-task, everything is lost. Multiple sessions working the same codebase stomp each other.

This is context debt. It compounds.

## What I built

Meridian is an open-source MCP server that gives every AI session shared persistent memory. Claude Code, Cursor, Codex — they all connect to the same local server and see:

- The same task log (what was done, by which session, when)
- The same pinned decisions ("use psycopg3 not asyncpg")  
- The same sprint board (what's claimed, what's pending, what's done)
- A compressed handoff so a fresh session resumes in seconds

When context fills up, `checkpoint()` snapshots progress and writes a delta handoff. The next session reads it and continues — no re-explaining.

## I built this using itself

Meridian was built with Meridian. Every sprint item you see in the live demo was tracked via its own MCP tools. Multiple parallel Claude Code sessions worked the same codebase simultaneously — each claiming their own tasks atomically, each logging their work, none stomping each other.

The `/demo` at usemeridian.us shows a real multi-session build, not a toy example.

## Auto-checkpoint hooks (shipped this week)

One command wires Claude Code and Codex to auto-checkpoint on every session end.

**Mac/Linux:**
```bash
curl -fsSL https://usemeridian.us/hooks.sh | bash
```

**Windows:**
```powershell
irm https://usemeridian.us/hooks.ps1 | iex
```

This writes a `SessionStart` hook that injects your project context into every new Claude Code session — no manual `start_session()` call needed. A `Stop` hook snapshots completed work and generates a delta handoff before the session closes. Zero config after the initial `project_id` prompt.

## Install

**Binary (no Python needed):** Download from [releases](https://github.com/meridianmcp/Meridian/releases) — `meridian.exe` (Windows) or `meridian-mac-arm64` (Apple Silicon). Double-click. Dashboard opens at `localhost:7878`.

**From source:**
```bash
git clone https://github.com/meridianmcp/Meridian && cd Meridian
pixi run start   # installs deps, starts server
```

## vs. Anthropic Managed Agents

Managed Agents is cloud-only, Claude-only, and closed source. Meridian:
- Runs locally — data stays on your machine
- Works with Claude Code, Codex, Cursor, Claude Desktop, any MCP client
- Is open source (MSL-1.0) — self-host for free forever
- Works offline (SQLite default, Postgres optional)

The MCP protocol is the interface; Meridian is the coordination layer underneath.

## What's in the box

**21 MCP tools:** `start_session` · `log_task` · `checkpoint` · `pin_decision` · `request_hitl` · `generate_handoff` · `claim_task` · and more

**Dashboard** at `localhost:7878` — sessions, sprint board, swimlane timeline, HITL queue, pinned decisions, activity log

**Human-in-the-loop queue** — surface blocking questions to a human without halting the session

**Multi-session coordination** — atomic task claiming, stale-claim auto-release, dependency enforcement

**Hosted tier** — managed Postgres, OAuth, magic-link login at usemeridian.us ($20/mo, 30-day free trial, no card required)

## Status

v1.0.0-alpha. 539 tests. The hosted tier is live with open signups. Self-hosted works today.

The README has a 30-second quickstart. The `/demo` shows a real project — the Meridian build itself, coordinated by Meridian.

Happy to answer questions about the MCP protocol design, the dogfood workflow, or why I chose psycopg3 over asyncpg on Windows.

---

*Built in ~6 weeks. Used daily. Launched on the same system it was built with.*
