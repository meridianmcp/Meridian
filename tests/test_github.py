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

    async def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
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


def test_patch_file_tool_registered_as_writeable():
    """patch_file is advertised, routed, and (unlike read_file) not read-only."""
    tenant = {"github_pat": db_module.encrypt_field("ghp_x")}
    tools = server_module._github_tools_for_tenant(tenant)
    patch = next((t for t in tools if t["name"] == "patch_file"), None)
    assert patch is not None
    assert patch["inputSchema"]["required"] == ["project_id", "file_path", "old_str", "new_str"]
    assert "patch_file" in server_module._GITHUB_TOOL_NAMES
    assert "patch_file" not in server_module._GITHUB_READ_ONLY  # it writes
    assert patch["annotations"]["readOnlyHint"] is False


def test_github_tool_annotations_describe_external_and_mutating_behavior():
    """Directory metadata must not understate GitHub side effects."""
    tenant = {"github_pat": db_module.encrypt_field("ghp_x")}
    by_name = {tool["name"]: tool for tool in server_module._github_tools_for_tenant(tenant)}

    for name, tool in by_name.items():
        assert tool["annotations"]["openWorldHint"] is True, name
        assert tool["annotations"]["title"], name
        assert isinstance(tool["annotations"]["readOnlyHint"], bool), name
        assert isinstance(tool["annotations"]["destructiveHint"], bool), name

    for name in ("patch_file", "trigger_workflow", "create_issue"):
        assert by_name[name]["annotations"]["readOnlyHint"] is False
        assert by_name[name]["annotations"]["destructiveHint"] is True

    for name in server_module._GITHUB_READ_ONLY:
        assert by_name[name]["annotations"]["readOnlyHint"] is True, name
        assert by_name[name]["annotations"]["destructiveHint"] is False, name


def test_builtin_external_search_annotations_are_open_world():
    """Public search providers are external even though the tools are read-only."""
    from meridian import mcp_tools

    by_name = {tool["name"]: tool for tool in mcp_tools._MCP_TOOLS_LIST}
    for name in ("paper_search", "social_search", "github_search"):
        annotations = by_name[name]["annotations"]
        assert annotations["readOnlyHint"] is True
        assert annotations["destructiveHint"] is False
        assert annotations["openWorldHint"] is True


@pytest.mark.asyncio
async def test_github_patch_file_replaces_and_commits(db, monkeypatch):
    import base64
    proj, tenant = await _gh_project(db)
    original = "line1\nTODO: fix\nline3\n"
    # One route serves both the GET (reads content+sha) and the PUT (reads commit),
    # since the mock matches by URL substring regardless of method.
    mock = _MockGHClient([
        ("/contents/app.py", _GHResp(200, {
            "content": base64.b64encode(original.encode()).decode(),
            "sha": "blobsha123", "path": "app.py", "size": len(original),
            "commit": {"sha": "commitsha456789"},
        })),
    ])
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: mock)
    res = await server_module._dispatch_github_tool(
        "patch_file",
        {"project_id": proj["id"], "file_path": "app.py",
         "old_str": "TODO: fix", "new_str": "done"},
        tenant, db,
    )
    assert res["patched"] is True
    assert res["branch"] == "main"  # defaulted to configured branch
    assert res["commit_sha"] == "commitsha456"  # truncated to 12 chars
    put_call = next(c for c in mock.calls if c[0] == "PUT")
    sent = base64.b64decode(put_call[2]["json"]["content"]).decode()
    assert sent == "line1\ndone\nline3\n"
    assert put_call[2]["json"]["sha"] == "blobsha123"  # optimistic-concurrency sha
    assert put_call[2]["json"]["branch"] == "main"


@pytest.mark.asyncio
async def test_github_patch_file_old_str_not_found(db, monkeypatch):
    import base64
    proj, tenant = await _gh_project(db)
    mock = _MockGHClient([
        ("/contents/app.py", _GHResp(200, {
            "content": base64.b64encode(b"hello\n").decode(), "sha": "s", "path": "app.py", "size": 6,
        })),
    ])
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: mock)
    res = await server_module._dispatch_github_tool(
        "patch_file",
        {"project_id": proj["id"], "file_path": "app.py", "old_str": "nope", "new_str": "x"},
        tenant, db,
    )
    assert "not found" in res["error"]
    # Nothing was written.
    assert not any(c[0] == "PUT" for c in mock.calls)


@pytest.mark.asyncio
async def test_github_patch_file_old_str_not_unique(db, monkeypatch):
    import base64
    proj, tenant = await _gh_project(db)
    mock = _MockGHClient([
        ("/contents/app.py", _GHResp(200, {
            "content": base64.b64encode(b"x\nx\n").decode(), "sha": "s", "path": "app.py", "size": 4,
        })),
    ])
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: mock)
    res = await server_module._dispatch_github_tool(
        "patch_file",
        {"project_id": proj["id"], "file_path": "app.py", "old_str": "x", "new_str": "y"},
        tenant, db,
    )
    assert "not unique" in res["error"]
    assert not any(c[0] == "PUT" for c in mock.calls)


@pytest.mark.asyncio
async def test_github_patch_file_missing_file(db, monkeypatch):
    proj, tenant = await _gh_project(db)
    mock = _MockGHClient([("/contents/ghost.py", _GHResp(404, {}))])
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: mock)
    res = await server_module._dispatch_github_tool(
        "patch_file",
        {"project_id": proj["id"], "file_path": "ghost.py", "old_str": "a", "new_str": "b"},
        tenant, db,
    )
    assert "File not found" in res["error"]


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


