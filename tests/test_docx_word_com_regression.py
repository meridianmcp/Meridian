"""``DocStructureStore.update_paragraph``'s render-gate integration (8d2ef784,
DOCS-R2-A).

Covers the opt-in ``check_render`` / ``allow_degraded_render`` /
``degraded_render_reason`` parameters wired into ``update_paragraph``
(``meridian/doc_store.py``), gating a promoted paragraph write on
``meridian.fallbacks.check_render_capability``'s tri-state result:

* the compatibility contract -- omitting all three parameters (the existing
  50+ call sites' behavior) must be BYTE-IDENTICAL to before this change:
  ``fallbacks.check_render_capability`` is never even called;
* ``check_render=True`` happy path: a "rendered" result is stamped onto the
  return value (``render_status`` / ``render_verified`` / ``render_backend``);
* ``check_render=True`` "failed" / "unavailable-with-reason" fails closed by
  default: :class:`meridian.doc_store.DocxRenderVerificationError`, with a
  best-effort restore from the ``.bak`` backup;
* ``allow_degraded_render=True`` (with a non-empty ``degraded_render_reason``)
  is the one audited override: the write stands, but ``render_verified``
  stays ``False`` and ``render_degraded``/``degraded_render_reason`` are
  stamped onto the result;
* ``allow_degraded_render=True`` with no reason raises ``ValueError`` BEFORE
  anything is read or mutated, regardless of ``check_render``;
* draft-mode writes are render-gated against the DRAFT file, never the
  canonical source;
* a genuine concurrent writer landing between this write's own promotion and
  its render-check raises :class:`DocxConcurrentWriteConflictError` instead
  of a render-verification error, mirroring the existing text-verification
  compare-and-swap safety check exactly (see
  ``tests/test_5988a5bb_update_paragraph_envelope.py``, whose fixtures this
  suite reuses).

The original 8d2ef784 orphaned commit (``e5045b3f``, Aug 9 2026, never
merged) found and "fixed" a missing ``pythoncom.CoInitialize()`` call in a
threaded Word-COM render implementation. Current dev's real
``extensions/meridian-docs/meridian_docs/render_gate.py`` already calls
``CoInitialize``/``CoUninitialize`` correctly in both its threaded and
isolated-process code paths -- that bug is not live in current dev, and
``meridian/fallbacks`` doesn't reimplement Word-COM automation at all (it
delegates -- see ``meridian/fallbacks/__init__.py``'s module docstring), so
there is nothing Word-COM-specific left to regression-test at that layer.
This file's actual job -- matching the module name's original intent -- is
the ``update_paragraph`` integration the orphaned commit's sibling test file
covered.
"""
from __future__ import annotations

import asyncio
import zipfile

import pytest

from meridian import doc_store
from meridian import fallbacks
from meridian import db as db_module


# ---------------------------------------------------------------------------
# Synthetic .docx fixture (mirrors tests/test_5988a5bb_update_paragraph_envelope.py's)
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
    proj = await db_module.create_project(db, name=f"proj-{name}")
    sess = await db_module.register_session(db, project_id=proj["id"], name=name)
    return sess["id"]


def _rendered(**overrides) -> dict:
    result = {"status": fallbacks.RENDERED, "backend": "fake-backend", "detail": {}}
    result.update(overrides)
    return result


def _failed(**overrides) -> dict:
    result = {"status": fallbacks.FAILED, "reason": "fake render failure", "detail": {}}
    result.update(overrides)
    return result


def _unavailable(**overrides) -> dict:
    result = {"status": fallbacks.UNAVAILABLE_WITH_REASON, "reason": "no backend in this env"}
    result.update(overrides)
    return result


# ---------------------------------------------------------------------------
# Compatibility contract -- omitting all three params is byte-identical.
# ---------------------------------------------------------------------------


