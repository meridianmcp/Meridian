"""Figure spec schema for drafted documents (draft-figure-spec-schema).

A figure spec describes one figure a drafted document should contain: its
kind, where its content comes from, the caption and alt text, the cross-ref
label, and the width it takes on the page.

Validation is pure: it never touches the filesystem. The source rules are:

* ``plot``: the source must be a data path relative to the project outputs
  directory.
* ``diagram``: the source is either a data path (same rules as ``plot``) or
  inline diagram text. Inline diagram text is multi-line; a single-line source
  is always treated as a path.

A path source is rejected when it is absolute (leading ``/``, ``\\``, ``~`` or
a drive letter) or contains ``..`` anywhere. That check is deliberately
conservative: it rejects any traversal-shaped string.

``spec_hash`` is the SHA-256 of the canonical JSON encoding of the normalized
spec (sorted keys, no whitespace), with any ``spec_hash`` key excluded. Key
order does not change the hash, and an omitted width hashes the same as the
explicit default.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

#: Default figure width on the page, in inches (single column).
DEFAULT_WIDTH_IN: float = 6.5
#: Largest width a figure may take. A single column, so it matches the default.
#: Journal presets may override the default later; this cap stays put.
MAX_WIDTH_IN: float = 6.5

ALLOWED_KINDS: frozenset[str] = frozenset({"plot", "diagram"})
LABEL_PREFIX: str = "fig:"

_SPEC_HASH_KEY = "spec_hash"
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:|[\\/]|~)")


class FigureSpecError(ValueError):
    """Raised when a figure spec fails validation."""


def _require_str(spec: Mapping[str, Any], key: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FigureSpecError(f"{key} must be a non-empty string")
    return value


def _check_data_path(source: str) -> None:
    if _ABSOLUTE_RE.match(source):
        raise FigureSpecError(f"source must be relative to the outputs directory, not absolute: {source!r}")
    if ".." in source:
        raise FigureSpecError(f"source must not contain '..': {source!r}")


def validate_figure_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a figure spec and return a normalized copy.

    The copy carries ``width_in`` as a float, filled with ``DEFAULT_WIDTH_IN``
    when the caller leaves it out. The input mapping is not modified.

    Raises:
        FigureSpecError: when any rule fails.
    """
    if not isinstance(spec, Mapping):
        raise FigureSpecError("figure spec must be a mapping")

    kind = spec.get("kind")
    if kind not in ALLOWED_KINDS:
        raise FigureSpecError(f"kind must be one of {sorted(ALLOWED_KINDS)}, got {kind!r}")

    fig_id = _require_str(spec, "id")
    if not _ID_RE.match(fig_id):
        raise FigureSpecError(f"id must be kebab-case (lowercase letters, digits, single hyphens): {fig_id!r}")

    label = _require_str(spec, "label")
    if not label.startswith(LABEL_PREFIX) or not label[len(LABEL_PREFIX):].strip():
        raise FigureSpecError(f"label must start with {LABEL_PREFIX!r} followed by a name: {label!r}")

    _require_str(spec, "caption")
    _require_str(spec, "alt_text")

    source = _require_str(spec, "source")
    if "\n" in source:
        if kind != "diagram":
            raise FigureSpecError("only a diagram may use inline diagram text as its source")
    else:
        _check_data_path(source)

    width = spec.get("width_in", DEFAULT_WIDTH_IN)
    if isinstance(width, bool) or not isinstance(width, (int, float)):
        raise FigureSpecError(f"width_in must be a number, got {width!r}")
    if not 0 < width <= MAX_WIDTH_IN:
        raise FigureSpecError(f"width_in must be greater than 0 and at most {MAX_WIDTH_IN}, got {width!r}")

    normalized = dict(spec)
    normalized["width_in"] = float(width)
    return normalized


def figure_spec_hash(spec: Mapping[str, Any]) -> str:
    """Return the SHA-256 hex digest of the canonical JSON of a valid spec.

    Validates first, so the hash always covers the normalized spec. Any
    ``spec_hash`` key is excluded from the digest.

    Raises:
        FigureSpecError: when the spec fails validation or cannot be encoded as JSON.
    """
    normalized = validate_figure_spec(spec)
    normalized.pop(_SPEC_HASH_KEY, None)
    try:
        canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise FigureSpecError(f"figure spec is not JSON-encodable: {exc}") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
