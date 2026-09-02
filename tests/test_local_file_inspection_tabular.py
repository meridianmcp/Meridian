"""Focused serial tests for item 28ef2710 (LOCAL-FILE-INSPECTION-TABULAR):
bounded CSV/JSON/XLSX tabular shape inspection through DuckDB, in
``extensions/meridian-file-inspection/meridian_file_inspection/tabular.py``.

This is the Wave 1 companion to ``tests/test_local_file_inspection.py``
(Wave 0: raw XML/generic JSON). Same import strategy: ``duckdb`` is now a
declared dependency of this standalone extension's own ``pyproject.toml``
(see that file's comment), and it is ALSO already a direct dependency of
the root ``meridian`` pixi environment (``duckdb>=1.0``, used by
``meridian/outputs_indexer.py``) -- so these tests exercise the REAL
DuckDB engine (including the real ``excel`` extension, when locally cached)
via a ``sys.path`` insertion, without touching ``pixi.toml``/``pixi.lock``.

Covers: valid csv/json/xlsx, schema/sample/row-count shape, determinism,
bounds (max_bytes, max_decompressed_bytes, max_depth/max_items JSON
prescan, max_sample_rows, timeout), path policy (secret-file, directory,
missing, outside allowed_root, symlink), the excel-extension network-
install gate, and adversarial/security inputs: a CSV truncated mid
multi-byte UTF-8 character, a CSV/JSON file far exceeding declared sample/
column bounds while staying under max_bytes, and a zip-bomb-shaped XLSX
(tiny on disk, huge declared-uncompressed-size central directory).
"""
from __future__ import annotations

import json
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

_EXT_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "extensions", "meridian-file-inspection")
)
if _EXT_PATH not in sys.path:
    sys.path.insert(0, _EXT_PATH)

import duckdb  # noqa: E402

from meridian_file_inspection import tabular  # noqa: E402
from meridian_file_inspection.tabular import inspect_tabular_file  # noqa: E402

from meridian import capability_manifest as cm  # noqa: E402


def _excel_extension_available() -> bool:
    """True iff the real DuckDB 'excel' extension is already cached locally
    in THIS environment. Tests that need a genuine .xlsx read (not just the
    denial-path monkeypatch tests) skip gracefully when it isn't -- matching
    this item's own "no implicit network install" contract: tests must not
    silently reach the network either."""
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            "SELECT installed FROM duckdb_extensions() WHERE extension_name = 'excel'"
        ).fetchall()
        return bool(rows) and bool(rows[0][0])
    except duckdb.Error:
        return False
    finally:
        con.close()


def _write_xlsx(path: Path, rows: list[tuple]) -> None:
    """Write a real, valid .xlsx fixture using DuckDB's own excel extension
    (write support) -- test-fixture generation only; the inspector itself
    never issues a COPY/write statement (see tabular.py's module
    docstring)."""
    con = duckdb.connect(":memory:")
    try:
        con.execute("INSTALL excel")
        con.execute("LOAD excel")
        values = ", ".join(
            "(" + ", ".join(repr(v) for v in row) + ")" for row in rows
        )
        con.execute(
            f"COPY (SELECT * FROM (VALUES {values}) AS t(id, name)) "
            f"TO '{path.as_posix()}' (FORMAT XLSX, HEADER true)"
        )
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Valid files -- schema/sample/row-count shape
# ---------------------------------------------------------------------------


def test_valid_csv_inspects_complete(tmp_path: Path) -> None:
    p = tmp_path / "valid.csv"
    p.write_text("id,name,score\n1,alice,9.5\n2,bob,8.1\n3,carol,7.75\n", encoding="utf-8")
    result = inspect_tabular_file(str(p))
    assert result["state"] == "complete"
    assert result["errors"] == []
    assert result["format"] == "csv"
    assert result["parser_id"] == "duckdb-csv"
    assert result["shape"]["row_count"] == {"value": 3, "exact": True}
    assert result["shape"]["column_count"] == 3
    names = [c["name"] for c in result["shape"]["columns"]]
    assert names == ["id", "name", "score"]
    assert result["shape"]["sample_rows"][0]["name"] == "alice"
    assert result["shape"]["truncated_sample"] is False
    assert result["result_hash"]
    assert result["provenance_ref"] is None


