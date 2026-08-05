"""b17ef22b -- regression fixture for the namespace-prefix-preserving,
fail-closed rewrite of ``_save_docx_xml_stdlib`` / ``_load_docx_xml_stdlib``.

Before this fix, ``_save_docx_xml_stdlib`` serialized ``word/document.xml``
via a bare ``ET.tostring(root, encoding="unicode")`` whose namespace-prefix
choices came ONLY from the module-load-time ``ET.register_namespace(...)``
calls (a fixed set of common OOXML namespaces). That silently mangled a
round-tripped document in two ways this file exercises directly:

  1. A namespace actually REFERENCED by some element/attribute in the
     document, but bound to a prefix this module never registered (a vendor
     extension, or simply a non-default prefix convention), got renumbered
     to ``ns0``/``ns1``/... instead of keeping its original prefix.
  2. A namespace declared on the root purely for markup-compatibility
     (listed in ``mc:Ignorable``, or simply one Word always declares whether
     or not this particular document uses it) is invisible to
     ``ET.tostring()``'s "is this URI referenced anywhere in the tree" walk
     and got dropped from the output ENTIRELY -- corrupting ``mc:Ignorable``,
     whose prefix tokens must stay declared per ECMA-376 Part 3.

The fix (scoped to ``_save_docx_xml_stdlib`` / ``_load_docx_xml_stdlib`` --
see the module-level comment in docs_intel.py above
``_NAMESPACE_REGISTRATION_LOCK``) preserves both cases and fails closed
(``DocxWriteVerificationError``, before anything is staged to disk) if the
serialized output would still lose a prefix or leave ``mc:Ignorable``
referencing an undeclared prefix.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from meridian_docs import docs_intel


@pytest.fixture(autouse=True)
def _default_render_capability(monkeypatch):
    """016015e1/ddd79188 -- insert_caption now invokes the real
    render-capability gate (render_gate.check_render_capability) AFTER
    structural verification passes. Tests in this file exercise namespace
    preservation and must not depend on -- or be slowed/blocked by --
    whichever render backends (LibreOffice, Word COM) happen to be
    installed on the machine running the suite. Stub a successful
    'rendered' result by default, mirroring
    test_19be1551_insert_figure_block.py's fixture of the same name.
    """
    monkeypatch.setattr(
        docs_intel.render_gate,
        "check_render_capability",
        lambda docx_path, **kwargs: {
            "status": "rendered",
            "backend": "test-stub",
            "detail": {"stub": True},
        },
    )


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"

# Deliberately NOT in docs_intel.py's module-load ET.register_namespace(...)
# baseline list, and deliberately unusual prefix strings -- this is exactly
# the class of namespace the pre-fix code would renumber/drop.
_CUSTOM_USED_URI = "urn:meridian-test:custom-used"
_CUSTOM_UNUSED_URI = "urn:meridian-test:custom-unused"

_MC_IGNORABLE_ATTR = f"{{{_MC}}}Ignorable"


# ``zzcustom`` is actually referenced (a real element uses it) -- exercises
# the "register the original prefix before tostring" half of the fix.
# ``unused9`` is declared on the root but never referenced by any element or
# attribute -- exercises the "splice back a dropped-but-declared namespace"
# half. Both are listed in mc:Ignorable, alongside the standard w14.
_NAMESPACE_DOC_XML = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:mc="{_MC}" xmlns:zzcustom="{_CUSTOM_USED_URI}" xmlns:unused9="{_CUSTOM_UNUSED_URI}" mc:Ignorable="w14 unused9">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:pPr>
        <zzcustom:extra val="1"/>
      </w:pPr>
      <w:r><w:t>Hello world.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
'''


def _make_docx_bytes(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _write_docx(tmp_path, xml: str, name: str = "doc.docx") -> str:
    path = str(tmp_path / name)
    with open(path, "wb") as fh:
        fh.write(_make_docx_bytes(xml))
    return path


def _read_document_xml(path: str) -> bytes:
    with zipfile.ZipFile(path, "r") as zf:
        return zf.read("word/document.xml")


def _declared_prefixes(document_xml_bytes: bytes) -> dict[str, str]:
    """uri -> prefix map (first occurrence wins), via the module's own
    namespace-declaration extractor -- exercising the real implementation,
    not a re-implementation that could drift from it."""
    result: dict[str, str] = {}
    for prefix, uri in docs_intel._root_namespace_declarations(document_xml_bytes):
        result.setdefault(uri, prefix)
    return result


# ---------------------------------------------------------------------------
# Core round-trip preservation, through the real _load/_save pair.
# ---------------------------------------------------------------------------

def test_unmodified_round_trip_preserves_all_original_namespace_prefixes(tmp_path):
    path = _write_docx(tmp_path, _NAMESPACE_DOC_XML)

    raw, root = docs_intel._load_docx_xml_stdlib(path)
    docs_intel._save_docx_xml_stdlib(raw, root, path)

    new_xml = _read_document_xml(path)
    prefixes = _declared_prefixes(new_xml)

    assert prefixes[_W] == "w"
    assert prefixes[_W14] == "w14"
    assert prefixes[_MC] == "mc"
    assert prefixes[_CUSTOM_USED_URI] == "zzcustom", (
        "a namespace actually referenced by an element must keep its "
        "original (non-baseline) prefix, not be renumbered to ns0/ns1/..."
    )
    assert prefixes[_CUSTOM_UNUSED_URI] == "unused9", (
        "a namespace declared on the root but never referenced by any "
        "element must still survive the round trip (ET drops these unless "
        "explicitly spliced back)"
    )


def test_unmodified_round_trip_keeps_custom_element_prefix_in_body(tmp_path):
    path = _write_docx(tmp_path, _NAMESPACE_DOC_XML)

    raw, root = docs_intel._load_docx_xml_stdlib(path)
    docs_intel._save_docx_xml_stdlib(raw, root, path)

    new_xml = _read_document_xml(path).decode("utf-8")
    assert "<zzcustom:extra" in new_xml, (
        "the body element using the custom namespace must be re-emitted "
        "under its ORIGINAL prefix, not an auto-generated ns0/ns1/..."
    )
    assert "ns0:" not in new_xml and "ns1:" not in new_xml


def test_unmodified_round_trip_preserves_mc_ignorable_value_and_validity(tmp_path):
    path = _write_docx(tmp_path, _NAMESPACE_DOC_XML)

    raw, root = docs_intel._load_docx_xml_stdlib(path)
    docs_intel._save_docx_xml_stdlib(raw, root, path)

    new_xml = _read_document_xml(path)
    reparsed = docs_intel.ET.fromstring(new_xml)
    ignorable = reparsed.get(_MC_IGNORABLE_ATTR)
    assert ignorable == "w14 unused9"

    declared = set(_declared_prefixes(new_xml).values())
    for token in ignorable.split():
        assert token in declared, (
            f"mc:Ignorable references prefix {token!r} which is not declared "
            "in the write-back output -- this is exactly the corruption "
            "this fix exists to prevent"
        )


def test_real_write_path_through_insert_caption_preserves_namespaces(tmp_path):
    """Integration check: a genuine public write API (not the private
    load/save pair directly) goes through the same fix."""
    path = _write_docx(tmp_path, _NAMESPACE_DOC_XML)

    result = docs_intel.insert_caption(
        path, "P0000001", "Table", "Results overview", index_db_path=None,
    )
    assert result.get("status") == "inserted", result

    new_xml = _read_document_xml(path)
    prefixes = _declared_prefixes(new_xml)
    assert prefixes[_CUSTOM_USED_URI] == "zzcustom"
    assert prefixes[_CUSTOM_UNUSED_URI] == "unused9"

    reparsed = docs_intel.ET.fromstring(new_xml)
    ignorable = reparsed.get(_MC_IGNORABLE_ATTR)
    declared = set(prefixes.values())
    for token in (ignorable or "").split():
        assert token in declared


# ---------------------------------------------------------------------------
# Fail-closed behavior: a write that would corrupt namespace/mc:Ignorable
# validity must be rejected BEFORE anything is staged to disk, leaving the
# destination byte-for-byte untouched.
# ---------------------------------------------------------------------------

def test_mc_ignorable_referencing_undeclared_prefix_fails_closed(tmp_path):
    path = _write_docx(tmp_path, _NAMESPACE_DOC_XML)
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    raw, root = docs_intel._load_docx_xml_stdlib(path)
    # Simulate a caller-introduced defect: mc:Ignorable now references a
    # prefix that was never declared anywhere.
    root.set(_MC_IGNORABLE_ATTR, "w14 totally_undeclared_prefix")

    with pytest.raises(docs_intel.DocxWriteVerificationError):
        docs_intel._save_docx_xml_stdlib(raw, root, path)

    with open(path, "rb") as fh:
        assert fh.read() == original_bytes, (
            "a fail-closed verification error must leave dest byte-for-byte "
            "untouched -- never a partially-written or corrupted package"
        )


def test_namespace_prefix_rename_defense_in_depth_fails_closed(tmp_path, monkeypatch):
    """Even if the register/restore step in _save_docx_xml_stdlib somehow
    still emitted a renamed prefix for an original namespace (the one
    scenario a single global ET.register_namespace slot cannot fully rule
    out), the post-serialization assertion must catch it and refuse to
    promote the write -- rather than silently writing a corrupted package.
    """
    path = _write_docx(tmp_path, _NAMESPACE_DOC_XML)
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    raw, root = docs_intel._load_docx_xml_stdlib(path)

    real_tostring = docs_intel.ET.tostring

    def _tostring_with_renamed_prefix(element, **kwargs):
        xml = real_tostring(element, **kwargs)
        # Simulate ET having renumbered the custom namespace's prefix.
        return xml.replace("zzcustom:", "renamed:").replace(
            'xmlns:zzcustom="', 'xmlns:renamed="'
        )

    monkeypatch.setattr(docs_intel.ET, "tostring", _tostring_with_renamed_prefix)

    with pytest.raises(docs_intel.DocxWriteVerificationError):
        docs_intel._save_docx_xml_stdlib(raw, root, path)

    with open(path, "rb") as fh:
        assert fh.read() == original_bytes


def test_malformed_serialization_fails_closed(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _NAMESPACE_DOC_XML)
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    raw, root = docs_intel._load_docx_xml_stdlib(path)

    def _tostring_malformed(element, **kwargs):
        return "<w:document unterminated"

    monkeypatch.setattr(docs_intel.ET, "tostring", _tostring_malformed)

    with pytest.raises(docs_intel.DocxWriteVerificationError):
        docs_intel._save_docx_xml_stdlib(raw, root, path)

    with open(path, "rb") as fh:
        assert fh.read() == original_bytes


# ---------------------------------------------------------------------------
# Unit coverage for the extraction/splice helpers themselves.
# ---------------------------------------------------------------------------

def test_root_namespace_declarations_order_and_default_ns():
    xml = b'''<?xml version="1.0"?>
<root xmlns="urn:default" xmlns:a="urn:a" xmlns:b="urn:b">
  <child/>
</root>'''
    decls = docs_intel._root_namespace_declarations(xml)
    assert decls == [("", "urn:default"), ("a", "urn:a"), ("b", "urn:b")]


def test_restore_dropped_namespace_declarations_reinserts_missing_only():
    original = _NAMESPACE_DOC_XML.encode("utf-8")
    # A "new" document that ET would have produced if it dropped the
    # never-referenced unused9 namespace entirely.
    new = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:mc="{_MC}" xmlns:zzcustom="{_CUSTOM_USED_URI}" mc:Ignorable="w14 unused9">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:pPr>
        <zzcustom:extra val="1"/>
      </w:pPr>
      <w:r><w:t>Hello world.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
'''.encode("utf-8")

    repaired = docs_intel._restore_dropped_namespace_declarations(original, new)
    prefixes = _declared_prefixes(repaired)
    assert prefixes[_CUSTOM_UNUSED_URI] == "unused9"
    # Untouched ones stay exactly as they were.
    assert prefixes[_CUSTOM_USED_URI] == "zzcustom"
    # And it's still well-formed.
    docs_intel.ET.fromstring(repaired)
