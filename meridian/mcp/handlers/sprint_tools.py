"""Per-tool handlers extracted from _handle_sprint_tools (ba4f879b).

Each function corresponds to exactly one MCP tool from the
``_handle_sprint_tools`` dispatch group in ``meridian/mcp/handler.py``.
The extraction is PURELY MECHANICAL: zero behaviour change, same tool names,
same arguments, same return values.

Callers (handler.py) assemble these into a dispatch table
(dict[str, Callable]) and invoke the matched function instead of walking
the original if/elif chain.  All 20 tools are extracted:
  add_sprint_note, get_sprint_notes, add_sprint_item, fan_out_sprint_items,
  update_sprint_item, set_sprint, get_sprint_progress, get_sprint_items,
  get_parallelizable_groups, assign_sprint_waves, analyze_sprint,
  claim_sprint_item, add_subtask, split_sprint_item, merge_sprint_items,
  complete_sprint_item, add_sprint_item_pointer, get_sprint_item_pointers,
  resolve_sprint_item_pointers, delete_sprint_item_pointer.

Handler-level helper functions (e.g. ``_infer_touches_resources``) are
imported lazily inside each function body to keep the import graph acyclic
and avoid circular imports with ``meridian.mcp.handler``.
"""
from __future__ import annotations

import json
import os
from typing import Any

import meridian.server as _server
from meridian import db as db_module
from meridian import goal_md as goal_md_module
from meridian._deps import validate_input_size, _hosted_mode


