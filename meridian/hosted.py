"""Hosted-tier auth: Google OAuth, bearer tokens, rate-limiting.

Env vars consumed:
  GOOGLE_CLIENT_ID       — OAuth app client ID
  GOOGLE_CLIENT_SECRET   — OAuth app client secret
  MERIDIAN_SESSION_SECRET — secret for signing session cookies (itsdangerous)
  MERIDIAN_BASE_URL      — public base URL, e.g. https://usemeridian.us
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

_ALIASES: dict[str, str] = {
    "MERIDIAN_BASE_URL": "APP_URL",        # Fly secret name
    "MERIDIAN_SESSION_SECRET": "SESSION_SECRET",  # Fly secret name
}


def _cfg(key: str, default: str | None = None) -> str | None:
    v = os.environ.get(key)
    if v:
        return v
    alias = _ALIASES.get(key)
    if alias:
        v = os.environ.get(alias)
        if v:
            return v
    return default


def _require_cfg(key: str) -> str:
    v = _cfg(key)
    if not v:
        alias = _ALIASES.get(key, "")
        hint = f" (or {alias!r})" if alias else ""
        raise RuntimeError(f"Required env var {key!r}{hint} is not set")
    return v


# ---------------------------------------------------------------------------
# Cookie signing (itsdangerous)
# ---------------------------------------------------------------------------

_SESSION_COOKIE = "meridian_session"
_SESSION_MAX_AGE_HOURS = 24 * 7  # 7 days


def _get_serializer():
    from itsdangerous import URLSafeTimedSerializer
    secret = _cfg("MERIDIAN_SESSION_SECRET", "dev-secret-change-me")
    return URLSafeTimedSerializer(secret, salt="meridian-session")


def _make_session_cookie(session_id: str) -> str:
    s = _get_serializer()
    return s.dumps(session_id)


def _read_session_cookie(cookie_value: str) -> str | None:
    from itsdangerous import BadSignature, SignatureExpired
    s = _get_serializer()
    max_age = _SESSION_MAX_AGE_HOURS * 3600
    try:
        return s.loads(cookie_value, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


# ---------------------------------------------------------------------------
# Google OAuth helpers (authlib)
# ---------------------------------------------------------------------------

def _oauth_client():
    from authlib.integrations.httpx_client import AsyncOAuth2Client
    return AsyncOAuth2Client(
        client_id=_require_cfg("GOOGLE_CLIENT_ID"),
        client_secret=_require_cfg("GOOGLE_CLIENT_SECRET"),
        scope="openid email profile",
        redirect_uri=_callback_url(),
    )


def _callback_url() -> str:
    base = _cfg("MERIDIAN_BASE_URL", "http://localhost:7878").rstrip("/")
    return f"{base}/auth/callback"


async def get_google_auth_url() -> str:
    """Return the Google OAuth authorization URL to redirect the user to."""
    client = _oauth_client()
    url, _state = client.create_authorization_url(
        "https://accounts.google.com/o/oauth2/v2/auth",
        access_type="online",
    )
    return url


async def exchange_code_for_userinfo(code: str) -> dict[str, Any]:
    """Exchange an OAuth code for Google user info dict."""
    import httpx
    client_id = _require_cfg("GOOGLE_CLIENT_ID")
    client_secret = _require_cfg("GOOGLE_CLIENT_SECRET")
    # Exchange code for token
    async with httpx.AsyncClient() as http:
        token_resp = await http.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": _callback_url(),
                "grant_type": "authorization_code",
            },
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
        # Fetch userinfo
        userinfo_resp = await http.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        userinfo_resp.raise_for_status()
        return userinfo_resp.json()


# ---------------------------------------------------------------------------
# GitHub OAuth helpers
# ---------------------------------------------------------------------------

def _github_callback_url() -> str:
    """Return the absolute callback URL for GitHub OAuth."""
    base = _cfg("MERIDIAN_BASE_URL", "http://localhost:7878").rstrip("/")
    return f"{base}/auth/github/callback"


async def get_github_auth_url() -> str:
    """Return the GitHub OAuth authorization URL."""
    client_id = _require_cfg("GITHUB_CLIENT_ID")
    callback = _github_callback_url()
    return (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={callback}"
        f"&scope=user:email"
    )


async def exchange_github_code_for_userinfo(code: str) -> dict[str, Any]:
    """Exchange a GitHub OAuth code for user info (email + login)."""
    import httpx

    client_id = _require_cfg("GITHUB_CLIENT_ID")
    client_secret = _require_cfg("GITHUB_CLIENT_SECRET")

    async with httpx.AsyncClient() as http:
        # Exchange code for access token
        token_resp = await http.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": _github_callback_url(),
            },
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
        access_token = token_data.get("access_token", "")
        if not access_token:
            raise RuntimeError(f"GitHub token exchange failed: {token_data}")

        auth_header = {"Authorization": f"Bearer {access_token}"}

        # Get user profile (login, id, name)
        user_resp = await http.get(
            "https://api.github.com/user",
            headers={**auth_header, "Accept": "application/vnd.github+json"},
        )
        user_resp.raise_for_status()
        user_data = user_resp.json()

        # Get primary verified email (user.email may be null if private)
        email = user_data.get("email")
        if not email:
            emails_resp = await http.get(
                "https://api.github.com/user/emails",
                headers={**auth_header, "Accept": "application/vnd.github+json"},
            )
            emails_resp.raise_for_status()
            emails = emails_resp.json()
            # Pick the primary verified email
            for e in emails:
                if e.get("primary") and e.get("verified"):
                    email = e["email"]
                    break
            if not email and emails:
                email = emails[0].get("email", "")

        return {
            "email": email,
            "sub": f"github:{user_data.get('id', '')}",
            "login": user_data.get("login", ""),
            "name": user_data.get("name") or user_data.get("login", ""),
        }


# ---------------------------------------------------------------------------
# Route handlers (imported and registered by server.py)
# ---------------------------------------------------------------------------

_LOGIN_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sign in — Meridian</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0f;color:#e8eaf0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#16181c;border:1px solid #2a2d35;border-radius:12px;padding:44px 40px;
  max-width:400px;width:100%;margin:20px}
.logo{font-size:1.3rem;font-weight:700;color:#e8eaf0;margin-bottom:6px;text-align:center}
.logo span{color:#6c8fff}
.subtitle{color:#8b8fa8;font-size:.875rem;text-align:center;margin-bottom:32px}
.btn{display:flex;align-items:center;justify-content:center;gap:12px;width:100%;padding:13px 20px;
  border-radius:8px;font-size:.95rem;font-weight:600;cursor:pointer;text-decoration:none;
  border:none;transition:opacity .15s;margin-bottom:12px}
.btn:hover{opacity:.88;text-decoration:none}
.btn-google{background:#fff;color:#3c4043}
.btn-github{background:#24292f;color:#fff}
.divider{display:flex;align-items:center;gap:12px;margin:8px 0 20px;color:#8b8fa8;font-size:.8rem}
.divider::before,.divider::after{content:"";flex:1;height:1px;background:#2a2d35}
.footer-note{text-align:center;font-size:.78rem;color:#8b8fa8;margin-top:24px}
.footer-note a{color:#6c8fff;text-decoration:none}
</style>
</head>
<body>
<div class="card">
  <div class="logo">⬡ <span>Meridian</span></div>
  <div class="subtitle">Sign in to your workspace</div>
  <a href="/auth/google/login" class="btn btn-google">
    <svg width="20" height="20" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>
    Continue with Google
  </a>
  <a href="/auth/github/login" class="btn btn-github">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/></svg>
    Continue with GitHub
  </a>
  <div class="footer-note">
    By signing in you agree to our <a href="/terms">Terms of Service</a>
    and <a href="/privacy">Privacy Policy</a>.
  </div>
</div>
</body>
</html>"""


