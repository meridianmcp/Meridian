"""bbb447ec — immutable, queryable wave-completion summaries, keyed by wave_id.

Why this exists (investigation-derived follow-up from Wave 3): ``wave_runs``
(2a654cb0) gives a wave a durable identity, state machine, and append-only
event history; ``board_snapshot`` (ef665ef8) gives it an immutable board
revision to plan/finalize against. Neither answers the planner's actual
question after a wave finishes: *"what happened to wave-3, for this project,
this version — which items completed vs. were blocked/failed/skipped/left
unverified, what did it change, did the tests actually pass, and can I trust
this without re-reading executor prose?"* Today that answer only exists as
free text in a handoff body or a session's task log — never a queryable,
structurally-typed record a planner (or another executor) can request by
``wave_id`` and get back the SAME authoritative answer every time.

This module is a thin persistence/reporting layer ON TOP of ``wave_runs`` and
``board_snapshot`` — it does not replace or re-implement either. A caller
(typically ``finalize_wave_run``'s caller, or a planner-side wrap-up step)
supplies the already-computed facts; this module's only job is to store them
immutably, keyed for retrieval by ``(project_id, version, wave_id)``, and
to let a correction be recorded without ever mutating the original record.

Design, mirroring precedent already established elsewhere in this codebase
rather than inventing a new shape:

* **Outcome is an explicit enum, never inferred.** :data:`WAVE_SUMMARY_ITEM_OUTCOMES`
  is the closed set every ``items[].outcome`` must belong to
  (``completed``/``blocked``/``failed``/``skipped``/``unverified``).
  :func:`_validate_summary_items` rejects anything else with an actionable
  ``ValueError`` — the same fail-closed discipline
  ``wave_runs._validate_finalizer_evidence`` uses for test evidence. Nothing
  in this module ever derives an outcome by scanning a narrative string.

* **Test receipts are structured evidence, not a self-report.** Each entry
  in ``test_receipts`` must carry ``command`` (str), ``exit_code`` (int),
  ``passed`` (int), and ``failed`` (int); ``scope`` defaults to
  ``'targeted'`` and must be ``'targeted'`` or ``'full'`` when given.
  :func:`_validate_test_receipts` rejects a malformed entry outright — same
  "structured evidence, fail closed" contract as
  ``wave_runs._validate_finalizer_evidence``, scaled down to per-receipt
  granularity (a wave summary may legitimately carry BOTH a targeted run
  during the wave and a full-suite run at the end).

* **Append-only corrections via superseded_by, never mutation of content.**
  Exactly the ``wave_run_events.supersede_wave_run_event`` pattern:
  :func:`record_wave_summary_correction` appends a NEW row (inheriting the
  original's identity — project/version/wave_id/wave_run_id/session_id — and
  defaulting every content field to the original's value unless the caller
  overrides it), then sets ONLY the original row's ``superseded_by`` pointer.
  The original's content columns are never touched again. A correction
  targeting an already-superseded row is refused (linear chains only, same
  rationale as ``supersede_wave_run_event``).

* **Retrieval returns the chain tip, scoped for project/version isolation.**
  :func:`get_wave_summary` is the ``get_wave_summary(project_id, wave_id)``
  primitive the sprint item asks for — it returns the newest
  non-superseded row for a ``(project_id, version_filter, wave_id)`` bucket
  (optionally further narrowed to one ``wave_run_id``), i.e. "the authoritative
  record after every correction so far." ``version_filter`` uses the SAME
  ``''`` = unscoped-bucket convention as
  ``board_snapshot_revisions.version_filter`` — a summary recorded under one
  ``(project_id, version)`` bucket can never be returned by a query scoped to
  a different one, including a different project entirely (project_id is
  always an equality predicate, never inferred). "Newest" is decided by a
  monotonic per-bucket ``seq`` column (:func:`_next_summary_seq`), never by
  ``created_at``/``id`` — the same ``wave_run_events.seq`` reasoning applies
  verbatim: ``datetime('now')`` is only second-granular and ``id`` is a
  random UUID, so neither is a safe "which row is newer" tiebreaker.

* **wave_id matches the existing "wave" vocabulary.** Sprint items already
  carry a ``wave`` field (e.g. ``"wave-5"``, set by ``assign_sprint_waves``)
  and ``wave_gate_results``/``wave_gate_configs`` already key on that same
  string as ``wave_label``/``wave_end``. This module calls the parameter
  ``wave_id`` (matching the sprint-item spec's own wording) but it is the
  IDENTICAL value — no new identifier scheme, no translation table.

* **Deterministic content hash, same recipe as executor_reports/board_snapshot.**
  :func:`canonical_wave_summary_hash` hashes ONLY content fields (never
  ``id``/``supersedes``/``superseded_by``/``created_at``) via
  ``board_snapshot.canonical_json`` (sorted keys, compact separators,
  ASCII-safe) — two independently-persisted summaries with byte-identical
  evidence hash identically, and reading the SAME row twice is byte-stable by
  construction (nothing in the row is wall-clock-derived at read time).

CREATE_TABLES covers fresh DBs (N/A here — this table is migration-only, no
inline literal, per the 2026-07-04 outage rule); this module's own
``_migrate_wave_run_summaries`` is the upgrade path for existing DBs, mirrors
the ``executor_reports.py`` / ``verification_runs.py`` precedent of a
feature module owning its own migration rather than living in
``migrations.py``. Every index lives INSIDE the guarded migration. Idempotent.
Mirrored in ``pg_adapter._migrate_pg_wave_run_summaries``.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import aiosqlite

from meridian.db import _new_id, _row_to_dict, canonical_json  # noqa: PLC0415

#: Explicit, closed outcome enum for a wave summary's per-item results.
#: Never inferred from narrative text — every ``items[].outcome`` MUST be one
#: of these five values or :func:`persist_wave_summary`/
#: :func:`record_wave_summary_correction` raises ``ValueError``.
WAVE_SUMMARY_ITEM_OUTCOMES: frozenset[str] = frozenset({
    "completed",   # the item shipped and its evidence gate (if any) passed
    "blocked",     # could not proceed — a dependency/resource/HITL blocker
    "failed",      # attempted and did not succeed (test failure, exception, ...)
    "skipped",     # deliberately not attempted this wave (deferred, descoped)
    "unverified",  # attempted, outcome unknown/unconfirmed — never silently 'completed'
})

#: Valid ``test_receipts[].scope`` values — a wave summary may carry both a
#: targeted (only the items touched) and a full-suite receipt.
WAVE_SUMMARY_TEST_SCOPES: frozenset[str] = frozenset({"targeted", "full"})

_JSON_LIST_FIELDS: tuple[str, ...] = (
    "items", "commits", "changed_resources", "test_receipts",
    "blockers", "exclusions", "tool_availability",
)


# ---------------------------------------------------------------------------
# JSON helpers (self-contained per this codebase's per-module convention —
# see board_snapshot.py / wave_runs.py / executor_reports.py, each of which
# defines its own tiny JSON helpers rather than sharing a central one).
# ---------------------------------------------------------------------------

def _dump_json_list(value: Any) -> str:
    """Canonical JSON for a list-shaped field. Non-list input degrades to
    ``'[]'`` rather than raising — these columns are NOT NULL DEFAULT '[]'."""
    return json.dumps(value if isinstance(value, list) else [], sort_keys=True, default=str)


def _load_json_or_default(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _hydrate_summary(row: Any) -> "dict[str, Any] | None":
    d = _row_to_dict(row)
    if d is None:
        return None
    for field in _JSON_LIST_FIELDS:
        d[field] = _load_json_or_default(d.get(field), [])
    return d


# ---------------------------------------------------------------------------
# Validation — fail closed, explicit enum, never inferred from prose.
# ---------------------------------------------------------------------------

def _validate_summary_items(items: Any) -> list[dict[str, Any]]:
    """Validate ``items`` — a list of ``{item_id, outcome, ...}``.

    Every entry must have a non-empty ``item_id`` and an ``outcome`` drawn
    from :data:`WAVE_SUMMARY_ITEM_OUTCOMES`. Extra per-item fields (e.g.
    ``detail``, ``commit_sha``) pass through unchanged. Raises ``ValueError``
    naming the offending entry — never silently drops or coerces a bad
    outcome to a guessed value.
    """
    if not isinstance(items, list):
        raise ValueError(
            "persist_wave_summary requires items to be a list of "
            "{'item_id': ..., 'outcome': ...} dicts."
        )
    validated: list[dict[str, Any]] = []
    for entry in items:
        if not isinstance(entry, dict):
            raise ValueError(f"Each wave-summary item must be a dict; got {entry!r}.")
        item_id = entry.get("item_id")
        if not item_id or not isinstance(item_id, str):
            raise ValueError(f"Wave-summary item missing a non-empty 'item_id': {entry!r}.")
        outcome = entry.get("outcome")
        if outcome not in WAVE_SUMMARY_ITEM_OUTCOMES:
            raise ValueError(
                f"Wave-summary item {item_id!r} has invalid outcome {outcome!r}. "
                f"Must be one of {sorted(WAVE_SUMMARY_ITEM_OUTCOMES)} — an explicit "
                "enum value, never inferred from narrative text."
            )
        validated.append(dict(entry))
    # Canonical, deterministic ordering — same field, independent of caller
    # submission order, so two submissions of the same logical item set hash
    # and serialize identically (matches board_snapshot's own ordering discipline).
    validated.sort(key=lambda e: str(e.get("item_id") or ""))
    return validated


def _validate_test_receipts(receipts: Any) -> list[dict[str, Any]]:
    """Validate ``test_receipts`` — a list of structured evidence records.

    Each entry must carry ``command`` (str), ``exit_code`` (int), ``passed``
    (int), and ``failed`` (int). ``scope`` defaults to ``'targeted'`` and, if
    given, must be one of :data:`WAVE_SUMMARY_TEST_SCOPES`. A self-reported
    boolean or a missing field is rejected outright — the same "structured
    evidence, fail closed" contract ``wave_runs._validate_finalizer_evidence``
    uses for the wave-run finalizer, scaled to per-receipt granularity.
    """
    if receipts is None:
        return []
    if not isinstance(receipts, list):
        raise ValueError(
            "test_receipts must be a list of {'command', 'exit_code', 'passed', "
            "'failed', 'scope'?} dicts — a self-report or a bare boolean is rejected."
        )
    validated: list[dict[str, Any]] = []
    for entry in receipts:
        if not isinstance(entry, dict):
            raise ValueError(f"Each test receipt must be a dict; got {entry!r}.")
        command = entry.get("command")
        if not command or not isinstance(command, str):
            raise ValueError(f"Test receipt missing a non-empty 'command': {entry!r}.")
        exit_code = entry.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise ValueError(
                f"Test receipt for {command!r} must carry an integer 'exit_code'; "
                f"got {exit_code!r}."
            )
        passed = entry.get("passed")
        failed = entry.get("failed")
        if not isinstance(passed, int) or isinstance(passed, bool):
            raise ValueError(
                f"Test receipt for {command!r} must carry an integer 'passed' count; "
                f"got {passed!r}."
            )
        if not isinstance(failed, int) or isinstance(failed, bool):
            raise ValueError(
                f"Test receipt for {command!r} must carry an integer 'failed' count; "
                f"got {failed!r}."
            )
        scope = entry.get("scope", "targeted")
        if scope not in WAVE_SUMMARY_TEST_SCOPES:
            raise ValueError(
                f"Test receipt for {command!r} has invalid scope {scope!r}. "
                f"Must be one of {sorted(WAVE_SUMMARY_TEST_SCOPES)}."
            )
        receipt = dict(entry)
        receipt["scope"] = scope
        validated.append(receipt)
    return validated


def canonical_wave_summary_hash(payload: dict[str, Any]) -> str:
    """Deterministic ``sha256:<hex>`` over a summary's CONTENT fields only —
    excludes ``id``/``supersedes``/``superseded_by``/``created_at`` bookkeeping,
    same "content, not identity" discipline as
    ``executor_reports.canonical_report_hash`` /
    ``board_snapshot._compute_revision_hash``. Uses ``board_snapshot.canonical_json``
    (sorted keys, compact separators, ASCII-safe) so this is byte-stable
    across processes.
    """
    tracked = {
        k: payload.get(k)
        for k in (
            "project_id", "version_filter", "wave_id", "wave_run_id", "session_id",
            "board_revision_hash", "handoff_status", *_JSON_LIST_FIELDS,
        )
    }
    digest = hashlib.sha256(canonical_json(tracked).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

async def _migrate_wave_run_summaries(db: aiosqlite.Connection) -> None:
    """bbb447ec — create ``wave_run_summaries`` on existing SQLite DBs.

    One row per persisted summary OR correction (never updated after insert,
    save for the ``superseded_by`` pointer a later correction sets on the row
    it corrects). Guarded migration — table + every index live here, never
    inline in an unguarded CREATE_TABLES literal (2026-07-04 outage rule).
    Idempotent. Mirrored in ``pg_adapter._migrate_pg_wave_run_summaries``.
    """
    await db.execute(
        """CREATE TABLE IF NOT EXISTS wave_run_summaries (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            version_filter TEXT NOT NULL DEFAULT '',
            wave_id TEXT NOT NULL,
            wave_run_id TEXT,
            session_id TEXT,
            board_revision_hash TEXT,
            items TEXT NOT NULL DEFAULT '[]',
            commits TEXT NOT NULL DEFAULT '[]',
            changed_resources TEXT NOT NULL DEFAULT '[]',
            test_receipts TEXT NOT NULL DEFAULT '[]',
            blockers TEXT NOT NULL DEFAULT '[]',
            exclusions TEXT NOT NULL DEFAULT '[]',
            tool_availability TEXT NOT NULL DEFAULT '[]',
            handoff_status TEXT,
            summary_hash TEXT,
            actor TEXT,
            supersedes TEXT,
            superseded_by TEXT,
            correction_reason TEXT,
            seq INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_wave_run_summaries_lookup "
        "ON wave_run_summaries(project_id, version_filter, wave_id, seq DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_wave_run_summaries_run "
        "ON wave_run_summaries(wave_run_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_wave_run_summaries_supersedes "
        "ON wave_run_summaries(supersedes)"
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

async def _next_summary_seq(
    db: aiosqlite.Connection, project_id: str, version_filter: str, wave_id: str,
) -> int:
    """Next monotonic per-bucket sequence number (1-based).

    Exists for the SAME reason ``wave_runs._next_event_seq`` exists: this
    table's ``created_at`` is only second-granular (SQLite ``datetime('now')``)
    and ``id`` is a random UUID (``_new_id``, uuid4) — neither is a safe
    "which row is newer" tiebreaker when two rows land in the same second, so
    :func:`get_wave_summary`/:func:`get_wave_summary_history` order by this
    column instead. Scoped per ``(project_id, version_filter, wave_id)``
    bucket (not globally) so it reads as "the Nth record in this bucket's
    history," mirroring ``seq`` being scoped per ``wave_run_id`` there.
    """
    async with db.execute(
        "SELECT MAX(seq) FROM wave_run_summaries "
        "WHERE project_id = ? AND version_filter = ? AND wave_id = ?",
        (project_id, version_filter, wave_id),
    ) as cur:
        row = await cur.fetchone()
    current = None
    if row is not None:
        current = row[0] if not isinstance(row, dict) else list(row.values())[0]
    return int(current or 0) + 1


async def persist_wave_summary(
    db: aiosqlite.Connection,
    project_id: str,
    wave_id: str,
    *,
    items: "list[dict[str, Any]]",
    version: "str | None" = None,
    wave_run_id: "str | None" = None,
    session_id: "str | None" = None,
    board_revision_hash: "str | None" = None,
    commits: "list[Any] | None" = None,
    changed_resources: "list[Any] | None" = None,
    test_receipts: "list[dict[str, Any]] | None" = None,
    blockers: "list[Any] | None" = None,
    exclusions: "list[Any] | None" = None,
    tool_availability: "list[Any] | None" = None,
    handoff_status: "str | None" = None,
    actor: "str | None" = None,
) -> dict[str, Any]:
    """Persist a NEW immutable wave-completion summary.

    ``wave_id`` is the SAME value as ``sprint_items.wave`` /
    ``wave_gate_results.wave_label`` (e.g. ``"wave-5"``) — no new identifier
    scheme. ``items`` is validated against :data:`WAVE_SUMMARY_ITEM_OUTCOMES`
    (:func:`_validate_summary_items`) and ``test_receipts`` against the
    structured-evidence contract (:func:`_validate_test_receipts`) — both
    raise ``ValueError`` on anything malformed rather than silently storing
    it. This function only ever INSERTs; to correct an existing summary use
    :func:`record_wave_summary_correction` — never call this again for the
    same wave and expect an update.
    """
    validated_items = _validate_summary_items(items)
    validated_receipts = _validate_test_receipts(test_receipts)
    version_filter = version or ""

    payload = {
        "project_id": project_id,
        "version_filter": version_filter,
        "wave_id": wave_id,
        "wave_run_id": wave_run_id,
        "session_id": session_id,
        "board_revision_hash": board_revision_hash,
        "items": validated_items,
        "commits": commits or [],
        "changed_resources": changed_resources or [],
        "test_receipts": validated_receipts,
        "blockers": blockers or [],
        "exclusions": exclusions or [],
        "tool_availability": tool_availability or [],
        "handoff_status": handoff_status,
    }
    summary_hash = canonical_wave_summary_hash(payload)
    seq = await _next_summary_seq(db, project_id, version_filter, wave_id)

    sid = _new_id()
    await db.execute(
        "INSERT INTO wave_run_summaries ("
        "id, project_id, version_filter, wave_id, wave_run_id, session_id, "
        "board_revision_hash, items, commits, changed_resources, test_receipts, "
        "blockers, exclusions, tool_availability, handoff_status, summary_hash, "
        "actor, seq"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sid, project_id, version_filter, wave_id, wave_run_id, session_id,
            board_revision_hash, _dump_json_list(validated_items),
            _dump_json_list(commits), _dump_json_list(changed_resources),
            _dump_json_list(validated_receipts), _dump_json_list(blockers),
            _dump_json_list(exclusions), _dump_json_list(tool_availability),
            handoff_status, summary_hash, actor, seq,
        ),
    )
    await db.commit()
    created = await get_wave_summary_by_id(db, sid)
    assert created is not None  # just inserted
    return created


