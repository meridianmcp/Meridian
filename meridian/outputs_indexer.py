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

import csv
import hashlib
import io
import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

_log = logging.getLogger(__name__)

# The three extensions this indexer watches/indexes — everything else (images,
# other binaries) is explicitly out of scope (see module docstring).
WATCHED_PATTERNS: tuple[str, ...] = ("*.csv", "*.json", "*.npy")

# Extensions the PERSISTENT FTS table walks (a0e9133e). Broader than
# WATCHED_PATTERNS's tabular/array set: image + other-binary artifacts are
# indexed too, but at METADATA-ONLY level (no content parsing, no FTS body) so a
# ``search_outputs`` hit can still surface "this figure exists" without ever
# opening the binary. .csv/.json carry extracted text content for BM25; .npy is
# metadata-only via the mmap header; everything else is filesystem metadata only.
_TEXT_CONTENT_SUFFIXES: frozenset[str] = frozenset({".csv", ".json"})
_METADATA_ONLY_SUFFIXES: frozenset[str] = frozenset({".npy"})

# How much extracted text to keep per file in the FTS ``content`` column. Plenty
# for BM25 term coverage; caps memory on a pathologically large CSV/JSON.
_MAX_CONTENT_CHARS = 200_000

# Stage-1 archival filename heuristic (a0e9133e): a trailing ``_old`` /
# ``_old_1``/``_old_2``/… suffix on the stem, OR a leading underscore, flags a
# file as a CANDIDATE archival copy. Confirmation is Stage-2 (SHA-256).
_ARCHIVAL_SUFFIX_RE = re.compile(r"_old(?:_\d+)?$", re.IGNORECASE)


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
# Per-file cheap fingerprint (5848e8c7) + content extraction for FTS (a0e9133e)
# ---------------------------------------------------------------------------

_SCRIPT_HINT_KEYS: tuple[str, ...] = (
    "generating_script", "generated_by", "source_script", "script",
    "producer", "producer_script", "generator",
)
# In-body markers a plotting script leaves behind, e.g. a matplotlib figure JSON
# or a sidecar manifest: "generated by plot_results.py".
_SCRIPT_HINT_RE = re.compile(
    r"(?:generated\s+by|produced\s+by|source[:=])\s*['\"]?"
    r"([\w./\\-]+\.(?:py|R|jl|ipynb|sh|m))",
    re.IGNORECASE,
)


@dataclass
class FileFingerprint:
    """A cheap, content-derived signature for one output file (5848e8c7).

    Lets "does this plot/table already exist?" be answered WITHOUT re-opening
    and re-parsing every candidate: for a CSV the column names, for a JSON its
    top-level keys, plus a best-effort ``generating_script`` when one is
    inferable from the file's content. ``kind`` records how the file was
    treated (text-content / metadata-only / binary-metadata).
    """

    path: str
    kind: str
    csv_columns: list[str] | None = None
    json_keys: list[str] | None = None
    generating_script: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _infer_generating_script_from_text(text: str) -> str | None:
    """Best-effort: scan a blob of text for a 'generated by <script>.py' marker."""
    m = _SCRIPT_HINT_RE.search(text)
    return m.group(1) if m else None


def _infer_generating_script_from_obj(obj: Any) -> str | None:
    """Pull a generating-script hint out of a parsed JSON object's known keys."""
    if isinstance(obj, dict):
        for key in _SCRIPT_HINT_KEYS:
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _extract_csv(text: str) -> tuple[list[str] | None, str | None]:
    """Return (column_names, generating_script_hint) for CSV text.

    Column names come from the header row via the stdlib ``csv`` sniffer-free
    reader (first row). A script hint is only picked up if the CSV embeds a
    'generated by ...' comment line — rare, but free to check.
    """
    columns: list[str] | None = None
    try:
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            columns = [c.strip() for c in row]
            break
    except Exception:  # noqa: BLE001 — a malformed CSV yields no columns, never raises
        columns = None
    return columns, _infer_generating_script_from_text(text)


