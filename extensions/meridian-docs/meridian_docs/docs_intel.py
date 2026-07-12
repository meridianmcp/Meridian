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

Pure library — every function is deterministic and unit-tested against a
synthetic in-memory .docx (see tests/test_docs_intel.py).
"""
from __future__ import annotations

import io
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
