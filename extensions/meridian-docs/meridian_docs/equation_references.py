"""Read-only extraction of equation-reference signals from Word XML.

This module intentionally operates on the raw contents of ``word/document.xml``
and has no DOCX writer or package mutation path.  It records three observable
OOXML signals:

* ``REF`` and ``SEQ`` field instructions, including their cached visible text;
* ``w:bookmarkStart`` names; and
* conservative lexical candidates such as ``Equation (3)`` in ordinary
  ``w:t`` text.

The extractor does not resolve a REF target, decide whether a bookmark belongs
to an equation, or assign scientific meaning to a SEQ identifier.  Those would
be inferences rather than facts present in ``document.xml``.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any


_TRANSITIONAL_WORD_NAMESPACE = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
)

_SOURCE_FIELD = "ooxml_field_instruction"
_SOURCE_BOOKMARK = "ooxml_bookmark_name"
_SOURCE_VISIBLE_TEXT = "visible_word_text"

_LIMITATIONS = [
    "REF and SEQ observations are not resolved to one another or to scientific meaning",
    "bookmark names are reported as raw w:bookmarkStart observations; pairing and ownership are not inferred",
    "visible-text records are lexical candidates only and may include captions or non-reference prose",
    "only the supplied word/document.xml payload is inspected; headers, footers, footnotes, and endnotes are out of scope",
]

_VISIBLE_REFERENCE_PATTERNS = (
    re.compile(
        r"(?<![A-Za-z0-9_])(?:equations?|eqs?\.)\s*"
        r"(?:numbers?\s*)?[\(\[]\s*"
        r"(?P<number>[A-Za-z]?\d+(?:[.:-][A-Za-z]?\d+)*)\s*[\)\]]",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9_])(?:equations?|eqs?\.)\s+"
        r"(?:numbers?\s+)?"
        r"(?P<number>[A-Za-z]?\d+(?:[.:-][A-Za-z]?\d+)*)"
        r"(?![A-Za-z0-9_])",
        re.IGNORECASE,
    ),
)

_FIELD_HEAD = re.compile(r"^(?P<field_type>REF|SEQ)(?:\s+|$)", re.IGNORECASE)
_WORD_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\S+')


class EquationReferenceExtractionError(ValueError):
    """Raised internally for invalid raw document.xml input."""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _tag(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}" if namespace else local


def _attribute(element: ET.Element, local: str) -> str | None:
    """Get an attribute by local name without depending on prefix spelling."""
    for key, value in element.attrib.items():
        if _local_name(key) == local:
            return value
    return None


def _normalise_instruction(value: str) -> str:
    return " ".join(value.split())


def _unquote_token(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _instruction_parts(instruction: str) -> tuple[str, str | None] | None:
    """Return the supported field type and its first non-switch argument."""
    normalised = _normalise_instruction(instruction)
    match = _FIELD_HEAD.match(normalised)
    if match is None:
        return None
    field_type = match.group("field_type").upper()
    tokens = [_unquote_token(token) for token in _WORD_TOKEN.findall(normalised)]
    argument = next(
        (token for token in tokens[1:] if token and not token.startswith("\\")),
        None,
    )
    return field_type, argument


def _field_record(
    *,
    field_type: str,
    instruction: str,
    cached_text: str,
    paragraph_index: int,
    paragraph_id: str,
    run_index: int,
    field_index: int,
) -> dict[str, Any]:
    """Build one JSON-safe record from an already-classified field."""
    record: dict[str, Any] = {
        "kind": "field",
        "field_type": field_type,
        "instruction": _normalise_instruction(instruction),
        "visible_text": cached_text,
        "confidence": "high",
        "source": _SOURCE_FIELD,
        "paragraph_index": paragraph_index,
        "paragraph_id": paragraph_id,
        "run_index": run_index,
        "field_index": field_index,
    }
    parsed = _instruction_parts(instruction)
    argument = parsed[1] if parsed is not None else None
    if field_type == "REF":
        # This is the literal field argument only.  It is deliberately not
        # called a resolved target: the bookmark may be absent or duplicated.
        record["bookmark_name"] = argument
    else:
        # A SEQ identifier is an OOXML field argument, not a scientific label
        # interpreted by this module.
        record["sequence_identifier"] = argument
    return record


def _paragraphs(
    root: ET.Element,
    paragraph_tag: str,
    paragraph_id_attribute: str,
) -> Iterator[tuple[ET.Element, int, str]]:
    for paragraph_index, paragraph in enumerate(root.iter(paragraph_tag)):
        paragraph_id = _attribute(paragraph, _local_name(paragraph_id_attribute))
        yield paragraph, paragraph_index, paragraph_id or f"p{paragraph_index}"


def _scan_complex_fields(
    paragraph: ET.Element,
    *,
    paragraph_index: int,
    paragraph_id: str,
    run_tag: str,
    field_char_tag: str,
    instruction_tag: str,
    text_tag: str,
) -> tuple[list[dict[str, Any]], set[int]]:
    """Scan Word's begin/instruction/separate/result/end field form.

    The second return value contains ``id`` values for cached ``w:t`` nodes
    inside every complete or incomplete complex field.  Masking those nodes
    keeps a cached REF result from being emitted a second time as a literal
    text candidate.
    """
    records: list[dict[str, Any]] = []
    masked_text_nodes: set[int] = set()
    stack: list[dict[str, Any]] = []
    field_index = 0

    def close_field(capture: dict[str, Any], run_index: int) -> None:
        nonlocal field_index
        masked_text_nodes.update(capture["text_nodes"])
        parsed = _instruction_parts("".join(capture["instruction_parts"]))
        if parsed is None:
            return
        field_type, _argument = parsed
        records.append(
            _field_record(
                field_type=field_type,
                instruction="".join(capture["instruction_parts"]),
                cached_text="".join(capture["display_parts"]),
                paragraph_index=paragraph_index,
                paragraph_id=paragraph_id,
                run_index=capture["start_run_index"],
                field_index=field_index,
            )
        )
        field_index += 1

    runs = list(paragraph.iter(run_tag))
    for run_index, run in enumerate(runs):
        for node in run.iter():
            if node is run:
                continue
            if node.tag == field_char_tag:
                field_char_type = (_attribute(node, "fldCharType") or "").lower()
                if field_char_type == "begin":
                    capture = {
                        "start_run_index": run_index,
                        "instruction_parts": [],
                        "display_parts": [],
                        "past_separator": False,
                        "text_nodes": set(),
                    }
                    stack.append(capture)
                elif field_char_type == "separate" and stack:
                    stack[-1]["past_separator"] = True
                elif field_char_type == "end" and stack:
                    close_field(stack.pop(), run_index)
            elif node.tag == instruction_tag:
                if stack and not stack[-1]["past_separator"]:
                    stack[-1]["instruction_parts"].append(node.text or "")
            elif node.tag == text_tag:
                if stack:
                    for capture in stack:
                        capture["text_nodes"].add(id(node))
                    if stack[-1]["past_separator"]:
                        stack[-1]["display_parts"].append(node.text or "")

    # Malformed/incomplete fields still hide their cached text from the
    # lexical pass.  No record is emitted unless a supported field closed.
    while stack:
        capture = stack.pop()
        masked_text_nodes.update(capture["text_nodes"])
    return records, masked_text_nodes


def _matches_visible_references(text: str) -> list[re.Match[str]]:
    matches: list[re.Match[str]] = []
    for pattern in _VISIBLE_REFERENCE_PATTERNS:
        matches.extend(pattern.finditer(text))
    # Prefer the first source-order match and avoid duplicate/overlapping
    # results if future patterns become more permissive.
    matches.sort(key=lambda match: (match.start(), -(match.end() - match.start())))
    selected: list[re.Match[str]] = []
    for match in matches:
        if selected and match.start() < selected[-1].end():
            continue
        selected.append(match)
    return selected


def _result(
    *,
    records: list[dict[str, Any]],
    error: str | None = None,
    error_type: str | None = None,
) -> dict[str, Any]:
    records.sort(
        key=lambda record: (
            record.get("paragraph_index") is None,
            record.get("paragraph_index") if record.get("paragraph_index") is not None else 0,
            record.get("node_order", 0),
            record.get("kind", ""),
        )
    )
    for record_index, record in enumerate(records):
        record.pop("node_order", None)
        record["record_index"] = record_index
    response: dict[str, Any] = {
        "schema_version": "1",
        "source": "word/document.xml",
        "mutation": False,
        "records": records,
        "record_count": len(records),
        "limitations": list(_LIMITATIONS),
    }
    if error is not None:
        response["error"] = error
        response["error_type"] = error_type or "document_invalid"
    return response


def extract_equation_references(
    document_xml: str | bytes | bytearray,
) -> dict[str, Any]:
    """Extract conservative equation-reference records from raw document.xml.

    Args:
        document_xml: UTF-8 XML text or bytes containing the raw
            ``word/document.xml`` part.  A DOCX path is intentionally not
            accepted; package traversal belongs outside this isolated parser.

    Returns:
        A deterministic JSON-safe dictionary with ``records`` and
        ``record_count``.  Invalid input returns the same shape with
        ``error`` and ``error_type``.  No input is written or mutated.
    """
    records: list[dict[str, Any]] = []
    if not isinstance(document_xml, (str, bytes, bytearray)):
        return _result(
            records=records,
            error="document_xml must be str, bytes, or bytearray",
            error_type="input_invalid",
        )

    try:
        root = ET.fromstring(document_xml)
        if _local_name(root.tag) != "document":
            raise EquationReferenceExtractionError(
                "document.xml root must be w:document"
            )
        word_namespace = _namespace(root.tag) or _TRANSITIONAL_WORD_NAMESPACE
        paragraph_tag = _tag(word_namespace, "p")
        text_tag = _tag(word_namespace, "t")
        run_tag = _tag(word_namespace, "r")
        field_char_tag = _tag(word_namespace, "fldChar")
        instruction_tag = _tag(word_namespace, "instrText")
        simple_field_tag = _tag(word_namespace, "fldSimple")
        bookmark_start_tag = _tag(word_namespace, "bookmarkStart")
        body_tag = _tag(word_namespace, "body")
        if root.find(body_tag) is None:
            raise EquationReferenceExtractionError(
                "document.xml has no w:body element"
            )
    except (ET.ParseError, EquationReferenceExtractionError) as exc:
        return _result(records=records, error=str(exc), error_type="xml_invalid")

    paragraph_info: dict[int, tuple[int, str]] = {}
    parent_by_id: dict[int, ET.Element] = {}
    for parent in root.iter():
        for child in list(parent):
            parent_by_id[id(child)] = parent

    for paragraph, paragraph_index, paragraph_id in _paragraphs(
        root, paragraph_tag, "paraId"
    ):
        paragraph_info[id(paragraph)] = (paragraph_index, paragraph_id)
        paragraph_nodes = list(paragraph.iter())
        node_orders = {id(node): node_index for node_index, node in enumerate(paragraph_nodes)}
        masked_text_nodes: set[int] = set()

        complex_records, complex_masked = _scan_complex_fields(
            paragraph,
            paragraph_index=paragraph_index,
            paragraph_id=paragraph_id,
            run_tag=run_tag,
            field_char_tag=field_char_tag,
            instruction_tag=instruction_tag,
            text_tag=text_tag,
        )
        masked_text_nodes.update(complex_masked)
        for record in complex_records:
            record["node_order"] = record["run_index"]
            records.append(record)

        simple_index = 0
        for simple_field in paragraph.iter(simple_field_tag):
            instruction = _attribute(simple_field, "instr") or ""
            parsed = _instruction_parts(instruction)
            if parsed is None:
                # It is still a Word field, but not one of the requested
                # REF/SEQ observations.  Its cached text is not prose.
                masked_text_nodes.update(
                    id(node) for node in simple_field.iter(text_tag)
                )
                continue
            field_type, _argument = parsed
            cached_text = "".join(
                node.text or "" for node in simple_field.iter(text_tag)
            )
            record = _field_record(
                field_type=field_type,
                instruction=instruction,
                cached_text=cached_text,
                paragraph_index=paragraph_index,
                paragraph_id=paragraph_id,
                run_index=node_orders.get(id(simple_field), 0),
                field_index=simple_index,
            )
            record["node_order"] = node_orders.get(id(simple_field), 0)
            records.append(record)
            simple_index += 1
            masked_text_nodes.update(
                id(node) for node in simple_field.iter(text_tag)
            )

        visible_nodes = [
            node
            for node in paragraph.iter(text_tag)
            if id(node) not in masked_text_nodes
        ]
        visible_text = "".join(node.text or "" for node in visible_nodes)
        for match in _matches_visible_references(visible_text):
            matched_text = match.group(0)
            records.append(
                {
                    "kind": "visible_text",
                    "matched_text": matched_text,
                    "text": matched_text,
                    "reference_number": match.group("number"),
                    "confidence": "lexical",
                    "source": _SOURCE_VISIBLE_TEXT,
                    "paragraph_index": paragraph_index,
                    "paragraph_id": paragraph_id,
                    "text_start": match.start(),
                    "text_end": match.end(),
                    "node_order": len(paragraph_nodes) + match.start(),
                }
            )

    # Bookmark starts are deliberately emitted independently of REF fields:
    # the extractor reports both raw signals but never joins them.
    root_node_orders = {id(node): index for index, node in enumerate(root.iter())}
    for bookmark in root.iter(bookmark_start_tag):
        bookmark_name = _attribute(bookmark, "name")
        if not bookmark_name:
            continue
        ancestor = parent_by_id.get(id(bookmark))
        paragraph_index: int | None = None
        paragraph_id: str | None = None
        while ancestor is not None:
            info = paragraph_info.get(id(ancestor))
            if info is not None:
                paragraph_index, paragraph_id = info
                break
            ancestor = parent_by_id.get(id(ancestor))
        records.append(
            {
                "kind": "bookmark",
                "bookmark_name": bookmark_name,
                "bookmark_id": _attribute(bookmark, "id"),
                "confidence": "high",
                "source": _SOURCE_BOOKMARK,
                "paragraph_index": paragraph_index,
                "paragraph_id": paragraph_id,
                "node_order": root_node_orders.get(id(bookmark), 0),
            }
        )

    return _result(records=records)


extract_from_document_xml = extract_equation_references


__all__ = [
    "EquationReferenceExtractionError",
    "extract_equation_references",
    "extract_from_document_xml",
]
