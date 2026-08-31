"""Deterministic, read-only equation graphing for DOCX research documents.

This module is deliberately an inventory and conflict detector, not a document
rewriter.  Native OMML remains the source representation.  The graph records
where equations live (display-line, inline, or table-embedded), which visible
numbers and symbols are associated with them, and conservative lexical
references between equations.  Inferred definition edges are always marked as
heuristic candidates; they are never treated as authoritative scientific
definitions.

The dependency graph is allowed to contain cycles because real drafts can have
mutually-referential text.  ``dag`` is a separate validation view over the
explicit equation-to-equation dependency edges, so callers can fail a release
gate without pretending that every document-wide relation is a DAG.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

from . import equation_references, nomenclature


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_XML = "http://www.w3.org/XML/1998/namespace"


def _q(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


_WP = _q(_W, "p")
_WTBL = _q(_W, "tbl")
_WTR = _q(_W, "tr")
_WTC = _q(_W, "tc")
_WT = _q(_W, "t")
_W14_PARA_ID = _q(_W14, "paraId")
_OMATH = _q(_M, "oMath")
_OMATH_PARA = _q(_M, "oMathPara")


# These patterns intentionally require a word such as equation/equation
# number/eq.  A naked number is not enough evidence to create a dependency.
_REFERENCE_PATTERNS = (
    re.compile(
        r"\b(?:equations?|formulae?|formulas?)\s*(?:numbers?\s*)?"
        r"[\(\[]\s*([A-Za-z]?\d+(?:[.:-][A-Za-z]?\d+)*)\s*[\)\]]",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:eqs?\.?|equations?|formulae?|formulas?)\s+"
        r"(?:numbers?\s+)?([A-Za-z]?\d+(?:[.:-][A-Za-z]?\d+)*)\b",
        re.IGNORECASE,
    ),
)

_DEFINITION_WORDS = re.compile(
    r"(?:\bis\b|\bdenotes?\b|\brepresents?\b|\bdefined\s+as\b|:|=)",
    re.IGNORECASE,
)


class EquationGraphError(ValueError):
    """Raised for malformed graph options or an unreadable DOCX."""


def _canonical_element(element: ET.Element) -> dict[str, Any]:
    """Return a namespace/prefix-independent structural representation."""
    attrs = sorted(
        (_local_name(key), str(value)) for key, value in element.attrib.items()
    )
    text = (element.text or "").strip()
    return {
        "tag": _local_name(element.tag),
        "attrs": attrs,
        "text": text,
        "children": [_canonical_element(child) for child in list(element)],
    }


def _digest_payload(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _read_document_xml(source: str | bytes | bytearray) -> tuple[bytes, bytes, str | None]:
    document_path = source if isinstance(source, str) else None
    if isinstance(source, (bytes, bytearray)):
        source_bytes = bytes(source)
        zf = zipfile.ZipFile(io.BytesIO(source_bytes))
    else:
        source_bytes = Path(source).read_bytes()
        zf = zipfile.ZipFile(source)
    try:
        xml_bytes = zf.read("word/document.xml")
    finally:
        zf.close()
    return source_bytes, xml_bytes, document_path


def _visible_text(element: ET.Element) -> str:
    """Extract visible Word text while excluding OMML run text."""
    return "".join((node.text or "") for node in element.iter(_WT))


def _omml_text(element: ET.Element) -> str:
    return "".join((node.text or "") for node in element.iter(_q(_M, "t")))


def _element_fingerprint(element: ET.Element) -> str:
    return _digest_payload(_canonical_element(element))


def _iter_paragraphs(
    body: ET.Element,
) -> Iterator[tuple[ET.Element, str | None, str | None, str | None, list[str], list[str]]]:
    """Yield paragraphs in document order with stable table/cell locations.

    The yielded tuple is ``(paragraph, table_path, equation_number,
    body_location, section_path, section_path_ids)``.  A table path is
    ``tN/rN/cN`` (one-based), and a paragraph without a real ``w14:paraId``
    gets a deterministic ``pN``.
    """
    paragraph_index = 0
    table_index = 0
    # Reuse the document parser's established heading-stack semantics so the
    # graph does not invent a second, subtly different section model.
    from . import docs_intel

    section_paths = docs_intel._build_equation_section_paths(body)
    section_path_ids: dict[int, list[str]] = {}
    heading_stack: list[tuple[int, str]] = []
    for body_child_index, child in enumerate(list(body)):
        if child.tag == _WP:
            ppr = child.find(_q(_W, "pPr"))
            style = None
            if ppr is not None:
                pstyle = ppr.find(_q(_W, "pStyle"))
                if pstyle is not None:
                    style = pstyle.get(_q(_W, "val"))
            if docs_intel._is_heading(style):
                level = docs_intel._heading_level(style)
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                anchor = child.get(_W14_PARA_ID) or f"p{body_child_index}"
                heading_stack.append((level, anchor))
        section_path_ids[body_child_index] = [anchor for _, anchor in heading_stack]

    def walk(
        parent: ET.Element,
        table_path: str | None = None,
        number: str | None = None,
        section_path: list[str] | None = None,
        section_ids: list[str] | None = None,
        top_level: bool = False,
    ) -> Iterator[tuple[ET.Element, str | None, str | None, str | None, list[str], list[str]]]:
        nonlocal paragraph_index, table_index
        for child_index, child in enumerate(list(parent)):
            child_section_path = (
                list(section_paths.get(child_index, []))
                if top_level
                else list(section_path or [])
            )
            child_section_ids = (
                list(section_path_ids.get(child_index, []))
                if top_level
                else list(section_ids or [])
            )
            if child.tag == _WP:
                para_id = child.get(_W14_PARA_ID) or f"p{paragraph_index}"
                location = f"{table_path}:{para_id}" if table_path else para_id
                yield child, table_path, number, location, child_section_path, child_section_ids
                paragraph_index += 1
            elif child.tag == _WTBL:
                table_index += 1
                current_table = f"t{table_index}"
                rows = [node for node in child if node.tag == _WTR]
                for row_index, row in enumerate(rows, 1):
                    cells = [node for node in row if node.tag == _WTC]
                    row_path = f"{current_table}/r{row_index}"
                    row_number = None
                    if len(cells) >= 2 and any(node.tag == _OMATH for node in cells[0].iter()):
                        for candidate in cells[1:]:
                            candidate_text = _visible_text(candidate).strip()
                            if re.fullmatch(r"\(\s*[A-Za-z0-9.:-]+\s*\)", candidate_text):
                                row_number = candidate_text
                                break
                    for cell_index, cell in enumerate(cells, 1):
                        cell_path = f"{row_path}/c{cell_index}"
                        yield from walk(cell, cell_path, row_number, child_section_path, child_section_ids)
            else:
                # Content controls, SDTs, custom XML, and similar wrappers can
                # contain ordinary paragraphs/tables.  Recurse without making
                # the wrapper part of the stable location key.
                yield from walk(child, table_path, number, child_section_path, child_section_ids)

    yield from walk(body, top_level=True)


def _equation_context(
    paragraph: ET.Element,
    table_path: str | None,
    table_number: str | None,
    location: str,
    local_index: int,
    equation: ET.Element,
    section_path: list[str],
    section_path_ids: list[str],
) -> dict[str, Any]:
    visible = _visible_text(paragraph).strip()
    display = any(node.tag == _OMATH_PARA for node in paragraph.iter())
    display_mode = "line_separated" if display or not visible else "inline"
    container = "table_cell" if table_path else "body_paragraph"
    if table_path:
        placement = (
            "table_numbered"
            if table_number
            else (
                "table_line_separated"
                if display_mode == "line_separated"
                else "table_embedded"
            )
        )
    else:
        placement = display_mode
    structural = _element_fingerprint(equation)
    location_key = f"{location}/e{local_index}"
    equation_id = "equation:" + _digest_payload(
        {"location": location_key, "structure": structural}
    )[:20]
    return {
        "id": equation_id,
        "kind": "equation",
        "ordinal": None,
        "location": location_key,
        "para_id": paragraph.get(_W14_PARA_ID) or location.rsplit(":", 1)[-1],
        "table_path": table_path,
        "section_path": list(section_path),
        "section_path_ids": list(section_path_ids),
        "container": container,
        "display_mode": display_mode,
        "placement": placement,
        "number": table_number,
        "flat_text": _omml_text(equation),
        "structure_sha256": structural,
        "omml_sha256": structural,
        "paragraph_text": visible,
    }


def _occurrences(text: str, spelling: str, case_sensitive: bool) -> int:
    return nomenclature._occurrence_count(text, spelling, case_sensitive=case_sensitive)


def _reference_numbers(text: str) -> list[str]:
    found: set[str] = set()
    for pattern in _REFERENCE_PATTERNS:
        found.update(match.group(1) for match in pattern.finditer(text))
    return sorted(found, key=lambda value: (value.casefold(), value))


def _looks_like_definition(text: str, spelling: str, case_sensitive: bool) -> bool:
    if not _occurrences(text, spelling, case_sensitive):
        return False
    escaped = re.escape(spelling)
    flags = 0 if case_sensitive else re.IGNORECASE
    # Keep this deliberately local: a definition candidate must have the
    # spelling and a definition verb/operator in the same short clause.
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_]).{{0,80}}", flags | re.DOTALL)
    return any(_DEFINITION_WORDS.search(match.group(0)) for match in pattern.finditer(text))


def _sorted_nodes(nodes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [nodes[key] for key in sorted(nodes)]


def _edge_key(edge: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(edge.get("source", "")),
        str(edge.get("target", "")),
        str(edge.get("type", "")),
        str(edge.get("spelling", "")),
    )


def _reference_signal_id(kind: str, record: dict[str, Any]) -> str:
    """Return a stable node id for one extracted Word reference signal."""
    identity = {
        key: value
        for key, value in record.items()
        if key not in {"record_index", "node_order"}
    }
    return f"{kind}:" + _digest_payload(identity)[:20]


def _dag_validation(
    equation_ids: list[str],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    dependencies: dict[str, set[str]] = {node_id: set() for node_id in equation_ids}
    outgoing: dict[str, set[str]] = {node_id: set() for node_id in equation_ids}
    for edge in edges:
        if edge.get("type") != "depends_on":
            continue
        source = edge.get("source")
        target = edge.get("target")
        if source in dependencies and target in dependencies and source != target:
            dependencies[source].add(target)
            outgoing[target].add(source)

    ready = sorted(node_id for node_id, required in dependencies.items() if not required)
    ordered: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for dependent in sorted(outgoing[current]):
            dependencies[dependent].discard(current)
            if not dependencies[dependent] and dependent not in ordered and dependent not in ready:
                ready.append(dependent)
        ready.sort()
    cycles = sorted(node_id for node_id in equation_ids if node_id not in ordered)
    return {
        "acyclic": not cycles,
        "ordered_equations": ordered,
        "cycle_equations": cycles,
        "edge_count": sum(1 for edge in edges if edge.get("type") == "depends_on"),
    }


def build_equation_graph(
    source: str | bytes | bytearray,
    notation_manifest: dict[str, Any] | None = None,
    *,
    max_nodes: int = 10000,
) -> dict[str, Any]:
    """Build a bounded, deterministic equation inventory and dependency report.

    ``notation_manifest`` uses the same schema as :func:`lint_nomenclature`.
    Without one, the graph still inventories equations and lexical references,
    but does not pretend that arbitrary tokens are scientific symbols.

    The result is JSON-safe and includes ``graph_sha256`` over all canonical
    content except that digest itself.  No files are written and the DOCX is
    never modified.
    """
    if not isinstance(max_nodes, int) or isinstance(max_nodes, bool) or not 1 <= max_nodes <= 50000:
        return {"error": "max_nodes must be an integer from 1 through 50000", "error_type": "options_invalid"}
    try:
        source_bytes, xml_bytes, document_path = _read_document_xml(source)
        root = ET.fromstring(xml_bytes)
        body = root.find(_q(_W, "body"))
        if body is None:
            raise EquationGraphError("word/document.xml has no w:body")
        manifest = nomenclature.normalize_nomenclature_manifest(notation_manifest)
    except (OSError, KeyError, ValueError, ET.ParseError, zipfile.BadZipFile, EquationGraphError, nomenclature.NomenclatureManifestError) as exc:
        return {"document_path": document_path, "error": str(exc), "error_type": "document_invalid"}

    source_fingerprint = hashlib.sha256(source_bytes).hexdigest()
    manifest_sha256 = _digest_payload(manifest)
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    equations: list[dict[str, Any]] = []
    paragraph_records: list[tuple[str, str]] = []
    paragraph_ids_by_index: dict[int, str] = {}
    equation_by_number: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    unresolved_references: set[str] = set()

    def add_node(node: dict[str, Any]) -> bool:
        if node["id"] in nodes:
            return True
        if len(nodes) >= max_nodes:
            return False
        nodes[node["id"]] = node
        return True

    def add_edge(edge: dict[str, Any]) -> None:
        edges.setdefault(_edge_key(edge), edge)

    for paragraph_index, (
        paragraph,
        table_path,
        table_number,
        location,
        section_path,
        section_path_ids,
    ) in enumerate(
        _iter_paragraphs(body)
    ):
        text = _visible_text(paragraph).strip()
        omaths = list(paragraph.iter(_OMATH))
        if not text and not omaths:
            continue
        paragraph_id = "paragraph:" + _digest_payload({"location": location})[:20]
        if not add_node({
            "id": paragraph_id,
            "kind": "paragraph",
            "location": location,
            "table_path": table_path,
            "section_path": list(section_path),
            "section_path_ids": list(section_path_ids),
            "text": text,
        }):
            return {"document_path": document_path, "source_fingerprint": source_fingerprint, "error": "graph node limit exceeded", "error_type": "limit_exceeded"}
        paragraph_ids_by_index[paragraph_index] = paragraph_id
        paragraph_records.append((paragraph_id, text))
        for local_index, omath in enumerate(omaths, 1):
            record = _equation_context(
                paragraph,
                table_path,
                table_number,
                location,
                local_index,
                omath,
                section_path,
                section_path_ids,
            )
            record["ordinal"] = len(equations)
            if not add_node(record):
                return {"document_path": document_path, "source_fingerprint": source_fingerprint, "error": "graph node limit exceeded", "error_type": "limit_exceeded"}
            equations.append(record)
            if record["number"]:
                equation_by_number[str(record["number"]).strip("() ")].append(record)
            add_edge({"source": paragraph_id, "target": record["id"], "type": "contains"})

    reference_extraction = equation_references.extract_equation_references(xml_bytes)
    reference_signals = reference_extraction.get("records", [])
    bookmark_nodes_by_name: defaultdict[str, list[str]] = defaultdict(list)
    for signal in reference_signals:
        paragraph_id = paragraph_ids_by_index.get(signal.get("paragraph_index"))
        if signal.get("kind") == "bookmark":
            bookmark_id = _reference_signal_id("bookmark", signal)
            bookmark_node = {
                "id": bookmark_id,
                "kind": "bookmark",
                "bookmark_name": signal["bookmark_name"],
                "bookmark_id": signal.get("bookmark_id"),
                "paragraph_index": signal.get("paragraph_index"),
                "paragraph_id": paragraph_id,
                "confidence": signal.get("confidence", "high"),
                "source": signal.get("source", "ooxml_bookmark_name"),
            }
            if not add_node(bookmark_node):
                return {"document_path": document_path, "source_fingerprint": source_fingerprint, "error": "graph node limit exceeded", "error_type": "limit_exceeded"}
            bookmark_nodes_by_name[signal["bookmark_name"]].append(bookmark_id)
            if paragraph_id:
                add_edge({
                    "source": paragraph_id,
                    "target": bookmark_id,
                    "type": "contains_bookmark",
                    "confidence": "ooxml_exact",
                })
        elif signal.get("kind") == "field":
            field_id = _reference_signal_id("word-field", signal)
            field_node = {
                "id": field_id,
                "kind": "word_field",
                "field_type": signal["field_type"],
                "instruction": signal["instruction"],
                "visible_text": signal.get("visible_text", ""),
                "paragraph_index": signal.get("paragraph_index"),
                "paragraph_id": paragraph_id,
                "run_index": signal.get("run_index"),
                "confidence": signal.get("confidence", "high"),
                "source": signal.get("source", "ooxml_field_instruction"),
            }
            if signal.get("bookmark_name") is not None:
                field_node["bookmark_name"] = signal["bookmark_name"]
            if signal.get("sequence_identifier") is not None:
                field_node["sequence_identifier"] = signal["sequence_identifier"]
            if not add_node(field_node):
                return {"document_path": document_path, "source_fingerprint": source_fingerprint, "error": "graph node limit exceeded", "error_type": "limit_exceeded"}
            if paragraph_id:
                add_edge({
                    "source": paragraph_id,
                    "target": field_id,
                    "type": "contains_reference_signal",
                    "confidence": "ooxml_exact",
                })
            if signal["field_type"] == "REF":
                bookmark_name = signal.get("bookmark_name")
                targets = bookmark_nodes_by_name.get(bookmark_name or "", [])
                if len(targets) == 1:
                    add_edge({
                        "source": field_id,
                        "target": targets[0],
                        "type": "refers_to_bookmark",
                        "confidence": "ooxml_exact",
                    })
                else:
                    unresolved_references.add(f"bookmark:{bookmark_name or ''}")
                    add_edge({
                        "source": field_id,
                        "target": "unresolved-bookmark:" + (bookmark_name or ""),
                        "type": "unresolved_bookmark_reference",
                        "bookmark_name": bookmark_name,
                        "confidence": "ooxml_exact",
                        "match_count": len(targets),
                    })

    symbol_entries = manifest["symbols"]
    symbol_by_spelling: list[tuple[str, dict[str, Any], str]] = []
    for entry in symbol_entries:
        symbol_id = "symbol:" + entry["symbol"]
        if not add_node({
            "id": symbol_id,
            "kind": "symbol",
            "symbol": entry["symbol"],
            "role": entry["role"],
            "description": entry["description"],
            "required": entry["required"],
        }):
            return {"document_path": document_path, "source_fingerprint": source_fingerprint, "error": "graph node limit exceeded", "error_type": "limit_exceeded"}
        for spelling in [entry["symbol"], *entry["aliases"], *entry["flattened_aliases"]]:
            kind = "canonical" if spelling == entry["symbol"] else (
                "alias" if spelling in entry["aliases"] else "flattened_alias"
            )
            symbol_by_spelling.append((spelling, entry, kind))
    symbol_by_spelling.sort(key=lambda row: (-len(row[0]), row[0].casefold(), row[0]))

    for paragraph_id, text in paragraph_records:
        for spelling, entry, match_kind in symbol_by_spelling:
            count = _occurrences(text, spelling, manifest["case_sensitive"])
            if count:
                edge = {
                    "source": paragraph_id,
                    "target": "symbol:" + entry["symbol"],
                    "type": "uses",
                    "spelling": spelling,
                    "match_kind": match_kind,
                    "count": count,
                    "confidence": "manifest_exact",
                }
                add_edge(edge)
                if _looks_like_definition(text, spelling, manifest["case_sensitive"]):
                    add_edge({
                        "source": paragraph_id,
                        "target": "symbol:" + entry["symbol"],
                        "type": "defines_candidate",
                        "spelling": spelling,
                        "confidence": "heuristic",
                        "evidence": text[:240],
                    })

    for record in equations:
        text = " ".join(part for part in (record["paragraph_text"], record["flat_text"]) if part)
        for referenced_number in _reference_numbers(text):
            matches = equation_by_number.get(referenced_number, [])
            if len(matches) == 1:
                target = matches[0]
                edge_type = "depends_on"
                add_edge({
                    "source": record["id"],
                    "target": target["id"],
                    "type": edge_type,
                    "reference": referenced_number,
                    "confidence": "lexical",
                })
            else:
                unresolved_references.add(referenced_number)
                add_edge({
                    "source": record["id"],
                    "target": "unresolved-equation:" + referenced_number,
                    "type": "unresolved_reference",
                    "reference": referenced_number,
                    "confidence": "lexical",
                    "match_count": len(matches),
                })

    for paragraph_id, text in paragraph_records:
        for referenced_number in _reference_numbers(text):
            matches = equation_by_number.get(referenced_number, [])
            if len(matches) == 1:
                add_edge({
                    "source": paragraph_id,
                    "target": matches[0]["id"],
                    "type": "references",
                    "reference": referenced_number,
                    "confidence": "lexical",
                })
            else:
                unresolved_references.add(referenced_number)
                add_edge({
                    "source": paragraph_id,
                    "target": "unresolved-equation:" + referenced_number,
                    "type": "unresolved_reference",
                    "reference": referenced_number,
                    "confidence": "lexical",
                    "match_count": len(matches),
                })

    for referenced_number in sorted(unresolved_references):
        if referenced_number.startswith("bookmark:"):
            bookmark_name = referenced_number.removeprefix("bookmark:")
            unresolved_id = "unresolved-bookmark:" + bookmark_name
            unresolved_node = {
                "id": unresolved_id,
                "kind": "unresolved_bookmark_reference",
                "bookmark_name": bookmark_name or None,
            }
        else:
            unresolved_id = "unresolved-equation:" + referenced_number
            unresolved_node = {
                "id": unresolved_id,
                "kind": "unresolved_reference",
                "reference": referenced_number,
            }
        if not add_node(unresolved_node):
            return {"document_path": document_path, "source_fingerprint": source_fingerprint, "error": "graph node limit exceeded", "error_type": "limit_exceeded"}

    conflicts: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    visible_numbers = sorted(equation_by_number, key=lambda value: (value.casefold(), value))
    pure_integer_numbers = sorted(
        {int(value) for value in visible_numbers if re.fullmatch(r"\d+", value)}
    )
    numbering_gaps: list[int] = []
    if pure_integer_numbers:
        numbering_gaps = [
            number
            for number in range(1, pure_integer_numbers[-1] + 1)
            if number not in pure_integer_numbers
        ]
    numbering = {
        "numbered_equation_count": sum(len(records) for records in equation_by_number.values()),
        "visible_numbers": visible_numbers,
        "pure_integer_numbers": pure_integer_numbers,
        "missing_pure_integer_numbers": numbering_gaps,
        "gap_detection_scope": "recognized visible pure-integer equation numbers only",
    }
    if numbering_gaps:
        observations.append({
            "type": "equation_number_gap",
            "severity": "warning",
            "missing_numbers": numbering_gaps,
            "message": "recognized visible equation numbers do not form a contiguous pure-integer sequence; unnumbered or section-scoped equations may make this intentional",
        })
    for number, records in sorted(equation_by_number.items()):
        if len(records) > 1:
            conflicts.append({
                "type": "duplicate_equation_number",
                "severity": "error",
                "number": number,
                "equations": sorted(record["id"] for record in records),
                "message": f"equation number {number!r} is assigned to multiple equations",
            })

    by_structure: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in equations:
        by_structure[record["structure_sha256"]].append(record)
    for structure, records in sorted(by_structure.items()):
        if len(records) > 1 and len({record.get("number") for record in records}) > 1:
            conflicts.append({
                "type": "duplicate_equation_structure",
                "severity": "warning",
                "structure_sha256": structure,
                "equations": sorted(record["id"] for record in records),
                "numbers": sorted({record.get("number") for record in records if record.get("number")}),
                "message": "identical native OMML structure appears under different visible numbers",
            })
        if len(records) > 1 and len({record["placement"] for record in records}) > 1:
            observations.append({
                "type": "placement_conflict",
                "severity": "info",
                "structure_sha256": structure,
                "equations": sorted(record["id"] for record in records),
                "placements": sorted({record["placement"] for record in records}),
                "message": "identical equation structure is reused in different document contexts; review only if the contexts should be unified",
            })

    for edge in sorted(edges.values(), key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))):
        if edge["type"] == "unresolved_reference":
            conflicts.append({
                "type": "unresolved_equation_reference",
                "severity": "warning",
                "source": edge["source"],
                "reference": edge["reference"],
                "match_count": edge["match_count"],
                "message": f"reference to equation {edge['reference']!r} could not be resolved uniquely",
            })
        elif edge["type"] == "unresolved_bookmark_reference":
            observations.append({
                "type": "unresolved_word_bookmark_reference",
                "severity": "warning",
                "source": edge["source"],
                "bookmark_name": edge.get("bookmark_name"),
                "match_count": edge.get("match_count", 0),
                "message": "Word REF field target was not uniquely found in word/document.xml; headers, footers, footnotes, and endnotes are outside this graph",
            })

    conflicts.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    edge_list = sorted(edges.values(), key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    equation_ids = [record["id"] for record in equations]
    placements: dict[str, list[str]] = defaultdict(list)
    for record in equations:
        placements[record["placement"]].append(record["id"])
    for values in placements.values():
        values.sort()

    graph: dict[str, Any] = {
        "schema_version": "1",
        "document_path": document_path,
        "source_fingerprint": source_fingerprint,
        "provenance": {
            "generator": "meridian-docs.equation_graph",
            "schema_version": "1",
            "source_representation": "native_omml",
            "source_fingerprint": source_fingerprint,
            "notation_manifest_sha256": manifest_sha256,
            "deterministic": True,
        },
        "notation_manifest": manifest,
        "manifest_sha256": manifest_sha256,
        "reference_extraction": {
            "schema_version": reference_extraction.get("schema_version", "1"),
            "source": reference_extraction.get("source", "word/document.xml"),
            "record_count": len(reference_signals),
            "records": reference_signals,
            "limitations": reference_extraction.get("limitations", []),
        },
        "nodes": _sorted_nodes(nodes),
        "edges": edge_list,
        "equations": sorted(equations, key=lambda record: int(record["ordinal"])),
        "equation_count": len(equations),
        "node_count": len(nodes),
        "edge_count": len(edge_list),
        "placements": {key: placements[key] for key in sorted(placements)},
        "display_modes": {
            mode: sorted(record["id"] for record in equations if record["display_mode"] == mode)
            for mode in sorted({record["display_mode"] for record in equations})
        },
        "containers": {
            container: sorted(record["id"] for record in equations if record["container"] == container)
            for container in sorted({record["container"] for record in equations})
        },
        "numbering": numbering,
        "conflicts": conflicts,
        "conflict_count": len(conflicts),
        "observations": sorted(
            observations,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        ),
        "dag": _dag_validation(equation_ids, edge_list),
        "limitations": [
            "references and definition candidates are lexical heuristics, not scientific claims",
            "native OMML is authoritative; structure hashes do not prove mathematical equivalence",
            "a document-wide graph may contain cycles; only explicit equation dependencies are DAG-validated",
        ],
    }
    graph["graph_sha256"] = _digest_payload(graph)
    return graph
