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
import os
import re
import json
import sqlite3
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

from .structural_parser import StructuralParser

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
    # Stack of currently-open complex fields. Each entry tracks the instruction
    # buffer, the cached-result buffer (run text after the ``separate`` marker —
    # what Word rendered last refresh), and whether we are past that separator.
    open_fields: list[dict[str, Any]] = []
    fld_simple = _q(_W, "fldSimple")
    fld_char = _q(_W, "fldChar")
    instr_text = _q(_W, "instrText")
    text_tag = _q(_W, "t")
    char_type_attr = _q(_W, "fldCharType")
    instr_attr = _q(_W, "instr")

    def _emit(instruction: str, cached: str = "") -> None:
        ftype = _field_type(instruction)
        fields.append(
            {
                "kind": "field",
                "field_type": ftype,
                "instruction": instruction.strip(),
                "needs_refresh": _field_needs_refresh(ftype),
                # ``cached_result`` is the stale run text Word baked in at the last
                # F9 refresh (the number for a SEQ, the entry list for a TOC). It is
                # NOT authored content — a consumer regenerating the field replaces
                # it — but capturing it lets us represent the current rendered value
                # and diff it against a fresh computation (a2f4c1e0).
                "cached_result": cached.strip(),
            }
        )

    for el in p.iter():
        tag = el.tag
        if tag == fld_simple:
            # Simple fields carry their cached result as child <w:t> runs.
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
            # Run text after the separator is the innermost open field's cached
            # rendered result. (Before the separator, a <w:t> is not part of any
            # field's value, so it is ignored here.)
            open_fields[-1]["cached"].append(el.text or "")
    # A malformed field left open (no matching end) is still surfaced so the
    # instruction is never silently dropped.
    for pending in open_fields:
        _emit("".join(pending["instr"]), "".join(pending["cached"]))
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


# ===========================================================================
# 967bb99b — TOC / LOF / SEQ structured extraction + deterministic regeneration
# ---------------------------------------------------------------------------
# The base parser (a62e5b4f) already *detects* field codes and flags TOC/SEQ/…
# as ``needs_refresh``. What was missing — and what neither docx-mcp nor
# meridian-docs did — is a *structured* representation of these field-driven
# structures (a TOC, a table/list of figures, the SEQ counters that number
# captions) and the ability to *regenerate* their entry list from the document's
# own live content, independent of the stale cached text Word baked in.
#
# BOUNDARY (honest): a faithful Word render — real page numbers, dot-leader
# layout, pagination — needs a full Word layout engine, which is out of scope
# here. So regeneration rebuilds the ENTRY LIST (the heading/caption text, its
# level or SEQ number, its anchor para_id) that a downstream renderer or Word's
# own F9 would lay out; page numbers are reported as ``None`` and never faked.
# ===========================================================================

# TOC content-family: a table of FIGURES/TABLES is encoded either as a TOC field
# scoped to a SEQ label (``TOC \c "Figure"`` / ``\f Figure``) or, historically,
# as a bare Table-of-Figures. We classify by the ``\c`` / ``\f`` switch argument.
_TOC_FIELD_TYPES = frozenset({"TOC"})


def _tokenize_field_instruction(instruction: str) -> list[str]:
    """Split a field instruction into tokens, honouring quoted arguments.

    ``TOC \\o "1-3" \\h \\z \\u`` -> ``['TOC', '\\o', '1-3', '\\h', '\\z', '\\u']``.
    Double-quoted spans are kept whole (quotes stripped); everything else splits
    on whitespace. Deterministic and dependency-free.
    """
    tokens: list[str] = []
    buf: list[str] = []
    in_quote = False
    for ch in instruction:
        if ch == '"':
            if in_quote:
                tokens.append("".join(buf))
                buf = []
            in_quote = not in_quote
        elif ch.isspace() and not in_quote:
            if buf:
                tokens.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens


