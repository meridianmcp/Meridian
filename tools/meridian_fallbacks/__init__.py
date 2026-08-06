"""tools/meridian_fallbacks -- versioned local fallback tools for safe
OOXML (.docx) editing.

Proposal 1abedabe-2f82-40e5-a320-3b32d550cc40. This package exists so an
executor still has a documented, already-wired way to make safe, validated,
atomic edits to a .docx file when the ``meridian-docs`` MCP extension or a
Word MCP tunnel slot is unavailable -- the "fallback_chain" concept
AGENTS.md's capability-manifest section (649e095f) describes, made concrete.
It has ZERO dependency on the Meridian server, its DB, any MCP tool, or
``python-docx`` (not a dependency of this repo's pixi environment) -- stdlib
``zipfile`` + ``xml.etree.ElementTree`` only. See ``capability_manifest.json``
in this same directory for the full package contract: version, per-module
source hashes, supported operations, limitations, related proposals, and a
copy/paste-ready usage example.

Do not depend on Serena memories for how this package behaves -- this
module's docstrings, ``capability_manifest.json``, and the tracked source
itself are the source of truth (per the originating proposal).

Package layout
--------------
``safe_ooxml_writer``   -- validate-then-atomic-replace primitive every
                            other module in this package is built on.
``safe_image_insert``   -- insert an image as an inline drawing run.
``patch_manifest``      -- durable, reviewable record of planned edits.
``transactional_merge`` -- apply a ``PatchManifest`` as one all-or-nothing
                            transaction.

Package-boundary note (sibling items landing in parallel worktrees):
``output_provenance_gate`` (sprint item d3374b0e) and
``docx_completion_gate`` (sprint item 8419f55f) are tracked in this SAME
package by two sibling sprint items implementing them in parallel isolated
worktrees. This ``__init__`` deliberately does NOT import either module --
see ``capability_manifest.json``'s ``modules`` section, where they are
recorded by string name only (``"status": "planned"``) rather than via a
live Python import here, specifically so landing this item never conflicts
with those two sibling commits. Once they land, import them directly
(``from tools.meridian_fallbacks import docx_completion_gate``) rather than
expecting either re-exported from this ``__init__``.
"""
from __future__ import annotations

__version__ = "0.1.0"
PACKAGE_VERSION = __version__
SCHEMA_VERSION = 1  # bump alongside capability_manifest.json's own schema_version

from .patch_manifest import (
    PATCH_MANIFEST_SCHEMA_VERSION,
    PatchManifest,
    PatchManifestError,
    PatchOperation,
)
from .safe_image_insert import (
    ImageInsertError,
    ImageInsertResult,
    ImageInsertWriteResult,
    compute_image_insert_parts,
    insert_image,
)
from .safe_ooxml_writer import (
    REQUIRED_PARTS,
    DocxValidationError,
    DocxValidationReport,
    DocxWriteError,
    SafeOoxmlWriter,
    WriteResult,
    build_zip_bytes,
    compute_sha256,
    verify_zip_bytes,
)
from .transactional_merge import (
    DEFAULT_APPLIERS,
    MergeConflictError,
    MergeResult,
    TransactionError,
    apply_patch_manifest,
    rollback,
)

__all__ = [
    "PACKAGE_VERSION",
    "SCHEMA_VERSION",
    # safe_ooxml_writer
    "REQUIRED_PARTS",
    "SafeOoxmlWriter",
    "WriteResult",
    "DocxWriteError",
    "DocxValidationError",
    "DocxValidationReport",
    "verify_zip_bytes",
    "build_zip_bytes",
    "compute_sha256",
    # safe_image_insert
    "insert_image",
    "compute_image_insert_parts",
    "ImageInsertError",
    "ImageInsertResult",
    "ImageInsertWriteResult",
    # patch_manifest
    "PatchManifest",
    "PatchOperation",
    "PatchManifestError",
    "PATCH_MANIFEST_SCHEMA_VERSION",
    # transactional_merge
    "apply_patch_manifest",
    "rollback",
    "MergeResult",
    "TransactionError",
    "MergeConflictError",
    "DEFAULT_APPLIERS",
]
