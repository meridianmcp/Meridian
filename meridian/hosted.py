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


def _truthy(v: str | None) -> bool:
    """Interpret an env-style string as a boolean.

    Unset/empty and the usual off-values ('0', 'false', 'no', 'off') are falsy;
    anything else is truthy. Lets a flag default to on (``"1"``) while still
    honouring an explicit ``"0"`` — plain ``if _cfg(...)`` can't, because a
    literal ``"0"`` string is truthy in Python.
    """
    return (v or "").strip().lower() not in ("", "0", "false", "no", "off")


def auth_setup_health() -> dict[str, Any]:
    """13583103 — self-hosted auth diagnostics.

    Reports, per provider, whether it's configured and which required env vars
    are missing, plus whether the session-signing secret is set and whether any
    provider is usable at all. Returns names and booleans only — never secret
    values — so it's safe to expose unauthenticated (like /health and /config).
    """
    required = {
        "google": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
        "github": ["GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"],
        "microsoft": ["MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET"],
        # Magic-link sign-in can generate links without Resend, but can only
        # deliver them by email when RESEND_API_KEY is set.
        "magic_link": ["RESEND_API_KEY"],
    }
    providers: dict[str, Any] = {}
    for name, keys in required.items():
        missing = [k for k in keys if not _cfg(k)]
        providers[name] = {
            "configured": not missing,
            "missing_env": missing,
            "required_env": list(keys),
        }
    return {
        "providers": providers,
        # MERIDIAN_SESSION_SECRET (aliases SESSION_SECRET) signs session cookies;
        # its absence falls back to an insecure dev default.
        "session_secret_configured": bool(_cfg("MERIDIAN_SESSION_SECRET")),
        "auth_available": any(p["configured"] for p in providers.values()),
    }


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


def _session_meta(request: Request) -> tuple[str | None, str | None]:
    """3c28450d — best-effort ``(user_agent, ip)`` for a new web session, shown
    in the active-sessions view. UA is truncated; IP is the direct client host."""
    ua = (request.headers.get("user-agent") or "")[:400] or None
    ip = (request.client.host if request.client else None) or None
    return ua, ip


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


async def get_microsoft_auth_url(next_url: str = "") -> str:
    """Return the Microsoft OAuth authorization URL."""
    if not MICROSOFT_CLIENT_ID:
        raise RuntimeError("MICROSOFT_CLIENT_ID is not set")
    import urllib.parse
    import base64 as _b64
    params: dict = {
        "client_id": MICROSOFT_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _microsoft_callback_url(),
        "response_mode": "query",
        "scope": _MICROSOFT_SCOPES,
    }
    if next_url:
        params["state"] = f"next:{_b64.urlsafe_b64encode(next_url.encode()).decode()}"
    return f"{_MICROSOFT_AUTH_URL}?{urllib.parse.urlencode(params)}"


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
    prompt: str = "",
) -> str:
    """Return the GitHub OAuth authorization URL.

    ``prompt='select_account'`` forces GitHub to show its account chooser
    instead of silently reusing the browser's current GitHub session — used by
    the multi-account "Connect another account" flow (330937c6).
    """
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
    if prompt:
        params.append(("prompt", prompt))
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


