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
