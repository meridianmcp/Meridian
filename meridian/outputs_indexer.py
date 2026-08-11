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
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

_log = logging.getLogger(__name__)

# Wall-clock budget for one rebuild() pass (5116078b): a cold/huge outputs tree
# bails out early with whatever rows it managed to (re)compute rather than
# hanging until an external client-side timeout kicks in. Subsequent calls
# pick up where this one left off (unprocessed files are still "stale" in the
# manifest and get retried). None disables the budget entirely.
DEFAULT_REBUILD_BUDGET_SECONDS = 5.0

# search_outputs()/resolve_figure_output() are stateless call sites hit
# repeatedly (once per MCP call) for a small set of distinct outputs_dir
# trees — cache the OutputsFtsIndex per directory so the incremental rebuild()
# below has a manifest to diff against instead of starting from empty every
# call. Bounded + LRU so an unbounded number of distinct directories (hosted,
# multi-tenant) can't leak DuckDB connections forever.
_MAX_CACHED_INDEXES = 32
_index_cache_lock = threading.Lock()
_index_cache: OrderedDict[str, OutputsFtsIndex] = OrderedDict()

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


def _normalize_output_path(path: Any) -> str:
    """Canonicalize a filesystem path for cross-store equality matching (d2a3537a).

    A document figure's stored ``file_path`` and an ``outputs_index`` row's
    ``path`` name the SAME file but need not be byte-identical strings — one may
    use back-slashes, the other forward; one may be relative, one absolute; case
    can differ on Windows. This normalizes both sides the same way so
    :meth:`OutputsFtsIndex.resolve_output` matches them: ``os.path.normpath`` +
    ``os.path.normcase`` on the absolute path, with separators unified to ``/``.
    Blank / non-string input yields ``""`` (never matches anything).
    """
    if not isinstance(path, str):
        return ""
    s = path.strip()
    if not s:
        return ""
    try:
        s = os.path.abspath(s)
    except (OSError, ValueError):
        pass
    # normcase lower-cases + flips slashes on Windows; normpath collapses ./..;
    # unify to forward slashes so a cross-platform stored path still matches.
    return os.path.normcase(os.path.normpath(s)).replace("\\", "/")


def _basename_key(path: Any) -> str:
    """Case/slash-insensitive basename key, for relocation-tolerant matching
    (mirrors the same helper in the meridian-outputs tunnel plugin's
    ``provenance.py``, kept dependency-free/duplicated rather than importing
    across the core/extension boundary)."""
    if not path:
        return ""
    s = str(path).replace("\\", "/").rstrip("/")
    return os.path.normcase(os.path.basename(s))


def _path_key(path: Any) -> str:
    """Case/slash-insensitive key for a path-like STRING (no ``abspath``).

    Deliberately does NOT resolve against the current working directory (unlike
    :func:`_normalize_output_path`, used for figure/output-path equality):
    ``generating_script`` values are inferred from free text (a CSV header
    comment, a JSON key) and are frequently a bare filename or a short relative
    fragment rather than a path meant to be resolved on this machine. Running
    them through ``os.path.abspath`` would silently rebase them onto an
    unrelated CWD and produce false negatives/positives. This key only
    normalizes case and slash direction so two textually-equivalent references
    compare equal.
    """
    if not path:
        return ""
    return os.path.normcase(str(path).strip().replace("\\", "/"))


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
MERIDIAN_NOTES_FILENAME = "MERIDIAN_NOTES.md"
"""The well-known filename for human-authored outputs annotations.

Any ``MERIDIAN_NOTES.md`` file found ANYWHERE in the walked outputs tree is
auto-ingested into the ``annotations`` table keyed to the directory it was
found in. This is a TESTED, ENFORCED step in the directory walk — NOT an
advisory convention. Deliberately NOT ``README.md`` to avoid collision with
existing package readmes or other tool conventions.
"""


