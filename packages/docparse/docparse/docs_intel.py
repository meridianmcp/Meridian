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
import json
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


# --- Citation markers (75d2196d) -------------------------------------------
#
# LaTeX surfaces in-text citations from ``\cite``-family macros (latex_intel).
# A .docx has no ``\cite`` — reference managers embed citations two ways, both
# of which this parser recognises:
#
# 1. **Reference-manager field codes** — Zotero and Mendeley write a *complex*
#    Word field whose ``ADDIN`` instruction carries a ``CSL_CITATION`` JSON
#    payload::
#
#        ADDIN ZOTERO_ITEM CSL_CITATION {"citationItems":[{"itemData":{...}}]}
#        ADDIN CSL_CITATION {"citationItems":[...]}            (Mendeley)
#
#    The JSON's ``citationItems`` each identify a source; we lift a stable key
#    per item (a DOI / ISBN / Zotero URI / id) so the marker is addressable and
#    can be matched against a bibliography, mirroring the LaTeX citation key.
#
# 2. **Footnote / endnote references** — ``<w:footnoteReference w:id="N"/>`` and
#    ``<w:endnoteReference w:id="N"/>`` in the body point at note content in
#    ``word/footnotes.xml`` / ``word/endnotes.xml``. Academic .docx authored
#    without a reference manager cite this way, so they are citation markers too.
#
# A citation instruction is one whose ADDIN payload is a CSL citation. EndNote
# and other managers also use ``ADDIN`` for non-citation content, so we key off
# the ``CSL_CITATION`` token specifically rather than the bare ``ADDIN`` prefix.
_CSL_CITATION_TOKEN = "CSL_CITATION"
# Managers we name explicitly for the marker's ``source`` field; anything else
# carrying a CSL_CITATION payload is reported with source ``"csl"``.
_ADDIN_SOURCES: tuple[tuple[str, str], ...] = (
    ("ZOTERO_ITEM", "zotero"),
    ("MENDELEY_CITATION", "mendeley"),
)


def _is_citation_instruction(instruction: str | None) -> bool:
    """True when a Word field instruction embeds a CSL citation payload.

    Zotero (``ADDIN ZOTERO_ITEM CSL_CITATION {..}``) and Mendeley
    (``ADDIN CSL_CITATION {..}`` / ``ADDIN MENDELEY_CITATION {..}``) both mark
    the payload with the ``CSL_CITATION`` token; that token is the reliable
    discriminator from the other ``ADDIN`` fields Word uses.
    """
    return bool(instruction) and _CSL_CITATION_TOKEN in instruction


def _citation_source(instruction: str) -> str:
    """Classify a CSL citation field by its reference manager."""
    upper = instruction.upper()
    for token, name in _ADDIN_SOURCES:
        if token in upper:
            return name
    if "MENDELEY" in upper:
        return "mendeley"
    return "csl"


def _csl_json(instruction: str) -> dict[str, Any] | None:
    """Extract and parse the JSON object embedded in a CSL citation instruction.

    The instruction is ``ADDIN ... CSL_CITATION {json}`` — the JSON begins at the
    first ``{`` after the token. Word occasionally trails switches after the
    payload, so we take the balanced ``{...}`` span rather than to end-of-string.
    Returns the decoded object, or ``None`` when no valid JSON is present.
    """
    start = instruction.find("{")
    if start == -1:
        return None
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
        return None
    try:
        parsed = json.loads(instruction[start:end])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _citation_keys_from_csl(instruction: str) -> list[str]:
    """Best-effort list of citation keys from a CSL_CITATION field instruction.

    Walks the ``citationItems`` array and derives one stable key per item,
    preferring (in order): DOI, ISBN, a Zotero item URI, the CSL ``id``, then the
    author/year of ``itemData``. Returns ``[]`` when the JSON is absent/malformed
    or carries no items (the marker is still surfaced with an empty key list by
    the caller, so a citation is never silently dropped).
    """
    data = _csl_json(instruction)
    if not data:
        return []
    items = data.get("citationItems")
    if not isinstance(items, list):
        return []
    keys: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = _key_for_citation_item(item)
        if key:
            keys.append(key)
    return keys


def _key_for_citation_item(item: dict[str, Any]) -> str | None:
    """Derive one stable key for a single CSL ``citationItems`` entry."""
    item_data = item.get("itemData") if isinstance(item.get("itemData"), dict) else {}
    doi = item_data.get("DOI")
    if isinstance(doi, str) and doi.strip():
        return doi.strip()
    isbn = item_data.get("ISBN")
    if isinstance(isbn, str) and isbn.strip():
        return isbn.strip()
    for uri in item.get("uris", []) or []:
        if isinstance(uri, str) and uri.strip():
            return uri.strip()
    for candidate in (item_data.get("id"), item.get("id")):
        if isinstance(candidate, (str, int)) and str(candidate).strip():
            return str(candidate).strip()
    # Fall back to author-year so a payload with only bibliographic fields still
    # yields an addressable key.
    return _author_year_key(item_data)


def _author_year_key(item_data: dict[str, Any]) -> str | None:
    """Compose a ``family:year`` key from CSL ``author`` + ``issued`` fields."""
    if not isinstance(item_data, dict):
        return None
    family = None
    authors = item_data.get("author")
    if isinstance(authors, list) and authors and isinstance(authors[0], dict):
        family = authors[0].get("family") or authors[0].get("literal")
    year = None
    issued = item_data.get("issued")
    if isinstance(issued, dict):
        parts = issued.get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            year = str(parts[0][0])
    if family and year:
        return f"{family}:{year}"
    if family:
        return str(family)
    return None


