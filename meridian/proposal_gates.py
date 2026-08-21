"""c6d13571 — typed proposal HITL gates and decision receipts.

Meridian already has ``decisions_pinned`` (the "constitution": an
informational title/body/category/priority record) and ``decision_evidence``
(a typed, code-linked pointer backing ONE decision). Neither represents a
LANE-BLOCKING gate: a durable record that a specific piece of work is
materially ambiguous, names exactly what it affects, carries the evidence
that made it ambiguous, and stays closed (blocked) until a human explicitly
accepts, restricts, or rejects it — with an auditable receipt of who decided,
when, and under what expiry/reopen policy.

This module is the gap-filler. It is intentionally split the same way
``meridian.capability_manifest`` is split from its DB persistence
(``db.get_project_capability_manifest`` / ``set_project_capability_manifest``):
pure schema/validation lives here, alongside the DB read/write functions
(mirroring ``meridian.handoff``'s convention of a top-level module owning
direct ``db.execute`` calls for its own table — e.g. ``mint_handoff_token`` /
``verify_handoff_token`` — rather than requiring a second file under
``meridian/db/``). ``_migrate_proposal_gates`` creates the SQLite table
(mirrored on Postgres by ``pg_adapter._migrate_pg_proposal_gates``); both are
wired into ``init_db`` from ``meridian/db/__init__.py``, exactly like every
other guarded, non-inline migration in this codebase (2026-07-04 outage
rule: never inline a CREATE INDEX for a migration-added column/table in the
unguarded base schema literals).

Gate categories (closed enum, :data:`GATE_CATEGORIES`) — the six kinds of
materially ambiguous decision this schema exists to gate:

* ``legal_ip``               — legal / intellectual-property exposure.
* ``product_scope``          — a product-scope decision beyond the item's
  original brief.
* ``destructive_ops``        — a destructive operation (data loss, hard
  delete, irreversible mutation).
* ``production_deploy``      — a production deployment / release action.
* ``contradiction_acceptance`` — a human accepting (or rejecting) a detected
  contradiction between two sources of truth.
* ``other_ambiguous``        — any other materially ambiguous decision that
  does not fit the five categories above.

Routine, read-only decomposition and bounded fallback work is explicitly
OUT of scope for this schema — nothing in this module requires a gate for
ordinary autonomous work; gates exist only for the categories above.

Gate lane states (closed enum, :data:`GATE_STATES`):

* ``blocked``      — the lane is closed. This is the ONLY state a freshly
  raised gate can start in (fail-safe default — see :func:`create_gate`); it
  is also a valid RESOLVED state when a human explicitly decides the lane
  stays closed.
* ``quarantined``  — the lane is open, but only within a restricted/limited
  scope the human's ``decision`` text describes (e.g. "only the read path,
  not the write path").
* ``allowed``      — the lane is fully open; the human explicitly cleared it.

Every gate carries the named fields the sprint item's acceptance criteria
calls out: affected items/pointers (:func:`normalize_affected`), evidence,
question, decision, actor, timestamp, and an expiry/reopen policy
(:data:`REOPEN_POLICIES`). ``decision``/``actor``/``decided_at`` are ``None``
until :func:`resolve_gate` is called — a freshly raised gate has a question
and evidence but no decision yet (it is a request for human judgment, not
already-decided).

Expiry/reopen policy (:data:`REOPEN_POLICIES`):

* ``manual``          (default) — a decided gate stays in its decided state
  forever until a human calls :func:`reopen_gate` explicitly. ``expires_at``
  is purely informational for this policy (nothing auto-reverts).
* ``auto_on_expiry``  — once ``expires_at`` has passed, :func:`effective_state`
  reports ``blocked`` regardless of the last stored decision — the decision
  lapsed and the lane fails safe back to closed until re-decided. The raw
  stored ``state`` is left untouched (so the last human decision stays in the
  audit trail); only the EFFECTIVE state changes.
* ``on_new_evidence`` — same fail-open/closed contract as ``manual`` (nothing
  auto-reverts on a timer); the distinction is purely a policy label for
  callers/UIs to prompt "reopen this if new evidence appears" — this module
  does not itself detect new evidence.

Regardless of ``reopen_policy``, once a gate's ``expires_at`` has passed,
:func:`resolve_gate` treats the prior decision as lapsed and accepts a fresh
decision directly (no :func:`reopen_gate` call required) — expiry itself is
always sufficient grounds for a new decision. Before expiry, a decided gate
(``decided_at is not None``) refuses a second :func:`resolve_gate` call with
:class:`ProposalGateError` until :func:`reopen_gate` is called explicitly —
this is what makes a decision a real, auditable receipt rather than a value
silently overwritable by the next caller.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from meridian.pointers import PointerValidationError, validate_pointer

# ---------------------------------------------------------------------------
# Closed enums
# ---------------------------------------------------------------------------

GATE_CATEGORIES: tuple[str, ...] = (
    "legal_ip",
    "product_scope",
    "destructive_ops",
    "production_deploy",
    "contradiction_acceptance",
    "other_ambiguous",
)
_GATE_CATEGORIES = frozenset(GATE_CATEGORIES)

#: Human-readable label for each category — for dashboards/handoff rendering.
GATE_CATEGORY_LABELS: dict[str, str] = {
    "legal_ip": "Legal / IP",
    "product_scope": "Product scope",
    "destructive_ops": "Destructive operation",
    "production_deploy": "Production deployment",
    "contradiction_acceptance": "Human acceptance of a contradiction",
    "other_ambiguous": "Other materially ambiguous decision",
}

GATE_STATES: tuple[str, ...] = ("blocked", "quarantined", "allowed")
_GATE_STATES = frozenset(GATE_STATES)

#: The lane state a freshly raised gate always starts in — fail-safe: a gate
#: nobody has decided yet blocks its lane, never defaults to open.
DEFAULT_GATE_STATE = "blocked"

REOPEN_POLICIES: tuple[str, ...] = ("manual", "auto_on_expiry", "on_new_evidence")
_REOPEN_POLICIES = frozenset(REOPEN_POLICIES)
DEFAULT_REOPEN_POLICY = "manual"


class ProposalGateError(ValueError):
    """Raised on a malformed gate (bad category/state/reopen_policy/affected
    shape) or an illegal state transition (resolving an already-decided,
    unexpired gate without reopening it first; reopening a gate that was
    never decided)."""


# ---------------------------------------------------------------------------
# Small local helpers — deliberately NOT imported from meridian.db.  Every
# other top-level module that writes its own table (meridian.handoff's
# handoff_tokens / handoff_corrections) either generates ids with a bare
# uuid4() at call time or reaches into ``db_module._new_id()`` at RUNTIME
# inside a function body — never via a module-level
# ``from meridian.db import _new_id`` — because meridian/db/__init__.py
# imports THIS module's migration/CRUD functions at the bottom of its own
# execution (mirroring how it imports meridian.db.decision_evidence). A
# module-level ``from meridian.db import ...`` here would risk a circular
# import if anything ever imports meridian.proposal_gates before
# meridian.db has finished initializing. These two one-liners avoid that
# risk entirely while staying byte-for-byte equivalent to db._new_id /
# db._row_to_dict.
# ---------------------------------------------------------------------------


def _new_id() -> str:
    return str(uuid.uuid4())


def _row_to_dict(row: Any) -> "dict[str, Any] | None":
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return {k: row[k] for k in row.keys()}


def _now_iso() -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS.ffffff' — matches decision_evidence's own
    cross-dialect-safe convention (computed in Python, not via a SQL
    now()/datetime('now') call — see the project's now() vs
    clock_timestamp() note: multiple inserts in one transaction would
    otherwise share an identical now()-frozen timestamp)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _parse_iso(value: Any) -> "datetime | None":
    """Best-effort parse of a stored timestamp back to an aware UTC
    datetime. Never raises — an unparseable/missing value is just None."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # Accept a trailing 'Z' (not produced by _now_iso, but a defensive
    # accommodation for hand-authored expires_at values).
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Validation (pure — no DB, unit-testable)
# ---------------------------------------------------------------------------


