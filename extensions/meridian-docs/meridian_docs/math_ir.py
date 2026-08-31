"""Small, deterministic intermediate representation for document math.

This is deliberately not a universal computer-algebra system.  It models the
constructs Meridian can safely inspect or generate and preserves an explicit
``opaque`` node for everything else.  Native OMML remains the authoritative
document representation; this module is an interchange/editing layer only.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable


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
        if any(not isinstance(child, MathNode) for child in self.children):
            raise TypeError("MathNode children must be MathNode instances")
        if self.text is not None and not isinstance(self.text, str):
            raise TypeError("MathNode text must be a string or None")
        keys: list[str] = []
        for key, value in self.attributes:
            if not isinstance(key, str) or not key:
                raise ValueError("MathNode attribute names must be non-empty strings")
            if not isinstance(value, str):
                raise TypeError("MathNode attribute values must be strings")
            keys.append(key)
        if keys != sorted(set(keys)):
            raise ValueError("MathNode attributes must be unique and sorted")

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


__all__ = [
    "MathNode",
    "SUPPORTED_KINDS",
    "from_dict",
    "make_node",
    "opaque",
    "sequence",
]
