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

import io
import json
import os
import re
import shutil
import sqlite3
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
    + an ordered ``headings`` list (level/text/para_id) — the queryable document
    structure docs_intel exposes without building a persistent index."""
    paras = parse_docx(source)
    headings = [
        {
            "level": _heading_level(p.get("style")),
            "text": p.get("text", ""),
            "para_id": p.get("para_id"),
        }
        for p in paras
        if _is_heading(p.get("style"))
    ]
    return {
        "paragraph_count": len(paras),
        "heading_count": len(headings),
        "headings": headings,
    }


def _connect(index_db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(index_db_path)
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
            text TEXT NOT NULL
        )
        """
    )
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
    """Build (or rebuild) a sidecar SQLite index of a .docx keyed by paraId.

    Returns a summary ``{index_db, paragraph_count, heading_count}``. Idempotent:
    the paragraph table is fully replaced each run so re-indexing an edited doc
    stays consistent.

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
        conn.execute("DELETE FROM docx_paragraphs")
        conn.executemany(
            "INSERT OR REPLACE INTO docx_paragraphs (para_id, idx, style, text) "
            "VALUES (?, ?, ?, ?)",
            [(p["para_id"], p["index"], p["style"], p["text"]) for p in paragraphs],
        )
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
    headings_out: list[tuple[str, int, int, str]] = []
    figures_out: list[tuple[int, str | None, str, str | None]] = []
    tables_out: list[tuple[int, int, int, str, str | None]] = []  # (..., caption)

    last_table_entry_index: int | None = None

    for block in blocks:
        kind = block.get("kind")
        idx = block.get("index", 0)

        if kind == "heading":
            headings_out.append((
                block.get("para_id", f"p{idx}"),
                idx,
                block.get("level", 1),
                block.get("text", ""),
            ))
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

    conn = _connect(index_db_path)
    try:
        conn.execute("DELETE FROM docx_headings")
        conn.execute("DELETE FROM docx_figures")
        conn.execute("DELETE FROM docx_tables")

        conn.executemany(
            "INSERT OR REPLACE INTO docx_headings (para_id, idx, level, text) "
            "VALUES (?, ?, ?, ?)",
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
            "SELECT para_id, idx, level, text FROM docx_headings ORDER BY idx"
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
        {"para_id": r[0], "index": r[1], "level": r[2], "text": r[3]}
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

    Searches the body's direct children by ``w14:paraId`` attribute first, then
    by synthesised ``p{N}`` index (counting every ``<w:p>`` in document order
    including those inside tables). Returns ``None`` when not found.

    The ``body_child_index`` is the index of the direct body child that contains
    or IS the matching paragraph — callers use it for before/after insertion.
    """
    body = root.find(_q(_W, "body"))
    if body is None:
        return None
    w_p = _q(_W, "p")
    w14_para_id = _q(_W14, "paraId")
    global_p_idx = 0
    for child_idx, child in enumerate(list(body)):
        if child.tag == w_p:
            real_id = child.get(w14_para_id)
            synth_id = f"p{global_p_idx}"
            if real_id == para_id or synth_id == para_id:
                return body, child, child_idx
            global_p_idx += 1
        else:
            # Walk into tables to find paragraphs inside cells.
            for p in child.iter(w_p):
                real_id = p.get(w14_para_id)
                synth_id = f"p{global_p_idx}"
                if real_id == para_id or synth_id == para_id:
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


def _build_caption_paragraph(
    kind: str,
    label_text: str,
    seq_cached: str = "1",
) -> ET.Element:
    """Build a ``<w:p>`` element for a Word caption using the Caption style.

    Produces::

        <w:p>
          <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
          <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
          <w:fldSimple w:instr="SEQ Figure \\* ARABIC">
            <w:r><w:t>1</w:t></w:r>
          </w:fldSimple>
          <w:r><w:t xml:space="preserve">. label_text</w:t></w:r>
        </w:p>

    ``kind`` is ``"Figure"`` or ``"Table"``.  ``seq_cached`` is the cached
    rendered number (e.g. ``"1"``).  The fldSimple approach matches Word's own
    caption wizard (single instruction + cached result, no fldChar dance needed).
    """
    if kind not in ("Figure", "Table"):
        raise ValueError(f"caption kind must be 'Figure' or 'Table', got {kind!r}")

    instr = _SEQ_FIGURE_INSTR if kind == "Figure" else _SEQ_TABLE_INSTR

    p = ET.Element(_q(_W, "p"))

    # Paragraph properties: Caption style.
    pPr = ET.SubElement(p, _q(_W, "pPr"))
    pStyle = ET.SubElement(pPr, _q(_W, "pStyle"))
    pStyle.set(_q(_W, "val"), _CAPTION_STYLE)

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
) -> None:
    """Upsert a caption record into the sidecar SQLite index.

    For Figure captions: inserts a new row into ``docx_figures``.
    For Table captions: updates the ``caption`` column in ``docx_tables`` for
    the most-recent table row, or inserts a placeholder if none exists yet.

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
                    "INSERT INTO docx_figures (idx, para_id, caption, seq_number, section) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (next_idx, para_id, caption_text, seq_number, section_heading),
                )
            else:
                # Table: update the most recent table's caption.
                row = conn.execute(
                    "SELECT id FROM docx_tables ORDER BY idx DESC LIMIT 1"
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE docx_tables SET caption = ? WHERE id = ?",
                        (caption_text, row[0]),
                    )
                else:
                    row2 = conn.execute("SELECT MAX(idx) FROM docx_tables").fetchone()
                    next_idx = (row2[0] or 0) + 1
                    conn.execute(
                        "INSERT INTO docx_tables "
                        "(idx, row_count, col_count, rows_json, caption) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (next_idx, 0, 0, "[]", caption_text),
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass  # sidecar sync is best-effort


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
        position:        ``"after"`` (default) or ``"before"``.
        section_heading: Optional heading text for the section this caption
                         belongs to.  Stored in the sidecar ``section`` column.
        index_db_path:   If supplied, the sidecar SQLite index is invalidated
                         after the write so the next read auto-reindexes.

    Returns:
        ``{status, kind, seq_number, label_text, section_heading, docx_path}``
        or ``{"error": <message>}`` on failure (file is NOT mutated on error).
    """
    kind = str(kind).strip()
    if kind not in ("Figure", "Table"):
        return {"error": f"kind must be 'Figure' or 'Table', got {kind!r}"}
    if position not in ("before", "after"):
        return {"error": f"position must be 'before' or 'after', got {position!r}"}
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

    caption_p = _build_caption_paragraph(
        kind=kind,
        label_text=label_text.strip(),
        seq_cached=str(seq_number),
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
        )

    return {
        "status": "inserted",
        "kind": kind,
        "seq_number": seq_number,
        "label_text": label_text.strip(),
        "section_heading": section_heading,
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