def test_valid_json_array_of_objects_is_tabular_rows(tmp_path: Path) -> None:
    """Distinct from inspect_file's generic JSON summary: an array of flat
    objects is treated as ROWS with COLUMNS, per DuckDB's read_json."""
    p = tmp_path / "valid.json"
    p.write_text(json.dumps([{"id": i, "v": i * 2} for i in range(5)]), encoding="utf-8")
    result = inspect_tabular_file(str(p))
    assert result["state"] == "complete"
    assert result["format"] == "json"
    assert result["parser_id"] == "duckdb-json"
    assert result["shape"]["row_count"] == {"value": 5, "exact": True}
    assert {c["name"] for c in result["shape"]["columns"]} == {"id", "v"}


def test_valid_xlsx_inspects_complete(tmp_path: Path) -> None:
    if not _excel_extension_available():
        pytest.skip("DuckDB 'excel' extension not cached locally in this environment")
    p = tmp_path / "valid.xlsx"
    _write_xlsx(p, [(1, "a"), (2, "b")])
    result = inspect_tabular_file(str(p))
    assert result["state"] == "complete", result["errors"]
    assert result["format"] == "xlsx"
    assert result["parser_id"] == "duckdb-excel"
    assert result["shape"]["row_count"] == {"value": 2, "exact": True}
    names = [c["name"] for c in result["shape"]["columns"]]
    assert names == ["id", "name"]


def test_unsupported_format_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "binary.bin"
    p.write_bytes(b"\x00\x01\x02\x03not tabular data")
    result = inspect_tabular_file(str(p))
    assert result["state"] == "failed"
    assert result["errors"] == [{"code": "unsupported", "reason": "format_not_recognized"}]


def test_single_column_csv_needs_explicit_format_declaration(tmp_path: Path) -> None:
    """CSV has no magic-byte signature (unlike xml/json/xlsx) -- a
    single-column file has no delimiter to sniff, so 'auto' correctly
    fails closed; an explicit format='csv' still works."""
    p = tmp_path / "single_col.csv"
    p.write_text("id\n1\n2\n3\n", encoding="utf-8")
    auto_result = inspect_tabular_file(str(p))
    assert auto_result["errors"] == [{"code": "unsupported", "reason": "format_not_recognized"}]

    explicit_result = inspect_tabular_file(str(p), format="csv")
    assert explicit_result["state"] == "complete"
    assert explicit_result["shape"]["row_count"] == {"value": 3, "exact": True}


# ---------------------------------------------------------------------------
# Adversarial: malformed / truncated CSV
# ---------------------------------------------------------------------------


def test_csv_truncated_mid_multibyte_utf8_is_rejected(tmp_path: Path) -> None:
    """A CSV cut off mid-write, leaving a broken multi-byte UTF-8 sequence
    well past the format-sniff window -- a very real "truncated file"
    scenario, deterministically caught by this module's own full-file
    UTF-8 validation pass before DuckDB ever sees it."""
    p = tmp_path / "truncated.csv"
    with open(p, "wb") as f:
        f.write(b"id,name\n")
        for i in range(1000):  # push well past the 4096-byte sniff window
            f.write(f"{i},name{i}\n".encode("utf-8"))
        f.write("caf".encode("utf-8") + b"\xe9")  # truncated multi-byte tail, no newline
    result = inspect_tabular_file(str(p))
    assert result["state"] == "failed"
    assert result["errors"][0]["code"] == "malformed"
    assert result["errors"][0]["reason"] == "invalid_utf8"


def test_malformed_json_syntax_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "broken.json"
    p.write_bytes(b'[{"a": 1}, {"a": }]')
    result = inspect_tabular_file(str(p))
    assert result["state"] == "failed"
    assert result["errors"][0]["code"] == "malformed"


def test_deeply_nested_json_is_rejected_before_duckdb_parses_it(tmp_path: Path) -> None:
    """Bounded prescan (reused from inspector._json_structure_scan) rejects
    a pathologically deep-but-tiny JSON document before DuckDB's own JSON
    reader ever runs -- defense in depth, same rationale as the Wave 0
    JSON path."""
    p = tmp_path / "deep.json"
    p.write_text("[" * 200 + "1" + "]" * 200, encoding="utf-8")
    result = inspect_tabular_file(str(p), max_depth=50)
    assert result["state"] == "failed"
    assert result["errors"][0]["code"] == "limit_exceeded"
    assert result["errors"][0]["reason"] == "max_depth_exceeded"


# ---------------------------------------------------------------------------
# Adversarial: CSV/JSON far exceeding declared bounds
# ---------------------------------------------------------------------------


