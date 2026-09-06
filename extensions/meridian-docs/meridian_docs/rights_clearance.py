"""Fail-closed rights and permission clearance for document artifacts.

This module is deliberately separate from structural DOCX ownership and from
bibliographic formatting.  A citation, DOI, or Zotero record identifies a
source; it does *not* prove that a figure, table, screenshot, map, quotation,
or data display may be reused.  Rights clearance therefore requires an
explicit source record and evidence whose scope covers the intended
publication uses.

The module is general-purpose and venue-configurable.  The built-in
``springer_jcshm`` profile captures the journal's print/online and
supplementary-material scope, but callers may pass another profile.  The
decision engine is intentionally conservative:

``clear`` / ``clear_with_attribution``
    Operationally allowed for the requested scopes.
``permission_required`` / ``blocked`` / ``unresolved`` / ``inconclusive``
    Submission-blocking until a human supplies better evidence or changes the
    artifact's source/use classification.

No result is a legal opinion.  It is an auditable release gate that refuses to
turn missing or ambiguous evidence into an approval.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET


SCHEMA_VERSION = "1.0"
ALLOWED_DECISIONS = {"clear", "clear_with_attribution"}
BLOCKING_DECISIONS = {
    "permission_required",
    "blocked",
    "unresolved",
    "inconclusive",
}
SOURCE_KINDS = {
    "original",
    "adapted",
    "reproduced",
    "third_party",
    "public_domain",
    "cc_license",
    "government",
    "dataset",
    "unknown",
}
SOURCE_IDENTITY_STATUSES = {
    "confirmed",
    "mismatch",
    "unresolved",
    "not_checked",
    "not_applicable",
}
RELEASE_ACTIONS = {
    "retain",
    "permission_request",
    "redraw",
    "remove",
    "prose_only",
    "review",
}
USE_SCOPES = {
    "journal_print",
    "journal_online",
    "supplementary_information",
    "repository",
    "arxiv",
    "commercial",
}

SPRINGER_JCSHM_PROFILE: dict[str, Any] = {
    "profile_id": "springer_jcshm",
    "name": "Journal of Civil Structural Health Monitoring",
    "required_scopes": [
        "journal_print",
        "journal_online",
        "supplementary_information",
    ],
    "requires_credit_line": True,
    "notes": (
        "JCSHM/Springer requires permission evidence for previously published "
        "figures, tables, and text in print and online formats; supplementary "
        "files are published as received."
    ),
}

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_W_T = f"{{{_W}}}t"
_W_INSTR = f"{{{_W}}}instrText"
_W_DELT = f"{{{_W}}}delText"
_W_P = f"{{{_W}}}p"
_W_DRAWING = f"{{{_W}}}drawing"
_R_EMBED = f"{{{_R}}}embed"

_DOI_RE = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", re.I)
_URL_RE = re.compile(r"https?://[^\s<>\]]+", re.I)
_CITATION_RE = re.compile(r"\[(\d+(?:\s*[-,]\s*\d+)*)\]")
_BIB_RE = re.compile(r"^\s*(?:\[(?P<bracket>\d+)\]|(?P<decimal>\d+)[.)])\s+(?P<text>.*)$")
_CAPTION_RE = re.compile(
    # Caption identity is anchored at paragraph start.  An unanchored match
    # would mistake ordinary prose such as "as illustrated in Fig. 7" for a
    # second caption and create duplicate rights obligations.
    r"^\s*(?P<kind>figure|fig\.?|table)\s+(?P<label>[A-Z]?\d+(?:[.\-]\d+)*)\b",
    re.I,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _doi_url(value: str) -> str | None:
    match = _DOI_RE.search(_text(value))
    if not match:
        return None
    return f"https://doi.org/{match.group(1).rstrip('.,;') }"


def _first_doi(values: Iterable[Any]) -> str | None:
    for value in values:
        found = _doi_url(_text(value))
        if found:
            return found
    return None


def _source_links(record: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    values.extend(record.get("source_urls") or [])
    values.extend(record.get("evidence_urls") or [])
    for key in ("source_url", "publisher_url", "doi", "DOI", "url"):
        if record.get(key):
            values.append(record[key])
    links: list[str] = []
    for value in values:
        value = _text(value)
        if not value:
            continue
        doi = _doi_url(value)
        link = doi or value
        if link not in links:
            links.append(link)
    return links


def normalize_zotero_record(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize common Zotero/CSL-JSON keys without treating them as rights."""
    if not isinstance(item, dict):
        return {}
    result = dict(item)
    doi = item.get("DOI") or item.get("doi") or item.get("extra", "")
    doi_link = _doi_url(_text(doi))
    if doi_link:
        result["doi_url"] = doi_link
        result.setdefault("source_url", doi_link)
    url = item.get("URL") or item.get("url")
    if url:
        result.setdefault("source_url", _text(url))
    result["identity_source"] = "zotero"
    result["identity_verified"] = bool(
        item.get("title") or item.get("DOI") or item.get("doi") or item.get("key")
    )
    result["rights_evidence"] = False
    return result


