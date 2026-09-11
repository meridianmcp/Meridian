"""3f6b8715 — W1-M Experiment Registry MCP handlers.

A SEPARATE concern from :mod:`meridian.mcp.handlers.research_tools`'s
bounded ephemeral scratch/probe runs (``start_research_run`` et al.): this
module is the durable, structured Experiment/Run/Artifact/Event registry
(:mod:`meridian.db.experiments`) -- deliberately its own module rather than
folded into research_tools.py, matching that module's own precedent of
growing the Research/Experiment tool family as separate sibling handler
modules instead of one another file keeps accreting into.

Every handler here catches ``ValueError`` from the validation/persistence
layer and returns ``{"error": ...}`` rather than letting it propagate --
mirrors ``handle_start_research_run``'s exact convention in
research_tools.py.
"""
from __future__ import annotations

from typing import Any


async def handle_create_experiment(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: create_experiment (3f6b8715)."""
    from meridian.db import experiments as exp_db  # noqa: PLC0415

    project_id = args["project_id"]
    try:
        experiment = await exp_db.create_experiment(
            db, project_id, args.get("session_id"),
            name=args.get("name"),
            hypothesis=args.get("hypothesis"),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"experiment": experiment}


async def handle_get_experiment(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_experiment (3f6b8715). Read-only."""
    from meridian.db import experiments as exp_db  # noqa: PLC0415

    project_id = args["project_id"]
    experiment = await exp_db.get_experiment(db, project_id, experiment_id=args.get("experiment_id"))
    if experiment is None:
        return {"error": "experiment not found in this project"}
    return {"experiment": experiment}


async def handle_list_experiments(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: list_experiments (3f6b8715). Read-only."""
    from meridian.db import experiments as exp_db  # noqa: PLC0415

    project_id = args["project_id"]
    try:
        experiments = await exp_db.list_experiments(
            db, project_id, status=args.get("status"), limit=args.get("limit", 100),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"experiments": experiments, "count": len(experiments)}


async def handle_start_experiment_run(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: start_experiment_run (3f6b8715).

    Passing ``pivot_parent_run_id`` auto-writes a 'pivot' experiment_events
    row on the new run -- unconditional, not something the caller triggers
    separately.
    """
    from meridian.db import experiments as exp_db  # noqa: PLC0415

    project_id = args["project_id"]
    try:
        run = await exp_db.start_experiment_run(
            db, project_id, args["session_id"],
            experiment_id=args["experiment_id"],
            trial_label=args.get("trial_label"),
            pivot_parent_run_id=args.get("pivot_parent_run_id"),
            resource_profile=args.get("resource_profile"),
            ttl_seconds=args.get("ttl_seconds"),
            repository_id=args.get("repository_id"),
            worktree_id=args.get("worktree_id"),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"run": run}


async def handle_complete_experiment_run(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: complete_experiment_run (3f6b8715).

    ``outcome_summary``/``disposition`` are explicit and REQUIRED -- never
    inferred. Rejects a missing/empty outcome_summary or a missing
    disposition with {error}, even against an already-terminal run (see
    meridian.db.experiments.complete_experiment_run's docstring for the
    exact validate-then-idempotent-check ordering).
    """
    from meridian.db import experiments as exp_db  # noqa: PLC0415

    project_id = args["project_id"]
    try:
        run = await exp_db.complete_experiment_run(
            db, project_id, args["session_id"],
            run_id=args["run_id"],
            outcome_summary=args.get("outcome_summary"),
            disposition=args.get("disposition"),
            result_receipt=args.get("result_receipt"),
            status=args.get("status", "completed"),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"run": run}


async def handle_promote_experiment_run(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: promote_experiment_run (3f6b8715).

    Requires the run's stored disposition to already be 'promote' (set at
    completion time). Always auto-writes a 'breakthrough' experiment_events
    row.
    """
    from meridian.db import experiments as exp_db  # noqa: PLC0415

    project_id = args["project_id"]
    try:
        result = await exp_db.promote_experiment_run(
            db, project_id, args.get("session_id"), run_id=args["run_id"],
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return result


async def handle_get_experiment_run(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_experiment_run (3f6b8715). Read-only."""
    from meridian.db import experiments as exp_db  # noqa: PLC0415

    project_id = args["project_id"]
    run = await exp_db.get_experiment_run(db, project_id, run_id=args.get("run_id"))
    if run is None:
        return {"error": "experiment run not found in this project"}
    return {"run": run}


async def handle_list_experiment_runs(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: list_experiment_runs (3f6b8715). Read-only."""
    from meridian.db import experiments as exp_db  # noqa: PLC0415

    project_id = args["project_id"]
    try:
        runs = await exp_db.list_experiment_runs(
            db, project_id,
            experiment_id=args.get("experiment_id"),
            status=args.get("status"),
            limit=args.get("limit", 100),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"runs": runs, "count": len(runs)}


async def handle_register_run_artifact(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: register_run_artifact (3f6b8715).

    ``logical_path`` must be project-relative -- an absolute path or a
    secret-shaped value is rejected with {error}.
    """
    from meridian.db import experiments as exp_db  # noqa: PLC0415

    project_id = args["project_id"]
    try:
        artifact = await exp_db.register_run_artifact(
            db, project_id, args.get("session_id"),
            run_id=args["run_id"],
            logical_path=args.get("logical_path"),
            content_hash=args.get("content_hash"),
            artifact_role=args.get("artifact_role"),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"artifact": artifact}


async def handle_record_experiment_event(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: record_experiment_event (3f6b8715).

    Manual/enrichment event-recording path -- coexists freely alongside the
    auto-skeleton writes start_experiment_run/complete_experiment_run/
    promote_experiment_run/expire_stale_runs make unconditionally.
    """
    from meridian.db import experiments as exp_db  # noqa: PLC0415

    project_id = args["project_id"]
    try:
        event = await exp_db.record_experiment_event(
            db, project_id, args.get("session_id"),
            experiment_id=args["experiment_id"],
            run_id=args.get("run_id"),
            event_type=args.get("event_type"),
            label=args.get("label"),
            body=args.get("body"),
            artifact_ids=args.get("artifact_ids"),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"event": event}


async def handle_get_experiment_events(
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """MCP tool: get_experiment_events (3f6b8715). Read-only."""
    from meridian.db import experiments as exp_db  # noqa: PLC0415

    project_id = args["project_id"]
    try:
        events = await exp_db.get_experiment_events(
            db, project_id,
            experiment_id=args["experiment_id"],
            run_id=args.get("run_id"),
            limit=args.get("limit", 200),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"events": events, "count": len(events)}
