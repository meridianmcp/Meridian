# Quickstart

Get Meridian running and connected to Claude Code in under 10 minutes.

---

## Prerequisites

- A Claude Code, Claude Desktop, Cursor, or Windsurf installation
- No Python required for binary installs (Windows/Linux/macOS tabs below)

---

## Step 1 — Install Meridian

=== "Windows .exe"

    Download the latest binary from the [Releases page](https://github.com/meridianmcp/Meridian/releases/latest).

    Look for `meridian.exe` under **Assets**.

    1. Download `meridian.exe`
    2. Double-click to run — Windows Firewall may prompt, click **Allow**
    3. Your browser opens automatically at **http://localhost:7878**

    !!! tip
        To run from a specific folder (for data persistence), open a terminal and run:
        ```
        meridian.exe
        ```

=== "Linux binary"

    Download the latest binary from the [Releases page](https://github.com/meridianmcp/Meridian/releases/latest).

    Look for `meridian-linux` under **Assets**.

    ```bash
    # Download (replace X.Y.Z with the latest version)
    curl -Lo meridian https://github.com/meridianmcp/Meridian/releases/latest/download/meridian-linux
    chmod +x meridian
    ./meridian
    # → Meridian running on http://127.0.0.1:7878
    ```

=== "macOS arm64"

    Download the latest binary from the [Releases page](https://github.com/meridianmcp/Meridian/releases/latest).

    Look for `meridian-mac-arm64` under **Assets**.

    ```bash
    # Download (replace X.Y.Z with the latest version)
    curl -Lo meridian https://github.com/meridianmcp/Meridian/releases/latest/download/meridian-mac-arm64
    chmod +x meridian
    # macOS may quarantine the binary — remove the quarantine flag:
    xattr -d com.apple.quarantine meridian 2>/dev/null || true
    ./meridian
    # → Meridian running on http://127.0.0.1:7878
    ```

=== "pixi (from source)"

    [pixi](https://pixi.sh) installs all dependencies in a locked environment. Best for contributors or power users.

    **Requires Python 3.11+**

    ```bash
    git clone https://github.com/meridianmcp/Meridian
    cd Meridian
    pixi install
    pixi run start
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
# → {"status": "ok", "version": "...", "db": "sqlite"}
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

### Browser clients (Claude and ChatGPT)

For **hosted Meridian**, no extension is required:

1. Open Claude **Customize > Connectors** or ChatGPT **Settings > Apps**
2. Add `https://usemeridian.us/mcp`
3. Complete the OAuth prompt

For **self-hosted localhost** browser access, use a local Claude SSE bridge or
put Meridian behind a public HTTPS URL first.

See the full guide here: [Browser Connector](browser-connector.md)

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

!!! tip "Set `MERIDIAN_HUMAN_ID` to identify yourself"
    Meridian attributes sessions and tasks to a human so the dashboard can group
    work per person. Set this env var before starting the server (or add it to
    your `.env` file):

    ```bash
    export MERIDIAN_HUMAN_ID=alice   # macOS / Linux
    set MERIDIAN_HUMAN_ID=alice      # Windows cmd
    $env:MERIDIAN_HUMAN_ID="alice"   # PowerShell
    ```

    If omitted, Meridian falls back to `$USER` / `$USERNAME` / hostname.  
    You can also pass it explicitly per session:

    ```
    start_session(project_id="...", session_name="...", human_id="alice")
    ```

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

## Casual user path (no CLI required)

Not running Claude Code? Meridian works entirely from the browser via the
[Browser Connector](browser-connector.md).

**1 — Connect on claude.ai**

Open **Customize > Connectors** and add:
- Hosted: `https://usemeridian.us/mcp`
- Self-hosted: your public HTTPS URL (see [self-hosting](self-hosting.md))

**2 — Start a session in chat**

```
start_session(project_id="<your-project-id>", session_name="planning-2026-06")
get_context_block(project_id="<your-project-id>", mode="chat")
```

**3 — Work inline with MCP tools**

Log decisions and notes as you chat:

```
pin_decision(project_id="...", title="Use psycopg3", body="asyncpg has DLL issues on Windows", category="TECHNICAL")
log_task(session_id="...", project_id="...", description="Reviewed auth refactor plan")
add_note(session_id="...", project_id="...", title="Design notes", body="...")
```

**4 — Save progress before ending**

```
checkpoint(session_id="...", project_id="...")
```

**5 — Hand off to the next session**

```
generate_handoff(project_id="...", mode="delta")
```

Paste the handoff at the start of the next chat. That's the full lightweight loop —
no local server, no terminal, full session continuity.

---

## Lightweight workflow (hosted tier)

Skip the install entirely. The hosted tier at [usemeridian.us](https://usemeridian.us) gives you a
managed Meridian instance — sign in with Google or GitHub, paste the `.mcp.json` snippet from your
welcome email, and you're coordinating in minutes.

For browser-based clients (Claude.ai, ChatGPT), the [Browser Connector](browser-connector.md) needs
no config files at all — just add your MCP URL in the client's connector settings.

**Minimal session pattern — paste at the start of every chat:**

```
start_session(project_id="<your-id>", session_name="what-youre-doing")
```

**Log work as you complete it:**

```
log_task(session_id="...", project_id="...", description="Fixed auth redirect bug")
```

**Before context fills or before ending:**

```
checkpoint(session_id="...", project_id="...")
```

**Start the next session with full context:**

```
generate_handoff(project_id="...", mode="delta")
```

Paste the handoff block at the top of the next chat. That's the whole workflow — four tools, no
local server, full session continuity.

---

## Troubleshooting

### MCP tools not showing up in Claude Code

1. Make sure the Meridian server is running (`./meridian` or `pixi run start`)
2. Verify the `cwd` path in your `.mcp.json` is correct and uses absolute path
3. Restart Claude Code after editing `.mcp.json`
4. Check Claude Code's MCP panel (⚙️ → MCP) for error messages

### Server won't start — port already in use

```bash
# Change the port (binary)
MERIDIAN_PORT=7879 ./meridian

# Change the port (pixi)
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
