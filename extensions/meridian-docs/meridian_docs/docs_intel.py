"""OOXML-Graph — DOCX document intelligence layer, Phase 1 (618adf32).

A .docx is a ZIP of XML, so this Phase-1 slice needs no third-party dependency
(python-docx / lxml are absent here): it reads ``word/document.xml`` with the
stdlib ``zipfile`` + ``xml.etree.ElementTree`` and builds a *sidecar SQLite
index keyed by* ``w14:paraId`` — the stable, revision-independent paragraph id
Word assigns. That id is the anchor the rest of the tool set navigates by
(structure outline, targeted lookup, cross-reference, search).

Scope of Phase 1 (foundation): parse → index → structure navigation → targeted
paragraph lookup → text search. The full vision (13 MCP tools, track-changes
editing, cross-ref resolution, a standalone ``meridian-docs`` uvx package +
tunnel plugin, LaTeX addon) builds on these primitives.

c39ae092 — also exposes :func:`index_docx_structure` and
:func:`get_local_structure_elements`, which extend the SAME sidecar SQLite DB
(or a standalone one) to store structural elements (headings, figures, tables)
parsed from the .docx via the vendored ``document_content_tree`` parser.
This is the local-only fallback for :func:`ingest_local_document_structure`
that avoids the Cloudflare-blocked hosted POST.

Pure library — every function is deterministic and unit-tested against a
synthetic in-memory .docx (see tests/test_docs_intel.py).
"""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

# OOXML namespaces.
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"


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
    _SEQ_TABLE_RE2 = _SEQ_TABLE_RE

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
