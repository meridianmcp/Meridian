"""e401221d — Meridian-side mitigation for Claude Code's dangling-process bug.

Root cause is Anthropic's own client lifecycle (a `claude -p ...
--dangerously-skip-permissions` process, or a spawned pixi/python/node child,
can outlive its terminal / parent session) -- not fixable from inside
Meridian. What IS fixable here: detect and reap orphaned pixi/python/node
processes left running from DEAD worktree sessions -- a worktree whose
owning sprint item reached a terminal status, or whose owning session was
closed/archived (``meridian.db.list_worktrees_pending_cleanup``, a03c0eeb) --
before they accumulate. Adam observed 10-20+ dangling processes building up
across repeated sessions.

Per the sprint item spec, this builds on 273287cb's user-creatable-hooks
infra (``meridian/db/hooks.py`` — ``add_custom_hook``/``custom_hooks`` table,
rendered to ``.claude/hooks/`` by ``handoff._write_custom_hooks``) instead of
hardcoding reaping logic directly into ``sprint_guard.{sh,ps1}``.
``seed_orphan_reaper_hook`` below registers ONE Stop-hook row (idempotent);
the generic 273287cb pipeline takes it from there — sprint_guard.{sh,ps1}
gain zero new logic.

The registered hook body is a thin one-line shim (``_ORPHAN_REAPER_SH`` /
``_ORPHAN_REAPER_PS1``) that shells out to ``python -m meridian.orphan_reaper``
so the actual enumeration/matching/kill logic lives in ONE well-tested,
cross-platform Python module instead of being duplicated across two shell
dialects (and so it can be unit-tested with mocked process-listing/kill —
see tests/test_e401221d_orphan_reaper.py). Uses ``psutil`` when available,
which on Windows is itself backed by WMI/Win32_Process — satisfying the
"Windows-compatible, not POSIX-only pkill" requirement without a second,
untestable implementation living only in PowerShell text. Degrades to a
silent no-op when psutil is missing or any step fails, matching the
optional-psutil pattern already used elsewhere in this codebase (see
``meridian/tunnel_client.py``'s slot-claim liveness checks). This hook is
registered ``blocking=False`` (advisory) — cleanup must never gate a Stop.

f7084ed0 — deterministic, tree-safe, opt-out cleanup. Three additions on top
of the above, mirroring patterns already proven elsewhere in this codebase
rather than inventing new ones:

1. **Ownership-validated kill.** ``list_orphan_candidates`` now also records
   each process's ``create_time`` (when psutil provides one). Immediately
   before signalling anything, ``reap_orphans`` re-snapshots live processes
   and requires the pid to still resolve to the SAME process (matching name
   and, when both sides have one, matching ``create_time`` within a 1s
   tolerance — see ``_identity_matches``) before it will touch it. A pid that
   has since exited, or been reassigned by the OS to an unrelated process
   (PID reuse), is a safe no-op (``"already_gone"`` / ``"identity_mismatch"``
   in the result's ``skipped`` list) rather than a kill. This is the exact
   PID+create_time guard ``meridian/tunnel_client.py`` already uses for its
   own orphan sweeps (``_kill_all_previously_spawned_pids``,
   ``_is_slot_claimed_by_live_client``) — reused here, not reinvented.
2. **Tree-safe kill.** The default kill primitive (``_psutil_kill_tree``)
   terminates the WHOLE process family rooted at the matched pid, not just
   that one pid — on Windows via ``taskkill /F /T /PID`` (mirrors
   ``tunnel_client._terminate_proc_tree``'s Windows path exactly, since a
   plain ``proc.terminate()`` only kills the direct child and orphans
   node/cmd/python grandchildren); on POSIX via psutil's recursive
   ``children(recursive=True)`` walked and terminated alongside the parent,
   escalating to ``kill()`` for anything still alive after the grace period.
3. **Disabled by default, dashboard-toggleable.** ``seed_orphan_reaper_hook``
   now registers the Stop hook DISABLED (``enabled=False``) the first time it
   runs for a project — cleanup is opt-in, never silently active. Toggling is
   ``set_orphan_reaper_enabled`` (flips the ``custom_hooks.enabled`` flag via
   the existing 273287cb ``update_custom_hook`` path — no schema change
   needed) exposed over HTTP as ``GET``/``POST .../orphan_reaper(/toggle)``
   in ``meridian/server.py`` for the dashboard. Disabling immediately deletes
   any already-written ``.claude/hooks/orphan_reaper.*`` files
   (``remove_orphan_reaper_artifacts``) instead of waiting for the next
   ``generate_handoff`` to simply stop re-writing them.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

# Slug this hook is registered under in custom_hooks. Deliberately NOT
# "sprint_guard" — that slug is reserved (see db/hooks.py::_RESERVED_HOOK_SLUGS)
# for Meridian's own auto-managed Stop guard.
HOOK_NAME = "orphan_reaper"
HOOK_EVENT = "Stop"

# Process name substrings we consider reap candidates, matched
# case-insensitively against the process's base executable name (e.g.
# "python.exe", "pixi", "node.exe", "pythonw.exe", "pixi.exe"). f7084ed0 adds
# cmd/uv/conhost — the original list missed cmd.exe (pixi/npm often shell out
# through it on Windows), uv.exe/uvx.exe (covered by the "uv" substring), and
# conhost.exe (the console host backing a detached worktree session's window)
# — all called out as coverage gaps in this item's prospecting notes.
_TARGET_NAME_SUBSTRINGS: tuple[str, ...] = (
    "pixi", "python", "node", "cmd", "uv", "conhost",
)

# f7084ed0 — defense-in-depth denylist: never treat a process as a candidate
# if its name matches one of these, even if it happens to also match a
# substring above (e.g. a hypothetical future target substring colliding with
# part of "msedgewebview2.exe"). Nothing in _TARGET_NAME_SUBSTRINGS matches
# WebView2 today, but this makes that safety property explicit and future-
# proof rather than incidental.
_EXCLUDED_NAME_SUBSTRINGS: tuple[str, ...] = ("webview2",)


def _proc_name_is_target(name: str) -> bool:
    """True if *name* looks like one of the target process families
    (case-insensitive substring match — covers .exe suffixes and
    pythonw/python3/uvx variants) AND is not explicitly denylisted."""
    lname = (name or "").lower()
    if any(sub in lname for sub in _EXCLUDED_NAME_SUBSTRINGS):
        return False
    return any(sub in lname for sub in _TARGET_NAME_SUBSTRINGS)


def _norm_path(p: str) -> str:
    """Normalize a path-like string for prefix/substring comparison: forward
    slashes, lowercased. Deliberately PURE STRING normalization — no
    filesystem resolution (Path.resolve()). The values compared here are
    often a dead worktree directory (which may no longer exist on disk by
    the time this runs) or a whole process cmdline string, neither of which
    is a real, resolvable path on this process's own filesystem; resolving
    against the filesystem would silently rewrite them relative to CWD and
    corrupt the comparison instead of leaving the caller's own recorded
    string intact. Always lowercased rather than OS-conditional so the same
    dead_paths/cmdline pairing (always recorded on the same host OS in real
    usage) compares consistently regardless of which OS this function itself
    happens to run on."""
    return p.replace("\\", "/").lower()


def process_belongs_to_dead_worktree(
    proc_cwd: str | None, proc_cmdline: str | None, dead_paths: list[str]
) -> str | None:
    """Return the matching dead-worktree path if *proc_cwd* or *proc_cmdline*
    is rooted under (or otherwise references) one of *dead_paths*, else None.

    Pure and side-effect-free (no OS calls) so the matching rule itself is
    trivially unit-testable independent of psutil / real process listing.
    """
    candidates = [c for c in (proc_cwd, proc_cmdline) if c]
    if not candidates or not dead_paths:
        return None
    norm_dead = [(_norm_path(dp), dp) for dp in dead_paths if dp]
    for raw in candidates:
        norm_raw = _norm_path(raw)
        for norm_dp, orig_dp in norm_dead:
            if not norm_dp:
                continue
            if norm_raw == norm_dp or norm_raw.startswith(norm_dp + "/"):
                return orig_dp
            # cmdline is a whole command string (e.g. "python C:\...\worktree\x.py");
            # the worktree path can appear anywhere inside it, not just as a prefix.
            if norm_dp in norm_raw:
                return orig_dp
    return None


def _identity_matches(candidate: dict[str, Any], current: dict[str, Any] | None) -> bool:
    """Pure ownership check: is *current* (a fresh process snapshot taken
    right before a kill, or ``None`` if that pid no longer resolves to any
    live process) still the SAME process *candidate* was matched as a moment
    earlier by ``list_orphan_candidates``?

    Guards against two distinct races between "we found this pid" and "we're
    about to signal this pid": the process simply exited on its own in the
    meantime (``current is None`` -> False, reason the caller should record
    as ``"already_gone"``), or — the more dangerous case — the OS reassigned
    that same pid to a completely unrelated process in the interim (PID
    reuse; ``current is not None`` but its name/create_time disagree with
    what was originally observed -> False, ``"identity_mismatch"``). Mirrors
    the PID+create_time guard ``meridian/tunnel_client.py`` already applies
    before killing anything (``_kill_all_previously_spawned_pids``).

    Pure and side-effect-free — both snapshots are plain dicts, no OS calls —
    so this is trivially unit-testable independent of psutil / real process
    listing, same as ``process_belongs_to_dead_worktree`` above.

    Missing/``None`` fields degrade to "nothing to contradict" rather than
    "reject": a candidate/snapshot pair that never carried ``create_time``
    (e.g. a psutil-less environment, or a caller that only tracks name/cwd)
    still matches on name alone, same as this reaper's pre-f7084ed0
    behavior — this check only ADDS a rejection path, it never narrows the
    cases that were previously allowed to proceed.
    """
    if current is None:
        return False
    cand_name = (candidate.get("name") or "").strip().lower()
    cur_name = (current.get("name") or "").strip().lower()
    if cand_name and cur_name and cand_name != cur_name:
        return False
    cand_ct = candidate.get("create_time")
    cur_ct = current.get("create_time")
    if cand_ct is not None and cur_ct is not None:
        try:
            if abs(float(cur_ct) - float(cand_ct)) >= 1.0:
                return False
        except (TypeError, ValueError):
            pass  # unparsable create_time on either side — nothing to contradict
    return True


def list_orphan_candidates(
    dead_paths: list[str],
    process_iter: Callable[[], Iterable[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Enumerate live processes and return the ones that look like orphans of
    *dead_paths*: name matches a target substring AND its cwd/cmdline is
    rooted under one of the dead paths.

    *process_iter* is the test injection point: a zero-arg callable returning
    an iterable of process-info dicts (``{"pid", "name", "cwd", "cmdline"}``).
    Defaults to a real ``psutil``-backed enumeration; when ``psutil`` is
    unavailable (or enumeration raises for any reason) this returns ``[]``
    rather than raising — best-effort, never crashes the calling hook.
    """
    if not dead_paths:
        return []
    if process_iter is None:
        process_iter = _psutil_process_iter
    try:
        procs = list(process_iter())
    except Exception:  # noqa: BLE001 — enumeration failure must never crash the hook
        logger.warning("orphan_reaper: process enumeration failed", exc_info=True)
        return []

    out: list[dict[str, Any]] = []
    for info in procs:
        try:
            name = info.get("name") or ""
            if not _proc_name_is_target(name):
                continue
            matched = process_belongs_to_dead_worktree(
                info.get("cwd"), info.get("cmdline"), dead_paths
            )
            if matched:
                out.append({**info, "matched_path": matched})
        except Exception:  # noqa: BLE001 — one bad process record must not sink the scan
            continue
    return out


