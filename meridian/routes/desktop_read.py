"""Read-only local routes for the Meridian desktop app (Code, Outputs, Research tabs).

The desktop app calls these over 127.0.0.1. They read local data only and never
touch the tunnel, so they answer with no tunnel connected.

Routes
------
``GET /desktop/code/search?q=&limit=``
    BM25 search over the local code-search index built by ``meridian/code_search.py``
    for the configured repository root. Returns ``{"results": [{path, line,
    snippet, score}], "index_state": ...}``.

``GET /desktop/outputs/search?q=&limit=``
    BM25 search over the outputs index (``meridian/outputs_indexer.py``) for the
    configured outputs directory. Returns ``{"results": [{path, score, kind,
    generating_script, csv_columns, json_keys, is_archival, canonical_path, size,
    mtime}], "hits": [...same list...], "index_state": ..., "convergence": {...}}``.
    ``hits`` is the same list as ``results``: the desktop outputs client reads
    ``hits``, and ``results`` is kept for existing callers. The outputs index holds
    no line or snippet. ``convergence`` is the index health the desktop client reads:
    ``partial_index``, ``pending_count``, ``degraded``, ``inconclusive``,
    ``walk_errors``, ``db_write_error`` and ``last_rebuilt_at`` (ISO 8601 UTC, or null).

``GET /desktop/research/runs?limit=&project_id=``
    Research runs from the project-scoped store (``meridian/db/research_runs.py``),
    newest first. ``project_id`` is an optional filter; without it the runs of every
    project in the local database are merged. Returns ``{"runs": [...], "count": n}``.

``index_state`` is one of:

* ``ready``         the index exists and was searched (zero hits is still ready);
* ``not_indexed``   no configured index exists yet for the configured location;
* ``unavailable``   an index exists but could not be read or searched.

Trust boundary
--------------
* The caller never supplies a filesystem path. The repository root and the outputs
  directory come from server configuration only (see below). A request carrying a
  path-like parameter (``path``, ``root``, ``outputs_dir``, ...) is refused with 400
  instead of being silently ignored.
* Code search only reads an index that already exists. A GET never indexes or walks
  the tree. An index is used only when it lives in the code-search index directory
  and records the configured root in its ``meta`` table.
* Hosted Meridian answers 404 on all three routes. They expose the server's own
  filesystem and database, which only a local single-user server should serve.
* Errors are plain JSON. Tracebacks are written to the server log, never returned.

Configuration
-------------
``MERIDIAN_DESKTOP_CODE_ROOT``
    Repository root to search. Defaults to the repository that contains the running
    ``meridian`` package.
``MERIDIAN_CODE_SEARCH_DIR``
    Code-search index directory (see ``meridian/code_search.py``).
``MERIDIAN_OUTPUTS_DIR``
    Outputs directory whose index is searched.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from .. import code_search
from .. import db as db_module
from .. import outputs_indexer
from .._deps import _db, _hosted_mode
from ..db import research_runs as run_db

_log = logging.getLogger(__name__)

router = APIRouter(tags=["desktop"])

CODE_ROOT_ENV = "MERIDIAN_DESKTOP_CODE_ROOT"
OUTPUTS_DIR_ENV = "MERIDIAN_OUTPUTS_DIR"

INDEX_READY = "ready"
INDEX_NOT_INDEXED = "not_indexed"
INDEX_UNAVAILABLE = "unavailable"

OUTPUTS_DEFAULT_LIMIT = 10
OUTPUTS_MAX_LIMIT = 50
RUNS_DEFAULT_LIMIT = 20
RUNS_MAX_LIMIT = 100
MAX_QUERY_CHARS = 500
MAX_PROJECT_ID_CHARS = 128
TITLE_MAX_CHARS = 120

# Query parameters that would let a caller name a filesystem location. Refused
# outright (400) so a caller cannot believe a path was honoured when it was not.
_PATH_PARAMS = frozenset({
    "path", "paths", "file", "files", "root", "roots", "root_dir", "dir",
    "directory", "outputs_dir", "project_path", "repo", "repo_path", "cwd", "src",
})

_WORD = re.compile(r"[A-Za-z0-9]+")

_NOT_HOSTED_MESSAGE = "Not found."
_PATH_REFUSED_MESSAGE = (
    "Filesystem paths are not accepted. Searches run only over the configured local indexes."
)
_QUERY_REQUIRED_MESSAGE = "q is required"
_QUERY_TOO_LONG_MESSAGE = f"q must be at most {MAX_QUERY_CHARS} characters"


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _refusal(request: Request) -> JSONResponse | None:
    """Gate shared by every desktop route: hosted refusal, then path-parameter refusal."""
    if _hosted_mode():
        return _error(404, _NOT_HOSTED_MESSAGE)
    if _PATH_PARAMS.intersection(request.query_params.keys()):
        return _error(400, _PATH_REFUSED_MESSAGE)
    return None


def _query_error(q: str) -> JSONResponse | None:
    text = (q or "").strip()
    if not text:
        return _error(400, _QUERY_REQUIRED_MESSAGE)
    if len(text) > MAX_QUERY_CHARS:
        return _error(400, _QUERY_TOO_LONG_MESSAGE)
    return None


def _clamp_limit(limit: int, maximum: int) -> int:
    return max(1, min(int(limit), maximum))


# ---------------------------------------------------------------------------
# Code search
# ---------------------------------------------------------------------------

def _configured_code_root() -> Path:
    """The repository root to search. Server configuration only, never the caller."""
    override = str(os.environ.get(CODE_ROOT_ENV) or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _read_only(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)


def _index_root(db: Path) -> str | None:
    """The repository root a code-search index was built for."""
    conn = _read_only(db)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'root'").fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _best_line(body: str, terms: set[str]) -> int | None:
    """1-based line holding the most distinct query terms; earliest line wins ties."""
    best_line: int | None = None
    best_hits = 0
    for number, line in enumerate(body.splitlines(), start=1):
        tokens = {word.lower() for word in _WORD.findall(line)}
        tokens.update(code_search.split_identifiers(line).split())
        hits = len(tokens & terms)
        if hits > best_hits:
            best_line, best_hits = number, hits
    return best_line


def _code_search(q: str, limit: int) -> tuple[str, list[dict[str, Any]], str | None]:
    root = _configured_code_root()
    db = code_search.db_path_for(root)
    if not db.is_file():
        return (
            INDEX_NOT_INDEXED, [],
            "No code index exists for the configured repository yet. "
            "Build one with: python -m meridian.code_search index <repository>",
        )
    if db.resolve().parent != code_search.index_dir().resolve():
        return INDEX_NOT_INDEXED, [], "The code index is not in the configured index directory."
    if _index_root(db) != str(root):
        return (
            INDEX_NOT_INDEXED, [],
            "The code index on disk was built for a different repository. "
            "Rebuild it for the configured repository.",
        )

    hits = code_search.search(root, q, limit=limit, refresh=False)
    results: list[dict[str, Any]] = []
    if hits:
        terms = set(code_search.query_terms(q))
        conn = _read_only(db)
        try:
            for hit in hits:
                row = conn.execute(
                    "SELECT body FROM docs WHERE path = ?", (hit["path"],),
                ).fetchone()
                results.append({
                    "path": hit["path"],
                    "line": _best_line(row[0], terms) if row else None,
                    "snippet": hit["snippet"],
                    "score": hit["score"],
                })
        finally:
            conn.close()
    return INDEX_READY, results, None


@router.get("/desktop/code/search")
def desktop_code_search(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=code_search.DEFAULT_LIMIT),
) -> JSONResponse:
    refusal = _refusal(request)
    if refusal is not None:
        return refusal
    invalid = _query_error(q)
    if invalid is not None:
        return invalid
    try:
        state, results, message = _code_search(q.strip(), _clamp_limit(limit, code_search.MAX_LIMIT))
    except Exception:  # noqa: BLE001 - plain error to the client, traceback to the log only
        _log.warning("desktop code search: index unavailable", exc_info=True)
        return JSONResponse({
            "results": [],
            "index_state": INDEX_UNAVAILABLE,
            "message": "The code index could not be read. Rebuild it and try again.",
        })
    body: dict[str, Any] = {"results": results, "index_state": state}
    if message:
        body["message"] = message
    return JSONResponse(body)


# ---------------------------------------------------------------------------
# Outputs search
# ---------------------------------------------------------------------------

_OUTPUT_FIELDS = (
    "path", "score", "kind", "generating_script", "csv_columns", "json_keys",
    "is_archival", "canonical_path", "size", "mtime",
)


def _outputs_dir() -> str:
    """The outputs directory to search. Server configuration only, never the caller."""
    return str(os.environ.get(OUTPUTS_DIR_ENV) or "").strip()


# Fixed sentences for the two index faults the client shows. The indexer's own
# messages carry exception text and paths, so they never leave the server.
INDEX_DB_WRITE_MESSAGE = (
    "The outputs index could not be saved to disk, so its results may be incomplete."
)
INDEX_WALK_MESSAGE = "A folder in the outputs directory could not be read."


def _iso_utc(stamp: Any) -> str | None:
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(stamp), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _index_convergence(found: dict[str, Any]) -> dict[str, Any]:
    """Index health from one ``search_outputs`` result, in the shape the desktop client reads.

    Uses the indexer's ``convergence`` snapshot when it has one, otherwise the top-level
    ``partial`` / ``pending_count`` / ``db_write_error`` fields. ``degraded`` is conservative:
    a partial, queued, unreadable or unsaved index is degraded whatever the snapshot says.
    """
    snapshot = found.get("convergence")
    source: dict[str, Any] = snapshot if isinstance(snapshot, dict) else found
    partial = bool(source.get("partial_index", source.get("partial")))
    pending = _count(source.get("pending_count"))
    walk_errors = source.get("walk_errors")
    walk_errors = walk_errors if isinstance(walk_errors, list) else []
    db_failed = bool(source.get("db_write_error"))
    inconclusive = bool(source.get("inconclusive")) or bool(walk_errors) or db_failed
    degraded = bool(source.get("degraded")) or partial or pending > 0 or inconclusive
    return {
        "partial_index": partial,
        "pending_count": pending,
        "degraded": degraded,
        "inconclusive": inconclusive,
        "walk_errors": [INDEX_WALK_MESSAGE for _ in walk_errors],
        "db_write_error": INDEX_DB_WRITE_MESSAGE if db_failed else None,
        "last_rebuilt_at": _iso_utc(source.get("last_rebuilt_at")),
    }


def _unknown_index(*, inconclusive: bool) -> dict[str, Any]:
    """Index health when no index answered. An empty result is then never a confirmed miss."""
    return {
        "partial_index": False,
        "pending_count": 0,
        "degraded": True,
        "inconclusive": inconclusive,
        "walk_errors": [],
        "db_write_error": None,
        "last_rebuilt_at": None,
    }


def _outputs_search(
    q: str, limit: int,
) -> tuple[str, list[dict[str, Any]], str | None, dict[str, Any]]:
    directory = _outputs_dir()
    if not directory or not os.path.isdir(directory):
        return (
            INDEX_NOT_INDEXED, [],
            f"No outputs index: set {OUTPUTS_DIR_ENV} to an existing outputs directory.",
            _unknown_index(inconclusive=False),
        )
    found = outputs_indexer.search_outputs(directory, q, limit=limit)
    if found.get("error"):
        return (
            INDEX_UNAVAILABLE, [], "The outputs index could not be searched.",
            _unknown_index(inconclusive=True),
        )
    results = [{field: hit.get(field) for field in _OUTPUT_FIELDS} for hit in found.get("hits", [])]
    return INDEX_READY, results, None, _index_convergence(found)


def _outputs_body(
    results: list[dict[str, Any]],
    state: str,
    message: str | None,
    index: dict[str, Any],
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "results": results,
        # The desktop outputs client reads "hits". It is the same list as "results".
        "hits": results,
        "index_state": state,
        "convergence": index,
    }
    if message:
        body["message"] = message
    if index["partial_index"]:
        # The outputs tree is still being indexed; a short result list is not final.
        body["partial"] = True
    return body


@router.get("/desktop/outputs/search")
def desktop_outputs_search(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=OUTPUTS_DEFAULT_LIMIT),
) -> JSONResponse:
    refusal = _refusal(request)
    if refusal is not None:
        return refusal
    invalid = _query_error(q)
    if invalid is not None:
        return invalid
    try:
        state, results, message, index = _outputs_search(
            q.strip(), _clamp_limit(limit, OUTPUTS_MAX_LIMIT),
        )
    except Exception:  # noqa: BLE001 - plain error to the client, traceback to the log only
        _log.warning("desktop outputs search: index unavailable", exc_info=True)
        return JSONResponse(_outputs_body(
            [], INDEX_UNAVAILABLE,
            "The outputs index could not be searched. Try again shortly.",
            _unknown_index(inconclusive=True),
        ))
    return JSONResponse(_outputs_body(results, state, message, index))


# ---------------------------------------------------------------------------
# Research runs
# ---------------------------------------------------------------------------

def _run_title(run: dict[str, Any]) -> str:
    receipt = run.get("result_receipt")
    summary = receipt.get("result_summary") if isinstance(receipt, dict) else None
    if summary:
        return " ".join(str(summary).split())[:TITLE_MAX_CHARS]
    # Research runs store no title or query column; a run without a receipt summary
    # is identified by its mode and repository instead.
    return f"{run.get('mode')} run in {run.get('repository_id')}"


def _run_view(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": run.get("id"),
        "project_id": run.get("project_id"),
        "title": _run_title(run),
        "state": run.get("status"),
        "mode": run.get("mode"),
        "repository_id": run.get("repository_id"),
        "disposition": run.get("disposition"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "updated_at": run.get("updated_at"),
        "completed_at": run.get("completed_at"),
        "expires_at": run.get("expires_at"),
    }


@router.get("/desktop/research/runs")
async def desktop_research_runs(
    request: Request,
    limit: int = Query(default=RUNS_DEFAULT_LIMIT),
    project_id: str | None = Query(default=None),
) -> JSONResponse:
    refusal = _refusal(request)
    if refusal is not None:
        return refusal
    scope = (project_id or "").strip()
    if len(scope) > MAX_PROJECT_ID_CHARS:
        return _error(400, "project_id is too long")
    count = _clamp_limit(limit, RUNS_MAX_LIMIT)
    db = await _db(request)
    try:
        if scope:
            project_ids = [scope]
        else:
            project_ids = [str(project["id"]) for project in await db_module.list_projects(db)]
        runs: list[dict[str, Any]] = []
        for pid in project_ids:
            runs.extend(await run_db.list_research_runs(
                db, pid, include_terminal=True, limit=count,
            ))
    except Exception:  # noqa: BLE001 - plain error to the client, traceback to the log only
        _log.warning("desktop research runs: store unavailable", exc_info=True)
        return _error(503, "Research runs are unavailable right now.")
    runs.sort(key=lambda run: (str(run.get("started_at") or ""), str(run.get("id") or "")), reverse=True)
    views = [_run_view(run) for run in runs[:count]]
    return JSONResponse({"runs": views, "count": len(views)})
