"""5988a5bb — centralized fail-closed DOCX write envelope for update_paragraph.

Covers the gap the item's own pointers identified in ``meridian/doc_store.py``:

* Part A — mandatory post-write verification: a write that promotes
  structurally (media/style/relationship counts unchanged) but does NOT
  actually land the intended paragraph edit is caught and rejected, with a
  best-effort restore from the ``.bak`` backup, never a false success.
* Part B — an OPT-IN, fail-closed ``expected_content_hash`` precondition
  gate: a stale hash rejects the write BEFORE anything is touched; a
  matching hash proceeds normally; omitting it is byte-identical to the
  pre-5988a5bb advisory-only behavior (covered by the untouched
  ``tests/test_update_paragraph.py`` staleness tests).
* Part C — opt-in wave-scoped draft mode (``draft_output_path`` +
  ``wave_run_id`` + ``session_id``): the write targets an isolated draft,
  the canonical file and its ``doc_elements`` index stay untouched, real
  ``meridian.db.docx_merge`` primitives gate anchor exclusivity, and
  ``DocStructureStore.merge_paragraph_draft`` promotes a drafted anchor into
  canonical only after ``check_merge_stale_or_overlap`` clears it.
* Part D — the full write manifest (``pre_counts`` / ``post_counts``,
  previously computed by ``_write_docx_transaction`` but discarded) is now
  surfaced on every successful ``update_paragraph`` / ``merge_paragraph_draft``
  result.
"""
from __future__ import annotations

import asyncio
import os
import zipfile

import pytest

from meridian import doc_store
from meridian import db as db_module


# ---------------------------------------------------------------------------
# Synthetic .docx fixture (mirrors tests/test_update_paragraph.py's)
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
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return path


def _read_document_xml(path: str) -> bytes:
    with zipfile.ZipFile(path, "r") as zf:
        return zf.read("word/document.xml")


async def _open_store(tmp_path) -> doc_store.DocStructureStore:
    conn = await db_module.init_db(str(tmp_path / "doc_structure.db"))
    store = doc_store.DocStructureStore(conn)
    await store.ensure_schema()
    return store


async def _mk_session(db, name: str) -> str:
    """Create a minimal project+session, return the session id.

    docx_merge_drafts.session_id carries a real ``REFERENCES sessions(id)``
    foreign key (enforced -- ``PRAGMA foreign_keys = ON``), so wave-scoped
    draft-mode calls need a genuine session row, not an arbitrary string.
    """
    proj = await db_module.create_project(db, name=f"proj-{name}")
    sess = await db_module.register_session(db, project_id=proj["id"], name=name)
    return sess["id"]


# ---------------------------------------------------------------------------
# Part A — mandatory post-write verification (fail-closed, restore-on-failure)
# ---------------------------------------------------------------------------

def test_update_paragraph_detects_silent_no_op_and_restores_backup(tmp_path, monkeypatch):
    """A write that promotes structurally but does not actually change the
    target paragraph's text must surface as DocxPostWriteVerificationError,
    never a false success, and the source file must be restored to its
    pre-write state from the .bak backup _write_docx_transaction wrote."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            original_bytes = open(docx_path, "rb").read()

            # Simulate a silent no-op / wrong-target write: the structural
            # manifest gate alone would never catch this (media/style/
            # relationship counts are unaffected by a paragraph text change).
            monkeypatch.setattr(
                doc_store, "_verify_paragraph_write",
                lambda *a, **k: "simulated silent no-op",
            )

            with pytest.raises(doc_store.DocxPostWriteVerificationError) as excinfo:
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "this edit must be rejected",
                )
            assert "simulated silent no-op" in str(excinfo.value)
            assert excinfo.value.manifest.get("restored") is True

            assert open(docx_path, "rb").read() == original_bytes
            xml = _read_document_xml(docx_path).decode("utf-8")
            assert "this edit must be rejected" not in xml
            assert "The original body sentence." in xml
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_verified_write_succeeds_normally(tmp_path):
    """The counterpart happy path -- an un-mocked, genuinely correct write
    passes post-write verification and reports success as before."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "a genuinely correct edit",
            )
            assert result["new_text"] == "a genuinely correct edit"
            assert "a genuinely correct edit" in _read_document_xml(docx_path).decode()
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Part B — opt-in fail-closed expected_content_hash precondition gate
# ---------------------------------------------------------------------------

