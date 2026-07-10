"""Recursive outputs indexer — 06df6ab3 Part 2.

A SEPARATE concern from ``doc_store`` (no shared tables, no shared code path):
watches a configurable "outputs" directory TREE (recursive) for ``.csv`` /
``.json`` / ``.npy`` changes and maintains a lightweight, queryable index over
whatever numeric/tabular artifacts a run produced.

* **.csv / .json** — a SINGLE unified index across the WHOLE tree via DuckDB's
  glob-aware ``read_csv`` / ``read_json`` (``filename=true`` traces each row
  back to its source file, exactly per the sprint spec:
  ``duckdb.sql("SELECT * FROM read_csv('outputs/**/*.csv', filename=true)")``).
  There is no in-process per-row cache to keep in sync file-by-file — a
  create/modify/delete of any matching file just means "re-run the glob query
  next time it's asked for" (DuckDB streams straight off disk, so this is cheap).
* **.npy** — METADATA ONLY via ``numpy.load(path, mmap_mode='r')`` (shape,
  dtype, file size, modified time) — the array is memory-mapped, never fully
  read into memory or indexed by content.
* **Everything else** (images, other binaries) is explicitly OUT OF SCOPE per
  the sprint spec: the watcher's patterns never match them, so they are simply
  invisible to this indexer — no filesystem-metadata listing is built for them
  either (don't build more than the spec asks for).

Uses ``watchdog`` (``watchdog.observers.Observer`` +
``PatternMatchingEventHandler``), NOT ``watchfiles`` — ``watchfiles`` deadlocks
the Windows ``ProactorEventLoop`` (see AGENTS.md); ``watchdog`` uses the native
``ReadDirectoryChangesW`` API on Windows and is unaffected.

Fully unit-testable without a running watcher: :func:`index_csv` /
:func:`index_json` / :func:`npy_metadata` are plain functions callable directly;
:class:`OutputsIndexer` just wires them to a ``watchdog`` ``Observer`` and can
also be driven directly via :meth:`OutputsIndexer.handle_event` in tests (no
real filesystem-event delivery needed).
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import asdict, dataclass
from typing import Any, Callable

_log = logging.getLogger(__name__)

# The three extensions this indexer watches/indexes — everything else (images,
# other binaries) is explicitly out of scope (see module docstring).
WATCHED_PATTERNS: tuple[str, ...] = ("*.csv", "*.json", "*.npy")


# ---------------------------------------------------------------------------
# .npy — metadata only, never full-array content (pure, unit-testable)
# ---------------------------------------------------------------------------

@dataclass
class NpyMetadata:
    """Metadata for one ``.npy`` file — shape/dtype/size/mtime, no array content."""

    path: str
    shape: tuple[int, ...] | None
    dtype: str | None
    size_bytes: int | None
    modified_at: float | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def npy_metadata(path: str) -> NpyMetadata:
    """Shape/dtype/size/modified-time of a ``.npy`` file, WITHOUT reading its
    array content — ``numpy.load(path, mmap_mode='r')`` memory-maps the file so
    only the header is actually parsed. Never raises: a missing/corrupt file
    still yields whatever of {size_bytes, modified_at} stat succeeded, plus an
    ``error`` string; a stat failure (file vanished mid-scan) yields an
    all-``None`` metadata row with ``error`` set.
    """
    try:
        st = os.stat(path)
        size_bytes: int | None = st.st_size
        modified_at: float | None = st.st_mtime
    except OSError as exc:
        return NpyMetadata(
            path=path, shape=None, dtype=None,
            size_bytes=None, modified_at=None, error=str(exc),
        )
    try:
        import numpy  # noqa: PLC0415 — optional/lazy

        arr = numpy.load(path, mmap_mode="r")
        try:
            shape = tuple(int(d) for d in arr.shape)
            dtype = str(arr.dtype)
        finally:
            del arr  # release the mmap promptly (matters on Windows)
        return NpyMetadata(
            path=path, shape=shape, dtype=dtype,
            size_bytes=size_bytes, modified_at=modified_at,
        )
    except Exception as exc:  # noqa: BLE001 — a corrupt .npy still gets size/mtime
        _log.debug("npy_metadata failed for %r", path, exc_info=True)
        return NpyMetadata(
            path=path, shape=None, dtype=None,
            size_bytes=size_bytes, modified_at=modified_at, error=str(exc),
        )


# ---------------------------------------------------------------------------
# .csv / .json — one unified DuckDB glob index across the whole tree
# ---------------------------------------------------------------------------

def _glob_pattern(outputs_dir: str, suffix: str) -> str:
    """The recursive DuckDB glob for one extension under ``outputs_dir``.

    DuckDB's glob matcher wants forward slashes even on Windows, so the
    absolute path is normalized before the ``**/*.<suffix>`` suffix is appended.
    """
    base = os.path.abspath(outputs_dir).replace("\\", "/")
    return f"{base}/**/*.{suffix}"


