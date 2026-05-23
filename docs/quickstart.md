# Quickstart

Get Meridian running and connected to Claude Code in under 10 minutes.

---

## Prerequisites

- **Python 3.11+** (check: `python --version`)
- One of: pixi, pip, or Docker
- A Claude Code, Claude Desktop, Cursor, or Windsurf installation

---

## Step 1 — Install Meridian

=== "pixi (recommended)"

    [pixi](https://pixi.sh) is the fastest way to install Meridian with all dependencies locked.

    ```bash
    git clone https://github.com/meridianmcp/Meridian
    cd Meridian
    pixi install
    ```

    Start the server:
    ```bash
    pixi run start
    # → Meridian running on http://127.0.0.1:7878
    ```

=== "pip"

    ```bash
    git clone https://github.com/meridianmcp/Meridian
    cd Meridian
    pip install -e ".[full]"
    python -m meridian
    # → Meridian running on http://127.0.0.1:7878
    ```

=== "Docker Compose"

    ```bash
    git clone https://github.com/meridianmcp/Meridian
    cd Meridian
    docker compose up
    # → Meridian running on http://localhost:7878
    ```

    Data is persisted in `./data/meridian.db` via volume mount.

Confirm it's running:
```bash
curl http://localhost:7878/health
# → {"status": "ok", "version": "1.9.0", "db": "sqlite"}
```

Open the dashboard: **http://localhost:7878**

---

## Step 2 — Create your first project

In the dashboard, click **New Project** and give it a name (e.g. `my-project`).

Or via the MCP tools (after Step 3):
```
create_project(name="my-project")
```

---

## Step 3 — Connect your AI client

### Claude Code

Add to `.mcp.json` in your project root (or `~/.claude/mcp.json` globally):

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

!!! tip
    Replace `/absolute/path/to/Meridian` with the actual path where you cloned the repo.
    On Windows use forward slashes: `C:/Users/you/Meridian`.

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

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

### Cursor

Add to `.cursor/mcp.json` in your project root:

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

### Windsurf

Add to `~/.codeium/windsurf/mcp_config.json`:

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

---

## Step 4 — Your first coordinated session

Start a session in Claude Code. Meridian tools will be available automatically.

Paste this into your first Claude Code message:

```
start_session(
  project_id="<your-project-id>",
  session_name="my-first-session"
)
get_goal(project_id="<your-project-id>")
```

!!! info "Finding your project ID"
    Open the Meridian dashboard at http://localhost:7878, select your project,
    and copy the ID from the URL or project settings.

---

## Step 5 — Use Meridian in your workflow

Once connected, add these calls to your Claude prompts:

**Start of session:**
```
start_session(project_id="...", session_name="feature-auth-fix")
```

**Log progress:**
```
log_task(session_id="...", project_id="...",
  description="Fixed JWT refresh token bug — tokens now invalidated on rotation",
  status="done")
```

**When context is filling up:**
```
generate_handoff(project_id="...")
```
Then start a new Claude Code session and paste the handoff file.

**See what other sessions are doing:**
```
get_tasks(project_id="...", limit=20)
```

---

## Troubleshooting

### MCP tools not showing up in Claude Code

1. Make sure the Meridian server is running (`pixi run start`)
2. Verify the `cwd` path in your `.mcp.json` is correct and uses absolute path
3. Restart Claude Code after editing `.mcp.json`
4. Check Claude Code's MCP panel (⚙️ → MCP) for error messages

### Server won't start — port already in use

```bash
# Change the port
MERIDIAN_PORT=7879 pixi run start
```

Update your `.mcp.json` if you use a non-default port (MCP stdio mode doesn't use HTTP, so port only matters for the dashboard).

### Database error on startup

Meridian creates `./data/meridian.db` automatically. If you see permission errors:

```bash
mkdir -p data && chmod 755 data
pixi run start
```

### Windows PATH too long error

This happens after many restarts on Windows. Fix:

```bash
# Run from a fresh terminal (not nested inside pixi)
python -m meridian
```

### Can't find project ID

Open http://localhost:7878, click your project, and look at the URL:
`http://localhost:7878/dashboard#project-<uuid>` — the UUID is your project ID.

Or use the MCP tool:
```
get_project_by_name(name="my-project")
```

### Postgres connection failing

Set `MERIDIAN_DB_URL` to your Postgres connection string:

```bash
export MERIDIAN_DB_URL="postgresql://user:pass@host/dbname"
pixi run start
```

Meridian auto-detects Postgres vs SQLite from the URL scheme.
