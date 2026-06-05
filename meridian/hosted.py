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


async def get_google_auth_url(next_url: str = "") -> str:
    """Return the Google OAuth authorization URL to redirect the user to."""
    import base64 as _b64
    client = _oauth_client()
    state_val = _b64.urlsafe_b64encode(next_url.encode()).decode() if next_url else ""
    url, _state = client.create_authorization_url(
        "https://accounts.google.com/o/oauth2/v2/auth",
        access_type="online",
        state=state_val or None,
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
# Microsoft OAuth helpers
# ---------------------------------------------------------------------------

MICROSOFT_CLIENT_ID = os.environ.get("MICROSOFT_CLIENT_ID", "")
MICROSOFT_CLIENT_SECRET = os.environ.get("MICROSOFT_CLIENT_SECRET", "")
_MICROSOFT_AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
_MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
_MICROSOFT_SCOPES = "openid email profile"


def _microsoft_callback_url() -> str:
    base = _cfg("MERIDIAN_BASE_URL", "http://localhost:7878").rstrip("/")
    return f"{base}/auth/microsoft/callback"


async def get_microsoft_auth_url() -> str:
    """Return the Microsoft OAuth authorization URL."""
    if not MICROSOFT_CLIENT_ID:
        raise RuntimeError("MICROSOFT_CLIENT_ID is not set")
    import urllib.parse
    params = urllib.parse.urlencode({
        "client_id": MICROSOFT_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _microsoft_callback_url(),
        "response_mode": "query",
        "scope": _MICROSOFT_SCOPES,
    })
    return f"{_MICROSOFT_AUTH_URL}?{params}"


async def exchange_microsoft_code_for_userinfo(code: str) -> dict[str, Any]:
    """Exchange a Microsoft OAuth code for user info (email + sub)."""
    import httpx

    if not MICROSOFT_CLIENT_ID or not MICROSOFT_CLIENT_SECRET:
        raise RuntimeError("MICROSOFT_CLIENT_ID / MICROSOFT_CLIENT_SECRET not set")

    async with httpx.AsyncClient() as http:
        token_resp = await http.post(
            _MICROSOFT_TOKEN_URL,
            data={
                "client_id": MICROSOFT_CLIENT_ID,
                "client_secret": MICROSOFT_CLIENT_SECRET,
                "code": code,
                "redirect_uri": _microsoft_callback_url(),
                "grant_type": "authorization_code",
            },
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
        access_token = token_data.get("access_token", "")
        if not access_token:
            raise RuntimeError(f"Microsoft token exchange failed: {token_data}")

        me_resp = await http.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        me_resp.raise_for_status()
        me = me_resp.json()

    email = me.get("mail") or me.get("userPrincipalName", "")
    sub = me.get("id", "")
    return {"email": email, "sub": sub}


# ---------------------------------------------------------------------------
# GitHub OAuth helpers
# ---------------------------------------------------------------------------

def _github_callback_url() -> str:
    """Return the absolute callback URL for GitHub OAuth."""
    base = _cfg("MERIDIAN_BASE_URL", "http://localhost:7878").rstrip("/")
    return f"{base}/auth/github/callback"


def _github_repo_callback_url() -> str:
    """Return the absolute callback URL for the GitHub repo-connect flow."""
    base = _cfg("MERIDIAN_BASE_URL", "http://localhost:7878").rstrip("/")
    return f"{base}/auth/github/callback"  # single registered callback URL


async def get_github_auth_url(
    scope: str = "user:email",
    *,
    state: str = "",
    redirect_uri: str | None = None,
) -> str:
    """Return the GitHub OAuth authorization URL."""
    from urllib.parse import urlencode

    client_id = _require_cfg("GITHUB_CLIENT_ID")
    callback = (redirect_uri or _github_callback_url()).rstrip("/")
    params: list[tuple[str, str]] = [
        ("scope", scope),
        ("client_id", client_id),
        ("redirect_uri", callback),
    ]
    if state:
        params.append(("state", state))
    return f"https://github.com/login/oauth/authorize?{urlencode(params)}"


async def _exchange_github_code_for_token(code: str, redirect_uri: str) -> str:
    """Exchange a GitHub OAuth code for an access token."""
    import httpx

    client_id = _require_cfg("GITHUB_CLIENT_ID")
    client_secret = _require_cfg("GITHUB_CLIENT_SECRET")

    async with httpx.AsyncClient() as http:
        token_resp = await http.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        if token_resp.status_code >= 400:
            raise RuntimeError(
                f"GitHub token exchange failed ({token_resp.status_code}): {token_resp.text}"
            )
        token_data = token_resp.json()
        access_token = token_data.get("access_token", "")
        if not access_token:
            raise RuntimeError(f"GitHub token exchange failed: {token_data}")

    return access_token


async def _github_user_snapshot(access_token: str) -> dict[str, Any]:
    """Return the current GitHub profile plus accessible repos."""
    import httpx

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "Meridian",
    }

    async with httpx.AsyncClient(timeout=15.0) as http:
        user_resp = await http.get("https://api.github.com/user", headers=headers)
        if user_resp.status_code >= 400:
            raise RuntimeError(
                f"GitHub user lookup failed ({user_resp.status_code}): {user_resp.text}"
            )
        user_data = user_resp.json()

        email = user_data.get("email")
        if not email:
            emails_resp = await http.get("https://api.github.com/user/emails", headers=headers)
            if emails_resp.status_code < 400:
                emails = emails_resp.json() or []
                if not isinstance(emails, list):
                    emails = []
                for e in emails:
                    if e.get("primary") and e.get("verified"):
                        email = e.get("email", "")
                        break
                if not email and emails:
                    email = emails[0].get("email", "")

        repos: list[dict[str, Any]] = []
        page = 1
        while True:
            repos_resp = await http.get(
                "https://api.github.com/user/repos",
                headers=headers,
                params={"sort": "updated", "per_page": "100", "page": str(page)},
            )
            if repos_resp.status_code >= 400:
                raise RuntimeError(
                    f"GitHub repo listing failed ({repos_resp.status_code}): {repos_resp.text}"
                )
            batch = repos_resp.json() or []
            if not isinstance(batch, list):
                break
            for repo in batch:
                owner = (repo.get("owner") or {}).get("login", "")
                name = repo.get("name", "")
                full_name = repo.get("full_name") or (f"{owner}/{name}" if owner and name else name)
                repos.append({
                    "full_name": full_name,
                    "name": name,
                    "owner": owner,
                    "html_url": repo.get("html_url", ""),
                    "default_branch": repo.get("default_branch") or "main",
                    "private": bool(repo.get("private")),
                    "updated_at": repo.get("updated_at", ""),
                })
            if len(batch) < 100:
                break
            page += 1

    return {
        "email": email,
        "sub": f"github:{user_data.get('id', '')}",
        "login": user_data.get("login", ""),
        "name": user_data.get("name") or user_data.get("login", ""),
        "avatar_url": user_data.get("avatar_url", ""),
        "repos": repos,
    }


async def exchange_github_code_for_userinfo(code: str) -> dict[str, Any]:
    """Exchange a GitHub OAuth code for user info (email + login)."""
    access_token = await _exchange_github_code_for_token(code, _github_callback_url())
    snapshot = await _github_user_snapshot(access_token)
    return {
        "email": snapshot.get("email", ""),
        "sub": snapshot.get("sub", ""),
        "login": snapshot.get("login", ""),
        "name": snapshot.get("name", ""),
        "avatar_url": snapshot.get("avatar_url", ""),
    }


async def exchange_github_repo_code_for_connection(code: str) -> dict[str, Any]:
    """Exchange a GitHub OAuth code for repo-connect data."""
    access_token = await _exchange_github_code_for_token(code, _github_repo_callback_url())
    snapshot = await _github_user_snapshot(access_token)
    return {
        "access_token": access_token,
        "login": snapshot.get("login", ""),
        "name": snapshot.get("name", ""),
        "avatar_url": snapshot.get("avatar_url", ""),
        "repos": snapshot.get("repos", []),
    }


# ---------------------------------------------------------------------------
# Route handlers (imported and registered by server.py)
# ---------------------------------------------------------------------------

_LOGIN_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sign in or create an account — Meridian</title>
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
.btn-microsoft{background:#2f2f2f;color:#fff}
.divider{display:flex;align-items:center;gap:12px;margin:18px 0;color:#8b8fa8;font-size:.78rem;text-transform:uppercase;letter-spacing:.6px}
.divider::before,.divider::after{content:"";flex:1;height:1px;background:#2a2d35}
.email-form{display:flex;flex-direction:column;gap:8px}
.email-input{width:100%;padding:12px 14px;background:#0d0d0f;border:1px solid #2a2d35;border-radius:8px;color:#e8eaf0;font-size:.95rem;outline:none;font-family:inherit}
.email-input:focus{border-color:#6c8fff}
.btn-email{background:#6c8fff;color:#fff;margin-bottom:0}
.btn-email:disabled{opacity:.6;cursor:wait}
.email-status{font-size:.82rem;color:#8b8fa8;margin-top:6px;min-height:1em;text-align:center}
.email-status.ok{color:#4ade80}
.email-status.err{color:#f87171}
.footer-note{text-align:center;font-size:.78rem;color:#8b8fa8;margin-top:24px}
.footer-note a{color:#6c8fff;text-decoration:none}
</style>
</head>
<body>
<div class="card">
  <div class="logo">⬡ <span>Meridian</span></div>
  <div class="subtitle">Sign in or create an account</div>
  <a href="/auth/google/login" class="btn btn-google">
    <svg width="20" height="20" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>
    Continue with Google
  </a>
  <a href="/auth/github/login" class="btn btn-github">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/></svg>
    Continue with GitHub
  </a>
<!-- MICROSOFT_BUTTON -->
  <div class="divider">or</div>
  <form class="email-form" id="magic-form" onsubmit="event.preventDefault();sendMagic();">
    <input type="email" class="email-input" id="magic-email" placeholder="you@example.com" autocomplete="email" required>
    <button type="submit" class="btn btn-email" id="magic-btn">Send magic link →</button>
    <div class="email-status" id="magic-status"></div>
  </form>
  <div class="footer-note">
    By signing in you agree to our <a href="/terms">Terms of Service</a>
    and <a href="/privacy">Privacy Policy</a>.
  </div>
</div>
<script>
async function sendMagic() {
  var status = document.getElementById('magic-status');
  var btn = document.getElementById('magic-btn');
  var email = (document.getElementById('magic-email').value || '').trim();
  if (!email) return;
  status.textContent = '';
  status.className = 'email-status';
  btn.disabled = true;
  btn.textContent = 'Sending…';
  try {
    var r = await fetch('/auth/magic', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: email}),
    });
    if (!r.ok) throw new Error('failed');
    var j = await r.json();
    status.className = 'email-status ok';
    status.textContent = j.message || 'Check your inbox.';
    if (j.dev_link) {
      // Local dev convenience — Resend not configured, surface the link.
      status.innerHTML += '<br><a href="' + j.dev_link + '" style="color:#6c8fff">[dev: open link]</a>';
    }
  } catch (e) {
    status.className = 'email-status err';
    status.textContent = 'Something went wrong. Try again in a moment.';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Send sign-in link →';
  }
}
</script>
</body>
</html>"""


def _build_login_page() -> str:
    """Build the login page HTML, injecting Microsoft button when configured."""
    ms_button = ""
    if MICROSOFT_CLIENT_ID:
        ms_button = """
  <a href="/auth/microsoft/login" class="btn btn-microsoft">
    <svg width="20" height="20" viewBox="0 0 21 21" fill="none"><rect x="1" y="1" width="9" height="9" fill="#f25022"/><rect x="11" y="1" width="9" height="9" fill="#7fba00"/><rect x="1" y="11" width="9" height="9" fill="#00a4ef"/><rect x="11" y="11" width="9" height="9" fill="#ffb900"/></svg>
    Continue with Microsoft
  </a>"""
    return _LOGIN_PAGE_HTML.replace("<!-- MICROSOFT_BUTTON -->", ms_button)


async def auth_login(request: Request):
    """Serve the sign-in page with Google and GitHub OAuth buttons."""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_build_login_page())


async def _post_login_redirect(tenant: dict, db=None, next_url: str = "") -> str:
    """v0.9 — paywall check shared by Google / GitHub / magic-link auth.

    Returns the target URL for the post-login redirect:
    * ``/waitlist-pending`` for non-admin users (pre-launch gate).
      Auto-adds the email to the waitlist table on first visit.
    * ``next_url`` if provided and safe (e.g. /oauth/authorize?... from MCP connector flow).
    * ``MERIDIAN_AFTER_LOGIN_URL`` (default ``/dashboard``) for admins.

    Keeps the auth callbacks symmetric — every login flow lands here so
    adding a new provider (magic link, Microsoft, SSO) inherits paywall.
    """
    # Admin bypass — respect next_url if present and safe, else dashboard
    if is_admin((tenant or {}).get("email", "")):
        if next_url and next_url.startswith("/") and not next_url.startswith("//"):
            return next_url
        return _cfg("MERIDIAN_AFTER_LOGIN_URL", "/dashboard")

    # MERIDIAN_LAUNCH_OPEN=true: admit the first N free-tier users directly.
    # Returning tenants keep access even after the launch cap is reached.
    if _cfg("MERIDIAN_LAUNCH_OPEN"):
        if db is not None:
            from . import db as db_module

            free_cap = int(_cfg("MERIDIAN_FREE_LAUNCH_CAP", "15") or "15")
            has_slot = bool(
                (tenant or {}).get("neon_project_id")
                or (tenant or {}).get("neon_db_url")
                or (tenant or {}).get("plan") in {"standard", "pro", "admin"}
            )
            if not has_slot:
                free_count = await db_module.count_tenants_by_plan(
                    db, "free", provisioned_only=True
                )
                if free_count >= free_cap:
                    email = (tenant or {}).get("email", "")
                    if email:
                        try:
                            await db_module.add_waitlist_entry(
                                db, email, note="auto:launch-full"
                            )
                        except Exception:
                            pass
                    return "/waitlist-pending?message=Early%20access%20is%20full"
                try:
                    tenant = await provision_neon_db(tenant["id"], db)
                except Exception:
                    pass
        if next_url and next_url.startswith("/") and not next_url.startswith("//"):
            return next_url
        return _cfg("MERIDIAN_AFTER_LOGIN_URL", "/dashboard")

    # Pre-launch: non-admin users are held at waitlist-pending.
    # Auto-add to waitlist table so admins can see who signed up.
    if db is not None:
        from . import db as db_module
        email = (tenant or {}).get("email", "")
        if email:
            try:
                await db_module.add_waitlist_entry(db, email, note="auto:login")
            except Exception:
                pass  # already on waitlist or DB error — not fatal
    return "/waitlist-pending"


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

    # Provision a Neon DB in the background for new tenants who don't have one yet.
    # Admin accounts and already-provisioned accounts are no-ops inside provision_neon_db.
    if not tenant.get("neon_project_id") and not tenant.get("neon_db_url"):
        import asyncio as _asyncio
        _asyncio.create_task(_provision_tenant_background(tenant["id"], db))

    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=_SESSION_MAX_AGE_HOURS)
    ).isoformat()
    session = await db_module.create_user_session(db, tenant["id"], expires_at)
    cookie_value = _make_session_cookie(session["id"])

    import base64 as _b64
    _raw_state = request.query_params.get("state", "")
    _next_url = ""
    if _raw_state:
        try:
            _next_url = _b64.urlsafe_b64decode(_raw_state + "==").decode()
        except Exception:
            _next_url = ""
    # Safety: only allow local paths
    if not (_next_url.startswith("/") and not _next_url.startswith("//")):
        _next_url = ""
    redirect_to = await _post_login_redirect(tenant, db, next_url=_next_url)
    response = RedirectResponse(redirect_to, status_code=302)
    response.set_cookie(
        _SESSION_COOKIE,
        cookie_value,
        httponly=True,
        secure=_cfg("MERIDIAN_BASE_URL", "").startswith("https://"),
        samesite="lax",
        max_age=_SESSION_MAX_AGE_HOURS * 3600,
    )
    response.delete_cookie("meridian_demo")
    return response


async def auth_google_login(request: Request) -> RedirectResponse:
    """Redirect the browser directly to Google's OAuth consent page."""
    next_url = request.query_params.get("next", "")
    try:
        url = await get_google_auth_url(next_url=next_url)
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
    """Handle GitHub OAuth callback — upsert tenant, set session cookie.
    Also handles repo-connect flow when state starts with 'repo:'."""
    # Delegate to repo-connect handler if this is a repo OAuth flow
    state = request.query_params.get("state", "")
    if state.startswith("repo:"):
        return await auth_github_repo_callback(request)

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
        from fastapi.responses import RedirectResponse as _Redirect
        return _Redirect("/auth/email-required?provider=github", status_code=302)

    db = request.app.state.db
    tenant = await db_module.upsert_tenant(db, email=email, github_sub=sub)

    # Provision a Neon DB in the background for new tenants who don't have one yet.
    if not tenant.get("neon_project_id") and not tenant.get("neon_db_url"):
        import asyncio as _asyncio
        _asyncio.create_task(_provision_tenant_background(tenant["id"], db))

    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=_SESSION_MAX_AGE_HOURS)
    ).isoformat()
    session = await db_module.create_user_session(db, tenant["id"], expires_at)
    cookie_value = _make_session_cookie(session["id"])

    redirect_to = await _post_login_redirect(tenant, db)
    response = RedirectResponse(redirect_to, status_code=302)
    response.set_cookie(
        _SESSION_COOKIE,
        cookie_value,
        httponly=True,
        secure=_cfg("MERIDIAN_BASE_URL", "").startswith("https://"),
        samesite="lax",
        max_age=_SESSION_MAX_AGE_HOURS * 3600,
    )
    response.delete_cookie("meridian_demo")
    return response


async def auth_github_repo_connect(request: Request) -> RedirectResponse:
    """Redirect the browser to GitHub's repo-connect OAuth consent page."""
    project_id = request.query_params.get("project_id", "").strip()
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    try:
        await get_current_tenant(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)
    try:
        url = await get_github_auth_url(
            scope="repo",
            state=f"repo:{project_id}",
            redirect_uri=_github_repo_callback_url(),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=302)


async def auth_github_repo_callback(request: Request) -> RedirectResponse:
    """Handle GitHub repo-connect callback and store the tenant token."""
    from . import db as db_module

    code = request.query_params.get("code", "").strip()
    _state = request.query_params.get("state", "").strip()
    project_id = _state.removeprefix("repo:") if _state.startswith("repo:") else _state
    if not code:
        raise HTTPException(status_code=400, detail="missing oauth code")
    if not project_id:
        raise HTTPException(status_code=400, detail="missing project_id state")

    try:
        tenant = await get_current_tenant(request)
    except HTTPException as exc:
        raise HTTPException(status_code=401, detail="not authenticated") from exc

    try:
        connection = await exchange_github_repo_code_for_connection(code)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub OAuth exchange failed: {exc}") from exc

    access_token = connection.get("access_token", "")
    repos = connection.get("repos") or []
    selected_repo = (tenant.get("github_repo") or "").strip()
    selected_branch = (tenant.get("github_branch") or "main").strip()
    repo_lookup = {repo.get("full_name", ""): repo for repo in repos if repo.get("full_name")}
    if selected_repo and selected_repo in repo_lookup:
        selected_branch = (selected_branch or repo_lookup[selected_repo].get("default_branch") or "main").strip()
    elif repos:
        first_repo = repos[0]
        selected_repo = (first_repo.get("full_name") or "").strip()
        selected_branch = (first_repo.get("default_branch") or "main").strip()

    await db_module.update_tenant(
        request.app.state.db,
        tenant["id"],
        github_pat=db_module.encrypt_field(access_token),
        github_repo=selected_repo or None,
        github_branch=selected_branch or "main",
    )

    return RedirectResponse(
        f"/dashboard?project_id={project_id}&tab=settings",
        status_code=302,
    )


async def auth_microsoft_login(request: Request) -> RedirectResponse:
    """Redirect the browser to Microsoft's OAuth consent page."""
    try:
        url = await get_microsoft_auth_url()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=302)


async def auth_microsoft_callback(request: Request) -> RedirectResponse:
    """Handle Microsoft OAuth callback — upsert tenant, set session cookie."""
    from . import db as db_module

    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="missing oauth code")

    try:
        userinfo = await exchange_microsoft_code_for_userinfo(code)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Microsoft OAuth exchange failed: {exc}") from exc

    email: str = userinfo.get("email", "")
    sub: str = userinfo.get("sub", "")
    if not email:
        raise HTTPException(status_code=400, detail="no email in Microsoft profile")

    db = request.app.state.db
    tenant = await db_module.upsert_tenant(db, email=email, microsoft_sub=sub)

    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=_SESSION_MAX_AGE_HOURS)
    ).isoformat()
    session = await db_module.create_user_session(db, tenant["id"], expires_at)
    cookie_value = _make_session_cookie(session["id"])

    redirect_to = await _post_login_redirect(tenant, db)
    response = RedirectResponse(redirect_to, status_code=302)
    response.set_cookie(
        _SESSION_COOKIE,
        cookie_value,
        httponly=True,
        secure=_cfg("MERIDIAN_BASE_URL", "").startswith("https://"),
        samesite="lax",
        max_age=_SESSION_MAX_AGE_HOURS * 3600,
    )
    response.delete_cookie("meridian_demo")
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
    response.delete_cookie("meridian_demo")
    return response


# ---------------------------------------------------------------------------
# Admin helpers
# ---------------------------------------------------------------------------

_ADMIN_COOKIE = "meridian_admin"


def is_admin(email: str) -> bool:
    """Return True if email is in the MERIDIAN_ADMIN_EMAILS whitelist (env var fallback)."""
    whitelist_raw = os.environ.get("MERIDIAN_ADMIN_EMAILS", os.environ.get("ADMIN_EMAIL", ""))
    if not whitelist_raw:
        return False
    admins = {e.strip().lower() for e in whitelist_raw.split(",") if e.strip()}
    return email.lower() in admins


async def is_admin_db(email: str, db: Any) -> bool:
    """Return True if email is in the admins DB table, or the env var fallback."""
    try:
        rows = await db.fetchall(
            "SELECT id FROM admins WHERE email = %s", (email.lower(),)
        )
        if rows:
            return True
    except Exception:  # noqa: BLE001 — table may not exist on older DBs
        pass
    return is_admin(email)


def check_admin_password(request: Request) -> bool:
    """Return True if the admin password cookie matches MERIDIAN_ADMIN_PASSWORD."""
    import secrets
    pwd = os.environ.get("MERIDIAN_ADMIN_PASSWORD", "")
    if not pwd:
        return True  # no password set — open (still protected by email whitelist)
    cookie_val = request.cookies.get(_ADMIN_COOKIE, "")
    return bool(cookie_val) and secrets.compare_digest(cookie_val, pwd)


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


async def _set_neon_pitr(api_key: str, neon_project_id: str, retention_seconds: int) -> None:
    """Set point-in-time recovery retention on a Neon project. Idempotent."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.patch(
            f"https://console.neon.tech/api/v2/projects/{neon_project_id}",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"project": {"history_retention_seconds": retention_seconds}},
        )
        resp.raise_for_status()


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
    import asyncio as _asyncio
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
        resp = None
        for attempt in range(10):
            resp = await http.post(
                f"https://console.neon.tech/api/v2/projects/{neon_project_id}/branches/{branch_id}/databases",
                headers=headers,
                json={"database": {"name": db_name, "owner_name": "neondb_owner"}},
            )
            if resp.status_code == 409:
                break
            if resp.status_code != 423:
                break
            await _asyncio.sleep(2)
        assert resp is not None
        if resp.status_code != 409:
            resp.raise_for_status()

    # Return connection URI for this database
    async with httpx.AsyncClient(timeout=15) as http:
        uri_resp = await http.get(
            f"https://console.neon.tech/api/v2/projects/{neon_project_id}/connection_uri"
            f"?branch_id={branch_id}&database_name={db_name}&role_name=neondb_owner",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        uri_resp.raise_for_status()
        return uri_resp.json().get("uri", "")


# ---------------------------------------------------------------------------
# Stripe Checkout
# ---------------------------------------------------------------------------

STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")            # standard tier
STRIPE_PRO_PRICE_ID = os.environ.get("STRIPE_PRO_PRICE_ID", "")    # pro tier
STRIPE_OVERAGE_PRICE_ID = os.environ.get("STRIPE_OVERAGE_PRICE_ID", "")  # metered storage overage


async def create_stripe_checkout_session(tenant: dict, plan: str) -> str:
    """Create a Stripe Checkout Session for the given plan.

    Returns the checkout URL to redirect the user to. Raises RuntimeError
    when the required price ID env var is not configured.
    """
    import stripe  # type: ignore[import]

    stripe.api_key = _require_cfg("STRIPE_API_KEY")
    base = _cfg("MERIDIAN_BASE_URL", "http://localhost:7878").rstrip("/")

    price_id = STRIPE_PRO_PRICE_ID if plan == "pro" else STRIPE_PRICE_ID
    if not price_id:
        key = "STRIPE_PRO_PRICE_ID" if plan == "pro" else "STRIPE_PRICE_ID"
        raise RuntimeError(f"{key} is not configured")

    line_items: list[dict] = [{"price": price_id, "quantity": 1}]
    if STRIPE_OVERAGE_PRICE_ID:
        # Meter-based price — no quantity; Stripe bills via MeterEvents
        line_items.append({"price": STRIPE_OVERAGE_PRICE_ID})

    params: dict = {
        "mode": "subscription",
        "payment_method_collection": "always",
        "subscription_data": {},
        "line_items": line_items,
        "metadata": {"plan": plan, "tenant_id": tenant.get("id", "")},
        "success_url": f"{base}/auth/success",
        "cancel_url": f"{base}/pricing",
    }
    customer_id = tenant.get("stripe_customer_id")
    if customer_id:
        params["customer"] = customer_id
    else:
        params["customer_email"] = tenant.get("email", "")

    session = stripe.checkout.Session.create(**params)
    return session.url


async def _provision_tenant_background(tenant_id: str, db: Any) -> None:
    """Best-effort background provisioning called from OAuth callbacks.

    Silently swallows errors — login must never fail due to provisioning issues.
    Logs failures at WARNING level for ops visibility.
    """
    import logging as _logging
    try:
        await provision_neon_db(tenant_id, db)
    except Exception as exc:  # noqa: BLE001
        _logging.getLogger(__name__).warning(
            "Background Neon provisioning failed for tenant %s: %s", tenant_id, exc
        )


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

    if tenant.get("plan") == "admin":
        return tenant  # admin accounts use manually-assigned DBs, never auto-provisioned

    tier = tenant.get("plan") or "standard"
    # Treat free-tier as standard for pool allocation (same API key, smaller quota pool)
    pool_tier = "free" if tier == "free" else tier
    api_key = _neon_api_key_for_tier("standard" if tier == "free" else tier)

    # Capacity check
    await check_capacity(db)

    # Find or create a pool project with room
    pool = await db_module.get_available_pool_project(
        db, tier=pool_tier, max_customers=_MAX_CUSTOMERS_PER_PROJECT
    )

    if pool is None:
        # All existing pool projects are full — create a new one
        neon_project_id, _first_conn_uri = await _create_neon_pool_project(api_key, pool_tier)
        pool = await db_module.register_pool_project(db, neon_project_id, pool_tier)
    else:
        neon_project_id = pool["neon_project_id"]

    # Create a customer-specific database inside the pool project
    email_slug = tenant["email"].split("@")[0][:20].replace(".", "_")
    db_name = f"cust_{email_slug}_{tenant_id[:8]}"
    conn_uri = await _create_customer_database(api_key, neon_project_id, db_name)

    if not conn_uri:
        raise RuntimeError(f"Failed to get connection URI for customer database {db_name!r}")

    # Persist on tenant — encrypt the connection string at rest.
    updated = await db_module.update_tenant(
        db,
        tenant_id,
        neon_project_id=neon_project_id,
        pool_project_id=pool["id"],
        neon_db_url=db_module.encrypt_field(conn_uri),
    )

    # Increment pool project customer count
    await db_module.increment_pool_project_count(db, neon_project_id)

    # v1.0 — set PITR retention on the pool project based on plan tier.
    # Pro: 7 days, Standard: 1 day (Neon default). Best-effort — never
    # block provisioning if the PATCH fails.
    try:
        retention_s = 604800 if tier == "pro" else 86400
        await _set_neon_pitr(api_key, neon_project_id, retention_s)
    except Exception:  # noqa: BLE001
        pass

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
<p>Docs: <a href="https://docs.usemeridian.us">docs.usemeridian.us</a></p>
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


async def send_waitlist_confirmation_email(email: str) -> None:
    """Send a confirmation email to a new waitlist signup via Resend.

    Silently skips if RESEND_API_KEY is not set (dev mode).
    """
    import httpx

    api_key = _cfg("RESEND_API_KEY")
    if not api_key:
        return

    from_addr = _cfg("MERIDIAN_FROM_EMAIL", "Meridian <noreply@usemeridian.us>")
    base = _cfg("MERIDIAN_BASE_URL", "https://usemeridian.us").rstrip("/")

    html_body = f"""<h2>You're on the Meridian waitlist</h2>
<p>Thanks for signing up! We'll reach out when your hosted account is ready.</p>
<p>While you wait:</p>
<ul>
  <li>Try the <a href="{base}/demo">live demo</a> — no account needed</li>
  <li>Self-host Meridian in 2 commands: <code>git clone https://github.com/meridianmcp/Meridian && cd Meridian && pixi run start</code></li>
  <li>Star us on <a href="https://github.com/meridianmcp/Meridian">GitHub</a></li>
</ul>
<p>Questions? Reply to this email or open a <a href="https://github.com/meridianmcp/Meridian/issues">GitHub issue</a>.</p>"""

    async with httpx.AsyncClient(timeout=15) as http:
        try:
            resp = await http.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "from": from_addr,
                    "to": [email],
                    "subject": "You're on the Meridian waitlist",
                    "html": html_body,
                },
            )
            resp.raise_for_status()
        except Exception:
            pass  # never block the waitlist endpoint on email failure


async def send_invite_email(
    email: str,
    invite_url: str,
    inviter_email: str,
) -> None:
    """Send a workspace invite email via Resend. Silently skips in dev mode."""
    import httpx

    api_key = _cfg("RESEND_API_KEY")
    if not api_key:
        return

    from_addr = _cfg("MERIDIAN_FROM_EMAIL", "Meridian <noreply@usemeridian.us>")
    html_body = f"""<h2>You've been invited to Meridian</h2>
<p><strong>{inviter_email}</strong> invited you to collaborate on Meridian — shared memory for AI coding sessions.</p>
<p style="margin:24px 0">
  <a href="{invite_url}" style="background:#6c8fff;color:#fff;padding:12px 24px;text-decoration:none;border-radius:6px;font-weight:700;display:inline-block">Accept invitation →</a>
</p>
<p style="color:#8b8fa8;font-size:.9em">Or copy this link: <code>{invite_url}</code></p>
<p style="color:#8b8fa8;font-size:.9em">This link expires in 7 days. If you didn't expect this, you can ignore it.</p>"""

    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "from": from_addr,
                "to": [email],
                "subject": f"{inviter_email} invited you to Meridian",
                "html": html_body,
            },
        )
        resp.raise_for_status()


async def cancel_stripe_subscription(stripe_customer_id: str) -> None:
    """Cancel all active Stripe subscriptions for a customer immediately. Best-effort."""
    api_key = _cfg("STRIPE_API_KEY", "")
    if not api_key:
        return
    try:
        import stripe  # type: ignore[import]
        stripe.api_key = api_key
        subs = stripe.Subscription.list(customer=stripe_customer_id, status="active", limit=10)
        for sub in subs.auto_paging_iter():
            stripe.Subscription.cancel(sub.id)
    except Exception:
        pass


async def _drop_tenant_neon_database(tenant: dict[str, Any]) -> None:
    """Drop the customer's database within their pool Neon project. Best-effort."""
    import httpx
    neon_project_id = tenant.get("neon_project_id")
    if not neon_project_id:
        return
    plan = tenant.get("plan", "standard")
    try:
        api_key = _neon_api_key_for_tier(plan)
    except RuntimeError:
        api_key = _cfg("NEON_API_KEY") or ""
    if not api_key:
        return
    email_slug = (tenant.get("email") or "x").split("@")[0][:20].replace(".", "_")
    tenant_id = tenant.get("id", "")
    db_name = f"cust_{email_slug}_{tenant_id[:8]}"
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            br = await http.get(
                f"https://console.neon.tech/api/v2/projects/{neon_project_id}/branches",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            branches = br.json().get("branches", [])
            branch_id = next((b["id"] for b in branches if b.get("default")), None)
            if not branch_id and branches:
                branch_id = branches[0]["id"]
            if not branch_id:
                return
            await http.delete(
                f"https://console.neon.tech/api/v2/projects/{neon_project_id}/branches/{branch_id}/databases/{db_name}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except Exception:
        pass


async def send_account_deleted_email(email: str) -> None:
    """Send account deletion confirmation via Resend. Silently skips in dev."""
    api_key = _cfg("RESEND_API_KEY")
    if not api_key:
        return
    from_addr = _cfg("MERIDIAN_FROM_EMAIL", "Meridian <noreply@usemeridian.us>")
    base = _cfg("MERIDIAN_BASE_URL", "https://usemeridian.us").rstrip("/")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "from": from_addr,
                    "to": [email],
                    "subject": "Your Meridian account has been deleted",
                    "html": (
                        "<p>Your Meridian account and all associated data have been permanently deleted.</p>"
                        "<p>Thank you for using Meridian. If you'd like to start again, you're always welcome back at "
                        f'<a href="{base}">{base}</a>.</p>'
                    ),
                },
            )
    except Exception:
        pass


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
# Storage overage billing
# ---------------------------------------------------------------------------

_PLAN_STORAGE_LIMIT_GB = {"standard": 1.0, "pro": 10.0}


async def get_neon_storage_gb(neon_project_id: str, neon_api_key: str) -> float:
    """Return current storage for a Neon project in GB.

    Uses ``data_storage_bytes_hour`` from the Neon project detail endpoint.
    Returns 0.0 on any error so callers can degrade gracefully.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(
                f"https://console.neon.tech/api/v2/projects/{neon_project_id}",
                headers={"Authorization": f"Bearer {neon_api_key}"},
            )
            resp.raise_for_status()
            data = resp.json().get("project", {})
            bytes_hour = float(data.get("data_storage_bytes_hour", 0) or 0)
            # bytes_hour is cumulative GB-hours; treat as proxy for GB
            return bytes_hour / 1e9
    except Exception:  # noqa: BLE001
        return 0.0


async def report_stripe_overage(
    stripe_customer_id: str,
    overage_gb: float,
    stripe_api_key: str,
) -> None:
    """Report storage overage to Stripe via billing meter events (best-effort).

    Uses the v2025 MeterEvent API — event_name matches the meter created
    in the one-time setup script (storage_overage_gb).  Customer mapping
    is by_id using the stripe_customer_id payload key.
    """
    import stripe  # type: ignore[import]
    from datetime import datetime, timezone as _tz

    stripe.api_key = stripe_api_key
    try:
        stripe.billing.MeterEvent.create(
            event_name="storage_overage_gb",
            payload={
                "value": str(round(overage_gb, 3)),
                "stripe_customer_id": stripe_customer_id,
            },
            timestamp=int(datetime.now(_tz.utc).timestamp()),
        )
    except Exception:  # noqa: BLE001
        pass


async def run_storage_overage_check(db: Any) -> None:
    """Check storage usage for all active tenants, log warnings, report to Stripe.

    Called hourly from the background loop. Uses NEON_API_KEY / NEON_API_KEY_PRO
    for storage reads and STRIPE_API_KEY for usage reporting.
    """
    stripe_api_key = _cfg("STRIPE_API_KEY", "")

    async with db.execute(
        "SELECT id, email, plan, neon_project_id, stripe_customer_id, stripe_metered_item_id "
        "FROM tenants WHERE neon_project_id IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()

    def _to_d(r: Any) -> dict:
        return r if isinstance(r, dict) else {k: r[k] for k in r.keys()}

    tenants = [_to_d(r) for r in rows] if rows else []

    for tenant in tenants:
        neon_project_id = tenant.get("neon_project_id")
        if not neon_project_id:
            continue
        plan = tenant.get("plan") or "standard"
        api_key = _neon_api_key_for_tier(plan)
        if not api_key:
            continue

        usage_gb = await get_neon_storage_gb(neon_project_id, api_key)
        limit_gb = _PLAN_STORAGE_LIMIT_GB.get(plan, 1.0)

        if usage_gb > limit_gb:
            overage_gb = usage_gb - limit_gb
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Storage overage: tenant=%s plan=%s usage=%.2f GB limit=%.1f GB overage=%.2f GB",
                tenant["email"], plan, usage_gb, limit_gb, overage_gb,
            )

            stripe_customer_id = tenant.get("stripe_customer_id")
            if stripe_api_key and stripe_customer_id:
                await report_stripe_overage(stripe_customer_id, overage_gb, stripe_api_key)


# ---------------------------------------------------------------------------
# Compute + storage overage billing
# ---------------------------------------------------------------------------

PLAN_LIMITS: dict[str, dict[str, float]] = {
    "admin":    {"cu_hours": float("inf"), "grace_cu_hours": float("inf"), "storage_gb": float("inf")},
    "standard": {"cu_hours": 50.0,  "grace_cu_hours": 20.0, "storage_gb": 1.0},
    "pro":      {"cu_hours": 200.0, "grace_cu_hours": 20.0, "storage_gb": 10.0},
    "free":     {"cu_hours": 10.0,  "grace_cu_hours": 5.0,  "storage_gb": 0.1},
}
COMPUTE_OVERAGE_RATE = 0.16   # $/CU-hour
STORAGE_OVERAGE_RATE = 0.50   # $/GB-month

# Never poll these — infrastructure / demo projects
EXCLUDED_NEON_PROJECTS: frozenset[str] = frozenset({
    "muddy-queen-15422822",   # auth / main meridian DB
    "blue-smoke-62506461",    # seeded demo DB
})


def _admin_emails() -> frozenset[str]:
    """Return the set of admin email addresses (from env)."""
    raw = os.environ.get("MERIDIAN_ADMIN_EMAILS", "") or os.environ.get("ADMIN_EMAIL", "")
    return frozenset(e.strip() for e in raw.split(",") if e.strip())


async def _fetch_neon_consumption(
    project_id: str,
    api_key: str,
    from_dt: str,
    to_dt: str,
) -> dict[str, Any]:
    """Fetch raw consumption data from Neon API for a billing window.

    Returns the raw response dict, or {} on any error.
    Query params ``from`` / ``to`` are ISO 8601 datetime strings.
    """
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(
                f"https://console.neon.tech/api/v2/projects/{project_id}/consumption",
                headers={"Authorization": f"Bearer {api_key}"},
                params={"from": from_dt, "to": to_dt},
            )
            if resp.status_code != 200:
                return {}
            return resp.json()
    except Exception:  # noqa: BLE001
        return {}


def _parse_consumption_metrics(data: dict[str, Any]) -> dict[str, float]:
    """Flatten Neon consumption API response into a {metric_name: total} dict.

    Handles the nested: periods[] → consumption[] → metrics[] shape.
    Sums across all periods and timeframes in the response.
    """
    totals: dict[str, float] = {}
    # Single-project endpoint returns the project object directly;
    # bulk endpoint wraps in projects[]. Handle both.
    candidates = data.get("projects", [data])
    for project in candidates:
        for period in project.get("periods", []):
            for timeframe in period.get("consumption", []):
                for metric in timeframe.get("metrics", []):
                    name = metric.get("metric_name", "")
                    value = float(metric.get("value") or 0)
                    totals[name] = totals.get(name, 0.0) + value
    return totals


def _metrics_to_cu_hours(metrics: dict[str, float]) -> float:
    """Convert Neon compute_unit_seconds to CU-hours."""
    return metrics.get("compute_unit_seconds", 0.0) / 3600.0


def _metrics_to_storage_gb(metrics: dict[str, float]) -> float:
    """Convert Neon root_branch_bytes_month to GB-month equivalent."""
    return metrics.get("root_branch_bytes_month", 0.0) / 1e9


async def _set_neon_max_cu(project_id: str, api_key: str, max_cu: float) -> None:
    """Set Neon project autoscaling max compute units. Best-effort."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            await http.patch(
                f"https://console.neon.tech/api/v2/projects/{project_id}",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"project": {"autoscaling_limit_max_cu": max_cu}},
            )
    except Exception:  # noqa: BLE001
        pass


