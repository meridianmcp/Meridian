"""NPZ header indexing and explicit PDF/NPZ degradation in meridian_outputs.

Covers:
  - _classify_suffix maps .npz to the new npz_content kind; .npy stays
    metadata_only (filename-only).
  - The stdlib NPZ parser: member names, shapes and dtypes from .npy
    headers, stored or deflated members, object dtype without unpickling,
    and every degrade path (not a zip, member cap, oversized or hostile
    header, unreadable member) falling back to filename-only.
  - The single-read analysis fast path and file_fingerprint agree.
  - search_outputs finds an archive by array name, shape token and dtype,
    and reports content_extractors (pypdf_missing, with one INFO log).
  - The content_index_version migration re-analyses existing PDF/NPZ rows
    once.

All NPZ fixtures are built with zipfile and struct; numpy is not needed.
"""
from __future__ import annotations

import io
import logging
import os
import pickle
import struct
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import outputs_local as OL
from meridian_outputs import search as S

try:
    import duckdb as _duckdb_probe  # noqa: F401
    _DUCKDB_AVAILABLE = True
except ImportError:
    _DUCKDB_AVAILABLE = False

try:
    import pypdf as _pypdf_probe  # noqa: F401
    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False

duckdb_required = pytest.mark.skipif(
    not _DUCKDB_AVAILABLE, reason="duckdb not installed"
)


# ---------------------------------------------------------------------------
# Fixture builders (stdlib only)
# ---------------------------------------------------------------------------

def _npy_bytes(
    descr: object, shape: tuple, *, payload: bytes = b"", major: int = 1,
    header_text: str | None = None,
) -> bytes:
    """An .npy stream with a real header, in the layout numpy writes."""
    if header_text is None:
        header_text = (
            f"{{'descr': {descr!r}, 'fortran_order': False, 'shape': {shape!r}, }}"
        )
    prefix_len = 10 if major == 1 else 12
    pad = (64 - (prefix_len + len(header_text) + 1) % 64) % 64
    encoding = "utf8" if major == 3 else "latin1"
    header = (header_text + " " * pad + "\n").encode(encoding)
    if major == 1:
        prefix = OL._NPY_MAGIC + bytes([1, 0]) + struct.pack("<H", len(header))
    else:
        prefix = OL._NPY_MAGIC + bytes([major, 0]) + struct.pack("<I", len(header))
    return prefix + header + payload


def _npz_bytes(members: dict[str, bytes], *, compressed: bool = False) -> bytes:
    """An .npz archive: one '<name>.npy' member per entry, like numpy.savez
    (or savez_compressed when ``compressed``)."""
    buf = io.BytesIO()
    method = zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED
    with zipfile.ZipFile(buf, "w", compression=method) as zf:
        for name, data in members.items():
            zf.writestr(f"{name}.npy", data)
    return buf.getvalue()


def _model_npz(*, compressed: bool = False) -> bytes:
    return _npz_bytes(
        {
            "weights": _npy_bytes("<f8", (100, 3), payload=b"\0" * 64),
            "labels": _npy_bytes("<i8", (100,), payload=b"\0" * 32),
        },
        compressed=compressed,
    )


_MODEL_ARRAYS = [
    {"name": "weights", "shape": [100, 3], "dtype": "float64"},
    {"name": "labels", "shape": [100], "dtype": "int64"},
]


def _single_page_pdf(text: str) -> bytes:
    """Minimal one-page PDF (same construction as test_fingerprint.py)."""
    stream_body = f"BT /F1 12 Tf 20 250 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> "
            b"/MediaBox [0 0 300 300] /Contents 4 0 R >>"
        ),
        f"<< /Length {len(stream_body)} >>\nstream\n".encode("ascii")
        + stream_body + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(buf.tell())
        buf.write(f"{i} 0 obj\n".encode("ascii"))
        buf.write(body)
        buf.write(b"\nendobj\n")
    xref_offset = buf.tell()
    n = len(objects) + 1
    buf.write(f"xref\n0 {n}\n".encode("ascii"))
    buf.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.write(f"{off:010d} 00000 n \n".encode("ascii"))
    buf.write(
        f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF"
        .encode("ascii")
    )
    return buf.getvalue()


