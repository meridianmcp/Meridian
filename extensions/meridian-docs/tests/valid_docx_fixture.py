"""Deterministic, relationship-complete DOCX fixture for read-only tests."""

from __future__ import annotations

import io
import zipfile


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"

_ZIP_DATE = (2020, 1, 1, 0, 0, 0)


def _zip_bytes(parts: list[tuple[str, bytes]]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(
        stream,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for name, payload in parts:
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload)
    return stream.getvalue()


def _valid_document_xml() -> bytes:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}" xmlns:w14="{W14}" xmlns:m="{M}" xmlns:r="{R}" xmlns:mc="{MC}" mc:Ignorable="w14">
  <w:body>
    <w:p w14:paraId="F0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Equation fixture</w:t></w:r>
    </w:p>
    <w:p w14:paraId="F0000002">
      <w:r><w:t xml:space="preserve">Inline radius: </w:t></w:r>
      <m:oMath>
        <m:sSub>
          <m:e><m:r><m:t>R</m:t></m:r></m:e>
          <m:sub><m:r><m:t>depth</m:t></m:r></m:sub>
        </m:sSub>
      </m:oMath>
      <w:r><w:t xml:space="preserve"> remains in prose.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="F0000003">
      <w:bookmarkStart w:id="7" w:name="_RefEquation1"/>
      <w:fldSimple w:instr=" SEQ Equation \\* ARABIC ">
        <w:r><w:t>1</w:t></w:r>
      </w:fldSimple>
      <w:bookmarkEnd w:id="7"/>
      <m:oMathPara>
        <m:oMath>
          <m:f>
            <m:num><m:e><m:r><m:t>DT</m:t></m:r></m:e></m:num>
            <m:den><m:e><m:r><m:t>2</m:t></m:r></m:e></m:den>
          </m:f>
        </m:oMath>
      </m:oMathPara>
    </w:p>
    <w:p w14:paraId="F0000004">
      <w:r><w:t xml:space="preserve">See equation </w:t></w:r>
      <w:fldSimple w:instr=" REF _RefEquation1 \\h ">
        <w:r><w:t>1</w:t></w:r>
      </w:fldSimple>
      <w:r><w:t>.</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc>
          <w:p w14:paraId="F0000005">
            <m:oMath>
              <m:sSub>
                <m:e><m:r><m:t>R</m:t></m:r></m:e>
                <m:sub><m:r><m:t>i</m:t></m:r></m:sub>
              </m:sSub>
              <m:r><m:t>=x</m:t></m:r>
            </m:oMath>
          </w:p>
        </w:tc>
        <w:tc><w:p><w:r><w:t>(2)</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:p w14:paraId="F0000006">
      <m:oMath>
        <m:f>
          <m:num/>
          <m:den><m:e><m:r><m:t>malformed</m:t></m:r></m:e></m:den>
        </m:f>
      </m:oMath>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''.encode("utf-8")


def valid_docx_bytes() -> bytes:
    """Return the same valid DOCX package bytes on every invocation."""
    parts = [
        (
            "[Content_Types].xml",
            f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ct:Types xmlns:ct="{CT}">
  <ct:Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <ct:Default Extension="xml" ContentType="application/xml"/>
  <ct:Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <ct:Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</ct:Types>'''.encode("utf-8"),
        ),
        (
            "_rels/.rels",
            f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<pr:Relationships xmlns:pr="{PKG_REL}">
  <pr:Relationship Id="rIdOfficeDocument" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</pr:Relationships>'''.encode("utf-8"),
        ),
        ("word/document.xml", _valid_document_xml()),
        (
            "word/_rels/document.xml.rels",
            f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<pr:Relationships xmlns:pr="{PKG_REL}">
  <pr:Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</pr:Relationships>'''.encode("utf-8"),
        ),
        (
            "word/styles.xml",
            f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W}">
  <w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="Heading 1"/></w:style>
</w:styles>'''.encode("utf-8"),
        ),
    ]
    return _zip_bytes(parts)


def fixture_notation_manifest() -> dict[str, object]:
    """Return the manifest used by the fixture's notation audits."""
    return {
        "version": "valid-docx-equation-fixture-v1",
        "case_sensitive": True,
        "symbols": [
            {
                "id": "radius",
                "symbol": "R",
                "preferred_notation": ["R_depth", "R_i"],
                "role": "radius",
                "kind": "quantity",
                "required": True,
            },
            {
                "id": "distance_transform",
                "symbol": "DT",
                "role": "distance transform",
                "kind": "quantity",
                "required": True,
            },
        ],
    }


__all__ = ["fixture_notation_manifest", "valid_docx_bytes"]
