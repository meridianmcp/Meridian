"""Bounded, ephemeral-per-call CSV/JSON/XLSX *tabular* shape inspection via
DuckDB (item 28ef2710 -- Wave 1 of the "Bounded file-shape inspector"
contract in ``docs/meridian-storage-and-file-inspector-contract-2026-08-31.md``,
extending item 2ffd763d's raw XML/JSON structural inspector in
:mod:`meridian_file_inspection.inspector`).

Scope: schema (column name/type), a bounded row sample, and a row count
(exact when cheap, explicitly flagged as a lower bound / unknown otherwise)
for ONE local CSV, JSON, or XLSX file -- never a second parser/index/
database, never a directory walk, never a write.

Why this is a SEPARATE module/tool from :func:`inspector.inspect_file`
rather than a new branch of it
--------------------------------------------------------------------------
DuckDB's ``read_json`` treats a bare JSON object as ONE ROW (each top-level
key becomes a COLUMN) -- a genuinely different, but equally valid,
interpretation of the same bytes as ``inspector.inspect_file``'s generic
structural summary (which treats a bare object as "an object with N keys").
Both views are useful for different callers; this module adds the tabular
one as a distinct tool (``inspect_tabular_file``) rather than overloading or
replacing the Wave-0 contract's JSON behavior, which already has passing
tests and callers depending on its exact shape.

Why DuckDB (per the design doc's "Engine choices and reuse")
--------------------------------------------------------------------------
``duckdb`` is already a direct dependency of the root ``meridian`` pixi
environment (``duckdb>=1.0`` in the root ``pyproject.toml``/``pixi.toml``,
resolved to 1.5.4 in ``pixi.lock``) and already powers the unified
whole-tree CSV/JSON reads in ``meridian/outputs_indexer.py``. Reusing it
here for a single-file bounded read adds zero new dependency surface to the
project as a whole -- the same rationale ``xml_safe.py`` documents for
reusing ``lxml`` instead of adding ``defusedxml``. This standalone
``meridian-file-inspection`` package itself, however, only ever declared
``mcp``/``lxml`` before this item -- so ``duckdb>=1.0`` IS a new, explicit
addition to *this package's own* ``pyproject.toml`` (see that file's
comment), even though it adds nothing to the overall project or to
``pixi.lock``.

DuckDB's native ``.xlsx`` support is NOT zero-dependency the way
``read_csv``/``read_json`` are: it lives in a separate ``excel`` *core
extension* that DuckDB will silently fetch over the network on first
``LOAD``/``INSTALL`` unless it is already cached locally
(``~/.duckdb/extensions/...``). That conflicts with this whole contract's
"no network access" bound, so :func:`_ensure_excel_extension` below refuses
(``denied``/``xlsx_extension_unavailable``) rather than triggering an
implicit fetch, UNLESS the caller explicitly opts in via
``allow_extension_network_install=True`` -- a one-time, deliberate action,
never a side effect of an ordinary bounded inspection call.

No Polars/fastexcel/Calamine here
--------------------------------------------------------------------------
Per the design doc's Wave 1 scope: "add Calamine/fastexcel only if
compatibility results justify it" against a real xls/xlsb/ods benchmark
corpus. No such corpus/benchmark exists yet, and DuckDB's own ``.xlsx``
path (verified empirically while building this module -- see the
walkthrough in this item's completion notes) is sufficient for schema/
sample/row-count inspection of well-formed ``.xlsx`` files. Nothing in this
module imports or depends on Polars/fastexcel/Calamine.

Security posture (mirrors :mod:`meridian_file_inspection.inspector`)
--------------------------------------------------------------------------
  - Same path-policy preflight (:func:`inspector._resolve_path_policy`) --
    secret-path exclusion, symlink policy, ``allowed_root`` containment,
    exists/is-file/readable -- ALL before opening the file, reused directly
    rather than re-implemented.
  - ``max_bytes`` is checked via ``os.stat`` before ANY content is read or
    handed to DuckDB.
  - XLSX (a ZIP container) gets an ADDITIONAL preflight:
    :func:`_check_zip_bomb` sums each archive member's DECLARED uncompressed
    size straight from the ZIP central directory (``zipfile.ZipFile.infolist``
    never decompresses anything to do this -- it only reads directory
    entries) and refuses before DuckDB's excel extension ever touches the
    file if the total would exceed ``max_decompressed_bytes``. This is the
    concrete defense against a small-on-disk, huge-when-inflated XLSX/ZIP
    the design doc's "ZIP-backed formats must also enforce archive member
    and expansion limits" requirement calls for.
  - Every DuckDB query this module issues runs on a fresh, ephemeral
    ``duckdb.connect(":memory:")`` connection -- never a persistent on-disk
    index -- with an explicit ``PRAGMA memory_limit`` (see
    :func:`_resolve_duckdb_memory_limit_bytes`, adapted from
    ``meridian_outputs.outputs_local``'s env-var/psutil-availability
    pattern but sized for ONE small bounded call rather than a persistent
    multi-GB FTS index) and ``PRAGMA threads=1`` (see :func:`_build_table_expr`
    docstring for why single-threaded execution is also a CORRECTNESS
    requirement here, not just a resource bound).
  - Every query runs through :func:`_run_bounded`, which cancels a
    still-running query via DuckDB's documented cross-thread
    ``Connection.interrupt()`` if it has not finished within the remaining
    ``timeout_seconds`` budget -- verified empirically (see this item's
    completion notes) to reliably raise ``duckdb.InterruptException`` in the
    executing thread within milliseconds.
  - The ONLY SQL this module ever constructs is ``SELECT``/``DESCRIBE`` over
    ``read_csv``/``read_json``/``read_xlsx`` -- never ``COPY``/``INSERT``/
    ``CREATE`` -- so even though the ``excel`` extension also supports
    *writing* ``.xlsx``, that surface is never reachable from here. The file
    path itself is always passed as a bound ``?`` parameter, never
    string-interpolated into the query text.
  - Never raises across the ``inspect_tabular_file`` boundary -- every
    failure mode (oversized/zip-bomb-shaped/malformed input, path-policy
    violation, missing excel extension, timeout, OOM) is reported via the
    same stable envelope/error-code contract as
    :mod:`meridian_file_inspection.inspector`.
"""
from __future__ import annotations