async def _send_overage_email(
    email: str,
    subject: str,
    html: str,
) -> None:
    """Send a single overage/limit notification email. Best-effort."""
    import httpx
    api_key = _cfg("RESEND_API_KEY")
    if not api_key:
        return
    from_addr = _cfg("MERIDIAN_FROM_EMAIL", "Meridian <noreply@usemeridian.us>")
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"from": from_addr, "to": [email], "subject": subject, "html": html},
            )
    except Exception:  # noqa: BLE001
        pass


async def run_overage_check(db: Any) -> None:
    """Daily job: poll Neon compute + storage usage, send warnings, bill overages.

    Timeline per tenant:
    - compute < limit         → no action
    - limit ≤ compute < grace → send one warning email
    - compute ≥ grace         → if overage cap set: bill via Stripe meter event
                                else: throttle Neon compute to 0.25 CU max
    - storage > limit         → bill via Stripe meter event if cap set, else email

    Excluded: EXCLUDED_NEON_PROJECTS, admin emails, tenants with no stripe_customer_id.
    Reset: columns are reset to 0 each month via overage_reset_at.
    """
    from datetime import datetime, timezone as _tz, timedelta as _td
    from . import db as db_module

    admins = _admin_emails()
    now = datetime.now(tz=_tz.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    now_iso = now.isoformat()
    stripe_api_key = _cfg("STRIPE_API_KEY", "")
    base = _cfg("MERIDIAN_BASE_URL", "https://usemeridian.us").rstrip("/")

    tenants = await db_module.list_tenants_with_neon(db)

    for tenant in tenants:
        email = tenant.get("email", "")
        if email in admins:
            continue

        neon_project_id = tenant.get("neon_project_id", "")
        if not neon_project_id or neon_project_id in EXCLUDED_NEON_PROJECTS:
            continue

        plan = tenant.get("plan") or "standard"
        limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])

        try:
            api_key = _neon_api_key_for_tier(plan)
        except RuntimeError:
            api_key = _cfg("NEON_API_KEY") or ""
        if not api_key:
            continue

        # Monthly reset: if overage_reset_at is from a previous month, zero out
        reset_at_raw = tenant.get("overage_reset_at") or ""
        if reset_at_raw:
            try:
                last_reset = datetime.fromisoformat(reset_at_raw.replace("Z", "+00:00"))
                if last_reset.year < now.year or last_reset.month < now.month:
                    await db_module.update_tenant(
                        db, tenant["id"],
                        compute_cu_hours_used=0.0,
                        storage_gb_used=0.0,
                        overage_reset_at=now_iso,
                        compute_throttled_at=None,
                    )
            except (ValueError, AttributeError):
                pass

        # Fetch Neon consumption for current billing month
        raw = await _fetch_neon_consumption(neon_project_id, api_key, month_start, now_iso)
        if not raw:
            continue

        metrics = _parse_consumption_metrics(raw)
        cu_used = _metrics_to_cu_hours(metrics)
        gb_used = _metrics_to_storage_gb(metrics)

        # Persist latest usage
        await db_module.update_tenant(db, tenant["id"],
            compute_cu_hours_used=round(cu_used, 4),
            storage_gb_used=round(gb_used, 4),
        )
        if not tenant.get("overage_reset_at"):
            await db_module.update_tenant(db, tenant["id"], overage_reset_at=now_iso)

        cu_limit = limits["cu_hours"]
        cu_grace = cu_limit + limits["grace_cu_hours"]
        gb_limit = limits["storage_gb"]
        stripe_id = tenant.get("stripe_customer_id")

        # --- Compute checks ---
        if cu_used >= cu_grace:
            compute_cap = float(tenant.get("compute_overage_cap_usd") or 0)
            overage_hours = cu_used - cu_limit
            charge = overage_hours * COMPUTE_OVERAGE_RATE

            if compute_cap > 0 and charge <= compute_cap and stripe_id and stripe_api_key:
                # Bill via Stripe meter event
                try:
                    import stripe as _stripe
                    _stripe.api_key = stripe_api_key
                    _stripe.billing.MeterEvent.create(
                        event_name="compute_overage_cu_hours",
                        payload={"value": str(round(overage_hours, 4)), "stripe_customer_id": stripe_id},
                        timestamp=int(now.timestamp()),
                    )
                except Exception:  # noqa: BLE001
                    pass
            elif not tenant.get("compute_throttled_at"):
                # Throttle compute to 0.25 CU and email
                await _set_neon_max_cu(neon_project_id, api_key, 0.25)
                await db_module.update_tenant(db, tenant["id"], compute_throttled_at=now_iso)
                await _send_overage_email(
                    email,
                    subject="Meridian: compute limit reached — sessions throttled",
                    html=(
                        f"<p>Your Meridian project has used <strong>{cu_used:.1f} CU-hours</strong> "
                        f"this month (limit: {cu_limit:.0f} + {limits['grace_cu_hours']:.0f} grace).</p>"
                        "<p>Compute has been throttled to 0.25 CU until next month or you set an overage budget.</p>"
                        f"<p><a href='{base}/dashboard'>Set an overage budget →</a></p>"
                    ),
                )

        elif cu_used >= cu_limit and not tenant.get("compute_throttled_at"):
            # Grace period — send one warning
            remaining = cu_grace - cu_used
            await _send_overage_email(
                email,
                subject="Meridian: compute approaching limit",
                html=(
                    f"<p>You've used <strong>{cu_used:.1f} of {cu_limit:.0f} CU-hours</strong> "
                    f"this month. You have {remaining:.1f} grace hours remaining before throttling.</p>"
                    f"<p><a href='{base}/dashboard'>Set an overage budget to avoid throttling →</a></p>"
                ),
            )

        # --- Storage checks ---
        if gb_used > gb_limit:
            storage_cap = float(tenant.get("storage_overage_cap_usd") or 0)
            overage_gb = gb_used - gb_limit
            charge = overage_gb * STORAGE_OVERAGE_RATE

            if storage_cap > 0 and charge <= storage_cap and stripe_id and stripe_api_key:
                await report_stripe_overage(stripe_id, overage_gb, stripe_api_key)
            else:
                await _send_overage_email(
                    email,
                    subject="Meridian: storage limit exceeded",
                    html=(
                        f"<p>Your Meridian storage is at <strong>{gb_used:.2f} GB</strong> "
                        f"(limit: {gb_limit:.1f} GB, overage: {overage_gb:.2f} GB).</p>"
                        f"<p>Overage rate is ${STORAGE_OVERAGE_RATE}/GB-month. "
                        f"<a href='{base}/dashboard'>Set an overage budget →</a></p>"
                    ),
                )


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


