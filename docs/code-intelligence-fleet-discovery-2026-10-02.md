# Code-intelligence fleet discovery — 2026-10-02

## Scope and method

This read-only inventory reconciles Codebase Memory project indexes, local project roots, per-root Serena configuration, the Meridian CodeIndex surface, and paper-search routing. Source-level discovery used Codebase Memory search_graph, get_code_snippet, trace_path, get_architecture, and query_graph against the registered project indexes. Local checks inspected Git root/HEAD and the presence of .serena/project.yml, .mcp.json, and the known data/code_index.duckdb sidecar; they did not read secrets or reindex any root.

The audit queried 24 registered Codebase Memory project names. All 24 returned architecture data and accepted a BM25-mode search request. This does not mean 24 distinct roots: ten project indexes report the Meridian Core checkout, and three report the same Masters Thesis code root. Thirteen project-name records therefore collapse to two local roots. Eight distinct roots could be tied to an extant Git checkout and/or an explicit graph canonical_root; five more graph records have no usable canonical-root metadata and remain unresolved.

Serena tools were not exposed to this Codex session. Serena config-file presence below is evidence of configuration only, not a healthy/live daemon. The hosted search_code_semantic MCP rejected all 12 read-only probes with the explicit message that its process cannot access the caller's local filesystem. No local CodeIndex reindex was attempted.

## Confirmed roots

Graph size is nodes/files. “Head match” compares the graph’s stored branch head_sha to the current local Git HEAD where both were available.

| Local root | Codebase Memory record(s) and graph size | Root and freshness evidence | Serena / local search |
|---|---|---|---|
| Meridian Core repository | C-Users-13144-Documents-Meridian-repository: 38,632 / 1,272. Nine additional project indexes also report this checkout; see duplicate-index findings. | Stored HEAD 5e113b5d matches the current checkout. root_exists=true. | Serena config and .mcp.json present. data/code_index.duckdb exists; its active root assignment and freshness were not verified. |
| DNABERT error correction | C-Users-13144-Documents-dnabert-error-correction: 65,663 / 408. | The graph’s branch node has blank canonical_root and head_sha; the DB name maps to the existing local Git root, whose HEAD is 6678fce. Index freshness cannot be confirmed. | Serena config and .mcp.json present. No standard CodeIndex sidecar found. |
| Masters Thesis code (CURRENT_PROJECT_CODE) | Three records: C-Users-13144-Documents-Masters_Thesis-CURRENT_PROJECT_CODE-width_baseline_generator (2,394 / 102), Camerer_MS_Graduation_2026 (3,592 / 133), and thesis-crack (2,126 / 83). | All three point to the same root and stored HEAD b277dff8, which matches local Git HEAD. Different graph sizes indicate separate snapshots/scopes despite the shared root and commit. | Serena config and .mcp.json present. No standard CodeIndex sidecar found. |
| Greenhouse Mapping | greenhouse-mapping: 640 / 28. | Stored HEAD 4a236a3d matches local Git HEAD. | Serena config and .mcp.json present. No standard CodeIndex sidecar found. |
| Advanced Perception | advanced_perception_stuff: 2,481 / 164. | Stored HEAD 39c60803 matches local Git HEAD. | Serena config and .mcp.json present. No standard CodeIndex sidecar found. |
| Cross-session study | meridian-cross-session-study: 2,331 / 317. | Stored HEAD 808e6e62 matches local Git HEAD. | No .serena/project.yml or .mcp.json at the root. |
| Meridian Outputs research | meridian-outputs-research: 579,714 / 29,254. | Stored HEAD 447d5557 does not match local HEAD 0d72a3d3. Treat this graph as stale until reindexed. Its unusually large file count also warrants a source/generated-data scope check. | No .serena/project.yml, .mcp.json, or standard CodeIndex sidecar found. |
| OOXML graph paper | ooxml-graph-paper: 775 / 11. | Stored HEAD 0165973e does not match local HEAD 048417e. Treat this graph as stale until reindexed. | .mcp.json present; no .serena/project.yml or standard CodeIndex sidecar found. |

## Unresolved graph roots

These five registered indexes are queryable, but their graph branch nodes have blank canonical_root values. Local candidate checks did not establish a current Git checkout for the indexed root. Do not route an executor to them as if they were verified project identities.

