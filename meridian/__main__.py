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
import sys


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

    if args.mcp:
        from .server import build_mcp_server

        _, run_stdio = build_mcp_server()
        asyncio.run(run_stdio())
        return 0

    import uvicorn

    uvicorn.run(
        "meridian.server:app",
        host=args.host,
        port=args.port,
        reload=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