def _extract_json(text: str) -> tuple[list[str] | None, str | None]:
    """Return (top_level_keys, generating_script_hint) for JSON text."""
    keys: list[str] | None = None
    script: str | None = None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            keys = [str(k) for k in obj.keys()]
        script = _infer_generating_script_from_obj(obj)
    except Exception:  # noqa: BLE001 — non-JSON / partial file: no keys, never raises
        keys = None
    if script is None:
        script = _infer_generating_script_from_text(text)
    return keys, script


def _read_text_capped(path: str) -> str:
    """Read up to :data:`_MAX_CONTENT_CHARS` chars of a text file (utf-8, lenient)."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(_MAX_CONTENT_CHARS)


def _sha256_file(path: str) -> str | None:
    """SHA-256 of a file's bytes, streamed in chunks. None if unreadable."""
    try:
        h = hashlib.sha256()
        with open(path, "rb", buffering=0) as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _classify_suffix(path: str) -> str:
    """Which indexing tier a path falls into (see the suffix constants)."""
    suffix = os.path.splitext(path)[1].lower()
    if suffix in _TEXT_CONTENT_SUFFIXES:
        return "text_content"
    if suffix in _METADATA_ONLY_SUFFIXES:
        return "metadata_only"
    return "binary_metadata"


def file_fingerprint(path: str) -> FileFingerprint:
    """Compute the cheap :class:`FileFingerprint` for one output file.

    * ``.csv`` → column names (header row) + any embedded script hint.
    * ``.json`` → top-level keys + a ``generating_script`` key/marker if present.
    * ``.npy`` and other binaries → no content parse (metadata-only /
      binary-metadata); ``generating_script`` may still be inferred from a
      sibling name pattern is NOT attempted here (kept cheap) — the field stays
      ``None``.

    Never raises: an unreadable/corrupt file yields a fingerprint with the
    content fields left ``None``.
    """
    kind = _classify_suffix(path)
    if kind != "text_content":
        return FileFingerprint(path=path, kind=kind)
    try:
        text = _read_text_capped(path)
    except OSError:
        return FileFingerprint(path=path, kind=kind)
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".csv":
        columns, script = _extract_csv(text)
        return FileFingerprint(
            path=path, kind=kind, csv_columns=columns, generating_script=script,
        )
    keys, script = _extract_json(text)
    return FileFingerprint(
        path=path, kind=kind, json_keys=keys, generating_script=script,
    )


def _content_for_fts(path: str, fingerprint: FileFingerprint) -> str:
    """The text body indexed into the FTS ``content`` column for one file.

    text-content files (.csv/.json) contribute their (capped) raw text PLUS a
    header line of the fingerprint's structural terms (column names / keys /
    generating script) so those are BM25-searchable even when they don't appear
    verbatim in the body. metadata-only / binary files contribute ONLY their
    basename + structural terms — never parsed content (an .npy array, an image)
    — so they stay findable by name/script without content indexing.
    """
    terms: list[str] = [os.path.basename(path)]
    if fingerprint.csv_columns:
        terms.extend(fingerprint.csv_columns)
    if fingerprint.json_keys:
        terms.extend(fingerprint.json_keys)
    if fingerprint.generating_script:
        terms.append(fingerprint.generating_script)
    header = " ".join(terms)
    if fingerprint.kind != "text_content":
        return header
    try:
        body = _read_text_capped(path)
    except OSError:
        body = ""
    return f"{header}\n{body}"


# ---------------------------------------------------------------------------
# Canonical-vs-archival — TWO-STAGE, never destructive (a0e9133e)
# ---------------------------------------------------------------------------

def archival_candidate(path: str) -> bool:
    """Stage 1 — cheap FILENAME heuristic: is this a CANDIDATE archival copy?

    Flags a file whose stem ends in ``_old`` / ``_old_1`` / ``_old_2`` / … OR
    whose basename starts with a leading underscore. This is a CANDIDATE only —
    it is never acted on without the Stage-2 SHA-256 confirmation, and NOTHING
    is ever deleted or hidden on the strength of the name alone.
    """
    base = os.path.basename(path)
    stem = os.path.splitext(base)[0]
    if base.startswith("_"):
        return True
    return bool(_ARCHIVAL_SUFFIX_RE.search(stem))


