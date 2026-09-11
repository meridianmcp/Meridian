"""Provider-neutral cross-client session recovery registry (RESCUE-D).

Investigation trigger: a Claude Remote Control (RC) session surfaced a
``cse_...`` bridge id with no ``environment_id``, and
``remote-control --session-id <bridge_id>`` rejected it outright. The bridge
id alone was never resumable -- it was one piece of a larger identity that
also needs the RC environment id to reconstruct a working resume command.

Meridian juggles (at least) FOUR independent identity spaces for what a human
casually calls "the session":

  1. **Local transcript / session id** -- the on-disk conversation id a CLI
     client (Claude Code, Cursor, ...) uses for ``--resume``.
  2. **RC bridge id** -- an opaque id Remote Control assigns a *bridge*
     between a phone/browser and a local session. NOT itself sufficient to
     resume (see above).
  3. **Environment id** -- the sandbox/environment identifier RC (or a cloud
     runner) needs alongside a bridge/session id to actually reattach.
  4. **Meridian session id** -- ``sessions.id``, already tracked by
     :func:`meridian.db.register_session`. Provider-agnostic and durable, but
     says nothing about whether the ORIGINATING client process is still
     alive or how to reattach to it.

This module keeps those four identity spaces separate on purpose, and draws
a hard line down the middle of the record that describes them:

  * **host-local sensitive references** -- local transcript ids, RC bridge
    ids, environment ids, and OS/client argv/paths. These NEVER leave the
    machine that registered them. They live only in a local JSON snapshot
    (:func:`write_local_recovery_snapshot` / :func:`read_local_recovery_snapshot`),
    the same "durable local cache, not a second authority" pattern
    :mod:`meridian.external_job_register` already established for external
    jobs.
  * **redacted hosted metadata** -- status, heartbeat, last checkpoint/handoff
    *reference* (an id/label, never content), project/version, transport
    kind, and a plain ``verified_resumable`` boolean. Safe to persist in
    Meridian's shared project DB (``meridian.db.session_recovery``) because
    every string that reaches this side is run through
    :func:`meridian.secret_redaction.check_for_secrets` plus the same
    machine-local-absolute-path / secret-shaped-value rejection
    :mod:`meridian.capability_manifest` already uses for manifest state
    (``_ABSOLUTE_PATH_RE`` / ``_SECRET_LIKE_RE``, reused here rather than
    reimplemented).

Nothing here re-renders handoff content. Recovery continuation
(:func:`meridian.db.session_recovery.build_recovery_continuation`) explicitly
re-derives the LIVE board via ``meridian.db.board_snapshot.build_board_snapshot``
(the same canonical snapshot ``meridian.handoff.build_continuation_manifest``
already wraps for 862f6522) rather than replaying a stale ``/goal`` body, and
surfaces the resuming session's own still-active file/symbol claims so a
recovering agent can see what it already owns instead of reclaiming or
force-completing it. See that module's docstring for the DB-facing half of
this feature; this module is pure validation/classification/local-snapshot
logic -- no DB, no network.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from meridian.capability_manifest import _ABSOLUTE_PATH_RE, _SECRET_LIKE_RE
from meridian.external_job_register import utcnow_iso  # noqa: F401 -- re-exported

__all__ = [
    "utcnow_iso",
    "TRANSPORTS",
    "LIFECYCLE_STATUSES",
    "LIVENESS_CLASSIFICATIONS",
    "DEFAULT_STALE_AFTER_SECONDS",
    "DEFAULT_DEAD_AFTER_SECONDS",
    "SNAPSHOT_SCHEMA_VERSION",
    "LOCAL_ONLY_IDENTITY_KEYS",
    "validate_transport",
    "validate_lifecycle_status",
    "validate_hosted_text",
    "validate_hosted_metadata",
    "reject_local_only_keys",
    "classify_liveness",
    "build_resume_recipe",
    "session_recovery_snapshot_path",
    "write_local_recovery_snapshot",
    "read_local_recovery_snapshot",
    "build_log_description",
]

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Provider-neutral transport kinds. "remote_control" is Claude Code RC
# (phone/browser <-> bridge <-> local session); "cloud_environment" is a
# hosted/cloud sandbox session with no local process at all; "tunnel" is
# Meridian's own tunnel_client.py reconnect loop (reconnect-based, not
# resume-based -- see build_resume_recipe).
TRANSPORTS = frozenset({
    "stdio", "remote_control", "cloud_environment", "tunnel", "unknown",
})

# Self-reported lifecycle status, mirroring sessions.status's existing
# vocabulary (active/idle/...) plus two terminal states a client can report
# about ITSELF that 'closed'/'archived' don't quite capture: 'ended' (a
# graceful stop -- checkpoint/handoff already written) and 'crashed' (the
# client detected its own abnormal exit, e.g. on next launch).
LIFECYCLE_STATUSES = frozenset({"active", "idle", "ended", "crashed", "unknown"})

# Computed (never self-reported) liveness classification -- see classify_liveness.
LIVENESS_CLASSIFICATIONS = frozenset({"resumable", "stale", "dead", "unknown"})

DEFAULT_STALE_AFTER_SECONDS = 15 * 60       # 15 minutes with no heartbeat
DEFAULT_DEAD_AFTER_SECONDS = 6 * 60 * 60    # 6 hours with no heartbeat

SNAPSHOT_SCHEMA_VERSION = 1
MAX_METADATA_BYTES = 20_000

# Identity fields that must NEVER cross into hosted metadata -- these are
# exactly the host-local sensitive references the sprint item calls out by
# name (bridge id, local transcript path, OS argv) plus the environment id
# (RESCUE-D: pairs with a bridge id to reconstruct a resume command, so it
# gets the same treatment). Checked defensively in addition to the DB layer
# simply never declaring columns for them.
LOCAL_ONLY_IDENTITY_KEYS = frozenset({
    "local_session_id", "bridge_id", "environment_id",
    "local_transcript_path", "argv", "host_argv",
})

_LOCAL_PATH_RE = re.compile(r"(?:^|[\s=(])(?:[A-Za-z]:[\\/]|\\\\|/(?!/))")

_SNAPSHOT_LOCK = threading.Lock()


class SessionRecoveryError(ValueError):
    """Raised when a session-recovery field fails schema or safety validation."""


# ---------------------------------------------------------------------------
# Validation (hosted-safe side)
# ---------------------------------------------------------------------------

def validate_transport(value: object) -> str:
    transport = value.strip().lower() if isinstance(value, str) else ""
    if transport not in TRANSPORTS:
        raise SessionRecoveryError(
            f"transport must be one of {sorted(TRANSPORTS)}, got {value!r}"
        )
    return transport


def validate_lifecycle_status(value: object) -> str:
    if value is None:
        return "active"
    status = value.strip().lower() if isinstance(value, str) else ""
    if status not in LIFECYCLE_STATUSES:
        raise SessionRecoveryError(
            f"lifecycle_status must be one of {sorted(LIFECYCLE_STATUSES)}, got {value!r}"
        )
    return status


def validate_hosted_text(
    value: object,
    *,
    field: str,
    required: bool = False,
    max_chars: int = 2_000,
) -> str | None:
    """Validate one string destined for the HOSTED (shared, multi-machine) side.

    Always rejects secret-shaped values, machine-local absolute paths, and
    anything matching :mod:`meridian.secret_redaction`'s pattern registry --
    unlike :mod:`meridian.external_job_register`'s ``_validate_text``, this
    has no ``reject_local_path=False`` escape hatch: EVERY string reaching
    this function is, by construction, about to enter the shared hosted
    table, so the path/secret check is unconditional.
    """
    if value is None:
        if required:
            raise SessionRecoveryError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise SessionRecoveryError(f"{field} must be a string")
    text = value.strip()
    if required and not text:
        raise SessionRecoveryError(f"{field} is required")
    if len(text) > max_chars:
        raise SessionRecoveryError(f"{field} exceeds the {max_chars}-character limit")
    if not text:
        return None
    from meridian.secret_redaction import check_for_secrets  # noqa: PLC0415
    try:
        check_for_secrets(text, context=f"session recovery {field}")
    except ValueError as exc:
        raise SessionRecoveryError(str(exc)) from exc
    if _ABSOLUTE_PATH_RE.search(text) or _LOCAL_PATH_RE.search(text):
        raise SessionRecoveryError(
            f"Refusing to persist session recovery {field}: machine-local absolute "
            "paths must stay in the host-local snapshot, never in hosted metadata"
        )
    if _SECRET_LIKE_RE.search(text):
        raise SessionRecoveryError(
            f"Refusing to persist session recovery {field}: secret-shaped value"
        )
    return text or None


def reject_local_only_keys(payload: dict[str, Any], *, context: str) -> None:
    """Defense in depth: raise if any known host-local identity key appears
    anywhere in a dict about to be persisted hosted-side (e.g. inside a
    caller-supplied ``metadata`` object). The DB layer never declares columns
    for these fields at all -- this catches a caller trying to smuggle one
    into the free-form metadata blob instead.
    """
    def visit(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, sub in node.items():
                if isinstance(key, str) and key.lower() in LOCAL_ONLY_IDENTITY_KEYS:
                    raise SessionRecoveryError(
                        f"Refusing to persist {context}: key {key!r} at {path} is a "
                        "host-local sensitive identity field (bridge id / environment "
                        "id / local transcript path / argv) and must never enter "
                        "hosted metadata"
                    )
                visit(sub, f"{path}.{key}")
        elif isinstance(node, (list, tuple)):
            for idx, sub in enumerate(node):
                visit(sub, f"{path}[{idx}]")

    visit(payload, context)


def validate_hosted_metadata(value: object) -> dict[str, Any]:
    """Validate a free-form JSON metadata object bound for the hosted table.

    Mirrors ``external_job_register.validate_metadata`` (recurse, check every
    string for secrets/local paths, bound the encoded size) plus the
    session-recovery-specific :func:`reject_local_only_keys` guard.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SessionRecoveryError("session recovery metadata must be an object")

    reject_local_only_keys(value, context="session recovery metadata")

    def visit(node: Any, path: str = "metadata") -> None:
        if isinstance(node, str):
            validate_hosted_text(node, field=path, max_chars=4_000)
        elif isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str):
                    raise SessionRecoveryError("session recovery metadata keys must be strings")
                visit(child, f"{path}.{key}")
        elif isinstance(node, (list, tuple)):
            for idx, child in enumerate(node):
                visit(child, f"{path}[{idx}]")
        elif node is not None and not isinstance(node, (bool, int, float)):
            raise SessionRecoveryError(f"session recovery {path} contains a non-JSON value")

    visit(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise SessionRecoveryError("session recovery metadata must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise SessionRecoveryError(f"session recovery metadata exceeds {MAX_METADATA_BYTES} bytes")
    return dict(value)


# ---------------------------------------------------------------------------
# Liveness classification (stale / dead / unknown)
# ---------------------------------------------------------------------------

def classify_liveness(
    last_heartbeat_at: str | None,
    lifecycle_status: str | None,
    *,
    now: "Any | None" = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    dead_after_seconds: int = DEFAULT_DEAD_AFTER_SECONDS,
) -> str:
    """Classify one recovery row as resumable / stale / dead / unknown.

    A self-reported ``crashed`` status is always ``dead`` regardless of how
    recent the last heartbeat was -- the client itself is the authority on
    "I detected my own abnormal exit". ``ended`` (a graceful stop with a
    checkpoint/handoff already written) is also ``dead`` for THIS row's
    liveness purposes: the originating process is gone on purpose, so there
    is nothing live to reattach to, even though the underlying sprint work
    is perfectly resumable via the ordinary board (never a reason to
    force-complete or duplicate anything -- see
    ``meridian.db.session_recovery.build_recovery_continuation``, which
    re-derives the board regardless of this classification).

    No parsable heartbeat timestamp means ``unknown`` -- never guessed as
    either resumable or dead.
    """
    from datetime import datetime, timezone  # local import: keep module import-light

    status = (lifecycle_status or "unknown").strip().lower()
    if status == "crashed":
        return "dead"
    if status == "ended":
        return "dead"
    if not last_heartbeat_at:
        return "unknown"
    try:
        text = last_heartbeat_at.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        heartbeat_dt = datetime.fromisoformat(text)
    except (ValueError, AttributeError):
        return "unknown"
    if heartbeat_dt.tzinfo is None:
        heartbeat_dt = heartbeat_dt.replace(tzinfo=timezone.utc)
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    elapsed = (now_dt - heartbeat_dt).total_seconds()
    if elapsed < 0:
        elapsed = 0.0
    if elapsed >= dead_after_seconds:
        return "dead"
    if elapsed >= stale_after_seconds:
        return "stale"
    return "resumable"


# ---------------------------------------------------------------------------
# Resume recipes -- exact OS/client argv recipes, built ONLY from host-local
# identity that never touches the hosted DB (callers pass identity in,
# nothing here reads or writes any shared state).
# ---------------------------------------------------------------------------

# Templates keyed by transport; {placeholders} are filled from the caller's
# OWN local identity dict, never from anything persisted hosted-side.
_RESUME_RECIPE_TEMPLATES: dict[str, str] = {
    "stdio": "claude --resume {local_session_id}",
    "remote_control": "claude --resume {local_session_id} --environment-id {environment_id}",
    "cloud_environment": "claude --environment-id {environment_id} --resume {local_session_id}",
}


def build_resume_recipe(
    transport: str,
    client_type: str | None,
    identity: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    """Return ``(recipe, blocked_reason)`` -- exactly one is non-``None``.

    ``identity`` may carry ``local_session_id`` / ``bridge_id`` /
    ``environment_id`` / ``argv`` -- all host-local values the CALLER already
    holds (this function never fetches or persists them). RESCUE-D's core
    lesson is enforced here as code, not just prose: a ``remote_control``
    transport REQUIRES ``environment_id`` -- a bridge id alone (the exact
    shape of the observed failure: a ``cse_...`` id with no environment_id)
    is explicitly rejected with a reason naming the gap, never silently
    treated as resumable.

    A caller-supplied ``argv`` (an explicit, already-correct command list)
    always wins when present and non-empty -- it is joined verbatim into the
    recipe string, since the caller's own client is the ground truth for its
    exact invocation shape better than any generic template here could be.
    """
    identity = identity or {}
    argv = identity.get("argv")
    if isinstance(argv, (list, tuple)) and argv:
        return " ".join(str(part) for part in argv), None

    transport = (transport or "unknown").strip().lower()
    local_session_id = identity.get("local_session_id")
    bridge_id = identity.get("bridge_id")
    environment_id = identity.get("environment_id")

    if transport == "stdio":
        if not local_session_id:
            return None, "missing local transcript/session id -- nothing to --resume"
        return _RESUME_RECIPE_TEMPLATES["stdio"].format(local_session_id=local_session_id), None

    if transport == "remote_control":
        # RESCUE-D: do NOT assume a bridge id is resumable on its own.
        if not environment_id:
            return None, (
                "remote_control resume requires environment_id; a bridge id "
                f"({bridge_id!r}) alone was rejected by --session-id in the "
                "RESCUE-D incident and is not assumed resumable here either"
            )
        if not local_session_id and not bridge_id:
            return None, "missing both local_session_id and bridge_id"
        return (
            _RESUME_RECIPE_TEMPLATES["remote_control"].format(
                local_session_id=local_session_id or bridge_id,
                environment_id=environment_id,
            ),
            None,
        )

    if transport == "cloud_environment":
        if not environment_id:
            return None, "cloud_environment resume requires environment_id"
        if not local_session_id:
            return None, "missing local_session_id for the cloud environment"
        return (
            _RESUME_RECIPE_TEMPLATES["cloud_environment"].format(
                local_session_id=local_session_id, environment_id=environment_id,
            ),
            None,
        )

    if transport == "tunnel":
        return None, (
            "tunnel transport is reconnect-based (see tunnel_client._reconnect_loop), "
            "not resume-based -- there is no argv recipe to hand back"
        )

    return None, f"unknown transport {transport!r}: no known resume recipe"


# ---------------------------------------------------------------------------
# Host-local snapshot -- the ONLY place bridge_id / environment_id /
# local_transcript_path / argv are ever written to disk. Same
# write-atomically-with-a-tempfile-then-replace technique as
# meridian.external_job_register.write_local_status_snapshot.
# ---------------------------------------------------------------------------

def _safe_component(value: str) -> str:
    component = re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip())
    return component[:160] or "project"


def session_recovery_snapshot_path(data_dir: str | os.PathLike[str], project_id: str) -> Path:
    return Path(data_dir) / "session_recovery" / f"{_safe_component(project_id)}.json"


def write_local_recovery_snapshot(
    data_dir: str | os.PathLike[str],
    project_id: str,
    records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Atomically write the local-only identity map for one project.

    ``records`` is keyed by ``local_ref_id`` (the opaque token also stored,
    meaninglessly on its own, in the hosted row) -> a dict that MAY contain
    ``local_session_id`` / ``bridge_id`` / ``environment_id`` /
    ``local_transcript_path`` / ``argv`` and a resolved ``resume_recipe`` /
    ``resume_blocked_reason``. Never validated against the hosted-safe
    rules above -- this file is explicitly the place those values are
    ALLOWED to live.
    """
    path = session_recovery_snapshot_path(data_dir, project_id)
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "project_id": project_id,
        "generated_at": utcnow_iso(),
        "records": records,
    }
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with _SNAPSHOT_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, path)
    except (OSError, TypeError, ValueError) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False, "path": str(path), "error": str(exc)}
    return {
        "ok": True, "path": str(path), "record_count": len(records),
        "generated_at": payload["generated_at"],
    }


def read_local_recovery_snapshot(
    data_dir: str | os.PathLike[str], project_id: str
) -> dict[str, Any] | None:
    path = session_recovery_snapshot_path(data_dir, project_id)
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("project_id") != project_id:
        return None
    return payload


def new_local_ref_id() -> str:
    """A fresh opaque correlation token -- meaningless without the local
    snapshot file, safe to persist hosted-side (it is not derived from, and
    does not embed, any sensitive identity value)."""
    return uuid.uuid4().hex


def build_log_description(action: str, record: dict[str, Any]) -> str:
    """Render the bounded, secret-checked task-log representation -- HOSTED
    fields only (mirrors external_job_register.build_log_description)."""
    from meridian.secret_redaction import check_for_secrets  # noqa: PLC0415

    state = {
        "recovery_id": record.get("id"),
        "meridian_session_id": record.get("meridian_session_id"),
        "transport": record.get("transport"),
        "client_type": record.get("client_type"),
        "lifecycle_status": record.get("lifecycle_status"),
        "verified_resumable": record.get("verified_resumable"),
        "liveness": record.get("liveness"),
        "sprint_version": record.get("sprint_version"),
        "last_heartbeat_at": record.get("last_heartbeat_at"),
    }
    encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    description = f"Session recovery {action}: {encoded}"
    check_for_secrets(description, context="session recovery task description")
    return description[:9_500]
