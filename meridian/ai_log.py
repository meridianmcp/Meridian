"""9e83be4a (Round 1 proposal e143949d) — the canonical, versioned,
append-only ExecutionEvent contract.

SCOPE (read this before extending this module)
------------------------------------------------
This is a SCHEMA/CONTRACT definition, not a feature. It defines:

  1. The ``ExecutionEvent`` envelope — the fixed set of fields every event
     Meridian ever records about "what an executor/session/tool DID" must
     carry, regardless of what kind of event it is.
  2. A two-axis versioning scheme (envelope ``schema_version`` vs. per-event-
     type ``payload_schema``) so the contract can evolve without a global
     migration on every change (see "VERSIONING" below).
  3. Validation + canonical hashing for one event.
  4. A minimal, storage-agnostic upgrade-path *mechanism* (empty registry
     today) so a future breaking envelope change has somewhere to plug in.

Deliberately OUT OF SCOPE for this item (9e83be4a), left for sibling items:

  * No capture/ingestion pipeline. Nothing in the running server calls
    :func:`meridian.db.ai_log.append_event` today — there is no hook in
    ``server.py``, no MCP tool, no automatic capture of tool calls, sprint-
    item transitions, or LLM turns. That wiring (what triggers an event,
    where in the request lifecycle, sampling/batching, backpressure) is a
    separate, larger design decision explicitly deferred per the Round 1
    proposal note ("schema-first investigation only... No implementation...
    implied").
  * No test/failure-matrix, capture-boundary enumeration, or export format —
    that is sibling item 0f18eb77's job
    (``tests/test_ai_log_capture_boundaries.py`` /
    ``tests/test_ai_log_export.py``). This module and its tests
    (``tests/test_ai_log_contract.py``) cover the CONTRACT only: schema
    shape, validation, versioning/compatibility, and that the storage
    scaffold's migration applies cleanly on both backends.
  * No Langfuse (or any other external sink) dependency or adapter.

WHY AN ENVELOPE, SEPARATE FROM PAYLOAD
---------------------------------------
Every event, no matter what happened, needs the same answers: what kind of
thing is this, when did it happen, which project/session does it belong to,
who/what did it, and how does it relate to other events. That's the
envelope. What ACTUALLY happened — a tool's arguments and result, an LLM
request/response, a sprint-item transition's before/after state — differs
per ``event_type`` and belongs in ``payload``, an open JSON object this
module intentionally does not constrain beyond "must be a dict".

ENVELOPE FIELDS
----------------
  schema_version   — int. The envelope SHAPE this event was written under.
                      See VERSIONING below. Always set to
                      :data:`EVENT_SCHEMA_VERSION` for a newly-constructed
                      event; a stored historical event keeps whatever
                      version it was written with, forever (append-only —
                      never rewritten in place).
  event_id         — str (UUID4). Identity, not content — excluded from
                      :func:`canonical_event_hash`.
  event_type       — str. Dotted, lower_snake namespacing, e.g.
                      ``"tool.invoked"``, ``"tool.completed"``,
                      ``"llm.request"``, ``"llm.response"``,
                      ``"session.started"``, ``"sprint_item.completed"``,
                      ``"error.raised"``. Deliberately an OPEN taxonomy (no
                      closed enum, no DB CHECK constraint) — new event
                      categories must never require a schema migration — but
                      validated to be non-empty and namespaced
                      (:func:`validate_event_type`) so the taxonomy stays
                      disciplined instead of accepting arbitrary strings.
  project_id       — str. Required. Every event is scoped to a project —
                      mirrors every other durable record in this codebase
                      (sprint_items, task_log, action_audit_log, ...).
  session_id       — str | None. Optional session scoping — many events
                      (e.g. a background job) have no session.
  tenant_id        — str | None. Hosted-tier workspace scope (mirrors
                      ``action_audit_log.tenant_id``); NULL on self-host.
  actor_kind       — str. One of :data:`ACTOR_KINDS` — WHAT KIND of thing
                      produced this event (a session, the system itself, a
                      human, a tool, a model). Closed set: unlike
                      ``event_type``, "what kind of actor can do something"
                      is a small, stable taxonomy worth enforcing at the
                      contract layer.
  actor_id         — str | None. Free-text identity of the specific actor
                      (a session id, a tool name, a human id, a model id).
  correlation_id   — str | None. Groups events belonging to one logical
                      operation (e.g. one sprint-item execution, one tool
                      round-trip) — distinct from ``session_id``, which
                      spans many correlated operations over a session's
                      lifetime.
  parent_event_id  — str | None. Optional link to a causally-prior event
                      (e.g. a ``tool.completed`` event's parent is the
                      ``tool.invoked`` event that started it) — an
                      append-only causal chain, never a pointer used to
                      mutate the parent (mirrors
                      ``executor_reports.parent_report_id`` /
                      ``handoff_corrections.source_handoff_id``'s own
                      non-destructive-lineage discipline).
  source           — str | None. Free text: what subsystem/interface
                      produced this event (e.g. ``"mcp"``, ``"dashboard"``,
                      ``"cli"``, ``"tunnel"``).
  occurred_at      — str. ISO-8601 UTC timestamp (``...Z``) of when the
                      event actually happened at its source. Caller-supplied
                      when known (e.g. replayed/batched ingestion); defaults
                      to "now" at construction time otherwise. Distinct from
                      the storage layer's ``recorded_at`` (see
                      ``db.ai_log`` — when the row was durably appended),
                      which this module's envelope does NOT carry: that is a
                      storage-assigned fact about the row, not part of the
                      portable event contract.
  payload          — dict[str, Any]. Event-type-specific structured data.
                      NOT validated beyond "must be a dict" — see
                      ``payload_schema``.
  payload_schema   — str | None. Identifies the SHAPE of ``payload``,
                      independent of ``schema_version`` — e.g.
                      ``"tool_call@1"``. This is the second versioning axis;
                      see VERSIONING below. Strongly recommended whenever a
                      payload has a shape worth naming, but not enforced
                      (some events legitimately carry no structured payload
                      at all, e.g. a bare ``"session.started"`` marker).
  idempotency_key  — str | None. Optional caller-supplied dedup key,
                      mirroring ``executor_reports.idempotency_key`` — lets
                      a retried append after a network blip return the
                      existing row instead of duplicating it. Scoped
                      ``(project_id, idempotency_key)`` at the storage layer.

VERSIONING — TWO INDEPENDENT AXES
-----------------------------------
1. ``schema_version`` (this module's :data:`EVENT_SCHEMA_VERSION`) governs
   the ENVELOPE shape above — the fields every event has, regardless of
   type. Bump it ONLY for a breaking envelope change (a required field
   renamed, removed, or retyped; or a change in what "required" means for an
   existing field). Adding a new OPTIONAL envelope field is NEVER a breaking
   change and must NOT bump this — a reader must always tolerate an unknown
   key it doesn't recognise (forward compatibility) and a missing OPTIONAL
   key it does recognise (backward compatibility). This is additive-only
   evolution within one ``schema_version``.

2. ``payload_schema`` governs the shape of ONE event_type's ``payload``,
   completely independently of the envelope. A specific event type's
   payload can iterate (``"tool_call@1"`` -> ``"tool_call@2"``) without ever
   touching :data:`EVENT_SCHEMA_VERSION` — this is what lets many
   independent event-type payload shapes evolve at their own pace instead of
   forcing a global envelope migration for every one of them. This module
   does not (and, by design, cannot) validate arbitrary payload shapes; a
   consumer that cares about a specific ``payload_schema`` is responsible
   for its own decode/validation, keyed on that tag.

An event's stored ``schema_version`` is IMMUTABLE once written (append-only
— the storage layer never rewrites a row's envelope in place). A reader
decoding historical events must dispatch on the PER-ROW ``schema_version``,
not assume every row in the table matches the module's current
:data:`EVENT_SCHEMA_VERSION`. :func:`upgrade_event_dict` is the (currently
empty, since only version 1 exists) extension point for that dispatch — see
its docstring.

COMPATIBILITY / MIGRATION NOTES FOR A FUTURE BREAKING CHANGE
----------------------------------------------------------------
When a genuinely breaking envelope change is needed:

  1. Bump :data:`EVENT_SCHEMA_VERSION`.
  2. Register an upgrader via :func:`register_schema_upgrader` that maps an
     event dict AT the OLD version to the NEW version's shape (pure
     function, no I/O — it never touches the DB; storage rows are never
     rewritten, only read-time-normalized by :func:`upgrade_event_dict`).
  3. Keep :data:`MIN_SUPPORTED_SCHEMA_VERSION` at the oldest version a
     registered upgrade chain can still reach the current version from. A
     stored row older than that can still be read RAW (nothing deletes it —
     append-only), but :func:`upgrade_event_dict` refuses to silently guess
     at its shape and raises :class:`UnsupportedSchemaVersionError` instead.
  4. Add the corresponding storage-side ``ALTER TABLE`` as a new guarded
     migration in ``meridian/db/ai_log.py`` (SQLite) AND
     ``meridian/pg_adapter.py`` (Postgres) — see that module's own docstring
     for this repo's dual-backend migration convention. Never an inline
     index on the new column in an unguarded base schema literal (the
     2026-06-13 / 2026-07-04 outage class documented in AGENTS.md).
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

#: Current envelope schema version this module writes. See the module
#: docstring's VERSIONING section for what does/doesn't warrant a bump.
EVENT_SCHEMA_VERSION = 1

#: Oldest envelope schema_version a reader in this codebase is still
#: expected to be able to normalize up to EVENT_SCHEMA_VERSION via
#: upgrade_event_dict. A row older than this is still readable RAW (it is
#: never deleted — append-only) but upgrade_event_dict refuses to guess at
#: its shape.
MIN_SUPPORTED_SCHEMA_VERSION = 1

#: The closed set of actor kinds — WHAT KIND of thing produced an event.
#: Unlike event_type (an open, namespaced taxonomy), this is intentionally
#: small and enforced: every event's actor is exactly one of these.
ACTOR_KINDS: frozenset[str] = frozenset({"session", "system", "human", "tool", "model"})

#: event_type must be dotted, lower_snake namespacing with at least one dot
#: — e.g. "tool.invoked", "sprint_item.completed". Keeps the open taxonomy
#: disciplined without hard-coding a closed list.
_EVENT_TYPE_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")

#: Fields hashed by canonical_event_hash — CONTENT only, excluding identity
#: (event_id, a random UUID) and nothing else, since (unlike
#: executor_reports) an ExecutionEvent has no separate bookkeeping/status
#: fields layered on top of its content. Two independently-constructed
#: events with byte-identical content hash identically regardless of their
#: (random) event_id.
_HASHED_FIELDS: tuple[str, ...] = (
    "schema_version",
    "event_type",
    "project_id",
    "session_id",
    "tenant_id",
    "actor_kind",
    "actor_id",
    "correlation_id",
    "parent_event_id",
    "source",
    "occurred_at",
    "payload",
    "payload_schema",
)


class ExecutionEventError(ValueError):
    """Raised for a structurally invalid ExecutionEvent."""


class UnsupportedSchemaVersionError(ExecutionEventError):
    """Raised by :func:`upgrade_event_dict` when a stored event's
    schema_version is outside [MIN_SUPPORTED_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION], or inside that range but no registered upgrader
    bridges it up to EVENT_SCHEMA_VERSION."""


def _utc_now_iso() -> str:
    """Millisecond-precision UTC ISO-8601 with a literal 'Z' suffix."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def validate_event_type(event_type: Any) -> str:
    """Validate + return ``event_type``. Raises :class:`ExecutionEventError`
    for anything that isn't a non-empty, dotted lower_snake string."""
    if not isinstance(event_type, str) or not event_type:
        raise ExecutionEventError("event_type must be a non-empty string")
    if not _EVENT_TYPE_RE.match(event_type):
        raise ExecutionEventError(
            f"event_type {event_type!r} must be dotted lower_snake namespacing "
            "with at least one dot, e.g. 'tool.invoked', 'session.started', "
            "'llm.response'"
        )
    return event_type


