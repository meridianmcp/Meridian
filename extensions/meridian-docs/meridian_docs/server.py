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

93cd9798 — also exposes :func:`check_render_capability`, a thin wrapper over
:mod:`render_gate` that detects whether this environment can actually render
a .docx for visual QA (LibreOffice headless / Word COM), returning one of
exactly three states (rendered / unavailable-with-reason / failed) so a
caller never mistakes "we could not check" for "we verified this renders".
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import docs_intel
from . import local_ingest
from . import render_gate

mcp = FastMCP("meridian-docs")


@mcp.tool()
def document_outline(
    path: str,
    page_size: int | None = None,
    cursor: str | None = None,
    section_anchor: str | None = None,
) -> dict[str, Any]:
    """Heading outline of a .docx: paragraph_count, heading_count, and an ordered
    list of headings (level, text, para_id, index, section_type). Stateless —
    builds no index. Every response includes document_fingerprint.

    1dff1300 — cursor-based pagination + section scoping, so a large
    document's outline can never silently truncate or exceed a token
    budget. Omitting page_size and cursor (the default) returns the full
    outline, unchanged from before.

    Pass page_size (no cursor) for the first page: at most page_size
    headings, plus cursor (opaque token for the next page, or None when
    this is the last page), has_more, and total. Pass cursor (from a prior
    call) for the next page. section_anchor (a heading's para_id or exact
    heading text) scopes the outline to just that heading's own subsection
    (itself + nested sub-headings + their body).

    A cursor whose embedded fingerprint no longer matches the document's
    current content, or whose section_anchor doesn't match this call's, or
    that is malformed, is rejected: {"error": ..., "reason":
    "stale_cursor" | "invalid_cursor"}. An unresolvable section_anchor
    returns {"error": ..., "reason": "section_not_found"}.
    """
    return docs_intel.document_outline(
        path, page_size=page_size, cursor=cursor, section_anchor=section_anchor
    )


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

    Returns {index_db, heading_count, figure_count, table_count, complete,
    source_sha256}.

    e9b2cd2b — complete is always True on a successful return (a failed/
    interrupted run raises instead); source_sha256 is the SHA-256 fingerprint
    of the source .docx bytes that were just indexed, so a caller can compare
    it against a later re-fingerprint to detect drift out-of-band.
    """
    return docs_intel.index_docx_structure(path, index_db_path)


@mcp.tool()
def get_structure_elements(
    index_db_path: str, allow_stale: bool = False
) -> dict[str, Any]:
    """c39ae092 — retrieve all locally-stored structural elements from the sidecar.

    Returns {headings, figures, tables} lists from the docx_headings,
    docx_figures, docx_tables tables populated by index_document_structure.
    Returns empty lists for any table not yet populated.

    e9b2cd2b — FAILS CLOSED by default: raises if the structural index is
    stale (source .docx content changed since the last successful
    index_document_structure run) or incomplete (that run never finished),
    instead of returning partial/outdated counts as if they were
    authoritative. Pass allow_stale=True to read the best-effort data anyway
    — the result then includes a "freshness" key explaining why it wasn't
    trusted.
    """
    return docs_intel.get_local_structure_elements(
        index_db_path, allow_stale=allow_stale
    )


@mcp.tool()
def get_paragraph(index_db_path: str, para_id: str) -> dict[str, Any] | None:
    """Fetch one paragraph by its para_id from a previously-built index."""
    return docs_intel.get_paragraph(index_db_path, para_id)


@mcp.tool()
def search_paragraphs(index_db_path: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Substring-search paragraphs in a previously-built index."""
    return docs_intel.find_paragraphs(index_db_path, query, limit)