def _hits(result: dict, name: str) -> bool:
    return any(h["path"].endswith(name) for h in result.get("hits", []))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class TestClassification:
    @pytest.mark.parametrize("name", ["model.npz", "MODEL.NPZ", "dir/run.Npz"])
    def test_npz_suffix_is_npz_content(self, name: str) -> None:
        assert OL._classify_suffix(f"/outputs/{name}") == "npz_content"

    def test_npy_stays_metadata_only(self, tmp_path: Path) -> None:
        """Regression: .npy keeps filename-only indexing even with a valid
        header that the NPZ parser could read."""
        f = tmp_path / "weights.npy"
        f.write_bytes(_npy_bytes("<f8", (100, 3)))
        fp = OL.file_fingerprint(str(f))
        assert fp.kind == "metadata_only"
        assert fp.npz_arrays is None
        assert OL._content_for_fts(str(f), fp) == "weights.npy"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class TestNpzParser:
    def test_stored_archive_lists_names_shapes_dtypes(self, tmp_path: Path) -> None:
        f = tmp_path / "model.npz"
        f.write_bytes(_model_npz())
        assert OL._npz_array_summary(str(f)) == _MODEL_ARRAYS

    def test_compressed_archive_still_yields_shape_and_dtype(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "model.npz"
        f.write_bytes(_model_npz(compressed=True))
        with zipfile.ZipFile(f) as zf:  # premise: really deflated
            assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in zf.infolist())
        assert OL._npz_array_summary(str(f)) == _MODEL_ARRAYS

    def test_object_dtype_reported_without_unpickling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        payload = pickle.dumps(["a", {"b": 1}])

        def _refuse(*_a, **_k):
            raise AssertionError("the NPZ parser unpickled a member")

        monkeypatch.setattr(pickle, "loads", _refuse)
        monkeypatch.setattr(pickle, "load", _refuse)
        f = tmp_path / "objects.npz"
        f.write_bytes(_npz_bytes(
            {"objs": _npy_bytes("|O", (2,), payload=payload)}, compressed=True,
        ))
        assert OL._npz_array_summary(str(f)) == [
            {"name": "objs", "shape": [2], "dtype": "object"},
        ]

    def test_header_versions_two_and_three(self) -> None:
        for major in (2, 3):
            parsed = OL._parse_npy_header(_npy_bytes("<f4", (7, 2, 5), major=major))
            assert parsed == ([7, 2, 5], "float32")

    def test_scalar_shape(self, tmp_path: Path) -> None:
        f = tmp_path / "scalar.npz"
        f.write_bytes(_npz_bytes({"lr": _npy_bytes("<f8", ())}))
        arrays = OL._npz_array_summary(str(f))
        assert arrays == [{"name": "lr", "shape": [], "dtype": "float64"}]
        assert OL._npz_fts_terms(arrays) == ["lr", "scalar", "float64"]

    @pytest.mark.parametrize("head", [
        b"",
        b"\x93NUMPY",
        b"NOTNUMPY\x01\x00" + b"\0" * 64,
        OL._NPY_MAGIC + bytes([4, 0]) + b"\0" * 64,   # unsupported version
        OL._NPY_MAGIC + bytes([2, 0]) + b"\x10\x00",  # v2 length field cut short
    ])
    def test_bad_prefix_is_rejected(self, head: bytes) -> None:
        assert OL._parse_npy_header(head) is None

    @pytest.mark.parametrize("header_text", [
        "__import__('os').system('echo should-never-run')",
        "['descr', 'shape']",
        "{'descr': '<f8', 'fortran_order': False, 'shape': [3, 4], }",
        "{'descr': '<f8', 'fortran_order': False, 'shape': (-1, 4), }",
        "{'descr': '<f8', 'fortran_order': False, 'shape': (True,), }",
        "{'descr': '<f8', 'fortran_order': False, ",
    ])
    def test_malformed_or_hostile_header_is_rejected(self, header_text: str) -> None:
        assert OL._parse_npy_header(_npy_bytes(None, (), header_text=header_text)) is None

    def test_header_longer_than_bounded_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(OL, "_NPZ_MAX_HEADER_BYTES", 32)
        f = tmp_path / "model.npz"
        f.write_bytes(_model_npz())
        assert OL._npz_array_summary(str(f)) == [
            {"name": "weights", "shape": None, "dtype": None},
            {"name": "labels", "shape": None, "dtype": None},
        ]

    @pytest.mark.parametrize("descr,expected", [
        ("<f8", "float64"), ("<f2", "float16"), ("f8", "float64"),
        ("<i4", "int32"), ("|u1", "uint8"), ("<c16", "complex128"),
        ("|b1", "bool"), ("|O", "object"), ("<M8[ns]", "datetime64[ns]"),
        ("<m8[s]", "timedelta64[s]"), ("<U10", "<U10"), ("|S5", "|S5"),
        ([("a", "<f8")], "structured"), ("", None), (5, None), ("<", None),
    ])
    def test_descr_to_dtype(self, descr: object, expected: str | None) -> None:
        assert OL._npy_descr_to_dtype(descr) == expected

    def test_directory_and_non_npy_members(self, tmp_path: Path) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("nested/", b"")
            zf.writestr("readme.txt", b"hello")
            zf.writestr("nested/grid.npy", _npy_bytes("<f8", (4, 4)))
        f = tmp_path / "mixed.npz"
        f.write_bytes(buf.getvalue())
        assert OL._npz_array_summary(str(f)) == [
            {"name": "readme.txt", "shape": None, "dtype": None},
            {"name": "nested/grid", "shape": [4, 4], "dtype": "float64"},
        ]

    def test_unknown_compression_method_is_filename_only(self, tmp_path: Path) -> None:
        """A member whose compression method is neither stored nor deflated
        (here 99, as an AES-encrypted entry declares) makes the whole file
        filename-only. Before B4 the member was opened and listed without a
        header; only stored and deflated reads are bounded, so no other
        method is opened at all now."""
        data = bytearray(_npz_bytes({"weights": _npy_bytes("<f8", (2,))}))
        cd = data.find(b"PK\x01\x02")
        struct.pack_into("<H", data, cd + 10, 99)  # unknown compression method
        f = tmp_path / "odd.npz"
        f.write_bytes(bytes(data))
        assert OL._npz_array_summary(str(f)) is None

    @pytest.mark.parametrize("method, expect_parsed", [
        (zipfile.ZIP_STORED, True),
        (zipfile.ZIP_DEFLATED, True),
        (zipfile.ZIP_BZIP2, False),
        (zipfile.ZIP_LZMA, False),
    ])
    def test_only_stored_and_deflated_members_are_opened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture, method: int, expect_parsed: bool,
    ) -> None:
        """B4: zipfile inflates a whole BZIP2/LZMA block on one read, so the
        64 KiB header read did not bound those members. They are refused
        before any member is opened; stored and deflated still parse."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=method) as zf:
            zf.writestr("weights.npy", _npy_bytes("<f8", (100, 3), payload=b"\0" * 4096))
        f = tmp_path / "model.npz"
        f.write_bytes(buf.getvalue())

        opened: list[str] = []
        real_open = zipfile.ZipFile.open

        def _spy_open(self, name, *args, **kwargs):
            opened.append(getattr(name, "filename", name))
            return real_open(self, name, *args, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, "open", _spy_open)
        monkeypatch.setattr(OL, "_NPZ_UNREADABLE_LOGGED", False)
        caplog.set_level(logging.INFO, logger=OL._log.name)
        arrays = OL._npz_array_summary(str(f))
        if expect_parsed:
            assert arrays == [{"name": "weights", "shape": [100, 3], "dtype": "float64"}]
            assert opened == ["weights.npy"]
        else:
            assert arrays is None
            assert opened == []
            logged = [r.getMessage() for r in caplog.records if ".npz archive" in r.getMessage()]
            assert len(logged) == 1
            assert f"compression method {method} is not stored or deflated" in logged[0]

    def test_one_bzip2_member_refuses_the_whole_archive_unread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a.npy", _npy_bytes("<f8", (2,)), compress_type=zipfile.ZIP_STORED)
            zf.writestr("b.npy", _npy_bytes("<f8", (3,)), compress_type=zipfile.ZIP_BZIP2)
        f = tmp_path / "mixed.npz"
        f.write_bytes(buf.getvalue())
        opened: list[object] = []
        monkeypatch.setattr(zipfile.ZipFile, "open", lambda self, name, *a, **k: opened.append(name))
        assert OL._npz_array_summary(str(f)) is None
        assert opened == []


# ---------------------------------------------------------------------------
# Degrade paths: corrupt archive, member cap
# ---------------------------------------------------------------------------

class TestNpzDegradesToFilenameOnly:
    @pytest.mark.parametrize("data", [
        b"",
        b"not a zip archive at all, just some bytes",
        b"PK\x05\x06" + b"\0" * 10,  # end record cut short
    ])
    def test_corrupt_npz_is_filename_only(self, tmp_path: Path, data: bytes) -> None:
        f = tmp_path / "broken.npz"
        f.write_bytes(data)
        fp = OL.file_fingerprint(str(f))
        assert fp.kind == "npz_content"
        assert fp.npz_arrays is None
        assert OL._content_for_fts(str(f), fp) == "broken.npz"

    def test_corrupt_central_directory_is_filename_only(self, tmp_path: Path) -> None:
        data = bytearray(_model_npz())
        cd = data.find(b"PK\x01\x02")
        data[cd:cd + 2] = b"XX"
        f = tmp_path / "broken.npz"
        f.write_bytes(bytes(data))
        assert OL._npz_array_summary(str(f)) is None

    def test_member_cap_breach_is_filename_only(self, tmp_path: Path) -> None:
        member = _npy_bytes("<f8", (1,))
        f = tmp_path / "many.npz"
        f.write_bytes(_npz_bytes(
            {f"a{i:03d}": member for i in range(OL._NPZ_MAX_MEMBERS + 1)},
        ))
        assert OL._NPZ_MAX_MEMBERS == 256
        fp = OL.file_fingerprint(str(f))
        assert fp.npz_arrays is None
        assert OL._content_for_fts(str(f), fp) == "many.npz"

    def test_cap_applies_to_listed_members_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An end record that under-declares its entries is still caught
        once the central directory is listed."""
        monkeypatch.setattr(OL, "_NPZ_MAX_MEMBERS", 2)
        monkeypatch.setattr(OL, "_zip_declared_entry_count", lambda _fh: 1)
        f = tmp_path / "three.npz"
        f.write_bytes(_npz_bytes(
            {name: _npy_bytes("<f8", (1,)) for name in ("a", "b", "c")},
        ))
        assert OL._npz_array_summary(str(f)) is None

    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert OL._npz_array_summary(str(tmp_path / "gone.npz")) is None

    def test_unreadable_npz_logged_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(OL, "_NPZ_UNREADABLE_LOGGED", False)
        caplog.set_level(logging.INFO, logger=OL._log.name)
        for name in ("a.npz", "b.npz", "c.npz"):
            (tmp_path / name).write_bytes(b"garbage")
            assert OL.file_fingerprint(str(tmp_path / name)).npz_arrays is None
        logged = [r for r in caplog.records if ".npz archive" in r.getMessage()]
        assert len(logged) == 1
        assert logged[0].levelno == logging.INFO
        assert "a.npz" in logged[0].getMessage()


