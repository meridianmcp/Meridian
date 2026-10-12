"""Native Word comment review workflow for meridian-docs (sprint item fb1f7149).

Slice 1 of fb1f7149: manage comments that already exist in a .docx as native
Word comments. A native comment is a ``w:comment`` body in
``word/comments.xml`` plus ``commentRangeStart`` / ``commentRangeEnd`` /
``commentReference`` markers in ``word/document.xml``.

Provided here:

- :func:`list_word_comments` and :func:`read_word_comment` (read-only).
- :func:`update_word_comment` rewrites the comment BODY only. The comment id,
  author, initials and ``w:date`` are left untouched, and the
  ``annotationRef`` run Word places at the start of a comment is kept.
- :func:`delete_word_comment` removes the comment body and its anchor markers
  for one id. Other comments and the anchored paragraph text are untouched.

Slice 2 adds threads and resolution, stored in ``word/commentsExtended.xml``:
:func:`reply_word_comment` and :func:`resolve_word_comment`. A reply is a new
``w:comment`` anchored beside its thread root and linked by ``w15:paraIdParent``.

Not provided yet (the rest of fb1f7149): list/accept/reject of ``w:ins``/``w:del``
revisions, anchor indexing, and MCP tool exposure in ``server.py``.

Writes reuse docs_intel's stage -> verify -> promote transaction
(``_save_docx_with_new_parts_stdlib`` under ``_docx_promotion_lock``) and the
same render-capability gate that :func:`docs_intel.insert_word_comment` uses.
Namespace declarations are preserved through
``ooxml_integrity.serialize_document_xml_preserving_namespaces``, so
``mc:Ignorable`` keeps pointing at declared prefixes.
"""

from __future__ import annotations

import io
import secrets
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

from . import ooxml_integrity
from .docs_intel import (
    _COMMENTS_REL_TYPE,
    _CONTENT_TYPES_NS,
    _REL_NS,
    _W,
    _W14,
    _XML_NS,
    DocxWriteVerificationError,
    _docx_file_sha256,
    _docx_promotion_lock,
    _enforce_render_verification,
    _load_docx_xml_stdlib,
    _next_word_comment_id,
    _q,
    _save_docx_with_new_parts_stdlib,
)

_COMMENTS_PART_DEFAULT = "word/comments.xml"
_DOCUMENT_PART = "word/document.xml"
_RANGE_TAGS = ("commentRangeStart", "commentRangeEnd")
_REFERENCE_TAG = "commentReference"


# ---------------------------------------------------------------------------
# Package and id helpers
# ---------------------------------------------------------------------------


def _normalize_comment_id(comment_id: Any) -> str | None:
    """Return the canonical string form of a comment id, or ``None`` if invalid.

    Accepts a non-negative ``int`` or an ASCII digit string. ``bool`` is
    refused on purpose: ``True`` would otherwise silently address comment 1.
    """
    if isinstance(comment_id, bool):
        return None
    if isinstance(comment_id, int):
        return str(comment_id) if comment_id >= 0 else None
    text = str(comment_id).strip()
    return str(int(text)) if text.isascii() and text.isdigit() else None


def _attr_id(element: ET.Element) -> str | None:
    """Read ``w:id``, falling back to a bare ``id`` attribute.

    Mirrors :func:`docs_intel._next_word_comment_id`, which accepts the same
    legacy unqualified form that ``ooxml_integrity.normalize_word_comment_attributes``
    repairs on write.
    """
    value = element.get(_q(_W, "id"))
    if value is None:
        value = element.get("id")
    return value


def _sorted_ids(ids: list[str]) -> list[str]:
    return sorted(ids, key=lambda value: (len(value), value))


def _comments_part_name(archive: zipfile.ZipFile) -> str:
    """Resolve the comments part from ``document.xml.rels`` (default ``word/comments.xml``)."""
    try:
        rels_root = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
    except (KeyError, ET.ParseError):
        return _COMMENTS_PART_DEFAULT
    for relation in rels_root.findall(_q(_REL_NS, "Relationship")):
        if relation.get("Type") == _COMMENTS_REL_TYPE:
            target = (relation.get("Target") or "comments.xml").lstrip("/")
            return target if target.startswith("word/") else f"word/{target}"
    return _COMMENTS_PART_DEFAULT


def _open_package(docx_path: str) -> tuple[bytes, ET.Element, str]:
    """Return ``(raw_bytes, document_root, comments_part_name)``.

    Raises ``FileNotFoundError`` or ``ValueError`` (same contract as
    :func:`docs_intel._load_docx_xml_stdlib`).
    """
    raw, document_root = _load_docx_xml_stdlib(docx_path)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        part_name = _comments_part_name(archive)
    return raw, document_root, part_name


