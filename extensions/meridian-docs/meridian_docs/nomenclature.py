"""Deterministic, read-only nomenclature checks for native DOCX equations.

The linter intentionally requires a project-owned notation manifest. It can
report whether declared symbols occur in equation/prose text and whether an
alias or forbidden flattened spelling was used, but it never guesses a
scientific definition and never rewrites a document.
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections import Counter, defaultdict
from typing import Any
import xml.etree.ElementTree as ET

from . import docs_intel
from . import notation_rules


class NomenclatureManifestError(ValueError):
    """Raised when a notation manifest is ambiguous or malformed."""


def _manifest_digest(manifest: dict[str, Any]) -> str:
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _as_string_list(value: Any, field: str, index: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(entry, str) or not entry.strip() for entry in value
    ):
        raise NomenclatureManifestError(
            f"symbols[{index}].{field} must be a list of non-empty strings"
        )
    unique = {entry.strip() for entry in value}
    return sorted(unique, key=lambda entry: (entry.casefold(), entry))


def normalize_nomenclature_manifest(
    notation_manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate and canonicalize a notation manifest for stable hashing.

    Accepted shapes are either ``{"symbols": [{"symbol": "R", ...}]}`` or
    ``{"symbols": {"R": {"role": "...", ...}}}``. A symbol entry may
    declare ``aliases``, ``flattened_aliases``, ``role``, ``description``, and
    ``required``. The output is sorted by canonical symbol and contains no
    caller-controlled ordering.
    """
    if notation_manifest is None:
        return {"version": "1", "case_sensitive": True, "symbols": []}
    if not isinstance(notation_manifest, dict):
        raise NomenclatureManifestError("notation_manifest must be an object")

    version = notation_manifest.get("version", "1")
    if not isinstance(version, (str, int)) or not str(version).strip():
        raise NomenclatureManifestError("notation_manifest.version must be non-empty")
    case_sensitive = notation_manifest.get("case_sensitive", True)
    if not isinstance(case_sensitive, bool):
        raise NomenclatureManifestError(
            "notation_manifest.case_sensitive must be boolean"
        )

    raw_symbols = notation_manifest.get("symbols")
    if isinstance(raw_symbols, dict):
        entries = []
        for symbol, config in raw_symbols.items():
            if not isinstance(symbol, str) or not symbol.strip():
                raise NomenclatureManifestError("symbol names must be non-empty strings")
            if config is None:
                config = {}
            if not isinstance(config, dict):
                raise NomenclatureManifestError(f"symbols.{symbol} must be an object")
            configured_symbol = config.get("symbol")
            if configured_symbol is not None and (
                not isinstance(configured_symbol, str)
                or configured_symbol.strip() != symbol.strip()
            ):
                raise NomenclatureManifestError(
                    f"symbols.{symbol}.symbol conflicts with its dictionary key"
                )
            entries.append({"symbol": symbol, **config})
    elif isinstance(raw_symbols, list):
        entries = raw_symbols
    else:
        raise NomenclatureManifestError("notation_manifest.symbols must be a list or object")

    normalized: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for index, raw in enumerate(entries):
        if isinstance(raw, str):
            raw = {"symbol": raw}
        if not isinstance(raw, dict):
            raise NomenclatureManifestError(f"symbols[{index}] must be an object")
        symbol = raw.get("symbol", raw.get("name"))
        if not isinstance(symbol, str) or not symbol.strip():
            raise NomenclatureManifestError(
                f"symbols[{index}].symbol must be a non-empty string"
            )
        symbol = symbol.strip()
        key = symbol.casefold()
        if key in seen:
            raise NomenclatureManifestError(
                f"duplicate canonical symbol: {symbol!r} conflicts with {seen[key]!r}"
            )
        seen[key] = symbol

        role = raw.get("role", "")
        description = raw.get("description", "")
        required = raw.get("required", False)
        if not isinstance(role, str) or not isinstance(description, str):
            raise NomenclatureManifestError(
                f"symbols[{index}].role and description must be strings"
            )
        if not isinstance(required, bool):
            raise NomenclatureManifestError(f"symbols[{index}].required must be boolean")
        normalized.append(
            {
                "symbol": symbol,
                "aliases": _as_string_list(raw.get("aliases"), "aliases", index),
                "flattened_aliases": _as_string_list(
                    raw.get("flattened_aliases"), "flattened_aliases", index
                ),
                "role": role.strip(),
                "description": description.strip(),
                "required": required,
            }
        )

    normalized.sort(key=lambda entry: (entry["symbol"].casefold(), entry["symbol"]))
    return {
        "version": str(version).strip(),
        "case_sensitive": case_sensitive,
        "symbols": normalized,
    }


