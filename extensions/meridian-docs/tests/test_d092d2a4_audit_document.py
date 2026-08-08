"""d092d2a4 -- authoritative figure/table/caption OWNERSHIP graph, read-only.

Exercises :func:`docs_intel.audit_document` against small, disposable,
synthetic .docx documents built in-memory (same style as
``test_docx_write_integrity.py``'s unit-level fixtures) -- never a canonical
document, never a network call, never a write. Each test isolates ONE finding
type so a regression in one detector cannot masquerade as a pass on another.
"""
from __future__ import annotations

import io
import zipfile

from meridian_docs import docs_intel


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _write_docx(tmp_path, xml: str, name: str = "doc.docx", media: dict[str, bytes] | None = None) -> str:
    path = tmp_path / name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
        for media_name, data in (media or {"word/media/image1.png": b"image"}).items():
            archive.writestr(media_name, data)
    path.write_bytes(buf.getvalue())
    return str(path)


def _findings_of_type(result: dict, finding_type: str) -> list[dict]:
    return [f for f in result["findings"] if f["type"] == finding_type]


# ---------------------------------------------------------------------------
# Baseline: a clean, correctly-captioned image produces zero findings and a
# well-formed ownership edge.
# ---------------------------------------------------------------------------

_CLEAN_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:r="{_R}" xmlns:a="{_A}">
  <w:body>
    <w:p w14:paraId="H0000001"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Results</w:t></w:r></w:p>
    <w:p w14:paraId="IMG000001"><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p>
    <w:p w14:paraId="CAP000001"><w:fldSimple w:instr=" SEQ Figure \\* ARABIC "><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''


def test_audit_document_clean_document_has_no_findings_and_one_ownership_edge(tmp_path):
    path = _write_docx(tmp_path, _CLEAN_XML)

    result = docs_intel.audit_document(path)

    assert result["status"] == "ok"
    assert result["findings"] == []
    assert result["finding_count"] == 0
    assert result["duplicate_relationships"] == {}
    assert result["total_image_paragraphs"] == 1
    edge = result["edges"][0]
    assert edge["type"] == "image_caption"
    assert edge["image_id"] == "IMG000001"
    assert edge["caption_id"] == "CAP000001"
    node_ids = {n["id"] for n in result["nodes"]}
    assert {"H0000001", "IMG000001", "CAP000001"} <= node_ids


def test_audit_document_missing_file_returns_error(tmp_path):
    result = docs_intel.audit_document(str(tmp_path / "does-not-exist.docx"))
    assert "error" in result


def test_audit_document_stale_fingerprint_short_circuits(tmp_path):
    path = _write_docx(tmp_path, _CLEAN_XML)

    result = docs_intel.audit_document(path, expected_source_fingerprint="deadbeef")

    assert result["status"] == "stale"
    assert result["reason"] == "source_fingerprint_mismatch"
    assert result["findings"] == []


# ---------------------------------------------------------------------------
# orphan_image + repeated_embed
# ---------------------------------------------------------------------------

_ORPHAN_AND_DUPLICATE_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:r="{_R}" xmlns:a="{_A}">
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Intro</w:t></w:r></w:p>
    <w:p w14:paraId="IMG000001"><w:r><w:drawing><a:blip r:embed="rId9"/></w:drawing></w:r></w:p>
    <w:p w14:paraId="P0000002"><w:r><w:t>Not a caption -- just prose.</w:t></w:r></w:p>
    <w:p w14:paraId="IMG000002"><w:r><w:drawing><a:blip r:embed="rId9"/></w:drawing></w:r></w:p>
    <w:p w14:paraId="CAP000001"><w:fldSimple w:instr=" SEQ Figure \\* ARABIC "><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''


def test_audit_document_flags_orphan_image(tmp_path):
    path = _write_docx(tmp_path, _ORPHAN_AND_DUPLICATE_XML)

    result = docs_intel.audit_document(path)

    orphans = _findings_of_type(result, "orphan_image")
    assert len(orphans) == 1
    assert orphans[0]["image_ids"] == ["IMG000001"]


