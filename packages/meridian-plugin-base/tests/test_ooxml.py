"""Tests for meridian_plugin_base.ooxml.document_content_tree.

Uses a synthetic in-memory .docx (a minimal OOXML ZIP with one heading and two
paragraphs) — no real .docx file required. Mirrors the test pattern used in the
main meridian test suite for the same function (via docparse).
"""
from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

from meridian_plugin_base.ooxml import (
    _W,
    _W14,
    _is_heading,
    _heading_level,
    _paragraph_text,
    document_content_tree,
)


# ---------------------------------------------------------------------------
# Helpers to build a minimal synthetic .docx
# ---------------------------------------------------------------------------

def _make_docx(body_xml: str) -> bytes:
    """Wrap raw body XML in a minimal OOXML document.xml and return .docx bytes."""
    doc_xml = f"""\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
{body_xml}
  </w:body>
</w:document>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", doc_xml)
    return buf.getvalue()


def _heading_para(text: str, level: int = 1, para_id: str | None = None) -> str:
    pid_attr = f' w14:paraId="{para_id}"' if para_id else ""
    return (
        f'<w:p{pid_attr}>'
        f'<w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>'
        f'<w:r><w:t>{text}</w:t></w:r>'
        f"</w:p>"
    )


def _plain_para(text: str, para_id: str | None = None) -> str:
    pid_attr = f' w14:paraId="{para_id}"' if para_id else ""
    return f'<w:p{pid_attr}><w:r><w:t>{text}</w:t></w:r></w:p>'


def _table_block(rows: list[list[str]]) -> str:
    """Build a minimal <w:tbl> XML string."""
    row_strs = []
    for row in rows:
        cells = "".join(
            f'<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>'
            for cell in row
        )
        row_strs.append(f"<w:tr>{cells}</w:tr>")
    return "<w:tbl>" + "".join(row_strs) + "</w:tbl>"


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_is_heading_true(self) -> None:
        assert _is_heading("Heading1") is True
        assert _is_heading("heading 2") is True
        assert _is_heading("HEADING3") is True

    def test_is_heading_false(self) -> None:
        assert _is_heading(None) is False
        assert _is_heading("Normal") is False
        assert _is_heading("") is False

    def test_heading_level(self) -> None:
        assert _heading_level("Heading1") == 1
        assert _heading_level("Heading3") == 3
        assert _heading_level("heading 2") == 2
        assert _heading_level(None) == 1   # default


class TestDocumentContentTree:
    def test_empty_document(self) -> None:
        """A document with no body paragraphs returns zero counts."""
        docx = _make_docx("")
        result = document_content_tree(docx)
        assert result["paragraph_count"] == 0
        assert result["heading_count"] == 0
        assert result["table_count"] == 0
        assert result["blocks"] == []
        assert result["tree"] == []

    def test_single_heading(self) -> None:
        body = _heading_para("Introduction", level=1, para_id="abc123")
        docx = _make_docx(body)
        result = document_content_tree(docx)
        assert result["heading_count"] == 1
        assert result["paragraph_count"] == 1  # headings count as paragraphs
        assert len(result["blocks"]) == 1
        block = result["blocks"][0]
        assert block["kind"] == "heading"
        assert block["level"] == 1
        assert block["text"] == "Introduction"
        assert block["para_id"] == "abc123"

    def test_heading_and_paragraphs(self) -> None:
        body = (
            _heading_para("Chapter 1", level=1)
            + _plain_para("First paragraph.")
            + _plain_para("Second paragraph.")
        )
        docx = _make_docx(body)
        result = document_content_tree(docx)
        assert result["heading_count"] == 1
        assert result["paragraph_count"] == 3  # 1 heading + 2 plain
        assert result["table_count"] == 0

    def test_tree_nesting(self) -> None:
        """Paragraphs beneath a heading are nested as children in the tree."""
        body = (
            _heading_para("Section 1", level=1)
            + _plain_para("Body text under section 1.")
            + _heading_para("Subsection 1.1", level=2)
            + _plain_para("Body text under 1.1.")
        )
        docx = _make_docx(body)
        result = document_content_tree(docx)
        tree = result["tree"]
        assert len(tree) == 1  # one top-level heading
        h1 = tree[0]
        assert h1["text"] == "Section 1"
        # Children: body text + the subsection heading node
        assert len(h1["children"]) == 2
        assert h1["children"][0]["kind"] == "paragraph"
        h11 = h1["children"][1]
        assert h11["kind"] == "heading"
        assert h11["level"] == 2
        assert h11["text"] == "Subsection 1.1"
        # Body text under 1.1 is a child of the subsection
        assert len(h11["children"]) == 1
        assert h11["children"][0]["text"] == "Body text under 1.1."

    def test_table_block(self) -> None:
        body = _table_block([["A", "B"], ["C", "D"]])
        docx = _make_docx(body)
        result = document_content_tree(docx)
        assert result["table_count"] == 1
        assert result["paragraph_count"] == 0
        tbl = result["blocks"][0]
        assert tbl["kind"] == "table"
        assert tbl["row_count"] == 2
        assert tbl["col_count"] == 2
        assert tbl["rows"] == [["A", "B"], ["C", "D"]]

    def test_bytes_input(self) -> None:
        """document_content_tree accepts raw bytes as well as file paths."""
        body = _plain_para("Hello from bytes.")
        docx_bytes = _make_docx(body)
        result = document_content_tree(docx_bytes)  # bytes input
        assert result["paragraph_count"] == 1
        assert result["blocks"][0]["text"] == "Hello from bytes."

    def test_synthesized_para_id_when_absent(self) -> None:
        """When w14:paraId is absent a synthesized p{index} id is assigned."""
        body = _plain_para("No paraId here.")  # no para_id kwarg
        docx = _make_docx(body)
        result = document_content_tree(docx)
        block = result["blocks"][0]
        assert block["para_id"].startswith("p")

    def test_field_count(self) -> None:
        """field_count sums fields across all blocks."""
        # A paragraph with a simple field (fldSimple)
        body_xml = (
            '<w:p><w:fldSimple w:instr="PAGE">'
            '<w:r><w:t>1</w:t></w:r>'
            "</w:fldSimple></w:p>"
        )
        docx = _make_docx(body_xml)
        result = document_content_tree(docx)
        assert result["field_count"] >= 1


class TestIngestClientImport:
    """Smoke-test that ingest_client is importable and has the expected public names."""

    def test_imports(self) -> None:
        from meridian_plugin_base import ingest_client  # noqa: F401
        from meridian_plugin_base.ingest_client import (
            MeridianClientError,
            call_mcp_tool,
            call_hosted_ingest,
            call_hosted_ingest_structure,
            resolve_base_url,
            resolve_token,
        )
        # resolve_base_url should return a non-empty string
        assert resolve_base_url().startswith("http")

    def test_resolve_token_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from meridian_plugin_base.ingest_client import resolve_token
        monkeypatch.setenv("MERIDIAN_API_KEY", "test-key")
        assert resolve_token() == "test-key"

    def test_resolve_token_bearer_strip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from meridian_plugin_base.ingest_client import resolve_token
        monkeypatch.delenv("MERIDIAN_API_KEY", raising=False)
        monkeypatch.setenv("BEARER_TOKEN", "Bearer sk_test_abc")
        assert resolve_token() == "sk_test_abc"