async def auth_login(request: Request):
    """Serve the sign-in page with Google and GitHub OAuth buttons."""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_LOGIN_PAGE_HTML)


async def auth_callback(request: Request) -> RedirectResponse:
    """Handle Google OAuth callback — upsert tenant, set session cookie."""
    from . import db as db_module

    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="missing oauth code")

    try:
        userinfo = await exchange_code_for_userinfo(code)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"OAuth exchange failed: {exc}") from exc

    email: str = userinfo.get("email", "")
    sub: str = userinfo.get("sub", "")
    if not email:
        raise HTTPException(status_code=400, detail="no email in Google profile")

    db = request.app.state.db
    tenant = await db_module.upsert_tenant(db, email=email, google_sub=sub)

    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=_SESSION_MAX_AGE_HOURS)
    ).isoformat()
    session = await db_module.create_user_session(db, tenant["id"], expires_at)
    cookie_value = _make_session_cookie(session["id"])

    redirect_to = _cfg("MERIDIAN_AFTER_LOGIN_URL", "/dashboard")
    response = RedirectResponse(redirect_to, status_code=302)
    response.set_cookie(
        _SESSION_COOKIE,
        cookie_value,
        httponly=True,
        secure=_cfg("MERIDIAN_BASE_URL", "").startswith("https://"),
        samesite="lax",
        max_age=_SESSION_MAX_AGE_HOURS * 3600,
    )
    return response


