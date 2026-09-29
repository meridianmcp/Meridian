"""ece2ac0a — default-deny authentication gate for the hosted service.

Background
----------
Every REST route in this app does its own auth inside the handler body (no
route declares a ``Depends()``). Most project-data routes rely on
``_deps._db(request)`` to pick the caller's tenant DB, and until ece2ac0a that
resolver fell back to the shared control-plane DB when no credential resolved a
tenant -- so an anonymous ``GET /projects`` on the hosted service listed the
operator's real projects, and every write route accepted anonymous writes.

``_db()`` now fails closed (401) in hosted mode. This module is the second,
independent layer: an app-wide FastAPI dependency that, in hosted mode, refuses
any request to a route that is NOT on an explicit allowlist unless the request
carries a VALID credential (a live session cookie or a ``sk_meridian_`` API
token, resolved through ``_get_tenant_from_request``) or the demo cookie.

Why a dependency and an allowlist (not a middleware, not a denylist)
--------------------------------------------------------------------
* Default deny. A route added tomorrow is gated automatically, whether or not
  it remembers to call ``_db()`` -- the failure mode that produced the
  unauthenticated ``POST /tunnel/plugins/install`` / ``POST /admin/shutdown``
  routes. A denylist of "data prefixes" would silently miss the next one.
* Exact matching. The key is the matched route's *path template* plus the HTTP
  method (``("GET", "/projects/{project_id}")``), read from
  ``scope["route"]`` after routing -- not a hand-rolled prefix match on the raw
  URL, which is what makes a middleware allowlist brittle.
* Ordering. FastAPI resolves route dependencies before it reports body/query
  validation errors, so an anonymous caller gets 401, not a 422 that leaks the
  route's input schema.
* WebSockets and the ``/static`` mount are untouched: websocket routes keep
  their own token checks, and ``Mount`` has no dependencies.
* Self-hosted mode (``MERIDIAN_HOSTED`` unset) returns immediately -- the local
  single-user server stays open by design.

The two allowlists
------------------
``HOSTED_PUBLIC_ROUTES``
    Public by design: login/OAuth/discovery endpoints, marketing pages, health
    and status badges, installer scripts, the public changelog and waitlist
    form. None of them serves tenant data to an anonymous caller.

``HOSTED_SELF_AUTHENTICATED_ROUTES``
    Routes that enforce their OWN credential and must keep their exact current
    response to a credential-less request: webhook signatures (Stripe, GitHub
    Marketplace, project ``X-Meridian-Token``), the OAuth-aware ``/mcp`` and
    ``/mcp/sse`` endpoints (their 401 carries the ``resource_metadata``
    discovery header MCP clients need), hook routes (``registration_token`` in the body;
    contractually never 401), and pages that redirect to the login page rather
    than returning 401 (``/dashboard``, ``/oauth/authorize``, ...). Every entry
    was reviewed to confirm the handler itself refuses anonymous callers;
    ``tests/test_ece2ac0a_hosted_anon_fail_closed.py`` re-verifies that for each
    one against the live route table.

Both sets are asserted verbatim by the test file, so widening either one is a
deliberate two-place edit that shows up in review.

Known residual gap (documented, not closed here): the demo cookie is an
unsigned ``"1"`` that anyone can set. It passes this gate so the public demo
dashboard keeps working, and ``_db()`` routes it to the separate demo DB. A
future route that neither calls ``_db()`` nor checks the demo cookie itself
would therefore be reachable with a forged demo cookie; routes acting on the
server process use ``_deps._require_hosted_operator`` which refuses demo.
"""
from __future__ import annotations

from starlette.requests import HTTPConnection

from ._deps import (
    _DEMO_CONTEXT_COOKIE,
    _authentication_required,
    _get_tenant_from_request,
    _hosted_mode,
)

RouteKey = tuple[str, str]  # (HTTP method, route path template)


