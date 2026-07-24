"""OOXML-Graph — DOCX document intelligence layer, Phase 1 (618adf32).

A .docx is a ZIP of XML, so this Phase-1 slice needs no third-party dependency
(python-docx / lxml are absent here): it reads ``word/document.xml`` with the
stdlib ``zipfile`` + ``xml.etree.ElementTree`` and builds a *sidecar SQLite
index keyed by* ``w14:paraId`` — the stable, revision-independent paragraph id
Word assigns. That id is the anchor the rest of the tool set navigates by
(structure outline, targeted lookup, cross-reference, search).

Scope of Phase 1 (foundation): parse -> index -> structure navigation -> targeted
paragraph lookup -> text search. The full vision (13 MCP tools, track-changes
editing, cross-ref resolution, a standalone ``meridian-docs`` uvx package +
tunnel plugin, LaTeX addon) builds on these primitives.

c39ae092 — also exposes :func:`index_docx_structure` and
:func:`get_local_structure_elements`, which extend the SAME sidecar SQLite DB
(or a standalone one) to store structural elements (headings, figures, tables)
parsed from the .docx via the vendored ``document_content_tree`` parser.
This is the local-only fallback for :func:`ingest_local_document_structure`
that avoids the Cloudflare-blocked hosted POST.

9d749639 — DOCX write-back: captions (Figure / Table) + citations.
Adds insert/edit/remove for real Word Caption paragraphs (Caption style +
SEQ field) and CSL_CITATION complex fields (Zotero/Mendeley format).
Stdlib only (zipfile + xml.etree.ElementTree, no lxml).

a80af3a0 — OMML/equation support (local extraction + write-back).
Two extraction patterns: standalone paragraph (<m:oMath> alone in <w:p>)
and table-cell-with-numbering (2-col <w:tbl> row: equation | "(1)").
Write-back: insert_equation_local / edit_equation_local / remove_equation_local.
LaTeX -> OMML conversion via latex2mathml + stdlib ET mapper (no lxml).
Local sidecar persistence: index_docx_equations / get_local_equations.

Pure library — every function is deterministic and unit-tested against a
synthetic in-memory .docx (see tests/test_docs_intel.py).
"""
from __future__ import annotations

import copy
import io
import json
import os
import re
import shutil
import sqlite3
import uuid
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

# OOXML namespaces.
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"

# XML namespace for xml:space attribute.
_XML_NS = "http://www.w3.org/XML/1998/namespace"

# Register namespace prefixes so ET.tostring() produces "w:p" not "ns0:p".
# This must happen at module load time, before any ET.tostring() call.
ET.register_namespace("w", _W)
ET.register_namespace("w14", _W14)
ET.register_namespace("xml", _XML_NS)
# Additional namespaces commonly present in .docx documents.
ET.register_namespace("r", "http://schemas.openxmlformats.org/officeDocument/2006/relationships")
ET.register_namespace("cp", "http://schemas.openxmlformats.org/package/2006/metadata/core-properties")
ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")
ET.register_namespace("m", "http://schemas.openxmlformats.org/officeDocument/2006/math")
ET.register_namespace("mc", "http://schemas.openxmlformats.org/markup-compatibility/2006")
ET.register_namespace("o", "urn:schemas-microsoft-com:office:office")
ET.register_namespace("v", "urn:schemas-microsoft-com:vml")
ET.register_namespace("wpc", "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas")
ET.register_namespace("wps", "http://schemas.microsoft.com/office/word/2010/wordprocessingShape")
ET.register_namespace("wpg", "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup")
ET.register_namespace("wne", "http://schemas.microsoft.com/office/word/2006/wordml")


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _is_heading(style: str | None) -> bool:
    return bool(style) and str(style).lower().startswith("heading")


def _heading_level(style: str | None) -> int:
    match = re.search(r"(\d+)", style or "")
    return int(match.group(1)) if match else 1


# ---------------------------------------------------------------------------
# 4a07e566 — Section-type differentiation + w:sectPr page-numbering awareness
#
# Section types mirror the regions a Word academic document actually uses:
#   abstract   — "Abstract", "Summary", "Executive Summary" headings
#   toc        — "Table of Contents", "Contents", "TOC" headings (or TOC fields)
#   lof        — "List of Figures", "List of Tables", "Figures", "Tables"
#   appendix   — "Appendix" / "Annex" headings, or headings that follow the last
#                level-1 "References"/"Bibliography" heading
#   main       — everything else (the body of the document)
#
# The classifier is positional: it makes a single left-to-right pass over all
# headings, maintaining a running region state.  Ambiguous headings at lower
# levels inherit the region of the nearest preceding level-1 heading.
# ---------------------------------------------------------------------------

# Text patterns for known front-matter section types (case-insensitive).
_ABSTRACT_RE = re.compile(
    r"^(abstract|summary|executive\s+summary|synopsis|preface|foreword|acknowledgements?|dedication)$",
    re.IGNORECASE,
)
_TOC_RE = re.compile(
    r"^(table\s+of\s+contents?|contents?|toc)$",
    re.IGNORECASE,
)
_LOF_RE = re.compile(
    r"^(list\s+of\s+(figures?|tables?|illustrations?|abbreviations?|symbols?|equations?|listings?)"
    r"|figures?|tables?|illustrations?|abbreviations?|nomenclature)$",
    re.IGNORECASE,
)
_APPENDIX_RE = re.compile(
    r"^(appendix|appendices|annex|annexure|supplement)",
    re.IGNORECASE,
)
_REFERENCES_RE = re.compile(
    r"^(references?|bibliography|works?\s+cited|literature\s+cited)$",
    re.IGNORECASE,
)

SectionType = str  # "abstract" | "toc" | "lof" | "main" | "appendix"


def _classify_heading_text(text: str) -> SectionType | None:
    """Return a forced section type for well-known front/back-matter headings.

    Returns None for headings whose type must be inferred from position (main
    body content, or lower-level headings that inherit their parent's region).
    """
    t = text.strip()
    if _ABSTRACT_RE.match(t):
        return "abstract"
    if _TOC_RE.match(t):
        return "toc"
    if _LOF_RE.match(t):
        return "lof"
    if _APPENDIX_RE.match(t):
        return "appendix"
    return None


def _assign_section_types(
    headings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Annotate each heading dict with a ``section_type`` field.

    Single-pass left-to-right classifier.  Rules (in priority order):

    1. Text-pattern match always wins (abstract/toc/lof/appendix).
    2. A level-1 heading that matches the ``_REFERENCES_RE`` pattern moves the
       region to "appendix" (back matter begins after references).
    3. Headings that follow an "appendix" region-start remain "appendix".
    4. Headings in the front matter (before the first non-front-matter level-1)
       default to "abstract" when they don't match any explicit pattern.
    5. Everything else is "main".

    Returns a NEW list of dicts (copies) with the added ``section_type`` key.
    """
    in_front_matter = True  # before the first non-classified level-1 heading
    in_appendix = False
    result: list[dict[str, Any]] = []

    for h in headings:
        text = h.get("text", "")
        level = h.get("level", 1)

        forced = _classify_heading_text(text)

        if forced == "appendix":
            in_appendix = True
            in_front_matter = False
            section_type: SectionType = "appendix"
        elif in_appendix:
            section_type = "appendix"
        elif forced is not None:
            # abstract / toc / lof — these keep us in front matter
            section_type = forced
        elif level == 1 and _REFERENCES_RE.match(text.strip()):
            # References/bibliography marks the transition to back matter
            in_appendix = True
            in_front_matter = False
            section_type = "appendix"
        elif level == 1:
            # First unclassified level-1 heading ends front matter
            in_front_matter = False
            section_type = "main"
        else:
            # Sub-headings inherit the current region
            section_type = "abstract" if in_front_matter else "main"

        result.append({**h, "section_type": section_type})

    return result


# ---------------------------------------------------------------------------
# w:sectPr parsing — page-number format and restart between sections
# ---------------------------------------------------------------------------

def parse_sectpr(source: str | bytes | bytearray) -> dict[str, Any]:
    """4a07e566 — Parse all ``<w:sectPr>`` elements in a .docx body.

    A .docx uses ``<w:sectPr>`` to define section properties.  In a document
    with front matter (roman numeral page numbers) and body (arabic numerals),
    Word inserts a ``<w:sectPr>`` as the last child of a ``<w:pPr>`` at each
    section boundary, plus one final ``<w:sectPr>`` as a direct child of
    ``<w:body>`` for the last section.

    This function returns:

    ``{section_count, sections}``

    Each entry in ``sections`` is::

        {
          "index": int,              # 0-based order of sectPr in the body
          "page_num_fmt": str,       # "decimal" | "upperRoman" | "lowerRoman" |
                                     # "upperLetter" | "lowerLetter" | "none" | ...
          "page_num_start": int | None,  # w:start val (restart value), or None
          "page_num_type": str | None,   # raw w:pgNumType element summary
          "is_continuous": bool,     # True when w:type val="continuous"
          "anchor_para_id": str | None,  # paraId of the paragraph whose pPr
                                         # contains this sectPr (None for the
                                         # body-level final section)
        }

    When there are no ``<w:sectPr>`` elements (a document with a single
    implicit section), returns ``{section_count: 0, sections: []}``.
    """
    if isinstance(source, (bytes, bytearray)):
        zf = zipfile.ZipFile(io.BytesIO(bytes(source)))
    else:
        zf = zipfile.ZipFile(source)
    try:
        with zf.open("word/document.xml") as handle:
            xml = handle.read()
    finally:
        zf.close()

    root = ET.fromstring(xml)
    body = root.find(_q(_W, "body"))
    if body is None:
        return {"section_count": 0, "sections": []}

    sections: list[dict[str, Any]] = []
    w_sectPr = _q(_W, "sectPr")
    w_pPr = _q(_W, "pPr")
    w_pgNumType = _q(_W, "pgNumType")
    w_type = _q(_W, "type")
    w_fmt = _q(_W, "fmt")
    w_start = _q(_W, "start")
    w_val = _q(_W, "val")
    w14_paraId = _q(_W14, "paraId")

    for child in body:
        # sectPr can appear as a direct child of body (final section)
        # OR inside a paragraph's pPr (section boundary mid-document).
        if child.tag == _q(_W, "p"):
            ppr = child.find(w_pPr)
            if ppr is not None:
                spr = ppr.find(w_sectPr)
                if spr is not None:
                    anchor_id = child.get(w14_paraId) or None
                    sections.append(_parse_one_sectpr(spr, anchor_id, len(sections),
                                                      w_pgNumType, w_type, w_fmt, w_start, w_val))
        elif child.tag == w_sectPr:
            # Body-level final sectPr — no anchor paragraph
            sections.append(_parse_one_sectpr(child, None, len(sections),
                                              w_pgNumType, w_type, w_fmt, w_start, w_val))

    return {"section_count": len(sections), "sections": sections}


def _parse_one_sectpr(
    spr: ET.Element,
    anchor_para_id: str | None,
    index: int,
    w_pgNumType: str,
    w_type: str,
    w_fmt: str,
    w_start: str,
    w_val: str,
) -> dict[str, Any]:
    """Extract page-numbering fields from a single ``<w:sectPr>`` element."""
    pg_num = spr.find(w_pgNumType)
    page_num_fmt: str = "decimal"
    page_num_start: int | None = None
    page_num_type_raw: str | None = None
    is_continuous: bool = False

    # w:type val="continuous" means no page break at this section boundary.
    type_el = spr.find(w_type)
    if type_el is not None:
        is_continuous = type_el.get(w_val, "") == "continuous"

    if pg_num is not None:
        fmt_val = pg_num.get(w_fmt)
        if fmt_val:
            page_num_fmt = fmt_val
        start_val = pg_num.get(w_start)
        if start_val is not None:
            try:
                page_num_start = int(start_val)
            except ValueError:
                pass
        page_num_type_raw = ET.tostring(pg_num, encoding="unicode")

    return {
        "index": index,
        "page_num_fmt": page_num_fmt,
        "page_num_start": page_num_start,
        "page_num_type": page_num_type_raw,
        "is_continuous": is_continuous,
        "anchor_para_id": anchor_para_id,
    }


def parse_docx(source: str | bytes | bytearray) -> list[dict[str, Any]]:
    """Parse a .docx (path or raw bytes) into ordered paragraph records.

    Each record is ``{index, para_id, style, text}``. ``para_id`` is the
    ``w14:paraId`` when Word wrote one (stable across edits), else a synthesized
    ``p{index}`` so every paragraph is still addressable. Returns an empty list
    for a document with no body.
    """
    if isinstance(source, (bytes, bytearray)):
        zf = zipfile.ZipFile(io.BytesIO(bytes(source)))
    else:
        zf = zipfile.ZipFile(source)
    try:
        with zf.open("word/document.xml") as handle:
            xml = handle.read()
    finally:
        zf.close()
    root = ET.fromstring(xml)
    body = root.find(_q(_W, "body"))
    paragraphs: list[dict[str, Any]] = []
    if body is None:
        return paragraphs
    for index, p in enumerate(body.findall(_q(_W, "p"))):
        para_id = p.get(_q(_W14, "paraId")) or f"p{index}"
        style: str | None = None
        ppr = p.find(_q(_W, "pPr"))
        if ppr is not None:
            pstyle = ppr.find(_q(_W, "pStyle"))
            if pstyle is not None:
                style = pstyle.get(_q(_W, "val"))
        text = "".join(t.text or "" for t in p.iter(_q(_W, "t")))
        paragraphs.append(
            {"index": index, "para_id": para_id, "style": style, "text": text}
        )
    return paragraphs


def document_outline(source: str | bytes | bytearray) -> dict[str, Any]:
    """13462df2 — stateless heading outline of a .docx (path or raw bytes). No
    sidecar index: a pure parse. Returns ``paragraph_count`` + ``heading_count``
    + an ordered ``headings`` list (level/text/para_id/section_type) — the
    queryable document structure docs_intel exposes without building a persistent
    index.

    4a07e566 — each heading now carries a ``section_type`` field
    (abstract/toc/lof/main/appendix) classifying the document region it belongs
    to. The ``section_regions`` key summarises the distinct regions in order.
    """
    paras = parse_docx(source)
    raw_headings = [
        {
            "level": _heading_level(p.get("style")),
            "text": p.get("text", ""),
            "para_id": p.get("para_id"),
        }
        for p in paras
        if _is_heading(p.get("style"))
    ]
    headings = _assign_section_types(raw_headings)

    # Collect the ordered distinct regions (deduped, preserving first-seen order).
    seen_regions: list[str] = []
    for h in headings:
        r = h["section_type"]
        if not seen_regions or seen_regions[-1] != r:
            seen_regions.append(r)

    return {
        "paragraph_count": len(paras),
        "heading_count": len(headings),
        "headings": headings,
        "section_regions": seen_regions,
    }


def _connect(index_db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(index_db_path)
    # 05256d4a -- WAL mode: journal_mode was never configured anywhere in this
    # module, so every connection defaulted to SQLite's rollback-journal mode.
    # WAL lets readers proceed concurrently with a writer (no exclusive lock
    # for the whole transaction) and generally reduces fsync overhead for the
    # small, frequent writes index_docx's delta-update path now does. A no-op
    # (silently ignored by SQLite) for :memory:/temporary databases, which
    # always report/keep journal_mode 'memory' regardless of this pragma.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS docx_paragraphs (
            para_id TEXT PRIMARY KEY,
            idx INTEGER NOT NULL,
            style TEXT,
            text TEXT NOT NULL
        )
        """
    )
    # 2426dce9 — staleness metadata: the source .docx's own path + mtime at
    # index time, so a read call can detect "the file changed since I was
    # last indexed" instead of silently serving a stale cached paragraph
    # table forever. NULL source_path (source was raw bytes, not a file) means
    # no staleness check is possible for this index — reads just skip it.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS docx_index_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    # c39ae092 — structural element tables: headings, figures, tables.
    # These extend the same sidecar DB so structural elements live alongside
    # the paragraph index without requiring a hosted POST.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS docx_headings (
            para_id TEXT PRIMARY KEY,
            idx INTEGER NOT NULL,
            level INTEGER NOT NULL,
            text TEXT NOT NULL,
            section_type TEXT
        )
        """
    )
    # 4a07e566 — migrate existing DBs: add section_type column if absent.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(docx_headings)").fetchall()}
    if "section_type" not in cols:
        conn.execute("ALTER TABLE docx_headings ADD COLUMN section_type TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS docx_figures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idx INTEGER NOT NULL,
            para_id TEXT,
            caption TEXT NOT NULL,
            seq_number TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS docx_tables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idx INTEGER NOT NULL,
            row_count INTEGER NOT NULL,
            col_count INTEGER NOT NULL,
            caption TEXT,
            rows_json TEXT NOT NULL
        )
        """
    )
    # 1c59cb90 — cross-reference bookmark column: records the "_Ref######"
    # bookmark name wrapping each caption's "<Kind> <N>" text so a REF field
    # elsewhere in the document can target it (see insert_cross_reference).
    fig_cols = {row[1] for row in conn.execute("PRAGMA table_info(docx_figures)").fetchall()}
    if "ref_bookmark" not in fig_cols:
        conn.execute("ALTER TABLE docx_figures ADD COLUMN ref_bookmark TEXT")
    tbl_cols = {row[1] for row in conn.execute("PRAGMA table_info(docx_tables)").fetchall()}
    if "ref_bookmark" not in tbl_cols:
        conn.execute("ALTER TABLE docx_tables ADD COLUMN ref_bookmark TEXT")
    # f1a92d6e -- internal-author-note audit table: records notes written by
    # insert_highlighted_note so list_internal_notes can query them without
    # re-parsing the .docx. See the "9 new primitives" section near the end
    # of this module.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS docx_internal_notes (
            note_id TEXT PRIMARY KEY,
            anchor_para_id TEXT,
            text TEXT NOT NULL
        )
        """
    )
    # 32d84131 — SQLite FTS5 external-content table for BM25 full-text search
    # over paragraph text. Uses external-content mode pointing at docx_paragraphs
    # so the text is not duplicated on disk. The FTS index is rebuilt atomically
    # by index_docx() after each paragraph table replacement; sync triggers keep
    # it consistent for incremental writes (caption inserts, etc.).
    #
    # WHY FTS5 (NOT Tantivy): meridian-docs is a stdlib-only, uvx-installable
    # extension (no compiled C extensions, no Rust binaries). SQLite FTS5 is
    # built into Python's sqlite3 module on all platforms, requires zero extra
    # dependencies, and provides native BM25 ranking via bm25(docx_fts).
    # Tantivy (used by meridian-outputs for its own FTS) requires a compiled
    # Rust extension and is a heavyweight dependency — inappropriate for a
    # lightweight DOCX parsing extension that ships as a pure-Python package.
    # The two subsystems are independent: meridian-outputs indexes flat output
    # files across a directory tree (high volume, append-heavy); meridian-docs
    # indexes paragraphs of a single DOCX in a sidecar SQLite (low volume,
    # rebuild-on-reindex). FTS5 is the right tool here. See decision pinned
    # under "meridian-docs uses SQLite FTS5, not Tantivy".
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS docx_fts
        USING fts5(
            text,
            content='docx_paragraphs',
            content_rowid='rowid'
        )
        """
    )
    # Sync triggers: keep docx_fts consistent when docx_paragraphs rows are
    # inserted or deleted individually (e.g. during caption write-back).
    # Full rebuilds via index_docx() bypass these and call
    # INSERT INTO docx_fts(docx_fts) VALUES('rebuild') directly.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS docx_paragraphs_ai
        AFTER INSERT ON docx_paragraphs BEGIN
            INSERT INTO docx_fts(rowid, text) VALUES (new.rowid, new.text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS docx_paragraphs_ad
        AFTER DELETE ON docx_paragraphs BEGIN
            INSERT INTO docx_fts(docx_fts, rowid, text)
            VALUES ('delete', old.rowid, old.text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS docx_paragraphs_au
        AFTER UPDATE ON docx_paragraphs BEGIN
            INSERT INTO docx_fts(docx_fts, rowid, text)
            VALUES ('delete', old.rowid, old.text);
            INSERT INTO docx_fts(rowid, text) VALUES (new.rowid, new.text);
        END
        """
    )
    return conn


def _stat_mtime(path: str) -> float | None:
    """Best-effort mtime lookup — None if the path is missing/unreadable."""
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def check_staleness(index_db_path: str) -> dict[str, Any]:
    """2426dce9 — compare the indexed source's stored mtime against its current
    mtime on disk. Returns {"stale": bool, "source_path": str|None, "reason": str}.
    A missing source_path (bytes-sourced index, or never set) always reports
    stale=False with reason "no-source-tracked" — there is nothing to compare
    against, so silence rather than a false-positive staleness claim.
    """
    conn = _connect(index_db_path)
    try:
        rows = dict(
            conn.execute(
                "SELECT key, value FROM docx_index_meta "
                "WHERE key IN ('source_path', 'source_mtime')"
            ).fetchall()
        )
    finally:
        conn.close()
    source_path = rows.get("source_path")
    if not source_path:
        return {"stale": False, "source_path": None, "reason": "no-source-tracked"}
    stored_mtime = rows.get("source_mtime")
    current_mtime = _stat_mtime(source_path)
    if current_mtime is None:
        return {"stale": False, "source_path": source_path, "reason": "source-unreadable"}
    if stored_mtime is None or float(stored_mtime) != current_mtime:
        return {"stale": True, "source_path": source_path, "reason": "mtime-mismatch"}
    return {"stale": False, "source_path": source_path, "reason": "current"}


def _ensure_fresh(index_db_path: str) -> None:
    """2426dce9 — auto-reindex transparently if the source has changed since
    the last index_docx call, so read functions never silently serve stale
    data. A no-op when there's nothing trackable (bytes-sourced index) or the
    source file is genuinely unreadable right now — those cases fall through
    to whatever's already in the index rather than raising.
    """
    info = check_staleness(index_db_path)
    if info["stale"] and info["source_path"]:
        index_docx(info["source_path"], index_db_path)


def index_docx(
    source: str | bytes | bytearray, index_db_path: str
) -> dict[str, Any]:
    """Build (or incrementally update) a sidecar SQLite index of a .docx keyed
    by paraId.

    Returns a summary ``{index_db, paragraph_count, heading_count, delta}``.
    Idempotent: re-indexing an edited doc always converges to the exact same
    ``docx_paragraphs`` contents a fresh index would produce.

    05256d4a — DELTA update, not a full rebuild: paragraphs are diffed against
    what's already stored, by ``para_id`` (the existing ``TEXT PRIMARY KEY``
    on ``docx_paragraphs``) plus an ``(idx, style, text)`` content comparison,
    and only the rows that actually differ are INSERTed / UPDATEd / DELETEd.
    An unchanged document touches zero rows. A one-paragraph text edit touches
    exactly that paragraph's row (plus any later paragraph whose ``idx``
    genuinely shifted because paragraphs were added/removed -- not relevant
    to a same-length in-place edit). This replaces the previous
    unconditional ``DELETE FROM docx_paragraphs`` + full reinsert + explicit
    FTS5 ``'rebuild'`` on every single call, which was a full-table rewrite
    even for a one-paragraph change.

    docx_paragraphs' existing per-row ``docx_paragraphs_ai``/``_au``/``_ad``
    triggers already keep ``docx_fts`` incrementally consistent on INSERT /
    UPDATE / DELETE (see :func:`_connect`) -- so a delta write no longer needs
    the explicit full ``'rebuild'`` command at all; only the FTS postings for
    rows that actually changed are touched, both for the first-ever index
    (every row is a fresh INSERT, still trigger-driven, one row at a time)
    and for every subsequent delta update.

    2426dce9 — when ``source`` is a file path (not raw bytes), the path and its
    current mtime are stamped into ``docx_index_meta`` so a later read call can
    detect the source changed since this index and auto-refresh (see
    :func:`_ensure_fresh`) instead of silently serving stale paragraphs. Bytes
    sources carry no path to track, so no staleness check is possible for them
    — read calls on such an index simply skip the check.
    """
    paragraphs = parse_docx(source)
    conn = _connect(index_db_path)
    try:
        existing = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute("SELECT para_id, idx, style, text FROM docx_paragraphs")
        }
        seen_ids: set[str] = set()
        inserted = updated = unchanged = 0
        for p in paragraphs:
            pid = p["para_id"]
            seen_ids.add(pid)
            new_sig = (p["index"], p["style"], p["text"])
            old_sig = existing.get(pid)
            if old_sig is None:
                conn.execute(
                    "INSERT INTO docx_paragraphs (para_id, idx, style, text) "
                    "VALUES (?, ?, ?, ?)",
                    (pid, p["index"], p["style"], p["text"]),
                )
                inserted += 1
            elif old_sig != new_sig:
                conn.execute(
                    "UPDATE docx_paragraphs SET idx = ?, style = ?, text = ? "
                    "WHERE para_id = ?",
                    (p["index"], p["style"], p["text"], pid),
                )
                updated += 1
            else:
                unchanged += 1

        stale_ids = existing.keys() - seen_ids
        if stale_ids:
            conn.executemany(
                "DELETE FROM docx_paragraphs WHERE para_id = ?",
                [(pid,) for pid in stale_ids],
            )
        deleted = len(stale_ids)

        if isinstance(source, str):
            mtime = _stat_mtime(source)
            conn.execute(
                "INSERT OR REPLACE INTO docx_index_meta (key, value) VALUES (?, ?)",
                ("source_path", source),
            )
            conn.execute(
                "INSERT OR REPLACE INTO docx_index_meta (key, value) VALUES (?, ?)",
                ("source_mtime", str(mtime) if mtime is not None else None),
            )
        conn.commit()
    finally:
        conn.close()
    return {
        "index_db": index_db_path,
        "paragraph_count": len(paragraphs),
        "heading_count": sum(1 for p in paragraphs if _is_heading(p["style"])),
        "delta": {
            "inserted": inserted,
            "updated": updated,
            "deleted": deleted,
            "unchanged": unchanged,
        },
    }


def get_paragraph(index_db_path: str, para_id: str) -> dict[str, Any] | None:
    """Look up one paragraph by its ``w14:paraId`` (the targeted-navigation op).

    2426dce9 — auto-refreshes the index first if the source .docx changed
    since it was last indexed (see :func:`_ensure_fresh`).
    """
    _ensure_fresh(index_db_path)
    conn = _connect(index_db_path)
    try:
        cur = conn.execute(
            "SELECT para_id, idx, style, text FROM docx_paragraphs WHERE para_id = ?",
            (para_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"para_id": row[0], "index": row[1], "style": row[2], "text": row[3]}


def get_structure(index_db_path: str) -> list[dict[str, Any]]:
    """Return the heading outline (para_id, level, text) in document order.

    2426dce9 — auto-refreshes the index first if the source .docx changed
    since it was last indexed (see :func:`_ensure_fresh`).
    """
    _ensure_fresh(index_db_path)
    conn = _connect(index_db_path)
    try:
        rows = conn.execute(
            "SELECT para_id, idx, style, text FROM docx_paragraphs ORDER BY idx"
        ).fetchall()
    finally:
        conn.close()
    outline: list[dict[str, Any]] = []
    for para_id, _idx, style, text in rows:
        if _is_heading(style):
            outline.append(
                {"para_id": para_id, "level": _heading_level(style), "text": text}
            )
    return outline


def find_paragraphs(
    index_db_path: str, query: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Substring search over paragraph text (document order); returns paraIds.

    2426dce9 — auto-refreshes the index first if the source .docx changed
    since it was last indexed (see :func:`_ensure_fresh`).
    """
    _ensure_fresh(index_db_path)
    conn = _connect(index_db_path)
    try:
        rows = conn.execute(
            "SELECT para_id, idx, style, text FROM docx_paragraphs "
            "WHERE text LIKE ? ORDER BY idx LIMIT ?",
            (f"%{query}%", int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"para_id": r[0], "index": r[1], "style": r[2], "text": r[3]} for r in rows
    ]