def parse_field_switches(instruction: str) -> dict[str, Any]:
    """Decode a Word field instruction into ``{field_type, switches, args}``.

    A field instruction is ``FIELDTYPE positional... \\switch [arg] ...``. We
    return:

    * ``field_type`` — the leading token, upper-cased (``TOC``, ``SEQ`` …).
    * ``args`` — the positional (non-switch) tokens after the type, in order
      (e.g. ``SEQ Figure`` -> ``["Figure"]``).
    * ``switches`` — a dict mapping each ``\\x`` switch (the ``x`` letter, case
      preserved) to its argument token when the next token is not itself a
      switch, else ``True`` (a bare flag). ``TOC \\o "1-3" \\h`` ->
      ``{"o": "1-3", "h": True}``.

    Pure/deterministic; the workhorse behind TOC-scope and SEQ-label detection.
    """
    tokens = _tokenize_field_instruction(instruction or "")
    if not tokens:
        return {"field_type": None, "args": [], "switches": {}}
    field_type = tokens[0].upper()
    args: list[str] = []
    switches: dict[str, Any] = {}
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("\\") and len(tok) >= 2:
            letter = tok[1]
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if nxt is not None and not nxt.startswith("\\"):
                switches[letter] = nxt
                i += 2
                continue
            switches[letter] = True
        else:
            args.append(tok)
        i += 1
    return {"field_type": field_type, "args": args, "switches": switches}


def _toc_level_range(switches: dict[str, Any]) -> tuple[int, int] | None:
    """The heading-level window a ``TOC`` includes, from its ``\\o "a-b"`` switch.

    Returns ``(low, high)`` inclusive, or ``None`` when the field carries no
    ``\\o`` argument (Word then defaults to all TOC-eligible levels; we signal
    "unbounded" so the regenerator includes every heading). A malformed range is
    treated as unbounded rather than raising.
    """
    raw = switches.get("o")
    if not isinstance(raw, str):
        return None
    match = re.match(r"\s*(\d+)\s*-\s*(\d+)\s*$", raw)
    if not match:
        return None
    low, high = int(match.group(1)), int(match.group(2))
    return (min(low, high), max(low, high))


def _classify_toc(parsed: dict[str, Any]) -> dict[str, Any]:
    """Classify a parsed ``TOC`` field as a document TOC vs a table-of-figures.

    A TOC scoped by ``\\c "Figure"`` (caption-label) or ``\\f Figure`` (entry
    identifier / SEQ label) is a *list of figures/tables*; otherwise it is the
    main document TOC built from heading styles. Returns
    ``{structure_kind, seq_label, level_range}``.
    """
    switches = parsed["switches"]
    seq_label = switches.get("c") if isinstance(switches.get("c"), str) else None
    if seq_label is None and isinstance(switches.get("f"), str):
        seq_label = switches.get("f")
    if seq_label is not None:
        structure_kind = "list_of_figures"
    else:
        structure_kind = "toc"
    return {
        "structure_kind": structure_kind,
        "seq_label": seq_label,
        "level_range": _toc_level_range(switches),
    }


