"""Coverage tests for meridian/routes/oauth.py and meridian/routes/github.py.

Targets uncovered lines: OAuth token-file helpers, refresh-token rotation,
cache hydration, the device-callback HTML page, hosted-mode authorize/activate
guards, and token-grant error branches; plus the full GitHub hosted integration
surface (connect/status/repo-image/push-template/disconnect/repos/branches).

All external HTTP (httpx) and GitHub snapshots are mocked — no network.
Reuses the hosted-client + auth-db patterns from test_github.py / test_v2_hosted.py.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from meridian import db as db_module
from meridian import hosted as hosted_module
from meridian import server as server_module
from meridian.routes import oauth as oa_mod
from meridian.routes import github as gh_mod


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Hosted client helper (mirrors test_github.py::_github_client) — makes the
# per-tenant DB resolve to the shared auth DB so bearer-auth routes work.
# ---------------------------------------------------------------------------

def _hosted_client(tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_HOSTED", "true")

    from meridian import _deps as deps_module

    mod = importlib.reload(server_module)
    deps_module._tenant_db_cache.clear()

    async def _use_auth_db(request, tenant_id):
        return request.app.state.db

    monkeypatch.setattr(deps_module, "_open_tenant_db_by_id", _use_auth_db)
    monkeypatch.setattr(mod, "_open_tenant_db_by_id", _use_auth_db)
    return mod, TestClient(mod.app)


def _tenant_with_pat(client, *, repo=None, branch="main", pat="ghp_secret"):
    """Create a tenant (with API token + encrypted PAT) and a project."""
    async def _setup():
        db = client.app.state.db
        tenant = await db_module.upsert_tenant(db, "cov@example.com")
        raw, _ = await db_module.create_api_token(db, tenant["id"])
        if pat is not None:
            await db_module.update_tenant(
                db, tenant["id"], github_pat=db_module.encrypt_field(pat)
            )
        proj = await db_module.create_project(db, "cov-proj")
        if repo:
            await db_module.update_project_settings(
                db, proj["id"], github_repo=repo, github_branch=branch
            )
        return raw, tenant["id"], proj["id"]

    return _run(_setup())


# ===========================================================================
# OAuth — token-file / normalization helpers (lines 47-93)
# ===========================================================================

def test_normalize_oa_tokens_filters_and_hashes():
    now = int(time.time())
    raw_hash = "a" * 64  # already a sha256-looking key
    plain_key = "sk_meridian_plain"
    tokens = {
        raw_hash: {"tenant_id": "t1", "client_id": "c1", "exp": now + 1000},
        plain_key: {"tenant_id": "t2", "client_id": "c2", "exp": now + 1000},
        "expired": {"tenant_id": "t3", "client_id": "c3", "exp": now - 5},
        "badexp": {"tenant_id": "t4", "client_id": "c4", "exp": "not-an-int"},
        "notadict": "ignored",
    }
    out = oa_mod._normalize_oa_tokens(tokens)
    # raw_hash kept verbatim; plain_key re-hashed; expired/bad/non-dict dropped
    assert raw_hash in out
    assert out[raw_hash]["tenant_id"] == "t1"
    assert oa_mod._oauth_token_hash(plain_key) in out
    assert len(out) == 2


def test_load_and_save_oa_tokens_file_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    # Not hosted → _save_oa_tokens actually writes.
    monkeypatch.setattr(server_module, "_hosted_mode", lambda: False)
    now = int(time.time())
    h = "b" * 64
    payload = {h: {"tenant_id": "t", "client_id": "c", "exp": now + 999}}
    oa_mod._save_oa_tokens(payload)
    assert oa_mod._oa_token_file().exists()
    loaded = oa_mod._load_oa_tokens_file()
    assert h in loaded


def test_save_oa_tokens_noop_in_hosted_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server_module, "_hosted_mode", lambda: True)
    oa_mod._save_oa_tokens({"x" * 64: {"tenant_id": "t", "client_id": "c", "exp": 1}})
    # Hosted mode never persists to disk.
    assert not oa_mod._oa_token_file().exists()


def test_load_oa_tokens_file_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path / "nope"))
    assert oa_mod._load_oa_tokens_file() == {}


# ===========================================================================
# OAuth — refresh token issue / consume (lines 187-250)
# ===========================================================================

@pytest.mark.asyncio
async def test_issue_and_consume_refresh_token_roundtrip(db):
    rt = await oa_mod._issue_refresh_token(db, tenant_id="ten1", client_id="cli1")
    assert rt.startswith("rt_meridian_")
    rt_hash = oa_mod._oauth_token_hash(rt)
    data = await oa_mod._consume_refresh_token(db, rt_hash)
    assert data == {"tenant_id": "ten1", "client_id": "cli1"}
    # Replay: second consume returns None (used_at set).
    assert await oa_mod._consume_refresh_token(db, rt_hash) is None


@pytest.mark.asyncio
async def test_get_oauth_token_from_db_roundtrip(db):
    await oa_mod._ensure_oauth_token_table(db)
    h = "c" * 64
    await oa_mod._upsert_oauth_token(
        db, h, tenant_id=None, client_id="cli", exp=int(time.time()) + 9999
    )
    got = await oa_mod._get_oauth_token_from_db(db, h)
    assert got is not None and got["client_id"] == "cli"
    # Unknown hash → None.
    assert await oa_mod._get_oauth_token_from_db(db, "d" * 64) is None


def test_oauth_device_grant_expired_code(client):
    """A device code past its expires_at returns expired_token and is deleted."""
    dc = client.post("/oauth/device").json()
    device_code = dc["device_code"]

    async def _expire():
        db = client.app.state.db
        # device_codes stores the SHA-256 hash, so match on the hashed value.
        await db.execute(
            "UPDATE device_codes SET expires_at = '2000-01-01 00:00:00', approved = 1 "
            "WHERE device_code = ?",
            (oa_mod._device_hash(device_code),),
        )
        await db.commit()

    _run(_expire())
    r = client.post("/oauth/token", json={
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
    })
    assert r.status_code == 400
    assert r.json()["error"] == "expired_token"


@pytest.mark.asyncio
async def test_consume_refresh_token_unknown_returns_none(db):
    assert await oa_mod._consume_refresh_token(db, "deadbeef") is None


@pytest.mark.asyncio
async def test_consume_refresh_token_expired_returns_none(db):
    from datetime import datetime, timedelta, timezone
    rt = "rt_meridian_expired"
    rt_hash = oa_mod._oauth_token_hash(rt)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    await db.execute(
        "INSERT INTO oauth_refresh_tokens (token_hash, tenant_id, client_id, expires_at)"
        " VALUES (?, ?, ?, ?)",
        (rt_hash, "t", "c", past),
    )
    await db.commit()
    assert await oa_mod._consume_refresh_token(db, rt_hash) is None


@pytest.mark.asyncio
async def test_consume_refresh_token_bad_expiry_returns_none(db):
    rt_hash = oa_mod._oauth_token_hash("rt_meridian_bad")
    await db.execute(
        "INSERT INTO oauth_refresh_tokens (token_hash, tenant_id, client_id, expires_at)"
        " VALUES (?, ?, ?, ?)",
        (rt_hash, "t", "c", "not-a-date"),
    )
    await db.commit()
    assert await oa_mod._consume_refresh_token(db, rt_hash) is None


# ===========================================================================
# OAuth — cache hydration (lines 253-289)
# ===========================================================================

@pytest.mark.asyncio
async def test_hydrate_oauth_cache_loads_tokens_and_clients(db, monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "_hosted_mode", lambda: False)
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    # Seed a persisted client + token row.
    await oa_mod._ensure_oauth_token_table(db)
    await db.execute(
        "INSERT INTO oauth_clients (client_id, client_secret, redirect_uris) VALUES (?, ?, ?)",
        ("cid1", "sec1", json.dumps(["https://x/cb"])),
    )
    await oa_mod._upsert_oauth_token(
        db, "f" * 64, tenant_id=None, client_id="cid1", exp=int(time.time()) + 9999
    )
    await db.commit()
    # Legacy token file present → exercises the file-merge branch.
    legacy_hash = "e" * 64
    oa_mod._save_oa_tokens({legacy_hash: {"tenant_id": None, "client_id": "lc", "exp": int(time.time()) + 9999}})

    await oa_mod._hydrate_oauth_cache(db)
    assert "cid1" in oa_mod._oa_clients
    assert "f" * 64 in oa_mod._oa_tokens
    assert legacy_hash in oa_mod._oa_tokens


# ===========================================================================
# OAuth — device-callback HTML page (lines 609-657)
# ===========================================================================

def test_oauth_device_callback_renders_url(client):
    r = client.get(
        "/oauth/device-callback",
        params={"code": "abc123", "state": "st", "to": "http://localhost:9999/cb"},
    )
    assert r.status_code == 200
    assert "Authorized" in r.text
    assert "http://localhost:9999/cb?code=abc123&state=st" in r.text


def test_oauth_device_callback_no_to_param(client):
    r = client.get("/oauth/device-callback", params={"code": "x", "state": "y"})
    assert r.status_code == 200
    # No "to" → callback_url empty, the auto-redirect script branch is skipped.
    assert "Authorized" in r.text


# ===========================================================================
# OAuth — activate GET (local mode, lines 388-435)
# ===========================================================================

def test_activate_get_blank_form(client):
    r = client.get("/activate")
    assert r.status_code == 200
    assert "Enter the code shown on your device" in r.text


def test_activate_get_unknown_code_shows_error(client):
    r = client.get("/activate", params={"code": "ZZZZ-ZZZZ"})
    assert r.status_code == 200
    assert "expired" in r.text.lower()


def test_activate_get_pending_code_shows_approve(client):
    # Create a real pending device code, then render the approval card.
    dc = client.post("/oauth/device").json()
    uc = dc["user_code"]
    r = client.get("/activate", params={"code": uc})
    assert r.status_code == 200
    assert "Approve" in r.text
    assert uc in r.text


# ===========================================================================
# OAuth — activate hosted-mode auth guards (lines 391-405, 505-512)
# ===========================================================================

def _login_hosted(client, email="authed@example.com"):
    """Create a tenant + user session and set the session cookie on the client."""
    async def _setup():
        db = client.app.state.db
        tenant = await db_module.upsert_tenant(db, email)
        session = await db_module.create_user_session(
            db, tenant["id"], "2099-01-01T00:00:00+00:00"
        )
        return tenant, session

    tenant, session = _run(_setup())
    client.cookies.set(
        hosted_module._SESSION_COOKIE,
        hosted_module._make_session_cookie(session["id"]),
    )
    return tenant


def test_activate_hosted_authed_approve_then_device_token(tmp_path, monkeypatch):
    """Full hosted device flow: issue → authed approve → token grant with tenant."""
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        _login_hosted(client)
        dc = client.post("/oauth/device").json()
        device_code, user_code = dc["device_code"], dc["user_code"]

        # Authed GET renders the approval card (covers 391-435 authed branch).
        g = client.get("/activate", params={"code": user_code})
        assert g.status_code == 200
        assert "Approve" in g.text

        # Authed POST approve persists tenant_id on the device code.
        p = client.post(
            "/activate",
            data={"user_code": user_code, "action": "approve"},
            follow_redirects=False,
        )
        assert p.status_code == 303
        assert p.headers["location"] == "/dashboard"

        # Device-code grant now succeeds and mints a token bound to the tenant.
        tok = client.post("/oauth/token", json={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        })
        assert tok.status_code == 200
        assert tok.json()["access_token"].startswith("sk_meridian_")


def test_activate_hosted_authed_deny_deletes_code(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        _login_hosted(client)
        dc = client.post("/oauth/device").json()
        p = client.post(
            "/activate",
            data={"user_code": dc["user_code"], "action": "deny"},
            follow_redirects=False,
        )
        assert p.status_code == 303
        # Denied → grant reports RFC 8628 access_denied and consumes the code.
        tok = client.post("/oauth/token", json={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": dc["device_code"],
        })
        assert tok.json()["error"] == "access_denied"


def test_authorize_hosted_session_cookie_full_flow(tmp_path, monkeypatch):
    """Authed-by-cookie authorize binds tenant_id; token grant inserts api_token."""
    import urllib.parse
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        _login_hosted(client, "authz@example.com")
        r = client.get(
            "/oauth/authorize",
            params={"client_id": "meridian", "redirect_uri": "http://a/cb", "state": "s1"},
            follow_redirects=False,
        )
        assert r.status_code in (302, 307)
        loc = r.headers["location"]
        assert loc.startswith("http://a/cb?")
        code = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query))["code"]
        tok = client.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://a/cb",
            "client_id": "meridian",
        })
        assert tok.status_code == 200
        body = tok.json()
        assert body["access_token"].startswith("sk_meridian_")
        # Refresh the tenant-bound token → exercises the refresh-with-tenant
        # api_tokens insert branch.
        ref = client.post("/oauth/token", json={
            "grant_type": "refresh_token",
            "refresh_token": body["refresh_token"],
        })
        assert ref.status_code == 200
        assert ref.json()["access_token"].startswith("sk_meridian_")


def test_token_authorization_code_in_memory_fallback(client):
    """When the code is only in the in-memory _oa_codes map (no DB row)."""
    code = "mem-only-code-xyz"
    oa_mod._oa_codes[code] = {
        "client_id": "meridian",
        "redirect_uri": "",
        "challenge": "",
        "tenant_id": None,
        "exp": time.time() + 600,
    }
    try:
        tok = client.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
        })
        assert tok.status_code == 200
        assert tok.json()["access_token"].startswith("sk_meridian_")
    finally:
        oa_mod._oa_codes.pop(code, None)


def test_token_in_memory_code_expired(client):
    code = "mem-expired-code"
    oa_mod._oa_codes[code] = {
        "client_id": "meridian",
        "redirect_uri": "",
        "challenge": "",
        "tenant_id": None,
        "exp": time.time() - 10,
    }
    try:
        tok = client.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
        })
        assert tok.status_code == 400
        assert tok.json()["error"] == "invalid_grant"
    finally:
        oa_mod._oa_codes.pop(code, None)


def test_activate_get_hosted_redirects_to_login(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        r = client.get("/activate", params={"code": "AAAA-BBBB"}, follow_redirects=False)
        assert r.status_code in (302, 307)
        assert "/auth/login" in r.headers["location"]


def test_activate_post_hosted_redirects_to_login(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        r = client.post(
            "/activate",
            data={"user_code": "AAAA-BBBB", "action": "approve"},
            follow_redirects=False,
        )
        assert r.status_code in (302, 307)
        assert "/auth/login" in r.headers["location"]


def test_activate_post_no_user_code_redirects(client):
    r = client.post("/activate", data={"action": "approve"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/activate"


def test_activate_post_unknown_code_redirects(client):
    r = client.post(
        "/activate",
        data={"user_code": "QQQQ-QQQQ", "action": "approve"},
        follow_redirects=False,
    )
    assert r.status_code == 303


# ===========================================================================
# OAuth — authorize hosted-mode guard (lines 556-574)
# ===========================================================================

def test_authorize_hosted_unauthed_redirects_to_login(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        r = client.get(
            "/oauth/authorize",
            params={"client_id": "meridian", "redirect_uri": "http://x/cb", "state": "s"},
            follow_redirects=False,
        )
        assert r.status_code in (302, 307)
        assert "/auth/login" in r.headers["location"]


def test_authorize_hosted_bearer_token_authorizes(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        raw, _tid, _pid = _tenant_with_pat(client, pat=None)
        r = client.get(
            "/oauth/authorize",
            params={"client_id": "meridian", "redirect_uri": "http://x/cb", "state": "s"},
            headers={"Authorization": f"Bearer {raw}"},
            follow_redirects=False,
        )
        # Authed via bearer → issues a code and redirects to redirect_uri.
        assert r.status_code in (302, 307)
        assert r.headers["location"].startswith("http://x/cb?")
        assert "code=" in r.headers["location"]


# ===========================================================================
# OAuth — token grant error branches (lines 671-840)
# ===========================================================================

def test_token_device_grant_missing_device_code(client):
    r = client.post("/oauth/token", json={
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    })
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_token_refresh_grant_missing_refresh_token(client):
    r = client.post("/oauth/token", json={"grant_type": "refresh_token"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_token_refresh_grant_invalid_token(client):
    r = client.post("/oauth/token", json={
        "grant_type": "refresh_token",
        "refresh_token": "rt_meridian_nope",
    })
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_token_unsupported_grant_type(client):
    r = client.post("/oauth/token", json={"grant_type": "password"})
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_grant_type"


def test_token_bad_code_challenge_method(client):
    r = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": "x",
        "code_challenge_method": "plain",
    })
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"
    assert "S256" in r.json()["error_description"]


def test_token_unknown_code_invalid_grant(client):
    r = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": "totally-unknown-code",
    })
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_token_redirect_uri_mismatch(client):
    """Authorize with a redirect_uri, then exchange with a different one → invalid_grant."""
    import urllib.parse
    r = client.get(
        "/oauth/authorize",
        params={"client_id": "meridian", "redirect_uri": "http://a/cb"},
        follow_redirects=False,
    )
    code = dict(urllib.parse.parse_qsl(
        urllib.parse.urlparse(r.headers["location"]).query
    ))["code"]
    tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://different/cb",
    })
    assert tok.status_code == 400
    assert tok.json()["error"] == "invalid_grant"


def test_token_code_verifier_wrong_length(client):
    import urllib.parse
    r = client.get(
        "/oauth/authorize",
        params={"client_id": "meridian", "redirect_uri": "http://a/cb"},
        follow_redirects=False,
    )
    code = dict(urllib.parse.parse_qsl(
        urllib.parse.urlparse(r.headers["location"]).query
    ))["code"]
    tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://a/cb",
        "code_verifier": "too-short",  # < 43 chars
    })
    assert tok.status_code == 400
    assert tok.json()["error"] == "invalid_request"
    assert "43-128" in tok.json()["error_description"]


def test_token_challenge_present_but_no_verifier(client):
    """Authorize with a PKCE challenge, exchange with no verifier → invalid_grant."""
    import urllib.parse
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(b"A" * 64).digest()
    ).decode().rstrip("=")
    r = client.get(
        "/oauth/authorize",
        params={
            "client_id": "meridian",
            "redirect_uri": "http://a/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    code = dict(urllib.parse.parse_qsl(
        urllib.parse.urlparse(r.headers["location"]).query
    ))["code"]
    tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://a/cb",
    })
    assert tok.status_code == 400
    assert tok.json()["error"] == "invalid_grant"


def test_refresh_token_grant_full_rotation(client):
    """authorization_code → refresh_token rotation issues a brand-new pair."""
    import urllib.parse
    r = client.get(
        "/oauth/authorize",
        params={"client_id": "meridian", "redirect_uri": "http://a/cb"},
        follow_redirects=False,
    )
    code = dict(urllib.parse.parse_qsl(
        urllib.parse.urlparse(r.headers["location"]).query
    ))["code"]
    first = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://a/cb",
    }).json()
    rt = first["refresh_token"]
    second = client.post("/oauth/token", json={
        "grant_type": "refresh_token",
        "refresh_token": rt,
    })
    assert second.status_code == 200
    body = second.json()
    assert body["access_token"].startswith("sk_meridian_")
    assert body["refresh_token"] != rt
    # Old refresh token is now consumed → replay rejected.
    replay = client.post("/oauth/token", json={
        "grant_type": "refresh_token",
        "refresh_token": rt,
    })
    assert replay.json()["error"] == "invalid_grant"


# ===========================================================================
# GitHub — local-mode 404 guards (every route 404s when not hosted)
# ===========================================================================

def test_github_routes_404_in_local_mode(client):
    p = client.post("/projects", json={"name": "gh404"}).json()
    pid = p["id"]
    assert client.post(f"/projects/{pid}/github/connect", json={}).status_code == 404
    assert client.get(f"/projects/{pid}/github/repos").status_code == 404
    assert client.get(f"/projects/{pid}/github/branches").status_code == 404
    assert client.get(f"/projects/{pid}/repo-image", params={"path": "a.png"}).status_code == 404
    assert client.post(f"/projects/{pid}/github/push-mcp-template").status_code == 404
    assert client.delete(f"/projects/{pid}/github/disconnect").status_code == 404


# ===========================================================================
# GitHub — connect (lines 51-82) with mocked snapshot
# ===========================================================================

def test_github_connect_corrects_repo_and_branch(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)

    async def _snap(_token):
        return {
            "login": "octo",
            "avatar_url": "http://av",
            "repos": [
                {"full_name": "octo/real", "default_branch": "trunk"},
            ],
        }

    monkeypatch.setattr(hosted_module, "_github_user_snapshot", _snap)
    with client:
        raw, _tid, pid = _tenant_with_pat(client, pat=None)
        # Request a repo not in the snapshot → server falls back to repos[0].
        r = client.post(
            f"/projects/{pid}/github/connect",
            json={"pat": "ghp_new", "repo": "octo/missing", "branch": "main"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["connected"] is True
        assert body["repo"] == "octo/real"
        assert body["branch"] == "trunk"
        assert body["github_user"] == "octo"


def test_github_connect_no_pat_not_connected_422(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        raw, _tid, pid = _tenant_with_pat(client, pat=None)
        # No pat in body and none stored → "GitHub is not connected".
        r = client.post(
            f"/projects/{pid}/github/connect",
            json={"repo": "owner/repo"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 422


def test_github_connect_snapshot_runtime_error_422(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)

    async def _snap(_token):
        raise RuntimeError("bad token")

    monkeypatch.setattr(hosted_module, "_github_user_snapshot", _snap)
    with client:
        raw, _tid, pid = _tenant_with_pat(client, pat=None)
        r = client.post(
            f"/projects/{pid}/github/connect",
            json={"pat": "ghp_bad", "repo": "owner/repo"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 422
        assert "bad token" in r.json()["detail"]


def test_github_connect_request_error_502(tmp_path, monkeypatch):
    import httpx
    _mod, client = _hosted_client(tmp_path, monkeypatch)

    async def _snap(_token):
        raise httpx.ConnectError("dns fail")

    monkeypatch.setattr(hosted_module, "_github_user_snapshot", _snap)
    with client:
        raw, _tid, pid = _tenant_with_pat(client, pat=None)
        r = client.post(
            f"/projects/{pid}/github/connect",
            json={"pat": "ghp_x", "repo": "owner/repo"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 502


def test_github_connect_unauthenticated_401(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        _raw, _tid, pid = _tenant_with_pat(client, pat=None)
        r = client.post(f"/projects/{pid}/github/connect", json={"repo": "a/b"})
        assert r.status_code == 401


# ===========================================================================
# GitHub — status (lines 93-124)
# ===========================================================================

def test_github_status_connected_with_snapshot(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)

    async def _snap(_token):
        return {"login": "me", "avatar_url": "http://a",
                "repos": [{"full_name": "me/other"}]}

    monkeypatch.setattr(hosted_module, "_github_user_snapshot", _snap)
    with client:
        raw, _tid, pid = _tenant_with_pat(client, repo="me/sel", branch="dev")
        r = client.get(
            f"/projects/{pid}/github/status",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["connected"] is True
        assert body["pat_linked"] is True
        assert body["repo"] == "me/sel"
        assert body["github_user"] == "me"
        # selected repo not in snapshot list → it's prepended.
        assert any(rp["full_name"] == "me/sel" for rp in body["repos"])


def test_github_status_unauthenticated_401(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        _raw, _tid, pid = _tenant_with_pat(client)
        r = client.get(f"/projects/{pid}/github/status")
        assert r.status_code == 401


# ===========================================================================
# GitHub — repo-image proxy (lines 127-177)
# ===========================================================================

class _ImgResp:
    def __init__(self, status, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}


class _MockHTTP:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, **kw):
        return self._resp


def test_repo_image_proxy_success(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kw: _MockHTTP(_ImgResp(200, b"PNGDATA", {"content-type": "image/png"})),
    )
    with client:
        raw, _tid, pid = _tenant_with_pat(client, repo="o/r", branch="main")
        r = client.get(
            f"/projects/{pid}/repo-image",
            params={"path": "docs/x.png"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200
        assert r.content == b"PNGDATA"
        assert r.headers["content-type"] == "image/png"


def test_repo_image_proxy_invalid_path(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        raw, _tid, pid = _tenant_with_pat(client, repo="o/r")
        r = client.get(
            f"/projects/{pid}/repo-image",
            params={"path": "../etc/passwd"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 400


def test_repo_image_proxy_no_repo_404(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        raw, _tid, pid = _tenant_with_pat(client)  # has pat, no repo
        r = client.get(
            f"/projects/{pid}/repo-image",
            params={"path": "x.png"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 404


def test_repo_image_proxy_upstream_404(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: _MockHTTP(_ImgResp(404)))
    with client:
        raw, _tid, pid = _tenant_with_pat(client, repo="o/r")
        r = client.get(
            f"/projects/{pid}/repo-image",
            params={"path": "missing.png"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 404


def test_repo_image_proxy_too_large_413(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    big = b"x" * 1_000_001
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kw: _MockHTTP(_ImgResp(200, big, {"content-type": "image/png"})),
    )
    with client:
        raw, _tid, pid = _tenant_with_pat(client, repo="o/r")
        r = client.get(
            f"/projects/{pid}/repo-image",
            params={"path": "huge.png"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 413


# ===========================================================================
# GitHub — push-mcp-template (lines 182-244)
# ===========================================================================

class _PushHTTP:
    """Routes a GET (existence check) then a PUT (create)."""

    def __init__(self, get_status, put_status, put_text=""):
        self._get_status = get_status
        self._put_status = put_status
        self._put_text = put_text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, **kw):
        return _ImgResp(self._get_status)

    async def put(self, url, **kw):
        r = _ImgResp(self._put_status)
        r.text = self._put_text
        return r


def test_push_mcp_template_success(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "httpx.AsyncClient", lambda **kw: _PushHTTP(get_status=404, put_status=201)
    )
    with client:
        raw, _tid, pid = _tenant_with_pat(client, repo="o/r")
        r = client.post(
            f"/projects/{pid}/github/push-mcp-template",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 201
        assert r.json()["pushed"] is True
        assert r.json()["file"] == "template.mcp.json"


def test_push_mcp_template_already_exists_409(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "httpx.AsyncClient", lambda **kw: _PushHTTP(get_status=200, put_status=201)
    )
    with client:
        raw, _tid, pid = _tenant_with_pat(client, repo="o/r")
        r = client.post(
            f"/projects/{pid}/github/push-mcp-template",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 409


def test_push_mcp_template_github_error(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kw: _PushHTTP(get_status=404, put_status=422, put_text="boom"),
    )
    with client:
        raw, _tid, pid = _tenant_with_pat(client, repo="o/r")
        r = client.post(
            f"/projects/{pid}/github/push-mcp-template",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 422


def test_push_mcp_template_no_repo_400(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        raw, _tid, pid = _tenant_with_pat(client)  # pat but no repo
        r = client.post(
            f"/projects/{pid}/github/push-mcp-template",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 400


def test_push_mcp_template_unauthenticated_401(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        _raw, _tid, pid = _tenant_with_pat(client, repo="o/r")
        r = client.post(f"/projects/{pid}/github/push-mcp-template")
        assert r.status_code == 401


# ===========================================================================
# GitHub — disconnect (lines 246-261)
# ===========================================================================

def test_github_disconnect_clears_repo(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        raw, tid, pid = _tenant_with_pat(client, repo="o/r")
        # Seed the repos cache so we exercise the pop().
        gh_mod._GITHUB_REPOS_CACHE[tid] = {"repos": [], "fetched_at": time.time()}
        r = client.delete(
            f"/projects/{pid}/github/disconnect",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200
        assert r.json()["disconnected"] is True
        assert tid not in gh_mod._GITHUB_REPOS_CACHE


def test_github_disconnect_unauthenticated_401(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        _raw, _tid, pid = _tenant_with_pat(client, repo="o/r")
        r = client.delete(f"/projects/{pid}/github/disconnect")
        assert r.status_code == 401


# ===========================================================================
# GitHub — repos listing + cache (lines 270-300)
# ===========================================================================

def test_github_repos_no_token_not_connected(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        raw, _tid, pid = _tenant_with_pat(client, pat=None)  # no PAT
        r = client.get(
            f"/projects/{pid}/github/repos",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200
        assert r.json() == {"connected": False, "repos": [], "synced_at": None}


def test_github_repos_fetch_and_cache(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def _snap(_token):
        calls["n"] += 1
        return {"repos": [{"full_name": "a/b"}]}

    monkeypatch.setattr(hosted_module, "_github_user_snapshot", _snap)
    with client:
        raw, tid, pid = _tenant_with_pat(client)
        gh_mod._GITHUB_REPOS_CACHE.pop(tid, None)
        r1 = client.get(
            f"/projects/{pid}/github/repos",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r1.status_code == 200
        assert r1.json()["cached"] is False
        # Second call → served from cache (snapshot not re-called).
        r2 = client.get(
            f"/projects/{pid}/github/repos",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r2.json()["cached"] is True
        assert calls["n"] == 1


def test_github_repos_fetch_error_502(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)

    async def _snap(_token):
        raise RuntimeError("api down")

    monkeypatch.setattr(hosted_module, "_github_user_snapshot", _snap)
    with client:
        raw, tid, pid = _tenant_with_pat(client)
        gh_mod._GITHUB_REPOS_CACHE.pop(tid, None)
        r = client.get(
            f"/projects/{pid}/github/repos?refresh=1",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 502


def test_github_repos_error_returns_stale_cache(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)

    async def _snap(_token):
        raise RuntimeError("api down")

    monkeypatch.setattr(hosted_module, "_github_user_snapshot", _snap)
    with client:
        raw, tid, pid = _tenant_with_pat(client)
        gh_mod._GITHUB_REPOS_CACHE[tid] = {"repos": [{"full_name": "stale/repo"}], "fetched_at": time.time()}
        r = client.get(
            f"/projects/{pid}/github/repos?refresh=1",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200
        assert r.json()["stale"] is True


def test_github_repos_unauthenticated_401(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        _raw, _tid, pid = _tenant_with_pat(client)
        r = client.get(f"/projects/{pid}/github/repos")
        assert r.status_code == 401


# ===========================================================================
# GitHub — branches (lines 309-343)
# ===========================================================================

def test_github_branches_from_github(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)

    async def _branches(_token, _repo):
        return ["main", "feature-x"]

    monkeypatch.setattr(hosted_module, "_github_repo_branches", _branches)
    with client:
        raw, _tid, pid = _tenant_with_pat(client, repo="o/r")
        r = client.get(
            f"/projects/{pid}/github/branches",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "github"
        assert "feature-x" in body["branches"]


def test_github_branches_fallback_on_error(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)

    async def _branches(_token, _repo):
        raise RuntimeError("nope")

    monkeypatch.setattr(hosted_module, "_github_repo_branches", _branches)
    with client:
        raw, _tid, pid = _tenant_with_pat(client, repo="o/r", branch="release")
        r = client.get(
            f"/projects/{pid}/github/branches",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "fallback"
        # saved branch is first, then the common defaults, no dupes.
        assert body["branches"][0] == "release"
        assert "main" in body["branches"]


def test_github_branches_unauthenticated_401(tmp_path, monkeypatch):
    _mod, client = _hosted_client(tmp_path, monkeypatch)
    with client:
        _raw, _tid, pid = _tenant_with_pat(client, repo="o/r")
        r = client.get(f"/projects/{pid}/github/branches")
        assert r.status_code == 401