def _canonical_name(path: str) -> str:
    """The name a candidate archival file would have WITHOUT its archival marker.

    ``run_old.csv`` → ``run.csv``; ``run_old_2.csv`` → ``run.csv``;
    ``_run.csv`` → ``run.csv``. Used to pair a candidate with its canonical
    twin for the Stage-2 hash comparison. Non-candidates map to themselves.
    """
    directory = os.path.dirname(path)
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    stem = _ARCHIVAL_SUFFIX_RE.sub("", stem)
    if base.startswith("_"):
        stem = stem.lstrip("_")
    return os.path.join(directory, f"{stem}{ext}")


@dataclass
class ArchivalClassification:
    """The Stage-2 verdict for one file in a name-variant group.

    ``is_archival`` is only ever True when a candidate's SHA-256 CONFIRMS it is
    byte-identical to a non-archival twin. A candidate whose hash DIFFERS from
    its twin (or which has no twin) is a genuinely distinct file — surfaced as
    canonical, never collapsed. Nothing here removes a file; ``is_archival`` is
    purely a ranking-deprioritization signal for search.
    """

    path: str
    is_archival: bool
    canonical_path: str | None = None  # the twin it duplicates, when archival
    reason: str = ""


def classify_canonical_archival(
    paths: list[str], *, hasher: Callable[[str], str | None] = _sha256_file,
) -> dict[str, ArchivalClassification]:
    """Two-stage canonical-vs-archival classification over a set of files.

    Stage 1 (cheap): :func:`archival_candidate` flags name-pattern candidates.
    Stage 2 (confirming): for each candidate, its canonical twin (same directory
    + de-marked name) is looked up in ``paths`` and SHA-256 hashed. Identical
    hash → the candidate IS archival (``canonical_path`` set to the twin).
    Different hash (or no twin present, or an unreadable file) → NOT archival:
    the file is genuinely distinct and both variants are surfaced. Never
    destructive — this only labels rows for ranking.

    ``hasher`` is injectable for tests. Returns ``{path: classification}`` for
    EVERY input path (non-candidates included, as ``is_archival=False``).
    """
    path_set = set(paths)
    # Memoize hashes so a canonical twin shared by several candidates hashes once.
    _hash_cache: dict[str, str | None] = {}

    def _h(p: str) -> str | None:
        if p not in _hash_cache:
            _hash_cache[p] = hasher(p)
        return _hash_cache[p]

    out: dict[str, ArchivalClassification] = {}
    for path in paths:
        if not archival_candidate(path):
            out[path] = ArchivalClassification(
                path=path, is_archival=False, reason="not a name-pattern candidate",
            )
            continue
        twin = _canonical_name(path)
        if twin == path or twin not in path_set:
            out[path] = ArchivalClassification(
                path=path, is_archival=False,
                reason="archival name pattern but no canonical twin present",
            )
            continue
        cand_hash = _h(path)
        twin_hash = _h(twin)
        if cand_hash is not None and twin_hash is not None and cand_hash == twin_hash:
            out[path] = ArchivalClassification(
                path=path, is_archival=True, canonical_path=twin,
                reason="SHA-256 identical to canonical twin",
            )
        else:
            out[path] = ArchivalClassification(
                path=path, is_archival=False, canonical_path=twin,
                reason="archival name pattern but content DIFFERS from twin — distinct file",
            )
    return out


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


# The extensions the persistent FTS table walks — the tabular/array set PLUS
# everything else (each other file becomes a binary-metadata row). Directory
# walking uses this to decide which files get a row at all: we index EVERY
# regular file so a figure/image is discoverable, but only .csv/.json get real
# text content (see _content_for_fts).
def _iter_output_files(outputs_dir: str) -> list[str]:
    """Every regular file under ``outputs_dir`` (recursive), sorted for stability."""
    found: list[str] = []
    for root, _dirs, files in os.walk(outputs_dir):
        for fn in files:
            found.append(os.path.join(root, fn))
    found.sort()
    return found


