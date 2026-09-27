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
5. Diagnose Meridian guard entries (55d48d69): which component a
   ``meridian_guard*`` command is, its installed scope and default mode, and
   whether its runtime (PowerShell, bash + awk, or the brief's Python)
   resolves on this machine (:func:`diagnose_guard_command`,
   :func:`summarize_guard`). The guard fails open when its runtime is
   missing, so a session never sees such a broken install; this does.

Missing OPTIONAL hooks are a silent, structured no-op -- never surfaced as
a blocking or confusing failure. Missing REQUIRED project hooks still
surface a clear diagnostic (``status == "missing_required"``).

Pure stdlib, no Meridian imports -- safe to import from any module
(``server.py``, ``handoff.py``, ``routes/hooks.py``) without circular-import
risk.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Mapping

# WSL /mnt/<drive>/... mount convention -> native Windows drive path.
_WSL_MOUNT_RE = re.compile(r"^/mnt/([a-zA-Z])(?:/(.*))?$")

# Spellings of the active project's absolute root inside a hook "command"
# string. Claude Code exports CLAUDE_PROJECT_DIR into the hook's ENVIRONMENT;
# bash expands ``$CLAUDE_PROJECT_DIR``, while a ``"shell": "powershell"`` hook
# must read ``$env:CLAUDE_PROJECT_DIR`` (the bare form is an unset PowerShell
# variable there -- see hook_settings_merge.PS_EXIT_SUFFIX). Longest first so
# stripping a token never leaves a fragment of another.
PROJECT_DIR_TOKENS: tuple[str, ...] = (
    "${env:CLAUDE_PROJECT_DIR}",
    "$env:CLAUDE_PROJECT_DIR",
    "${CLAUDE_PROJECT_DIR}",
    "$CLAUDE_PROJECT_DIR",
)

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
    guard_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> list[dict[str, Any]]:
    """Read ``settings_path`` (a ``.claude/settings.json``) and return a
    structured diagnostic per configured hook command -- required project
    hooks resolved against ``repo_root``, optional global hooks resolved as
    absolute paths. Never raises: an unreadable/malformed settings file
    yields an empty list rather than propagating the parse error, since a
    diagnostics helper must never itself become a source of failure.

    A Meridian guard entry (55d48d69) also gets a ``guard`` key -- see
    :func:`diagnose_guard_command`. ``guard_dir``, ``env`` and ``which`` feed
    only that check.
    """
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    results: list[dict[str, Any]] = []
    for event, command in parse_hook_commands(settings):
        diag = resolve_configured_hook_command(command, repo_root)
        diag["event"] = event
        if GUARD_MARKER in command:
            diag["guard"] = diagnose_guard_command(
                command, repo_root, guard_dir=guard_dir, env=env, which=which
            )
        results.append(diag)
    return results


# ---------------------------------------------------------------------------
# Meridian guard entries (55d48d69)
# ---------------------------------------------------------------------------
#
# ``python -m meridian hooks install-guard`` (meridian/hook_settings_merge.py)
# owns every hook entry whose command contains ``meridian_guard``. Project
# scope uses the project-dir token forms handled above (``$env:`` under
# PowerShell, ``$CLAUDE_PROJECT_DIR`` under bash). User scope runs a
# ``meridian_guard_defer`` check first, so the FIRST quoted script in the
# command is the defer check, not the guard shim; the shim is the last one.
# The guard fails open when its runtime is missing (a missing interpreter is a
# non-2 exit, which never blocks), so a broken install is silent in a session:
# this is where it becomes visible.

GUARD_MARKER = "meridian_guard"
GUARD_DEFER_STEM = "meridian_guard_defer"
# Shim stem -> component name.
GUARD_COMPONENTS: dict[str, str] = {
    "meridian_guard": "pre",
    "meridian_guard_post": "post",
    "meridian_guard_brief": "brief",
}
_ALL_SCRIPTS_RE = re.compile(r'"([^"]+\.(?:ps1|sh))"')
# The installer's env-var prefixes (hook_settings_merge.build_command), in
# both the PowerShell ($env:X='v') and the bash (X=v) spelling.
_GUARD_USER_SCOPE_RE = re.compile(r"MERIDIAN_GUARD_SCOPE\s*=\s*'?user\b", re.IGNORECASE)
_GUARD_DEFAULT_ADVISORY_RE = re.compile(r"MERIDIAN_GUARD_DEFAULT_MODE\s*=\s*'?advisory\b", re.IGNORECASE)


def _script_stem(token: str) -> str:
    name = normalize_wsl_path(token).rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]


def guard_script_token(command: str) -> str | None:
    """The quoted guard SHIM path in a hook command (the last quoted script
    whose stem is a guard component), or ``None``."""
    for token in reversed(_ALL_SCRIPTS_RE.findall(command or "")):
        if _script_stem(token) in GUARD_COMPONENTS:
            return token
    return None


def _resolve_token(token: str, repo_root: Path | None) -> Path | None:
    if is_project_relative_command(token):
        if repo_root is None:
            return None
        rel = token
        for tok in PROJECT_DIR_TOKENS:
            rel = rel.replace(tok, "")
        rel_norm = normalize_wsl_path(rel).lstrip("/")
        return (repo_root / rel_norm) if rel_norm else repo_root
    normalized = normalize_wsl_path(token)
    return Path(normalized) if normalized else None