def validate_actor_kind(actor_kind: Any) -> str:
    """Validate + return ``actor_kind``. Raises :class:`ExecutionEventError`
    if it is not one of :data:`ACTOR_KINDS`."""
    if actor_kind not in ACTOR_KINDS:
        raise ExecutionEventError(
            f"actor_kind {actor_kind!r} must be one of {sorted(ACTOR_KINDS)}"
        )
    return actor_kind


@dataclass(frozen=True)
class ExecutionEvent:
    """The canonical, immutable-once-constructed ExecutionEvent envelope.

    See the module docstring for the full field-by-field rationale. Field
    order below intentionally places every REQUIRED field first (no
    default), then optional/defaulted fields — a caller constructs one with
    ``ExecutionEvent(project_id=..., event_type=..., actor_kind=...)`` at
    minimum.

    Validated in ``__post_init__`` — constructing an invalid event raises
    :class:`ExecutionEventError` immediately rather than deferring the
    problem to a later serialize/store call.
    """

    project_id: str
    event_type: str
    actor_kind: str
    actor_id: "str | None" = None
    session_id: "str | None" = None
    tenant_id: "str | None" = None
    correlation_id: "str | None" = None
    parent_event_id: "str | None" = None
    source: "str | None" = None
    payload: dict[str, Any] = field(default_factory=dict)
    payload_schema: "str | None" = None
    idempotency_key: "str | None" = None
    occurred_at: "str | None" = None
    schema_version: int = EVENT_SCHEMA_VERSION
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        if not self.project_id or not isinstance(self.project_id, str):
            raise ExecutionEventError("project_id is required and must be a non-empty string")
        validate_event_type(self.event_type)
        validate_actor_kind(self.actor_kind)
        if not isinstance(self.payload, dict):
            raise ExecutionEventError("payload must be a dict")
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool):
            raise ExecutionEventError("schema_version must be an int")
        if self.schema_version < MIN_SUPPORTED_SCHEMA_VERSION or self.schema_version > EVENT_SCHEMA_VERSION:
            raise ExecutionEventError(
                f"schema_version {self.schema_version} outside supported range "
                f"[{MIN_SUPPORTED_SCHEMA_VERSION}, {EVENT_SCHEMA_VERSION}]"
            )
        if self.occurred_at is None:
            # frozen dataclass — object.__setattr__ is the documented escape
            # hatch for filling in a computed default in __post_init__.
            object.__setattr__(self, "occurred_at", _utc_now_iso())
        elif not isinstance(self.occurred_at, str) or not self.occurred_at:
            raise ExecutionEventError("occurred_at must be a non-empty ISO-8601 string")

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe plain dict of every field (identity + content)."""
        return asdict(self)

    def content_hash(self) -> str:
        """``sha256:<hex>`` over this event's CONTENT fields only (excludes
        ``event_id`` — see :data:`_HASHED_FIELDS`). Two independently-
        constructed events with byte-identical content hash identically,
        regardless of their random event_id — usable for content-based
        dedup independent of the optional ``idempotency_key`` mechanism."""
        return canonical_event_hash(self.to_dict())


def canonical_event_hash(event: dict[str, Any]) -> str:
    """Deterministic ``sha256:<hex>`` over an event dict's CONTENT fields
    (:data:`_HASHED_FIELDS`) — mirrors
    ``db.executor_reports.canonical_report_hash``'s "content, not identity"
    discipline. Works on any dict with at least the hashed keys present
    (e.g. a row freshly read back from storage), not just
    ``ExecutionEvent.to_dict()`` output."""
    tracked = {k: event.get(k) for k in _HASHED_FIELDS}
    canonical = json.dumps(tracked, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Forward/backward compatibility mechanism (extension point — empty today)
# ---------------------------------------------------------------------------

#: Maps a schema_version N to a pure function upgrading an event dict AT
#: version N to version N+1's shape. Empty today because
#: EVENT_SCHEMA_VERSION is still 1 — there is no prior version to upgrade
#: FROM yet. This registry is for ENVELOPE changes only; a payload_schema
#: change is self-describing via the payload_schema tag and never routed
#: through this mechanism (see the module docstring's VERSIONING section).
_SCHEMA_UPGRADERS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def register_schema_upgrader(
    from_version: int, upgrader: Callable[[dict[str, Any]], dict[str, Any]]
) -> None:
    """Register a pure ``upgrader(event_dict_at_from_version) -> event_dict_at_from_version+1``
    function. Raises :class:`ExecutionEventError` if a second upgrader is
    registered for the same ``from_version`` (never silently overwrite an
    existing upgrade path)."""
    if from_version in _SCHEMA_UPGRADERS:
        raise ExecutionEventError(
            f"an upgrader for schema_version {from_version} is already registered"
        )
    _SCHEMA_UPGRADERS[from_version] = upgrader


def upgrade_event_dict(event: dict[str, Any]) -> dict[str, Any]:
    """Return a NEW dict normalizing ``event`` up to
    :data:`EVENT_SCHEMA_VERSION`'s shape, by applying every registered
    upgrader in sequence starting from the event's own stored
    ``schema_version``. Never mutates ``event`` in place, and never touches
    storage — this is an in-memory, READ-time normalization only; a stored
    row's own ``schema_version`` column is never rewritten (append-only).

    Raises :class:`UnsupportedSchemaVersionError` when:
      * ``event["schema_version"]`` is missing or not an int.
      * it is below :data:`MIN_SUPPORTED_SCHEMA_VERSION` — too old to trust
        an automatic upgrade for.
      * it is above :data:`EVENT_SCHEMA_VERSION` — from a NEWER writer than
        this reader understands; never guess forward.
      * it is in-range but the upgrade chain to EVENT_SCHEMA_VERSION is
        incomplete (a registered upgrader is missing for some intermediate
        version) — never silently returns a partially-upgraded event.

    A row already AT ``EVENT_SCHEMA_VERSION`` is returned as a shallow copy,
    unchanged — the common case today, since only version 1 exists.
    """
    version = event.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ExecutionEventError("event is missing an integer schema_version")
    if version < MIN_SUPPORTED_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"schema_version {version} predates the oldest supported version "
            f"{MIN_SUPPORTED_SCHEMA_VERSION} — no automatic upgrade is attempted"
        )
    if version > EVENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"schema_version {version} is newer than this reader's "
            f"EVENT_SCHEMA_VERSION {EVENT_SCHEMA_VERSION} — refusing to guess "
            "at an unknown future shape"
        )
    current = dict(event)
    v = version
    while v < EVENT_SCHEMA_VERSION:
        upgrader = _SCHEMA_UPGRADERS.get(v)
        if upgrader is None:
            raise UnsupportedSchemaVersionError(
                f"no registered upgrader bridges schema_version {v} to {v + 1} "
                f"(needed to reach EVENT_SCHEMA_VERSION {EVENT_SCHEMA_VERSION})"
            )
        current = upgrader(current)
        v += 1
    return current