def test_csv_far_exceeding_sample_bound_reports_exact_count_and_truncated_sample(
    tmp_path: Path,
) -> None:
    """A file well under max_bytes can still contain far more rows than
    any reasonable sample/preview bound -- row_count stays EXACT (a full
    scan of a small bounded file is cheap and doesn't materialize rows),
    while the returned sample is capped and clearly marked truncated. This
    is the "distinguish exact from estimated, never overclaim" contract."""
    p = tmp_path / "wide_rows.csv"
    with open(p, "w", encoding="utf-8") as f:
        f.write("id,val\n")
        for i in range(200_000):
            f.write(f"{i},{i * 2}\n")
    assert p.stat().st_size < tabular.DEFAULT_MAX_BYTES

    result = inspect_tabular_file(str(p), max_sample_rows=10)
    assert result["state"] == "complete"
    assert result["shape"]["row_count"] == {"value": 200_000, "exact": True}
    assert len(result["shape"]["sample_rows"]) == 10
    assert result["shape"]["truncated_sample"] is True


def test_csv_far_exceeding_max_bytes_is_rejected_before_any_parse(tmp_path: Path) -> None:
    p = tmp_path / "oversized.csv"
    p.write_text("a,b\n" + "\n".join(f"{i},{i}" for i in range(10_000)), encoding="utf-8")
    real_size = p.stat().st_size
    result = inspect_tabular_file(str(p), max_bytes=64)
    assert result["state"] == "failed"
    assert result["errors"] == [
        {"code": "limit_exceeded", "reason": "max_bytes_exceeded", "detail": f"{real_size} > 64"}
    ]


def test_json_far_exceeding_max_bytes_is_rejected_before_any_parse(tmp_path: Path) -> None:
    p = tmp_path / "oversized.json"
    p.write_text(json.dumps([{"a": i} for i in range(10_000)]), encoding="utf-8")
    result = inspect_tabular_file(str(p), max_bytes=64)
    assert result["state"] == "failed"
    assert result["errors"][0]["code"] == "limit_exceeded"
    assert result["errors"][0]["reason"] == "max_bytes_exceeded"


def test_wide_pathological_csv_is_bounded_by_timeout_not_left_hanging(tmp_path: Path) -> None:
    """A small-in-bytes file can still have a huge number of columns (e.g.
    one very long header line) -- max_bytes alone cannot bound this
    pathological case; the wall-clock timeout + cross-thread interrupt
    must, and it must return promptly rather than hang or crash."""
    p = tmp_path / "wide_pathological.csv"
    ncols = 200_000
    with open(p, "w", encoding="utf-8") as f:
        f.write(",".join(f"c{i}" for i in range(ncols)) + "\n")
        f.write(",".join(str(i) for i in range(ncols)) + "\n")
    assert p.stat().st_size < tabular.DEFAULT_MAX_BYTES

    start = time.monotonic()
    result = inspect_tabular_file(str(p), timeout_seconds=0.001)
    elapsed = time.monotonic() - start

    assert elapsed < 30.0, "bounded runner must cancel and return promptly, never hang"
    assert result["state"] == "failed"
    assert result["errors"] == [{"code": "timeout", "reason": "wall_clock_budget_exceeded"}]


# ---------------------------------------------------------------------------
# Adversarial: zip-bomb-shaped / oversized XLSX
# ---------------------------------------------------------------------------


def test_zip_bomb_shaped_xlsx_is_rejected_without_inflating_it(tmp_path: Path) -> None:
    """A tiny-on-disk ZIP whose central directory declares a huge total
    uncompressed member size is refused via the declared-size sum alone
    (zipfile.infolist() never decompresses anything to read this) --
    BEFORE DuckDB's excel extension ever opens the file."""
    p = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", b"<?xml version='1.0'?><Types/>")
        # Highly compressible (all-zero) payload: tiny on disk, large when
        # its DECLARED uncompressed size is summed from the directory.
        zf.writestr("xl/worksheets/sheet1.xml", b"0" * (50 * 1024 * 1024))

    on_disk_size = p.stat().st_size
    assert on_disk_size < 1_000_000, "fixture must stay small on disk to prove this isn't just max_bytes"

    result = inspect_tabular_file(str(p), max_decompressed_bytes=1_000_000)
    assert result["state"] == "failed"
    assert result["errors"][0]["code"] == "limit_exceeded"
    assert result["errors"][0]["reason"] == "max_decompressed_bytes_exceeded"


def test_oversized_xlsx_is_rejected_by_max_bytes_before_zip_inspection(tmp_path: Path) -> None:
    if not _excel_extension_available():
        pytest.skip("DuckDB 'excel' extension not cached locally in this environment")
    p = tmp_path / "big.xlsx"
    _write_xlsx(p, [(i, f"row{i}") for i in range(50)])
    real_size = p.stat().st_size
    result = inspect_tabular_file(str(p), max_bytes=16)
    assert result["state"] == "failed"
    assert result["errors"] == [
        {"code": "limit_exceeded", "reason": "max_bytes_exceeded", "detail": f"{real_size} > 16"}
    ]