def fts5_search_paragraphs(
    index_db_path: str, query: str, limit: int = 20
) -> list[dict[str, Any]]:
    """32d84131 — BM25 full-text search over paragraph text via SQLite FTS5.

    Uses the ``docx_fts`` external-content FTS5 virtual table (backed by
    ``docx_paragraphs``) populated by :func:`index_docx`. Results are ranked
    by BM25 relevance (most relevant first); document order is NOT preserved
    (use :func:`find_paragraphs` for substring/order-preserving scan).

    The FTS5 query syntax is a subset of SQLite FTS5 query syntax:
    bare tokens are AND-ed by default; ``OR``, ``NOT``, and phrase queries
    (``"two words"``) are supported. Wildcards (``term*``) are supported
    for prefix matching.

    Auto-refreshes the index if the source .docx changed since last indexed
    (see :func:`_ensure_fresh`).

    Args:
        index_db_path:  Path to the sidecar SQLite index built by
                        :func:`index_docx`.
        query:          FTS5 query string (e.g. ``"meridian"`` or
                        ``'"AI sessions"'`` for a phrase).
        limit:          Maximum number of results to return (default 20).

    Returns:
        List of ``{para_id, index, style, text, bm25_score}`` dicts ordered
        by relevance (best match first). Empty list when no matches or when
        the FTS5 index has not been built yet (first call before
        :func:`index_docx`).
    """
    _ensure_fresh(index_db_path)
    conn = _connect(index_db_path)
    try:
        rows = conn.execute(
            """
            SELECT p.para_id, p.idx, p.style, p.text,
                   bm25(docx_fts) AS score
            FROM docx_fts
            JOIN docx_paragraphs p ON p.rowid = docx_fts.rowid
            WHERE docx_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (query, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        # FTS5 table may not exist in an older sidecar that pre-dates 32d84131,
        # or the query may be syntactically invalid — degrade to empty rather
        # than raising so callers get a consistent empty-list signal.
        return []
    finally:
        conn.close()
    return [
        {
            "para_id": r[0],
            "index": r[1],
            "style": r[2],
            "text": r[3],
            "bm25_score": r[4],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# c39ae092 — local structural element store: headings / figures / tables
# stored in the SAME sidecar SQLite DB (or a standalone one) without any
# hosted round-trip. This is the local-only fallback for
# ingest_local_document_structure that avoids the Cloudflare-blocked POST.
# ---------------------------------------------------------------------------

_SEQ_FIGURE_RE = re.compile(r"\bSEQ\s+Figure\b", re.IGNORECASE)
_SEQ_TABLE_RE = re.compile(r"\bSEQ\s+Table\b", re.IGNORECASE)


def _is_figure_caption(block: dict[str, Any]) -> bool:
    """Return True if a paragraph block contains a SEQ Figure field (figure caption)."""
    for fld in block.get("fields", []):
        if fld.get("field_type") == "SEQ" and _SEQ_FIGURE_RE.search(
            fld.get("instruction", "")
        ):
            return True
    return False


def _is_table_caption(block: dict[str, Any]) -> bool:
    """Return True if a paragraph block contains a SEQ Table field (table caption)."""
    for fld in block.get("fields", []):
        if fld.get("field_type") == "SEQ" and _SEQ_TABLE_RE.search(
            fld.get("instruction", "")
        ):
            return True
    return False


def _seq_cached_number(block: dict[str, Any], seq_re: re.Pattern[str]) -> str | None:
    """Extract the cached sequence number (e.g. '1') from a SEQ field."""
    for fld in block.get("fields", []):
        if fld.get("field_type") == "SEQ" and seq_re.search(fld.get("instruction", "")):
            return fld.get("cached_result") or None
    return None


def index_docx_structure(
    source: str | bytes | bytearray,
    index_db_path: str,
) -> dict[str, Any]:
    """c39ae092 — parse a .docx and store its structural elements locally.

    Extends the sidecar SQLite DB at ``index_db_path`` (created if absent) with
    three new tables — ``docx_headings``, ``docx_figures``, ``docx_tables`` —
    populated from the blocks produced by :func:`document_content_tree`.

    This is the local-only substitute for forwarding blocks to the hosted
    ``ingest_document_structure`` endpoint: it produces the same queryable
    structural index without any network call.

    Figure captions are detected by SEQ Figure field codes in paragraph blocks;
    table captions by SEQ Table field codes.  Raw table blocks (``kind="table"``)
    are stored with their cell data serialised as JSON.

    Returns a summary ``{index_db, heading_count, figure_count, table_count}``.
    Idempotent: all three structural tables are fully replaced on each run.
    """
    from ._vendored_content_tree import document_content_tree  # noqa: PLC0415

    tree = document_content_tree(source)
    blocks: list[dict[str, Any]] = tree.get("blocks") or []

    headings: list[tuple[str, int, int, str]] = []  # (para_id, idx, level, text)
    figures: list[tuple[int, str | None, str, str | None]] = []  # (idx, para_id, caption, seq_num)
    tables: list[tuple[int, int, int, str]] = []  # (idx, row_count, col_count, rows_json)

    # Track the most recent table block so its following SEQ Table caption
    # can be linked back.
    last_table_idx: int | None = None

    for block in blocks:
        kind = block.get("kind")
        idx = block.get("index", 0)

        if kind == "heading":
            headings.append((
                block.get("para_id", f"p{idx}"),
                idx,
                block.get("level", 1),
                block.get("text", ""),
            ))
        elif kind == "table":
            rows = block.get("rows") or []
            tables.append((
                idx,
                block.get("row_count", len(rows)),
                block.get("col_count", max((len(r) for r in rows), default=0)),
                json.dumps(rows),
            ))
            last_table_idx = idx
        elif kind == "paragraph":
            if _is_figure_caption(block):
                seq_num = _seq_cached_number(block, _SEQ_FIGURE_RE)
                figures.append((idx, block.get("para_id"), block.get("text", ""), seq_num))
            elif _is_table_caption(block):
                # A table-caption paragraph immediately follows its table;
                # store as the caption of the most recent table by updating
                # rows list after-the-fact. (We append a separate figure-like
                # record keyed to the table idx for easy lookup.)
                seq_num = _seq_cached_number(block, _SEQ_TABLE_RE)
                # Store in figures list under kind=table_caption for distinction?
                # Keep separate: we attach caption to the table by position.
                # Simplest: find the table with last_table_idx and update its
                # caption row.  Since we build tables as a list we patch the
                # last entry.
                if tables and last_table_idx is not None:
                    t = tables[-1]
                    tables[-1] = (t[0], t[1], t[2], t[3])
                    # Store caption separately in the table record below.
                    # Use a sentinel: append a 5-tuple when there's a caption.
                    # Instead, switch to a dict approach for clarity.
                    pass

    # Rebuild tables with caption support (5-tuple: idx, row_count, col_count, rows_json, caption).
    # Re-parse to attach captions properly.
    # 4a07e566 — headings are collected as dicts so section_type can be assigned.
    headings_raw: list[dict[str, Any]] = []
    figures_out: list[tuple[int, str | None, str, str | None]] = []
    tables_out: list[tuple[int, int, int, str, str | None]] = []  # (..., caption)

    last_table_entry_index: int | None = None

    for block in blocks:
        kind = block.get("kind")
        idx = block.get("index", 0)

        if kind == "heading":
            headings_raw.append({
                "para_id": block.get("para_id", f"p{idx}"),
                "idx": idx,
                "level": block.get("level", 1),
                "text": block.get("text", ""),
            })
        elif kind == "table":
            rows = block.get("rows") or []
            tables_out.append((
                idx,
                block.get("row_count", len(rows)),
                block.get("col_count", max((len(r) for r in rows), default=0)),
                json.dumps(rows),
                None,  # caption filled in by following SEQ Table para
            ))
            last_table_entry_index = len(tables_out) - 1
        elif kind == "paragraph":
            if _is_figure_caption(block):
                seq_num = _seq_cached_number(block, _SEQ_FIGURE_RE)
                figures_out.append((idx, block.get("para_id"), block.get("text", ""), seq_num))
            elif _is_table_caption(block) and last_table_entry_index is not None:
                # Patch the caption onto the preceding table entry.
                t = tables_out[last_table_entry_index]
                tables_out[last_table_entry_index] = (t[0], t[1], t[2], t[3], block.get("text", ""))

    # 4a07e566 — classify section types for all headings in one pass.
    typed_headings = _assign_section_types([
        {"level": h["level"], "text": h["text"], "para_id": h["para_id"]}
        for h in headings_raw
    ])
    headings_out: list[tuple[str, int, int, str, str | None]] = [
        (typed["para_id"], headings_raw[i]["idx"], typed["level"],
         typed["text"], typed["section_type"])
        for i, typed in enumerate(typed_headings)
    ]

    conn = _connect(index_db_path)
    try:
        conn.execute("DELETE FROM docx_headings")
        conn.execute("DELETE FROM docx_figures")
        conn.execute("DELETE FROM docx_tables")

        conn.executemany(
            "INSERT OR REPLACE INTO docx_headings (para_id, idx, level, text, section_type) "
            "VALUES (?, ?, ?, ?, ?)",
            headings_out,
        )
        conn.executemany(
            "INSERT INTO docx_figures (idx, para_id, caption, seq_number) "
            "VALUES (?, ?, ?, ?)",
            figures_out,
        )
        conn.executemany(
            "INSERT INTO docx_tables (idx, row_count, col_count, rows_json, caption) "
            "VALUES (?, ?, ?, ?, ?)",
            tables_out,
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "index_db": index_db_path,
        "heading_count": len(headings_out),
        "figure_count": len(figures_out),
        "table_count": len(tables_out),
    }


def get_local_structure_elements(index_db_path: str) -> dict[str, Any]:
    """c39ae092 — retrieve all locally-stored structural elements from the sidecar.

    Returns ``{headings, figures, tables}`` lists read from the
    ``docx_headings``, ``docx_figures``, ``docx_tables`` tables populated by
    :func:`index_docx_structure`.  Returns empty lists for any table that
    does not yet exist (i.e., :func:`index_docx_structure` was never called on
    this sidecar).
    """
    conn = _connect(index_db_path)
    try:
        heading_rows = conn.execute(
            "SELECT para_id, idx, level, text, section_type FROM docx_headings ORDER BY idx"
        ).fetchall()
        figure_rows = conn.execute(
            "SELECT id, idx, para_id, caption, seq_number FROM docx_figures ORDER BY idx"
        ).fetchall()
        table_rows = conn.execute(
            "SELECT id, idx, row_count, col_count, rows_json, caption FROM docx_tables ORDER BY idx"
        ).fetchall()
    finally:
        conn.close()

    headings = [
        {"para_id": r[0], "index": r[1], "level": r[2], "text": r[3], "section_type": r[4]}
        for r in heading_rows
    ]
    figures = [
        {
            "id": r[0],
            "index": r[1],
            "para_id": r[2],
            "caption": r[3],
            "seq_number": r[4],
        }
        for r in figure_rows
    ]
    tables = [
        {
            "id": r[0],
            "index": r[1],
            "row_count": r[2],
            "col_count": r[3],
            "rows": json.loads(r[4]) if r[4] else [],
            "caption": r[5],
        }
        for r in table_rows
    ]
    return {
        "headings": headings,
        "figures": figures,
        "tables": tables,
        "heading_count": len(headings),
        "figure_count": len(figures),
        "table_count": len(tables),
    }


def get_document_section_map(source: str | bytes | bytearray) -> dict[str, Any]:
    """4a07e566 — Return a full section-type map + sectPr page-numbering summary.

    Combines :func:`document_outline` (section-typed heading outline) with
    :func:`parse_sectpr` (w:sectPr multi-section page-numbering) into a single
    document intelligence view.

    Returns::

        {
          "paragraph_count": int,
          "heading_count": int,
          "headings": [{level, text, para_id, section_type}, ...],
          "section_regions": [str, ...],   # distinct ordered regions
          "sectpr": {
            "section_count": int,
            "sections": [{index, page_num_fmt, page_num_start,
                          page_num_type, is_continuous, anchor_para_id}, ...]
          }
        }

    When there are no ``<w:sectPr>`` elements the ``sectpr.sections`` list is
    empty, indicating a single implicit section with no explicit page-number
    restart.
    """
    outline = document_outline(source)
    sectpr = parse_sectpr(source)
    return {
        "paragraph_count": outline["paragraph_count"],
        "heading_count": outline["heading_count"],
        "headings": outline["headings"],
        "section_regions": outline["section_regions"],
        "sectpr": sectpr,
    }


# ---------------------------------------------------------------------------
# 9d749639 — DOCX write-back: captions (Figure / Table) + citations
#
# Design decisions:
#   - Stdlib only: zipfile + xml.etree.ElementTree, no lxml.
#   - Shared _load_docx_xml_stdlib / _save_docx_xml_stdlib helpers (pattern
#     from meridian/doc_store.py, ported to ET rather than lxml).
#   - Caption insert/edit/remove: real Word Caption style + SEQ field.
#   - Citation insert/edit/remove: ADDIN ZOTERO_ITEM CSL_CITATION complex field.
#   - After every successful write the sidecar is invalidated so _ensure_fresh()
#     triggers a re-index on the next read call.
# ---------------------------------------------------------------------------

# The paragraph style name Word uses for all figure/table captions.
_CAPTION_STYLE = "Caption"

# SEQ field instruction templates (shared for Figure and Table).
_SEQ_FIGURE_INSTR = "SEQ Figure \\* ARABIC"
_SEQ_TABLE_INSTR = "SEQ Table \\* ARABIC"

# 1c59cb90 — cross-reference bookmark prefix, matching the naming convention
# Word itself uses for caption bookmarks ("_Ref123456789"). A REF field
# elsewhere in the document targets this bookmark's name so Word's field
# refresh (F9) — not a fixed literal string — resolves prose like "Figure 3"
# even after captions are reordered/renumbered.
_REF_BOOKMARK_PREFIX = "_Ref"
_REF_BOOKMARK_RE = re.compile(r"^_Ref(\d+)$")


def _load_docx_xml_stdlib(path: str) -> tuple[bytes, ET.Element]:
    """Read a .docx at ``path`` and return ``(raw_bytes, document_root)``.

    ``raw_bytes`` is the full original ZIP content (used for re-packing);
    ``document_root`` is the parsed ``<w:document>`` element from
    ``word/document.xml``.

    Raises ``FileNotFoundError`` when the path is absent.
    Raises ``ValueError`` when the file is not a valid .docx ZIP or is missing
    ``word/document.xml``.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"no such file: {path}")
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a valid .docx (not a ZIP): {path}") from exc
    try:
        try:
            with zf.open("word/document.xml") as handle:
                xml_bytes = handle.read()
        except KeyError as exc:
            raise ValueError(
                f"not a valid .docx: missing word/document.xml: {path}"
            ) from exc
    finally:
        zf.close()
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"malformed word/document.xml in {path}: {exc}") from exc
    return raw, root


def _save_docx_xml_stdlib(raw: bytes, root: ET.Element, dest: str) -> None:
    """Write ``root`` back into ``dest`` as ``word/document.xml``.

    All other ZIP members from ``raw`` are preserved byte-for-byte.
    Writes to a BytesIO buffer first, then flushes to disk.

    Backs up the existing file to ``dest + ".bak"`` when it already exists
    (best-effort, non-fatal on failure — same pattern as meridian/doc_store.py).
    """
    new_xml = ET.tostring(root, encoding="unicode")
    new_document_bytes = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + new_xml
    ).encode("utf-8")

    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src:
        infos = src.infolist()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in infos:
                data = src.read(info.filename)
                if info.filename == "word/document.xml":
                    data = new_document_bytes
                dst.writestr(info, data)

    if os.path.exists(dest):
        backup = dest + ".bak"
        try:
            shutil.copy2(dest, backup)
        except OSError:
            pass  # backup failure is non-fatal

    with open(dest, "wb") as fh:
        fh.write(out.getvalue())


def _find_para_by_id(
    root: ET.Element, para_id: str
) -> tuple[ET.Element, ET.Element, int] | None:
    """Return ``(body, paragraph_element, body_child_index)`` for the given para_id.

    7600db1c -- resolution order matches the same three id schemes
    :func:`_vendored_content_tree.document_content_tree` (and therefore
    :func:`get_section_content`, :func:`document_outline`,
    :func:`parse_document`) already resolve paragraphs by, so any id those
    functions hand back to a caller resolves correctly here too:

      1. Native ``w14:paraId`` (Word-assigned, 8 hex digits).
      2. Synthesized ``sp<hash>`` id from
         :func:`_vendored_content_tree._build_synth_id_map` -- the stable,
         content-derived id emitted for any direct-body-child paragraph that
         lacks a native id. This is the id real documents actually carry for
         almost all figure/table captions and most headings (Word does not
         assign w14:paraId to every paragraph), so resolving it here is what
         makes every one of this helper's 17 call sites (insert_caption,
         insert_cross_reference, insert_equation_local, write_section,
         move_section, copy_section, find_references_to, etc.) work on real
         documents instead of only on synthetic fixtures built with native
         ids on every paragraph.
      3. The old positional ``p{N}`` fallback (kept for backward
         compatibility with any caller still passing it; ``N`` counts every
         ``<w:p>`` in document order, including inside tables).

    Only schemes 1 and 3 apply to paragraphs inside tables -- like
    ``document_content_tree``, the synth-id map is built over direct body
    children only (table-cell paragraphs are not part of the
    heading-path/synth-id scheme).

    Returns ``None`` when ``para_id`` matches none of the three schemes.
    The ``body_child_index`` is the index of the direct body child that contains
    or IS the matching paragraph — callers use it for before/after insertion.
    """
    body = root.find(_q(_W, "body"))
    if body is None:
        return None
    w_p = _q(_W, "p")
    w14_para_id = _q(_W14, "paraId")

    from ._vendored_content_tree import _build_synth_id_map  # noqa: PLC0415

    synth_map = _build_synth_id_map(body)

    global_p_idx = 0
    for child_idx, child in enumerate(list(body)):
        if child.tag == w_p:
            real_id = child.get(w14_para_id)
            synth_id = synth_map.get(id(child))
            legacy_id = f"p{global_p_idx}"
            if (
                real_id == para_id
                or (synth_id is not None and synth_id == para_id)
                or legacy_id == para_id
            ):
                return body, child, child_idx
            global_p_idx += 1
        else:
            # Walk into tables to find paragraphs inside cells. No synth id
            # applies here (see docstring) -- only native and legacy ids.
            for p in child.iter(w_p):
                real_id = p.get(w14_para_id)
                legacy_id = f"p{global_p_idx}"
                if real_id == para_id or legacy_id == para_id:
                    # Paragraph is inside a table; body_child_index points at
                    # the table so callers can insert relative to it.
                    return body, p, child_idx
                global_p_idx += 1
    return None


