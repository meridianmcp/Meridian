"""OOXML (.docx) parsing primitives — stdlib only, no third-party deps.

This module is the CANONICAL home for:
  - OOXML namespace constants (_W, _W14)
  - Low-level element helpers (_q, _is_heading, _heading_level, _paragraph_text,
    _paragraph_style, _paragraph_node, _table_node, _fields_in_paragraph)
  - :func:`document_content_tree` — parse a .docx ZIP into a structured block tree.

DEDUPLICATION NOTE (e6385777):
Before this package existed, `document_content_tree` lived in three copies:
  1. packages/docparse/docparse/docs_intel.py        (source of truth, 613+ lines)
  2. extensions/meridian-docs/meridian_docs/_vendored_content_tree.py
     (vendored, with a header comment explaining why it was copied — uvx
     isolated environments can't resolve path-based cross-package deps)
  3. Partially re-derived inside meridian/doc_store.py:elements_from_docx_content_tree

The correct long-term fix is:
  1. Publish THIS package to PyPI as `meridian-plugin-base`.
  2. extensions/meridian-docs adds `meridian-plugin-base>=0.1` to its
     [project].dependencies (NOT [tool.uv.sources] — a real PyPI dep).
  3. Delete extensions/meridian-docs/meridian_docs/_vendored_content_tree.py;
     replace imports with `from meridian_plugin_base.ooxml import document_content_tree`.
  4. packages/docparse/docparse/docs_intel.py can import from here too, or
     the canonical copy can live only here and docparse can be deprecated/merged.

Until the PyPI publish happens, the vendored copy in meridian-docs MUST stay
in sync with this file manually. The header comment on that file (0d1b0809)
explains this tradeoff.
"""
from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

# ---------------------------------------------------------------------------
# OOXML namespace constants
# ---------------------------------------------------------------------------

# Primary WordprocessingML namespace (Word 2007+).
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
# Word 2010+ extended namespace — carries w14:paraId, the stable paragraph id.
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"

# Field instructions Word regenerates on F9 (TOC, SEQ counters, cross-refs).
# These are marked needs_refresh=True because their cached text is computed,
# not authored — a downstream consumer must re-evaluate, not trust the stale run.
_REFRESHABLE_FIELDS: frozenset[str] = frozenset(
    {"TOC", "SEQ", "PAGEREF", "REF", "NOTEREF", "PAGE", "NUMPAGES", "STYLEREF"}
)


# ---------------------------------------------------------------------------
# Low-level element helpers
# ---------------------------------------------------------------------------

def _q(ns: str, tag: str) -> str:
    """Build a Clark-notation qualified name: ``{namespace}localname``."""
    return f"{{{ns}}}{tag}"


def _is_heading(style: str | None) -> bool:
    """True when the paragraph style is a Word heading (Heading1 / heading 1 / ...)."""
    return bool(style) and str(style).lower().startswith("heading")


def _heading_level(style: str | None) -> int:
    """Extract the numeric level from a heading style (Heading2 -> 2). Defaults to 1."""
    match = re.search(r"(\d+)", style or "")
    return int(match.group(1)) if match else 1


def _field_type(instruction: str | None) -> str | None:
    """First whitespace-delimited token of a Word field instruction, upper-cased."""
    if not instruction:
        return None
    token = instruction.strip().split(maxsplit=1)
    return token[0].upper() if token else None


def _field_needs_refresh(ftype: str | None) -> bool:
    return ftype in _REFRESHABLE_FIELDS


