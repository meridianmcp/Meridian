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
DEFAULT_EXTRACT_PROXY_PORT = 8810
# Server's proxy timeout is 30s (_PROXY_TIMEOUT); stay just under it so a slow
# local response surfaces as our error rather than the server's tunnel timeout.
_LOCAL_REQUEST_TIMEOUT = 28.0
_MAX_BACKOFF = 30.0


def _force_utf8_io() -> None:
    """Make stdio UTF-8 so the tunnel's Unicode status output can't crash.

    Windows consoles default to cp1252; printing the URLs, ✓ marks, and box
    characters in the startup banner would raise ``UnicodeEncodeError`` and kill
    the tunnel. Setting ``PYTHONIOENCODING`` propagates UTF-8 to the spawned
    proxy children, and reconfiguring the live streams fixes the already-started
    parent process (the env var alone is read only at interpreter startup).
    """
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — non-TextIOWrapper streams (e.g. captured in tests)
            pass


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


def _ws_extract_url(base_url: str, tenant_id: str, token: str) -> str:
    """Build the mcp-server-code-extractor tunnel WebSocket URL."""
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://"):]
    else:
        ws_base = base
    from urllib.parse import quote
    return f"{ws_base}/tunnel-extract/{tenant_id}?token={quote(token, safe='')}"


def _permanent_extract_url(base_url: str, tenant_id: str) -> str:
    """The URL for mcp-server-code-extractor — add to claude.ai once."""
    return f"{base_url.rstrip('/')}/extract/mcp/{tenant_id}/mcp"


def _find_uvx() -> "str | None":
    """Locate the ``uvx`` launcher (uv's ephemeral-tool runner)."""
    found = shutil.which("uvx")
    if found:
        return found
    # uv's standalone installer drops uvx in ~/.local/bin on every platform.
    name = "uvx.exe" if sys.platform == "win32" else "uvx"
    candidate = Path.home() / ".local" / "bin" / name
    if candidate.exists():
        return str(candidate)
    return None


def _resolve_extractor_inner_cmd() -> "list[str] | None":
    """Resolve the launcher for mcp-server-code-extractor (a **PyPI** package).

    It is published on PyPI, not npm. Preferred: ``uvx mcp-server-code-extractor``
    (zero install, ephemeral). Fallback: pip-install the package into the current
    interpreter's environment and run it as ``python -m code_extractor`` (the
    module is ``code_extractor``). Returns the inner-command token list, or None
    if neither path is available.
    """
    uvx = _find_uvx()
    if uvx:
        return [uvx, "mcp-server-code-extractor"]
    # Fallback: ensure the package is importable in this env, then run as a module.
    import importlib.util
    if importlib.util.find_spec("code_extractor") is None:
        print(
            "  code-extractor: uvx not found — pip installing mcp-server-code-extractor...",
            flush=True,
        )
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "mcp-server-code-extractor"],
                check=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"  warning: could not pip install mcp-server-code-extractor: {exc}",
                file=sys.stderr, flush=True,
            )
            return None
    return [sys.executable, "-m", "code_extractor"]


def _build_extractor_proxy_command(
    npx: str, inner_cmd: list[str], port: int = DEFAULT_EXTRACT_PROXY_PORT
) -> list[str]:
    """Wrap *inner_cmd* (the code-extractor launcher) in mcp-proxy on *port*.

    mcp-server-code-extractor is a PyPI package, so *inner_cmd* is the resolved
    Python launcher — e.g. ``[uvx, "mcp-server-code-extractor"]`` or
    ``[python, "-m", "code_extractor"]`` (see :func:`_resolve_extractor_inner_cmd`).
    The OUTER launcher is still ``npx`` because mcp-proxy itself is an npm tool.

    On Windows, ``--shell`` is added only when the inner launcher is a ``.cmd``/
    ``.bat`` shim (Node 24's CVE-2024-27980 mitigation blocks direct ``.cmd``
    spawns). ``uvx.exe`` / ``python.exe`` are real executables that spawn directly
    and preserve support for paths with spaces, so no shell is needed for them.
    """
    cmd = [npx, "-y", "mcp-proxy", "--port", str(port),
           "--server", "stream", "--stateless"]
    if sys.platform == "win32" and inner_cmd and inner_cmd[0].lower().endswith((".cmd", ".bat")):
        cmd.append("--shell")
    cmd += ["--", *inner_cmd]
    return cmd