def test_auth_login_page_has_github_button(client, monkeypatch):
    """GET /auth/login shows the GitHub OAuth button when GITHUB_CLIENT_ID is set.

    98c45dd0 — the button only renders for a configured provider.
    """
    monkeypatch.setenv("GITHUB_CLIENT_ID", "gh-fake")
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


def test_get_github_auth_url_prompt_select_account(monkeypatch):
    """330937c6 — prompt is added only when requested (multi-account picker)."""
    monkeypatch.setenv("GITHUB_CLIENT_ID", "Ov23liFakeId")
    default_url = asyncio.run(hosted_module.get_github_auth_url(scope="repo"))
    assert "prompt=" not in default_url
    forced = asyncio.run(
        hosted_module.get_github_auth_url(scope="repo", prompt="select_account")
    )
    assert "prompt=select_account" in forced


def test_auth_github_repo_connect_select_account_forces_picker(tmp_path, monkeypatch):
    """330937c6 — select_account=1 makes the redirect carry prompt=select_account."""
    monkeypatch.setenv("GITHUB_CLIENT_ID", "Ov23liFakeId")
    monkeypatch.setenv("MERIDIAN_SESSION_SECRET", "test-session-secret")
    mod, client = _github_client(tmp_path, monkeypatch)

    with client:
        async def _setup():
            db = client.app.state.db
            tenant = await db_module.upsert_tenant(db, "gh_multi@example.com")
            session = await db_module.create_user_session(
                db, tenant["id"], "2099-01-01T00:00:00+00:00",
            )
            return hosted_module._make_session_cookie(session["id"])

        cookie = asyncio.run(_setup())
        client.cookies.set(hosted_module._SESSION_COOKIE, cookie)

        # With select_account=1 → picker forced.
        r = client.get(
            "/auth/github/repo-connect?project_id=proj-9&select_account=1",
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "prompt=select_account" in r.headers["location"]

        # Without it → no prompt param (back-compat with the single-account flow).
        r2 = client.get(
            "/auth/github/repo-connect?project_id=proj-9",
            follow_redirects=False,
        )
        client.cookies.clear()
        assert r2.status_code == 302
        assert "prompt=" not in r2.headers["location"]


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


# ---------------------------------------------------------------------------
# f1c6dd63 — GitHub tool descriptions must disclose connected-repo scope
# ---------------------------------------------------------------------------

_REPO_SCOPED_GITHUB_TOOLS = (
    "read_file",
    "patch_file",
    "list_files",
    "search_code",
    "get_commits",
    "search_commits",
    "get_commit",
    "git_diff",
    "list_branches",
    "get_workflow_runs",
    "get_workflow_run_logs",
    "trigger_workflow",
    "list_issues",
    "create_issue",
    "get_issue",
)


def test_github_tool_descriptions_disclose_connected_repo_scope():
    """All connected-repository GitHub tools must explicitly say they operate
    on the project's connected GitHub repository, including git_diff,
    list_branches, get_workflow_runs, trigger_workflow, list_issues, and
    create_issue.
    GitHub repository (Anthropic MCP Directory disclosure requirement,
    f1c6dd63). Checked against the actual generated tool list returned by
    _github_tools_for_tenant, not just the source literal in isolation.
    """
    tenant = {"github_pat": db_module.encrypt_field("ghp_x")}
    tools_by_name = {t["name"]: t for t in server_module._github_tools_for_tenant(tenant)}

    for name in _REPO_SCOPED_GITHUB_TOOLS:
        assert name in tools_by_name, f"{name} missing from generated GitHub tool list"
        description = tools_by_name[name]["description"]
        assert "connected GitHub repository" in description, (
            f"{name} description does not disclose connected-repo scope: {description!r}"
        )
        # None of these tools accept an owner/repo override today, so the
        # description must claim scoping to the connected repo outright --
        # not a hedged "defaults to" that would imply a broader reach than
        # the tool actually has. If an owner/repo param is ever added, this
        # assertion (and the description wording) must be revisited together.
        props = tools_by_name[name]["inputSchema"]["properties"]
        assert "owner" not in props
        assert "repo" not in props


def test_mcp_tools_list_github_tool_descriptions_disclose_connected_repo(tmp_path, monkeypatch):
    """Same disclosure check against the live /mcp tools/list JSON-RPC
    response (not just the in-process generator), per f1c6dd63's acceptance
    criterion to verify the actual tools/list output.
    """

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
            tenant = await db_module.upsert_tenant(db, "gh_disclosure@example.com")
            raw, _ = await db_module.create_api_token(db, tenant["id"])
            proj = await db_module.create_project(db, "gh-disclosure-proj")
            return raw, proj["id"]

        token, pid = asyncio.run(_setup())

        client.post(
            f"/projects/{pid}/github/connect",
            json={"pat": "ghp_test", "repo": "a/b"},
            headers={"Authorization": f"Bearer {token}"},
        )

        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        tools_by_name = {tool["name"]: tool for tool in r.json()["result"]["tools"]}

        for name in _REPO_SCOPED_GITHUB_TOOLS:
            assert name in tools_by_name, f"{name} missing from live tools/list output"
            description = tools_by_name[name]["description"]
            assert "connected GitHub repository" in description, (
                f"{name} live tools/list description missing disclosure: {description!r}"
            )
