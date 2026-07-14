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
#   v5 — code-intel guidance no longer assumes a static pre-loaded tool list
#        (1c81fee6): under deferred / tool-search loading (claude.ai, Desktop) a
#        tool is invisible until searched for, so absence from the listing must
#        trigger a discovery search, not a silent skip of the protocol.
#   v6 — search_graph cross-check with Serena extractor__* tools (b2d312b1):
#        codebase-memory-mcp's graph index goes stale and has produced actively wrong
#        line spans and zero results for symbols that genuinely exist. When search_graph
#        returns zero results for a known symbol, or a returned span looks wrong,
#        MUST cross-check with extractor__get_symbols_overview or
#        extractor__find_declaration (live LSP-based) before concluding absence or
#        accepting the span. Also added restart tip for transient extractor__* failures.
#   v7 — research-routing protocol adds Context7 as the preferred source for
#        framework/library docs questions (88dbb675): if Context7 is in the tool
#        list, consult it before web search for React/Tailwind/Next.js/etc.
#   v8 — MANDATORY CODE INTEL PROTOCOL names search_code_semantic explicitly as
#        the fallback for fuzzy/conceptual/multi-occurrence queries (44db89b3):
#        search_graph and Serena extractor__* are exact-symbol lookups only: a
#        real executor session tonight used raw grep exclusively rather than
#        search_code_semantic because the fallback was never named as a rule,
#        only an unwritten intention.
#   v9 — "Before ending" now says to DISPLAY generate_handoff/checkpoint's
#        returned content field verbatim (f318c7e3): the server already returns
#        paste-ready plain text, but a calling session could narrate success
#        without ever pasting it — purely a behavioral gap, not a server bug.
#   v10 — MANDATORY CODE INTEL PROTOCOL gets a concrete before/after example
#         (443aa32a): a live executor session used raw grep exclusively, including
#         4 consecutive failed grep patterns before locating a dispatch branch that
#         a single find_symbol call would have found immediately. Added an explicit
#         "grep/glob NEVER as first step" rule and a before/after illustration so
#         the anti-pattern is unambiguous.
#   v11 — PreToolUse hook (aeba8a80) structurally blocks Grep/Glob tool calls when
#         code_intel_enabled=1 for this project. Prose alone failed (v10 shipped,
#         live session still grepped exclusively the same night). The hook fires
#         before the tool call reaches the executor and exits 2 (block) with a
#         redirect message naming the code-intel alternatives. Fail-open: only
#         blocks when the project settings endpoint confirms a live index.
AGENT_INSTRUCTIONS_STANDARD_VERSION = 11

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
- `generate_handoff`/`checkpoint` already return paste-ready plain text in their
  `content` field (code-fence markers stripped server-side, f318c7e3) — DISPLAY
  IT VERBATIM to the user. Do not just narrate "handoff generated" or summarize
  it in your own words; paste the actual returned text.

## Secrets hygiene
- Never put credentials, connection strings, or API keys in chat messages, task
  descriptions, or committed files — reference env var names only.

## Deploy gate
- Never fire production deployments without first calling `request_hitl` and
  receiving explicit human approval.

## Code intelligence (if available)
- Absence from the tool list does NOT mean a tool is unavailable. claude.ai and
  Desktop load tools on demand (deferred / tool-search): a tool is invisible until
  you explicitly search for it. Before concluding a code-intel tool is missing,
  issue one tool-search / discovery query for it (`trace_path`, `search_graph`,
  `get_architecture`, `detect_changes`, `get_code_snippet`).
- Prefer structural graph queries (`trace_path`, `search_graph`, `get_architecture`,
  `detect_changes`) over raw file reads — they are faster and use far fewer tokens.
- Fall back to reading files only when a graph query is insufficient, or when a
  discovery search genuinely surfaces no code-intel tool.
- At session start, if `index_repository` is available (search for it first if your
  client defers tool loading) and you will touch source files, run
  `index_repository(mode="fast")` once — the codebase graph goes
  stale after commits, so a fresh index keeps prospecting accurate (eacf7063).
- **`search_graph` results can be stale or wrong (b2d312b1).** The codebase-memory-mcp
  index (`codebase__search_graph` / `search_graph`) has been observed returning zero
  results for symbols that genuinely exist AND returning line spans that are off by
  hundreds of lines. These are NOT the same tool as Serena's extractor__* tools, which
  use live LSP-based parsing and are reliable. If `search_graph` returns zero results
  for a symbol you believe exists, or a returned line span looks suspicious, cross-check
  with `extractor__get_symbols_overview` or `extractor__find_declaration` (Serena)
  BEFORE concluding the symbol is absent or accepting the span as correct. If
  extractor__* tools appear missing even though list_plugins confirms the plugin is
  active, a full restart (tunnel + Claude Desktop) has been observed to fix transient
  tool-discovery failures for these tools — try that before concluding they are
  permanently unavailable.