def _occurrence_count(text: str, spelling: str, *, case_sensitive: bool) -> int:
    """Count a spelling without matching it inside a larger identifier."""
    if not spelling:
        return 0
    escaped = re.escape(spelling)
    if re.fullmatch(r"[A-Za-z0-9_]+", spelling):
        pattern = rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
    else:
        pattern = escaped
    flags = 0 if case_sensitive else re.IGNORECASE
    return len(re.findall(pattern, text, flags))


def _locations(
    paragraphs: list[dict[str, Any]],
    equations: list[dict[str, Any]],
    spelling: str,
    *,
    case_sensitive: bool,
) -> list[dict[str, Any]]:
    locations: list[dict[str, Any]] = []
    for paragraph in paragraphs:
        if _occurrence_count(
            paragraph.get("text", ""), spelling, case_sensitive=case_sensitive
        ):
            locations.append(
                {
                    "kind": "paragraph",
                    "index": paragraph.get("index"),
                    "para_id": paragraph.get("para_id"),
                }
            )
    for equation in equations:
        if _occurrence_count(
            equation.get("flat_text", ""), spelling, case_sensitive=case_sensitive
        ):
            locations.append(
                {
                    "kind": "equation",
                    "ordinal": equation.get("ordinal"),
                    "anchor": equation.get("anchor"),
                    "number": equation.get("number"),
                }
            )
    return locations


