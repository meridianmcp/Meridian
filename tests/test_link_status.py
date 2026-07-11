"""Coverage for doc_documents.link_status — explicit source-link lifecycle (14015718).

The store's document header now carries a ``link_status`` with three states:

* ``live`` (default) — the source points at a real, writable .docx;
  insert_equation / update_paragraph write to it and reindex keeps it in sync.
  This is the only state the store implemented before this column, so every
  pre-existing row must read back as ``live`` (backward compat).
* ``deprecated`` — the link existed but the file moved / was renamed /
  superseded; the header persists as history and write-backs surface the
  ordinary missing-file error.
* ``independent`` — a standalone captured snapshot with no live file, never
  meant to be written back; write attempts refuse LOUDLY with a DISTINCT
  no-write-back error (not the generic "file not found").

Exercised here on a local SQLite sidecar (the store's real dual-backend
connection; the psycopg3 adapter runs the SAME ``?``-placeholder SQL and
intercepts the ``PRAGMA table_info`` used by the ADD-COLUMN guard):

* the ALTER-on-existing-DB path adds the column to a table created WITHOUT it,
  without wiping data, and defaults every pre-existing row to ``live``;
* ``put_document`` persists each of the three statuses, defaults unknown/omitted
  to ``live``, and PRESERVES an existing status on a source-less upsert;
* ``insert_equation`` / ``update_paragraph`` refuse an ``independent`` document
  with the distinct no-write-back error, while ``live`` keeps the existing
  missing-file behaviour;
* ``set_link_status`` transitions a stored document between states.
"""
from __future__ import annotations

import asyncio
import io
import zipfile

from meridian import doc_store
from meridian import db as db_module


# ---------------------------------------------------------------------------
# A real .docx ZIP with one id-addressable heading + body paragraph.
# ---------------------------------------------------------------------------

_DOCX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
    xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">
  <w:body>
    <w:p w14:paraId="0000C001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Chapter</w:t></w:r>
    </w:p>
    <w:p w14:paraId="0000C002">
      <w:r><w:t>A body paragraph.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def _write_docx(tmp_path, name: str = "doc.docx", xml: str = _DOCX_XML) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    path = tmp_path / name
    path.write_bytes(buf.getvalue())
    return str(path)


async def _open_store(tmp_path, name: str = "doc_structure.db") -> doc_store.DocStructureStore:
    conn = await db_module.init_db(str(tmp_path / name))
    store = doc_store.DocStructureStore(conn)
    await store.ensure_schema()
    return store


_HEADING = [
    {"ordinal": 0, "level": 1, "kind": "heading", "text": "Intro",
     "ref": "p1", "parent_ordinal": None},
]


# ---------------------------------------------------------------------------
# Pure enum normalization
# ---------------------------------------------------------------------------

def test_normalize_link_status_valid_and_fallback():
    assert doc_store._normalize_link_status("live") == "live"
    assert doc_store._normalize_link_status("deprecated") == "deprecated"
    assert doc_store._normalize_link_status("independent") == "independent"
    # Case/whitespace tolerant.
    assert doc_store._normalize_link_status("  INDEPENDENT ") == "independent"
    # Anything unknown/blank/non-string falls back to the backward-compat default.
    assert doc_store._normalize_link_status("bogus") == "live"
    assert doc_store._normalize_link_status("") == "live"
    assert doc_store._normalize_link_status(None) == "live"
    assert doc_store._normalize_link_status(42) == "live"


# ---------------------------------------------------------------------------
# put_document persists each status; default + preserve-on-upsert semantics
# ---------------------------------------------------------------------------