def _rows_from_relation(relation: Any) -> list[dict[str, Any]]:
    """Materialize a DuckDB relation (or an injected stand-in) into row dicts."""
    columns = None
    if hasattr(relation, "columns"):
        columns = list(relation.columns)
    elif hasattr(relation, "description"):  # DB-API-ish stand-ins (tests)
        columns = [c[0] for c in relation.description]
    rows = relation.fetchall()
    if columns:
        return [dict(zip(columns, row)) for row in rows]
    return [dict(row) if isinstance(row, dict) else row for row in rows]


def _run_duckdb(
    sql: str, sql_runner: Callable[[str], Any] | None
) -> list[dict[str, Any]]:
    """Run ``sql`` via ``sql_runner`` (tests) or real ``duckdb.sql`` (production).

    Best-effort: an empty/missing glob (no matching files yet), a malformed
    file DuckDB can't parse, or any other failure yields ``[]`` rather than
    raising — a transient bad file must never crash the indexer.
    """
    try:
        relation = sql_runner(sql) if sql_runner is not None else _duckdb_sql(sql)
        return _rows_from_relation(relation)
    except Exception:  # noqa: BLE001
        _log.debug("duckdb query failed for %r", sql, exc_info=True)
        return []


def _duckdb_sql(sql: str) -> Any:
    import duckdb  # noqa: PLC0415 — optional/lazy

    return duckdb.sql(sql)


def index_csv(
    outputs_dir: str, *, sql_runner: Callable[[str], Any] | None = None
) -> list[dict[str, Any]]:
    """Unified CSV index across ``outputs_dir`` (recursive), one row per source
    row, each carrying a ``filename`` column tracing it back to its file — the
    exact ``read_csv(..., filename=true)`` shape from the sprint spec."""
    pattern = _glob_pattern(outputs_dir, "csv")
    sql = f"SELECT * FROM read_csv('{pattern}', filename=true, union_by_name=true)"
    return _run_duckdb(sql, sql_runner)


def index_json(
    outputs_dir: str, *, sql_runner: Callable[[str], Any] | None = None
) -> list[dict[str, Any]]:
    """Unified JSON index across ``outputs_dir`` (recursive) — the ``read_json``
    counterpart of :func:`index_csv`."""
    pattern = _glob_pattern(outputs_dir, "json")
    sql = f"SELECT * FROM read_json('{pattern}', filename=true, union_by_name=true)"
    return _run_duckdb(sql, sql_runner)


# ---------------------------------------------------------------------------
# watchdog wiring
# ---------------------------------------------------------------------------

