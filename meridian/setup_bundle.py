"""Install Meridian's local MCP bundle into supported AI coding clients.

The generated server names use the reserved ``meridian_setup_`` prefix so
multiple repositories can coexist in global clients and uninstall can remove
only this command's entries. Credentials are never written to client config;
hosted Meridian reads ``BEARER_TOKEN`` from the MCP host process environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


DEFAULT_MERIDIAN_URL = "https://usemeridian.us"
MANAGED_PREFIX = "meridian_setup_"
HOSTS = ("claude-code", "claude-desktop", "codex", "cursor")
SERENA_CONTEXTS = {
    "claude-code": "claude-code",
    "claude-desktop": "desktop-app",
    "codex": "codex",
    "cursor": "ide",
}
TOOLS = ("meridian", "serena", "codebase_memory", "docs", "outputs", "latex")
# A server the user wrote that matches one of these needles already covers that tool, so the managed entry for the
# tool is not added beside it. Two copies of one server in one client are the duplicates this avoids.
_TOOL_SIGNATURES: dict[str, tuple[str, ...]] = {
    # The Meridian MCP endpoint itself. Other usemeridian.us paths are other tools behind the same account.
    "meridian": ("usemeridian.us/mcp", '"--mcp"'),
    # The hosted /extract/mcp and /code/mcp endpoints are Meridian's Serena and code-memory tools behind an account.
    "serena": ("oraios/serena", "/extract/mcp"),
    "codebase_memory": ("codebase-memory-mcp", "/code/mcp"),
    "docs": ("meridian-docs",),
    "outputs": ("meridian-outputs",),
    "latex": ("meridian-latex", "@meridianmcp/mcp"),
}
# A server the user named after a tool covers it too, for example "serena" or "codebase-memory" in Cursor's config.
_NAME_SIGNATURES: dict[str, tuple[str, ...]] = {
    "serena": ("serena",),
    "codebase_memory": ("codebase-memory",),
    "docs": ("meridian-docs",),
    "outputs": ("meridian-outputs",),
    "latex": ("meridian-latex",),
}
# The per-session hook: a SessionStart entry that runs setup for the repo the chat opened in. Matches the matcher the
# Meridian guard hooks use. The marker identifies this entry, so reinstalling replaces it and uninstall removes only it.
SESSION_HOOK_MATCHER = "startup|resume|clear|compact"
SESSION_HOOK_MARKER = "python -m meridian setup --repo"
# An entry is this command's if its command has either form: the inline bash command or the Windows script call.
SESSION_HOOK_MARKERS = (SESSION_HOOK_MARKER, "session-setup.ps1")
SESSION_HOOK_BACKUP_SUFFIX = ".meridian-setup.bak"


class SetupBundleError(ValueError):
    """A config is invalid or conflicts with an existing user-owned entry."""


@dataclass(frozen=True)
class PlannedWrite:
    path: Path
    content: str
    action: str


def _endpoint(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise SetupBundleError("Meridian URL must be an http(s) base URL without credentials, query, or fragment")
    return value if parsed.path.rstrip("/").endswith("/mcp") else f"{value}/mcp"


def _repo_slug(repo: Path) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", repo.name.lower()).strip("_") or "repo"
    stable_path = os.path.normcase(str(repo)).encode("utf-8", errors="surrogatepass")
    suffix = hashlib.sha256(stable_path).hexdigest()[:8]
    return f"{slug[:24]}_{suffix}"


def _server_name(tool: str, repo: Path) -> str:
    return f"{MANAGED_PREFIX}{tool}_{_repo_slug(repo)}"


def _node_command(arguments: list[str], *, windows: bool) -> dict[str, Any]:
    if windows:
        return {"command": "cmd", "args": ["/c", "npx", *arguments]}
    return {"command": "npx", "args": arguments}


def _uvx_command(arguments: list[str]) -> dict[str, Any]:
    return {"command": "uvx", "args": arguments}


def _server_entries(
    repo: Path,
    host: str,
    *,
    meridian_mode: str,
    meridian_url: str,
    windows: bool,
) -> dict[str, dict[str, Any]]:
    """Build one repo's six deterministic, credential-free MCP entries."""
    endpoint = _endpoint(meridian_url)
    root = str(repo)
    from .tunnel_plugins import extension_uvx_command

    if meridian_mode == "hosted":
        if host == "codex":
            meridian = {
                "url": endpoint,
                "bearer_token_env_var": "BEARER_TOKEN",
            }
        else:
            # mcp-remote reads BEARER_TOKEN from its inherited process env.
            meridian = _node_command(["-y", "mcp-remote", endpoint], windows=windows)
    elif meridian_mode == "self-hosted":
        meridian = {"command": "meridian", "args": ["--mcp"], "cwd": root}
    else:
        raise SetupBundleError("Meridian mode must be 'hosted' or 'self-hosted'")

    entries = {
        "meridian": meridian,
        "serena": {
            **_uvx_command(
                [
                    "--from",
                    "git+https://github.com/oraios/serena",
                    "serena",
                    "start-mcp-server",
                    "--context",
                    SERENA_CONTEXTS[host],
                    "--project",
                    root,
                ]
            ),
            "cwd": root,
        },
        "codebase_memory": {
            **_node_command(["-y", "codebase-memory-mcp"], windows=windows),
            "cwd": root,
        },
        "docs": {
            **_uvx_command(extension_uvx_command("meridian-docs")[1:]),
            "cwd": root,
        },
        "outputs": {
            **_uvx_command(extension_uvx_command("meridian-outputs")[1:]),
            "cwd": root,
        },
        "latex": {
            **_node_command(
                ["-y", "--package", "@meridianmcp/mcp", "meridian-latex", "mcp"],
                windows=windows,
            ),
            "cwd": root,
        },
    }
    return {_server_name(tool, repo): value for tool, value in entries.items()}


