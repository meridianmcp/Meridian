"""pytest conftest for meridian-plugin-base tests.

Adds the package root (packages/meridian-plugin-base) to sys.path so tests can
import meridian_plugin_base without installing the package first. This mirrors
the pattern used by packages/docparse tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Insert the package root so `import meridian_plugin_base` works when running
# pytest directly from the monorepo root (where the package is not yet installed
# into the pixi env's site-packages).
_PKG_ROOT = Path(__file__).parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
