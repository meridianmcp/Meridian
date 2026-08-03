"""Fail-closed completion-evidence verification — meridian/sprint_evidence_guard.py

5fe3502e — ``complete_sprint_item``'s existing evidence checks
(``required_notes``'s existence-only gate, ``_check_stored_evidence``'s
mechanical disk/DB check) are ADVISORY: a missing or bogus evidence
declaration produces a warning string on the returned item, but completion
always proceeds. That is a structural gap for any caller that actually wants
a hard guarantee: nothing stops an item from being marked ``done`` on the
strength of evidence that does not exist, existed once but is stale, or
belongs to a different git worktree entirely.

This module is the OPT-IN, fail-closed alternative — same design contract as
the already-merged ``meridian.worktree_merge_guard`` (eb2e44f8):

* :func:`verify_strict_completion_evidence` never raises for an expected
  validation failure. Every rejection reason comes back as a structured
  ``{"code", "message"}`` entry in the result's ``errors`` list so a caller
  gets a clean, machine-readable rejection instead of a raw exception. Only a
  genuinely unexpected error (a DB failure, not an evidence problem) would
  propagate, and every check below is individually wrapped so a single
  unexpected failure degrades that ONE check to "skipped", not "everything
  aborts".
* Distinct, typed failure codes (the caller "needs to know WHICH failure mode
  occurred" — spec 5fe3502e point 2): ``EVIDENCE_ABSENT`` (nothing was
  declared at all), ``EVIDENCE_INVALID`` (something was declared but does not
  resolve to anything real), ``EVIDENCE_STALE`` (it resolves, but predates
  the current claim — leftover evidence from an earlier pass at the item),
  ``WRONG_WORKTREE`` (it resolves in the main checkout but not in the
  session's own registered worktree), ``UNCLAIMED_EDIT`` (a file was modified
  without a claim_file/claim_symbol lock). These are independent checks — a
  single completion can accumulate more than one.
* **Self-hosted-only for the git/filesystem-touching checks** (WRONG_WORKTREE,
  UNCLAIMED_EDIT), per the same local-fs-access architectural law
  ``worktree_merge_guard`` already follows (workspace decision 0dedff91): a
  hosted multi-tenant server has no access to a caller's own checkout.
  Callers pass ``repo_root=None`` in that case; those two checks are then
  skipped (not reported as failures — "unverifiable" is not "failed").
* **Opt-in, never on by default.** This module is never called from
  ``db.complete_sprint_item``'s default path. It is invoked ONLY by
  ``meridian.mcp.handlers.sprint_tools.handle_complete_sprint_item`` when the
  caller explicitly passes ``strict_evidence=true`` (or the item itself has
  ``require_strict_evidence`` set) — mirroring exactly how eb2e44f8's worktree
  merge guard only engages when a worktree opted into a persisted manifest.
  A caller that never asks for strict verification sees zero behavior change.
* **Overrides are audited, never silent.** :func:`record_strict_evidence_override`
  writes an ``action_audit_log`` row (who, when via ``created_at``, why via the
  required ``reason``) — see ``meridian.db.workspace.record_action_audit_event``,
  the same append-only audit table already used for other discretionary
  security-relevant actions (manual-issue-screening toggles, velocity
  anomalies). Raises ``ValueError`` if ``reason`` is empty: an override with no
  stated reason is not auditable and is refused outright, not silently
  defaulted to some placeholder text.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from . import worktree_cleanup

logger = logging.getLogger(__name__)

# Typed evidence-failure codes — kept as module-level constants so callers
# (and tests) can reference them instead of hardcoding strings.
EVIDENCE_ABSENT = "EVIDENCE_ABSENT"
EVIDENCE_INVALID = "EVIDENCE_INVALID"
EVIDENCE_STALE = "EVIDENCE_STALE"
WRONG_WORKTREE = "WRONG_WORKTREE"
UNCLAIMED_EDIT = "UNCLAIMED_EDIT"

#: event_type recorded in action_audit_log for an audited override.
OVERRIDE_EVENT_TYPE = "sprint_item_strict_evidence_override"


def _declared_evidence_paths(item: dict[str, Any]) -> list[str]:
    """Extract file paths from an item's ``touches_resources`` declaration.

    Mirrors the ``file:``/``symbol:``/``inferred:`` prefix handling in
    ``db.sprint_items._check_stored_evidence`` (kept independent rather than
    imported, so this module has no dependency on that advisory function's
    internals — only on the public, stable ``parse_touches_resources``).
    """
    resources_raw = item.get("touches_resources")
    if not resources_raw:
        return []
    from . import db as db_module  # noqa: PLC0415 — avoid import cycle at module load

    resources = db_module.parse_touches_resources(resources_raw)
    paths: list[str] = []
    for rid in resources:
        rid_norm = rid[len("inferred:"):] if rid.lower().startswith("inferred:") else rid
        if rid_norm.startswith("file:"):
            paths.append(rid_norm[len("file:"):])
        elif rid_norm.startswith("symbol:"):
            paths.append(rid_norm[len("symbol:"):].partition("::")[0])
    return paths


def _has_any_declared_evidence(
    item: dict[str, Any], task_id: str | None, notes: str | None
) -> bool:
    return bool(
        _declared_evidence_paths(item)
        or task_id
        or item.get("task_id")
        or (notes or "").strip()
        or (item.get("notes") or "").strip()
    )


def _check_absent(
    item: dict[str, Any], task_id: str | None, notes: str | None
) -> dict[str, str] | None:
    if _has_any_declared_evidence(item, task_id, notes):
        return None
    return {
        "code": EVIDENCE_ABSENT,
        "message": (
            "no evidence at all is declared for this completion — no "
            "touches_resources file/symbol entries, no linked task_id, and no "
            "notes (argument or already stored on the item). Strict mode "
            "requires at least one verifiable evidence source before a "
            "completion is allowed to stick."
        ),
    }


async def _check_invalid_and_stale(
    db: Any,
    item: dict[str, Any],
    task_id: str | None,
    notes: str | None,
    claimed_at_dt: "datetime | None",
) -> list[dict[str, str]]:
    """EVIDENCE_INVALID / EVIDENCE_STALE — only meaningful once something WAS
    declared (the ABSENT check already covers "nothing declared at all")."""
    errors: list[dict[str, str]] = []

    # -- touches_resources-declared files --------------------------------
    paths = _declared_evidence_paths(item)
    if paths:
        try:
            existing = [p for p in paths if os.path.exists(p)]
        except Exception:  # noqa: BLE001 — never let a bad path crash the gate
            existing = []
        if not existing:
            absent = paths[:3]
            more = f" (and {len(paths) - 3} more)" if len(paths) > 3 else ""
            errors.append({
                "code": EVIDENCE_INVALID,
                "message": (
                    f"{len(paths)} file(s) declared in touches_resources cannot "
                    f"be found on disk: {', '.join(absent)}{more}. Strict mode "
                    "refuses to accept evidence that cannot be verified."
                ),
            })
        elif claimed_at_dt is not None:
            # None of the files that DO exist were touched since the claim
            # began -> this looks like leftover evidence, not proof of the
            # current work session.
            stale = True
            for p in existing:
                try:
                    mtime = datetime.utcfromtimestamp(os.path.getmtime(p))
                except OSError:
                    continue
                if mtime >= claimed_at_dt:
                    stale = False
                    break
            if stale:
                errors.append({
                    "code": EVIDENCE_STALE,
                    "message": (
                        "declared touches_resources file(s) exist but none have "
                        f"been modified since this item was claimed "
                        f"({item.get('claimed_at')}) — this looks like leftover "
                        "evidence from before the current work session, not "
                        "proof of this session's work."
                    ),
                })

    # -- linked task_id ----------------------------------------------------
    effective_task_id = task_id or item.get("task_id")
    if effective_task_id:
        from . import db as db_module  # noqa: PLC0415

        try:
            task_row = await db_module.get_task(db, effective_task_id)
        except Exception:  # noqa: BLE001 — a DB hiccup is not itself evidence of fraud
            task_row = None
        if task_row is None:
            errors.append({
                "code": EVIDENCE_INVALID,
                "message": (
                    f"task_id {effective_task_id!r} is linked as evidence but no "
                    "matching task_log row was found. The task may have been "
                    "deleted or the id may be incorrect."
                ),
            })
        elif claimed_at_dt is not None:
            from .db.sprint_items import _parse_deferral_ts  # noqa: PLC0415

            task_created = _parse_deferral_ts(task_row.get("created_at"))
            if task_created is not None and task_created < claimed_at_dt:
                errors.append({
                    "code": EVIDENCE_STALE,
                    "message": (
                        f"linked task_id {effective_task_id!r} was logged at "
                        f"{task_row.get('created_at')}, BEFORE this item was "
                        f"claimed ({item.get('claimed_at')}) — it predates the "
                        "current claim and cannot attest to work done during it."
                    ),
                })

    return errors


async def _check_wrong_worktree(
    db: Any,
    repo_root: "Path | None",
    session_id: str | None,
    item: dict[str, Any],
) -> dict[str, str] | None:
    """WRONG_WORKTREE — declared evidence files resolve against the SERVER's
    own checkout (``repo_root``) but not against the session's OWN registered
    worktree. Self-hosted only (needs ``repo_root``); skipped, not failed,
    when unverifiable (hosted mode, no session_id, no registered worktree)."""
    if repo_root is None or not session_id:
        return None
    paths = _declared_evidence_paths(item)
    if not paths:
        return None
    from . import db as db_module  # noqa: PLC0415

    try:
        wt = await db_module.get_active_worktree_for_session(db, session_id)
    except Exception:  # noqa: BLE001
        return None
    if not wt:
        return None
    try:
        wt_abs = worktree_cleanup.resolve_worktree_disk_path(repo_root, wt["path"])
    except Exception:  # noqa: BLE001
        return None
    if not wt_abs.exists():
        return None

    repo_root_path = Path(repo_root)
    mismatched: list[str] = []
    for p in paths:
        try:
            in_worktree = (wt_abs / p).exists()
            in_repo_root = (repo_root_path / p).exists()
        except Exception:  # noqa: BLE001
            continue
        if in_repo_root and not in_worktree:
            mismatched.append(p)
    if not mismatched:
        return None
    absent = mismatched[:3]
    more = f" (and {len(mismatched) - 3} more)" if len(mismatched) > 3 else ""
    return {
        "code": WRONG_WORKTREE,
        "message": (
            f"{len(mismatched)} declared evidence file(s) exist in the main "
            f"checkout ({repo_root_path}) but NOT in session {session_id!r}'s "
            f"registered worktree ({wt_abs}): {', '.join(absent)}{more}. This "
            "looks like evidence from a different checkout than the one this "
            "session's worktree claim is for."
        ),
    }


async def _check_unclaimed_edits(
    db: Any,
    repo_root: "Path | None",
    session_id: str | None,
) -> dict[str, str] | None:
    """UNCLAIMED_EDIT — files modified in the repo without a claim_file lock
    held by this session. Self-contained re-implementation of
    ``mcp.handler._unclaimed_file_warnings``'s git-diff technique (not
    imported directly, to keep this module free of any dependency on the
    mcp/handler layer) — the one difference is this version passes an
    explicit ``cwd=repo_root`` to the git subprocess rather than relying on
    the server process's ambient working directory, so it stays correct
    regardless of what directory the server happens to have been launched
    from. Self-hosted only; skipped (not failed) when unverifiable."""
    if repo_root is None or not session_id:
        return None
    try:
        cur = await db.execute(
            "SELECT file_path FROM file_locks WHERE session_id = ?", (session_id,)
        )
        rows = await cur.fetchall()
        claimed = {(r["file_path"] if isinstance(r, dict) else r[0]) for r in rows}

        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--name-only", "HEAD",
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        unstaged = set(stdout.decode().splitlines()) if stdout else set()

        proc2 = await asyncio.create_subprocess_exec(
            "git", "diff", "--name-only", "--cached",
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=5.0)
        staged = set(stdout2.decode().splitlines()) if stdout2 else set()

        modified = {p for p in (unstaged | staged) if p}
        unclaimed = sorted(modified - claimed)
    except Exception:  # noqa: BLE001 — unverifiable must never crash the gate
        return None
    if not unclaimed:
        return None
    absent = unclaimed[:5]
    more = f" (and {len(unclaimed) - 5} more)" if len(unclaimed) > 5 else ""
    return {
        "code": UNCLAIMED_EDIT,
        "message": (
            f"{len(unclaimed)} file(s) were modified without a claim_file/"
            f"claim_symbol lock held by session {session_id!r}: "
            f"{', '.join(absent)}{more}. Another session may have conflicted "
            "with this work."
        ),
    }


async def verify_strict_completion_evidence(
    db: Any,
    repo_root: "Path | None",
    project_id: str,
    item_id: str,
    item: dict[str, Any],
    *,
    task_id: str | None = None,
    notes: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """The fail-closed pre-completion gate. Never raises for an expected
    validation failure — see the module docstring for the full contract.

    Returns ``{"ok": bool, "project_id", "item_id", "errors": [{"code",
    "message"}, ...]}``. ``ok`` is True only when there are zero entries in
    ``errors``. Every check that fires contributes its own entry — several
    can co-occur (e.g. EVIDENCE_STALE and UNCLAIMED_EDIT are independent).
    """
    from .db.sprint_items import _parse_deferral_ts  # noqa: PLC0415

    errors: list[dict[str, str]] = []
    try:
        claimed_at_dt = _parse_deferral_ts(item.get("claimed_at"))
    except Exception:  # noqa: BLE001
        claimed_at_dt = None

    absent = _check_absent(item, task_id, notes)
    if absent is not None:
        errors.append(absent)
    else:
        try:
            errors.extend(
                await _check_invalid_and_stale(db, item, task_id, notes, claimed_at_dt)
            )
        except Exception:  # noqa: BLE001 — an unexpected failure here must not crash the gate
            logger.warning("sprint_evidence_guard: invalid/stale check failed", exc_info=True)

    try:
        wrong_wt = await _check_wrong_worktree(db, repo_root, session_id, item)
    except Exception:  # noqa: BLE001
        wrong_wt = None
    if wrong_wt is not None:
        errors.append(wrong_wt)

    try:
        unclaimed = await _check_unclaimed_edits(db, repo_root, session_id)
    except Exception:  # noqa: BLE001
        unclaimed = None
    if unclaimed is not None:
        errors.append(unclaimed)

    return {
        "ok": not errors,
        "project_id": project_id,
        "item_id": item_id,
        "errors": errors,
    }


async def record_strict_evidence_override(
    db: Any,
    project_id: str,
    item_id: str,
    *,
    actor: str | None,
    reason: str | None,
    errors: list[dict[str, str]],
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Audit-log an explicit override of a strict-evidence rejection.

    5fe3502e point 3 — "an explicit override must be auditable (who
    overrode, when, why) and CANNOT be the default". ``reason`` is REQUIRED
    and must be non-empty: an override with no stated reason is refused
    outright (``ValueError``), never silently accepted or defaulted to a
    generic placeholder — that would defeat the point of an audit trail.

    Writes to ``action_audit_log`` (``event_type="sprint_item_strict_evidence_
    override"``) via the existing, already-audited
    ``db.record_action_audit_event`` (cd495afa / d86d70a5) — the same
    append-only table used for other discretionary security-relevant actions.
    ``created_at`` (when) and ``actor`` (who) are recorded by that helper;
    ``detail`` (why + what was overridden) is a JSON blob here.
    """
    _reason = (reason or "").strip()
    if not _reason:
        raise ValueError(
            "override_reason is required and must be non-empty to override a "
            "strict-evidence rejection — an override with no stated reason is "
            "not auditable and is refused."
        )
    from . import db as db_module  # noqa: PLC0415

    detail = json.dumps({
        "item_id": item_id,
        "reason": _reason,
        "error_codes": sorted({e.get("code") for e in errors if e.get("code")}),
        "errors": errors,
    })
    return await db_module.record_action_audit_event(
        db, OVERRIDE_EVENT_TYPE,
        tenant_id=tenant_id, project_id=project_id,
        actor=actor, detail=detail,
    )
