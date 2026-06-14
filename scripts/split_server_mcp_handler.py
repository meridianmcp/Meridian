"""One-time refactor (2026-06-14, sprint cda0d16b): extract the MCP tool
dispatcher out of server.py into meridian/mcp/handler.py.

Moves the contiguous block (_dispatch_github_tool, _handle_mcp_request, the four
dispatch helpers, and the ~950-line _dispatch_mcp_tool) verbatim. The block's 18
references to server-module helpers/constants are rewritten to `_server.NAME`
(word-boundary-safe; verified none occur in string literals), and handler.py
binds `import meridian.server as _server` lazily-at-call-time (circular-safe:
only accessed inside functions, never at import). server.py re-exports the four
public names so existing importers (its own HTTP routes + mcp.stdio_handler)
keep working unchanged. Both files ast-parsed before write. Safe to delete after
2026-07-01.

Run:  pixi run python scripts/split_server_mcp_handler.py
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "meridian" / "server.py"
HANDLER = ROOT / "meridian" / "mcp" / "handler.py"

lines = SERVER.read_text(encoding="utf-8").splitlines(keepends=True)


def find_line(prefix: str, start: int = 0) -> int:
    for i in range(start, len(lines)):
        if lines[i].startswith(prefix):
            return i
    raise SystemExit(f"anchor not found: {prefix!r}")


start = find_line("async def _dispatch_github_tool(")
end = find_line("# ---------------------------------------------------------------------------", start)
# `end` is the OAuth section header that immediately follows _dispatch_mcp_tool.
block = "".join(lines[start:end]).rstrip() + "\n"

# Names the block references that live in server.py — bind via `_server.`
SERVER_NAMES = [
    "_GITHUB_TOOL_NAMES", "_MCP_PROTOCOL_VERSION", "_MCP_SERVER_INFO",
    "_MCP_TOOLS_LIST", "_answer_hitl_and_apply", "_append_decision_to_md",
    "_append_note_to_roadmap", "_finalize_session_md", "_github_tools_for_tenant",
    "_idle_until_session_done", "_jsonrpc_err", "_jsonrpc_ok", "_maybe_notify",
    "_mcp_readonly_tools", "_on_hitl_answered", "_render_context_block",
    "_render_workspace_block", "_start_session_composite",
]
for n in SERVER_NAMES:
    block = re.sub(rf"\b{n}\b", f"_server.{n}", block)

HEADER = '''"""MCP tool dispatcher — extracted from server.py.

Routes ``tools/call`` requests to the appropriate db_module function and the
session composites. This is the core MCP surface: every tool call from every
AI session (HTTP /mcp, /mcp/sse, remote-MCP, and stdio) funnels through
``_dispatch_mcp_tool`` / ``_handle_mcp_request`` here.

Server-module helpers/constants are reached through ``_server`` (bound at call
time, never at import) to keep the server<->handler relationship non-circular.
server.py re-exports the public names so existing importers keep working.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import meridian.server as _server
from .. import db as db_module
from .. import goal_md as goal_md_module
from .. import md_anchors as md_anchors_module
from .._deps import _hosted_mode, validate_input_size

'''

module = HEADER + block
ast.parse(module)
HANDLER.write_text(module, encoding="utf-8")

# excise from server.py and drop in the re-export
new_lines = (
    lines[:start]
    + [
        "# MCP tool dispatcher moved to meridian/mcp/handler.py. Re-exported here so\n",
        "# server.py's HTTP routes and mcp.stdio_handler keep importing from .server.\n",
        "from .mcp.handler import (  # noqa: E402\n",
        "    _dispatch_github_tool,\n",
        "    _handle_mcp_request,\n",
        "    _dispatch_mcp_tool,\n",
        "    _maybe_add_log_task_nudge,\n",
        ")\n",
        "\n",
    ]
    + lines[end:]
)
new_src = "".join(new_lines)
ast.parse(new_src)
SERVER.write_text(new_src, encoding="utf-8")

print("handler.py written:", HANDLER)
print("server.py lines:", new_src.count("\n"))
