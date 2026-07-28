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
from meridian import capability_availability as _capability_availability
from meridian import capability_manifest as _capability_manifest
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

    64b9907a — when neither project_id nor project_name is supplied, fall back
    to the workspace-default project_id from the environment or meridian.toml
    ``[project]`` section (via toml_config.get_default_project_id()).  This
    lets a repo that always works on one project call
    ``start_session(session_name="...")`` without repeating the project_id.
    The toml snippet is::

        [project]
        project_id = "5787cc92-ba7d-4788-b17c-28ab7938b839"

    or, equivalently, set ``MERIDIAN_PROJECT_ID`` in the environment / MCP
    server env block.  When no default is configured and neither field is
    supplied, the existing "project_id (or project_name) is required" error
    is returned unchanged.
    """
    _pid = (args.get("project_id") or "").strip()
    if not _pid and args.get("project_name"):
        _p = await db_module.get_project_by_name(db, str(args["project_name"]))
        _pid = (_p or {}).get("id", "") if _p else ""
    if not _pid:
        # 64b9907a — last-resort: workspace-scoped default project_id.
        from meridian import toml_config as _tc  # noqa: PLC0415
        _pid = _tc.get_default_project_id() or ""
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
    # 74e79d15 — executor /goal skill presence check (GitHub issue #9).
    # When role=executor and a repo_path is configured, verify that the target
    # repo has a .claude/skills/goal/ directory (or .claude/commands/goal.md)
    # so the /goal slash command is recognised by Claude Code.  Without this
    # file a fresh executor session silently cannot respond to /goal blocks,
    # blocking real work (the issue that prompted this fix).  Best-effort:
    # never break start_session if the check or the path look-up fails.
    try:
        if (
            isinstance(result, dict)
            and "continuation" not in result
            and args.get("role") == "executor"
            and not _hosted_mode()
        ):
            # decision 0dedff91 — repo_path is the CALLER's local path; the
            # os.path.isdir/isfile checks below can only run meaningfully
            # against this process's own filesystem (self-hosted). On hosted
            # Meridian there is nothing honest to check here, so skip rather
            # than mis-resolve against the server's filesystem and emit a
            # false setup_warning.
            import os as _os  # noqa: PLC0415

            _exec_cfg_check: dict | None = None
            try:
                _exec_cfg_check = await db_module.get_executor_config(db, _pid)
            except Exception:  # noqa: BLE001
                pass
            _repo_path_check = (
                (_exec_cfg_check or {}).get("repo_path") or ""
            ).strip()
            if _repo_path_check:
                # Normalise: split on both "/" and "\" so Windows paths work on
                # any platform without relying on pathlib.Path.name (which is
                # platform-native and breaks Windows-style paths on Linux CI).
                _rp = _repo_path_check.rstrip("/\\")
                _skill_dir = _rp + "/.claude/skills/goal"
                _cmd_file = _rp + "/.claude/commands/goal.md"
                _has_skill = _os.path.isdir(_skill_dir)
                _has_cmd = _os.path.isfile(_cmd_file)
                if not _has_skill and not _has_cmd:
                    result["setup_warning"] = (
                        "The /goal slash command is not registered in this "
                        f"repo ({_repo_path_check}). "
                        "Claude Code will not recognise /goal blocks until "
                        "you add a skill file. "
                        "Run: mkdir -p .claude/skills/goal && "
                        "curl -fsSL https://usemeridian.us/install/goal-skill.md "
                        "-o .claude/skills/goal/SKILL.md "
                        "(or copy the template from the AGENTS.md 'First-time "
                        "executor install' section). "
                        "Commit the file so all future sessions in this repo "
                        "recognise /goal automatically."
                    )
    except Exception:  # noqa: BLE001 — setup_warning must never break orientation
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


async def handle_add_custom_hook(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: add_custom_hook (273287cb).

    Creates a user-defined PreToolUse/PostToolUse/Stop hook for a project.
    Written into the repo's ``.claude/hooks/`` on the next ``generate_handoff``
    (see ``handoff._write_sprint_guard_hooks`` / ``_write_custom_hooks``), the
    same mechanism that already auto-writes sprint_guard.{sh,ps1}. The db
    layer raises ValueError for a bad event, empty name/script_sh, the
    reserved 'sprint_guard' name, or a duplicate slug — surfaced as {error}.
    """
    _pid = (args.get("project_id") or "").strip()
    if not _pid:
        return {"error": "project_id (or project_name) is required"}
    script_sh = args.get("script_sh")
    if not script_sh:
        return {"error": "script_sh is required"}
    try:
        return await db_module.add_custom_hook(
            db, _pid,
            name=args.get("name") or "",
            event=args.get("event") or "",
            script_sh=script_sh,
            script_ps1=args.get("script_ps1"),
            matcher=args.get("matcher"),
            blocking=bool(args.get("blocking", True)),
            enabled=bool(args.get("enabled", True)),
        )
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_get_custom_hooks(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_custom_hooks (273287cb). List a project's user-defined hooks."""
    _pid = (args.get("project_id") or "").strip()
    if not _pid:
        return {"error": "project_id (or project_name) is required"}
    hooks = await db_module.get_custom_hooks(
        db, _pid,
        event=args.get("event"),
        enabled_only=bool(args.get("enabled_only", False)),
    )
    return {"project_id": _pid, "hooks": hooks}


async def handle_delete_custom_hook(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: delete_custom_hook (273287cb).

    Idempotent: deleting an already-gone hook returns {"deleted": False}, not
    an error (matches delete_sprint_item_pointer's convention). Does not
    remove any already-written .claude/hooks/<slug>.* files — see
    handoff._write_custom_hooks / db.delete_custom_hook docstrings.
    """
    _pid = (args.get("project_id") or "").strip()
    _hook_id = (args.get("hook_id") or "").strip()
    if not _pid or not _hook_id:
        return {"error": "project_id and hook_id are both required"}
    deleted = await db_module.delete_custom_hook(db, _pid, _hook_id)
    return {"hook_id": _hook_id, "deleted": deleted}


async def handle_get_capability_manifest(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_capability_manifest (649e095f).

    Read-only. A project with no persisted manifest gets an empty profile
    back, never an error.
    """
    _pid = (args.get("project_id") or "").strip()
    if not _pid:
        return {"error": "project_id (or project_name) is required"}
    return await db_module.get_project_capability_manifest(db, _pid)


async def handle_set_capability_manifest(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: set_capability_manifest (649e095f).

    Validates and persists a project's capability manifest wholesale.
    Malformed input (unknown/missing fields, duplicate ids, secret-shaped
    values, machine-local absolute paths) rejects deterministically with
    {error} — never a partial write.
    """
    _pid = (args.get("project_id") or "").strip()
    if not _pid:
        return {"error": "project_id (or project_name) is required"}
    capabilities = args.get("capabilities")
    if capabilities is None:
        return {"error": "capabilities is required"}
    try:
        return await db_module.set_project_capability_manifest(db, _pid, capabilities)
    except _capability_manifest.CapabilityManifestError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_set_capability_profile(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: set_capability_profile (02038afe).

    Persists ONE layer of the workspace/user/project/sprint_version/item
    capability-inheritance chain — wholesale-replaces that scope's stored
    capabilities and disabled-capability-id list (not a merge). Rejects
    deterministically with {error} on a bad scope_type, any malformed
    capability entry (same schema/safety checks as set_capability_manifest),
    a malformed disabled_capability_ids list, or unsafe (secret-shaped /
    machine-local-path) provenance.
    """
    scope_type = (args.get("scope_type") or "").strip()
    scope_id = (args.get("scope_id") or "").strip()
    if not scope_type:
        return {"error": "scope_type is required"}
    if not scope_id:
        return {"error": "scope_id is required"}
    try:
        return await db_module.set_capability_profile(
            db,
            scope_type,
            scope_id,
            capabilities=args.get("capabilities"),
            disabled_capability_ids=args.get("disabled_capability_ids"),
            provenance=args.get("provenance"),
        )
    except _capability_manifest.CapabilityManifestError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_clear_capability_profile(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: clear_capability_profile (02038afe).

    Deletes a scope's ENTIRE capability profile row (both its capabilities
    and its disabled-capability-id list) — distinct from disabling
    individual capability ids. Idempotent: clearing an already-empty or
    never-set scope is a no-op, not an error.
    """
    scope_type = (args.get("scope_type") or "").strip()
    scope_id = (args.get("scope_id") or "").strip()
    if not scope_type:
        return {"error": "scope_type is required"}
    if not scope_id:
        return {"error": "scope_id is required"}
    try:
        return await db_module.clear_capability_profile(db, scope_type, scope_id)
    except _capability_manifest.CapabilityManifestError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_get_effective_capability_profile(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_effective_capability_profile (02038afe).

    Read-only. Resolves and returns the merged capability profile for a
    project (optionally narrowed to one sprint item) across every applicable
    layer: workspace -> user -> project -> sprint_version -> item. Includes
    a per-capability source-layer map and audit trails of every override
    (flagging the ones that were real conflicts — same capability id,
    incompatible required_tools/availability_policy across layers) and every
    disable that actually removed something.
    """
    _pid = (args.get("project_id") or "").strip()
    if not _pid:
        return {"error": "project_id (or project_name) is required"}
    sprint_item_id = (args.get("sprint_item_id") or "").strip() or None
    user_scope_id = (args.get("user_scope_id") or "").strip() or None
    workspace_scope_id = (args.get("workspace_scope_id") or "").strip() or "singleton"
    try:
        return await db_module.get_effective_capability_profile(
            db,
            _pid,
            sprint_item_id=sprint_item_id,
            workspace_scope_id=workspace_scope_id,
            user_scope_id=user_scope_id,
        )
    except _capability_manifest.CapabilityManifestError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}


async def _build_live_inventory(tenant: dict[str, Any] | None) -> dict[str, Any]:
    """Live-inventory snapshot (ac80aaaf) for :func:`check_capability_availability`.

    Derives ``{tunnel_reachable, builtin_tools, plugins, stdio_registry}`` --
    the shape :func:`meridian.capability_availability.classify_tool` expects --
    from the tenant's resolved tunnel-plugin config and current tunnel
    connection state, mirroring the same cross-instance-aware reachability
    check ``list_plugins``/``get_plugin_details`` already use (see
    ``meridian/mcp/handler.py::_handle_plugin_tools``). Best-effort and
    non-fatal throughout: any failure fetching a slot's live tools just leaves
    that slot with an empty tool set (not invocable) rather than raising, so a
    capability-availability check never crashes on a flaky tunnel probe.

    ``stdio_registry`` is always empty here -- stdio tool identities are
    project/capability-declared local state with no live-fetchable
    equivalent, so callers who use ``stdio:`` tool references must build their
    own inventory (or merge one in) rather than rely on this default.
    """
    import asyncio  # noqa: PLC0415
    import json  # noqa: PLC0415

    from meridian.mcp_tools import _MCP_TOOLS_LIST  # noqa: PLC0415
    from meridian.routes import tunnel as _tunnel_mod  # noqa: PLC0415
    from meridian.tool_manifest import build_tool_manifest  # noqa: PLC0415
    from meridian.tunnel_plugins import resolve_plugins  # noqa: PLC0415

    builtin_manifest = build_tool_manifest(_MCP_TOOLS_LIST)
    builtin_tool_names = {
        t["name"] for t in builtin_manifest.get("tools", []) if isinstance(t, dict) and t.get("name")
    }

    raw_tp = tenant.get("tunnel_plugins") if tenant else None
    if isinstance(raw_tp, str) and raw_tp.strip():
        try:
            raw_tp = json.loads(raw_tp)
        except Exception:  # noqa: BLE001
            raw_tp = None
    resolved = resolve_plugins(raw_tp)

    tenant_id = tenant.get("id") if tenant else None
    local_active = bool(tenant_id and _tunnel_mod.has_active_tunnel(tenant_id))
    cross_instance_active = bool(
        tenant_id and (
            _tunnel_mod.tenant_owner_instance(tenant_id)
            or (tenant and tenant.get("tunnel_active"))
        )
    )
    tunnel_reachable = local_active or cross_instance_active

    # Live per-slot tool names are only fetchable when THIS instance holds the
    # tunnel socket (mirrors list_plugins -- _fetch_slot_tools returns [] on a
    # cross-instance miss, so there's no point calling it otherwise).
    slot_tool_names: dict[str, set[str]] = {}
    if tenant_id and tunnel_reachable and local_active:
        try:
            slot_results = await asyncio.gather(*[
                _tunnel_mod._fetch_slot_tools(  # type: ignore[attr-defined]
                    tenant_id, p["slot"],
                    budget=_tunnel_mod._slot_tools_fetch_budget(p["slot"]),  # type: ignore[attr-defined]
                )
                for p in resolved
            ])
        except Exception:  # noqa: BLE001
            slot_results = []
        for label, tools in (slot_results or []):
            if tools:
                slot_tool_names[label] = {
                    str(t.get("name")) for t in tools if isinstance(t, dict) and t.get("name")
                }

    # Index plugins by BOTH their catalog name (e.g. "code-intel") and their
    # connector-facing display name (e.g. "codebase", from SLOT_DISPLAY_NAMES)
    # so a capability's tool_ref can use either prefix convention.
    display_map = _tunnel_mod.SLOT_DISPLAY_NAMES
    plugins: dict[str, dict[str, Any]] = {}
    for p in resolved:
        slot = p["slot"]
        tools_here = slot_tool_names.get(slot, set())
        entry = {
            "slot": slot,
            "enabled": bool(p.get("enabled")),
            "invocable": bool(tools_here),
            "tools": tools_here,
        }
        plugins[p["name"]] = entry
        disp = display_map.get(slot)
        if disp and disp not in plugins:
            plugins[disp] = entry

    return {
        "tunnel_reachable": tunnel_reachable,
        "builtin_tools": builtin_tool_names,
        "plugins": plugins,
        "stdio_registry": {},
    }


async def check_capability_availability(
    db: Any,
    project_id: str,
    tenant: dict[str, Any] | None = None,
    *,
    capability_id: "str | None" = None,
    live_inventory: "dict[str, Any] | None" = None,
) -> list[dict[str, Any]]:
    """Verify a project's declared capability manifest against live MCP/tunnel state (ac80aaaf).

    Plain importable helper (deliberately NOT its own MCP tool -- this item's
    job is the verification logic itself; a user-facing surface is later
    capability-contract work, 98aaccf4) so that work can call straight into
    this. Loads the project's persisted capability manifest
    (:func:`meridian.db.get_project_capability_manifest` -- an empty manifest
    for a project with none, never an error) and classifies each capability's
    ``required_tools``/``fallback_chain`` against a live inventory snapshot via
    :func:`meridian.capability_availability.evaluate_capability_availability`.

    ``live_inventory`` is normally derived automatically from *tenant*'s
    resolved tunnel-plugin config and tunnel connection state (see
    :func:`_build_live_inventory`); passing it explicitly (as tests do) skips
    that async, I/O-bound derivation entirely -- the standard no-network,
    mocked-tunnel-state test seam for this function.

    Returns one availability result per declared capability (``[]`` for a
    project with no manifest), each shaped as
    :func:`meridian.capability_availability.evaluate_capability_availability`
    returns.
    """
    manifest = await db_module.get_project_capability_manifest(db, project_id)
    capabilities = manifest.get("capabilities") or []
    if capability_id:
        capabilities = [c for c in capabilities if c.get("id") == capability_id]
    if live_inventory is None:
        live_inventory = await _build_live_inventory(tenant)
    return _capability_availability.evaluate_manifest_availability(capabilities, live_inventory)
