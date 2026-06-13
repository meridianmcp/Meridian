"""GitHub integration tests for Meridian."""

from __future__ import annotations

import asyncio

import pytest

from meridian import db as db_module
from meridian import hosted as hosted_module
from meridian import server as server_module


# ---------------------------------------------------------------------------
# v1.1 — CI/CD, search/diff, and Issues GitHub MCP tools
# ---------------------------------------------------------------------------

class _GHResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _MockGHClient:
    """Routes GitHub API calls to canned responses by URL substring."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def _match(self, url):
        for frag, resp in self.routes:
            if frag in url:
                return resp
        return _GHResp(404, {})

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._match(url)

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._match(url)


async def _gh_project(db):
    """Create a project with a connected GitHub repo + a tenant dict with a PAT."""
    proj = await db_module.create_project(db, "gh-tools")
    await db.execute(
        "UPDATE projects SET github_repo = ?, github_branch = ? WHERE id = ?",
        ("octo/repo", "main", proj["id"]),
    )
    await db.commit()
    tenant = {"github_pat": db_module.encrypt_field("ghp_secret")}
    return proj, tenant


def test_new_github_tools_registered():
    """The CI/CD, diff, and Issues tools are advertised when a PAT is set."""
    tenant = {"github_pat": db_module.encrypt_field("ghp_x")}
    names = {t["name"] for t in server_module._github_tools_for_tenant(tenant)}
    for expected in (
        "get_workflow_runs", "get_workflow_run_logs", "trigger_workflow",
        "git_diff", "list_branches", "list_issues", "create_issue", "get_issue",
    ):
        assert expected in names
        assert expected in server_module._GITHUB_TOOL_NAMES
    # No PAT → no tools.
    assert server_module._github_tools_for_tenant({"github_pat": None}) == []


@pytest.mark.asyncio
async def test_github_get_workflow_runs(db, monkeypatch):
    proj, tenant = await _gh_project(db)
    mock = _MockGHClient([
        ("/actions/workflows/deploy.yml/runs", _GHResp(200, {"workflow_runs": [
            {"id": 42, "name": "Deploy", "status": "completed", "conclusion": "success",
             "created_at": "2026-06-13T00:00:00Z", "html_url": "https://gh/runs/42"},
        ]})),
    ])
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: mock)
    res = await server_module._dispatch_github_tool(
        "get_workflow_runs",
        {"project_id": proj["id"], "workflow_name": "deploy.yml"}, tenant, db,
    )
    assert res["count"] == 1
    assert res["runs"][0]["id"] == 42
    assert res["runs"][0]["conclusion"] == "success"


@pytest.mark.asyncio
async def test_github_git_diff(db, monkeypatch):
    proj, tenant = await _gh_project(db)
    mock = _MockGHClient([
        ("/compare/main...feature", _GHResp(200, {
            "total_commits": 3,
            "files": [{"filename": "a.py", "status": "modified", "additions": 5,
                       "deletions": 2, "patch": "@@ -1 +1 @@"}],
        })),
    ])
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: mock)
    res = await server_module._dispatch_github_tool(
        "git_diff", {"project_id": proj["id"], "base": "main", "head": "feature"}, tenant, db,
    )
    assert res["total_commits"] == 3
    assert res["files"][0]["filename"] == "a.py"


@pytest.mark.asyncio
async def test_github_list_issues_excludes_prs(db, monkeypatch):
    proj, tenant = await _gh_project(db)
    mock = _MockGHClient([
        ("/issues", _GHResp(200, [
            {"number": 1, "title": "Bug", "state": "open", "labels": [{"name": "bug"}],
             "created_at": "2026-06-13T00:00:00Z", "html_url": "u", "body": "broken"},
            {"number": 2, "title": "A PR", "state": "open", "labels": [],
             "created_at": "x", "html_url": "u2", "body": "", "pull_request": {"url": "p"}},
        ])),
    ])
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: mock)
    res = await server_module._dispatch_github_tool(
        "list_issues", {"project_id": proj["id"]}, tenant, db,
    )
    nums = [i["number"] for i in res["issues"]]
    assert nums == [1]  # PR #2 excluded
    assert res["issues"][0]["labels"] == ["bug"]


@pytest.mark.asyncio
async def test_github_create_issue(db, monkeypatch):
    proj, tenant = await _gh_project(db)
    mock = _MockGHClient([
        ("/issues", _GHResp(201, {"number": 7, "title": "New", "state": "open",
                                   "html_url": "https://gh/issues/7"})),
    ])
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: mock)
    res = await server_module._dispatch_github_tool(
        "create_issue",
        {"project_id": proj["id"], "title": "New", "body": "b", "labels": ["bug"]},
        tenant, db,
    )
    assert res["number"] == 7
    # POST body carried title/body/labels.
    post_call = next(c for c in mock.calls if c[0] == "POST")
    assert post_call[2]["json"]["title"] == "New"
    assert post_call[2]["json"]["labels"] == ["bug"]


@pytest.mark.asyncio
async def test_github_trigger_workflow_defaults_ref(db, monkeypatch):
    proj, tenant = await _gh_project(db)
    mock = _MockGHClient([("/dispatches", _GHResp(204))])
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: mock)
    res = await server_module._dispatch_github_tool(
        "trigger_workflow",
        {"project_id": proj["id"], "workflow_name": "deploy.yml", "inputs": {"promote": "yes"}},
        tenant, db,
    )
    assert res["dispatched"] is True
    assert res["ref"] == "main"  # defaulted to configured branch
    post_call = next(c for c in mock.calls if c[0] == "POST")
    assert post_call[2]["json"]["inputs"] == {"promote": "yes"}


def _github_client(tmp_path, monkeypatch):
    """Return a hosted TestClient that uses the auth DB for MCP bearer calls."""
    import importlib

    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")
    from fastapi.testclient import TestClient
    from meridian import _deps as deps_module

    mod = importlib.reload(server_module)
    deps_module._tenant_db_cache.clear()

    async def _use_auth_db(request, tenant_id):
        return request.app.state.db

    monkeypatch.setattr(deps_module, "_open_tenant_db_by_id", _use_auth_db)
    monkeypatch.setattr(mod, "_open_tenant_db_by_id", _use_auth_db)
    return mod, TestClient(mod.app)


def test_auth_login_page_has_github_button(client):
    """GET /auth/login shows GitHub OAuth button."""
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 200
    assert "GitHub" in r.text
    assert "/auth/github/login" in r.text


def test_auth_github_login_redirects_when_configured(client, monkeypatch):
    """GET /auth/github/login redirects to GitHub when GITHUB_CLIENT_ID is set."""
    monkeypatch.setenv("GITHUB_CLIENT_ID", "Ov23liFakeId")
    r = client.get("/auth/github/login", follow_redirects=False)
    # Should redirect to GitHub (302) or 503 if key missing
    assert r.status_code in (302, 503)


def test_auth_github_callback_missing_code(client):
    """GET /auth/github/callback without code param returns 400."""
    r = client.get("/auth/github/callback", follow_redirects=False)
    assert r.status_code == 400
    assert "missing oauth code" in r.json().get("detail", "")


def test_auth_github_repo_connect_redirects_to_repo_callback(tmp_path, monkeypatch):
    """GET /auth/github/repo-connect uses the single registered callback URL.

    Only one callback URL is registered on the GitHub OAuth app
    (/auth/github/callback); the state=repo: prefix distinguishes the
    repo-connect flow from login. See workspace rule — never change this
    to /auth/github/repo-callback."""
    monkeypatch.setenv("GITHUB_CLIENT_ID", "Ov23liFakeId")
    monkeypatch.setenv("MERIDIAN_SESSION_SECRET", "test-session-secret")
    mod, client = _github_client(tmp_path, monkeypatch)

    with client:
        async def _setup():
            db = client.app.state.db
            tenant = await db_module.upsert_tenant(db, "gh_repo_connect@example.com")
            session = await db_module.create_user_session(
                db,
                tenant["id"],
                "2099-01-01T00:00:00+00:00",
            )
            return hosted_module._make_session_cookie(session["id"])

        cookie = asyncio.run(_setup())
        client.cookies.set(hosted_module._SESSION_COOKIE, cookie)
        r = client.get(
            "/auth/github/repo-connect?project_id=proj-123",
            follow_redirects=False,
        )
        client.cookies.clear()
        assert r.status_code == 302
        location = r.headers["location"]
        assert "redirect_uri=" in location
        assert "%2Fauth%2Fgithub%2Fcallback" in location
        assert "%2Fauth%2Fgithub%2Frepo-callback" not in location
        assert "state=repo%3Aproj-123" in location


def test_exchange_github_repo_code_uses_repo_callback(monkeypatch):
    """Repo token exchange must send GitHub the single registered callback URL."""
    seen: dict[str, str] = {}

    async def fake_exchange(code, redirect_uri):
        seen["code"] = code
        seen["redirect_uri"] = redirect_uri
        return "access-token"

    async def fake_snapshot(_access_token):
        return {"login": "octocat", "name": "Octo Cat", "avatar_url": "", "repos": []}

    monkeypatch.setattr(hosted_module, "_exchange_github_code_for_token", fake_exchange)
    monkeypatch.setattr(hosted_module, "_github_user_snapshot", fake_snapshot)
    monkeypatch.setenv("MERIDIAN_BASE_URL", "https://usemeridian.us")

    data = asyncio.run(hosted_module.exchange_github_repo_code_for_connection("oauth-code"))
    assert data["access_token"] == "access-token"
    assert seen["code"] == "oauth-code"
    assert seen["redirect_uri"] == "https://usemeridian.us/auth/github/callback"


def test_github_connect_validates_pat(tmp_path, monkeypatch):
    """POST /projects/{id}/github/connect returns 422 when PAT is missing."""
    mod, client = _github_client(tmp_path, monkeypatch)

    with client:
        async def _setup():
            db = client.app.state.db
            tenant = await db_module.upsert_tenant(db, "gh_connect@example.com")
            raw, _ = await db_module.create_api_token(db, tenant["id"])
            proj = await db_module.create_project(db, "gh-proj")
            return raw, proj["id"]

        token, pid = asyncio.run(_setup())

        r = client.post(
            f"/projects/{pid}/github/connect",
            json={"repo": "owner/repo"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 422

        r = client.post(
            f"/projects/{pid}/github/connect",
            json={"pat": "ghp_test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 422

        r = client.post(
            f"/projects/{pid}/github/connect",
            json={"pat": "ghp_test", "repo": "noslash"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 422


def test_github_connect_stores_and_status(tmp_path, monkeypatch):
    """POST connect stores credentials; GET status returns connected=True."""

    class _MockAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            class _Response:
                status_code = 200

                def json(self):
                    return {"login": "testuser"}

            return _Response()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: _MockAsyncClient())

    mod, client = _github_client(tmp_path, monkeypatch)

    with client:
        async def _setup():
            db = client.app.state.db
            tenant = await db_module.upsert_tenant(db, "gh_status@example.com")
            raw, _ = await db_module.create_api_token(db, tenant["id"])
            proj = await db_module.create_project(db, "gh-status-proj")
            return raw, proj["id"]

        token, pid = asyncio.run(_setup())

        r = client.post(
            f"/projects/{pid}/github/connect",
            json={"pat": "ghp_testtoken123", "repo": "owner/myrepo", "branch": "develop"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["connected"] is True
        assert data["repo"] == "owner/myrepo"
        assert data["branch"] == "develop"

        r = client.get(
            f"/projects/{pid}/github/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        status = r.json()
        assert status["connected"] is True
        assert status["repo"] == "owner/myrepo"
        assert status["branch"] == "develop"


def test_github_disconnect_clears_credentials(tmp_path, monkeypatch):
    """DELETE /disconnect clears github_pat; status returns connected=False."""

    class _MockAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            class _Response:
                status_code = 200

                def json(self):
                    return {"login": "u"}

            return _Response()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: _MockAsyncClient())

    mod, client = _github_client(tmp_path, monkeypatch)

    with client:
        async def _setup():
            db = client.app.state.db
            tenant = await db_module.upsert_tenant(db, "gh_disc@example.com")
            raw, _ = await db_module.create_api_token(db, tenant["id"])
            proj = await db_module.create_project(db, "gh-disc-proj")
            return raw, proj["id"]

        token, pid = asyncio.run(_setup())

        client.post(
            f"/projects/{pid}/github/connect",
            json={"pat": "ghp_test", "repo": "a/b"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert client.get(
            f"/projects/{pid}/github/status",
            headers={"Authorization": f"Bearer {token}"},
        ).json()["connected"] is True

        r = client.delete(
            f"/projects/{pid}/github/disconnect",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.json()["disconnected"] is True

        status = client.get(
            f"/projects/{pid}/github/status",
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        assert status["connected"] is False


def test_mcp_tools_list_includes_github_tools_when_connected(tmp_path, monkeypatch):
    """MCP tools/list returns 5 extra GitHub tools when tenant has github_pat."""

    class _MockAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            class _Response:
                status_code = 200

                def json(self):
                    return {"login": "u"}

            return _Response()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: _MockAsyncClient())

    mod, client = _github_client(tmp_path, monkeypatch)

    with client:
        async def _setup():
            db = client.app.state.db
            tenant = await db_module.upsert_tenant(db, "gh_tools@example.com")
            raw, _ = await db_module.create_api_token(db, tenant["id"])
            proj = await db_module.create_project(db, "gh-tools-proj")
            return raw, proj["id"]

        token, pid = asyncio.run(_setup())

        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        tool_names_before = {tool["name"] for tool in r.json()["result"]["tools"]}
        assert "read_file" not in tool_names_before
        assert "get_commits" not in tool_names_before

        client.post(
            f"/projects/{pid}/github/connect",
            json={"pat": "ghp_test", "repo": "a/b"},
            headers={"Authorization": f"Bearer {token}"},
        )

        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        tool_names_after = {tool["name"] for tool in r.json()["result"]["tools"]}
        assert "read_file" in tool_names_after
        assert "list_files" in tool_names_after
        assert "search_code" in tool_names_after
        assert "get_commits" in tool_names_after
        assert "get_commit" in tool_names_after


def test_mcp_tools_list_no_github_tools_when_disconnected(tmp_path, monkeypatch):
    """MCP tools/list omits GitHub tools when tenant has no github_pat."""
    mod, client = _github_client(tmp_path, monkeypatch)

    with client:
        async def _setup():
            db = client.app.state.db
            tenant = await db_module.upsert_tenant(db, "gh_notools@example.com")
            raw, _ = await db_module.create_api_token(db, tenant["id"])
            return raw

        token = asyncio.run(_setup())

        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        tool_names = {tool["name"] for tool in r.json()["result"]["tools"]}
        assert "read_file" not in tool_names
        assert "get_commits" not in tool_names