async def _github_repo_branches(access_token: str, repo: str) -> list[str]:
    """v2.8 — return the branch names for ``owner/repo`` (paged, up to 300).

    Used by the dashboard Branch dropdown so users pick an existing branch
    instead of typing one. Raises ``RuntimeError`` on a GitHub error so the
    caller can fall back to a static default list.
    """
    import httpx

    if not repo or "/" not in repo:
        raise RuntimeError("repo must be owner/repo format")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "Meridian",
    }
    branches: list[str] = []
    async with httpx.AsyncClient(timeout=15.0) as http:
        for page in range(1, 4):  # cap at 3 pages (300 branches)
            resp = await http.get(
                f"https://api.github.com/repos/{repo}/branches",
                headers=headers,
                params={"per_page": "100", "page": str(page)},
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"GitHub branch listing failed ({resp.status_code}): {resp.text}"
                )
            batch = resp.json() or []
            if not isinstance(batch, list):
                break
            for b in batch:
                name = b.get("name") if isinstance(b, dict) else None
                if name:
                    branches.append(name)
            if len(batch) < 100:
                break
    return branches


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
.email-hint{font-size:.78rem;color:#8b8fa8;line-height:1.4;margin:-2px 2px 2px}
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
  <div class="logo">🧭 <span>Meridian</span></div>
  <div class="subtitle">Sign in or create an account</div>
<!-- GOOGLE_BUTTON -->
<!-- GITHUB_BUTTON -->
<!-- MICROSOFT_BUTTON -->
  <div class="divider">or</div>
  <form class="email-form" id="magic-form" onsubmit="event.preventDefault();sendMagic();">
    <input type="email" class="email-input" id="magic-email" placeholder="you@example.com" autocomplete="email" required>
    <div class="email-hint">We'll send a magic link to this address — click it to sign in. No password needed.</div>
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
    btn.textContent = 'Send magic link →';
  }
}
</script>
</body>
</html>"""


def _build_login_page(next_url: str = "") -> str:
    """Build the login page HTML.

    Each OAuth provider button is rendered only when its client-id env var is
    configured (98c45dd0), so a self-hosted instance never shows a button that
    503s the moment it's clicked. ``next_url`` is injected into every rendered
    button href. When no OAuth provider is configured the "or" divider is
    dropped so the magic-link form isn't left under a dangling separator.
    """
    from urllib.parse import quote as _q
    next_qs = f"?next={_q(next_url)}" if next_url else ""

    google_button = ""
    if _cfg("GOOGLE_CLIENT_ID"):
        google_button = (
            f'  <a href="/auth/google/login{next_qs}" class="btn btn-google">'
            '<svg width="20" height="20" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>'
            'Continue with Google</a>'
        )

    github_button = ""
    if _cfg("GITHUB_CLIENT_ID"):
        github_button = (
            f'  <a href="/auth/github/login{next_qs}" class="btn btn-github">'
            '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/></svg>'
            'Continue with GitHub</a>'
        )

    ms_button = ""
    if MICROSOFT_CLIENT_ID:
        ms_button = (
            f'  <a href="/auth/microsoft/login{next_qs}" class="btn btn-microsoft">'
            '<svg width="20" height="20" viewBox="0 0 21 21" fill="none"><rect x="1" y="1" width="9" height="9" fill="#f25022"/><rect x="11" y="1" width="9" height="9" fill="#7fba00"/><rect x="1" y="11" width="9" height="9" fill="#00a4ef"/><rect x="11" y="11" width="9" height="9" fill="#ffb900"/></svg>'
            'Continue with Microsoft</a>'
        )

    page = (
        _LOGIN_PAGE_HTML
        .replace("<!-- GOOGLE_BUTTON -->", google_button)
        .replace("<!-- GITHUB_BUTTON -->", github_button)
        .replace("<!-- MICROSOFT_BUTTON -->", ms_button)
    )
    if not (google_button or github_button or ms_button):
        page = page.replace('  <div class="divider">or</div>', "")
    return page


async def auth_login(request: Request):
    """Serve the sign-in page with Google and GitHub OAuth buttons."""
    from fastapi.responses import HTMLResponse
    next_url = request.query_params.get("next", "")
    if not (next_url.startswith("/") and not next_url.startswith("//")):
        next_url = ""
    return HTMLResponse(_build_login_page(next_url=next_url))


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

    # Launch is OPEN by default (98c45dd0): admit the first N free-tier users
    # directly. Set MERIDIAN_LAUNCH_OPEN=0 to re-enable the pre-launch waitlist
    # gate. Returning tenants keep access even after the launch cap is reached.
    if _truthy(_cfg("MERIDIAN_LAUNCH_OPEN", "1")):
        if db is not None:
            from . import db as db_module

            free_cap = int(_cfg("MERIDIAN_FREE_LAUNCH_CAP", "1000") or "1000")
            has_slot = bool(
                (tenant or {}).get("neon_project_id")
                or (tenant or {}).get("neon_db_url")
                or (tenant or {}).get("plan") in {"standard", "pro", "admin"}
            )
            if not has_slot:
                # G5.22 — invitees who already accepted an invite into another
                # tenant's workspace inherit that workspace's DB; don't burn a
                # pool slot for them and don't waitlist them.
                tenant_email = (tenant or {}).get("email", "")
                existing_membership = await db_module.workspace_member_accepted_for_email(
                    db, tenant_email,
                )
                if existing_membership is not None:
                    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
                        return next_url
                    # 90de5ac9 — invited user with no own projects: send them
                    # directly into the inviter's workspace by encoding the
                    # owner's tenant_id in the redirect URL.  The dashboard JS
                    # reads ?ws= on init and sets activeWorkspaceTenantId so
                    # /projects returns the owner's project list immediately.
                    owner_tid = existing_membership.get("tenant_id", "")
                    base = _cfg("MERIDIAN_AFTER_LOGIN_URL", "/dashboard")
                    if owner_tid:
                        return f"{base}?ws={owner_tid}"
                    return base
                free_count = await db_module.count_tenants_by_plan(
                    db, "free", provisioned_only=True
                )
                if free_count >= free_cap:
                    if tenant_email:
                        try:
                            await db_module.add_waitlist_entry(
                                db, tenant_email, note="auto:launch-full"
                            )
                        except Exception:
                            pass
                    return "/waitlist-pending?message=Early%20access%20is%20full"
                try:
                    tenant = await provision_neon_db(tenant["id"], db)
                except Exception as exc:  # noqa: BLE001
                    # 9f584879 — don't swallow silently: a failed provision lands
                    # the user without a DB (503 on every API call). Log at WARNING
                    # so operators can see and retry instead of guessing.
                    import logging as _logging
                    _logging.getLogger("meridian.hosted").warning(
                        "provisioning failed for tenant %s: %s — user will land "
                        "without a DB (likely 503 until retried)",
                        (tenant or {}).get("id"), exc,
                    )
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


async def _auto_accept_pending_invites(db: Any, email: str) -> None:
    """fbbe99af fallback — auto-accept any pending workspace invites for email at login.

    Invited users who sign in via OAuth before clicking the invite link land
    here without an accepted membership row, locking them out of the workspace.
    This accepts all pending rows so they get access immediately on login.
    """
    from . import db as db_module
    try:
        pending = await db_module.get_pending_invites_for_email(db, email)
        for invite in pending:
            await db_module.accept_workspace_invite(db, invite["id"])
    except Exception:
        pass  # never block login on invite-accept failure


async def _consume_pending_invite_cookie(request: Any, db: Any) -> str | None:
    """fbbe99af — accept invite token stored in the pending_invite_token cookie.

    /workspace/accept stores the token here before redirecting to OAuth so it
    survives the provider redirect chain (the ?next= URL alone drops the token).
    Returns "/dashboard" if an invite was found and accepted, None otherwise.
    Caller must call response.delete_cookie("pending_invite_token") to clear it.
    """
    import hashlib
    from . import db as db_module
    token = request.cookies.get("pending_invite_token", "")
    if not token:
        return None
    try:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        invite = await db_module.get_workspace_invite_by_token_hash(db, token_hash)
        if invite:
            await db_module.accept_workspace_invite(db, invite["id"])
            return "/dashboard"
    except Exception:
        pass
    return None


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
    await _auto_accept_pending_invites(db, email)

    # G5.22 — Skip auto-provisioning a Neon DB when the user has already
    # accepted an invite to someone else's workspace. Provisioning then
    # would burn a pool slot for a tenant that will only ever read from
    # the inviter's DB. Admin accounts and already-provisioned tenants
    # are also no-ops inside provision_neon_db.
    if not tenant.get("neon_project_id") and not tenant.get("neon_db_url"):
        existing_membership = await db_module.workspace_member_accepted_for_email(db, email)
        if existing_membership is None:
            import asyncio as _asyncio
            _asyncio.create_task(_provision_tenant_background(tenant["id"], db))

    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=_SESSION_MAX_AGE_HOURS)
    ).isoformat()
    session = await db_module.create_user_session(
        db, tenant["id"], expires_at, *_session_meta(request)
    )
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
    # fbbe99af — consume pending invite cookie (takes priority; clears token so
    # the /workspace/accept redirect doesn't 404 on the already-accepted token)
    _invite_url = await _consume_pending_invite_cookie(request, db)
    if _invite_url:
        _next_url = _invite_url
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
    response.delete_cookie("pending_invite_token")
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
    import base64 as _b64
    next_url = request.query_params.get("next", "")
    if not (next_url.startswith("/") and not next_url.startswith("//")):
        next_url = ""
    state = f"next:{_b64.urlsafe_b64encode(next_url.encode()).decode()}" if next_url else ""
    try:
        url = await get_github_auth_url(state=state)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=302)


async def auth_github_callback(request: Request) -> RedirectResponse:
    """Handle GitHub OAuth callback — upsert tenant, set session cookie.
    Also handles repo-connect flow when state starts with 'repo:'."""
    # Delegate to repo-connect handler if this is a repo OAuth flow
    state = request.query_params.get("state", "")
    if state.startswith("repo:"):
        return await auth_github_repo_callback(request, request.app.state.db)

    from . import db as db_module

    # Extract next_url from state (set by auth_github_login when ?next= was present)
    import base64 as _b64
    _next_url = ""
    if state.startswith("next:"):
        try:
            _next_url = _b64.urlsafe_b64decode(state[5:] + "==").decode()
        except Exception:
            _next_url = ""
    if not (_next_url.startswith("/") and not _next_url.startswith("//")):
        _next_url = ""

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
    await _auto_accept_pending_invites(db, email)

    # G5.22 — same invite-aware skip as the Google callback. See the comment
    # in auth_callback for details.
    if not tenant.get("neon_project_id") and not tenant.get("neon_db_url"):
        existing_membership = await db_module.workspace_member_accepted_for_email(db, email)
        if existing_membership is None:
            import asyncio as _asyncio
            _asyncio.create_task(_provision_tenant_background(tenant["id"], db))

    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=_SESSION_MAX_AGE_HOURS)
    ).isoformat()
    session = await db_module.create_user_session(
        db, tenant["id"], expires_at, *_session_meta(request)
    )
    cookie_value = _make_session_cookie(session["id"])

    _invite_url = await _consume_pending_invite_cookie(request, db)
    if _invite_url:
        _next_url = _invite_url
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
    response.delete_cookie("pending_invite_token")
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
    # 330937c6 — "Connect another account" passes select_account=1 so GitHub shows
    # the account chooser rather than reusing the current GitHub browser session,
    # which is the only way to attach a second account from one browser.
    select_account = request.query_params.get("select_account", "").strip().lower() in ("1", "true", "yes")
    try:
        url = await get_github_auth_url(
            scope="repo",
            state=f"repo:{project_id}",
            redirect_uri=_github_repo_callback_url(),
            prompt="select_account" if select_account else "",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=302)


async def auth_github_repo_callback(request: Request, db: Any) -> RedirectResponse:
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
    project = await db_module.get_project(db, project_id)
    selected_repo = ((project or {}).get("github_repo") or "").strip()
    selected_branch = ((project or {}).get("github_branch") or "main").strip()
    repo_lookup = {repo.get("full_name", ""): repo for repo in repos if repo.get("full_name")}
    if selected_repo and selected_repo in repo_lookup:
        selected_branch = (selected_branch or repo_lookup[selected_repo].get("default_branch") or "main").strip()
    elif repos:
        first_repo = repos[0]
        selected_repo = (first_repo.get("full_name") or "").strip()
        selected_branch = (first_repo.get("default_branch") or "main").strip()

    # PAT stays on tenant (it's the auth credential, not project-specific)
    await db_module.update_tenant(
        request.app.state.db,
        tenant["id"],
        github_pat=db_module.encrypt_field(access_token),
    )
    # Repo + branch are per-project
    await db_module.update_project_settings(
        db, project_id,
        github_repo=selected_repo or None,
        github_branch=selected_branch or "main",
    )

    return RedirectResponse(
        f"/dashboard?project_id={project_id}&tab=settings",
        status_code=302,
    )


async def auth_microsoft_login(request: Request) -> RedirectResponse:
    """Redirect the browser to Microsoft's OAuth consent page."""
    next_url = request.query_params.get("next", "")
    if not (next_url.startswith("/") and not next_url.startswith("//")):
        next_url = ""
    try:
        url = await get_microsoft_auth_url(next_url=next_url)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=302)


