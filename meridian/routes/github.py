"""GitHub integration routes (hosted-tier only) — extracted from server.py.

Repo connect/status/disconnect, repo + branch listing (with a 24h in-memory
per-tenant cache), the repo-image proxy, and the MCP-template push. All routes
404 in self-host mode.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .._deps import _db, _hosted_mode, _get_tenant_from_request
from .. import db as db_module

router = APIRouter()


@router.post("/projects/{project_id}/github/connect")
async def github_connect(project_id: str, request: Request) -> dict[str, Any]:
    """Connect or update the tenant's GitHub repo settings."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    import httpx as _httpx
    from ..hosted import _github_user_snapshot as _github_snapshot
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    body = await request.json()
    pat = (body.get("pat") or body.get("token") or body.get("access_token") or "").strip()
    repo = (body.get("repo") or "").strip()
    branch = (body.get("branch") or "main").strip()
    if not repo or "/" not in repo:
        raise HTTPException(status_code=422, detail="repo must be owner/repo format")
    tenant_db = request.app.state.db
    fresh = await db_module.get_tenant_by_id(tenant_db, tenant["id"])
    # Resolve: new PAT > first github_connection token > legacy tenant.github_pat
    db = await _db(request)
    conn_token, _conn_login = await db_module.get_github_token_for_project(
        db, tenant["id"], project_id
    )
    stored_pat = db_module.decrypt_field((fresh or {}).get("github_pat"))
    validate_pat = pat or conn_token or stored_pat or ""
    github_user = ""
    avatar_url = ""
    repos: list[dict[str, Any]] = []
    try:
        if validate_pat:
            snapshot = await _github_snapshot(validate_pat)
            github_user = snapshot.get("login", "")
            avatar_url = snapshot.get("avatar_url", "")
            repos = snapshot.get("repos") or []
            if repo and repos:
                repo_lookup = {r.get("full_name", ""): r for r in repos if r.get("full_name")}
                if repo not in repo_lookup:
                    repo = repos[0].get("full_name", repo)
                    branch = repos[0].get("default_branch") or branch
                elif branch == "main" and repo_lookup[repo].get("default_branch"):
                    branch = repo_lookup[repo].get("default_branch") or branch
        elif not stored_pat:
            raise HTTPException(status_code=422, detail="GitHub is not connected")
    except _httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach GitHub: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if pat and github_user:
        # Save to multi-account connections table and legacy field for back-compat.
        await db_module.add_github_connection(db, tenant["id"], github_user, pat)
        await db_module.update_tenant(
            tenant_db, tenant["id"],
            github_pat=db_module.encrypt_field(pat),
        )
    await db_module.update_project_settings(
        db, project_id,
        github_repo=repo,
        github_branch=branch,
        github_account_login=github_user or None,
    )
    return {
        "connected": True,
        "repo": repo,
        "branch": branch,
        "github_user": github_user,
        "avatar_url": avatar_url,
        "repos": repos,
    }


