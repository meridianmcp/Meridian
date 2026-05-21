# Meridian

Your AI sessions don't remember each other.

Every Claude tab starts fresh. Parallel sessions duplicate work. Context fills up
and you lose everything. Meridian fixes this — a local server every session connects
to so they share goal state, see each other's task logs, and can resume instantly
from a compressed handoff.

## What you get

- **Dashboard** at `http://localhost:7878` — live sessions, task queue, sprint board,
  goal state, rewind timeline
- **MCP tools** for Claude: `start_session`, `log_task`, `generate_handoff`, `claim_task`
- **SQLite** by default; swap in Postgres for teams sharing one brain
- Works with **Claude Code, Claude Desktop, Cursor, Windsurf**

## Install

### Option A — single executable (Windows, no Python required)

Download `meridian.exe` from [Releases](https://github.com/ajc3xc/Meridian/releases).
Double-click. Dashboard opens automatically at `http://localhost:7700`.

### Option B — from source (all platforms)

```bash
# Requires pixi — https://prefix.dev
git clone https://github.com/ajc3xc/Meridian
cd Meridian
pixi run start       # dashboard at http://localhost:7878
```

## Connect to Claude

### Claude Code (recommended)

Create `.mcp.json` in your project root:

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

Every Claude Code session in that project gets Meridian tools automatically.

### Claude Desktop

Add to your Claude Desktop config:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

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

Restart Claude Desktop. Your next chat has Meridian tools available.

## How it works

1. Session starts: `start_session(project_id=..., session_name="my-session")` — returns
   the current goal, recent task log, and active sprint in one call
2. Do work, log tasks: `log_task(..., description="built auth endpoint", status="done")`
3. Before context fills: `generate_handoff(project_id=...)` — compressed file captures
   everything; a new session reads it and picks up exactly where you left off
4. Parallel sessions? Each one calls `claim_task` to atomically lock a work item —
   no duplicated effort, no stepping on each other

State lives in a local SQLite file. No accounts, no cloud, no sync required.

## Privacy

**Free tier (this repo):** runs entirely on your machine. No telemetry, no accounts.
We physically cannot see your data — it never leaves your computer.

**Paid/hosted tier (future):** your data lives on our server. We do not sell it.
We do not train models on it. Full export and deletion available at any time.

**Enterprise/self-hosted:** runs on your infrastructure. We never have access.

Meridian shows what shipped, not how many hours someone worked. No surveillance
features, no productivity scores, no per-developer rankings. The developer is the
customer.

## License

[MSL-2.0](LICENSE) — free for personal use. Commercial use requires a license.
See LICENSE for details.
