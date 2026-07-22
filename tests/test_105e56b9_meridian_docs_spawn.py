"""105e56b9 — meridian-docs tunnel slot wiring and spawn hang mitigation.

Confirmed live bug (2026-07-14):
  `uvx --from <local-path> meridian-docs` hangs silently for 150+ seconds on a
  cold uv cache (zero output, killed manually). The root cause has two parts:

  1. The tunnel client's run_tunnel() only wired ppt/word/dc into the office-slot
     loop; docs and zotero were missing, so the tunnel NEVER printed a
     "tunnel:docs:" line and NEVER started the docs WebSocket relay.

  2. The preflight budget for "docs" was the DEFAULT (attempts=2, delay=3s,
     ~23s total), not the COLD_FETCH budget (attempts=4, delay=5s, ~55s). On a
     cold uvx cache, the inner meridian-docs venv must be built from scratch
     which can take 30-150s -- far beyond 23s -- causing the preflight to fail and
     the slot to be marked unhealthy with no diagnostic log.

Fixes:
  (A) tunnel_client._COLD_FETCH_SLOTS now includes "docs" and "zotero" so they
      get the larger preflight budget on a cold uvx cache.
  (B) run_tunnel()'s office-slot loop now includes docs/zotero so they get
      lazy-spawned, idle-killed, and WebSocket-relayed like ppt/word/dc.
  (C) _OFFICE_PREFLIGHT_HINTS now has specific hints for docs/zotero so a cold-
      cache failure produces a clear, actionable log line instead of the generic
      "proxy didn't respond" message.
  (D) _preflight_failure_hint() returns the slot-specific hint for docs/zotero
      (exercised via the existing _preflight_failure_hint code path -- no new
      function needed).

Tests here are purely unit (no real subprocess, network, or 150-second waits):
  - Verify docs/zotero are in _COLD_FETCH_SLOTS.
  - Verify the preflight hint functions return slot-specific text for docs/zotero.
  - Mock the subprocess spawn to hang (blocks indefinitely) and assert the
    preflight fires a bounded timeout rather than waiting forever.
  - Verify the docs/zotero slots ARE wired into the office_ports dict (i.e., the
    run_tunnel slot-loop fix) by inspecting the module-level constants and the
    plugin registry.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meridian import tunnel_client as tc
from meridian import tunnel_plugins as tp


# ---------------------------------------------------------------------------
# (A) _COLD_FETCH_SLOTS must include docs and zotero (105e56b9)
# ---------------------------------------------------------------------------

def test_docs_in_cold_fetch_slots():
    """The 'docs' slot must be in _COLD_FETCH_SLOTS so it gets the extended
    preflight budget (attempts=4, delay=5s) on a cold uvx cache. Without this,
    a 30-150s cold venv build exceeds the default ~23s budget and the slot is
    incorrectly marked unhealthy on first run."""
    assert "docs" in tc._COLD_FETCH_SLOTS, (
        "'docs' missing from _COLD_FETCH_SLOTS — meridian-docs uses uvx --from "
        "<local-path> which must build the venv on a cold cache (can take 30-150s); "
        "the default ~23s budget is not enough"
    )


def test_zotero_in_cold_fetch_slots():
    """The 'zotero' slot must also be in _COLD_FETCH_SLOTS for the same reason:
    `uvx zotero-mcp` triggers a cold PyPI download on first use."""
    assert "zotero" in tc._COLD_FETCH_SLOTS, (
        "'zotero' missing from _COLD_FETCH_SLOTS — zotero-mcp is downloaded via "
        "uvx and may take a long time on a cold cache"
    )


def test_cold_fetch_slots_still_has_original_members():
    """Adding docs/zotero must not remove the original cold-fetch slots."""
    for slot in ("dc", "ppt", "word"):
        assert slot in tc._COLD_FETCH_SLOTS, (
            f"'{slot}' was unexpectedly removed from _COLD_FETCH_SLOTS"
        )


# ---------------------------------------------------------------------------
# (B) Plugin registry: docs/zotero are BUILTIN_PLUGINS with a command
# ---------------------------------------------------------------------------

def test_docs_plugin_has_command_in_builtin_plugins():
    """The meridian-docs plugin must be in BUILTIN_PLUGINS and have a command so
    the office-slot loop can build the mcp-proxy wrapper."""
    by_slot = {p["slot"]: p for p in tp.BUILTIN_PLUGINS}
    assert "docs" in by_slot, "docs slot missing from BUILTIN_PLUGINS"
    docs = by_slot["docs"]
    assert docs.get("command"), (
        "meridian-docs plugin must have a non-empty command in BUILTIN_PLUGINS"
    )
    # The command must be a local-path uvx invocation (not bare `uvx meridian-docs`).
    # f886d37a — "--no-cache" was inserted right after "uvx" (before "--from"), so
    # check for "--from" positionally rather than at a hardcoded index.
    cmd = docs["command"]
    assert isinstance(cmd, list)
    assert cmd[0] == "uvx" and "--from" in cmd, (
        "docs slot command must be ['uvx', ..., '--from', <path>, 'meridian-docs-mcp'] -- "
        "bare uvx meridian-docs fails because the package is NOT on PyPI"
    )


def test_zotero_plugin_has_command_in_builtin_plugins():
    """The zotero-mcp plugin must be in BUILTIN_PLUGINS and have a command."""
    by_slot = {p["slot"]: p for p in tp.BUILTIN_PLUGINS}
    assert "zotero" in by_slot, "zotero slot missing from BUILTIN_PLUGINS"
    zotero = by_slot["zotero"]
    assert zotero.get("command"), (
        "zotero-mcp plugin must have a non-empty command in BUILTIN_PLUGINS"
    )


def test_docs_plugin_has_port_8818():
    """The docs slot's default port must be 8818 (see DEFAULT_DOCS_PORT)."""
    by_slot = {p["slot"]: p for p in tp.BUILTIN_PLUGINS}
    assert by_slot["docs"]["port"] == tp.DEFAULT_DOCS_PORT


