"""Hardened XML parsing: reject DTDs/entities/external resolution, bound
depth/items/text (item 2ffd763d).

Threat model (see docs/meridian-storage-and-file-inspector-contract-2026-08-31.md
"Bounded file-shape inspector" -> "Common preflight limits"):

  "XML must reject DTDs, entities, external resources, network access, and
   unsafe XInclude/XSLT behavior."

Why lxml instead of ``defusedxml``
----------------------------------
``defusedxml`` is documented (and recommended in the design doc's "Research
basis") as the standard answer here, but it is only ever a *transitive*
dependency of ``fpdf2`` in this repo's root ``pixi.lock`` -- verified
empirically before choosing this path: a fresh ``pixi run python -c "import
defusedxml"`` in this project's own environment raises ``ModuleNotFoundError``.
It is not actually installed by the root pixi environment despite resolving
in the lock file (fpdf2's dependency on it is evidently not part of the
installed set). ``lxml``, by contrast, IS a direct, already-installed
dependency of both the root ``meridian`` pixi env and ``extensions/
meridian-docs`` (root ``pyproject.toml``/``pixi.toml``, and
``extensions/meridian-docs/meridian_docs/ooxml_integrity.py`` already imports
``lxml.etree``). Building this package's hardening directly on lxml -- with
an explicit, defense-in-depth secure parser configuration -- adds ZERO new
dependency surface to the project (this package's own declared
``lxml>=5.0`` is already satisfied by every environment that would run it)
and, unlike a defusedxml-based implementation, is importable and testable
directly from the repo's root pixi environment without touching
``pixi.toml``/``pixi.lock`` at all.

Defense layers (all independent -- any ONE of them alone would already stop
the classic XXE/billion-laughs attack classes; they are stacked deliberately
so no single implementation detail is load-bearing):

  1. **Raw byte/text prescan** for the literal (case-insensitive) substring
     ``<!doctype`` anywhere in the document before it is ever handed to a
     parser. This is intentionally broad/conservative (it does not attempt
     to distinguish a real top-level DOCTYPE declaration from, say, that
     literal string appearing inside a comment or CDATA section) -- a false
     positive here just means an unusual-but-harmless file is rejected as
     "denied", which is the safe direction to err in for a security control.
     Per the XML spec a DOCTYPE, if present, MUST appear before the root
     element, so this also cannot produce a false NEGATIVE for a real
     declaration.
  2. **Secure parser configuration** (defense in depth, in case layer 1 is
     ever bypassed by an encoding trick): ``resolve_entities=False`` (no
     internal OR external entity is ever expanded -- this alone defeats
     "billion laughs" since the entity reference is kept as a literal,
     unexpanded node rather than recursively substituted), ``no_network=True``
     (libxml2 may never fetch a URL to resolve an external entity/DTD),
     ``load_dtd=False`` (never even attempt to load a DTD, internal or
     external), ``dtd_validation=False``, and ``huge_tree=False`` (KEEPS
     libxml2's own built-in hardening limits on depth/text/entity-expansion
     active -- setting this True is itself a known way to accidentally
     disable protection, so it is pinned False explicitly here rather than
     left to the lxml default).
  3. **Post-parse structural check** of ``ElementTree.docinfo`` -- even
     with ``load_dtd=False``, libxml2 still records whether a DOCTYPE
     declaration was present in the source (``internalDTD``/``externalDTD``
     are non-``None``); this is checked and rejected even if layer 1 somehow
     missed it.
  4. **This module never calls XInclude or XSLT processing** at all -- there
     is no code path here that would substitute/transform content, so those
     attack classes are structurally absent rather than merely disabled.

None of this substitutes for the caller-side bounds (max_bytes/max_depth/
max_items/preview_chars/timeout_seconds) enforced by
:mod:`meridian_file_inspection.inspector` -- this module only hardens the
*parser*; the *inspector* enforces the *resource* bounds around it.
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from typing import Any

import lxml.etree as LET

#: Case-insensitive marker checked for anywhere in the raw source. See the
#: module docstring, layer 1, for why a broad substring scan (rather than a
#: structural pre-parse) is the deliberate choice here.
_DOCTYPE_MARKER = b"<!doctype"


class XmlSecurityError(Exception):
    """Raised when a document is rejected on security grounds (DTD/entity/
    external-resolution). Always carries a stable ``code``/``reason`` pair
    matching the inspector's error-code contract."""

    def __init__(self, code: str, reason: str, detail: str | None = None) -> None:
        self.code = code
        self.reason = reason
        self.detail = detail
        super().__init__(f"{code}:{reason}" + (f" ({detail})" if detail else ""))


