"""7c96d41b — durable persistence for the ``paper_contract`` schema: a
first-class, versioned editorial-intent document for Meridian's
manuscript/paper editorial tooling line. See :mod:`meridian.paper_contract`
for the closed vocabularies (``CONTRACT_STATUSES`` /
``REVISION_APPROVAL_STATUSES``) and the content-fingerprint helper this
module is built on top of.

SCHEMA
------

``paper_contracts`` — the stable identity for one manuscript's editorial
contract, scoped ``(project_id, paper_key)`` unique. Never itself holds
editorial content — see ``paper_contract_revisions``. ``current_revision_id``
is the ONE mutable pointer on this row (mirrors
``meridian.db.profile_layers``'s revision-pointer pattern): it names the
most recently APPROVED revision, never the newest draft, so an in-flight
proposed edit can never become binding without passing the human-approval
gate below. Deliberately carries no ``REFERENCES`` on
``current_revision_id`` — same reasoning as
``workspace_proposals.promoted_to_sprint_item_id``: it is a forward/mutable
pointer into a sibling table, not an ownership edge.

``paper_contract_revisions`` — the append-only, immutable ledger of
proposed/approved editorial-intent snapshots. ``content_json`` is the
serialized ``PaperContractContent`` payload (see ``meridian.models``);
``content_hash`` is :func:`meridian.paper_contract.content_fingerprint` of
that same payload, stored so a caller can detect "this revision's content is
byte-identical to that one" without re-parsing and re-hashing JSON.
:func:`create_paper_contract_revision` numbers revisions via the same
``COALESCE(MAX(...), 0) + 1`` + bounded-retry idiom as
``meridian.db.experiment_model.create_attempt`` /
``meridian.db.research_graph._next_node_sequence``, to survive a
concurrent-create race rather than trusting the read-then-write gap.

HUMAN APPROVAL GATE
--------------------

:func:`approve_paper_contract_revision` is the only way a revision's
``approval_status`` becomes ``'approved'`` and the only way a contract's
``current_revision_id`` ever changes. It REQUIRES a non-empty
``approved_by_human_id`` — an unattributed approval would defeat the whole
point of a human-approval gate. Approving supersedes the previously-approved
revision (``superseded_by_revision_id`` set exactly once, mirroring
``decisions_pinned.superseded_by``) and flips the contract's ``status`` to
``'active'``. Approving an already-approved revision is an idempotent
no-op (matches ``transition_attempt``'s self-transition contract); approving
a ``'rejected'`` revision raises — propose a new revision instead of trying
to revive a dead one.

This is intentionally a BARE schema addition (7c96d41b is scoped as
"SCHEMA:", matching the naming convention this project uses for
narrowly-scoped data-model items): no MCP tool, no HTTP route, and no
rejection/list-contracts/list-by-status surface beyond what a real round
trip through the schema needs to exercise. A full editorial-tooling API is
explicitly a follow-on, not this item.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from meridian.db import _new_id, _row_to_dict
from meridian.paper_contract import content_fingerprint
from meridian.secret_redaction import check_for_secrets

_MAX_REVISION_NUMBER_RETRIES = 5


def _now_iso() -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS' — matches
    ``meridian.db.experiment_model``'s cross-dialect-safe timestamp
    convention (computed in Python, not a SQL now()/datetime('now') call)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _is_unique_violation(exc: BaseException) -> bool:
    """Heuristic: does ``exc`` look like a UNIQUE/duplicate-key violation?

    Matches sqlite3's ``UNIQUE constraint failed`` and psycopg3's
    ``UniqueViolation``. Never raises. Duplicated per-module by existing
    convention (see ``meridian.db.experiment_model._is_unique_violation``)."""
    msg = str(exc).lower()
    return "unique" in msg or "duplicate key" in msg


# ---------------------------------------------------------------------------
# Migration — guarded, idempotent (2026-07-04 outage rule: no unguarded
# CREATE INDEX on a migration-added column/table in CREATE_TABLES/
# CREATE_TABLES_CORE). Mirrored on Postgres by
# pg_adapter._migrate_pg_paper_contract.
# ---------------------------------------------------------------------------


async def _migrate_paper_contract(db: aiosqlite.Connection) -> None:
    """7c96d41b — create ``paper_contracts`` / ``paper_contract_revisions``
    if absent. See this module's docstring for the full schema rationale."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS paper_contracts (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            paper_key TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'active', 'archived')),
            current_revision_id TEXT,
            latest_revision_number INTEGER NOT NULL DEFAULT 0,
            created_by_human_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (project_id, paper_key)
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_contracts_project "
        "ON paper_contracts(project_id)"
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS paper_contract_revisions (
            id TEXT PRIMARY KEY,
            contract_id TEXT NOT NULL REFERENCES paper_contracts(id),
            revision_number INTEGER NOT NULL,
            content_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            change_summary TEXT,
            approval_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (approval_status IN ('pending', 'approved', 'rejected')),
            approved_by_human_id TEXT,
            approved_at TEXT,
            superseded_by_revision_id TEXT REFERENCES paper_contract_revisions(id),
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (contract_id, revision_number)
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_contract_revisions_contract "
        "ON paper_contract_revisions(contract_id, revision_number DESC)"
    )
    await db.commit()


