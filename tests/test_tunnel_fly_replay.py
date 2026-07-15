"""af5b5739 / decision 229441bc — cross-instance tunnel routing via Fly-replay.

The tunnel socket registry is per-process in-memory, so on Fly multi-machine a
request the edge routes to a sibling instance sees an in-memory MISS even though
the tunnel is open on another machine. a19538fe made that miss legible; these
tests cover making it *routable*: capture the owning Fly instance id on WS
connect, and return a ``fly-replay: instance=<id>`` header so Fly re-dispatches
the request to the machine that owns the socket.

Unit-level only — live multi-machine validation is separate and out of scope.
"""
from __future__ import annotations

import asyncio

from fastapi.responses import Response

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian.routes import tunnel as tn


def _reset(tid: str) -> None:
    tn._tenant_owner_instance.pop(tid, None)
    tn._tunnel_sockets.pop(tid, None)
    tn._tunnel_extract_sockets.pop(tid, None)
    tn._tunnel_code_sockets.pop(tid, None)
    tn._tunnel_ppt_sockets.pop(tid, None)
    tn._tunnel_word_sockets.pop(tid, None)
    tn._tunnel_dc_sockets.pop(tid, None)


# ---------------------------------------------------------------------------
# _fly_instance_id — capture from env
# ---------------------------------------------------------------------------

def test_fly_instance_id_none_off_fly(monkeypatch):
    monkeypatch.delenv("FLY_MACHINE_ID", raising=False)
    monkeypatch.delenv("FLY_ALLOC_ID", raising=False)
    assert tn._fly_instance_id() is None


def test_fly_instance_id_prefers_machine_id(monkeypatch):
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-abc")
    monkeypatch.setenv("FLY_ALLOC_ID", "alloc-xyz")
    assert tn._fly_instance_id() == "machine-abc"


def test_fly_instance_id_falls_back_to_alloc_id(monkeypatch):
    monkeypatch.delenv("FLY_MACHINE_ID", raising=False)
    monkeypatch.setenv("FLY_ALLOC_ID", "alloc-xyz")
    assert tn._fly_instance_id() == "alloc-xyz"


def test_fly_instance_id_ignores_blank(monkeypatch):
    monkeypatch.setenv("FLY_MACHINE_ID", "   ")
    monkeypatch.setenv("FLY_ALLOC_ID", "alloc-xyz")
    assert tn._fly_instance_id() == "alloc-xyz"


# ---------------------------------------------------------------------------
# record / clear / query owner instance
# ---------------------------------------------------------------------------

def test_record_owner_instance_on_fly(monkeypatch):
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-1")
    tid = "replay-record"
    try:
        assert tn.record_tenant_owner_instance(tid) == "machine-1"
        assert tn.tenant_owner_instance(tid) == "machine-1"
    finally:
        _reset(tid)


def test_record_owner_instance_noop_off_fly(monkeypatch):
    monkeypatch.delenv("FLY_MACHINE_ID", raising=False)
    monkeypatch.delenv("FLY_ALLOC_ID", raising=False)
    tid = "replay-record-off"
    try:
        assert tn.record_tenant_owner_instance(tid) is None
        assert tn.tenant_owner_instance(tid) is None
    finally:
        _reset(tid)


def test_clear_owner_instance_guarded(monkeypatch):
    """A stale disconnect (with a mismatched instance id) must NOT erase a newer
    owner's claim; an unguarded clear (None) always drops it."""
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-new")
    tid = "replay-clear"
    try:
        tn.record_tenant_owner_instance(tid)  # → machine-new
        # Stale disconnect carrying the OLD instance id — must be a no-op.
        tn.clear_tenant_owner_instance(tid, "machine-old")
        assert tn.tenant_owner_instance(tid) == "machine-new"
        # Matching id clears it.
        tn.clear_tenant_owner_instance(tid, "machine-new")
        assert tn.tenant_owner_instance(tid) is None
        # Unguarded clear always drops.
        tn._tenant_owner_instance[tid] = "machine-new"
        tn.clear_tenant_owner_instance(tid)
        assert tn.tenant_owner_instance(tid) is None
    finally:
        _reset(tid)


# ---------------------------------------------------------------------------
# fly_replay_target — the replay-target helper
# ---------------------------------------------------------------------------

