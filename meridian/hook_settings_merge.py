"""Idempotent install / uninstall of the Meridian guard hooks into a Claude
Code ``settings.json`` (sprint item 55d48d69, design "install_scope").

``python -m meridian hooks install-guard --repo P [--scope project|user]
[--mode enforce|advisory] [--shell powershell|bash] [--dry-run] [--uninstall]``

Ownership rule
--------------
This module owns ONLY hook entries whose ``command`` contains
``meridian_guard``. Every other entry -- the repo's existing guards
(hitl_guard, worktree_guard, secret_guard, ...), third-party ``cbm-*`` hooks,
anything a user added by hand -- is preserved exactly: same position, same
keys, same values. Only the file's whitespace is re-rendered, using the
file's own detected indentation, newline style, BOM and trailing-newline so
an unrelated entry round-trips byte-for-byte in the common case.
``autoMemoryEnabled`` is never written (item 81f403aa owns that switch, and
only after an owner-approved memory import).

Registered entries (explicit timeouts: 3 s Pre/PostToolUse, 10 s
SessionStart/SubagentStart -- the Claude Code default is 60 s):

* project scope -- one PreToolUse dispatcher (G1-G11 matchers, never matches
  ``Read``), one PostToolUse entry (G12-G14), SessionStart + SubagentStart
  brief (G15/G16).
* user scope -- only G6-G8 (auto-memory / Serena-memory write denies) and the
  G15/G16 brief, per the design.

Shim contract (what the guard shims read; nothing else is passed)
-----------------------------------------------------------------
* ``MERIDIAN_GUARD_DEFAULT_MODE=advisory`` is prefixed to the command when
  installed with ``--mode advisory``. It is the LOWEST-precedence mode input:
  the owner's ``MERIDIAN_GUARD`` env var and the sentinel files still win.
  ``--mode enforce`` (the owner-chosen default) adds nothing.
* ``MERIDIAN_GUARD_SCOPE=user`` is set for user-scope entries; a shim that
  sees it evaluates only G0, G6-G8 and the brief.
* Mode and scope travel as environment variables rather than script
  parameters on purpose: an unknown named argument makes a PowerShell script
  that declares ``param()`` fail to bind, which (hooks fail open) would
  silently disable the guard.

Dedupe rule (design: "--scope user ... is skipped when a project-scope guard
exists")
-----------------------------------------------------------------------------
User-scope commands first run a generated ``meridian_guard_defer`` check. It
exits the hook with no output when the active project (``CLAUDE_PROJECT_DIR``)
registers its own project-scope ``meridian_guard`` entry in
``.claude/settings.json`` or ``.claude/settings.local.json``, so the same
rule never fires twice from two settings scopes. The check never reads stdin
(the payload stays for the shim) and fails toward running the guard. Within a
single project, a project install also removes stray owned entries from
``settings.local.json`` so the project is registered exactly once.

Safety
------
* Malformed settings JSON aborts the whole operation; nothing is written.
* A timestamped backup of every settings file is written to
  ``<guard dir>/backups/`` (outside the repo, so it never shows up in git)
  before the first byte changes.
* ``--dry-run`` renders the unified diff and the file operations and writes
  nothing at all.
* ``--uninstall`` removes the owned entries (settings.json AND
  settings.local.json), the install marker, and every shim file this
  installer copied whose content is unchanged since the copy. Shims that are
  committed source files (the Meridian repo dogfood case, where the shim
  source directory IS the target) are never copied and never deleted.
* Project id and Meridian URL are recorded only in the per-machine
  ``<guard dir>/config.json`` (never in repo files), together with the Python
  interpreter that ran the install -- the snapshot builder must use that
  runtime rather than whatever ``python`` is first on PATH.

Pure stdlib; imports only ``hook_paths`` from Meridian.
"""
from __future__ import annotations

import argparse
import codecs
import datetime as _dt
import difflib
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .hook_paths import normalize_wsl_path

# ---------------------------------------------------------------------------
# Constants (the contract other tracks rely on)
# ---------------------------------------------------------------------------

OWNER_MARKER = "meridian_guard"
SHIM_PRE = "meridian_guard"
SHIM_POST = "meridian_guard_post"
SHIM_BRIEF = "meridian_guard_brief"
DEFER_BASENAME = "meridian_guard_defer"
INSTALL_MARKER_NAME = "meridian_guard.install.json"
CONFIG_NAME = "config.json"

DEFAULT_MODE_ENV = "MERIDIAN_GUARD_DEFAULT_MODE"
SCOPE_ENV = "MERIDIAN_GUARD_SCOPE"
GUARD_DIR_ENV = "MERIDIAN_GUARD_DIR"
SHIM_DIR_ENV = "MERIDIAN_GUARD_SHIM_DIR"

MODES = ("enforce", "advisory")
SCOPES = ("project", "user")
SHELLS = ("powershell", "bash")

MARKER_SCHEMA = "meridian.guard.install/v1"
CONFIG_SCHEMA = "meridian.guard.config/v1"

TIMEOUTS: dict[str, int] = {
    "PreToolUse": 3,
    "PostToolUse": 3,
    "SessionStart": 10,
    "SubagentStart": 10,
}

# Rule-family matcher fragments (design final_design.rules). Kept as separate
# tuples so the structural tests can assert each family is covered.
_CBM_READ_TOOLS = (
    "mcp__codebase-memory(-mcp)?__"
    "(search_graph|search_code|trace_path|get_code_snippet|query_graph|get_architecture)"
)
_SHELL_TOOLS = ("Bash", "PowerShell", "Monitor")
_DC_PROCESS_TOOLS = ("mcp__dc__start_process", "mcp__dc__interact_with_process")
_FILE_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_DC_WRITE_TOOLS = ("mcp__dc__write_file", "mcp__dc__edit_block", "mcp__dc__move_file")
_PATCH_TOOLS = ("mcp__.*__patch_file",)
_SERENA_MEMORY_WRITES = ("mcp__.*__(write_memory|edit_memory|rename_memory)",)


