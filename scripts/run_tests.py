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

2cebf4ae -- durable observable test-run lifecycle
==================================================

Historically this wrapper only ever produced two signals: nothing (while
running) and a completion-only exit code (once ``subprocess.run`` returned).
That is unusable for two real, observed failure modes:

1. pytest can print its full ``N passed, M failed in X.XXs`` results summary
   line and then hang indefinitely on process exit -- a leaked ``aiosqlite``
   connection's non-daemon thread blocks interpreter exit. A caller tailing
   a log has no way to distinguish "still running the last test" from
   "already has real results, just hasn't exited" -- both look identical:
   silence.
2. ``TestRunLock._pid_is_running`` used ``os.kill(pid, 0)`` as its liveness
   probe. On Windows this raises ``OSError: [WinError 87]`` (invalid
   parameter) for a PID that no longer exists, instead of the
   ``ProcessLookupError`` POSIX raises -- so a stale lock left behind by a
   force-killed run could crash the NEXT run's lock acquisition instead of
   being correctly reclaimed, blocking all subsequent test runs.

This module now maintains a small, persistent, streamed JSON run record
(``TestRunRecord`` / ``TestRunTracker``) next to the lock file with an
explicit state machine (queued -> starting -> collecting -> running ->
{stalled} -> one of {timed_out, crashed, cancelled, passed, failed}),
records monotonic start/last-progress/terminal times, command, cwd, owner
session, worker count, child pid/process-tree identity, bounded
stdout/stderr tails, exit code/signal, timeout classification, and a
cleanup receipt -- and fixes the Windows PID-liveness bug so a stale lock
is reliably reclaimed on every platform.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable


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


# ---------------------------------------------------------------------------
# Cross-platform PID liveness (2cebf4ae) -- fixes WinError 87 propagating
# out of the old os.kill(pid, 0) probe on Windows instead of being treated
# as "process does not exist", which used to crash TestRunLock.acquire()'s
# stale-lock reclaim path instead of correctly reclaiming it.
# ---------------------------------------------------------------------------

_ERROR_INVALID_PARAMETER = 87  # "the pid does not exist" -- dead.
_ERROR_ACCESS_DENIED = 5  # process exists, we lack rights to query it -- alive.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259  # GetExitCodeProcess sentinel meaning "hasn't exited yet".


class _Win32ProcessProbe:
    """Thin, injectable wrapper over the kernel32 calls PID liveness needs.
    Mirrors ``meridian.process_lifecycle.Win32JobAPI``'s pattern exactly --
    a real instance wraps a properly-prototyped ``ctypes.WinDLL``; tests
    construct a fake object exposing the same method names so this
    Windows-only code path is fully exercisable on non-Windows CI without
    ever touching a real ``ctypes.WinDLL`` (which does not exist off
    Windows -- see this repo's documented "must not touch Windows-only
    stdlib attrs on a monkeypatched platform" trap)."""

    def __init__(self, kernel32: Any):
        self._k = kernel32

    def open_process(self, pid: int) -> "int | None":
        h = self._k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        return int(h) if h else None

    def close_handle(self, handle: int) -> bool:
        return bool(self._k.CloseHandle(handle))

    def get_last_error(self) -> int:
        return int(self._k.GetLastError())

    def get_exit_code(self, handle: int) -> "int | None":
        """``GetExitCodeProcess`` -- ``OpenProcess`` succeeding only means the
        PID's process OBJECT still exists, which remains true as long as ANY
        handle references it (e.g. a parent's own ``subprocess.Popen``
        handle, held open until it calls ``wait()``/``poll()``) even after
        the process has actually terminated. The exit code is the real
        liveness signal: ``STILL_ACTIVE`` (259) means genuinely running,
        anything else means it has exited. Returns ``None`` on any failure
        (caller treats that as "can't tell, assume alive" -- fail safe)."""
        import ctypes  # noqa: PLC0415 -- Windows-only

        code = ctypes.c_uint32(0)
        ok = self._k.GetExitCodeProcess(handle, ctypes.byref(code))
        return int(code.value) if ok else None


