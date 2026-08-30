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

import hashlib
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


# ---------------------------------------------------------------------------
# 4a07e566 — Section-type differentiation + w:sectPr page-numbering awareness
#
# Section types mirror the regions a Word academic document actually uses:
#   abstract   — "Abstract", "Summary", "Executive Summary" headings
#   toc        — "Table of Contents", "Contents", "TOC" headings
#   lof        — "List of Figures", "List of Tables", etc.
#   appendix   — "Appendix"/"Annex" headings, or headings after References
#   main       — everything else (body of the document)
# ---------------------------------------------------------------------------

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

    Returns None for headings whose type must be inferred from position.
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
    2. A level-1 heading matching ``_REFERENCES_RE`` moves the region to
       "appendix" (back matter begins after references).
    3. Headings that follow an "appendix" region-start remain "appendix".
    4. Headings before the first non-classified level-1 default to "abstract"
       when they don't match any explicit pattern.
    5. Everything else is "main".

    Returns a NEW list of dicts (copies) with the added ``section_type`` key.
    """
    in_front_matter = True
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
            section_type = forced
        elif level == 1 and _REFERENCES_RE.match(text.strip()):
            in_appendix = True
            in_front_matter = False
            section_type = "appendix"
        elif level == 1:
            in_front_matter = False
            section_type = "main"
        else:
            section_type = "abstract" if in_front_matter else "main"

        result.append({**h, "section_type": section_type})

    return result


def parse_sectpr(source: str | bytes | bytearray) -> dict[str, Any]:
    """4a07e566 — Parse all ``<w:sectPr>`` elements in a .docx body.

    A .docx uses ``<w:sectPr>`` to define section properties.  In a document
    with front matter (roman numeral page numbers) and body (arabic numerals),
    Word inserts a ``<w:sectPr>`` as the last child of a ``<w:pPr>`` at each
    section boundary, plus one final ``<w:sectPr>`` as a direct child of
    ``<w:body>`` for the last section.

    Returns::

        {"section_count": int, "sections": [{index, page_num_fmt,
          page_num_start, page_num_type, is_continuous, anchor_para_id}]}

    When there are no ``<w:sectPr>`` elements (a single implicit section),
    returns ``{section_count: 0, sections: []}``.
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
    w_type_el = _q(_W, "type")
    w_fmt = _q(_W, "fmt")
    w_start = _q(_W, "start")
    w_val = _q(_W, "val")
    w14_paraId = _q(_W14, "paraId")

    for child in body:
        if child.tag == _q(_W, "p"):
            ppr = child.find(w_pPr)
            if ppr is not None:
                spr = ppr.find(w_sectPr)
                if spr is not None:
                    anchor_id = child.get(w14_paraId) or None
                    sections.append(_parse_one_sectpr(
                        spr, anchor_id, len(sections),
                        w_pgNumType, w_type_el, w_fmt, w_start, w_val,
                    ))
        elif child.tag == w_sectPr:
            sections.append(_parse_one_sectpr(
                child, None, len(sections),
                w_pgNumType, w_type_el, w_fmt, w_start, w_val,
            ))

    return {"section_count": len(sections), "sections": sections}


