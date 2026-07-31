"""Coverage for the ID-addressable docx WRITE tool ``update_paragraph`` (f978e588).

Exercises:

* the pure docx-write helpers (_load_docx_xml, _find_paragraph_by_id,
  _normalize_runs, _set_paragraph_runs, _save_docx_xml) on a synthetic .docx,
* DocStructureStore.update_paragraph end-to-end on a local SQLite sidecar —
  rewriting a real paragraph in the on-disk .docx, addressed ONLY by its
  w14:paraId, and resyncing the matching doc_elements row,
* string input AND runs-list input (with basic bold/italic run formatting),
* the error surfaces (unknown doc, missing source path, unknown para_id),
* the tool through the real _dispatch_mcp_tool MCP path,
* dccc2311 — the hardened disposable-worker-artifact write transaction
  underneath _save_docx_xml: structural manifests, manifest-hash determinism,
  fail-closed verification (count mismatch -> rollback, original untouched),
  and single-serialized-promotion-point concurrency.
"""
from __future__ import annotations

import asyncio
import io
import os
import threading
import time
import zipfile

import pytest
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


# ---------------------------------------------------------------------------
# dccc2311 — hardened DOCX transaction manifests + fail-closed verification
#
# _save_docx_xml now routes through _write_docx_transaction: a disposable
# staged artifact (never the live file directly), a structural manifest
# (media/style/equation/relationship counts) compared pre- vs post-write, a
# deterministic manifest hash of what actually changed, fail-closed
# verification (a protected-count mismatch never promotes -- the original
# is left byte-for-byte untouched), and a single serialized promotion point
# per destination so concurrent writers never interleave.
# ---------------------------------------------------------------------------


def _build_transaction_payload(docx_path: str, new_text: str):
    """Load, edit, and repackage a .docx WITHOUT saving — returns
    (raw, payload_bytes, changed_parts) for direct _write_docx_transaction calls."""
    raw, root = doc_store._load_docx_xml(docx_path)
    p = doc_store._find_paragraph_by_id(root, "AAAA0002")
    doc_store._set_paragraph_runs(p, [{"text": new_text}])
    new_document = LET.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == doc_store._DOCX_DOCUMENT_PART:
                    data = new_document
                dst.writestr(info, data)
    return raw, out.getvalue(), {doc_store._DOCX_DOCUMENT_PART: new_document}


def test_docx_structural_manifest_counts_media_style_equation_relationship(tmp_path):
    """The structural manifest reports all four families accurately, and is
    resilient to a .docx missing some of the optional parts entirely."""
    docx = _write_docx(str(tmp_path / "d.docx"))
    raw, _root = doc_store._load_docx_xml(docx)
    manifest = doc_store._docx_structural_manifest(raw)
    assert manifest == {
        "media_count": 0,
        "style_count": 0,
        "equation_count": 0,
        "relationship_count": 1,  # the fixture's _rels/.rels has one Relationship
    }


def test_write_docx_transaction_count_mismatch_triggers_rollback(tmp_path):
    """dccc2311 — a protected-key pre/post structural-count mismatch is
    fail-closed: DocxWriteVerificationError is raised (the write is REJECTED
    / rolled back) instead of promoting a corrupted-looking artifact."""
    docx = _write_docx(str(tmp_path / "d.docx"))
    raw, payload, changed_parts = _build_transaction_payload(docx, "Edited body.")

    # A deliberately WRONG pre_manifest (claims one MORE style than the real
    # pre-write count) makes the real post-write count mismatch even though
    # the payload itself is perfectly well-formed.
    real_pre = doc_store._docx_structural_manifest(raw)
    bad_pre = dict(real_pre)
    bad_pre["style_count"] = real_pre["style_count"] + 1

    with pytest.raises(doc_store.DocxWriteVerificationError) as excinfo:
        doc_store._write_docx_transaction(
            payload, docx,
            pre_manifest=bad_pre,
            protected_keys=("media_count", "style_count", "relationship_count"),
            changed_parts=changed_parts,
        )
    mismatches = excinfo.value.manifest["count_mismatches"]
    assert mismatches["style_count"] == {
        "expected": bad_pre["style_count"], "actual": real_pre["style_count"],
    }
    # relationship/media counts, which DID match, are not reported as mismatches.
    assert "relationship_count" not in mismatches
    assert "media_count" not in mismatches