def _load_win32_process_probe() -> "_Win32ProcessProbe | None":
    """Real loader: binds + prototypes ``ctypes.WinDLL('kernel32')``.
    Returns ``None`` (never raises) off Windows or on any bind failure --
    :func:`_win32_pid_is_running` degrades to "assume alive" (fail safe:
    never delete a lock file we can't actually confirm is stale) when this
    returns ``None``."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes  # noqa: PLC0415 -- Windows-only, avoid import cost elsewhere

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = ctypes.c_uint32
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        return _Win32ProcessProbe(kernel32)
    except Exception:  # noqa: BLE001
        return None


def _win32_pid_is_running(
    pid: int, probe_loader: "Callable[[], _Win32ProcessProbe | None] | None" = None
) -> bool:
    """Windows liveness probe via ``OpenProcess`` -- the actual fix for the
    documented bug. Treats a NULL handle whose ``GetLastError()`` is
    ``ERROR_INVALID_PARAMETER`` (87, "no such process") as DEAD,
    ``ERROR_ACCESS_DENIED`` (5, process exists but we lack rights) as ALIVE,
    and any other unexpected condition (including the probe being
    unavailable at all) as ALIVE -- fail safe, so this can never cause a
    live run's lock to be deleted out from under it. Never lets
    ``WinError 87`` propagate as an exception; that was the historical bug.

    When ``OpenProcess`` DOES succeed, this also checks the real exit code
    via ``GetExitCodeProcess`` rather than treating "handle opened" alone
    as proof of life: the process OBJECT persists (making ``OpenProcess``
    succeed) as long as ANY handle references it -- including a parent's
    own still-open ``subprocess.Popen`` handle -- even well after the
    process has actually terminated. Only ``STILL_ACTIVE`` (259) counts as
    genuinely running; any other exit code (or a failed exit-code query)
    falls through to the same fail-safe ALIVE default as before.
    """
    loader = probe_loader or _load_win32_process_probe
    try:
        probe = loader()
    except Exception:  # noqa: BLE001
        probe = None
    if probe is None:
        return True
    try:
        handle = probe.open_process(pid)
    except Exception:  # noqa: BLE001
        return True
    if handle:
        try:
            exit_code = probe.get_exit_code(handle)
        except Exception:  # noqa: BLE001
            exit_code = None
        try:
            probe.close_handle(handle)
        except Exception:  # noqa: BLE001
            pass
        if exit_code is not None and exit_code != _STILL_ACTIVE:
            return False
        return True
    try:
        err = probe.get_last_error()
    except Exception:  # noqa: BLE001
        return True
    if err == _ERROR_INVALID_PARAMETER:
        return False
    if err == _ERROR_ACCESS_DENIED:
        return True
    return True


def _pid_is_running(pid: int) -> bool:
    """Cross-platform PID liveness. Prefers ``psutil`` (already an optional
    dependency elsewhere in this codebase) when available; otherwise falls
    back to a real, platform-correct probe -- ``os.kill(pid, 0)`` on POSIX,
    the ``OpenProcess``-based :func:`_win32_pid_is_running` on Windows
    (never the bare ``os.kill`` call that used to raise ``WinError 87``
    there)."""
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        psutil = None  # type: ignore[assignment]
    if psutil is not None:
        try:
            return bool(psutil.pid_exists(pid))
        except Exception:  # noqa: BLE001
            pass  # fall through to the OS-level probes below
    if sys.platform == "win32":
        return _win32_pid_is_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Any other unexpected errno -- fail safe: never silently delete a
        # lock we can't actually confirm is stale.
        return True
    return True


class TestRunLock:
    """Small cross-platform process lock preventing duplicate repo test
    runs, paired with a durable JSON run-state file at ``state_path``
    (2cebf4ae) so a caller can tell WHICH run currently owns the checkout
    and what state it's in, not just that the lock file exists."""

    def __init__(self, repo_root: Path) -> None:
        key = hashlib.sha256(str(repo_root).casefold().encode()).hexdigest()[:20]
        self.path = Path(tempfile.gettempdir()) / f"meridian-pytest-{key}.lock"
        self.state_path = Path(tempfile.gettempdir()) / f"meridian-pytest-{key}.state.json"
        self.acquired = False
        # pid found in an existing lock file, whether or not WE end up
        # owning the lock -- set on both the success and failure paths so
        # callers can build a truthful duplicate-run report.
        self.owner_pid: "int | None" = None
        # Set only when acquire() reclaimed a STALE lock (owner confirmed
        # dead) -- distinguishes "fresh acquire" from "took over from a
        # crashed/killed prior run" for the caller's own audit trail.
        self.reclaimed_stale_pid: "int | None" = None

    def _read_owner_pid(self) -> "int | None":
        try:
            raw = self.path.read_text(encoding="utf-8").split("\t", 1)[0]
            return int(raw)
        except (OSError, ValueError):
            return None

    def acquire(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = self._read_owner_pid()
            self.owner_pid = pid if pid is not None else -1
            if _pid_is_running(self.owner_pid):
                return False
            self.reclaimed_stale_pid = self.owner_pid
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return self.acquire()
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\t{time.time()}\t{Path.cwd()}\n")
        self.acquired = True
        self.owner_pid = os.getpid()
        return True

    def owner_pid_is_confirmed_alive(self) -> bool:
        """True only when ``owner_pid`` is a real, currently-live process --
        the precondition :func:`main`'s ``--supersede`` path requires before
        it will terminate another run's process tree. Never guesses."""
        return self.owner_pid is not None and self.owner_pid > 0 and _pid_is_running(self.owner_pid)

    def reclaim_after_supersede(self) -> bool:
        """Explicit, ``--supersede``-only path: after the caller has already
        verified + terminated the previous owner's process tree itself,
        remove its lock file (idempotent -- the prior owner's own cleanup
        may have already removed it) and acquire fresh. Never called on the
        default (non-superseding) duplicate-run path."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        return self.acquire()

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


def _infer_worker_count(run_args: list[str], max_workers: int) -> int:
    """Best-effort worker-count classification for the run record, purely
    for observability -- never affects scheduling itself (``build_run_args``
    already decided that)."""
    if "-n" in run_args:
        idx = run_args.index("-n")
        val = run_args[idx + 1] if idx + 1 < len(run_args) else "auto"
        if val == "auto":
            try:
                return max(1, min(max_workers, os.cpu_count() or 1))
            except Exception:  # noqa: BLE001
                return 1
        try:
            return max(1, int(val))
        except ValueError:
            return 1
    return 1


# ---------------------------------------------------------------------------
# 2cebf4ae -- durable, streamed test-run lifecycle record.
# ---------------------------------------------------------------------------

STATE_QUEUED = "queued"
STATE_STARTING = "starting"
STATE_COLLECTING = "collecting"
STATE_RUNNING = "running"
STATE_STALLED = "stalled"
STATE_TIMED_OUT = "timed_out"
STATE_CRASHED = "crashed"
STATE_CANCELLED = "cancelled"
STATE_PASSED = "passed"
STATE_FAILED = "failed"

RUN_STATES: tuple[str, ...] = (
    STATE_QUEUED, STATE_STARTING, STATE_COLLECTING, STATE_RUNNING, STATE_STALLED,
    STATE_TIMED_OUT, STATE_CRASHED, STATE_CANCELLED, STATE_PASSED, STATE_FAILED,
)
TERMINAL_STATES = frozenset({STATE_TIMED_OUT, STATE_CRASHED, STATE_CANCELLED, STATE_PASSED, STATE_FAILED})

DEFAULT_WALL_TIMEOUT_SECONDS = 1800.0
DEFAULT_STALL_TIMEOUT_SECONDS = 300.0
DEFAULT_POST_RESULTS_GRACE_SECONDS = 20.0
# How many consecutive stall-timeout windows of true silence we tolerate
# before escalating "stalled" (still waiting, might recover) to a terminal
# "timed_out" (kind=stalled) -- keeps a brief hiccup from being fatal while
# still bounding a genuinely wedged run.
_STALL_ESCALATION_FACTOR = 3
_TAIL_CHARS = 8_000
_OUTPUT_SAVE_THROTTLE_SECONDS = 0.25
_HEARTBEAT_INTERVAL_SECONDS = 60.0

# Matches pytest's final one-line summary. In a real TTY this is wrapped in
# "=" padding ("===== 5 passed, 1 failed in 2.34s ====="), but pytest drops
# that padding when stdout is NOT a TTY -- which is exactly the case here,
# since this module always pipes the child's stdout (subprocess.PIPE) to
# stream/tail it, so the real-world line is plain "5 passed in 2.34s".
# Deliberately lenient (no "=" requirement) so detection works in both
# forms -- confirmed against a real, non-TTY pixi/pytest invocation.
_SUMMARY_LINE_RE = re.compile(r"\bin\s+\d+\.\d+s\b")
_PASSED_RE = re.compile(r"(\d+)\s+passed")
_FAILED_RE = re.compile(r"(\d+)\s+failed")


def _now_wall() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclasses.dataclass
class TestRunRecord:
    """The durable, structured shape of one test run -- persisted as JSON so
    a concurrent reader (a status CLI, CI log tailer, dashboard) always has
    a CURRENT snapshot to read instead of waiting for a completion-only
    file that, in the motivating hang bug, may never even get written."""

    run_id: str
    state: str = STATE_QUEUED
    command: "list[str]" = dataclasses.field(default_factory=list)
    cwd: str = ""
    worktree: "str | None" = None
    owner_session: "str | None" = None
    worker_count: "int | None" = None
    pid: "int | None" = None
    process_tree: "list[int]" = dataclasses.field(default_factory=list)
    collected_count: "int | None" = None
    created_at: float = 0.0
    started_monotonic: "float | None" = None
    last_progress_monotonic: "float | None" = None
    terminal_monotonic: "float | None" = None
    results_seen_monotonic: "float | None" = None
    started_at: "str | None" = None
    last_progress_at: "str | None" = None
    terminal_at: "str | None" = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    passed: "int | None" = None
    failed: "int | None" = None
    exit_code: "int | None" = None
    signal: "int | None" = None
    timeout_kind: "str | None" = None  # "wall_clock" | "stalled" | "post_results_hang" | None
    results_line_seen: bool = False
    cleanup: "dict[str, Any] | None" = None
    error: "str | None" = None
    superseded_run_id: "str | None" = None  # a PRIOR stale/superseded run THIS run took over from
    superseded_by: "str | None" = None  # set on the OLD record once a newer run reclaims/supersedes it

    def to_dict(self) -> "dict[str, Any]":
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: "dict[str, Any]") -> "TestRunRecord":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


def _write_record_atomic(state_path: Path, record: "TestRunRecord") -> None:
    """Atomic temp-file-then-``os.replace`` write -- mirrors
    ``process_registry.ProcessLeaseBroker._save``'s pattern exactly, so a
    crash mid-write can never leave a concurrent reader looking at a
    corrupt/partial JSON file."""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(state_path.parent), prefix=".test_run_state_", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record.to_dict(), fh)
            os.replace(tmp_name, state_path)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.remove(tmp_name)
            except OSError:
                pass
    except OSError:
        # Persistence is observability, never load-bearing for the test run
        # itself -- a disk failure here must not abort the run.
        pass


class TestRunTracker:
    """Owns a :class:`TestRunRecord` and streams it to disk on every
    meaningful update. ``clock`` is injectable (defaults to
    ``time.monotonic``) so tests can drive state transitions with a fake
    clock instead of real sleeps -- the same pattern
    ``process_registry.ProcessLeaseBroker`` already uses."""

    def __init__(
        self,
        state_path: Path,
        run_id: "str | None" = None,
        clock: "Callable[[], float] | None" = None,
    ) -> None:
        self.state_path = state_path
        self._clock = clock or time.monotonic
        self._last_output_save = float("-inf")
        self.record = TestRunRecord(run_id=run_id or uuid.uuid4().hex, created_at=time.time())

    def _touch_progress(self) -> None:
        self.record.last_progress_monotonic = self._clock()
        self.record.last_progress_at = _now_wall()

    def mark_queued(self) -> None:
        self.record.state = STATE_QUEUED
        self.save()

    def mark_starting(
        self, *, command: list[str], cwd: str, owner_session: "str | None" = None,
    ) -> None:
        self.record.state = STATE_STARTING
        self.record.command = list(command)
        self.record.cwd = cwd
        self.record.worktree = cwd
        self.record.owner_session = owner_session
        self.record.started_monotonic = self._clock()
        self.record.started_at = _now_wall()
        self._touch_progress()
        self.save()

    def mark_collecting(self) -> None:
        self.record.state = STATE_COLLECTING
        self._touch_progress()
        self.save()

    def mark_running(self, *, pid: "int | None", worker_count: "int | None") -> None:
        self.record.state = STATE_RUNNING
        self.record.pid = pid
        self.record.worker_count = worker_count
        self._touch_progress()
        self.save()

    def mark_stalled(self) -> None:
        if self.record.state not in TERMINAL_STATES:
            self.record.state = STATE_STALLED
        self.save()

    def mark_terminal(
        self,
        state: str,
        *,
        exit_code: "int | None" = None,
        signal: "int | None" = None,
        timeout_kind: "str | None" = None,
        error: "str | None" = None,
        cleanup: "dict[str, Any] | None" = None,
    ) -> None:
        if state not in TERMINAL_STATES:
            raise ValueError(f"not a terminal state: {state!r}")
        self.record.state = state
        self.record.exit_code = exit_code
        self.record.signal = signal
        self.record.timeout_kind = timeout_kind
        self.record.error = error
        if cleanup is not None:
            self.record.cleanup = cleanup
        self.record.terminal_monotonic = self._clock()
        self.record.terminal_at = _now_wall()
        self.save()

    def note_output(self, stream: str, text: str) -> None:
        """Called for every chunk read from the child's stdout/stderr --
        this IS the heartbeat/progress signal ('heartbeat/progress remains
        visible during collection and long tests'), plus bounded
        tail-buffer capture and results-summary detection (the mechanism
        that distinguishes 'still running' from 'printed results, now
        hung')."""
        self._touch_progress()
        tail_attr = "stdout_tail" if stream == "stdout" else "stderr_tail"
        combined = (getattr(self.record, tail_attr) + text)[-_TAIL_CHARS:]
        setattr(self.record, tail_attr, combined)

        newly_detected_results = False
        if not self.record.results_line_seen and _SUMMARY_LINE_RE.search(text):
            m_p = _PASSED_RE.search(text)
            m_f = _FAILED_RE.search(text)
            if m_p or m_f:
                self.record.results_line_seen = True
                self.record.results_seen_monotonic = self._clock()
                if m_p:
                    self.record.passed = int(m_p.group(1))
                if m_f:
                    self.record.failed = int(m_f.group(1))
                newly_detected_results = True

        now = self._clock()
        if newly_detected_results or (now - self._last_output_save) >= _OUTPUT_SAVE_THROTTLE_SECONDS:
            self._last_output_save = now
            self.save()

    def save(self) -> None:
        _write_record_atomic(self.state_path, self.record)

    @classmethod
    def load_record(cls, state_path: Path) -> "TestRunRecord | None":
        try:
            raw = state_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        try:
            return TestRunRecord.from_dict(data)
        except Exception:  # noqa: BLE001 -- corrupt/foreign JSON shape
            return None


# ---------------------------------------------------------------------------
# Process-tree cleanup -- reuses meridian.orphan_reaper's proven tree-safe
# kill/collect primitives (taskkill /F /T on Windows, psutil recursive
# terminate-then-kill on POSIX) when the meridian package is importable,
# with a small dependency-free fallback so a broken/uninstalled meridian
# package can never prevent a hung test run from being terminated.
#
# 2cebf4ae verifier finding: orphan_reaper's psutil-backed primitives are
# intentionally silent-degrade for THEIR OWN callers (reap_orphans etc) --
# kill_process_tree() returns False and report_tree_survivors() returns []
# when psutil is simply not installed, rather than raising. This repo's
# pixi env genuinely has no psutil in the base/CI install (it's gated
# behind the optional [semantic] pyproject extra), so calling straight
# into the reaper here would silently swallow that as "no survivors" /
# "kill failed" instead of engaging the dependency-free fallback below --
# a falsely clean cleanup receipt for a process that is, in fact, still
# running. Checking psutil importability BEFORE delegating to the reaper
# routes that exact case to the real (non-psutil) fallback instead.
# ---------------------------------------------------------------------------


def _psutil_importable() -> bool:
    try:
        import psutil  # type: ignore  # noqa: PLC0415,F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _process_tree_pids(pid: "int | None") -> list[int]:
    if pid is None:
        return []
    if _psutil_importable():
        try:
            from meridian import orphan_reaper as _reaper  # noqa: PLC0415

            return list(_reaper.collect_process_tree(pid))
        except Exception:  # noqa: BLE001
            pass
    try:
        import psutil  # type: ignore  # noqa: PLC0415

        proc = psutil.Process(pid)
        return [pid, *[c.pid for c in proc.children(recursive=True)]]
    except Exception:  # noqa: BLE001
        return [pid]


def _kill_process_tree(pid: "int | None") -> bool:
    if pid is None:
        return True
    if _psutil_importable():
        try:
            from meridian import orphan_reaper as _reaper  # noqa: PLC0415

            return bool(_reaper.kill_process_tree(pid))
        except Exception:  # noqa: BLE001
            pass
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, check=False, timeout=10,
            )
        else:
            try:
                os.killpg(pid, 15)
            except Exception:  # noqa: BLE001
                try:
                    os.kill(pid, 15)
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(1)
            try:
                os.killpg(pid, 9)
            except Exception:  # noqa: BLE001
                try:
                    os.kill(pid, 9)
                except Exception:  # noqa: BLE001
                    pass
        # taskkill /F (and POSIX SIGKILL) signal termination but don't
        # guarantee the OS has fully reaped the process by the time the
        # call returns -- an immediate single _pid_is_running check can be
        # a false negative. Under concurrent load (e.g. many xdist workers
        # each spawning+killing a process at once) process teardown itself
        # can take several real seconds, not milliseconds -- confirmed via
        # a real spawned-process test under -n auto where a naive 5s poll
        # window still occasionally missed the eventual exit. Poll for up
        # to 10s rather than trusting one instantaneous (or briefly
        # retried) sample.
        for _ in range(40):
            if not _pid_is_running(pid):
                return True
            time.sleep(0.25)
        return not _pid_is_running(pid)
    except Exception:  # noqa: BLE001
        return False


def _report_survivors(pids: "list[int]") -> list[int]:
    """Pure reporting of which of *pids* -- a tree the caller already knows
    it owns -- are still alive. Never kills anything itself and never
    touches a process outside this explicit list, so an unrelated process
    is never at risk. See the module-level comment above
    :func:`_psutil_importable` for why this checks psutil availability
    before delegating to ``orphan_reaper`` rather than trusting a silently
    psutil-degraded ``[]`` as if it meant "confirmed no survivors"."""
    if _psutil_importable():
        try:
            from meridian import orphan_reaper as _reaper  # noqa: PLC0415

            return list(_reaper.report_tree_survivors(pids))
        except Exception:  # noqa: BLE001
            pass
    return [p for p in pids if _pid_is_running(p)]


# ---------------------------------------------------------------------------
# Best-effort Meridian integration -- owner-session heartbeat + cross-tool
# process-lease registration. Both are pure observability: any failure
# (Meridian unreachable, package not importable, no session configured)
# degrades to a silent no-op and NEVER blocks or slows the test run itself.
# ---------------------------------------------------------------------------


def _maybe_heartbeat_owner_session() -> None:
    """If ``MERIDIAN_SESSION_ID`` is set, POST to the session heartbeat
    endpoint (``meridian.db.heartbeat_session`` / ``routes/sessions.py``)
    so a long local test run keeps its owning Meridian session's 30-minute
    idle TTL fresh without requiring ``log_task`` calls mid-run."""
    session_id = os.environ.get("MERIDIAN_SESSION_ID", "").strip()
    if not session_id:
        return
    base_url = os.environ.get("MERIDIAN_URL", "http://localhost:7878").rstrip("/")
    url = f"{base_url}/sessions/{session_id}/heartbeat"
    try:
        import urllib.request  # noqa: PLC0415

        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=5.0):  # noqa: S310 -- trusted local MERIDIAN_URL
            pass
    except Exception:  # noqa: BLE001
        pass


def _maybe_register_process_lease(pid: "int | None", run_id: str) -> "Callable[[], None]":
    """Best-effort: register this run's pytest child as a
    ``process_registry`` lease (315b0a63) so other tools sharing this
    machine (Codex, Cursor, a dashboard) can see it via
    ``python -m meridian.process_registry list`` without needing to know
    about this script specifically. Returns a zero-arg release callback
    that is always safe to call, even if registration itself silently
    failed."""
    if pid is None:
        return lambda: None
    try:
        from meridian import process_registry as _registry  # noqa: PLC0415

        broker = _registry.get_broker()
        lease = broker.register(
            "meridian-test-runner", pid, run_id=run_id, cwd=str(Path.cwd()),
        )

        def _release() -> None:
            try:
                broker.release("meridian-test-runner", lease.run_id)
            except Exception:  # noqa: BLE001
                pass

        return _release
    except Exception:  # noqa: BLE001
        return lambda: None


def _pump_stream(pipe: Any, out_stream: Any, tracker: "TestRunTracker", which: str) -> None:
    """Reader-thread body: forwards every line to the parent's own
    stdout/stderr (real-time output must never go silent) while feeding
    :meth:`TestRunTracker.note_output` for progress/heartbeat + tail-buffer
    + results-summary detection. Always a daemon thread -- must never be
    the thing blocking interpreter exit, which is exactly the class of bug
    (a leaked non-daemon thread) this item exists to make observable
    elsewhere, not reproduce here."""
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            try:
                out_stream.write(line)
                out_stream.flush()
            except Exception:  # noqa: BLE001
                pass
            try:
                tracker.note_output(which, line)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            pipe.close()
        except Exception:  # noqa: BLE001
            pass


def _run_pytest_observed(
    run_args: list[str],
    tracker: "TestRunTracker",
    *,
    wall_timeout: float,
    stall_timeout: float,
    post_results_grace: float,
    max_workers: int,
    poll_interval: float = 1.0,
    popen_factory: "Callable[..., Any] | None" = None,
    sleep_fn: "Callable[[float], None] | None" = None,
    clock: "Callable[[], float] | None" = None,
) -> int:
    """Spawn pytest, stream its output while tracking phase/progress, and
    classify the outcome truthfully -- including the two failure modes a
    completion-only ``subprocess.run`` call could never distinguish:
    a genuinely stuck/no-progress run (STALLED -> TIMED_OUT[stalled]) versus
    a run that already printed real results but never exited
    (TIMED_OUT[post_results_hang], classified PASSED/FAILED from the parsed
    counts since we DO have trustworthy results) versus a real wall-clock
    timeout (TIMED_OUT[wall_clock]) versus an honest crash (CRASHED, e.g. a
    negative/signal return code or pytest's own internal-error exit codes).
    """
    popen_factory = popen_factory or subprocess.Popen
    sleep_fn = sleep_fn or time.sleep
    clock = clock or time.monotonic

    cmd = [sys.executable, "-m", "pytest", *run_args]
    popen_kwargs: "dict[str, Any]" = dict(
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        cwd=tracker.record.cwd or None,
    )
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    else:
        popen_kwargs["start_new_session"] = True

    try:
        proc = popen_factory(cmd, **popen_kwargs)
    except Exception as exc:  # noqa: BLE001
        tracker.mark_terminal(STATE_CRASHED, error=f"failed to spawn pytest: {exc}")
        return 1

    worker_count = _infer_worker_count(run_args, max_workers)
    tracker.mark_running(pid=proc.pid, worker_count=worker_count)
    release_lease = _maybe_register_process_lease(proc.pid, tracker.record.run_id)
    tracker.record.process_tree = _process_tree_pids(proc.pid)

    threads = [
        threading.Thread(target=_pump_stream, args=(proc.stdout, sys.stdout, tracker, "stdout"), daemon=True),
        threading.Thread(target=_pump_stream, args=(proc.stderr, sys.stderr, tracker, "stderr"), daemon=True),
    ]
    for t in threads:
        t.start()

    outcome_state: "str | None" = None
    timeout_kind: "str | None" = None
    last_heartbeat = clock()

    try:
        while True:
            now = clock()
            if now - last_heartbeat >= _HEARTBEAT_INTERVAL_SECONDS:
                _maybe_heartbeat_owner_session()
                last_heartbeat = now

            returncode = proc.poll()
            if returncode is not None:
                break

            elapsed_since_start = now - (tracker.record.started_monotonic or now)
            elapsed_since_progress = now - (tracker.record.last_progress_monotonic or now)
            elapsed_since_results = (
                now - tracker.record.results_seen_monotonic
                if tracker.record.results_line_seen and tracker.record.results_seen_monotonic is not None
                else None
            )

            if elapsed_since_results is not None and elapsed_since_results > post_results_grace:
                # The exact motivating bug: results were already printed,
                # the process just never exited. We have real counts --
                # classify truthfully instead of hanging forever.
                outcome_state = STATE_FAILED if (tracker.record.failed or 0) > 0 else STATE_PASSED
                timeout_kind = "post_results_hang"
                break
            if elapsed_since_start > wall_timeout:
                outcome_state = STATE_TIMED_OUT
                timeout_kind = "wall_clock"
                break
            if elapsed_since_progress > stall_timeout:
                if tracker.record.state != STATE_STALLED:
                    tracker.mark_stalled()
                if elapsed_since_progress > stall_timeout * _STALL_ESCALATION_FACTOR:
                    outcome_state = STATE_TIMED_OUT
                    timeout_kind = "stalled"
                    break
            elif tracker.record.state == STATE_STALLED:
                tracker.mark_running(pid=proc.pid, worker_count=worker_count)  # recovered

            sleep_fn(poll_interval)
    except KeyboardInterrupt:
        outcome_state = STATE_CANCELLED
        timeout_kind = None

    if outcome_state is not None:
        # We decided to terminate the run ourselves (timeout / hang / Ctrl-C).
        killed_ok = _kill_process_tree(proc.pid)
        try:
            proc.wait(timeout=10.0)
        except Exception:  # noqa: BLE001
            pass
        survivors = _report_survivors(tracker.record.process_tree or [proc.pid])
        cleanup_receipt: "dict[str, Any]" = {
            "attempted": True, "method": "process_tree_kill",
            "kill_confirmed": bool(killed_ok), "survivors": survivors,
        }
        exit_code: "int | None" = None
        signal_val: "int | None" = None
    else:
        returncode = proc.returncode
        if returncode is not None and returncode < 0:
            signal_val = -returncode
            exit_code = None
        else:
            signal_val = None
            exit_code = returncode
        if returncode == 0:
            outcome_state = STATE_PASSED
        elif signal_val is not None:
            outcome_state = STATE_CRASHED
        elif returncode in (3, 4):  # pytest INTERNAL_ERROR / USAGE_ERROR
            outcome_state = STATE_CRASHED
        elif returncode == 2:  # pytest INTERRUPTED (e.g. Ctrl-C reached the child)
            outcome_state = STATE_CANCELLED
        else:
            outcome_state = STATE_FAILED
        # Bounded survivor sweep of OUR OWN spawned tree only (e.g. a
        # detached xdist worker that outlived its parent) -- never touches
        # an unrelated process; the pid list came from _process_tree_pids
        # scoped to THIS proc.pid.
        survivors = _report_survivors(tracker.record.process_tree or [])
        if survivors:
            for spid in survivors:
                _kill_process_tree(spid)
            sleep_fn(0.5)
            survivors_after = _report_survivors(survivors)
        else:
            survivors_after = []
        cleanup_receipt = {
            "attempted": bool(survivors), "method": "survivor_sweep" if survivors else "none_needed",
            "survivors": survivors_after,
        }

    for t in threads:
        t.join(timeout=5.0)
    release_lease()

    tracker.mark_terminal(
        outcome_state, exit_code=exit_code, signal=signal_val,
        timeout_kind=timeout_kind, cleanup=cleanup_receipt,
    )

    if outcome_state == STATE_PASSED:
        return exit_code if exit_code is not None else 0
    if outcome_state == STATE_TIMED_OUT:
        return 124
    if outcome_state == STATE_CANCELLED:
        return 130
    # FAILED / CRASHED
    return exit_code if exit_code else 1


def _record_superseded_stale_run(lock: "TestRunLock", tracker: "TestRunTracker") -> None:
    """After :meth:`TestRunLock.acquire` reclaims a stale lock (its owner
    pid is confirmed dead -- the Windows WinError-87 bug this item fixes
    made this path unreachable on Windows before), write TRUTHFUL crash
    evidence for the abandoned run instead of leaving its last-known state
    stuck at 'running'/'collecting' forever with no terminal outcome."""
    stale = TestRunTracker.load_record(lock.state_path)
    if stale is not None and stale.state not in TERMINAL_STATES:
        stale.state = STATE_CRASHED
        stale.error = (
            f"owner pid {lock.reclaimed_stale_pid} was no longer running "
            "-- stale lock reclaimed by a new run"
        )
        stale.terminal_at = _now_wall()
        stale.superseded_by = tracker.record.run_id
        _write_record_atomic(lock.state_path, stale)
        tracker.record.superseded_run_id = stale.run_id


def _print_duplicate_report(existing: "TestRunRecord | None", lock: "TestRunLock") -> None:
    if existing is not None:
        print(
            "Another Meridian test run is active for this repository "
            f"(run_id={existing.run_id}, state={existing.state}, pid={lock.owner_pid}, "
            f"started_at={existing.started_at}, last_progress_at={existing.last_progress_at}). "
            "Wait for it to finish, pass --supersede to take over explicitly (verifies "
            "liveness before terminating it), or set MERIDIAN_ALLOW_CONCURRENT_TESTS=1 "
            "for intentionally independent roots.",
            file=sys.stderr,
        )
    else:
        print(
            f"Another Meridian test run is active for this repository (pid={lock.owner_pid}, "
            "no run-state record found -- pre-existing lock or corrupt state file). Wait for "
            "it to finish, pass --supersede to take over explicitly, or set "
            "MERIDIAN_ALLOW_CONCURRENT_TESTS=1 for intentionally independent roots.",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--serial",
        action="store_true",
        help="Force serial execution (used for wall-clock-sensitive tests).",
    )
    parser.add_argument(
        "--supersede",
        action="store_true",
        help=(
            "If another Meridian test run is already active for this checkout, verify its "
            "owner process is genuinely alive and, if so, terminate its process tree and "
            "take over. Explicit opt-in only -- never automatic; by default a duplicate run "
            "is rejected, never silently run alongside another full gate."
        ),
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv)
    pytest_args = ns.pytest_args
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    if not pytest_args:
        pytest_args = ["tests/"]

    repo_root = Path.cwd().resolve()
    lock = TestRunLock(repo_root)
    tracker = TestRunTracker(lock.state_path)
    tracker.mark_queued()

    lock_owned_by_us = False
    if os.environ.get("MERIDIAN_ALLOW_CONCURRENT_TESTS") != "1":
        if lock.acquire():
            lock_owned_by_us = True
            if lock.reclaimed_stale_pid is not None:
                _record_superseded_stale_run(lock, tracker)
        else:
            existing = TestRunTracker.load_record(lock.state_path)
            if ns.supersede and lock.owner_pid_is_confirmed_alive():
                superseded_pid = lock.owner_pid
                _kill_process_tree(superseded_pid)
                if existing is not None and existing.state not in TERMINAL_STATES:
                    existing.state = STATE_CANCELLED
                    existing.error = (
                        f"superseded by an explicit --supersede takeover "
                        f"(new run_id={tracker.record.run_id})"
                    )
                    existing.terminal_at = _now_wall()
                    existing.superseded_by = tracker.record.run_id
                    _write_record_atomic(lock.state_path, existing)
                if lock.reclaim_after_supersede():
                    lock_owned_by_us = True
                    tracker.record.superseded_run_id = existing.run_id if existing else None
                else:
                    _print_duplicate_report(existing, lock)
                    return 2
            else:
                _print_duplicate_report(existing, lock)
                return 2

    try:
        serial_threshold = int(
            os.environ.get("MERIDIAN_TEST_SERIAL_THRESHOLD", DEFAULT_SERIAL_THRESHOLD)
        )
        max_workers = int(
            os.environ.get("MERIDIAN_TEST_MAX_WORKERS", DEFAULT_MAX_WORKERS)
        )
        tracker.mark_starting(
            command=pytest_args, cwd=str(repo_root),
            owner_session=os.environ.get("MERIDIAN_SESSION_ID") or None,
        )
        tracker.mark_collecting()
        collected, code = collect_count(pytest_args)
        if code:
            tracker.mark_terminal(STATE_CRASHED, exit_code=code, error="collection preflight failed")
            return code
        if collected is None:
            print("Could not determine collected test count; refusing to guess scheduling.", file=sys.stderr)
            tracker.mark_terminal(STATE_CRASHED, exit_code=2, error="could not determine collected test count")
            return 2
        tracker.record.collected_count = collected
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

        wall_timeout = float(
            os.environ.get("MERIDIAN_TEST_WALL_TIMEOUT_SECONDS", DEFAULT_WALL_TIMEOUT_SECONDS)
        )
        stall_timeout = float(
            os.environ.get("MERIDIAN_TEST_STALL_TIMEOUT_SECONDS", DEFAULT_STALL_TIMEOUT_SECONDS)
        )
        post_results_grace = float(
            os.environ.get(
                "MERIDIAN_TEST_POST_RESULTS_GRACE_SECONDS", DEFAULT_POST_RESULTS_GRACE_SECONDS
            )
        )
        return _run_pytest_observed(
            run_args, tracker,
            wall_timeout=wall_timeout, stall_timeout=stall_timeout,
            post_results_grace=post_results_grace, max_workers=max_workers,
        )
    finally:
        tracker.save()
        if lock_owned_by_us:
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
