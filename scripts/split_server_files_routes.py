"""One-time refactor (2026-06-14, sprint cda0d16b): finish extracting the
file-editing routes out of server.py into meridian/routes/files.py.

routes/files.py is hand-authored with a `#@@DEMO_DICT@@` sentinel; this script
(1) splices the 275-line `_DEMO_FILE_CONTENT` literal from server.py into that
sentinel byte-exact, and (2) excises the literal + the three moved routes from
server.py. Both files are ast-parsed before writing. Safe to delete after
2026-07-01.

Run:  pixi run python scripts/split_server_files_routes.py
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "meridian" / "server.py"
FILES_ROUTE = ROOT / "meridian" / "routes" / "files.py"

lines = SERVER.read_text(encoding="utf-8").splitlines(keepends=True)


def find_line(prefix: str, start: int = 0) -> int:
    for i in range(start, len(lines)):
        if lines[i].startswith(prefix):
            return i
    raise SystemExit(f"anchor not found: {prefix!r}")


# locate the _DEMO_FILE_CONTENT literal (inclusive of closing brace line)
dict_start = find_line("_DEMO_FILE_CONTENT: dict[str, dict[str, str]] = {")
dict_end = next((i for i in range(dict_start + 1, len(lines)) if lines[i] == "}\n"), None)
if dict_end is None:
    raise SystemExit("closing brace for _DEMO_FILE_CONTENT not found")
demo_block = "".join(lines[dict_start:dict_end + 1]).rstrip("\n")

# locate the three file routes (up to, not including, the next /devlog route)
routes_start = find_line('@app.get("/projects/{project_id}/files")')
routes_end = find_line('@app.post("/projects/{project_id}/devlog")', routes_start)

# --- 1. fill the sentinel in routes/files.py -------------------------------
files_src = FILES_ROUTE.read_text(encoding="utf-8")
if "#@@DEMO_DICT@@" not in files_src:
    raise SystemExit("sentinel already filled / not present in files.py")
files_src = files_src.replace("#@@DEMO_DICT@@", demo_block, 1)
ast.parse(files_src)
FILES_ROUTE.write_text(files_src, encoding="utf-8")

# --- 2. excise from server.py (routes first, then the literal) -------------
new_lines = lines[:routes_start] + lines[routes_end:]
new_lines = (
    new_lines[:dict_start]
    + ["# _DEMO_FILE_CONTENT + file-editing routes moved to meridian/routes/files.py\n"]
    + new_lines[dict_end + 1:]
)
new_src = "".join(new_lines)
ast.parse(new_src)
SERVER.write_text(new_src, encoding="utf-8")

print("files.py filled:", FILES_ROUTE)
print("server.py lines:", new_src.count("\n"))