async def auth_google_login(request: Request) -> RedirectResponse:
    """Redirect the browser directly to Google's OAuth consent page."""
    try:
        url = await get_google_auth_url()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=302)


async def auth_github_login(request: Request) -> RedirectResponse:
    """Redirect the browser to GitHub's OAuth consent page."""
    try:
        url = await get_github_auth_url()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=302)


async def auth_github_callback(request: Request) -> RedirectResponse:
    """Handle GitHub OAuth callback — upsert tenant, set session cookie."""
    from . import db as db_module

    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="missing oauth code")

    try:
        userinfo = await exchange_github_code_for_userinfo(code)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub OAuth exchange failed: {exc}") from exc

    email: str = userinfo.get("email", "")
    sub: str = userinfo.get("sub", "")  # "github:<id>"
    if not email:
        raise HTTPException(status_code=400, detail="no email in GitHub profile — ensure your GitHub account has a verified public email")

    db = request.app.state.db
    tenant = await db_module.upsert_tenant(db, email=email, google_sub=sub)

    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=_SESSION_MAX_AGE_HOURS)
    ).isoformat()
    session = await db_module.create_user_session(db, tenant["id"], expires_at)
    cookie_value = _make_session_cookie(session["id"])

    redirect_to = _cfg("MERIDIAN_AFTER_LOGIN_URL", "/dashboard")
    response = RedirectResponse(redirect_to, status_code=302)
    response.set_cookie(
        _SESSION_COOKIE,
        cookie_value,
        httponly=True,
        secure=_cfg("MERIDIAN_BASE_URL", "").startswith("https://"),
        samesite="lax",
        max_age=_SESSION_MAX_AGE_HOURS * 3600,
    )
    return response


async def auth_logout(request: Request) -> RedirectResponse:
    """Clear the session cookie and delete the DB session."""
    from . import db as db_module

    cookie_val = request.cookies.get(_SESSION_COOKIE)
    if cookie_val:
        session_id = _read_session_cookie(cookie_val)
        if session_id:
            db = request.app.state.db
            await db_module.delete_user_session(db, session_id)

    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(_SESSION_COOKIE)
    return response


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

async def get_current_tenant(request: Request) -> dict[str, Any]:
    """FastAPI dependency: resolve the tenant from a signed session cookie.

    Raises 401 if the cookie is missing, invalid, or the session is expired.
    """
    from . import db as db_module

    cookie_val = request.cookies.get(_SESSION_COOKIE)
    if not cookie_val:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
        )
    session_id = _read_session_cookie(cookie_val)
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired session",
        )
    db = request.app.state.db
    session = await db_module.get_user_session(db, session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="session not found or expired",
        )
    tenant = await db_module.get_tenant_by_id(db, session["tenant_id"])
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="tenant not found",
        )
    return tenant


