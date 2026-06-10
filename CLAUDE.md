# CLAUDE.md — Meridian User Template

This file is read by Claude Code at session start. Copy it to your project root and
fill in your `project_id` to get Meridian session coordination automatically.

---

## Connect to Meridian

```json
{
  "mcpServers": {
    "meridian": {
      "command": "pixi",
      "args": ["run", "python", "-m", "meridian", "--mcp"],
      "cwd": "/path/to/Meridian"
    }
  }
}
```

Or use the hosted tier (no install):
```json
{
  "mcpServers": {
    "meridian": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://usemeridian.us/mcp"],
      "env": { "BEARER_TOKEN": "sk_meridian_YOUR_TOKEN" }
    }
  }
}
```

---

## Your project ID

```
PROJECT_ID=your-project-id-here
```

Get your project ID from the Meridian dashboard at `http://localhost:7878` after
running `create_project(name="your-project")`.

---

## Session rules

ALWAYS at session start:
- Call `start_session(project_id="PROJECT_ID", session_name="describe-what-youre-doing")`
- This returns the goal, recent tasks, pending sprint items, and active sessions in one call.

ALWAYS during work:
- Call `log_task(session_id, project_id, description)` after completing meaningful work.
- Call `pin_decision(project_id, title, body, category)` for any architectural choice.
- Call `request_hitl(project_id, question)` when you need a human decision before continuing.

ALWAYS before ending:
- Call `checkpoint(session_id, project_id)` — snapshots progress, generates delta handoff, returns next `/goal` string.

---

## The 5 tools you use 90% of the time

| Tool | When | Example |
|------|------|---------|
| `start_session` | First thing, every session | `start_session(project_id="abc", session_name="auth-refactor")` |
| `log_task` | After finishing anything meaningful | `log_task(session_id, project_id, "Fixed OAuth redirect bug")` |
| `checkpoint` | Before context fills, before ending | `checkpoint(session_id, project_id)` |
| `pin_decision` | Architectural choices | `pin_decision(project_id, "Use psycopg3", "asyncpg has DLL issues on Windows", "TECHNICAL")` |
| `request_hitl` | Blocking questions for a human | `request_hitl(project_id, "Should we rate-limit per IP or per token?")` |

---

## Auto-checkpoint with hooks

Wire Claude Code to checkpoint automatically on every session end:

```bash
# Mac/Linux
curl -fsSL https://usemeridian.us/hooks.sh | bash

# Windows
irm https://usemeridian.us/hooks.ps1 | iex
```

Prompts for your Meridian URL and project ID, then writes `SessionStart` and `Stop`
hooks to `~/.claude/settings.json`. From that point on, every session auto-injects
your project context on start and snapshots progress on end.

---

## Docs

- Full MCP tool reference: `http://localhost:7878/mcp/tools-doc`
- Quick reference: `http://localhost:7878/mcp/quickstart`
- Web docs: https://docs.usemeridian.us

---
## Executor rules (Meridian project only)

- **Secrets hygiene**: Never put credentials, connection strings, API keys, or secrets in chat or task descriptions. Mention env var names only.
- **Before every push**: Run `pixi run test` locally first. CI is a safety net — not the first check. Never push broken code.
- **End every session**: If tests pass, merge `dev → main` and push `main` to trigger deploy. Do not end the session with work stranded only on `dev`.
- **Set sprint name**: Use `PATCH /projects/{id}/goal` with body `{"sprint": "name"}` directly (or the `set_sprint` MCP tool). Do NOT use `set_goal` for sprint-only updates.
- **Handoff**: Use `get_context_block(project_id)` for the handoff context block. Do NOT read from `data/meridian-build_handoff.md` — that file is local-only and not reliable across sessions.
- **Project discovery**: Use `list_projects()` when the project ID is unknown. Never call `create_project()` without explicit human instruction — use `list_projects()` to find existing projects first. Never create a project just to get a working ID.
- **Staging pipeline**: `dev push → test → deploy preview → smoke test → merge main → prod`. Rollback fires automatically if prod /health returns non-200 after deploy.
- **Demo write protection**: Adding a new write endpoint requires NO demo exception — the middleware in `server.py` handles it globally. When adding a new write UI element, add it to the `hideDemoAdminControls()` selector list in `dashboard.js`.

---
## Meridian-managed notes

<!-- Agent-proposed via update_md_section, human-approved via the dashboard HITL queue. -->
<!-- MERIDIAN:ANCHOR:START:claude-body -->
<!-- MERIDIAN:ANCHOR:END:claude-body -->

---
<!-- MERIDIAN STATE — auto-generated, do not edit below -->
## Current Sprint State  _(auto-updated 2026-06-10 06:48 UTC)_

**North Star:** Meridian is the open-source coordination layer for AI coding sessions — persistent memory, task tracking, and session coordination that your AI tools don't provide.

Core value: "Your AI sessions don't remember each other. Meridian fixes that."
Single chat or 10 parallel Claude Code sessions — same product, same value.

