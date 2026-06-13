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
    """Return dashboard.js + every dashboard-*.js module concatenated (raw)."""
    parts = [(_STATIC / "dashboard.js").read_text(encoding="utf-8")]
    for mod in sorted(_STATIC.glob("dashboard-*.js")):
        if mod.name == "dashboard.bundle.js":
            continue
        parts.append(mod.read_text(encoding="utf-8"))
    return "\n".join(parts)