def _iter_output_files(outputs_dir: str) -> list[str]:
    """Every regular file under ``outputs_dir`` (recursive), sorted for stability.

    ``MERIDIAN_NOTES.md`` files are included in the walk (they are real
    filesystem files) but they are handled separately in
    :meth:`OutputsFtsIndex._ingest_meridian_notes` — they are NOT indexed as
    FTS content rows (which would pollute search results with annotation text).
    The caller (:meth:`OutputsFtsIndex.rebuild`) is responsible for calling
    ``_ingest_meridian_notes`` so the pickup is a guaranteed, tested step.
    """
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
    process restarts). :meth:`rebuild` is INCREMENTAL (5116078b): it walks the
    tree (cheap — stat only) and, for each file, compares (mtime, size) against
    the signature used to build its currently-cached row — only files that are
    new, changed, or removed since the last call get re-hashed / re-parsed /
    re-extracted. The DuckDB table + FTS index are only rewritten when at least
    one row actually changed (rewriting an unchanged table wastes the same
    O(tree) DuckDB work the incremental hashing above was added to avoid).
    :meth:`search` runs a BM25 query, deprioritizing (never excluding)
    archival-flagged rows.

    ``connection`` / ``hasher`` are injectable for tests; production leaves them
    default. Thread-safe: every DB op holds an internal lock so the watchdog
    thread and a query thread never touch the connection concurrently.

    a52216e2 -- NO process-aware cross-process locking here (deliberately):
    the only production caller of this class (``meridian/mcp/handlers/
    notes_decisions.py``) always constructs it with the default
    ``db_path=":memory:"`` -- a fresh, per-call, per-process in-memory DB, so
    there is no shared on-disk file for two Meridian server processes to
    contend over in the first place. The real cross-process ownership/lease
    problem this item was opened to fix lives in the SEPARATE, standalone
    ``extensions/meridian-outputs`` package (its own copy of this class,
    persisted to ``<outputs_dir>/.meridian-outputs-cache/index.duckdb`` and
    shared across however many ``meridian-outputs-mcp`` server processes are
    running against the same outputs_dir) -- see
    ``extensions/meridian-outputs/meridian_outputs/outputs_local.py``'s
    ``IndexFileLock`` / ``read_index_lock_owner`` / ``IndexLockOwner`` for the
    process-aware single-writer lease (owner pid/hostname/session_id/
    started_at/heartbeat_at, active-vs-stale distinction, and a genuine
    acquisition failure that raises instead of being silently swallowed). If
    a caller ever DOES construct this class against a real, persistent,
    multi-process-shared ``db_path``, it should use that implementation's
    locking primitives rather than assuming this one provides any -- it does
    not.
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
        # Incremental-rebuild state (5116078b): the (mtime, size) signature and
        # computed OutputRow last used for each path, so an unchanged file is
        # never re-hashed/re-parsed on a later rebuild() call.
        self._manifest: dict[str, tuple[float | None, int | None]] = {}
        self._row_cache: dict[str, OutputRow] = {}
        self.last_rebuild_partial = False
        # 5b897ad3 — convergence-authoritative signals, mirroring the
        # extensions/meridian-outputs package's OutputsFtsIndex contract
        # (1a799e52/81a0b23d there): a large/cold tree can legitimately need
        # several rebuild() calls to finish, and this module previously gave
        # a caller no way to tell "still catching up" apart from "genuinely
        # nothing found" beyond the bare `last_rebuild_partial` boolean, and
        # SILENTLY swallowed DB-write failures (a real Phase 2 persistence
        # failure looked identical to a healthy index). last_pending_count is
        # the number of stale files still awaiting analysis+write as of the
        # MOST RECENT rebuild() call (0 once a call fully catches up);
        # last_db_write_error is the most recent DB-write exception's message
        # (None when the last call's write, if any, succeeded).
        self.last_pending_count = 0
        self.last_db_write_error: str | None = None

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
        # 9e02e448 — annotations table: same DuckDB, same connection. Two tiers:
        # Tier 1 = root annotation (path == outputs_dir); Tier 2 = per-path.
        # ``source`` is "tool" (from annotate_outputs) or "MERIDIAN_NOTES.md"
        # (auto-ingested from a file found in the tree).
        con.execute(
            "CREATE TABLE IF NOT EXISTS annotations ("
            "path VARCHAR NOT NULL, note VARCHAR NOT NULL, "
            "run_params_json VARCHAR, "
            "created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL, "
            "source VARCHAR NOT NULL DEFAULT 'tool')"
        )

    # -- annotations (9e02e448) -----------------------------------------------

    def add_annotation(
        self,
        path: str,
        note: str,
        *,
        run_params: dict[str, Any] | None = None,
        source: str = "tool",
    ) -> dict[str, Any]:
        """Upsert an annotation for ``path`` in the ``annotations`` table.

        If an annotation already exists for ``(path, source)`` it is updated
        (note + run_params + updated_at); otherwise a new row is inserted.
        Returns the stored annotation as a dict. Thread-safe (holds the index
        lock). Never raises on a benign DuckDB error — returns ``{error: ...}``.
        """
        now = time.time()
        run_params_json = json.dumps(run_params) if run_params else None
        with self._lock:
            try:
                con = self._connect()
                self._ensure_schema(con)
                # DuckDB doesn't have ON CONFLICT UPDATE; emulate with DELETE + INSERT.
                con.execute(
                    "DELETE FROM annotations WHERE path = ? AND source = ?",
                    [path, source],
                )
                con.execute(
                    "INSERT INTO annotations (path, note, run_params_json, "
                    "created_at, updated_at, source) VALUES (?, ?, ?, ?, ?, ?)",
                    [path, note, run_params_json, now, now, source],
                )
            except Exception as exc:  # noqa: BLE001 — never crash the caller
                _log.debug("add_annotation failed for %r", path, exc_info=True)
                return {"error": str(exc)}
        return {
            "path": path,
            "note": note,
            "run_params": run_params,
            "created_at": now,
            "updated_at": now,
            "source": source,
        }

    def get_annotations_for_path(self, path: str) -> list[dict[str, Any]]:
        """Return all annotations for ``path`` OR any of its ancestor directories.

        The auto-surfacing rule: when search_outputs returns a hit at
        ``/outputs/subdir/results.csv``, this method returns annotations keyed
        to ``/outputs/subdir/results.csv`` itself AND to ``/outputs/subdir``
        AND to ``/outputs`` — whichever of those has annotations, all are
        returned. This mirrors get_file_claims/claim_file's auto-surfacing of
        code notes: the caller never needs a second tool call to see context.
        """
        target = _normalize_output_path(path)
        if not target:
            return []
        # Build the set of ancestor paths to check (path + all parent dirs
        # down to the filesystem root).
        candidates: set[str] = {target}
        parts = target.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            if parent:
                candidates.add(parent)
        with self._lock:
            try:
                con = self._connect()
                self._ensure_schema(con)
                relation = con.execute(
                    "SELECT path, note, run_params_json, created_at, "
                    "updated_at, source FROM annotations"
                )
                columns = [c[0] for c in relation.description]
                fetched = relation.fetchall()
            except Exception:  # noqa: BLE001 — empty index yields []
                _log.debug("get_annotations_for_path failed for %r", path, exc_info=True)
                return []
        rows: list[dict[str, Any]] = []
        for row in fetched:
            rec = dict(zip(columns, row))
            norm = _normalize_output_path(rec.get("path") or "")
            if norm in candidates:
                rows.append({
                    "path": rec["path"],
                    "note": rec.get("note"),
                    "run_params": (
                        json.loads(rec["run_params_json"])
                        if rec.get("run_params_json") else None
                    ),
                    "created_at": rec.get("created_at"),
                    "updated_at": rec.get("updated_at"),
                    "source": rec.get("source"),
                })
        # Newest first within each path; parent-path annotations after hit-path.
        rows.sort(key=lambda r: (
            _normalize_output_path(r.get("path") or "") != target,
            -(r.get("updated_at") or 0),
        ))
        return rows

    def _ingest_meridian_notes(self, paths: list[str]) -> int:
        """Auto-ingest every ``MERIDIAN_NOTES.md`` found in ``paths`` into
        the ``annotations`` table keyed to its containing directory.

        This is a GUARANTEED, TESTED step called by :meth:`rebuild` on every
        rebuild pass — not an advisory convention. Returns the count of
        MERIDIAN_NOTES.md files ingested. Never raises.
        """
        ingested = 0
        for p in paths:
            if os.path.basename(p) != MERIDIAN_NOTES_FILENAME:
                continue
            directory = os.path.dirname(p)
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read(_MAX_CONTENT_CHARS)
            except OSError:
                _log.debug("_ingest_meridian_notes: could not read %r", p, exc_info=True)
                continue
            if not text.strip():
                continue
            self.add_annotation(directory, text, source=MERIDIAN_NOTES_FILENAME)
            ingested += 1
        return ingested

    # -- (re)build -----------------------------------------------------------

    def rebuild(
        self, *, max_seconds: float | None = DEFAULT_REBUILD_BUDGET_SECONDS,
    ) -> int:
        """Incrementally rebuild the table + FTS index. Returns the row count.

        Only files whose (mtime, size) changed since the last call to this
        instance are re-hashed/re-parsed (5116078b) — a repeat call on a mostly
        unchanged tree is cheap regardless of tree size. The DuckDB table + FTS
        index are only rewritten when at least one row actually changed.
        Idempotent; safe to call on every watchdog event. Best-effort: a
        DuckDB/FTS failure is logged and swallowed (the indexer must never
        crash a run), returning the count of rows that were staged.

        ``max_seconds`` bounds the wall-clock time spent (re)computing rows —
        a huge/cold tree bails out early with a partial result
        (:attr:`last_rebuild_partial` set) instead of hanging; already-cached
        rows are unaffected, and files not yet reached stay "stale" for the
        next call. ``None`` disables the budget. :attr:`last_pending_count`
        (5b897ad3) is set to how many stale files are STILL awaiting
        analysis+write as of this call — 0 once a call fully catches up, even
        if an earlier call left ``last_rebuild_partial`` set from a prior
        incomplete pass.

        A DB-write failure (the ``changed`` branch below) no longer just logs
        at DEBUG and disappears (5b897ad3) — it is captured in
        :attr:`last_db_write_error` (reset to ``None`` at the top of every
        call, so it reflects only the MOST RECENT call), mirroring the
        extensions/meridian-outputs package's ``1a799e52`` fix: a real
        persistence failure must never look identical to a healthy index.
        """
        with self._lock:
            self.last_db_write_error = None
            deadline = None if max_seconds is None else time.monotonic() + max_seconds
            # 9e02e448 — MERIDIAN_NOTES.md auto-ingest: a GUARANTEED step on
            # every rebuild so human-authored annotations are never silently
            # skipped. Called while holding the lock (add_annotation re-acquires
            # via RLock so this is safe). We walk inside _compute_rows_incremental
            # too, but the notes pickup must happen even on a partial/unchanged
            # rebuild so tests can assert it unconditionally.
            all_paths = _iter_output_files(self.outputs_dir) if os.path.isdir(self.outputs_dir) else []
            self._ingest_meridian_notes(all_paths)
            rows, changed = self._compute_rows_incremental(deadline)
            if changed:
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
                except Exception as exc:  # noqa: BLE001 — never crash the watcher/run
                    _log.debug("OutputsFtsIndex.rebuild failed", exc_info=True)
                    # 5b897ad3 — surface the failure instead of only logging
                    # at DEBUG: `rows`/`len(rows)` below are derived from the
                    # in-memory row_cache (already updated by
                    # _compute_rows_incremental BEFORE this write was
                    # attempted), so total_indexed/total_in_index would keep
                    # reporting growing "success" while nothing was actually
                    # persisted — exactly the deceptive state this attribute
                    # (and search_outputs()'s db_write_error field) exists to
                    # make visible.
                    self.last_db_write_error = str(exc)
            return len(rows)

    def _compute_rows_incremental(
        self, deadline: float | None,
    ) -> tuple[list[OutputRow], bool]:
        """Diff the current tree against :attr:`_manifest`/:attr:`_row_cache`.

        Returns ``(rows, changed)`` — ``rows`` is every currently-cacheable row
        (existing files this instance has ever computed, refreshed as needed);
        ``changed`` is True iff the caller must rewrite the DuckDB table + FTS
        index (a file was added/modified/removed, or an archival verdict
        flipped because a SIBLING file changed — see below).
        """
        self.last_rebuild_partial = False
        # 5b897ad3 — reset per-call; recomputed below from `stale` so a call
        # that fully catches up (or finds nothing stale) always reports 0,
        # never a stale count left over from an EARLIER partial call.
        self.last_pending_count = 0
        paths = _iter_output_files(self.outputs_dir)
        path_set = set(paths)
        changed = False

        removed = set(self._manifest) - path_set
        for p in removed:
            self._manifest.pop(p, None)
            self._row_cache.pop(p, None)
            changed = True

        stale: list[str] = []
        for p in paths:
            try:
                st = os.stat(p)
                sig: tuple[float | None, int | None] = (st.st_mtime, st.st_size)
            except OSError:
                sig = (None, None)
            if self._manifest.get(p) != sig or p not in self._row_cache:
                stale.append(p)

        if stale:
            # Re-run the two-stage classification over the WHOLE current path
            # set — cheap (only hashes name-pattern candidates + their twins),
            # and a sibling's add/remove/edit can flip another file's archival
            # verdict even when that file's own content is untouched.
            classifications = classify_canonical_archival(paths, hasher=self._hasher)
            processed = 0
            for p in stale:
                if deadline is not None and time.monotonic() > deadline:
                    self.last_rebuild_partial = True
                    break
                fp = file_fingerprint(p)
                try:
                    st = os.stat(p)
                    size: int | None = st.st_size
                    mtime: float | None = st.st_mtime
                except OSError:
                    size = mtime = None
                cls = classifications.get(p)
                row = OutputRow(
                    path=p,
                    content=_content_for_fts(p, fp),
                    mtime=mtime,
                    sha256=self._hasher(p),
                    size=size,
                    generating_script=fp.generating_script,
                    kind=fp.kind,
                    is_archival=bool(cls and cls.is_archival),
                    canonical_path=(cls.canonical_path if cls else None),
                    csv_columns=fp.csv_columns,
                    json_keys=fp.json_keys,
                )
                self._row_cache[p] = row
                self._manifest[p] = (mtime, size)
                changed = True
                processed += 1
            # 5b897ad3 — how many of `stale` this call did NOT get to (0 when
            # the loop above ran to completion without hitting the deadline).
            self.last_pending_count = len(stale) - processed
            # Files that didn't need re-fingerprinting may still need their
            # is_archival/canonical_path refreshed (cheap metadata-only patch,
            # no re-hash/re-read) if a sibling's change flipped their verdict.
            for p in paths:
                if p in stale:
                    continue
                row = self._row_cache.get(p)
                if row is None:
                    continue
                cls = classifications.get(p)
                new_is_archival = bool(cls and cls.is_archival)
                new_canonical = cls.canonical_path if cls else None
                if row.is_archival != new_is_archival or row.canonical_path != new_canonical:
                    row.is_archival = new_is_archival
                    row.canonical_path = new_canonical
                    changed = True

        rows = [self._row_cache[p] for p in paths if p in self._row_cache]
        return rows, changed

    def invalidate(self, path: str) -> None:
        """Force ``path`` to be re-hashed/re-parsed on the next :meth:`rebuild`.

        The mtime+size diff in :meth:`_compute_rows_incremental` is a heuristic
        (a same-size in-place edit landing inside one mtime tick could be
        missed). The live watchdog, however, KNOWS a specific file changed, so
        it drops that path's CACHED ROW here — the ``p not in self._row_cache``
        stale test then re-processes it — giving exact correctness on the
        watcher path, heuristic-only on the stateless ``search_outputs`` path.

        Only the row cache is dropped, NOT the manifest entry: a delete event
        names a path that is already gone from disk, and the manifest is what
        the removed-set diff uses to notice the deletion — popping it here would
        hide the delete and leave a stale row in the table. Unknown paths are a
        harmless no-op."""
        with self._lock:
            self._row_cache.pop(path, None)

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

    # -- resolve-through (d2a3537a) ------------------------------------------

    def resolve_output(self, file_path: str) -> dict[str, Any] | None:
        """Cross-store resolver: look up an indexed output row by its FILE PATH.

        The connective tissue between ``doc_store`` and this index (d2a3537a):
        given a document figure's ``file_path``, return the ``outputs_index`` row
        for the SAME file when the figure points at an already-indexed output —
        so "does this plot exist as a run output?" + "where is it referenced in
        my thesis?" collapse into one lookup. This is a plain equality query on
        ``path`` (NOT a BM25 relevance search), so it is exact: only a figure
        whose file_path names a real indexed output resolves through.

        Matching is path-normalized (:func:`_normalize_output_path`) so a figure
        stored with back-slashes / a trailing slash / mixed case still resolves
        to its output on Windows, without depending on the caller having stored a
        byte-identical string. Returns the linked row as a dict
        (path, generating_script, is_archival/canonical_path, sha256/mtime/size,
        kind, csv_columns, json_keys) or ``None`` when no indexed output matches.
        Never raises: an empty index or a query error yields ``None``.
        """
        target = _normalize_output_path(file_path)
        if not target:
            return None
        with self._lock:
            try:
                con = self._connect()
                self._ensure_schema(con)
                relation = con.execute(
                    "SELECT path, content, mtime, sha256, size, generating_script, "
                    "kind, is_archival, canonical_path, csv_columns, json_keys "
                    "FROM outputs_index"
                )
                columns = [c[0] for c in relation.description]
                fetched = relation.fetchall()
            except Exception:  # noqa: BLE001 — a bad/empty index resolves to None
                _log.debug("OutputsFtsIndex.resolve_output failed", exc_info=True)
                return None
        for row in fetched:
            rec = dict(zip(columns, row))
            if _normalize_output_path(rec.get("path")) == target:
                return {
                    "path": rec["path"],
                    "generating_script": rec.get("generating_script"),
                    "is_archival": bool(rec.get("is_archival")),
                    "canonical_path": rec.get("canonical_path"),
                    "sha256": rec.get("sha256"),
                    "kind": rec.get("kind"),
                    "size": rec.get("size"),
                    "mtime": rec.get("mtime"),
                    "csv_columns": (
                        json.loads(rec["csv_columns"]) if rec.get("csv_columns") else None
                    ),
                    "json_keys": (
                        json.loads(rec["json_keys"]) if rec.get("json_keys") else None
                    ),
                }
        return None

    def find_by_source(self, source_path: str) -> list[dict[str, Any]]:
        """Reverse resolver: a script/data ``source_path`` -> the outputs it produced.

        The mirror image of :meth:`resolve_output` (2ae25966): that answers "is
        THIS EXACT file already an indexed output" starting from the output
        side; this answers "what did THIS script/data file produce" starting
        from the SOURCE side — the direction plain exact/basename resolution
        can never answer. Scans the index for rows whose recorded
        ``generating_script`` traces back to ``source_path``, by either an
        exact (case/slash-insensitive) string match or a basename match (so
        ``analysis/run.py`` matches a ``generating_script`` recorded as just
        ``run.py``). This is what catches a docx figure quietly citing STALE
        data: walk the source's outputs forward, newest first, and compare
        against what the docx actually shows.

        Matching deliberately does NOT go through :func:`_normalize_output_path`
        (unlike :meth:`resolve_output`) — see :func:`_path_key` for why an
        ``abspath`` would silently rebase a free-text-inferred script name onto
        an unrelated CWD.

        Returns every matching row (same shape as :meth:`resolve_output`:
        path, generating_script, is_archival, canonical_path, sha256, kind,
        size, mtime, csv_columns, json_keys), sorted newest-first by ``mtime``,
        UNSLICED (the caller applies its own limit + reports the pre-truncation
        total). Never raises: an empty/unindexed tree or no matches yields
        ``[]``; a blank ``source_path`` also yields ``[]``.
        """
        target_path = _path_key(source_path)
        if not target_path:
            return []
        target_base = _basename_key(source_path)
        with self._lock:
            try:
                con = self._connect()
                self._ensure_schema(con)
                relation = con.execute(
                    "SELECT path, content, mtime, sha256, size, generating_script, "
                    "kind, is_archival, canonical_path, csv_columns, json_keys "
                    "FROM outputs_index"
                )
                columns = [c[0] for c in relation.description]
                fetched = relation.fetchall()
            except Exception:  # noqa: BLE001 — a bad/empty index resolves to []
                _log.debug("OutputsFtsIndex.find_by_source failed", exc_info=True)
                return []
        matches: list[dict[str, Any]] = []
        for row in fetched:
            rec = dict(zip(columns, row))
            gs = rec.get("generating_script")
            if not gs:
                continue
            if _path_key(gs) != target_path and _basename_key(gs) != target_base:
                continue
            matches.append({
                "path": rec["path"],
                "generating_script": gs,
                "is_archival": bool(rec.get("is_archival")),
                "canonical_path": rec.get("canonical_path"),
                "sha256": rec.get("sha256"),
                "kind": rec.get("kind"),
                "size": rec.get("size"),
                "mtime": rec.get("mtime"),
                "csv_columns": (
                    json.loads(rec["csv_columns"]) if rec.get("csv_columns") else None
                ),
                "json_keys": (
                    json.loads(rec["json_keys"]) if rec.get("json_keys") else None
                ),
            })
        matches.sort(key=lambda h: (h.get("mtime") or 0), reverse=True)
        return matches

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


