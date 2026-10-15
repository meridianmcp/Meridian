"""Local BM25 code search over any project directory.

SQLite FTS5 ranks matches with BM25 (its built-in ``bm25()``), so this needs no
extra dependency. Each project gets its own index under the Meridian data
directory; nothing is written into the repository being searched. Files that
look like secrets, binaries and oversized files are never indexed.

Refresh is incremental: each search re-checks file mtimes and sizes, re-reads
only what changed, and drops rows for deleted files.

Run as an MCP stdio server (the default) with ``python -m meridian.code_search``.
Tools: ``search``, ``index``, ``list_projects``. The subcommands ``index``,
``search`` and ``list`` do the same from a shell.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

MAX_FILE_BYTES = 512 * 1024
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
MAX_QUERY_TERMS = 32

_TEXT_SUFFIXES = frozenset({
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".html",
    ".md", ".rst", ".txt", ".tex", ".bib", ".sql", ".sh", ".ps1", ".bat", ".cmd",
    ".go", ".rs", ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb",
    ".yml", ".yaml", ".ini", ".cfg",
})
# Config files named like these commonly hold credentials, so they are never read.
_SECRET_NAME_PATTERNS = (
    ".env*", "*.env", "meridian.toml", "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa*", "*secret*", "*credential*",
)
_SKIP_DIR_NAMES = frozenset({
    ".git", ".pixi", ".venv", "venv", "node_modules", "__pycache__", "dist",
    "build", ".serena", ".codex", ".claude", ".cursor",
})

_WORD = re.compile(r"[A-Za-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    path UNINDEXED,
    body,
    split_terms,
    tokenize = 'unicode61'
);
"""


class CodeSearchError(RuntimeError):
    """Raised for a project path that cannot be searched."""


@dataclass(frozen=True)
class RefreshReport:
    project: str
    db_path: str
    total: int
    updated: int
    removed: int
    skipped: int

    def as_dict(self) -> dict:
        return {
            "project": self.project,
            "db_path": self.db_path,
            "total": self.total,
            "updated": self.updated,
            "removed": self.removed,
            "skipped": self.skipped,
        }


def index_dir(env=None) -> Path:
    """Where per-project indexes live. Override with MERIDIAN_CODE_SEARCH_DIR."""
    source = os.environ if env is None else env
    override = str(source.get("MERIDIAN_CODE_SEARCH_DIR") or "").strip()
    if override:
        return Path(override)
    local = str(source.get("LOCALAPPDATA") or "").strip()
    if local:
        return Path(local) / "meridian" / "code-search"
    return Path.home() / ".local" / "share" / "meridian" / "code-search"


def db_path_for(root: Path, base: Path | None = None) -> Path:
    base = base if base is not None else index_dir()
    key = str(root.resolve()).lower().encode("utf-8")
    return base / f"{hashlib.sha1(key).hexdigest()[:16]}.sqlite"


def split_identifiers(text: str) -> str:
    """Lowercase words with camelCase boundaries split: ``ReadPipeline`` -> ``read pipeline``."""
    out: list[str] = []
    for word in _WORD.findall(text):
        out.extend(part.lower() for part in _CAMEL_BOUNDARY.split(word) if part)
    return " ".join(out)


def query_terms(query: str) -> list[str]:
    """Search terms from a query, keeping both raw words and camelCase parts."""
    seen: list[str] = []
    for term in [w.lower() for w in _WORD.findall(query)] + split_identifiers(query).split():
        if term not in seen:
            seen.append(term)
    return seen[:MAX_QUERY_TERMS]


def _is_secret_name(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch(lowered, pattern) for pattern in _SECRET_NAME_PATTERNS)


