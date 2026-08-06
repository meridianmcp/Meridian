"""Run pytest with one adaptive, resource-aware local/CI policy.

The repository has many small, database-heavy tests. Spawning one xdist
process per CPU for a small test selection is slower than running those
tests in-process, while the full suite benefits from dynamic work stealing.
This wrapper keeps that decision identical on developer machines and CI.

Environment overrides:

``MERIDIAN_TEST_SERIAL_THRESHOLD``
    Maximum collected test count that runs serially (default: 40). The
    measured 37-test batch was 3x faster serially than with xdist auto.
``MERIDIAN_TEST_MAX_WORKERS``
    Upper bound applied to ``-n auto`` (default: 8).
``MERIDIAN_ALLOW_CONCURRENT_TESTS=1``
    Explicit escape hatch for intentionally independent test roots.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


DEFAULT_SERIAL_THRESHOLD = 40
DEFAULT_MAX_WORKERS = 8
_COLLECTED_RE = re.compile(
    r"(?:collected\s+)?(\d+)\s+(?:tests?|items?)\s+collected|"
    r"collected\s+(\d+)\s+(?:tests?|items?)",
    re.IGNORECASE,
)


def parse_collected_count(output: str) -> int | None:
    """Extract pytest's final collection count from stdout/stderr."""

    matches = list(_COLLECTED_RE.finditer(output))
    if not matches:
        return None
    match = matches[-1]
    return int(match.group(1) or match.group(2))


def _without_xdist_args(args: list[str]) -> list[str]:
    """Remove scheduling flags before the serial collection preflight."""

    result: list[str] = []
    skip_next = False
    separate = {"-n", "--numprocesses", "--dist", "--maxprocesses"}
    prefixes = ("--numprocesses=", "--dist=", "--maxprocesses=")
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in separate:
            skip_next = True
            continue
        if arg in {"-p", "--pyargs"}:
            # ``-p no:xdist`` is harmless during collection, but removing the
            # plugin flag keeps this helper tolerant of caller-provided args.
            result.append(arg)
            continue
        if arg.startswith(prefixes) or arg == "-d":
            continue
        result.append(arg)
    return result


def _has_option(args: list[str], name: str) -> bool:
    return any(arg == name or arg.startswith(name + "=") for arg in args)


def _without_verbosity_args(args: list[str]) -> list[str]:
    """Strip ``-q``/``--quiet``/``-v``/``--verbose`` before the collect-only
    preflight appends its own single ``-q``.

    Every caller in this repo already passes its own ``-q`` (matching this
    repo's pytest-invocation convention), so appending another ``-q``
    on top produced pytest verbosity -2, not -1. At -2, pytest's terminal
    reporter switches ``--collect-only`` to a per-file "path: count" summary
    with NO final "N tests collected" line at all -- confirmed live: this
    silently broke every ``pixi run test``/``test-cov`` invocation (local
    and CI) with ``Could not determine collected test count``, which blocked
    the dev->main auto-promote deploy pipeline outright. Stripping any
    caller-supplied verbosity flags here guarantees the preflight always
    runs at exactly one ``-q`` (verbosity -1), independent of the caller's
    own flags, so its output format is deterministic and parseable.
    """
    return [arg for arg in args if arg not in ("-q", "--quiet", "-v", "--verbose")]


def collect_count(args: list[str]) -> tuple[int | None, int]:
    """Collect tests once, returning ``(count, pytest_exit_code)``."""

    collect_args = _without_verbosity_args(_without_xdist_args(args))
    collect_args.extend(["--collect-only", "-q", "-p", "no:xdist"])
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *collect_args],
        text=True,
        capture_output=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        sys.stderr.write(output)
        return None, completed.returncode
    return parse_collected_count(output), 0


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestRunLock:
    """Small cross-platform process lock preventing duplicate repo test runs."""

    def __init__(self, repo_root: Path) -> None:
        key = hashlib.sha256(str(repo_root).casefold().encode()).hexdigest()[:20]
        self.path = Path(tempfile.gettempdir()) / f"meridian-pytest-{key}.lock"
        self.acquired = False

    def acquire(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                raw = self.path.read_text(encoding="utf-8").split("\t", 1)[0]
                pid = int(raw)
            except (OSError, ValueError):
                pid = -1
            if _pid_is_running(pid):
                return False
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return self.acquire()
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\t{time.time()}\t{Path.cwd()}\n")
        self.acquired = True
        return True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.acquired = False


def build_run_args(
    pytest_args: list[str],
    collected: int,
    *,
    serial_threshold: int = DEFAULT_SERIAL_THRESHOLD,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> list[str]:
    """Apply the repository's deterministic scheduling policy."""

    args = list(pytest_args)
    args = _without_xdist_args(args)
    if not _has_option(args, "--durations"):
        args.append("--durations=20")
    if not _has_option(args, "--timeout"):
        args.append("--timeout=60")
    if collected <= serial_threshold:
        args.extend(["-p", "no:xdist"])
    else:
        args.extend(["-n", "auto", "--dist=worksteal", "--maxprocesses", str(max_workers)])
    return args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--serial",
        action="store_true",
        help="Force serial execution (used for wall-clock-sensitive tests).",
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv)
    pytest_args = ns.pytest_args
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    if not pytest_args:
        pytest_args = ["tests/"]

    if os.environ.get("MERIDIAN_ALLOW_CONCURRENT_TESTS") != "1":
        lock = TestRunLock(Path.cwd().resolve())
        if not lock.acquire():
            print(
                "Another Meridian test run is active for this repository. "
                "Wait for it to finish or set MERIDIAN_ALLOW_CONCURRENT_TESTS=1 "
                "for intentionally independent roots.",
                file=sys.stderr,
            )
            return 2
    else:
        lock = None

    try:
        serial_threshold = int(
            os.environ.get("MERIDIAN_TEST_SERIAL_THRESHOLD", DEFAULT_SERIAL_THRESHOLD)
        )
        max_workers = int(
            os.environ.get("MERIDIAN_TEST_MAX_WORKERS", DEFAULT_MAX_WORKERS)
        )
        collected, code = collect_count(pytest_args)
        if code:
            return code
        if collected is None:
            print("Could not determine collected test count; refusing to guess scheduling.", file=sys.stderr)
            return 2
        effective_count = 0 if ns.serial else collected
        run_args = build_run_args(
            pytest_args,
            effective_count,
            serial_threshold=serial_threshold,
            max_workers=max_workers,
        )
        mode = "serial (forced)" if ns.serial else (
            "serial" if collected <= serial_threshold else f"auto/worksteal (max {max_workers})"
        )
        print(f"Meridian test policy: {collected} tests -> {mode}", flush=True)
        return subprocess.run([sys.executable, "-m", "pytest", *run_args], check=False).returncode
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