def _cache_key(outputs_dir: str) -> str:
    return _normalize_output_path(outputs_dir) or os.path.abspath(str(outputs_dir))


def _get_cached_index(outputs_dir: str) -> OutputsFtsIndex:
    """Look up (or create) the cached :class:`OutputsFtsIndex` for a directory.

    ``search_outputs``/``resolve_figure_output`` are stateless call sites hit
    repeatedly for a small set of distinct trees (once per MCP call) — without
    this cache each call would start ``rebuild()``'s manifest from empty,
    defeating the incremental diff entirely. Bounded + LRU (evicting closes the
    connection) so an unbounded number of distinct directories can't leak
    DuckDB connections in a long-running (hosted, multi-tenant) process.
    """
    key = _cache_key(outputs_dir)
    with _index_cache_lock:
        idx = _index_cache.pop(key, None)
        if idx is None:
            idx = OutputsFtsIndex(outputs_dir)
        _index_cache[key] = idx  # (re-)insert at the MRU end
        while len(_index_cache) > _MAX_CACHED_INDEXES:
            _, evicted = _index_cache.popitem(last=False)
            evicted.close()
        return idx


def search_outputs(
    outputs_dir: str,
    query: str,
    *,
    limit: int = 10,
    include_archival: bool = True,
    max_seconds: float | None = DEFAULT_REBUILD_BUDGET_SECONDS,
) -> dict[str, Any]:
    """BM25 search over an outputs tree (backs the ``search_outputs`` MCP tool).

    Reuses a cached, persistent :class:`OutputsFtsIndex` per ``outputs_dir``
    (5116078b) so repeat calls only re-hash/re-parse files that changed since
    the last call — a call on an unchanged real Outputs directory no longer
    re-walks/re-hashes/re-rebuilds the whole tree. Archival rows are
    DEPRIORITIZED (halved score), never hard-excluded, unless
    ``include_archival=False``. A missing directory / empty tree returns an
    empty result rather than an error. Each hit carries path, score, the cheap
    fingerprint (csv_columns / json_keys / generating_script), the archival flag
    and its canonical twin. ``result["partial"]`` is set when ``max_seconds``
    was exceeded mid-rebuild — the returned hits reflect whatever was indexed
    so far, not necessarily every file on disk right now.

    *** A ZERO-HIT RESULT IS NOT PROOF THE FILE/TERM DOESN'T EXIST *** (5b897ad3),
    exactly as for ``extensions/meridian-outputs``'s own ``search_outputs`` — on
    a large/cold tree, a single call may not finish rebuilding within
    ``max_seconds``. When ``hits`` is empty AND the index is not known-complete
    (``partial`` and/or ``db_write_error`` set), this also sets
    ``zero_hits_warning`` — a single, hard-to-miss field explaining that this
    specific zero-hit result must not be read as "not found"; re-invoke with
    the same ``outputs_dir``/``query`` to let indexing continue (``pending_count``
    reports how many files are still queued) rather than concluding the term
    doesn't exist. ``result["db_write_error"]`` (5b897ad3) surfaces a DB-write
    failure that would otherwise be silently swallowed — a real persistence
    failure that never made it into the index must not look identical to a
    healthy, converged one.
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
    index = _get_cached_index(outputs_dir)
    result["total_indexed"] = index.rebuild(max_seconds=max_seconds)
    hits = index.search(query, limit=limit, include_archival=include_archival)
    # 9e02e448 — auto-surface annotations: each hit gets any annotation keyed
    # to its own path OR any ancestor directory, so the caller sees relevant
    # context without a second tool call (mirrors claim_file's code_notes).
    for hit in hits:
        hit["annotations"] = index.get_annotations_for_path(hit["path"])
    result["hits"] = hits
    if index.last_rebuild_partial:
        result["partial"] = True
    if index.last_pending_count:
        # 5b897ad3 — only meaningful alongside partial=True (mirrors
        # extensions/meridian-outputs's pending_stale_count precedent): a
        # fully-converged rebuild always leaves this at 0, so it's omitted
        # there to keep the existing full-coverage response shape unchanged.
        result["pending_count"] = index.last_pending_count
    if index.last_db_write_error:
        # 5b897ad3 — a real Phase-2 persistence failure this call. Rows may
        # still be visible in total_indexed (in-memory row_cache, populated
        # BEFORE the write is attempted), but they were NOT confirmed written
        # to disk.
        result["db_write_error"] = index.last_db_write_error
    if not hits and (result.get("partial") or result.get("db_write_error")):
        # 5b897ad3 — <surface-it-loudly>, mirroring the
        # extensions/meridian-outputs package's zero_hits_warning precedent:
        # make an incomplete/unpersisted index impossible to mistake for a
        # confirmed "not found" on a large/cold tree.
        result["zero_hits_warning"] = (
            "search_outputs returned 0 hits, but indexing of outputs_dir has "
            "NOT finished (partial=True and/or db_write_error is set on this "
            "same response) -- this is NOT a reliable 'file does not exist' "
            "signal. Re-invoke search_outputs on the same outputs_dir and "
            "query to let indexing continue (see pending_count for how much "
            "work remains queued), and only treat a miss as confirmed once a "
            "response comes back with no partial/db_write_error fields at all."
        )
        _log.warning(
            "search_outputs: zero hits for query=%r under outputs_dir=%r "
            "while the index is incomplete (partial=%s, pending_count=%s, "
            "db_write_error=%s) -- this is NOT a confirmed miss; caller "
            "should re-invoke rather than conclude the file/term doesn't exist",
            query, outputs_dir, result.get("partial"), result.get("pending_count"),
            result.get("db_write_error"),
        )
    return result


def annotate_outputs(
    outputs_dir: str,
    path: str,
    note: str,
    *,
    run_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture a human annotation for ``path`` in the outputs tree (9e02e448).

    Upserts a row into the ``annotations`` table of the cached
    :class:`OutputsFtsIndex` for ``outputs_dir``. ``path`` may be:

    * The ``outputs_dir`` root itself (Tier 1 — "what this tree is about").
    * Any sub-path (file or directory) within the tree (Tier 2 — per-run /
      per-file context such as "PCA on, BFS off, overwritten 5x").

    ``run_params`` is an optional free-form dict of parameters logged alongside
    the note (e.g. ``{"lr": 0.001, "batch_size": 32}``).

    Returns the stored annotation as a dict, or ``{"error": ...}`` on failure.
    """
    if not outputs_dir or not str(outputs_dir).strip():
        return {"error": "outputs_dir is required"}
    if not path or not str(path).strip():
        return {"error": "path is required"}
    if not note or not str(note).strip():
        return {"error": "note is required"}
    index = _get_cached_index(outputs_dir)
    return index.add_annotation(path, note, run_params=run_params, source="tool")


