"""Run a Claude Code agentic session with a /goal directive.

Spawns the Claude CLI as a subprocess, streams output to the console and
a timestamped log file, and polls the Meridian task log every N seconds
so you can see what the session is doing without staring at raw output.

Usage:
    pixi run executor --goal "Fix the auth bug in server.py"
    pixi run executor --goal "$(cat my_goal.txt)"
    echo "Fix the auth bug" | pixi run executor
    pixi run executor --goal "..." --project 5787cc92-... --interval 60

Environment variables:
    MERIDIAN_CLAUDE_CLI   Override the claude CLI argv prefix (shell-split).
                          Default: auto-detected (Windows .cmd or bare 'claude').
    MERIDIAN_PROJECT_ID   Default project ID (avoids --project arg on every run).
    MERIDIAN_BASE_URL     Meridian server URL (default: http://localhost:7878).
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Claude CLI resolution — mirrors meridian/dashboard.py:_default_claude_cli_argv
# ---------------------------------------------------------------------------

def _claude_argv() -> list[str]:
    env = os.environ.get("MERIDIAN_CLAUDE_CLI")
    if env:
        return shlex.split(env)
    windows_claude = Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd"
    if windows_claude.exists():
        return ["cmd", "/c", str(windows_claude), "-p"]
    return ["claude", "-p"]


# ---------------------------------------------------------------------------
# Task-log polling thread
# ---------------------------------------------------------------------------

def _poll_task_log(
    project_id: str,
    base_url: str,
    stop: threading.Event,
    interval: int,
) -> None:
    """Periodically fetch the last few tasks from Meridian and print them."""
    last_id: str | None = None
    while not stop.wait(interval):
        try:
            url = f"{base_url.rstrip('/')}/api/tasks?project_id={project_id}&limit=5"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                import json
                payload = json.loads(resp.read().decode())
                tasks = payload.get("tasks") or payload.get("items") or []
                for task in reversed(tasks):
                    tid = task.get("id") or ""
                    if tid and tid != last_id:
                        ts = (task.get("created_at") or "")[:19]
                        status = (task.get("status") or "").upper()[:6]
                        desc = (task.get("description") or "")[:100]
                        print(f"  [task][{status:<6}] {ts}  {desc}", flush=True)
                        last_id = tid
        except (urllib.error.URLError, OSError):
            pass  # Server may be unreachable — silently skip
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Spawn a Claude Code agentic session with a /goal directive.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--goal", "-g",
        metavar="GOAL",
        help="The /goal text to pass to Claude. Reads from stdin if omitted.",
    )
    parser.add_argument(
        "--project", "-p",
        metavar="PROJECT_ID",
        default=os.environ.get("MERIDIAN_PROJECT_ID", ""),
        help="Meridian project ID for task-log polling. "
             "Reads MERIDIAN_PROJECT_ID env var if not set.",
    )
    parser.add_argument(
        "--base-url",
        metavar="URL",
        default=os.environ.get("MERIDIAN_BASE_URL", "http://localhost:7878"),
        help="Meridian server base URL (default: http://localhost:7878).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        metavar="SECONDS",
        help="Task-log poll interval in seconds (default: 30).",
    )
    parser.add_argument(
        "--logs-dir",
        metavar="DIR",
        default="logs",
        help="Directory for log files (default: logs/).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved claude command and exit without spawning.",
    )
    args = parser.parse_args()

    # Read goal from stdin if not provided via --goal
    if not args.goal:
        if not sys.stdin.isatty():
            args.goal = sys.stdin.read().strip()
        else:
            parser.error("--goal/-g is required (or pipe the goal via stdin)")
    if not args.goal:
        parser.error("Goal cannot be empty")

    # Build the prompt — wrap in /goal if not already prefixed
    goal_text = args.goal.strip()
    prompt = goal_text if goal_text.startswith("/goal") else f"/goal {goal_text}"

    # Resolve argv
    argv_prefix = _claude_argv()
    cmd = argv_prefix + [prompt]

    # Dry-run
    if args.dry_run:
        print(f"[executor] cmd: {' '.join(repr(a) for a in cmd)}")
        return

    # Ensure logs dir exists
    logs_dir = Path(args.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = logs_dir / f"executor_{timestamp}.log"

    # Header
    separator = "─" * 72
    print(f"[executor] {separator}", flush=True)
    print(f"[executor] Goal   : {goal_text[:120]}", flush=True)
    print(f"[executor] Log    : {log_path}", flush=True)
    if args.project:
        print(f"[executor] Project: {args.project}", flush=True)
        print(f"[executor] Polling: every {args.interval}s → {args.base_url}", flush=True)
    print(f"[executor] {separator}", flush=True)

    # Write log header
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write(f"# executor session — {timestamp}\n")
        lf.write(f"# goal: {goal_text}\n")
        lf.write(f"# cmd: {' '.join(cmd)}\n\n")

    # Start task-log poller (daemon — dies with main thread)
    stop_event = threading.Event()
    if args.project:
        poller = threading.Thread(
            target=_poll_task_log,
            args=(args.project, args.base_url, stop_event, args.interval),
            daemon=True,
            name="task-log-poller",
        )
        poller.start()

    # Spawn Claude and tail output to console + log file
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # Don't use shell=True — cmd prefix already handles Windows .cmd dispatch
    )

    assert proc.stdout is not None
    try:
        with open(log_path, "a", encoding="utf-8", errors="replace") as lf:
            for raw_line in iter(proc.stdout.readline, b""):
                line = raw_line.decode("utf-8", errors="replace")
                sys.stdout.write(line)
                sys.stdout.flush()
                lf.write(line)
        proc.wait()
    except KeyboardInterrupt:
        print(f"\n[executor] Interrupted — terminating subprocess…", flush=True)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        stop_event.set()

    rc = proc.returncode or 0
    print(f"\n[executor] {separator}", flush=True)
    print(f"[executor] Exit code : {rc}", flush=True)
    print(f"[executor] Log saved : {log_path}", flush=True)
    print(f"[executor] {separator}", flush=True)

    sys.exit(rc)


if __name__ == "__main__":
    main()
