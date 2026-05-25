Show HN: Meridian – open-source MCP server for persistent AI session memory

Hi HN,

Claude Code has no memory between sessions. You explain your architecture, your constraints, your decisions — and when the context window fills, it's gone. CLAUDE.md is a workaround, not a solution. Databases are overkill. Copy-pasting summaries by hand doesn't scale.

I built Meridian to solve this for myself. It's a local MCP server that gives your AI sessions shared persistent memory: a task log, pinned architectural decisions, sprint items, and a goal state that survives context resets. When a session fills up, it generates a tiered handoff that a new session resumes from in seconds. No copy-paste, no re-explaining from scratch.

I'm a solo developer, and I built the whole thing in 7–8 days with Claude Code — Meridian building itself, dog-fooding its own memory tools along the way — 14k lines of Python and JavaScript. That context debt problem is what Meridian exists to prevent.

It's model-agnostic. Works with any MCP-compatible client: Claude Code, Cursor, Cline, or anything that speaks MCP. Self-hosted runs locally with SQLite — your data never leaves your machine. The MCP surface is 25 tools: log_task, set_goal, pin_decision, request_hitl, generate_handoff, and more.

There's a hosted tier at usemeridian.us ($20/mo, 7-day trial) for anyone who doesn't want to run infrastructure, backed by Neon Postgres with per-tenant DB provisioning.

GitHub: https://github.com/meridianmcp/Meridian
Hosted: https://usemeridian.us

Feedback I'm looking for: Is the MCP abstraction right, or is there a better interface? Is "context debt" the right framing — does it resonate? And what would make you actually try this vs. just bookmarking it?
