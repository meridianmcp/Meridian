"""OAuth 2.0 routes for claude.ai custom connector — c5f8ac43.

Extracted from server.py to bring it under <6k lines.
Globals _oa_tokens / _oa_clients / _oa_codes are module-level so server.py
middleware can reference them via `import meridian.routes.oauth as _oauth_module`.
"""
from __future__ import annotations

import base64 as _b64
import hashlib as _hs
import json as _json
import os
import secrets as _sec
import time as _tm
from pathlib import Path
from typing import Any
from urllib.parse import urlencode as _ue

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse as _RR

from .. import db as db_module

router = APIRouter()

# ---------------------------------------------------------------------------
# In-process state
# ---------------------------------------------------------------------------

_oa_clients: dict = {}
_oa_codes: dict = {}
_oa_tokens: dict[str, dict[str, Any]] = {}

def _oa_token_file() -> Path:
    return Path(os.environ.get("MERIDIAN_DATA_DIR", "data")) / "oauth_tokens.json"


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _oauth_token_hash(token: str) -> str:
    return _hs.sha256(token.encode()).hexdigest()


def _normalize_oa_tokens(tokens: dict[str, Any]) -> dict[str, dict[str, Any]]:
    now = int(_tm.time())
    normalized: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in (tokens or {}).items():
        if not isinstance(raw_value, dict):
            continue
        try:
            exp = int(raw_value.get("exp", 0))
        except (TypeError, ValueError):
            continue
        if exp <= now:
            continue
        token_hash = (
            raw_key
            if isinstance(raw_key, str)
            and len(raw_key) == 64
            and all(c in "0123456789abcdef" for c in raw_key.lower())
            else _oauth_token_hash(str(raw_key))
        )
        normalized[token_hash] = {
            "tenant_id": raw_value.get("tenant_id"),
            "client_id": raw_value.get("client_id"),
            "exp": exp,
        }
    return normalized


def _load_oa_tokens_file() -> dict[str, dict[str, Any]]:
    try:
        f = _oa_token_file()
        if f.exists():
            data = _json.loads(f.read_text())
            return _normalize_oa_tokens(data)
    except Exception:
        pass
    return {}


def _save_oa_tokens(tokens: dict) -> None:
    from ..server import _hosted_mode  # noqa: PLC0415
    if _hosted_mode():
        return
    try:
        f = _oa_token_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_json.dumps(tokens))
    except Exception:
        pass


