"""Focused tests for item a4cb12bf (LOCAL-MCP-FILE-ROUTER): the bounded
local inspect/read router in
``extensions/meridian-outputs/meridian_outputs/file_inspector.py`` that
exposes ``inspect_local_file`` on the ``meridian-outputs`` MCP server.

Import strategy: ``meridian_outputs`` is an independently-installable
extension (own ``pyproject.toml``) -- NOT a ``[pypi-dependencies]`` entry of
the core ``meridian`` pixi env. Matching the precedent in
``tests/test_local_file_inspection.py``/``tests/test_local_file_inspection_
tabular.py`` (both siblings this router dispatches to), it is imported here
via a ``sys.path`` insertion rather than a declared pixi dependency.

Two test tiers:
  - Fast, subprocess-free unit tests for ``classify_path`` and the
    path-policy preflight (secret-path exclusion, symlinks, allowed_root,
    missing/directory targets) -- these never spawn anything.
  - Real end-to-end tests that drive ``inspect_local_file`` all the way
    through a REAL subprocess-spawned MCP session against the actual
    ``meridian-file-inspection``/``meridian-docs`` sibling packages (their
    dependencies -- lxml, duckdb, mcp, latex2mathml -- are already present
    in this repo's own pixi env, exactly like the sibling test files'
    ``sys.executable``-based fast launch path relies on), verifying the
    real routing table, the operation post-filter tiers, the DOCX envelope
    adapter, and the ``local_only``/``unavailable``/``failed`` state
    distinctions this router introduces.
"""
from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

_EXT_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "extensions", "meridian-outputs")
)
if _EXT_PATH not in sys.path:
    sys.path.insert(0, _EXT_PATH)

from meridian_outputs import file_inspector  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_STRUCTURAL_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="AA000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="AA000002">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Background</w:t></w:r>
    </w:p>
    <w:p w14:paraId="AA000003">
      <w:r><w:t>Some body text here.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def _make_docx(path: Path, xml: str = _STRUCTURAL_DOCUMENT_XML) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)


# ---------------------------------------------------------------------------
# classify_path -- fast, subprocess-free
# ---------------------------------------------------------------------------


