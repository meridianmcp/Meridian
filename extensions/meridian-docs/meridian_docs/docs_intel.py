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

import base64
import copy
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

from . import render_gate

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

    Each record is ``{index, para_id, style, text}``. ``para_id`` resolves with
    the same three-tier scheme every mutation primitive in this module already
    uses (``_find_para_by_id``, ``_locate_section_bounds``,
    ``_vendored_content_tree._paragraph_node``): the native ``w14:paraId`` when
    Word wrote one (stable across edits), else the synthesized ``sp<hash>`` id
    from :func:`_vendored_content_tree._build_synth_id_map` (a content-derived
    id that is ALSO stable across edits -- unlike a raw position counter), else
    a positional ``p{index}`` fallback. Returns an empty list for a document
    with no body.

    71db285b -- previously this used a bare ``p{index}`` position counter,
    unaware of the synth_id scheme that ``move_section``/``copy_section``/
    ``_locate_section_bounds`` actually resolve against, so ``document_outline``
    handed back ids that those functions could not reliably locate on any real
    (non-synthetic) docx lacking ``w14:paraId`` on every paragraph.
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

    from ._vendored_content_tree import _build_synth_id_map  # noqa: PLC0415

    synth_map = _build_synth_id_map(body)
    for index, p in enumerate(body.findall(_q(_W, "p"))):
        para_id = p.get(_q(_W14, "paraId")) or synth_map.get(id(p)) or f"p{index}"
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


# ---------------------------------------------------------------------------
# 1dff1300 -- cursor-based pagination + section scoping shared by
# document_outline and read_document_snapshot, so neither can silently
# truncate or exceed a caller's token budget on a large document.
#
# The cursor is a small, opaque, self-contained (not server-side-stateful)
# token: base64(json({v, kind, fp, off, ps, sa})). It is NOT a security
# boundary (no signing/HMAC -- there is no secret key material anywhere in
# this stdlib-only, DB-free extension to sign with), only a structural/
# freshness guard: malformed input or a fingerprint mismatch (the document
# changed between page requests) is rejected with a clear, explicit reason
# rather than silently served against stale or fabricated data.
# ---------------------------------------------------------------------------

_PAGE_CURSOR_VERSION = 1


def _encode_page_cursor(
    *, kind: str, fingerprint: str, offset: int, page_size: int, section_anchor: str | None
) -> str:
    payload = {
        "v": _PAGE_CURSOR_VERSION,
        "kind": kind,
        "fp": fingerprint,
        "off": offset,
        "ps": page_size,
        "sa": section_anchor,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_page_cursor(cursor: str, *, kind: str) -> dict[str, Any] | None:
    """Decode and structurally validate a page cursor previously minted by
    :func:`_encode_page_cursor`. Returns ``None`` (never raises) for
    anything malformed, tampered with, or issued for a different ``kind``
    (an outline cursor can never be replayed against read_document_snapshot,
    or vice versa) -- callers turn a ``None`` into a clear, explicit
    ``"invalid_cursor"`` error rather than a stack trace."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("v") != _PAGE_CURSOR_VERSION or payload.get("kind") != kind:
        return None
    if not isinstance(payload.get("fp"), str) or not payload["fp"]:
        return None
    off = payload.get("off")
    if not isinstance(off, int) or isinstance(off, bool) or off < 0:
        return None
    ps = payload.get("ps")
    if not isinstance(ps, int) or isinstance(ps, bool) or ps <= 0:
        return None
    sa = payload.get("sa")
    if sa is not None and not isinstance(sa, str):
        return None
    return payload


def _resolve_pagination_window(
    *,
    kind: str,
    fingerprint: str,
    total_items: int,
    page_size: int | None,
    cursor: str | None,
    section_anchor: str | None,
) -> tuple[int, int, None] | tuple[None, None, dict[str, Any]]:
    """Resolve the ``(offset, page_size)`` window a pagination call should
    return, validating a caller-supplied ``cursor`` against the CURRENT
    ``fingerprint``/``section_anchor``. Only called once the caller has
    already established this IS a paginating call (``page_size is not None
    or cursor is not None``).

    Returns ``(offset, page_size, None)`` on success, or
    ``(None, None, error)`` with a clear, explicit ``error["reason"]`` (one
    of ``"invalid_cursor"``, ``"stale_cursor"``, ``"invalid_page_size"``) on
    any rejection -- never silently served against stale/mismatched data.
    """
    if cursor is not None:
        decoded = _decode_page_cursor(cursor, kind=kind)
        if decoded is None:
            return None, None, {
                "error": "cursor is malformed, tampered with, or was not issued by this function",
                "reason": "invalid_cursor",
            }
        if decoded["fp"] != fingerprint:
            return None, None, {
                "error": (
                    "cursor is stale: the document's content has changed "
                    "since this cursor was issued -- restart pagination "
                    "with page_size (no cursor) to get a fresh sequence"
                ),
                "reason": "stale_cursor",
            }
        if (decoded.get("sa") or None) != (section_anchor or None):
            return None, None, {
                "error": (
                    "cursor was issued for a different section_anchor than "
                    "the one passed to this call"
                ),
                "reason": "invalid_cursor",
            }
        offset = decoded["off"]
        resolved_page_size = page_size if page_size is not None else decoded["ps"]
        if not isinstance(resolved_page_size, int) or isinstance(resolved_page_size, bool) or resolved_page_size <= 0:
            return None, None, {
                "error": f"page_size must be a positive int, got {resolved_page_size!r}",
                "reason": "invalid_page_size",
            }
        if offset > total_items:
            return None, None, {
                "error": (
                    f"cursor offset {offset} is beyond the current item "
                    f"count ({total_items}) for this fingerprint -- the "
                    "cursor is stale"
                ),
                "reason": "stale_cursor",
            }
        return offset, resolved_page_size, None

    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0:
        return None, None, {
            "error": f"page_size must be a positive int, got {page_size!r}",
            "reason": "invalid_page_size",
        }
    return 0, page_size, None


def _paginated_page_result(
    offset: int,
    page_size: int,
    total_items: int,
    *,
    kind: str,
    fingerprint: str,
    section_anchor: str | None,
) -> tuple[int, int, str | None, bool]:
    """Compute ``(page_start, page_end, next_cursor, has_more)`` for an
    already-resolved ``(offset, page_size)`` pagination window."""
    page_end = min(offset + page_size, total_items)
    has_more = page_end < total_items
    next_cursor = (
        _encode_page_cursor(
            kind=kind, fingerprint=fingerprint, offset=page_end,
            page_size=page_size, section_anchor=section_anchor,
        )
        if has_more else None
    )
    return offset, page_end, next_cursor, has_more


def _resolve_section_anchor_bounds(
    paras: list[dict[str, Any]], anchor: str
) -> tuple[int, int] | None:
    """Resolve ``anchor`` (a heading's ``para_id``, or its exact heading
    text) against ``paras`` (:func:`parse_docx`'s flat paragraph list) to a
    ``[start, end)`` PARAGRAPH-INDEX range covering that heading's own
    paragraph plus its entire subsection (nested sub-headings and their
    body) -- the same "whole section, not just the heading line" semantics
    :func:`move_section` / :func:`copy_section` / :func:`relocate_table`
    already use via :func:`_locate_section_bounds`, resolved here against the
    flat paragraph list instead of the raw XML body (this module's other
    section-bounds helper needs a live ``ET.Element`` body; document_outline
    and read_document_snapshot only ever have :func:`parse_docx`'s output).

    Returns ``None`` when ``anchor`` does not resolve to any heading.
    """
    heading_positions = [
        (i, _heading_level(p.get("style")))
        for i, p in enumerate(paras)
        if _is_heading(p.get("style"))
    ]
    target_pos = next(
        (
            pos
            for pos, (idx, _level) in enumerate(heading_positions)
            if paras[idx].get("para_id") == anchor or paras[idx].get("text") == anchor
        ),
        None,
    )
    if target_pos is None:
        return None
    start_idx, target_level = heading_positions[target_pos]
    end_idx = len(paras)
    for idx, level in heading_positions[target_pos + 1 :]:
        if level <= target_level:
            end_idx = idx
            break
    return start_idx, end_idx


def _annotate_section_paths(paras: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a NEW list of paragraph dicts (shallow copies), each carrying
    a ``section_path`` (ancestor heading texts, root first -- ``[]`` for
    content before the first heading; a heading paragraph's own text is the
    LAST entry of its own ``section_path``) and ``heading_para_id`` (the
    innermost ancestor heading's ``para_id``, ``None`` before the first
    heading). Mirrors the same heading-stack walk :func:`_build_chunks_from_paras`
    uses for chunk boundaries, applied per-paragraph instead of per-chunk.
    """
    heading_stack: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    for p in paras:
        style = p.get("style")
        if _is_heading(style):
            lvl = _heading_level(style)
            while heading_stack and heading_stack[-1]["level"] >= lvl:
                heading_stack.pop()
            heading_stack.append(
                {"level": lvl, "text": p.get("text", ""), "para_id": p.get("para_id")}
            )
        section_path = [h["text"] for h in heading_stack]
        heading_para_id = heading_stack[-1]["para_id"] if heading_stack else None
        out.append({**p, "section_path": section_path, "heading_para_id": heading_para_id})
    return out


def document_outline(
    source: str | bytes | bytearray,
    *,
    page_size: int | None = None,
    cursor: str | None = None,
    section_anchor: str | None = None,
) -> dict[str, Any]:
    """13462df2 — stateless heading outline of a .docx (path or raw bytes). No
    sidecar index: a pure parse. Returns ``paragraph_count`` + ``heading_count``
    + an ordered ``headings`` list (level/text/para_id/section_type) — the
    queryable document structure docs_intel exposes without building a persistent
    index.

    4a07e566 — each heading now carries a ``section_type`` field
    (abstract/toc/lof/main/appendix) classifying the document region it belongs
    to. The ``section_regions`` key summarises the distinct regions in order.

    1dff1300 — cursor-based pagination + section scoping, so a large
    document's outline can never silently truncate or exceed a caller's
    token budget:

      - Every call (paginated or not) now includes ``document_fingerprint``
        (SHA-256 of the exact source bytes just parsed -- see
        :func:`_source_fingerprint`) — the identity a returned ``cursor`` is
        bound to. Each heading also now carries its ``index`` (position in
        :func:`parse_docx`'s paragraph list -- deterministic document
        order, stable across calls on unchanged content).
      - Pass ``page_size`` (no ``cursor``) for the FIRST page: at most
        ``page_size`` headings, plus ``cursor`` (an opaque token for the
        NEXT page, or ``None`` when this is the last page), ``has_more``,
        and ``total`` (the true heading count, after ``section_anchor``
        scoping if given).
      - Pass ``cursor`` (from a prior call) for the NEXT page. Its
        ``page_size``/``section_anchor`` are remembered from the call that
        minted it unless explicitly overridden.
      - ``section_anchor`` (a heading's ``para_id`` or exact heading text)
        scopes the outline to just that heading's own subsection (itself +
        nested sub-headings + their body) — the same "whole section" bounds
        :func:`move_section` / :func:`copy_section` use.
      - A cursor whose embedded fingerprint no longer matches the
        document's CURRENT content (it changed between page requests), or
        whose embedded ``section_anchor`` doesn't match this call's, or
        that is simply malformed, is rejected with a clear
        ``{"error": ..., "reason": "stale_cursor" | "invalid_cursor"}`` —
        never silently served against stale/mismatched data.
      - Omitting BOTH ``page_size`` and ``cursor`` (the default) returns
        the ENTIRE outline exactly as before this item — fully backward
        compatible; ``document_fingerprint`` (and each heading's ``index``)
        are the only new fields added to that response shape.

    Returns:
      Non-paginated (default): ``{paragraph_count, heading_count, headings,
      section_regions, document_fingerprint}``.
      Paginated: adds ``{cursor, has_more, total, section_anchor}`` —
      ``headings``/``paragraph_count``/``heading_count`` reflect just the
      current page / section scope, ``total`` is the true (post-scoping)
      heading count.
      ``{"error": ..., "reason": ...}`` on an invalid cursor/page_size
      (``reason`` one of ``"invalid_cursor"``, ``"stale_cursor"``,
      ``"invalid_page_size"``) or an unresolvable ``section_anchor``
      (``reason="section_not_found"``) — never a partial/misleading page.
    """
    paras = parse_docx(source)
    fingerprint = _source_fingerprint(source)

    scoped_paras = paras
    if section_anchor is not None:
        bounds = _resolve_section_anchor_bounds(paras, section_anchor)
        if bounds is None:
            return {
                "error": f"section_anchor {section_anchor!r} does not resolve to any heading",
                "reason": "section_not_found",
            }
        start_idx, end_idx = bounds
        scoped_paras = paras[start_idx:end_idx]

    raw_headings = [
        {
            "index": p.get("index"),
            "level": _heading_level(p.get("style")),
            "text": p.get("text", ""),
            "para_id": p.get("para_id"),
        }
        for p in scoped_paras
        if _is_heading(p.get("style"))
    ]
    headings = _assign_section_types(raw_headings)

    # Collect the ordered distinct regions (deduped, preserving first-seen order).
    seen_regions: list[str] = []
    for h in headings:
        r = h["section_type"]
        if not seen_regions or seen_regions[-1] != r:
            seen_regions.append(r)

    if page_size is None and cursor is None:
        return {
            "paragraph_count": len(scoped_paras),
            "heading_count": len(headings),
            "headings": headings,
            "section_regions": seen_regions,
            "document_fingerprint": fingerprint,
        }

    offset, resolved_page_size, error = _resolve_pagination_window(
        kind="outline",
        fingerprint=fingerprint,
        total_items=len(headings),
        page_size=page_size,
        cursor=cursor,
        section_anchor=section_anchor,
    )
    if error is not None:
        return error

    page_start, page_end, next_cursor, has_more = _paginated_page_result(
        offset, resolved_page_size, len(headings),
        kind="outline", fingerprint=fingerprint, section_anchor=section_anchor,
    )
    page_headings = headings[page_start:page_end]

    return {
        "paragraph_count": len(scoped_paras),
        "heading_count": len(page_headings),
        "headings": page_headings,
        "section_regions": seen_regions,
        "document_fingerprint": fingerprint,
        "cursor": next_cursor,
        "has_more": has_more,
        "total": len(headings),
        "section_anchor": section_anchor,
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


class StructureIndexNotTrustworthyError(Exception):
    """e9b2cd2b — the structural sidecar (docx_headings/docx_figures/docx_tables,
    populated by :func:`index_docx_structure`) is either STALE (the source
    .docx's content has changed since the last successful indexing run) or
    INCOMPLETE (that run never finished walking the document -- e.g. it
    crashed partway through, or is still in progress).

    Raised by :func:`get_local_structure_elements` (default behavior) instead
    of silently returning partial or outdated counts as if they were
    authoritative. Callers that explicitly want the best-effort data anyway
    (diagnostics, manual inspection) can pass ``allow_stale=True`` to get the
    data back with a ``"freshness"`` key describing why it wasn't trusted.
    """


def _source_fingerprint(source: str | bytes | bytearray) -> str:
    """SHA-256 hex digest of the exact source .docx bytes.

    e9b2cd2b — reads the FULL file into memory when ``source`` is a path, so
    the fingerprint reflects the exact bytes :func:`document_content_tree`
    is about to parse. This is a stronger freshness signal than the mtime
    tracking :func:`index_docx`/:func:`check_staleness` already do for the
    paragraph index: an mtime changes on a touch/copy/restore even when the
    content is byte-identical (false positive staleness) and can also stay
    put across a content-changing in-place edit on some filesystems/tools
    (false negative). A content hash has neither failure mode.
    """
    if isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
    else:
        with open(source, "rb") as fh:
            raw = fh.read()
    return hashlib.sha256(raw).hexdigest()


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

    Returns a summary ``{index_db, heading_count, figure_count, table_count,
    complete, source_sha256, duplicate_para_ids}``.  Idempotent: all three
    structural tables are fully replaced on each run.

    827b6bdc — ``duplicate_para_ids`` is document_content_tree's own
    native-``w14:paraId``-collision report, passed through unchanged (READ-ONLY:
    this indexer never renumbers/mutates the source to resolve a collision it
    finds — see ``meridian.doc_store.repair_duplicate_para_ids`` for the
    explicit, separately-invoked repair path). Empty list on the overwhelmingly
    common case of a document with no duplicated native ids.

    e9b2cd2b — freshness metadata: a SHA-256 fingerprint of the source .docx
    bytes and an explicit "complete boundary" marker are stamped into
    ``docx_index_meta`` (keys ``structure_source_sha256``,
    ``structure_source_path``, ``structure_complete``,
    ``structure_indexed_at``). The completeness marker is written as ``"0"``
    (and committed immediately, in its own small transaction) BEFORE the
    document walk starts, and only flipped to ``"1"`` -- atomically, in the
    SAME commit as the headings/figures/tables replacement -- once the walk
    and the write have both fully succeeded. If this function is interrupted
    anywhere in between (a malformed/truncated document, a crash mid-walk,
    etc.) the marker is left at ``"0"``, so a later read via
    :func:`get_structure_freshness` / :func:`get_local_structure_elements`
    correctly reports the index as incomplete rather than trusting whatever
    rows happen to be sitting in the tables. See also
    :func:`check_structure_staleness` for detecting a source that has
    changed content since the last COMPLETE run.
    """
    from ._vendored_content_tree import document_content_tree  # noqa: PLC0415

    source_path = source if isinstance(source, str) else None
    source_sha256 = _source_fingerprint(source)

    # Open the completeness boundary: commit "incomplete" up front so any
    # interruption during the walk/write below leaves this as the last
    # truthfully-committed state.
    _boundary_conn = _connect(index_db_path)
    try:
        _boundary_conn.execute(
            "INSERT OR REPLACE INTO docx_index_meta (key, value) VALUES (?, ?)",
            ("structure_complete", "0"),
        )
        _boundary_conn.commit()
    finally:
        _boundary_conn.close()

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
        # e9b2cd2b — close the completeness boundary opened above and stamp
        # the source fingerprint in the SAME commit as the table replacement,
        # so a reader never observes "complete=1" paired with rows that
        # haven't actually finished writing (or a fingerprint that doesn't
        # match what was just indexed).
        conn.execute(
            "INSERT OR REPLACE INTO docx_index_meta (key, value) VALUES (?, ?)",
            ("structure_source_sha256", source_sha256),
        )
        conn.execute(
            "INSERT OR REPLACE INTO docx_index_meta (key, value) VALUES (?, ?)",
            ("structure_source_path", source_path),
        )
        conn.execute(
            "INSERT OR REPLACE INTO docx_index_meta (key, value) VALUES (?, ?)",
            ("structure_indexed_at", datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO docx_index_meta (key, value) VALUES (?, ?)",
            ("structure_complete", "1"),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "index_db": index_db_path,
        "heading_count": len(headings_out),
        "figure_count": len(figures_out),
        "table_count": len(tables_out),
        "complete": True,
        "source_sha256": source_sha256,
        # 827b6bdc — surfaces document_content_tree's own duplicate native
        # w14:paraId report (see _vendored_content_tree.document_content_tree)
        # so a caller of this read-only indexer learns about an ambiguous
        # source document without indexing having silently "fixed" it.
        "duplicate_para_ids": tree.get("duplicate_para_ids", []),
    }


def check_structure_staleness(index_db_path: str) -> dict[str, Any]:
    """e9b2cd2b — compare the structural index's last-recorded SHA-256
    fingerprint against the source .docx's CURRENT content on disk.

    Mirrors :func:`check_staleness` (which tracks the paragraph index via
    mtime) but for the structural index populated by
    :func:`index_docx_structure`, and compares a content hash rather than an
    mtime -- see :func:`_source_fingerprint` for why that's a stronger
    signal.

    Returns ``{"stale": bool, "source_path": str|None, "reason": str}``.
    A structural index that was never built, or was built from raw bytes
    (no trackable path), always reports ``stale=False`` -- there is nothing
    to compare against, so silence rather than a false-positive staleness
    claim.
    """
    conn = _connect(index_db_path)
    try:
        rows = dict(
            conn.execute(
                "SELECT key, value FROM docx_index_meta "
                "WHERE key IN ('structure_source_path', 'structure_source_sha256')"
            ).fetchall()
        )
    finally:
        conn.close()
    source_path = rows.get("structure_source_path")
    if not source_path:
        return {"stale": False, "source_path": None, "reason": "no-source-tracked"}
    stored_sha256 = rows.get("structure_source_sha256")
    try:
        with open(source_path, "rb") as fh:
            current_sha256 = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return {"stale": False, "source_path": source_path, "reason": "source-unreadable"}
    if stored_sha256 is None or stored_sha256 != current_sha256:
        return {"stale": True, "source_path": source_path, "reason": "sha256-mismatch"}
    return {"stale": False, "source_path": source_path, "reason": "current"}


def get_structure_freshness(index_db_path: str) -> dict[str, Any]:
    """e9b2cd2b — combined completeness + staleness verdict for the
    structural index (:func:`index_docx_structure`'s ``docx_headings`` /
    ``docx_figures`` / ``docx_tables`` tables).

    Returns::

        {
          "indexed": bool,      # False if index_docx_structure was never run
          "complete": bool,     # False if the last run didn't finish (crashed
                                 # mid-walk, or is currently in progress)
          "stale": bool,        # True if the source .docx's content has
                                 # changed since the last COMPLETE run
          "trustworthy": bool,  # complete and not stale (or never indexed --
                                 # there's nothing to distrust yet)
          "source_path": str | None,
          "source_sha256": str | None,
          "reason": str,
        }

    ``trustworthy=False`` is the fail-closed signal :func:`get_local_structure_elements`
    checks by default.
    """
    conn = _connect(index_db_path)
    try:
        rows = dict(
            conn.execute(
                "SELECT key, value FROM docx_index_meta WHERE key IN ("
                "'structure_complete', 'structure_source_path', "
                "'structure_source_sha256')"
            ).fetchall()
        )
    finally:
        conn.close()

    if "structure_complete" not in rows:
        return {
            "indexed": False,
            "complete": True,
            "stale": False,
            "trustworthy": True,
            "source_path": None,
            "source_sha256": None,
            "reason": "never-indexed",
        }

    complete = rows.get("structure_complete") == "1"
    source_path = rows.get("structure_source_path")
    source_sha256 = rows.get("structure_source_sha256")

    if not complete:
        return {
            "indexed": True,
            "complete": False,
            "stale": False,
            "trustworthy": False,
            "source_path": source_path,
            "source_sha256": source_sha256,
            "reason": "incomplete-run",
        }

    staleness = check_structure_staleness(index_db_path)
    return {
        "indexed": True,
        "complete": True,
        "stale": staleness["stale"],
        "trustworthy": not staleness["stale"],
        "source_path": staleness["source_path"],
        "source_sha256": source_sha256,
        "reason": staleness["reason"],
    }


def get_local_structure_elements(
    index_db_path: str, *, allow_stale: bool = False
) -> dict[str, Any]:
    """c39ae092 — retrieve all locally-stored structural elements from the sidecar.

    Returns ``{headings, figures, tables}`` lists read from the
    ``docx_headings``, ``docx_figures``, ``docx_tables`` tables populated by
    :func:`index_docx_structure`.  Returns empty lists for any table that
    does not yet exist (i.e., :func:`index_docx_structure` was never called on
    this sidecar).

    e9b2cd2b — FAILS CLOSED by default: if the structural index is STALE (the
    source .docx's content changed since the last successful
    :func:`index_docx_structure` run) or INCOMPLETE (that run never
    finished), raises :class:`StructureIndexNotTrustworthyError` instead of
    returning partial/outdated counts as if they were authoritative. A
    sidecar that was never structurally indexed is NOT considered stale or
    incomplete (there's nothing to distrust yet) -- it just returns empty
    lists, same as before this change.

    Pass ``allow_stale=True`` to opt into reading the best-effort data
    anyway (e.g. for diagnostics); the returned dict then always carries a
    ``"freshness"`` key with the :func:`get_structure_freshness` verdict so
    the caller can see exactly why it wasn't trusted.
    """
    freshness = get_structure_freshness(index_db_path)
    if not freshness["trustworthy"] and not allow_stale:
        raise StructureIndexNotTrustworthyError(
            f"structural index at {index_db_path!r} is not trustworthy "
            f"(reason={freshness['reason']!r}); pass allow_stale=True to "
            "read the best-effort data anyway"
        )

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
    result = {
        "headings": headings,
        "figures": figures,
        "tables": tables,
        "heading_count": len(headings),
        "figure_count": len(figures),
        "table_count": len(tables),
    }
    if allow_stale:
        # e9b2cd2b — only attached when the caller explicitly opted into
        # reading possibly-untrustworthy data, so it's visible exactly when
        # it matters and callers that never hit the stale/incomplete path
        # see the same shape this function has always returned.
        result["freshness"] = freshness
    return result


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

    b17ef22b — contract with :func:`_save_docx_xml_stdlib`: callers must
    thread ``raw_bytes`` through to ``_save_docx_xml_stdlib`` UNMODIFIED
    (every existing call site already does — ``raw, root =
    _load_docx_xml_stdlib(path)`` followed later by
    ``_save_docx_xml_stdlib(raw, root, dest)``). ``_save_docx_xml_stdlib``
    re-reads the ORIGINAL ``word/document.xml`` straight out of ``raw`` to
    recover the namespace prefixes/declarations the source document actually
    used, so it can preserve them (and keep ``mc:Ignorable`` valid) on
    write-back rather than letting ``ET.tostring`` renumber or silently drop
    them. Passing a ``raw`` that doesn't match ``document_root``'s true
    origin defeats that preservation.
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


class DocxWriteVerificationError(OSError):
    """dccc2311 — fail-closed rejection of a staged DOCX write transaction.

    Raised by :func:`_atomic_write_docx_bytes` when the disposable staged
    artifact fails structural verification (see ``_docx_structural_manifest``)
    BEFORE it is ever promoted over the live file — ``dest`` is guaranteed
    byte-for-byte untouched whenever this is raised.

    Subclasses ``OSError`` (not a bare ``Exception``) so it is caught, without
    any call-site change, by every one of this module's ~25 existing
    ``except OSError as exc: return {"error": ...}`` guards around
    ``_save_docx_xml_stdlib`` / ``_save_docx_with_new_parts_stdlib`` — while
    still being distinguishable via ``isinstance()`` by tests/callers that
    want the specific fail-closed-verification failure mode rather than a
    generic disk/permission error.
    """

    def __init__(self, message: str, *, manifest: dict[str, Any] | None = None):
        super().__init__(message)
        self.manifest = manifest or {}


# dccc2311 — single serialized canonical-merge point per destination path.
# Every write that goes through _atomic_write_docx_bytes only ever mutates
# the live file inside this lock, keyed on the destination's normalized
# absolute path — two threads racing to promote a staged draft into the SAME
# .docx can never interleave their promotion (one full stage -> verify ->
# promote cycle always completes before the next begins for that path).
# Writers targeting DIFFERENT destinations never block each other, and the
# lock table only ever grows by distinct live destination paths (bounded by
# the number of documents actually being written to, not by request count).
#
# 5988a5bb — widened from ``threading.Lock`` to ``threading.RLock``. Callers
# with their own caller-specific post-write check (move_section /
# copy_section / relocate_figure / relocate_table / merge_draft_into_canonical)
# now hold this SAME lock across their entire stage+promote ->
# verify -> conditional-restore sequence, not just the promote step —
# ``_atomic_write_docx_bytes`` still acquires it internally for its own
# stage+promote step, so a caller holding it at the top needs reentrant
# acquisition from the SAME thread to avoid deadlocking itself. This closes
# the SAME-PROCESS race window between promotion and a subsequent
# verify/restore completely. It does NOT — and structurally cannot, since a
# lock (reentrant or not) is process-local — protect against a DIFFERENT
# process promoting to the same ``dest`` in that window; the cross-process
# case is instead covered by the compare-and-swap fingerprint check in
# ``_safe_restore_after_verification_failure`` (comparing ``dest``'s CURRENT
# on-disk bytes against what THIS writer itself promoted before deciding
# whether a verification-failure restore is safe).
_DOCX_PROMOTION_LOCKS: dict[str, threading.RLock] = {}
_DOCX_PROMOTION_LOCKS_GUARD = threading.Lock()


def _docx_promotion_lock(dest: str) -> threading.RLock:
    """Return the process-wide, reentrant promotion lock for ``dest``'s
    canonical path (5988a5bb — reentrant so a caller can hold it across a
    stage+promote -> verify -> conditional-restore sequence while
    ``_atomic_write_docx_bytes`` also reentrantly acquires it internally for
    its own stage+promote step; see the module-level comment above)."""
    key = os.path.normcase(os.path.abspath(dest))
    with _DOCX_PROMOTION_LOCKS_GUARD:
        lock = _DOCX_PROMOTION_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _DOCX_PROMOTION_LOCKS[key] = lock
        return lock


def _docx_style_count(raw: bytes) -> int:
    """Count ``<w:style>`` elements in ``word/styles.xml`` (0 when absent)."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        if "word/styles.xml" not in zf.namelist():
            return 0
        data = zf.read("word/styles.xml")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return 0
    return sum(1 for _ in root.iter(_q(_W, "style")))


_HEADER_FOOTER_PART_RE = re.compile(r"^word/(?:header|footer)\d+\.xml$")


def _docx_equation_count(raw: bytes) -> int:
    """Count ``<m:oMath>`` elements across ``word/document.xml`` plus any
    ``word/header<N>.xml`` / ``word/footer<N>.xml`` parts present."""
    total = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = [
            name for name in zf.namelist()
            if name == "word/document.xml" or _HEADER_FOOTER_PART_RE.match(name)
        ]
        for name in names:
            try:
                part_root = ET.fromstring(zf.read(name))
            except ET.ParseError:
                continue
            total += sum(1 for _ in part_root.iter(_q(_M, "oMath")))
    return total


def _docx_relationship_count(raw: bytes) -> int:
    """Count ``<Relationship>`` elements across every ``*.rels`` part."""
    total = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        for name in zf.namelist():
            if not name.endswith(".rels"):
                continue
            try:
                rels_root = ET.fromstring(zf.read(name))
            except ET.ParseError:
                continue
            total += sum(1 for _ in rels_root.iter(_q(_PKG_REL_NS, "Relationship")))
    return total


def _docx_structural_manifest(raw: bytes) -> dict[str, int]:
    """Structural fingerprint used to gate a write transaction (dccc2311).

    Counts the four structural families a DOCX write transaction must never
    silently lose: embedded media (images), paragraph styles, equations, and
    OOXML package relationships. Computed identically on the PRE-write bytes
    and the STAGED post-write bytes so a caller can compare before ever
    promoting a staged artifact into the live file.
    """
    return {
        "media_count": _docx_media_count(raw),
        "style_count": _docx_style_count(raw),
        "equation_count": _docx_equation_count(raw),
        "relationship_count": _docx_relationship_count(raw),
    }


def _docx_manifest_hash(changed_parts: dict[str, bytes]) -> str:
    """Deterministic SHA-256 over the parts a write transaction actually changed.

    Hashing only the CHANGED parts (never the whole repackaged archive) means
    the hash identifies the transaction's actual delta: two transactions that
    produce the exact same logical edit hash IDENTICALLY regardless of
    unrelated ZIP member ordering, and two transactions that touch different
    content hash differently even when everything else in the document is
    byte-identical. Part names are sorted first, so caller iteration order
    never affects the result — pure function of ``changed_parts``, so the
    same input always yields the same hash.
    """
    h = hashlib.sha256()
    for name in sorted(changed_parts):
        name_bytes = name.encode("utf-8")
        h.update(len(name_bytes).to_bytes(4, "big"))
        h.update(name_bytes)
        data = changed_parts[name]
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# b17ef22b — namespace-prefix-preserving, fail-closed word/document.xml
# write-back.
#
# THE DEFECT: ``ET.tostring()`` decides which prefix to print for a given
# namespace URI purely from the process-global ``register_namespace()``
# table (module load only pre-registers the common OOXML namespaces above)
# plus auto-generated ``ns0``/``ns1``/... fallbacks for anything else. Two
# distinct ways this silently mangles a round-tripped document that this
# module didn't intend to touch at all:
#
#   1. A namespace that IS referenced by some element/attribute tag in the
#      document, but uses a prefix this module never registered (a vendor
#      extension namespace, or simply a non-default prefix convention some
#      other tool wrote the file with) gets renumbered to ``ns0``/``ns1``/...
#      on write-back instead of keeping its original prefix.
#   2. A namespace declared on the root purely for markup-compatibility
#      (listed in ``mc:Ignorable``, or simply one of the several namespaces
#      real Word documents always declare whether or not THIS document
#      happens to use it) is invisible to ``ET.tostring()``'s "is this URI
#      referenced by some q-name in the tree" walk — xmlns declarations are
#      consumed into the parser's namespace context at parse time and are
#      never represented as element attributes, so an unreferenced-but-
#      declared namespace is dropped from the output ENTIRELY. This breaks
#      ``mc:Ignorable`` (per ECMA-376 Part 3, every prefix token in its
#      value must stay a currently-declared namespace prefix) and narrows
#      the package's advertised namespace support out from under it.
#
# THE FIX (scoped to _save_docx_xml_stdlib / _load_docx_xml_stdlib only —
# see AGENTS.md item b17ef22b; this module's other ET-based part writers,
# e.g. header/footer/comments/rels parts, are NOT touched here):
#
#   1. Before ``ET.tostring()``, temporarily register EVERY namespace prefix
#      the ORIGINAL word/document.xml actually declared (not just the fixed
#      module-load set) so referenced namespaces keep their real prefix
#      instead of getting renumbered. Reads ``raw`` (the untouched original
#      ZIP bytes every caller already threads through unchanged from
#      ``_load_docx_xml_stdlib`` to ``_save_docx_xml_stdlib``) rather than
#      requiring a signature change — ``_load_docx_xml_stdlib`` has ~50
#      call sites across this file owned by other in-flight work, so
#      widening its return tuple is out of scope here.
#   2. After serializing, re-splice back any root-level namespace
#      declaration that was present in the original but that ET's "only if
#      referenced" walk dropped (case 2 above) — preserving markup-
#      compatibility declarations even when nothing in the tree happens to
#      use them.
#   3. Fail closed, BEFORE ever building the replacement ZIP or touching
#      disk (raising :class:`DocxWriteVerificationError`, same fail-closed
#      contract as dccc2311's structural-manifest gate) if: the serialized
#      XML isn't well-formed; any original namespace URI ends up bound to a
#      DIFFERENT prefix in the output (defense-in-depth behind step 1 — the
#      one scenario step 1 alone cannot fully rule out is two distinct
#      original URIs sharing a prefix via a nested re-declaration, since
#      ``register_namespace`` is a single global URI -> prefix slot); or
#      ``mc:Ignorable`` ends up referencing an undeclared prefix.
#
# Known, deliberately out-of-scope limitations (documented per this item's
# own fallback clause rather than risking a rushed full rewrite):
#   - Only ROOT-level namespace declarations are tracked/preserved. A
#     namespace prefix re-declared on some DESCENDANT element (legal XML,
#     essentially never produced by Word) is not specially preserved.
#   - This does not convert the writer to a true "edit only the touched XML
#     parts" transactional model — it keeps the existing whole-document
#     ET parse/serialize architecture (structurally required by the ~50
#     other call sites in this file) and hardens IT to be namespace- and
#     mc:Ignorable-preserving plus fail-closed instead.
#   - The temporary registration window is guarded by a single process-wide
#     lock (``_NAMESPACE_REGISTRATION_LOCK``), not a per-document one, since
#     ``xml.etree.ElementTree``'s namespace table is itself process-global
#     state with no per-call scoping available in the stdlib. This fully
#     preserves correctness (no cross-document leakage is possible, since
#     the critical section is serialized end-to-end) at the cost of
#     serializing the in-memory ``ET.tostring()`` step (not any disk I/O)
#     across concurrent writes to *different* documents.
# ---------------------------------------------------------------------------

# Guards the temporary ET.register_namespace(...) / ET.tostring() / restore
# window in _save_docx_xml_stdlib -- see the module comment above for why a
# lock (rather than per-call scoping, which the stdlib namespace table does
# not support) is required for correctness under concurrent writers.
_NAMESPACE_REGISTRATION_LOCK = threading.Lock()

_MC_URI = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_MC_IGNORABLE_ATTR = _q(_MC_URI, "Ignorable")


def _root_namespace_declarations(document_xml_bytes: bytes) -> list[tuple[str, str]]:
    """Return the ``(prefix, uri)`` namespace declarations bound on the ROOT
    element of ``document_xml_bytes``, in document order (``prefix`` is
    ``""`` for an unprefixed/default declaration).

    Uses ``ET.iterparse(events=("start-ns", "start"))`` — the real XML
    parser's own namespace-scope tracking — rather than a regex over the raw
    start tag, so this can never disagree with what ``ET.fromstring`` itself
    considers a namespace binding (single- vs double-quoted attribute
    values, entity-escaped characters in a URI, whitespace variations, etc.
    all come for free). Collection stops at the first ``start`` event: every
    real .docx declares its namespaces once, on the ``<w:document>`` root,
    so that is also the first element this ever sees.
    """
    declarations: list[tuple[str, str]] = []
    for event, value in ET.iterparse(io.BytesIO(document_xml_bytes), events=("start-ns", "start")):
        if event == "start-ns":
            prefix, uri = value
            declarations.append((prefix or "", uri))
        else:
            break
    return declarations


def _extract_root_start_tag(xml_bytes: bytes) -> str:
    """Return the literal root-element start tag (e.g. the full
    ``<w:document xmlns:w="..." ...>`` or self-closing ``<w:document .../>``
    text, verbatim) from a serialized XML document, skipping past any
    leading XML declaration / comments / processing instructions.

    A plain ``str.index("<")`` is not enough because ``<?xml ...?>`` also
    starts with ``<``; this walks forward past any ``<?...?>``/``<!--...-->``
    prologue, then scans the root tag's own text honoring quoted attribute
    values so a literal ``>`` inside one (illegal in OOXML, but this stays
    defensive) cannot truncate the match early.
    """
    text = xml_bytes.decode("utf-8")
    pos = 0
    length = len(text)
    while True:
        start = text.index("<", pos)
        if text.startswith("<?", start):
            pos = text.index("?>", start) + 1
            continue
        if text.startswith("<!--", start):
            pos = text.index("-->", start) + 1
            continue
        i = start + 1
        quote: str | None = None
        while i < length:
            ch = text[i]
            if quote:
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
            elif ch == ">":
                return text[start : i + 1]
            i += 1
        raise ValueError("unterminated root element start tag")


def _xml_escape_attr_value(value: str) -> str:
    """Escape ``value`` for embedding as a double-quoted XML attribute value."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def _restore_dropped_namespace_declarations(
    original_document_xml: bytes, new_document_xml: bytes
) -> bytes:
    """Re-insert any root-level ``xmlns:*`` declaration present in the
    ORIGINAL ``word/document.xml`` that ``ET.tostring`` silently dropped
    from the newly-serialized output because nothing in the tree happens to
    reference that namespace URI (see the module comment above — this is
    the case ``ET.register_namespace`` cannot fix, since it only affects the
    prefix chosen for namespaces ET decides to emit at all).
    """
    original_decls = _root_namespace_declarations(original_document_xml)
    if not original_decls:
        return new_document_xml

    new_decls = _root_namespace_declarations(new_document_xml)
    new_uris = {uri for _prefix, uri in new_decls}
    missing = [(prefix, uri) for prefix, uri in original_decls if uri not in new_uris]
    if not missing:
        return new_document_xml

    insertion = "".join(
        f' xmlns:{prefix}="{_xml_escape_attr_value(uri)}"'
        if prefix
        else f' xmlns="{_xml_escape_attr_value(uri)}"'
        for prefix, uri in missing
    )

    text = new_document_xml.decode("utf-8")
    tag = _extract_root_start_tag(new_document_xml)
    tag_start = text.index(tag)
    if tag.endswith("/>"):
        repaired_tag = tag[:-2] + insertion + "/>"
    else:
        repaired_tag = tag[:-1] + insertion + ">"
    repaired_text = text[:tag_start] + repaired_tag + text[tag_start + len(tag) :]
    return repaired_text.encode("utf-8")


def _assert_namespace_prefixes_preserved(
    original_decls: list[tuple[str, str]],
    final_decls: list[tuple[str, str]],
    dest: str,
) -> None:
    """Fail closed if any namespace URI bound to a given prefix in the
    ORIGINAL document.xml ends up bound to a DIFFERENT prefix in the
    newly-serialized output.

    Defense-in-depth behind the register/restore step in
    :func:`_save_docx_xml_stdlib`: that step makes this impossible in the
    overwhelming common case, but ``ET.register_namespace`` is keyed on a
    single global URI -> prefix slot, so two distinct original namespace
    URIs that happen to share a prefix via a nested re-declaration (legal
    XML, essentially never produced by Word, and not itself tracked by
    :func:`_root_namespace_declarations`) could still yield an ambiguous or
    incorrect result this check exists to catch — rejecting the write
    rather than risking a silently corrupted package.
    """
    original_by_uri: dict[str, str] = {}
    for prefix, uri in original_decls:
        original_by_uri.setdefault(uri, prefix)

    final_by_uri: dict[str, str] = {}
    for prefix, uri in final_decls:
        final_by_uri.setdefault(uri, prefix)

    mismatches = {
        uri: {"original_prefix": orig_prefix, "new_prefix": final_by_uri[uri]}
        for uri, orig_prefix in original_by_uri.items()
        if uri in final_by_uri and final_by_uri[uri] != orig_prefix
    }
    if mismatches:
        raise DocxWriteVerificationError(
            f"post-write verification failed: {dest} would have one or more "
            "namespace prefixes renamed on write-back (an original "
            "xmlns declaration re-emitted under a different prefix) -- "
            f"discarding the staged write, {dest} is untouched",
            manifest={"prefix_mismatches": mismatches},
        )


def _assert_mc_ignorable_prefixes_declared(
    reparsed_root: ET.Element,
    final_decls: list[tuple[str, str]],
    dest: str,
) -> None:
    """Fail closed if ``mc:Ignorable`` on the write-back's root element lists
    a namespace prefix that is not (or no longer) declared.

    Per ECMA-376 Part 3 (Markup Compatibility and Extensibility), every
    prefix token in ``mc:Ignorable`` must be a currently in-scope namespace
    prefix -- an ``mc:Ignorable`` referencing an undeclared prefix is a
    document Word and other OOXML consumers can refuse to open cleanly or
    silently mishandle.
    """
    ignorable = reparsed_root.get(_MC_IGNORABLE_ATTR)
    if not ignorable:
        return
    declared_prefixes = {prefix for prefix, _uri in final_decls if prefix}
    missing = [token for token in ignorable.split() if token not in declared_prefixes]
    if missing:
        raise DocxWriteVerificationError(
            f"post-write verification failed: mc:Ignorable in {dest} "
            f"references undeclared namespace prefix(es) {missing!r} after "
            f"write-back -- discarding the staged write, {dest} is untouched",
            manifest={"ignorable": ignorable, "missing_prefixes": missing},
        )


def _save_docx_xml_stdlib(raw: bytes, root: ET.Element, dest: str) -> dict[str, Any]:
    """Write ``root`` back into ``dest`` as ``word/document.xml``.

    All other ZIP members from ``raw`` are preserved byte-for-byte.
    Writes to a BytesIO buffer first, then flushes to disk.

    Hardened (dccc2311) to route through :func:`_atomic_write_docx_bytes`'s
    stage -> verify -> promote transaction: media/style/relationship counts
    are gated to be UNCHANGED (this function only ever rewrites
    ``word/document.xml`` — every other part is copied through byte-for-byte,
    so those three families can never legitimately move here; a mismatch
    means the staged artifact is corrupt, not that an intentional edit
    happened). Equation count is intentionally NOT gated — editing
    ``word/document.xml`` is the entire point of most callers
    (insert_equation_local et al.) and is expected to change it.

    Backs up the existing file to ``dest + ".bak"`` when it already exists
    (best-effort, non-fatal on failure — same pattern as meridian/doc_store.py).

    Returns :func:`_atomic_write_docx_bytes`'s transaction dict, ``{
    "manifest_hash", "pre_counts", "post_counts", "promoted_sha256"}``
    (5988a5bb) — callers that don't need it (most existing call sites, which
    predate this change) simply ignore the return value.

    b17ef22b — namespace-prefix-preserving, fail-closed write-back (see the
    module comment above ``_NAMESPACE_REGISTRATION_LOCK`` for the full
    design). Before serializing, every namespace prefix ``raw``'s ORIGINAL
    ``word/document.xml`` actually declared is registered so referenced
    namespaces keep their real prefix instead of being renumbered; after
    serializing, any originally-declared-but-unreferenced namespace ET
    dropped is spliced back onto the root; and the result is rejected
    (``DocxWriteVerificationError``, before any bytes are staged to disk) if
    it isn't well-formed XML, if any original namespace prefix was renamed,
    or if ``mc:Ignorable`` ends up referencing an undeclared prefix.
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        original_document_xml = zf.read("word/document.xml")
    original_ns_decls = _root_namespace_declarations(original_document_xml)

    with _NAMESPACE_REGISTRATION_LOCK:
        snapshot = dict(ET._namespace_map)  # type: ignore[attr-defined]
        try:
            for prefix, uri in original_ns_decls:
                ET.register_namespace(prefix, uri)
            new_xml = ET.tostring(root, encoding="unicode")
        finally:
            ET._namespace_map.clear()  # type: ignore[attr-defined]
            ET._namespace_map.update(snapshot)  # type: ignore[attr-defined]

    new_document_bytes = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + new_xml
    ).encode("utf-8")

    # Validate well-formedness BEFORE the namespace-restore splice touches
    # the string: _restore_dropped_namespace_declarations itself parses
    # (via _root_namespace_declarations) to discover what ET emitted, and a
    # malformed ET.tostring() result would otherwise surface as an
    # unhandled xml.etree.ElementTree.ParseError there instead of the
    # intended fail-closed DocxWriteVerificationError.
    try:
        ET.fromstring(new_document_bytes)
    except ET.ParseError as exc:
        raise DocxWriteVerificationError(
            "post-write verification failed: the serialized "
            f"word/document.xml for {dest} is not well-formed XML: {exc} -- "
            f"discarding the staged write, {dest} is untouched",
            manifest={"parse_error": str(exc)},
        ) from exc

    new_document_bytes = _restore_dropped_namespace_declarations(
        original_document_xml, new_document_bytes
    )

    try:
        reparsed_root = ET.fromstring(new_document_bytes)
    except ET.ParseError as exc:
        raise DocxWriteVerificationError(
            "post-write verification failed: the namespace-repaired "
            f"word/document.xml for {dest} is not well-formed XML: {exc} -- "
            f"discarding the staged write, {dest} is untouched",
            manifest={"parse_error": str(exc)},
        ) from exc

    final_ns_decls = _root_namespace_declarations(new_document_bytes)
    _assert_namespace_prefixes_preserved(original_ns_decls, final_ns_decls, dest)
    _assert_mc_ignorable_prefixes_declared(reparsed_root, final_ns_decls, dest)

    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src:
        infos = src.infolist()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in infos:
                data = src.read(info.filename)
                if info.filename == "word/document.xml":
                    data = new_document_bytes
                dst.writestr(info, data)

    return _atomic_write_docx_bytes(
        out.getvalue(),
        dest,
        pre_manifest=_docx_structural_manifest(raw),
        protected_keys=("media_count", "style_count", "relationship_count"),
        changed_parts={"word/document.xml": new_document_bytes},
    )


def _atomic_write_docx_bytes(
    payload: bytes,
    dest: str,
    *,
    pre_manifest: dict[str, int] | None = None,
    protected_keys: tuple[str, ...] = (),
    changed_parts: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    """Persist a DOCX as a disposable-worker-artifact transaction (dccc2311).

    1. STAGE — ``payload`` (the complete, already-repackaged ZIP) is flushed
       to a disposable temp file in ``dest``'s own directory. It is NEVER
       written to ``dest`` directly — ``dest`` is not touched at all unless
       and until verification (step 2) passes.
    2. VERIFY — when ``pre_manifest`` is supplied, the staged file is
       re-opened FRESH FROM DISK (never the in-memory ``payload`` object,
       which would just re-validate the build step's own intent) and its
       structural manifest (:func:`_docx_structural_manifest`) is compared
       against ``pre_manifest`` for every key in ``protected_keys``. Any
       mismatch — or a staged artifact that isn't even a valid .docx — is a
       fail-closed verification failure: :class:`DocxWriteVerificationError`
       is raised, the staged file is discarded, and ``dest`` is left
       byte-for-byte untouched (never a partially-written or corrupted file).
       Independently of ``pre_manifest``, when ``changed_parts`` is given,
       every changed ``.xml``/``.rels`` member is also re-read from that SAME
       staged-and-flushed file and checked for XML well-formedness — a
       malformed member is a ZIP-valid but corrupt .docx that the structural
       manifest counts alone would not necessarily catch (see the inline
       comment above the check), and is rejected the same fail-closed way.
    3. PROMOTE — the ONLY point at which the live file changes is inside
       :func:`_docx_promotion_lock`'s single serialized canonical-merge
       point for this destination — an ``os.replace`` (atomic on the same
       filesystem) swaps the verified staged artifact over ``dest``. Two
       concurrent writers targeting the same ``dest`` can never interleave
       their promotions.

    A pre-existing ``dest`` is backed up to ``dest + ".bak"`` immediately
    before promotion (best-effort, non-fatal on failure).

    Returns ``{"manifest_hash", "pre_counts", "post_counts", "promoted_sha256"}``
    — the manifest hash is always computed (from ``changed_parts`` when
    given, else ``None``); ``pre_counts``/``post_counts`` are ``None`` when
    ``pre_manifest`` was not supplied (legacy callers that run their OWN
    separate range-hash verification, like move_section/copy_section/
    relocate_table via :func:`_verify_docx_write`, keep working exactly as
    before — this is purely additive). ``promoted_sha256`` (5988a5bb) is a
    full-body SHA-256 over the EXACT bytes this call promoted (the staged
    artifact, re-read fresh from disk after flush — never the in-memory
    ``payload`` object), always computed regardless of ``pre_manifest``. It
    is the "what did THIS writer actually put on disk" fingerprint a
    caller-specific post-write check uses to tell apart "verification
    failed but nobody has touched dest since I promoted" (safe to restore my
    own pre-image) from "a different writer's promotion has already landed
    since mine" (restoring would destroy that writer's completed work — see
    :func:`_safe_restore_after_verification_failure`).
    """
    parent = os.path.dirname(os.path.abspath(dest)) or "."
    os.makedirs(parent, exist_ok=True)
    manifest_hash = _docx_manifest_hash(changed_parts) if changed_parts else None
    post_counts: dict[str, int] | None = None
    promoted_sha256: str | None = None

    staged_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".meridian-docx-stage-", suffix=".tmp", dir=parent, delete=False
        ) as fh:
            staged_path = fh.name
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

        with open(staged_path, "rb") as fh:
            staged_bytes = fh.read()
        promoted_sha256 = hashlib.sha256(staged_bytes).hexdigest()

        if pre_manifest is not None:
            try:
                post_counts = _docx_structural_manifest(staged_bytes)
            except (zipfile.BadZipFile, KeyError) as exc:
                raise DocxWriteVerificationError(
                    "post-write verification failed: the staged artifact for "
                    f"{dest} is not a valid .docx after being flushed to disk: "
                    f"{exc} — discarding it, {dest} is untouched",
                    manifest={"pre_counts": pre_manifest, "post_counts": None},
                ) from exc

            mismatches = {
                key: {"expected": pre_manifest.get(key), "actual": post_counts.get(key)}
                for key in protected_keys
                if post_counts.get(key) != pre_manifest.get(key)
            }
            if mismatches:
                raise DocxWriteVerificationError(
                    "post-write verification failed: the staged .docx does "
                    "not preserve structural elements this write must never "
                    f"lose ({dest}) — discarding the staged artifact instead "
                    f"of promoting a corrupted write; {dest} is untouched",
                    manifest={
                        "pre_counts": pre_manifest,
                        "post_counts": post_counts,
                        "count_mismatches": mismatches,
                    },
                )

        # dccc2311 follow-up -- verify every CHANGED part that claims to be
        # XML (``.xml``/``.rels``, which also covers the un-suffixed-but-XML
        # ``[Content_Types].xml``) is actually well-formed, re-read from the
        # STAGED file fresh off disk (never the in-memory ``changed_parts``
        # values, which would only re-validate the build step's own intent --
        # same "verify from disk" discipline as the structural-manifest check
        # above). Multi-part writers like
        # :func:`_save_docx_with_new_parts_stdlib` build some parts via raw
        # text splicing (:func:`_insert_before_closing_tag`) rather than an
        # ElementTree round-trip, so -- unlike a ``word/document.xml``
        # rewrite that always emits valid XML straight out of
        # ``ET.tostring`` -- a splice bug CAN produce a byte sequence that is
        # not well-formed XML. Nothing upstream of this point catches that: a
        # malformed member does not fail the ZIP-container check the
        # ``BadZipFile`` branch above performs, and the structural-manifest
        # counts silently treat an unparsable part as "0 elements" (see
        # ``_docx_style_count`` / ``_docx_equation_count`` /
        # ``_docx_relationship_count``) rather than raising -- so without
        # this check a corrupted-but-still-a-valid-ZIP write would be
        # promoted and reported as success.
        if changed_parts:
            xml_changed_names = [
                name for name in changed_parts
                if name.endswith(".xml") or name.endswith(".rels")
            ]
            if xml_changed_names:
                try:
                    with zipfile.ZipFile(io.BytesIO(staged_bytes)) as staged_zip:
                        staged_names = set(staged_zip.namelist())
                        xml_parse_errors: dict[str, str] = {}
                        for part_name in xml_changed_names:
                            if part_name not in staged_names:
                                xml_parse_errors[part_name] = (
                                    "part missing from staged archive"
                                )
                                continue
                            try:
                                ET.fromstring(staged_zip.read(part_name))
                            except ET.ParseError as exc:
                                xml_parse_errors[part_name] = str(exc)
                except zipfile.BadZipFile as exc:
                    # Already reported above when pre_manifest is supplied;
                    # when it is not, this is the first (and only) chance to
                    # catch it.
                    raise DocxWriteVerificationError(
                        "post-write verification failed: the staged artifact "
                        f"for {dest} is not a valid .docx after being "
                        f"flushed to disk: {exc} — discarding it, {dest} is "
                        "untouched",
                        manifest={"pre_counts": pre_manifest, "post_counts": post_counts},
                    ) from exc
                if xml_parse_errors:
                    raise DocxWriteVerificationError(
                        "post-write verification failed: one or more parts "
                        f"this write changed in {dest} are not well-formed "
                        "XML after being flushed to disk — discarding the "
                        "staged artifact instead of promoting a corrupted "
                        f"write; {dest} is untouched",
                        manifest={
                            "pre_counts": pre_manifest,
                            "post_counts": post_counts,
                            "xml_parse_errors": xml_parse_errors,
                        },
                    )

        # --- PROMOTE: the single serialized canonical-merge point ----------
        with _docx_promotion_lock(dest):
            if os.path.exists(dest):
                try:
                    shutil.copy2(dest, dest + ".bak")
                except OSError:
                    pass
            os.replace(staged_path, dest)
            staged_path = None
    finally:
        if staged_path:
            try:
                os.unlink(staged_path)
            except OSError:
                pass

    return {
        "manifest_hash": manifest_hash,
        "pre_counts": pre_manifest,
        "post_counts": post_counts,
        "promoted_sha256": promoted_sha256,
    }


# ---------------------------------------------------------------------------
# 9907df44 — mandatory post-write verification for move_section /
# copy_section / relocate_table.
#
# All three primitives above cut/copy live elements, splice them into a new
# body position, then trust their OWN in-memory bookkeeping (moved_block_count
# / copied_block_count / row_count / col_count) to build the success payload
# -- nothing re-reads the file that was just written to confirm the mutation
# actually landed. A real incident: two consecutive live calls both reported
# `status="moved"` with a `moved_block_count` that didn't even match the
# expected block count, while the on-disk document was byte-identical before
# and after (paragraph/heading counts unchanged) -- a stale/buggy write path
# silently no-op'd and the tool reported false success anyway.
#
# The fix mirrors the discipline the existing PRE-write safety checks already
# use (e87b8338: abort cleanly, file untouched, real error) but for the
# POST-write side: after `_save_docx_xml_stdlib` returns, re-read the file
# FRESH FROM DISK (never reuse the in-memory root that was just serialized --
# that would just re-validate our own intent, not the actual write) and
# compare structural counts (paragraph/heading/table/image) plus a content
# hash of the affected range against what the operation should have produced.
# A mismatch returns a real error instead of the success payload, and
# best-effort restores the pre-write backup `_save_docx_xml_stdlib` already
# wrote to `<dest>.bak` so a failed verification doesn't leave the file in an
# inconsistent reported state.
# ---------------------------------------------------------------------------

def _structural_counts(elements: list[ET.Element]) -> dict[str, int]:
    """Paragraph / heading / table counts found within ``elements``.

    Each element is walked with :meth:`ET.Element.iter`, so nested
    paragraphs/tables (e.g. inside a table cell, or the document body itself)
    are counted too. Called both with ``[body]`` for a whole-document
    baseline and with a specific moved/copied element list for the delta a
    copy should have produced.
    """
    w_p = _q(_W, "p")
    w_tbl = _q(_W, "tbl")
    w_pPr = _q(_W, "pPr")
    w_pStyle = _q(_W, "pStyle")
    w_val = _q(_W, "val")

    paragraph_count = 0
    heading_count = 0
    table_count = 0
    for el in elements:
        for p in el.iter(w_p):
            paragraph_count += 1
            ppr = p.find(w_pPr)
            style = None
            if ppr is not None:
                ps = ppr.find(w_pStyle)
                style = ps.get(w_val) if ps is not None else None
            if _is_heading(style):
                heading_count += 1
        table_count += sum(1 for _ in el.iter(w_tbl))
    return {
        "paragraph_count": paragraph_count,
        "heading_count": heading_count,
        "table_count": table_count,
    }


def _hash_elements(elements: list[ET.Element]) -> str:
    """SHA-256 hex digest of ``elements`` serialized in order.

    Used to fingerprint the exact byte content of a moved/copied range so a
    post-write re-read can confirm those SAME bytes actually landed at the
    expected destination -- catching a false-success write that left the
    document's structural counts unchanged (a plain no-op write is invisible
    to count-based checks alone).
    """
    h = hashlib.sha256()
    for el in elements:
        h.update(ET.tostring(el, encoding="unicode").encode("utf-8"))
    return h.hexdigest()


def _docx_media_count(raw: bytes) -> int:
    """Count ``word/media/*`` parts in the (pre- or post-write) ZIP bytes.

    Stdlib stand-in for "image count via package.image_parts" (this module
    has no python-docx dependency) -- none of move_section/copy_section/
    relocate_table add or remove media parts (copy_section reuses shared
    image relationships rather than duplicating the media part), so this is
    expected to be invariant across all three operations.
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        return sum(1 for name in zf.namelist() if name.startswith("word/media/"))


def _restore_docx_backup(dest: str) -> bool:
    """Best-effort restore of ``dest`` from the ``.bak`` copy left by
    :func:`_save_docx_xml_stdlib`, when post-write verification fails.

    Mirrors the "file untouched" guarantee the pre-write safety checks give
    on abort -- a failed post-write verification should not leave a
    partially-trusted mutation on disk. Returns whether the restore
    succeeded; a missing/failed backup is reported (not raised) so the
    caller can surface it in the error payload rather than mask the
    original verification failure.

    5988a5bb (finding 2) -- routed through the SAME stage-to-temp-in-
    ``dest``'s-own-directory + fsync + ``os.replace`` pattern every other
    write in this module uses, instead of writing straight into ``dest`` via
    ``shutil.copy2``. An interrupted restore (disk full, AV lock, permission
    revoked mid-copy) now leaves ``dest`` and the disposable temp file
    untouched -- ``os.replace`` is atomic on the same filesystem, so ``dest``
    is either the OLD content (interrupted before replace) or the FULLY
    restored backup content (replace completed); it can never be left
    truncated or partially overwritten as a direct in-place copy risked.
    """
    backup = dest + ".bak"
    if not os.path.exists(backup):
        return False
    parent = os.path.dirname(os.path.abspath(dest)) or "."
    staged_path: str | None = None
    try:
        fd, staged_path = tempfile.mkstemp(
            prefix=".meridian-docx-restore-", suffix=".tmp", dir=parent
        )
        with open(backup, "rb") as src, os.fdopen(fd, "wb") as fh:
            shutil.copyfileobj(src, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(staged_path, dest)
        staged_path = None
        return True
    except OSError:
        return False
    finally:
        if staged_path:
            try:
                os.unlink(staged_path)
            except OSError:
                pass


def _docx_file_sha256(path: str) -> str | None:
    """SHA-256 over ``path``'s raw bytes, read fresh from disk.

    5988a5bb -- the compare-and-swap fingerprint used to tell "dest still
    holds exactly what THIS writer promoted" apart from "a different writer
    has already promoted something newer" (see
    :func:`_safe_restore_after_verification_failure`). Returns ``None``
    (rather than raising) when ``path`` cannot be read -- a missing/
    unreadable file is itself informative to the caller (it can never match
    a real ``promoted_sha256``), not a reason to blow up a post-write
    verification-failure handler that is already in an error path.
    """
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _safe_restore_after_verification_failure(
    write_dest: str,
    promoted_sha256: str | None,
) -> tuple[bool, bool, bool]:
    """Compare-and-swap-safe gate in front of :func:`_restore_docx_backup` (5988a5bb).

    Only called once a caller-specific post-write check (e.g.
    :func:`_verify_docx_write`) has already found a mismatch on
    ``write_dest``. A bare, unconditional restore is unsafe: if a DIFFERENT
    writer (a different process, or a different concurrent claim on a
    non-overlapping region of the SAME file -- see
    :func:`claim_docx_region`'s module docs) promoted its own write to
    ``write_dest`` in the window between THIS writer's own promotion and
    THIS writer's verify, the mismatch this writer is reacting to is a false
    positive caused by that other writer's legitimate, already-promoted
    work -- blindly restoring from THIS writer's own ``.bak`` would silently
    destroy it.

    Re-reads ``write_dest``'s CURRENT on-disk bytes (fresh from disk) and
    compares their SHA-256 against ``promoted_sha256`` --
    :func:`_atomic_write_docx_bytes`'s own fingerprint of exactly what THIS
    writer promoted (returned as ``transaction["promoted_sha256"]``).
    ``promoted_sha256`` can itself be ``None`` -- not only from a genuinely
    concurrent writer, but also when the caller's own write helper didn't
    return transaction info at all (e.g. a stubbed/broken lower-level write
    path in a test, or a future caller that hasn't been updated) -- that
    case is reported distinctly below rather than misdiagnosed as a
    confirmed concurrent write.

    Returns ``(safe_to_restore, restored, concurrent_write_detected)``:

    * ``(True, restored, False)`` -- ``write_dest`` still held exactly what
      this writer promoted (nobody has touched it since); a restore was
      attempted and ``restored`` reports whether it succeeded.
    * ``(False, False, True)`` -- POSITIVE evidence of a different writer:
      both fingerprints were available and did not match. Restore was
      deliberately NOT attempted, and ``write_dest`` is left exactly as
      that other writer left it.
    * ``(False, False, False)`` -- restore eligibility could not be
      determined at all (``promoted_sha256`` or ``write_dest`` itself was
      unavailable) -- NOT positive evidence of a concurrent write, just
      insufficient information to safely restore. Restore is still NOT
      attempted (the same fail-closed default), but callers should not
      report this as a confirmed cross-writer clobber.
    """
    current_sha256 = _docx_file_sha256(write_dest) if os.path.isfile(write_dest) else None
    safe_to_restore = (
        promoted_sha256 is not None
        and current_sha256 is not None
        and current_sha256 == promoted_sha256
    )
    if not safe_to_restore:
        concurrent_write_detected = promoted_sha256 is not None and current_sha256 is not None
        return False, False, concurrent_write_detected
    return True, _restore_docx_backup(write_dest), False


# ---------------------------------------------------------------------------
# Render-capability gate (ddd79188) -- shared by insert_figure_block and
# merge_draft_into_canonical. Closes the gap between STRUCTURAL verification
# (this module's own re-parse-and-check, above) and REAL Word/COM (or
# LibreOffice) render verification: a document can pass every structural
# check here and still fail to open in Word. Must only be called AFTER
# structural verification has already succeeded, and while the caller still
# holds ``write_dest``'s :func:`_docx_promotion_lock` -- this function may
# itself restore ``write_dest`` from the SAME pre-write ``.bak`` a structural
# verification failure would restore from, so it needs the identical
# compare-and-swap safety :func:`_safe_restore_after_verification_failure`
# gives that path (never blindly clobber a different, already-promoted
# concurrent writer's work).
# ---------------------------------------------------------------------------

def _enforce_render_verification(
    write_dest: str,
    *,
    promoted_sha256: str | None,
    allow_degraded_render: bool,
    degraded_render_reason: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Invoke :func:`render_gate.check_render_capability` on ``write_dest``
    and enforce its three-state contract (93cd9798: ``rendered`` /
    ``unavailable-with-reason`` / ``failed``) as a write-time gate.

    Returns ``(error, render_info)`` -- exactly one is non-``None``:

    * ``(None, render_info)`` -- the write may stand. ``render_info`` is a
      dict of render-evidence fields (``render_status``, ``render_verified``,
      plus backend/reason detail) to merge into the caller's success payload.
    * ``(error, None)`` -- the write must be rejected. ``error`` already
      carries ``"error"``, ``"file_restored"``, and
      ``"concurrent_write_detected"`` -- the same shape the caller's own
      structural-verification-failure branch returns -- so the caller can
      layer its own identifying fields (``docx_path`` / ``canonical_path`` /
      ``draft_path`` / ``merged``) on top and return it verbatim.

    Three-state handling:

      - ``"rendered"`` -- a real render backend actually produced visual
        output for this document. The ONLY status that means "verified" --
        ``render_verified`` is ``True`` only here.
      - ``"failed"`` -- a render backend WAS available but the render attempt
        for THIS document errored. This is a real, confirmed problem: the
        write is restored from backup (never left half-verified) and an
        error is returned -- never reported as ``"rendered"``.
      - ``"unavailable-with-reason"`` -- no render backend exists in this
        environment. This says nothing about the document itself, but it
        also means visual-render verification genuinely did not happen --
        so this FAILS CLOSED (restore + error) for canonical/production
        promotion by default, exactly like ``"failed"`` does. The only way
        to proceed anyway is an EXPLICIT, audited opt-in:
        ``allow_degraded_render=True`` paired with a non-empty
        ``degraded_render_reason`` -- the write is then kept, but
        ``render_verified`` is still ``False`` and ``render_degraded`` /
        ``degraded_render_reason`` are stamped onto the payload so no
        caller can mistake a degraded acceptance for a real verification.
    """
    try:
        render_result = render_gate.check_render_capability(write_dest)
    except Exception as exc:  # noqa: BLE001 -- a broken checker must fail closed, never crash the write or masquerade as verified
        render_result = {
            "status": render_gate.FAILED,
            "reason": f"render capability check raised {type(exc).__name__}: {exc}",
        }
    if (
        not isinstance(render_result, dict)
        or render_result.get("status") not in render_gate.RENDER_STATUSES
    ):
        render_result = {
            "status": render_gate.FAILED,
            "reason": f"render capability check returned an invalid result: {render_result!r}",
        }

    status = render_result.get("status")
    reason = render_result.get("reason")
    backend = render_result.get("backend")
    detail = render_result.get("detail")

    if status == render_gate.RENDERED:
        return None, {
            "render_status": status,
            "render_verified": True,
            "render_backend": backend,
            "render_detail": detail,
        }

    degraded_accepted = (
        status == render_gate.UNAVAILABLE_WITH_REASON
        and allow_degraded_render
        and bool(degraded_render_reason and str(degraded_render_reason).strip())
    )
    if degraded_accepted:
        return None, {
            "render_status": status,
            "render_verified": False,
            "render_reason": reason,
            "render_degraded": True,
            "degraded_render_reason": str(degraded_render_reason).strip(),
        }

    # Fail closed: restore-then-error -- identical CAS discipline to a
    # structural verification failure, since a real (concurrent) writer may
    # have already promoted something newer to write_dest since our own
    # promotion.
    safe_to_restore, restored, concurrent_write_detected = (
        _safe_restore_after_verification_failure(write_dest, promoted_sha256)
    )
    if status == render_gate.FAILED:
        headline = f"render verification failed: {reason or 'unknown render error'}"
    else:
        headline = (
            "render verification unavailable in this environment "
            f"({reason or 'no render backend available'}) -- failing closed for "
            "canonical/production promotion. Pass allow_degraded_render=True "
            "with a non-empty degraded_render_reason to explicitly accept this "
            "write without real visual-render verification (audited degrade, "
            "never a silent one)."
        )
    error: dict[str, Any] = {
        "error": headline,
        "render_status": status,
        "render_reason": reason,
        "file_restored": restored,
        "concurrent_write_detected": concurrent_write_detected,
    }
    if not safe_to_restore:
        if concurrent_write_detected:
            error["error"] = (
                error["error"]
                + " -- AND a different writer's promotion has landed on this "
                "file since ours, so this could not be safely auto-corrected: "
                "restoring from our own backup would destroy that writer's "
                f"already-promoted work. {write_dest} was left untouched, "
                "exactly as that other writer left it -- investigate manually."
            )
        else:
            error["error"] = (
                error["error"]
                + " -- this write's own promotion fingerprint is unavailable, "
                "so it could not be safely confirmed that restoring from "
                "backup would not destroy a different writer's work; "
                f"{write_dest} was left untouched rather than risk it -- "
                "investigate manually."
            )
    return error, None


def _verify_docx_write(
    docx_path: str,
    *,
    expected_counts: dict[str, int],
    expected_hash: str | None = None,
    expected_range: tuple[int, int] | None = None,
    locate_by_paraid: str | None = None,
    expected_len: int = 1,
) -> dict[str, Any] | None:
    """Mandatory post-write verification (9907df44). Returns ``None`` when the
    on-disk document matches expectations, or an error dict when it doesn't.

    Re-reads ``docx_path`` FRESH FROM DISK -- a stale/buggy build that
    fabricates a success payload without actually mutating the file, or a
    write that silently no-ops, is caught here instead of trusted.

    The destination range to hash-check is located either by fixed body
    index (``expected_range`` -- used by move_section/relocate_table, whose
    insertion index is known exactly) or by searching for a native
    ``w14:paraId`` (``locate_by_paraid`` -- used by copy_section, whose final
    position can shift again if the caller also trims the original section
    after inserting the copy).
    """
    try:
        raw2, root2 = _load_docx_xml_stdlib(docx_path)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "error": (
                "post-write verification failed: could not re-read "
                f"{docx_path} after writing it: {exc}"
            )
        }

    body2 = root2.find(_q(_W, "body"))
    if body2 is None:
        return {
            "error": (
                "post-write verification failed: re-read of "
                f"{docx_path} has no <w:body> element"
            )
        }

    actual_counts = _structural_counts([body2])
    actual_counts["image_count"] = _docx_media_count(raw2)

    count_mismatches = {
        key: {"expected": expected, "actual": actual_counts.get(key)}
        for key, expected in expected_counts.items()
        if actual_counts.get(key) != expected
    }

    hash_mismatch: dict[str, str] | None = None
    position_error: str | None = None
    if expected_hash is not None:
        body2_list = list(body2)
        start_idx: int | None = None
        end_idx: int | None = None
        if locate_by_paraid is not None:
            w14_paraId = _q(_W14, "paraId")
            start_idx = next(
                (
                    i
                    for i, el in enumerate(body2_list)
                    if el.get(w14_paraId) == locate_by_paraid
                ),
                None,
            )
            if start_idx is None:
                position_error = (
                    f"expected paraId {locate_by_paraid!r} not found anywhere "
                    f"in {docx_path} after the write"
                )
            else:
                end_idx = start_idx + expected_len
        elif expected_range is not None:
            start_idx, end_idx = expected_range

        if start_idx is not None and end_idx is not None:
            actual_slice = body2_list[start_idx:end_idx]
            actual_hash = _hash_elements(actual_slice)
            if actual_hash != expected_hash:
                hash_mismatch = {"expected": expected_hash, "actual": actual_hash}

    if count_mismatches or hash_mismatch or position_error:
        return {
            "error": (
                "post-write verification failed: the on-disk document does "
                "not match the expected result of this write -- returning "
                "an error instead of a false success payload"
                + (f" ({position_error})" if position_error else "")
            ),
            "count_mismatches": count_mismatches,
            "content_hash_mismatch": hash_mismatch,
        }
    return None


# ---------------------------------------------------------------------------
# 679c86f4 -- post-write image-ownership invariant (orphan image paragraphs +
# duplicate drawing references).
#
# _verify_docx_write (9907df44, above) confirms a move/copy landed the
# EXPECTED structural counts and content hash -- but it has no notion of
# image *ownership*: a bug that detaches an image paragraph from its Figure
# caption (leaving an orphan image with no caption immediately after it), or
# that duplicates an image paragraph without also duplicating its underlying
# media relationship (leaving two independent figure blocks pointing at the
# SAME r:embed id -- confirmed live for copy_section, whose deep-copy pass
# renames every w14:paraId and bookmark name but never touches a drawing's
# r:embed attribute), can still produce structurally-consistent counts and a
# perfectly-matching content hash for the range that moved/copied, because
# neither check ever looks at drawing/relationship identity.
#
# _verify_image_ownership re-reads the file FRESH FROM DISK (same discipline
# as _verify_docx_write) and enforces, for every DIRECT body image paragraph:
#
#   (a) it is immediately followed by a body paragraph carrying a SEQ Figure
#       field (its caption), OR
#   (b) it is part of a run of CONSECUTIVE direct-body image paragraphs (a
#       multi-image composite -- OOXML's own idiom for side-by-side figures
#       sharing one caption) whose LAST member is immediately followed by a
#       shared SEQ Figure caption. A composite is recognised by physical
#       adjacency in body order; nothing but more images may sit between its
#       members.
#
# and, independently, that no r:embed relationship id is referenced by more
# than one such image/composite block -- a duplicate is a strong signal that
# a copy/duplicate operation forgot to mint a fresh media relationship for
# the paragraph it duplicated (rather than the writer's own intentional
# same-image reuse), so it is rejected fail-closed unless the caller
# explicitly opts in via ``allow_relationship_reuse`` (the narrow,
# caller-declared stand-in this hardening item scopes for "the manifest
# declares intentional reuse" -- the full claim/manifest pipeline described
# in fe989980 is a separate, larger sprint item).
#
# insert_image intentionally creates an image paragraph with no caption yet
# (the documented insert_image -> insert_caption two-step composition
# insert_figure_block's own docstring calls out as still-supported), so its
# caller passes ``require_immediate_caption=False`` -- it still gets the
# duplicate-relationship half of the invariant (structurally unreachable via
# insert_image's own always-fresh relationship id, but real defense-in-depth
# against a future id-collision bug) plus its first-ever post-write
# verification.
# ---------------------------------------------------------------------------

def _direct_body_image_paragraphs(body: ET.Element) -> list[tuple[int, ET.Element]]:
    """``(body_child_index, paragraph)`` for every DIRECT body child paragraph
    that contains an embedded image (DrawingML ``<w:drawing>`` or legacy VML
    ``<w:pict>``).

    Restricted to direct body children (not table-cell paragraphs) -- "the
    caption immediately follows the image" is a body-sibling-order relation,
    the same restriction :func:`relocate_figure` already applies when
    selecting a figure block to move.
    """
    w_p = _q(_W, "p")
    w_drawing = _q(_W, "drawing")
    w_pict = _q(_W, "pict")
    return [
        (idx, child)
        for idx, child in enumerate(body)
        if child.tag == w_p
        and (
            child.find(f".//{w_drawing}") is not None
            or child.find(f".//{w_pict}") is not None
        )
    ]


def _has_figure_seq_field(paragraph: ET.Element) -> bool:
    """True when ``paragraph`` carries a ``SEQ Figure`` field (simple or complex)."""
    w_fld_simple = _q(_W, "fldSimple")
    w_instr = _q(_W, "instr")
    w_instr_text = _q(_W, "instrText")
    for field in paragraph.iter(w_fld_simple):
        if _SEQ_FIGURE_RE.search(field.get(w_instr) or ""):
            return True
    for instr in paragraph.iter(w_instr_text):
        if _SEQ_FIGURE_RE.search("".join(instr.itertext())):
            return True
    return False


def _image_paragraph_relationship_ids(paragraph: ET.Element) -> list[str]:
    """Every ``r:embed`` relationship id referenced by drawings inside ``paragraph``."""
    embed_attr = _q(_IMAGE_REL_NS, "embed")
    return [
        blip.get(embed_attr)
        for blip in paragraph.iter(_q(_A, "blip"))
        if blip.get(embed_attr)
    ]


def _verify_image_ownership(
    docx_path: str,
    *,
    require_immediate_caption: bool = True,
    allow_relationship_reuse: bool = False,
) -> dict[str, Any] | None:
    """679c86f4 -- mandatory post-write image-ownership verification.

    Re-reads ``docx_path`` FRESH FROM DISK -- mirrors :func:`_verify_docx_write`'s
    "never trust the in-memory tree that was just serialized" discipline.
    Returns ``None`` when the on-disk document satisfies the image-ownership
    invariant, or an ``{"error": ..., "orphan_image_paragraphs": [...],
    "duplicate_relationships": {...}}`` dict on the first violation -- never a
    false success payload.

    ``require_immediate_caption`` (default ``True``) toggles rule (a)/(b)
    above; pass ``False`` for a write whose OWN contract intentionally leaves
    an image paragraph uncaptioned (see :func:`insert_image`). The duplicate
    ``r:embed`` check always runs regardless of this flag.

    ``allow_relationship_reuse`` (default ``False``, fail closed) is the
    caller's explicit declaration that a detected relationship reuse is
    intentional; when set, a detected duplicate is not reported as a
    violation.
    """
    try:
        raw2, root2 = _load_docx_xml_stdlib(docx_path)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "error": (
                "post-write image-ownership verification failed: could not "
                f"re-read {docx_path} after writing it: {exc}"
            )
        }

    body2 = root2.find(_q(_W, "body"))
    if body2 is None:
        return {
            "error": (
                "post-write image-ownership verification failed: re-read of "
                f"{docx_path} has no <w:body> element"
            )
        }

    body_list = list(body2)
    w_p = _q(_W, "p")
    w14_para_id = _q(_W14, "paraId")
    image_paras = _direct_body_image_paragraphs(body2)

    orphans: list[dict[str, Any]] = []
    relationship_owners: dict[str, list[str]] = {}

    pos = 0
    while pos < len(image_paras):
        # Group a run of CONSECUTIVE direct-body image paragraphs together --
        # OOXML's own idiom for a multi-image composite (side-by-side images
        # sharing one caption) is physical adjacency with nothing but more
        # images in between; a run of length 1 is an ordinary single-image
        # figure.
        group_start = pos
        while (
            pos + 1 < len(image_paras)
            and image_paras[pos + 1][0] == image_paras[pos][0] + 1
        ):
            pos += 1
        group = image_paras[group_start:pos + 1]
        pos += 1

        last_idx = group[-1][0]
        next_idx = last_idx + 1
        has_caption = (
            next_idx < len(body_list)
            and body_list[next_idx].tag == w_p
            and _has_figure_seq_field(body_list[next_idx])
        )

        if require_immediate_caption and not has_caption:
            for member_idx, member_el in group:
                orphans.append(
                    {
                        "para_id": member_el.get(w14_para_id)
                        or f"body-index-{member_idx}",
                        "body_index": member_idx,
                        "composite_size": len(group),
                        "reason": (
                            "image paragraph is not immediately followed by a "
                            "SEQ Figure caption, and is not declared as part "
                            "of a multi-image composite that is"
                            if len(group) == 1
                            else "multi-image composite (adjacent image "
                            "paragraphs) is not immediately followed by a "
                            "shared SEQ Figure caption"
                        ),
                    }
                )

        block_key = group[0][1].get(w14_para_id) or f"body-index-{group[0][0]}"
        for _member_idx, member_el in group:
            for rel_id in _image_paragraph_relationship_ids(member_el):
                relationship_owners.setdefault(rel_id, []).append(block_key)

    duplicate_relationships = {
        rel_id: sorted(set(owners))
        for rel_id, owners in relationship_owners.items()
        if len(set(owners)) > 1
    }
    if allow_relationship_reuse:
        duplicate_relationships = {}

    if orphans or duplicate_relationships:
        return {
            "error": (
                "post-write image-ownership verification failed: the "
                f"on-disk document at {docx_path} violates the image-"
                "ownership invariant -- returning an error instead of a "
                "false success payload"
            ),
            "orphan_image_paragraphs": orphans,
            "duplicate_relationships": duplicate_relationships,
        }
    return None


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
    centered: bool = False,
) -> ET.Element:
    """Build a ``<w:p>`` element for a Word caption using the Caption style.

    Produces::

        <w:p>
          <w:pPr>
            <w:pStyle w:val="Caption"/>
            <w:jc w:val="center"/>     <!-- only when centered=True -->
          </w:pPr>
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

    5b2ce3fb — the bookmark pair's numeric ``w:id`` is derived from
    ``ref_bookmark``'s own digit suffix (every caller sources ``ref_bookmark``
    from :func:`_next_ref_bookmark_name`, or a local seed reserved from it, so
    that suffix is always both present and already document-unique) rather
    than a hardcoded constant.  A previous hardcoded ``w:id="0"`` meant every
    caption inserted into the same document produced ANOTHER bookmarkStart/
    bookmarkEnd pair sharing that same id -- Word-invalid duplicate ``w:id``
    markers that make bookmarkStart/bookmarkEnd pairing ambiguous the moment
    more than one caption (or an internal note, see
    :func:`_build_internal_note_paragraph`) exists in the file. The
    ``w:name`` stays the sole human-readable identifier; ``w:id`` is now just
    as unique, satisfying OOXML's per-document ``w:id`` uniqueness
    requirement for bookmarks.

    4efc63fd — ``centered`` (from ``style_policy["caption_centered"]`` via
    :func:`resolve_style_policy`) adds ``w:jc w:val="center"`` to the
    paragraph; default ``False`` preserves this function's original output.
    """
    if kind not in ("Figure", "Table"):
        raise ValueError(f"caption kind must be 'Figure' or 'Table', got {kind!r}")

    instr = _SEQ_FIGURE_INSTR if kind == "Figure" else _SEQ_TABLE_INSTR

    p = ET.Element(_q(_W, "p"))

    # Paragraph properties: Caption style.
    pPr = ET.SubElement(p, _q(_W, "pPr"))
    pStyle = ET.SubElement(pPr, _q(_W, "pStyle"))
    pStyle.set(_q(_W, "val"), _CAPTION_STYLE)
    if centered:
        ET.SubElement(pPr, _q(_W, "jc"), {_q(_W, "val"): "center"})

    if ref_bookmark:
        # 5b2ce3fb -- reuse ref_bookmark's own digit suffix as the numeric
        # w:id (see docstring above) instead of a hardcoded "0" that
        # collided across every caption in the same document.
        bm_id_match = _REF_BOOKMARK_RE.match(ref_bookmark)
        bm_id = bm_id_match.group(1) if bm_id_match else ref_bookmark
        bm_start = ET.SubElement(p, _q(_W, "bookmarkStart"))
        bm_start.set(_q(_W, "id"), bm_id)
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
        bm_end.set(_q(_W, "id"), bm_id)

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



# ---------------------------------------------------------------------------
# Native image insertion
# ---------------------------------------------------------------------------

_IMAGE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
_IMAGE_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
)
_EMU_PER_INCH = 914400
_IMAGE_TYPES = {
    ".png": ("png", "image/png"),
    ".jpg": ("jpeg", "image/jpeg"),
    ".jpeg": ("jpeg", "image/jpeg"),
    ".gif": ("gif", "image/gif"),
    ".bmp": ("bmp", "image/bmp"),
    ".tif": ("tiff", "image/tiff"),
    ".tiff": ("tiff", "image/tiff"),
}


def _image_dimensions_px(data: bytes, extension: str) -> tuple[int, int] | None:
    """Read common raster dimensions without adding an image-library dependency."""
    if extension == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if extension == ".gif" and data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if extension == ".bmp" and data[:2] == b"BM" and len(data) >= 26:
        return abs(int.from_bytes(data[18:22], "little", signed=True)), abs(
            int.from_bytes(data[22:26], "little", signed=True)
        )
    if extension not in (".jpg", ".jpeg") or not data.startswith(b"\xff\xd8"):
        return None
    offset = 2
    sof_markers = set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8))
    sof_markers |= set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0))
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0xD8, 0xD9):
            continue
        if offset + 2 > len(data):
            break
        segment_length = int.from_bytes(data[offset:offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(data):
            break
        if marker in sof_markers and segment_length >= 7:
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            return width, height
        offset += segment_length
    return None


def _image_size_emu(
    data: bytes,
    extension: str,
    width_inches: float | None,
    height_inches: float | None,
) -> tuple[int, int]:
    dimensions = _image_dimensions_px(data, extension)
    if width_inches is None and height_inches is None:
        width_inches = 6.0
        if dimensions and dimensions[0] > 0 and dimensions[1] > 0:
            height_inches = width_inches * dimensions[1] / dimensions[0]
        else:
            height_inches = 4.0
    elif width_inches is None:
        if not dimensions or dimensions[1] <= 0:
            raise ValueError("width_inches is required when image dimensions cannot be read")
        width_inches = height_inches * dimensions[0] / dimensions[1]
    elif height_inches is None:
        if not dimensions or dimensions[0] <= 0:
            raise ValueError("height_inches is required when image dimensions cannot be read")
        height_inches = width_inches * dimensions[1] / dimensions[0]
    return round(width_inches * _EMU_PER_INCH), round(height_inches * _EMU_PER_INCH)


def _next_relationship_id(rels_root: ET.Element) -> str:
    used = {child.get("Id") for child in rels_root if child.get("Id")}
    number = 1
    while f"rId{number}" in used:
        number += 1
    return f"rId{number}"


def _next_media_name(entries: dict[str, bytes], extension: str) -> str:
    stem = "word/media/image"
    number = 1
    pattern = re.compile(r"^word/media/image(\d+)\.[^.]+$", re.IGNORECASE)
    for name in entries:
        match = pattern.match(name)
        if match:
            number = max(number, int(match.group(1)) + 1)
    candidate = f"{stem}{number}{extension}"
    while candidate in entries:
        number += 1
        candidate = f"{stem}{number}{extension}"
    return candidate


def _build_image_drawing(
    relationship_id: str,
    width_emu: int,
    height_emu: int,
    doc_pr_id: int,
    image_name: str,
) -> ET.Element:
    """Build an inline DrawingML picture in a centered image paragraph."""
    drawing = ET.Element(_q(_W, "drawing"))
    inline = ET.SubElement(drawing, _q(_WP, "inline"))
    for attr in ("distT", "distB", "distL", "distR"):
        inline.set(attr, "0")
    ET.SubElement(
        inline, _q(_WP, "extent"), {"cx": str(width_emu), "cy": str(height_emu)}
    )
    ET.SubElement(
        inline, _q(_WP, "docPr"),
        {"id": str(doc_pr_id), "name": f"Picture {doc_pr_id}"},
    )
    graphic = ET.SubElement(inline, _q(_A, "graphic"))
    graphic_data = ET.SubElement(
        graphic, _q(_A, "graphicData"),
        {"uri": "http://schemas.openxmlformats.org/drawingml/2006/picture"},
    )
    pic = ET.SubElement(graphic_data, _q(_PIC, "pic"))
    nv_pic_pr = ET.SubElement(pic, _q(_PIC, "nvPicPr"))
    ET.SubElement(
        nv_pic_pr, _q(_PIC, "cNvPr"),
        {"id": str(doc_pr_id), "name": os.path.basename(image_name)},
    )
    ET.SubElement(nv_pic_pr, _q(_PIC, "cNvPicPr"))
    blip_fill = ET.SubElement(pic, _q(_PIC, "blipFill"))
    ET.SubElement(
        blip_fill, _q(_A, "blip"), {_q(_IMAGE_REL_NS, "embed"): relationship_id}
    )
    stretch = ET.SubElement(blip_fill, _q(_A, "stretch"))
    ET.SubElement(stretch, _q(_A, "fillRect"))
    sp_pr = ET.SubElement(pic, _q(_PIC, "spPr"))
    xfrm = ET.SubElement(sp_pr, _q(_A, "xfrm"))
    ET.SubElement(xfrm, _q(_A, "off"), {"x": "0", "y": "0"})
    ET.SubElement(
        xfrm, _q(_A, "ext"), {"cx": str(width_emu), "cy": str(height_emu)}
    )
    prst_geom = ET.SubElement(sp_pr, _q(_A, "prstGeom"), {"prst": "rect"})
    ET.SubElement(prst_geom, _q(_A, "avLst"))
    return drawing


def _save_docx_with_image(
    raw: bytes,
    root: ET.Element,
    image_bytes: bytes,
    image_name: str,
    relationship_id: str,
    content_type: str,
    dest: str,
) -> None:
    """Repack a DOCX after changing document.xml, relationships, and media."""
    entries: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        for info in source.infolist():
            entries[info.filename] = source.read(info.filename)

    rels_path = "word/_rels/document.xml.rels"
    rels_xml = entries.get(
        rels_path,
        b'<?xml version="1.0" encoding="UTF-8"?><Relationships '
        b'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
    )
    rels_root = ET.fromstring(rels_xml)
    ET.SubElement(
        rels_root, _q(_PACKAGE_REL_NS, "Relationship"),
        {
            "Id": relationship_id,
            "Type": _IMAGE_REL_TYPE,
            "Target": f"media/{os.path.basename(image_name)}",
        },
    )
    entries[rels_path] = ET.tostring(rels_root, encoding="utf-8", xml_declaration=True)

    content_types_path = "[Content_Types].xml"
    content_types_xml = entries.get(
        content_types_path,
        b'<?xml version="1.0" encoding="UTF-8"?><Types '
        b'xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
    )
    content_types_root = ET.fromstring(content_types_xml)
    extension = os.path.splitext(image_name)[1].lstrip(".")
    has_default = any(
        child.get("Extension", "").lower() == extension.lower()
        for child in content_types_root
        if child.tag.rsplit("}", 1)[-1] == "Default"
    )
    if not has_default:
        ET.SubElement(
            content_types_root, _q(_CONTENT_TYPES_NS, "Default"),
            {"Extension": extension, "ContentType": content_type},
        )
    entries[content_types_path] = ET.tostring(
        content_types_root, encoding="utf-8", xml_declaration=True
    )
    entries["word/document.xml"] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        + ET.tostring(root, encoding="utf-8")
    )
    entries[image_name] = image_bytes

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as destination:
        for name, data in entries.items():
            destination.writestr(name, data)
    if os.path.exists(dest):
        try:
            shutil.copy2(dest, dest + ".bak")
        except OSError:
            pass
    with open(dest, "wb") as handle:
        handle.write(out.getvalue())


def _verify_image_insertion_write(
    docx_path: str,
    *,
    image_para_id: str,
    expected_image_bytes: bytes,
) -> dict[str, Any] | None:
    """efa6cb53 -- post-write verification for :func:`insert_image`.

    Re-reads ``docx_path`` FRESH FROM DISK (never the in-memory tree that was
    just serialized -- that would only re-validate this function's own
    intent, not the actual write) and confirms, in order: the image
    paragraph is present and centered (``w:jc w:val="center"``); it still
    contains a ``<w:drawing>`` whose ``<a:blip>`` references a relationship
    id that ACTUALLY resolves in the freshly re-read
    ``word/_rels/document.xml.rels``; that relationship's target names a
    ``word/media/*`` part that is genuinely present in the freshly re-read
    ZIP package (not merely referenced); that part's bytes match exactly
    what was supposed to be written (a real, uncorrupted new media part, not
    a dangling reference or a truncated/mismatched write); and that
    ``[Content_Types].xml`` declares a content type for it. Returns ``None``
    when every check passes, or an ``{"error": ...}`` dict on the first
    mismatch -- mirroring :func:`_verify_figure_block_write`'s "real error
    instead of a false success payload" discipline (9907df44), which
    :func:`insert_image` itself never had until now.
    """
    try:
        raw2, root2 = _load_docx_xml_stdlib(docx_path)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "error": (
                "post-write verification failed: could not re-read "
                f"{docx_path} after writing it: {exc}"
            )
        }

    body2 = root2.find(_q(_W, "body"))
    if body2 is None:
        return {
            "error": (
                "post-write verification failed: re-read of "
                f"{docx_path} has no <w:body> element"
            )
        }

    w14_para_id = _q(_W14, "paraId")
    image_para = next(
        (el for el in body2.iter(_q(_W, "p")) if el.get(w14_para_id) == image_para_id),
        None,
    )
    if image_para is None:
        return {
            "error": (
                f"post-write verification failed: image paragraph "
                f"{image_para_id!r} not found anywhere in {docx_path} after "
                "the write"
            )
        }
    if _paragraph_alignment(image_para) != "center":
        return {
            "error": (
                "post-write verification failed: image paragraph "
                f"{image_para_id!r} is not centered (w:jc=\"center\") after "
                "the write"
            )
        }

    blip = image_para.find(f".//{_q(_A, 'blip')}")
    relationship_id = blip.get(_q(_IMAGE_REL_NS, "embed")) if blip is not None else None
    if blip is None or not relationship_id:
        return {
            "error": (
                "post-write verification failed: image paragraph "
                f"{image_para_id!r} no longer contains a resolvable "
                "<a:blip r:embed=...> reference after the write"
            )
        }

    try:
        with zipfile.ZipFile(io.BytesIO(raw2)) as archive:
            names = set(archive.namelist())
            rels_bytes = (
                archive.read("word/_rels/document.xml.rels")
                if "word/_rels/document.xml.rels" in names
                else None
            )
            content_types_bytes = (
                archive.read("[Content_Types].xml")
                if "[Content_Types].xml" in names
                else None
            )
            media_bytes: bytes | None = None
            media_part_name: str | None = None
            if rels_bytes is not None:
                rels_root2 = ET.fromstring(rels_bytes)
                relationship = next(
                    (child for child in rels_root2 if child.get("Id") == relationship_id),
                    None,
                )
                if relationship is not None:
                    target = relationship.get("Target") or ""
                    if target.startswith("media/"):
                        media_part_name = f"word/{target}"
                    elif target.startswith("word/media/"):
                        media_part_name = target
                    if media_part_name is not None and media_part_name in names:
                        media_bytes = archive.read(media_part_name)
    except zipfile.BadZipFile as exc:
        return {
            "error": (
                f"post-write verification failed: {docx_path} is not a "
                f"readable ZIP package after the write: {exc}"
            )
        }

    if rels_bytes is None:
        return {
            "error": (
                "post-write verification failed: word/_rels/document.xml.rels "
                f"is missing from {docx_path} after the write"
            )
        }
    if media_part_name is None:
        return {
            "error": (
                "post-write verification failed: relationship "
                f"{relationship_id!r} referenced by the image is missing (or "
                f"does not target a word/media/ part) in "
                f"word/_rels/document.xml.rels of {docx_path} after the write"
            )
        }
    if media_bytes is None:
        return {
            "error": (
                "post-write verification failed: media part "
                f"{media_part_name!r} referenced by relationship "
                f"{relationship_id!r} is not actually present in the ZIP "
                f"package of {docx_path} after the write"
            )
        }
    if hashlib.sha256(media_bytes).hexdigest() != hashlib.sha256(expected_image_bytes).hexdigest():
        return {
            "error": (
                "post-write verification failed: media part "
                f"{media_part_name!r} in {docx_path} does not match the "
                "image bytes that were supposed to be written -- the media "
                "part is present but corrupted or mismatched"
            )
        }

    if content_types_bytes is None:
        return {
            "error": (
                "post-write verification failed: [Content_Types].xml is "
                f"missing from {docx_path} after the write"
            )
        }
    content_types_root2 = ET.fromstring(content_types_bytes)
    extension = os.path.splitext(media_part_name)[1].lstrip(".").lower()
    part_name_abs = f"/{media_part_name}"
    has_content_type = any(
        child.get("Extension", "").lower() == extension
        for child in content_types_root2
        if child.tag.rsplit("}", 1)[-1] == "Default"
    ) or any(
        child.get("PartName", "") == part_name_abs
        for child in content_types_root2
        if child.tag.rsplit("}", 1)[-1] == "Override"
    )
    if not has_content_type:
        return {
            "error": (
                "post-write verification failed: [Content_Types].xml has no "
                f"Default/Override declaring a content type for "
                f"{media_part_name!r} in {docx_path} after the write"
            )
        }

    return None


def insert_image(
    docx_path: str,
    image_path: str,
    anchor_para_id: str | None = None,
    position: str = "after",
    width_inches: float | None = None,
    height_inches: float | None = None,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """Insert a local raster image as a centered inline OOXML figure.

    The new image is always placed in a dedicated paragraph with
    w:jc w:val="center" (the OOXML equivalent of Ctrl+E). When an anchor
    is supplied, position controls whether the image paragraph is inserted
    before or after that direct body paragraph. With no anchor, it is appended
    before the document's final sectPr.

    Supported formats are PNG, JPEG, GIF, BMP, and TIFF. Width/height are in
    inches; if omitted, dimensions are inferred from the image header when
    possible, with a six-inch default width.

    679c86f4/efa6cb53 -- after the write, the file is re-read FRESH FROM DISK
    and checked two ways: the image-ownership invariant
    (:func:`_verify_image_ownership`, ``require_immediate_caption=False``
    since this function's own contract intentionally leaves the new image
    uncaptioned -- pair it with :func:`insert_caption` or use
    :func:`insert_figure_block` for an atomic image+caption insert), and
    that the brand-new relationship+media this call itself created actually
    landed intact (:func:`_verify_image_insertion_write`: the image
    paragraph must be present and centered, its relationship must resolve,
    the referenced media part must genuinely exist in the ZIP package, its
    bytes must match what was written, and its content type must be
    declared). Either check failing fails the write closed: the pre-write
    backup is restored (subject to the same compare-and-swap concurrent-
    writer safety as every other write in this module, guarded against
    clobbering a concurrent writer's already-promoted work -- 5988a5bb) and
    an error is returned instead of a false success payload.

    Returns {status, image_para_id, image_name, docx_path}, or
    {error: message} without mutating the document on validation failure or
    a post-write verification failure that could not be cleanly restored.
    """
    if not isinstance(docx_path, str) or not docx_path:
        return {"error": "docx_path must be a non-empty string"}
    if not isinstance(image_path, str) or not image_path:
        return {"error": "image_path must be a non-empty string"}
    if position not in ("before", "after"):
        return {"error": "position must be before or after"}
    suffix = os.path.splitext(image_path)[1].lower()
    image_type = _IMAGE_TYPES.get(suffix)
    if image_type is None:
        return {"error": f"unsupported image format: {suffix or 'missing extension'}"}
    if width_inches is not None and width_inches <= 0:
        return {"error": "width_inches must be greater than zero"}
    if height_inches is not None and height_inches <= 0:
        return {"error": "height_inches must be greater than zero"}
    try:
        with open(image_path, "rb") as handle:
            image_bytes = handle.read()
    except OSError as exc:
        return {"error": f"could not read image {image_path}: {exc}"}
    if not image_bytes:
        return {"error": f"image file is empty: {image_path}"}
    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
        width_emu, height_emu = _image_size_emu(
            image_bytes, suffix, width_inches, height_inches
        )
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    if body is None:
        return {"error": f"document has no body: {docx_path}"}
    children = list(body)
    if anchor_para_id is None:
        insert_at = next(
            (idx for idx, child in enumerate(children) if child.tag == _q(_W, "sectPr")),
            len(children),
        )
    else:
        located = _find_para_by_id(root, anchor_para_id)
        if located is None:
            return {"error": f"para_id {anchor_para_id!r} not found in {docx_path}"}
        _located_body, anchor, anchor_idx = located
        if _located_body is not body or children[anchor_idx] is not anchor:
            return {
                "error": (
                    "anchor_para_id must identify a direct body paragraph; "
                    "table-cell paragraphs cannot anchor image insertion"
                )
            }
        insert_at = anchor_idx + (1 if position == "after" else 0)

    entries: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        for info in source.infolist():
            entries[info.filename] = source.read(info.filename)
    rels_xml = entries.get("word/_rels/document.xml.rels")
    rels_root = (
        ET.fromstring(rels_xml)
        if rels_xml
        else ET.Element(_q(_PACKAGE_REL_NS, "Relationships"))
    )
    relationship_id = _next_relationship_id(rels_root)
    image_name = _next_media_name(entries, f".{image_type[0]}")
    taken = _existing_para_ids(root)
    image_para_id = _new_para_id(taken)
    doc_pr_id = len(root.findall(f".//{_q(_WP, 'docPr')}")) + 1
    paragraph = ET.Element(_q(_W, "p"))
    paragraph.set(_q(_W14, "paraId"), image_para_id)
    ppr = ET.SubElement(paragraph, _q(_W, "pPr"))
    ET.SubElement(ppr, _q(_W, "jc"), {_q(_W, "val"): "center"})
    run = ET.SubElement(paragraph, _q(_W, "r"))
    run.append(
        _build_image_drawing(
            relationship_id, width_emu, height_emu, doc_pr_id, image_name
        )
    )
    body.insert(insert_at, paragraph)

    # 679c86f4/efa6cb53 -- hold docx_path's promotion lock across
    # stage+promote (_save_docx_with_image is not itself lock-aware)
    # THROUGH both post-write verification passes (image-ownership, then
    # insertion-write) and any conditional restore below, matching the same
    # discipline insert_figure_block already applies to this same save
    # helper (5988a5bb) so a brand-new media part and relationship are
    # never reported as "inserted" unless a fresh re-read from disk
    # actually proves it landed intact. promoted_sha256 is computed
    # locally right after the write for the same reason insert_figure_
    # block's own comment gives: this function is one of
    # _save_docx_with_image's several callers, so the helper itself cannot
    # return a shared transaction dict.
    with _docx_promotion_lock(docx_path):
        try:
            _save_docx_with_image(
                raw, root, image_bytes, image_name, relationship_id,
                image_type[1], docx_path
            )
        except OSError as exc:
            return {"error": f"could not write {docx_path}: {exc}"}

        promoted_sha256 = _docx_file_sha256(docx_path)

        verify_error = _verify_image_ownership(
            docx_path, require_immediate_caption=False
        )
        if verify_error is None:
            verify_error = _verify_image_insertion_write(
                docx_path,
                image_para_id=image_para_id,
                expected_image_bytes=image_bytes,
            )
        if verify_error is not None:
            # 5988a5bb -- do NOT blindly restore: a different (concurrent)
            # writer may have already promoted something newer to docx_path
            # since our own promotion, in which case this verification
            # "failure" is a false positive and restoring from our own
            # backup would destroy that writer's completed, already-
            # promoted work.
            safe_to_restore, restored, concurrent_write_detected = (
                _safe_restore_after_verification_failure(docx_path, promoted_sha256)
            )
            verify_error["file_restored"] = restored
            verify_error["concurrent_write_detected"] = concurrent_write_detected
            if not safe_to_restore:
                if concurrent_write_detected:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- AND a different writer's promotion has landed on "
                        "this file since ours, so this verification failure "
                        "could not be safely auto-corrected: restoring from our "
                        "own backup would destroy that writer's already-promoted "
                        f"work. {docx_path} was left untouched, exactly as that "
                        "other writer left it -- investigate manually."
                    )
                else:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- this write's own promotion fingerprint is "
                        "unavailable, so it could not be safely confirmed that "
                        "restoring from backup would not destroy a different "
                        f"writer's work; {docx_path} was left untouched rather "
                        "than risk it -- investigate manually."
                    )
            verify_error["image_para_id"] = image_para_id
            verify_error["docx_path"] = docx_path
            return verify_error

    _invalidate_sidecar_mtime(index_db_path)
    return {
        "status": "inserted",
        "image_para_id": image_para_id,
        "image_name": image_name,
        "docx_path": docx_path,
    }


def _verify_figure_block_write(
    docx_path: str,
    *,
    image_para_id: str,
    expected_seq_number: int,
    expected_label_text: str,
) -> dict[str, Any] | None:
    """19be1551 — post-write verification for :func:`insert_figure_block`.

    Re-reads ``docx_path`` FRESH FROM DISK (never the in-memory tree that was
    just serialized -- that would only re-validate this function's own
    intent, not the actual write) and confirms, in order: the image
    paragraph is present and centered (``w:jc w:val="center"``); the very
    next body element is a paragraph (no other paragraph -- and in
    particular no OTHER image -- landed between them); that paragraph
    carries a ``SEQ Figure`` field whose cached number matches
    ``expected_seq_number``; and its rendered text contains
    ``expected_label_text``. Returns ``None`` when every check passes, or an
    ``{"error": ...}`` dict on the first mismatch -- mirroring
    :func:`_verify_docx_write`'s "real error instead of a false success
    payload" discipline (9907df44) for the one case that helper does not
    already cover (a newly inserted pair with no prior on-disk baseline to
    diff against).
    """
    try:
        _raw2, root2 = _load_docx_xml_stdlib(docx_path)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "error": (
                "post-write verification failed: could not re-read "
                f"{docx_path} after writing it: {exc}"
            )
        }

    body2 = root2.find(_q(_W, "body"))
    if body2 is None:
        return {
            "error": (
                "post-write verification failed: re-read of "
                f"{docx_path} has no <w:body> element"
            )
        }

    body_list = list(body2)
    w14_para_id = _q(_W14, "paraId")
    image_idx = next(
        (i for i, el in enumerate(body_list) if el.get(w14_para_id) == image_para_id),
        None,
    )
    if image_idx is None:
        return {
            "error": (
                f"post-write verification failed: image paragraph "
                f"{image_para_id!r} not found anywhere in {docx_path} after "
                "the write"
            )
        }

    image_para = body_list[image_idx]
    if image_para.find(f".//{_q(_W, 'drawing')}") is None:
        return {
            "error": (
                "post-write verification failed: paragraph "
                f"{image_para_id!r} no longer contains a <w:drawing> image "
                "after the write"
            )
        }
    if _paragraph_alignment(image_para) != "center":
        return {
            "error": (
                "post-write verification failed: image paragraph "
                f"{image_para_id!r} is not centered (w:jc=\"center\") after "
                "the write"
            )
        }

    if image_idx + 1 >= len(body_list):
        return {
            "error": (
                "post-write verification failed: no paragraph immediately "
                f"follows the image paragraph {image_para_id!r} after the "
                "write -- the caption is missing"
            )
        }
    caption_para = body_list[image_idx + 1]
    if caption_para.tag != _q(_W, "p"):
        return {
            "error": (
                "post-write verification failed: the element immediately "
                f"following image paragraph {image_para_id!r} is not a "
                "paragraph"
            )
        }

    fld = caption_para.find(_q(_W, "fldSimple"))
    instr = fld.get(_q(_W, "instr")) if fld is not None else None
    if fld is None or "SEQ Figure" not in (instr or ""):
        return {
            "error": (
                "post-write verification failed: the paragraph immediately "
                f"following image paragraph {image_para_id!r} does not "
                "contain a SEQ Figure field"
            )
        }
    seq_text_el = fld.find(f".//{_q(_W, 't')}")
    seq_text = seq_text_el.text if seq_text_el is not None else None
    if seq_text != str(expected_seq_number):
        return {
            "error": (
                "post-write verification failed: caption SEQ number "
                f"mismatch (expected {expected_seq_number!r}, got "
                f"{seq_text!r})"
            )
        }

    caption_text = "".join(t.text or "" for t in caption_para.iter(_q(_W, "t")))
    if expected_label_text not in caption_text:
        return {
            "error": (
                "post-write verification failed: caption label text "
                f"mismatch (expected to contain {expected_label_text!r}, "
                f"got {caption_text!r})"
            )
        }
    return None


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
) -> dict[str, Any]:
    """19be1551 — atomically insert a centered image paragraph AND its
    adjacent real SEQ Figure caption in a SINGLE document-load-mutate-save
    transaction.

    This is deliberately NOT the same as calling :func:`insert_image` then
    :func:`insert_caption` back to back: that composition already "works"
    today but performs two separate zip rewrites, so a failure between them
    can leave an orphan image with no caption, or a caption whose SEQ number
    raced against a concurrent writer. Here both paragraphs are built
    against ONE shared in-memory document tree and reach disk via exactly
    one call to :func:`_save_docx_with_image` -- there is no window in which
    only one half of the pair exists on disk.

    Anchor resolution is identical to :func:`insert_image`: ``anchor_para_id``
    must name a direct body paragraph, and ``position`` ("before"/"after",
    default "after") places the IMAGE paragraph relative to it; ``None``
    appends the block before the document's trailing ``sectPr``. The caption
    paragraph is always inserted immediately after the image paragraph --
    that placement is not itself configurable, mirroring
    :func:`insert_caption`'s own rule that a Figure caption can never precede
    its image.

    The caption is built exactly like :func:`insert_caption` with
    ``kind="Figure"``: ``seq_number`` is the count of existing SEQ Figure
    captions in the document plus one, and ``ref_bookmark`` is a fresh
    ``_Ref<digits>`` cross-reference bookmark -- both computed against the
    document tree BEFORE either new paragraph is inserted, exactly like
    :func:`insert_caption` does, which is safe here specifically because
    atomicity removes the race a second writer's caption could otherwise
    land in between. ``style_policy["caption_centered"]`` (via
    :func:`resolve_style_policy`) controls whether the caption itself also
    gets ``w:jc w:val="center"``; the image paragraph is always centered
    regardless of style_policy or of any alignment the anchor paragraph
    happens to carry -- the new paragraph's own ``w:jc`` is authoritative and
    never inherits a neighbor's alignment.

    After the single save, the file is re-read FRESH FROM DISK and verified
    (see :func:`_verify_figure_block_write`): the image paragraph must be
    present and centered, the caption must immediately follow with nothing
    in between, and its SEQ number/label text must match what was written.
    On a verification failure the pre-write backup :func:`_save_docx_with_image`
    left at ``docx_path + ".bak"`` is restored via :func:`_restore_docx_backup`
    -- the same backup-then-restore mechanism this module's other post-write
    verification failures already use (9907df44) -- and an error is returned
    instead of a false success payload.

    Supported image formats, dimension inference, and the six-inch default
    width all match :func:`insert_image`.

    ddd79188 -- AFTER structural verification passes, this also invokes
    :func:`render_gate.check_render_capability` on the just-written
    ``docx_path`` (see :func:`_enforce_render_verification`) -- structural
    XML re-parse alone (the check above) can never prove the document
    actually opens/renders correctly in Word. ``"rendered"`` continues
    normally with render evidence attached to the success payload.
    ``"failed"`` (a render backend WAS available but errored on this
    document) restores ``docx_path`` from the SAME pre-write backup and
    returns an error -- exactly like a structural verification failure.
    ``"unavailable-with-reason"`` (no render backend in this environment)
    ALSO fails closed by default -- it is never reported as verified --
    unless the caller explicitly passes ``allow_degraded_render=True`` with
    a non-empty ``degraded_render_reason``, an audited opt-in that keeps the
    write but stamps ``render_verified=False`` / ``render_degraded=True`` on
    the payload rather than silently treating "could not check" as "passed".

    Returns ``{status, image_para_id, image_name, kind, seq_number,
    label_text, section_heading, ref_bookmark, docx_path, render_status,
    render_verified, ...}``, or ``{"error": message}`` without mutating the
    document on validation failure, structural verification failure, or
    render-verification failure that could not be cleanly restored.
    """
    if not isinstance(docx_path, str) or not docx_path:
        return {"error": "docx_path must be a non-empty string"}
    if not isinstance(image_path, str) or not image_path:
        return {"error": "image_path must be a non-empty string"}
    if position not in ("before", "after"):
        return {"error": "position must be before or after"}
    if not label_text or not str(label_text).strip():
        return {"error": "label_text must be a non-empty string"}
    if allow_degraded_render and not (
        degraded_render_reason and str(degraded_render_reason).strip()
    ):
        return {
            "error": (
                "degraded_render_reason is required and must be non-empty "
                "when allow_degraded_render=True -- an audited degrade with "
                "no stated reason is not auditable and is refused"
            )
        }
    suffix = os.path.splitext(image_path)[1].lower()
    image_type = _IMAGE_TYPES.get(suffix)
    if image_type is None:
        return {"error": f"unsupported image format: {suffix or 'missing extension'}"}
    if width_inches is not None and width_inches <= 0:
        return {"error": "width_inches must be greater than zero"}
    if height_inches is not None and height_inches <= 0:
        return {"error": "height_inches must be greater than zero"}
    try:
        policy = resolve_style_policy(style_policy)
    except ValueError as exc:
        return {"error": str(exc)}
    try:
        with open(image_path, "rb") as handle:
            image_bytes = handle.read()
    except OSError as exc:
        return {"error": f"could not read image {image_path}: {exc}"}
    if not image_bytes:
        return {"error": f"image file is empty: {image_path}"}
    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
        width_emu, height_emu = _image_size_emu(
            image_bytes, suffix, width_inches, height_inches
        )
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    if body is None:
        return {"error": f"document has no body: {docx_path}"}
    children = list(body)
    if anchor_para_id is None:
        insert_at = next(
            (idx for idx, child in enumerate(children) if child.tag == _q(_W, "sectPr")),
            len(children),
        )
    else:
        located = _find_para_by_id(root, anchor_para_id)
        if located is None:
            return {"error": f"para_id {anchor_para_id!r} not found in {docx_path}"}
        _located_body, anchor, anchor_idx = located
        if _located_body is not body or children[anchor_idx] is not anchor:
            return {
                "error": (
                    "anchor_para_id must identify a direct body paragraph; "
                    "table-cell paragraphs cannot anchor image insertion"
                )
            }
        insert_at = anchor_idx + (1 if position == "after" else 0)

    entries: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        for info in source.infolist():
            entries[info.filename] = source.read(info.filename)
    rels_xml = entries.get("word/_rels/document.xml.rels")
    rels_root = (
        ET.fromstring(rels_xml)
        if rels_xml
        else ET.Element(_q(_PACKAGE_REL_NS, "Relationships"))
    )
    relationship_id = _next_relationship_id(rels_root)
    image_name = _next_media_name(entries, f".{image_type[0]}")
    taken = _existing_para_ids(root)
    image_para_id = _new_para_id(taken)
    doc_pr_id = len(root.findall(f".//{_q(_WP, 'docPr')}")) + 1

    # Caption metadata (seq_number, ref_bookmark) is computed against the
    # ORIGINAL root, before either new paragraph is inserted -- identical
    # timing to insert_caption's own computation. Safe here specifically
    # because both paragraphs land in the same save transaction: there is no
    # window for a second writer's caption to land in between and shift the
    # count out from under us.
    seq_number = _count_seq_captions(root, "Figure") + 1
    ref_bookmark = _next_ref_bookmark_name(root)
    label_text_clean = label_text.strip()

    paragraph = ET.Element(_q(_W, "p"))
    paragraph.set(_q(_W14, "paraId"), image_para_id)
    ppr = ET.SubElement(paragraph, _q(_W, "pPr"))
    ET.SubElement(ppr, _q(_W, "jc"), {_q(_W, "val"): "center"})
    run = ET.SubElement(paragraph, _q(_W, "r"))
    run.append(
        _build_image_drawing(
            relationship_id, width_emu, height_emu, doc_pr_id, image_name
        )
    )

    caption_p = _build_caption_paragraph(
        kind="Figure",
        label_text=label_text_clean,
        seq_cached=str(seq_number),
        ref_bookmark=ref_bookmark,
        centered=policy["caption_centered"],
    )

    body.insert(insert_at, paragraph)
    body.insert(insert_at + 1, caption_p)

    # 5988a5bb -- hold docx_path's promotion lock across stage+promote
    # (_save_docx_with_image, which is not itself lock-aware, so this
    # caller must bracket it explicitly) THROUGH the post-write verify and
    # any conditional restore below, closing the same-process window
    # between promotion and verify/restore entirely (see
    # _docx_promotion_lock's module-level comment). promoted_sha256 is
    # computed locally right after the write (rather than returned from
    # _save_docx_with_image, which has other callers -- insert_image /
    # insert_caption -- this fix does not touch) since it is simply
    # docx_path's own fresh-from-disk fingerprint immediately after THIS
    # writer's own promotion.
    with _docx_promotion_lock(docx_path):
        try:
            _save_docx_with_image(
                raw, root, image_bytes, image_name, relationship_id,
                image_type[1], docx_path
            )
        except OSError as exc:
            return {"error": f"could not write {docx_path}: {exc}"}

        promoted_sha256 = _docx_file_sha256(docx_path)

        verify_error = _verify_figure_block_write(
            docx_path,
            image_para_id=image_para_id,
            expected_seq_number=seq_number,
            expected_label_text=label_text_clean,
        )
        if verify_error is not None:
            # 5988a5bb -- do NOT blindly restore: a different (concurrent)
            # writer may have already promoted something newer to docx_path
            # since our own promotion, in which case this verification
            # "failure" is a false positive and restoring from our own
            # backup would destroy that writer's completed, already-
            # promoted work.
            safe_to_restore, restored, concurrent_write_detected = (
                _safe_restore_after_verification_failure(docx_path, promoted_sha256)
            )
            verify_error["file_restored"] = restored
            verify_error["concurrent_write_detected"] = concurrent_write_detected
            if not safe_to_restore:
                if concurrent_write_detected:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- AND a different writer's promotion has landed on "
                        "this file since ours, so this verification failure "
                        "could not be safely auto-corrected: restoring from our "
                        "own backup would destroy that writer's already-promoted "
                        f"work. {docx_path} was left untouched, exactly as that "
                        "other writer left it -- investigate manually."
                    )
                else:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- this write's own promotion fingerprint is "
                        "unavailable, so it could not be safely confirmed that "
                        "restoring from backup would not destroy a different "
                        f"writer's work; {docx_path} was left untouched rather "
                        "than risk it -- investigate manually."
                    )
            verify_error["image_para_id"] = image_para_id
            verify_error["docx_path"] = docx_path
            return verify_error

        # ddd79188 -- structural verification alone (above) can never prove
        # the document actually renders in Word; run the real render-
        # capability gate now, still inside the promotion lock so a
        # fail-closed restore has the same CAS safety a structural failure
        # gets. Must run AFTER structural verification, not instead of it.
        render_error, render_info = _enforce_render_verification(
            docx_path,
            promoted_sha256=promoted_sha256,
            allow_degraded_render=allow_degraded_render,
            degraded_render_reason=degraded_render_reason,
        )
        if render_error is not None:
            render_error["image_para_id"] = image_para_id
            render_error["docx_path"] = docx_path
            return render_error

    _invalidate_sidecar_mtime(index_db_path)
    if index_db_path and os.path.exists(index_db_path):
        _upsert_sidecar_caption(
            index_db_path=index_db_path,
            kind="Figure",
            para_id=None,  # newly inserted caption para has no w14:paraId yet
            seq_number=str(seq_number),
            caption_text=f"Figure {seq_number}. {label_text_clean}",
            section_heading=section_heading,
            ref_bookmark=ref_bookmark,
        )

    return {
        "status": "inserted",
        "image_para_id": image_para_id,
        "image_name": image_name,
        "kind": "Figure",
        "seq_number": seq_number,
        "label_text": label_text_clean,
        "section_heading": section_heading,
        "ref_bookmark": ref_bookmark,
        "docx_path": docx_path,
        **render_info,
    }


# ---------------------------------------------------------------------------
# d371b00b -- verified DOCX package-part and relationship add/remove
# primitives.
#
# Two directions over the SAME package-level infrastructure
# insert_image/insert_figure_block already use (word/_rels/document.xml.rels
# relationships, [Content_Types].xml Default/Override entries, word/media/*
# parts):
#
#   1. :func:`remove_docx_package_part` -- dry-run-capable, reference-counted
#      REMOVAL. Never deletes a part that is still referenced anywhere in
#      word/document.xml (a real refusal, not a silent skip); on a real
#      removal, cleans up the relationship(s) that pointed at the deleted
#      part and any [Content_Types].xml entry that is no longer needed.
#   2. :func:`insert_docx_media_part` -- safe INSERTION of a brand-new
#      image/media package member: collision-free relationship id + media
#      part name (reusing :func:`_next_relationship_id` /
#      :func:`_next_media_name`), matching Default/Override content-type
#      entries, and the same drawing/frame-extent construction
#      :func:`insert_figure_block` uses (:func:`_build_image_drawing` /
#      :func:`_image_size_emu`) -- plus an explicit post-write
#      relationship<->media BIJECTION check before the write is ever reported
#      as successful.
#
# Both route through the SAME transactional backup/CAS-safe write envelope
# every other writer in this module uses (:func:`_atomic_write_docx_bytes`
# via a thin per-primitive save helper), hold :func:`_docx_promotion_lock`
# across their own stage+promote -> verify -> conditional-restore sequence
# (5988a5bb discipline), and run the SAME tri-state real-render canary
# (:func:`_enforce_render_verification`) after structural verification
# passes -- on Windows a failed/unavailable render fails the write closed by
# default; ``allow_degraded_render=True`` + a non-empty
# ``degraded_render_reason`` is the same audited opt-in
# :func:`insert_figure_block` already exposes.
#
# Scope (documented, not silently assumed): both primitives operate on
# word/media/* parts reachable via word/_rels/document.xml.rels and
# referenced from word/document.xml only -- header/footer-embedded media
# (their own separate word/_rels/header<N>.xml.rels parts) are out of scope
# for this sprint item, matching this module's existing image tooling
# (insert_image/insert_figure_block/find_image_paragraph are all
# document-body-only too).
# ---------------------------------------------------------------------------


def _docx_zip_entries(raw: bytes) -> dict[str, bytes]:
    """Every ZIP member of ``raw`` as an in-memory ``{name: bytes}`` map.

    Same "load once, mutate the dict, repack" shape :func:`insert_image` /
    :func:`insert_figure_block` / :func:`_save_docx_with_image` already build
    inline at each call site -- factored out here so the two new package-part
    primitives (and their shared helpers) don't each re-derive it.
    """
    entries: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        for info in source.infolist():
            entries[info.filename] = source.read(info.filename)
    return entries


def _rel_target_part_name(target: str, part_dir: str = "word") -> str:
    """Resolve a ``<Relationship Target="...">`` value into a full ZIP member
    path, relative to ``part_dir`` (the directory the ``.rels`` part itself
    lives alongside -- ``"word"`` for ``word/_rels/document.xml.rels``).

    Handles a leading ``"/"`` (package-root-absolute, per OPC) and ``".."``
    segments (a relationship target may legally climb out of ``part_dir``,
    though real Word output for media never does).
    """
    if target.startswith("/"):
        return target.lstrip("/")
    combined = f"{part_dir}/{target}"
    parts: list[str] = []
    for segment in combined.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)
    return "/".join(parts)


def _docx_relationships_targeting_part(
    rels_root: ET.Element, part_name: str, part_dir: str = "word"
) -> list[str]:
    """Every ``Relationship`` id in ``rels_root`` whose (resolved, non-
    External) ``Target`` is ``part_name``."""
    matches: list[str] = []
    for child in rels_root:
        if child.get("TargetMode") == "External":
            continue
        target = child.get("Target")
        rid = child.get("Id")
        if not target or not rid:
            continue
        if _rel_target_part_name(target, part_dir) == part_name:
            matches.append(rid)
    return matches


def _docx_attribute_value_reference_count(xml_bytes: bytes, value: str) -> int:
    """Count every element attribute anywhere in ``xml_bytes`` whose VALUE is
    exactly ``value``.

    Deliberately namespace/attribute-name agnostic -- a relationship id can
    be referenced as ``r:embed`` (inline image), ``r:link`` (linked image),
    ``r:id`` (hyperlink/OLE/chart/etc.), or other relationship-typed
    attributes this module does not enumerate individually; scanning every
    attribute VALUE in the tree is the only way to get a real reference
    count instead of an incomplete allowlist that silently under-counts.
    Returns 0 (never raises) for unparsable XML -- a caller treats that as
    "could not confirm any reference", which is the fail-closed direction
    for a REMOVAL gate (see :func:`remove_docx_package_part`).
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return 0
    count = 0
    for el in root.iter():
        for attr_value in el.attrib.values():
            if attr_value == value:
                count += 1
    return count


def _docx_media_bijection_report(
    media_part_names: "set[str] | list[str]",
    rels_root: ET.Element,
    part_dir: str = "word",
) -> dict[str, Any]:
    """Package-wide relationship<->media reference map for the parts named in
    ``media_part_names`` (typically every ``word/media/*`` entry).

    Returns ``{"media_to_relationship_ids": {part_name: [rel_id, ...]},
    "orphaned_media": [...], "dangling_relationships": [...],
    "shared_media": {part_name: [rel_id, ...]}}``:

    * ``orphaned_media`` -- media parts targeted by ZERO image relationships
      (candidates :func:`remove_docx_package_part` would report as safe to
      remove).
    * ``dangling_relationships`` -- image-type relationship ids whose Target
      does not resolve to any part in ``media_part_names`` (package
      inconsistency: a relationship pointing at nothing).
    * ``shared_media`` -- media parts targeted by MORE THAN ONE relationship
      id. Not itself an error (legitimate relationship reuse is possible),
      but :func:`insert_docx_media_part`'s own post-write bijection check
      treats its OWN brand-new media part appearing here as a hard failure --
      a freshly inserted part must be a clean 1:1 pairing with the
      relationship this write itself created.
    """
    media_to_rel_ids: dict[str, list[str]] = {name: [] for name in media_part_names}
    dangling: list[str] = []
    for child in rels_root:
        rel_type = child.get("Type") or ""
        if not rel_type.endswith("/image"):
            continue
        if child.get("TargetMode") == "External":
            continue
        target = child.get("Target")
        rid = child.get("Id")
        if not target or not rid:
            continue
        resolved = _rel_target_part_name(target, part_dir)
        if resolved in media_to_rel_ids:
            media_to_rel_ids[resolved].append(rid)
        else:
            dangling.append(rid)
    orphaned = [name for name, rids in media_to_rel_ids.items() if not rids]
    shared = {name: rids for name, rids in media_to_rel_ids.items() if len(rids) > 1}
    return {
        "media_to_relationship_ids": media_to_rel_ids,
        "orphaned_media": orphaned,
        "dangling_relationships": dangling,
        "shared_media": shared,
    }


def _cleanup_content_types_after_removal(
    content_types_root: ET.Element,
    removed_part_name: str,
    remaining_entries: "dict[str, bytes] | set[str]",
) -> dict[str, Any]:
    """Mutate ``content_types_root`` in place to drop [Content_Types].xml
    entries made unnecessary by removing ``removed_part_name``.

    1. Any ``Override`` entry naming ``removed_part_name`` exactly is always
       dropped -- an Override is per-PartName, so it is unconditionally
       orphaned once that part is gone.
    2. The ``Default`` entry for ``removed_part_name``'s extension is dropped
       ONLY if no part remaining in ``remaining_entries`` still needs it --
       i.e. no other remaining part shares that extension without its own
       Override (Default is a package-wide, extension-keyed fallback other
       parts may legitimately still rely on).

    Returns ``{"removed_overrides": [...], "removed_defaults": [...]}`` for
    the caller's dry-run preview / success payload. Never raises.
    """
    ct_override = _q(_CONTENT_TYPES_NS, "Override")
    ct_default = _q(_CONTENT_TYPES_NS, "Default")
    target_partname = "/" + removed_part_name.lstrip("/")

    removed_overrides: list[str] = []
    for child in list(content_types_root):
        if child.tag == ct_override and child.get("PartName") == target_partname:
            content_types_root.remove(child)
            removed_overrides.append(target_partname)

    removed_defaults: list[str] = []
    extension = os.path.splitext(removed_part_name)[1].lstrip(".").lower()
    if extension:
        override_partnames = {
            child.get("PartName", "").lstrip("/")
            for child in content_types_root
            if child.tag == ct_override
        }
        still_needed = any(
            os.path.splitext(name)[1].lstrip(".").lower() == extension
            and name not in override_partnames
            for name in remaining_entries
        )
        if not still_needed:
            for child in list(content_types_root):
                if (
                    child.tag == ct_default
                    and child.get("Extension", "").lower() == extension
                ):
                    content_types_root.remove(child)
                    removed_defaults.append(extension)
    return {"removed_overrides": removed_overrides, "removed_defaults": removed_defaults}


def _save_docx_with_part_removed_stdlib(
    raw: bytes,
    *,
    remove_part_names: tuple[str, ...],
    updated_parts: dict[str, bytes],
    dest: str,
) -> dict[str, Any]:
    """Repackage ``raw`` with ``remove_part_names`` DROPPED and
    ``updated_parts`` applied (overwritten if pre-existing, appended if not),
    routed through the SAME :func:`_atomic_write_docx_bytes` stage -> verify
    -> promote transaction every other writer in this module uses.

    The genuinely new capability :func:`_save_docx_with_new_parts_stdlib`
    does not have: that writer can add or overwrite parts but never drops
    one. ``protected_keys=("style_count",)`` -- media/relationship counts are
    EXPECTED to decrease by exactly the removal this call performs; the
    caller (:func:`remove_docx_package_part`) verifies that precise delta
    itself post-write (see :func:`_verify_part_removal_write`) rather than
    relying on an exact-match invariant here, which has no way to express
    "decreased by exactly N".
    """
    out = io.BytesIO()
    removed = set(remove_part_names)
    written: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(raw)) as src:
        infos = src.infolist()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in infos:
                if info.filename in removed:
                    continue
                data = src.read(info.filename)
                if info.filename in updated_parts:
                    data = updated_parts[info.filename]
                dst.writestr(info, data)
                written.add(info.filename)
            for part_name, data in updated_parts.items():
                if part_name not in written and part_name not in removed:
                    dst.writestr(part_name, data)

    return _atomic_write_docx_bytes(
        out.getvalue(),
        dest,
        pre_manifest=_docx_structural_manifest(raw),
        protected_keys=("style_count",),
        changed_parts=dict(updated_parts),
    )


def _verify_part_removal_write(
    docx_path: str,
    *,
    removed_part_name: str,
    removed_relationship_ids: list[str],
) -> dict[str, Any] | None:
    """d371b00b post-write verification for :func:`remove_docx_package_part`.

    Re-reads ``docx_path`` FRESH FROM DISK (never the in-memory state this
    function's own caller just built -- same discipline as every other
    ``_verify_*`` helper in this module) and confirms: ``removed_part_name``
    is genuinely gone from the ZIP; every relationship id in
    ``removed_relationship_ids`` is genuinely gone from
    ``word/_rels/document.xml.rels``; and the resulting package has NO
    dangling image relationship (a relationship whose Target no longer
    resolves to any part -- the bijection invariant, checked package-wide
    here since a removal is the one operation that can introduce a dangling
    reference elsewhere if this function's own relationship cleanup missed
    something). Returns ``None`` on success, an ``{"error": ...}`` dict on
    the first violation.
    """
    try:
        raw2, _root2 = _load_docx_xml_stdlib(docx_path)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "error": (
                "post-write verification failed: could not re-read "
                f"{docx_path} after writing it: {exc}"
            )
        }

    with zipfile.ZipFile(io.BytesIO(raw2)) as zf2:
        names2 = set(zf2.namelist())
        if removed_part_name in names2:
            return {
                "error": (
                    "post-write verification failed: package part "
                    f"{removed_part_name!r} is still present in {docx_path} "
                    "after removal"
                )
            }
        rels_path = "word/_rels/document.xml.rels"
        rels_root2 = (
            ET.fromstring(zf2.read(rels_path))
            if rels_path in names2
            else ET.Element(_q(_PACKAGE_REL_NS, "Relationships"))
        )
        remaining_ids = {child.get("Id") for child in rels_root2}
        leftover = [rid for rid in removed_relationship_ids if rid in remaining_ids]
        if leftover:
            return {
                "error": (
                    f"post-write verification failed: relationship id(s) "
                    f"{leftover!r} that should have been removed with "
                    f"{removed_part_name!r} are still present in {rels_path}"
                )
            }
        media_names = {name for name in names2 if name.startswith("word/media/")}
        bijection = _docx_media_bijection_report(media_names, rels_root2)
        if bijection["dangling_relationships"]:
            return {
                "error": (
                    "post-write verification failed: removal left dangling "
                    f"relationship id(s) {bijection['dangling_relationships']!r} "
                    "whose target part no longer exists"
                ),
                "dangling_relationships": bijection["dangling_relationships"],
            }
    return None


def remove_docx_package_part(
    docx_path: str,
    part_name: str,
    dry_run: bool = True,
    index_db_path: str | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
) -> dict[str, Any]:
    """d371b00b -- reference-counted, dry-run-capable removal of an
    unreferenced ``word/media/*`` package part and its relationship(s).

    ``part_name`` (e.g. ``"word/media/image3.png"``) must name a
    ``word/media/*`` ZIP member -- removal of any other package part
    (``word/document.xml``, style/rels/content-types infrastructure, etc.)
    is refused outright; this primitive's scope is deliberately narrow
    (arbitrary-part removal is explicitly NOT supported).

    Reference counting: every relationship in
    ``word/_rels/document.xml.rels`` whose Target resolves to ``part_name``
    is found first; then ``word/document.xml`` is scanned for any attribute
    whose value equals one of those relationship ids (a real reference
    count, not a heuristic scoped to just ``<a:blip r:embed>`` -- see
    :func:`_docx_attribute_value_reference_count`). A part with a NONZERO
    reference count is REFUSED -- a real ``{"error": ...}`` result with
    ``status="refused_still_referenced"``, never a silent skip -- identically
    whether ``dry_run`` is ``True`` or ``False``, since dry-run's entire
    point is to preview the exact decision a real run would make.

    ``dry_run=True`` (the default -- fail-safe): for a genuinely
    zero-reference part, reports exactly what WOULD be removed
    (``relationship_ids``, and which [Content_Types].xml Default/Override
    entries would be cleaned up) WITHOUT touching the zip at all.

    ``dry_run=False``: performs the removal for real, through the SAME
    transactional backup/CAS-safe write envelope every other writer in this
    module uses (:func:`_save_docx_with_part_removed_stdlib` ->
    :func:`_atomic_write_docx_bytes`), holding :func:`_docx_promotion_lock`
    across stage+promote -> :func:`_verify_part_removal_write` ->
    conditional restore (5988a5bb discipline: a verification failure is
    restored from backup ONLY when compare-and-swap confirms no OTHER
    writer's promotion has landed since ours -- see
    :func:`_safe_restore_after_verification_failure`). After structural
    verification passes, the same tri-state real-render canary
    :func:`insert_figure_block` uses
    (:func:`_enforce_render_verification`) gates final promotion:
    ``allow_degraded_render``/``degraded_render_reason`` are the identical
    audited opt-in for the "no render backend available" case.

    Returns, on success: ``{"status": "dry_run"|"removed", "part_name",
    "relationship_ids"|"relationship_ids_removed",
    "content_type_overrides_removed", "content_type_defaults_removed",
    "reference_count": 0, "docx_path", ...render fields on a real
    removal...}``. On refusal: ``{"error": ..., "status":
    "refused_still_referenced", "reference_count", "part_name",
    "referencing_relationship_ids"}``. On any other failure: ``{"error":
    ...}`` without mutating the document.
    """
    if not isinstance(docx_path, str) or not docx_path:
        return {"error": "docx_path must be a non-empty string"}
    if not isinstance(part_name, str) or not part_name.strip():
        return {"error": "part_name must be a non-empty string"}
    normalized_part = part_name.strip().lstrip("/")
    if not normalized_part.startswith("word/media/"):
        return {
            "error": (
                "part_name must be a word/media/* package part -- got "
                f"{part_name!r}. This primitive refuses arbitrary-part "
                "removal by design (content parts like word/document.xml, "
                "or package infrastructure like [Content_Types].xml, can "
                "never be removed through it)."
            )
        }
    if allow_degraded_render and not (
        degraded_render_reason and str(degraded_render_reason).strip()
    ):
        return {
            "error": (
                "degraded_render_reason is required and must be non-empty "
                "when allow_degraded_render=True -- an audited degrade with "
                "no stated reason is not auditable and is refused"
            )
        }
    if not os.path.exists(docx_path):
        return {"error": f"no such file: {docx_path}"}

    with open(docx_path, "rb") as fh:
        raw = fh.read()
    try:
        entries = _docx_zip_entries(raw)
    except zipfile.BadZipFile as exc:
        return {"error": f"not a valid .docx (not a ZIP): {docx_path}: {exc}"}

    if "word/document.xml" not in entries:
        return {"error": f"not a valid .docx: missing word/document.xml: {docx_path}"}
    if normalized_part not in entries:
        return {"error": f"package part not found: {normalized_part!r} in {docx_path}"}

    rels_path = "word/_rels/document.xml.rels"
    rels_xml = entries.get(rels_path)
    try:
        rels_root = (
            ET.fromstring(rels_xml)
            if rels_xml
            else ET.Element(_q(_PACKAGE_REL_NS, "Relationships"))
        )
        document_xml = entries["word/document.xml"]
        ET.fromstring(document_xml)  # fail fast on malformed document.xml
    except ET.ParseError as exc:
        return {"error": f"malformed XML part in {docx_path}: {exc}"}

    referencing_rel_ids = _docx_relationships_targeting_part(rels_root, normalized_part)
    reference_count = sum(
        _docx_attribute_value_reference_count(document_xml, rid)
        for rid in referencing_rel_ids
    )

    if reference_count > 0:
        return {
            "error": (
                f"refusing to remove {normalized_part!r}: it is still "
                f"referenced {reference_count} time(s) in word/document.xml "
                f"via relationship id(s) {referencing_rel_ids!r} -- remove "
                "the referencing drawing(s)/content first, or target a "
                "genuinely unreferenced part"
            ),
            "status": "refused_still_referenced",
            "part_name": normalized_part,
            "reference_count": reference_count,
            "referencing_relationship_ids": referencing_rel_ids,
            "dry_run": dry_run,
        }

    content_types_xml = entries.get("[Content_Types].xml")
    remaining_entries = {name for name in entries if name != normalized_part}

    if dry_run:
        preview_root = (
            ET.fromstring(content_types_xml)
            if content_types_xml
            else ET.Element(_q(_CONTENT_TYPES_NS, "Types"))
        )
        ct_preview = _cleanup_content_types_after_removal(
            preview_root, normalized_part, remaining_entries
        )
        return {
            "status": "dry_run",
            "would_remove": {
                "part_name": normalized_part,
                "relationship_ids": referencing_rel_ids,
                "content_type_overrides_removed": ct_preview["removed_overrides"],
                "content_type_defaults_removed": ct_preview["removed_defaults"],
            },
            "part_name": normalized_part,
            "relationship_ids": referencing_rel_ids,
            "reference_count": 0,
            "docx_path": docx_path,
            "dry_run": True,
        }

    # --- Real removal ---
    for rel_id in referencing_rel_ids:
        for child in list(rels_root):
            if child.get("Id") == rel_id:
                rels_root.remove(child)
    new_rels_bytes = ET.tostring(rels_root, encoding="utf-8", xml_declaration=True)

    content_types_root = (
        ET.fromstring(content_types_xml)
        if content_types_xml
        else ET.Element(_q(_CONTENT_TYPES_NS, "Types"))
    )
    ct_result = _cleanup_content_types_after_removal(
        content_types_root, normalized_part, remaining_entries
    )
    new_ct_bytes = ET.tostring(content_types_root, encoding="utf-8", xml_declaration=True)

    updated_parts: dict[str, bytes] = {
        rels_path: new_rels_bytes,
        "[Content_Types].xml": new_ct_bytes,
    }

    with _docx_promotion_lock(docx_path):
        try:
            transaction = _save_docx_with_part_removed_stdlib(
                raw,
                remove_part_names=(normalized_part,),
                updated_parts=updated_parts,
                dest=docx_path,
            )
        except OSError as exc:
            return {"error": f"could not write {docx_path}: {exc}"}

        promoted_sha256 = transaction.get("promoted_sha256")

        verify_error = _verify_part_removal_write(
            docx_path,
            removed_part_name=normalized_part,
            removed_relationship_ids=referencing_rel_ids,
        )
        if verify_error is not None:
            safe_to_restore, restored, concurrent_write_detected = (
                _safe_restore_after_verification_failure(docx_path, promoted_sha256)
            )
            verify_error["file_restored"] = restored
            verify_error["concurrent_write_detected"] = concurrent_write_detected
            if not safe_to_restore:
                if concurrent_write_detected:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- AND a different writer's promotion has landed on "
                        "this file since ours, so this verification failure "
                        "could not be safely auto-corrected: restoring from our "
                        "own backup would destroy that writer's already-promoted "
                        f"work. {docx_path} was left untouched, exactly as that "
                        "other writer left it -- investigate manually."
                    )
                else:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- this write's own promotion fingerprint is "
                        "unavailable, so it could not be safely confirmed that "
                        "restoring from backup would not destroy a different "
                        f"writer's work; {docx_path} was left untouched rather "
                        "than risk it -- investigate manually."
                    )
            verify_error["part_name"] = normalized_part
            verify_error["docx_path"] = docx_path
            return verify_error

        render_error, render_info = _enforce_render_verification(
            docx_path,
            promoted_sha256=promoted_sha256,
            allow_degraded_render=allow_degraded_render,
            degraded_render_reason=degraded_render_reason,
        )
        if render_error is not None:
            render_error["part_name"] = normalized_part
            render_error["docx_path"] = docx_path
            return render_error

    _invalidate_sidecar_mtime(index_db_path)
    return {
        "status": "removed",
        "part_name": normalized_part,
        "relationship_ids_removed": referencing_rel_ids,
        "content_type_overrides_removed": ct_result["removed_overrides"],
        "content_type_defaults_removed": ct_result["removed_defaults"],
        "docx_path": docx_path,
        **render_info,
    }


def _verify_media_part_insertion_write(
    docx_path: str,
    *,
    image_para_id: str,
    relationship_id: str,
    media_part_name: str,
    expected_media_bytes: bytes,
    expected_width_emu: int,
    expected_height_emu: int,
) -> dict[str, Any] | None:
    """d371b00b post-write verification for :func:`insert_docx_media_part`.

    Re-reads ``docx_path`` FRESH FROM DISK and confirms, in order: the image
    paragraph is present and centered; its ``wp:extent`` frame matches the
    expected EMU width/height (frame-extent check); its ``a:blip`` references
    ``relationship_id``; that relationship resolves (in the freshly re-read
    rels part) to ``media_part_name``, which genuinely exists in the ZIP with
    the EXACT expected bytes; and finally the explicit BIJECTION check --
    ``relationship_id`` appears exactly once in the rels part, and
    ``media_part_name`` is not targeted by any OTHER relationship (via
    :func:`_docx_media_bijection_report`'s ``shared_media`` -- this brand-new
    part must be a clean 1:1 pairing, never reused). Also confirms
    [Content_Types].xml declares an applicable Default or Override for it.
    Returns ``None`` on success, an ``{"error": ...}`` dict on the first
    violation.
    """
    try:
        raw2, root2 = _load_docx_xml_stdlib(docx_path)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "error": (
                "post-write verification failed: could not re-read "
                f"{docx_path} after writing it: {exc}"
            )
        }

    body2 = root2.find(_q(_W, "body"))
    if body2 is None:
        return {
            "error": (
                f"post-write verification failed: re-read of {docx_path} "
                "has no <w:body> element"
            )
        }

    w14_para_id = _q(_W14, "paraId")
    image_para = next(
        (el for el in body2 if el.get(w14_para_id) == image_para_id), None
    )
    if image_para is None:
        return {
            "error": (
                f"post-write verification failed: image paragraph "
                f"{image_para_id!r} not found anywhere in {docx_path} after "
                "the write"
            )
        }
    if _paragraph_alignment(image_para) != "center":
        return {
            "error": (
                "post-write verification failed: image paragraph "
                f"{image_para_id!r} is not centered (w:jc=\"center\") after "
                "the write"
            )
        }

    extent = image_para.find(f".//{_q(_WP, 'extent')}")
    if extent is None:
        return {
            "error": (
                "post-write verification failed: image paragraph "
                f"{image_para_id!r} has no wp:extent frame element after "
                "the write"
            )
        }
    actual_cx, actual_cy = extent.get("cx"), extent.get("cy")
    if actual_cx != str(expected_width_emu) or actual_cy != str(expected_height_emu):
        return {
            "error": (
                "post-write verification failed: image frame extents "
                f"mismatch (expected cx={expected_width_emu} "
                f"cy={expected_height_emu}, got cx={actual_cx} cy={actual_cy})"
            )
        }

    blip = image_para.find(f".//{_q(_A, 'blip')}")
    embed_attr = _q(_IMAGE_REL_NS, "embed")
    actual_rid = blip.get(embed_attr) if blip is not None else None
    if actual_rid != relationship_id:
        return {
            "error": (
                f"post-write verification failed: image paragraph "
                f"{image_para_id!r} does not reference relationship "
                f"{relationship_id!r} (found {actual_rid!r})"
            )
        }

    with zipfile.ZipFile(io.BytesIO(raw2)) as zf2:
        names2 = set(zf2.namelist())
        rels_path = "word/_rels/document.xml.rels"
        if rels_path not in names2:
            return {
                "error": (
                    f"post-write verification failed: {rels_path} is "
                    "missing after the write"
                )
            }
        rels_root2 = ET.fromstring(zf2.read(rels_path))
        matching = [child for child in rels_root2 if child.get("Id") == relationship_id]
        if len(matching) != 1:
            return {
                "error": (
                    "post-write verification failed: relationship id "
                    f"{relationship_id!r} bijection violated -- expected "
                    f"exactly one matching <Relationship>, found "
                    f"{len(matching)}"
                )
            }
        resolved_target = _rel_target_part_name(matching[0].get("Target") or "", "word")
        if resolved_target != media_part_name:
            return {
                "error": (
                    f"post-write verification failed: relationship "
                    f"{relationship_id!r} targets {resolved_target!r}, "
                    f"expected {media_part_name!r}"
                )
            }
        if media_part_name not in names2:
            return {
                "error": (
                    f"post-write verification failed: media part "
                    f"{media_part_name!r} is missing from the package "
                    "after the write"
                )
            }
        actual_bytes = zf2.read(media_part_name)
        if actual_bytes != expected_media_bytes:
            return {
                "error": (
                    f"post-write verification failed: media part "
                    f"{media_part_name!r} bytes do not match what was "
                    "written"
                )
            }

        media_names = {name for name in names2 if name.startswith("word/media/")}
        bijection = _docx_media_bijection_report(media_names, rels_root2)
        if media_part_name in bijection["shared_media"]:
            return {
                "error": (
                    f"post-write verification failed: media part "
                    f"{media_part_name!r} bijection violated -- referenced "
                    "by multiple relationships "
                    f"{bijection['shared_media'][media_part_name]!r}"
                )
            }

        content_types_path = "[Content_Types].xml"
        if content_types_path not in names2:
            return {
                "error": (
                    f"post-write verification failed: {content_types_path} "
                    "is missing after the write"
                )
            }
        content_types_root2 = ET.fromstring(zf2.read(content_types_path))
        extension = os.path.splitext(media_part_name)[1].lstrip(".").lower()
        ct_default = _q(_CONTENT_TYPES_NS, "Default")
        ct_override = _q(_CONTENT_TYPES_NS, "Override")
        has_default = any(
            child.tag == ct_default and child.get("Extension", "").lower() == extension
            for child in content_types_root2
        )
        has_override = any(
            child.tag == ct_override and child.get("PartName") == f"/{media_part_name}"
            for child in content_types_root2
        )
        if not (has_default or has_override):
            return {
                "error": (
                    "post-write verification failed: [Content_Types].xml "
                    "declares no Default or Override content-type entry "
                    f"for {media_part_name!r} after the write"
                )
            }
    return None


def insert_docx_media_part(
    docx_path: str,
    image_path: str,
    anchor_para_id: str | None = None,
    position: str = "after",
    width_inches: float | None = None,
    height_inches: float | None = None,
    index_db_path: str | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
) -> dict[str, Any]:
    """d371b00b -- safe insertion of a brand-new image/media package member.

    Lower-level, caption-less sibling of :func:`insert_figure_block` (pair
    with :func:`insert_caption` / :func:`insert_figure_block` for a captioned
    figure -- same documented two-step composition :func:`insert_image`
    already offers). What this primitive adds beyond :func:`insert_image`:

    * COLLISION-FREE relationship id (:func:`_next_relationship_id`) and
      media part name (:func:`_next_media_name`) generation, re-verified
      (not just trusted) before use.
    * MATCHING content-type entries: reuses an existing ``Default`` for the
      image's extension when its declared ContentType already matches;
      registers a new ``Default`` when the extension is wholly new to the
      package; and falls back to a part-specific ``Override`` (never
      mutating a pre-existing, DIFFERENT ``Default`` out from under every
      other part relying on it) when the extension's Default disagrees with
      this image's own content type.
    * The SAME drawing/frame-extent construction :func:`insert_figure_block`
      uses (:func:`_build_image_drawing` / :func:`_image_size_emu`).
    * An explicit post-write relationship<->media BIJECTION check
      (:func:`_verify_media_part_insertion_write`) -- the new relationship id
      and the new media part must be a clean 1:1 pairing, not merely "both
      present somewhere" -- before the write is ever reported as successful.

    Routes through :func:`_save_docx_with_new_parts_stdlib` with
    ``protected_keys=("style_count",)`` (media/relationship counts are
    EXPECTED to grow by exactly one here -- this write's entire point --
    unlike that helper's other, non-media-touching callers), holds
    :func:`_docx_promotion_lock` across stage+promote -> verify ->
    conditional restore (5988a5bb discipline, identical to
    :func:`insert_figure_block`), and runs the SAME tri-state real-render
    canary after structural verification passes
    (:func:`_enforce_render_verification`) -- ``allow_degraded_render`` /
    ``degraded_render_reason`` are the identical audited opt-in.

    Anchor resolution (``anchor_para_id`` / ``position``), supported image
    formats, and dimension inference all match :func:`insert_image`.

    Returns ``{status, image_para_id, image_name, relationship_id,
    content_type_action, width_emu, height_emu, docx_path, render_status,
    render_verified, ...}``, or ``{"error": message}`` without mutating the
    document on validation failure, structural/bijection verification
    failure, or render-verification failure that could not be cleanly
    restored.
    """
    if not isinstance(docx_path, str) or not docx_path:
        return {"error": "docx_path must be a non-empty string"}
    if not isinstance(image_path, str) or not image_path:
        return {"error": "image_path must be a non-empty string"}
    if position not in ("before", "after"):
        return {"error": "position must be before or after"}
    if allow_degraded_render and not (
        degraded_render_reason and str(degraded_render_reason).strip()
    ):
        return {
            "error": (
                "degraded_render_reason is required and must be non-empty "
                "when allow_degraded_render=True -- an audited degrade with "
                "no stated reason is not auditable and is refused"
            )
        }
    suffix = os.path.splitext(image_path)[1].lower()
    image_type = _IMAGE_TYPES.get(suffix)
    if image_type is None:
        return {"error": f"unsupported image format: {suffix or 'missing extension'}"}
    if width_inches is not None and width_inches <= 0:
        return {"error": "width_inches must be greater than zero"}
    if height_inches is not None and height_inches <= 0:
        return {"error": "height_inches must be greater than zero"}
    try:
        with open(image_path, "rb") as handle:
            image_bytes = handle.read()
    except OSError as exc:
        return {"error": f"could not read image {image_path}: {exc}"}
    if not image_bytes:
        return {"error": f"image file is empty: {image_path}"}
    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
        width_emu, height_emu = _image_size_emu(
            image_bytes, suffix, width_inches, height_inches
        )
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    if body is None:
        return {"error": f"document has no body: {docx_path}"}
    children = list(body)
    if anchor_para_id is None:
        insert_at = next(
            (idx for idx, child in enumerate(children) if child.tag == _q(_W, "sectPr")),
            len(children),
        )
    else:
        located = _find_para_by_id(root, anchor_para_id)
        if located is None:
            return {"error": f"para_id {anchor_para_id!r} not found in {docx_path}"}
        _located_body, anchor, anchor_idx = located
        if _located_body is not body or children[anchor_idx] is not anchor:
            return {
                "error": (
                    "anchor_para_id must identify a direct body paragraph; "
                    "table-cell paragraphs cannot anchor image insertion"
                )
            }
        insert_at = anchor_idx + (1 if position == "after" else 0)

    entries = _docx_zip_entries(raw)
    rels_path = "word/_rels/document.xml.rels"
    rels_xml = entries.get(rels_path)
    rels_root = (
        ET.fromstring(rels_xml)
        if rels_xml
        else ET.Element(_q(_PACKAGE_REL_NS, "Relationships"))
    )

    # Collision-free relationship id -- re-asserted explicitly (never just
    # trusted) rather than only relying on _next_relationship_id's own
    # correctness, since a colliding id would silently corrupt the bijection
    # this primitive exists to guarantee.
    relationship_id = _next_relationship_id(rels_root)
    if any(child.get("Id") == relationship_id for child in rels_root):
        return {
            "error": (
                f"internal error: generated relationship id "
                f"{relationship_id!r} already exists in {rels_path}"
            )
        }

    # Collision-free media part name -- same explicit re-assertion.
    image_name = _next_media_name(entries, f".{image_type[0]}")
    if image_name in entries:
        return {
            "error": (
                f"internal error: generated media part name {image_name!r} "
                f"already exists in {docx_path}"
            )
        }

    taken = _existing_para_ids(root)
    image_para_id = _new_para_id(taken)
    doc_pr_id = len(root.findall(f".//{_q(_WP, 'docPr')}")) + 1

    paragraph = ET.Element(_q(_W, "p"))
    paragraph.set(_q(_W14, "paraId"), image_para_id)
    ppr = ET.SubElement(paragraph, _q(_W, "pPr"))
    ET.SubElement(ppr, _q(_W, "jc"), {_q(_W, "val"): "center"})
    run = ET.SubElement(paragraph, _q(_W, "r"))
    run.append(
        _build_image_drawing(relationship_id, width_emu, height_emu, doc_pr_id, image_name)
    )
    body.insert(insert_at, paragraph)

    ET.SubElement(
        rels_root, _q(_PACKAGE_REL_NS, "Relationship"),
        {
            "Id": relationship_id,
            "Type": _IMAGE_REL_TYPE,
            "Target": f"media/{os.path.basename(image_name)}",
        },
    )
    new_rels_bytes = ET.tostring(rels_root, encoding="utf-8", xml_declaration=True)

    content_types_xml = entries.get("[Content_Types].xml")
    content_types_root = (
        ET.fromstring(content_types_xml)
        if content_types_xml
        else ET.Element(_q(_CONTENT_TYPES_NS, "Types"))
    )
    extension = os.path.splitext(image_name)[1].lstrip(".")
    content_type = image_type[1]
    ct_default = _q(_CONTENT_TYPES_NS, "Default")
    ct_override = _q(_CONTENT_TYPES_NS, "Override")
    existing_default = next(
        (
            child for child in content_types_root
            if child.tag == ct_default
            and child.get("Extension", "").lower() == extension.lower()
        ),
        None,
    )
    if existing_default is None:
        ET.SubElement(
            content_types_root, ct_default,
            {"Extension": extension, "ContentType": content_type},
        )
        content_type_action = "default_added"
    elif existing_default.get("ContentType") == content_type:
        content_type_action = "default_reused"
    else:
        # The extension's shared Default disagrees with THIS part's required
        # content type -- add a part-specific Override instead of mutating a
        # Default entry every other part with this extension may rely on.
        ET.SubElement(
            content_types_root, ct_override,
            {"PartName": f"/{image_name}", "ContentType": content_type},
        )
        content_type_action = "override_added"
    new_ct_bytes = ET.tostring(content_types_root, encoding="utf-8", xml_declaration=True)

    new_document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        + ET.tostring(root, encoding="unicode")
    ).encode("utf-8")

    updated_parts: dict[str, bytes] = {
        rels_path: new_rels_bytes,
        "[Content_Types].xml": new_ct_bytes,
        "word/document.xml": new_document_xml,
        image_name: image_bytes,
    }

    with _docx_promotion_lock(docx_path):
        try:
            transaction = _save_docx_with_new_parts_stdlib(
                raw, updated_parts, docx_path,
                protected_keys=("style_count",),
            )
        except OSError as exc:
            return {"error": f"could not write {docx_path}: {exc}"}

        promoted_sha256 = transaction.get("promoted_sha256")

        verify_error = _verify_media_part_insertion_write(
            docx_path,
            image_para_id=image_para_id,
            relationship_id=relationship_id,
            media_part_name=image_name,
            expected_media_bytes=image_bytes,
            expected_width_emu=width_emu,
            expected_height_emu=height_emu,
        )
        if verify_error is not None:
            safe_to_restore, restored, concurrent_write_detected = (
                _safe_restore_after_verification_failure(docx_path, promoted_sha256)
            )
            verify_error["file_restored"] = restored
            verify_error["concurrent_write_detected"] = concurrent_write_detected
            if not safe_to_restore:
                if concurrent_write_detected:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- AND a different writer's promotion has landed on "
                        "this file since ours, so this verification failure "
                        "could not be safely auto-corrected: restoring from our "
                        "own backup would destroy that writer's already-promoted "
                        f"work. {docx_path} was left untouched, exactly as that "
                        "other writer left it -- investigate manually."
                    )
                else:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- this write's own promotion fingerprint is "
                        "unavailable, so it could not be safely confirmed that "
                        "restoring from backup would not destroy a different "
                        f"writer's work; {docx_path} was left untouched rather "
                        "than risk it -- investigate manually."
                    )
            verify_error["image_para_id"] = image_para_id
            verify_error["docx_path"] = docx_path
            return verify_error

        render_error, render_info = _enforce_render_verification(
            docx_path,
            promoted_sha256=promoted_sha256,
            allow_degraded_render=allow_degraded_render,
            degraded_render_reason=degraded_render_reason,
        )
        if render_error is not None:
            render_error["image_para_id"] = image_para_id
            render_error["docx_path"] = docx_path
            return render_error

    _invalidate_sidecar_mtime(index_db_path)
    return {
        "status": "inserted",
        "image_para_id": image_para_id,
        "image_name": image_name,
        "relationship_id": relationship_id,
        "content_type_action": content_type_action,
        "width_emu": width_emu,
        "height_emu": height_emu,
        "docx_path": docx_path,
        **render_info,
    }


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

def _verify_caption_write(
    docx_path: str,
    *,
    ref_bookmark: str,
    kind: str,
    expected_seq_number: int,
    expected_label_text: str,
) -> dict[str, Any] | None:
    """9d749639 follow-up (ddd79188) — post-write verification for
    :func:`insert_caption`, mirroring :func:`_verify_figure_block_write`'s
    "brand new content, no prior on-disk baseline to diff against" style
    (:func:`_verify_docx_write`'s count/hash comparison has nothing to diff
    a freshly inserted caption against).

    Re-reads ``docx_path`` FRESH FROM DISK and locates the caption paragraph
    by its unique ``ref_bookmark`` (1c59cb90's ``_Ref<digits>`` bookmark,
    document-unique and assigned before the write) rather than by position —
    positions shift, bookmarks don't. Confirms that paragraph still carries a
    ``SEQ <kind>`` field with the expected cached number and the expected
    label text. Returns ``None`` when every check passes, or an
    ``{"error": ...}`` dict on the first mismatch.
    """
    try:
        _raw2, root2 = _load_docx_xml_stdlib(docx_path)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "error": (
                "post-write verification failed: could not re-read "
                f"{docx_path} after writing it: {exc}"
            )
        }

    body2 = root2.find(_q(_W, "body"))
    if body2 is None:
        return {
            "error": (
                "post-write verification failed: re-read of "
                f"{docx_path} has no <w:body> element"
            )
        }

    w_bookmark_start = _q(_W, "bookmarkStart")
    w_p = _q(_W, "p")
    caption_para = next(
        (
            child
            for child in body2
            if child.tag == w_p
            and any(
                bm.get(_q(_W, "name")) == ref_bookmark
                for bm in child.findall(w_bookmark_start)
            )
        ),
        None,
    )
    if caption_para is None:
        return {
            "error": (
                "post-write verification failed: no caption paragraph "
                f"carrying bookmark {ref_bookmark!r} was found in "
                f"{docx_path} after the write"
            )
        }

    fld = caption_para.find(_q(_W, "fldSimple"))
    instr = fld.get(_q(_W, "instr")) if fld is not None else None
    if fld is None or f"SEQ {kind}" not in (instr or ""):
        return {
            "error": (
                "post-write verification failed: caption paragraph "
                f"(bookmark {ref_bookmark!r}) does not contain a SEQ {kind} "
                "field after the write"
            )
        }
    seq_text_el = fld.find(f".//{_q(_W, 't')}")
    seq_text = seq_text_el.text if seq_text_el is not None else None
    if seq_text != str(expected_seq_number):
        return {
            "error": (
                "post-write verification failed: caption SEQ number "
                f"mismatch (expected {expected_seq_number!r}, got "
                f"{seq_text!r})"
            )
        }

    caption_text = "".join(t.text or "" for t in caption_para.iter(_q(_W, "t")))
    if expected_label_text not in caption_text:
        return {
            "error": (
                "post-write verification failed: caption label text "
                f"mismatch (expected to contain {expected_label_text!r}, "
                f"got {caption_text!r})"
            )
        }
    return None


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
) -> dict[str, Any]:
    """9d749639 — Insert a real Word Caption paragraph into a .docx file.

    Writes a new ``<w:p>`` with ``w:pStyle="Caption"`` and a ``SEQ Figure``
    or ``SEQ Table`` field directly into ``word/document.xml`` inside the .docx
    ZIP, then re-packs the ZIP preserving all other members.

    The SEQ number is auto-incremented: it equals the count of existing SEQ
    captions of the same kind in the document plus one.  Word will recompute
    the final numbering on the next field refresh (F9).

    4efc63fd — ``style_policy["caption_centered"]`` (default ``False``, via
    :func:`resolve_style_policy`) controls whether the new caption paragraph
    gets ``w:jc w:val="center"``.

    ddd79188 — AFTER the write is staged, structurally verified (see
    :func:`_verify_caption_write`), and promoted, a real Word/COM (or
    LibreOffice) render-capability check also runs against the just-written
    file (:func:`_enforce_render_verification`), mirroring the same gate
    :func:`insert_figure_block` already enforces: structural XML re-parse
    alone can never prove the document actually opens/renders in Word.
    ``"rendered"`` continues normally with render evidence attached to the
    success payload. ``"failed"`` (a render backend WAS available but errored
    on this document) restores ``docx_path`` from the pre-write backup and
    returns an error — exactly like a structural verification failure.
    ``"unavailable-with-reason"`` (no render backend in this environment)
    ALSO fails closed by default — never reported as verified — unless the
    caller explicitly passes ``allow_degraded_render=True`` with a non-empty
    ``degraded_render_reason``, an audited opt-in that keeps the write but
    stamps ``render_verified=False`` / ``render_degraded=True`` on the
    payload rather than silently treating "could not check" as "passed".

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
        style_policy:    Optional overrides merged via
                         :func:`resolve_style_policy`.
        allow_degraded_render: ddd79188 — explicit, audited opt-in to accept
                         this write when no render backend is available in
                         this environment (render status
                         "unavailable-with-reason"). Requires
                         degraded_render_reason. Never bypasses a real render
                         "failed" status.
        degraded_render_reason: Required, non-empty when
                         allow_degraded_render is True; carried onto the
                         result as an audit trail (this stdlib-only, DB-free
                         extension does not persist it itself — a caller with
                         DB access, e.g. Meridian core, is responsible for
                         logging/pinning it).

    Returns:
        ``{status, kind, seq_number, label_text, section_heading, ref_bookmark,
        docx_path, render_status, render_verified, render_backend,
        render_detail}`` or ``{"error": <message>}`` on failure (file NOT
        left mutated on validation failure; restored from backup on a
        structural- or render-verification failure).  ``ref_bookmark``
        (1c59cb90) is the ``_Ref<digits>`` bookmark name wrapping the
        caption's "<Kind> <N>" text — pass it as ``bookmark_name`` to
        :func:`insert_cross_reference` to insert a live "Figure N" prose
        reference elsewhere that survives reordering.
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
    if allow_degraded_render and not (
        degraded_render_reason and str(degraded_render_reason).strip()
    ):
        return {
            "error": (
                "degraded_render_reason is required and must be non-empty "
                "when allow_degraded_render=True -- an audited degrade with "
                "no stated reason is not auditable and is refused"
            )
        }

    try:
        policy = resolve_style_policy(style_policy)
    except ValueError as exc:
        return {"error": str(exc)}

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
    label_text_clean = label_text.strip()

    caption_p = _build_caption_paragraph(
        kind=kind,
        label_text=label_text_clean,
        seq_cached=str(seq_number),
        ref_bookmark=ref_bookmark,
        centered=policy["caption_centered"],
    )

    insert_at = child_idx if position == "before" else child_idx + 1
    body.insert(insert_at, caption_p)

    # ddd79188 -- hold docx_path's promotion lock across stage+promote
    # (_save_docx_xml_stdlib, which reentrantly acquires it internally)
    # THROUGH the post-write structural verify, any conditional restore, and
    # the real render-capability gate below -- closing the same-process
    # window between promotion and verify/restore entirely (see
    # _docx_promotion_lock's module-level comment).
    with _docx_promotion_lock(docx_path):
        try:
            transaction = _save_docx_xml_stdlib(raw, root, docx_path)
        except OSError as exc:
            return {"error": f"could not write {docx_path}: {exc}"}

        promoted_sha256 = transaction.get("promoted_sha256") if transaction else None

        verify_error = _verify_caption_write(
            docx_path,
            ref_bookmark=ref_bookmark,
            kind=kind,
            expected_seq_number=seq_number,
            expected_label_text=label_text_clean,
        )
        if verify_error is not None:
            # 5988a5bb -- do NOT blindly restore: a different (concurrent)
            # writer may have already promoted something newer to docx_path
            # since our own promotion, in which case this verification
            # "failure" is a false positive and restoring from our own
            # backup would destroy that writer's completed, already-
            # promoted work.
            safe_to_restore, restored, concurrent_write_detected = (
                _safe_restore_after_verification_failure(docx_path, promoted_sha256)
            )
            verify_error["file_restored"] = restored
            verify_error["concurrent_write_detected"] = concurrent_write_detected
            if not safe_to_restore:
                if concurrent_write_detected:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- AND a different writer's promotion has landed on "
                        "this file since ours, so this verification failure "
                        "could not be safely auto-corrected: restoring from our "
                        "own backup would destroy that writer's already-promoted "
                        f"work. {docx_path} was left untouched, exactly as that "
                        "other writer left it -- investigate manually."
                    )
                else:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- this write's own promotion fingerprint is "
                        "unavailable, so it could not be safely confirmed that "
                        "restoring from backup would not destroy a different "
                        f"writer's work; {docx_path} was left untouched rather "
                        "than risk it -- investigate manually."
                    )
            verify_error["kind"] = kind
            verify_error["docx_path"] = docx_path
            return verify_error

        # ddd79188 -- structural verification alone (above) can never prove
        # the document actually renders in Word; run the real render-
        # capability gate now, still inside the promotion lock so a
        # fail-closed restore has the same CAS safety a structural failure
        # gets. Must run AFTER structural verification, not instead of it.
        render_error, render_info = _enforce_render_verification(
            docx_path,
            promoted_sha256=promoted_sha256,
            allow_degraded_render=allow_degraded_render,
            degraded_render_reason=degraded_render_reason,
        )
        if render_error is not None:
            render_error["kind"] = kind
            render_error["docx_path"] = docx_path
            return render_error

    _invalidate_sidecar_mtime(index_db_path)

    if index_db_path and os.path.exists(index_db_path):
        _upsert_sidecar_caption(
            index_db_path=index_db_path,
            kind=kind,
            para_id=None,  # newly inserted para has no w14:paraId yet
            seq_number=str(seq_number),
            caption_text=f"{kind} {seq_number}. {label_text_clean}",
            section_heading=section_heading,
            ref_bookmark=ref_bookmark,
        )

    return {
        "status": "inserted",
        "kind": kind,
        "seq_number": seq_number,
        "label_text": label_text_clean,
        "section_heading": section_heading,
        "ref_bookmark": ref_bookmark,
        "docx_path": docx_path,
        **render_info,
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
# Configurable document style policy (4efc63fd)
# ---------------------------------------------------------------------------
#
# A single dict-shaped "style policy" that write-back functions consult
# instead of hardcoding style choices (caption centering, equation body
# indentation/alignment, internal-note style name + highlight color), and
# that audit_equation_style below uses as its "expected" baseline. Plain
# dicts -- not a dataclass -- to match this module's existing convention of
# returning/consuming JSON-shaped dicts everywhere (MCP tool boundary).
#
# _style_policy_defaults() is a function (not a module-level dict literal)
# specifically so it can reference _INTERNAL_NOTE_STYLE_DEFAULT /
# _INTERNAL_NOTE_HIGHLIGHT_COLOR, which are defined later in this file --
# names inside a function body resolve at CALL time, long after the whole
# module has finished importing, so the forward reference is safe.

_VALID_EQUATION_ALIGNMENTS = {"left", "center", "right", "both"}

# The fixed set of values OOXML's <w:highlight w:val="..."/> accepts.
_VALID_HIGHLIGHT_COLORS = {
    "black", "blue", "cyan", "darkBlue", "darkCyan", "darkGray", "darkGreen",
    "darkMagenta", "darkRed", "darkYellow", "green", "lightGray", "magenta",
    "none", "red", "white", "yellow",
}


def _style_policy_defaults() -> dict[str, Any]:
    """Built-in defaults -- reproduce today's pre-4efc63fd behavior except
    where called out below.

    ``equation_alignment`` defaults to ``"center"`` (the conventional
    display-equation layout, matching :func:`insert_image`'s already-centered
    figures) rather than "leave unset" -- this is a deliberate, documented
    behavior addition for newly inserted display equations, not a bug: no
    existing test asserts an inserted equation paragraph has no ``pPr``, and
    keeping the audit's "expected" alignment and the writer's actual output
    in sync (one policy, two consumers) is the whole point of this feature.
    """
    return {
        "caption_centered": False,
        "body_indent_twips": 0,
        "equation_alignment": "center",
        "equation_punctuation_required": True,
        "equation_punctuation_chars": ".,;:",
        "note_style": _INTERNAL_NOTE_STYLE_DEFAULT,
        "note_highlight_color": _INTERNAL_NOTE_HIGHLIGHT_COLOR,
    }


def resolve_style_policy(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """4efc63fd -- merge caller-supplied overrides onto the default document
    style policy, validating every key/value so a malformed policy raises
    ``ValueError`` immediately (callers turn that into ``{"error": ...}``
    *before* touching any file) instead of producing malformed OOXML or
    silently doing nothing.

    Recognised keys (all optional -- any subset may be overridden):

      caption_centered (bool):     whether :func:`insert_caption` adds
                                    ``w:jc w:val="center"`` to new captions.
      body_indent_twips (int>=0):  left-indent (in twips, 1/20 pt) applied to
                                    a newly inserted DISPLAY equation
                                    paragraph's ``pPr`` by
                                    :func:`insert_equation_local`.
      equation_alignment (str):    one of "left"/"center"/"right"/"both" --
                                    both the alignment :func:`insert_equation_local`
                                    writes into new display-equation paragraphs
                                    AND the alignment :func:`audit_equation_style`
                                    treats as "correct".
      equation_punctuation_required (bool): whether
                                    :func:`audit_equation_style` checks
                                    trailing punctuation at all.
      equation_punctuation_chars (str): the accepted trailing-punctuation
                                    characters (checked by
                                    :func:`audit_equation_style`).
      note_style (str):            OOXML paragraph style name
                                    :func:`insert_highlighted_note` (inline
                                    mode) writes for new notes.
      note_highlight_color (str):  ``<w:highlight>`` value (must be a valid
                                    OOXML highlight color) for new notes.

    Raises:
      ValueError: an unknown key, or a value of the wrong type/out of range.
    """
    defaults = _style_policy_defaults()
    if not overrides:
        return defaults

    unknown = sorted(set(overrides) - set(defaults))
    if unknown:
        raise ValueError(f"unknown style policy key(s): {unknown}")

    policy = dict(defaults)
    policy.update(overrides)

    if not isinstance(policy["caption_centered"], bool):
        raise ValueError("style policy 'caption_centered' must be a bool")

    indent = policy["body_indent_twips"]
    if not isinstance(indent, int) or isinstance(indent, bool) or indent < 0:
        raise ValueError("style policy 'body_indent_twips' must be a non-negative int")

    if policy["equation_alignment"] not in _VALID_EQUATION_ALIGNMENTS:
        raise ValueError(
            "style policy 'equation_alignment' must be one of "
            f"{sorted(_VALID_EQUATION_ALIGNMENTS)}"
        )

    if not isinstance(policy["equation_punctuation_required"], bool):
        raise ValueError("style policy 'equation_punctuation_required' must be a bool")

    punct_chars = policy["equation_punctuation_chars"]
    if not isinstance(punct_chars, str) or not punct_chars:
        raise ValueError("style policy 'equation_punctuation_chars' must be a non-empty string")

    note_style = policy["note_style"]
    if not isinstance(note_style, str) or not note_style.strip():
        raise ValueError("style policy 'note_style' must be a non-empty string")

    if policy["note_highlight_color"] not in _VALID_HIGHLIGHT_COLORS:
        raise ValueError(
            "style policy 'note_highlight_color' must be one of "
            f"{sorted(_VALID_HIGHLIGHT_COLORS)}"
        )

    return policy


def _paragraph_alignment(para_elem: ET.Element) -> str | None:
    """Return the explicit ``w:jc`` value on ``para_elem``'s ``pPr``, or
    ``None`` when no alignment is explicitly set (Word's own default renders
    that as left-aligned)."""
    pPr = para_elem.find(_q(_W, "pPr"))
    if pPr is None:
        return None
    jc = pPr.find(_q(_W, "jc"))
    if jc is None:
        return None
    return jc.get(_q(_W, "val"))


def _trailing_text_after_omath(para_elem: ET.Element, omath_el: ET.Element) -> str:
    """Concatenate the text of every element following ``omath_el`` within its
    immediate parent inside ``para_elem``.

    Mirrors :func:`append_text_run_after_math`'s insertion point exactly --
    this is how :func:`audit_equation_style` reads back whatever a prior
    ``append_text_run_after_math`` call wrote (or detects that nothing was
    ever appended).
    """
    parent = next(
        (candidate for candidate in para_elem.iter() if omath_el in list(candidate)),
        None,
    )
    if parent is None:
        return ""
    siblings = list(parent)
    idx = siblings.index(omath_el)
    w_t = _q(_W, "t")
    return "".join(
        "".join(t.text or "" for t in sib.iter(w_t)) for sib in siblings[idx + 1:]
    )


_EQ_LEADING_INT_RE = re.compile(r"^\(\s*(\d+)")


def _leading_equation_number(number_text: str | None) -> int | None:
    """Extract the leading integer from an equation number like ``"(2a)"`` ->
    ``2``, or ``None`` for a non-numeric label like ``"(A.1)"``/``"(eq3)"``
    that has no well-defined "next integer" for gap detection."""
    if not number_text:
        return None
    m = _EQ_LEADING_INT_RE.match(number_text)
    return int(m.group(1)) if m else None


def audit_equation_style(
    docx_path: str,
    style_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """4efc63fd -- audit every equation in a .docx and report STRUCTURED
    findings (never free-text) covering alignment, trailing punctuation, and
    numbering consistency, so a caller can programmatically triage a document
    before submission instead of eyeballing it.

    Three finding categories:

    1. ``misaligned_equation`` -- scoped to STANDALONE equations that occupy
       their own paragraph (a "display" equation: the paragraph's only
       content besides ``pPr`` is the ``<m:oMath>``/``<m:oMathPara>`` --
       exactly what :func:`insert_equation_local`'s ``before``/``after``
       positions produce). Its paragraph-level ``w:jc`` (missing == "left")
       is compared against ``style_policy["equation_alignment"]``. Inline
       equations mixed into running prose, and table-numbered equations
       (whose 2-column layout has its own alignment conventions), are
       intentionally excluded -- neither has one well-defined "expected"
       paragraph alignment.

    2. ``missing_trailing_punctuation`` / ``incorrect_trailing_punctuation``
       -- for the same display-equation paragraphs (skipped entirely when
       ``style_policy["equation_punctuation_required"]`` is False), the text
       of any run(s) immediately following the ``<m:oMath>`` -- the exact
       spot :func:`append_text_run_after_math` writes to -- is checked
       against ``style_policy["equation_punctuation_chars"]``. No trailing
       text at all -> "missing"; trailing text whose last non-whitespace
       character isn't an accepted character -> "incorrect".

    3. ``duplicate_equation_number`` / ``equation_number_gap`` -- across every
       ``table-numbered`` equation (the ``"(1)"``/``"(2a)"`` pattern
       :func:`parse_docx_equations_local` already detects), numbers are
       compared whitespace-normalized for exact duplicates, and each number's
       LEADING integer (``"2a"`` -> ``2``) is checked for a contiguous
       1..max sequence. Non-numeric labels (``"(A.1)"``, ``"(eq3)"``) still
       participate in duplicate detection but are excluded from gap
       detection (no well-defined "next integer").

    Args:
      docx_path:     Absolute path to the .docx file. Read-only -- this
                     function never mutates the file.
      style_policy:  Optional overrides merged onto the default style policy
                     via :func:`resolve_style_policy`.

    Returns:
      ``{docx_path, equation_count, findings, finding_count,
      findings_by_type, policy}`` or ``{"error": <message>}`` when the file
      cannot be read or the style policy is invalid.
    """
    try:
        policy = resolve_style_policy(style_policy)
    except ValueError as exc:
        return {"error": str(exc)}

    try:
        _raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    equations = parse_docx_equations_local(docx_path)
    findings: list[dict[str, Any]] = []

    m_omath_tag = _qm("oMath")
    m_omath_para_tag = _qm("oMathPara")

    for eq in equations:
        if eq["pattern"] != "standalone":
            continue
        located = _find_para_by_id(root, eq["para_id"])
        if located is None:
            continue  # unresolvable id (e.g. a table-embedded standalone) -- skip
        _body, para_elem, _idx = located

        # Resolve the paragraph's direct-child oMath(s) (unwrapping a single
        # oMathPara if that's how it's wrapped). A paragraph containing more
        # than one equation is ambiguous -- which one does trailing text
        # belong to? -- so it's skipped entirely, mirroring
        # append_text_run_after_math's own "math_index required" guard
        # against the same ambiguity rather than guessing.
        direct_omaths = para_elem.findall(m_omath_tag)
        top_level_el = None
        if direct_omaths:
            top_level_el = direct_omaths[0] if len(direct_omaths) == 1 else None
        else:
            omath_para_el = para_elem.find(m_omath_para_tag)
            if omath_para_el is not None:
                direct_omaths = omath_para_el.findall(m_omath_tag)
                if len(direct_omaths) == 1:
                    top_level_el = omath_para_el
        if len(direct_omaths) != 1 or top_level_el is None:
            continue
        omath_el = direct_omaths[0]

        # "Display equation" = nothing but pPr precedes the equation in the
        # paragraph. Content AFTER the equation -- e.g. a trailing
        # punctuation run written by append_text_run_after_math -- is
        # expected and does NOT disqualify it (that's exactly what the
        # punctuation check below reads back). Content BEFORE the equation
        # (prose mixed with the equation, as in an inline
        # "Einstein: E=mc^2" sentence) DOES disqualify it -- there is no
        # single sensible alignment/punctuation expectation for a sentence
        # that merely contains an equation.
        siblings = list(para_elem)
        top_idx = siblings.index(top_level_el)
        preceding = [c for c in siblings[:top_idx] if c.tag != _q(_W, "pPr")]
        if preceding:
            continue  # inline equation mixed with prose -- no alignment/punctuation check

        actual_alignment = _paragraph_alignment(para_elem) or "left"
        expected_alignment = policy["equation_alignment"]
        if actual_alignment != expected_alignment:
            findings.append({
                "type": "misaligned_equation",
                "para_id": eq["para_id"],
                "ordinal": eq["ordinal"],
                "expected_alignment": expected_alignment,
                "actual_alignment": actual_alignment,
            })

        if policy["equation_punctuation_required"]:
            trailing = _trailing_text_after_omath(para_elem, omath_el)
            stripped = trailing.rstrip()
            if not stripped:
                findings.append({
                    "type": "missing_trailing_punctuation",
                    "para_id": eq["para_id"],
                    "ordinal": eq["ordinal"],
                    "expected_punctuation_chars": policy["equation_punctuation_chars"],
                })
            elif stripped[-1] not in policy["equation_punctuation_chars"]:
                findings.append({
                    "type": "incorrect_trailing_punctuation",
                    "para_id": eq["para_id"],
                    "ordinal": eq["ordinal"],
                    "actual_trailing_text": trailing,
                    "actual_char": stripped[-1],
                    "expected_punctuation_chars": policy["equation_punctuation_chars"],
                })

    numbered = [eq for eq in equations if eq["pattern"] == "table-numbered" and eq["number"]]

    grouped_by_norm: dict[str, list[dict[str, Any]]] = {}
    for eq in numbered:
        norm = re.sub(r"\s+", "", eq["number"])
        grouped_by_norm.setdefault(norm, []).append(eq)
    for group in grouped_by_norm.values():
        if len(group) > 1:
            findings.append({
                "type": "duplicate_equation_number",
                "number": group[0]["number"],
                "para_ids": [g["para_id"] for g in group],
                "ordinals": [g["ordinal"] for g in group],
            })

    leading_ints = sorted({
        v for v in (_leading_equation_number(eq["number"]) for eq in numbered)
        if v is not None
    })
    if leading_ints:
        expected_range = set(range(1, leading_ints[-1] + 1))
        for missing in sorted(expected_range - set(leading_ints)):
            findings.append({"type": "equation_number_gap", "missing_number": missing})

    findings_by_type: dict[str, int] = {}
    for finding in findings:
        findings_by_type[finding["type"]] = findings_by_type.get(finding["type"], 0) + 1

    return {
        "docx_path": docx_path,
        "equation_count": len(equations),
        "findings": findings,
        "finding_count": len(findings),
        "findings_by_type": findings_by_type,
        "policy": policy,
    }


# ---------------------------------------------------------------------------
# WRITE: insert / edit / remove equation
# ---------------------------------------------------------------------------

def _build_omath_paragraph(
    omml_raw: str,
    alignment: str | None = None,
    indent_twips: int = 0,
) -> ET.Element:
    """Wrap a raw OMML string in a new <w:p> for display-mode insertion.

    Produces::

        <w:p>
          <w:pPr>
            <w:jc w:val="..."/>        <!-- only when alignment is given -->
            <w:ind w:left="..."/>      <!-- only when indent_twips > 0 -->
          </w:pPr>
          <m:oMath>...</m:oMath>
        </w:p>

    ``alignment``/``indent_twips`` (4efc63fd) come from a resolved style
    policy (see :func:`resolve_style_policy`) so the paragraph's ``pPr`` is
    omitted entirely when neither is set -- matching this function's
    original (pre-4efc63fd) output exactly.

    The oMath element is parsed from ``omml_raw`` and appended as a child.
    """
    p = ET.Element(_q(_W, "p"))
    if alignment or indent_twips:
        pPr = ET.SubElement(p, _q(_W, "pPr"))
        if alignment:
            ET.SubElement(pPr, _q(_W, "jc"), {_q(_W, "val"): alignment})
        if indent_twips:
            ET.SubElement(pPr, _q(_W, "ind"), {_q(_W, "left"): str(indent_twips)})
    omath_el = ET.fromstring(omml_raw)
    p.append(omath_el)
    return p


def _verify_equation_write(
    docx_path: str,
    *,
    position: str,
    anchor_para_id: str,
    insert_at: int | None,
    expected_flat_text: str,
) -> dict[str, Any] | None:
    """a80af3a0 follow-up (ddd79188) — post-write verification for
    :func:`insert_equation_local`, mirroring :func:`_verify_figure_block_write`'s
    "brand new content, no prior on-disk baseline to diff against" style.

    Re-reads ``docx_path`` FRESH FROM DISK. For ``position="append"``, the
    anchor paragraph itself was mutated (nothing new was inserted at the body
    level) so it is re-resolved via :func:`_find_para_by_id` using the SAME
    ``anchor_para_id`` the caller was given. For ``position="before"``/
    ``"after"``, a brand-new paragraph carries no bookmark or native id of
    its own (unlike :func:`insert_caption`/:func:`insert_highlighted_note`'s
    paragraphs) — it is located by the exact body index (``insert_at``) it
    was spliced into, still inside the SAME promotion-lock critical section
    as our own promotion, mirroring :func:`_verify_docx_write`'s
    ``expected_range`` positional check.

    Either way, confirms the located paragraph contains an ``<m:oMath>``
    whose flattened ``<m:t>`` text content (:func:`_omml_flatten_text_local`)
    matches ``expected_flat_text``. Returns ``None`` when every check
    passes, or an ``{"error": ...}`` dict on the first mismatch.
    """
    try:
        _raw2, root2 = _load_docx_xml_stdlib(docx_path)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "error": (
                "post-write verification failed: could not re-read "
                f"{docx_path} after writing it: {exc}"
            )
        }

    body2 = root2.find(_q(_W, "body"))
    if body2 is None:
        return {
            "error": (
                "post-write verification failed: re-read of "
                f"{docx_path} has no <w:body> element"
            )
        }

    if position == "append":
        located = _find_para_by_id(root2, anchor_para_id)
        if located is None:
            return {
                "error": (
                    "post-write verification failed: anchor paragraph "
                    f"{anchor_para_id!r} not found in {docx_path} after "
                    "the write"
                )
            }
        _abody, para_elem, _acidx = located
        where = f"anchor paragraph {anchor_para_id!r}"
    else:
        body_list = list(body2)
        if insert_at is None or insert_at >= len(body_list):
            return {
                "error": (
                    "post-write verification failed: expected a new "
                    f"equation paragraph at body index {insert_at!r} in "
                    f"{docx_path} after the write, but the document only "
                    f"has {len(body_list)} top-level element(s)"
                )
            }
        para_elem = body_list[insert_at]
        if para_elem.tag != _q(_W, "p"):
            return {
                "error": (
                    "post-write verification failed: body index "
                    f"{insert_at!r} is not a paragraph in {docx_path} after "
                    "the write"
                )
            }
        where = f"new equation paragraph at body index {insert_at!r}"

    omath_els = para_elem.findall(f".//{_qm('oMath')}")
    if not omath_els:
        return {
            "error": (
                f"post-write verification failed: {where} does not contain "
                f"an <m:oMath> element in {docx_path} after the write"
            )
        }
    actual_flat_text = "".join(t.text or "" for t in omath_els[-1].iter(_qm("t")))
    if actual_flat_text != expected_flat_text:
        return {
            "error": (
                f"post-write verification failed: equation content "
                f"mismatch in {where} (expected flattened text "
                f"{expected_flat_text!r}, got {actual_flat_text!r})"
            )
        }
    return None


def insert_equation_local(
    docx_path: str,
    anchor_para_id: str,
    payload: str,
    position: str = "after",
    index_db_path: str | None = None,
    style_policy: dict[str, Any] | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
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

    4efc63fd — ``style_policy`` (resolved via :func:`resolve_style_policy`)
    supplies the new display paragraph's alignment (``equation_alignment``,
    default ``"center"``) and left indentation (``body_indent_twips``,
    default 0 / no indent). Not consulted for ``position="append"`` (inline
    equations have no paragraph of their own to style).

    ddd79188 — AFTER the write is staged, structurally verified (see
    :func:`_verify_equation_write`), and promoted, a real Word/COM (or
    LibreOffice) render-capability check also runs against the just-written
    file (:func:`_enforce_render_verification`), mirroring the same gate
    :func:`insert_figure_block` already enforces: structural XML re-parse
    alone can never prove the document actually opens/renders in Word.
    ``"rendered"`` continues normally with render evidence attached to the
    success payload. ``"failed"`` restores ``docx_path`` from the pre-write
    backup and returns an error. ``"unavailable-with-reason"`` (no render
    backend in this environment) ALSO fails closed by default — never
    reported as verified — unless the caller explicitly passes
    ``allow_degraded_render=True`` with a non-empty ``degraded_render_reason``.

    Args:
        docx_path:       Absolute path to the .docx file (mutated in place).
        anchor_para_id:  ``w14:paraId`` or ``p{N}`` / ``tbl{N}`` of the
                         paragraph to anchor on.
        payload:         Raw OMML XML string (``<m:oMath>...</m:oMath>``) or a
                         LaTeX expression (e.g. ``r"\\frac{a}{b}"``).
        position:        ``"before"``, ``"after"``, or ``"append"`` (default
                         ``"after"``).
        index_db_path:   If supplied, sidecar is invalidated after write.
        style_policy:    Optional overrides merged via
                         :func:`resolve_style_policy`; see that function's
                         docstring for keys.
        allow_degraded_render: ddd79188 — explicit, audited opt-in to accept
                         this write when no render backend is available in
                         this environment. Requires degraded_render_reason.
        degraded_render_reason: Required, non-empty when
                         allow_degraded_render is True; carried onto the
                         result as an audit trail.

    Returns:
        ``{status, position, para_id, omml, docx_path, render_status,
        render_verified, render_backend, render_detail}``
        or ``{"error": <message>}`` on failure (file NOT left mutated on
        validation failure; restored from backup on a structural- or
        render-verification failure).
    """
    if position not in ("before", "after", "append"):
        return {"error": f"position must be 'before', 'after', or 'append', got {position!r}"}
    if not payload or not str(payload).strip():
        return {"error": "payload must be a non-empty string (OMML XML or LaTeX)"}
    if allow_degraded_render and not (
        degraded_render_reason and str(degraded_render_reason).strip()
    ):
        return {
            "error": (
                "degraded_render_reason is required and must be non-empty "
                "when allow_degraded_render=True -- an audited degrade with "
                "no stated reason is not auditable and is refused"
            )
        }

    try:
        policy = resolve_style_policy(style_policy)
    except ValueError as exc:
        return {"error": str(exc)}

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

    insert_at: int | None = None
    if position == "append":
        # Inline: append <m:oMath> directly to the anchor paragraph.
        omath_el = ET.fromstring(omml)
        anchor_elem.append(omath_el)
    else:
        # Display: insert a new <w:p> wrapping the equation.
        new_p = _build_omath_paragraph(
            omml,
            alignment=policy["equation_alignment"],
            indent_twips=policy["body_indent_twips"],
        )
        insert_at = child_idx if position == "before" else child_idx + 1
        body.insert(insert_at, new_p)

    expected_flat_text = _omml_flatten_text_local(omml)

    # ddd79188 -- hold docx_path's promotion lock across stage+promote
    # (_save_docx_xml_stdlib, which reentrantly acquires it internally)
    # THROUGH the post-write structural verify, any conditional restore, and
    # the real render-capability gate below -- closing the same-process
    # window between promotion and verify/restore entirely (see
    # _docx_promotion_lock's module-level comment).
    with _docx_promotion_lock(docx_path):
        try:
            transaction = _save_docx_xml_stdlib(raw, root, docx_path)
        except OSError as exc:
            return {"error": f"could not write {docx_path}: {exc}"}

        promoted_sha256 = transaction.get("promoted_sha256") if transaction else None

        verify_error = _verify_equation_write(
            docx_path,
            position=position,
            anchor_para_id=anchor_para_id,
            insert_at=insert_at,
            expected_flat_text=expected_flat_text,
        )
        if verify_error is not None:
            # 5988a5bb -- do NOT blindly restore: a different (concurrent)
            # writer may have already promoted something newer to docx_path
            # since our own promotion, in which case this verification
            # "failure" is a false positive and restoring from our own
            # backup would destroy that writer's completed, already-
            # promoted work.
            safe_to_restore, restored, concurrent_write_detected = (
                _safe_restore_after_verification_failure(docx_path, promoted_sha256)
            )
            verify_error["file_restored"] = restored
            verify_error["concurrent_write_detected"] = concurrent_write_detected
            if not safe_to_restore:
                if concurrent_write_detected:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- AND a different writer's promotion has landed on "
                        "this file since ours, so this verification failure "
                        "could not be safely auto-corrected: restoring from our "
                        "own backup would destroy that writer's already-promoted "
                        f"work. {docx_path} was left untouched, exactly as that "
                        "other writer left it -- investigate manually."
                    )
                else:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- this write's own promotion fingerprint is "
                        "unavailable, so it could not be safely confirmed that "
                        "restoring from backup would not destroy a different "
                        f"writer's work; {docx_path} was left untouched rather "
                        "than risk it -- investigate manually."
                    )
            verify_error["position"] = position
            verify_error["para_id"] = anchor_para_id
            verify_error["docx_path"] = docx_path
            return verify_error

        # ddd79188 -- structural verification alone (above) can never prove
        # the document actually renders in Word; run the real render-
        # capability gate now, still inside the promotion lock so a
        # fail-closed restore has the same CAS safety a structural failure
        # gets. Must run AFTER structural verification, not instead of it.
        render_error, render_info = _enforce_render_verification(
            docx_path,
            promoted_sha256=promoted_sha256,
            allow_degraded_render=allow_degraded_render,
            degraded_render_reason=degraded_render_reason,
        )
        if render_error is not None:
            render_error["position"] = position
            render_error["para_id"] = anchor_para_id
            render_error["docx_path"] = docx_path
            return render_error

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "inserted",
        "position": position,
        "para_id": anchor_para_id,
        "omml": omml,
        "docx_path": docx_path,
        **render_info,
    }


def _direct_parent_in_paragraph(
    para_elem: ET.Element, target: ET.Element
) -> ET.Element | None:
    """b6a9ec99 -- find ``target``'s real direct parent within ``para_elem``.

    ``xml.etree.ElementTree`` gives no parent pointers, so this walks every
    element in the paragraph (itself included) and returns the first one
    whose immediate children contain ``target`` (by identity -- ``Element``
    has no custom ``__eq__``, so ``in`` here is exactly "is this the same
    node", never a structural/content match). Returns ``None`` if ``target``
    is not a descendant of ``para_elem`` at all.
    """
    for candidate in para_elem.iter():
        if target in list(candidate):
            return candidate
    return None


def edit_equation_local(
    docx_path: str,
    equation_para_id: str,
    new_payload: str,
    equation_index: int | None = None,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """a80af3a0 — Replace the <m:oMath> in an existing equation paragraph.

    Locates the paragraph by equation_para_id, verifies it contains at
    least one <m:oMath>, and replaces exactly ONE targeted equation with a
    precise single-element swap inside that equation's OWN real parent --
    the paragraph itself, a display-equation <m:oMathPara> wrapper, or any
    other container the source document nested it in (e.g. a hyperlink or
    content control). Every other child of that parent -- other equations,
    text runs, fields, bookmarks, drawings -- keeps its exact original
    identity and order; nothing is ever rebuilt or reordered wholesale.

    b6a9ec99 -- fail-closed hardening. The previous implementation rebuilt
    the paragraph's ENTIRE child list and kept only the FIRST direct
    <m:oMath>/<m:oMathPara> child, silently discarding every other
    equation a multi-equation paragraph held (real data loss with no error
    and no warning). It also picked the first nested <m:oMath> found
    anywhere in the paragraph via document-order traversal without ever
    checking whether that pick was unambiguous. Both are fixed here: when
    the paragraph contains more than one <m:oMath> (direct children,
    equations nested elsewhere, or several stacked under one shared
    <m:oMathPara>), equation_index is now REQUIRED -- mirroring
    append_text_run_after_math's existing math_index contract -- so this
    function never again guesses which equation to touch.

    Args:
        docx_path:         Absolute path to the .docx file (mutated in place).
        equation_para_id:  w14:paraId or p{N} of the equation paragraph.
        new_payload:       Raw OMML XML or LaTeX expression.
        equation_index:    0-based index (document order) of which equation
                            to replace when the paragraph holds more than
                            one. Required in that case; ignored (the sole
                            equation is used) when there is exactly one.
        index_db_path:     If supplied, sidecar is invalidated after write.

    Returns:
        {status, equation_para_id, equation_index, omml, docx_path}
        or {"error": <message>} on failure.
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

    m_omath_tag = _qm("oMath")
    existing = list(para_elem.iter(m_omath_tag))
    if not existing:
        return {
            "error": (
                f"paragraph {equation_para_id!r} does not contain an <m:oMath> element; "
                "use insert_equation_local to add a new equation"
            )
        }

    if equation_index is None and len(existing) != 1:
        return {
            "error": (
                f"paragraph {equation_para_id!r} contains {len(existing)} equations; "
                "equation_index is required to avoid guessing which one to replace"
            )
        }
    if equation_index is None:
        selected_index = 0
    elif not isinstance(equation_index, int) or isinstance(equation_index, bool):
        return {"error": "equation_index must be a non-negative integer"}
    elif equation_index < 0 or equation_index >= len(existing):
        return {
            "error": (
                f"equation_index {equation_index} is out of range for "
                f"{len(existing)} equations"
            )
        }
    else:
        selected_index = equation_index

    target_omath = existing[selected_index]
    target_parent = _direct_parent_in_paragraph(para_elem, target_omath)
    if target_parent is None:
        return {"error": "could not locate the existing equation container"}

    # Precise single-slot swap: replace ONLY target_omath within its own
    # direct parent's children, at its own index. Whether that parent is
    # the paragraph, an <m:oMathPara> wrapper (preserved intact -- including
    # any oMathParaPr formatting -- unlike the old wrapper-replacing path),
    # or a deeper nested container, every sibling keeps its exact identity
    # and order.
    replacement = ET.fromstring(omml)
    siblings = list(target_parent)
    siblings[siblings.index(target_omath)] = replacement
    target_parent[:] = siblings

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "edited",
        "equation_para_id": equation_para_id,
        "equation_index": selected_index,
        "omml": omml,
        "docx_path": docx_path,
    }

def append_text_run_after_math(
    docx_path: str,
    equation_para_id: str,
    text: str,
    math_index: int | None = None,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """Append a normal Word text run immediately after a selected equation.

    The paragraph is resolved by its stable paragraph id. If it contains more
    than one equation, math_index is required to avoid guessing which equation
    receives the text.
    """
    if not isinstance(text, str) or not text:
        return {"error": "text must be a non-empty string"}

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
    m_omath_tag = _qm("oMath")
    equations = list(para_elem.iter(m_omath_tag))
    if not equations:
        return {
            "error": (
                f"paragraph {equation_para_id!r} does not contain an <m:oMath> element"
            )
        }
    if math_index is None and len(equations) != 1:
        return {
            "error": (
                f"paragraph {equation_para_id!r} contains {len(equations)} equations; "
                "math_index is required"
            )
        }
    if math_index is None:
        selected_index = 0
    elif not isinstance(math_index, int) or isinstance(math_index, bool):
        return {"error": "math_index must be a non-negative integer"}
    elif math_index < 0 or math_index >= len(equations):
        return {
            "error": (
                f"math_index {math_index} is out of range for "
                f"{len(equations)} equations"
            )
        }
    else:
        selected_index = math_index

    selected = equations[selected_index]
    direct_parent = _direct_parent_in_paragraph(para_elem, selected)
    if direct_parent is None:
        return {"error": "could not locate the selected equation container"}

    # b6a9ec99 -- a bare <w:r> run is not a schema-valid child of
    # <m:oMathPara> (CT_OMathPara only allows an optional oMathParaPr plus
    # one or more oMath elements); the old code inserted directly inside it
    # whenever a display equation happened to be wrapped that way, silently
    # producing ill-formed OOXML. Climb to insert after the WHOLE wrapper
    # instead -- but only when `selected` is the SOLE equation inside it: a
    # multi-equation <m:oMathPara> block has no single unambiguous "right
    # after this one" slot without splitting the block apart, so that case
    # is rejected below rather than guessed at.
    m_omath_para_tag = _qm("oMathPara")
    if direct_parent.tag == m_omath_para_tag:
        stacked = [c for c in direct_parent if c.tag == m_omath_tag]
        if len(stacked) != 1:
            return {
                "error": (
                    f"equation {selected_index} in paragraph {equation_para_id!r} is "
                    "one of several equations stacked inside a shared <m:oMathPara> "
                    "block; appending a run immediately after just one of them is "
                    "ambiguous -- edit the paragraph directly instead"
                )
            }
        container_elem = direct_parent
        container_parent = _direct_parent_in_paragraph(para_elem, direct_parent)
    else:
        container_elem = selected
        container_parent = direct_parent

    # Fail closed rather than guess: only insert when the resolved slot is a
    # DIRECT child of the paragraph itself. An equation nested any deeper --
    # inside a hyperlink, a content control's sdtContent, a drawing's
    # txbxContent, or similar -- has no single obviously-correct place for a
    # brand-new run relative to that container's own semantics (e.g.
    # inserting inside a hyperlink would silently make the appended text
    # part of the hyperlink).
    if container_parent is not para_elem:
        return {
            "error": (
                f"equation {selected_index} in paragraph {equation_para_id!r} is "
                "nested inside a container (hyperlink, content control, drawing "
                "text box, or similar) this tool will not guess an insertion "
                "point inside of -- edit the paragraph directly instead"
            )
        }

    run = ET.Element(_q(_W, "r"))
    text_elem = ET.SubElement(run, _q(_W, "t"))
    text_elem.text = text
    children = list(container_parent)
    children.insert(children.index(container_elem) + 1, run)
    container_parent[:] = children

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "appended",
        "equation_para_id": equation_para_id,
        "math_index": selected_index,
        "text": text,
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

    # b6a9ec99 -- fail-closed hardening: the prior implementation replaced
    # only the FIRST <w:t> found (via document-order iteration) and `break`,
    # unconditionally. Two related gaps: a paragraph with zero <w:t> runs
    # (e.g. hand-edited down to just its bookmark pair) silently reported
    # {"status": "updated"} despite writing nothing; a paragraph with MORE
    # than one <w:t> silently overwrote only the first and left every other
    # run's stale old text sitting right next to the new text -- corrupting
    # the entry rather than updating it. Both now fail closed instead of
    # guessing. This does not touch element order/structure at all (only
    # .text on the single matched node), so it carries no OMML/child-order
    # risk of its own -- it does not share edit_equation_local's rewrite path.
    text_elems = list(entry_elem.iter(_q(_W, "t")))
    if not text_elems:
        return {
            "error": (
                f"bibliography entry {clean_key!r} has no <w:t> text run to update "
                "-- the paragraph may have been hand-edited into an unexpected "
                "shape; fix it in Word or remove and re-insert the entry"
            )
        }
    if len(text_elems) > 1:
        return {
            "error": (
                f"bibliography entry {clean_key!r} contains {len(text_elems)} text "
                "runs; refusing to guess which one holds the reference text -- "
                "silently overwriting the first and leaving the rest stale would "
                "corrupt the entry -- fix it in Word or remove and re-insert it"
            )
        }
    text_elems[0].text = formatted_text
    text_elems[0].set(_q(_XML_NS, "space"), "preserve")

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

    5b2ce3fb -- seeded at 200000000 (mirroring :func:`_next_ref_bookmark_name`'s
    own 100000000 baseline for the identical reason): :func:`_build_internal_note_paragraph`
    reuses this name's digit suffix verbatim as the paragraph's numeric
    ``w:bookmarkStart``/``w:bookmarkEnd`` ``w:id`` (see that function), so the
    number must stay clear of the small sequential ids (0, 1, 2, ...) Word
    itself assigns to bookmarks a human author creates -- a low seed like the
    previous ``0`` would reliably collide with those on any document that
    already has real bookmarks, producing Word-invalid duplicate ``w:id``
    markers. Distinct from the ``_Ref`` range (100000000-199999999) so the
    two schemes stay visually and numerically separate, matching this
    function's own "never collide" docstring claim above.
    """
    max_seen = 200000000 - 1
    for bm in root.iter(_q(_W, "bookmarkStart")):
        name = bm.get(_q(_W, "name")) or ""
        m = _INTERNAL_NOTE_BOOKMARK_RE.match(name)
        if m:
            max_seen = max(max_seen, int(m.group(1)))
    return f"{_INTERNAL_NOTE_BOOKMARK_PREFIX}{max_seen + 1}"


def _build_internal_note_paragraph(
    text: str,
    note_id: str,
    style: str,
    highlight_color: str = _INTERNAL_NOTE_HIGHLIGHT_COLOR,
) -> ET.Element:
    """Build a ``<w:p>`` for a highlighted internal-author-note paragraph.

    Produces a paragraph styled ``style`` (falls back to Normal rendering in
    Word if that style isn't defined in styles.xml -- the run-level
    ``w:highlight`` is what guarantees visible distinctiveness regardless),
    wrapped in a ``_MNote<digits>`` bookmark so :func:`list_internal_notes`
    and future tooling can locate it precisely instead of re-matching on text.

    4efc63fd -- ``highlight_color`` (from
    ``style_policy["note_highlight_color"]`` via :func:`resolve_style_policy`)
    defaults to the original hardcoded ``"yellow"``.

    5b2ce3fb -- the bookmark pair's numeric ``w:id`` is derived from
    ``note_id``'s own digit suffix (the sole caller sources ``note_id`` from
    :func:`_next_note_bookmark_name`, so that suffix is always present and
    already document-unique) instead of a hardcoded constant. A previous
    hardcoded ``w:id="0"`` meant every internal note (and every caption, see
    :func:`_build_caption_paragraph`) inserted into the same document
    produced ANOTHER bookmarkStart/bookmarkEnd pair sharing that id --
    Word-invalid duplicate ``w:id`` markers that make bookmarkStart/
    bookmarkEnd pairing ambiguous the moment more than one such element
    exists in the file.
    """
    p = ET.Element(_q(_W, "p"))
    pPr = ET.SubElement(p, _q(_W, "pPr"))
    pStyle = ET.SubElement(pPr, _q(_W, "pStyle"))
    pStyle.set(_q(_W, "val"), style)

    bm_id_match = _INTERNAL_NOTE_BOOKMARK_RE.match(note_id)
    bm_id = bm_id_match.group(1) if bm_id_match else note_id

    bm_start = ET.SubElement(p, _q(_W, "bookmarkStart"))
    bm_start.set(_q(_W, "id"), bm_id)
    bm_start.set(_q(_W, "name"), note_id)

    r = ET.SubElement(p, _q(_W, "r"))
    rPr = ET.SubElement(r, _q(_W, "rPr"))
    highlight = ET.SubElement(rPr, _q(_W, "highlight"))
    highlight.set(_q(_W, "val"), highlight_color)
    t = ET.SubElement(r, _q(_W, "t"))
    t.set(_q(_XML_NS, "space"), "preserve")
    t.text = text

    bm_end = ET.SubElement(p, _q(_W, "bookmarkEnd"))
    bm_end.set(_q(_W, "id"), bm_id)
    return p


# 7205c8e0 -- tracked-changes insertion support: a w:ins-wrapped paragraph
# insert. Scope is deliberately narrow -- insertion only, NOT deletion
# tracking (w:del) or review/accept-reject (a separate, deprioritized
# concern, proposal 9b7ecceb). w:ins/w:del are inline within document.xml
# itself (confirmed via the OOXML spec), no new part/relationship needed --
# fits the existing single-part write invariant every function above already
# relies on, unlike set_page_header/set_page_footer's multi-part write below.


def _next_revision_id(root: ET.Element) -> int:
    """Mint the next unused numeric ``w:id`` for a new ``w:ins``/``w:del``
    element, by scanning every existing ``w:ins``/``w:del`` element's ``w:id``
    anywhere in the document and returning one past the max seen. Mirrors
    :func:`_next_note_bookmark_name`'s max-seen-plus-one pattern, but over
    revision ids (shared by both w:ins and w:del, per OOXML's own ``w:id``
    revision-numbering scheme) rather than bookmark names.
    """
    w_id_attr = _q(_W, "id")
    ins_tag = _q(_W, "ins")
    del_tag = _q(_W, "del")
    max_seen = 0
    for el in root.iter():
        if el.tag not in (ins_tag, del_tag):
            continue
        raw_id = el.get(w_id_attr)
        if raw_id is None:
            continue
        try:
            max_seen = max(max_seen, int(raw_id))
        except ValueError:
            continue
    return max_seen + 1


def _build_tracked_insertion_paragraph(
    text: str, para_id: str, revision_id: int, author: str
) -> ET.Element:
    """Build a ``<w:p>`` whose entire content is one ``<w:ins>``-wrapped
    ``<w:r>`` -- a genuine tracked-changes paragraph INSERT, not a plain
    untracked paragraph. Carries a real ``w14:paraId`` (this is a brand-new
    paragraph, so a real id is minted for it up front -- never a synth id,
    which only ever describes paragraphs already present at parse time).

    ``w:date`` is stamped as UTC ISO-8601 with a literal ``"Z"`` suffix --
    Word's own convention. ``datetime.isoformat()`` on an aware UTC
    ``datetime`` instead produces a ``"+00:00"`` offset suffix, which real
    Word output never emits and which some strict OOXML consumers reject.
    """
    p = ET.Element(_q(_W, "p"))
    p.set(_q(_W14, "paraId"), para_id)

    ins = ET.SubElement(p, _q(_W, "ins"))
    ins.set(_q(_W, "id"), str(revision_id))
    ins.set(_q(_W, "author"), author)
    ins.set(_q(_W, "date"), datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    r = ET.SubElement(ins, _q(_W, "r"))
    t = ET.SubElement(r, _q(_W, "t"))
    t.set(_q(_XML_NS, "space"), "preserve")
    t.text = text
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
# b2035fb4 -- literal-text reference profiles for find_references_to.
#
# The field-based scan below only ever sees REF/PAGEREF/NOTEREF fields --
# nothing in OOXML distinguishes an intentional literal mention ("as shown in
# Figure 5.21") from any other prose, so a caption referenced only by typed-in
# text is invisible to it. Each "profile" is just the alias words + number
# shape for one caption kind (deliberately named like
# :func:`resolve_style_policy`'s vocabulary even though this scan is
# read-only and has no policy to persist).  Figure/Table are the only kinds
# find_references_to can currently resolve a "current number" for (via
# :func:`_caption_kind_and_seq`, which reads SEQ Figure / SEQ Table fields);
# Equation is included in the profile table for symmetry with any future
# SEQ-Equation-backed caption support, but is inert until a target actually
# resolves to that kind.
# ---------------------------------------------------------------------------
_LITERAL_REF_ALIASES: dict[str, tuple[str, ...]] = {
    "Figure": ("Figure", r"Fig\."),
    "Table": ("Table", r"Tab\."),
    "Equation": ("Equation", r"Eq\."),
}
_LITERAL_REF_NUMBER = r"(\d+(?:\.\d+)*)"
_FIELD_DRIVEN_TYPES = frozenset({"REF", "PAGEREF", "NOTEREF", "SEQ"})


def _literal_reference_pattern(kind: str) -> re.Pattern[str] | None:
    """Compile the literal-text reference regex for ``kind`` (``None`` if
    ``kind`` has no registered profile)."""
    aliases = _LITERAL_REF_ALIASES.get(kind)
    if not aliases:
        return None
    alt = "|".join(aliases)
    return re.compile(rf"\b(?:{alt})\s+{_LITERAL_REF_NUMBER}", re.IGNORECASE)


_LITERAL_REF_PATTERNS: dict[str, re.Pattern[str]] = {
    kind: _literal_reference_pattern(kind) for kind in _LITERAL_REF_ALIASES
}


def _block_has_field_driven_text(block: dict[str, Any]) -> bool:
    """True when ``block`` contains a REF/PAGEREF/NOTEREF/SEQ field.

    Such a block's ``text`` is (at least partly) a field's CACHED rendering
    (e.g. a REF field's cached display text is literally ``"Figure 1"``, and
    a caption paragraph's own text embeds its SEQ number) rather than
    typed-in prose -- scanning it for literal references would either
    double-count a reference the field-based scan already found, or flag a
    caption's own number against itself. Literal scanning skips these blocks
    entirely so ``literal_references`` only ever reports genuine plain text.
    """
    return any(
        fld.get("field_type") in _FIELD_DRIVEN_TYPES for fld in block.get("fields", [])
    )


def _current_kind_number_counts(blocks: list[dict[str, Any]], kind: str) -> dict[str, int]:
    """Count how many live captions of ``kind`` (``"Figure"``/``"Table"``)
    currently cache each SEQ number, across ``blocks`` from
    :func:`document_content_tree`.

    Mirrors :func:`renumber_sequences`'s own collision bookkeeping so a
    literal match against a number two DIFFERENT captions currently share is
    reported ``"ambiguous"`` rather than a false-confident ``"exact"``.
    """
    is_caption = _is_figure_caption if kind == "Figure" else _is_table_caption
    seq_re = _SEQ_FIGURE_RE if kind == "Figure" else _SEQ_TABLE_RE
    counts: dict[str, int] = {}
    for block in blocks:
        if not is_caption(block):
            continue
        num = _seq_cached_number(block, seq_re)
        if num:
            counts[num] = counts.get(num, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Public API 2/9: find_references_to (fea654f9)
# ---------------------------------------------------------------------------

def find_references_to(
    docx_path: str,
    target_id: str,
    include_literal: bool = True,
) -> dict[str, Any]:
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

    b2035fb4 -- when the target resolves to a Figure/Table caption (so its
    CURRENT cached SEQ number is known) and ``include_literal`` is True
    (the default), a second, independent scan runs over every plain-text
    paragraph/table cell -- excluding any block that itself carries a
    REF/PAGEREF/NOTEREF/SEQ field, see :func:`_block_has_field_driven_text`
    -- for literal mentions like ``"Figure 5.21"`` or ``"Table 11"`` using
    the alias/number profile in ``_LITERAL_REF_ALIASES``. Each hit is
    classified:
      - ``"exact"``: the matched number equals the target's current cached
        number, and no OTHER live caption of the same kind currently shares
        that number.
      - ``"ambiguous"``: the matched number equals the target's current
        cached number, but at least one other live caption of the same kind
        ALSO currently caches that number (a numbering collision), so the
        literal text cannot be confidently attributed to this target alone.
      - ``"stale"``: the matched number does not equal the target's current
        cached number -- almost always a manually-typed reference that
        predates a renumbering and was never updated by hand (this is the
        motivating gap: :func:`renumber_sequences` fixes REF field caches
        automatically, but has no way to find or fix literal prose).
    These never mutate the document and never suppress the field-based
    ``references`` list -- ``combined_references`` is the closure of both,
    meant to be reviewed BEFORE calling :func:`renumber_sequences` so any
    ``"stale"``/``"ambiguous"`` literal mention can be triaged by a human
    while the pre-renumber numbers are still visible in the report.

    This is read-only -- it never mutates ``docx_path`` and never retrofits a
    missing bookmark (unlike :func:`insert_cross_reference`, which creates
    one when the target caption predates cross-reference support).

    Args:
        docx_path: Absolute path to the .docx file.
        target_id: A caption/heading para_id, or an existing bookmark name.
        include_literal: When True (default), also run the literal-text scan
            described above. Set False to reproduce the pre-b2035fb4,
            field-only behavior exactly.

    Returns:
        ``{target_id, target_kind, bookmark_names, references,
        reference_count, literal_references, literal_reference_count,
        combined_references, combined_reference_count, docx_path}``.
        Each entry in ``references`` is ``{para_id, index, field_type,
        bookmark_name, display_text, paragraph_text, match_kind="field"}``.
        Each entry in ``literal_references`` is ``{para_id, index,
        match_kind="literal", field_type=None, matched_text, matched_number,
        status, paragraph_text}``. ``combined_references`` is simply
        ``references + literal_references``.

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
    kind_seq: tuple[str, str] | None = None
    own_body_idx: int | None = None

    if _REF_BOOKMARK_RE.match(target_id) or target_id.startswith(_BIBKEY_BOOKMARK_PREFIX) \
            or _INTERNAL_NOTE_BOOKMARK_RE.match(target_id):
        found_directly = any(
            bm.get(_q(_W, "name")) == target_id for bm in body.iter(_q(_W, "bookmarkStart"))
        )
        if not found_directly:
            return {"error": f"bookmark {target_id!r} not found in {docx_path}"}
        bookmark_names.append(target_id)

        if _REF_BOOKMARK_RE.match(target_id):
            caption_hit = _find_caption_by_ref_bookmark(root, target_id)
            if caption_hit is not None:
                caption_elem, kind_seq = caption_hit
                target_kind = kind_seq[0]
                body_children = list(body)
                if caption_elem in body_children:
                    own_body_idx = body_children.index(caption_elem)
    else:
        result = _find_para_by_id(root, target_id)
        if result is None:
            return {"error": f"para_id {target_id!r} not found in {docx_path}"}
        _body, target_elem, _idx = result
        own_body_idx = _idx

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
            "literal_references": [],
            "literal_reference_count": 0,
            "combined_references": [],
            "combined_reference_count": 0,
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
                    "match_kind": "field",
                })

    own_para_id = (
        blocks[own_body_idx].get("para_id")
        if own_body_idx is not None and 0 <= own_body_idx < len(blocks)
        else None
    )

    literal_references: list[dict[str, Any]] = []
    if include_literal and kind_seq is not None:
        literal_kind, current_number = kind_seq
        pattern = _LITERAL_REF_PATTERNS.get(literal_kind)
        if pattern is not None:
            number_counts = _current_kind_number_counts(blocks, literal_kind)
            for block in blocks:
                if own_para_id is not None and block.get("para_id") == own_para_id:
                    continue
                if _block_has_field_driven_text(block):
                    continue
                text = block.get("text") or ""
                for m in pattern.finditer(text):
                    matched_number = m.group(1)
                    if matched_number == current_number:
                        status = (
                            "ambiguous" if number_counts.get(matched_number, 0) > 1 else "exact"
                        )
                    else:
                        status = "stale"
                    literal_references.append({
                        "para_id": block.get("para_id"),
                        "index": block.get("index"),
                        "match_kind": "literal",
                        "field_type": None,
                        "matched_text": m.group(0),
                        "matched_number": matched_number,
                        "status": status,
                        "paragraph_text": text,
                    })

    return {
        "target_id": target_id,
        "target_kind": target_kind,
        "bookmark_names": bookmark_names,
        "references": references,
        "reference_count": len(references),
        "literal_references": literal_references,
        "literal_reference_count": len(literal_references),
        "combined_references": references + literal_references,
        "combined_reference_count": len(references) + len(literal_references),
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

def _verify_note_write(
    docx_path: str,
    *,
    note_id: str,
    expected_text: str,
) -> dict[str, Any] | None:
    """65c8eb31 follow-up (ddd79188) — post-write verification for
    :func:`insert_highlighted_note`'s ``mode="inline"`` path, mirroring
    :func:`_verify_figure_block_write`'s "brand new content, no prior
    on-disk baseline to diff against" style.

    Re-reads ``docx_path`` FRESH FROM DISK and locates the note paragraph by
    its unique ``_MNote<digits>`` bookmark (assigned before the write via
    :func:`_next_note_bookmark_name`) rather than by position — positions
    shift, bookmarks don't. Confirms that paragraph's full text matches
    ``expected_text`` exactly. Returns ``None`` when every check passes, or
    an ``{"error": ...}`` dict on the first mismatch.
    """
    try:
        _raw2, root2 = _load_docx_xml_stdlib(docx_path)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "error": (
                "post-write verification failed: could not re-read "
                f"{docx_path} after writing it: {exc}"
            )
        }

    body2 = root2.find(_q(_W, "body"))
    if body2 is None:
        return {
            "error": (
                "post-write verification failed: re-read of "
                f"{docx_path} has no <w:body> element"
            )
        }

    w_bookmark_start = _q(_W, "bookmarkStart")
    w_p = _q(_W, "p")
    note_para = next(
        (
            child
            for child in body2
            if child.tag == w_p
            and any(
                bm.get(_q(_W, "name")) == note_id
                for bm in child.findall(w_bookmark_start)
            )
        ),
        None,
    )
    if note_para is None:
        return {
            "error": (
                "post-write verification failed: no internal-note "
                f"paragraph carrying bookmark {note_id!r} was found in "
                f"{docx_path} after the write"
            )
        }

    actual_text = "".join(t.text or "" for t in note_para.iter(_q(_W, "t")))
    if actual_text != expected_text:
        return {
            "error": (
                "post-write verification failed: internal-note text "
                f"mismatch in paragraph (bookmark {note_id!r}) (expected "
                f"{expected_text!r}, got {actual_text!r})"
            )
        }
    return None


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
) -> dict[str, Any]:
    """Insert an internal note inline or as a native Word comment.

    mode="inline" preserves the original Meridian highlighted-note behavior.
    mode="comment" writes Word's comments.xml part, relationship, content-type
    override, range markers, and comment reference so Microsoft Word displays
    the note in its normal review pane.

    4efc63fd — ``style_policy`` (resolved via :func:`resolve_style_policy`)
    supplies the OOXML paragraph style name (``note_style``, default
    ``"MeridianInternalNote"``) and highlight color (``note_highlight_color``,
    default ``"yellow"``) for ``mode="inline"`` notes. Not consulted for
    ``mode="comment"`` (Word comments have no paragraph style/highlight of
    their own — they live in a separate comments.xml part).  Distinct from
    the existing ``style`` parameter above, which selects the note's
    *category* (currently only ``"internal_note"`` is supported), not its
    OOXML rendering.

    ddd79188 — for ``mode="inline"``, AFTER the write is staged, structurally
    verified (see :func:`_verify_note_write`), and promoted, a real Word/COM
    (or LibreOffice) render-capability check also runs against the
    just-written file (:func:`_enforce_render_verification`), mirroring the
    same gate :func:`insert_figure_block` already enforces. ``"rendered"``
    continues normally with render evidence attached. ``"failed"`` restores
    ``docx_path`` from the pre-write backup and returns an error.
    ``"unavailable-with-reason"`` (no render backend in this environment)
    ALSO fails closed by default unless the caller explicitly passes
    ``allow_degraded_render=True`` with a non-empty ``degraded_render_reason``.
    For ``mode="comment"``, ``allow_degraded_render``/``degraded_render_reason``
    are forwarded verbatim to :func:`insert_word_comment`, which already
    enforces this same gate (5bab074/W2-C) for its own comments.xml write.
    """
    if not text or not str(text).strip():
        return {"error": "text must be a non-empty string"}
    if position not in ("before", "after"):
        return {"error": f"position must be 'before' or 'after', got {position!r}"}
    if style != "internal_note":
        return {
            "error": (
                "style must be 'internal_note' (the only supported internal-note "
                f"style), got {style!r}"
            )
        }
    if mode not in ("inline", "comment"):
        return {"error": f"mode must be 'inline' or 'comment', got {mode!r}"}
    if allow_degraded_render and not (
        degraded_render_reason and str(degraded_render_reason).strip()
    ):
        return {
            "error": (
                "degraded_render_reason is required and must be non-empty "
                "when allow_degraded_render=True -- an audited degrade with "
                "no stated reason is not auditable and is refused"
            )
        }

    try:
        policy = resolve_style_policy(style_policy)
    except ValueError as exc:
        return {"error": str(exc)}

    if mode == "comment":
        result = insert_word_comment(
            docx_path=docx_path,
            text=text,
            anchor_para_id=anchor_para_id,
            author=author,
            initials=initials,
            allow_degraded_render=allow_degraded_render,
            degraded_render_reason=degraded_render_reason,
        )
        if result.get("status") == "inserted":
            comment_id = result["comment_id"]
            note_id = f"_MComment{comment_id}"
            result["note_id"] = note_id
            result["style"] = style
            _invalidate_sidecar_mtime(index_db_path)
            if index_db_path and os.path.exists(index_db_path):
                _upsert_sidecar_note(index_db_path, note_id, text.strip(), anchor_para_id)
        return result

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
    text_clean = text.strip()
    note_p = _build_internal_note_paragraph(
        text_clean, note_id, policy["note_style"], policy["note_highlight_color"]
    )

    insert_at = child_idx if position == "before" else child_idx + 1
    body.insert(insert_at, note_p)

    # ddd79188 -- hold docx_path's promotion lock across stage+promote
    # (_save_docx_xml_stdlib, which reentrantly acquires it internally)
    # THROUGH the post-write structural verify, any conditional restore, and
    # the real render-capability gate below -- closing the same-process
    # window between promotion and verify/restore entirely (see
    # _docx_promotion_lock's module-level comment).
    with _docx_promotion_lock(docx_path):
        try:
            transaction = _save_docx_xml_stdlib(raw, root, docx_path)
        except OSError as exc:
            return {"error": f"could not write {docx_path}: {exc}"}

        promoted_sha256 = transaction.get("promoted_sha256") if transaction else None

        verify_error = _verify_note_write(
            docx_path,
            note_id=note_id,
            expected_text=text_clean,
        )
        if verify_error is not None:
            # 5988a5bb -- do NOT blindly restore: a different (concurrent)
            # writer may have already promoted something newer to docx_path
            # since our own promotion, in which case this verification
            # "failure" is a false positive and restoring from our own
            # backup would destroy that writer's completed, already-
            # promoted work.
            safe_to_restore, restored, concurrent_write_detected = (
                _safe_restore_after_verification_failure(docx_path, promoted_sha256)
            )
            verify_error["file_restored"] = restored
            verify_error["concurrent_write_detected"] = concurrent_write_detected
            if not safe_to_restore:
                if concurrent_write_detected:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- AND a different writer's promotion has landed on "
                        "this file since ours, so this verification failure "
                        "could not be safely auto-corrected: restoring from our "
                        "own backup would destroy that writer's already-promoted "
                        f"work. {docx_path} was left untouched, exactly as that "
                        "other writer left it -- investigate manually."
                    )
                else:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- this write's own promotion fingerprint is "
                        "unavailable, so it could not be safely confirmed that "
                        "restoring from backup would not destroy a different "
                        f"writer's work; {docx_path} was left untouched rather "
                        "than risk it -- investigate manually."
                    )
            verify_error["note_id"] = note_id
            verify_error["docx_path"] = docx_path
            return verify_error

        # ddd79188 -- structural verification alone (above) can never prove
        # the document actually renders in Word; run the real render-
        # capability gate now, still inside the promotion lock so a
        # fail-closed restore has the same CAS safety a structural failure
        # gets. Must run AFTER structural verification, not instead of it.
        render_error, render_info = _enforce_render_verification(
            docx_path,
            promoted_sha256=promoted_sha256,
            allow_degraded_render=allow_degraded_render,
            degraded_render_reason=degraded_render_reason,
        )
        if render_error is not None:
            render_error["note_id"] = note_id
            render_error["docx_path"] = docx_path
            return render_error

    _invalidate_sidecar_mtime(index_db_path)

    if index_db_path and os.path.exists(index_db_path):
        _upsert_sidecar_note(index_db_path, note_id, text_clean, anchor_para_id)

    return {
        "status": "inserted",
        "mode": "inline",
        "note_id": note_id,
        "text": text_clean,
        "anchor_para_id": anchor_para_id,
        "position": position,
        "style": policy["note_style"],
        "docx_path": docx_path,
        **render_info,
    }


def insert_tracked_paragraph(
    docx_path: str,
    text: str,
    anchor_para_id: str,
    position: str = "after",
    author: str = "Meridian Agent",
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """7205c8e0 -- insert a genuinely tracked-changes ("w:ins"-wrapped)
    paragraph, mirroring :func:`insert_highlighted_note`'s structure exactly.

    Addresses a real, table-stakes DOCX gap: every major word processor
    (Word, Google Docs) supports inserting new content under Track Changes so
    a reviewer can see exactly what an editing pass added. This module had no
    tracked-changes support at all. The new paragraph's entire content is one
    ``<w:ins>``-wrapped ``<w:r>`` (see :func:`_build_tracked_insertion_paragraph`),
    carrying a real, freshly-minted ``w14:paraId`` (this is brand-new content,
    not something :func:`_build_synth_id_map` could ever have derived an id
    for) and a fresh, never-before-used ``w:id`` revision number (see
    :func:`_next_revision_id`) distinct from any ``w:ins``/``w:del`` already
    in the document.

    Scope (confirmed): INSERTION only -- NOT deletion tracking (``w:del``) or
    review/accept-reject workflows (a separate, deprioritized concern,
    proposal 9b7ecceb). ``w:ins``/``w:del`` live inline within
    ``word/document.xml`` itself (no new OOXML part or relationship needed,
    unlike :func:`set_page_header`/:func:`set_page_footer`'s multi-part write
    path) -- so this fits the existing single-part write invariant every
    write-back function above (up to the header/footer section) already
    relies on. Does NOT touch :func:`write_section`.

    Args:
        docx_path:      Absolute path to the .docx file (mutated in place).
        text:            The inserted paragraph's text content.
        anchor_para_id:  w14:paraId (synth id, or legacy p{N}) of the
                         paragraph to anchor on -- resolved via
                         :func:`_find_para_by_id`'s usual three-tier scheme.
        position:        "before" or "after" (default) the anchor.
        author:          Recorded as the ``w:ins`` element's ``w:author``
                         attribute (what Word displays as the reviewer name
                         for this tracked insertion). Defaults to
                         ``"Meridian Agent"``.
        index_db_path:   If supplied, invalidates that sidecar's cached mtime
                         so the next read re-parses the document (mirrors
                         every other write-back function's sidecar handling;
                         there is no dedicated tracked-insertion sidecar table
                         the way :func:`insert_highlighted_note` has
                         ``docx_internal_notes`` -- the new paragraph is
                         immediately findable via its real ``w14:paraId`` by
                         any other function in this module, no reindex
                         needed).

    Returns:
        ``{status, para_id, revision_id, text, anchor_para_id, position,
        author, docx_path}`` on success, or ``{"error": <message>}`` on
        failure (file NOT mutated on error).
    """
    if not text or not str(text).strip():
        return {"error": "text must be a non-empty string"}
    if position not in ("before", "after"):
        return {"error": f"position must be 'before' or 'after', got {position!r}"}
    if not author or not str(author).strip():
        return {"error": "author must be a non-empty string"}

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

    taken = _existing_para_ids(root)
    new_para_id = _new_para_id(taken)
    revision_id = _next_revision_id(root)
    tracked_p = _build_tracked_insertion_paragraph(text.strip(), new_para_id, revision_id, author.strip())

    insert_at = child_idx if position == "before" else child_idx + 1
    body.insert(insert_at, tracked_p)

    try:
        _save_docx_xml_stdlib(raw, root, docx_path)
    except OSError as exc:
        return {"error": f"could not write {docx_path}: {exc}"}

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "inserted",
        "para_id": new_para_id,
        "revision_id": revision_id,
        "text": text.strip(),
        "anchor_para_id": anchor_para_id,
        "position": position,
        "author": author.strip(),
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


# ---------------------------------------------------------------------------
# fe989980 -- wave-scoped merge manifests: the file-level promotion step.
#
# meridian.db.docx_merge (in the hosted/self-hosted Meridian core package)
# owns the DURABLE, cross-session coordination for a wave of parallel DOCX
# edits: open_merge_manifest / declare_merge_anchors / claim_merge_owner /
# check_merge_stale_or_overlap / record_merge_result / finalize_merge_manifest.
# That module resolves WHO may merge and WHETHER a draft is still valid
# (ownership, declared-anchor overlap, staleness against the canonical
# file's current revision) -- but it never touches a real .docx: this
# extension is stdlib-only and deliberately has NO dependency on the
# meridian core package or its database (see server.py's module docstring
# -- "Thin MCP stdio server exposing the docs_intel DOCX parser as tools",
# run locally via ``uvx meridian-docs``, no DB connectivity at all).
#
# merge_draft_into_canonical is the file-level counterpart those DB
# primitives call out to once their gate is clear: a wave's serialized
# merge owner promotes their already-accepted draft (a COMPLETE, isolated
# .docx produced by move_section/copy_section/relocate_table/relocate_figure
# below, called with draft_output_path -- never the canonical file itself)
# over the canonical file. It reuses the exact stage -> verify -> promote
# transaction (_atomic_write_docx_bytes) every direct write in this module
# already goes through, so a structurally corrupt draft can never reach
# canonical_path, plus the SAME post-write re-read-from-disk verification +
# backup/restore discipline (_verify_docx_write / _restore_docx_backup,
# 9907df44) move_section et al. use for their own in-place writes -- applied
# here to a whole-document promotion instead of an in-place range edit.
# ---------------------------------------------------------------------------

def merge_draft_into_canonical(
    canonical_path: str,
    draft_path: str,
    index_db_path: str | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
) -> dict[str, Any]:
    """fe989980 -- promote an isolated wave-scoped draft into ``canonical_path``.

    Called by a wave's serialized merge owner AFTER the DB-side gate
    (``meridian.db.docx_merge.check_merge_stale_or_overlap``, over the
    separate Meridian MCP connection) has already cleared -- this function
    performs no ownership/overlap/staleness checks of its own; that
    coordination is durable, cross-session state this stdlib-only, DB-free
    extension cannot see.

    Promotion:
      1. ``draft_path`` is read and parsed; a draft that does not exist or
         is not a valid .docx is rejected with ``canonical_path`` untouched.
      2. The draft's whole-document bytes are staged, structurally verified
         against ``canonical_path``'s CURRENT media/style/relationship
         counts (:func:`_atomic_write_docx_bytes`'s existing ``pre_manifest``
         gate -- the same invariant every direct write in this module
         preserves), and only then promoted -- an existing ``canonical_path``
         is backed up to ``canonical_path + ".bak"`` immediately before the
         swap. A structural-invariant violation here means the STAGED draft
         is corrupt: raised as an error, ``canonical_path`` is guaranteed
         byte-for-byte untouched (promotion never runs).
      3. Post-promotion, ``canonical_path`` is re-read FRESH FROM DISK and
         its structural counts + a whole-body content hash are compared
         against the draft's OWN (pre-promotion) counts/hash -- the same
         :func:`_verify_docx_write` discipline move_section/copy_section/
         relocate_table/relocate_figure apply to their in-place writes,
         applied here to confirm the promotion itself actually landed
         (catches a silent no-op promotion or a concurrent external write).
         On mismatch, ``canonical_path`` is best-effort restored from the
         backup :func:`_atomic_write_docx_bytes` just wrote and this returns
         an ERROR -- never a false success.
      4. ddd79188 -- AFTER step 3's structural verification passes,
         :func:`render_gate.check_render_capability` is invoked on the now-
         promoted ``canonical_path`` (see :func:`_enforce_render_verification`).
         Structural reparse alone (step 3) can never prove the promoted
         document actually opens/renders in Word. ``"rendered"`` continues
         normally with render evidence attached. ``"failed"`` (a render
         backend errored on this specific document) restores
         ``canonical_path`` from the SAME backup and returns an error, same
         as a step-3 failure. ``"unavailable-with-reason"`` (no render
         backend in this environment) ALSO fails closed by default --
         never reported as verified -- unless the caller explicitly passes
         ``allow_degraded_render=True`` with a non-empty
         ``degraded_render_reason`` (audited opt-in: the promotion is kept
         but ``render_verified=False`` / ``render_degraded=True`` are
         stamped onto the payload).

    Returns ``{"merged": True, "status": "merged", "canonical_path",
    "draft_path", "paragraph_count", "heading_count", "table_count",
    "image_count", "render_status", "render_verified", ...}`` on success.

    Returns ``{"merged": False, "error": <message>, ...}`` on failure --
    with ``"file_restored": <bool>`` present only for the post-promotion
    structural-verification-failure case (step 3) or the render-verification
    failure case (step 4); every other failure mode leaves
    ``canonical_path`` untouched by construction, so there is nothing to
    restore.
    """
    if allow_degraded_render and not (
        degraded_render_reason and str(degraded_render_reason).strip()
    ):
        return {
            "merged": False,
            "error": (
                "degraded_render_reason is required and must be non-empty "
                "when allow_degraded_render=True -- an audited degrade with "
                "no stated reason is not auditable and is refused"
            ),
        }
    if not draft_path or not os.path.exists(draft_path):
        return {
            "merged": False,
            "error": f"draft_path {draft_path!r} does not exist",
        }

    try:
        with open(draft_path, "rb") as fh:
            draft_bytes = fh.read()
    except OSError as exc:
        return {
            "merged": False,
            "error": f"could not read draft_path {draft_path!r}: {exc}",
        }

    try:
        draft_raw, draft_root = _load_docx_xml_stdlib(draft_path)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "merged": False,
            "error": f"draft_path {draft_path!r} is not a valid .docx: {exc}",
        }

    draft_body = draft_root.find(_q(_W, "body"))
    if draft_body is None:
        return {
            "merged": False,
            "error": f"draft_path {draft_path!r} has no <w:body> element",
        }

    draft_children = list(draft_body)
    draft_counts = _structural_counts([draft_body])
    draft_counts["image_count"] = _docx_media_count(draft_raw)
    expected_hash = _hash_elements(draft_children)

    pre_manifest: dict[str, int] | None = None
    if os.path.exists(canonical_path):
        try:
            with open(canonical_path, "rb") as fh:
                canonical_raw = fh.read()
            pre_manifest = _docx_structural_manifest(canonical_raw)
        except (OSError, zipfile.BadZipFile, KeyError) as exc:
            return {
                "merged": False,
                "error": (
                    f"could not read existing canonical_path {canonical_path!r} "
                    f"before merging: {exc}"
                ),
            }

    # 5988a5bb -- hold canonical_path's promotion lock across stage+promote
    # (_atomic_write_docx_bytes, which reentrantly acquires it internally)
    # THROUGH the post-write verify and any conditional restore below,
    # closing the same-process window between promotion and verify/restore
    # entirely (see _docx_promotion_lock's module-level comment).
    with _docx_promotion_lock(canonical_path):
        try:
            transaction = _atomic_write_docx_bytes(
                draft_bytes,
                canonical_path,
                pre_manifest=pre_manifest,
                protected_keys=("media_count", "style_count", "relationship_count"),
            )
        except DocxWriteVerificationError as exc:
            return {
                "merged": False,
                "error": (
                    "merge rejected: the draft does not preserve structural "
                    f"elements the canonical file must never lose: {exc}"
                ),
            }
        except OSError as exc:
            return {
                "merged": False,
                "error": f"could not write {canonical_path}: {exc}",
            }

        promoted_sha256 = transaction.get("promoted_sha256") if transaction else None

        verify_error = _verify_docx_write(
            canonical_path,
            expected_counts=draft_counts,
            expected_hash=expected_hash,
            expected_range=(0, len(draft_children)),
        )
        if verify_error is not None:
            # 5988a5bb -- do NOT blindly restore. A different (concurrent)
            # writer may have already promoted something newer to
            # canonical_path since our own promotion, in which case this
            # verification "failure" is a false positive and restoring from
            # our own backup would destroy that writer's completed,
            # already-promoted work -- check first.
            safe_to_restore, restored, concurrent_write_detected = (
                _safe_restore_after_verification_failure(canonical_path, promoted_sha256)
            )
            verify_error["merged"] = False
            verify_error["file_restored"] = restored
            verify_error["concurrent_write_detected"] = concurrent_write_detected
            if not safe_to_restore:
                if concurrent_write_detected:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- AND a different writer's promotion has landed on "
                        "this file since ours, so this verification failure "
                        "could not be safely auto-corrected: restoring from our "
                        "own backup would destroy that writer's already-promoted "
                        f"work. {canonical_path} was left untouched, exactly as "
                        "that other writer left it -- investigate manually."
                    )
                else:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- this write's own promotion fingerprint is "
                        "unavailable, so it could not be safely confirmed that "
                        "restoring from backup would not destroy a different "
                        f"writer's work; {canonical_path} was left untouched "
                        "rather than risk it -- investigate manually."
                    )
            verify_error["canonical_path"] = canonical_path
            verify_error["draft_path"] = draft_path
            return verify_error

        # ddd79188 -- structural verification alone (above) can never prove
        # the promoted document actually renders in Word; run the real
        # render-capability gate now, still inside the promotion lock so a
        # fail-closed restore has the same CAS safety a structural failure
        # gets. Must run AFTER structural verification, not instead of it.
        render_error, render_info = _enforce_render_verification(
            canonical_path,
            promoted_sha256=promoted_sha256,
            allow_degraded_render=allow_degraded_render,
            degraded_render_reason=degraded_render_reason,
        )
        if render_error is not None:
            render_error["merged"] = False
            render_error["canonical_path"] = canonical_path
            render_error["draft_path"] = draft_path
            return render_error

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "merged": True,
        "status": "merged",
        "canonical_path": canonical_path,
        "draft_path": draft_path,
        **draft_counts,
        **render_info,
    }


def _resolve_draft_dest(
    docx_path: str,
    draft_output_path: str | None,
    wave_run_id: str | None,
) -> "dict[str, Any] | str":
    """fe989980 -- shared opt-in draft-mode validation for the four
    structural mutators (move_section / copy_section / relocate_table /
    relocate_figure). Returns the resolved write destination (a plain
    ``str``) on success, or an ``{"error": ...}`` dict for the caller to
    return verbatim.

    ``draft_output_path`` and ``wave_run_id`` must be supplied together or
    not at all -- wave-scoped drafting needs both an isolated write target
    AND the identifier that scopes its ``meridian.db.docx_merge`` manifest
    (this extension has no DB access to validate ``wave_run_id`` against;
    it is opaque here, threaded through only for the caller's own
    cross-reference). Omitting both is the legacy path: the destination is
    ``docx_path`` itself, byte-identical to pre-fe989980 behavior.
    """
    if bool(wave_run_id) != bool(draft_output_path):
        return {
            "error": (
                "wave_run_id and draft_output_path must be provided "
                "together -- wave-scoped drafting requires both an "
                "isolated draft target and the wave identifier that scopes "
                "its merge manifest"
            )
        }
    if not draft_output_path:
        return docx_path
    dest = draft_output_path.strip()
    if not dest:
        return {"error": "draft_output_path must be a non-empty path"}
    if os.path.normcase(os.path.abspath(dest)) == os.path.normcase(os.path.abspath(docx_path)):
        return {
            "error": (
                "draft_output_path must differ from docx_path -- a "
                "wave-scoped draft must be an isolated artifact, never the "
                "canonical file itself"
            )
        }
    return dest


def move_section(
    docx_path: str,
    section_id: str,
    destination_anchor_para_id: str,
    destination_position: str = "after",
    index_db_path: str | None = None,
    allow_bookmark_split: bool = False,
    draft_output_path: str | None = None,
    wave_run_id: str | None = None,
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
        draft_output_path:            fe989980 -- when given (together with
                                      ``wave_run_id``), the move is written to
                                      this ISOLATED path instead of
                                      ``docx_path`` -- ``docx_path`` is only
                                      ever READ, never mutated. Must differ
                                      from ``docx_path``. Omitted (the
                                      default), this call is byte-identical
                                      to the pre-fe989980 direct-write
                                      behavior.
        wave_run_id:                  fe989980 -- opaque wave identifier,
                                      required together with
                                      ``draft_output_path``; threaded straight
                                      into the return payload so a caller can
                                      cross-reference this write against the
                                      matching ``meridian.db.docx_merge``
                                      manifest. Never validated or persisted
                                      by this stdlib-only, DB-free extension.

    Returns:
        ``{status, section_id, heading_text, moved_block_count,
        destination_anchor_para_id, destination_position,
        renumber_sequences, find_references_to, docx_path, wave_run_id,
        is_draft}``. ``docx_path`` in the result is the file actually
        written -- ``draft_output_path`` when given, else the input
        ``docx_path`` (unchanged legacy behavior).

        ``{"error": <message>}`` when ``section_id`` /
        ``destination_anchor_para_id`` can't be resolved, the destination
        falls inside the section being moved, the move would split a
        bookmark (and ``allow_bookmark_split`` is not set), ``wave_run_id``/
        ``draft_output_path`` are not both given or not both omitted, or the
        write fails (file NOT mutated on error in every one of these cases).

        9907df44 -- after a successful write, mandatory post-write
        verification re-reads the file from disk and compares structural
        counts + a content hash of the moved range against what the move
        should have produced. On mismatch, returns ``{"error": <message>,
        "count_mismatches": {...}, "content_hash_mismatch": {...} | None,
        "file_restored": <bool>}`` instead of a success payload -- the file
        is best-effort restored from the pre-write ``.bak`` backup first.
    """
    if destination_position not in ("before", "after"):
        return {
            "error": f"destination_position must be 'before' or 'after', got {destination_position!r}"
        }

    dest_error = _resolve_draft_dest(docx_path, draft_output_path, wave_run_id)
    if isinstance(dest_error, dict):
        return dest_error
    dest = dest_error

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    if body is None:
        return {"error": "document has no body element"}

    # 9907df44 -- baseline structural counts, captured BEFORE any mutation, so
    # the post-write verification below has something real to compare the
    # re-read-from-disk result against (a plain relocate should leave every
    # one of these totals unchanged -- nothing is added or removed).
    baseline_counts = _structural_counts([body])
    baseline_counts["image_count"] = _docx_media_count(raw)

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

    # 9907df44 -- fingerprint the exact range being moved BEFORE it's cut, so
    # post-write verification can confirm these SAME bytes actually landed at
    # the destination (see the _verify_docx_write docstring for why a plain
    # count comparison alone can't catch a silent no-op write here).
    expected_hash = _hash_elements(moved_elements)

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

    # 5988a5bb -- hold dest's promotion lock across stage+promote
    # (_save_docx_xml_stdlib, which reentrantly acquires it internally)
    # THROUGH the post-write verify and any conditional restore below,
    # closing the same-process window between promotion and verify/restore
    # entirely (see _docx_promotion_lock's module-level comment).
    with _docx_promotion_lock(dest):
        try:
            transaction = _save_docx_xml_stdlib(raw, root, dest)
        except OSError as exc:
            return {"error": f"could not write {dest}: {exc}"}

        # 9907df44 -- mandatory post-write verification: re-read dest FRESH
        # FROM DISK and confirm the on-disk document actually reflects this move
        # before trusting/reporting the write as a success. A stale/buggy write
        # path (or one that silently no-ops) is caught here instead of producing
        # a false "moved" success -- same abort discipline as the pre-write
        # reference/bookmark-split checks above (real error, no misleading
        # status), except the file has already been written, so best-effort
        # restore it to the pre-write backup first.
        verify_error = _verify_docx_write(
            dest,
            expected_counts=baseline_counts,
            expected_hash=expected_hash,
            expected_range=(insert_at, insert_at + len(moved_elements)),
        )
        if verify_error is not None:
            # 5988a5bb -- do NOT blindly restore: a different (concurrent)
            # writer may have already promoted something newer to dest since
            # our own promotion, in which case this verification "failure"
            # is a false positive and restoring from our own backup would
            # destroy that writer's completed, already-promoted work.
            safe_to_restore, restored, concurrent_write_detected = (
                _safe_restore_after_verification_failure(
                    dest, transaction.get("promoted_sha256") if transaction else None,
                )
            )
            verify_error["file_restored"] = restored
            verify_error["concurrent_write_detected"] = concurrent_write_detected
            if not safe_to_restore:
                if concurrent_write_detected:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- AND a different writer's promotion has landed on "
                        "this file since ours, so this verification failure "
                        "could not be safely auto-corrected: restoring from our "
                        "own backup would destroy that writer's already-promoted "
                        f"work. {dest} was left untouched, exactly as that other "
                        "writer left it -- investigate manually."
                    )
                else:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- this write's own promotion fingerprint is "
                        "unavailable, so it could not be safely confirmed that "
                        "restoring from backup would not destroy a different "
                        f"writer's work; {dest} was left untouched rather than "
                        "risk it -- investigate manually."
                    )
            verify_error["section_id"] = section_id
            verify_error["moved_block_count"] = len(moved_elements)
            return verify_error

    # fe989980 -- in draft mode, docx_path (the canonical/source file) was
    # never touched, so its sidecar index is still accurate: skip invalidating it.
    if not draft_output_path:
        _invalidate_sidecar_mtime(index_db_path)

    renumber_result = renumber_sequences(dest, index_db_path=index_db_path)

    return {
        "status": "moved",
        "section_id": section_id,
        "heading_text": heading_text,
        "moved_block_count": len(moved_elements),
        "destination_anchor_para_id": destination_anchor_para_id,
        "destination_position": destination_position,
        "renumber_sequences": renumber_result,
        "find_references_to": references_result,
        "docx_path": dest,
        "wave_run_id": wave_run_id,
        "is_draft": bool(draft_output_path),
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
    draft_output_path: str | None = None,
    wave_run_id: str | None = None,
    allow_relationship_reuse: bool = False,
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

    679c86f4 -- what this deep copy does NOT do: rewrite a copied image
    paragraph's ``r:embed`` relationship id, or duplicate its underlying
    ``word/media/*`` part. A section whose copied range contains a Figure
    (an image paragraph immediately followed by its SEQ Figure caption) ends
    up with TWO independent figure blocks -- the original and the copy --
    both pointing at the SAME image relationship. After the write, the
    on-disk document is re-read and checked against the image-ownership
    invariant (:func:`_verify_image_ownership`); a detected relationship
    reuse fails the copy closed (the pre-write backup is restored, subject
    to the same compare-and-swap concurrent-writer safety as the rest of
    this module) UNLESS ``allow_relationship_reuse=True`` -- the caller's
    explicit declaration that duplicating the reference (rather than the
    underlying image) is intentional. This is a narrower, caller-declared
    stand-in for "the manifest declares intentional reuse"; actually
    duplicating the media part and minting a fresh relationship is out of
    scope here (a separate, larger change -- see fe989980's broader
    claim/manifest integration).

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
        draft_output_path:            fe989980 -- same opt-in wave-scoped
                                      draft mode as :func:`move_section`: when
                                      given (with ``wave_run_id``), the copy
                                      is written to this ISOLATED path instead
                                      of ``docx_path``, which is only ever
                                      read. Omitted (the default), behavior
                                      is byte-identical to pre-fe989980.
        wave_run_id:                  fe989980 -- required together with
                                      ``draft_output_path``; see
                                      :func:`move_section`.
        allow_relationship_reuse:    679c86f4 -- ``False`` (default, fail
                                      closed) rejects a copy that leaves an
                                      image's ``r:embed`` relationship shared
                                      between the original and the copy;
                                      ``True`` is the caller's explicit
                                      declaration that this specific reuse is
                                      intentional.

    Returns:
        ``{status, section_id, heading_text, new_heading_para_id,
        copied_block_count, para_id_map, bookmark_map,
        destination_anchor_para_id, destination_position,
        renumber_sequences, find_references_to, trimmed_original, docx_path,
        wave_run_id, is_draft}`` -- ``docx_path`` in the result is the file
        actually written (``draft_output_path`` when given, else the input
        ``docx_path``). ``para_id_map`` / ``bookmark_map`` are ``{old: new}``
        dicts for
        every paraId/bookmark that existed in the original section and was
        renamed in the copy (originals lacking a native paraId aren't keyed
        in ``para_id_map``, but the copy still gets one -- see
        ``new_heading_para_id``). ``trimmed_original`` is False when
        ``trim_original_to`` was not given.

        ``{"error": <message>}`` on failure (file NOT mutated on error).

        9907df44 -- after a successful write, mandatory post-write
        verification re-reads the file from disk and compares structural
        counts + a content hash of the copied range (located by its own
        fresh ``new_heading_para_id``) against what the copy should have
        produced. On mismatch, returns ``{"error": <message>,
        "count_mismatches": {...}, "content_hash_mismatch": {...} | None,
        "file_restored": <bool>}`` instead of a success payload -- the file
        is best-effort restored from the pre-write ``.bak`` backup first.
    """
    if destination_position not in ("before", "after"):
        return {
            "error": f"destination_position must be 'before' or 'after', got {destination_position!r}"
        }

    dest_path_error = _resolve_draft_dest(docx_path, draft_output_path, wave_run_id)
    if isinstance(dest_path_error, dict):
        return dest_path_error
    dest = dest_path_error

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    if body is None:
        return {"error": "document has no body element"}

    # 9907df44 -- baseline structural counts, captured BEFORE any mutation
    # (see the matching comment in move_section / _verify_docx_write for why
    # a post-write re-read needs a real baseline, not just the in-memory
    # intent, to compare against).
    baseline_counts = _structural_counts([body])
    baseline_counts["image_count"] = _docx_media_count(raw)

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

    # 9907df44 -- fingerprint the finalized copy (fresh paraIds/bookmark names
    # and repointed REF fields already applied by Pass 1/2 above) and derive
    # the total structural counts the write SHOULD produce, so post-write
    # verification can compare the on-disk result against real expectations
    # instead of trusting copied_block_count blindly.
    expected_hash = _hash_elements(copied_elements)
    copied_counts = _structural_counts(copied_elements)
    expected_counts = {
        key: baseline_counts[key] + copied_counts[key] for key in copied_counts
    }
    expected_counts["image_count"] = baseline_counts["image_count"]

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
        # 9907df44 -- adjust expected counts for the trim: whatever's removed
        # here no longer counts toward the post-write total, and a truthy
        # trim_original_to adds back exactly one (non-heading, non-table)
        # placeholder paragraph.
        removed_counts = _structural_counts(to_remove)
        for key in copied_counts:
            expected_counts[key] -= removed_counts[key]
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
            expected_counts["paragraph_count"] += 1
        trimmed = True

    # 5988a5bb -- hold dest's promotion lock across stage+promote
    # (_save_docx_xml_stdlib, which reentrantly acquires it internally)
    # THROUGH the post-write verify and any conditional restore below,
    # closing the same-process window between promotion and verify/restore
    # entirely (see _docx_promotion_lock's module-level comment).
    with _docx_promotion_lock(dest):
        try:
            transaction = _save_docx_xml_stdlib(raw, root, dest)
        except OSError as exc:
            return {"error": f"could not write {dest}: {exc}"}

        # 9907df44 -- mandatory post-write verification: re-read dest FRESH
        # FROM DISK and confirm the copy actually landed before trusting/
        # reporting success. The copy is located by its own fresh
        # new_heading_para_id rather than a fixed body index, since a subsequent
        # trim of the original section can itself shift indices -- searching by
        # paraId sidesteps needing to re-derive that arithmetic here too. Same
        # abort discipline as the pre-write checks above (real error, no
        # misleading status), except the file has already been written, so
        # best-effort restore it to the pre-write backup first.
        verify_error = _verify_docx_write(
            dest,
            expected_counts=expected_counts,
            expected_hash=expected_hash if new_heading_para_id is not None else None,
            locate_by_paraid=new_heading_para_id,
            expected_len=len(copied_elements),
        )
        if verify_error is not None:
            # 5988a5bb -- do NOT blindly restore: a different (concurrent)
            # writer may have already promoted something newer to dest since
            # our own promotion, in which case this verification "failure"
            # is a false positive and restoring from our own backup would
            # destroy that writer's completed, already-promoted work.
            safe_to_restore, restored, concurrent_write_detected = (
                _safe_restore_after_verification_failure(
                    dest, transaction.get("promoted_sha256") if transaction else None,
                )
            )
            verify_error["file_restored"] = restored
            verify_error["concurrent_write_detected"] = concurrent_write_detected
            if not safe_to_restore:
                if concurrent_write_detected:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- AND a different writer's promotion has landed on "
                        "this file since ours, so this verification failure "
                        "could not be safely auto-corrected: restoring from our "
                        "own backup would destroy that writer's already-promoted "
                        f"work. {dest} was left untouched, exactly as that other "
                        "writer left it -- investigate manually."
                    )
                else:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- this write's own promotion fingerprint is "
                        "unavailable, so it could not be safely confirmed that "
                        "restoring from backup would not destroy a different "
                        f"writer's work; {dest} was left untouched rather than "
                        "risk it -- investigate manually."
                    )
            verify_error["section_id"] = section_id
            verify_error["copied_block_count"] = len(copied_elements)
            return verify_error

        # 679c86f4 -- additionally, the copy must satisfy the image-ownership
        # invariant post-write: every image paragraph in the WHOLE document
        # (original section, its copy, and anything else) must still be
        # immediately followed by its SEQ Figure caption (or be part of a
        # composite whose last member is), and no r:embed relationship may
        # be shared between two independent figure blocks. copy_section's
        # deep-copy pass mints fresh paraIds/bookmark names for the copied
        # range but never rewrites a drawing's r:embed attribute, so copying
        # a section that contains a Figure leaves the original and the copy
        # pointing at the SAME underlying image relationship -- exactly the
        # violation this check exists to catch. Fails closed (pre-write
        # backup restored, subject to the same compare-and-swap
        # concurrent-writer safety as above) unless the caller passed
        # allow_relationship_reuse=True to explicitly declare the reuse
        # intentional.
        ownership_error = _verify_image_ownership(
            dest, allow_relationship_reuse=allow_relationship_reuse
        )
        if ownership_error is not None:
            safe_to_restore, restored, concurrent_write_detected = (
                _safe_restore_after_verification_failure(
                    dest, transaction.get("promoted_sha256") if transaction else None,
                )
            )
            ownership_error["file_restored"] = restored
            ownership_error["concurrent_write_detected"] = concurrent_write_detected
            if not safe_to_restore:
                if concurrent_write_detected:
                    ownership_error["error"] = (
                        ownership_error["error"]
                        + " -- AND a different writer's promotion has landed on "
                        "this file since ours, so this verification failure "
                        "could not be safely auto-corrected: restoring from our "
                        "own backup would destroy that writer's already-promoted "
                        f"work. {dest} was left untouched, exactly as that other "
                        "writer left it -- investigate manually."
                    )
                else:
                    ownership_error["error"] = (
                        ownership_error["error"]
                        + " -- this write's own promotion fingerprint is "
                        "unavailable, so it could not be safely confirmed that "
                        "restoring from backup would not destroy a different "
                        f"writer's work; {dest} was left untouched rather than "
                        "risk it -- investigate manually."
                    )
            ownership_error["section_id"] = section_id
            ownership_error["copied_block_count"] = len(copied_elements)
            return ownership_error

    # fe989980 -- in draft mode, docx_path was never touched; its sidecar
    # index is still accurate, so skip invalidating it.
    if not draft_output_path:
        _invalidate_sidecar_mtime(index_db_path)

    renumber_result = renumber_sequences(dest, index_db_path=index_db_path)

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
        "docx_path": dest,
        "wave_run_id": wave_run_id,
        "is_draft": bool(draft_output_path),
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

def relocate_figure(
    docx_path: str,
    figure_index: int,
    destination_anchor_para_id: str,
    destination_position: str = "after",
    index_db_path: str | None = None,
    allow_bookmark_split: bool = False,
    draft_output_path: str | None = None,
    wave_run_id: str | None = None,
) -> dict[str, Any]:
    """Relocate one image paragraph together with its immediately following Figure caption.

    The source is selected by the same 1-based image order exposed by
    find_image_paragraph. The operation is deliberately strict: the image
    must be a direct body paragraph and the next body child must contain
    a SEQ Figure field. This prevents accidentally detaching a caption
    or moving an image out of a table cell.

    The two existing body elements are moved as one live OOXML range, so image
    relationship IDs, drawing properties, paragraph IDs, bookmarks, and
    caption formatting are preserved verbatim. The operation gates bookmark
    splits before writing, verifies the saved document from disk, invalidates
    the local structure sidecar, and runs renumber_sequences so Figure
    SEQ caches and REF display text remain correct after the reorder.

    679c86f4 -- after :func:`_verify_docx_write`'s structural-count/content-hash
    check passes, the saved document is additionally re-read and checked
    against the image-ownership invariant (:func:`_verify_image_ownership`):
    the moved image paragraph must still be immediately followed by its SEQ
    Figure caption, and its ``r:embed`` relationship must not be shared with
    an independent figure block elsewhere in the document. On either check's
    failure the pre-write backup is restored (subject to the same
    compare-and-swap concurrent-writer safety as the rest of this module)
    and an error is returned instead of a false success payload.

    Returns {status, figure_index, moved_block_count, image_para_id,
    caption_para_id, new_body_index, renumber_sequences, docx_path,
    wave_run_id, is_draft}, or an {"error": ...} result with the source
    document untouched for validation and pre-write safety failures.
    ``docx_path`` in the result is the file actually written --
    ``draft_output_path`` when given (fe989980; requires ``wave_run_id`` too
    -- see :func:`move_section`), else the input ``docx_path`` (unchanged
    legacy behavior).
    """
    if destination_position not in ("before", "after"):
        return {
            "error": (
                "destination_position must be 'before' or 'after', "
                f"got {destination_position!r}"
            )
        }
    if (
        not isinstance(figure_index, int)
        or isinstance(figure_index, bool)
        or figure_index < 1
    ):
        return {
            "error": (
                "figure_index must be a positive 1-based int, "
                f"got {figure_index!r}"
            )
        }

    dest_path_error = _resolve_draft_dest(docx_path, draft_output_path, wave_run_id)
    if isinstance(dest_path_error, dict):
        return dest_path_error
    dest = dest_path_error

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
    w_p = _q(_W, "p")
    w14_para_id = _q(_W14, "paraId")
    w_drawing = _q(_W, "drawing")
    w_pict = _q(_W, "pict")
    w_fld_simple = _q(_W, "fldSimple")
    w_instr = _q(_W, "instr")
    w_instr_text = _q(_W, "instrText")

    def _has_image(paragraph: ET.Element) -> bool:
        return (
            paragraph.find(f".//{w_drawing}") is not None
            or paragraph.find(f".//{w_pict}") is not None
        )

    def _has_figure_seq(paragraph: ET.Element) -> bool:
        for field in paragraph.iter(w_fld_simple):
            if _SEQ_FIGURE_RE.search(field.get(w_instr) or ""):
                return True
        for instr in paragraph.iter(w_instr_text):
            if _SEQ_FIGURE_RE.search("".join(instr.itertext())):
                return True
        return False

    image_indices = [
        i for i, child in enumerate(body_list)
        if child.tag == w_p and _has_image(child)
    ]
    if figure_index > len(image_indices):
        return {
            "error": (
                f"figure_index {figure_index} is out of range: document has "
                f"{len(image_indices)} direct-body image paragraph(s)"
            )
        }

    source_idx = image_indices[figure_index - 1]
    caption_idx = source_idx + 1
    if caption_idx >= len(body_list) or body_list[caption_idx].tag != w_p:
        return {
            "error": (
                f"figure {figure_index} image paragraph at body index {source_idx} "
                "is not immediately followed by a paragraph caption"
            )
        }
    caption_el = body_list[caption_idx]
    if not _has_figure_seq(caption_el):
        return {
            "error": (
                f"figure {figure_index} image paragraph at body index {source_idx} "
                "is not immediately followed by a SEQ Figure caption"
            )
        }

    dest_result = _find_para_by_id(root, destination_anchor_para_id)
    if dest_result is None:
        return {
            "error": (
                f"para_id {destination_anchor_para_id!r} not found in {docx_path}"
            )
        }
    _dbody, _delem, dest_idx = dest_result
    if source_idx <= dest_idx < caption_idx + 1:
        return {
            "error": (
                "destination_anchor_para_id resolves inside the figure block "
                "(image + caption); choose an anchor outside it"
            )
        }

    dest_section_bounds = (
        _locate_section_bounds(body, destination_anchor_para_id)
        if destination_position == "after"
        else None
    )

    split_bookmarks = _bookmarks_split_by_range(
        body_list, source_idx, caption_idx + 1
    )
    if split_bookmarks and not allow_bookmark_split:
        return {
            "error": (
                f"aborting relocate_figure: moving figure {figure_index} would "
                f"split bookmark(s) {split_bookmarks!r} across the move boundary "
                "(their w:bookmarkStart and w:bookmarkEnd would end up in two "
                "disconnected parts of the document) -- pass "
                "allow_bookmark_split=True to force the move anyway"
            ),
            "split_bookmarks": split_bookmarks,
        }

    baseline_counts = _structural_counts([body])
    baseline_counts["image_count"] = _docx_media_count(raw)
    moved_elements = body_list[source_idx:caption_idx + 1]
    expected_hash = _hash_elements(moved_elements)
    removed_count = len(moved_elements)

    for element in moved_elements:
        body.remove(element)

    def _shift(index: int) -> int:
        if index >= caption_idx + 1:
            return index - removed_count
        if index >= source_idx:
            return source_idx
        return index

    if dest_section_bounds is not None:
        _dest_start_idx, dest_end_idx, _dest_heading_text, _dest_level = (
            dest_section_bounds
        )
        insert_at = _shift(dest_end_idx)
    else:
        dest_idx_after = _shift(dest_idx)
        insert_at = (
            dest_idx_after
            if destination_position == "before"
            else dest_idx_after + 1
        )

    for offset, element in enumerate(moved_elements):
        body.insert(insert_at + offset, element)

    # 5988a5bb -- hold dest's promotion lock across stage+promote
    # (_save_docx_xml_stdlib, which reentrantly acquires it internally)
    # THROUGH the post-write verify and any conditional restore below,
    # closing the same-process window between promotion and verify/restore
    # entirely (see _docx_promotion_lock's module-level comment).
    with _docx_promotion_lock(dest):
        try:
            transaction = _save_docx_xml_stdlib(raw, root, dest)
        except OSError as exc:
            return {"error": f"could not write {dest}: {exc}"}

        verify_error = _verify_docx_write(
            dest,
            expected_counts=baseline_counts,
            expected_hash=expected_hash,
            expected_range=(insert_at, insert_at + removed_count),
        )
        if verify_error is not None:
            # 5988a5bb -- do NOT blindly restore: a different (concurrent)
            # writer may have already promoted something newer to dest since
            # our own promotion, in which case this verification "failure"
            # is a false positive and restoring from our own backup would
            # destroy that writer's completed, already-promoted work.
            safe_to_restore, restored, concurrent_write_detected = (
                _safe_restore_after_verification_failure(
                    dest, transaction.get("promoted_sha256") if transaction else None,
                )
            )
            verify_error["file_restored"] = restored
            verify_error["concurrent_write_detected"] = concurrent_write_detected
            if not safe_to_restore:
                if concurrent_write_detected:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- AND a different writer's promotion has landed on "
                        "this file since ours, so this verification failure "
                        "could not be safely auto-corrected: restoring from our "
                        "own backup would destroy that writer's already-promoted "
                        f"work. {dest} was left untouched, exactly as that other "
                        "writer left it -- investigate manually."
                    )
                else:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- this write's own promotion fingerprint is "
                        "unavailable, so it could not be safely confirmed that "
                        "restoring from backup would not destroy a different "
                        f"writer's work; {dest} was left untouched rather than "
                        "risk it -- investigate manually."
                    )
            verify_error["figure_index"] = figure_index
            verify_error["moved_block_count"] = removed_count
            return verify_error

        # 679c86f4 -- additionally, the moved figure block must still satisfy
        # the image-ownership invariant post-write: the image paragraph must
        # remain immediately followed by its SEQ Figure caption (or be part
        # of a composite whose last member is), and no r:embed relationship
        # may now be shared with an independent figure block elsewhere in
        # the document. relocate_figure moves the SAME live elements as one
        # atomic pair rather than duplicating them, so a legitimate move can
        # never itself introduce a relationship duplicate or detach the
        # caption -- a violation here means the move produced a genuinely
        # broken document, not an expected/allowed shape, so no
        # allow_relationship_reuse escape hatch is offered.
        ownership_error = _verify_image_ownership(dest)
        if ownership_error is not None:
            safe_to_restore, restored, concurrent_write_detected = (
                _safe_restore_after_verification_failure(
                    dest, transaction.get("promoted_sha256") if transaction else None,
                )
            )
            ownership_error["file_restored"] = restored
            ownership_error["concurrent_write_detected"] = concurrent_write_detected
            if not safe_to_restore:
                if concurrent_write_detected:
                    ownership_error["error"] = (
                        ownership_error["error"]
                        + " -- AND a different writer's promotion has landed on "
                        "this file since ours, so this verification failure "
                        "could not be safely auto-corrected: restoring from our "
                        "own backup would destroy that writer's already-promoted "
                        f"work. {dest} was left untouched, exactly as that other "
                        "writer left it -- investigate manually."
                    )
                else:
                    ownership_error["error"] = (
                        ownership_error["error"]
                        + " -- this write's own promotion fingerprint is "
                        "unavailable, so it could not be safely confirmed that "
                        "restoring from backup would not destroy a different "
                        f"writer's work; {dest} was left untouched rather than "
                        "risk it -- investigate manually."
                    )
            ownership_error["figure_index"] = figure_index
            ownership_error["moved_block_count"] = removed_count
            return ownership_error

    # fe989980 -- in draft mode, docx_path was never touched; its sidecar
    # index is still accurate, so skip invalidating it.
    if not draft_output_path:
        _invalidate_sidecar_mtime(index_db_path)
    renumber_result = renumber_sequences(
        dest, index_db_path=index_db_path
    )

    image_para_id = body_list[source_idx].get(w14_para_id)
    caption_para_id = body_list[caption_idx].get(w14_para_id)
    return {
        "status": "moved",
        "figure_index": figure_index,
        "moved_block_count": removed_count,
        "image_para_id": image_para_id,
        "caption_para_id": caption_para_id,
        "new_body_index": insert_at,
        "destination_anchor_para_id": destination_anchor_para_id,
        "destination_position": destination_position,
        "renumber_sequences": renumber_result,
        "docx_path": dest,
        "wave_run_id": wave_run_id,
        "is_draft": bool(draft_output_path),
    }

def relocate_table(
    docx_path: str,
    table_index: int,
    destination_anchor_para_id: str,
    destination_position: str = "after",
    index_db_path: str | None = None,
    allow_bookmark_split: bool = False,
    draft_output_path: str | None = None,
    wave_run_id: str | None = None,
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
        draft_output_path:            fe989980 -- same opt-in wave-scoped
                                      draft mode as :func:`move_section`: when
                                      given (with ``wave_run_id``), the move
                                      is written to this ISOLATED path instead
                                      of ``docx_path``, which is only ever
                                      read. Omitted (the default), behavior
                                      is byte-identical to pre-fe989980.
        wave_run_id:                  fe989980 -- required together with
                                      ``draft_output_path``; see
                                      :func:`move_section`.

    Returns:
        ``{status, table_index, new_table_index, row_count, col_count,
        destination_anchor_para_id, destination_position, docx_path,
        wave_run_id, is_draft}``. ``docx_path`` in the result is the file
        actually written (``draft_output_path`` when given, else the input
        ``docx_path``).

        ``{"error": <message>}`` when ``table_index`` is out of range or does
        not identify a ``<w:tbl>``, ``destination_anchor_para_id`` can't be
        resolved, the destination falls on/inside the table being moved, or
        the move would split a bookmark (and ``allow_bookmark_split`` is not
        set) -- the file is NOT mutated in any of these cases.

        9907df44 -- after a successful write, mandatory post-write
        verification re-reads the file from disk and compares structural
        counts + a content hash of the relocated table against what the move
        should have produced. On mismatch, returns ``{"error": <message>,
        "count_mismatches": {...}, "content_hash_mismatch": {...} | None,
        "file_restored": <bool>}`` instead of a success payload -- the file
        is best-effort restored from the pre-write ``.bak`` backup first.
    """
    if destination_position not in ("before", "after"):
        return {
            "error": f"destination_position must be 'before' or 'after', got {destination_position!r}"
        }

    dest_path_error = _resolve_draft_dest(docx_path, draft_output_path, wave_run_id)
    if isinstance(dest_path_error, dict):
        return dest_path_error
    dest = dest_path_error

    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}

    body = root.find(_q(_W, "body"))
    if body is None:
        return {"error": "document has no body element"}

    # 9907df44 -- baseline structural counts, captured BEFORE any mutation
    # (see the matching comment in move_section / _verify_docx_write): a bare
    # table relocate never adds/removes a paragraph, heading, table, or media
    # part, so these totals must be identical after the write.
    baseline_counts = _structural_counts([body])
    baseline_counts["image_count"] = _docx_media_count(raw)

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

    # 9907df44 -- fingerprint the exact <w:tbl> being relocated BEFORE it's
    # cut, so post-write verification can confirm this SAME table (verbatim
    # w:tblPr/w:tblGrid/cell content) actually landed at the destination.
    expected_hash = _hash_elements([target_el])

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

    # 5988a5bb -- hold dest's promotion lock across stage+promote
    # (_save_docx_xml_stdlib, which reentrantly acquires it internally)
    # THROUGH the post-write verify and any conditional restore below,
    # closing the same-process window between promotion and verify/restore
    # entirely (see _docx_promotion_lock's module-level comment).
    with _docx_promotion_lock(dest):
        try:
            transaction = _save_docx_xml_stdlib(raw, root, dest)
        except OSError as exc:
            return {"error": f"could not write {dest}: {exc}"}

        # 9907df44 -- mandatory post-write verification: re-read dest FRESH
        # FROM DISK and confirm the table actually landed at insert_at before
        # trusting/reporting the move as a success. Same abort discipline as the
        # pre-write bookmark-split check above (real error, no misleading
        # status), except the file has already been written, so best-effort
        # restore it to the pre-write backup first.
        verify_error = _verify_docx_write(
            dest,
            expected_counts=baseline_counts,
            expected_hash=expected_hash,
            expected_range=(insert_at, insert_at + 1),
        )
        if verify_error is not None:
            # 5988a5bb -- do NOT blindly restore: a different (concurrent)
            # writer may have already promoted something newer to dest since
            # our own promotion, in which case this verification "failure"
            # is a false positive and restoring from our own backup would
            # destroy that writer's completed, already-promoted work.
            safe_to_restore, restored, concurrent_write_detected = (
                _safe_restore_after_verification_failure(
                    dest, transaction.get("promoted_sha256") if transaction else None,
                )
            )
            verify_error["file_restored"] = restored
            verify_error["concurrent_write_detected"] = concurrent_write_detected
            if not safe_to_restore:
                if concurrent_write_detected:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- AND a different writer's promotion has landed on "
                        "this file since ours, so this verification failure "
                        "could not be safely auto-corrected: restoring from our "
                        "own backup would destroy that writer's already-promoted "
                        f"work. {dest} was left untouched, exactly as that other "
                        "writer left it -- investigate manually."
                    )
                else:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- this write's own promotion fingerprint is "
                        "unavailable, so it could not be safely confirmed that "
                        "restoring from backup would not destroy a different "
                        f"writer's work; {dest} was left untouched rather than "
                        "risk it -- investigate manually."
                    )
            verify_error["table_index"] = table_index
            return verify_error

    # fe989980 -- in draft mode, docx_path was never touched; its sidecar
    # index is still accurate, so skip invalidating it.
    if not draft_output_path:
        _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "moved",
        "table_index": table_index,
        "new_table_index": insert_at,
        "row_count": table_meta["row_count"],
        "col_count": table_meta["col_count"],
        "destination_anchor_para_id": destination_anchor_para_id,
        "destination_position": destination_position,
        "docx_path": dest,
        "wave_run_id": wave_run_id,
        "is_draft": bool(draft_output_path),
    }


# ---------------------------------------------------------------------------
# a2cd9f54 -- safe structural table edit primitives: insert_column,
# split_cell, transpose_table.
#
# Word's native table model represents a "column" only implicitly, through
# w:tblGrid (the column-boundary ruler) plus each row's own w:tc children,
# whose horizontal extent is governed by w:gridSpan (default 1, meaning "no
# horizontal merge") and whose VERTICAL continuation is governed by
# w:vMerge ("restart" starts a merge group, an unadorned/"continue" value
# extends it). There is no single authoritative "cell (row, col)" address
# the format hands you directly -- every primitive below derives it by
# walking a row and accumulating gridSpan (see _row_cell_spans).
#
# Scope (explicit, matching this item's "explicit ambiguity and
# unsupported-merge failures" requirement rather than a general-purpose
# merge-aware rewrite of arbitrary table geometry):
#
#   * insert_column -- supported for ANY table, including ones that already
#     contain gridSpan/vMerge, AS LONG AS every row's cells consistently
#     account for the table's declared grid-column count (a genuinely
#     malformed/ambiguous table is refused with reason="ambiguous_grid").
#     For each row, the insertion point either (a) lands strictly INSIDE an
#     existing horizontally-merged cell's span -- its gridSpan is simply
#     incremented, so the new column becomes part of that merge, matching
#     what Word itself does when you insert a column through a merged
#     region -- or (b) lands on a cell boundary -- a brand-new, empty cell
#     is spliced in for that row.
#   * split_cell -- supported for the TARGET cell only when it has no
#     gridSpan>1 and no w:vMerge of its own (neither "restart" nor
#     "continue"); a target that is already merged is refused with
#     reason="unsupported_merge" rather than guessing how a split should
#     interact with an existing merge. A column-split (cols>1) reuses the
#     exact same grid-widening engine insert_column uses (applied cols-1
#     times) so every OTHER row in the table stays grid-consistent. A
#     row-split (rows>1) inserts brand-new <w:tr> rows immediately after the
#     target row: the split cell's own column(s) get independent new
#     content in each new row, while every OTHER cell in the target row
#     grows a w:vMerge spanning the new rows so the table stays visually
#     rectangular.
#   * transpose_table -- supported ONLY for a fully rectangular table with
#     NO gridSpan>1 and NO w:vMerge anywhere (every row has exactly the same
#     number of plain, unmerged cells). Any merged cell makes the correct
#     transposed merge geometry genuinely ambiguous (a horizontal merge does
#     not have one canonical vertical-merge equivalent), so this refuses
#     rather than risk silently producing a wrong table --
#     reason="unsupported_merge". Row heights and column widths also have no
#     canonical semantic mapping under a transpose; the new w:tblGrid falls
#     back to the table's original total width divided evenly across the
#     new column count -- an honest, documented default, not a fabricated
#     precise one.
#
# Every write goes through the SAME disposable-copy, byte/zip/XML-integrity
# pipeline the rest of this module uses (_load_docx_xml_stdlib /
# _save_docx_xml_stdlib / _atomic_write_docx_bytes / _verify_docx_write /
# _enforce_render_verification) via the shared _write_table_mutation tail
# below -- never a whole-document native rewrite. Because every mutation
# repositions or clones the SAME live ET.Element cell objects (never
# re-serializes cell content from scratch), everything already inside a
# cell -- paragraph styles, numbering references, bookmarks, run formatting,
# relationship ids (images, hyperlinks) -- survives verbatim. Brand-new
# cells get exactly one fresh, empty <w:p> with its own newly-minted,
# document-unique w14:paraId (see _existing_para_ids / _new_para_id).
#
# Known, documented scope limitation: unlike move_section / copy_section /
# relocate_table, these three primitives do not (yet) support the
# draft_output_path / wave_run_id isolated-draft mode -- they always mutate
# docx_path in place. Adding draft-mode support is a mechanical follow-up
# (thread the same _resolve_draft_dest call these other primitives use),
# not attempted here to keep this item's surface reviewable.
# ---------------------------------------------------------------------------

_W_TBL = _q(_W, "tbl")
_W_TR = _q(_W, "tr")
_W_TC = _q(_W, "tc")
_W_TBLGRID = _q(_W, "tblGrid")
_W_GRIDCOL = _q(_W, "gridCol")
_W_TCPR = _q(_W, "tcPr")
_W_TCW = _q(_W, "tcW")
_W_GRIDSPAN = _q(_W, "gridSpan")
_W_VMERGE = _q(_W, "vMerge")

# CT_TcPr's fixed child-element sequence (ECMA-376 Part 1, SS17.4.70) -- used
# to insert a NEW gridSpan/vMerge element at a schema-valid position rather
# than blindly appending, which could produce an XML document Word rejects.
_TCPR_CHILD_ORDER = (
    "cnfStyle", "tcW", "gridSpan", "hMerge", "vMerge", "tcBorders", "shd",
    "noWrap", "tcMar", "textDirection", "tcFitText", "vAlign", "hideMark",
    "cellIns", "cellDel", "cellMerge",
)


def _resolve_table_element(
    body: ET.Element, table_index: Any
) -> tuple[ET.Element, None] | tuple[None, dict[str, Any]]:
    """Validate ``table_index`` against ``body`` and return ``(tbl, None)``
    on success or ``(None, error_dict)`` -- the same 0-based body-child
    addressing scheme :func:`relocate_table` uses."""
    body_list = list(body)
    if not isinstance(table_index, int) or isinstance(table_index, bool) or table_index < 0:
        return None, {"error": f"table_index must be a non-negative int, got {table_index!r}"}
    if table_index >= len(body_list):
        return None, {
            "error": (
                f"table_index {table_index} out of range -- document body has "
                f"{len(body_list)} top-level children"
            )
        }
    target_el = body_list[table_index]
    if target_el.tag != _W_TBL:
        found_tag = target_el.tag.rsplit("}", 1)[-1]
        return None, {
            "error": f"body child at index {table_index} is not a <w:tbl> (found <{found_tag}>)"
        }
    return target_el, None


def _table_rows(tbl: ET.Element) -> list[ET.Element]:
    return list(tbl.findall(_W_TR))


def _cell_grid_span(tc: ET.Element) -> int:
    tcPr = tc.find(_W_TCPR)
    if tcPr is None:
        return 1
    gridSpan = tcPr.find(_W_GRIDSPAN)
    if gridSpan is None:
        return 1
    try:
        return max(1, int(gridSpan.get(_q(_W, "val"), "1")))
    except (TypeError, ValueError):
        return 1


def _cell_vmerge(tc: ET.Element) -> str | None:
    """``"restart"`` / ``"continue"`` / ``None`` (no w:vMerge at all).

    A ``<w:vMerge/>`` with no ``w:val`` attribute means ``"continue"`` per
    ECMA-376 -- only ``w:val="restart"`` is ever spelled out explicitly.
    """
    tcPr = tc.find(_W_TCPR)
    if tcPr is None:
        return None
    vMerge = tcPr.find(_W_VMERGE)
    if vMerge is None:
        return None
    return vMerge.get(_q(_W, "val"), "continue")


def _row_cell_spans(tr: ET.Element) -> list[tuple[ET.Element, int, int]]:
    """``[(tc, start_col, span), ...]`` for one row, in document order."""
    out: list[tuple[ET.Element, int, int]] = []
    col = 0
    for tc in tr.findall(_W_TC):
        span = _cell_grid_span(tc)
        out.append((tc, col, span))
        col += span
    return out


def _table_grid_col_count(tbl: ET.Element) -> int | None:
    grid = tbl.find(_W_TBLGRID)
    if grid is None:
        return None
    cols = grid.findall(_W_GRIDCOL)
    return len(cols) if cols else None


def _validate_uniform_row_spans(tbl: ET.Element, grid_col_count: int) -> list[str]:
    """Human-readable problems (empty == consistent): every row's cells must
    account for EXACTLY ``grid_col_count`` grid columns. A row that over- or
    under-shoots is a malformed-or-ambiguous table this module refuses to
    guess about (see reason="ambiguous_grid" on the public functions)."""
    problems: list[str] = []
    for row_idx, tr in enumerate(_table_rows(tbl)):
        total = sum(span for _tc, _start, span in _row_cell_spans(tr))
        if total != grid_col_count:
            problems.append(
                f"row {row_idx} spans {total} grid column(s), expected {grid_col_count}"
            )
    return problems


def _gridcol_width(tbl: ET.Element, index: int) -> int | None:
    grid = tbl.find(_W_TBLGRID)
    if grid is None:
        return None
    cols = grid.findall(_W_GRIDCOL)
    if not (0 <= index < len(cols)):
        return None
    raw = cols[index].get(_q(_W, "w"))
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _tc_width(tc: ET.Element) -> int | None:
    tcPr = tc.find(_W_TCPR)
    if tcPr is None:
        return None
    tcW = tcPr.find(_W_TCW)
    if tcW is None:
        return None
    raw = tcW.get(_q(_W, "w"))
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _ensure_tblgrid(tbl: ET.Element, col_count: int) -> ET.Element:
    """Return ``tbl``'s ``<w:tblGrid>``, creating one with ``col_count``
    width-less ``<w:gridCol>`` entries first if it doesn't exist yet (a real
    Word-authored .docx always has one; this only matters for a hand-built
    fixture)."""
    grid = tbl.find(_W_TBLGRID)
    if grid is not None:
        return grid
    grid = ET.Element(_W_TBLGRID)
    tblPr = tbl.find(_q(_W, "tblPr"))
    tbl.insert((list(tbl).index(tblPr) + 1) if tblPr is not None else 0, grid)
    for _ in range(col_count):
        ET.SubElement(grid, _W_GRIDCOL)
    return grid


def _tcpr_insert_index(tcPr: ET.Element, tag_name: str) -> int:
    """Index at which to insert a new ``tag_name`` child into ``tcPr`` to
    respect CT_TcPr's fixed element sequence (see ``_TCPR_CHILD_ORDER``)."""
    try:
        target_rank = _TCPR_CHILD_ORDER.index(tag_name)
    except ValueError:  # pragma: no cover -- defensive; every caller here is a known tag
        return len(list(tcPr))
    for i, child in enumerate(tcPr):
        local = child.tag.rsplit("}", 1)[-1]
        try:
            rank = _TCPR_CHILD_ORDER.index(local)
        except ValueError:
            continue
        if rank > target_rank:
            return i
    return len(list(tcPr))


def _set_grid_span(tc: ET.Element, span: int) -> None:
    """Set (``span`` > 1) or remove (``span`` <= 1) ``tc``'s ``w:gridSpan``,
    preserving CT_TcPr element ordering and creating ``w:tcPr`` on demand."""
    tcPr = tc.find(_W_TCPR)
    if tcPr is None:
        if span <= 1:
            return
        tcPr = ET.Element(_W_TCPR)
        tc.insert(0, tcPr)
    gridSpan = tcPr.find(_W_GRIDSPAN)
    if span <= 1:
        if gridSpan is not None:
            tcPr.remove(gridSpan)
        return
    if gridSpan is None:
        gridSpan = ET.Element(_W_GRIDSPAN)
        tcPr.insert(_tcpr_insert_index(tcPr, "gridSpan"), gridSpan)
    gridSpan.set(_q(_W, "val"), str(span))


def _set_vmerge(tc: ET.Element, mode: str | None) -> None:
    """Set (``"restart"`` / ``"continue"``) or remove (``None``) ``tc``'s
    ``w:vMerge``, preserving CT_TcPr element ordering."""
    tcPr = tc.find(_W_TCPR)
    if tcPr is None:
        if mode is None:
            return
        tcPr = ET.Element(_W_TCPR)
        tc.insert(0, tcPr)
    vMerge = tcPr.find(_W_VMERGE)
    if mode is None:
        if vMerge is not None:
            tcPr.remove(vMerge)
        return
    if vMerge is None:
        vMerge = ET.Element(_W_VMERGE)
        tcPr.insert(_tcpr_insert_index(tcPr, "vMerge"), vMerge)
    if mode == "restart":
        vMerge.set(_q(_W, "val"), "restart")
    elif _q(_W, "val") in vMerge.attrib:
        del vMerge.attrib[_q(_W, "val")]


def _new_empty_tc(width: int | None, taken_ids: set[str]) -> ET.Element:
    """Brand-new, empty ``<w:tc>``: one ``<w:tcPr>`` (with ``w:tcW`` when
    ``width`` is known) plus one empty ``<w:p>`` carrying a freshly-minted,
    never-before-seen ``w14:paraId`` (see ``_new_para_id``)."""
    tc = ET.Element(_W_TC)
    if width is not None:
        tcPr = ET.SubElement(tc, _W_TCPR)
        ET.SubElement(tcPr, _W_TCW, {_q(_W, "w"): str(width), _q(_W, "type"): "dxa"})
    p = ET.SubElement(tc, _q(_W, "p"))
    p.set(_q(_W14, "paraId"), _new_para_id(taken_ids))
    return tc


def _new_continuation_tc(width: int | None, taken_ids: set[str]) -> ET.Element:
    """A brand-new ``<w:tc>`` marked ``w:vMerge`` (continue) -- a vertical-
    merge placeholder cell for a row inserted by a row-split."""
    tc = _new_empty_tc(width, taken_ids)
    _set_vmerge(tc, "continue")
    return tc


def _insert_grid_columns(
    tbl: ET.Element,
    insertion_col: int,
    count: int,
    default_width: int | None,
    *,
    skip_row: ET.Element | None = None,
    taken_ids: set[str] | None = None,
) -> int:
    """Insert ``count`` new grid columns at ``insertion_col`` into ``tbl``'s
    ``w:tblGrid`` AND, for every row except ``skip_row``, either widen a
    straddling cell's ``gridSpan`` or splice in a brand-new empty cell at
    the boundary -- the shared engine behind both :func:`insert_column` and
    :func:`split_cell`'s column-split mode. Caller must ensure ``tbl`` already
    has a ``w:tblGrid`` (see :func:`_ensure_tblgrid`). Returns the number of
    brand-new ``<w:p>`` paragraphs created (one per newly-spliced cell)."""
    if count <= 0:
        return 0
    new_paragraphs = 0
    ids = taken_ids if taken_ids is not None else _existing_para_ids(tbl)
    grid = tbl.find(_W_TBLGRID)

    for offset in range(count):
        col = insertion_col + offset
        for tr in _table_rows(tbl):
            if tr is skip_row:
                continue
            spans = _row_cell_spans(tr)
            straddling = next((tc for tc, s, sp in spans if s < col < s + sp), None)
            if straddling is not None:
                _set_grid_span(straddling, _cell_grid_span(straddling) + 1)
                continue
            tc_children = list(tr.findall(_W_TC))
            insert_at = next(
                (i for i, (_tc, s, _sp) in enumerate(spans) if s >= col), len(tc_children)
            )
            new_tc = _new_empty_tc(default_width, ids)
            if insert_at >= len(tc_children):
                tr.append(new_tc)
            else:
                anchor = tc_children[insert_at]
                tr_children = list(tr)
                tr.insert(tr_children.index(anchor), new_tc)
            new_paragraphs += 1

        if grid is not None:
            new_gridcol = ET.Element(_W_GRIDCOL)
            if default_width is not None:
                new_gridcol.set(_q(_W, "w"), str(default_width))
            grid_cols = grid.findall(_W_GRIDCOL)
            if col >= len(grid_cols):
                grid.append(new_gridcol)
            else:
                grid_children = list(grid)
                grid.insert(grid_children.index(grid_cols[col]), new_gridcol)

    return new_paragraphs


def _split_rows(
    target_el: ET.Element,
    target_row: ET.Element,
    target_cells: list[ET.Element],
    start_col: int,
    extra_rows: int,
    taken_ids: set[str],
) -> int:
    """Insert ``extra_rows`` new ``<w:tr>`` immediately after ``target_row``.
    The cell(s) in ``target_cells`` get brand-new, independent content in
    each new row (the actual split-into-rows result). Every OTHER cell in
    ``target_row`` grows a ``w:vMerge`` spanning the new rows (promoted to
    ``"restart"`` if it wasn't already part of a merge; left untouched if it
    already was one, correctly extending that existing merge) so the table
    stays visually rectangular. Returns the number of brand-new ``<w:p>``
    paragraphs created."""
    new_paragraphs = 0

    sibling_spans = [
        (tc, s, sp) for tc, s, sp in _row_cell_spans(target_row) if tc not in target_cells
    ]
    for tc, _s, _sp in sibling_spans:
        if _cell_vmerge(tc) is None:
            _set_vmerge(tc, "restart")

    insert_after = target_row
    for _ in range(extra_rows):
        new_tr = ET.Element(_W_TR)
        ordered: list[tuple[int, ET.Element]] = []
        for tc, s, sp in sibling_spans:
            placeholder = _new_continuation_tc(_tc_width(tc), taken_ids)
            _set_grid_span(placeholder, sp)
            ordered.append((s, placeholder))
            new_paragraphs += 1
        for offset, tc in enumerate(target_cells):
            fresh = _new_empty_tc(_tc_width(tc), taken_ids)
            ordered.append((start_col + offset, fresh))
            new_paragraphs += 1
        ordered.sort(key=lambda item: item[0])
        for _pos, tc in ordered:
            new_tr.append(tc)

        siblings_of_target_el = list(target_el)
        insert_idx = siblings_of_target_el.index(insert_after) + 1
        target_el.insert(insert_idx, new_tr)
        insert_after = new_tr

    return new_paragraphs


def _write_table_mutation(
    *,
    docx_path: str,
    raw: bytes,
    root: ET.Element,
    target_el: ET.Element,
    table_index: int,
    expected_counts: dict[str, int],
    expected_hash: str,
    index_db_path: str | None,
    allow_degraded_render: bool,
    degraded_render_reason: str | None,
) -> dict[str, Any]:
    """Shared stage -> verify -> render-gate -> promote tail for the table
    structural-edit primitives (insert_column / split_cell / transpose_table)
    -- identical discipline to relocate_table / insert_caption (see their
    docstrings), factored out since all three share it verbatim. Returns
    ``{"docx_path": ..., "render_info": {...}}`` on success, or an error
    dict (already carrying ``"error"``, ``"file_restored"``,
    ``"concurrent_write_detected"``, ``"table_index"``, ``"docx_path"``) on
    failure -- the caller can layer its own identifying fields on top and
    return it verbatim."""
    with _docx_promotion_lock(docx_path):
        try:
            transaction = _save_docx_xml_stdlib(raw, root, docx_path)
        except OSError as exc:
            return {"error": f"could not write {docx_path}: {exc}"}

        promoted_sha256 = transaction.get("promoted_sha256") if transaction else None

        verify_error = _verify_docx_write(
            docx_path,
            expected_counts=expected_counts,
            expected_hash=expected_hash,
            expected_range=(table_index, table_index + 1),
        )
        if verify_error is not None:
            safe_to_restore, restored, concurrent_write_detected = (
                _safe_restore_after_verification_failure(docx_path, promoted_sha256)
            )
            verify_error["file_restored"] = restored
            verify_error["concurrent_write_detected"] = concurrent_write_detected
            if not safe_to_restore:
                if concurrent_write_detected:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- AND a different writer's promotion has landed on "
                        "this file since ours, so this verification failure "
                        "could not be safely auto-corrected: restoring from our "
                        "own backup would destroy that writer's already-promoted "
                        f"work. {docx_path} was left untouched, exactly as that "
                        "other writer left it -- investigate manually."
                    )
                else:
                    verify_error["error"] = (
                        verify_error["error"]
                        + " -- this write's own promotion fingerprint is "
                        "unavailable, so it could not be safely confirmed that "
                        "restoring from backup would not destroy a different "
                        f"writer's work; {docx_path} was left untouched rather "
                        "than risk it -- investigate manually."
                    )
            verify_error["table_index"] = table_index
            verify_error["docx_path"] = docx_path
            return verify_error

        render_error, render_info = _enforce_render_verification(
            docx_path,
            promoted_sha256=promoted_sha256,
            allow_degraded_render=allow_degraded_render,
            degraded_render_reason=degraded_render_reason,
        )
        if render_error is not None:
            render_error["table_index"] = table_index
            render_error["docx_path"] = docx_path
            return render_error

    _invalidate_sidecar_mtime(index_db_path)
    return {"docx_path": docx_path, "render_info": render_info}


def insert_column(
    docx_path: str,
    table_index: int,
    col_index: int,
    position: str = "before",
    index_db_path: str | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
) -> dict[str, Any]:
    """a2cd9f54 -- Insert a new, empty grid column into an existing table.

    ``col_index`` addresses an existing GRID column (0-based, from
    ``_table_grid_col_count`` / the table's ``w:tblGrid``); ``position``
    ("before", default, or "after") says which side of it the new column
    lands on.

    For each row: if the insertion point falls strictly INSIDE an existing
    horizontally-merged cell's span, that cell's ``w:gridSpan`` is
    incremented (the new column joins the merge) -- otherwise a brand-new,
    empty cell (one empty paragraph, a fresh ``w14:paraId``) is spliced in
    at the boundary. ``w:tblGrid`` always gets exactly one new
    ``w:gridCol``, defaulted to the width of the column at ``col_index``.

    Refuses (file untouched) with ``reason="ambiguous_grid"`` when the
    table's rows do not consistently account for its declared grid-column
    count -- this module never guesses at an inconsistent/malformed table's
    real column addressing.

    After a successful write, mandatory post-write verification (see
    :func:`_write_table_mutation` / :func:`_verify_docx_write`) re-reads the
    file from disk and confirms the exact expected byte content landed --
    the file is best-effort restored from backup on any mismatch. A real
    Word/COM (or LibreOffice) render-capability check also runs
    (:func:`_enforce_render_verification`); see ``allow_degraded_render``.

    Args:
        docx_path:       Absolute path to the .docx file (mutated in place).
        table_index:      0-based body-child position of the ``<w:tbl>``
                          (same addressing as :func:`relocate_table`).
        col_index:        0-based existing grid-column index to insert
                          relative to.
        position:         "before" (default) or "after" ``col_index``.
        index_db_path:    If supplied, sidecar is invalidated after write.
        allow_degraded_render: Explicit, audited opt-in to accept this write
                          when no render backend is available in this
                          environment. Requires ``degraded_render_reason``.
        degraded_render_reason: Required, non-empty when
                          ``allow_degraded_render`` is True.

    Returns:
        ``{status, table_index, col_index, position, grid_col_count,
        row_count, col_count, docx_path, render_status, render_verified,
        render_backend, render_detail}`` or ``{"error": ...}`` (with
        ``"reason"`` one of ``"ambiguous_grid"`` when applicable) on
        failure.
    """
    if position not in ("before", "after"):
        return {"error": f"position must be 'before' or 'after', got {position!r}"}
    if allow_degraded_render and not (
        degraded_render_reason and str(degraded_render_reason).strip()
    ):
        return {
            "error": (
                "degraded_render_reason is required and must be non-empty "
                "when allow_degraded_render=True -- an audited degrade with "
                "no stated reason is not auditable and is refused"
            )
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

    target_el, err = _resolve_table_element(body, table_index)
    if err is not None:
        return err

    rows = _table_rows(target_el)
    grid_col_count = _table_grid_col_count(target_el)
    if grid_col_count is None:
        grid_col_count = max(
            (sum(span for _tc, _s, span in _row_cell_spans(tr)) for tr in rows), default=0
        )
    if grid_col_count <= 0:
        return {"error": "table has no columns to insert relative to"}

    problems = _validate_uniform_row_spans(target_el, grid_col_count)
    if problems:
        return {
            "error": (
                "insert_column refused: this table's rows do not "
                f"consistently account for its {grid_col_count} grid "
                "column(s) -- ambiguous column addressing"
            ),
            "reason": "ambiguous_grid",
            "details": problems,
        }

    if (
        not isinstance(col_index, int)
        or isinstance(col_index, bool)
        or not (0 <= col_index < grid_col_count)
    ):
        return {
            "error": (
                f"col_index must be a non-negative int less than the "
                f"table's {grid_col_count} grid column(s), got {col_index!r}"
            )
        }

    insertion_col = col_index if position == "before" else col_index + 1

    baseline_counts = _structural_counts([body])
    baseline_counts["image_count"] = _docx_media_count(raw)

    taken_ids = _existing_para_ids(root)
    new_width = _gridcol_width(target_el, min(col_index, grid_col_count - 1))
    _ensure_tblgrid(target_el, grid_col_count)

    new_paragraphs_added = _insert_grid_columns(
        target_el, insertion_col, 1, new_width, taken_ids=taken_ids
    )

    expected_counts = dict(baseline_counts)
    expected_counts["paragraph_count"] = baseline_counts["paragraph_count"] + new_paragraphs_added
    expected_hash = _hash_elements([target_el])

    write_result = _write_table_mutation(
        docx_path=docx_path,
        raw=raw,
        root=root,
        target_el=target_el,
        table_index=table_index,
        expected_counts=expected_counts,
        expected_hash=expected_hash,
        index_db_path=index_db_path,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
    )
    if "error" in write_result:
        return write_result

    from ._vendored_content_tree import _table_node  # noqa: PLC0415

    table_meta = _table_node(target_el, table_index)

    return {
        "status": "inserted",
        "table_index": table_index,
        "col_index": col_index,
        "position": position,
        "grid_col_count": grid_col_count + 1,
        "row_count": table_meta["row_count"],
        "col_count": table_meta["col_count"],
        "docx_path": write_result["docx_path"],
        **write_result["render_info"],
    }


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
) -> dict[str, Any]:
    """a2cd9f54 -- Split one table cell into ``cols`` columns and/or ``rows``
    rows.

    ``row_index`` is a 0-based ``<w:tr>`` index; ``col_index`` is the
    target cell's STARTING grid column (from ``_row_cell_spans`` -- pass the
    ``start_col`` a caller already knows from ``document_outline`` /
    ``get_structure_elements`` table metadata, not an arbitrary grid
    position inside a wider merged cell).

    Refuses (file untouched) with ``reason="unsupported_merge"`` when the
    target cell already has ``w:gridSpan`` > 1 or any ``w:vMerge`` --
    splitting an already-merged cell is not attempted. Also refuses with
    ``reason="ambiguous_grid"`` (``cols`` > 1 only) under the same
    inconsistent-table condition :func:`insert_column` checks.

    Column split (``cols`` > 1): the target cell is replaced by ``cols``
    brand-new, independent cells (its width divided evenly, remainder on
    the last one); every OTHER row is widened by ``cols - 1`` grid columns
    via the SAME engine :func:`insert_column` uses (a straddling merged
    cell's ``gridSpan`` grows; otherwise a blank cell is spliced in), so the
    whole table stays grid-consistent.

    Row split (``rows`` > 1): ``rows - 1`` brand-new ``<w:tr>`` are inserted
    immediately after the target row. The split cell's own column(s) get
    independent new content in each new row; every OTHER cell in the target
    row grows a ``w:vMerge`` spanning the new rows (idempotent if it was
    already part of one) so the table stays visually rectangular.

    Args:
        docx_path:       Absolute path to the .docx file (mutated in place).
        table_index:     0-based body-child position of the ``<w:tbl>``.
        row_index:       0-based ``<w:tr>`` index of the target cell.
        col_index:       Target cell's starting grid column.
        cols:            Number of columns to split into (default 1 = no
                          column split).
        rows:             Number of rows to split into (default 1 = no row
                          split). At least one of ``cols``/``rows`` must be
                          > 1.
        index_db_path:    If supplied, sidecar is invalidated after write.
        allow_degraded_render: Same audited opt-in as :func:`insert_column`.
        degraded_render_reason: Required, non-empty when
                          ``allow_degraded_render`` is True.

    Returns:
        ``{status, table_index, row_index, col_index, cols, rows, row_count,
        col_count, docx_path, render_status, render_verified, render_backend,
        render_detail}`` or ``{"error": ...}`` (with ``"reason"`` one of
        ``"unsupported_merge"`` / ``"ambiguous_grid"`` when applicable) on
        failure.
    """
    if not isinstance(cols, int) or isinstance(cols, bool) or cols < 1:
        return {"error": f"cols must be a positive int, got {cols!r}"}
    if not isinstance(rows, int) or isinstance(rows, bool) or rows < 1:
        return {"error": f"rows must be a positive int, got {rows!r}"}
    if cols == 1 and rows == 1:
        return {"error": "split_cell requires cols>1 and/or rows>1 -- nothing to split"}
    if allow_degraded_render and not (
        degraded_render_reason and str(degraded_render_reason).strip()
    ):
        return {
            "error": (
                "degraded_render_reason is required and must be non-empty "
                "when allow_degraded_render=True -- an audited degrade with "
                "no stated reason is not auditable and is refused"
            )
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

    target_el, err = _resolve_table_element(body, table_index)
    if err is not None:
        return err

    table_rows_list = _table_rows(target_el)
    if (
        not isinstance(row_index, int)
        or isinstance(row_index, bool)
        or not (0 <= row_index < len(table_rows_list))
    ):
        return {
            "error": (
                f"row_index must be a non-negative int less than the "
                f"table's {len(table_rows_list)} row(s), got {row_index!r}"
            )
        }

    target_row = table_rows_list[row_index]
    row_spans = _row_cell_spans(target_row)

    hit = next(((tc, s, sp) for tc, s, sp in row_spans if s == col_index), None)
    if hit is None:
        inside = next(
            ((tc, s, sp) for tc, s, sp in row_spans if s < col_index < s + sp), None
        )
        if inside is not None:
            return {
                "error": (
                    f"col_index {col_index} is inside an existing merged "
                    f"cell (grid columns {inside[1]}-{inside[1] + inside[2] - 1}) "
                    "-- address a split by the cell's STARTING grid column"
                ),
                "reason": "unsupported_merge",
            }
        return {
            "error": f"col_index {col_index} does not address any cell in row {row_index}"
        }

    target_tc, start_col, span = hit
    if span > 1:
        return {
            "error": (
                f"cannot split cell at row {row_index}, col {col_index}: it "
                f"already spans {span} grid column(s) (w:gridSpan) -- "
                "splitting an already-merged cell is not supported"
            ),
            "reason": "unsupported_merge",
        }
    if _cell_vmerge(target_tc) is not None:
        return {
            "error": (
                f"cannot split cell at row {row_index}, col {col_index}: it "
                "is already part of a vertical merge (w:vMerge) -- "
                "splitting an already-merged cell is not supported"
            ),
            "reason": "unsupported_merge",
        }

    grid_col_count = _table_grid_col_count(target_el)
    if grid_col_count is None:
        grid_col_count = max(
            (sum(sp for _tc, _s, sp in _row_cell_spans(tr)) for tr in table_rows_list),
            default=0,
        )
    if cols > 1:
        problems = _validate_uniform_row_spans(target_el, grid_col_count)
        if problems:
            return {
                "error": (
                    "split_cell refused: this table's rows do not "
                    f"consistently account for its {grid_col_count} grid "
                    "column(s) -- ambiguous column addressing"
                ),
                "reason": "ambiguous_grid",
                "details": problems,
            }

    baseline_counts = _structural_counts([body])
    baseline_counts["image_count"] = _docx_media_count(raw)
    taken_ids = _existing_para_ids(root)
    new_paragraphs_added = 0

    original_width = _tc_width(target_tc)

    if cols > 1:
        # Net delta, not a gross addition: the ORIGINAL target cell (and
        # whatever paragraph(s) it held) is removed as part of the splice,
        # so only the DIFFERENCE between what's added and what's removed
        # counts toward the structural-verification expectation below.
        removed_paragraphs = _structural_counts([target_tc])["paragraph_count"]
        per_col_width = (original_width // cols) if original_width else None
        new_cells = [_new_empty_tc(per_col_width, taken_ids) for _ in range(cols)]
        new_paragraphs_added += cols - removed_paragraphs

        tr_children = list(target_row)
        anchor_idx = tr_children.index(target_tc)
        target_row.remove(target_tc)
        for offset, tc in enumerate(new_cells):
            target_row.insert(anchor_idx + offset, tc)

        _ensure_tblgrid(target_el, grid_col_count)
        new_paragraphs_added += _insert_grid_columns(
            target_el, start_col + 1, cols - 1, per_col_width,
            skip_row=target_row, taken_ids=taken_ids,
        )
        target_cells = new_cells
    else:
        target_cells = [target_tc]

    if rows > 1:
        new_paragraphs_added += _split_rows(
            target_el=target_el,
            target_row=target_row,
            target_cells=target_cells,
            start_col=start_col,
            extra_rows=rows - 1,
            taken_ids=taken_ids,
        )

    expected_counts = dict(baseline_counts)
    expected_counts["paragraph_count"] = baseline_counts["paragraph_count"] + new_paragraphs_added
    expected_hash = _hash_elements([target_el])

    write_result = _write_table_mutation(
        docx_path=docx_path,
        raw=raw,
        root=root,
        target_el=target_el,
        table_index=table_index,
        expected_counts=expected_counts,
        expected_hash=expected_hash,
        index_db_path=index_db_path,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
    )
    if "error" in write_result:
        return write_result

    from ._vendored_content_tree import _table_node  # noqa: PLC0415

    table_meta = _table_node(target_el, table_index)

    return {
        "status": "split",
        "table_index": table_index,
        "row_index": row_index,
        "col_index": col_index,
        "cols": cols,
        "rows": rows,
        "row_count": table_meta["row_count"],
        "col_count": table_meta["col_count"],
        "docx_path": write_result["docx_path"],
        **write_result["render_info"],
    }


def transpose_table(
    docx_path: str,
    table_index: int,
    index_db_path: str | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
) -> dict[str, Any]:
    """a2cd9f54 -- Transpose a table's rows and columns in place.

    Supported ONLY for a fully rectangular table with NO ``w:gridSpan`` > 1
    and NO ``w:vMerge`` anywhere -- refuses with ``reason="unsupported_merge"``
    otherwise (a horizontally-merged cell has no single canonical
    vertically-merged equivalent, so this never guesses). Also refuses with
    ``reason="ambiguous_grid"`` if the table's rows do not all have the same
    cell count.

    Reuses the SAME ``<w:tc>`` element objects (never deep-copied), only
    repositioning them -- every relationship id (image/hyperlink), bookmark,
    numbering reference, and run of formatted text inside a cell survives
    verbatim; only each cell's row/column position changes.

    Row heights and column widths have no canonical semantic mapping under
    a transpose (a row's height does not become a column's width in any
    well-defined way): the new ``w:tblGrid`` falls back to the table's
    original total width (summed from its old ``w:gridCol`` widths, when
    present) divided evenly across the new column count -- a documented,
    honest default rather than a fabricated precise one. ``w:trPr`` (e.g.
    explicit row heights) is intentionally dropped from the new rows for
    the same reason.

    Args:
        docx_path:       Absolute path to the .docx file (mutated in place).
        table_index:      0-based body-child position of the ``<w:tbl>``.
        index_db_path:    If supplied, sidecar is invalidated after write.
        allow_degraded_render: Same audited opt-in as :func:`insert_column`.
        degraded_render_reason: Required, non-empty when
                          ``allow_degraded_render`` is True.

    Returns:
        ``{status, table_index, row_count, col_count, docx_path,
        render_status, render_verified, render_backend, render_detail}`` or
        ``{"error": ...}`` (with ``"reason"`` one of ``"unsupported_merge"``
        / ``"ambiguous_grid"`` when applicable) on failure.
    """
    if allow_degraded_render and not (
        degraded_render_reason and str(degraded_render_reason).strip()
    ):
        return {
            "error": (
                "degraded_render_reason is required and must be non-empty "
                "when allow_degraded_render=True -- an audited degrade with "
                "no stated reason is not auditable and is refused"
            )
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

    target_el, err = _resolve_table_element(body, table_index)
    if err is not None:
        return err

    rows = _table_rows(target_el)
    if not rows:
        return {"error": "table has no rows to transpose"}

    row_cell_lists: list[list[ET.Element]] = []
    for tr in rows:
        cells = tr.findall(_W_TC)
        for tc in cells:
            if _cell_grid_span(tc) != 1 or _cell_vmerge(tc) is not None:
                return {
                    "error": (
                        "transpose_table refused: this table contains a "
                        "merged cell (w:gridSpan or w:vMerge) -- the "
                        "correct transposed merge geometry is ambiguous, so "
                        "this primitive only supports a fully rectangular, "
                        "unmerged table"
                    ),
                    "reason": "unsupported_merge",
                }
        row_cell_lists.append(cells)

    col_count = len(row_cell_lists[0]) if row_cell_lists else 0
    if col_count == 0 or any(len(cells) != col_count for cells in row_cell_lists):
        return {
            "error": (
                "transpose_table refused: every row must have the SAME "
                "number of cells -- this table's rows are not uniform"
            ),
            "reason": "ambiguous_grid",
        }
    row_count = len(row_cell_lists)

    baseline_counts = _structural_counts([body])
    baseline_counts["image_count"] = _docx_media_count(raw)

    # Build the transposed row list: new row r = column r of every old row,
    # in old-row order. Reuses the SAME <w:tc> element objects (never
    # deep-copied) -- see docstring.
    new_trs: list[ET.Element] = []
    for new_row_idx in range(col_count):
        new_tr = ET.Element(_W_TR)
        for old_row_idx in range(row_count):
            new_tr.append(row_cell_lists[old_row_idx][new_row_idx])
        new_trs.append(new_tr)

    for tr in rows:
        target_el.remove(tr)
    for tr in new_trs:
        target_el.append(tr)

    # Rebuild w:tblGrid for the new column count (== old row_count). See
    # docstring for why widths fall back to an even split of the original
    # total rather than a fabricated precise mapping.
    old_grid = target_el.find(_W_TBLGRID)
    total_width = None
    if old_grid is not None:
        widths = []
        for gc in old_grid.findall(_W_GRIDCOL):
            raw_w = gc.get(_q(_W, "w"))
            if raw_w is not None:
                try:
                    widths.append(int(raw_w))
                except ValueError:
                    pass
        if widths:
            total_width = sum(widths)
        target_el.remove(old_grid)

    new_grid = ET.Element(_W_TBLGRID)
    tblPr = target_el.find(_q(_W, "tblPr"))
    target_el.insert((list(target_el).index(tblPr) + 1) if tblPr is not None else 0, new_grid)
    per_col_width = (total_width // row_count) if total_width else None
    for _ in range(row_count):
        gc = ET.SubElement(new_grid, _W_GRIDCOL)
        if per_col_width is not None:
            gc.set(_q(_W, "w"), str(per_col_width))

    # A pure cell-reshuffle: paragraph/heading/table/image counts are ALL
    # invariant -- no content is created, destroyed, or leaves the table.
    expected_counts = dict(baseline_counts)
    expected_hash = _hash_elements([target_el])

    write_result = _write_table_mutation(
        docx_path=docx_path,
        raw=raw,
        root=root,
        target_el=target_el,
        table_index=table_index,
        expected_counts=expected_counts,
        expected_hash=expected_hash,
        index_db_path=index_db_path,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
    )
    if "error" in write_result:
        return write_result

    from ._vendored_content_tree import _table_node  # noqa: PLC0415

    table_meta = _table_node(target_el, table_index)

    return {
        "status": "transposed",
        "table_index": table_index,
        "row_count": table_meta["row_count"],
        "col_count": table_meta["col_count"],
        "docx_path": write_result["docx_path"],
        **write_result["render_info"],
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


def _save_docx_with_new_parts_stdlib(
    raw: bytes,
    updated_parts: dict[str, bytes],
    dest: str,
    *,
    protected_keys: tuple[str, ...] = ("media_count", "style_count"),
) -> dict[str, Any]:
    """Write MULTIPLE ZIP parts back into ``dest`` in one repackage.

    Unlike :func:`_save_docx_xml_stdlib` (which can only ever overwrite the
    single, already-existing ``word/document.xml`` member), this adds parts
    that are not already present in the original archive AND overwrites parts
    that are. Every other original ZIP member is preserved byte-for-byte.

    Hardened (dccc2311) to route through :func:`_atomic_write_docx_bytes`'s
    stage -> verify -> promote transaction, gating ``protected_keys`` (default
    ``("media_count", "style_count")``) to be UNCHANGED. Unlike
    :func:`_save_docx_xml_stdlib`, relationship and equation counts are
    deliberately NOT gated by the default: every pre-d371b00b caller of this
    multi-part writer (insert_word_comment, highlight_document_matches,
    set_page_header/footer) legitimately adds relationships and/or new
    content-type overrides as part of a correct write, so a relationship-count
    delta is expected, not a corruption signal. Media and styles are never
    legitimately touched by any of them, so those two stay hard invariants by
    default.

    d371b00b -- ``protected_keys`` is now caller-overridable so a writer that
    LEGITIMATELY changes media (:func:`insert_docx_media_part`, which adds a
    brand-new ``word/media/*`` part as its entire point) can pass
    ``protected_keys=("style_count",)`` instead of inheriting an invariant
    that would always reject its own correct write. That caller is
    responsible for its OWN precise verification of the media/relationship
    delta it expects (see :func:`_verify_media_part_insertion_write`) --
    widening ``protected_keys`` here only removes an invariant that would
    always fire false-positive for it; it adds no new laxness of its own.

    Backs up the existing file to ``dest + ".bak"`` when it already exists
    (best-effort, non-fatal on failure -- same pattern as
    :func:`_save_docx_xml_stdlib`).

    Returns :func:`_atomic_write_docx_bytes`'s transaction dict, ``{
    "manifest_hash", "pre_counts", "post_counts", "promoted_sha256"}`` --
    pre-existing callers that ignore the return value (every call site before
    d371b00b) are unaffected.
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

    return _atomic_write_docx_bytes(
        out.getvalue(),
        dest,
        pre_manifest=_docx_structural_manifest(raw),
        protected_keys=protected_keys,
        changed_parts=dict(updated_parts),
    )


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
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
) -> dict[str, Any]:
    """Shared implementation for :func:`set_page_header` / :func:`set_page_footer`.

    ddd79188 follow-up -- holds ``docx_path``'s promotion lock across the
    multi-part write (:func:`_save_docx_with_new_parts_stdlib`) THROUGH a
    real Word/COM (or LibreOffice) render-capability check
    (:func:`_enforce_render_verification`), mirroring the same gate
    :func:`insert_figure_block` / :func:`merge_draft_into_canonical` already
    enforce: structural ZIP/XML/relationship verification alone can never
    prove the document actually opens/renders in Word. ``allow_degraded_render``
    / ``degraded_render_reason`` are the same audited opt-in those functions
    expose for the "no render backend available in this environment" case --
    see :func:`_enforce_render_verification` for the full three-state
    contract.
    """
    if not text or not str(text).strip():
        return {"error": "text must be a non-empty string"}
    if header_footer_type not in _HDR_FTR_VALID_TYPES:
        return {
            "error": (
                f"type must be one of {sorted(_HDR_FTR_VALID_TYPES)}, "
                f"got {header_footer_type!r}"
            )
        }
    if allow_degraded_render and not (
        degraded_render_reason and str(degraded_render_reason).strip()
    ):
        return {
            "error": (
                "degraded_render_reason is required and must be non-empty "
                "when allow_degraded_render=True -- an audited degrade with "
                "no stated reason is not auditable and is refused"
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

    with _docx_promotion_lock(docx_path):
        try:
            _save_docx_with_new_parts_stdlib(raw, updated_parts, docx_path)
        except OSError as exc:
            return {"error": f"could not write {docx_path}: {exc}"}

        promoted_sha256 = _docx_file_sha256(docx_path)
        render_error, render_info = _enforce_render_verification(
            docx_path,
            promoted_sha256=promoted_sha256,
            allow_degraded_render=allow_degraded_render,
            degraded_render_reason=degraded_render_reason,
        )
        if render_error is not None:
            render_error["kind"] = kind
            render_error["type"] = header_footer_type
            render_error["docx_path"] = docx_path
            return render_error

    _invalidate_sidecar_mtime(index_db_path)

    return {
        "status": "set",
        "kind": kind,
        "type": header_footer_type,
        "part_name": part_name,
        "relationship_id": rel_id,
        "text": text.strip(),
        "docx_path": docx_path,
        **render_info,
    }


def set_page_header(
    docx_path: str,
    text: str,
    header_type: str = "default",
    index_db_path: str | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
) -> dict[str, Any]:
    """f1185012 -- Add or replace a page header on ``docx_path``.

    Allocates a new ``word/header<N>.xml`` part, a new relationship in
    ``word/_rels/document.xml.rels``, a new content-type override in
    ``[Content_Types].xml``, and a ``<w:headerReference>`` on the document's
    final ``<w:sectPr>`` -- OR, if a header of this exact ``header_type``
    already exists, overwrites that SAME part's content in place (no new
    part/relationship/override, no sectPr change).

    ddd79188 follow-up -- after the write is staged, verified (ZIP/XML/
    relationship/media integrity), and promoted, a real Word/COM (or
    LibreOffice) render-capability check also runs against the just-written
    file (see :func:`_enforce_render_verification`). ``"rendered"`` continues
    normally with render evidence attached to the result. ``"failed"``
    restores the pre-write backup and returns an error. ``"unavailable-with-
    reason"`` (no render backend in this environment) ALSO fails closed by
    default -- never reported as verified -- unless ``allow_degraded_render``
    and a non-empty ``degraded_render_reason`` are both supplied.

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
        allow_degraded_render: Explicit, audited opt-in to accept this write
                      when no render backend is available in this
                      environment. Requires ``degraded_render_reason``.
        degraded_render_reason: Required, non-empty when
                      ``allow_degraded_render`` is True; carried onto the
                      result as an audit trail.

    Returns:
        ``{status, kind, type, part_name, relationship_id, text, docx_path,
        render_status, render_verified, ...}``
        or ``{"error": <message>}`` on failure (file NOT left mutated on
        validation failure; restored from backup on a structural- or
        render-verification failure).
    """
    return _set_page_header_or_footer(
        docx_path,
        text,
        "header",
        header_type,
        index_db_path,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
    )


def set_page_footer(
    docx_path: str,
    text: str,
    footer_type: str = "default",
    index_db_path: str | None = None,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
) -> dict[str, Any]:
    """f1185012 -- Add or replace a page footer on ``docx_path``.

    Mirrors :func:`set_page_header` exactly (same allocation-vs-overwrite
    "set" semantics, same part/relationship/content-type plumbing, same
    fail-closed render-verification gate) for ``<w:footerReference>`` /
    ``word/footer<N>.xml`` instead. See its docstring for the full
    parameter/return contract.
    """
    return _set_page_header_or_footer(
        docx_path,
        text,
        "footer",
        footer_type,
        index_db_path,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
    )

# ---------------------------------------------------------------------------
# 7c5e0e9a — document-wide XML search and native Word-comment write-back.
# ---------------------------------------------------------------------------

_SEARCH_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_COMMENTS_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
)
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"


def _search_tokens(text: str) -> list[str]:
    return _SEARCH_TOKEN_RE.findall(text.casefold())


def _search_snippet(text: str, terms: list[str], radius: int = 90) -> str:
    if not text:
        return ""
    folded = text.casefold()
    positions = [folded.find(term) for term in terms if term and folded.find(term) >= 0]
    if not positions:
        return text[: radius * 2]
    start = max(0, min(positions) - radius)
    end = min(len(text), max(positions) + max(map(len, terms)) + radius)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


def _search_locator_text(text: str, terms: list[str], radius: int = 40) -> str:
    """c7cc9da4 -- a short, LITERAL substring of ``text`` around the first
    match: exact characters only, never an ellipsis or any other synthesized
    character, so pasting this verbatim into Word's own Ctrl+F Find box
    actually locates this occurrence.

    Distinct from :func:`_search_snippet`, which is a human-readable preview
    (may be prefixed/suffixed with "…") and is not guaranteed to be an exact
    substring Word's own Find can match.
    """
    if not text:
        return ""
    folded = text.casefold()
    positions = [folded.find(term) for term in terms if term and folded.find(term) >= 0]
    if not positions:
        return text.strip()[: radius * 2]
    start = max(0, min(positions) - radius)
    end = min(len(text), max(positions) + max(map(len, terms)) + radius)
    return text[start:end].strip()


def _search_element_text(element: ET.Element) -> str:
    return "".join(
        node.text or ""
        for node in element.iter()
        if node.tag in (_q(_W, "t"), _q(_W, "delText"))
    ).strip()


def _search_part_kind(part_name: str) -> str:
    lower = part_name.lower()
    if lower == "word/document.xml":
        return "document"
    if "/header" in lower:
        return "header"
    if "/footer" in lower:
        return "footer"
    if "footnotes" in lower:
        return "footnote"
    if "endnotes" in lower:
        return "endnote"
    return "xml"


def _search_style(paragraph: ET.Element) -> str | None:
    p_pr = paragraph.find(_q(_W, "pPr"))
    p_style = p_pr.find(_q(_W, "pStyle")) if p_pr is not None else None
    return p_style.get(_q(_W, "val")) if p_style is not None else None


def _search_is_caption(paragraph: ET.Element, text: str) -> str | None:
    lowered = text.casefold()
    instruction_parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == _q(_W, "fldSimple"):
            instruction_parts.append(node.get(_q(_W, "instr"), ""))
        elif node.tag == _q(_W, "instrText"):
            instruction_parts.append(node.text or "")
    instructions = " ".join(instruction_parts).casefold()
    style = (_search_style(paragraph) or "").casefold()
    figure = "seq figure" in instructions or re.match(r"^\s*figure\b", lowered)
    table = "seq table" in instructions or re.match(r"^\s*table\b", lowered)
    if figure and (style == "caption" or "seq figure" in instructions):
        return "figure_caption"
    if table and (style == "caption" or "seq table" in instructions):
        return "table_caption"
    return None


def _search_paragraph_kind(
    paragraph: ET.Element, part_kind: str, text: str
) -> str:
    if part_kind in {"header", "footer"}:
        return part_kind
    caption_kind = _search_is_caption(paragraph, text)
    if caption_kind:
        return caption_kind
    style = _search_style(paragraph)
    p_pr = paragraph.find(_q(_W, "pPr"))
    has_outline = p_pr is not None and p_pr.find(_q(_W, "outlineLvl")) is not None
    if _is_heading(style) or has_outline:
        return "heading"
    return "paragraph"


def _search_heading_path(
    paragraphs: list[tuple[ET.Element, str, str | None, str]],
) -> dict[int, list[str]]:
    path: list[str] = []
    result: dict[int, list[str]] = {}
    for index, (paragraph, kind, _style, text) in enumerate(paragraphs):
        if kind == "heading":
            level = _heading_level(_search_style(paragraph))
            path = path[: max(0, level - 1)]
            path.append(text)
        result[index] = list(path)
    return result


def _iter_document_search_units(raw: bytes) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        part_names = [
            name
            for name in archive.namelist()
            if name.startswith("word/")
            and name.endswith(".xml")
            and "/_rels/" not in name
            and not name.endswith(".rels")
        ]
        for part_name in part_names:
            try:
                root = ET.fromstring(archive.read(part_name))
            except ET.ParseError:
                continue
            part_kind = _search_part_kind(part_name)
            paragraphs: list[tuple[ET.Element, str, str | None, str]] = []
            for paragraph in root.iter(_q(_W, "p")):
                text = _search_element_text(paragraph)
                style = _search_style(paragraph)
                kind = _search_paragraph_kind(paragraph, part_kind, text)
                paragraphs.append((paragraph, kind, style, text))
            section_paths = _search_heading_path(paragraphs)
            for p_index, (paragraph, kind, style, text) in enumerate(paragraphs):
                if not text:
                    continue
                element_id = paragraph.get(_q(_W14, "paraId")) or f"{part_name}#p{p_index}"
                section_path = section_paths[p_index]
                units.append(
                    {
                        "element_id": element_id,
                        "element_type": kind,
                        "part": part_name,
                        "text": text,
                        "style": style,
                        "section_path": section_path,
                        "xml_kind": "paragraph",
                    }
                )
                if kind == "heading" and part_kind == "document":
                    units.append(
                        {
                            "element_id": f"{part_name}#section{p_index}",
                            "element_type": "section",
                            "part": part_name,
                            "text": text,
                            "style": style,
                            "section_path": section_path,
                            "xml_kind": "section",
                        }
                    )
            for table_index, table in enumerate(root.iter(_q(_W, "tbl"))):
                table_text = _search_element_text(table)
                if table_text:
                    units.append(
                        {
                            "element_id": f"{part_name}#table{table_index}",
                            "element_type": "table",
                            "part": part_name,
                            "text": table_text,
                            "style": None,
                            "section_path": [],
                            "xml_kind": "table",
                        }
                    )
    return units


def _search_allowed_type(element_type: str, requested: set[str] | None) -> bool:
    if not requested:
        return True
    aliases = {
        "body": "paragraph",
        "paragraphs": "paragraph",
        "headings": "heading",
        "figures": "figure_caption",
        "tables": "table",
        "sections": "section",
        "headers": "header",
        "footers": "footer",
    }
    normalized = {aliases.get(value, value) for value in requested}
    return element_type in normalized or (
        "caption" in requested and element_type.endswith("_caption")
    )


def _bm25_search_units(
    units: list[dict[str, Any]], query: str, limit: int
) -> list[dict[str, Any]]:
    terms = _search_tokens(query)
    if not terms or not units:
        return []
    term_set = set(terms)
    document_frequency = {
        term: sum(term in set(_search_tokens(unit["text"])) for unit in units)
        for term in term_set
    }
    average_length = sum(len(_search_tokens(unit["text"])) for unit in units) / len(units)
    average_length = max(average_length, 1.0)
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for order, unit in enumerate(units):
        tokens = _search_tokens(unit["text"])
        counts = {term: tokens.count(term) for term in term_set}
        length = max(len(tokens), 1)
        score = 0.0
        for term in terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            df = document_frequency[term]
            idf = __import__("math").log(1.0 + (len(units) - df + 0.5) / (df + 0.5))
            score += idf * (tf * 2.2) / (
                tf + 1.2 * (0.25 + 0.75 * length / average_length)
            )
        if score:
            enriched = dict(unit)
            enriched["bm25_score"] = score
            enriched["snippet"] = _search_snippet(unit["text"], terms)
            enriched["quoted_text"] = _search_locator_text(unit["text"], terms)
            enriched["highlight_ranges"] = [
                [match.start(), match.end()]
                for term in terms
                for match in re.finditer(re.escape(term), unit["text"], re.IGNORECASE)
            ]
            scored.append((score, order, enriched))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [unit for _score, _order, unit in scored[: max(0, int(limit))]]


def _attach_word_search_locators(
    results: list[dict[str, Any]], all_units: list[dict[str, Any]]
) -> None:
    """c7cc9da4 -- stamp each result with a Word-Find-ready locator.

    A recommendation anchored to a search result needs more than
    ``element_id`` (which addresses an OOXML paragraph/table/section, not
    something a human reviewer can act on directly inside Word):
    ``word_search_locator`` pairs the same literal text ``quoted_text``
    already carries with how many times that EXACT literal string occurs
    anywhere else in the same XML part (counted across ``all_units`` --
    the unfiltered, whole-part unit list, not just the returned matches, so
    the count reflects the real document, not just this query's results).

    ``unique_in_part=True`` means pasting ``find_text`` into Word's own
    Ctrl+F box lands on this occurrence and only this one; otherwise
    ``occurrence_count_in_part`` tells the reviewer how many Find-Next
    presses to expect -- this module has no way to drive Word's cursor
    itself, so an honest count is the best it can hand back.
    """
    part_corpus: dict[str, str] = {}
    for unit in all_units:
        part_corpus[unit["part"]] = part_corpus.get(unit["part"], "") + "\n" + unit["text"]
    for result in results:
        quoted_text = result.get("quoted_text") or ""
        part = result.get("part", "")
        corpus = part_corpus.get(part, "")
        occurrences = corpus.casefold().count(quoted_text.casefold()) if quoted_text else 0
        result["word_search_locator"] = {
            "find_text": quoted_text,
            "part": part,
            "element_id": result.get("element_id"),
            "unique_in_part": occurrences == 1,
            "occurrence_count_in_part": occurrences,
        }


def search_document_xml(
    docx_path: str,
    query: str,
    element_types: list[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """BM25-search all searchable Word XML parts with structural filters.

    The search reads word/document.xml plus header/footer, footnote, and
    endnote parts. Results are typed as paragraph, heading, section,
    figure_caption, table_caption, table, header, or footer. element_types
    accepts those names and aliases such as caption, body, headings, and
    tables. This is a stateless first-stage search; the existing sidecar FTS5
    index remains the fast paragraph-only path, while this surface covers the
    whole package and leaves a clean seam for a future vector engine.

    c7cc9da4 -- this is the anchor-resolution surface for a Meridian-docs
    review session's recommendations. Each result carries, in addition to
    ``element_id`` (the stable paragraph/section/table/caption id a
    recommendation attaches to):

      - ``quoted_text``: an exact, literal substring of the source text
        around the match -- unlike ``snippet``, it never contains a
        synthesized "…" truncation marker, so it is safe to quote verbatim
        in a recommendation or paste into Word's own Find box.
      - ``word_search_locator``: ``{find_text, part, element_id,
        unique_in_part, occurrence_count_in_part}`` -- ``find_text`` is the
        same literal text as ``quoted_text``; ``unique_in_part`` tells a
        reviewer whether a plain Ctrl+F search for it in Word will land on
        this occurrence and only this one, or whether ``Find Next`` will be
        needed ``occurrence_count_in_part`` times.

    Image anchors resolve via the neighboring ``figure_caption`` result's
    ``element_id`` (the caption paragraph) together with
    :func:`find_image_paragraph`, which returns the picture paragraph's own
    id for a given ``figure_index`` -- an image paragraph carries no
    searchable text of its own, so it is never a direct search hit. Equation
    anchors are NOT covered by this search (OMML math markup has no
    ``<w:t>`` body text to match against) -- use
    :func:`parse_docx_equations_local` (exposed as the ``extract_equations``
    / ``get_equations`` MCP tools), which carries its own stable equation
    ids, for those.
    """
    if not query or not str(query).strip():
        return []
    try:
        with open(docx_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return [{"error": str(exc)}]
    all_units = _iter_document_search_units(raw)
    requested = {str(value).casefold() for value in element_types or []}
    units = [
        unit
        for unit in all_units
        if _search_allowed_type(unit["element_type"], requested)
    ]
    results = _bm25_search_units(units, str(query), limit)
    _attach_word_search_locators(results, all_units)
    return results


def _highlight_run_if_matching(run: ET.Element, terms: list[str], color: str) -> bool:
    run_text = _search_element_text(run)
    if not run_text:
        return False
    if not any(re.search(re.escape(term), run_text, re.IGNORECASE) for term in terms):
        return False
    r_pr = run.find(_q(_W, "rPr"))
    if r_pr is None:
        r_pr = ET.Element(_q(_W, "rPr"))
        run.insert(0, r_pr)
    highlight = r_pr.find(_q(_W, "highlight"))
    if highlight is None:
        highlight = ET.SubElement(r_pr, _q(_W, "highlight"))
    highlight.set(_q(_W, "val"), color)
    return True


def highlight_document_matches(
    docx_path: str,
    query: str,
    element_types: list[str] | None = None,
    color: str = "yellow",
    limit: int = 100,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
) -> dict[str, Any]:
    """Apply native w:highlight to matching text runs in a DOCX.

    Search and write-back use the same structural filters as search_document_xml.
    Matching runs retain their original content and formatting; a run is
    highlighted when it contains at least one query term. The operation is
    idempotent and returns the matched result records.

    ddd79188 follow-up -- once the highlighted parts are staged, verified
    (ZIP/XML/relationship/media integrity via
    :func:`_save_docx_with_new_parts_stdlib`), and promoted, a real Word/COM
    (or LibreOffice) render-capability check also runs against the
    just-written file (:func:`_enforce_render_verification`), mirroring the
    same gate :func:`insert_figure_block` / :func:`merge_draft_into_canonical`
    already enforce. ``allow_degraded_render`` / ``degraded_render_reason``
    are the same audited opt-in those functions expose for the "no render
    backend available in this environment" case.
    """
    if color not in {
        "yellow", "brightGreen", "turquoise", "pink", "blue", "red",
        "darkBlue", "teal", "green", "violet", "darkRed", "darkYellow",
        "gray50", "gray25", "black", "white",
    }:
        return {"error": f"unsupported highlight color: {color!r}"}
    if not query or not str(query).strip():
        return {"error": "query must be a non-empty string"}
    if allow_degraded_render and not (
        degraded_render_reason and str(degraded_render_reason).strip()
    ):
        return {
            "error": (
                "degraded_render_reason is required and must be non-empty "
                "when allow_degraded_render=True -- an audited degrade with "
                "no stated reason is not auditable and is refused"
            )
        }
    try:
        raw, _root = _load_docx_xml_stdlib(docx_path)
    except (OSError, ValueError) as exc:
        return {"error": str(exc)}
    matches = search_document_xml(docx_path, query, element_types, limit)
    target_keys = {
        (match["part"], match["element_id"])
        for match in matches
        if match.get("xml_kind") == "paragraph"
    }
    if not target_keys:
        return {"status": "no_matches", "matched_runs": 0, "matches": matches}
    terms = _search_tokens(query)
    updated_parts: dict[str, bytes] = {}
    matched_runs = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for part_name, target_id in target_keys:
            if part_name not in archive.namelist():
                continue
            root = ET.fromstring(archive.read(part_name))
            for index, paragraph in enumerate(root.iter(_q(_W, "p"))):
                expected = paragraph.get(_q(_W14, "paraId")) or f"{part_name}#p{index}"
                if expected != target_id:
                    continue
                for run in paragraph.iter(_q(_W, "r")):
                    if _highlight_run_if_matching(run, terms, color):
                        matched_runs += 1
                updated_parts[part_name] = (
                    b'<?xml version="1.0" encoding="UTF-8"?>\n'
                    + ET.tostring(root, encoding="utf-8")
                )
                break
    if not updated_parts:
        return {"status": "no_matches", "matched_runs": 0, "matches": matches}

    with _docx_promotion_lock(docx_path):
        try:
            _save_docx_with_new_parts_stdlib(raw, updated_parts, docx_path)
        except OSError as exc:
            return {"error": f"could not write {docx_path}: {exc}"}

        promoted_sha256 = _docx_file_sha256(docx_path)
        render_error, render_info = _enforce_render_verification(
            docx_path,
            promoted_sha256=promoted_sha256,
            allow_degraded_render=allow_degraded_render,
            degraded_render_reason=degraded_render_reason,
        )
        if render_error is not None:
            render_error["matches"] = matches
            render_error["docx_path"] = docx_path
            return render_error

    return {
        "status": "highlighted",
        "matched_runs": matched_runs,
        "match_count": len(matches),
        "matches": matches,
        "color": color,
        "docx_path": docx_path,
        **render_info,
    }


def _next_word_comment_id(document_root: ET.Element, comments_root: ET.Element | None) -> int:
    ids: list[int] = []
    for element in document_root.iter():
        if element.tag.rsplit("}", 1)[-1] in {
            "commentRangeStart", "commentRangeEnd", "commentReference",
        }:
            value = element.get(_q(_W, "id")) or element.get("id")
            if value is not None:
                try:
                    ids.append(int(value))
                except ValueError:
                    pass
    if comments_root is not None:
        for comment in comments_root.findall(_q(_W, "comment")):
            try:
                ids.append(int(comment.get(_q(_W, "id"), "-1")))
            except ValueError:
                pass
    return max(ids, default=-1) + 1


def insert_word_comment(
    docx_path: str,
    text: str,
    anchor_para_id: str,
    author: str = "Meridian",
    initials: str = "M",
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
) -> dict[str, Any]:
    """Insert a real Word comment anchored to an existing paragraph.

    ddd79188 follow-up -- once the comment parts are staged, verified
    (ZIP/XML/relationship/media integrity via
    :func:`_save_docx_with_new_parts_stdlib`), and promoted, a real Word/COM
    (or LibreOffice) render-capability check also runs against the
    just-written file (:func:`_enforce_render_verification`), mirroring the
    same gate :func:`insert_figure_block` / :func:`merge_draft_into_canonical`
    already enforce. ``allow_degraded_render`` / ``degraded_render_reason``
    are the same audited opt-in those functions expose for the "no render
    backend available in this environment" case.
    """
    if not text or not str(text).strip():
        return {"error": "text must be a non-empty string"}
    if not author or not str(author).strip():
        return {"error": "author must be a non-empty string"}
    if allow_degraded_render and not (
        degraded_render_reason and str(degraded_render_reason).strip()
    ):
        return {
            "error": (
                "degraded_render_reason is required and must be non-empty "
                "when allow_degraded_render=True -- an audited degrade with "
                "no stated reason is not auditable and is refused"
            )
        }
    try:
        raw, root = _load_docx_xml_stdlib(docx_path)
    except (OSError, ValueError) as exc:
        return {"error": str(exc)}
    found = _find_para_by_id(root, anchor_para_id)
    if found is None:
        return {"error": f"para_id {anchor_para_id!r} not found in {docx_path}"}
    _body, paragraph, _child_index = found
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        comments_part = "word/comments.xml"
        try:
            rels_root = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
        except (KeyError, ET.ParseError):
            rels_root = ET.Element(_q(_REL_NS, "Relationships"))
        for relation in rels_root.findall(_q(_REL_NS, "Relationship")):
            if relation.get("Type") == _COMMENTS_REL_TYPE:
                target = relation.get("Target", "comments.xml").lstrip("/")
                comments_part = target if target.startswith("word/") else f"word/{target}"
                break
        try:
            comments_root = ET.fromstring(archive.read(comments_part))
        except (KeyError, ET.ParseError):
            comments_root = ET.Element(_q(_W, "comments"))
    comment_id = _next_word_comment_id(root, comments_root)
    comment = ET.SubElement(comments_root, _q(_W, "comment"))
    comment.set(_q(_W, "id"), str(comment_id))
    comment.set(_q(_W, "author"), str(author).strip())
    comment.set(_q(_W, "initials"), str(initials or "").strip()[:9])
    comment.set(_q(_W, "date"), datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    comment_paragraph = ET.SubElement(comment, _q(_W, "p"))
    comment_run = ET.SubElement(comment_paragraph, _q(_W, "r"))
    comment_text = ET.SubElement(comment_run, _q(_W, "t"))
    comment_text.set(_q(_XML_NS, "space"), "preserve")
    comment_text.text = str(text).strip()

    p_pr = paragraph.find(_q(_W, "pPr"))
    start_index = list(paragraph).index(p_pr) + 1 if p_pr is not None else 0
    start = ET.Element(_q(_W, "commentRangeStart"))
    start.set(_q(_W, "id"), str(comment_id))
    paragraph.insert(start_index, start)
    end = ET.Element(_q(_W, "commentRangeEnd"))
    end.set(_q(_W, "id"), str(comment_id))
    paragraph.append(end)
    reference_run = ET.SubElement(paragraph, _q(_W, "r"))
    reference = ET.SubElement(reference_run, _q(_W, "commentReference"))
    reference.set(_q(_W, "id"), str(comment_id))

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        updated_parts: dict[str, bytes] = {
            "word/document.xml": (
                b'<?xml version="1.0" encoding="UTF-8"?>\n'
                + ET.tostring(root, encoding="utf-8")
            ),
            comments_part: (
                b'<?xml version="1.0" encoding="UTF-8"?>\n'
                + ET.tostring(comments_root, encoding="utf-8")
            ),
        }
        try:
            rels_root = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
        except (KeyError, ET.ParseError):
            rels_root = ET.Element(_q(_REL_NS, "Relationships"))
        comments_relation = next(
            (
                rel for rel in rels_root.findall(_q(_REL_NS, "Relationship"))
                if rel.get("Type") == _COMMENTS_REL_TYPE
            ),
            None,
        )
        if comments_relation is None:
            used_ids = {
                rel.get("Id", "")
                for rel in rels_root.findall(_q(_REL_NS, "Relationship"))
            }
            next_id = 1
            while f"rId{next_id}" in used_ids:
                next_id += 1
            comments_relation = ET.SubElement(rels_root, _q(_REL_NS, "Relationship"))
            comments_relation.set("Id", f"rId{next_id}")
            comments_relation.set("Type", _COMMENTS_REL_TYPE)
            comments_relation.set("Target", comments_part.removeprefix("word/"))
        updated_parts["word/_rels/document.xml.rels"] = (
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            + ET.tostring(rels_root, encoding="utf-8")
        )
        try:
            content_types_root = ET.fromstring(archive.read("[Content_Types].xml"))
        except (KeyError, ET.ParseError):
            content_types_root = ET.Element(_q(_CONTENT_TYPES_NS, "Types"))
        override = next(
            (
                item for item in content_types_root.findall(_q(_CONTENT_TYPES_NS, "Override"))
                if item.get("PartName") == f"/{comments_part}"
            ),
            None,
        )
        if override is None:
            override = ET.SubElement(content_types_root, _q(_CONTENT_TYPES_NS, "Override"))
            override.set("PartName", f"/{comments_part}")
            override.set(
                "ContentType",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
            )
        updated_parts["[Content_Types].xml"] = (
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            + ET.tostring(content_types_root, encoding="utf-8")
        )
    with _docx_promotion_lock(docx_path):
        try:
            _save_docx_with_new_parts_stdlib(raw, updated_parts, docx_path)
        except OSError as exc:
            return {"error": f"could not write {docx_path}: {exc}"}

        promoted_sha256 = _docx_file_sha256(docx_path)
        render_error, render_info = _enforce_render_verification(
            docx_path,
            promoted_sha256=promoted_sha256,
            allow_degraded_render=allow_degraded_render,
            degraded_render_reason=degraded_render_reason,
        )
        if render_error is not None:
            render_error["comment_id"] = comment_id
            render_error["anchor_para_id"] = anchor_para_id
            render_error["docx_path"] = docx_path
            return render_error

    return {
        "status": "inserted",
        "mode": "comment",
        "comment_id": comment_id,
        "text": str(text).strip(),
        "anchor_para_id": anchor_para_id,
        "author": str(author).strip(),
        "initials": str(initials or "").strip()[:9],
        "docx_path": docx_path,
        **render_info,
    }


# ---------------------------------------------------------------------------
# Public API: locate_anchor / locate_anchors (2271789f)
#
# A read-only, fresh-snapshot anchor locator. Every call re-parses the .docx
# from disk (via document_content_tree + parse_docx_equations_local) rather
# than trusting any sidecar SQLite index -- there is no staleness window to
# reason about because nothing is cached between calls. Each resolved anchor
# is stamped with the SHA-256 fingerprint (_source_fingerprint) of the exact
# bytes it was resolved against, so a caller holding an OLDER fingerprint can
# detect drift by passing query["expected_source_fingerprint"] before
# trusting a previously-returned target_para_id against a document that may
# have since changed.
#
# Query keys (all optional; at least one is required so the query is not a
# no-op):
#   para_id           -- direct lookup: a real w14:paraId, the
#                         document_content_tree/parse_docx synthesized
#                         "sp<hash>" id, a table id "tbl<index>", a
#                         table-cell id "tbl<index>:r<row>:c<col>", or an
#                         equation's own para_id. Short-circuits every other
#                         key below.
#   section_path      -- e.g. "3.2.4". Matched against BOTH an explicit
#                         numeric prefix parsed out of the heading's own
#                         text (Word's "3.2.4 Title" auto/manual outline
#                         numbering rendered as literal text) and a
#                         positional path computed from heading levels/order
#                         when no explicit prefix is present. NOTE: a
#                         heading that merely *starts* with a number that
#                         isn't outline numbering (e.g. "5 Steps to
#                         Success") is indistinguishable from real section
#                         numbering by this heuristic -- a known tradeoff.
#   section_text      -- substring match (casefold unless case_sensitive)
#                         against heading text; combinable with
#                         section_path (both must match).
#   caption_label     -- e.g. "Table 3" / "Figure 2". Matched against the
#                         caption's SEQ-field cached number, falling back to
#                         a positional occurrence count when the field was
#                         never recalculated by Word.
#   text              -- literal Ctrl+F-style substring (casefold unless
#                         case_sensitive) searched over paragraph/heading/
#                         caption text and table cell text, scoped to
#                         whatever section_path/caption_label already
#                         narrowed (unscoped text queries also fall back to
#                         equation flat_text so equations stay findable).
#   element_types     -- optional list restricting result kinds to any of
#                         heading | paragraph | table | table_cell |
#                         figure_caption | table_caption | equation.
#   case_sensitive    -- bool, default False.
#   expected_source_fingerprint -- optional; if given and it does not match
#                         the CURRENT source fingerprint, resolution is
#                         skipped entirely and {"status": "stale", ...} is
#                         returned immediately -- never trust stale
#                         paragraph indices against a document that moved.
#
# Every result carries an explicit "candidates" list -- empty when a query
# resolved uniquely, populated (with status="ambiguous") when more than one
# element matched -- rather than silently guessing which one was meant.
# Never mutates document_path.
# ---------------------------------------------------------------------------

_EXPLICIT_SECTION_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,8})[.)]?\s+(?=\S)")
_TABLE_ID_RE = re.compile(r"^tbl(\d+)$")
_TABLE_CELL_ID_RE = re.compile(r"^tbl(\d+):r(\d+):c(\d+)$")
_CAPTION_LABEL_QUERY_RE = re.compile(r"^\s*(figure|table)\s+(.+?)\s*$", re.IGNORECASE)


def _normalize_preview(text: str, max_words: int = 12) -> str:
    """Collapse whitespace and take the leading ``max_words`` words.

    Display preview only -- never changes the source text. ``quoted_text``
    in a locator result always carries the verbatim, un-normalized string.
    """
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return ""
    words = normalized.split(" ")
    preview = " ".join(words[:max_words])
    return preview + "..." if len(words) > max_words else preview


def _heading_numbering(heading_blocks: list[dict[str, Any]]) -> dict[str, dict[str, str | None]]:
    """Compute both an explicit and a positional section path per heading.

    ``explicit_number`` comes from a leading "N", "N.N", "N.N.N" ... prefix
    already present in the heading's own text. ``computed_path`` is a
    positional fallback: per-level counters that increment left to right and
    reset any deeper level whenever a shallower heading is seen -- the same
    scheme a generated table of contents would use.
    """
    counters: list[int] = []
    result: dict[str, dict[str, str | None]] = {}
    for h in heading_blocks:
        level = max(1, int(h.get("level", 1) or 1))
        if len(counters) < level:
            counters.extend([0] * (level - len(counters)))
        else:
            counters = counters[:level]
        counters[level - 1] += 1
        computed_path = ".".join(str(c) for c in counters[:level])
        match = _EXPLICIT_SECTION_NUM_RE.match(h.get("text", "") or "")
        result[h["para_id"]] = {
            "explicit_number": match.group(1) if match else None,
            "computed_path": computed_path,
        }
    return result


def _table_anchor_id(index: int) -> str:
    return f"tbl{index}"


def _iter_anchor_records(
    source: str | bytes | bytearray,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fresh (never cached) ordered anchor index for one document snapshot.

    Returns ``(records, tree)`` where ``tree`` is the raw
    :func:`document_content_tree` result (paragraph_count/table_count/
    heading_count/duplicate_para_ids) and ``records`` is one dict per
    document_content_tree block, in document order, each augmented with:

      - ``para_id``        -- stable id (table blocks get a synthesized
                               ``"tbl<index>"`` id; paragraph/heading blocks
                               already carry document_content_tree's own
                               w14:paraId / synth-id / positional id).
      - ``element_kind``    -- heading | paragraph | table | figure_caption
                               | table_caption.
      - ``caption_label``   -- "Figure N" / "Table N" for caption
                               paragraphs, else None.
      - ``table_ref``       -- for a table_caption paragraph, the ``index``
                               of the table block it immediately follows.
      - ``section_path``    -- nearest enclosing heading's path.
      - ``heading_para_id`` -- nearest enclosing heading's para_id.
      - ``section_stack``   -- ordered list of ancestor heading para_ids
                               (root first): subtree membership is simply
                               ``heading_id in record["section_stack"]``.
      - ``explicit_number`` / ``computed_path`` -- heading blocks only.
    """
    from ._vendored_content_tree import document_content_tree  # noqa: PLC0415

    tree = document_content_tree(source)
    blocks: list[dict[str, Any]] = tree.get("blocks") or []
    numbering = _heading_numbering([b for b in blocks if b.get("kind") == "heading"])

    records: list[dict[str, Any]] = []
    section_stack: list[tuple[int, str, str]] = []
    last_table_index: int | None = None
    figure_seq_counter = 0
    table_seq_counter = 0

    for block in blocks:
        kind = block.get("kind")
        record = dict(block)
        caption_label: str | None = None
        table_ref: int | None = None
        element_kind = kind

        if kind == "heading":
            level = max(1, int(block.get("level", 1) or 1))
            while section_stack and section_stack[-1][0] >= level:
                section_stack.pop()
            info = numbering.get(
                block["para_id"], {"explicit_number": None, "computed_path": str(level)}
            )
            path = info["explicit_number"] or info["computed_path"]
            section_stack.append((level, block["para_id"], path))
            record["explicit_number"] = info["explicit_number"]
            record["computed_path"] = info["computed_path"]
        elif kind == "table":
            record["para_id"] = _table_anchor_id(block["index"])
            rows = block.get("rows") or []
            record["text"] = " | ".join(", ".join(row) for row in rows[:3])
            last_table_index = block["index"]
        elif kind == "paragraph":
            if _is_figure_caption(block):
                figure_seq_counter += 1
                num = _seq_cached_number(block, _SEQ_FIGURE_RE) or str(figure_seq_counter)
                caption_label = f"Figure {num}"
                element_kind = "figure_caption"
            elif _is_table_caption(block):
                table_seq_counter += 1
                num = _seq_cached_number(block, _SEQ_TABLE_RE) or str(table_seq_counter)
                caption_label = f"Table {num}"
                element_kind = "table_caption"
                table_ref = last_table_index

        stack_ids = [entry[1] for entry in section_stack]
        record["section_path"] = section_stack[-1][2] if section_stack else None
        record["heading_para_id"] = stack_ids[-1] if stack_ids else None
        record["section_stack"] = stack_ids
        record["caption_label"] = caption_label
        record["element_kind"] = element_kind
        record["table_ref"] = table_ref
        records.append(record)

    return records, tree


def _table_cell_record(table_record: dict[str, Any], row: int, col: int) -> dict[str, Any]:
    rows = table_record.get("rows") or []
    return {
        "element_kind": "table_cell",
        "index": table_record.get("index"),
        "para_id": f"{_table_anchor_id(table_record['index'])}:r{row}:c{col}",
        "text": rows[row][col],
        "section_path": table_record.get("section_path"),
        "heading_para_id": table_record.get("heading_para_id"),
        "section_stack": table_record.get("section_stack", []),
        "table_index": table_record.get("index"),
        "row": row,
        "col": col,
    }


def _match_table_cell_by_id(records: list[dict[str, Any]], target_id: str) -> list[dict[str, Any]]:
    match = _TABLE_CELL_ID_RE.match(target_id or "")
    if not match:
        return []
    table_index, row, col = int(match.group(1)), int(match.group(2)), int(match.group(3))
    for record in records:
        if record.get("element_kind") == "table" and record.get("index") == table_index:
            rows = record.get("rows") or []
            if row < len(rows) and col < len(rows[row]):
                return [_table_cell_record(record, row, col)]
    return []


def _fold(value: str, case_sensitive: bool) -> str:
    return value if case_sensitive else value.casefold()


def _search_text_in_records(
    scope: list[dict[str, Any]], query_text: str, *, case_sensitive: bool
) -> list[dict[str, Any]]:
    needle = _fold(query_text, case_sensitive)
    matches: list[dict[str, Any]] = []
    for record in scope:
        if record.get("element_kind") == "table":
            for row_idx, row in enumerate(record.get("rows") or []):
                for col_idx, cell_text in enumerate(row):
                    if needle in _fold(cell_text, case_sensitive):
                        matches.append(_table_cell_record(record, row_idx, col_idx))
        else:
            text = record.get("text", "") or ""
            if needle in _fold(text, case_sensitive):
                matches.append(record)
    return matches


def _parse_caption_label(label: str) -> tuple[str, str] | None:
    match = _CAPTION_LABEL_QUERY_RE.match(label or "")
    if not match:
        return None
    return match.group(1).casefold(), match.group(2).strip()


def _filter_by_element_types(
    records: list[dict[str, Any]], element_types: list[str] | None
) -> list[dict[str, Any]]:
    if not element_types:
        return records
    wanted = {str(t).casefold() for t in element_types}
    return [r for r in records if str(r.get("element_kind", "")).casefold() in wanted]


def _anchor_brief(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_para_id": record.get("para_id"),
        "element_type": record.get("element_kind"),
        "document_order": record.get("index"),
        "section_path": record.get("section_path"),
        "leading_text_preview": _normalize_preview(record.get("text", "") or ""),
    }


def _bookmark_and_ref_status(document_path: str, target_para_id: str | None) -> tuple[bool, dict[str, Any]]:
    """Best-effort bookmark/REF lookup for a resolved anchor's target_para_id.

    Table/table-cell synthetic ids ("tbl<n>" / "tbl<n>:r<n>:c<n>") are never
    real paragraph ids in the document -- :func:`find_references_to` cannot
    resolve them, so those are reported as un-checked rather than guessed.
    """
    if (
        not target_para_id
        or _TABLE_ID_RE.match(target_para_id)
        or _TABLE_CELL_ID_RE.match(target_para_id)
    ):
        return False, {
            "checked": False,
            "reason": "synthetic table identifier -- not a real paragraph id",
        }
    try:
        refs = find_references_to(document_path, target_para_id)
    except (OSError, ValueError, KeyError, ET.ParseError):
        return False, {"checked": False, "reason": "lookup failed"}
    if not refs or refs.get("error"):
        return False, {
            "checked": True,
            "reference_count": 0,
            "references": [],
            "bookmark_names": [],
            "note": (refs or {}).get("error"),
        }
    return bool(refs.get("bookmark_names")), {
        "checked": True,
        "reference_count": refs.get("reference_count", 0),
        "references": refs.get("references", []),
        "bookmark_names": refs.get("bookmark_names", []),
    }


def _build_resolved_anchor(
    record: dict[str, Any],
    *,
    document_path: str,
    source_fingerprint: str,
    word_search_locator: str | None,
) -> dict[str, Any]:
    text = record.get("text", "") or ""
    preview = _normalize_preview(text)
    target_para_id = record.get("para_id")
    bookmark_exists, ref_status = _bookmark_and_ref_status(document_path, target_para_id)
    return {
        "status": "resolved",
        "document_path": document_path,
        "source_fingerprint": source_fingerprint,
        "element_type": record.get("element_kind"),
        "section_path": record.get("section_path"),
        "heading_para_id": record.get("heading_para_id"),
        "target_para_id": target_para_id,
        "document_order": record.get("index"),
        "quoted_text": text,
        "leading_text_preview": preview,
        "first_words": preview,
        "word_search_locator": word_search_locator or preview,
        "bookmark_exists": bookmark_exists,
        "ref_status": ref_status,
        "candidates": [],
    }


def _ambiguous_anchor_result(
    candidates: list[dict[str, Any]],
    *,
    document_path: str,
    source_fingerprint: str,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "status": "ambiguous",
        "document_path": document_path,
        "source_fingerprint": source_fingerprint,
        "reason": reason or f"{len(candidates)} elements matched this query; narrow it",
        "candidate_count": len(candidates),
        "candidates": [_anchor_brief(c) for c in candidates],
    }


def _not_found_anchor_result(
    reason: str, *, document_path: str, source_fingerprint: str | None
) -> dict[str, Any]:
    return {
        "status": "not_found",
        "document_path": document_path,
        "source_fingerprint": source_fingerprint,
        "reason": reason,
        "candidates": [],
    }


def _equation_pseudo_record(eq: dict[str, Any]) -> dict[str, Any]:
    return {
        "element_kind": "equation",
        "index": eq.get("ordinal"),
        "para_id": eq.get("para_id"),
        "text": eq.get("flat_text", ""),
        "section_path": None,
        "heading_para_id": None,
        "section_stack": [],
    }


def _resolve_anchor_query(
    records: list[dict[str, Any]],
    equations: list[dict[str, Any]],
    query: dict[str, Any],
    *,
    document_path: str,
    source_fingerprint: str,
) -> dict[str, Any]:
    query = dict(query or {})
    case_sensitive = bool(query.get("case_sensitive", False))
    element_types = query.get("element_types")
    para_id_query = query.get("para_id")
    section_path_query = query.get("section_path")
    section_text_query = query.get("section_text")
    caption_label_query = query.get("caption_label")
    text_query = query.get("text")

    if not any(
        [para_id_query, section_path_query, section_text_query, caption_label_query, text_query]
    ):
        return _not_found_anchor_result(
            "query must set at least one of para_id, section_path, section_text, "
            "caption_label, or text",
            document_path=document_path,
            source_fingerprint=source_fingerprint,
        )

    def _finish(matches: list[dict[str, Any]], *, locator: str | None = None) -> dict[str, Any]:
        matches = _filter_by_element_types(matches, element_types)
        if not matches:
            return _not_found_anchor_result(
                "no element matched this query",
                document_path=document_path,
                source_fingerprint=source_fingerprint,
            )
        if len(matches) > 1:
            return _ambiguous_anchor_result(
                matches, document_path=document_path, source_fingerprint=source_fingerprint
            )
        return _build_resolved_anchor(
            matches[0],
            document_path=document_path,
            source_fingerprint=source_fingerprint,
            word_search_locator=locator,
        )

    # 1. Direct para_id short-circuits everything else.
    if para_id_query:
        direct = [r for r in records if r.get("para_id") == para_id_query]
        if not direct:
            direct = _match_table_cell_by_id(records, para_id_query)
        if not direct:
            direct = [
                _equation_pseudo_record(eq)
                for eq in equations
                if eq.get("para_id") == para_id_query
            ]
        if not direct:
            return _not_found_anchor_result(
                f"para_id {para_id_query!r} not found",
                document_path=document_path,
                source_fingerprint=source_fingerprint,
            )
        return _finish(direct)

    scope = records

    # 2. Section scoping (heading resolution).
    heading_target: dict[str, Any] | None = None
    if section_path_query or section_text_query:
        wanted_path = str(section_path_query).strip() if section_path_query else None
        heading_candidates = [
            r
            for r in scope
            if r.get("element_kind") == "heading"
            and (
                wanted_path is None
                or r.get("explicit_number") == wanted_path
                or r.get("computed_path") == wanted_path
            )
            and (
                not section_text_query
                or _fold(str(section_text_query), case_sensitive)
                in _fold(r.get("text") or "", case_sensitive)
            )
        ]
        if not heading_candidates:
            return _not_found_anchor_result(
                "no heading matched section_path/section_text",
                document_path=document_path,
                source_fingerprint=source_fingerprint,
            )
        if len(heading_candidates) > 1:
            return _ambiguous_anchor_result(
                heading_candidates,
                document_path=document_path,
                source_fingerprint=source_fingerprint,
                reason="multiple headings matched section_path/section_text; narrow the query",
            )
        heading_target = heading_candidates[0]
        if not (caption_label_query or text_query):
            return _finish([heading_target])
        scope = [r for r in scope if heading_target["para_id"] in r.get("section_stack", [])]

    # 3. caption_label scoping.
    caption_target: dict[str, Any] | None = None
    if caption_label_query:
        parsed_query = _parse_caption_label(str(caption_label_query))
        caption_candidates = []
        for r in scope:
            if not r.get("caption_label"):
                continue
            parsed_candidate = _parse_caption_label(r["caption_label"])
            if parsed_query and parsed_candidate and parsed_query == parsed_candidate:
                caption_candidates.append(r)
        if not caption_candidates:
            return _not_found_anchor_result(
                f"no caption matched {caption_label_query!r}",
                document_path=document_path,
                source_fingerprint=source_fingerprint,
            )
        if len(caption_candidates) > 1:
            return _ambiguous_anchor_result(
                caption_candidates,
                document_path=document_path,
                source_fingerprint=source_fingerprint,
                reason=f"multiple captions matched {caption_label_query!r}",
            )
        caption_target = caption_candidates[0]
        if not text_query:
            return _finish([caption_target])
        if (
            caption_target.get("element_kind") == "table_caption"
            and caption_target.get("table_ref") is not None
        ):
            scope = [
                r
                for r in records
                if r.get("element_kind") == "table" and r.get("index") == caption_target["table_ref"]
            ] + [caption_target]
        else:
            scope = [caption_target]

    # 4. text (Ctrl+F) search within whatever scope survived steps 2-3.
    if text_query:
        text_matches = _search_text_in_records(scope, str(text_query), case_sensitive=case_sensitive)
        if not text_matches and heading_target is None and caption_target is None:
            # Fully unscoped text query: also try equation flat_text so
            # equations stay reachable via plain Ctrl+F-style search.
            needle = _fold(str(text_query), case_sensitive)
            text_matches.extend(
                _equation_pseudo_record(eq)
                for eq in equations
                if needle in _fold(eq.get("flat_text", "") or "", case_sensitive)
            )
        return _finish(text_matches, locator=str(text_query))

    # section/caption-only queries already returned above.
    return _not_found_anchor_result(
        "query did not resolve to any element",
        document_path=document_path,
        source_fingerprint=source_fingerprint,
    )


def locate_anchor(document_path: str, query: dict[str, Any]) -> dict[str, Any]:
    """2271789f -- read-only, fresh-snapshot deterministic anchor locator.

    Re-parses ``document_path`` from disk on every call (no sidecar SQLite
    index, so there is nothing that can go stale between calls) and resolves
    ``query`` against sections, paragraphs, captions, tables (incl. cell
    text), and equations. See the module-level comment above this function
    for the full query-key contract and the shape of a resolved result:
    ``{status, section_path, heading_para_id, target_para_id,
    document_order, element_type, quoted_text, leading_text_preview,
    first_words, word_search_locator, bookmark_exists, ref_status,
    candidates, document_path, source_fingerprint}``.

    Pass ``query["expected_source_fingerprint"]`` (a value previously
    returned as ``source_fingerprint``) to detect the document having
    changed underneath a stashed locator result before trusting it again --
    a mismatch short-circuits to ``{"status": "stale", ...}`` without
    attempting resolution against what may now be the wrong paragraph.

    Never mutates ``document_path``. Returns ``{"error": ...}`` only when
    the document itself cannot be read or parsed as a .docx.
    """
    if not isinstance(query, dict) or not query:
        return {"error": "query must be a non-empty dict"}
    try:
        with open(document_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return {"error": str(exc)}

    source_fingerprint = _source_fingerprint(raw)
    expected = query.get("expected_source_fingerprint")
    if expected and expected != source_fingerprint:
        return {
            "status": "stale",
            "document_path": document_path,
            "reason": "source_fingerprint_mismatch",
            "expected_source_fingerprint": expected,
            "source_fingerprint": source_fingerprint,
            "candidates": [],
        }

    try:
        records, _tree = _iter_anchor_records(raw)
        equations = parse_docx_equations_local(raw)
    except (ValueError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        return {"error": str(exc)}

    return _resolve_anchor_query(
        records, equations, query, document_path=document_path, source_fingerprint=source_fingerprint
    )


def locate_anchors(document_path: str, queries: list[dict[str, Any]]) -> dict[str, Any]:
    """2271789f -- resolve multiple independent :func:`locate_anchor` queries
    against ONE fresh parse of ``document_path`` (one source_fingerprint, one
    document_content_tree walk) instead of re-reading the file per query.

    Query order is preserved in ``results``; each entry has the exact same
    shape :func:`locate_anchor` returns for a single query. Returns
    ``{"document_path", "source_fingerprint", "query_count", "results"}``,
    or ``{"error": ...}`` if the document itself cannot be read/parsed.
    """
    if not isinstance(queries, list) or not queries:
        return {"error": "queries must be a non-empty list of query dicts"}
    try:
        with open(document_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return {"error": str(exc)}

    source_fingerprint = _source_fingerprint(raw)
    try:
        records, _tree = _iter_anchor_records(raw)
        equations = parse_docx_equations_local(raw)
    except (ValueError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        return {"error": str(exc)}

    results: list[dict[str, Any]] = []
    for query in queries:
        if not isinstance(query, dict) or not query:
            results.append({"error": "query must be a non-empty dict"})
            continue
        expected = query.get("expected_source_fingerprint")
        if expected and expected != source_fingerprint:
            results.append({
                "status": "stale",
                "document_path": document_path,
                "reason": "source_fingerprint_mismatch",
                "expected_source_fingerprint": expected,
                "source_fingerprint": source_fingerprint,
                "candidates": [],
            })
            continue
        results.append(
            _resolve_anchor_query(
                records,
                equations,
                query,
                document_path=document_path,
                source_fingerprint=source_fingerprint,
            )
        )

    return {
        "document_path": document_path,
        "source_fingerprint": source_fingerprint,
        "query_count": len(queries),
        "results": results,
    }


# ---------------------------------------------------------------------------
# b67ec6b5 -- non-mutating DOCX review: aggregate existing read-only finding
# primitives into ONE grouped, locator-enriched result for the dashboard
# review panel. Every finding is enriched via _resolve_anchor_query -- the
# SAME resolver locate_anchor itself calls -- so this never re-derives
# anchor-resolution logic; it only decides WHICH para_id to ask about.
# ---------------------------------------------------------------------------

#: Fixed category set the dashboard groups findings by. Always present in
#: ``findings_by_category`` (count 0 when nothing was found/checked) so a
#: caller can render a stable set of section headers rather than guessing
#: which categories exist for a given document profile -- "structure",
#: "section_page", and "ownership" have no v1 detector yet (framework-
#: agnostic first version -- see the sprint item notes) and always report 0
#: until a future item adds one.
REVIEW_CATEGORIES: tuple[str, ...] = (
    "structure", "equation", "caption", "section_page", "ownership",
    "provenance", "render_integrity",
)


def _legacy_plaintext_caption_findings(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Read-only DETECTION half of :func:`retrofit_plaintext_captions` --
    flags a body paragraph (``element_kind == "paragraph"``, i.e. NOT already
    classified as a native ``figure_caption``/``table_caption`` record by
    :func:`_iter_anchor_records`, which only happens when a SEQ field is
    present) whose visible text opens with "Figure <N>" / "Table <N>" -- the
    exact text pattern :func:`retrofit_plaintext_captions` migrates. Never
    mutates the document; only reports what that primitive WOULD convert, so
    "mixed native/legacy captions" is visible without running the write.
    """
    out: list[dict[str, Any]] = []
    for record in records:
        if record.get("element_kind") != "paragraph":
            continue
        text = record.get("text") or ""
        kind = "Figure"
        m = _PLAINTEXT_FIGURE_RE.match(text)
        if m is None:
            kind = "Table"
            m = _PLAINTEXT_TABLE_RE.match(text)
        if m is None:
            continue
        out.append({
            "type": "legacy_plaintext_caption",
            "para_id": record.get("para_id"),
            "kind": kind,
            "old_cached_number": m.group(1),
            "label_text": m.group(2).strip(),
        })
    return out


def _review_finding_severity(category: str, finding_type: str) -> str:
    if category == "equation" and finding_type in (
        "duplicate_equation_number", "equation_number_gap",
    ):
        return "error"
    if category == "render_integrity":
        return "error"
    if category == "provenance":
        return "info"
    return "warning"


def build_document_review(
    docx_path: str,
    *,
    expected_source_fingerprint: str | None = None,
    style_policy: dict[str, Any] | None = None,
    include_render_check: bool = False,
) -> dict[str, Any]:
    """b67ec6b5 -- non-mutating DOCX review for the dashboard review panel.

    Re-parses ``docx_path`` fresh on every call (same "no sidecar index, so
    nothing can go stale between calls" discipline as :func:`locate_anchor`)
    and composes EXISTING read-only finding primitives instead of
    re-implementing detection logic:

    * ``equation``   -- :func:`audit_equation_style` findings (alignment,
                        trailing punctuation, numbering).
    * ``caption``     -- :func:`_legacy_plaintext_caption_findings` (a
                        plain-text "Figure N"/"Table N" paragraph with no SEQ
                        field -- what :func:`retrofit_plaintext_captions`
                        would convert, reported without mutating).
    * ``provenance``  -- :func:`scan_stale_notes` findings (placeholder/TODO
                        text that may now be outdated).
    * ``render_integrity`` -- :func:`render_gate.check_render_capability`,
                        ONLY when ``include_render_check=True`` (a live
                        render probe is slow/backend-dependent, so it is
                        never invoked implicitly; a "failed" status becomes a
                        finding, "rendered"/"unavailable-with-reason" do
                        not -- mirrors ``docx_integrity_gate``'s "can't
                        confirm never manufactures a finding" rule).
    * ``structure`` / ``section_page`` / ``ownership`` -- reserved, always 0
                        in this first version (see :data:`REVIEW_CATEGORIES`).

    Every finding with a ``para_id`` is enriched with a ``locator`` --
    resolved via :func:`_resolve_anchor_query` (the SAME function
    :func:`locate_anchor` itself calls) against ONE shared parse of the
    document, never a second re-derivation of anchor logic. A finding with no
    ``para_id`` (e.g. a document-level render finding) gets
    ``{"status": "not_applicable", ...}`` instead of a fabricated locator.
    This is deliberate: a caller must never show a raw paragraph id alone --
    the locator always carries ``section_path``/``quoted_text`` alongside
    ``target_para_id``, and an ambiguous/not-found match surfaces
    ``candidates``/a reason instead of guessing.

    Pass ``expected_source_fingerprint`` (a value previously returned as
    ``source_fingerprint``) to detect the document having changed underneath
    a stashed review before trusting it again -- a mismatch short-circuits to
    ``{"status": "stale", ...}`` with empty findings, exactly like
    :func:`locate_anchor`.

    Returns ``{status: "ok", docx_path, source_fingerprint, findings,
    finding_count, findings_by_category, findings_by_severity, categories}``
    on success, ``{status: "stale", ...}`` on a fingerprint mismatch, or
    ``{"error": <message>}`` when the file cannot be read or parsed. Never
    mutates ``docx_path``.
    """
    try:
        with open(docx_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return {"error": str(exc)}

    source_fingerprint = _source_fingerprint(raw)
    if expected_source_fingerprint and expected_source_fingerprint != source_fingerprint:
        return {
            "status": "stale",
            "docx_path": docx_path,
            "reason": "source_fingerprint_mismatch",
            "expected_source_fingerprint": expected_source_fingerprint,
            "source_fingerprint": source_fingerprint,
            "findings": [],
            "finding_count": 0,
            "findings_by_category": {c: 0 for c in REVIEW_CATEGORIES},
            "findings_by_severity": {},
            "categories": list(REVIEW_CATEGORIES),
        }

    try:
        records, _tree = _iter_anchor_records(raw)
        equations = parse_docx_equations_local(raw)
    except (ValueError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        return {"error": str(exc)}

    findings: list[dict[str, Any]] = []

    eq_audit = audit_equation_style(docx_path, style_policy)
    if isinstance(eq_audit, dict) and not eq_audit.get("error"):
        for f in eq_audit.get("findings", []):
            findings.append({
                "category": "equation",
                "severity": _review_finding_severity("equation", f["type"]),
                "type": f["type"],
                "para_id": f.get("para_id"),
                "detail": f,
            })

    for f in _legacy_plaintext_caption_findings(records):
        findings.append({
            "category": "caption",
            "severity": _review_finding_severity("caption", f["type"]),
            "type": f["type"],
            "para_id": f.get("para_id"),
            "detail": f,
        })

    stale_notes = scan_stale_notes(docx_path)
    if isinstance(stale_notes, dict) and not stale_notes.get("error"):
        for f in stale_notes.get("findings", []):
            findings.append({
                "category": "provenance",
                "severity": _review_finding_severity("provenance", "stale_note"),
                "type": "stale_note",
                "para_id": f.get("para_id"),
                "detail": f,
            })

    if include_render_check:
        try:
            render_result = render_gate.check_render_capability(docx_path)
        except Exception:  # noqa: BLE001 -- a broken backend must never break the review
            render_result = None
        if isinstance(render_result, dict) and render_result.get("status") == "failed":
            findings.append({
                "category": "render_integrity",
                "severity": _review_finding_severity("render_integrity", "render_failed"),
                "type": "render_failed",
                "para_id": None,
                "detail": render_result,
            })

    for finding in findings:
        para_id = finding.pop("para_id", None)
        if para_id:
            finding["locator"] = _resolve_anchor_query(
                records, equations, {"para_id": para_id},
                document_path=docx_path, source_fingerprint=source_fingerprint,
            )
        else:
            finding["locator"] = {
                "status": "not_applicable",
                "document_path": docx_path,
                "source_fingerprint": source_fingerprint,
                "candidates": [],
            }

    findings_by_category = {c: 0 for c in REVIEW_CATEGORIES}
    findings_by_severity: dict[str, int] = {}
    for f in findings:
        findings_by_category[f["category"]] = findings_by_category.get(f["category"], 0) + 1
        findings_by_severity[f["severity"]] = findings_by_severity.get(f["severity"], 0) + 1

    return {
        "status": "ok",
        "docx_path": docx_path,
        "source_fingerprint": source_fingerprint,
        "findings": findings,
        "finding_count": len(findings),
        "findings_by_category": findings_by_category,
        "findings_by_severity": findings_by_severity,
        "categories": list(REVIEW_CATEGORIES),
    }


def read_document_snapshot(
    docx_path: str,
    page_size: int | None = None,
    cursor: str | None = None,
    section_anchor: str | None = None,
    index_db_path: str | None = None,
) -> dict[str, Any]:
    """Read the saved DOCX snapshot without writing or requiring a close.

    Word normally leaves a sibling ~$ lock file while a document is open.
    That file is reported as a hint, not treated as a blocker. The returned
    content is the last saved on-disk snapshot; unsaved edits remain visible
    only inside Word until the document is saved.

    c7cc9da4 -- this is the entry point for a Meridian-docs REVIEW SESSION: a
    non-mutating pass over a .docx that may currently be open in Word. Two
    fields exist specifically to support that workflow:

      - "source_sha256": a SHA-256 fingerprint of the EXACT bytes this call
        just read (same family as index_docx_structure's source_sha256).
        A review session that accumulates recommendations anchored against
        this snapshot and later stages accepted edits into a disposable
        draft (never docx_path itself -- see move_section / copy_section /
        relocate_table / relocate_figure's draft_output_path parameter, and
        merge_docx_draft for promotion) must re-fingerprint docx_path
        immediately before that promotion and refuse to proceed on any
        mismatch. See render_gate.verify_promotion_readiness, which
        performs exactly that compare-and-refuse check plus structural/
        render verification, fail-closed, without mutating either file.
      - "limitations": explicit, human-readable caveats about what this
        snapshot does NOT prove -- most importantly, that content typed in
        Word since the last save is invisible here. A caller (human or
        agent) building recommendations from this snapshot should surface
        these limitations alongside anything derived from it, rather than
        silently treating "read succeeded" as "reflects what's on screen in
        Word right now".

    1dff1300 -- cursor-based pagination + section scoping, identical
    contract to :func:`document_outline` (see its docstring for the full
    cursor/staleness/rejection rules) applied to the ``paragraphs`` list
    instead of ``headings``. A cursor minted by ``document_outline`` is
    never accepted here, and vice versa (each is bound to its own
    ``kind``). Omitting BOTH ``page_size`` and ``cursor`` (the default)
    returns the FULL paragraph list exactly as before this item -- fully
    backward compatible; ``source_sha256`` (already present) doubles as
    this function's document-identity fingerprint, so no new top-level
    field is needed for that.

    Each paragraph in a PAGINATED response additionally carries
    ``section_path`` (ancestor heading texts, root first) and
    ``heading_para_id`` -- deterministic document order and stable
    per-paragraph identity were already true of ``para_id`` (see
    :func:`parse_docx`'s three-tier id scheme); this adds the section
    identity alongside it.

    ``index_db_path``, when given, attaches ``stale_index`` (this
    document's structural-sidecar freshness -- see
    :func:`get_structure_freshness`) and, best-effort, whole-document
    ``tables`` / ``figures`` identity metadata already recorded in that
    sidecar (see :func:`get_local_structure_elements`) plus ``equations``
    (see :func:`get_local_equations`, filtered to the ones whose
    ``para_id`` falls within the current page/section when paginating --
    ``para_id`` is directly comparable across both functions, unlike the
    sidecar's raw body-child ``index``, which counts tables/paragraphs
    together and is therefore NOT filtered per-page here). Never raises
    and never blocks on a missing/stale/incomplete sidecar -- absence or
    staleness is reported via ``stale_index``, never a hard failure.
    """
    try:
        with open(docx_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return {"error": str(exc)}
    try:
        paragraphs = parse_docx(raw)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {"error": str(exc)}

    fingerprint = _source_fingerprint(raw)

    scoped_paragraphs = paragraphs
    if section_anchor is not None:
        bounds = _resolve_section_anchor_bounds(paragraphs, section_anchor)
        if bounds is None:
            return {
                "error": f"section_anchor {section_anchor!r} does not resolve to any heading",
                "reason": "section_not_found",
            }
        start_idx, end_idx = bounds
        scoped_paragraphs = paragraphs[start_idx:end_idx]

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        xml_parts = sorted(
            name
            for name in archive.namelist()
            if name.startswith("word/")
            and name.endswith(".xml")
            and "/_rels/" not in name
            and not name.endswith(".rels")
        )
    lock_hint = os.path.join(
        os.path.dirname(docx_path),
        "~$" + os.path.basename(docx_path),
    )

    result: dict[str, Any] = {
        "status": "read_only",
        "docx_path": docx_path,
        "byte_size": len(raw),
        "saved_mtime": _stat_mtime(docx_path),
        "source_sha256": fingerprint,
        "word_lock_hint": os.path.exists(lock_hint),
        "xml_parts": xml_parts,
        "limitations": [
            "This reflects only the last SAVED state of docx_path. Any "
            "edits made in Word since the last save -- including changes "
            "Word is only holding in memory or in autosave/recovery "
            "buffers -- are NOT visible here until the document is "
            "actually saved to disk.",
            "word_lock_hint=True only means a ~$ lock file exists next to "
            "docx_path, which usually indicates Word (or another "
            "application) currently has it open. It is informational "
            "only -- it never blocked and never delayed this read, and "
            "its absence is not proof the file is closed (a crashed Word "
            "session can leave the lock file behind, and a non-Word "
            "writer may not create one at all).",
        ],
    }

    if index_db_path is not None:
        result["stale_index"] = get_structure_freshness(index_db_path)
        try:
            elements = get_local_structure_elements(index_db_path, allow_stale=True)
            result["tables"] = elements.get("tables", [])
            result["figures"] = elements.get("figures", [])
        except sqlite3.OperationalError:
            result["tables"] = []
            result["figures"] = []
        try:
            result["equations"] = get_local_equations(index_db_path)
        except sqlite3.OperationalError:
            result["equations"] = []

    if page_size is None and cursor is None:
        result["paragraph_count"] = len(scoped_paragraphs)
        result["heading_count"] = sum(
            1 for paragraph in scoped_paragraphs if _is_heading(paragraph["style"])
        )
        result["paragraphs"] = scoped_paragraphs
        return result

    offset, resolved_page_size, error = _resolve_pagination_window(
        kind="snapshot",
        fingerprint=fingerprint,
        total_items=len(scoped_paragraphs),
        page_size=page_size,
        cursor=cursor,
        section_anchor=section_anchor,
    )
    if error is not None:
        return error

    page_start, page_end, next_cursor, has_more = _paginated_page_result(
        offset, resolved_page_size, len(scoped_paragraphs),
        kind="snapshot", fingerprint=fingerprint, section_anchor=section_anchor,
    )
    annotated = _annotate_section_paths(scoped_paragraphs)
    page_paragraphs = annotated[page_start:page_end]

    if index_db_path is not None and "equations" in result:
        page_para_ids = {p.get("para_id") for p in page_paragraphs}
        result["equations"] = [
            eq for eq in result["equations"] if eq.get("para_id") in page_para_ids
        ]

    result["paragraph_count"] = len(page_paragraphs)
    result["heading_count"] = sum(1 for p in page_paragraphs if _is_heading(p.get("style")))
    result["paragraphs"] = page_paragraphs
    result["cursor"] = next_cursor
    result["has_more"] = has_more
    result["total"] = len(scoped_paragraphs)
    result["section_anchor"] = section_anchor
    return result
