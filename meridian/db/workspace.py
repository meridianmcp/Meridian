"""Workspace-layer persistence functions — extracted from meridian/db/__init__.py.

This module contains all functions whose primary subject is the workspace layer:
tenant-global notes, decisions, proposals, sprint-items, settings (cross-project
backlog distinct from per-project sprint_items), and workspace member/invite
management.

Imported back into meridian.db via an explicit named re-export at the bottom of
db/__init__.py so all existing ``db_module.function_name()``-style call sites are
unaffected.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

import aiosqlite

# Shared helpers from the parent db package.  These are imported at the BOTTOM
# of db/__init__.py (after the parent module defines them), then re-used here.
# Using a lazy module-attribute import via the package __init__ avoids the
# circular-import that would occur if we imported at module top-level while
# db/__init__.py is still being initialised.
from meridian.db import (  # noqa: PLC0415
    _new_id,
    _row_to_dict,
    normalize_execution_mode,
    get_project,
    add_project_note,
    serialize_touches_resources,
    _unique_proposal_slug,
    _unique_proposal_nickname,
    _sprint_item_slug_base,
    _sprint_item_nickname_base,
)


# ---------------------------------------------------------------------------
# a56f0951 — touches_resources inference for promote_workspace_proposal.
#
# When a workspace proposal is promoted to a sprint item the created item had
# zero touches_resources by construction — same gap class as fba94f1a. This
# helper mirrors the keyword-match logic in handoff._annotate_touches_files:
# extract significant words from the proposal title + body, then match them
# against recently-changed files (git diff --name-only HEAD~3) to produce
# inferred:file:<path> resource identifiers.
#
# Returns a list of inferred resource strings (may be empty on no match or
# error). Safe to call from any context — never raises.
# ---------------------------------------------------------------------------

_PROPOSAL_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "in", "on", "at", "to", "for",
    "of", "is", "it", "fix", "add", "update", "remove", "change",
    "with", "from", "by", "via", "use", "set", "get", "put", "new",
    "this", "that", "into", "as", "be", "has", "was", "not", "no",
})


def _proposal_keywords(text: str) -> set[str]:
    """Extract significant lowercase keywords from a proposal title or body.

    Mirrors handoff._extract_keywords without its extra_stop parameter:
    returns 3+-char alphanumeric/underscore/dash/slash tokens excluding the
    common English stop-words."""
    words = re.findall(r"[a-z0-9_/-]{3,}", text.lower())
    return {w for w in words if w not in _PROPOSAL_STOP_WORDS}


def _infer_touches_resources_from_proposal(title: str, body: str) -> list[str]:
    """Infer inferred:file:<path> resource identifiers for a workspace proposal.

    Queries ``git diff --name-only HEAD~3`` for recently changed files, then
    keyword-matches the proposal's title+body against each path. Returns at
    most 10 ``inferred:file:<path>`` strings, or an empty list when no match
    is found or git is unavailable. Never raises."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~3"],
            capture_output=True, text=True, timeout=5,
        )
        changed_files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
    except Exception:  # noqa: BLE001
        return []
    if not changed_files:
        return []

    combined_text = f"{title} {body}"
    title_kws = _proposal_keywords(combined_text)
    if len(title_kws) < 2:
        return []

    matched: list[str] = []
    seen: set[str] = set()
    for fpath in changed_files:
        if fpath in seen:
            continue
        fname = os.path.basename(fpath)
        fname_stem = os.path.splitext(fname)[0]
        path_kws = _proposal_keywords(fpath.replace("/", " ").replace(".", " "))
        if fname_stem and len(fname_stem) >= 3 and fname_stem in title_kws:
            matched.append(fpath)
            seen.add(fpath)
        elif len(title_kws & path_kws) >= 2:
            matched.append(fpath)
            seen.add(fpath)
    return [f"inferred:file:{fpath}" for fpath in matched[:10]]


# ---------------------------------------------------------------------------
# v3.1 — workspace layer: tenant-global notes + decisions above projects
# ---------------------------------------------------------------------------


def _ws_tenant_clause(tenant_id: str | None) -> tuple[str, list[Any]]:
    """Return an ``AND (...)`` scope fragment + params for tenant isolation.

    When ``tenant_id`` is None (self-host / internal callers) returns ('', [])
    so behaviour is unchanged. When provided, rows owned by that tenant *or*
    pre-isolation rows (``tenant_id IS NULL``, only ever present on a dedicated
    per-tenant DB) match — see ``_migrate_workspace_tenant_isolation``.
    """
    if tenant_id is None:
        return "", []
    return "(tenant_id = ? OR tenant_id IS NULL)", [tenant_id]