def _managed_bin_dir() -> "Path":
    """~/.meridian/bin — where auto-downloaded binaries are installed."""
    return Path.home() / ".meridian" / "bin"


def _find_codebase_memory_mcp() -> str | None:
    """Return path to codebase-memory-mcp, checking PATH, npm global, then managed dir.

    ``shutil.which`` honours PATHEXT, so on Windows it already resolves the npm
    shim ``codebase-memory-mcp.cmd`` when ``%APPDATA%\\npm`` is on PATH. The
    explicit npm-global probe is a fallback for when it is not.
    """
    found = shutil.which("codebase-memory-mcp")
    if found:
        return found
    # Windows npm global install: %APPDATA%\npm\codebase-memory-mcp.cmd
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            shim = Path(appdata) / "npm" / "codebase-memory-mcp.cmd"
            if shim.exists():
                return str(shim)
    name = "codebase-memory-mcp.exe" if sys.platform == "win32" else "codebase-memory-mcp"
    managed = _managed_bin_dir() / name
    if managed.exists():
        return str(managed)
    return None


def _pick_release_asset(assets: list[dict]) -> "dict | None":
    """Pick the best GitHub release asset for the current platform and arch.

    Hard-excludes assets for other platforms before scoring so an arch-only
    match can never cause a cross-platform download (e.g. darwin-amd64 on
    Windows when no windows asset is present).
    """
    import platform as _platform

    machine = _platform.machine().lower()
    is_arm = machine in ("arm64", "aarch64")

    if sys.platform == "win32":
        os_kws = ["win", "windows"]
        os_exclude = ["linux", "darwin", "macos", "mac", "apple"]
    elif sys.platform == "darwin":
        os_kws = ["darwin", "macos", "mac", "apple"]
        # "win" is a substring of "darwin" — never use it as an exclusion keyword here
        os_exclude = ["linux", "windows", "msvc"]
    else:
        os_kws = ["linux"]
        os_exclude = ["darwin", "macos", "mac", "apple", "windows", "win"]

    arch_kws = ["aarch64", "arm64"] if is_arm else ["x86_64", "amd64", "x64"]

    def _score(name: str) -> int:
        n = name.lower()
        # Hard-exclude wrong-platform assets — never download a binary that
        # won't run on this OS, even if the arch matches.
        if any(kw in n for kw in os_exclude):
            return -100
        s = 0
        for kw in os_kws:
            if kw in n:
                s += 10
                break
        for kw in arch_kws:
            if kw in n:
                s += 5
                break
        if sys.platform == "win32" and n.endswith(".exe"):
            s += 3
        elif sys.platform != "win32" and not any(n.endswith(e) for e in (".exe", ".zip", ".tar.gz", ".tgz")):
            s += 1
        if any(n.endswith(e) for e in (".tar.gz", ".tgz", ".zip")):
            s -= 5
        return s

    candidates = [
        (a, _score(a["name"]))
        for a in assets
        if a.get("name") and a.get("browser_download_url")
    ]
    candidates.sort(key=lambda x: x[1], reverse=True)
    if not candidates or candidates[0][1] <= 0:
        return None
    return candidates[0][0]


async def _download_codebase_memory_mcp() -> "str | None":
    """Download the latest codebase-memory-mcp release for this platform.

    Saves to ~/.meridian/bin/ and makes the file executable. Returns the path
    on success, None on failure (error printed to stderr).
    """
    import httpx

    bin_dir = _managed_bin_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    bin_name = "codebase-memory-mcp.exe" if sys.platform == "win32" else "codebase-memory-mcp"
    dest = bin_dir / bin_name

    print("  code-intel: codebase-memory-mcp not found — downloading from GitHub...", flush=True)

    api_url = "https://api.github.com/repos/DeusData/codebase-memory-mcp/releases/latest"
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            r = await client.get(api_url, headers={"Accept": "application/vnd.github+json"})
            r.raise_for_status()
            release = r.json()

        assets = release.get("assets", [])
        asset = _pick_release_asset(assets)
        if asset is None:
            print(
                "  code-intel: no suitable binary found in the GitHub release — "
                "install codebase-memory-mcp manually and re-run `meridian --tunnel`.",
                file=sys.stderr, flush=True,
            )
            return None

        version = release.get("tag_name", "unknown")
        print(f"  code-intel: downloading {asset['name']} ({version})...", flush=True)

        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            r = await client.get(asset["browser_download_url"])
            r.raise_for_status()
            dest.write_bytes(r.content)

        if sys.platform != "win32":
            dest.chmod(dest.stat().st_mode | 0o111)  # make executable

        print(f"  code-intel: installed to {dest}", flush=True)
        return str(dest)
    except Exception as exc:
        print(f"  code-intel: download failed ({exc})", file=sys.stderr, flush=True)
        return None


