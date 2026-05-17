# Meridian

Your AI sessions don't remember each other.

Every Claude tab starts fresh. Parallel sessions duplicate work. Context fills up
and you start over from scratch. Meridian fixes this — a local server every session
connects to so they share goal state, see each other's task logs, and can resume
instantly from a compressed handoff.

## Install

```bash
# Requires pixi — https://prefix.dev/docs/pixi/overview
git clone https://github.com/yourusername/meridian
cd meridian
pixi run start       # dashboard at http://localhost:7878
```

Open `http://localhost:7878` to see the dashboard.

## Connect to Claude

### Claude Code (recommended)

Create `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "meridian": {
      "command": "pixi",
      "args": ["run", "python", "-m", "meridian", "--mcp"],
      "cwd": "/path/to/meridian"
    }
  }
}
```

Every Claude Code session in that project gets Meridian tools automatically.

### Claude Desktop

Add to `claude_desktop_config.json`:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "meridian": {
      "command": "pixi",
      "args": ["run", "python", "-m", "meridian", "--mcp"],
      "cwd": "/path/to/meridian"
    }
  }
}
```

Restart Claude Desktop. Your next chat has Meridian tools available.

## How it works

1. Start a session: `start_session(project_id=..., session_name="my-session")`
2. Read the goal, see what other sessions did: returned in the same call
3. Do work, log tasks: `log_task(..., description="built the auth endpoint", status="done")`
4. Before context fills up: `generate_handoff(project_id=...)` — writes a compressed file
5. New session reads the handoff and picks up exactly where you left off

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

[MSL-2.0](LICENSE) — free for individual personal use. Commercial use requires a
license. See LICENSE for details.