# ---------------------------------------------------------------------------
# Fast path vs file_fingerprint
# ---------------------------------------------------------------------------

class TestAnalysisPathsAgree:
    @pytest.mark.parametrize("compressed", [False, True])
    def test_fast_path_matches_file_fingerprint(
        self, tmp_path: Path, compressed: bool,
    ) -> None:
        f = tmp_path / "model.npz"
        f.write_bytes(_model_npz(compressed=compressed))
        fp = OL.file_fingerprint(str(f))
        fast = OL._analyse_file(str(f), OL._xxh3_file)
        unhashed = OL._analyse_file(str(f), OL._xxh3_file, needs_hash=False)
        assert fp.npz_arrays == _MODEL_ARRAYS
        assert fast.fingerprint.npz_arrays == fp.npz_arrays
        assert unhashed.fingerprint.npz_arrays == fp.npz_arrays
        assert fast.sha256 is not None
        assert fast.content == OL._content_for_fts(str(f), fp)
        for token in ("weights", "100x3", "float64", "labels", "100", "int64"):
            assert token in fast.content.split()

    def test_fingerprint_to_dict_carries_npz_arrays(self, tmp_path: Path) -> None:
        f = tmp_path / "model.npz"
        f.write_bytes(_model_npz())
        assert OL.file_fingerprint(str(f)).to_dict()["npz_arrays"] == _MODEL_ARRAYS


