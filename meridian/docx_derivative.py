"""Shared validation for derivative-document (DOCX) provenance tracking (W1-K).

A "derivative" here is a document (typically a rendered export -- PDF/DOCX
render, a translated copy, a stripped-down summary) generated FROM a
canonical ``.docx`` source document, tracked well enough to later answer
"does this derivative still genuinely reflect its source" without re-reading
either file's bytes through the MCP server itself.

Mirrors :mod:`meridian.research_run` / :mod:`meridian.experiment`'s shape and
conventions (pure validation/normalization, no DB, no network, no filesystem
access) -- read either module first. See :mod:`meridian.db.docx_derivatives`
for persistence.

Deliberate deviation from those two siblings: ``source_path`` /
``derivative_path`` are NOT required to be project-relative and absolute
paths are NOT rejected here. A tracked ``.docx`` source/derivative pair
routinely lives outside the repository entirely (a user's Documents folder,
a shared drive, a Zotero attachments directory) -- unlike
``research_run.validate_allowed_paths`` / ``experiment.validate_logical_path``,
which bound a WRITE surface inside the project the caller must never escape,
a docx derivative path is pure descriptive metadata: it says where a document
lives, not where this session is allowed to write. Secret-shaped values are
still rejected exactly like every other text field in this codebase.

Hash-verification note: this module treats every content hash as an OPAQUE,
caller-supplied string (bounded length, secret-checked) -- it never computes
or re-derives a hash itself, mirroring ``experiment.validate_content_hash``'s
existing convention exactly. The MCP server has no guaranteed filesystem
access to either the source or the derivative document (the hosted tier in
particular cannot read a caller's local disk -- see
``extensions/meridian-outputs/meridian_outputs/fingerprint.py``'s identical
"NO hosted call is made" / caller-computes-the-hash convention for the
analogous non-docx artifact-staleness problem). A caller (self-hosted CLI,
local extension, or a human pasting a value) is responsible for computing
each hash from the document's actual current bytes before calling
``register_docx_derivative``/``verify_docx_diff``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from meridian.capability_manifest import _SECRET_LIKE_RE
from meridian.secret_redaction import check_for_secrets

# 88277b63 -- reuse the existing canonical ISO-timestamp helper rather than
# duplicating it, exactly like meridian.research_run / meridian.experiment do.
from meridian.external_job_register import utcnow_iso  # noqa: F401

DOCX_DERIVATIVE_STATUSES = frozenset({"candidate", "accepted", "superseded"})
# Only a 'candidate' may ever be promoted (meridian.db.docx_derivatives.
# promote_docx_candidate). 'accepted' is terminal-but-idempotent (a repeat
# promote call on an already-accepted row is a no-op, never an error);
# 'superseded' is terminal (a caller must register a fresh candidate, never
# resurrect a superseded row).
PROMOTABLE_STATUS = "candidate"

_DOCX_EXT = ".docx"

MAX_PATH_CHARS = 1_000
MAX_CONTENT_HASH_CHARS = 128
MAX_GENERATING_TOOL_CHARS = 300
MAX_NOTES_CHARS = 2_000


class DocxDerivativeError(ValueError):
    """Raised when docx-derivative input fails schema or safety validation."""


def _validate_text(
    value: object,
    *,
    field: str,
    max_chars: int,
    required: bool = False,
) -> "str | None":
    if value is None:
        if required:
            raise DocxDerivativeError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise DocxDerivativeError(f"{field} must be a string")
    text = value.strip()
    if not text:
        if required:
            raise DocxDerivativeError(f"{field} is required")
        return None
    if len(text) > max_chars:
        raise DocxDerivativeError(f"{field} exceeds the {max_chars}-character limit")
    if _SECRET_LIKE_RE.search(text):
        raise DocxDerivativeError(f"{field} looks secret-shaped; refusing to persist")
    # check_for_secrets raises ValueError (not DocxDerivativeError) on a
    # match -- let it propagate as-is, matching research_run/experiment's
    # own convention of reusing this exact fail-closed gate unmodified.
    check_for_secrets(text, context=f"docx derivative {field}")
    return text


def validate_docx_path(value: object, *, field: str) -> str:
    """Validate a source/derivative document path.

    Required, non-empty, secret-checked, bounded, and must name a ``.docx``
    file (case-insensitive suffix check) -- this tooling is explicitly
    scoped to Word documents, not an arbitrary-file provenance tracker.
    Deliberately does NOT reject absolute paths -- see module docstring.
    """
    text = _validate_text(value, field=field, max_chars=MAX_PATH_CHARS, required=True)
    assert text is not None
    normalized = text.replace("\\", "/")
    if not normalized.lower().endswith(_DOCX_EXT):
        raise DocxDerivativeError(f"{field} must name a {_DOCX_EXT} file, got {value!r}")
    return text


def validate_content_hash(value: object, *, field: str, required: bool) -> "str | None":
    """Validate a caller-supplied content-hash string.

    Treated as an OPAQUE identifier (see module docstring) -- bounded length
    and secret-checked, but no particular hash algorithm/format is enforced,
    mirroring ``meridian.experiment.validate_content_hash`` exactly.
    """
    return _validate_text(value, field=field, max_chars=MAX_CONTENT_HASH_CHARS, required=required)


def validate_generating_tool(value: object) -> "str | None":
    return _validate_text(value, field="generating_tool", max_chars=MAX_GENERATING_TOOL_CHARS)


def validate_notes(value: object) -> "str | None":
    return _validate_text(value, field="notes", max_chars=MAX_NOTES_CHARS)


def validate_status(value: object) -> str:
    """Normalize and validate a docx-derivative status against the closed
    vocabulary (candidate | accepted | superseded)."""
    status = value.strip().lower() if isinstance(value, str) else ""
    if status not in DOCX_DERIVATIVE_STATUSES:
        raise DocxDerivativeError(
            f"status must be one of {sorted(DOCX_DERIVATIVE_STATUSES)}, got {value!r}"
        )
    return status


def validate_generated_at(value: object) -> str:
    """Validate an explicit ``generated_at`` timestamp (ISO 8601), defaulting
    to now (UTC) when omitted. Never inferred from anything other than an
    explicit caller value or "now" -- there is no third option."""
    if value is None:
        return utcnow_iso()
    if not isinstance(value, str) or not value.strip():
        raise DocxDerivativeError("generated_at must be an ISO-8601 timestamp string")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise DocxDerivativeError(f"generated_at is not a valid ISO-8601 timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat(timespec="microseconds")


def validate_register_fields(
    *,
    source_path: object,
    derivative_path: object,
    source_content_hash: object,
    derivative_content_hash: object = None,
    generating_tool: object = None,
    generated_at: object = None,
    notes: object = None,
) -> dict[str, Any]:
    """Validate the full field set needed to register a new docx derivative."""
    return {
        "source_path": validate_docx_path(source_path, field="source_path"),
        "derivative_path": validate_docx_path(derivative_path, field="derivative_path"),
        "source_content_hash": validate_content_hash(
            source_content_hash, field="source_content_hash", required=True,
        ),
        "derivative_content_hash": validate_content_hash(
            derivative_content_hash, field="derivative_content_hash", required=False,
        ),
        "generating_tool": validate_generating_tool(generating_tool),
        "generated_at": validate_generated_at(generated_at),
        "notes": validate_notes(notes),
    }
