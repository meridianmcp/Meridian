"""6d1abc98 — github_search: external prior-art / competitive-repo research.

Sibling to paper_search/social_search (handle_paper_search/handle_social_search,
still in :mod:`meridian.mcp.handlers.session_tools`), following the identical
per-source dispatch pattern: keyless external lookup, degrades to {error}, never
raises, no project scope needed. This is a NEW handler module (rather than adding
to session_tools.py) since the Research Module is growing its own family of
handlers; session_tools.py's paper_search/social_search handlers are left in place
to avoid an unrelated, purely-cosmetic house-move diff.

Two keyless GitHub endpoints via the 'type' param: 'code' (default; GitHub Code
Search — actual usage across public repos, distinct from search_code which only
searches the CALLING project's own connected repo) and 'repo' (GitHub Repository
Search — competitor/prior-art repositories by topic/description/stars).
"""
from __future__ import annotations

from typing import Any


async def handle_github_search(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: github_search.

    6d1abc98 — real callable GitHub search, distinct from search_code (which only
    searches the calling project's own connected repo). Keyless external lookup;
    degrades to {error}, never raises. No project scope needed — it's an external
    search. 'type' routes between two keyless GitHub endpoints: code (default) and
    repo. Both return the same {query, count, results} shape.
    """
    from meridian.github_search import github_code_search, github_repo_search  # noqa: PLC0415
    search_type = str(args.get("type", "code") or "code").strip().lower()
    search = github_repo_search if search_type == "repo" else github_code_search
    return await search(
        args.get("query", ""),
        limit=args.get("limit", 10),
        sort_by=args.get("sort_by", "relevance"),
    )


# ---------------------------------------------------------------------------
# a5343387 — bounded ephemeral research runs: an ADJACENT, project-scoped
# scratch/probe primitive for disposable subagent work that should not need
# a formal sprint item, a formal claim_file, or a durable handoff entry
# unless a caller explicitly promotes it. Model layer: meridian.research_run
# (validation). Persistence: meridian.db.research_runs — see that module's
# docstring for why the underlying table is named `scratch_research_runs`,
# not `research_runs` (that name already identifies an unrelated ML-style
# experiment-tracking table, meridian/db/experiment_model.py, 4376e655).
#
# Every handler here catches ValueError from the validation/persistence
# layer and returns {"error": ...} rather than letting it propagate —
# matches handle_create_project / handle_set_capability_manifest's
# established convention in this same dispatch family.
# ---------------------------------------------------------------------------


async def handle_start_research_run(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: start_research_run (a5343387).

    Read-only runs need no claim_file. Isolated-write runs require the
    caller to explicitly attest ``is_isolated_worktree=true`` (never
    inferred) and a non-empty ``allowed_paths`` list bounding what may be
    written.
    """
    from meridian.db import research_runs as run_db  # noqa: PLC0415

    project_id = args["project_id"]
    try:
        run = await run_db.start_research_run(
            db, project_id, args["session_id"],
            mode=args.get("mode"),
            repository_id=args.get("repository_id"),
            allowed_paths=args.get("allowed_paths"),
            turn_budget=args.get("turn_budget"),
            ttl_seconds=args.get("ttl_seconds"),
            is_isolated_worktree=bool(args.get("is_isolated_worktree", False)),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"run": run}


async def handle_complete_research_run(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: complete_research_run (a5343387).

    Idempotent on an already-terminal run — a duplicate call returns the
    existing terminal state, never an error. ``disposition`` is explicit and
    required (keep|discard|promote), never inferred.
    """
    from meridian.db import research_runs as run_db  # noqa: PLC0415

    project_id = args["project_id"]
    try:
        run = await run_db.complete_research_run(
            db, project_id, args["session_id"],
            run_id=args["run_id"],
            receipt=args.get("receipt") or {},
            disposition=args.get("disposition"),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"run": run}


async def handle_get_research_run(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_research_run (a5343387). Read-only."""
    from meridian.db import research_runs as run_db  # noqa: PLC0415

    project_id = args["project_id"]
    run = await run_db.get_research_run(db, project_id, run_id=args.get("run_id"))
    if run is None:
        return {"error": "research run not found in this project"}
    return {"run": run}


async def handle_list_research_runs(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: list_research_runs (a5343387). Read-only. By default
    terminal runs (completed/failed/abandoned/expired) are omitted so a
    fresh session sees only runs that may still be live."""
    from meridian.db import research_runs as run_db  # noqa: PLC0415

    project_id = args["project_id"]
    runs = await run_db.list_research_runs(
        db, project_id,
        include_terminal=bool(args.get("include_terminal", False)),
        status=args.get("status"),
        limit=args.get("limit", 100),
    )
    return {"runs": runs, "count": len(runs)}


async def handle_promote_research_run(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: promote_research_run (a5343387).

    Explicit only — never triggered by completion itself. Requires the run's
    stored ``disposition`` to already be ``'promote'`` (set at completion
    time). Creates a durable, addressable project finding summarizing the
    run's receipt — see meridian.db.research_runs.promote_research_run's
    docstring for why a finding is the chosen promotion target over a
    proposal update or a formal sprint item.
    """
    from meridian.db import research_runs as run_db  # noqa: PLC0415

    project_id = args["project_id"]
    try:
        result = await run_db.promote_research_run(
            db, project_id, args.get("session_id"), run_id=args["run_id"],
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return result