# ---------------------------------------------------------------------------
# Search end to end, and the content_extractors envelope field
# ---------------------------------------------------------------------------

class TestSearch:
    @duckdb_required
    @pytest.mark.parametrize("query", ["weights", "100x3", "float64", "labels"])
    def test_search_finds_npz_by_header_terms(self, tmp_path: Path, query: str) -> None:
        (tmp_path / "model.npz").write_bytes(_model_npz(compressed=True))
        (tmp_path / "notes.txt").write_text("unrelated words only", encoding="utf-8")
        result = OL.search_outputs(str(tmp_path), query)
        assert _hits(result, "model.npz"), (query, result.get("hits"))
        assert not _hits(result, "notes.txt")

    @duckdb_required
    def test_corrupt_npz_still_found_by_filename(self, tmp_path: Path) -> None:
        (tmp_path / "checkpoint.npz").write_bytes(b"not a zip")
        result = OL.search_outputs(str(tmp_path), "checkpoint")
        assert _hits(result, "checkpoint.npz")

    @duckdb_required
    def test_envelope_reports_extractors(self, tmp_path: Path) -> None:
        (tmp_path / "model.npz").write_bytes(_model_npz())
        result = S.search_outputs(str(tmp_path), "weights")
        expected_pdf = "ok" if _PYPDF_AVAILABLE else "pypdf_missing"
        assert result["content_extractors"] == {"pdf": expected_pdf, "npz": "ok"}
        assert _hits(result, "model.npz")

    @duckdb_required
    def test_envelope_reports_extractors_on_zero_hits(self, tmp_path: Path) -> None:
        (tmp_path / "a.csv").write_text("x\n1", encoding="utf-8")
        result = S.search_outputs(str(tmp_path), "nothingmatchesthis")
        assert result["hits"] == []
        assert "content_extractors" in result

    @duckdb_required
    def test_pypdf_missing_reported_and_logged_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setitem(sys.modules, "pypdf", None)
        monkeypatch.setattr(OL, "_PYPDF_MISSING_LOGGED", False)
        caplog.set_level(logging.INFO, logger=OL._log.name)
        (tmp_path / "first.pdf").write_bytes(_single_page_pdf("alpha body"))
        (tmp_path / "second.pdf").write_bytes(_single_page_pdf("beta body"))

        result = S.search_outputs(str(tmp_path), "first")
        assert result["content_extractors"] == {"pdf": "pypdf_missing", "npz": "ok"}
        assert _hits(result, "first.pdf")  # still found by filename
        S.search_outputs(str(tmp_path), "second")

        logged = [
            r for r in caplog.records if "pypdf is not importable" in r.getMessage()
        ]
        assert len(logged) == 1
        assert logged[0].levelno == logging.INFO

    def test_content_extractor_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        if _PYPDF_AVAILABLE:
            assert OL.content_extractor_status() == {"pdf": "ok", "npz": "ok"}
        monkeypatch.setitem(sys.modules, "pypdf", None)
        assert OL.content_extractor_status() == {"pdf": "pypdf_missing", "npz": "ok"}

    def test_error_envelope_unchanged(self) -> None:
        result = S.search_outputs("/nonexistent/dir/for/npz", "q")
        assert "error" in result
        assert "content_extractors" not in result


