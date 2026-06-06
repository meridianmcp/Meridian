"""Best-effort git commit of Meridian-managed markdown files.

Called at checkpoint / session-end with the set of .md files the auto-update
machinery touched this session (see ``md_anchors.drain_touched``). The commit is:

* **scoped** — only the named files are staged (explicit pathspec, never
  ``git add -A``) and ``git commit -- <paths>`` commits *only* those pathspecs,
  leaving any other staged/unstaged work in the user's tree untouched.
* **best-effort** — every git invocation tolerates a non-zero exit; a missing
  git, a non-repo dir, a failing pre-commit hook, or an unconfigured identity
  simply means "not committed" and never blocks the checkpoint.
* **hosted no-op** — skipped entirely under ``MERIDIAN_HOSTED``.

Subprocesses run via ``subprocess.run`` on a worker thread (``asyncio.to_thread``)
rather than ``asyncio.create_subprocess_exec`` because Meridian forces the Windows
SelectorEventLoop, which cannot spawn asyncio subprocesses.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path


def _hosted_mode() -> bool:
    return os.environ.get("MERIDIAN_HOSTED", "").lower() in ("1", "true", "yes")


def _run_git_sync(argv: list[str], cwd: str, timeout: float) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return (proc.returncode, proc.stdout, proc.stderr)
    except FileNotFoundError:
        return (-1, "", "git executable not found")
    except subprocess.TimeoutExpired:
        return (-1, "", "git timed out")
    except Exception as exc:  # noqa: BLE001 — surface any spawn error as non-fatal
        return (-1, "", str(exc))


async def _git(*argv: str, cwd: Path, timeout: float = 10.0) -> tuple[int, str, str]:
    """Run a git command off the event loop. Never raises; returns
    ``(returncode, stdout, stderr)`` with returncode ``-1`` on spawn failure."""
    return await asyncio.to_thread(_run_git_sync, list(argv), str(cwd), timeout)


async def commit_touched_md(
    paths: list[Path], message: str, *, cwd: Path
) -> dict:
    """Stage and commit exactly ``paths`` (the touched .md files). Returns a small
    status dict; ``{"committed": True, ...}`` only when a commit was actually
    created. Always safe to call — never raises, never touches files outside
    ``paths``."""
    if _hosted_mode():
        return {"committed": False, "reason": "hosted"}
    if not paths:
        return {"committed": False, "reason": "nothing-touched"}

    rc, out, _ = await _git("rev-parse", "--is-inside-work-tree", cwd=cwd)
    if rc != 0 or out.strip() != "true":
        return {"committed": False, "reason": "not-a-repo"}

    staged: list[str] = []
    for p in paths:
        rc, _, _ = await _git("add", "--", str(p), cwd=cwd)
        if rc == 0:
            staged.append(str(p))
    if not staged:
        return {"committed": False, "reason": "add-failed"}

    # `git diff --cached --quiet`: rc 0 => nothing staged among these paths.
    rc, _, _ = await _git("diff", "--cached", "--quiet", "--", *staged, cwd=cwd)
    if rc == 0:
        return {"committed": False, "reason": "nothing-staged"}

    rc, _, err = await _git("commit", "-m", message, "--", *staged, cwd=cwd)
    if rc != 0:
        return {"committed": False, "reason": "commit-failed", "stderr": err.strip()[:300]}
    return {"committed": True, "paths": staged}