| Codebase Memory project | Graph size | What remains unresolved |
|---|---:|---|
| C-Users-13144-Documents-round3_interview | 492 / 41 | Graph root/HEAD blank. The same-named local directory exists but is not a Git root and has no Serena/MCP config. |
| chinampa-registration | 1,140 / 121 | Graph root/HEAD blank. The likely Documents/Chinampa candidate exists but is not a Git root; exact source path is unconfirmed. |
| gps-slam-itmlib | 1,723 / 136 | Graph root/HEAD blank; the checked PhD/Robotics candidate path does not exist. |
| gps-slam | 4,888 / 374 | Graph root/HEAD blank; the checked PhD/Robotics candidate path does not exist. |
| kensington-video-tools | 925 / 53 | Graph root/HEAD blank; no matching local root was identified in the checked project locations. |

## Index and search findings

- Codebase Memory uses BM25 for search_graph. The report did not preserve the exact shared query behind its original 21/24 hit count, so that rate is not reproducible and should not be treated as a verified metric. A fresh generic query returned hits in 18/24 indexes; zero-hit responses are query-specific and do not prove an empty or corrupt index. A follow-up project-specific query found 104 matches for Greenhouse Mapping and 6 for Meridian Outputs research. Round3 Interview and OOXML graph paper still need a known-symbol/query probe once their roots and index freshness are established.
- The ten Meridian Core project names are C-Users-13144-Documents-Meridian-repository, meridian-build-local, meridian-build, meridian-core-current, meridian-core, meridian-dev-crossref-core, Meridian-Docs-equation-contract-audit, meridian-docs-equation-graph-20260831, meridian-docs-release, and meridian-repo. Their stored branch SHAs differ, and several carry worktree markers. Keep them as separate snapshots until an alias registry explicitly resolves canonical root, worktree path, and indexed HEAD; do not pick one merely by a similar name.
- The three Masters Thesis indexes share a canonical root and HEAD but contain different file counts. This is duplicate-index drift even though their Git commit identity agrees.
- The local CodeIndex source caches instances by absolute root_dir plus db_path, and the default db_path is in-memory. A persistent DB schema has path and one metadata row but no root namespace. Reusing one persistent DB file across roots can therefore mix relative paths and metadata; use a distinct persistent sidecar per canonical root/worktree, or add explicit root partitioning before sharing a DB.
- Only the Meridian Core root had the standard local sidecar data/code_index.duckdb in the 13-root check. This is a path-presence observation, not a health/freshness result. The hosted Meridian tool cannot inspect local CodeIndex state; local search must run through the workstation helper/tunnel or a local executor and return explicit root, convergence, revision, and degraded state.
- The current Codex tool list contains no Serena operations. Five of the eight confirmed roots have .serena/project.yml; actual server registration, project activation, and daemon health are unverified. A setup/doctor flow should distinguish “config file exists” from “Serena is connected to this exact root.”

## Research-search surfaces

The registered paper_search handler routes six explicit sources: arXiv, OpenAlex, Semantic Scholar, PubMed, Crossref, and CORE. arXiv is tried first; for recognized availability/format failures it falls back to OpenAlex and then Semantic Scholar, and reports the fallback source and warning. Direct OpenAlex search is available separately, uses a free keyless path with an optional OPENALEX_API_KEY, and returns errors instead of raising. The MCP handler is an external search and does not require a project ID. These are distinct from local code-search indexes and should be represented as research providers in the workstation health view.

## Handoff to canonical-root enforcement

The next implementation item should use this inventory to make root identity and health explicit. At minimum, every index/tool result should bind to: canonical root, linked-worktree path and common Git dir, current HEAD and indexed HEAD, root-exists status, index database path/namespace, Serena config fingerprint and daemon health, BM25/vector availability, index revision/checkpoint, and a truthful stale/partial/error state. A missing identity or unavailable local filesystem must be reported as unknown/unavailable, never as an empty healthy search.

Before enabling broad discovery, resolve the five pathless graph records and collapse or label the duplicate Core/Thesis snapshots without destroying useful historical indexes. Keep root discovery read-only by default and exclude worktrees, generated outputs, caches, and ignored paths unless the user explicitly selects them.


## Source anchors

- Root-scoped CodeIndex behavior and persistent schema: extensions/meridian-codeindex/meridian_codeindex/code_index.py and extensions/meridian-codeindex/meridian_codeindex/bm25_index.py.
- Meridian bounded index preflight: meridian/code_index.py.
- Per-repo Serena pool identity and health diagnostics: meridian/serena_pool.py.
- Paper-search source routing: meridian/mcp/handlers/session_tools.py and meridian/paper_search.py.