def _license_token(record: dict[str, Any]) -> str:
    raw = " ".join(
        _text(record.get(key))
        for key in ("license_spdx", "license_name", "license", "rights_statement")
    ).casefold()
    raw = re.sub(r"\s+", " ", raw)
    if "cc0" in raw or "public domain" in raw:
        return "cc0"
    if "cc by-nc-nd" in raw or "cc-by-nc-nd" in raw:
        return "cc-by-nc-nd"
    if "cc by-nc-sa" in raw or "cc-by-nc-sa" in raw:
        return "cc-by-nc-sa"
    if "cc by-nc" in raw or "cc-by-nc" in raw:
        return "cc-by-nc"
    if "cc by-nd" in raw or "cc-by-nd" in raw:
        return "cc-by-nd"
    if "cc by-sa" in raw or "cc-by-sa" in raw:
        return "cc-by-sa"
    if re.search(r"\bcc\s*by\b|cc-by", raw):
        return "cc-by"
    if "all rights reserved" in raw or "proprietary" in raw:
        return "all-rights-reserved"
    return ""


def _evidence_types(record: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for evidence in record.get("evidence") or []:
        if isinstance(evidence, dict) and evidence.get("type"):
            result.add(_text(evidence["type"]).casefold())
    for key in ("permission_evidence_paths", "evidence_paths"):
        if record.get(key):
            result.add("permission_document")
    if record.get("permission_granted") is True:
        result.add("permission_document")
    if record.get("author_attestation"):
        result.add("author_attestation")
    return result


def _evidence_scopes(record: dict[str, Any]) -> set[str]:
    scopes: set[str] = set(_text(x) for x in (record.get("permitted_uses") or []))
    for evidence in record.get("evidence") or []:
        if not isinstance(evidence, dict):
            continue
        scopes.update(_text(x) for x in (evidence.get("scopes") or []))
    return {scope for scope in scopes if scope}


def _base_result(artifact: dict[str, Any], decision: str, reasons: list[str]) -> dict[str, Any]:
    kind = _text(artifact.get("source_kind")).casefold()
    source_identity_status = (
        _text(artifact.get("source_identity_status")).casefold()
        or ("confirmed" if kind == "original" and artifact.get("author_confirmed") else "not_checked")
    )
    return {
        "artifact_id": _text(artifact.get("artifact_id")),
        "decision": decision,
        "allowed": decision in ALLOWED_DECISIONS,
        "reasons": reasons,
        "source_identity_status": source_identity_status,
        "release_action": _text(artifact.get("release_action")).casefold() or "retain",
        "source_reference_id": _text(artifact.get("source_reference_id")) or None,
        "source_links": _source_links(artifact),
    }


def evaluate_artifact(
    artifact: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate one manifest artifact using a conservative, deterministic gate."""
    profile = profile or SPRINGER_JCSHM_PROFILE
    if not isinstance(artifact, dict):
        return {
            "artifact_id": "",
            "decision": "unresolved",
            "allowed": False,
            "reasons": ["artifact record is not an object"],
        }
    reasons: list[str] = []
    kind = _text(artifact.get("source_kind")).casefold() or "unknown"
    use_class = _text(artifact.get("use_class")).casefold() or "reproduced"
    source_identity_status = (
        _text(artifact.get("source_identity_status")).casefold()
        or ("confirmed" if kind == "original" and artifact.get("author_confirmed") else "not_checked")
    )
    release_action = _text(artifact.get("release_action")).casefold() or "retain"
    if kind not in SOURCE_KINDS:
        reasons.append(f"unknown source_kind {kind!r}")
        return _base_result(artifact, "unresolved", reasons)
    if source_identity_status not in SOURCE_IDENTITY_STATUSES:
        reasons.append(f"unknown source_identity_status {source_identity_status!r}")
    if release_action not in RELEASE_ACTIONS:
        reasons.append(f"unknown release_action {release_action!r}")
    required_scopes = set(profile.get("required_scopes") or [])
    if not required_scopes:
        return _base_result(artifact, "inconclusive", ["publication profile has no required scopes"])
    if not _text(artifact.get("artifact_id")):
        reasons.append("artifact_id is required")
    if not _text(artifact.get("asset_sha256")):
        reasons.append("asset_sha256 is required to bind the decision to the submitted asset")
    if kind != "original" and not _text(artifact.get("source_reference_id")):
        reasons.append("non-original material requires an explicit source_reference_id")
    if kind != "original" and not _source_links(artifact):
        reasons.append("source DOI or URL is missing")
    if artifact.get("zotero_record") and not isinstance(artifact["zotero_record"], dict):
        reasons.append("zotero_record must be an object")
    if artifact.get("zotero_record") and artifact["zotero_record"].get("rights_evidence"):
        reasons.append("Zotero metadata is identity evidence only, not permission evidence")
    if reasons:
        return _base_result(artifact, "unresolved", reasons)

    # A citation/DOI can identify a paper without proving that the embedded
    # asset came from that paper.  Keep that review claim explicit and fail
    # closed for every non-original asset until a human or source-specific
    # verification step records a confirmed match.  This catches the exact
    # class of defect where a plausible bibliography entry is attached to the
    # wrong historical image (or an image whose true source was never found).
    if kind != "original":
        if source_identity_status == "mismatch":
            return _base_result(
                artifact,
                "blocked",
                ["embedded asset does not match the declared source/reference"],
            )
        if source_identity_status != "confirmed":
            return _base_result(
                artifact,
                "unresolved",
                [
                    "source identity is not confirmed for the embedded asset; "
                    "verify the exact figure/table and its cited source before clearance",
                ],
            )
    elif source_identity_status == "mismatch":
        return _base_result(
            artifact,
            "blocked",
            ["manifest marks the embedded original asset as source-mismatched"],
        )

    if release_action in {"remove", "prose_only", "redraw"}:
        return _base_result(
            artifact,
            "blocked",
            [f"release_action={release_action!r} requires replacing or removing the current embedded asset"],
        )

    evidence_types = _evidence_types(artifact)
    evidence_scopes = _evidence_scopes(artifact)
    missing_scopes = sorted(required_scopes - evidence_scopes)
    credit_required = bool(profile.get("requires_credit_line", True))
    credit_missing = credit_required and kind != "original" and not _text(artifact.get("credit_line"))
    license_token = _license_token(artifact)

    if kind == "original":
        if "author_attestation" not in evidence_types and not artifact.get("author_confirmed"):
            return _base_result(
                artifact,
                "unresolved",
                ["original status requires an author_attestation or author_confirmed=true"],
            )
        if missing_scopes:
            return _base_result(
                artifact,
                "inconclusive",
                [f"author confirmation does not enumerate required scopes: {', '.join(missing_scopes)}"],
            )
        return _base_result(artifact, "clear", ["author-confirmed original asset with required scopes"])

    if "permission_document" in evidence_types:
        if missing_scopes:
            return _base_result(
                artifact,
                "permission_required",
                [f"permission evidence does not cover: {', '.join(missing_scopes)}"],
            )
        if credit_missing:
            return _base_result(artifact, "inconclusive", ["permission exists but a required credit line is missing"])
        return _base_result(artifact, "clear_with_attribution", ["explicit permission evidence covers all requested scopes"])

    # The declared source kind is authoritative.  A stray ``license_name``
    # copied from Zotero or a bibliography field must not silently turn an
    # ``unknown``/``third_party`` artifact into a licensed one.
    if kind == "cc_license":
        if license_token == "cc0":
            if missing_scopes:
                return _base_result(artifact, "inconclusive", [f"CC0 evidence does not cover: {', '.join(missing_scopes)}"])
            return _base_result(artifact, "clear_with_attribution" if credit_missing else "clear", ["explicit CC0/public-domain license evidence"])
        if not _text(artifact.get("license_url")) and not any("license" in x for x in evidence_types):
            return _base_result(artifact, "unresolved", ["CC license name is present but license deed/evidence URL is missing"])
        if license_token in {"cc-by-nc", "cc-by-nc-sa", "cc-by-nc-nd"}:
            return _base_result(artifact, "permission_required", ["non-commercial CC license is not accepted for journal clearance without explicit permission"])
        if license_token == "cc-by-nd" and use_class in {"adapted", "derived", "modified"}:
            return _base_result(artifact, "blocked", ["CC BY-ND does not authorize an adapted/modified version"])
        if missing_scopes:
            return _base_result(artifact, "inconclusive", [f"license evidence does not enumerate required scopes: {', '.join(missing_scopes)}"])
        if credit_missing:
            return _base_result(artifact, "inconclusive", ["CC license permits reuse only with a recorded credit line"])
        return _base_result(artifact, "clear_with_attribution", [f"{license_token.upper()} evidence covers the requested use"])

    if kind in {"third_party", "adapted", "reproduced", "dataset", "government", "unknown", "public_domain"}:
        if kind == "public_domain" and (license_token == "cc0" or "public domain" in _text(artifact.get("rights_statement")).casefold()):
            if missing_scopes:
                return _base_result(artifact, "inconclusive", [f"public-domain evidence does not cover: {', '.join(missing_scopes)}"])
            return _base_result(artifact, "clear_with_attribution" if credit_missing else "clear", ["explicit public-domain evidence"])
        return _base_result(
            artifact,
            "permission_required",
            ["third-party/reproduced/adapted material needs explicit permission or a compatible license evidence record"],
        )

    return _base_result(artifact, "unresolved", ["no deterministic rights rule matched this record"])


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Return schema/shape errors; semantic rights errors are per-artifact decisions."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest must be an object"]
    if _text(manifest.get("schema_version")) != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("artifacts must be a list")
        return errors
    seen: set[str] = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            errors.append(f"artifacts[{index}] must be an object")
            continue
        artifact_id = _text(artifact.get("artifact_id"))
        if not artifact_id:
            errors.append(f"artifacts[{index}].artifact_id is required")
        elif artifact_id in seen:
            errors.append(f"duplicate artifact_id {artifact_id!r}")
        seen.add(artifact_id)
        if artifact.get("permitted_uses") is not None and not isinstance(artifact["permitted_uses"], list):
            errors.append(f"artifacts[{index}].permitted_uses must be a list")
        if artifact.get("evidence") is not None and not isinstance(artifact["evidence"], list):
            errors.append(f"artifacts[{index}].evidence must be a list")
    return errors


def evaluate_manifest(
    manifest: dict[str, Any],
    *,
    profile: dict[str, Any] | None = None,
    zotero_records: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate all manifest artifacts and return a promotion-ready summary."""
    errors = validate_manifest(manifest)
    profile = profile or SPRINGER_JCSHM_PROFILE
    records = zotero_records or {}
    results: list[dict[str, Any]] = []
    for original in manifest.get("artifacts", []) if isinstance(manifest, dict) else []:
        artifact = dict(original) if isinstance(original, dict) else original
        if isinstance(artifact, dict):
            key_candidates = [
                _text(artifact.get("zotero_item_key")),
                _text(artifact.get("source_reference_id")),
                _text(artifact.get("doi")),
            ]
            for key in key_candidates:
                if key and key in records:
                    artifact["zotero_record"] = normalize_zotero_record(records[key])
                    if not _source_links(artifact):
                        zotero = artifact["zotero_record"]
                        artifact["source_url"] = zotero.get("source_url") or zotero.get("doi_url")
                    break
        results.append(evaluate_artifact(artifact, profile))
    blocking = [result for result in results if not result.get("allowed")]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "allowed" if not errors and not blocking else "blocked",
        "submission_allowed": not errors and not blocking,
        "profile": profile.get("profile_id", "custom"),
        "manifest_errors": errors,
        "artifact_count": len(results),
        "allowed_count": sum(1 for result in results if result.get("allowed")),
        "blocking_count": len(blocking),
        "artifacts": results,
    }


class _SourcePageParser(HTMLParser):
    """Small, dependency-free extractor; results are signals, never approval."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: list[dict[str, str]] = []
        self.links: list[dict[str, str]] = []
        self._in_body = False
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "meta":
            self.meta.append(values)
        elif tag.casefold() == "link":
            self.links.append(values)
        elif tag.casefold() == "body":
            self._in_body = True

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "body":
            self._in_body = False

    def handle_data(self, data: str) -> None:
        if self._in_body:
            self._text_parts.append(data)

    @property
    def body_text(self) -> str:
        return re.sub(r"\s+", " ", html.unescape(" ".join(self._text_parts))).strip()


def inspect_source_urls(urls: list[str], *, timeout_seconds: float = 15.0) -> dict[str, Any]:
    """Fetch source pages and cacheable rights signals without auto-approving.

    This is intentionally an evidence collector.  A publisher page, DOI
    landing page, or Zotero record can say *what* the source is while the
    article's figure credit line can exclude an otherwise open-licensed work.
    A human must therefore promote collected signals into an evidence record.
    """
    pages: list[dict[str, Any]] = []
    for raw_url in urls:
        url = _text(raw_url)
        if not url:
            continue
        page: dict[str, Any] = {"url": url, "fetched_at": _now(), "status": "unresolved"}
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Meridian-docs rights audit/1.0"})
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read(2_000_000)
                content_type = response.headers.get("Content-Type", "")
                page.update({
                    "http_status": getattr(response, "status", None),
                    "content_type": content_type,
                    "content_sha256": hashlib.sha256(body).hexdigest(),
                })
            if "html" in content_type.casefold() or body.lstrip().startswith((b"<", b"<!")):
                parser = _SourcePageParser()
                parser.feed(body.decode("utf-8", errors="replace"))
                license_signals: list[str] = []
                for item in parser.meta + parser.links:
                    blob = " ".join(item.values())
                    if any(token in blob.casefold() for token in ("license", "rights", "creative commons", "cc by", "copyright")):
                        license_signals.append(blob[:500])
                text_blob = parser.body_text
                for token in ("CC BY 4.0", "CC BY-SA", "CC BY-ND", "CC BY-NC", "All rights reserved", "permission"):
                    if token.casefold() in text_blob.casefold() and token not in license_signals:
                        license_signals.append(token)
                page["metadata"] = parser.meta
                page["license_signals"] = license_signals
                page["doi"] = _first_doi([text_blob, json.dumps(parser.meta)])
                page["status"] = "signals_collected" if license_signals else "inconclusive"
            else:
                page["status"] = "inconclusive"
                page["warning"] = "non-HTML source fetched; inspect the document and credit lines manually"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            page["status"] = "unresolved"
            page["error"] = f"{type(exc).__name__}: {exc}"
        pages.append(page)
    return {
        "status": "signals_collected" if pages and all(page.get("status") == "signals_collected" for page in pages) else "inconclusive",
        "pages": pages,
        "note": "Collected web signals are not permission; record explicit evidence before an artifact can pass.",
    }


def _xml_text(element: ET.Element) -> str:
    return "".join(element.itertext()).strip()


def _paragraph_records(document_xml: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(document_xml)
    records: list[dict[str, Any]] = []
    for index, paragraph in enumerate(root.iter(_W_P)):
        text = "".join(
            node.text or ""
            for node in paragraph.iter()
            if node.tag in {_W_T, _W_INSTR, _W_DELT}
        ).strip()
        embeds = [
            node.get(_R_EMBED)
            for drawing in paragraph.iter(_W_DRAWING)
            for node in drawing.iter()
            if node.get(_R_EMBED)
        ]
        records.append({
            "index": index,
            "para_id": paragraph.get(f"{{{_W14}}}paraId") or f"word/document.xml#p{index}",
            "text": text,
            "embed_ids": embeds,
        })
    return records


def _relationship_targets(archive: zipfile.ZipFile) -> dict[str, str]:
    try:
        root = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
    except (KeyError, ET.ParseError):
        return {}
    result: dict[str, str] = {}
    for relation in root:
        if relation.get("Type", "").endswith("/image"):
            target = relation.get("Target", "").lstrip("/")
            if not target.startswith("word/"):
                target = f"word/{target}"
            result[relation.get("Id", "")] = target
    return result


def _docx_candidates(docx_path: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    with zipfile.ZipFile(docx_path) as archive:
        document_xml = archive.read("word/document.xml")
        records = _paragraph_records(document_xml)
        targets = _relationship_targets(archive)
        media_hashes: dict[str, str] = {}
        for relation_id, target in targets.items():
            try:
                media_hashes[relation_id] = hashlib.sha256(archive.read(target)).hexdigest()
            except KeyError:
                continue
    candidates: list[dict[str, Any]] = []
    for record in records:
        match = _CAPTION_RE.search(record["text"])
        if not match:
            continue
        kind = "figure" if match.group("kind").casefold().startswith(("fig", "figure")) else "table"
        label = match.group("label")
        cited = _expand_citations(record["text"])
        attached_ids = list(record["embed_ids"])
        nearby: list[dict[str, Any]] = []
        if kind == "figure" and not attached_ids:
            for other in records[max(0, record["index"] - 3) : record["index"] + 4]:
                if other is record or not other["embed_ids"]:
                    continue
                nearby.extend(other["embed_ids"])
            attached_ids = list(dict.fromkeys(nearby))
        candidates.append({
            "artifact_id": f"{kind}:{label}",
            "kind": kind,
            "label": label,
            "caption": record["text"],
            "caption_para_id": record["para_id"],
            "document_order": record["index"],
            "source_reference_ids": cited,
            "media_relationship_ids": attached_ids,
            "asset_sha256": [media_hashes[x] for x in attached_ids if x in media_hashes],
            "asset_binding_status": "bound" if attached_ids and all(x in media_hashes for x in attached_ids) else "unbound",
        })
    return candidates, media_hashes


def _expand_citations(text: str) -> list[str]:
    values: list[str] = []
    for match in _CITATION_RE.finditer(text):
        for part in re.split(r"\s*,\s*", match.group(1)):
            if "-" in part:
                start, end = (int(x) for x in re.split(r"\s*-\s*", part, maxsplit=1))
                values.extend(str(number) for number in range(start, end + 1))
            else:
                values.append(str(int(part)))
    return list(dict.fromkeys(values))


def _reference_index(docx_path: str) -> dict[str, dict[str, Any]]:
    with zipfile.ZipFile(docx_path) as archive:
        records = _paragraph_records(archive.read("word/document.xml"))
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        match = _BIB_RE.match(record["text"])
        if not match:
            continue
        reference_id = match.group("bracket") or match.group("decimal")
        text = match.group("text")
        doi = _doi_url(text)
        urls = _URL_RE.findall(text)
        result[reference_id] = {
            "reference_id": reference_id,
            "text": text,
            "doi": doi,
            "urls": list(dict.fromkeys(urls + ([doi] if doi else []))),
        }
    return result


def audit_docx_rights(
    docx_paths: str | list[str],
    manifest: dict[str, Any],
    *,
    profile: dict[str, Any] | None = None,
    zotero_records: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Audit every figure/table caption in one or more DOCX files.

    A manifest row must use the scanner's ``artifact_id`` (or an explicit
    ``document_id:artifact_id`` key for multiple documents).  A caption's own
    bracket citation is reported and linked to the in-document reference list;
    nearby prose is never silently promoted to source evidence.
    """
    paths = [docx_paths] if isinstance(docx_paths, str) else list(docx_paths)
    profile = profile or SPRINGER_JCSHM_PROFILE
    manifest_records = {
        _text(item.get("artifact_id")): item
        for item in manifest.get("artifacts", [])
        if isinstance(item, dict) and _text(item.get("artifact_id"))
    }
    shared_references: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            for number, reference in _reference_index(path).items():
                # Prefer the first complete reference, but allow a later
                # document in the package to fill a missing URL/text field.
                existing = shared_references.get(number)
                if existing is None or (not existing.get("urls") and reference.get("urls")):
                    shared_references[number] = reference
        except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
            continue
    rows: list[dict[str, Any]] = []
    for path in paths:
        document_id = _text(Path(path).stem)
        try:
            candidates, _ = _docx_candidates(path)
            references = shared_references
        except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
            rows.append({"document_id": document_id, "docx_path": path, "status": "unresolved", "error": str(exc)})
            continue
        for candidate in candidates:
            keys = [f"{document_id}:{candidate['artifact_id']}", candidate["artifact_id"]]
            artifact = next((manifest_records[key] for key in keys if key in manifest_records), None)
            if artifact is None:
                result = {
                    "artifact_id": candidate["artifact_id"],
                    "decision": "unresolved",
                    "allowed": False,
                    "reasons": ["caption has no rights manifest record"],
                }
            else:
                artifact = dict(artifact)
                candidate_hashes = set(candidate["asset_sha256"])
                manifest_hash = _text(artifact.get("asset_sha256"))
                hash_mismatch = bool(
                    manifest_hash
                    and candidate_hashes
                    and manifest_hash not in candidate_hashes
                )
                artifact.setdefault("asset_sha256", candidate["asset_sha256"][0] if len(candidate["asset_sha256"]) == 1 else "")
                if not artifact.get("source_reference_id") and len(candidate["source_reference_ids"]) == 1:
                    artifact["source_reference_id"] = candidate["source_reference_ids"][0]
                if not artifact.get("source_urls") and artifact.get("source_reference_id") in references:
                    artifact["source_urls"] = references[artifact["source_reference_id"]]["urls"]
                if hash_mismatch:
                    result = _base_result(
                        artifact,
                        "inconclusive",
                        ["manifest asset_sha256 does not match the embedded DOCX asset"],
                    )
                else:
                    result = evaluate_artifact(artifact, profile)
            rows.append({
                "document_id": document_id,
                "docx_path": path,
                **candidate,
                "references": [references[number] for number in candidate["source_reference_ids"] if number in references],
                "rights": result,
            })
    blocking = [row for row in rows if not row.get("rights", {}).get("allowed")]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "allowed" if rows and not blocking else "blocked",
        "submission_allowed": bool(rows) and not blocking,
        "profile": profile.get("profile_id", "custom"),
        "document_count": len(paths),
        "artifact_count": len(rows),
        "blocking_count": len(blocking),
        "artifacts": rows,
    }