def _desktop_config_path(
    *, system: str | None = None, home: Path | None = None, environ: dict[str, str] | None = None
) -> Path:
    system = system or sys.platform
    home = home or Path.home()
    environ = os.environ if environ is None else environ
    if system == "win32":
        appdata = environ.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "Claude" / "claude_desktop_config.json"
    if system == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return home / ".config" / "Claude" / "claude_desktop_config.json"


def _config_operations(
    repos: list[Path],
    hosts: list[str],
    *,
    meridian_mode: str,
    meridian_url: str,
    windows: bool,
    desktop_path: Path | None = None,
) -> list[tuple[Path, str, dict[str, dict[str, Any]]]]:
    operations: list[tuple[Path, str, dict[str, dict[str, Any]]]] = []
    desktop_entries: dict[str, dict[str, Any]] = {}
    for host in hosts:
        for repo in repos:
            entries = _server_entries(
                repo,
                host,
                meridian_mode=meridian_mode,
                meridian_url=meridian_url,
                windows=windows,
            )
            if host == "claude-desktop":
                desktop_entries.update(entries)
                continue
            if host == "claude-code":
                path, kind = repo / ".mcp.json", "json"
            elif host == "codex":
                path, kind = repo / ".codex" / "config.toml", "toml"
            elif host == "cursor":
                path, kind = repo / ".cursor" / "mcp.json", "json"
            else:
                raise SetupBundleError(f"Unknown MCP host: {host}")
            operations.append((path, kind, entries))
    if desktop_entries:
        operations.append((desktop_path or _desktop_config_path(), "json", desktop_entries))
    return operations