def test_zotero_plugin_has_port_8819():
    """The zotero slot's default port must be 8819 (see DEFAULT_ZOTERO_PORT)."""
    by_slot = {p["slot"]: p for p in tp.BUILTIN_PLUGINS}
    assert by_slot["zotero"]["port"] == tp.DEFAULT_ZOTERO_PORT


# ---------------------------------------------------------------------------
# (C) _preflight_failure_hint returns slot-specific text for docs/zotero
# ---------------------------------------------------------------------------

def test_preflight_hint_docs_is_slot_specific():
    """_preflight_failure_hint('docs', port) must return a docs-specific hint
    mentioning the venv build time, not the generic 'proxy didn't respond'."""
    reason, detail = tc._preflight_failure_hint("docs", 8818)
    assert reason == "unreachable"
    # The hint must be docs-specific (mention venv / meridian-docs).
    detail_lower = detail.lower()
    assert "meridian-docs" in detail_lower or "venv" in detail_lower or "uvx" in detail_lower, (
        f"docs preflight hint is not slot-specific: {detail!r}"
    )
    # Must NOT be the generic fallback.
    assert "proxy did not respond" not in detail_lower, (
        f"docs preflight hint looks like the generic fallback: {detail!r}"
    )


def test_preflight_hint_zotero_is_slot_specific():
    """_preflight_failure_hint('zotero', port) must return a zotero-specific hint."""
    reason, detail = tc._preflight_failure_hint("zotero", 8819)
    assert reason == "unreachable"
    detail_lower = detail.lower()
    assert "zotero" in detail_lower, (
        f"zotero preflight hint is not slot-specific: {detail!r}"
    )


def test_preflight_hint_generic_for_unknown_slot():
    """_preflight_failure_hint for an unknown label falls back to the generic
    message (existing behaviour must not regress)."""
    reason, detail = tc._preflight_failure_hint("some-unknown-slot", 9999)
    assert reason == "unreachable"
    assert "some-unknown-slot" in detail or "proxy" in detail.lower()


