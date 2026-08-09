"""Cross-platform active-repository resolver for generated + configured
Claude Code hooks (e5eec33b).

Reproduced 2026-08-07 in a Claude executor: PreToolUse hook commands in
``.claude/settings.json`` reference scripts using the ``$CLAUDE_PROJECT_DIR``
convention (e.g. ``"& \\"$CLAUDE_PROJECT_DIR\\.claude\\hooks\\secret_guard.ps1\\""``).
Claude Code substitutes that token with the active project's absolute root
before invoking the shell. When the launcher shell's ``CLAUDE_PROJECT_DIR``
is empty, the substitution collapses the whole command to a bare,
drive-root-relative fragment (``"\\.claude\\hooks\\secret_guard.ps1"`` on
Windows) that never resolves to the real repo -- even though the
repo-local hook file genuinely exists on disk. That is an
invocation/path-resolution failure, not a missing-hook-content problem.

This module gives Meridian's own Python-side tooling (the handoff
hook-writer, the hooks route, diagnostics) ONE canonical, testable way to:

1. Resolve the active repo root without ever trusting a blank
   ``CLAUDE_PROJECT_DIR`` to produce a root-relative path
   (:func:`resolve_active_repo_root`).
2. Normalize WSL-style ``/mnt/c/...`` paths to native Windows form, so a
   path recorded from a WSL/Linux session still resolves correctly when
   read back on Windows (:func:`normalize_wsl_path` -- the canonical
   implementation; ``meridian.server._normalize_hook_cwd_path`` delegates
   to it so the two never drift apart).
3. Classify a configured hook "command" string as a REQUIRED project hook
   (rooted at ``$CLAUDE_PROJECT_DIR``, must exist in the active repo) vs an
   OPTIONAL global hook (a hardcoded per-machine path outside the repo,
   e.g. ``~/.claude/hooks/meridian-stop.ps1`` written once by
   ``hooks.ps1``/``hooks.sh`` -- legitimately absent until that installer
   has run on this machine) (:func:`resolve_configured_hook_command`,
   :func:`diagnose_configured_hooks`).
4. Validate a stored ``executor_config.repo_path`` before trusting it as a
   generated-hook write target (:func:`resolve_repo_root_for_handoff`),
   used by ``handoff._write_sprint_guard_hooks``.

Missing OPTIONAL hooks are a silent, structured no-op -- never surfaced as
a blocking or confusing failure. Missing REQUIRED project hooks still
surface a clear diagnostic (``status == "missing_required"``).

Pure stdlib, no Meridian imports -- safe to import from any module
(``server.py``, ``handoff.py``, ``routes/hooks.py``) without circular-import
risk.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# WSL /mnt/<drive>/... mount convention -> native Windows drive path.
_WSL_MOUNT_RE = re.compile(r"^/mnt/([a-zA-Z])(?:/(.*))?$")

# Token(s) Claude Code substitutes with the active project's absolute root
# before invoking a hook "command" string.
PROJECT_DIR_TOKENS: tuple[str, ...] = ("$CLAUDE_PROJECT_DIR", "${CLAUDE_PROJECT_DIR}")

# Script path embedded in a hook "command" string, e.g.
# '& "$CLAUDE_PROJECT_DIR\.claude\hooks\secret_guard.ps1"' or a bare
# absolute global path like '"C:\Users\me\.claude\hooks\meridian-stop.ps1"'.
_COMMAND_SCRIPT_RE = re.compile(r'"([^"]+\.(?:ps1|sh))"')

# Diagnostic status values -- see resolve_configured_hook_command.
STATUS_OK = "ok"
STATUS_MISSING_REQUIRED = "missing_required"
STATUS_OPTIONAL_ABSENT = "optional_absent"
STATUS_UNRESOLVABLE = "unresolvable"


def normalize_wsl_path(path: str) -> str:
    """Normalize a filesystem path to the canonical form used for hook-path
    matching and resolution.

    Converts WSL ``/mnt/c/...`` paths to ``C:/...``, backslashes to forward
    slashes, and strips a trailing slash. Mirrors (and is the canonical
    implementation backing) ``meridian.server._normalize_hook_cwd_path`` and
    the nested ``_normalize_hook_cwd`` in ``hooks_session_start``, so every
    hook-path consumer normalizes identically.
    """
    value = (path or "").strip().replace("\\", "/")
    m = _WSL_MOUNT_RE.match(value)
    if m:
        drive = m.group(1).upper()
        rest = (m.group(2) or "").strip("/")
        value = f"{drive}:/{rest}" if rest else f"{drive}:/"
    return value.rstrip("/")


def resolve_active_repo_root(
    claude_project_dir: str | None = None,
    *,
    cwd: str | None = None,
    session_project_root: str | None = None,
) -> Path | None:
    """Resolve the active repository root for locating generated/configured
    Claude Code hooks.

    Precedence:

    1. ``claude_project_dir`` (normally the ``CLAUDE_PROJECT_DIR`` env var)
       if non-empty -- the normal, fast path.
    2. An explicit ``session_project_root`` (e.g. a project's stored
       ``executor_config.repo_path``) if given.
    3. The process ``cwd`` (or an explicit ``cwd`` override), walked upward
       to the nearest directory containing a ``.claude`` marker -- this
       keeps worktree cwds correct: a worktree checkout has its OWN
       ``.claude`` directory, so the walk stops there rather than
       continuing up into the main checkout.

    Never collapses to a bare, root-relative ``".claude"`` fragment: when
    every input is blank, callers get ``None`` (or the raw cwd as a last
    resort) instead of a path built by concatenating an empty string with
    ``".claude/..."``.
    """
    for raw in (claude_project_dir, session_project_root):
        if raw and raw.strip():
            normalized = normalize_wsl_path(raw)
            if normalized:
                return Path(normalized)

    start = Path(cwd) if cwd and cwd.strip() else Path.cwd()
    try:
        start = start.resolve()
    except OSError:
        pass
    for candidate in (start, *start.parents):
        if (candidate / ".claude").is_dir():
            return candidate
    # Nothing had a .claude marker -- still return the (resolved) cwd rather
    # than None, since a caller with only a bare cwd and no signal at all is
    # better served by "best guess" than by silently vanishing. This is
    # never confused with a root-relative ".claude" path because it is a
    # full, absolute directory.
    return start


def resolve_repo_root_for_handoff(repo_path: str) -> Path | None:
    """Validate + normalize a stored ``executor_config.repo_path`` before
    trusting it as a generated-hook write target.

    Handles a ``repo_path`` recorded from a WSL/Linux session (e.g.
    ``/mnt/c/Users/me/repo``) being read back on native Windows -- without
    normalization, ``Path("/mnt/c/Users/me/repo")`` never resolves on
    Windows and a real, valid repo looks indistinguishable from a garbage
    value. Returns ``None`` when the (normalized) path doesn't exist or has
    no ``.claude`` directory -- "no repo of its own" case documented on
    ``handoff._write_sprint_guard_hooks``.
    """
    normalized = normalize_wsl_path(repo_path or "")
    if not normalized:
        return None
    root = Path(normalized)
    if not (root / ".claude").exists():
        return None
    return root


def is_project_relative_command(command: str) -> bool:
    """True when a hook "command" string is scoped to the active repo via
    the ``$CLAUDE_PROJECT_DIR`` substitution token (a REQUIRED project
    hook), as opposed to a hardcoded absolute path outside the repo (an
    OPTIONAL global hook)."""
    return any(tok in (command or "") for tok in PROJECT_DIR_TOKENS)


def extract_script_path_token(command: str) -> str | None:
    """Pull the quoted ``*.ps1``/``*.sh`` script path out of a hook
    "command" string, or ``None`` if the command doesn't reference one."""
    m = _COMMAND_SCRIPT_RE.search(command or "")
    return m.group(1) if m else None