async def get_wave_summary_by_id(
    db: aiosqlite.Connection, summary_id: str,
) -> "dict[str, Any] | None":
    """Fetch a single summary row by id, JSON fields decoded."""
    async with db.execute(
        "SELECT * FROM wave_run_summaries WHERE id = ?", (summary_id,),
    ) as cur:
        row = await cur.fetchone()
    return _hydrate_summary(row)


async def get_wave_summary(
    db: aiosqlite.Connection,
    project_id: str,
    wave_id: str,
    *,
    version: "str | None" = None,
    wave_run_id: "str | None" = None,
) -> "dict[str, Any] | None":
    """Return the AUTHORITATIVE (chain-tip, non-superseded) summary for one
    ``(project_id, version, wave_id)`` bucket — the ``get_wave_summary``
    primitive the sprint item asks for.

    Project/version isolation is unconditional: ``project_id`` is always an
    equality predicate (never inferred, never cross-project), and
    ``version`` (default unscoped, ``''`` bucket) uses the same
    ``board_snapshot_revisions.version_filter`` convention — a summary
    recorded for one version bucket is never returned for another. Pass
    ``wave_run_id`` to further narrow to one specific run/attempt of this
    wave; omitted, the newest chain tip across every run of this wave_id
    wins (the common "what's the current answer for this wave" query).
    Returns ``None`` when no summary has ever been recorded for the bucket.
    """
    version_filter = version or ""
    clauses = [
        "project_id = ?", "version_filter = ?", "wave_id = ?",
        "superseded_by IS NULL",
    ]
    params: list[Any] = [project_id, version_filter, wave_id]
    if wave_run_id is not None:
        clauses.append("wave_run_id = ?")
        params.append(wave_run_id)
    sql = (
        f"SELECT * FROM wave_run_summaries WHERE {' AND '.join(clauses)} "
        "ORDER BY seq DESC LIMIT 1"
    )
    async with db.execute(sql, tuple(params)) as cur:
        row = await cur.fetchone()
    return _hydrate_summary(row)


