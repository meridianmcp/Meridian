"""``DocStructureStore``'s DOCX promotion-evidence wiring (ba0af0a4, DOCS-R2-B).

Covers what tests/test_docx_promotion_evidence.py deliberately does NOT
(pure-function coverage of ``meridian.fallbacks.check_docx_promotion_evidence``
in isolation) -- the actual doc_store.py integration:

* ``promoted_sha256`` is now unconditionally surfaced on a successful
  ``update_paragraph`` / ``merge_paragraph_draft`` result (previously
  computed, silently discarded).
* ``update_paragraph``'s existing ``check_render=True`` path now ALSO runs
  the unified promotion-evidence gate, and a genuine hash contradiction
  (the promoted file's current on-disk bytes no longer match what THIS
  writer just promoted) raises the NEW ``DocxPromotionEvidenceError`` --
  even when render status itself reports "rendered" -- something nothing
  checked before this item.
* ``update_paragraph``'s PRE-EXISTING render-degraded contract (FAILED /
  UNAVAILABLE_WITH_REASON, with/without ``allow_degraded_render``) is
  UNCHANGED -- this is the regression-compatibility half of this file.
* ``merge_paragraph_draft`` gets the SAME opt-in ``check_render`` /
  ``allow_degraded_render`` / ``degraded_render_reason`` parameters,
  entirely new (this method had ZERO evidence checking before this item):
  compatibility contract (omitted -> byte-identical), happy path,
  render-degraded fails closed, allow_degraded_render override, and the
  new promotion-evidence contradiction path.

Fixtures mirror tests/test_docx_word_com_regression.py and
tests/test_5988a5bb_update_paragraph_envelope.py exactly (same synthetic
.docx, same store/session helpers) so this file stays a drop-in sibling of
both rather than inventing a third fixture convention.
"""
from __future__ import annotations

import asyncio
import zipfile

import pytest

from meridian import doc_store
from meridian import fallbacks
from meridian import db as db_module


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
    proj = await db_module.create_project(db, name=f"proj-{name}")
    sess = await db_module.register_session(db, project_id=proj["id"], name=name)
    return sess["id"]


async def _claim_merge_owner(db, wave_id: str, docx_path: str, session_id: str) -> None:
    owner = await db_module.claim_merge_owner(db, wave_id, docx_path, session_id)
    assert owner["claimed"] is True, owner


def _rendered(**overrides) -> dict:
    result = {"status": fallbacks.RENDERED, "backend": "fake-backend", "detail": {}}
    result.update(overrides)
    return result


def _failed(**overrides) -> dict:
    result = {"status": fallbacks.FAILED, "reason": "fake render failure", "detail": {}}
    result.update(overrides)
    return result


# ---------------------------------------------------------------------------
# promoted_sha256 surfacing -- unconditional, both write paths.
# ---------------------------------------------------------------------------


def test_update_paragraph_surfaces_promoted_sha256_unconditionally(tmp_path):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "a genuinely correct edit",
            )
            assert isinstance(result["promoted_sha256"], str) and result["promoted_sha256"]
            assert result["promoted_sha256"] == doc_store._docx_file_sha256(docx_path)
            # check_render was never requested -- no promotion_evidence key.
            assert "promotion_evidence" not in result
        finally:
            await store.close()

    asyncio.run(_run())


def test_merge_paragraph_draft_surfaces_promoted_sha256_unconditionally(tmp_path):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "canonical.docx"))
        draft_path = str(tmp_path / "draft-a.docx")
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            session_a = await _mk_session(store._db, "sess-a")
            await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "merged into canonical",
                draft_output_path=draft_path, wave_run_id="wave-1", session_id=session_a,
            )
            await _claim_merge_owner(store._db, "wave-1", docx_path, session_a)
            result = await store.merge_paragraph_draft(
                "proj-1", docx_path, "AAAA0002", draft_path, "wave-1", session_a,
            )
            assert isinstance(result["promoted_sha256"], str) and result["promoted_sha256"]
            assert result["promoted_sha256"] == doc_store._docx_file_sha256(docx_path)
            assert "promotion_evidence" not in result
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# update_paragraph -- new contradiction coverage, existing contract preserved.
# ---------------------------------------------------------------------------