async def handle_add_sprint_note(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: add_sprint_note."""
    validate_input_size(args.get("title"), "note title", 500)
    validate_input_size(args.get("body"), "note body", 10_000_000)
    return await db_module.add_session_note(
        db, args["session_id"], args["title"], args["body"],
        note_kind=args.get("note_kind"),
    )


async def handle_get_sprint_notes(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_sprint_notes."""
    return await db_module.get_session_notes(
        db, args["session_id"], note_kind=args.get("note_kind")
    )


async def handle_add_sprint_item(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: add_sprint_item.

    8f01cdfe — project_name is an accepted alternative to project_id (the
    dispatcher resolver at _dispatch_mcp_tool resolves a present, resolvable
    project_name → project_id before we get here). If neither a project_id
    nor a resolvable project_name reached us, return a clean, descriptive
    error instead of letting the direct args["project_id"] reads below raise
    a raw KeyError that leaks as a cryptic JSON-RPC -32603.
    """
    from ..handler import (  # noqa: PLC0415
        _infer_touches_resources,
        _active_executor_session_warnings,
        _prospecting_result,
        _persist_prospected_pointer,
        _check_file_only_resources_warning,
        _wave_assignment_hint,
        _fetch_recent_commits,
    )
    if not args.get("project_id"):
        return {"error": "project_id is required (or pass project_name)"}
    validate_input_size(args.get("title"), "sprint item title", 500)
    validate_input_size(args.get("notes"), "sprint item notes", 50_000)
    # 7e212375 — codebase drift check: if the title looks already-implemented
    # (3+ keyword overlap with a specific migration or a recent commit),
    # block with a warning unless force=true. Closes the "adding items for
    # already-shipped work" gap. Migration check is offline (cached file);
    # the commit check degrades to empty if git isn't reachable.
    if not bool(args.get("force", False)):
        from ... import handoff as _handoff_drift  # noqa: PLC0415
        try:
            _proj_for_drift = await db_module.get_project(db, args["project_id"])
            _drift_commits = (
                await _fetch_recent_commits(_proj_for_drift, tenant)
                if _proj_for_drift else []
            )
        except Exception:  # noqa: BLE001
            _drift_commits = []
        _drift = _handoff_drift.detect_sprint_item_drift(
            args.get("title") or "", _drift_commits,
        )
        if _drift:
            return {
                "drift_warning": True,
                "title": args.get("title"),
                "matches": _drift[:5],
                "message": (
                    "This may already be implemented — "
                    + "; ".join(f"{m['kind']}:{m['ref']}" for m in _drift[:3])
                    + ". Pass force=true to add it anyway."
                ),
            }
    # fd86aacc — warn if active executor sessions exist when adding a new item
    _active_session_warnings = await _active_executor_session_warnings(db, args["project_id"])
    # 07bdfdbb — auto-infer touches_resources from the title when the caller
    # supplied none, so the item can parallelize instead of falling into its
    # own sequential undeclared group (de730a25).
    _touches = args.get("touches_resources")
    if not _touches:
        _touches = _infer_touches_resources(args.get("title") or "") or None
    try:
        _new_item = await db_module.add_sprint_item(
            db, args["project_id"], args["version"], args["title"],
            group=args.get("group"),
            notes=args.get("notes"),
            human_id=args.get("human_id"),
            depends_on=args.get("depends_on"),
            failure_mode=args.get("failure_mode"),
            milestone_type=args.get("milestone_type", "task"),
            touches_resources=_touches,
            force=bool(args.get("force", False)),
            deferred_until=args.get("deferred_until"),
            track=args.get("track"),
            priority=args.get("priority"),
            blocker_kind=args.get("blocker_kind"),
            wave=args.get("wave"),
            sprint_name=args.get("sprint_name"),
            required_tool=args.get("required_tool"),
            tool_requirements=args.get("tool_requirements"),
            artifact_kind=args.get("artifact_kind"),
            planned_output=args.get("planned_output"),
            artifact_policy=args.get("policy"),
        )
    except ValueError as exc:
        # 501ec93f — malformed touches_resources identifier; also e08fee30 /
        # 2282a636 bad priority / blocker_kind; also 76dde31f (665 follow-up)
        # malformed tool_requirements entries; also 2f9cb288 (665 follow-up)
        # malformed artifact_kind/planned_output/policy declarations.
        # Surface, don't crash.
        return {"error": str(exc)}
    # b0d42ef6 — duplicate guard blocked the insert: surface the error as-is
    # (no item was created, so the warnings below don't apply).
    if isinstance(_new_item, dict) and _new_item.get("error") == "duplicate":
        return _new_item
    _extra: dict[str, Any] = {}
    if _active_session_warnings:
        _extra["active_session_warning"] = (
            "WARNING: " + "; ".join(_active_session_warnings)
            + " — new item added but may not be picked up until next session start."
        )
    # 2d932f60 — if the title reads like a decision/insight, propose capturing
    # it (no auto-write; the planner confirms).
    from ... import handoff as _handoff_ins  # noqa: PLC0415
    _ins_sig = _handoff_ins.detect_insight_candidate(args.get("title") or "")
    if _ins_sig:
        _extra["insight_hint"] = (
            f"The wording ('{_ins_sig}') looks like a decision/insight — consider "
            "add_insight() or pin_decision() so it isn't lost at session end."
        )
    # a8550238 — surface code-prospecting context INLINE at add time, so the
    # planner sees which real files/symbols the item touches (and can prospect
    # them) while the sprint is still being shaped — not only later at claim
    # time. 926bf221 — always report an explicit prospecting_status so the
    # caller can distinguish prospected / no-targets / skipped / errored.
    _pc_ctx, _pc_status = _prospecting_result(_new_item)
    _extra["prospecting_status"] = _pc_status
    if _pc_ctx:
        _extra["code_context"] = _pc_ctx
    # 691f4e1c — persist a DURABLE symbol pointer (not just the inline hint) when
    # the item declares real symbols, so prospecting is queryable later via
    # get_sprint_item_pointers / resolvable through the tunnel, instead of a
    # one-shot code_context the caller has to act on manually. Fully guarded.
    _ptr = await _persist_prospected_pointer(
        db, args["project_id"], _new_item, _pc_status
    )
    if _ptr:
        _extra["prospected_pointer"] = _ptr
    # 7fe57cea — when the caller declared explicit touches_resources at file-only
    # granularity (file:path.py, no :symbol), emit a non-blocking informational
    # hint suggesting symbol-scoped form instead.  Only fires for explicit caller
    # declarations — inferred: and symbol: entries are already handled or fine.
    _sym_hint = _check_file_only_resources_warning(
        args.get("touches_resources"),
        args.get("title") or "",
        args.get("notes") or "",
    )
    if _sym_hint:
        _extra["symbol_scope_hint"] = _sym_hint
    # a389b95d — nudge callers to run assign_sprint_waves when unassigned items
    # exist and no session is in-flight (safe to re-label). Mirrors the
    # drift_warning / symbol_scope_hint pattern: non-blocking, info-only field.
    _wave_hint = await _wave_assignment_hint(db, args["project_id"])
    if _wave_hint:
        _extra["wave_assignment_hint"] = _wave_hint
    if _extra:
        _new_item = {**_new_item, **_extra}
    return _new_item


async def handle_fan_out_sprint_items(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: fan_out_sprint_items."""
    from ..handler import (  # noqa: PLC0415
        _infer_touches_resources,
        _active_executor_session_warnings,
        _check_file_only_resources_warning,
        _wave_assignment_hint,
        _prospecting_result,
        _persist_prospected_pointer,
    )
    items = args.get("items")
    if not isinstance(items, list) or not items:
        return {"error": "items must be a non-empty list of {title, ...} dicts"}
    _enriched: list[dict] = []
    for spec in items:
        validate_input_size(spec.get("title"), "sprint item title", 500)
        validate_input_size(spec.get("description"), "sprint item description", 10_000)
        # 07bdfdbb — auto-infer per-item touches_resources when none supplied,
        # so a fanned-out batch doesn't become an all-undeclared pile-up.
        if isinstance(spec, dict) and not spec.get("touches_resources"):
            _inf = _infer_touches_resources(spec.get("title") or "")
            if _inf:
                spec = {**spec, "touches_resources": _inf}
        _enriched.append(spec)
    ids = await db_module.fan_out_sprint_items(
        db, args["project_id"], _enriched
    )
    _result: dict[str, Any] = {"item_ids": ids, "count": len(ids)}
    # 02a52bf6 — mirror add_sprint_item's inline prospecting/pointer-persist for
    # each bulk-filed item. Without this, an item fanned out with real (declared
    # or inferred) touches_resources ends up with zero rows in
    # sprint_item_pointers and unconditionally hits the claim-time UNPROSPECTED
    # gate (meridian/db/sprint_items.py claim_sprint_item) regardless of whether
    # its target symbol genuinely exists — add_sprint_item never has this problem
    # because it already calls _prospecting_result + _persist_prospected_pointer
    # inline; fan_out_sprint_items only ever returned bare ids and skipped both.
    # Best-effort per item: a prospecting/persist failure for one item must never
    # break the rest of the batch or the fan-out call itself.
    _fo_prospecting_status: dict[str, str] = {}
    _fo_prospected_pointers: dict[str, Any] = {}
    for _fo_id in ids:
        try:
            _fo_item = await db_module.get_sprint_item(db, _fo_id)
        except Exception:  # noqa: BLE001 — best-effort; never break fan-out
            _fo_item = None
        _fo_ctx, _fo_status = _prospecting_result(_fo_item)
        _fo_prospecting_status[_fo_id] = _fo_status
        _fo_ptr = await _persist_prospected_pointer(
            db, args["project_id"], _fo_item, _fo_status
        )
        if _fo_ptr:
            _fo_prospected_pointers[_fo_id] = _fo_ptr
    if _fo_prospecting_status:
        _result["prospecting_status"] = _fo_prospecting_status
    if _fo_prospected_pointers:
        _result["prospected_pointers"] = _fo_prospected_pointers
    # 586eeda9 — same active-session warning as add_sprint_item: a fanned-out
    # batch injected mid-run is a board change executors should know about.
    _fo_warnings = await _active_executor_session_warnings(db, args["project_id"])
    if _fo_warnings:
        _result["active_session_warning"] = (
            "WARNING: " + "; ".join(_fo_warnings)
            + " — items added but may not be picked up until next session start."
        )
    # 7fe57cea — same symbol-scope hint as add_sprint_item: collect per-item
    # file-only resource warnings so the planner can upgrade them before
    # executors claim and parallelize.
    _fo_sym_hints: list[str] = []
    for _spec in items:
        if not isinstance(_spec, dict):
            continue
        _sh = _check_file_only_resources_warning(
            _spec.get("touches_resources"),
            _spec.get("title") or "",
            _spec.get("description") or "",
        )
        if _sh:
            _fo_sym_hints.append(_sh)
    if _fo_sym_hints:
        _result["symbol_scope_hints"] = _fo_sym_hints
    # a389b95d — same wave-assignment nudge as add_sprint_item: after a batch
    # fan-out, the newly-filed items almost certainly have wave IS NULL.
    _fo_wave_hint = await _wave_assignment_hint(db, args["project_id"])
    if _fo_wave_hint:
        _result["wave_assignment_hint"] = _fo_wave_hint
    return _result


async def handle_update_sprint_item(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: update_sprint_item."""
    from ..handler import (  # noqa: PLC0415
        _prospecting_result,
        _persist_prospected_pointer,
    )
    validate_input_size(args.get("title"), "sprint item title", 500)
    validate_input_size(args.get("notes"), "sprint item notes", 50_000)
    # 586eeda9 — hard-block mutating an in_progress item: an executor owns it
    # and a concurrent title/notes/resources change is undefined behaviour.
    # force=true overrides (destructive). Mirrors the set_sprint force guard.
    if not (args.get("force") in (True, 1, "true", "1", "yes")):
        _cur = await db_module.get_sprint_item(db, args["item_id"])
        if _cur is not None and _cur.get("status") == "in_progress":
            _owner = ""
            _ca = _cur.get("claimed_at")
            if _ca:
                _owner = f" (claimed {_ca})"
            return {
                "error": "IN_PROGRESS",
                "message": (
                    f"Item is in_progress and owned by an active session{_owner} — "
                    "cannot mutate while claimed. Wait for completion, pass "
                    "status='pending' together with force=true to release a "
                    "stale claim, or pass force=true to override for other fields."
                ),
                "item_id": args["item_id"],
                "claimed_at": _ca,
            }
    _patch_kwargs: dict[str, Any] = dict(
        title=args.get("title"),
        version=args.get("version"),
        notes=args.get("notes"),
        human_id=args.get("human_id"),
        item_group=args.get("group"),
    )
    # dcbd55a0 — expose a NARROW status-recovery path. update_sprint_item's own
    # STALE_CLAIM/IN_PROGRESS guard messages have long told callers to
    # force-reclaim a stuck in_progress claim via
    # update_sprint_item(status='pending'), but this handler never actually
    # forwarded a status key to patch_sprint_item, so that documented recovery
    # path silently did nothing -- the item just stayed in_progress forever
    # (the "crashed session leaves items stuck" bug). Fixed here, but
    # deliberately NOT a general status setter: patch_sprint_item's own status
    # kwarg already hard-rejects every terminal/guarded status (done, skipped,
    # failed, pushed, in_progress, provisional_complete) with a ValueError
    # naming the correct dedicated function (fix 6a17e735, closing the earlier
    # d71cfaaf backdoor), so we simply forward the value and let that existing,
    # tested gate do the validation -- no new allowlist duplicated here.
    if "status" in args:
        _patch_kwargs["status"] = args.get("status")
    # 501ec93f — only forward touches_resources when the caller supplied it,
    # so omitting the key leaves the stored value untouched (_UNSET sentinel).
    if "touches_resources" in args:
        _patch_kwargs["touches_resources"] = args.get("touches_resources")
    # 5823db0b — allow flagging an item as requiring completion evidence.
    if "required_notes" in args:
        _patch_kwargs["required_notes"] = args.get("required_notes")
    # dec69708 — set/clear the enforced deferral + track. Only forward when the
    # caller supplied the key, so omitting it leaves the stored value untouched
    # (patch_sprint_item uses the _UNSET sentinel); pass "" / null to clear.
    if "deferred_until" in args:
        _patch_kwargs["deferred_until"] = args.get("deferred_until")
    if "track" in args:
        _patch_kwargs["track"] = args.get("track")
    # e08fee30 — set the priority enum. Only forward when supplied (None leaves
    # the stored value untouched in patch_sprint_item).
    if args.get("priority") is not None:
        _patch_kwargs["priority"] = args.get("priority")
    # 2282a636 — set/clear blocker_kind. Only forward when the caller supplied
    # the key, so omitting it leaves the stored value untouched (_UNSET
    # sentinel); pass "" / null to clear (ordinary item), or 'manual' to set.
    if "blocker_kind" in args:
        _patch_kwargs["blocker_kind"] = args.get("blocker_kind")
    # 58a45b92 — set/clear the stored wave label. Only forward when the caller
    # supplied the key (_UNSET sentinel), so omitting it leaves the stored value
    # untouched; pass "" / null to clear (unassigned).
    if "wave" in args:
        _patch_kwargs["wave"] = args.get("wave")
    # 3d6bd938 — set/clear sprint_name. Only forward when the caller supplied the
    # key (_UNSET sentinel), so omitting it leaves the stored value untouched;
    # pass "" / null to clear (no name), or a non-empty string to set.
    if "sprint_name" in args:
        _patch_kwargs["sprint_name"] = args.get("sprint_name")
    # 94c26322 — set/clear prospect_bypass. Only forward when the caller supplied
    # the key (_UNSET sentinel). True/1 sets the bypass (human override allowing
    # an unprospected item through the goal-generation and claim safety gates);
    # False/0/null clears it (re-enables the structural gate).
    if "prospect_bypass" in args:
        _patch_kwargs["prospect_bypass"] = args.get("prospect_bypass")
    # 56f607ec — set/clear depends_on. Only forward when the caller supplied the
    # key (_UNSET sentinel), so omitting it leaves the stored value untouched;
    # pass "" / null to clear (independently claimable), or another sprint
    # item's id to set/fix dependency ordering retroactively. Previously this
    # was creation-time-only via add_sprint_item.
    if "depends_on" in args:
        _patch_kwargs["depends_on"] = args.get("depends_on")
    # e2e1b682 — set/clear require_verification. Only forward when the caller
    # supplied the key (_UNSET sentinel). True/1 sets the independent
    # fresh-session verifier gate (complete_sprint_item then requires an
    # on-file PASS filed by a session distinct from the one completing it);
    # False/0/null clears it (ordinary completion, evidence gate only).
    if "require_verification" in args:
        _patch_kwargs["require_verification"] = args.get("require_verification")
    # 4d1fb28f — set/clear the required_tool pin. Only forward when the caller
    # supplied the key (_UNSET sentinel), so omitting it leaves the stored
    # value untouched; pass "" / null to clear (ordinary executor discretion),
    # or a tool/plugin name to pin it — rendered as hard /goal guidance.
    if "required_tool" in args:
        _patch_kwargs["required_tool"] = args.get("required_tool")
    # 76dde31f (665 follow-up) — set/clear/replace the typed tool_requirements
    # contract. Only forward when the caller supplied the key (_UNSET
    # sentinel), so omitting it leaves the stored value untouched; pass
    # None/[] to clear (falls back to required_tool if still set), or a list
    # of typed entries to set/replace it wholesale.
    if "tool_requirements" in args:
        _patch_kwargs["tool_requirements"] = args.get("tool_requirements")
    # 2f9cb288 (665 follow-up) — set/clear/replace the typed artifact
    # declaration contract. Only forward when the caller supplied the key
    # (_UNSET sentinel), so omitting it leaves the stored value untouched;
    # pass null (or "" for artifact_kind) to clear, or a valid value to
    # set/replace it wholesale.
    if "artifact_kind" in args:
        _patch_kwargs["artifact_kind"] = args.get("artifact_kind")
    if "planned_output" in args:
        _patch_kwargs["planned_output"] = args.get("planned_output")
    if "policy" in args:
        _patch_kwargs["artifact_policy"] = args.get("policy")
    # 7c82f7c8 — set/clear github_channel. Only forward when the caller
    # supplied the key (_UNSET sentinel), so omitting it leaves the stored
    # value untouched; pass "" / null to clear, or one of
    # {nightly, stable, graduated} to set it.
    if "github_channel" in args:
        _patch_kwargs["github_channel"] = args.get("github_channel")
    try:
        item = await db_module.patch_sprint_item(
            db, args["project_id"], args["item_id"], **_patch_kwargs
        )
    except ValueError as exc:
        return {"error": str(exc)}
    if not item:
        return {"error": "sprint item not found"}
    # 926bf221 — update_sprint_item now auto-prospects too (previously only
    # add/claim did), so a substantive edit that names real files re-derives
    # code context instead of staying null. Explicit prospecting_status keeps
    # success / no-targets / skipped distinguishable to the caller.
    _u_ctx, _u_status = _prospecting_result(item)
    item = {**item, "prospecting_status": _u_status}
    if _u_ctx:
        item["code_context"] = _u_ctx
    # 691f4e1c — persist the durable symbol pointer on update too (skips if the
    # item already carries a code/symbol pointer, so re-editing doesn't stack
    # duplicates). Guarded; a persist failure never breaks the update.
    _u_ptr = await _persist_prospected_pointer(
        db, args["project_id"], item, _u_status
    )
    if _u_ptr:
        item["prospected_pointer"] = _u_ptr
    return item


async def handle_set_sprint(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: set_sprint."""
    # 62d321dd — guard: warn if pending items were never started before rolling sprint
    if not args.get("force"):
        _unstarted = [
            it for it in await db_module.get_sprint_items(db, args["project_id"], status="pending")
            if it.get("claimed_at") is None
        ]
        if _unstarted:
            _list = "\n".join(
                f'  [{it["id"][:8]}] {it.get("title","")[:100]}'
                for it in _unstarted[:10]
            )
            return {
                "warning": (
                    f"WARNING: {len(_unstarted)} item(s) from the current sprint were never "
                    f"started:\n{_list}\n"
                    "Proceeding will leave these orphaned. Move them to the new sprint version "
                    "or push to backlog first. Call set_sprint again with force=true to override."
                ),
                "unstarted_count": len(_unstarted),
                "unstarted_ids": [it["id"] for it in _unstarted],
                "sprint_not_updated": True,
            }
    result = await db_module.set_sprint(db, args["project_id"], args["sprint"])
    await goal_md_module.sync_db_to_goal_md(db, args["project_id"])
    return result


async def handle_get_sprint_progress(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_sprint_progress.

    0507f4a1 — sprint progress summary.
    """
    from ..handler import _board_change_for_session  # noqa: PLC0415
    _version_filter = args.get("version")
    _group_filter = args.get("item_group")
    # Short TTL cache (a1d75ff3): parallel executors polling between tasks share
    # one DB query. Bounded, per-instance staleness only — see the a1d75ff3 note
    # above _SPRINT_ITEMS_CACHE in meridian/db/sprint_items.py.
    _all = await db_module.get_sprint_items_cached(db, args["project_id"])
    if _version_filter:
        _all = [it for it in _all if it.get("version") == _version_filter]
    if _group_filter:
        _all = [it for it in _all if it.get("item_group") == _group_filter]
    _counts: dict[str, int] = {}
    for _it in _all:
        _st = _it.get("status") or "pending"
        _counts[_st] = _counts.get(_st, 0) + 1
    _done_n = _counts.get("done", 0)
    _total = len(_all)
    _pct = round(100 * _done_n / _total) if _total else 0
    _resp_progress = {
        "total": _total,
        "done": _done_n,
        "in_progress": _counts.get("in_progress", 0),
        "provisional_complete": _counts.get("provisional_complete", 0),
        "pending": _counts.get("pending", 0),
        "failed": _counts.get("failed", 0),
        "skipped": _counts.get("skipped", 0),
        "percent_complete": _pct,
        "by_status": _counts,
    }
    # 1da83459 — summary-only: the per-item list scaled ~100 chars/item and
    # bloated every between-item progress poll on a large board. Counts +
    # by_status + board_change are what an executor needs; call
    # get_sprint_items(status="pending") for the live item list.
    _bc = await _board_change_for_session(db, args["project_id"], args.get("session_id"))
    if _bc:
        _resp_progress["board_change"] = _bc
    # f78d7644 — surface the wave-urgent* lane (assign_sprint_waves) so a
    # polling orchestrator sees urgent work is claimable RIGHT NOW, in
    # parallel with whatever wave-N is currently in flight, rather than
    # having to diff get_sprint_items for it. Guarded: never break the
    # progress poll.
    try:
        _urgent_ready = [
            it for it in _all
            if (it.get("priority") or "normal") == "urgent"
            and str(it.get("wave") or "").startswith("wave-urgent")
            and (it.get("status") or "pending") in ("pending", "todo")
        ]
        if _urgent_ready:
            _resp_progress["urgent_wave"] = {
                "count": len(_urgent_ready),
                "item_ids": [it["id"] for it in _urgent_ready],
                "message": (
                    f"{len(_urgent_ready)} urgent item(s) are queued in wave-urgent — "
                    "claimable immediately, alongside any wave already in flight. "
                    "Don't wait for the current wave to close."
                ),
            }
    except Exception:  # noqa: BLE001
        pass
    # f9188526 — include version bucket descriptions so a caller sees the
    # concise summary for each version without a separate request.
    # Guarded: a DB failure here must never break the progress poll.
    try:
        _ver_descs = await db_module.get_all_sprint_version_descriptions(
            db, args["project_id"]
        )
        if _ver_descs:
            _resp_progress["version_descriptions"] = _ver_descs
    except Exception:  # noqa: BLE001 — descriptions are informational only
        pass
    # 5abf3e12 — when scoped to a session, report that session's live goal
    # compliance (N items it took on via actor= vs M complete_sprint_item()'d)
    # so an executor can see mid-run whether it's on track to fully complete
    # its /goal list. Guarded: never break the progress poll.
    _sess_id = args.get("session_id")
    if _sess_id:
        try:
            _resp_progress["goal_compliance"] = (
                await db_module.compute_session_goal_compliance(
                    db, args["project_id"], _sess_id
                )
            )
        except Exception:  # noqa: BLE001
            pass
    return _resp_progress


async def handle_get_sprint_items(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_sprint_items."""
    include_human = args.get("human", True)
    if isinstance(include_human, bool):
        pass
    else:
        include_human = str(include_human).lower() not in ("false", "0", "no")
    _items = await db_module.get_sprint_items(
        db, args["project_id"],
        status=args.get("status"),
        include_human=include_human,
    )
    # 10c0f6a0 — stale-session warning: in_progress items claimed >2h ago
    from datetime import datetime as _dt_cls  # noqa: PLC0415
    _now_utc = _dt_cls.utcnow()
    for _i, _it in enumerate(_items):
        if _it.get("status") == "in_progress" and _it.get("claimed_at"):
            try:
                _ca = _dt_cls.fromisoformat(_it["claimed_at"].split(".")[0].replace("Z", ""))
                _age_h = (_now_utc - _ca).total_seconds() / 3600
                if _age_h > 2:
                    _items[_i] = {**_it, "stale_warning": True, "stale_age_hours": round(_age_h, 1)}
            except Exception:  # noqa: BLE001
                pass
    # 9d8e858c — default-collapse item_group/parent_id clusters into one
    # summary row each so a caller browsing the board isn't flooded by every
    # fanned-out subtask; expand=true (default false) restores the full list.
    _expand = args.get("expand", False)
    if isinstance(_expand, str):
        _expand = _expand.lower() not in ("false", "0", "no", "")
    return db_module.collapse_sprint_item_clusters(_items, expand=bool(_expand))


async def handle_get_parallelizable_groups(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_parallelizable_groups.

    255096d9 — cluster pending items safe to fan out simultaneously.
    """
    _grp = await db_module.get_parallelizable_groups(
        db, args["project_id"], version=args.get("version")
    )
    # de730a25 — flag undeclared items prominently. They each run in their own
    # sequential group now, but the orchestrator should know parallel safety
    # couldn't be proven for them.
    _und = _grp.get("undeclared_count", 0)
    if _und:
        _grp["warning"] = (
            f"{_und} item(s) lack resource declarations — parallel safety not "
            "guaranteed; each is scheduled in its own sequential group. Add "
            "touches_resources to let them parallelize."
        )
    return _grp


async def handle_assign_sprint_waves(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: assign_sprint_waves.

    58a45b92 — persist the parallelizable grouping onto each eligible item's
    stored `wave` field so parallelism is deterministic + inspectable, then
    hand-editable via update_sprint_item(wave=...).
    """
    from ..handler import _active_executor_session_warnings  # noqa: PLC0415
    _wave_result = await db_module.assign_sprint_waves(
        db, args["project_id"], version=args.get("version")
    )
    # 605ca2c4 — warn if active executor sessions exist when re-running
    # assign_sprint_waves: greedy coloring order can shift if the pending set
    # changed since the last assignment, silently relabeling still-pending items
    # to different wave numbers and desyncing a mid-flight /goal that already
    # references specific wave labels.
    _wave_warnings = await _active_executor_session_warnings(db, args["project_id"])
    if _wave_warnings:
        _wave_result["active_session_warning"] = (
            "WARNING: " + "; ".join(_wave_warnings)
            + " — re-labeling wave numbers while a session is mid-flight can"
            " desync it from a /goal string that already references specific wave labels."
        )
    return _wave_result


async def handle_analyze_sprint(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: analyze_sprint.

    e77f09d1 — one-call planning brief: parallelism + dependency chains +
    resource conflicts + stalls synthesized for a planning session.
    """
    return await db_module.analyze_sprint(
        db, args["project_id"], version=args.get("version")
    )


async def handle_claim_sprint_item(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: claim_sprint_item."""
    from ..handler import (  # noqa: PLC0415
        _parse_touches_files,
        _sprint_item_file_claim_conflicts,
        _sprint_item_resource_claim_gate,
        _rollback_sprint_item_resource_locks,
        _board_change_for_session,
        _suggest_files_for_title,
        _prospect_code_context,
        _code_notes_for_item_resources,
    )
    # ITEM 3 — protect installer scripts: refuse to claim a sprint item whose
    # touches_files includes hooks.ps1 / hooks.sh unless force=true is passed.
    _force = args.get("force") in (True, 1, "true", "1", "yes")
    if not _force:
        _pitem = await db_module.get_sprint_item(db, args["item_id"])
        if _pitem is not None:
            _touched = [p.lower() for p in _parse_touches_files(_pitem.get("touches_files"))]
            _hits = sorted({fn for fn in ("hooks.ps1", "hooks.sh")
                            if any(t == fn or t.endswith("/" + fn) for t in _touched)})
            if _hits:
                return {
                    "error": "PROTECTED",
                    "message": ("Sprint item touches protected installer scripts "
                                f"({', '.join(_hits)}). Pass force=true to override."),
                    "protected_files": _hits,
                }

    # 0716c9e0 — parallel safety: load project settings once for both
    # auto_worktrees (suggest worktree by default) and isolation=worktree.
    _suggest_worktree = False
    _exec_cfg: dict[str, Any] = {}
    _proj_settings_claim: dict[str, Any] = {}
    try:
        _ps = await db_module.get_project_settings(db, args["project_id"])
        _proj_settings_claim = _ps or {}
        _raw_cfg = _proj_settings_claim.get("executor_config")
        if _raw_cfg:
            _exec_cfg = json.loads(_raw_cfg) if isinstance(_raw_cfg, str) else (_raw_cfg or {})
    except Exception:  # noqa: BLE001
        pass
    _isolation = (_exec_cfg or {}).get("isolation", "")
    _aw_raw = _proj_settings_claim.get("auto_worktrees")
    _auto_worktrees = bool(int(_aw_raw) if _aw_raw is not None else 1)
    # Compute file-claim overlaps once. In worktree mode they're a soft
    # informational warning (worktrees isolate the edit); otherwise a hard
    # block. (d01a74bf part 2)
    _file_conflicts = await _sprint_item_file_claim_conflicts(
        db,
        args["project_id"],
        args["item_id"],
        exclude_session_id=args.get("session_id"),
    )
    if _isolation == "worktree" or _auto_worktrees:
        _suggest_worktree = True
    else:
        if _file_conflicts:
            return {
                "error": "CONFLICT",
                "message": "Cannot claim sprint item: active session has overlapping claimed files.",
                "conflicts": _file_conflicts,
            }

    # 18c488b6 — symbol-scoped resource-lock gate: ACQUIRES (not just checks)
    # a file or symbol claim for every touches_resources entry the item
    # declares, under the caller's session_id. Unlike the touches_files
    # CONFLICT check above, this is NEVER softened by worktree isolation —
    # worktrees isolate the working tree, not the eventual merge, so a real
    # symbol-range overlap stays a hard block regardless of isolation mode.
    _resource_lock_gate = await _sprint_item_resource_claim_gate(
        db, args["project_id"], args["item_id"], args.get("session_id"),
        resource_contents=args.get("resource_contents"),
    )
    if not _resource_lock_gate.get("ok"):
        return _resource_lock_gate

    try:
        # 5823db0b — actor attribution: record who claimed the item (explicit
        # actor arg, else the claiming session id).
        _claim_actor = args.get("actor") or args.get("session_id")
        item = await db_module.claim_sprint_item(
            db, args["project_id"], args["item_id"], actor=_claim_actor
        )
    except ValueError:
        # 18c488b6 — the status transition never landed for THIS session (lost
        # the race, or the item was otherwise unclaimable) — any resource locks
        # the gate above newly acquired must not outlive a claim that never
        # actually happened.
        await _rollback_sprint_item_resource_locks(
            db, args.get("session_id"), _resource_lock_gate
        )
        # 10c0f6a0 — if already in_progress, check for stale claim and surface info
        _stale_item = await db_module.get_sprint_item(db, args["item_id"])
        if _stale_item and _stale_item.get("status") == "in_progress" and _stale_item.get("claimed_at"):
            from datetime import datetime as _dt_cls  # noqa: PLC0415
            try:
                _ca = _dt_cls.fromisoformat(_stale_item["claimed_at"].split(".")[0].replace("Z", ""))
                _age_h = (_dt_cls.utcnow() - _ca).total_seconds() / 3600
                if _age_h > 2:
                    return {
                        "error": "STALE_CLAIM",
                        "message": (
                            f"Item is in_progress but claimed {round(_age_h, 1)}h ago with no recent "
                            "activity — the claiming session may have ended. Safe to force-reclaim "
                            "by calling update_sprint_item(status='pending', force=true)."
                        ),
                        "stale_age_hours": round(_age_h, 1),
                        "claimed_at": _stale_item["claimed_at"],
                        "item": _stale_item,
                    }
            except Exception:  # noqa: BLE001
                pass
        # df573218 — claim race: another session grabbed this item between
        # planning and claiming. Instead of crashing the worker with a hard
        # error, point it at the next claimable item so it can keep going.
        _next_item = None
        try:
            _grp = await db_module.get_parallelizable_groups(
                db, args["project_id"], version=_stale_item.get("version") if _stale_item else None
            )
            for _group in _grp.get("groups", []):
                for _cand in _group:
                    if _cand.get("id") != args["item_id"]:
                        _next_item = _cand
                        break
                if _next_item is not None:
                    break
        except Exception:  # noqa: BLE001
            pass
        return {
            "status": "already_claimed",
            "item_id": args["item_id"],
            "current_status": (_stale_item or {}).get("status"),
            "next_available_id": (_next_item or {}).get("id"),
            "next_available_title": (_next_item or {}).get("title"),
            "message": (
                "Item was already claimed by another session. "
                + (
                    f"Claim next_available_id ({(_next_item or {}).get('id')}) instead."
                    if _next_item else
                    "No other claimable items remain in this version."
                )
            ),
        }
    # dec69708 — ENFORCED deferral: claim_sprint_item returns a blocked dict
    # (not a real item) when deferred_until is in the future. Surface it as-is
    # and stop; the item was NOT claimed, so the worktree plumbing below (which
    # assumes a real item row) must be skipped.
    if isinstance(item, dict) and item.get("blocked"):
        # 18c488b6 — a structural gate (DEFERRED/SUPERSEDED/WAVE_GATE_PENDING/
        # UNPROSPECTED) declined the transition — same "never outlive a claim
        # that didn't land" rollback as the ValueError-race path above.
        await _rollback_sprint_item_resource_locks(
            db, args.get("session_id"), _resource_lock_gate
        )
        return item
    if item is None:
        await _rollback_sprint_item_resource_locks(
            db, args.get("session_id"), _resource_lock_gate
        )
        raise ValueError("sprint item not found")

    if _suggest_worktree:
        item_id_short = item["id"][:8]
        _session_id_claim = args.get("session_id") or ""
        if _auto_worktrees and _isolation != "worktree" and _session_id_claim:
            # Default path: .claude/worktrees/{session_id_short} (gitignored)
            wt_branch = f"worktree/{item_id_short}"
            wt_path = f".claude/worktrees/{_session_id_claim[:8]}"
        else:
            # Legacy isolation=worktree path: repo-relative sibling dir
            repo_path = ""
            _repo_paths = _exec_cfg.get("repo_paths")
            if _repo_paths and isinstance(_repo_paths, list) and _repo_paths:
                _first = _repo_paths[0]
                repo_path = (_first.get("cwd") or "") if isinstance(_first, dict) else str(_first)
            if not repo_path:
                repo_path = _exec_cfg.get("repo_path") or ""
            repo_name = os.path.basename(repo_path.rstrip("/\\")) if repo_path else "repo"
            wt_branch = f"worktree/{item_id_short}"
            wt_path = f"../{repo_name}-worktree-{item_id_short}"
        item = dict(item)
        item.update({
            "worktree_suggested": True,
            "worktree_branch": wt_branch,
            "worktree_path": wt_path,
            # eb2e44f8 — the base branch this worktree is expected to be
            # created FROM. Matches worktree_merge_cmd's target below.
            # Executors should pass this (plus the base SHA they observe
            # right before `git worktree add`) to POST /worktrees'
            # base_branch/base_sha so an immutable base manifest gets
            # persisted for later merge-time validation.
            "worktree_base_branch": "dev",
            "worktree_setup_cmd": f"git worktree add {wt_path} -b {wt_branch}",
            "worktree_cleanup_cmd": f"git worktree remove {wt_path} --force",
            "worktree_merge_cmd": (
                f"git checkout dev && git merge {wt_branch} --no-edit "
                f"&& git branch -d {wt_branch}"
            ),
        })

    # Soft, non-blocking overlap warning (e.g. in worktree mode where the
    # hard CONFLICT check is intentionally skipped). (d01a74bf part 2)
    if _file_conflicts:
        item = dict(item)
        _paths = ", ".join(sorted({c["file_path"] for c in _file_conflicts}))
        item["file_overlap_warning"] = {
            "message": (
                f"Heads up: {_paths} also claimed by another live session. "
                "Your worktree isolates the edit, but coordinate before merging."
            ),
            "conflicts": _file_conflicts,
        }

    # 18c488b6 — expose the symbol/file resource-lock scope this claim actually
    # acquired (or explicitly fell back on) so the executor sees machine-readable
    # lock_scope metadata rather than having to infer it. Omitted entirely when
    # there was nothing to declare (no touches_resources) so the field stays
    # compact for the common case.
    if _resource_lock_gate.get("lock_scope"):
        item = dict(item)
        item["resource_lock_scope"] = _resource_lock_gate["lock_scope"]

    _bc_claim = await _board_change_for_session(
        db, args["project_id"], args.get("session_id")
    )
    if _bc_claim:
        item = dict(item)
        item["board_change"] = _bc_claim

    # f5726fd0 — suggest files to claim based on item title keywords.
    _sug = _suggest_files_for_title(item.get("title") or "")
    if _sug:
        item = dict(item)
        item["suggested_files"] = _sug

    # 04a15d3f — auto-prospect: surface the code-intel targets (files/symbols)
    # the executor should search before editing, derived from touches_resources
    # (or inferred from the title). Best-effort; never blocks the claim.
    try:
        _code_ctx = _prospect_code_context(item)
    except Exception:  # noqa: BLE001
        _code_ctx = None
    if _code_ctx:
        item = dict(item)
        item["code_context"] = _code_ctx

    # c37ea630 — proactively surface code-anchored notes for the item's
    # declared files/symbols at claim time. Mirrors the code_notes field
    # already returned by claim_file / get_file_claims, so executors see
    # relevant per-file warnings before they even decide which files to touch,
    # rather than only reactively when they call claim_file/get_file_claims
    # for a specific path later. Fail-open: empty list on error.
    try:
        _resource_code_notes = await _code_notes_for_item_resources(
            db, args["project_id"], item
        )
    except Exception:  # noqa: BLE001
        _resource_code_notes = []
    if _resource_code_notes:
        item = dict(item)
        item["touches_resources_code_notes"] = _resource_code_notes

    return item


async def handle_claim_parallel_batch(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: claim_parallel_batch.

    22cad9b8 — thin wrapper over db.claim_parallel_batch: atomically claim an
    entire parallel-safe batch of sprint items (every item's status AND
    every declared resource) before launching workers, closing the gap
    between get_parallelizable_groups computing a safe batch and each worker
    individually calling claim_sprint_item afterward. See
    meridian.db.sprint_items.claim_parallel_batch's docstring for the full
    contract (structured error codes, all-or-nothing rollback semantics, the
    immutable batch manifest).

    ``item_ids`` (required, non-empty list) is typically one group from
    get_parallelizable_groups' ``groups`` field. ``item_sessions`` (optional
    ``{item_id: session_id}``) pre-assigns each item to the distinct worker
    session that will execute it; omitted items default to the top-level
    ``session_id``. ``resource_contents`` (optional ``{file_path: source}``)
    supplies source text for any ``symbol:`` resources so they get a real
    AST-resolved symbol claim instead of falling back to a whole-file lock.
    """
    return await db_module.claim_parallel_batch(
        db, args["project_id"], args.get("session_id"), args.get("item_ids") or [],
        item_sessions=args.get("item_sessions"),
        resource_contents=args.get("resource_contents"),
        force_manifest=args.get("force_manifest") in (True, 1, "true", "1", "yes"),
        manifest_reason=args.get("manifest_reason"),
    )


async def handle_add_subtask(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: add_subtask.

    94938492 — project_id is resolved from project_name by the shared
    _dispatch_mcp_tool resolver on the HTTP path, but the stdio path has no
    such fallback for add_subtask and neither caller can help it if BOTH are
    omitted. Guard like handle_add_sprint_item does, instead of letting the
    direct args["project_id"] read below raise a raw KeyError that leaks as
    a cryptic JSON-RPC -32603.
    """
    if not args.get("project_id"):
        return {"error": "project_id is required (or pass project_name)"}
    return await db_module.add_subtask(
        db, args["project_id"], args["parent_id"], args["title"],
        owner=args.get("owner"),
    )


async def handle_split_sprint_item(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: split_sprint_item."""
    return await db_module.split_sprint_item(
        db, args["project_id"], args["item_id"], args["titles"]
    )


async def handle_merge_sprint_items(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: merge_sprint_items."""
    return await db_module.merge_sprint_items(
        db, args["project_id"], args["item_ids"], args["new_title"]
    )


async def handle_complete_sprint_item(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: complete_sprint_item."""
    from ..handler import (  # noqa: PLC0415
        _verify_item_ci,
        _fetch_recent_commits,
        _unclaimed_file_warnings,
        _board_change_for_session,
        _close_or_propose_github_issue,
    )
    # 0716c9e0 — check active worktree before marking done.
    _complete_session_id = args.get("session_id") or ""
    _merge_warning: dict[str, Any] | None = None
    if _complete_session_id:
        try:
            _ps_complete = await db_module.get_project_settings(db, args["project_id"])
            _req_merge = bool(int((_ps_complete or {}).get("require_merge_approval") or 1))
            if _req_merge:
                _wt = await db_module.get_active_worktree_for_session(db, _complete_session_id)
                if _wt:
                    # eb2e44f8 — HARD GATE: when this worktree has a persisted
                    # immutable base manifest, validate its actual git state
                    # (HEAD ancestry, dirty tree, manifest staleness) before
                    # completion is allowed to proceed at all — a real reject,
                    # not just the advisory HITL below. Self-hosted only: the
                    # git-level checks need local FS access, per the same
                    # architectural law worktree_cleanup already follows.
                    # Hosted mode and worktrees that never opted into a
                    # manifest (base_sha/base_branch never supplied at
                    # `POST /worktrees` time) fall through unchanged to the
                    # pre-existing advisory-only HITL behavior below.
                    _wt_manifest = await db_module.get_worktree_manifest(db, _wt["id"])
                    if _wt_manifest is not None and not _hosted_mode():
                        from meridian.worktree_merge_guard import (  # noqa: PLC0415
                            validate_worktree_merge,
                        )
                        _validation = await validate_worktree_merge(
                            db, _server._REPO_ROOT, _wt["id"]
                        )
                        if not _validation.get("ok"):
                            return {
                                "error": "WORKTREE_MERGE_BLOCKED",
                                "item_id": args["item_id"],
                                "worktree_id": _wt["id"],
                                "validation": _validation,
                                "message": (
                                    "Refusing to complete: worktree failed pre-merge "
                                    "validation ("
                                    + ", ".join(
                                        e["code"] for e in _validation.get("errors", [])
                                    )
                                    + "). Fix the underlying issue (commit/stash "
                                    "uncommitted changes, reconcile the branch with its "
                                    "recorded base, or reclaim a fresh worktree if the "
                                    "manifest is stale) and retry."
                                ),
                            }
                    _hitl = await db_module.request_hitl(
                        db, args["project_id"],
                        f"Session has active worktree on branch '{_wt['branch']}' "
                        f"at '{_wt['path']}'. Merge to main before closing. "
                        f"Run: git checkout dev && git merge {_wt['branch']} --no-edit",
                        session_id=_complete_session_id,
                        urgency="normal", kind="correction",
                    )
                    _merge_warning = {
                        "worktree_branch": _wt["branch"],
                        "worktree_path": _wt["path"],
                        "hitl_id": (_hitl or {}).get("id"),
                        "message": "Merge reminder filed — see HITL queue.",
                    }
        except Exception:  # noqa: BLE001
            pass

    # 427b7902 — HARD CI GATE: refuse to complete when GitHub Actions CI for the
    # commit named in the notes is GENUINELY FAILING. This upgrades the b121348e
    # advisory (which only WARNED post-completion) to a real gate, mirroring the
    # EVIDENCE_REQUIRED refusal: computed BEFORE marking done, returns a clean
    # error, and does NOT flip the item to done. Guardrails:
    #   * ONLY a real "failure" state blocks. "unknown" (no repo configured, no
    #     check-runs yet, self-hosted / no-GitHub) and "pending" (CI still
    #     running — the normal push-then-complete race) are ALWAYS allowed
    #     through, so this never blocks on absent/unknown CI.
    #   * Escape hatch consistent with existing force= patterns: pass
    #     override_ci=true to complete anyway (records that the failing CI was
    #     acknowledged). The result is cached so the advisory block below reuses
    #     it without a second GitHub round-trip.
    _ci_pre: dict[str, Any] | None = None
    _ci_checked = False
    _override_ci = bool(args.get("override_ci"))
    try:
        _pre_item = await db_module.get_sprint_item(db, args["item_id"])
    except Exception:  # noqa: BLE001
        _pre_item = None
    if _pre_item is not None and _pre_item.get("project_id") == args["project_id"]:
        _ci_text = f"{args.get('notes') or ''} {(_pre_item.get('notes') or '')}"
        _ci_pre = await _verify_item_ci(db, args["project_id"], tenant, _ci_text)
        _ci_checked = True
        if (
            not _override_ci
            and _ci_pre is not None
            and _ci_pre.get("state") == "failure"
        ):
            return {
                "error": "CI_FAILING",
                "item_id": args["item_id"],
                "ci_verification": _ci_pre,
                "message": (
                    f"Refusing to complete {args['item_id']}: GitHub Actions CI "
                    f"is FAILING for commit {_ci_pre.get('sha')} "
                    f"({_ci_pre.get('failed')}/{_ci_pre.get('total')} checks failed). "
                    "Fix CI and re-push, or pass override_ci=true to acknowledge "
                    "and complete anyway. (Unknown/pending CI is never blocked — "
                    "only a real failing status.)"
                ),
            }

    # 5823db0b — quality gate + actor attribution. Pass evidence notes and
    # the completing actor; surface the required_notes gate as a clean error.
    _complete_actor = args.get("actor") or _complete_session_id or None
    try:
        item = await db_module.complete_sprint_item(
            db, args["project_id"], args["item_id"],
            task_id=args.get("task_id"),
            notes=args.get("notes"),
            actor=_complete_actor,
            # e2e1b682 — a fresh, independent verifier subsession passes its
            # own session id + PASS/FAIL verdict so complete_sprint_item can
            # check (and, when supplied, file) the require_verification gate
            # in the same call. Ignored entirely for items without the gate.
            verifier_session_id=args.get("verifier_session_id"),
            verification_verdict=args.get("verification_verdict"),
            verification_notes=args.get("verification_notes"),
            # 8693b6a8 — claim-ownership gate escape hatch: explicit
            # acknowledgement that the caller is completing a DIFFERENT,
            # NON-stale session's live claim. Never inferred/defaulted true.
            force_foreign_claim=bool(args.get("force_foreign_claim")),
        )
    except db_module.SprintItemEvidenceRequired as exc:
        return {
            "error": "EVIDENCE_REQUIRED",
            "item_id": args["item_id"],
            "message": str(exc),
        }
    except db_module.SprintItemVerificationRequired as exc:
        return {
            "error": "VERIFICATION_REQUIRED",
            "item_id": args["item_id"],
            "message": str(exc),
        }
    except db_module.SprintItemClaimMismatch as exc:
        # 8693b6a8 — completing actor doesn't hold this item's claim, and the
        # claim is neither stale nor force-acknowledged. Surfaced distinctly
        # so an executor can tell "not mine to complete" apart from evidence/
        # verification gates and decide whether to force or back off.
        return {
            "error": "CLAIM_MISMATCH",
            "item_id": args["item_id"],
            "message": str(exc),
        }
    except db_module.SprintItemStatusRace as exc:
        # fa3e3331 — another caller already moved this item out of an
        # active state (e.g. a concurrent skip/fail/complete won the
        # race). Surface it distinctly rather than a misleading
        # "not found".
        return {
            "error": "STATUS_RACE",
            "item_id": exc.item_id,
            "current_status": exc.current_status,
            "message": str(exc),
        }
    if item is None:
        raise ValueError("sprint item not found")
    if _merge_warning:
        item = dict(item)
        item["merge_warning"] = _merge_warning
    # fdaa5b55 — item has a linked GitHub issue: auto-close (meridian_auto)
    # or post a proposed-closure comment + non-blocking HITL (manual/unset).
    # Never lets a GitHub failure undo the completion that already succeeded
    # above — any error is captured in github_issue_action["error"], not
    # raised. See _close_or_propose_github_issue for the trust-boundary and
    # single-issue-blast-radius guarantees.
    if item.get("github_issue_number"):
        try:
            _gh_action = await _close_or_propose_github_issue(
                db, args["project_id"], item, tenant,
            )
            if _gh_action:
                item = dict(item)
                item["github_issue_action"] = _gh_action
        except Exception:  # noqa: BLE001 — advisory/side-effect only
            pass
    # 02cd3992 — unclaimed-file warning: flag files modified without a lock.
    # Non-blocking; surfaces the open-door problem so the executor can act.
    if _complete_session_id:
        try:
            _unclaimed_warnings = await _unclaimed_file_warnings(
                db, _complete_session_id
            )
            if _unclaimed_warnings:
                item = dict(item)
                item["unclaimed_file_warnings"] = _unclaimed_warnings
        except Exception:  # noqa: BLE001
            pass
    # d01a74bf — surface board additions at the item boundary so a planner's
    # mid-run injections get picked up before the next claim.
    _bc_complete = await _board_change_for_session(
        db, args["project_id"], args.get("session_id")
    )
    if _bc_complete:
        item = dict(item)
        item["board_change"] = _bc_complete
    # b121348e / 427b7902 — INDEPENDENT CI verification. The 427b7902 hard gate
    # above already looked this up (and REFUSED completion on a real failing
    # state unless override_ci=true), so reuse that result — no second GitHub
    # round-trip. Attach ``ci_verification`` for transparency, and when CI is
    # genuinely FAILING (i.e. this was an acknowledged override_ci completion),
    # attach a ``ci_warning`` so the closed-on-red item stays visible.
    try:
        _ci = _ci_pre if _ci_checked else await _verify_item_ci(
            db, args["project_id"], tenant,
            f"{args.get('notes') or ''} {item.get('notes') or ''}",
        )
        if _ci is not None:
            item = dict(item)
            item["ci_verification"] = _ci
            if _ci.get("state") == "failure":
                item["ci_warning"] = (
                    f"⚠ GitHub Actions CI is FAILING for commit {_ci.get('sha')} "
                    f"({_ci.get('failed')}/{_ci.get('total')} checks) — this item "
                    f"was closed on a commit whose CI did not pass"
                    + (" (override_ci=true)." if _override_ci else ".")
                    + " Verify before trusting 'done'."
                )
    except Exception:  # noqa: BLE001 — advisory only; completion already succeeded
        pass
    # bb29a06f — ADVISORY completion sanity-check. Extends the required_notes
    # gate (evidence EXISTS) with a check that evidence is PLAUSIBLE: when the
    # completion looks weakly-supported (no linked task, no notes anywhere) and
    # no recent commit/migration shares keywords with the title, add a soft
    # nudge. NEVER blocks, never raises — the completion already succeeded. The
    # drift heuristic is noisy, so this is a hint, not a gate.
    try:
        _weakly_supported = not (
            args.get("task_id")
            or (args.get("notes") or "").strip()
            or (item.get("notes") or "").strip()
            or item.get("task_id")
        )
        if _weakly_supported:
            from ... import handoff as _handoff_advisory  # noqa: PLC0415
            try:
                _adv_project = await db_module.get_project(db, args["project_id"])
                _adv_commits = (
                    await _fetch_recent_commits(_adv_project, tenant)
                    if _adv_project else []
                )
            except Exception:  # noqa: BLE001 — never let commit-fetch break completion
                _adv_commits = []
            _adv_matches = _handoff_advisory.detect_sprint_item_drift(
                item.get("title") or "", _adv_commits,
            )
            if not _adv_matches:
                item = dict(item)
                item["completion_advisory"] = (
                    "No recent commit or linked evidence appears to reference "
                    "this item — double-check it actually shipped (this is a "
                    "heuristic; ignore if you completed it via docs/config/decision)."
                )
    except Exception:  # noqa: BLE001 — advisory must never affect completion
        pass
    # Notify only when the sprint is fully complete.
    active_statuses = {"pending", "todo", "in_progress"}
    remaining_items = await db_module.get_sprint_items(db, args["project_id"])
    if not any((it.get("status") or "") in active_statuses for it in remaining_items):
        await _server._maybe_notify(
            db, args["project_id"],
            "Sprint done ✓",
            "All sprint items are complete.",
            event="sprint_done",
            tenant=tenant,
            pref_key="sprint",
        )
    return item


async def handle_add_sprint_item_pointer(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: add_sprint_item_pointer.

    2976e168 — attach a GENERIC POINTER to a sprint item. Validation lives in
    db.add_sprint_item_pointer (via meridian.pointers.validate_pointer); a
    malformed pointer raises ValueError, surfaced here as a clean {error}.
    """
    if not args.get("project_id"):
        return {"error": "project_id is required (or pass project_name)"}
    if not args.get("sprint_item_id"):
        return {"error": "sprint_item_id is required"}
    validate_input_size(args.get("label"), "pointer label", 500)
    source_type = args.get("source_type") or ""
    targets = args.get("targets") or []
    if source_type == "web" and isinstance(targets, list):
        # 1d3f6e71 — archive the exact cited passage at CITATION TIME so it
        # survives link-rot / content-drift. Best-effort + guarded: an archiving
        # failure must NEVER block pointer creation — the pointer still stores the
        # live URL + exact quote, and drift is detected on resolve regardless.
        from ... import web_archive  # noqa: PLC0415
        for t in targets:
            if not isinstance(t, dict):
                continue
            sel = t.get("selector")
            uri = t.get("uri")
            if not (
                isinstance(sel, dict)
                and sel.get("type") == "text_quote"
                and isinstance(uri, str) and uri
                and not sel.get("archived_url")
            ):
                continue
            try:
                res = await web_archive.save_page_now(uri)
            except Exception:  # noqa: BLE001 — belt-and-suspenders
                res = None
            if isinstance(res, dict) and res.get("archived_url"):
                sel["archived_url"] = res["archived_url"]
                if res.get("archived_at"):
                    sel["archived_at"] = res["archived_at"]
            else:
                # Fallback: the deterministic Wayback "latest snapshot" URL.
                sel["archived_url"] = web_archive.wayback_latest_url(uri)
    try:
        return await db_module.add_sprint_item_pointer(
            db,
            args["project_id"],
            args["sprint_item_id"],
            source_type,
            targets,
            label=args.get("label"),
        )
    except ValueError as exc:
        return {"error": str(exc)}


async def handle_get_sprint_item_pointers(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_sprint_item_pointers."""
    if not args.get("sprint_item_id"):
        return {"error": "sprint_item_id is required"}
    pointers = await db_module.get_sprint_item_pointers(
        db, args["sprint_item_id"]
    )
    return {"sprint_item_id": args["sprint_item_id"], "pointers": pointers}


async def handle_resolve_sprint_item_pointers(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: resolve_sprint_item_pointers.

    2976e168 — resolve EVERY pointer on an item, dispatching by selector.type.
    Best-effort + guarded: unresolvable targets become {resolved:false}; the
    pass NEVER raises. node_id targets need the doc-structure store, resolved
    via the same tier-aware helper the citation tools use; zotero uses the
    pointers module's default seam (zotero_client). project_id scopes the
    code-graph search.

    653579c5 — symbol targets now resolve through :func:`prospect.build_symbol_resolver`,
    which tries the LIVE three-rung ``prospect_symbol`` chain (graph → Serena →
    semantic) using THIS handler's own ``tenant`` first, falling back to the
    cached ``codebase_graph_entities`` snapshot (``db.search_graph_entities``)
    that used to be the ONLY thing consulted. Previously ``tenant`` was a
    handler parameter that was never threaded anywhere, so a symbol pointer
    could never resolve against the live/tunnel-connected graph even when the
    exact same tenant's ``prospect_symbol`` / direct ``codebase__search_graph``
    calls resolved instantly — see build_symbol_resolver's docstring for the
    full root-cause writeup.
    """
    from ..handler import _resolve_ingest_doc_store  # noqa: PLC0415
    from ...pointers import resolve_pointer  # noqa: PLC0415
    from ...prospect import build_symbol_resolver  # noqa: PLC0415

    if not args.get("project_id"):
        return {"error": "project_id is required (or pass project_name)"}
    if not args.get("sprint_item_id"):
        return {"error": "sprint_item_id is required"}

    # Resolve the doc-structure store once for node_id lookups (best-effort;
    # None → node_id targets degrade to {resolved:false}).
    _ptr_store = await _resolve_ingest_doc_store(db, data_dir, tenant)

    async def _node_resolver(element_id: str) -> Any:
        if _ptr_store is None:
            return None
        try:
            return await _ptr_store.get_element_by_id(element_id)
        except Exception:  # noqa: BLE001 — resolver seam must never raise
            return None

    # 653579c5 — root_dir is optional: it only extends reach to the semantic
    # (search_code_semantic) rung when no tunnel/tenant is available; omitting
    # it just means that rung is skipped, same as prospect_symbol itself.
    _root_dir = str(args.get("root_dir") or "").strip()
    _symbol_resolver = build_symbol_resolver(
        tenant=tenant, root_dir=_root_dir, data_dir=data_dir,
    )

    pointers = await db_module.get_sprint_item_pointers(
        db, args["sprint_item_id"]
    )
    resolved: list[dict[str, Any]] = []
    for ptr in pointers:
        resolved.append(
            await resolve_pointer(
                db, ptr,
                project_id=args["project_id"],
                node_resolver=_node_resolver,
                symbol_resolver=_symbol_resolver,
            )
        )
    return {"sprint_item_id": args["sprint_item_id"], "pointers": resolved}


async def handle_delete_sprint_item_pointer(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: delete_sprint_item_pointer.

    98c71a42 — the DELETE (edit-via-replace) half of the pointer CRUD. The DB
    layer (db.delete_sprint_item_pointer) has existed since 2976e168, but no
    MCP tool wrapped it — a pointer could be created / listed / resolved yet
    never removed. Idempotent: {deleted:false} when no pointer had that id,
    rather than an error.
    """
    if not args.get("pointer_id"):
        return {"error": "pointer_id is required"}
    removed = await db_module.delete_sprint_item_pointer(db, args["pointer_id"])
    return {"pointer_id": args["pointer_id"], "deleted": removed}


async def handle_complete_wave_gate(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: complete_wave_gate.

    d2430713 — executor calls this AFTER actually running the wave's gate action
    list (e.g. push, deploy, wait, run_verification) to signal that the gate has
    passed and the next wave's items may now be claimed.

    The REAL structured result from run_verification MUST be passed as
    verification_payload.  The server validates it server-side (status=='ok',
    exit_code==0).  A plain self-report or a fabricated payload is rejected.

    Returns {gate_completed, wave_label, next_wave_label, next_wave_item_count,
    next_wave_item_ids, gate_id} on success, or raises ValueError on bad evidence.
    """
    project_id = str(args.get("project_id") or "").strip()
    project_name = str(args.get("project_name") or "").strip()
    if not project_id and project_name:
        _proj = await db_module.get_project_by_name(db, project_name)
        if _proj:
            project_id = _proj.get("id", "")
    if not project_id:
        return {"error": "project_id is required"}

    wave_label = str(args.get("wave_label") or "").strip()
    if not wave_label:
        return {"error": "wave_label is required (e.g. 'wave-1')"}

    verification_payload = args.get("verification_payload")
    if verification_payload is None:
        return {
            "error": "verification_payload is required",
            "hint": (
                "Pass the full dict returned by run_verification. "
                "Only a genuine run_verification result with status='ok' and "
                "exit_code=0 satisfies the gate — a self-report is rejected."
            ),
        }

    actor = str(args.get("actor") or "").strip() or None

    try:
        result = await db_module.complete_wave_gate(
            db, project_id, wave_label, verification_payload, actor=actor
        )
    except ValueError as exc:
        return {"error": str(exc)}

    return result


async def handle_start_wave_run(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: start_wave_run.

    2a654cb0 — open a durable wave run: an immutable wave_run_id pinned to the
    canonical expanded board snapshot (ef665ef8) the wave was planned against,
    so a session that dies mid-wave can be resumed against a manifest whose
    staleness is detectable rather than assumed.

    Builds the board snapshot server-side (the caller cannot supply one — a
    caller-supplied snapshot would defeat the point of pinning what the SERVER
    saw). Optionally records the wave's items as children with their
    failure_mode, and any degraded tools.

    Returns the created run, or {"error": ...}.
    """
    project_id = str(args.get("project_id") or "").strip()
    project_name = str(args.get("project_name") or "").strip()
    if not project_id and project_name:
        _proj = await db_module.get_project_by_name(db, project_name)
        if _proj:
            project_id = _proj.get("id", "")
    if not project_id:
        return {"error": "project_id is required"}

    version = str(args.get("version") or "").strip() or None
    wave_label = str(args.get("wave_label") or "").strip() or None
    actor = str(args.get("actor") or "").strip() or None

    item_ids = args.get("item_ids")
    if item_ids is not None and not isinstance(item_ids, list):
        return {"error": "item_ids must be a list of sprint item ids"}
    item_ids = [str(i) for i in (item_ids or [])]

    snapshot = await db_module.build_board_snapshot(db, project_id, version=version)

    try:
        run = await db_module.create_wave_run(
            db,
            project_id,
            version=version,
            wave_label=wave_label,
            snapshot=snapshot,
            item_ids=item_ids,
            actor=actor,
        )
    except ValueError as exc:
        return {"error": str(exc)}

    # Register the wave's items as children up front so their failure_mode is
    # on record BEFORE any of them can fail — a stop-mode contract discovered
    # only after the failure is not a contract.
    failure_modes = args.get("failure_modes")
    failure_modes = failure_modes if isinstance(failure_modes, dict) else {}
    for item_id in item_ids:
        mode = str(failure_modes.get(item_id) or "continue")
        if mode not in db_module.WAVE_RUN_CHILD_FAILURE_MODES:
            mode = "continue"
        await db_module.record_wave_run_child(
            db, run["id"], item_id, failure_mode=mode, status="running", actor=actor,
        )

    degraded = args.get("degraded_tools")
    if isinstance(degraded, list):
        for entry in degraded:
            if not isinstance(entry, dict) or not entry.get("tool"):
                continue
            await db_module.record_degraded_tool(
                db,
                run["id"],
                str(entry["tool"]),
                str(entry.get("reason") or "unspecified"),
                fallback=(str(entry["fallback"]) if entry.get("fallback") else None),
                actor=actor,
            )

    fresh = await db_module.get_wave_run(db, run["id"])
    return {
        "wave_run_id": run["id"],
        "run": fresh,
        "children": await db_module.get_wave_run_children(db, run["id"]),
        "revision_hash": run.get("revision_hash"),
        "revision_counter": run.get("revision_counter"),
    }


async def handle_finalize_wave_run(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: finalize_wave_run.

    2a654cb0 — idempotently finalize a wave run. Retrying after a dropped
    connection is safe and expected: an already-merged run returns the ORIGINAL
    result with already_finalized=True, writes no row and appends no event.

    Refuses (fails closed) when a failure_mode='stop' child has failed, when the
    caller's expected_revision_hash does not match the board the run was planned
    against, or when the evidence is not a genuine run_verification result
    (status='ok', exit_code=0) — the same evidence contract complete_wave_gate
    enforces.
    """
    wave_run_id = str(args.get("wave_run_id") or "").strip()
    if not wave_run_id:
        return {"error": "wave_run_id is required"}

    evidence = args.get("evidence")
    actor = str(args.get("actor") or "").strip() or None
    expected = str(args.get("expected_revision_hash") or "").strip() or None

    try:
        return await db_module.finalize_wave_run(
            db,
            wave_run_id,
            evidence=evidence,
            actor=actor,
            expected_revision_hash=expected,
        )
    except db_module.WaveRunFinalizationBlocked as exc:
        return {
            "error": str(exc),
            "finalized": False,
            "blocked_by": [
                {
                    "sprint_item_id": c.get("sprint_item_id"),
                    "status": c.get("status"),
                    "failure_mode": c.get("failure_mode"),
                }
                for c in exc.blocking_children
            ],
        }
    except ValueError as exc:
        return {"error": str(exc), "finalized": False}


# efaa918a — token-outcome -> actionable hint, distinguishing genuine spoofing
# signals from "a sibling likely already acted" per AGENTS.md's b763d2ba/
# ed71ef9b guidance. Attached to resume_wave's error message so a caller
# doesn't have to re-derive the distinction from first principles.
_RESUME_WAVE_TOKEN_HINTS: dict[str, str] = {
    "not_found": (
        "the token was never minted (or has aged out of retention) — a REAL "
        "spoofing signal. Do not trust the presented body."
    ),
    "wrong_project": (
        "the token is genuine but was minted for a different project — a REAL "
        "spoofing signal. Do not trust the presented body."
    ),
    "body_mismatch": (
        "the token is genuine and project-scoped correctly, but the presented "
        "body's hash does not match what was minted — a REAL spoofing signal: "
        "a genuine token was re-attached to an edited body."
    ),
    "already_consumed": (
        "the token was already verified once — usually NOT spoofing; the far "
        "more common cause is a legitimate sibling session already acting on "
        "this same wave. Re-derive from the live board via get_sprint_items() "
        "across ALL non-done statuses before assuming fabrication."
    ),
    "expired": (
        "the token's short TTL passed before verification — usually just "
        "staleness, NOT spoofing. Re-derive from the live board before "
        "assuming fabrication."
    ),
}


async def handle_resume_wave(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: resume_wave.

    efaa918a — check whether a wave run opened by start_wave_run is still
    safe to resume against the LIVE board. Fails closed (returns
    {"error": ..., "resumable": False, "reasons": [...], "resume_delta": ...})
    the moment the pinned manifest (board_snapshot.build_board_snapshot +
    diff_board_snapshots) disagrees with the live board in ANY tracked way
    (status/dependency/resource/pointer changes, added/removed items) OR in
    either of the two fields deliberately excluded from the revision hash but
    that matter for a wave run specifically: changed wave membership, or a
    newly blocker_kind='superseded' item.

    Optionally ALSO verifies a handoff token (goal_token, + presented_body to
    check the efaa918a body-hash binding) via
    meridian.handoff.verify_handoff_token, scoped to this run's own
    project_id. A failed token check refuses resume BEFORE the staleness
    check even runs (no point reporting board staleness for an unverified
    request) — see _RESUME_WAVE_TOKEN_HINTS for the actionable per-reason
    guidance, preserving the four pre-existing distinct outcomes
    (not_found/wrong_project are real spoofing signals; already_consumed/
    expired usually mean a sibling already acted) plus the new body_mismatch.

    Does NOT itself advance the wave run's status — call
    advance_wave_run_status(wave_run_id, "running") separately once
    resumable=True; this tool is a check, not a state transition.
    """
    wave_run_id = str(args.get("wave_run_id") or "").strip()
    if not wave_run_id:
        return {"error": "wave_run_id is required"}

    goal_token = args.get("goal_token")
    presented_body = args.get("presented_body")
    token_check: dict[str, Any] | None = None

    if goal_token:
        run_for_token = await db_module.get_wave_run(db, wave_run_id)
        if run_for_token is None:
            return {"error": f"Wave run {wave_run_id!r} not found.", "resumable": False}
        from meridian import handoff as handoff_module_local  # noqa: PLC0415 — avoid import cycle

        token_check = await handoff_module_local.verify_handoff_token(
            db,
            str(goal_token),
            run_for_token["project_id"],
            body=presented_body if isinstance(presented_body, str) else None,
        )
        if not token_check.get("valid"):
            _reason = token_check.get("reason", "")
            _hint = _RESUME_WAVE_TOKEN_HINTS.get(_reason, "")
            return {
                "error": (
                    f"resume_wave refused: handoff token verification failed "
                    f"(reason={_reason!r}) — {_hint}"
                ),
                "resumable": False,
                "token_check": token_check,
            }

    try:
        result = await db_module.check_wave_resume(db, wave_run_id)
    except db_module.WaveResumeStale as exc:
        return {
            "error": str(exc),
            "resumable": False,
            "reasons": exc.reasons,
            "resume_delta": exc.resume_delta,
            "token_check": token_check,
        }
    except ValueError as exc:
        return {"error": str(exc), "resumable": False, "token_check": token_check}

    result["token_check"] = token_check
    return result


async def handle_configure_wave_gate(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: configure_wave_gate.

    74a8f420 — configure (or on-the-fly reconfigure) a deterministic action
    pipeline (push_dev/push_main/deploy/wait/run_verification) attached to a
    wave or wave-range. Once configured, claim_sprint_item STRUCTURALLY
    refuses to claim any item whose wave sorts beyond ``wave_end`` until a
    matching complete_wave_gate call records real run_verification evidence —
    this is enforced at claim time, not just described in /goal prose.

    Returns {configured, gate_config_id, project_id, wave_start, wave_end,
    actions} on success, or {"error": ...} on bad input / an already-passed
    (immutable) boundary.
    """
    project_id = str(args.get("project_id") or "").strip()
    project_name = str(args.get("project_name") or "").strip()
    if not project_id and project_name:
        _proj = await db_module.get_project_by_name(db, project_name)
        if _proj:
            project_id = _proj.get("id", "")
    if not project_id:
        return {"error": "project_id is required"}

    wave_end = str(args.get("wave_end") or "").strip()
    if not wave_end:
        return {"error": "wave_end is required (e.g. 'wave-3')"}

    actions = args.get("actions")
    if not isinstance(actions, list) or not actions:
        return {
            "error": "actions is required and must be a non-empty list",
            "hint": (
                "Each entry is a dict with a 'type' in push_dev | push_main | "
                "deploy | wait | run_verification, e.g. "
                "[{\"type\": \"push_dev\"}, {\"type\": \"run_verification\"}, "
                "{\"type\": \"push_main\"}, {\"type\": \"deploy\"}]."
            ),
        }

    wave_start = str(args.get("wave_start") or "").strip() or None
    actor = str(args.get("actor") or "").strip() or None

    try:
        result = await db_module.configure_wave_gate(
            db, project_id, wave_end, actions, wave_start=wave_start, actor=actor,
        )
    except ValueError as exc:
        return {"error": str(exc)}

    return result