def _read_part(raw: bytes, part_name: str) -> bytes | None:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        try:
            return archive.read(part_name)
        except KeyError:
            return None


def _load_comments(raw: bytes, part_name: str) -> tuple[bytes | None, ET.Element | None]:
    """Return ``(original_bytes, comments_root)``, or ``(None, None)`` when no comments part exists."""
    data = _read_part(raw, part_name)
    if data is None:
        return None, None
    try:
        return data, ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"{part_name} is not well-formed XML: {exc}") from exc


def _comments_by_id(comments_root: ET.Element | None) -> dict[str, ET.Element]:
    if comments_root is None:
        return {}
    found: dict[str, ET.Element] = {}
    for element in comments_root.findall(_q(_W, "comment")):
        comment_id = _attr_id(element)
        if comment_id is not None and comment_id not in found:
            found[comment_id] = element
    return found


def _marker_elements(document_root: ET.Element) -> dict[str, list[ET.Element]]:
    """Map each comment id to every range/reference marker that names it."""
    markers: dict[str, list[ET.Element]] = {}
    marker_tags = {_q(_W, tag) for tag in (*_RANGE_TAGS, _REFERENCE_TAG)}
    for element in document_root.iter():
        if element.tag in marker_tags:
            comment_id = _attr_id(element)
            if comment_id is not None:
                markers.setdefault(comment_id, []).append(element)
    return markers


def _parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in parent}


def _enclosing_paragraph(element: ET.Element, parents: dict[ET.Element, ET.Element]) -> ET.Element | None:
    node: ET.Element | None = element
    while node is not None:
        if node.tag == _q(_W, "p"):
            return node
        node = parents.get(node)
    return None


# ---------------------------------------------------------------------------
# Body text helpers
# ---------------------------------------------------------------------------


def _comment_text(comment: ET.Element) -> str:
    """Join a comment's paragraphs with newlines (the same shape updates write)."""
    lines = []
    for paragraph in comment.findall(_q(_W, "p")):
        lines.append("".join(node.text or "" for node in paragraph.iter(_q(_W, "t"))))
    return "\n".join(lines)


def _normalize_body_text(text: Any) -> str:
    if text is None:
        return ""
    return str(text).replace("\r\n", "\n").replace("\r", "\n").strip()


def _append_text_run(paragraph: ET.Element, line: str) -> None:
    run = ET.SubElement(paragraph, _q(_W, "r"))
    node = ET.SubElement(run, _q(_W, "t"))
    node.set(_q(_XML_NS, "space"), "preserve")
    node.text = line


def _replace_comment_body_text(comment: ET.Element, text: str, used: set[str]) -> None:
    """Replace the comment body with ``text`` (one paragraph per line).

    Keeps the first paragraph's ``w:pPr`` and its ``annotationRef`` run, drops
    every other run and every later paragraph, then writes the new text. The
    comment element's own ``w:id``, ``w:author``, ``w:initials`` and
    ``w:date`` attributes are never touched. When the comment's last paragraph
    carries a ``w14:paraId`` that commentsExtended.xml refers to, the rewritten
    last paragraph keeps that id, so a thread link survives the rewrite.
    """
    paragraphs = comment.findall(_q(_W, "p"))
    thread_para_id = _last_para_id(comment)
    if paragraphs:
        head = paragraphs[0]
        for child in list(head):
            if child.tag == _q(_W, "pPr"):
                continue
            if child.tag == _q(_W, "r") and child.find(_q(_W, "annotationRef")) is not None:
                continue
            head.remove(child)
        for extra in paragraphs[1:]:
            comment.remove(extra)
    else:
        head = ET.SubElement(comment, _q(_W, "p"))

    lines = text.split("\n")
    _append_text_run(head, lines[0])
    for line in lines[1:]:
        _append_text_run(ET.SubElement(comment, _q(_W, "p")), line)
    if thread_para_id:
        last = comment.findall(_q(_W, "p"))[-1]
        if last is not head and _para_id_of(head) == thread_para_id:
            head.set(_q(_W14, "paraId"), _new_para_id(used))
        last.set(_q(_W14, "paraId"), thread_para_id)


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------


def _comment_record(
    comment_id: str,
    comment: ET.Element,
    markers: list[ET.Element],
    parents: dict[ET.Element, ET.Element],
) -> dict[str, Any]:
    starts = [m for m in markers if m.tag == _q(_W, "commentRangeStart")]
    ends = [m for m in markers if m.tag == _q(_W, "commentRangeEnd")]
    references = [m for m in markers if m.tag == _q(_W, _REFERENCE_TAG)]
    anchor = _enclosing_paragraph(starts[0], parents) if starts else None
    return {
        "comment_id": comment_id,
        "author": comment.get(_q(_W, "author")),
        "initials": comment.get(_q(_W, "initials")),
        "date": comment.get(_q(_W, "date")),
        "text": _comment_text(comment),
        "anchor_para_id": anchor.get(_q(_W14, "paraId")) if anchor is not None else None,
        "anchored": bool(starts and ends and references),
    }


