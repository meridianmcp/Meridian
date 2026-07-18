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
"""
from __future__ import annotations

import argparse
import json
import logging
import os
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
# "python.exe", "pixi", "node.exe", "pythonw.exe", "pixi.exe").
_TARGET_NAME_SUBSTRINGS: tuple[str, ...] = ("pixi", "python", "node")


def _proc_name_is_target(name: str) -> bool:
    """True if *name* looks like one of pixi/python/node (case-insensitive
    substring match — covers .exe suffixes and pythonw/python3 variants)."""
    lname = (name or "").lower()
    return any(sub in lname for sub in _TARGET_NAME_SUBSTRINGS)


def _norm_path(p: str) -> str:
    """Normalize a filesystem path for prefix/substring comparison: resolve
    when possible, forward slashes, lowercase on Windows (case-insensitive
    filesystem). Never raises — falls back to a lightly-normalized raw string
    if resolution fails (e.g. the path no longer exists, or *p* is actually a
    whole command-line string rather than a bare path)."""
    try:
        resolved = str(Path(p).resolve())
    except Exception:  # noqa: BLE001 — best-effort normalization only
        resolved = p
    resolved = resolved.replace("\\", "/")
    if os.name == "nt" or sys.platform == "win32":
        resolved = resolved.lower()
    return resolved


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
    for p in psutil.process_iter(["pid", "name", "cwd", "cmdline"]):
        try:
            info = p.info
            cmdline = " ".join(info.get("cmdline") or [])
            out.append(
                {
                    "pid": info.get("pid"),
                    "name": info.get("name") or "",
                    "cwd": info.get("cwd") or "",
                    "cmdline": cmdline,
                }
            )
        except Exception:  # noqa: BLE001 — a single vanished/permission-denied process must not sink the scan
            continue
    return out


def _psutil_kill(pid: int) -> bool:
    """Best-effort real kill: terminate(), escalate to kill() if still alive
    after a short grace wait. Returns True iff the process is confirmed gone
    afterward (or was already gone). Never raises."""
    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001 — psutil not installed
        return False
    try:
        proc = psutil.Process(pid)
    except Exception:  # noqa: BLE001 — psutil.NoSuchProcess or similar — already gone
        return True
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001 — still alive after grace period; escalate
            proc.kill()
            proc.wait(timeout=3)
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
    """Find and (unless *dry_run*) kill orphaned pixi/python/node processes
    rooted under one of *dead_paths*. Never raises.

    *kill_fn* is the test injection point for killing (given a pid, return
    True if the process was successfully terminated); defaults to a real
    ``psutil``-backed terminate/kill escalation.
    """
    candidates = list_orphan_candidates(dead_paths, process_iter=process_iter)
    if kill_fn is None:
        kill_fn = _psutil_kill
    killed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for c in candidates:
        if dry_run:
            skipped.append({**c, "reason": "dry_run"})
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
    Always exits 0 — advisory-only cleanup, must never block Claude Code."""
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
        if result["killed_count"]:
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
    ``add_custom_hook``. Returns the resulting ``custom_hooks`` row.
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
        enabled=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