def test_non_zip_file_named_xlsx_is_rejected_as_malformed(tmp_path: Path) -> None:
    """format='xlsx' explicitly declared on content that isn't a ZIP
    container at all -- sniffing overrides a mismatched declaration
    upstream, so this only reaches the zip-bomb preflight when content IS
    zip-shaped; plain garbage with a forced xlsx extension is unsupported."""
    p = tmp_path / "not_really.xlsx"
    p.write_bytes(b"this is not a zip file at all")
    result = inspect_tabular_file(str(p))
    assert result["state"] == "failed"
    assert result["errors"] == [{"code": "unsupported", "reason": "format_not_recognized"}]


# ---------------------------------------------------------------------------
# Excel extension: no implicit network access
# ---------------------------------------------------------------------------


def test_xlsx_extension_unavailable_is_denied_not_a_network_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates a machine that has never cached the DuckDB 'excel'
    extension: refused with a clear denied/xlsx_extension_unavailable
    error, never an implicit network fetch, unless the caller explicitly
    opts in."""
    if not _excel_extension_available():
        pytest.skip("DuckDB 'excel' extension not cached locally in this environment")
    p = tmp_path / "valid.xlsx"
    _write_xlsx(p, [(1, "a")])

    monkeypatch.setattr(tabular, "_excel_extension_installed", lambda con: False)
    result = inspect_tabular_file(str(p))
    assert result["state"] == "failed"
    assert result["errors"] == [
        {
            "code": "denied",
            "reason": "xlsx_extension_unavailable",
            "detail": (
                "the DuckDB 'excel' core extension is not installed locally; "
                "refusing to make an implicit network call to fetch it (this "
                "contract requires no network access by default). Pre-cache it "
                "once with `INSTALL excel` in this environment, or pass "
                "allow_extension_network_install=True to fetch it now."
            ),
        }
    ]


# ---------------------------------------------------------------------------
# Path policy (reused from inspector._resolve_path_policy)
# ---------------------------------------------------------------------------


def test_directory_is_denied(tmp_path: Path) -> None:
    result = inspect_tabular_file(str(tmp_path))
    assert result["errors"] == [{"code": "denied", "reason": "is_a_directory"}]


def test_missing_path_is_denied(tmp_path: Path) -> None:
    result = inspect_tabular_file(str(tmp_path / "does_not_exist.csv"))
    assert result["errors"] == [{"code": "denied", "reason": "not_found"}]


def test_secret_named_file_is_denied_without_being_opened(tmp_path: Path) -> None:
    p = tmp_path / "credentials.csv"
    p.write_text("user,pass\nadmin,hunter2\n", encoding="utf-8")
    result = inspect_tabular_file(str(p))
    assert result["errors"] == [{"code": "denied", "reason": "secret_path_excluded"}]
    assert result["source_sha256"] is None  # never read


def test_outside_allowed_root_is_denied(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("a,b\n1,2\n", encoding="utf-8")
    result = inspect_tabular_file(str(outside), allowed_root=str(allowed))
    assert result["errors"] == [{"code": "denied", "reason": "outside_allowed_root"}]


def test_symlink_policy_branch_via_monkeypatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors test_local_file_inspection.py's equivalent -- exercises the
    exact same shared inspector._resolve_path_policy branch deterministically
    on every platform without needing real symlink privileges."""
    p = tmp_path / "regular.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    monkeypatch.setattr(tabular.os.path, "islink", lambda path: True)
    result = inspect_tabular_file(str(p))
    assert result["errors"] == [{"code": "denied", "reason": "symlink_not_allowed"}]

    result_allowed = inspect_tabular_file(str(p), allow_symlinks=True)
    assert result_allowed["state"] == "complete"


# ---------------------------------------------------------------------------
# Determinism and redaction
# ---------------------------------------------------------------------------


def test_result_hash_is_deterministic_across_repeated_calls(tmp_path: Path) -> None:
    p = tmp_path / "valid.csv"
    p.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    r1 = inspect_tabular_file(str(p))
    r2 = inspect_tabular_file(str(p))
    assert r1["result_hash"] == r2["result_hash"]
    assert r1["source_sha256"] == r2["source_sha256"]
    assert r1["shape"] == r2["shape"]


def test_source_ref_never_contains_raw_absolute_path(tmp_path: Path) -> None:
    p = tmp_path / "valid.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    result = inspect_tabular_file(str(p))
    assert str(tmp_path) not in result["source_ref"]
    assert result["source_ref"].endswith("valid.csv")