class XmlLimitExceeded(Exception):
    """Raised internally to unwind an in-progress bounded walk early. Always
    caught by :func:`parse_secure` and turned into a partial result -- never
    escapes this module."""


def _looks_like_doctype(data: bytes) -> bool:
    """Broad, case-insensitive substring scan for a DOCTYPE declaration
    marker anywhere in ``data``. See module docstring layer 1."""
    return _DOCTYPE_MARKER in data.lower()


def build_secure_parser(*, huge_tree: bool = False) -> "LET.XMLParser":
    """Construct an lxml ``XMLParser`` with every entity/DTD/network
    protection explicitly pinned (module docstring, layer 2). Never resolves
    entities, never loads a DTD, never touches the network, and never
    disables libxml2's own built-in hardening limits (``huge_tree=False``).
    """
    return LET.XMLParser(
        resolve_entities=False,
        no_network=True,
        dtd_validation=False,
        load_dtd=False,
        huge_tree=huge_tree,
        remove_blank_text=False,
        recover=False,
        collect_ids=False,
    )


def _check_docinfo(tree: "LET._ElementTree") -> None:
    """Layer 3: reject if libxml2 recorded a DOCTYPE even though we never
    loaded it. Defense in depth against layer 1 ever being bypassed."""
    docinfo = tree.docinfo
    if docinfo is not None and (
        docinfo.internalDTD is not None or docinfo.externalDTD is not None
    ):
        raise XmlSecurityError("denied", "dtd_disallowed", "docinfo reported a DTD")


@dataclass
class XmlShape:
    root_tag: str | None = None
    element_count: int = 0
    max_depth_reached: int = 0
    element_tag_counts: dict[str, int] = field(default_factory=dict)
    attribute_name_counts: dict[str, int] = field(default_factory=dict)
    namespace_uris: list[str] = field(default_factory=list)
    text_preview: str = ""
    truncated_tags: bool = False
    truncated_attrs: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_tag": self.root_tag,
            "element_count": self.element_count,
            "max_depth_reached": self.max_depth_reached,
            "element_tag_counts": dict(sorted(self.element_tag_counts.items())),
            "attribute_name_counts": dict(sorted(self.attribute_name_counts.items())),
            "namespace_uris": sorted(self.namespace_uris),
            "text_preview": self.text_preview,
            "truncated_tags": self.truncated_tags,
            "truncated_attrs": self.truncated_attrs,
        }


#: Cap on distinct tag/attribute names tracked in the summary -- bounds
#: memory for an adversarial file with huge tag-name cardinality, independent
#: of max_items (which bounds total element COUNT, not distinct NAME count).
_MAX_DISTINCT_NAMES = 500