def resolve_figure_output(
    outputs_dir: str, file_path: str,
) -> dict[str, Any] | None:
    """Stateless cross-store resolve-through (d2a3537a).

    Reuses the same cached, incremental index as :func:`search_outputs`
    (5116078b) and returns the ``outputs_index`` row whose path IS
    ``file_path`` (path-normalized equality, not a relevance search) — the
    module-level counterpart of :meth:`OutputsFtsIndex.resolve_output` for
    callers that don't hold a live index. Returns ``None`` for a blank path, a
    missing outputs directory, or a figure that names no indexed output. Never
    raises.
    """
    if not file_path or not str(file_path).strip():
        return None
    if not os.path.isdir(outputs_dir):
        return None
    index = _get_cached_index(outputs_dir)
    index.rebuild()
    return index.resolve_output(file_path)


def resolve_output_with_fallback(
    outputs_dir: str, file_path: str, *, fuzzy_limit: int = 25,
) -> dict[str, Any] | None:
    """Two-tier resolve: exact path, then basename fallback (6b657a8b).

    :func:`resolve_figure_output` is EXACT-PATH-ONLY: if a figure's embedded
    ``file_path`` isn't recorded byte-for-byte at that path in the outputs
    index (the file was relocated, copied into a docs/media folder, or the
    run that produced it was re-executed elsewhere), it returns ``None`` with
    no further signal. This wraps it with a second tier — mirroring
    ``extensions/meridian-outputs/meridian_outputs/provenance.py``'s
    relocation-tolerant design for this package's own cached
    :class:`OutputsFtsIndex` — so a relocated/renamed figure can still be
    resolved, while making the reduced confidence explicit rather than
    silent.

    Returns ``None`` only when NEITHER tier finds anything. Otherwise the
    resolved row (path, generating_script, is_archival, canonical_path,
    sha256, kind, size, mtime, csv_columns, json_keys) plus:

      - ``match_type``: ``"exact"`` or ``"basename"``.
      - ``queried_path``: the ``file_path`` that was looked up (for audit).
      - ``candidate_count``: (basename tier only) how many same-basename
        files were found — more than 1 means the match is AMBIGUOUS and
        must be treated as a best guess, never authoritative.
    """
    if not file_path or not str(file_path).strip():
        return None
    if not os.path.isdir(outputs_dir):
        return None

    exact = resolve_figure_output(outputs_dir, file_path)
    if exact is not None:
        return {**exact, "match_type": "exact", "queried_path": file_path}

    target_base = os.path.normcase(
        os.path.basename(str(file_path).replace("\\", "/").rstrip("/"))
    )
    if not target_base:
        return None
    query = os.path.basename(str(file_path).replace("\\", "/").rstrip("/"))
    if not query:
        return None
    result = search_outputs(outputs_dir, query, limit=max(int(fuzzy_limit), 1))
    hits = result.get("hits") or []
    candidates = [
        h for h in hits
        if os.path.normcase(os.path.basename(str(h.get("path") or "").replace("\\", "/"))) == target_base
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda h: (h.get("score") or 0.0), reverse=True)
    best = dict(candidates[0])
    best.pop("score", None)
    best.pop("bm25", None)
    best.pop("annotations", None)
    best["match_type"] = "basename"
    best["queried_path"] = file_path
    best["candidate_count"] = len(candidates)
    return best