def test_audit_document_flags_repeated_embed(tmp_path):
    path = _write_docx(tmp_path, _ORPHAN_AND_DUPLICATE_XML)

    result = docs_intel.audit_document(path)

    dupes = _findings_of_type(result, "repeated_embed")
    assert len(dupes) == 1
    assert dupes[0]["relationship_id"] == "rId9"
    assert set(dupes[0]["owning_blocks"]) == {"IMG000001", "IMG000002"}
    assert result["duplicate_relationships"]["rId9"] == sorted(["IMG000001", "IMG000002"])


# ---------------------------------------------------------------------------
# caption_only_figure vs non_adjacent_caption_attachment
# ---------------------------------------------------------------------------

_CAPTION_ONLY_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:r="{_R}" xmlns:a="{_A}">
  <w:body>
    <w:p w14:paraId="H0000001"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Appendix</w:t></w:r></w:p>
    <w:p w14:paraId="CAP000001"><w:fldSimple w:instr=" SEQ Figure \\* ARABIC "><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''


def test_audit_document_flags_caption_only_figure_when_no_image_is_nearby(tmp_path):
    path = _write_docx(tmp_path, _CAPTION_ONLY_XML, media={})

    result = docs_intel.audit_document(path)

    only = _findings_of_type(result, "caption_only_figure")
    assert len(only) == 1
    assert only[0]["caption_id"] == "CAP000001"
    assert _findings_of_type(result, "non_adjacent_caption_attachment") == []


_NON_ADJACENT_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:r="{_R}" xmlns:a="{_A}">
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Intro</w:t></w:r></w:p>
    <w:p w14:paraId="IMG000001"><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p>
    <w:p w14:paraId="P0000002"><w:r><w:t>An unrelated sentence sits between the image and its caption.</w:t></w:r></w:p>
    <w:p w14:paraId="CAP000001"><w:fldSimple w:instr=" SEQ Figure \\* ARABIC "><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''


def test_audit_document_flags_non_adjacent_caption_attachment(tmp_path):
    path = _write_docx(tmp_path, _NON_ADJACENT_XML)

    result = docs_intel.audit_document(path)

    non_adjacent = _findings_of_type(result, "non_adjacent_caption_attachment")
    assert len(non_adjacent) == 1
    assert non_adjacent[0]["caption_id"] == "CAP000001"
    assert non_adjacent[0]["distance"] == 2
    # The image itself is also reported from the image side -- both are
    # legitimate, complementary views of the same underlying defect.
    assert len(_findings_of_type(result, "orphan_image")) == 1
    assert _findings_of_type(result, "caption_only_figure") == []


# ---------------------------------------------------------------------------
# duplicate_para_id
# ---------------------------------------------------------------------------

_DUPLICATE_PARA_ID_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}">
  <w:body>
    <w:p w14:paraId="DEADBEEF"><w:r><w:t>First</w:t></w:r></w:p>
    <w:p w14:paraId="P0000001"><w:r><w:t>Middle</w:t></w:r></w:p>
    <w:p w14:paraId="DEADBEEF"><w:r><w:t>Second, same native id as the first</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''


def test_audit_document_flags_duplicate_para_id(tmp_path):
    path = _write_docx(tmp_path, _DUPLICATE_PARA_ID_XML, media={})

    result = docs_intel.audit_document(path)

    dupes = _findings_of_type(result, "duplicate_para_id")
    assert len(dupes) == 1
    assert dupes[0]["para_id"] == "DEADBEEF"
    assert dupes[0]["occurrence_count"] == 2


# ---------------------------------------------------------------------------
# table_cell_image_ambiguity
# ---------------------------------------------------------------------------

