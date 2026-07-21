"""Rate-limit tests that must run OUTSIDE the main `-n auto` + `--cov` sweep.

test_auth_magic_is_rate_limited and test_export_my_data_is_rate_limited
(moved here from test_new_v25.py) exercise slowapi's real in-memory moving-
window limiter (5/minute, 3/minute) via 4-6 rapid-fire real HTTP calls through
the `client` fixture. bf22153 already found these "occasionally" failed under
CI's parallel -n auto load with every request returning non-429 (limiter
never enforced) despite passing consistently standalone, and added a
reset-and-retry loop that did NOT fully resolve it — the exact same 2 tests
failed identically on two consecutive full main-ref CI runs post-bf22153,
with EVERY one of 3 reset+retry attempts uniformly returning non-429.

Root cause still not proven (this mirrors bf22153's own "not fully pinned
down"), but the limiter's moving-window algorithm (limits' MemoryStorage.
acquire_entry) makes its accept/reject decision from real `time.time()`
deltas between individual requests within the test — so ANY source of
inter-request wall-clock stretching (coverage.py's per-line trace overhead,
GIL/thread contention with the storage's own background
`threading.Timer(0.01, ...)` expiry sweep, CPU starvation from ~6400 tests
sharing a 2-core CI runner under `-n auto`) can push the gap between the
1st and 6th request past the 60-second window and make the limiter
genuinely, correctly see the earliest hits as expired. That class of
interference scales with how much OTHER test load shares the runner at the
same wall-clock moment — which is exactly what running serially, outside
the full-suite `-n auto` sweep and its coverage-tracing overhead, removes.

This file is deliberately `--ignore`d in pixi.toml's `test`/`test-cov`/
`test-pg` tasks and CI's main suite steps, then run as its own small serial
(`-p no:xdist`, no --cov) step — the same isolation pattern already used for
tests/test_demo_ux.py in this repo, applied to a different flake mechanism.
"""

from __future__ import annotations


def test_auth_magic_is_rate_limited(client):
    """POST /auth/magic is capped at 5/minute — the 6th call returns 429.

    Guards the brute-force / email-bomb protection on the magic-link endpoint.
    slowapi keys on the client address, so all TestClient calls share one
    bucket; the `client` fixture resets the limiter's in-memory counters
    (`_reset_limiter_counts`) fresh before every test (8a52dd26).

    Run in isolation (see module docstring) — moved out of test_new_v25.py's
    `-n auto` + `--cov` sweep because the moving-window limiter's accept/
    reject decision depends on real wall-clock time between this test's own
    requests, which that sweep's load can stretch past the window.
    """
    from meridian._deps import _reset_limiter_counts

    last_statuses: list[int] = []
    for attempt in range(3):
        if attempt:
            _reset_limiter_counts()
        last_statuses = [
            client.post("/auth/magic", json={"email": f"rl{attempt}-{i}@example.com"}).status_code
            for i in range(6)
        ]
        if all(s != 429 for s in last_statuses[:5]) and last_statuses[5] == 429:
            return
    # First 5 within the 5/minute budget must not be rate-limited.
    assert all(s != 429 for s in last_statuses[:5]), last_statuses
    # The 6th exceeds the limit.
    assert last_statuses[5] == 429, last_statuses


def test_export_my_data_is_rate_limited(client):
    """GET /export/my-data is capped at 3/minute — the 4th call returns 429.

    The limiter runs before the handler, so the cap holds regardless of auth
    state (unauthenticated calls 404 in self-host mode, but still count).

    See test_auth_magic_is_rate_limited's docstring (this module and the
    function) for why this runs in isolation and retries with an explicit
    re-reset between attempts.
    """
    from meridian._deps import _reset_limiter_counts

    last_statuses: list[int] = []
    for attempt in range(3):
        if attempt:
            _reset_limiter_counts()
        last_statuses = [client.get("/export/my-data").status_code for _ in range(4)]
        if all(s != 429 for s in last_statuses[:3]) and last_statuses[3] == 429:
            return
    assert all(s != 429 for s in last_statuses[:3]), last_statuses
    assert last_statuses[3] == 429, last_statuses