def infer_generating_script_hint(text: str) -> str | None:
    """Public wrapper for pulling a 'generated by <script>.py' hint out of free
    text (6b657a8b). A table caption carries no ``file_path`` the way a figure
    does (``doc_tables`` has no path column) — this is the fallback signal
    used to trace a table's provenance from its caption text alone. Returns
    ``None`` when the text is blank or carries no recognizable hint.
    """
    if not text or not str(text).strip():
        return None
    return _infer_generating_script_from_text(str(text))


def find_outputs_by_source(
    outputs_dir: str,
    source_path: str,
    *,
    limit: int = 25,
    max_seconds: float | None = DEFAULT_REBUILD_BUDGET_SECONDS,
) -> dict[str, Any]:
    """Stateless reverse resolve-through (2ae25966): script/data ``source_path``
    -> the outputs it produced.

    Reuses the same cached, incremental index as :func:`search_outputs` /
    :func:`resolve_figure_output` (5116078b) — the module-level counterpart of
    :meth:`OutputsFtsIndex.find_by_source` for callers that don't hold a live
    index. This is the direction :func:`resolve_figure_output` can never
    answer, because that always starts from the OUTPUT side: given the
    generating script or data file, this scans the outputs index for rows
    whose recorded ``generating_script`` traces back to it (exact-string or
    basename match) — i.e. "what did this thing produce?". That is the
    direction needed to catch a docx figure quietly citing STALE data: walk
    the source's outputs forward, newest first, and compare against what the
    docx actually shows.

    Returns ``{outputs_dir, source_path, outputs: [...], total}`` where each
    output row carries the same fields as
    :meth:`OutputsFtsIndex.resolve_output` (path, generating_script,
    is_archival, canonical_path, sha256, kind, size, mtime, csv_columns,
    json_keys), sorted newest-first by ``mtime``. ``total`` is the full match
    count before ``limit`` truncation. ``outputs`` is empty (not an error)
    when nothing in the tree cites this source, ``source_path`` is blank, or
    ``outputs_dir`` doesn't exist. Never raises.
    """
    empty: dict[str, Any] = {
        "outputs_dir": outputs_dir,
        "source_path": source_path,
        "outputs": [],
        "total": 0,
    }
    if not source_path or not str(source_path).strip():
        return empty
    if not os.path.isdir(outputs_dir):
        return empty
    index = _get_cached_index(outputs_dir)
    index.rebuild(max_seconds=max_seconds)
    matches = index.find_by_source(source_path)
    lim = max(int(limit), 1) if limit else 25
    return {
        "outputs_dir": outputs_dir,
        "source_path": source_path,
        "outputs": matches[:lim],
        "total": len(matches),
    }


