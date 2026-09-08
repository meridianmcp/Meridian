"""Tests for the slim tunnel-client entry point (meridian/tunnel_main.py).

This module is the PyInstaller entry point for the downloadable ``meridian``
binary. Its whole purpose is to stay small: importing it must NOT drag in the
server stack (FastAPI / uvicorn / psycopg / langgraph / anthropic). These tests
lock that in so a future import added at module scope can't silently re-bloat
the binary.

The "not in sys.modules" check runs in a fresh subprocess because the pytest
process itself imports fastapi/uvicorn elsewhere in the suite — only a clean
interpreter can prove the entry module doesn't pull them in.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from meridian import tunnel_main


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------

def test_main_is_callable():
    assert callable(tunnel_main.main)


def test_build_parser_defaults():
    parser = tunnel_main._build_parser()
    args = parser.parse_args([])
    assert args.token is None
    assert args.server is None
    assert args.repo is None
    assert args.tunnel_port == 8808
    assert args.code_dirs is None


def test_build_parser_accepts_tunnel_flags():
    parser = tunnel_main._build_parser()
    args = parser.parse_args(
        [
            "--tunnel",
            "--token", "sk_meridian_x",
            "--server", "https://example.test",
            "--repo", "/tmp/repo",
            "--tunnel-port", "9000",
            "--code-dir", "/a",
            "--code-dir", "/b",
        ]
    )
    assert args.tunnel is True
    assert args.token == "sk_meridian_x"
    assert args.server == "https://example.test"
    # cbbd0eb4 — --repo is nargs='+', so a single path is a one-element list.
    assert args.repo == ["/tmp/repo"]
    assert args.tunnel_port == 9000
    assert args.code_dirs == ["/a", "/b"]


def test_build_parser_repo_accepts_multiple_paths():
    """cbbd0eb4 — --repo takes multiple paths."""
    parser = tunnel_main._build_parser()
    args = parser.parse_args(["--repo", "/a", "/b", "/c"])
    assert args.repo == ["/a", "/b", "/c"]


def test_build_parser_no_kill_flag():
    """a887155d — the binary accepts --no-kill for CLI parity (default False)."""
    parser = tunnel_main._build_parser()
    assert parser.parse_args([]).no_kill is False
    assert parser.parse_args(["--tunnel", "--no-kill"]).no_kill is True


def test_main_invokes_run_tunnel(monkeypatch):
    """main() should forward parsed args to run_tunnel and return its code."""
    captured = {}

    async def fake_run_tunnel(*, token, base_url, repo_path, extra_fs_roots, port, code_dirs):
        captured.update(
            token=token,
            base_url=base_url,
            repo_path=repo_path,
            extra_fs_roots=extra_fs_roots,
            port=port,
            code_dirs=code_dirs,
        )
        return 0

    from meridian import tunnel_client

    monkeypatch.setattr(tunnel_client, "run_tunnel", fake_run_tunnel)

    rc = tunnel_main.main(
        ["--token", "sk_t", "--server", "https://s", "--repo", "/r", "--tunnel-port", "8888"]
    )
    assert rc == 0
    assert captured == {
        "token": "sk_t",
        "base_url": "https://s",
        "repo_path": "/r",
        "extra_fs_roots": [],
        "port": 8888,
        "code_dirs": None,
    }


def test_main_repo_multipath_splits_first_vs_rest(monkeypatch):
    """cbbd0eb4 — first --repo path is repo_path, the rest are extra fs roots."""
    captured = {}

    async def fake_run_tunnel(*, token, base_url, repo_path, extra_fs_roots, port, code_dirs):
        captured.update(repo_path=repo_path, extra_fs_roots=extra_fs_roots)
        return 0

    from meridian import tunnel_client
    monkeypatch.setattr(tunnel_client, "run_tunnel", fake_run_tunnel)

    rc = tunnel_main.main(["--repo", "/first", "/second", "/third"])
    assert rc == 0
    assert captured["repo_path"] == "/first"
    assert captured["extra_fs_roots"] == ["/second", "/third"]


def test_resolve_loop_reuses_open_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        assert tunnel_main._resolve_loop() is loop
    finally:
        loop.close()


def test_resolve_loop_replaces_closed_loop(monkeypatch):
    """A closed/missing current loop should be replaced with a fresh usable one."""
    class _DummyPolicy:
        def get_event_loop(self):
            raise RuntimeError("no current event loop")

    monkeypatch.setattr(asyncio, "get_event_loop_policy", lambda: _DummyPolicy())
    loop = tunnel_main._resolve_loop()
    try:
        assert not loop.is_closed()
        # Sanity: the resolved loop actually runs coroutines.
        assert loop.run_until_complete(asyncio.sleep(0)) is None
    finally:
        loop.close()


def test_main_handles_keyboard_interrupt(monkeypatch):
    async def boom(**_kwargs):
        raise KeyboardInterrupt

    from meridian import tunnel_client

    monkeypatch.setattr(tunnel_client, "run_tunnel", boom)
    assert tunnel_main.main([]) == 0


# ---------------------------------------------------------------------------
# 7b457c55 — Windows event-loop/subprocess compatibility regression test.
#
# Confirmed live bug (docs/meridian-local-runner-tunnel-investigation-
# 2026-08-31.md): this module used to force WindowsSelectorEventLoopPolicy
# at import time. tunnel_client._handle_run_cmd (used by run_verification's
# run_cmd control message) spawns children via
# asyncio.create_subprocess_exec/_shell, which raise a bare, message-less
# NotImplementedError on a Windows SelectorEventLoop — they require a
# Proactor-compatible loop. meridian/__main__.py's own --tunnel dispatch
# already carries the equivalent fix; this was the one entry point (the
# frozen meridian.exe binary) that never got it mirrored.
# ---------------------------------------------------------------------------


def test_resolve_loop_does_not_force_selector_event_loop_on_windows():
    """_resolve_loop() must not hand back a SelectorEventLoop on Windows —
    that is exactly the loop type that breaks asyncio subprocess spawning
    (see module comment above). Only meaningful on Windows; elsewhere
    Selector/Proactor is not a real distinction.

    _resolve_loop()'s own logic never touches the event-loop POLICY at all
    (it only reads whatever policy is already ambient via
    get_event_loop_policy()/new_event_loop()) -- so this test must pin that
    ambient policy to Windows' real default (Proactor) itself before
    asserting, exactly like the OS would have it in a real, un-contaminated
    process. Without this, an earlier test in the SAME xdist worker process
    that left a WindowsSelectorEventLoopPolicy installed (asyncio event-loop
    policy is real process-global mutable state, same class of leak
    conftest.py's own _reset_tunnel_launcher_diagnostics/
    _reset_graph_searcher_resolver autouse fixtures guard against for other
    modules) would make asyncio.new_event_loop() hand back a Selector loop
    regardless of what _resolve_loop() itself does -- confirmed live: this
    test passed in isolation but failed under the full suite for exactly
    this reason.
    """
    if sys.platform != "win32":
        pytest.skip("SelectorEventLoop/ProactorEventLoop distinction is Windows-only")
    original_policy = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            resolved = tunnel_main._resolve_loop()
            assert not isinstance(resolved, asyncio.SelectorEventLoop)
        finally:
            loop.close()
    finally:
        asyncio.set_event_loop_policy(original_policy)


def test_resolved_loop_supports_asyncio_subprocess_on_windows():
    """End-to-end regression test for the confirmed bug: a loop resolved by
    this module's own _resolve_loop() must actually be able to run
    asyncio.create_subprocess_exec (what tunnel_client._handle_run_cmd uses
    for run_verification) without raising NotImplementedError.

    Runs in a clean subprocess: importing meridian.tunnel_main has real,
    process-wide asyncio event-loop-policy side effects (module-scope code),
    so a prior test/import in the SAME process could otherwise mask or
    leak state across this check. Only meaningful on Windows — Selector vs
    Proactor loops behave identically for subprocess support elsewhere.
    """
    if sys.platform != "win32":
        pytest.skip("SelectorEventLoop/ProactorEventLoop subprocess support only differs on Windows")
    code = (
        "import asyncio, sys\n"
        "import meridian.tunnel_main as tm\n"
        "loop = tm._resolve_loop()\n"
        "async def _probe():\n"
        "    proc = await asyncio.create_subprocess_exec(sys.executable, '-c', 'pass')\n"
        "    return await proc.wait()\n"
        "rc = loop.run_until_complete(_probe())\n"
        "print('OK', rc)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "OK 0" in result.stdout


# ---------------------------------------------------------------------------
# Import isolation — the load-bearing test for binary size
# ---------------------------------------------------------------------------

def test_entry_import_excludes_server_stack():
    """Importing the entry module must not load the heavy server deps.

    Runs in a clean subprocess so an earlier in-process import of fastapi/etc
    can't mask a regression.
    """
    code = (
        "import sys; import meridian.tunnel_main; "
        "heavy = [m for m in ("
        "'fastapi', 'uvicorn', 'starlette', 'psycopg', 'psycopg_pool', "
        "'langgraph', 'anthropic', 'meridian.server', 'meridian.pg_adapter', "
        "'meridian.hosted'"
        ") if m in sys.modules]; "
        "assert not heavy, 'unexpected heavy imports: %r' % heavy; "
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
