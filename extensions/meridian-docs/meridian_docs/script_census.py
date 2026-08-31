"""Read-only census of native OMML scripts.

This is intentionally separate from semantic notation binding. It answers the
forensic question "what scripted math is actually in this DOCX?" before any
role manifest or typography policy is applied.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from typing import Any

from .notation_audit import (
    _document_equations,
    _omml_term_records,
    _read_docx,
)


def census_equation_scripts(
    document_path: str,
    *,
    max_unique_terms: int = 50000,
) -> dict[str, Any]:
    """Inventory every native subscript/superscript term without editing DOCX."""

    if not isinstance(max_unique_terms, int) or isinstance(max_unique_terms, bool) or max_unique_terms < 1:
        return {"error": "max_unique_terms must be a positive integer", "error_type": "options_invalid"}
    try:
        source_bytes, root, resolved_path = _read_docx(document_path)
        equations = _document_equations(root)
    except Exception as exc:  # keep the read-only audit fail-closed for bad packages
        return {"document_path": document_path, "error": str(exc), "error_type": "document_invalid"}

    grouped: defaultdict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "count": 0,
            "base": "",
            "subscript": None,
            "superscript": None,
            "structures": Counter(),
            "locators": [],
        }
    )
    occurrence_count = 0
    for equation in equations:
        for term in _omml_term_records(equation.get("omml_raw", "")):
            if not (term.get("subscript") or term.get("superscript")):
                continue
            occurrence_count += 1
            key = term["term"]
            entry = grouped[key]
            entry["count"] += 1
            entry["base"] = term.get("base") or ""
            entry["subscript"] = term.get("subscript")
            entry["superscript"] = term.get("superscript")
            entry["structures"][term.get("structure") or "plain"] += 1
            locator = {
                "equation_number": equation.get("number"),
                "equation_ordinal": equation.get("ordinal"),
                "para_id": equation.get("para_id"),
                "heading": equation.get("heading"),
            }
            if locator not in entry["locators"]:
                entry["locators"].append(locator)
            if len(grouped) > max_unique_terms:
                return {
                    "document_path": resolved_path,
                    "error": "unique scripted-term limit exceeded",
                    "error_type": "limit_exceeded",
                }

    terms = []
    for term, entry in sorted(grouped.items(), key=lambda pair: (pair[0].casefold(), pair[0])):
        terms.append(
            {
                "term": term,
                "base": entry["base"],
                "subscript": entry["subscript"],
                "superscript": entry["superscript"],
                "occurrence_count": entry["count"],
                "structures": dict(sorted(entry["structures"].items())),
                "locators": entry["locators"],
            }
        )
    subscript_values = Counter(
        item["subscript"] for item in terms if item["subscript"] is not None
    )
    superscript_values = Counter(
        item["superscript"] for item in terms if item["superscript"] is not None
    )
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    result = {
        "schema_version": "1",
        "document_path": resolved_path,
        "source_fingerprint": source_sha256,
        "equation_count": len(equations),
        "scripted_occurrence_count": occurrence_count,
        "unique_scripted_term_count": len(terms),
        "unique_subscript_count": len(subscript_values),
        "unique_superscript_count": len(superscript_values),
        "subscript_values": dict(sorted(subscript_values.items(), key=lambda item: (item[0].casefold(), item[0]))),
        "superscript_values": dict(sorted(superscript_values.items(), key=lambda item: (item[0].casefold(), item[0]))),
        "terms": terms,
        "provenance": {
            "generator": "meridian-docs.script_census",
            "source_representation": "native_omml",
            "deterministic": True,
            "document_mutated": False,
        },
    }
    result["result_sha256"] = hashlib.sha256(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return result


__all__ = ["census_equation_scripts"]