async def add_workspace_note(
    db: aiosqlite.Connection,
    title: str,
    body: str,
    tags: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Insert a workspace_notes row. tags is comma-separated free-form.
    Workspace notes belong to the whole workspace, not a single project."""
    nid = _new_id()
    await db.execute(
        "INSERT INTO workspace_notes (id, title, body, tags, tenant_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (nid, title, body, tags, tenant_id),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM workspace_notes WHERE id = ?", (nid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {"id": nid}


async def get_workspace_notes(
    db: aiosqlite.Connection,
    tag: str | None = None,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return workspace notes, newest first. Optional tag substring filter.
    Scoped to ``tenant_id`` when provided (hosted)."""
    clauses: list[str] = []
    params: list[Any] = []
    if tag:
        clauses.append("tags LIKE ?")
        params.append(f"%{tag}%")
    scope, scope_params = _ws_tenant_clause(tenant_id)
    if scope:
        clauses.append(scope)
        params.extend(scope_params)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with db.execute(
        f"SELECT * FROM workspace_notes{where} ORDER BY created_at DESC",
        params or None,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def delete_workspace_note(
    db: aiosqlite.Connection, note_id: str, tenant_id: str | None = None
) -> bool:
    """Hard-delete a workspace note. Returns True if a row was removed.
    Cannot delete another tenant's note when ``tenant_id`` is set."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    sql = "DELETE FROM workspace_notes WHERE id = ?" + (f" AND {scope}" if scope else "")
    async with db.execute(sql, [note_id, *scope_params]) as cur:
        rc = cur.rowcount or 0
    await db.commit()
    return rc > 0


async def move_workspace_note_to_project(
    db: aiosqlite.Connection,
    note_id: str,
    project_id: str,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Convert a workspace note into a project note on ``project_id``.

    Copies title/body/tags to a new project_notes row, then deletes the
    workspace note. Returns the new project note, or None if the workspace
    note was not found (or belongs to another tenant). Atomic: the delete
    only runs after the project note is created.
    """
    scope, scope_params = _ws_tenant_clause(tenant_id)
    sql = "SELECT * FROM workspace_notes WHERE id = ?" + (f" AND {scope}" if scope else "")
    async with db.execute(sql, [note_id, *scope_params]) as cur:
        row = await cur.fetchone()
    note = _row_to_dict(row) if row is not None else None
    if not note:
        return None
    # Guard against moving to a non-existent project.
    if await get_project(db, project_id) is None:
        return None
    created = await add_project_note(
        db,
        project_id,
        note.get("title") or "",
        note.get("body") or "",
        note.get("tags"),
    )
    await delete_workspace_note(db, note_id, tenant_id=tenant_id)
    return created


async def update_workspace_note(
    db: aiosqlite.Connection,
    note_id: str,
    title: str | None = None,
    body: str | None = None,
    tags: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Patch title/body/tags on an existing workspace note (tenant-scoped)."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    sets, params = [], []
    if title is not None:
        sets.append("title = ?"); params.append(title)
    if body is not None:
        sets.append("body = ?"); params.append(body)
    if tags is not None:
        sets.append("tags = ?"); params.append(tags)
    if not sets:
        async with db.execute(
            f"SELECT * FROM workspace_notes WHERE id = ?{scope_sql}",
            [note_id, *scope_params],
        ) as cur:
            row = await cur.fetchone()
        return _row_to_dict(row)
    await db.execute(
        f"UPDATE workspace_notes SET {', '.join(sets)} WHERE id = ?{scope_sql}",
        [*params, note_id, *scope_params],
    )
    await db.commit()
    async with db.execute(
        f"SELECT * FROM workspace_notes WHERE id = ?{scope_sql}",
        [note_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def pin_workspace_decision(
    db: aiosqlite.Connection,
    title: str,
    body: str,
    category: str = "TECHNICAL",
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Create a workspace-level pinned decision. category is free-text
    (STRATEGIC, TECHNICAL, PRODUCT, ...)."""
    did = _new_id()
    await db.execute(
        "INSERT INTO workspace_decisions (id, title, body, category, tenant_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (did, title, body, category, tenant_id),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM workspace_decisions WHERE id = ?", (did,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {"id": did}


async def get_workspace_decisions(
    db: aiosqlite.Connection,
    include_superseded: bool = False,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return workspace decisions, newest first. Active only by default.
    Scoped to ``tenant_id`` when provided (hosted)."""
    clauses: list[str] = []
    params: list[Any] = []
    if not include_superseded:
        clauses.append("status = 'active'")
    scope, scope_params = _ws_tenant_clause(tenant_id)
    if scope:
        clauses.append(scope)
        params.extend(scope_params)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with db.execute(
        f"SELECT * FROM workspace_decisions{where} ORDER BY created_at DESC",
        params or None,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def delete_workspace_decision(
    db: aiosqlite.Connection, decision_id: str, tenant_id: str | None = None
) -> bool:
    """Hard-delete a workspace decision. Returns True if a row was removed.
    Cannot delete another tenant's decision when ``tenant_id`` is set."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    sql = "DELETE FROM workspace_decisions WHERE id = ?" + (f" AND {scope}" if scope else "")
    async with db.execute(sql, [decision_id, *scope_params]) as cur:
        rc = cur.rowcount or 0
    await db.commit()
    return rc > 0


# --- Workspace proposals (human-only "drawer of inspiration") ---------------
# 5c4dcc0f — workspace-scoped (tenant_id) flash-of-insight capture. Distinct
# from workspace_notes (no lifecycle) and sprint_items (executor-claimable).
# status machine: raw → investigating → promoted | rejected.

_VALID_PROPOSAL_STATUSES = {"raw", "investigating", "promoted", "rejected"}
# 45c4c178 — "live" statuses: proposals still awaiting human triage. Terminal
# statuses (promoted/rejected) are excluded from the default (status-omitted)
# view of get_workspace_proposals so the common "what's still open" query
# doesn't require the caller to already know to pass status= explicitly.
_LIVE_PROPOSAL_STATUSES = {"raw", "investigating"}
_PROPOSAL_TRANSITIONS: dict[str, set[str]] = {
    "raw": {"investigating", "rejected"},
    "investigating": {"promoted", "rejected", "raw"},
    "promoted": set(),   # terminal — use promote_workspace_proposal instead
    "rejected": {"raw"},  # allow un-reject back to raw
}


async def add_workspace_proposal(
    db: aiosqlite.Connection,
    title: str,
    body: str,
    tags: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Insert a workspace_proposals row with status='raw'.

    Workspace-scoped by ``tenant_id`` (like ``add_workspace_note``). These are
    human-authored flashes of insight — NOT auto-claimable by executors.

    6fb48898 — a kebab-cased ``slug`` and a short memorable ``nickname`` are
    auto-generated from the title, unique per tenant scope, mirroring the
    sprint_items slug/nickname pattern (ae87699d).
    """
    pid = _new_id()
    # 6fb48898 — derive human-readable secondary keys from the title.
    _slug = await _unique_proposal_slug(
        db, tenant_id, _sprint_item_slug_base(title)
    )
    _nickname = await _unique_proposal_nickname(
        db, tenant_id, _sprint_item_nickname_base(title, pid)
    )
    await db.execute(
        "INSERT INTO workspace_proposals (id, title, body, tags, tenant_id, slug, nickname) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pid, title, body, tags, tenant_id, _slug, _nickname),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM workspace_proposals WHERE id = ?", (pid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {"id": pid}


async def get_workspace_proposals(
    db: aiosqlite.Connection,
    status: str | None = None,
    tag: str | None = None,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return workspace proposals, newest first.

    Optional filters: ``status`` (raw/investigating/promoted/rejected) and
    ``tag`` (substring match). Scoped to ``tenant_id`` when provided.

    When ``status`` is omitted, defaults to "live" proposals only (raw +
    investigating) — terminal proposals (promoted/rejected) are excluded so
    the default view reflects what's actually still open. Pass
    ``status="all"`` to fetch every status, or an explicit status value to
    filter to just that one (including "promoted"/"rejected")."""
    clauses: list[str] = []
    params: list[Any] = []
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    elif not status:
        placeholders = ", ".join("?" for _ in _LIVE_PROPOSAL_STATUSES)
        clauses.append(f"status IN ({placeholders})")
        params.extend(sorted(_LIVE_PROPOSAL_STATUSES))
    if tag:
        clauses.append("tags LIKE ?")
        params.append(f"%{tag}%")
    scope, scope_params = _ws_tenant_clause(tenant_id)
    if scope:
        clauses.append(scope)
        params.extend(scope_params)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with db.execute(
        f"SELECT * FROM workspace_proposals{where} ORDER BY created_at DESC",
        params or None,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def advance_workspace_proposal_status(
    db: aiosqlite.Connection,
    proposal_id: str,
    new_status: str,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Transition a proposal to ``new_status`` following the enforced state machine.

    Allowed transitions::

        raw         → investigating | rejected
        investigating → promoted | rejected | raw
        promoted    → (terminal — use promote_workspace_proposal)
        rejected    → raw

    Returns the updated row, or None if not found / wrong tenant.
    Raises ``ValueError`` on an invalid or disallowed transition."""
    if new_status not in _VALID_PROPOSAL_STATUSES:
        raise ValueError(
            f"Invalid proposal status '{new_status}'. "
            f"Valid: {sorted(_VALID_PROPOSAL_STATUSES)}"
        )
    scope, scope_params = _ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    async with db.execute(
        f"SELECT * FROM workspace_proposals WHERE id = ?{scope_sql}",
        [proposal_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    proposal = _row_to_dict(row) if row is not None else None
    if not proposal:
        return None
    current = proposal["status"]
    allowed = _PROPOSAL_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        raise ValueError(
            f"Cannot transition proposal from '{current}' to '{new_status}'. "
            f"Allowed from '{current}': {sorted(allowed) or '(none)'}"
        )
    await db.execute(
        f"UPDATE workspace_proposals SET status = ?, updated_at = datetime('now') WHERE id = ?{scope_sql}",
        [new_status, proposal_id, *scope_params],
    )
    await db.commit()
    async with db.execute(
        f"SELECT * FROM workspace_proposals WHERE id = ?{scope_sql}",
        [proposal_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def promote_workspace_proposal(
    db: aiosqlite.Connection,
    proposal_id: str,
    project_id: str,
    sprint_item_title: str | None = None,
    sprint_item_version: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Promote a proposal to a real sprint item and link the two.

    Creates a sprint item under ``project_id`` (using the proposal's title by
    default, overrideable via ``sprint_item_title``). Sets the proposal's
    status to 'promoted' and records ``promoted_to_sprint_item_id``.

    Raises ``ValueError`` if the proposal is not found, wrong tenant, or is
    not in 'raw' or 'investigating' state (cannot promote rejected/promoted)."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    async with db.execute(
        f"SELECT * FROM workspace_proposals WHERE id = ?{scope_sql}",
        [proposal_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    proposal = _row_to_dict(row) if row is not None else None
    if not proposal:
        raise ValueError(f"Proposal '{proposal_id}' not found")
    current = proposal["status"]
    if current not in ("raw", "investigating"):
        raise ValueError(
            f"Cannot promote a proposal in status '{current}'. "
            "Only 'raw' or 'investigating' proposals can be promoted."
        )
    # Verify the target project exists.
    project = await get_project(db, project_id)
    if project is None:
        raise ValueError(f"Project '{project_id}' not found")
    title = sprint_item_title or proposal["title"]
    version = sprint_item_version or "current"
    # a56f0951 — infer touches_resources from proposal content so the created
    # sprint item is not silently bare (same gap class as fba94f1a).
    proposal_body = proposal.get("body") or ""
    inferred = _infer_touches_resources_from_proposal(title, proposal_body)
    resources_json: str | None = None
    item_notes: str | None = None
    if inferred:
        try:
            resources_json = serialize_touches_resources(inferred)
        except Exception:  # noqa: BLE001 — never block promotion
            resources_json = None
    if not resources_json:
        # No file match from git history. Flag the item explicitly so it
        # doesn't ship with a silently-empty touches_resources — mirrors the
        # pattern used for under-specified items that need manual scoping.
        item_notes = (
            "[resource-scope:unset] touches_resources could not be inferred "
            "from this proposal's content. Update touches_resources manually "
            "before claiming (e.g. file:meridian/... or db:migrations)."
        )
    # Create the sprint item with inferred resources (or a note flagging the gap).
    si_id = _new_id()
    await db.execute(
        "INSERT INTO sprint_items "
        "(id, project_id, version, title, status, touches_resources, notes) "
        "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
        (si_id, project_id, version, title, resources_json, item_notes),
    )
    # Mark the proposal promoted.
    await db.execute(
        f"UPDATE workspace_proposals "
        f"SET status = 'promoted', promoted_to_sprint_item_id = ?, updated_at = datetime('now') "
        f"WHERE id = ?{scope_sql}",
        [si_id, proposal_id, *scope_params],
    )
    await db.commit()
    async with db.execute(
        f"SELECT * FROM workspace_proposals WHERE id = ?{scope_sql}",
        [proposal_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    promoted_proposal = _row_to_dict(row)

    # 3999d90f — conditional proposal-to-GitHub-issue workflow via HITL. When
    # the promoted proposal is code-related (the same inferred-resources
    # signal used for touches_resources above) AND the target project has a
    # GitHub repo connected, ask a human whether to also file a GitHub issue
    # for it. Fire-and-forget: request_hitl either auto-answers (safe/normal
    # mode) or sits pending for a human. Either way the answer is consumed by
    # _on_hitl_answered's 'proposal_github_issue' handler, which files the
    # issue via the GitHub tool and calls set_proposal_github_issue to store
    # the number/URL back on this proposal — never done inline here, since
    # this module has no GitHub API / tenant-PAT access.
    github_issue_hitl: dict[str, Any] | None = None
    is_code_related = bool(inferred)
    github_repo = (project.get("github_repo") or "").strip()
    if is_code_related and github_repo:
        # Lazy import: request_hitl lives in meridian.db (defined after this
        # module is imported by it) — see the identical pattern in
        # sprint_items.py's _maybe_file_chain_handoff.
        from meridian.db import request_hitl  # noqa: PLC0415

        yes_option = "Yes — file a GitHub issue"
        no_option = "No — skip"
        github_issue_hitl = await request_hitl(
            db, project_id,
            question=(
                f"Proposal '{title}' was promoted to sprint item {si_id} and looks "
                f"code-related (matched files in {github_repo}). Also file a GitHub issue for it?"
            ),
            context=(
                f"Proposal {proposal_id} -> sprint item {si_id}. "
                f"Repo: {github_repo}. If yes, an issue titled {title!r} is filed "
                f"and its number/URL are stored back on the proposal."
            ),
            urgency="normal",
            kind="proposal_github_issue",
            options=[yes_option, no_option],
            recommended=yes_option,
            payload=json.dumps({
                "proposal_id": proposal_id,
                "sprint_item_id": si_id,
                "project_id": project_id,
                "github_repo": github_repo,
                "issue_title": title,
                "issue_body": (
                    f"{proposal_body}\n\n---\nFiled from Meridian proposal "
                    f"{proposal_id} / sprint item {si_id}."
                ),
            }),
        )

    return {
        "proposal": promoted_proposal,
        "sprint_item_id": si_id,
        "sprint_item_title": title,
        "project_id": project_id,
        "sprint_item_touches_resources": resources_json,
        "sprint_item_notes": item_notes,
        "github_issue_hitl": github_issue_hitl,
    }


async def set_proposal_github_issue(
    db: aiosqlite.Connection,
    proposal_id: str,
    issue_number: int | None,
    issue_url: str | None,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """3999d90f — persist a filed GitHub issue's number/URL back onto a proposal.

    The write side of the conditional proposal-to-GitHub-issue HITL workflow:
    called by _on_hitl_answered (meridian/server.py) once a
    ``kind='proposal_github_issue'`` HITL is answered affirmatively and the
    issue has been created via the GitHub tool. Returns the updated proposal,
    or None if not found / wrong tenant scope."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    await db.execute(
        f"UPDATE workspace_proposals SET github_issue_number = ?, "
        f"github_issue_url = ?, updated_at = datetime('now') "
        f"WHERE id = ?{scope_sql}",
        [issue_number, issue_url, proposal_id, *scope_params],
    )
    await db.commit()
    async with db.execute(
        f"SELECT * FROM workspace_proposals WHERE id = ?{scope_sql}",
        [proposal_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def delete_workspace_proposal(
    db: aiosqlite.Connection, proposal_id: str, tenant_id: str | None = None
) -> bool:
    """Hard-delete a workspace proposal. Returns True if a row was removed."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    sql = "DELETE FROM workspace_proposals WHERE id = ?" + (f" AND {scope}" if scope else "")
    async with db.execute(sql, [proposal_id, *scope_params]) as cur:
        rc = cur.rowcount or 0
    await db.commit()
    return rc > 0


# --- Workspace sprint board (tenant-global personal backlog) ----------------
# A cross-project backlog that is NOT tied to any single project. Mirrors the
# useful subset of the per-project sprint_items shape but is keyed by tenant_id
# (see _ws_tenant_clause), exactly like workspace_notes / workspace_decisions.
# ``item_group`` is the cross-project bucket ('thesis'/'meridian'/'personal').

_VALID_WS_SPRINT_STATUSES = {
    "todo", "pending", "in_progress", "done", "skipped", "failed",
}


async def add_workspace_sprint_item(
    db: aiosqlite.Connection,
    title: str,
    item_group: str | None = None,
    human_id: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Append a ``todo`` item to the workspace-level personal backlog.

    ``item_group`` is the cross-project bucket the item lives under (e.g.
    'thesis' / 'meridian' / 'personal'); ``human_id`` attributes it to a
    person. Workspace sprint items belong to the whole workspace, not a single
    project, so there is no project_id. Scoped to ``tenant_id`` when provided
    (hosted); None on self-host."""
    iid = _new_id()
    # New items go to the end of their group (highest position + 1).
    scope, scope_params = _ws_tenant_clause(tenant_id)
    where = f" WHERE {scope}" if scope else ""
    async with db.execute(
        f"SELECT COALESCE(MAX(position), -1) + 1 AS next_pos "
        f"FROM workspace_sprint_items{where}",
        scope_params or None,
    ) as cur:
        prow = await cur.fetchone()
    next_pos = (prow["next_pos"] if isinstance(prow, dict) else prow[0]) or 0
    await db.execute(
        "INSERT INTO workspace_sprint_items "
        "(id, tenant_id, title, item_group, human_id, position) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (iid, tenant_id, title, item_group or None, human_id or None, next_pos),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM workspace_sprint_items WHERE id = ?", (iid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {"id": iid}


async def get_workspace_sprint_items(
    db: aiosqlite.Connection,
    status: str | None = None,
    item_group: str | None = None,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """List workspace sprint items, grouped by ``item_group`` then position.

    Optional ``status`` and ``item_group`` filters. Scoped to ``tenant_id``
    when provided (hosted); None returns everything on self-host."""
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        if status not in _VALID_WS_SPRINT_STATUSES:
            raise ValueError(
                f"invalid workspace sprint-item status filter: {status!r}. "
                f"Valid: {sorted(_VALID_WS_SPRINT_STATUSES)}"
            )
        clauses.append("status = ?")
        params.append(status)
    if item_group is not None:
        clauses.append("item_group = ?")
        params.append(item_group)
    scope, scope_params = _ws_tenant_clause(tenant_id)
    if scope:
        clauses.append(scope)
        params.extend(scope_params)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with db.execute(
        f"SELECT * FROM workspace_sprint_items{where} "
        "ORDER BY item_group IS NULL, item_group ASC, position ASC, created_at ASC",
        params or None,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


async def update_workspace_sprint_item(
    db: aiosqlite.Connection,
    item_id: str,
    title: str | None = None,
    status: str | None = None,
    item_group: str | None = None,
    human_id: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Patch editable fields of a workspace sprint item (tenant-scoped).

    Editable: title, status, item_group, human_id. Only fields passed as
    non-None are changed. Pass an empty string to clear item_group/human_id.
    A terminal status of 'done'/'skipped'/'failed' stamps ``completed_at``;
    any other status clears it. Returns the updated item, or None if no row
    matched (unknown id, or another tenant's item)."""
    scope, scope_params = _ws_tenant_clause(tenant_id)
    scope_sql = f" AND {scope}" if scope else ""
    # workspace_sprint_items.completed_at/updated_at are TIMESTAMPTZ on Postgres;
    # see update_agent_task_status for why the shared datetime('now') form breaks
    # there (adapter-rewritten to a text-typed to_char(...) expression).
    now_expr = "now()" if hasattr(db, "_pool") else "datetime('now')"
    fields: list[str] = []
    values: list[Any] = []
    if title is not None:
        fields.append("title = ?")
        values.append(title)
    if status is not None:
        if status not in _VALID_WS_SPRINT_STATUSES:
            raise ValueError(f"invalid workspace sprint-item status: {status!r}")
        fields.append("status = ?")
        values.append(status)
        if status in {"done", "skipped", "failed"}:
            fields.append(f"completed_at = {now_expr}")
        else:
            fields.append("completed_at = NULL")
    if item_group is not None:
        fields.append("item_group = ?")
        values.append(item_group or None)
    if human_id is not None:
        fields.append("human_id = ?")
        values.append(human_id or None)
    if not fields:
        async with db.execute(
            f"SELECT * FROM workspace_sprint_items WHERE id = ?{scope_sql}",
            [item_id, *scope_params],
        ) as cur:
            row = await cur.fetchone()
        return _row_to_dict(row)
    fields.append(f"updated_at = {now_expr}")
    cursor = await db.execute(
        f"UPDATE workspace_sprint_items SET {', '.join(fields)} "
        f"WHERE id = ?{scope_sql}",
        [*values, item_id, *scope_params],
    )
    await db.commit()
    if cursor.rowcount == 0:
        return None
    async with db.execute(
        f"SELECT * FROM workspace_sprint_items WHERE id = ?{scope_sql}",
        [item_id, *scope_params],
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def complete_workspace_sprint_item(
    db: aiosqlite.Connection,
    item_id: str,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Mark a workspace sprint item ``done`` (stamps ``completed_at``).

    Returns the updated item, or None if no row matched (unknown id / wrong
    tenant)."""
    return await update_workspace_sprint_item(
        db, item_id, status="done", tenant_id=tenant_id
    )


_WORKSPACE_SETTINGS_ID = "singleton"

# 4ef6ce5e — claim_verification_mode: does a PostToolUse hook re-check
# claim_sprint_item/complete_sprint_item calls against live DB state before
# trusting the calling session's own narration? See
# db.migrations._migrate_workspace_claim_verification_mode for the full
# incident/design writeup; meridian/claim_verify.py is the hook's actual
# comparison logic; handoff.seed_claim_verification_hook wires it into
# .claude/hooks/ via the existing custom_hooks (273287cb) infra.
_VALID_CLAIM_VERIFICATION_MODES: frozenset[str] = frozenset({"off", "advisory", "strict"})


def _ws_settings_key(tenant_id: str | None) -> str:
    """Row key for the workspace_settings singleton.

    Hosted callers key by their tenant_id so internal accounts that share the
    control-plane DB get isolated settings; self-host keeps the legacy
    'singleton' row.
    """
    return tenant_id or _WORKSPACE_SETTINGS_ID


async def get_workspace_settings(
    db: aiosqlite.Connection, tenant_id: str | None = None
) -> dict[str, Any]:
    """Return the workspace-level settings (tenant-global defaults).

    Always returns a dict — defaults when no row has been written yet — so
    callers never have to None-check. One row per tenant (or the legacy
    'singleton' row in self-host).
    """
    _cols = (
        "SELECT hitl_auto_answer_default, sprint_name_default, display_name, "
        "log_task_sprint_nudge_threshold, handoff_template, "
        "execution_mode_default, code_intel_enabled_default, "
        "loop_enabled_default, auto_refresh_enabled, refresh_interval_turns, "
        "refresh_triggers, handoff_inline_pointers, "
        "active_session_warning_minutes, manual_issue_screening_enabled, "
        "tool_priority_map, claim_verification_mode, updated_at "
        "FROM workspace_settings"
    )
    async with db.execute(
        f"{_cols} WHERE id = ?", (_ws_settings_key(tenant_id),)
    ) as cur:
        row = await cur.fetchone()
    if row is None and tenant_id is None:
        # Internal/self-host caller with no tenant context. On a dedicated
        # per-tenant DB the sole settings row is keyed by that tenant's id, so
        # fall back to it when there is exactly one row. The shared control-plane
        # DB has many rows, so this safely no-ops there (returns defaults).
        async with db.execute(f"{_cols} LIMIT 2") as cur:
            some = await cur.fetchall()
        if len(some) == 1:
            row = some[0]
    data = _row_to_dict(row) or {}
    # 0bf67524 — cascade defaults. None ⇒ "no workspace default set" (new
    # projects keep their own built-in default); a value ⇒ seed new projects.
    _emode = data.get("execution_mode_default")
    _ci_default = data.get("code_intel_enabled_default")
    # 76cf8bda — /loop auto-continue workspace default. Missing column/row ⇒
    # True (Meridian sessions default to auto-continue); a stored 0 turns it off.
    _loop_default = data.get("loop_enabled_default")
    # bf51b12e — planner context-refresh config. refresh_triggers is a JSON list
    # (NULL ⇒ None ⇒ hook uses its built-in default trigger set).
    _refresh_triggers_raw = data.get("refresh_triggers")
    _refresh_triggers: list[str] | None = None
    if _refresh_triggers_raw:
        try:
            _decoded = json.loads(_refresh_triggers_raw)
            if isinstance(_decoded, list):
                _refresh_triggers = [str(t) for t in _decoded]
        except Exception:  # noqa: BLE001 — malformed row ⇒ fall back to default
            _refresh_triggers = None
    _interval = data.get("refresh_interval_turns")
    # 36fea6ca — inline-resolved-pointers toggle. Missing column/row ⇒ True
    # (default on); a stored 0 keeps pointers DB-only in the handoff.
    _inline_ptrs = data.get("handoff_inline_pointers")
    # 6e0e5cea — configurable active-session warning window. NULL/missing ⇒
    # 10 minutes (matches the previous hardcoded constant). Minimum 1 minute.
    _asw_mins_raw = data.get("active_session_warning_minutes")
    _active_session_warning_minutes = max(1, int(_asw_mins_raw)) if _asw_mins_raw is not None else 10
    # 490e100d — workspace-level default MCP tool priority per semantic task
    # category (generalizes 4d1fb28f's per-item required_tool pin up one
    # level). NULL/missing/malformed ⇒ None ("no workspace default set");
    # a stored JSON object ⇒ the {category: tool} dict, non-string values
    # coerced to str so a stray non-string JSON value can't break callers.
    _tool_priority_map_raw = data.get("tool_priority_map")
    _tool_priority_map: dict[str, str] | None = None
    if _tool_priority_map_raw:
        try:
            _decoded_tpm = json.loads(_tool_priority_map_raw)
            if isinstance(_decoded_tpm, dict) and _decoded_tpm:
                _tool_priority_map = {
                    str(k): str(v) for k, v in _decoded_tpm.items() if k and v
                }
                if not _tool_priority_map:
                    _tool_priority_map = None
        except Exception:  # noqa: BLE001 — malformed row ⇒ no workspace default
            _tool_priority_map = None
    # 4ef6ce5e — claim_verification_mode. NOT NULL DEFAULT 'off' at the column
    # level, but a legacy row predating the column (or any unrecognized value
    # that slipped in some other way) falls back to 'off' too — fail toward
    # the no-enforcement, unchanged-behavior state rather than silently
    # enabling a blocking hook nobody asked for.
    _claim_verification_mode = data.get("claim_verification_mode")
    if _claim_verification_mode not in _VALID_CLAIM_VERIFICATION_MODES:
        _claim_verification_mode = "off"
    return {
        "hitl_auto_answer_default": bool(data.get("hitl_auto_answer_default")),
        "sprint_name_default": data.get("sprint_name_default"),
        "display_name": data.get("display_name"),
        "log_task_sprint_nudge_threshold": int(data["log_task_sprint_nudge_threshold"])
        if data.get("log_task_sprint_nudge_threshold") is not None
        else 5,
        "handoff_template": data.get("handoff_template"),
        "execution_mode_default": _emode if _emode in ("autonomous", "interactive") else None,
        "code_intel_enabled_default": (None if _ci_default is None else bool(_ci_default)),
        "loop_enabled_default": (True if _loop_default is None else bool(_loop_default)),
        "auto_refresh_enabled": bool(data.get("auto_refresh_enabled")),
        "refresh_interval_turns": (int(_interval) if _interval is not None else 10) or 10,
        "refresh_triggers": _refresh_triggers,
        "handoff_inline_pointers": (True if _inline_ptrs is None else bool(_inline_ptrs)),
        "active_session_warning_minutes": _active_session_warning_minutes,
        # 5dfe34b2 / cd495afa — OFF-by-default opt-in toggle. Deliberately NOT a
        # parameter of update_workspace_settings below: the ONLY writer is
        # set_manual_issue_screening_enabled, gated on a completed
        # require_human=True HITL (see that function's docstring). Surfaced here
        # alongside a human-readable risk label per the design spec's "label the
        # toggle's risk clearly" requirement.
        "manual_issue_screening_enabled": bool(data.get("manual_issue_screening_enabled")),
        "manual_issue_screening_risk_warning": _MANUAL_ISSUE_SCREENING_RISK_WARNING,
        "tool_priority_map": _tool_priority_map,
        "claim_verification_mode": _claim_verification_mode,
        "updated_at": data.get("updated_at"),
    }


async def update_workspace_settings(
    db: aiosqlite.Connection,
    *,
    hitl_auto_answer_default: bool | None = None,
    sprint_name_default: str | None = None,
    display_name: str | None = None,
    log_task_sprint_nudge_threshold: int | None = None,
    handoff_template: str | None = None,
    execution_mode_default: str | None = None,
    code_intel_enabled_default: "bool | int | str | None" = None,
    loop_enabled_default: "bool | int | str | None" = None,
    auto_refresh_enabled: "bool | int | str | None" = None,
    refresh_interval_turns: int | None = None,
    refresh_triggers: "list[str] | str | None" = None,
    handoff_inline_pointers: "bool | int | str | None" = None,
    active_session_warning_minutes: int | None = None,
    tool_priority_map: "dict[str, str] | str | None" = None,
    claim_verification_mode: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Upsert the per-tenant workspace settings row and return the new values.

    Only the fields passed (non-None) are changed. ``sprint_name_default=""``
    or ``display_name=""`` explicitly clears that label. ``execution_mode_default=""``
    clears the execution-mode default (new projects revert to their own default);
    ``code_intel_enabled_default`` accepts a bool/0/1 or the string ``""`` to clear.

    ``claim_verification_mode`` (4ef6ce5e) — ``"off"`` / ``"advisory"`` /
    ``"strict"``, case-insensitive; ``""`` clears back to the default
    (``"off"``). Unlike ``manual_issue_screening_enabled``, this is a PLAIN
    parameter of this generic settings-patch function, not gated behind a
    separate HITL-approved writer: moving TOWARD ``"strict"`` REDUCES risk
    (it catches false-success narration rather than introducing a new
    untrusted-content surface), so it doesn't need the self-escalation-proof
    HITL gate that toggle has. Raises ``ValueError`` for any other non-empty
    string. See ``get_workspace_settings`` for what each mode does.

    ``tool_priority_map`` (490e100d) — workspace-level default MCP tool
    priority per semantic task category, generalizing 4d1fb28f's per-item
    ``required_tool`` pin up one level (e.g. ``{"code-reading": "Serena:
    find_symbol"}``). A ``dict`` is JSON-encoded; an empty dict or the string
    ``""`` clears the workspace default entirely (reverts to ordinary
    executor discretion / per-item pins only); a non-empty string is stored
    verbatim (assumed already JSON-encoded, mirrors ``refresh_triggers``).
    Rendered as a HARD, unconditional directive by
    ``handoff._build_quick_start_goal`` — not a soft hint — for every pending
    item whose title/notes match a configured category and that has no
    item-level ``required_tool`` override (the item-level pin always wins).
    """
    settings_key = _ws_settings_key(tenant_id)
    # Ensure the row exists before updating individual fields.
    await db.execute(
        "INSERT INTO workspace_settings (id, tenant_id) VALUES (?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (settings_key, tenant_id),
    )
    updates: list[str] = []
    params: list[Any] = []
    if hitl_auto_answer_default is not None:
        updates.append("hitl_auto_answer_default = ?")
        params.append(1 if hitl_auto_answer_default else 0)
    if sprint_name_default is not None:
        updates.append("sprint_name_default = ?")
        params.append(sprint_name_default or None)
    if display_name is not None:
        updates.append("display_name = ?")
        params.append(display_name.strip() or None)
    if log_task_sprint_nudge_threshold is not None:
        updates.append("log_task_sprint_nudge_threshold = ?")
        params.append(max(0, int(log_task_sprint_nudge_threshold)))
    if handoff_template is not None:
        updates.append("handoff_template = ?")
        # Empty string clears the custom template (reverts to server default).
        params.append(handoff_template.strip() or None)
    if execution_mode_default is not None:
        updates.append("execution_mode_default = ?")
        # Empty string clears the default; otherwise normalize to a valid posture.
        _emode = (execution_mode_default or "").strip().lower()
        params.append(normalize_execution_mode(_emode) if _emode else None)
    if code_intel_enabled_default is not None:
        updates.append("code_intel_enabled_default = ?")
        # "" clears; any truthy/1 → 1, falsey/0 → 0.
        if isinstance(code_intel_enabled_default, str) and not code_intel_enabled_default.strip():
            params.append(None)
        else:
            params.append(1 if code_intel_enabled_default and code_intel_enabled_default not in ("0", "false", "False") else 0)
    if loop_enabled_default is not None:
        # 76cf8bda — /loop auto-continue default. Truthy/1 → 1, falsey/0 → 0.
        updates.append("loop_enabled_default = ?")
        params.append(1 if loop_enabled_default and loop_enabled_default not in ("0", "false", "False") else 0)
    if auto_refresh_enabled is not None:
        # bf51b12e — planner context-refresh toggle. Truthy/1 → 1, falsey/0 → 0.
        updates.append("auto_refresh_enabled = ?")
        params.append(1 if auto_refresh_enabled and auto_refresh_enabled not in ("0", "false", "False") else 0)
    if refresh_interval_turns is not None:
        # At least 1 turn between interval-based refreshes.
        updates.append("refresh_interval_turns = ?")
        params.append(max(1, int(refresh_interval_turns)))
    if refresh_triggers is not None:
        # A list ⇒ JSON-encode; "" clears (revert to default trigger set);
        # any other string is stored verbatim (already JSON).
        updates.append("refresh_triggers = ?")
        if isinstance(refresh_triggers, list):
            params.append(json.dumps(refresh_triggers))
        elif isinstance(refresh_triggers, str):
            params.append(refresh_triggers.strip() or None)
        else:
            params.append(None)
    if handoff_inline_pointers is not None:
        # 36fea6ca — inline resolved pointers in the handoff. Truthy/1 → 1,
        # falsey/0 (incl. the strings "0"/"false") → 0.
        updates.append("handoff_inline_pointers = ?")
        params.append(
            1 if handoff_inline_pointers and handoff_inline_pointers not in ("0", "false", "False") else 0
        )
    if active_session_warning_minutes is not None:
        # 6e0e5cea — configurable active-session warning window (minutes).
        # Minimum 1 minute; 0 or negative values are clamped to 1.
        updates.append("active_session_warning_minutes = ?")
        params.append(max(1, int(active_session_warning_minutes)))
    if tool_priority_map is not None:
        # 490e100d — workspace-level default tool priority per task category.
        # A dict ⇒ JSON-encode (empty dict clears); "" clears; any other
        # string is stored verbatim (already JSON, mirrors refresh_triggers).
        updates.append("tool_priority_map = ?")
        if isinstance(tool_priority_map, dict):
            params.append(json.dumps(tool_priority_map) if tool_priority_map else None)
        elif isinstance(tool_priority_map, str):
            params.append(tool_priority_map.strip() or None)
        else:
            params.append(None)
    if claim_verification_mode is not None:
        # 4ef6ce5e — "" clears back to the 'off' default; any other value
        # must be one of the three valid modes (case-insensitive).
        _cvm = (claim_verification_mode or "").strip().lower()
        if _cvm and _cvm not in _VALID_CLAIM_VERIFICATION_MODES:
            raise ValueError(
                f"claim_verification_mode must be one of "
                f"{sorted(_VALID_CLAIM_VERIFICATION_MODES)} or '' to clear, got "
                f"{claim_verification_mode!r}"
            )
        updates.append("claim_verification_mode = ?")
        params.append(_cvm or "off")
    if updates:
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        updates.append("updated_at = ?")
        params.append(now_ts)
        params.append(settings_key)
        await db.execute(
            f"UPDATE workspace_settings SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
    await db.commit()
    return await get_workspace_settings(db, tenant_id=tenant_id)


async def seed_workspace_settings_from_toml(db: aiosqlite.Connection) -> None:
    """1d69d5d9 — seed the self-host singleton workspace_settings row from
    meridian.toml (env > toml > default, via toml_config) on first boot. This is
    what makes the 46c83e55 toml readers actually take effect. No-op when the
    singleton row already exists (the DB is authoritative once configured) or
    when no self-host config (a meridian.toml file or MERIDIAN_* env var) is
    present. Fully guarded — a seed failure must never block server startup."""
    try:
        from .. import toml_config  # local: avoid import cycle at module load
        _has_cfg = toml_config.load_toml() is not None or any(
            os.environ.get(k) for k in (
                "MERIDIAN_AUTO_REFRESH", "MERIDIAN_REFRESH_INTERVAL_TURNS",
                "MERIDIAN_REFRESH_TRIGGERS", "MERIDIAN_LOOP_ENABLED",
                "MERIDIAN_MAX_TURNS", "MERIDIAN_FILESYSTEM_ROOTS",
            )
        )
        if not _has_cfg:
            return
        async with db.execute(
            "SELECT id FROM workspace_settings WHERE id = ?", ("singleton",)
        ) as cur:
            if await cur.fetchone() is not None:
                return  # already configured — the DB row wins over toml
        refresh = toml_config.get_context_refresh_config()
        defaults = toml_config.get_self_host_defaults()
        await update_workspace_settings(
            db,
            auto_refresh_enabled=bool(refresh.get("auto_refresh_enabled")),
            refresh_interval_turns=int(refresh.get("refresh_interval_turns") or 10),
            refresh_triggers=refresh.get("refresh_triggers"),
            loop_enabled_default=bool(defaults.get("loop_enabled_default", True)),
            tenant_id=None,
        )
    except Exception:  # noqa: BLE001 — seeding must never block startup
        pass


# ---------------------------------------------------------------------------
# 5dfe34b2 / cd495afa — manual-issue-screening toggle + action audit trail
#
# The toggle governs WHO can trigger a read of a manually-filed GitHub issue
# (an internal person choosing to enable it), not whether the CONTENT read is
# trustworthy — anyone on the internet can file a GitHub issue regardless of
# who flipped the toggle. Hardcoded protections must hold "just in case of
# internal hacking": (a) a compromised/malicious internal session or token
# must not be able to silently self-enable this mode, and (b) enabling it must
# not, on its own, make unscreened content actionable (see
# meridian.db.manual_issue_intel for the content-screening side of that).
# ---------------------------------------------------------------------------

_MANUAL_ISSUE_SCREENING_RISK_WARNING = (
    "RISK: enabling this lets Meridian's automated GitHub-issue comment/propose "
    "flow (never auto-close) act on issues filed directly on GitHub by ANYONE, "
    "not just issues Meridian itself created. Content is heuristically screened "
    "for prompt-injection shapes before use, but no screening technique is a "
    "complete mitigation (OWASP LLM01) — treat flagged/borderline content with "
    "human judgment, not blind trust."
)


async def record_action_audit_event(
    db: aiosqlite.Connection,
    event_type: str,
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
    actor: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """cd495afa / d86d70a5 — append a row to the action_audit_log.

    Append-only: this module never exposes an update/delete for this table.
    Used for toggle enable/disable events AND velocity/anomaly escalations
    (WHAT MERIDIAN DID — distinct from manual_issue_intel's raw-content log,
    which records WHAT MERIDIAN SAW). Never raises on a logging failure from
    the caller's perspective is NOT guaranteed here — callers that must not be
    blocked by audit-log trouble should wrap this in their own try/except (as
    e.g. the velocity-anomaly escalation path does), since a silently-dropped
    audit entry would defeat the point of an audit trail.
    """
    aid = _new_id()
    await db.execute(
        "INSERT INTO action_audit_log (id, tenant_id, project_id, event_type, actor, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (aid, tenant_id, project_id, event_type, actor, detail),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM action_audit_log WHERE id = ?", (aid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {"id": aid}


async def get_action_audit_log(
    db: aiosqlite.Connection,
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
    event_type: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read the action_audit_log, newest first. All filters optional/AND'd.

    ``since`` is an inclusive lower bound on ``created_at`` (string-comparable
    ``YYYY-MM-DD HH:MM:SS`` form, matching this codebase's other TEXT
    timestamps) — used by the velocity/anomaly check to scope a time window.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if tenant_id is not None:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    if since is not None:
        clauses.append("created_at >= ?")
        params.append(since)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, int(limit)))
    async with db.execute(
        f"SELECT * FROM action_audit_log{where} ORDER BY created_at DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


class ManualIssueScreeningToggleError(ValueError):
    """Raised when set_manual_issue_screening_enabled(True, ...) is called
    without a genuine, completed, require_human=True HITL approval backing
    it. Distinct exception type so callers can distinguish "you tried to
    self-escalate" from an ordinary validation error."""


async def set_manual_issue_screening_enabled(
    db: aiosqlite.Connection,
    enabled: bool,
    *,
    tenant_id: str | None = None,
    actor: str | None = None,
    hitl_id: str | None = None,
) -> dict[str, Any]:
    """cd495afa — the ONE and ONLY writer of
    workspace_settings.manual_issue_screening_enabled anywhere in this
    codebase. ``update_workspace_settings`` (the generic, MCP-exposed
    settings-patch function) deliberately does NOT accept this field as a
    parameter — there is no update_workspace_settings-style direct write path
    to this column at all.

    Enabling (``enabled=True``) requires ``hitl_id`` naming a HITL request
    that, at the moment this function is called, is ALL of:
      1. found (exists in hitl_requests),
      2. ``kind == 'manual_issue_screening_toggle'``,
      3. ``status == 'answered'``,
      4. its stored payload has ``require_human: true`` (the e43e6941
         guarantee — this was persisted at request_hitl() time and can never
         be forged after the fact by an answer),
      5. its ``answer`` affirms enabling (case-insensitively starts with
         'yes').
    Any failure raises :class:`ManualIssueScreeningToggleError` and writes
    NOTHING — there is no partial-enable state. Because ``require_human=True``
    HITLs are structurally exempt from Meridian's auto-answer machinery (see
    ``request_hitl`` / ``_hitl_should_auto_answer``), condition 3+4 together
    mean a genuine human reply is the only way condition 3 can ever become
    true for this ``kind`` — an autonomous/executor session or a compromised
    API token cannot manufacture an 'answered' row for a require_human HITL by
    itself.

    Disabling (``enabled=False``) has no HITL gate — turning the riskier mode
    OFF is the fail-safe direction — but every flip, either direction, is
    recorded to action_audit_log (cd495afa point 2) before returning.

    Returns the updated get_workspace_settings() dict.
    """
    settings_key = _ws_settings_key(tenant_id)
    if enabled:
        if not hitl_id:
            raise ManualIssueScreeningToggleError(
                "enabling manual_issue_screening requires hitl_id referencing a "
                "completed require_human=True HITL of "
                "kind='manual_issue_screening_toggle' — there is no direct-write path"
            )
        async with db.execute(
            "SELECT * FROM hitl_requests WHERE id = ?", (hitl_id,)
        ) as cur:
            row = await cur.fetchone()
        hitl = _row_to_dict(row)
        if hitl is None:
            raise ManualIssueScreeningToggleError(f"hitl request '{hitl_id}' not found")
        if hitl.get("kind") != "manual_issue_screening_toggle":
            raise ManualIssueScreeningToggleError(
                f"hitl request '{hitl_id}' is kind={hitl.get('kind')!r}, not "
                "'manual_issue_screening_toggle'"
            )
        if hitl.get("status") != "answered":
            raise ManualIssueScreeningToggleError(
                f"hitl request '{hitl_id}' is not answered (status={hitl.get('status')!r})"
            )
        try:
            _payload = json.loads(hitl.get("payload") or "{}")
        except (TypeError, ValueError):
            _payload = {}
        if not (isinstance(_payload, dict) and _payload.get("require_human") is True):
            raise ManualIssueScreeningToggleError(
                f"hitl request '{hitl_id}' was not filed with require_human=True — "
                "refusing to trust it as a self-escalation-proof approval"
            )
        _answer = (hitl.get("answer") or "").strip().lower()
        if not _answer.startswith("yes"):
            raise ManualIssueScreeningToggleError(
                f"hitl request '{hitl_id}' answer ({hitl.get('answer')!r}) does not "
                "affirm enabling"
            )
    # Ensure the settings row exists (same upsert pattern as update_workspace_settings).
    await db.execute(
        "INSERT INTO workspace_settings (id, tenant_id) VALUES (?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (settings_key, tenant_id),
    )
    await db.execute(
        "UPDATE workspace_settings SET manual_issue_screening_enabled = ? WHERE id = ?",
        (1 if enabled else 0, settings_key),
    )
    await db.commit()
    await record_action_audit_event(
        db,
        "manual_issue_screening_enabled" if enabled else "manual_issue_screening_disabled",
        tenant_id=tenant_id,
        actor=actor or (f"hitl:{hitl_id}" if hitl_id else None),
        detail=json.dumps({"hitl_id": hitl_id}) if hitl_id else None,
    )
    return await get_workspace_settings(db, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Workspace members — team invite flow
# ---------------------------------------------------------------------------


async def create_workspace_invite(
    db: aiosqlite.Connection,
    tenant_id: str,
    email: str,
    role: str,
    token_hash: str,
    *,
    github_access: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Insert a pending workspace member row (joined_at=NULL).

    G5.20 — ``github_access`` caps repo-touching MCP tools for this invitee.
    Defaults from role when omitted (viewer→none, member→read, admin/owner→write).

    d116642e — ``project_id`` scopes the invite to a single project. When
    ``None`` (default) the member is workspace-wide and sees every project
    (current behavior). When set, the member is project-scoped: listing-only
    scoping applies (they see only that project in listings). Airtight
    per-request access enforcement is deferred pending the product decision
    (pin b11c7cf6).
    """
    from .. import roles as _roles  # noqa: PLC0415
    mid = _new_id()
    if github_access is None:
        github_access = _roles.default_github_access_for_role(role)
    await db.execute(
        "INSERT INTO workspace_members "
        "(id, tenant_id, email, role, github_access, token_hash, project_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (mid, tenant_id, email, role, github_access, token_hash, project_id),
    )
    await db.commit()
    async with db.execute("SELECT * FROM workspace_members WHERE id = ?", (mid,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_workspace_invite_by_token_hash(
    db: aiosqlite.Connection,
    token_hash: str,
) -> dict[str, Any] | None:
    """Return a pending invite by token hash, or None if not found / already accepted."""
    async with db.execute(
        "SELECT * FROM workspace_members WHERE token_hash = ? AND joined_at IS NULL",
        (token_hash,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def accept_workspace_invite(
    db: aiosqlite.Connection,
    member_id: str,
) -> dict[str, Any] | None:
    """Mark an invite as accepted (set joined_at, clear token_hash)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE workspace_members SET joined_at = ?, token_hash = NULL WHERE id = ?",
        (now, member_id),
    )
    await db.commit()
    async with db.execute("SELECT * FROM workspace_members WHERE id = ?", (member_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_pending_invites_for_email(
    db: aiosqlite.Connection,
    email: str,
) -> list[dict[str, Any]]:
    """Return all pending workspace_members rows for email (joined_at IS NULL).

    Used at OAuth login to auto-accept invites sent before the user had an
    account (fbbe99af fallback).
    """
    async with db.execute(
        "SELECT * FROM workspace_members WHERE LOWER(email) = LOWER(?) AND joined_at IS NULL",
        (email,),
    ) as cur:
        rows = await cur.fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r]


async def resolve_member_role(
    db: aiosqlite.Connection,
    tenant_id: str,
    email: str,
) -> tuple[str, str] | None:
    """G5.19 / G5.20 — return ``(role, github_access)`` for the given user
    in the given tenant's workspace.

    Order:
     1. If ``email`` matches the tenant row's own email → ('owner','write').
     2. Else if an accepted workspace_members row exists for this
        (tenant_id, email) → use the row's role + github_access.
     3. Else → None (caller should treat as 403 / not a member).
    """
    e = (email or "").strip().lower()
    if not e:
        return None
    async with db.execute(
        "SELECT email FROM tenants WHERE id = ?", (tenant_id,),
    ) as cur:
        tenant_row = await cur.fetchone()
    if tenant_row and (str(tenant_row["email"]).lower() == e):
        return ("owner", "write")
    async with db.execute(
        "SELECT role, github_access FROM workspace_members "
        "WHERE tenant_id = ? AND LOWER(email) = ? AND joined_at IS NOT NULL "
        "ORDER BY joined_at DESC LIMIT 1",
        (tenant_id, e),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return (
        (row["role"] or "member"),
        (row["github_access"] or "read"),
    )


async def workspace_member_accepted_for_email(
    db: aiosqlite.Connection,
    email: str,
) -> dict[str, Any] | None:
    """G5.22 — return an accepted workspace membership for ``email`` (joined,
    not pending), or None. Used by the OAuth callback to skip auto-Neon
    provisioning for invitees who already belong to someone else's workspace."""
    e = (email or "").strip().lower()
    if not e:
        return None
    async with db.execute(
        "SELECT * FROM workspace_members "
        "WHERE LOWER(email) = ? AND joined_at IS NOT NULL "
        "ORDER BY joined_at DESC LIMIT 1",
        (e,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_workspace_member_by_id(
    db: aiosqlite.Connection,
    member_id: str,
    tenant_id: str,
) -> dict[str, Any] | None:
    """Return a single workspace_members row scoped to tenant_id."""
    async with db.execute(
        "SELECT * FROM workspace_members WHERE id = ? AND tenant_id = ?",
        (member_id, tenant_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def refresh_workspace_invite_token(
    db: aiosqlite.Connection,
    member_id: str,
    tenant_id: str,
    token_hash: str,
) -> dict[str, Any] | None:
    """Replace the invite token for a pending member row."""
    await db.execute(
        "UPDATE workspace_members SET token_hash = ? WHERE id = ? AND tenant_id = ? AND joined_at IS NULL",
        (token_hash, member_id, tenant_id),
    )
    await db.commit()
    async with db.execute("SELECT * FROM workspace_members WHERE id = ?", (member_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def list_workspace_members(
    db: aiosqlite.Connection,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """Return all workspace members (pending and accepted) for a tenant."""
    async with db.execute(
        "SELECT * FROM workspace_members WHERE tenant_id = ? ORDER BY invited_at ASC",
        (tenant_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]


async def count_workspace_members(
    db: aiosqlite.Connection,
    tenant_id: str,
) -> int:
    """Return the total member count (pending + accepted) for a tenant."""
    async with db.execute(
        "SELECT COUNT(*) FROM workspace_members WHERE tenant_id = ?",
        (tenant_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0
    vals = list(row.values()) if hasattr(row, "values") else list(row)
    return int(vals[0]) if vals else 0


async def delete_workspace_member(
    db: aiosqlite.Connection,
    member_id: str,
    tenant_id: str,
) -> bool:
    """Remove a workspace member row. Returns True (no-op on missing row)."""
    await db.execute(
        "DELETE FROM workspace_members WHERE id = ? AND tenant_id = ?",
        (member_id, tenant_id),
    )
    await db.commit()
    return True


async def update_workspace_member(
    db: aiosqlite.Connection,
    member_id: str,
    tenant_id: str,
    *,
    role: str | None = None,
    github_access: str | None = None,
) -> dict[str, Any] | None:
    """v2.8 — update a member's role and/or github_access (admin-only edit).

    Only the fields passed (non-None) are changed. Scoped by tenant_id so a
    caller can never touch another workspace's rows. Returns the updated row,
    or None when no member matched.
    """
    updates: list[str] = []
    params: list[Any] = []
    if role is not None:
        updates.append("role = ?")
        params.append(role)
    if github_access is not None:
        updates.append("github_access = ?")
        params.append(github_access)
    if updates:
        params.extend([member_id, tenant_id])
        await db.execute(
            f"UPDATE workspace_members SET {', '.join(updates)} "
            "WHERE id = ? AND tenant_id = ?",
            tuple(params),
        )
        await db.commit()
    async with db.execute(
        "SELECT * FROM workspace_members WHERE id = ? AND tenant_id = ?",
        (member_id, tenant_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_workspaces_for_email(
    db: aiosqlite.Connection,
    email: str,
) -> list[dict[str, Any]]:
    """Return workspaces the email has been invited to (accepted rows only).

    Each row: {tenant_id, owner_email, role, github_access, project_id}.
    ``project_id`` (d116642e) is NULL for workspace-wide members and set for
    project-scoped members. Used to populate the workspace-switcher dropdown.
    """
    e = (email or "").strip().lower()
    if not e:
        return []
    async with db.execute(
        "SELECT wm.tenant_id, wm.role, wm.github_access, wm.project_id, "
        "t.email AS owner_email "
        "FROM workspace_members wm "
        "JOIN tenants t ON t.id = wm.tenant_id "
        "WHERE LOWER(wm.email) = ? AND wm.joined_at IS NOT NULL "
        "ORDER BY wm.invited_at ASC",
        (e,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]


async def get_scoped_project_ids_for_member(
    db: aiosqlite.Connection,
    tenant_id: str,
    email: str,
) -> list[str] | None:
    """d116642e — listing-only project scoping for a workspace member.

    Returns the set of project_ids an accepted member is scoped to within the
    given tenant's workspace, or ``None`` when the member is NOT project-scoped
    (i.e. has any workspace-wide row, or is not a member at all) — meaning they
    see every project.

    Semantics:
      - returns ``None``  → no scoping; caller lists all projects (default).
      - returns ``[...]`` → list only these project_ids in UI listings.

    NOTE: this is listing-only scoping. It does NOT block direct-by-ID access to
    other projects in the same workspace — airtight per-request enforcement is
    deferred pending the open product decision (pin b11c7cf6). Writes remain
    gated by role enforcement (393eed0a).
    """
    e = (email or "").strip().lower()
    if not e:
        return None
    async with db.execute(
        "SELECT project_id FROM workspace_members "
        "WHERE tenant_id = ? AND LOWER(email) = ? AND joined_at IS NOT NULL",
        (tenant_id, e),
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return None
    scoped: list[str] = []
    for r in rows:
        pid = r["project_id"] if isinstance(r, dict) else r[0]
        if pid is None:
            # Any workspace-wide membership row wins → no scoping.
            return None
        scoped.append(pid)
    return scoped
