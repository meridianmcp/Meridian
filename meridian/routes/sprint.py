"""Sprint items routes — extracted from server.py."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .._deps import _db, _get_tenant_from_request, validate_input_size
from .. import db as db_module

router = APIRouter()


@router.get("/projects/{project_id}/sprint-items")
async def list_sprint_items(
    project_id: str,
    request: Request,
    status: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    with_counts: bool = False,
    page: int | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """List sprint items, optionally filtered by status.

    Pass ``page`` (1-based) for true server-side pagination — returns
    ``{items, total, page, pages}`` using SQL LIMIT/OFFSET so large completed
    lists don't fetch every row. ``limit`` defaults to 50 in this mode. Without
    ``page`` the legacy list/slice behaviour is unchanged.
    """
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if page is not None:
        try:
            per = max(1, min(limit or 50, 500))
            page_n = max(1, page)
            items, total = await db_module.get_sprint_items_page(
                db, project_id, status=status, limit=per, offset=(page_n - 1) * per,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        pages = (total + per - 1) // per if per else 1
        return {"items": items, "total": total, "page": page_n, "pages": pages}
    try:
        items = await db_module.get_sprint_items(
            db, project_id, status=status
        )
        total_done_count = sum(1 for it in items if it.get("status") == "done")
        if status is not None and with_counts:
            all_items = await db_module.get_sprint_items(await _db(request), project_id)
            total_done_count = sum(1 for it in all_items if it.get("status") == "done")
        if limit is not None:
            start = max(0, offset)
            end = start + max(0, min(limit, 500))
            items = items[start:end]
        if with_counts:
            return {"items": items, "total_done_count": total_done_count}
        return items
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/projects/{project_id}/sprint-items", status_code=201)
async def add_sprint_item_endpoint(
    project_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Append a todo sprint item. Body: ``{version, title, group?, human_id?}``."""
    project = await db_module.get_project(await _db(request), project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    version = (body.get("version") or "").strip()
    title = (body.get("title") or "").strip()
    if not version or not title:
        raise HTTPException(status_code=422, detail="version and title are required")
    validate_input_size(title, "sprint item title", 500)
    group = body.get("group") or body.get("item_group") or None
    human_id = body.get("human_id") or None
    depends_on = body.get("depends_on") or None
    failure_mode = body.get("failure_mode") or None
    force = bool(body.get("force", False))
    # G4.15 — safety limit
    from .. import limits as _limits  # noqa: PLC0415
    existing = await db_module.get_sprint_items(await _db(request), project_id)
    _limits.check_sprint_items_per_project(len(existing))
    touches_resources = body.get("touches_resources") or None
    result = await db_module.add_sprint_item(
        await _db(request), project_id, version, title,
        group=group, human_id=human_id,
        depends_on=depends_on, failure_mode=failure_mode,
        force=force, touches_resources=touches_resources,
    )
    # b0d42ef6 — duplicate guard blocked the insert: 409 Conflict with details.
    if isinstance(result, dict) and result.get("error") == "duplicate":
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/projects/{project_id}/sprint-items/{item_id}/complete")
async def complete_sprint_item_endpoint(
    project_id: str, item_id: str, request: Request,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a sprint item ``done``. Optional body: ``{task_id}``."""
    db = await _db(request)
    item = await db_module.complete_sprint_item(
        db, project_id, item_id,
        task_id=(body or {}).get("task_id"),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    # Lazy import to avoid circular dependency on server.py at module level.
    try:
        from meridian.server import _update_roadmap_version_history, _REPO_ROOT  # noqa: PLC0415
        await _update_roadmap_version_history(
            db, project_id, item["version"], _REPO_ROOT
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        active_statuses = {"pending", "todo", "in_progress"}
        remaining = await db_module.get_sprint_items(db, project_id)
        if not any((row.get("status") or "") in active_statuses for row in remaining):
            from meridian.server import _maybe_notify  # noqa: PLC0415

            tenant = await _get_tenant_from_request(request)
            await _maybe_notify(
                db,
                project_id,
                "Sprint done ✓",
                "All sprint items are complete.",
                event="sprint_done",
                tenant=tenant,
                pref_key="sprint",
            )
    except Exception:  # noqa: BLE001
        pass
    return item


@router.post("/projects/{project_id}/sprint-items/{item_id}/skip")
async def skip_sprint_item_endpoint(
    project_id: str, item_id: str, request: Request,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a sprint item ``skipped``. Optional body: ``{reason}``."""
    item = await db_module.skip_sprint_item(
        await _db(request), project_id, item_id,
        reason=(body or {}).get("reason"),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    return item


@router.post("/projects/{project_id}/sprint-items/{item_id}/fail")
async def fail_sprint_item_endpoint(
    project_id: str, item_id: str, request: Request,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a sprint item ``failed``. Optional body: ``{reason}``."""
    item = await db_module.fail_sprint_item(
        await _db(request), project_id, item_id,
        reason=(body or {}).get("reason"),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    return item


@router.delete("/projects/{project_id}/sprint-items/{item_id}", status_code=204)
async def delete_sprint_item_endpoint(
    project_id: str, item_id: str, request: Request
) -> None:
    """Delete a sprint item permanently."""
    db = await _db(request)
    await db.execute(
        "DELETE FROM sprint_items WHERE id = ? AND project_id = ?",
        (item_id, project_id),
    )
    await db.commit()


@router.patch("/projects/{project_id}/sprint-items/{item_id}")
async def patch_sprint_item_endpoint(
    project_id: str, item_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Update editable fields (title, version) of a sprint item."""
    title = body.get("title")
    if title is not None:
        title = title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="title cannot be empty")
        validate_input_size(title, "sprint item title", 500)
    version = body.get("version")
    if version is not None:
        version = version.strip() or None
    status = body.get("status")
    if status is not None and status not in {"pending", "indeterminate"}:
        raise HTTPException(status_code=422, detail="status patch only supports 'pending' or 'indeterminate'")
    feedback_thumb = body.get("feedback_thumb")
    if feedback_thumb is not None:
        try:
            feedback_thumb = int(feedback_thumb)
            if feedback_thumb not in (-1, 1):
                raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="feedback_thumb must be -1 or 1")
    feedback_note = body.get("feedback_note")
    notes = body.get("notes")
    human_id = body.get("human_id")
    item_group = body.get("group", body.get("item_group"))
    touches_resources = body.get("touches_resources", db_module._UNSET)
    item = await db_module.patch_sprint_item(
        await _db(request), project_id, item_id, title=title, version=version,
        status=status,
        feedback_thumb=feedback_thumb, feedback_note=feedback_note,
        notes=notes, human_id=human_id, item_group=item_group,
        touches_resources=touches_resources,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    return item


@router.post("/projects/{project_id}/sprint-items/{item_id}/push")
async def push_sprint_item_endpoint(
    project_id: str, item_id: str, body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Push a sprint item to a future version. Body: ``{to_version}``."""
    to_version = (body.get("to_version") or "").strip()
    if not to_version:
        raise HTTPException(status_code=422, detail="to_version is required")
    try:
        item = await db_module.push_sprint_item(
            await _db(request), project_id, item_id, to_version
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=404, detail="sprint item not found")
    return item


@router.get("/projects/{project_id}/reconcile")
async def reconcile_sprint_items_endpoint(
    project_id: str, request: Request
) -> dict[str, Any]:
    """Cross-reference pending sprint items against recent git commits.

    Fetches commits from GitHub if the project has a repo+PAT connected,
    otherwise falls back to local ``git log --oneline -20``.

    Returns a list of pending items whose title keywords match recent commits,
    with confidence='high' (3+ keyword overlap) or 'medium' (1-2 overlap).
    """
    db = await _db(request)
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    commits: list[dict[str, str]] = []

    # Try GitHub API first
    try:
        tenant = await _get_tenant_from_request(request)
        if tenant:
            pat = db_module.decrypt_field(tenant.get("github_pat"))
            repo = (project.get("github_repo") or "").strip()
            if pat and repo:
                import httpx as _httpx  # noqa: PLC0415
                gh_headers = {
                    "Authorization": f"token {pat}",
                    "Accept": "application/vnd.github+json",
                }
                async with _httpx.AsyncClient(timeout=10.0) as http:
                    r = await http.get(
                        f"https://api.github.com/repos/{repo}/commits",
                        headers=gh_headers,
                        params={"per_page": "20"},
                    )
                    if r.status_code == 200:
                        for c in r.json():
                            commits.append({
                                "sha": c["sha"][:12],
                                "message": c["commit"]["message"].split("\n")[0],
                            })
    except Exception:  # noqa: BLE001
        pass

    # Fall back to local git log if no commits yet
    if not commits:
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-20"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if line and " " in line:
                    sha, _, msg = line.partition(" ")
                    commits.append({"sha": sha, "message": msg})
        except Exception:  # noqa: BLE001
            pass

    pending = await db_module.get_sprint_items(db, project_id, status="pending")

    from .. import handoff as handoff_module  # noqa: PLC0415
    matches = handoff_module.reconcile_sprint_items(pending, commits)

    return {
        "reconciled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit_count": len(commits),
        "pending_count": len(pending),
        "matched_count": len(matches),
        "matches": matches,
    }


@router.get("/projects/{project_id}/resources/sprint-items")
async def sprint_items_for_resource(
    project_id: str, resource: str, request: Request
) -> list[dict[str, Any]]:
    """f5f2a89d — reverse lookup: sprint items whose touches_resources includes resource.

    ``resource`` is a typed id, e.g. ``file:meridian/db/__init__.py``,
    ``note:my-note``, ``decision:abc123``.  Returns all sprint items (any status)
    that declare or infer that resource.
    """
    try:
        db_module.parse_resource_identifier(resource)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return await db_module.get_sprint_items_for_resource(
        await _db(request), project_id, resource
    )
