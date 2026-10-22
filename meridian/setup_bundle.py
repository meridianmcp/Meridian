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
    args = parser.parse_args(argv)

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