def validate_category(category: Any) -> str:
    if not isinstance(category, str) or not category.strip():
        raise ProposalGateError("category must be a non-empty string")
    normalized = category.strip().lower()
    if normalized not in _GATE_CATEGORIES:
        raise ProposalGateError(
            f"category must be one of {GATE_CATEGORIES}, got {category!r}"
        )
    return normalized


def validate_state(state: Any) -> str:
    if not isinstance(state, str) or not state.strip():
        raise ProposalGateError("state must be a non-empty string")
    normalized = state.strip().lower()
    if normalized not in _GATE_STATES:
        raise ProposalGateError(
            f"state must be one of {GATE_STATES}, got {state!r}"
        )
    return normalized


def validate_reopen_policy(policy: Any) -> str:
    if policy is None:
        return DEFAULT_REOPEN_POLICY
    if not isinstance(policy, str) or not policy.strip():
        raise ProposalGateError("reopen_policy must be a non-empty string")
    normalized = policy.strip().lower()
    if normalized not in _REOPEN_POLICIES:
        raise ProposalGateError(
            f"reopen_policy must be one of {REOPEN_POLICIES}, got {policy!r}"
        )
    return normalized


def normalize_affected(affected: Any) -> list[dict[str, Any]]:
    """Validate + normalize the "affected items/pointers" list a gate names.

    Each entry is one of:

    * a bare non-empty string — shorthand for a sprint item id, normalized
      to ``{"sprint_item_id": "<id>"}``.
    * ``{"sprint_item_id": "<id>"}`` — the same shape, explicit.
    * a full generic pointer (``{"source_type": ..., "targets": [...]}``,
      see :mod:`meridian.pointers`) — validated via
      :func:`meridian.pointers.validate_pointer` (the SAME primitive
      ``decision_evidence``/``sprint_item_pointers`` already use, not
      reinvented) and stored as ``{"pointer": <normalized pointer>}``.

    Raises :class:`ProposalGateError` if ``affected`` is not a non-empty
    list, or if any entry is malformed. A gate must name at least one
    concrete thing it affects — an empty ``affected`` list defeats the
    entire point of a lane-blocking gate.
    """
    if not isinstance(affected, list) or not affected:
        raise ProposalGateError(
            "affected must be a non-empty list of sprint_item_id strings "
            "and/or generic pointer objects"
        )
    normalized: list[dict[str, Any]] = []
    for i, entry in enumerate(affected):
        if isinstance(entry, str):
            if not entry.strip():
                raise ProposalGateError(f"affected[{i}] must be a non-empty string")
            normalized.append({"sprint_item_id": entry.strip()})
            continue
        if not isinstance(entry, dict):
            raise ProposalGateError(
                f"affected[{i}] must be a string, {{sprint_item_id}}, or a "
                "generic pointer object"
            )
        if "sprint_item_id" in entry:
            sid = entry.get("sprint_item_id")
            if not isinstance(sid, str) or not sid.strip():
                raise ProposalGateError(
                    f"affected[{i}].sprint_item_id must be a non-empty string"
                )
            normalized.append({"sprint_item_id": sid.strip()})
            continue
        if "source_type" in entry or "targets" in entry:
            try:
                pointer = validate_pointer(entry)
            except PointerValidationError as exc:
                raise ProposalGateError(f"affected[{i}]: {exc}") from exc
            normalized.append({"pointer": pointer})
            continue
        raise ProposalGateError(
            f"affected[{i}] must carry either 'sprint_item_id' or the "
            "generic pointer shape ('source_type' + 'targets')"
        )
    return normalized


