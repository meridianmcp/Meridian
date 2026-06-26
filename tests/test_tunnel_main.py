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