# ---------------------------------------------------------------------------
# content_index_version migration
# ---------------------------------------------------------------------------

def _set_content_version(db_path: str, value: str | None) -> None:
    import duckdb
    con = duckdb.connect(db_path)
    try:
        con.execute(
            "DELETE FROM outputs_index_meta WHERE key = ?",
            [OL._CONTENT_INDEX_VERSION_KEY],
        )
        if value is not None:
            con.execute(
                "INSERT INTO outputs_index_meta (key, value) VALUES (?, ?)",
                [OL._CONTENT_INDEX_VERSION_KEY, value],
            )
    finally:
        con.close()


def _make_pre_feature_npz_row(db_path: str) -> None:
    """Rewrite the .npz row as code before npz_content wrote it."""
    import duckdb
    con = duckdb.connect(db_path)
    try:
        con.execute(
            "UPDATE outputs_index SET kind = 'binary_metadata', "
            "content = 'model.npz' WHERE path LIKE '%model.npz'"
        )
    finally:
        con.close()


class TestContentIndexVersionMigration:
    @duckdb_required
    def test_fresh_db_is_marked_current_immediately(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "index.duckdb")
        idx = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            con = idx._connect()
            assert idx._read_meta_version(
                con, OL._CONTENT_INDEX_VERSION_KEY,
            ) == OL._CONTENT_INDEX_VERSION
            assert not idx._content_reindex_pending
        finally:
            idx.close()

    @duckdb_required
    def test_version_bump_reanalyses_existing_pdf_and_npz_rows_once(
        self, tmp_path: Path,
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "model.npz").write_bytes(_model_npz())
        (out / "report.pdf").write_bytes(_single_page_pdf("quarterly loss summary"))
        (out / "table.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        db_path = str(tmp_path / "index.duckdb")

        idx1 = OL.OutputsFtsIndex(str(out), db_path=db_path)
        idx1.rebuild()
        idx1.close()
        _set_content_version(db_path, "1")
        _make_pre_feature_npz_row(db_path)

        idx2 = OL.OutputsFtsIndex(str(out), db_path=db_path)
        try:
            con = idx2._connect()
            queued = {os.path.basename(p) for p in idx2._content_reindex_pending}
            assert queued == {"model.npz", "report.pdf"}
            npz_key = next(p for p in idx2._row_cache if p.endswith("model.npz"))
            assert idx2._row_cache[npz_key].kind == "binary_metadata"  # premise

            analysed: list[str] = []
            real = OL._analyse_file

            def _spy(p, hasher, **kwargs):
                analysed.append(os.path.basename(p))
                return real(p, hasher, **kwargs)

            with patch.object(OL, "_analyse_file", side_effect=_spy):
                idx2.rebuild()
            assert sorted(analysed) == ["model.npz", "report.pdf"]
            assert idx2._read_meta_version(
                con, OL._CONTENT_INDEX_VERSION_KEY,
            ) == OL._CONTENT_INDEX_VERSION
            assert idx2._row_cache[npz_key].kind == "npz_content"
            content = idx2.get_content(npz_key) or ""
            assert "weights" in content and "100x3" in content

            analysed.clear()
            with patch.object(OL, "_analyse_file", side_effect=_spy):
                idx2.rebuild()
            assert analysed == []
        finally:
            idx2.close()

        idx3 = OL.OutputsFtsIndex(str(out), db_path=db_path)
        try:
            idx3._connect()
            assert not idx3._content_reindex_pending
            assert not idx3._pending_stale
        finally:
            idx3.close()

    @duckdb_required
    def test_marker_waits_until_every_queued_row_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "a.npz").write_bytes(_model_npz())
        (out / "b.npz").write_bytes(_model_npz(compressed=True))
        db_path = str(tmp_path / "index.duckdb")
        idx1 = OL.OutputsFtsIndex(str(out), db_path=db_path)
        idx1.rebuild()
        idx1.close()
        _set_content_version(db_path, None)  # written before the marker existed

        idx2 = OL.OutputsFtsIndex(str(out), db_path=db_path)
        monkeypatch.setattr(idx2, "_adaptive_batch_limit", lambda: 1)
        try:
            con = idx2._connect()
            assert len(idx2._content_reindex_pending) == 2
            idx2.rebuild()
            assert len(idx2._content_reindex_pending) == 1
            assert idx2._read_meta_version(con, OL._CONTENT_INDEX_VERSION_KEY) == 0
            idx2.rebuild()
            assert not idx2._content_reindex_pending
            assert idx2._read_meta_version(
                con, OL._CONTENT_INDEX_VERSION_KEY,
            ) == OL._CONTENT_INDEX_VERSION
        finally:
            idx2.close()

    @duckdb_required
    def test_index_without_pdf_or_npz_rows_is_marked_without_work(
        self, tmp_path: Path,
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "table.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        db_path = str(tmp_path / "index.duckdb")
        idx1 = OL.OutputsFtsIndex(str(out), db_path=db_path)
        idx1.rebuild()
        idx1.close()
        _set_content_version(db_path, "1")

        idx2 = OL.OutputsFtsIndex(str(out), db_path=db_path)
        try:
            con = idx2._connect()
            assert not idx2._content_reindex_pending
            assert not idx2._pending_stale
            assert idx2._read_meta_version(
                con, OL._CONTENT_INDEX_VERSION_KEY,
            ) == OL._CONTENT_INDEX_VERSION
        finally:
            idx2.close()

    def test_needs_content_reindex(self) -> None:
        assert OL.OutputsFtsIndex._needs_content_reindex("/o/a.bin", "pdf_content")
        assert OL.OutputsFtsIndex._needs_content_reindex("/o/a.bin", "npz_content")
        assert OL.OutputsFtsIndex._needs_content_reindex("/o/A.NPZ", "binary_metadata")
        assert OL.OutputsFtsIndex._needs_content_reindex("/o/r.pdf", None)
        assert not OL.OutputsFtsIndex._needs_content_reindex("/o/w.npy", "metadata_only")
        assert not OL.OutputsFtsIndex._needs_content_reindex("/o/t.csv", "text_content")

    def test_read_meta_version_tolerates_bad_values(self) -> None:
        class _Con:
            def __init__(self, row=None, exc=None):
                self._row, self._exc = row, exc

            def execute(self, *_a, **_k):
                if self._exc:
                    raise self._exc
                return self

            def fetchone(self):
                return self._row

        idx = OL.OutputsFtsIndex("/nonexistent/for/meta")
        try:
            assert idx._read_meta_version(_Con(row=None), "k") == 0
            assert idx._read_meta_version(_Con(row=(None,)), "k") == 0
            assert idx._read_meta_version(_Con(row=("x",)), "k") == 0
            assert idx._read_meta_version(_Con(row=("3",)), "k") == 3
            assert idx._read_meta_version(_Con(exc=RuntimeError("boom")), "k") == 0
        finally:
            idx.close()