def is_gate_expired(gate: dict[str, Any], *, now: "datetime | None" = None) -> bool:
    """True when ``gate['expires_at']`` is set and has already passed.

    Never raises: an unparseable/missing ``expires_at`` is simply "not
    expired". ``now`` is injectable for deterministic tests; defaults to the
    real current UTC time.
    """
    expires_at = _parse_iso(gate.get("expires_at"))
    if expires_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current >= expires_at


def effective_state(gate: dict[str, Any], *, now: "datetime | None" = None) -> str:
    """The lane state a caller should actually honor right now.

    Identical to the stored ``state`` EXCEPT: when ``reopen_policy ==
    'auto_on_expiry'`` and the gate :func:`is_gate_expired`, this reports
    ``'blocked'`` regardless of the last stored decision — the decision
    lapsed and the lane fails safe back to closed until re-decided via
    :func:`resolve_gate`. The raw stored ``state`` column is left untouched
    by this function (read-only) so the last human decision stays visible in
    the audit trail; only the EFFECTIVE state a caller should act on changes.
    """
    stored = gate.get("state") or DEFAULT_GATE_STATE
    if gate.get("reopen_policy") == "auto_on_expiry" and is_gate_expired(gate, now=now):
        return "blocked"
    return stored


# ---------------------------------------------------------------------------
# Migration — SQLite. Mirrored on Postgres by
# pg_adapter._migrate_pg_proposal_gates. Guarded (CREATE TABLE/INDEX IF NOT
# EXISTS), called unconditionally from init_db, never inlined in either base
# CREATE_TABLES literal (the 2026-07-04 outage rule).
# ---------------------------------------------------------------------------


