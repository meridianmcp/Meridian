"""One-time refactor (2026-06-14, sprint cda0d16b): extract the GitHub
integration routes out of server.py into meridian/routes/github.py.

Mechanical slice of the contiguous block (github/connect ... github/branches,
incl. the in-module repo/branch caches), with @app->@router and
`from .hosted`->`from ..hosted` rewrites. Both files ast-parsed before write.
Safe to delete after 2026-07-01.

Run:  pixi run python scripts/split_server_github_routes.py
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "meridian" / "server.py"
GH_ROUTE = ROOT / "meridian" / "routes" / "github.py"

lines = SERVER.read_text(encoding="utf-8").splitlines(keepends=True)


def find_line(prefix: str, start: int = 0) -> int:
    for i in range(start, len(lines)):
        if lines[i].startswith(prefix):
            return i
    raise SystemExit(f"anchor not found: {prefix!r}")


start = find_line('@app.post("/projects/{project_id}/github/connect")')
end = find_line('@app.get("/mcp/quickstart"', start)  # exclusive
block = "".join(lines[start:end]).rstrip() + "\n"

block = block.replace("@app.post(", "@router.post(")
block = block.replace("@app.get(", "@router.get(")
block = block.replace("@app.delete(", "@router.delete(")
block = block.replace("from .hosted import", "from ..hosted import")

HEADER = '''"""GitHub integration routes (hosted-tier only) — extracted from server.py.

Repo connect/status/disconnect, repo + branch listing (with a 24h in-memory
per-tenant cache), the repo-image proxy, and the MCP-template push. All routes
404 in self-host mode.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .._deps import _db, _hosted_mode, _get_tenant_from_request
from .. import db as db_module

router = APIRouter()


'''

module = HEADER + block
ast.parse(module)
GH_ROUTE.write_text(module, encoding="utf-8")

# excise from server.py (keep the section-header comment as a pointer)
new_lines = (
    lines[:start]
    + ["# GitHub integration routes moved to meridian/routes/github.py\n", "\n"]
    + lines[end:]
)
new_src = "".join(new_lines)
ast.parse(new_src)
SERVER.write_text(new_src, encoding="utf-8")

print("github.py written:", GH_ROUTE)
print("server.py lines:", new_src.count("\n"))