def resolve_configured_hook_command(
    command: str, repo_root: Path | None
) -> dict[str, Any]:
    """Resolve ONE hook "command" string to a structured diagnostic.

    Returns a dict with keys ``command``, ``script_token``,
    ``resolved_path``, ``required``, ``exists``, ``status``. ``status`` is
    one of:

    * ``"ok"`` -- resolved and the file exists.
    * ``"missing_required"`` -- a repo-scoped ($CLAUDE_PROJECT_DIR) hook
      whose script does not exist -- a genuine problem, surface it.
    * ``"optional_absent"`` -- a global/per-machine hook (no
      $CLAUDE_PROJECT_DIR token) whose script does not exist -- expected
      until the user runs the global installer on this machine; treat as a
      silent no-op, never a blocking or confusing failure.
    * ``"unresolvable"`` -- no script path could be extracted, or a
      required hook has no repo root to resolve against.
    """
    token = extract_script_path_token(command)
    if token is None:
        return {
            "command": command,
            "script_token": None,
            "resolved_path": None,
            "required": False,
            "exists": False,
            "status": STATUS_UNRESOLVABLE,
        }

    required = is_project_relative_command(token)
    resolved: Path | None
    if required:
        if repo_root is None:
            return {
                "command": command,
                "script_token": token,
                "resolved_path": None,
                "required": True,
                "exists": False,
                "status": STATUS_UNRESOLVABLE,
            }
        rel = token
        for tok in PROJECT_DIR_TOKENS:
            rel = rel.replace(tok, "")
        rel_norm = normalize_wsl_path(rel).lstrip("/")
        resolved = (repo_root / rel_norm) if rel_norm else repo_root
    else:
        normalized = normalize_wsl_path(token)
        resolved = Path(normalized) if normalized else None

    exists = bool(resolved is not None and resolved.exists())
    if exists:
        status = STATUS_OK
    elif required:
        status = STATUS_MISSING_REQUIRED
    else:
        status = STATUS_OPTIONAL_ABSENT

    return {
        "command": command,
        "script_token": token,
        "resolved_path": str(resolved) if resolved is not None else None,
        "required": required,
        "exists": exists,
        "status": status,
    }


def parse_hook_commands(settings: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract ``(event_name, command)`` pairs from a parsed
    ``.claude/settings.json`` ``hooks`` block, across all events/matchers."""
    out: list[tuple[str, str]] = []
    hooks = (settings or {}).get("hooks") or {}
    for event, entries in hooks.items():
        for entry in entries or []:
            for h in (entry or {}).get("hooks", []) or []:
                cmd = h.get("command", "")
                if cmd:
                    out.append((event, cmd))
    return out


def diagnose_configured_hooks(
    settings_path: Path,
    *,
    repo_root: Path | None,
) -> list[dict[str, Any]]:
    """Read ``settings_path`` (a ``.claude/settings.json``) and return a
    structured diagnostic per configured hook command -- required project
    hooks resolved against ``repo_root``, optional global hooks resolved as
    absolute paths. Never raises: an unreadable/malformed settings file
    yields an empty list rather than propagating the parse error, since a
    diagnostics helper must never itself become a source of failure.
    """
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    results: list[dict[str, Any]] = []
    for event, command in parse_hook_commands(settings):
        diag = resolve_configured_hook_command(command, repo_root)
        diag["event"] = event
        results.append(diag)
    return results
