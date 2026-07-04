"""Read/write meridian.toml connection profiles.

meridian.toml lives in the working directory (repo root) and stores named DB
connections plus the currently-active one.  The format is:

    [default]
    connection = "local"

    [connections.local]
    type = "sqlite"

    [connections.my_neon]
    type = "postgres"
    url = "postgresql://user:pass@host/db"

Environment variable ``MERIDIAN_DB_URL`` always takes precedence over the
toml so CI / container deployments are not affected.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Location searched in order: CWD, then package parent.
_TOML_FILENAME = "meridian.toml"


def _toml_path() -> Path | None:
    """Return the path to meridian.toml if it exists, else None."""
    cwd_path = Path.cwd() / _TOML_FILENAME
    if cwd_path.exists():
        return cwd_path
    pkg_parent = Path(__file__).parent.parent / _TOML_FILENAME
    if pkg_parent.exists():
        return pkg_parent
    return None


def toml_exists() -> bool:
    return _toml_path() is not None


def load_toml() -> dict[str, Any] | None:
    """Parse meridian.toml, returning the dict or None if absent/unreadable."""
    p = _toml_path()
    if p is None:
        return None
    try:
        import tomllib  # Python 3.11+
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_toml(default_connection: str, connections: dict[str, dict[str, str]]) -> Path:
    """Write meridian.toml to the current working directory."""
    lines: list[str] = [
        "# Meridian connection profiles\n",
        "# Edit manually or via the dashboard.\n\n",
        "[default]\n",
        f'connection = "{default_connection}"\n\n',
    ]
    for name, cfg in connections.items():
        lines.append(f"[connections.{name}]\n")
        for k, v in cfg.items():
            lines.append(f'{k} = "{v}"\n')
        lines.append("\n")

    dest = Path.cwd() / _TOML_FILENAME
    dest.write_text("".join(lines), encoding="utf-8")
    return dest


def get_toml_db_url() -> tuple[str | None, str | None]:
    """Return ``(db_url_or_none, conn_name_or_none)`` from toml only, ignoring env.

    Returns (None, None) if no meridian.toml exists.
    Returns (None, "local") if toml says sqlite.
    Returns (url, name) if toml says postgres.
    """
    data = load_toml()
    if data is None:
        return None, None  # no toml at all
    conn_name = data.get("default", {}).get("connection", "local")
    conn = data.get("connections", {}).get(conn_name, {})
    conn_type = conn.get("type", "sqlite")
    if conn_type == "postgres":
        url = conn.get("url") or os.environ.get(conn.get("url_env", ""), "")
        return url or None, conn_name
    return None, conn_name  # sqlite


def get_active_db_url() -> tuple[str | None, str]:
    """Return ``(db_url_or_none, connection_name)``.

    db_url_or_none: None means use local SQLite; a non-empty string is a
    Postgres URL.  connection_name is the display name of the active profile.

    Priority: MERIDIAN_DB_URL env > meridian.toml > "local" default.
    """
    env_url = os.environ.get("MERIDIAN_DB_URL")
    if env_url:
        return env_url, "env"

    data = load_toml()
    if data is None:
        return None, "local"

    active = data.get("default", {}).get("connection", "local")
    connections = data.get("connections", {})
    cfg = connections.get(active, {})

    if cfg.get("type") == "postgres":
        url = cfg.get("url", "")
        # Expand simple ${VAR} references.
        if url.startswith("${") and url.endswith("}"):
            url = os.environ.get(url[2:-1], "")
        return (url or None), active

    return None, active


def list_connections() -> list[dict[str, Any]]:
    """List all named connections from meridian.toml.

    Always returns at least one entry (the implicit 'local' SQLite profile).
    """
    data = load_toml()
    if data is None:
        return [{"name": "local", "type": "sqlite", "active": True}]

    active = data.get("default", {}).get("connection", "local")
    connections = data.get("connections", {})

    result = []
    for name, cfg in connections.items():
        entry: dict[str, Any] = {
            "name": name,
            "type": cfg.get("type", "sqlite"),
            "active": name == active,
        }
        if cfg.get("type") == "postgres":
            raw = cfg.get("url", "")
            # Mask the password for display.
            entry["url_masked"] = _mask_url(raw)
        result.append(entry)

    if not result:
        result = [{"name": "local", "type": "sqlite", "active": True}]

    return result


def _mask_url(url: str) -> str:
    """Replace password in Postgres URL with *** for display."""
    try:
        import re
        return re.sub(r"(?<=://)[^:@]+:[^@]+@", "***:***@", url)
    except Exception:
        return "***"


# ---------------------------------------------------------------------------
# 46c83e55 — generic self-host config readers (env > toml > hardcoded default).
#
# These provide the SELF-HOST fallback values. For per-tenant behaviour the
# authoritative override is the workspace_settings DB row (bf51b12e): the MCP
# dispatch hook reads workspace_settings, and this toml is only the self-host
# default seed. toml is intentionally NOT wired into the hook — a self-host
# operator seeds workspace_settings (or relies on the built-in defaults); these
# readers exist so a future seed/self-host path can consult meridian.toml.
# ---------------------------------------------------------------------------

# Mirror of handler._PLANNER_REFRESH_TRIGGERS, duplicated here to avoid importing
# the heavy MCP handler module just to read config. Keep the two in sync.
_DEFAULT_REFRESH_TRIGGERS: list[str] = [
    "add_insight",
    "pin_decision",
    "pin_workspace_decision",
    "set_north_star",
    "set_goal",
    "generate_handoff",
]


def _coerce_bool(value: Any) -> bool:
    """Interpret an env/toml scalar as a boolean.

    Accepts real bools (toml) and the usual truthy/falsey strings (env).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _read_table(table: str) -> dict[str, Any]:
    """Return the named top-level toml table as a dict, or {} if absent."""
    data = load_toml()
    if not isinstance(data, dict):
        return {}
    section = data.get(table)
    return section if isinstance(section, dict) else {}


