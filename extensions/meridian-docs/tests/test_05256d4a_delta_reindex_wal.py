"""Tests for 05256d4a: delta re-index (not full DELETE+reinsert+FTS5 rebuild)
+ WAL mode.

PERF: index_docx() previously always did a full DELETE + reinsert + explicit
FTS5 'rebuild', even for a one-paragraph change -- and _ensure_fresh()
triggered that same full rebuild transparently on every stale read. No
incremental/delta path existed at all.

Scope (confirmed, per Adam): NOT Parquet/columnar, NOT diff/version-history
tracking -- purely (a) a delta index_docx() that only touches paragraph rows
that actually changed (diffed by para_id + an (idx, style, text) content
comparison against docx_paragraphs' existing para_id TEXT PRIMARY KEY), and
(b) PRAGMA journal_mode=WAL somewhere in this module's connection setup.

These tests prove:
  1. An unchanged document re-index is a near-no-op (no rows touched).
  2. A one-paragraph edit only touches that paragraph's row, not the whole
     table.
  3. WAL mode is active after connection setup.
  4. FTS5 search results are still correct after a delta update (not just
     full rebuilds) -- including a paragraph that was ADDED and one that was
     REMOVED between index_docx calls.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from meridian_docs import docs_intel


def _make_docx(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _doc_xml(paragraphs: list[str]) -> str:
    """Build a minimal document.xml body with one native-paraId <w:p> per
    string in *paragraphs* (8-hex-digit ids P0000001, P0000002, ...),
    assigned by LIST POSITION. Only safe when every "version" of a document
    passed to a single test keeps the same paragraph COUNT and ORDER -- use
    :func:`_doc_xml_by_id` instead when paragraphs are added/removed/reordered
    between versions, so each paragraph keeps a STABLE id across versions
    regardless of its position in a given version's list."""
    body = "\n".join(
        f'    <w:p w14:paraId="P{i + 1:07d}"><w:r><w:t>{text}</w:t></w:r></w:p>'
        for i, text in enumerate(paragraphs)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
{body}
  </w:body>
</w:document>
"""


def _doc_xml_by_id(paragraphs: list[tuple[str, str]]) -> str:
    """Build a minimal document.xml body from explicit ``(id_suffix, text)``
    pairs, e.g. ``[("1", "Alpha."), ("3", "Gamma.")]`` -> paraIds
    P0000001/P0000003. Each paragraph keeps a STABLE id across two "versions"
    of a document regardless of insertions/removals elsewhere -- exactly the
    scenario :func:`_doc_xml` (position-based ids) cannot represent."""
    body = "\n".join(
        f'    <w:p w14:paraId="P{id_suffix:0>7}"><w:r><w:t>{text}</w:t></w:r></w:p>'
        for id_suffix, text in paragraphs
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
{body}
  </w:body>
</w:document>
"""


def _row_count(index_db_path: str) -> int:
    conn = docs_intel._connect(index_db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM docx_paragraphs").fetchone()[0]
    finally:
        conn.close()


def _table_snapshot(index_db_path: str) -> dict[str, tuple]:
    conn = docs_intel._connect(index_db_path)
    try:
        return {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute("SELECT para_id, idx, style, text FROM docx_paragraphs")
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Unchanged document re-index is a near-no-op.
# ---------------------------------------------------------------------------


def test_unchanged_reindex_touches_no_rows(tmp_path):
    docx_bytes = _make_docx(_doc_xml(["Alpha paragraph.", "Beta paragraph.", "Gamma paragraph."]))
    index_db_path = str(tmp_path / "sidecar.sqlite3")

    first = docs_intel.index_docx(docx_bytes, index_db_path)
    assert first["delta"] == {"inserted": 3, "updated": 0, "deleted": 0, "unchanged": 0}

    second = docs_intel.index_docx(docx_bytes, index_db_path)
    assert second["delta"] == {"inserted": 0, "updated": 0, "deleted": 0, "unchanged": 3}
    assert second["paragraph_count"] == 3


# ---------------------------------------------------------------------------
# 2. A one-paragraph edit only touches that paragraph's row.
# ---------------------------------------------------------------------------


def test_one_paragraph_edit_touches_only_that_row(tmp_path):
    index_db_path = str(tmp_path / "sidecar.sqlite3")
    original = _make_docx(_doc_xml(["Alpha paragraph.", "Beta paragraph.", "Gamma paragraph."]))
    docs_intel.index_docx(original, index_db_path)
    before = _table_snapshot(index_db_path)

    edited = _make_docx(_doc_xml(["Alpha paragraph.", "Beta paragraph -- EDITED.", "Gamma paragraph."]))
    result = docs_intel.index_docx(edited, index_db_path)

    assert result["delta"] == {"inserted": 0, "updated": 1, "deleted": 0, "unchanged": 2}

    after = _table_snapshot(index_db_path)
    # Only the edited paragraph's row actually changed.
    changed_ids = {pid for pid in before if before[pid] != after.get(pid)}
    assert changed_ids == {"P0000002"}
    assert after["P0000002"][2] == "Beta paragraph -- EDITED."
    # Untouched rows are byte-identical to before, including their idx.
    assert after["P0000001"] == before["P0000001"]
    assert after["P0000003"] == before["P0000003"]


def test_paragraph_insert_and_removal_are_isolated_to_affected_rows(tmp_path):
    index_db_path = str(tmp_path / "sidecar.sqlite3")
    # Each paragraph keeps a STABLE id across both versions (by id, not
    # position) so removing the middle one is unambiguous: Beta (id 2) is
    # deleted outright, and Gamma (id 3) merely shifts idx 2 -> 1.
    original = _make_docx(_doc_xml_by_id([("1", "Alpha."), ("2", "Beta."), ("3", "Gamma.")]))
    docs_intel.index_docx(original, index_db_path)

    # Remove "Beta." entirely -- Gamma shifts from idx 2 to idx 1.
    edited = _make_docx(_doc_xml_by_id([("1", "Alpha."), ("3", "Gamma.")]))
    result = docs_intel.index_docx(edited, index_db_path)

    assert result["delta"]["deleted"] == 1
    assert result["delta"]["updated"] == 1  # Gamma's idx shifted
    assert result["delta"]["unchanged"] == 1  # Alpha untouched

    snapshot = _table_snapshot(index_db_path)
    assert "P0000002" not in snapshot  # Beta's row is gone
    assert snapshot["P0000001"] == (0, None, "Alpha.")
    assert snapshot["P0000003"] == (1, None, "Gamma.")  # idx updated to 1


# ---------------------------------------------------------------------------
# 3. WAL mode is active after connection setup.
# ---------------------------------------------------------------------------


def test_wal_mode_active_after_connect(tmp_path):
    index_db_path = str(tmp_path / "sidecar.sqlite3")
    conn = docs_intel._connect(index_db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_wal_mode_persists_across_reconnects(tmp_path):
    """journal_mode=WAL is a database-file-level setting -- once set by one
    connection, a later fresh connection to the same file is already WAL
    without needing to re-issue the pragma (though _connect always does)."""
    index_db_path = str(tmp_path / "sidecar.sqlite3")
    docs_intel._connect(index_db_path).close()

    conn2 = docs_intel._connect(index_db_path)
    try:
        mode = conn2.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# 4. FTS5 search is still correct after a delta update.
# ---------------------------------------------------------------------------


def test_fts5_search_correct_after_delta_update(tmp_path):
    index_db_path = str(tmp_path / "sidecar.sqlite3")
    # Stable ids across both versions: id "1" = fox paragraph, "2" = lorem
    # ipsum (removed in the edit), "3" = meridian paragraph (unchanged).
    original = _make_docx(
        _doc_xml_by_id(
            [
                ("1", "The quick brown fox."),
                ("2", "Lorem ipsum dolor sit amet."),
                ("3", "Meridian coordinates sessions."),
            ]
        )
    )
    docs_intel.index_docx(original, index_db_path)

    results = docs_intel.fts5_search_paragraphs(index_db_path, "meridian")
    assert [r["para_id"] for r in results] == ["P0000003"]

    # Edit paragraph "1" to mention "meridian" too, ADD a brand-new paragraph
    # "4" also mentioning it, and REMOVE paragraph "2" (lorem ipsum) entirely
    # -- all in one delta update.
    edited = _make_docx(
        _doc_xml_by_id(
            [
                ("1", "The quick brown fox loves meridian."),
                ("3", "Meridian coordinates sessions."),
                ("4", "A brand new paragraph about meridian too."),
            ]
        )
    )
    result = docs_intel.index_docx(edited, index_db_path)
    # "1": text edited (idx unchanged at 0) -> updated. "2": removed ->
    # deleted. "3": text unchanged, but its idx shifts 2 -> 1 because "2" was
    # removed ahead of it -> also updated (idx IS part of the stored row and
    # must reflect reality). "4": brand new -> inserted. Nothing is a true
    # no-op here since every surviving paragraph's idx or text changed.
    assert result["delta"]["updated"] == 2  # paragraph "1" (text) + "3" (idx shift)
    assert result["delta"]["inserted"] == 1  # brand-new paragraph "4"
    assert result["delta"]["deleted"] == 1  # old paragraph "2" (lorem ipsum) gone
    assert result["delta"]["unchanged"] == 0

    results_after = docs_intel.fts5_search_paragraphs(index_db_path, "meridian")
    matched_ids = {r["para_id"] for r in results_after}
    assert matched_ids == {"P0000001", "P0000003", "P0000004"}

    # A query for the now-deleted paragraph's distinctive term returns nothing.
    stale_results = docs_intel.fts5_search_paragraphs(index_db_path, "lorem")
    assert stale_results == []


def test_fts5_search_no_op_reindex_still_correct(tmp_path):
    """A pure no-op delta re-index (nothing changed) must not corrupt or
    empty out the FTS5 index."""
    index_db_path = str(tmp_path / "sidecar.sqlite3")
    docx_bytes = _make_docx(_doc_xml(["Alpha paragraph.", "Meridian is great."]))
    docs_intel.index_docx(docx_bytes, index_db_path)
    docs_intel.index_docx(docx_bytes, index_db_path)  # no-op re-index

    results = docs_intel.fts5_search_paragraphs(index_db_path, "meridian")
    assert len(results) == 1
    assert results[0]["para_id"] == "P0000002"
