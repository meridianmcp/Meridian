"""Helper: the full dashboard frontend source as one string.

dashboard.js was split into dashboard-*.js ES modules (v1.1 extraction sprint,
item c47759ba). White-box tests that assert on frontend source — feature
strings, exact emoji labels, "function F must not use window.prompt" — need the
*raw, untransformed* source of every module, not the esbuild bundle (which
renames variables, reflows, and may \\u-escape unicode) and not dashboard.js
alone (which no longer holds the moved tab renderers).

Concatenating the raw module files preserves exact strings/structure and covers
all extracted code, so both `in` and `not in` assertions stay correct.
"""
from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).parent.parent / "meridian" / "static"


def dashboard_source() -> str:
    """Return dashboard entry + every dashboard-* module concatenated (raw).

    423f5929 — the modules migrated from .js to .ts. We glob both extensions so
    white-box assertions keep working across the migration, and read whichever
    entry file exists (dashboard.ts after the migration, dashboard.js before).
    """
    entry = _STATIC / "dashboard.ts"
    if not entry.exists():
        entry = _STATIC / "dashboard.js"
    parts = [entry.read_text(encoding="utf-8")]
    seen = {entry.name}
    for mod in sorted(_STATIC.glob("dashboard-*.*")):
        if mod.suffix not in (".js", ".ts") or mod.name in seen:
            continue
        if mod.name == "dashboard.bundle.js":
            continue
        seen.add(mod.name)
        parts.append(mod.read_text(encoding="utf-8"))
    return "\n".join(parts)