async def auth_microsoft_callback(request: Request) -> RedirectResponse:
    """Handle Microsoft OAuth callback — upsert tenant, set session cookie."""
    from . import db as db_module

    # Extract next_url from state (set by auth_microsoft_login when ?next= was present)
    import base64 as _b64
    _ms_state = request.query_params.get("state", "")
    _next_url = ""
    if _ms_state.startswith("next:"):
        try:
            _next_url = _b64.urlsafe_b64decode(_ms_state[5:] + "==").decode()
        except Exception:
            _next_url = ""
    if not (_next_url.startswith("/") and not _next_url.startswith("//")):
        _next_url = ""

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
    await _auto_accept_pending_invites(db, email)

    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=_SESSION_MAX_AGE_HOURS)
    ).isoformat()
    session = await db_module.create_user_session(
        db, tenant["id"], expires_at, *_session_meta(request)
    )
    cookie_value = _make_session_cookie(session["id"])

    _invite_url = await _consume_pending_invite_cookie(request, db)
    if _invite_url:
        _next_url = _invite_url
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
    response.delete_cookie("pending_invite_token")
    return response


async def auth_logout(request: Request) -> RedirectResponse:
    """Clear the session cookie and delete the DB session.

    Supports an optional ``?next=<relative-path>`` query param to redirect
    somewhere other than / after sign-out (e.g. /auth/login for account switching).
    """
    from . import db as db_module
    from urllib.parse import urlsplit

    cookie_val = request.cookies.get(_SESSION_COOKIE)
    if cookie_val:
        session_id = _read_session_cookie(cookie_val)
        if session_id:
            db = request.app.state.db
            await db_module.delete_user_session(db, session_id)

    # Only allow relative redirects (no scheme/netloc) to prevent open-redirect.
    next_path = request.query_params.get("next", "")
    parsed = urlsplit(next_path)
    if parsed.scheme or parsed.netloc:
        next_path = "/"
    redirect_to = next_path or "/"

    response = RedirectResponse(redirect_to, status_code=302)
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


async def create_stripe_billing_portal_session(tenant: dict) -> str:
    """G2.11 — open a Stripe Customer Portal session for an existing subscriber.

    The portal lets the customer update card, change plan, view invoices,
    and cancel. Returns the portal URL to redirect to. Raises ValueError
    if the tenant has no stripe_customer_id; callers should route those
    users to /pricing for first subscription instead. Raises RuntimeError
    if STRIPE_API_KEY is not configured.
    """
    customer_id = tenant.get("stripe_customer_id")
    if not customer_id:
        raise ValueError("tenant has no stripe_customer_id — send to /pricing")

    import stripe  # type: ignore[import]  # noqa: PLC0415

    stripe.api_key = _require_cfg("STRIPE_API_KEY")
    base = _cfg("MERIDIAN_BASE_URL", "http://localhost:7878").rstrip("/")
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=f"{base}/dashboard",
    )
    return session.url


