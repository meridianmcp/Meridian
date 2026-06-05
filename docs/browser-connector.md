# Connecting Meridian from browser AI clients

Use Meridian from browser-based AI clients without giving up the shared session memory, sprint board, or HITL queue. The setup path depends on the client:

- **claude.ai** can connect directly to hosted Meridian with native connectors; use an SSE bridge only for self-hosted local servers
- **ChatGPT** uses a custom connector backed by a remote MCP server; no Chrome extension is required

---

## claude.ai

Use Meridian directly inside claude.ai. Hosted Meridian uses Claude's native connectors flow, so there is no local install, no terminal, and no browser extension required. If you are self-hosting Meridian on `localhost`, use an SSE bridge to expose that local server to claude.ai.

## Video walkthrough

<iframe width="100%" height="400" src="https://www.youtube.com/embed/dxX1Nwi72lI" frameborder="0" allowfullscreen></iframe>

---

## Quick start (hosted tier)

1. **Sign in to Meridian** - Go to [usemeridian.us](https://usemeridian.us) and sign in with Google or GitHub.

2. **Open claude.ai connectors** - In claude.ai, go to **Settings** -> **Connectors** -> **Add custom**.

3. **Add Meridian** - Paste the hosted MCP server details:
    - **Name:** `meridian`
    - **URL:** `https://usemeridian.us/mcp`

4. **Authorize** - Claude opens Meridian's OAuth flow. Approve the connection and sign in if prompted.

5. **Start chatting** - Meridian tools (`start_session`, `log_task`, `generate_handoff`, etc.) are now available in your claude.ai chat.

---

## Quick start (self-hosted)

1. Start your local Meridian server:

```bash
pixi run start
# or: python -m meridian
```

2. In claude.ai, use a browser-based SSE bridge for your local server:

<details>
<summary>Self-hosted only: install the Chrome extension bridge</summary>

- Install [dnakov/claude-mcp](https://github.com/dnakov/claude-mcp) from the project's releases page.
- Open the extension and choose **Add server**.
- Add your local Meridian instance:
  - **Name:** `meridian`
  - **URL:** `http://localhost:7878/mcp`

</details>

3. Open claude.ai. The bridge discovers all tools via the SSE handshake.

!!! note "No auth popup for self-hosted"
    The auth popup only appears for the hosted tier at usemeridian.us. Self-hosted connections go directly to your local server with no additional login step.

---

## ChatGPT

ChatGPT connects to Meridian as a **custom connector** backed by a **remote MCP server**. Hosted Meridian is the easiest path because it already exposes a public MCP endpoint, and you do not need any browser extension for this flow.

### Setup

1. Open ChatGPT and sign in.
2. Enable Developer mode or custom connectors if your workspace requires it.
3. Add Meridian as a custom connector and set the MCP URL to `https://usemeridian.us/mcp`.
4. Complete the authorization flow if ChatGPT prompts for sign-in or approval.

### Video walkthrough

<iframe width="100%" height="400" src="https://www.youtube.com/embed/U5pUUpOy5H4" frameborder="0" allowfullscreen></iframe>

!!! note "Remote MCP only"
    ChatGPT connects to remote MCP servers. A local URL such as `http://localhost:7878/mcp` will not connect directly from ChatGPT. If you are self-hosting Meridian, put it behind a secure public URL or tunnel first.

Depending on your ChatGPT plan or workspace, you may need developer mode enabled or admin approval before custom connectors are available. OpenAI updates those requirements over time, so check the latest guidance here:

- [Apps in ChatGPT](https://help.openai.com/en/articles/11487775-connectors-in-chatgpt)
- [Developer mode and MCP apps in ChatGPT](https://help.openai.com/en/articles/12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta)

---

## Troubleshooting

**"No tools loaded" / connection hangs**

- Make sure Meridian is running (`pixi run start` locally, or check your hosted account is active)
- For hosted: reconnect from claude.ai **Settings** -> **Connectors** and confirm the URL is `https://usemeridian.us/mcp`
- For self-hosted: verify the browser extension is installed and pointing at `http://localhost:7878/mcp`

**Auth popup doesn't close**

- Complete the Meridian OAuth flow in the popup, then return to claude.ai
- If it still hangs, reconnect Meridian from **Settings** -> **Connectors**

**Tools appear but calls fail**

- For self-hosted: check the extension logs for 401 / 403 errors
- For hosted: your API token may need to be regenerated (Settings -> API token)

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

-> [Full MCP tool reference](mcp-tools.md)

---

## FAQ

**Does this work on Firefox?**

Hosted Meridian works anywhere claude.ai supports native connectors. The optional dnakov/claude-mcp bridge for self-hosted local servers is Chrome/Chromium only.

**Can I use a different MCP extension?**

For self-hosted local Meridian, any browser extension that supports the [MCP SSE transport](https://spec.modelcontextprotocol.io) will work. The URL to add is `http://localhost:7878/mcp`.

**Is my data safe?**

For the hosted tier, all MCP calls go over HTTPS to your isolated Neon Postgres database. The claude.ai tab never has direct access to your DB - all operations go through the Meridian API layer.