async def _migrate_proposal_gates(db: Any) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS proposal_gates (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            category TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'blocked',
            question TEXT NOT NULL,
            affected TEXT NOT NULL,
            evidence TEXT NOT NULL,
            decision TEXT,
            actor TEXT,
            decided_at TEXT,
            previous_decision TEXT,
            previous_actor TEXT,
            previous_decided_at TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT,
            expires_at TEXT,
            reopen_policy TEXT NOT NULL DEFAULT 'manual',
            reopen_count INTEGER NOT NULL DEFAULT 0,
            reopened_at TEXT,
            reopen_reason TEXT,
            reopened_by TEXT
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_proposal_gates_project "
        "ON proposal_gates(project_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_proposal_gates_project_state "
        "ON proposal_gates(project_id, state)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_proposal_gates_project_category "
        "ON proposal_gates(project_id, category)"
    )
    await db.commit()


def _row_to_gate(row: Any) -> "dict[str, Any] | None":
    d = _row_to_dict(row)
    if d is None:
        return None
    raw_affected = d.get("affected")
    if isinstance(raw_affected, str):
        try:
            d["affected"] = json.loads(raw_affected)
        except (ValueError, TypeError):
            d["affected"] = []
    return d


# ---------------------------------------------------------------------------
# CRUD + state machine
# ---------------------------------------------------------------------------


async def create_gate(
    db: Any,
    project_id: str,
    category: str,
    question: str,
    affected: list[Any],
    evidence: str,
    *,
    created_by: "str | None" = None,
    expires_at: "str | None" = None,
    reopen_policy: str = DEFAULT_REOPEN_POLICY,
) -> dict[str, Any]:
    """Raise a new HITL gate. Always starts in ``state='blocked'`` (fail-safe
    default — see the module docstring) with no decision yet.

    Raises :class:`ProposalGateError` on any malformed field. ``question``
    and ``evidence`` must be non-empty strings; ``affected`` is validated via
    :func:`normalize_affected` (non-empty, at least one concrete target).
    """
    if not project_id or not isinstance(project_id, str):
        raise ProposalGateError("project_id must be a non-empty string")
    normalized_category = validate_category(category)
    if not isinstance(question, str) or not question.strip():
        raise ProposalGateError("question must be a non-empty string")
    if not isinstance(evidence, str) or not evidence.strip():
        raise ProposalGateError("evidence must be a non-empty string")
    normalized_affected = normalize_affected(affected)
    normalized_reopen_policy = validate_reopen_policy(reopen_policy)

    gid = _new_id()
    affected_json = json.dumps(normalized_affected, ensure_ascii=False, sort_keys=True)
    await db.execute(
        "INSERT INTO proposal_gates "
        "(id, project_id, category, state, question, affected, evidence, "
        "created_by, reopen_policy, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            gid, project_id, normalized_category, DEFAULT_GATE_STATE,
            question.strip(), affected_json, evidence.strip(),
            created_by, normalized_reopen_policy, expires_at,
        ),
    )
    await db.commit()
    created = await get_gate(db, gid, project_id=project_id)
    assert created is not None  # just written
    return created