def check_promotion_source_freshness(
    outputs_dir: str, source_output_path: str, *, expected_sha256: "str | None" = None,
) -> dict[str, Any]:
    """24f5146d — provenance-closure check for the SOURCE half of a
    script->artifact->document promotion: does the script-run output about
    to be promoted into a docx still match an EXPECTED content hash a caller
    captured earlier (e.g. when the promotion's declaration/resource
    footprint was first authored, or from a prior
    ``meridian-outputs`` provenance record)?

    Reuses :func:`resolve_output_with_fallback` (never reimplemented) to
    find ``source_output_path`` in the outputs index — exact path first,
    then a relocation-tolerant basename fallback — purely to resolve the
    real on-disk path and confirm the output is genuinely indexed at all.
    The actual freshness comparison is against ``expected_sha256`` — NOT
    against the index's own recorded ``sha256`` — because
    :func:`resolve_output_with_fallback` triggers an INCREMENTAL rebuild on
    every call (5116078b), which re-hashes any changed file before this
    function ever reads it; comparing the index's own hash against a
    freshly-computed one taken microseconds later can never observe a
    change; it would always read "fresh," which is not a genuine check.
    ``expected_sha256`` breaks that self-healing loop by supplying a
    baseline from OUTSIDE this call.

    This closes the "script-run -> artifact" half of provenance; the
    "artifact -> document" half (has the DOCX target itself changed since
    the promotion was declared) is
    :func:`meridian.artifact_declaration.check_promotion_preconditions` —
    the two are deliberately separate, composable checks rather than one
    function conflating two different staleness questions.

    Returns ``{"resolved": bool, "match_type": str | None, "fresh": bool | None,
    "expected_sha256": str | None, "current_sha256": str | None, "reason": str}``.
    ``fresh`` is ``None`` — "cannot confirm," never silently treated as
    fresh or stale — when the source could not be resolved in the index at
    all, when ``expected_sha256`` was not supplied (nothing to compare
    against), or when the current content could not be hashed. Never
    raises.
    """
    resolved = resolve_output_with_fallback(outputs_dir, source_output_path)
    if resolved is None:
        return {
            "resolved": False,
            "match_type": None,
            "fresh": None,
            "expected_sha256": expected_sha256,
            "current_sha256": None,
            "reason": (
                "source output not found in the outputs index — provenance "
                "freshness cannot be verified"
            ),
        }
    if resolved.get("match_type") != "exact":
        return {
            "resolved": False,
            "match_type": resolved.get("match_type"),
            "fresh": None,
            "expected_sha256": expected_sha256,
            "current_sha256": None,
            "reason": (
                "source output was found only by basename/label fallback -- "
                "that diagnostic match cannot establish exact provenance "
                "for a promotion"
            ),
        }
    current_sha256 = _sha256_file(resolved.get("path") or source_output_path)
    if expected_sha256 is None:
        return {
            "resolved": True,
            "match_type": resolved.get("match_type"),
            "fresh": None,
            "expected_sha256": None,
            "current_sha256": current_sha256,
            "reason": (
                "no expected_sha256 supplied — resolved the source but have "
                "nothing to compare its current content against"
            ),
        }
    if current_sha256 is None:
        return {
            "resolved": True,
            "match_type": resolved.get("match_type"),
            "fresh": None,
            "expected_sha256": expected_sha256,
            "current_sha256": None,
            "reason": "current content could not be hashed — freshness cannot be confirmed",
        }
    fresh = current_sha256 == expected_sha256
    return {
        "resolved": True,
        "match_type": resolved.get("match_type"),
        "fresh": fresh,
        "expected_sha256": expected_sha256,
        "current_sha256": current_sha256,
        "reason": (
            "output content matches the expected hash"
            if fresh
            else (
                "output content no longer matches the expected hash — the "
                "artifact this promotion would embed may be stale"
            )
        ),
    }


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
        # incrementally. The watcher knows the EXACT path that changed, so it
        # force-invalidates that path (5116078b — exact, not mtime-heuristic)
        # then triggers an incremental rebuild: only the changed file is
        # re-hashed/re-parsed, and the table + FTS index are rewritten only
        # because that one row changed.
        if self.fts is not None:
            self.fts.invalidate(src_path)
            self.fts.rebuild(max_seconds=None)

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