@dataclass
class OutputRow:
    """One row of the persistent ``outputs_index`` table (a0e9133e)."""

    path: str
    content: str
    mtime: float | None
    sha256: str | None
    size: int | None
    generating_script: str | None
    kind: str = ""
    is_archival: bool = False
    canonical_path: str | None = None
    csv_columns: list[str] | None = None
    json_keys: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_output_rows(
    outputs_dir: str, *, hasher: Callable[[str], str | None] = _sha256_file,
) -> list[OutputRow]:
    """Walk ``outputs_dir`` and build one :class:`OutputRow` per regular file.

    Extracts fingerprint + FTS content per file, hashes every file (SHA-256),
    then runs the two-stage canonical-vs-archival classification across the
    whole set. Never raises on an individual bad file — a file that vanished
    mid-walk or is unreadable simply contributes a best-effort row.
    """
    paths = _iter_output_files(outputs_dir)
    classifications = classify_canonical_archival(paths, hasher=hasher)
    rows: list[OutputRow] = []
    for path in paths:
        fp = file_fingerprint(path)
        try:
            st = os.stat(path)
            size: int | None = st.st_size
            mtime: float | None = st.st_mtime
        except OSError:
            size = mtime = None
        cls = classifications.get(path)
        rows.append(OutputRow(
            path=path,
            content=_content_for_fts(path, fp),
            mtime=mtime,
            sha256=hasher(path),
            size=size,
            generating_script=fp.generating_script,
            kind=fp.kind,
            is_archival=bool(cls and cls.is_archival),
            canonical_path=(cls.canonical_path if cls else None),
            csv_columns=fp.csv_columns,
            json_keys=fp.json_keys,
        ))
    return rows