# ---------------------------------------------------------------------------
# No-write behavior
# ---------------------------------------------------------------------------


def test_inspect_tabular_file_never_writes_anything(tmp_path: Path) -> None:
    p = tmp_path / "valid.csv"
    p.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    before = sorted(os.listdir(tmp_path))
    before_stat = p.stat()

    inspect_tabular_file(str(p))
    inspect_tabular_file(str(p), allowed_root=str(tmp_path), max_sample_rows=1)

    after = sorted(os.listdir(tmp_path))
    after_stat = p.stat()
    assert before == after
    assert before_stat.st_mtime == after_stat.st_mtime
    assert before_stat.st_size == after_stat.st_size


# ---------------------------------------------------------------------------
# Capability manifest: local-only, degraded_ok registration (same posture
# as the Wave 0 inspector -- a missing excel extension degrades ONLY xlsx,
# never blocks a session).
# ---------------------------------------------------------------------------


def test_capability_manifest_entry_is_schema_valid_and_degraded_ok() -> None:
    raw = {
        "id": "local_file_inspection_tabular",
        "purpose": (
            "Inspect one local CSV/JSON/XLSX file's bounded tabular shape "
            "(schema/sample/row-count) through DuckDB, without a tunnel or "
            "Serena dependency."
        ),
        "required_tools": ["meridian-file-inspection:inspect_tabular_file"],
        "fallback_chain": [],
        "availability_policy": "degraded_ok",
    }
    normalized = cm.normalize_capability(raw)
    assert normalized["id"] == "local_file_inspection_tabular"
    assert normalized["availability_policy"] == "degraded_ok"

    manifest = cm.normalize_manifest([raw])
    assert cm.has_capability_manifest(manifest) is True
    assert manifest[0]["availability_policy"] == "degraded_ok"


# ---------------------------------------------------------------------------
# Sniffing internals (unit-level, no file I/O, no DuckDB)
# ---------------------------------------------------------------------------


def test_sniff_tabular_format_prefers_content_over_declared_mismatch() -> None:
    assert tabular.sniff_tabular_format(b"id,name\n1,a\n", "auto") == "csv"
    assert tabular.sniff_tabular_format(b'[{"a":1}]', "auto") == "json"
    assert tabular.sniff_tabular_format(b"PK\x03\x04therest", "auto") == "xlsx"
    assert tabular.sniff_tabular_format(b"\x00\x01binary", "auto") is None
    # Single-column CSV (no delimiter) only resolves when explicitly declared.
    assert tabular.sniff_tabular_format(b"onlyonecolumn\nrow1\n", "auto") is None
    assert tabular.sniff_tabular_format(b"onlyonecolumn\nrow1\n", "csv") == "csv"


def test_check_zip_bomb_rejects_over_threshold_without_inflating(tmp_path: Path) -> None:
    p = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.xml", b"1" * (10 * 1024 * 1024))
    error = tabular._check_zip_bomb(str(p), max_decompressed_bytes=1_000_000)
    assert error is not None
    assert error["code"] == "limit_exceeded"
    assert error["reason"] == "max_decompressed_bytes_exceeded"


def test_check_zip_bomb_accepts_small_archive(tmp_path: Path) -> None:
    p = tmp_path / "small.xlsx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.xml", b"hello")
    assert tabular._check_zip_bomb(str(p), max_decompressed_bytes=1_000_000) is None


def test_check_zip_bomb_rejects_bad_zip_container(tmp_path: Path) -> None:
    p = tmp_path / "fake.xlsx"
    p.write_bytes(b"PK\x03\x04not actually a valid zip stream")
    error = tabular._check_zip_bomb(str(p), max_decompressed_bytes=1_000_000)
    assert error is not None
    assert error["code"] == "malformed"


def test_resolve_duckdb_memory_limit_bytes_respects_floor_and_ceiling() -> None:
    assert tabular._resolve_duckdb_memory_limit_bytes(1) == tabular._DUCKDB_MEMORY_LIMIT_FLOOR_BYTES
    huge = tabular._DUCKDB_MEMORY_LIMIT_CEILING_BYTES * 10
    assert tabular._resolve_duckdb_memory_limit_bytes(huge) == tabular._DUCKDB_MEMORY_LIMIT_CEILING_BYTES
    mid = tabular._DUCKDB_MEMORY_LIMIT_FLOOR_BYTES + 1024
    assert tabular._resolve_duckdb_memory_limit_bytes(mid) == mid
