"""Entrypoint for ``python -m meridian``.

Default: run the FastAPI HTTP server on port 7878. Useful for local
development, the demo script, and any HTTP client.

With ``--mcp``: run the MCP stdio server. This is the mode you wire into
Claude Desktop / Claude Code via an ``mcpServers`` config block, e.g.

```json
{
  "mcpServers": {
    "meridian": {
      "command": "pixi",
      "args": ["run", "python", "-m", "meridian", "--mcp"],
      "cwd": "/absolute/path/to/meridian/repository"
    }
  }
}
```
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import subprocess
import sys

# Allow running as `python meridian --tunnel` (script mode) in addition to
# the canonical `python -m meridian --tunnel` (module mode).
# Without this, all relative imports (from .tunnel_client import ...) raise
# "attempted relative import with no known parent package".
if __package__ is None or __package__ == "":
    _pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _pkg_dir not in sys.path:
        sys.path.insert(0, _pkg_dir)
    __package__ = "meridian"


def _kill_port(port: int) -> None:
    """Kill any process listening on the given port before starting.

    The free-port probe uses a short timeout so a dropped SYN (e.g. a firewall
    that black-holes rather than refuses) can't stall startup for seconds per
    port. A refused connection still returns instantly; a timeout is treated as
    "free" so we never block. (a887155d)
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        try:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return  # port is free (connection refused)
        except OSError:
            return  # timed out / unreachable → treat as free, don't hang
    if sys.platform == "win32":
        result = subprocess.run(
            f"netstat -ano | findstr :{port}",
            shell=True, capture_output=True, text=True,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts and parts[-1].isdigit():
                pid = int(parts[-1])
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
                break
    else:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"], capture_output=True, text=True
        )
        for pid_str in result.stdout.split():
            if pid_str.isdigit():
                try:
                    os.kill(int(pid_str), signal.SIGTERM)
                except OSError:
                    pass

# psycopg3 requires SelectorEventLoop on Windows (ProactorEventLoop not supported).
# Must be set before any asyncio.run() or uvicorn.run() call.
if sys.platform == "win32":
    import selectors
    # psycopg3 requires SelectorEventLoop — override Windows default ProactorEventLoop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
    asyncio.set_event_loop(loop)


