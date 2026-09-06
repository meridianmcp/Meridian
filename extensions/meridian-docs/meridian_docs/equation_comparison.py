"""Read-only cross-format equation comparison.

The comparison is deliberately a gate, not a converter.  It parses Word's
native OMML and a LaTeX candidate through the existing loss-aware bridge,
compares the neutral semantic trees, and reports placement, punctuation,
typography, and loss findings before any caller can consider mutation.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Mapping

from .latex_bridge import latex_to_ir, omml_to_ir
from .math_ir import MathNode, from_dict, normalize_math_tree


_PUNCTUATION = frozenset(",.;:")
_PUNCTUATION_OWNERS = frozenset({"none", "math", "surrounding_prose"})
_OMML_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _latex_placement(source: str) -> str:
    stripped = source.strip()
    if stripped.startswith("\\[") and stripped.endswith("\\]"):
        return "display"
    if stripped.startswith("$$") and stripped.endswith("$$"):
        return "display"
    if re.search(r"\\begin\{(?:equation|displaymath|gather|align)\*?\}", stripped):
        return "display"
    return "inline"


def _omml_placement(source: str) -> str:
    try:
        return "display" if _local(ET.fromstring(source).tag) == "oMathPara" else "inline"
    except ET.ParseError:
        return "unknown"


def _trailing_latex_punctuation(source: str) -> str | None:
    match = re.search(r"([,.;:])\s*(?:\\\]|\$\$)?\s*$", source)
    return match.group(1) if match and match.group(1) in _PUNCTUATION else None


def _node_roles(node: MathNode, path: str = "") -> dict[str, tuple[str, ...]]:
    roles: dict[str, tuple[str, ...]] = {}
    attrs = node.attrs
    explicit: list[str] = []
    for key in ("style", "role"):
        if attrs.get(key):
            explicit.append(attrs[key])
    if explicit:
        roles[path or "/"] = tuple(sorted(set(explicit)))
    for index, child in enumerate(node.children):
        roles.update(_node_roles(child, f"{path}/{index}"))
    return roles


def _omml_roles(source: str) -> dict[str, tuple[str, ...]]:
    """Extract explicit OMML run-property roles without changing the IR."""

    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return {}
    roles: dict[str, tuple[str, ...]] = {}
    for index, run in enumerate(element for element in root.iter() if _local(element.tag) == "r"):
        properties = next((child for child in run if _local(child.tag) == "rPr"), None)
        if properties is None:
            continue
        values: list[str] = []
        for child in properties:
            name = _local(child.tag)
            if name in {"sty", "scr"}:
                value = child.attrib.get(f"{{{_OMML_NS}}}val") or child.attrib.get("val")
                if value:
                    values.append(f"{name}:{value}")
            elif name in {"b", "i"}:
                values.append(name)
        if values:
            roles[f"run:{index}"] = tuple(sorted(set(values)))
    return roles


def _role_signature(roles: Mapping[str, tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
    """Compare explicit roles by occurrence order across format-specific paths."""

    return tuple(
        roles[key]
        for key in sorted(
            roles,
            key=lambda item: (
                int(item.split(":", 1)[1]) if item.startswith("run:") and item.split(":", 1)[1].isdigit() else item
            ),
        )
    )


def _valid_punctuation(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value in _PUNCTUATION)


@dataclass(frozen=True)
class EquationComparisonResult:
    """Machine-readable pre-mutation comparison result."""

    semantic_match: bool | None
    typography_match: bool | None
    placement_match: bool | None
    punctuation_match: bool | None
    punctuation_ownership_match: bool | None
    automatic_insertion_allowed: bool
    repair_required: bool
    findings: tuple[dict[str, Any], ...] = ()
    word_ir_sha256: str | None = None
    latex_ir_sha256: str | None = None

    @property
    def blocked(self) -> bool:
        return not self.automatic_insertion_allowed

    def as_dict(self) -> dict[str, Any]:
        return {
            "semantic_match": self.semantic_match,
            "typography_match": self.typography_match,
            "placement_match": self.placement_match,
            "punctuation_match": self.punctuation_match,
            "punctuation_ownership_match": self.punctuation_ownership_match,
            "automatic_insertion_allowed": self.automatic_insertion_allowed,
            "repair_required": self.repair_required,
            "blocked": self.blocked,
            "findings": [dict(item) for item in self.findings],
            "word_ir_sha256": self.word_ir_sha256,
            "latex_ir_sha256": self.latex_ir_sha256,
        }


def compare_equation_artifacts(
    word_omml: str,
    latex_source: str,
    profile: Mapping[str, Any] | None = None,
) -> EquationComparisonResult:
    """Compare OMML and LaTeX without writing or repairing either source.

    ``profile`` may provide explicit ``word_placement``, ``latex_placement``,
    ``word_punctuation``, ``latex_punctuation``, and
    ``allow_typography_repair`` values.  Missing values are inferred only when
    the source format makes the inference unambiguous.
    """

    findings: list[dict[str, Any]] = []
    word = omml_to_ir(word_omml)
    latex = latex_to_ir(latex_source)
    profile = dict(profile or {})
    if not word.success:
        findings.append({"kind": "word_parse", "severity": "error", "reasons": list(word.warnings)})
    if not latex.success:
        findings.append(
            {
                "kind": "latex_parse",
                "severity": "error",
                "reasons": list(latex.warnings),
            }
        )
    if word.unsupported or latex.unsupported:
        findings.append(
            {
                "kind": "unsupported_construct",
                "severity": "error",
                "word": list(word.unsupported),
                "latex": list(latex.unsupported),
            }
        )
    if word.lossy or latex.lossy:
        findings.append(
            {
                "kind": "lossy_conversion",
                "severity": "error",
                "word_warnings": list(word.warnings),
                "latex_warnings": list(latex.warnings),
                "word_unsupported": list(word.unsupported),
                "latex_unsupported": list(latex.unsupported),
            }
        )

    word_node = from_dict(word.value) if word.value and isinstance(word.value, dict) else None
    latex_node = from_dict(latex.value) if latex.value and isinstance(latex.value, dict) else None
    semantic_match: bool | None = None
    if word_node is not None and latex_node is not None:
        normalized_word = normalize_math_tree(word_node)
        normalized_latex = normalize_math_tree(latex_node)
        semantic_match = normalized_word.canonical_json() == normalized_latex.canonical_json()
        if not semantic_match:
            findings.append(
                {
                    "kind": "semantic_mismatch",
                    "severity": "error",
                    "word_ir_sha256": normalized_word.digest(),
                    "latex_ir_sha256": normalized_latex.digest(),
                }
            )

    latex_roles = _node_roles(latex_node) if latex_node else {}
    word_roles = _omml_roles(word_omml)
    typography_match: bool | None = True
    if latex_roles and not word_roles:
        typography_match = None
        findings.append(
            {
                "kind": "unresolved_typography",
                "severity": "error",
                "reason": "LaTeX has explicit style roles but OMML has no inspectable m:rPr roles",
            }
        )
    elif word_roles and not latex_roles:
        typography_match = None
        findings.append(
            {
                "kind": "unresolved_typography",
                "severity": "error",
                "reason": "OMML has explicit m:rPr roles but LaTeX has no matching explicit style roles",
            }
        )
    elif latex_roles and word_roles:
        typography_match = _role_signature(latex_roles) == _role_signature(word_roles)
        if not typography_match:
            findings.append(
                {
                    "kind": "typography_mismatch",
                    "severity": "warning",
                    "word_roles": word_roles,
                    "latex_roles": latex_roles,
                }
            )

    style_profile = profile.get("style")
    if (
        isinstance(style_profile, Mapping)
        and (style_profile.get("require_explicit_operator_roles") or style_profile.get("require_explicit_script_roles"))
        and not (latex_roles or word_roles)
    ):
        typography_match = None
        findings.append(
            {
                "kind": "required_typography_roles_missing",
                "severity": "error",
                "reason": "profile requires explicit operator or script typography roles",
            }
        )

    word_placement = profile.get("word_placement") or _omml_placement(word_omml)
    latex_placement = profile.get("latex_placement") or _latex_placement(latex_source)
    placement_match: bool | None = (
        None if "unknown" in {word_placement, latex_placement} else word_placement == latex_placement
    )
    if placement_match is False:
        findings.append(
            {
                "kind": "placement_mismatch",
                "severity": "error",
                "word": word_placement,
                "latex": latex_placement,
            }
        )
    elif placement_match is None:
        findings.append({"kind": "unresolved_placement", "severity": "error"})

    word_punctuation_supplied = "word_punctuation" in profile
    word_punctuation = profile.get("word_punctuation")
    latex_punctuation = profile.get("latex_punctuation", _trailing_latex_punctuation(latex_source))
    punctuation_match: bool | None = None
    if not _valid_punctuation(word_punctuation) or not _valid_punctuation(latex_punctuation):
        findings.append(
            {
                "kind": "invalid_punctuation",
                "severity": "error",
                "word": word_punctuation,
                "latex": latex_punctuation,
            }
        )
    elif word_punctuation_supplied:
        punctuation_match = word_punctuation == latex_punctuation
        if not punctuation_match:
            findings.append(
                {
                    "kind": "punctuation_mismatch",
                    "severity": "error",
                    "word": word_punctuation,
                    "latex": latex_punctuation,
                }
            )

    word_owner = profile.get("word_punctuation_ownership")
    latex_owner = profile.get(
        "latex_punctuation_ownership",
        "none" if latex_punctuation is None else None,
    )
    if word_punctuation_supplied and word_owner is None and word_punctuation is None:
        word_owner = "none"
    ownership_match: bool | None = None
    if word_owner in _PUNCTUATION_OWNERS and latex_owner in _PUNCTUATION_OWNERS:
        ownership_match = word_owner == latex_owner
        if not ownership_match:
            findings.append(
                {
                    "kind": "punctuation_ownership_mismatch",
                    "severity": "error",
                    "word": word_owner,
                    "latex": latex_owner,
                }
            )
    elif word_punctuation_supplied and (word_owner is not None or latex_owner is not None):
        findings.append({"kind": "invalid_punctuation_ownership", "severity": "error"})
    elif word_punctuation_supplied:
        findings.append({"kind": "unresolved_punctuation_ownership", "severity": "error"})

    repair_required = any(item["kind"] == "typography_mismatch" for item in findings)
    hard_block = any(item["severity"] == "error" for item in findings)
    repair_receipt = profile.get("typography_repair_receipt")
    if repair_required and not (isinstance(repair_receipt, Mapping) and repair_receipt.get("status") == "pass"):
        hard_block = True
    allowed = (
        not hard_block
        and semantic_match is True
        and placement_match is True
        and punctuation_match is True
        and ownership_match is True
        and typography_match is True
    )
    return EquationComparisonResult(
        semantic_match=semantic_match,
        typography_match=typography_match,
        placement_match=placement_match,
        punctuation_match=punctuation_match,
        punctuation_ownership_match=ownership_match,
        automatic_insertion_allowed=allowed,
        repair_required=repair_required,
        findings=tuple(findings),
        word_ir_sha256=normalized_word.digest() if word_node is not None else word.ir_sha256,
        latex_ir_sha256=normalized_latex.digest() if latex_node is not None else latex.ir_sha256,
    )


__all__ = ["EquationComparisonResult", "compare_equation_artifacts"]