class OutputsFtsIndex:
    """Persistent DuckDB ``outputs_index`` table + native FTS (Okapi BM25).

    Backs the ``search_outputs`` MCP tool. Holds a single owned in-process
    DuckDB connection (``:memory:`` by default; a file path persists across
    process restarts). :meth:`rebuild` walks the outputs tree, replaces the
    table's rows, and — CRITICAL, per the DuckDB FTS contract — REBUILDS the FTS
    index with ``overwrite`` every time, because the FTS index does NOT
    auto-update when its source table changes. :meth:`search` runs a BM25 query,
    deprioritizing (never excluding) archival-flagged rows.

    ``connection`` / ``hasher`` are injectable for tests; production leaves them
    default. Thread-safe: every DB op holds an internal lock so the watchdog
    thread and a query thread never touch the connection concurrently.
    """

    _COLUMNS = (
        "path", "content", "mtime", "sha256", "size", "generating_script",
        "kind", "is_archival", "canonical_path", "csv_columns", "json_keys",
    )

    def __init__(
        self,
        outputs_dir: str,
        *,
        db_path: str = ":memory:",
        connection: Any = None,
        hasher: Callable[[str], str | None] = _sha256_file,
    ) -> None:
        self.outputs_dir = outputs_dir
        self._db_path = db_path
        self._hasher = hasher
        self._lock = threading.RLock()
        self._con = connection
        self._owns_con = connection is None
        self._fts_built = False

    # -- connection / schema --------------------------------------------------

    def _connect(self) -> Any:
        if self._con is None:
            import duckdb  # noqa: PLC0415 — optional/lazy

            self._con = duckdb.connect(self._db_path)
        return self._con

    def _ensure_schema(self, con: Any) -> None:
        con.execute(
            "CREATE TABLE IF NOT EXISTS outputs_index ("
            "path VARCHAR PRIMARY KEY, content VARCHAR, mtime DOUBLE, "
            "sha256 VARCHAR, size BIGINT, generating_script VARCHAR, "
            "kind VARCHAR, is_archival BOOLEAN, canonical_path VARCHAR, "
            "csv_columns VARCHAR, json_keys VARCHAR)"
        )

    # -- (re)build -----------------------------------------------------------

    def rebuild(self) -> int:
        """Rebuild the table from a full walk of ``outputs_dir`` AND rebuild the
        FTS index (``overwrite``). Returns the row count. Idempotent; safe to
        call on every watchdog event. Best-effort: a DuckDB/FTS failure is logged
        and swallowed (the indexer must never crash a run), returning the count
        of rows that were staged."""
        with self._lock:
            rows = build_output_rows(self.outputs_dir, hasher=self._hasher)
            try:
                con = self._connect()
                self._ensure_schema(con)
                con.execute("DELETE FROM outputs_index")
                for r in rows:
                    con.execute(
                        "INSERT INTO outputs_index VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            r.path, r.content, r.mtime, r.sha256, r.size,
                            r.generating_script, r.kind, r.is_archival,
                            r.canonical_path,
                            json.dumps(r.csv_columns) if r.csv_columns else None,
                            json.dumps(r.json_keys) if r.json_keys else None,
                        ],
                    )
                self._rebuild_fts(con)
            except Exception:  # noqa: BLE001 — never crash the watcher/run
                _log.debug("OutputsFtsIndex.rebuild failed", exc_info=True)
            return len(rows)

    def _rebuild_fts(self, con: Any) -> None:
        """(Re)build the DuckDB FTS index over ``content``, keyed on ``path``.

        The FTS index does NOT track source-table changes, so this MUST run on
        every rebuild — with ``overwrite`` so a pre-existing index is replaced,
        not errored on. ``stemmer='none'`` keeps exact-token matching (paths,
        column names, script names shouldn't be stemmed)."""
        con.execute("INSTALL fts")
        con.execute("LOAD fts")
        con.execute(
            "PRAGMA create_fts_index("
            "'outputs_index', 'path', 'content', "
            "stemmer = 'porter', stopwords = 'none', overwrite = 1)"
        )
        self._fts_built = True

    # -- search --------------------------------------------------------------

    def search(
        self, query: str, *, limit: int = 10, include_archival: bool = True,
    ) -> list[dict[str, Any]]:
        """BM25 search over the FTS index. Returns ranked hits, each a dict with
        path / score / fingerprint fields / is_archival + canonical_path.

        Archival-flagged rows are DEPRIORITIZED (their score is discounted so
        they sink below equally-relevant canonical rows) but NEVER hard-excluded
        — pass ``include_archival=False`` to drop them entirely. Best-effort: an
        empty index or a query error yields ``[]``, never raises."""
        q = (query or "").strip()
        if not q:
            return []
        with self._lock:
            try:
                con = self._connect()
                self._ensure_schema(con)
                if not self._fts_built:
                    self._rebuild_fts(con)
                # match_bm25 returns NULL for non-matching rows; filter them out.
                sql = (
                    "SELECT path, content, mtime, sha256, size, generating_script, "
                    "kind, is_archival, canonical_path, csv_columns, json_keys, "
                    "fts_main_outputs_index.match_bm25(path, ?) AS bm25 "
                    "FROM outputs_index"
                )
                relation = con.execute(sql, [q])
                columns = [c[0] for c in relation.description]
                fetched = relation.fetchall()
            except Exception:  # noqa: BLE001
                _log.debug("OutputsFtsIndex.search failed", exc_info=True)
                return []
        hits: list[dict[str, Any]] = []
        for row in fetched:
            rec = dict(zip(columns, row))
            bm25 = rec.get("bm25")
            if bm25 is None:
                continue
            is_arch = bool(rec.get("is_archival"))
            if is_arch and not include_archival:
                continue
            # Deprioritize archival: halve the score so a canonical twin with the
            # same relevance always ranks above it, without hard exclusion.
            score = float(bm25) * (0.5 if is_arch else 1.0)
            hits.append({
                "path": rec["path"],
                "score": score,
                "bm25": float(bm25),
                "is_archival": is_arch,
                "canonical_path": rec.get("canonical_path"),
                "kind": rec.get("kind"),
                "generating_script": rec.get("generating_script"),
                "csv_columns": json.loads(rec["csv_columns"]) if rec.get("csv_columns") else None,
                "json_keys": json.loads(rec["json_keys"]) if rec.get("json_keys") else None,
                "size": rec.get("size"),
                "mtime": rec.get("mtime"),
            })
        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits[: max(1, int(limit))]

    def close(self) -> None:
        """Close the owned DuckDB connection (no-op for an injected one)."""
        with self._lock:
            if self._owns_con and self._con is not None:
                try:
                    self._con.close()
                except Exception:  # noqa: BLE001
                    _log.debug("OutputsFtsIndex.close failed", exc_info=True)
            if self._owns_con:
                self._con = None
                self._fts_built = False


