# FAQ

Answers to common questions about Meridian.

---

## Privacy and data

### Does Meridian staff read my code or workflows?

No. For **self-hosted** installs, your data never leaves your machine — we have no access whatsoever. The server runs on localhost, your DB is a local SQLite file, and there are no telemetry calls home.

For the **hosted tier** (usemeridian.us), your data lives in an isolated Neon Postgres database provisioned for your workspace. Meridian staff cannot read your project data. The HITL queue means *you* are the human who reviews and approves agent decisions — not us.

### What happens to my data if I cancel hosted?

You have 28 days after cancellation before your database is deleted. We'll email a reminder. You can export a full SQLite dump of your data at any time via the dashboard. Self-hosted users are unaffected — you always own your own files.

---

## Comparison and use cases

### How is Meridian different from CLAUDE.md?

`CLAUDE.md` is a static text file injected into every Claude session. It's great for standing context but it can't:

- Track what happened in previous sessions
- Coordinate between two parallel Claude Code terminals
- Queue HITL requests that need a human decision
- Maintain a sprint board or decision log

Meridian is live state: sessions read and write to a shared DB in real time. Think of `CLAUDE.md` as a README and Meridian as the project management layer on top.

### Does it work with Cursor, Windsurf, or other clients (not Claude)?

Yes. Meridian exposes a standard [MCP server](https://spec.modelcontextprotocol.io). Any MCP-compatible client works — Cursor, Windsurf, Claude Desktop, Claude Code, LangGraph agents, AutoGen. The coordination layer is model-agnostic.

### Can I use local models (Ollama, LM Studio, etc.)?

Yes, if your local model supports MCP tool use. Meridian doesn't care what model drives the session — it only sees the MCP tool calls. Local model support depends on the client (e.g. LM Studio's MCP support), not on Meridian.

### Do I need a Claude Max or Pro subscription?

No. Meridian works with any Claude plan, including the free tier. It also works with any other model or client that supports MCP. Your Claude subscription is between you and Anthropic — Meridian is a separate coordination layer.

---

## Technical

### What is MCP?

MCP (Model Context Protocol) is an open standard for connecting AI models to external tools and data sources. Think of it like a plugin system: Meridian registers itself as an MCP server, and any AI client that supports MCP can call its tools (`start_session`, `log_task`, `generate_handoff`, etc.) as if they were built-in capabilities.

The protocol is model-agnostic and was designed to work across Claude, GPT, Gemini, and local models.

### What happens if two sessions edit the same thing?

Meridian uses atomic task claiming to prevent conflicts. When a session calls `claim_task(task_id)`, the DB locks that task so no other session can claim it simultaneously. Sprint items work the same way: one session claims an item, others see it as taken.

For goal state, the last write wins (Meridian uses an incrementing version number). Session focus updates are non-conflicting by design — each session has its own sprint field.

File-level conflicts (two sessions editing the same file) are outside Meridian's scope. Design sprint items to minimize file overlap; use HITL when overlap happens.

### Is there performance overhead?

Minimal. MCP calls are async and typically resolve in under 50ms for local SQLite. The DB is read on every `start_session` call and written on `log_task` / `checkpoint`. There is no polling loop in the AI session — Meridian only runs when your session calls a tool.

For hosted Postgres (Neon), the first query after scale-to-zero takes ~300ms for cold start. Subsequent queries are fast.

### Does the license allow commercial use?

**MSL-1.0** (based on BUSL) is free for any internal use — including commercial projects. You pay only if you host Meridian as a service for others (i.e., resell it). The license converts to MIT automatically after 6 years. Most developers and teams will never pay anything.

---

## Setup and migration

### Can I migrate from an existing CLAUDE.md setup?

Yes. Run `pixi run start` (or the binary), create a project, and paste your `CLAUDE.md` content into the **North Star** and **Version Goal** fields in the dashboard. Your AI clients pick it up via `start_session`. The two systems are complementary — keep `CLAUDE.md` for static context and use Meridian for dynamic coordination.

### Can teams share a single Meridian instance?

Yes. The self-hosted tier has no seat limits — any number of team members can connect to the same Meridian server. Each session identifies itself via `human_id`. The dashboard shows per-human activity in the Team tab.

For remote team access, set `MERIDIAN_HOST=0.0.0.0` and protect the server behind a reverse proxy (e.g. nginx + mTLS or Cloudflare Tunnel).

### Can I bring my own Postgres database (BYODB)?

Yes, for both self-hosted and hosted tiers. Set `MERIDIAN_DB_URL=postgresql://...` and Meridian switches from SQLite to Postgres automatically. On the hosted tier, you can paste your own Neon connection string at signup.

### Does Meridian integrate with LangGraph or AutoGen?

Yes. Meridian exposes an HTTP REST API and an MCP server. LangGraph agents can call the REST endpoints directly or via an HTTP MCP client. The webhook intake endpoint (POST `/projects/{id}/tasks`) lets external agents log tasks without the MCP protocol.

---

## About Meridian

### Who built this and why?

Meridian is open-source (MSL-1.0), built to solve the coordination problem in long-running AI coding workflows. The core insight: AI sessions are stateless by default, but the *project* isn't. Meridian is the persistence layer that lets sessions remember each other.

### Is there a hosted option for teams?

Yes — [usemeridian.us](https://usemeridian.us). Standard is $20/month, Pro is $49/month. Both include a 30-day free tier with no card required. You get a managed Postgres database, hosted dashboard, and remote MCP endpoint — zero install on your end.

### Can I use Meridian from claude.ai without installing anything?

Yes. The [dnakov/claude-mcp](https://github.com/dnakov/claude-mcp) Chrome extension connects claude.ai to any MCP server via SSE. For the hosted tier:

1. Sign in at [usemeridian.us](https://usemeridian.us)
2. Install the extension → Add server → URL: `https://usemeridian.us/mcp`
3. An auth popup appears — sign in and the connection completes automatically

For self-hosted: start `pixi run start` locally, then use `http://localhost:7878/mcp` as the URL. No auth popup needed.

→ [Full browser connector guide](browser-connector.md)

### What does the 30-day free trial include?

The hosted free trial gives you full access to the Standard tier — managed Postgres, hosted dashboard, remote MCP endpoint, HITL queue, and all MCP tools. No credit card is required to start. After 30 days of inactivity the account expires; active accounts stay on the trial until you choose to subscribe.

The self-hosted tier is free forever with no time limit.

### Can I export my data before the trial expires?

Yes. Dashboard → Settings → Export → download your full SQLite snapshot. This includes all projects, sessions, tasks, decisions, and notes. You can import it into a self-hosted Meridian instance or keep it as a backup.
