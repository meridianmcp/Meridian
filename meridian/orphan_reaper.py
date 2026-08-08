"""e401221d — Meridian-side mitigation for Claude Code's dangling-process bug.

15610335 — this module also reclaims stale EXTERNAL Pixi detached
environments left behind by dead worktrees (see ``meridian.pixi_env_retention``
for why a registry/marker is needed at all: Pixi's ``detached-environments``
moves each worktree's ~1GB environment OUTSIDE the git-tracked tree, into a
directory keyed by a hash of the worktree's own path, so ``git worktree
remove`` never touches it). ``reclaim_stale_pixi_envs`` below reuses the same
``dead_paths`` this module already fetches for process-reaping and matches
them against marker files written under the configured external root — no
new server route needed, this is pure local-filesystem work.

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
import ctypes
import json
import logging
import os
import re
import shutil
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


# ---------------------------------------------------------------------------
# 60a96ece -- read-only runtime-pressure diagnostics (opt-in, never kills).
#
# Fresh Windows evidence (2026-08-08) reported kernel pools growing (6.42GB
# paged / 4.52GB nonpaged) while per-process pool counters summed to only
# ~156MB paged / ~19MB nonpaged, and at least two Serena servers targeting
# the SAME repo. This section answers the item's scope questions WITHOUT
# mutating anything:
#
#   1. "Capture process ownership ... duplicate Serena/MCP fingerprints
#      without killing unrelated processes" -> list_live_runtime_processes /
#      find_duplicate_runtime_fingerprints below: pure enumeration, grouped
#      by (runtime_kind, repo_key), never signals or kills.
#   2. "Determine whether Meridian's owned-process registry/reaper ... can
#      deduplicate compatible runtimes" -> for Serena specifically, YES --
#      meridian.serena_pool already implements a host-local broker
#      (config-fingerprint-matched adoption + refcounted lease files) that
#      is wired ON BY DEFAULT in tunnel_client.py
#      (`SerenaDaemonPool(..., broker_dir=serena_pool.default_broker_dir())`).
#      This module's own reap_orphans/list_orphan_candidates deliberately do
#      NOT dedup -- they only ever reap orphans of DEAD worktrees, a
#      narrower problem than "two live daemons for one live repo".
#      tunnel_client.py's generic owned-process primitive
#      (_spawn_owned_with_cache_retry / _close_owned_process /
#      process_lifecycle.WindowsJobObjectBackend) also does not dedup, BY
#      DESIGN: its single responsibility is safely owning/tearing-down ONE
#      process tree (Job Object / process group), not resource pooling --
#      pooling belongs one layer up, exactly where SerenaDaemonPool already
#      sits for Serena. Extending an equivalent broker to OTHER (non-Serena)
#      owned runtime kinds is a real gap, but is its own sprint-scale design
#      question (what counts as a "compatible" config fingerprint for an
#      arbitrary MCP server?) -- flagged as a documented follow-up rather
#      than improvised here.
#   3. "Add a cross-platform, opt-in diagnostic path for kernel-pool/driver
#      evidence where available" -> process_pool_evidence: Windows prefers
#      psutil's memory_info().paged_pool/nonpaged_pool, falling back to a
#      PSUTIL-FREE raw K32GetProcessMemoryInfo ctypes call (same
#      prefer-psutil / ctypes-fallback shape as
#      scripts/run_tests.py::_pid_is_running's psutil-preferred / win32
#      ctypes-fallback pid-liveness probe); POSIX returns process-group
#      membership only, EXPLICITLY marked `kernel_pool_attributable: False`
#      -- never claimed as kernel-pool evidence. poolmon_available()
#      documents, rather than closes, the driver-tag-attribution gap: this
#      module's per-process accounting is real but partial evidence (the
#      investigation's own numbers show per-process sums an order of
#      magnitude below system-wide pool growth) -- full driver-tag
#      attribution needs poolmon.exe, which this diagnostic detects the
#      absence of but does not install or substitute for.
# ---------------------------------------------------------------------------

# Cmdline tokens marking a Serena MCP-server launch -- mirrors
# meridian.serena_pool.is_serena_command's own two-token check, but against
# a joined cmdline STRING (this module's process dicts store cmdline as one
# string -- see _psutil_process_iter -- not the list serena_pool works
# with), so it is duplicated here rather than imported to avoid a
# cross-module type mismatch.
_SERENA_CMDLINE_MARKERS: tuple[str, ...] = ("serena-agent", "start-mcp-server")

# A target-family process (pixi/python/node/... -- see
# _TARGET_NAME_SUBSTRINGS) is only treated as a runtime-diagnostic candidate
# when its cmdline ALSO hints at being a Meridian-relevant MCP runtime --
# narrows this diagnostic to "duplicate Serena/MCP fingerprints" (the
# sprint's literal ask) instead of flagging every unrelated python/node dev
# process on a shared machine that happens to share a cwd.
_RUNTIME_CMDLINE_HINTS: tuple[str, ...] = ("mcp", "meridian", "serena")

_PROJECT_FLAG_RE = re.compile(r"--project(?:=|\s+)(\"[^\"]+\"|'[^']+'|\S+)")


def _runtime_candidate_kind(name: str, cmdline: str) -> "str | None":
    """Return a runtime-kind label if (*name*, *cmdline*) looks like a
    Meridian-relevant Serena/MCP runtime worth diagnosing, else None. Pure
    string matching -- no OS calls, trivially unit-testable independent of
    process enumeration."""
    lname = (name or "").lower()
    lcmd = (cmdline or "").lower()
    if lcmd and all(marker in lcmd for marker in _SERENA_CMDLINE_MARKERS):
        return "serena"
    if not _proc_name_is_target(name):
        return None
    if not lcmd or not any(hint in lcmd for hint in _RUNTIME_CMDLINE_HINTS):
        return None
    for sub in _TARGET_NAME_SUBSTRINGS:
        if sub in lname:
            return sub
    return lname or "unknown"


def _extract_repo_fingerprint(cwd: "str | None", cmdline: "str | None") -> "str | None":
    """Best-effort repo-path fingerprint for a process: the value of a
    ``--project <path>`` flag in its cmdline (Serena's own launch flag) when
    present, else its cwd, else None. Pure string handling, no filesystem
    resolution -- the same "don't resolve a possibly-foreign path" caution
    documented on :func:`_norm_path` above."""
    if cmdline:
        m = _PROJECT_FLAG_RE.search(cmdline)
        if m:
            val = m.group(1).strip("\"'")
            if val:
                return _norm_path(val)
    if cwd:
        return _norm_path(cwd)
    return None


def list_live_runtime_processes(
    process_iter: "Callable[[], Iterable[dict[str, Any]]] | None" = None,
) -> "list[dict[str, Any]]":
    """Enumerate ALL currently-live Meridian-relevant Serena/MCP runtime
    processes -- unlike :func:`list_orphan_candidates`, this is NOT scoped
    to dead worktrees; it is the "what's running right now" half of this
    item's diagnostic scope. Pure, read-only: never signals or kills
    anything.

    Each entry additionally carries ``runtime_kind`` (e.g. ``"serena"``,
    ``"pixi"``, ``"python"``) and ``repo_key`` (a best-effort repo-path
    fingerprint, see :func:`_extract_repo_fingerprint`), plus a combined
    ``fingerprint`` string used by :func:`find_duplicate_runtime_fingerprints`
    to group candidate duplicates. Entries whose repo_key could not be
    determined get ``fingerprint: None`` and are excluded from duplicate
    grouping -- ambiguous evidence is never silently treated as a match.

    *process_iter* is the same test-injection point as
    :func:`list_orphan_candidates`; defaults to the real psutil-backed
    enumeration and degrades to ``[]`` (never raises) on any enumeration
    failure.
    """
    if process_iter is None:
        process_iter = _psutil_process_iter
    try:
        procs = list(process_iter())
    except Exception:  # noqa: BLE001 — enumeration failure must never crash a diagnostic
        logger.warning("orphan_reaper: live-runtime enumeration failed", exc_info=True)
        return []

    out: "list[dict[str, Any]]" = []
    for info in procs:
        try:
            name = info.get("name") or ""
            cmdline = info.get("cmdline") or ""
            kind = _runtime_candidate_kind(name, cmdline)
            if kind is None:
                continue
            repo_key = _extract_repo_fingerprint(info.get("cwd"), cmdline)
            fingerprint = f"{kind}:{repo_key}" if repo_key else None
            out.append({**info, "runtime_kind": kind, "repo_key": repo_key, "fingerprint": fingerprint})
        except Exception:  # noqa: BLE001 — one bad record must not sink the scan
            continue
    return out


def find_duplicate_runtime_fingerprints(
    process_iter: "Callable[[], Iterable[dict[str, Any]]] | None" = None,
) -> "list[dict[str, Any]]":
    """Group :func:`list_live_runtime_processes` output by fingerprint
    (runtime_kind + repo_key) and return only groups with MORE THAN ONE live
    pid -- candidate duplicate Meridian-owned Serena/MCP runtimes for the
    same repo. Read-only: returns data for a human/dashboard to review,
    never kills anything itself.

    A returned group is a CANDIDATE, not a verdict: two distinct worktree
    directories for "the same" logical repo (e.g. a main checkout and a
    ``.claude/worktrees/<id>`` child) are, correctly, DIFFERENT repo_key
    values and never grouped here -- each worktree legitimately needs its
    own Serena instance to serve the files actually on disk there. A group
    forming means the SAME resolved repo_key + runtime_kind was observed
    across more than one live pid -- exactly the situation
    ``meridian.serena_pool``'s broker exists to prevent for Serena
    specifically (see this section's module-level comment above) -- a
    non-empty group here is either a pre-broker-era leftover process, a
    broker adoption that failed for some reason (e.g. config-fingerprint
    drift), or a non-Serena runtime kind the broker doesn't cover yet.
    """
    procs = list_live_runtime_processes(process_iter=process_iter)
    groups: "dict[str, list[dict[str, Any]]]" = {}
    for p in procs:
        fp = p.get("fingerprint")
        if fp:
            groups.setdefault(fp, []).append(p)
    return [
        {
            "fingerprint": fp,
            "runtime_kind": items[0]["runtime_kind"],
            "repo_key": items[0]["repo_key"],
            "pids": [i.get("pid") for i in items],
            "processes": items,
        }
        for fp, items in groups.items()
        if len(items) > 1
    ]


# ── Windows kernel-pool evidence: psutil-preferred, ctypes psutil-free fallback ──

_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class Win32MemoryInfoAPI:
    """Thin, injectable wrapper over the kernel32 calls needed for a
    psutil-free per-process paged/nonpaged kernel-pool read -- mirrors
    ``process_lifecycle.Win32JobAPI``'s own injectable-loader shape so tests
    exercise the Windows code path on non-Windows CI via a fake object,
    never a real ``ctypes.WinDLL`` (which does not exist off Windows)."""

    def __init__(self, kernel32: Any):
        self._k = kernel32

    def open_process(self, pid: int) -> "int | None":
        h = self._k.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, int(pid))
        return int(h) if h else None

    def get_memory_info(self, handle: int) -> "_PROCESS_MEMORY_COUNTERS_EX | None":
        counters = _PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        ok = self._k.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        return counters if ok else None

    def close_handle(self, handle: int) -> bool:
        return bool(self._k.CloseHandle(handle))


def _load_win32_memory_api() -> "Win32MemoryInfoAPI | None":
    """Real loader: binds + prototypes the kernel32 calls
    :class:`Win32MemoryInfoAPI` needs. Returns ``None`` (never raises) off
    Windows or on any bind failure -- :func:`process_pool_evidence` degrades
    to psutil-or-nothing in that case, mirroring
    ``process_lifecycle._load_win32_job_api``'s own degrade contract."""
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.K32GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
        ]
        kernel32.K32GetProcessMemoryInfo.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        return Win32MemoryInfoAPI(kernel32)
    except Exception:  # noqa: BLE001
        return None


def _win32_pool_usage(
    pid: int, api_loader: "Callable[[], Win32MemoryInfoAPI | None] | None" = None,
) -> "dict[str, Any] | None":
    """psutil-free fallback: paged/nonpaged kernel-pool bytes *pid* is
    charged for, via a raw ``K32GetProcessMemoryInfo`` call. ``None`` (never
    raises) on any failure -- missing API, ``OpenProcess`` denied, process
    already gone."""
    loader = api_loader or _load_win32_memory_api
    try:
        api = loader()
    except Exception:  # noqa: BLE001
        api = None
    if api is None:
        return None
    handle = None
    try:
        handle = api.open_process(pid)
        if handle is None:
            return None
        counters = api.get_memory_info(handle)
        if counters is None:
            return None
        return {
            "paged_pool_bytes": int(counters.QuotaPagedPoolUsage),
            "nonpaged_pool_bytes": int(counters.QuotaNonPagedPoolUsage),
            "source": "ctypes",
        }
    except Exception:  # noqa: BLE001
        return None
    finally:
        if handle is not None:
            try:
                api.close_handle(handle)
            except Exception:  # noqa: BLE001
                pass


# os.getpgid does not exist AT ALL on Windows -- resolved once at import time
# via getattr with a None fallback (mirrors process_lifecycle.py's own
# `_killpg = getattr(os, "killpg", None)` pattern) so a test that
# monkeypatches `orphan_reaper.sys.platform` to "linux" on a real Windows dev
# box can exercise the POSIX branch of process_pool_evidence below without
# ever touching a genuinely-missing Windows stdlib attribute.
_getpgid = getattr(os, "getpgid", None)


def process_pool_evidence(
    pid: int, api_loader: "Callable[[], Win32MemoryInfoAPI | None] | None" = None,
) -> "dict[str, Any]":
    """Best-effort, read-only per-process kernel-pool/process-group evidence
    for *pid*. Never raises; always returns a dict (never ``None``) so a
    caller can render it directly.

    Windows: prefers ``psutil``'s ``memory_info().paged_pool``/
    ``nonpaged_pool`` (a real Windows-only field psutil already exposes,
    backed by the same ``PROCESS_MEMORY_COUNTERS_EX`` this module's own
    ctypes fallback reads directly) and falls back to :func:`_win32_pool_usage`
    -- a psutil-FREE raw ``K32GetProcessMemoryInfo`` call -- when psutil is
    not installed, matching this codebase's established
    prefer-psutil/ctypes-fallback convention (see
    ``scripts/run_tests.py::_pid_is_running``).

    POSIX: returns process-group membership only (``os.getpgid``) --
    "process-group evidence, explicitly marked unverified for pool/driver
    attribution" per this item's own scope notes
    (``kernel_pool_attributable: False``). Linux has no equivalent of
    Windows' paged/nonpaged kernel-pool accounting exposed per-process
    through a comparably simple call.

    Returns ``{"source": "unavailable", "kernel_pool_attributable": False}``
    (never raises) when no evidence could be gathered at all.
    """
    if sys.platform == "win32":
        try:
            import psutil  # type: ignore

            mem = psutil.Process(pid).memory_info()
            paged = getattr(mem, "paged_pool", None)
            nonpaged = getattr(mem, "nonpaged_pool", None)
            if paged is not None and nonpaged is not None:
                return {
                    "paged_pool_bytes": int(paged),
                    "nonpaged_pool_bytes": int(nonpaged),
                    "source": "psutil",
                    "kernel_pool_attributable": True,
                }
        except Exception:  # noqa: BLE001 — fall through to the ctypes fallback below
            pass
        fallback = _win32_pool_usage(pid, api_loader=api_loader)
        if fallback is not None:
            fallback["kernel_pool_attributable"] = True
            return fallback
        return {"source": "unavailable", "kernel_pool_attributable": False}

    if _getpgid is None:
        return {"source": "unavailable", "kernel_pool_attributable": False}
    try:
        pgid = _getpgid(int(pid))
    except Exception:  # noqa: BLE001
        return {"source": "unavailable", "kernel_pool_attributable": False}
    return {
        "process_group_id": pgid,
        "source": "process_group",
        "kernel_pool_attributable": False,
        "note": "process-group membership only -- not kernel-pool/driver evidence",
    }


def poolmon_available() -> bool:
    """True iff ``poolmon``/``poolmon.exe`` is discoverable on ``PATH``.
    ``False`` (the expected result on most machines -- poolmon ships with
    the Windows Driver Kit, not a default Windows install) means full
    driver-tag kernel-pool attribution stays an explicit, DOCUMENTED gap for
    :func:`diagnose_runtime_pressure`: this module can report best-effort
    PER-PROCESS pool usage (:func:`process_pool_evidence`) but cannot
    attribute pool growth to a specific kernel driver/tag without poolmon.
    Never raises."""
    try:
        return shutil.which("poolmon") is not None or shutil.which("poolmon.exe") is not None
    except Exception:  # noqa: BLE001
        return False


def diagnose_runtime_pressure(
    process_iter: "Callable[[], Iterable[dict[str, Any]]] | None" = None,
    pool_evidence_fn: "Callable[[int], dict[str, Any]] | None" = None,
) -> "dict[str, Any]":
    """Read-only, opt-in diagnostic snapshot combining live-runtime
    enumeration, duplicate-fingerprint detection, and best-effort
    per-process kernel-pool/process-group evidence into one JSON-safe dict
    -- the dashboard-visible surface for this item's scope (see the section
    comment above for how each scope point is answered). NEVER kills,
    signals, or otherwise mutates anything; safe to call at any time,
    including against a live, shared, multi-session dev machine.

    Does NOT claim Meridian caused any observed system-wide kernel-pool
    growth. Per the 2026-08-08 investigation this diagnostic exists to
    follow up on, per-process pool usage summed across every process this
    function can see was a small fraction of total system-wide pool growth
    -- the remainder is plausibly kernel/driver or sustained IPC/IO pressure
    with NO per-process attribution available short of poolmon's driver-tag
    accounting (see :func:`poolmon_available`).

    *process_iter* / *pool_evidence_fn* are the test-injection points
    (mirrors :func:`list_orphan_candidates` / :func:`reap_orphans`'s own
    *process_iter* / *kill_fn* injection pattern) -- production callers get
    the real psutil enumeration and the real :func:`process_pool_evidence`;
    tests always inject fakes so no real OS process is ever inspected or
    signalled by a test.
    """
    evidence_fn = pool_evidence_fn or process_pool_evidence
    procs = list_live_runtime_processes(process_iter=process_iter)
    for p in procs:
        pid = p.get("pid")
        if pid is None:
            p["pool_evidence"] = None
            continue
        try:
            p["pool_evidence"] = evidence_fn(pid)
        except Exception:  # noqa: BLE001 — one bad evidence lookup must not sink the scan
            p["pool_evidence"] = None

    groups: "dict[str, list[dict[str, Any]]]" = {}
    for p in procs:
        fp = p.get("fingerprint")
        if fp:
            groups.setdefault(fp, []).append(p)
    duplicate_groups = [
        {
            "fingerprint": fp,
            "runtime_kind": items[0]["runtime_kind"],
            "repo_key": items[0]["repo_key"],
            "pids": [i.get("pid") for i in items],
        }
        for fp, items in groups.items()
        if len(items) > 1
    ]

    return {
        "platform": sys.platform,
        "process_count": len(procs),
        "processes": procs,
        "duplicate_groups": duplicate_groups,
        "duplicate_group_count": len(duplicate_groups),
        "poolmon_available": poolmon_available(),
        "kernel_pool_attribution_note": (
            "Per-process paged/nonpaged pool usage only, best-effort "
            "(process_pool_evidence). Full driver-tag attribution requires "
            "poolmon.exe -- see poolmon_available. This diagnostic never "
            "claims Meridian caused observed system-wide kernel-pool growth."
        ),
    }


def reclaim_stale_pixi_envs(
    dead_paths: list[str],
    pixi_env_root: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Find and (unless *dry_run*) remove external Pixi detached-environment
    directories whose marker file matches one of *dead_paths* — the
    external-environment counterpart to :func:`reap_orphans`. Never raises.

    *pixi_env_root* defaults to
    ``meridian.pixi_env_retention.default_detached_environments_root()``
    when omitted (``~/.pixi/workspace-envs``); pass it explicitly (or set
    ``MERIDIAN_PIXI_ENV_ROOT``, wired in :func:`main`) when this machine's
    global Pixi config points somewhere else.
    """
    from . import pixi_env_retention  # noqa: PLC0415 — avoid import cost when unused

    root = Path(pixi_env_root) if pixi_env_root else pixi_env_retention.default_detached_environments_root()
    try:
        candidates = pixi_env_retention.find_external_envs_for_dead_worktrees(root, dead_paths)
    except Exception:  # noqa: BLE001 — discovery failure must never crash the hook
        logger.warning("orphan_reaper: pixi env discovery failed", exc_info=True)
        return {"candidates_count": 0, "reclaimed_count": 0, "skipped_count": 0, "reclaimed": [], "skipped": []}

    reclaimed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for c in candidates:
        if dry_run:
            skipped.append({**c, "reason": "dry_run"})
            continue
        try:
            outcome = pixi_env_retention.reclaim_external_env(c["path"], root, confirm=True)
        except Exception:  # noqa: BLE001 — one bad reclaim must not sink the batch
            skipped.append({**c, "reason": "reclaim_raised"})
            continue
        if outcome.get("removed"):
            reclaimed.append(c)
        else:
            skipped.append({**c, "reason": outcome.get("detail", "reclaim_failed")})
    return {
        "candidates_count": len(candidates),
        "reclaimed_count": len(reclaimed),
        "skipped_count": len(skipped),
        "reclaimed": reclaimed,
        "skipped": skipped,
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


# ---------------------------------------------------------------------------
# 2ae3f011 -- temp-output quarantine discovery for dead worktrees.
#
# Dead worktrees (the same `dead_paths` this module already fetches to reap
# orphaned processes above) can also contain genuinely valuable TEMPORARY
# OUTPUT files -- data/figures a script dropped during the run -- that a
# naive `worktree_cleanup.remove_worktree_on_disk` would otherwise wipe
# along with the rest of the scaffolding. This is the DISCOVERY half of
# that story: a pure, best-effort filesystem walk for files whose NAME
# matches a temp/archival-output naming convention. It deliberately mirrors
# (does NOT import -- extensions/meridian-outputs is not on this pixi env's
# dependency graph, see pixi.toml's 52cbe5d8 comment) the broader stage-1b
# suffix conventions in
# `extensions/meridian-outputs/meridian_outputs/classify.py`.
#
# Matching a name pattern here is only a PREFILTER, never a verdict: turning
# a candidate into an actual quarantine action still requires a real
# ownership/provenance check (see `worktree_cleanup.build_quarantine_manifest`'s
# injected `ownership_check` -- a real caller supplies
# `provenance.classify_temp_output_ownership` from the meridian-outputs
# extension). `main()`'s own `--quarantine-outputs` flag below only ever
# prints a DRY-RUN manifest -- it never moves or deletes a file itself.
# ---------------------------------------------------------------------------

_TEMP_OUTPUT_SUFFIX_RE = re.compile(
    r"[_-](?:backup|bak|deprecated|mislabeled|wip|copy|stale|archived?|old(?:_\d+)?)"
    r"(?:[_.]\S*)?$|\.(?:bak|orig|backup)(?:[_.].*)?$|~$",
    re.IGNORECASE,
)


def find_temp_output_candidates(dead_paths: list[str]) -> list[str]:
    """Best-effort filesystem walk of *dead_paths* for files whose name
    matches a temp/archival-output naming convention. Returns absolute-ish
    file paths as strings (whatever ``os.walk`` yields joined with the
    filename) -- pure discovery, no filesystem mutation. Never raises: an
    unreadable/missing directory just contributes nothing, matching every
    other best-effort scan in this module."""
    out: list[str] = []
    for root in dead_paths or []:
        if not root or not os.path.isdir(root):
            continue
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    stem = os.path.splitext(name)[0]
                    if _TEMP_OUTPUT_SUFFIX_RE.search(name) or _TEMP_OUTPUT_SUFFIX_RE.search(stem):
                        out.append(os.path.join(dirpath, name))
        except OSError:  # noqa: BLE001 -- best-effort scan, one bad dir must not sink it
            continue
    return out


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
    parser.add_argument(
        "--quarantine-outputs",
        action="store_true",
        help=(
            "Also print a DRY-RUN temp-output quarantine manifest for dead "
            "worktrees. Advisory only -- this CLI entry point never moves or "
            "deletes a file itself (no ownership_check is wired here; see "
            "meridian.worktree_cleanup.build_quarantine_manifest)."
        ),
    )
    parser.add_argument(
        "--archive-root",
        default=None,
        help="Archive root used only for the --quarantine-outputs dry-run manifest.",
    )
    parser.add_argument(
        "--pixi-env-root",
        default=os.environ.get("MERIDIAN_PIXI_ENV_ROOT"),
        help="15610335 — external Pixi detached-environments root to sweep for "
             "stale entries (default: ~/.pixi/workspace-envs via "
             "meridian.pixi_env_retention.default_detached_environments_root).",
    )
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
        if args.quarantine_outputs:
            _print_quarantine_dry_run(dead_paths, args.archive_root)
        env_result = reclaim_stale_pixi_envs(
            dead_paths, pixi_env_root=args.pixi_env_root, dry_run=args.dry_run,
        )
        if env_result["reclaimed_count"]:
            print(
                f"Meridian orphan_reaper: reclaimed {env_result['reclaimed_count']} "
                f"stale external Pixi environment(s).",
                file=sys.stderr,
            )
    except Exception:  # noqa: BLE001 — advisory cleanup must never fail the Stop hook
        logger.warning("orphan_reaper: unexpected failure", exc_info=True)
    return 0


def _print_quarantine_dry_run(dead_paths: list[str], archive_root: str | None) -> None:
    """Best-effort dry-run manifest print for ``--quarantine-outputs``. Never
    raises (caught by ``main``'s own broad except regardless, but this stays
    consistent with every other best-effort step in this module) and never
    moves/deletes anything -- see the module docstring above ``main``."""
    from . import worktree_cleanup  # noqa: PLC0415 — avoid import cost when unused

    candidates = find_temp_output_candidates(dead_paths)
    if not candidates:
        return
    root = archive_root or os.path.join(os.getcwd(), ".meridian_quarantine")
    manifest = worktree_cleanup.build_quarantine_manifest(candidates, archive_root=root)
    print(
        f"Meridian orphan_reaper: temp-output quarantine dry-run found "
        f"{manifest['eligible_count']} eligible file(s) of {manifest['total']} "
        f"candidate(s) under dead worktree(s) (archive_root={root}). No files "
        "were moved or deleted -- ownership confirmation must come from the "
        "meridian-outputs extension.",
        file=sys.stderr,
    )


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
