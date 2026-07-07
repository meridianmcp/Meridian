"""Regression tests for c8e6b61c — the Desktop Commander (DC) tunnel-slot
respawn storm.

Symptom: "tunnel:dc: spawning proxy … on port 8813" repeating dozens of times
rapidly. Root cause: on Windows DC launches as
``cmd /c npx -y @wonderwhy-er/desktop-commander@latest``. ``cmd /c`` returns the
instant npx hands off to a detached MCP-server grandchild, so the tracked
launcher ``Popen.poll()`` reports *not running* while the real server is still
listening on the port → ``ensure_running`` respawns on every request.

Fix (``meridian/tunnel_client.py``): ``SlotProxy.is_running`` now also treats the
slot as running when the launcher handle has exited but the port is still
accepting connections (a quick ``127.0.0.1:<port>`` socket connect via
``_port_is_open``). Matrix under test — with the launcher handle DEAD:

  * open port   ⇒ is_running True   (no respawn — the server is up)
  * closed port ⇒ is_running False  (really down — respawn is correct)

Unit-level with mocks ONLY: the Popen handle is a stub with a ``poll()`` method
and the port check is monkeypatched. No real subprocess, port, network, or
sleeps.
"""
from __future__ import annotations

import socket

import pytest

from meridian import tunnel_client as tc


# ---------------------------------------------------------------------------
# Fake Popen handles (mirror the style in tests/test_tunnel_client.py)
# ---------------------------------------------------------------------------

class _DeadProc:
    """A launcher handle that has already exited (poll() returns an exit code).

    This is exactly the state the ``cmd /c`` DC launcher lands in immediately
    after npx detaches the real server child.
    """

    returncode = 0

    def poll(self):
        return 0  # exited


class _LiveProc:
    """A launcher handle whose process is still alive (poll() is None)."""

    returncode = None

    def poll(self):
        return None  # still running


def _make_proxy(port: int = 8813, label: str = "dc") -> tc.SlotProxy:
    """A SlotProxy wired like the DC slot — never spawns anything real."""
    return tc.SlotProxy(
        ["cmd", "/c", "npx", "-y", "@wonderwhy-er/desktop-commander@latest"],
        port,
        label,
    )


# ---------------------------------------------------------------------------
# The core matrix: dead launcher handle × port open/closed
# ---------------------------------------------------------------------------

def test_dead_launcher_open_port_is_running_true(monkeypatch):
    """Dead launcher handle + OPEN port ⇒ is_running True (no respawn).

    This is the DC bug: the ``cmd /c`` handle has exited but the detached
    server is still listening. is_running MUST report True so ensure_running
    does not respawn on every request.
    """
    proxy = _make_proxy()
    proxy._proc = _DeadProc()

    seen: list[int] = []

    def fake_port_open(port, *a, **k):
        seen.append(port)
        return True

    monkeypatch.setattr(tc, "_port_is_open", fake_port_open)

    assert proxy.is_running is True
    # The port fallback was actually consulted, on the slot's own port.
    assert seen == [proxy.port]


def test_dead_launcher_closed_port_is_running_false(monkeypatch):
    """Dead launcher handle + CLOSED port ⇒ is_running False (respawn correct)."""
    proxy = _make_proxy()
    proxy._proc = _DeadProc()

    monkeypatch.setattr(tc, "_port_is_open", lambda *a, **k: False)

    assert proxy.is_running is False


def test_live_launcher_short_circuits_without_port_probe(monkeypatch):
    """A live launcher handle ⇒ is_running True WITHOUT touching the port.

    Keeps the healthy hot path a cheap ``poll()`` — no socket call per request.
    """
    proxy = _make_proxy()
    proxy._proc = _LiveProc()

    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("port probe must not run when launcher is alive")

    monkeypatch.setattr(tc, "_port_is_open", _boom)

    assert proxy.is_running is True


def test_never_spawned_is_running_false_without_port_probe(monkeypatch):
    """A brand-new / killed slot (``_proc is None``) is NOT falsely revived.

    is_running must return False WITHOUT probing the port — otherwise an
    unrelated process holding the port could resurrect a slot that was never
    started or was explicitly killed.
    """
    proxy = _make_proxy()
    assert proxy._proc is None

    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("port must not be probed when _proc is None")

    monkeypatch.setattr(tc, "_port_is_open", _boom)

    assert proxy.is_running is False