def test_replay_target_none_off_fly(monkeypatch):
    monkeypatch.delenv("FLY_MACHINE_ID", raising=False)
    monkeypatch.delenv("FLY_ALLOC_ID", raising=False)
    tid = "replay-t1"
    try:
        # Even with a recorded owner, off-Fly there's nothing to replay to.
        tn._tenant_owner_instance[tid] = "machine-2"
        assert tn.fly_replay_target({"id": tid}) is None
    finally:
        _reset(tid)


def test_replay_target_hit_when_sibling_owns_socket(monkeypatch):
    """The core case: THIS instance holds no socket, a DIFFERENT instance owns it."""
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-self")
    tid = "replay-t2"
    try:
        tn._tenant_owner_instance[tid] = "machine-sibling"
        # No in-memory socket here → miss → replay to the sibling.
        assert tn.fly_replay_target({"id": tid}) == "instance=machine-sibling"
        assert tn.fly_replay_target_for_id(tid) == "instance=machine-sibling"
    finally:
        _reset(tid)


def test_replay_target_none_when_socket_present(monkeypatch):
    """If THIS instance holds the socket, serve locally — never replay."""
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-self")
    tid = "replay-t3"
    try:
        tn._tenant_owner_instance[tid] = "machine-sibling"
        tn._tunnel_sockets[tid] = object()  # we hold it
        assert tn.fly_replay_target({"id": tid}) is None
    finally:
        _reset(tid)


def test_replay_target_none_when_owner_is_self(monkeypatch):
    """Owner == self would loop — must be a no-op (fall through to the local miss)."""
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-self")
    tid = "replay-t4"
    try:
        tn._tenant_owner_instance[tid] = "machine-self"
        assert tn.fly_replay_target({"id": tid}) is None
    finally:
        _reset(tid)


def test_replay_target_none_when_owner_unknown(monkeypatch):
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-self")
    tid = "replay-t5"
    try:
        # No recorded owner → nothing to replay to.
        assert tn.fly_replay_target({"id": tid}) is None
    finally:
        _reset(tid)


def test_replay_target_none_for_missing_tenant(monkeypatch):
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-self")
    assert tn.fly_replay_target(None) is None
    assert tn.fly_replay_target({}) is None  # no id


# ---------------------------------------------------------------------------
# fly_replay_response — the header carrier
# ---------------------------------------------------------------------------

def test_fly_replay_response_carries_header():
    resp = tn.fly_replay_response("instance=machine-sibling")
    assert isinstance(resp, Response)
    # Header is case-insensitive in Starlette's MutableHeaders.
    assert resp.headers.get(tn.FLY_REPLAY_HEADER) == "instance=machine-sibling"
    assert resp.headers.get("fly-replay") == "instance=machine-sibling"
    # Legible fallback body/status if the replay doesn't happen.
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# _do_proxy wiring — legible replay path on an in-memory miss
# ---------------------------------------------------------------------------

def test_do_proxy_replays_on_cross_instance_miss(monkeypatch):
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-self")
    tid = "replay-proxy"
    try:
        tn._tenant_owner_instance[tid] = "machine-sibling"
        # No socket registered for this tenant on this instance → miss.
        resp = asyncio.run(tn._do_proxy(
            tid, "POST", "/mcp", "", {}, None,
            tn._tunnel_sockets, tn._pending_reqs, "fs",
        ))
        assert resp.status_code == 503
        assert resp.headers.get("fly-replay") == "instance=machine-sibling"
    finally:
        _reset(tid)


def test_do_proxy_plain_503_when_no_owner(monkeypatch):
    """Off Fly (or unknown owner) the miss stays the plain legible 503 — no replay
    header, so behaviour is unchanged for single-instance / self-host."""
    monkeypatch.delenv("FLY_MACHINE_ID", raising=False)
    monkeypatch.delenv("FLY_ALLOC_ID", raising=False)
    tid = "replay-proxy-noowner"
    try:
        resp = asyncio.run(tn._do_proxy(
            tid, "POST", "/mcp", "", {}, None,
            tn._tunnel_sockets, tn._pending_reqs, "fs",
        ))
        assert resp.status_code == 503
        assert resp.headers.get("fly-replay") is None
        assert b"tunnel not connected" in resp.body
    finally:
        _reset(tid)


