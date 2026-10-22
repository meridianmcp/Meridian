"""Claude Code PostToolUse entry point for opted-in local artifact capture."""

from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from meridian.artifact_capture import hook_main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(hook_main())