def _parse_one_sectpr(
    spr: ET.Element,
    anchor_para_id: str | None,
    index: int,
    w_pgNumType: str,
    w_type_el: str,
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

    type_el = spr.find(w_type_el)
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
    synth_map = _build_synth_id_map(body)
    citations: list[dict[str, Any]] = []
    heading_ordinal = -1  # index of the nearest preceding heading (-1 => none yet)
    for index, p in enumerate(body.findall(_q(_W, "p"))):
        style = _paragraph_style(p)
        if _is_heading(style):
            heading_ordinal += 1
        para_id = p.get(_q(_W14, "paraId")) or synth_map.get(id(p)) or f"p{index}"
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
    synth_map = _build_synth_id_map(body)
    for index, p in enumerate(body.findall(_q(_W, "p"))):
        para_id = p.get(_q(_W14, "paraId")) or synth_map.get(id(p)) or f"p{index}"
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
    + an ordered ``headings`` list (level/text/para_id/section_type) — the
    queryable document structure docs_intel exposes without building a persistent
    index.

    a62e5b4f — also surfaces every Word field code (TOC / SEQ / PAGEREF ...) in
    an ordered ``fields`` list (``para_id``, ``field_type``, ``instruction``,
    ``needs_refresh``) plus a ``field_count``. Purely additive: the existing
    ``headings`` shape is unchanged so all callers keep working.

    75d2196d — additionally surfaces in-text citation markers (Zotero / Mendeley
    ``CSL_CITATION`` field codes and footnote / endnote references) in an ordered
    ``citations`` list plus a ``citation_count`` (mirroring how ``latex_intel``
    surfaces LaTeX ``\\cite`` markers). Each entry is ``{source, marker_text,
    keys, para_id, section_ordinal}``. Still purely additive.

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

    # Collect distinct regions in document order (deduplicated, first-seen).
    seen_regions: list[str] = []
    for h in headings:
        r = h["section_type"]
        if not seen_regions or seen_regions[-1] != r:
            seen_regions.append(r)

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
        "section_regions": seen_regions,
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


_TAB_TAG = _q(_W, "tab")
_BR_TAG = _q(_W, "br")
_CR_TAG = _q(_W, "cr")
_T_TAG = _q(_W, "t")


def _paragraph_text(p: ET.Element) -> str:
    """Concatenate w:t descendant text within *p*, in document order,
    converting <w:tab/> to a literal tab and <w:br/>/<w:cr/> to a newline --
    matching python-docx's own .text convention.

    PAPER-S4 (ooxml-graph-paper) -- this previously dropped <w:tab/>/<w:br/>
    entirely (iterating only w:t via p.iter(_q(_W, "t"))), reconstructing a
    tab-separated run like "(a)\\tsome text" as "(a)some text" with no
    separator at all. Independently found and root-caused via the paper
    project's own gold-extractor fix (tools/independent_gold_extractor.py's
    _local_text) -- fixing gold alone exposed that native's real extraction
    shared the identical bug (task_945705a3).
    """
    parts: list[str] = []
    for child in p.iter():
        if child.tag == _T_TAG:
            parts.append(child.text or "")
        elif child.tag == _TAB_TAG:
            parts.append("\t")
        elif child.tag in (_BR_TAG, _CR_TAG):
            parts.append("\n")
    return "".join(parts)


def _build_synth_id_map(body: ET.Element) -> dict[int, str]:
    """Build a stable synthesized-id map for <w:p> elements in *body* that lack
    a native w14:paraId attribute.

    Returns ``{id(element): synth_id_str}`` — only for elements without a real
    paraId.  The synthesized id is a 16-hex-char SHA-1 digest (prefixed ``sp``)
    derived from:

    - The paragraph's normalised text (lowercased, whitespace collapsed).
    - An occurrence counter disambiguating duplicate paragraphs sharing that
      exact normalised text, scoped across the whole document in document
      order (0, 1, 2, ... per successive occurrence) so no two paragraphs can
      ever collide onto the same id.

    Using ``hashlib.sha1`` (not Python's built-in ``hash()``) ensures
    reproducibility across processes and Python restarts.

    e21b2ca7 — the ancestor heading TEXT trail this hash previously included
    (an "insertion-resistant structural path") is no longer part of the
    input: it made a paragraph's own id drift whenever an ANCESTOR heading
    was retitled, even though the paragraph itself was never touched. Only
    this paragraph's own content and its own document-order occurrence count
    feed the hash now, so retitling a heading no longer perturbs any
    descendant's id, while distinct paragraphs (and distinct occurrences of
    duplicated text) still never collide.

    Residual, explicitly accepted limitations (inherent to an id derived
    purely by re-parsing content on every call, with no persisted state):
    editing a paragraph's OWN text still changes ITS OWN id (there is
    nothing else here to hash it from), and inserting/removing an EARLIER
    paragraph that shares this paragraph's exact normalized text still
    shifts this paragraph's occurrence index and therefore its id. Both
    require a real persisted, document-bound identity (minted once and
    remembered, not recomputed fresh from content on every parse) to fully
    close — native ``w14:paraId`` already gives real Word-authored
    paragraphs that guarantee; this fallback does not yet.
    """
    p_tag = _q(_W, "p")
    w14_paraId = _q(_W14, "paraId")
    seen: dict[str, int] = {}
    result: dict[int, str] = {}
    for child in body:
        if child.tag != p_tag:
            continue
        native_id = child.get(w14_paraId)
        if native_id:
            continue
        norm_text = re.sub(r"\s+", " ", _paragraph_text(child).lower()).strip()
        occ = seen.get(norm_text, 0)
        seen[norm_text] = occ + 1
        raw = f"{norm_text}\x00{occ}"
        digest = hashlib.sha1(raw.encode()).hexdigest()[:16]
        result[id(child)] = f"sp{digest}"
    return result


def _find_duplicate_native_para_ids(body: ET.Element) -> list[dict[str, Any]]:
    """Detect native ``w14:paraId`` values shared by more than one direct
    ``<w:p>`` child of *body* (827b6bdc — a malformed / hand-edited / merged
    .docx: Word's own invariant is that ``w14:paraId`` is unique per
    paragraph, but nothing upstream of this function ever verified that; the
    reference regression is a document with two distinct paragraphs both
    carrying ``w14:paraId="6BDC5378"``).

    Returns one entry per duplicated id, in first-occurrence document order:
    ``{"para_id", "occurrence_count", "occurrences": [{"index", "text"}, ...]}``.
    ``index`` is the body-order position — the SAME index
    :func:`document_content_tree` assigns each block, so a caller can jump
    straight from a duplicate report to the offending blocks — and ``text``
    is a short (200-char) snippet of that paragraph's own text, so a
    duplicate can actually be located and told apart, not just flagged.
    Paragraphs with no native id (see :func:`_build_synth_id_map` for how
    those are synthesized instead) are never part of this report. Empty list
    when every native id in the document is unique.
    """
    p_tag = _q(_W, "p")
    w14_paraId = _q(_W14, "paraId")
    seen: dict[str, list[dict[str, Any]]] = {}
    for index, child in enumerate(body):
        if child.tag != p_tag:
            continue
        native_id = child.get(w14_paraId)
        if not native_id:
            continue
        seen.setdefault(native_id, []).append(
            {"index": index, "text": _paragraph_text(child)[:200]}
        )
    return [
        {"para_id": pid, "occurrence_count": len(occurrences), "occurrences": occurrences}
        for pid, occurrences in seen.items()
        if len(occurrences) > 1
    ]


def _paragraph_node(p: ET.Element, index: int, synth_id: str | None = None) -> dict[str, Any]:
    """Build a content-tree node for one ``<w:p>`` (heading or body paragraph).

    ``synth_id`` is a pre-computed stable id from :func:`_build_synth_id_map`;
    when absent and the element has no native paraId, falls back to the legacy
    positional ``f"p{index}"``.
    """
    para_id = p.get(_q(_W14, "paraId")) or synth_id or f"p{index}"
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
            duplicate_para_ids: [ ...see below... ],
        }

    ``blocks`` is the flat sequential stream (paragraphs, headings, tables);
    ``tree`` is the same nodes nested by heading level. Purely additive — this is
    a sibling of ``document_outline``, which is untouched.

    827b6bdc — ``duplicate_para_ids`` (:func:`_find_duplicate_native_para_ids`)
    reports every native ``w14:paraId`` shared by 2+ paragraphs (a malformed /
    hand-edited / merged document — Word's own uniqueness invariant is never
    actually enforced upstream of this parser). This is READ-ONLY reporting:
    when duplicates exist, the affected ``blocks``/``tree`` nodes still carry
    whatever ``para_id`` the document literally has (first-match ambiguity is
    surfaced here, not silently resolved) — this function never renumbers or
    otherwise mutates the source to "fix" it; see ``meridian.doc_store``'s
    ``repair_duplicate_para_ids`` for the explicit, separately-invoked repair
    path. Empty list on a document with no duplicated native ids (the
    overwhelmingly common case) — existing callers that never look at this key
    are unaffected.
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
            "duplicate_para_ids": [],
        }

    p_tag = _q(_W, "p")
    tbl_tag = _q(_W, "tbl")
    synth_map = _build_synth_id_map(body)
    duplicate_para_ids = _find_duplicate_native_para_ids(body)
    # Walk direct body children in stored order so paragraphs and tables keep
    # their real interleaving.
    for index, child in enumerate(list(body)):
        if child.tag == p_tag:
            blocks.append(_paragraph_node(child, index, synth_map.get(id(child))))
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
        "duplicate_para_ids": duplicate_para_ids,
    }