def _fields_in_paragraph(p: ET.Element) -> list[dict[str, Any]]:
    """Walk a <w:p> element and extract all field codes (simple and complex).

    Returns a list of field records:
      {kind, field_type, instruction, needs_refresh, cached_result}
    """
    fields: list[dict[str, Any]] = []
    open_fields: list[dict[str, Any]] = []

    fld_simple = _q(_W, "fldSimple")
    fld_char = _q(_W, "fldChar")
    instr_text = _q(_W, "instrText")
    text_tag = _q(_W, "t")
    char_type_attr = _q(_W, "fldCharType")
    instr_attr = _q(_W, "instr")

    def _emit(instruction: str, cached: str = "") -> None:
        ftype = _field_type(instruction)
        fields.append({
            "kind": "field",
            "field_type": ftype,
            "instruction": instruction.strip(),
            "needs_refresh": _field_needs_refresh(ftype),
            "cached_result": cached.strip(),
        })

    for el in p.iter():
        tag = el.tag
        if tag == fld_simple:
            cached = "".join(t.text or "" for t in el.iter(text_tag))
            _emit(el.get(instr_attr) or "", cached)
        elif tag == fld_char:
            char_type = el.get(char_type_attr)
            if char_type == "begin":
                open_fields.append({"instr": [], "cached": [], "past_sep": False})
            elif char_type == "separate" and open_fields:
                open_fields[-1]["past_sep"] = True
            elif char_type == "end" and open_fields:
                done = open_fields.pop()
                _emit("".join(done["instr"]), "".join(done["cached"]))
        elif tag == instr_text and open_fields:
            open_fields[-1]["instr"].append(el.text or "")
        elif tag == text_tag and open_fields and open_fields[-1]["past_sep"]:
            open_fields[-1]["cached"].append(el.text or "")

    for pending in open_fields:
        _emit("".join(pending["instr"]), "".join(pending["cached"]))
    return fields


def _paragraph_style(p: ET.Element) -> str | None:
    """Return the w:pStyle val attribute for a <w:p>, or None."""
    ppr = p.find(_q(_W, "pPr"))
    if ppr is None:
        return None
    pstyle = ppr.find(_q(_W, "pStyle"))
    return pstyle.get(_q(_W, "val")) if pstyle is not None else None


def _paragraph_text(p: ET.Element) -> str:
    """Concatenate all <w:t> run text in a paragraph (no spacing/tab handling)."""
    return "".join(t.text or "" for t in p.iter(_q(_W, "t")))


def _paragraph_node(p: ET.Element, index: int) -> dict[str, Any]:
    """Build the block dict for a <w:p> element."""
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
    """Build the block dict for a <w:tbl> element."""
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def document_content_tree(source: str | bytes | bytearray) -> dict[str, Any]:
    """Parse a .docx file into a structured block tree.

    ``source`` is either a file path (str) or raw bytes/bytearray. Returns a
    dict with:
      - ``paragraph_count`` / ``table_count`` / ``heading_count`` / ``field_count``
      - ``blocks`` — flat list of all body blocks in document order, each a dict
        with ``kind`` in {``paragraph``, ``heading``, ``table``}, ``index``, and
        format-specific fields.
      - ``tree`` — same blocks nested by heading level (``children`` list on each
        heading node). Paragraphs/tables that precede the first heading are
        top-level in the tree.

    This is the function that was previously duplicated in:
      - packages/docparse/docparse/docs_intel.py (source of truth before v0.1)
      - extensions/meridian-docs/meridian_docs/_vendored_content_tree.py (copy)

    Pure stdlib — no third-party dependencies.
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

    empty: dict[str, Any] = {
        "paragraph_count": 0,
        "table_count": 0,
        "heading_count": 0,
        "field_count": 0,
        "blocks": [],
        "tree": [],
    }
    if body is None:
        return empty

    p_tag = _q(_W, "p")
    tbl_tag = _q(_W, "tbl")
    blocks: list[dict[str, Any]] = []
    for index, child in enumerate(list(body)):
        if child.tag == p_tag:
            blocks.append(_paragraph_node(child, index))
        elif child.tag == tbl_tag:
            blocks.append(_table_node(child, index))

    paragraph_count = sum(1 for b in blocks if b["kind"] in ("paragraph", "heading"))
    table_count = sum(1 for b in blocks if b["kind"] == "table")
    heading_count = sum(1 for b in blocks if b["kind"] == "heading")
    field_count = sum(len(b.get("fields", [])) for b in blocks)

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
