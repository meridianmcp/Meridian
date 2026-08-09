"""Authoritative persistence for the PROFILE-1/PROFILE-2 layered profile
contract (62c41508 contract, d8481276 persistence).

Why this exists: 62c41508 defined a 5-layer scope precedence (hosted_default
-> workspace -> user -> project -> session) generalizing the already-shipped
``meridian.capability_profile`` inheritance chain, plus two-counter
versioning, a hosted_default-only lifecycle state machine, and optimistic
concurrency — see ``meridian.profile_contract`` for the pure contract (no DB).
This module is the persistence + resolution layer: two new tables
(``profile_layers``, ``profile_layer_revisions``), CRUD respecting the
contract's validation, and :func:`get_effective_profile`, the DB-aware
wrapper around ``profile_contract.resolve_effective_profile``.

MIGRATION COMPATIBILITY (the actual point of this sprint item): ZERO
duplication of the 7 existing ``ProjectSettings`` fields. Those keep flowing
through the EXISTING ``meridian.db.get_project_settings`` /
``update_project_settings`` + ``projects`` table columns — see
:func:`_legacy_project_settings_to_fields`. A ``profile_layers`` row at
``scope_type="project"`` carries ONLY the 3 genuinely-new PROFILE-1 fields
(``tool_priority_map``, ``capability_manifest_ref``,
``claim_verification_mode``). :func:`get_effective_profile` is the one place
that stitches the two sources back together into a single synthetic
``project`` layer before merging — every other layer (hosted_default,
workspace, user, session) is a plain ``profile_layers`` row, full stop.

Claims, leases, pointers, completion state, and every other source-of-truth
write in this codebase stay exactly where they already are — this module
adds a new, narrowly-scoped persistence surface; it does not touch
``sprint_items``, ``file_locks``/``resource_locks``, or any claim/lease table.

Redis is explicitly OUT OF SCOPE here (62c41508 flagged Redis-cache-layer
budget-sharing as item 3's job, not this one) — every read in this module
hits the authoritative DB directly. See the module docstring's
"NEON VS REDIS BOUNDARY" note in profile_contract.py's source item for the
full design; nothing here special-cases a cache.

Design decision: 62c41508 described the finest layer as "session/run". This
module implements ``scope_type="session"`` only (scope_id = session_id) — a
per-run sub-layer was one of 62c41508's 7 open questions and is not
implemented here. Flagged as a genuine gap in the PROFILE-2 handoff, not
silently dropped.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import aiosqlite

from meridian import profile_contract as _pc

# Available at import time — profile_layers.py is imported at the bottom of
# db/__init__.py, after get_project/get_project_settings are already bound
# onto this module's namespace (see the sprint_items.py / board_snapshot.py
# precedent this mirrors).
from meridian.db import (  # noqa: PLC0415
    _new_id,
    _row_to_dict,
    get_project,
    get_project_settings,
)

#: Fields whose project-scope value flows through the EXISTING
#: get_project_settings authority (see module docstring). Built once from
#: the registry so this module never hard-codes the 6 scalar names twice.
_LEGACY_PROJECT_SETTINGS_FIELDS: tuple[str, ...] = tuple(
    name for name, spec in _pc.FIELD_REGISTRY.items()
    if spec.legacy_source == "project_settings" and not name.startswith("executor_config.")
)
_LEGACY_EXECUTOR_CONFIG_FIELDS: tuple[str, ...] = tuple(
    name.split(".", 1)[1] for name, spec in _pc.FIELD_REGISTRY.items()
    if spec.legacy_source == "project_settings" and name.startswith("executor_config.")
)


def _content_hash(fields: dict[str, Any], reset_fields: list[str]) -> str:
    """Deterministic content hash — ledgers a profile_layers row exactly
    like meridian.db.board_snapshot's revision_hash (canonical JSON, sorted
    keys, sha256). Identical (fields, reset_fields) always hash identically
    regardless of caller's dict key order."""
    canonical = json.dumps(
        {"fields": fields, "reset_fields": sorted(reset_fields)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _empty_layer_dict(scope_type: str, scope_id: str) -> dict[str, Any]:
    return {
        "scope_type": scope_type,
        "scope_id": scope_id,
        "schema_version": _pc.SCHEMA_VERSION,
        "revision": 0,
        "fields": {},
        "reset_fields": [],
        "lifecycle_state": "draft" if scope_type == "hosted_default" else None,
        "content_hash": _content_hash({}, []),
        "provenance": None,
        "updated_at": None,
    }


def _decode_profile_layer_row(row: aiosqlite.Row) -> dict[str, Any]:
    """Decode one ``profile_layers`` row into the dict shape
    :func:`get_profile_layer` returns. Shared by :func:`get_profile_layer`
    and :func:`list_profile_layers` (PROFILE-5, 0bec79a7) so the two never
    drift on JSON-decode logic."""
    data = _row_to_dict(row) or {}
    try:
        fields = json.loads(data.get("fields") or "{}")
    except (TypeError, ValueError):
        fields = {}
    try:
        reset_fields = json.loads(data.get("reset_fields") or "[]")
    except (TypeError, ValueError):
        reset_fields = []
    raw_provenance = data.get("provenance")
    try:
        provenance = json.loads(raw_provenance) if raw_provenance else None
    except (TypeError, ValueError):
        provenance = None
    return {
        "scope_type": data.get("scope_type"),
        "scope_id": data.get("scope_id"),
        "schema_version": int(data.get("schema_version") or _pc.SCHEMA_VERSION),
        "revision": int(data.get("revision") or 0),
        "fields": fields,
        "reset_fields": reset_fields,
        "lifecycle_state": data.get("lifecycle_state"),
        "content_hash": data.get("content_hash"),
        "provenance": provenance,
        "updated_at": data.get("updated_at"),
    }


async def get_profile_layer(db: aiosqlite.Connection, scope_type: str, scope_id: str) -> dict[str, Any]:
    """Return the raw, single-layer profile for one scope (d8481276).

    A scope with no persisted row gets an empty profile back, never an error
    — mirrors get_capability_profile's "never a read error" contract. This
    is one layer only; see :func:`get_effective_profile` for the merged,
    multi-layer view.
    """
    scope_type = _pc.normalize_scope_type(scope_type)
    scope_id = _pc.normalize_scope_id(scope_id)
    async with db.execute(
        "SELECT scope_type, scope_id, schema_version, revision, fields, reset_fields, "
        "lifecycle_state, content_hash, provenance, updated_at FROM profile_layers "
        "WHERE scope_type = ? AND scope_id = ?",
        (scope_type, scope_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return _empty_layer_dict(scope_type, scope_id)
    return _decode_profile_layer_row(row)


async def list_profile_layers(
    db: aiosqlite.Connection, scope_type: str | None = None
) -> list[dict[str, Any]]:
    """Read-only enumeration of every persisted ``profile_layers`` row
    (PROFILE-5, 0bec79a7), optionally narrowed to one ``scope_type`` —
    validated via :func:`profile_contract.normalize_scope_type` the same way
    every other scope_type-accepting function in this module validates it.

    Ordered by ``(scope_type, scope_id)`` for deterministic output — this is
    a raw listing, not a resolved/merged view; see :func:`get_effective_profile`
    for the merged per-project result. Each entry is shaped exactly like
    :func:`get_profile_layer`'s return dict. An empty table (or an empty
    scope_type filter) returns ``[]``, never an error.
    """
    params: tuple[Any, ...] = ()
    where_clause = ""
    if scope_type is not None:
        scope_type = _pc.normalize_scope_type(scope_type)
        where_clause = "WHERE scope_type = ?"
        params = (scope_type,)
    async with db.execute(
        "SELECT scope_type, scope_id, schema_version, revision, fields, reset_fields, "
        "lifecycle_state, content_hash, provenance, updated_at FROM profile_layers "
        f"{where_clause} ORDER BY scope_type, scope_id",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_decode_profile_layer_row(row) for row in rows]


async def set_profile_layer(
    db: aiosqlite.Connection,
    scope_type: str,
    scope_id: str,
    *,
    fields: dict[str, Any] | None = None,
    reset_fields: list[str] | None = None,
    provenance: dict[str, Any] | None = None,
    expected_revision: int | None = None,
    override_reason: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Validate, normalize, and persist one layer's profile (d8481276).

    Wholesale-replaces this scope's stored ``fields``/``reset_fields`` (not a
    merge — same "replace, not merge" contract as
    ``set_capability_profile``): a caller wanting to add one field to an
    existing layer must pass the full desired field set, not a delta.

    Optimistic concurrency: ``expected_revision=None`` (default) is
    last-write-wins. A given ``expected_revision`` that no longer matches the
    stored row raises :class:`profile_contract.ProfileStaleRevisionError`.

    Idempotent no-op resave: when the new (fields, reset_fields) hash to the
    SAME content_hash as what's already stored, this returns the current row
    unchanged — no revision bump, no revision-history row, exactly the
    ``board_snapshot_revisions`` "unchanged hash never grows the table"
    contract.

    ``override_reason`` is accepted here for symmetry with
    ``resolve_effective_profile`` (both share the same "override narrow_only"
    knob), but set_profile_layer itself never blocks a write on narrow_only —
    narrow_only/safe_direction is a MERGE-time (resolve) concern, not a
    write-time one: a layer may legitimately declare any value for a field it
    owns; whether that value is honored during resolution is decided later,
    per-request, by whoever calls resolve_effective_profile /
    get_effective_profile.

    Raises ``profile_contract.ProfileContractError`` on any malformed field
    (unknown name, disallowed layer, secret-shaped/absolute-path value) or
    bad scope_type — callers (the MCP handler) turn that into an ``{error}``
    dict rather than a partial write. Raises ``ValueError`` on lifecycle
    misuse (only hosted_default rows may carry a lifecycle_state — use
    :func:`transition_hosted_default_lifecycle` to change it, never this
    function).
    """
    scope_type = _pc.normalize_scope_type(scope_type)
    scope_id = _pc.normalize_scope_id(scope_id)
    fields = dict(fields or {})
    reset_fields = _pc.normalize_reset_fields(reset_fields)
    _pc.validate_layer_fields(scope_type, fields, reset_fields=reset_fields)
    normalized_provenance = _pc.normalize_provenance(provenance)

    current = await get_profile_layer(db, scope_type, scope_id)

    if expected_revision is not None and current["revision"] != expected_revision:
        raise _pc.ProfileStaleRevisionError(scope_type, scope_id, expected_revision, current["revision"])

    new_hash = _content_hash(fields, reset_fields)
    if new_hash == current["content_hash"]:
        return current  # idempotent no-op — never a fake revision bump

    new_revision = current["revision"] + 1
    lifecycle_state = current["lifecycle_state"]
    if scope_type == "hosted_default" and lifecycle_state is None:
        lifecycle_state = "draft"  # first-ever write on a hosted_default scope starts in draft

    await db.execute(
        "INSERT INTO profile_layers "
        "(scope_type, scope_id, schema_version, revision, fields, reset_fields, "
        "lifecycle_state, content_hash, provenance, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(scope_type, scope_id) DO UPDATE SET "
        "revision = excluded.revision, fields = excluded.fields, "
        "reset_fields = excluded.reset_fields, lifecycle_state = excluded.lifecycle_state, "
        "content_hash = excluded.content_hash, provenance = excluded.provenance, "
        "updated_at = excluded.updated_at",
        (
            scope_type, scope_id, _pc.SCHEMA_VERSION, new_revision,
            json.dumps(fields), json.dumps(reset_fields), lifecycle_state, new_hash,
            json.dumps(normalized_provenance) if normalized_provenance is not None else None,
        ),
    )
    await db.commit()

    if scope_type == "hosted_default":
        await _record_profile_layer_revision(
            db, scope_type, scope_id, new_revision, new_hash, lifecycle_state, fields, reset_fields, actor,
        )

    return await get_profile_layer(db, scope_type, scope_id)


async def reset_profile_layer(db: aiosqlite.Connection, scope_type: str, scope_id: str) -> dict[str, Any]:
    """Delete a scope's entire profile-layer row (d8481276).

    Idempotent — resetting an already-empty/never-set scope is a no-op.
    Explicit "reset" semantics distinct from a layer's own ``reset_fields``
    list: this removes this LAYER's contribution entirely, so the scope
    reverts to inheriting purely from less-specific layers — mirrors
    ``clear_capability_profile``.

    For ``hosted_default`` this clears the row (back to no-row / implicit
    draft) but is NOT an audited lifecycle transition — use
    :func:`transition_hosted_default_lifecycle` with ``new_state="retired"``
    for the audited terminal-state path instead.
    """
    scope_type = _pc.normalize_scope_type(scope_type)
    scope_id = _pc.normalize_scope_id(scope_id)
    await db.execute(
        "DELETE FROM profile_layers WHERE scope_type = ? AND scope_id = ?",
        (scope_type, scope_id),
    )
    await db.commit()
    return await get_profile_layer(db, scope_type, scope_id)


async def clone_profile_layer(
    db: aiosqlite.Connection,
    source_scope_type: str,
    source_scope_id: str,
    target_scope_type: str,
    target_scope_id: str,
    *,
    actor: str | None = None,
) -> dict[str, Any]:
    """Copy one layer's ``fields``/``reset_fields``/``provenance`` onto
    another scope (PROFILE-5, 0bec79a7).

    Reads the source layer via :func:`get_profile_layer`, then writes it
    onto the target scope via :func:`set_profile_layer` — NOT a raw SQL
    copy, so this goes through the exact same validation/hashing path any
    other write does. This matters because the target scope's
    ``FIELD_REGISTRY`` ``allowed_layers`` may differ from the source's: a
    field the source layer legally carries (e.g. an ``executor_config.*``
    field at ``session``) can still be rejected when cloned onto a target
    scope_type that doesn't allow it.

    Raises :class:`profile_contract.ProfileContractError` when the source
    layer does not exist (``revision == 0``, i.e. :func:`_empty_layer_dict`)
    — cloning nothing is a caller error, not a silent no-op.

    Cloning INTO a ``hosted_default`` target never carries over the
    source's ``lifecycle_state``: only ``fields``/``reset_fields``/
    ``provenance`` are copied, so a fresh hosted_default clone always lands
    in ``draft`` — exactly like any other first-ever write on a
    hosted_default scope (see :func:`set_profile_layer`). No special-casing
    needed here; it falls out of only copying those three fields.
    """
    source = await get_profile_layer(db, source_scope_type, source_scope_id)
    if source["revision"] == 0:
        raise _pc.ProfileContractError(
            f"cannot clone from ({source_scope_type!r}, {source_scope_id!r}): "
            "source layer does not exist (nothing to clone)"
        )
    return await set_profile_layer(
        db,
        target_scope_type,
        target_scope_id,
        fields=source["fields"],
        reset_fields=source["reset_fields"],
        provenance=source["provenance"],
        actor=actor,
    )


async def transition_hosted_default_lifecycle(
    db: aiosqlite.Connection,
    scope_id: str,
    new_state: str,
    *,
    actor: str | None = None,
) -> dict[str, Any]:
    """Advance a hosted_default layer's lifecycle state (d8481276).

    Idempotent: calling with the scope's CURRENT state is a no-op success
    (returns the row unchanged, no revision bump, no new revision-history
    row) — satisfies the "idempotent activate" acceptance criterion. A
    genuine transition must be listed in
    ``profile_contract.LIFECYCLE_TRANSITIONS[current_state]`` or this raises
    ``ProfileContractError``. draft -> {active, retired}; active ->
    {deprecated}; deprecated -> {retired, active}; retired is terminal.
    """
    scope_id = _pc.normalize_scope_id(scope_id)
    if new_state not in _pc.LIFECYCLE_STATES:
        raise _pc.ProfileContractError(
            f"invalid lifecycle_state {new_state!r}; must be one of {_pc.LIFECYCLE_STATES}"
        )
    current = await get_profile_layer(db, "hosted_default", scope_id)
    current_state = current["lifecycle_state"] or "draft"

    if new_state == current_state:
        return current  # idempotent no-op

    allowed = _pc.LIFECYCLE_TRANSITIONS.get(current_state, set())
    if new_state not in allowed:
        raise _pc.ProfileContractError(
            f"cannot transition hosted_default {scope_id!r} lifecycle from "
            f"{current_state!r} to {new_state!r}; allowed: {sorted(allowed) or '(none)'}"
        )

    new_revision = current["revision"] + 1
    content_hash = current["content_hash"] or _content_hash(current["fields"], current["reset_fields"])
    await db.execute(
        "INSERT INTO profile_layers "
        "(scope_type, scope_id, schema_version, revision, fields, reset_fields, "
        "lifecycle_state, content_hash, provenance, updated_at) "
        "VALUES ('hosted_default', ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(scope_type, scope_id) DO UPDATE SET "
        "revision = excluded.revision, lifecycle_state = excluded.lifecycle_state, "
        "updated_at = excluded.updated_at",
        (
            scope_id, _pc.SCHEMA_VERSION, new_revision,
            json.dumps(current["fields"]), json.dumps(current["reset_fields"]),
            new_state, content_hash,
            json.dumps(current["provenance"]) if current["provenance"] is not None else None,
        ),
    )
    await db.commit()
    await _record_profile_layer_revision(
        db, "hosted_default", scope_id, new_revision, content_hash, new_state,
        current["fields"], current["reset_fields"], actor,
    )
    return await get_profile_layer(db, "hosted_default", scope_id)


async def _record_profile_layer_revision(
    db: aiosqlite.Connection,
    scope_type: str,
    scope_id: str,
    revision: int,
    content_hash: str,
    lifecycle_state: str | None,
    fields: dict[str, Any],
    reset_fields: list[str],
    actor: str | None,
) -> None:
    """Append-only audit ledger — hosted_default ONLY, mirrors
    meridian.db.board_snapshot's board_snapshot_revisions table: one row per
    revision actually written (idempotent no-ops never reach here)."""
    row_id = _new_id()
    await db.execute(
        "INSERT INTO profile_layer_revisions "
        "(id, scope_type, scope_id, revision, content_hash, lifecycle_state, "
        "fields, reset_fields, actor, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
        (
            row_id, scope_type, scope_id, revision, content_hash, lifecycle_state,
            json.dumps(fields), json.dumps(reset_fields), actor,
        ),
    )
    await db.commit()


async def get_profile_layer_revisions(
    db: aiosqlite.Connection, scope_id: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Return the hosted_default revision history for one scope_id, newest
    first — the rollback/audit trail PROFILE-1 requires."""
    scope_id = _pc.normalize_scope_id(scope_id)
    async with db.execute(
        "SELECT id, scope_type, scope_id, revision, content_hash, lifecycle_state, "
        "fields, reset_fields, actor, created_at FROM profile_layer_revisions "
        "WHERE scope_type = 'hosted_default' AND scope_id = ? "
        "ORDER BY revision DESC LIMIT ?",
        (scope_id, int(limit)),
    ) as cur:
        rows = await cur.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = _row_to_dict(row) or {}
        try:
            d["fields"] = json.loads(d.get("fields") or "{}")
        except (TypeError, ValueError):
            d["fields"] = {}
        try:
            d["reset_fields"] = json.loads(d.get("reset_fields") or "[]")
        except (TypeError, ValueError):
            d["reset_fields"] = []
        out.append(d)
    return out


def _legacy_project_settings_to_fields(settings: dict[str, Any]) -> dict[str, Any]:
    """Map an existing get_project_settings() result onto profile-field
    names, for the synthetic 'project' layer get_effective_profile builds.

    This is READ-ONLY glue — it never writes anything back to profile_layers;
    the 7 legacy fields' single source of truth stays get_project_settings /
    update_project_settings / the projects table columns, per this module's
    "zero duplication" contract.
    """
    out: dict[str, Any] = {}
    for name in _LEGACY_PROJECT_SETTINGS_FIELDS:
        value = settings.get(name)
        if value is not None:
            out[name] = value
    executor_config = settings.get("executor_config") or {}
    for sub in _LEGACY_EXECUTOR_CONFIG_FIELDS:
        value = executor_config.get(sub)
        if value is not None:
            out[f"executor_config.{sub}"] = value
    return out


def _to_profile_layer(row: dict[str, Any]) -> _pc.ProfileLayer:
    return _pc.ProfileLayer(
        scope_type=row["scope_type"],
        scope_id=row["scope_id"],
        schema_version=row["schema_version"],
        revision=row["revision"],
        fields=row["fields"],
        reset_fields=row["reset_fields"],
        lifecycle_state=row["lifecycle_state"],
        provenance=row["provenance"],
        updated_at=row["updated_at"],
    )


async def get_effective_profile(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    session_id: str | None = None,
    user_scope_id: str | None = None,
    workspace_scope_id: str = "singleton",
    hosted_default_scope_id: str = "global",
    override_reason: str | None = None,
    previous_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the merged profile across all 5 applicable layers (d8481276).

    Walks hosted_default -> workspace -> user -> project -> session (least to
    most specific — see meridian.profile_contract.resolve_effective_profile),
    skipping any layer with nothing stored, exactly like
    get_effective_capability_profile does for its own (shorter) chain. A
    hosted_default row only participates when its lifecycle_state is
    "active" or "deprecated" — "draft" (unpublished) and "retired"
    (terminal) never apply. Read-only: never persists anything (except that
    reading a project's settings/profile rows has no side effects of its
    own). Raises ValueError for an unknown project_id.

    The 'project' layer is SYNTHETIC: its 6 scalar + 8 executor_config.*
    legacy fields come from get_project_settings (the existing, unduplicated
    authority); its revision and any of the 3 new fields
    (tool_priority_map/capability_manifest_ref/claim_verification_mode) come
    from the real profile_layers row at scope_type="project" — see module
    docstring.

    EXECUTABLE/DEGRADED STATUS (folded in from profile_resolution.py's
    EffectiveProfile.executable/degraded during the second PROFILE-RECON
    re-verification pass, 732c113e): ``resolve_effective_profile`` itself
    only ever sees a LIVE hosted_default layer (or none at all) — the
    "skip a non-live hosted_default row entirely" filter above means it has
    no way to distinguish "no hosted_default configured" from "a
    draft/retired hosted_default exists but doesn't apply". This function
    reads the raw ``hosted_row`` BEFORE that filter runs, so it computes
    that half of the signal itself and folds it into the resolved dict:

    * ``hosted_row["revision"] == 0`` (no row ever persisted for this
      scope_id — the common case for a project that never configured a
      hosted_default policy at all) contributes nothing; ``executable``/
      ``degraded`` stay whatever resolve_effective_profile already decided
      (True/False) from blocked_widens alone. This distinction matters
      because ``get_profile_layer`` reports a virtual ``lifecycle_state="draft"``
      for a scope_id with no row at all (see ``_empty_layer_dict``) — treating
      that the same as a REAL admin-authored draft would incorrectly mark
      every project that has simply never touched hosted_default as
      "degraded".
    * A REAL row (``revision > 0``) with ``lifecycle_state="retired"`` marks
      the resolution NOT executable (``executable_reasons`` gets
      ``"hosted_default_retired"``) and degraded.
    * A REAL row with ``lifecycle_state`` in ``("draft", "deprecated")``
      marks the resolution degraded but still executable
      (``degraded_reasons`` gets ``"hosted_default_lifecycle_{state}"``) —
      "deprecated" still applies its fields (see the live-states filter
      above) but is flagged so a caller can surface the caveat;
      "draft" never applies its fields at all, per the filter, but is still
      flagged so a caller/admin can see a draft exists and hasn't been
      published yet.
    * ``lifecycle_state="active"`` contributes nothing (the normal case).
    """
    project = await get_project(db, project_id)
    if project is None:
        raise ValueError(f"unknown project: {project_id}")

    layers: list[_pc.ProfileLayer] = []

    hosted_row = await get_profile_layer(db, "hosted_default", hosted_default_scope_id)
    if hosted_row["lifecycle_state"] in _pc.LIVE_HOSTED_DEFAULT_STATES:
        layers.append(_to_profile_layer(hosted_row))

    workspace_row = await get_profile_layer(db, "workspace", workspace_scope_id)
    layers.append(_to_profile_layer(workspace_row))

    if user_scope_id:
        user_row = await get_profile_layer(db, "user", user_scope_id)
        layers.append(_to_profile_layer(user_row))

    settings = await get_project_settings(db, project_id)
    project_fields = _legacy_project_settings_to_fields(settings or {})
    project_row = await get_profile_layer(db, "project", project_id)
    project_fields.update(project_row["fields"])
    layers.append(_pc.ProfileLayer(
        scope_type="project",
        scope_id=project_id,
        schema_version=project_row["schema_version"],
        revision=project_row["revision"],
        fields=project_fields,
        reset_fields=project_row["reset_fields"],
    ))

    if session_id:
        session_row = await get_profile_layer(db, "session", session_id)
        layers.append(_to_profile_layer(session_row))

    effective = _pc.resolve_effective_profile(
        layers, override_reason=override_reason, previous_fields=previous_fields,
    )
    result = effective.model_dump()
    result["project_id"] = project_id
    result["session_id"] = session_id

    # hosted_default-lifecycle half of executable/degraded -- see docstring
    # above. Only a REAL persisted row (revision > 0) counts; the virtual
    # "draft" _empty_layer_dict reports for a never-configured scope_id must
    # not make every ordinary project look degraded.
    if hosted_row["revision"] > 0:
        hosted_lifecycle = hosted_row["lifecycle_state"]
        if hosted_lifecycle == "retired":
            result["executable"] = False
            result["executable_reasons"] = [*result["executable_reasons"], "hosted_default_retired"]
            result["degraded"] = True
            result["degraded_reasons"] = [*result["degraded_reasons"], "hosted_default_retired"]
        elif hosted_lifecycle in ("draft", "deprecated"):
            result["degraded"] = True
            result["degraded_reasons"] = [
                *result["degraded_reasons"], f"hosted_default_lifecycle_{hosted_lifecycle}",
            ]

    return result


async def get_workspace_effective_profile(
    db: aiosqlite.Connection,
    *,
    workspace_scope_id: str = "singleton",
    hosted_default_scope_id: str = "global",
) -> dict[str, Any]:
    """Resolve the tenant/workspace-wide profile ONLY — hosted_default +
    workspace layers, no user/project/session layers (PROFILE-6, 89a06e40).

    Why this exists, distinct from :func:`get_effective_profile`: the
    tunnel/connector surface (``meridian.routes.tunnel``'s
    ``/tunnel/status/{tenant_id}`` and ``GET``/``PUT /tunnel/plugins``) is
    scoped by ``tenant_id`` only — there is no ``project_id`` anywhere in
    that machinery, and a tenant can plausibly have multiple projects, so
    calling ``get_effective_profile`` there would require an
    arbitrary/wrong ``project_id`` choice. See pinned decision ee7bccc9
    (project 5787cc92-ba7d-4788-b17c-28ab7938b839) for the full
    investigation this codifies: profile identity attached to the tunnel
    surface reflects ONLY the two layers that are genuinely tenant/
    workspace-wide rather than tied to one project. Project- and
    session-scoped profile identity stays covered by
    :func:`get_effective_profile` (via ``start_session``/
    ``generate_handoff``, both inherently ``project_id``-scoped).

    Calls :func:`meridian.profile_contract.resolve_effective_profile`
    directly (bypassing :func:`get_effective_profile`'s hard
    ``project_id`` requirement entirely) — this function never raises for
    an unknown/absent project, unlike ``get_effective_profile``.

    Mirrors ``get_effective_profile``'s own hosted_default-lifecycle
    handling verbatim: the ``LIVE_HOSTED_DEFAULT_STATES`` filter on which
    layer to pass into the merge, PLUS the raw-``hosted_row``
    executable/degraded computation (only a REAL persisted row —
    ``revision > 0`` — counts; the virtual "draft" ``_empty_layer_dict``
    report for a scope_id that has simply never configured hosted_default
    must not make an ordinary tenant look degraded). See
    :func:`get_effective_profile`'s own docstring for the full
    "draft never-configured vs real draft" distinction this replicates.
    """
    layers: list[_pc.ProfileLayer] = []

    hosted_row = await get_profile_layer(db, "hosted_default", hosted_default_scope_id)
    if hosted_row["lifecycle_state"] in _pc.LIVE_HOSTED_DEFAULT_STATES:
        layers.append(_to_profile_layer(hosted_row))

    workspace_row = await get_profile_layer(db, "workspace", workspace_scope_id)
    layers.append(_to_profile_layer(workspace_row))

    effective = _pc.resolve_effective_profile(layers)
    result = effective.model_dump()
    result["project_id"] = None
    result["session_id"] = None

    if hosted_row["revision"] > 0:
        hosted_lifecycle = hosted_row["lifecycle_state"]
        if hosted_lifecycle == "retired":
            result["executable"] = False
            result["executable_reasons"] = [*result["executable_reasons"], "hosted_default_retired"]
            result["degraded"] = True
            result["degraded_reasons"] = [*result["degraded_reasons"], "hosted_default_retired"]
        elif hosted_lifecycle in ("draft", "deprecated"):
            result["degraded"] = True
            result["degraded_reasons"] = [
                *result["degraded_reasons"], f"hosted_default_lifecycle_{hosted_lifecycle}",
            ]

    return result