# ---------------------------------------------------------------------------
# 7a98286b — structural linter (0ff8b982 Piece 1): a read-only report that
# flags section/figure ordering drift by consuming document_content_tree's
# existing ``blocks``/``tree`` output. No new .docx parsing — every signal
# below (heading text, level, adjacency, field list) is already produced by
# document_content_tree. Not wired into update_paragraph as a write blocker;
# ship the check, defer enforcement (per the item's own scoping note).
# ---------------------------------------------------------------------------

# Section tags are authored as "[§4.2.1] ..." / "[§C.5 — ...]" / multi-tag
# "[§5.1.1 + §5.1.2 — ...]" in the leading bracketed annotation of a heading.
# The § prefix is the reliable signal — plain numbers elsewhere in the
# annotation prose (e.g. "do not reuse B.1") must NOT be mistaken for tags.
# An appendix letter is its OWN dot-separated component ("C.5", "C.1.3"), not
# fused to the first digit ("C5") — the two alternatives below match that
# (letter + one-or-more ".digit" groups) vs. a plain numeric-dotted tag.
_SECTION_TAG_RE = re.compile(r"§([A-Za-z](?:\.\d+)+|\d+(?:\.\d+)*)")

# A caption paragraph: a plain-text leading label, no SEQ field required (real
# documents frequently type "Figure 3b." by hand rather than use Word's SEQ
# field mechanism — this document has zero SEQ fields at all).
_CAPTION_LABEL_RE = re.compile(r"^\s*(Figure|Table|Fig\.)\s+([A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)", re.IGNORECASE)

