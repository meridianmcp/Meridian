"""Vendored subset of docparse.docs_intel.document_content_tree.

Copied directly rather than declared as a cross-package dependency: uvx's
isolated `--from <local path>` tool environments do not reliably resolve
uv path-source dependencies (confirmed live, 2026-07-14 -- adding
`[tool.uv.sources]` to pyproject.toml did not resolve the import at
runtime). document_content_tree itself is fully stdlib-only and has no
dependency on docparse.structural_parser (that import in the source file
is used only by other, unrelated functions there) -- so this vendored copy
is the complete, correct function, not a stub or approximation.

Source of truth: packages/docparse/docparse/docs_intel.py (0d1b0809).
If that function changes, this copy needs the same change -- there is no
mechanism keeping them in sync automatically. Flagged as a known tradeoff,
not a silent duplication.

827b6bdc -- also vendors _find_duplicate_native_para_ids (native w14:paraId
duplicate detection) for the same reason: document_content_tree here calls
it, so it has to exist here too.
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _is_heading(style: str | None) -> bool:
    return bool(style) and str(style).lower().startswith("heading")


def _heading_level(style: str | None) -> int:
    match = re.search(r"(\d+)", style or "")
    return int(match.group(1)) if match else 1


_REFRESHABLE_FIELDS = frozenset(
    {"TOC", "SEQ", "PAGEREF", "REF", "NOTEREF", "PAGE", "NUMPAGES", "STYLEREF"}
)


def _field_type(instruction: str | None) -> str | None:
    if not instruction:
        return None
    token = instruction.strip().split(maxsplit=1)
    return token[0].upper() if token else None


def _field_needs_refresh(field_type: str | None) -> bool:
    return field_type in _REFRESHABLE_FIELDS


def _fields_in_paragraph(p: ET.Element) -> list[dict[str, Any]]:
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
    ppr = p.find(_q(_W, "pPr"))
    if ppr is None:
        return None
    pstyle = ppr.find(_q(_W, "pStyle"))
    return pstyle.get(_q(_W, "val")) if pstyle is not None else None


def _paragraph_text(p: ET.Element) -> str:
    return "".join(t.text or "" for t in p.iter(_q(_W, "t")))


def _build_synth_id_map(body: ET.Element) -> dict[int, str]:
    """Stable synthesized-id map for direct <w:p> children of *body* without a
    native w14:paraId.  See docparse.docs_intel._build_synth_id_map for the
    full docstring; this is the vendored copy kept in sync manually."""
    p_tag = _q(_W, "p")
    w14_paraId = _q(_W14, "paraId")
    heading_path: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    result: dict[int, str] = {}
    for child in body:
        if child.tag != p_tag:
            continue
        native_id = child.get(w14_paraId)
        style = _paragraph_style(child)
        if _is_heading(style):
            level = _heading_level(style)
            text = _paragraph_text(child)
            heading_path = heading_path[: level - 1] + [text]
        if native_id:
            continue
        path_str = " > ".join(heading_path) if heading_path else "<root>"
        norm_text = re.sub(r"\s+", " ", _paragraph_text(child).lower()).strip()
        slot_key = (path_str, norm_text)
        occ = seen.get(slot_key, 0)
        seen[slot_key] = occ + 1
        raw = f"{path_str}\x00{norm_text}\x00{occ}"
        digest = hashlib.sha1(raw.encode()).hexdigest()[:16]
        result[id(child)] = f"sp{digest}"
    return result


def _find_duplicate_native_para_ids(body: ET.Element) -> list[dict[str, Any]]:
    """Vendored copy of docparse.docs_intel._find_duplicate_native_para_ids
    (827b6bdc). See the canonical source for the full docstring; this is the
    vendored copy kept in sync manually."""
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
    """Vendored copy of docparse.docs_intel.document_content_tree (0d1b0809).
    See module docstring for why this is a local copy, not a cross-package
    import."""
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
    for index, child in enumerate(list(body)):
        if child.tag == p_tag:
            blocks.append(_paragraph_node(child, index, synth_map.get(id(child))))
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
        "duplicate_para_ids": duplicate_para_ids,
    }