@router.get("/projects/{project_id}/github/status")
async def github_status(project_id: str, request: Request) -> dict[str, Any]:
    """Return the project's current GitHub connection status."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from ..hosted import _github_user_snapshot as _github_snapshot
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        fresh = await db_module.get_tenant_by_id(request.app.state.db, tenant["id"])
        if not fresh:
            return {"connected": False, "pat_linked": False, "repo": "", "branch": "main",
                    "github_user": "", "avatar_url": "", "repos": [], "last_verified": None}
        db = await _db(request)
        # Multi-account: resolve token for this project.
        conn_token, conn_login = await db_module.get_github_token_for_project(
            db, tenant["id"], project_id
        )
        legacy_token = db_module.decrypt_field(fresh.get("github_pat"))
        token = conn_token or legacy_token
        project = await db_module.get_project(db, project_id)
        selected_repo = (project or {}).get("github_repo") or ""
        selected_branch = (project or {}).get("github_branch") or "main"
        snapshot: dict[str, Any] | None = None
        if token:
            try:
                snapshot = await _github_snapshot(token)
            except Exception:
                snapshot = None
        repos = (snapshot or {}).get("repos") or []
        if selected_repo and repos and not any(r.get("full_name") == selected_repo for r in repos):
            repos = [{"full_name": selected_repo, "name": selected_repo.split("/")[-1], "owner": selected_repo.split("/")[0] if "/" in selected_repo else "", "html_url": "", "default_branch": selected_branch, "private": False, "updated_at": ""}] + repos
        connections = await db_module.get_github_connections(db, tenant["id"])
        return {
            "connected": bool(token and selected_repo),
            "pat_linked": bool(token),
            "repo": selected_repo,
            "branch": selected_branch,
            "github_user": conn_login or (snapshot or {}).get("login", ""),
            "avatar_url": (snapshot or {}).get("avatar_url", ""),
            "repos": repos,
            "connections": connections,
            "last_verified": None,
        }
    except Exception:
        return {"connected": False, "pat_linked": False, "repo": "", "branch": "main",
                "github_user": "", "avatar_url": "", "repos": [], "connections": [],
                "last_verified": None}


@router.get("/projects/{project_id}/repo-image")
async def repo_image_proxy(project_id: str, request: Request, path: str = ""):
    """G7.32 — proxy a repo-relative image through the project's GitHub PAT.

    Used by markdown preview to render images that live in the connected
    repo (e.g. ``![](docs/screenshots/foo.png)``) without exposing the PAT
    to the browser. Returns the raw bytes with the upstream Content-Type.

    Limits:
     - Hosted-only (PAT lives on the tenant).
     - Path is normalized to disallow ``..``; absolute URLs are rejected.
     - Falls back to 404 when the project isn't connected to a repo.
     - 1 MB response cap (oversized images are 413).
    """
    if not _hosted_mode():
        raise HTTPException(404)
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(401, "not authenticated")
    db = await _db(request)
    conn_token, _conn_login = await db_module.get_github_token_for_project(
        db, tenant["id"], project_id
    )
    fresh = await db_module.get_tenant_by_id(request.app.state.db, tenant["id"])
    pat = conn_token or (db_module.decrypt_field((fresh or {}).get("github_pat")) if fresh else None)
    project = await db_module.get_project(db, project_id)
    repo = (project or {}).get("github_repo") or ""
    branch = (project or {}).get("github_branch") or "main"
    if not repo or not pat:
        raise HTTPException(404, "no repo connected")
    clean = path.strip().lstrip("/")
    if not clean or ".." in clean.split("/") or "://" in clean:
        raise HTTPException(400, "invalid path")
    if "/" not in repo:
        raise HTTPException(400, "repo not owner/name")
    owner, repo_name = repo.split("/", 1)
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo_name}/{branch}/{clean}"
    import httpx  # noqa: PLC0415
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(raw_url, headers={"Authorization": f"Bearer {pat}"})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"upstream fetch failed: {exc}") from exc
    if r.status_code == 404:
        raise HTTPException(404, "file not found in repo")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, "upstream error")
    body = r.content
    if len(body) > 1_000_000:
        raise HTTPException(413, "image too large")
    return Response(
        content=body,
        media_type=r.headers.get("content-type", "application/octet-stream"),
        headers={"Cache-Control": "private, max-age=60"},
    )




@router.post("/projects/{project_id}/github/push-mcp-template", status_code=201)
async def push_mcp_template(project_id: str, request: Request) -> dict[str, Any]:
    """Push template.mcp.json to the connected GitHub repo.

    Fails with 409 if the file already exists. The template contains a
    placeholder Bearer token — users fill it in locally; .mcp.json should
    be gitignored so the real token never gets committed.
    """
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")

    db = await _db(request)
    conn_token, _conn_login = await db_module.get_github_token_for_project(
        db, tenant["id"], project_id
    )
    fresh = await db_module.get_tenant_by_id(request.app.state.db, tenant["id"])
    token = conn_token or db_module.decrypt_field((fresh or {}).get("github_pat"))
    project = await db_module.get_project(db, project_id)
    repo = (project or {}).get("github_repo")
    if not repo:
        raise HTTPException(status_code=400, detail="No GitHub repo connected. Connect one in Settings first.")
    if not token:
        raise HTTPException(status_code=400, detail="GitHub token could not be decrypted. Reconnect your repo.")

    import httpx as _httpx
    gh_headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

    template_content = json.dumps({
        "mcpServers": {
            "meridian": {
                "type": "http",
                "url": "https://usemeridian.us/mcp",
                "headers": {
                    "Authorization": "Bearer sk_meridian_YOUR_KEY_HERE"
                }
            }
        }
    }, indent=2)
    template_b64 = base64.b64encode(template_content.encode()).decode()

    async with _httpx.AsyncClient(timeout=10) as http:
        # Check if file already exists
        check = await http.get(
            f"https://api.github.com/repos/{repo}/contents/template.mcp.json",
            headers=gh_headers,
        )
        if check.status_code == 200:
            raise HTTPException(status_code=409, detail="template.mcp.json already exists in the repo.")

        # Create the file
        r = await http.put(
            f"https://api.github.com/repos/{repo}/contents/template.mcp.json",
            headers=gh_headers,
            json={
                "message": "Add Meridian MCP config template",
                "content": template_b64,
            },
        )
        if r.status_code not in (201, 200):
            raise HTTPException(status_code=r.status_code, detail=f"GitHub API error: {r.text[:200]}")

    return {"pushed": True, "file": "template.mcp.json", "repo": repo}

@router.delete("/projects/{project_id}/github/disconnect", status_code=200)
async def github_disconnect(project_id: str, request: Request) -> dict[str, Any]:
    """Clear the project's stored GitHub repo (keeps tenant PAT for other projects)."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    db = await _db(request)
    await db_module.update_project_settings(
        db, project_id,
        github_repo=None,
        github_branch=None,
    )
    _GITHUB_REPOS_CACHE.pop(tenant["id"], None)
    return {"disconnected": True}