def _psutil_process_iter() -> list[dict[str, Any]]:
    """Real process enumeration via ``psutil`` (optional dependency). On
    Windows psutil's process listing is itself backed by native process APIs
    equivalent to WMI/Win32_Process; on POSIX it reads /proc. Returns ``[]``
    when psutil is unavailable — self-hosted-only, best-effort, same degrade
    pattern as the rest of this codebase's optional psutil usage."""
    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001 — psutil not installed
        return []
    out: list[dict[str, Any]] = []
    for p in psutil.process_iter(["pid", "name", "cwd", "cmdline", "create_time"]):
        try:
            info = p.info
            cmdline = " ".join(info.get("cmdline") or [])
            out.append(
                {
                    "pid": info.get("pid"),
                    "name": info.get("name") or "",
                    "cwd": info.get("cwd") or "",
                    "cmdline": cmdline,
                    "create_time": info.get("create_time"),
                }
            )
        except Exception:  # noqa: BLE001 — a single vanished/permission-denied process must not sink the scan
            continue
    return out


def _psutil_collect_tree(pid: int) -> list[int]:
    """Real (psutil-backed) descendant enumeration: given a root pid, return
    that pid plus every currently-live descendant's pid (recursive), so a
    tree-safe kill can terminate the whole family a matched process spawned
    (pixi -> python -> ...), not just the single matched pid — addresses the
    "doesn't recurse child groups" gap noted in this item's prospecting.
    Best-effort: a lookup failure (process already gone, permission denied)
    degrades to just ``[pid]`` rather than raising."""
    try:
        import psutil  # type: ignore

        proc = psutil.Process(pid)
        children = [c.pid for c in proc.children(recursive=True)]
        return [pid, *children]
    except Exception:  # noqa: BLE001
        return [pid]


