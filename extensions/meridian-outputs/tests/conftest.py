"""Shared pytest fixtures for the meridian-outputs test suite."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import outputs_local as OL


@pytest.fixture(autouse=True)
def _clear_index_cache():
    """Close and clear the module-level index cache after every test.

    _get_cached_index (0c1a4349) now opens real on-disk DuckDB files under
    each test's tmp_path. Left cached across tests, an open connection can
    block pytest's tmp_path cleanup on Windows (file in use), and stale
    entries would leak between unrelated tests since the cache is a module
    global, not per-test state.
    """
    yield
    with OL._index_cache_lock:
        while OL._index_cache:
            _, idx = OL._index_cache.popitem()
            idx.close()