def _row_to_contract(row: Any) -> "dict[str, Any] | None":
    return _row_to_dict(row)


def _row_to_revision(row: Any) -> "dict[str, Any] | None":
    d = _row_to_dict(row)
    if d is None:
        return None
    raw = d.pop("content_json", None)
    try:
        d["content"] = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        d["content"] = {}
    return d


# ---------------------------------------------------------------------------
# paper_contracts
# ---------------------------------------------------------------------------


async def create_paper_contract(
    db: aiosqlite.Connection,
    project_id: str,
    paper_key: str,
    title: str,
    *,
    created_by_human_id: "str | None" = None,
) -> dict[str, Any]:
    """Create a new paper_contract identity scoped to ``project_id``.

    Not idempotent on ``paper_key`` — a repeat call with the same key raises
    ``ValueError`` (a genuine caller error: this is a stable identity, not a
    resumable submission like ``research_runs.idempotency_key``). Callers
    that need "get or create" semantics should call
    :func:`get_paper_contract_by_key` first.
    """
    project_id = (project_id or "").strip()
    paper_key = (paper_key or "").strip()
    title = (title or "").strip()
    if not project_id:
        raise ValueError("create_paper_contract requires a non-empty project_id")
    if not paper_key:
        raise ValueError("create_paper_contract requires a non-empty paper_key")
    if not title:
        raise ValueError("create_paper_contract requires a non-empty title")
    check_for_secrets(paper_key, context="paper_contract paper_key")
    check_for_secrets(title, context="paper_contract title")

    existing = await get_paper_contract_by_key(db, project_id, paper_key)
    if existing is not None:
        raise ValueError(
            f"paper_contract with paper_key {paper_key!r} already exists "
            f"in project {project_id!r} (id={existing['id']!r})"
        )

    cid = _new_id()
    try:
        await db.execute(
            "INSERT INTO paper_contracts (id, project_id, paper_key, title, created_by_human_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (cid, project_id, paper_key, title, created_by_human_id),
        )
    except Exception as exc:  # noqa: BLE001 — classified below
        if _is_unique_violation(exc):
            # Lost a create race against another caller submitting the SAME
            # paper_key — nothing of ours was written; surface the same
            # "already exists" error a sequential caller would have seen.
            winner = await get_paper_contract_by_key(db, project_id, paper_key)
            if winner is not None:
                raise ValueError(
                    f"paper_contract with paper_key {paper_key!r} already exists "
                    f"in project {project_id!r} (id={winner['id']!r})"
                ) from exc
        raise
    await db.commit()
    created = await get_paper_contract(db, project_id, cid)
    assert created is not None  # just written
    return created