async def provision_with_retry(
    tenant_id: str,
    db: Any,
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    _sleep: Any = None,
) -> dict[str, Any]:
    """4c559d4e — provision a tenant's Neon DB with exponential backoff. On the
    final failure, enqueue the tenant to provision_queue for durable later retry,
    then re-raise. ``_sleep`` is injectable for tests (no real delays)."""
    import asyncio as _asyncio
    from . import db as db_module
    sleep = _sleep or _asyncio.sleep
    n = max(1, int(attempts))
    last_exc: Exception | None = None
    for i in range(n):
        try:
            result = await provision_neon_db(tenant_id, db)
            try:
                await db_module.mark_provision_done(db, tenant_id)
            except Exception:  # noqa: BLE001
                pass
            return result
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if i < n - 1:
                await sleep(base_delay * (2 ** i))
    try:
        await db_module.enqueue_provision(
            db, tenant_id, last_error=(str(last_exc)[:500] if last_exc else None)
        )
    except Exception:  # noqa: BLE001
        pass
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("provisioning failed")  # pragma: no cover


async def provisioning_health(db: Any) -> dict[str, Any]:
    """4c559d4e — provisioning-queue health for /health/deep monitoring."""
    from . import db as db_module
    try:
        pending = await db_module.count_pending_provisions(db)
    except Exception:  # noqa: BLE001
        pending = None
    return {"pending_provisions": pending}


