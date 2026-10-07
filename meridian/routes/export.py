"""GDPR / account routes — data export + self-service account deletion.

Extracted from server.py. Hosted-tier only (both 404 in self-host mode).
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .. import _deps
from .._deps import _db, _hosted_mode, _is_demo_request, _rate_limit
from .. import db as db_module

router = APIRouter()


@router.get("/export/my-data")
@_rate_limit("3/minute")
async def export_my_data(request: Request) -> Response:
    """GDPR data portability — returns a JSON file of all account data."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    if _is_demo_request(request):
        return JSONResponse(
            {"detail": "Not available in demo mode. Sign up at usemeridian.us"},
            status_code=403,
        )
    from ..hosted import get_current_tenant
    tenant = await get_current_tenant(request)
    # Account rows (tenant/tokens/members) live in the auth DB; the tenant's
    # project data lives in its own per-tenant DB. Pass both so the export
    # actually contains projects (hosted mode previously exported empty arrays).
    #
    # RT-TI-005 (pass 3, F-D2) -- ``_db`` honours X-Workspace-Tenant-Id, so a PROJECT-SCOPED
    # member of someone else's workspace used to download every project of that workspace
    # (goals, decisions, sessions, tasks, notes). A scoped caller now gets only the projects
    # in their scope (and no workspace-global notes/decisions); owners, workspace-wide
    # members, self-hosted and demo callers get ``None`` here and the export is unchanged.
    # A failure resolving the scope propagates (500) rather than exporting everything.
    scoped = await _deps._scoped_project_ids_for_request(request)
    data = await db_module.export_tenant_data(
        request.app.state.db, tenant["id"], project_db=await _db(request),
        project_ids=scoped,
    )
    payload = json.dumps(data, indent=2, default=str).encode()
    email_slug = (tenant.get("email") or "user").split("@")[0][:20]
    filename = f"meridian-export-{email_slug}.json"
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/account/delete")
async def delete_account(request: Request) -> Response:
    """Self-service account deletion. Requires JSON body: {\"confirmation\": \"DELETE\"}."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    if _is_demo_request(request):
        return JSONResponse(
            {"detail": "Not available in demo mode. Sign up at usemeridian.us"},
            status_code=403,
        )
    from ..hosted import get_current_tenant, cancel_stripe_subscription, _drop_tenant_neon_database, send_account_deleted_email
    from ..roles import PERM_DELETE_TENANT  # noqa: PLC0415
    from meridian.server import _require_workspace_perm  # noqa: PLC0415
    tenant = await get_current_tenant(request)
    # G5.19 — only the tenant owner can delete the account. Admin
    # invitees are explicitly excluded by ROLE_PERMS.
    await _require_workspace_perm(request, tenant, PERM_DELETE_TENANT)
    body = await request.json()
    if body.get("confirmation") != "DELETE":
        raise HTTPException(status_code=400, detail="Type DELETE to confirm account deletion.")

    stripe_id = tenant.get("stripe_customer_id")
    if stripe_id:
        await cancel_stripe_subscription(stripe_id)

    if tenant.get("neon_project_id"):
        asyncio.create_task(_drop_tenant_neon_database(tenant))

    email = tenant.get("email", "")
    await db_module.delete_tenant_records(request.app.state.db, tenant["id"])

    if email:
        asyncio.create_task(send_account_deleted_email(email))

    resp = JSONResponse({"deleted": True})
    resp.delete_cookie("meridian_session")
    return resp