# ---------------------------------------------------------------------------
# Neon provisioning — pool architecture (v2.2)
#
# Architecture:
#   - Standard tier: uses NEON_API_KEY (hello@usemeridian.us free-tier account)
#   - Pro tier:      uses NEON_API_KEY_PRO (pro@usemeridian.us Launch account — future)
#   - Pool projects: Neon projects shared by up to MAX_CUSTOMERS_PER_PROJECT customers.
#     Each customer gets their own DATABASE within a shared pool project.
#     When a pool project fills up, a new Neon project is created automatically.
#   - MAX_PROJECTS_STANDARD = 90  (alert admin at 85)
#   - MAX_PROJECTS_PRO       = 95  (alert admin at 90 — for future pro@... account)
#   - MAX_CUSTOMERS_PER_PROJECT = 8
# ---------------------------------------------------------------------------

_MAX_CUSTOMERS_PER_PROJECT = int(os.environ.get("MAX_CUSTOMERS_PER_PROJECT", "8"))
_MAX_PROJECTS_STANDARD = int(os.environ.get("MAX_PROJECTS_STANDARD", "90"))
_MAX_PROJECTS_PRO = int(os.environ.get("MAX_PROJECTS_PRO", "95"))
_ALERT_THRESHOLD_STANDARD = int(os.environ.get("ALERT_THRESHOLD_STANDARD", "85"))
_ALERT_THRESHOLD_PRO = int(os.environ.get("ALERT_THRESHOLD_PRO", "90"))


def _neon_api_key_for_tier(tier: str) -> str:
    """Return the Neon API key for the given tier.  Raises if not configured."""
    if tier == "pro":
        key = _cfg("NEON_API_KEY_PRO")
        if not key:
            raise RuntimeError(
                "NEON_API_KEY_PRO not set — pro tier provisioning unavailable"
            )
        return key
    return _require_cfg("NEON_API_KEY")