# "see Section 3.2" / "defined in C.1.3" / "Section 5.1.1" cross-references.
_CROSS_REF_RE = re.compile(r"(?:defined in|see\s+section|section)\s+([A-Za-z]?\d+(?:\.\d+)*)", re.IGNORECASE)


def _extract_section_tags(heading_text: str) -> list[str]:
    """All §-prefixed section tags in a heading's leading ``[...]`` annotation,
    in the order they appear. A heading with no bracketed tag (document title,
    a ``[META — ...]`` organizational note) returns ``[]`` — it is not a real
    section and is excluded from every check below."""
    m = re.match(r"^\s*\[([^\]]*)\]", heading_text or "")
    scope = m.group(1) if m else (heading_text or "")
    return _SECTION_TAG_RE.findall(scope)


def _tag_sort_key(tag: str) -> tuple:
    """Natural/version sort key for a section tag like ``4.2.1`` or ``C.2.5``.

    Numeric chapter prefixes always sort before lettered appendix prefixes
    (the standard convention: chapters 1..N, then appendices A, B, C...).
    Remaining dot-separated components compare numerically.
    """
    parts = tag.split(".")
    top = parts[0]
    if top and top[0].isalpha():
        head = (1, top.upper())
    else:
        try:
            head = (0, int(top))
        except ValueError:
            head = (0, top)
    rest: list[Any] = []
    for p in parts[1:]:
        try:
            rest.append(int(p))
        except ValueError:
            rest.append(p)
    return (head, tuple(rest))


def _caption_label(text: str) -> tuple[str, str] | None:
    """``("Figure", "3b")`` for a caption paragraph's leading label, else None."""
    m = _CAPTION_LABEL_RE.match(text or "")
    if not m:
        return None
    kind = "Figure" if m.group(1).lower().startswith("fig") else "Table"
    return (kind, m.group(2))