async def _provision_tenant_background(tenant_id: str, db: Any) -> None:
    """Best-effort background provisioning called from OAuth callbacks.

    Silently swallows errors — login must never fail due to provisioning issues.
    Logs failures at WARNING level for ops visibility. 4c559d4e — retries with
    backoff and enqueues to provision_queue on final failure (durable retry).
    """
    import logging as _logging
    try:
        await provision_with_retry(tenant_id, db)
    except Exception as exc:  # noqa: BLE001
        _logging.getLogger(__name__).warning(
            "Background Neon provisioning failed for tenant %s (queued for retry): %s",
            tenant_id, exc,
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

    # Item 38 — atomic claim+increment to avoid the overprovisioning race
    # where two concurrent signups both pick a pool with 7/8 slots and bump
    # it to 9/8. claim_pool_project_slot returns the row already-incremented.
    pool = await db_module.claim_pool_project_slot(
        db, tier=pool_tier, max_customers=_MAX_CUSTOMERS_PER_PROJECT
    )

    if pool is None:
        # All existing pool projects are full (or all last-slot races lost).
        # Create a new one and claim a slot from it. claim() on the fresh pool
        # is still atomic, so even if a sibling signup raced to create another
        # new pool, both claims succeed harmlessly.
        neon_project_id, _first_conn_uri = await _create_neon_pool_project(api_key, pool_tier)
        await db_module.register_pool_project(db, neon_project_id, pool_tier)
        pool = await db_module.claim_pool_project_slot(
            db, tier=pool_tier, max_customers=_MAX_CUSTOMERS_PER_PROJECT
        )
        if pool is None:  # extremely unlikely — fresh pool with cap-1 capacity
            raise RuntimeError(
                "newly-registered pool project had no claimable slot "
                "(check _MAX_CUSTOMERS_PER_PROJECT > 0)"
            )

    neon_project_id = pool["neon_project_id"]

    # Create a customer-specific database inside the pool project
    email_slug = tenant["email"].split("@")[0][:20].replace(".", "_")
    db_name = f"cust_{email_slug}_{tenant_id[:8]}"
    conn_uri = await _create_customer_database(api_key, neon_project_id, db_name)

    if not conn_uri:
        # Roll back the claim we made so the slot isn't leaked.
        await db_module.decrement_pool_project_count(db, neon_project_id)
        raise RuntimeError(f"Failed to get connection URI for customer database {db_name!r}")

    # Persist on tenant — encrypt the connection string at rest.
    # Per-tenant key when MERIDIAN_MASTER_SECRET is set, else legacy global key.
    from .tenant_crypto import encrypt_tenant_db_url  # noqa: PLC0415
    updated = await db_module.update_tenant(
        db,
        tenant_id,
        neon_project_id=neon_project_id,
        pool_project_id=pool["id"],
        neon_db_url=encrypt_tenant_db_url(tenant_id, conn_uri),
    )

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
<p><strong>{inviter_email.split('@')[0]}</strong> invited you to collaborate on Meridian — shared memory for AI coding sessions.</p>
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
        "SELECT id, email, plan, neon_project_id, stripe_customer_id, stripe_metered_item_id, is_internal "
        "FROM tenants WHERE neon_project_id IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()

    def _to_d(r: Any) -> dict:
        return r if isinstance(r, dict) else {k: r[k] for k in r.keys()}

    tenants = [_to_d(r) for r in rows] if rows else []

    for tenant in tenants:
        # G2.10 — internal tenants are never charged for storage overage.
        if tenant.get("is_internal"):
            continue
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

        # G2.10 — internal tenants are never billed for overage or warned.
        if tenant.get("is_internal"):
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
    # G2.10 — internal tenants are never churned.
    async with db.execute(
        "SELECT id, email, neon_project_id, pool_project_id, created_at FROM tenants "
        "WHERE stripe_customer_id IS NULL AND neon_project_id IS NOT NULL "
        "AND (is_internal IS NULL OR is_internal = 0)"
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
# Trial-expiration reminder emails (9f7bfcca)
# ---------------------------------------------------------------------------

# Days-remaining thresholds at which a trial tenant is reminded, once each.
TRIAL_REMINDER_THRESHOLDS: tuple[int, ...] = (14, 7, 1)

# Only these plans are on a free trial that can expire. Paying plans
# (standard/pro) renew via Stripe and never receive a trial-expiry reminder.
_TRIAL_PLANS: frozenset[str] = frozenset({"free", "trial"})


def _parse_tenant_ts(raw: Any) -> "datetime | None":
    """Parse a tenant timestamp column into an aware UTC datetime, or None.

    Tolerates the two formats Meridian writes: the ``"%Y-%m-%d %H:%M:%S"``
    form used for ``inactivity_expires_at`` and ISO-8601 (``created_at`` via
    ``datetime('now')`` / explicit isoformat writes). Naive values are treated
    as UTC.
    """
    if not raw:
        return None
    s = str(raw).strip()
    for parser in (
        lambda v: datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S"),
        lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")),
    ):
        try:
            dt = parser(s)
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def compute_trial_reminder(
    tenant: dict[str, Any],
    now: "datetime",
) -> "int | None":
    """Pure decision: which single reminder threshold should fire *now* for
    this tenant, or ``None`` if no email should be sent.

    This function does no I/O — it is the unit-testable core of the daily
    trial-reminder pass. It:

    * skips non-trial plans (``standard``/``pro``/``admin`` — only ``free``/
      ``trial`` are eligible),
    * skips internal tenants,
    * requires a resolvable trial expiry (``inactivity_expires_at``, falling
      back to a 30-day window from ``trial_started_at`` — mirroring the
      dashboard banner in ``server.py``),
    * skips already-expired trials (nothing to remind about),
    * reads which thresholds were already sent from the existing
      ``notification_prefs`` JSON blob (key ``trial_reminders_sent``), so no
      schema column is needed for idempotency,
    * buckets the tenant into the *tightest* threshold they currently fall in
      — the smallest threshold ``>= days_remaining`` — so the email always
      states an accurate "ends in N days" (at 10 days out you get the 14-day
      notice, at 7 the 7-day notice, at 1 the 1-day notice), and returns that
      threshold only if it has not already been sent. Once sent, that bucket
      is skipped forever; the next (smaller) bucket fires on a later pass,
      giving exactly one email per threshold.
    """
    if tenant.get("is_internal"):
        return None
    plan = (tenant.get("plan") or "").lower()
    if plan not in _TRIAL_PLANS:
        return None

    expires_at = _parse_tenant_ts(tenant.get("inactivity_expires_at"))
    if expires_at is None:
        anchor = _parse_tenant_ts(
            tenant.get("trial_started_at") or tenant.get("created_at")
        )
        if anchor is not None:
            expires_at = anchor + timedelta(days=30)
    if expires_at is None:
        return None

    remaining = expires_at - now
    # Already expired (or expiring this instant) — the dunning/expiry flow
    # handles those; a "your trial ends soon" nudge would be wrong.
    if remaining.total_seconds() <= 0:
        return None
    days_remaining = remaining.days  # floor; 1.9 days -> 1

    # Tightest bucket the tenant currently falls in: the smallest configured
    # threshold that is still >= days_remaining. Beyond the largest threshold
    # (too far out) there is no bucket yet.
    buckets = sorted(TRIAL_REMINDER_THRESHOLDS)
    bucket = next((th for th in buckets if days_remaining <= th), None)
    if bucket is None:
        return None

    if bucket in _trial_reminders_sent(tenant):
        return None
    return bucket


def _trial_reminders_sent(tenant: dict[str, Any]) -> set[int]:
    """Return the set of trial-reminder thresholds already emailed to this
    tenant, read from the ``notification_prefs`` JSON blob. Tolerant of a
    missing / malformed blob (treated as none sent)."""
    import json as _json

    raw = tenant.get("notification_prefs")
    if not raw:
        return set()
    try:
        prefs = raw if isinstance(raw, dict) else _json.loads(raw)
    except (ValueError, TypeError):
        return set()
    sent = prefs.get("trial_reminders_sent") if isinstance(prefs, dict) else None
    if not isinstance(sent, (list, tuple, set)):
        return set()
    out: set[int] = set()
    for v in sent:
        try:
            out.add(int(v))
        except (ValueError, TypeError):
            continue
    return out


def _with_reminder_recorded(
    tenant: dict[str, Any], threshold: int
) -> str:
    """Return the tenant's ``notification_prefs`` JSON with ``threshold`` added
    to ``trial_reminders_sent`` — the value to persist after a send. Preserves
    every other key already in the blob."""
    import json as _json

    raw = tenant.get("notification_prefs")
    try:
        prefs = (
            dict(raw)
            if isinstance(raw, dict)
            else (_json.loads(raw) if raw else {})
        )
    except (ValueError, TypeError):
        prefs = {}
    if not isinstance(prefs, dict):
        prefs = {}
    sent = sorted(_trial_reminders_sent(tenant) | {int(threshold)})
    prefs["trial_reminders_sent"] = sent
    return _json.dumps(prefs)


def _trial_reminder_email(threshold: int, base: str) -> tuple[str, str]:
    """Return ``(subject, html)`` for the given days-remaining threshold."""
    if threshold == 1:
        when = "tomorrow"
    else:
        when = f"in {threshold} days"
    subject = f"Your Meridian free trial ends {when}"
    html = (
        f"<h2>Your Meridian trial ends {when}</h2>"
        f"<p>Your free trial is almost over. Upgrade now to keep your "
        f"projects, session memory, and coordination data.</p>"
        f"<p><a href='{base}/pricing'>Choose a plan &rarr;</a></p>"
        f"<p>Nothing is deleted the moment your trial ends — but upgrading "
        f"now avoids any interruption to your AI coding sessions.</p>"
        f"<p>Questions? Reply to this email or contact "
        f"<a href='mailto:hello@usemeridian.us'>hello@usemeridian.us</a>.</p>"
    )
    return subject, html


async def run_trial_reminder_check(db: Any, *, now: "datetime | None" = None) -> None:
    """Daily pass: email each trialing tenant once per {14, 7, 1}-day threshold
    before their free trial ends.

    Idempotent without a schema change — which thresholds were sent is stored
    in the existing ``tenants.notification_prefs`` JSON blob (see
    ``compute_trial_reminder``). Guarded to hosted mode by the caller (the
    background loop only invokes this when ``MERIDIAN_HOSTED`` is set).

    ``now`` is injectable purely for testing; production calls pass nothing and
    get ``datetime.now(timezone.utc)``.
    """
    import httpx

    from . import db as db_module

    now = now or datetime.now(timezone.utc)
    resend_key = _cfg("RESEND_API_KEY")
    if not resend_key:
        return  # dev mode — no email transport configured
    from_addr = _cfg("MERIDIAN_FROM_EMAIL", "Meridian <noreply@usemeridian.us>")
    base = _cfg("MERIDIAN_BASE_URL", "https://usemeridian.us").rstrip("/")

    async with db.execute(
        "SELECT * FROM tenants WHERE plan IN ('free', 'trial')"
    ) as cur:
        rows = await cur.fetchall()

    def _to_d(r: Any) -> dict:
        return r if isinstance(r, dict) else {k: r[k] for k in r.keys()}

    for row in rows or []:
        tenant = _to_d(row)
        threshold = compute_trial_reminder(tenant, now)
        if threshold is None:
            continue
        email = tenant.get("email")
        if not email:
            continue
        subject, html = _trial_reminder_email(threshold, base)
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {resend_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "from": from_addr,
                        "to": [email],
                        "subject": subject,
                        "html": html,
                    },
                )
                resp.raise_for_status()
        except Exception:  # noqa: BLE001 — one bad send must not stall the pass
            continue
        # Persist idempotency marker only after a successful send, so a
        # transient Resend failure retries on the next daily pass.
        try:
            await db_module.update_tenant(
                db,
                tenant["id"],
                notification_prefs=_with_reminder_recorded(tenant, threshold),
            )
        except Exception:  # noqa: BLE001
            continue


# ---------------------------------------------------------------------------
# 342dd15f — Redis command budget: warning emails + monthly counter reset
# ---------------------------------------------------------------------------

#: Upstash pricing: $0.20 / 100 000 commands → 1 command = $2e-6.
#: Tier-1 warning threshold (~$1.00 / month).
REDIS_BUDGET_WARN_COMMANDS: int = 500_000
#: Tier-2 disable threshold (~$2.00 / month). publish_session_message returns
#: False when the tenant's counter is >= this value.
REDIS_BUDGET_DISABLE_COMMANDS: int = 1_000_000


def compute_redis_overage_action(
    tenant: dict[str, Any],
) -> "str | None":
    """Pure decision: which Redis-budget action (if any) to take for *tenant*.

    Returns:
      ``'warn'``  — tenant's redis_commands_used has crossed Tier-1 (500 000
                    commands / ~$1.00) but has not yet been warned this month.
                    Caller should send a warning email and record the flag.
      ``'hard_limit'``  — tenant's counter has crossed Tier-2 (1 000 000
                          commands / ~$2.00) but the "hard limit reached" email
                          has not been sent yet.  The publish gate in
                          redis_bridge already blocks new publishes at this
                          threshold; this email is purely informational.
      ``None``   — no action needed (counter below Tier-1, already notified,
                   or tenant is internal).

    Idempotent — which notifications were sent is stored in the tenant's
    existing ``notification_prefs`` JSON blob (keys ``redis_warn_sent`` and
    ``redis_hard_limit_sent``), so no new schema column is needed for the
    idempotency flags themselves.
    """
    if tenant.get("is_internal"):
        return None

    used = int(tenant.get("redis_commands_used") or 0)
    prefs = _parse_notification_prefs(tenant)

    if used >= REDIS_BUDGET_DISABLE_COMMANDS:
        if not prefs.get("redis_hard_limit_sent"):
            return "hard_limit"
        return None

    if used >= REDIS_BUDGET_WARN_COMMANDS:
        if not prefs.get("redis_warn_sent"):
            return "warn"
        return None

    return None


def _parse_notification_prefs(tenant: dict[str, Any]) -> dict[str, Any]:
    """Return the tenant's notification_prefs dict, tolerant of missing/malformed blobs."""
    import json as _json  # noqa: PLC0415
    raw = tenant.get("notification_prefs")
    if not raw:
        return {}
    try:
        prefs = raw if isinstance(raw, dict) else _json.loads(raw)
        return prefs if isinstance(prefs, dict) else {}
    except (ValueError, TypeError):
        return {}


def _with_redis_flag_recorded(
    tenant: dict[str, Any], flag: str
) -> str:
    """Return the tenant's ``notification_prefs`` JSON with ``flag`` set to True.

    Preserves every other key already in the blob.
    """
    import json as _json  # noqa: PLC0415
    prefs = _parse_notification_prefs(tenant)
    prefs[flag] = True
    return _json.dumps(prefs)


def _redis_warning_email(used: int, base: str) -> tuple[str, str]:
    """Return ``(subject, html)`` for the Tier-1 Redis budget warning email."""
    subject = "[Meridian] High real-time message volume — Redis usage notice"
    html = (
        f"<h2>Your Meridian real-time messaging usage is elevated</h2>"
        f"<p>Your account has issued approximately <strong>{used:,} Redis publish "
        f"commands</strong> this billing month (threshold: 500,000 commands / ~$1.00 "
        f"Upstash cost).</p>"
        f"<p>The Meridian <code>send_message</code> tool uses Redis for real-time "
        f"push delivery. At current volume, Redis costs for your account are tracking "
        f"above our $1.00/month per-account budget.</p>"
        f"<p>No action is required immediately. If usage continues to increase, "
        f"real-time push delivery will be automatically disabled at 1,000,000 commands "
        f"(~$2.00/month). Messages will still be reliably delivered via Postgres "
        f"polling — only the <em>real-time push</em> feature is affected.</p>"
        f"<p>Questions? Reply to this email or contact "
        f"<a href='mailto:hello@usemeridian.us'>hello@usemeridian.us</a>.</p>"
    )
    return subject, html


def _redis_hard_limit_email(used: int, base: str) -> tuple[str, str]:
    """Return ``(subject, html)`` for the Tier-2 Redis budget hard-limit email."""
    subject = "[Meridian] Real-time push messaging paused — Redis limit reached"
    html = (
        f"<h2>Real-time push messaging has been paused for your account</h2>"
        f"<p>Your account has reached the Redis publish command limit for this billing "
        f"month: <strong>{used:,} commands</strong> (limit: 1,000,000 / ~$2.00 "
        f"Upstash cost).</p>"
        f"<p><strong>What this means:</strong> The <code>send_message</code> MCP tool "
        f"will continue to work normally — messages are durably stored in your Postgres "
        f"database. Only the <em>real-time push notification</em> layer has been paused. "
        f"Recipients will receive messages via polling instead of instant delivery.</p>"
        f"<p><strong>When does it reset?</strong> Real-time push will automatically "
        f"resume at the start of next billing month when your counter resets.</p>"
        f"<p>If you believe this limit was reached in error, or you need a higher limit, "
        f"please contact "
        f"<a href='mailto:hello@usemeridian.us'>hello@usemeridian.us</a>.</p>"
    )
    return subject, html


async def run_redis_overage_check(db: Any, *, now: "datetime | None" = None) -> None:
    """Hourly pass: send Redis-budget warning/hard-limit emails to affected tenants.

    Tier-1 (500 000 commands / ~$1.00): warning email. Idempotent via the
    tenant's notification_prefs JSON blob (``redis_warn_sent`` flag).

    Tier-2 (1 000 000 commands / ~$2.00): separate "hard limit reached" email.
    Idempotent via ``redis_hard_limit_sent`` flag. The publish gate in
    redis_bridge.publish_session_message already enforces the hard limit at the
    call site; this function's job is purely the informational email.

    Counter reset: at the start of each calendar month (UTC), reset
    redis_commands_used to 0 and clear the per-month idempotency flags so
    tenants get fresh notifications next month if their pattern recurs.

    ``now`` is injectable purely for testing; production calls pass nothing.
    """
    import httpx  # noqa: PLC0415

    from . import db as db_module  # noqa: PLC0415

    now = now or datetime.now(timezone.utc)
    resend_key = _cfg("RESEND_API_KEY")
    if not resend_key:
        return  # dev mode — no email transport configured
    from_addr = _cfg("MERIDIAN_FROM_EMAIL", "Meridian <noreply@usemeridian.us>")
    base = _cfg("MERIDIAN_BASE_URL", "https://usemeridian.us").rstrip("/")

    async with db.execute(
        "SELECT * FROM tenants WHERE plan NOT IN ('free') OR plan IS NULL"
    ) as cur:
        rows = await cur.fetchall()

    def _to_d(r: Any) -> dict:
        return r if isinstance(r, dict) else {k: r[k] for k in r.keys()}

    for row in rows or []:
        tenant = _to_d(row)

        # Monthly counter reset: if overage_reset_at is from a previous month,
        # clear redis_commands_used and the per-month notification flags.
        reset_at_raw = tenant.get("overage_reset_at")
        if reset_at_raw:
            try:
                from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415
                reset_dt = _dt.fromisoformat(reset_at_raw.replace("Z", "+00:00"))
                if reset_dt.month != now.month or reset_dt.year != now.year:
                    # New month — reset counter and flags.
                    import json as _json  # noqa: PLC0415
                    prefs = _parse_notification_prefs(tenant)
                    prefs.pop("redis_warn_sent", None)
                    prefs.pop("redis_hard_limit_sent", None)
                    try:
                        await db_module.update_tenant(
                            db,
                            tenant["id"],
                            redis_commands_used=0,
                            notification_prefs=_json.dumps(prefs),
                        )
                        tenant["redis_commands_used"] = 0
                        tenant["notification_prefs"] = _json.dumps(prefs)
                    except Exception:  # noqa: BLE001
                        continue
            except (ValueError, AttributeError):
                pass

        action = compute_redis_overage_action(tenant)
        if action is None:
            continue
        email = tenant.get("email")
        if not email:
            continue

        used = int(tenant.get("redis_commands_used") or 0)
        if action == "warn":
            subject, html = _redis_warning_email(used, base)
            flag = "redis_warn_sent"
        else:  # hard_limit
            subject, html = _redis_hard_limit_email(used, base)
            flag = "redis_hard_limit_sent"

        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {resend_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "from": from_addr,
                        "to": [email],
                        "subject": subject,
                        "html": html,
                    },
                )
                resp.raise_for_status()
        except Exception:  # noqa: BLE001 — one bad send must not stall the pass
            continue

        # Persist idempotency flag only after a successful send.
        try:
            await db_module.update_tenant(
                db,
                tenant["id"],
                notification_prefs=_with_redis_flag_recorded(tenant, flag),
            )
        except Exception:  # noqa: BLE001
            continue


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
        # G2.10 — internal tenants are never sent dunning warnings.
        if tenant.get("is_internal"):
            continue
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