def build_rights_manifest_template(
    docx_paths: str | list[str],
    *,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a review template from real DOCX captions and reference lists.

    This does not assert that any artifact is original or permitted.  It
    pre-populates only deterministic observations: caption identity, stable
    paragraph id, embedded asset hash, the caption's own citations, and any
    DOI/URL recovered from matching bibliography entries.  A user/reviewer
    fills the rights fields and then runs :func:`audit_docx_rights`.
    """
    paths = [docx_paths] if isinstance(docx_paths, str) else list(docx_paths)
    profile = profile or SPRINGER_JCSHM_PROFILE
    artifacts: list[dict[str, Any]] = []
    source_references: dict[str, dict[str, Any]] = {}
    shared_references: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            for number, reference in _reference_index(path).items():
                existing = shared_references.get(number)
                if existing is None or (not existing.get("urls") and reference.get("urls")):
                    shared_references[number] = reference
        except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
            continue
    for path in paths:
        document_id = _text(Path(path).stem)
        try:
            candidates, _ = _docx_candidates(path)
            references = shared_references
        except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
            return {"schema_version": SCHEMA_VERSION, "status": "error", "error": str(exc), "docx_path": path}
        for candidate in candidates:
            citation_ids = candidate["source_reference_ids"]
            for number in citation_ids:
                if number in references:
                    source_references.setdefault(number, references[number])
            one_reference = citation_ids[0] if len(citation_ids) == 1 else ""
            reference = references.get(one_reference, {})
            artifacts.append({
                "artifact_id": f"{document_id}:{candidate['artifact_id']}",
                "document_id": document_id,
                "kind": candidate["kind"],
                "label": candidate["label"],
                "caption_para_id": candidate["caption_para_id"],
                "caption": candidate["caption"],
                "asset_sha256": candidate["asset_sha256"][0] if len(candidate["asset_sha256"]) == 1 else "",
                "asset_binding_status": candidate["asset_binding_status"],
                "source_reference_candidates": citation_ids,
                "source_reference_id": one_reference,
                "source_reference_text": reference.get("text", ""),
                "source_urls": reference.get("urls", []),
                "source_kind": "unknown",
                "use_class": "reproduced" if citation_ids else "original_or_derived_review",
                "source_identity_status": "not_checked",
                "source_identity_notes": "",
                "release_action": "review",
                "zotero_item_key": "",
                "license_name": "",
                "license_url": "",
                "rights_holder": "",
                "permitted_uses": [],
                "credit_line": "",
                "evidence": [],
                "reviewed_at": "",
                "reviewer": "",
            })
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile.get("profile_id", "custom"),
        "generated_at": _now(),
        "status": "template_only",
        "submission_allowed": False,
        "source_references": source_references,
        "artifacts": artifacts,
        "note": "Template observations are not rights clearance; complete evidence fields and rerun the audit.",
    }


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Meridian-docs fail-closed rights clearance")
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit", help="audit DOCX figures/tables against a rights manifest")
    audit.add_argument("--manifest", required=True)
    audit.add_argument("docx", nargs="+")
    evaluate = sub.add_parser("evaluate", help="evaluate a rights manifest without a DOCX")
    evaluate.add_argument("manifest")
    args = parser.parse_args()
    if args.command == "evaluate":
        result = evaluate_manifest(_load_json(args.manifest))
    else:
        result = audit_docx_rights(args.docx, _load_json(args.manifest))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("submission_allowed") else 2


if __name__ == "__main__":  # pragma: no cover - exercised through the console command
    sys.exit(_cli())