def _join(*parts: Iterable[str]) -> str:
    seen: list[str] = []
    for group in parts:
        for item in group:
            if item not in seen:
                seen.append(item)
    return "|".join(seen)


# G1-G11. ``mcp__dc__.*`` (G9) subsumes the individual dc tools of G3/G4/G6.
PRE_MATCHER_PROJECT = _join(
    ("Grep", "Glob"),
    _SHELL_TOOLS,
    _FILE_WRITE_TOOLS,
    ("WebSearch", "WebFetch"),
    ("mcp__dc__.*",),
    (_CBM_READ_TOOLS,),
    _PATCH_TOOLS,
    _SERENA_MEMORY_WRITES,
)

# G6-G8 only (auto-memory write via file tools, via shell, Serena memory).
PRE_MATCHER_USER = _join(
    _SHELL_TOOLS,
    _FILE_WRITE_TOOLS,
    _DC_PROCESS_TOOLS,
    _DC_WRITE_TOOLS,
    _PATCH_TOOLS,
    _SERENA_MEMORY_WRITES,
)

# G12 (web capture reminder), G13 (receipts), G14 (directive quarantine).
POST_MATCHER = _join(
    ("WebSearch", "WebFetch"),
    ("mcp__codebase-memory(-mcp)?__.*", "mcp__serena__find.*"),
    (
        "mcp__.*__(paper_search|github_search|start_session|capture_research_finding"
        "|add_note|search_code|prospect_symbol|load_handoff|get_sprint_items"
        "|get_session_brief|refresh_context|get_agent_instructions|claim_sprint_item)",
    ),
)

SESSION_START_MATCHER = "startup|resume|clear|compact"
SUBAGENT_START_MATCHER = "*"