def test_write_docx_transaction_verification_failure_leaves_original_untouched(tmp_path):
    """dccc2311 — a failed verification never promotes: the destination file
    is byte-for-byte identical to before the call, and no .bak is created
    (promotion, and therefore backup, never happens)."""
    docx = _write_docx(str(tmp_path / "d.docx"))
    original_bytes = open(docx, "rb").read()
    raw, payload, changed_parts = _build_transaction_payload(docx, "Edited body.")

    bad_pre = dict(doc_store._docx_structural_manifest(raw))
    bad_pre["relationship_count"] += 5

    with pytest.raises(doc_store.DocxWriteVerificationError):
        doc_store._write_docx_transaction(
            payload, docx,
            pre_manifest=bad_pre,
            protected_keys=("media_count", "style_count", "relationship_count"),
            changed_parts=changed_parts,
        )

    assert open(docx, "rb").read() == original_bytes
    assert not os.path.exists(docx + ".bak")
    # No leaked staged temp artifacts either.
    leftovers = [n for n in os.listdir(tmp_path) if ".meridian-docx-stage-" in n]
    assert leftovers == []


def test_write_docx_transaction_succeeds_when_protected_counts_match(tmp_path):
    """The counterpart happy path: a correct pre_manifest promotes normally
    and returns the expected transaction manifest shape."""
    docx = _write_docx(str(tmp_path / "d.docx"))
    raw, payload, changed_parts = _build_transaction_payload(docx, "A clean edit.")
    pre_manifest = doc_store._docx_structural_manifest(raw)

    txn = doc_store._write_docx_transaction(
        payload, docx,
        pre_manifest=pre_manifest,
        protected_keys=("media_count", "style_count", "relationship_count"),
        changed_parts=changed_parts,
    )
    assert txn["pre_counts"] == pre_manifest
    assert txn["post_counts"]["relationship_count"] == pre_manifest["relationship_count"]
    assert isinstance(txn["manifest_hash"], str) and len(txn["manifest_hash"]) == 64
    assert b"A clean edit." in _read_document_xml(docx)


def test_docx_manifest_hash_deterministic_for_identical_input():
    """dccc2311 — the manifest hash is a pure function of ``changed_parts``:
    identical input always yields an identical hash, key ORDER never
    matters, and a genuinely different transaction hashes differently."""
    parts_a = {
        "word/document.xml": b"<w:document>hello</w:document>",
        "word/styles.xml": b"<styles/>",
    }
    h1 = doc_store._docx_manifest_hash(parts_a)
    h2 = doc_store._docx_manifest_hash(dict(parts_a))  # fresh dict, same content
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex digest

    # Re-inserting the SAME keys in a different order must not change the hash.
    reordered = {
        "word/styles.xml": parts_a["word/styles.xml"],
        "word/document.xml": parts_a["word/document.xml"],
    }
    assert doc_store._docx_manifest_hash(reordered) == h1

    # A genuinely different transaction hashes differently.
    changed = dict(parts_a)
    changed["word/document.xml"] = b"<w:document>different</w:document>"
    assert doc_store._docx_manifest_hash(changed) != h1

    # An entirely different part set also hashes differently.
    assert doc_store._docx_manifest_hash({"word/document.xml": parts_a["word/document.xml"]}) != h1