def _magic_email_subject() -> str:
    """88affef6 — unique, timestamped subject line so Gmail/Outlook don't thread
    or collapse repeated 'Sign in to Meridian' emails (which hides the newest
    link and hurts deliverability). The SPF/DKIM/DMARC DNS records are a separate
    manual operator step."""
    return (
        "Sign in to Meridian ("
        + datetime.now(timezone.utc).strftime("%b %d, %H:%M:%S UTC")
        + ")"
    )


# 925909aa — persistent per-IP signup limit (defence in depth beyond the slowapi
# 5/min IP window; survives restarts via the signup_attempts table).
_SIGNUP_IP_WINDOW_HOURS = 24
_SIGNUP_IP_MAX_PER_WINDOW = 20


async def _verify_turnstile(token: object) -> bool:
    """925909aa — verify a Cloudflare Turnstile token. Returns True (allow) when
    no ``TURNSTILE_SECRET_KEY`` is configured (self-host / dev / tests skip the
    challenge). When a key IS set, POSTs to Cloudflare's siteverify and returns
    its success flag; a network error fails OPEN (True) so a Cloudflare outage
    can never lock everyone out of signup. A configured key with a missing/blank
    token fails closed (False)."""
    secret = _cfg("TURNSTILE_SECRET_KEY", "") or ""
    if not secret:
        return True
    tok = token.strip() if isinstance(token, str) else ""
    if not tok:
        return False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8) as http:
            resp = await http.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": secret, "response": tok},
            )
            data = resp.json()
        return bool(data.get("success"))
    except Exception:  # noqa: BLE001 — fail open on Cloudflare/network error
        return True