async def _ensure_codebase_memory_mcp() -> "str | None":
    """Return path to codebase-memory-mcp, auto-downloading if not already installed."""
    found = _find_codebase_memory_mcp()
    if found:
        return found
    return await _download_codebase_memory_mcp()


def _build_code_proxy_command(
    npx: str, binary: str, port: int = DEFAULT_CODE_PROXY_PORT
) -> list[str]:
    """Build the mcp-proxy command wrapping codebase-memory-mcp on the given port.

    ``binary`` may be a native executable (the auto-downloaded ``.exe`` in
    ``~/.meridian/bin``) or an npm shim (``codebase-memory-mcp.cmd`` from a global
    npm install). On Windows a ``.cmd``/``.bat`` shim must be spawned through a
    shell (``--shell``) — mcp-proxy's direct spawn hits EINVAL under Node 24's
    CVE-2024-27980 mitigation. A real ``.exe`` spawns directly (and preserves
    support for paths with spaces), so ``--shell`` is added only for shims.
    """
    cmd = [npx, "-y", "mcp-proxy", "--port", str(port),
           "--server", "stream", "--stateless"]
    if sys.platform == "win32" and binary.lower().endswith((".cmd", ".bat")):
        cmd.append("--shell")
    cmd += ["--", binary]
    return cmd


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
# Auto-index helper (calls index_repository on codebase-memory-mcp proxy)
# ---------------------------------------------------------------------------

async def _index_code_dir(port: int, code_dir: str) -> None:
    """Wait for the code-intel proxy to start, then call index_repository on code_dir.

    Uses Streamable HTTP (MCP 2025-03-26): mcp-proxy handles the stdio lifecycle
    per POST, so a direct tools/call is sufficient — no client-side initialize needed.
    Failures are non-fatal (logged to stderr, tunnel continues).
    """
    import httpx

    local = f"http://127.0.0.1:{port}/mcp"
    probe = {"jsonrpc": "2.0", "id": "probe", "method": "tools/list", "params": {}}

    # Poll until the proxy is accepting connections (up to 60s).
    for _ in range(60):
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.post(local, json=probe,
                                 headers={"Content-Type": "application/json"})
                if r.status_code < 500:
                    break
        except Exception:
            pass
        await asyncio.sleep(1.0)
    else:
        print(
            f"  code-intel: proxy not ready after 60s — skipping auto-index of {code_dir}",
            file=sys.stderr, flush=True,
        )
        return

    payload = {
        "jsonrpc": "2.0",
        "id": "idx",
        "method": "tools/call",
        "params": {"name": "index_repository", "arguments": {"path": code_dir}},
    }
    try:
        async with httpx.AsyncClient(timeout=300.0) as c:
            r = await c.post(local, json=payload,
                             headers={"Content-Type": "application/json"})
        if r.status_code < 400:
            print(f"  code-intel: indexed {code_dir}", flush=True)
        else:
            print(
                f"  code-intel: index returned HTTP {r.status_code} for {code_dir}",
                file=sys.stderr, flush=True,
            )
    except Exception as exc:
        print(f"  code-intel: index failed for {code_dir}: {exc}",
              file=sys.stderr, flush=True)


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


# ---------------------------------------------------------------------------
# Local MCP config auto-update (.mcp.json / .cursor/mcp.json) — pure, unit tested
# ---------------------------------------------------------------------------

# Connector keys we inject; used for restore so we only touch our own entries.
TUNNEL_MCP_KEYS = ("meridian-fs", "meridian-code", "meridian-extractor")