def _guard_python(
    repo_root: Path | None,
    guard_dir: str | Path | None,
    env: Mapping[str, str],
    which: Callable[[str], str | None],
) -> tuple[str | None, str]:
    """The Python the brief shim would start, in its documented order
    (meridian_guard_brief.ps1 header): ``MERIDIAN_GUARD_PYTHON`` (authoritative,
    no fall-through), ``runtime.python`` in ``<guard dir>/config.json``, the
    repo's pixi env, then the ``py`` launcher (``python3`` off Windows).
    Returns ``(path_or_None, source)``."""
    explicit = (env.get("MERIDIAN_GUARD_PYTHON") or "").strip()
    if explicit:
        return (explicit if Path(explicit).is_file() else None), "MERIDIAN_GUARD_PYTHON"
    if guard_dir:
        try:
            cfg = json.loads((Path(guard_dir) / "config.json").read_text(encoding="utf-8"))
            recorded = str(((cfg or {}).get("runtime") or {}).get("python") or "").strip()
        except (OSError, ValueError, AttributeError):
            recorded = ""
        if recorded and Path(recorded).is_file():
            return recorded, "config.json"
    if repo_root is not None:
        for rel in (".pixi/envs/default/python.exe", ".pixi/envs/default/bin/python"):
            candidate = repo_root / rel
            if candidate.is_file():
                return str(candidate), "pixi"
    for launcher in ("py", "python3"):
        found = which(launcher)
        if found:
            return found, launcher
    return None, "unresolved"


def diagnose_guard_command(
    command: str,
    repo_root: Path | None,
    *,
    guard_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Diagnose one Meridian guard hook command: which component it is,
    its installed scope and default mode (from the installer's env-var
    prefixes), and whether its runtime resolves on this machine.

    ``runtime_missing`` lists what is absent: ``script``, ``powershell``
    (a .ps1 shim with neither powershell nor pwsh on PATH), ``bash``,
    ``awk`` / ``meridian_guard.awk`` (the sh PreToolUse/PostToolUse engine),
    ``python`` (the brief). ``env`` and ``which`` default to the current
    process; they describe THIS process, which may differ from the Claude
    Code session that actually runs the hook. Never raises.
    """
    env_map: Mapping[str, str] = os.environ if env is None else env
    which_fn = which or shutil.which
    token = guard_script_token(command)
    stem = _script_stem(token) if token else None
    component = GUARD_COMPONENTS.get(stem or "", "unknown")
    out: dict[str, Any] = {
        "component": component,
        "script_token": token,
        "script_path": None,
        "scope": "user" if _GUARD_USER_SCOPE_RE.search(command or "") else "project",
        "installed_mode": "advisory" if _GUARD_DEFAULT_ADVISORY_RE.search(command or "") else "enforce",
        "interpreter": None,
        "runtime_ok": False,
        "runtime_missing": [],
    }
    try:
        missing: list[str] = []
        path = _resolve_token(token, repo_root) if token else None
        out["script_path"] = str(path) if path is not None else None
        if path is None or not path.is_file():
            missing.append("script")
        ext = (token or "").rsplit(".", 1)[-1].lower() if token else ""
        if ext == "ps1":
            out["interpreter"] = which_fn("powershell") or which_fn("pwsh")
            if not out["interpreter"]:
                missing.append("powershell")
        elif ext == "sh":
            out["interpreter"] = which_fn("bash")
            if not out["interpreter"]:
                missing.append("bash")
            if component in ("pre", "post"):
                if not which_fn("awk"):
                    missing.append("awk")
                if path is not None and not (path.parent / "meridian_guard.awk").is_file():
                    missing.append("meridian_guard.awk")
        if component == "brief":
            py, source = _guard_python(repo_root, guard_dir, env_map, which_fn)
            out["python"] = py
            out["python_source"] = source
            if not py:
                missing.append("python")
        out["runtime_missing"] = missing
        out["runtime_ok"] = not missing and component != "unknown"
    except Exception as exc:  # noqa: BLE001 - diagnostics never raise
        out["runtime_missing"] = [f"error: {type(exc).__name__}"]
        out["runtime_ok"] = False
    return out


def summarize_guard(diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll the per-entry ``guard`` diagnostics up into one status:
    ``registered`` (any guard entry), ``components`` (sorted), ``complete``
    (pre, post and brief all registered), ``runtime_ok`` (every registered
    guard entry resolves) and ``problems`` (one line per broken entry)."""
    entries = [d for d in diagnostics if isinstance(d.get("guard"), dict)]
    components = sorted({d["guard"]["component"] for d in entries})
    problems = [
        f"{d.get('event')}: {d['guard']['component']} missing {', '.join(d['guard']['runtime_missing'])}"
        for d in entries
        if not d["guard"].get("runtime_ok")
    ]
    return {
        "registered": bool(entries),
        "components": components,
        "complete": {"pre", "post", "brief"}.issubset(components),
        "runtime_ok": bool(entries) and not problems,
        "problems": problems,
    }