import os
import threading
import time
import zipfile
from typing import Any

import duckdb

from .inspector import (
    STATE_COMPLETE,
    STATE_FAILED,
    STATE_PARTIAL,
    _canonical_json,
    _err,
    _json_structure_scan,
    _redact_source_ref,
    _resolve_path_policy,
    _sha256_bytes,
)

SCHEMA_VERSION = "1.0.0"

DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB -- same default as inspector.py
#: Zip-bomb guard for XLSX: total DECLARED uncompressed size (summed from the
#: ZIP central directory, never actually inflated to check this) allowed
#: before refusing. 20x the default max_bytes -- generous for a legitimate
#: small/medium spreadsheet (XLSX's XML-based cell format compresses well,
#: so a real workbook's inflated size is routinely a multiple of its
#: on-disk size) while still catching a genuine bomb (a real bomb's ratio is
#: orders of magnitude higher than this).
DEFAULT_MAX_DECOMPRESSED_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_DEPTH = 100  # JSON prescan only; ignored for csv/xlsx
DEFAULT_MAX_ITEMS = 50_000  # JSON prescan only; ignored for csv/xlsx
#: Cap on distinct columns reported in the schema summary -- bounds response
#: size for a pathologically wide file (e.g. a small-in-bytes CSV whose
#: single header line has millions of tiny fields). Mirrors
#: xml_safe.py's _MAX_DISTINCT_NAMES pattern. Does NOT bound what DuckDB
#: itself computes (that's timeout_seconds/memory_limit's job) -- only what
#: is included in the returned shape.
DEFAULT_MAX_COLUMNS = 500
DEFAULT_MAX_SAMPLE_ROWS = 100
DEFAULT_PREVIEW_CHARS = 2000
DEFAULT_TIMEOUT_SECONDS = 5.0