def list_word_comments(docx_path: str) -> dict[str, Any]:
    """List every native Word comment in ``docx_path`` (read-only).

    Returns ``{status, docx_path, comments_part, count, comments,
    orphan_marker_ids}``. Each comment carries ``comment_id``, ``author``,
    ``initials``, ``date`` (all as stored), ``text`` (paragraphs joined with
    newlines), ``anchor_para_id`` (the ``w14:paraId`` of the paragraph holding
    the range start) and ``anchored`` (False when any of the start, end or
    reference markers is missing). ``orphan_marker_ids`` lists ids that have
    markers in the document body but no comment body. Each record also carries
    ``parent_comment_id`` (the comment it replies to, else ``None``),
    ``thread_root_id``, and ``resolved`` (the thread root's flag from
    commentsExtended.xml).
    """
    try:
        raw, document_root, part_name = _open_package(docx_path)
        _, comments_root = _load_comments(raw, part_name)
        _, extended_root = _load_comments(raw, _comments_extended_part_name(raw))
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}

    parents = _parent_map(document_root)
    markers = _marker_elements(document_root)
    bodies = _comments_by_id(comments_root)
    records = [
        _comment_record(comment_id, element, markers.get(comment_id, []), parents)
        for comment_id, element in bodies.items()
    ]
    records.sort(key=lambda record: (len(record["comment_id"]), record["comment_id"]))
    _thread_fields(records, bodies, extended_root)
    orphans = _sorted_ids([cid for cid in markers if cid not in bodies])
    return {
        "status": "ok",
        "docx_path": docx_path,
        "comments_part": part_name if comments_root is not None else None,
        "count": len(records),
        "comments": records,
        "orphan_marker_ids": orphans,
    }


def read_word_comment(docx_path: str, comment_id: Any) -> dict[str, Any]:
    """Return one native Word comment by id (read-only). See :func:`list_word_comments`."""
    cid = _normalize_comment_id(comment_id)
    if cid is None:
        return {"error": f"comment_id must be a non-negative integer, got {comment_id!r}"}
    listing = list_word_comments(docx_path)
    if "error" in listing:
        return listing
    for record in listing["comments"]:
        if record["comment_id"] == cid:
            return {"status": "ok", "docx_path": docx_path, "comment": record}
    return {"error": f"comment_id {cid} not found in {docx_path}"}


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------


def _degraded_argument_error(allow_degraded_render: bool, degraded_render_reason: str | None) -> str | None:
    if allow_degraded_render and not (degraded_render_reason and str(degraded_render_reason).strip()):
        return (
            "degraded_render_reason is required and must be non-empty "
            "when allow_degraded_render=True -- an audited degrade with "
            "no stated reason is not auditable and is refused"
        )
    return None


