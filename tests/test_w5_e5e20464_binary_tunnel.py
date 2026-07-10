"""Argument-dispatch tests for the ``--tunnel`` CLI entrypoint (e5e20464).

URGENT BUG (v0.1.9): the downloaded ``meridian`` / ``meridian-connect`` binary,
run with ``--tunnel``, started a local web dashboard on port 7700 and never
lazy-spawned any plugin slots — i.e. it behaved like the desktop/server entry
point (``meridian/__main__entry.py``: uvicorn on port 7700) instead of entering
the tunnel client. ``pixi run python -m meridian --tunnel`` from source works.

These tests lock the source-side dispatch in ``meridian/__main__.py``:

  * ``--tunnel`` ALWAYS enters the tunnel client (``run_tunnel``), never the
    dashboard HTTP server (``uvicorn``) — from source AND under a simulated
    frozen binary (``sys.frozen = True``).
  * ``--mcp`` still routes to the MCP stdio server.
  * The bare default (no flag, not frozen) still routes to the dashboard HTTP
    server — source behaviour is unchanged.
  * A frozen binary with NO flag defaults to the tunnel client (so the shipped
    binary can never fall through to the 7700 dashboard), while explicit
    ``--host``/``--port`` or ``MERIDIAN_FROZEN_MODE=server`` still opt into the
    server.

Everything is mocked: no real servers, ports, network, or sleeps. ``run_tunnel``
/ ``build_mcp_server`` / uvicorn / ``_kill_port`` are all replaced with fakes,
and the tunnel path's event loop is driven only over a fast fake coroutine.
"""
from __future__ import annotations

import sys
import types

import pytest

from meridian import __main__ as m


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

class _Recorder:
    """Records which dispatch path was taken and with what kwargs."""

    def __init__(self) -> None:
        self.tunnel_called = False
        self.tunnel_kwargs: dict = {}
        self.mcp_called = False
        self.uvicorn_run_called = False
        self.uvicorn_server_served = False
        self.killed_ports: list[int] = []


@pytest.fixture
def routed(monkeypatch):
    """Neutralise every side-effecting dispatch target and record which fired.

    Patches the *tunnel_client* module functions that ``__main__`` imports
    lazily, plus ``_kill_port`` and a fake ``uvicorn`` module, so no real
    server/port/network is ever touched. Returns a ``_Recorder``.
    """
    rec = _Recorder()

    from meridian import tunnel_client

    async def fake_run_tunnel(**kwargs):
        rec.tunnel_called = True
        rec.tunnel_kwargs = kwargs
        return 0

    def fake_normalize(p):
        return p

    monkeypatch.setattr(tunnel_client, "run_tunnel", fake_run_tunnel)
    monkeypatch.setattr(tunnel_client, "_normalize_path_arg", fake_normalize)

    # never actually probe/kill a port
    monkeypatch.setattr(m, "_kill_port", lambda port: rec.killed_ports.append(port))

    # MCP path: build_mcp_server lives on meridian.server (imported lazily).
    from meridian import server as server_mod

    def fake_build_mcp_server():
        async def fake_run_stdio():
            rec.mcp_called = True
            return None
        return (object(), fake_run_stdio)

    monkeypatch.setattr(server_mod, "build_mcp_server", fake_build_mcp_server)

    # Fake uvicorn so the default (dashboard) path never binds a socket.
    fake_uvicorn = types.ModuleType("uvicorn")

    def fake_uvicorn_run(*args, **kwargs):
        rec.uvicorn_run_called = True

    class _FakeConfig:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _FakeServer:
        def __init__(self, config):
            self.config = config

        async def serve(self):
            rec.uvicorn_server_served = True
            return None

    fake_uvicorn.run = fake_uvicorn_run
    fake_uvicorn.Config = _FakeConfig
    fake_uvicorn.Server = _FakeServer
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    return rec


@pytest.fixture
def not_frozen(monkeypatch):
    """Ensure sys.frozen is absent for the duration of a test."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    yield


@pytest.fixture
def frozen(monkeypatch):
    """Simulate a PyInstaller frozen binary (sys.frozen = True)."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("MERIDIAN_FROZEN_MODE", raising=False)
    yield


# ---------------------------------------------------------------------------
# Source-mode dispatch (not frozen)
# ---------------------------------------------------------------------------

def test_tunnel_flag_routes_to_tunnel_client(routed, not_frozen):
    """--tunnel enters run_tunnel, NOT the dashboard server."""
    rc = m.main(["--tunnel"])
    assert rc == 0
    assert routed.tunnel_called is True
    assert routed.uvicorn_run_called is False
    assert routed.uvicorn_server_served is False


def test_tunnel_flag_forwards_repo_and_port(routed, not_frozen):
    """--tunnel forwards parsed repo/port/token to run_tunnel."""
    rc = m.main(
        ["--tunnel", "--repo", "/r1", "/r2", "--tunnel-port", "8899",
         "--token", "sk_x", "--server", "https://s", "--no-kill"]
    )
    assert rc == 0
    assert routed.tunnel_called is True
    assert routed.tunnel_kwargs["repo_path"] == "/r1"
    assert routed.tunnel_kwargs["extra_fs_roots"] == ["/r2"]
    assert routed.tunnel_kwargs["port"] == 8899
    assert routed.tunnel_kwargs["token"] == "sk_x"
    assert routed.tunnel_kwargs["base_url"] == "https://s"
    # --no-kill: the stale-port loop must not run.
    assert routed.killed_ports == []


