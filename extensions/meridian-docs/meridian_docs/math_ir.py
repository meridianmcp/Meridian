"""Small, deterministic intermediate representation for document math.

This is deliberately not a universal computer-algebra system.  It models the
constructs Meridian can safely inspect or generate and preserves an explicit
``opaque`` node for everything else.  Native OMML remains the authoritative
document representation; this module is an interchange/editing layer only.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


SUPPORTED_KINDS = frozenset(
    {
        "sequence",
        "text",
        "symbol",
        "number",
        "operator",
        "fraction",
        "superscript",
        "subscript",
        "subsup",
        "radical",
        "function",
        "delimiter",
        "nary",
        "matrix",
        "cases",
        "accent",
        "opaque",
    }
)

EQUATION_ARTIFACT_SCHEMA_VERSION = 1
EQUATION_PLACEMENTS = frozenset(
    {"inline", "display", "line_separated", "table_associated"}
)
PUNCTUATION_OWNERS = frozenset({"none", "math", "surrounding_prose"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class MathNode:
    """Immutable math node with deterministic serialization.

    ``attributes`` is a sorted tuple instead of a mutable mapping so a node
    can safely be cached, compared, and hashed by callers.  Attributes are
    presentation/interchange metadata, not inferred scientific meaning.
    """

    kind: str
    children: tuple["MathNode", ...] = ()
    text: str | None = None
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in SUPPORTED_KINDS:
            raise ValueError(f"unsupported MathNode kind: {self.kind!r}")
        children = tuple(self.children)
        if any(not isinstance(child, MathNode) for child in children):
            raise TypeError("MathNode children must be MathNode instances")
        object.__setattr__(self, "children", children)
        if self.text is not None and not isinstance(self.text, str):
            raise TypeError("MathNode text must be a string or None")
        raw_attributes = self.attributes.items() if isinstance(self.attributes, Mapping) else self.attributes
        keys: list[str] = []
        normalized_attributes: list[tuple[str, str]] = []
        for key, value in raw_attributes:
            if not isinstance(key, str) or not key:
                raise ValueError("MathNode attribute names must be non-empty strings")
            if not isinstance(value, str):
                raise TypeError("MathNode attribute values must be strings")
            keys.append(key)
            normalized_attributes.append((key, value))
        if len(keys) != len(set(keys)):
            raise ValueError("MathNode attributes must be unique")
        object.__setattr__(self, "attributes", tuple(sorted(normalized_attributes)))

    @property
    def attrs(self) -> dict[str, str]:
        """Return a defensive attribute mapping for ergonomic consumers."""

        return dict(self.attributes)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind}
        if self.text is not None:
            result["text"] = self.text
        if self.attributes:
            result["attributes"] = {key: value for key, value in self.attributes}
        if self.children:
            result["children"] = [child.to_dict() for child in self.children]
        return result

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def validate(self) -> list[str]:
        """Return machine-readable structural issues without raising."""

        issues: list[str] = []
        if self.kind == "opaque" and not self.attrs.get("reason"):
            issues.append("opaque node requires a reason attribute")
        if self.kind in {"fraction", "superscript", "subscript", "radical", "accent"} and not self.children:
            issues.append(f"{self.kind} requires at least one child")
        if self.kind == "subsup" and len(self.children) != 3:
            issues.append("subsup requires exactly three children")
        if self.kind == "fraction" and len(self.children) != 2:
            issues.append("fraction requires exactly two children")
        if self.kind in {"superscript", "subscript"} and len(self.children) != 2:
            issues.append(f"{self.kind} requires exactly two children")
        if self.kind == "radical" and len(self.children) not in {1, 2}:
            issues.append("radical requires one child, or a radicand and an index")
        if self.kind == "accent" and len(self.children) != 1:
            issues.append("accent requires exactly one child")
        if self.kind == "function" and not self.attrs.get("name"):
            issues.append("function requires a name attribute")
        if self.kind == "delimiter" and not self.attrs.get("open"):
            issues.append("delimiter requires an open attribute")
        if self.kind == "delimiter" and not self.attrs.get("close"):
            issues.append("delimiter requires a close attribute")
        if self.kind == "nary" and not self.attrs.get("operator"):
            issues.append("nary requires an operator attribute")
        for child in self.children:
            issues.extend(child.validate())
        return issues


def make_node(
    kind: str,
    *children: MathNode,
    text: str | None = None,
    **attributes: str,
) -> MathNode:
    """Construct a node while canonicalizing attribute order."""

    return MathNode(
        kind=kind,
        children=tuple(children),
        text=text,
        attributes=tuple(sorted(attributes.items())),
    )


def sequence(children: Iterable[MathNode]) -> MathNode:
    return make_node("sequence", *tuple(children))


def opaque(reason: str, *, source_format: str | None = None, raw_digest: str | None = None) -> MathNode:
    attrs = {"reason": reason}
    if source_format:
        attrs["source_format"] = source_format
    if raw_digest:
        attrs["raw_digest"] = raw_digest
    return make_node("opaque", **attrs)


def from_dict(value: dict[str, Any]) -> MathNode:
    """Parse the stable dictionary form and reject malformed trees."""

    if not isinstance(value, dict):
        raise ValueError("math node must be an object")
    unknown = set(value) - {"kind", "children", "text", "attributes"}
    if unknown:
        raise ValueError(f"unknown math node fields: {sorted(unknown)!r}")
    kind = value.get("kind")
    if not isinstance(kind, str):
        raise ValueError("math node kind must be a string")
    children_value = value.get("children", [])
    if not isinstance(children_value, list):
        raise ValueError("math node children must be a list")
    attrs_value = value.get("attributes", {})
    if not isinstance(attrs_value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in attrs_value.items()
    ):
        raise ValueError("math node attributes must be a string mapping")
    return make_node(
        kind,
        *(from_dict(child) for child in children_value),
        text=value.get("text"),
        **attrs_value,
    )


def normalize_math_tree(node: MathNode, *, _preserve_sequence: bool = False) -> MathNode:
    """Canonicalize only redundant singleton/nested sequence wrappers.

    The LaTeX and OMML readers legitimately introduce different container
    depths.  Removing those wrappers preserves operator/script/matrix meaning
    while giving cross-format artifacts one stable semantic identity.
    """

    preserve_children = node.kind in {"matrix", "cases"}
    children = [
        normalize_math_tree(child, _preserve_sequence=preserve_children)
        for child in node.children
    ]
    if node.kind == "sequence":
        flattened: list[MathNode] = []
        for child in children:
            if child.kind == "sequence":
                flattened.extend(child.children)
            else:
                flattened.append(child)
        if len(flattened) == 1 and not _preserve_sequence:
            return flattened[0]
        return make_node("sequence", *flattened, text=node.text, **node.attrs)
    return make_node(node.kind, *children, text=node.text, **node.attrs)


def _freeze_json(value: Any) -> Any:
    """Make JSON-shaped metadata immutable while retaining its shape."""

    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("metadata mapping keys must be strings")
        return ("__map__", tuple((key, _freeze_json(item)) for key, item in sorted(value.items())))
    if isinstance(value, (list, tuple)):
        return ("__list__", tuple(_freeze_json(item) for item in value))
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("metadata numbers must be finite")
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise TypeError("metadata must be JSON-compatible")
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, tuple):
        if len(value) == 2 and value[0] == "__map__":
            return {key: _thaw_json(item) for key, item in value[1]}
        if len(value) == 2 and value[0] == "__list__":
            return [_thaw_json(item) for item in value[1]]
    return value


@dataclass(frozen=True)
class EquationArtifact:
    """Versioned identity envelope for one cross-format equation artifact.

    ``MathNode`` remains the neutral semantic tree.  This envelope adds the
    provenance and placement facts that cannot safely be inferred from a
    serialized LaTeX or OMML string.  It is intentionally immutable and has no
    document-writing behavior; native OMML remains authoritative for Word.
    """

    equation_id: str
    document_id: str
    source_format: str
    source_hash: str
    semantic_tree: MathNode
    placement: str
    punctuation_ownership: str = "none"
    punctuation: str | None = None
    typography_roles: Mapping[str, Iterable[str]] = ()
    source_span: Mapping[str, Any] | None = None
    paragraph_anchor: str | None = None
    warnings: tuple[str, ...] = ()
    loss_flags: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    superseded_by: tuple[str, ...] = ()
    unknown_fields: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = EQUATION_ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in ("equation_id", "document_id", "source_format"):
            if not isinstance(getattr(self, field_name), str) or not getattr(self, field_name):
                raise ValueError(f"{field_name} must be a non-empty string")
        if not isinstance(self.source_hash, str) or not _SHA256_RE.fullmatch(self.source_hash):
            raise ValueError("source_hash must be a lowercase SHA-256 digest")
        if not isinstance(self.semantic_tree, MathNode):
            raise TypeError("semantic_tree must be a MathNode")
        object.__setattr__(self, "semantic_tree", normalize_math_tree(self.semantic_tree))
        if self.placement not in EQUATION_PLACEMENTS:
            raise ValueError(f"unsupported equation placement: {self.placement!r}")
        if self.punctuation_ownership not in PUNCTUATION_OWNERS:
            raise ValueError(f"unsupported punctuation ownership: {self.punctuation_ownership!r}")
        if self.punctuation is not None and not isinstance(self.punctuation, str):
            raise TypeError("punctuation must be a string or None")
        if self.schema_version != EQUATION_ARTIFACT_SCHEMA_VERSION:
            raise ValueError(f"unsupported EquationArtifact schema_version: {self.schema_version!r}")

        roles = self.typography_roles
        if isinstance(roles, Mapping):
            role_items = roles.items()
        else:
            role_items = roles or ()
        normalized_roles: list[tuple[str, tuple[str, ...]]] = []
        for token, role_values in role_items:
            if not isinstance(token, str) or not token:
                raise ValueError("typography role keys must be non-empty strings")
            if isinstance(role_values, str):
                role_values = (role_values,)
            values = tuple(sorted(set(str(item) for item in role_values)))
            normalized_roles.append((token, values))
        object.__setattr__(self, "typography_roles", tuple(sorted(normalized_roles)))
        if self.source_span is not None:
            if not isinstance(self.source_span, Mapping):
                raise TypeError("source_span must be a mapping")
            for key in ("source_file", "start_offset", "end_offset", "start_line", "end_line"):
                if key not in self.source_span:
                    raise ValueError(f"source_span requires {key}")
            if not isinstance(self.source_span["source_file"], str) or not self.source_span["source_file"]:
                raise ValueError("source_span.source_file must be a non-empty string")
            for key in ("start_offset", "end_offset", "start_line", "end_line"):
                if not isinstance(self.source_span[key], int) or self.source_span[key] < 0:
                    raise ValueError(f"source_span.{key} must be a non-negative integer")
            if self.source_span["end_offset"] < self.source_span["start_offset"]:
                raise ValueError("source_span.end_offset must not precede start_offset")
            if self.source_span["end_line"] < self.source_span["start_line"]:
                raise ValueError("source_span.end_line must not precede start_line")
        object.__setattr__(self, "source_span", _freeze_json(self.source_span) if self.source_span is not None else None)
        if self.unknown_fields is not None and not isinstance(self.unknown_fields, Mapping):
            raise TypeError("unknown_fields must be a mapping")
        known_fields = {
            "schema_version", "equation_id", "document_id", "source_format", "source_hash",
            "semantic_tree", "placement", "punctuation_ownership", "punctuation",
            "typography_roles", "source_span", "paragraph_anchor", "warnings", "loss_flags",
            "supersedes", "superseded_by",
        }
        unknown_fields = self.unknown_fields or {}
        if set(unknown_fields).intersection(known_fields):
            raise ValueError("unknown_fields cannot overwrite canonical fields")
        object.__setattr__(self, "unknown_fields", _freeze_json(unknown_fields))
        for field_name in ("warnings", "loss_flags", "supersedes", "superseded_by"):
            object.__setattr__(self, field_name, tuple(str(item) for item in getattr(self, field_name)))

    @property
    def artifact_id(self) -> str:
        """Return the stable logical identity, distinct from content digest."""

        return f"{len(self.document_id)}:{self.document_id}{len(self.equation_id)}:{self.equation_id}"

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "equation_id": self.equation_id,
            "document_id": self.document_id,
            "source_format": self.source_format,
            "source_hash": self.source_hash,
            "semantic_tree": self.semantic_tree.to_dict(),
            "placement": self.placement,
            "punctuation_ownership": self.punctuation_ownership,
            "punctuation": self.punctuation,
            "typography_roles": {
                token: list(roles) for token, roles in self.typography_roles
            },
            "source_span": _thaw_json(self.source_span) if self.source_span is not None else None,
            "paragraph_anchor": self.paragraph_anchor,
            "warnings": list(self.warnings),
            "loss_flags": list(self.loss_flags),
            "supersedes": list(self.supersedes),
            "superseded_by": list(self.superseded_by),
        }
        result.update(_thaw_json(self.unknown_fields))
        return result

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def validate(self) -> list[str]:
        """Return non-raising machine-readable artifact issues."""

        issues = list(self.semantic_tree.validate())
        if any(node.kind == "opaque" for node in _walk_nodes(self.semantic_tree)):
            issues.append("semantic tree contains opaque content")
        if self.punctuation_ownership == "none" and self.punctuation is not None:
            issues.append("punctuation cannot be supplied when ownership is none")
        if self.punctuation_ownership != "none" and not self.punctuation:
            issues.append("punctuation owner requires explicit punctuation")
        if self.loss_flags and not self.warnings:
            issues.append("loss_flags require at least one warning")
        return issues

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EquationArtifact":
        if not isinstance(value, Mapping):
            raise ValueError("EquationArtifact must be an object")
        known = {
            "schema_version", "equation_id", "document_id", "source_format", "source_hash",
            "semantic_tree", "placement", "punctuation_ownership", "punctuation",
            "typography_roles", "source_span", "paragraph_anchor", "warnings", "loss_flags",
            "supersedes", "superseded_by",
        }
        kwargs = {key: value[key] for key in known if key in value}
        kwargs["semantic_tree"] = from_dict(kwargs["semantic_tree"])
        kwargs["unknown_fields"] = {key: item for key, item in value.items() if key not in known}
        return cls(**kwargs)


def _walk_nodes(node: MathNode) -> Iterable[MathNode]:
    yield node
    for child in node.children:
        yield from _walk_nodes(child)


__all__ = [
    "EQUATION_ARTIFACT_SCHEMA_VERSION",
    "EQUATION_PLACEMENTS",
    "PUNCTUATION_OWNERS",
    "EquationArtifact",
    "MathNode",
    "SUPPORTED_KINDS",
    "from_dict",
    "make_node",
    "normalize_math_tree",
    "opaque",
    "sequence",
]