async def get_paper_contract(
    db: aiosqlite.Connection, project_id: str, contract_id: str
) -> "dict[str, Any] | None":
    """Fetch one paper_contract by id, scoped to ``project_id`` — a
    cross-project lookup (right id, wrong project) returns ``None``, exactly
    like a nonexistent id, never leaking the row's existence to the wrong
    tenant."""
    async with db.execute(
        "SELECT * FROM paper_contracts WHERE id = ? AND project_id = ?",
        (contract_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_contract(row)


async def get_paper_contract_by_key(
    db: aiosqlite.Connection, project_id: str, paper_key: str
) -> "dict[str, Any] | None":
    """Fetch one paper_contract by its natural key ``(project_id, paper_key)``."""
    async with db.execute(
        "SELECT * FROM paper_contracts WHERE project_id = ? AND paper_key = ?",
        (project_id, paper_key),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_contract(row)


# ---------------------------------------------------------------------------
# paper_contract_revisions
# ---------------------------------------------------------------------------


async def create_paper_contract_revision(
    db: aiosqlite.Connection,
    project_id: str,
    contract_id: str,
    content: "dict[str, Any]",
    *,
    change_summary: "str | None" = None,
    created_by: "str | None" = None,
) -> dict[str, Any]:
    """Append a new PENDING revision to ``contract_id``'s ledger.

    ``content`` is the caller's already-validated
    ``meridian.models.PaperContractContent`` payload, passed as a plain
    dict (this module stays a plain-dict CRUD layer, like
    ``meridian.db.experiment_model``'s ``params``/``config_template`` —
    shape validation is the Pydantic model's job, not this function's).
    Only a minimal non-empty check happens here.

    Numbered via a bounded retry loop against the
    ``(contract_id, revision_number)`` unique index — see module docstring.
    Does NOT touch ``current_revision_id``: a new revision is never binding
    until :func:`approve_paper_contract_revision` says so.
    """
    project_id = (project_id or "").strip()
    contract_id = (contract_id or "").strip()
    contract = await get_paper_contract(db, project_id, contract_id)
    if contract is None:
        raise ValueError(f"paper_contract {contract_id!r} not found in project {project_id!r}")
    if not isinstance(content, dict) or not content:
        raise ValueError("create_paper_contract_revision requires non-empty content")
    if change_summary is not None:
        check_for_secrets(change_summary, context="paper_contract revision change_summary")

    content_json = json.dumps(content, ensure_ascii=False, sort_keys=True)
    content_hash = content_fingerprint(content)

    last_exc: "Exception | None" = None
    for _ in range(_MAX_REVISION_NUMBER_RETRIES):
        next_number = await _next_revision_number(db, contract_id)
        rid = _new_id()
        try:
            await db.execute(
                "INSERT INTO paper_contract_revisions "
                "(id, contract_id, revision_number, content_json, content_hash, "
                "change_summary, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rid, contract_id, next_number, content_json, content_hash, change_summary, created_by),
            )
        except Exception as exc:  # noqa: BLE001 — classified below
            if _is_unique_violation(exc):
                last_exc = exc
                continue
            raise
        await db.execute(
            "UPDATE paper_contracts SET latest_revision_number = ?, updated_at = ? "
            "WHERE id = ? AND project_id = ?",
            (next_number, _now_iso(), contract_id, project_id),
        )
        await db.commit()
        created = await get_paper_contract_revision(db, project_id, rid)
        assert created is not None  # just written
        return created
    raise RuntimeError(
        f"create_paper_contract_revision: exhausted {_MAX_REVISION_NUMBER_RETRIES} "
        f"retries numbering a revision for contract {contract_id!r}"
    ) from last_exc


async def _next_revision_number(db: aiosqlite.Connection, contract_id: str) -> int:
    """Mirrors ``experiment_model._next_attempt_number``'s identical
    ``COALESCE(MAX(...), 0) + 1`` pattern."""
    async with db.execute(
        "SELECT COALESCE(MAX(revision_number), 0) + 1 AS next_n "
        "FROM paper_contract_revisions WHERE contract_id = ?",
        (contract_id,),
    ) as cur:
        row = await cur.fetchone()
    return int((row["next_n"] if row is not None else 1) or 1)


async def get_paper_contract_revision(
    db: aiosqlite.Connection, project_id: str, revision_id: str
) -> "dict[str, Any] | None":
    """Fetch one revision by id, scoped to ``project_id`` via a join back to
    ``paper_contracts`` (a revision has no ``project_id`` column of its
    own — its contract is the single source of truth for project scope)."""
    async with db.execute(
        "SELECT r.* FROM paper_contract_revisions r "
        "JOIN paper_contracts c ON c.id = r.contract_id "
        "WHERE r.id = ? AND c.project_id = ?",
        (revision_id, project_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_revision(row)


async def list_paper_contract_revisions(
    db: aiosqlite.Connection, project_id: str, contract_id: str
) -> list[dict[str, Any]]:
    """Every revision for ``contract_id``, oldest first — the full editorial
    history."""
    async with db.execute(
        "SELECT r.* FROM paper_contract_revisions r "
        "JOIN paper_contracts c ON c.id = r.contract_id "
        "WHERE r.contract_id = ? AND c.project_id = ? "
        "ORDER BY r.revision_number ASC",
        (contract_id, project_id),
    ) as cur:
        rows = await cur.fetchall()
    return [rev for rev in (_row_to_revision(r) for r in rows) if rev is not None]


async def approve_paper_contract_revision(
    db: aiosqlite.Connection,
    project_id: str,
    revision_id: str,
    approved_by_human_id: str,
) -> dict[str, Any]:
    """The human-approval gate: pin ``revision_id`` as its contract's
    binding ``current_revision_id``.

    Requires a non-empty ``approved_by_human_id`` — an unattributed approval
    would defeat the point of a human-approval gate. Idempotent when the
    revision is already approved (re-approving is a no-op success, matching
    ``transition_attempt``'s self-transition contract); raises when the
    revision is ``rejected`` (a dead end — propose a new revision instead).

    Supersedes the contract's previously-approved revision (if any) by
    setting its ``superseded_by_revision_id``, and flips the contract's
    ``status`` to ``'active'``.
    """
    project_id = (project_id or "").strip()
    approved_by_human_id = (approved_by_human_id or "").strip()
    if not approved_by_human_id:
        raise ValueError(
            "approve_paper_contract_revision requires a non-empty approved_by_human_id"
        )

    revision = await get_paper_contract_revision(db, project_id, revision_id)
    if revision is None:
        raise ValueError(f"paper_contract revision {revision_id!r} not found in project {project_id!r}")
    if revision["approval_status"] == "approved":
        return revision  # idempotent no-op — already the binding revision
    if revision["approval_status"] == "rejected":
        raise ValueError(
            f"cannot approve revision {revision_id!r}: already rejected — "
            "propose a new revision instead"
        )

    contract = await get_paper_contract(db, project_id, revision["contract_id"])
    assert contract is not None  # FK guarantees the parent row exists
    now = _now_iso()

    await db.execute(
        "UPDATE paper_contract_revisions SET approval_status = 'approved', "
        "approved_by_human_id = ?, approved_at = ? WHERE id = ?",
        (approved_by_human_id, now, revision_id),
    )
    previous_current = contract.get("current_revision_id")
    if previous_current and previous_current != revision_id:
        await db.execute(
            "UPDATE paper_contract_revisions SET superseded_by_revision_id = ? WHERE id = ?",
            (revision_id, previous_current),
        )
    await db.execute(
        "UPDATE paper_contracts SET current_revision_id = ?, status = 'active', updated_at = ? "
        "WHERE id = ? AND project_id = ?",
        (revision_id, now, contract["id"], project_id),
    )
    await db.commit()
    updated = await get_paper_contract_revision(db, project_id, revision_id)
    assert updated is not None
    return updated
