"""Project CRUD and project-level routes — extracted from server.py."""
from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .._deps import (
    _db,
    _data_dir,
    _get_tenant_from_request,
    _scoped_project_ids_for_request,
    validate_input_size,
)
from .. import db as db_module
from .. import goal_md as goal_md_module
from ..executor_config import normalize_executor_config
from ..models import (
    GoalModeSet,
    GoalSet,
    GoalState,
    Project,
    ProjectCreate,
    ProjectSettings,
    ProjectSettingsPatch,
    Session,
    SetNorthStarRequest,
    SetSprintRequest,
    WorktreeCreate,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers used only by routes in this module
# ---------------------------------------------------------------------------


def _canonicalize_notify_target(raw: str | None) -> str | None:
    """G1.7 — normalize the notify target so ntfy entries are stored as the
    topic path segment only, while emails and webhooks pass through.

    Examples:
      "https://ntfy.sh/foo"   -> "foo"
      "https://ntfy.sh/foo/"  -> "foo"
      "ntfy.sh/foo"           -> "foo"
      "foo"                   -> "foo"
      "you@example.com"       -> "you@example.com"
      "https://hooks.slack.com/services/abc" -> "https://hooks.slack.com/services/abc"
      ""                      -> None
    """
    if not raw:
        return None
    val = str(raw).strip()
    if not val:
        return None
    # Email → pass through.
    if "@" in val and "://" not in val:
        return val
    lower = val.lower()
    for prefix in ("https://ntfy.sh/", "http://ntfy.sh/", "ntfy.sh/"):
        if lower.startswith(prefix):
            topic = val[len(prefix):].strip().strip("/")
            return topic or None
    # Any other URL with a scheme → webhook, pass through.
    if "://" in val:
        return val
    # Bare token, no slashes → treat as ntfy topic.
    return val.strip("/") or None


async def _ensure_unique_ntfy_topic(
    db: Any, project_id: str, topic: str
) -> str:
    """G1.7 — make sure ``topic`` is not already in use by another project in
    this DB. Suffix with -2, -3, … until free. Returns the topic actually
    used. Pure topic strings only; webhooks/emails skip this check upstream.
    """
    projects = await db_module.list_projects(db)
    in_use = {
        str(p.get("ntfy_url") or "").strip().lower()
        for p in projects
        if p.get("id") != project_id and p.get("ntfy_url")
    }
    base = topic
    candidate = base
    n = 2
    while candidate.lower() in in_use:
        candidate = f"{base}-{n}"
        n += 1
        if n > 999:
            break
    return candidate


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------


@router.get("/projects", response_model=list[Project])
async def list_projects(request: Request) -> list[dict[str, Any]]:
    """List projects visible to the caller.

    d116642e — project-scoped workspace members (invited to a single project,
    not the whole workspace) only see their scoped project(s) here. Workspace
    owners and workspace-wide members see everything. This is listing-only
    scoping; see _scoped_project_ids_for_request.
    """
    projects = await db_module.list_projects(await _db(request))
    scoped = await _scoped_project_ids_for_request(request)
    if scoped is not None:
        allowed = set(scoped)
        projects = [p for p in projects if p.get("id") in allowed]
    return projects


@router.post("/projects", response_model=Project, status_code=201)
async def create_project(
    body: ProjectCreate, request: Request
) -> dict[str, Any]:
    """Create a new project. 409 if the name is already in use."""
    existing = await db_module.get_project_by_name(await _db(request), body.name)
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"project '{body.name}' already exists"
        )
    tenant = await _get_tenant_from_request(request)
    if tenant and tenant.get("plan") == "free":
        existing_projects = await db_module.list_projects(await _db(request))
        if len(existing_projects) >= 1:
            raise HTTPException(
                status_code=403,
                detail="Free tier is limited to 1 project. Upgrade to Solo ($20/mo) for unlimited projects.",
            )
    # G4.15 — safety limit: projects per tenant
    from .. import limits as _limits  # noqa: PLC0415
    all_projects = await db_module.list_projects(await _db(request))
    _limits.check_projects_per_tenant(len(all_projects))
    db = await _db(request)
    project = await db_module.create_project(db, body.name, human_id=body.human_id)
    from ..agent_defaults import DEFAULT_AGENT_INSTRUCTIONS  # noqa: PLC0415
    await db_module.set_agent_instructions(db, project["id"], DEFAULT_AGENT_INSTRUCTIONS)
    # c3e91df4 — start the free-tier trial clock on first own project creation,
    # not at signup. Invited members who never create their own project stay
    # at trial_started_at=NULL and never consume a trial slot.
    if tenant and tenant.get("plan") == "free" and not tenant.get("trial_started_at"):
        if len(all_projects) == 0:
            from datetime import datetime, timezone, timedelta  # noqa: PLC0415
            now = datetime.now(timezone.utc)
            await db_module.update_tenant(
                request.app.state.db,
                tenant["id"],
                trial_started_at=now.strftime("%Y-%m-%d %H:%M:%S"),
                inactivity_expires_at=(now + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
            )
    return project


@router.get("/setup/needed")
async def setup_needed(request: Request) -> dict[str, Any]:
    """Returns {needed: true} if no projects exist yet (first-run wizard trigger)."""
    projects = await db_module.list_projects(await _db(request))
    return {"needed": len(projects) == 0}


@router.get("/projects/by-name/{name}")
async def get_project_by_name(name: str, request: Request) -> dict[str, Any]:
    """Look up a project by name (case-insensitive substring match).

    Returns the project row plus a brief goal summary so a cold session
    can confirm it found the right project without a second round-trip.
    """
    db = await _db(request)
    # Exact match first (most common case).
    project = await db_module.get_project_by_name(db, name)
    if project is None:
        # Case-insensitive substring fallback.
        all_projects = await db_module.list_projects(db)
        lower = name.lower()
        matches = [p for p in all_projects if lower in p["name"].lower()]
        if matches:
            project = matches[0]
    if project is None:
        raise HTTPException(
            status_code=404, detail=f"no project found matching '{name}'"
        )
    goal = await db_module.get_goal(db, project["id"])
    return {
        "project": project,
        "goal_version": goal["version"] if goal else None,
        "goal_summary": (str(goal["content"])[:200] if goal else None),
    }


@router.get("/projects/{project_id}", response_model=Project)
async def get_project(project_id: str, request: Request) -> dict[str, Any]:
    """Look up a project by id."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@router.get("/projects/{project_id}/settings", response_model=ProjectSettings)
async def get_project_settings(project_id: str, request: Request) -> dict[str, Any]:
    """Return persisted per-project dashboard settings."""
    settings = await db_module.get_project_settings(await _db(request), project_id)
    if settings is None:
        raise HTTPException(status_code=404, detail="project not found")
    return settings


@router.patch("/projects/{project_id}/settings", response_model=ProjectSettings)
async def patch_project_settings(
    project_id: str, body: ProjectSettingsPatch, request: Request
) -> dict[str, Any]:
    """Update persisted per-project dashboard settings."""
    executor_config_dict = (
        normalize_executor_config(body.executor_config.model_dump(exclude_none=True))
        if body.executor_config is not None
        else None
    )
    settings = await db_module.update_project_settings(
        await _db(request),
        project_id,
        max_pinned_decisions=body.max_pinned_decisions,
        executor_config=executor_config_dict,
        hitl_auto_answer=body.hitl_auto_answer,
        auto_worktrees=body.auto_worktrees,
        require_merge_approval=body.require_merge_approval,
        code_intel_enabled=body.code_intel_enabled,
        execution_mode=body.execution_mode,
    )
    if settings is None:
        raise HTTPException(status_code=404, detail="project not found")
    return settings


# ---------------------------------------------------------------------------
# Notification / ntfy routes
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/ntfy")
async def get_project_ntfy(
    project_id: str, request: Request
) -> dict[str, Any]:
    """Return the current notification settings for this project."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    db = await _db(request)
    notify_url = await db_module.get_project_ntfy_url(db, project_id)
    notify_email = await db_module.get_project_notify_email(db, project_id)
    return {
        "ntfy_url": notify_url or "",
        "notify_url": notify_url or "",
        "notify_email": notify_email or "",
    }


@router.patch("/projects/{project_id}/ntfy")
async def set_project_ntfy(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Save (or clear) the notify URL and/or notify_email for this project.

    Accepts ``notify_url`` (preferred) or ``ntfy_url`` (legacy) key for the
    ntfy/webhook channel, and ``notify_email`` for the email channel.
    ntfy entries are canonicalized to the topic path segment only and
    suffixed with -2/-3/… if another project in this DB already uses
    the same topic. Emails and non-ntfy webhooks pass through verbatim.

    After saving a non-empty notify_url, fires a welcome notification so
    ntfy.sh topics are created on first publish (avoids 404 on first
    real alert).
    """
    # Lazy import to avoid circular dependency on server.py at module level.
    from meridian.server import _dispatch_notification  # noqa: PLC0415

    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    db = await _db(request)
    # Handle ntfy_url / webhook channel
    if "notify_url" in body or "ntfy_url" in body:
        raw_value = str(body.get("notify_url") or body.get("ntfy_url") or "").strip() or None
        notify_url = _canonicalize_notify_target(raw_value)
        if notify_url and "://" not in notify_url and "@" not in notify_url:
            # bare topic → enforce per-DB uniqueness
            notify_url = await _ensure_unique_ntfy_topic(db, project_id, notify_url)
        await db_module.set_project_ntfy_url(db, project_id, notify_url)
        if notify_url:
            # Fire a welcome notification immediately so ntfy.sh creates the topic
            try:
                ntfy_full = notify_url
                if "://" not in ntfy_full and "@" not in ntfy_full:
                    ntfy_full = f"https://ntfy.sh/{ntfy_full}"
                await _dispatch_notification(
                    ntfy_full,
                    "Notifications active",
                    "You will receive alerts here for HITL requests and sprint completions.",
                    event="setup",
                )
            except Exception:  # noqa: BLE001
                pass
    else:
        notify_url = await db_module.get_project_ntfy_url(db, project_id)
    # Handle notify_email channel
    if "notify_email" in body:
        raw_email = str(body.get("notify_email") or "").strip() or None
        await db_module.set_project_notify_email(db, project_id, raw_email)
        notify_email = raw_email
    else:
        notify_email = await db_module.get_project_notify_email(db, project_id)
    return {
        "ntfy_url": notify_url or "",
        "notify_url": notify_url or "",
        "notify_email": notify_email or "",
    }


@router.post("/projects/{project_id}/notify/test")
async def test_project_notification(
    project_id: str, request: Request
) -> dict[str, Any]:
    """Send a test notification to verify the configured notify URL and/or email."""
    # Lazy import to avoid circular dependency on server.py at module level.
    from meridian.server import _dispatch_notification, _send_email_notification  # noqa: PLC0415

    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    db = await _db(request)
    notify_url = await db_module.get_project_ntfy_url(db, project_id)
    notify_email = await db_module.get_project_notify_email(db, project_id)
    if not notify_url and not notify_email:
        raise HTTPException(status_code=400, detail="No notify URL or email configured for this project")
    sent_to = []
    if notify_url:
        ntfy_full = notify_url
        if "://" not in ntfy_full and "@" not in ntfy_full:
            ntfy_full = f"https://ntfy.sh/{ntfy_full}"
        await _dispatch_notification(
            ntfy_full,
            "Meridian test notification",
            "Test from the Meridian dashboard. If you see this, notifications are working!",
            event="test",
        )
        sent_to.append(notify_url)
    if notify_email:
        await _send_email_notification(
            notify_email,
            "[Meridian] Test notification",
            "Test from the Meridian dashboard. If you see this, email notifications are working!",
        )
        sent_to.append(notify_email)
    return {"ok": True, "sent_to": sent_to}


# ---------------------------------------------------------------------------
# Project management routes
# ---------------------------------------------------------------------------


@router.patch("/projects/{project_id}/icon")
async def set_project_icon(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """G4.17 — set or clear the single-emoji icon for a project.

    Body: ``{"icon": "🎯"}`` or ``{"icon": null}``. Stored as the user-provided
    string capped to a short length (typical emoji is 1-4 codepoints); the
    frontend never expects more than ~8 chars. Wider validation lives in
    the UI picker.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    raw = body.get("icon")
    icon: str | None
    if raw is None:
        icon = None
    else:
        icon = str(raw).strip()[:8] or None
    db = await _db(request)
    await db.execute(
        "UPDATE projects SET icon = ? WHERE id = ?",
        (icon, project_id),
    )
    await db.commit()
    db_module.publish_global(
        {"type": "project_icon_changed", "project_id": project_id, "icon": icon}
    )
    return await db_module.get_project(db, project_id)


@router.get("/projects/{project_id}/agent-instructions")
async def get_agent_instructions(project_id: str, request: Request) -> dict[str, Any]:
    """Return the current agent_instructions for a project."""
    instructions = await db_module.get_agent_instructions(await _db(request), project_id)
    return {"project_id": project_id, "agent_instructions": instructions}


@router.patch("/projects/{project_id}/agent-instructions")
async def patch_agent_instructions(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Set or clear agent_instructions. Pass null to reset to server defaults."""
    from ..agent_defaults import DEFAULT_AGENT_INSTRUCTIONS  # noqa: PLC0415
    instructions = body.get("agent_instructions")
    if instructions is None:
        instructions = DEFAULT_AGENT_INSTRUCTIONS
    elif instructions == "":
        instructions = None
    result = await db_module.set_agent_instructions(
        await _db(request), project_id, instructions
    )
    return result


@router.get("/projects/{project_id}/agent-instructions/default")
async def get_default_agent_instructions(project_id: str, request: Request) -> dict[str, Any]:
    """Return the server DEFAULT_AGENT_INSTRUCTIONS (for the Reset button)."""
    from ..agent_defaults import DEFAULT_AGENT_INSTRUCTIONS  # noqa: PLC0415
    return {"default_agent_instructions": DEFAULT_AGENT_INSTRUCTIONS}


@router.post("/projects/{project_id}/rename")
async def rename_project(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """v1.9.x — rename a project.  Broadcasts project_renamed WS event."""
    new_name = str(body.get("name") or "").strip()
    if not new_name:
        raise HTTPException(400, "name is required")
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    existing = await db_module.get_project_by_name(await _db(request), new_name)
    if existing and existing["id"] != project_id:
        raise HTTPException(409, f"project '{new_name}' already exists")
    updated = await db_module.rename_project(await _db(request), project_id, new_name)
    db_module.publish_global(
        {"type": "project_renamed", "project_id": project_id, "name": new_name}
    )
    return updated


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: str, request: Request) -> None:
    """v1.9.x — delete a project and all data.

    Returns 409 if any tasks are in_progress, 404 if the project is unknown.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        await db_module.delete_project(await _db(request), project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


# ---------------------------------------------------------------------------
# Goal routes
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/goal", response_model=GoalState)
async def get_goal(project_id: str, request: Request) -> dict[str, Any]:
    """Read the latest goal state plus ambient task context.

    The response payload (v0.4.2+) includes ``ambient_tasks`` — the
    five most recent task rows, newest first, as ``{status, description,
    created_at}`` dicts. Cold sessions can render the directive *and*
    last activity from a single MCP call.

    G8.34/G9 — Returns 200 with an empty stub when the project exists
    but no goal has been set yet (previously 404). The 404-as-empty
    semantics produced a console error on the dashboard's initial
    render for every fresh project, which made the panel-render
    Playwright test flake by environment. Browsers can't tell the
    difference between "field is empty" and "fetch threw 4xx", so
    the only honest answer is 200 with empty fields. Returns 404 still
    when the project itself does not exist.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    goal = await db_module.get_goal(await _db(request), project_id)
    if goal is None:
        recent = await db_module.get_tasks(await _db(request), project_id, limit=5)
        return {
            "id": "",
            "project_id": project_id,
            "content": "",
            "version": 0,
            "created_at": "",
            "updated_at": "",
            "ambient_tasks": [
                {
                    "status": t["status"],
                    "description": t["description"],
                    "created_at": t["created_at"],
                }
                for t in recent
            ],
            "north_star": None,
            "sprint": None,
        }
    recent = await db_module.get_tasks(await _db(request), project_id, limit=5)
    goal["ambient_tasks"] = [
        {
            "status": t["status"],
            "description": t["description"],
            "created_at": t["created_at"],
        }
        for t in recent
    ]
    # v1.1.3 — per-field ages + coherence warning so the dashboard
    # can paint green / amber / red dots and so cold sessions see
    # which fields have gone stale before doing anything.
    field_ages = await db_module.get_goal_field_ages(
        await _db(request), project_id
    )
    coherence = db_module.compute_coherence_warning(field_ages)
    goal["field_ages"] = field_ages
    goal["coherence_warning"] = coherence
    # v1.1.4 — append-only decisions log.
    decisions = await db_module.get_decisions(await _db(request), project_id)
    goal["decisions"] = decisions
    # v0.6.1 — also serve the XML envelope so MCP / cache-aware consumers
    # don't have to re-stitch fields locally. The JSON keys stay for the
    # dashboard and the test suite.
    goal["xml"] = db_module.build_goal_xml(
        goal, project["name"], goal["ambient_tasks"], coherence,
        decisions=decisions,
    )
    # v0.6.2 — pre-built Anthropic content blocks with cache_control
    # markers on the static fields. Callers can pass these straight
    # into messages.create() to get prompt caching for free.
    goal["cache_blocks"] = db_module.build_goal_cache_blocks(
        goal, project["name"], goal["ambient_tasks"]
    )
    return goal


@router.patch("/projects/{project_id}/goal-mode")
async def patch_goal_mode(
    project_id: str, body: GoalModeSet, request: Request
) -> dict[str, str]:
    """Switch a project between 'manual' and 'auto' goal modes."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        await db_module.set_goal_mode(await _db(request), project_id, body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"project_id": project_id, "goal_mode": body.mode}


@router.get("/projects/{project_id}/goal-mode")
async def get_goal_mode(project_id: str, request: Request) -> dict[str, str]:
    """Return the current goal mode for a project."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    mode = await db_module.get_goal_mode(await _db(request), project_id)
    return {"project_id": project_id, "goal_mode": mode}


@router.post("/projects/{project_id}/goal", response_model=GoalState)
async def set_goal(
    project_id: str, body: GoalSet, request: Request
) -> dict[str, Any]:
    """Upsert the goal state, incrementing version.

    Goal-ownership rule (v0.3.2): if the project has a recorded
    ``creator_human_id`` *and* the request body supplies a ``human_id``
    that doesn't match, refuse with 403. Sessions without a human_id
    (legacy callers, MCP workers that don't claim an identity) keep
    their old write privilege.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    owner = await db_module.get_project_owner(await _db(request), project_id)
    if owner is not None and body.human_id is not None and body.human_id != owner:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "goal_locked",
                "message": (
                    "Only the project owner can set the goal. "
                    "Use the HITL queue to propose changes."
                ),
            },
        )
    _goal_str = body.content if isinstance(body.content, str) else json.dumps(body.content)
    validate_input_size(_goal_str, "goal", 10_000)
    if body.north_star is not None:
        validate_input_size(body.north_star, "north_star", 10_000)
    if body.sprint is not None:
        validate_input_size(body.sprint, "version_goal", 10_000)
    result = await db_module.set_goal(
        await _db(request), project_id, body.content,
        north_star=body.north_star, sprint=body.sprint,
        minor=body.minor,
    )
    await goal_md_module.sync_db_to_goal_md(await _db(request), project_id)
    return result


@router.post("/projects/{project_id}/goal/north-star", response_model=GoalState)
async def set_north_star(
    project_id: str, body: SetNorthStarRequest, request: Request
) -> dict[str, Any]:
    """v0.5.2 — update only the north star field.

    Owner-only: requires ``human_id`` matching the project creator.
    Returns the new goal version.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    validate_input_size(body.north_star, "north_star", 10_000)
    # Ownership check skipped in hosted mode — session cookie already proves
    # the caller owns this project. human_id check only applies to local no-auth.
    try:
        result = await db_module.set_north_star(
            await _db(request), project_id, body.north_star
        )
        await goal_md_module.sync_db_to_goal_md(await _db(request), project_id)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/projects/{project_id}/goal/sprint", response_model=GoalState)
async def set_sprint(
    project_id: str, body: SetSprintRequest, request: Request
) -> dict[str, Any]:
    """v0.5.2 — update only the sprint field.

    Any team member can update the sprint — no ownership check.
    Returns the new goal version.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    validate_input_size(body.sprint, "version_goal", 10_000)
    try:
        result = await db_module.set_sprint(
            await _db(request), project_id, body.sprint
        )
        await goal_md_module.sync_db_to_goal_md(await _db(request), project_id)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ---------------------------------------------------------------------------
# Miscellaneous project-level routes
# ---------------------------------------------------------------------------


@router.post("/projects/{project_id}/start-worker-session")
async def start_worker_session_endpoint(
    project_id: str, request: Request,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """v1.2.0 — REST mirror of the MCP ``start_worker_session`` tool.

    Optional body: ``{task_id}``. Returns
    ``{session_id, task, worker_context}`` or 404 when there's no
    claimable task / the named task doesn't belong to this project.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        return await db_module.start_worker_session(
            await _db(request),
            project_id,
            task_id=(body or {}).get("task_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/projects/{project_id}/decisions")
async def post_decision_endpoint(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """v1.1.4 — append a decision entry to the project's append-only
    decisions log. Body: ``{text}``."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    validate_input_size(text, "decision text", 100_000)
    updated = await db_module.set_decision(await _db(request), project_id, text)
    return {"project_id": project_id, "decisions": updated}


@router.get("/projects/{project_id}/timeline")
async def get_timeline_endpoint(
    project_id: str, request: Request
) -> dict[str, Any]:
    """v1.1.1 — return the data needed to render the Activity Timeline.

    v1.6.x — filtered to only meaningful history: completed/failed tasks
    plus goal-change events. Session idle/active events were noise and
    have been dropped (the LIVE vtab covers active sessions instead).
    Pending/in_progress tasks belong on the LIVE tab, not in history.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    timeline = await db_module.get_timeline(await _db(request), project_id)
    return {
        "tasks": [
            t for t in timeline.get("tasks", [])
            if t.get("status") in ("done", "failed")
        ],
        "sessions": [],
        "goal_events": timeline.get("goal_events", []),
        "daily_counts": timeline.get("daily_counts", []),
        "people": timeline.get("people", []),
        "clients": timeline.get("clients", []),
    }


@router.get("/projects/{project_id}/rewind")
async def get_rewind(
    project_id: str,
    request: Request,
    days: int = 7,
    token: str | None = None,
) -> dict[str, Any]:
    """v1.3.0 — "Last X days" project rewind summary.

    Returns versions shipped, goal changes, decisions logged, session
    summaries, sprint items completed, and task counts for the period.
    When a ``token`` query param is supplied, it must match the project's
    stored ``rewind_token`` — letting an external link validate ownership
    without any other auth (Meridian is local-first; no token = no auth
    required, same as every other endpoint).
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if token is not None:
        stored = await db_module.get_rewind_token(await _db(request), project_id)
        if not stored or token != stored:
            raise HTTPException(status_code=403, detail="invalid rewind token")
    if days <= 0:
        raise HTTPException(status_code=422, detail="days must be positive")
    return await db_module.get_rewind_data(await _db(request), project_id, days)


@router.post("/projects/{project_id}/rewind-token")
async def post_rewind_token(
    project_id: str, request: Request
) -> dict[str, str]:
    """v1.3.0 — mint (or return) the project's shareable rewind token.

    The token is stored on the projects row so subsequent calls return
    the same value; teams can publish a link once without it rotating.
    Response: ``{"token": "<uuid4>", "expires": "never"}``.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    token = await db_module.get_or_create_rewind_token(await _db(request), project_id)
    return {"token": token, "expires": "never"}


@router.get("/projects/{project_id}/goal-history")
async def get_goal_history(
    project_id: str, request: Request
) -> list[dict[str, Any]]:
    """Return meaningful goal versions for a project, newest first.

    AUTO BLOCKS-only versions are collapsed out so the history shows
    only real content changes. Each entry: version, north_star,
    version_goal, sprint, created_at. Used by the Rewind goal subtab.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.get_goal_history(await _db(request), project_id)


@router.get("/projects/{project_id}/stats")
async def get_project_stats(
    project_id: str, request: Request, days: int = 30
) -> dict[str, Any]:
    """Return activity stats for the Charts subtab.

    Returns tasks/day series and sprint completion % per version.
    ``days`` defaults to 30, max 365.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    days = max(1, min(days, 365))
    return await db_module.get_project_stats(await _db(request), project_id, days)


@router.get("/projects/{project_id}/sessions", response_model=list[Session])
async def get_sessions(
    project_id: str, request: Request, active_only: bool = True
) -> list[dict[str, Any]]:
    """List sessions attached to the project.

    Pass ``?active_only=false`` to include closed and archived sessions
    (useful for the LIVE tab showing recent session outcomes).
    Expires stale sessions (last_seen > 30 min ago) before returning so
    the dashboard doesn't accumulate ghost entries indefinitely.
    """
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    # Lazy import to avoid circular dependency on server.py at module level.
    from meridian.server import _expire_and_generate_handoffs  # noqa: PLC0415
    await _expire_and_generate_handoffs(await _db(request), _data_dir(request))
    return await db_module.get_sessions(
        await _db(request), project_id, active_only=active_only
    )


# ---------------------------------------------------------------------------
# Worktree routes
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/worktrees")
async def list_worktrees(project_id: str, request: Request) -> list[dict[str, Any]]:
    """List active git worktrees registered for a project."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    # Degrade gracefully: a missing/not-yet-migrated active_worktrees table must
    # not 500 the dashboard panel — return [] and log instead.
    try:
        return await db_module.list_active_worktrees(await _db(request), project_id)
    except Exception as exc:  # noqa: BLE001
        import logging as _l
        _l.getLogger("meridian.server").warning(
            "list_worktrees failed for project %s: %s", project_id, exc
        )
        return []


@router.post("/projects/{project_id}/worktrees", status_code=201)
async def create_worktree(
    project_id: str, request: Request, body: WorktreeCreate
) -> dict[str, Any]:
    """Register a git worktree for a session. Call after `git worktree add`."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await db_module.register_worktree(
        await _db(request),
        body.session_id,
        project_id,
        body.branch,
        body.path,
        item_id=body.item_id,
    )


@router.delete("/projects/{project_id}/worktrees/{worktree_id}", status_code=204)
async def delete_worktree(
    project_id: str, worktree_id: str, request: Request
) -> None:
    """Mark a registered worktree as removed. Call after `git worktree remove`."""
    removed = await db_module.remove_worktree(await _db(request), worktree_id)
    if not removed:
        raise HTTPException(status_code=404, detail="worktree not found or already removed")


# ---------------------------------------------------------------------------
# PDF export
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/export/pdf")
async def export_project_pdf(project_id: str, request: Request):
    """Generate a tamper-evident IP attribution PDF for the project.

    Contains north star, version goal, sprint, full task log with
    timestamps and session names, and a SHA-256 hash of the content
    embedded in the footer.
    """
    import hashlib
    from fpdf import FPDF
    import io

    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    goal = await db_module.get_goal(db, project_id)
    tasks = await db_module.get_tasks(db, project_id, limit=200)
    sessions = await db_module.get_sessions(db, project_id, active_only=False)
    session_names = {s["id"]: s["name"] for s in sessions}

    # Build text content for hashing
    lines = [
        f"MERIDIAN IP ATTRIBUTION RECORD",
        f"Project: {project['name']} ({project['id']})",
        f"Generated: {__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}",
        "",
    ]
    if goal:
        lines += [
            f"Goal Version: {goal['version']}",
            f"North Star: {goal.get('north_star') or '(not set)'}",
            f"Version Goal: {goal['content']}",
            f"Sprint: {goal.get('sprint') or '(not set)'}",
            "",
        ]
    lines.append("TASK LOG:")
    for t in tasks:
        sname = session_names.get(t["session_id"], t["session_id"][:8])
        lines.append(f"[{t['created_at']}] [{t['status'].upper()}] {sname}: {t['description']}")

    full_text = "\n".join(lines)
    sha256 = hashlib.sha256(full_text.encode()).hexdigest()

    # Build PDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Meridian IP Attribution Record", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, f"Project: {project['name']}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    if goal:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, "Goal Hierarchy", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for label, val in [
            ("North Star", goal.get("north_star") or "(not set)"),
            ("Version Goal", str(goal["content"])),
            ("Sprint", goal.get("sprint") or "(not set)"),
        ]:
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(30, 6, f"{label}:", new_x="RIGHT", new_y="LAST")
            pdf.set_font("Helvetica", "", 9)
            # Multi-line safe: use multi_cell for value
            x, y = pdf.get_x(), pdf.get_y()
            pdf.multi_cell(0, 6, val[:300])
        pdf.ln(4)

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, f"Task Log ({len(tasks)} entries)", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Courier", "", 7)
    for t in tasks:
        sname = session_names.get(t["session_id"], t["session_id"][:8])
        row = f"[{t['created_at']}] [{t['status'].upper()}] {sname}: {t['description']}"
        pdf.multi_cell(0, 5, row[:200])

    # Footer with SHA256
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 5, f"SHA-256: {sha256}")

    pdf_bytes = pdf.output()
    return Response(
        content=bytes(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{project["name"]}_ip_record.pdf"'
        },
    )


# ---------------------------------------------------------------------------
# Team summary
# ---------------------------------------------------------------------------


@router.get("/team/summary")
async def get_team_summary_endpoint(
    request: Request, project_id: str | None = None, days: int = 1
) -> dict[str, Any]:
    """Aggregate task_log + sessions by human_id over the last N days.

    ``project_id`` optional — omit to roll up across all projects.
    Returns ``{period_days, humans:[...], active_count}``. Used by the
    Team tab cards, swimlane timeline, and standup digest.
    """
    return await db_module.get_team_summary(await _db(request), project_id, days)


# ---------------------------------------------------------------------------
# Webhook / event intake
# ---------------------------------------------------------------------------


@router.post("/projects/{project_id}/events", status_code=201)
async def post_project_event(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Normalize a framework event into Meridian's task_log.

    Auth: ``X-Meridian-Token: <project_token>`` header.  The token grants
    write-access to a single project's task_log only — call
    ``ensure_project_token`` to mint one (returned by the dashboard's
    Project settings panel, by GET /projects/{id}/webhook-token).

    Body schema (all optional except description+session_name):
    ```
    {
      "type": "task_completed" | "checkpoint" | "hitl_request" | "session_start",
      "session_name": "langgraph-researcher",
      "human_id": "langgraph",
      "agent_framework": "langgraph",
      "description": "researcher agent fetched 3 sources",
      "status": "done",
      "parent_task_id": null,
      "metadata": {}
    }
    ```
    """
    db = await _db(request)
    auth_token = request.headers.get("X-Meridian-Token", "")
    project_by_token = await db_module.get_project_by_token(db, auth_token) if auth_token else None
    if project_by_token is None or project_by_token["id"] != project_id:
        raise HTTPException(status_code=401, detail="invalid or missing X-Meridian-Token")

    event_type = body.get("type") or "task_completed"
    description = (body.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description required")
    session_name = body.get("session_name") or f"webhook/{body.get('agent_framework', 'custom')}"
    human_id = body.get("human_id")
    framework = body.get("agent_framework") or "custom"
    status = body.get("status") or "done"

    # Find-or-create a session for this framework/human/name combo so
    # bursty webhook traffic doesn't create one session per event.
    sessions = await db_module.get_sessions(db, project_id, active_only=False)
    target = next(
        (s for s in sessions if s.get("name") == session_name and s.get("agent_framework") == framework),
        None,
    )
    if target is None:
        target = await db_module.register_session(
            db, project_id, session_name,
            human_id=human_id, agent_framework=framework,
        )

    if event_type == "hitl_request":
        return await db_module.request_hitl(
            db, project_id, description,
            session_id=target["id"], context=body.get("context"),
            urgency=body.get("urgency", "normal"),
            assigned_to=body.get("assigned_to"),
        )

    task = await db_module.log_task(
        db, target["id"], project_id, description,
        status=status, parent_task_id=body.get("parent_task_id"),
    )
    return {"task": task, "session_id": target["id"], "event_type": event_type}


@router.get("/projects/{project_id}/webhook-token")
async def get_project_webhook_token(
    project_id: str, request: Request
) -> dict[str, Any]:
    """Mint-and-return the project webhook token. Shown ONCE in the UI."""
    token = await db_module.ensure_project_token(await _db(request), project_id)
    if token is None:
        raise HTTPException(status_code=404, detail="project not found")
    return {"project_id": project_id, "token": token}


# ---------------------------------------------------------------------------
# Search + executor runs
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/search")
async def search_project_all(
    project_id: str,
    request: Request,
    q: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """Universal search across tasks, notes, decisions, and sprint items."""
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if not q.strip():
        return {"query": q, "tasks": [], "notes": [], "decisions": [], "sprint_items": [], "total": 0}
    return await db_module.search_all(db, project_id, q.strip(), limit=limit)


@router.get("/projects/{project_id}/runs")
async def get_project_runs(
    project_id: str,
    request: Request,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List executor_runs for a project, newest first."""
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    runs = await db_module.get_executor_runs(db, project_id, limit=limit)
    for run in runs:
        if run.get("started_at") and run.get("ended_at"):
            from datetime import datetime
            try:
                fmt = "%Y-%m-%d %H:%M:%S"
                start = datetime.strptime(run["started_at"], fmt)
                end = datetime.strptime(run["ended_at"], fmt)
                run["duration_s"] = int((end - start).total_seconds())
            except Exception:
                run["duration_s"] = None
        else:
            run["duration_s"] = None
    return runs


@router.get("/projects/{project_id}/runs/{run_id}")
async def get_project_run(
    project_id: str,
    run_id: str,
    request: Request,
) -> dict[str, Any]:
    """Return a single executor_run with full transcript."""
    db = await _db(request)
    run = await db_module.get_executor_run(db, run_id)
    if run is None or run.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="run not found")
    return run