def _build_handler(on_event: Callable[[str, str], None]) -> Any:
    """A ``PatternMatchingEventHandler`` over :data:`WATCHED_PATTERNS` that calls
    ``on_event(event_type, src_path)`` for created/modified/deleted files
    (directories are ignored)."""
    from watchdog.events import PatternMatchingEventHandler  # noqa: PLC0415

    class _Handler(PatternMatchingEventHandler):
        def on_created(self, event: Any) -> None:
            if not event.is_directory:
                on_event("created", event.src_path)

        def on_modified(self, event: Any) -> None:
            if not event.is_directory:
                on_event("modified", event.src_path)

        def on_deleted(self, event: Any) -> None:
            if not event.is_directory:
                on_event("deleted", event.src_path)

    return _Handler(patterns=list(WATCHED_PATTERNS), ignore_directories=True)


class OutputsIndexer:
    """Recursive watcher + queryable index over an outputs directory tree.

    ``sql_runner`` is an injectable seam for tests (a callable ``sql -> relation``
    standing in for ``duckdb.sql``); production leaves it ``None`` and uses real
    DuckDB. The npy metadata index is an in-memory dict kept current via
    :meth:`handle_event` (called by the watchdog handler, or directly in tests —
    no real filesystem-event delivery required to exercise the logic).
    """

    def __init__(
        self, outputs_dir: str, *, sql_runner: Callable[[str], Any] | None = None
    ) -> None:
        self.outputs_dir = outputs_dir
        self._sql_runner = sql_runner
        self._observer: Any = None
        self._lock = threading.Lock()
        self.npy_index: dict[str, NpyMetadata] = {}
        self.events: list[tuple[str, str]] = []

    # -- unified tabular index (always computed fresh from disk) ------------

    def csv_rows(self) -> list[dict[str, Any]]:
        return index_csv(self.outputs_dir, sql_runner=self._sql_runner)

    def json_rows(self) -> list[dict[str, Any]]:
        return index_json(self.outputs_dir, sql_runner=self._sql_runner)

    # -- npy metadata index ---------------------------------------------------

    def refresh_npy_index(self) -> None:
        """Rebuild :attr:`npy_index` from a full walk of ``outputs_dir`` (the
        initial-state pass — watchdog only reports CHANGES after it starts, so
        pre-existing files need this explicit walk)."""
        fresh: dict[str, NpyMetadata] = {}
        for root, _dirs, files in os.walk(self.outputs_dir):
            for fn in files:
                if fn.lower().endswith(".npy"):
                    p = os.path.join(root, fn)
                    fresh[p] = npy_metadata(p)
        with self._lock:
            self.npy_index = fresh

    # -- event handling (the watchdog callback target) -----------------------

    def handle_event(self, event_type: str, src_path: str) -> None:
        """Handle one created/modified/deleted event for a watched file.

        ``.npy`` events update :attr:`npy_index` directly (removed on delete,
        (re)computed on created/modified). ``.csv``/``.json`` events need no
        per-file cache maintenance — :meth:`csv_rows`/:meth:`json_rows` always
        recompute fresh straight from disk, so a create/modify/delete is simply
        reflected the next time either is called.
        """
        self.events.append((event_type, src_path))
        if src_path.lower().endswith(".npy"):
            with self._lock:
                if event_type == "deleted":
                    self.npy_index.pop(src_path, None)
                else:
                    self.npy_index[src_path] = npy_metadata(src_path)

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Start watching ``outputs_dir`` (recursive) and build the initial npy
        index. Idempotent — a second call while already running is a no-op."""
        if self._observer is not None:
            return
        from watchdog.observers import Observer  # noqa: PLC0415

        os.makedirs(self.outputs_dir, exist_ok=True)
        self.refresh_npy_index()
        handler = _build_handler(self.handle_event)
        observer = Observer()
        observer.schedule(handler, self.outputs_dir, recursive=True)
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        """Stop watching (best-effort, idempotent)."""
        if self._observer is None:
            return
        try:
            self._observer.stop()
            self._observer.join(timeout=5)
        except Exception:  # noqa: BLE001 — shutdown best-effort
            _log.debug("OutputsIndexer.stop() failed", exc_info=True)
        finally:
            self._observer = None

    @property
    def running(self) -> bool:
        return self._observer is not None
