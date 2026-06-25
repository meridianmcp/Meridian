"""Minimal PyInstaller entry point for the slim ``meridian.exe`` tunnel client.

The full server entry point (``meridian/__main__entry.py``) pulls in FastAPI,
uvicorn, psycopg3, langgraph, the hosted/billing routes and every DB dependency.
The standalone binary that users download (``meridian.exe`` / ``meridian-linux``
/ ``meridian-mac-*``) is only ever used as a Pro filesystem tunnel client
(``meridian --tunnel --repo .``), so bundling the whole server is wasteful.

This module is the dedicated tunnel-client entry point. It imports ONLY what the
tunnel needs:

  * stdlib (argparse / asyncio / os / selectors / sys)
  * ``meridian.tunnel_client`` (which itself only imports stdlib + serena_pool
    at module scope; httpx / websockets are imported lazily inside ``run_tunnel``)
  * ``meridian.serena_pool`` (pure stdlib)

It deliberately does NOT import ``meridian.server``, ``meridian.pg_adapter``,
``meridian.hosted``, langgraph, FastAPI or uvicorn. The PyInstaller spec
(``meridian.spec``) excludes those modules so the frozen binary stays small.

CLI flags mirror the ``--tunnel`` subset of ``meridian/__main__.py`` so the
binary behaves exactly like ``python -m meridian --tunnel ...``.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# psycopg3 requires SelectorEventLoop on Windows (ProactorEventLoop is not
# supported). The tunnel client itself does not use psycopg, but websockets /
# subprocess pipe transports are also happier on the SelectorEventLoop, and we
# mirror __main__.py's handling so behaviour is identical across entry points.
# Must run before any asyncio.run()/loop call.
if sys.platform == "win32":
    import selectors

    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    _loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
    asyncio.set_event_loop(_loop)


def _build_parser() -> argparse.ArgumentParser:
    """Argument parser for the slim tunnel binary (mirrors __main__ --tunnel)."""
    parser = argparse.ArgumentParser(
        prog="meridian",
        description="Meridian Pro filesystem tunnel client.",
    )
    parser.add_argument(
        "--tunnel",
        action="store_true",
        help="Run the Pro filesystem tunnel client (default for this binary).",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="API token (defaults to MERIDIAN_API_KEY / BEARER_TOKEN).",
    )
    parser.add_argument(
        "--server",
        default=None,
        help="Meridian server base URL (defaults to MERIDIAN_URL or "
        "https://usemeridian.us).",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Repo path to expose over the tunnel (defaults to ~).",
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
    return parser


def _resolve_loop() -> asyncio.AbstractEventLoop:
    """Return a usable event loop without relying on get_event_loop() state.

    On Windows the module-scope SelectorEventLoop (set at import) is preferred so
    psycopg/websocket transports behave. If it is missing or closed, fall back to
    a brand-new SelectorEventLoop (win32) or a plain new loop (other platforms).
    """
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
        if not loop.is_closed():
            return loop
    except RuntimeError:
        pass

    if sys.platform == "win32":
        import selectors

        loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
    else:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop


def main(argv: list[str] | None = None) -> int:
    """Run the tunnel client. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Import lazily so merely importing this module (e.g. in tests or at frozen
    # bootstrap) does not pull httpx/websockets into sys.modules.
    from .tunnel_client import run_tunnel

    # Reuse the module-scope SelectorEventLoop on Windows (set above); elsewhere
    # create a fresh loop. asyncio.get_event_loop() is unreliable off the main
    # thread / after a loop is closed, so resolve it defensively.
    loop = _resolve_loop()
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


if __name__ == "__main__":
    sys.exit(main())