async def account_sessions_list(request: Request):
    """3c28450d — list the current tenant's active web sessions with device
    metadata, marking the session making this request as ``current``. Hosted-only;
    get_current_tenant raises 401 without a valid session cookie."""
    from fastapi.responses import JSONResponse
    from . import db as db_module

    tenant = await get_current_tenant(request)
    db = request.app.state.db
    cookie_val = request.cookies.get(_SESSION_COOKIE)
    current_sid = _read_session_cookie(cookie_val) if cookie_val else None
    sessions = await db_module.get_user_sessions_for_tenant(db, tenant["id"])
    out = [
        {
            "id": s["id"],
            "current": s["id"] == current_sid,
            "user_agent": s.get("user_agent"),
            "ip": s.get("ip"),
            "created_at": s.get("created_at"),
            "last_seen_at": s.get("last_seen_at"),
            "expires_at": s.get("expires_at"),
        }
        for s in sessions
    ]
    return JSONResponse({"sessions": out})


async def account_session_revoke(request: Request, session_id: str):
    """3c28450d — revoke (sign out) one of the current tenant's web sessions.
    Tenant-scoped: a user can never revoke another tenant's session."""
    from fastapi.responses import JSONResponse
    from . import db as db_module

    tenant = await get_current_tenant(request)
    db = request.app.state.db
    revoked = await db_module.revoke_user_session(db, session_id, tenant["id"])
    return JSONResponse({"status": "ok", "revoked": bool(revoked)})


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

    # 925909aa — reject known disposable/throwaway domains. Generic 200 so the
    # rejection reason is never revealed (no enumeration signal).
    from .email_blocklist import is_disposable_email
    if is_disposable_email(email):
        return JSONResponse({"status": "ok", "message": "check your inbox"})

    # 925909aa — Cloudflare Turnstile: enforced only when TURNSTILE_SECRET_KEY is
    # set (prod). Self-host / dev / tests have no key → skipped (allowed).
    if not await _verify_turnstile(body.get("turnstile_token")):
        return JSONResponse({"status": "ok", "message": "check your inbox"})

    if _magic_rate_limited(email):
        # Always return 200 — don't leak whether the email is registered
        # or whether they're rate-limited.
        return JSONResponse(
            {"status": "ok", "message": "check your inbox"}
        )

    db = request.app.state.db

    # 925909aa — persistent per-IP signup limit (survives restarts; complements
    # the slowapi 5/min window). Salted hashes only — never store raw IP/email.
    try:
        _ip = (request.client.host if request.client else "") or ""
        _salt = _cfg("MERIDIAN_HASH_SALT", "meridian-signup") or "meridian-signup"
        _ip_hash = hashlib.sha256(f"{_salt}:{_ip}".encode()).hexdigest()
        _since = (
            datetime.now(timezone.utc) - timedelta(hours=_SIGNUP_IP_WINDOW_HOURS)
        ).strftime("%Y-%m-%d %H:%M:%S")
        if await db_module.count_recent_signup_attempts(
            db, _ip_hash, _since
        ) >= _SIGNUP_IP_MAX_PER_WINDOW:
            return JSONResponse({"status": "ok", "message": "check your inbox"})
        _email_hash = hashlib.sha256(f"{_salt}:{email}".encode()).hexdigest()
        await db_module.record_signup_attempt(db, _ip_hash, _email_hash)
    except Exception:  # noqa: BLE001 — never block signup on the limiter failing
        pass

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
                    "subject": _magic_email_subject(),
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

    if not sent:
        # Email delivery unavailable or failed (no RESEND_API_KEY, or Resend
        # errored) — log the link at WARNING so a self-hoster/operator can
        # recover it from stdout and still complete sign-in (98c45dd0). This
        # only fires when the email was NOT delivered.
        import logging as _logging
        _logging.getLogger("meridian.auth").warning(
            "magic link for %s (email delivery unavailable): %s", email, link
        )

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