def check_document_structure_issues(source: str | bytes | bytearray) -> dict[str, Any]:
    """Read-only structural lint over ``document_content_tree(source)``.

    Flags five classes of drift, each entry a dict with at least ``type``,
    ``message``, ``para_id``, ``index``:

    1. ``heading_order_vs_tag`` — a heading's own ``[§X.Y.Z]`` tag sorts
       *earlier* than the previous tagged heading's, i.e. the physical
       document order and the tag-implied order disagree (an appendix entry
       stranded mid-chapter, a subsection preceding its own parent, ...).
    2. ``caption_before_image`` — a caption paragraph has no adjacent blank
       ("image-like") block immediately before it, but does have one
       immediately after — the inverse of the caption-below convention.
    3. ``duplicate_label`` — two or more captions share the same Figure/Table
       label (e.g. "Figure 3b" used twice).
    4. ``heading_depth_mismatch`` — a heading's Word style level (Heading1,
       Heading2, ...) does not match the nesting depth implied by its own
       tag's dot-count (e.g. a Heading2 tagged ``[§3.2.3.1]``, depth 4).
    5. ``dangling_cross_reference`` — a "see Section X.Y" / "defined in X.Y"
       reference to a section number no heading's tag set actually contains.

    Only consumes ``blocks``/``tree`` already produced by
    :func:`document_content_tree` — no new .docx parsing.
    """
    content = document_content_tree(source)
    blocks = content["blocks"]
    issues: list[dict[str, Any]] = []

    # --- Collect heading entries with their tag(s), in document order. ---
    heading_entries: list[dict[str, Any]] = []
    all_known_tags: set[str] = set()
    for b in blocks:
        if b["kind"] != "heading":
            continue
        tags = _extract_section_tags(b.get("text", ""))
        entry = {
            "index": b["index"],
            "para_id": b.get("para_id"),
            "level": b.get("level", 1),
            "text": b.get("text", ""),
            "tags": tags,
        }
        heading_entries.append(entry)
        all_known_tags.update(tags)

    # --- Check 1: heading order vs. its own tag. ---
    prev_tagged: dict[str, Any] | None = None
    for h in heading_entries:
        if not h["tags"]:
            continue
        if prev_tagged is not None:
            cur_key = _tag_sort_key(h["tags"][0])
            prev_key = _tag_sort_key(prev_tagged["tags"][0])
            if cur_key < prev_key:
                issues.append({
                    "type": "heading_order_vs_tag",
                    "message": (
                        f"Heading tagged [§{h['tags'][0]}] appears immediately after "
                        f"[§{prev_tagged['tags'][0]}] in document order, but its own "
                        f"tag sorts earlier — out of sequence."
                    ),
                    "para_id": h["para_id"],
                    "index": h["index"],
                    "tag": h["tags"][0],
                    "previous_tag": prev_tagged["tags"][0],
                    "previous_para_id": prev_tagged["para_id"],
                })
        prev_tagged = h

    # --- Check 4: heading nesting level vs. tag-implied depth. ---
    for h in heading_entries:
        if not h["tags"]:
            continue
        primary = h["tags"][0]
        implied_depth = len(primary.split("."))
        if h["level"] != implied_depth:
            issues.append({
                "type": "heading_depth_mismatch",
                "message": (
                    f"Heading [§{primary}] is styled Heading{h['level']} but its own "
                    f"tag implies nesting depth {implied_depth}."
                ),
                "para_id": h["para_id"],
                "index": h["index"],
                "tag": primary,
                "style_level": h["level"],
                "implied_depth": implied_depth,
            })

    # --- Check 2: caption-before-image (adjacent-block check on blocks). ---
    def _is_blank_paragraph(blk: dict[str, Any] | None) -> bool:
        return bool(blk) and blk["kind"] == "paragraph" and not (blk.get("text") or "").strip()

    caption_positions: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for pos, b in enumerate(blocks):
        if b["kind"] != "paragraph":
            continue
        label = _caption_label(b.get("text", ""))
        if label is None:
            continue
        caption_positions.setdefault(label, []).append({
            "index": b["index"], "para_id": b.get("para_id"), "pos": pos, "text": b.get("text", ""),
        })
        before = blocks[pos - 1] if pos > 0 else None
        after = blocks[pos + 1] if pos + 1 < len(blocks) else None
        # A genuine inversion signal: no image-like slot before this caption,
        # but one right after — the caption-below convention flipped. (Plain
        # "blank on both sides" spacing, common in this document, is NOT
        # flagged — that pattern is consistent with a normal image-above
        # caption plus routine trailing whitespace, not an inversion.)
        if not _is_blank_paragraph(before) and _is_blank_paragraph(after):
            issues.append({
                "type": "caption_before_image",
                "message": (
                    f"Caption \"{label[0]} {label[1]}\" has no image-like block "
                    f"before it, but one immediately after — possible caption-before-image "
                    f"(convention is caption-below)."
                ),
                "para_id": b.get("para_id"),
                "index": b["index"],
                "label": f"{label[0]} {label[1]}",
            })

    # --- Check 3: duplicate figure/table labels. ---
    for label, occurrences in caption_positions.items():
        if len(occurrences) > 1:
            issues.append({
                "type": "duplicate_label",
                "message": (
                    f"\"{label[0]} {label[1]}\" is used as a caption label "
                    f"{len(occurrences)} times."
                ),
                "label": f"{label[0]} {label[1]}",
                "para_id": occurrences[0]["para_id"],
                "index": occurrences[0]["index"],
                "occurrences": [
                    {"para_id": o["para_id"], "index": o["index"]} for o in occurrences
                ],
            })

    # --- Check 5: dangling cross-references. ---
    for b in blocks:
        text = b.get("text", "") or ""
        for m in _CROSS_REF_RE.finditer(text):
            ref = m.group(1)
            if ref not in all_known_tags:
                issues.append({
                    "type": "dangling_cross_reference",
                    "message": (
                        f"Reference to \"Section {ref}\" does not match any heading's "
                        f"own [§...] tag."
                    ),
                    "para_id": b.get("para_id"),
                    "index": b["index"],
                    "referenced_tag": ref,
                    "context": text[max(0, m.start() - 30): m.end() + 30].strip(),
                })

    issues.sort(key=lambda i: i["index"])
    return {"issue_count": len(issues), "issues": issues}


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
        # 32d84131 — rebuild the FTS5 index from the current docx_paragraphs
        # content. This is a full content-sync (not trigger-driven) so the index
        # is always consistent after index_docx regardless of how the paragraph
        # table was populated. The 'rebuild' command re-reads the content table
        # and regenerates all FTS posting lists atomically.
        conn.execute("INSERT INTO docx_fts(docx_fts) VALUES ('rebuild')")
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
# 4a07e566 — structural element store: headings with section types + sectPr
# ---------------------------------------------------------------------------

