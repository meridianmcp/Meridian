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

Beyond headings the parser also understands two OOXML constructs the outline
alone missed: **field codes** (``kind="field"`` — TOC / SEQ / PAGEREF etc.,
flagged ``needs_refresh`` because Word regenerates them, a62e5b4f) and **full
body content in true document order** (:func:`document_content_tree`, which walks
every paragraph *and* table preserving heading hierarchy, 0d1b0809).

Pure library — every function is deterministic and unit-tested against a
synthetic in-memory .docx (see tests/test_docs_intel.py).
"""
from __future__ import annotations

import io
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


# Field instructions Word regenerates on refresh (F9): TOC/table-of-figures,
# SEQ counters (LOF/LOT entry numbers), and the cross-reference family that
# resolves to those live numbers. Anything in this set is marked
# ``needs_refresh=True`` because its rendered text is a cached computation, not
# authored content — a downstream consumer must re-evaluate it, never trust the
# stale run text baked into the XML.
_REFRESHABLE_FIELDS = frozenset(
    {"TOC", "SEQ", "PAGEREF", "REF", "NOTEREF", "PAGE", "NUMPAGES", "STYLEREF"}
)


def _field_type(instruction: str | None) -> str | None:
    """First whitespace-delimited token of a field instruction, upper-cased.

    A Word field instruction is ``FIELDTYPE arg1 arg2 \\switches`` — e.g.
    ``TOC \\o "1-3" \\h`` or ``SEQ Figure \\* ARABIC``. The leading token is the
    field type; return it normalized (``TOC``, ``SEQ``, ...), or ``None`` for a
    blank instruction.
    """
    if not instruction:
        return None
    token = instruction.strip().split(maxsplit=1)
    return token[0].upper() if token else None


def _field_needs_refresh(field_type: str | None) -> bool:
    """A field is refreshable when Word recomputes it (TOC/SEQ/PAGEREF/...)."""
    return field_type in _REFRESHABLE_FIELDS


def _fields_in_paragraph(p: ET.Element) -> list[dict[str, Any]]:
    """Extract every Word field code embedded in one ``<w:p>``, in order.

    Handles both OOXML encodings:

    * **Simple fields** — ``<w:fldSimple w:instr="TOC \\o ...">`` carry the whole
      instruction as an attribute.
    * **Complex fields** — a ``<w:fldChar w:fldCharType="begin"/>`` opens a field,
      one or more ``<w:instrText>`` runs carry the instruction (Word splits long
      instructions across runs, so they are concatenated), and a matching
      ``<w:fldChar w:fldCharType="end"/>`` closes it. Fields nest (e.g. a
      PAGEREF inside a TOC), so a depth counter attributes each ``instrText`` to
      the innermost open field and emits a field only when it is closed.

    Each returned record is ``{kind: "field", field_type, instruction,
    needs_refresh}``. Document iteration order is preserved by walking the
    paragraph subtree with ``iter()`` and emitting complex fields at their
    ``end`` marker.
    """
    fields: list[dict[str, Any]] = []
    # Stack of instruction-text buffers, one per currently-open complex field.
    open_fields: list[list[str]] = []
    fld_simple = _q(_W, "fldSimple")
    fld_char = _q(_W, "fldChar")
    instr_text = _q(_W, "instrText")
    char_type_attr = _q(_W, "fldCharType")
    instr_attr = _q(_W, "instr")

    def _emit(instruction: str) -> None:
        ftype = _field_type(instruction)
        fields.append(
            {
                "kind": "field",
                "field_type": ftype,
                "instruction": instruction.strip(),
                "needs_refresh": _field_needs_refresh(ftype),
            }
        )

    for el in p.iter():
        tag = el.tag
        if tag == fld_simple:
            _emit(el.get(instr_attr) or "")
        elif tag == fld_char:
            char_type = el.get(char_type_attr)
            if char_type == "begin":
                open_fields.append([])
            elif char_type == "end" and open_fields:
                _emit("".join(open_fields.pop()))
        elif tag == instr_text and open_fields:
            open_fields[-1].append(el.text or "")
    # A malformed field left open (no matching end) is still surfaced so the
    # instruction is never silently dropped.
    for pending in open_fields:
        _emit("".join(pending))
    return fields


def parse_docx(source: str | bytes | bytearray) -> list[dict[str, Any]]:
    """Parse a .docx (path or raw bytes) into ordered paragraph records.

    Each record is ``{index, para_id, style, text, kind, fields}``. ``para_id``
    is the ``w14:paraId`` when Word wrote one (stable across edits), else a
    synthesized ``p{index}`` so every paragraph is still addressable. ``kind`` is
    ``"heading"`` for a heading-styled paragraph else ``"paragraph"``. ``fields``
    is the ordered list of embedded Word field codes (a62e5b4f) — TOC / SEQ /
    PAGEREF etc. — each ``{kind: "field", field_type, instruction, needs_refresh}``
    (empty list when the paragraph has none). Returns an empty list for a
    document with no body.
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
        style = _paragraph_style(p)
        text = _paragraph_text(p)
        paragraphs.append(
            {
                "index": index,
                "para_id": para_id,
                "style": style,
                "text": text,
                "kind": "heading" if _is_heading(style) else "paragraph",
                "fields": _fields_in_paragraph(p),
            }
        )
    return paragraphs


