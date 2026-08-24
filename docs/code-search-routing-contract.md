# Code search / symbol-prospecting routing contract

Status: documents the LIVE routing behavior of `prospect_symbol` and
`search_code_semantic` as of sprint item `ec91e311` (Track B — Retrieval /
Code Intelligence). No new MCP server was introduced by that item; this page
records the investigation and the one real gap it found and closed.

## The three layers

| Layer | What it is | Freshness model | Needs |
|---|---|---|---|
| **codebase-memory graph** (`codebase__search_graph`) | Indexed structural/BM25-style graph over a repo, built by a separate `index_repository` run | Point-in-time snapshot; goes stale as commits land without a re-index | A tunnel-connected `codebase-memory-mcp` slot + a prior `index_repository` |
| **Serena** (`extractor__find_symbol` / `extractor__find_declaration`) | Live AST-accurate symbol navigation, no index to go stale | Always current | A tunnel-connected `extractor` (Serena) slot |
| **meridian-codeindex** (`extensions/meridian-codeindex`) | Dependency-light local fallback: tree-sitter/ast chunking, Merkle-incremental reindex, DuckDB FTS (BM25) + optional VSS vector leg | Explicit, queryable (`get_convergence_state`) | Nothing but a local `root_dir` — no graph, no Serena, no tunnel, no hosted access |

`meridian-codeindex` is a real standalone package (its own
`extensions/meridian-codeindex/pyproject.toml`, zero import of `meridian`,
`Serena`, or `codebase-memory-mcp`) — the same "independent tool, Meridian is
just one caller" relationship Meridian already has with codebase-memory-mcp.
It ships its own `meridian-codeindex` CLI and is `pip install`-able on its
own, so "usable standalone, without the core Meridian server" is already
true today via the CLI or a direct `import meridian_codeindex` — see
[Standalone usability](#standalone-usability-without-meridian) below.

## `prospect_symbol` — the three-rung fallback chain

`prospect_symbol_impl` (`meridian/prospect.py`) tries, in order, and stops at
the first rung that produces hits:

1. **Rung 1 — graph** (`codebase__search_graph`). Skipped when:
   - the caller passes `stale_graph=true`, or
   - a local `git rev-list --count` finds real commits since the project's
     last `index_repository` fingerprint (`_detect_graph_commit_drift` —
     closes the gap where nobody ever re-indexes, not just where a sibling
     process re-indexes with a different fingerprint), or
   - there is no tunnel-connected `codebase-memory-mcp` slot for this
     tenant, or
   - there is no `tenant_id` at all (self-hosted, no tunnel context).

   A zero-hit or "project not found"/"not indexed" result additionally
   triggers a **broad retry without `project_id`** before the rung is
   counted as missed — a `project_id` that doesn't match the repo-path slug
   `index_repository` auto-assigned is a confirmed live failure mode.

2. **Rung 2 — Serena** (`extractor__find_symbol` then
   `extractor__find_declaration`). Tried whenever a tunnel-connected slot
   exists for the tenant, regardless of whether Rung 1 ran — it never goes
   stale, so it is always worth trying before falling to the local fallback.

3. **Rung 3 — semantic** (`search_code_semantic`, i.e. meridian-codeindex).
   Tried whenever a `root_dir` was supplied, whether or not a tenant/tunnel
   exists — this is the rung that needs **nothing** but a local directory,
   exactly the "works without graph freshness, Serena, a tunnel, or hosted
   access" fallback this sprint item's scope calls for.

Every rung's outcome is recorded in the response's `rungs.{graph,serena,semantic}`
map (`status`, `attempted_tool`/`selected_tool`, `reason`/`error`/`error_kind`)
so a caller never has to infer *why* a rung missed. When every rung misses,
`fallback_reason` is synthesized from those per-rung diagnostics rather than
left `null`.

## `search_code_semantic` — direct, always-local

Unlike `prospect_symbol`, the `search_code_semantic` MCP tool never touches
the graph or Serena. It calls `meridian.code_index.search_code_semantic`
(`meridian/code_index.py`), a thin wrapper around
`meridian_codeindex.code_index.search_code_semantic` that adds exactly one
thing the extracted package cannot know for itself: the **hosted-mode
guard**. `search_code_semantic` reads `root_dir` off the local filesystem of
whatever process runs it; on hosted Meridian that process is the server,
which can never reach a caller's own machine, so the wrapper fails honestly
(`error: "...cannot run on hosted Meridian..."`) instead of letting the
underlying package silently mis-resolve a path against the server's own cwd
(workspace decision `0dedff91`). This guard is statically enforced by
`tests/test_no_local_fs_access.py`.

## Degraded / fallback signaling

Two independent "something is not fully fresh" signals exist, at two
different layers — do not conflate them:

- **`prospect_symbol`'s per-rung `status`/`fallback_reason`** answers "which
  rung produced these hits, and why did the others miss" (graph vs Serena vs
  local fallback).
