"""Local DOCX completion gate (8419f55f) -- ported from the ad hoc
``_codex_docx_completion_gate`` helper (a repo search across the working
tree, ``.codex/``, every worktree under ``.codex/worktrees``, and
``workspace/`` at port time found no surviving copy of that helper to port
verbatim -- see the sprint item's evidence notes; this module is written
from the acceptance criteria instead, closing the gaps the item names).

WHY THIS EXISTS: ``tools/meridian_fallbacks`` is the local, no-MCP-required
fallback toolkit an executor (Claude Code, Codex, or any other agent) reaches
for when it has just produced a ``.docx`` artifact and needs to decide,
BEFORE claiming a sprint item complete, whether that artifact is actually
structurally sound -- not just "a zip file exists at this path". A naive
completion check (file exists, non-zero size, maybe re-opens it with
python-docx) misses an entire class of defects that only show up once you
actually walk the OOXML package: a ZIP entry with a mismatched CRC (silent
corruption), an XML part that fails to parse, an image relationship that
points at nothing, a body paragraph that shares its ``w14:paraId`` with
another paragraph (breaks every downstream tool keyed on paraId), or a
document that "looks right" but was generated from a now-stale source and
never regenerated.

This module is deliberately **stdlib-only** for every structural check
(``zipfile`` + ``xml.etree.ElementTree`` only) -- it must work in an
environment where ``extensions/meridian-docs`` (a separately-installed
extension, see that package's own module docstrings) is NOT installed, and
it must never assume ``tools/meridian_fallbacks/__init__.py`` exists or
carries any particular contents (a sibling sprint item owns that file; this
module is written so it can be imported and run standalone, straight off its
own file path, with zero package-level coupling).

The ONE thing this module does NOT implement itself is visual rendering.
Per the sprint item's acceptance criteria, a real Word render (via COM
automation, Windows-only) is accepted as an EXTERNAL VERIFICATION RECEIPT --
optionally delegated to ``meridian_docs.render_gate.check_word_com_render_receipt``
when that extension happens to be installed -- but it is never required for
every OTHER check in this module to run, and its absence (non-Windows, no
Word installed, extension not installed) is always reported as an explicit
``render_unverified`` / ``render_unavailable`` status, NEVER silently folded
into overall success. See :func:`run_completion_gate`'s docstring for the
full four-state render contract.

Every check function below is a pure function of already-read bytes (no
hidden I/O beyond what its own docstring says) so each is independently
unit-testable without constructing a full completion-gate run.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import os
import sys
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "GATE_SCHEMA_VERSION",
    "RENDER_VERIFIED",
    "RENDER_UNVERIFIED",
    "RENDER_UNAVAILABLE",
    "RENDER_FAILED",
    "RENDER_STATUSES",
    "CompletionRequirements",
    "check_zip_integrity",
    "enumerate_para_ids",
    "check_relationship_reachability",
    "count_equations_and_captions",
    "check_required_text",
    "check_stale_source",
    "run_completion_gate",
    "main",
]

GATE_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# OOXML namespaces -- names/values match extensions/meridian-docs/meridian_docs
# /docs_intel.py's own module-level constants (``_W``, ``_W14``, ``_M``,
# ``_R_NS``, ``_PKG_REL_NS``) so anyone cross-referencing the two modules sees
# the same values, even though this module never imports that one.
# ---------------------------------------------------------------------------
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
_M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

_REQUIRED_PACKAGE_PARTS = ("[Content_Types].xml", "_rels/.rels", "word/document.xml")

# ---------------------------------------------------------------------------
# Render-receipt contract (four explicit states -- see run_completion_gate).
# ---------------------------------------------------------------------------
RENDER_VERIFIED = "render_verified"
RENDER_UNVERIFIED = "render_unverified"
RENDER_UNAVAILABLE = "render_unavailable"
RENDER_FAILED = "render_failed"
RENDER_STATUSES: tuple[str, str, str, str] = (
    RENDER_VERIFIED,
    RENDER_UNVERIFIED,
    RENDER_UNAVAILABLE,
    RENDER_FAILED,
)

RenderChecker = Callable[[str], "dict[str, Any]"]


def _qn(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


# ---------------------------------------------------------------------------
# 1. ZIP CRC + XML well-formedness + required-part presence.
# ---------------------------------------------------------------------------

def check_zip_integrity(raw: bytes) -> dict[str, Any]:
    """Verify the whole OOXML package's ZIP-level integrity.

    Three independent signals, all computed even if one already failed (a
    caller sees the whole picture, not just the first problem):

      * ``bad_crc_entries`` -- every ZIP entry that fails its own CRC-32
        check on full read (``zipfile`` validates this automatically inside
        ``ZipFile.read``/``ZipExtFile`` and raises ``BadZipFile`` on
        mismatch; that exception is caught per-entry here so one corrupt
        entry doesn't abort the scan of the rest).
      * ``malformed_xml_entries`` -- every ``*.xml`` / ``*.rels`` entry that
        fails ``ET.fromstring`` (not well-formed XML).
      * ``missing_required_parts`` -- any of ``[Content_Types].xml``,
        ``_rels/.rels``, ``word/document.xml`` absent from the archive (a
        minimal, real OPC/WordprocessingML package always has all three).

    A totally unreadable/non-ZIP ``raw`` reports ``error`` and ``ok=False``
    without raising.
    """
    result: dict[str, Any] = {
        "ok": False,
        "entry_count": 0,
        "bad_crc_entries": [],
        "malformed_xml_entries": [],
        "missing_required_parts": [],
        "error": None,
    }
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            result["entry_count"] = len(names)
            result["missing_required_parts"] = [
                part for part in _REQUIRED_PACKAGE_PARTS if part not in names
            ]
            for name in names:
                try:
                    data = zf.read(name)  # validates CRC-32 on full read
                except (zipfile.BadZipFile, RuntimeError) as exc:
                    result["bad_crc_entries"].append({"name": name, "error": str(exc)})
                    continue
                if name.endswith(".xml") or name.endswith(".rels"):
                    try:
                        ET.fromstring(data)
                    except ET.ParseError as exc:
                        result["malformed_xml_entries"].append({"name": name, "error": str(exc)})
    except (OSError, zipfile.BadZipFile) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["ok"] = (
        result["error"] is None
        and not result["bad_crc_entries"]
        and not result["malformed_xml_entries"]
        and not result["missing_required_parts"]
    )
    return result


# ---------------------------------------------------------------------------
# 2. w14:paraId enumeration -- body vs table-cell, duplicates, missing.
# ---------------------------------------------------------------------------

def enumerate_para_ids(document_xml: bytes) -> dict[str, Any]:
    """Enumerate ``w14:paraId`` across ``word/document.xml``, split into
    paragraphs directly in the body vs paragraphs nested inside a table cell
    (``w:tbl`` -> ... -> ``w:p``) -- a known gap class where artificially
    generated ``.docx`` files (built by hand or by a naive writer, never
    opened/saved by real Word) most often produce duplicate or missing
    paraIds specifically inside tables.

    Duplicate paraIds are always a real structural defect (Word itself never
    produces them); missing paraIds are only reported as a count, never as a
    hard failure here -- some legitimately valid ``.docx`` files (older
    format versions, files never touched by Word 2010+) have none at all.
    Callers that need to REQUIRE full paraId coverage do so via
    :class:`CompletionRequirements`.

    Raises ``xml.etree.ElementTree.ParseError`` if ``document_xml`` is not
    well-formed -- callers should run this after :func:`check_zip_integrity`
    confirms well-formedness, or catch it themselves.
    """
    root = ET.fromstring(document_xml)
    body_ids: list[str | None] = []
    table_cell_ids: list[str | None] = []

    p_tag = _qn(_W_NS, "p")
    tbl_tag = _qn(_W_NS, "tbl")
    para_id_attr = _qn(_W14_NS, "paraId")

    def _walk(elem: ET.Element, in_table: bool) -> None:
        if elem.tag == tbl_tag:
            in_table = True
        if elem.tag == p_tag:
            para_id = elem.get(para_id_attr)
            (table_cell_ids if in_table else body_ids).append(para_id)
        for child in elem:
            _walk(child, in_table)

    _walk(root, False)

    all_present_ids = [pid for pid in (body_ids + table_cell_ids) if pid]
    seen: set[str] = set()
    duplicates: list[str] = []
    for pid in all_present_ids:
        if pid in seen and pid not in duplicates:
            duplicates.append(pid)
        seen.add(pid)

    return {
        "body_paragraph_count": len(body_ids),
        "table_cell_paragraph_count": len(table_cell_ids),
        "body_missing_para_id_count": sum(1 for pid in body_ids if not pid),
        "table_cell_missing_para_id_count": sum(1 for pid in table_cell_ids if not pid),
        "total_unique_para_ids": len(set(all_present_ids)),
        "duplicate_para_ids": sorted(duplicates),
    }


def _paragraph_text_by_para_id(document_xml: bytes) -> dict[str, str]:
    root = ET.fromstring(document_xml)
    out: dict[str, str] = {}
    para_id_attr = _qn(_W14_NS, "paraId")
    t_tag = _qn(_W_NS, "t")
    for p in root.iter(_qn(_W_NS, "p")):
        pid = p.get(para_id_attr)
        if not pid:
            continue
        out[pid] = "".join(t.text or "" for t in p.iter(t_tag))
    return out


# ---------------------------------------------------------------------------
# 3. Relationship / media reachability.
# ---------------------------------------------------------------------------

def _owning_part_for_rels(rels_name: str) -> str:
    """``word/_rels/document.xml.rels`` -> ``word/document.xml``;
    ``_rels/.rels`` -> ``.rels`` stripped -> ``""`` (package-root pseudo-part,
    only its Target resolution against "" matters -- root rels targets are
    already resolved relative to the package root)."""
    slash = rels_name.rfind("/_rels/")
    if slash == -1:
        # "_rels/.rels" at the package root.
        base = rels_name.split("/")[-1]
        return base[: -len(".rels")] if base.endswith(".rels") else base
    parent_dir = rels_name[:slash]
    base = rels_name[slash + len("/_rels/") :]
    if base.endswith(".rels"):
        base = base[: -len(".rels")]
    return f"{parent_dir}/{base}" if parent_dir else base


def _resolve_rel_target(owning_part: str, target: str) -> str:
    """Resolve a relationship ``Target`` (posix-style, zip-internal) against
    the directory containing ``owning_part``. Deliberately implemented with
    plain string splitting rather than ``os.path`` -- ZIP-internal paths are
    always ``/``-separated regardless of host OS, and ``os.path.join`` on
    Windows would silently produce backslash-separated paths that never match
    ``ZipFile.namelist()``."""
    if target.startswith("/"):
        return target.lstrip("/")
    base_parts = owning_part.split("/")[:-1] if "/" in owning_part else []
    for piece in target.split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if base_parts:
                base_parts.pop()
            continue
        base_parts.append(piece)
    return "/".join(base_parts)


def _parse_rels(raw: bytes) -> dict[str, dict[str, str]]:
    root = ET.fromstring(raw)
    rels: dict[str, dict[str, str]] = {}
    for rel in root.iter(_qn(_PKG_REL_NS, "Relationship")):
        rid = rel.get("Id")
        if not rid:
            continue
        rels[rid] = {
            "type": rel.get("Type", ""),
            "target": rel.get("Target", ""),
            "mode": rel.get("TargetMode", "Internal"),
        }
    return rels


def _collect_referenced_rids(root: ET.Element) -> set[str]:
    prefix = f"{{{_R_NS}}}"
    rids: set[str] = set()
    for elem in root.iter():
        for attr_name, attr_value in elem.attrib.items():
            if attr_name.startswith(prefix) and attr_value:
                rids.add(attr_value)
    return rids


def check_relationship_reachability(raw: bytes) -> dict[str, Any]:
    """Verify every internal relationship target resolves to a real ZIP
    entry, every ``r:id``/``r:embed``/... reference used inside a part
    resolves to a relationship actually declared for that part, and every
    ``word/media/*`` file is the target of at least one relationship
    (orphaned media -- embedded but never referenced, or referenced from a
    relationship that itself doesn't resolve).

    ``TargetMode="External"`` relationships (hyperlinks to real URLs) are
    never treated as unresolved -- they are not supposed to resolve inside
    the ZIP.
    """
    result: dict[str, Any] = {
        "ok": True,
        "checked_rels_parts": [],
        "unresolved_relationship_targets": [],
        "unresolved_rids": [],
        "orphaned_media": [],
        "errors": [],
    }
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            namelist = set(zf.namelist())
            rels_parts = sorted(n for n in namelist if n.endswith(".rels"))
            all_resolved_targets: set[str] = set()

            parsed_rels_by_part: dict[str, dict[str, dict[str, str]]] = {}
            for rels_name in rels_parts:
                try:
                    rels = _parse_rels(zf.read(rels_name))
                except (KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
                    result["errors"].append(f"{rels_name}: {exc}")
                    continue
                result["checked_rels_parts"].append(rels_name)
                owning_part = _owning_part_for_rels(rels_name)
                parsed_rels_by_part[owning_part] = rels
                for rid, rel in rels.items():
                    if rel.get("mode") == "External":
                        continue
                    resolved = _resolve_rel_target(owning_part, rel.get("target", ""))
                    all_resolved_targets.add(resolved)
                    if resolved not in namelist:
                        result["unresolved_relationship_targets"].append(
                            {
                                "rels_part": rels_name,
                                "rid": rid,
                                "target": rel.get("target", ""),
                                "resolved": resolved,
                            }
                        )

            for owning_part, rels in parsed_rels_by_part.items():
                if owning_part not in namelist:
                    continue
                try:
                    part_root = ET.fromstring(zf.read(owning_part))
                except (KeyError, ET.ParseError, zipfile.BadZipFile):
                    continue
                for rid in _collect_referenced_rids(part_root):
                    if rid not in rels:
                        result["unresolved_rids"].append({"part": owning_part, "rid": rid})

            media_files = sorted(n for n in namelist if n.startswith("word/media/"))
            for media in media_files:
                if media not in all_resolved_targets:
                    result["orphaned_media"].append(media)
    except (OSError, zipfile.BadZipFile) as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")

    result["ok"] = not (
        result["errors"]
        or result["unresolved_relationship_targets"]
        or result["unresolved_rids"]
        or result["orphaned_media"]
    )
    return result


# ---------------------------------------------------------------------------
# 4. Equation / caption counts.
# ---------------------------------------------------------------------------

def count_equations_and_captions(document_xml: bytes) -> dict[str, Any]:
    """Count ``<m:oMath>`` equations and Word-native captions in
    ``word/document.xml``.

    Captions are counted two ways (matching how real Word captions are
    represented -- see extensions/meridian-docs/meridian_docs/docs_intel.py's
    own ``_CAPTION_STYLE`` / SEQ-field handling for the same convention this
    mirrors, without importing that module):

      * ``caption_style_paragraph_count`` -- paragraphs whose ``w:pStyle``
        is ``"Caption"``.
      * ``seq_field_count`` -- ``SEQ`` field instructions found in either
        simple fields (``w:fldSimple[@w:instr]``) or complex fields
        (``w:instrText``), which is how a caption's auto-number is actually
        implemented regardless of paragraph style.

    Both counts are reported rather than collapsed into one, since a
    document can have one without the other (a caption-styled paragraph with
    no SEQ field is dead/plain-text; a SEQ field can theoretically live
    outside a Caption-styled paragraph).
    """
    root = ET.fromstring(document_xml)
    equation_count = sum(1 for _ in root.iter(_qn(_M_NS, "oMath")))

    p_style_tag = _qn(_W_NS, "pStyle")
    ppr_tag = _qn(_W_NS, "pPr")
    val_attr = _qn(_W_NS, "val")
    caption_style_count = 0
    for p in root.iter(_qn(_W_NS, "p")):
        ppr = p.find(ppr_tag)
        if ppr is None:
            continue
        pstyle = ppr.find(p_style_tag)
        if pstyle is not None and (pstyle.get(val_attr) or "").strip().lower() == "caption":
            caption_style_count += 1

    instr_attr = _qn(_W_NS, "instr")
    seq_field_count = 0
    for fld in root.iter(_qn(_W_NS, "fldSimple")):
        if "SEQ " in (fld.get(instr_attr) or ""):
            seq_field_count += 1
    for instr_text in root.iter(_qn(_W_NS, "instrText")):
        if "SEQ " in (instr_text.text or ""):
            seq_field_count += 1

    return {
        "equation_count": equation_count,
        "caption_style_paragraph_count": caption_style_count,
        "seq_field_count": seq_field_count,
    }


# ---------------------------------------------------------------------------
# 5. Required text / locator assertions.
# ---------------------------------------------------------------------------

def check_required_text(
    document_xml: bytes,
    required_texts: Sequence[str] = (),
    required_locators: Sequence[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Assert that every string in ``required_texts`` appears SOMEWHERE in
    the document's extracted plain text, and that every
    ``(para_id, expected_substring)`` pair in ``required_locators`` names a
    paragraph that actually exists (by ``w14:paraId``) AND contains the
    expected substring -- a much stronger, position-anchored assertion than
    a bare substring search, since it pins the requirement to a specific
    paragraph rather than "this text exists somewhere in a 40-page document".
    """
    root = ET.fromstring(document_xml)
    full_text = "".join(t.text or "" for t in root.iter(_qn(_W_NS, "t")))
    missing_texts = [needle for needle in required_texts if needle not in full_text]

    para_text_by_id = _paragraph_text_by_para_id(document_xml)
    missing_locators: list[dict[str, Any]] = []
    for para_id, expected_substring in required_locators:
        actual = para_text_by_id.get(para_id)
        if actual is None:
            missing_locators.append(
                {
                    "para_id": para_id,
                    "expected": expected_substring,
                    "reason": "paraId not found in document",
                }
            )
        elif expected_substring not in actual:
            missing_locators.append(
                {
                    "para_id": para_id,
                    "expected": expected_substring,
                    "actual": actual,
                    "reason": "paraId found but text does not contain expected substring",
                }
            )

    return {
        "ok": not missing_texts and not missing_locators,
        "missing_required_texts": missing_texts,
        "missing_required_locators": missing_locators,
        "checked_required_text_count": len(required_texts),
        "checked_required_locator_count": len(required_locators),
    }


# ---------------------------------------------------------------------------
# 6. Stale-source refusal.
# ---------------------------------------------------------------------------

def check_stale_source(
    docx_path: str,
    *,
    source_path: str | None = None,
    expected_source_sha256: str | None = None,
    expected_min_mtime: float | None = None,
) -> dict[str, Any]:
    """Refuse a completion claim when the produced DOCX cannot be shown to
    postdate the source it should have been (re)generated from.

    Three independent staleness signals -- any one flags ``stale=True``:

      1. ``source_path`` mtime ordering: the docx's mtime must be strictly
         newer than ``source_path``'s mtime. Catches "edited the source,
         forgot to regenerate the docx".
      2. ``expected_source_sha256``: if the caller recorded the source's
         hash at the time this artifact was supposedly generated/verified,
         the source's CURRENT hash must still match. This is a stronger,
         tamper-evident signal than mtime alone (mtimes can be reset by a
         checkout, a copy, or simple clock skew; content hashes cannot).
      3. ``expected_min_mtime``: an explicit epoch-seconds floor (e.g. "this
         claim/session started at this timestamp") -- the docx must have
         been written strictly after it, catching a leftover artifact from
         an unrelated earlier run being passed off as this run's output.

    Every signal is optional; with none supplied, only the docx's own
    existence/readability is checked (``stale=False`` unless the file itself
    cannot be stat'd).
    """
    result: dict[str, Any] = {
        "ok": True,
        "stale": False,
        "reasons": [],
        "docx_mtime": None,
        "source_mtime": None,
        "source_sha256": None,
    }
    try:
        docx_mtime = os.path.getmtime(docx_path)
    except OSError as exc:
        result["ok"] = False
        result["stale"] = True
        result["reasons"].append(f"could not stat docx_path: {exc}")
        return result
    result["docx_mtime"] = docx_mtime

    if source_path:
        try:
            source_mtime = os.path.getmtime(source_path)
        except OSError as exc:
            result["stale"] = True
            result["reasons"].append(f"could not stat source_path: {exc}")
        else:
            result["source_mtime"] = source_mtime
            if docx_mtime <= source_mtime:
                result["stale"] = True
                result["reasons"].append(
                    "docx mtime is not newer than source_path's mtime -- the "
                    "artifact was not regenerated after its source last changed "
                    f"(docx_mtime={docx_mtime}, source_mtime={source_mtime})"
                )

        if expected_source_sha256:
            try:
                with open(source_path, "rb") as handle:
                    current_hash = hashlib.sha256(handle.read()).hexdigest()
            except OSError as exc:
                result["stale"] = True
                result["reasons"].append(f"could not hash source_path: {exc}")
            else:
                result["source_sha256"] = current_hash
                if current_hash != expected_source_sha256:
                    result["stale"] = True
                    result["reasons"].append(
                        "source_path's current sha256 does not match "
                        "expected_source_sha256 -- the source has changed since "
                        "this artifact was generated/verified; refusing to treat "
                        "it as complete"
                    )

    if expected_min_mtime is not None and docx_mtime <= expected_min_mtime:
        result["stale"] = True
        result["reasons"].append(
            f"docx mtime ({docx_mtime}) is not after expected_min_mtime "
            f"({expected_min_mtime}) -- looks like a leftover artifact from a "
            "prior run rather than freshly produced output"
        )

    result["ok"] = not result["stale"]
    return result


# ---------------------------------------------------------------------------
# 7. External render receipt -- Word/COM only, four explicit states.
# ---------------------------------------------------------------------------

def _default_render_checker(docx_path: str) -> dict[str, Any]:
    """Best-effort delegation to
    ``meridian_docs.render_gate.check_word_com_render_receipt`` -- the ONLY
    render signal this gate accepts (a LibreOffice/soffice conversion is
    explicitly NOT accepted here; see module docstring). Guarded, dotted-
    string ``importlib`` resolution (never a bare ``from meridian_docs import
    ...``) exactly like ``meridian.docx_integrity_gate``'s sibling
    resolution -- ``extensions/meridian-docs`` is a separately-installed
    extension, not a hard dependency of this fallback tool, and its absence
    must degrade to :data:`RENDER_UNAVAILABLE`, never a crash and never a
    silent success.
    """
    try:
        module = importlib.import_module("meridian_docs.render_gate")
    except Exception:  # noqa: BLE001 -- optional sibling; any import failure degrades
        return {
            "status": RENDER_UNAVAILABLE,
            "reason": (
                "meridian_docs.render_gate is not importable in this "
                "environment (extensions/meridian-docs is an optional, "
                "separately-installed package)"
            ),
        }
    checker = getattr(module, "check_word_com_render_receipt", None)
    if not callable(checker):
        return {
            "status": RENDER_UNAVAILABLE,
            "reason": (
                "meridian_docs.render_gate.check_word_com_render_receipt is "
                "not available in the installed meridian_docs version"
            ),
        }
    try:
        raw_result = checker(docx_path)
    except Exception as exc:  # noqa: BLE001 -- a checker must never crash this gate
        return {"status": RENDER_FAILED, "reason": f"{type(exc).__name__}: {exc}"}
    if not isinstance(raw_result, dict):
        return {
            "status": RENDER_UNVERIFIED,
            "reason": "render checker returned a non-dict result",
        }
    status_map = {
        "rendered": RENDER_VERIFIED,
        "unavailable-with-reason": RENDER_UNAVAILABLE,
        "failed": RENDER_FAILED,
    }
    mapped_status = status_map.get(raw_result.get("status"), RENDER_UNVERIFIED)
    return {
        "status": mapped_status,
        "reason": raw_result.get("reason"),
        "backend": raw_result.get("backend"),
        "detail": raw_result.get("detail"),
        "source_status": raw_result.get("status"),
    }


# ---------------------------------------------------------------------------
# The orchestrator.
# ---------------------------------------------------------------------------

@dataclass
class CompletionRequirements:
    """Declarative pass/fail knobs for :func:`run_completion_gate`.

    Every field is optional and defaults to "don't gate on this" -- the gate
    always COMPUTES and reports every structural signal (paraId report,
    equation/caption counts, relationship reachability, stale-source check,
    ...) regardless of which requirements are set; these fields only control
    which of the computed signals are allowed to flip the overall ``ready``
    verdict to ``False``. Duplicate paraIds, ZIP CRC failures, malformed XML,
    and unresolved relationships/orphaned media are ALWAYS hard failures --
    they are not policy choices, they are structural corruption.
    """

    required_texts: Sequence[str] = field(default_factory=tuple)
    required_locators: Sequence[tuple[str, str]] = field(default_factory=tuple)
    min_equation_count: int | None = None
    min_caption_count: int | None = None
    source_path: str | None = None
    expected_source_sha256: str | None = None
    expected_min_mtime: float | None = None
    require_render_verified: bool = False


def run_completion_gate(
    docx_path: str,
    requirements: "CompletionRequirements | None" = None,
    *,
    render_checker: "RenderChecker | None" = None,
    skip_render_check: bool = False,
) -> dict[str, Any]:
    """The local DOCX completion gate: inspect the FULL OOXML package
    structure of ``docx_path`` and return one comprehensive, fail-closed
    verdict.

    Render-receipt contract (four explicit states, never silently folded
    into success):

      * :data:`RENDER_VERIFIED` -- a real Word COM render was attempted and
        succeeded. The ONLY status meaning "an external application actually
        opened and rendered this document."
      * :data:`RENDER_FAILED` -- Word COM was available and the render
        attempt raised/errored.
      * :data:`RENDER_UNAVAILABLE` -- Word COM automation is not usable in
        this environment (non-Windows platform, ``pywin32`` missing, Word
        not installed, or the optional ``meridian_docs`` extension itself
        not installed). This is an ENVIRONMENT fact, not a statement about
        the document.
      * :data:`RENDER_UNVERIFIED` -- the render check was explicitly skipped
        (``skip_render_check=True``) or a checker returned something this
        gate could not interpret. Distinct from ``RENDER_UNAVAILABLE``: this
        means "we chose not to check", not "we could not check".

    Structural checks (:func:`check_zip_integrity`,
    :func:`enumerate_para_ids`, :func:`check_relationship_reachability`,
    :func:`count_equations_and_captions`, :func:`check_required_text`,
    :func:`check_stale_source`) always run and are always reported in full,
    regardless of ``requirements``. ``ready`` is ``True`` only when every
    hard structural check passes AND every requirement in ``requirements``
    that was actually set is satisfied.

    Never raises for a bad/missing ``docx_path`` or malformed XML -- those
    become reported failures (``ready=False`` with an itemized ``reasons``
    list), matching every OTHER read-only status-dict convention in this
    codebase's DOCX tooling (``render_gate.check_render_capability``,
    ``docs_intel.read_document_snapshot``).
    """
    requirements = requirements or CompletionRequirements()
    report: dict[str, Any] = {
        "schema_version": GATE_SCHEMA_VERSION,
        "docx_path": docx_path,
        "ready": False,
        "reasons": [],
    }

    if not docx_path or not isinstance(docx_path, str) or not docx_path.strip():
        report["reasons"].append("docx_path must be a non-empty string")
        return report
    if not os.path.isfile(docx_path):
        report["reasons"].append(f"docx_path does not exist or is not a file: {docx_path!r}")
        return report

    try:
        with open(docx_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        report["reasons"].append(f"could not read docx_path: {exc}")
        return report

    zip_integrity = check_zip_integrity(raw)
    report["zip_integrity"] = zip_integrity
    if not zip_integrity["ok"]:
        report["reasons"].append("zip_integrity_failed")

    relationship_check = check_relationship_reachability(raw)
    report["relationship_check"] = relationship_check
    if not relationship_check["ok"]:
        report["reasons"].append("relationship_reachability_failed")

    document_xml: bytes | None = None
    if "word/document.xml" not in zip_integrity["missing_required_parts"]:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                document_xml = zf.read("word/document.xml")
        except (KeyError, OSError, zipfile.BadZipFile) as exc:
            report["reasons"].append(f"could not read word/document.xml: {exc}")

    para_id_report = None
    equation_caption_report = None
    required_text_report = None
    if document_xml is not None:
        try:
            para_id_report = enumerate_para_ids(document_xml)
        except ET.ParseError as exc:
            report["reasons"].append(f"document.xml not well-formed for paraId scan: {exc}")
        try:
            equation_caption_report = count_equations_and_captions(document_xml)
        except ET.ParseError as exc:
            report["reasons"].append(
                f"document.xml not well-formed for equation/caption scan: {exc}"
            )
        try:
            required_text_report = check_required_text(
                document_xml, requirements.required_texts, requirements.required_locators
            )
        except ET.ParseError as exc:
            report["reasons"].append(
                f"document.xml not well-formed for required-text scan: {exc}"
            )

    report["para_id_report"] = para_id_report
    if para_id_report is not None and para_id_report["duplicate_para_ids"]:
        report["reasons"].append(
            "duplicate_para_ids_found: " + ", ".join(para_id_report["duplicate_para_ids"])
        )

    report["equation_caption_report"] = equation_caption_report
    if equation_caption_report is not None:
        if (
            requirements.min_equation_count is not None
            and equation_caption_report["equation_count"] < requirements.min_equation_count
        ):
            report["reasons"].append(
                f"equation_count {equation_caption_report['equation_count']} below "
                f"required minimum {requirements.min_equation_count}"
            )
        if (
            requirements.min_caption_count is not None
            and equation_caption_report["caption_style_paragraph_count"]
            < requirements.min_caption_count
        ):
            report["reasons"].append(
                "caption_style_paragraph_count "
                f"{equation_caption_report['caption_style_paragraph_count']} below "
                f"required minimum {requirements.min_caption_count}"
            )

    report["required_text_report"] = required_text_report
    if required_text_report is not None and not required_text_report["ok"]:
        report["reasons"].append("required_text_or_locator_missing")

    stale_report = check_stale_source(
        docx_path,
        source_path=requirements.source_path,
        expected_source_sha256=requirements.expected_source_sha256,
        expected_min_mtime=requirements.expected_min_mtime,
    )
    report["stale_source_report"] = stale_report
    if stale_report["stale"]:
        report["reasons"].append("stale_source_refused: " + "; ".join(stale_report["reasons"]))

    if skip_render_check:
        render_report: dict[str, Any] = {
            "status": RENDER_UNVERIFIED,
            "reason": "render check explicitly skipped by caller (skip_render_check=True)",
        }
    else:
        checker = render_checker if render_checker is not None else _default_render_checker
        try:
            render_report = checker(docx_path)
        except Exception as exc:  # noqa: BLE001 -- a render checker must never crash the gate
            render_report = {"status": RENDER_FAILED, "reason": f"{type(exc).__name__}: {exc}"}
        if not isinstance(render_report, dict) or render_report.get("status") not in RENDER_STATUSES:
            render_report = {
                "status": RENDER_UNVERIFIED,
                "reason": "render checker returned an invalid/unrecognized result",
            }

    report["render_report"] = render_report
    if requirements.require_render_verified and render_report["status"] != RENDER_VERIFIED:
        report["reasons"].append(
            "require_render_verified=True but render status is "
            f"{render_report['status']!r}: {render_report.get('reason') or '(no reason given)'}"
        )

    report["ready"] = not report["reasons"]
    return report


# ---------------------------------------------------------------------------
# CLI entry point -- so an executor can run this as a standalone script
# without importing it as part of any package (tools/meridian_fallbacks is a
# fallback toolkit, meant to work even when nothing else in this repo is on
# sys.path).
# ---------------------------------------------------------------------------

def _parse_locator(text: str) -> tuple[str, str]:
    if "::" not in text:
        raise argparse.ArgumentTypeError(
            f"--required-locator must be PARA_ID::EXPECTED_TEXT, got: {text!r}"
        )
    para_id, _, expected = text.partition("::")
    return para_id, expected


def main(argv: "Sequence[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="docx_completion_gate",
        description=(
            "Inspect the full OOXML package structure of a .docx file and "
            "report a fail-closed completion verdict."
        ),
    )
    parser.add_argument("docx_path")
    parser.add_argument("--required-text", action="append", default=[], dest="required_texts")
    parser.add_argument(
        "--required-locator",
        action="append",
        default=[],
        dest="required_locators",
        type=_parse_locator,
        help="PARA_ID::EXPECTED_TEXT, repeatable",
    )
    parser.add_argument("--min-equations", type=int, default=None)
    parser.add_argument("--min-captions", type=int, default=None)
    parser.add_argument("--source-path", default=None)
    parser.add_argument("--expected-source-sha256", default=None)
    parser.add_argument("--expected-min-mtime", type=float, default=None)
    parser.add_argument("--require-render-verified", action="store_true")
    parser.add_argument("--skip-render-check", action="store_true")
    args = parser.parse_args(argv)

    requirements = CompletionRequirements(
        required_texts=tuple(args.required_texts),
        required_locators=tuple(args.required_locators),
        min_equation_count=args.min_equations,
        min_caption_count=args.min_captions,
        source_path=args.source_path,
        expected_source_sha256=args.expected_source_sha256,
        expected_min_mtime=args.expected_min_mtime,
        require_render_verified=args.require_render_verified,
    )
    report = run_completion_gate(
        args.docx_path, requirements, skip_render_check=args.skip_render_check
    )
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["ready"] else 1


# Lineage note: this module ports and hardens the ad hoc
# ``_codex_docx_completion_gate`` helper named in sprint item 8419f55f. No
# surviving implementation was found anywhere in the repo to port verbatim
# (see module docstring) -- ``run_completion_gate`` is the ported/hardened
# replacement. Alias kept for anyone grepping for the old name.
_codex_docx_completion_gate = run_completion_gate


if __name__ == "__main__":
    sys.exit(main())
