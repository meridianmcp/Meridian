"""Coverage for the ID-addressable docx WRITE tool ``update_paragraph`` (f978e588).

Exercises:

* the pure docx-write helpers (_load_docx_xml, _find_paragraph_by_id,
  _normalize_runs, _set_paragraph_runs, _save_docx_xml) on a synthetic .docx,
* DocStructureStore.update_paragraph end-to-end on a local SQLite sidecar —
  rewriting a real paragraph in the on-disk .docx, addressed ONLY by its
  w14:paraId, and resyncing the matching doc_elements row,
* string input AND runs-list input (with basic bold/italic run formatting),
* the error surfaces (unknown doc, missing source path, unknown para_id),
* the tool through the real _dispatch_mcp_tool MCP path.
"""
from __future__ import annotations

import asyncio
import os
import zipfile

from lxml import etree as LET

from meridian import doc_store
from meridian import db as db_module


# ---------------------------------------------------------------------------
# Synthetic .docx fixture — a valid-enough OOXML package for the parser
# ---------------------------------------------------------------------------

_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="AAAA0001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="AAAA0002">
      <w:r><w:t>The original body sentence.</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>A paragraph with no paraId (p2 fallback).</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""

_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""

_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""


def _write_docx(path: str, document_xml: str = _DOCUMENT_XML) -> str:
    """Write a minimal but structurally valid .docx package to ``path``."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return path


def _read_document_xml(path: str) -> bytes:
    with zipfile.ZipFile(path, "r") as zf:
        return zf.read("word/document.xml")


# ---------------------------------------------------------------------------
# Pure helpers (no DB)
# ---------------------------------------------------------------------------

def test_load_and_find_paragraph_by_id(tmp_path):
    docx = _write_docx(str(tmp_path / "d.docx"))
    _raw, root = doc_store._load_docx_xml(docx)
    # Real paraId resolves to the bare paragraph element.
    p = doc_store._find_paragraph_by_id(root, "AAAA0002")
    assert p is not None
    assert doc_store._paragraph_plain_text(p) == "The original body sentence."
    # The (element, idx) variant reports the body-order index.
    found = doc_store._find_paragraph_with_index(root, "AAAA0002")
    assert found is not None
    _p, idx = found
    assert idx == 1
    # The p{index} fallback resolves the unlabelled third paragraph.
    found_fallback = doc_store._find_paragraph_with_index(root, "p2")
    assert found_fallback is not None
    assert found_fallback[1] == 2
    # An unknown id resolves to None (both accessors).
    assert doc_store._find_paragraph_by_id(root, "NOPE") is None
    assert doc_store._find_paragraph_with_index(root, "NOPE") is None


# ---------------------------------------------------------------------------
# c034fa24 — _save_docx_xml backup-before-overwrite
# ---------------------------------------------------------------------------


def test_save_docx_xml_writes_bak_of_prior_content_before_overwrite(tmp_path):
    docx = _write_docx(str(tmp_path / "d.docx"))
    original_bytes = _read_document_xml(docx)
    raw, root = doc_store._load_docx_xml(docx)
    p = doc_store._find_paragraph_by_id(root, "AAAA0002")
    doc_store._set_paragraph_runs(p, [{"text": "Edited body sentence."}])

    doc_store._save_docx_xml(raw, root, docx)

    backup_path = docx + ".bak"
    assert os.path.exists(backup_path)
    # The backup holds the PRE-edit content, not the new content.
    assert _read_document_xml(backup_path) == original_bytes
    # The real file holds the new content.
    assert b"Edited body sentence." in _read_document_xml(docx)
    assert b"The original body sentence." not in _read_document_xml(docx)


def test_save_docx_xml_bak_is_overwritten_not_accumulated_across_saves(tmp_path):
    """A single most-recent backup, not unbounded per-edit history."""
    docx = _write_docx(str(tmp_path / "d.docx"))
    raw, root = doc_store._load_docx_xml(docx)
    p = doc_store._find_paragraph_by_id(root, "AAAA0002")
    doc_store._set_paragraph_runs(p, [{"text": "First edit."}])
    doc_store._save_docx_xml(raw, root, docx)
    backup_path = docx + ".bak"
    assert b"The original body sentence." in _read_document_xml(backup_path)

    # Second save: the .bak now holds what was on disk before THIS save
    # (the first edit), not the original content, and there is still only
    # ever the one backup file.
    raw2, root2 = doc_store._load_docx_xml(docx)
    p2 = doc_store._find_paragraph_by_id(root2, "AAAA0002")
    doc_store._set_paragraph_runs(p2, [{"text": "Second edit."}])
    doc_store._save_docx_xml(raw2, root2, docx)
    assert b"First edit." in _read_document_xml(backup_path)
    assert b"Second edit." in _read_document_xml(docx)


def test_save_docx_xml_no_backup_when_dest_does_not_exist_yet(tmp_path):
    """A brand-new dest (never existed on disk) has nothing to back up."""
    docx_source = _write_docx(str(tmp_path / "source.docx"))
    raw, root = doc_store._load_docx_xml(docx_source)
    new_dest = str(tmp_path / "brand_new.docx")

    doc_store._save_docx_xml(raw, root, new_dest)

    assert os.path.exists(new_dest)
    assert not os.path.exists(new_dest + ".bak")


def test_save_docx_xml_backup_failure_does_not_block_the_save(tmp_path, monkeypatch):
    """Backup housekeeping is best-effort -- a failure to write it must never
    prevent the real save from completing."""
    docx = _write_docx(str(tmp_path / "d.docx"))
    raw, root = doc_store._load_docx_xml(docx)
    p = doc_store._find_paragraph_by_id(root, "AAAA0002")
    doc_store._set_paragraph_runs(p, [{"text": "Edited despite backup failure."}])

    def _boom(*args, **kwargs):
        raise OSError("simulated backup failure")

    monkeypatch.setattr(doc_store.shutil, "copy2", _boom)
    doc_store._save_docx_xml(raw, root, docx)  # must not raise

    assert b"Edited despite backup failure." in _read_document_xml(docx)
    assert not os.path.exists(docx + ".bak")


def test_normalize_runs_string_and_list_and_none():
    assert doc_store._normalize_runs("hello") == [{"text": "hello"}]
    # None empties the paragraph (single empty run), never a no-op.
    assert doc_store._normalize_runs(None) == [{"text": ""}]
    # A list of bare strings and dict-runs (bold/italic).
    runs = doc_store._normalize_runs(
        ["plain", {"text": "strong", "bold": True}, {"text": "em", "italic": True}]
    )
    assert runs[0] == {"text": "plain"}
    assert runs[1] == {"text": "strong", "bold": True}
    assert runs[2] == {"text": "em", "italic": True}
    # An empty list still yields one empty run.
    assert doc_store._normalize_runs([]) == [{"text": ""}]


def test_set_paragraph_runs_preserves_ppr_and_applies_formatting(tmp_path):
    docx = _write_docx(str(tmp_path / "d.docx"))
    _raw, root = doc_store._load_docx_xml(docx)
    p = doc_store._find_paragraph_by_id(root, "AAAA0001")
    doc_store._set_paragraph_runs(p, [{"text": "New Heading", "bold": True}])
    # pPr (the Heading1 style) survives the run rewrite.
    ppr = p.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pPr")
    assert ppr is not None
    assert doc_store._paragraph_plain_text(p) == "New Heading"
    # The bold toggle is present.
    xml = LET.tostring(p, encoding="unicode")
    assert "}b" in xml or "<w:b" in xml or ":b" in xml


# ---------------------------------------------------------------------------
# DocStructureStore.update_paragraph — end to end on a local sidecar
# ---------------------------------------------------------------------------

async def _open_store(tmp_path) -> doc_store.DocStructureStore:
    conn = await db_module.init_db(str(tmp_path / "doc_structure.db"))
    store = doc_store.DocStructureStore(conn)
    await store.ensure_schema()
    return store


def test_update_paragraph_string_input_rewrites_file_and_resyncs(tmp_path):
    """A heading paragraph: text changes in the .docx AND the doc_elements row
    (keyed by ref==paraId) is resynced."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            # reindex_document persists headings into doc_elements (ref = paraId).
            await store.reindex_document("proj-1", docx_path, source=docx_path)

            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0001", "Introduction (revised)",
            )
            assert result["para_id"] == "AAAA0001"
            assert result["new_text"] == "Introduction (revised)"
            # The heading IS a persisted element, so exactly one row resyncs.
            assert result["elements_resynced"] == 1
            assert result["source_path"] == docx_path

            # The .docx file on disk actually changed.
            new_xml = _read_document_xml(docx_path).decode("utf-8")
            assert "Introduction (revised)" in new_xml
            assert ">Introduction<" not in new_xml  # old run text gone

            # The doc_elements index row matches the new text.
            structure = await store.get_structure("proj-1", docx_path)
            heading = next(
                e for e in structure["elements"] if e["ref"] == "AAAA0001"
            )
            assert heading["text"] == "Introduction (revised)"
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_body_paragraph_resyncs_zero_but_writes_file(tmp_path):
    """A plain body paragraph is NOT persisted as an element, so resync is 0 —
    expected, not a failure — but the .docx is still rewritten."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "A rewritten body sentence.",
            )
            assert result["elements_resynced"] == 0
            new_xml = _read_document_xml(docx_path).decode("utf-8")
            assert "A rewritten body sentence." in new_xml
            assert "The original body sentence." not in new_xml
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_runs_list_applies_formatting(tmp_path):
    """A runs-list input builds multiple runs with basic formatting; the joined
    text is the new element text and the runs land in the file."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002",
                ["Plain ", {"text": "bold", "bold": True}, " tail"],
            )
            assert result["new_text"] == "Plain bold tail"
            new_xml = _read_document_xml(docx_path).decode("utf-8")
            assert "Plain " in new_xml and "bold" in new_xml and " tail" in new_xml
            # A bold toggle run property is present.
            root = LET.fromstring(_read_document_xml(docx_path))
            p = doc_store._find_paragraph_by_id(root, "AAAA0002")
            b_tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}b"
            assert any(el.tag == b_tag for el in p.iter())
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_fallback_paraid_targets_unlabelled_paragraph(tmp_path):
    """A paragraph with no w14:paraId is addressable by its 'p{index}' fallback."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            result = await store.update_paragraph(
                "proj-1", docx_path, "p2", "Now it has content.",
            )
            assert result["new_text"] == "Now it has content."
            new_xml = _read_document_xml(docx_path).decode("utf-8")
            assert "Now it has content." in new_xml
            assert "no paraId" not in new_xml
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_unknown_para_id_raises(tmp_path):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            raised = False
            try:
                await store.update_paragraph("proj-1", docx_path, "ZZZZ9999", "x")
            except ValueError as exc:
                raised = True
                assert "ZZZZ9999" in str(exc)
            assert raised
            # The file was NOT touched (still holds the original text).
            new_xml = _read_document_xml(docx_path).decode("utf-8")
            assert "The original body sentence." in new_xml
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_unknown_document_raises(tmp_path):
    async def _run():
        store = await _open_store(tmp_path)
        try:
            raised = False
            try:
                await store.update_paragraph(
                    "proj-1", "never-ingested.docx", "AAAA0001", "x",
                )
            except ValueError as exc:
                raised = True
                assert "never-ingested.docx" in str(exc)
            assert raised
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_missing_source_file_raises(tmp_path):
    """The document row exists but its source path is not on disk any more."""
    async def _run():
        store = await _open_store(tmp_path)
        try:
            # Persist a doc whose source points at a non-existent file.
            ghost = str(tmp_path / "ghost.docx")
            await store.put_document("proj-1", "docx", [], source=ghost)
            raised = False
            try:
                await store.update_paragraph("proj-1", ghost, "AAAA0001", "x")
            except ValueError as exc:
                raised = True
                assert "not found on disk" in str(exc)
            assert raised
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# eab6930a — staleness check before docx write (mtime/hash comparison)
# ---------------------------------------------------------------------------

def test_update_paragraph_no_stale_warning_when_file_unchanged_since_index(tmp_path):
    """Baseline: reindex then immediately update -- nothing changed externally,
    so no stale_warning key should appear at all."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "unrelated body edit",
            )
            assert "stale_warning" not in result
        finally:
            await store.close()

    asyncio.run(_run())


