"""OAuth / magic-link sign-in routes — extracted from server.py.

Thin wrappers over meridian.hosted; the actual provider flows (Google / GitHub /
Microsoft / magic link) live there. /auth/magic is rate-limited.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .._deps import _db, _rate_limit

router = APIRouter()


@router.get("/auth/login")
async def auth_login(request: Request):
    """Serve sign-in page with Google and GitHub OAuth buttons."""
    from ..hosted import auth_login as _auth_login
    return await _auth_login(request)


@router.get("/auth/google/login")
async def auth_google_login(request: Request):
    """Redirect browser directly to Google OAuth consent page."""
    from ..hosted import auth_google_login as _auth_google_login
    return await _auth_google_login(request)


@router.get("/auth/callback")
async def auth_callback(request: Request):
    """Handle Google OAuth callback — create/update tenant, set session cookie."""
    from ..hosted import auth_callback as _auth_callback
    return await _auth_callback(request)


@router.get("/auth/github/login")
async def auth_github_login(request: Request):
    """Redirect browser to GitHub OAuth consent page."""
    from ..hosted import auth_github_login as _auth_github_login
    return await _auth_github_login(request)


@router.get("/auth/github/callback")
async def auth_github_callback(request: Request):
    """Handle GitHub OAuth callback — create/update tenant, set session cookie."""
    from ..hosted import auth_github_callback as _auth_github_callback
    return await _auth_github_callback(request)


@router.get("/auth/github/repo-connect")
async def auth_github_repo_connect(request: Request):
    """Redirect browser to GitHub OAuth for repo connection."""
    from ..hosted import auth_github_repo_connect as _auth_github_repo_connect
    return await _auth_github_repo_connect(request)


@router.get("/auth/github/repo-callback")
async def auth_github_repo_callback(request: Request):
    """Handle GitHub repo-connect callback and store repo access."""
    from ..hosted import auth_github_repo_callback as _auth_github_repo_callback
    db = await _db(request)
    return await _auth_github_repo_callback(request, db)


@router.get("/auth/microsoft/login")
async def auth_microsoft_login(request: Request):
    """Redirect browser to Microsoft OAuth consent page."""
    from ..hosted import auth_microsoft_login as _auth_microsoft_login
    return await _auth_microsoft_login(request)


@router.get("/auth/microsoft/callback")
async def auth_microsoft_callback(request: Request):
    """Handle Microsoft OAuth callback — create/update tenant, set session cookie."""
    from ..hosted import auth_microsoft_callback as _auth_microsoft_callback
    return await _auth_microsoft_callback(request)


@router.get("/auth/email-required")
async def auth_email_required(request: Request) -> HTMLResponse:
    """Shown when OAuth provider returned no usable email (e.g. GitHub with private email)."""
    provider = request.query_params.get("provider", "your provider")
    html = f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Email required — Meridian</title>
<style>body{{font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 20px;color:#e8eaed;background:#0d1117}}
h2{{color:#58a6ff}}p{{color:#8b949e;line-height:1.6}}a{{color:#58a6ff}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:24px;margin-top:24px}}
</style></head><body>
<div class="card">
<h2>Email address required</h2>
<p>We couldn't get a verified email from {provider}. Meridian needs your email to create your account.</p>
<p>To fix this:<br>
&nbsp;&nbsp;1. Go to <a href="https://github.com/settings/emails" target="_blank" rel="noopener">github.com/settings/emails</a><br>
&nbsp;&nbsp;2. Add and verify a primary email address<br>
&nbsp;&nbsp;3. <a href="/auth/github/login">Try signing in again</a></p>
<p>Or <a href="/auth/login">use a magic link</a> to sign in with your email directly.</p>
</div>
</body></html>"""
    return HTMLResponse(html)


@router.get("/auth/logout")
async def auth_logout(request: Request):
    """Clear session cookie and delete DB session."""
    from ..hosted import auth_logout as _auth_logout
    return await _auth_logout(request)


@router.post("/auth/magic")
@_rate_limit("5/minute")
async def auth_magic_request(request: Request):
    """v0.9 — request a magic-link email. Rate-limited.

    Body: ``{"email": "user@example.com"}``. Sends a single-use signed
    link via Resend. Idempotent within the 24-hour token window — if a
    valid unused token exists for this email, returns success without
    sending a duplicate email.
    """
    from ..hosted import auth_magic_request as _impl
    return await _impl(request)


@router.get("/auth/magic/verify")
async def auth_magic_verify(request: Request, token: str = ""):
    """v0.9 — consume a magic-link token, create a session, redirect.

    Single-use: marks ``used_at`` on success so re-clicking the same
    link doesn't re-authenticate. New tenants flow through the OAuth
    paywall check — redirected to /pricing?signup=1 if no Stripe
    subscription yet.
    """
    from ..hosted import auth_magic_verify as _impl
    return await _impl(request, token)


@router.get("/setup/health")
async def setup_health(request: Request):
    """13583103 — self-hosted diagnostics: which auth providers are configured
    and which env vars are still missing. Public (reveals no secret values), so
    a self-hoster can curl it to see why sign-in isn't working."""
    from ..hosted import auth_setup_health as _impl
    return _impl()
