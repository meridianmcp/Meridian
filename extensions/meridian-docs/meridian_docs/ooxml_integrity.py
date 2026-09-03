"""Word-compatible OOXML package integrity primitives.

Real DOCX files often declare prefixes such as ``ns10``.  ElementTree treats
those names as serializer internals and renumbers them on write-back.  The
result remains valid XML and often renders in LibreOffice, but Word may show
its "unreadable content" repair dialog.  This module keeps package validation
and prefix-preserving serialization in one reusable boundary.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

try:
    from lxml import etree as LET
except ImportError:  # pragma: no cover - protected by the package dependency
    LET = None

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_R_ID = f"{{{R_NS}}}id"
_R_EMBED = f"{{{R_NS}}}embed"
_R_LINK = f"{{{R_NS}}}link"

_HEADING_STYLE_RE = re.compile(r"^heading ?([1-9])$", re.IGNORECASE)
_HEADING_NUMBER_RE = re.compile(r"^\s*\d+(?:\.\d+)+\.?\s+")
_HEADING_MINOR_WORDS = frozenset(
    {
        "a", "an", "and", "as", "at", "by", "for", "from", "in",
        "nor", "of", "on", "or", "the", "to", "via", "vs", "with",
    }
)


class DocxPackageIntegrityError(ValueError):
    """Raised when a DOCX cannot safely be promoted to Word."""


@dataclass(frozen=True)
class PackageIssue:
    code: str
    message: str
    part: str | None = None
    severity: str = "error"
    # Optional, heading-audit-specific diagnostic: which style label a
    # heading_case_inconsistency finding was raised against (a real style
    # name for a formal Heading N paragraph, or the literal string
    # "hidden-bold-heading-like" for the hidden/bold/vanish heuristic --
    # see audit_heading_capitalization). None for every other issue code;
    # kept as a real field (not folded into `message`) so a caller can
    # branch on it without parsing text.
    style: str | None = None


def _heading_text_is_title_case(text: str) -> bool:
    """Return whether a heading uses the project's title-case convention.

    Acronyms (``AI``, ``MAT``, ``RS3``), parenthesized abbreviations, and
    hyphenated compounds are accepted.  Small connective words remain lower
    case unless they begin or end the heading.  This intentionally audits
    Heading 3 rather than imposing a new case policy on all journal headings.
    """
    text = _HEADING_NUMBER_RE.sub("", text, count=1)
    words = re.findall(r"[A-Za-z\u00c0-\u00ff�]+(?:[-'][A-Za-z\u00c0-\u00ff�]+)*", text)
    if not words:
        return True
    for word_index, word in enumerate(words):
        parts = re.split(r"[-']", word)
        for part_index, part in enumerate(parts):
            if not part:
                continue
            if part.isupper() and len(part) >= 2:
                continue
            lower = part.lower()
            is_edge = word_index == 0 or word_index == len(words) - 1
            if lower in _HEADING_MINOR_WORDS and not is_edge:
                if part != lower:
                    return False
                continue
            if not part[0].isupper():
                return False
    return True


def audit_heading_capitalization(source: str | bytes | bytearray, *, heading_levels: tuple[int, ...] = (3,)) -> list[dict[str, str]]:
    """Find heading-case drift without changing the document.

    The JCSHM candidates use all-caps Heading 1/2 text and title-case Heading
    3 text.  The default therefore audits only Heading 3, where accidental
    sentence-case edits such as ``AI-assisted`` or ``Length-weighted`` are
    most likely to escape visual review.  Short, hidden, fully-bold paragraphs
    that behave like heading labels are audited too. Results are serializable
    so callers can surface them in a release manifest or comment proposal.
    """
    raw = _source_bytes(source)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            document_root = ET.fromstring(archive.read("word/document.xml"))
            styles_root = ET.fromstring(archive.read("word/styles.xml"))
    except (KeyError, ET.ParseError, zipfile.BadZipFile):
        return []
    # ElementTree does not support XPath attribute selection in findtext;
    # resolve the style name explicitly for compatibility with Python 3.10.
    style_names: dict[str, str] = {}
    for style in styles_root.findall(f"{{{W_NS}}}style"):
        style_id = style.get(f"{{{W_NS}}}styleId") or style.get("styleId")
        name = style.find(f"{{{W_NS}}}name")
        style_name = (name.get(f"{{{W_NS}}}val") or name.get("val")) if name is not None else ""
        if style_id:
            style_names[style_id] = style_name or style_id
    findings: list[dict[str, str]] = []
    paragraph_index = 0
    for paragraph in document_root.iter(f"{{{W_NS}}}p"):
        paragraph_index += 1
        style_element = paragraph.find(f"{{{W_NS}}}pPr/{{{W_NS}}}pStyle")
        style_value = (style_element.get(f"{{{W_NS}}}val") or style_element.get("val")) if style_element is not None else None
        style_name = style_names.get(style_value, style_value or "")
        match = _HEADING_STYLE_RE.fullmatch(style_name)
        text = "".join(node.text or "" for node in paragraph.iter(f"{{{W_NS}}}t")).strip()
        is_formal_heading = match and int(match.group(1)) in heading_levels
        p_properties = paragraph.find(f"{{{W_NS}}}pPr")
        hidden = bool(
            p_properties is not None
            and (
                p_properties.find(f"{{{W_NS}}}rPr/{{{W_NS}}}vanish") is not None
                or p_properties.find(f"{{{W_NS}}}rPr/{{{W_NS}}}specVanish") is not None
            )
        )
        runs = list(paragraph.iter(f"{{{W_NS}}}r"))
        all_bold = bool(runs) and all(
            run.find(f"{{{W_NS}}}rPr/{{{W_NS}}}b") is not None for run in runs
        )
        hidden_heading_like = (
            not is_formal_heading
            and hidden
            and all_bold
            and len(text) <= 120
            and text.endswith(".")
        )
        if text and (is_formal_heading or hidden_heading_like) and not _heading_text_is_title_case(text):
            findings.append(
                {
                    "code": "heading_case_inconsistency",
                    "message": f"Heading is not title case: {text}",
                    "part": f"word/document.xml#paragraph-{paragraph_index}",
                    "text": text,
                    "style": "hidden-bold-heading-like" if hidden_heading_like else style_name,
                }
            )
    return findings


def _source_bytes(source: str | bytes | bytearray) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    with open(source, "rb") as handle:
        return handle.read()


def _part_for_rels(rels_name: str) -> str:
    if rels_name == "_rels/.rels":
        return ""
    directory, filename = posixpath.split(rels_name)
    if directory.endswith("/_rels") and filename.endswith(".rels"):
        return posixpath.join(directory[:-5], filename[:-5]).lstrip("/")
    return rels_name


def _rels_for_part(part_name: str) -> str:
    if not part_name:
        return "_rels/.rels"
    directory, filename = posixpath.split(part_name)
    return posixpath.join(directory, "_rels", filename + ".rels")


def _resolve_target(owner_part: str, target: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(owner_part), target)).lstrip("/")


def _xml_parts(names: set[str]) -> list[str]:
    return sorted(n for n in names if n.endswith(".xml") or n.endswith(".rels"))


def _relationship_records(archive: zipfile.ZipFile, names: set[str]) -> tuple[dict[str, list[dict[str, str]]], list[PackageIssue]]:
    records: dict[str, list[dict[str, str]]] = {}
    issues: list[PackageIssue] = []
    for rels_name in sorted(n for n in names if n.endswith(".rels")):
        try:
            root = ET.fromstring(archive.read(rels_name))
        except ET.ParseError as exc:
            issues.append(PackageIssue("xml_parse_error", str(exc), rels_name))
            continue
        owner = _part_for_rels(rels_name)
        rows: list[dict[str, str]] = []
        seen: Counter[str] = Counter()
        for rel in root.findall(f"{{{PKG_REL_NS}}}Relationship"):
            rel_id = rel.get("Id") or ""
            target = rel.get("Target") or ""
            mode = rel.get("TargetMode") or ""
            seen[rel_id] += 1
            rows.append({"Id": rel_id, "Target": target, "TargetMode": mode, "Type": rel.get("Type") or ""})
            if mode != "External" and _resolve_target(owner, target) not in names:
                issues.append(PackageIssue("dangling_relationship", f"{rel_id} targets missing part {_resolve_target(owner, target)}", rels_name))
        for rel_id, count in seen.items():
            if rel_id and count > 1:
                issues.append(PackageIssue("duplicate_relationship_id", f"relationship id {rel_id!r} occurs {count} times", rels_name))
        records[rels_name] = rows
    return records, issues


def _content_type_issues(archive: zipfile.ZipFile, names: set[str]) -> list[PackageIssue]:
    try:
        root = ET.fromstring(archive.read("[Content_Types].xml"))
    except KeyError:
        return [PackageIssue("missing_content_types", "[Content_Types].xml is missing", "[Content_Types].xml")]
    except ET.ParseError as exc:
        return [PackageIssue("xml_parse_error", str(exc), "[Content_Types].xml")]
    defaults = {e.get("Extension") for e in root.findall(f"{{{CT_NS}}}Default")}
    overrides = {e.get("PartName", "").lstrip("/") for e in root.findall(f"{{{CT_NS}}}Override")}
    issues: list[PackageIssue] = []
    for name in sorted(names - {"[Content_Types].xml"}):
        extension = name.rsplit(".", 1)[-1] if "." in name else ""
        if name not in overrides and extension not in defaults:
            issues.append(PackageIssue("missing_content_type", f"no content type for {name}", name))
    for name in sorted(overrides - names):
        issues.append(PackageIssue("dangling_content_type", f"content-type override points to {name}", "[Content_Types].xml"))
    return issues


def validate_docx_package(source: str | bytes | bytearray, *, include_warnings: bool = True) -> dict[str, Any]:
    """Validate ZIP, XML, relationships, content types, and anchor warnings."""
    raw = _source_bytes(source)
    issues: list[PackageIssue] = []
    warnings: list[PackageIssue] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        return {"ok": False, "issues": [asdict(PackageIssue("bad_zip", str(exc)))], "warnings": [], "part_count": 0}
    try:
        if archive.testzip() is not None:
            issues.append(PackageIssue("zip_crc_error", "a ZIP member failed CRC validation"))
        name_list = archive.namelist()
        for duplicate in sorted({n for n, count in Counter(name_list).items() if count > 1}):
            issues.append(
                PackageIssue(
                    "duplicate_zip_part",
                    f"part {duplicate!r} occurs more than once in the ZIP "
                    "(last-write-wins on read, never healed on write)",
                    duplicate,
                )
            )
        names = set(name_list)
        for required in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
            if required not in names:
                issues.append(PackageIssue("missing_required_part", f"required part {required} is missing", required))
        for part in _xml_parts(names):
            try:
                ET.fromstring(archive.read(part))
            except ET.ParseError as exc:
                issues.append(PackageIssue("xml_parse_error", str(exc), part))
        records, rel_issues = _relationship_records(archive, names)
        issues.extend(rel_issues)
        issues.extend(_content_type_issues(archive, names))
        for part in _xml_parts(names):
            if part.endswith(".rels"):
                continue
            # Deliberately do NOT skip when rel_ids is empty: a part that
            # carries an r:id/r:embed attribute despite its .rels part being
            # missing or declaring zero relationships is exactly the dangling
            # case this check exists to catch, not a reason to exit early.
            rel_ids = {row["Id"] for row in records.get(_rels_for_part(part), [])}
            try:
                root = ET.fromstring(archive.read(part))
            except (KeyError, ET.ParseError):
                continue
            for element in root.iter():
                for attr_name, value in element.attrib.items():
                    if attr_name in {_R_ID, _R_EMBED} and value not in rel_ids:
                        issues.append(PackageIssue("dangling_relationship_reference", f"{value!r} is not declared in {_rels_for_part(part)}", part))
        if "word/document.xml" in names:
            try:
                root = ET.fromstring(archive.read("word/document.xml"))
            except ET.ParseError:
                root = None
            if root is None:
                root = None
            else:
                para_ids = [p.get(f"{{{W14_NS}}}paraId") for p in root.iter(f"{{{W_NS}}}p")]
                duplicates = sorted({value for value, count in Counter(v for v in para_ids if v).items() if count > 1})
                if duplicates:
                    warnings.append(PackageIssue("duplicate_para_id", f"duplicate w14:paraId values: {duplicates[:20]}", "word/document.xml", "warning"))
        for comments_part in ("word/comments.xml",):
            if comments_part not in names:
                continue
            try:
                comments_root = ET.fromstring(archive.read(comments_part))
            except ET.ParseError:
                continue
            for element in comments_root.iter():
                if element.tag == f"{{{W_NS}}}comment":
                    for local_name in ("id", "author", "initials", "date"):
                        if local_name in element.attrib:
                            issues.append(PackageIssue("unqualified_comment_attribute", f"comment {local_name} must be w:{local_name}", comments_part))
        if "word/document.xml" in names:
            try:
                document_root = ET.fromstring(archive.read("word/document.xml"))
            except ET.ParseError:
                document_root = None
            if document_root is not None:
                for element in document_root.iter():
                    if element.tag.rsplit("}", 1)[-1] in {"commentRangeStart", "commentRangeEnd", "commentReference"} and "id" in element.attrib:
                        issues.append(PackageIssue("unqualified_comment_attribute", "comment marker id must be w:id", "word/document.xml"))
    finally:
        archive.close()
    for finding in audit_heading_capitalization(raw):
        warnings.append(
            PackageIssue(
                finding["code"],
                finding["message"],
                finding["part"],
                "warning",
                style=finding.get("style"),
            )
        )
    return {"ok": not issues, "issues": [asdict(i) for i in issues], "warnings": [asdict(i) for i in (warnings if include_warnings else [])], "part_count": len(names)}


def serialize_document_xml_preserving_namespaces(original_xml: bytes, root: ET.Element) -> bytes:
    """Serialize an ET-mutated tree with the original Word namespace map."""
    if LET is None:
        raise DocxPackageIntegrityError("lxml is required for Word-compatible DOCX write-back")
    try:
        source_root = LET.fromstring(original_xml)
        mutated = LET.fromstring(ET.tostring(root, encoding="utf-8"))
        nsmap = dict(source_root.nsmap)
        output_root = LET.Element(mutated.tag, nsmap=nsmap)
        for key, value in mutated.attrib.items():
            output_root.set(key, value)
        output_root.text = mutated.text
        output_root.tail = mutated.tail
        for child in mutated:
            output_root.append(child)
        result = LET.tostring(output_root, xml_declaration=True, encoding="UTF-8", standalone=True)
        if LET.fromstring(result).nsmap != nsmap:
            raise DocxPackageIntegrityError("serialization changed the source namespace map")
        return result
    except (ET.ParseError, LET.XMLSyntaxError, ValueError) as exc:
        if isinstance(exc, DocxPackageIntegrityError):
            raise
        raise DocxPackageIntegrityError(f"could not serialize document.xml safely: {exc}") from exc


def _used_document_relationship_ids(document_xml: bytes) -> set[str]:
    root = ET.fromstring(document_xml)
    return {value for element in root.iter() for name, value in element.attrib.items() if name in {_R_ID, _R_EMBED, _R_LINK}}


def prune_unreferenced_document_media(source: str | bytes | bytearray) -> bytes:
    """Remove only unreferenced document media relationships and parts."""
    raw = _source_bytes(source)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        rels_name = "word/_rels/document.xml.rels"
        document_xml = archive.read("word/document.xml")
        used_ids = _used_document_relationship_ids(document_xml)
        rels_root = ET.fromstring(archive.read(rels_name))
        removed_relationships: list[ET.Element] = []
        for rel in list(rels_root.findall(f"{{{PKG_REL_NS}}}Relationship")):
            target = rel.get("Target") or ""
            if target.startswith("media/") and rel.get("Id") not in used_ids:
                removed_relationships.append(rel)
        if not removed_relationships:
            return raw
        for rel in removed_relationships:
            rels_root.remove(rel)
        remaining_targets = {
            _resolve_target("word/document.xml", rel.get("Target") or "")
            for rel in rels_root.findall(f"{{{PKG_REL_NS}}}Relationship")
            if (rel.get("Target") or "").startswith("media/")
        }
        removed = {
            _resolve_target("word/document.xml", rel.get("Target") or "")
            for rel in removed_relationships
            if _resolve_target("word/document.xml", rel.get("Target") or "") not in remaining_targets
        }
        changed_rels = ET.tostring(rels_root, encoding="UTF-8", xml_declaration=True)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as output:
            for info in archive.infolist():
                if info.filename in removed:
                    continue
                output.writestr(info, changed_rels if info.filename == rels_name else archive.read(info.filename))
    result = out.getvalue()
    report = validate_docx_package(result)
    if not report["ok"]:
        raise DocxPackageIntegrityError(f"normalized package is invalid: {report['issues']}")
    return result


def normalize_word_comment_attributes(source: str | bytes | bytearray) -> bytes:
    """Repair legacy comments written with unqualified XML attributes.

    ``id=`` on ``w:comment`` and comment markers is not equivalent to
    ``w:id=`` in OOXML.  Some readers accept the former, but Word can reject a
    package containing several such comments.  Only those comment attributes
    are changed; comment text, ranges, and all non-comment parts are preserved.
    """
    raw = _source_bytes(source)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        parts = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    changed: dict[str, bytes] = {}
    for part_name in ("word/document.xml", "word/comments.xml"):
        if part_name not in parts or LET is None:
            continue
        try:
            root = LET.fromstring(parts[part_name])
        except LET.XMLSyntaxError as exc:
            raise DocxPackageIntegrityError(f"cannot normalize {part_name}: {exc}") from exc
        for element in root.iter():
            local = LET.QName(element).localname
            if local == "comment":
                attrs = ("id", "author", "initials", "date")
            elif local in {"commentRangeStart", "commentRangeEnd", "commentReference"}:
                attrs = ("id",)
            else:
                continue
            for attr in attrs:
                if attr in element.attrib:
                    value = element.attrib.pop(attr)
                    element.set(f"{{{W_NS}}}{attr}", value)
        changed[part_name] = LET.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    if not changed:
        return raw
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as output:
        for info in archive.infolist():
            output.writestr(info, changed.get(info.filename, archive.read(info.filename)))
    result = out.getvalue()
    # Scoped, self-contained validation: confirm the ZIP round-tripped
    # cleanly and that exactly the parts this function rewrote landed
    # byte-for-byte in the output. Deliberately NOT a full
    # validate_docx_package() call -- that checks package-wide invariants
    # (Content_Types.xml, _rels/.rels, dangling relationships, ...) this
    # function never promised to establish and many legitimate callers
    # (synthetic/partial fixtures that only carry word/document.xml, e.g.
    # tests/test_meridian_docs_bibliography_write.py's _zip_docx) never
    # satisfied in the first place. Imposing that as a new precondition on
    # every pre-write call would reject inputs this function did not
    # corrupt -- the actual failure mode to guard against is the ZIP
    # round-trip itself silently dropping or corrupting a member.
    try:
        with zipfile.ZipFile(io.BytesIO(result)) as check:
            if check.testzip() is not None:
                raise DocxPackageIntegrityError(
                    "comment normalization produced a corrupt ZIP member"
                )
            for part_name, expected in changed.items():
                if check.read(part_name) != expected:
                    raise DocxPackageIntegrityError(
                        f"comment normalization failed to round-trip {part_name}"
                    )
    except zipfile.BadZipFile as exc:
        raise DocxPackageIntegrityError(
            f"comment normalization produced an unreadable ZIP: {exc}"
        ) from exc
    return result
