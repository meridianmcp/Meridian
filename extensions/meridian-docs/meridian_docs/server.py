"""Thin MCP stdio server exposing the docs_intel DOCX parser as tools.

Run with ``uvx meridian-docs`` (console entry point) or
``python -m meridian_docs.server``. Every tool delegates to the vendored,
stdlib-only :mod:`meridian_docs.docs_intel`.

fdbd4296 — also exposes :func:`ingest_local_document`, a tunnel-routed
wrapper that reads a local file, extracts its full text programmatically, and
forwards the text to the hosted Meridian ``ingest_document`` tool as a single
call.  Eliminates the lossy two-step manual workaround (local read + hand-copy
into ``ingest_document(content=...)``) that was observed on four hosted-only
tools tonight.  NOTE (db42acce): this only populates the flat note store —
see ``ingest_local_document_structure`` for the structural doc-store path.

db42acce — also exposes :func:`ingest_local_document_structure`, a tunnel-routed
wrapper that parses a local .docx's structural content (headings/figures/tables)
via ``docparse.docs_intel.document_content_tree`` and forwards the resulting
blocks to the hosted ``ingest_document_structure`` MCP tool, which stores them
into the doc-structure store.  This makes find_similar_figure / index_figure /
index_table / index_equation work correctly on locally-stored .docx files from
hosted Meridian — the gap that fdbd4296 alone could NOT close.
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
def index_document_structure(path: str, index_db_path: str) -> dict[str, Any]:
    """c39ae092 — parse a .docx and store structural elements (headings/figures/tables)
    into the sidecar SQLite index at index_db_path (local, no network call).

    Extends the same sidecar DB used by index_document / search_paragraphs with
    three new tables: docx_headings, docx_figures, docx_tables.  Figures are
    detected by SEQ Figure field codes; tables by raw <w:tbl> blocks plus optional
    SEQ Table captions.

    Returns {index_db, heading_count, figure_count, table_count}.
    """
    return docs_intel.index_docx_structure(path, index_db_path)


@mcp.tool()
def get_structure_elements(index_db_path: str) -> dict[str, Any]:
    """c39ae092 — retrieve all locally-stored structural elements from the sidecar.

    Returns {headings, figures, tables} lists from the docx_headings,
    docx_figures, docx_tables tables populated by index_document_structure.
    Returns empty lists for any table not yet populated.
    """
    return docs_intel.get_local_structure_elements(index_db_path)


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

    SCOPE LIMITATION (db42acce): this tool populates the FLAT note store ONLY
    (searchable via search_all / search_synthesis). The structural doc-store
    (headings tree, doc_figures, doc_tables, doc_equations) is NOT populated —
    structural elements require parsing the .docx binary, not just its text.
    To also populate the structural doc-store (so find_similar_figure returns a
    real document_id), call ``ingest_local_document_structure`` with the SAME
    path and source after this call.

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


@mcp.tool()
def ingest_local_document_structure(
    path: str,
    project_id: str,
    title: str | None = None,
    source: str | None = None,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """db42acce/c39ae092 — parse a local .docx's structural content and persist it.

    TWO PATHS:

    1. LOCAL SIDECAR (c39ae092, preferred) — supply ``index_db_path`` to store
       headings/figures/tables into a local SQLite sidecar (same DB used by
       ``index_document`` / ``search_paragraphs``).  NO network call — immune to
       Cloudflare 403 blocks that affect the hosted POST path.

    2. HOSTED POST (db42acce, legacy fallback) — when ``index_db_path`` is
       omitted, blocks are forwarded to the hosted ``ingest_document_structure``
       MCP tool.  Subject to Cloudflare 403 on blocked IPs.

    The structural complement to ``ingest_local_document``.  Where that tool can
    only forward plain text (populating the flat note store), this tool parses
    the REAL .docx binary locally to extract its structural tree
    (headings/figures/tables in true document order).

    The ``source`` MUST match the source used in any prior
    ``ingest_local_document`` call for the same file (default: ``path`` for
    both), so that ``find_similar_figure`` / ``index_figure`` / ``index_table``
    can look up the same ``document_id`` / source key.

    Args:
      path:           Local .docx file path.
      project_id:     Meridian project UUID (used only on hosted path).
      title:          Document title (optional).
      source:         Source key (defaults to ``path``).
      index_db_path:  Path to the local sidecar SQLite index (enables local
                      storage path — no network call).

    Returns (local path):  ``{index_db, source, heading_count, figure_count,
                              table_count, local_path}``.
    Returns (hosted path): ``{document_id, source, doc_type, element_count,
                              local_path, blocks_forwarded}``.
    """
    return local_ingest.ingest_local_document_structure(
        path=path,
        project_id=project_id,
        title=title,
        source=source,
        index_db_path=index_db_path,
    )


def main() -> None:
    """Console entry point (``uvx meridian-docs``)."""
    mcp.run()


if __name__ == "__main__":
    main()
