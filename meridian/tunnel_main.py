"""Minimal PyInstaller entry point for the slim ``meridian.exe`` tunnel client.

The full server entry point (``meridian/__main__entry.py``) pulls in FastAPI,
uvicorn, psycopg3, langgraph, the hosted/billing routes and every DB dependency.
The standalone binary that users download (``meridian.exe`` / ``meridian-linux``
/ ``meridian-mac-*``) is only ever used as a Pro filesystem tunnel client
(``meridian --tunnel --repo .``), so bundling the whole server is wasteful.

This module is the dedicated tunnel-client entry point. It imports ONLY what the
tunnel needs:

  * stdlib (argparse / asyncio / os / selectors / sys)
  * ``meridian.tunnel_client`` (which itself only imports stdlib + serena_pool
    at module scope; httpx / websockets are imported lazily inside ``run_tunnel``)
  * ``meridian.serena_pool`` (pure stdlib)

It deliberately does NOT import ``meridian.server``, ``meridian.pg_adapter``,
``meridian.hosted``, langgraph, FastAPI or uvicorn. The PyInstaller spec
(``meridian.spec``) excludes those modules so the frozen binary stays small.

CLI flags mirror the ``--tunnel`` subset of ``meridian/__main__.py`` so the
binary behaves exactly like ``python -m meridian --tunnel ...``.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Any


_TUNNEL_CHILD_FLAG = "--_tunnel-child"
_SUPERVISOR_POLL_SECONDS = 1.0
_RESTART_BASE_SECONDS = 1.0
_RESTART_MAX_SECONDS = 30.0

# 7b457c55 — REVERSED (was: force WindowsSelectorEventLoopPolicy here). This
# module is the slim, tunnel-ONLY PyInstaller entry point (no --mcp/--server
# dispatch exists in this binary at all — see the module docstring), and the
# tunnel's own `run_cmd` control-message handling
# (tunnel_client._handle_run_cmd, used by run_verification) spawns child
# processes via asyncio.create_subprocess_exec/_shell. Those asyncio-native
# subprocess APIs raise a bare, message-less NotImplementedError on Windows'
# SelectorEventLoop — they require a Proactor-compatible loop. Forcing
# WindowsSelectorEventLoopPolicy here (originally added for a psycopg_pool
# issue that predates this module's later split into a pure, psycopg-free
# tunnel-only entry point — see the historical f73810d5/3ac13517 note this
# comment replaces) broke every run_cmd/run_verification call made through
# the compiled `meridian.exe` tunnel binary on Windows — confirmed live via
# docs/meridian-local-runner-tunnel-investigation-2026-08-31.md ("the slim
# tunnel entrypoint can force a selector loop while tunnel command handling
# uses asyncio subprocess APIs that require a Proactor-compatible loop on
# Windows").
#
# meridian/__main__.py's own `--tunnel` dispatch already carries the
# identical fix (see its module-scope comment: "Skipped for --tunnel mode...
# Leaving tunnel mode on the Windows-default ProactorEventLoop... fixes
# that") — this was the one tunnel entry point that never got it mirrored.
# Unlike __main__.py, this module has NO competing --mcp/server mode that
# would need SelectorEventLoop for psycopg3, so the fix here is simply: do
# not force anything, and let Windows' own default policy (Proactor) apply.
# This never touches meridian/__main__.py's real MCP stdio dispatch (--mcp),
# which keeps forcing SelectorEventLoop exactly as before — that protocol
# path is untouched by this module entirely.


def _build_parser() -> argparse.ArgumentParser:
    """Argument parser for the slim tunnel binary (mirrors __main__ --tunnel)."""
    parser = argparse.ArgumentParser(
        prog="meridian",
        description="Meridian Pro filesystem tunnel client.",
    )
    parser.add_argument(
        "--tunnel",
        action="store_true",
        help="Run the Pro filesystem tunnel client (default for this binary).",
    )
    parser.add_argument(
        "--token",
        default=None,
        help=(
            "API token (defaults to MERIDIAN_TOKEN / MERIDIAN_API_KEY / "
            "BEARER_TOKEN, in that order). Prefer the env var: a --token value "
            "is visible in process listings."
        ),
    )
    parser.add_argument(
        "--server",
        default=None,
        help="Meridian server base URL (defaults to MERIDIAN_URL or "
        "https://usemeridian.us).",
    )
    parser.add_argument(
        "--repo",
        nargs="+",
        default=None,
        help="Repo path(s) to expose over the tunnel (defaults to ~). The FIRST "
             "path is the active repo (Serena --project); any additional paths are "
             "extra filesystem roots. Passing specific repo dirs (not a parent like "
             "Documents) avoids Serena scanning broken Windows junctions. (cbbd0eb4)",
    )
    parser.add_argument(
        "--tunnel-port",
        type=int,
        default=8808,
        help="Local port for the tunnel's mcp-proxy (default 8808).",
    )
    parser.add_argument(
        "--no-kill",
        action="store_true",
        help="Skip stale-port cleanup for fast restarts. Accepted for parity "
        "with `python -m meridian --tunnel`; the slim binary does not run the "
        "port-kill loop, so this is a no-op here. (a887155d)",
    )
    parser.add_argument(
        _TUNNEL_CHILD_FLAG,
        dest="tunnel_child",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--code-dir",
        action="append",
        metavar="PATH",
        dest="code_dirs",
        help="Directory to auto-index with codebase-memory-mcp on tunnel start "
        "(repeatable: --code-dir /repo1 --code-dir /repo2).",
    )
    return parser


def _build_child_command(args: argparse.Namespace) -> tuple[list[str], dict[str, str]]:
    """Return the tunnel worker invocation and inherited environment.

    The parent process owns restart supervision. A source checkout is relaunched
    as a module; a frozen PyInstaller executable relaunches itself. Credentials
    stay out of the worker command line even when the caller used ``--token``.
    """
    child_args = [_TUNNEL_CHILD_FLAG]
    if args.server is not None:
        child_args.extend(("--server", args.server))
    if args.repo:
        child_args.extend(("--repo", *args.repo))
    child_args.extend(("--tunnel-port", str(args.tunnel_port)))
    if args.no_kill:
        child_args.append("--no-kill")
    for code_dir in args.code_dirs or ():
        child_args.extend(("--code-dir", code_dir))

    if getattr(sys, "frozen", False):
        command = [sys.executable, *child_args]
    else:
        command = [sys.executable, "-m", "meridian.tunnel_main", *child_args]

    child_env = os.environ.copy()
    if args.token:
        child_env["MERIDIAN_TOKEN"] = args.token
    return command, child_env


def _restart_delay(restart_count: int, consecutive_failures: int = 0) -> float:
    """Exponential process-restart backoff, capped to keep recovery bounded."""
    exponent = min(5, max(0, restart_count) + max(0, consecutive_failures))
    return min(_RESTART_MAX_SECONDS, _RESTART_BASE_SECONDS * (2**exponent))


def _show_child_log(runner: Any) -> None:
    """Show the latest bounded worker log so the tunnel URL remains visible."""
    try:
        output = runner.tail_log(max_bytes=16 * 1024)
    except Exception:  # noqa: BLE001 — logging must not interfere with supervision
        return
    if output:
        sys.stdout.write(output)
        if not output.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()


def _run_supervised(args: argparse.Namespace) -> int:
    """Keep the tunnel worker alive through LocalRunner's owned-process API."""
    from .local_runner import ChildState, LocalRunner, RunnerAlreadyRunningError

    command, child_env = _build_child_command(args)
    runner = LocalRunner(
        scope=f"meridian-tunnel-client-{args.tunnel_port}",
        command=command,
        cwd=os.getcwd(),
        env=child_env,
        tunnel_label="tunnel-client",
    )
    started = False
    consecutive_failures = 0
    try:
        while True:
            try:
                runner.start()
                started = True
                _show_child_log(runner)
                break
            except RunnerAlreadyRunningError:
                raise
            except Exception as exc:  # noqa: BLE001 — transient spawn failures get retried
                delay = _restart_delay(0, consecutive_failures)
                consecutive_failures += 1
                print(
                    f"tunnel: client start failed ({type(exc).__name__}); retrying in {delay:.0f}s",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)

        while True:
            time.sleep(_SUPERVISOR_POLL_SECONDS)
            status = runner.status()
            if status.child.state is ChildState.RUNNING:
                consecutive_failures = 0
                continue

            delay = _restart_delay(status.child.restart_count, consecutive_failures)
            print(
                f"tunnel: client process {status.child.state.value} "
                f"(exit={status.child.exit_code}); restarting in {delay:.0f}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
            while True:
                try:
                    runner.restart()
                    consecutive_failures = 0
                    _show_child_log(runner)
                    break
                except Exception as exc:  # noqa: BLE001 — keep retrying the owned child
                    retry_delay = _restart_delay(
                        status.child.restart_count, consecutive_failures + 1
                    )
                    consecutive_failures += 1
                    print(
                        f"tunnel: client restart failed ({type(exc).__name__}); "
                        f"retrying in {retry_delay:.0f}s",
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(retry_delay)
    except KeyboardInterrupt:
        return 0
    except RunnerAlreadyRunningError as exc:
        print(f"tunnel: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if started:
            try:
                runner.stop()
            except Exception:  # noqa: BLE001 — don't mask the supervisor's exit
                pass


def _run_tunnel(args: argparse.Namespace) -> int:
    """Run one tunnel worker; the normal entry point wraps this with a supervisor."""
    # Import lazily so importing the entry module doesn't pull tunnel deps in.
    from .tunnel_client import run_tunnel

    loop = _resolve_loop()
    _repo_list = args.repo if isinstance(args.repo, list) else ([args.repo] if args.repo else [])
    _repo_path = _repo_list[0] if _repo_list else None
    _extra_roots = _repo_list[1:]
    try:
        return loop.run_until_complete(
            run_tunnel(
                token=args.token,
                base_url=args.server,
                repo_path=_repo_path,
                extra_fs_roots=_extra_roots,
                port=args.tunnel_port,
                code_dirs=args.code_dirs,
            )
        )
    except KeyboardInterrupt:
        return 0


def _resolve_loop() -> asyncio.AbstractEventLoop:
    """Return a usable event loop without relying on get_event_loop() state.

    7b457c55 — no longer forces a SelectorEventLoop on Windows (see the
    module-scope comment above for the confirmed bug this reverses: this
    entry point is tunnel-only, and the tunnel's run_cmd/run_verification
    handling needs asyncio.create_subprocess_exec/_shell, which requires a
    Proactor-compatible loop on Windows). Falls back to Windows' own default
    policy (Proactor) when there is no usable ambient loop, instead of a
    hand-built SelectorEventLoop.
    """
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
        if not loop.is_closed():
            return loop
    except RuntimeError:
        pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop


def main(argv: list[str] | None = None) -> int:
    """Run the supervised tunnel client or its internal worker process."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.tunnel_child:
        return _run_tunnel(args)
    return _run_supervised(args)


if __name__ == "__main__":
    sys.exit(main())
