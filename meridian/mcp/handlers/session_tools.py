"""Per-tool handlers extracted from _handle_session_tools (81abd31f).

Each function corresponds to exactly one MCP tool from the
``_handle_session_tools`` dispatch group in ``meridian/mcp/handler.py``.
The extraction is PURELY MECHANICAL: zero behaviour change, same tool names,
same arguments, same return values.

Callers (handler.py) assemble these into a dispatch table
(dict[str, Callable]) and invoke the matched function instead of walking
the original if/elif chain.  Functions that need cross-cutting handler-level
state (e.g. ``_fetch_recent_commits``, ``_resolve_caller_identity``) receive
that state as explicit keyword arguments to keep the import graph acyclic.
"""
from __future__ import annotations

import asyncio
from typing import Any, TYPE_CHECKING

import meridian.server as _server
from meridian import db as db_module
from meridian._deps import validate_input_size

if TYPE_CHECKING:
    from collections.abc import Callable


async def handle_checkpoint(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
    *,
    fetch_recent_commits: "Callable[..., Any]",
    resolve_caller_identity: "Callable[..., Any]",
) -> Any:
    """MCP tool: checkpoint."""
    session_id = args["session_id"]
    project_id = args["project_id"]
    await db_module.auto_capture_session(db, project_id, session_id)
    await _server._finalize_session_md(db, project_id, session_id)
    from meridian import handoff as handoff_module_local  # noqa: PLC0415  # local import — avoid top-level cycle
    # Fetch recent commits for reconcile annotations (non-fatal)
    _ckpt_project = await db_module.get_project(db, project_id)
    _commits = await fetch_recent_commits(_ckpt_project or {}, tenant)
    try:
        _, content, _ = await asyncio.wait_for(
            handoff_module_local.generate_handoff(
                db, project_id, data_dir, mode="delta", session_id=session_id,
                commit_messages=[c["message"] for c in _commits],
                identity=resolve_caller_identity(tenant),
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        content = "delta handoff timed out"
    pending_items = await db_module.get_sprint_items(db, project_id, status="pending")
    # Log drift warnings for high-confidence reconcile matches (non-fatal)
    if _commits and pending_items:
        try:
            _matches = handoff_module_local.reconcile_sprint_items(
                pending_items, _commits
            )
            for _m in _matches:
                if _m.get("confidence") == "high":
                    _first_sha = (_m["matching_commits"][0].get("sha") or "")[:8]
                    await db_module.log_task(
                        db, session_id, project_id,
                        f"Sprint board drift detected: {_m['item_id'][:8]} "
                        f"may already be done (matches commit {_first_sha})",
                        status="pending",
                    )
        except Exception:  # noqa: BLE001
            pass
    ids_str = ", ".join(it["id"][:8] for it in pending_items[:8])
    next_goal = (
        f'/goal Complete sprint items: {", ".join(it["id"] for it in pending_items[:8])}. '
        f"Done when complete_sprint_item()\'d, tests pass, generate_handoff called."
    ) if pending_items else "/goal Continue work — all sprint items done."
    # 04f03ee4 — include start_session one-liner so next session can resume immediately
    # 11a91d31 — default to project_name (idiomatic per 8a449ec0); project_id as a comment.
    try:
        _ck_proj = await db_module.get_project(db, project_id)
        _ck_pname = (_ck_proj or {}).get("name") or project_id
    except Exception:  # noqa: BLE001
        _ck_pname = project_id
    start_fresh = (
        f'start_session(project_name="{_ck_pname}", session_name="describe-what-youre-doing")'
        f'  # project_id={project_id}'
    )
    # fa595ad8 — store snapshot for Recent Sessions dashboard panel (non-fatal)
    # v3.1 — snapshot now lives on sessions.checkpoint_data, not a checkpoint:* note.
    try:
        from datetime import datetime as _ckpt_dt, timezone as _ckpt_tz  # noqa: PLC0415
        async with db.execute(
            "SELECT name FROM sessions WHERE id = ?", (session_id,)
        ) as _sc:
            _sr = await _sc.fetchone()
        _session_name = (_sr["name"] if _sr else None) or session_id[:8]
        async with db.execute(
            "SELECT COUNT(*) AS n FROM task_log "
            "WHERE session_id = ? AND status = 'done'",
            (session_id,),
        ) as _tc:
            _tr = await _tc.fetchone()
        _items_done = int(_tr["n"]) if _tr else 0
        _summary_line = (content or "").split("\n")[0][:140]
        await db_module.set_session_checkpoint(
            db, session_id,
            {
                "session_id": session_id,
                "session_name": _session_name,
                "items_done": _items_done,
                "summary_line": _summary_line,
                "next_goal": next_goal,
                "start_fresh": start_fresh,
                "checkpointed_at": _ckpt_dt.now(_ckpt_tz.utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            },
        )
    except Exception:
        pass  # non-fatal — checkpoint still returns normally
    # Write plain-text session_summary for RECENT RUNS panel display
    try:
        _shipped_titles: list[str] = []
        async with db.execute(
            "SELECT si.title FROM sprint_items si "
            "JOIN task_log tl ON tl.id = si.task_id "
            "WHERE tl.session_id = ? AND si.status = 'done'",
            (session_id,),
        ) as _si_cur:
            for _si_row in await _si_cur.fetchall():
                _t = _si_row["title"] if hasattr(_si_row, "__getitem__") else _si_row[0]
                if _t not in _shipped_titles:
                    _shipped_titles.append(_t)
        async with db.execute(
            "SELECT DISTINCT si.title FROM sprint_items si "
            "JOIN task_log tl ON tl.sprint_item_id = si.id "
            "WHERE tl.session_id = ? AND tl.status = 'done' AND si.status = 'done'",
            (session_id,),
        ) as _si_cur2:
            for _si_row2 in await _si_cur2.fetchall():
                _t2 = _si_row2["title"] if hasattr(_si_row2, "__getitem__") else _si_row2[0]
                if _t2 not in _shipped_titles:
                    _shipped_titles.append(_t2)
        _shipped_str = ", ".join(_shipped_titles) if _shipped_titles else "none"
        _plain_summary = (
            f"Shipped: {_shipped_str}. "
            f"Tasks done: {_items_done}. "
            f"Deploy: no."
        )
        await db.execute(
            "UPDATE sessions SET session_summary = ? WHERE id = ?",
            (_plain_summary, session_id),
        )
    except Exception:
        pass  # non-fatal
    # 1c4fdd6c — sprint-drift guard: sweep in_progress items and surface them
    # so the executor confirms done/not-done before the handoff is final
    # (the #1 cause of board drift is forgetting complete_sprint_item).
    _in_progress = await db_module.get_sprint_items(
        db, project_id, status="in_progress"
    )
    _ckpt_resp = {
        "summary": content,
        "pending_count": len(pending_items),
        "pending_ids": ids_str,
        "next_goal": next_goal,
        "start_fresh": start_fresh,
    }
    if _in_progress:
        _ckpt_resp["in_progress_items"] = [
            {"id": it["id"], "title": (it.get("title") or "")[:80]}
            for it in _in_progress
        ]
        _n = len(_in_progress)
        _ckpt_resp["action_required"] = (
            f"You have {_n} in_progress sprint item{'s' if _n != 1 else ''}. "
            "Before this handoff is final, confirm each: call "
            "complete_sprint_item(item_id) for any that shipped, or leave it "
            "in_progress / fail_sprint_item(item_id, reason) if not done. "
            "These are NOT auto-reconciled from git — you must mark them."
        )
    return _ckpt_resp


async def handle_get_context_block(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_context_block.

    v2.3 — assemble the same shape as /projects/{id}/context-block but
    return both the rendered text AND the source dict so MCP clients
    can choose to render their own variant.
    v2.5 — wrap in semantic XML for better Claude Code parsing. The
    returned 'text' field is XML-wrapped:
      <meridian_context project_id="..." mode="...">
        ... plain-text context ...
      </meridian_context>
    The HTTP route /projects/{id}/context-block returns the same content
    as unwrapped plain text (suitable for clipboard paste).
    """
    project_id = args["project_id"]
    mode = args.get("mode", "full")
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise ValueError("project not found")
    goal = await db_module.get_goal(db, project_id)
    sprint_items = await db_module.get_sprint_items(
        db, project_id, status="pending"
    )
    all_tasks = await db_module.get_tasks(db, project_id, limit=20)
    pending_tasks = [
        t for t in all_tasks if t.get("status") in ("pending", "in_progress", "done")
    ][:10]
    sessions = await db_module.get_sessions(db, project_id, active_only=True)
    decisions_raw = (project.get("decisions") or "").strip()
    recent_decisions = [
        l.strip() for l in decisions_raw.splitlines() if l.strip()
    ][-5:]
    text = _server._render_context_block(
        project, goal, sprint_items, pending_tasks, sessions, recent_decisions,
        mode=mode,
    )
    # v3.1 — workspace decisions + notes apply across all projects; surface
    # them at the very top so a fresh session sees org-wide truth first.
    ws_decisions = await db_module.get_workspace_decisions(db, tenant_id=_mcp_tenant_id)
    ws_notes = await db_module.get_workspace_notes(db, tenant_id=_mcp_tenant_id)
    ws_block = _server._render_workspace_block(ws_decisions, ws_notes)
    if ws_block:
        text = f"{ws_block}\n\n{text}"
    xml_text = f'<meridian_context project_id="{project_id}" mode="{mode}">\n{text}\n</meridian_context>'
    return {"mode": mode, "text": xml_text, "project_id": project_id}


async def handle_list_sessions(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: list_sessions."""
    active_only = args.get("status", "active") != "all"
    return await db_module.get_sessions(db, args["project_id"], active_only=active_only)


async def handle_get_session_log(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_session_log.

    8c147109 — include the ring-buffer activity feed so a remote planner
    sees real signs of life even before the executor calls log_task().
    """
    _log_sid = args.get("session_id", "")
    run = await db_module.get_executor_run_by_session(db, _log_sid)
    if run is None:
        return {"error": "no run found for session"}
    _activity: list[dict] = []
    try:
        _activity = await db_module.get_session_activity(
            db, _log_sid, limit=args.get("activity_limit", 20)
        )
    except Exception:  # noqa: BLE001
        pass
    return {
        "run_id": run["id"],
        "session_id": run["session_id"],
        "started_at": run["started_at"],
        "ended_at": run.get("ended_at"),
        "status": run["status"],
        "task_count": run["task_count"],
        "transcript": run["transcript"],
        "recent_activity": _activity,
        "activity_note": (
            "recent_activity is a ring-buffer of the last tool calls made by "
            "this executor session (newest first, max 50 entries). It is "
            "populated automatically by the MCP dispatcher — no explicit "
            "log_task() call is needed."
        ),
    }


async def handle_get_session_activity(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_session_activity.

    8c147109 — standalone tool so planners can fetch just the heartbeat
    feed without the full transcript when polling for liveness.
    """
    _ga_sid = args.get("session_id", "")
    _ga_limit = min(int(args.get("limit", 20)), 50)
    try:
        _ga_entries = await db_module.get_session_activity(db, _ga_sid, limit=_ga_limit)
    except Exception:  # noqa: BLE001
        _ga_entries = []
    return {
        "session_id": _ga_sid,
        "activity": _ga_entries,
        "count": len(_ga_entries),
    }


async def handle_get_connection_log(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_connection_log.

    b12cc29f — returns the /mcp connection-event log for this tenant.
    Scoped to the calling tenant's id so a tenant can only read their own log.
    Self-hosted (no tenant) callers get the full log (no tenant filter).
    """
    _since = (args.get("since") or "").strip() or None
    _limit = min(int(args.get("limit", 100)), 200)
    try:
        _events = await db_module.get_connection_log(
            db,
            tenant_id=_mcp_tenant_id,
            since=_since,
            limit=_limit,
        )
    except Exception:  # noqa: BLE001 — degrade gracefully, never surface a DB error here
        _events = []
    return {
        "tenant_id": _mcp_tenant_id,
        "since": _since,
        "count": len(_events),
        "events": _events,
    }


async def handle_get_server_logs(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_server_logs.

    f0a48685 — returns recent application-level WARNING/ERROR/EXCEPTION log
    records from the server_logs ring-buffer.  Unlike get_connection_log (which
    is scoped per-tenant by /mcp request metadata), server_logs are process-global
    and not scoped by tenant_id.  Any authenticated caller can read the full log
    — this is intentional, since server errors are not tenant-private data and the
    most common use case is incident diagnosis from a hosted-only session.

    b241a437 — positional seeking: when ``seek_to`` is provided and the
    checkpoint index is warm, we derive a tight ``since=`` hint from the index
    and pass it to the DB query, skipping earlier rows without a full scan.
    Falls back to a normal query when the index is empty or the hint is None.
    """
    _since = (args.get("since") or "").strip() or None
    _limit = min(int(args.get("limit", 100)), 500)
    _level = (args.get("level_filter") or "").strip().upper() or None
    _module = (args.get("module_filter") or "").strip() or None

    # b241a437: positional seek — use the checkpoint index to derive a tight
    # since= hint when the caller supplies seek_to=<timestamp>.
    _seek_to = (args.get("seek_to") or "").strip() or None
    if _seek_to and _since is None:
        from meridian import server_log_checkpoint as _slc  # noqa: PLC0415
        _hint = _slc.seek_hint_for(_seek_to)
        if _hint:
            _since = _hint

    try:
        _entries = await db_module.get_server_logs(
            db,
            since=_since,
            limit=_limit,
            level_filter=_level,
            module_filter=_module,
        )
    except Exception:  # noqa: BLE001 — degrade gracefully, never surface a DB error
        _entries = []

    # b241a437: keep the checkpoint index warm — rebuild it from a full
    # snapshot whenever we have an unfiltered fetch (i.e. no level/module
    # filters and limit is large) so subsequent seek_to calls are fast.
    # We use a fire-and-forget pattern (best-effort, never blocks the response).
    if not _level and not _module and _limit >= 500:
        try:
            from meridian import server_log_checkpoint as _slc2  # noqa: PLC0415
            _slc2.build_checkpoint(_entries)
        except Exception:  # noqa: BLE001
            pass

    return {
        "count": len(_entries),
        "since": _since,
        "level_filter": _level,
        "module_filter": _module,
        "entries": _entries,
    }


async def handle_search_server_logs(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: search_server_logs.

    222d54f8 — BM25 full-text search over the server_logs ring-buffer using
    DuckDB native FTS.  Fetches the complete current snapshot of server_logs
    from the DB (async, here), then hands off to the synchronous
    :mod:`meridian.server_log_index` module which manages the DuckDB sidecar.

    Design: the DB fetch is async (standard Meridian DB pattern); the DuckDB
    FTS work is synchronous (DuckDB has its own thread safety model).  We avoid
    asyncio.run_in_executor because the index is tiny (max 2000 rows) and the
    BM25 search is sub-millisecond.
    """
    _query = (args.get("query") or "").strip()
    if not _query:
        return {"query": "", "total_in_index": 0, "count": 0, "hits": [],
                "error": "query is required"}
    _since = (args.get("since") or "").strip() or None
    _level = (args.get("level") or "").strip().upper() or None
    _limit = max(1, min(int(args.get("limit", 20)), 200))

    # Fetch the complete current server_logs snapshot (no filters -- we want
    # all rows so the FTS index is fully synced, and BM25 + post-filters handle
    # narrowing).  The ring-buffer is at most 2000 rows so this is always cheap.
    try:
        _all_rows = await db_module.get_server_logs(db, limit=2000)
    except Exception:  # noqa: BLE001
        _all_rows = []

    from meridian import server_log_index as _sli  # noqa: PLC0415
    import os  # noqa: PLC0415
    _db_path = ":memory:"
    if data_dir:
        _db_path = os.path.join(data_dir, "server_log_index.duckdb")

    try:
        _result = _sli.search_server_logs(
            _all_rows,
            _query,
            limit=_limit,
            level=_level,
            since=_since,
            db_path=_db_path,
        )
    except Exception:  # noqa: BLE001 — degrade gracefully, never surface a DB error
        _result = {"query": _query, "total_in_index": 0, "count": 0, "hits": []}

    # b241a437: keep the checkpoint index warm from the full snapshot we just
    # fetched (no extra DB round-trip needed -- we already have all rows).
    try:
        from meridian import server_log_checkpoint as _slc  # noqa: PLC0415
        _slc.build_checkpoint(_all_rows)
    except Exception:  # noqa: BLE001
        pass

    return _result


async def handle_get_server_log_checkpoint(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_server_log_checkpoint.

    b241a437 -- return the current positional/checkpoint index for the
    server_logs ring-buffer.

    The checkpoint is a lightweight 'table of contents' mapping minute-level
    timestamp buckets to the first/last row id and row count in that bucket.
    Callers can use this to navigate large log windows efficiently:

    1. Call this tool to get the bucket list.
    2. Find the bucket at or just before the target timestamp.
    3. Use its ``min_recorded_at`` as the ``since=`` argument to
       ``get_server_logs`` -- the DB query will skip all older rows.

    The index is rebuilt from the in-memory snapshot (at most 2000 rows) by
    ``get_server_logs`` and ``search_server_logs`` on every call, so it is
    always up-to-date with the current ring-buffer state.  When no log rows
    have been fetched yet in this process (fresh start), the index is empty
    and ``bucket_count`` will be 0 -- callers should fall back to a full
    ``get_server_logs`` scan.

    The optional ``seek_to`` argument is a convenience shortcut: when provided,
    the tool returns the best ``since`` hint for that timestamp directly in the
    response field ``seek_hint``, saving the caller a manual bucket scan.

    Returns::

        {
            "total_rows":              int,   -- rows in the current ring-buffer snapshot
            "bucket_granularity_label": str,  -- e.g. "minute"
            "min_recorded_at":         str | null,
            "max_recorded_at":         str | null,
            "bucket_count":            int,
            "buckets": [
                {
                    "bucket":           str,  -- "YYYY-MM-DD HH:MM"
                    "count":            int,
                    "min_recorded_at":  str,
                    "max_recorded_at":  str,
                    "first_id":         str,
                    "last_id":          str,
                },
                ...                            -- oldest-first
            ],
            "seek_hint": str | null,  -- only present when seek_to= was given
        }
    """
    _seek_to = (args.get("seek_to") or "").strip() or None

    # If the checkpoint is empty (no rows seen yet), do a lazy full fetch to
    # populate it so this tool is self-warming on first call.
    from meridian import server_log_checkpoint as _slc  # noqa: PLC0415
    _current = _slc.get_checkpoint_dict()
    if _current.get("total_rows", 0) == 0:
        try:
            _all_rows = await db_module.get_server_logs(db, limit=2000)
            _slc.build_checkpoint(_all_rows)
            _current = _slc.get_checkpoint_dict()
        except Exception:  # noqa: BLE001
            pass

    result: dict[str, Any] = dict(_current)

    if _seek_to:
        result["seek_hint"] = _slc.seek_hint_for(_seek_to)

    return result


async def handle_get_agent_instructions(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_agent_instructions."""
    instructions = await db_module.get_agent_instructions(db, args["project_id"])
    return {"project_id": args["project_id"], "agent_instructions": instructions}


async def handle_set_agent_instructions(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: set_agent_instructions."""
    validate_input_size(args.get("instructions"), "agent_instructions", 100_000)
    instructions = (args.get("instructions") or "").strip() or None
    return await db_module.set_agent_instructions(db, args["project_id"], instructions)


async def handle_set_executor_config(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: set_executor_config.

    Merge onto the existing config so we never wipe other keys (repo_paths,
    hostnames, filesystem_roots, ...). repo_paths is merged entry-by-entry so
    a manual {cwd, hostname} coexists with hook-registered ones.
    """
    from meridian.executor_config import merge_repo_paths  # noqa: PLC0415 — avoid import cycle
    # Merge onto the existing config so we never wipe other keys (repo_paths,
    # hostnames, filesystem_roots, …). repo_paths is merged entry-by-entry so
    # a manual {cwd, hostname} coexists with hook-registered ones.
    existing = await db_module.get_executor_config(db, args["project_id"])
    cfg = dict(existing) if isinstance(existing, dict) else {}
    # 3adbc954 — filesystem_roots, hostnames, context_threshold and isolation
    # were missing from this copy list, so passing them was silently dropped
    # (the call returned filesystem_roots: []). All scalar/list keys overwrite;
    # repo_paths alone is merged entry-by-entry below.
    for k in ("repo_path", "env_file", "test_cmd", "test_min",
              "deploy_cmd", "shell_type", "branch",
              "filesystem_roots", "hostnames", "context_threshold",
              "isolation", "max_turns",
              # b970fe07 — dashboard-configurable Serena default repo + code-intel
              # index dirs (mirror filesystem_roots: scalar/list overwrite).
              "serena_repo_path", "codebase_code_dirs"):
        if k in args:
            cfg[k] = args[k]
    if "repo_paths" in args:
        cfg["repo_paths"] = merge_repo_paths(cfg.get("repo_paths"), args["repo_paths"])
    return await db_module.set_executor_config(db, args["project_id"], cfg)


async def handle_idle_until_session_done(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: idle_until_session_done."""
    _idle_kwargs: dict[str, Any] = {}
    if args.get("timeout_seconds") is not None:
        _idle_kwargs["timeout_seconds"] = float(args["timeout_seconds"])
    return await _server._idle_until_session_done(
        db, args["watching_session_id"], **_idle_kwargs
    )


async def handle_search_all(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: search_all."""
    return await db_module.search_all(
        db, args["project_id"], args["query"],
        limit=args.get("limit", 10),
    )


async def handle_search_synthesis(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: search_synthesis.

    ebc242ad — a natural-language query gets a short, CITED answer on top of
    the same tsvector/LIKE retrieval as search_all, not just a list of hits.
    Reuses the Haiku-tier pattern (deterministic fallback to raw results when
    ANTHROPIC_API_KEY is unset or the call fails).
    """
    if not args.get("query"):
        return {"error": "query is required"}
    results = await db_module.search_all(
        db, args["project_id"], args["query"], limit=args.get("limit", 10),
    )
    from meridian.handoff import synthesize_search_answer  # noqa: PLC0415
    synth = await synthesize_search_answer(args["query"], results)
    return {"query": args["query"], **synth, "results": results}


async def handle_paper_search(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: paper_search.

    811881c6 — real callable arXiv search so the research-routing protocol's
    "use the paper-search MCP first" finally points at a tool that exists (it was
    instruction-only before). Keyless external lookup; degrades to {error}, never
    raises. No project scope needed — it's an external search.
    f65f6111 — 'source' routes between two keyless sources: arxiv (default) and
    openalex. Both return the same {query, count, results} shape.
    """
    from meridian.paper_search import arxiv_search, openalex_search  # noqa: PLC0415
    source = str(args.get("source", "arxiv") or "arxiv").strip().lower()
    search = openalex_search if source == "openalex" else arxiv_search
    return await search(
        args.get("query", ""),
        limit=args.get("limit", 10),
        sort_by=args.get("sort_by", "relevance"),
    )


async def handle_get_session_brief(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_session_brief.

    v2.5 — single-call orientation, <500 tokens, XML output.
    """
    project_id = args["project_id"]
    role = args.get("role", "worker")
    session_id_for_notes = args.get("session_id")
    goal = await db_module.get_goal(db, project_id)
    tasks = await db_module.get_tasks(db, project_id, limit=5)
    # dcf1e428 — use 'recent' to surface pending + answered last 24h for planning sessions
    hitl_rows = await db_module.list_hitl_requests(db, project_id, status="recent")
    sprint_items = await db_module.get_sprint_items(db, project_id, status="pending")
    # 0507f4a1 — sprint progress summary for session brief
    _all_items_for_progress = await db_module.get_sprint_items(db, project_id)
    _done_count = sum(1 for it in _all_items_for_progress if it.get("status") == "done")
    _total_count = len(_all_items_for_progress)
    _pct = round(100 * _done_count / _total_count) if _total_count else 0
    blocking = [t for t in tasks if t.get("status") == "failed"]
    sprint_str = (goal.get("sprint") or "") if goal else ""
    tasks_xml = "\n".join(
        f'  <task status="{t.get("status","?")}">{(t.get("description") or "")[:80]}</task>'
        for t in tasks
    )
    sprint_items_xml = "\n".join(
        f'  <item version="{it.get("version","")}">{(it.get("title") or "")[:80]}</item>'
        for it in sprint_items[:5]
    )
    # 277567dc — surface pending HITLs so session sees what needs a human decision.
    # dcf1e428 — also surface recently answered HITLs so planning sessions can see what was decided.
    _pending_hitls = [h for h in hitl_rows if h.get("status") == "pending"]
    _answered_hitls = [h for h in hitl_rows if h.get("status") != "pending"]
    if _pending_hitls:
        hitl_xml = (
            f'<hitl_pending count="{len(_pending_hitls)}">\n'
            + "\n".join(
                f'  <request id="{h.get("id","")}" urgency="{h.get("urgency","normal")}">'
                f'{(h.get("question") or "")[:140]}</request>'
                for h in _pending_hitls[:5]
            )
            + "\n</hitl_pending>"
        )
    else:
        hitl_xml = ""
    if _answered_hitls:
        hitl_xml += (
            f'\n<hitl_recent count="{len(_answered_hitls)}">\n'
            + "\n".join(
                f'  <request status="{h.get("status","?")}">'
                f'Q: {(h.get("question") or "")[:80]} '
                f'A: {(h.get("answer") or "")[:80]}</request>'
                for h in _answered_hitls[:3]
            )
            + "\n</hitl_recent>"
        )
    blocking_xml = f'<blocking>{(blocking[0].get("description") or "")[:100]}</blocking>' if blocking else ""
    # v2.6 — include session scratch-pad notes at top of brief
    notes_xml = ""
    new_items_xml = ""
    if session_id_for_notes:
        try:
            session_notes = await db_module.get_session_notes(db, session_id_for_notes)
            if session_notes:
                notes_xml = "<session_notes>\n" + "\n".join(
                    f'  <note title="{n.get("title","")}">{(n.get("body") or "")[:120]}</note>'
                    for n in session_notes
                ) + "\n</session_notes>\n"
        except Exception:
            pass
        # fd86aacc — show items added to the board since this session started
        try:
            _all_sess = await db_module.get_sessions(db, project_id, active_only=False)
            _curr_sess = next(
                (s for s in _all_sess if s.get("id") == session_id_for_notes), None
            )
            if _curr_sess and _curr_sess.get("created_at"):
                _sess_started = str(_curr_sess["created_at"])
                _all_items = await db_module.get_sprint_items(db, project_id)
                _new_count = sum(
                    1 for it in _all_items
                    if (it.get("added_at") or "") >= _sess_started
                )
                if _new_count > 0:
                    new_items_xml = (
                        f'<board_change>{_new_count} item{"s" if _new_count != 1 else ""}'
                        f' added since this session started</board_change>\n'
                    )
        except Exception:
            pass
    _progress_xml = (
        f'<progress done="{_done_count}" total="{_total_count}" pct="{_pct}%"/>\n'
        if _total_count else ""
    )
    # Sprint-4: planner role gets richer context — all decisions + all notes + active sessions.
    planner_extra_xml = ""
    if role == "planner":
        try:
            _decisions = await db_module.get_pinned_decisions(db, project_id, include_superseded=False)
            if _decisions:
                # 366317e9 — already ordered urgent → normal → low by the DB
                # layer; surface the priority so the planner weights them.
                planner_extra_xml += "<decisions>\n" + "\n".join(
                    f'  <decision priority="{d.get("priority","normal")}" category="{d.get("category","")}">{(d.get("title") or "")}: {(d.get("body") or "")[:120]}</decision>'
                    for d in _decisions[:20]
                ) + "\n</decisions>\n"
        except Exception:
            pass
        try:
            # 5a5bba43 — planner context renders note bodies, so ask for them.
            _wiki_notes = await db_module.get_project_notes(db, project_id, bodies=True)
            # High-priority notes first
            _wiki_notes = sorted(_wiki_notes, key=lambda n: {"high": 0, "normal": 1, "low": 2}.get(n.get("priority", "normal"), 1))
            if _wiki_notes:
                planner_extra_xml += "<project_notes>\n" + "\n".join(
                    f'  <note priority="{n.get("priority","normal")}" kind="{n.get("note_kind","wiki")}" tags="{n.get("tags","")}">'
                    f'{(n.get("title") or "")}: {(n.get("body") or "")[:120]}</note>'
                    for n in _wiki_notes[:30]
                ) + "\n</project_notes>\n"
        except Exception:
            pass
        try:
            _active_sessions = await db_module.get_sessions(db, project_id, active_only=True)
            if _active_sessions:
                planner_extra_xml += "<active_sessions>\n" + "\n".join(
                    f'  <session id="{s.get("id","")}" name="{s.get("name","")}" human="{s.get("human_id","")}" last_seen="{s.get("last_seen","")[:19]}"/>'
                    for s in _active_sessions[:10]
                ) + "\n</active_sessions>\n"
        except Exception:
            pass
    # 1750dccf — role-specific enrichment so executor and planner briefs differ.
    role_extra_xml = ""
    if role == "executor":
        # Version-scoped pending items + this session's file claims + the
        # decisions code-anchored to the files it holds.
        _sess_row = None
        if session_id_for_notes:
            _sess_all_x = await db_module.get_sessions(
                db, project_id, active_only=False
            )
            _sess_row = next(
                (s for s in _sess_all_x if s.get("id") == session_id_for_notes),
                None,
            )
        _ver = (_sess_row or {}).get("sprint_version")
        if _ver:
            _scoped = [it for it in sprint_items if it.get("version") == _ver]
            role_extra_xml += (
                f'<version_scope version="{_ver}" pending="{len(_scoped)}">\n'
                + "\n".join(
                    f'  <item>{(it.get("title") or "")[:80]}</item>'
                    for it in _scoped[:5]
                )
                + "\n</version_scope>\n"
            )
        if session_id_for_notes:
            try:
                _claimed = await db_module.get_session_file_claims(
                    db, session_id_for_notes
                )
            except Exception:
                _claimed = []
            if _claimed:
                role_extra_xml += (
                    "<my_file_claims>\n"
                    + "\n".join(f"  <file>{c}</file>" for c in _claimed[:20])
                    + "\n</my_file_claims>\n"
                )
                _rel: list[dict[str, Any]] = []
                _seen_dec: set[Any] = set()
                for _fp in _claimed[:20]:
                    try:
                        for _d in await db_module.get_decisions_for_file(
                            db, project_id, _fp
                        ):
                            if _d.get("id") not in _seen_dec:
                                _seen_dec.add(_d.get("id"))
                                _rel.append(_d)
                    except Exception:
                        pass
                if _rel:
                    role_extra_xml += (
                        "<relevant_decisions>\n"
                        + "\n".join(
                            f'  <decision anchor="{d.get("code_anchor","")}">'
                            f'{(d.get("title") or "")}: '
                            f'{(d.get("body") or "")[:100]}</decision>'
                            for d in _rel[:10]
                        )
                        + "\n</relevant_decisions>\n"
                    )
    elif role == "planner":
        # Last session summary + decisions sitting on unconfirmed assumptions
        # (what needs revisiting).
        try:
            _ls = await db_module.get_last_session_brief(
                db, project_id, exclude_session_id=session_id_for_notes
            )
        except Exception:
            _ls = None
        if _ls:
            role_extra_xml += (
                f'<last_session name="{_ls.get("name","")}" '
                f'status="{_ls.get("status","")}">\n'
                + "".join(
                    f'  <completed>{(ci.get("title") or "")[:80]}</completed>\n'
                    for ci in (_ls.get("completed_items") or [])[:8]
                )
                + "</last_session>\n"
            )
        try:
            _pin_r = await db_module.get_pinned_decisions(db, project_id)
            _revisit = [
                d for d in _pin_r
                if d.get("assumption")
                and d.get("assumption_status") != "confirmed"
            ]
            if _revisit:
                role_extra_xml += (
                    "<decisions_needing_revisit>\n"
                    + "\n".join(
                        f'  <decision status="{d.get("assumption_status")}">'
                        f'{(d.get("title") or "")}: assumption='
                        f'{(d.get("assumption") or "")[:80]}</decision>'
                        for d in _revisit[:10]
                    )
                    + "\n</decisions_needing_revisit>\n"
                )
        except Exception:
            pass
    brief = (
        f'<session_brief project_id="{project_id}" role="{role}">\n'
        f'{notes_xml}'
        f'{new_items_xml}'
        f'{_progress_xml}'
        f'<sprint>{sprint_str[:200]}</sprint>\n'
        f'<pending_items>\n{sprint_items_xml}\n</pending_items>\n'
        f'<last_tasks>\n{tasks_xml}\n</last_tasks>\n'
        f'{blocking_xml}\n'
        f'{hitl_xml}\n'
        f'{planner_extra_xml}'
        f'{role_extra_xml}'
        f'</session_brief>'
    )
    return {"text": brief, "project_id": project_id, "role": role}
