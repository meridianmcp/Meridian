<!-- mcp-name: io.github.ajc3xc/meridian -->
<p align="center">
  <img src="meridian/static/logo.svg" width="72" height="72" alt="Meridian">
</p>

<h1 align="center">Meridian</h1>

<p align="center"><strong>Claude Code has no memory between sessions. Meridian fixes that.</strong></p>

<p align="center">
  Open-source MCP server for persistent AI session memory — shared task log,<br>
  pinned decisions, human-in-the-loop queue, and tiered handoffs.<br>
  Works with Claude Code, Cursor, Cline, Claude Desktop, or any MCP client.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MSL--1.0-blue" alt="License: MSL-1.0"></a>
  <a href="https://github.com/meridianmcp/Meridian/actions/workflows/test.yml"><img src="https://github.com/meridianmcp/Meridian/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  <a href="https://docs.usemeridian.us"><img src="https://img.shields.io/badge/docs-docs.usemeridian.us-6c8fff" alt="Docs"></a>
  <a href="https://usemeridian.us"><img src="https://img.shields.io/badge/hosted-usemeridian.us-a78bfa" alt="Hosted"></a>
  <a href="https://github.com/meridianmcp/Meridian/releases/latest"><img src="https://img.shields.io/github/v/release/meridianmcp/Meridian?color=00d4aa&label=latest" alt="Latest release"></a>
  <a href="https://github.com/meridianmcp/Meridian/stargazers"><img src="https://img.shields.io/github/stars/meridianmcp/Meridian?style=social" alt="GitHub Stars"></a>
</p>

---

## Why Meridian

Every AI coding session boots blind. You re-explain the architecture, re-describe
the constraints, re-list what's been tried. When context fills up mid-task,
everything is lost. This is **context debt** — and it compounds.

Meridian gives your sessions shared memory. They see the same task log, the same
pinned decisions, the same goal state. When context fills up, a new session resumes
from a compressed handoff in seconds. No copy-paste, no re-explaining from scratch.

### Meridian vs. the alternatives

|  | Meridian | CLAUDE.md | Mem0 / Zep | Anthropic Managed Agents |
|---|---|---|---|---|
| Persistent task log | ✓ | ✗ | ✗ | partial |
| Pinned decisions (editable) | ✓ | manual | ✗ | ✗ |
| Human-in-the-loop queue | ✓ | ✗ | ✗ | raw hook only |
| Atomic task claiming (parallel agents) | ✓ | ✗ | ✗ | ✗ |
| Self-hosted, data you own | ✓ | ✓ | ✗ | ✗ |
| Model-agnostic | ✓ | ✓ | ✓ | Claude only |
| Sprint board + swimlane timeline | ✓ | ✗ | ✗ | ✗ |
| Free forever | ✓ | ✓ | freemium | pay-per-session |

---

## Quickstart — binary (no Python required)

Download, double-click, done. Dashboard opens automatically at `http://localhost:7700`.

| Platform | Download |
|---|---|
| Windows | [meridian.exe](https://github.com/meridianmcp/Meridian/releases/latest/download/meridian.exe) |
| macOS (Apple Silicon) | [meridian-mac-arm64](https://github.com/meridianmcp/Meridian/releases/latest/download/meridian-mac-arm64) |
| Linux | [meridian-linux](https://github.com/meridianmcp/Meridian/releases/latest/download/meridian-linux) |

Data is stored in `~/.meridian/meridian.db`. Set `MERIDIAN_PORT` to change the port.

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

Dashboard opens at **http://localhost:7878**.

---

## Wire it into your AI client

### Claude Code

Drop a `.mcp.json` at your project root:
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

### claude.ai web (planning chat)

Use [dnakov/claude-mcp](https://github.com/dnakov/claude-mcp) — included as a submodule:

```bash
git clone --recurse-submodules https://github.com/meridianmcp/Meridian
```

1. Open `chrome://extensions` → enable **Developer mode**
2. **Load unpacked** → select `extensions/claude-mcp`
3. Set the URL to `http://localhost:7878/mcp`

All Meridian tools are now available directly in claude.ai planning chat.

### Hosted tier (no install)

Sign in at [usemeridian.us](https://usemeridian.us) → Settings → Copy MCP config.

```json
{
  "mcpServers": {
    "meridian": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://usemeridian.us/mcp"],
      "env": {"BEARER_TOKEN": "sk_meridian_YOUR_KEY_HERE"}
    }
  }
}
```

**Claude Desktop** users: install [meridian-hosted.dxt](https://github.com/meridianmcp/Meridian/releases/latest/download/meridian-hosted.dxt) — one click, no config.

---

## What you get

- **Dashboard** — sessions, tasks, sprint board, swimlane timeline, HITL queue, decisions
- **MCP tools** — `start_session`, `log_task`, `claim_task`, `pin_decision`, `request_hitl`, `generate_handoff`, and more
- **Tiered handoffs** — L0/L1/L2 compression so a fresh session resumes in seconds
- **Webhook intake** — push events from LangGraph / AutoGen / custom agents
- **Auto-checkpoint hooks** — every session start injects context; every end snapshots work

## How it works

```
> start_session(project_id="my-app", session_name="auth-refactor")
  ✓ session registered · sprint loaded · 8 pending tasks

> claim_task(task_id="a1f3...")
  ✓ claimed — parallel sessions skip this one automatically

> request_hitl(project_id="my-app", question="Redis or Postgres for session tokens?")
  ✓ queued — you get pinged, answer in dashboard, session resumes
```

## Auto-checkpoint with hooks

One command wires Claude Code and Codex to Meridian permanently.

**Mac/Linux:**
```bash
curl -fsSL https://usemeridian.us/hooks.sh | bash
```
**Windows:**
```powershell
irm https://usemeridian.us/hooks.ps1 | iex
```

After setup, every session automatically injects your project context on start and checkpoints on end.

---

## Hosted tier

Try free for 30 days — no card required, full features.

|  | **Free** | **Standard** | **Pro** |
|---|---|---|---|
| **Price** | Free · 30 days | $20 / mo | $49 / mo |
| **Card required** | No | Yes | Yes |
| **Storage** | Shared pool | 1 GB | 10 GB |
| **Compute** | 0.5 CU shared | 50 CU-hrs / mo | 200 CU-hrs / mo |
| **Projects** | 1 | Unlimited | Unlimited |
| **Concurrent sessions** | 1 | Unlimited | Unlimited |
| **Team members** | — | 25 | 50 |
| **Bring your own Postgres** | ✓ | ✓ | ✓ |
| **OAuth + magic link** | ✓ | ✓ | ✓ |
| **Support** | — | Email | Priority |

Overage: $0.16 / CU-hr · $0.50 / GB-month · billed via Stripe.
Self-hosted is always free — no limits, no expiry.

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=meridianmcp/Meridian&type=Date)](https://star-history.com/#meridianmcp/Meridian&Date)

---

## License

[MSL-1.0](LICENSE) — free for local and internal use at any team size. Paid
license required only if you host Meridian as a service for others. Converts to
MIT after 6 years.

Questions: [hello@usemeridian.us](mailto:hello@usemeridian.us)

## Contributors

[![Contributors](https://contrib.rocks/image?repo=meridianmcp/Meridian)](https://github.com/meridianmcp/Meridian/graphs/contributors)