def _commit_package_write(
    docx_path: str,
    raw: bytes,
    updated_parts: dict[str, bytes],
    *,
    allow_degraded_render: bool,
    degraded_render_reason: str | None,
    identity: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Stage, verify, promote, then run the render gate, as insert_word_comment does.

    Returns ``(error, render_info)``. Exactly one side is meaningful: a
    non-``None`` ``error`` already carries ``identity`` and ``docx_path``.
    """
    with _docx_promotion_lock(docx_path):
        try:
            _save_docx_with_new_parts_stdlib(raw, updated_parts, docx_path)
        except DocxWriteVerificationError as exc:
            return {"error": str(exc), **identity, "docx_path": docx_path}, {}
        except OSError as exc:
            return {"error": f"could not write {docx_path}: {exc}", **identity, "docx_path": docx_path}, {}

        promoted_sha256 = _docx_file_sha256(docx_path)
        render_error, render_info = _enforce_render_verification(
            docx_path,
            promoted_sha256=promoted_sha256,
            allow_degraded_render=allow_degraded_render,
            degraded_render_reason=degraded_render_reason,
        )
        if render_error is not None:
            render_error.update(identity)
            render_error["docx_path"] = docx_path
            return render_error, {}
    return None, dict(render_info or {})


def update_word_comment(
    docx_path: str,
    comment_id: Any,
    text: str,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
) -> dict[str, Any]:
    """Replace the body text of one native Word comment, keeping its metadata.

    Only ``word/comments.xml`` is rewritten. The comment's id, author,
    initials and date are preserved exactly, and the comment's anchor markers
    in ``document.xml`` are not touched. ``text`` may contain newlines, which
    become separate comment paragraphs.

    Refuses (file unchanged) when the id is invalid, the text is empty, the
    comment has no body, or the degraded-render opt-in lacks a reason.
    """
    cid = _normalize_comment_id(comment_id)
    if cid is None:
        return {"error": f"comment_id must be a non-negative integer, got {comment_id!r}"}
    body_text = _normalize_body_text(text)
    if not body_text:
        return {"error": "text must be a non-empty string"}
    degraded_error = _degraded_argument_error(allow_degraded_render, degraded_render_reason)
    if degraded_error is not None:
        return {"error": degraded_error}

    try:
        raw, _document_root, part_name = _open_package(docx_path)
        original, comments_root = _load_comments(raw, part_name)
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}
    comment = _comments_by_id(comments_root).get(cid)
    if comment is None:
        return {"error": f"comment_id {cid} has no comment body in {docx_path}"}

    author = comment.get(_q(_W, "author"))
    initials = comment.get(_q(_W, "initials"))
    date = comment.get(_q(_W, "date"))
    _replace_comment_body_text(comment, body_text, _used_para_ids(_document_root, comments_root))
    try:
        updated = {
            part_name: ooxml_integrity.serialize_document_xml_preserving_namespaces(original, comments_root),
        }
    except ooxml_integrity.DocxPackageIntegrityError as exc:
        return {"error": f"could not serialize {part_name}: {exc}"}

    identity = {"comment_id": cid}
    error, render_info = _commit_package_write(
        docx_path,
        raw,
        updated,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
        identity=identity,
    )
    if error is not None:
        return error
    return {
        **render_info,
        "status": "updated",
        "mode": "update_comment",
        "comment_id": cid,
        "text": body_text,
        "author": author,
        "initials": initials,
        "date": date,
        "docx_path": docx_path,
    }


def _remove_comment_markers(
    markers: list[ET.Element],
    parents: dict[ET.Element, ET.Element],
    comment_id: str,
) -> int:
    """Remove the range and reference markers for one id; return how many were removed.

    A reference sitting alone in a run (plus an optional ``rPr``) takes that
    whole run with it, so no empty run is left behind. A run that also holds a
    reference to a different comment keeps that reference.
    """
    removed = 0
    seen: set[int] = set()
    for marker in markers:
        target = marker
        if marker.tag == _q(_W, _REFERENCE_TAG):
            run = parents.get(marker)
            if run is not None and run.tag == _q(_W, "r") and all(
                child.tag == _q(_W, "rPr")
                or (child.tag == _q(_W, _REFERENCE_TAG) and _attr_id(child) == comment_id)
                for child in run
            ):
                target = run
        if id(target) in seen:
            continue
        holder = parents.get(target)
        if holder is None or target not in list(holder):
            continue
        holder.remove(target)
        seen.add(id(target))
        removed += 1
    return removed


def delete_word_comment(
    docx_path: str,
    comment_id: Any,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
) -> dict[str, Any]:
    """Delete one native Word comment: its body and every anchor marker naming it.

    Other comments, their markers, and the anchored paragraph text are left
    alone. An id that has markers but no body (an orphan) is also cleaned up.
    Refuses (file unchanged) when neither a body nor any marker exists for
    the id.
    """
    cid = _normalize_comment_id(comment_id)
    if cid is None:
        return {"error": f"comment_id must be a non-negative integer, got {comment_id!r}"}
    degraded_error = _degraded_argument_error(allow_degraded_render, degraded_render_reason)
    if degraded_error is not None:
        return {"error": degraded_error}

    try:
        raw, document_root, part_name = _open_package(docx_path)
        original_comments, comments_root = _load_comments(raw, part_name)
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}
    original_document = _read_part(raw, _DOCUMENT_PART)
    if original_document is None:
        return {"error": f"{_DOCUMENT_PART} is missing from {docx_path}"}

    body = _comments_by_id(comments_root).get(cid)
    markers = _marker_elements(document_root).get(cid, [])
    if body is None and not markers:
        return {"error": f"comment_id {cid} not found in {docx_path}"}

    parents = _parent_map(document_root)
    removed_markers = _remove_comment_markers(markers, parents, cid)
    try:
        updated = {
            _DOCUMENT_PART: ooxml_integrity.serialize_document_xml_preserving_namespaces(
                original_document, document_root
            ),
        }
        if body is not None:
            comments_root.remove(body)
            updated[part_name] = ooxml_integrity.serialize_document_xml_preserving_namespaces(
                original_comments, comments_root
            )
    except ooxml_integrity.DocxPackageIntegrityError as exc:
        return {"error": f"could not serialize package parts: {exc}"}

    identity = {"comment_id": cid}
    error, render_info = _commit_package_write(
        docx_path,
        raw,
        updated,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
        identity=identity,
    )
    if error is not None:
        return error
    return {
        **render_info,
        "status": "deleted",
        "mode": "delete_comment",
        "comment_id": cid,
        "removed_comment_body": body is not None,
        "removed_markers": removed_markers,
        "docx_path": docx_path,
    }


# ---------------------------------------------------------------------------
# Threads and resolution (word/commentsExtended.xml), slice 2
# ---------------------------------------------------------------------------

_W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_COMMENTS_EXTENDED_REL_TYPE = (
    "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
)
_COMMENTS_EXTENDED_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml"
)
_COMMENTS_EXTENDED_DEFAULT = "word/commentsExtended.xml"
_PARA_ID_LIMIT = 0x80000000


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _para_id_of(element: ET.Element) -> str | None:
    return element.get(_q(_W14, "paraId")) or None


def _used_para_ids(*roots: ET.Element | None) -> set[str]:
    """Every w14:paraId and w15:paraId already present, so a minted id never collides."""
    used: set[str] = set()
    for root in roots:
        if root is None:
            continue
        for element in root.iter():
            for name in (_q(_W14, "paraId"), _q(_W15, "paraId")):
                value = element.get(name)
                if value:
                    used.add(value)
    return used


def _new_para_id(used: set[str]) -> str:
    """Mint an unused paragraph id: eight hex digits, below 0x80000000 as Word requires."""
    while True:
        candidate = f"{secrets.randbelow(_PARA_ID_LIMIT - 1) + 1:08X}"
        if candidate not in used:
            used.add(candidate)
            return candidate


def _last_para_id(comment: ET.Element) -> str | None:
    """The w14:paraId of a comment's last paragraph, the key commentsExtended.xml uses."""
    paragraphs = comment.findall(_q(_W, "p"))
    return _para_id_of(paragraphs[-1]) if paragraphs else None


def _ensure_last_para_id(comment: ET.Element, used: set[str]) -> tuple[str | None, bool]:
    """Return ``(paraId, created)`` for the last paragraph, minting one when it has none."""
    paragraphs = comment.findall(_q(_W, "p"))
    if not paragraphs:
        return None, False
    existing = _para_id_of(paragraphs[-1])
    if existing:
        return existing, False
    value = _new_para_id(used)
    paragraphs[-1].set(_q(_W14, "paraId"), value)
    return value, True


def _is_anchored(markers: list[ET.Element]) -> bool:
    tags = {marker.tag for marker in markers}
    return {
        _q(_W, "commentRangeStart"),
        _q(_W, "commentRangeEnd"),
        _q(_W, _REFERENCE_TAG),
    } <= tags


def _first_marker(markers: list[ET.Element], tag: str) -> ET.Element | None:
    return next((marker for marker in markers if marker.tag == _q(_W, tag)), None)


def _range_marker(tag: str, comment_id: int) -> ET.Element:
    element = ET.Element(_q(_W, tag))
    element.set(_q(_W, "id"), str(comment_id))
    return element


def _insert_after(parents: dict[ET.Element, ET.Element], anchor: ET.Element, new: ET.Element) -> None:
    holder = parents[anchor]
    holder.insert(list(holder).index(anchor) + 1, new)


def _xml_bytes(root: ET.Element) -> bytes:
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


def _commentex_done(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "on"}


def _comments_extended_part_name(raw: bytes) -> str:
    """Resolve the commentsExtended part from document.xml.rels (default word/commentsExtended.xml)."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        try:
            rels_root = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
        except (KeyError, ET.ParseError):
            return _COMMENTS_EXTENDED_DEFAULT
    for relation in rels_root.findall(_q(_REL_NS, "Relationship")):
        if relation.get("Type") == _COMMENTS_EXTENDED_REL_TYPE:
            target = (relation.get("Target") or "commentsExtended.xml").lstrip("/")
            return target if target.startswith("word/") else f"word/{target}"
    return _COMMENTS_EXTENDED_DEFAULT


def _extended_entries(extended_root: ET.Element | None) -> dict[str, ET.Element]:
    """Map each ``w15:commentEx``'s ``w15:paraId`` to its entry (first one wins)."""
    entries: dict[str, ET.Element] = {}
    if extended_root is None:
        return entries
    for entry in extended_root.iter(_q(_W15, "commentEx")):
        para_id = entry.get(_q(_W15, "paraId"))
        if para_id and para_id not in entries:
            entries[para_id] = entry
    return entries


def _comment_by_last_para(bodies: dict[str, ET.Element]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for comment_id, element in bodies.items():
        para_id = _last_para_id(element)
        if para_id and para_id not in mapping:
            mapping[para_id] = comment_id
    return mapping


def _parent_comment_id(
    comment: ET.Element,
    para_to_id: dict[str, str],
    entries: dict[str, ET.Element],
) -> str | None:
    entry = entries.get(_last_para_id(comment) or "")
    if entry is None:
        return None
    parent_para = entry.get(_q(_W15, "paraIdParent"))
    return para_to_id.get(parent_para) if parent_para else None


def _thread_root_id(
    comment_id: str,
    bodies: dict[str, ET.Element],
    para_to_id: dict[str, str],
    entries: dict[str, ET.Element],
) -> str:
    """Walk ``paraIdParent`` links up to the root of the thread (cycle-safe)."""
    current, seen = comment_id, {comment_id}
    while True:
        parent = _parent_comment_id(bodies[current], para_to_id, entries)
        if parent is None or parent in seen:
            return current
        seen.add(parent)
        current = parent


def _thread_fields(
    records: list[dict[str, Any]],
    bodies: dict[str, ET.Element],
    extended_root: ET.Element | None,
) -> None:
    """Add ``parent_comment_id``, ``thread_root_id`` and ``resolved`` to each record."""
    entries = _extended_entries(extended_root)
    para_to_id = _comment_by_last_para(bodies)
    for record in records:
        comment_id = record["comment_id"]
        root_id = _thread_root_id(comment_id, bodies, para_to_id, entries)
        root_entry = entries.get(_last_para_id(bodies[root_id]) or "")
        record["parent_comment_id"] = _parent_comment_id(bodies[comment_id], para_to_id, entries)
        record["thread_root_id"] = root_id
        record["resolved"] = root_entry is not None and _commentex_done(
            root_entry.get(_q(_W15, "done"))
        )


def _upsert_commentex(
    raw_extended: bytes | None,
    para_id: str,
    *,
    parent_para_id: str | None = None,
    done: bool | None = None,
) -> bytes:
    """Return commentsExtended.xml bytes with a ``commentEx`` for ``para_id`` present.

    lxml is used so the existing root namespace map (``mc:Ignorable`` prefixes
    included) survives the rewrite. A new entry starts open (``done="0"``) and
    may name its parent thread paragraph. ``done`` is applied when given.
    """
    lxml_etree = ooxml_integrity.LET
    if lxml_etree is None:
        raise ooxml_integrity.DocxPackageIntegrityError(
            "lxml is required for Word-compatible DOCX write-back"
        )
    if raw_extended is None:
        root = lxml_etree.Element(_q(_W15, "commentsEx"), nsmap={"w15": _W15, "mc": _MC})
    else:
        root = lxml_etree.fromstring(raw_extended)
    entry = next(
        (
            candidate
            for candidate in root.iter(_q(_W15, "commentEx"))
            if candidate.get(_q(_W15, "paraId")) == para_id
        ),
        None,
    )
    if entry is None:
        entry = lxml_etree.SubElement(root, _q(_W15, "commentEx"))
        entry.set(_q(_W15, "paraId"), para_id)
        if parent_para_id:
            entry.set(_q(_W15, "paraIdParent"), parent_para_id)
        entry.set(_q(_W15, "done"), "0")
    if done is not None:
        entry.set(_q(_W15, "done"), "1" if done else "0")
    return lxml_etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _extended_wiring_parts(raw: bytes, part_name: str) -> dict[str, bytes]:
    """Return the rels and content-types parts updated to register ``part_name``, only when missing."""
    updated: dict[str, bytes] = {}
    target = part_name.removeprefix("word/")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        try:
            rels_root = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
        except (KeyError, ET.ParseError):
            rels_root = ET.Element(_q(_REL_NS, "Relationships"))
        relations = rels_root.findall(_q(_REL_NS, "Relationship"))
        if not any(rel.get("Type") == _COMMENTS_EXTENDED_REL_TYPE for rel in relations):
            used_ids = {rel.get("Id", "") for rel in relations}
            next_id = 1
            while f"rId{next_id}" in used_ids:
                next_id += 1
            relation = ET.SubElement(rels_root, _q(_REL_NS, "Relationship"))
            relation.set("Id", f"rId{next_id}")
            relation.set("Type", _COMMENTS_EXTENDED_REL_TYPE)
            relation.set("Target", target)
            updated["word/_rels/document.xml.rels"] = _xml_bytes(rels_root)
        try:
            types_root = ET.fromstring(archive.read("[Content_Types].xml"))
        except (KeyError, ET.ParseError):
            types_root = ET.Element(_q(_CONTENT_TYPES_NS, "Types"))
        overrides = types_root.findall(_q(_CONTENT_TYPES_NS, "Override"))
        if not any(item.get("PartName") == f"/{part_name}" for item in overrides):
            override = ET.SubElement(types_root, _q(_CONTENT_TYPES_NS, "Override"))
            override.set("PartName", f"/{part_name}")
            override.set("ContentType", _COMMENTS_EXTENDED_CONTENT_TYPE)
            updated["[Content_Types].xml"] = _xml_bytes(types_root)
    return updated


def reply_word_comment(
    docx_path: str,
    parent_comment_id: Any,
    text: str,
    author: str,
    initials: str = "",
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
) -> dict[str, Any]:
    """Add a threaded reply to a native Word comment (the Word "Reply" action).

    The reply is a new ``w:comment`` with its own id, author, initials and
    ``w:date``. Its range start, range end and reference marker are placed
    beside the thread root's markers, so it shares the root's anchored text.
    ``word/commentsExtended.xml`` gets a ``w15:commentEx`` for the reply whose
    ``w15:paraIdParent`` names the root's last paragraph. A reply to a reply
    attaches to the thread root, because Word threads one level deep.

    Refuses (file unchanged) for an invalid id, an unknown or unanchored
    parent, empty text or author, or a degraded-render opt-in without a reason.
    """
    cid = _normalize_comment_id(parent_comment_id)
    if cid is None:
        return {"error": f"comment_id must be a non-negative integer, got {parent_comment_id!r}"}
    body_text = _normalize_body_text(text)
    if not body_text:
        return {"error": "text must be a non-empty string"}
    reply_author = str(author or "").strip()
    if not reply_author:
        return {"error": "author must be a non-empty string"}
    reply_initials = str(initials or "").strip()[:9]
    degraded_error = _degraded_argument_error(allow_degraded_render, degraded_render_reason)
    if degraded_error is not None:
        return {"error": degraded_error}

    try:
        raw, document_root, part_name = _open_package(docx_path)
        original_comments, comments_root = _load_comments(raw, part_name)
        extended_part = _comments_extended_part_name(raw)
        original_extended, extended_root = _load_comments(raw, extended_part)
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}
    original_document = _read_part(raw, _DOCUMENT_PART)
    if original_document is None:
        return {"error": f"{_DOCUMENT_PART} is missing from {docx_path}"}

    bodies = _comments_by_id(comments_root)
    if cid not in bodies:
        return {"error": f"comment_id {cid} has no comment body in {docx_path}"}
    entries = _extended_entries(extended_root)
    root_id = _thread_root_id(cid, bodies, _comment_by_last_para(bodies), entries)
    root = bodies[root_id]
    markers = _marker_elements(document_root).get(root_id, [])
    if not _is_anchored(markers):
        return {
            "error": (
                f"comment_id {root_id} is not anchored in {docx_path}; "
                "a reply needs the thread's range and reference markers"
            )
        }

    used = _used_para_ids(document_root, comments_root, extended_root)
    root_para, _root_created = _ensure_last_para_id(root, used)
    if root_para is None:
        return {"error": f"comment_id {root_id} has no paragraphs to thread a reply onto"}

    reply_id = _next_word_comment_id(document_root, comments_root)
    date = _utc_timestamp()
    reply = ET.SubElement(comments_root, _q(_W, "comment"))
    reply.set(_q(_W, "id"), str(reply_id))
    reply.set(_q(_W, "author"), reply_author)
    reply.set(_q(_W, "initials"), reply_initials)
    reply.set(_q(_W, "date"), date)
    for index, line in enumerate(body_text.split("\n")):
        paragraph = ET.SubElement(reply, _q(_W, "p"))
        paragraph.set(_q(_W14, "paraId"), _new_para_id(used))
        if index == 0:
            annotation_run = ET.SubElement(paragraph, _q(_W, "r"))
            ET.SubElement(annotation_run, _q(_W, "annotationRef"))
        _append_text_run(paragraph, line)
    reply_para = _last_para_id(reply)

    parents = _parent_map(document_root)
    _insert_after(parents, _first_marker(markers, "commentRangeStart"), _range_marker("commentRangeStart", reply_id))
    _insert_after(parents, _first_marker(markers, "commentRangeEnd"), _range_marker("commentRangeEnd", reply_id))
    reference_marker = _first_marker(markers, _REFERENCE_TAG)
    reference_holder = parents.get(reference_marker)
    if reference_holder is not None and reference_holder.tag == _q(_W, "r"):
        reference_run = ET.Element(_q(_W, "r"))
        reference_run.append(_range_marker(_REFERENCE_TAG, reply_id))
        _insert_after(parents, reference_holder, reference_run)
    else:
        _insert_after(parents, reference_marker, _range_marker(_REFERENCE_TAG, reply_id))

    identity = {"comment_id": str(reply_id), "parent_comment_id": cid}
    try:
        updated_extended = _upsert_commentex(original_extended, root_para)
        updated_extended = _upsert_commentex(
            updated_extended, reply_para, parent_para_id=root_para, done=False
        )
        updated: dict[str, bytes] = {
            _DOCUMENT_PART: ooxml_integrity.serialize_document_xml_preserving_namespaces(
                original_document, document_root
            ),
            part_name: ooxml_integrity.serialize_document_xml_preserving_namespaces(
                original_comments, comments_root
            ),
            extended_part: updated_extended,
        }
    except ooxml_integrity.DocxPackageIntegrityError as exc:
        return {"error": f"could not serialize package parts: {exc}", **identity}
    updated.update(_extended_wiring_parts(raw, extended_part))

    error, render_info = _commit_package_write(
        docx_path,
        raw,
        updated,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
        identity=identity,
    )
    if error is not None:
        return error
    return {
        **render_info,
        "status": "replied",
        "mode": "reply_comment",
        "comment_id": str(reply_id),
        "parent_comment_id": root_id,
        "requested_comment_id": cid,
        "thread_root_id": root_id,
        "text": body_text,
        "author": reply_author,
        "initials": reply_initials,
        "date": date,
        "docx_path": docx_path,
    }


def resolve_word_comment(
    docx_path: str,
    comment_id: Any,
    resolved: bool = True,
    allow_degraded_render: bool = False,
    degraded_render_reason: str | None = None,
) -> dict[str, Any]:
    """Mark a comment thread resolved, or reopen it, as Word's "Resolve" action does.

    Word keeps the flag on the thread root's ``w15:commentEx``. Any comment id
    in a thread therefore resolves or reopens the whole thread. A root without
    an entry gets one, and a root without a paragraph id gets one on its last
    paragraph. Status is ``resolved``, ``reopened`` or ``unchanged``; an
    unchanged thread writes nothing.
    """
    cid = _normalize_comment_id(comment_id)
    if cid is None:
        return {"error": f"comment_id must be a non-negative integer, got {comment_id!r}"}
    if not isinstance(resolved, bool):
        return {"error": f"resolved must be True or False, got {resolved!r}"}
    degraded_error = _degraded_argument_error(allow_degraded_render, degraded_render_reason)
    if degraded_error is not None:
        return {"error": degraded_error}

    try:
        raw, document_root, part_name = _open_package(docx_path)
        original_comments, comments_root = _load_comments(raw, part_name)
        extended_part = _comments_extended_part_name(raw)
        original_extended, extended_root = _load_comments(raw, extended_part)
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}

    bodies = _comments_by_id(comments_root)
    if cid not in bodies:
        return {"error": f"comment_id {cid} has no comment body in {docx_path}"}
    entries = _extended_entries(extended_root)
    root_id = _thread_root_id(cid, bodies, _comment_by_last_para(bodies), entries)
    root = bodies[root_id]
    root_entry = entries.get(_last_para_id(root) or "")
    currently = root_entry is not None and _commentex_done(root_entry.get(_q(_W15, "done")))
    if currently == resolved:
        return {
            "status": "unchanged",
            "comment_id": cid,
            "thread_root_id": root_id,
            "resolved": currently,
            "docx_path": docx_path,
        }

    used = _used_para_ids(document_root, comments_root, extended_root)
    root_para, root_created = _ensure_last_para_id(root, used)
    if root_para is None:
        return {"error": f"comment_id {root_id} has no paragraphs to resolve"}
    identity = {"comment_id": cid}
    try:
        updated_extended = _upsert_commentex(original_extended, root_para, done=resolved)
        updated: dict[str, bytes] = {extended_part: updated_extended}
        if root_created:
            updated[part_name] = ooxml_integrity.serialize_document_xml_preserving_namespaces(
                original_comments, comments_root
            )
    except ooxml_integrity.DocxPackageIntegrityError as exc:
        return {"error": f"could not serialize package parts: {exc}", **identity}
    updated.update(_extended_wiring_parts(raw, extended_part))

    error, render_info = _commit_package_write(
        docx_path,
        raw,
        updated,
        allow_degraded_render=allow_degraded_render,
        degraded_render_reason=degraded_render_reason,
        identity=identity,
    )
    if error is not None:
        return error
    return {
        **render_info,
        "status": "resolved" if resolved else "reopened",
        "mode": "resolve_comment",
        "comment_id": cid,
        "thread_root_id": root_id,
        "resolved": resolved,
        "docx_path": docx_path,
    }
