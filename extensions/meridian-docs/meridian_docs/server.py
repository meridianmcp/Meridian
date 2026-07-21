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
    force_hosted: bool = False,
) -> dict[str, Any]:
    """db42acce/c39ae092/f8c7ffdc — parse a local .docx's structural content and persist it.

    TWO PATHS — hosted routing is OPT-IN (f8c7ffdc):

    1. LOCAL SIDECAR (DEFAULT, no network) — supply ``index_db_path`` to store
       headings/figures/tables into a local SQLite sidecar (same DB used by
       ``index_document`` / ``search_paragraphs``).  NO network call — immune to
       Cloudflare 403 blocks and the 100 KB hosted body cap.

    2. HOSTED POST (explicit opt-in only) — when ``index_db_path`` is omitted
       AND ``force_hosted=True`` is set, blocks are forwarded to the hosted
       ``ingest_document_structure`` MCP tool.  Subject to Cloudflare 403 on
       blocked IPs and a 100 KB body cap.  If ``index_db_path`` is None and
       ``force_hosted`` is False (the default), an error is raised — the hosted
       path is NEVER the silent default.

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
      index_db_path:  Path to the local sidecar SQLite index (recommended —
                      enables local storage path, no network call).
      force_hosted:   Set True to explicitly use the hosted POST path when
                      index_db_path is None.  Default False — hosted routing is
                      never the silent default.

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
        force_hosted=force_hosted,
    )


@mcp.tool()
def find_image_paragraph(
    docx_path: str,
    figure_index: int | None = None,
) -> dict[str, Any]:
    """Scan a .docx for paragraphs that contain an embedded image.

    Detects both DrawingML (<w:drawing>) and legacy VML (<w:pict>) image
    paragraphs.  Use this to find the correct anchor_para_id before calling
    insert_caption with kind="Figure" — passing the image paragraph's own
    para_id (not the preceding paragraph) ensures the caption lands BELOW
    the image, not above it.

    Args:
      docx_path:    Absolute path to the .docx file.
      figure_index: 1-based index selecting which image to return when the
                    document has multiple images.  None (default) returns ALL
                    image paragraphs as a list.

    Returns (figure_index=None):
      {image_paragraphs: [{para_id, index, text}], count: int}
    Returns (figure_index given):
      {para_id, index, text, figure_index: int}
    Returns on error:
      {error: <message>}
    """
    return docs_intel.find_image_paragraph(
        docx_path=docx_path,
        figure_index=figure_index,
    )


@mcp.tool()
def insert_caption(
    docx_path: str,
    anchor_para_id: str,
    kind: str,
    label_text: str,
    position: str = "after",
    section_heading: str | None = None,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """9d749639 — Insert a real Word Caption paragraph into a .docx file.

    Writes a new paragraph with style Caption and a SEQ Figure / SEQ Table
    field directly into word/document.xml, then re-packs the ZIP preserving
    all other members.  The SEQ number is auto-incremented (count of existing
    same-kind captions + 1).

    Both Figure and Table captions use the same Word Caption-style + SEQ
    mechanism — the ``kind`` parameter selects which counter to use.

    Args:
      docx_path:       Absolute path to the .docx file (mutated in place).
      anchor_para_id:  w14:paraId or p{N} of the paragraph/table to anchor on.
      kind:            "Figure" or "Table".
      label_text:      Caption label text (e.g. "Loss curve for run 42").
                       Rendered text will be e.g. "Figure 1. Loss curve...".
      position:        "after" (default) or "before".
      section_heading: Optional section heading for organizational association.
                       Stored in the sidecar index section column.
      index_db_path:   If supplied, sidecar is invalidated after write so the
                       next read auto-reindexes (keeps metadata in sync).

    Returns:
      {status, kind, seq_number, label_text, section_heading, docx_path}
      or {error: <message>} on failure (file NOT mutated on error).
    """
    return docs_intel.insert_caption(
        docx_path=docx_path,
        anchor_para_id=anchor_para_id,
        kind=kind,
        label_text=label_text,
        position=position,
        section_heading=section_heading,
        index_db_path=index_db_path,
    )


@mcp.tool()
def edit_caption(
    docx_path: str,
    caption_para_id: str,
    new_label_text: str,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """9d749639 — Edit the label text of an existing Word Caption paragraph.

    Locates the paragraph by caption_para_id, verifies it is a Caption
    paragraph (Caption style or SEQ field present), replaces the label text
    run while preserving the SEQ field and style.  The SEQ number is NOT
    changed so Word's field-refresh cycle continues to work correctly.

    Args:
      docx_path:       Absolute path to the .docx file (mutated in place).
      caption_para_id: w14:paraId or p{N} of the Caption paragraph.
      new_label_text:  Replacement label text.
      index_db_path:   If supplied, sidecar is invalidated after write.

    Returns:
      {status, caption_para_id, new_label_text, docx_path}
      or {error: <message>} on failure.
    """
    return docs_intel.edit_caption(
        docx_path=docx_path,
        caption_para_id=caption_para_id,
        new_label_text=new_label_text,
        index_db_path=index_db_path,
    )


@mcp.tool()
def remove_caption(
    docx_path: str,
    caption_para_id: str,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """9d749639 — Remove a Caption paragraph from a .docx file.

    Locates the paragraph by caption_para_id, verifies it is a Caption
    paragraph (Caption style or SEQ field present), removes it from the body,
    and re-packs the ZIP.

    Args:
      docx_path:       Absolute path to the .docx file (mutated in place).
      caption_para_id: w14:paraId or p{N} of the Caption paragraph.
      index_db_path:   If supplied, sidecar is invalidated after write.

    Returns:
      {status, caption_para_id, docx_path}
      or {error: <message>} on failure.
    """
    return docs_intel.remove_caption(
        docx_path=docx_path,
        caption_para_id=caption_para_id,
        index_db_path=index_db_path,
    )


@mcp.tool()
def insert_cross_reference(
    docx_path: str,
    anchor_para_id: str,
    target_caption_para_id: str | None = None,
    bookmark_name: str | None = None,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """1c59cb90 — Insert a live Word REF-field cross-reference into a .docx file.

    REFILED (original 7b5bfb00) — insert_caption above only gives captions
    Word's SEQ-field auto-numbering. This adds the other half: a REF field in
    prose ELSEWHERE that quotes a caption's number (e.g. "as shown in Figure
    3"). A hand-typed "Figure 3" string goes stale the instant captions are
    reordered; this REF field tracks the SAME field-refresh cycle (F9) that
    keeps the caption's own SEQ number correct, because it targets the
    caption's own cross-reference bookmark rather than a fixed string.

    Appends a REF complex field to the paragraph at anchor_para_id, with
    cached display text like "Figure 3". Identify the target caption EITHER
    way (exactly one required):
      - target_caption_para_id: the caption paragraph's w14:paraId / p{N}.
        If it has no _Ref bookmark yet (predates this feature), one is
        created now as part of the same write.
      - bookmark_name: an existing _Ref<digits> bookmark, e.g. the
        ref_bookmark field returned by a prior insert_caption call.

    A separating space is inserted first if the anchor paragraph's existing
    text doesn't already end in whitespace, so the field reads naturally as
    trailing prose.

    Args:
      docx_path:              Absolute path to the .docx file (mutated in place).
      anchor_para_id:         w14:paraId or p{N} of the paragraph the field is
                               appended into.
      target_caption_para_id: w14:paraId or p{N} of the Figure/Table Caption
                               paragraph being referenced.
      bookmark_name:          Alternative to target_caption_para_id — an
                               existing _Ref<digits> bookmark name.
      index_db_path:          If supplied, sidecar is invalidated after write.

    Returns:
      {status, anchor_para_id, bookmark_name, kind, seq_number, display_text,
      docx_path} or {error: <message>} on failure (file NOT mutated on error).
    """
    return docs_intel.insert_cross_reference(
        docx_path=docx_path,
        anchor_para_id=anchor_para_id,
        target_caption_para_id=target_caption_para_id,
        bookmark_name=bookmark_name,
        index_db_path=index_db_path,
    )


@mcp.tool()
def insert_citation(
    docx_path: str,
    anchor_para_id: str,
    citation_keys: list[str],
    formatted_text: str,
    source: str = "zotero",
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """9d749639 — Insert a real CSL_CITATION complex field into a .docx paragraph.

    Appends the citation complex field (begin / instrText / separate / cached /
    end) to the end of the target paragraph.  The field instruction is
    "ADDIN ZOTERO_ITEM CSL_CITATION {...}" (Zotero) or "ADDIN CSL_CITATION {...}"
    (generic CSL), making it recognisable by Zotero/Mendeley on document open and
    by the extraction side (CSL_CITATION token in docparse.docs_intel).

    This is the write counterpart of the read-side citation extraction already
    present in packages/docparse.  The bibliography write path (1258794a) depends
    on this producing recognisable citation fields.

    Args:
      docx_path:       Absolute path to the .docx file (mutated in place).
      anchor_para_id:  w14:paraId or p{N} of the paragraph to cite in.
      citation_keys:   One or more stable citation identifiers (DOI, URI, etc.).
      formatted_text:  Rendered in-text marker (e.g. "(Smith et al., 2023)").
      source:          "zotero" (default) or "csl".
      index_db_path:   If supplied, sidecar is invalidated after write.

    Returns:
      {status, anchor_para_id, citation_keys, formatted_text, source, docx_path}
      or {error: <message>} on failure.
    """
    return docs_intel.insert_citation(
        docx_path=docx_path,
        anchor_para_id=anchor_para_id,
        citation_keys=citation_keys,
        formatted_text=formatted_text,
        source=source,
        index_db_path=index_db_path,
    )


@mcp.tool()
def edit_citation(
    docx_path: str,
    anchor_para_id: str,
    new_citation_keys: list[str] | None = None,
    new_formatted_text: str | None = None,
    source: str = "zotero",
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """9d749639 — Replace an existing CSL_CITATION field with updated keys/text.

    Locates the first complex field in the paragraph whose instrText contains
    CSL_CITATION, removes the old field runs (begin through end), and inserts a
    new complex field with the updated keys / formatted text in their place.

    At least one of new_citation_keys or new_formatted_text must be supplied.
    When only one is given the other is inferred from the existing field.

    Args:
      docx_path:          Absolute path to the .docx file (mutated in place).
      anchor_para_id:     w14:paraId or p{N} of the paragraph to edit.
      new_citation_keys:  Replacement citation keys (None = keep existing).
      new_formatted_text: Replacement display text (None = keep existing).
      source:             "zotero" or "csl".
      index_db_path:      If supplied, sidecar is invalidated after write.

    Returns:
      {status, anchor_para_id, citation_keys, formatted_text, source, docx_path}
      or {error: <message>} on failure.
    """
    return docs_intel.edit_citation(
        docx_path=docx_path,
        anchor_para_id=anchor_para_id,
        new_citation_keys=new_citation_keys,
        new_formatted_text=new_formatted_text,
        source=source,
        index_db_path=index_db_path,
    )


@mcp.tool()
def remove_citation(
    docx_path: str,
    anchor_para_id: str,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """9d749639 — Remove the first CSL_CITATION complex field from a paragraph.

    Locates the field by scanning for a complex field (fldChar begin...end)
    whose instrText contains CSL_CITATION, removes all its constituent runs,
    and re-packs the ZIP.

    Args:
      docx_path:       Absolute path to the .docx file (mutated in place).
      anchor_para_id:  w14:paraId or p{N} of the paragraph to edit.
      index_db_path:   If supplied, sidecar is invalidated after write.

    Returns:
      {status, anchor_para_id, docx_path}
      or {error: <message>} on failure.
    """
    return docs_intel.remove_citation(
        docx_path=docx_path,
        anchor_para_id=anchor_para_id,
        index_db_path=index_db_path,
    )


@mcp.tool()
def extract_equations(path: str) -> list[dict[str, Any]]:
    """a80af3a0 — Parse all OMML equations out of a local .docx (stdlib only, no lxml).

    Detects two patterns:
      1. Standalone: an <m:oMath> inside a regular <w:p> body paragraph.
      2. Table-numbered: a 2-column <w:tbl> row where the first cell contains
         an <m:oMath> and the second cell holds a parenthesised equation number
         like "(1)" or "(2a)".  The number is attached as the "number" field.

    Each record: {ordinal, para_id, omml_raw, pattern, number, flat_text}.
    """
    return docs_intel.parse_docx_equations_local(path)


@mcp.tool()
def index_equations(path: str, index_db_path: str) -> dict[str, Any]:
    """a80af3a0 — Parse equations from a .docx and store them in the sidecar SQLite.

    Extends the sidecar DB at index_db_path with a docx_equations table.
    Idempotent — fully replaces the table on each run.  Call after
    index_document / index_document_structure on the same file.

    Returns {index_db, equation_count}.
    """
    return docs_intel.index_docx_equations(path, index_db_path)


@mcp.tool()
def get_equations(index_db_path: str) -> list[dict[str, Any]]:
    """a80af3a0 — Retrieve all locally-stored equations from the sidecar SQLite.

    Returns a list of equation records in ordinal order from the docx_equations
    table populated by index_equations.  Returns an empty list when no equations
    have been indexed yet.
    """
    return docs_intel.get_local_equations(index_db_path)


@mcp.tool()
def insert_equation(
    docx_path: str,
    anchor_para_id: str,
    payload: str,
    position: str = "after",
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """a80af3a0 — Insert an equation into a .docx file.

    Accepts a raw OMML XML string (starting with "<") or a LaTeX expression
    (e.g. r"\\frac{a}{b}" or "E=mc^2") which is converted to OMML locally
    using latex2mathml (pure Python, no lxml).

    Three positions:
      "before" — new display-mode paragraph immediately before the anchor.
      "after"  — new display-mode paragraph immediately after the anchor.
      "append" — append the <m:oMath> inline to the anchor paragraph itself.

    Args:
      docx_path:       Absolute path to the .docx file (mutated in place).
      anchor_para_id:  w14:paraId or p{N}/tbl{N} to anchor the insertion.
      payload:         Raw OMML XML or LaTeX expression.
      position:        "before", "after" (default), or "append".
      index_db_path:   If supplied, sidecar is invalidated after write.

    Returns:
      {status, position, para_id, omml, docx_path}
      or {error: <message>} on failure (file NOT mutated on error).
    """
    return docs_intel.insert_equation_local(
        docx_path=docx_path,
        anchor_para_id=anchor_para_id,
        payload=payload,
        position=position,
        index_db_path=index_db_path,
    )


@mcp.tool()
def edit_equation(
    docx_path: str,
    equation_para_id: str,
    new_payload: str,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """a80af3a0 — Replace the <m:oMath> in an existing equation paragraph.

    Locates the paragraph by equation_para_id, verifies it contains at least
    one <m:oMath>, removes the existing equation content, and inserts the new
    equation resolved from OMML or LaTeX.

    Args:
      docx_path:         Absolute path to the .docx file (mutated in place).
      equation_para_id:  w14:paraId or p{N} of the equation paragraph.
      new_payload:       Replacement OMML XML or LaTeX expression.
      index_db_path:     If supplied, sidecar is invalidated after write.

    Returns:
      {status, equation_para_id, omml, docx_path}
      or {error: <message>} on failure.
    """
    return docs_intel.edit_equation_local(
        docx_path=docx_path,
        equation_para_id=equation_para_id,
        new_payload=new_payload,
        index_db_path=index_db_path,
    )


@mcp.tool()
def remove_equation(
    docx_path: str,
    equation_para_id: str,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """a80af3a0 — Remove an equation from a .docx file.

    If the paragraph contains ONLY an <m:oMath> (a display-mode equation
    paragraph), the entire paragraph is removed.  If the paragraph also
    contains non-equation text runs (an inline equation in a text paragraph),
    only the <m:oMath> elements are removed, leaving the paragraph's text intact.

    Args:
      docx_path:         Absolute path to the .docx file (mutated in place).
      equation_para_id:  w14:paraId or p{N} of the equation paragraph.
      index_db_path:     If supplied, sidecar is invalidated after write.

    Returns:
      {status, equation_para_id, removed_whole_paragraph, docx_path}
      or {error: <message>} on failure.
    """
    return docs_intel.remove_equation_local(
        docx_path=docx_path,
        equation_para_id=equation_para_id,
        index_db_path=index_db_path,
    )


@mcp.tool()
def scan_citation_keys(docx_path: str) -> list[str]:
    """1258794a — Return all citation keys present in a .docx (in appearance order).

    Walks every paragraph looking for CSL_CITATION complex fields (the same
    fields written by insert_citation) and extracts the citation key from each.
    Keys are deduplicated; first-appearance order is preserved.  Returns [] when
    the document has no in-text citations or cannot be read.

    Args:
      docx_path: Absolute path to the .docx file.

    Returns:
      A list of citation key strings, e.g. ["smith2023", "doi:10.1/x"].
    """
    return docs_intel.scan_all_citation_keys(docx_path)


@mcp.tool()
def format_reference(csl_item: dict) -> str:
    """1258794a — Format a CSL-JSON item as an APA 7th-edition reference string.

    Formats journal articles, books, book chapters, and conference papers
    per APA 7th edition.  Other item types produce a minimal fallback:
    Author (Year). Title.

    This is a pure formatting utility — it does not read or write any file.
    Use it to preview or verify a reference before writing it to a document.

    Args:
      csl_item: A CSL-JSON-shaped dict with at minimum ``author``, ``title``,
                ``type`` (or ``itemType``), and ``issued`` (or ``year``) fields.
                As returned by Zotero's local API or zotero_client.

    Returns:
      The formatted reference string.
    """
    return docs_intel.format_apa_reference(csl_item)


@mcp.tool()
def insert_bibliography_entry(
    docx_path: str,
    citation_key: str,
    csl_item: dict,
    index_db_path: str | None = None,
) -> dict:
    """1258794a — Write a formatted APA bibliography entry into a .docx.

    Locates (or creates) a References heading at the end of the document,
    then appends a new entry paragraph at the end of the references block.
    The entry is formatted from the supplied CSL-JSON item (journal article,
    book, book chapter, or conference paper; other types use a minimal fallback).

    A bookmark (bibkey_<key>) is embedded in the paragraph so that
    update_bibliography_entry and remove_bibliography_entry can locate it.

    If an entry for citation_key already exists, returns an error — use
    update_bibliography_entry to refresh an existing entry.

    The citation_key should match the key used in insert_citation for the
    corresponding in-text marker.

    Args:
      docx_path:     Absolute path to the .docx file (mutated in place).
      citation_key:  Stable citation identifier (DOI, zotero:KEY, citekey, etc.).
      csl_item:      CSL-JSON-shaped item dict (from Zotero local API /
                     zotero_client.resolve_citation_ref + item fetch).
      index_db_path: If supplied, sidecar is invalidated after write.

    Returns:
      {status, citation_key, formatted_text, docx_path}
      or {error: <message>} on failure (file NOT mutated on error).
    """
    return docs_intel.insert_bibliography_entry(
        docx_path=docx_path,
        citation_key=citation_key,
        csl_item=csl_item,
        index_db_path=index_db_path,
    )


@mcp.tool()
def update_bibliography_entry(
    docx_path: str,
    citation_key: str,
    csl_item: dict,
    index_db_path: str | None = None,
) -> dict:
    """1258794a — Refresh the formatted text of an existing bibliography entry.

    Locates the entry paragraph for citation_key by its embedded bookmark
    (bibkey_<key>), re-formats the reference from the updated csl_item,
    and replaces the text run in-place.

    Use this after fetching fresh Zotero data for an already-inserted entry
    (e.g. if the journal or author list changed).

    Args:
      docx_path:     Absolute path to the .docx file (mutated in place).
      citation_key:  The same key used when the entry was inserted.
      csl_item:      Updated CSL-JSON item dict.
      index_db_path: If supplied, sidecar is invalidated after write.

    Returns:
      {status, citation_key, formatted_text, docx_path}
      or {error: <message>} on failure.
    """
    return docs_intel.update_bibliography_entry(
        docx_path=docx_path,
        citation_key=citation_key,
        csl_item=csl_item,
        index_db_path=index_db_path,
    )


@mcp.tool()
def remove_bibliography_entry(
    docx_path: str,
    citation_key: str,
    index_db_path: str | None = None,
) -> dict:
    """1258794a — Remove a bibliography entry paragraph from a .docx.

    Locates the entry by the bibkey_<key> bookmark and removes the entire
    paragraph.  Use this when a citation is deleted from the document body
    and the corresponding reference list entry should be removed.

    Args:
      docx_path:     Absolute path to the .docx file (mutated in place).
      citation_key:  The citation key of the entry to remove.
      index_db_path: If supplied, sidecar is invalidated after write.

    Returns:
      {status, citation_key, docx_path}
      or {error: <message>} on failure.
    """
    return docs_intel.remove_bibliography_entry(
        docx_path=docx_path,
        citation_key=citation_key,
        index_db_path=index_db_path,
    )


@mcp.tool()
def sync_bibliography(
    docx_path: str,
    csl_items: dict,
    index_db_path: str | None = None,
) -> dict:
    """1258794a — Reconcile bibliography entries against in-document citations.

    Scans the document for all in-text citation keys (inserted via
    insert_citation) and reconciles the bibliography section:

      - Keys with csl_items entries but no bibliography paragraph are inserted.
      - Keys with both in-text citations and bibliography entries are updated
        (in case Zotero data changed since the entry was first written).
      - Keys cited in-text but absent from csl_items are reported as
        missing_data (caller must fetch from Zotero and re-call).
      - Keys with bibliography entries but no longer cited in-text are reported
        as stale_entries (caller decides whether to call remove_bibliography_entry).

    Workflow: call scan_citation_keys, then fetch CSL-JSON from Zotero for each
    key (via zotero_client.resolve_citation_ref / the Zotero local API), then
    pass the {key: csl_item} mapping to this tool.

    Args:
      docx_path:   Absolute path to the .docx file (mutated in place).
      csl_items:   Dict mapping citation_key -> CSL-JSON item dict.
      index_db_path: If supplied, sidecar is invalidated after each write.

    Returns:
      {status, inserted, updated, missing_data, stale_entries, docx_path}
      or {error: <message>} on failure.
    """
    return docs_intel.sync_bibliography(
        docx_path=docx_path,
        csl_items=csl_items,
        index_db_path=index_db_path,
    )


@mcp.tool()
def get_section_content(docx_path: str, heading_id: str) -> dict[str, Any]:
    """178a82dd — Targeted read of ONE section's content (no full parse_document dump).

    A "section" is the heading paragraph at heading_id plus every block that
    follows it up to (not including) the next heading at the same or a
    shallower level, or the end of the document. Read-only; builds no index.
    Building block for move_section / copy_section below.

    Args:
      docx_path:  Absolute path to the .docx file.
      heading_id: w14:paraId (or synthesised p{N}) of the section's OWN
                  heading paragraph.

    Returns:
      {heading_id, heading_text, level, start_index, end_index, blocks,
      paragraph_count, table_count, figure_caption_count,
      table_caption_count, docx_path} or {error: <message>}.
    """
    return docs_intel.get_section_content(docx_path=docx_path, heading_id=heading_id)


@mcp.tool()
def find_references_to(docx_path: str, target_id: str) -> dict[str, Any]:
    """fea654f9 — Find everything that points AT a figure/table/heading id.

    The missing inverse of insert_cross_reference: given a target (a
    Figure/Table caption's para_id, a heading's para_id, or an existing
    bookmark name directly), scans the whole document for REF / PAGEREF /
    NOTEREF fields whose instruction targets that same bookmark. Needed for
    safe renumbering/moving without breaking references elsewhere.

    Args:
      docx_path: Absolute path to the .docx file.
      target_id: A caption/heading para_id, or an existing bookmark name.

    Returns:
      {target_id, target_kind, bookmark_names, references, reference_count,
      docx_path} or {error: <message>}.
    """
    return docs_intel.find_references_to(docx_path=docx_path, target_id=target_id)


@mcp.tool()
def scan_stale_notes(docx_path: str) -> dict[str, Any]:
    """563118d4 — Scan a .docx for placeholder/TODO-shaped text that may now be outdated.

    Recurring pattern this catches: an ad-hoc bracket-header or inline note
    (e.g. "[NOTE: currently pending relocation]") that never got updated
    after the thing it describes actually happened. Paragraphs already using
    the structured internal-note style (insert_highlighted_note) are
    excluded — those are already tracked via list_internal_notes.

    Args:
      docx_path: Absolute path to the .docx file.

    Returns:
      {docx_path, findings, finding_count} where each finding is {para_id,
      index, text, matched_terms, bracket_header, section_path}, or
      {error: <message>}.
    """
    return docs_intel.scan_stale_notes(docx_path=docx_path)


@mcp.tool()
def renumber_sequences(docx_path: str, index_db_path: str | None = None) -> dict[str, Any]:
    """595ccea1 — Re-scan SEQ Figure / SEQ Table fields and confirm/fix sequential numbering.

    Motivated directly by a real Figure 41/42 numbering collision found by
    hand after a structural move. Walks the document once in true body
    order, computes the correct 1-based number for each kind, and rewrites
    any cached SEQ number that doesn't match. Any REF field elsewhere caching
    the OLD "<Kind> <N>" display text for a corrected caption is also
    updated. A first-class primitive so move_section / copy_section can call
    into it rather than duplicate renumbering logic.

    Args:
      docx_path:      Absolute path to the .docx file (mutated in place only
                      if a correction is needed).
      index_db_path:  If supplied, sidecar is invalidated after a write.

    Returns:
      {status, figure_count, table_count, collisions_found, corrections,
      ref_fields_updated, docx_path} or {error: <message>}.
    """
    return docs_intel.renumber_sequences(docx_path=docx_path, index_db_path=index_db_path)


@mcp.tool()
def insert_highlighted_note(
    docx_path: str,
    text: str,
    anchor_para_id: str,
    position: str = "after",
    style: str = "internal_note",
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """65c8eb31 — Insert a genuinely highlighted internal-author-note paragraph.

    Addresses a real recurring pattern: bracket-header/NOTE-block text left
    inline in results-section prose, indistinguishable from real
    dissertation content. Writes a structurally distinct paragraph instead —
    a real w:highlight run property plus a dedicated paragraph style name and
    its own bookmark — so notes can be found and stripped programmatically
    (see list_internal_notes / scan_stale_notes) before final submission.

    Args:
      docx_path:       Absolute path to the .docx file (mutated in place).
      text:            Note content (no bracket/NOTE-prefix decoration
                       needed — the highlight + style ARE the signal).
      anchor_para_id:  w14:paraId (or p{N}) of the paragraph to anchor on.
      position:        "before" or "after" (default) the anchor.
      style:           Must be "internal_note" (the only supported style
                       today).
      index_db_path:   If supplied, the note is ALSO recorded in the
                       sidecar's docx_internal_notes table so
                       list_internal_notes can find it.

    Returns:
      {status, note_id, text, anchor_para_id, position, style, docx_path}
      or {error: <message>} (file NOT mutated on error).
    """
    return docs_intel.insert_highlighted_note(
        docx_path=docx_path,
        text=text,
        anchor_para_id=anchor_para_id,
        position=position,
        style=style,
        index_db_path=index_db_path,
    )


@mcp.tool()
def list_internal_notes(index_db_path: str) -> list[dict[str, Any]]:
    """65c8eb31 — List internal-author-note paragraphs recorded in the sidecar.

    Reads the docx_internal_notes table populated by insert_highlighted_note.
    Sidecar QUERY (matching the convention of get_equations /
    get_local_structure_elements) — reports notes recorded at insertion
    time, not a live re-scan of the .docx. A note inserted without
    index_db_path set will NOT appear here even though it exists in the
    document.

    Args:
      index_db_path: Path to the sidecar SQLite index.

    Returns:
      A list of {note_id, anchor_para_id, text} dicts. [] when the sidecar
      doesn't exist yet or has no recorded notes.
    """
    return docs_intel.list_internal_notes(index_db_path=index_db_path)


@mcp.tool()
def write_section(
    docx_path: str,
    heading_text: str,
    level: int,
    content_spec: list[dict[str, Any]],
    anchor_para_id: str,
    position: str = "after",
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """82d22824 — Create a whole new section (heading + body + figure/table
    references) as ONE atomic operation from a structured spec.

    Replaces the failure-prone pattern of separate insert_caption /
    insert_cross_reference / raw-paragraph calls that can each independently
    fail or land at the wrong position: every block is validated before any
    XML is touched, then spliced into the document in a single write.

    content_spec is an ordered list of block dicts, each with a "type":
      {"type": "paragraph", "text": str, "references": [ref_spec, ...]}
        ref_spec is {"target_caption_para_id": ...} or
        {"bookmark_name": ...} (exactly one) — appends a live REF
        cross-reference field at the end of the paragraph.
      {"type": "caption", "kind": "Figure"|"Table", "label_text": str}
        A Caption-styled paragraph with its own SEQ field. NOTE: this module
        has no image/table INSERTION primitive — the caller places the
        actual image/table separately, same as insert_caption requires today.

    Every paragraph created (heading included) gets a fresh w14:paraId
    immediately, so returned para_ids are usable right away by
    insert_cross_reference / find_references_to / move_section.

    Args:
      docx_path:       Absolute path to the .docx file (mutated in place).
      heading_text:    Text of the new section's heading.
      level:           Heading level (1 = Heading1, 2 = Heading2, ...).
      content_spec:    Ordered list of block specs (see above).
      anchor_para_id:  w14:paraId (or p{N}) of the paragraph/table to anchor on.
      position:        "before" or "after" (default) the anchor.
      index_db_path:   If supplied, sidecar is invalidated after the write.

    Returns:
      {status, heading_para_id, heading_text, level, block_para_ids,
      docx_path} or {error: <message>} (file NOT mutated on error).
    """
    return docs_intel.write_section(
        docx_path=docx_path,
        heading_text=heading_text,
        level=level,
        content_spec=content_spec,
        anchor_para_id=anchor_para_id,
        position=position,
        index_db_path=index_db_path,
    )


@mcp.tool()
def move_section(
    docx_path: str,
    section_id: str,
    destination_anchor_para_id: str,
    destination_position: str = "after",
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """6ff24136 — Move an existing section (heading + its content) to a new
    location in the document.

    Cuts the heading at section_id and every block up to (not including) the
    next same-or-shallower heading, then re-inserts that exact range
    relative to destination_anchor_para_id. Existing paraIds/bookmarks are
    preserved (not regenerated), so cross-references INTO the moved section
    stay valid. After the move, automatically calls renumber_sequences (the
    move may have reordered Figure/Table captions) and find_references_to
    for section_id itself (in case something references the section's own
    heading).

    Args:
      docx_path:                    Absolute path to the .docx file (mutated
                                     in place).
      section_id:                   w14:paraId (or p{N}) of the section's OWN
                                     heading paragraph.
      destination_anchor_para_id:   w14:paraId (or p{N}) of the paragraph/
                                     table to move the section next to. Must
                                     be OUTSIDE the section being moved.
      destination_position:         "before" or "after" (default).
      index_db_path:                If supplied, sidecar is invalidated
                                     (and threaded into renumber_sequences).

    Returns:
      {status, section_id, heading_text, moved_block_count,
      destination_anchor_para_id, destination_position, renumber_sequences,
      find_references_to, docx_path} or {error: <message>} (file NOT
      mutated on error).
    """
    return docs_intel.move_section(
        docx_path=docx_path,
        section_id=section_id,
        destination_anchor_para_id=destination_anchor_para_id,
        destination_position=destination_position,
        index_db_path=index_db_path,
    )


@mcp.tool()
def copy_section(
    docx_path: str,
    section_id: str,
    destination_anchor_para_id: str,
    destination_position: str = "after",
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """8213050a — Duplicate an existing section (heading + its content) to a
    new location, leaving the original untouched.

    Same section-boundary rule as move_section, but deep-COPIES the range:
    every copied paragraph gets a FRESH w14:paraId, and every bookmark name
    inside the copied range is renamed to a fresh unique name (duplicate
    paraIds/bookmark names would silently break paraId-addressed tools and
    cross-reference resolution). A REF/PAGEREF/NOTEREF field inside the
    copied range that targets a bookmark ALSO inside the copy is repointed
    at the copy's own renamed bookmark; a field targeting something outside
    the copy is left pointing at the (shared) original. Calls
    renumber_sequences as the final step, same as move_section.

    Args:
      docx_path:                    Absolute path to the .docx file (mutated
                                     in place).
      section_id:                   w14:paraId (or p{N}) of the section's OWN
                                     heading paragraph (the ORIGINAL, not the
                                     copy).
      destination_anchor_para_id:   w14:paraId (or p{N}) to copy the section
                                     next to.
      destination_position:         "before" or "after" (default).
      index_db_path:                If supplied, sidecar is invalidated
                                     (and threaded into renumber_sequences).

    Returns:
      {status, section_id, heading_text, new_heading_para_id,
      copied_block_count, para_id_map, bookmark_map,
      destination_anchor_para_id, destination_position, renumber_sequences,
      docx_path} or {error: <message>} (file NOT mutated on error).
    """
    return docs_intel.copy_section(
        docx_path=docx_path,
        section_id=section_id,
        destination_anchor_para_id=destination_anchor_para_id,
        destination_position=destination_position,
        index_db_path=index_db_path,
    )


def main() -> None:
    """Console entry point (``uvx meridian-docs``)."""
    mcp.run()


if __name__ == "__main__":
    main()
