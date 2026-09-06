"""b924fd7c — recurring research watchlist on top of paper_search/github_search/
social_search: saved queries + diff-against-prior-findings.

Design (scoped subset — see the sprint item's discovery brief for the full ask
and what was deliberately deferred):

- **Saved queries ride on project_notes, not a new table.** A watchlist query is
  a ``kind='reference'`` note tagged ``research_watchlist,<source_type>`` whose
  body is a small JSON blob (``{source_type, query, limit, sort_by}``). This
  avoids a schema migration entirely — ``get_notes(tag='research_watchlist')``
  already makes it addressable/listable, and the note's own ``id`` IS the
  watchlist's stable identity (``watchlist_id``), so no separate uuid needs
  minting. ``add_project_note``'s ``kind`` column is a closed vocabulary
  (wiki/insight/reference/code/document, see meridian/db/__init__.py) — this
  reuses the existing ``'reference'`` value rather than widening that enum.

- **Diff-against-prior-findings via tags, not a new column.** Each newly-seen
  result is captured through the EXISTING :func:`meridian.db.save_finding`
  path unmodified (preserving its locked summary/url validation and
  ``finding,<type>[,decision:<id>]`` tag contract — see
  tests/test_ac4df52f_notes_decisions_dispatch.py /
  tests/test_cov_handler.py). Immediately after, this module appends
  ``watchlist:<id>,item:<key>`` onto that SAME note's tags via
  :func:`meridian.db.update_project_note` (a tags-replace, not an insert) so
  the next run can reconstruct the "already seen" set by reading tags alone
  (cheap: ``get_project_notes(..., bodies=False)`` already returns tags).

- **Per-source identity keys.** paper_search/github_search/social_search
  deliberately use different id field names per source
  (arxiv_id/openalex_id/s2_id/pmid/sha/hn_id) — see meridian/paper_search.py,
  meridian/github_search.py, meridian/social_search.py. ``_identity_key``
  picks the right field per source_type, falling back to ``url`` and then a
  content hash so every result is always diffable even when a source/result
  is missing its natural id (e.g. a malformed payload).

- **Search functions are called directly**, bypassing the ``paper_search`` MCP
  tool's dispatch (which today only routes 'arxiv'/'openalex' — see
  tests/test_paper_search.py's locked ``source`` enum). Calling
  ``meridian.paper_search.semantic_scholar_search``/``pubmed_search`` etc.
  directly is legitimate server-internal reuse of already-shipped, tested
  functions; it does not touch the paper_search tool's own schema/enum.
  ``author_search`` (a profile lookup, not a list-of-matches query) and the
  ``paper_search``/``social_search``/``github_search`` MCP tools' wiring gap
  for semantic_scholar/pubmed are explicitly OUT of scope here — flagged back
  to the sprint board as a separate follow-up per the discovery brief.

- **Deferred (documented in AGENTS.md, not built here):** scheduled/recurring
  execution (a host-level CronCreate/`schedule`-skill pairing calling
  ``run_watchlist_query`` on an interval — Meridian is a coordination store,
  not a second in-repo scheduler) and a cross-project aggregated view (would
  need a workspace-level query across projects' notes — a bigger, separate
  change).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from meridian import db as db_module
from meridian._deps import validate_input_size

# ---------------------------------------------------------------------------
# Source dispatch
# ---------------------------------------------------------------------------

# source_type -> the field in that source's result dict carrying its stable id.
# Mirrors the per-source field names paper_search.py/github_search.py/
# social_search.py already established (never unified, deliberately — see
# each module's own parse_*/​*_search docstrings).
_SOURCE_IDENTITY_FIELD: dict[str, str] = {
    "arxiv": "arxiv_id",
    "openalex": "openalex_id",
    "semantic_scholar": "s2_id",
    "pubmed": "pmid",
    "github_code": "sha",
    "github_repo": "repo",
    "hn": "hn_id",
}

# source_type -> save_finding's closed source_type vocabulary (web|arxiv|code|
# conversation, meridian/db/__init__.py:_FINDING_SOURCE_TYPES). Anything not
# listed here falls back to "web" (save_finding's own default), which is
# correct for openalex/semantic_scholar/pubmed/hn — none of those are "arxiv"
# or "code" in the sense save_finding means.
_SAVE_FINDING_SOURCE_TYPE: dict[str, str] = {
    "arxiv": "arxiv",
    "github_code": "code",
    "github_repo": "code",
}

_WATCHLIST_TAG = "research_watchlist"
_VALID_SORT_BY = ("relevance", "date")


def _resolve_search_fn(source_type: str) -> Any:
    """Return the callable ``<source>_search`` function for ``source_type``, or
    None for an unsupported one. Imported lazily (matching the existing
    research-module convention in research_tools.py/session_tools.py) so this
    module carries no import-time dependency on the network-search modules,
    and so tests can monkeypatch e.g. ``meridian.paper_search.arxiv_search``
    and have it take effect (the import below re-reads the module attribute
    at call time, not at module load time).
    """
    if source_type == "arxiv":
        from meridian.paper_search import arxiv_search as fn  # noqa: PLC0415
    elif source_type == "openalex":
        from meridian.paper_search import openalex_search as fn  # noqa: PLC0415
    elif source_type == "semantic_scholar":
        from meridian.paper_search import semantic_scholar_search as fn  # noqa: PLC0415
    elif source_type == "pubmed":
        from meridian.paper_search import pubmed_search as fn  # noqa: PLC0415
    elif source_type == "github_code":
        from meridian.github_search import github_code_search as fn  # noqa: PLC0415
    elif source_type == "github_repo":
        from meridian.github_search import github_repo_search as fn  # noqa: PLC0415
    elif source_type == "hn":
        from meridian.social_search import hn_search as fn  # noqa: PLC0415
    else:
        return None
    return fn


def _identity_key(source_type: str, item: dict[str, Any]) -> str:
    """Stable per-result dedup key for diffing across runs.

    Prefers the source's natural id field, falls back to ``url``, and finally
    to a content hash — so every result is always diffable even one with a
    blank/malformed id and no url (never returns an empty string, which would
    make every such item silently collide with every other).
    """
    field = _SOURCE_IDENTITY_FIELD.get(source_type, "")
    key = str(item.get(field) or "").strip() if field else ""
    if key:
        return f"{field}:{key}"
    url = str(item.get("url") or "").strip()
    if url:
        return f"url:{url}"
    digest = hashlib.sha256(
        json.dumps(item, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"hash:{digest}"


def _parse_tags(tags: str | None) -> list[str]:
    return [t.strip() for t in (tags or "").split(",") if t.strip()]


def _clamp_limit(raw: Any, default: int = 10) -> int:
    try:
        return max(1, min(int(raw or default), 50))
    except (TypeError, ValueError):
        return default


def _normalize_sort_by(raw: Any) -> str:
    val = str(raw or "relevance").strip().lower()
    return val if val in _VALID_SORT_BY else "relevance"


async def _load_watchlist_note(
    db: Any, project_id: str, watchlist_id: str
) -> dict[str, Any] | None:
    """Fetch a watchlist note, scoped to ``project_id`` and the watchlist tag —
    never returns a note belonging to another project or a plain non-watchlist
    note that happens to share an id lookup path."""
    note = await db_module.get_project_note(db, watchlist_id)
    if note is None:
        return None
    if note.get("project_id") != project_id:
        return None
    if _WATCHLIST_TAG not in _parse_tags(note.get("tags")):
        return None
    return note


# ---------------------------------------------------------------------------
# MCP tool handlers
# ---------------------------------------------------------------------------

async def handle_save_watchlist_query(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: save_watchlist_query.

    Persists a recurring research query as an addressable, project-scoped
    note (kind='reference', tagged research_watchlist + the source_type). The
    returned note's ``id`` IS the ``watchlist_id`` used by
    ``run_watchlist_query``/``list_watchlist_queries``/``delete_watchlist_query``.
    """
    validate_input_size(args.get("query"), "watchlist query", 2_000)
    validate_input_size(args.get("name"), "watchlist name", 200)
    project_id = args.get("project_id")
    if not project_id:
        return {"error": "save_watchlist_query requires project_id"}
    source_type = str(args.get("source_type") or "").strip().lower()
    if source_type not in _SOURCE_IDENTITY_FIELD:
        return {
            "error": "source_type must be one of: "
            + ", ".join(sorted(_SOURCE_IDENTITY_FIELD))
        }
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "save_watchlist_query requires a non-empty query"}
    limit = _clamp_limit(args.get("limit", 10))
    sort_by = _normalize_sort_by(args.get("sort_by"))
    label = (args.get("name") or "").strip()
    payload = {
        "source_type": source_type,
        "query": query,
        "limit": limit,
        "sort_by": sort_by,
    }
    title = f"Watchlist: {label or query}"[:200]
    note = await db_module.add_project_note(
        db, project_id, title, json.dumps(payload),
        tags=f"{_WATCHLIST_TAG},{source_type}",
        kind="reference",
    )
    return {"watchlist_id": note["id"], "note": note, **payload}