def _invalidate_sidecar_mtime(index_db_path: str | None) -> None:
    """Force the next _ensure_fresh() call to re-index this sidecar.

    Clears the stored ``source_mtime`` so the mtime comparison always reports
    stale.  No-op when ``index_db_path`` is None or does not exist yet.
    """
    if not index_db_path or not os.path.exists(index_db_path):
        return
    try:
        conn = sqlite3.connect(index_db_path)
        try:
            conn.execute(
                "UPDATE docx_index_meta SET value = NULL WHERE key = 'source_mtime'"
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass  # staleness invalidation is best-effort


# ---------------------------------------------------------------------------
# Caption paragraph builder (shared for Figure and Table)
# ---------------------------------------------------------------------------

def _count_seq_captions(root: ET.Element, kind: str) -> int:
    """Count existing SEQ captions of the given kind in the document.

    Counts ``<w:fldSimple>`` elements whose ``w:instr`` attribute contains
    ``SEQ Figure`` or ``SEQ Table``, plus complex-field ``<w:instrText>``
    elements with the same token.  Used to derive the next auto-increment number.
    """
    seq_token = f"SEQ {kind}"
    count = 0
    for fld in root.iter(_q(_W, "fldSimple")):
        instr = fld.get(_q(_W, "instr")) or ""
        if seq_token in instr:
            count += 1
    for instrText in root.iter(_q(_W, "instrText")):
        text = instrText.text or ""
        if seq_token in text:
            count += 1
    return count


def _next_ref_bookmark_name(root: ET.Element) -> str:
    """Return the next unused ``_Ref<digits>`` cross-reference bookmark name.

    Scans every ``<w:bookmarkStart>`` in the document for names matching
    ``_Ref<digits>`` (Word's own caption-bookmark convention) and returns one
    higher than the current maximum, seeded at 100000000 the way Word's own
    9-digit random-looking bookmark ids start.  Deterministic (not random) so
    repeated inserts against the same document are reproducible/testable.
    """
    max_seen = 100000000 - 1
    for bm in root.iter(_q(_W, "bookmarkStart")):
        name = bm.get(_q(_W, "name")) or ""
        m = _REF_BOOKMARK_RE.match(name)
        if m:
            max_seen = max(max_seen, int(m.group(1)))
    return f"{_REF_BOOKMARK_PREFIX}{max_seen + 1}"


def _build_caption_paragraph(
    kind: str,
    label_text: str,
    seq_cached: str = "1",
    ref_bookmark: str | None = None,
) -> ET.Element:
    """Build a ``<w:p>`` element for a Word caption using the Caption style.

    Produces::

        <w:p>
          <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
          <w:bookmarkStart w:id="0" w:name="_Ref123456789"/>
          <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
          <w:fldSimple w:instr="SEQ Figure \\* ARABIC">
            <w:r><w:t>1</w:t></w:r>
          </w:fldSimple>
          <w:bookmarkEnd w:id="0"/>
          <w:r><w:t xml:space="preserve">. label_text</w:t></w:r>
        </w:p>

    ``kind`` is ``"Figure"`` or ``"Table"``.  ``seq_cached`` is the cached
    rendered number (e.g. ``"1"``).  The fldSimple approach matches Word's own
    caption wizard (single instruction + cached result, no fldChar dance needed).

    1c59cb90 — when ``ref_bookmark`` is given, the ``"<kind> "`` prefix run and
    the SEQ field are wrapped in a ``w:bookmarkStart``/``w:bookmarkEnd`` pair
    named ``ref_bookmark``.  That bookmark's rendered content is exactly the
    "label and number" text (e.g. ``"Figure 3"``) — deliberately excluding the
    descriptive ``". label_text"`` suffix — so a ``REF <bookmark> \\h`` field
    inserted elsewhere (see :func:`insert_cross_reference`) resolves to just
    ``"Figure 3"`` and stays correct across reordering/renumbering on Word's
    next field refresh, instead of hand-typed prose text going stale.
    """
    if kind not in ("Figure", "Table"):
        raise ValueError(f"caption kind must be 'Figure' or 'Table', got {kind!r}")

    instr = _SEQ_FIGURE_INSTR if kind == "Figure" else _SEQ_TABLE_INSTR

    p = ET.Element(_q(_W, "p"))

    # Paragraph properties: Caption style.
    pPr = ET.SubElement(p, _q(_W, "pPr"))
    pStyle = ET.SubElement(pPr, _q(_W, "pStyle"))
    pStyle.set(_q(_W, "val"), _CAPTION_STYLE)

    if ref_bookmark:
        bm_start = ET.SubElement(p, _q(_W, "bookmarkStart"))
        bm_start.set(_q(_W, "id"), "0")
        bm_start.set(_q(_W, "name"), ref_bookmark)

    # Run: "<kind> " prefix.
    r_prefix = ET.SubElement(p, _q(_W, "r"))
    t_prefix = ET.SubElement(r_prefix, _q(_W, "t"))
    t_prefix.set(_q(_XML_NS, "space"), "preserve")
    t_prefix.text = f"{kind} "

    # SEQ field (simple field).
    fld = ET.SubElement(p, _q(_W, "fldSimple"))
    fld.set(_q(_W, "instr"), instr)
    r_fld = ET.SubElement(fld, _q(_W, "r"))
    t_fld = ET.SubElement(r_fld, _q(_W, "t"))
    t_fld.text = seq_cached

    if ref_bookmark:
        bm_end = ET.SubElement(p, _q(_W, "bookmarkEnd"))
        bm_end.set(_q(_W, "id"), "0")

    # Run: ". <label_text>".
    r_label = ET.SubElement(p, _q(_W, "r"))
    t_label = ET.SubElement(r_label, _q(_W, "t"))
    t_label.set(_q(_XML_NS, "space"), "preserve")
    t_label.text = f". {label_text}"

    return p


# ---------------------------------------------------------------------------
# Sidecar sync helpers for captions
# ---------------------------------------------------------------------------

def _ensure_caption_section_column(conn: sqlite3.Connection) -> None:
    """Add a ``section`` column to ``docx_figures`` if not already present."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(docx_figures)").fetchall()]
    if "section" not in cols:
        conn.execute("ALTER TABLE docx_figures ADD COLUMN section TEXT")


def _upsert_sidecar_caption(
    index_db_path: str,
    kind: str,
    para_id: str | None,
    seq_number: str,
    caption_text: str,
    section_heading: str | None,
    ref_bookmark: str | None = None,
) -> None:
    """Upsert a caption record into the sidecar SQLite index.

    For Figure captions: inserts a new row into ``docx_figures``.
    For Table captions: updates the ``caption`` column in ``docx_tables`` for
    the most-recent table row, or inserts a placeholder if none exists yet.

    ``ref_bookmark`` (1c59cb90) is the ``_Ref<digits>`` cross-reference
    bookmark name wrapping the caption's "<Kind> <N>" text, persisted so
    :func:`insert_cross_reference` can look it up by figure/table id without
    re-parsing the .docx XML.

    Non-fatal: exceptions are swallowed so the caller's main result is unaffected.
    """
    try:
        conn = _connect(index_db_path)
        try:
            _ensure_caption_section_column(conn)
            if kind == "Figure":
                row = conn.execute("SELECT MAX(idx) FROM docx_figures").fetchone()
                next_idx = (row[0] or 0) + 1
                conn.execute(
                    "INSERT INTO docx_figures "
                    "(idx, para_id, caption, seq_number, section, ref_bookmark) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (next_idx, para_id, caption_text, seq_number, section_heading, ref_bookmark),
                )
            else:
                # Table: update the most recent table's caption.
                row = conn.execute(
                    "SELECT id FROM docx_tables ORDER BY idx DESC LIMIT 1"
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE docx_tables SET caption = ?, ref_bookmark = ? WHERE id = ?",
                        (caption_text, ref_bookmark, row[0]),
                    )
                else:
                    row2 = conn.execute("SELECT MAX(idx) FROM docx_tables").fetchone()
                    next_idx = (row2[0] or 0) + 1
                    conn.execute(
                        "INSERT INTO docx_tables "
                        "(idx, row_count, col_count, rows_json, caption, ref_bookmark) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (next_idx, 0, 0, "[]", caption_text, ref_bookmark),
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass  # sidecar sync is best-effort


# ---------------------------------------------------------------------------
# Image-paragraph detection helper
# ---------------------------------------------------------------------------

# DrawingML and VML namespaces for image detection.
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_V = "urn:schemas-microsoft-com:vml"


def find_image_paragraph(
    docx_path: str,
    figure_index: int | None = None,
) -> dict[str, Any]:
    """Scan a .docx for paragraphs that contain an embedded image.

    A paragraph is considered an *image paragraph* when its ``<w:r>`` runs
    contain a ``<w:drawing>`` element (DrawingML inline/anchored images, the
    modern path) or a ``<w:pict>`` element (legacy VML path).  Both patterns
    are checked.

    This is the recommended helper for callers that need to supply a correct
    ``anchor_para_id`` to :func:`insert_caption` for ``kind="Figure"`` without
    manually guessing which paragraph holds the image.

    Args:
        docx_path:     Absolute path to the .docx file.
        figure_index:  1-based index selecting which image paragraph to return
                       when the document contains multiple images.  ``None``
                       (default) returns ALL image paragraphs as a list.

    Returns:
        ``{image_paragraphs: [{para_id, index, text}], count: int}``
        when ``figure_index`` is ``None``.

        ``{para_id, index, text, figure_index: int}``
        when ``figure_index`` is given and an image is found at that position.

        ``{"error": <message>}`` on any failure (file not found, not a valid
        .docx, or ``figure_index`` out of range).
    """
    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    if body is None:
        if figure_index is None:
            return {"image_paragraphs": [], "count": 0}
        return {"error": "document body is empty"}

    w_p = _q(_W, "p")
    w14_para_id = _q(_W14, "paraId")
    w_drawing = _q(_W, "drawing")
    w_pict = _q(_W, "pict")

    image_paras: list[dict[str, Any]] = []
    global_p_idx = 0

    for child in body:
        if child.tag == w_p:
            has_image = (
                child.find(f".//{w_drawing}") is not None
                or child.find(f".//{w_pict}") is not None
            )
            if has_image:
                real_id = child.get(w14_para_id)
                para_id = real_id if real_id else f"p{global_p_idx}"
                text = "".join(t.text or "" for t in child.iter(_q(_W, "t")))
                image_paras.append(
                    {"para_id": para_id, "index": global_p_idx, "text": text}
                )
            global_p_idx += 1
        else:
            # Walk into tables so images in table cells are also found.
            for p in child.iter(w_p):
                has_image = (
                    p.find(f".//{w_drawing}") is not None
                    or p.find(f".//{w_pict}") is not None
                )
                if has_image:
                    real_id = p.get(w14_para_id)
                    para_id = real_id if real_id else f"p{global_p_idx}"
                    text = "".join(t.text or "" for t in p.iter(_q(_W, "t")))
                    image_paras.append(
                        {"para_id": para_id, "index": global_p_idx, "text": text}
                    )
                global_p_idx += 1

    if figure_index is None:
        return {"image_paragraphs": image_paras, "count": len(image_paras)}

    # 1-based selection.
    if figure_index < 1 or figure_index > len(image_paras):
        return {
            "error": (
                f"figure_index {figure_index} is out of range: document has "
                f"{len(image_paras)} image paragraph(s)"
            )
        }
    entry = image_paras[figure_index - 1]
    return {
        "para_id": entry["para_id"],
        "index": entry["index"],
        "text": entry["text"],
        "figure_index": figure_index,
    }


# ---------------------------------------------------------------------------
# Public caption API: insert / edit / remove
# ---------------------------------------------------------------------------

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

    Writes a new ``<w:p>`` with ``w:pStyle="Caption"`` and a ``SEQ Figure``
    or ``SEQ Table`` field directly into ``word/document.xml`` inside the .docx
    ZIP, then re-packs the ZIP preserving all other members.

    The SEQ number is auto-incremented: it equals the count of existing SEQ
    captions of the same kind in the document plus one.  Word will recompute
    the final numbering on the next field refresh (F9).

    Args:
        docx_path:       Absolute path to the .docx file (mutated in place).
        anchor_para_id:  ``w14:paraId`` (or ``p{N}`` synthesised id) of the
                         paragraph/table next to which the caption is inserted.
        kind:            ``"Figure"`` or ``"Table"``.
        label_text:      Caption label text (e.g. ``"Loss curve for run 42"``).
                         Rendered text will be e.g. ``"Figure 1. Loss curve..."``.
        position:        ``"after"`` (default) or ``"before"``.  For
                         ``kind="Figure"`` only ``"after"`` is valid: a figure
                         caption must always follow its image, so ``"before"``
                         is rejected with an error.  Table captions may use
                         either ``"before"`` or ``"after"``.
        section_heading: Optional heading text for the section this caption
                         belongs to.  Stored in the sidecar ``section`` column.
        index_db_path:   If supplied, the sidecar SQLite index is invalidated
                         after the write so the next read auto-reindexes.

    Returns:
        ``{status, kind, seq_number, label_text, section_heading, ref_bookmark,
        docx_path}`` or ``{"error": <message>}`` on failure (file is NOT
        mutated on error).  ``ref_bookmark`` (1c59cb90) is the ``_Ref<digits>``
        bookmark name wrapping the caption's "<Kind> <N>" text — pass it as
        ``bookmark_name`` to :func:`insert_cross_reference` to insert a live
        "Figure N" prose reference elsewhere that survives reordering.
    """
    kind = str(kind).strip()
    if kind not in ("Figure", "Table"):
        return {"error": f"kind must be 'Figure' or 'Table', got {kind!r}"}
    if position not in ("before", "after"):
        return {"error": f"position must be 'before' or 'after', got {position!r}"}
    # A figure caption can NEVER precede its image — it is always placed after
    # the image paragraph.  Rejecting position="before" for kind="Figure" removes
    # this invalid state from the API surface entirely so callers cannot
    # accidentally produce captions above their images.
    if kind == "Figure" and position == "before":
        return {
            "error": (
                "position='before' is not valid for kind='Figure': a figure caption "
                "must always follow its image.  Pass the image paragraph's para_id as "
                "anchor_para_id and omit position (defaults to 'after')."
            )
        }
    if not label_text or not str(label_text).strip():
        return {"error": "label_text must be a non-empty string"}

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    result = _find_para_by_id(root, anchor_para_id)
    if result is None:
        return {"error": f"para_id {anchor_para_id!r} not found in {docx_path}"}

    body, _anchor_elem, child_idx = result

    seq_number = _count_seq_captions(root, kind) + 1
    ref_bookmark = _next_ref_bookmark_name(root)

    caption_p = _build_caption_paragraph(
        kind=kind,
        label_text=label_text.strip(),
        seq_cached=str(seq_number),
        ref_bookmark=ref_bookmark,
    )

    insert_at = child_idx if position == "before" else child_idx + 1
    body.insert(insert_at, caption_p)

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    if index_db_path and os.path.exists(index_db_path):
        _upsert_sidecar_caption(
            index_db_path=index_db_path,
            kind=kind,
            para_id=None,  # newly inserted para has no w14:paraId yet
            seq_number=str(seq_number),
            caption_text=f"{kind} {seq_number}. {label_text.strip()}",
            section_heading=section_heading,
            ref_bookmark=ref_bookmark,
        )

    return {
        "status": "inserted",
        "kind": kind,
        "seq_number": seq_number,
        "label_text": label_text.strip(),
        "section_heading": section_heading,
        "ref_bookmark": ref_bookmark,
        "docx_path": docx_path,
    }


def edit_caption(
    docx_path: str,
    caption_para_id: str,
    new_label_text: str,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """9d749639 — Edit the label text of an existing Word Caption paragraph.

    Locates the paragraph by ``caption_para_id``, verifies it uses the Caption
    style or contains a SEQ field, replaces the label text run (the last
    ``<w:r>`` that is a direct child of the paragraph and NOT inside a
    fldSimple) while preserving the SEQ field and paragraph style.

    The SEQ number is NOT changed — it is left as the existing cached value so
    Word's field-refresh cycle continues to work correctly.

    Args:
        docx_path:       Absolute path to the .docx file (mutated in place).
        caption_para_id: ``w14:paraId`` or ``p{N}`` of the Caption paragraph.
        new_label_text:  Replacement label text.
        index_db_path:   If supplied, sidecar is invalidated after the write.

    Returns:
        ``{status, caption_para_id, new_label_text, docx_path}``
        or ``{"error": <message>}`` on failure.
    """
    if not new_label_text or not str(new_label_text).strip():
        return {"error": "new_label_text must be a non-empty string"}

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    result = _find_para_by_id(root, caption_para_id)
    if result is None:
        return {"error": f"para_id {caption_para_id!r} not found in {docx_path}"}

    _body, caption_elem, _cidx = result

    # Validate: must be a Caption paragraph or contain a SEQ field.
    pPr = caption_elem.find(_q(_W, "pPr"))
    style_val: str | None = None
    if pPr is not None:
        pStyle = pPr.find(_q(_W, "pStyle"))
        if pStyle is not None:
            style_val = pStyle.get(_q(_W, "val"))

    has_seq = any(
        "SEQ" in (fld.get(_q(_W, "instr")) or "")
        for fld in caption_elem.iter(_q(_W, "fldSimple"))
    ) or any(
        "SEQ" in (it.text or "")
        for it in caption_elem.iter(_q(_W, "instrText"))
    )

    if style_val != _CAPTION_STYLE and not has_seq:
        return {
            "error": (
                f"paragraph {caption_para_id!r} is not a Caption paragraph "
                f"(style={style_val!r}, has_seq={has_seq})"
            )
        }

    # Find the last direct <w:r> child of the paragraph (not inside fldSimple).
    # This is the label text run produced by _build_caption_paragraph.
    label_run: ET.Element | None = None
    for child in caption_elem:
        if child.tag == _q(_W, "r"):
            label_run = child

    if label_run is None:
        return {
            "error": f"could not find label text run in paragraph {caption_para_id!r}"
        }

    t_el = label_run.find(_q(_W, "t"))
    if t_el is None:
        t_el = ET.SubElement(label_run, _q(_W, "t"))
    t_el.text = f". {new_label_text.strip()}"
    t_el.set(_q(_XML_NS, "space"), "preserve")

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "edited",
        "caption_para_id": caption_para_id,
        "new_label_text": new_label_text.strip(),
        "docx_path": docx_path,
    }


def remove_caption(
    docx_path: str,
    caption_para_id: str,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """9d749639 — Remove a Caption paragraph from a .docx file.

    Locates the paragraph by ``caption_para_id``, verifies it is a Caption
    paragraph (Caption style or SEQ field present), removes it from the body,
    and re-packs the ZIP.

    1c59cb90 — this also removes the caption's ``_Ref<digits>`` cross-reference
    bookmark (it lives inside the removed paragraph).  Any ``REF`` field
    elsewhere that pointed at it becomes a dangling reference — the same
    behavior Word itself has when a captioned item is deleted; existing
    cross-references must be re-pointed manually (or removed) afterward.

    Args:
        docx_path:       Absolute path to the .docx file (mutated in place).
        caption_para_id: ``w14:paraId`` or ``p{N}`` of the Caption paragraph.
        index_db_path:   If supplied, sidecar is invalidated after the write.

    Returns:
        ``{status, caption_para_id, docx_path}``
        or ``{"error": <message>}`` on failure.
    """
    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    result = _find_para_by_id(root, caption_para_id)
    if result is None:
        return {"error": f"para_id {caption_para_id!r} not found in {docx_path}"}

    body, caption_elem, _cidx = result

    pPr = caption_elem.find(_q(_W, "pPr"))
    style_val: str | None = None
    if pPr is not None:
        pStyle = pPr.find(_q(_W, "pStyle"))
        if pStyle is not None:
            style_val = pStyle.get(_q(_W, "val"))

    has_seq = any(
        "SEQ" in (fld.get(_q(_W, "instr")) or "")
        for fld in caption_elem.iter(_q(_W, "fldSimple"))
    ) or any(
        "SEQ" in (it.text or "")
        for it in caption_elem.iter(_q(_W, "instrText"))
    )

    if style_val != _CAPTION_STYLE and not has_seq:
        return {
            "error": (
                f"paragraph {caption_para_id!r} is not a Caption paragraph "
                f"(style={style_val!r}, has_seq={has_seq})"
            )
        }

    body.remove(caption_elem)

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "removed",
        "caption_para_id": caption_para_id,
        "docx_path": docx_path,
    }


# ---------------------------------------------------------------------------
# retrofit_plaintext_captions (82b0b1a6) -- bulk-migrate hardcoded caption
# text to real SEQ fields
#
# insert_caption (9d749639, above) only ever creates NEW captions with real,
# auto-numbering SEQ fields. Nothing converted EXISTING plain-text captions
# -- a paragraph whose visible text literally reads "Figure 41" or "Table 3"
# with no SEQ field backing it at all (carried over from a document authored
# before insert_caption existed, or pasted in from another source) -- into
# that mechanism. renumber_sequences (595ccea1) walks <w:fldSimple> SEQ
# fields ONLY (see its seq_fields scan above); a plain-text caption has no
# such field, so it is invisible to a renumbering pass and silently survives
# untouched, duplicate number and all. That is exactly the failure mode that
# let a real 4-way "Figure 42" duplicate (plus three more 2x duplicates)
# survive a full renumber_sequences pass on a real document: nothing could
# migrate the old hardcoded numbers onto a mechanically-unique system in the
# first place.
# ---------------------------------------------------------------------------

_PLAINTEXT_FIGURE_RE = re.compile(
    r"^\s*Figure\s+(\d+)\b\s*[.:]?\s*(.*)$", re.IGNORECASE | re.DOTALL
)
_PLAINTEXT_TABLE_RE = re.compile(
    r"^\s*Table\s+(\d+)\b\s*[.:]?\s*(.*)$", re.IGNORECASE | re.DOTALL
)


def retrofit_plaintext_captions(
    docx_path: str, index_db_path: str | None = None
) -> dict[str, Any]:
    """82b0b1a6 -- bulk-convert existing plain-text Figure/Table captions into
    real Word SEQ fields, then re-derive correct numbering across the WHOLE
    document in the same call.

    Scans every paragraph in the document (including ones nested in table
    cells -- same walk order as :func:`renumber_sequences`) for one that:

      1. Has NO SEQ field already -- checked the same way :func:`edit_caption`
         / :func:`remove_caption` validate an EXISTING caption (a ``SEQ``
         ``<w:fldSimple w:instr=...>`` or complex-field ``<w:instrText>``
         anywhere in the paragraph); and
      2. Has visible text that starts with ``"Figure <N>"`` or ``"Table
         <N>"`` (case-insensitive, optional trailing ``.``/``:`` before the
         descriptive label) -- the plain-text caption pattern this primitive
         exists to migrate.

    Note this is a text-pattern match, not a semantic one: a normal body
    paragraph that happens to OPEN with "Figure 3 ..." (referring to a figure
    in prose, not captioning one) would also match. In practice this is rare
    -- real captions are short, standalone paragraphs -- but callers dealing
    with an unusual document should spot-check ``conversions`` before
    trusting the result.

    Each match is rebuilt via :func:`_build_caption_paragraph` -- the EXACT
    same ``SEQ Figure \\* ARABIC`` / ``SEQ Table \\* ARABIC`` ``fldSimple``
    shape :func:`insert_caption` already constructs, with its own
    ``_Ref<digits>`` cross-reference bookmark (:func:`_next_ref_bookmark_name`)
    -- while preserving the paragraph's own identity (``w14:paraId`` and any
    other attributes are untouched; only its children are replaced) and its
    existing descriptive label text (everything after the old ``"<Kind>
    <N>"`` prefix). The OLD hardcoded number is kept as the field's cached
    value for now; it does not need to already be correct, because:

    :func:`renumber_sequences` is called automatically as the final step
    (same "call it as a first-class primitive rather than duplicate the
    logic" pattern :func:`move_section` / :func:`copy_section` already use)
    -- it re-reads the just-saved document from disk and re-derives every
    SEQ number (Figure and Table counted independently) from actual body
    order, INCLUDING the fields this call just created. That is what
    actually closes the duplicate-number gap: once a plain-text caption is a
    real SEQ field, a renumbering pass can finally see it and fix it, instead
    of walking straight past it.

    Fails safe on any single candidate rather than risk corrupting an
    unusual paragraph shape: a candidate paragraph that already carries ANY
    ``<w:bookmarkStart>`` (e.g. a hand-made cross-reference bookmark
    pointing at this exact paragraph, predating this migration) is SKIPPED
    -- not converted -- since rebuilding its children would silently destroy
    that bookmark. Skipped paragraphs are reported in ``skipped`` so a
    caller can migrate them by hand.

    Args:
        docx_path:     Absolute path to the .docx file (mutated in place --
                       only if at least one plain-text caption is found).
        index_db_path: If supplied, the sidecar is invalidated after the
                       write (and threaded into the :func:`renumber_sequences`
                       call so its own sidecar invalidation happens too).

    Returns:
        ``{status, candidates_found, conversions, skipped, renumber_sequences,
        docx_path}``. ``status`` is ``"unchanged"`` when no plain-text
        caption was found (no write performed) or ``"converted"`` otherwise.
        ``conversions`` lists ``{para_id, kind, old_cached_number, label_text,
        ref_bookmark}`` for every paragraph actually migrated, in document
        order. ``skipped`` lists ``{para_id, kind, reason}`` for candidates
        that matched the text pattern but were left untouched for safety.
        ``{"error": <message>}`` on failure (file NOT mutated on error).
    """
    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    if body is None:
        return {"error": "document has no body element"}

    w_p = _q(_W, "p")
    w14_para_id = _q(_W14, "paraId")
    w_bookmarkStart = _q(_W, "bookmarkStart")

    conversions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for global_p_idx, p in enumerate(body.iter(w_p)):
        has_seq = any(
            "SEQ" in (fld.get(_q(_W, "instr")) or "")
            for fld in p.iter(_q(_W, "fldSimple"))
        ) or any("SEQ" in (it.text or "") for it in p.iter(_q(_W, "instrText")))
        if has_seq:
            continue

        text = "".join(t.text or "" for t in p.iter(_q(_W, "t")))

        kind = "Figure"
        m = _PLAINTEXT_FIGURE_RE.match(text)
        if m is None:
            kind = "Table"
            m = _PLAINTEXT_TABLE_RE.match(text)
        if m is None:
            continue

        real_id = p.get(w14_para_id)
        para_id = real_id if real_id else f"p{global_p_idx}"
        old_number = m.group(1)
        label_text = m.group(2).strip()

        if next(iter(p.iter(w_bookmarkStart)), None) is not None:
            skipped.append({
                "para_id": para_id,
                "kind": kind,
                "reason": "paragraph already has a bookmark; conversion would destroy it",
            })
            continue

        ref_bookmark = _next_ref_bookmark_name(root)
        new_p = _build_caption_paragraph(
            kind=kind,
            label_text=label_text,
            seq_cached=old_number,
            ref_bookmark=ref_bookmark,
        )
        if not label_text:
            # _build_caption_paragraph always appends a trailing
            # "<kind> <N>. <label>" run -- drop it when there was no
            # descriptive text at all rather than leave a dangling bare
            # "Figure 41. " period behind.
            trailing = list(new_p)[-1]
            if trailing.tag == _q(_W, "r"):
                new_p.remove(trailing)

        for child in list(p):
            p.remove(child)
        for child in list(new_p):
            p.append(child)

        conversions.append({
            "para_id": para_id,
            "kind": kind,
            "old_cached_number": old_number,
            "label_text": label_text,
            "ref_bookmark": ref_bookmark,
        })

    if not conversions:
        return {
            "status": "unchanged",
            "candidates_found": 0,
            "conversions": [],
            "skipped": skipped,
            "renumber_sequences": None,
            "docx_path": docx_path,
        }

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    renumber_result = renumber_sequences(docx_path, index_db_path=index_db_path)

    return {
        "status": "converted",
        "candidates_found": len(conversions),
        "conversions": conversions,
        "skipped": skipped,
        "renumber_sequences": renumber_result,
        "docx_path": docx_path,
    }


# ---------------------------------------------------------------------------
# Public cross-reference API: Word REF-field mechanism (1c59cb90)
#
# REFILED (original 7b5bfb00) — captions previously only got Word's SEQ-field
# auto-numbering (insert_caption above). Prose that refers to a figure/table
# by number ("as shown in Figure 3") had to be hand-typed, so it silently went
# stale the moment captions were reordered or one was inserted/removed earlier
# in the document. This adds the other half of Word's numbering system: a
# REF field that targets the caption's own cross-reference bookmark, so the
# rendered text tracks the SAME field-refresh cycle (F9) that keeps the SEQ
# numbers themselves correct.
# ---------------------------------------------------------------------------

def _caption_kind_and_seq(caption_elem: ET.Element) -> tuple[str, str] | None:
    """Return ``(kind, cached_seq_number)`` for a Caption paragraph, or ``None``.

    ``kind`` is ``"Figure"`` or ``"Table"``, detected from the paragraph's
    ``SEQ Figure`` / ``SEQ Table`` ``fldSimple``.  ``cached_seq_number`` is the
    field's cached rendered text (e.g. ``"3"``).
    """
    for fld in caption_elem.iter(_q(_W, "fldSimple")):
        instr = fld.get(_q(_W, "instr")) or ""
        if _SEQ_FIGURE_RE.search(instr):
            kind = "Figure"
        elif _SEQ_TABLE_RE.search(instr):
            kind = "Table"
        else:
            continue
        cached = "".join(t.text or "" for t in fld.iter(_q(_W, "t")))
        return kind, cached
    return None


def _find_caption_ref_bookmark(caption_elem: ET.Element) -> str | None:
    """Return the ``_Ref<digits>`` bookmark name wrapping a caption, if any."""
    for bm in caption_elem.iter(_q(_W, "bookmarkStart")):
        name = bm.get(_q(_W, "name")) or ""
        if _REF_BOOKMARK_RE.match(name):
            return name
    return None


def _wrap_caption_in_ref_bookmark(caption_elem: ET.Element, ref_name: str) -> None:
    """Retrofit a ``_Ref`` bookmark onto a caption paragraph built pre-1c59cb90.

    Wraps the ``"<kind> "`` prefix run and the SEQ ``fldSimple`` element — the
    same span :func:`_build_caption_paragraph` brackets natively when given a
    ``ref_bookmark`` — with a fresh ``bookmarkStart``/``bookmarkEnd`` pair,
    mutating ``caption_elem`` in place.  No-op (fails safe) if the expected
    prefix-run/SEQ-field shape isn't found rather than risk corrupting the XML.
    """
    children = list(caption_elem)
    w_r = _q(_W, "r")
    w_fldSimple = _q(_W, "fldSimple")

    prefix_idx: int | None = None
    fld_idx: int | None = None
    for i, child in enumerate(children):
        if child.tag == w_r and prefix_idx is None:
            prefix_idx = i
        if child.tag == w_fldSimple:
            instr = child.get(_q(_W, "instr")) or ""
            if _SEQ_FIGURE_RE.search(instr) or _SEQ_TABLE_RE.search(instr):
                fld_idx = i
                break

    if prefix_idx is None or fld_idx is None or fld_idx < prefix_idx:
        return

    bm_start = ET.Element(_q(_W, "bookmarkStart"))
    bm_start.set(_q(_W, "id"), "0")
    bm_start.set(_q(_W, "name"), ref_name)
    caption_elem.insert(prefix_idx, bm_start)

    bm_end = ET.Element(_q(_W, "bookmarkEnd"))
    bm_end.set(_q(_W, "id"), "0")
    # fld_idx shifts by +1 because bm_start was just inserted ahead of it;
    # +1 more to land the close tag immediately after the fldSimple element.
    caption_elem.insert(fld_idx + 2, bm_end)


def _find_caption_by_ref_bookmark(
    root: ET.Element, bookmark_name: str
) -> tuple[ET.Element, tuple[str, str]] | None:
    """Find the caption paragraph owning ``bookmark_name`` and its ``(kind, seq)``.

    Returns ``None`` when no paragraph in the document has a
    ``w:bookmarkStart`` with that name wrapping a recognisable SEQ field.
    """
    body = root.find(_q(_W, "body"))
    if body is None:
        return None
    w_bookmarkStart = _q(_W, "bookmarkStart")
    w_name = _q(_W, "name")
    w_p = _q(_W, "p")
    for p in body.iter(w_p):
        for bm in p.iter(w_bookmarkStart):
            if bm.get(w_name) == bookmark_name:
                kind_seq = _caption_kind_and_seq(p)
                if kind_seq is not None:
                    return p, kind_seq
    return None


def insert_cross_reference(
    docx_path: str,
    anchor_para_id: str,
    target_caption_para_id: str | None = None,
    bookmark_name: str | None = None,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """1c59cb90 — Insert a live Word REF-field cross-reference into a .docx file.

    Appends a REF complex field (``fldChar begin`` / ``instrText`` / ``fldChar
    separate`` / cached display text / ``fldChar end``, the same 5-run shape
    :func:`insert_citation` uses) to the paragraph identified by
    ``anchor_para_id``.  The field's instruction targets the ``_Ref<digits>``
    bookmark wrapping a figure or table caption's ``"<Kind> <N>"`` text (see
    :func:`insert_caption` / :func:`_build_caption_paragraph`), with cached
    display text like ``"Figure 3"``.

    This is the difference between SEQ-field numbering (the caption's own
    number, handled by :func:`insert_caption`) and REF-field cross-referencing
    (prose *elsewhere* that quotes that number): a hand-typed "Figure 3" goes
    stale the instant captions are reordered or one is inserted earlier in the
    document; a REF field recomputes on Word's next field refresh (F9, or
    automatically on print / Save As PDF) because it reads the SAME bookmarked
    SEQ field the caption itself renders.

    Callers identify the target caption EITHER way (exactly one required):
      - ``target_caption_para_id``: the caption paragraph's ``w14:paraId`` (or
        synthesised ``p{N}``).  If that caption doesn't yet carry a ``_Ref``
        bookmark (it predates 1c59cb90, or was built by an older
        ``insert_caption`` call), one is created now as part of this same
        write (retrofit — still a single atomic re-pack of
        ``word/document.xml``).
      - ``bookmark_name``: an existing ``_Ref<digits>`` bookmark name, e.g.
        the ``ref_bookmark`` field returned by a prior ``insert_caption`` call.

    The field is appended at the end of the anchor paragraph's existing
    content, with a separating space inserted first if the paragraph's
    trailing text doesn't already end in whitespace — so it reads naturally
    as trailing prose (e.g. ``"...as shown in Figure 3"``).

    Args:
        docx_path:              Absolute path to the .docx file (mutated in place).
        anchor_para_id:         ``w14:paraId`` (or ``p{N}``) of the paragraph the
                                 cross-reference field is appended into.
        target_caption_para_id: ``w14:paraId`` (or ``p{N}``) of the Figure/Table
                                 Caption paragraph being referenced.
        bookmark_name:          Alternative to ``target_caption_para_id`` — an
                                 existing ``_Ref<digits>`` bookmark name.
        index_db_path:          If supplied, sidecar is invalidated after the write.

    Returns:
        ``{status, anchor_para_id, bookmark_name, kind, seq_number,
        display_text, docx_path}`` or ``{"error": <message>}`` on failure
        (file is NOT mutated on error).
    """
    if bool(target_caption_para_id) == bool(bookmark_name):
        return {
            "error": (
                "exactly one of target_caption_para_id or bookmark_name must be given"
            )
        }

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    anchor_result = _find_para_by_id(root, anchor_para_id)
    if anchor_result is None:
        return {"error": f"para_id {anchor_para_id!r} not found in {docx_path}"}
    _anchor_body, anchor_elem, _anchor_idx = anchor_result

    if target_caption_para_id is not None:
        target_result = _find_para_by_id(root, target_caption_para_id)
        if target_result is None:
            return {
                "error": f"para_id {target_caption_para_id!r} not found in {docx_path}"
            }
        _target_body, caption_elem, _target_idx = target_result

        kind_seq = _caption_kind_and_seq(caption_elem)
        if kind_seq is None:
            return {
                "error": (
                    f"paragraph {target_caption_para_id!r} is not a Figure/Table "
                    "Caption paragraph (no SEQ field found)"
                )
            }
        kind, seq_cached = kind_seq

        ref_name = _find_caption_ref_bookmark(caption_elem)
        if ref_name is None:
            ref_name = _next_ref_bookmark_name(root)
            _wrap_caption_in_ref_bookmark(caption_elem, ref_name)
    else:
        found = _find_caption_by_ref_bookmark(root, bookmark_name)
        if found is None:
            return {
                "error": (
                    f"bookmark {bookmark_name!r} not found (or is not a caption "
                    f"cross-reference bookmark) in {docx_path}"
                )
            }
        _caption_elem, kind_seq = found
        kind, seq_cached = kind_seq
        ref_name = bookmark_name

    display_text = f"{kind} {seq_cached}"

    # Separating space so the field reads naturally as trailing prose (only
    # when the paragraph already has text that doesn't end in whitespace).
    existing_t_texts = [t.text or "" for t in anchor_elem.iter(_q(_W, "t"))]
    trailing_text = existing_t_texts[-1] if existing_t_texts else ""
    if trailing_text and not trailing_text[-1].isspace():
        r_space = ET.SubElement(anchor_elem, _q(_W, "r"))
        t_space = ET.SubElement(r_space, _q(_W, "t"))
        t_space.set(_q(_XML_NS, "space"), "preserve")
        t_space.text = " "

    for r in _build_complex_field_runs(f"REF {ref_name} \\h", display_text):
        anchor_elem.append(r)

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "inserted",
        "anchor_para_id": anchor_para_id,
        "bookmark_name": ref_name,
        "kind": kind,
        "seq_number": seq_cached,
        "display_text": display_text,
        "docx_path": docx_path,
    }


# ---------------------------------------------------------------------------
# Citation complex-field builder
# ---------------------------------------------------------------------------

def _build_complex_field_runs(instruction: str, display_text: str) -> list[ET.Element]:
    """Build the five ``<w:r>`` elements for a Word complex field.

    Word complex fields consist of:
      1. ``<w:r><w:fldChar w:fldCharType="begin"/></w:r>``
      2. ``<w:r><w:instrText xml:space="preserve"> INSTRUCTION </w:instrText></w:r>``
      3. ``<w:r><w:fldChar w:fldCharType="separate"/></w:r>``
      4. ``<w:r><w:t>display_text</w:t></w:r>``  (cached result shown to reader)
      5. ``<w:r><w:fldChar w:fldCharType="end"/></w:r>``

    Returns a list of 5 ``<w:r>`` elements to append to a ``<w:p>``.
    """
    elements: list[ET.Element] = []

    # 1. fldChar begin
    r1 = ET.Element(_q(_W, "r"))
    fc1 = ET.SubElement(r1, _q(_W, "fldChar"))
    fc1.set(_q(_W, "fldCharType"), "begin")
    elements.append(r1)

    # 2. instrText
    r2 = ET.Element(_q(_W, "r"))
    it = ET.SubElement(r2, _q(_W, "instrText"))
    it.set(_q(_XML_NS, "space"), "preserve")
    it.text = f" {instruction} "
    elements.append(r2)

    # 3. fldChar separate
    r3 = ET.Element(_q(_W, "r"))
    fc3 = ET.SubElement(r3, _q(_W, "fldChar"))
    fc3.set(_q(_W, "fldCharType"), "separate")
    elements.append(r3)

    # 4. Cached display text
    r4 = ET.Element(_q(_W, "r"))
    t4 = ET.SubElement(r4, _q(_W, "t"))
    t4.set(_q(_XML_NS, "space"), "preserve")
    t4.text = display_text
    elements.append(r4)

    # 5. fldChar end
    r5 = ET.Element(_q(_W, "r"))
    fc5 = ET.SubElement(r5, _q(_W, "fldChar"))
    fc5.set(_q(_W, "fldCharType"), "end")
    elements.append(r5)

    return elements


def _build_csl_citation_instruction(
    citation_keys: list[str],
    formatted_text: str,
    source: str = "zotero",
) -> str:
    """Build an ``ADDIN ZOTERO_ITEM CSL_CITATION {...}`` field instruction.

    Produces the minimal CSL_CITATION JSON payload that Zotero/Mendeley
    recognise.  Each entry in ``citation_keys`` becomes one ``citationItems``
    element.  The ``formatted_text`` is stored as the formattedCitation
    property and as the field's cached display result.

    Args:
        citation_keys:  List of stable citation identifiers (DOI, URI, etc.).
        formatted_text: Rendered in-text marker (e.g. ``"(Smith et al., 2023)"``).
        source:         ``"zotero"`` (default) or ``"csl"``.

    Returns:
        The complete field instruction string (without leading/trailing spaces
        — the caller adds those when constructing the instrText element).
    """
    items = [
        {
            "id": key,
            "uris": [],
            "itemData": {"id": key, "type": "article"},
        }
        for key in citation_keys
    ]
    payload: dict[str, Any] = {
        "citationID": f"cit_{abs(hash(tuple(citation_keys))) % (10 ** 9)}",
        "properties": {"formattedCitation": formatted_text},
        "citationItems": items,
        "schema": (
            "https://github.com/citation-style-language/schema"
            "/raw/master/csl-citation.json"
        ),
    }
    json_str = json.dumps(payload, separators=(",", ":"))
    if source == "zotero":
        return f"ADDIN ZOTERO_ITEM CSL_CITATION {json_str}"
    return f"ADDIN CSL_CITATION {json_str}"


def _extract_keys_from_instruction(instruction: str) -> list[str]:
    """Best-effort extraction of citation keys from a CSL_CITATION instruction.

    Mirrors _citation_keys_from_csl in packages/docparse/docparse/docs_intel
    but inlined here to avoid a cross-package dependency.
    """
    start = instruction.find("{")
    if start == -1:
        return []
    depth = 0
    in_str = False
    escape = False
    end = -1
    for i in range(start, len(instruction)):
        ch = instruction[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return []
    try:
        data = json.loads(instruction[start:end])
    except (ValueError, TypeError):
        return []
    items = data.get("citationItems") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    keys: list[str] = []
    for item in items:
        if isinstance(item, dict):
            kid = item.get("id")
            if kid:
                keys.append(str(kid))
    return keys


def _scan_citation_field(
    para_elem: ET.Element,
) -> tuple[int, int, str, str] | None:
    """Scan a paragraph for the first CSL_CITATION complex field.

    Returns ``(begin_idx, end_idx, instruction, display_text)`` where
    ``begin_idx`` / ``end_idx`` are indices into ``list(para_elem)`` for the
    first (fldCharType=begin) and last (fldCharType=end) run of the field.
    Returns ``None`` when no CSL_CITATION field is found.
    """
    children = list(para_elem)
    w_r = _q(_W, "r")
    w_fldChar = _q(_W, "fldChar")
    w_instrText = _q(_W, "instrText")
    w_fldCharType = _q(_W, "fldCharType")
    w_t = _q(_W, "t")

    i = 0
    while i < len(children):
        el = children[i]
        if el.tag == w_r:
            fc = el.find(w_fldChar)
            if fc is not None and fc.get(w_fldCharType) == "begin":
                j = i + 1
                instr_parts: list[str] = []
                display_parts: list[str] = []
                past_sep = False
                while j < len(children):
                    el2 = children[j]
                    if el2.tag == w_r:
                        fc2 = el2.find(w_fldChar)
                        if fc2 is not None:
                            ftype = fc2.get(w_fldCharType)
                            if ftype == "separate":
                                past_sep = True
                            elif ftype == "end":
                                instr = "".join(instr_parts).strip()
                                if "CSL_CITATION" in instr:
                                    return (
                                        i,
                                        j,
                                        instr,
                                        "".join(display_parts),
                                    )
                                break
                        it = el2.find(w_instrText)
                        if it is not None and not past_sep:
                            instr_parts.append(it.text or "")
                        t_el = el2.find(w_t)
                        if t_el is not None and past_sep:
                            display_parts.append(t_el.text or "")
                    j += 1
        i += 1
    return None


# ---------------------------------------------------------------------------
# Public citation API: insert / edit / remove
# ---------------------------------------------------------------------------

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
    ``ADDIN ZOTERO_ITEM CSL_CITATION {...}`` (Zotero) or
    ``ADDIN CSL_CITATION {...}`` (generic CSL), making it recognisable by the
    extraction side (``CSL_CITATION`` token check in docparse.docs_intel).

    Args:
        docx_path:       Absolute path to the .docx file (mutated in place).
        anchor_para_id:  ``w14:paraId`` or ``p{N}`` of the paragraph to cite in.
        citation_keys:   One or more stable citation identifiers (DOI, URI, etc.).
        formatted_text:  Rendered in-text marker (e.g. ``"(Smith et al., 2023)"``).
        source:          ``"zotero"`` (default) or ``"csl"``.
        index_db_path:   If supplied, sidecar is invalidated after the write.

    Returns:
        ``{status, anchor_para_id, citation_keys, formatted_text, source,
           docx_path}``
        or ``{"error": <message>}`` on failure.
    """
    if not citation_keys or not any(str(k).strip() for k in citation_keys):
        return {"error": "citation_keys must be a non-empty list of strings"}
    if not formatted_text or not str(formatted_text).strip():
        return {"error": "formatted_text must be a non-empty string"}
    if source not in ("zotero", "csl"):
        return {"error": f"source must be 'zotero' or 'csl', got {source!r}"}

    clean_keys = [str(k).strip() for k in citation_keys if str(k).strip()]
    if not clean_keys:
        return {"error": "citation_keys must contain at least one non-empty string"}

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    result = _find_para_by_id(root, anchor_para_id)
    if result is None:
        return {"error": f"para_id {anchor_para_id!r} not found in {docx_path}"}

    _body, para_elem, _cidx = result

    instruction = _build_csl_citation_instruction(
        citation_keys=clean_keys,
        formatted_text=formatted_text.strip(),
        source=source,
    )
    field_runs = _build_complex_field_runs(instruction, formatted_text.strip())
    for run in field_runs:
        para_elem.append(run)

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "inserted",
        "anchor_para_id": anchor_para_id,
        "citation_keys": clean_keys,
        "formatted_text": formatted_text.strip(),
        "source": source,
        "docx_path": docx_path,
    }


def edit_citation(
    docx_path: str,
    anchor_para_id: str,
    new_citation_keys: list[str] | None = None,
    new_formatted_text: str | None = None,
    source: str = "zotero",
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """9d749639 — Replace an existing CSL_CITATION field with updated keys/text.

    Locates the first complex field in the paragraph whose ``instrText`` contains
    ``CSL_CITATION``, removes the old field runs (begin through end), and inserts
    a new complex field with the updated keys / formatted text in their place.

    At least one of ``new_citation_keys`` or ``new_formatted_text`` must be
    supplied.  When only one is given the other is inferred from the existing field.

    Args:
        docx_path:          Absolute path to the .docx file (mutated in place).
        anchor_para_id:     ``w14:paraId`` or ``p{N}`` of the paragraph to edit.
        new_citation_keys:  Replacement citation keys (``None`` = keep existing).
        new_formatted_text: Replacement display text (``None`` = keep existing).
        source:             ``"zotero"`` or ``"csl"``.
        index_db_path:      If supplied, sidecar is invalidated after the write.

    Returns:
        ``{status, anchor_para_id, citation_keys, formatted_text, source,
           docx_path}``
        or ``{"error": <message>}`` on failure.
    """
    if new_citation_keys is None and new_formatted_text is None:
        return {"error": "supply at least one of new_citation_keys or new_formatted_text"}

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    result = _find_para_by_id(root, anchor_para_id)
    if result is None:
        return {"error": f"para_id {anchor_para_id!r} not found in {docx_path}"}

    _body, para_elem, _cidx = result

    scan = _scan_citation_field(para_elem)
    if scan is None:
        return {
            "error": f"no CSL_CITATION field found in paragraph {anchor_para_id!r}"
        }

    begin_idx, end_idx, old_instr, old_display = scan

    if new_citation_keys is None:
        new_citation_keys = _extract_keys_from_instruction(old_instr)
    if new_formatted_text is None:
        new_formatted_text = old_display

    clean_keys = [str(k).strip() for k in new_citation_keys if str(k).strip()]
    if not clean_keys:
        return {"error": "new_citation_keys must contain at least one non-empty string"}
    clean_text = str(new_formatted_text).strip()

    # Remove old field runs (end to begin to keep indices stable).
    children = list(para_elem)
    for idx in range(end_idx, begin_idx - 1, -1):
        para_elem.remove(children[idx])

    instruction = _build_csl_citation_instruction(
        citation_keys=clean_keys,
        formatted_text=clean_text,
        source=source,
    )
    new_runs = _build_complex_field_runs(instruction, clean_text)
    for offset, run in enumerate(new_runs):
        para_elem.insert(begin_idx + offset, run)

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "edited",
        "anchor_para_id": anchor_para_id,
        "citation_keys": clean_keys,
        "formatted_text": clean_text,
        "source": source,
        "docx_path": docx_path,
    }


def remove_citation(
    docx_path: str,
    anchor_para_id: str,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """9d749639 — Remove the first CSL_CITATION complex field from a paragraph.

    Locates the field by scanning for a complex field (fldChar begin...end)
    whose instrText contains ``CSL_CITATION``, removes all its constituent
    runs, and re-packs the ZIP.

    Args:
        docx_path:       Absolute path to the .docx file (mutated in place).
        anchor_para_id:  ``w14:paraId`` or ``p{N}`` of the paragraph to edit.
        index_db_path:   If supplied, sidecar is invalidated after the write.

    Returns:
        ``{status, anchor_para_id, docx_path}``
        or ``{"error": <message>}`` on failure.
    """
    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    result = _find_para_by_id(root, anchor_para_id)
    if result is None:
        return {"error": f"para_id {anchor_para_id!r} not found in {docx_path}"}

    _body, para_elem, _cidx = result

    scan = _scan_citation_field(para_elem)
    if scan is None:
        return {
            "error": f"no CSL_CITATION field found in paragraph {anchor_para_id!r}"
        }

    begin_idx, end_idx, _instr, _display = scan

    children = list(para_elem)
    for idx in range(end_idx, begin_idx - 1, -1):
        para_elem.remove(children[idx])

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "removed",
        "anchor_para_id": anchor_para_id,
        "docx_path": docx_path,
    }


# ---------------------------------------------------------------------------
# a80af3a0 — OMML/equation support
#
# EXTRACTION (stdlib ET, no lxml):
#   parse_docx_equations_local() — reads every <m:oMath> from word/document.xml
#   via zipfile + ET.  Two patterns are detected:
#     1. Standalone-paragraph: an oMath that lives alone in a <w:p> with no
#        meaningful text siblings (only oMath content in that paragraph).
#     2. Table-cell-with-numbering: a <w:tbl> row whose first cell contains an
#        oMath and whose second cell contains a parenthesised number like "(1)".
#        The number is extracted and associated as the equation's "number" field.
#
# WRITE (stdlib ET):
#   insert_equation_local() — inserts a new standalone-paragraph equation
#     (before / after / append-inline) into a .docx, accepting raw OMML XML
#     or a LaTeX string (converted via latex2mathml + stdlib ET mapper below).
#   edit_equation_local()   — replaces the <m:oMath> content in an existing
#     equation paragraph.
#   remove_equation_local() — removes the equation paragraph (or the inline
#     oMath, for append-position).
#
# LOCAL SIDECAR:
#   index_docx_equations() — populates a docx_equations table in the sidecar
#     SQLite DB (same DB as docx_figures / docx_tables).
#   get_local_equations()   — reads all locally-stored equations back out.
#
# DEPENDENCY DECISION (a80af3a0):
#   latex2mathml is added as an explicit dependency in pyproject.toml.  It is
#   pure Python with no C extension — safe for uvx isolated installs.  The
#   MathML->OMML conversion (_stdlib_mathml_to_omml below) is a stdlib ET port
#   of the lxml-based _mathml_to_omml in meridian/doc_store.py; it produces
#   identical OMML output using ET.SubElement with Clark-notation tags.  The
#   latex_to_omml_local() function degrades to None when latex2mathml is absent
#   or the LaTeX is blank/invalid — never raises.
#   The WRITE side therefore accepts BOTH raw OMML XML (caller-supplied, e.g.
#   from doc_store.latex_to_omml on the core server) AND a LaTeX string
#   converted locally.  This is the cleanest path: no lxml in the extension,
#   full LaTeX support in the extension, same contract as the core server.
# ---------------------------------------------------------------------------

# OMML namespace (math markup language used by Word for inline equations).
# The "m" prefix is already registered at module load time (see the ET.register_namespace
# block near the top of this file) — no second call needed here.
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"

# MathML namespace (intermediate representation from latex2mathml).
_MATHML_NS = "http://www.w3.org/1998/Math/MathML"

# Tags that are pure sequencing in MathML — flatten into their children.
_MATHML_ROW_TAGS = frozenset({"math", "mrow", "mstyle", "mpadded", "mphantom"})
# Tags whose text content maps to a literal OMML run.
_MATHML_TEXT_TAGS = frozenset({"mi", "mn", "mo", "mtext"})

# Parenthesised-number pattern for detecting equation number cells.
# Matches "(1)", "(2a)", "(A.1)", "(eq3)", etc.
_EQ_NUMBER_RE = re.compile(r"^\s*\(\s*[\w.]+\s*\)\s*$")


def _qm(tag: str) -> str:
    """Clark-notation for an OMML <m:tag>."""
    return f"{{{_M}}}{tag}"


def _omml_flatten_text_local(omml_raw: str | None) -> str:
    """Concatenate every <m:t> text run inside a raw OMML string (best-effort).

    Stdlib ET port of doc_store._omml_flatten_text — used as the dedup key for
    equations that only carry OMML (no LaTeX source).  Returns "" on blank /
    malformed input, never raises.
    """
    if not omml_raw:
        return ""
    try:
        el = ET.fromstring(omml_raw)
    except ET.ParseError:
        return ""
    return "".join(t.text or "" for t in el.iter(_qm("t")))


def _stdlib_append_mathml(node: ET.Element, parent: ET.Element) -> None:
    """Recursively convert a MathML element subtree into OMML children.

    Stdlib ET port of doc_store._append_mathml (the lxml-based version).
    Handles the common OMML subset; unrecognized constructs (matrices, etc.)
    degrade to a flattened literal text run.
    """
    # Strip the namespace prefix to get the local tag name.
    tag_full = node.tag
    tag = tag_full.rsplit("}", 1)[-1] if "}" in tag_full else tag_full

    if tag in _MATHML_ROW_TAGS:
        for child in node:
            _stdlib_append_mathml(child, parent)
        return

    if tag in _MATHML_TEXT_TAGS:
        text = node.text or ""
        if text:
            run = ET.SubElement(parent, _qm("r"))
            ET.SubElement(run, _qm("t")).text = text
        return

    if tag == "msup":
        kids = list(node)
        base = kids[0] if kids else None
        exp = kids[1] if len(kids) > 1 else None
        sup = ET.SubElement(parent, _qm("sSup"))
        e = ET.SubElement(sup, _qm("e"))
        if base is not None:
            _stdlib_append_mathml(base, e)
        sup_el = ET.SubElement(sup, _qm("sup"))
        if exp is not None:
            _stdlib_append_mathml(exp, sup_el)
        return

    if tag == "msub":
        kids = list(node)
        base = kids[0] if kids else None
        sub = kids[1] if len(kids) > 1 else None
        s = ET.SubElement(parent, _qm("sSub"))
        e = ET.SubElement(s, _qm("e"))
        if base is not None:
            _stdlib_append_mathml(base, e)
        sub_el = ET.SubElement(s, _qm("sub"))
        if sub is not None:
            _stdlib_append_mathml(sub, sub_el)
        return

    if tag == "msubsup":
        kids = list(node)
        base = kids[0] if kids else None
        sub = kids[1] if len(kids) > 1 else None
        sup = kids[2] if len(kids) > 2 else None
        ss = ET.SubElement(parent, _qm("sSubSup"))
        e = ET.SubElement(ss, _qm("e"))
        if base is not None:
            _stdlib_append_mathml(base, e)
        sub_el = ET.SubElement(ss, _qm("sub"))
        if sub is not None:
            _stdlib_append_mathml(sub, sub_el)
        sup_el = ET.SubElement(ss, _qm("sup"))
        if sup is not None:
            _stdlib_append_mathml(sup, sup_el)
        return

    if tag == "mfrac":
        kids = list(node)
        num = kids[0] if kids else None
        den = kids[1] if len(kids) > 1 else None
        f = ET.SubElement(parent, _qm("f"))
        n_el = ET.SubElement(f, _qm("num"))
        if num is not None:
            _stdlib_append_mathml(num, n_el)
        d_el = ET.SubElement(f, _qm("den"))
        if den is not None:
            _stdlib_append_mathml(den, d_el)
        return

    if tag == "msqrt":
        rad = ET.SubElement(parent, _qm("rad"))
        rad_pr = ET.SubElement(rad, _qm("radPr"))
        deg_hide = ET.SubElement(rad_pr, _qm("degHide"))
        deg_hide.set(_qm("val"), "1")
        ET.SubElement(rad, _qm("deg"))
        e = ET.SubElement(rad, _qm("e"))
        for child in node:
            _stdlib_append_mathml(child, e)
        return

    if tag == "mroot":
        kids = list(node)
        base = kids[0] if kids else None
        index = kids[1] if len(kids) > 1 else None
        rad = ET.SubElement(parent, _qm("rad"))
        deg_el = ET.SubElement(rad, _qm("deg"))
        if index is not None:
            _stdlib_append_mathml(index, deg_el)
        e = ET.SubElement(rad, _qm("e"))
        if base is not None:
            _stdlib_append_mathml(base, e)
        return

    if tag == "mfenced":
        d = ET.SubElement(parent, _qm("d"))
        for child in node:
            _stdlib_append_mathml(child, d)
        return

    # Unrecognized construct (mtable, mmultiscripts, menclose, ...) — degrade
    # to a flattened literal text run so the rest of the expression still converts.
    flat = "".join(node.itertext())
    if flat:
        run = ET.SubElement(parent, _qm("r"))
        ET.SubElement(run, _qm("t")).text = flat


def latex_to_omml_local(latex: str | None) -> str | None:
    """Best-effort LaTeX -> OOXML <m:oMath> XML string, or None.

    a80af3a0 — stdlib ET port of doc_store.latex_to_omml (the lxml version).
    Pipeline: latex2mathml (pure Python) -> standard MathML string ->
    ET.fromstring -> _stdlib_append_mathml -> ET.tostring.

    Never raises: any failure (missing dependency, unparsable LaTeX, malformed
    MathML) returns None.  latex2mathml is declared in pyproject.toml as an
    explicit dependency; if it is absent the function degrades to None gracefully.
    """
    if not isinstance(latex, str) or not latex.strip():
        return None
    try:
        import latex2mathml.converter as _l2m  # noqa: PLC0415 — optional dep
    except Exception:  # noqa: BLE001
        return None
    try:
        mathml_str = _l2m.convert(latex)
        mathml_root = ET.fromstring(mathml_str)
        # Build the <m:oMath> root element.
        omath = ET.Element(_qm("oMath"))
        _stdlib_append_mathml(mathml_root, omath)
        return ET.tostring(omath, encoding="unicode")
    except Exception:  # noqa: BLE001 — conversion is best-effort
        return None


def _resolve_omml(payload: str) -> str | None:
    """Resolve ``payload`` to a raw OMML string or None.

    Accepts three input forms:
      - Raw OMML XML:  starts with "<" and parses as XML containing <m:oMath>.
      - LaTeX string:  anything else, converted via latex_to_omml_local().
      - Blank string:  returns None.

    Returns the OMML string, or None when the payload is unresolvable.
    Raises ValueError on malformed XML payloads (start with "<" but not valid).
    """
    if not payload or not payload.strip():
        return None
    stripped = payload.strip()
    if stripped.startswith("<"):
        # Validate: must be parseable XML.
        try:
            ET.fromstring(stripped)
        except ET.ParseError as exc:
            raise ValueError(f"payload starts with '<' but is not valid XML: {exc}") from exc
        return stripped
    return latex_to_omml_local(stripped)


# ---------------------------------------------------------------------------
# EXTRACTION: parse_docx_equations_local
# ---------------------------------------------------------------------------

def _cell_text(tc: ET.Element) -> str:
    """Concatenate all <w:t> text inside a table cell element."""
    return "".join(t.text or "" for t in tc.iter(_q(_W, "t")))


def _cell_has_omath(tc: ET.Element) -> bool:
    """Return True if a table cell contains at least one <m:oMath>."""
    return tc.find(f".//{_qm('oMath')}") is not None


def parse_docx_equations_local(
    source: str | bytes | bytearray,
) -> list[dict[str, Any]]:
    """Parse every <m:oMath> in word/document.xml via stdlib ET (a80af3a0).

    Reads the real OOXML tree directly out of the .docx ZIP using zipfile +
    xml.etree.ElementTree — no lxml, no third-party deps beyond stdlib.

    Returns an ordered list of records::

        {
            "ordinal":    int,          # 0-based document order
            "para_id":    str,          # w14:paraId or synthesized "p{index}"
            "omml_raw":   str,          # serialized <m:oMath>...</m:oMath> XML
            "pattern":    str,          # "standalone" | "table-numbered"
            "number":     str | None,   # equation number "(1)" for table pattern
            "flat_text":  str,          # flattened <m:t> content (dedup key)
        }

    Two patterns are detected:

    1. **standalone**: an <m:oMath> occurring inside a <w:p> in the body
       (including inside table cells that are not numbered-equation tables).
       ``number`` is ``None``.

    2. **table-numbered**: a <w:tbl> row where the first cell contains an
       <m:oMath> and the second cell contains a parenthesised equation number
       (e.g. "(1)", "(2a)").  The number is extracted and associated as the
       equation's ``number`` field.  The ``para_id`` is synthesized from the
       table's position in the body (``tbl{body_child_index}``) unless the cell
       paragraph has a real w14:paraId.

    A document with no equations returns [].
    """
    if isinstance(source, (bytes, bytearray)):
        zf = zipfile.ZipFile(io.BytesIO(bytes(source)))
    else:
        zf = zipfile.ZipFile(source)
    try:
        with zf.open("word/document.xml") as handle:
            xml_bytes = handle.read()
    finally:
        zf.close()

    root = ET.fromstring(xml_bytes)
    body = root.find(_q(_W, "body"))
    if body is None:
        return []

    w_p = _q(_W, "p")
    w_tbl = _q(_W, "tbl")
    w_tr = _q(_W, "tr")
    w_tc = _q(_W, "tc")
    w14_para_id = _q(_W14, "paraId")
    m_omath = _qm("oMath")

    equations: list[dict[str, Any]] = []
    ordinal = 0
    p_global_idx = 0  # counts every <w:p> in document order for synth ids

    for body_child_idx, child in enumerate(body):
        if child.tag == w_p:
            para_id = child.get(w14_para_id) or f"p{p_global_idx}"
            for omath_el in child.findall(f".//{m_omath}"):
                equations.append({
                    "ordinal": ordinal,
                    "para_id": para_id,
                    "omml_raw": ET.tostring(omath_el, encoding="unicode"),
                    "pattern": "standalone",
                    "number": None,
                    "flat_text": _omml_flatten_text_local(
                        ET.tostring(omath_el, encoding="unicode")
                    ),
                })
                ordinal += 1
            p_global_idx += 1

        elif child.tag == w_tbl:
            # Check every row for the equation-with-numbering pattern:
            # first cell has oMath, second cell has a parenthesised number.
            for tr in child.findall(f".//{w_tr}"):
                cells = tr.findall(w_tc)
                if len(cells) >= 2 and _cell_has_omath(cells[0]):
                    number_text = _cell_text(cells[1]).strip()
                    if _EQ_NUMBER_RE.match(number_text):
                        # Table-numbered equation.
                        # Use the first paragraph's para_id inside the cell, or synth.
                        cell0_para = cells[0].find(w_p)
                        if cell0_para is not None:
                            para_id = cell0_para.get(w14_para_id) or f"tbl{body_child_idx}"
                        else:
                            para_id = f"tbl{body_child_idx}"
                        for omath_el in cells[0].iter(m_omath):
                            equations.append({
                                "ordinal": ordinal,
                                "para_id": para_id,
                                "omml_raw": ET.tostring(omath_el, encoding="unicode"),
                                "pattern": "table-numbered",
                                "number": number_text,
                                "flat_text": _omml_flatten_text_local(
                                    ET.tostring(omath_el, encoding="unicode")
                                ),
                            })
                            ordinal += 1
                        continue  # handled — don't fall through to standalone scan

                # Not a numbered-equation table row — scan any oMath as standalone.
                for omath_el in tr.iter(m_omath):
                    # Derive para_id from containing <w:p> if possible.
                    para_id = f"tbl{body_child_idx}"
                    equations.append({
                        "ordinal": ordinal,
                        "para_id": para_id,
                        "omml_raw": ET.tostring(omath_el, encoding="unicode"),
                        "pattern": "standalone",
                        "number": None,
                        "flat_text": _omml_flatten_text_local(
                            ET.tostring(omath_el, encoding="unicode")
                        ),
                    })
                    ordinal += 1

    return equations


# ---------------------------------------------------------------------------
# LOCAL SIDECAR: docx_equations table
# ---------------------------------------------------------------------------

def _ensure_equations_table(conn: sqlite3.Connection) -> None:
    """Add the docx_equations table to the sidecar if not already present."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS docx_equations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ordinal INTEGER NOT NULL,
            para_id TEXT,
            omml_raw TEXT NOT NULL,
            pattern TEXT NOT NULL DEFAULT 'standalone',
            number TEXT,
            flat_text TEXT NOT NULL DEFAULT ''
        )
        """
    )


def index_docx_equations(
    source: str | bytes | bytearray,
    index_db_path: str,
) -> dict[str, Any]:
    """a80af3a0 — parse a .docx and store its equations into the sidecar SQLite.

    Extends the sidecar DB at ``index_db_path`` (created if absent) with the
    ``docx_equations`` table.  Idempotent: the table is fully replaced on each
    run.

    Returns ``{index_db, equation_count}``.
    """
    equations = parse_docx_equations_local(source)
    conn = _connect(index_db_path)
    try:
        _ensure_equations_table(conn)
        conn.execute("DELETE FROM docx_equations")
        conn.executemany(
            "INSERT INTO docx_equations "
            "(ordinal, para_id, omml_raw, pattern, number, flat_text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    eq["ordinal"],
                    eq["para_id"],
                    eq["omml_raw"],
                    eq["pattern"],
                    eq["number"],
                    eq["flat_text"],
                )
                for eq in equations
            ],
        )
        conn.commit()
    finally:
        conn.close()

    return {"index_db": index_db_path, "equation_count": len(equations)}


def get_local_equations(index_db_path: str) -> list[dict[str, Any]]:
    """a80af3a0 — retrieve all locally-stored equations from the sidecar.

    Returns a list of equation records in ordinal order, or an empty list
    when the docx_equations table does not yet exist.
    """
    conn = _connect(index_db_path)
    try:
        _ensure_equations_table(conn)
        rows = conn.execute(
            "SELECT id, ordinal, para_id, omml_raw, pattern, number, flat_text "
            "FROM docx_equations ORDER BY ordinal"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r[0],
            "ordinal": r[1],
            "para_id": r[2],
            "omml_raw": r[3],
            "pattern": r[4],
            "number": r[5],
            "flat_text": r[6],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# WRITE: insert / edit / remove equation
# ---------------------------------------------------------------------------

def _build_omath_paragraph(omml_raw: str) -> ET.Element:
    """Wrap a raw OMML string in a new <w:p> for display-mode insertion.

    Produces::

        <w:p>
          <m:oMath>...</m:oMath>
        </w:p>

    The oMath element is parsed from ``omml_raw`` and appended as a child.
    """
    p = ET.Element(_q(_W, "p"))
    omath_el = ET.fromstring(omml_raw)
    p.append(omath_el)
    return p


def insert_equation_local(
    docx_path: str,
    anchor_para_id: str,
    payload: str,
    position: str = "after",
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """a80af3a0 — Insert an equation into a .docx file.

    Accepts raw OMML XML (starts with "<") or a LaTeX string (converted to OMML
    via latex_to_omml_local).  Three positions are supported:

      ``"before"`` — new display-mode paragraph immediately before anchor.
      ``"after"``  — new display-mode paragraph immediately after anchor.
      ``"append"`` — append the <m:oMath> inline to the anchor paragraph.

    For ``"before"`` and ``"after"``, a fresh ``<w:p>`` wrapping the
    ``<m:oMath>`` is inserted relative to the anchor paragraph (or its
    containing body child for table-embedded anchors).  For ``"append"``,
    the ``<m:oMath>`` element is appended as a direct child of the anchor
    ``<w:p>`` (inline equation style).

    Args:
        docx_path:       Absolute path to the .docx file (mutated in place).
        anchor_para_id:  ``w14:paraId`` or ``p{N}`` / ``tbl{N}`` of the
                         paragraph to anchor on.
        payload:         Raw OMML XML string (``<m:oMath>...</m:oMath>``) or a
                         LaTeX expression (e.g. ``r"\\frac{a}{b}"``).
        position:        ``"before"``, ``"after"``, or ``"append"`` (default
                         ``"after"``).
        index_db_path:   If supplied, sidecar is invalidated after write.

    Returns:
        ``{status, position, para_id, omml, docx_path}``
        or ``{"error": <message>}`` on failure (file NOT mutated on error).
    """
    if position not in ("before", "after", "append"):
        return {"error": f"position must be 'before', 'after', or 'append', got {position!r}"}
    if not payload or not str(payload).strip():
        return {"error": "payload must be a non-empty string (OMML XML or LaTeX)"}

    # Resolve OMML before touching the file — fail fast on bad input.
    try:
        omml = _resolve_omml(payload.strip())
    except ValueError as exc:
        return {"error": str(exc)}
    if omml is None:
        return {"error": f"could not convert payload to OMML: {payload!r}"}

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    result = _find_para_by_id(root, anchor_para_id)
    if result is None:
        return {"error": f"para_id {anchor_para_id!r} not found in {docx_path}"}

    body, anchor_elem, child_idx = result

    if position == "append":
        # Inline: append <m:oMath> directly to the anchor paragraph.
        omath_el = ET.fromstring(omml)
        anchor_elem.append(omath_el)
    else:
        # Display: insert a new <w:p> wrapping the equation.
        new_p = _build_omath_paragraph(omml)
        insert_at = child_idx if position == "before" else child_idx + 1
        body.insert(insert_at, new_p)

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "inserted",
        "position": position,
        "para_id": anchor_para_id,
        "omml": omml,
        "docx_path": docx_path,
    }


def edit_equation_local(
    docx_path: str,
    equation_para_id: str,
    new_payload: str,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """a80af3a0 — Replace the <m:oMath> in an existing equation paragraph.

    Locates the paragraph by ``equation_para_id``, verifies it contains at
    least one ``<m:oMath>``, removes all existing ``<m:oMath>`` children, and
    inserts the new equation (resolved from OMML or LaTeX).

    Args:
        docx_path:         Absolute path to the .docx file (mutated in place).
        equation_para_id:  ``w14:paraId`` or ``p{N}`` of the equation paragraph.
        new_payload:       Raw OMML XML or LaTeX expression.
        index_db_path:     If supplied, sidecar is invalidated after write.

    Returns:
        ``{status, equation_para_id, omml, docx_path}``
        or ``{"error": <message>}`` on failure.
    """
    if not new_payload or not str(new_payload).strip():
        return {"error": "new_payload must be a non-empty string (OMML XML or LaTeX)"}

    try:
        omml = _resolve_omml(new_payload.strip())
    except ValueError as exc:
        return {"error": str(exc)}
    if omml is None:
        return {"error": f"could not convert new_payload to OMML: {new_payload!r}"}

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    result = _find_para_by_id(root, equation_para_id)
    if result is None:
        return {"error": f"para_id {equation_para_id!r} not found in {docx_path}"}

    _body, para_elem, _cidx = result

    # Verify: paragraph must contain at least one oMath.
    m_omath_tag = _qm("oMath")
    existing = [el for el in para_elem if el.tag == m_omath_tag]
    if not existing:
        # Also check deeper (oMath might be wrapped in oMathPara).
        existing = list(para_elem.iter(m_omath_tag))
    if not existing:
        return {
            "error": (
                f"paragraph {equation_para_id!r} does not contain an <m:oMath> element; "
                "use insert_equation_local to add a new equation"
            )
        }

    # Remove all direct-child oMath elements (and oMathPara wrappers).
    m_omath_para_tag = _qm("oMathPara")
    for child in list(para_elem):
        if child.tag in (m_omath_tag, m_omath_para_tag):
            para_elem.remove(child)

    # Append the new oMath.
    omath_el = ET.fromstring(omml)
    para_elem.append(omath_el)

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "edited",
        "equation_para_id": equation_para_id,
        "omml": omml,
        "docx_path": docx_path,
    }


def remove_equation_local(
    docx_path: str,
    equation_para_id: str,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """a80af3a0 — Remove an equation paragraph (or inline oMath) from a .docx.

    If the paragraph contains ONLY an <m:oMath> (a display-mode equation
    paragraph), the entire paragraph is removed from the body.  If the
    paragraph also contains non-equation text runs (an inline equation appended
    to a text paragraph), only the <m:oMath> elements are removed, leaving the
    paragraph intact.

    Args:
        docx_path:         Absolute path to the .docx file (mutated in place).
        equation_para_id:  ``w14:paraId`` or ``p{N}`` of the equation paragraph.
        index_db_path:     If supplied, sidecar is invalidated after write.

    Returns:
        ``{status, equation_para_id, removed_whole_paragraph, docx_path}``
        or ``{"error": <message>}`` on failure.
    """
    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    result = _find_para_by_id(root, equation_para_id)
    if result is None:
        return {"error": f"para_id {equation_para_id!r} not found in {docx_path}"}

    body, para_elem, _cidx = result

    m_omath_tag = _qm("oMath")
    m_omath_para_tag = _qm("oMathPara")

    # Check whether the paragraph has ANY oMath.
    all_omath = list(para_elem.iter(m_omath_tag))
    if not all_omath:
        return {
            "error": (
                f"paragraph {equation_para_id!r} does not contain an <m:oMath> element"
            )
        }

    # Determine whether this paragraph is SOLELY an equation paragraph.
    # "Solely" = all direct children are oMath / oMathPara / pPr (style).
    eq_tags = {m_omath_tag, m_omath_para_tag, _q(_W, "pPr")}
    non_eq_children = [c for c in para_elem if c.tag not in eq_tags]
    remove_whole = len(non_eq_children) == 0

    if remove_whole:
        body.remove(para_elem)
    else:
        # Inline: remove only the oMath elements (and oMathPara wrappers).
        for child in list(para_elem):
            if child.tag in (m_omath_tag, m_omath_para_tag):
                para_elem.remove(child)

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "removed",
        "equation_para_id": equation_para_id,
        "removed_whole_paragraph": remove_whole,
        "docx_path": docx_path,
    }


# ---------------------------------------------------------------------------
# 1258794a — Bibliography write-back: insert / update / remove / sync
#
# Design decisions:
#   - Zotero is the sole citation backend (no Mendeley / EndNote / RefWorks
#     manual entry). Item data is fetched from the local Zotero API via
#     meridian.zotero_client (same client used by the cross-document resolver),
#     called through a small synchronous shim so we stay stdlib-only inside
#     this extension process.
#   - APA 7 formatting is supported for four item types (journal article, book,
#     book chapter, conference paper / proceedings). A full citeproc-js-equivalent
#     CSL style engine is explicitly out of scope — the formatter is a solid,
#     tested APA formatter for the most common academic item types. Other types
#     degrade gracefully to a minimal "Author (Year). Title." form.
#   - Bibliography entries are written as plain <w:p> paragraphs with a
#     "Bibliography" style when available (Word's built-in hanging-indent style),
#     located after a "References" or "Bibliography" heading. If no such heading
#     exists one is created at the end of the document body.
#   - Each entry paragraph carries a bookmark pair (w:bookmarkStart /
#     w:bookmarkEnd) with name "bibkey_<safe_key>" so update / remove can locate
#     the correct paragraph reliably without re-parsing the formatted text string.
#     This is the standard OOXML mechanism that Word preserves across re-saves.
#   - Re-using _load_docx_xml_stdlib / _save_docx_xml_stdlib / _find_para_by_id
#     / _extract_keys_from_instruction / _scan_citation_field — no new I/O or
#     OOXML primitives needed beyond what captions + citations already supply.
# ---------------------------------------------------------------------------

_REFS_HEADING_TEXTS = frozenset({"references", "bibliography", "works cited", "literature cited"})
_BIBKEY_BOOKMARK_PREFIX = "bibkey_"
_BIBLIOGRAPHY_STYLE = "Bibliography"


# ---------------------------------------------------------------------------
# APA 7 formatter — CSL-JSON item → formatted reference-list string
#
# Scope: journal article, book, book chapter, conference paper.
# Out of scope: full CSL style-engine generality (citeproc-js equivalent),
# report / thesis / dataset / webpage / patent types — these degrade to a
# minimal "Author (Year). Title." form. Documented in the commit message.
# ---------------------------------------------------------------------------

def _apa_authors(authors: list[dict[str, Any]]) -> str:
    """Format a CSL-JSON author array into an APA author string.

    CSL-JSON author: {"family": "Smith", "given": "John A."} or
    {"literal": "World Health Organization"} for corporate authors.

    APA format:
      1 author:  Smith, J. A.
      2 authors: Smith, J. A., & Jones, B.
      3-19:      Smith, J. A., Jones, B., ... & Last, C.
      20+:       first 19 then "... & Last, C."
    """
    if not authors:
        return "Unknown Author"

    def _fmt_one(a: dict[str, Any]) -> str:
        lit = a.get("literal")
        if lit:
            return str(lit).strip()
        family = str(a.get("family") or "").strip()
        given = str(a.get("given") or "").strip()
        if not family:
            return given or "Unknown"
        if not given:
            return family
        # Abbreviate given name(s): "John A." -> "J. A.", "J." -> "J."
        initials = " ".join(
            p[0].upper() + "." if not p.endswith(".") else p.upper()
            for p in given.replace("-", " ").split()
        )
        return f"{family}, {initials}"

    if len(authors) == 1:
        return _fmt_one(authors[0])
    if len(authors) == 2:
        return f"{_fmt_one(authors[0])}, & {_fmt_one(authors[1])}"
    if len(authors) <= 19:
        parts = [_fmt_one(a) for a in authors[:-1]]
        return ", ".join(parts) + f", & {_fmt_one(authors[-1])}"
    # 20+ authors: first 19, ellipsis, last.
    parts19 = [_fmt_one(a) for a in authors[:19]]
    return ", ".join(parts19) + f", ... {_fmt_one(authors[-1])}"


def _apa_year(item: dict[str, Any]) -> str:
    """Extract the publication year from a CSL-JSON item.

    CSL-JSON ``issued`` field: {"date-parts": [[2023]]} or [[2023, 5, 12]].
    Falls back to ``year`` (some Zotero exports use this), then to "n.d.".
    """
    issued = item.get("issued")
    if isinstance(issued, dict):
        parts = issued.get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            year = parts[0][0]
            if year:
                return str(year)
    # Fallback: top-level year (non-standard but used by some Zotero exports).
    year_raw = item.get("year")
    if year_raw:
        return str(year_raw).strip()
    return "n.d."


def _apa_doi_or_url(item: dict[str, Any]) -> str:
    """Return 'https://doi.org/<doi>' if DOI present, else URL, else ''."""
    doi = item.get("DOI") or item.get("doi")
    if doi and str(doi).strip():
        d = str(doi).strip()
        if not d.startswith("http"):
            d = "https://doi.org/" + d
        return d
    url = item.get("URL") or item.get("url")
    if url and str(url).strip():
        return str(url).strip()
    return ""


def format_apa_reference(item: dict[str, Any]) -> str:
    """Format a CSL-JSON item as an APA 7th-edition reference-list entry.

    Supported item types (``type`` / ``itemType`` field):
      - ``article-journal`` / ``journalArticle`` -> journal article format
      - ``book``                                  -> book format
      - ``chapter`` / ``bookSection``             -> book chapter format
      - ``paper-conference`` / ``conferencePaper`` -> conference paper format
      - anything else                             -> minimal fallback

    The ``item`` dict is CSL-JSON-shaped (as returned by Zotero's local API).
    Pure / deterministic and never raises (returns a best-effort string on
    any missing / malformed data).

    OUT OF SCOPE: reports, theses, datasets, webpages, patents, and any type
    not listed above — they get the minimal fallback: Author (Year). Title.
    Full CSL style-engine support (citeproc-js equivalent) is a declared
    non-goal for this item (1258794a).
    """
    if not isinstance(item, dict):
        return ""

    authors = item.get("author") or []
    if not isinstance(authors, list):
        authors = []

    item_type = str(item.get("type") or item.get("itemType") or "").strip()

    author_str = _apa_authors(authors)
    year = _apa_year(item)
    title = str(item.get("title") or "Untitled").strip()
    doi_url = _apa_doi_or_url(item)

    # --- Journal article ---
    if item_type in ("article-journal", "journalArticle", "article"):
        journal = str(
            item.get("container-title") or item.get("journalAbbreviation") or ""
        ).strip()
        volume = str(item.get("volume") or "").strip()
        issue = str(item.get("issue") or "").strip()
        page = str(item.get("page") or "").strip()
        parts = [f"{author_str} ({year}). {title}."]
        source_parts: list[str] = []
        if journal:
            if volume and issue:
                source_parts.append(f"{journal}, {volume}({issue})")
            elif volume:
                source_parts.append(f"{journal}, {volume}")
            else:
                source_parts.append(journal)
        if page:
            if source_parts:
                source_parts[-1] += f", {page}"
            else:
                source_parts.append(page)
        if source_parts:
            parts.append(" ".join(source_parts) + ".")
        if doi_url:
            parts.append(doi_url)
        return " ".join(parts)

    # --- Book ---
    if item_type in ("book",):
        publisher = str(item.get("publisher") or "").strip()
        place = str(
            item.get("publisher-place") or item.get("place") or ""
        ).strip()
        edition = str(item.get("edition") or "").strip()
        ed_str = f" ({edition} ed.)" if edition else ""
        parts = [f"{author_str} ({year}). {title}{ed_str}."]
        pub_parts: list[str] = []
        if place:
            pub_parts.append(place)
        if publisher:
            pub_parts.append(publisher)
        if pub_parts:
            parts.append(": ".join(pub_parts) + ".")
        if doi_url:
            parts.append(doi_url)
        return " ".join(parts)

    # --- Book chapter ---
    if item_type in ("chapter", "bookSection"):
        editor_list = item.get("editor") or []
        if not isinstance(editor_list, list):
            editor_list = []
        container = str(item.get("container-title") or "").strip()
        publisher = str(item.get("publisher") or "").strip()
        page = str(item.get("page") or "").strip()
        ed_names = _apa_authors(editor_list) if editor_list else ""
        ed_role = " (Ed.)," if len(editor_list) == 1 else " (Eds.),"
        parts = [f"{author_str} ({year}). {title}."]
        in_parts: list[str] = []
        if ed_names:
            in_parts.append(f"In {ed_names}{ed_role}")
        if container:
            in_parts.append(container)
        if page:
            in_parts.append(f"(pp. {page})")
        if in_parts:
            parts.append(" ".join(in_parts) + ".")
        if publisher:
            parts.append(publisher + ".")
        if doi_url:
            parts.append(doi_url)
        return " ".join(parts)

    # --- Conference paper ---
    if item_type in ("paper-conference", "conferencePaper"):
        conference = str(
            item.get("container-title")
            or item.get("event-title")
            or item.get("event")
            or ""
        ).strip()
        publisher = str(item.get("publisher") or "").strip()
        page = str(item.get("page") or "").strip()
        parts = [f"{author_str} ({year}). {title}."]
        if conference:
            conf_str = conference
            if page:
                conf_str += f", {page}"
            parts.append(conf_str + ".")
        if publisher:
            parts.append(publisher + ".")
        if doi_url:
            parts.append(doi_url)
        return " ".join(parts)

    # --- Fallback for unrecognised types ---
    parts = [f"{author_str} ({year}). {title}."]
    if doi_url:
        parts.append(doi_url)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Document scan: discover all citation keys present in the document
# ---------------------------------------------------------------------------

def scan_all_citation_keys(docx_path: str) -> list[str]:
    """Return a deduplicated list of all citation keys present in a .docx.

    Walks every paragraph in ``word/document.xml`` looking for CSL_CITATION
    complex fields (same ``_scan_citation_field`` used by ``remove_citation``).
    Returns keys in first-appearance order.  Returns [] on any error.
    """
    try:
        _raw, root = _load_docx_xml_stdlib(docx_path)
    except (FileNotFoundError, ValueError):
        return []

    body = root.find(_q(_W, "body"))
    if body is None:
        return []

    seen: set[str] = set()
    ordered: list[str] = []

    for para in body.iter(_q(_W, "p")):
        scan = _scan_citation_field(para)
        if scan is None:
            continue
        _bi, _ei, instr, _disp = scan
        for key in _extract_keys_from_instruction(instr):
            if key not in seen:
                seen.add(key)
                ordered.append(key)

    return ordered


# ---------------------------------------------------------------------------
# Bibliography section locator / creator
# ---------------------------------------------------------------------------

def _find_references_heading(
    body: ET.Element,
) -> tuple[int, ET.Element] | None:
    """Find a References / Bibliography heading paragraph in the body.

    Returns ``(body_child_index, heading_element)`` for the FIRST body-level
    paragraph whose style is a heading style AND whose text normalises to one
    of the recognised heading texts (``references``, ``bibliography``, etc.).

    Returns ``None`` when no such heading is found.
    """
    w_p = _q(_W, "p")
    w_pStyle = _q(_W, "pStyle")
    w_pPr = _q(_W, "pPr")
    w_val = _q(_W, "val")
    w_t = _q(_W, "t")

    for idx, child in enumerate(body):
        if child.tag != w_p:
            continue
        ppr = child.find(w_pPr)
        if ppr is None:
            continue
        pstyle = ppr.find(w_pStyle)
        if pstyle is None:
            continue
        style_val = pstyle.get(w_val) or ""
        if not _is_heading(style_val):
            continue
        text = "".join(t.text or "" for t in child.iter(w_t)).strip().lower()
        if text in _REFS_HEADING_TEXTS:
            return (idx, child)

    return None


def _build_references_heading() -> ET.Element:
    """Build a ``<w:p>`` element for a 'References' heading (Heading1 style)."""
    p = ET.Element(_q(_W, "p"))
    pPr = ET.SubElement(p, _q(_W, "pPr"))
    pStyle = ET.SubElement(pPr, _q(_W, "pStyle"))
    pStyle.set(_q(_W, "val"), "Heading1")
    r = ET.SubElement(p, _q(_W, "r"))
    t = ET.SubElement(r, _q(_W, "t"))
    t.text = "References"
    return p


def _build_bibliography_paragraph(
    citation_key: str,
    formatted_text: str,
) -> ET.Element:
    """Build a ``<w:p>`` for a bibliography entry.

    Uses the ``Bibliography`` style (Word's built-in hanging-indent style).
    Embeds a ``w:bookmarkStart`` / ``w:bookmarkEnd`` pair with name
    ``bibkey_<safe_key>`` so update / remove operations can locate this
    paragraph reliably.
    """
    # Sanitise the citation key for use as a bookmark name.
    # Word bookmark names: alphanumeric + underscore, max 40 chars, starts with letter.
    safe_key = re.sub(r"[^A-Za-z0-9_]", "_", citation_key)
    if safe_key and safe_key[0].isdigit():
        safe_key = "k_" + safe_key
    safe_key = safe_key[:40]
    bookmark_name = f"{_BIBKEY_BOOKMARK_PREFIX}{safe_key}"

    p = ET.Element(_q(_W, "p"))

    # Paragraph properties: Bibliography style.
    pPr = ET.SubElement(p, _q(_W, "pPr"))
    pStyle = ET.SubElement(pPr, _q(_W, "pStyle"))
    pStyle.set(_q(_W, "val"), _BIBLIOGRAPHY_STYLE)

    # Bookmark start (id=0; Word renumbers on next open — that's fine).
    bm_start = ET.SubElement(p, _q(_W, "bookmarkStart"))
    bm_start.set(_q(_W, "id"), "0")
    bm_start.set(_q(_W, "name"), bookmark_name)

    # Text run with the formatted reference.
    r = ET.SubElement(p, _q(_W, "r"))
    t = ET.SubElement(r, _q(_W, "t"))
    t.set(_q(_XML_NS, "space"), "preserve")
    t.text = formatted_text

    # Bookmark end.
    bm_end = ET.SubElement(p, _q(_W, "bookmarkEnd"))
    bm_end.set(_q(_W, "id"), "0")

    return p


def _find_bibliography_entry(
    body: ET.Element,
    citation_key: str,
) -> tuple[int, ET.Element] | None:
    """Find an existing bibliography entry paragraph for ``citation_key``.

    Searches for a ``<w:bookmarkStart>`` whose ``w:name`` is
    ``bibkey_<safe_key>`` and returns ``(body_child_index, paragraph_element)``
    for the body-level ``<w:p>`` that contains it.

    Returns ``None`` when not found.
    """
    safe_key = re.sub(r"[^A-Za-z0-9_]", "_", citation_key)
    if safe_key and safe_key[0].isdigit():
        safe_key = "k_" + safe_key
    safe_key = safe_key[:40]
    bookmark_name = f"{_BIBKEY_BOOKMARK_PREFIX}{safe_key}"

    w_p = _q(_W, "p")
    w_bookmarkStart = _q(_W, "bookmarkStart")
    w_name = _q(_W, "name")

    for idx, child in enumerate(body):
        if child.tag != w_p:
            continue
        for bm in child.iter(w_bookmarkStart):
            if bm.get(w_name) == bookmark_name:
                return (idx, child)

    return None


def _bibliography_entries_range(
    body: ET.Element,
    heading_idx: int,
) -> tuple[int, int]:
    """Return ``(start_idx, end_idx)`` for the bibliography block after the heading.

    ``start_idx`` is heading_idx + 1.
    ``end_idx`` is the index of the first body child AFTER the bibliography
    block (i.e., the next heading or the body's end).  A new entry should be
    inserted at ``end_idx``.
    """
    start = heading_idx + 1
    body_list = list(body)
    n = len(body_list)
    end = start

    for i in range(start, n):
        child = body_list[i]
        if child.tag != _q(_W, "p"):
            end = i
            break
        ppr = child.find(_q(_W, "pPr"))
        style_val = ""
        if ppr is not None:
            ps = ppr.find(_q(_W, "pStyle"))
            if ps is not None:
                style_val = ps.get(_q(_W, "val")) or ""
        # Stop at the next heading.
        if _is_heading(style_val):
            end = i
            break
        end = i + 1
    else:
        end = n

    return (start, end)


# ---------------------------------------------------------------------------
# Public bibliography API: insert / update / remove / sync
# ---------------------------------------------------------------------------

def insert_bibliography_entry(
    docx_path: str,
    citation_key: str,
    csl_item: dict[str, Any],
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """1258794a — Write a formatted APA bibliography entry into a .docx.

    Locates (or creates) a ``References`` heading at the end of the document,
    then inserts a new entry paragraph at the end of the references block.
    The entry is formatted as an APA 7th-edition reference from the supplied
    CSL-JSON ``csl_item`` dict (as returned by Zotero's local API or by
    ``zotero_client.resolve_citation_ref`` + ``fetch_zotero_csl_item``).

    If an entry for ``citation_key`` already exists (detected by bookmark name)
    this function returns an error — use ``update_bibliography_entry`` instead.

    Args:
        docx_path:     Absolute path to the .docx file (mutated in place).
        citation_key:  Stable citation identifier (DOI, Zotero key, citekey —
                       used as the bookmark name so update/remove can find it).
        csl_item:      CSL-JSON-shaped item dict with at minimum ``author``,
                       ``title``, ``type``/``itemType``, and ``issued`` fields.
        index_db_path: If supplied, sidecar is invalidated after the write.

    Returns:
        ``{status, citation_key, formatted_text, docx_path}``
        or ``{"error": <message>}`` on failure (file NOT mutated on error).
    """
    if not citation_key or not str(citation_key).strip():
        return {"error": "citation_key must be a non-empty string"}
    if not isinstance(csl_item, dict):
        return {"error": "csl_item must be a CSL-JSON dict"}

    clean_key = str(citation_key).strip()

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    if body is None:
        return {"error": "document has no body element"}

    # Check for duplicate.
    if _find_bibliography_entry(body, clean_key) is not None:
        return {
            "error": (
                f"bibliography entry for {clean_key!r} already exists; "
                "use update_bibliography_entry to refresh it"
            )
        }

    # Format the reference text.
    try:
        formatted_text = format_apa_reference(csl_item)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not format CSL-JSON item: {exc}"}

    if not formatted_text.strip():
        return {"error": "formatted reference text is empty — malformed CSL-JSON item?"}

    # Locate or create the References heading.
    heading_result = _find_references_heading(body)
    if heading_result is None:
        # Create heading at end of body (before sectPr if present).
        body_list = list(body)
        insert_pos = len(body_list)
        # sectPr is usually the last element; don't insert after it.
        if body_list and body_list[-1].tag == _q(_W, "sectPr"):
            insert_pos = len(body_list) - 1
        heading_p = _build_references_heading()
        body.insert(insert_pos, heading_p)
        heading_idx = insert_pos
        entry_insert_pos = heading_idx + 1
    else:
        heading_idx, _heading_elem = heading_result
        _start, end = _bibliography_entries_range(body, heading_idx)
        entry_insert_pos = end

    entry_p = _build_bibliography_paragraph(clean_key, formatted_text)
    body.insert(entry_insert_pos, entry_p)

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "inserted",
        "citation_key": clean_key,
        "formatted_text": formatted_text,
        "docx_path": docx_path,
    }


def update_bibliography_entry(
    docx_path: str,
    citation_key: str,
    csl_item: dict[str, Any],
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """1258794a — Refresh the formatted text of an existing bibliography entry.

    Locates the entry paragraph for ``citation_key`` by its embedded bookmark
    name, re-formats the reference from the (possibly updated) ``csl_item``,
    and replaces the text run in-place.

    Args:
        docx_path:     Absolute path to the .docx file (mutated in place).
        citation_key:  The same key used when the entry was inserted.
        csl_item:      Updated CSL-JSON item dict.
        index_db_path: If supplied, sidecar is invalidated after the write.

    Returns:
        ``{status, citation_key, formatted_text, docx_path}``
        or ``{"error": <message>}`` on failure.
    """
    if not citation_key or not str(citation_key).strip():
        return {"error": "citation_key must be a non-empty string"}
    if not isinstance(csl_item, dict):
        return {"error": "csl_item must be a CSL-JSON dict"}

    clean_key = str(citation_key).strip()

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    if body is None:
        return {"error": "document has no body element"}

    entry_result = _find_bibliography_entry(body, clean_key)
    if entry_result is None:
        return {
            "error": (
                f"no bibliography entry found for {clean_key!r}; "
                "use insert_bibliography_entry to add it first"
            )
        }

    _entry_idx, entry_elem = entry_result

    try:
        formatted_text = format_apa_reference(csl_item)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not format CSL-JSON item: {exc}"}

    if not formatted_text.strip():
        return {"error": "formatted reference text is empty — malformed CSL-JSON item?"}

    # Replace the first <w:t> text run in the paragraph.
    for t_el in entry_elem.iter(_q(_W, "t")):
        t_el.text = formatted_text
        t_el.set(_q(_XML_NS, "space"), "preserve")
        break

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "updated",
        "citation_key": clean_key,
        "formatted_text": formatted_text,
        "docx_path": docx_path,
    }


def remove_bibliography_entry(
    docx_path: str,
    citation_key: str,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """1258794a — Remove a bibliography entry paragraph from a .docx.

    Locates the entry by the ``bibkey_<key>`` bookmark and removes the entire
    paragraph.

    Args:
        docx_path:     Absolute path to the .docx file (mutated in place).
        citation_key:  The citation key of the entry to remove.
        index_db_path: If supplied, sidecar is invalidated after the write.

    Returns:
        ``{status, citation_key, docx_path}``
        or ``{"error": <message>}`` on failure.
    """
    if not citation_key or not str(citation_key).strip():
        return {"error": "citation_key must be a non-empty string"}

    clean_key = str(citation_key).strip()

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    if body is None:
        return {"error": "document has no body element"}

    entry_result = _find_bibliography_entry(body, clean_key)
    if entry_result is None:
        return {"error": f"no bibliography entry found for {clean_key!r}"}

    _entry_idx, entry_elem = entry_result
    body.remove(entry_elem)

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "removed",
        "citation_key": clean_key,
        "docx_path": docx_path,
    }


def sync_bibliography(
    docx_path: str,
    csl_items: dict[str, dict[str, Any]],
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """1258794a — Reconcile bibliography entries against in-document citations.

    Given a ``csl_items`` mapping from citation key -> CSL-JSON item dict:

      1. Scans the document for all in-text citation keys.
      2. For each in-text key that has a ``csl_items`` entry:
         - Inserts it if no bibliography entry yet exists.
         - Updates it if an entry already exists (refresh in case Zotero
           data changed).
      3. Reports keys cited in-text but absent from ``csl_items`` as
         ``missing_data`` (caller must fetch from Zotero and re-call).
      4. Reports ``stale_entries``: keys with bibliography entries no longer
         cited in-text (informational; caller decides whether to remove them).

    This function does NOT alphabetize existing entries in-place — that
    would require removing and re-inserting all entries, which is risky.
    New entries are inserted at the end of the references block in citation-
    appearance order.

    Args:
        docx_path:     Absolute path to the .docx file (mutated in place).
        csl_items:     Mapping from citation key -> CSL-JSON item dict.
        index_db_path: If supplied, sidecar is invalidated after each write.

    Returns:
        ``{status, inserted, updated, missing_data, stale_entries, docx_path}``
        or ``{"error": <message>}`` on failure.

        ``missing_data``: list of keys cited in-text but absent from csl_items.
        ``stale_entries``: list of bookmark-key suffixes with entries no longer
                           cited in-text.
    """
    if not isinstance(csl_items, dict):
        return {"error": "csl_items must be a dict mapping citation_key -> CSL-JSON item"}

    if not os.path.exists(docx_path):
        return {"error": f"no such file: {docx_path}"}

    # Step 1: discover in-text keys.
    in_text_keys = scan_all_citation_keys(docx_path)

    # Step 2: discover existing bibliography entry keys (by bookmark names).
    try:
        _raw, root = _load_docx_xml_stdlib(docx_path)
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    existing_bm_keys: set[str] = set()
    if body is not None:
        w_bookmarkStart = _q(_W, "bookmarkStart")
        w_name = _q(_W, "name")
        for p in body.iter(_q(_W, "p")):
            for bm in p.iter(w_bookmarkStart):
                name = bm.get(w_name) or ""
                if name.startswith(_BIBKEY_BOOKMARK_PREFIX):
                    bm_suffix = name[len(_BIBKEY_BOOKMARK_PREFIX):]
                    existing_bm_keys.add(bm_suffix)

    in_text_set = set(in_text_keys)

    # Build the set of bookmark-key suffixes for in-text keys.
    def _safe_bm_key(k: str) -> str:
        s = re.sub(r"[^A-Za-z0-9_]", "_", k)
        if s and s[0].isdigit():
            s = "k_" + s
        return s[:40]

    in_text_bm_keys = {_safe_bm_key(k) for k in in_text_keys}
    stale_entries: list[str] = list(existing_bm_keys - in_text_bm_keys)

    missing_data: list[str] = []
    inserted: list[str] = []
    updated: list[str] = []
    errors: list[str] = []

    for key in in_text_keys:
        if key not in csl_items:
            missing_data.append(key)
            continue
        item = csl_items[key]
        bm_key = _safe_bm_key(key)

        if bm_key in existing_bm_keys:
            res = update_bibliography_entry(docx_path, key, item, index_db_path)
            if "error" in res:
                errors.append(f"{key}: {res['error']}")
            else:
                updated.append(key)
        else:
            res = insert_bibliography_entry(docx_path, key, item, index_db_path)
            if "error" in res:
                errors.append(f"{key}: {res['error']}")
            else:
                inserted.append(key)
                existing_bm_keys.add(bm_key)

    result: dict[str, Any] = {
        "status": "ok" if not errors else "partial",
        "inserted": inserted,
        "updated": updated,
        "missing_data": missing_data,
        "stale_entries": stale_entries,
        "docx_path": docx_path,
    }
    if errors:
        result["errors"] = errors
    return result


# ===========================================================================
# c84ca127 -- chunk-level heading-aware BM25 indexing (parse_docx adapter)
#
# The canonical implementation in packages/docparse/docparse/docs_intel.py
# walks ``document_content_tree``'s blocks (headings / paragraphs / tables in
# true document order) to build heading-anchored chunks.  This copy does NOT
# have ``document_content_tree`` -- it only has ``parse_docx()`` (a flat list
# of ``{index, para_id, style, text}`` dicts) and ``document_outline()``
# (heading outline derived from those dicts).
#
# Adaptation:
#   - ``_build_chunks_from_paras()`` takes the flat ``parse_docx()`` list
#     directly.  Heading detection reuses the identical ``_is_heading()`` /
#     ``_heading_level()`` helpers already in this file, so the two codepaths
#     stay consistent.
#   - Paragraphs are paragraph-text-only: ``parse_docx()`` does not carry
#     table content (tables are not paragraphs in OOXML -- they are <w:tbl>
#     siblings; the flat ``body.findall("w:p")`` pass misses them entirely).
#     This is an honest scope difference from the canonical version and is
#     documented in the docstring.  No table content is fabricated.
#   - Schema, trigger names, weight ratio (5:1), and return shapes are
#     identical to the canonical version so callers of either package see a
#     consistent API.
# ===========================================================================

# BM25 column weights: heading_text weight, body_text weight.
# 5:1 ratio -- a term in the heading is treated as 5x more relevant than the
# same term in body prose.  Matches the canonical packages/docparse value.
_CHUNK_WEIGHT_HEADING: float = 5.0
_CHUNK_WEIGHT_BODY: float = 1.0


def _build_chunks_from_paras(
    paras: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group a flat ``parse_docx()`` paragraph list into heading-anchored chunks.

    This is the parse_docx-adapted counterpart of ``_build_chunks`` in
    packages/docparse.  The algorithm is identical in structure but works from
    a flat paragraph list (each ``{index, para_id, style, text}``) rather than
    the interleaved blocks of ``document_content_tree``.

    Scope note: ``parse_docx()`` in this package collects ONLY ``<w:p>``
    paragraphs; ``<w:tbl>`` elements are structural siblings of paragraphs in
    OOXML and are not surfaced here.  Consequently chunk ``body_text`` contains
    only paragraph text -- table cell content is absent.  This is a transparent,
    honest scope difference from the canonical version; no table data is
    fabricated.

    Algorithm:
    - Walk paragraphs in document order, maintaining a ``heading_stack`` of
      ancestor headings (each ``{level, text, para_id}``).
    - When a heading paragraph is encountered:
        - Pop the stack until empty or the top has strictly lower level than
          the new heading (pop while ``top.level >= new.level``).
        - Push the new heading.
        - ``heading_path`` = ordered text of every stack entry, root first.
        - Flush any accumulating chunk and start a new one.
    - Non-heading paragraphs append their text to the current chunk.
    - Paragraphs preceding the first heading are collected into a synthetic
      "preamble" chunk with ``heading_text=""`` and ``heading_path=[]``.

    Returns a list of chunk dicts::

        {
            "chunk_id": int,               # 0-based sequential index
            "heading_text": str,           # own heading text ("" for preamble)
            "heading_path": list[str],     # ancestor texts, root first
            "heading_para_id": str | None,
            "body_text": str,              # all body paragraphs joined
            "start_para_id": str | None,   # para_id of heading (or first body para)
            "end_para_id": str | None,     # para_id of last body para (or heading)
        }
    """
    chunks: list[dict[str, Any]] = []
    heading_stack: list[dict[str, Any]] = []  # {level, text, para_id}
    current_body_parts: list[str] = []
    current_body_para_ids: list[str | None] = []
    current_heading_text: str = ""
    current_heading_path: list[str] = []
    current_heading_para_id: str | None = None
    current_start_para_id: str | None = None

    def _flush(chunk_id: int) -> dict[str, Any]:
        body_text = " ".join(t for t in current_body_parts if t)
        end_pid = (
            current_body_para_ids[-1]
            if current_body_para_ids
            else current_heading_para_id
        )
        return {
            "chunk_id": chunk_id,
            "heading_text": current_heading_text,
            "heading_path": list(current_heading_path),
            "heading_para_id": current_heading_para_id,
            "body_text": body_text,
            "start_para_id": current_start_para_id,
            "end_para_id": end_pid,
        }

    for para in paras:
        style = para.get("style")
        if _is_heading(style):
            # Flush the chunk that was accumulating (skip empty preamble).
            if current_heading_text or current_body_parts:
                chunks.append(_flush(len(chunks)))

            lvl = _heading_level(style)
            # Pop stack entries of equal or deeper level (>= lvl).
            while heading_stack and heading_stack[-1]["level"] >= lvl:
                heading_stack.pop()
            heading_stack.append({
                "level": lvl,
                "text": para.get("text", ""),
                "para_id": para.get("para_id"),
            })

            current_heading_text = para.get("text", "")
            current_heading_path = [h["text"] for h in heading_stack]
            current_heading_para_id = para.get("para_id")
            current_start_para_id = para.get("para_id")
            current_body_parts = []
            current_body_para_ids = []
        else:
            # Body paragraph: collect text.
            text = para.get("text", "")
            if text:
                current_body_parts.append(text)
            pid = para.get("para_id")
            current_body_para_ids.append(pid)
            if current_start_para_id is None:
                current_start_para_id = pid

    # Flush the last chunk.
    if current_heading_text or current_body_parts:
        chunks.append(_flush(len(chunks)))

    return chunks


def _connect_chunks(index_db_path: str) -> sqlite3.Connection:
    """Open/create the sidecar SQLite DB with chunk tables and FTS5 virtual table.

    Schema and trigger names match the canonical packages/docparse version
    exactly so sidecars created by either package are interchangeable.
    """
    conn = sqlite3.connect(index_db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS docx_chunks (
            chunk_id          INTEGER PRIMARY KEY,
            heading_path_json TEXT NOT NULL,
            heading_text      TEXT NOT NULL,
            body_text         TEXT NOT NULL,
            start_para_id     TEXT,
            end_para_id       TEXT
        )
        """
    )
    # FTS5 external-content table: two weighted columns, backed by docx_chunks.
    # content_rowid maps to docx_chunks.chunk_id (INTEGER PRIMARY KEY = rowid).
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS docx_chunks_fts
        USING fts5(
            heading_text,
            body_text,
            content='docx_chunks',
            content_rowid='chunk_id'
        )
        """
    )
    # Sync triggers: keep docx_chunks_fts consistent with docx_chunks rows.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS docx_chunks_ai
        AFTER INSERT ON docx_chunks BEGIN
            INSERT INTO docx_chunks_fts(rowid, heading_text, body_text)
            VALUES (new.chunk_id, new.heading_text, new.body_text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS docx_chunks_ad
        AFTER DELETE ON docx_chunks BEGIN
            INSERT INTO docx_chunks_fts(docx_chunks_fts, rowid, heading_text, body_text)
            VALUES ('delete', old.chunk_id, old.heading_text, old.body_text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS docx_chunks_au
        AFTER UPDATE ON docx_chunks BEGIN
            INSERT INTO docx_chunks_fts(docx_chunks_fts, rowid, heading_text, body_text)
            VALUES ('delete', old.chunk_id, old.heading_text, old.body_text);
            INSERT INTO docx_chunks_fts(rowid, heading_text, body_text)
            VALUES (new.chunk_id, new.heading_text, new.body_text);
        END
        """
    )
    return conn


def index_docx_chunks(
    source: str | bytes | bytearray, index_db_path: str
) -> dict[str, Any]:
    """c84ca127 -- build (or rebuild) the chunk-level heading-aware FTS5 index.

    Parses the .docx via :func:`parse_docx`, groups paragraphs into
    heading-anchored chunks via :func:`_build_chunks_from_paras`, stores them
    in ``docx_chunks``, and rebuilds ``docx_chunks_fts`` atomically.

    Scope note: chunks contain paragraph text only -- table cell content is not
    present because ``parse_docx()`` in this package collects only ``<w:p>``
    elements (not ``<w:tbl>`` siblings).  This is an honest scope difference
    from the packages/docparse canonical version.

    Returns ``{index_db, chunk_count}``.  Idempotent: the chunk table is fully
    replaced each run so re-indexing an edited document stays consistent.

    Args:
        source:        Path to the .docx file, or its raw bytes.
        index_db_path: Path to the sidecar SQLite DB (created if absent).

    Returns:
        ``{index_db: str, chunk_count: int}``
    """
    paras = parse_docx(source)
    chunks = _build_chunks_from_paras(paras)

    conn = _connect_chunks(index_db_path)
    try:
        conn.execute("DELETE FROM docx_chunks")
        conn.executemany(
            """
            INSERT OR REPLACE INTO docx_chunks
                (chunk_id, heading_path_json, heading_text, body_text,
                 start_para_id, end_para_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    c["chunk_id"],
                    json.dumps(c["heading_path"]),
                    c["heading_text"],
                    c["body_text"],
                    c["start_para_id"],
                    c["end_para_id"],
                )
                for c in chunks
            ],
        )
        # Full FTS5 rebuild from the current docx_chunks content.
        conn.execute("INSERT INTO docx_chunks_fts(docx_chunks_fts) VALUES ('rebuild')")
        conn.commit()
    finally:
        conn.close()

    return {"index_db": index_db_path, "chunk_count": len(chunks)}


def fts5_search_chunks(
    index_db_path: str,
    query: str,
    limit: int = 20,
    weight_heading: float = _CHUNK_WEIGHT_HEADING,
    weight_body: float = _CHUNK_WEIGHT_BODY,
) -> list[dict[str, Any]]:
    """c84ca127 -- BM25 search over the chunk-level heading-aware FTS5 index.

    Searches ``docx_chunks_fts`` (populated by :func:`index_docx_chunks`)
    with column weights so a term hit in a section heading outranks the same
    term in body prose.  Results carry ``heading_path`` so every hit reports
    which section it came from.

    Default weights: heading_text=5.0, body_text=1.0 (5:1 ratio; matches the
    canonical packages/docparse implementation).  The caller can override via
    keyword arguments.

    Args:
        index_db_path:   Path to the sidecar SQLite index built by
                         :func:`index_docx_chunks`.
        query:           FTS5 query string (e.g. ``"design"`` or
                         ``'"AI sessions"'`` for a phrase).
        limit:           Maximum number of results (default 20).
        weight_heading:  BM25 column weight for ``heading_text`` (default 5.0).
        weight_body:     BM25 column weight for ``body_text`` (default 1.0).

    Returns:
        List of dicts ordered by BM25 relevance (most relevant first)::

            {
                "chunk_id": int,
                "heading_path": list[str],   # section ancestry, root first
                "heading_text": str,
                "body_text": str,
                "start_para_id": str | None,
                "end_para_id": str | None,
                "bm25_score": float,         # negative; lower = more relevant
            }

        Empty list when no matches or when the FTS5 table does not exist yet
        (degrades gracefully rather than raising).
    """
    conn = _connect_chunks(index_db_path)
    try:
        sql = (
            "SELECT c.chunk_id, c.heading_path_json, c.heading_text, "
            "c.body_text, c.start_para_id, c.end_para_id, "
            f"bm25(docx_chunks_fts, {weight_heading!r}, {weight_body!r}) AS score "
            "FROM docx_chunks_fts "
            "JOIN docx_chunks c ON c.chunk_id = docx_chunks_fts.rowid "
            "WHERE docx_chunks_fts MATCH ? "
            "ORDER BY score "
            "LIMIT ?"
        )
        rows = conn.execute(sql, (query, int(limit))).fetchall()
    except sqlite3.OperationalError:
        # FTS5 table may not exist in an older sidecar or the query may be
        # syntactically invalid -- degrade to empty rather than raising.
        return []
    finally:
        conn.close()

    return [
        {
            "chunk_id": r[0],
            "heading_path": json.loads(r[1]) if r[1] else [],
            "heading_text": r[2],
            "body_text": r[3],
            "start_para_id": r[4],
            "end_para_id": r[5],
            "bm25_score": r[6],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# f1a92d6e -- 9 new primitives (sprint items 178a82dd/fea654f9/563118d4/
# 595ccea1/65c8eb31/82d22824/6ff24136/8213050a):
#
#   1. get_section_content   -- targeted single-section read (178a82dd)
#   2. find_references_to    -- inverse of insert_cross_reference (fea654f9)
#   3. scan_stale_notes      -- ad-hoc TODO/bracket-note detector (563118d4)
#   4. renumber_sequences    -- SEQ Figure/Table re-sync (595ccea1)
#   5. insert_highlighted_note / list_internal_notes -- structured, audit-
#      able author notes (65c8eb31)
#   6. write_section         -- atomic heading+body+refs section build (82d22824)
#   7. move_section           (6ff24136)
#   8. copy_section           (8213050a)
#
# Design notes (see server.py / task report for the full write-up):
#   - Everything here operates directly on word/document.xml via the same
#     _load_docx_xml_stdlib / _save_docx_xml_stdlib / _find_para_by_id /
#     _invalidate_sidecar_mtime primitives the caption/citation/equation/
#     bibliography write-back code above already uses -- no new I/O layer.
#   - move_section / copy_section / write_section all mint FRESH w14:paraId
#     values for any paragraph they create or duplicate (via _new_para_id)
#     rather than leaving Word to assign one on next open. Two paragraphs
#     sharing a paraId would silently break every paraId-addressed tool in
#     this file, so this is treated as a hard invariant, not an optimisation.
#   - copy_section additionally renames every bookmark name inside the
#     copied range (_Ref### caption cross-refs, _MNote### internal notes)
#     so REF fields resolve unambiguously -- see copy_section's docstring.
# ---------------------------------------------------------------------------

# 65c8eb31 -- internal-author-note paragraph type: a real highlighted run +
# a dedicated paragraph style name, wrapped in its own bookmark scheme
# (mirrors the _Ref<digits> scheme _next_ref_bookmark_name uses for
# captions). Deliberately NOT a Word Comment (comments live in a separate
# word/comments.xml part + [Content_Types].xml override + a document.xml.rels
# relationship; every other write-back function in this module only ever
# rewrites word/document.xml and preserves all other ZIP members byte-for-
# byte via _save_docx_xml_stdlib -- adding a new part would break that
# invariant and there is no test coverage for the extra plumbing). A
# highlighted run + distinct style is visually and structurally
# distinguishable and stays inside the existing single-part write model.
_INTERNAL_NOTE_STYLE_DEFAULT = "MeridianInternalNote"
_INTERNAL_NOTE_HIGHLIGHT_COLOR = "yellow"
_INTERNAL_NOTE_BOOKMARK_PREFIX = "_MNote"
_INTERNAL_NOTE_BOOKMARK_RE = re.compile(r"^_MNote(\d+)$")

# 563118d4 -- stale-note detection patterns. Deliberately broad: false
# positives (flagging real prose that happens to contain "TBD") are cheap for
# a human to dismiss during a pre-submission audit pass; false negatives (a
# genuinely stale placeholder note that ships in the final document) are the
# expensive failure mode this tool exists to catch.
_STALE_NOTE_RE = re.compile(
    r"\b(TODO|FIXME|XXX|TBD|PLACEHOLDER|DRAFT[- ]ONLY|NOTE\s+TO\s+SELF|"
    r"REMOVE\s+BEFORE\s+(?:SUBMISSION|DEFENSE|FINAL)|"
    r"PENDING\s+RELOCATION|CURRENTLY\s+PENDING|"
    r"(?:TO\s+BE|WILL\s+BE)\s+(?:MOVED|RELOCATED|UPDATED)|"
    r"TEMPORARILY\s+(?:LOCATED|HERE|PLACED)|"
    r"UNDER\s+CONSTRUCTION|COMING\s+SOON|"
    r"FOR\s+REVIEW\s+ONLY|INTERNAL\s+USE\s+ONLY)\b",
    re.IGNORECASE,
)
# A bracket/angle-bracket "header" line such as "[NOTE: ...]" or
# "<<TODO ...>>" or "**DRAFT**" left inline in prose -- the exact
# bracket-header anti-pattern insert_highlighted_note (65c8eb31) replaces
# with a real, structural note type.
_BRACKET_HEADER_RE = re.compile(r"^\s*[\[<]{1,2}\s*(NOTE|TODO|DRAFT|INTERNAL)\b", re.IGNORECASE)


def _existing_para_ids(root: ET.Element) -> set[str]:
    """Every native w14:paraId currently present anywhere in the document."""
    w14_paraId = _q(_W14, "paraId")
    return {
        pid
        for p in root.iter(_q(_W, "p"))
        if (pid := p.get(w14_paraId))
    }


def _new_para_id(taken: set[str]) -> str:
    """Mint a fresh w14:paraId (Word's own 8 hex-char format), reserving it in
    ``taken`` immediately so repeated calls within the same batch never
    collide with each other (not just with paraIds already on disk)."""
    while True:
        candidate = uuid.uuid4().hex[:8].upper()
        if candidate not in taken:
            taken.add(candidate)
            return candidate


def _next_note_bookmark_name(root: ET.Element) -> str:
    """Return the next unused ``_MNote<digits>`` internal-note bookmark name.

    Mirrors :func:`_next_ref_bookmark_name` but with its own numbering track
    so internal-note bookmarks never collide with caption cross-reference
    bookmarks even though both live in the same w:bookmarkStart namespace.
    """
    max_seen = 0
    for bm in root.iter(_q(_W, "bookmarkStart")):
        name = bm.get(_q(_W, "name")) or ""
        m = _INTERNAL_NOTE_BOOKMARK_RE.match(name)
        if m:
            max_seen = max(max_seen, int(m.group(1)))
    return f"{_INTERNAL_NOTE_BOOKMARK_PREFIX}{max_seen + 1}"


def _build_internal_note_paragraph(text: str, note_id: str, style: str) -> ET.Element:
    """Build a ``<w:p>`` for a highlighted internal-author-note paragraph.

    Produces a paragraph styled ``style`` (falls back to Normal rendering in
    Word if that style isn't defined in styles.xml -- the run-level
    ``w:highlight`` is what guarantees visible distinctiveness regardless),
    wrapped in a ``_MNote<digits>`` bookmark so :func:`list_internal_notes`
    and future tooling can locate it precisely instead of re-matching on text.
    """
    p = ET.Element(_q(_W, "p"))
    pPr = ET.SubElement(p, _q(_W, "pPr"))
    pStyle = ET.SubElement(pPr, _q(_W, "pStyle"))
    pStyle.set(_q(_W, "val"), style)

    bm_start = ET.SubElement(p, _q(_W, "bookmarkStart"))
    bm_start.set(_q(_W, "id"), "0")
    bm_start.set(_q(_W, "name"), note_id)

    r = ET.SubElement(p, _q(_W, "r"))
    rPr = ET.SubElement(r, _q(_W, "rPr"))
    highlight = ET.SubElement(rPr, _q(_W, "highlight"))
    highlight.set(_q(_W, "val"), _INTERNAL_NOTE_HIGHLIGHT_COLOR)
    t = ET.SubElement(r, _q(_W, "t"))
    t.set(_q(_XML_NS, "space"), "preserve")
    t.text = text

    bm_end = ET.SubElement(p, _q(_W, "bookmarkEnd"))
    bm_end.set(_q(_W, "id"), "0")
    return p


def _upsert_sidecar_note(index_db_path: str, note_id: str, text: str, anchor_para_id: str) -> None:
    """Best-effort record of a newly-inserted internal note into the sidecar.

    Mirrors :func:`_upsert_sidecar_caption` -- exceptions are swallowed so the
    caller's main result (the successful docx write) is unaffected.
    """
    try:
        conn = _connect(index_db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO docx_internal_notes (note_id, anchor_para_id, text) "
                "VALUES (?, ?, ?)",
                (note_id, anchor_para_id, text),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _locate_section_bounds(
    body: ET.Element, heading_id: str
) -> tuple[int, int, str, int] | None:
    """Find a heading paragraph's body-child index range for move/copy_section.

    Returns ``(start_idx, end_idx, heading_text, level)`` where ``start_idx``
    is the body-child index of the heading paragraph itself and ``end_idx``
    is the index of the first body child AFTER the section: the next heading
    at the same or shallower level, a body-level ``<w:sectPr>`` (the
    OOXML-mandated final body child -- never moved/copied into or out of
    position), or the end of the body.

    Works directly on the LIVE ``body`` element (not a fresh re-parse, unlike
    :func:`get_section_content`'s document_content_tree-based read) so the
    caller can cut/move/copy the exact same elements afterward without a
    second parse losing paraId/bookmark identity.

    6822b142 -- ``heading_id`` is resolved with the same three id schemes as
    :func:`_find_para_by_id` (native ``w14:paraId``, synthesized ``sp<hash>``
    via :func:`_vendored_content_tree._build_synth_id_map`, legacy ``p{N}``):
    this function has its own inline id-matching loop (it needs the live
    ``body`` element and heading level/text as it walks, which
    ``_find_para_by_id`` doesn't return), so it needs the same fix
    independently -- headings identified by a caller via their synth id
    (i.e. almost every real heading) would otherwise fail to resolve here
    even after ``_find_para_by_id`` learned about synth ids.

    Returns ``None`` when no heading paragraph with that para_id is found.
    """
    body_list = list(body)
    w_p = _q(_W, "p")
    w_sectPr = _q(_W, "sectPr")
    w_pPr = _q(_W, "pPr")
    w_pStyle = _q(_W, "pStyle")
    w_val = _q(_W, "val")
    w_t = _q(_W, "t")
    w14_paraId = _q(_W14, "paraId")

    from ._vendored_content_tree import _build_synth_id_map  # noqa: PLC0415

    synth_map = _build_synth_id_map(body)

    def _style_of(p: ET.Element) -> str | None:
        ppr = p.find(w_pPr)
        if ppr is None:
            return None
        ps = ppr.find(w_pStyle)
        return ps.get(w_val) if ps is not None else None

    global_p_idx = 0
    start_idx: int | None = None
    level = 1
    heading_text = ""

    for idx, child in enumerate(body_list):
        if child.tag == w_sectPr:
            if start_idx is not None:
                return (start_idx, idx, heading_text, level)
            continue
        if child.tag == w_p:
            real_id = child.get(w14_paraId)
            synth_id = synth_map.get(id(child))
            pid = real_id or synth_id or f"p{global_p_idx}"
            style = _style_of(child)
            if start_idx is None:
                if pid == heading_id and _is_heading(style):
                    start_idx = idx
                    level = _heading_level(style)
                    heading_text = "".join(t.text or "" for t in child.iter(w_t))
            elif _is_heading(style) and _heading_level(style) <= level:
                return (start_idx, idx, heading_text, level)
            global_p_idx += 1
        else:
            # Tables etc: not addressable as a heading themselves, but their
            # nested paragraphs (table cells) still consume synthetic p{N}
            # slots -- keep the counter aligned with _find_para_by_id's scheme.
            for _p in child.iter(w_p):
                global_p_idx += 1

    if start_idx is None:
        return None
    return (start_idx, len(body_list), heading_text, level)


def _iter_complex_fields(para_elem: ET.Element) -> list[dict[str, Any]]:
    """Generic Word complex-field scanner (begin/instrText/separate/cached/end).

    Unlike :func:`_scan_citation_field` (which looks for exactly one
    CSL_CITATION field and stops), this returns EVERY complex field in a
    paragraph regardless of instruction type, together with the actual
    ``instrText`` element(s) and the cached-display ``<w:t>`` element so a
    caller can rewrite them in place. Used by :func:`renumber_sequences`
    (to refresh REF fields' cached display text after a SEQ number changes)
    and :func:`copy_section` (to repoint an internal REF field at its
    duplicated bookmark).

    Returns a list of ``{instruction, instr_elements, display_run}`` dicts,
    where ``display_run`` is the ``<w:t>`` element holding the cached display
    text (``None`` if the field has no cached run).
    """
    children = list(para_elem)
    w_r = _q(_W, "r")
    w_fldChar = _q(_W, "fldChar")
    w_instrText = _q(_W, "instrText")
    w_fldCharType = _q(_W, "fldCharType")
    w_t = _q(_W, "t")

    fields: list[dict[str, Any]] = []
    i = 0
    while i < len(children):
        el = children[i]
        if el.tag == w_r:
            fc = el.find(w_fldChar)
            if fc is not None and fc.get(w_fldCharType) == "begin":
                j = i + 1
                instr_parts: list[str] = []
                instr_elements: list[ET.Element] = []
                display_run: ET.Element | None = None
                past_sep = False
                while j < len(children):
                    el2 = children[j]
                    if el2.tag == w_r:
                        fc2 = el2.find(w_fldChar)
                        if fc2 is not None:
                            ftype = fc2.get(w_fldCharType)
                            if ftype == "separate":
                                past_sep = True
                            elif ftype == "end":
                                fields.append({
                                    "instruction": "".join(instr_parts).strip(),
                                    "instr_elements": instr_elements,
                                    "display_run": display_run,
                                })
                                break
                        it = el2.find(w_instrText)
                        if it is not None and not past_sep:
                            instr_parts.append(it.text or "")
                            instr_elements.append(it)
                        if past_sep:
                            t_el = el2.find(w_t)
                            if t_el is not None:
                                display_run = t_el
                    j += 1
        i += 1
    return fields


def _rename_bookmark_for_copy(
    old_name: str,
    ref_seed: list[int],
    note_seed: list[int],
    fallback_seed: list[int],
) -> str:
    """Mint a fresh, unique bookmark name for a duplicated bookmark.

    Uses the SAME naming scheme as the original when recognised (``_Ref<n>``
    caption cross-references, ``_MNote<n>`` internal notes) so the copy's
    bookmarks remain indistinguishable in *shape* from natively-created ones;
    anything else (e.g. a ``bibkey_`` bibliography bookmark, or a hand-authored
    bookmark this module didn't itself create) gets a generic ``<name>_copyN``
    suffix -- unique, but deliberately NOT pretending to understand a naming
    scheme it doesn't own.
    """
    if _REF_BOOKMARK_RE.match(old_name):
        name = f"{_REF_BOOKMARK_PREFIX}{ref_seed[0]}"
        ref_seed[0] += 1
        return name
    if _INTERNAL_NOTE_BOOKMARK_RE.match(old_name):
        name = f"{_INTERNAL_NOTE_BOOKMARK_PREFIX}{note_seed[0]}"
        note_seed[0] += 1
        return name
    name = f"{old_name}_copy{fallback_seed[0]}"
    fallback_seed[0] += 1
    return name


# ---------------------------------------------------------------------------
# Public API 1/9: get_section_content (178a82dd)
# ---------------------------------------------------------------------------

def get_section_content(docx_path: str, heading_id: str) -> dict[str, Any]:
    """178a82dd -- targeted read of ONE section's content, without a full
    parse_document dump.

    A "section" is the heading paragraph at ``heading_id`` plus every block
    that follows it up to (not including) the next heading at the same or a
    shallower level, or the end of the document. Read-only; builds no index.
    This is the building block :func:`move_section` / :func:`copy_section`
    use to report what they moved/copied, and a light-weight alternative to
    calling :func:`parse_document` and filtering client-side when a caller
    only cares about one section.

    Args:
        docx_path:  Absolute path to the .docx file.
        heading_id: ``w14:paraId`` (or synthesised ``p{N}``) of the section's
                    OWN heading paragraph.

    Returns:
        ``{heading_id, heading_text, level, start_index, end_index, blocks,
        paragraph_count, table_count, figure_caption_count,
        table_caption_count, docx_path}`` where ``blocks`` is the ordered
        list of block dicts (``kind`` in ``"heading"``/``"paragraph"``/
        ``"table"``, same shape as :func:`document_content_tree`'s
        ``blocks``) from the heading itself through the end of the section.
        ``end_index`` is the document-order ``index`` of the first block
        NOT included (``None`` when the section runs to the end of the
        document).

        ``{"error": <message>}`` when ``docx_path`` cannot be read, or
        ``heading_id`` does not identify a heading paragraph.
    """
    try:
        _load_docx_xml_stdlib(docx_path)  # validates the file up front
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    from ._vendored_content_tree import document_content_tree  # noqa: PLC0415

    tree = document_content_tree(docx_path)
    blocks: list[dict[str, Any]] = tree.get("blocks") or []

    heading_pos: int | None = None
    for i, b in enumerate(blocks):
        if b.get("kind") == "heading" and b.get("para_id") == heading_id:
            heading_pos = i
            break
    if heading_pos is None:
        return {
            "error": (
                f"heading_id {heading_id!r} not found (or is not a heading "
                f"paragraph) in {docx_path}"
            )
        }

    heading_block = blocks[heading_pos]
    level = heading_block.get("level", 1)

    end_pos = len(blocks)
    for j in range(heading_pos + 1, len(blocks)):
        b = blocks[j]
        if b.get("kind") == "heading" and b.get("level", 1) <= level:
            end_pos = j
            break

    section_blocks = blocks[heading_pos:end_pos]

    return {
        "heading_id": heading_id,
        "heading_text": heading_block.get("text", ""),
        "level": level,
        "start_index": heading_block.get("index"),
        "end_index": blocks[end_pos]["index"] if end_pos < len(blocks) else None,
        "blocks": section_blocks,
        "paragraph_count": sum(1 for b in section_blocks if b.get("kind") in ("paragraph", "heading")),
        "table_count": sum(1 for b in section_blocks if b.get("kind") == "table"),
        "figure_caption_count": sum(
            1 for b in section_blocks if b.get("kind") == "paragraph" and _is_figure_caption(b)
        ),
        "table_caption_count": sum(
            1 for b in section_blocks if b.get("kind") == "paragraph" and _is_table_caption(b)
        ),
        "docx_path": docx_path,
    }


# ---------------------------------------------------------------------------
# Public API 2/9: find_references_to (fea654f9)
# ---------------------------------------------------------------------------

def find_references_to(docx_path: str, target_id: str) -> dict[str, Any]:
    """fea654f9 -- find everything that points AT a figure/table/heading id.

    The missing inverse of :func:`insert_cross_reference`: given a target
    (a Figure/Table caption's para_id, a heading's para_id, or an existing
    bookmark name directly, e.g. ``"_Ref123456789"``), scans the whole
    document for REF / PAGEREF / NOTEREF fields whose instruction targets
    that same bookmark, so a caller can check "is anything pointing at this
    before I move/renumber/delete it".

    ``target_id`` resolution (exactly one of these applies):
      - Already a bookmark name (matches ``_Ref<digits>`` or starts with
        ``bibkey_``): used directly, after confirming it exists somewhere.
      - A Figure/Table Caption paragraph's para_id: resolved to its
        ``_Ref<digits>`` cross-reference bookmark (the one
        :func:`insert_caption` / :func:`insert_cross_reference` create/use).
      - Any other paragraph's para_id (e.g. a heading): every
        ``w:bookmarkStart`` name found directly on that paragraph is used
        (covers manually-bookmarked headings; there is no automatic
        heading-bookmark mechanism elsewhere in this module).

    This is read-only -- it never mutates ``docx_path`` and never retrofits a
    missing bookmark (unlike :func:`insert_cross_reference`, which creates
    one when the target caption predates cross-reference support).

    Args:
        docx_path: Absolute path to the .docx file.
        target_id: A caption/heading para_id, or an existing bookmark name.

    Returns:
        ``{target_id, target_kind, bookmark_names, references,
        reference_count, docx_path}`` where each entry in ``references`` is
        ``{para_id, index, field_type, bookmark_name, display_text,
        paragraph_text}``.

        ``{"error": <message>}`` when ``docx_path`` cannot be read, or
        ``target_id`` cannot be resolved to anything in the document.
    """
    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    if body is None:
        return {"error": "document has no body element"}

    bookmark_names: list[str] = []
    target_kind = "bookmark"

    if _REF_BOOKMARK_RE.match(target_id) or target_id.startswith(_BIBKEY_BOOKMARK_PREFIX) \
            or _INTERNAL_NOTE_BOOKMARK_RE.match(target_id):
        found_directly = any(
            bm.get(_q(_W, "name")) == target_id for bm in body.iter(_q(_W, "bookmarkStart"))
        )
        if not found_directly:
            return {"error": f"bookmark {target_id!r} not found in {docx_path}"}
        bookmark_names.append(target_id)
    else:
        result = _find_para_by_id(root, target_id)
        if result is None:
            return {"error": f"para_id {target_id!r} not found in {docx_path}"}
        _body, target_elem, _idx = result

        kind_seq = _caption_kind_and_seq(target_elem)
        if kind_seq is not None:
            target_kind = kind_seq[0]
            ref_name = _find_caption_ref_bookmark(target_elem)
            if ref_name:
                bookmark_names.append(ref_name)

        for bm in target_elem.iter(_q(_W, "bookmarkStart")):
            name = bm.get(_q(_W, "name")) or ""
            if name and name not in bookmark_names:
                bookmark_names.append(name)

    if not bookmark_names:
        return {
            "target_id": target_id,
            "target_kind": target_kind,
            "bookmark_names": [],
            "references": [],
            "reference_count": 0,
            "note": (
                "target paragraph carries no bookmark, so no REF/PAGEREF field "
                "could possibly point at it yet -- nothing to find. If this is "
                "a Figure/Table caption that predates cross-reference support, "
                "call insert_cross_reference once (it retrofits a bookmark) "
                "and re-run find_references_to."
            ),
            "docx_path": docx_path,
        }

    from ._vendored_content_tree import document_content_tree  # noqa: PLC0415

    tree = document_content_tree(docx_path)
    blocks: list[dict[str, Any]] = tree.get("blocks") or []

    references: list[dict[str, Any]] = []
    for block in blocks:
        for fld in block.get("fields", []):
            ftype = fld.get("field_type")
            if ftype not in ("REF", "PAGEREF", "NOTEREF"):
                continue
            parts = (fld.get("instruction") or "").split()
            bm_target = parts[1] if len(parts) > 1 else None
            if bm_target in bookmark_names:
                references.append({
                    "para_id": block.get("para_id"),
                    "index": block.get("index"),
                    "field_type": ftype,
                    "bookmark_name": bm_target,
                    "display_text": fld.get("cached_result"),
                    "paragraph_text": block.get("text", ""),
                })

    return {
        "target_id": target_id,
        "target_kind": target_kind,
        "bookmark_names": bookmark_names,
        "references": references,
        "reference_count": len(references),
        "docx_path": docx_path,
    }


# ---------------------------------------------------------------------------
# Public API 3/9: scan_stale_notes (563118d4)
# ---------------------------------------------------------------------------

def scan_stale_notes(docx_path: str) -> dict[str, Any]:
    """563118d4 -- scan a .docx for placeholder/TODO-shaped text that may now
    be outdated.

    Recurring pattern this catches: an ad-hoc bracket-header or inline note
    like ``"[NOTE: currently pending relocation to Section 4]"`` that never
    got removed/updated after the thing it describes actually happened (the
    section WAS moved, the note wasn't). This is a plain-text regex scan --
    it has no notion of "did the described event actually occur"; it flags
    every paragraph that LOOKS like a stale/placeholder note so a human can
    triage the list before final submission.

    Paragraphs already using the structured internal-note style (written by
    :func:`insert_highlighted_note`) are excluded -- those are already
    tracked/auditable via :func:`list_internal_notes` and are not the ad-hoc
    pattern this function targets.

    Args:
        docx_path: Absolute path to the .docx file.

    Returns:
        ``{docx_path, findings, finding_count}`` where each finding is
        ``{para_id, index, text, matched_terms, bracket_header,
        section_path}`` -- ``section_path`` is the ancestor heading text
        stack (root first) so a hit can be located without re-opening the
        whole document.

        ``{"error": <message>}`` when ``docx_path`` cannot be read.
    """
    try:
        _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    paras = parse_docx(docx_path)

    heading_stack: list[dict[str, Any]] = []  # {level, text}
    findings: list[dict[str, Any]] = []

    for p in paras:
        style = p.get("style")
        text = p.get("text") or ""

        if _is_heading(style):
            lvl = _heading_level(style)
            while heading_stack and heading_stack[-1]["level"] >= lvl:
                heading_stack.pop()
            heading_stack.append({"level": lvl, "text": text})
            continue

        if style == _INTERNAL_NOTE_STYLE_DEFAULT:
            continue

        matched_terms = sorted({m.group(0) for m in _STALE_NOTE_RE.finditer(text)})
        bracket_header = bool(_BRACKET_HEADER_RE.match(text))
        if not matched_terms and not bracket_header:
            continue

        findings.append({
            "para_id": p.get("para_id"),
            "index": p.get("index"),
            "text": text,
            "matched_terms": matched_terms,
            "bracket_header": bracket_header,
            "section_path": [h["text"] for h in heading_stack],
        })

    return {
        "docx_path": docx_path,
        "findings": findings,
        "finding_count": len(findings),
    }


# ---------------------------------------------------------------------------
# Public API 4/9: renumber_sequences (595ccea1)
# ---------------------------------------------------------------------------

def renumber_sequences(docx_path: str, index_db_path: str | None = None) -> dict[str, Any]:
    """595ccea1 -- re-scan every SEQ Figure / SEQ Table field and confirm/fix
    sequential numbering.

    Motivated directly by a real Figure 41/42 numbering collision found by
    hand after a structural move: two captions ended up caching the same
    number because whatever moved them didn't re-derive numbering from
    actual document order afterward. This walks the document ONCE in true
    body order (including captions nested in table cells), computes the
    correct 1-based number for each kind (Figure / Table counted
    independently, matching :func:`insert_caption`'s own counting rule), and
    rewrites any cached SEQ number that doesn't match -- Word will confirm
    the same numbers on its own next field refresh (F9), so this is
    "pre-computing" that refresh rather than fighting it.

    Any REF field elsewhere in the document that caches the OLD "<Kind> <N>"
    display text for a caption whose number this call corrects is ALSO
    updated to the new text, so cross-references don't silently show a
    stale number until the next manual Word field refresh.

    A first-class primitive (not a private helper) precisely so
    :func:`move_section` and :func:`copy_section` can call into it rather
    than duplicate renumbering logic, per the sprint note.

    Args:
        docx_path:     Absolute path to the .docx file (mutated in place --
                       only if a correction is actually needed).
        index_db_path: If supplied, sidecar is invalidated after a write.

    Returns:
        ``{status, figure_count, table_count, collisions_found, corrections,
        ref_fields_updated, docx_path}``. ``status`` is ``"unchanged"`` when
        every cached number was already correct (no write performed) or
        ``"corrected"`` otherwise. ``collisions_found`` lists any TWO
        captions of the same kind that cached the identical number BEFORE
        this call fixed them (the exact class of bug that motivated this
        tool). ``{"error": <message>}`` on failure.
    """
    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    if body is None:
        return {"error": "document has no body element"}

    w_p = _q(_W, "p")
    w_fldSimple = _q(_W, "fldSimple")
    w_instr = _q(_W, "instr")
    w_t = _q(_W, "t")

    seq_fields: dict[str, list[dict[str, Any]]] = {"Figure": [], "Table": []}
    for p in body.iter(w_p):
        for fld in p.findall(w_fldSimple):
            instr = fld.get(w_instr) or ""
            if _SEQ_FIGURE_RE.search(instr):
                kind = "Figure"
            elif _SEQ_TABLE_RE.search(instr):
                kind = "Table"
            else:
                continue
            # The cached number lives on a <w:t> NESTED inside a <w:r> child
            # of fldSimple (see _build_caption_paragraph), not a direct child
            # of fldSimple itself -- must search descendants, not .find().
            t_el = next(iter(fld.iter(w_t)), None)
            if t_el is None:
                r_new = ET.SubElement(fld, _q(_W, "r"))
                t_el = ET.SubElement(r_new, w_t)
            seq_fields[kind].append({"para": p, "t_el": t_el, "cached": t_el.text or ""})

    corrections: list[dict[str, Any]] = []
    collisions: list[dict[str, Any]] = []

    for kind, entries in seq_fields.items():
        seen_numbers: dict[str, int] = {}
        for n, entry in enumerate(entries, start=1):
            cached = entry["cached"]
            if cached in seen_numbers:
                collisions.append({
                    "kind": kind,
                    "cached_number": cached,
                    "occurrence_positions": [seen_numbers[cached], n],
                })
            else:
                seen_numbers[cached] = n
            expected = str(n)
            if cached != expected:
                bookmark = _find_caption_ref_bookmark(entry["para"])
                corrections.append({
                    "kind": kind,
                    "position": n,
                    "old_cached": cached,
                    "new_cached": expected,
                    "ref_bookmark": bookmark,
                })
                entry["t_el"].text = expected

    if not corrections:
        return {
            "status": "unchanged",
            "figure_count": len(seq_fields["Figure"]),
            "table_count": len(seq_fields["Table"]),
            "collisions_found": collisions,
            "corrections": [],
            "ref_fields_updated": 0,
            "docx_path": docx_path,
        }

    # Propagate corrected numbers into any REF field elsewhere caching the
    # OLD "<Kind> <N>" display text for a corrected bookmark.
    updated_display: dict[str, str] = {
        c["ref_bookmark"]: f"{c['kind']} {c['new_cached']}"
        for c in corrections
        if c["ref_bookmark"]
    }
    ref_updates = 0
    if updated_display:
        for p in body.iter(w_p):
            for fld in _iter_complex_fields(p):
                parts = fld["instruction"].split()
                if len(parts) < 2 or parts[0].upper() != "REF":
                    continue
                new_text = updated_display.get(parts[1])
                if new_text is not None and fld["display_run"] is not None:
                    fld["display_run"].text = new_text
                    ref_updates += 1

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "corrected",
        "figure_count": len(seq_fields["Figure"]),
        "table_count": len(seq_fields["Table"]),
        "collisions_found": collisions,
        "corrections": corrections,
        "ref_fields_updated": ref_updates,
        "docx_path": docx_path,
    }


# ---------------------------------------------------------------------------
# Public API 5/9: insert_highlighted_note + list_internal_notes (65c8eb31)
# ---------------------------------------------------------------------------

def insert_highlighted_note(
    docx_path: str,
    text: str,
    anchor_para_id: str,
    position: str = "after",
    style: str = "internal_note",
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """65c8eb31 -- insert a genuinely highlighted internal-author-note paragraph.

    Addresses a real recurring pattern: bracket-header/NOTE-block text
    (``"[NOTE: ...]"``) left inline in results-section prose, indistinguishable
    from real dissertation content until a human re-reads every paragraph
    looking for it. This writes a STRUCTURALLY distinct paragraph instead --
    a real ``w:highlight`` run property (renders visibly highlighted in Word
    regardless of whether the ``style`` name below is defined in styles.xml)
    plus a dedicated paragraph style name and its own ``_MNote<digits>``
    bookmark -- so notes can be found and stripped programmatically (see
    :func:`list_internal_notes` and :func:`scan_stale_notes`) before final
    submission, rather than grepped for by hoping the author's bracket
    convention was followed consistently.

    ``text`` should be the note's plain content -- no bracket/NOTE-prefix
    decoration needed; the highlight + dedicated style ARE the signal.

    Args:
        docx_path:      Absolute path to the .docx file (mutated in place).
        text:            Note content.
        anchor_para_id:  w14:paraId (or p{N}) of the paragraph to anchor on.
        position:        "before" or "after" (default) the anchor.
        style:           Must be ``"internal_note"`` -- the only supported
                         note style today. Present as an explicit parameter
                         (rather than hard-coded) so a future note *kind*
                         (e.g. a reviewer-question style distinct from an
                         author-note style) can be added without an API
                         break.
        index_db_path:   If supplied, the note is ALSO recorded in the
                         sidecar's docx_internal_notes table so
                         :func:`list_internal_notes` can find it. Without
                         it the note still exists in the .docx (findable via
                         :func:`scan_stale_notes`'s style exclusion, or by
                         its ``MeridianInternalNote`` paragraph style /
                         ``_MNote`` bookmark directly) but won't show up in
                         a sidecar-backed audit query -- see the "risks"
                         section of the accompanying task report.

    Returns:
        ``{status, note_id, text, anchor_para_id, position, style, docx_path}``
        or ``{"error": <message>}`` on failure (file NOT mutated on error).
    """
    if not text or not str(text).strip():
        return {"error": "text must be a non-empty string"}
    if position not in ("before", "after"):
        return {"error": f"position must be 'before' or 'after', got {position!r}"}
    if style != "internal_note":
        return {"error": f"style must be 'internal_note' (the only supported note style), got {style!r}"}

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    result = _find_para_by_id(root, anchor_para_id)
    if result is None:
        return {"error": f"para_id {anchor_para_id!r} not found in {docx_path}"}
    body, _anchor_elem, child_idx = result

    note_id = _next_note_bookmark_name(root)
    note_p = _build_internal_note_paragraph(text.strip(), note_id, _INTERNAL_NOTE_STYLE_DEFAULT)

    insert_at = child_idx if position == "before" else child_idx + 1
    body.insert(insert_at, note_p)

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    if index_db_path and os.path.exists(index_db_path):
        _upsert_sidecar_note(index_db_path, note_id, text.strip(), anchor_para_id)

    return {
        "status": "inserted",
        "note_id": note_id,
        "text": text.strip(),
        "anchor_para_id": anchor_para_id,
        "position": position,
        "style": _INTERNAL_NOTE_STYLE_DEFAULT,
        "docx_path": docx_path,
    }


def list_internal_notes(index_db_path: str) -> list[dict[str, Any]]:
    """65c8eb31 -- list internal-author-note paragraphs recorded in the sidecar.

    Reads the ``docx_internal_notes`` table populated by
    :func:`insert_highlighted_note`. This is a sidecar QUERY (matching the
    convention of :func:`get_equations` / :func:`get_local_structure_elements`
    in this module) -- it reports notes recorded at insertion time, not a
    live re-scan of the .docx. A note inserted without ``index_db_path`` set
    will NOT appear here even though it really exists in the document (see
    the caveat on :func:`insert_highlighted_note`); use
    :func:`scan_stale_notes`'s style-aware exclusion, or a direct structural
    scan for the ``MeridianInternalNote`` paragraph style, as a live
    cross-check before treating this list as exhaustive.

    Args:
        index_db_path: Path to the sidecar SQLite index.

    Returns:
        A list of ``{note_id, anchor_para_id, text}`` dicts. ``[]`` when the
        sidecar doesn't exist yet or has no recorded notes.
    """
    if not os.path.exists(index_db_path):
        return []
    conn = _connect(index_db_path)
    try:
        rows = conn.execute(
            "SELECT note_id, anchor_para_id, text FROM docx_internal_notes ORDER BY note_id"
        ).fetchall()
    finally:
        conn.close()
    return [{"note_id": r[0], "anchor_para_id": r[1], "text": r[2]} for r in rows]


# ---------------------------------------------------------------------------
# Public API 6/9: write_section (82d22824)
# ---------------------------------------------------------------------------

def write_section(
    docx_path: str,
    heading_text: str,
    level: int,
    content_spec: list[dict[str, Any]],
    anchor_para_id: str,
    position: str = "after",
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """82d22824 -- create a whole new section (heading + body + figure/table
    references) as ONE atomic operation from a structured spec.

    Replaces the failure-prone pattern of separate insert_caption /
    insert_cross_reference / raw-paragraph calls that can each independently
    fail or land at the wrong position: every block is built in memory first
    and validated BEFORE any XML is touched, then spliced into the document
    in a single write. Either the whole section lands correctly, or the file
    is not modified at all.

    ``content_spec`` is an ordered list of block dicts, each with a ``type``:

      ``{"type": "paragraph", "text": str, "references": [ref_spec, ...]}``
        A plain body paragraph. Each optional ``ref_spec`` is
        ``{"target_caption_para_id": ...}`` or ``{"bookmark_name": ...}``
        (exactly one) and appends a live REF cross-reference field at the
        end of the paragraph -- the same mechanism as
        :func:`insert_cross_reference` -- so "as shown in Figure 3" prose
        can be authored as part of the new section instead of a separate
        follow-up call.

      ``{"type": "caption", "kind": "Figure"|"Table", "label_text": str}``
        A Caption-styled paragraph with its own SEQ field, numbered by
        counting existing same-kind captions already in the document (same
        rule :func:`insert_caption` uses, extended across this whole batch
        so two captions of the same kind in one ``content_spec`` get
        consecutive numbers). NOTE: this module has no image/table
        INSERTION primitive -- this block type declares a caption's
        position; the caller is responsible for placing the actual
        image/table itself (e.g. via a separate tool), same as
        :func:`insert_caption` already requires today.

    Every paragraph created here (the heading included) is assigned a fresh
    ``w14:paraId`` immediately via :func:`_new_para_id` rather than leaving
    Word to assign one on next open, so the returned para_ids are usable
    right away by :func:`insert_cross_reference` / :func:`find_references_to`
    / :func:`move_section` without a save-reload round trip.

    Args:
        docx_path:      Absolute path to the .docx file (mutated in place).
        heading_text:   Text of the new section's heading.
        level:          Heading level (1 = Heading1, 2 = Heading2, ...).
        content_spec:   Ordered list of block specs (see above).
        anchor_para_id: w14:paraId (or synthesized/legacy id, same schemes
                        :func:`_find_para_by_id` accepts) of the paragraph/
                        table/heading to anchor on.
        position:       "before" or "after" (default) the anchor.

                        6822b142 -- when ``anchor_para_id`` is itself a
                        HEADING paragraph and ``position="after"``, this
                        lands the new section after that heading's ENTIRE
                        existing section (its own body paragraphs and any
                        subsections), not immediately after the heading
                        paragraph itself. A literal "next body child" splice
                        there would insert the new section's heading between
                        the anchor heading and its own body, which silently
                        re-parents that pre-existing content under the new
                        heading on the next read (sections are delimited by
                        document order + heading level, not authorship) --
                        i.e. it would look like the anchor heading's content
                        had vanished. There is no ambiguity for "before" (an
                        anchor heading, or any other anchor kind) or for
                        "after" a non-heading anchor -- both remain a literal
                        splice at that exact position.
        index_db_path:  If supplied, sidecar is invalidated after the write.

    Returns:
        ``{status, heading_para_id, heading_text, level, block_para_ids,
        docx_path}`` where ``block_para_ids`` is a list parallel to
        ``content_spec`` (``{"type": "paragraph", "para_id": ...}`` or
        ``{"type": "caption", "kind", "para_id", "seq_number",
        "ref_bookmark"}``).

        ``{"error": <message>}`` on any validation or write failure (file
        NOT mutated on error -- validation happens before the file is
        touched).
    """
    if not heading_text or not str(heading_text).strip():
        return {"error": "heading_text must be a non-empty string"}
    try:
        level_int = int(level)
    except (TypeError, ValueError):
        return {"error": f"level must be an integer, got {level!r}"}
    if level_int < 1:
        return {"error": f"level must be >= 1, got {level_int}"}
    if position not in ("before", "after"):
        return {"error": f"position must be 'before' or 'after', got {position!r}"}
    if not isinstance(content_spec, list):
        return {"error": "content_spec must be a list of block specs"}
    for i, block in enumerate(content_spec):
        if not isinstance(block, dict) or "type" not in block:
            return {"error": f"content_spec[{i}] must be a dict with a 'type' key"}
        if block["type"] not in ("paragraph", "caption"):
            return {"error": f"content_spec[{i}]['type'] must be 'paragraph' or 'caption', got {block['type']!r}"}
        if block["type"] == "paragraph" and not str(block.get("text", "")).strip():
            return {"error": f"content_spec[{i}] (paragraph) requires a non-empty 'text'"}
        if block["type"] == "caption":
            if block.get("kind") not in ("Figure", "Table"):
                return {"error": f"content_spec[{i}] (caption) requires kind='Figure' or 'Table'"}
            if not str(block.get("label_text", "")).strip():
                return {"error": f"content_spec[{i}] (caption) requires a non-empty 'label_text'"}

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    result = _find_para_by_id(root, anchor_para_id)
    if result is None:
        return {"error": f"para_id {anchor_para_id!r} not found in {docx_path}"}
    body, _anchor_elem, child_idx = result

    # 6822b142 -- resolve "after this section" semantics when anchor_para_id
    # is itself a heading. A literal child_idx + 1 splice would land the new
    # section between the anchor heading and that heading's OWN body/
    # subsections, which silently re-parents that pre-existing content under
    # the newly-inserted heading on the very next read (get_section_content /
    # document_content_tree group body blocks by the nearest PRECEDING
    # heading in document order, not by who authored them) -- i.e. from the
    # anchor heading's perspective its own content just vanished. Anchoring
    # "before" a heading has no such ambiguity (nothing of the anchor's own
    # is between "before it" and the heading itself), so it is unchanged.
    # _locate_section_bounds only matches when anchor_para_id resolves to a
    # HEADING paragraph (returns None otherwise), so this is a safe no-op for
    # every other anchor kind (plain paragraph, caption, table).
    section_bounds = _locate_section_bounds(body, anchor_para_id) if position == "after" else None

    taken_ids = _existing_para_ids(root)

    # Newly-created captions in THIS batch aren't attached to `root` until
    # the final splice below, so _next_ref_bookmark_name(root) alone would
    # hand out the SAME name twice for two captions in one content_spec.
    # Reserve names from a local monotonic seed instead.
    seed_match = _REF_BOOKMARK_RE.match(_next_ref_bookmark_name(root))
    ref_seed = [int(seed_match.group(1))]

    def _reserve_ref_bookmark() -> str:
        name = f"{_REF_BOOKMARK_PREFIX}{ref_seed[0]}"
        ref_seed[0] += 1
        return name

    heading_id = _new_para_id(taken_ids)
    heading_p = ET.Element(_q(_W, "p"))
    heading_p.set(_q(_W14, "paraId"), heading_id)
    hPr = ET.SubElement(heading_p, _q(_W, "pPr"))
    hStyle = ET.SubElement(hPr, _q(_W, "pStyle"))
    hStyle.set(_q(_W, "val"), f"Heading{level_int}")
    hr = ET.SubElement(heading_p, _q(_W, "r"))
    ht = ET.SubElement(hr, _q(_W, "t"))
    ht.text = heading_text.strip()

    new_elements: list[ET.Element] = [heading_p]
    block_para_ids: list[dict[str, Any]] = []

    fig_seq = _count_seq_captions(root, "Figure")
    tbl_seq = _count_seq_captions(root, "Table")

    for block in content_spec:
        if block["type"] == "paragraph":
            pid = _new_para_id(taken_ids)
            p = ET.Element(_q(_W, "p"))
            p.set(_q(_W14, "paraId"), pid)
            r = ET.SubElement(p, _q(_W, "r"))
            t = ET.SubElement(r, _q(_W, "t"))
            t.set(_q(_XML_NS, "space"), "preserve")
            t.text = str(block["text"])

            for ref_spec in block.get("references") or []:
                target_pid = ref_spec.get("target_caption_para_id")
                bm_name = ref_spec.get("bookmark_name")
                if bool(target_pid) == bool(bm_name):
                    return {
                        "error": (
                            "each entry in a paragraph block's 'references' must give "
                            "exactly one of target_caption_para_id or bookmark_name"
                        )
                    }
                if target_pid is not None:
                    target_result = _find_para_by_id(root, target_pid)
                    if target_result is None:
                        return {"error": f"target_caption_para_id {target_pid!r} not found in {docx_path}"}
                    _tb, caption_elem, _ti = target_result
                    kind_seq = _caption_kind_and_seq(caption_elem)
                    if kind_seq is None:
                        return {
                            "error": f"paragraph {target_pid!r} is not a Figure/Table Caption paragraph"
                        }
                    kind, seq_cached = kind_seq
                    ref_name = _find_caption_ref_bookmark(caption_elem)
                    if ref_name is None:
                        ref_name = _reserve_ref_bookmark()
                        _wrap_caption_in_ref_bookmark(caption_elem, ref_name)
                else:
                    found = _find_caption_by_ref_bookmark(root, bm_name)
                    if found is None:
                        return {
                            "error": (
                                f"bookmark {bm_name!r} not found (or not a caption "
                                "cross-reference bookmark)"
                            )
                        }
                    _ce, kind_seq = found
                    kind, seq_cached = kind_seq
                    ref_name = bm_name

                display_text = f"{kind} {seq_cached}"
                existing_texts = [tt.text or "" for tt in p.iter(_q(_W, "t"))]
                trailing = existing_texts[-1] if existing_texts else ""
                if trailing and not trailing[-1].isspace():
                    r_sp = ET.SubElement(p, _q(_W, "r"))
                    t_sp = ET.SubElement(r_sp, _q(_W, "t"))
                    t_sp.set(_q(_XML_NS, "space"), "preserve")
                    t_sp.text = " "
                for run_el in _build_complex_field_runs(f"REF {ref_name} \\h", display_text):
                    p.append(run_el)

            new_elements.append(p)
            block_para_ids.append({"type": "paragraph", "para_id": pid})
        else:  # "caption"
            kind = block["kind"]
            if kind == "Figure":
                fig_seq += 1
                seq_number = fig_seq
            else:
                tbl_seq += 1
                seq_number = tbl_seq
            ref_bookmark = _reserve_ref_bookmark()
            cap_p = _build_caption_paragraph(
                kind=kind,
                label_text=str(block["label_text"]).strip(),
                seq_cached=str(seq_number),
                ref_bookmark=ref_bookmark,
            )
            pid = _new_para_id(taken_ids)
            cap_p.set(_q(_W14, "paraId"), pid)
            new_elements.append(cap_p)
            block_para_ids.append({
                "type": "caption",
                "kind": kind,
                "para_id": pid,
                "seq_number": seq_number,
                "ref_bookmark": ref_bookmark,
            })

    if position == "before":
        insert_at = child_idx
    elif section_bounds is not None:
        # anchor_para_id is a heading -- land after its entire section
        # (including any subsections), not right after the heading paragraph
        # itself. See the section_bounds comment above for why the literal
        # child_idx + 1 splice is unsafe here.
        _anchor_start_idx, anchor_end_idx, _anchor_heading_text, _anchor_level = section_bounds
        insert_at = anchor_end_idx
    else:
        insert_at = child_idx + 1
    for offset, el in enumerate(new_elements):
        body.insert(insert_at + offset, el)

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "inserted",
        "heading_para_id": heading_id,
        "heading_text": heading_text.strip(),
        "level": level_int,
        "block_para_ids": block_para_ids,
        "docx_path": docx_path,
    }


# ---------------------------------------------------------------------------
# Public API 7/9: move_section (6ff24136)
# ---------------------------------------------------------------------------

def _bookmarks_split_by_range(
    body_list: list[ET.Element], start_idx: int, end_idx: int
) -> list[str]:
    """e87b8338 -- names of any bookmark whose ``w:bookmarkStart``/
    ``w:bookmarkEnd`` pair would end up on OPPOSITE sides of the
    ``[start_idx, end_idx)`` body-child boundary.

    A Word bookmark can validly span multiple paragraphs (bookmarkStart in
    one paragraph, bookmarkEnd many paragraphs later) -- e.g. a manually
    bookmarked range covering a heading plus some of its neighbours. Moving
    exactly the ``[start_idx, end_idx)`` slice while leaving the rest of the
    document in place would tear such a bookmark's start and end apart into
    two disconnected locations, which is the one thing about a move that is
    genuinely, structurally broken (as opposed to REF/PAGEREF/NOTEREF fields
    targeting a bookmark by name, which stay valid regardless of where in
    the document the bookmark now lives).

    ``w:id`` (not ``w:name`` -- ``bookmarkEnd`` carries no name) pairs each
    bookmarkStart with its bookmarkEnd; ``w:id`` is only required to be
    unique within one bookmarkStart/bookmarkEnd pair, so this pairs by id
    within the whole document, matching how Word itself resolves the range.
    """
    w_bookmarkStart = _q(_W, "bookmarkStart")
    w_bookmarkEnd = _q(_W, "bookmarkEnd")
    w_id = _q(_W, "id")
    w_name = _q(_W, "name")

    start_positions: dict[str, int] = {}
    end_positions: dict[str, int] = {}
    names: dict[str, str] = {}
    for idx, child in enumerate(body_list):
        for bm in child.iter(w_bookmarkStart):
            bm_id = bm.get(w_id)
            if bm_id is not None:
                start_positions[bm_id] = idx
                names[bm_id] = bm.get(w_name) or bm_id
        for bm in child.iter(w_bookmarkEnd):
            bm_id = bm.get(w_id)
            if bm_id is not None:
                end_positions[bm_id] = idx

    split_names: list[str] = []
    for bm_id, s_idx in start_positions.items():
        e_idx = end_positions.get(bm_id)
        if e_idx is None:
            continue  # unmatched bookmarkStart -- not this check's concern
        s_inside = start_idx <= s_idx < end_idx
        e_inside = start_idx <= e_idx < end_idx
        if s_inside != e_inside:
            split_names.append(names[bm_id])
    return split_names


def move_section(
    docx_path: str,
    section_id: str,
    destination_anchor_para_id: str,
    destination_position: str = "after",
    index_db_path: str | None = None,
    allow_bookmark_split: bool = False,
) -> dict[str, Any]:
    """6ff24136 -- move an existing section (heading + its content) to a new
    location in the document.

    Cuts the heading at ``section_id`` and every block up to (not including)
    the next same-or-shallower heading (see :func:`_locate_section_bounds`
    -- the same boundary rule :func:`get_section_content` reports), then
    re-inserts that exact same range of elements relative to
    ``destination_anchor_para_id``. Operates on a single live parse so every
    paragraph keeps its original ``w14:paraId`` and every bookmark keeps its
    original name -- existing cross-references INTO the moved section stay
    valid (they don't care where in the document their target lives).

    e87b8338 -- the reference-safety check runs and GATES the operation
    BEFORE anything is cut/spliced/saved, not after: this used to call
    :func:`find_references_to` for ``section_id`` only after the mutated
    document was already written to disk, which made it a post-hoc report
    that could not actually prevent anything -- by the time a problem was
    visible, it was already permanent. Now, before any mutation:
      1. :func:`find_references_to` for ``section_id`` runs against the
         still-intact file (same info either way -- REF/PAGEREF/NOTEREF
         fields resolve by bookmark name, unaffected by the section's
         position -- but a failure here now aborts cleanly with the file
         untouched instead of surfacing after the write).
      2. :func:`_bookmarks_split_by_range` checks whether the move would tear
         apart any bookmark that spans the ``[start_idx, end_idx)`` boundary
         (start inside the moved section, end outside, or vice versa) --
         this IS a genuinely broken bookmark (REF-by-name safety does not
         cover it). If any are found, the move is aborted with a clear error
         UNLESS the caller passes ``allow_bookmark_split=True``.

    :func:`renumber_sequences` still runs AFTER the write (it must -- it
    reads the moved document's new SEQ ordering from disk) -- that part of
    the post-move follow-up is unchanged.

    Args:
        docx_path:                   Absolute path to the .docx file
                                      (mutated in place).
        section_id:                  w14:paraId (or p{N}) of the section's
                                      OWN heading paragraph.
        destination_anchor_para_id:  w14:paraId (or synthesized/legacy id,
                                      same schemes :func:`_find_para_by_id`
                                      accepts) of the paragraph/table/heading
                                      to move the section next to. Must be
                                      OUTSIDE the section being moved.
        destination_position:        "before" or "after" (default) the
                                      destination anchor.

                                      027b7ada -- when
                                      destination_anchor_para_id is itself a
                                      HEADING and destination_position is
                                      "after", the moved section lands after
                                      that heading's ENTIRE section (its own
                                      body + subsections), not immediately
                                      after the heading paragraph -- same
                                      "after this section" fix as
                                      write_section (6822b142), for the same
                                      re-parenting reason. "before" and
                                      "after" a non-heading anchor are an
                                      unchanged literal splice.
        index_db_path:                If supplied, sidecar is invalidated
                                      (and threaded into the renumber_sequences
                                      call) after the write.
        allow_bookmark_split:         Explicit override (default ``False``)
                                      to proceed even when the move would
                                      split a bookmark's start/end across the
                                      move boundary (see e87b8338 above).

    Returns:
        ``{status, section_id, heading_text, moved_block_count,
        destination_anchor_para_id, destination_position,
        renumber_sequences, find_references_to, docx_path}``.

        ``{"error": <message>}`` when ``section_id`` /
        ``destination_anchor_para_id`` can't be resolved, the destination
        falls inside the section being moved, the move would split a
        bookmark (and ``allow_bookmark_split`` is not set), or the write
        fails (file NOT mutated on error in every one of these cases).
    """
    if destination_position not in ("before", "after"):
        return {
            "error": f"destination_position must be 'before' or 'after', got {destination_position!r}"
        }

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    if body is None:
        return {"error": "document has no body element"}

    bounds = _locate_section_bounds(body, section_id)
    if bounds is None:
        return {
            "error": (
                f"heading_id {section_id!r} not found (or is not a heading "
                f"paragraph) in {docx_path}"
            )
        }
    start_idx, end_idx, heading_text, _level = bounds

    dest_result = _find_para_by_id(root, destination_anchor_para_id)
    if dest_result is None:
        return {"error": f"para_id {destination_anchor_para_id!r} not found in {docx_path}"}
    _dbody, _delem, dest_idx = dest_result

    if start_idx <= dest_idx < end_idx:
        return {
            "error": (
                "destination_anchor_para_id falls INSIDE the section being moved "
                f"(body indices [{start_idx}, {end_idx})); choose an anchor outside it"
            )
        }

    # 027b7ada -- same "after this section" fix as write_section (6822b142):
    # when the destination anchor is itself a heading and
    # destination_position is "after", resolve to after that heading's WHOLE
    # section (its own body + subsections), not a literal next-body-child
    # splice -- otherwise the just-moved section's heading would land between
    # the destination heading and its own body, silently re-parenting that
    # pre-existing content. _locate_section_bounds only matches when
    # destination_anchor_para_id resolves to a heading (None otherwise), so
    # this is a no-op for every other destination anchor kind.
    dest_section_bounds = (
        _locate_section_bounds(body, destination_anchor_para_id)
        if destination_position == "after"
        else None
    )

    # e87b8338 -- reference-safety check(s) run and GATE here, BEFORE any
    # mutation. Nothing has been cut, spliced, or saved yet at this point.
    references_result = find_references_to(docx_path, section_id)
    if "error" in references_result:
        return {
            "error": (
                "aborting move_section: pre-move find_references_to check "
                f"failed: {references_result['error']}"
            )
        }

    body_list = list(body)
    split_bookmarks = _bookmarks_split_by_range(body_list, start_idx, end_idx)
    if split_bookmarks and not allow_bookmark_split:
        return {
            "error": (
                f"aborting move_section: moving section {section_id!r} would "
                f"split bookmark(s) {split_bookmarks!r} across the move "
                "boundary (their w:bookmarkStart and w:bookmarkEnd would end "
                "up in two disconnected parts of the document) -- pass "
                "allow_bookmark_split=True to force the move anyway"
            ),
            "split_bookmarks": split_bookmarks,
        }

    moved_elements = body_list[start_idx:end_idx]
    removed_count = end_idx - start_idx

    for el in moved_elements:
        body.remove(el)

    # Adjust the destination index ARITHMETICALLY rather than re-resolving
    # destination_anchor_para_id by string after the removal: if it's a
    # synthesised p{N} id, removing `removed_count` paragraphs earlier in the
    # document shifts every later paragraph's synthetic id, so re-searching
    # for the OLD literal string post-removal could silently match the wrong
    # paragraph (or none). Arithmetic shift sidesteps that entirely.
    def _shift(idx: int) -> int:
        if idx >= end_idx:
            return idx - removed_count
        if idx >= start_idx:
            # Only reachable for dest_section_bounds' end_idx, when the
            # destination heading's OWN section extends up to/into the
            # section being moved (e.g. moving a subsection to "after" its
            # own parent heading) -- the parent's content now ends exactly
            # where the removed range used to start.
            return start_idx
        return idx

    dest_idx_after = _shift(dest_idx)

    if dest_section_bounds is not None:
        _dest_start_idx, dest_end_idx, _dest_heading_text, _dest_level = dest_section_bounds
        insert_at = _shift(dest_end_idx)
    else:
        insert_at = dest_idx_after if destination_position == "before" else dest_idx_after + 1
    for offset, el in enumerate(moved_elements):
        body.insert(insert_at + offset, el)

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    renumber_result = renumber_sequences(docx_path, index_db_path=index_db_path)

    return {
        "status": "moved",
        "section_id": section_id,
        "heading_text": heading_text,
        "moved_block_count": len(moved_elements),
        "destination_anchor_para_id": destination_anchor_para_id,
        "destination_position": destination_position,
        "renumber_sequences": renumber_result,
        "find_references_to": references_result,
        "docx_path": docx_path,
    }


# ---------------------------------------------------------------------------
# Public API 8/9: copy_section (8213050a)
# ---------------------------------------------------------------------------

def copy_section(
    docx_path: str,
    section_id: str,
    destination_anchor_para_id: str,
    destination_position: str = "after",
    index_db_path: str | None = None,
    trim_original_to: str | None = None,
) -> dict[str, Any]:
    """8213050a -- duplicate an existing section (heading + its content) to a
    new location in the document, leaving the original untouched (unless
    ``trim_original_to`` is given -- see below).

    Same section-boundary rule as :func:`move_section`
    (:func:`_locate_section_bounds`), but deep-COPIES the range instead of
    cutting it:

      - Every copied ``<w:p>`` gets a FRESH ``w14:paraId`` (via
        :func:`_new_para_id`) -- duplicate paraIds across a document would
        silently break every paraId-addressed tool in this module, so this
        is a hard invariant, not an optimisation.
      - Every bookmark name inside the copied range (caption ``_Ref<n>``
        cross-reference bookmarks, internal-note ``_MNote<n>`` bookmarks,
        bibliography ``bibkey_`` bookmarks, or anything else) is renamed to
        a fresh unique name (:func:`_rename_bookmark_for_copy`). Copying a
        Figure caption verbatim would otherwise leave TWO captions answering
        to the SAME bookmark name -- :func:`find_references_to` / Word's own
        field resolution would then nondeterministically pick whichever one
        occurs first in document order, silently misdirecting any existing
        cross-reference into either the original or the copy.
      - A REF/PAGEREF/NOTEREF field INSIDE the copied range that targets a
        bookmark ALSO inside the copied range (an internal
        "as shown in Figure 3 above" self-reference) is repointed at the
        COPY's own renamed bookmark, so the duplicated section is internally
        self-consistent. A field targeting a bookmark OUTSIDE the copied
        range is left pointing at the original (shared) target, since that
        target was not duplicated.

    48daaf66 -- ``destination_position="after"`` onto a HEADING anchor
    resolves to after that heading's ENTIRE section (own body + subsections),
    not a literal next-body-child splice -- the exact same fix
    :func:`move_section` got in 027b7ada, reusing the same
    :func:`_locate_section_bounds` call rather than reimplementing it.

    48daaf66 -- a pre-write reference-safety check runs BEFORE any mutation,
    reusing the same two checks :func:`move_section` runs (e87b8338):
    :func:`find_references_to` for ``section_id`` (aborts cleanly, file
    untouched, if it fails) and -- only when ``trim_original_to`` makes this
    a real removal from the original location -- :func:`_bookmarks_split_by_range`
    over the range that would be trimmed away.

    ``trim_original_to`` (48daaf66, optional): when given, the ORIGINAL
    section's body (everything after its heading paragraph, up to
    ``end_idx``) is replaced with a single short paragraph containing this
    text -- a "moved to <destination>, see there" pointer/summary -- instead
    of leaving the original fully untouched. The heading paragraph itself
    (and therefore ``section_id`` / its bookmark / any existing reference
    that targets the section BY HEADING) is always preserved. Content that
    lived in the trimmed body but was never copied anywhere else (e.g. a
    figure caption with its own bookmark) is genuinely gone from that
    bookmark name after this -- this is exactly the same class of risk
    :func:`move_section`'s e87b8338 fix gates on, which is why the same
    pre-write reference check applies here too. ``destination_anchor_para_id``
    must resolve OUTSIDE ``[start_idx, end_idx)`` when ``trim_original_to``
    is set (mirroring :func:`move_section`'s own invariant) -- otherwise the
    trim step would delete the copy this same call just inserted.
    The copy is inserted BEFORE the trim runs, so index arithmetic for the
    trim step accounts for the just-inserted copy the same way
    :func:`move_section`'s ``_shift`` accounts for its own cut.

    :func:`renumber_sequences` is called as the final step (same as
    :func:`move_section`) -- since the copy's SEQ fields start out with the
    SAME cached numbers as the original (deep copy), inserting the copy
    almost always leaves at least one caption's position/number mismatched,
    which also refreshes any now-stale REF display text for the copy's own
    captions as a side effect.

    Args:
        docx_path:                   Absolute path to the .docx file
                                      (mutated in place).
        section_id:                  w14:paraId (or p{N}) of the section's
                                      OWN heading paragraph (the ORIGINAL,
                                      not the copy).
        destination_anchor_para_id:  w14:paraId (or p{N}) to copy the section
                                      next to. Must be OUTSIDE the section
                                      being copied when ``trim_original_to``
                                      is set.
        destination_position:        "before" or "after" (default) the
                                      destination anchor.
        index_db_path:                If supplied, sidecar is invalidated
                                      (and threaded into renumber_sequences)
                                      after the write.
        trim_original_to:            Optional replacement text for the
                                      original section's body (heading kept).
                                      ``None`` (default) leaves the original
                                      fully untouched.

    Returns:
        ``{status, section_id, heading_text, new_heading_para_id,
        copied_block_count, para_id_map, bookmark_map,
        destination_anchor_para_id, destination_position,
        renumber_sequences, find_references_to, trimmed_original, docx_path}``
        -- ``para_id_map`` / ``bookmark_map`` are ``{old: new}`` dicts for
        every paraId/bookmark that existed in the original section and was
        renamed in the copy (originals lacking a native paraId aren't keyed
        in ``para_id_map``, but the copy still gets one -- see
        ``new_heading_para_id``). ``trimmed_original`` is False when
        ``trim_original_to`` was not given.

        ``{"error": <message>}`` on failure (file NOT mutated on error).
    """
    if destination_position not in ("before", "after"):
        return {
            "error": f"destination_position must be 'before' or 'after', got {destination_position!r}"
        }

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    if body is None:
        return {"error": "document has no body element"}

    bounds = _locate_section_bounds(body, section_id)
    if bounds is None:
        return {
            "error": (
                f"heading_id {section_id!r} not found (or is not a heading "
                f"paragraph) in {docx_path}"
            )
        }
    start_idx, end_idx, heading_text, _level = bounds

    dest_result = _find_para_by_id(root, destination_anchor_para_id)
    if dest_result is None:
        return {"error": f"para_id {destination_anchor_para_id!r} not found in {docx_path}"}
    _dbody, _delem, dest_idx = dest_result

    if trim_original_to is not None and start_idx <= dest_idx < end_idx:
        return {
            "error": (
                "destination_anchor_para_id falls INSIDE the section being "
                f"trimmed (body indices [{start_idx}, {end_idx})); choose an "
                "anchor outside it, or omit trim_original_to"
            )
        }

    # 48daaf66 -- same "after this section" fix as move_section (027b7ada):
    # resolve a heading anchor + destination_position="after" to after that
    # heading's WHOLE section, not a literal next-body-child splice.
    dest_section_bounds = (
        _locate_section_bounds(body, destination_anchor_para_id)
        if destination_position == "after"
        else None
    )

    # 48daaf66 -- same pre-write reference-safety gate as move_section
    # (e87b8338): runs and GATES here, BEFORE any mutation.
    references_result = find_references_to(docx_path, section_id)
    if "error" in references_result:
        return {
            "error": (
                "aborting copy_section: pre-copy find_references_to check "
                f"failed: {references_result['error']}"
            )
        }

    body_list = list(body)
    if trim_original_to is not None:
        split_bookmarks = _bookmarks_split_by_range(body_list, start_idx + 1, end_idx)
        if split_bookmarks:
            return {
                "error": (
                    f"aborting copy_section: trimming section {section_id!r} "
                    f"would split bookmark(s) {split_bookmarks!r} across the "
                    "trim boundary"
                ),
                "split_bookmarks": split_bookmarks,
            }

    original_elements = body_list[start_idx:end_idx]
    if not original_elements:
        return {"error": f"section {section_id!r} has no content to copy"}

    taken_ids = _existing_para_ids(root)
    w14_paraId = _q(_W14, "paraId")
    w_p = _q(_W, "p")
    w_bookmarkStart = _q(_W, "bookmarkStart")
    w_name = _q(_W, "name")

    ref_seed_match = _REF_BOOKMARK_RE.match(_next_ref_bookmark_name(root))
    ref_seed = [int(ref_seed_match.group(1))]
    note_seed_match = _INTERNAL_NOTE_BOOKMARK_RE.match(_next_note_bookmark_name(root))
    note_seed = [int(note_seed_match.group(1))]
    fallback_seed = [1]

    para_id_map: dict[str, str] = {}
    bookmark_map: dict[str, str] = {}
    copied_elements: list[ET.Element] = []

    # Pass 1: deep-copy every element, minting fresh paraIds + bookmark names.
    for el in original_elements:
        new_el = copy.deepcopy(el)

        for p in new_el.iter(w_p):
            old_pid = p.get(w14_paraId)
            new_pid = _new_para_id(taken_ids)
            if old_pid:
                para_id_map[old_pid] = new_pid
            p.set(w14_paraId, new_pid)

        for bm in new_el.iter(w_bookmarkStart):
            old_name = bm.get(w_name)
            if not old_name:
                continue
            if old_name not in bookmark_map:
                bookmark_map[old_name] = _rename_bookmark_for_copy(
                    old_name, ref_seed, note_seed, fallback_seed
                )
            bm.set(w_name, bookmark_map[old_name])

        copied_elements.append(new_el)

    # Pass 2: repoint any REF/PAGEREF/NOTEREF field that targets a bookmark
    # ALSO inside the copied range at the copy's own renamed bookmark. Fields
    # targeting a bookmark outside the copy are left alone (still valid --
    # that target wasn't duplicated).
    for new_el in copied_elements:
        for p in new_el.iter(w_p):
            for fld in _iter_complex_fields(p):
                parts = fld["instruction"].split()
                if len(parts) < 2 or parts[0].upper() not in ("REF", "PAGEREF", "NOTEREF"):
                    continue
                old_target = parts[1]
                new_target = bookmark_map.get(old_target)
                if new_target and new_target != old_target:
                    for it_el in fld["instr_elements"]:
                        if it_el.text and old_target in it_el.text:
                            it_el.text = it_el.text.replace(old_target, new_target)

    new_heading_para_id = (
        copied_elements[0].get(w14_paraId) if copied_elements[0].tag == w_p else None
    )

    # 48daaf66 -- same "after this section" resolution as move_section
    # (027b7ada): a heading anchor + "after" lands after that heading's WHOLE
    # section, not a literal next-body-child splice.
    if dest_section_bounds is not None:
        _dest_start_idx, dest_end_idx, _dest_heading_text, _dest_level = dest_section_bounds
        insert_at = dest_end_idx
    else:
        insert_at = dest_idx if destination_position == "before" else dest_idx + 1

    for offset, el in enumerate(copied_elements):
        body.insert(insert_at + offset, el)

    # 48daaf66 -- trim_original_to runs AFTER the copy is inserted (not
    # before): destination_anchor_para_id was already required to resolve
    # OUTSIDE [start_idx, end_idx) above, so the insertion above only shifts
    # start_idx/end_idx when it landed AT OR BEFORE start_idx -- the same
    # arithmetic-shift reasoning move_section's own post-cut _shift relies on,
    # just for an insert instead of a removal.
    trimmed = False
    if trim_original_to is not None:
        inserted_count = len(copied_elements)
        shift = inserted_count if insert_at <= start_idx else 0
        trim_start = start_idx + shift + 1  # keep the heading itself
        trim_end = end_idx + shift
        body_list_now = list(body)
        to_remove = body_list_now[trim_start:trim_end]
        for el in to_remove:
            body.remove(el)
        if trim_original_to:
            placeholder = ET.Element(_q(_W, "p"))
            r = ET.SubElement(placeholder, _q(_W, "r"))
            t = ET.SubElement(r, _q(_W, "t"))
            t.set(_q(_XML_NS, "space"), "preserve")
            t.text = trim_original_to
            placeholder.set(w14_paraId, _new_para_id(taken_ids))
            body.insert(trim_start, placeholder)
        trimmed = True

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    renumber_result = renumber_sequences(docx_path, index_db_path=index_db_path)

    return {
        "status": "copied",
        "section_id": section_id,
        "heading_text": heading_text,
        "new_heading_para_id": new_heading_para_id,
        "copied_block_count": len(copied_elements),
        "para_id_map": para_id_map,
        "bookmark_map": bookmark_map,
        "destination_anchor_para_id": destination_anchor_para_id,
        "destination_position": destination_position,
        "renumber_sequences": renumber_result,
        "find_references_to": references_result,
        "trimmed_original": trimmed,
        "docx_path": docx_path,
    }


# ---------------------------------------------------------------------------
# relocate_table (c031622b) -- move a bare <w:tbl> with no owning heading
#
# Scoped narrower than the original "OOXML-Graph" proposal (81899c27):
# move_section (027b7ada) already relocates a HEADING-delimited section, and
# a table that lives inside one is carried along for free as part of that
# range (see test_move_section_relocates_table_and_caption_and_fixes_seq_and_
# ref_atomically in test_docs_intel_new_primitives.py). This primitive covers
# the other case: a bare <w:tbl> with no owning heading at all -- there is no
# section boundary to reuse move_section's heading-based SOURCE addressing
# for, so this locates the source purely by its own body-child position.
# ---------------------------------------------------------------------------

def relocate_table(
    docx_path: str,
    table_index: int,
    destination_anchor_para_id: str,
    destination_position: str = "after",
    index_db_path: str | None = None,
    allow_bookmark_split: bool = False,
) -> dict[str, Any]:
    """c031622b -- move an existing bare ``<w:tbl>`` (no owning heading) to a
    new location in the document, atomically.

    Unlike :func:`move_section` (which locates its source via a HEADING's
    para_id and cuts the whole heading-delimited range), a bare table has no
    heading of its own to anchor on. ``table_index`` instead identifies the
    table by its own 0-based body-child position -- the exact same ``index``
    value :func:`index_docx_structure` stores in the ``docx_tables`` sidecar
    table and :func:`get_local_structure_elements` returns for each entry in
    its ``tables`` list (``{"index": ..., ...}``), so a caller already holding
    that lookup's output can pass it straight through without any new
    addressing scheme.

    The table is cut from its current body-child slot and re-inserted, as ONE
    atomic operation (single load -> mutate -> save), at a new position
    relative to ``destination_anchor_para_id`` -- the same anchor/position
    convention :func:`move_section` / :func:`copy_section` use
    (:func:`_find_para_by_id`'s three id schemes: native ``w14:paraId``,
    synthesized ``sp<hash>``, legacy ``p{N}``). Because this operates on the
    SAME live ``<w:tbl>`` element object (never re-serialized or rebuilt),
    every descendant is carried verbatim: ``w:tblPr`` (style/borders/
    shading), ``w:tblGrid`` (column widths), and any relationship reference
    inside a cell (e.g. an image's ``r:embed``/``r:id`` attribute, or a
    hyperlink's ``r:id``) -- nothing is renamed or reparented, since a
    relocate (unlike :func:`copy_section`) never duplicates the element, so
    there is no id collision to resolve in the first place.

    027b7ada-style destination fix: when ``destination_anchor_para_id``
    resolves to a HEADING and ``destination_position`` is ``"after"``, the
    table lands after that heading's ENTIRE section (its own body +
    subsections), not immediately after the heading paragraph -- same fix
    :func:`move_section` / :func:`copy_section` rely on, reusing
    :func:`_locate_section_bounds` rather than reimplementing it. "before"
    and "after" a non-heading anchor are an unchanged literal splice.

    e87b8338-style safety check: before anything is cut/spliced/saved, this
    checks whether the move would split a bookmark's ``w:bookmarkStart``/
    ``w:bookmarkEnd`` pair across the ``[table_index, table_index + 1)``
    boundary (rare for a table -- OOXML bookmarks almost never straddle a
    table boundary -- but the check is cheap and the helper already exists,
    see :func:`_bookmarks_split_by_range`). Aborts with the file untouched
    unless ``allow_bookmark_split=True``.

    This primitive intentionally does NOT move a caption paragraph that may
    sit next to the table, and does NOT call :func:`renumber_sequences` --
    since no caption moves with it, SEQ Table numbering is unaffected by this
    call. If the table has a caption that should travel with it, relocate
    that paragraph separately, or use :func:`move_section` when the table is
    actually owned by a heading.

    Args:
        docx_path:                   Absolute path to the .docx file
                                      (mutated in place).
        table_index:                 0-based body-child position of the
                                      ``<w:tbl>`` to relocate (see above).
        destination_anchor_para_id:  w14:paraId (or synthesized/legacy id,
                                      same schemes :func:`_find_para_by_id`
                                      accepts) of the paragraph/table to move
                                      the table next to. Must be OUTSIDE the
                                      table being moved.
        destination_position:        "before" or "after" (default) the
                                      destination anchor.
        index_db_path:                If supplied, sidecar is invalidated
                                      after the write.
        allow_bookmark_split:         Explicit override (default ``False``)
                                      to proceed even when the move would
                                      split a bookmark's start/end across the
                                      move boundary (see e87b8338 above).

    Returns:
        ``{status, table_index, new_table_index, row_count, col_count,
        destination_anchor_para_id, destination_position, docx_path}``.

        ``{"error": <message>}`` when ``table_index`` is out of range or does
        not identify a ``<w:tbl>``, ``destination_anchor_para_id`` can't be
        resolved, the destination falls on/inside the table being moved, or
        the move would split a bookmark (and ``allow_bookmark_split`` is not
        set) -- the file is NOT mutated in any of these cases.
    """
    if destination_position not in ("before", "after"):
        return {
            "error": f"destination_position must be 'before' or 'after', got {destination_position!r}"
        }

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    if body is None:
        return {"error": "document has no body element"}

    body_list = list(body)
    w_tbl = _q(_W, "tbl")

    if not isinstance(table_index, int) or isinstance(table_index, bool) or table_index < 0:
        return {"error": f"table_index must be a non-negative int, got {table_index!r}"}
    if table_index >= len(body_list):
        return {
            "error": (
                f"table_index {table_index} out of range -- document body has "
                f"{len(body_list)} top-level children"
            )
        }
    target_el = body_list[table_index]
    if target_el.tag != w_tbl:
        found_tag = target_el.tag.rsplit("}", 1)[-1]
        return {
            "error": (
                f"body child at index {table_index} is not a <w:tbl> "
                f"(found <{found_tag}>)"
            )
        }

    dest_result = _find_para_by_id(root, destination_anchor_para_id)
    if dest_result is None:
        return {"error": f"para_id {destination_anchor_para_id!r} not found in {docx_path}"}
    _dbody, _delem, dest_idx = dest_result

    if dest_idx == table_index:
        return {
            "error": (
                "destination_anchor_para_id resolves to the table being "
                "relocated (or a paragraph inside it); choose an anchor "
                "outside it"
            )
        }

    # 027b7ada-style fix (see move_section / copy_section): a heading anchor +
    # destination_position="after" lands after that heading's WHOLE section,
    # not a literal next-body-child splice. _locate_section_bounds only
    # matches when destination_anchor_para_id resolves to a heading (None
    # otherwise), so this is a no-op for every other destination anchor kind.
    dest_section_bounds = (
        _locate_section_bounds(body, destination_anchor_para_id)
        if destination_position == "after"
        else None
    )

    # e87b8338-style safety check, applied to the single-element
    # [table_index, table_index + 1) range this move cuts. Runs and GATES
    # here, BEFORE anything is cut/spliced/saved.
    split_bookmarks = _bookmarks_split_by_range(body_list, table_index, table_index + 1)
    if split_bookmarks and not allow_bookmark_split:
        return {
            "error": (
                f"aborting relocate_table: moving table at index {table_index} "
                f"would split bookmark(s) {split_bookmarks!r} across the move "
                "boundary (their w:bookmarkStart and w:bookmarkEnd would end "
                "up in two disconnected parts of the document) -- pass "
                "allow_bookmark_split=True to force the move anyway"
            ),
            "split_bookmarks": split_bookmarks,
        }

    from ._vendored_content_tree import _table_node  # noqa: PLC0415

    table_meta = _table_node(target_el, table_index)

    start_idx = table_index
    end_idx = table_index + 1
    removed_count = 1

    body.remove(target_el)

    # Adjust the destination index ARITHMETICALLY rather than re-resolving
    # destination_anchor_para_id by string after the removal -- same reasoning
    # as move_section's own _shift: if it's a synthesised id, removing the
    # table earlier in the document shifts every later paragraph's synthetic
    # id, so re-searching for the OLD literal string post-removal could
    # silently match the wrong paragraph (or none).
    def _shift(idx: int) -> int:
        if idx >= end_idx:
            return idx - removed_count
        if idx >= start_idx:
            return start_idx
        return idx

    dest_idx_after = _shift(dest_idx)

    if dest_section_bounds is not None:
        _dest_start_idx, dest_end_idx, _dest_heading_text, _dest_level = dest_section_bounds
        insert_at = _shift(dest_end_idx)
    else:
        insert_at = dest_idx_after if destination_position == "before" else dest_idx_after + 1

    body.insert(insert_at, target_el)

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "moved",
        "table_index": table_index,
        "new_table_index": insert_at,
        "row_count": table_meta["row_count"],
        "col_count": table_meta["col_count"],
        "destination_anchor_para_id": destination_anchor_para_id,
        "destination_position": destination_position,
        "docx_path": docx_path,
    }


# ---------------------------------------------------------------------------
# f1185012 -- page header / footer support.
#
# python-docx, the Google Docs API + UI, and Apryse (a commercial DOCX SDK)
# all treat header/footer as first-class, table-stakes DOCX functionality;
# this module had none at all.
#
# Design notes -- a genuinely SEPARATE write path from everything above:
#   - Every write-back function above this line (captions, citations,
#     equations, notes, move_section/copy_section/write_section/
#     relocate_table, ...) only ever rewrites the ONE existing
#     word/document.xml member via _load_docx_xml_stdlib / _save_docx_xml_stdlib,
#     preserving every other ZIP member byte-for-byte. A header/footer needs
#     THREE additional moving pieces docs_intel has never had to manage: a new
#     OOXML part (word/header<N>.xml or word/footer<N>.xml), a new
#     relationship in word/_rels/document.xml.rels, and a new content-type
#     override in [Content_Types].xml.
#   - Rather than spread that new multi-part-write risk into the existing,
#     currently-clean single-part call sites, it lives entirely in its own
#     functions (_save_docx_with_new_parts_stdlib, _set_page_header_or_footer,
#     set_page_header, set_page_footer) with their own dedicated tests. None
#     of the code above this line calls into any of it, and none of it calls
#     into _save_docx_xml_stdlib.
#   - "Set" semantics: calling set_page_header/set_page_footer a second time
#     with the same ``type`` (default/even/first) overwrites the SAME part in
#     place (found via the sectPr's existing headerReference/footerReference
#     -> rels lookup) rather than allocating a new part + orphaning the old
#     one on every call.
#   - Scope: wires the reference into the document's FINAL, body-level
#     <w:sectPr> only (the section properties governing a single-section
#     document -- the overwhelming common case -- and the last section of a
#     multi-section one). Per-section overrides for EARLIER sections of a
#     multi-section document, and the settings.xml <w:titlePg>/
#     <w:evenAndOddHeaders> switches that make "first"/"even" page types
#     actually render differently in Word, are real but narrower follow-ups,
#     not attempted here.
# ---------------------------------------------------------------------------

_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

_HDR_FTR_CONTENT_TYPES = {
    "header": "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml",
    "footer": "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml",
}
_HDR_FTR_VALID_TYPES = frozenset({"default", "even", "first"})

# Minimal-but-valid fallback parts for the (rare, mostly test-fixture) case of
# a .docx that has no [Content_Types].xml / word/_rels/document.xml.rels at
# all yet. Real Word output always has both; this just means "add a header to
# a hand-built minimal docx" doesn't hard-fail for lack of scaffolding.
_MINIMAL_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
).encode("utf-8")

_MINIMAL_DOCUMENT_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    "</Relationships>"
).encode("utf-8")


def _save_docx_with_new_parts_stdlib(raw: bytes, updated_parts: dict[str, bytes], dest: str) -> None:
    """Write MULTIPLE ZIP parts back into ``dest`` in one repackage.

    Unlike :func:`_save_docx_xml_stdlib` (which can only ever overwrite the
    single, already-existing ``word/document.xml`` member), this adds parts
    that are not already present in the original archive AND overwrites parts
    that are. Every other original ZIP member is preserved byte-for-byte.

    Backs up the existing file to ``dest + ".bak"`` when it already exists
    (best-effort, non-fatal on failure -- same pattern as
    :func:`_save_docx_xml_stdlib`).
    """
    out = io.BytesIO()
    written: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(raw)) as src:
        infos = src.infolist()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in infos:
                data = src.read(info.filename)
                if info.filename in updated_parts:
                    data = updated_parts[info.filename]
                dst.writestr(info, data)
                written.add(info.filename)
            # Anything in updated_parts that wasn't already a ZIP member is a
            # genuinely new part -- append it.
            for part_name, data in updated_parts.items():
                if part_name not in written:
                    dst.writestr(part_name, data)

    if os.path.exists(dest):
        backup = dest + ".bak"
        try:
            shutil.copy2(dest, backup)
        except OSError:
            pass  # backup failure is non-fatal

    with open(dest, "wb") as fh:
        fh.write(out.getvalue())


def _insert_before_closing_tag(xml_bytes: bytes, root_tag_name: str, new_element_xml: str) -> bytes:
    """Insert ``new_element_xml`` as the last child of ``<root_tag_name>``.

    Text-splice rather than an ET parse/re-serialize round-trip: OPC
    infrastructure parts ([Content_Types].xml, .rels files) are always
    unprefixed-default-namespace XML in real Word output, and ET's
    ``register_namespace`` registry is a single global default-prefix slot --
    registering it for a SECOND unrelated namespace (this one, alongside the
    package content-types namespace) would silently evict whichever one was
    registered first (both want the empty '' prefix), corrupting unrelated
    serialization elsewhere in this module. Splicing the raw text instead
    sidesteps that global-state hazard entirely and guarantees byte-for-byte
    preservation of everything except the one new child.

    Handles both an existing ``<root_tag_name>...</root_tag_name>`` (appends
    before the closing tag) and a childless self-closing
    ``<root_tag_name .../>`` (expands it to hold the new child).
    """
    text = xml_bytes.decode("utf-8")
    closing = f"</{root_tag_name}>"
    idx = text.rfind(closing)
    if idx != -1:
        return (text[:idx] + new_element_xml + text[idx:]).encode("utf-8")

    self_close_pattern = re.compile(rf"<{root_tag_name}\b([^>]*?)/>")
    match = self_close_pattern.search(text)
    if not match:
        raise ValueError(f"could not locate <{root_tag_name}> element to extend")
    expanded = f"<{root_tag_name}{match.group(1)}>{new_element_xml}</{root_tag_name}>"
    return (text[: match.start()] + expanded + text[match.end() :]).encode("utf-8")


def _next_relationship_id(rels_root: ET.Element) -> str:
    """Return the next unused ``rId<N>`` relationship id (Word's own convention)."""
    max_seen = 0
    for rel in rels_root:
        rid = rel.get("Id") or ""
        m = re.match(r"^rId(\d+)$", rid)
        if m:
            max_seen = max(max_seen, int(m.group(1)))
    return f"rId{max_seen + 1}"


def _next_header_footer_part_name(namelist: list[str], kind: str) -> str:
    """Return the next unused ``word/header<N>.xml`` / ``word/footer<N>.xml`` name."""
    pattern = re.compile(rf"^word/{kind}(\d+)\.xml$")
    max_seen = 0
    for name in namelist:
        m = pattern.match(name)
        if m:
            max_seen = max(max_seen, int(m.group(1)))
    return f"word/{kind}{max_seen + 1}.xml"


def _build_header_footer_part_xml(kind: str, text: str) -> bytes:
    """Build a minimal but valid ``<w:hdr>``/``<w:ftr>`` OOXML part: one
    paragraph containing ``text`` as a single run."""
    root_tag = "hdr" if kind == "header" else "ftr"
    root = ET.Element(_q(_W, root_tag))
    p = ET.SubElement(root, _q(_W, "p"))
    r = ET.SubElement(p, _q(_W, "r"))
    t = ET.SubElement(r, _q(_W, "t"))
    t.set(_q(_XML_NS, "space"), "preserve")
    t.text = text
    xml_body = ET.tostring(root, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + xml_body).encode("utf-8")


def _sectpr_insert_ref_index(sectpr: ET.Element, kind: str) -> int:
    """Return the correct child index to insert a new headerReference/
    footerReference at, per CT_SectPr's schema order: all ``headerReference``
    elements come first (as a group), then all ``footerReference`` elements,
    then everything else (``type``, ``pgSz``, ``pgMar``, ...)."""
    header_tag = _q(_W, "headerReference")
    footer_tag = _q(_W, "footerReference")
    children = list(sectpr)
    idx = 0
    if kind == "header":
        while idx < len(children) and children[idx].tag == header_tag:
            idx += 1
    else:
        while idx < len(children) and children[idx].tag in (header_tag, footer_tag):
            idx += 1
    return idx


def _set_page_header_or_footer(
    docx_path: str,
    text: str,
    kind: str,
    header_footer_type: str,
    index_db_path: str | None,
) -> dict[str, Any]:
    """Shared implementation for :func:`set_page_header` / :func:`set_page_footer`."""
    if not text or not str(text).strip():
        return {"error": "text must be a non-empty string"}
    if header_footer_type not in _HDR_FTR_VALID_TYPES:
        return {
            "error": (
                f"type must be one of {sorted(_HDR_FTR_VALID_TYPES)}, "
                f"got {header_footer_type!r}"
            )
        }
    if not os.path.exists(docx_path):
        return {"error": f"no such file: {docx_path}"}

    with open(docx_path, "rb") as fh:
        raw = fh.read()

    try:
        src = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return {"error": f"not a valid .docx (not a ZIP): {docx_path}"}

    try:
        namelist = src.namelist()
        if "word/document.xml" not in namelist:
            return {"error": f"not a valid .docx: missing word/document.xml: {docx_path}"}
        document_xml = src.read("word/document.xml")
        try:
            doc_root = ET.fromstring(document_xml)
        except ET.ParseError as exc:
            return {"error": f"malformed word/document.xml in {docx_path}: {exc}"}

        body = doc_root.find(_q(_W, "body"))
        if body is None:
            return {"error": "document has no <w:body>"}

        final_sectpr = None
        for child in body:
            if child.tag == _q(_W, "sectPr"):
                final_sectpr = child
        if final_sectpr is None:
            final_sectpr = ET.SubElement(body, _q(_W, "sectPr"))

        ref_tag = _q(_W, f"{kind}Reference")
        w_type_attr = _q(_W, "type")
        r_id_attr = _q(_R_NS, "id")

        existing_ref = None
        for child in final_sectpr:
            if child.tag == ref_tag and child.get(w_type_attr) == header_footer_type:
                existing_ref = child
                break

        rels_path = "word/_rels/document.xml.rels"
        rels_bytes = src.read(rels_path) if rels_path in namelist else _MINIMAL_DOCUMENT_RELS_XML
        rels_root = ET.fromstring(rels_bytes)
        rel_tag = _q(_PKG_REL_NS, "Relationship")

        part_xml = _build_header_footer_part_xml(kind, text.strip())
        updated_parts: dict[str, bytes] = {}

        if existing_ref is not None:
            # "Set" -- an active reference of this exact type already exists.
            # Overwrite the part it already points to, in place. No new
            # relationship, content-type override, or sectPr change needed.
            existing_rid = existing_ref.get(r_id_attr)
            target = None
            for rel in rels_root:
                if rel.tag == rel_tag and rel.get("Id") == existing_rid:
                    target = rel.get("Target")
                    break
            if not target:
                return {
                    "error": (
                        f"sectPr has a {kind}Reference r:id={existing_rid!r} with no "
                        "matching relationship in word/_rels/document.xml.rels -- "
                        "docx is internally inconsistent"
                    )
                }
            part_name = target if target.startswith("word/") else f"word/{target}"
            updated_parts[part_name] = part_xml
            rel_id = existing_rid
        else:
            part_name = _next_header_footer_part_name(namelist, kind)
            rel_id = _next_relationship_id(rels_root)
            rel_type = f"{_R_NS}/{kind}"
            target_rel = part_name.split("/", 1)[1]  # relative to word/_rels/

            new_rel_xml = f'<Relationship Id="{rel_id}" Type="{rel_type}" Target="{target_rel}"/>'
            new_rels_bytes = _insert_before_closing_tag(rels_bytes, "Relationships", new_rel_xml)

            ct_path = "[Content_Types].xml"
            ct_bytes = src.read(ct_path) if ct_path in namelist else _MINIMAL_CONTENT_TYPES_XML
            new_override_xml = (
                f'<Override PartName="/{part_name}" '
                f'ContentType="{_HDR_FTR_CONTENT_TYPES[kind]}"/>'
            )
            new_ct_bytes = _insert_before_closing_tag(ct_bytes, "Types", new_override_xml)

            ref_el = ET.Element(ref_tag)
            ref_el.set(w_type_attr, header_footer_type)
            ref_el.set(r_id_attr, rel_id)
            insert_at = _sectpr_insert_ref_index(final_sectpr, kind)
            final_sectpr.insert(insert_at, ref_el)

            new_document_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                + ET.tostring(doc_root, encoding="unicode")
            ).encode("utf-8")

            updated_parts[rels_path] = new_rels_bytes
            updated_parts[ct_path] = new_ct_bytes
            updated_parts["word/document.xml"] = new_document_xml
            updated_parts[part_name] = part_xml
    finally:
        src.close()

    try:
        _save_docx_with_new_parts_stdlib(raw, updated_parts, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "set",
        "kind": kind,
        "type": header_footer_type,
        "part_name": part_name,
        "relationship_id": rel_id,
        "text": text.strip(),
        "docx_path": docx_path,
    }


def set_page_header(
    docx_path: str,
    text: str,
    header_type: str = "default",
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """f1185012 -- Add or replace a page header on ``docx_path``.

    Allocates a new ``word/header<N>.xml`` part, a new relationship in
    ``word/_rels/document.xml.rels``, a new content-type override in
    ``[Content_Types].xml``, and a ``<w:headerReference>`` on the document's
    final ``<w:sectPr>`` -- OR, if a header of this exact ``header_type``
    already exists, overwrites that SAME part's content in place (no new
    part/relationship/override, no sectPr change).

    Args:
        docx_path:    Absolute path to the .docx file (mutated in place).
        text:         Header text (a single paragraph, single run).
        header_type:  One of ``"default"``, ``"even"``, ``"first"``
                      (``w:type`` on ``<w:headerReference>``). Note: making
                      "even"/"first" actually render distinctly in Word ALSO
                      requires ``settings.xml``'s ``<w:evenAndOddHeaders>`` /
                      a section's ``<w:titlePg>`` -- not set by this function.
        index_db_path: If supplied, invalidates that sidecar's cached mtime
                      so the next read re-parses the document.

    Returns:
        ``{status, kind, type, part_name, relationship_id, text, docx_path}``
        or ``{"error": <message>}`` on failure (file NOT mutated on error).
    """
    return _set_page_header_or_footer(docx_path, text, "header", header_type, index_db_path)


def set_page_footer(
    docx_path: str,
    text: str,
    footer_type: str = "default",
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """f1185012 -- Add or replace a page footer on ``docx_path``.

    Mirrors :func:`set_page_header` exactly (same allocation-vs-overwrite
    "set" semantics, same part/relationship/content-type plumbing) for
    ``<w:footerReference>`` / ``word/footer<N>.xml`` instead. See its
    docstring for the full parameter/return contract.
    """
    return _set_page_header_or_footer(docx_path, text, "footer", footer_type, index_db_path)