def _psutil_kill_tree(pid: int) -> bool:
    """Best-effort real kill: tree-safe and cross-platform.

    Windows: ``taskkill /F /T /PID`` — kills the whole process tree by PID in
    one OS call. Mirrors ``meridian.tunnel_client._terminate_proc_tree``'s
    Windows path exactly (a plain ``proc.terminate()`` only kills the direct
    child, orphaning node/cmd/python grandchildren — the same failure mode
    this item's prospecting notes flagged).

    POSIX: recursively collects live descendants (:func:`_psutil_collect_tree`),
    terminates the whole family together, waits, then escalates to ``kill()``
    for any stragglers — graceful-then-forced signaling.

    Returns True iff the process is confirmed gone afterward (or was already
    gone). Never raises — ownership/identity validation happens one layer up
    in ``reap_orphans``, not here; this is purely "given a pid we've already
    decided is safe to touch, make it and its children go away."
    """
    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001 — psutil not installed
        return False

    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, check=False, timeout=10,
            )
        except Exception:  # noqa: BLE001 — fall through to the pid_exists check below
            pass
        try:
            return not psutil.pid_exists(pid)
        except Exception:  # noqa: BLE001
            return False

    tree_pids = _psutil_collect_tree(pid)
    procs = []
    for p in tree_pids:
        try:
            procs.append(psutil.Process(p))
        except Exception:  # noqa: BLE001 — already gone by the time we look it up
            continue
    for proc in procs:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    try:
        _gone, alive = psutil.wait_procs(procs, timeout=3)
    except Exception:  # noqa: BLE001
        alive = procs
    for proc in alive:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        psutil.wait_procs(alive, timeout=3)
    except Exception:  # noqa: BLE001
        pass
    try:
        return not psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001
        return False


