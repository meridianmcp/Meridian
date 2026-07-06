"""Thin MCP stdio server exposing the docs_intel DOCX parser as tools.

Run with ``uvx meridian-docs`` (console entry point) or
``python -m meridian_docs.server``. Every tool delegates to the vendored,
stdlib-only :mod:`meridian_docs.docs_intel`.
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import docs_intel

mcp = FastMCP("meridian-docs")


@mcp.tool()
def document_outline(path: str) -> dict[str, Any]:
    """Heading outline of a .docx: paragraph_count, heading_count, and an ordered
    list of headings (level, text, para_id). Stateless — builds no index."""
    return docs_intel.document_outline(path)


@mcp.tool()
def parse_document(path: str) -> list[dict[str, Any]]:
    """Parse a .docx into ordered paragraph records ({index, para_id, style, text})."""
    return docs_intel.parse_docx(path)


@mcp.tool()
def index_document(path: str, index_db_path: str) -> dict[str, Any]:
    """Build a sidecar SQLite index for a .docx so paragraphs are navigable by id."""
    return docs_intel.index_docx(path, index_db_path)


@mcp.tool()
def get_structure(index_db_path: str) -> list[dict[str, Any]]:
    """Return the heading outline from a previously-built index."""
    return docs_intel.get_structure(index_db_path)


@mcp.tool()
def get_paragraph(index_db_path: str, para_id: str) -> dict[str, Any] | None:
    """Fetch one paragraph by its para_id from a previously-built index."""
    return docs_intel.get_paragraph(index_db_path, para_id)


@mcp.tool()
def search_paragraphs(index_db_path: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Substring-search paragraphs in a previously-built index."""
    return docs_intel.find_paragraphs(index_db_path, query, limit)


def main() -> None:
    """Console entry point (``uvx meridian-docs``)."""
    mcp.run()


if __name__ == "__main__":
    main()