@mcp.tool()
def search_document(
    docx_path: str,
    query: str,
    element_types: list[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """BM25-search all searchable DOCX XML parts with structural filters.

    c7cc9da4 -- the anchor-resolution surface for review-session
    recommendations: every result carries a stable ``element_id``
    (paragraph/heading/section/table/caption), an exact literal
    ``quoted_text`` (safe to quote verbatim, unlike ``snippet`` which may be
    "…"-truncated), and a ``word_search_locator`` -- ``{find_text, part,
    element_id, unique_in_part, occurrence_count_in_part}`` -- telling a
    reviewer whether pasting ``find_text`` into Word's own Ctrl+F box lands
    unambiguously on this occurrence. Image anchors resolve via a
    ``figure_caption`` result's ``element_id`` (its caption) paired with
    ``find_image_paragraph`` for the picture paragraph itself; equation
    anchors come from ``extract_equations`` / ``get_equations`` instead,
    since OMML math has no searchable body text. See
    ``docs_intel.search_document_xml`` for the full contract.
    """
    return docs_intel.search_document_xml(
        docx_path=docx_path,
        query=query,
        element_types=element_types,
        limit=limit,
    )


@mcp.tool()
def highlight_document(
    docx_path: str,
    query: str,
    element_types: list[str] | None = None,
    color: str = "yellow",
    limit: int = 100,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Apply native Word highlighting to structural XML search matches.

    ddd79188 follow-up -- once the highlighted parts are staged, verified
    (ZIP/XML/relationship/media integrity), and promoted, a real Word/COM
    (or LibreOffice) render-capability check also runs against the
    just-written file, mirroring insert_figure_block / merge_docx_draft.
    "rendered" continues normally with render evidence attached to the
    result. "failed" (a render backend was available but errored on this
    document) restores the pre-write backup and returns an error, same as a
    structural verification failure. "unavailable-with-reason" (no render
    backend in this environment) ALSO fails closed by default -- never
    reported as verified -- unless allow_degraded_render=True is passed
    together with a non-empty degraded_render_reason, an audited opt-in that
    keeps the write but stamps render_verified=False / render_degraded=True
    on the result instead of silently treating "could not check" as
    "passed".

    allow_degraded_render: see insert_figure_block's docstring for the full
      contract. Requires degraded_render_reason.
    degraded_render_reason: required, non-empty when allow_degraded_render is
      True; carried onto the result as an audit trail.
    session_id: 273df573 — identifies the calling Meridian session to the
      tunnel-layer DOCX region-claim guard (check_docs_write_conflict in
      meridian/routes/tunnel.py). Not forwarded to docs_intel; has no effect
      when this tool is invoked outside Meridian's tunnel (e.g. standalone
      `uvx meridian-docs`).
    """
    return docs_intel.highlight_document_matches(
        docx_path=docx_path,
        query=query,
        element_types=element_types,
        color=color,
        limit=limit,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
    )

@mcp.tool()
def read_document_snapshot(
    docx_path: str,
    page_size: int | None = None,
    cursor: str | None = None,
    section_anchor: str | None = None,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """Read the last saved DOCX snapshot without writing or requiring a close.

    c7cc9da4 -- the entry point for a Meridian-docs review session: never
    treats a Word ~$ lock file as a blocker, and always reads the last
    SAVED on-disk bytes (unsaved Word edits are invisible until saved).
    Returns a ``source_sha256`` fingerprint of those exact bytes -- record
    it and re-check it (e.g. via ``render_gate.verify_promotion_readiness``)
    before promoting any later draft/overlay, so a promotion never lands
    against content the review never actually saw -- plus an explicit
    ``limitations`` list spelling out both caveats for a caller that
    forwards this snapshot into a recommendation or report.

    1dff1300 — cursor-based pagination + section scoping (same contract as
    document_outline — see its docstring) so a large document's snapshot
    can never silently truncate or exceed a token budget. Omitting
    page_size and cursor (the default) returns the full paragraph list,
    unchanged from before. Each paginated paragraph carries section_path
    and heading_para_id. index_db_path, when given, attaches stale_index
    (the structural sidecar's freshness) plus whole-document tables/
    figures identity and page-scoped equations identity already recorded
    there, best-effort — never blocks on a missing/stale sidecar. See
    ``docs_intel.read_document_snapshot`` for the full contract.
    """
    return docs_intel.read_document_snapshot(
        docx_path,
        page_size=page_size,
        cursor=cursor,
        section_anchor=section_anchor,
        index_db_path=index_db_path,
    )


@mcp.tool()
def locate_anchor(document_path: str, query: dict[str, Any]) -> dict[str, Any]:
    """2271789f -- read-only, fresh-snapshot deterministic anchor locator.

    Resolves ``query`` against sections, paragraphs, captions, tables
    (including cell text), and equations, re-parsing ``document_path`` from
    disk on every call (never a stale sidecar index). Query keys (all
    optional; at least one required): ``para_id``, ``section_path`` (e.g.
    "3.2.4"), ``section_text``, ``caption_label`` (e.g. "Table 3"), ``text``
    (Ctrl+F-style literal substring), ``element_types``, ``case_sensitive``,
    and ``expected_source_fingerprint`` (detects the document having
    changed since a previously-returned ``source_fingerprint``).

    A resolved result includes ``section_path``, ``heading_para_id``,
    ``target_para_id``, ``document_order``, ``quoted_text``,
    ``leading_text_preview``/``first_words``, ``word_search_locator``,
    ``bookmark_exists``, ``ref_status``, and an explicit (empty when
    unambiguous) ``candidates`` list. ``status`` is one of "resolved",
    "ambiguous", "not_found", or "stale". Never mutates document_path. See
    :func:`meridian_docs.docs_intel.locate_anchor` for the full contract.
    """
    return docs_intel.locate_anchor(document_path=document_path, query=query)


@mcp.tool()
def locate_anchors(document_path: str, queries: list[dict[str, Any]]) -> dict[str, Any]:
    """2271789f -- resolve multiple independent locate_anchor queries against
    ONE fresh parse of document_path (one source_fingerprint, one index
    pass), preserving query order in the returned ``results`` list. See
    :func:`locate_anchor` for the per-query contract and
    :func:`meridian_docs.docs_intel.locate_anchors` for full details.
    """
    return docs_intel.locate_anchors(document_path=document_path, queries=queries)


@mcp.tool()
def get_document_review(
    docx_path: str,
    expected_source_fingerprint: str | None = None,
    include_render_check: bool = False,
) -> dict[str, Any]:
    """b67ec6b5 -- non-mutating DOCX review: findings grouped by category
    (structure/equation/caption/section_page/ownership/provenance/
    render_integrity) and severity, each carrying a locate_anchor-style
    locator (section_path, target_para_id, document_order, quoted_text,
    word_search_locator, bookmark/REF status) instead of a raw paragraph id
    alone.

    Composes existing read-only primitives (audit_equation_style,
    scan_stale_notes, a read-only legacy-plaintext-caption detector, and
    optionally check_render_capability) rather than re-deriving detection or
    anchor-resolution logic. Pass expected_source_fingerprint (a value
    previously returned as source_fingerprint) to detect the document having
    changed since a stashed review -- a mismatch returns
    ``{"status": "stale", ...}`` with empty findings instead of resolving
    against what may now be the wrong document. include_render_check opts
    into a live render-capability probe (slow/backend-dependent -- never run
    implicitly); only a "failed" render status becomes a finding. See
    :func:`meridian_docs.docs_intel.build_document_review` for the full
    contract. No DOCX writes -- read-only in every code path.
    """
    return docs_intel.build_document_review(
        docx_path,
        expected_source_fingerprint=expected_source_fingerprint,
        include_render_check=include_render_check,
    )


@mcp.tool()
def check_render_capability(docx_path: str) -> dict[str, Any]:
    """93cd9798 -- lightweight render-capability detection for visual QA.

    This is capability DETECTION, not a rendering engine (mirrors the .pdf
    boundary in ``ingest_local_document``, which also declines to embed a
    rendering pipeline). Returns a dict with a ``status`` key that is exactly
    one of three states:

      - "rendered"                -- a real render backend (LibreOffice
                                      headless, or Word COM on Windows) was
                                      available and actually produced visual
                                      output for this document. This is the
                                      ONLY status that means "verified visual
                                      QA" -- treat every other status as NOT
                                      verified.
      - "unavailable-with-reason" -- no render backend is installed/reachable
                                      in this environment. ``reason`` names
                                      every backend checked and why, never a
                                      generic message. Says nothing about the
                                      document itself.
      - "failed"                  -- a backend was available but the render
                                      attempt for this document errored.
                                      ``reason`` carries the underlying error.
                                      Never silently reported as "rendered".

    See ``render_gate.check_render_capability`` for the full contract.
    """
    return render_gate.check_render_capability(docx_path)


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
def insert_image(
    docx_path: str,
    image_path: str,
    anchor_para_id: str | None = None,
    position: str = "after",
    width_inches: float | None = None,
    height_inches: float | None = None,
    index_db_path: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Insert a local image as a centered inline OOXML figure.

    The generated image paragraph is always centered with w:jc val="center",
    equivalent to pressing Ctrl+E in Word.

    session_id: 273df573 — identifies the calling Meridian session to the
      tunnel-layer DOCX region-claim guard (check_docs_write_conflict in
      meridian/routes/tunnel.py). Not forwarded to docs_intel; has no effect
      when this tool is invoked outside Meridian's tunnel (e.g. standalone
      `uvx meridian-docs`).
    """
    return docs_intel.insert_image(
        docx_path=docx_path,
        image_path=image_path,
        anchor_para_id=anchor_para_id,
        position=position,
        width_inches=width_inches,
        height_inches=height_inches,
        index_db_path=index_db_path,
    )

@mcp.tool()
def insert_figure_block(
    docx_path: str,
    image_path: str,
    label_text: str,
    anchor_para_id: str | None = None,
    position: str = "after",
    width_inches: float | None = None,
    height_inches: float | None = None,
    section_heading: str | None = None,
    index_db_path: str | None = None,
    style_policy: dict[str, Any] | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """19be1551 — atomically insert a centered image paragraph AND its
    adjacent real SEQ Figure caption in ONE document-load-mutate-save
    transaction.

    Unlike calling insert_image then insert_caption as two separate write
    transactions, both paragraphs are built against a single in-memory
    document tree and reach disk via exactly one zip rewrite — there is no
    window in which a failure leaves an orphan image with no caption, or an
    inconsistent caption index.

    The image paragraph is always centered (w:jc val="center", equivalent to
    Ctrl+E), regardless of any alignment the anchor paragraph carries. The
    caption paragraph is always inserted immediately after the image
    paragraph — that ordering is not configurable, mirroring insert_caption's
    own rule that a Figure caption can never precede its image. anchor_para_id
    / position control where the IMAGE paragraph (and therefore the whole
    block) lands relative to an existing direct body paragraph, exactly like
    insert_image; None appends the block before the document's trailing
    sectPr.

    The caption's SEQ number is the count of existing SEQ Figure captions in
    the document plus one (same semantics as insert_caption), and it gets a
    fresh _Ref<digits> cross-reference bookmark. style_policy["caption_centered"]
    (default False, via resolve_style_policy) controls whether the caption
    itself also gets w:jc val="center".

    After the single save, the file is re-read fresh from disk and verified:
    the image paragraph must be present and centered, the caption must
    immediately follow with nothing in between, and its SEQ number/label text
    must match what was written. On a verification failure, the pre-write
    backup is restored and an error is returned instead of a false success.

    ddd79188 — AFTER that structural verification passes, a real
    render-capability check (check_render_capability) also runs against the
    just-written file: structural re-parse alone can never prove the
    document actually opens/renders in Word. "rendered" continues normally
    with render evidence attached. "failed" (a render backend was available
    but errored on this document) restores the pre-write backup and returns
    an error, same as a structural verification failure. "unavailable-with-
    reason" (no render backend in this environment) ALSO fails closed by
    default — never reported as verified — unless allow_degraded_render=True
    is passed together with a non-empty degraded_render_reason, an audited
    opt-in that keeps the write but stamps render_verified=False /
    render_degraded=True on the result instead of silently treating "could
    not check" as "passed".

    Supported image formats, dimension inference, and the six-inch default
    width all match insert_image.

    Args:
      docx_path:       Absolute path to the .docx file (mutated in place).
      image_path:      Absolute path to a local PNG/JPEG/GIF/BMP/TIFF image.
      label_text:      Caption label text (e.g. "Loss curve for run 42").
                       Rendered text will be e.g. "Figure 1. Loss curve...".
      anchor_para_id:  w14:paraId or p{N} of the direct body paragraph to
                       anchor the block on. None appends before the trailing
                       sectPr.
      position:        "before" or "after" (default) — placement of the
                       IMAGE paragraph relative to anchor_para_id.
      width_inches:    Optional explicit width; inferred from the image
                       header when omitted (six-inch default width).
      height_inches:   Optional explicit height; inferred from the image
                       header when omitted (preserves aspect ratio).
      section_heading: Optional section heading for organizational
                       association. Stored in the sidecar index.
      index_db_path:   If supplied, the sidecar index is invalidated and the
                       new caption is upserted so the next read reflects the
                       new figure/caption without a stale cache.
      style_policy:    Optional style policy overrides (see
                       resolve_style_policy).
      allow_degraded_render: ddd79188 — explicit, audited opt-in to accept
                       this write when no render backend is available in
                       this environment (render status
                       "unavailable-with-reason"). Requires
                       degraded_render_reason. Never bypasses a real render
                       "failed" status — only the "no backend available"
                       case can be degraded.
      degraded_render_reason: Required, non-empty when allow_degraded_render
                       is True; carried onto the result as an audit trail
                       (this stdlib-only, DB-free extension does not persist
                       it itself — a caller with DB access, e.g. Meridian
                       core, is responsible for logging/pinning it).
      session_id:      273df573 — identifies the calling Meridian session to
                       the tunnel-layer DOCX region-claim guard
                       (check_docs_write_conflict in meridian/routes/
                       tunnel.py). Not forwarded to docs_intel; has no effect
                       when this tool is invoked outside Meridian's tunnel
                       (e.g. standalone `uvx meridian-docs`).

    Returns:
      {status, image_para_id, image_name, kind, seq_number, label_text,
      section_heading, ref_bookmark, docx_path, render_status,
      render_verified, render_backend, render_detail}
      or {error: <message>} on failure (file NOT left mutated on validation
      failure; restored from backup on a structural- or render-verification
      failure).
    """
    return docs_intel.insert_figure_block(
        docx_path=docx_path,
        image_path=image_path,
        label_text=label_text,
        anchor_para_id=anchor_para_id,
        position=position,
        width_inches=width_inches,
        height_inches=height_inches,
        section_heading=section_heading,
        index_db_path=index_db_path,
        style_policy=style_policy,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
    )


@mcp.tool()
def insert_media_part(
    docx_path: str,
    image_path: str,
    anchor_para_id: str | None = None,
    position: str = "after",
    width_inches: float | None = None,
    height_inches: float | None = None,
    index_db_path: str | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """d371b00b -- safe insertion of a brand-new image/media package member.

    Lower-level, caption-less sibling of insert_figure_block (pair with
    insert_caption / insert_figure_block for a captioned figure, same
    two-step composition insert_image already documents). Adds beyond
    insert_image: collision-free relationship id + media part name
    generation that is explicitly RE-VERIFIED (not just trusted) before use;
    matching [Content_Types].xml Default/Override entries (a Default is
    reused when it already declares the right content type, added when the
    extension is new to the package, or a part-specific Override is added
    instead of mutating a pre-existing, disagreeing Default other parts may
    rely on); the same drawing/frame-extent construction insert_figure_block
    uses; and an explicit post-write relationship<->media BIJECTION check
    (the new relationship id and new media part must be a clean 1:1 pairing)
    before the write is ever reported as successful.

    Routed through the same transactional backup/CAS-safe write envelope,
    _docx_promotion_lock discipline, and tri-state real-render canary as
    insert_figure_block. allow_degraded_render/degraded_render_reason: same
    audited opt-in contract as insert_figure_block's own.

    Anchor resolution, supported image formats, and dimension inference all
    match insert_image.

    Returns {status, image_para_id, image_name, relationship_id,
    content_type_action, width_emu, height_emu, docx_path, render_status,
    render_verified, ...}, or {error: message} without mutating the document
    on validation failure, structural/bijection verification failure, or a
    render-verification failure that could not be cleanly restored.

    session_id: 273df573 — identifies the calling Meridian session to the
      tunnel-layer DOCX region-claim guard (check_docs_write_conflict in
      meridian/routes/tunnel.py). Not forwarded to docs_intel; has no effect
      when this tool is invoked outside Meridian's tunnel (e.g. standalone
      `uvx meridian-docs`).
    """
    return docs_intel.insert_docx_media_part(
        docx_path=docx_path,
        image_path=image_path,
        anchor_para_id=anchor_para_id,
        position=position,
        width_inches=width_inches,
        height_inches=height_inches,
        index_db_path=index_db_path,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
    )


@mcp.tool()
def remove_package_part(
    docx_path: str,
    part_name: str,
    dry_run: bool = True,
    index_db_path: str | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """d371b00b -- reference-counted, dry-run-capable removal of an
    unreferenced word/media/* package part and its relationship(s).

    part_name (e.g. "word/media/image3.png") must name a word/media/* ZIP
    member — removal of any other package part is refused outright; this
    tool's scope is deliberately narrow, not arbitrary-part removal.

    Every relationship in word/_rels/document.xml.rels that targets
    part_name is found, then word/document.xml is scanned for any attribute
    referencing one of those relationship ids — a real reference count, not
    a heuristic restricted to just inline images. A part with a NONZERO
    reference count is REFUSED (a real error, status="refused_still_
    referenced", never a silent skip) whether dry_run is True or False.

    dry_run=True (the default — fail-safe): for a genuinely zero-reference
    part, reports exactly what WOULD be removed (relationship ids, and which
    [Content_Types].xml Default/Override entries would be cleaned up)
    WITHOUT touching the zip.

    dry_run=False: performs the removal for real through the same
    transactional backup/CAS-safe write envelope, _docx_promotion_lock
    discipline, and tri-state real-render canary insert_figure_block uses.
    allow_degraded_render/degraded_render_reason: same audited opt-in
    contract as insert_figure_block's own.

    Returns, on success: {status: "dry_run"|"removed", part_name,
    relationship_ids / relationship_ids_removed,
    content_type_overrides_removed, content_type_defaults_removed,
    reference_count: 0, docx_path, ...render fields on a real removal...}.
    On refusal: {error, status: "refused_still_referenced", reference_count,
    part_name, referencing_relationship_ids}. On any other failure: {error}
    without mutating the document.

    session_id: 273df573 — identifies the calling Meridian session to the
      tunnel-layer DOCX region-claim guard (check_docs_write_conflict in
      meridian/routes/tunnel.py). Not forwarded to docs_intel; has no effect
      when this tool is invoked outside Meridian's tunnel (e.g. standalone
      `uvx meridian-docs`).
    """
    return docs_intel.remove_docx_package_part(
        docx_path=docx_path,
        part_name=part_name,
        dry_run=dry_run,
        index_db_path=index_db_path,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
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
    style_policy: dict[str, Any] | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """9d749639 — Insert a real Word Caption paragraph into a .docx file.

    Writes a new paragraph with style Caption and a SEQ Figure / SEQ Table
    field directly into word/document.xml, then re-packs the ZIP preserving
    all other members.  The SEQ number is auto-incremented (count of existing
    same-kind captions + 1).

    Both Figure and Table captions use the same Word Caption-style + SEQ
    mechanism — the ``kind`` parameter selects which counter to use.

    4efc63fd — style_policy["caption_centered"] (default False) controls
    whether the new caption gets w:jc w:val="center".

    ddd79188 — AFTER the write is staged, structurally verified, and
    promoted, a real render-capability check (check_render_capability) also
    runs against the just-written file: structural re-parse alone can never
    prove the document actually opens/renders in Word. "rendered" continues
    normally with render evidence attached. "failed" (a render backend was
    available but errored on this document) restores the pre-write backup
    and returns an error, same as a structural verification failure.
    "unavailable-with-reason" (no render backend in this environment) ALSO
    fails closed by default — never reported as verified — unless
    allow_degraded_render=True is passed together with a non-empty
    degraded_render_reason, an audited opt-in that keeps the write but
    stamps render_verified=False / render_degraded=True on the result
    instead of silently treating "could not check" as "passed".

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
      style_policy:    Optional style policy overrides (see
                       resolve_style_policy / audit_equation_style).
      allow_degraded_render: ddd79188 — explicit, audited opt-in to accept
                       this write when no render backend is available in
                       this environment (render status
                       "unavailable-with-reason"). Requires
                       degraded_render_reason. Never bypasses a real render
                       "failed" status.
      degraded_render_reason: Required, non-empty when allow_degraded_render
                       is True; carried onto the result as an audit trail
                       (this stdlib-only, DB-free extension does not persist
                       it itself — a caller with DB access, e.g. Meridian
                       core, is responsible for logging/pinning it).
      session_id:      273df573 — identifies the calling Meridian session to
                       the tunnel-layer DOCX region-claim guard
                       (check_docs_write_conflict in meridian/routes/
                       tunnel.py). Not forwarded to docs_intel; has no effect
                       when this tool is invoked outside Meridian's tunnel
                       (e.g. standalone `uvx meridian-docs`).

    Returns:
      {status, kind, seq_number, label_text, section_heading, ref_bookmark,
      docx_path, render_status, render_verified, render_backend,
      render_detail}
      or {error: <message>} on failure (file NOT left mutated on validation
      failure; restored from backup on a structural- or render-verification
      failure).
    """
    return docs_intel.insert_caption(
        docx_path=docx_path,
        anchor_para_id=anchor_para_id,
        kind=kind,
        label_text=label_text,
        position=position,
        section_heading=section_heading,
        index_db_path=index_db_path,
        style_policy=style_policy,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
    )


@mcp.tool()
def edit_caption(
    docx_path: str,
    caption_para_id: str,
    new_label_text: str,
    index_db_path: str | None = None,
    session_id: str | None = None,
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
      session_id:      273df573 — identifies the calling Meridian session to
                       the tunnel-layer DOCX region-claim guard
                       (check_docs_write_conflict in meridian/routes/
                       tunnel.py). Not forwarded to docs_intel; has no effect
                       when this tool is invoked outside Meridian's tunnel
                       (e.g. standalone `uvx meridian-docs`).

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
    session_id: str | None = None,
) -> dict[str, Any]:
    """9d749639 — Remove a Caption paragraph from a .docx file.

    Locates the paragraph by caption_para_id, verifies it is a Caption
    paragraph (Caption style or SEQ field present), removes it from the body,
    and re-packs the ZIP.

    Args:
      docx_path:       Absolute path to the .docx file (mutated in place).
      caption_para_id: w14:paraId or p{N} of the Caption paragraph.
      index_db_path:   If supplied, sidecar is invalidated after write.
      session_id:      273df573 — identifies the calling Meridian session to
                       the tunnel-layer DOCX region-claim guard
                       (check_docs_write_conflict in meridian/routes/
                       tunnel.py). Not forwarded to docs_intel; has no effect
                       when this tool is invoked outside Meridian's tunnel
                       (e.g. standalone `uvx meridian-docs`).

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
def retrofit_plaintext_captions(
    docx_path: str,
    index_db_path: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """82b0b1a6 — Bulk-convert existing plain-text Figure/Table captions
    (hardcoded numbers, no SEQ field) into real Word SEQ fields.

    insert_caption above only ever creates NEW captions with real SEQ
    fields — nothing converted EXISTING plain-text captions (a paragraph
    whose visible text literally reads "Figure 41" with no SEQ field behind
    it at all). renumber_sequences only walks SEQ fields, so a plain-text
    caption is invisible to it and silently survives a renumbering pass —
    duplicate numbers and all.

    Scans every paragraph (including inside table cells) for one with no
    SEQ field already, whose text starts with "Figure <N>" or "Table <N>"
    (case-insensitive). Each match is rebuilt using the exact same SEQ
    fldSimple shape insert_caption constructs, preserving the paragraph's
    own identity and its existing descriptive label text. renumber_sequences
    is then called automatically so the (possibly still-duplicate) numbers
    just converted are corrected from actual document order — closing the
    gap where duplicate hardcoded captions survived a renumbering pass.

    A candidate paragraph that already carries a bookmark is skipped rather
    than converted, to avoid destroying it.

    Args:
      docx_path:       Absolute path to the .docx file (mutated in place —
                       only if at least one plain-text caption is found).
      index_db_path:   If supplied, sidecar is invalidated after write (and
                       threaded into the renumber_sequences call).
      session_id:      273df573 — identifies the calling Meridian session to
                       the tunnel-layer DOCX region-claim guard
                       (check_docs_write_conflict in meridian/routes/
                       tunnel.py). Not forwarded to docs_intel; has no effect
                       when this tool is invoked outside Meridian's tunnel
                       (e.g. standalone `uvx meridian-docs`).

    Returns:
      {status, candidates_found, conversions, skipped, renumber_sequences,
      docx_path} or {error: <message>} on failure (file NOT mutated on
      error).
    """
    return docs_intel.retrofit_plaintext_captions(
        docx_path=docx_path,
        index_db_path=index_db_path,
    )


@mcp.tool()
def insert_cross_reference(
    docx_path: str,
    anchor_para_id: str,
    target_caption_para_id: str | None = None,
    bookmark_name: str | None = None,
    index_db_path: str | None = None,
    session_id: str | None = None,
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
      session_id:             273df573 — identifies the calling Meridian
                               session to the tunnel-layer DOCX region-claim
                               guard (check_docs_write_conflict in
                               meridian/routes/tunnel.py). Not forwarded to
                               docs_intel; has no effect when this tool is
                               invoked outside Meridian's tunnel (e.g.
                               standalone `uvx meridian-docs`).

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
    session_id: str | None = None,
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
      session_id:      273df573 — identifies the calling Meridian session to
                       the tunnel-layer DOCX region-claim guard
                       (check_docs_write_conflict in meridian/routes/
                       tunnel.py). Not forwarded to docs_intel; has no effect
                       when this tool is invoked outside Meridian's tunnel
                       (e.g. standalone `uvx meridian-docs`).

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
    session_id: str | None = None,
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
      session_id:         273df573 — identifies the calling Meridian session
                          to the tunnel-layer DOCX region-claim guard
                          (check_docs_write_conflict in meridian/routes/
                          tunnel.py). Not forwarded to docs_intel; has no
                          effect when this tool is invoked outside
                          Meridian's tunnel (e.g. standalone
                          `uvx meridian-docs`).

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
    session_id: str | None = None,
) -> dict[str, Any]:
    """9d749639 — Remove the first CSL_CITATION complex field from a paragraph.

    Locates the field by scanning for a complex field (fldChar begin...end)
    whose instrText contains CSL_CITATION, removes all its constituent runs,
    and re-packs the ZIP.

    Args:
      docx_path:       Absolute path to the .docx file (mutated in place).
      anchor_para_id:  w14:paraId or p{N} of the paragraph to edit.
      index_db_path:   If supplied, sidecar is invalidated after write.
      session_id:      273df573 — identifies the calling Meridian session to
                       the tunnel-layer DOCX region-claim guard
                       (check_docs_write_conflict in meridian/routes/
                       tunnel.py). Not forwarded to docs_intel; has no effect
                       when this tool is invoked outside Meridian's tunnel
                       (e.g. standalone `uvx meridian-docs`).

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
    style_policy: dict[str, Any] | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """a80af3a0 — Insert an equation into a .docx file.

    Accepts a raw OMML XML string (starting with "<") or a LaTeX expression
    (e.g. r"\\frac{a}{b}" or "E=mc^2") which is converted to OMML locally
    using latex2mathml (pure Python, no lxml).

    Three positions:
      "before" — new display-mode paragraph immediately before the anchor.
      "after"  — new display-mode paragraph immediately after the anchor.
      "append" — append the <m:oMath> inline to the anchor paragraph itself.

    4efc63fd — style_policy (see resolve_style_policy) controls the new
    display paragraph's alignment (equation_alignment, default "center") and
    left indentation (body_indent_twips, default 0). Not consulted for
    position="append".

    ddd79188 — AFTER the write is staged, structurally verified, and
    promoted, a real render-capability check (check_render_capability) also
    runs against the just-written file: structural re-parse alone can never
    prove the document actually opens/renders in Word. "rendered" continues
    normally with render evidence attached. "failed" (a render backend was
    available but errored on this document) restores the pre-write backup
    and returns an error, same as a structural verification failure.
    "unavailable-with-reason" (no render backend in this environment) ALSO
    fails closed by default — never reported as verified — unless
    allow_degraded_render=True is passed together with a non-empty
    degraded_render_reason, an audited opt-in that keeps the write but
    stamps render_verified=False / render_degraded=True on the result
    instead of silently treating "could not check" as "passed".

    Args:
      docx_path:       Absolute path to the .docx file (mutated in place).
      anchor_para_id:  w14:paraId or p{N}/tbl{N} to anchor the insertion.
      payload:         Raw OMML XML or LaTeX expression.
      position:        "before", "after" (default), or "append".
      index_db_path:   If supplied, sidecar is invalidated after write.
      style_policy:    Optional style policy overrides (see
                       resolve_style_policy / audit_equation_style).
      allow_degraded_render: ddd79188 — explicit, audited opt-in to accept
                       this write when no render backend is available in
                       this environment. Requires degraded_render_reason.
      degraded_render_reason: Required, non-empty when allow_degraded_render
                       is True; carried onto the result as an audit trail.
      session_id:      273df573 — identifies the calling Meridian session to
                       the tunnel-layer DOCX region-claim guard
                       (check_docs_write_conflict in meridian/routes/
                       tunnel.py). Not forwarded to docs_intel; has no effect
                       when this tool is invoked outside Meridian's tunnel
                       (e.g. standalone `uvx meridian-docs`).

    Returns:
      {status, position, para_id, omml, docx_path, render_status,
      render_verified, render_backend, render_detail}
      or {error: <message>} on failure (file NOT left mutated on validation
      failure; restored from backup on a structural- or render-verification
      failure).
    """
    return docs_intel.insert_equation_local(
        docx_path=docx_path,
        anchor_para_id=anchor_para_id,
        payload=payload,
        position=position,
        index_db_path=index_db_path,
        style_policy=style_policy,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
    )


@mcp.tool()
def edit_equation(
    docx_path: str,
    equation_para_id: str,
    new_payload: str,
    equation_index: int | None = None,
    index_db_path: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """a80af3a0 — Replace the <m:oMath> in an existing equation paragraph.

    Locates the paragraph by equation_para_id, verifies it contains at least
    one <m:oMath>, and replaces exactly ONE targeted equation with a precise
    single-element swap, preserving every other child of the paragraph (other
    equations, text runs, fields, bookmarks, drawings) in its exact original
    order.

    b6a9ec99 — when a paragraph contains multiple equations, equation_index
    is required so the operation never guesses (and never silently drops
    the equations it doesn't touch).

    Args:
      docx_path:         Absolute path to the .docx file (mutated in place).
      equation_para_id:  w14:paraId or p{N} of the equation paragraph.
      new_payload:       Replacement OMML XML or LaTeX expression.
      equation_index:    0-based index (document order) of which equation to
                         replace when the paragraph holds more than one.
                         Required in that case.
      index_db_path:     If supplied, sidecar is invalidated after write.
      session_id:        273df573 — identifies the calling Meridian session
                         to the tunnel-layer DOCX region-claim guard
                         (check_docs_write_conflict in meridian/routes/
                         tunnel.py). Not forwarded to docs_intel; has no
                         effect when this tool is invoked outside Meridian's
                         tunnel (e.g. standalone `uvx meridian-docs`).

    Returns:
      {status, equation_para_id, equation_index, omml, docx_path}
      or {error: <message>} on failure.
    """
    return docs_intel.edit_equation_local(
        docx_path=docx_path,
        equation_para_id=equation_para_id,
        new_payload=new_payload,
        equation_index=equation_index,
        index_db_path=index_db_path,
    )

@mcp.tool()
def append_text_run_after_math(
    docx_path: str,
    equation_para_id: str,
    text: str,
    math_index: int | None = None,
    index_db_path: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Append a normal text run after an equation selected by stable paragraph id.

    When a paragraph contains multiple equations, math_index is required so the
    operation never guesses which equation should receive the text.

    session_id: 273df573 — identifies the calling Meridian session to the
      tunnel-layer DOCX region-claim guard (check_docs_write_conflict in
      meridian/routes/tunnel.py). Not forwarded to docs_intel; has no effect
      when this tool is invoked outside Meridian's tunnel (e.g. standalone
      `uvx meridian-docs`).
    """
    return docs_intel.append_text_run_after_math(
        docx_path=docx_path,
        equation_para_id=equation_para_id,
        text=text,
        math_index=math_index,
        index_db_path=index_db_path,
    )


@mcp.tool()
def remove_equation(
    docx_path: str,
    equation_para_id: str,
    index_db_path: str | None = None,
    session_id: str | None = None,
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
      session_id:        273df573 — identifies the calling Meridian session
                         to the tunnel-layer DOCX region-claim guard
                         (check_docs_write_conflict in meridian/routes/
                         tunnel.py). Not forwarded to docs_intel; has no
                         effect when this tool is invoked outside Meridian's
                         tunnel (e.g. standalone `uvx meridian-docs`).

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
def audit_equation_style(
    docx_path: str,
    style_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """4efc63fd — Audit every equation in a .docx for alignment, trailing
    punctuation, and numbering consistency; returns structured findings, not
    free text.

    Three finding types:
      misaligned_equation                — a display equation's paragraph
        alignment doesn't match style_policy["equation_alignment"] (default
        "center"). Inline and table-numbered equations are excluded.
      missing_trailing_punctuation /
      incorrect_trailing_punctuation     — a display equation has no (or the
        wrong) trailing punctuation immediately after the <m:oMath> — the
        same spot append_text_run_after_math writes to. Skipped entirely
        when style_policy["equation_punctuation_required"] is False.
      duplicate_equation_number /
      equation_number_gap                — table-numbered equations (the
        "(1)"/"(2a)" pattern) checked for exact duplicate numbers and gaps in
        the leading-integer sequence.

    Args:
      docx_path:     Absolute path to the .docx file (read-only).
      style_policy:  Optional style policy overrides (see
                     resolve_style_policy).

    Returns:
      {docx_path, equation_count, findings, finding_count, findings_by_type,
      policy} or {error: <message>}.
    """
    return docs_intel.audit_equation_style(
        docx_path=docx_path,
        style_policy=style_policy,
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
    session_id: str | None = None,
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
      session_id:    273df573 — identifies the calling Meridian session to
                     the tunnel-layer DOCX region-claim guard
                     (check_docs_write_conflict in meridian/routes/
                     tunnel.py). Not forwarded to docs_intel; has no effect
                     when this tool is invoked outside Meridian's tunnel
                     (e.g. standalone `uvx meridian-docs`).

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
    session_id: str | None = None,
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
      session_id:    273df573 — identifies the calling Meridian session to
                     the tunnel-layer DOCX region-claim guard
                     (check_docs_write_conflict in meridian/routes/
                     tunnel.py). Not forwarded to docs_intel; has no effect
                     when this tool is invoked outside Meridian's tunnel
                     (e.g. standalone `uvx meridian-docs`).

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
    session_id: str | None = None,
) -> dict:
    """1258794a — Remove a bibliography entry paragraph from a .docx.

    Locates the entry by the bibkey_<key> bookmark and removes the entire
    paragraph.  Use this when a citation is deleted from the document body
    and the corresponding reference list entry should be removed.

    Args:
      docx_path:     Absolute path to the .docx file (mutated in place).
      citation_key:  The citation key of the entry to remove.
      index_db_path: If supplied, sidecar is invalidated after write.
      session_id:    273df573 — identifies the calling Meridian session to
                     the tunnel-layer DOCX region-claim guard
                     (check_docs_write_conflict in meridian/routes/
                     tunnel.py). Not forwarded to docs_intel; has no effect
                     when this tool is invoked outside Meridian's tunnel
                     (e.g. standalone `uvx meridian-docs`).

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
    session_id: str | None = None,
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
      session_id:  273df573 — identifies the calling Meridian session to the
                   tunnel-layer DOCX region-claim guard
                   (check_docs_write_conflict in meridian/routes/
                   tunnel.py). Not forwarded to docs_intel; has no effect
                   when this tool is invoked outside Meridian's tunnel
                   (e.g. standalone `uvx meridian-docs`).

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
def find_references_to(
    docx_path: str, target_id: str, include_literal: bool = True
) -> dict[str, Any]:
    """fea654f9 — Find everything that points AT a figure/table/heading id.

    The missing inverse of insert_cross_reference: given a target (a
    Figure/Table caption's para_id, a heading's para_id, or an existing
    bookmark name directly), scans the whole document for REF / PAGEREF /
    NOTEREF fields whose instruction targets that same bookmark. Needed for
    safe renumbering/moving without breaking references elsewhere.

    b2035fb4 — when the target resolves to a Figure/Table caption and
    include_literal is True (default), also scans plain-text paragraphs
    (never fielded ones) for literal mentions such as "Figure 5.21" or
    "Table 11" that no REF field backs, classifying each as exact/ambiguous/
    stale against the caption's CURRENT cached number. Review the combined
    field + literal closure before calling renumber_sequences so a stale
    literal mention can be triaged while the pre-renumber numbers are still
    visible in the report.

    Args:
      docx_path: Absolute path to the .docx file.
      target_id: A caption/heading para_id, or an existing bookmark name.
      include_literal: Also run the literal-text scan described above
        (default True). Pass False to reproduce the field-only behavior.

    Returns:
      {target_id, target_kind, bookmark_names, references, reference_count,
      literal_references, literal_reference_count, combined_references,
      combined_reference_count, docx_path} or {error: <message>}.
    """
    return docs_intel.find_references_to(
        docx_path=docx_path, target_id=target_id, include_literal=include_literal
    )


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
def renumber_sequences(
    docx_path: str,
    index_db_path: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
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
      session_id:     273df573 — identifies the calling Meridian session to
                      the tunnel-layer DOCX region-claim guard
                      (check_docs_write_conflict in meridian/routes/
                      tunnel.py). Not forwarded to docs_intel; has no effect
                      when this tool is invoked outside Meridian's tunnel
                      (e.g. standalone `uvx meridian-docs`).

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
    mode: str = "inline",
    author: str = "Meridian",
    initials: str = "M",
    style_policy: dict[str, Any] | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Insert an internal note inline or as a native Word comment.

    mode="inline" preserves the highlighted Meridian note paragraph.
    mode="comment" creates a real Word comment visible in Word's review pane.

    4efc63fd — for mode="inline", style_policy["note_style"] (default
    "MeridianInternalNote") and style_policy["note_highlight_color"] (default
    "yellow") control the OOXML paragraph style / highlight color. Not
    consulted for mode="comment". Distinct from the `style` parameter above,
    which selects the note's category (only "internal_note" is supported),
    not its rendering.

    ddd79188 — for mode="inline", AFTER the write is staged, structurally
    verified, and promoted, a real render-capability check
    (check_render_capability) also runs against the just-written file:
    structural re-parse alone can never prove the document actually
    opens/renders in Word. "rendered" continues normally with render
    evidence attached. "failed" restores the pre-write backup and returns an
    error. "unavailable-with-reason" (no render backend in this environment)
    ALSO fails closed by default — never reported as verified — unless
    allow_degraded_render=True is passed together with a non-empty
    degraded_render_reason. For mode="comment", these two params are
    forwarded to insert_word_comment, which already enforces this same gate.

    allow_degraded_render / degraded_render_reason: explicit, audited opt-in
      to accept a write when no render backend is available in this
      environment; degraded_render_reason is required and must be non-empty
      whenever allow_degraded_render=True.

    session_id: 273df573 — identifies the calling Meridian session to the
      tunnel-layer DOCX region-claim guard (check_docs_write_conflict in
      meridian/routes/tunnel.py). Not forwarded to docs_intel; has no effect
      when this tool is invoked outside Meridian's tunnel (e.g. standalone
      `uvx meridian-docs`).
    """
    return docs_intel.insert_highlighted_note(
        docx_path=docx_path,
        text=text,
        anchor_para_id=anchor_para_id,
        position=position,
        style=style,
        index_db_path=index_db_path,
        mode=mode,
        author=author,
        initials=initials,
        style_policy=style_policy,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
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
    session_id: str | None = None,
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
      session_id:      273df573 — identifies the calling Meridian session to
                       the tunnel-layer DOCX region-claim guard
                       (check_docs_write_conflict in meridian/routes/
                       tunnel.py). Not forwarded to docs_intel; has no effect
                       when this tool is invoked outside Meridian's tunnel
                       (e.g. standalone `uvx meridian-docs`).

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
    allow_bookmark_split: bool = False,
    draft_output_path: str | None = None,
    wave_run_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """6ff24136 — Move an existing section (heading + its content) to a new
    location in the document.

    Cuts the heading at section_id and every block up to (not including) the
    next same-or-shallower heading, then re-inserts that exact range
    relative to destination_anchor_para_id. Existing paraIds/bookmarks are
    preserved (not regenerated), so cross-references INTO the moved section
    stay valid. Before anything is cut/spliced/saved, this runs two safety
    checks and ABORTS cleanly (file untouched) if either fails:
      1. find_references_to for section_id itself, against the still-intact
         file.
      2. A check for any bookmark whose w:bookmarkStart/w:bookmarkEnd pair
         would end up split across the move boundary (start inside the
         moved section, end outside, or vice versa) — this would tear the
         bookmark apart. Pass allow_bookmark_split=True to force the move
         anyway.
    After a successful write, automatically calls renumber_sequences (the
    move may have reordered Figure/Table captions — cached SEQ numbers AND
    any REF field elsewhere that displays a stale "<Kind> <N>" for a
    corrected caption are fixed in the same call).

    Args:
      docx_path:                    Absolute path to the .docx file (mutated
                                     in place).
      section_id:                   w14:paraId (or p{N}) of the section's OWN
                                     heading paragraph.
      destination_anchor_para_id:   w14:paraId (or p{N}) of the paragraph/
                                     table to move the section next to. Must
                                     be OUTSIDE the section being moved.
      destination_position:         "before" or "after" (default).

                                     When destination_anchor_para_id is
                                     itself a HEADING and destination_position
                                     is "after", the moved section lands
                                     after that heading's ENTIRE section (its
                                     own body + subsections), not immediately
                                     after the heading paragraph — anchor on
                                     a section's LAST body paragraph instead
                                     if you need a literal splice right after
                                     the heading.
      index_db_path:                If supplied, sidecar is invalidated
                                     (and threaded into renumber_sequences).
      allow_bookmark_split:         Explicit override (default False) to
                                     proceed even when the move would split a
                                     bookmark's start/end across the move
                                     boundary (see safety check 2 above).
      draft_output_path:            fe989980 — wave-scoped opt-in: when given
                                     together with wave_run_id, the move is
                                     written to this ISOLATED path instead of
                                     docx_path (which is only ever read, never
                                     mutated). Must differ from docx_path.
                                     Omitted (the default), this call is
                                     byte-identical to the direct-write
                                     behavior that predates fe989980. Pair
                                     with merge_docx_draft to promote an
                                     accepted draft into the canonical file
                                     once the Meridian MCP connection's
                                     docx_merge manifest gate (open_merge_
                                     manifest / declare_merge_anchors /
                                     claim_merge_owner / check_merge_stale_or_
                                     overlap) has cleared.
      wave_run_id:                  fe989980 — required together with
                                     draft_output_path; opaque wave
                                     identifier threaded into the return
                                     payload for cross-referencing against
                                     the matching docx_merge manifest.
      session_id:                   273df573 — identifies the calling
                                     Meridian session to the tunnel-layer
                                     DOCX region-claim guard
                                     (check_docs_write_conflict in
                                     meridian/routes/tunnel.py). Not
                                     forwarded to docs_intel; has no effect
                                     when this tool is invoked outside
                                     Meridian's tunnel (e.g. standalone
                                     `uvx meridian-docs`).

    Returns:
      {status, section_id, heading_text, moved_block_count,
      destination_anchor_para_id, destination_position, renumber_sequences,
      find_references_to, docx_path, wave_run_id, is_draft} or
      {error: <message>} (file NOT mutated on error; a blocked
      bookmark-split also returns {split_bookmarks: [...]}).
    """
    return docs_intel.move_section(
        docx_path=docx_path,
        section_id=section_id,
        destination_anchor_para_id=destination_anchor_para_id,
        destination_position=destination_position,
        index_db_path=index_db_path,
        allow_bookmark_split=allow_bookmark_split,
        draft_output_path=draft_output_path,
        wave_run_id=wave_run_id,
    )


@mcp.tool()
def copy_section(
    docx_path: str,
    section_id: str,
    destination_anchor_para_id: str,
    destination_position: str = "after",
    index_db_path: str | None = None,
    trim_original_to: str | None = None,
    draft_output_path: str | None = None,
    wave_run_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """8213050a — Duplicate an existing section (heading + its content) to a
    new location, leaving the original untouched (unless trim_original_to is
    given).

    Same section-boundary rule as move_section, but deep-COPIES the range:
    every copied paragraph gets a FRESH w14:paraId, and every bookmark name
    inside the copied range is renamed to a fresh unique name (duplicate
    paraIds/bookmark names would silently break paraId-addressed tools and
    cross-reference resolution). A REF/PAGEREF/NOTEREF field inside the
    copied range that targets a bookmark ALSO inside the copy is repointed
    at the copy's own renamed bookmark; a field targeting something outside
    the copy is left pointing at the (shared) original. Calls
    renumber_sequences as the final step, same as move_section.

    48daaf66 — destination_position="after" onto a HEADING anchor resolves to
    after that heading's ENTIRE section (own body + subsections), the same
    fix move_section got in 027b7ada. A pre-write find_references_to check
    (plus a bookmark-split check when trim_original_to is set) runs BEFORE
    any mutation, the same fix move_section got in e87b8338.

    Args:
      docx_path:                    Absolute path to the .docx file (mutated
                                     in place).
      section_id:                   w14:paraId (or p{N}) of the section's OWN
                                     heading paragraph (the ORIGINAL, not the
                                     copy).
      destination_anchor_para_id:   w14:paraId (or p{N}) to copy the section
                                     next to. Must be OUTSIDE the section
                                     being copied when trim_original_to is set.
      destination_position:         "before" or "after" (default).
      index_db_path:                If supplied, sidecar is invalidated
                                     (and threaded into renumber_sequences).
      trim_original_to:             Optional replacement text for the
                                     original section's body (heading kept,
                                     e.g. a "moved to <destination>" pointer).
                                     None (default) leaves the original fully
                                     untouched.
      draft_output_path:            fe989980 — same wave-scoped opt-in as
                                     move_section: when given with
                                     wave_run_id, the copy is written to this
                                     ISOLATED path instead of docx_path.
                                     Omitted (the default), byte-identical to
                                     pre-fe989980 behavior.
      wave_run_id:                  fe989980 — required together with
                                     draft_output_path; see move_section.
      session_id:                   273df573 — identifies the calling
                                     Meridian session to the tunnel-layer
                                     DOCX region-claim guard
                                     (check_docs_write_conflict in
                                     meridian/routes/tunnel.py). Not
                                     forwarded to docs_intel; has no effect
                                     when this tool is invoked outside
                                     Meridian's tunnel (e.g. standalone
                                     `uvx meridian-docs`).

    Returns:
      {status, section_id, heading_text, new_heading_para_id,
      copied_block_count, para_id_map, bookmark_map,
      destination_anchor_para_id, destination_position, renumber_sequences,
      find_references_to, trimmed_original, docx_path, wave_run_id,
      is_draft} or {error: <message>} (file NOT mutated on error).
    """
    return docs_intel.copy_section(
        docx_path=docx_path,
        section_id=section_id,
        destination_anchor_para_id=destination_anchor_para_id,
        destination_position=destination_position,
        index_db_path=index_db_path,
        trim_original_to=trim_original_to,
        draft_output_path=draft_output_path,
        wave_run_id=wave_run_id,
    )


@mcp.tool()
def relocate_figure(
    docx_path: str,
    figure_index: int,
    destination_anchor_para_id: str,
    destination_position: str = "after",
    index_db_path: str | None = None,
    allow_bookmark_split: bool = False,
    draft_output_path: str | None = None,
    wave_run_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Move an image paragraph together with its immediately following Figure caption.

    figure_index is the 1-based order among direct-body image paragraphs. The
    operation requires the next body child to be a SEQ Figure caption and
    preserves the original OOXML elements and relationship IDs. It rejects
    bookmark-splitting moves before writing, verifies the saved document,
    invalidates the local structure sidecar, and renumbers Figure SEQ/REF
    caches after a successful reorder.

    fe989980 — draft_output_path + wave_run_id (both or neither): when given,
    writes to the ISOLATED draft_output_path instead of docx_path, which is
    only ever read. Omitted (the default), byte-identical to pre-fe989980
    behavior. Pair with merge_docx_draft to promote an accepted draft into
    the canonical file.

    session_id: 273df573 — identifies the calling Meridian session to the
      tunnel-layer DOCX region-claim guard (check_docs_write_conflict in
      meridian/routes/tunnel.py). Not forwarded to docs_intel; has no effect
      when this tool is invoked outside Meridian's tunnel (e.g. standalone
      `uvx meridian-docs`).
    """
    return docs_intel.relocate_figure(
        docx_path=docx_path,
        figure_index=figure_index,
        destination_anchor_para_id=destination_anchor_para_id,
        destination_position=destination_position,
        index_db_path=index_db_path,
        allow_bookmark_split=allow_bookmark_split,
        draft_output_path=draft_output_path,
        wave_run_id=wave_run_id,
    )

@mcp.tool()
def relocate_table(
    docx_path: str,
    table_index: int,
    destination_anchor_para_id: str,
    destination_position: str = "after",
    index_db_path: str | None = None,
    allow_bookmark_split: bool = False,
    draft_output_path: str | None = None,
    wave_run_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """c031622b — Move an existing bare <w:tbl> (no owning heading) to a new
    location in the document, atomically.

    Unlike move_section (which locates its source via a heading's para_id),
    a bare table has no heading to anchor on: table_index identifies it by
    its own 0-based body-child position — the same "index" value
    index_document_structure stores in the docx_tables sidecar table and
    get_structure_elements returns for each entry in its "tables" list.

    Cuts the table from its current position and re-inserts it, as ONE
    atomic operation, relative to destination_anchor_para_id (same
    anchor/position convention as move_section / copy_section). Operates on
    the same live <w:tbl> element object, so w:tblPr, w:tblGrid, and any
    relationship reference inside a cell (image r:embed, hyperlink r:id) are
    carried verbatim — nothing is renamed, since nothing is duplicated.

    destination_position="after" onto a HEADING anchor lands the table after
    that heading's ENTIRE section (own body + subsections), the same fix
    move_section / copy_section rely on. Before anything is
    cut/spliced/saved, checks whether the move would split a bookmark across
    the table's own boundary and aborts (file untouched) unless
    allow_bookmark_split=True.

    Does NOT move an adjacent caption paragraph and does NOT call
    renumber_sequences — no caption travels with a bare table, so SEQ Table
    numbering is unaffected. If the table is actually owned by a heading, use
    move_section instead so the caption/heading move with it.

    Args:
      docx_path:                    Absolute path to the .docx file (mutated
                                     in place).
      table_index:                  0-based body-child position of the
                                     <w:tbl> to relocate.
      destination_anchor_para_id:   w14:paraId (or p{N}) of the paragraph/
                                     table to move the table next to. Must be
                                     OUTSIDE the table being moved.
      destination_position:         "before" or "after" (default).
      index_db_path:                If supplied, sidecar is invalidated
                                     after the write.
      allow_bookmark_split:         Explicit override (default False) to
                                     proceed even when the move would split a
                                     bookmark's start/end across the move
                                     boundary.
      draft_output_path:            fe989980 — same wave-scoped opt-in as
                                     move_section: when given with
                                     wave_run_id, the relocate is written to
                                     this ISOLATED path instead of docx_path.
                                     Omitted (the default), byte-identical to
                                     pre-fe989980 behavior.
      wave_run_id:                  fe989980 — required together with
                                     draft_output_path; see move_section.
      session_id:                   273df573 — identifies the calling
                                     Meridian session to the tunnel-layer
                                     DOCX region-claim guard
                                     (check_docs_write_conflict in
                                     meridian/routes/tunnel.py). Not
                                     forwarded to docs_intel; has no effect
                                     when this tool is invoked outside
                                     Meridian's tunnel (e.g. standalone
                                     `uvx meridian-docs`).

    Returns:
      {status, table_index, new_table_index, row_count, col_count,
      destination_anchor_para_id, destination_position, docx_path,
      wave_run_id, is_draft} or {error: <message>} (file NOT mutated on
      error; a blocked bookmark-split also returns
      {split_bookmarks: [...]}).
    """
    return docs_intel.relocate_table(
        docx_path=docx_path,
        table_index=table_index,
        destination_anchor_para_id=destination_anchor_para_id,
        destination_position=destination_position,
        index_db_path=index_db_path,
        allow_bookmark_split=allow_bookmark_split,
        draft_output_path=draft_output_path,
        wave_run_id=wave_run_id,
    )


@mcp.tool()
def insert_column(
    docx_path: str,
    table_index: int,
    col_index: int,
    position: str = "before",
    index_db_path: str | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """a2cd9f54 — Insert a new, empty grid column into an existing table.

    col_index addresses an existing GRID column (0-based); position
    ("before", default, or "after") says which side of it the new column
    lands on. For each row: if the insertion point falls strictly inside an
    existing horizontally-merged cell's span, that cell's w:gridSpan is
    incremented (the new column joins the merge) — otherwise a brand-new,
    empty cell is spliced in. w:tblGrid always gets exactly one new
    w:gridCol.

    Refuses (file untouched) with reason="ambiguous_grid" when the table's
    rows do not consistently account for its declared grid-column count.

    Mandatory post-write structural verification + a real Word/COM (or
    LibreOffice) render-capability check both run before this reports
    success — see allow_degraded_render / degraded_render_reason (same
    audited-opt-in contract as insert_caption).

    Args:
      docx_path:       Absolute path to the .docx file (mutated in place).
      table_index:      0-based body-child position of the <w:tbl> (same
                        addressing as relocate_table).
      col_index:        0-based existing grid-column index to insert
                        relative to.
      position:         "before" (default) or "after" col_index.
      index_db_path:    If supplied, sidecar is invalidated after write.
      allow_degraded_render: Explicit, audited opt-in to accept this write
                        when no render backend is available. Requires
                        degraded_render_reason.
      degraded_render_reason: Required, non-empty when
                        allow_degraded_render is True.
      session_id:       273df573 — identifies the calling Meridian session
                        to the tunnel-layer DOCX region-claim guard. Not
                        forwarded to docs_intel.

    Returns:
      {status, table_index, col_index, position, grid_col_count, row_count,
      col_count, docx_path, render_status, render_verified, render_backend,
      render_detail} or {error: <message>} (with "reason" one of
      "ambiguous_grid" when applicable) on failure.
    """
    return docs_intel.insert_column(
        docx_path=docx_path,
        table_index=table_index,
        col_index=col_index,
        position=position,
        index_db_path=index_db_path,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
    )


@mcp.tool()
def split_cell(
    docx_path: str,
    table_index: int,
    row_index: int,
    col_index: int,
    cols: int = 1,
    rows: int = 1,
    index_db_path: str | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """a2cd9f54 — Split one table cell into `cols` columns and/or `rows` rows.

    row_index is a 0-based <w:tr> index; col_index is the target cell's
    STARTING grid column. Refuses (file untouched) with
    reason="unsupported_merge" when the target cell already has
    w:gridSpan > 1 or any w:vMerge — splitting an already-merged cell is not
    attempted. Also refuses with reason="ambiguous_grid" (cols > 1 only)
    under the same inconsistent-table condition insert_column checks.

    Column split (cols > 1): the target cell is replaced by `cols`
    brand-new, independent cells; every OTHER row is widened by cols - 1
    grid columns via the same engine insert_column uses, so the whole table
    stays grid-consistent.

    Row split (rows > 1): rows - 1 brand-new <w:tr> are inserted immediately
    after the target row. The split cell's own column(s) get independent
    new content in each new row; every OTHER cell in the target row grows a
    w:vMerge spanning the new rows so the table stays visually rectangular.

    Mandatory post-write structural verification + a real render-capability
    check both run before this reports success — see allow_degraded_render.

    Args:
      docx_path:       Absolute path to the .docx file (mutated in place).
      table_index:     0-based body-child position of the <w:tbl>.
      row_index:       0-based <w:tr> index of the target cell.
      col_index:       Target cell's starting grid column.
      cols:            Number of columns to split into (default 1 = no
                       column split).
      rows:            Number of rows to split into (default 1 = no row
                       split). At least one of cols/rows must be > 1.
      index_db_path:   If supplied, sidecar is invalidated after write.
      allow_degraded_render: Same audited opt-in as insert_column.
      degraded_render_reason: Required, non-empty when
                       allow_degraded_render is True.
      session_id:      273df573 — identifies the calling Meridian session to
                       the tunnel-layer DOCX region-claim guard. Not
                       forwarded to docs_intel.

    Returns:
      {status, table_index, row_index, col_index, cols, rows, row_count,
      col_count, docx_path, render_status, render_verified, render_backend,
      render_detail} or {error: <message>} (with "reason" one of
      "unsupported_merge" / "ambiguous_grid" when applicable) on failure.
    """
    return docs_intel.split_cell(
        docx_path=docx_path,
        table_index=table_index,
        row_index=row_index,
        col_index=col_index,
        cols=cols,
        rows=rows,
        index_db_path=index_db_path,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
    )


@mcp.tool()
def transpose_table(
    docx_path: str,
    table_index: int,
    index_db_path: str | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """a2cd9f54 — Transpose a table's rows and columns in place.

    Supported ONLY for a fully rectangular table with NO w:gridSpan > 1 and
    NO w:vMerge anywhere — refuses with reason="unsupported_merge"
    otherwise (a horizontally-merged cell has no single canonical
    vertically-merged equivalent). Also refuses with reason="ambiguous_grid"
    if the table's rows do not all have the same cell count.

    Reuses the SAME <w:tc> element objects (never deep-copied), only
    repositioning them — every relationship id, bookmark, numbering
    reference, and run of formatted text inside a cell survives verbatim.

    Row heights and column widths have no canonical semantic mapping under
    a transpose: the new w:tblGrid falls back to the table's original total
    width divided evenly across the new column count — a documented,
    honest default. w:trPr (e.g. explicit row heights) is intentionally
    dropped from the new rows for the same reason.

    Mandatory post-write structural verification + a real render-capability
    check both run before this reports success — see allow_degraded_render.

    Args:
      docx_path:       Absolute path to the .docx file (mutated in place).
      table_index:      0-based body-child position of the <w:tbl>.
      index_db_path:    If supplied, sidecar is invalidated after write.
      allow_degraded_render: Same audited opt-in as insert_column.
      degraded_render_reason: Required, non-empty when
                        allow_degraded_render is True.
      session_id:       273df573 — identifies the calling Meridian session
                        to the tunnel-layer DOCX region-claim guard. Not
                        forwarded to docs_intel.

    Returns:
      {status, table_index, row_count, col_count, docx_path, render_status,
      render_verified, render_backend, render_detail} or {error: <message>}
      (with "reason" one of "unsupported_merge" / "ambiguous_grid" when
      applicable) on failure.
    """
    return docs_intel.transpose_table(
        docx_path=docx_path,
        table_index=table_index,
        index_db_path=index_db_path,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
    )


@mcp.tool()
def merge_docx_draft(
    canonical_path: str,
    draft_path: str,
    index_db_path: str | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """fe989980 — promote an isolated wave-scoped draft into canonical_path.

    The file-level counterpart to the Meridian core package's
    meridian.db.docx_merge coordination layer (open_merge_manifest /
    declare_merge_anchors / claim_merge_owner / check_merge_stale_or_overlap
    / record_merge_result / finalize_merge_manifest — a SEPARATE MCP
    connection, since this stdlib-only extension has no database access of
    its own). Call this ONLY after that DB-side gate has cleared for the
    caller — this tool performs no ownership/overlap/staleness checks
    itself; it only performs the physical promotion, verification, and
    restore-on-failure.

    draft_path must be a complete .docx previously produced by move_section /
    copy_section / relocate_table / relocate_figure called with
    draft_output_path (or any other isolated draft artifact). The draft's
    whole-document bytes are staged, checked against canonical_path's
    current media/style/relationship counts, and only then promoted (an
    existing canonical_path is backed up to canonical_path + ".bak" first).
    After promotion, canonical_path is re-read fresh from disk and compared
    against the draft's own structural counts + content hash; on any
    mismatch canonical_path is best-effort restored from that backup and
    this returns an error, never a false success.

    ddd79188 — AFTER that structural verification passes, a real
    render-capability check (check_render_capability) also runs against the
    now-promoted canonical_path: structural re-parse alone can never prove
    the promoted document actually opens/renders in Word. "rendered"
    continues normally with render evidence attached. "failed" (a render
    backend was available but errored on this document) restores
    canonical_path from the SAME backup and returns an error, same as a
    structural verification failure. "unavailable-with-reason" (no render
    backend in this environment) ALSO fails closed by default — never
    reported as verified — unless allow_degraded_render=True is passed
    together with a non-empty degraded_render_reason, an audited opt-in
    that keeps the promotion but stamps render_verified=False /
    render_degraded=True on the result instead of silently treating "could
    not check" as "passed".

    session_id: 273df573 — identifies the calling Meridian session to the
      tunnel-layer DOCX region-claim guard (check_docs_write_conflict in
      meridian/routes/tunnel.py; the guarded path is canonical_path, since
      that's the file this call mutates). Not forwarded to docs_intel; has
      no effect when this tool is invoked outside Meridian's tunnel (e.g.
      standalone `uvx meridian-docs`).

    allow_degraded_render / degraded_render_reason: see insert_figure_block's
      docstring for the shared contract — required together, and the
      reason is carried onto the result as an audit trail rather than
      persisted by this stdlib-only, DB-free extension itself.

    Returns {merged: True, status: "merged", canonical_path, draft_path,
    paragraph_count, heading_count, table_count, image_count, render_status,
    render_verified, render_backend, render_detail} on success, or
    {merged: False, error: <message>, ...} on failure — with
    file_restored: <bool> present only when a post-promotion structural- or
    render-verification failure triggered a restore.
    """
    return docs_intel.merge_draft_into_canonical(
        canonical_path=canonical_path,
        draft_path=draft_path,
        index_db_path=index_db_path,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
    )


def main() -> None:
    """Console entry point (``uvx meridian-docs``)."""
    mcp.run()


if __name__ == "__main__":
    main()