def parse_secure(
    data: bytes,
    *,
    max_depth: int,
    max_items: int,
    preview_chars: int,
    timeout_seconds: float,
) -> tuple[XmlShape, bool, list[dict[str, Any]]]:
    """Parse ``data`` as XML under full hardening + resource bounds.

    Returns ``(shape, partial, warnings)``. Raises :class:`XmlSecurityError`
    for a DTD/entity finding (layer 1 or 3) or a malformed-XML
    :class:`lxml.etree.XMLSyntaxError` for genuinely invalid input -- both
    are caught by the inspector and mapped to the stable envelope error
    codes; this function never returns a "complete" result for either case.
    """
    if _looks_like_doctype(data):
        raise XmlSecurityError("denied", "dtd_disallowed", "DOCTYPE marker found in source")

    warnings: list[dict[str, Any]] = []
    partial = False
    shape = XmlShape()
    depth = 0
    start = time.monotonic()
    deadline_checks = 0

    # lxml's iterparse builds its own internal parser from these kwargs
    # directly -- it does NOT accept a pre-built XMLParser via `parser=`
    # (that's a plain fromstring()/parse() thing). Mirrors
    # build_secure_parser()'s settings exactly so the streaming walk gets
    # the identical hardening.
    context = LET.iterparse(
        io.BytesIO(data),
        events=("start", "end"),
        resolve_entities=False,
        no_network=True,
        dtd_validation=False,
        load_dtd=False,
        huge_tree=False,
        remove_blank_text=False,
        recover=False,
        collect_ids=False,
    )

    try:
        for event, elem in context:
            if event == "start":
                depth += 1
                shape.max_depth_reached = max(shape.max_depth_reached, depth)
                shape.element_count += 1

                if shape.root_tag is None:
                    shape.root_tag = _local_name(elem.tag)

                tag = _local_name(elem.tag)
                if len(shape.element_tag_counts) < _MAX_DISTINCT_NAMES or tag in shape.element_tag_counts:
                    shape.element_tag_counts[tag] = shape.element_tag_counts.get(tag, 0) + 1
                else:
                    shape.truncated_tags = True

                for attr_name in elem.attrib.keys():
                    aname = _local_name(attr_name)
                    if len(shape.attribute_name_counts) < _MAX_DISTINCT_NAMES or aname in shape.attribute_name_counts:
                        shape.attribute_name_counts[aname] = shape.attribute_name_counts.get(aname, 0) + 1
                    else:
                        shape.truncated_attrs = True

                for prefix, uri in (elem.nsmap or {}).items():
                    if uri not in shape.namespace_uris:
                        shape.namespace_uris.append(uri)

                if len(shape.text_preview) < preview_chars and elem.text:
                    remaining = preview_chars - len(shape.text_preview)
                    shape.text_preview += elem.text.strip()[:remaining]

                if depth > max_depth:
                    warnings.append({
                        "code": "limit_exceeded",
                        "reason": "max_depth_exceeded",
                        "detail": f"depth {depth} > max_depth {max_depth}",
                    })
                    partial = True
                    raise XmlLimitExceeded()

                if shape.element_count > max_items:
                    warnings.append({
                        "code": "limit_exceeded",
                        "reason": "max_items_exceeded",
                        "detail": f"element_count {shape.element_count} > max_items {max_items}",
                    })
                    partial = True
                    raise XmlLimitExceeded()

            elif event == "end":
                depth -= 1
                # Bound memory on a huge-but-shallow document: drop the
                # subtree's children once we're done with it. Never touches
                # ancestors we still need for depth bookkeeping above.
                elem.clear(keep_tail=False)
                while elem.getprevious() is not None:
                    del elem.getparent()[0]

            deadline_checks += 1
            if deadline_checks % 500 == 0 and (time.monotonic() - start) > timeout_seconds:
                warnings.append({
                    "code": "timeout",
                    "reason": "wall_clock_budget_exceeded",
                    "detail": f"exceeded {timeout_seconds}s",
                })
                partial = True
                raise XmlLimitExceeded()
    except XmlLimitExceeded:
        pass
    finally:
        root = context.root if hasattr(context, "root") else None
        del context

    # Layer 3: authoritative post-parse DTD check, even on a partial walk.
    try:
        tree = root.getroottree() if root is not None else None
    except Exception:
        tree = None
    if tree is not None:
        _check_docinfo(tree)

    return shape, partial, warnings


def _local_name(tag: Any) -> str:
    """Strip a Clark-notation namespace prefix (``{uri}local``) for display."""
    if not isinstance(tag, str):
        return str(tag)
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def parser_version() -> str:
    return f"lxml-{LET.LXML_VERSION[0]}.{LET.LXML_VERSION[1]}.{LET.LXML_VERSION[2]}/libxml2-{'.'.join(str(v) for v in _libxml2_version())}"


def _libxml2_version() -> tuple[int, int, int]:
    raw = LET.LIBXML_VERSION
    if isinstance(raw, tuple) and len(raw) == 3:
        return raw  # type: ignore[return-value]
    return (0, 0, 0)