async def _ensure_oauth_token_table(db: Any) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS oauth_tokens (
            token_hash TEXT PRIMARY KEY,
            tenant_id TEXT,
            client_id TEXT,
            exp BIGINT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS oauth_clients (
            client_id TEXT PRIMARY KEY,
            client_secret TEXT NOT NULL,
            redirect_uris TEXT NOT NULL DEFAULT '[]',
            client_name TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS oauth_codes (
            code TEXT PRIMARY KEY,
            tenant_id TEXT,
            redirect_uri TEXT NOT NULL,
            code_challenge TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.commit()


async def _upsert_oauth_token(
    db: Any,
    token_hash: str,
    *,
    tenant_id: str | None,
    client_id: str,
    exp: int,
) -> None:
    await db.execute(
        "INSERT INTO oauth_tokens (token_hash, tenant_id, client_id, exp) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(token_hash) DO UPDATE SET "
        "tenant_id = excluded.tenant_id, "
        "client_id = excluded.client_id, "
        "exp = excluded.exp",
        (token_hash, tenant_id, client_id, exp),
    )
    await db.commit()


async def _get_oauth_token_from_db(
    db: Any,
    token_hash: str,
) -> dict[str, Any] | None:
    # Check oauth_tokens first (self-hosted / anonymous flows)
    async with db.execute(
        "SELECT token_hash, tenant_id, client_id, exp "
        "FROM oauth_tokens WHERE token_hash = ? AND exp > ?",
        (token_hash, int(_tm.time())),
    ) as cur:
        row = await cur.fetchone()
    if row is not None:
        return {
            "tenant_id": row["tenant_id"],
            "client_id": row["client_id"],
            "exp": int(row["exp"]),
        }
    # Also check api_tokens — hosted-mode OAuth tokens (authorization_code +
    # device_code flows with a tenant_id) are stored there, not in oauth_tokens.
    # Without this fallback, _oa_tokens cache misses after a server restart
    # fail to repopulate tenant_id, breaking the project-DB routing path.
    try:
        async with db.execute(
            "SELECT tenant_id FROM api_tokens WHERE token_hash = ?",
            (token_hash,),
        ) as cur2:
            row2 = await cur2.fetchone()
        if row2 is not None:
            tid = row2["tenant_id"] if hasattr(row2, "__getitem__") else row2[0]
            return {
                "tenant_id": tid,
                "client_id": "claude-ai",
                "exp": int(_tm.time()) + 86400 * 90,
                "_is_api_token": True,  # Regular API token — falls through to bearer path in remote_mcp
            }
    except Exception:
        pass
    return None


async def _load_oauth_tokens_from_db(db: Any) -> dict[str, dict[str, Any]]:
    async with db.execute(
        "SELECT token_hash, tenant_id, client_id, exp "
        "FROM oauth_tokens WHERE exp > ?",
        (int(_tm.time()),),
    ) as cur:
        rows = await cur.fetchall()
    return {
        row["token_hash"]: {
            "tenant_id": row["tenant_id"],
            "client_id": row["client_id"],
            "exp": int(row["exp"]),
        }
        for row in rows
    }


_RT_LIFETIME_SECS = 86400 * 90  # 90 days


async def _issue_refresh_token(
    db: Any,
    *,
    tenant_id: str | None,
    client_id: str,
) -> str:
    """Generate, store, and return a new opaque refresh token."""
    rt = f"rt_meridian_{_sec.token_urlsafe(32)}"
    rt_hash = _oauth_token_hash(rt)
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415
    expires = (datetime.now(timezone.utc) + timedelta(seconds=_RT_LIFETIME_SECS)).isoformat()
    try:
        await db.execute(
            "INSERT INTO oauth_refresh_tokens (token_hash, tenant_id, client_id, expires_at)"
            " VALUES (?, ?, ?, ?)",
            (rt_hash, tenant_id, client_id, expires),
        )
        await db.commit()
    except Exception:
        pass
    return rt


async def _consume_refresh_token(
    db: Any,
    rt_hash: str,
) -> dict[str, Any] | None:
    """Look up a refresh token, mark it used, and return its data.

    Returns None if the token doesn't exist, has expired, or was already used
    (replay protection for token rotation).
    """
    from datetime import datetime, timezone  # noqa: PLC0415
    async with db.execute(
        "SELECT token_hash, tenant_id, client_id, expires_at, used_at"
        " FROM oauth_refresh_tokens WHERE token_hash = ?",
        (rt_hash,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    row_d = dict(row) if hasattr(row, "keys") else dict(zip(
        ["token_hash", "tenant_id", "client_id", "expires_at", "used_at"], row
    ))
    if row_d.get("used_at"):
        return None
    try:
        exp_dt = datetime.fromisoformat(str(row_d["expires_at"]).replace("Z", "+00:00"))
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        if datetime.now(tz=timezone.utc) > exp_dt:
            return None
    except Exception:
        return None
    now_str = datetime.now(timezone.utc).isoformat()
    try:
        await db.execute(
            "UPDATE oauth_refresh_tokens SET used_at = ? WHERE token_hash = ?",
            (now_str, rt_hash),
        )
        await db.commit()
    except Exception:
        pass
    return {"tenant_id": row_d.get("tenant_id"), "client_id": row_d.get("client_id") or ""}


async def _hydrate_oauth_cache(auth_db: Any) -> None:
    global _oa_tokens, _oa_clients

    await _ensure_oauth_token_table(auth_db)
    _oa_tokens = await _load_oauth_tokens_from_db(auth_db)
    # Load persisted OAuth client registrations (DCR)
    try:
        async with auth_db.execute("SELECT client_id, client_secret, redirect_uris FROM oauth_clients") as cur:
            rows = await cur.fetchall()
        for row in rows:
            _oa_clients[row["client_id"]] = {
                "secret": row["client_secret"],
                "redirect_uris": _json.loads(row["redirect_uris"] or "[]")
            }
        if rows:
            print(f"[oauth] loaded {len(rows)} persisted client registrations")
    except Exception:
        pass

    from ..server import _hosted_mode  # noqa: PLC0415
    if _hosted_mode():
        return

    legacy_tokens = _load_oa_tokens_file()
    if not legacy_tokens:
        return

    _oa_tokens.update(legacy_tokens)
    for token_hash, token_data in legacy_tokens.items():
        await _upsert_oauth_token(
            auth_db,
            token_hash,
            tenant_id=token_data.get("tenant_id"),
            client_id=str(token_data.get("client_id") or ""),
            exp=int(token_data.get("exp", 0)),
        )
    _save_oa_tokens(_oa_tokens)


# ---------------------------------------------------------------------------
# Discovery endpoints
# ---------------------------------------------------------------------------

@router.get("/.well-known/oauth-authorization-server")
async def _oauth_meta(request: Request):
    b = str(request.base_url).rstrip("/")
    return JSONResponse({"issuer": b,
        "client_name": "Meridian", "logo_uri": "https://usemeridian.us/static/logo.svg",
        "authorization_endpoint": f"{b}/oauth/authorize",
        "token_endpoint": f"{b}/oauth/token",
        "registration_endpoint": f"{b}/oauth/register",
        "device_authorization_endpoint": f"{b}/oauth/device",
        "scopes_supported": ["mcp"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token", "urn:ietf:params:oauth:grant-type:device_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"]})


@router.get("/.well-known/oauth-protected-resource")
async def _oauth_protected_resource_meta(request: Request):
    b = str(request.base_url).rstrip("/")
    return JSONResponse({
        "resource": f"{b}/mcp",
        "authorization_servers": [b],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
    })


# ---------------------------------------------------------------------------
# Client registration (DCR)
# ---------------------------------------------------------------------------

@router.post("/oauth/register")
async def _oauth_reg(request: Request):
    d = await request.json()
    cid, cs = _sec.token_urlsafe(16), _sec.token_urlsafe(32)
    redirect_uris = d.get("redirect_uris", [])
    client_name = d.get("client_name", "")
    _oa_clients[cid] = {"secret": cs, "redirect_uris": redirect_uris}
    try:
        auth_db = request.app.state.db
        await auth_db.execute(
            "INSERT OR REPLACE INTO oauth_clients (client_id, client_secret, redirect_uris, client_name) VALUES (?, ?, ?, ?)",
            (cid, cs, _json.dumps(redirect_uris), client_name)
        )
        await auth_db.commit()
    except Exception:
        pass  # in-memory fallback still works
    return JSONResponse({"client_id": cid, "client_secret": cs,
        "client_secret_expires_at": 0,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code"],
        "token_endpoint_auth_method": "client_secret_post"}, status_code=201)


# ---------------------------------------------------------------------------
# Device flow (RFC 8628)
# ---------------------------------------------------------------------------

@router.post("/oauth/device")
async def _oauth_device(request: Request):
    """RFC 8628 device authorization endpoint."""
    import string as _str  # noqa: PLC0415
    auth_db = request.app.state.db
    b = str(request.base_url).rstrip("/")
    device_code = _sec.token_hex(32)
    _chars = _str.ascii_uppercase
    user_code = (
        "".join(_sec.choice(_chars) for _ in range(4))
        + "-"
        + "".join(_sec.choice(_chars) for _ in range(4))
    )
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td_cls  # noqa: PLC0415
    expires_at = (_dt.now(tz=_tz.utc) + _td_cls(seconds=300)).strftime("%Y-%m-%d %H:%M:%S")
    await auth_db.execute(
        "INSERT INTO device_codes (device_code, user_code, expires_at) VALUES (?, ?, ?)",
        (device_code, user_code, expires_at),
    )
    await auth_db.commit()
    return JSONResponse({
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": f"{b}/activate",
        "verification_uri_complete": f"{b}/activate?code={user_code}",
        "expires_in": 300,
        "interval": 5,
    })


# ---------------------------------------------------------------------------
# Device activation UI
# ---------------------------------------------------------------------------

@router.get("/activate", response_class=HTMLResponse)
async def _activate_get(request: Request):
    """Device activation page — shows approval UI for a pending device_code."""
    from ..server import _hosted_mode  # noqa: PLC0415
    if _hosted_mode():
        try:
            from ..hosted import _SESSION_COOKIE, _read_session_cookie  # noqa: PLC0415
            cookie_val = request.cookies.get(_SESSION_COOKIE, "")
            if not cookie_val:
                raise ValueError("no session cookie")
            sid = _read_session_cookie(cookie_val)
            if not sid or not await db_module.get_user_session(request.app.state.db, sid):
                raise ValueError("invalid session")
        except Exception:
            from urllib.parse import quote as _q  # noqa: PLC0415
            orig_qs = str(request.url.query)
            next_path = f"/activate?{orig_qs}" if orig_qs else "/activate"
            return _RR(f"/auth/login?next={_q(next_path)}")

    code_param = (request.query_params.get("code") or "").strip().upper()
    b = str(request.base_url).rstrip("/")
    auth_db = request.app.state.db

    row_data: dict | None = None
    if code_param:
        from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415
        async with auth_db.execute(
            "SELECT device_code, user_code, expires_at, approved FROM device_codes WHERE user_code = ?",
            (code_param,),
        ) as _cur:
            _row = await _cur.fetchone()
        if _row:
            _row_d = dict(zip(["device_code", "user_code", "expires_at", "approved"], _row)) if not hasattr(_row, "keys") else dict(_row)
            _exp_str = _row_d.get("expires_at", "")
            try:
                _exp_dt = _dt.fromisoformat(str(_exp_str).replace("Z", "+00:00"))
                if _exp_dt.tzinfo is None:
                    _exp_dt = _exp_dt.replace(tzinfo=_tz.utc)
                if _dt.now(tz=_tz.utc) <= _exp_dt and not _row_d.get("approved"):
                    row_data = _row_d
            except Exception:
                pass

    if code_param and row_data is None:
        error_msg = "This code has expired or was already used. Start the device flow again."
        return HTMLResponse(content=_activate_page(b, code_param, error=error_msg))

    return HTMLResponse(content=_activate_page(b, code_param, row=row_data))


def _activate_page(base_url: str, code: str, *, row: dict | None = None, error: str | None = None) -> str:
    if error:
        body_html = f'<div class="error">{error}</div>'
    elif row:
        uc = row.get("user_code", code)
        body_html = f'''
        <p class="sub">A device or application wants to connect to your Meridian account.</p>
        <div class="code-box">{uc}</div>
        <p class="sub" style="font-size:13px;margin-bottom:24px">Confirm this code matches what your device shows.</p>
        <form method="POST" action="{base_url}/activate">
          <input type="hidden" name="user_code" value="{uc}">
          <div class="btn-row">
            <button type="submit" name="action" value="approve" class="btn-approve">Approve</button>
            <button type="submit" name="action" value="deny" class="btn-deny">Deny</button>
          </div>
        </form>'''
    else:
        body_html = '''
        <p class="sub">Enter the code shown on your device.</p>
        <form method="GET" action="/activate" style="margin-top:16px">
          <input type="text" name="code" placeholder="XXXX-XXXX" autofocus
            style="width:100%;max-width:220px;text-align:center;font-size:20px;font-family:var(--mono);
                   background:#1a1a1a;border:1px solid #444;border-radius:6px;color:#e8e8e8;
                   padding:10px 12px;letter-spacing:4px">
          <button type="submit" style="display:block;margin:12px auto 0;padding:8px 24px;background:#3b82f6;
            color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:14px">Continue</button>
        </form>'''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Meridian — Activate Device</title>
  <style>
    :root{{--mono:'IBM Plex Mono',monospace}}
    body{{font-family:system-ui,sans-serif;background:#0d0d0d;color:#e8e8e8;
          display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
    .card{{background:#1a1a1a;border:1px solid #2e2e2e;border-radius:12px;
           padding:32px 40px;max-width:460px;width:100%;text-align:center}}
    h2{{margin:0 0 6px;font-size:20px;color:#fff}}
    .sub{{color:#888;font-size:14px;margin:0 0 16px}}
    .code-box{{font-family:var(--mono);font-size:28px;letter-spacing:8px;color:#7dd3fc;
               background:#0d0d0d;border:1px solid #333;border-radius:8px;
               padding:16px 24px;display:inline-block;margin-bottom:16px}}
    .btn-row{{display:flex;gap:12px;justify-content:center;margin-top:8px}}
    .btn-approve{{padding:10px 28px;background:#22c55e;color:#fff;border:none;
                  border-radius:6px;font-size:15px;cursor:pointer;font-weight:600}}
    .btn-approve:hover{{background:#16a34a}}
    .btn-deny{{padding:10px 28px;background:#3a3a3a;color:#ccc;border:1px solid #555;
               border-radius:6px;font-size:15px;cursor:pointer}}
    .btn-deny:hover{{background:#555}}
    .error{{color:#f87171;background:#2a1111;border:1px solid #7f1d1d;border-radius:6px;
            padding:12px 16px;font-size:14px}}
  </style>
</head>
<body>
  <div class="card">
    <h2>Meridian</h2>
    {body_html}
  </div>
</body>
</html>"""


@router.post("/activate")
async def _activate_post(request: Request):
    """Handle device approval or denial."""
    from ..server import _hosted_mode  # noqa: PLC0415
    if _hosted_mode():
        try:
            from ..hosted import _SESSION_COOKIE, _read_session_cookie, get_current_tenant  # noqa: PLC0415
            tenant = await get_current_tenant(request)
            tenant_id = tenant["id"]
        except Exception:
            return _RR("/auth/login?next=/activate")
    else:
        tenant_id = None

    form = dict(await request.form())
    user_code = (form.get("user_code") or "").strip().upper()
    action = (form.get("action") or "").strip()
    auth_db = request.app.state.db

    if not user_code:
        return _RR("/activate", status_code=303)

    async with auth_db.execute(
        "SELECT device_code, user_code, expires_at, approved FROM device_codes WHERE user_code = ?",
        (user_code,),
    ) as _cur:
        _row = await _cur.fetchone()

    if _row is None:
        return _RR("/activate", status_code=303)

    if action == "approve":
        await auth_db.execute(
            "UPDATE device_codes SET tenant_id = ?, approved = 1 WHERE user_code = ?",
            (tenant_id, user_code),
        )
        await auth_db.commit()
    else:
        await auth_db.execute("DELETE FROM device_codes WHERE user_code = ?", (user_code,))
        await auth_db.commit()

    return _RR("/dashboard", status_code=303)


# ---------------------------------------------------------------------------
# Authorization code flow
# ---------------------------------------------------------------------------

@router.get("/oauth/authorize")
async def _oauth_auth(request: Request):
    import hashlib  # noqa: PLC0415
    p = dict(request.query_params)
    from ..server import _hosted_mode  # noqa: PLC0415
    if _hosted_mode():
        authed = False
        from ..hosted import _SESSION_COOKIE, _read_session_cookie  # noqa: PLC0415
        auth_db = request.app.state.db
        cookie_val = request.cookies.get(_SESSION_COOKIE, "")
        if cookie_val:
            sid = _read_session_cookie(cookie_val)
            if sid and await db_module.get_user_session(auth_db, sid):
                authed = True
        if not authed:
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                tok_hash = hashlib.sha256(auth_header[7:].encode()).hexdigest()
                if await db_module.get_tenant_from_token_hash(auth_db, tok_hash):
                    authed = True
        if not authed:
            from urllib.parse import quote as _q  # noqa: PLC0415
            orig_qs = str(request.url.query)
            next_path = f"/oauth/authorize?{orig_qs}" if orig_qs else "/oauth/authorize"
            return _RR(f"/auth/login?next={_q(next_path)}")
    _tenant_id: str | None = None
    if _hosted_mode():
        from ..hosted import _SESSION_COOKIE, _read_session_cookie  # noqa: PLC0415
        _cookie = request.cookies.get(_SESSION_COOKIE, "")
        _sid = _read_session_cookie(_cookie) if _cookie else None
        if _sid:
            _sess = await db_module.get_user_session(request.app.state.db, _sid)
            if _sess:
                _tenant_id = _sess.get("tenant_id")

    code = _sec.token_hex(32)
    _redirect_uri = p.get("redirect_uri", "")
    _challenge = p.get("code_challenge") or ""
    _oa_codes[code] = {"client_id": p.get("client_id", ""),
        "redirect_uri": _redirect_uri,
        "challenge": _challenge,
        "tenant_id": _tenant_id,
        "exp": _tm.time() + 600}
    try:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td_cls  # noqa: PLC0415
        _expires = (_dt.now(tz=_tz.utc) + _td_cls(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        _adb = request.app.state.db
        await _adb.execute(
            "INSERT OR REPLACE INTO oauth_codes (code, tenant_id, redirect_uri, code_challenge, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (code, _tenant_id, _redirect_uri, _challenge, _expires),
        )
        await _adb.commit()
    except Exception:
        pass
    qs = _ue({"code": code, "state": p.get("state", "")})
    return _RR(f"{_redirect_uri}?{qs}")


@router.get("/oauth/device-callback")
async def _oauth_device_callback(request: Request):
    """Show auth code; JS auto-redirects to the local callback so MCP SDK completes the flow."""
    p = dict(request.query_params)
    code = p.get("code", "")
    state = p.get("state", "")
    to = p.get("to", "")
    callback_url = f"{to}?code={code}&state={state}" if to else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Meridian — Authorized</title>
<style>
body{{font-family:system-ui,sans-serif;background:#0f0f0f;color:#e5e5e5;
  display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.card{{background:#1a1a1a;border:1px solid #333;border-radius:12px;padding:2rem 2.5rem;
  max-width:520px;width:90%;text-align:center}}
h1{{color:#4ade80;margin:0 0 .5rem}}
p{{color:#999;margin:.5rem 0}}
.url{{background:#111;border:1px solid #444;border-radius:6px;padding:.75rem 1rem;
  font-family:monospace;font-size:.8rem;word-break:break-all;text-align:left;
  color:#e5e5e5;margin:1rem 0;cursor:pointer;user-select:all}}
.copy-btn{{background:#4ade80;color:#000;border:none;border-radius:6px;
  padding:.5rem 1.25rem;font-weight:600;cursor:pointer;font-size:.9rem}}
.copy-btn:hover{{background:#22c55e}}
.note{{font-size:.8rem;color:#666;margin-top:.75rem}}
</style></head>
<body><div class="card">
<h1>&#10003; Authorized</h1>
<p>Paste this URL into your terminal when prompted:</p>
<div class="url" id="cburl" onclick="copyUrl()">{callback_url}</div>
<button class="copy-btn" onclick="copyUrl()">Copy URL</button>
<p class="note" id="note">In a local session this page will auto-close.</p>
</div>
<script>
function copyUrl(){{
  var u = document.getElementById('cburl').textContent;
  navigator.clipboard && navigator.clipboard.writeText(u).then(function(){{
    document.querySelector('.copy-btn').textContent = 'Copied!';
  }});
}}
var to = {_json.dumps(callback_url)};
if (to) {{
  fetch(to, {{mode:'no-cors'}}).then(function(){{
    document.getElementById('note').textContent = 'Local session detected — you can close this tab.';
  }}).catch(function(){{}});
  setTimeout(function(){{ window.location.href = to; }}, 800);
}}
</script></body></html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------

@router.post("/oauth/token")
async def _oauth_token(request: Request):
    ct = request.headers.get("content-type", "")
    d = dict(await request.json() if "json" in ct else await request.form())
    grant_type = d.get("grant_type", "")

    # ── RFC 8628 device_code grant ──────────────────────────────────────────
    if grant_type == "urn:ietf:params:oauth:grant-type:device_code":
        device_code = (d.get("device_code") or "").strip()
        if not device_code:
            return JSONResponse({"error": "invalid_request", "error_description": "device_code required"}, status_code=400)
        auth_db = request.app.state.db
        from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415
        async with auth_db.execute(
            "SELECT device_code, user_code, tenant_id, expires_at, approved FROM device_codes WHERE device_code = ?",
            (device_code,),
        ) as _cur:
            _row = await _cur.fetchone()
        if _row is None:
            return JSONResponse({"error": "expired_token", "error_description": "device code expired or not found"}, status_code=400)
        _row_d = dict(zip(["device_code", "user_code", "tenant_id", "expires_at", "approved"], _row)) if not hasattr(_row, "keys") else dict(_row)
        try:
            _exp_dt = _dt.fromisoformat(str(_row_d["expires_at"]).replace("Z", "+00:00"))
            if _exp_dt.tzinfo is None:
                _exp_dt = _exp_dt.replace(tzinfo=_tz.utc)
            if _dt.now(tz=_tz.utc) > _exp_dt:
                await auth_db.execute("DELETE FROM device_codes WHERE device_code = ?", (device_code,))
                await auth_db.commit()
                return JSONResponse({"error": "expired_token", "error_description": "device code expired"}, status_code=400)
        except Exception:
            return JSONResponse({"error": "expired_token"}, status_code=400)
        if not _row_d.get("approved"):
            return JSONResponse({"error": "authorization_pending"}, status_code=200)
        await auth_db.execute("DELETE FROM device_codes WHERE device_code = ?", (device_code,))
        await auth_db.commit()
        tok = f"sk_meridian_{_sec.token_urlsafe(32)}"
        tok_hash = _oauth_token_hash(tok)
        _oa_tenant_id = _row_d.get("tenant_id")
        tok_data = {"client_id": d.get("client_id", "meridian"), "exp": int(_tm.time() + 86400 * 90), "tenant_id": _oa_tenant_id}
        _oa_tokens[tok_hash] = tok_data
        if _oa_tenant_id:
            import uuid as _uuid  # noqa: PLC0415
            _api_tid = str(_uuid.uuid4())
            try:
                await auth_db.execute(
                    "INSERT INTO api_tokens (id, tenant_id, token_hash, label, token_type) VALUES (?, ?, ?, ?, ?)",
                    (_api_tid, _oa_tenant_id, tok_hash, "claude-code-oauth", "readwrite"),
                )
                await auth_db.commit()
            except Exception:
                pass
        else:
            await _upsert_oauth_token(auth_db, tok_hash, tenant_id=None, client_id=tok_data["client_id"], exp=tok_data["exp"])
        _save_oa_tokens(_oa_tokens)
        return JSONResponse({"access_token": tok, "token_type": "bearer", "expires_in": 86400 * 90})

    # ── RFC 6749 refresh_token grant ────────────────────────────────────────
    if grant_type == "refresh_token":
        rt_val = (d.get("refresh_token") or "").strip()
        if not rt_val:
            return JSONResponse({"error": "invalid_request", "error_description": "refresh_token required"}, status_code=400)
        auth_db = request.app.state.db
        rt_hash = _oauth_token_hash(rt_val)
        rt_data = await _consume_refresh_token(auth_db, rt_hash)
        if rt_data is None:
            return JSONResponse({"error": "invalid_grant", "error_description": "refresh token expired, not found, or already used"}, status_code=400)
        rt_tenant_id = rt_data.get("tenant_id")
        rt_client_id = rt_data.get("client_id") or d.get("client_id", "meridian")
        # Issue new access token
        new_tok = f"sk_meridian_{_sec.token_urlsafe(32)}"
        new_tok_hash = _oauth_token_hash(new_tok)
        new_tok_data = {"client_id": rt_client_id, "exp": int(_tm.time() + 86400 * 90), "tenant_id": rt_tenant_id}
        _oa_tokens[new_tok_hash] = new_tok_data
        if rt_tenant_id:
            import uuid as _uuid  # noqa: PLC0415
            _api_tid = str(_uuid.uuid4())
            try:
                await auth_db.execute(
                    "INSERT INTO api_tokens (id, tenant_id, token_hash, label, token_type)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (_api_tid, rt_tenant_id, new_tok_hash, "oauth", "readwrite"),
                )
                await auth_db.commit()
            except Exception:
                pass
        else:
            await _upsert_oauth_token(auth_db, new_tok_hash, tenant_id=None, client_id=rt_client_id, exp=new_tok_data["exp"])
        _save_oa_tokens(_oa_tokens)
        # Issue new refresh token (rotation)
        new_rt = await _issue_refresh_token(auth_db, tenant_id=rt_tenant_id, client_id=rt_client_id)
        return JSONResponse({
            "access_token": new_tok,
            "token_type": "bearer",
            "expires_in": 86400 * 90,
            "refresh_token": new_rt,
        })

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    _method = d.get("code_challenge_method", "")
    if _method and _method != "S256":
        return JSONResponse({"error": "invalid_request", "error_description": "Only S256 code_challenge_method is supported"}, status_code=400)
    code = d.get("code", "")
    auth_db = request.app.state.db
    cd: dict | None = None
    try:
        from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415
        async with auth_db.execute(
            "SELECT tenant_id, redirect_uri, code_challenge, expires_at FROM oauth_codes WHERE code = ?",
            (code,),
        ) as _cur:
            _row = await _cur.fetchone()
        if _row:
            _exp_str = _row["expires_at"] if hasattr(_row, "__getitem__") else _row[3]
            _exp_dt = _dt.fromisoformat(str(_exp_str).replace("Z", "+00:00"))
            if _exp_dt.tzinfo is None:
                from datetime import timezone as _tz2  # noqa: PLC0415
                _exp_dt = _exp_dt.replace(tzinfo=_tz2.utc)
            if _dt.now(tz=_tz.utc) > _exp_dt:
                await auth_db.execute("DELETE FROM oauth_codes WHERE code = ?", (code,))
                await auth_db.commit()
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            cd = {
                "client_id": d.get("client_id", ""),
                "redirect_uri": _row["redirect_uri"] if hasattr(_row, "__getitem__") else _row[1],
                "challenge": _row["code_challenge"] if hasattr(_row, "__getitem__") else _row[2],
                "tenant_id": _row["tenant_id"] if hasattr(_row, "__getitem__") else _row[0],
            }
            await auth_db.execute("DELETE FROM oauth_codes WHERE code = ?", (code,))
            await auth_db.commit()
    except Exception:
        pass
    if cd is None:
        if code not in _oa_codes:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        _mem = _oa_codes.pop(code)
        if _tm.time() > _mem["exp"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        cd = {
            "client_id": _mem.get("client_id", ""),
            "redirect_uri": _mem.get("redirect_uri", ""),
            "challenge": _mem.get("challenge", ""),
            "tenant_id": _mem.get("tenant_id"),
        }
    if cd["redirect_uri"] and d.get("redirect_uri") != cd["redirect_uri"]:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    v = d.get("code_verifier", "")
    if v and not (43 <= len(v) <= 128):
        return JSONResponse({"error": "invalid_request", "error_description": "code_verifier must be 43-128 characters"}, status_code=400)
    if cd["challenge"]:
        if not v:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        ch = _b64.urlsafe_b64encode(_hs.sha256(v.encode()).digest()).decode().rstrip("=")
        if ch != cd["challenge"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
    tok = f"sk_meridian_{_sec.token_urlsafe(32)}"
    tok_hash = _oauth_token_hash(tok)
    tenant_id = cd.get("tenant_id")
    tok_data = {
        "client_id": cd["client_id"],
        "exp": int(_tm.time() + 86400 * 90),
        "tenant_id": tenant_id,
    }
    _oa_tokens[tok_hash] = tok_data
    if tenant_id:
        import uuid as _uuid  # noqa: PLC0415
        _api_tid = str(_uuid.uuid4())
        try:
            await auth_db.execute(
                "INSERT INTO api_tokens (id, tenant_id, token_hash, label, token_type)"
                " VALUES (?, ?, ?, ?, ?)",
                (_api_tid, tenant_id, tok_hash, "oauth", "readwrite"),
            )
            await auth_db.commit()
            await db_module.delete_orphaned_oauth_tokens(auth_db, tenant_id)
        except Exception:
            pass
    else:
        await _upsert_oauth_token(
            auth_db,
            tok_hash,
            tenant_id=None,
            client_id=tok_data["client_id"],
            exp=tok_data["exp"],
        )
    _save_oa_tokens(_oa_tokens)
    refresh_tok = await _issue_refresh_token(auth_db, tenant_id=tenant_id, client_id=tok_data["client_id"])
    return JSONResponse({
        "access_token": tok,
        "token_type": "bearer",
        "expires_in": 86400 * 90,
        "refresh_token": refresh_tok,
    })