def _magic_error_page(message: str, *, status_code: int, title: str = "Sign-in link problem"):
    """fdf1120f — render a friendly HTML page for magic-link failures.

    The verify endpoint is opened directly in a browser (often a mobile mail
    app), so a raw JSON HTTPException renders as a blank/confusing page. Return
    real HTML with a clear path back to request a fresh link.
    """
    from fastapi.responses import HTMLResponse
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Meridian — {title}</title></head>'
        '<body style="margin:0;background:#0a0e14;color:#e6edf3;font-family:-apple-system,'
        "BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;display:flex;min-height:100vh;"
        'align-items:center;justify-content:center">'
        '<div style="max-width:380px;padding:32px 28px;text-align:center">'
        '<div style="font-size:30px;margin-bottom:10px">🧭</div>'
        f'<h1 style="font-size:18px;margin:0 0 10px;font-weight:600">{title}</h1>'
        f'<p style="font-size:14px;color:#9ca3af;line-height:1.6;margin:0 0 22px">{message}</p>'
        '<a href="/auth/login" style="display:inline-block;padding:10px 22px;background:#3b82f6;'
        'color:#fff;text-decoration:none;border-radius:6px;font-size:14px;font-weight:600">'
        'Request a new link →</a>'
        '</div></body></html>'
    )
    return HTMLResponse(html, status_code=status_code)


async def auth_magic_verify(request: Request, token: str = ""):
    """v0.9 — GET /auth/magic/verify?token=xxx.

    Validates the token (single-use, unexpired), upserts the tenant for
    that email, creates a session, sets the cookie, and redirects via
    the shared _post_login_redirect — paywall gate applies symmetrically
    to magic-link sign-ups.

    fdf1120f — this endpoint is opened directly in a browser, so failures
    return a real HTML page (not a blank JSON error) and the sign-in work is
    guarded so a transient error never renders a blank 500.
    """
    import logging as _logging
    from . import db as db_module

    raw = (token or "").strip()
    if not raw:
        return _magic_error_page(
            "This sign-in link is missing its token. Please request a new one.",
            status_code=400,
        )
    token_hash = hashlib.sha256(raw.encode()).hexdigest()

    db = request.app.state.db
    try:
        row = await db_module.consume_magic_token(db, token_hash)
    except Exception:
        _logging.getLogger("meridian.auth").exception("magic verify: token consume failed")
        return _magic_error_page(
            "Something went wrong verifying your link. Please request a new one.",
            status_code=500, title="Sign-in problem",
        )
    if row is None:
        # Don't reveal whether expired vs used vs nonexistent.
        return _magic_error_page(
            "This sign-in link has expired or was already used. Magic links work "
            "once and expire after a short time.",
            status_code=401, title="Link expired",
        )

    try:
        email = row["email"]
        tenant = await db_module.upsert_tenant(db, email=email)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=_SESSION_MAX_AGE_HOURS)
        ).isoformat()
        session = await db_module.create_user_session(
        db, tenant["id"], expires_at, *_session_meta(request)
    )
        cookie_value = _make_session_cookie(session["id"])
        redirect_to = await _post_login_redirect(tenant, db)
    except Exception:
        _logging.getLogger("meridian.auth").exception("magic verify: sign-in failed")
        return _magic_error_page(
            "Something went wrong signing you in. Please request a new link.",
            status_code=500, title="Sign-in problem",
        )

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
