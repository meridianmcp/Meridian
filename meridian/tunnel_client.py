"""Client side of the Pro permanent tunnel (`meridian --tunnel`).

Run once on the machine that holds your repo::

    meridian --tunnel

It:
  1. Reads your API token + server URL from CLI args or the environment.
  2. Calls ``GET /me`` to resolve your ``tenant_id`` and confirm a Pro plan.
  3. Spawns a local ``mcp-proxy`` wrapping ``@modelcontextprotocol/server-filesystem``
     pointed at your repo, listening on ``127.0.0.1:8808``.
  4. Opens a persistent WebSocket to ``wss://<server>/tunnel/{tenant_id}`` and
     relays every proxied request to the local proxy, returning the response.
  5. Auto-reconnects with exponential backoff if the socket drops.
  6. Prints the permanent URL to paste into claude.ai once:
     ``https://<server>/fs/mcp/{tenant_id}``

The server side lives in ``meridian/routes/tunnel.py`` — the framing here
mirrors the protocol documented there.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_BASE_URL = "https://usemeridian.us"
DEFAULT_PROXY_PORT = 8808
DEFAULT_CODE_PROXY_PORT = 8809
# Server's proxy timeout is 30s (_PROXY_TIMEOUT); stay just under it so a slow
# local response surfaces as our error rather than the server's tunnel timeout.
_LOCAL_REQUEST_TIMEOUT = 28.0
_MAX_BACKOFF = 30.0


# ---------------------------------------------------------------------------
# Config resolution (pure — unit tested)
# ---------------------------------------------------------------------------

def _resolve_token(arg_token: str | None = None) -> str:
    """Resolve the API token: CLI arg > MERIDIAN_API_KEY > BEARER_TOKEN.

    A leading ``Bearer `` prefix (e.g. copied from a header) is stripped.
    """
    token = (
        arg_token
        or os.environ.get("MERIDIAN_API_KEY")
        or os.environ.get("BEARER_TOKEN")
        or ""
    ).strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _resolve_base_url(arg_url: str | None = None) -> str:
    """Resolve the server base URL: CLI arg > MERIDIAN_URL > default."""
    url = (arg_url or os.environ.get("MERIDIAN_URL") or DEFAULT_BASE_URL).strip()
    return url.rstrip("/")


def _ws_url(base_url: str, tenant_id: str, token: str) -> str:
    """Build the tunnel WebSocket URL with the token as a query param.

    The token is passed via ``?token=`` (which the server accepts) rather than
    an Authorization header so we don't depend on the ``websockets`` version's
    header kwarg name (``extra_headers`` vs ``additional_headers``).
    """
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://"):]
    else:
        ws_base = base
    from urllib.parse import quote
    return f"{ws_base}/tunnel/{tenant_id}?token={quote(token, safe='')}"


def _permanent_url(base_url: str, tenant_id: str) -> str:
    """The URL the user adds to claude.ai once.

    Points at the mcp-proxy **Streamable HTTP** transport (`/mcp`), NOT the
    proxy root: mcp-proxy serves transports at `/mcp` (streamable) and `/sse`
    (SSE) and returns 404 for `/`. The server route `/fs/mcp/{tenant_id}/{rest}`
    relays the `/mcp` suffix straight to the local proxy.
    """
    return f"{base_url.rstrip('/')}/fs/mcp/{tenant_id}/mcp"


def _sse_url(base_url: str, tenant_id: str) -> str:
    """SSE-transport variant of the permanent URL, for older MCP clients."""
    return f"{base_url.rstrip('/')}/fs/mcp/{tenant_id}/sse"


def _ws_code_url(base_url: str, tenant_id: str, token: str) -> str:
    """Build the codebase-memory-mcp tunnel WebSocket URL."""
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://"):]
    else:
        ws_base = base
    from urllib.parse import quote
    return f"{ws_base}/tunnel-code/{tenant_id}?token={quote(token, safe='')}"


def _permanent_code_url(base_url: str, tenant_id: str) -> str:
    """The URL for codebase-memory-mcp — add to claude.ai once."""
    return f"{base_url.rstrip('/')}/code/mcp/{tenant_id}/mcp"


def _find_codebase_memory_mcp() -> str | None:
    """Return the path to the codebase-memory-mcp binary, or None if not installed."""
    return shutil.which("codebase-memory-mcp")


def _build_code_proxy_command(
    npx: str, port: int = DEFAULT_CODE_PROXY_PORT
) -> list[str] | None:
    """Build the mcp-proxy command wrapping codebase-memory-mcp on the given port.

    Returns None when codebase-memory-mcp is not installed — caller skips the
    code tunnel gracefully.
    """
    binary = _find_codebase_memory_mcp()
    if binary is None:
        return None
    # codebase-memory-mcp is a native binary, not an npm package — no --shell needed.
    return [npx, "-y", "mcp-proxy", "--port", str(port),
            "--server", "stream", "--stateless", "--", binary]


def _find_npx() -> str:
    """Locate the npx launcher.

    On Windows a bare ``npx`` resolves to the extension-less shell shim, which
    fails when spawned without a shell (``[WinError 193]``). We need the full
    path to ``npx.cmd``. Fall back to the standard npm global location.
    """
    if sys.platform == "win32":
        found = shutil.which("npx.cmd") or shutil.which("npx")
        if found:
            return found
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidate = Path(appdata) / "npm" / "npx.cmd"
            if candidate.exists():
                return str(candidate)
        return "npx.cmd"
    return shutil.which("npx") or "npx"


def _build_proxy_command(
    npx: str, repo_path: str, port: int = DEFAULT_PROXY_PORT
) -> list[str]:
    """Build the ``mcp-proxy`` command that wraps the filesystem MCP server.

    Roughly::

        npx -y mcp-proxy [--shell] --port <port> -- \
            npx -y @modelcontextprotocol/server-filesystem <repo_path>

    The OUTER ``npx`` is the resolved launcher (full ``npx.cmd`` path on Windows,
    so Python's ``subprocess`` can start it). The INNER command is bare ``npx``,
    resolved by mcp-proxy.

    On Windows we pass ``--shell`` so mcp-proxy spawns the inner ``npx`` through
    cmd.exe. Without it, mcp-proxy's direct spawn fails two ways on modern Node:
    bare ``npx`` → ENOENT, and a full ``npx.cmd`` path → EINVAL (Node 24's
    CVE-2024-27980 mitigation refuses to spawn ``.cmd``/``.bat`` without a shell).

    Note: with ``--shell`` mcp-proxy concatenates args unescaped, so a
    ``repo_path`` containing spaces is not yet supported on Windows.
    """
    # --server stream: serve only Streamable HTTP (/mcp), no SSE.
    # --stateless: each POST is handled independently — required for the
    #   tunnel relay's one-shot request/response model (no persistent SSE pipe).
    cmd = [npx, "-y", "mcp-proxy", "--port", str(port),
           "--server", "stream", "--stateless"]
    if sys.platform == "win32":
        cmd.append("--shell")
    cmd += ["--", "npx", "-y", "@modelcontextprotocol/server-filesystem", repo_path]
    return cmd


# ---------------------------------------------------------------------------
# Request relay (mostly pure — unit tested with httpx MockTransport)
# ---------------------------------------------------------------------------

async def _relay_request(http_client, local_base: str, msg: dict) -> dict:
    """Proxy one server ``request`` message to the local mcp-proxy.

    Returns a ``response`` message (same correlation id) with a base64 body,
    matching the protocol in ``routes/tunnel.py``. Local failures come back as
    a 502 so the server resolves its pending future instead of timing out.
    """
    req_id = msg.get("id")
    method = (msg.get("method") or "GET").upper()
    path = msg.get("path") or "/"
    query = msg.get("query") or ""
    headers = dict(msg.get("headers") or {})
    # Drop any stale Host — httpx sets it from the local target.
    headers = {k: v for k, v in headers.items() if k.lower() != "host"}
    body_b64 = msg.get("body")
    body = base64.b64decode(body_b64) if body_b64 else None

    url = local_base.rstrip("/") + path
    if query:
        url += ("&" if "?" in url else "?") + query.lstrip("?")

    try:
        resp = await http_client.request(
            method, url, headers=headers, content=body,
            timeout=_LOCAL_REQUEST_TIMEOUT,
        )
        resp_body = resp.content or b""
        return {
            "type": "response",
            "id": req_id,
            "status": resp.status_code,
            "headers": dict(resp.headers),
            "body": base64.b64encode(resp_body).decode() if resp_body else "",
        }
    except Exception as exc:  # local proxy down / timeout / bad response
        err = json.dumps({"error": f"local proxy error: {exc}"}).encode()
        return {
            "type": "response",
            "id": req_id,
            "status": 502,
            "headers": {"content-type": "application/json"},
            "body": base64.b64encode(err).decode(),
        }


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

async def _fetch_me(base_url: str, token: str) -> dict:
    """GET /me and return the JSON body (raises on transport/HTTP error)."""
    import httpx

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{base_url}/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        return r.json()


async def _run_connection(ws_url: str, port: int, label: str = "fs") -> None:
    """Hold one WebSocket session open, relaying requests until it drops."""
    import httpx
    import websockets

    local_base = f"http://127.0.0.1:{port}"
    async with websockets.connect(ws_url, max_size=None, ping_interval=20) as ws:
        print(f"tunnel:{label}: connected", flush=True)
        async with httpx.AsyncClient() as http_client:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "ping":
                    continue
                if msg.get("type") == "request":
                    resp = await _relay_request(http_client, local_base, msg)
                    await ws.send(json.dumps(resp))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def _reconnect_loop(ws_url: str, port: int, label: str) -> None:
    """Keep one tunnel alive, reconnecting with exponential backoff."""
    backoff = 1.0
    while True:
        try:
            await _run_connection(ws_url, port, label)
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                f"tunnel:{label}: disconnected ({exc}); reconnecting in {backoff:.0f}s",
                file=sys.stderr,
                flush=True,
            )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF)


async def run_tunnel(
    *,
    token: str | None = None,
    base_url: str | None = None,
    repo_path: str | None = None,
    port: int = DEFAULT_PROXY_PORT,
    code_port: int = DEFAULT_CODE_PROXY_PORT,
) -> int:
    """Resolve config, start local proxies, and keep both tunnels up.

    Starts a filesystem tunnel on *port* (always) and a codebase-memory-mcp
    tunnel on *code_port* (only if the binary is installed). Blocks until
    interrupted (Ctrl-C). Returns a process exit code.
    """
    token = _resolve_token(token)
    if not token:
        print(
            "error: no API token. Pass --token or set MERIDIAN_API_KEY "
            "(or BEARER_TOKEN).",
            file=sys.stderr,
        )
        return 2

    base_url = _resolve_base_url(base_url)
    repo_path = str(Path(repo_path or Path.home()).resolve())

    # 1. Resolve tenant_id + plan from /me.
    try:
        me = await _fetch_me(base_url, token)
    except Exception as exc:
        print(f"error: could not reach {base_url}/me: {exc}", file=sys.stderr)
        return 1

    tenant_id = me.get("tenant_id")
    if not tenant_id:
        print(
            "error: /me returned no tenant_id — is this a hosted account with "
            "a valid token?",
            file=sys.stderr,
        )
        return 1

    plan = me.get("plan") or "free"
    if plan not in ("pro", "admin") and not me.get("is_internal"):
        print(
            f"error: the tunnel is a Pro feature; your plan is '{plan}'. "
            "Upgrade at " + base_url + "/pricing",
            file=sys.stderr,
        )
        return 1

    # 2. Spawn filesystem proxy.
    npx = _find_npx()
    cmd_fs = _build_proxy_command(npx, repo_path, port)
    print(f"meridian tunnel: serving {repo_path}", flush=True)
    print(f"  filesystem proxy: http://127.0.0.1:{port}", flush=True)
    try:
        proc_fs = subprocess.Popen(cmd_fs)
    except FileNotFoundError:
        print(
            f"error: could not launch npx ({npx!r}). Is Node.js installed and "
            "on PATH?",
            file=sys.stderr,
        )
        return 1

    # 3. Optionally spawn codebase-memory-mcp proxy.
    proc_code: subprocess.Popen | None = None
    cmd_code = _build_code_proxy_command(npx, code_port)
    if cmd_code is not None:
        print(f"  code-intel proxy:  http://127.0.0.1:{code_port}", flush=True)
        try:
            proc_code = subprocess.Popen(cmd_code)
        except Exception as exc:
            print(f"  warning: could not start code-intel proxy: {exc}", file=sys.stderr)
            proc_code = None
    else:
        print(
            "  code-intel:        not available (install codebase-memory-mcp to enable)",
            flush=True,
        )

    # 4. Print permanent URLs.
    print("", flush=True)
    print("  Permanent MCP URLs — add these to claude.ai once:", flush=True)
    print(f"    Filesystem:  {_permanent_url(base_url, tenant_id)}", flush=True)
    if proc_code is not None:
        print(f"    Code intel:  {_permanent_code_url(base_url, tenant_id)}", flush=True)
    print(f"  (SSE clients: {_sse_url(base_url, tenant_id)})", flush=True)
    print("", flush=True)

    # 5. Run reconnect loops — filesystem always, code only when proxy started.
    ws_fs = _ws_url(base_url, tenant_id, token)
    ws_code = _ws_code_url(base_url, tenant_id, token)
    tasks = [asyncio.ensure_future(_reconnect_loop(ws_fs, port, "fs"))]
    if proc_code is not None:
        tasks.append(asyncio.ensure_future(_reconnect_loop(ws_code, code_port, "code")))

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\ntunnel: shutting down", flush=True)
        return 0
    finally:
        for t in tasks:
            t.cancel()
        proc_fs.terminate()
        try:
            proc_fs.wait(timeout=5)
        except Exception:
            proc_fs.kill()
        if proc_code is not None:
            proc_code.terminate()
            try:
                proc_code.wait(timeout=5)
            except Exception:
                proc_code.kill()
    return 0
