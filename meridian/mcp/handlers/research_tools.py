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
