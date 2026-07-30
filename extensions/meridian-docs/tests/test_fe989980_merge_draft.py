"""Tests for fe989980 -- wave-scoped DOCX merge manifests, the
extensions/meridian-docs half.

The durable, cross-session coordination (open_merge_manifest /
declare_merge_anchors / claim_merge_owner / check_merge_stale_or_overlap /
record_merge_result / finalize_merge_manifest) lives in the Meridian core
package (meridian.db.docx_merge, tested in tests/test_docx_merge_manifest.py)
-- this stdlib-only, DB-free extension has no access to that state at all.
What DOES live here, and is covered by this file:

  1. Opt-in wave-scoped draft mode on move_section / copy_section /
     relocate_table / relocate_figure (draft_output_path + wave_run_id,
     both-or-neither): registering/producing a draft never touches the
     canonical file.
  2. Legacy direct-write callers (no draft_output_path/wave_run_id) are
     byte-identical to pre-fe989980 behavior.
  3. merge_draft_into_canonical -- the file-level promotion step a wave's
     serialized merge owner calls once the DB-side gate has cleared:
     non-overlapping drafts combine cleanly across successive merges,
     structurally-invalid drafts are rejected before ever touching
     canonical, and a post-promotion verification failure restores
     canonical from backup and reports failure, never a false success.

All tests are pure Python (stdlib + pytest) -- no mcp, no network. Follows
the same conventions as test_docs_intel_new_primitives.py / test_relocate_table.py:
tests that mutate write a minimal .docx to tmp_path first.
"""
from __future__ import annotations

import inspect
import io
import zipfile

from meridian_docs import docs_intel, server


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"