async def handle_list_watchlist_queries(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: list_watchlist_queries. Read-only. Optional ``source_type`` filter."""
    project_id = args.get("project_id")
    if not project_id:
        return {"error": "list_watchlist_queries requires project_id"}
    source_type_filter = str(args.get("source_type") or "").strip().lower() or None
    notes = await db_module.get_project_notes(
        db, project_id, tag=_WATCHLIST_TAG, bodies=True,
    )
    out: list[dict[str, Any]] = []
    for note in notes:
        try:
            payload = json.loads(note.get("body") or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if source_type_filter and payload.get("source_type") != source_type_filter:
            continue
        out.append({
            "watchlist_id": note.get("id"),
            "title": note.get("title"),
            "created_at": note.get("created_at"),
            "updated_at": note.get("updated_at"),
            **payload,
        })
    return {"count": len(out), "watchlists": out}


async def handle_delete_watchlist_query(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: delete_watchlist_query. Scoped to project_id + the watchlist
    tag so it can never delete an unrelated note by guessing/being given a
    stale id."""
    project_id = args.get("project_id")
    watchlist_id = args.get("watchlist_id")
    if not project_id or not watchlist_id:
        return {"error": "delete_watchlist_query requires project_id and watchlist_id"}
    note = await _load_watchlist_note(db, project_id, watchlist_id)
    if note is None:
        return {"error": "watchlist query not found"}
    ok = await db_module.delete_project_note(db, watchlist_id)
    return {"deleted": ok}


async def handle_run_watchlist_query(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: run_watchlist_query.

    Loads a saved watchlist query, re-runs its underlying search function
    directly (never through the paper_search/social_search/github_search MCP
    tools — see module docstring), diffs the results against everything
    already captured for this watchlist (via tags on prior
    ``save_finding``-created notes), and auto-captures each newly-seen result
    through the unmodified ``save_finding`` path before tagging it with this
    watchlist's identity for the next diff.

    Never raises: an unresolvable watchlist_id, an unsupported source_type, or
    a network/parse failure from the underlying search all degrade to
    ``{error, ...}`` — matching the non-raising convention every source
    module in the Research family already follows.
    """
    project_id = args.get("project_id")
    watchlist_id = args.get("watchlist_id")
    if not project_id or not watchlist_id:
        return {"error": "run_watchlist_query requires project_id and watchlist_id"}
    note = await _load_watchlist_note(db, project_id, watchlist_id)
    if note is None:
        return {"error": "watchlist query not found"}
    try:
        payload = json.loads(note.get("body") or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    source_type = str(payload.get("source_type") or "")
    query = str(payload.get("query") or "")
    limit = _clamp_limit(payload.get("limit", 10))
    sort_by = _normalize_sort_by(payload.get("sort_by"))

    search_fn = _resolve_search_fn(source_type)
    if search_fn is None:
        return {
            "error": f"unsupported watchlist source_type: {source_type!r}",
            "watchlist_id": watchlist_id,
        }

    result = await search_fn(query, limit=limit, sort_by=sort_by)
    if not isinstance(result, dict) or result.get("error"):
        # Network/parse failure from the underlying search function — degrade,
        # never raise (matches arxiv_search/openalex_search/github_code_search/
        # github_repo_search/hn_search's own non-raising convention).
        err = result.get("error") if isinstance(result, dict) else "search failed"
        return {
            "error": err,
            "watchlist_id": watchlist_id,
            "source_type": source_type,
            "query": query,
        }
    results = result.get("results") or []
    if not isinstance(results, list):
        results = []

    # Prior seen-keys: read tags only (bodies=False) — cheap, and tags alone
    # carry every "item:<key>" this watchlist has already captured.
    prior_notes = await db_module.get_project_notes(
        db, project_id, tag=f"watchlist:{watchlist_id}", bodies=False,
    )
    seen_keys: set[str] = set()
    for prior in prior_notes:
        for tag in _parse_tags(prior.get("tags")):
            if tag.startswith("item:"):
                seen_keys.add(tag[len("item:"):])

    new_results: list[dict[str, Any]] = []
    captured: list[dict[str, Any]] = []
    finding_source_type = _SAVE_FINDING_SOURCE_TYPE.get(source_type, "web")
    for item in results:
        if not isinstance(item, dict):
            continue
        key = _identity_key(source_type, item)
        if key in seen_keys:
            continue
        seen_keys.add(key)  # de-dup duplicates within the same response too
        new_results.append(item)

        title = str(item.get("title") or item.get("path") or "").strip() or "(untitled result)"
        excerpt = str(item.get("summary") or "").strip()
        summary_text = title if not excerpt else f"{title}\n\n{excerpt[:1000]}"
        source_url = str(item.get("url") or "").strip() or None
        try:
            saved = await db_module.save_finding(
                db, project_id, summary_text,
                source_url=source_url,
                source_type=finding_source_type,
            )
        except ValueError as exc:
            captured.append({"item_key": key, "title": title, "error": str(exc)})
            continue
        saved_note = saved.get("note") or {}
        note_id = saved_note.get("id")
        if note_id:
            existing_tags = saved_note.get("tags") or ""
            merged_tags = f"{existing_tags},watchlist:{watchlist_id},item:{key}"
            await db_module.update_project_note(db, note_id, tags=merged_tags)
        captured.append({"item_key": key, "title": title, "note_id": note_id})

    return {
        "watchlist_id": watchlist_id,
        "source_type": source_type,
        "query": query,
        "total_results": len(results),
        "new_count": len(new_results),
        "already_seen_count": len(results) - len(new_results),
        "new_results": new_results,
        "captured": captured,
    }