# ---------------------------------------------------------------------------
# Dunning flow — payment failure → warnings → hard delete
# ---------------------------------------------------------------------------

async def run_dunning_cleanup(db: Any) -> None:
    """Warn failing-payment tenants on day 3 and day 7; hard delete on day 15.

    Timeline (from payment_failed_at):
      day 0  — Stripe fires invoice.payment_failed → payment_failed_at stamped
      day 3  — first warning email  (dunning_email_sent = 1)
      day 7  — final warning email  (dunning_email_sent = 2)
      day 15 — cancel Stripe, delete all tenant data

    Idempotent: dunning_email_sent tracks which emails have been sent.
    A successful payment (invoice.paid) clears payment_failed_at and resets.
    """
    from datetime import datetime, timezone as _tz, timedelta as _td
    import httpx

    from . import db as db_module

    now = datetime.now(tz=_tz.utc)
    resend_key = _cfg("RESEND_API_KEY")
    from_addr = _cfg("MERIDIAN_FROM_EMAIL", "Meridian <noreply@usemeridian.us>")
    base = _cfg("MERIDIAN_BASE_URL", "https://usemeridian.us").rstrip("/")

    tenants = await db_module.get_tenants_with_payment_failures(db)

    for tenant in tenants:
        raw_ts = tenant.get("payment_failed_at") or ""
        if not raw_ts:
            continue
        try:
            failed_at = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        days = (now - failed_at).days
        email = tenant.get("email", "")
        sent = int(tenant.get("dunning_email_sent") or 0)
        tenant_id = tenant["id"]

        if days >= 15:
            # Hard delete — cancel Stripe, wipe data, send confirmation
            stripe_id = tenant.get("stripe_customer_id")
            if stripe_id:
                await cancel_stripe_subscription(stripe_id)
            if tenant.get("neon_project_id"):
                await _drop_tenant_neon_database(tenant)
            await db_module.delete_tenant_records(db, tenant_id)
            if email:
                try:
                    async with httpx.AsyncClient(timeout=10) as http:
                        await http.post(
                            "https://api.resend.com/emails",
                            headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                            json={
                                "from": from_addr,
                                "to": [email],
                                "subject": "Your Meridian account has been closed",
                                "html": (
                                    "<p>Your Meridian account has been closed due to a billing issue. "
                                    "All data has been permanently deleted.</p>"
                                    f"<p>To start a new account: <a href='{base}/auth/login'>{base}</a></p>"
                                ),
                            },
                        )
                except Exception:  # noqa: BLE001
                    pass

        elif days >= 7 and sent < 2 and resend_key:
            # Day 7 — final warning
            try:
                async with httpx.AsyncClient(timeout=10) as http:
                    await http.post(
                        "https://api.resend.com/emails",
                        headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                        json={
                            "from": from_addr,
                            "to": [email],
                            "subject": "Action required: Meridian payment failed — account closes in 8 days",
                            "html": (
                                "<p><strong>Your Meridian payment has failed.</strong> "
                                "Your account and all data will be permanently deleted in 8 days "
                                "unless your payment method is updated.</p>"
                                f"<p><a href='{base}/auth/login'>Update payment →</a></p>"
                                "<p>If you no longer want your account, no action needed — "
                                "it will be automatically closed.</p>"
                            ),
                        },
                    )
                await db_module.update_tenant(db, tenant_id, dunning_email_sent=2)
            except Exception:  # noqa: BLE001
                pass

        elif days >= 3 and sent < 1 and resend_key:
            # Day 3 — first warning
            try:
                async with httpx.AsyncClient(timeout=10) as http:
                    await http.post(
                        "https://api.resend.com/emails",
                        headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                        json={
                            "from": from_addr,
                            "to": [email],
                            "subject": "Meridian payment failed — please update your billing info",
                            "html": (
                                "<p>We couldn't process your Meridian payment. "
                                "Please update your billing information to keep your account active.</p>"
                                f"<p><a href='{base}/auth/login'>Update payment →</a></p>"
                                "<p>Your account and data are safe for now. "
                                "If we can't collect payment within 12 days, your account will be closed.</p>"
                            ),
                        },
                    )
                await db_module.update_tenant(db, tenant_id, dunning_email_sent=1)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# v0.9 — Email magic-link authentication
