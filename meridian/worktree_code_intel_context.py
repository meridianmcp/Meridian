"""32ba4125 — validated, worktree-aware code-intelligence context.

Investigation finding (32ba4125): Serena already routes each request per-repo
via the ``X-Meridian-Repo-Path`` header (:mod:`meridian.serena_pool`,
``resolve_repo_path`` + ``SerenaDaemonPool.get_or_spawn``), and
codebase-memory accepts a list of ``code_dirs`` to auto-index
(``executor_config.codebase_code_dirs``). Neither backend was ever bound to
the registered ``active_worktrees`` record: the ``set_active_repo`` MCP tool
(and the ``send_active_repo_control`` helper behind it,
:mod:`meridian.routes.tunnel`) accepted ANY path string with zero check
against what a session had actually registered via
``POST /projects/{id}/worktrees`` (``create_worktree``,
:mod:`meridian.routes.projects`).

This module is the validated context boundary:

- :func:`find_registered_worktree` / :func:`resolve_worktree_code_intel_context`
  resolve a path against the DB's ``active_worktrees`` table — fail-closed,
  they never treat an unregistered path as legitimate.
- :func:`activate_worktree_code_intel_context` performs the atomic
  activation keyed by ``worktree_id`` (not a caller-supplied path): it looks
  up the worktree row, pushes the resulting repo_path to the Serena/fs
  backends via the EXISTING ``send_active_repo_control`` /
  ``send_add_fs_roots_control`` control-plane helpers, and only reports
  success when the push itself actually reached the tunnel — so the
  server-side "what's active" view and the tunnel-side daemon pool never
  silently diverge. A worktree that does not exist, has been removed, or
  whose backend push fails, raises :class:`WorktreeCodeIntelContextError`
  instead of leaving a half-applied context.
- :func:`clear_stale_active_repo_cache` is the cleanup half: called when a
  worktree is removed (``delete_worktree``) so a stale cached "active repo"
  pointer can never keep referencing a worktree that no longer exists.

Existing main-repo (non-worktree) behavior is completely untouched — this
module only ever adds a NEW, opt-in, ``worktree_id``-keyed activation path;
the original ``repo_path``-keyed ``set_active_repo`` call remains exactly as
it was for callers that don't pass ``worktree_id``. Daemon/index REUSE is
also untouched: :class:`~meridian.serena_pool.SerenaDaemonPool` already keys
daemons by normalized repo_path, so activating the same worktree twice reuses
the same daemon with no extra work needed here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class WorktreeCodeIntelContextError(ValueError):
    """Raised when a code-intel activation targets an unregistered/removed
    worktree, or when the atomic push to the Serena/fs backend fails.

    Fail-closed by design: any caller catching this should treat the
    activation as having had NO effect (see the module docstring).
    """


def normalize_context_path(path: str) -> str:
    """Canonicalize a path the same way ``SerenaDaemonPool._normalize`` does,
    so worktree-path comparisons are stable across trailing slashes,
    relative segments, and Windows path casing.

    Falls back to a plain stripped string for anything that isn't a valid
    filesystem path on this machine (mirrors the pool's own fallback) rather
    than raising — path *comparison*, not path *validation*, is this
    function's job.
    """
    try:
        return str(Path(path).expanduser().resolve())
    except Exception:  # noqa: BLE001 — non-filesystem-ish key, use as-is
        return str(path or "").strip()


def build_context_fingerprint(worktree_row: dict[str, Any]) -> dict[str, Any]:
    """Addressable metadata describing a resolved worktree context.

    Returned alongside every successful activation/resolution so a caller
    (MCP tool response, dashboard) can see exactly which worktree is active
    without a second round-trip.
    """
    return {
        "worktree_id": worktree_row.get("id"),
        "project_id": worktree_row.get("project_id"),
        "session_id": worktree_row.get("session_id"),
        "item_id": worktree_row.get("item_id"),
        "branch": worktree_row.get("branch"),
        "path": worktree_row.get("path"),
        "registered_at": worktree_row.get("created_at"),
    }


async def find_registered_worktree(
    db: Any, project_id: str, repo_path: str
) -> "dict[str, Any] | None":
    """Return the active (non-removed) ``active_worktrees`` row for
    ``project_id`` whose path normalizes to ``repo_path``, or ``None``.

    This is the fail-closed lookup underneath every other function in this
    module: anything that doesn't come back with a row here is, by
    definition, not a registered worktree context.
    """
    from meridian import db as db_module  # noqa: PLC0415 — avoid import cycle

    target = normalize_context_path(repo_path)
    if not target:
        return None
    rows = await db_module.list_active_worktrees(db, project_id)
    for row in rows:
        if normalize_context_path(row.get("path") or "") == target:
            return row
    return None


async def resolve_worktree_code_intel_context(
    db: Any,
    project_id: str,
    repo_path: str,
    *,
    approved_roots: "list[str] | None" = None,
) -> dict[str, Any]:
    """Resolve+validate ``repo_path`` into a code-intel activation context.

    Fail-closed: raises :class:`WorktreeCodeIntelContextError` unless
    ``repo_path`` is either:

    - a registered, non-removed ``active_worktrees`` row for ``project_id``
      (``kind="worktree"``, ``context`` is the fingerprint dict), or
    - one of ``approved_roots`` — the project's EXISTING main-repo /
      executor_config roots, passed in by the caller so today's non-worktree
      behavior is completely preserved (``kind="approved_root"``,
      ``context`` is ``None``).

    Anything else — an arbitrary, never-registered path — raises rather than
    silently treating it as valid.
    """
    target = normalize_context_path(repo_path)
    worktree = await find_registered_worktree(db, project_id, repo_path)
    if worktree is not None:
        return {
            "kind": "worktree",
            "repo_path": target,
            "context": build_context_fingerprint(worktree),
        }
    for root in approved_roots or []:
        norm_root = normalize_context_path(root)
        if not norm_root:
            continue
        if target == norm_root or target.startswith(norm_root.rstrip("/\\") + "/") \
                or target.startswith(norm_root.rstrip("/\\") + "\\"):
            return {"kind": "approved_root", "repo_path": target, "context": None}
    raise WorktreeCodeIntelContextError(
        f"repo_path {repo_path!r} is not a registered worktree for project "
        f"{project_id!r} and is not one of the project's approved roots — "
        "register it first via POST /projects/{project_id}/worktrees "
        "(create_worktree) or add it to the project's executor_config roots."
    )


async def activate_worktree_code_intel_context(
    db: Any,
    tenant_id: str,
    worktree_id: str,
) -> dict[str, Any]:
    """Atomically activate a REGISTERED worktree as the code-intel context.

    Fail-closed end to end:

    1. The worktree must exist and not be removed (``db.get_worktree``) —
       otherwise raises :class:`WorktreeCodeIntelContextError` before
       touching any backend at all.
    2. The push to the Serena backend (``send_active_repo_control``) must
       itself report ``status == "ok"`` — a ``not_connected``/``error``
       result raises instead of reporting a successful activation the tunnel
       never actually received. (This does NOT change
       ``send_active_repo_control``'s own cache-always-updates contract —
       that cache exists so a reconnecting tunnel picks up the desired repo;
       this function simply refuses to tell ITS caller "activated" unless
       the push really landed.)
    3. The fs-roots expansion (``send_add_fs_roots_control``) is best-effort,
       matching the existing ``set_active_repo`` MCP-tool behavior — it never
       turns a successful Serena activation into a failure.

    On success, returns ``{"status": "ok", "repo_path": ..., "worktree":
    <fingerprint dict>}`` so the caller can surface exactly which worktree is
    now active without a second round-trip. Reuses whatever Serena daemon
    already exists for this path (:class:`~meridian.serena_pool.
    SerenaDaemonPool` keys purely by normalized repo_path) — no extra spawn
    bookkeeping needed here.
    """
    from meridian import db as db_module  # noqa: PLC0415
    from meridian.routes import tunnel as tunnel_routes  # noqa: PLC0415

    row = await db_module.get_worktree(db, worktree_id)
    if row is None or row.get("removed_at") is not None:
        raise WorktreeCodeIntelContextError(
            f"worktree {worktree_id!r} is not registered or has been removed — "
            "activation refused (fail closed)."
        )
    repo_path = row.get("path") or ""
    if not repo_path:
        raise WorktreeCodeIntelContextError(
            f"worktree {worktree_id!r} has no registered path — activation refused."
        )
    result = await tunnel_routes.send_active_repo_control(tenant_id, repo_path)
    if result.get("status") != "ok":
        raise WorktreeCodeIntelContextError(
            "failed to activate worktree code-intel context for "
            f"{worktree_id!r} (status={result.get('status')!r}): "
            f"{result.get('message') or 'no further detail'}"
        )
    try:
        await tunnel_routes.send_add_fs_roots_control(tenant_id, [repo_path])
    except Exception:  # noqa: BLE001 — fs-root expansion is best-effort, same
        # as the existing set_active_repo MCP-tool behavior; the code-intel
        # activation above already succeeded and must not be undone by this.
        pass
    return {
        "status": "ok",
        "repo_path": repo_path,
        "worktree": build_context_fingerprint(row),
    }


def clear_stale_active_repo_cache(worktree_row: dict[str, Any]) -> list[str]:
    """Clear any tenant's cached active-repo pointer that targets a removed
    worktree.

    Called from ``delete_worktree`` (:mod:`meridian.routes.projects`) right
    alongside its existing best-effort on-disk cleanup — same fail-open
    posture: this never raises and never blocks the worktree removal itself.
    Without this, ``_tenant_active_repo`` (and thus every subsequent
    ``call_tunnel_tool``'s ``X-Meridian-Repo-Path`` injection, 4d9ad87b)
    would keep silently pointing code-intel calls at a path that no longer
    has a registered worktree behind it.

    Returns the list of tenant_ids whose cache entry was cleared (empty list
    when nothing matched — the common case).
    """
    from meridian.routes import tunnel as tunnel_routes  # noqa: PLC0415

    target = normalize_context_path(worktree_row.get("path") or "")
    if not target:
        return []
    cleared: list[str] = []
    for tenant_id, cached_path in list(tunnel_routes._tenant_active_repo.items()):
        if normalize_context_path(cached_path) == target:
            del tunnel_routes._tenant_active_repo[tenant_id]
            cleared.append(tenant_id)
    return cleared