def search_outputs(
    outputs_dir: str,
    query: str,
    *,
    limit: int = 10,
    include_archival: bool = True,
) -> dict[str, Any]:
    """Stateless one-shot BM25 search over an outputs tree (backs the
    ``search_outputs`` MCP tool).

    Walks ``outputs_dir``, builds the persistent-table + FTS index in an
    in-memory DuckDB, runs the BM25 query, and returns ranked hits. Archival
    rows are DEPRIORITIZED (halved score), never hard-excluded, unless
    ``include_archival=False``. A missing directory / empty tree returns an
    empty result rather than an error. Each hit carries path, score, the cheap
    fingerprint (csv_columns / json_keys / generating_script), the archival flag
    and its canonical twin.
    """
    result: dict[str, Any] = {
        "outputs_dir": outputs_dir,
        "query": query,
        "hits": [],
        "total_indexed": 0,
    }
    if not query or not str(query).strip():
        result["error"] = "query is required"
        return result
    if not os.path.isdir(outputs_dir):
        result["error"] = f"outputs_dir does not exist: {outputs_dir}"
        return result
    index = OutputsFtsIndex(outputs_dir)
    try:
        result["total_indexed"] = index.rebuild()
        result["hits"] = index.search(
            query, limit=limit, include_archival=include_archival,
        )
    finally:
        index.close()
    return result


class OutputsIndexer:
    """Recursive watcher + queryable index over an outputs directory tree.

    ``sql_runner`` is an injectable seam for tests (a callable ``sql -> relation``
    standing in for ``duckdb.sql``); production leaves it ``None`` and uses real
    DuckDB. The npy metadata index is an in-memory dict kept current via
    :meth:`handle_event` (called by the watchdog handler, or directly in tests —
    no real filesystem-event delivery required to exercise the logic).

    When ``enable_fts`` is set (default), the indexer also owns an
    :class:`OutputsFtsIndex`: the initial walk populates it, and every watchdog
    event triggers a full table + FTS rebuild (the FTS index can't track source
    changes incrementally). ``search_outputs`` reads through :meth:`search`.
    """

    def __init__(
        self,
        outputs_dir: str,
        *,
        sql_runner: Callable[[str], Any] | None = None,
        enable_fts: bool = True,
        fts_db_path: str = ":memory:",
    ) -> None:
        self.outputs_dir = outputs_dir
        self._sql_runner = sql_runner
        self._observer: Any = None
        self._lock = threading.Lock()
        self.npy_index: dict[str, NpyMetadata] = {}
        self.events: list[tuple[str, str]] = []
        self.fts: OutputsFtsIndex | None = (
            OutputsFtsIndex(outputs_dir, db_path=fts_db_path)
            if enable_fts else None
        )

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
        # a0e9133e — the persistent FTS index can't track source-table changes
        # incrementally, so ANY watched change (create/modify/delete of a
        # csv/json/npy) triggers a full table + FTS rebuild with overwrite.
        if self.fts is not None:
            self.fts.rebuild()

    def search(
        self, query: str, *, limit: int = 10, include_archival: bool = True,
    ) -> list[dict[str, Any]]:
        """BM25 search over the persistent FTS index (backs ``search_outputs``).

        Returns ``[]`` when FTS is disabled. Delegates to
        :meth:`OutputsFtsIndex.search`; archival rows are deprioritized, not
        excluded (unless ``include_archival=False``)."""
        if self.fts is None:
            return []
        return self.fts.search(
            query, limit=limit, include_archival=include_archival,
        )

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Start watching ``outputs_dir`` (recursive) and build the initial npy
        index. Idempotent — a second call while already running is a no-op."""
        if self._observer is not None:
            return
        from watchdog.observers import Observer  # noqa: PLC0415

        os.makedirs(self.outputs_dir, exist_ok=True)
        self.refresh_npy_index()
        if self.fts is not None:
            self.fts.rebuild()  # initial-state pass: index pre-existing files
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
            if self.fts is not None:
                self.fts.close()

    @property
    def running(self) -> bool:
        return self._observer is not None