PHILOSOPHY:
Anthropic's approach: black box, cloud-only, trust us.
Meridian's approach: transparent, local-first, verify yourself.
Every goal state is inspectable. Every task has a paper trail. You own your data.
The developer is the customer. The product serves them, not their manager.

COMPETITIVE POSITION:
Anthropic shipped Managed Agents (May 2026) — closed, managed, Claude-only.
Meridian is the open-source alternative for developers who won't trust a black box.
Even if Anthropic takes market share: self-hosted Meridian gives companies their data on their infra.
That's a structural advantage no hosted black box can match.

LICENSE: MSL-2.0 (BUSL-based). Free for local/internal use. Paid for hosting as a service.

GROWTH MODEL: Product-led. Developers install it. They tell their team. Team tells manager. Manager pays.

BUSINESS MODEL:
Free:   Local install, any team size, SQLite or Postgres (Neon), full features. Self-hosted forever.
Paid:   Hosted tier — usemeridian.us, dedicated DB, zero install. $20-49/team/month.
Enterprise: SSO, audit logs, RBAC, on-premise support.

EXIT OPPORTUNITIES: standard paths for a developer tools company — continued growth, strategic partnership, or acquisition.

WHAT WE WON'T BUILD:
- Productivity scores, rankings, leaderboards per developer
- Manager surveillance features
- Per-developer comparison metrics without developer consent
- Any feature where the surveillance target is the person, not the project

**Sprint:** HN launch — fix /demo crash, sprint board live, security audit, SHOW_HN, binary release, deploy pipeline

**Pending Sprint Items:**
- [ ] FEAT: XML anchor sections in .md files for structured auto-update. Pattern: wrap sections with MERIDIAN:ANCHOR:START/END HTML comments. Three behaviors: (1) DEVLOG.md = append-only via auto_capture on session end, never overwrites. (2) ROADMAP.md/CLAUDE.md/AGENTS.md = replace-section via MCP tool update_md_section(file, anchor, content), requires HITL confirmation before write to prevent backwards overwrites. (3) README.md = manual only. Dashboard UI: show each anchor section with current content + propose-update button that queues HITL request. Agent drafts, human approves, file updates. New MCP tool: update_md_section(project_id, file, anchor, content) → creates HITL request with diff preview.
- [ ] FEAT: Auto-append to ROADMAP.md, CLAUDE.md, AGENTS.md, DEVLOG.md when new decisions/notes added. Pattern: XML anchors (MERIDIAN:ANCHOR:START/END). Append-only by default when pin_decision() or add_note() called with relevant tags. Manual override: update_md_section(file, anchor, content) forces a full section replace — use when a decision supersedes a previous one. Implementation: (1) add XML anchors to each .md file, (2) hook pin_decision() to append to CLAUDE.md anchor, (3) hook add_note(tag=roadmap) to append to ROADMAP.md anchor, (4) hook auto_capture to append to DEVLOG.md anchor, (5) add update_md_section MCP tool for forced replace. All writes go through git commit so they're versioned.
- [ ] MANUAL (Adam only): HN account warmup before Monday launch. Post 2-3 genuine comments on HN this weekend (Friday-Sunday) to clear new-account filter. Comments must be real, substantive, on relevant threads — not throwaway. New accounts with zero comment history get deprioritized in Show HN ranking. Do this from home IP. MUST be done before 9am ET Monday June 2 post time.
- [ ] POST-LAUNCH: File-level conflict detection for parallel executors. Currently Meridian guarantees no two executors claim the same sprint item (atomic claiming) but does NOT guarantee they won't touch the same file. When parallel /goal runs both edit server.py etc, git merge conflicts result. Mitigation today: human designs sprint items to avoid file overlap, HITL surfaces conflicts when they happen. Future: detect file overlap at claim time and warn/block. Requires codebase understanding (see future-ideation note on Understand Anything integration) — up to human to resolve until then. Not a launch blocker — current alpha-launch sprint items touch distinct files.
- [ ] FEAT: Autopilot / AI-side controller (post-launch research spike). The MCP primitives exist but there's no enforcement layer — the AI has to *choose* to call pin_decision, log_task, checkpoint etc. and often doesn't. Two approaches to explore: (1) LLM monitor process — a lightweight sidecar that watches the conversation transcript (via claude.ai export or MCP hooks) and auto-fires Meridian tools when it detects task completions, decisions, or context drift. (2) Prompt-injection approach — a CLAUDE.md / system prompt template that hardcodes Meridian tool calls at specific triggers (session start, every 10 turns, session end). Neither requires new server code. Approach 2 is shippable today. Approach 1 requires Anthropic exposing conversation hooks (post-launch research). File as research spike — outcome is either a recommended CLAUDE.md template users can adopt, or a prototype sidecar. Related to auto-restore after compaction idea (note 1c8511ed).
- [ ] QA: Test Windows .exe install flow end-to-end. Steps: (1) trigger binary release by pushing git tag v1.0.0-alpha if not already done, (2) wait for release.yml to build meridian.exe on windows-latest runner, (3) download the produced meridian.exe artifact, (4) run it — verify browser opens at localhost:7878/dashboard, (5) verify /demo loads, (6) verify MCP server responds to a simple start_session call via stdio. Document any failures. If exe fails to start or crashes, fix PyInstaller spec (meridian.spec) — common issues: missing hidden imports, missing static files, wrong entry point. The exe install story is critical for HN — developers won't install pixi just to try it.
- [ ] FEAT: Add code-intelligence MCP as submodule for complex coding tasks. Research and add the best available code-understanding MCP server as a submodule under extensions/code-mcp or similar. Candidates: (1) modelcontextprotocol/servers/tree-sitter — AST-based code analysis, (2) repomix MCP — packages entire codebase for AI context, (3) mcp-server-git — git history + blame + diff tools, (4) any "understand anything" style server that does semantic code search. Pick the one with most GitHub stars + active maintenance. Add as submodule, document in README under "Power Tools" section. The use case: when Claude Code hits a complex refactor or needs to understand a large unfamiliar codebase, it can call the code-intelligence MCP to get semantic context rather than reading files linearly. Generalizable to any project using Meridian.
- [ ] FEAT: Hosted pack_repo endpoint — server-side Repomix for hosted tier users. Hosted users can't run Repomix locally against their repo. Add POST /tools/pack-repo {"github_url": "https://github.com/..."} that runs `npx repomix` server-side against the remote GitHub URL (Repomix supports remote repos natively) and returns the packed XML/markdown context. Auth: Bearer token required. Rate-limited: 5 calls/hour free, 50/hour paid. This gives hosted users codebase context without local install. Surface as a Meridian MCP tool: pack_repo(github_url) → returns packed codebase string. Post-launch once Repomix remote repo support is confirmed stable.
- [ ] FEAT: Project fork/merge/duplicate for multi-team coordination. Currently one project = one goal state shared by all members. For teams where members diverge (different features, different directions): (1) fork_project(project_id, new_name) — clones goal state, decisions, sprint items into a new project. Both projects are independent after fork. (2) merge_project(source_id, target_id, strategy="decisions_only"|"sprint_items"|"all") — merges non-conflicting items. Conflicting decisions surface as HITL for human resolution. (3) duplicate_project(project_id) — full clone including sessions, useful for branching a prototype. Use cases: team splits on architecture direction, indie contributor wants their own fork of a shared project, A/B testing two product directions. DB: add forked_from FK on projects table. MCP tools: fork_project, merge_project, duplicate_project. Dashboard: fork button on project card, merge UI on project settings. Post-launch — design carefully, git-like semantics.