def _tunnel_mcp_entries(base_url: str, tenant_id: str) -> dict[str, dict]:
    """The three HTTP MCP connector entries pointing at this tenant's tunnel."""
    return {
        "meridian-fs": {"type": "http", "url": _permanent_url(base_url, tenant_id)},
        "meridian-code": {"type": "http", "url": _permanent_code_url(base_url, tenant_id)},
        "meridian-extractor": {"type": "http", "url": _permanent_extract_url(base_url, tenant_id)},
    }


def _mcp_json_paths(cwd: "str | Path") -> list["Path"]:
    """MCP config files to update: `.mcp.json` always, `.cursor/mcp.json` if present.

    `.mcp.json` (Claude Code) is created if absent — that is the whole point of
    auto-update. `.cursor/mcp.json` is only touched when it already exists so we
    never create Cursor config for non-Cursor users.
    """
    cwd = Path(cwd)
    paths = [cwd / ".mcp.json"]
    cursor = cwd / ".cursor" / "mcp.json"
    if cursor.exists():
        paths.append(cursor)
    return paths


def _inject_mcp_entries(text: "str | None", entries: dict[str, dict]) -> str:
    """Merge *entries* under ``mcpServers`` in an existing `.mcp.json` body.

    *text* is the current file content (``None``/empty for a new file). Existing
    servers and other top-level keys are preserved. Returns the new file text.
    """
    data = {}
    if text:
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001 — malformed config: start clean rather than crash
            data = {}
    if not isinstance(data, dict):
        data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers.update(entries)
    data["mcpServers"] = servers
    return json.dumps(data, indent=2) + "\n"


def _install_mcp_json(
    cwd: "str | Path", base_url: str, tenant_id: str,
) -> list[tuple["Path", "str | None"]]:
    """Inject tunnel connector entries into local MCP config files.

    Returns a list of ``(path, original_text_or_None)`` snapshots for restore.
    ``original_text_or_None`` is ``None`` when we created the file. Failures on
    any single file are reported and skipped — never fatal to the tunnel.
    """
    entries = _tunnel_mcp_entries(base_url, tenant_id)
    snapshots: list[tuple[Path, str | None]] = []
    for path in _mcp_json_paths(cwd):
        existed = path.exists()
        original = path.read_text(encoding="utf-8") if existed else None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_inject_mcp_entries(original, entries), encoding="utf-8")
            snapshots.append((path, original))
        except Exception as exc:  # noqa: BLE001
            print(f"  warning: could not update {path}: {exc}", file=sys.stderr, flush=True)
    return snapshots


def _restore_mcp_json(snapshots: list[tuple["Path", "str | None"]]) -> None:
    """Undo :func:`_install_mcp_json`: restore originals, delete files we created."""
    for path, original in snapshots:
        try:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(original, encoding="utf-8")
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


