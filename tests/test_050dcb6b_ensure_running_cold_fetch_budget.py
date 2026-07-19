"""Regression test for 050dcb6b.

BUG: SlotProxy.ensure_running's cold-spawn readiness probe called
_probe_slot_health with a hardcoded (attempts=6, delay=3.0) budget, with zero
awareness of the module-level _COLD_FETCH_SLOTS set / _PREFLIGHT_BUDGET_*
tuples that _preflight_slot already consulted correctly. That mismatch meant
a genuinely cold-fetch slot (meridian-docs' `uvx --from <local-path>` venv
build, confirmed live via tunnel.log — "docs" is a member of
_COLD_FETCH_SLOTS) could get a SHORTER readiness window from ensure_running
than the _PREFLIGHT_BUDGET_COLD_FETCH budget _preflight_slot would apply to
the exact same probe.

FIX: both call sites now derive their (attempts, delay) budget from the same
shared helper, _cold_spawn_budget(label), so they can never drift apart
again.
"""
import asyncio

from unittest.mock import AsyncMock

import meridian.tunnel_client as tc


class _FakeProc:
    """Minimal subprocess.Popen stand-in: alive until .terminate()/.kill()."""

    def __init__(self, cmd=None, *a, **k):
        self.cmd = cmd
        self.pid = 4242
        self._alive = True
        self.returncode = None

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False
        self.returncode = -9


def _patch_spawn(monkeypatch):
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)


def test_cold_spawn_budget_helper_matches_the_constants():
    """_cold_spawn_budget is the single source of truth both call sites share:
    cold-fetch labels get _PREFLIGHT_BUDGET_COLD_FETCH, everything else gets
    _PREFLIGHT_BUDGET_DEFAULT."""
    for label in tc._COLD_FETCH_SLOTS:  # dc, ppt, word, docs, zotero
        assert tc._cold_spawn_budget(label) == tc._PREFLIGHT_BUDGET_COLD_FETCH
    for label in ("fs", "extract", "some-unknown-slot"):
        assert tc._cold_spawn_budget(label) == tc._PREFLIGHT_BUDGET_DEFAULT


def test_ensure_running_uses_cold_fetch_budget_for_docs_slot(monkeypatch):
    """(a) A cold-fetch-labeled SlotProxy (label='docs', the exact slot named in
    the live bug report) gets the LARGER _PREFLIGHT_BUDGET_COLD_FETCH budget on
    its cold-spawn readiness probe, matching what _preflight_slot already gives
    that label."""
    _patch_spawn(monkeypatch)
    calls: list[dict] = []

    async def fake_probe(port, *, attempts=2, delay=3.0):
        calls.append({"port": port, "attempts": attempts, "delay": delay})
        return True

    monkeypatch.setattr(tc, "_probe_slot_health", fake_probe)

    sp = tc.SlotProxy(["proxy", "cmd"], 8820, "docs")
    asyncio.run(sp.ensure_running())

    assert len(calls) == 1
    assert calls[0]["port"] == 8820
    assert (calls[0]["attempts"], calls[0]["delay"]) == tc._PREFLIGHT_BUDGET_COLD_FETCH


def test_ensure_running_uses_default_budget_for_non_cold_fetch_slot(monkeypatch):
    """(b) A non-cold-fetch label (e.g. 'fs') still gets the smaller
    _PREFLIGHT_BUDGET_DEFAULT budget, unchanged from prior behavior — the fix
    must not slow down the common (already-on-disk launcher) path."""
    _patch_spawn(monkeypatch)
    calls: list[dict] = []

    async def fake_probe(port, *, attempts=2, delay=3.0):
        calls.append({"port": port, "attempts": attempts, "delay": delay})
        return True

    monkeypatch.setattr(tc, "_probe_slot_health", fake_probe)

    sp = tc.SlotProxy(["proxy", "cmd"], 8810, "fs")
    asyncio.run(sp.ensure_running())

    assert len(calls) == 1
    assert calls[0]["port"] == 8810
    assert (calls[0]["attempts"], calls[0]["delay"]) == tc._PREFLIGHT_BUDGET_DEFAULT


def test_ensure_running_and_preflight_slot_agree_on_budget_for_same_label(monkeypatch):
    """Cross-check: SlotProxy.ensure_running and _preflight_slot must resolve to
    the IDENTICAL budget for the same label — the exact drift this fix closes.
    Exercised for both a cold-fetch label (docs) and a non-cold-fetch label (fs)."""
    _patch_spawn(monkeypatch)

    for label, port in (("docs", 8821), ("fs", 8811)):
        calls: list[dict] = []

        async def fake_probe(port_, *, attempts=2, delay=3.0):
            calls.append({"attempts": attempts, "delay": delay})
            return True

        monkeypatch.setattr(tc, "_probe_slot_health", fake_probe)
        monkeypatch.setattr(tc, "_report_slot_health", AsyncMock())

        sp = tc.SlotProxy(["proxy", "cmd"], port, label)
        asyncio.run(sp.ensure_running())
        ensure_running_budget = (calls[0]["attempts"], calls[0]["delay"])

        calls.clear()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(tc._preflight_slot(object(), port, label))
        finally:
            loop.close()
        preflight_budget = (calls[0]["attempts"], calls[0]["delay"])

        assert ensure_running_budget == preflight_budget
