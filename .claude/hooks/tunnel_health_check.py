#!/usr/bin/env python3
"""Best-effort SessionStart tunnel diagnostics for Claude Code.

Claude Code starts SessionStart hooks before its MCP connections are ready.
The authenticated diagnostics HTTP route uses the same builder as the
get_tunnel_diagnostics MCP tool, so this hook calls that route directly. Any
missing config, auth, tunnel, timeout, or malformed response is a silent
no-op; session startup must always continue.

The route URL and bearer token are taken from explicit environment variables
or an existing Meridian MCP server entry in .mcp.json / ~/.claude.json. The
token is used only in the HTTPS request and is never logged or returned.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


_EMPTY_OUTPUT = (
    '{"hookSpecificOutput":{"hookEventName":"SessionStart",'
    '"additionalContext":""}}'
)
_REQUEST_TIMEOUT_SECONDS = 1.5
_MAX_RESPONSE_BYTES = 512 * 1024
_URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_ENV_PATTERN = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)"
)
_SAFE_LABEL_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


def _env_lookup(environ: Mapping[str, str]) -> dict[str, str]:
    return {str(key).upper(): str(value) for key, value in environ.items()}


def _expand_env(value: Any, environ: Mapping[str, str]) -> str:
    if not isinstance(value, str):
        return ""
    values = _env_lookup(environ)

    def replace(match: re.Match[str]) -> str:
        braced_name, default, plain_name = match.groups()
        name = (braced_name or plain_name or "").upper()
        return values.get(name, default or "")

    return _ENV_PATTERN.sub(replace, value).strip()


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        left_value = str(Path(left).expanduser().resolve()).replace("\\", "/").casefold()
        right_value = str(Path(right).expanduser().resolve()).replace("\\", "/").casefold()
        return left_value == right_value
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _server_maps(config: Mapping[str, Any], project_dir: Path):
    direct = config.get("mcpServers")
    if isinstance(direct, dict):
        yield direct

    projects = config.get("projects")
    if not isinstance(projects, dict):
        return
    for project_path, project_config in projects.items():
        if not _same_path(str(project_path), project_dir):
            continue
        if not isinstance(project_config, dict):
            continue
        scoped = project_config.get("mcpServers")
        if isinstance(scoped, dict):
            yield scoped


def _mcp_config_paths(project_dir: Path, home_dir: Path, environ: Mapping[str, str]):
    config_dir = Path(environ.get("CLAUDE_CONFIG_DIR") or (home_dir / ".claude"))
    paths = [
        project_dir / ".mcp.json",
        project_dir / ".claude" / "settings.json",
        home_dir / ".claude.json",
        config_dir.parent / ".claude.json",
        config_dir / "settings.json",
    ]
    seen: set[str] = set()
    for path in paths:
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        yield path


def _server_url(server: Mapping[str, Any]) -> str | None:
    direct = server.get("url")
    candidates: list[str] = [direct] if isinstance(direct, str) else []
    for field in ("args", "command"):
        value = server.get(field)
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, str))

    for candidate in candidates:
        match = _URL_PATTERN.search(candidate)
        if match:
            return match.group(0).rstrip(",;.)]")
    return None


def _server_is_meridian(name: str, server_url: str) -> bool:
    if "meridian" in name.casefold():
        return True
    try:
        return urlsplit(server_url).hostname == "usemeridian.us"
    except ValueError:
        return False


def _auth_token(server: Mapping[str, Any], environ: Mapping[str, str]) -> str:
    config_env = server.get("env")
    if not isinstance(config_env, dict):
        config_env = {}
    values = _env_lookup(environ)
    for name in (
        "MERIDIAN_TUNNEL_DIAGNOSTICS_TOKEN",
        "MERIDIAN_BEARER_TOKEN",
        "BEARER_TOKEN",
    ):
        value = _expand_env(config_env.get(name), environ) or values.get(name, "")
        if value:
            return value.removeprefix("Bearer ").strip()

    headers = server.get("headers")
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).casefold() == "authorization":
                expanded = _expand_env(value, environ)
                if expanded.casefold().startswith("bearer "):
                    return expanded[7:].strip()
    return ""


def _diagnostics_url(server_url: str) -> str | None:
    try:
        parsed = urlsplit(server_url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.path.startswith("/tunnel/diagnostics/"):
        return server_url
    return f"{parsed.scheme}://{parsed.netloc}/tunnel/diagnostics/session-start"


def resolve_diagnostics_target(
    *,
    project_dir: str | Path,
    home_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    """Resolve an endpoint and optional token without printing credentials."""
    env = dict(os.environ if environ is None else environ)
    project = Path(project_dir).expanduser()
    home = Path(home_dir or Path.home()).expanduser()
    direct_url = env.get("MERIDIAN_TUNNEL_DIAGNOSTICS_URL", "").strip()
    direct_token = (
        env.get("MERIDIAN_TUNNEL_DIAGNOSTICS_TOKEN")
        or env.get("MERIDIAN_BEARER_TOKEN")
        or ""
    ).removeprefix("Bearer ").strip()
    if direct_url:
        endpoint = _diagnostics_url(direct_url)
        return (endpoint, direct_token) if endpoint else None

    api_url = (env.get("MERIDIAN_API_URL") or env.get("MERIDIAN_BASE_URL") or "").strip()
    if api_url and direct_token:
        endpoint = _diagnostics_url(api_url)
        return (endpoint, direct_token) if endpoint else None

    for config_path in _mcp_config_paths(project, home, env):
        config = _load_json(config_path)
        if config is None:
            continue
        for servers in _server_maps(config, project):
            for name, server in servers.items():
                if not isinstance(server, dict):
                    continue
                server_url = _server_url(server)
                if not server_url or not _server_is_meridian(str(name), server_url):
                    continue
                endpoint = _diagnostics_url(server_url)
                if not endpoint:
                    continue
                token = _auth_token(server, env)
                try:
                    hosted = urlsplit(server_url).hostname == "usemeridian.us"
                except ValueError:
                    hosted = False
                if hosted and not token:
                    continue
                return endpoint, token

    return None


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        try:
            old = urlsplit(request.full_url)
            new = urlsplit(new_url)
        except ValueError:
            return None
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            return None
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


def fetch_diagnostics(
    target: tuple[str, str],
    *,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    url, token = target
    headers = {"Accept": "application/json", "User-Agent": "Meridian-SessionStart/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    if opener is None:
        opener = build_opener(_SameOriginRedirectHandler()).open
    try:
        with opener(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            return None
        payload = json.loads(body.decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _safe_label(value: Any) -> str:
    label = _SAFE_LABEL_PATTERN.sub("_", str(value).strip())[:64]
    return label or "unknown"


def unhealthy_slots(diagnostics: Mapping[str, Any]) -> list[tuple[str, str]]:
    slots = diagnostics.get("slots")
    if not isinstance(slots, dict):
        return []
    unhealthy: list[tuple[str, str]] = []
    for name, details in slots.items():
        if not isinstance(details, dict):
            continue
        configured = details.get("dashboard_configured")
        enabled = configured.get("enabled") if isinstance(configured, dict) else details.get("enabled")
        state = str(details.get("state") or "unknown").strip()
        if enabled is not True or state.casefold() == "disabled":
            continue
        if state.casefold() != "healthy":
            unhealthy.append((_safe_label(name), _safe_label(state)))
    return unhealthy


def warning_message(diagnostics: Mapping[str, Any]) -> str | None:
    unhealthy = unhealthy_slots(diagnostics)
    if not unhealthy:
        return None
    slots = ", ".join(f"{name} ({state})" for name, state in unhealthy)
    return (
        f"Meridian tunnel slots need attention: {slots}. "
        "Reconnect via /mcp or restart the tray app."
    )


def build_hook_output(message: str | None) -> str:
    if not message:
        return _EMPTY_OUTPUT
    return json.dumps({"systemMessage": message}, ensure_ascii=True, separators=(",", ":"))


def run(
    *,
    project_dir: str | Path | None = None,
    home_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    opener: Callable[..., Any] | None = None,
) -> str:
    env = dict(os.environ if environ is None else environ)
    project = project_dir or env.get("CLAUDE_PROJECT_DIR") or Path.cwd()
    target = resolve_diagnostics_target(
        project_dir=project,
        home_dir=home_dir,
        environ=env,
    )
    if target is None:
        return _EMPTY_OUTPUT
    diagnostics = fetch_diagnostics(target, opener=opener)
    if diagnostics is None:
        return _EMPTY_OUTPUT
    return build_hook_output(warning_message(diagnostics))


def main() -> int:
    try:
        print(run())
    except Exception:  # noqa: BLE001 — startup health checks must fail open.
        print(_EMPTY_OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