_TABLE_CELL_IMAGE_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:r="{_R}" xmlns:a="{_A}">
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Before the table.</w:t></w:r></w:p>
    <w:tbl>
      <w:tr>
        <w:tc>
          <w:p><w:r><w:drawing><a:blip r:embed="rId5"/></w:drawing></w:r></w:p>
        </w:tc>
        <w:tc>
          <w:p><w:r><w:t>plain cell</w:t></w:r></w:p>
        </w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>'''


def test_audit_document_flags_table_cell_image_ambiguity(tmp_path):
    path = _write_docx(tmp_path, _TABLE_CELL_IMAGE_XML)

    result = docs_intel.audit_document(path)

    ambiguous = _findings_of_type(result, "table_cell_image_ambiguity")
    assert len(ambiguous) == 1
    assert ambiguous[0]["table_id"] == "table:1"
    assert ambiguous[0]["row"] == 0
    assert ambiguous[0]["col"] == 0
    assert ambiguous[0]["relationship_ids"] == ["rId5"]
    # find_image_paragraph's independent body+table-cell walk agrees there is
    # exactly one image paragraph total, and it is NOT a direct-body one.
    assert result["total_image_paragraphs"] == 1
    assert result["edges"] == []
    table_nodes = [n for n in result["nodes"] if n["kind"] == "table"]
    assert table_nodes and table_nodes[0]["id"] == "table:1"


# ---------------------------------------------------------------------------
# stale_or_empty_sidecar
# ---------------------------------------------------------------------------

_BLANK_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}">
  <w:body>
    <w:sectPr/>
  </w:body>
</w:document>'''


def test_audit_document_flags_stale_or_empty_sidecar_built_from_wrong_document(tmp_path):
    # The sidecar is indexed against a BLANK document (so it legitimately
    # records zero headings/figures/tables and is not stale relative to that
    # blank source), but the audit itself targets the real, non-empty
    # document -- reproducing the "sidecar built from the wrong/placeholder
    # document" failure mode.
    blank_path = _write_docx(tmp_path, _BLANK_XML, name="blank.docx", media={})
    real_path = _write_docx(tmp_path, _CLEAN_XML, name="real.docx")
    index_db_path = str(tmp_path / "sidecar.db")

    index_result = docs_intel.index_docx_structure(blank_path, index_db_path)
    assert index_result["complete"] is True
    assert index_result["heading_count"] == 0

    result = docs_intel.audit_document(real_path, index_db_path=index_db_path)

    stale = _findings_of_type(result, "stale_or_empty_sidecar")
    assert len(stale) == 1
    assert result["sidecar"]["empty"] is True
    assert result["sidecar"]["trustworthy"] is True


def test_audit_document_never_indexed_sidecar_is_not_flagged(tmp_path):
    path = _write_docx(tmp_path, _CLEAN_XML)
    index_db_path = str(tmp_path / "never-indexed.db")

    result = docs_intel.audit_document(path, index_db_path=index_db_path)

    assert _findings_of_type(result, "stale_or_empty_sidecar") == []
    assert result["sidecar"]["indexed"] is False


def test_audit_document_no_index_db_path_omits_sidecar(tmp_path):
    path = _write_docx(tmp_path, _CLEAN_XML)

    result = docs_intel.audit_document(path)

    assert result["sidecar"] is None


# ---------------------------------------------------------------------------
# Defensive: every emitted paragraph/heading node id round-trips through
# _find_para_by_id across every fixture above -- never expected to fire.
# ---------------------------------------------------------------------------

def test_audit_document_never_emits_unresolvable_structural_id(tmp_path):
    for name, xml, media in (
        ("clean", _CLEAN_XML, None),
        ("orphan", _ORPHAN_AND_DUPLICATE_XML, None),
        ("caption_only", _CAPTION_ONLY_XML, {}),
        ("non_adjacent", _NON_ADJACENT_XML, None),
        ("duplicate_id", _DUPLICATE_PARA_ID_XML, {}),
        ("table_cell", _TABLE_CELL_IMAGE_XML, None),
    ):
        path = _write_docx(tmp_path, xml, name=f"{name}.docx", media=media)
        result = docs_intel.audit_document(path)
        assert _findings_of_type(result, "unresolvable_structural_id") == [], name