def test_put_document_defaults_to_live(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document("proj-1", "docx", _HEADING, source="a.docx")
            assert doc["link_status"] == "live"
            # Round-trips through get_document / get_structure.
            got = await store.get_document("proj-1", "a.docx")
            assert got["link_status"] == "live"
            struct = await store.get_structure("proj-1", "a.docx")
            assert struct["document"]["link_status"] == "live"
        finally:
            await store.close()

    asyncio.run(_run())


def test_put_document_persists_each_status(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            for status, src in (
                ("live", "live.docx"),
                ("deprecated", "dep.docx"),
                ("independent", "indep.docx"),
            ):
                doc = await store.put_document(
                    "proj-1", "docx", _HEADING, source=src, link_status=status,
                )
                assert doc["link_status"] == status
                assert (await store.get_document("proj-1", src))["link_status"] == status
        finally:
            await store.close()

    asyncio.run(_run())


def test_put_document_unknown_status_coerced_to_live(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            doc = await store.put_document(
                "proj-1", "docx", _HEADING, source="a.docx", link_status="garbage",
            )
            assert doc["link_status"] == "live"
        finally:
            await store.close()

    asyncio.run(_run())


def test_upsert_preserves_status_when_omitted_but_explicit_wins(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            # Mark independent, then re-store the SAME source without a status:
            # the independent status must survive (not silently revert to live).
            await store.put_document(
                "proj-1", "docx", _HEADING, source="a.docx", link_status="independent",
            )
            re_stored = await store.put_document(
                "proj-1", "docx", _HEADING, source="a.docx",
            )
            assert re_stored["link_status"] == "independent"

            # An explicit status on the next upsert overrides it.
            promoted = await store.put_document(
                "proj-1", "docx", _HEADING, source="a.docx", link_status="live",
            )
            assert promoted["link_status"] == "live"
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# set_link_status transitions
# ---------------------------------------------------------------------------

def test_set_link_status_transitions_and_missing(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            await store.put_document("proj-1", "docx", _HEADING, source="a.docx")
            updated = await store.set_link_status("proj-1", "a.docx", "deprecated")
            assert updated["link_status"] == "deprecated"
            assert (await store.get_document("proj-1", "a.docx"))["link_status"] == "deprecated"

            # Unknown status coerces to live.
            back = await store.set_link_status("proj-1", "a.docx", "nonsense")
            assert back["link_status"] == "live"

            # No such document -> None (never fabricates a row).
            assert await store.set_link_status("proj-1", "missing.docx", "live") is None
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# insert_equation / update_paragraph refuse an independent document loudly
# ---------------------------------------------------------------------------

def test_insert_equation_refuses_independent_with_distinct_error(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            docx_path = _write_docx(tmp_path)
            # Reindex creates a live doc; then mark it independent.
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            await store.set_link_status("proj-1", docx_path, "independent")

            res = await store.insert_equation(
                "proj-1", docx_path, "0000C002", "a^2 + b^2 = c^2", position="append",
            )
            assert "error" in res
            # DISTINCT no-write-back error, NOT the generic missing-file message.
            assert "independent" in res["error"]
            assert "no write-back" in res["error"]
            assert "not found on disk" not in res["error"]
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_refuses_independent_with_distinct_error(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            docx_path = _write_docx(tmp_path)
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            await store.set_link_status("proj-1", docx_path, "independent")

            raised = ""
            try:
                await store.update_paragraph(
                    "proj-1", docx_path, "0000C001", "Rewritten",
                )
            except ValueError as exc:
                raised = str(exc)
            assert "independent" in raised
            assert "no write-back" in raised
            assert "not found on disk" not in raised

            # The file was NOT mutated (still holds the original heading text).
            with zipfile.ZipFile(docx_path, "r") as zf:
                xml = zf.read("word/document.xml").decode("utf-8")
            assert "Chapter" in xml
            assert "Rewritten" not in xml
        finally:
            await store.close()

    asyncio.run(_run())


def test_live_document_keeps_ordinary_missing_file_behavior(tmp_path):
    """A LIVE doc whose file is gone still gets the ordinary missing-file error
    (proving 'independent' and 'temporarily missing' stay distinguishable)."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            docx_path = _write_docx(tmp_path)
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            # Doc stays 'live'; delete the underlying file out from under it.
            import os
            os.remove(docx_path)

            res = await store.insert_equation(
                "proj-1", docx_path, "0000C002", "x=1", position="append",
            )
            assert "error" in res
            assert "not found on disk" in res["error"]
            assert "no write-back" not in res["error"]

            raised = ""
            try:
                await store.update_paragraph("proj-1", docx_path, "0000C001", "x")
            except ValueError as exc:
                raised = str(exc)
            assert "not found on disk" in raised
            assert "no write-back" not in raised
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# The ALTER-on-existing-DB migration path
# ---------------------------------------------------------------------------

# The pre-link_status doc_documents CREATE literal (no link_status column) —
# exactly the schema an already-deployed prod DB carries. ensure_schema's
# CREATE TABLE IF NOT EXISTS is a NO-OP against this, so only the additive
# ALTER can introduce the column.
_LEGACY_DOC_DOCUMENTS = """
    CREATE TABLE doc_documents (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        source TEXT,
        doc_type TEXT NOT NULL,
        title TEXT,
        content_hash TEXT,
        element_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
"""


def test_alter_adds_column_on_existing_db_without_wiping_data(tmp_path):
    async def _run():
        db_path = str(tmp_path / "legacy.db")
        conn = await db_module.init_db(db_path)
        try:
            # 1) Build the OLD schema (no link_status) and seed a row + an element,
            #    simulating a database provisioned before this column existed.
            await conn.execute(_LEGACY_DOC_DOCUMENTS)
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS doc_elements ("
                "id TEXT PRIMARY KEY, document_id TEXT NOT NULL, parent_id TEXT, "
                "ordinal INTEGER NOT NULL, level INTEGER, kind TEXT NOT NULL, "
                "text TEXT, ref TEXT)"
            )
            await conn.execute(
                "INSERT INTO doc_documents "
                "(id, project_id, source, doc_type, title, content_hash, "
                "element_count, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("d1", "proj-1", "a.docx", "docx", "A", "h1", 1,
                 "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            await conn.execute(
                "INSERT INTO doc_elements "
                "(id, document_id, parent_id, ordinal, level, kind, text, ref) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("e1", "d1", None, 0, 1, "heading", "Intro", "p1"),
            )
            await conn.commit()

            # Column is genuinely absent before the migration runs.
            store = doc_store.DocStructureStore(conn)
            assert await store._column_exists("doc_documents", "link_status") is False

            # 2) ensure_schema runs the additive ALTER on the EXISTING table.
            await store.ensure_schema()
            assert await store._column_exists("doc_documents", "link_status") is True

            # 3) The pre-existing row survived AND defaults to 'live' (backward compat).
            got = await store.get_document("proj-1", "a.docx")
            assert got is not None
            assert got["title"] == "A"                 # data not wiped
            assert got["content_hash"] == "h1"
            assert got["link_status"] == "live"
            struct = await store.get_structure("proj-1", "a.docx")
            assert [e["text"] for e in struct["elements"]] == ["Intro"]  # element survived

            # 4) ensure_schema is idempotent — re-running does not error or duplicate.
            await store.ensure_schema()
            assert await store._column_exists("doc_documents", "link_status") is True

            # 5) The column is fully usable post-migration.
            await store.set_link_status("proj-1", "a.docx", "deprecated")
            assert (await store.get_document("proj-1", "a.docx"))["link_status"] == "deprecated"
        finally:
            await conn.close()

    asyncio.run(_run())