def _citations_in_paragraph(p: ET.Element) -> list[dict[str, Any]]:
    """Extract every citation marker embedded in one ``<w:p>``, in order.

    Two marker families are surfaced (see the module-level notes):

    * **CSL citation fields** — a complex Word field whose ADDIN instruction
      carries a ``CSL_CITATION`` payload (Zotero / Mendeley). Reuses the same
      begin/instrText/end complex-field machinery as :func:`_fields_in_paragraph`
      (fields nest, and Word splits a long instruction across ``instrText`` runs,
      so instruction buffers are concatenated per open field). A ``fldSimple``
      form is handled too for completeness.
    * **Footnote / endnote references** — ``<w:footnoteReference w:id="N"/>`` /
      ``<w:endnoteReference w:id="N"/>``, keyed on the note id so a consumer can
      resolve them against ``word/footnotes.xml`` / ``word/endnotes.xml``.

    Each CSL record is ``{kind: "citation", source, marker_text, keys,
    instruction}``; each note record is ``{kind: "citation", source, note_id,
    marker_text, keys}``. Markers appear in document order.
    """
    citations: list[dict[str, Any]] = []
    open_fields: list[list[str]] = []
    fld_simple = _q(_W, "fldSimple")
    fld_char = _q(_W, "fldChar")
    instr_text = _q(_W, "instrText")
    char_type_attr = _q(_W, "fldCharType")
    instr_attr = _q(_W, "instr")
    fn_ref = _q(_W, "footnoteReference")
    en_ref = _q(_W, "endnoteReference")
    id_attr = _q(_W, "id")

    def _emit_field(instruction: str) -> None:
        if not _is_citation_instruction(instruction):
            return
        instruction = instruction.strip()
        citations.append(
            {
                "kind": "citation",
                "source": _citation_source(instruction),
                "marker_text": instruction,
                "keys": _citation_keys_from_csl(instruction),
                "instruction": instruction,
            }
        )

    def _emit_note(el: ET.Element, note_kind: str) -> None:
        note_id = el.get(id_attr)
        citations.append(
            {
                "kind": "citation",
                "source": note_kind,
                "note_id": note_id,
                "marker_text": f"[{note_kind} {note_id}]"
                if note_id is not None
                else f"[{note_kind}]",
                "keys": [note_id] if note_id is not None else [],
            }
        )

    for el in p.iter():
        tag = el.tag
        if tag == fld_simple:
            _emit_field(el.get(instr_attr) or "")
        elif tag == fld_char:
            char_type = el.get(char_type_attr)
            if char_type == "begin":
                open_fields.append([])
            elif char_type == "end" and open_fields:
                _emit_field("".join(open_fields.pop()))
        elif tag == instr_text and open_fields:
            open_fields[-1].append(el.text or "")
        elif tag == fn_ref:
            _emit_note(el, "footnote")
        elif tag == en_ref:
            _emit_note(el, "endnote")
    # A citation field left open (no matching end) is still surfaced.
    for pending in open_fields:
        _emit_field("".join(pending))
    return citations


def parse_docx_citations(source: str | bytes | bytearray) -> list[dict[str, Any]]:
    """Parse in-text citation markers from a .docx (path or raw bytes).

    The DOCX counterpart to :func:`docparse.latex_intel.parse_latex_citations`.
    Returns a document-ordered list of citation-marker dicts, each carrying:

    * ``source`` — ``"zotero"`` / ``"mendeley"`` / ``"csl"`` (reference-manager
      field codes) or ``"footnote"`` / ``"endnote"`` (note references).
    * ``marker_text`` — the raw field instruction (CSL fields) or a compact
      ``[footnote N]`` label (note references).
    * ``keys`` — the citation keys the marker resolves to (DOI / ISBN / URI /
      CSL id / author-year for CSL fields; the note id for note references). May
      be empty when a payload is malformed — the marker is still surfaced.
    * ``para_id`` — the ``w14:paraId`` of the enclosing paragraph (stable anchor).
    * ``section_ordinal`` — the document-order index (into the heading list) of
      the nearest preceding heading, or ``None`` before the first heading. This
      matches the ordinal :func:`elements_from_docx_outline` assigns that heading
      element, so a consumer can resolve each citation's parent section — exactly
      the contract the LaTeX citation parser honours.

    Never raises: a malformed / non-docx source degrades to ``[]``.
    """
    try:
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
    except Exception:  # noqa: BLE001 — not a docx / unreadable -> empty, never crash
        return []
    body = root.find(_q(_W, "body"))
    if body is None:
        return []
    citations: list[dict[str, Any]] = []
    heading_ordinal = -1  # index of the nearest preceding heading (-1 => none yet)
    for index, p in enumerate(body.findall(_q(_W, "p"))):
        style = _paragraph_style(p)
        if _is_heading(style):
            heading_ordinal += 1
        para_id = p.get(_q(_W14, "paraId")) or f"p{index}"
        section_ordinal = heading_ordinal if heading_ordinal >= 0 else None
        for marker in _citations_in_paragraph(p):
            citations.append(
                {
                    **marker,
                    "para_id": para_id,
                    "section_ordinal": section_ordinal,
                }
            )
    return citations


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
    ``headings`` shape is unchanged so all callers keep working.

    75d2196d — additionally surfaces in-text citation markers (Zotero / Mendeley
    ``CSL_CITATION`` field codes and footnote / endnote references) in an ordered
    ``citations`` list plus a ``citation_count`` (mirroring how ``latex_intel``
    surfaces LaTeX ``\\cite`` markers). Each entry is ``{source, marker_text,
    keys, para_id, section_ordinal}``. Still purely additive."""
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
    citations = parse_docx_citations(source)
    return {
        "paragraph_count": len(paras),
        "heading_count": len(headings),
        "headings": headings,
        "field_count": len(fields),
        "fields": fields,
        "citation_count": len(citations),
        "citations": citations,
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
