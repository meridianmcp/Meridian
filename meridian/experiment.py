"""Shared validation for the Experiment Registry (W1-M, item 3f6b8715).

Mirrors :mod:`meridian.research_run`'s structure and conventions exactly
(read that module first): pure validation/normalization, no DB, no network.
See :mod:`meridian.db.experiments` for persistence.

RELATIONSHIP TO THE PRE-EXISTING ``experiments`` TABLE (4376e655)
--------------------------------------------------------------------------
An ``experiments`` table already exists (:mod:`meridian.db.experiment_model`)
with columns ``id, project_id, name, config_template, created_by, created_at,
updated_at`` feeding an unrelated ML-style Experiment/Run/RunAttempt state
model (``research_runs`` / ``research_run_attempts``). This registry reuses
the SAME ``experiments`` table (adding ``hypothesis``, ``status``,
``creator_session_id`` columns via a guarded ``ALTER TABLE`` migration —
never ``CREATE TABLE IF NOT EXISTS``, which would silently no-op against the
already-existing table) but is otherwise a SEPARATE, additive interface:
:mod:`meridian.db.experiments` (new file) never reads or writes
``config_template``/``created_by``, and :mod:`meridian.db.experiment_model`
is left completely unchanged. The two interfaces coexist on the same table
the way two independent readers can each own a disjoint column subset.

This registry's own run/artifact/event tables (``experiment_runs``,
``run_artifacts``, ``run_manifest_items``, ``experiment_events``) are all
brand new — no naming collision to route around, unlike
:mod:`meridian.research_run`'s ``scratch_research_runs`` drift note.

DEVIATIONS FROM THE SPRINT-ITEM BRIEF (documented, not silently resolved)
--------------------------------------------------------------------------
1. ``experiment_runs.outcome_summary`` / ``.disposition`` are declared
   NULLABLE in the actual schema, not ``NOT NULL`` as the brief's column
   list literally states. A run row is created by :func:`start_experiment_run`
   before any outcome exists — a real SQL ``NOT NULL`` would force a
   placeholder value into a closed-enum column (``disposition``) or defeat
   :func:`validate_outcome_summary`'s own "reject null/empty" contract by
   requiring a non-empty placeholder that isn't the real outcome. The
   substantive requirement — a run can never be COMPLETED without a real,
   validated outcome_summary/disposition — is enforced in
   :func:`meridian.db.experiments.complete_experiment_run` (raises
   ``ValueError`` exactly as specified) and is stronger than a bare column
   constraint could express anyway (a DB-level NOT NULL can't tell "not yet
   started" apart from "actively being withheld").
2. ``start_experiment_run``'s brief signature omits ``repository_id``/
   ``worktree_id`` even though ``experiment_runs`` declares both columns.
   Both are added as optional keyword arguments (default ``None``) so the
   columns have a real write path; omitted, they simply stay ``NULL``,
   matching this codebase's established "optional, nullable, never
   inferred" convention for identity hints (see
   :func:`meridian.research_run.validate_repository_id`, used only when the
   caller supplies one).
3. ``complete_experiment_run``'s brief signature has no ``status`` argument,
   yet the brief's own next sentence conditions dead-end detection on
   "status ends up 'abandoned'" — a value nothing in the given signature can
   ever produce. Resolved by adding an optional ``status`` keyword
   (``"completed"`` default, ``"abandoned"`` the only other accepted value —
   ``"active"``/``"expired"`` are rejected here: expiry is
   :func:`expire_stale_runs`'s own exclusive path) so a caller can
   explicitly mark a run abandoned while still supplying the required
   ``outcome_summary``/``disposition``.
4. The brief's ``complete_experiment_run`` bullet only spells out an
   auto-write for the dead-end trigger ("If status ends up 'abandoned' OR
   outcome_summary contains 'dead end'/'failed' ... AUTO-WRITE ... dead_end
   ...") — read literally, a clean 'keep' completion would reach a terminal
   status with ZERO experiment_events rows. That directly contradicts the
   brief's own separately-stated HARD INVARIANT ("no run should ever reach a
   terminal status ... without a corresponding experiment_events row ...
   every terminal-transition code path above already auto-writes one"),
   which is unconditional and governs here.
   :func:`meridian.db.experiments.complete_experiment_run` resolves this by
   ALSO auto-writing an event on the non-dead-end path — ``event_type:
   'milestone'``, ``label: 'auto'``, ``body: outcome_summary`` — so every
   terminal transition, dead-end or not, leaves a trail. This was caught
   by, and is exactly what, this sprint item's own required dedicated
   invariant test (tests/test_experiments.py) is for.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from meridian.capability_manifest import _ABSOLUTE_PATH_RE, _SECRET_LIKE_RE
from meridian.secret_redaction import check_for_secrets

# 3f6b8715 -- reuse the existing canonical ISO-timestamp helper rather than
# duplicating it, exactly like meridian.research_run does. Re-exported under
# the same name here so callers of this module don't need to know it
# actually lives in external_job_register.
from meridian.external_job_register import utcnow_iso  # noqa: F401

EXPERIMENT_STATUSES = frozenset({"active", "archived"})
RUN_STATUSES = frozenset({"active", "completed", "abandoned", "expired"})
RUN_TERMINAL_STATUSES = frozenset({"completed", "abandoned", "expired"})
# complete_experiment_run only ever produces one of these two terminal
# statuses explicitly -- 'expired' is exclusively expire_stale_runs' path.
RUN_COMPLETION_STATUSES = frozenset({"completed", "abandoned"})
RUN_DISPOSITIONS = frozenset({"keep", "discard", "promote"})
EVENT_TYPES = frozenset({"dead_end", "pivot", "breakthrough", "note", "milestone"})
ARTIFACT_ROLES = frozenset({"figure", "dataset", "model", "checkpoint", "log"})
HOST_VISIBILITIES = frozenset({"local", "tunnel", "public"})

# --- byte/length bounds -----------------------------------------------------
MAX_RESULT_RECEIPT_BYTES = 32_768
MAX_OUTCOME_SUMMARY_CHARS = 4_000
MAX_HYPOTHESIS_CHARS = 4_000
MAX_NAME_CHARS = 300
MAX_TRIAL_LABEL_CHARS = 200
MAX_LOGICAL_PATH_CHARS = 500
MAX_CONTENT_HASH_CHARS = 128
MAX_LABEL_CHARS = 200
MAX_BODY_CHARS = 8_000
MAX_ARTIFACT_IDS_ENTRIES = 100
MAX_ARTIFACT_ID_CHARS = 200
MAX_RESOURCE_PROFILE_BYTES = 8_000

MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 30 * 24 * 3_600

# A bare leading "/" or a Windows drive-letter prefix on an otherwise
# project-relative path -- checked in ADDITION to capability_manifest's own
# _ABSOLUTE_PATH_RE, so an arbitrary "/anything" or "C:\anything" is caught
# too, not just the handful of home/etc/var-style prefixes that regex
# recognizes. Identical to meridian.research_run's own helper.
_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:[\\/]")


class ExperimentError(ValueError):
    """Raised when experiment-registry input fails schema or safety validation."""


def _looks_like_absolute_path(text: str) -> bool:
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
        raise ExperimentError(
            f"{field} must be a project-relative path, never a machine-local "
            f"absolute path: {text!r}"
        )
    if _SECRET_LIKE_RE.search(text):
        raise ExperimentError(f"{field} looks secret-shaped; refusing to persist")
    # check_for_secrets raises ValueError (not ExperimentError) on a match --
    # let it propagate as-is, matching research_run's own convention of
    # reusing this exact fail-closed gate unmodified.
    check_for_secrets(text, context=f"experiment {field}")


def _validate_text(
    value: object,
    *,
    field: str,
    max_chars: int,
    required: bool = False,
    reject_absolute: bool = False,
) -> "str | None":
    if value is None:
        if required:
            raise ExperimentError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ExperimentError(f"{field} must be a string")
    text = value.strip()
    if not text:
        if required:
            raise ExperimentError(f"{field} is required")
        return None
    if len(text) > max_chars:
        raise ExperimentError(f"{field} exceeds the {max_chars}-character limit")
    _reject_secrets_and_absolute_paths(text, field=field, reject_absolute=reject_absolute)
    return text


def validate_experiment_name(value: object) -> str:
    """A non-empty, bounded experiment name -- required, never inferred."""
    text = _validate_text(value, field="name", max_chars=MAX_NAME_CHARS, required=True)
    assert text is not None
    return text


def validate_hypothesis(value: object) -> "str | None":
    return _validate_text(value, field="hypothesis", max_chars=MAX_HYPOTHESIS_CHARS)


def validate_trial_label(value: object) -> "str | None":
    return _validate_text(value, field="trial_label", max_chars=MAX_TRIAL_LABEL_CHARS)


def validate_run_status(value: object) -> str:
    """Normalize and validate a run status against the closed vocabulary."""
    status = value.strip().lower() if isinstance(value, str) else ""
    if status not in RUN_STATUSES:
        raise ExperimentError(
            f"run status must be one of {sorted(RUN_STATUSES)}, got {value!r}"
        )
    return status


def validate_completion_status(value: object) -> str:
    """The subset of RUN_STATUSES complete_experiment_run may explicitly
    produce -- 'active' (not a completion) and 'expired' (expire_stale_runs'
    exclusive path) are both rejected here."""
    status = value.strip().lower() if isinstance(value, str) else ""
    if status not in RUN_COMPLETION_STATUSES:
        raise ExperimentError(
            f"status must be one of {sorted(RUN_COMPLETION_STATUSES)} when "
            f"explicitly completing a run, got {value!r}"
        )
    return status


def validate_disposition(value: object, *, required: bool = True) -> "str | None":
    """Normalize and validate a run disposition. Never inferred -- the
    caller must always pass one explicitly when ``required`` (the default)."""
    if value is None:
        if required:
            raise ExperimentError(
                "disposition is required and must be explicit: one of "
                f"{sorted(RUN_DISPOSITIONS)} -- never inferred"
            )
        return None
    disposition = value.strip().lower() if isinstance(value, str) else ""
    if disposition not in RUN_DISPOSITIONS:
        raise ExperimentError(
            f"disposition must be one of {sorted(RUN_DISPOSITIONS)}, got {value!r}"
        )
    return disposition


def validate_outcome_summary(value: object, *, required: bool = True) -> "str | None":
    """Normalize and validate an outcome summary. REJECTS null/empty when
    ``required`` (the default) -- never inferred, never defaulted."""
    is_blank = value is None or (isinstance(value, str) and not value.strip())
    if is_blank:
        if required:
            raise ExperimentError(
                "outcome_summary is required and must be a non-empty string -- "
                "never inferred"
            )
        return None
    if not isinstance(value, str):
        raise ExperimentError("outcome_summary must be a string")
    text = value.strip()
    if len(text) > MAX_OUTCOME_SUMMARY_CHARS:
        raise ExperimentError(
            f"outcome_summary exceeds the {MAX_OUTCOME_SUMMARY_CHARS}-character limit"
        )
    check_for_secrets(text, context="experiment run outcome_summary")
    return text


def validate_event_type(value: object) -> str:
    event_type = value.strip().lower() if isinstance(value, str) else ""
    if event_type not in EVENT_TYPES:
        raise ExperimentError(
            f"event_type must be one of {sorted(EVENT_TYPES)}, got {value!r}"
        )
    return event_type


def validate_artifact_role(value: object) -> "str | None":
    if value is None:
        return None
    role = value.strip().lower() if isinstance(value, str) else ""
    if role not in ARTIFACT_ROLES:
        raise ExperimentError(
            f"artifact_role must be one of {sorted(ARTIFACT_ROLES)}, got {value!r}"
        )
    return role


def validate_logical_path(value: object) -> str:
    """Validate a project-RELATIVE artifact path. Rejects an absolute path
    (any shape capability_manifest recognizes, a bare leading '/', a drive
    letter, or a UNC path), a '..' path-traversal segment, or a
    secret-shaped value -- mirrors meridian.research_run.validate_allowed_paths'
    per-entry checks exactly."""
    text = _validate_text(
        value, field="logical_path", max_chars=MAX_LOGICAL_PATH_CHARS,
        required=True, reject_absolute=True,
    )
    assert text is not None
    normalized = text.replace("\\", "/")
    if any(seg == ".." for seg in normalized.split("/")):
        raise ExperimentError(
            f"logical_path must not escape the project root via '..': {value!r}"
        )
    return normalized.lstrip("/")


def validate_content_hash(value: object) -> "str | None":
    return _validate_text(value, field="content_hash", max_chars=MAX_CONTENT_HASH_CHARS)


def validate_label(value: object) -> "str | None":
    return _validate_text(value, field="label", max_chars=MAX_LABEL_CHARS)


def validate_body(value: object) -> "str | None":
    return _validate_text(value, field="body", max_chars=MAX_BODY_CHARS)


def validate_artifact_ids(value: object) -> "list[str]":
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExperimentError("artifact_ids must be a list of strings")
    if len(value) > MAX_ARTIFACT_IDS_ENTRIES:
        raise ExperimentError(f"artifact_ids exceeds {MAX_ARTIFACT_IDS_ENTRIES} entries")
    out: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ExperimentError(f"artifact_ids[{idx}] must be a non-empty string")
        text = item.strip()
        if len(text) > MAX_ARTIFACT_ID_CHARS:
            raise ExperimentError(f"artifact_ids[{idx}] exceeds {MAX_ARTIFACT_ID_CHARS} characters")
        out.append(text)
    return out


def validate_resource_profile(value: object) -> "dict[str, Any] | None":
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ExperimentError("resource_profile must be an object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ExperimentError("resource_profile must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_RESOURCE_PROFILE_BYTES:
        raise ExperimentError(
            f"resource_profile exceeds the {MAX_RESOURCE_PROFILE_BYTES}-byte cap"
        )
    check_for_secrets(encoded, context="experiment run resource_profile")
    return value


def validate_result_receipt(value: object) -> "dict[str, Any] | None":
    """Validate the bounded result receipt (32KB cap; REJECTS, never
    truncates -- matches meridian.research_run.validate_result_receipt's own
    established convention for exactly this situation)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ExperimentError("result_receipt must be an object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ExperimentError("result_receipt must be JSON serializable") from exc
    size = len(encoded.encode("utf-8"))
    if size > MAX_RESULT_RECEIPT_BYTES:
        raise ExperimentError(
            f"result_receipt exceeds the {MAX_RESULT_RECEIPT_BYTES}-byte cap "
            f"({size} bytes) -- trim it before completing this run"
        )
    check_for_secrets(encoded, context="experiment run result_receipt")
    return value


def validate_ttl_seconds(value: object) -> "int | None":
    """Validate the run's optional time-to-live in seconds. Unlike
    meridian.research_run's bounded-ephemeral-probe TTL (always defaulted),
    an experiment run has no forced default here -- omitting ttl_seconds
    means the run never auto-expires via expire_stale_runs."""
    if value is None:
        return None
    try:
        ttl = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ExperimentError("ttl_seconds must be an integer") from None
    if isinstance(value, bool):
        raise ExperimentError("ttl_seconds must be an integer, not a bool")
    if ttl < MIN_TTL_SECONDS or ttl > MAX_TTL_SECONDS:
        raise ExperimentError(
            f"ttl_seconds must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS}, got {ttl}"
        )
    return ttl


def validate_run_fields(
    *,
    trial_label: object = None,
    resource_profile: object = None,
    ttl_seconds: object = None,
) -> "dict[str, Any]":
    """Validate the field set accepted by start_experiment_run beyond
    identity (experiment_id/repository_id/worktree_id/pivot_parent_run_id
    are validated by the persistence layer, which alone knows how to look
    them up)."""
    return {
        "trial_label": validate_trial_label(trial_label),
        "resource_profile": validate_resource_profile(resource_profile),
        "ttl_seconds": validate_ttl_seconds(ttl_seconds),
    }


def compute_expires_at(started_at_iso: str, ttl_seconds: "int | None") -> "str | None":
    """Return ``started_at_iso + ttl_seconds`` as an ISO8601 UTC string, or
    ``None`` when ``ttl_seconds`` is ``None`` (the run never auto-expires)."""
    if ttl_seconds is None:
        return None
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


def is_dead_end_outcome(status: str, outcome_summary: "str | None") -> bool:
    """True when a completed run's terminal state should auto-record a
    dead_end event: an explicit 'abandoned' status, or the outcome_summary
    text itself flags failure via a case-insensitive 'dead end'/'failed'
    substring match."""
    if status == "abandoned":
        return True
    text = (outcome_summary or "").lower()
    return "dead end" in text or "failed" in text