async def _create_neon_pool_project(
    api_key: str,
    tier: str,
) -> tuple[str, str]:
    """Create a new Neon pool project and return (neon_project_id, conn_uri).

    Applies tier-appropriate compute and storage limits.
    """
    import httpx
    import uuid as _uuid

    pool_name = f"meridian-pool-{tier}-{str(_uuid.uuid4())[:8]}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # Tier-specific project settings
    if tier == "pro":
        quota: dict[str, Any] = {
            "active_time_seconds": 300 * 3600,  # 300 CU-hrs
            "compute_time_seconds": 300 * 3600,
        }
        autoscaling_limit_max_cu = 4.0
    else:
        quota = {
            "active_time_seconds": 100 * 3600,   # 100 CU-hrs
            "compute_time_seconds": 100 * 3600,
        }
        autoscaling_limit_max_cu = 2.0

    payload: dict[str, Any] = {
        "project": {
            "name": pool_name,
            "region_id": "aws-us-east-2",
            "pg_version": 16,
            "default_endpoint_settings": {
                "autoscaling_limit_min_cu": 0.25,
                "autoscaling_limit_max_cu": autoscaling_limit_max_cu,
                "suspend_timeout_seconds": 300,  # scale to zero after 5 min
            },
            "quota": quota,
        }
    }

    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(
            "https://console.neon.tech/api/v2/projects",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    neon_project_id: str = data["project"]["id"]
    conn_uri: str = data.get("connection_uris", [{}])[0].get("connection_uri", "")
    if not conn_uri:
        async with httpx.AsyncClient(timeout=15) as http:
            cr = await http.get(
                f"https://console.neon.tech/api/v2/projects/{neon_project_id}/connection_uri",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            cr.raise_for_status()
            conn_uri = cr.json().get("uri", "")

    return neon_project_id, conn_uri


async def _create_customer_database(
    api_key: str,
    neon_project_id: str,
    db_name: str,
) -> str:
    """Create a customer database inside an existing pool project.

    Returns the connection URI for the new database.
    """
    import httpx

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # Get default branch ID
    async with httpx.AsyncClient(timeout=15) as http:
        br = await http.get(
            f"https://console.neon.tech/api/v2/projects/{neon_project_id}/branches",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        br.raise_for_status()
        branches = br.json().get("branches", [])
        branch_id = next(
            (b["id"] for b in branches if b.get("primary") or b.get("default")),
            branches[0]["id"] if branches else None,
        )

    if branch_id is None:
        raise RuntimeError(f"No branch found in Neon project {neon_project_id}")

    # Create the database
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(
            f"https://console.neon.tech/api/v2/projects/{neon_project_id}/branches/{branch_id}/databases",
            headers=headers,
            json={"database": {"name": db_name, "owner_name": "neondb_owner"}},
        )
        resp.raise_for_status()

    # Return connection URI for this database
    async with httpx.AsyncClient(timeout=15) as http:
        uri_resp = await http.get(
            f"https://console.neon.tech/api/v2/projects/{neon_project_id}/connection_uri"
            f"?database_name={db_name}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        uri_resp.raise_for_status()
        return uri_resp.json().get("uri", "")


async def provision_neon_db(tenant_id: str, db: Any) -> dict[str, Any]:
    """Provision a Neon database for a tenant using the pool architecture.

    Pool architecture:
      1. Check capacity (raise if at hard limit, warn if at soft limit).
      2. Find an existing pool project with available slot (count < 8).
      3. If none available, create a new Neon pool project.
      4. Create a customer-specific database within the pool project.
      5. Store neon_project_id, pool_project_id, and neon_db_url on the tenant.

    Tier selection:
      - Standard tenants use NEON_API_KEY (hello@usemeridian.us).
      - Pro tenants use NEON_API_KEY_PRO (pro@usemeridian.us — future).

    Returns the updated tenant dict.
    """
    from . import db as db_module

    tenant = await db_module.get_tenant_by_id(db, tenant_id)
    if tenant is None:
        raise ValueError(f"tenant {tenant_id!r} not found")
    if tenant.get("neon_project_id"):
        return tenant  # already provisioned

    tier = tenant.get("plan", "standard")
    api_key = _neon_api_key_for_tier(tier)

    # Capacity check
    await check_capacity(db)

    # Find or create a pool project with room
    pool = await db_module.get_available_pool_project(
        db, tier=tier, max_customers=_MAX_CUSTOMERS_PER_PROJECT
    )

    if pool is None:
        # All existing pool projects are full — create a new one
        neon_project_id, _first_conn_uri = await _create_neon_pool_project(api_key, tier)
        pool = await db_module.register_pool_project(db, neon_project_id, tier)
    else:
        neon_project_id = pool["neon_project_id"]

    # Create a customer-specific database inside the pool project
    email_slug = tenant["email"].split("@")[0][:20].replace(".", "_")
    db_name = f"cust_{email_slug}_{tenant_id[:8]}"
    conn_uri = await _create_customer_database(api_key, neon_project_id, db_name)

    if not conn_uri:
        raise RuntimeError(f"Failed to get connection URI for customer database {db_name!r}")

    # Persist on tenant
    updated = await db_module.update_tenant(
        db,
        tenant_id,
        neon_project_id=neon_project_id,
        pool_project_id=pool["id"],
        neon_db_url=conn_uri,
    )

    # Increment pool project customer count
    await db_module.increment_pool_project_count(db, neon_project_id)

    return updated


# ---------------------------------------------------------------------------
# Welcome email (Resend)
# ---------------------------------------------------------------------------

async def send_welcome_email(
    email: str,
    raw_token: str,
    tenant: dict[str, Any],
) -> None:
    """Send a welcome email with the bearer token and MCP config snippet.

    Requires ``RESEND_API_KEY`` env var.  Silently skips if not set (dev mode).
    Never logs the raw token.
    """
    import httpx

    api_key = _cfg("RESEND_API_KEY")
    if not api_key:
        return  # dev mode — skip

    from_addr = _cfg("MERIDIAN_FROM_EMAIL", "Meridian <noreply@usemeridian.us>")
    base = _cfg("MERIDIAN_BASE_URL", "https://usemeridian.us").rstrip("/")

    mcp_snippet = f'''{{
  "mcpServers": {{
    "meridian": {{
      "command": "npx",
      "args": ["-y", "mcp-remote", "{base}/mcp"],
      "env": {{"BEARER_TOKEN": "{raw_token}"}}
    }}
  }}
}}'''

    html_body = f"""<h2>Welcome to Meridian</h2>
<p>Your account is ready. Sign in any time with <strong>Google</strong> or <strong>GitHub</strong> at
<a href="{base}/auth/login">{base}/auth/login</a>.</p>
<p>To use Meridian's MCP tools, add this to your Claude Code <code>.mcp.json</code>:</p>
<pre>{mcp_snippet}</pre>
<p><strong>Keep your token private.</strong> It grants full access to your Meridian projects.</p>
<p>Dashboard: <a href="{base}/dashboard">{base}/dashboard</a></p>
<p>Docs: <a href="https://ajc3xc.github.io/Meridian/">ajc3xc.github.io/Meridian</a></p>
<p>Questions? Reply to this email or contact <a href="mailto:hello@usemeridian.us">hello@usemeridian.us</a>.</p>"""

    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "from": from_addr,
                "to": [email],
                "subject": "Your Meridian API token",
                "html": html_body,
            },
        )
        resp.raise_for_status()


async def get_tenant_from_bearer(request: Request) -> dict[str, Any]:
    """FastAPI dependency: resolve the tenant from a Bearer token.

    Reads the ``Authorization: Bearer sk_meridian_...`` header, hashes it
    with SHA-256, and looks it up in ``api_tokens``.  Raises 401 on failure.
    """
    from . import db as db_module

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
        )
    raw_token = auth_header[len("Bearer "):]
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    db = request.app.state.db
    tenant = await db_module.get_tenant_from_token_hash(db, token_hash)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid API token",
        )
    return tenant