_SEQ_FIGURE_RE = re.compile(r"\bSEQ\s+Figure\b", re.IGNORECASE)
_SEQ_TABLE_RE = re.compile(r"\bSEQ\s+Table\b", re.IGNORECASE)


def _is_figure_caption(block: dict[str, Any]) -> bool:
    for fld in block.get("fields", []):
        if fld.get("field_type") == "SEQ" and _SEQ_FIGURE_RE.search(
            fld.get("instruction", "")
        ):
            return True
    return False


def _is_table_caption(block: dict[str, Any]) -> bool:
    for fld in block.get("fields", []):
        if fld.get("field_type") == "SEQ" and _SEQ_TABLE_RE.search(
            fld.get("instruction", "")
        ):
            return True
    return False


def _seq_cached_number(block: dict[str, Any], seq_re: re.Pattern[str]) -> str | None:
    for fld in block.get("fields", []):
        if fld.get("field_type") == "SEQ" and seq_re.search(fld.get("instruction", "")):
            return fld.get("cached_result") or None
    return None


def _connect_structural(index_db_path: str) -> sqlite3.Connection:
    """Extend a sidecar SQLite DB with structural element tables."""
    conn = sqlite3.connect(index_db_path)
    # Ensure the base paragraph + meta tables exist.
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS docx_index_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
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
    # 4a07e566 — migrate existing DBs: add section_type column if absent.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(docx_headings)").fetchall()}
    if "section_type" not in cols:
        conn.execute("ALTER TABLE docx_headings ADD COLUMN section_type TEXT")
    return conn


