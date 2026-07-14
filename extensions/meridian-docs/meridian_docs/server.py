"""Thin MCP stdio server exposing the docs_intel DOCX parser as tools.

Run with ``uvx meridian-docs`` (console entry point) or
``python -m meridian_docs.server``. Every tool delegates to the vendored,
stdlib-only :mod:`meridian_docs.docs_intel`.

fdbd4296 — also exposes :func:`ingest_local_document`, a tunnel-routed
wrapper that reads a local file, extracts its full text programmatically, and
forwards the text to the hosted Meridian ``ingest_document`` tool as a single
call.  Eliminates the lossy two-step manual workaround (local read + hand-copy
into ``ingest_document(content=...)``) that was observed on four hosted-only
tools tonight.
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import docs_intel
from . import local_ingest

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


@mcp.tool()
def ingest_local_document(
    path: str,
    project_id: str,
    title: str | None = None,
    source: str | None = None,
    tags: str | None = None,
) -> dict[str, Any]:
    """fdbd4296 — read a local file, extract its full text, and ingest it into Meridian.

    This is the single-call replacement for the two-step manual workaround
    required when the hosted ``ingest_document`` tool is called with a local
    file path (it runs on Fly.io and has no access to the caller's machine).

    Supported file types (stdlib only, no third-party deps):
      - .docx  -- OOXML paragraph text extracted via zipfile + ElementTree.
      - .txt / .md / .markdown / common source extensions -- read as UTF-8.

    The extracted text is forwarded to the hosted Meridian ``ingest_document``
    tool via an authenticated HTTP call (``MERIDIAN_URL`` + ``MERIDIAN_API_KEY``
    or ``BEARER_TOKEN`` from the tunnel process environment).

    Args:
      path:        Local file path (.docx / .txt / .md / source file).
      project_id:  Meridian project UUID to ingest into.
      title:       Note title (defaults to the file basename on the server).
      source:      Provenance label stored on the note (defaults to ``path``).
      tags:        Comma-separated tags.

    Returns:
      The ingested note from Meridian (id, slug, title, source) plus
      ``chars_extracted`` (int) and ``local_path`` (str).
    """
    return local_ingest.ingest_local_document(
        path=path,
        project_id=project_id,
        title=title,
        source=source,
        tags=tags,
    )


def main() -> None:
    """Console entry point (``uvx meridian-docs``)."""
    mcp.run()


if __name__ == "__main__":
    main()