- **`search_code_semantic`'s `convergence`/`degraded`** (from
  `CodeIndex.get_convergence_state()`, item `e631d54f`) answers "is the
  *local* index's optional vector leg fully caught up with its keyword leg
  right now" — always `degraded=False` when the vector leg is off (a pure
  BM25 result has no partial state to flag); `True` only when the vector leg
  is enabled but the last vector build didn't finish, some chunks still have
  no embedding, or the configured embedding model no longer matches the one
  that produced the persisted embeddings.

**Gap found and closed by this item:** `meridian.code_index.search_code_semantic`
used to build its own result dict by hand (`total_indexed` / `vectors_active`
/ `hits` only) instead of delegating to the extracted package's own
`search_code_semantic`, which silently dropped `convergence`/`degraded` on
every call — so neither the `search_code_semantic` MCP tool nor
`prospect_symbol`'s Rung 3 (`semantic_raw`) ever surfaced that state, even
though the underlying `CodeIndex` computed it every time. The shim now
delegates to the extracted package's function for the actual
index/search/convergence work and only layers the hosted-mode guard +
root_dir pre-normalization on top, so its result is a strict superset of what
it returned before. See `meridian/code_index.py::search_code_semantic` and
`tests/test_code_index.py` / `tests/test_prospect_symbol_and_graph_staleness.py`
(`test_semantic_rung_end_to_end_carries_convergence_and_degraded`, unmocked
end-to-end) for the regression coverage.

## Standalone usability without Meridian

`extensions/meridian-codeindex/meridian_codeindex/bm25_index.py` (sprint item
`58e64c86`, already shipped and covered by `tests/test_bm25_fallback.py`)
layers three more primitives on top of `CodeIndex`, all with the same
zero-Meridian-dependency posture:

- `lookup_exact(root_dir, path=..., content_hash=...)` — a ranking-free exact
  row match against the persisted chunk store, distinguishing `found=False`
  from `found=False, inconclusive=True` (walk didn't complete cleanly).
- `refresh_subtree(root_dir, subtree)` — bounds a reindex to one named
  subdirectory instead of walking the whole `root_dir`.
- `bm25_fallback_search` / `resolve_canonical_root` — worktree-aware root
  resolution plus an explicit `inconclusive` state for a directory walk that
  hit a real `OSError`, so an empty/short `hits` list is never silently read
  as an authoritative "nothing here."

These are **not** currently wired into either Meridian MCP tool — `prospect_symbol`
and `search_code_semantic` route through the plainer `CodeIndex.search` /
`get_convergence_state` path, not `bm25_index`. That is a deliberate scoping
choice, not an oversight: every capability `bm25_index.py` adds is already
reachable *right now*, with zero Meridian involvement, by anyone who
`pip install -e extensions/meridian-codeindex` and either runs the
`meridian-codeindex` CLI or `import meridian_codeindex` / `from
meridian_codeindex import bm25_index` directly — including from inside a
Claude Desktop session that has no Meridian MCP server configured at all.

## Why no standalone MCP adapter was built

The item's own bar for a new adapter was: does the existing route lack
**direct local root selection**, **bounded refresh/subtree**, **explicit
convergence/degraded state**, **exact path/hash lookup**, or **usable
standalone operation without the core Meridian server**? Checked against the
current code:

| Capability | Exists today? | Where |
|---|---|---|
| Direct local root selection | Yes | `root_dir` is the first argument everywhere in this stack |
| Bounded refresh/subtree | Yes | `CodeIndex.index_paths` / `bm25_index.refresh_subtree` |
| Explicit convergence/degraded state | Yes (now also reachable through the MCP route — see the gap above) | `CodeIndex.get_convergence_state` |
| Exact path/hash lookup | Yes | `bm25_index.lookup_exact` |
| Standalone operation without Meridian | Yes | own CLI + zero-dependency library, see above |

Every capability that would justify a *new* adapter already exists in the
codebase. The one place a capability existed but wasn't actually reachable
through Meridian's own MCP route (`convergence`/`degraded`) was a ~15-line
delegation fix in the existing shim, not a reason to stand up and maintain a
second MCP server process. Doing the latter would also cut against this
item's own explicit non-goals: it would mean maintaining a second tool
surface a client has to separately configure, and — for `lookup_exact` /
`refresh_subtree` specifically — there is no evidence today that MCP-tool
reachability (as opposed to direct library/CLI usability, which already
exists) is actually needed by a real caller.

**If that changes** (a real caller needs `lookup_exact` / `refresh_subtree`
from an MCP tool call, not a library import), the minimal correct move is
registering one or two additional tools on Meridian's *existing* MCP server
that call into `meridian_codeindex.bm25_index` directly — mirroring how
`search_code_semantic` and `prospect_symbol` are already registered in
`meridian/mcp/handler.py` — not a second, standalone server.