def test_update_paragraph_rejects_mismatched_expected_content_hash_before_write(tmp_path):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            original_bytes = open(docx_path, "rb").read()

            with pytest.raises(ValueError, match="expected_content_hash mismatch"):
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "should never land",
                    expected_content_hash="not-the-real-hash",
                )

            assert open(docx_path, "rb").read() == original_bytes
            assert "should never land" not in _read_document_xml(docx_path).decode()
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_accepts_matching_expected_content_hash(tmp_path):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            current_hash = doc_store._docx_current_content_hash(docx_path)

            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "hash-gated edit",
                expected_content_hash=current_hash,
            )
            assert result["new_text"] == "hash-gated edit"
            assert "hash-gated edit" in _read_document_xml(docx_path).decode()
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_expected_content_hash_blank_string_rejected(tmp_path):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            with pytest.raises(ValueError, match="non-empty string"):
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "x", expected_content_hash="   ",
                )
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Backward compatibility — omitting the new params is byte-identical
# ---------------------------------------------------------------------------

def test_update_paragraph_omitting_new_params_is_unaffected(tmp_path):
    """Every new keyword argument omitted: identical call shape and result
    shape to the pre-5988a5bb contract (plus the additive pre_counts/
    post_counts fields from part D)."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0001", "Introduction (revised)",
            )
            assert result["para_id"] == "AAAA0001"
            assert result["new_text"] == "Introduction (revised)"
            assert result["elements_resynced"] == 1
            assert result["source_path"] == docx_path
            assert "draft_path" not in result
            assert "is_draft" not in result
            assert isinstance(result["manifest_hash"], str)
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Part D — full write manifest surfaced (pre_counts / post_counts)
# ---------------------------------------------------------------------------

def test_update_paragraph_surfaces_full_write_manifest(tmp_path):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "manifest surfaced edit",
            )
            assert result["pre_counts"] == {
                "media_count": 0, "style_count": 0,
                "equation_count": 0, "relationship_count": 1,
            }
            assert result["post_counts"]["relationship_count"] == 1
            assert len(result["manifest_hash"]) == 64
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Part C — wave-scoped draft mode + merge_paragraph_draft
# ---------------------------------------------------------------------------

def test_update_paragraph_draft_mode_requires_both_or_neither(tmp_path):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            session_id = await _mk_session(store._db, "solo")

            with pytest.raises(ValueError, match="together"):
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "x",
                    draft_output_path=str(tmp_path / "draft.docx"),
                    session_id=session_id,
                )
            with pytest.raises(ValueError, match="together"):
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "x",
                    wave_run_id="wave-1", session_id=session_id,
                )
            with pytest.raises(ValueError, match="session_id"):
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "x",
                    draft_output_path=str(tmp_path / "draft.docx"), wave_run_id="wave-1",
                )
            with pytest.raises(ValueError, match="differ"):
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "x",
                    draft_output_path=docx_path, wave_run_id="wave-1",
                    session_id=session_id,
                )
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_draft_mode_writes_isolated_draft_without_touching_canonical(tmp_path):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "canonical.docx"))
        draft_path = str(tmp_path / "draft-session-a.docx")
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            session_a = await _mk_session(store._db, "sess-a")
            original_bytes = open(docx_path, "rb").read()

            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "drafted edit, not yet canonical",
                draft_output_path=draft_path, wave_run_id="wave-1", session_id=session_a,
            )
            assert result["is_draft"] is True
            assert result["draft_path"] == draft_path
            assert result["wave_run_id"] == "wave-1"
            assert "elements_resynced" not in result

            # Canonical byte-for-byte untouched.
            assert open(docx_path, "rb").read() == original_bytes
            assert "drafted edit" not in _read_document_xml(docx_path).decode()

            # The isolated draft holds the edit.
            assert os.path.exists(draft_path)
            assert "drafted edit, not yet canonical" in _read_document_xml(draft_path).decode()
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_draft_mode_does_not_resync_canonical_index(tmp_path):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "canonical.docx"))
        draft_path = str(tmp_path / "draft-session-a.docx")
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            session_a = await _mk_session(store._db, "sess-a")

            await store.update_paragraph(
                "proj-1", docx_path, "AAAA0001", "drafted heading, not yet canonical",
                draft_output_path=draft_path, wave_run_id="wave-1", session_id=session_a,
            )

            structure = await store.get_structure("proj-1", docx_path)
            heading = next(e for e in structure["elements"] if e["ref"] == "AAAA0001")
            assert heading["text"] == "Introduction"
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_draft_mode_rejects_conflicting_anchor_claim(tmp_path):
    """A second session drafting the SAME paragraph in the SAME wave is
    rejected -- real meridian.db.docx_merge DB-backed exclusivity via
    declare_merge_anchors, not an opaque wave_run_id passthrough."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            session_a = await _mk_session(store._db, "sess-a")
            session_b = await _mk_session(store._db, "sess-b")

            await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "session a's draft edit",
                draft_output_path=str(tmp_path / "draft-a.docx"),
                wave_run_id="wave-1", session_id=session_a,
            )
            with pytest.raises(ValueError, match="could not claim"):
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "session b's conflicting edit",
                    draft_output_path=str(tmp_path / "draft-b.docx"),
                    wave_run_id="wave-1", session_id=session_b,
                )
        finally:
            await store.close()

    asyncio.run(_run())


