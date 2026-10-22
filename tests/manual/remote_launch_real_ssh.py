"""Manual check: remote task launch and status against a REAL sshd.

This is NOT a unit test. Pytest does not collect it (the name does not match
test_*.py), and it needs a live SSH server. Run it by hand against an sshd you
control, for example a throwaway container:

    pixi run python tests/manual/remote_launch_real_ssh.py \
        --host meridian-test@127.0.0.1 --port 2222 \
        --key-path ~/.ssh/meridian_test_ed25519

Arguments (nothing about the target is embedded in this file):
    --host         ssh destination: hostname, user@host, or ssh-config alias. Required.
    --port         ssh port. Default 22.
    --key-path     private key FILE for key-based auth. Required. Only the path is
                   used; the key contents are never read or printed.
    --known-hosts  known_hosts FILE that ssh reads and writes for this run. Default:
                   a new temporary file, deleted on exit. The host key is recorded
                   there, never in your own ~/.ssh/known_hosts. Pass a path to keep
                   the host key between runs.

What it checks, using the real ssh binary and the real launch/status scripts:
  1. Launch: the call returns in under LAUNCH_RETURN_LIMIT_SECONDS and prints a
     PID, even though the job it starts runs for LONGER than that. This is the
     regression check for the outer background job holding the SSH channel
     open. Before the fix, the launch waited for the whole job.
  2. While the job runs: the status check reports RUNNING and the same PID.
  3. After the job exits: the status check reports completed-failure with the
     exit status the job chose (JOB_EXIT_CODE), and the stdout and stderr tails
     come back separately. The stderr output is larger than the tail bound, so a
     combined tail would hide stdout. This checks that stdout survives.

Exit status: 0 when every check passes, 1 when any check fails.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from meridian import remote_exec  # noqa: E402
from meridian import remote_ssh  # noqa: E402

LAUNCH_RETURN_LIMIT_SECONDS = 5.0
JOB_RUNTIME_SECONDS = 8
JOB_EXIT_CODE = 3
STDERR_BYTES = 6000  # larger than MAX_LOG_TAIL_BYTES on purpose
JOB_COMMAND = (
    "echo hello-out; "
    f"head -c {STDERR_BYTES} /dev/zero | tr '\\0' e 1>&2; "
    f"sleep {JOB_RUNTIME_SECONDS}; exit {JOB_EXIT_CODE}"
)


def _ssh(
    host: str, port: int, key_path: str, known_hosts: str,
    remote_command: str, timeout: float,
):
    """Run one real ssh call. Returns (CompletedProcess | None, elapsed, error).

    ``known_hosts`` goes in as UserKnownHostsFile, so accept-new writes the host
    key to this run's file and not to the user's ~/.ssh/known_hosts. The value is
    passed unquoted: on Windows, Python's argv quoting turns embedded double quotes
    into backslash-escaped ones, which ssh rejects as "invalid quotes". The path is
    checked by main() to contain no whitespace and no percent sign, so it needs no
    quoting."""
    argv = [
        "ssh", *remote_ssh.DEFAULT_SSH_OPTIONS,
        "-o", "IdentitiesOnly=yes",
        "-o", f"UserKnownHostsFile={Path(known_hosts).as_posix()}",
        "-p", str(port), "-i", key_path,
        host, remote_command,
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 -- argv list, no shell
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, time.monotonic() - started, "timeout"
    return completed, time.monotonic() - started, None


def _report(label: str, ok: bool, detail: str) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}: {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manual launch/status check of remote tasks against a real sshd.",
    )
    parser.add_argument("--host", required=True, help="ssh destination (host, user@host, or alias)")
    parser.add_argument("--port", type=int, default=22, help="ssh port (default 22)")
    parser.add_argument("--key-path", required=True, help="private key file (path only)")
    parser.add_argument(
        "--known-hosts", default=None,
        help="known_hosts file ssh reads and writes for this run (default: a new "
             "temporary file, deleted on exit)",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not os.path.isfile(args.key_path):
        parser.error("--key-path does not point to a readable file")
    if args.known_hosts is not None:
        problem = _known_hosts_problem(args.known_hosts)
        if problem:
            parser.error(f"--known-hosts {problem}")
        return _run(args, args.known_hosts)
    # Default: ssh gets a new empty file, so the test host key never reaches the
    # user's own known_hosts. The file is removed however the run ends.
    fd, temp_known_hosts = tempfile.mkstemp(prefix="meridian_known_hosts_", suffix=".txt")
    os.close(fd)
    try:
        problem = _known_hosts_problem(temp_known_hosts)
        if problem:
            parser.error(
                f"the default temporary known_hosts path {problem}; pass --known-hosts "
                "with a path that has no spaces or percent signs"
            )
        return _run(args, temp_known_hosts)
    finally:
        os.unlink(temp_known_hosts)


def _known_hosts_problem(path: str) -> str | None:
    """Why this path cannot be passed to ssh unquoted, or None if it can."""
    if not path:
        return "must be a non-empty path"
    if any(ch.isspace() for ch in path) or '"' in path or "%" in path:
        return "must not contain whitespace, quotes or percent signs (ssh expands %)"
    return None


def _run(args: argparse.Namespace, known_hosts: str) -> int:
    host = remote_exec.validate_host(args.host)
    command = remote_exec.validate_command(JOB_COMMAND)
    job_id = str(uuid.uuid4())
    failures: list[str] = []
    print(f"job_id={job_id}")
    print(f"known_hosts={known_hosts}")

    # 1. Launch -- must return quickly even though the job runs for longer.
    launch_completed, launch_elapsed, launch_error = _ssh(
        host, args.port, args.key_path, known_hosts,
        remote_exec.build_launch_script(job_id, command),
        remote_ssh.DEFAULT_LAUNCH_TIMEOUT_SECONDS,
    )
    if launch_error or launch_completed is None or launch_completed.returncode != 0:
        _report("launch", False, f"error={launch_error} returncode="
                f"{getattr(launch_completed, 'returncode', None)}")
        return 1
    pid = remote_exec.parse_launch_pid(launch_completed.stdout)
    launch_ok = launch_elapsed < LAUNCH_RETURN_LIMIT_SECONDS and pid is not None
    _report(
        "launch returns quickly with a pid", launch_ok,
        f"elapsed={launch_elapsed:.2f}s (limit {LAUNCH_RETURN_LIMIT_SECONDS}s, "
        f"job runs {JOB_RUNTIME_SECONDS}s) pid={pid}",
    )
    if not launch_ok:
        failures.append("launch")

    # 2. Status while the job is still running.
    running_completed, _, running_error = _ssh(
        host, args.port, args.key_path, known_hosts,
        remote_exec.build_status_check_script(job_id),
        remote_ssh.DEFAULT_STATUS_TIMEOUT_SECONDS,
    )
    if running_error or running_completed is None:
        _report("status while running", False, f"error={running_error}")
        failures.append("status while running")
    else:
        running = remote_exec.parse_status_check_output(running_completed.stdout)
        running_ok = running["marker"] == "RUNNING" and running["pid"] == pid
        _report(
            "status while running", running_ok,
            f"marker={running['marker']} pid_from_pid_txt={running['pid']}",
        )
        if not running_ok:
            failures.append("status while running")

    # 3. Status after the job has exited.
    time.sleep(JOB_RUNTIME_SECONDS + 2)  # the job exits after JOB_RUNTIME_SECONDS
    done_completed, _, done_error = _ssh(
        host, args.port, args.key_path, known_hosts,
        remote_exec.build_status_check_script(job_id),
        remote_ssh.DEFAULT_STATUS_TIMEOUT_SECONDS,
    )
    if done_error or done_completed is None:
        _report("status after exit", False, f"error={done_error}")
        failures.append("status after exit")
        return 1
    done = remote_exec.parse_status_check_output(done_completed.stdout)
    status = remote_exec.map_check_marker_to_status(done["marker"], done["exit_code"])
    exit_ok = (
        status == "completed-failure"
        and done["exit_code"] == JOB_EXIT_CODE
    )
    _report(
        "exit status read from exit_code.txt", exit_ok,
        f"status={status} exit_code={done['exit_code']} (expected {JOB_EXIT_CODE})",
    )
    if not exit_ok:
        failures.append("exit status")

    stdout_ok = "hello-out" in done["stdout_tail"]
    _report(
        "stdout tail survives a large stderr", stdout_ok,
        f"stdout_tail={len(done['stdout_tail'])}B "
        f"preview={done['stdout_tail'][:40]!r}",
    )
    if not stdout_ok:
        failures.append("stdout tail")

    stderr_ok = (
        0 < len(done["stderr_tail"].encode("utf-8")) <= remote_exec.MAX_LOG_TAIL_BYTES
        and set(done["stderr_tail"]) <= {"e", "\n"}
    )
    _report(
        "stderr tail is bounded", stderr_ok,
        f"stderr_tail={len(done['stderr_tail'].encode('utf-8'))}B "
        f"(limit {remote_exec.MAX_LOG_TAIL_BYTES}B)",
    )
    if not stderr_ok:
        failures.append("stderr tail")

    print("RESULT: " + ("all checks passed" if not failures else f"failed: {', '.join(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