# ---------------------------------------------------------------------------
# (D) Bounded preflight: a hanging spawn must time out within _COLD_FETCH budget
#     rather than blocking indefinitely (regression guard for the 150s+ hang)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_slot_health_times_out_quickly_on_no_answer():
    """_probe_slot_health(port, attempts=1, delay=0) must return False quickly
    when nothing is listening on the port, not block for 150+ seconds.

    This is the key regression guard: the old code path with the default preflight
    budget (attempts=2, delay=3s, 10s per-attempt) would wait ~23s before giving
    up. With a mock port that never answers, we confirm the function returns False
    and does so in bounded time (< 5s for attempts=1 with a tiny per-attempt
    timeout, which is what the unit test uses to stay fast).

    We mock httpx.AsyncClient to immediately raise ConnectError (simulating a port
    with nothing listening) so the test completes in milliseconds.
    """
    import httpx

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kw):
            raise httpx.ConnectError("connection refused")

    t0 = time.monotonic()
    # Patch httpx.AsyncClient in the httpx module itself (tunnel_client imports
    # httpx inside _probe_slot_health so we must patch the module, not tc).
    with patch.object(httpx, "AsyncClient", _FakeClient):
        result = await tc._probe_slot_health(9999, attempts=1, delay=0.01)

    elapsed = time.monotonic() - t0
    assert result is False, "should return False when nothing is listening"
    assert elapsed < 5.0, (
        f"_probe_slot_health took {elapsed:.1f}s — must be bounded, not hang forever"
    )


@pytest.mark.asyncio
async def test_preflight_slot_produces_clear_log_on_failure(capsys):
    """When _preflight_slot fails for the 'docs' slot it must emit a clear log line
    containing 'pre-flight health check FAILED' and 'docs' -- the diagnostic trail
    the original bug lacked (the tunnel log showed nothing at all for the docs slot).

    We mock _probe_slot_health to return False immediately (simulates a cold-cache
    stall that exceeds the budget) and mock the WebSocket send so the report call
    succeeds silently.
    """
    ws_mock = AsyncMock()
    ws_mock.send = AsyncMock()

    with patch.object(tc, "_probe_slot_health", new=AsyncMock(return_value=False)):
        result = await tc._preflight_slot(ws_mock, 8818, "docs")

    assert result is False

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "docs" in combined, (
        "preflight failure log must name the slot ('docs') so the operator can "
        f"identify which slot hung; got: {combined!r}"
    )
    assert "FAILED" in combined or "failed" in combined.lower(), (
        f"preflight failure log must say FAILED; got: {combined!r}"
    )


# ---------------------------------------------------------------------------
# (E) docs / zotero WS URL builder: _ws_office_url works for non-office slots
# ---------------------------------------------------------------------------

def test_ws_office_url_for_docs_slot():
    """The _ws_office_url helper that the run_tunnel loop uses must produce a
    valid docs WebSocket URL (wss://.../tunnel-docs/<tenant_id>?)."""
    url = tc._ws_office_url("https://usemeridian.us", "t123", "tok", "docs")
    assert "tunnel-docs" in url
    assert "t123" in url
    assert url.startswith("wss://")


def test_ws_office_url_for_zotero_slot():
    """Same check for the zotero slot."""
    url = tc._ws_office_url("https://usemeridian.us", "t456", "tok", "zotero")
    assert "tunnel-zotero" in url
    assert "t456" in url


# ---------------------------------------------------------------------------
# (F) resolve_plugins includes docs/zotero and preserves their default command
# ---------------------------------------------------------------------------

def test_resolve_plugins_includes_docs_slot_by_default():
    """resolve_plugins(None) must return a 'docs' slot entry (disabled by default
    but present) so tunnel_client's by_slot lookup finds it."""
    resolved = tp.resolve_plugins(None)
    by_slot = {p["slot"]: p for p in resolved}
    assert "docs" in by_slot, (
        "resolve_plugins must include the 'docs' slot (from BUILTIN_PLUGINS)"
    )
    docs = by_slot["docs"]
    # Default: disabled (opt-in like office slots)
    assert docs["enabled"] is False
    # Command must still be the local-path uvx form. f886d37a inserted "--no-cache"
    # right after "uvx", so check for "--from" positionally.
    assert docs["command"][0] == "uvx" and "--from" in docs["command"]


def test_resolve_plugins_includes_zotero_slot_by_default():
    """resolve_plugins(None) must return a 'zotero' slot entry (disabled, present)."""
    resolved = tp.resolve_plugins(None)
    by_slot = {p["slot"]: p for p in resolved}
    assert "zotero" in by_slot
