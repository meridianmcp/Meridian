# Connecting claude.ai in the browser

Use Meridian directly inside claude.ai — no local install, no terminal. The [dnakov/claude-mcp](https://github.com/dnakov/claude-mcp) Chrome extension bridges the gap between your browser tab and any MCP server via SSE.

---

## Quick start (hosted tier)

1. **Install the extension** — [dnakov/claude-mcp on GitHub](https://github.com/dnakov/claude-mcp). Install the Chrome extension from the repository's releases page.

2. **Sign in to Meridian** — Go to [usemeridian.us](https://usemeridian.us) and sign in with Google or GitHub. Your session must be active in the same browser.

3. **Add the server** — Click the extension icon → **Add server**:
    - **Name:** `meridian`
    - **URL:** `https://usemeridian.us/mcp`

4. **Authorise** — The extension opens an auth popup from Meridian. This is expected — sign in if prompted. The connection completes automatically once the session cookie is confirmed.

5. **Open claude.ai** — All Meridian tools (`start_session`, `log_task`, `generate_handoff`, etc.) are now available in your claude.ai chat.

---

## Quick start (self-hosted)

1. Install the extension (same as above).

2. Start your local Meridian server:
```bash
pixi run start
# or: python -m meridian
```

3. Add the server in the extension:
    - **Name:** `meridian`
    - **URL:** `http://localhost:7878/mcp`

4. Open claude.ai. The extension discovers all tools via the SSE handshake.

!!! note "No auth popup for self-hosted"
    The auth popup only appears for the hosted tier at usemeridian.us. Self-hosted connections go directly to your local server with no additional login step.

---

## Troubleshooting

**"No tools loaded" / connection hangs**

- Make sure Meridian is running (`pixi run start` locally, or check your hosted account is active)
- For hosted: verify you are signed in to usemeridian.us in the same browser window *before* clicking Connect
- Try removing the server and adding it again after signing in

**Auth popup doesn't close**

- Sign in with Google or GitHub on the popup, then close it — the connection retries automatically
- If it still hangs, reload the claude.ai tab and try again

**Tools appear but calls fail**

- Check the extension logs for 401 / 403 errors — your session may have expired
- For hosted: your API token may need to be regenerated (Settings → API token)

---

## What you get

Once connected, claude.ai has access to all Meridian MCP tools:

| Tool | What it does |
|------|-------------|
| `start_session` | Load full project context in one call |
| `log_task` | Log what the session just did |
| `generate_handoff` | Create a resumable context file |
| `request_hitl` | Surface a blocking question to the dashboard |
| `get_sprint_items` | See the sprint board |
| `pin_decision` | Record an architectural or product decision |

→ [Full MCP tool reference](mcp-tools.md)

---

## FAQ

**Does this work on Firefox?**

The dnakov/claude-mcp extension is Chrome/Chromium only. Firefox support is not planned.

**Can I use a different MCP extension?**

Any browser extension that supports the [MCP SSE transport](https://spec.modelcontextprotocol.io) will work. The URL to add is the same: `https://usemeridian.us/mcp` (hosted) or `http://localhost:7878/mcp` (self-hosted).

**Is my data safe?**

For the hosted tier, all MCP calls go over HTTPS to your isolated Neon Postgres database. The claude.ai tab never has direct access to your DB — all operations go through the Meridian API layer.