**Recent Decisions:**
- Window estimate: 6-12 months before Anthropic or OpenAI ships native multi-session coordination and commoditizes the "sessions share memory" pitch. The features that create the moat must ship before that: HITL queue, team visibility dashboard, decisions log UI, weekly auto-digest.
- Positioning: human-facing, not agent-facing. Pitch is "your AI doesn't forget and your team stays coordinated" not "better RAG." Enterprise hook is HITL queue — companies will pay for "autonomous until it needs me" workflow. That's the $500-2000/mo line item, not the $49 dev tool.
- [2026-05-24] v2.3 — MERIDIAN_HOSTED=true env var disables GOAL.md ↔ DB sync in goal_md.py (sync_goal_md_to_db / sync_db_to_goal_md / watch_goal_md all early-return). Necessary because the hosted multi-tenant deployment serves many tenants from a single process — reading/writing a single repo-root GOAL.md would corrupt or leak data. Self-hosted users keep the full file sync behavior unchanged. Set as a Fly secret on meridian-hosted.
- [2026-05-24] v2.3 — goal_states gets per-field timestamps (ns_updated_at / content_updated_at / sprint_updated_at). Set whenever the corresponding field actually changes; carried forward when it doesn't. Replaces multi-row history walking in get_goal_field_ages so in-place UPDATEs (sprint-only changes, auto-summary appends) no longer collapse per-field freshness. Backward compat: NULL columns fall back to row updated_at + history walk.
- [2026-05-24] DRIVER DECISION — psycopg3 replaces asyncpg permanently as sole Postgres driver. asyncpg has Windows DLL/libpq hang issues (PostgreSQL BUG #19424 — concurrent PQconnectdb() hangs on Windows 11) and Cython DLL AV scanning delays. psycopg3 is pure Python (binary C extension optional), actively maintained, SelectorEventLoop required on Windows (set via uvicorn.Server bypass). asyncpg removed from pixi.toml entirely. pg_adapter.py now uses psycopg3 AsyncConnectionPool throughout.

**Key Files:**
- `meridian/server.py` — FastAPI app + MCP handlers
- `meridian/db.py` — all DB functions (SQLite + Postgres)
- `meridian/static/dashboard.js` — dashboard UI
- `tests/test_core.py` — full test suite
- `data/meridian-build_handoff.md` — session handoff