async def get_gate(
    db: Any, gate_id: str, *, project_id: "str | None" = None,
) -> "dict[str, Any] | None":
    if project_id is not None:
        async with db.execute(
            "SELECT * FROM proposal_gates WHERE id = ? AND project_id = ?",
            (gate_id, project_id),
        ) as cur:
            row = await cur.fetchone()
    else:
        async with db.execute(
            "SELECT * FROM proposal_gates WHERE id = ?", (gate_id,)
        ) as cur:
            row = await cur.fetchone()
    return _row_to_gate(row)


async def list_gates(
    db: Any,
    project_id: str,
    *,
    category: "str | None" = None,
    state: "str | None" = None,
) -> list[dict[str, Any]]:
    """All gates for ``project_id``, newest first. Optionally filtered by
    ``category`` and/or (raw, stored) ``state`` — pass the STORED state, not
    the effective one; callers that want the expiry-aware view should run
    the result through :func:`effective_state` themselves (mirrors
    ``decision_evidence``'s "confidence is metadata, never a gate" contract:
    this function never silently reinterprets what it returns)."""
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if category is not None:
        clauses.append("category = ?")
        params.append(validate_category(category))
    if state is not None:
        clauses.append("state = ?")
        params.append(validate_state(state))
    sql = (
        f"SELECT * FROM proposal_gates WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC, id DESC"
    )
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [g for g in (_row_to_gate(r) for r in rows) if g is not None]


async def resolve_gate(
    db: Any,
    project_id: str,
    gate_id: str,
    state: str,
    decision: str,
    actor: str,
    *,
    expires_at: Any = ...,
    reopen_policy: Any = ...,
) -> dict[str, Any]:
    """Record a human decision on a gate: the lane's new ``state``
    (blocked/quarantined/allowed), the free-text ``decision``, and the
    ``actor`` who decided (with an auto-stamped ``decided_at``).

    Raises :class:`ProposalGateError` if the gate does not exist in
    ``project_id``, if ``state``/``decision``/``actor`` are malformed, or —
    the one-shot-receipt guard — if the gate was already decided
    (``decided_at is not None``) and has NOT expired: call
    :func:`reopen_gate` first. An EXPIRED prior decision is treated as
    lapsed and a fresh decision is accepted directly, no reopen needed (see
    the module docstring's expiry/reopen policy section).

    ``expires_at``/``reopen_policy`` default (the ``...`` sentinel) to
    leaving the existing stored value unchanged; pass an explicit value
    (including ``None``) to update it as part of this decision.
    """
    gate = await get_gate(db, gate_id, project_id=project_id)
    if gate is None:
        raise ProposalGateError(
            f"proposal gate {gate_id!r} not found in project {project_id!r}"
        )
    normalized_state = validate_state(state)
    if not isinstance(decision, str) or not decision.strip():
        raise ProposalGateError("decision must be a non-empty string")
    if not isinstance(actor, str) or not actor.strip():
        raise ProposalGateError("actor must be a non-empty string")

    if gate.get("decided_at") is not None and not is_gate_expired(gate):
        raise ProposalGateError(
            f"proposal gate {gate_id!r} was already decided at "
            f"{gate['decided_at']!r} (state={gate.get('state')!r}) — call "
            "reopen_gate first to record a new decision, or wait for it to "
            "expire"
        )

    new_expires_at = gate.get("expires_at") if expires_at is ... else expires_at
    new_reopen_policy = (
        gate.get("reopen_policy") if reopen_policy is ...
        else validate_reopen_policy(reopen_policy)
    )
    now = _now_iso()
    await db.execute(
        "UPDATE proposal_gates SET state = ?, decision = ?, actor = ?, "
        "decided_at = ?, previous_decision = ?, previous_actor = ?, "
        "previous_decided_at = ?, expires_at = ?, reopen_policy = ?, "
        "updated_at = ? WHERE id = ? AND project_id = ?",
        (
            normalized_state, decision.strip(), actor.strip(), now,
            gate.get("decision"), gate.get("actor"), gate.get("decided_at"),
            new_expires_at, new_reopen_policy, now, gate_id, project_id,
        ),
    )
    await db.commit()
    updated = await get_gate(db, gate_id, project_id=project_id)
    assert updated is not None
    return updated


