"""Per-tool handlers extracted from _handle_project_tools (97d695c4).

Each function corresponds to exactly one MCP tool from the
``_handle_project_tools`` dispatch group in ``meridian/mcp/handler.py``.
The extraction is PURELY MECHANICAL: zero behaviour change, same tool names,
same arguments, same return values.

Callers (handler.py) assemble these into a dispatch table
(dict[str, Callable]) and invoke the matched function instead of walking
the original if/elif chain.  Functions that need cross-cutting handler-level
state (e.g. ``_EXECUTOR_SESSIONS``) receive that state as an explicit
keyword argument to keep the import graph acyclic.
"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

import meridian.server as _server
from meridian import db as db_module
from meridian._deps import _hosted_mode
from meridian.mcp_tools import _select_active_tool_set

if TYPE_CHECKING:
    pass


async def handle_create_project(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: create_project."""
    existing = await db_module.get_project_by_name(db, args["name"])
    if existing is not None:
        return {"error": f"project '{args['name']}' already exists", "project": existing}
    # 3b6ff466 — optional parent_project_id makes this a one-level-deep
    # subproject; an invalid/nested parent raises ValueError → error dict.
    try:
        return await db_module.create_project(
            db, args["name"], execution_mode=args.get("execution_mode"),
            # 0bf67524 — seed from workspace cascade defaults when authenticated.
            tenant_id=(tenant.get("id") if tenant else None),
            parent_project_id=args.get("parent_project_id"),
        )
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_set_parent_project(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: set_parent_project (7acb8563).

    Set / change / clear parent_project_id on an EXISTING project (create_project
    only accepted it at creation time). Resolve the project and parent by id or
    name; an invalid / nested / self / has-children parent raises ValueError in
    the db layer -> surfaced as {error}. Omitting the parent (or passing empty)
    DETACHES the project (makes it top-level).
    """
    _pid = (args.get("project_id") or "").strip()
    if not _pid and args.get("project_name"):
        _p = await db_module.get_project_by_name(db, str(args["project_name"]))
        _pid = (_p or {}).get("id", "") if _p else ""
    if not _pid:
        return {"error": "project_id (or project_name) is required"}
    _parent = args.get("parent_project_id")
    if not _parent and args.get("parent_project_name"):
        _pp = await db_module.get_project_by_name(db, str(args["parent_project_name"]))
        _parent = (_pp or {}).get("id") if _pp else None
    if not _parent:  # "" / missing -> detach
        _parent = None
    try:
        updated = await db_module.set_parent_project(db, _pid, _parent)
    except ValueError as exc:
        return {"error": str(exc)}
    if updated is None:
        return {"error": f"project '{_pid}' not found"}
    return updated


async def handle_rename_project(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: rename_project (7acb8563).

    MCP wrapper over the existing db.rename_project (previously only reachable
    through the HTTP route, so an agent had to use raw SQL).
    """
    _pid = (args.get("project_id") or "").strip()
    if not _pid and args.get("project_name"):
        _p = await db_module.get_project_by_name(db, str(args["project_name"]))
        _pid = (_p or {}).get("id", "") if _p else ""
    if not _pid:
        return {"error": "project_id (or project_name) is required"}
    _new = (args.get("new_name") or "").strip()
    if not _new:
        return {"error": "new_name is required"}
    updated = await db_module.rename_project(db, _pid, _new)
    if updated is None:
        return {"error": f"project '{_pid}' not found"}
    return updated


async def handle_register_session(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: register_session."""
    hid = args.get("human_id")
    if not hid and not _hosted_mode():
        hid = db_module.get_default_human_id()
    return await db_module.register_session(
        db, args["project_id"], args["session_name"],
        hid,
        agent_framework=args.get("agent_framework", "claude_code"),
        client_type=args.get("client"),
    )


async def handle_start_session(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
    *,
    executor_sessions: "set[str]",
) -> Any:
    """MCP tool: start_session.

    3689f680 — defaults to a compact response so an executor's context isn't
    blown by the full goal/instructions payload.  Pass compact=False explicitly
    for the full block.

    a76cb7c0 — optional ``version`` scopes the session to a sprint-version bucket.

    599d0097 — session_name is optional: when omitted/blank, generate a
    meaningful default from the first pending item title + timestamp.

    ce3693e4 — resolve project_name → project_id HERE too (the central
    _dispatch_mcp_tool resolver already does this for the HTTP surface, but
    start_session must never index args["project_id"] blind: if only a
    project_name reached this handler it would raise a bare
    KeyError('project_id') that leaks as a cryptic -32603).

    ``executor_sessions`` is the handler-level in-memory set (bf51b12e) passed
    in by the caller so this module stays import-acyclic w.r.t. handler.py.
    """
    _pid = (args.get("project_id") or "").strip()
    if not _pid and args.get("project_name"):
        _p = await db_module.get_project_by_name(db, str(args["project_name"]))
        _pid = (_p or {}).get("id", "") if _p else ""
    if not _pid:
        return {"error": "project_id (or project_name) is required"}
    _sname = (args.get("session_name") or "").strip()
    if not _sname:
        _sname = await db_module.generate_default_session_name(db, _pid)
    result = await _server._start_session_composite(
        db,
        _pid,
        _sname,
        data_dir,
        human_id=args.get("human_id"),
        client_type=args.get("client"),
        role=args.get("role"),
        compact=args.get("compact", True),
        version=args.get("version"),
        mode=args.get("mode"),
    )
    # bf51b12e — register executor sessions in-memory so the planner
    # context-refresh hook skips them (the nudge is planner-only). Best-effort:
    # any failure here must never break start_session.
    try:
        if args.get("role") == "executor" and isinstance(result, dict):
            _sid = result.get("session_id") or result.get("session", {}).get("id")
            if _sid:
                executor_sessions.add(_sid)
    except Exception:  # noqa: BLE001 — non-fatal
        pass
    # b9d1b606 / b2a417ad / bc2e5ff0 — expand fs proxy roots so the executor's
    # repo_path is accessible without a separate set_active_repo call, and also
    # point the Serena daemon pool at this project's repo so claude.ai chat
    # sessions route code-intel requests to the right daemon.
    if tenant is not None:
        try:
            tenant_id = tenant.get("id", "")
            exec_cfg = await db_module.get_executor_config(db, _pid)
            repo_path = exec_cfg.get("repo_path") if exec_cfg else None
            fs_roots = (exec_cfg.get("filesystem_roots") if exec_cfg else None) or []
            # Union repo_path + filesystem_roots, deduped, order-preserved.
            candidates = []
            if isinstance(repo_path, str):
                candidates.append(repo_path)
            candidates.extend(r for r in fs_roots if isinstance(r, str))
            all_roots: list[str] = []
            seen: set[str] = set()
            for r in candidates:
                s = r.strip()
                if s and s not in seen:
                    seen.add(s)
                    all_roots.append(s)
            if all_roots:
                from meridian.routes import tunnel as _tunnel_mod
                await _tunnel_mod.send_add_fs_roots_control(tenant_id, all_roots)
                # Serena pool tracks one active repo: prefer repo_path, else
                # the first root (Serena indexes a single project at a time).
                serena_target = (
                    repo_path.strip()
                    if isinstance(repo_path, str) and repo_path.strip()
                    else all_roots[0]
                )
                await _tunnel_mod.send_active_repo_control(tenant_id, serena_target)
        except Exception:  # noqa: BLE001 — non-fatal background expansion
            pass
        # 9f6aec5f / 2c645647 — inject a codebase-architecture summary so the
        # executor starts already knowing the code's shape.  Also prepend the
        # CODEBASE INDEX directive to agent_instructions when the index is
        # available.  No-op without a healthy, indexed code-intel tunnel; never
        # fails the orientation.
        try:
            if isinstance(result, dict) and "continuation" not in result:
                _cc = await _server._build_codebase_context(
                    tenant.get("id", ""), args["project_id"],
                    compact=args.get("compact", True),
                )
                if _cc:
                    result["codebase_context"] = _cc
                    _existing_ai = result.get("agent_instructions")
                    result["agent_instructions"] = (
                        f"{_server.CODEBASE_INDEX_DIRECTIVE}\n\n{_existing_ai}"
                        if _existing_ai else _server.CODEBASE_INDEX_DIRECTIVE
                    )
        except Exception:  # noqa: BLE001 — orientation must not break
            pass
    # 5efe254b / 590dcdd5 — deliver any pending handoff /goal through this
    # trusted tool result (keyed on project_id) rather than as a spoofable
    # copy-pasted chat string. Read-once: pop clears it so it surfaces exactly
    # once. Outside the tenant gate so self-hosted sessions receive it too.
    # Guarded so a pre-migration DB never breaks the orientation.
    try:
        if isinstance(result, dict):
            _pg_meta = await db_module.pop_pending_goal_with_meta(
                db, args["project_id"]
            )
            if _pg_meta:
                result["pending_goal"] = _pg_meta["goal"]
                if _pg_meta["stale"]:
                    result["pending_goal_stale"] = True
                    result["pending_goal_age_hours"] = _pg_meta["age_hours"]
    except Exception:  # noqa: BLE001
        pass
    # a749f87c — PUSH active_tool_set: deterministic, role/context-based
    # tool pre-selection. Injected here (into the start_session response
    # itself) so an executor never has to remember to call a separate
    # tool-search — the right narrow subset is already present at session
    # start. Best-effort: a failure here must NEVER break orientation.
    try:
        if isinstance(result, dict) and "continuation" not in result:
            _goal_text: str | None = None
            try:
                _goal_row = await db_module.get_goal(db, args["project_id"])
                if _goal_row:
                    _goal_text = " ".join(
                        str(_goal_row.get(f) or "")
                        for f in ("sprint", "north_star", "content")
                    )
            except Exception:  # noqa: BLE001 — non-fatal
                pass
            result["active_tool_set"] = _select_active_tool_set(
                args.get("role"), _goal_text
            )
    except Exception:  # noqa: BLE001 — orientation must not break
        pass
    return result


async def handle_list_projects(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: list_projects."""
    return await db_module.list_project_summaries(db)


async def handle_get_project_by_name(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_project_by_name."""
    project = await db_module.get_project_by_name(db, args["name"])
    if project is None:
        raise ValueError(f"no project found matching '{args['name']}'")
    return {
        "id": project["id"],
        "name": project["name"],
        "sprint": project.get("sprint"),
    }


async def handle_get_goal(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_goal."""
    goal = await db_module.get_goal(db, args["project_id"])
    if goal and goal.get("decisions") and len(goal["decisions"]) > 3000:
        goal["decisions"] = goal["decisions"][-3000:]
    return goal


async def handle_set_goal(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: set_goal."""
    return await db_module.set_goal(db, args["project_id"], args["content"])


async def handle_set_north_star(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: set_north_star."""
    return await db_module.set_north_star(db, args["project_id"], args["north_star"])


async def handle_merge_project(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: merge_project (d6bd60e0).

    Merge a phantom-duplicate project INTO another.  Re-parents the source's
    child rows to the target (pure UPDATEs, never a delete) and soft-archives
    the source unless archive_source is explicitly false.  The db layer returns
    an {error} dict on self-merge / unknown project, which we surface verbatim.
    Resolve BOTH sides by id or name — a name-only arg for the source never
    touches the central project_id resolver (that only maps
    project_id/project_name), so mirror the set_parent_project resolve-then-
    guard pattern for each side.
    """
    _src = (args.get("source_project_id") or "").strip()
    if not _src and args.get("source_project_name"):
        _sp = await db_module.get_project_by_name(db, str(args["source_project_name"]))
        _src = (_sp or {}).get("id", "") if _sp else ""
    _tgt = (args.get("target_project_id") or "").strip()
    if not _tgt and args.get("target_project_name"):
        _tp = await db_module.get_project_by_name(db, str(args["target_project_name"]))
        _tgt = (_tp or {}).get("id", "") if _tp else ""
    if not _src or not _tgt:
        return {"error": "source_project_id and target_project_id are both required"}
    return await db_module.merge_project(
        db, _src, _tgt,
        archive_source=bool(args.get("archive_source", True)),
    )