def lint_nomenclature(
    source: str | bytes | bytearray,
    notation_manifest: dict[str, Any] | None,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Lint declared notation against a DOCX's prose and equation inventory.

    This is read-only and deterministic. ``notation_manifest`` is required
    for meaningful results; omitting it returns one explicit
    ``notation_manifest_missing`` finding instead of silently pretending that
    a generic token scan is authoritative.
    """
    try:
        manifest = normalize_nomenclature_manifest(notation_manifest)
        normalized_rules = notation_rules.normalize_notation_rules(rules)
    except NomenclatureManifestError as exc:
        return {"error": str(exc), "error_type": "manifest_invalid"}
    except notation_rules.NotationRulesError as exc:
        return {"error": str(exc), "error_type": "rules_invalid"}

    manifest_sha256 = _manifest_digest(manifest)
    if notation_manifest is None:
        findings = notation_rules.apply_notation_rules(
            [
                {
                    "type": "notation_manifest_missing",
                    "severity": "error",
                    "message": "a project notation manifest is required for nomenclature linting",
                }
            ],
            normalized_rules,
        )
        by_type = Counter(str(finding["type"]) for finding in findings)
        return {
            "document_path": source if isinstance(source, str) else None,
            "source_fingerprint": None,
            "manifest": manifest,
            "manifest_sha256": manifest_sha256,
            "notation_rules": normalized_rules,
            "equation_count": 0,
            "declared_symbol_count": 0,
            "used_symbol_count": 0,
            "findings": findings,
            "finding_count": len(findings),
            "findings_by_type": dict(sorted(by_type.items())),
            "valid": not findings,
        }

    audit = docs_intel.audit_equation_integrity(source)
    if audit.get("error"):
        return {
            "document_path": audit.get("document_path"),
            "source_fingerprint": audit.get("source_fingerprint"),
            "manifest": manifest,
            "manifest_sha256": manifest_sha256,
            "error": audit["error"],
            "error_type": "document_invalid",
        }
    try:
        paragraphs = docs_intel.parse_docx(source)
        parsed_equations = docs_intel.parse_docx_equations_local(source)
    except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        return {
            "document_path": audit.get("document_path"),
            "source_fingerprint": audit.get("source_fingerprint"),
            "manifest": manifest,
            "manifest_sha256": manifest_sha256,
            "error": str(exc),
            "error_type": "document_invalid",
        }

    equations = audit.get("records", [])
    prose = "\n".join(str(paragraph.get("text", "")) for paragraph in paragraphs)
    equation_text = "\n".join(
        str(equation.get("flat_text", "")) for equation in equations
    )
    searchable_text = f"{prose}\n{equation_text}"
    case_sensitive = manifest["case_sensitive"]

    findings: list[dict[str, Any]] = []
    for equation in parsed_equations:
        try:
            docs_intel._validate_omml_structure(equation["omml_raw"])
        except (ET.ParseError, ValueError) as exc:
            findings.append(
                {
                    "type": "omml_invalid",
                    "severity": "error",
                    "ordinal": equation.get("ordinal"),
                    "anchor": equation.get("para_id"),
                    "number": equation.get("number"),
                    "message": str(exc),
                }
            )
    used_symbols: set[str] = set()
    spelling_owners: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in manifest["symbols"]:
        canonical = entry["symbol"]
        spellings = [canonical, *entry["aliases"]]
        for spelling in spellings:
            spelling_owners[spelling.casefold()].append(entry)
        canonical_count = _occurrence_count(
            searchable_text, canonical, case_sensitive=case_sensitive
        )
        alias_hits = [
            (
                alias,
                _occurrence_count(searchable_text, alias, case_sensitive=case_sensitive),
            )
            for alias in entry["aliases"]
        ]
        exact_used = canonical_count > 0 or any(count > 0 for _, count in alias_hits)
        ci_used = exact_used
        if case_sensitive and not exact_used:
            ci_used = _occurrence_count(
                searchable_text, canonical, case_sensitive=False
            ) > 0
            ci_used = ci_used or any(
                _occurrence_count(searchable_text, alias, case_sensitive=False) > 0
                for alias in entry["aliases"]
            )
            if ci_used:
                findings.append(
                    {
                        "type": "case_mismatch",
                        "severity": "warning",
                        "symbol": canonical,
                        "locations": _locations(
                            paragraphs,
                            equations,
                            canonical,
                            case_sensitive=False,
                        ),
                    }
                )
        if exact_used or ci_used:
            used_symbols.add(canonical)
        elif entry["required"]:
            findings.append(
                {
                    "type": "missing_symbol",
                    "severity": "error",
                    "symbol": canonical,
                    "role": entry["role"],
                }
            )
        else:
            findings.append(
                {
                    "type": "declared_symbol_unused",
                    "severity": "warning",
                    "symbol": canonical,
                    "role": entry["role"],
                }
            )

        for alias, count in alias_hits:
            if count:
                findings.append(
                    {
                        "type": "alias_used",
                        "severity": "warning",
                        "symbol": canonical,
                        "alias": alias,
                        "count": count,
                        "locations": _locations(
                            paragraphs,
                            equations,
                            alias,
                            case_sensitive=case_sensitive,
                        ),
                    }
                )
        for flattened in entry["flattened_aliases"]:
            count = _occurrence_count(
                searchable_text, flattened, case_sensitive=case_sensitive
            )
            if count:
                findings.append(
                    {
                        "type": "flattened_subscript",
                        "severity": "error",
                        "symbol": canonical,
                        "flattened": flattened,
                        "count": count,
                        "locations": _locations(
                            paragraphs,
                            equations,
                            flattened,
                            case_sensitive=case_sensitive,
                        ),
                    }
                )

    for spelling, owners in sorted(spelling_owners.items()):
        unique_symbols = sorted({entry["symbol"] for entry in owners})
        if len(unique_symbols) > 1:
            findings.append(
                {
                    "type": "symbol_role_collision",
                    "severity": "error",
                    "spelling": spelling,
                    "symbols": unique_symbols,
                    "roles": sorted(
                        {entry["role"] for entry in owners if entry["role"]}
                    ),
                }
            )

    findings = notation_rules.apply_notation_rules(findings, normalized_rules)
    findings.sort(
        key=lambda finding: json.dumps(finding, sort_keys=True, separators=(",", ":"))
    )
    by_type = Counter(str(finding["type"]) for finding in findings)
    return {
        "document_path": audit.get("document_path"),
        "source_fingerprint": audit.get("source_fingerprint"),
        "manifest": manifest,
        "manifest_sha256": manifest_sha256,
        "notation_rules": normalized_rules,
        "equation_count": audit.get("equation_count", 0),
        "declared_symbol_count": len(manifest["symbols"]),
        "used_symbol_count": len(used_symbols),
        "used_symbols": sorted(
            used_symbols, key=lambda value: (value.casefold(), value)
        ),
        "findings": findings,
        "finding_count": len(findings),
        "findings_by_type": dict(sorted(by_type.items())),
        "valid": not findings,
    }