def index_docx_structure(
    source: str | bytes | bytearray,
    index_db_path: str,
) -> dict[str, Any]:
    """4a07e566 — parse a .docx and store its structural elements locally.

    Populates ``docx_headings`` (with ``section_type``), ``docx_figures``, and
    ``docx_tables`` in the sidecar SQLite DB at ``index_db_path`` (created if
    absent). Uses :func:`document_content_tree` for the parse so headings and
    tables are extracted in true document order.

    Returns ``{index_db, heading_count, figure_count, table_count}``. Idempotent.
    """
    tree = document_content_tree(source)
    blocks: list[dict[str, Any]] = tree.get("blocks") or []

    headings_raw: list[dict[str, Any]] = []
    figures_out: list[tuple[int, str | None, str, str | None]] = []
    tables_out: list[tuple[int, int, int, str, str | None]] = []
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
                None,
            ))
            last_table_entry_index = len(tables_out) - 1
        elif kind == "paragraph":
            if _is_figure_caption(block):
                seq_num = _seq_cached_number(block, _SEQ_FIGURE_RE)
                figures_out.append((idx, block.get("para_id"), block.get("text", ""), seq_num))
            elif _is_table_caption(block) and last_table_entry_index is not None:
                t = tables_out[last_table_entry_index]
                tables_out[last_table_entry_index] = (t[0], t[1], t[2], t[3], block.get("text", ""))

    typed_headings = _assign_section_types([
        {"level": h["level"], "text": h["text"], "para_id": h["para_id"]}
        for h in headings_raw
    ])
    headings_out: list[tuple[str, int, int, str, str | None]] = [
        (typed["para_id"], headings_raw[i]["idx"], typed["level"],
         typed["text"], typed["section_type"])
        for i, typed in enumerate(typed_headings)
    ]

    conn = _connect_structural(index_db_path)
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
    """4a07e566 — retrieve locally-stored structural elements from the sidecar.

    Returns ``{headings, figures, tables}`` lists populated by
    :func:`index_docx_structure`. Each heading carries a ``section_type`` field.
    Returns empty lists for tables not yet populated.
    """
    conn = _connect_structural(index_db_path)
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
        {"id": r[0], "index": r[1], "para_id": r[2], "caption": r[3], "seq_number": r[4]}
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
    """4a07e566 — Full section-type map + sectPr page-numbering in one call.

    Combines :func:`document_outline` (section-typed heading outline) with
    :func:`parse_sectpr` (w:sectPr multi-section page-numbering).

    Returns::

        {paragraph_count, heading_count, headings (with section_type),
         section_regions, sectpr: {section_count, sections}}
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
    synth_map = _build_synth_id_map(body)
    # ``iter`` yields every <w:p> in document order, whether a direct body child
    # or nested inside a table cell — exactly the caption-search domain.
    for index, p in enumerate(body.iter(p_tag)):
        out.append(
            {
                "para_id": p.get(_q(_W14, "paraId")) or synth_map.get(id(p)) or f"p{index}",
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


# ===========================================================================
# 25743ec6 — Chunk-level, heading-aware weighted BM25 indexing
# ---------------------------------------------------------------------------
# A "chunk" is the atomic retrieval unit: one heading plus all consecutive
# body paragraphs that follow it until the next heading of equal or higher
# level (or end of document). Each chunk carries a ``heading_path`` — the
# ordered list of ancestor heading texts from document root to the chunk's
# own heading, e.g. ["Introduction", "Background"] for a nested H2 under
# an H1 — so a search hit can always report *which section it is in*.
#
# The new FTS5 virtual table ``docx_chunks_fts`` indexes two columns:
#   heading_text  — the chunk's own heading text (weighted 5.0)
#   body_text     — all body-paragraph text concatenated (weighted 1.0)
#
# Column weights rationale: a heading-text match should strongly outrank an
# equivalent term hit in prose.  5:1 is the conventional starting point for
# heading vs. body in BM25 multi-column schemes (mirrors Elasticsearch/Lucene
# defaults for title-vs-body field boosts in retrieval literature).  The
# constants are exposed at module level so tests can validate them and callers
# can override them via the search function's keyword arguments.
#
# The existing paragraph-level ``fts5_search_paragraphs`` / ``docx_fts``
# are completely untouched — this layer is purely additive.
# ===========================================================================

# BM25 column weights: heading_text weight, body_text weight.
# 5:1 ratio — a term in the heading is treated as 5x more relevant than the
# same term in body prose.
_CHUNK_WEIGHT_HEADING: float = 5.0
_CHUNK_WEIGHT_BODY: float = 1.0


def _build_chunks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group flat ``document_content_tree`` blocks into heading-anchored chunks.

    Algorithm:
    - Walk blocks in document order, maintaining a ``heading_stack`` of the
      ancestor headings seen so far (each entry is ``{level, text, para_id}``).
    - When a heading block is encountered:
        - Pop the stack until the stack is empty or the top has strictly lower
          level than the new heading (heading levels are numeric: 1=H1, 2=H2;
          "lower level" = "higher in the document hierarchy" = smaller integer;
          pop until ``top.level < new.level``).
        - Push the new heading onto the stack.
        - The current heading_path is the ordered text of every entry in the
          stack (root first, deepest last).
        - Flush any accumulating chunk and start a new one for this heading.
    - Non-heading body blocks (paragraph, table) append to the current chunk.
    - Blocks that precede the first heading are collected into a synthetic
      "preamble" chunk with ``heading_text=""`` and ``heading_path=[]``.

    Returns a list of chunk dicts::

        {
            "chunk_id": int,               # 0-based sequential index
            "heading_text": str,           # own heading text ("" for preamble)
            "heading_path": list[str],     # ancestor texts, root first
            "heading_para_id": str | None,
            "body_text": str,              # all body blocks joined
            "start_para_id": str | None,   # para_id of heading (or first body)
            "end_para_id": str | None,     # para_id of last body (or heading)
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

    for block in blocks:
        kind = block.get("kind")
        if kind == "heading":
            # Flush the chunk that was accumulating (skip empty preamble).
            if current_heading_text or current_body_parts:
                chunks.append(_flush(len(chunks)))

            lvl = block.get("level", 1)
            # Pop stack entries of equal or deeper level (>= lvl).
            while heading_stack and heading_stack[-1]["level"] >= lvl:
                heading_stack.pop()
            heading_stack.append({
                "level": lvl,
                "text": block.get("text", ""),
                "para_id": block.get("para_id"),
            })

            current_heading_text = block.get("text", "")
            current_heading_path = [h["text"] for h in heading_stack]
            current_heading_para_id = block.get("para_id")
            current_start_para_id = block.get("para_id")
            current_body_parts = []
            current_body_para_ids = []
        else:
            # Body block: collect text.
            if kind == "table":
                rows = block.get("rows") or []
                text = " ".join(cell for row in rows for cell in row if cell)
            else:
                text = block.get("text", "")
            if text:
                current_body_parts.append(text)
            pid = block.get("para_id")
            current_body_para_ids.append(pid)
            if current_start_para_id is None:
                current_start_para_id = pid

    # Flush the last chunk.
    if current_heading_text or current_body_parts:
        chunks.append(_flush(len(chunks)))

    return chunks


def _connect_chunks(index_db_path: str) -> sqlite3.Connection:
    """Open/create the sidecar SQLite DB with chunk tables and FTS5 virtual table."""
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
    """25743ec6 — build (or rebuild) the chunk-level heading-aware FTS5 index.

    Parses the .docx via :func:`document_content_tree`, groups blocks into
    chunks via :func:`_build_chunks`, stores them in ``docx_chunks``, and
    rebuilds ``docx_chunks_fts`` atomically.

    Returns ``{index_db, chunk_count}``. Idempotent: the chunk table is fully
    replaced each run so re-indexing an edited document stays consistent.
    """
    tree = document_content_tree(source)
    blocks = tree.get("blocks") or []
    chunks = _build_chunks(blocks)

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
    """25743ec6 — BM25 search over the chunk-level heading-aware FTS5 index.

    Searches ``docx_chunks_fts`` (populated by :func:`index_docx_chunks`)
    with column weights so a term hit in a section heading outranks the same
    term in body prose.  Results carry ``heading_path`` so every hit reports
    which section it came from.

    Default weights: heading_text=5.0, body_text=1.0 (5:1 ratio; see the
    25743ec6 block comment for the rationale).  The caller can override both
    via keyword arguments.

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