def test_save_docx_xml_concurrent_writes_serialize_promotion(tmp_path, monkeypatch):
    """dccc2311 — concurrent writers targeting the SAME .docx never
    interleave their promotion step. Verified by instrumenting the REAL
    os.replace call inside the write pipeline and asserting the recorded
    enter/exit windows never overlap -- proving actual serialization, not
    merely the absence of an exception (which luck alone could produce)."""
    docx = _write_docx(str(tmp_path / "d.docx"))

    intervals: list[tuple[float, float]] = []
    intervals_lock = threading.Lock()
    real_replace = os.replace

    def instrumented_replace(src, dst):
        start = time.monotonic()
        time.sleep(0.02)  # widen the promotion window so a race would show up
        real_replace(src, dst)
        end = time.monotonic()
        with intervals_lock:
            intervals.append((start, end))

    monkeypatch.setattr(doc_store.os, "replace", instrumented_replace)

    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            raw, root = doc_store._load_docx_xml(docx)
            p = doc_store._find_paragraph_by_id(root, "AAAA0002")
            doc_store._set_paragraph_runs(p, [{"text": f"concurrent edit {n}"}])
            doc_store._save_docx_xml(raw, root, docx)
        except BaseException as exc:  # noqa: BLE001 — surfaced via assertion below
            errors.append(exc)

    n_threads = 6
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(intervals) == n_threads
    intervals.sort()
    for (_start1, end1), (start2, _end2) in zip(intervals, intervals[1:]):
        assert end1 <= start2, "two promotions overlapped -- serialization failed"

    # The file itself is left in a perfectly valid, single-writer-won state
    # (whichever thread promoted last), never a corrupted/interleaved mix.
    _raw, root = doc_store._load_docx_xml(docx)
    assert doc_store._find_paragraph_by_id(root, "AAAA0002") is not None


def test_docx_promotion_lock_distinct_destinations_do_not_block_each_other():
    """Locks are keyed per destination path -- writers to DIFFERENT files
    never serialize against each other."""
    lock_a = doc_store._docx_promotion_lock("/tmp/one.docx")
    lock_b = doc_store._docx_promotion_lock("/tmp/two.docx")
    assert lock_a is not lock_b
    # The SAME path (even relative vs normalized) always returns the SAME lock.
    lock_a_again = doc_store._docx_promotion_lock("/tmp/one.docx")
    assert lock_a is lock_a_again


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


# ---------------------------------------------------------------------------
# 827b6bdc — duplicated native w14:paraId: fail-closed writes + explicit
# repair. Word's own invariant is that w14:paraId is unique per paragraph,
# but nothing enforced that on read; first-match-wins addressing on a WRITE
# path would silently edit whichever duplicate the tree walk reached first.
# ---------------------------------------------------------------------------

_DUPLICATE_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="AAAA0001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="6BDC5378">
      <w:r><w:t>First paragraph, original owner of 6BDC5378.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="AAAA0002">
      <w:r><w:t>An unrelated, uniquely-identified paragraph in between.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="6BDC5378">
      <w:r><w:t>Second, unrelated paragraph that WRONGLY shares 6BDC5378.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def test_find_paragraph_by_id_raises_on_duplicate_native_para_id(tmp_path):
    """827b6bdc regression: the reference case this item names -- two
    distinct paragraphs both carrying w14:paraId="6BDC5378" (e.g. a real
    hand-edited/merged .docx). _find_paragraph_by_id must fail closed
    instead of silently returning the first match."""
    docx = _write_docx(str(tmp_path / "dup.docx"), _DUPLICATE_DOCUMENT_XML)
    _raw, root = doc_store._load_docx_xml(docx)

    with pytest.raises(doc_store.AmbiguousParagraphIdError) as exc_info:
        doc_store._find_paragraph_by_id(root, "6BDC5378")

    exc = exc_info.value
    assert "6BDC5378" in str(exc)
    assert exc.para_id == "6BDC5378"
    assert len(exc.matches) == 2
    # Enough location info to actually find the problem: body-order index
    # (1 and 3, since the duplicated id is the 2nd and 4th <w:p>) plus a text
    # snippet distinguishing the two, not just "duplicate found".
    assert [m["index"] for m in exc.matches] == [1, 3]
    assert "First paragraph" in exc.matches[0]["text"]
    assert "Second, unrelated paragraph" in exc.matches[1]["text"]

    # A non-duplicated id in the SAME document still resolves normally --
    # only the actually-ambiguous id fails closed.
    unique = doc_store._find_paragraph_by_id(root, "AAAA0002")
    assert unique is not None
    assert doc_store._paragraph_plain_text(unique) == (
        "An unrelated, uniquely-identified paragraph in between."
    )
    assert doc_store._find_paragraph_by_id(root, "NOPE") is None