HOSTED_PUBLIC_ROUTES: frozenset[RouteKey] = frozenset({
    # --- Landing, legal, marketing, PWA assets -------------------------------
    ("GET", "/"),
    ("GET", "/robots.txt"),
    ("GET", "/favicon.ico"),
    ("GET", "/sw.js"),
    ("GET", "/manifest.webmanifest"),
    ("GET", "/sitemap.xml"),
    ("GET", "/terms"),
    ("GET", "/privacy"),
    ("GET", "/pricing"),          # reads only the caller's OWN session (email prefill)
    ("GET", "/onboarding"),       # reads only the caller's OWN session (email prefill)
    ("GET", "/install-mcp"),
    ("GET", "/tools"),
    ("GET", "/setup"),            # redirect only
    ("GET", "/blog"),             # published posts only
    ("GET", "/blog/{slug}"),
    ("GET", "/waitlist-pending"),
    ("GET", "/changelog"),        # via _deps.list_public_changelog_entries
    ("GET", "/api/changelog-entries"),
    ("POST", "/waitlist"),        # via _deps.add_public_waitlist_entry (write-only)
    # --- OAuth / agent discovery metadata ------------------------------------
    ("GET", "/.well-known/oauth-authorization-server"),
    ("GET", "/.well-known/openid-configuration"),
    ("GET", "/.well-known/oauth-protected-resource"),
    ("GET", "/.well-known/agent.json"),
    ("POST", "/.well-known/agent.json"),
    # --- OAuth flow endpoints (must be reachable before login) ---------------
    ("POST", "/oauth/register"),
    ("POST", "/oauth/device"),
    ("POST", "/oauth/token"),     # validates the grant itself
    ("GET", "/oauth/device-callback"),
    # --- Login flows ---------------------------------------------------------
    ("GET", "/auth/login"),
    ("GET", "/auth/callback"),
    ("GET", "/auth/google/login"),
    ("GET", "/auth/github/login"),
    ("GET", "/auth/github/callback"),
    ("GET", "/auth/microsoft/login"),
    ("GET", "/auth/microsoft/callback"),
    ("GET", "/auth/email-required"),
    ("GET", "/auth/logout"),
    ("POST", "/auth/magic"),
    ("GET", "/auth/magic/verify"),  # single-use token
    ("GET", "/auth/tunnel-poll"),    # protected by its device code
    ("GET", "/admin/login"),
    ("POST", "/admin/login"),
    ("POST", "/__gate__"),
    ("GET", "/demo"),                # sets the demo cookie -> separate demo DB
    ("POST", "/demo-auth"),
    # --- Health / status badges (booleans and counts only) -------------------
    ("GET", "/health"),
    ("GET", "/health/deep"),
    ("GET", "/failover-status"),
    ("GET", "/status/server"),
    ("GET", "/status/tools"),
    ("GET", "/status/sessions"),
    ("GET", "/setup/health"),
    ("GET", "/admin/__error_test"),  # 404 unless an env flag is set
    # --- Docs and MCP discovery ----------------------------------------------
    ("GET", "/mcp"),                 # discovery JSON / SSE redirect only
    ("OPTIONS", "/mcp/sse"),         # CORS preflight
    ("GET", "/mcp/quickstart"),
    ("GET", "/mcp/tools-doc"),
    ("GET", "/api-reference-doc"),
    ("GET", "/config"),
    ("GET", "/config/api-key"),
    # --- Installer scripts and stateless hook utilities ----------------------
    ("GET", "/install.sh"),
    ("GET", "/install.ps1"),
    ("GET", "/install-windows.ps1"),
    ("GET", "/install_tunnel.sh"),
    ("GET", "/install_tunnel.ps1"),
    ("GET", "/install_watcher.sh"),
    ("GET", "/install_watcher.ps1"),
    ("GET", "/hooks.sh"),
    ("GET", "/hooks.ps1"),
    ("GET", "/hooks_install.ps1"),
    ("GET", "/hooks/diagnostics"),
    ("POST", "/pkg-guard/check"),
    # --- Tunnel utilities with no tenant data --------------------------------
    ("GET", "/tunnel/registry"),
    ("GET", "/tunnel/plugins/check"),
    ("POST", "/tunnel/openai/diagnostics/{tenant_id}"),
    # --- Return empty defaults to anonymous callers --------------------------
    ("GET", "/me"),
    ("GET", "/me/workspaces"),
    ("GET", "/tunnel/plugins"),
    ("GET", "/tunnel/filesystem-roots"),
    # --- Static content, no tenant data --------------------------------------
    ("GET", "/control-plane/artifacts"),
    ("GET", "/projects/{project_id}/agent-instructions/default"),
})