def reap_orphans(
    dead_paths: list[str],
    process_iter: Callable[[], Iterable[dict[str, Any]]] | None = None,
    kill_fn: Callable[[int], bool] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Find and (unless *dry_run*) kill orphaned target-family processes
    (see ``_TARGET_NAME_SUBSTRINGS``) rooted under one of *dead_paths*. Never
    raises. Returns a machine-readable, JSON-safe result dict — the same
    shape whether run for real or in ``dry_run`` mode — suitable for a
    dashboard preview or a CLI ``--dry-run`` print.

    f7084ed0 — ownership-validated: immediately before signalling anything
    (never during a dry run, which signals nothing), re-snapshots live
    processes via *process_iter* (or the real psutil enumeration when not
    injected) and requires each candidate's pid to still resolve to the SAME
    process it was originally matched as (see ``_identity_matches``) — a pid
    that has exited, or been reused by an unrelated process, in the window
    between the initial scan and the kill is recorded in ``skipped`` with
    reason ``"already_gone"`` / ``"identity_mismatch"`` and never signalled.
    A revalidation-snapshot failure degrades to "treat every candidate as
    unconfirmed" (fail closed — never falls back to killing blindly).

    *kill_fn* is the test injection point for killing (given a pid, return
    True if the process was successfully terminated); defaults to
    :func:`_psutil_kill_tree` — a real, tree-safe (whole process family, not
    just the matched pid), ``psutil``-backed terminate/kill escalation.
    """
    candidates = list_orphan_candidates(dead_paths, process_iter=process_iter)
    if kill_fn is None:
        kill_fn = _psutil_kill_tree
    killed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    fresh_by_pid: dict[Any, dict[str, Any]] = {}
    if candidates and not dry_run:
        revalidate_iter = process_iter or _psutil_process_iter
        try:
            fresh_by_pid = {p.get("pid"): p for p in revalidate_iter()}
        except Exception:  # noqa: BLE001 — can't revalidate; every candidate below
            fresh_by_pid = {}  # will correctly fail closed as "already_gone"

    for c in candidates:
        if dry_run:
            skipped.append({**c, "reason": "dry_run"})
            continue
        current = fresh_by_pid.get(c.get("pid"))
        if not _identity_matches(c, current):
            reason = "already_gone" if current is None else "identity_mismatch"
            skipped.append({**c, "reason": reason})
            continue
        try:
            ok = bool(kill_fn(int(c["pid"])))
        except Exception:  # noqa: BLE001 — a single kill failure must not abort the batch
            ok = False
        if ok:
            killed.append(c)
        else:
            skipped.append({**c, "reason": "kill_failed"})
    return {
        "candidates_count": len(candidates),
        "killed": killed,
        "skipped": skipped,
        "killed_count": len(killed),
        "skipped_count": len(skipped),
    }


def fetch_dead_worktree_paths(
    base_url: str, project_id: str, timeout: float = 5.0
) -> list[str]:
    """Fetch this project's pending-cleanup worktree paths from the Meridian
    server (``GET /projects/{project_id}/worktrees/pending_cleanup`` — see
    ``routes/projects.py``, which wraps ``db.list_worktrees_pending_cleanup``,
    a03c0eeb). Best-effort: returns ``[]`` on any network/parse failure so the
    hook degrades to a silent no-op — this is a Stop hook, never a blocking
    gate, and MUST fail open exactly like sprint_guard.{sh,ps1}."""
    url = f"{base_url.rstrip('/')}/projects/{project_id}/worktrees/pending_cleanup"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — trusted, own MERIDIAN_URL
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [str(row["path"]) for row in data if isinstance(row, dict) and row.get("path")]


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m meridian.orphan_reaper``, invoked from the
    thin sh/ps1 hook wrappers registered by ``seed_orphan_reaper_hook``.
    Always exits 0 — advisory-only cleanup, must never block Claude Code.

    ``--dry-run`` prints the full machine-readable ``reap_orphans`` result as
    JSON to stdout (candidates found, none signalled) — so a human or the
    dashboard can preview exactly what a real run would touch, including any
    identity-mismatch/already-gone safe no-ops, before opting in for real.
    """
    parser = argparse.ArgumentParser(description="Reap orphaned pixi/python/node processes from dead Meridian worktree sessions.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--url", default=os.environ.get("MERIDIAN_URL") or "http://localhost:7878")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        dead_paths = fetch_dead_worktree_paths(args.url, args.project_id)
        if not dead_paths:
            return 0
        result = reap_orphans(dead_paths, dry_run=args.dry_run)
        if args.dry_run:
            print(json.dumps(result, default=str))
        elif result["killed_count"]:
            print(
                f"Meridian orphan_reaper: reaped {result['killed_count']} dangling "
                f"process(es) from {len(dead_paths)} dead worktree(s).",
                file=sys.stderr,
            )
    except Exception:  # noqa: BLE001 — advisory cleanup must never fail the Stop hook
        logger.warning("orphan_reaper: unexpected failure", exc_info=True)
    return 0


# ---------------------------------------------------------------------------
# Hook script bodies — registered via seed_orphan_reaper_hook (273287cb infra)
# ---------------------------------------------------------------------------

_ORPHAN_REAPER_SH = """# e401221d — Meridian orphan-process reaper (auto-registered via
# add_custom_hook / 273287cb custom-hooks infra, NOT hardcoded into
# sprint_guard.sh). Advisory-only (blocking=False): best-effort cleanup of
# dangling pixi/python/node processes left behind by dead worktree sessions.
# Never blocks the Stop hook even if this fails, times out, or python/pixi
# is not on PATH in this repo.
{
  if [ -n "$CLAUDE_PROJECT_DIR" ]; then cd "$CLAUDE_PROJECT_DIR" 2>/dev/null || true; fi
  MERIDIAN_HOOK_URL="${MERIDIAN_URL:-__URL__}"
  ( pixi run python -m meridian.orphan_reaper --project-id "__PROJECT_ID__" --url "$MERIDIAN_HOOK_URL" \\
    || python3 -m meridian.orphan_reaper --project-id "__PROJECT_ID__" --url "$MERIDIAN_HOOK_URL" \\
    || python -m meridian.orphan_reaper --project-id "__PROJECT_ID__" --url "$MERIDIAN_HOOK_URL" ) >/dev/null 2>&1
} || true
exit 0
"""

_ORPHAN_REAPER_PS1 = """# e401221d — Meridian orphan-process reaper (auto-registered via
# add_custom_hook / 273287cb custom-hooks infra, NOT hardcoded into
# sprint_guard.ps1). Advisory-only (blocking=False): best-effort cleanup of
# dangling pixi/python/node processes left behind by dead worktree sessions.
# Never blocks the Stop hook even if this fails, times out, or python/pixi
# is not on PATH in this repo.
$ErrorActionPreference = 'SilentlyContinue'
if ($env:CLAUDE_PROJECT_DIR) { Set-Location $env:CLAUDE_PROJECT_DIR }
$ProjectId = '__PROJECT_ID__'
$Url = if ($env:MERIDIAN_URL) { $env:MERIDIAN_URL } else { '__URL__' }
try {
    pixi run python -m meridian.orphan_reaper --project-id $ProjectId --url $Url 2>$null | Out-Null
} catch {
    try {
        python -m meridian.orphan_reaper --project-id $ProjectId --url $Url 2>$null | Out-Null
    } catch {}
}
exit 0
"""


async def seed_orphan_reaper_hook(db: Any, project_id: str, url: str | None = None) -> dict[str, Any]:
    """Idempotently register the orphan-reaper Stop hook for *project_id* via
    the 273287cb custom-hooks infra (``db.hooks.add_custom_hook`` /
    ``update_custom_hook``) — this is the ONLY place reaping logic gets wired
    in; ``sprint_guard.{sh,ps1}`` are never touched.

    Safe to call repeatedly (e.g. once per ``generate_handoff``): if a hook
    with slug ``orphan_reaper`` already exists for this project, its script
    bodies are refreshed in place (so a template update ships to existing
    projects) rather than erroring on the duplicate-slug guard in
    ``add_custom_hook``. Refreshing NEVER touches ``enabled`` — whatever a
    human (or ``set_orphan_reaper_enabled``) last set persists across every
    future ``generate_handoff`` call. Returns the resulting ``custom_hooks``
    row.

    f7084ed0 — a brand-new row is created ``enabled=False``: cleanup is
    opt-in, disabled by default, until a human flips it on via the dashboard
    toggle (``set_orphan_reaper_enabled`` / ``GET``/``POST
    /projects/{id}/orphan_reaper`` in ``meridian/server.py``). Before this,
    every project got this Stop hook silently active the moment it first ran
    ``generate_handoff`` — no opt-out existed.
    """
    from . import db as db_module  # noqa: PLC0415 — avoid import cycle at module load

    base_url = url or os.environ.get("MERIDIAN_URL") or "http://localhost:7878"
    script_sh = _ORPHAN_REAPER_SH.replace("__PROJECT_ID__", project_id).replace("__URL__", base_url)
    script_ps1 = _ORPHAN_REAPER_PS1.replace("__PROJECT_ID__", project_id).replace("__URL__", base_url)

    existing_hooks = await db_module.get_custom_hooks(db, project_id, event=HOOK_EVENT)
    existing = next((h for h in existing_hooks if h.get("slug") == HOOK_NAME), None)
    if existing is not None:
        return await db_module.update_custom_hook(
            db, project_id, existing["id"],
            script_sh=script_sh, script_ps1=script_ps1,
        )
    return await db_module.add_custom_hook(
        db, project_id,
        name=HOOK_NAME,
        event=HOOK_EVENT,
        script_sh=script_sh,
        script_ps1=script_ps1,
        blocking=False,
        enabled=False,
    )


# f7084ed0 — filenames _write_custom_hooks (meridian/handoff.py's
# _render_custom_hook_files) writes for this hook: a non-blocking hook with
# both script_sh and script_ps1 renders 3 files (the .ps1 body is wrapped, so
# there's both the wrapper and the "_body.ps1" it shells out to). Kept as a
# single source of truth so seeding and artifact-removal never drift apart.
ORPHAN_REAPER_HOOK_FILENAMES: tuple[str, ...] = (
    f"{HOOK_NAME}.sh", f"{HOOK_NAME}.ps1", f"{HOOK_NAME}_body.ps1",
)


async def set_orphan_reaper_enabled(
    db: Any, project_id: str, enabled: bool, url: str | None = None
) -> dict[str, Any]:
    """The ONLY path that flips the orphan-reaper Stop hook's ``enabled``
    flag — backs the dashboard toggle (``meridian/server.py``'s
    ``GET``/``POST /projects/{project_id}/orphan_reaper(/toggle)`` routes).

    Ensures the hook row exists first (via ``seed_orphan_reaper_hook`` —
    idempotent, created disabled by default on a brand-new project, or
    refreshed with its CURRENT enabled state preserved for an existing one),
    then explicitly sets ``enabled`` only if it differs from the current
    value — an update that never touches ``script_sh``/``script_ps1``.
    Returns the resulting ``custom_hooks`` row.

    Callers that want the "disabling removes generated hook artifacts
    immediately" behavior should follow a ``enabled=False`` call with
    :func:`remove_orphan_reaper_artifacts` against the project's own
    ``.claude/hooks`` dir (this function has no filesystem access of its
    own — it only touches the DB row, mirroring every other function in this
    module's DB-layer/filesystem-layer split).
    """
    from . import db as db_module  # noqa: PLC0415 — avoid import cycle at module load

    hook = await seed_orphan_reaper_hook(db, project_id, url=url)
    if bool(hook.get("enabled")) == bool(enabled):
        return hook
    updated = await db_module.update_custom_hook(
        db, project_id, hook["id"], enabled=bool(enabled),
    )
    return updated if updated is not None else hook


def remove_orphan_reaper_artifacts(hooks_dir: "Path | str") -> list[str]:
    """Delete any already-written orphan-reaper hook files from *hooks_dir*
    (a repo's ``.claude/hooks`` directory) immediately, rather than waiting
    for the next ``generate_handoff`` to simply stop re-writing them (the
    existing behavior for every other custom hook — see
    ``db.hooks.delete_custom_hook``'s docstring). Returns the filenames
    actually removed (empty list if none were present). Best-effort and pure
    filesystem cleanup: never raises, never touches the DB, never touches
    anything outside *hooks_dir* itself.
    """
    removed: list[str] = []
    try:
        base = Path(hooks_dir)
    except Exception:  # noqa: BLE001 — malformed path input
        return removed
    for filename in ORPHAN_REAPER_HOOK_FILENAMES:
        try:
            path = base / filename
            if path.exists():
                path.unlink()
                removed.append(filename)
        except Exception:  # noqa: BLE001 — one bad file must not abort the rest
            continue
    return removed


if __name__ == "__main__":
    raise SystemExit(main())