def test_find_paragraph_with_index_raises_on_duplicate_native_para_id(tmp_path):
    """Same fail-closed contract for the (element, idx) accessor."""
    docx = _write_docx(str(tmp_path / "dup.docx"), _DUPLICATE_DOCUMENT_XML)
    _raw, root = doc_store._load_docx_xml(docx)

    with pytest.raises(doc_store.AmbiguousParagraphIdError):
        doc_store._find_paragraph_with_index(root, "6BDC5378")

    found = doc_store._find_paragraph_with_index(root, "AAAA0002")
    assert found is not None and found[1] == 2


def test_update_paragraph_rejects_write_to_duplicated_para_id(tmp_path):
    """The ambiguous-address-rejection regression: a WRITE attempt against a
    duplicated para_id must be REJECTED -- never silently land on whichever
    of the two matching paragraphs the resolver reached first."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "dup.docx"), _DUPLICATE_DOCUMENT_XML)
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)

            raised = False
            try:
                await store.update_paragraph(
                    "proj-1", docx_path, "6BDC5378", "This must never land.",
                )
            except doc_store.AmbiguousParagraphIdError as exc:
                raised = True
                assert "6BDC5378" in str(exc)
            assert raised, "expected AmbiguousParagraphIdError, write was not rejected"

            # Neither of the two candidate paragraphs was touched -- the
            # file is byte-for-byte the original text on both sides of the
            # ambiguity, not just "one of them happened to survive".
            new_xml = _read_document_xml(docx_path).decode("utf-8")
            assert "First paragraph, original owner of 6BDC5378." in new_xml
            assert "Second, unrelated paragraph that WRONGLY shares 6BDC5378." in new_xml
            assert "This must never land." not in new_xml
        finally:
            await store.close()

    asyncio.run(_run())


def test_insert_equation_rejects_ambiguous_para_id_without_mutating(tmp_path):
    """insert_equation's own {"error": ...} contract is preserved for the
    ambiguous case (never raises out of this call, never mutates)."""
    async def _run():
        docx_path = _write_docx(str(tmp_path / "dup.docx"), _DUPLICATE_DOCUMENT_XML)
        store = await _open_store(tmp_path)
        try:
            await store.reindex_document("proj-1", docx_path, source=docx_path)
            before = _read_document_xml(docx_path)

            result = await store.insert_equation(
                "proj-1", docx_path, "6BDC5378",
                "<m:oMath xmlns:m=\"http://schemas.openxmlformats.org/officeDocument/2006/math\">"
                "<m:r><m:t>x</m:t></m:r></m:oMath>",
            )
            assert "error" in result
            assert "6BDC5378" in result["error"]
            assert _read_document_xml(docx_path) == before
        finally:
            await store.close()

    asyncio.run(_run())


def test_mcp_update_paragraph_rejects_ambiguous_para_id(tmp_path, monkeypatch):
    """Through the real MCP dispatch path: AmbiguousParagraphIdError is a
    ValueError subclass, so the existing `except ValueError` in
    handle_update_paragraph surfaces it as a plain {"error": ...} with zero
    handler changes required."""
    async def _run():
        from meridian import server as mh

        sidecar = _seed_store_via_env(tmp_path, monkeypatch)
        docx_path = _write_docx(str(tmp_path / "dup.docx"), _DUPLICATE_DOCUMENT_XML)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "dup-proj")
            pid = proj["id"]
            seed = await doc_store.open_doc_store_for(
                plan=None, hosted=False, data_dir=str(tmp_path),
                tenant_pg_url=None, override_url=sidecar,
            )
            await seed.reindex_document(pid, docx_path, source=docx_path)

            res = await mh._dispatch_mcp_tool(
                "update_paragraph",
                {"project_id": pid, "doc": docx_path, "para_id": "6BDC5378",
                 "new_text": "must not land"},
                db, str(tmp_path),
            )
            assert "error" in res
            assert "6BDC5378" in res["error"]
            new_xml = _read_document_xml(docx_path).decode("utf-8")
            assert "must not land" not in new_xml
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_repair_duplicate_para_ids_mapping_only_does_not_touch_source(tmp_path):
    """Omitting dest_path is a pure, read-only MAPPING -- no bytes written
    anywhere, source untouched, regardless of whether duplicates exist."""
    docx_path = _write_docx(str(tmp_path / "dup.docx"), _DUPLICATE_DOCUMENT_XML)
    before = _read_document_xml(docx_path)

    result = doc_store.repair_duplicate_para_ids(docx_path)

    assert result["duplicates_found"] == 1
    assert result["applied"] is False
    assert result["dest_path"] is None
    assert len(result["remapped"]) == 1
    remap = result["remapped"][0]
    assert remap["old_para_id"] == "6BDC5378"
    assert remap["new_para_id"] != "6BDC5378"
    assert remap["index"] == 3
    assert "Second, unrelated paragraph" in remap["text"]
    # Nothing was written -- source .docx is byte-for-byte unchanged.
    assert _read_document_xml(docx_path) == before
    assert not os.path.exists(docx_path + ".bak")


def test_repair_duplicate_para_ids_applies_and_resolves_the_ambiguity(tmp_path):
    """Given dest_path: the repaired copy is written there (never back onto
    source implicitly), and afterward BOTH the original id (now unique --
    first occurrence only) and the freshly minted id resolve unambiguously."""
    docx_path = _write_docx(str(tmp_path / "dup.docx"), _DUPLICATE_DOCUMENT_XML)
    before = _read_document_xml(docx_path)
    dest_path = str(tmp_path / "dup_repaired.docx")

    result = doc_store.repair_duplicate_para_ids(docx_path, dest_path=dest_path)

    assert result["applied"] is True
    assert result["dest_path"] == dest_path
    new_id = result["remapped"][0]["new_para_id"]

    # Source is untouched -- repair never implicitly overwrites it.
    assert _read_document_xml(docx_path) == before

    # The repaired copy now resolves BOTH ids unambiguously.
    _raw, repaired_root = doc_store._load_docx_xml(dest_path)
    first = doc_store._find_paragraph_by_id(repaired_root, "6BDC5378")
    second = doc_store._find_paragraph_by_id(repaired_root, new_id)
    assert first is not None and second is not None
    assert doc_store._paragraph_plain_text(first) == (
        "First paragraph, original owner of 6BDC5378."
    )
    assert doc_store._paragraph_plain_text(second) == (
        "Second, unrelated paragraph that WRONGLY shares 6BDC5378."
    )


def test_repair_duplicate_para_ids_no_duplicates_is_a_noop(tmp_path):
    """A document with no duplicated native ids reports zero duplicates and
    writes nothing, even when dest_path is given."""
    docx_path = _write_docx(str(tmp_path / "clean.docx"))
    dest_path = str(tmp_path / "clean_out.docx")

    result = doc_store.repair_duplicate_para_ids(docx_path, dest_path=dest_path)

    assert result["duplicates_found"] == 0
    assert result["remapped"] == []
    assert result["applied"] is False
    assert result["dest_path"] is None
    assert not os.path.exists(dest_path)