async def run_tunnel(
    *,
    token: str | None = None,
    base_url: str | None = None,
    repo_path: str | None = None,
    port: int = DEFAULT_PROXY_PORT,
    code_port: int = DEFAULT_CODE_PROXY_PORT,
    extract_port: int = DEFAULT_EXTRACT_PROXY_PORT,
    code_dirs: list[str] | None = None,
) -> int:
    """Resolve config, start local proxies, and keep all tunnels up.

    Always starts a filesystem tunnel on *port*. Starts a codebase-memory-mcp
    tunnel on *code_port* (auto-installing if needed) and a code-extractor
    tunnel on *extract_port*. Blocks until interrupted (Ctrl-C). Returns a
    process exit code.

    If *code_dirs* is provided, calls ``index_repository`` on each path via the
    code-intel proxy after it starts — so the first session has a fully indexed
    codebase without any manual tool call.
    """
    _force_utf8_io()
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

    # 3. Optionally spawn codebase-memory-mcp proxy (auto-install if not found).
    proc_code: subprocess.Popen | None = None
    code_binary = await _ensure_codebase_memory_mcp()
    if code_binary is not None:
        # Ensure managed bin dir is on PATH so child processes can find the binary too.
        managed_bin = str(_managed_bin_dir())
        if managed_bin not in os.environ.get("PATH", ""):
            os.environ["PATH"] = managed_bin + os.pathsep + os.environ.get("PATH", "")
        cmd_code = _build_code_proxy_command(npx, code_binary, code_port)
        print(f"  code-intel proxy:  http://127.0.0.1:{code_port}", flush=True)
        try:
            proc_code = subprocess.Popen(cmd_code)
        except Exception as exc:
            print(f"  warning: could not start code-intel proxy: {exc}", file=sys.stderr)
            proc_code = None
    else:
        print(
            "  code-intel:        not available (codebase-memory-mcp could not be installed)",
            flush=True,
        )

    # 4. Spawn mcp-server-code-extractor proxy. It's a PyPI package (run via uvx,
    #    or pip-installed and run as `python -m code_extractor`), wrapped in
    #    mcp-proxy. None if the launcher can't be resolved.
    proc_extract = None
    extractor_inner = _resolve_extractor_inner_cmd()
    if extractor_inner is not None:
        cmd_extract = _build_extractor_proxy_command(npx, extractor_inner, extract_port)
        print(f"  code-extractor:    http://127.0.0.1:{extract_port}", flush=True)
        try:
            proc_extract = subprocess.Popen(cmd_extract)
        except Exception as exc:
            print(f"  warning: could not start code-extractor proxy: {exc}", file=sys.stderr)
            proc_extract = None
    else:
        print(
            "  code-extractor:    not available (uvx missing and pip install failed)",
            flush=True,
        )

    # 5. Print permanent URLs.
    print("", flush=True)
    print("  Permanent MCP URLs — add these to claude.ai once:", flush=True)
    print(f"    Filesystem:      {_permanent_url(base_url, tenant_id)}", flush=True)
    if proc_code is not None:
        print(f"    Code Intel:      {_permanent_code_url(base_url, tenant_id)}", flush=True)
    if proc_extract is not None:
        print(f"    Code Extractor:  {_permanent_extract_url(base_url, tenant_id)}", flush=True)
    print(f"  (SSE clients: {_sse_url(base_url, tenant_id)})", flush=True)
    print("", flush=True)

    # 5b. Auto-update local MCP client config so a co-located Claude Code / Cursor
    #     session picks up the fs/code/extractor connectors with zero manual edits.
    #     Restored to its original state on shutdown (step 8 finally block).
    mcp_snapshots: list[tuple[Path, str | None]] = []
    try:
        mcp_snapshots = _install_mcp_json(Path.cwd(), base_url, tenant_id)
    except Exception as exc:  # noqa: BLE001
        print(f"  warning: could not update local MCP config: {exc}", file=sys.stderr, flush=True)
    if mcp_snapshots:
        for path, _orig in mcp_snapshots:
            print(f"  Updated MCP config: {path}", flush=True)
        print(
            "    added connectors: meridian-fs, meridian-code, meridian-extractor "
            "(removed on Ctrl+C)",
            flush=True,
        )
        print(
            "    Other clients (e.g. Cursor → .cursor/mcp.json): add the three URLs above manually.",
            flush=True,
        )
        print("", flush=True)

    # 6. Auto-index code dirs via code-intel proxy (fire-and-forget background tasks).
    index_tasks: list[asyncio.Task] = []
    if proc_code is not None and code_dirs:
        for d in code_dirs:
            index_tasks.append(
                asyncio.ensure_future(_index_code_dir(code_port, str(Path(d).resolve())))
            )

    # 7. Run reconnect loops — filesystem always, code + extract when proxies started.
    ws_fs = _ws_url(base_url, tenant_id, token)
    ws_code = _ws_code_url(base_url, tenant_id, token)
    ws_extract = _ws_extract_url(base_url, tenant_id, token)
    tasks = [asyncio.ensure_future(_reconnect_loop(ws_fs, port, "fs"))]
    if proc_code is not None:
        tasks.append(asyncio.ensure_future(_reconnect_loop(ws_code, code_port, "code")))
    if proc_extract is not None:
        tasks.append(asyncio.ensure_future(_reconnect_loop(ws_extract, extract_port, "extract")))

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\ntunnel: shutting down", flush=True)
        return 0
    finally:
        # Restore local MCP config first so a Ctrl+C never leaves stale tunnel URLs.
        if mcp_snapshots:
            _restore_mcp_json(mcp_snapshots)
            print("  Restored local MCP config (removed tunnel connectors).", flush=True)
        for t in tasks + index_tasks:
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
        if proc_extract is not None:
            proc_extract.terminate()
            try:
                proc_extract.wait(timeout=5)
            except Exception:
                proc_extract.kill()
    return 0
