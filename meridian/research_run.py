"""Shared validation for bounded ephemeral research runs (a5343387).

A research run is an ADJACENT, project-scoped scratch/probe primitive for
disposable subagent work -- a read-only investigation, or a small
isolated-worktree write -- that should not need a formal sprint item, a
formal ``claim_file``, or a durable handoff entry unless a caller
explicitly promotes it (see :func:`meridian.db.research_runs.promote_research_run`).
This module mirrors :mod:`meridian.external_job_register`'s shape and
conventions (pure validation/normalization, no DB, no network) -- read that
module first. See :mod:`meridian.db.research_runs` for persistence.

DRIFT NOTE -- table naming collision (read before touching the DB layer)
--------------------------------------------------------------------------
The sprint-item brief that produced this module suggested a SQL table named
``research_runs``. That name is ALREADY TAKEN: ``meridian/db/experiment_model.py``
(4376e655) already defines a ``research_runs`` table for an entirely
different concept -- one logical execution of a named ML-style experiment,
with ``research_run_attempts`` tracking numbered retries. Reusing that name
here would silently collide with a live, differently-shaped table (no
``mode``/``allowed_paths``/``turn_budget``/``expires_at``/``result_receipt``
columns there at all). To avoid that collision, the persistence layer for
THIS module (:mod:`meridian.db.research_runs`, a different, new file) uses
the table name ``scratch_research_runs`` instead. The Python module names
requested by the brief (``meridian/research_run.py``, i.e. this file, and
``meridian/db/research_runs.py``) do not themselves collide with anything
and are used as specified.

Byte-bound policy for ``result_receipt`` (documented choice)
--------------------------------------------------------------------------
The brief allows either "reject" or "truncate deterministically" once the
16KB receipt cap is exceeded. This module REJECTS (raises ``ValueError``)
rather than silently or deterministically truncating -- matching this
codebase's existing convention for exactly this situation
(``external_job_register.validate_metadata`` does the same: raises rather
than truncating when the encoded metadata exceeds its own byte cap). A
caller that hits the cap must trim ``files_touched``/``commands_run``/
``result_summary`` and retry; nothing is ever silently dropped.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from meridian.capability_manifest import _ABSOLUTE_PATH_RE, _SECRET_LIKE_RE
from meridian.secret_redaction import check_for_secrets

# 88277b63 -- reuse the existing canonical ISO-timestamp helper rather than
# duplicating it (per this sprint item's own instructions). It is re-exported
# under the same name here so callers of this module don't need to know it
# actually lives in external_job_register.
from meridian.external_job_register import utcnow_iso  # noqa: F401

RESEARCH_RUN_STATUSES = frozenset(
    {"active", "completed", "failed", "abandoned", "expired"}
)
RESEARCH_RUN_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "abandoned", "expired"}
)
RUN_MODES = frozenset({"read_only", "isolated_write"})
RUN_DISPOSITIONS = frozenset({"keep", "discard", "promote"})

# --- byte/length bounds -----------------------------------------------------
MAX_RECEIPT_BYTES = 16_000

MAX_FILES_TOUCHED_ENTRIES = 200
MAX_FILES_TOUCHED_PATH_CHARS = 500
MAX_COMMANDS_RUN_ENTRIES = 50
MAX_COMMAND_CHARS = 1_000
MAX_RESULT_SUMMARY_CHARS = 4_000
MAX_ARTIFACT_REFERENCES_ENTRIES = 100
MAX_ARTIFACT_REFERENCE_CHARS = 200
MAX_FAILURE_REASON_CHARS = 2_000

MAX_ALLOWED_PATHS_ENTRIES = 100
MAX_ALLOWED_PATH_CHARS = 500
MAX_REPOSITORY_ID_CHARS = 300

MAX_TURN_BUDGET = 1_000

DEFAULT_TTL_SECONDS = 3_600
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 24 * 3_600

_RECEIPT_ALLOWED_FIELDS = frozenset(
    {"files_touched", "commands_run", "result_summary", "artifact_references", "failure_reason"}
)

# A bare leading "/" or a Windows drive-letter prefix on an otherwise
# project-relative path -- checked in ADDITION to capability_manifest's own
# _ABSOLUTE_PATH_RE (which only matches a few well-known absolute roots), so
# an arbitrary "/anything" or "C:\anything" is caught too, not just the
# handful of home/etc/var-style prefixes that regex recognizes.
_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:[\\/]")


class ResearchRunError(ValueError):
    """Raised when research-run input fails schema or safety validation."""


def validate_run_status(value: object) -> str:
    """Normalize and validate a research-run status against the closed vocabulary."""
    status = value.strip().lower() if isinstance(value, str) else ""
    if status not in RESEARCH_RUN_STATUSES:
        raise ResearchRunError(
            f"research run status must be one of {sorted(RESEARCH_RUN_STATUSES)}, got {value!r}"
        )
    return status


def validate_run_mode(value: object) -> str:
    """Normalize and validate a research-run mode: read_only | isolated_write."""
    mode = value.strip().lower() if isinstance(value, str) else ""
    if mode not in RUN_MODES:
        raise ResearchRunError(f"mode must be one of {sorted(RUN_MODES)}, got {value!r}")
    return mode


def validate_disposition(value: object, *, required: bool = True) -> "str | None":
    """Normalize and validate a receipt disposition. Never inferred -- the
    caller must always pass one explicitly when ``required`` (the default)."""
    if value is None:
        if required:
            raise ResearchRunError(
                "disposition is required and must be explicit: one of "
                f"{sorted(RUN_DISPOSITIONS)} -- never inferred"
            )
        return None
    disposition = value.strip().lower() if isinstance(value, str) else ""
    if disposition not in RUN_DISPOSITIONS:
        raise ResearchRunError(
            f"disposition must be one of {sorted(RUN_DISPOSITIONS)}, got {value!r}"
        )
    return disposition


def validate_turn_budget(value: object) -> int:
    """Validate the max-turns/steps budget for a run: a positive, bounded int."""
    try:
        budget = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ResearchRunError("turn_budget must be an integer") from None
    if isinstance(value, bool):
        raise ResearchRunError("turn_budget must be an integer, not a bool")
    if budget <= 0:
        raise ResearchRunError("turn_budget must be a positive integer")
    if budget > MAX_TURN_BUDGET:
        raise ResearchRunError(f"turn_budget exceeds the maximum of {MAX_TURN_BUDGET}")
    return budget


def validate_ttl_seconds(value: object) -> int:
    """Validate the run's time-to-live in seconds; defaults when omitted."""
    if value is None:
        return DEFAULT_TTL_SECONDS
    try:
        ttl = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ResearchRunError("ttl_seconds must be an integer") from None
    if ttl < MIN_TTL_SECONDS or ttl > MAX_TTL_SECONDS:
        raise ResearchRunError(
            f"ttl_seconds must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS}, got {ttl}"
        )
    return ttl