_TUNNEL_PROXY_SLOTS = (
    "fs", "code", "extract", "ppt", "word", "dc", "docs", "zotero", "outputs", "debug",
)

HOSTED_SELF_AUTHENTICATED_ROUTES: frozenset[RouteKey] = frozenset({
    # --- Own-credential webhooks (signature / per-project token) -------------
    ("POST", "/webhooks/stripe"),
    ("POST", "/webhooks/github-marketplace"),
    ("POST", "/projects/{project_id}/events"),     # X-Meridian-Token
    # --- MCP endpoints: OAuth-aware 401 with resource_metadata ---------------
    ("POST", "/mcp"),
    ("POST", "/mcp/openai"),
    # /mcp/sse (ece2ac0a): the GET needs a credential; a message POST needs a
    # credential OR a live session id opened by that authenticated GET (the
    # HTTP+SSE transport's endpoint-URL capability). Both answer 401 with the
    # same OAuth discovery challenge as POST /mcp -- see server.mcp_sse_*.
    ("GET", "/mcp/sse"),
    ("POST", "/mcp/sse"),
    # --- Hook routes: Bearer OR body registration_token; never 401 anon ------
    ("POST", "/hooks/session-start"),
    ("POST", "/hooks/stop"),
    # --- Session-cookie pages that redirect to login instead of 401 ----------
    ("GET", "/dashboard"),
    ("GET", "/admin"),
    ("GET", "/activate"),
    ("POST", "/activate"),
    ("GET", "/oauth/authorize"),
    ("GET", "/checkout"),
    ("GET", "/billing/portal"),
    ("POST", "/billing/portal"),
    ("GET", "/auth/github/repo-connect"),
    ("GET", "/auth/github/repo-callback"),
    ("GET", "/auth/hooks-connect"),
    ("GET", "/auth/tunnel-connect"),
    ("POST", "/auth/tunnel-connect"),
    ("GET", "/auth/install"),
    ("GET", "/workspace/accept"),
    # --- Account / token management (cookie or Bearer, own 401/403) ----------
    ("GET", "/auth/me"),
    ("GET", "/auth/hooks-status"),
    ("GET", "/auth/tokens"),
    ("POST", "/auth/tokens"),
    ("DELETE", "/auth/tokens/{token_id}"),
    ("DELETE", "/api/keys/orphaned"),
    ("GET", "/account/sessions"),
    ("POST", "/account/sessions/{session_id}/revoke"),
    ("POST", "/account/delete"),
    ("GET", "/export/my-data"),
    ("POST", "/feedback"),
    ("GET", "/settings/mcp-config"),
    ("GET", "/settings/notifications"),
    ("PATCH", "/settings/notifications"),
    ("GET", "/settings/usage"),
    ("PATCH", "/settings/usage"),
    ("GET", "/projects/{project_id}/registered-machines"),
    ("DELETE", "/projects/{project_id}/registered-machines/{machine_id}"),
    # --- Workspace membership (session cookie + role checks) -----------------
    ("POST", "/workspace/connect-db"),
    ("POST", "/workspace/invite"),
    ("POST", "/workspace/invite/{member_id}/resend"),
    ("GET", "/workspace/members"),
    ("PATCH", "/workspace/members/{member_id}"),
    ("DELETE", "/workspace/members/{member_id}"),
    # --- GitHub integration (hosted-only, _get_tenant_from_request -> 401) ---
    ("GET", "/github/connections"),
    ("DELETE", "/github/connections/{account_login}"),
    ("GET", "/projects/{project_id}/github/status"),
    ("GET", "/projects/{project_id}/github/repos"),
    ("GET", "/projects/{project_id}/github/branches"),
    ("POST", "/projects/{project_id}/github/connect"),
    ("DELETE", "/projects/{project_id}/github/disconnect"),
    ("PATCH", "/projects/{project_id}/github/account"),
    ("POST", "/projects/{project_id}/github/push-mcp-template"),
    ("GET", "/projects/{project_id}/repo-image"),
    # --- Operator-only admin surface (session + is_admin_db [+ password]) ----
    ("GET", "/admin/health"),
    ("GET", "/admin/stats"),
    ("GET", "/admin/waitlist"),
    ("DELETE", "/admin/waitlist/{entry_id}"),
    ("POST", "/admin/tenants/{tenant_id}/reset-provisioning"),
    ("GET", "/admin/blog/posts"),
    ("POST", "/admin/blog/posts"),
    ("GET", "/admin/blog/posts/{post_id}"),
    ("DELETE", "/admin/blog/posts/{post_id}"),
    ("POST", "/admin/blog/posts/{post_id}/publish"),
    ("POST", "/admin/blog/posts/{post_id}/unpublish"),
    ("POST", "/admin/blog/generate-draft"),
    ("POST", "/api/admin/changelog-entries"),
    ("PATCH", "/api/admin/changelog-entries/{entry_id}"),
    ("DELETE", "/api/admin/changelog-entries/{entry_id}"),
    ("POST", "/config/connections"),
    ("DELETE", "/config/connections/{name}"),
    # --- Tunnel control plane (Bearer sk_meridian_ -> tenant, own 401/403) ---
    ("GET", "/tunnel/manifest"),
    ("POST", "/tunnel/active-repo"),
    ("POST", "/tunnel/refresh"),
    ("GET", "/tunnel/status/{tenant_id}"),
    ("PUT", "/tunnel/plugins"),
    ("POST", "/tunnel/plugins/custom"),
    ("DELETE", "/tunnel/plugins/custom"),
    ("POST", "/tunnel/filesystem-roots"),
    ("DELETE", "/tunnel/filesystem-roots"),
    # --- Tunnel HTTP MCP proxies: _authorize_tunnel_proxy_caller requires the
    # caller's own tenant to equal the {tenant_id} path segment (5de3d422). ---
    *(
        (method, f"/{slot}/mcp/{{tenant_id}}{suffix}")
        for slot in _TUNNEL_PROXY_SLOTS
        for suffix in ("", "/{rest:path}")
        for method in ("GET", "POST", "OPTIONS")
    ),
})