def document_outline(source: str | bytes | bytearray) -> dict[str, Any]:
    """13462df2 — stateless heading outline of a .docx (path or raw bytes). No
    sidecar index: a pure parse. Returns ``paragraph_count`` + ``heading_count``
    + an ordered ``headings`` list (level/text/para_id) — the queryable document
    structure docs_intel exposes without building a persistent index.

    a62e5b4f — also surfaces every Word field code (TOC / SEQ / PAGEREF ...) in
    an ordered ``fields`` list (``para_id``, ``field_type``, ``instruction``,
    ``needs_refresh``) plus a ``field_count``. Purely additive: the existing
    ``headings`` shape is unchanged so all callers keep working."""
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
    fields = [
        {
            "para_id": p.get("para_id"),
            "field_type": f.get("field_type"),
            "instruction": f.get("instruction", ""),
            "needs_refresh": f.get("needs_refresh", False),
        }
        for p in paras
        for f in p.get("fields", [])
    ]
    return {
        "paragraph_count": len(paras),
        "heading_count": len(headings),
        "headings": headings,
        "field_count": len(fields),
        "fields": fields,
    }


def _paragraph_style(p: ET.Element) -> str | None:
    ppr = p.find(_q(_W, "pPr"))
    if ppr is None:
        return None
    pstyle = ppr.find(_q(_W, "pStyle"))
    return pstyle.get(_q(_W, "val")) if pstyle is not None else None


def _paragraph_text(p: ET.Element) -> str:
    return "".join(t.text or "" for t in p.iter(_q(_W, "t")))


def _paragraph_node(p: ET.Element, index: int) -> dict[str, Any]:
    """Build a content-tree node for one ``<w:p>`` (heading or body paragraph)."""
    para_id = p.get(_q(_W14, "paraId")) or f"p{index}"
    style = _paragraph_style(p)
    fields = _fields_in_paragraph(p)
    node: dict[str, Any] = {
        "kind": "heading" if _is_heading(style) else "paragraph",
        "index": index,
        "para_id": para_id,
        "style": style,
        "text": _paragraph_text(p),
        "fields": fields,
    }
    if _is_heading(style):
        node["level"] = _heading_level(style)
    return node


def _table_node(tbl: ET.Element, index: int) -> dict[str, Any]:
    """Build a ``kind="table"`` node, rows -> cells -> joined cell text.

    Cell text is the concatenation of every paragraph in the cell (Word wraps
    cell content in ``<w:p>``); embedded fields inside cells are collected onto
    the table node so a TOC/SEQ living in a table is not lost.
    """
    rows: list[list[str]] = []
    cell_fields: list[dict[str, Any]] = []
    for tr in tbl.findall(_q(_W, "tr")):
        row: list[str] = []
        for tc in tr.findall(_q(_W, "tc")):
            cell_text_parts: list[str] = []
            for cp in tc.findall(_q(_W, "p")):
                cell_text_parts.append(_paragraph_text(cp))
                cell_fields.extend(_fields_in_paragraph(cp))
            row.append("\n".join(cell_text_parts))
        rows.append(row)
    return {
        "kind": "table",
        "index": index,
        "rows": rows,
        "row_count": len(rows),
        "col_count": max((len(r) for r in rows), default=0),
        "fields": cell_fields,
    }


