# Parallel Safety

Run two (or ten) AI coding sessions against the same repo without them silently
overwriting each other's work. Meridian coordinates parallel sessions at three
levels of granularity — pick the finest one that fits the work.

## 1. Whole-file claims

Before editing a shared file, a session claims it:

```
claim_file(session_id="…", file_path="meridian/server.py")
```

Another live session that tries to claim the same file gets a warning, and new
sessions see `file_warnings` in their `start_session` response. Locks auto-expire
after 2 hours so a crashed session never blocks the file forever. Release early
with `release_file(session_id, file_path)`.

High-contention files (`dashboard.js`, `server.py`, `db/__init__.py`) are best
serialized rather than raced.

## 2. Symbol-level claims

Whole-file locking is too coarse for a 7,000-line `server.py`. Instead, claim a
single class, function, or method — two sessions can then edit the same file as
long as they own different symbols:

```
claim_file(
  session_id="…",
  file_path="meridian/server.py",
  symbol="AuthRouter",          # or "AuthRouter.login" for a single method
  content="<the file's full source>",
)
```

Meridian parses `content` server-side — Python with the stdlib `ast`, and
JavaScript / TypeScript / C / C++ / Go / Rust / Java / C# with tree-sitter — to
resolve the symbol's line range. If another **live** session already holds an
overlapping range, the claim is hard-blocked and tells you what's still free:

```
⚠️ AuthRouter (lines 45-120) claimed by session big-boi
   — you can safely claim MCPHandler, MCPHandler.handle, helper
```

Notes:

- Pass the **full file source** as `content`; the hosted server has no access to
  your local filesystem, so it can't read the file itself.
- Unsupported languages, syntax errors, or a missing grammar fall back
  transparently to a whole-file lock.
- Class ranges cover their methods, so claiming a class and a method inside it
  correctly conflict.
- `get_symbol_claims(file_path)` lists who owns what; `get_symbol_hotspots()`
  surfaces symbols 3+ sessions have touched in the last 14 days (a refactor smell).

## 3. `touches_files` intent

Sprint items can declare the files they expect to edit:

```
touches_files: ["meridian/server.py", "meridian/static/dashboard.js"]
```

`claim_sprint_item` then warns (or, outside worktree mode, blocks) when another
live session already claims an overlapping file, and handoffs surface the overlap
before a second agent copies the same work.

## Worktree isolation

When a project enables worktrees, `claim_sprint_item` returns a ready-to-run
`worktree_setup_cmd` so each session works in its own `git worktree` and merges
back when done — full isolation with no shared-file contention at all. A
non-blocking `file_overlap_warning` still flags overlaps so you coordinate the
merge.