def regenerate_toc(
    source: str | bytes | bytearray,
    *,
    level_range: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Rebuild a document's table-of-contents *entry list* from its headings.

    Deterministic, layout-free regeneration: every heading paragraph becomes a
    TOC entry ``{level, text, para_id, page}`` in document order, filtered to the
    ``level_range`` (inclusive) when given — matching Word's ``\\o "low-high"``
    scope. ``page`` is always ``None`` (see the module BOUNDARY note: page
    numbers require a Word layout engine and are never fabricated).

    Returns ``{structure_kind: "toc", level_range, entry_count, entries}``. This
    is the fresh computation a consumer substitutes for a stale cached TOC.
    """
    outline = document_outline(source)
    low, high = (level_range or (1, 10 ** 9))
    entries = [
        {"level": h["level"], "text": h.get("text", ""), "para_id": h.get("para_id"),
         "page": None}
        for h in outline.get("headings", [])
        if low <= int(h.get("level", 1)) <= high
    ]
    return {
        "structure_kind": "toc",
        "level_range": list(level_range) if level_range else None,
        "entry_count": len(entries),
        "entries": entries,
    }


def _iter_caption_paragraphs(
    source: str | bytes | bytearray,
) -> list[dict[str, Any]]:
    """Every paragraph in the body **including those nested in table cells**.

    ``document_content_tree`` collapses a table into a single node (its captions'
    text/para_id are lost inside aggregated cell fields). A list-of-figures must
    number captions wherever they live — many figure captions sit in a one-cell
    table — so this walks ``word/document.xml`` and yields *paragraph-level*
    records ``{para_id, text, fields}`` in true document order, descending into
    ``<w:tc>`` cells. Deterministic, dependency-free.
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
    out: list[dict[str, Any]] = []
    if body is None:
        return out
    p_tag = _q(_W, "p")
    # ``iter`` yields every <w:p> in document order, whether a direct body child
    # or nested inside a table cell — exactly the caption-search domain.
    for index, p in enumerate(body.iter(p_tag)):
        out.append(
            {
                "para_id": p.get(_q(_W14, "paraId")) or f"p{index}",
                "text": _paragraph_text(p),
                "fields": _fields_in_paragraph(p),
            }
        )
    return out


def _seq_captions(
    paragraphs: list[dict[str, Any]], seq_label: str
) -> list[dict[str, Any]]:
    """Caption entries for one SEQ label, numbered deterministically.

    A caption is a paragraph carrying a ``SEQ <label>`` field (case-insensitively
    matched, e.g. ``SEQ Figure``). We renumber them 1..N in document order (Word's
    default ``\\* ARABIC``), which is exactly what an F9 refresh does when figures
    are added/removed/reordered — independent of the stale cached number. Each
    entry is ``{number, text, para_id, cached_number, page}`` where ``text`` is
    the full caption paragraph text and ``cached_number`` is the possibly-stale
    value Word had rendered (``None`` if it had never been computed).
    """
    label_lower = seq_label.lower()
    entries: list[dict[str, Any]] = []
    counter = 0
    for p in paragraphs:
        seq_here = False
        cached: str | None = None
        for f in p.get("fields", []):
            if f.get("field_type") != "SEQ":
                continue
            parsed = parse_field_switches(f.get("instruction", ""))
            args = parsed.get("args") or []
            if args and args[0].lower() == label_lower:
                seq_here = True
                cr = f.get("cached_result")
                cached = cr if cr else cached
        if seq_here:
            counter += 1
            entries.append(
                {
                    "number": counter,
                    "text": p.get("text", ""),
                    "para_id": p.get("para_id"),
                    "cached_number": cached,
                    "page": None,
                }
            )
    return entries


def regenerate_list_of_figures(
    source: str | bytes | bytearray, *, seq_label: str = "Figure"
) -> dict[str, Any]:
    """Rebuild a list-of-figures (or -tables) from SEQ-numbered captions.

    Walks every paragraph — body *and* table-cell (captions frequently sit in a
    one-cell table) — and renumbers each ``SEQ <seq_label>`` caption 1..N in
    document order: the layout-free half of what Word's F9 does for a Table of
    Figures. Page numbers are ``None`` (BOUNDARY: no layout engine). Returns
    ``{structure_kind: "list_of_figures", seq_label, entry_count, entries}``.
    """
    entries = _seq_captions(_iter_caption_paragraphs(source), seq_label)
    return {
        "structure_kind": "list_of_figures",
        "seq_label": seq_label,
        "entry_count": len(entries),
        "entries": entries,
    }


def document_field_structures(source: str | bytes | bytearray) -> dict[str, Any]:
    """Detect + structurally represent every field-driven structure in a .docx.

    The single entry point for 967bb99b. Parses the document once, then:

    * classifies each ``TOC`` field as a document **toc** or a **list_of_figures**
      (when scoped by ``\\c``/``\\f`` to a SEQ label), decoding its ``\\o`` level
      range;
    * inventories the **SEQ** counters (grouped by label, e.g. ``Figure`` /
      ``Table``) with their per-occurrence cached numbers;
    * for every detected TOC / LOF, attaches a freshly **regenerated** entry list
      (from live headings / SEQ captions) alongside — so a consumer can diff the
      stale cached structure against the current document without a Word render.

    Returns::

        {
            has_toc, has_list_of_figures,
            structures: [ {structure_kind, para_id, instruction, level_range?,
                           seq_label?, regenerated: {...}}, ... ],
            seq_counters: [ {label, occurrences, cached_numbers}, ... ],
            boundary: "<honest note about page numbers / layout>",
        }

    Purely additive — every existing function is untouched.
    """
    # Walk paragraph-level (body + table cells) so a TOC/LOF/SEQ living inside a
    # table cell is detected and counted, not collapsed into an aggregate node.
    all_fields = [
        {**f, "para_id": p["para_id"]}
        for p in _iter_caption_paragraphs(source)
        for f in p.get("fields", [])
    ]

    structures: list[dict[str, Any]] = []
    has_toc = False
    has_lof = False
    for f in all_fields:
        if f.get("field_type") not in _TOC_FIELD_TYPES:
            continue
        parsed = parse_field_switches(f.get("instruction", ""))
        cls = _classify_toc(parsed)
        rec: dict[str, Any] = {
            "structure_kind": cls["structure_kind"],
            "para_id": f.get("para_id"),
            "instruction": f.get("instruction", ""),
            "level_range": (
                list(cls["level_range"]) if cls["level_range"] else None
            ),
            "seq_label": cls["seq_label"],
        }
        if cls["structure_kind"] == "list_of_figures":
            has_lof = True
            rec["regenerated"] = regenerate_list_of_figures(
                source, seq_label=cls["seq_label"] or "Figure"
            )
        else:
            has_toc = True
            rec["regenerated"] = regenerate_toc(
                source, level_range=cls["level_range"]
            )
        structures.append(rec)

    # SEQ counter inventory, grouped by label (first positional arg).
    seq_by_label: dict[str, list[str | None]] = {}
    for f in all_fields:
        if f.get("field_type") != "SEQ":
            continue
        parsed = parse_field_switches(f.get("instruction", ""))
        args = parsed.get("args") or []
        label = args[0] if args else ""
        cr = f.get("cached_result")
        seq_by_label.setdefault(label, []).append(cr if cr else None)
    seq_counters = [
        {
            "label": label,
            "occurrences": len(values),
            "cached_numbers": values,
        }
        for label, values in seq_by_label.items()
    ]

    return {
        "has_toc": has_toc,
        "has_list_of_figures": has_lof,
        "structures": structures,
        "seq_counters": seq_counters,
        "boundary": (
            "Regenerated entries rebuild the TOC/LOF entry list (text, level or "
            "SEQ number, anchor para_id) deterministically from live document "
            "structure. Page numbers are None: faithful pagination requires a "
            "Word layout engine, which is out of scope."
        ),
    }


# --- Shared structural-parser conformance (67402ce7) ------------------------


class DocxStructuralParser(StructuralParser):
    """DOCX conformance to the shared :class:`StructuralParser` interface.

    Interface-only (67402ce7): the OOXML zip/XML parsing logic is untouched — the
    methods delegate to the existing module functions. :meth:`parse_structure`
    surfaces the same heading outline :func:`document_outline` already produces,
    additionally nesting it into a ``tree`` via the shared
    :meth:`StructuralParser.build_tree` so DOCX exposes the identical
    ``{heading_count, headings, tree}`` shape the LaTeX layer does. The existing
    functional API (``document_outline`` / ``document_content_tree`` / ...) is the
    public entry point and is unchanged — ``document_outline`` in particular still
    returns exactly its historical keys; the ``tree`` is composed here so no
    existing caller's result shape shifts.
    """

    def parse_structure(self, source: Any) -> dict[str, Any]:
        """Heading outline + level-nested tree, in the shared structural shape.

        Delegates the parse to :func:`document_outline` (behaviour unchanged) and
        adds a ``tree`` built from its ``headings`` via the shared level-nesting
        helper, so the returned dict spreads ``document_outline``'s keys plus
        ``tree``.
        """
        outline = document_outline(source)
        return {**outline, "tree": self.build_tree(outline.get("headings", []))}

    def analyze(self, source: Any) -> dict[str, Any]:
        """One-call structural map of a .docx.

        Delegates to :func:`document_outline` — docs_intel's stateless one-call
        structural entrypoint (headings + field codes + citation markers) — with a
        level-nested ``tree`` added, matching the LaTeX ``analyze`` contract.
        """
        return self.parse_structure(source)