# ---------------------------------------------------------------------------

# Per-email cooldown to stop "click resend N times" abuse. The endpoint-
# level slowapi limit handles IP-based abuse; this catches single-IP +
# single-email burst patterns (e.g. user accidentally double-tapping).
_MAGIC_COOLDOWN_SECONDS = 60

# Token lifetime — long enough that users coming back to their inbox the
# next day still get a working link, short enough that lost emails don't
# linger forever.
_MAGIC_TTL_HOURS = 24

# In-process rate limit fallback when slowapi isn't initialised (tests).
_magic_last_send: dict[str, float] = {}


def _magic_rate_limited(email: str) -> bool:
    """Return True if this email got a magic link in the last cooldown
    window. Keeps the in-process cache bounded by pruning stale entries
    on every call."""
    import time
    now = time.time()
    # Prune entries older than the cooldown so the dict doesn't grow.
    for k, ts in list(_magic_last_send.items()):
        if now - ts > _MAGIC_COOLDOWN_SECONDS * 4:
            _magic_last_send.pop(k, None)
    last = _magic_last_send.get(email.lower())
    if last is not None and (now - last) < _MAGIC_COOLDOWN_SECONDS:
        return True
    _magic_last_send[email.lower()] = now
    return False


async def auth_magic_request(request: Request):
    """v0.9 — POST /auth/magic.

    Body: ``{"email": "..."}``. Sends a single-use magic link to the
    supplied email via Resend. Idempotent within the active-token
    window: if a valid unused token already exists, returns success
    without emailing a duplicate (so spam-clicking "resend" doesn't
    spray the inbox).
    """
    import secrets
    from fastapi.responses import JSONResponse
    from . import db as db_module

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email or len(email) > 320:
        raise HTTPException(status_code=400, detail="valid email required")

    if _magic_rate_limited(email):
        # Always return 200 — don't leak whether the email is registered
        # or whether they're rate-limited.
        return JSONResponse(
            {"status": "ok", "message": "check your inbox"}
        )

    db = request.app.state.db
    # If a fresh unused token exists, reuse the side-effect (email is
    # already in their inbox) — don't insert another row.
    existing = await db_module.get_active_magic_token(db, email)
    if existing is not None:
        return JSONResponse({"status": "ok", "message": "check your inbox"})

    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=_MAGIC_TTL_HOURS)
    ).strftime("%Y-%m-%d %H:%M:%S")
    await db_module.store_magic_token(db, email, token_hash, expires_at)

    base = _cfg("MERIDIAN_BASE_URL", "http://localhost:7878") or "http://localhost:7878"
    link = f"{base.rstrip('/')}/auth/magic/verify?token={raw}"

    sent = False
    resend_key = os.environ.get("RESEND_API_KEY", "")
    if resend_key:
        try:
            import resend  # type: ignore[import]
            resend.api_key = resend_key
            resend.Emails.send(
                {
                    "from": os.environ.get(
                        "MERIDIAN_FROM_EMAIL",
                        "Meridian <hello@usemeridian.us>",
                    ),
                    "to": [email],
                    "subject": "Sign in to Meridian",
                    "html": (
                        f"<p>Click below to sign in to Meridian:</p>"
                        f"<p><a href='{link}'>{link}</a></p>"
                        f"<p>Single-use link. Expires in {_MAGIC_TTL_HOURS} hours.</p>"
                        f"<p style='color:#888;font-size:12px;margin-top:24px'>"
                        f"If you didn't request this, you can safely ignore it.</p>"
                    ),
                }
            )
            sent = True
        except Exception:  # noqa: BLE001 — never reveal Resend errors
            sent = False

    payload: dict[str, Any] = {
        "status": "ok",
        "message": "check your inbox" if sent else (
            "magic link generated (email delivery unavailable — check server logs for the URL)"
        ),
    }
    # When Resend isn't configured (local dev), surface the link so the
    # tester can click through. Never do this in production.
    if not sent and not resend_key and os.environ.get("MERIDIAN_HOSTED", "").lower() not in ("1", "true", "yes"):
        payload["dev_link"] = link
    return JSONResponse(payload)


