"""b71e0960 -- bounded cross-MCP concurrent tool-call fanout stress/contract
tests (the "batch_read" half of the sprint item's requested matrix).

INVESTIGATION FINDING (recorded before writing a single test): the sprint
item's own touches_resources name ``meridian/batch_read.py::batch_read`` and
``meridian/batch_mutate.py::batch_mutate`` as the target symbols for this
file. Neither module exists anywhere in this repository -- confirmed via a
repo-wide grep for "batch_read"/"batch_mutate" (zero hits, including in
tests/) and a codebase-memory ``search_graph`` query (zero hits). There is no
MCP tool, route, or Python symbol by either name. The only real "batch"
primitive in this codebase is ``meridian.db.batch_management.execute_batch``
(a single, homogeneous MUTATION-only engine -- see
tests/test_batch_management_writes.py, which this item also touches), and it
processes entries strictly SEQUENTIALLY with no external I/O at all (its own
module docstring: "Never holds a transaction across an external call") -- it
structurally cannot be the target of "independent concurrent ops actually run
concurrently" / "bounded concurrency" / "one slow/failed external slot can't
starve unrelated slots", all of which describe CONCURRENT fan-out to
EXTERNAL processes, not a sequential internal DB-write engine.

The actual, real implementation of "cross-MCP fan-out to external processes,
bounded and slot-isolated" is meridian/routes/tunnel.py's per-(slot, tenant)
in-flight bulkhead (``_slot_semaphore`` / ``_slot_inflight``, sprint item
1d021501) guarding ``_do_proxy`` -- the shared relay every tunneled
``tools/call`` (read or mutating, proxied to an externally-connected MCP
server slot such as filesystem/code/word/etc.) funnels through, whether
reached via the HTTP relay routes or ``call_tunnel_tool``. This file targets
THAT real mechanism instead of the nonexistent batch_read/batch_mutate
modules, using disposable fake WebSocket objects (no live tunnel, no real
external MCP process) to control exactly when each "external" response
arrives and prove the concurrency/bulkhead/isolation contract the sprint
item asks for.

Existing coverage note (checked before adding anything here, to avoid padding
the suite with near-duplicates): tests/test_fault_injection.py already has
``test_hung_tunnel_slot_does_not_block_other_slot`` (a permanently-silent 'fs'
slot vs. a healthy 'code' slot) and ``test_saturated_slot_fails_fast_with_503``
(single-slot 503 under a real, non-zero 1-permit saturation); together with
tests/test_tunnel_coverage_0882b8d6.py's ``test_do_proxy_503_when_slot_saturated``
(a synthetically forced capacity=0 slot), cross-slot isolation and basic
saturation are already solidly covered. What none of those cover, and what
this file adds:

* A real (non-zero, N=2) admission-control cap proven to admit EXACTLY N
  concurrent in-flight calls to one slot under genuine asyncio concurrency,
  with the (N+1)th observably still queued until a permit frees up --
  distinct from a synthetic 0-capacity 503 case.
* A COMBINED fault: a saturated, slowly-recovering 'fs' slot (real 503 on the
  queued caller) occurring AT THE SAME TIME as an unrelated 'code' slot call,
  proving saturation on one slot never leaks into another slot's latency even
  while that other slot independently succeeds.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import meridian.server  # noqa: F401 -- load through the normal path (avoids
# the same handler/server import cycle every other tunnel test file guards
# against).
from meridian.routes import tunnel as tn


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_tunnel_state():
    """Reset every per-process tunnel registry this file touches, including
    the in-flight bulkhead semaphores and _max_slot_inflight override --
    hermetic against both earlier tests in this file and any other test
    module that shares these module-level dicts."""
    def _reset():
        for d in (
            tn._tunnel_sockets, tn._tunnel_code_sockets,
            tn._pending_reqs, tn._pending_code_reqs,
            tn._slot_inflight,
        ):
            d.clear()
    _reset()
    yield
    _reset()


class _RecordingWebSocket:
    """Fake WebSocket standing in for a real tunnel client connection.

    ``send_json`` never actually sends anything over a network -- it just
    records the outgoing payload (keyed by the request's own correlation id)
    so the test can decide, independently and on its own schedule, when (and
    with what) to resolve the matching ``pending[req_id]`` future -- exactly
    mimicking "the external MCP server responded now" without a real
    process or socket.
    """

    def __init__(self) -> None:
        self.sent: "list[dict]" = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def _resolve(pending: "dict[str, asyncio.Future]", req_id: str, result: dict) -> None:
    fut = pending.get(req_id)
    if fut is not None and not fut.done():
        fut.set_result(result)


async def _respond_after(
    pending: "dict[str, asyncio.Future]", ws: _RecordingWebSocket, delay: float,
    body: bytes = b"{}",
) -> None:
    """Background helper: wait for `ws` to have sent exactly one request,
    then resolve it after `delay` seconds with a 200 OK envelope."""
    for _ in range(200):
        if ws.sent:
            break
        await asyncio.sleep(0.005)
    assert ws.sent, "no request was sent to respond to"
    req_id = ws.sent[-1]["id"]
    if delay:
        await asyncio.sleep(delay)
    import base64
    _resolve(pending, req_id, {
        "status": 200, "headers": {}, "body": base64.b64encode(body).decode(),
    })


# ---------------------------------------------------------------------------
# Note: "independent concurrent ops on different slots actually run
# concurrently" is already solidly covered by
# tests/test_fault_injection.py::test_hung_tunnel_slot_does_not_block_other_slot
# (a permanently-silent 'fs' slot vs. a healthy 'code' slot returning fast) --
# deliberately not duplicated here. See test_saturated_slow_slot_does_not_delay_a_different_slot
# below for this file's own, non-overlapping combination of that property
# with real (non-synthetic) saturation.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 1. Bounded concurrency: the per-slot semaphore admits exactly N in flight.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bounded_concurrency_caps_a_single_slot(monkeypatch):
    """With _max_slot_inflight=2, three concurrent calls to the SAME slot
    must admit only 2 at once; the 3rd only proceeds once one of the first
    two releases its permit -- proving the bulkhead is a real admission
    control under genuine concurrency, not just a synthetic 0-capacity
    503 case (the only shape test_do_proxy_503_when_slot_saturated covers)."""
    monkeypatch.setattr(tn, "_max_slot_inflight", lambda: 2)
    tenant = "t-bounded"
    ws = _RecordingWebSocket()
    tn._tunnel_sockets[tenant] = ws

    async def _respond_when_n_sent(n: int, delay: float) -> None:
        for _ in range(400):
            if len(ws.sent) >= n:
                break
            await asyncio.sleep(0.005)
        assert len(ws.sent) >= n, f"expected at least {n} requests sent, saw {len(ws.sent)}"
        if delay:
            await asyncio.sleep(delay)
        import base64
        for msg in ws.sent[:n]:
            _resolve(tn._pending_reqs, msg["id"], {
                "status": 200, "headers": {}, "body": base64.b64encode(b"{}").decode(),
            })

    # Resolve the first two admitted requests only after a short hold, giving
    # the test a window to observe "exactly 2 sent, 3rd not yet sent".
    asyncio.ensure_future(_respond_when_n_sent(2, delay=0.15))

    tasks = [
        asyncio.ensure_future(tn._do_proxy(
            tenant, "POST", "/mcp", "", {}, b"x",
            tn._tunnel_sockets, tn._pending_reqs, "fs",
        ))
        for _ in range(3)
    ]

    # Give the two admitted calls time to actually reach ws.send_json, and
    # confirm the 3rd has NOT been sent yet (blocked waiting on the semaphore).
    await asyncio.sleep(0.05)
    assert len(ws.sent) == 2, f"expected exactly 2 in-flight sends, saw {len(ws.sent)}"

    # After the first two resolve and release their permits, the 3rd proceeds.
    for _ in range(400):
        if len(ws.sent) >= 3:
            break
        await asyncio.sleep(0.01)
    assert len(ws.sent) == 3, "3rd call never proceeded after a permit freed up"

    import base64
    _resolve(tn._pending_reqs, ws.sent[2]["id"], {
        "status": 200, "headers": {}, "body": base64.b64encode(b"{}").decode(),
    })

    results = await asyncio.gather(*tasks)
    assert all(r.status_code == 200 for r in results)


# ---------------------------------------------------------------------------
# 2. One slow/failed external slot can't starve an unrelated slot (combined
#    with real, non-synthetic saturation -- see the module-level note above).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_saturated_slow_slot_does_not_delay_a_different_slot(monkeypatch):
    """Slot "fs" is fully saturated (capacity 1, held open indefinitely by a
    call that never resolves within the test) and a SECOND "fs" call is
    queued behind it (fails fast on the acquire timeout). Meanwhile an
    entirely independent "code" call must complete quickly and successfully
    -- the saturated/starved slot's own backlog must never leak into a
    different slot's budget or latency."""
    monkeypatch.setattr(tn, "_max_slot_inflight", lambda: 1)
    monkeypatch.setattr(tn, "_SLOT_ACQUIRE_TIMEOUT", 0.1)
    tenant = "t-starve"
    fs_ws = _RecordingWebSocket()
    code_ws = _RecordingWebSocket()
    tn._tunnel_sockets[tenant] = fs_ws
    tn._tunnel_code_sockets[tenant] = code_ws

    # This call holds the ONE fs permit forever (from this test's point of
    # view) -- never resolved, so it never releases the semaphore.
    holder_task = asyncio.ensure_future(tn._do_proxy(
        tenant, "POST", "/mcp", "", {}, b"hold",
        tn._tunnel_sockets, tn._pending_reqs, "fs", timeout=5.0,
    ))
    await asyncio.sleep(0.03)  # let it actually acquire the permit and send

    # A second fs call queues behind the saturated slot and must fail fast
    # (503) once _SLOT_ACQUIRE_TIMEOUT elapses -- it must NOT hang forever.
    second_fs_task = asyncio.ensure_future(tn._do_proxy(
        tenant, "POST", "/mcp", "", {}, b"queued",
        tn._tunnel_sockets, tn._pending_reqs, "fs",
    ))

    # The independent "code" slot resolves quickly and must be completely
    # unaffected by fs's saturation.
    asyncio.ensure_future(_respond_after(tn._pending_code_reqs, code_ws, delay=0.02))
    start = time.monotonic()
    code_resp = await tn._do_proxy(
        tenant, "POST", "/mcp", "", {}, b"z",
        tn._tunnel_code_sockets, tn._pending_code_reqs, "code",
    )
    code_elapsed = time.monotonic() - start

    assert code_resp.status_code == 200
    assert code_elapsed < 0.3, (
        f"'code' call took {code_elapsed:.2f}s while 'fs' was saturated -- "
        "the starved slot leaked into an unrelated slot's latency"
    )

    second_fs_resp = await second_fs_task
    assert second_fs_resp.status_code == 503
    assert b"saturated" in second_fs_resp.body

    # Clean up the still-pending holder task so it doesn't leak into another
    # test -- resolve it now.
    import base64
    if fs_ws.sent:
        _resolve(tn._pending_reqs, fs_ws.sent[0]["id"], {
            "status": 200, "headers": {}, "body": base64.b64encode(b"{}").decode(),
        })
    await holder_task
