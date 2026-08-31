"""Read-only equation-to-notation binding and proposal audit.

The existing equation graph inventories structure and the nomenclature linter
checks declared spellings. This module joins each observed equation occurrence
to the semantic notation registry, preserving the equation locator, nearest
heading, scope decision, match type, and declared typography. It proposes
findings; it never renames or edits a document.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from typing import Any

from . import docs_intel
from . import equation_graph
from .notation_manifest import validate_notation_manifest


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def _q(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def _read_docx(source: str | bytes | bytearray) -> tuple[bytes, ET.Element, str | None]:
    if isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
        path = None
        package = zipfile.ZipFile(io.BytesIO(raw))
    else:
        path = source
        with open(source, "rb") as handle:
            raw = handle.read()
        package = zipfile.ZipFile(io.BytesIO(raw))
    try:
        xml = package.read("word/document.xml")
    finally:
        package.close()
    return raw, ET.fromstring(xml), path


def _compact(value: str) -> str:
    """Compact a notation spelling for comparison with flattened OMML text."""

    return re.sub(r"[\s_{}]", "", value)


def _match_spelling(text: str, spelling: str) -> bool:
    compact_text = _compact(text)
    compact_spelling = _compact(spelling)
    if not compact_spelling:
        return False
    escaped = re.escape(compact_spelling)
    if len(compact_spelling) == 1 or compact_spelling.isalnum():
        return re.search(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", compact_text) is not None
    return compact_spelling in compact_text


def _omml_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(node.text or "" for node in element.iter(_q(_M, "t")))


def _compact_term(value: str, *, case_sensitive: bool = True) -> str:
    """Normalize a structured term while retaining its script identity."""

    compact = _compact(value)
    return compact if case_sensitive else compact.casefold()


def _omml_term_records(raw: str) -> list[dict[str, Any]]:
    """Extract script-aware OMML terms from one equation.

    OMML stores a subscript as ``m:sSub`` (and superscripts analogously), not
    as a literal underscore. This distinction is essential: a flattened
    ``Cdt`` string and a native ``C_DT`` tree may look similar after text
    extraction but are not equivalent publication representations.
    """

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    parent_map = {id(child): parent for parent in root.iter() for child in parent}
    script_tags = {_q(_M, name) for name in ("sSub", "sSup", "sSubSup")}
    records: list[dict[str, Any]] = []

    for element in root.iter():
        if element.tag not in script_tags:
            continue
        parts = [child for child in list(element) if child.tag.rsplit("}", 1)[-1] not in {"sSubPr", "sSupPr", "sSubSupPr"}]
        if not parts:
            continue
        base = _omml_text(parts[0])
        subscript = ""
        superscript = ""
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name == "sSub" and len(parts) > 1:
            subscript = _omml_text(parts[1])
        elif local_name == "sSup" and len(parts) > 1:
            superscript = _omml_text(parts[1])
        elif local_name == "sSubSup" and len(parts) > 2:
            subscript = _omml_text(parts[1])
            superscript = _omml_text(parts[2])
        if not base:
            continue
        term = base
        if subscript:
            term += "_" + subscript
        if superscript:
            term += "^" + superscript
        records.append(
            {
                "term": term,
                "base": base,
                "subscript": subscript or None,
                "superscript": superscript or None,
                "structure": local_name,
                "structured": True,
            }
        )

    # A run outside a script is retained as a legacy/bare term. It allows the
    # audit to recognize ordinary expressions such as ``R+C`` while avoiding
    # duplicate leaf matches for the children of a native script node.
    for run in root.iter(_q(_M, "r")):
        ancestor = parent_map.get(id(run))
        inside_script = False
        while ancestor is not None:
            if ancestor.tag in script_tags:
                inside_script = True
                break
            ancestor = parent_map.get(id(ancestor))
        if inside_script:
            continue
        text = _omml_text(run)
        if text:
            records.append(
                {
                    "term": text,
                    "base": None,
                    "subscript": None,
                    "superscript": None,
                    "structure": None,
                    "structured": False,
                }
            )
    return records


def _heading_style(paragraph: ET.Element) -> str | None:
    ppr = paragraph.find(_q(_W, "pPr"))
    if ppr is None:
        return None
    pstyle = ppr.find(_q(_W, "pStyle"))
    return pstyle.get(_q(_W, "val")) if pstyle is not None else None


def _is_heading(paragraph: ET.Element) -> bool:
    style = (_heading_style(paragraph) or "").casefold()
    return "heading" in style or bool(re.match(r"(?:h|heading)[1-9]$", style))


def _heading_contexts(root: ET.Element) -> dict[str, str | None]:
    body = root.find(_q(_W, "body"))
    if body is None:
        return {}
    contexts: dict[str, str | None] = {}
    current: str | None = None
    for record in equation_graph._iter_paragraphs(body):
        paragraph, _table_path, _number, location = record[:4]
        visible = equation_graph._visible_text(paragraph).strip()
        numbered_heading = bool(
            re.match(r"^(?:appendix\s+)?[A-Z]?\d+(?:\.\d+)*\.?\s+\S", visible, re.I)
        )
        if (_is_heading(paragraph) or numbered_heading) and visible:
            current = visible
        para_id = paragraph.get(_q(_W14, "paraId")) or location.split(":")[-1]
        contexts.setdefault(para_id, current)
        contexts.setdefault(location, current)
    return contexts


def _scope_matches(scope: str, heading: str | None) -> bool:
    if scope.casefold() in {"document", "global", "all"}:
        return True
    if not heading:
        return False
    target = scope.split(":", 1)[-1].strip().casefold()
    return bool(target) and target in heading.casefold()


def _omml_style_observation(raw: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {"styles": [], "script_structures": [], "style_metadata_present": False}
    styles: set[str] = set()
    for element in root.iter():
        if element.tag == _q(_M, "sty"):
            value = element.get(_q(_M, "val")) or element.get("val")
            if value:
                styles.add({"p": "upright", "i": "italic", "b": "bold", "bi": "bold_italic"}.get(value, value))
        if element.tag in {_q(_W, "i"), _q(_W, "b")}:
            styles.add("italic" if element.tag == _q(_W, "i") else "bold")
    script_structures = sorted(
        {
            element.tag.rsplit("}", 1)[-1]
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] in {"sSub", "sSup", "sSubSup"}
        }
    )
    return {
        "styles": sorted(styles),
        "script_structures": script_structures,
        "style_metadata_present": bool(styles),
    }


def _document_equations(root: ET.Element) -> list[dict[str, Any]]:
    """Extract display/inline equations with row-level visible numbers.

    The general equation graph intentionally uses a conservative two-cell
    numbered-table heuristic. Publication documents also use equation layout
    rows with an extra alignment cell or a number field, so this audit uses the
    broader contract: any ancestor row text matching ``(N)`` is evidence for a
    visible equation number.
    """

    parent_map = {id(child): parent for parent in root.iter() for child in parent}
    paragraphs = list(root.iter(_q(_W, "p")))
    heading_by_paragraph: dict[int, str | None] = {}
    current_heading: str | None = None
    for paragraph in paragraphs:
        visible = "".join(node.text or "" for node in paragraph.iter(_q(_W, "t"))).strip()
        if _is_heading(paragraph) and visible:
            current_heading = visible
        heading_by_paragraph[id(paragraph)] = current_heading

    def ancestors(element: ET.Element):
        parent = parent_map.get(id(element))
        while parent is not None:
            yield parent
            parent = parent_map.get(id(parent))

    records: list[dict[str, Any]] = []
    for element in root.iter():
        is_display = element.tag == _q(_M, "oMathPara")
        is_inline = element.tag == _q(_M, "oMath") and not any(
            ancestor.tag == _q(_M, "oMathPara") for ancestor in ancestors(element)
        )
        if not (is_display or is_inline):
            continue
        paragraph = next(
            (ancestor for ancestor in ancestors(element) if ancestor.tag == _q(_W, "p")),
            None,
        )
        row = next(
            (ancestor for ancestor in ancestors(element) if ancestor.tag == _q(_W, "tr")),
            None,
        )
        row_text = "".join(node.text or "" for node in row.iter(_q(_W, "t"))) if row is not None else ""
        number_match = re.search(r"\(\s*\d+\s*\)", row_text)
        raw = ET.tostring(element, encoding="unicode")
        para_id = paragraph.get(_q(_W14, "paraId")) if paragraph is not None else None
        if not para_id:
            para_id = f"p{paragraphs.index(paragraph)}" if paragraph is not None else None
        records.append(
            {
                "ordinal": len(records),
                "para_id": para_id,
                "omml_raw": raw,
                "pattern": "table-numbered" if number_match else ("standalone" if is_display else "inline"),
                "number": number_match.group(0) if number_match else None,
                "flat_text": docs_intel._omml_flatten_text_local(raw),
                "heading": heading_by_paragraph.get(id(paragraph)) if paragraph is not None else None,
            }
        )
    return records


def audit_equation_notation(
    source: str | bytes | bytearray,
    notation_manifest: dict[str, Any],
    *,
    max_occurrences: int = 50000,
) -> dict[str, Any]:
    """Bind equation occurrences to a semantic notation manifest.

    Matching is deliberately evidence-preserving: exact canonical spellings,
    aliases, and flattened aliases are distinguished. A single glyph with
    multiple active roles becomes a blocking ambiguity; reuse in disjoint
    scopes becomes a review finding. The source DOCX is never modified.
    """

    if not isinstance(max_occurrences, int) or isinstance(max_occurrences, bool) or not 1 <= max_occurrences <= 500000:
        return {"error": "max_occurrences must be an integer from 1 through 500000", "error_type": "options_invalid"}
    semantic = validate_notation_manifest(notation_manifest)
    if not semantic.get("manifest"):
        return semantic
    try:
        source_bytes, root, document_path = _read_docx(source)
        equations = _document_equations(root)
    except (OSError, KeyError, ValueError, ET.ParseError, zipfile.BadZipFile) as exc:
        return {"document_path": document_path if "document_path" in locals() else None, "error": str(exc), "error_type": "document_invalid"}

    contexts = _heading_contexts(root)
    entries = semantic["manifest"]["symbols"]
    findings: list[dict[str, Any]] = []
    for finding in semantic.get("findings", []):
        copied = dict(finding)
        copied["source"] = "notation_manifest"
        findings.append(copied)

    occurrences: list[dict[str, Any]] = []
    counts: defaultdict[str, int] = defaultdict(int)
    case_sensitive = semantic["manifest"]["case_sensitive"]
    for equation in equations:
        heading = contexts.get(equation.get("para_id"))
        style = _omml_style_observation(equation.get("omml_raw", ""))
        term_records = _omml_term_records(equation.get("omml_raw", ""))
        matched: list[dict[str, Any]] = []
        for entry in entries:
            spellings = [(entry["symbol"], "canonical")]
            spellings.extend((alias, "alias") for alias in entry["aliases"])
            spellings.extend(
                (preferred, "preferred_structured")
                for preferred in entry["preferred_notation"]
            )
            spellings.extend((flat, "flattened_alias") for flat in entry["flattened_aliases"])
            match: tuple[str, str, dict[str, Any] | None] | None = None
            # Prefer exact structural evidence. A native ``sSub``/``sSup``
            # match is stronger than a text-only match against the flattened
            # equation string.
            for spelling, match_kind in spellings:
                if match_kind == "flattened_alias":
                    continue
                target = _compact_term(spelling, case_sensitive=case_sensitive)
                term = next(
                    (
                        item for item in term_records
                        if _compact_term(item["term"], case_sensitive=case_sensitive) == target
                    ),
                    None,
                )
                if term is not None:
                    match = (spelling, match_kind, term)
                    break

            # If a legacy flattened alias is textually identical after
            # compaction, prefer the explicit legacy classification. Otherwise
            # ``C_DT`` and ``Cdt`` would be indistinguishable in a bare m:r.
            if match is not None and match[2] is not None and not match[2]["structured"]:
                flattened = next(
                    (
                        flat for flat in entry["flattened_aliases"]
                        if _compact_term(flat, case_sensitive=case_sensitive)
                        == _compact_term(match[2]["term"], case_sensitive=case_sensitive)
                    ),
                    None,
                )
                if flattened is not None:
                    match = (flattened, "flattened_alias", match[2])

            # Preserve evidence when the manifest declares a bare base glyph
            # but OMML contains a qualified native subscript/superscript.
            if match is None:
                base_target = _compact_term(entry["symbol"], case_sensitive=case_sensitive)
                term = next(
                    (
                        item for item in term_records
                        if item["structured"]
                        and _compact_term(item["base"], case_sensitive=case_sensitive) == base_target
                    ),
                    None,
                )
                if term is not None:
                    match = (entry["symbol"], "base_of_structured_term", term)

            if match is None:
                fallback_spellings = [
                    item for item in spellings if item[1] == "flattened_alias"
                ] + [
                    item for item in spellings if item[1] != "flattened_alias"
                ]
                match = next(
                    (
                        (spelling, match_kind, None)
                        for spelling, match_kind in fallback_spellings
                        if _match_spelling(equation.get("flat_text", ""), spelling)
                    ),
                    None,
                )
            if match is None:
                continue
            spelling, match_kind, term = match
            scope_matches = [scope for scope in entry["scope"] if _scope_matches(scope, heading)]
            occurrence = {
                "symbol_id": entry["id"],
                "symbol": entry["symbol"],
                "role": entry["role"],
                "kind": entry["kind"],
                "scope": entry["scope"],
                "scope_match": scope_matches,
                "spelling": spelling,
                "match_kind": match_kind,
                "equation_ordinal": equation.get("ordinal"),
                "equation_number": equation.get("number"),
                "para_id": equation.get("para_id"),
                "pattern": equation.get("pattern"),
                "heading": heading,
                "flat_text": equation.get("flat_text", ""),
                "term": term.get("term") if term else None,
                "base": term.get("base") if term else None,
                "subscript": term.get("subscript") if term else None,
                "superscript": term.get("superscript") if term else None,
                "binding_confidence": (
                    "structured" if term and term["structured"] else "textual"
                ),
                "typography": entry["typography"],
                "omml_style_observation": style,
            }
            matched.append(occurrence)
            occurrences.append(occurrence)
            counts[entry["id"]] += 1
            if len(occurrences) > max_occurrences:
                return {"error": "notation occurrence limit exceeded", "error_type": "limit_exceeded"}

            if match_kind == "flattened_alias":
                findings.append(
                    {
                        "type": "flattened_subscript_occurrence",
                        "severity": "error",
                        "symbol_id": entry["id"],
                        "symbol": entry["symbol"],
                        "flattened": spelling,
                        "equation_ordinal": equation.get("ordinal"),
                        "equation_number": equation.get("number"),
                        "para_id": equation.get("para_id"),
                    }
                )
            if match_kind == "base_of_structured_term" and entry["preferred_notation"]:
                findings.append(
                    {
                        "type": "unqualified_symbol_occurrence",
                        "severity": "warning",
                        "symbol_id": entry["id"],
                        "symbol": entry["symbol"],
                        "observed_term": term.get("term") if term else None,
                        "preferred_notation": entry["preferred_notation"],
                        "equation_ordinal": equation.get("ordinal"),
                        "equation_number": equation.get("number"),
                        "para_id": equation.get("para_id"),
                        "message": "manifest matched the base glyph inside a qualified native OMML term; review whether the role-specific notation is declared consistently",
                    }
                )
            if entry["scope"] and not scope_matches:
                findings.append(
                    {
                        "type": "symbol_scope_mismatch",
                        "severity": "warning",
                        "symbol_id": entry["id"],
                        "symbol": entry["symbol"],
                        "declared_scope": entry["scope"],
                        "heading": heading,
                        "equation_ordinal": equation.get("ordinal"),
                        "equation_number": equation.get("number"),
                        "para_id": equation.get("para_id"),
                    }
                )

        active_by_glyph: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in matched:
            if item["scope_match"]:
                glyph = _compact(item["symbol"])
                if not case_sensitive:
                    glyph = glyph.casefold()
                active_by_glyph[glyph].append(item)
        for glyph, active in sorted(active_by_glyph.items()):
            roles = sorted({item["role"] for item in active if item["role"]})
            if len(roles) > 1:
                findings.append(
                    {
                        "type": "ambiguous_symbol_occurrence",
                        "severity": "error",
                        "symbol": glyph,
                        "roles": roles,
                        "symbol_ids": sorted({item["symbol_id"] for item in active}),
                        "equation_ordinal": equation.get("ordinal"),
                        "equation_number": equation.get("number"),
                        "para_id": equation.get("para_id"),
                        "heading": heading,
                    }
                )

    for entry in entries:
        if entry["required"] and not counts[entry["id"]]:
            findings.append(
                {
                    "type": "required_symbol_unobserved",
                    "severity": "error",
                    "symbol_id": entry["id"],
                    "symbol": entry["symbol"],
                    "role": entry["role"],
                    "scope": entry["scope"],
                }
            )

    findings.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    has_errors = any(item.get("severity") == "error" for item in findings)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    finding_types = Counter(item.get("type", "unknown") for item in findings)
    match_kinds = Counter(item.get("match_kind", "unknown") for item in occurrences)
    return {
        "schema_version": "2",
        "document_path": document_path,
        "source_fingerprint": source_sha256,
        "manifest": semantic["manifest"],
        "manifest_sha256": semantic["manifest_sha256"],
        "equation_count": len(equations),
        "occurrence_count": len(occurrences),
        "occurrences": occurrences,
        "counts_by_symbol_id": dict(sorted(counts.items())),
        "findings": findings,
        "finding_count": len(findings),
        "valid": not has_errors,
        "review_required": bool(findings),
        "status": "blocked" if has_errors else ("review_required" if findings else "clear"),
        "summary": {
            "findings_by_type": dict(sorted(finding_types.items())),
            "match_kinds": dict(sorted(match_kinds.items())),
            "structured_occurrences": sum(
                1 for item in occurrences if item.get("binding_confidence") == "structured"
            ),
            "textual_occurrences": sum(
                1 for item in occurrences if item.get("binding_confidence") == "textual"
            ),
        },
        "provenance": {
            "generator": "meridian-docs.notation_audit",
            "schema_version": "2",
            "source_representation": "native_omml",
            "equation_extraction": "raw-word-document-xml-v2",
            "manifest_case_sensitive": case_sensitive,
            "deterministic": True,
            "document_mutated": False,
        },
    }


__all__ = ["audit_equation_notation"]
