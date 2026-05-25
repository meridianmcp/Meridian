Show HN: Meridian – open-source MCP server that gives AI coding sessions shared memory

Hi HN,

Every Claude Code tab opens blind. It doesn't know what you built yesterday, what the other tab is working on, or why you made that architectural call last week. When context fills up mid-task, you lose everything and start over. If you run parallel AI sessions, they duplicate work and contradict each other.

Meridian is a local MCP server your AI sessions connect to. They share a persistent task log, pinned architectural decisions, and a goal state that survives context resets. When context gets full, the server generates a tiered handoff (north star + active decisions + recent tasks) that a new session can resume from in seconds. Multiple Claude Code tabs or LangGraph agents all see the same shared state — no duplication.

It's self-hostable and runs locally by default. There's no required cloud service; your data stays in a SQLite file on your machine. The MCP server exposes 25 tools: log_task, set_goal, pin_decision, request_hitl, generate_handoff, and more.

For teams or anyone who doesn't want to manage infrastructure, there's a hosted tier at usemeridian.us ($20/mo, 7-day trial, Postgres-backed with per-tenant Neon provisioning).

GitHub: https://github.com/meridianmcp/Meridian
Hosted: https://usemeridian.us

I'd love feedback on: whether the MCP tool surface is the right abstraction, whether local-first is the right default vs. hosted-first, and what integrations would be most useful (LangGraph checkpointer is next).