def document_content_tree(source: str | bytes | bytearray) -> dict[str, Any]:
    """0d1b0809 — full body content of a .docx in *true document order*.

    ``document_outline`` yields headings only; this walks **every** top-level body
    block — paragraphs *and* tables — in the sequence Word stored them (so a table
    interleaved between two paragraphs keeps its real position, which a
    ``findall("w:p")`` pass would drop). Paragraph nodes are keyed on
    ``w14:paraId`` when present (else a synthesized ``p{index}``); heading nodes
    additionally carry ``level`` and are used to build a nested ``tree`` that
    reflects the heading hierarchy — each heading owns the body blocks and lower
    headings that follow it until a heading of equal-or-higher rank.

    Returns::

        {
            paragraph_count, table_count, heading_count, field_count,
            blocks: [ ...flat, document-ordered nodes... ],
            tree:   [ ...roots, each with nested "children"... ],
        }

    ``blocks`` is the flat sequential stream (paragraphs, headings, tables);
    ``tree`` is the same nodes nested by heading level. Purely additive — this is
    a sibling of ``document_outline``, which is untouched.
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
    blocks: list[dict[str, Any]] = []
    if body is None:
        return {
            "paragraph_count": 0,
            "table_count": 0,
            "heading_count": 0,
            "field_count": 0,
            "blocks": [],
            "tree": [],
        }

    p_tag = _q(_W, "p")
    tbl_tag = _q(_W, "tbl")
    # Walk direct body children in stored order so paragraphs and tables keep
    # their real interleaving.
    for index, child in enumerate(list(body)):
        if child.tag == p_tag:
            blocks.append(_paragraph_node(child, index))
        elif child.tag == tbl_tag:
            blocks.append(_table_node(child, index))
        # Other body children (sectPr, bookmarks at body level, ...) are skipped
        # from the content stream but do not disturb the running index.

    paragraph_count = sum(1 for b in blocks if b["kind"] in ("paragraph", "heading"))
    table_count = sum(1 for b in blocks if b["kind"] == "table")
    heading_count = sum(1 for b in blocks if b["kind"] == "heading")
    field_count = sum(len(b.get("fields", [])) for b in blocks)

    # Build the heading-nested tree. A stack holds the open heading ancestors;
    # non-heading blocks attach as children of the deepest open heading (or to a
    # synthetic root list when they precede the first heading).
    tree: list[dict[str, Any]] = []
    heading_stack: list[dict[str, Any]] = []
    for block in blocks:
        node = {**block, "children": []}
        if block["kind"] == "heading":
            lvl = block.get("level", 1)
            while heading_stack and heading_stack[-1].get("level", 1) >= lvl:
                heading_stack.pop()
            (heading_stack[-1]["children"] if heading_stack else tree).append(node)
            heading_stack.append(node)
        else:
            (heading_stack[-1]["children"] if heading_stack else tree).append(node)

    return {
        "paragraph_count": paragraph_count,
        "table_count": table_count,
        "heading_count": heading_count,
        "field_count": field_count,
        "blocks": blocks,
        "tree": tree,
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
    return conn


def index_docx(
    source: str | bytes | bytearray, index_db_path: str
) -> dict[str, Any]:
    """Build (or rebuild) a sidecar SQLite index of a .docx keyed by paraId.

    Returns a summary ``{index_db, paragraph_count, heading_count}``. Idempotent:
    the paragraph table is fully replaced each run so re-indexing an edited doc
    stays consistent.
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
        conn.commit()
    finally:
        conn.close()
    return {
        "index_db": index_db_path,
        "paragraph_count": len(paragraphs),
        "heading_count": sum(1 for p in paragraphs if _is_heading(p["style"])),
    }


def get_paragraph(index_db_path: str, para_id: str) -> dict[str, Any] | None:
    """Look up one paragraph by its ``w14:paraId`` (the targeted-navigation op)."""
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
    """Return the heading outline (para_id, level, text) in document order."""
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
    """Substring search over paragraph text (document order); returns paraIds."""
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
