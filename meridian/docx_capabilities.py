"""Shared DOCX capability manifest for the meridian-docs (docs) slot.

One declarative description of the DOCX-touching docs-slot operations: the
canonical operation name, aliases, read/write class, anchor model, lock scope,
fallback policy, sidecar effects, and whether the tunnel-layer DOCX
region-claim guard enforces the operation today.

This module is pure: no FastAPI, no database, no filesystem access, so the docs
extension and the tests can import it without the server stack.

Enforcement is NOT moved here. ``meridian/routes/tunnel.py`` still owns
``_DOCS_WRITE_TOOLS`` and ``_docs_write_target``. This manifest records the same
facts, and ``tests/test_sprint_c8eabc3d_docx_capabilities.py`` pins parity with
them. Server-side DOCX writers that the guard does not list are reported with
``gated_by_tunnel_guard: False`` (see ``UNGATED_DOCX_WRITERS``) instead of being
added to the gate, because adding them would change tunnel behaviour.

Deliberately not populated yet: ``parts`` (OOXML parts touched) and
``verification`` (the post-write gate per operation). No per-tool declaration of
either exists in code today, so filling them in here would invent semantics.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

DOCX_CAPABILITY_SCHEMA = "docx-capabilities/1"

RW_DOCX_WRITE = "docx_write"
RW_SIDECAR_WRITE = "sidecar_write"
RW_CLASSES = frozenset({RW_DOCX_WRITE, RW_SIDECAR_WRITE})

ANCHOR_ELEMENT = "element_id"
ANCHOR_WHOLE_DOCUMENT = "whole_document"
ANCHOR_NONE = "none"

LOCK_SCOPED_ELEMENT = "scoped_element_claim"
LOCK_WHOLE_DOCUMENT = "whole_document_claim"
LOCK_NONE = "none"

FALLBACK_WHOLE_DOCUMENT_WHEN_ANCHOR_ABSENT = "whole_document_when_anchor_absent"
FALLBACK_WHOLE_DOCUMENT = "whole_document"
FALLBACK_NOT_APPLICABLE = "not_applicable"

SIDECAR_SQLITE_INDEX = "sqlite_index_cache"


@dataclass(frozen=True)
class DocxOperation:
    """One docs-slot operation as the shared DOCX manifest describes it."""

    operation: str
    rw_class: str
    path_arg: str
    anchor_arg: str | None = None
    aliases: tuple[str, ...] = ()
    sidecar_effects: tuple[str, ...] = ()
    gated_by_tunnel_guard: bool = False

    def __post_init__(self) -> None:
        op = self.operation
        if not op or "__" in op:
            raise ValueError(f"invalid DOCX operation name {op!r}")
        if self.rw_class not in RW_CLASSES:
            raise ValueError(f"{op}: unknown rw_class {self.rw_class!r}")
        if not self.path_arg:
            raise ValueError(f"{op}: path_arg is required")
        if self.anchor_arg is not None and self.anchor_arg == self.path_arg:
            raise ValueError(f"{op}: anchor_arg must differ from path_arg")
        if self.rw_class == RW_DOCX_WRITE:
            if self.sidecar_effects:
                raise ValueError(f"{op}: docx_write must not declare sidecar_effects")
        else:
            if self.gated_by_tunnel_guard:
                raise ValueError(f"{op}: sidecar_write cannot be gated")
            if self.anchor_arg is not None:
                raise ValueError(f"{op}: sidecar_write has no anchor")
            if not self.sidecar_effects:
                raise ValueError(f"{op}: sidecar_write must declare sidecar_effects")
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError(f"{op}: duplicate aliases")
        for alias in self.aliases:
            if not alias or "__" in alias or alias == op:
                raise ValueError(f"{op}: invalid alias {alias!r}")

    @property
    def anchor_model(self) -> str:
        if self.rw_class == RW_SIDECAR_WRITE:
            return ANCHOR_NONE
        return ANCHOR_ELEMENT if self.anchor_arg is not None else ANCHOR_WHOLE_DOCUMENT

    @property
    def lock_scope(self) -> str:
        if self.rw_class == RW_SIDECAR_WRITE:
            return LOCK_NONE
        return LOCK_SCOPED_ELEMENT if self.anchor_arg is not None else LOCK_WHOLE_DOCUMENT

    @property
    def fallback_policy(self) -> str:
        if self.rw_class == RW_SIDECAR_WRITE:
            return FALLBACK_NOT_APPLICABLE
        if self.anchor_arg is not None:
            return FALLBACK_WHOLE_DOCUMENT_WHEN_ANCHOR_ABSENT
        return FALLBACK_WHOLE_DOCUMENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "aliases": sorted(self.aliases),
            "rw_class": self.rw_class,
            "path_arg": self.path_arg,
            "anchor_arg": self.anchor_arg,
            "anchor_model": self.anchor_model,
            "lock_scope": self.lock_scope,
            "fallback_policy": self.fallback_policy,
            "sidecar_effects": sorted(self.sidecar_effects),
            "gated_by_tunnel_guard": self.gated_by_tunnel_guard,
        }


# (operation, path_arg, anchor_arg). Mirrors meridian/routes/tunnel.py
# _DOCS_WRITE_TOOLS exactly; tests/test_sprint_c8eabc3d_docx_capabilities.py
# fails if the two drift apart.
_GATED_DOCX_WRITES: tuple[tuple[str, str, str | None], ...] = (
    ("insert_image", "docx_path", "anchor_para_id"),
    ("insert_figure_block", "docx_path", "anchor_para_id"),
    ("insert_caption", "docx_path", "anchor_para_id"),
    ("edit_caption", "docx_path", "caption_para_id"),
    ("remove_caption", "docx_path", "caption_para_id"),
    ("retrofit_plaintext_captions", "docx_path", None),
    ("insert_cross_reference", "docx_path", "anchor_para_id"),
    ("insert_citation", "docx_path", "anchor_para_id"),
    ("edit_citation", "docx_path", "anchor_para_id"),
    ("remove_citation", "docx_path", "anchor_para_id"),
    ("insert_equation", "docx_path", "anchor_para_id"),
    ("edit_equation", "docx_path", "equation_para_id"),
    ("append_text_run_after_math", "docx_path", "equation_para_id"),
    ("remove_equation", "docx_path", "equation_para_id"),
    ("insert_bibliography_entry", "docx_path", None),
    ("update_bibliography_entry", "docx_path", None),
    ("remove_bibliography_entry", "docx_path", None),
    ("sync_bibliography", "docx_path", None),
    ("renumber_sequences", "docx_path", None),
    ("insert_highlighted_note", "docx_path", "anchor_para_id"),
    ("write_section", "docx_path", "anchor_para_id"),
    ("move_section", "docx_path", "section_id"),
    ("copy_section", "docx_path", "section_id"),
    ("relocate_figure", "docx_path", None),
    ("relocate_table", "docx_path", None),
    ("highlight_document", "docx_path", None),
    ("merge_docx_draft", "canonical_path", None),
    ("apply_reviewable_edit_transaction", "canonical_path", None),
)

# DOCX writers in meridian-docs/server.py whose docstrings name the tunnel-layer
# guard but that are absent from _DOCS_WRITE_TOOLS. Reported, not gated here.
# flag_for_review takes anchor as str | dict, so no element id is declared.
# remove_package_part mutates only when dry_run is False (default True).
_UNGATED_DOCX_WRITES: tuple[tuple[str, str, str | None], ...] = (
    ("remove_cross_reference", "docx_path", "anchor_para_id"),
    ("insert_media_part", "docx_path", "anchor_para_id"),
    ("remove_package_part", "docx_path", None),
    ("flag_for_review", "docx_path", None),
)

# Sidecar-only writers: they read the .docx and write a local SQLite index cache,
# never the .docx itself (see the tunnel.py comment beside _DOCS_WRITE_TOOLS).
_SIDECAR_WRITES: tuple[tuple[str, str], ...] = (
    ("index_document", "path"),
    ("index_document_structure", "path"),
    ("index_equations", "path"),
)

DOCX_OPERATIONS: tuple[DocxOperation, ...] = (
    tuple(
        DocxOperation(op, RW_DOCX_WRITE, path, anchor, gated_by_tunnel_guard=True)
        for op, path, anchor in _GATED_DOCX_WRITES
    )
    + tuple(
        DocxOperation(op, RW_DOCX_WRITE, path, anchor, gated_by_tunnel_guard=False)
        for op, path, anchor in _UNGATED_DOCX_WRITES
    )
    + tuple(
        DocxOperation(
            op, RW_SIDECAR_WRITE, path, None, sidecar_effects=(SIDECAR_SQLITE_INDEX,)
        )
        for op, path in _SIDECAR_WRITES
    )
)

UNGATED_DOCX_WRITERS: frozenset[str] = frozenset(op for op, _, _ in _UNGATED_DOCX_WRITES)


def _name_index(operations: Iterable[DocxOperation]) -> dict[str, DocxOperation]:
    """Map every canonical name and alias to its operation; reject collisions."""
    table: dict[str, DocxOperation] = {}
    for op in operations:
        for name in (op.operation, *op.aliases):
            if name in table:
                raise ValueError(f"duplicate DOCX capability name {name!r}")
            table[name] = op
    return table


def _canonical_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_docx_capability_manifest(
    operations: Iterable[DocxOperation] | None = None,
) -> dict[str, Any]:
    """Return the deterministic manifest for ``operations`` (default: the shared set).

    The manifest is sorted by canonical operation name and carries a
    ``manifest_hash`` over its contents, so a consumer can detect any change in
    the declared contract without comparing every field.
    """
    ops = tuple(DOCX_OPERATIONS if operations is None else operations)
    _name_index(ops)  # validates alias and name collisions
    entries = sorted((op.to_dict() for op in ops), key=lambda entry: entry["operation"])
    body = {"schema": DOCX_CAPABILITY_SCHEMA, "count": len(entries), "operations": entries}
    return {**body, "manifest_hash": _canonical_hash(body)}


def bare_tool_name(name: str) -> str:
    """Strip the ``<slot>__`` prefix the same way the tunnel gate does."""
    return name.split("__", 1)[1] if "__" in name else name


def resolve_operation(
    name: str, operations: Iterable[DocxOperation] | None = None
) -> DocxOperation | None:
    """Resolve a canonical name, alias, or prefixed tool name to its operation."""
    table = _name_index(DOCX_OPERATIONS if operations is None else operations)
    return table.get(bare_tool_name(name))


@dataclass(frozen=True)
class DocxWriteTarget:
    """The document and anchor a DOCX write call addresses."""

    operation: str
    docx_path: str
    element_id: str | None
    lock_scope: str
    fallback_applied: bool
    gated_by_tunnel_guard: bool


def extract_docx_write_target(
    name: str,
    arguments: dict | None,
    operations: Iterable[DocxOperation] | None = None,
) -> DocxWriteTarget | None:
    """Normalise a docs-slot write call into its target, or None.

    Fail-open, matching the tunnel gate: returns None for reads, sidecar writers,
    unknown tools, non-dict arguments, and calls whose path argument is missing or
    blank. A blank or absent anchor argument degrades to a whole-document target.
    """
    op = resolve_operation(name, operations)
    if op is None or op.rw_class != RW_DOCX_WRITE or not isinstance(arguments, dict):
        return None
    path_val = arguments.get(op.path_arg)
    if not isinstance(path_val, str) or not path_val.strip():
        return None
    element_id: str | None = None
    if op.anchor_arg is not None:
        anchor_val = arguments.get(op.anchor_arg)
        if isinstance(anchor_val, str) and anchor_val.strip():
            element_id = anchor_val.strip()
    fallback_applied = op.anchor_arg is not None and element_id is None
    return DocxWriteTarget(
        operation=op.operation,
        docx_path=path_val.strip(),
        element_id=element_id,
        lock_scope=LOCK_SCOPED_ELEMENT if element_id is not None else LOCK_WHOLE_DOCUMENT,
        fallback_applied=fallback_applied,
        gated_by_tunnel_guard=op.gated_by_tunnel_guard,
    )