_DOCUMENT_XML_EXTERNALLY_EDITED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="AAAA0001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction -- edited directly in Word</w:t></w:r>
    </w:p>
    <w:p w14:paraId="AAAA0002">
      <w:r><w:t>The original body sentence.</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>A paragraph with no paraId (p2 fallback).</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def test_update_paragraph_flags_stale_warning_when_heading_changed_externally(tmp_path):
    """The heading's text (a persisted, hashed element) changes on disk OUTSIDE
    Meridian between reindex and update_paragraph -- a real content-hash drift.
    update_paragraph must still SUCCEED (advisory only) but surface stale_warning."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)

            # Simulate an external edit (e.g. opened directly in Word): rewrite
            # the same file, same para_ids, but different heading text -- this
            # changes the structural content hash without removing the AAAA0002
            # target this test's update_paragraph call will address.
            _write_docx(docx_path, _DOCUMENT_XML_EXTERNALLY_EDITED)

            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "a normal edit, unrelated to the drift",
            )
            # The write still succeeds -- advisory only, never a hard block.
            assert result["new_text"] == "a normal edit, unrelated to the drift"
            assert "stale_warning" in result
            warning = result["stale_warning"]
            assert warning["stale"] is True
            assert warning["stored_content_hash"] != warning["current_content_hash"]
            assert "edited outside Meridian" in warning["reason"]
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_fails_open_when_no_content_hash_recorded(tmp_path):
    """A document with no stored content_hash (e.g. an older row from before
    this column was populated) must not be treated as stale -- fails open,
    mirroring docs_intel's check_staleness "no-source-tracked" case."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            # Null out the recorded hash directly, simulating a row with none.
            doc = await store.get_document("proj-1", docx_path)
            await store._db.execute(
                "UPDATE doc_documents SET content_hash = NULL WHERE id = ?",
                (doc["id"],),
            )
            await store._db.commit()

            # Also drift the file externally -- even so, no hash to compare
            # against means no warning is possible (fail open).
            _write_docx(docx_path, _DOCUMENT_XML_EXTERNALLY_EDITED)

            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "edit with no baseline hash",
            )
            assert "stale_warning" not in result
        finally:
            await store.close()

    asyncio.run(_run())