def _looks_like_absolute_path(text: str) -> bool:
    """Combine capability_manifest's _ABSOLUTE_PATH_RE with a couple of
    generic checks (bare leading '/', any drive letter) so THIS module
    rejects a broader set of absolute-path shapes than that regex alone
    covers (it only recognizes a handful of well-known unix roots)."""
    if not text:
        return False
    if text.startswith("/") or text.startswith("\\\\"):
        return True
    if _DRIVE_LETTER_RE.match(text):
        return True
    return bool(_ABSOLUTE_PATH_RE.search(text))


def _reject_secrets_and_absolute_paths(
    text: str, *, field: str, reject_absolute: bool
) -> None:
    if reject_absolute and _looks_like_absolute_path(text):
        raise ResearchRunError(
            f"{field} must be a project-relative path, never a machine-local "
            f"absolute path: {text!r}"
        )
    if _SECRET_LIKE_RE.search(text):
        raise ResearchRunError(f"{field} looks secret-shaped; refusing to persist")
    # check_for_secrets raises ValueError (not ResearchRunError) on a match --
    # let it propagate as-is, matching external_job_register's own convention
    # of reusing this exact fail-closed gate unmodified.
    check_for_secrets(text, context=f"research run {field}")


def _validate_str_list(
    value: object,
    *,
    field: str,
    max_entries: int,
    max_chars: int,
    reject_absolute: bool = False,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ResearchRunError(f"{field} must be a list of strings")
    if len(value) > max_entries:
        raise ResearchRunError(f"{field} exceeds {max_entries} entries")
    out: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise ResearchRunError(f"{field}[{idx}] must be a string")
        text = item.strip()
        if not text:
            continue
        if len(text) > max_chars:
            raise ResearchRunError(f"{field}[{idx}] exceeds {max_chars} characters")
        _reject_secrets_and_absolute_paths(text, field=f"{field}[{idx}]", reject_absolute=reject_absolute)
        out.append(text.replace("\\", "/") if reject_absolute else text)
    return out


def _validate_text(value: object, *, field: str, max_chars: int) -> "str | None":
    if value is None:
        return None
    if not isinstance(value, str):
        raise ResearchRunError(f"{field} must be a string")
    text = value.strip()
    if not text:
        return None
    if len(text) > max_chars:
        raise ResearchRunError(f"{field} exceeds the {max_chars}-character limit")
    _reject_secrets_and_absolute_paths(text, field=field, reject_absolute=True)
    return text


def validate_repository_id(value: object) -> str:
    """Validate the canonical repository/worktree identity: a non-empty
    STRING identifier, never a machine-local absolute path (e.g.
    ``"meridian-repo@worktree:wf_a1ea1dc6-002-1"``, not ``C:\\Users\\...``)."""
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ResearchRunError("repository_id is required")
    if len(text) > MAX_REPOSITORY_ID_CHARS:
        raise ResearchRunError(f"repository_id exceeds {MAX_REPOSITORY_ID_CHARS} characters")
    _reject_secrets_and_absolute_paths(text, field="repository_id", reject_absolute=True)
    return text


def validate_allowed_paths(value: object) -> list[str]:
    """Validate the bounded list of project-RELATIVE write paths.

    Rejects: non-list input, too many entries, an absolute path (any of
    capability_manifest's recognized shapes, a bare leading '/', a drive
    letter, or a UNC path), a '..' path-traversal segment, or a
    secret-shaped value. Never silently drops or truncates an entry --
    a violation always raises.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ResearchRunError("allowed_paths must be a list of project-relative path strings")
    if len(value) > MAX_ALLOWED_PATHS_ENTRIES:
        raise ResearchRunError(f"allowed_paths exceeds {MAX_ALLOWED_PATHS_ENTRIES} entries")
    out: list[str] = []
    for idx, raw in enumerate(value):
        if not isinstance(raw, str) or not raw.strip():
            raise ResearchRunError(f"allowed_paths[{idx}] must be a non-empty string")
        text = raw.strip()
        if len(text) > MAX_ALLOWED_PATH_CHARS:
            raise ResearchRunError(f"allowed_paths[{idx}] exceeds {MAX_ALLOWED_PATH_CHARS} characters")
        normalized = text.replace("\\", "/")
        if any(seg == ".." for seg in normalized.split("/")):
            raise ResearchRunError(
                f"allowed_paths[{idx}] must not escape the project root via '..': {raw!r}"
            )
        _reject_secrets_and_absolute_paths(text, field=f"allowed_paths[{idx}]", reject_absolute=True)
        out.append(normalized.lstrip("/"))
    return out


def validate_run_fields(
    *,
    mode: object,
    repository_id: object,
    allowed_paths: object = None,
    turn_budget: object,
    ttl_seconds: object = None,
) -> dict[str, Any]:
    """Validate the full field set needed to start a new research run."""
    validated_mode = validate_run_mode(mode)
    validated_paths = validate_allowed_paths(allowed_paths)
    if validated_mode == "isolated_write" and not validated_paths:
        raise ResearchRunError(
            "isolated_write mode requires a non-empty allowed_paths list bounding "
            "what may be written"
        )
    return {
        "mode": validated_mode,
        "repository_id": validate_repository_id(repository_id),
        "allowed_paths": validated_paths,
        "turn_budget": validate_turn_budget(turn_budget),
        "ttl_seconds": validate_ttl_seconds(ttl_seconds),
    }


def is_write_path_allowed(path: object, allowed_paths: "list[str]") -> bool:
    """True when ``path`` (a project-relative candidate write path) falls
    inside one of ``allowed_paths`` -- an exact match or nested under an
    allowed directory. Used to enforce the isolated_write bounded-paths
    rule: any write path must be inside the run's declared allowed_paths."""
    if not isinstance(path, str) or not path.strip():
        return False
    candidate = path.strip().replace("\\", "/").lstrip("/")
    for allowed in allowed_paths or []:
        norm = str(allowed).replace("\\", "/").strip("/")
        if not norm:
            continue
        if candidate == norm or candidate.startswith(norm + "/"):
            return True
    return False


def validate_result_receipt(value: object) -> dict[str, Any]:
    """Validate a (disposition-less) result receipt: bounded per-field, then
    the whole encoded receipt is checked against the 16KB cap.

    ``disposition`` is validated and stored SEPARATELY (see
    :func:`validate_disposition`) -- it is its own column on the persisted
    record, not embedded in this JSON blob, so it is not accepted here.
    """
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ResearchRunError("result_receipt must be an object")
    unknown = set(value) - _RECEIPT_ALLOWED_FIELDS
    if unknown:
        raise ResearchRunError(f"result_receipt has unknown field(s): {sorted(unknown)}")

    receipt = {
        "files_touched": _validate_str_list(
            value.get("files_touched"), field="files_touched",
            max_entries=MAX_FILES_TOUCHED_ENTRIES, max_chars=MAX_FILES_TOUCHED_PATH_CHARS,
            reject_absolute=True,
        ),
        "commands_run": _validate_str_list(
            value.get("commands_run"), field="commands_run",
            max_entries=MAX_COMMANDS_RUN_ENTRIES, max_chars=MAX_COMMAND_CHARS,
            reject_absolute=True,
        ),
        "result_summary": _validate_text(
            value.get("result_summary"), field="result_summary", max_chars=MAX_RESULT_SUMMARY_CHARS,
        ),
        "artifact_references": _validate_str_list(
            value.get("artifact_references"), field="artifact_references",
            max_entries=MAX_ARTIFACT_REFERENCES_ENTRIES, max_chars=MAX_ARTIFACT_REFERENCE_CHARS,
            reject_absolute=True,
        ),
        "failure_reason": _validate_text(
            value.get("failure_reason"), field="failure_reason", max_chars=MAX_FAILURE_REASON_CHARS,
        ),
    }
    encoded = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    size = len(encoded.encode("utf-8"))
    if size > MAX_RECEIPT_BYTES:
        raise ResearchRunError(
            f"result_receipt exceeds the {MAX_RECEIPT_BYTES}-byte cap ({size} bytes) -- "
            "trim files_touched/commands_run/result_summary/artifact_references "
            "before completing this run"
        )
    return receipt


def compute_expires_at(started_at_iso: str, ttl_seconds: int) -> str:
    """Return ``started_at_iso + ttl_seconds`` as an ISO8601 UTC string."""
    started = datetime.fromisoformat(started_at_iso)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    expires = started + timedelta(seconds=ttl_seconds)
    return expires.isoformat(timespec="microseconds")


def is_run_expired(expires_at_iso: "str | None", *, now: "datetime | None" = None) -> bool:
    """True when ``expires_at_iso`` is a real timestamp in the past."""
    if not expires_at_iso:
        return False
    try:
        expires = datetime.fromisoformat(expires_at_iso)
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return expires < now