def _eligible(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    if _is_secret_name(name):
        return False
    if Path(name).suffix.lower() not in _TEXT_SUFFIXES:
        return False
    return not any(part in _SKIP_DIR_NAMES for part in parts[:-1])


def list_candidate_files(root: Path) -> list[str]:
    """Tracked and untracked-but-not-ignored files for a git repo; a directory walk otherwise."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True,
            check=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return _walk(root)
    return [p for p in completed.stdout.decode("utf-8", "replace").split("\0") if p]


def _walk(root: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for filename in filenames:
            full = Path(dirpath) / filename
            found.append(full.relative_to(root).as_posix())
    return found


def _read_text(path: Path, size: int) -> str | None:
    if size > MAX_FILE_BYTES:
        return None
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def _connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    return conn


def _delete_path(conn: sqlite3.Connection, rel: str) -> None:
    conn.execute("DELETE FROM docs WHERE path = ?", (rel,))
    conn.execute("DELETE FROM files WHERE path = ?", (rel,))


def refresh_index(root: Path, base: Path | None = None) -> RefreshReport:
    """Bring the project's index up to date with its files. Incremental and idempotent."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise CodeSearchError(f"not a directory: {root}")
    db = db_path_for(root, base)
    conn = _connect(db)
    updated = removed = skipped = 0
    try:
        known = {path: (mtime, size) for path, mtime, size in conn.execute(
            "SELECT path, mtime, size FROM files")}
        live: set[str] = set()
        with conn:
            for rel in list_candidate_files(root):
                if not _eligible(rel):
                    continue
                full = root / rel
                try:
                    st = full.stat()
                except OSError:
                    skipped += 1
                    continue
                if known.get(rel) == (st.st_mtime, st.st_size):
                    live.add(rel)
                    continue
                _delete_path(conn, rel)
                text = _read_text(full, st.st_size)
                if text is None:
                    skipped += 1
                    continue
                conn.execute(
                    "INSERT INTO docs(path, body, split_terms) VALUES (?, ?, ?)",
                    (rel, text, split_identifiers(text)),
                )
                conn.execute(
                    "INSERT INTO files(path, mtime, size) VALUES (?, ?, ?)",
                    (rel, st.st_mtime, st.st_size),
                )
                live.add(rel)
                updated += 1
            for rel in set(known) - live:
                _delete_path(conn, rel)
                removed += 1
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('root', ?)", (str(root),))
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('indexed_at', ?)",
                (f"{time.time():.3f}",),
            )
        return RefreshReport(
            project=str(root),
            db_path=str(db),
            total=len(live),
            updated=updated,
            removed=removed,
            skipped=skipped,
        )
    finally:
        conn.close()


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def search(root: Path, query: str, limit: int = DEFAULT_LIMIT, base: Path | None = None,
           refresh: bool = True) -> list[dict]:
    """BM25-ranked matches in one project. Higher score is more relevant."""
    root = Path(root).resolve()
    if refresh:
        refresh_index(root, base)
    terms = query_terms(query)
    if not terms:
        return []
    match = " OR ".join(f'"{term}"' for term in terms)
    db = db_path_for(root, base)
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            """
            SELECT path, snippet(docs, 1, '[', ']', ' ... ', 16), bm25(docs, 0.0, 1.0, 0.5)
            FROM docs
            WHERE docs MATCH ?
            ORDER BY bm25(docs, 0.0, 1.0, 0.5)
            LIMIT ?
            """,
            (match, _clamp_limit(limit)),
        ).fetchall()
    finally:
        conn.close()
    # Keep significant digits, not decimal places: in a small corpus SQLite clamps
    # the IDF, so scores can be far below 0.0001 and still rank correctly.
    return [
        {"path": path, "score": float(f"{-score:.6g}"), "snippet": snippet}
        for path, snippet, score in rows
    ]


def list_indexes(base: Path | None = None) -> list[dict]:
    """Every project index on disk, with its root, file count and last refresh time."""
    directory = base if base is not None else index_dir()
    if not directory.is_dir():
        return []
    out: list[dict] = []
    for db in sorted(directory.glob("*.sqlite")):
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            try:
                meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
                files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        out.append({
            "root": meta.get("root"),
            "files": files,
            "indexed_at": float(meta["indexed_at"]) if "indexed_at" in meta else None,
            "db_path": str(db),
        })
    return out


def _project_root(project: str) -> Path:
    path = Path(str(project)).expanduser()
    if not path.is_dir():
        raise CodeSearchError(f"project must be an existing directory: {project}")
    return path.resolve()


def build_server():
    """MCP server exposing search, index and list_projects over stdio."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("meridian-search")

    @mcp.tool()
    def search_code(project: str, query: str, limit: int = DEFAULT_LIMIT) -> dict:
        """BM25-ranked search over one project directory. Refreshes the index first.

        ``project`` is an absolute directory path. Returns path, score (higher is
        better) and a snippet per hit.
        """
        root = _project_root(project)
        hits = search(root, query, limit=limit)
        return {"project": str(root), "query": query, "hits": hits}

    @mcp.tool()
    def index_project(project: str) -> dict:
        """Refresh the search index for a project directory and report what changed."""
        return refresh_index(_project_root(project)).as_dict()

    @mcp.tool()
    def list_projects() -> dict:
        """Every project that has a search index, with file counts and refresh times."""
        return {"indexes": list_indexes()}

    return mcp


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m meridian.code_search",
        description="BM25 code search over local project directories. No subcommand runs the MCP server.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Run the MCP stdio server (default).")
    index = sub.add_parser("index", help="Refresh a project's index.")
    index.add_argument("project")
    find = sub.add_parser("search", help="Search a project.")
    find.add_argument("project")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    sub.add_parser("list", help="List indexed projects.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_cli().parse_args(argv)
    if args.command in (None, "serve"):
        build_server().run()
        return 0
    try:
        if args.command == "index":
            payload = refresh_index(_project_root(args.project)).as_dict()
        elif args.command == "search":
            payload = {"hits": search(_project_root(args.project), args.query, limit=args.limit)}
        else:
            payload = {"indexes": list_indexes()}
    except CodeSearchError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