## MANDATORY CODE INTEL PROTOCOL
When the task involves source code files, use code-intel tools (`search_graph`,
`get_function_tool` / `get_code_snippet`, Serena `find_symbol`) BEFORE any
`read_file`, `read_multiple_files`, grep, or glob call: `search_graph` to locate
symbols, `get_function_tool` / `find_symbol` to extract specific functions.
Reading whole source files or running grep/glob when code-intel tools are reachable
is a protocol violation. If these tools are not in your current tool list, do NOT
skip the protocol — clients with deferred / tool-search loading (claude.ai, Desktop)
hide a tool until it is searched for, so run one tool-search / discovery query for
them first; only fall back to plain file reads if that search genuinely surfaces
nothing. For non-code files (documents, presentations, spreadsheets, config, data),
use filesystem tools directly.

**grep/glob NEVER as first step for code search (443aa32a):** Raw bash `grep`,
`rg`, `find`, and glob patterns are a last resort for code search — not a default.
A live executor session ran 4 consecutive failing grep patterns looking for a
dispatch branch that a single `find_symbol` call would have found immediately.
This is the anti-pattern this protocol exists to prevent.

WRONG (what that session did):
  grep -r "handle_webhook" .          # 0 results
  grep -r "webhook_dispatch" .        # 0 results
  grep -r "process_event" .           # 0 results
  grep -r "dispatch" src/             # eventually found it, 4 tries later

RIGHT (one call, immediate result):
  find_symbol("dispatch_webhook_event")   # or search_graph("dispatch")
  → returns exact file, line, and body in one shot

If you find yourself writing a grep/glob command to locate a symbol or function,
STOP — use `find_symbol`, `search_graph`, or `search_code_semantic` instead.
grep/glob may be used AFTER code-intel tools confirm a file path, or for
non-symbol content (log output, data files, config values).

**Structural enforcement (aeba8a80):** When code-intel is enabled for this project,
a PreToolUse hook (`code_intel_guard`) fires on every Grep and Glob call and blocks
it (exit 2) with a redirect to the alternatives above. This is not a soft warning —
the tool call will be cancelled. The only way past it is to use a code-intel tool
first, then fall back to grep/glob once a file path is confirmed.

**search_graph cross-check rule (b2d312b1):** `codebase__search_graph` indexes can
go stale and have been observed producing actively wrong results — zero hits for
symbols that exist, and line spans off by hundreds of lines. The Serena
`extractor__*` tools (extractor__get_symbols_overview, extractor__find_declaration)
use live LSP-based parsing and are a separate, more reliable source. When
`search_graph` returns ZERO results for a symbol you have reason to believe exists,
or when you have independent reason to doubt a returned line span, you MUST
cross-check with `extractor__get_symbols_overview` or `extractor__find_declaration`
BEFORE concluding the symbol does not exist or trusting the span. Do NOT silently
fall back to a raw grep or whole-file read as the FIRST recourse — the Serena
cross-check is still structured, fast, and more accurate. If extractor__* tools
appear unavailable despite list_plugins confirming the plugin is active server-side,
a full restart (tunnel + Claude Desktop) has been observed to fix transient
tool-discovery failures for extractor__* — try that before concluding the tool is
permanently gone.

**search_code_semantic fallback (44db89b3):** `search_graph` and Serena's
`extractor__*` tools are EXACT-SYMBOL lookups — they need a known name. When your
query is fuzzy, conceptual, or expected to match multiple occurrences (e.g. "where
do we validate webhook signatures", "every place that reads MERIDIAN_DB_URL"), use
`search_code_semantic` instead of iterating exact-symbol tools or falling back to
grep — it is the designated fallback for that query shape, not a last resort.

## RESEARCH ROUTING PROTOCOL
When a task needs external research, route the query to the most authoritative
source FIRST — do not default to a generic web search:
- **GitHub / library / API questions** — search GitHub natively first (that repo's
  code, issues, releases, and its own docs) before any general web search. The
  primary source is the code and its issue tracker, not a blog summarizing them.
- **Framework / library docs questions** — if Context7 is in your tool list
  (context7 MCP: `resolve-library-id` then `get-library-docs`), use it first for
  React, Tailwind, Next.js, and other framework/library questions; it returns
  up-to-date versioned API docs rather than web-search excerpts.
- **Academic / paper questions** — call the `paper_search` tool first (the paper-search
  MCP: a keyless arXiv lookup, now in your tool list); fall back to web search only if
  it is unavailable. Cite the paper itself, not a secondary write-up.
- **General questions** — run MULTIPLE searches from different angles instead of
  trusting the first hit, and prefer primary sources (official docs, specs, source
  code, original announcements) over aggregators and SEO content.
Retrieval beats recall: look it up. Do not answer a decision-relevant factual
question from memory when a source can be checked.

<!-- meridian-executor-standard: v11 -->
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