def test_insert_equation_flags_stale_warning_when_content_changed_externally(tmp_path):
    """The same staleness check applies to insert_equation -- the other real
    docx-write path through _save_docx_xml."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            _write_docx(docx_path, _DOCUMENT_XML_EXTERNALLY_EDITED)

            result = await store.insert_equation(
                "proj-1", docx_path, "AAAA0002", "x = y", position="append",
            )
            assert "error" not in result
            assert "stale_warning" in result
            assert result["stale_warning"]["stale"] is True
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# MCP tool — update_paragraph through the real dispatch path
# ---------------------------------------------------------------------------

def _seed_store_via_env(tmp_path, monkeypatch):
    sidecar = str(tmp_path / "mcp_doc_structure.db")
    monkeypatch.setenv("MERIDIAN_DOC_STORE_URL", sidecar)
    doc_store._reset_doc_store_cache()
    return sidecar


def test_mcp_update_paragraph_round_trip(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        docx_path = _write_docx(str(tmp_path / "chapter1.docx"))
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "up-proj")
            pid = proj["id"]

            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.reindex_document(pid, docx_path, source=docx_path)

            res = await mh._dispatch_mcp_tool(
                "update_paragraph",
                {"project_id": pid, "doc": docx_path, "para_id": "AAAA0001",
                 "new_text": "Introduction (edited via MCP)"},
                db, str(tmp_path),
            )
            assert "error" not in res, res
            assert res["new_text"] == "Introduction (edited via MCP)"
            assert res["elements_resynced"] == 1

            new_xml = _read_document_xml(docx_path).decode("utf-8")
            assert "Introduction (edited via MCP)" in new_xml

            # runs-list variant through MCP.
            res_runs = await mh._dispatch_mcp_tool(
                "update_paragraph",
                {"project_id": pid, "doc": docx_path, "para_id": "AAAA0002",
                 "runs": [{"text": "Body ", "italic": True}, "plus tail"]},
                db, str(tmp_path),
            )
            assert "error" not in res_runs, res_runs
            assert res_runs["new_text"] == "Body plus tail"
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_update_paragraph_validation_errors(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            # Missing project_id.
            assert (await mh._dispatch_mcp_tool(
                "update_paragraph", {}, db, str(tmp_path),
            )).get("error")
            # Missing doc.
            assert (await mh._dispatch_mcp_tool(
                "update_paragraph", {"project_id": "p"}, db, str(tmp_path),
            )).get("error")
            # Missing para_id.
            assert (await mh._dispatch_mcp_tool(
                "update_paragraph", {"project_id": "p", "doc": "x.docx"},
                db, str(tmp_path),
            )).get("error")
            # Neither new_text nor runs.
            assert (await mh._dispatch_mcp_tool(
                "update_paragraph",
                {"project_id": "p", "doc": "x.docx", "para_id": "a"},
                db, str(tmp_path),
            )).get("error")
            # Both new_text AND runs.
            both = await mh._dispatch_mcp_tool(
                "update_paragraph",
                {"project_id": "p", "doc": "x.docx", "para_id": "a",
                 "new_text": "t", "runs": ["r"]},
                db, str(tmp_path),
            )
            assert both.get("error")
            assert "only one" in both["error"]
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_update_paragraph_unknown_doc_returns_error(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "up-proj-2")
            res = await mh._dispatch_mcp_tool(
                "update_paragraph",
                {"project_id": proj["id"], "doc": "never-ingested.docx",
                 "para_id": "AAAA0001", "new_text": "x"},
                db, str(tmp_path),
            )
            assert "error" in res
            assert "never-ingested.docx" in res["error"]
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())
