"""GitHub integration tests for Meridian."""

from __future__ import annotations

import asyncio

from meridian import db as db_module
from meridian import hosted as hosted_module
from meridian import server as server_module


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
