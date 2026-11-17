"""Optional live read-only checks against a real InvenTree instance.

These run only when INVENTREE_URL and INVENTREE_TOKEN are both set in the environment, and they
skip otherwise, so the default suite stays offline. Every check is a read. Nothing here writes to
InvenTree. Use a read-only user's token.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from meridian_inventree.server import build_app

LIVE = bool(os.environ.get("INVENTREE_URL", "").strip() and os.environ.get("INVENTREE_TOKEN", "").strip())
pytestmark = pytest.mark.skipif(not LIVE, reason="INVENTREE_URL and INVENTREE_TOKEN are not both set")


def _app(tmp_path: Path):
    env = {
        "INVENTREE_URL": os.environ["INVENTREE_URL"],
        "INVENTREE_TOKEN": os.environ["INVENTREE_TOKEN"],
        "MERIDIAN_INVENTREE_STATE": str(tmp_path / "state"),
        "MERIDIAN_INVENTREE_READ_ONLY": "1",
    }
    return build_app(env)


def test_live_version_and_role_object(tmp_path: Path) -> None:
    app = _app(tmp_path)
    version = app.tools["inventree_check_version"]()
    assert version["reported_version"]
    roles = app.tools["inventree_roles"]()
    assert isinstance(roles["roles"], dict)
    assert isinstance(roles["is_superuser"], bool)


def test_live_part_search_returns_items(tmp_path: Path) -> None:
    result = _app(tmp_path).tools["inventree_search_parts"](query="")
    assert isinstance(result["items"], list)