def get_context_refresh_config() -> dict[str, Any]:
    """Return the self-host context-refresh defaults.

    Precedence per key (matching ``get_active_db_url``'s env-first style):
        env  >  [context_refresh] toml table  >  hardcoded default.

    Keys / env vars:
        auto_refresh_enabled     — MERIDIAN_AUTO_REFRESH            (default False)
        refresh_interval_turns   — MERIDIAN_REFRESH_INTERVAL_TURNS  (default 10)
        refresh_triggers         — MERIDIAN_REFRESH_TRIGGERS (csv)  (default set)

    This is the self-host seed only; a per-tenant ``workspace_settings`` row is
    the authoritative override when present (see module docstring above).
    """
    toml_tbl = _read_table("context_refresh")

    # auto_refresh_enabled
    env_enabled = os.environ.get("MERIDIAN_AUTO_REFRESH")
    if env_enabled is not None:
        auto_refresh_enabled = _coerce_bool(env_enabled)
    elif "auto_refresh_enabled" in toml_tbl:
        auto_refresh_enabled = _coerce_bool(toml_tbl["auto_refresh_enabled"])
    else:
        auto_refresh_enabled = False

    # refresh_interval_turns
    env_interval = os.environ.get("MERIDIAN_REFRESH_INTERVAL_TURNS")
    interval_raw: Any = env_interval if env_interval is not None else toml_tbl.get(
        "refresh_interval_turns", 10
    )
    try:
        refresh_interval_turns = max(1, int(interval_raw))
    except (TypeError, ValueError):
        refresh_interval_turns = 10

    # refresh_triggers
    env_triggers = os.environ.get("MERIDIAN_REFRESH_TRIGGERS")
    if env_triggers is not None:
        refresh_triggers = [t.strip() for t in env_triggers.split(",") if t.strip()]
    elif "refresh_triggers" in toml_tbl:
        raw = toml_tbl["refresh_triggers"]
        if isinstance(raw, list):
            refresh_triggers = [str(t).strip() for t in raw if str(t).strip()]
        elif isinstance(raw, str):
            refresh_triggers = [t.strip() for t in raw.split(",") if t.strip()]
        else:
            refresh_triggers = list(_DEFAULT_REFRESH_TRIGGERS)
    else:
        refresh_triggers = list(_DEFAULT_REFRESH_TRIGGERS)

    return {
        "auto_refresh_enabled": auto_refresh_enabled,
        "refresh_interval_turns": refresh_interval_turns,
        "refresh_triggers": refresh_triggers,
    }


def get_self_host_defaults() -> dict[str, Any]:
    """Return misc self-host default seeds (env > toml > hardcoded default).

    Read from the ``[meridian]`` toml table. These are documented defaults; the
    per-project / per-tenant DB values remain authoritative at runtime — wiring
    these readers into their consumers is a follow-up (see the sprint report).

    Keys / env vars:
        loop_enabled_default  — MERIDIAN_LOOP_ENABLED     (default True)
        max_turns_default     — MERIDIAN_MAX_TURNS        (default 0 = unlimited)
        filesystem_roots      — MERIDIAN_FILESYSTEM_ROOTS (csv, default [])
    """
    toml_tbl = _read_table("meridian")

    # loop_enabled_default
    env_loop = os.environ.get("MERIDIAN_LOOP_ENABLED")
    if env_loop is not None:
        loop_enabled_default = _coerce_bool(env_loop)
    elif "loop_enabled_default" in toml_tbl:
        loop_enabled_default = _coerce_bool(toml_tbl["loop_enabled_default"])
    else:
        loop_enabled_default = True

    # max_turns_default
    env_turns = os.environ.get("MERIDIAN_MAX_TURNS")
    turns_raw: Any = env_turns if env_turns is not None else toml_tbl.get(
        "max_turns_default", 0
    )
    try:
        max_turns_default = max(0, int(turns_raw))
    except (TypeError, ValueError):
        max_turns_default = 0

    # filesystem_roots
    env_roots = os.environ.get("MERIDIAN_FILESYSTEM_ROOTS")
    if env_roots is not None:
        filesystem_roots = [r.strip() for r in env_roots.split(",") if r.strip()]
    elif "filesystem_roots" in toml_tbl:
        raw = toml_tbl["filesystem_roots"]
        if isinstance(raw, list):
            filesystem_roots = [str(r).strip() for r in raw if str(r).strip()]
        elif isinstance(raw, str):
            filesystem_roots = [r.strip() for r in raw.split(",") if r.strip()]
        else:
            filesystem_roots = []
    else:
        filesystem_roots = []

    return {
        "loop_enabled_default": loop_enabled_default,
        "max_turns_default": max_turns_default,
        "filesystem_roots": filesystem_roots,
    }