_SUPPORTED_TABULAR_FORMATS = frozenset({"csv", "json", "xlsx"})
_MIME = {
    "csv": "text/csv",
    "json": "application/json",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_PARSER_ID = {"csv": "duckdb-csv", "json": "duckdb-json", "xlsx": "duckdb-excel"}

_ZIP_MAGIC = b"PK\x03\x04"
#: CSV has no magic-byte signature at all (unlike xml/json/xlsx) -- this is
#: the necessary, explicitly-documented adaptation of "extension plus magic/
#: signature check, never extension alone" for a format that fundamentally
#: has no magic number: a content-shape heuristic (decodable text, no NUL
#: bytes, a recognized delimiter present in the first non-empty line).
_TEXT_DELIMITERS = (",", "\t", ";", "|")

# ---------------------------------------------------------------------------
# DuckDB memory_limit resolution -- adapted from
# meridian_outputs.outputs_local's _default_duckdb_memory_limit_bytes /
# _resolve_duckdb_memory_limit_bytes (env-var-in-MB override -> psutil
# available-memory fraction -> floor/ceiling clamp -> fixed fallback if
# psutil is missing), but sized for ONE ephemeral, per-call, bounded-input
# (<= max_bytes, default 10 MiB) connection rather than a persistent,
# multi-GB, whole-corpus FTS index -- hence much smaller floor/ceiling/share
# than outputs_local.py's 1.5GB/16GB/0.8 values, which were tuned for a very
# different, long-lived workload.
# ---------------------------------------------------------------------------
_DUCKDB_MEMORY_LIMIT_ENV_VAR = "MERIDIAN_INSPECTOR_DUCKDB_MEMORY_LIMIT_MB"
_DEFAULT_DUCKDB_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
_DUCKDB_MEMORY_RESERVE_BYTES = 512 * 1024 * 1024
_DUCKDB_MEMORY_LIMIT_SHARE = 0.25
_DUCKDB_MEMORY_LIMIT_FLOOR_BYTES = 128 * 1024 * 1024
_DUCKDB_MEMORY_LIMIT_CEILING_BYTES = 2 * 1024 * 1024 * 1024


def _default_duckdb_memory_limit_bytes() -> int:
    raw = os.environ.get(_DUCKDB_MEMORY_LIMIT_ENV_VAR)
    if raw is not None and raw.strip():
        try:
            env_bytes = int(raw.strip()) * 1024 * 1024
        except ValueError:
            env_bytes = None
        if env_bytes is not None:
            return max(
                _DUCKDB_MEMORY_LIMIT_FLOOR_BYTES,
                min(env_bytes, _DUCKDB_MEMORY_LIMIT_CEILING_BYTES),
            )
    try:
        import psutil  # noqa: PLC0415

        available = int(psutil.virtual_memory().available)
    except (ImportError, OSError, AttributeError):
        return _DEFAULT_DUCKDB_MEMORY_LIMIT_BYTES
    usable = available - _DUCKDB_MEMORY_RESERVE_BYTES
    limit = int(usable * _DUCKDB_MEMORY_LIMIT_SHARE)
    return max(_DUCKDB_MEMORY_LIMIT_FLOOR_BYTES, min(limit, _DUCKDB_MEMORY_LIMIT_CEILING_BYTES))


def _resolve_duckdb_memory_limit_bytes(explicit: int | None) -> int:
    if explicit is not None:
        if explicit >= _DUCKDB_MEMORY_LIMIT_FLOOR_BYTES:
            return min(explicit, _DUCKDB_MEMORY_LIMIT_CEILING_BYTES)
        return _DUCKDB_MEMORY_LIMIT_FLOOR_BYTES
    return _default_duckdb_memory_limit_bytes()


# ---------------------------------------------------------------------------
# Format sniffing
# ---------------------------------------------------------------------------


def sniff_tabular_format(head: bytes, declared: str) -> str | None:
    """Sniff csv/json/xlsx from content, never from extension alone -- with
    the documented CSV exception (see module-level ``_TEXT_DELIMITERS``
    comment: CSV has no magic-byte signature to sniff)."""
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped[:4] == _ZIP_MAGIC:
        return "xlsx"
    first = stripped[:1]
    if first in (b"{", b"["):
        return "json"
    if b"\x00" in head:
        return None
    try:
        text_head = stripped.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return None
    first_line = text_head.splitlines()[0] if text_head.splitlines() else ""
    if any(d in first_line for d in _TEXT_DELIMITERS):
        return "csv"
    if declared == "csv" and first_line:
        # No recognized delimiter found (e.g. a genuine single-column CSV)
        # -- honour an explicit, non-"auto" caller declaration as long as
        # the content is at least plausible text, matching inspector.py's
        # "an explicit non-auto value is honoured only when the magic bytes
        # are at least plausible for it" rule.
        return "csv"
    return None


# ---------------------------------------------------------------------------
# XLSX zip-bomb preflight
# ---------------------------------------------------------------------------


def _check_zip_bomb(path: str, *, max_decompressed_bytes: int) -> dict[str, Any] | None:
    """Sum declared uncompressed member sizes straight from the ZIP central
    directory -- never inflates anything to do this -- and refuse before
    DuckDB's excel extension ever opens the file if the total exceeds
    ``max_decompressed_bytes``. See module docstring."""
    try:
        if not zipfile.is_zipfile(path):
            return _err("malformed", "not_a_valid_zip_container")
        with zipfile.ZipFile(path) as zf:
            total_uncompressed = 0
            for info in zf.infolist():
                total_uncompressed += info.file_size
                if total_uncompressed > max_decompressed_bytes:
                    return _err(
                        "limit_exceeded",
                        "max_decompressed_bytes_exceeded",
                        f">= {total_uncompressed} > {max_decompressed_bytes}",
                    )
    except zipfile.BadZipFile as exc:
        return _err("malformed", "bad_zip_container", str(exc))
    except OSError as exc:
        return _err("denied", "unreadable", str(exc))
    return None


# ---------------------------------------------------------------------------
# Excel extension preflight -- never an implicit network fetch
# ---------------------------------------------------------------------------


def _excel_extension_installed(con: "duckdb.DuckDBPyConnection") -> bool:
    """True iff the 'excel' extension is already installed (cached on
    local disk) -- checked via duckdb_extensions(), which never itself
    triggers a network call. Never raises."""
    try:
        rows = con.execute(
            "SELECT installed FROM duckdb_extensions() WHERE extension_name = 'excel'"
        ).fetchall()
    except duckdb.Error:
        return False
    return bool(rows) and bool(rows[0][0])


def _ensure_excel_extension(
    con: "duckdb.DuckDBPyConnection", *, allow_network_install: bool
) -> dict[str, Any] | None:
    """Load the 'excel' extension if already cached locally. Refuses
    (denied/xlsx_extension_unavailable) rather than silently making a
    network call, UNLESS the caller explicitly opts in via
    ``allow_network_install=True``. See module docstring."""
    if _excel_extension_installed(con):
        try:
            con.execute("LOAD excel")
            return None
        except duckdb.Error as exc:
            return _err("denied", "xlsx_extension_load_failed", str(exc)[:300])
    if not allow_network_install:
        return _err(
            "denied",
            "xlsx_extension_unavailable",
            "the DuckDB 'excel' core extension is not installed locally; "
            "refusing to make an implicit network call to fetch it (this "
            "contract requires no network access by default). Pre-cache it "
            "once with `INSTALL excel` in this environment, or pass "
            "allow_extension_network_install=True to fetch it now.",
        )
    try:
        con.execute("INSTALL excel")
        con.execute("LOAD excel")
    except duckdb.Error as exc:
        return _err("denied", "xlsx_extension_install_failed", str(exc)[:300])
    return None


# ---------------------------------------------------------------------------
# Bounded query execution with cross-thread cancellation
# ---------------------------------------------------------------------------


def _run_bounded(
    con: "duckdb.DuckDBPyConnection", sql: str, params: list[Any], deadline: float
) -> tuple[list[tuple] | None, list[str] | None, dict[str, Any] | None]:
    """Run one query on ``con``, bounded by the absolute ``deadline``
    (a ``time.monotonic()`` timestamp). Returns
    ``(rows, column_names, error_or_None)``.

    Verified empirically (this item's investigation) that
    ``Connection.interrupt()`` called from a different thread than the one
    executing a query reliably raises ``duckdb.InterruptException`` in the
    EXECUTING thread within milliseconds -- this is DuckDB's own documented
    cross-thread cancellation mechanism, not a guess. A single
    ``DuckDBPyConnection`` is safe to use this way (execute on one thread,
    interrupt from another); it is NOT safe to run two concurrent
    ``execute`` calls on the same connection, which this function never
    does (always joins/abandons the previous worker before returning).
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, None, _err("timeout", "wall_clock_budget_exceeded")

    outcome: dict[str, Any] = {}

    def _worker() -> None:
        try:
            cur = con.execute(sql, params)
            outcome["rows"] = cur.fetchall()
            outcome["columns"] = [d[0] for d in cur.description] if cur.description else []
        except BaseException as exc:  # noqa: BLE001 -- must also capture InterruptException
            outcome["exc"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(remaining)
    if thread.is_alive():
        con.interrupt()
        # Bounded grace period for the interrupt to actually land and the
        # worker thread to unwind -- never joins unboundedly.
        thread.join(5.0)
        return None, None, _err("timeout", "wall_clock_budget_exceeded")

    exc = outcome.get("exc")
    if exc is not None:
        if isinstance(exc, duckdb.InterruptException):
            return None, None, _err("timeout", "wall_clock_budget_exceeded")
        if isinstance(exc, MemoryError) or isinstance(exc, getattr(duckdb, "OutOfMemoryException", ())):
            return None, None, _err("limit_exceeded", "memory_limit_exceeded", str(exc)[:300])
        if isinstance(exc, duckdb.Error):
            return None, None, _err("malformed", "duckdb_query_error", str(exc)[:300])
        raise exc
    return outcome.get("rows"), outcome.get("columns"), None


# ---------------------------------------------------------------------------
# Table expression construction -- read-only by construction
# ---------------------------------------------------------------------------


def _build_table_expr(resolved_format: str) -> str:
    """Return the ``read_*`` table-function call for ``resolved_format``,
    with the file path always left as a ``?`` bind parameter (never
    string-interpolated). The only SQL this module ever issues wraps this
    expression in ``SELECT``/``DESCRIBE`` -- never ``COPY``/``INSERT``/
    ``CREATE`` -- so the excel extension's write surface is never reachable
    from here even though it exists.

    ``null_padding=true`` is used for CSV (rather than a stricter/fixed
    schema) deliberately: DuckDB's CSV dialect sniffer is already lenient
    about ragged real-world CSVs (verified empirically -- see this item's
    investigation notes), and this inspector's job is to REPORT a file's
    actual shape, not to reject every real-world CSV that has occasional
    short rows. Genuine malformed/undecodable content is still caught by
    this module's own UTF-8 validation pass (:func:`_validate_utf8`) before
    DuckDB ever sees the file, and by DuckDB's own error surface for
    content it truly cannot parse at all.
    """
    if resolved_format == "csv":
        return "read_csv(?, auto_detect=true, null_padding=true, ignore_errors=false)"
    if resolved_format == "json":
        return "read_json(?, auto_detect=true)"
    if resolved_format == "xlsx":
        return "read_xlsx(?)"
    raise ValueError(f"unsupported tabular format {resolved_format!r}")  # pragma: no cover -- guarded by caller


# ---------------------------------------------------------------------------
# CSV/JSON text validation (mirrors inspector.py's own pre-parse checks)
# ---------------------------------------------------------------------------


def _validate_utf8(path: str, max_bytes: int) -> dict[str, Any] | None:
    """Read the whole (already byte-bounded, <= max_bytes) file and verify
    it decodes as UTF-8. Catches truncated/invalid encoding -- a very real
    "file cut off mid-write" scenario -- deterministically, BEFORE handing
    the file to DuckDB (whose CSV/JSON readers are lenient about structural
    raggedness but still assume valid text encoding)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError as exc:
        return _err("denied", "unreadable", str(exc))
    if len(data) > max_bytes:
        return _err("limit_exceeded", "max_bytes_exceeded", f">= {len(data)} > {max_bytes}")
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return _err("malformed", "invalid_utf8", str(exc))
    return None


def _json_prescan(path: str, max_bytes: int, *, max_depth: int, max_items: int) -> dict[str, Any] | None:
    """Reuse inspector._json_structure_scan's bounded, string/escape-aware
    bracket scan to bound nesting depth/container count BEFORE DuckDB's own
    JSON parser ever sees the text -- defense in depth against a
    pathologically deep (but tiny) document, exactly the concern that
    prescan already exists to address for the Wave-0 stdlib-based JSON
    path. Ignored for csv/xlsx (no nesting concept applies)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError as exc:
        return _err("denied", "unreadable", str(exc))
    if len(data) > max_bytes:
        return _err("limit_exceeded", "max_bytes_exceeded", f">= {len(data)} > {max_bytes}")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return _err("malformed", "invalid_utf8", str(exc))
    return _json_structure_scan(text, max_depth=max_depth, max_items=max_items)


def _stringify_cell(value: Any, preview_chars: int) -> Any:
    """Bound an individual cell's rendered size -- never returns a raw huge
    blob for one cell. Non-string scalars pass through as-is (so JSON
    round-trips a real int/float/bool/None instead of always stringifying);
    only strings (and anything else JSON can't natively encode) are capped
    by length."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else str(value)
    if len(text) > preview_chars:
        return text[:preview_chars]
    return text


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def inspect_tabular_file(
    path: str,
    format: str = "auto",
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_columns: int = DEFAULT_MAX_COLUMNS,
    max_sample_rows: int = DEFAULT_MAX_SAMPLE_ROWS,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    allowed_root: str | None = None,
    allow_symlinks: bool = False,
    allow_extension_network_install: bool = False,
    duckdb_memory_limit_bytes: int | None = None,
) -> dict[str, Any]:
    """Inspect exactly ONE local CSV, JSON, or XLSX file's TABULAR shape
    (schema/sample/row-count) through DuckDB and return a bounded,
    deterministic summary. Never raises -- every failure mode is reported
    in the returned envelope's ``errors``/``state`` fields.

    See the module docstring and
    ``docs/meridian-storage-and-file-inspector-contract-2026-08-31.md`` for
    the full contract this implements (Wave 1).
    """
    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_ref": _redact_source_ref(path),
        "format": None,
        "mime": None,
        "size_bytes": None,
        "source_sha256": None,
        "parser_id": None,
        "parser_version": None,
        "result_hash": None,
        "state": STATE_FAILED,
        "shape": {},
        "bounds": {
            "max_bytes": max_bytes,
            "max_decompressed_bytes": max_decompressed_bytes,
            "max_depth": max_depth,
            "max_items": max_items,
            "max_columns": max_columns,
            "max_sample_rows": max_sample_rows,
            "preview_chars": preview_chars,
            "timeout_seconds": timeout_seconds,
        },
        "warnings": [],
        "errors": [],
        "provenance_ref": None,
    }

    policy_error = _resolve_path_policy(path, allowed_root=allowed_root, allow_symlinks=allow_symlinks)
    if policy_error is not None:
        envelope["errors"].append(policy_error)
        return envelope

    try:
        size_bytes = os.path.getsize(path)
    except OSError as exc:
        envelope["errors"].append(_err("denied", "unreadable", str(exc)))
        return envelope
    envelope["size_bytes"] = size_bytes
    if size_bytes > max_bytes:
        envelope["errors"].append(_err("limit_exceeded", "max_bytes_exceeded", f"{size_bytes} > {max_bytes}"))
        return envelope

    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError as exc:
        envelope["errors"].append(_err("denied", "unreadable", str(exc)))
        return envelope

    declared = format if format in _SUPPORTED_TABULAR_FORMATS else "auto"
    resolved_format = sniff_tabular_format(head, declared)
    if resolved_format is None:
        envelope["errors"].append(_err("unsupported", "format_not_recognized"))
        return envelope

    envelope["format"] = resolved_format
    envelope["mime"] = _MIME[resolved_format]

    try:
        envelope["source_sha256"] = _sha256_file(path, max_bytes)
    except OSError as exc:
        envelope["errors"].append(_err("denied", "unreadable", str(exc)))
        return envelope

    if resolved_format == "xlsx":
        zip_error = _check_zip_bomb(path, max_decompressed_bytes=max_decompressed_bytes)
        if zip_error is not None:
            envelope["errors"].append(zip_error)
            return envelope
    elif resolved_format == "csv":
        text_error = _validate_utf8(path, max_bytes)
        if text_error is not None:
            envelope["errors"].append(text_error)
            return envelope
    elif resolved_format == "json":
        prescan_error = _json_prescan(path, max_bytes, max_depth=max_depth, max_items=max_items)
        if prescan_error is not None:
            envelope["errors"].append(prescan_error)
            return envelope

    envelope["parser_id"] = _PARSER_ID[resolved_format]
    envelope["parser_version"] = f"duckdb-{duckdb.__version__}"

    memory_limit_bytes = _resolve_duckdb_memory_limit_bytes(duckdb_memory_limit_bytes)
    con = duckdb.connect(":memory:")
    try:
        con.execute("PRAGMA threads=1")
        limit_mb = max(1, memory_limit_bytes // (1024 * 1024))
        con.execute(f"PRAGMA memory_limit='{limit_mb}MB'")

        if resolved_format == "xlsx":
            ext_error = _ensure_excel_extension(con, allow_network_install=allow_extension_network_install)
            if ext_error is not None:
                envelope["errors"].append(ext_error)
                return envelope

        table_expr = _build_table_expr(resolved_format)
        deadline = time.monotonic() + timeout_seconds

        columns_rows, _cols, describe_error = _run_bounded(
            con, f"DESCRIBE SELECT * FROM {table_expr}", [path], deadline
        )
        if describe_error is not None:
            envelope["errors"].append(describe_error)
            return envelope

        column_defs = [{"name": r[0], "type": r[1]} for r in (columns_rows or [])]
        total_columns = len(column_defs)
        shown_columns = column_defs[:max_columns]

        count_rows, _cols2, count_error = _run_bounded(
            con, f"SELECT count(*) FROM {table_expr}", [path], deadline
        )
        state = STATE_COMPLETE
        warnings: list[dict[str, Any]] = []
        if count_error is not None:
            row_count: dict[str, Any] = {"value": None, "exact": False}
            warnings.append(count_error)
            state = STATE_PARTIAL
        else:
            row_count = {"value": count_rows[0][0], "exact": True}

        sample_rows_raw, sample_columns, sample_error = _run_bounded(
            con,
            f"SELECT * FROM {table_expr} LIMIT {int(max_sample_rows)}",
            [path],
            deadline,
        )
        if sample_error is not None:
            warnings.append(sample_error)
            state = STATE_PARTIAL
            sample = []
            truncated_sample = False
        else:
            cols_for_sample = sample_columns or [c["name"] for c in column_defs]
            sample = [
                {name: _stringify_cell(value, preview_chars) for name, value in zip(cols_for_sample, row)}
                for row in (sample_rows_raw or [])
            ]
            if row_count["exact"]:
                truncated_sample = row_count["value"] > len(sample)
            else:
                truncated_sample = len(sample) >= max_sample_rows

        shape: dict[str, Any] = {
            "row_count": row_count,
            "column_count": total_columns,
            "columns": shown_columns,
            "truncated_columns": total_columns > len(shown_columns),
            "sample_rows": sample,
            "truncated_sample": truncated_sample,
        }

        envelope["shape"] = shape
        envelope["state"] = state
        envelope["warnings"].extend(warnings)
        envelope["result_hash"] = _sha256_bytes(_canonical_json(shape).encode("utf-8"))
        return envelope
    finally:
        con.close()


def _sha256_file(path: str, max_bytes: int) -> str:
    """Hash the file's bytes (bounded to max_bytes+1, matching the size
    check already performed by the caller) without depending on
    inspector.py's private helper reading a pre-loaded buffer -- this
    module reads from ``path`` directly since it never needs the full
    content in memory for anything else (unlike inspector.py's XML/JSON
    path, which parses the in-memory buffer it already hashed)."""
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read(max_bytes + 1))
    return h.hexdigest()