# ---------------------------------------------------------------------------
# Capacity monitor
# ---------------------------------------------------------------------------

async def _send_capacity_alert(tier: str, projects: int, cap: int) -> None:
    """Best-effort Resend alert to ADMIN_EMAIL when pool projects exceed threshold."""
    admin_email = _cfg("ADMIN_EMAIL")
    if not admin_email:
        return
    try:
        import httpx
        api_key = _cfg("RESEND_API_KEY")
        if not api_key:
            return
        from_addr = _cfg("MERIDIAN_FROM_EMAIL", "Meridian <noreply@usemeridian.us>")
        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "from": from_addr,
                    "to": [admin_email],
                    "subject": f"[Meridian] Capacity warning ({tier}): {projects}/{cap} pool projects",
                    "html": (
                        f"<p>Pool projects used (<strong>{tier}</strong>): "
                        f"<strong>{projects}/{cap}</strong>. "
                        f"Consider expanding pool capacity before hitting the hard limit.</p>"
                    ),
                },
            )
    except Exception:  # noqa: BLE001
        pass


async def check_capacity(db: Any) -> dict[str, Any]:
    """Return Neon pool usage stats and emit alerts when thresholds are hit.

    Per-tier thresholds (pool projects, not customers):
      Standard: alert at {_ALERT_THRESHOLD_STANDARD}, block at {_MAX_PROJECTS_STANDARD}
      Pro:      alert at {_ALERT_THRESHOLD_PRO},      block at {_MAX_PROJECTS_PRO}

    Raises RuntimeError to block provisioning when a tier hits its hard limit.
    Returns a stats dict with project counts per tier.
    """
    from . import db as db_module

    std_counts = await db_module.get_pool_project_counts(db, tier="standard")
    pro_counts = await db_module.get_pool_project_counts(db, tier="pro")

    std_projects = std_counts["projects"]
    pro_projects = pro_counts["projects"]

    # Hard block at cap
    if std_projects >= _MAX_PROJECTS_STANDARD:
        raise RuntimeError(
            f"Standard pool cap reached ({std_projects}/{_MAX_PROJECTS_STANDARD} projects) — "
            "provisioning blocked. Expand pool or upgrade Neon account."
        )
    if pro_projects >= _MAX_PROJECTS_PRO:
        raise RuntimeError(
            f"Pro pool cap reached ({pro_projects}/{_MAX_PROJECTS_PRO} projects) — "
            "provisioning blocked."
        )

    # Soft alerts
    if std_projects >= _ALERT_THRESHOLD_STANDARD:
        await _send_capacity_alert("standard", std_projects, _MAX_PROJECTS_STANDARD)
    if pro_projects >= _ALERT_THRESHOLD_PRO:
        await _send_capacity_alert("pro", pro_projects, _MAX_PROJECTS_PRO)

    return {
        "standard": {
            "projects": std_projects,
            "customers": std_counts["customers"],
            "cap": _MAX_PROJECTS_STANDARD,
            "warning": std_projects >= _ALERT_THRESHOLD_STANDARD,
        },
        "pro": {
            "projects": pro_projects,
            "customers": pro_counts["customers"],
            "cap": _MAX_PROJECTS_PRO,
            "warning": pro_projects >= _ALERT_THRESHOLD_PRO,
        },
    }


