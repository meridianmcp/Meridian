"""The keepalive window must stay inside the claim window.

Each keepalive refresh is a database write, and on a scale-to-zero Neon endpoint a write keeps the compute awake. The
TTL is the awake tail after each tool call, so it is kept short. A session that stops refreshing keeps its claims only
until the claim window in db/locks.py runs out. These tests keep the two windows consistent.
"""
from __future__ import annotations

from pathlib import Path

import meridian.server as srv  # import first -- avoids handler/server import cycle

CLAIM_WINDOW_S = 10 * 60
LOCKS_SOURCE = Path(__file__).resolve().parents[1] / "meridian" / "db" / "locks.py"


def test_keepalive_window_fits_the_claim_window():
    # A session refreshed on every tick stays fresh; the last refresh is at most one interval past the TTL.
    assert srv.SESSION_KEEPALIVE_TTL_S + srv.SESSION_KEEPALIVE_INTERVAL_S <= CLAIM_WINDOW_S


def test_default_keepalive_ttl_is_five_minutes():
    # Read the source, not a reload: reloading the server module would disturb state other tests share.
    server_source = (Path(__file__).resolve().parents[1] / "meridian" / "server.py").read_text(encoding="utf-8")
    assert 'os.environ.get("MERIDIAN_KEEPALIVE_TTL_S", "300")' in server_source


def test_claim_window_in_locks_is_still_ten_minutes():
    # The invariant above assumes this value. If it changes, change CLAIM_WINDOW_S with it.
    assert "datetime('now', '-10 minutes')" in LOCKS_SOURCE.read_text(encoding="utf-8")