async def get_wave_summary_history(
    db: aiosqlite.Connection,
    project_id: str,
    wave_id: str,
    *,
    version: "str | None" = None,
    wave_run_id: "str | None" = None,
) -> "list[dict[str, Any]]":
    """Return EVERY summary/correction row for one ``(project_id, version,
    wave_id)`` bucket, oldest first — the full audit trail, superseded rows
    included (mirrors ``get_wave_run_events(include_superseded=True)``'s
    "the audit trail is the point" default). Same project/version isolation
    as :func:`get_wave_summary`.
    """
    version_filter = version or ""
    clauses = ["project_id = ?", "version_filter = ?", "wave_id = ?"]
    params: list[Any] = [project_id, version_filter, wave_id]
    if wave_run_id is not None:
        clauses.append("wave_run_id = ?")
        params.append(wave_run_id)
    sql = (
        f"SELECT * FROM wave_run_summaries WHERE {' AND '.join(clauses)} "
        "ORDER BY seq"
    )
    async with db.execute(sql, tuple(params)) as cur:
        rows = await cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        h = _hydrate_summary(r)
        if h is not None:
            out.append(h)
    return out


async def record_wave_summary_correction(
    db: aiosqlite.Connection,
    original_id: str,
    *,
    items: "list[dict[str, Any]] | None" = None,
    board_revision_hash: "str | None" = None,
    commits: "list[Any] | None" = None,
    changed_resources: "list[Any] | None" = None,
    test_receipts: "list[dict[str, Any]] | None" = None,
    blockers: "list[Any] | None" = None,
    exclusions: "list[Any] | None" = None,
    tool_availability: "list[Any] | None" = None,
    handoff_status: "str | None" = None,
    actor: "str | None" = None,
    reason: "str | None" = None,
) -> dict[str, Any]:
    """Correct an earlier summary by APPENDING a new row — never mutating it.

    ``original_id`` must name an existing, NOT-yet-superseded summary row
    (mirrors ``supersede_wave_run_event``'s linear-chain rule — correct the
    newest row in the chain, not an already-corrected one). Every unset
    keyword argument here defaults to the ORIGINAL row's value, so a caller
    can submit a correction that changes just one field (e.g. only
    ``handoff_status``) without having to re-supply everything. Identity
    fields (``project_id``, ``version``, ``wave_id``, ``wave_run_id``,
    ``session_id``) are always inherited from the original and are never
    overridable here — a correction fixes WHAT happened, never WHICH wave/run
    it is about.

    Raises ``ValueError`` when ``original_id`` doesn't exist, or is already
    superseded.
    """
    original = await get_wave_summary_by_id(db, original_id)
    if original is None:
        raise ValueError(f"Wave summary {original_id!r} not found.")
    if original.get("superseded_by"):
        raise ValueError(
            f"Wave summary {original_id!r} has already been superseded by "
            f"{original['superseded_by']!r}. Correct the newest row in the "
            "chain, not an already-corrected one."
        )

    def _pick(new_value: Any, field: str) -> Any:
        return new_value if new_value is not None else original.get(field)

    validated_items = _validate_summary_items(_pick(items, "items"))
    validated_receipts = _validate_test_receipts(_pick(test_receipts, "test_receipts"))

    payload = {
        "project_id": original["project_id"],
        "version_filter": original["version_filter"],
        "wave_id": original["wave_id"],
        "wave_run_id": original.get("wave_run_id"),
        "session_id": original.get("session_id"),
        "board_revision_hash": _pick(board_revision_hash, "board_revision_hash"),
        "items": validated_items,
        "commits": _pick(commits, "commits"),
        "changed_resources": _pick(changed_resources, "changed_resources"),
        "test_receipts": validated_receipts,
        "blockers": _pick(blockers, "blockers"),
        "exclusions": _pick(exclusions, "exclusions"),
        "tool_availability": _pick(tool_availability, "tool_availability"),
        "handoff_status": _pick(handoff_status, "handoff_status"),
    }
    summary_hash = canonical_wave_summary_hash(payload)
    seq = await _next_summary_seq(
        db, payload["project_id"], payload["version_filter"], payload["wave_id"],
    )

    new_id = _new_id()
    await db.execute(
        "INSERT INTO wave_run_summaries ("
        "id, project_id, version_filter, wave_id, wave_run_id, session_id, "
        "board_revision_hash, items, commits, changed_resources, test_receipts, "
        "blockers, exclusions, tool_availability, handoff_status, summary_hash, "
        "actor, supersedes, correction_reason, seq"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id, payload["project_id"], payload["version_filter"], payload["wave_id"],
            payload["wave_run_id"], payload["session_id"], payload["board_revision_hash"],
            _dump_json_list(validated_items), _dump_json_list(payload["commits"]),
            _dump_json_list(payload["changed_resources"]),
            _dump_json_list(validated_receipts), _dump_json_list(payload["blockers"]),
            _dump_json_list(payload["exclusions"]), _dump_json_list(payload["tool_availability"]),
            payload["handoff_status"], summary_hash, actor, original_id, reason, seq,
        ),
    )
    await db.execute(
        "UPDATE wave_run_summaries SET superseded_by = ? WHERE id = ?",
        (new_id, original_id),
    )
    await db.commit()
    created = await get_wave_summary_by_id(db, new_id)
    assert created is not None
    return created
