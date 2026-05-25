Show HN: Meridian — Claude Code has no memory between sessions. I built the fix.

Hi HN,

Claude Code has no memory between sessions. You explain your architecture, your constraints, your decisions — and when the context window fills, it's gone. CLAUDE.md is a workaround, not a solution. Copy-pasting summaries by hand doesn't scale. I call this context debt — and it compounds.

I built Meridian to solve it. It's an open-source MCP server that gives your AI sessions shared persistent memory: a task log, pinned architectural decisions, sprint items, and a goal state that survives context resets. When a session fills up, it generates a tiered handoff that a new session resumes from in seconds.

I'm a solo developer. I built the whole thing in 7–8 days with Claude Code — Meridian building itself, dog-fooding its own memory tools along the way — 14k lines of Python and JavaScript. That context debt problem is exactly what Meridian exists to prevent.

Model-agnostic — works with any MCP-compatible client, not just Claude Code: Cursor, Cline, Claude Desktop, or anything that speaks MCP. Self-hosted runs locally with SQLite — your data never leaves your machine. The MCP surface is 25 tools: log_task, set_goal, pin_decision, request_hitl, generate_handoff, and more.

There's a hosted tier at usemeridian.us ($20/mo, 7-day trial) for anyone who doesn't want to run infrastructure, backed by Neon Postgres with per-tenant DB provisioning.

GitHub: https://github.com/meridianmcp/Meridian
Hosted: https://usemeridian.us

Has anyone else built persistent context layers for their AI coding sessions? Curious what approaches people have tried.
