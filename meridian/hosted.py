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
# Route handlers (imported and registered by server.py)
# ---------------------------------------------------------------------------

async def auth_login(request: Request) -> RedirectResponse:
    """Redirect the browser to Google's OAuth consent page."""
    try:
        url = await get_google_auth_url()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=302)


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
# Neon provisioning
# ---------------------------------------------------------------------------

async def provision_neon_db(tenant_id: str, db: Any) -> dict[str, Any]:
    """Create a Neon project for a tenant and save the connection URL.

    Requires ``NEON_API_KEY`` env var.  Returns the updated tenant dict.
    The connection URL is stored in ``tenants.neon_db_url`` and is the
    Postgres URL that should be passed to ``init_db()`` for this tenant.

    Raises ``RuntimeError`` if the Neon API call fails or the key is missing.
    """
    import httpx
    from . import db as db_module

    api_key = _require_cfg("NEON_API_KEY")
    tenant = await db_module.get_tenant_by_id(db, tenant_id)
    if tenant is None:
        raise ValueError(f"tenant {tenant_id!r} not found")
    if tenant.get("neon_project_id"):
        return tenant  # already provisioned

    project_name = f"meridian-{tenant['email'].split('@')[0][:20]}-{tenant_id[:8]}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "project": {
            "name": project_name,
            "region_id": "aws-us-east-2",
            "pg_version": 16,
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

    project = data["project"]
    neon_project_id = project["id"]

    # Get the connection string for the default branch/database
    conn_uri = data.get("connection_uris", [{}])[0].get("connection_uri", "")
    if not conn_uri:
        # Fallback: query the connection string endpoint
        async with httpx.AsyncClient(timeout=15) as http:
            cr = await http.get(
                f"https://console.neon.tech/api/v2/projects/{neon_project_id}/connection_uri",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            cr.raise_for_status()
            conn_uri = cr.json().get("uri", "")

    updated = await db_module.update_tenant(
        db,
        tenant_id,
        neon_project_id=neon_project_id,
        neon_db_url=conn_uri,
    )
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
<p>Your account is ready. Add this to your Claude Code <code>.mcp.json</code>:</p>
<pre>{mcp_snippet}</pre>
<p><strong>Keep your token private.</strong> It grants full access to your Meridian projects.</p>
<p>Dashboard: <a href="{base}/dashboard">{base}/dashboard</a></p>
<p>Questions? Reply to this email.</p>"""

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

async def check_capacity(db: Any) -> dict[str, Any]:
    """Return Neon project usage stats and emit alerts when thresholds are hit.

    Sends an email to ADMIN_EMAIL when active tenants exceed 950.
    Raises RuntimeError (blocks provisioning) when count reaches 1000.
    """
    async with db.execute(
        "SELECT COUNT(*) as n FROM tenants WHERE neon_project_id IS NOT NULL"
    ) as cur:
        row = await cur.fetchone()
        if row is None:
            active = 0
        elif isinstance(row, dict):
            active = row.get("n", 0)
        else:
            active = row["n"] if hasattr(row, "keys") else (row[0] if row else 0)

    if active >= 1000:
        raise RuntimeError(
            f"Neon project cap reached ({active}/1000) — provisioning blocked"
        )

    if active >= 950:
        admin_email = _cfg("ADMIN_EMAIL")
        if admin_email:
            try:
                import httpx
                api_key = _cfg("RESEND_API_KEY")
                if api_key:
                    from_addr = _cfg("MERIDIAN_FROM_EMAIL", "Meridian <noreply@usemeridian.us>")
                    async with httpx.AsyncClient(timeout=10) as http:
                        await http.post(
                            "https://api.resend.com/emails",
                            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                            json={
                                "from": from_addr,
                                "to": [admin_email],
                                "subject": f"[Meridian] Capacity warning: {active}/1000 Neon projects",
                                "html": f"<p>Active Neon projects: <strong>{active}/1000</strong>. "
                                        f"Consider upgrading the Neon tier before hitting the cap.</p>",
                            },
                        )
            except Exception:  # noqa: BLE001
                pass  # best-effort alert

    return {"active": active, "cap": 1000, "warning": active >= 950}


# ---------------------------------------------------------------------------
# Churn cleanup
# ---------------------------------------------------------------------------

async def run_churn_cleanup(db: Any) -> None:
    """Warn churned tenants on day 3-7 and day 14; delete on day 28.

    A tenant is considered churned when their plan is 'free' and they have a
    neon_project_id (i.e. previously paid).  In practice you'll call this from
    a scheduled job or on server startup.
    """
    import httpx
    from datetime import datetime, timedelta, timezone

    now = datetime.now(tz=timezone.utc)
    api_key = _cfg("RESEND_API_KEY")
    from_addr = _cfg("MERIDIAN_FROM_EMAIL", "Meridian <noreply@usemeridian.us>")
    base = _cfg("MERIDIAN_BASE_URL", "https://usemeridian.us").rstrip("/")

    async with db.execute(
        "SELECT id, email, neon_project_id, created_at FROM tenants "
        "WHERE plan='free' AND neon_project_id IS NOT NULL"
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