# Per-tenant in-memory cache of the accessible GitHub repo list. Avoids hitting
# the GitHub API on every dropdown render; refreshed lazily after 24h or on demand.
_GITHUB_REPOS_CACHE: dict[str, dict[str, Any]] = {}
_GITHUB_REPOS_TTL_SECONDS = 24 * 3600


@router.get("/projects/{project_id}/github/repos")
async def github_repos(project_id: str, request: Request) -> dict[str, Any]:
    """Return the tenant's accessible GitHub repos for the connect dropdown.

    Cached in-memory for 24h per tenant; pass ?refresh=1 to force a re-fetch.
    """
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    import time as _time
    from ..hosted import _github_user_snapshot as _github_snapshot
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    db = await _db(request)
    conn_token, _conn_login = await db_module.get_github_token_for_project(
        db, tenant["id"], project_id
    )
    fresh = await db_module.get_tenant_by_id(request.app.state.db, tenant["id"])
    token = conn_token or db_module.decrypt_field((fresh or {}).get("github_pat"))
    if not token:
        return {"connected": False, "repos": [], "synced_at": None}
    force = request.query_params.get("refresh") in ("1", "true", "yes")
    now = _time.time()
    cached = _GITHUB_REPOS_CACHE.get(tenant["id"])
    if cached and not force and (now - cached["fetched_at"]) < _GITHUB_REPOS_TTL_SECONDS:
        return {"connected": True, "repos": cached["repos"], "synced_at": cached["fetched_at"], "cached": True}
    try:
        snapshot = await _github_snapshot(token)
    except Exception as exc:
        if cached:
            return {"connected": True, "repos": cached["repos"], "synced_at": cached["fetched_at"], "cached": True, "stale": True}
        raise HTTPException(status_code=502, detail=f"GitHub repo fetch failed: {exc}") from exc
    repos = snapshot.get("repos") or []
    _GITHUB_REPOS_CACHE[tenant["id"]] = {"repos": repos, "fetched_at": now}
    return {"connected": True, "repos": repos, "synced_at": now, "cached": False}


# Common branch names we offer as a fallback when the live GitHub list is
# unavailable (e.g. the API is unreachable). The repo's current/default branch
# is always merged in by the caller so the saved value never disappears.
_FALLBACK_BRANCHES = ("main", "master", "dev", "develop", "gh-pages")


@router.get("/projects/{project_id}/github/branches")
async def github_branches(project_id: str, request: Request) -> dict[str, Any]:
    """v2.8 — list the branches of a repo so the Branch field can be a dropdown.

    Query: ``?repo=owner/name`` (defaults to the tenant's connected repo).
    Falls back to a static list of common branches if GitHub can't be reached,
    so the dropdown always has sensible options.
    """
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    from ..hosted import _github_repo_branches
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    db = await _db(request)
    conn_token, _conn_login = await db_module.get_github_token_for_project(
        db, tenant["id"], project_id
    )
    fresh = await db_module.get_tenant_by_id(request.app.state.db, tenant["id"])
    project = await db_module.get_project(db, project_id)
    repo = (request.query_params.get("repo") or (project or {}).get("github_repo") or "").strip()
    default_branch = (project or {}).get("github_branch") or "main"
    token = conn_token or (db_module.decrypt_field((fresh or {}).get("github_pat")) if fresh else None)
    branches: list[str] = []
    source = "fallback"
    if token and repo and "/" in repo:
        try:
            branches = await _github_repo_branches(token, repo)
            source = "github"
        except Exception:  # noqa: BLE001
            branches = []
    if not branches:
        # Merge the saved branch + common defaults, preserving order, no dupes.
        seen: set[str] = set()
        for b in (default_branch, *_FALLBACK_BRANCHES):
            if b and b not in seen:
                seen.add(b)
                branches.append(b)
    return {"repo": repo, "branches": branches, "default_branch": default_branch, "source": source}


# ---------------------------------------------------------------------------
# 0b061f45 — Multi-account GitHub OAuth management routes
# ---------------------------------------------------------------------------

@router.get("/github/connections")
async def list_github_connections(request: Request) -> dict[str, Any]:
    """List all connected GitHub accounts for the current tenant."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    db = await _db(request)
    connections = await db_module.get_github_connections(db, tenant["id"])
    return {"connections": connections}


@router.delete("/github/connections/{account_login}", status_code=200)
async def remove_github_connection(account_login: str, request: Request) -> dict[str, Any]:
    """Remove a connected GitHub account."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    db = await _db(request)
    await db_module.remove_github_connection(db, tenant["id"], account_login)
    return {"removed": True, "account_login": account_login}


@router.patch("/projects/{project_id}/github/account")
async def set_project_github_account(project_id: str, request: Request) -> dict[str, Any]:
    """Pin a specific GitHub account to a project (or clear the pin)."""
    if not _hosted_mode():
        raise HTTPException(status_code=404)
    tenant = await _get_tenant_from_request(request)
    if tenant is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    body = await request.json()
    account_login = (body.get("account_login") or "").strip() or None
    db = await _db(request)
    await db_module.update_project_settings(db, project_id, github_account_login=account_login)
    return {"project_id": project_id, "github_account_login": account_login}