def _ensure_event_loop() -> "asyncio.AbstractEventLoop":
    """Return the thread's event loop, creating one if none is set or it's closed.

    Python 3.12 makes ``asyncio.get_event_loop()`` raise ``RuntimeError`` when no
    loop is set for the current thread (e.g. after another test closed it under
    pytest-xdist, or on a non-Windows entrypoint where the module-scope loop
    above never ran). The CLI dispatch drives its own loop via
    ``run_until_complete``, so it just needs *a* usable loop regardless of
    ambient state — this keeps that robust without changing Windows behaviour
    (the module-scope SelectorEventLoop is returned unchanged when present).
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("event loop is closed")
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


def main(argv: list[str] | None = None) -> int:
    """CLI dispatch: HTTP server by default, MCP stdio with ``--mcp``."""
    parser = argparse.ArgumentParser(
        prog="meridian",
        description="Multi-session Claude coordinator.",
    )
    parser.add_argument(
        "--mcp",
        action="store_true",
        help="Run the MCP server over stdio (for Claude Desktop / Code).",
    )
    parser.add_argument(
        "--tunnel",
        action="store_true",
        help="Run the Pro filesystem tunnel client (gives claude.ai a "
        "permanent URL to your repo). Requires a Pro plan + Node.js/npx.",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="API token for --tunnel (defaults to MERIDIAN_API_KEY / "
        "BEARER_TOKEN).",
    )
    parser.add_argument(
        "--server",
        default=None,
        help="Meridian server base URL for --tunnel (defaults to MERIDIAN_URL "
        "or https://usemeridian.us).",
    )
    parser.add_argument(
        "--repo",
        nargs="+",
        default=None,
        help="Repo path(s) to expose over --tunnel (defaults to current "
             "directory). First path = active repo (Serena --project); extra "
             "paths = additional filesystem roots. (cbbd0eb4)",
    )
    parser.add_argument(
        "--tunnel-port",
        type=int,
        default=8808,
        help="Local port for the tunnel's mcp-proxy (default 8808).",
    )
    parser.add_argument(
        "--no-kill",
        action="store_true",
        help="Skip the stale-port cleanup (8808-8813) at --tunnel startup. Use "
        "for fast restarts when you know no old proxies are lingering. (a887155d)",
    )
    parser.add_argument(
        "--code-dir",
        action="append",
        metavar="PATH",
        dest="code_dirs",
        help="Directory to auto-index with codebase-memory-mcp on tunnel start "
        "(repeatable: --code-dir /repo1 --code-dir /repo2).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MERIDIAN_HOST", "127.0.0.1"),
        help="HTTP bind host (default 127.0.0.1, override with MERIDIAN_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MERIDIAN_PORT", "7878")),
        help="HTTP port (default 7878, override with MERIDIAN_PORT).",
    )
    args = parser.parse_args(argv)

    if args.tunnel:
        # path-quote-strip — de-quote pasted path args (e.g. '"C:\\My Docs"')
        # so a user-supplied surrounding quote pair is not treated as part of
        # the path. Idempotent; interior chars/spaces untouched.
        from .tunnel_client import _normalize_path_arg
        if args.repo:
            args.repo = [_normalize_path_arg(p) for p in args.repo if _normalize_path_arg(p)]
        if args.code_dirs:
            args.code_dirs = [
                _normalize_path_arg(p) for p in args.code_dirs if _normalize_path_arg(p)
            ]
        if args.no_kill:
            print("[meridian] --tunnel: --no-kill set, skipping stale-port cleanup", flush=True)
        else:
            print("[meridian] --tunnel: killing stale ports 8808-8813", flush=True)
            for _p in range(8808, 8814):
                _kill_port(_p)
        from .tunnel_client import run_tunnel

        loop = _ensure_event_loop()
        # cbbd0eb4 — --repo is nargs='+': first = active repo, rest = extra fs roots.
        _repo_list = args.repo if isinstance(args.repo, list) else ([args.repo] if args.repo else [])
        _repo_path = _repo_list[0] if _repo_list else None
        _extra_roots = _repo_list[1:]
        try:
            return loop.run_until_complete(
                run_tunnel(
                    token=args.token,
                    base_url=args.server,
                    repo_path=_repo_path,
                    extra_fs_roots=_extra_roots,
                    port=args.tunnel_port,
                    code_dirs=args.code_dirs,
                )
            )
        except KeyboardInterrupt:
            return 0

    if args.mcp:
        from .server import build_mcp_server

        _, run_stdio = build_mcp_server()
        # On Windows asyncio.run() creates a new ProactorEventLoop, breaking psycopg3.
        # Use the SelectorEventLoop we set at module scope instead.
        loop = _ensure_event_loop()
        loop.run_until_complete(run_stdio())
        return 0

    import uvicorn
    import sys as _sys

    _kill_port(args.port)

    if _sys.platform == "win32":
        # psycopg3 requires SelectorEventLoop. On Windows, asyncio.run()
        # (used by uvicorn.run()) creates ProactorEventLoop by default.
        # Bypass asyncio.run() entirely: run uvicorn.Server on our SelectorEventLoop.
        config = uvicorn.Config(
            "meridian.server:app",
            host=args.host,
            port=args.port,
            reload=False,
            loop="none",
        )
        server = uvicorn.Server(config)
        loop = _ensure_event_loop()  # SelectorEventLoop set above in module scope on win32
        loop.run_until_complete(server.serve())
    else:
        uvicorn.run(
            "meridian.server:app",
            host=args.host,
            port=args.port,
            reload=False,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
