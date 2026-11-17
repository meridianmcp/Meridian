"""AGENTS.md high-contention file list must name only files that exist.

The "High-contention files" bullet in AGENTS.md tells parallel executors which
shared files to serialize on. A stale path there (for example a renamed
dashboard file) silently defeats that guidance, so every path in the list must
resolve to a real file in this checkout.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_MD = REPO_ROOT / "AGENTS.md"

_BULLET_RE = re.compile(r"^- \*\*High-contention files\b")
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def _high_contention_paths(text: str) -> list[str]:
    """Return the backticked repo paths in the High-contention files bullet."""
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if _BULLET_RE.match(line)),
        None,
    )
    assert start is not None, "High-contention files bullet not found in AGENTS.md"

    paths: list[str] = []
    for offset, line in enumerate(lines[start:]):
        # Continuation lines of the bullet are indented; the first unindented
        # line after the bullet ends the list.
        if offset and not line.startswith(" "):
            break
        paths.extend(m for m in _BACKTICK_RE.findall(line) if "/" in m)
    return paths


def test_high_contention_paths_parse_and_exist():
    paths = _high_contention_paths(AGENTS_MD.read_text(encoding="utf-8-sig"))

    assert paths, "High-contention files list parsed to zero paths"
    missing = [p for p in paths if not (REPO_ROOT / p).exists()]
    assert not missing, f"AGENTS.md high-contention list names missing files: {missing}"