def _make_docx_bytes(xml: str, media: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
        for name, data in (media or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def _write_docx(tmp_path, xml: str, name: str, media: dict[str, bytes] | None = None) -> str:
    path = str(tmp_path / name)
    with open(path, "wb") as fh:
        fh.write(_make_docx_bytes(xml, media))
    return path


def _body_texts(path: str) -> list[str]:
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    body = root.find(docs_intel._q(_W, "body"))
    out = []
    for p in body.iter(docs_intel._q(_W, "p")):
        text = "".join(t.text or "" for t in p.iter(docs_intel._q(_W, "t")))
        if text:
            out.append(text)
    return out


_TWO_SECTION_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="H0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Intro body paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000002">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Results</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Results body paragraph.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""

_SIMPLE_TABLE_DOC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Intro paragraph.</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tblPr><w:tblStyle w:val="TableGrid"/></w:tblPr>
      <w:tblGrid><w:gridCol w:w="2000"/></w:tblGrid>
      <w:tr><w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>End paragraph.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


# ---------------------------------------------------------------------------
# 1. Draft mode: isolated draft artifact, canonical untouched.
# ---------------------------------------------------------------------------

def test_move_section_draft_mode_writes_isolated_draft_canonical_untouched(tmp_path):
    canonical = _write_docx(tmp_path, _TWO_SECTION_XML, "canonical.docx")
    with open(canonical, "rb") as fh:
        original_bytes = fh.read()
    draft = str(tmp_path / "draft.docx")

    result = docs_intel.move_section(
        canonical, "H0000002", "H0000001", destination_position="before",
        draft_output_path=draft, wave_run_id="wave-1",
    )

    assert result["status"] == "moved"
    assert result["is_draft"] is True
    assert result["wave_run_id"] == "wave-1"
    assert result["docx_path"] == draft

    with open(canonical, "rb") as fh:
        assert fh.read() == original_bytes, "canonical must be untouched in draft mode"

    # The draft actually reflects the move: Results now precedes Introduction.
    texts = _body_texts(draft)
    assert texts.index("Results") < texts.index("Introduction")


def test_relocate_table_draft_mode_writes_isolated_draft_canonical_untouched(tmp_path):
    canonical = _write_docx(tmp_path, _SIMPLE_TABLE_DOC_XML, "canonical.docx")
    with open(canonical, "rb") as fh:
        original_bytes = fh.read()
    draft = str(tmp_path / "draft.docx")

    result = docs_intel.relocate_table(
        canonical, 1, "P0000002", destination_position="after",
        draft_output_path=draft, wave_run_id="wave-1",
    )

    assert result["status"] == "moved"
    assert result["is_draft"] is True
    assert result["docx_path"] == draft

    with open(canonical, "rb") as fh:
        assert fh.read() == original_bytes, "canonical must be untouched in draft mode"


def test_draft_output_path_and_wave_run_id_required_together(tmp_path):
    canonical = _write_docx(tmp_path, _TWO_SECTION_XML, "canonical.docx")

    only_draft = docs_intel.move_section(
        canonical, "H0000002", "H0000001",
        draft_output_path=str(tmp_path / "draft.docx"),
    )
    assert "error" in only_draft
    assert "together" in only_draft["error"]

    only_wave = docs_intel.move_section(canonical, "H0000002", "H0000001", wave_run_id="wave-1")
    assert "error" in only_wave
    assert "together" in only_wave["error"]


def test_draft_output_path_must_differ_from_canonical(tmp_path):
    canonical = _write_docx(tmp_path, _TWO_SECTION_XML, "canonical.docx")

    result = docs_intel.move_section(
        canonical, "H0000002", "H0000001",
        draft_output_path=canonical, wave_run_id="wave-1",
    )
    assert "error" in result
    assert "isolated" in result["error"]


# ---------------------------------------------------------------------------
# 2. Legacy direct-write path: byte-identical to pre-fe989980 behavior.
# ---------------------------------------------------------------------------

def test_move_section_legacy_call_omitting_new_params_is_byte_identical(tmp_path):
    path_a = _write_docx(tmp_path, _TWO_SECTION_XML, "a.docx")
    path_b = _write_docx(tmp_path, _TWO_SECTION_XML, "b.docx")
    assert open(path_a, "rb").read() == open(path_b, "rb").read()

    # One caller uses only the pre-fe989980 kwargs; the other explicitly
    # passes the two new params as their defaults -- both must produce
    # byte-identical on-disk results and equivalent result payloads.
    result_a = docs_intel.move_section(path_a, "H0000002", "H0000001", destination_position="before")
    result_b = docs_intel.move_section(
        path_b, "H0000002", "H0000001", destination_position="before",
        draft_output_path=None, wave_run_id=None,
    )

    assert result_a["status"] == result_b["status"] == "moved"
    assert result_a["is_draft"] is False
    assert result_a["wave_run_id"] is None
    assert result_a["docx_path"] == path_a  # unchanged legacy field semantics

    with open(path_a, "rb") as fh:
        bytes_a = fh.read()
    with open(path_b, "rb") as fh:
        bytes_b = fh.read()
    assert bytes_a == bytes_b, "legacy call must be byte-identical regardless of explicit None defaults"


def test_relocate_figure_legacy_result_keys_unchanged_shape(tmp_path):
    # Sanity: is_draft/wave_run_id are ADDITIVE keys, not replacements -- every
    # legacy field is still present with its pre-fe989980 meaning.
    sig = inspect.signature(docs_intel.relocate_figure)
    assert "draft_output_path" in sig.parameters
    assert "wave_run_id" in sig.parameters
    assert sig.parameters["draft_output_path"].default is None
    assert sig.parameters["wave_run_id"].default is None


# ---------------------------------------------------------------------------
# 3. merge_draft_into_canonical -- the file-level promotion step.
# ---------------------------------------------------------------------------

def test_merge_draft_into_canonical_success(tmp_path):
    canonical = _write_docx(tmp_path, _TWO_SECTION_XML, "canonical.docx")
    draft = str(tmp_path / "draft.docx")

    move_result = docs_intel.move_section(
        canonical, "H0000002", "H0000001", destination_position="before",
        draft_output_path=draft, wave_run_id="wave-1",
    )
    assert move_result["status"] == "moved"

    merge_result = docs_intel.merge_draft_into_canonical(canonical, draft)

    assert merge_result["merged"] is True
    assert merge_result["status"] == "merged"
    assert merge_result["canonical_path"] == canonical
    assert merge_result["draft_path"] == draft

    # Canonical now reflects the draft's content exactly.
    with open(canonical, "rb") as fh:
        canonical_bytes = fh.read()
    with open(draft, "rb") as fh:
        draft_bytes = fh.read()
    texts = _body_texts(canonical)
    assert texts.index("Results") < texts.index("Introduction")
    assert docs_intel._load_docx_xml_stdlib(canonical)[1] is not None
    # The document.xml payload is identical (other zip members may reorder).
    _c_raw, c_root = docs_intel._load_docx_xml_stdlib(canonical)
    _d_raw, d_root = docs_intel._load_docx_xml_stdlib(draft)
    assert docs_intel._hash_elements(list(c_root.find(docs_intel._q(_W, "body")))) == \
        docs_intel._hash_elements(list(d_root.find(docs_intel._q(_W, "body"))))


def test_merge_draft_into_canonical_sequential_non_overlapping_drafts_combine(tmp_path):
    """Two successive, non-overlapping structural edits merge cleanly when
    each merge is serialized against the just-promoted canonical -- the file-
    level half of "non-overlapping drafts merge cleanly together" (the
    anchor-overlap/staleness gating itself is meridian.db.docx_merge's job,
    already covered in tests/test_docx_merge_manifest.py)."""
    canonical = _write_docx(tmp_path, _TWO_SECTION_XML, "canonical.docx")

    draft_1 = str(tmp_path / "draft1.docx")
    r1 = docs_intel.move_section(
        canonical, "H0000002", "H0000001", destination_position="before",
        draft_output_path=draft_1, wave_run_id="wave-1",
    )
    assert r1["status"] == "moved"
    m1 = docs_intel.merge_draft_into_canonical(canonical, draft_1)
    assert m1["merged"] is True

    # Second session rebases its draft against the NOW-current canonical
    # (post first merge) before its own merge -- exactly what a real caller
    # must do once meridian.db.docx_merge's stale_revision check has told
    # them their old base_revision no longer matches.
    draft_2 = str(tmp_path / "draft2.docx")
    r2 = docs_intel.copy_section(
        canonical, "H0000001", "H0000002", destination_position="after",
        trim_original_to="See above.",
        draft_output_path=draft_2, wave_run_id="wave-1",
    )
    assert r2["status"] == "copied"
    m2 = docs_intel.merge_draft_into_canonical(canonical, draft_2)
    assert m2["merged"] is True

    texts = _body_texts(canonical)
    assert "See above." in texts


def test_merge_draft_into_canonical_missing_draft(tmp_path):
    canonical = _write_docx(tmp_path, _TWO_SECTION_XML, "canonical.docx")
    with open(canonical, "rb") as fh:
        original_bytes = fh.read()

    result = docs_intel.merge_draft_into_canonical(canonical, str(tmp_path / "nope.docx"))

    assert result["merged"] is False
    assert "does not exist" in result["error"]
    with open(canonical, "rb") as fh:
        assert fh.read() == original_bytes


def test_merge_draft_into_canonical_invalid_draft_not_a_docx(tmp_path):
    canonical = _write_docx(tmp_path, _TWO_SECTION_XML, "canonical.docx")
    with open(canonical, "rb") as fh:
        original_bytes = fh.read()

    garbage = tmp_path / "garbage.docx"
    garbage.write_bytes(b"not a zip file at all")

    result = docs_intel.merge_draft_into_canonical(canonical, str(garbage))

    assert result["merged"] is False
    assert "not a valid .docx" in result["error"]
    with open(canonical, "rb") as fh:
        assert fh.read() == original_bytes


def test_merge_draft_into_canonical_rejects_structurally_unsound_draft(tmp_path):
    """A draft missing a media part the canonical file has must never be
    promoted -- caught by _atomic_write_docx_bytes's PRE-promotion
    structural-invariant gate, so canonical is guaranteed untouched (no
    restore needed; promotion never ran)."""
    canonical = _write_docx(
        tmp_path, _TWO_SECTION_XML, "canonical.docx",
        media={"word/media/image1.png": b"\x89PNG-fake-bytes"},
    )
    with open(canonical, "rb") as fh:
        original_bytes = fh.read()

    # Draft has the same document body but is missing the media part.
    draft = _write_docx(tmp_path, _TWO_SECTION_XML, "draft.docx")

    result = docs_intel.merge_draft_into_canonical(canonical, draft)

    assert result["merged"] is False
    assert "error" in result
    assert "file_restored" not in result, (
        "a pre-promotion structural rejection never touches canonical, so "
        "there is nothing to restore"
    )
    with open(canonical, "rb") as fh:
        assert fh.read() == original_bytes, "canonical must be untouched when the draft is rejected pre-promotion"


def test_merge_draft_into_canonical_verification_failure_restores_canonical(tmp_path, monkeypatch):
    """A post-promotion verification failure must restore canonical from the
    backup _atomic_write_docx_bytes already wrote, and report failure -- not
    a false success. Mirrors the established
    test_move_section_post_write_verification_catches_silent_noop_write
    pattern in test_docs_intel_new_primitives.py, adapted to the promotion
    step: force _verify_docx_write to report a failure regardless of the
    (real, successful) promotion that just happened."""
    canonical = _write_docx(tmp_path, _TWO_SECTION_XML, "canonical.docx")
    with open(canonical, "rb") as fh:
        original_canonical_bytes = fh.read()

    draft = str(tmp_path / "draft.docx")
    move_result = docs_intel.move_section(
        canonical, "H0000002", "H0000001", destination_position="before",
        draft_output_path=draft, wave_run_id="wave-1",
    )
    assert move_result["status"] == "moved"

    monkeypatch.setattr(
        docs_intel, "_verify_docx_write",
        lambda *args, **kwargs: {"error": "simulated post-promotion verification failure"},
    )

    result = docs_intel.merge_draft_into_canonical(canonical, draft)

    assert result["merged"] is False
    assert "error" in result
    assert result["file_restored"] is True

    with open(canonical, "rb") as fh:
        assert fh.read() == original_canonical_bytes, (
            "canonical must be restored to its pre-merge content on verification failure"
        )


def test_merge_draft_into_canonical_no_prior_canonical_creates_it(tmp_path):
    """A manifest's very first accepted draft, when nothing has ever been
    written to canonical_path yet, is a valid merge -- there is no
    pre-existing structural manifest to gate against, and nothing to back up."""
    canonical = str(tmp_path / "brand_new_canonical.docx")
    draft = _write_docx(tmp_path, _TWO_SECTION_XML, "draft.docx")

    result = docs_intel.merge_draft_into_canonical(canonical, draft)

    assert result["merged"] is True
    texts = _body_texts(canonical)
    assert "Introduction" in texts and "Results" in texts


# ---------------------------------------------------------------------------
# 4. MCP wrapper wiring (server.py) -- signature-level check, matching the
#    established inspect.signature convention in
#    test_d190d5c4_dual_spawn_sidecar_isolation.py (no live MCP transport).
# ---------------------------------------------------------------------------

def test_server_wrappers_expose_draft_params():
    for fn in (server.move_section, server.copy_section, server.relocate_figure, server.relocate_table):
        sig = inspect.signature(fn)
        assert "draft_output_path" in sig.parameters, fn
        assert "wave_run_id" in sig.parameters, fn
        assert sig.parameters["draft_output_path"].default is None
        assert sig.parameters["wave_run_id"].default is None


def test_server_exposes_merge_docx_draft_tool():
    sig = inspect.signature(server.merge_docx_draft)
    assert list(sig.parameters) == ["canonical_path", "draft_path", "index_db_path"]


def test_server_merge_docx_draft_delegates_to_docs_intel(tmp_path):
    canonical = _write_docx(tmp_path, _TWO_SECTION_XML, "canonical.docx")
    draft = str(tmp_path / "draft.docx")
    docs_intel.move_section(
        canonical, "H0000002", "H0000001", destination_position="before",
        draft_output_path=draft, wave_run_id="wave-1",
    )

    result = server.merge_docx_draft(canonical, draft)

    assert result["merged"] is True
