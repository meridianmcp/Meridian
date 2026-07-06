"""Default agent_instructions injected into every new project.

This text is set automatically on project creation and is returned by
``start_session`` so every AI coding session picks up the coordination rules
without any per-repo file configuration.

Users can view / edit / reset this in the dashboard:
  Project → Settings → Executor Rules
"""

from __future__ import annotations

import re

# 99e50a1d — the executor-rules / handoff standard is versioned. Bump this
# whenever the default changes in a way stored per-project copies should
# re-sync to. The hidden marker below is embedded in DEFAULT_AGENT_INSTRUCTIONS,
# so a project's stored copy carries the version it was last synced from and
# generate_handoff can warn when that copy predates the current standard.
#
#   v1 — pre-versioning baseline (implicit; stored copies with no marker).
#   v2 — project_name-first start_session idiom + code-intel protocol; marker added.
#   v3 — reindex-at-session-start guidance (eacf7063).
#   v4 — request_hitl is the ONLY human-decision channel (never native ask, d261ea2e)
#        + research-routing protocol (f8c70f9a).
AGENT_INSTRUCTIONS_STANDARD_VERSION = 4

_STANDARD_MARKER_RE = re.compile(r"meridian-executor-standard:\s*v(\d+)")

DEFAULT_AGENT_INSTRUCTIONS = """\
# Meridian — executor rules

These rules are injected automatically via `start_session`. Edit or reset them
in the Meridian dashboard → Settings → Executor Rules.

## Session start
- Call `start_session(project_id=..., session_name=...)` **first**, every session.
- If the response lists pending sprint items, immediately call `claim_sprint_item`
  on the first unclaimed one and start working. Do NOT ask "what would you like
  to work on?" when items are waiting.

## File & symbol ownership
- Before editing a shared file, call `claim_file(session_id, file_path)` and
  check the `file_warnings` field from `start_session`. If another session already
  holds the file, coordinate before editing.
- For symbol-level edits (one class or function inside a large file), use
  `claim_symbol(session_id, file_path, symbol, content)` — two sessions can safely
  work in the same file when they own different symbols.
- To check active locks before starting parallel work, call
  `get_file_claims(file_path)`.

## Sprint board discipline
- `complete_sprint_item(item_id, project_id)` is **mandatory** for every sprint
  item you finish — call it before `checkpoint`. The board never auto-reconciles
  from git; skipping this drifts the board and breaks handoffs.
- After completing each item, call `get_sprint_progress(project_id, session_id)`
  (pass `session_id`) before claiming the next — the `board_change` field surfaces
  items injected mid-run. Never call `get_sprint_items` just to check for new work.
- When `claim_sprint_item()` returns `worktree_suggested: true`, use the provided
  worktree commands to isolate your work from parallel sessions.

## During work
- Call `log_task(session_id, project_id, description)` after every meaningful action.
- Call `pin_decision(project_id, title, body, category)` for architectural choices.

## Human decisions route through `request_hitl` ONLY — never the native "ask" UI
- Any question that needs a human answer — "how should I proceed", "which option",
  "is this okay to do", anything blocking on a person — MUST go through
  `request_hitl(project_id, question)`. Do NOT ask it in the executor's own chat/CLI
  prompt instead.
- Asking natively is invisible to Meridian: no dashboard entry, no audit trail, and
  `list_hitl_requests` stays empty, so the human never actually sees it and the
  session silently stalls. Reading a pinned decision first does not exempt you — if
  you still need a human call, `request_hitl` is the only channel.
- Do not guess and do not ask natively — pause and call `request_hitl`, then continue
  once it returns an answer.

## Before ending
- Call `checkpoint(session_id, project_id)` before context fills and before ending
  the session. It snapshots progress, generates a delta handoff, and returns the
  next `/goal` string.

## Secrets hygiene
- Never put credentials, connection strings, or API keys in chat messages, task
  descriptions, or committed files — reference env var names only.

## Deploy gate
- Never fire production deployments without first calling `request_hitl` and
  receiving explicit human approval.

## Code intelligence (if available)
- If `trace_path` appears in your MCP tool list, use it before opening any file.
- Prefer structural graph queries (`trace_path`, `search_graph`, `get_architecture`,
  `detect_changes`) over raw file reads — they are faster and use far fewer tokens.
- Fall back to reading files only when a graph query is insufficient.
- If `trace_path` is not in your tool list, ignore this section.
- At session start, if `index_repository` is in your tool list and you will touch
  source files, run `index_repository(mode="fast")` once — the codebase graph goes
  stale after commits, so a fresh index keeps prospecting accurate (eacf7063).

## MANDATORY CODE INTEL PROTOCOL
When `search_graph`, `get_function_tool`, or `get_code_snippet` are in your tool
list AND the task involves source code files, use them BEFORE any `read_file` or
`read_multiple_files` call. Call `search_graph` to locate symbols. Call
`get_function_tool` to extract specific functions. Reading whole source code
files when code intel tools are present is a protocol violation. For non-code
files (documents, presentations, spreadsheets, config, data), use filesystem
tools directly.

## RESEARCH ROUTING PROTOCOL
When a task needs external research, route the query to the most authoritative
source FIRST — do not default to a generic web search:
- **GitHub / library / API questions** — search GitHub natively first (that repo's
  code, issues, releases, and its own docs) before any general web search. The
  primary source is the code and its issue tracker, not a blog summarizing them.
- **Academic / paper questions** — use the paper-search MCP first when it is in your
  tool list (arXiv / Semantic Scholar-style lookup); fall back to web search only if
  it is unavailable. Cite the paper itself, not a secondary write-up.
- **General questions** — run MULTIPLE searches from different angles instead of
  trusting the first hit, and prefer primary sources (official docs, specs, source
  code, original announcements) over aggregators and SEO content.
Retrieval beats recall: look it up. Do not answer a decision-relevant factual
question from memory when a source can be checked.

<!-- meridian-executor-standard: v4 -->
"""


def parse_standard_version(text: str | None) -> int | None:
    """Return the embedded executor-standard version in ``text``, or None.

    Stored copies from before versioning (v1) carry no marker and return None.
    """
    if not text:
        return None
    match = _STANDARD_MARKER_RE.search(text)
    return int(match.group(1)) if match else None


def agent_instructions_stale(stored: str | None) -> bool:
    """True when a project's STORED executor rules predate the current standard.

    ``None`` (no stored copy — the session falls back to the live default) is
    never stale. A stored copy is stale only when it *looks like* a Meridian
    executor-rules doc (so genuinely bespoke instructions are never nagged) and
    its embedded standard version is missing or older than
    :data:`AGENT_INSTRUCTIONS_STANDARD_VERSION`.
    """
    if not stored or not stored.strip():
        return False
    looks_like_standard = "Meridian" in stored and "start_session" in stored
    if not looks_like_standard:
        return False
    version = parse_standard_version(stored)
    return version is None or version < AGENT_INSTRUCTIONS_STANDARD_VERSION
