"""Shared pytest setup for the meridian-inventree test suite."""

from __future__ import annotations

import sys
from pathlib import Path

_PACKAGE_ROOT = str(Path(__file__).resolve().parents[1])
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)