# ---------------------------------------------------------------------------
# Churn cleanup
# ---------------------------------------------------------------------------

async def run_churn_cleanup(db: Any) -> None:
    """Warn churned tenants on day 3-7 and day 14; delete on day 28.

    A tenant is considered churned when their stripe_customer_id is NULL (no
    active payment) and they have a neon_project_id (previously provisioned).
    In practice you'll call this from a scheduled job or on server startup.
    """
    import httpx
    from datetime import datetime, timedelta, timezone

    now = datetime.now(tz=timezone.utc)
    api_key = _cfg("RESEND_API_KEY")
    from_addr = _cfg("MERIDIAN_FROM_EMAIL", "Meridian <noreply@usemeridian.us>")
    base = _cfg("MERIDIAN_BASE_URL", "https://usemeridian.us").rstrip("/")

    # Churned = had a Neon project but no active Stripe customer (payment lapsed)
    async with db.execute(
        "SELECT id, email, neon_project_id, pool_project_id, created_at FROM tenants "
        "WHERE stripe_customer_id IS NULL AND neon_project_id IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()
    def _to_d(r: Any) -> dict[str, Any]:
        if isinstance(r, dict):
            return r
        return {k: r[k] for k in r.keys()}

    churned = [_to_d(r) for r in rows] if rows else []

    for tenant in churned:
        try:
            cancelled_at = datetime.fromisoformat(tenant["created_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        days_since = (now - cancelled_at).days

        if days_since >= 28:
            # Delete Neon project and mark tenant fully inactive
            neon_id = tenant.get("neon_project_id")
            if neon_id:
                try:
                    neon_key = _cfg("NEON_API_KEY")
                    if neon_key:
                        async with httpx.AsyncClient(timeout=15) as http:
                            await http.delete(
                                f"https://console.neon.tech/api/v2/projects/{neon_id}",
                                headers={"Authorization": f"Bearer {neon_key}"},
                            )
                except Exception:  # noqa: BLE001
                    pass
            await db.execute(
                "UPDATE tenants SET neon_project_id=NULL, neon_db_url=NULL WHERE id=?",
                (tenant["id"],),
            )
            await db.commit()

        elif days_since >= 14 and api_key:
            try:
                async with httpx.AsyncClient(timeout=10) as http:
                    await http.post(
                        "https://api.resend.com/emails",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={
                            "from": from_addr,
                            "to": [tenant["email"]],
                            "subject": "Your Meridian data will be deleted in 14 days",
                            "html": f"<p>Your Meridian account was cancelled. Your data will be "
                                    f"permanently deleted 14 days from now.</p>"
                                    f"<p>To resubscribe: <a href='{base}/auth/login'>{base}</a></p>",
                        },
                    )
            except Exception:  # noqa: BLE001
                pass

        elif 3 <= days_since <= 7 and api_key:
            try:
                async with httpx.AsyncClient(timeout=10) as http:
                    await http.post(
                        "https://api.resend.com/emails",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={
                            "from": from_addr,
                            "to": [tenant["email"]],
                            "subject": "Your Meridian data — action required in 21+ days",
                            "html": f"<p>You cancelled your Meridian subscription. Your data is "
                                    f"still intact. Resubscribe to keep it:</p>"
                                    f"<p><a href='{base}/auth/login'>{base}</a></p>"
                                    f"<p>If you do nothing, your data will be deleted 28 days after "
                                    f"cancellation.</p>",
                        },
                    )
            except Exception:  # noqa: BLE001
                pass