# (event, matcher, shim) per scope, in registration order.
PROJECT_ENTRIES: tuple[tuple[str, str, str], ...] = (
    ("PreToolUse", PRE_MATCHER_PROJECT, SHIM_PRE),
    ("PostToolUse", POST_MATCHER, SHIM_POST),
    ("SessionStart", SESSION_START_MATCHER, SHIM_BRIEF),
    ("SubagentStart", SUBAGENT_START_MATCHER, SHIM_BRIEF),
)
USER_ENTRIES: tuple[tuple[str, str, str], ...] = (
    ("PreToolUse", PRE_MATCHER_USER, SHIM_PRE),
    ("SessionStart", SESSION_START_MATCHER, SHIM_BRIEF),
    ("SubagentStart", SUBAGENT_START_MATCHER, SHIM_BRIEF),
)

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TOML_SECTION_RE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*(?:#.*)?$")
_TOML_PROJECT_ID_RE = re.compile(r"""^\s*project_id\s*=\s*["']([^"']+)["']""")
_CLAUDE_LOCAL_PROJECT_ID_RE = re.compile(
    r"project[ _-]?id\s*[:=]\s*`?(" + _UUID_RE.pattern + r")", re.IGNORECASE
)

# Generated user-scope dedupe check. PURE ASCII: PowerShell 5.1 reads a
# BOM-less UTF-8 file as cp1252. Returns True only for a POSITIVE match of a
# project-scope guard; any error returns False so the guard still runs.
DEFER_PS1 = r"""# meridian_guard_defer.ps1 -- user-scope dedupe check for the Meridian guard.
# Generated by: python -m meridian hooks install-guard --scope user
# Re-run the installer instead of editing this file. Keep it pure ASCII.
#
# Outputs True when the active project registers its OWN project-scope
# meridian_guard hook (.claude/settings.json or .claude/settings.local.json);
# the user-scope hook command that called it then exits 0 with no output, so
# guard rules never fire twice. Outputs False otherwise and on ANY error, so a
# broken check fails toward running the guard. Never reads stdin: the hook
# payload is left untouched for the guard shim.
$ErrorActionPreference = 'Stop'
try {
    $proj = [string]$env:CLAUDE_PROJECT_DIR
    if ([string]::IsNullOrWhiteSpace($proj)) { return $false }
    foreach ($name in @('settings.json', 'settings.local.json')) {
        $path = [System.IO.Path]::Combine($proj, '.claude', $name)
        if (-not [System.IO.File]::Exists($path)) { continue }
        $obj = [System.IO.File]::ReadAllText($path) | ConvertFrom-Json
        if ($null -eq $obj -or $null -eq $obj.hooks) { continue }
        foreach ($ev in $obj.hooks.PSObject.Properties) {
            foreach ($grp in @($ev.Value)) {
                if ($null -eq $grp -or $null -eq $grp.hooks) { continue }
                foreach ($h in @($grp.hooks)) {
                    $cmd = [string]$h.command
                    if ($cmd.Contains('meridian_guard') -and -not $cmd.Contains('meridian_guard_defer')) {
                        return $true
                    }
                }
            }
        }
    }
    return $false
} catch {
    return $false
}
"""

DEFER_SH = r"""#!/usr/bin/env bash
# meridian_guard_defer.sh -- user-scope dedupe check for the Meridian guard.
# Generated by: python -m meridian hooks install-guard --scope user
# Re-run the installer instead of editing this file.
#
# Exit 0 when the active project registers its OWN project-scope
# meridian_guard hook (.claude/settings.json or .claude/settings.local.json);
# the user-scope hook command that called it then exits 0 with no output, so
# guard rules never fire twice. Exit 1 otherwise and on any error, so a broken
# check fails toward running the guard. Never reads stdin.
proj="${CLAUDE_PROJECT_DIR:-}"
[ -n "$proj" ] || exit 1
for name in settings.json settings.local.json; do
  p="$proj/.claude/$name"
  [ -f "$p" ] || continue
  if grep -F 'meridian_guard' "$p" 2>/dev/null | grep -vF 'meridian_guard_defer' | grep -qF '"command"'; then
    exit 0
  fi
done
exit 1
"""


class GuardInstallError(Exception):
    """A condition that aborts install/uninstall before anything is written."""


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def guard_dir(env: Mapping[str, str] | None = None) -> Path:
    """Per-machine guard state dir: ``%LOCALAPPDATA%/meridian/guard`` on
    Windows (design), ``$XDG_STATE_HOME/meridian/guard`` or
    ``~/.local/state/meridian/guard`` elsewhere. ``MERIDIAN_GUARD_DIR``
    overrides (tests, relocation)."""
    env = os.environ if env is None else env
    override = (env.get(GUARD_DIR_ENV) or "").strip()
    if override:
        return Path(override)
    local = (env.get("LOCALAPPDATA") or "").strip()
    if local:
        return Path(local) / "meridian" / "guard"
    xdg = (env.get("XDG_STATE_HOME") or "").strip()
    if xdg:
        return Path(xdg) / "meridian" / "guard"
    return Path.home() / ".local" / "state" / "meridian" / "guard"


def default_shell() -> str:
    return "powershell" if sys.platform == "win32" else "bash"


def _shim_ext(shell: str) -> str:
    return "ps1" if shell == "powershell" else "sh"


def user_settings_path(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def project_settings_paths(repo: Path) -> tuple[Path, Path]:
    return repo / ".claude" / "settings.json", repo / ".claude" / "settings.local.json"


# ---------------------------------------------------------------------------
# Project id (read-only; meridian.toml is read for [project].project_id ONLY)
# ---------------------------------------------------------------------------


def _toml_project_id(toml_path: Path) -> str | None:
    """Line-scan ``[project] project_id`` without parsing (or retaining) any
    other part of the file -- meridian.toml also holds live credentials."""
    try:
        with toml_path.open("r", encoding="utf-8", errors="replace") as fh:
            section = None
            for line in fh:
                m = _TOML_SECTION_RE.match(line)
                if m:
                    section = m.group(1).strip()
                    continue
                if section == "project":
                    pm = _TOML_PROJECT_ID_RE.match(line)
                    if pm:
                        value = pm.group(1).strip()
                        return value or None
    except OSError:
        return None
    return None


def _claude_local_project_id(md_path: Path) -> str | None:
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _CLAUDE_LOCAL_PROJECT_ID_RE.search(text)
    return m.group(1).lower() if m else None


def resolve_repo_project_id(
    repo: Path, env: Mapping[str, str] | None = None
) -> tuple[str | None, str]:
    """Return ``(project_id, source)``. Order (design session_start_brief #6):
    ``MERIDIAN_PROJECT_ID`` env (only when ``env`` is given), then
    ``meridian.toml`` ``[project] project_id``, then ``CLAUDE.local.md``.
    ``source`` is ``"env"``, ``"meridian.toml"``, ``"CLAUDE.local.md"`` or
    ``"unresolved"``."""
    if env is not None:
        value = (env.get("MERIDIAN_PROJECT_ID") or "").strip()
        if value:
            return value, "env"
    toml_id = _toml_project_id(repo / "meridian.toml")
    if toml_id:
        return toml_id, "meridian.toml"
    md_id = _claude_local_project_id(repo / "CLAUDE.local.md")
    if md_id:
        return md_id, "CLAUDE.local.md"
    return None, "unresolved"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _ps_quote(path: str) -> str:
    """Double-quoted PowerShell string (keeps hook_paths' ``"...ps1"`` token
    extraction working) with the characters PowerShell expands escaped."""
    escaped = path.replace("`", "``").replace("$", "`$").replace('"', '`"')
    return f'"{escaped}"'


def _sh_quote(path: str) -> str:
    escaped = (
        path.replace("\\", "/").replace("`", "\\`").replace("$", "\\$").replace('"', '\\"')
    )
    return f'"{escaped}"'


def build_command(
    shim: str,
    *,
    scope: str,
    mode: str,
    shell: str,
    user_hooks_dir: Path | None = None,
) -> str:
    """Render the ``command`` string for one hook entry.

    Project scope reuses the repo's established ``$CLAUDE_PROJECT_DIR`` form
    (test_e5eec33b forbids personal absolute paths in tracked settings).
    User scope uses absolute paths into ``~/.claude/hooks`` (per-machine
    settings, same as the global cbm hooks) and runs the dedupe check first.
    """
    if scope not in SCOPES:
        raise GuardInstallError(f"unknown scope {scope!r}")
    if mode not in MODES:
        raise GuardInstallError(f"unknown mode {mode!r}")
    if shell not in SHELLS:
        raise GuardInstallError(f"unknown shell {shell!r}")
    ext = _shim_ext(shell)
    if shell == "powershell":
        mode_prefix = f"$env:{DEFAULT_MODE_ENV}='advisory'; " if mode == "advisory" else ""
        if scope == "project":
            return f'{mode_prefix}& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\{shim}.{ext}"'
        assert user_hooks_dir is not None
        defer = _ps_quote(str(user_hooks_dir / f"{DEFER_BASENAME}.{ext}"))
        target = _ps_quote(str(user_hooks_dir / f"{shim}.{ext}"))
        return (
            f"if (& {defer}) {{ exit 0 }}; $env:{SCOPE_ENV}='user'; "
            f"{mode_prefix}& {target}"
        )
    mode_prefix = f"{DEFAULT_MODE_ENV}=advisory " if mode == "advisory" else ""
    if scope == "project":
        return f'{mode_prefix}bash "$CLAUDE_PROJECT_DIR/.claude/hooks/{shim}.{ext}"'
    assert user_hooks_dir is not None
    defer = _sh_quote(str(user_hooks_dir / f"{DEFER_BASENAME}.{ext}"))
    target = _sh_quote(str(user_hooks_dir / f"{shim}.{ext}"))
    return f"bash {defer} && exit 0; {SCOPE_ENV}=user {mode_prefix}bash {target}"


def desired_entries(
    *, scope: str, mode: str, shell: str, user_hooks_dir: Path | None = None
) -> list[tuple[str, str, dict[str, Any]]]:
    """``(event, matcher, hook)`` triples this installer owns for a scope."""
    spec = PROJECT_ENTRIES if scope == "project" else USER_ENTRIES
    out: list[tuple[str, str, dict[str, Any]]] = []
    for event, matcher, shim in spec:
        hook: dict[str, Any] = {"type": "command"}
        if shell == "powershell":
            hook["shell"] = "powershell"
        hook["command"] = build_command(
            shim, scope=scope, mode=mode, shell=shell, user_hooks_dir=user_hooks_dir
        )
        hook["timeout"] = TIMEOUTS[event]
        out.append((event, matcher, hook))
    return out


def required_shims(scope: str) -> tuple[str, ...]:
    spec = PROJECT_ENTRIES if scope == "project" else USER_ENTRIES
    names: list[str] = []
    for _event, _matcher, shim in spec:
        if shim not in names:
            names.append(shim)
    return tuple(names)


# ---------------------------------------------------------------------------
# settings.json load / merge / render
# ---------------------------------------------------------------------------


@dataclass
class SettingsDoc:
    """A parsed settings file plus the formatting needed to re-render it."""

    path: Path
    data: dict[str, Any]
    original_text: str
    existed: bool
    indent: int | str = 2
    newline: str = "\n"
    trailing_newline: bool = True
    bom: bool = False

    def render(self, data: dict[str, Any] | None = None) -> str:
        obj = self.data if data is None else data
        text = json.dumps(obj, indent=self.indent, ensure_ascii=False)
        if self.trailing_newline:
            text += "\n"
        if self.newline != "\n":
            text = text.replace("\n", self.newline)
        return text


def _detect_indent(text: str) -> int | str:
    m = re.search(r"\n([ \t]+)\S", text)
    if not m:
        return 2
    ws = m.group(1)
    if "\t" in ws:
        return "\t"
    return len(ws)


def load_settings(path: Path) -> SettingsDoc:
    """Parse ``path``; a missing or whitespace-only file is an empty object.
    Anything that is not a JSON object with a well-formed ``hooks`` block
    raises :class:`GuardInstallError` -- the caller aborts, writing nothing."""
    if not path.exists():
        return SettingsDoc(path=path, data={}, original_text="", existed=False)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GuardInstallError(f"cannot read {path}: {exc}") from exc
    bom = raw.startswith(codecs.BOM_UTF8)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GuardInstallError(f"{path} is not valid UTF-8: {exc}") from exc
    if not text.strip():
        data: Any = {}
    else:
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise GuardInstallError(
                f"{path} is not valid JSON ({exc}); refusing to modify it"
            ) from exc
    if not isinstance(data, dict):
        raise GuardInstallError(f"{path} must contain a JSON object at the top level")
    _validate_hooks_block(path, data)
    newline = "\r\n" if "\r\n" in text else "\n"
    return SettingsDoc(
        path=path,
        data=data,
        original_text=text,
        existed=True,
        indent=_detect_indent(text) if text.strip() else 2,
        newline=newline,
        trailing_newline=text.endswith("\n") if text.strip() else True,
        bom=bom,
    )


def _validate_hooks_block(path: Path, data: dict[str, Any]) -> None:
    hooks = data.get("hooks")
    if hooks is None:
        return
    if not isinstance(hooks, dict):
        raise GuardInstallError(f"{path}: 'hooks' must be an object")
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise GuardInstallError(f"{path}: hooks.{event} must be an array")
        for i, group in enumerate(groups):
            if not isinstance(group, dict):
                raise GuardInstallError(f"{path}: hooks.{event}[{i}] must be an object")
            inner = group.get("hooks")
            if inner is not None and not isinstance(inner, list):
                raise GuardInstallError(f"{path}: hooks.{event}[{i}].hooks must be an array")


def is_owned_hook(hook: Any) -> bool:
    return isinstance(hook, dict) and OWNER_MARKER in str(hook.get("command") or "")


def is_project_scope_guard_command(command: str) -> bool:
    """True for a project-scope guard command (the dedupe check's notion of
    "the project has its own guard"): owned, and not a user-scope command."""
    return OWNER_MARKER in command and DEFER_BASENAME not in command


def owned_entries(data: Mapping[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event, groups in hooks.items():
        for group in groups or []:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks") or []:
                if is_owned_hook(hook):
                    out.append((event, str(group.get("matcher", "")), hook))
    return out


def has_project_scope_guard(repo: Path) -> bool:
    """True when ``repo`` registers a project-scope guard in either project
    settings file (Python twin of the generated defer check)."""
    for path in project_settings_paths(repo):
        try:
            doc = load_settings(path)
        except GuardInstallError:
            continue
        for _event, _matcher, hook in owned_entries(doc.data):
            if is_project_scope_guard_command(str(hook.get("command") or "")):
                return True
    return False


def _canonical(entries: Iterable[tuple[str, str, dict[str, Any]]]) -> list[str]:
    return sorted(json.dumps([e, m, h], sort_keys=True) for e, m, h in entries)


def remove_owned(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Return a copy of ``data`` with every owned hook removed. A group or an
    event list that becomes empty *because of the removal* is dropped; so is
    a ``hooks`` object emptied by it. Everything else keeps its position."""
    new = json.loads(json.dumps(data))
    hooks = new.get("hooks")
    if not isinstance(hooks, dict):
        return new, 0
    removed = 0
    for event in list(hooks.keys()):
        groups = hooks[event]
        kept_groups: list[Any] = []
        removed_here = 0
        for group in groups:
            inner = group.get("hooks") if isinstance(group, dict) else None
            if isinstance(inner, list):
                kept = [h for h in inner if not is_owned_hook(h)]
                n = len(inner) - len(kept)
                if n:
                    removed_here += n
                    if not kept:
                        continue
                    group["hooks"] = kept
            kept_groups.append(group)
        if removed_here:
            removed += removed_here
            if kept_groups:
                hooks[event] = kept_groups
            else:
                del hooks[event]
    if removed and not hooks:
        del new["hooks"]
    return new, removed


def merge_owned(
    data: dict[str, Any], desired: list[tuple[str, str, dict[str, Any]]]
) -> tuple[dict[str, Any], bool]:
    """Idempotent merge. Returns ``(new_data, changed)``. When the owned
    entries already equal ``desired`` the input is returned untouched, even if
    the user moved them; otherwise owned entries are removed and ``desired``
    is appended (one group per entry) at the end of each event's list."""
    if _canonical(owned_entries(data)) == _canonical(desired):
        return data, False
    new, _ = remove_owned(data)
    hooks = new.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        new["hooks"] = hooks
    for event, matcher, hook in desired:
        hooks.setdefault(event, []).append({"matcher": matcher, "hooks": [dict(hook)]})
    return new, new != data


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class SettingsChange:
    path: Path
    doc: SettingsDoc
    new_data: dict[str, Any]
    reason: str

    @property
    def new_text(self) -> str:
        return self.doc.render(self.new_data)

    def diff(self) -> str:
        before = self.doc.original_text if self.doc.existed else ""
        return "".join(
            difflib.unified_diff(
                before.replace("\r\n", "\n").splitlines(keepends=True),
                self.new_text.replace("\r\n", "\n").splitlines(keepends=True),
                fromfile=f"{self.path} (current)" if self.doc.existed else "/dev/null",
                tofile=f"{self.path} (after)",
            )
        )


@dataclass
class FileOp:
    """``kind`` is ``copy`` (``source`` -> ``path``), ``write`` (``content``
    -> ``path``) or ``delete`` (``path``)."""

    kind: str
    path: Path
    source: Path | None = None
    content: str | None = None
    note: str = ""

    def describe(self) -> str:
        if self.kind == "copy":
            return f"copy   {self.source} -> {self.path}"
        if self.kind == "write":
            return f"write  {self.path}" + (f"  ({self.note})" if self.note else "")
        return f"delete {self.path}" + (f"  ({self.note})" if self.note else "")


@dataclass
class GuardPlan:
    action: str  # "install" | "uninstall"
    scope: str
    mode: str
    shell: str
    repo: Path
    guard_dir: Path
    settings_changes: list[SettingsChange] = field(default_factory=list)
    file_ops: list[FileOp] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.settings_changes or self.file_ops)

    def render(self, *, include_diff: bool = True) -> str:
        lines = [
            f"meridian guard {self.action}: scope={self.scope} mode={self.mode} "
            f"shell={self.shell} repo={self.repo}"
        ]
        lines.extend(f"  - {m}" for m in self.messages)
        if not self.changed:
            lines.append("  no changes (already in the requested state)")
            return "\n".join(lines) + "\n"
        for ch in self.settings_changes:
            lines.append(f"  settings: {ch.path} ({ch.reason})")
        for op in self.file_ops:
            lines.append(f"  file: {op.describe()}")
        out = "\n".join(lines) + "\n"
        if include_diff:
            for ch in self.settings_changes:
                out += ch.diff()
        return out


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def find_shim_source(
    names: Iterable[str],
    shell: str,
    *,
    explicit: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[Path | None, list[Path]]:
    """Locate the directory holding the guard shims. Candidates, in order:
    ``explicit``; ``$MERIDIAN_GUARD_SHIM_DIR``; ``meridian/guard_shims``
    (packaged); ``<checkout>/.claude/hooks`` (a source checkout of Meridian).
    Returns ``(dir_or_None, candidates_checked)``."""
    env = os.environ if env is None else env
    ext = _shim_ext(shell)
    pkg = Path(__file__).resolve().parent
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    env_dir = (env.get(SHIM_DIR_ENV) or "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(pkg / "guard_shims")
    candidates.append(pkg.parent / ".claude" / "hooks")
    wanted = list(names)
    for cand in candidates:
        if all((cand / f"{n}.{ext}").is_file() for n in wanted):
            return cand, candidates
    return None, candidates


def _shim_files_in(source: Path) -> list[Path]:
    """Every ``meridian_guard*.{ps1,sh}`` in ``source`` (shims plus any helper
    library a shim sources), excluding the installer-generated defer check."""
    out: list[Path] = []
    for p in sorted(source.iterdir()):
        if not p.is_file() or not p.name.startswith(OWNER_MARKER):
            continue
        if p.suffix not in (".ps1", ".sh"):
            continue
        if p.stem == DEFER_BASENAME:
            continue
        out.append(p)
    return out


def _check_ascii_ps1(path: Path) -> None:
    if path.suffix != ".ps1":
        return
    data = path.read_bytes()
    try:
        data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise GuardInstallError(
            f"{path} is not pure ASCII (PowerShell 5.1 reads BOM-less UTF-8 as "
            f"cp1252); refusing to install it"
        ) from exc


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _now_iso(now: _dt.datetime | None = None) -> str:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return now.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _install_key(scope: str, repo: Path) -> str:
    return "user" if scope == "user" else "project:" + normalize_wsl_path(str(repo))


def _config_op(
    gdir: Path,
    key: str,
    record: dict[str, Any] | None,
) -> FileOp | None:
    """Plan the update of ``<guard dir>/config.json``: set (or with
    ``record=None`` remove) ``installs[key]``. ``None`` when unchanged."""
    path = gdir / CONFIG_NAME
    current = _read_json_file(path)
    base: dict[str, Any] = dict(current) if current else {}
    base.setdefault("schema", CONFIG_SCHEMA)
    installs = dict(base.get("installs") or {})
    if record is None:
        if key not in installs:
            return None
        installs.pop(key)
    else:
        url = (os.environ.get("MERIDIAN_URL") or "http://localhost:7878").strip()
        previous = installs.get(key)
        comparable_prev = dict(previous or {})
        comparable_prev.pop("installed_at", None)
        comparable_new = dict(record)
        comparable_new.pop("installed_at", None)
        if (
            previous is not None
            and current is not None
            and comparable_prev == comparable_new
            and (current.get("runtime") or {}).get("python") == sys.executable
            and current.get("meridian_url") == url
        ):
            return None
        installs[key] = record
        base["runtime"] = {"python": sys.executable}
        base["meridian_url"] = url
    base["installs"] = installs
    content = json.dumps(base, indent=2, sort_keys=False) + "\n"
    return FileOp(kind="write", path=path, content=content, note="per-machine guard config")


def plan_install(
    repo: Path | str,
    *,
    scope: str = "project",
    mode: str = "enforce",
    shell: str | None = None,
    home: Path | None = None,
    gdir: Path | None = None,
    shim_source: Path | None = None,
    env: Mapping[str, str] | None = None,
    now: _dt.datetime | None = None,
) -> GuardPlan:
    """Compute (but do not perform) an install. Raises GuardInstallError on
    malformed settings, a missing repo, or missing/non-ASCII shims."""
    env = os.environ if env is None else env
    shell = shell or default_shell()
    if scope not in SCOPES:
        raise GuardInstallError(f"--scope must be one of {SCOPES}")
    if mode not in MODES:
        raise GuardInstallError(f"--mode must be one of {MODES}")
    if shell not in SHELLS:
        raise GuardInstallError(f"--shell must be one of {SHELLS}")
    repo_path = Path(normalize_wsl_path(str(repo)) or ".").resolve()
    if not repo_path.is_dir():
        raise GuardInstallError(f"--repo {repo} is not a directory")
    home = Path(home) if home is not None else Path.home()
    gdir = Path(gdir) if gdir is not None else guard_dir(env)
    plan = GuardPlan(
        action="install", scope=scope, mode=mode, shell=shell, repo=repo_path, guard_dir=gdir
    )
    ext = _shim_ext(shell)
    names = required_shims(scope)
    copied: dict[str, str] = {}

    if scope == "project":
        hooks_dir = repo_path / ".claude" / "hooks"
        source, checked = find_shim_source(names, shell, explicit=shim_source, env=env)
        target_has = all((hooks_dir / f"{n}.{ext}").is_file() for n in names)
        if source is None and not target_has:
            looked = list(dict.fromkeys(str(c) for c in [*checked, hooks_dir]))
            raise GuardInstallError(
                "guard shim scripts not found (need "
                + ", ".join(f"{n}.{ext}" for n in names)
                + "); looked in: "
                + ", ".join(looked)
                + f". Build the shims (item 55d48d69) or set {SHIM_DIR_ENV}."
            )
        if source is not None and not _same_dir(source, hooks_dir):
            for src in _shim_files_in(source):
                _check_ascii_ps1(src)
                dst = hooks_dir / src.name
                src_hash = _sha256_file(src)
                copied[src.name] = src_hash or ""
                if _sha256_file(dst) != src_hash:
                    plan.file_ops.append(FileOp(kind="copy", path=dst, source=src))
        elif source is not None:
            plan.messages.append(
                f"shims are source files in {hooks_dir} (dogfood); nothing copied"
            )
            for src in _shim_files_in(hooks_dir):
                _check_ascii_ps1(src)
        else:
            plan.messages.append(f"using shims already present in {hooks_dir}")

        settings_path, local_path = project_settings_paths(repo_path)
        desired = desired_entries(scope="project", mode=mode, shell=shell)
        doc = load_settings(settings_path)
        new_data, changed = merge_owned(doc.data, desired)
        if changed:
            plan.settings_changes.append(
                SettingsChange(settings_path, doc, new_data, "register meridian_guard entries")
            )
        local_doc = load_settings(local_path)
        local_new, removed = remove_owned(local_doc.data)
        if removed:
            plan.settings_changes.append(
                SettingsChange(
                    local_path,
                    local_doc,
                    local_new,
                    f"remove {removed} duplicate meridian_guard entr"
                    + ("y" if removed == 1 else "ies")
                    + " (registered once, in settings.json)",
                )
            )

        if copied:
            marker = {
                "schema": MARKER_SCHEMA,
                "installed_by": "python -m meridian hooks install-guard",
                "scope": "project",
                "mode": mode,
                "shell": shell,
                "copied_files": copied,
            }
            marker_path = hooks_dir / INSTALL_MARKER_NAME
            existing = _read_json_file(marker_path)
            if existing is None or {k: v for k, v in existing.items() if k != "installed_at"} != marker:
                marker["installed_at"] = _now_iso(now)
                plan.file_ops.append(
                    FileOp(
                        kind="write",
                        path=marker_path,
                        content=json.dumps(marker, indent=2) + "\n",
                        note="install marker: generate_handoff refreshes shims only where it exists",
                    )
                )
        # Repo files only: MERIDIAN_PROJECT_ID describes the *current* session's
        # project, which need not be the --repo being installed into.
        project_id, pid_source = resolve_repo_project_id(repo_path)
        record = {
            "scope": "project",
            "repo": normalize_wsl_path(str(repo_path)),
            "mode": mode,
            "shell": shell,
            "project_id": project_id,
            "project_id_source": pid_source,
            "copied_files": copied,
            "installed_at": _now_iso(now),
        }
        if project_id is None:
            plan.messages.append(
                "project_id unresolved (set MERIDIAN_PROJECT_ID, meridian.toml [project] "
                "project_id, or CLAUDE.local.md); the brief will omit it"
            )
    else:
        user_hooks = home / ".claude" / "hooks"
        source, checked = find_shim_source(names, shell, explicit=shim_source, env=env)
        if source is None:
            project_hooks = repo_path / ".claude" / "hooks"
            if all((project_hooks / f"{n}.{ext}").is_file() for n in names):
                source = project_hooks
        if source is None:
            looked = list(dict.fromkeys(str(c) for c in [*checked, repo_path / ".claude" / "hooks"]))
            raise GuardInstallError(
                "guard shim scripts not found for --scope user (need "
                + ", ".join(f"{n}.{ext}" for n in names)
                + "); looked in: "
                + ", ".join(looked)
            )
        for src in _shim_files_in(source):
            _check_ascii_ps1(src)
            dst = user_hooks / src.name
            src_hash = _sha256_file(src)
            copied[src.name] = src_hash or ""
            if _sha256_file(dst) != src_hash:
                plan.file_ops.append(FileOp(kind="copy", path=dst, source=src))
        for defer_name, body in ((f"{DEFER_BASENAME}.ps1", DEFER_PS1), (f"{DEFER_BASENAME}.sh", DEFER_SH)):
            dst = user_hooks / defer_name
            digest = hashlib.sha256(body.encode("ascii")).hexdigest()
            copied[defer_name] = digest
            if _sha256_file(dst) != digest:
                plan.file_ops.append(
                    FileOp(kind="write", path=dst, content=body, note="user-scope dedupe check")
                )
        settings_path = user_settings_path(home)
        desired = desired_entries(scope="user", mode=mode, shell=shell, user_hooks_dir=user_hooks)
        doc = load_settings(settings_path)
        new_data, changed = merge_owned(doc.data, desired)
        if changed:
            plan.settings_changes.append(
                SettingsChange(
                    settings_path, doc, new_data, "register user-scope meridian_guard entries (G6-G8, G15/G16)"
                )
            )
        if has_project_scope_guard(repo_path):
            plan.messages.append(
                f"{repo_path} registers its own project-scope guard: the user-scope "
                "entries defer to it there (dedupe), so nothing fires twice"
            )
        record = {
            "scope": "user",
            "mode": mode,
            "shell": shell,
            "copied_files": copied,
            "installed_at": _now_iso(now),
        }

    op = _config_op(gdir, _install_key(scope, repo_path), record)
    if op is not None:
        plan.file_ops.append(op)
    return plan


def plan_uninstall(
    repo: Path | str,
    *,
    scope: str = "project",
    home: Path | None = None,
    gdir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> GuardPlan:
    """Compute a real uninstall: owned settings entries, the install marker,
    and copied shim files whose content still matches what was copied."""
    env = os.environ if env is None else env
    if scope not in SCOPES:
        raise GuardInstallError(f"--scope must be one of {SCOPES}")
    repo_path = Path(normalize_wsl_path(str(repo)) or ".").resolve()
    if scope == "project" and not repo_path.is_dir():
        raise GuardInstallError(f"--repo {repo} is not a directory")
    home = Path(home) if home is not None else Path.home()
    gdir = Path(gdir) if gdir is not None else guard_dir(env)
    plan = GuardPlan(
        action="uninstall", scope=scope, mode="-", shell="-", repo=repo_path, guard_dir=gdir
    )
    config = _read_json_file(gdir / CONFIG_NAME) or {}
    record = (config.get("installs") or {}).get(_install_key(scope, repo_path)) or {}

    if scope == "project":
        paths = project_settings_paths(repo_path)
        hooks_dir = repo_path / ".claude" / "hooks"
        marker_path = hooks_dir / INSTALL_MARKER_NAME
        marker = _read_json_file(marker_path) or {}
        copied = dict(record.get("copied_files") or {})
        copied.update(marker.get("copied_files") or {})
        if marker_path.exists():
            plan.file_ops.append(FileOp(kind="delete", path=marker_path, note="install marker"))
    else:
        paths = (user_settings_path(home),)
        hooks_dir = home / ".claude" / "hooks"
        copied = dict(record.get("copied_files") or {})
        for defer_name in (f"{DEFER_BASENAME}.ps1", f"{DEFER_BASENAME}.sh"):
            copied.setdefault(
                defer_name,
                hashlib.sha256(
                    (DEFER_PS1 if defer_name.endswith(".ps1") else DEFER_SH).encode("ascii")
                ).hexdigest(),
            )

    for path in paths:
        doc = load_settings(path)
        new_data, removed = remove_owned(doc.data)
        if removed:
            plan.settings_changes.append(
                SettingsChange(path, doc, new_data, f"remove {removed} meridian_guard entries")
            )

    for name, digest in sorted(copied.items()):
        if not name.startswith(OWNER_MARKER):
            continue  # never delete anything this installer does not own
        target = hooks_dir / name
        if not target.is_file():
            continue
        current = _sha256_file(target)
        if digest and current == digest:
            plan.file_ops.append(FileOp(kind="delete", path=target, note="copied by install-guard"))
        else:
            plan.messages.append(
                f"kept {target}: modified since install-guard copied it (delete it by hand if unwanted)"
            )

    op = _config_op(gdir, _install_key(scope, repo_path), None)
    if op is not None:
        plan.file_ops.append(op)
    return plan


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _backup_name(path: Path, now: _dt.datetime) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", normalize_wsl_path(str(path))).strip("-")[-120:]
    return f"{slug}.{now.strftime('%Y%m%dT%H%M%S%fZ')}.bak"


def apply_plan(plan: GuardPlan, *, now: _dt.datetime | None = None) -> list[str]:
    """Perform ``plan``. Every settings file that changes is backed up before
    anything at all is written. Install writes the shim files BEFORE
    registering them (a registration never points at a missing script);
    uninstall unregisters BEFORE deleting them. Returns a log."""
    now = (now or _dt.datetime.now(_dt.timezone.utc)).astimezone(_dt.timezone.utc)
    log: list[str] = []
    backup_dir = plan.guard_dir / "backups"
    for ch in plan.settings_changes:
        if ch.doc.existed:
            backup_dir.mkdir(parents=True, exist_ok=True)
            dest = backup_dir / _backup_name(ch.path, now)
            n = 1
            while dest.exists():
                dest = backup_dir / (_backup_name(ch.path, now) + f".{n}")
                n += 1
            dest.write_bytes(ch.path.read_bytes())
            log.append(f"backup {ch.path} -> {dest}")

    def _settings() -> None:
        for ch in plan.settings_changes:
            data = (codecs.BOM_UTF8 if ch.doc.bom else b"") + ch.new_text.encode("utf-8")
            _atomic_write_bytes(ch.path, data)
            log.append(f"wrote {ch.path} ({ch.reason})")

    def _files() -> None:
        for op in plan.file_ops:
            if op.kind == "copy":
                assert op.source is not None
                _atomic_write_bytes(op.path, op.source.read_bytes())
                log.append(f"copied {op.source} -> {op.path}")
            elif op.kind == "write":
                assert op.content is not None
                _atomic_write_bytes(op.path, op.content.encode("utf-8"))
                log.append(f"wrote {op.path}")
            elif op.kind == "delete":
                try:
                    op.path.unlink()
                    log.append(f"deleted {op.path}")
                except FileNotFoundError:
                    pass

    if plan.action == "install":
        _files()
        _settings()
    else:
        _settings()
        _files()
    return log


# ---------------------------------------------------------------------------
# CLI: python -m meridian hooks install-guard ...
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meridian hooks", description="Meridian Claude Code hook tools.")
    sub = parser.add_subparsers(dest="command", required=True)
    ig = sub.add_parser(
        "install-guard",
        help="Register (or --uninstall) the Meridian guard hooks in a Claude Code settings.json.",
    )
    ig.add_argument("--repo", required=True, help="Target repository root.")
    ig.add_argument("--scope", choices=SCOPES, default="project",
                    help="project: <repo>/.claude/settings.json (all rules). "
                         "user: ~/.claude/settings.json (G6-G8 + brief only).")
    ig.add_argument("--mode", choices=MODES, default="enforce",
                    help="Installed default mode (MERIDIAN_GUARD env / sentinel files still win).")
    ig.add_argument("--shell", choices=SHELLS, default=None,
                    help="Hook shell (default: powershell on Windows, bash elsewhere).")
    ig.add_argument("--dry-run", action="store_true", help="Print the diff and planned file operations; write nothing.")
    ig.add_argument("--uninstall", action="store_true", help="Remove everything install-guard added.")
    ig.add_argument("--shim-dir", default=None, help=f"Directory holding the guard shims (else ${SHIM_DIR_ENV} / packaged / checkout).")
    return parser


def cli_main(argv: list[str] | None = None, *, stdout=None, stderr=None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        if args.uninstall:
            plan = plan_uninstall(args.repo, scope=args.scope)
        else:
            plan = plan_install(
                args.repo,
                scope=args.scope,
                mode=args.mode,
                shell=args.shell,
                shim_source=Path(args.shim_dir) if args.shim_dir else None,
            )
    except GuardInstallError as exc:
        print(f"error: {exc}", file=stderr)
        return 1
    if args.dry_run:
        stdout.write("[dry-run] nothing written\n")
        stdout.write(plan.render(include_diff=True))
        return 0
    stdout.write(plan.render(include_diff=False))
    if not plan.changed:
        return 0
    try:
        for line in apply_plan(plan):
            stdout.write(f"  {line}\n")
    except OSError as exc:
        print(f"error: {exc}", file=stderr)
        return 1
    return 0