def test_update_paragraph_rendered_but_hash_contradiction_raises_promotion_evidence_error(
    tmp_path, monkeypatch,
):
    """The genuinely NEW check this item adds: even a "rendered" (clean)
    render status must not paper over a promoted file whose current bytes
    no longer match what THIS writer just promoted. Simulates a different
    writer landing between our own promotion+text-verify and the render
    check, exactly like test_docx_word_com_regression.py's own concurrent-
    writer test, but returning a CLEAN render status this time -- proving
    the new hash-contradiction check fires independently of render status,
    not merely as a side effect of a bad render outcome."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)

            def _inject_concurrent_write_then_render_clean(path):
                _write_docx(
                    path,
                    _DOCUMENT_XML.replace(
                        "The original body sentence.",
                        "a concurrent writer's own payload, landed after ours",
                    ),
                )
                return _rendered()

            monkeypatch.setattr(
                doc_store.fallbacks, "check_render_capability",
                _inject_concurrent_write_then_render_clean,
            )

            with pytest.raises(doc_store.DocxConcurrentWriteConflictError) as excinfo:
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "our own genuinely correct edit",
                    check_render=True,
                )
            assert excinfo.value.manifest.get("restored") is False
            assert excinfo.value.manifest.get("concurrent_write_detected") is True
            evidence = excinfo.value.manifest.get("promotion_evidence")
            assert evidence is not None
            assert evidence["verdict"] == fallbacks.PROMOTION_CONTRADICTORY

            xml = _read_document_xml(docx_path).decode("utf-8")
            assert "a concurrent writer's own payload" in xml
            assert "our own genuinely correct edit" not in xml
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_hash_contradiction_without_concurrent_writer_restores_and_raises(
    tmp_path, monkeypatch,
):
    """A contradiction that IS safe to restore (no genuine concurrent
    writer -- just a fabricated bad canonical_hash) raises
    DocxPromotionEvidenceError, restores from backup, and is NEVER
    downgradable via allow_degraded_render (no such override exists for a
    contradiction)."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            original_bytes = open(docx_path, "rb").read()

            monkeypatch.setattr(
                doc_store.fallbacks, "check_render_capability", lambda path: _rendered(),
            )
            real_evidence_fn = doc_store.fallbacks.check_docx_promotion_evidence

            def _lie_about_canonical_hash(docx_path_, stage_hash, canonical_hash, observed_hash, **kw):
                return real_evidence_fn(docx_path_, stage_hash, "deliberately-wrong-hash", observed_hash, **kw)

            monkeypatch.setattr(
                doc_store.fallbacks, "check_docx_promotion_evidence", _lie_about_canonical_hash,
            )

            with pytest.raises(doc_store.DocxPromotionEvidenceError) as excinfo:
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "this edit must be rejected",
                    check_render=True, allow_degraded_render=True,
                    degraded_render_reason="irrelevant -- contradictions are never downgradable",
                )
            assert excinfo.value.manifest.get("restored") is True
            assert open(docx_path, "rb").read() == original_bytes
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_render_degraded_contract_unchanged_by_this_item(tmp_path, monkeypatch):
    """Regression guard: DOCS-R2-A's pre-existing render-degraded contract
    (FAILED status, no allow_degraded_render -> DocxRenderVerificationError)
    must be completely unaffected by ba0af0a4's new evidence gate, since the
    hashes genuinely match (no concurrent writer, no lied-about hash)."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            original_bytes = open(docx_path, "rb").read()
            monkeypatch.setattr(
                doc_store.fallbacks, "check_render_capability", lambda path: _failed(),
            )

            with pytest.raises(doc_store.DocxRenderVerificationError) as excinfo:
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "this edit must be rejected",
                    check_render=True,
                )
            assert excinfo.value.manifest.get("restored") is True
            assert excinfo.value.manifest.get("promotion_evidence") is None
            assert open(docx_path, "rb").read() == original_bytes
        finally:
            await store.close()

    asyncio.run(_run())


def test_update_paragraph_allow_degraded_render_still_works_with_evidence_gate_present(tmp_path, monkeypatch):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            monkeypatch.setattr(
                doc_store.fallbacks, "check_render_capability", lambda path: _failed(),
            )
            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "accepted despite failed render",
                check_render=True, allow_degraded_render=True,
                degraded_render_reason="no backend in CI",
            )
            assert result["render_degraded"] is True
            assert result["promotion_evidence"]["verdict"] == fallbacks.PROMOTION_DEGRADED
            assert isinstance(result["promoted_sha256"], str) and result["promoted_sha256"]
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# merge_paragraph_draft -- net-new opt-in evidence gate (previously had NONE).
# ---------------------------------------------------------------------------


def test_merge_paragraph_draft_omitting_check_render_never_calls_render_gate(tmp_path, monkeypatch):
    """Compatibility contract for merge_paragraph_draft's brand-new opt-in
    parameters, mirroring update_paragraph's own compatibility test:
    existing wave-merge callers (which never pass check_render) must see
    byte-identical behavior."""
    def _must_not_be_called(path):
        raise AssertionError("check_render_capability must never be called when check_render=False")

    async def _run():
        docx_path = _write_docx(str(tmp_path / "canonical.docx"))
        draft_path = str(tmp_path / "draft-a.docx")
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            session_a = await _mk_session(store._db, "sess-a")
            await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "merged into canonical",
                draft_output_path=draft_path, wave_run_id="wave-1", session_id=session_a,
            )
            monkeypatch.setattr(doc_store.fallbacks, "check_render_capability", _must_not_be_called)

            await _claim_merge_owner(store._db, "wave-1", docx_path, session_a)
            result = await store.merge_paragraph_draft(
                "proj-1", docx_path, "AAAA0002", draft_path, "wave-1", session_a,
            )
            assert "render_status" not in result
            assert "promotion_evidence" not in result
        finally:
            await store.close()

    asyncio.run(_run())


def test_merge_paragraph_draft_check_render_happy_path(tmp_path, monkeypatch):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "canonical.docx"))
        draft_path = str(tmp_path / "draft-a.docx")
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            session_a = await _mk_session(store._db, "sess-a")
            await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "merged into canonical",
                draft_output_path=draft_path, wave_run_id="wave-1", session_id=session_a,
            )
            monkeypatch.setattr(
                doc_store.fallbacks, "check_render_capability", lambda path: _rendered(),
            )
            await _claim_merge_owner(store._db, "wave-1", docx_path, session_a)
            result = await store.merge_paragraph_draft(
                "proj-1", docx_path, "AAAA0002", draft_path, "wave-1", session_a,
                check_render=True,
            )
            assert result["render_status"] == fallbacks.RENDERED
            assert result["render_verified"] is True
            assert result["promotion_evidence"]["verdict"] == fallbacks.PROMOTION_VERIFIED
            assert result["promoted_sha256"] == doc_store._docx_file_sha256(docx_path)
        finally:
            await store.close()

    asyncio.run(_run())


def test_merge_paragraph_draft_check_render_failed_fails_closed_and_restores(tmp_path, monkeypatch):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "canonical.docx"))
        draft_path = str(tmp_path / "draft-a.docx")
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            session_a = await _mk_session(store._db, "sess-a")
            await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "merged into canonical",
                draft_output_path=draft_path, wave_run_id="wave-1", session_id=session_a,
            )
            canonical_before_second_merge = open(docx_path, "rb").read()

            # A second draft write + merge attempt, this time gated with a
            # failing render check.
            await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "second edit, should be rejected",
                draft_output_path=draft_path, wave_run_id="wave-2", session_id=session_a,
            )
            monkeypatch.setattr(
                doc_store.fallbacks, "check_render_capability", lambda path: _failed(),
            )
            await _claim_merge_owner(store._db, "wave-2", docx_path, session_a)
            with pytest.raises(doc_store.DocxRenderVerificationError) as excinfo:
                await store.merge_paragraph_draft(
                    "proj-1", docx_path, "AAAA0002", draft_path, "wave-2", session_a,
                    check_render=True,
                )
            assert excinfo.value.manifest.get("restored") is True
            assert open(docx_path, "rb").read() == canonical_before_second_merge
        finally:
            await store.close()

    asyncio.run(_run())


def test_merge_paragraph_draft_allow_degraded_render_accepts_with_reason(tmp_path, monkeypatch):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "canonical.docx"))
        draft_path = str(tmp_path / "draft-a.docx")
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            session_a = await _mk_session(store._db, "sess-a")
            await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "merged into canonical",
                draft_output_path=draft_path, wave_run_id="wave-1", session_id=session_a,
            )
            monkeypatch.setattr(
                doc_store.fallbacks, "check_render_capability", lambda path: _failed(),
            )
            await _claim_merge_owner(store._db, "wave-1", docx_path, session_a)
            result = await store.merge_paragraph_draft(
                "proj-1", docx_path, "AAAA0002", draft_path, "wave-1", session_a,
                check_render=True, allow_degraded_render=True,
                degraded_render_reason="no backend in CI",
            )
            assert result["render_degraded"] is True
            assert result["degraded_render_reason"] == "no backend in CI"
            assert result["promotion_evidence"]["verdict"] == fallbacks.PROMOTION_DEGRADED
        finally:
            await store.close()

    asyncio.run(_run())


def test_merge_paragraph_draft_allow_degraded_render_without_reason_raises_before_any_mutation(tmp_path):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "canonical.docx"))
        draft_path = str(tmp_path / "draft-a.docx")
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            session_a = await _mk_session(store._db, "sess-a")
            await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "merged into canonical",
                draft_output_path=draft_path, wave_run_id="wave-1", session_id=session_a,
            )
            original_bytes = open(docx_path, "rb").read()
            with pytest.raises(ValueError, match="degraded_render_reason"):
                await store.merge_paragraph_draft(
                    "proj-1", docx_path, "AAAA0002", draft_path, "wave-1", session_a,
                    check_render=True, allow_degraded_render=True,
                )
            # Nothing was touched -- the ValueError fires before any read/write.
            assert open(docx_path, "rb").read() == original_bytes
        finally:
            await store.close()

    asyncio.run(_run())


def test_merge_paragraph_draft_hash_contradiction_raises_promotion_evidence_error(tmp_path, monkeypatch):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "canonical.docx"))
        draft_path = str(tmp_path / "draft-a.docx")
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            session_a = await _mk_session(store._db, "sess-a")
            await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "merged into canonical",
                draft_output_path=draft_path, wave_run_id="wave-1", session_id=session_a,
            )
            original_bytes = open(docx_path, "rb").read()

            monkeypatch.setattr(
                doc_store.fallbacks, "check_render_capability", lambda path: _rendered(),
            )
            real_evidence_fn = doc_store.fallbacks.check_docx_promotion_evidence

            def _lie_about_canonical_hash(docx_path_, stage_hash, canonical_hash, observed_hash, **kw):
                return real_evidence_fn(docx_path_, stage_hash, "deliberately-wrong-hash", observed_hash, **kw)

            monkeypatch.setattr(
                doc_store.fallbacks, "check_docx_promotion_evidence", _lie_about_canonical_hash,
            )

            await _claim_merge_owner(store._db, "wave-1", docx_path, session_a)
            with pytest.raises(doc_store.DocxPromotionEvidenceError) as excinfo:
                await store.merge_paragraph_draft(
                    "proj-1", docx_path, "AAAA0002", draft_path, "wave-1", session_a,
                    check_render=True,
                )
            assert excinfo.value.manifest.get("restored") is True
            assert open(docx_path, "rb").read() == original_bytes
        finally:
            await store.close()

    asyncio.run(_run())