def test_kill_clears_proc_so_is_running_false(monkeypatch):
    """After kill() the slot reports not-running and does not probe the port.

    kill() nulls ``_proc``; even if some orphan still briefly holds the port we
    must not count a deliberately-killed slot as running (it would never respawn
    on the next request).
    """
    proxy = _make_proxy()
    proxy._proc = _DeadProc()
    # Neutralise the real process-tree teardown — no real PID to kill.
    monkeypatch.setattr(tc, "_terminate_proc_tree", lambda proc: None)

    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("port must not be probed after kill()")

    monkeypatch.setattr(tc, "_port_is_open", _boom)

    proxy.kill()
    assert proxy._proc is None
    assert proxy.is_running is False


# ---------------------------------------------------------------------------
# ensure_running: the actual anti-respawn behaviour end-to-end (mocked spawn)
# ---------------------------------------------------------------------------

def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_ensure_running_no_respawn_when_dead_handle_but_port_open(monkeypatch):
    """The regression itself: dead launcher handle + open port ⇒ NO respawn.

    ensure_running() short-circuits on is_running, so with the port open it must
    not call subprocess.Popen at all (this is what stops the spawn storm).
    """
    proxy = _make_proxy()
    proxy._proc = _DeadProc()

    monkeypatch.setattr(tc, "_port_is_open", lambda *a, **k: True)

    spawned: list[object] = []

    def fake_popen(*a, **k):  # pragma: no cover - must never be called
        spawned.append((a, k))
        raise AssertionError("must not respawn when the server port is open")

    monkeypatch.setattr(tc.subprocess, "Popen", fake_popen)

    _run(proxy.ensure_running())
    assert spawned == []


def test_ensure_running_respawns_when_dead_handle_and_port_closed(monkeypatch):
    """Dead launcher handle + closed port ⇒ ensure_running DOES respawn."""
    proxy = _make_proxy()
    proxy._proc = _DeadProc()

    monkeypatch.setattr(tc, "_port_is_open", lambda *a, **k: False)

    spawned: list[object] = []

    def fake_popen(cmd, env=None, **kwargs):
        spawned.append(cmd)
        return _LiveProc()

    monkeypatch.setattr(tc.subprocess, "Popen", fake_popen)

    async def _instant_sleep(_d):
        return None

    monkeypatch.setattr(tc.asyncio, "sleep", _instant_sleep)

    _run(proxy.ensure_running())
    assert len(spawned) == 1
    assert isinstance(proxy._proc, _LiveProc)


# ---------------------------------------------------------------------------
# _port_is_open — the socket helper (socket monkeypatched; no real network)
# ---------------------------------------------------------------------------

def test_port_is_open_true_on_successful_connect(monkeypatch):
    """A completed connect ⇒ True. socket.create_connection is stubbed."""

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    captured = {}

    def fake_create_connection(addr, timeout=None):
        captured["addr"] = addr
        captured["timeout"] = timeout
        return _FakeConn()

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    assert tc._port_is_open(8813) is True
    assert captured["addr"] == ("127.0.0.1", 8813)
    # A short, bounded timeout keeps this cheap on the request hot path.
    assert captured["timeout"] is not None and captured["timeout"] <= 1.0


def test_port_is_open_false_on_connection_refused(monkeypatch):
    """Connection refused (server down) ⇒ False, never raises."""

    def fake_create_connection(addr, timeout=None):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    assert tc._port_is_open(8813) is False


def test_port_is_open_false_on_timeout(monkeypatch):
    """A socket timeout (filtered/hung port) ⇒ False, never raises."""

    def fake_create_connection(addr, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    assert tc._port_is_open(8813) is False


def test_port_is_open_false_on_unexpected_error(monkeypatch):
    """A non-OSError blip is swallowed too — the probe must never raise."""

    def fake_create_connection(addr, timeout=None):
        raise RuntimeError("weird")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    assert tc._port_is_open(8813) is False


@pytest.mark.parametrize("bad_port", [0, -1, None, "nope"])
def test_port_is_open_false_for_invalid_port(bad_port, monkeypatch):
    """Non-positive / non-numeric ports short-circuit to False (no connect)."""

    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("must not attempt a connect for an invalid port")

    monkeypatch.setattr(socket, "create_connection", _boom)
    assert tc._port_is_open(bad_port) is False