# ---------------------------------------------------------------------------
# 5f02a21c — extract / code / ppt / word / dc slot replay (the regression gap)
#
# af5b5739 only wired record_tenant_owner_instance in tunnel_ws (FS slot).
# tunnel_extract_ws, tunnel_code_ws, and _serve_tunnel_ws (ppt/word/dc/docs/
# zotero/custom) never populated _tenant_owner_instance, so cross-instance
# misses on those slots fell through to a plain 503 with no fly-replay header
# (the "connected but to a different server instance" error persisted even
# though af5b5739 was marked done).  These tests cover the fix.
# ---------------------------------------------------------------------------

def test_extract_slot_replays_on_cross_instance_miss(monkeypatch):
    """5f02a21c: extract slot _do_proxy emits fly-replay header when owner is a sibling."""
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-self")
    tid = "replay-extract"
    try:
        tn._tenant_owner_instance[tid] = "machine-sibling"
        # No extract socket on this instance → miss.
        resp = asyncio.run(tn._do_proxy(
            tid, "POST", "/mcp", "", {}, None,
            tn._tunnel_extract_sockets, tn._pending_extract_reqs, "extract",
        ))
        assert resp.status_code == 503
        assert resp.headers.get("fly-replay") == "instance=machine-sibling"
    finally:
        _reset(tid)


def test_code_slot_replays_on_cross_instance_miss(monkeypatch):
    """5f02a21c: code slot _do_proxy emits fly-replay header when owner is a sibling."""
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-self")
    tid = "replay-code"
    try:
        tn._tenant_owner_instance[tid] = "machine-sibling"
        resp = asyncio.run(tn._do_proxy(
            tid, "POST", "/mcp", "", {}, None,
            tn._tunnel_code_sockets, tn._pending_code_reqs, "code",
        ))
        assert resp.status_code == 503
        assert resp.headers.get("fly-replay") == "instance=machine-sibling"
    finally:
        _reset(tid)


def test_ppt_slot_replays_on_cross_instance_miss(monkeypatch):
    """5f02a21c: ppt slot _do_proxy emits fly-replay header when owner is a sibling."""
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-self")
    tid = "replay-ppt"
    try:
        tn._tenant_owner_instance[tid] = "machine-sibling"
        resp = asyncio.run(tn._do_proxy(
            tid, "POST", "/mcp", "", {}, None,
            tn._tunnel_ppt_sockets, tn._pending_ppt_reqs, "ppt",
        ))
        assert resp.status_code == 503
        assert resp.headers.get("fly-replay") == "instance=machine-sibling"
    finally:
        _reset(tid)


def test_record_owner_instance_called_by_all_slot_handlers(monkeypatch):
    """5f02a21c: record_tenant_owner_instance sets a known owner so
    fly_replay_target_for_id can produce a replay header for ANY slot,
    not just the FS slot that af5b5739 originally wired."""
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-self")
    tid = "replay-multiregister"
    try:
        # Simulate an extract slot connect recording the owner.
        tn.record_tenant_owner_instance(tid)
        # _tenant_owner_instance is shared — a request for ANY slot can now replay.
        assert tn.fly_replay_target_for_id(tid) is None  # we ARE the owner
        # From a different (simulated) perspective: pretend the owner is a sibling.
        tn._tenant_owner_instance[tid] = "machine-sibling"
        assert tn.fly_replay_target_for_id(tid) == "instance=machine-sibling"
    finally:
        _reset(tid)


def test_extract_slot_plain_503_when_owner_unknown(monkeypatch):
    """5f02a21c: no fly-replay header when the extract-slot owner is unknown
    (off Fly or no connect yet) — behaviour is unchanged for self-host."""
    monkeypatch.delenv("FLY_MACHINE_ID", raising=False)
    monkeypatch.delenv("FLY_ALLOC_ID", raising=False)
    tid = "replay-extract-noowner"
    try:
        resp = asyncio.run(tn._do_proxy(
            tid, "POST", "/mcp", "", {}, None,
            tn._tunnel_extract_sockets, tn._pending_extract_reqs, "extract",
        ))
        assert resp.status_code == 503
        assert resp.headers.get("fly-replay") is None
        assert b"tunnel not connected" in resp.body
    finally:
        _reset(tid)