def test_omitting_check_render_never_calls_the_render_gate_at_all(tmp_path, monkeypatch):
    """The single most load-bearing guarantee of this change: update_paragraph
    has 50+ pre-existing callers that must see IDENTICAL behavior when they
    don't know this feature exists."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)

            def _must_not_be_called(*args, **kwargs):
                raise AssertionError(
                    "fallbacks.check_render_capability must never be called "
                    "when check_render is omitted"
                )

            monkeypatch.setattr(doc_store.fallbacks, "check_render_capability", _must_not_be_called)

            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "a perfectly ordinary edit",
            )

            assert "render_status" not in result
            assert "render_verified" not in result
            assert "render_backend" not in result
            assert "render_degraded" not in result
            assert "degraded_render_reason" not in result
            assert result["new_text"] == "a perfectly ordinary edit"
        finally:
            await store.close()

    asyncio.run(_run())


def test_check_render_false_explicit_also_never_calls_the_render_gate(tmp_path, monkeypatch):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            monkeypatch.setattr(
                doc_store.fallbacks, "check_render_capability",
                lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")),
            )

            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "still ordinary", check_render=False,
            )

            assert "render_status" not in result
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# check_render=True -- happy path.
# ---------------------------------------------------------------------------


def test_check_render_true_rendered_stamps_verified_evidence(tmp_path, monkeypatch):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            seen_paths: list[str] = []

            def _fake_check(path):
                seen_paths.append(path)
                return _rendered(backend="libreoffice-soffice")

            monkeypatch.setattr(doc_store.fallbacks, "check_render_capability", _fake_check)

            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "a rendered edit", check_render=True,
            )

            assert result["render_status"] == fallbacks.RENDERED
            assert result["render_verified"] is True
            assert result["render_backend"] == "libreoffice-soffice"
            assert "render_degraded" not in result
            assert seen_paths == [docx_path], "render check must run against the promoted write_dest"
            assert _read_document_xml(docx_path).decode() .find("a rendered edit") != -1
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# check_render=True -- fails closed by default (failed / unavailable).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fake_result_factory", [_failed, _unavailable])
def test_check_render_true_fails_closed_and_restores_backup(tmp_path, monkeypatch, fake_result_factory):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            original_bytes = open(docx_path, "rb").read()

            monkeypatch.setattr(
                doc_store.fallbacks, "check_render_capability",
                lambda path: fake_result_factory(),
            )

            with pytest.raises(doc_store.DocxRenderVerificationError) as excinfo:
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "this edit must be rejected",
                    check_render=True,
                )

            assert excinfo.value.manifest.get("restored") is True
            assert excinfo.value.manifest["render_check"]["status"] in (
                fallbacks.FAILED, fallbacks.UNAVAILABLE_WITH_REASON,
            )
            # Genuinely restored -- byte-for-byte back to the pre-write file,
            # exactly like the text-verification restore path.
            assert open(docx_path, "rb").read() == original_bytes
            xml = _read_document_xml(docx_path).decode("utf-8")
            assert "this edit must be rejected" not in xml
            assert "The original body sentence." in xml
        finally:
            await store.close()

    asyncio.run(_run())


def test_check_render_true_failed_reason_is_surfaced_in_the_error_message(tmp_path, monkeypatch):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            monkeypatch.setattr(
                doc_store.fallbacks, "check_render_capability",
                lambda path: _failed(reason="soffice exited with code 1: corrupt zip"),
            )

            with pytest.raises(doc_store.DocxRenderVerificationError) as excinfo:
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "rejected edit", check_render=True,
                )

            assert "corrupt zip" in str(excinfo.value)
            assert "AAAA0002" in str(excinfo.value)
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# allow_degraded_render -- the one audited override.
# ---------------------------------------------------------------------------


def test_allow_degraded_render_accepts_the_write_with_reason_stamped(tmp_path, monkeypatch):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            monkeypatch.setattr(
                doc_store.fallbacks, "check_render_capability",
                lambda path: _unavailable(reason="no render backend on this CI runner"),
            )

            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "accepted despite no render check",
                check_render=True,
                allow_degraded_render=True,
                degraded_render_reason="CI runner has no LibreOffice/Word installed",
            )

            assert result["render_status"] == fallbacks.UNAVAILABLE_WITH_REASON
            assert result["render_verified"] is False
            assert result["render_degraded"] is True
            assert result["degraded_render_reason"] == "CI runner has no LibreOffice/Word installed"
            # The write genuinely stands -- this is the whole point of the override.
            xml = _read_document_xml(docx_path).decode("utf-8")
            assert "accepted despite no render check" in xml
        finally:
            await store.close()

    asyncio.run(_run())


def test_allow_degraded_render_without_reason_raises_before_any_mutation(tmp_path, monkeypatch):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            original_bytes = open(docx_path, "rb").read()
            monkeypatch.setattr(
                doc_store.fallbacks, "check_render_capability",
                lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("must fail validation before ever reading the docx")
                ),
            )

            with pytest.raises(ValueError, match="degraded_render_reason"):
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "should never land",
                    allow_degraded_render=True,
                )

            assert open(docx_path, "rb").read() == original_bytes
        finally:
            await store.close()

    asyncio.run(_run())


def test_allow_degraded_render_with_blank_reason_also_raises(tmp_path):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)

            with pytest.raises(ValueError, match="degraded_render_reason"):
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "should never land",
                    check_render=True,
                    allow_degraded_render=True,
                    degraded_render_reason="   ",
                )
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Draft mode -- render-gated against the DRAFT file, not the canonical source.
# ---------------------------------------------------------------------------


def test_check_render_true_in_draft_mode_checks_the_draft_not_canonical(tmp_path, monkeypatch):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "canonical.docx"))
        draft_path = str(tmp_path / "draft-session-a.docx")
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            session_a = await _mk_session(store._db, "sess-a")
            canonical_bytes = open(docx_path, "rb").read()
            seen_paths: list[str] = []

            monkeypatch.setattr(
                doc_store.fallbacks, "check_render_capability",
                lambda path: seen_paths.append(path) or _rendered(),
            )

            result = await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "draft content",
                draft_output_path=draft_path, wave_run_id="wave-1", session_id=session_a,
                check_render=True,
            )

            assert seen_paths == [draft_path]
            assert result["render_status"] == fallbacks.RENDERED
            assert result["is_draft"] is True
            # Canonical file is completely untouched by a draft-mode write,
            # render-gated or not.
            assert open(docx_path, "rb").read() == canonical_bytes
        finally:
            await store.close()

    asyncio.run(_run())


def test_check_render_true_in_draft_mode_fails_closed_restores_draft_backup(tmp_path, monkeypatch):
    async def _run():
        docx_path = _write_docx(str(tmp_path / "canonical.docx"))
        draft_path = str(tmp_path / "draft-session-a.docx")
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            session_a = await _mk_session(store._db, "sess-a")

            # Baseline: a genuinely correct first draft write (establishes a
            # .bak once the SECOND write below promotes over it).
            await store.update_paragraph(
                "proj-1", docx_path, "AAAA0002", "initial draft content",
                draft_output_path=draft_path, wave_run_id="wave-1", session_id=session_a,
            )
            first_draft_bytes = open(draft_path, "rb").read()

            monkeypatch.setattr(
                doc_store.fallbacks, "check_render_capability",
                lambda path: _failed(reason="draft render broke"),
            )

            with pytest.raises(doc_store.DocxRenderVerificationError) as excinfo:
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "second draft edit rejected",
                    draft_output_path=draft_path, wave_run_id="wave-1", session_id=session_a,
                    check_render=True,
                )

            assert excinfo.value.manifest.get("restored") is True
            assert open(draft_path, "rb").read() == first_draft_bytes
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Concurrent-writer race during the render-check window.
# ---------------------------------------------------------------------------


def test_concurrent_writer_during_render_check_raises_conflict_not_render_error(tmp_path, monkeypatch):
    """Mirrors tests/test_5988a5bb_update_paragraph_envelope.py's own
    concurrent-write test for TEXT verification, one layer up: a different
    writer's promotion lands on write_dest in the window between OUR
    promotion (already complete) and OUR render-check. The resulting
    compare-and-swap mismatch must raise DocxConcurrentWriteConflictError,
    NEVER DocxRenderVerificationError -- restoring from our own backup would
    destroy that other writer's already-promoted work.
    """
    async def _run():
        docx_path = _write_docx(str(tmp_path / "doc.docx"))
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)

            def _inject_concurrent_write_then_fail(path):
                # A DIFFERENT writer's promotion lands here, AFTER our own
                # promotion+text-verify already succeeded but BEFORE our own
                # render-check reads/reports on the file.
                _write_docx(
                    path,
                    _DOCUMENT_XML.replace(
                        "The original body sentence.",
                        "a concurrent writer's own payload, landed after ours",
                    ),
                )
                return _failed(reason="render check observes a file we no longer own")

            monkeypatch.setattr(
                doc_store.fallbacks, "check_render_capability", _inject_concurrent_write_then_fail,
            )

            with pytest.raises(doc_store.DocxConcurrentWriteConflictError) as excinfo:
                await store.update_paragraph(
                    "proj-1", docx_path, "AAAA0002", "our own genuinely correct edit",
                    check_render=True,
                )

            assert excinfo.value.manifest.get("restored") is False
            assert excinfo.value.manifest.get("concurrent_write_detected") is True

            # Left EXACTLY as the "other writer" left it.
            xml = _read_document_xml(docx_path).decode("utf-8")
            assert "a concurrent writer's own payload" in xml
            assert "our own genuinely correct edit" not in xml
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# DocxRenderVerificationError -- basic exception-shape contract.
# ---------------------------------------------------------------------------


def test_docx_render_verification_error_is_an_os_error_with_manifest():
    exc = doc_store.DocxRenderVerificationError("boom", manifest={"restored": True})
    assert isinstance(exc, OSError)
    assert exc.manifest == {"restored": True}
    assert str(exc) == "boom"


def test_docx_render_verification_error_defaults_manifest_to_empty_dict():
    exc = doc_store.DocxRenderVerificationError("boom")
    assert exc.manifest == {}