def is_gate_exempt(method: str, route_path: str) -> bool:
    """True when ``(method, route_path)`` is on either explicit allowlist."""
    key = (method.upper(), route_path)
    return key in HOSTED_PUBLIC_ROUTES or key in HOSTED_SELF_AUTHENTICATED_ROUTES


async def hosted_route_gate(conn: HTTPConnection) -> None:
    """App-wide dependency: in hosted mode, 401 any non-allowlisted route
    unless the request carries a valid session cookie / API token, or the
    demo cookie (which ``_db()`` routes to the separate demo DB).

    ``HTTPConnection`` (not ``Request``) so FastAPI can also resolve it for
    websocket routes, which are skipped -- they authenticate their own token.
    """
    if not _hosted_mode():
        return
    if conn.scope.get("type") != "http":
        return
    route = conn.scope.get("route")
    route_path = getattr(route, "path", None)
    method = str(conn.scope.get("method") or "").upper()
    if route_path and is_gate_exempt(method, route_path):
        return
    # Fail closed if the route cannot be identified: treat it as gated.
    if conn.cookies.get(_DEMO_CONTEXT_COOKIE):
        return
    tenant = await _get_tenant_from_request(conn)  # type: ignore[arg-type]
    if tenant is None or not tenant.get("id"):
        raise _authentication_required()
