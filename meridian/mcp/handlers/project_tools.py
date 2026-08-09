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
from meridian import profile_contract as _profile_contract
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
                # 99e0bb6a — use the already-resolved _pid, not the raw
                # args["project_id"]. When start_session is called with
                # neither project_id nor project_name (the AGENTS.md
                # auto-scoping pattern: project_id resolved from
                # MERIDIAN_PROJECT_ID / meridian.toml [project]), the
                # "project_id" key is absent from args entirely — blind
                # indexing here silently KeyErrored (swallowed by the except
                # below) and dropped codebase_context for every call using
                # that documented, recommended pattern.
                _cc = await _server._build_codebase_context(
                    tenant.get("id", ""), _pid,
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
    # 89a06e40 — machine-readable effective profile identity/generation
    # (hosted_default+workspace+user+project+session layers; see
    # meridian.profile_contract.EffectiveProfile /
    # db.profile_layers.get_effective_profile), attached alongside every
    # start_session orientation. SIBLING to the codebase_context enrichment
    # above but guarded in its OWN try/except — a profile-resolution failure
    # must never affect codebase_context, and vice versa. Outside the tenant
    # gate (unlike codebase_context) since get_effective_profile needs only
    # project_id/session_id, not a tenant — self-hosted sessions get this
    # field too. See pinned decision ee7bccc9 for why the tunnel/connector
    # surface deliberately does NOT call this same db.get_effective_profile
    # path (no project_id is available there).
    try:
        if isinstance(result, dict):
            # Same session_id extraction the bf51b12e block above uses.
            _sid_for_profile = (
                result.get("session_id") or result.get("session", {}).get("id")
            )
            _effective_profile = await db_module.get_effective_profile(
                db, _pid, session_id=_sid_for_profile,
            )
            result["profile_binding"] = _profile_contract.project_profile_binding(
                _effective_profile
            )
    except Exception:  # noqa: BLE001 — profile binding is best-effort
        pass
    # 5efe254b / 590dcdd5 — deliver any pending handoff /goal through this
    # trusted tool result (keyed on project_id) rather than as a spoofable
    # copy-pasted chat string. Read-once: pop clears it so it surfaces exactly
    # once. Outside the tenant gate so self-hosted sessions receive it too.
    # Guarded so a pre-migration DB never breaks the orientation.
    try:
        if isinstance(result, dict):
            # 99e0bb6a — resolved _pid, not raw args["project_id"] (see the
            # codebase_context comment above for why the raw key can be
            # absent). This one matters most: silently dropping pending_goal
            # here means the handoff /goal a prior generate_handoff minted
            # for THIS project never reaches the session that resolved via
            # the default-project fallback, even though the session itself
            # was correctly created under that project.
            _pg_meta = await db_module.pop_pending_goal_with_meta(
                db, _pid
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
                # 99e0bb6a — resolved _pid, not raw args["project_id"].
                _goal_row = await db_module.get_goal(db, _pid)
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
    # 98aaccf4 — machine-readable effective capability contract, emitted
    # alongside every start_session orientation (both compact and full;
    # neither shape excludes it). board_stale mirrors this session's own
    # pending_goal_stale signal (set just above, when present) -- a stale
    # unconsumed handoff is a reasonable proxy for "the board/profile
    # snapshot behind this contract may itself be stale." Fully guarded: a
    # failure here degrades to no field rather than breaking start_session.
    #
    # 0de0599a — the compact orientation must stay small BY CONSTRUCTION:
    # the per-item item_tool_requirements/item_sprint_item_pointers/
    # item_artifact_pointer_findings/item_executor_contracts sections were
    # observed inflating a real board's compact response to ~593KB (382KB
    # from this field alone). Those per-item breakdowns exist for handoff
    # consumers (planner/executor routing), not for a session-start check —
    # a compact caller only needs the scalar executable/executable_reasons/
    # availability/manifest_hash fields, so cap the list sections to 0 for
    # compact=True. compact=False keeps the full, uncapped contract exactly
    # as before (identical to every generate_handoff call site, which never
    # pass these overrides).
    _is_compact = bool(args.get("compact", True))
    try:
        if isinstance(result, dict):
            from meridian import handoff as _handoff_module  # noqa: PLC0415
            _cc_kwargs: dict[str, Any] = {}
            if _is_compact:
                _cc_kwargs["max_executor_contracts"] = 0
                _cc_kwargs["max_contract_list_items"] = 0
            result["capability_contract"] = await _handoff_module.build_effective_capability_contract(
                db, _pid, board_stale=bool(result.get("pending_goal_stale")),
                **_cc_kwargs,
            )
    except Exception:  # noqa: BLE001 — capability contract is best-effort
        pass
    # 75ac1c8e — canonical, machine-readable execution policy: bounds
    # planning/tool-free turns and names the required first action
    # deterministically (see executor_config.build_execution_policy). Added
    # here — after the composite call — so BOTH the compact and full
    # start_session shapes (and the "continue" resume shape) get the exact
    # same structured dict from one code path, mirroring how
    # capability_contract is attached just above rather than duplicating the
    # compute inside _start_session_composite's two branches. Best-effort: a
    # failure here must never break start_session.
    try:
        if isinstance(result, dict):
            from meridian.executor_config import build_execution_policy  # noqa: PLC0415
            _policy_project = await db_module.get_project(db, _pid)
            _policy_mode = db_module.normalize_execution_mode(
                (_policy_project or {}).get("execution_mode")
            )
            _policy_exec_cfg = await db_module.get_executor_config(db, _pid)
            result["execution_policy"] = build_execution_policy(
                _policy_exec_cfg, execution_mode=_policy_mode,
            )
    except Exception:  # noqa: BLE001 — execution policy is best-effort
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


async def handle_update_custom_hook(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: update_custom_hook (b4f4627f).

    The previously-missing generic enable/disable/edit path: patches a subset
    of an existing hook's editable fields (name, event, matcher, script_sh,
    script_ps1, blocking, enabled) without the delete+recreate round-trip
    add_custom_hook/delete_custom_hook would otherwise require. Renaming
    re-derives the slug (same reserved-name / uniqueness checks as
    add_custom_hook). The db layer raises ValueError for a bad event, the
    reserved 'sprint_guard' name, or a slug collision — surfaced as {error},
    same convention as add_custom_hook. Returns {"error": ...} (never raises)
    when hook_id doesn't resolve to a row for this project.

    b4f4627f — when this call flips ``enabled`` True -> False, any already-
    written ``.claude/hooks/<slug>.*`` artifacts for THIS hook are removed
    immediately — best-effort, only when the project has a resolvable
    ``executor_config.repo_path`` with a ``.claude`` dir — instead of waiting
    for the next ``generate_handoff`` to simply stop re-writing them. This
    generalizes the per-hook convergence f7084ed0 built specifically for the
    orphan_reaper Stop hook (``orphan_reaper.remove_orphan_reaper_artifacts``)
    to every user-defined hook, via ``handoff.remove_custom_hook_artifacts``.
    Nonblocking/fail-open: a removal failure never fails the update itself,
    matching the orphan_reaper toggle route's own best-effort convention.
    """
    _pid = (args.get("project_id") or "").strip()
    _hook_id = (args.get("hook_id") or "").strip()
    if not _pid or not _hook_id:
        return {"error": "project_id and hook_id are both required"}
    _editable_keys = (
        "name", "event", "matcher", "script_sh", "script_ps1", "blocking", "enabled",
    )
    fields = {k: args[k] for k in _editable_keys if k in args}
    if not fields:
        return {"error": "at least one editable field must be supplied"}
    before = await db_module.get_custom_hook(db, _pid, _hook_id)
    try:
        updated = await db_module.update_custom_hook(db, _pid, _hook_id, **fields)
    except ValueError as exc:
        return {"error": str(exc)}
    if updated is None:
        return {"error": f"no such hook: {_hook_id}"}
    removed_files: list[str] = []
    _was_enabled = bool(before.get("enabled")) if before else False
    _now_enabled = bool(updated.get("enabled"))
    # Hosted Meridian cannot safely resolve or mutate a caller's local
    # repository.  Persist the hook state, but leave local artifact cleanup to
    # the caller-side handoff/launcher instead of crossing the hosted/local
    # filesystem boundary (no_local_fs_access guard).
    if before is not None and _was_enabled and not _now_enabled and not _hosted_mode():
        try:
            from pathlib import Path as _Path  # noqa: PLC0415
            from meridian import handoff as _handoff_module  # noqa: PLC0415
            exec_cfg = await db_module.get_executor_config(db, _pid)
            repo_path = (exec_cfg.get("repo_path") or "").strip()
            if repo_path and (_Path(repo_path) / ".claude").exists():
                hooks_dir = _Path(repo_path) / ".claude" / "hooks"
                removed_files = _handoff_module.remove_custom_hook_artifacts(
                    hooks_dir, updated.get("slug") or before.get("slug"),
                )
        except Exception:  # noqa: BLE001 — artifact cleanup is best-effort, never fails the update
            pass
    if removed_files:
        return {**updated, "removed_files": removed_files}
    return updated


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


async def handle_list_profile_layers(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: list_profile_layers (0bec79a7, PROFILE-5).

    Read-only enumeration of every persisted profile_layers row across the
    hosted_default -> workspace -> user -> project -> session contract,
    optionally narrowed to one scope_type. Ordered by (scope_type,
    scope_id) for deterministic output — a raw listing, not a resolved/
    merged view (see handle_get_effective_profile for the merged view).
    """
    scope_type = (args.get("scope_type") or "").strip() or None
    try:
        return await db_module.list_profile_layers(db, scope_type)
    except _profile_contract.ProfileContractError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_get_profile_layer(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_profile_layer (0bec79a7, PROFILE-5).

    Read-only: return the raw, single-layer profile for one (scope_type,
    scope_id) — no merging against any other layer. A scope with no
    persisted row gets an empty profile back, never an error. See
    handle_get_effective_profile for the MERGED, multi-layer view.
    """
    scope_type = (args.get("scope_type") or "").strip()
    scope_id = (args.get("scope_id") or "").strip()
    if not scope_type:
        return {"error": "scope_type is required"}
    if not scope_id:
        return {"error": "scope_id is required"}
    try:
        return await db_module.get_profile_layer(db, scope_type, scope_id)
    except _profile_contract.ProfileContractError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_save_profile_layer(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: save_profile_layer (0bec79a7, PROFILE-5).

    Validates and persists one layer's profile — wholesale-replaces this
    scope's stored fields/reset_fields (not a merge). expected_revision
    enables optimistic concurrency: omit it for last-write-wins, or pass the
    revision you last read to get a structured {error, code:
    "STALE_REVISION", current_revision} instead of silently clobbering a
    concurrent write. override_reason is accepted for forward symmetry with
    resolve_effective_profile's identically-named knob, but this tool never
    blocks a write on it — narrow_only/safe_direction enforcement is a
    merge-time (resolve) concern, not a write-time one.
    """
    scope_type = (args.get("scope_type") or "").strip()
    scope_id = (args.get("scope_id") or "").strip()
    if not scope_type:
        return {"error": "scope_type is required"}
    if not scope_id:
        return {"error": "scope_id is required"}
    try:
        return await db_module.set_profile_layer(
            db,
            scope_type,
            scope_id,
            fields=args.get("fields"),
            reset_fields=args.get("reset_fields"),
            provenance=args.get("provenance"),
            expected_revision=args.get("expected_revision"),
            override_reason=args.get("override_reason"),
            actor=args.get("actor"),
        )
    except _profile_contract.ProfileStaleRevisionError as exc:
        return {"error": str(exc), "code": "STALE_REVISION", "current_revision": exc.actual_revision}
    except _profile_contract.ProfileContractError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_clone_profile_layer(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: clone_profile_layer (0bec79a7, PROFILE-5).

    Copies one layer's fields/reset_fields/provenance onto another scope —
    through the same validation/hashing path as save_profile_layer (not a
    raw copy), since the target scope's allowed_layers may differ from the
    source's. Rejects with {error} when the source layer does not exist
    (nothing to clone).
    """
    source_scope_type = (args.get("source_scope_type") or "").strip()
    source_scope_id = (args.get("source_scope_id") or "").strip()
    target_scope_type = (args.get("target_scope_type") or "").strip()
    target_scope_id = (args.get("target_scope_id") or "").strip()
    if not source_scope_type:
        return {"error": "source_scope_type is required"}
    if not source_scope_id:
        return {"error": "source_scope_id is required"}
    if not target_scope_type:
        return {"error": "target_scope_type is required"}
    if not target_scope_id:
        return {"error": "target_scope_id is required"}
    try:
        return await db_module.clone_profile_layer(
            db, source_scope_type, source_scope_id, target_scope_type, target_scope_id,
            actor=args.get("actor"),
        )
    except _profile_contract.ProfileContractError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_activate_profile_layer(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: activate_profile_layer (0bec79a7, PROFILE-5).

    Advances a hosted_default layer's lifecycle to "active" — the single
    "reach active" operation for the hosted_default floor (pinned decision
    fae6e882 collapsed "publish" and "activate" into this one tool: a
    hosted_default layer becomes authoritative the moment it reaches
    "active", so there is no distinct publish step to expose separately).
    Idempotent: calling on an already-active scope is a no-op success (same
    revision, no new audit row). Only a draft -> active or deprecated ->
    active transition is valid; any other current state (e.g. retired,
    which is terminal) rejects with {error}.
    """
    scope_id = (args.get("scope_id") or "").strip()
    if not scope_id:
        return {"error": "scope_id is required"}
    try:
        return await db_module.transition_hosted_default_lifecycle(
            db, scope_id, "active", actor=args.get("actor"),
        )
    except _profile_contract.ProfileContractError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_reset_profile_layer(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: reset_profile_layer (0bec79a7, PROFILE-5).

    Deletes a scope's ENTIRE profile-layer row so it reverts to purely
    inheriting from less-specific layers — mirrors clear_capability_profile's
    semantics for the profile-layers contract. Idempotent: resetting an
    already-empty or never-set scope is a no-op, not an error. For
    hosted_default this clears the row (back to no-row / implicit draft)
    but is NOT an audited lifecycle transition — the audited path lives in
    transition_hosted_default_lifecycle (see handle_activate_profile_layer).
    """
    scope_type = (args.get("scope_type") or "").strip()
    scope_id = (args.get("scope_id") or "").strip()
    if not scope_type:
        return {"error": "scope_type is required"}
    if not scope_id:
        return {"error": "scope_id is required"}
    try:
        return await db_module.reset_profile_layer(db, scope_type, scope_id)
    except _profile_contract.ProfileContractError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_get_profile_layer_revisions(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_profile_layer_revisions (0bec79a7, PROFILE-5).

    Read-only: the hosted_default revision/audit history for one scope_id,
    newest first — the rollback/audit trail for the one layer that is
    "immutable once published". A non-hosted_default scope_id always
    returns [] (only hosted_default writes are ledgered).
    """
    scope_id = (args.get("scope_id") or "").strip()
    if not scope_id:
        return {"error": "scope_id is required"}
    limit = args.get("limit")
    try:
        if limit is None:
            return await db_module.get_profile_layer_revisions(db, scope_id)
        return await db_module.get_profile_layer_revisions(db, scope_id, limit=int(limit))
    except _profile_contract.ProfileContractError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_get_effective_profile(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_effective_profile (0bec79a7, PROFILE-5).

    Read-only: resolve and return the MERGED profile for a project across
    every applicable layer — hosted_default -> workspace -> user -> project
    -> session, least to most specific (see
    meridian.db.profile_layers.get_effective_profile). The 'project' layer
    is synthetic: its 7 legacy ProjectSettings/executor_config fields come
    from the existing get_project_settings authority (zero duplication);
    its 3 new fields (tool_priority_map, capability_manifest_ref,
    claim_verification_mode) come from the real profile_layers row. Pass
    session_id/user_scope_id to also fold in those layers;
    workspace_scope_id/hosted_default_scope_id default to
    'singleton'/'global'. Returns {error} for an unknown project_id.
    """
    _pid = (args.get("project_id") or "").strip()
    if not _pid and args.get("project_name"):
        _p = await db_module.get_project_by_name(db, str(args["project_name"]))
        _pid = (_p or {}).get("id", "") if _p else ""
    if not _pid:
        return {"error": "project_id (or project_name) is required"}
    session_id = (args.get("session_id") or "").strip() or None
    user_scope_id = (args.get("user_scope_id") or "").strip() or None
    workspace_scope_id = (args.get("workspace_scope_id") or "").strip() or "singleton"
    hosted_default_scope_id = (args.get("hosted_default_scope_id") or "").strip() or "global"
    try:
        return await db_module.get_effective_profile(
            db, _pid,
            session_id=session_id,
            user_scope_id=user_scope_id,
            workspace_scope_id=workspace_scope_id,
            hosted_default_scope_id=hosted_default_scope_id,
        )
    except _profile_contract.ProfileContractError as exc:
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