def test_tunnel_flag_kills_stale_ports_by_default(routed, not_frozen):
    """Without --no-kill the tunnel path clears stale ports 8808-8813."""
    rc = m.main(["--tunnel"])
    assert rc == 0
    assert routed.killed_ports == list(range(8808, 8814))


def test_mcp_flag_routes_to_mcp_stdio(routed, not_frozen):
    """--mcp routes to the MCP stdio server, not tunnel or dashboard."""
    rc = m.main(["--mcp"])
    assert rc == 0
    assert routed.mcp_called is True
    assert routed.tunnel_called is False
    assert routed.uvicorn_run_called is False
    assert routed.uvicorn_server_served is False


def test_default_from_source_routes_to_dashboard(routed, not_frozen):
    """No flag, not frozen: the HTTP dashboard server runs (unchanged)."""
    rc = m.main([])
    assert rc == 0
    assert routed.tunnel_called is False
    # win32 uses uvicorn.Server().serve(); other platforms uvicorn.run().
    assert routed.uvicorn_run_called or routed.uvicorn_server_served


# ---------------------------------------------------------------------------
# Frozen-binary dispatch (the actual bug surface)
# ---------------------------------------------------------------------------

def test_frozen_tunnel_flag_routes_to_tunnel(routed, frozen):
    """Frozen + --tunnel MUST enter the tunnel client, never the 7700 dashboard."""
    rc = m.main(["--tunnel"])
    assert rc == 0
    assert routed.tunnel_called is True
    assert routed.uvicorn_run_called is False
    assert routed.uvicorn_server_served is False


def test_frozen_no_flag_defaults_to_tunnel_not_dashboard(routed, frozen):
    """THE BUG: a frozen binary with no flag must NOT start the dashboard.

    v0.1.9 fell through to uvicorn-on-7700. Frozen-aware routing now defaults
    the downloadable binary to the tunnel client.
    """
    rc = m.main([])
    assert rc == 0
    assert routed.tunnel_called is True
    assert routed.uvicorn_run_called is False
    assert routed.uvicorn_server_served is False


def test_frozen_mcp_flag_still_routes_to_mcp(routed, frozen):
    """Explicit --mcp is respected even when frozen (not forced to tunnel)."""
    rc = m.main(["--mcp"])
    assert rc == 0
    assert routed.mcp_called is True
    assert routed.tunnel_called is False


def test_frozen_explicit_port_opts_into_server(routed, frozen):
    """Explicit --port is a deliberate opt-in to the HTTP server, even frozen."""
    rc = m.main(["--port", "7700"])
    assert rc == 0
    assert routed.tunnel_called is False
    assert routed.uvicorn_run_called or routed.uvicorn_server_served


def test_frozen_explicit_host_opts_into_server(routed, frozen):
    """Explicit --host is a deliberate opt-in to the HTTP server, even frozen."""
    rc = m.main(["--host", "0.0.0.0"])
    assert rc == 0
    assert routed.tunnel_called is False
    assert routed.uvicorn_run_called or routed.uvicorn_server_served


def test_frozen_mode_server_env_opts_into_server(routed, frozen, monkeypatch):
    """MERIDIAN_FROZEN_MODE=server is the escape hatch for a desktop/server binary."""
    monkeypatch.setenv("MERIDIAN_FROZEN_MODE", "server")
    rc = m.main([])
    assert rc == 0
    assert routed.tunnel_called is False
    assert routed.uvicorn_run_called or routed.uvicorn_server_served


# ---------------------------------------------------------------------------
# Helper-level unit checks
# ---------------------------------------------------------------------------

def test_is_frozen_reflects_sys_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert m._is_frozen() is False
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert m._is_frozen() is True


def test_frozen_default_to_tunnel_is_noop_when_not_frozen(monkeypatch):
    """From source the helper must never rewrite args (no behaviour change)."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    ns = types.SimpleNamespace(tunnel=False, mcp=False)
    m._frozen_default_to_tunnel(ns, [])
    assert ns.tunnel is False


def test_frozen_default_to_tunnel_sets_tunnel_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("MERIDIAN_FROZEN_MODE", raising=False)
    ns = types.SimpleNamespace(tunnel=False, mcp=False)
    m._frozen_default_to_tunnel(ns, [])
    assert ns.tunnel is True


def test_frozen_default_respects_equals_form_port(monkeypatch):
    """--port=7700 (equals form) still opts into the server when frozen."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("MERIDIAN_FROZEN_MODE", raising=False)
    ns = types.SimpleNamespace(tunnel=False, mcp=False)
    m._frozen_default_to_tunnel(ns, ["--port=7700"])
    assert ns.tunnel is False