async def auth_magic_verify(request: Request, token: str = ""):
    """v0.9 — GET /auth/magic/verify?token=xxx.

    Validates the token (single-use, unexpired), upserts the tenant for
    that email, creates a session, sets the cookie, and redirects via
    the shared _post_login_redirect — paywall gate applies symmetrically
    to magic-link sign-ups.
    """
    from . import db as db_module

    raw = (token or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="missing token")
    token_hash = hashlib.sha256(raw.encode()).hexdigest()

    db = request.app.state.db
    row = await db_module.consume_magic_token(db, token_hash)
    if row is None:
        # Don't reveal whether expired vs used vs nonexistent.
        raise HTTPException(status_code=401, detail="link expired or already used")

    email = row["email"]
    tenant = await db_module.upsert_tenant(db, email=email)

    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=_SESSION_MAX_AGE_HOURS)
    ).isoformat()
    session = await db_module.create_user_session(db, tenant["id"], expires_at)
    cookie_value = _make_session_cookie(session["id"])

    redirect_to = await _post_login_redirect(tenant, db)
    response = RedirectResponse(redirect_to, status_code=302)
    response.set_cookie(
        _SESSION_COOKIE,
        cookie_value,
        httponly=True,
        secure=_cfg("MERIDIAN_BASE_URL", "").startswith("https://"),
        samesite="lax",
        max_age=_SESSION_MAX_AGE_HOURS * 3600,
    )
    response.delete_cookie("meridian_demo")
    return response
