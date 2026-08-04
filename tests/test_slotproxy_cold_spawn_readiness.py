"""Regression test for e75f4fc4 — SlotProxy.ensure_running's cold-spawn readiness.

GROUND-TRUTH FINDING (direct code read of meridian/tunnel_client.py): the lazy
respawn path (SlotProxy.ensure_running, fired on the first request after a
30-minute idle-kill or on first-ever use) waited a BLIND asyncio.sleep(1.0)
before considering the proxy ready — long enough for a warm restart, not for a
genuinely cold npx/uvx package-resolution + server-boot. That gap let the first
real request after a cold spawn hit an unready proxy and hang until mcp-proxy's
own internal ~60s timeout (-32001), which the 1s pause did nothing to prevent.

_probe_slot_health already existed and is proven for exactly this "is the proxy
actually answering tools/list yet" check — it already backs the REACTIVE
post-timeout watchdog (3bde892a). The fix reuses it in ensure_running's cold-
spawn path too, instead of a blind sleep, so the FIRST attempt gets the same
readiness guarantee the reactive recovery path already had.
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


def test_ensure_running_calls_probe_slot_health_not_blind_sleep(monkeypatch):
    """The cold-spawn path must consult _probe_slot_health (real readiness),
    not just wait a fixed duration and assume success."""
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    calls = []

    async def fake_probe(port, *, attempts=2, delay=3.0):
        calls.append({"port": port, "attempts": attempts, "delay": delay})
        return True

    monkeypatch.setattr(tc, "_probe_slot_health", fake_probe)

    sp = tc.SlotProxy(["proxy", "cmd"], 8813, "dc")
    asyncio.run(sp.ensure_running())

    assert len(calls) == 1
    assert calls[0]["port"] == 8813
    # A generous window — enough for a real cold npx/uvx resolve, not a blind 1s.
    assert calls[0]["attempts"] >= 4
    assert calls[0]["delay"] >= 2.0


def test_ensure_running_succeeds_quietly_when_probe_healthy(monkeypatch, capsys):
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=True))

    sp = tc.SlotProxy(["proxy", "cmd"], 8813, "dc")
    asyncio.run(sp.ensure_running())

    assert sp.is_running
    out = capsys.readouterr().out
    assert "did not answer" not in out


def test_ensure_running_warns_but_does_not_raise_when_probe_unhealthy(monkeypatch, capsys):
    """A cold spawn that never answers within the readiness window must not
    raise (matches the prior no-raise behavior) but MUST log a diagnosable
    warning instead of silently pretending the proxy is ready."""
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=False))

    sp = tc.SlotProxy(["proxy", "cmd"], 8813, "dc")
    asyncio.run(sp.ensure_running())  # must not raise

    out = capsys.readouterr().out
    assert "did not answer" in out
    assert "dc" in out


def test_ensure_running_touches_last_used_regardless_of_probe_outcome(monkeypatch):
    """touch() must fire whether the probe succeeds or fails — a request is
    about to be attempted against the slot either way, so idle-tracking must
    reflect that (matches prior behavior of the unconditional sleep+touch)."""
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    for outcome in (True, False):
        monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=outcome))
        sp = tc.SlotProxy(["proxy", "cmd"], 8813, "dc")
        assert sp.idle_seconds() == 0.0
        asyncio.run(sp.ensure_running())
        assert sp._last_used != 0.0


def test_ensure_running_still_noop_when_already_running(monkeypatch):
    """Second call while the proxy is already running must not re-probe —
    matches the existing is_running short-circuit at the top of ensure_running."""
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(tc, "_probe_slot_health", probe)

    sp = tc.SlotProxy(["proxy", "cmd"], 8813, "dc")
    asyncio.run(sp.ensure_running())
    assert probe.call_count == 1
    asyncio.run(sp.ensure_running())
    assert probe.call_count == 1  # no second probe — already running


# ---------------------------------------------------------------------------
# ddd46cc8 — unified slot lifecycle diagnostics: ensure_running must classify
# the cold-spawn readiness OUTCOME onto SlotProxy.diagnostics (HEALTHY /
# STARTUP_TIMEOUT), and a startup-timeout classification must stay TRANSIENT
# (never immediately quarantine-eligible) since a cold cache can legitimately
# still be resolving.
# ---------------------------------------------------------------------------

def test_ensure_running_marks_diagnostics_healthy_on_successful_probe(monkeypatch):
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=True))

    sp = tc.SlotProxy(["proxy", "cmd"], 8813, "dc")
    assert sp.diagnostics.state is tc.SlotState.CONFIGURED
    asyncio.run(sp.ensure_running())

    assert sp.diagnostics.state is tc.SlotState.HEALTHY
    assert sp.diagnostics.last_healthy_at is not None
    assert sp.diagnostics.retry_count == 0
    assert sp.diagnostics.quarantine_reason is None


def test_ensure_running_marks_diagnostics_startup_timeout_on_failed_probe(monkeypatch):
    """A cold-spawn probe that never answers is STARTUP_TIMEOUT — a TRANSIENT
    state (in tc._TRANSIENT_STATES, never in tc._DETERMINISTIC_STATES) since a
    cold uvx/npx cache may simply still be resolving in the background."""
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=False))

    sp = tc.SlotProxy(["proxy", "cmd"], 8813, "dc")
    asyncio.run(sp.ensure_running())

    assert sp.diagnostics.state is tc.SlotState.STARTUP_TIMEOUT
    assert sp.diagnostics.state in tc._TRANSIENT_STATES
    assert sp.diagnostics.state not in tc._DETERMINISTIC_STATES
    assert sp.diagnostics.root_cause
    assert sp.diagnostics.retry_count == 1


def test_ensure_running_startup_timeout_then_recovery_resets_diagnostics(monkeypatch):
    """A slot that misses its readiness window once but comes up on the NEXT
    ensure_running() call (post idle-kill respawn, say) fully recovers —
    retry_count/quarantine bookkeeping resets to a clean HEALTHY state."""
    monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    probe = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(tc, "_probe_slot_health", probe)

    sp = tc.SlotProxy(["proxy", "cmd"], 8813, "dc")
    asyncio.run(sp.ensure_running())
    assert sp.diagnostics.state is tc.SlotState.STARTUP_TIMEOUT
    assert sp.diagnostics.retry_count == 1

    # Force a fresh spawn attempt (idle-kill would normally do this).
    sp.kill(reason="idle_killed")
    assert sp.diagnostics.state is tc.SlotState.IDLE_KILLED
    asyncio.run(sp.ensure_running())

    assert sp.diagnostics.state is tc.SlotState.HEALTHY
    assert sp.diagnostics.retry_count == 0
    assert sp.diagnostics.quarantine_reason is None


def test_ensure_running_dependency_missing_launch_failure_is_deterministic(monkeypatch):
    """A launcher binary/interpreter that's missing (FileNotFoundError from
    Popen — the confirmed pattern for a missing runtime dependency) classifies
    as DEPENDENCY_MISSING, a DETERMINISTIC state, not the transient
    STARTUP_TIMEOUT the probe-timeout case above gets."""
    def boom(cmd, *a, **k):
        raise FileNotFoundError("no such file: python")
    monkeypatch.setattr(tc.subprocess, "Popen", boom)

    sp = tc.SlotProxy(["python", "-m", "some_missing_connector"], 8813, "docs")
    asyncio.run(sp.ensure_running())  # must not raise

    assert sp.diagnostics.state is tc.SlotState.DEPENDENCY_MISSING
    assert sp.diagnostics.state in tc._DETERMINISTIC_STATES
    assert "python" in sp.diagnostics.root_cause
    assert not sp.is_running


def test_ensure_running_generic_launch_exception_is_child_crashed(monkeypatch):
    """A launch failure that ISN'T a missing-executable signature (e.g. a
    generic OSError) still classifies deterministically, but as the more
    generic CHILD_CRASHED rather than the specific DEPENDENCY_MISSING."""
    def boom(cmd, *a, **k):
        raise OSError("some other launch failure")
    monkeypatch.setattr(tc.subprocess, "Popen", boom)

    sp = tc.SlotProxy(["some", "cmd"], 8813, "dc")
    asyncio.run(sp.ensure_running())

    assert sp.diagnostics.state is tc.SlotState.CHILD_CRASHED
    assert sp.diagnostics.state in tc._DETERMINISTIC_STATES


# ---------------------------------------------------------------------------
# e0f7bd72 — filesystem mcp-proxy cold spawn with an incomplete npx cache
# (missing npm-cache/_npx/.../node_modules/ajv/dist/ajv.js). End-to-end
# through SlotProxy.ensure_running(), covering both outcomes of the
# repair/retry: cache-clear genuinely fixes it (recovers to HEALTHY) and
# cache-clear does NOT fix it (stays DEPENDENCY_MISSING — never claimed
# ready off the back of a process merely staying alive).
# ---------------------------------------------------------------------------

class _FakeDeadProc:
    """A proc that has already exited by the time poll() is first checked —
    simulates the cold-spawn's first Popen dying on a missing dependency
    file before it can ever answer tools/list."""
    def __init__(self, cmd=None, *a, **k):
        self.cmd = cmd
        self.pid = 4243
        self.returncode = 1

    def poll(self):
        return 1

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 1

    def kill(self):
        pass


_FS_AJV_DETAIL = {
    "missing_file": "C:\\npm-cache\\_npx\\abc123\\node_modules\\ajv\\dist\\ajv.js",
    "cache_path": "C:\\npm-cache\\_npx\\abc123",
    "package": "ajv",
    "stderr": "Error: Cannot find module '...ajv.js'\n  code: 'MODULE_NOT_FOUND'\n",
}


def _patch_dependency_integrity_repair(monkeypatch, spawn_calls):
    """Shared plumbing for the two dependency-integrity-repair tests below:
    the first Popen dies immediately with the ajv.js signature, every
    subsequent Popen stays alive (the retry's process-level outcome — real
    *readiness* is controlled separately via _probe_slot_health)."""
    def _popen(cmd, *a, **k):
        spawn_calls.append(cmd)
        if len(spawn_calls) == 1:
            return _FakeDeadProc(cmd)
        return _FakeProc(cmd)

    monkeypatch.setattr(tc.subprocess, "Popen", _popen)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_probe_tar_entry_error", lambda *a, **k: False)
    monkeypatch.setattr(tc, "_probe_fast_exit_stderr", lambda *a, **k: None)
    monkeypatch.setattr(
        tc, "_probe_missing_dependency_file",
        lambda *a, **k: dict(_FS_AJV_DETAIL),
    )
    _thorough_called = []
    monkeypatch.setattr(
        tc, "_scoped_cache_clear_thorough",
        lambda cmd, label="": _thorough_called.append(cmd),
    )
    return _thorough_called


def test_ensure_running_dependency_missing_cache_repair_recovers_to_healthy(monkeypatch):
    """When the thorough cache clear + retry genuinely fixes the cache (the
    retried process comes up AND answers tools/list), the slot recovers to
    HEALTHY exactly like any other successful cold-spawn — the repair path
    is not a dead end."""
    spawn_calls = []
    thorough_called = _patch_dependency_integrity_repair(monkeypatch, spawn_calls)
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=True))

    sp = tc.SlotProxy(["npx", "-y", "mcp-proxy", "--", "npx", "-y",
                        "@modelcontextprotocol/server-filesystem"], 8814, "fs")
    asyncio.run(sp.ensure_running())

    assert len(spawn_calls) == 2, "must retry exactly once after the cache repair"
    assert len(thorough_called) == 1
    assert sp.diagnostics.state is tc.SlotState.HEALTHY
    assert sp.diagnostics.last_healthy_at is not None


def test_ensure_running_dependency_missing_cache_repair_never_claims_ready_if_still_broken(
    monkeypatch,
):
    """When the repair does NOT fix it (retried process starts but still
    never answers tools/list — e.g. the underlying corruption needs a human
    to look at disk space / AV exclusions), the slot must stay classified as
    DEPENDENCY_MISSING (deterministic) and must NEVER be reported HEALTHY —
    this is the 'without claiming the slot is ready' contract the item asks
    for. dependency_missing/root_cause still carry the package + cache path
    for the operator to act on."""
    spawn_calls = []
    thorough_called = _patch_dependency_integrity_repair(monkeypatch, spawn_calls)
    monkeypatch.setattr(tc, "_probe_slot_health", AsyncMock(return_value=False))

    sp = tc.SlotProxy(["npx", "-y", "mcp-proxy", "--", "npx", "-y",
                        "@modelcontextprotocol/server-filesystem"], 8815, "fs")
    asyncio.run(sp.ensure_running())

    assert len(spawn_calls) == 2
    assert len(thorough_called) == 1
    assert sp.diagnostics.state is tc.SlotState.DEPENDENCY_MISSING
    assert sp.diagnostics.state in tc._DETERMINISTIC_STATES
    assert sp.diagnostics.state is not tc.SlotState.HEALTHY
    assert sp.diagnostics.dependency_missing == "ajv"
    assert "ajv" in sp.diagnostics.root_cause
    assert "cache path" in sp.diagnostics.root_cause