class TestClassifyPath:
    def test_docx_extension_routes_to_docs(self, tmp_path):
        p = tmp_path / "report.docx"
        p.write_bytes(b"not a real docx, extension is what matters here")
        result = file_inspector.classify_path(str(p))
        assert result == {"route": "docs", "format": "docx"}

    @pytest.mark.parametrize("ext", [".docx", ".dotx", ".docm", ".dotm"])
    def test_all_docx_family_extensions_route_to_docs(self, tmp_path, ext):
        p = tmp_path / f"report{ext}"
        p.write_bytes(b"stub")
        assert file_inspector.classify_path(str(p))["route"] == "docs"

    def test_csv_extension_routes_to_tabular(self, tmp_path):
        p = tmp_path / "data.csv"
        p.write_text("a,b\n1,2\n")
        assert file_inspector.classify_path(str(p)) == {"route": "tabular", "format": "csv"}

    def test_xlsx_extension_routes_to_tabular(self, tmp_path):
        p = tmp_path / "data.xlsx"
        p.write_bytes(b"PK\x03\x04stub")
        assert file_inspector.classify_path(str(p)) == {"route": "tabular", "format": "xlsx"}

    def test_xml_extension_routes_to_generic(self, tmp_path):
        p = tmp_path / "data.xml"
        p.write_text("<root/>")
        assert file_inspector.classify_path(str(p)) == {"route": "generic", "format": "xml"}

    def test_json_object_routes_to_generic(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text(json.dumps({"a": 1}))
        assert file_inspector.classify_path(str(p)) == {"route": "generic", "format": "json"}

    def test_json_array_of_objects_routes_to_tabular(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text(json.dumps([{"a": 1}, {"a": 2}]))
        assert file_inspector.classify_path(str(p)) == {"route": "tabular", "format": "json"}

    def test_explicit_format_overrides_extension(self, tmp_path):
        # A .txt file explicitly declared as csv should still route tabular.
        p = tmp_path / "data.txt"
        p.write_text("a,b\n1,2\n")
        assert file_inspector.classify_path(str(p), format="csv") == {"route": "tabular", "format": "csv"}

    def test_extensionless_xml_content_sniffed(self, tmp_path):
        p = tmp_path / "noext"
        p.write_text("<root><a/></root>")
        assert file_inspector.classify_path(str(p)) == {"route": "generic", "format": "xml"}

    def test_extensionless_csv_like_content_falls_back_to_tabular_csv(self, tmp_path):
        p = tmp_path / "noext"
        p.write_text("a,b,c\n1,2,3\n")
        assert file_inspector.classify_path(str(p)) == {"route": "tabular", "format": "csv"}

    def test_invalid_format_is_malformed_error(self, tmp_path):
        p = tmp_path / "data.txt"
        p.write_text("x")
        result = file_inspector.classify_path(str(p), format="yaml")
        assert result["error"]["code"] == "malformed"
        assert result["error"]["reason"] == "invalid_format"


# ---------------------------------------------------------------------------
# Path-policy preflight (via inspect_local_file, which runs it before ANY
# subprocess is spawned -- these tests never touch a sibling process).
# ---------------------------------------------------------------------------


class TestPreflightPolicy:
    def test_secret_named_file_is_denied(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("SECRET=1")
        result = file_inspector.inspect_local_file(str(p))
        assert result["state"] == "failed"
        assert result["errors"][0]["code"] == "denied"
        assert result["errors"][0]["reason"] == "secret_path_excluded"
        assert result["local_only"] is True

    def test_missing_file_is_denied_not_found(self, tmp_path):
        result = file_inspector.inspect_local_file(str(tmp_path / "nope.xml"))
        assert result["errors"][0]["reason"] == "not_found"

    def test_directory_is_denied(self, tmp_path):
        result = file_inspector.inspect_local_file(str(tmp_path))
        assert result["errors"][0]["reason"] == "is_a_directory"

    def test_outside_allowed_root_is_denied(self, tmp_path):
        outside = tmp_path / "outside.xml"
        outside.write_text("<root/>")
        allowed = tmp_path / "allowed_subdir"
        allowed.mkdir()
        result = file_inspector.inspect_local_file(str(outside), allowed_root=str(allowed))
        assert result["errors"][0]["reason"] == "outside_allowed_root"

    def test_oversized_file_is_limit_exceeded(self, tmp_path):
        p = tmp_path / "big.xml"
        p.write_bytes(b"<root/>" + b" " * 1000)
        result = file_inspector.inspect_local_file(str(p), max_bytes=10)
        assert result["errors"][0]["code"] == "limit_exceeded"

    def test_invalid_operation_is_rejected(self, tmp_path):
        p = tmp_path / "data.xml"
        p.write_text("<root/>")
        result = file_inspector.inspect_local_file(str(p), operation="bogus")
        assert result["errors"][0]["reason"] == "invalid_operation"

    def test_source_ref_is_redacted_never_the_raw_absolute_path(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("x")
        result = file_inspector.inspect_local_file(str(p))
        assert result["source_ref"] != str(p)
        assert result["source_ref"].endswith(".env")
        assert not os.path.isabs(result["source_ref"])


# ---------------------------------------------------------------------------
# Real end-to-end routing -- actually spawns the sibling MCP subprocess.
# ---------------------------------------------------------------------------


class TestEndToEndRouting:
    def test_xml_routes_to_generic_and_completes(self, tmp_path):
        p = tmp_path / "data.xml"
        p.write_text('<?xml version="1.0"?><root><a>1</a><b>2</b></root>')
        result = file_inspector.inspect_local_file(str(p), operation="preview")
        assert result["route"] == "generic"
        assert result["format"] == "xml"
        assert result["state"] == "complete", result["errors"]
        assert result["local_only"] is True
        assert result["parser_id"] == "lxml-xml-secure"
        assert result["source_sha256"]
        assert result["result_hash"]

    def test_csv_routes_to_tabular_and_completes(self, tmp_path):
        p = tmp_path / "data.csv"
        p.write_text("name,age\nalice,30\nbob,40\n")
        result = file_inspector.inspect_local_file(str(p), operation="preview")
        assert result["route"] == "tabular"
        assert result["format"] == "csv"
        assert result["state"] == "complete", result["errors"]
        assert result["shape"]["column_count"] == 2
        assert result["shape"]["row_count"]["value"] == 2

    def test_json_rows_route_to_tabular(self, tmp_path):
        p = tmp_path / "rows.json"
        p.write_text(json.dumps([{"x": 1}, {"x": 2}, {"x": 3}]))
        result = file_inspector.inspect_local_file(str(p), operation="preview")
        assert result["route"] == "tabular"
        assert result["state"] == "complete", result["errors"]
        assert result["shape"]["row_count"]["value"] == 3

    def test_json_object_routes_to_generic(self, tmp_path):
        p = tmp_path / "obj.json"
        p.write_text(json.dumps({"a": 1, "b": {"c": 2}}))
        result = file_inspector.inspect_local_file(str(p), operation="preview")
        assert result["route"] == "generic"
        assert result["state"] == "complete", result["errors"]
        assert result["shape"]["root_kind"] == "object"

    def test_docx_metadata_operation_uses_bounded_outline(self, tmp_path):
        p = tmp_path / "report.docx"
        _make_docx(p)
        result = file_inspector.inspect_local_file(str(p), operation="metadata")
        assert result["route"] == "docs"
        assert result["format"] == "docx"
        assert result["state"] == "complete", result["errors"]
        assert result["parser_id"] == "meridian-docs:document_outline"
        # metadata tier strips the headings listing itself -- only small
        # scalar facts should remain.
        assert "headings" not in result["shape"]
        assert result["source_sha256"]

    def test_docx_shape_operation_keeps_headings(self, tmp_path):
        p = tmp_path / "report.docx"
        _make_docx(p)
        result = file_inspector.inspect_local_file(str(p), operation="shape")
        assert result["state"] == "complete", result["errors"]
        assert result["shape"]["heading_count"] == 2
        assert len(result["shape"]["headings"]) == 2

    def test_docx_preview_operation_reads_paragraphs_via_snapshot(self, tmp_path):
        p = tmp_path / "report.docx"
        _make_docx(p)
        result = file_inspector.inspect_local_file(str(p), operation="preview")
        assert result["state"] == "complete", result["errors"]
        assert result["parser_id"] == "meridian-docs:read_document_snapshot"
        assert "paragraphs" in result["shape"]

    def test_malformed_docx_reports_failed_not_unavailable(self, tmp_path):
        """A .docx-named file that isn't a real ZIP makes document_outline
        RAISE (parse_docx -> ValueError), surfaced by FastMCP as an
        MCP-level tool error. The router must map this to STATE_FAILED
        (a real, deterministic parse failure) -- never STATE_UNAVAILABLE,
        which is reserved for the sibling process being unreachable."""
        p = tmp_path / "broken.docx"
        p.write_bytes(b"this is not a zip file at all")
        result = file_inspector.inspect_local_file(str(p), operation="metadata")
        assert result["state"] == "failed", result
        assert result["errors"][0]["code"] == "malformed"
        assert result["local_only"] is True

    def test_operation_metadata_strips_heavy_fields_for_tabular(self, tmp_path):
        p = tmp_path / "data.csv"
        p.write_text("name,age\nalice,30\n")
        result = file_inspector.inspect_local_file(str(p), operation="metadata")
        assert result["state"] == "complete", result["errors"]
        assert "columns" not in result["shape"]
        assert "sample_rows" not in result["shape"]

    def test_operation_shape_keeps_columns_but_not_sample_rows(self, tmp_path):
        p = tmp_path / "data.csv"
        p.write_text("name,age\nalice,30\n")
        result = file_inspector.inspect_local_file(str(p), operation="shape")
        assert result["state"] == "complete", result["errors"]
        assert "columns" in result["shape"]
        assert "sample_rows" not in result["shape"]

    def test_operation_preview_keeps_sample_rows(self, tmp_path):
        p = tmp_path / "data.csv"
        p.write_text("name,age\nalice,30\n")
        result = file_inspector.inspect_local_file(str(p), operation="preview")
        assert result["state"] == "complete", result["errors"]
        assert "sample_rows" in result["shape"]


# ---------------------------------------------------------------------------
# unavailable state -- sibling process cannot be reached at all
# ---------------------------------------------------------------------------


class TestUnavailable:
    def test_missing_sibling_directory_reports_unavailable(self, tmp_path):
        p = tmp_path / "data.xml"
        p.write_text("<root/>")
        bogus_dir = tmp_path / "no_such_sibling_package"
        result = file_inspector.inspect_local_file(
            str(p), file_inspection_dir=str(bogus_dir),
        )
        assert result["state"] == "unavailable", result
        assert result["errors"][0]["code"] == "unavailable"
        assert result["errors"][0]["reason"] == "sibling_process_unreachable"
        assert result["local_only"] is True

    def test_missing_docs_sibling_directory_reports_unavailable(self, tmp_path):
        p = tmp_path / "report.docx"
        _make_docx(p)
        bogus_dir = tmp_path / "no_such_docs_package"
        result = file_inspector.inspect_local_file(str(p), docs_dir=str(bogus_dir))
        assert result["state"] == "unavailable", result
        assert result["errors"][0]["code"] == "unavailable"