async def reopen_gate(
    db: Any, project_id: str, gate_id: str, actor: str, reason: str,
) -> dict[str, Any]:
    """Invalidate a still-standing decision before it expires (e.g. new
    evidence surfaced) so :func:`resolve_gate` can be called again.

    Snapshots the current ``decision``/``actor``/``decided_at`` into
    ``previous_*`` (a one-hop audit trail — mirrors ``decision_evidence``'s
    supersession snapshot, without a full multi-row event log), clears the
    live decision fields, resets ``state`` to ``'blocked'`` (fail-safe — the
    lane closes again pending a fresh decision), and increments
    ``reopen_count``.

    Raises :class:`ProposalGateError` if the gate does not exist, or if it
    was never decided (``decided_at is None`` — nothing to reopen; raise a
    NEW gate instead, or just resolve this one directly).
    """
    gate = await get_gate(db, gate_id, project_id=project_id)
    if gate is None:
        raise ProposalGateError(
            f"proposal gate {gate_id!r} not found in project {project_id!r}"
        )
    if not isinstance(actor, str) or not actor.strip():
        raise ProposalGateError("actor must be a non-empty string")
    if not isinstance(reason, str) or not reason.strip():
        raise ProposalGateError("reason must be a non-empty string")
    if gate.get("decided_at") is None:
        raise ProposalGateError(
            f"proposal gate {gate_id!r} has never been decided — nothing to "
            "reopen"
        )

    now = _now_iso()
    reopen_count = int(gate.get("reopen_count") or 0) + 1
    await db.execute(
        "UPDATE proposal_gates SET state = ?, decision = NULL, actor = NULL, "
        "decided_at = NULL, previous_decision = ?, previous_actor = ?, "
        "previous_decided_at = ?, reopen_count = ?, reopened_at = ?, "
        "reopen_reason = ?, reopened_by = ?, updated_at = ? "
        "WHERE id = ? AND project_id = ?",
        (
            DEFAULT_GATE_STATE, gate.get("decision"), gate.get("actor"),
            gate.get("decided_at"), reopen_count, now, reason.strip(),
            actor.strip(), now, gate_id, project_id,
        ),
    )
    await db.commit()
    updated = await get_gate(db, gate_id, project_id=project_id)
    assert updated is not None
    return updated


async def blocking_gates_for_sprint_item(
    db: Any, project_id: str, sprint_item_id: str,
) -> list[dict[str, Any]]:
    """Which gates currently block or quarantine ``sprint_item_id``.

    A gate "names" this item when its ``affected`` list contains a
    ``{"sprint_item_id": sprint_item_id}`` entry. Only gates whose
    :func:`effective_state` is NOT ``'allowed'`` are returned — an allowed
    gate no longer restricts anything. Read-only: this function does not
    change claim/complete behavior itself (see
    ``meridian.db.sprint_items.get_sprint_item_blocking_gates`` for the
    thin wrapper an executor-facing caller would use).
    """
    all_gates = await list_gates(db, project_id)
    blocking: list[dict[str, Any]] = []
    for gate in all_gates:
        if effective_state(gate) == "allowed":
            continue
        for entry in gate.get("affected") or []:
            if isinstance(entry, dict) and entry.get("sprint_item_id") == sprint_item_id:
                blocking.append(gate)
                break
    return blocking