def _read_json(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, ""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise SetupBundleError(f"Could not read {path}: {exc}") from exc
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError as exc:
        raise SetupBundleError(f"{path} is not valid JSON; it was left untouched") from exc
    if not isinstance(data, dict):
        raise SetupBundleError(f"{path} must contain a JSON object; it was left untouched")
    return data, raw


def _tool_of(name: str) -> str | None:
    """The tool a managed entry name stands for, or None for a name this command did not write."""
    if not name.startswith(MANAGED_PREFIX):
        return None
    rest = name[len(MANAGED_PREFIX):]
    return next((tool for tool in TOOLS if rest.startswith(tool + "_")), None)


def _covered_tools(servers: dict[str, Any]) -> set[str]:
    """Tools that a server this command did not write already covers. Managed entries never count."""
    covered: set[str] = set()
    for name, entry in servers.items():
        if name.startswith(MANAGED_PREFIX):
            continue
        text = json.dumps(entry, sort_keys=True, default=str)
        lowered = name.lower()
        for tool, needles in _TOOL_SIGNATURES.items():
            if any(needle in text for needle in needles) or any(needle in lowered for needle in _NAME_SIGNATURES.get(tool, ())):
                covered.add(tool)
    return covered


def _without_covered(entries: dict[str, dict[str, Any]], servers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The managed entries whose tool the existing servers do not already cover."""
    covered = _covered_tools(servers)
    return {name: entry for name, entry in entries.items() if _tool_of(name) not in covered}


def _plan_json(path: Path, entries: dict[str, dict[str, Any]], *, uninstall: bool) -> PlannedWrite | None:
    data, original = _read_json(path)
    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise SetupBundleError(f"mcpServers in {path} must be an object; it was left untouched")
    updated = dict(servers)
    if uninstall:
        for name in entries:
            updated.pop(name, None)
    else:
        entries = _without_covered(entries, servers)
        conflicts = [name for name, entry in entries.items() if name in updated and updated[name] != entry]
        if conflicts:
            joined = ", ".join(conflicts)
            raise SetupBundleError(f"{path} already has different server config for {joined}; it was left untouched")
        updated.update(entries)
    changed = updated != servers
    if not changed:
        return None
    result = dict(data)
    if updated:
        result["mcpServers"] = updated
    else:
        result.pop("mcpServers", None)
    action = "remove" if uninstall else "write"
    return PlannedWrite(path=path, content=json.dumps(result, indent=2, ensure_ascii=False) + "\n", action=action)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_table(name: str, values: dict[str, Any]) -> str:
    lines = [f"[mcp_servers.{name}]\n"]
    for key, value in values.items():
        if isinstance(value, str):
            serialized = _toml_string(value)
        elif isinstance(value, list) and all(isinstance(part, str) for part in value):
            serialized = "[" + ", ".join(_toml_string(part) for part in value) + "]"
        else:
            raise SetupBundleError(f"Unsupported generated Codex config value for {key}")
        lines.append(f"{key} = {serialized}\n")
    lines.append("\n")
    return "".join(lines)


def _toml_without_servers(raw: str, names: set[str]) -> str:
    heading = re.compile(r"(?m)^\s*\[([A-Za-z0-9_.-]+)\]\s*(?:#.*)?\r?$")
    matches = list(heading.finditer(raw))
    if not matches:
        return raw
    parts: list[str] = [raw[: matches[0].start()]]
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        section = match.group(1)
        remove = any(
            section == f"mcp_servers.{name}" or section.startswith(f"mcp_servers.{name}.")
            for name in names
        )
        if not remove:
            parts.append(raw[match.start() : end])
    return "".join(parts)


def _plan_toml(path: Path, entries: dict[str, dict[str, Any]], *, uninstall: bool) -> PlannedWrite | None:
    if path.exists():
        try:
            original = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise SetupBundleError(f"Could not read {path}: {exc}") from exc
    else:
        original = ""
    try:
        config = tomllib.loads(original)
    except tomllib.TOMLDecodeError as exc:
        raise SetupBundleError(f"{path} is not valid TOML; it was left untouched ({exc})") from exc
    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise SetupBundleError(f"mcp_servers in {path} must be a table; it was left untouched")

    if uninstall:
        names = set(entries)
        if not any(name in servers for name in names):
            return None
        result = _toml_without_servers(original, names)
    else:
        entries = _without_covered(entries, servers)
        conflicts = [name for name, entry in entries.items() if name in servers and servers[name] != entry]
        if conflicts:
            raise SetupBundleError(f"{path} already has different server config for {', '.join(conflicts)}; it was left untouched")
        missing = {name: entry for name, entry in entries.items() if name not in servers}
        if not missing:
            return None
        suffix = "" if not original or original.endswith("\n\n") else ("\n" if original.endswith("\n") else "\n\n")
        result = original + suffix + "".join(_toml_table(name, entry) for name, entry in missing.items())
    try:
        tomllib.loads(result)
    except tomllib.TOMLDecodeError as exc:
        raise SetupBundleError(f"Generated invalid Codex TOML for {path}: {exc}") from exc
    return PlannedWrite(path=path, content=result, action="remove" if uninstall else "write")


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise


def configure_bundle(
    repos: list[Path],
    hosts: list[str] | None = None,
    *,
    meridian_mode: str = "hosted",
    meridian_url: str = DEFAULT_MERIDIAN_URL,
    windows: bool | None = None,
    dry_run: bool = False,
    uninstall: bool = False,
    desktop_path: Path | None = None,
) -> list[str]:
    """Plan and apply a merged configuration; returns user-facing result lines."""
    normalized_repos = sorted({Path(repo).expanduser().resolve(strict=True) for repo in repos}, key=str)
    if not normalized_repos or any(not repo.is_dir() for repo in normalized_repos):
        raise SetupBundleError("At least one existing repository directory is required")
    selected_hosts = list(dict.fromkeys(hosts or HOSTS))
    invalid_hosts = sorted(set(selected_hosts) - set(HOSTS))
    if invalid_hosts:
        raise SetupBundleError(f"Unknown host(s): {', '.join(invalid_hosts)}")
    if windows is None:
        windows = os.name == "nt"

    operations = _config_operations(
        normalized_repos,
        selected_hosts,
        meridian_mode=meridian_mode,
        meridian_url=meridian_url,
        windows=windows,
        desktop_path=desktop_path,
    )
    plans: list[PlannedWrite] = []
    for path, kind, entries in operations:
        plan = _plan_json(path, entries, uninstall=uninstall) if kind == "json" else _plan_toml(
            path, entries, uninstall=uninstall
        )
        if plan is not None:
            plans.append(plan)

    if not plans:
        return ["No config changes needed; the bundle is already in the requested state."]
    results: list[str] = []
    for plan in plans:
        verb = "Would update" if dry_run else ("Updated" if plan.action == "write" else "Removed entries from")
        results.append(f"{verb} {plan.path}")
        if not dry_run:
            _write_atomic(plan.path, plan.content)
    if not uninstall:
        results.append("Restart the selected MCP hosts to load the servers.")
        if meridian_mode == "hosted":
            results.append("Set BEARER_TOKEN in each host's process environment for hosted Meridian; no credential was written to config.")
        results.append("The docs, outputs, and LaTeX servers require their published packages and uvx/npx runtimes.")
    return results


def session_hook_command(root: Path, pixi: str, *, windows: bool) -> str:
    """The SessionStart command: set up the repo the chat opened in, for Claude Code, Codex and Cursor.

    It does nothing unless the folder is a git repo, and it always exits 0, so a failed setup never blocks a session.
    The Windows form is PowerShell, which this machine's Meridian guard hooks already use.
    """
    manifest = (Path(root) / "pixi.toml").as_posix()
    checkout = Path(root).as_posix()
    pixi_path = Path(pixi).as_posix()
    hosts = "--host claude-code --host codex --host cursor"
    # The command enters the Meridian checkout first. `python -m meridian` imports the package from the current folder,
    # so running it from the chat's repo would pick up that repo's copy of Meridian, which may not have `setup`.
    if windows:
        # The script runs the same setup. A file path needs no quoting across shells, which Claude Code and Codex differ on.
        script = (Path(root) / "scripts" / "session-setup.ps1").as_posix()
        return f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script}"'
    return (
        f'[ -d "$CLAUDE_PROJECT_DIR/.git" ] && (cd "{checkout}" && "{pixi_path}" run --manifest-path "{manifest}" '
        f'python -m meridian setup --repo "$CLAUDE_PROJECT_DIR" {hosts} >/dev/null 2>&1); true'
    )


def _is_session_hook(hook: Any) -> bool:
    return isinstance(hook, dict) and any(marker in str(hook.get("command", "")) for marker in SESSION_HOOK_MARKERS)


def _with_session_hook(data: dict[str, Any], command: str | None) -> dict[str, Any]:
    """``data`` with every copy of this command's SessionStart hook removed, and one added when ``command`` is given.

    Nothing else in the file changes. A file with no hooks and no command to add comes back as it was.
    """
    updated = json.loads(json.dumps(data))
    hooks = updated.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise SetupBundleError("hooks in the Claude Code settings must be an object; it was left untouched")
    if hooks is None and command is None:
        return updated
    if hooks is None:
        hooks = {}
        updated["hooks"] = hooks
    groups = hooks.get("SessionStart", [])
    if not isinstance(groups, list):
        raise SetupBundleError("hooks.SessionStart in the Claude Code settings must be a list; it was left untouched")
    kept: list[Any] = []
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            remaining = [hook for hook in group["hooks"] if not _is_session_hook(hook)]
            if len(remaining) != len(group["hooks"]):
                if not remaining:
                    continue  # the group held only this hook
                group = {**group, "hooks": remaining}
        kept.append(group)
    if command is not None:
        kept.append({"matcher": SESSION_HOOK_MATCHER, "hooks": [{"type": "command", "command": command}]})
    if kept or "SessionStart" in hooks:
        hooks["SessionStart"] = kept
    return updated


def _settings_for_hook(settings: Path) -> dict[str, Any]:
    if not settings.exists():
        return {}
    try:
        text = settings.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise SetupBundleError(f"Could not read {settings}: {exc}") from exc
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise SetupBundleError(f"{settings} is not valid JSON; it was left untouched") from exc
    if not isinstance(data, dict):
        raise SetupBundleError(f"{settings} must contain a JSON object; it was left untouched")
    return data


def _write_settings(settings: Path, data: dict[str, Any]) -> None:
    """Back the settings file up once, before the first change, then write it atomically."""
    backup = settings.with_name(settings.name + SESSION_HOOK_BACKUP_SUFFIX)
    if settings.exists() and not backup.exists():
        shutil.copy2(settings, backup)
    _write_atomic(settings, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def install_session_hook(settings: Path, *, root: Path, pixi: str, windows: bool) -> str:
    """Install the SessionStart hook into Claude Code's user settings. Returns ``written`` or ``unchanged``.

    Other hooks are kept. Reinstalling replaces this hook only. Everything else in the file is left as it is.
    """
    if not (Path(root) / "pixi.toml").is_file():
        raise SetupBundleError(f"No pixi.toml in {root}; the hook must point at a Meridian checkout")
    data = _settings_for_hook(settings)
    updated = _with_session_hook(data, session_hook_command(root, pixi, windows=windows))
    if updated == data:
        return "unchanged"
    _write_settings(settings, updated)
    return "written"


def uninstall_session_hook(settings: Path) -> str:
    """Remove this command's SessionStart hook from Claude Code's user settings. Returns ``removed`` or ``unchanged``."""
    data = _settings_for_hook(settings)
    updated = _with_session_hook(data, None)
    if updated == data:
        return "unchanged"
    _write_settings(settings, updated)
    return "removed"


CODEX_HOOK_BEGIN = "# >>> meridian setup SessionStart >>>"
CODEX_HOOK_END = "# <<< meridian setup SessionStart <<<"


def _codex_hook_block(command: str) -> str:
    """The marked TOML block for Codex's SessionStart hook. The literal string keeps the command free of escaping."""
    if "'" in command or "\n" in command:
        raise SetupBundleError("The session hook command cannot contain a single quote or a newline for Codex")
    return "\n".join(
        [
            CODEX_HOOK_BEGIN,
            "[[hooks.SessionStart]]",
            f'matcher = "{SESSION_HOOK_MATCHER}"',
            "",
            "[[hooks.SessionStart.hooks]]",
            'type = "command"',
            f"command = '{command}'",
            CODEX_HOOK_END,
            "",
        ]
    )


def _codex_with_hook(original: str, block: str | None) -> str:
    """``original`` with this command's marked block replaced by ``block``, or removed when ``block`` is None.

    The result is checked as TOML before it is returned, so a bad block is never written.
    """
    lines = original.splitlines(keepends=True)
    kept: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped == CODEX_HOOK_BEGIN:
            skipping = True
            continue
        if skipping:
            if stripped == CODEX_HOOK_END:
                skipping = False
            continue
        kept.append(line)
    text = "".join(kept)
    if block is not None:
        separator = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        text = text + separator + block
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SetupBundleError(f"The Codex config would not parse with the session hook; it was left untouched ({exc})") from exc
    return text


def install_codex_session_hook(config: Path, *, command: str) -> str:
    """Write this command's SessionStart hook into Codex's config. Returns ``written`` or ``unchanged``."""
    original = config.read_text(encoding="utf-8-sig") if config.exists() else ""
    updated = _codex_with_hook(original, _codex_hook_block(command))
    if updated == original:
        return "unchanged"
    backup = config.with_name(config.name + SESSION_HOOK_BACKUP_SUFFIX)
    if config.exists() and not backup.exists():
        shutil.copy2(config, backup)
    _write_atomic(config, updated)
    return "written"


def uninstall_codex_session_hook(config: Path) -> str:
    """Remove this command's marked block from Codex's config. Returns ``removed`` or ``unchanged``."""
    if not config.exists():
        return "unchanged"
    original = config.read_text(encoding="utf-8-sig")
    updated = _codex_with_hook(original, None)
    if updated == original:
        return "unchanged"
    _write_atomic(config, updated)
    return "removed"


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="meridian setup",
        description="Merge Meridian, Serena, Codebase Memory, Docs, Outputs, and LaTeX MCP servers into host configs.",
    )
    parser.add_argument(
        "--repo", "--repos", action="append", nargs="+", metavar="PATH",
        help="Repository root to configure (repeatable; defaults to the current directory).",
    )
    parser.add_argument(
        "--host", "--hosts", action="append", nargs="+", choices=HOSTS, metavar="HOST",
        help="Host to configure: claude-code, claude-desktop, codex, or cursor (repeatable; default: all).",
    )
    parser.add_argument(
        "--mode", "--meridian-mode", dest="meridian_mode", choices=("hosted", "self-hosted"), default="hosted",
        help="Meridian transport mode (default: hosted).",
    )
    parser.add_argument("--meridian-url", default=DEFAULT_MERIDIAN_URL, help="Hosted Meridian base URL.")
    parser.add_argument("--dry-run", action="store_true", help="Show config files that would change without writing.")
    parser.add_argument("--uninstall", action="store_true", help=f"Remove this command's {MANAGED_PREFIX} entries.")
    parser.add_argument(
        "--install-session-hook", action="store_true",
        help=(
            "Install the Claude Code and Codex SessionStart hooks: a chat in a git repo sets up that repo. "
            "Writes ~/.claude/settings.json and ~/.codex/config.toml."
        ),
    )
    parser.add_argument(
        "--uninstall-session-hook", action="store_true", help="Remove only this command's SessionStart hook.",
    )
    args = parser.parse_args(argv)
    if args.install_session_hook or args.uninstall_session_hook:
        claude_settings = Path.home() / ".claude" / "settings.json"
        codex_config = Path.home() / ".codex" / "config.toml"
        try:
            if args.uninstall_session_hook:
                claude = uninstall_session_hook(claude_settings)
                codex = uninstall_codex_session_hook(codex_config)
            else:
                pixi = shutil.which("pixi")
                if not pixi:
                    raise SetupBundleError("pixi was not found on PATH; install pixi, then run this again")
                root = Path(__file__).resolve().parents[1]
                if not (root / "scripts" / "session-setup.ps1").is_file() and os.name == "nt":
                    raise SetupBundleError(f"scripts/session-setup.ps1 is missing from {root}")
                windows = os.name == "nt"
                claude = install_session_hook(claude_settings, root=root, pixi=pixi, windows=windows)
                command = session_hook_command(root, pixi, windows=windows)
                codex = install_codex_session_hook(codex_config, command=command)
        except (OSError, SetupBundleError, ValueError) as exc:
            print(f"meridian setup: {exc}", file=sys.stderr)
            return 1
        print(f"Claude Code session hook {claude}: {claude_settings}")
        print(f"Codex session hook {codex}: {codex_config}")
        return 0

    repo_args = [item for group in (args.repo or []) for item in group]
    host_args = [item for group in (args.host or []) for item in group]
    raw_repos = [Path(item) for item in repo_args] if repo_args else [Path.cwd()]
    try:
        repos = [repo.expanduser().resolve(strict=True) for repo in raw_repos]
        if any(not repo.is_dir() for repo in repos):
            raise SetupBundleError("Every --repo path must be an existing directory")
        for line in configure_bundle(
            repos,
            host_args or None,
            meridian_mode=args.meridian_mode,
            meridian_url=args.meridian_url,
            dry_run=args.dry_run,
            uninstall=args.uninstall,
        ):
            print(line)
    except (OSError, SetupBundleError, ValueError) as exc:
        print(f"meridian setup: {exc}", file=sys.stderr)
        return 1
    return 0