def test_merge_paragraph_draft_promotes_draft_into_canonical(tmp_path):
    """End-to-end: draft-write via update_paragraph, then a REAL merge via
    merge_paragraph_draft -- exercising claim_merge_owner,
    check_merge_stale_or_overlap, and record_merge_result for real."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "canonical.docx"))
        draft_path = str(tmp_path / "draft-session-a.docx")
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            session_a = await _mk_session(store._db, "sess-a")

            draft_result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "merged into canonical",
                draft_output_path=draft_path, wave_run_id="wave-1", session_id=session_a,
            )
            assert draft_result["is_draft"] is True
            assert "merged into canonical" not in _read_document_xml(docx_path).decode()

            owner = await db_module.claim_merge_owner(store._db, "wave-1", docx_path, session_a)
            assert owner["claimed"] is True

            merge_result = await store.merge_paragraph_draft(
                "proj-1", docx_path, "AAAA0002", draft_path, "wave-1", session_a,
            )
            assert merge_result["new_text"] == "merged into canonical"
            assert merge_result["source_path"] == docx_path
            assert merge_result["draft_path"] == draft_path
            assert merge_result["merge_result"]["recorded"] is True
            assert isinstance(merge_result["pre_counts"], dict)
            assert isinstance(merge_result["post_counts"], dict)

            # Canonical now genuinely holds the merged edit.
            assert "merged into canonical" in _read_document_xml(docx_path).decode()
            # The doc_elements index (which tracks canonical) DID resync now.
            structure = await store.get_structure("proj-1", docx_path)
            assert structure is not None

            # A second merge of the SAME anchor by the SAME session is
            # idempotent (record_merge_result's own contract).
            merge_result_2 = await store.merge_paragraph_draft(
                "proj-1", docx_path, "AAAA0002", draft_path, "wave-1", session_a,
            )
            assert merge_result_2["merge_result"]["already_merged"] is True
        finally:
            await store.close()

    asyncio.run(_run())


def test_merge_paragraph_draft_blocked_when_caller_is_not_merge_owner(tmp_path):
    """A session that never claimed merge ownership is rejected by the real
    check_merge_stale_or_overlap gate -- source_path is left untouched."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "canonical.docx"))
        draft_path = str(tmp_path / "draft-session-a.docx")
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            session_a = await _mk_session(store._db, "sess-a")

            await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "attempted merge without ownership",
                draft_output_path=draft_path, wave_run_id="wave-1", session_id=session_a,
            )
            original_bytes = open(docx_path, "rb").read()

            with pytest.raises(ValueError, match="not_merge_owner"):
                await store.merge_paragraph_draft(
                    "proj-1", docx_path, "AAAA0002", draft_path, "wave-1", session_a,
                )

            assert open(docx_path, "rb").read() == original_bytes
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# MCP handler wiring (notes_decisions.handle_update_paragraph)
# ---------------------------------------------------------------------------

def _seed_store_via_env(tmp_path, monkeypatch):
    sidecar = str(tmp_path / "mcp_doc_structure.db")
    monkeypatch.setenv("MERIDIAN_DOC_STORE_URL", sidecar)
    doc_store._reset_doc_store_cache()
    return sidecar


def test_mcp_update_paragraph_expected_content_hash_mismatch_returns_error(tmp_path, monkeypatch):
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        docx_path = _write_docx(str(tmp_path / "chapter1.docx"))
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "up-proj-hash")
            pid = proj["id"]
            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.reindex_document(pid, docx_path, source=docx_path)

            res = await mh._dispatch_mcp_tool(
                "update_paragraph",
                {"project_id": pid, "doc": docx_path, "para_id": "AAAA0001",
                 "new_text": "should be rejected",
                 "expected_content_hash": "definitely-wrong"},
                db, str(tmp_path),
            )
            assert "error" in res
            assert "expected_content_hash mismatch" in res["error"]
            assert "should be rejected" not in _read_document_xml(docx_path).decode()
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_mcp_update_paragraph_still_surfaces_pre_and_post_counts(tmp_path, monkeypatch):
    """Part D through the real MCP dispatch path."""
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        docx_path = _write_docx(str(tmp_path / "chapter1.docx"))
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "up-proj-manifest")
            pid = proj["id"]
            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.reindex_document(pid, docx_path, source=docx_path)

            res = await mh._dispatch_mcp_tool(
                "update_paragraph",
                {"project_id": pid, "doc": docx_path, "para_id": "AAAA0002",
                 "new_text": "via MCP with manifest"},
                db, str(tmp_path),
            )
            assert "error" not in res, res
            assert res["pre_counts"]["relationship_count"] == 1
            assert res["post_counts"]["relationship_count"] == 1
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())
