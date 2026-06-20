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


def _kill_port(port: int) -> None:
    """Kill any process listening on the given port before starting."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return  # port is free
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
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
    asyncio.set_event_loop(loop)


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
        default=None,
        help="Repo path to expose over --tunnel (defaults to your home directory (~)).",
    )
    parser.add_argument(
        "--tunnel-port",
        type=int,
        default=8808,
        help="Local port for the tunnel's mcp-proxy (default 8808).",
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
        from .tunnel_client import run_tunnel

        loop = asyncio.get_event_loop()
        try:
            return loop.run_until_complete(
                run_tunnel(
                    token=args.token,
                    base_url=args.server,
                    repo_path=args.repo,
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
        loop = asyncio.get_event_loop()
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
        loop = asyncio.get_event_loop()  # SelectorEventLoop set above in module scope
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
