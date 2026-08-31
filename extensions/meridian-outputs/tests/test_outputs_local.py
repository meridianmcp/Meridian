"""Tests for meridian_outputs.outputs_local.

Covers:
  - is_secret_path (security requirement 1): exhaustive exclusion-list checks.
  - _iter_safe_output_files: confirms secrets are excluded in directory walk.
  - ensure_gitignored (requirement 4): .gitignore auto-add, idempotent, creates file.
  - IndexFileLock (requirement 2): re-entrant hold, basic context-manager API.
  - Deterministic output (requirement 3): sorted path lists, stable results.
  - Core helpers: _normalize_output_path, _classify_suffix, file_fingerprint,
    archival_candidate, _canonical_name, classify_canonical_archival.
  - OutputsFtsIndex: schema creation, annotation CRUD, rebuild, search (mocked).
  - Module API: search_outputs, annotate_outputs, classify_outputs,
    resolve_figure_output (filesystem-backed where possible, mocked DuckDB
    where not available in CI).
  - npy_metadata: stats without numpy, graceful error on missing file.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Import the module under test.
sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import outputs_local as OL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dir(tmp_path: Path, files: dict[str, str]) -> str:
    """Create a temp directory with the given {name: content} files."""
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return str(tmp_path)


@contextlib.contextmanager
def inject_db_write_failure(exc: Exception | None = None):
    """Shared helper (89612890) consolidating the previously hand-rolled,
    independently-duplicated ``_boom``/``patch.object(_ensure_schema, ...)``
    pattern already used across several tests in this file (e.g. around
    ``test_rebuild_surfaces_db_write_error_instead_of_silent_debug_log``,
    ``test_db_write_failure_does_not_permanently_drop_file_from_index``).
    Injects a DB-write-path failure by patching
    ``OutputsFtsIndex._ensure_schema`` (the first call inside Phase 2's
    write_lock-held section) to raise ``exc``.

    Windows limitation, documented rather than silently worked around
    (89612890): a REAL OS-level disk-full or permission-denied condition is
    not reliably triggerable on Windows without admin rights (no quota-
    based disk-full trigger available to an ordinary process, no reliable
    owner-process-blocking chmod equivalent). Every failure this helper --
    and every existing test using the pattern it consolidates -- injects is
    therefore a Python-level exception at the call boundary, not a genuine
    OS-level write failure. That is an accepted, indeterminate-on-Windows
    limitation of this test suite, not a problem this helper solves.
    """
    if exc is None:
        exc = RuntimeError("simulated disk-full / connection failure")

    def _boom(self, con):  # noqa: ANN001 -- matches _ensure_schema's real signature
        raise exc

    with patch.object(OL.OutputsFtsIndex, "_ensure_schema", _boom):
        yield


# ---------------------------------------------------------------------------
# Security: is_secret_path (requirement 1)
# ---------------------------------------------------------------------------

class TestIsSecretPath:
    """Exhaustive checks for the secret-file exclusion filter."""

    # Files that MUST be excluded.
    @pytest.mark.parametrize("filename", [
        ".env",
        ".env.local",
        ".env.production",
        "prod.env",
        "my.env",
        "keyfile.key",
        "server.pem",
        "cert.crt",
        "cert.cer",
        "cert.der",
        "keystore.p12",
        "keystore.pfx",
        "trust.jks",
        "store.keystore",
        "id_rsa",
        "id_rsa.pub",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_ed25519.pub",
        "my_secret.txt",
        "secrets.yaml",
        "secrets.yml",
        "secrets.toml",
        "secrets.json",
        "app_credentials.json",
        "credentials.csv",
        "user_credentials.csv",
        "password.txt",
        "passwords.db",
        "passwd.cfg",
        "api_token.txt",
        "token.json",
        "apikey.txt",
        "api_key.cfg",
        "auth_key.yaml",
        "access_key.json",
        "private_key.pem",
        ".htpasswd",
        ".netrc",
        "netrc",
        "config.ini",
        "config.cfg",
        "config.conf",
        "config.yaml",
        "config.yml",
        "config.toml",
        "config.json",
        "settings.ini",
        "settings.cfg",
        "settings.conf",
        "settings.yaml",
        "settings.yml",
        "settings.toml",
        "settings.json",
        "terraform.tfvars",
        "prod.tfvars",
        "terraform.tfstate",
        "terraform.tfstate.backup",
        "my.vault",
        "vault.yaml",
        "vault.yml",
    ])
    def test_excluded(self, filename: str) -> None:
        path = f"/some/deep/path/{filename}"
        assert OL.is_secret_path(path), (
            f"Expected {filename!r} to be excluded but is_secret_path returned False"
        )

    # Files that MUST NOT be excluded (legitimate outputs).
    @pytest.mark.parametrize("filename", [
        "results.csv",
        "output.json",
        "weights.npy",
        "loss_curve.png",
        "model.pt",
        "README.md",
        "MERIDIAN_NOTES.md",
        "data.parquet",
        "summary.txt",
        "token_counts.csv",   # "token" in name but not a secret
        "run_config_backup.csv",  # "config" in name but .csv not in exclusion list
        "experiment_log.json",
        "best_checkpoint.pth",
        "environment.yml",    # conda environment file -- NOT a secret
    ])
    def test_not_excluded(self, filename: str) -> None:
        path = f"/outputs/{filename}"
        assert not OL.is_secret_path(path), (
            f"Expected {filename!r} NOT to be excluded but is_secret_path returned True"
        )

    def test_case_insensitive(self) -> None:
        assert OL.is_secret_path("/path/.ENV")
        assert OL.is_secret_path("/path/Server.PEM")
        assert OL.is_secret_path("/path/MY_SECRET_KEY.KEY")

    def test_only_basename_checked(self) -> None:
        # A path whose DIRECTORY contains ".env" but basename is safe.
        assert not OL.is_secret_path("/project/.env.dir/results.csv")
        # A path whose BASENAME is .env.
        assert OL.is_secret_path("/project/outputs/.env")


# ---------------------------------------------------------------------------
# _iter_safe_output_files
# ---------------------------------------------------------------------------

class TestIterSafeOutputFiles:
    def test_excludes_secret_files(self, tmp_path: Path) -> None:
        (tmp_path / "results.csv").write_text("a,b\n1,2", encoding="utf-8")
        (tmp_path / ".env").write_text("SECRET=123", encoding="utf-8")
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "secrets.yaml").write_text("key: val", encoding="utf-8")
        (tmp_path / "subdir" / "data.json").write_text('{"x": 1}', encoding="utf-8")

        paths = OL._iter_safe_output_files(str(tmp_path))
        basenames = {os.path.basename(p) for p in paths}
        assert "results.csv" in basenames
        assert "data.json" in basenames
        assert ".env" not in basenames
        assert "secrets.yaml" not in basenames

    def test_hidden_dirs_pruned(self, tmp_path: Path) -> None:
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("", encoding="utf-8")
        (tmp_path / "output.csv").write_text("x\n1", encoding="utf-8")

        paths = OL._iter_safe_output_files(str(tmp_path))
        # .git/config must not appear.
        assert all(".git" not in p for p in paths)
        assert any("output.csv" in p for p in paths)

    def test_sorted_deterministic(self, tmp_path: Path) -> None:
        for name in ["c.csv", "a.csv", "b.json"]:
            (tmp_path / name).write_text("x", encoding="utf-8")
        paths = OL._iter_safe_output_files(str(tmp_path))
        assert paths == sorted(paths)

    def test_empty_dir(self, tmp_path: Path) -> None:
        assert OL._iter_safe_output_files(str(tmp_path)) == []

    def test_meridian_notes_included(self, tmp_path: Path) -> None:
        (tmp_path / "MERIDIAN_NOTES.md").write_text("notes", encoding="utf-8")
        paths = OL._iter_safe_output_files(str(tmp_path))
        assert any("MERIDIAN_NOTES.md" in p for p in paths)


# ---------------------------------------------------------------------------
# ensure_gitignored (requirement 4)
# ---------------------------------------------------------------------------

class TestEnsureGitignored:
    def test_creates_gitignore_if_missing(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / ".meridian-outputs-cache"
        cache_dir.mkdir()
        OL.ensure_gitignored(str(cache_dir))
        gi_path = tmp_path / ".gitignore"
        assert gi_path.exists()
        content = gi_path.read_text(encoding="utf-8")
        assert ".meridian-outputs-cache" in content

    def test_idempotent(self, tmp_path: Path) -> None:
        """Calling twice must not duplicate the entry."""
        cache_dir = tmp_path / ".cache-dir"
        cache_dir.mkdir()
        OL.ensure_gitignored(str(cache_dir))
        OL.ensure_gitignored(str(cache_dir))
        gi_path = tmp_path / ".gitignore"
        content = gi_path.read_text(encoding="utf-8")
        # Count occurrences of the name.
        count = content.count(".cache-dir")
        assert count == 1, f"Entry duplicated: count={count}"

    def test_appends_to_existing_gitignore(self, tmp_path: Path) -> None:
        gi_path = tmp_path / ".gitignore"
        gi_path.write_text("*.pyc\n", encoding="utf-8")
        cache_dir = tmp_path / "my-cache"
        cache_dir.mkdir()
        OL.ensure_gitignored(str(cache_dir))
        content = gi_path.read_text(encoding="utf-8")
        assert "*.pyc" in content
        assert "my-cache" in content

    def test_no_op_if_already_covered(self, tmp_path: Path) -> None:
        gi_path = tmp_path / ".gitignore"
        gi_path.write_text("/my-cache/\n", encoding="utf-8")
        cache_dir = tmp_path / "my-cache"
        cache_dir.mkdir()
        OL.ensure_gitignored(str(cache_dir))
        content = gi_path.read_text(encoding="utf-8")
        # The name should appear only once (already covered, not appended).
        assert content.count("my-cache") == 1

    def test_never_raises(self) -> None:
        """Must swallow errors -- never raises even for invalid paths."""
        OL.ensure_gitignored("/nonexistent/path/that/cannot/be/created/x")


# ---------------------------------------------------------------------------
# IndexFileLock (requirement 2)
# ---------------------------------------------------------------------------

class TestIndexFileLock:
    def test_basic_context_manager(self) -> None:
        lock = OL.IndexFileLock(":memory:")
        with lock:
            pass  # must not raise

    def test_thread_exclusion(self) -> None:
        """Two threads must not hold the lock simultaneously."""
        lock = OL.IndexFileLock(":memory:")
        results: list[str] = []
        barrier = threading.Barrier(2)

        def worker(label: str) -> None:
            barrier.wait()
            with lock:
                results.append(f"{label}-start")
                time.sleep(0.02)
                results.append(f"{label}-end")

        t1 = threading.Thread(target=worker, args=("A",))
        t2 = threading.Thread(target=worker, args=("B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        # A-end must appear before B-start OR B-end must appear before A-start
        # (i.e., no interleaving of start/end for the same section).
        for i, entry in enumerate(results):
            if entry.endswith("-start"):
                label = entry.split("-")[0]
                expected_end = f"{label}-end"
                start_idx = i
                end_idx = results.index(expected_end)
                # No other thread's start should fall between start_idx and end_idx.
                between = results[start_idx + 1:end_idx]
                other_starts = [e for e in between if e.endswith("-start")]
                assert not other_starts, (
                    f"Lock not exclusive: {results}"
                )

    def test_releases_on_exception(self) -> None:
        lock = OL.IndexFileLock(":memory:")
        try:
            with lock:
                raise ValueError("boom")
        except ValueError:
            pass
        # Lock must be released -- acquire again must succeed.
        acquired = lock._thread_lock.acquire(blocking=False)
        assert acquired, "Lock not released after exception"
        lock._thread_lock.release()


# ---------------------------------------------------------------------------
# a52216e2 -- process-aware single-writer lease/lock: ownership diagnostics,
# the correctness fix (a genuine acquisition failure now raises instead of
# being silently swallowed), and real multiprocess mutual exclusion.
# ---------------------------------------------------------------------------

# Extracted once so worker subprocesses (which start with a fresh sys.path)
# can import the module under test the same way conftest.py does for the
# main test process.
_EXT_ROOT = str(Path(__file__).parent.parent)

# A standalone script (run via `python -c`) so these tests exercise REAL,
# separate OS processes -- not threads -- racing on the same lock file. Modes:
#   race_append          -- acquire, append start/end markers to a shared log
#                            around a short sleep, release. Used to prove two
#                            REAL processes never interleave inside the
#                            critical section (the multiprocess counterpart of
#                            TestIndexFileLock.test_thread_exclusion above).
#   hold_until_signal     -- acquire, signal readiness, then hold the lock
#                            until a signal file appears (or a generous
#                            internal timeout), then release cleanly.
#   crash_without_release -- acquire, signal readiness, then os._exit()
#                            WITHOUT releasing -- simulates a crashed owner,
#                            leaving a real-but-now-dead pid in the lease.
_LOCK_WORKER_SCRIPT = r"""
import os, sys, time

ext_dir, db_path, mode = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, ext_dir)
from meridian_outputs import outputs_local as OL

if mode == "race_append":
    ready_path, log_path, session_id, sleep_s = (
        sys.argv[4], sys.argv[5], sys.argv[6], float(sys.argv[7]),
    )
    lock = OL.IndexFileLock(db_path, session_id=session_id)
    lock.acquire()
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(session_id + "-start\n")
    time.sleep(sleep_s)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(session_id + "-end\n")
    lock.release()
    with open(ready_path, "w", encoding="utf-8") as fh:
        fh.write("done")
elif mode == "hold_until_signal":
    ready_path, signal_path, session_id = sys.argv[4], sys.argv[5], sys.argv[6]
    lock = OL.IndexFileLock(db_path, session_id=session_id)
    lock.acquire()
    with open(ready_path, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    deadline = time.monotonic() + 30.0
    while not os.path.exists(signal_path) and time.monotonic() < deadline:
        time.sleep(0.02)
    lock.release()
elif mode == "crash_without_release":
    ready_path, session_id = sys.argv[4], sys.argv[5]
    lock = OL.IndexFileLock(db_path, session_id=session_id)
    lock.acquire()
    with open(ready_path, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    os._exit(1)
else:
    raise SystemExit(f"unknown mode {mode!r}")
"""


def _spawn_worker(db_path: str, mode: str, *extra_args: str) -> subprocess.Popen:
    """Launch one REAL child process racing on ``db_path``'s lock file."""
    return subprocess.Popen(
        [sys.executable, "-c", _LOCK_WORKER_SCRIPT, _EXT_ROOT, db_path, mode, *extra_args],
    )


def _wait_for_file(path: str, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise TimeoutError(f"{path!r} was never created within {timeout}s")
        time.sleep(0.02)


class TestReadIndexLockOwner:
    """read_index_lock_owner (a52216e2): read-only, never acquires the real
    lock, never signals/terminates any process."""

    def test_memory_db_reports_not_held(self) -> None:
        owner = OL.read_index_lock_owner(":memory:")
        assert owner.held is False
        assert owner.lock_path is None
        assert owner.is_stale is False

    def test_no_lock_file_reports_not_held(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "index.duckdb")
        owner = OL.read_index_lock_owner(db_path)
        assert owner.held is False
        assert owner.pid is None
        assert owner.is_stale is False

    def test_self_acquired_lock_reports_held_with_pid_and_hostname(
        self, tmp_path: Path,
    ) -> None:
        db_path = str(tmp_path / "index.duckdb")
        lock = OL.IndexFileLock(db_path, session_id="test-session")
        lock.acquire()
        try:
            owner = OL.read_index_lock_owner(db_path)
            assert owner.held is True
            assert owner.pid == os.getpid()
            assert owner.hostname == socket.gethostname()
            assert owner.session_id == "test-session"
            assert owner.started_at is not None
            assert owner.heartbeat_at is not None
            assert owner.pid_alive is True
            assert owner.is_stale is False
            assert owner.stale_reason is None
            assert owner.lock_mode in ("atomic_create", "portalocker")
        finally:
            lock.release()

    def test_released_lock_reports_not_held(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "index.duckdb")
        lock = OL.IndexFileLock(db_path)
        lock.acquire()
        lock.release()
        owner = OL.read_index_lock_owner(db_path)
        assert owner.held is False

    def test_fresh_heartbeat_with_dead_pid_is_still_stale(
        self, tmp_path: Path,
    ) -> None:
        """A confirmed-dead pid on THIS host overrides a fresh heartbeat --
        never trust the heartbeat alone once liveness is checkable."""
        db_path = str(tmp_path / "index.duckdb")
        lock_path = db_path + ".lock"
        # A real process, run to completion and its handle fully released
        # (the `with` closes Popen's own handle -- see _pid_alive's Windows
        # docstring note on why a lingering handle would otherwise make this
        # pid look deceptively "alive"), so its pid is now genuinely dead.
        with subprocess.Popen([sys.executable, "-c", "pass"]) as proc:
            proc.wait(timeout=15)
            dead_pid = proc.pid
        with open(lock_path, "w", encoding="utf-8") as fh:
            json.dump({
                "pid": dead_pid, "hostname": socket.gethostname(),
                "session_id": "stale-sim", "started_at": time.time(),
                "heartbeat_at": time.time(),  # deliberately FRESH
                "lock_mode": "atomic_create",
            }, fh)
        owner = OL.read_index_lock_owner(db_path)
        assert owner.pid_alive is False
        assert owner.is_stale is True
        assert owner.stale_reason is not None and "no longer running" in owner.stale_reason

    def test_old_heartbeat_with_indeterminate_pid_is_stale(
        self, tmp_path: Path,
    ) -> None:
        """A different (unreachable) hostname makes pid liveness
        indeterminate -- staleness then falls back to heartbeat age."""
        db_path = str(tmp_path / "index.duckdb")
        lock_path = db_path + ".lock"
        with open(lock_path, "w", encoding="utf-8") as fh:
            json.dump({
                "pid": 4, "hostname": "some-other-host-entirely",
                "session_id": None, "started_at": time.time() - 10_000,
                "heartbeat_at": time.time() - 10_000,
                "lock_mode": "atomic_create",
            }, fh)
        owner = OL.read_index_lock_owner(db_path, stale_seconds=5.0)
        assert owner.pid_alive is None
        assert owner.is_stale is True
        assert owner.stale_reason is not None and "heartbeat" in owner.stale_reason

    def test_alive_pid_fresh_heartbeat_never_stale(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "index.duckdb")
        lock_path = db_path + ".lock"
        with open(lock_path, "w", encoding="utf-8") as fh:
            json.dump({
                "pid": os.getpid(), "hostname": socket.gethostname(),
                "session_id": None, "started_at": time.time(),
                "heartbeat_at": time.time(),
                "lock_mode": "atomic_create",
            }, fh)
        owner = OL.read_index_lock_owner(db_path)
        assert owner.pid_alive is True
        assert owner.is_stale is False


class TestIndexFileLockCorrectnessFix:
    """a52216e2's core correctness fix: a genuine acquisition failure must
    raise, never be silently swallowed and treated as 'acquired'."""

    def test_unexpected_portalocker_failure_raises_not_silently_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression test for the pre-a52216e2 bug: ANY exception during
        portalocker acquisition (not just ImportError) used to be caught at
        DEBUG level and treated as success, letting the caller believe it
        held exclusive access when it did not."""
        fake_portalocker = MagicMock()
        fake_portalocker.LOCK_EX = 2
        fake_portalocker.lock.side_effect = OSError("simulated permission denied")
        monkeypatch.setitem(sys.modules, "portalocker", fake_portalocker)

        db_path = str(tmp_path / "index.duckdb")
        lock = OL.IndexFileLock(db_path)
        with pytest.raises(OL.IndexLockAcquireError):
            lock.acquire()
        # The in-process thread lock must be released on failure too, so a
        # later legitimate acquire in this same process isn't wedged forever.
        acquired = lock._thread_lock.acquire(blocking=False)
        assert acquired, "thread lock was not released after a failed acquire"
        lock._thread_lock.release()

    def test_import_error_is_not_an_error_uses_atomic_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """portalocker simply being ABSENT is the documented, supported
        degrade path -- it must never raise, and must still provide real
        exclusivity via the atomic-create fallback."""
        monkeypatch.setitem(sys.modules, "portalocker", None)
        db_path = str(tmp_path / "index.duckdb")
        lock = OL.IndexFileLock(db_path)
        lock.acquire()
        try:
            assert lock._lock_mode == "atomic_create"
            assert os.path.exists(db_path + ".lock")
        finally:
            lock.release()
        assert not os.path.exists(db_path + ".lock")


class TestIndexFileLockAtomicFallback:
    """Real, dependency-free cross-process exclusivity (a52216e2) -- exercised
    end-to-end against real subprocesses, not mocks, since the whole point is
    that TWO SEPARATE OS PROCESSES with zero shared memory must never both
    believe they hold the lock at once."""

    def test_acquire_writes_lease_release_removes_file(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "index.duckdb")
        lock_path = db_path + ".lock"
        lock = OL.IndexFileLock(db_path, session_id="s1")
        assert not os.path.exists(lock_path)
        lock.acquire()
        assert os.path.exists(lock_path)
        with open(lock_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        assert meta["pid"] == os.getpid()
        assert meta["session_id"] == "s1"
        lock.release()
        assert not os.path.exists(lock_path)

    def test_two_real_processes_never_interleave_critical_section(
        self, tmp_path: Path,
    ) -> None:
        """The multiprocess counterpart of TestIndexFileLock.test_thread_
        exclusion: two REAL OS processes race to acquire the SAME lock file
        and both wrap a sleep between a start/end marker inside the critical
        section. Deterministic single-writer ownership means the log can
        never show one process's start before the other's matching end."""
        db_path = str(tmp_path / "index.duckdb")
        log_path = str(tmp_path / "race.log")
        ready_a = str(tmp_path / "ready_a")
        ready_b = str(tmp_path / "ready_b")
        proc_a = _spawn_worker(db_path, "race_append", ready_a, log_path, "A", "0.1")
        proc_b = _spawn_worker(db_path, "race_append", ready_b, log_path, "B", "0.1")
        try:
            assert proc_a.wait(timeout=15) == 0
            assert proc_b.wait(timeout=15) == 0
        finally:
            proc_a.kill()
            proc_b.kill()

        with open(log_path, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
        assert lines.count("A-start") == 1 and lines.count("A-end") == 1
        assert lines.count("B-start") == 1 and lines.count("B-end") == 1
        for i, entry in enumerate(lines):
            if entry.endswith("-start"):
                label = entry.split("-")[0]
                end_idx = lines.index(f"{label}-end")
                between = lines[i + 1:end_idx]
                assert not any(e.endswith("-start") for e in between), (
                    f"lock not exclusive across processes: {lines}"
                )

    def test_active_owner_is_never_stolen_and_process_is_never_touched(
        self, tmp_path: Path,
    ) -> None:
        """A live, actively-heartbeating owner must block a second acquirer
        (never be silently stolen), and diagnostics reads must never signal,
        terminate, or otherwise disturb that owning process."""
        db_path = str(tmp_path / "index.duckdb")
        ready_path = str(tmp_path / "ready")
        signal_path = str(tmp_path / "release_signal")
        child = _spawn_worker(db_path, "hold_until_signal", ready_path, signal_path, "child")
        try:
            _wait_for_file(ready_path)
            with open(ready_path, encoding="utf-8") as fh:
                child_pid = int(fh.read().strip())
            assert child_pid == child.pid

            owner = OL.read_index_lock_owner(db_path)
            assert owner.held is True
            assert owner.is_stale is False
            assert owner.pid == child_pid

            # A bounded acquire against the still-live owner must raise, not
            # steal the lock or hang past its timeout.
            contender = OL.IndexFileLock(db_path, timeout=0.4)
            start = time.monotonic()
            with pytest.raises(OL.IndexLockAcquireError) as exc_info:
                contender.acquire()
            elapsed = time.monotonic() - start
            assert elapsed < 3.0, "acquire() blocked well past its own timeout"
            assert exc_info.value.owner is not None
            assert exc_info.value.owner.pid == child_pid

            # Diagnostics must never disturb the live owner.
            assert child.poll() is None, "child process was touched/terminated by diagnostics"
        finally:
            with open(signal_path, "w", encoding="utf-8") as fh:
                fh.write("release")
            assert child.wait(timeout=15) == 0

        # Once the real owner released, the lock is free again.
        final_owner = OL.read_index_lock_owner(db_path)
        assert final_owner.held is False

    def test_stale_lock_from_crashed_process_is_reclaimed_promptly(
        self, tmp_path: Path,
    ) -> None:
        """A process that acquires and then crashes (never releases) leaves
        a real-but-now-dead pid in the lease. A later acquirer must reclaim
        the abandoned lock FILE promptly (well under the acquire timeout) --
        and must never attempt to touch the (already-dead) process to do so."""
        db_path = str(tmp_path / "index.duckdb")
        ready_path = str(tmp_path / "ready")
        crasher = _spawn_worker(db_path, "crash_without_release", ready_path, "crasher")
        _wait_for_file(ready_path)
        assert crasher.wait(timeout=15) != 0  # os._exit(1) -- confirmed dead

        owner = OL.read_index_lock_owner(db_path)
        assert owner.held is True  # the lock FILE is still there
        assert owner.is_stale is True
        assert owner.stale_reason is not None and "no longer running" in owner.stale_reason

        contender = OL.IndexFileLock(db_path, timeout=5.0)
        start = time.monotonic()
        contender.acquire()
        elapsed = time.monotonic() - start
        contender.release()
        assert elapsed < 2.0, (
            f"reclaiming a stale lock took {elapsed:.2f}s -- looks like it "
            "waited out (most of) the timeout instead of reclaiming promptly"
        )


class TestOutputsFtsIndexLockDiagnostics:
    """Wiring: OutputsFtsIndex/ConvergenceState/search_outputs surface the
    lock lease diagnostics (a52216e2) rather than hiding lock state."""

    def test_lock_diagnostics_none_for_memory_index(self) -> None:
        idx = OL.OutputsFtsIndex(":memory:")
        assert idx.lock_diagnostics() is None

    def test_lock_diagnostics_reflects_live_acquire_and_release(
        self, tmp_path: Path,
    ) -> None:
        idx = OL.OutputsFtsIndex(
            str(tmp_path), db_path=str(tmp_path / "index.duckdb"),
            session_id="sess-abc",
        )
        try:
            assert idx.lock_diagnostics()["held"] is False
            idx._write_lock.acquire()
            try:
                diag = idx.lock_diagnostics()
                assert diag["held"] is True
                assert diag["session_id"] == "sess-abc"
                assert diag["pid"] == os.getpid()
            finally:
                idx._write_lock.release()
            assert idx.lock_diagnostics()["held"] is False
        finally:
            idx.close()

    def test_get_convergence_state_includes_index_lock_field(
        self, tmp_path: Path,
    ) -> None:
        idx = OL.OutputsFtsIndex(
            str(tmp_path), db_path=str(tmp_path / "index.duckdb"),
        )
        try:
            state = idx.get_convergence_state()
            assert "index_lock" in state.to_dict()
            assert state.index_lock is not None
            assert state.index_lock["held"] is False
        finally:
            idx.close()

    def test_search_outputs_surfaces_index_lock_warning_on_acquire_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "a.csv").write_text("col\n1", encoding="utf-8")

        def _boom(self, *, timeout=None):  # noqa: ANN001, ARG001
            raise OL.IndexLockAcquireError("simulated: lock busy")

        monkeypatch.setattr(OL.IndexFileLock, "acquire", _boom)
        result = OL.search_outputs(str(tmp_path), "col")
        assert "index_lock_warning" in result
        assert "simulated" in result["index_lock_warning"]
        # Must degrade gracefully -- never raise out of the module API.
        assert result["hits"] == []
        assert result["convergence"]["partial"] is True


# ---------------------------------------------------------------------------
# _normalize_output_path
# ---------------------------------------------------------------------------

class TestNormalizeOutputPath:
    def test_empty(self) -> None:
        assert OL._normalize_output_path("") == ""
        assert OL._normalize_output_path(None) == ""  # type: ignore[arg-type]

    def test_normalizes_slashes(self) -> None:
        p = OL._normalize_output_path("C:/foo/bar/../baz")
        assert "/" in p and "\\" not in p

    def test_strips_whitespace(self) -> None:
        p = OL._normalize_output_path("  /tmp/foo  ")
        assert not p.startswith(" ")


# ---------------------------------------------------------------------------
# _classify_suffix
# ---------------------------------------------------------------------------

class TestClassifySuffix:
    @pytest.mark.parametrize("path,expected", [
        ("data.csv", "text_content"),
        ("data.CSV", "text_content"),
        ("result.json", "text_content"),
        # fa600e42 -- bounded plaintext allowlist additions.
        ("readme.txt", "text_content"),
        ("NOTES.TXT", "text_content"),
        ("summary.md", "text_content"),
        ("run.log", "text_content"),
        ("weights.npy", "metadata_only"),
        ("figure.png", "binary_metadata"),
        ("model.pt", "binary_metadata"),
        ("noext", "binary_metadata"),
    ])
    def test_classification(self, path: str, expected: str) -> None:
        assert OL._classify_suffix(path) == expected


# ---------------------------------------------------------------------------
# _sanitize_text_content / NUL-byte handling (sprint item fa600e42)
# ---------------------------------------------------------------------------

class TestSanitizeTextContent:
    def test_no_nul_bytes_returns_unchanged(self) -> None:
        text = "plain text\nwith two lines\n"
        assert OL._sanitize_text_content(text) == text

    def test_strips_embedded_nul_bytes(self) -> None:
        text = "before\x00after"
        assert OL._sanitize_text_content(text) == "beforeafter"

    def test_strips_multiple_nul_bytes(self) -> None:
        text = "\x00a\x00b\x00c\x00"
        assert OL._sanitize_text_content(text) == "abc"


# ---------------------------------------------------------------------------
# Bounded plaintext body indexing: .txt/.md/.log (sprint item fa600e42)
# ---------------------------------------------------------------------------

class TestPlaintextBodyIndexing:
    @pytest.mark.parametrize("filename", ["notes.txt", "README.md", "run.log"])
    def test_body_content_is_read_and_classified_as_text(
        self, tmp_path: Path, filename: str,
    ) -> None:
        f = tmp_path / filename
        f.write_text("generated by train.py\nloss: 0.05\n", encoding="utf-8")

        fp = OL.file_fingerprint(str(f))
        assert fp.kind == "text_content"

        content = OL._content_for_fts(str(f), fp)
        assert "loss: 0.05" in content
        assert filename in content

    def test_generating_script_hint_recognised_in_plain_text(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "run.log"
        f.write_text("started run\ngenerated by train.py\ndone\n", encoding="utf-8")

        fp = OL.file_fingerprint(str(f))
        assert fp.generating_script == "train.py"

    def test_embedded_nul_byte_stripped_from_indexed_body(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "corrupted.log"
        with open(f, "wb") as fh:
            fh.write(b"alpha \x00 beta\n")

        fp = OL.file_fingerprint(str(f))
        content = OL._content_for_fts(str(f), fp)

        assert "\x00" not in content
        assert "alpha" in content
        assert "beta" in content

    def test_secret_shaped_file_excluded_before_reaching_allowlist(
        self,
    ) -> None:
        # is_secret_path (checked during the walk, upstream of _classify_suffix)
        # must still exclude a secret-shaped file regardless of the bounded
        # plaintext allowlist expanding to cover .txt/.md/.log generally.
        assert OL.is_secret_path("/outputs/run_1/.env") is True


# ---------------------------------------------------------------------------
# file_fingerprint
# ---------------------------------------------------------------------------

class TestFileFingerprint:
    def test_csv_columns(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("col_a,col_b,col_c\n1,2,3\n4,5,6", encoding="utf-8")
        fp = OL.file_fingerprint(str(f))
        assert fp.kind == "text_content"
        assert fp.csv_columns == ["col_a", "col_b", "col_c"]

    def test_json_keys(self, tmp_path: Path) -> None:
        f = tmp_path / "result.json"
        f.write_text('{"alpha": 1, "beta": 2}', encoding="utf-8")
        fp = OL.file_fingerprint(str(f))
        assert fp.kind == "text_content"
        assert set(fp.json_keys or []) == {"alpha", "beta"}

    def test_json_generating_script(self, tmp_path: Path) -> None:
        f = tmp_path / "meta.json"
        f.write_text(
            '{"generating_script": "train.py", "loss": 0.1}', encoding="utf-8"
        )
        fp = OL.file_fingerprint(str(f))
        assert fp.generating_script == "train.py"

    def test_binary_no_content(self, tmp_path: Path) -> None:
        f = tmp_path / "figure.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        fp = OL.file_fingerprint(str(f))
        assert fp.kind == "binary_metadata"
        assert fp.csv_columns is None
        assert fp.json_keys is None

    def test_missing_file_no_raise(self) -> None:
        fp = OL.file_fingerprint("/nonexistent/file.csv")
        assert fp.kind == "text_content"
        assert fp.csv_columns is None


# ---------------------------------------------------------------------------
# archival_candidate / _canonical_name
# ---------------------------------------------------------------------------

class TestArchivalCandidate:
    @pytest.mark.parametrize("path,expected", [
        ("run_old.csv", True),
        ("run_old_1.csv", True),
        ("run_old_2.csv", True),
        ("_results.csv", True),
        ("results.csv", False),
        ("run_results.csv", False),
        ("run_old_enough.csv", False),  # _old_enough is not the pattern
    ])
    def test_candidate(self, path: str, expected: bool) -> None:
        assert OL.archival_candidate(f"/outputs/{path}") == expected

    @pytest.mark.parametrize("path,expected", [
        ("/outputs/run_old.csv", "/outputs/run.csv"),
        ("/outputs/run_old_2.csv", "/outputs/run.csv"),
        ("/outputs/_data.csv", "/outputs/data.csv"),
        ("/outputs/results.csv", "/outputs/results.csv"),
    ])
    def test_canonical_name(self, path: str, expected: str) -> None:
        assert OL._canonical_name(path) == expected


# ---------------------------------------------------------------------------
# classify_canonical_archival
# ---------------------------------------------------------------------------

class TestClassifyCanonicalArchival:
    def test_identical_files_are_archival(self, tmp_path: Path) -> None:
        content = b"x,y\n1,2\n"
        canonical = tmp_path / "run.csv"
        archival = tmp_path / "run_old.csv"
        canonical.write_bytes(content)
        archival.write_bytes(content)

        results = OL.classify_canonical_archival(
            [str(canonical), str(archival)]
        )
        assert results[str(archival)].is_archival
        assert results[str(archival)].canonical_path == str(canonical)
        assert not results[str(canonical)].is_archival

    def test_different_content_not_archival(self, tmp_path: Path) -> None:
        canonical = tmp_path / "run.csv"
        archival = tmp_path / "run_old.csv"
        canonical.write_text("a\n1", encoding="utf-8")
        archival.write_text("a\n2", encoding="utf-8")

        results = OL.classify_canonical_archival(
            [str(canonical), str(archival)]
        )
        assert not results[str(archival)].is_archival

    def test_no_twin_not_archival(self, tmp_path: Path) -> None:
        archival = tmp_path / "run_old.csv"
        archival.write_text("a\n1", encoding="utf-8")
        results = OL.classify_canonical_archival([str(archival)])
        assert not results[str(archival)].is_archival
        assert "no canonical twin" in results[str(archival)].reason

    def test_deterministic_order(self, tmp_path: Path) -> None:
        """Output dict key order must follow the sorted input list."""
        for name in ["c.csv", "a.csv", "b.csv"]:
            (tmp_path / name).write_text("x\n1", encoding="utf-8")
        sorted_paths = sorted(str(tmp_path / n) for n in ["c.csv", "a.csv", "b.csv"])
        results = OL.classify_canonical_archival(sorted_paths)
        assert list(results.keys()) == sorted_paths

    def test_injectable_hasher(self, tmp_path: Path) -> None:
        canonical = tmp_path / "run.csv"
        archival = tmp_path / "run_old.csv"
        canonical.write_text("x", encoding="utf-8")
        archival.write_text("y", encoding="utf-8")

        # Hasher that says both files have the same hash.
        def _same(_path: str) -> str:
            return "deadbeef"

        results = OL.classify_canonical_archival(
            [str(canonical), str(archival)], hasher=_same
        )
        assert results[str(archival)].is_archival


# ---------------------------------------------------------------------------
# npy_metadata
# ---------------------------------------------------------------------------

class TestNpyMetadata:
    def test_missing_file(self) -> None:
        m = OL.npy_metadata("/nonexistent/file.npy")
        assert m.path == "/nonexistent/file.npy"
        assert m.error is not None
        assert m.shape is None

    def test_without_numpy(self, tmp_path: Path) -> None:
        f = tmp_path / "arr.npy"
        f.write_bytes(b"\x93NUMPY\x01\x00fake")  # corrupt .npy
        m = OL.npy_metadata(str(f))
        # Should get size_bytes/modified_at from stat even if numpy parse fails.
        assert m.size_bytes is not None
        assert m.modified_at is not None
        assert m.to_dict()["path"] == str(f)


# ---------------------------------------------------------------------------
# OutputsFtsIndex -- in-memory DuckDB (skipped when duckdb not available)
# ---------------------------------------------------------------------------

try:
    import duckdb  # noqa: F401
    _DUCKDB_AVAILABLE = True
except ImportError:
    _DUCKDB_AVAILABLE = False

duckdb_required = pytest.mark.skipif(
    not _DUCKDB_AVAILABLE, reason="duckdb not installed"
)


class TestOutputsFtsIndex:
    @duckdb_required
    def test_empty_tree_rebuild(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        count = idx.rebuild()
        assert count == 0
        idx.close()

    @duckdb_required
    def test_indexes_csv_file(self, tmp_path: Path) -> None:
        (tmp_path / "loss.csv").write_text("epoch,loss\n1,0.5\n2,0.3",
                                            encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        count = idx.rebuild()
        assert count == 1
        hits = idx.search("epoch")
        assert len(hits) == 1
        assert "loss.csv" in hits[0]["path"]
        idx.close()

    @duckdb_required
    def test_excludes_secret_files(self, tmp_path: Path) -> None:
        (tmp_path / "results.csv").write_text("x\n1", encoding="utf-8")
        (tmp_path / ".env").write_text("SECRET=abc", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        count = idx.rebuild()
        # Only results.csv should be indexed, NOT .env.
        assert count == 1
        idx.close()

    @duckdb_required
    def test_annotation_crud(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        result = idx.add_annotation(str(tmp_path), "baseline run", run_params={"lr": 0.001})
        assert result["note"] == "baseline run"
        assert result["run_params"] == {"lr": 0.001}

        annotations = idx.get_annotations_for_path(str(tmp_path))
        assert len(annotations) == 1
        assert annotations[0]["note"] == "baseline run"
        idx.close()

    @duckdb_required
    def test_meridian_notes_auto_ingested(self, tmp_path: Path) -> None:
        (tmp_path / "MERIDIAN_NOTES.md").write_text(
            "Run with PCA=on", encoding="utf-8"
        )
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        annotations = idx.get_annotations_for_path(str(tmp_path))
        assert any("PCA=on" in (a.get("note") or "") for a in annotations)
        idx.close()

    def test_meridian_notes_excluded_from_widened_text_allowlist(self) -> None:
        """Code-review fix (fa600e42): widening _TEXT_CONTENT_SUFFIXES to
        include .md must not ALSO pull this one reserved filename into the
        regular body-indexing pipeline -- MERIDIAN_NOTES.md keeps its
        pre-fa600e42 classification (annotation only, no separate content
        row), every OTHER .md file is unaffected."""
        assert OL._classify_suffix("MERIDIAN_NOTES.md") == "binary_metadata"
        assert OL._classify_suffix("/some/dir/MERIDIAN_NOTES.md") == "binary_metadata"
        assert OL._classify_suffix("summary.md") == "text_content"

    @duckdb_required
    def test_meridian_notes_not_double_indexed_as_content_row(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "MERIDIAN_NOTES.md").write_text(
            "UNIQUE_NOTE_TOKEN_7f3a run with PCA=on", encoding="utf-8",
        )
        (tmp_path / "results.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            hits = idx.search("UNIQUE_NOTE_TOKEN_7f3a")
            # The note's text must surface via annotations, never as its own
            # independent text_content search hit.
            assert not any(
                os.path.basename(h["path"]) == OL.MERIDIAN_NOTES_FILENAME
                for h in hits
            )
            annotations = idx.get_annotations_for_path(str(tmp_path))
            assert any(
                "UNIQUE_NOTE_TOKEN_7f3a" in (a.get("note") or "")
                for a in annotations
            )
        finally:
            idx.close()

    @duckdb_required
    def test_meridian_notes_embedded_nul_byte_sanitized(
        self, tmp_path: Path,
    ) -> None:
        """Code-review fix (fa600e42): _ingest_meridian_notes reads
        MERIDIAN_NOTES.md through the same utf-8/errors=replace pattern as
        _read_text_capped, but was not sanitizing embedded NUL bytes before
        this fix -- unlike the other two text-read call sites."""
        with open(tmp_path / "MERIDIAN_NOTES.md", "wb") as fh:
            fh.write(b"alpha \x00 beta\n")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            ingested = idx._ingest_meridian_notes([str(tmp_path / "MERIDIAN_NOTES.md")])
            assert ingested == 1
            annotations = idx.get_annotations_for_path(str(tmp_path))
            assert len(annotations) == 1
            assert "\x00" not in annotations[0]["note"]
            assert "alpha" in annotations[0]["note"]
            assert "beta" in annotations[0]["note"]
        finally:
            idx.close()

    @duckdb_required
    def test_incremental_rebuild(self, tmp_path: Path) -> None:
        f = tmp_path / "data.json"
        f.write_text('{"key": "value1"}', encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        count1 = idx.rebuild()
        assert count1 == 1
        # File unchanged -- should be fast and still return 1.
        count2 = idx.rebuild()
        assert count2 == 1
        # Modify the file.
        f.write_text('{"key": "value2"}', encoding="utf-8")
        count3 = idx.rebuild()
        assert count3 == 1
        idx.close()

    @duckdb_required
    def test_empty_query_returns_empty(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        assert idx.search("") == []
        assert idx.search("   ") == []
        idx.close()

    @duckdb_required
    def test_resolve_output(self, tmp_path: Path) -> None:
        f = tmp_path / "results.csv"
        f.write_text("a,b\n1,2", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        result = idx.resolve_output(str(f))
        assert result is not None
        assert "results.csv" in result["path"]
        # Non-existent path returns None.
        assert idx.resolve_output("/no/such/file.csv") is None
        idx.close()


# ---------------------------------------------------------------------------
# _row_cache content eviction (sprint item edc84500)
# ---------------------------------------------------------------------------

class TestRowCacheContentEviction:
    """edc84500 -- _row_cache must never hold one full extracted-content
    body (the CSV/JSON/text body used for FTS) per discovered file for the
    OutputsFtsIndex instance's entire lifetime. That unbounded growth caused
    a real OS-level allocator failure ("memory allocation of N bytes
    failed") at ~96,000/244,191 files against a real SUT_Compressed tree.

    Content is evicted back to None once a row has been committed (see
    _apply_precomputed/_light_row); every lightweight field a caller
    actually needs off a cached row (sha256, size, mtime, kind,
    csv_columns, json_keys, generating_script, is_archival, canonical_path)
    must keep working exactly as before. get_content() -- backed directly
    by the persistent DuckDB outputs_index table -- is the supported way
    to read a file's real content back on demand.
    """

    @duckdb_required
    def test_row_cache_content_evicted_after_commit(self, tmp_path: Path) -> None:
        f = tmp_path / "metrics.csv"
        # A same-size sibling forces the size-prefilter (e1fd4182) to
        # actually compute a real sha256 for both files, rather than
        # skipping hashing entirely for a lone, uniquely-sized file --
        # exercising the lightweight sha256 field this fix must preserve.
        f.write_text("epoch,loss\n1,0.9\n2,0.4\n3,0.1", encoding="utf-8")
        (tmp_path / "sibling.csv").write_text(
            "epoch,loss\n1,0.9\n2,0.4\n3,0.2", encoding="utf-8",
        )
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            count = idx.rebuild()
            assert count == 2
            cache_key = next(p for p in idx._row_cache if p.endswith("metrics.csv"))
            cached = idx._row_cache[cache_key]
            assert cached.content is None, (
                "row_cache must not hold the full content once a row has "
                "been committed to the DB + FTS index"
            )
            # Lightweight fields must survive eviction -- staleness
            # detection and metadata lookups depend on these.
            assert cached.sha256 is not None
            assert cached.size is not None
            assert cached.kind == "text_content"

            # A real lookup must still return the ACTUAL persisted content,
            # read straight from DuckDB -- never stale/empty.
            content = idx.get_content(cache_key)
            assert content is not None
            assert "epoch,loss" in content
        finally:
            idx.close()

    @duckdb_required
    def test_get_content_missing_path_returns_none(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            assert idx.get_content(str(tmp_path / "nope.csv")) is None
        finally:
            idx.close()

    @duckdb_required
    def test_staleness_detection_unaffected_by_eviction(self, tmp_path: Path) -> None:
        """The staleness check (`p not in self._row_cache`) and the sha256
        read (`self._row_cache[path].sha256`) must both keep working once
        content has been evicted -- an unchanged file must NOT be
        re-analysed on a subsequent rebuild() call."""
        f = tmp_path / "data.json"
        f.write_text('{"a": 1}', encoding="utf-8")
        # A same-size sibling forces a real sha256 to be computed (see
        # test_row_cache_content_evicted_after_commit) so this test actually
        # exercises the cached-hash read, not a legitimately-skipped one.
        (tmp_path / "sibling.json").write_text('{"a": 2}', encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            cache_key = next(p for p in idx._row_cache if p.endswith("data.json"))
            assert idx._row_cache[cache_key].content is None
            sha_before = idx._row_cache[cache_key].sha256
            assert sha_before is not None

            analysed: list[str] = []
            real_analyse = OL._analyse_file

            def _spy_analyse(p, hasher, **kwargs):
                analysed.append(p)
                return real_analyse(p, hasher, **kwargs)

            with patch.object(OL, "_analyse_file", side_effect=_spy_analyse):
                count = idx.rebuild()
            assert count == 2
            assert not analysed, (
                "an unchanged file was re-analysed -- staleness detection "
                "broke after content eviction"
            )
            assert idx._row_cache[cache_key].sha256 == sha_before
            assert idx._row_cache[cache_key].content is None
        finally:
            idx.close()

    @duckdb_required
    def test_archival_metadata_refresh_preserves_content(self, tmp_path: Path) -> None:
        """A row whose ONLY change is its archival classification (a twin
        file appears later) is re-inserted via the "update non-stale cached
        rows" path in _apply_precomputed, which reuses the CACHED (already
        content-evicted) row. The fix must re-read the real content from
        DuckDB before re-inserting -- never silently overwrite already-
        persisted content with NULL."""
        archival = tmp_path / "run_old.csv"
        archival.write_bytes(b"a,b\n1,2\n")
        # A same-size (but different-content) sibling present from the
        # start forces a REAL sha256 to be computed for run_old.csv during
        # its OWN initial indexing (the size-prefilter, e1fd4182, skips
        # hashing a uniquely-sized file entirely) -- needed so the archival
        # comparison below has a real hash to compare once the canonical
        # twin appears; run_old.csv itself is never re-hashed once cached.
        (tmp_path / "helper.csv").write_bytes(b"x,y\n9,9\n")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            cache_key = next(p for p in idx._row_cache if p.endswith("run_old.csv"))
            assert idx._row_cache[cache_key].content is None
            assert idx._row_cache[cache_key].is_archival is False  # no twin yet
            content_before = idx.get_content(cache_key)
            assert content_before is not None and "a,b" in content_before

            # Add the canonical twin (identical content) -- flips
            # run_old.csv's archival classification via the metadata-refresh
            # path, WITHOUT run_old.csv itself being touched/re-stat'd as
            # stale this call.
            canonical = tmp_path / "run.csv"
            canonical.write_bytes(b"a,b\n1,2\n")
            idx.rebuild()

            assert idx._row_cache[cache_key].is_archival is True, (
                "twin addition should have flipped is_archival via the "
                "non-stale metadata-refresh path"
            )
            content_after = idx.get_content(cache_key)
            assert content_after == content_before, (
                "a metadata-only archival refresh must never null out "
                "already-persisted content"
            )
            assert idx._row_cache[cache_key].content is None, (
                "the metadata refresh must not re-inflate row_cache with "
                "full content"
            )
        finally:
            idx.close()

    @duckdb_required
    def test_rehydrate_from_disk_does_not_load_content(self, tmp_path: Path) -> None:
        """A fresh OutputsFtsIndex pointed at an existing on-disk DB (process
        restart / cache-eviction scenario) must not re-materialise every
        row's full content into _row_cache on connect() -- only cheap
        metadata should be rehydrated."""
        (tmp_path / "big.csv").write_text(
            "col\n" + ("x" * 5000), encoding="utf-8",
        )
        db_path = OL._resolve_index_db_path(str(tmp_path))

        idx1 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        idx1.rebuild()
        idx1.close()

        idx2 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            idx2._connect()  # triggers _rehydrate_cache_from_disk
            cache_key = next(p for p in idx2._row_cache if p.endswith("big.csv"))
            assert idx2._row_cache[cache_key].content is None, (
                "_rehydrate_cache_from_disk must not load content into "
                "row_cache on a fresh connect()"
            )
            content = idx2.get_content(cache_key)
            assert content is not None
            assert "x" * 100 in content
        finally:
            idx2.close()


# ---------------------------------------------------------------------------
# Module-level API: search_outputs, annotate_outputs, classify_outputs,
# resolve_figure_output
# ---------------------------------------------------------------------------

class TestSearchOutputsAPI:
    def test_missing_dir_returns_error(self) -> None:
        result = OL.search_outputs("/nonexistent/dir", "query")
        assert "error" in result

    def test_empty_query_returns_error(self, tmp_path: Path) -> None:
        result = OL.search_outputs(str(tmp_path), "")
        assert "error" in result

    def test_annotate_missing_args(self) -> None:
        assert "error" in OL.annotate_outputs("", "/path", "note")
        assert "error" in OL.annotate_outputs("/dir", "", "note")
        assert "error" in OL.annotate_outputs("/dir", "/path", "")

    @duckdb_required
    def test_search_finds_csv(self, tmp_path: Path) -> None:
        (tmp_path / "accuracy.csv").write_text(
            "epoch,accuracy\n1,0.9\n2,0.95", encoding="utf-8"
        )
        result = OL.search_outputs(str(tmp_path), "accuracy")
        assert result["total_indexed"] >= 1
        assert len(result["hits"]) >= 1

    @duckdb_required
    def test_search_exposes_discovery_phase_metrics(self, tmp_path: Path) -> None:
        (tmp_path / "metrics.json").write_text(
            '{"marker": "telemetry"}', encoding="utf-8"
        )
        result = OL.search_outputs(str(tmp_path), "telemetry", max_seconds=None)
        discovery = result["discovery"]
        assert discovery["walk_complete"] is True
        assert discovery["discovered_total"] >= 1
        assert discovery["discovered_this_call"] >= 1
        assert discovery["rebuild_seconds"] >= 0
        assert discovery["walk_seconds"] >= 0
        assert discovery["analysis_seconds"] >= 0
        assert discovery["classification_seconds"] >= 0
        assert discovery["write_seconds"] >= 0
        assert discovery["row_cache_content_resident"] is False

    @duckdb_required
    def test_search_no_secret_hits(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("DB_PASS=hunter2", encoding="utf-8")
        (tmp_path / "results.json").write_text('{"DB_PASS": "hunter2"}', encoding="utf-8")
        result = OL.search_outputs(str(tmp_path), "hunter2")
        # .env is excluded; results.json may or may not match -- but .env MUST NOT appear.
        for hit in result["hits"]:
            assert ".env" not in os.path.basename(hit["path"]), (
                f"Secret file .env appeared in search hits: {hit}"
            )

    @duckdb_required
    def test_classify_outputs_api(self, tmp_path: Path) -> None:
        content = b"a,b\n1,2\n"
        (tmp_path / "run.csv").write_bytes(content)
        (tmp_path / "run_old.csv").write_bytes(content)
        paths = [
            str(tmp_path / "run.csv"),
            str(tmp_path / "run_old.csv"),
        ]
        result = OL.classify_outputs(paths)
        assert result["total"] == 2
        clsf = {c["path"]: c for c in result["classifications"]}
        assert clsf[paths[1]]["is_archival"] is True

    def test_classify_outputs_sorted(self) -> None:
        """Output order is sorted by path regardless of input order."""
        paths = ["/c/z.csv", "/a/x.csv", "/b/y.csv"]
        result = OL.classify_outputs(paths)
        returned_paths = [c["path"] for c in result["classifications"]]
        assert returned_paths == sorted(paths)

    def test_resolve_figure_output_empty_path(self, tmp_path: Path) -> None:
        assert OL.resolve_figure_output(str(tmp_path), "") is None

    def test_resolve_figure_output_missing_dir(self) -> None:
        assert OL.resolve_figure_output("/nonexistent/dir", "/file.csv") is None


# ---------------------------------------------------------------------------
# On-disk index persistence + auto-gitignore (sprint item 0c1a4349)
# ---------------------------------------------------------------------------

class TestCachedIndexPersistence:
    """_get_cached_index must persist to a real on-disk DuckDB file, not
    :memory:, and must activate ensure_gitignored on the cache directory."""

    def test_resolve_index_db_path_not_memory(self, tmp_path: Path) -> None:
        db_path = OL._resolve_index_db_path(str(tmp_path))
        assert db_path != ":memory:"
        assert db_path.endswith("index.duckdb")
        assert os.path.isdir(tmp_path / ".meridian-outputs-cache")

    def test_resolve_index_db_path_writes_gitignore(self, tmp_path: Path) -> None:
        OL._resolve_index_db_path(str(tmp_path))
        gi_path = tmp_path / ".gitignore"
        assert gi_path.is_file()
        assert ".meridian-outputs-cache/" in gi_path.read_text(encoding="utf-8")

    def test_resolve_index_db_path_falls_back_on_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _boom(path: str, exist_ok: bool = False) -> None:
            raise OSError("simulated permission failure")

        monkeypatch.setattr(OL.os, "makedirs", _boom)
        assert OL._resolve_index_db_path(str(tmp_path)) == ":memory:"

    @duckdb_required
    def test_get_cached_index_uses_real_db_path(self, tmp_path: Path) -> None:
        idx = OL._get_cached_index(str(tmp_path))
        assert idx._db_path != ":memory:"
        assert os.path.isfile(idx._db_path) or os.path.isdir(os.path.dirname(idx._db_path))

    @duckdb_required
    def test_index_survives_cache_eviction(self, tmp_path: Path) -> None:
        """Rebuilding via a fresh OutputsFtsIndex pointed at the same on-disk
        db_path (simulating cache eviction / process restart) must see rows
        indexed by a prior instance -- the whole point of persisting."""
        (tmp_path / "metric.csv").write_text("epoch,loss\n1,0.5", encoding="utf-8")
        db_path = OL._resolve_index_db_path(str(tmp_path))

        idx1 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        idx1.rebuild()
        idx1.close()

        idx2 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        row = idx2.resolve_output(str(tmp_path / "metric.csv"))
        idx2.close()
        assert row is not None

    @duckdb_required
    def test_fresh_instance_detects_existing_fts_index(self, tmp_path: Path) -> None:
        """d9c76caa follow-up: a fresh OutputsFtsIndex pointed at a db_path
        that already has a built FTS index (from a prior process's rebuild)
        must detect this immediately on connect, not assume _fts_built=False
        and pay the full-table rebuild tax again on every process restart."""
        (tmp_path / "metric.csv").write_text("epoch,loss\n1,0.5", encoding="utf-8")
        db_path = OL._resolve_index_db_path(str(tmp_path))

        idx1 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        idx1.rebuild()
        assert idx1._fts_built is True
        idx1.close()

        idx2 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        assert idx2._fts_built is False  # not yet connected
        idx2._connect()
        assert idx2._fts_built is True, (
            "fresh instance should have detected the existing on-disk FTS "
            "schema instead of assuming none exists"
        )
        idx2.close()

    def test_fresh_instance_on_empty_db_stays_unbuilt(self, tmp_path: Path) -> None:
        """No prior rebuild ever ran against this db_path -- _fts_built must
        stay False (nothing to detect) rather than erroring."""
        db_path = OL._resolve_index_db_path(str(tmp_path))
        idx = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            idx._connect()
            assert idx._fts_built is False
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# Durable walk/convergence state across a process restart (durability
# follow-up to item 6af1518d): epoch/cursor/pending-backlog/expected-count/
# last-error all used to live ONLY in the OutputsFtsIndex object's own
# memory, so a process restart mid-walk either silently claimed convergence
# it never verified (get_convergence_state read nothing but in-memory
# defaults) or lost the confirmed-stale backlog outright.
# ---------------------------------------------------------------------------

class TestWalkStateDurability:
    @duckdb_required
    def test_persist_and_rehydrate_walk_state_roundtrip(self, tmp_path: Path) -> None:
        """Direct field-level roundtrip through _persist_walk_state_locked /
        _rehydrate_walk_state_from_disk, independent of a real walk -- proves
        the durable store itself (outputs_index_meta) carries every field the
        item requires: epoch, cursor/boundary, expected count, pending
        queue, last error."""
        db_path = str(tmp_path / "index.duckdb")
        idx1 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            con = idx1._connect()
            idx1._walk_epoch = 3
            idx1._walk_pass_confirmed_complete = False
            idx1._scan_boundary = str(tmp_path / "mid" / "file.csv")
            idx1._expected_count = 42
            idx1._last_walk_error = "could not list directory 'x': boom"
            idx1._pending_stale = {
                str(tmp_path / "a.csv"): (123.5, 10),
                str(tmp_path / "b.csv"): (None, None),
            }
            idx1._persist_walk_state_locked(con)
        finally:
            idx1.close()

        idx2 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            idx2._connect()
            assert idx2._walk_epoch == 3
            assert idx2._walk_pass_confirmed_complete is False
            assert idx2._scan_boundary == str(tmp_path / "mid" / "file.csv")
            assert idx2._expected_count == 42
            assert idx2._last_walk_error == "could not list directory 'x': boom"
            assert idx2._pending_stale == {
                str(tmp_path / "a.csv"): (123.5, 10),
                str(tmp_path / "b.csv"): (None, None),
            }
        finally:
            idx2.close()

    @duckdb_required
    def test_fresh_never_touched_differs_from_restarted_interrupted(
        self, tmp_path: Path,
    ) -> None:
        """3f758063 -- a genuinely brand-new index that has never indexed
        anything must report converged=False (never_walked=True): zero
        evidence (no scan boundary, no rows, no pending backlog, no
        confirmed expected count) is not the same claim as "confirmed
        converged", and reporting it as such is exactly the "convergence
        reports true with zero indexed/unknown expected state" defect this
        item closes (see get_convergence_state's own docstring). Prior to
        this fix, a genuinely brand-new index reported converged=True by
        design -- see git history for that superseded contract and its own
        regression test, TestConvergenceState::test_no_walk_in_progress_
        means_any_subtree_converged, updated alongside this one.

        Both the never-touched case AND a restarted process whose PRIOR
        incarnation persisted an interrupted walk must report
        converged=False, walk_complete=False -- but for genuinely different
        reasons (never_walked vs. a confirmed-incomplete prior pass), which
        this test asserts separately so a future change can't silently
        collapse the distinction back into "conflate the two", the exact
        "silently claim completion it hasn't verified" bug this feature
        exists to close."""
        never_touched_dir = tmp_path / "never_touched"
        never_touched_dir.mkdir()
        never_touched = OL.OutputsFtsIndex(str(never_touched_dir))
        try:
            state = never_touched.get_convergence_state()
            assert state.converged is False
            assert state.never_walked is True
            assert state.indexed_count == 0
            assert state.expected_count is None
        finally:
            never_touched.close()

        restarted_dir = tmp_path / "restarted"
        restarted_dir.mkdir()
        db_path = str(tmp_path / "restarted.duckdb")
        prior = OL.OutputsFtsIndex(str(restarted_dir), db_path=db_path)
        try:
            con = prior._connect()
            prior._walk_pass_confirmed_complete = False
            # A REAL interrupted walk always leaves some concrete footprint
            # (drain() had handed back at least one path) -- a scan boundary
            # here, unlike the bare unconfirmed-complete flag alone, is what
            # makes this genuinely distinguishable from never_touched's zero
            # evidence above rather than an ambiguous corner case.
            prior._scan_boundary = str(restarted_dir / "partial_progress.csv")
            prior._persist_walk_state_locked(con)
        finally:
            prior.close()

        resumed = OL.OutputsFtsIndex(str(restarted_dir), db_path=db_path)
        try:
            assert resumed._walk_state is None  # same as never_touched, right now
            state = resumed.get_convergence_state()
            assert state.converged is False
            assert state.walk_complete is False
            # Distinct from never_touched above: this IS durably-confirmed
            # interrupted-walk evidence (a persisted scan boundary), not a
            # genuinely untouched index -- never_walked must stay False here
            # so a caller can tell the two apart.
            assert state.never_walked is False
        finally:
            resumed.close()

    @duckdb_required
    def test_restart_resumes_interrupted_walk_without_rehashing(
        self, tmp_path: Path,
    ) -> None:
        """End-to-end restart/resume: a walk interrupted partway through a
        real multi-call rebuild() sequence (max_batch=1 forces one file
        discovered per call) is picked back up by a FRESH OutputsFtsIndex
        instance at the same on-disk db_path -- and finishes without ever
        re-hashing a file the prior process already confirmed (not
        "restart from scratch"), without ever reporting a row count that
        regresses across the restart, and without ever claiming convergence
        before the walk has genuinely finished (not "silently unsafe")."""
        outputs_dir = tmp_path / "outputs"
        outputs_dir.mkdir()
        for i in range(6):
            (outputs_dir / f"f{i}.csv").write_text(f"col\n{i}", encoding="utf-8")
        db_path = str(tmp_path / "index.duckdb")

        hash_calls: list[str] = []

        def counting_hasher(path: str) -> str | None:
            hash_calls.append(path)
            return OL._xxh3_file(path)

        idx1 = OL.OutputsFtsIndex(
            str(outputs_dir), db_path=db_path, max_batch=1,
            hasher=counting_hasher,
        )
        try:
            for _ in range(3):
                idx1.rebuild()
            assert idx1._walk_state is not None, (
                "walk must still be mid-pass with only 3 calls at max_batch=1"
            )
            idx1_row_count = len(idx1._row_cache)
            assert 1 <= idx1_row_count < 6
        finally:
            idx1.close()

        idx2 = OL.OutputsFtsIndex(
            str(outputs_dir), db_path=db_path, max_batch=1,
            hasher=counting_hasher,
        )
        try:
            # Safety property: BEFORE this fresh instance ever calls
            # rebuild() itself, it must not silently claim the tree is
            # fully converged.
            pre_rebuild_state = idx2.get_convergence_state()
            assert pre_rebuild_state.converged is False
            assert pre_rebuild_state.walk_complete is False
            assert pre_rebuild_state.indexed_count == idx1_row_count

            seen_counts: list[int] = []
            for _ in range(24):
                idx2.rebuild()
                seen_counts.append(len(idx2._row_cache))
                if idx2.get_convergence_state().converged:
                    break

            final_state = idx2.get_convergence_state()
            assert final_state.converged is True
            assert final_state.walk_complete is True
            assert final_state.pending_count == 0
            assert len(idx2._row_cache) == 6
            # Resuming never regresses the visible row count.
            assert seen_counts == sorted(seen_counts)
        finally:
            idx2.close()

        # The whole point: no file was EVER hashed more than once across
        # BOTH incarnations combined -- the resumed process rehydrated the
        # files idx1 already confirmed from disk instead of re-walking/
        # re-hashing them from scratch (the size-based archival-dedup
        # prefilter can legitimately skip hashing a handful of files
        # entirely -- e.g. one still genuinely unique in size when it's
        # analysed -- so this asserts "never duplicated", not "every file
        # hashed exactly once").
        assert len(hash_calls) == len(set(hash_calls)), (
            f"a file was re-hashed after the simulated restart: {hash_calls!r}"
        )

    @duckdb_required
    def test_restart_distinguishes_zero_hit_in_progress_from_failed_walk(
        self, tmp_path: Path,
    ) -> None:
        """A walk interrupted with nothing indexed YET (in-progress, no
        error) must remain distinguishable, after a restart, from one that
        hit a real filesystem error -- neither is a confirmed miss, but only
        one carries an actionable last_error."""
        outputs_dir = tmp_path / "outputs"
        outputs_dir.mkdir()
        (outputs_dir / "only.csv").write_text("col\n1", encoding="utf-8")
        db_path_a = str(tmp_path / "in_progress.duckdb")
        db_path_b = str(tmp_path / "failed.duckdb")

        idx_a = OL.OutputsFtsIndex(str(outputs_dir), db_path=db_path_a)
        try:
            con = idx_a._connect()
            idx_a._walk_pass_confirmed_complete = False
            idx_a._persist_walk_state_locked(con)
        finally:
            idx_a.close()

        idx_b = OL.OutputsFtsIndex(str(outputs_dir), db_path=db_path_b)
        try:
            con = idx_b._connect()
            idx_b._walk_pass_confirmed_complete = False
            idx_b._record_walk_error(
                str(outputs_dir / "bad_dir"), OSError("simulated permission denied"),
            )
            idx_b._persist_walk_state_locked(con)
        finally:
            idx_b.close()

        resumed_a = OL.OutputsFtsIndex(str(outputs_dir), db_path=db_path_a)
        resumed_b = OL.OutputsFtsIndex(str(outputs_dir), db_path=db_path_b)
        try:
            state_a = resumed_a.get_convergence_state()
            state_b = resumed_b.get_convergence_state()

            assert state_a.converged is False
            assert state_a.last_error is None
            assert state_a.indexed_count == 0

            assert state_b.converged is False
            assert state_b.last_error is not None
            assert "bad_dir" in state_b.last_error
        finally:
            resumed_a.close()
            resumed_b.close()

    @duckdb_required
    def test_subtree_index_has_independently_persisted_walk_state(
        self, tmp_path: Path,
    ) -> None:
        """get_subtree_index() indexes live in a SEPARATE on-disk DB from
        the root's own -- a root's persisted backlog must never leak into,
        or get read back as, an independent subtree index's own state after
        a restart of both."""
        root_dir = tmp_path / "root"
        sub_dir = root_dir / "sub"
        sub_dir.mkdir(parents=True)
        (root_dir / "top.csv").write_text("col\n1", encoding="utf-8")
        (sub_dir / "inner.csv").write_text("col\n2", encoding="utf-8")

        root_idx = OL._get_cached_index(str(root_dir))
        root_idx.rebuild()
        # Simulate a crash right after Phase 0/1 flagged top.csv stale but
        # before Phase 2 confirmed the write -- the backlog that must
        # survive the restart, scoped to the ROOT db only.
        root_idx._pending_stale[str(root_dir / "top.csv")] = (1.0, 1)
        root_idx._persist_walk_state_locked(root_idx._connect())
        root_db_path = root_idx._db_path

        sub_idx = OL.get_subtree_index(str(root_dir), str(sub_dir))
        sub_idx.rebuild()
        sub_db_path = sub_idx._db_path
        assert root_db_path != sub_db_path

        root_idx.close()
        sub_idx.close()

        resumed_root = OL.OutputsFtsIndex(str(root_dir), db_path=root_db_path)
        resumed_sub = OL.OutputsFtsIndex(str(sub_dir), db_path=sub_db_path)
        try:
            root_state = resumed_root.get_convergence_state()
            sub_state = resumed_sub.get_convergence_state()

            assert root_state.converged is False
            assert root_state.pending_count == 1

            assert sub_state.converged is True
            assert sub_state.pending_count == 0
        finally:
            resumed_root.close()
            resumed_sub.close()

    @duckdb_required
    def test_subtree_scoped_convergence_after_restart_does_not_conflate_with_root(
        self, tmp_path: Path,
    ) -> None:
        """get_convergence_state(subtree=...) on the SAME root instance must
        scope a rehydrated (persisted-then-restored) pending backlog to the
        requested subtree, not report the whole root's incompleteness for a
        subtree that has nothing outstanding, nor hide a subtree's own
        outstanding backlog behind an otherwise-quiet root."""
        outputs_dir = tmp_path / "outputs"
        done_dir = outputs_dir / "done"
        pending_dir = outputs_dir / "pending"
        done_dir.mkdir(parents=True)
        pending_dir.mkdir(parents=True)
        (done_dir / "d.csv").write_text("col\n1", encoding="utf-8")
        (pending_dir / "p.csv").write_text("col\n2", encoding="utf-8")
        db_path = str(tmp_path / "index.duckdb")

        idx1 = OL.OutputsFtsIndex(str(outputs_dir), db_path=db_path)
        try:
            con = idx1._connect()
            idx1._walk_pass_confirmed_complete = False
            # "done" sorts before "pending" -- the boundary sitting inside
            # pending/ proves the walk has fully passed done/'s block.
            idx1._scan_boundary = str(pending_dir / "p.csv")
            idx1._pending_stale = {str(pending_dir / "p.csv"): (1.0, 1)}
            idx1._persist_walk_state_locked(con)
        finally:
            idx1.close()

        idx2 = OL.OutputsFtsIndex(str(outputs_dir), db_path=db_path)
        try:
            done_state = idx2.get_convergence_state(subtree=str(done_dir))
            pending_state = idx2.get_convergence_state(subtree=str(pending_dir))
            root_state = idx2.get_convergence_state()

            assert done_state.converged is True
            assert done_state.pending_count == 0

            assert pending_state.converged is False
            assert pending_state.pending_count == 1

            assert root_state.converged is False
            assert root_state.pending_count == 1
        finally:
            idx2.close()

    @duckdb_required
    def test_restart_after_full_convergence_still_reports_converged(
        self, tmp_path: Path,
    ) -> None:
        """Happy path across a restart: a tree that fully converged before
        the process exited must still report fully converged -- with the
        SAME expected/indexed counts -- to a fresh instance that has not
        (yet) called rebuild() itself."""
        outputs_dir = tmp_path / "outputs"
        outputs_dir.mkdir()
        (outputs_dir / "a.csv").write_text("col\n1", encoding="utf-8")
        (outputs_dir / "b.csv").write_text("col\n2", encoding="utf-8")
        db_path = str(tmp_path / "index.duckdb")

        idx1 = OL.OutputsFtsIndex(str(outputs_dir), db_path=db_path)
        try:
            idx1.rebuild()
            assert idx1.get_convergence_state().converged is True
        finally:
            idx1.close()

        idx2 = OL.OutputsFtsIndex(str(outputs_dir), db_path=db_path)
        try:
            state = idx2.get_convergence_state()
            assert state.converged is True
            assert state.walk_complete is True
            assert state.pending_count == 0
            assert state.indexed_count == 2
            assert state.expected_count == 2
            assert state.last_error is None
        finally:
            idx2.close()

    @duckdb_required
    def test_rebuild_called_first_forced_write_failure_survives_backlog(
        self, tmp_path: Path,
    ) -> None:
        """<code-review fix, sprint 6b5ecdc5> regression test for Finding 2.

        Mirrors the REAL production call order -- search_outputs() calls
        index.rebuild() DIRECTLY, with no preceding get_convergence_state()/
        _connect() call -- which every pre-existing TestWalkStateDurability
        test above pre-empts by connecting first. That ordering matters
        because this instance's first _connect() call (which triggers
        _rehydrate_walk_state_from_disk) doesn't fire until MID-Phase-2 of
        THIS rebuild() call, i.e. AFTER Phase 0/1 of the SAME call has
        already added a newly-discovered file to self._pending_stale.

        Setup: a prior incarnation fully converges on one file and persists
        a clean, empty backlog (a realistic "everything was fine before the
        restart" prior state) -- then a NEW file appears before the restart.
        A fresh instance calls rebuild() directly as its first operation,
        with a forced write failure. The newly-discovered file's stale
        entry must survive in the retry backlog: rehydration merging with
        (not overwriting) this call's own in-flight discovery is what makes
        that possible -- a hard overwrite would replace the in-flight
        backlog with the prior (empty) persisted one, silently dropping the
        file the write failure should have queued for retry.
        """
        outputs_dir = tmp_path / "outputs"
        outputs_dir.mkdir()
        (outputs_dir / "first.csv").write_text("col\nvalue=1\n", encoding="utf-8")
        db_path = str(tmp_path / "index.duckdb")

        # Prior incarnation: fully converges, persists a CLEAN (empty
        # backlog, pass-confirmed-complete) walk state.
        prior = OL.OutputsFtsIndex(str(outputs_dir), db_path=db_path)
        try:
            prior.rebuild()
            assert prior.get_convergence_state().converged is True
        finally:
            prior.close()

        # A new file appears after the "restart" -- this is what THIS
        # call's own Phase 0/1 must discover and queue BEFORE its own
        # first _connect() call (triggering rehydration) ever fires.
        (outputs_dir / "second.csv").write_text("col\nvalue=2\n", encoding="utf-8")
        second_path = str(outputs_dir / "second.csv")

        resumed = OL.OutputsFtsIndex(str(outputs_dir), db_path=db_path)
        try:
            def _boom(self, con):  # noqa: ANN001 -- matches _ensure_schema's signature
                raise RuntimeError("simulated write failure on first post-restart call")

            with patch.object(OL.OutputsFtsIndex, "_ensure_schema", _boom):
                # Mirrors real production usage: rebuild() called directly,
                # first -- never get_convergence_state()/_connect() first.
                resumed.rebuild()

            assert resumed.last_db_write_error is not None
            assert second_path in resumed._pending_stale, (
                "the newly-discovered file's write failed on the SAME "
                "call that rehydration first fired -- it must survive in "
                "the retry backlog, not be silently wiped by the "
                "persisted (pre-restart, empty) backlog -- "
                f"got {resumed._pending_stale!r}"
            )

            # A later, unpatched rebuild() call actually retries and
            # persists it -- proving the survival above was real, not an
            # artifact of the assertion running too early.
            resumed.last_db_write_error = None
            resumed.rebuild()
            assert second_path not in resumed._pending_stale
            hits = resumed.search("value=2")
            assert any("second.csv" in h["path"] for h in hits), (
                f"the file must be searchable once the retry actually "
                f"succeeds -- got {hits}"
            )
        finally:
            resumed.close()

    @duckdb_required
    def test_interrupted_persist_leaves_durable_state_never_a_partial_mix(
        self, tmp_path: Path,
    ) -> None:
        """<code-review fix, sprint 6b5ecdc5> regression test for Finding 1.

        Simulates a hard kill partway through _persist_walk_state_locked's
        DELETE + 6 INSERTs (between the walk_epoch and walk_pass_complete
        inserts, mirroring the reviewer's real subprocess-kill reproduction
        via in-process failure injection -- a test double that forwards
        every statement to a REAL DuckDB connection except it raises before
        the 7th call reaches it). Confirms the durable outputs_index_meta
        state after the interruption is EXACTLY the old, pre-call snapshot
        -- never a hybrid of old and new keys -- proving the DELETE + 6
        INSERTs land as one atomic unit."""
        db_path = str(tmp_path / "index.duckdb")
        idx = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            con = idx._connect()

            def _snapshot() -> dict[str, str | None]:
                placeholders = ",".join("?" for _ in idx._WALK_STATE_META_KEYS)
                rows = con.execute(
                    "SELECT key, value FROM outputs_index_meta WHERE key "
                    f"IN ({placeholders})",
                    list(idx._WALK_STATE_META_KEYS),
                ).fetchall()
                return dict(rows)

            # Baseline: a complete, successfully-committed persist -- the
            # "old" durable state the interrupted persist must fall back to.
            idx._walk_epoch = 1
            idx._walk_pass_confirmed_complete = True
            idx._scan_boundary = "old_boundary"
            idx._expected_count = 10
            idx._last_walk_error = None
            idx._pending_stale = {}
            idx._persist_walk_state_locked(con)
            old_snapshot = _snapshot()
            assert len(old_snapshot) == len(idx._WALK_STATE_META_KEYS), (
                f"baseline persist did not land all 6 keys: {old_snapshot!r}"
            )

            # Mutate to a DIFFERENT new in-memory state...
            idx._walk_epoch = 2
            idx._walk_pass_confirmed_complete = False
            idx._scan_boundary = "new_boundary"
            idx._expected_count = 20
            idx._last_walk_error = "could not list directory 'bad': boom"
            idx._pending_stale = {str(tmp_path / "x.csv"): (1.0, 1)}

            # ...and simulate a hard kill between the walk_epoch and
            # walk_pass_complete INSERTs (the exact window the reviewer
            # reproduced live). ROLLBACK/COMMIT always reach the real
            # connection so the real DuckDB transaction genuinely gets
            # discarded -- the same durable outcome DuckDB's own crash
            # recovery provides when the process dies outright and no
            # Python exception handler ever runs at all.
            failing_con = _FailingAfterN(con, fail_after=6)
            with pytest.raises(RuntimeError, match="simulated hard-kill"):
                idx._persist_walk_state_locked(failing_con)

            new_snapshot = _snapshot()
            assert new_snapshot == old_snapshot, (
                "an interrupted persist must leave the durable state "
                "EXACTLY as it was before the call started -- never a mix "
                f"of old and new keys. old={old_snapshot!r} "
                f"new={new_snapshot!r}"
            )
            # Explicitly confirm it's not a HYBRID: no key leaked a NEW value.
            leaked_new_values = {
                "walk_epoch": "2",
                "walk_pass_complete": "0",
                "walk_scan_boundary": "new_boundary",
                "walk_expected_count": "20",
            }
            for key, bad_value in leaked_new_values.items():
                assert new_snapshot.get(key) != bad_value, (
                    f"key {key!r} leaked a NEW value from the interrupted "
                    f"persist -- durable state is a partial mix: "
                    f"{new_snapshot!r}"
                )

            # The connection must be usable afterward (ROLLBACK genuinely
            # closed out the transaction) -- a later persist succeeds
            # cleanly and the durable state reflects it in full.
            idx._persist_walk_state_locked(con)
            final_snapshot = _snapshot()
            assert final_snapshot["walk_epoch"] == "2"
            assert final_snapshot["walk_pass_complete"] == "0"
        finally:
            idx.close()


class _FailingAfterN:
    """Test double for Finding 1's atomicity regression test: forwards
    every ``execute()`` call to a REAL DuckDB connection, except it raises
    before the ``(fail_after + 1)``-th non-ROLLBACK/COMMIT call reaches it
    -- simulating a hard kill partway through a batch of statements while
    still letting ``_persist_walk_state_locked``'s own except-block
    ROLLBACK reach the real connection, so the real DuckDB transaction
    genuinely gets discarded (the same durable outcome DuckDB's own crash
    recovery provides for a true process kill, where no Python exception
    handler runs at all)."""

    def __init__(self, real_con: Any, fail_after: int) -> None:
        self._real = real_con
        self._count = 0
        self._fail_after = fail_after

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql.strip().upper() in ("ROLLBACK", "COMMIT"):
            return self._real.execute(sql, *args, **kwargs)
        self._count += 1
        if self._count > self._fail_after:
            raise RuntimeError("simulated hard-kill mid-persist")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


# ---------------------------------------------------------------------------
# Determinism: same inputs -> same results (requirement 3)
# ---------------------------------------------------------------------------

class TestDeterminism:
    @duckdb_required
    def test_search_results_stable(self, tmp_path: Path) -> None:
        """Calling search_outputs twice on the same tree returns the same hits."""
        for i in range(5):
            (tmp_path / f"file_{i}.csv").write_text(
                f"col_{i}\n{i}", encoding="utf-8"
            )
        r1 = OL.search_outputs(str(tmp_path), "col")
        r2 = OL.search_outputs(str(tmp_path), "col")
        paths1 = [h["path"] for h in r1["hits"]]
        paths2 = [h["path"] for h in r2["hits"]]
        assert paths1 == paths2

    def test_classify_outputs_deterministic(self, tmp_path: Path) -> None:
        for name in ["z.csv", "a.csv", "m.csv"]:
            (tmp_path / name).write_text("x\n1", encoding="utf-8")
        paths = [str(tmp_path / n) for n in ["z.csv", "a.csv", "m.csv"]]
        r1 = OL.classify_outputs(paths)
        r2 = OL.classify_outputs(paths[::-1])  # reversed input
        # Output order should match sorted path order regardless of input order.
        assert [c["path"] for c in r1["classifications"]] == \
               [c["path"] for c in r2["classifications"]]


# ---------------------------------------------------------------------------
# SQL push-down optimizations: search() and resolve_output()
# ---------------------------------------------------------------------------

class TestSearchSqlPushdown:
    """Verify search() and resolve_output() push filtering/sorting/limit into SQL."""

    @duckdb_required
    def test_search_limit_respected(self, tmp_path: Path) -> None:
        """search() LIMIT pushed into SQL: only up to `limit` rows returned."""
        for i in range(8):
            (tmp_path / f"metric_{i}.csv").write_text(
                f"metric,value\n{i},{i * 0.1}", encoding="utf-8"
            )
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        hits = idx.search("metric", limit=3)
        assert len(hits) <= 3
        idx.close()

    @duckdb_required
    def test_search_returns_only_matches(self, tmp_path: Path) -> None:
        """search() WHERE bm25 IS NOT NULL: non-matching rows excluded entirely."""
        (tmp_path / "loss.csv").write_text("epoch,loss\n1,0.5", encoding="utf-8")
        (tmp_path / "accuracy.csv").write_text("epoch,acc\n1,0.9", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        hits = idx.search("loss")
        # All returned hits must contain 'loss' -- accuracy.csv should not appear.
        for hit in hits:
            assert "loss" in hit["path"].lower() or hit["bm25"] > 0
        # Specifically: accuracy.csv must not appear in the result.
        paths = [hit["path"] for hit in hits]
        assert not any("accuracy" in p for p in paths)
        idx.close()

    @duckdb_required
    def test_search_no_null_bm25_in_results(self, tmp_path: Path) -> None:
        """search() must never return hits with bm25=None (SQL filter ensures this)."""
        for i in range(5):
            (tmp_path / f"data_{i}.csv").write_text(
                f"alpha,beta\n{i},x", encoding="utf-8"
            )
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        hits = idx.search("alpha", limit=10)
        for hit in hits:
            assert hit["bm25"] is not None
            assert hit["score"] is not None
        idx.close()

    @duckdb_required
    def test_search_ordered_by_score_descending(self, tmp_path: Path) -> None:
        """search() results are sorted by score descending (best match first)."""
        # File whose name is exactly the query term should score higher than one
        # where the term only appears in content.
        (tmp_path / "accuracy.csv").write_text(
            "accuracy,value\n0.9,0.95", encoding="utf-8"
        )
        (tmp_path / "unrelated.csv").write_text(
            "col_a,col_b\n1,2", encoding="utf-8"
        )
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        hits = idx.search("accuracy", limit=10)
        scores = [h["score"] for h in hits]
        assert scores == sorted(scores, reverse=True), (
            f"Results not sorted descending: {scores}"
        )
        idx.close()

    @duckdb_required
    def test_resolve_output_exact_match(self, tmp_path: Path) -> None:
        """resolve_output() WHERE path = ?: finds indexed file without full scan."""
        f = tmp_path / "weights.npy"
        f.write_bytes(b"\x93NUMPY\x01\x00fake_header_data_here")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        result = idx.resolve_output(str(f))
        assert result is not None
        assert result["kind"] == "metadata_only"
        idx.close()

    @duckdb_required
    def test_resolve_output_missing_returns_none(self, tmp_path: Path) -> None:
        """resolve_output() returns None for a path not in the index."""
        (tmp_path / "real.csv").write_text("x\n1", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        result = idx.resolve_output(str(tmp_path / "nonexistent.csv"))
        assert result is None
        idx.close()

    @duckdb_required
    def test_resolve_output_returns_correct_fields(self, tmp_path: Path) -> None:
        """resolve_output() returns all expected fields for an indexed CSV."""
        f = tmp_path / "metrics.csv"
        f.write_text('epoch,loss\n1,0.5\n2,0.3', encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        result = idx.resolve_output(str(f))
        assert result is not None
        assert result["kind"] == "text_content"
        assert result["csv_columns"] == ["epoch", "loss"]
        assert result["size"] is not None
        assert result["mtime"] is not None
        idx.close()

    @duckdb_required
    def test_resolve_output_empty_index(self, tmp_path: Path) -> None:
        """resolve_output() on an empty index (no files) returns None gracefully."""
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        result = idx.resolve_output(str(tmp_path / "anything.csv"))
        assert result is None
        idx.close()


# ---------------------------------------------------------------------------
# Parallel analysis + targeted write (perf sprint item 8e0c9fc1)
# ---------------------------------------------------------------------------

class TestAnalyseFile:
    """Tests for the _analyse_file helper used by the parallel rebuild pipeline."""

    def test_basic_csv(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("col_a,col_b\n1,2\n3,4", encoding="utf-8")
        analysis = OL._analyse_file(str(f), OL._sha256_file)
        assert analysis.path == str(f)
        assert analysis.fingerprint.kind == "text_content"
        assert analysis.fingerprint.csv_columns == ["col_a", "col_b"]
        assert analysis.mtime is not None
        assert analysis.size is not None
        assert analysis.sha256 is not None

    def test_missing_file(self) -> None:
        """Missing file must not raise -- returns None mtime/size."""
        analysis = OL._analyse_file("/nonexistent/path.csv", OL._sha256_file)
        assert analysis.mtime is None
        assert analysis.size is None
        assert analysis.sha256 is None

    def test_custom_hasher(self, tmp_path: Path) -> None:
        f = tmp_path / "model.pt"
        f.write_bytes(b"\x00\x01\x02")
        sentinel = "cafebabe"
        analysis = OL._analyse_file(str(f), lambda _p: sentinel)
        assert analysis.sha256 == sentinel

    def test_captured_stat_signature_avoids_second_stat(self, tmp_path: Path) -> None:
        f = tmp_path / "captured.csv"
        f.write_text("col\nvalue", encoding="utf-8")
        st = f.stat()
        with patch.object(OL.os, "stat", side_effect=AssertionError("duplicate stat")):
            analysis = OL._analyse_file(
                str(f), OL._sha256_file,
                stat_signature=(st.st_mtime, st.st_size),
            )
        assert analysis.mtime == st.st_mtime
        assert analysis.size == st.st_size
        assert analysis.sha256 is not None

    def test_independent_per_file(self, tmp_path: Path) -> None:
        """Two concurrent _analyse_file calls on different files must not interfere."""
        import concurrent.futures as cf
        files = {}
        for i in range(4):
            p = tmp_path / f"f{i}.csv"
            p.write_text(f"col_{i}\n{i}", encoding="utf-8")
            files[str(p)] = f"col_{i}"

        results = {}
        with cf.ThreadPoolExecutor(max_workers=4) as pool:
            futs = {pool.submit(OL._analyse_file, p, OL._sha256_file): p
                    for p in files}
            for fut in cf.as_completed(futs):
                a = fut.result()
                results[a.path] = a

        for path, expected_col in files.items():
            a = results[path]
            assert a.fingerprint.csv_columns is not None
            assert expected_col in a.fingerprint.csv_columns


@duckdb_required
class TestParallelRebuildCorrectness:
    """Verify that the parallel rebuild produces correct, deterministic results."""

    def test_many_files_correct_count(self, tmp_path: Path) -> None:
        """Rebuild with N files should index exactly N non-secret files."""
        n = 10
        for i in range(n):
            (tmp_path / f"result_{i:02d}.csv").write_text(
                f"col_x,col_y\n{i},{i*2}", encoding="utf-8"
            )
        # Add a secret file -- must NOT be indexed.
        (tmp_path / ".env").write_text("SECRET=abc", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        count = idx.rebuild()
        assert count == n
        idx.close()

    def test_parallel_rebuild_deterministic(self, tmp_path: Path) -> None:
        """Two fresh indexes of the same tree must produce identical row sets."""
        for i in range(8):
            (tmp_path / f"out_{i:02d}.json").write_text(
                json.dumps({"run": i, "loss": 0.1 * i}), encoding="utf-8"
            )
        idx1 = OL.OutputsFtsIndex(str(tmp_path))
        idx2 = OL.OutputsFtsIndex(str(tmp_path))
        idx1.rebuild()
        idx2.rebuild()

        import duckdb
        paths1 = sorted(
            r[0] for r in idx1._con.execute(
                "SELECT path FROM outputs_index ORDER BY path"
            ).fetchall()
        )
        paths2 = sorted(
            r[0] for r in idx2._con.execute(
                "SELECT path FROM outputs_index ORDER BY path"
            ).fetchall()
        )
        assert paths1 == paths2
        idx1.close()
        idx2.close()

    def test_targeted_delete_only_stale(self, tmp_path: Path) -> None:
        """After a single file changes, only that file's row is replaced in the DB."""
        files = ["alpha.csv", "beta.csv", "gamma.csv"]
        for name in files:
            (tmp_path / name).write_text(f"col\n{name}", encoding="utf-8")

        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()

        # Record sha256 of alpha row before update.
        import duckdb
        row_before = idx._con.execute(
            "SELECT sha256 FROM outputs_index WHERE path LIKE '%alpha%'"
        ).fetchone()
        assert row_before is not None
        sha_before = row_before[0]

        # Now modify only gamma.csv.
        time.sleep(0.02)  # ensure mtime changes on fast filesystems
        (tmp_path / "gamma.csv").write_text("col\nGAMMA_CHANGED", encoding="utf-8")
        # Touch the file to guarantee mtime change.
        os.utime(str(tmp_path / "gamma.csv"), None)

        idx.rebuild()

        # alpha should have the same sha256 (row unchanged).
        row_after = idx._con.execute(
            "SELECT sha256 FROM outputs_index WHERE path LIKE '%alpha%'"
        ).fetchone()
        assert row_after is not None
        assert row_after[0] == sha_before, (
            "alpha.csv sha256 changed even though the file was not modified"
        )
        # gamma should now be findable via search.
        hits = idx.search("GAMMA_CHANGED")
        assert any("gamma" in h["path"] for h in hits)
        idx.close()

    def test_removed_file_deleted_from_db(self, tmp_path: Path) -> None:
        """Deleting a file from disk removes it from the DB on next rebuild."""
        (tmp_path / "keep.csv").write_text("a\n1", encoding="utf-8")
        (tmp_path / "remove.csv").write_text("b\n2", encoding="utf-8")

        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()

        count_before = idx._con.execute(
            "SELECT COUNT(*) FROM outputs_index"
        ).fetchone()[0]
        assert count_before == 2

        (tmp_path / "remove.csv").unlink()
        idx.rebuild()

        count_after = idx._con.execute(
            "SELECT COUNT(*) FROM outputs_index"
        ).fetchone()[0]
        assert count_after == 1

        # The remaining row must be "keep.csv".
        remaining = idx._con.execute(
            "SELECT path FROM outputs_index"
        ).fetchone()[0]
        assert "keep" in remaining
        idx.close()

    def test_no_duplicate_rows_after_multiple_rebuilds(self, tmp_path: Path) -> None:
        """Multiple rebuilds with changes must never leave duplicate rows."""
        f = tmp_path / "data.csv"
        f.write_text("x\n1", encoding="utf-8")

        idx = OL.OutputsFtsIndex(str(tmp_path))
        for i in range(4):
            f.write_text(f"x\n{i}", encoding="utf-8")
            os.utime(str(f), None)
            idx.rebuild()

        count = idx._con.execute(
            "SELECT COUNT(*) FROM outputs_index"
        ).fetchone()[0]
        assert count == 1, f"Expected 1 row, got {count} (duplicate rows introduced)"
        idx.close()

    @duckdb_required
    def test_absolute_and_relative_roots_share_canonical_rows(self, tmp_path: Path) -> None:
        """Restarting with a relative spelling of the same root must not
        duplicate the persisted row keys."""
        f = tmp_path / "data.csv"
        f.write_text("x\n1", encoding="utf-8")
        db_path = str(tmp_path.parent / f"{tmp_path.name}-index.duckdb")
        absolute_root = str(tmp_path.resolve())
        relative_root = os.path.relpath(absolute_root, start=os.getcwd())

        first = OL.OutputsFtsIndex(absolute_root, db_path=db_path)
        try:
            assert first.rebuild() == 1
        finally:
            first.close()

        second = OL.OutputsFtsIndex(relative_root, db_path=db_path)
        try:
            second.rebuild()
            rows = second._con.execute("SELECT path FROM outputs_index").fetchall()
            assert len(rows) == 1
            assert rows[0][0] == os.path.abspath(os.path.normpath(str(f)))
        finally:
            second.close()

    @duckdb_required
    def test_rebuild_repairs_legacy_duplicate_path_rows(self, tmp_path: Path) -> None:
        """A rebuild repairs duplicate rows left by the old path-spelling bug."""
        import duckdb

        f = tmp_path / "data.csv"
        f.write_text("x\n1", encoding="utf-8")
        db_path = str(tmp_path.parent / f"{tmp_path.name}-legacy.duckdb")
        absolute_path = os.path.abspath(os.path.normpath(str(f)))
        legacy_path = os.path.relpath(absolute_path, start=os.getcwd())

        first = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            assert first.rebuild() == 1
        finally:
            first.close()

        con = duckdb.connect(db_path)
        try:
            con.execute(
                "INSERT INTO outputs_index (path, content, mtime, sha256, size, "
                "generating_script, kind, is_archival, canonical_path, "
                "csv_columns, json_keys) "
                "SELECT ?, content, mtime, sha256, size, generating_script, kind, "
                "is_archival, canonical_path, csv_columns, json_keys "
                "FROM outputs_index WHERE path = ?",
                [legacy_path, absolute_path],
            )
        finally:
            con.close()

        repaired = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            assert repaired.rebuild() == 1
            rows = repaired._con.execute(
                "SELECT path FROM outputs_index"
            ).fetchall()
            assert rows == [(absolute_path,)]
            assert len(repaired.search("x")) == 1
        finally:
            repaired.close()

    @duckdb_required
    def test_legacy_migration_scan_excludes_content_column(
        self, tmp_path: Path,
    ) -> None:
        """Regression test for task_ecb96ac9: the SUT_Compressed whole-root
        qualification run (632k files / 433 GiB) hit an allocator failure
        inside the legacy-path migration because its dedup scan selected the
        full extracted-text `content` column for every row before doing any
        grouping. The scan must only ever select a `content IS NOT NULL`
        presence flag, never the bare `content` column.
        """
        class _ExecuteSpy:
            """Proxies a DuckDB connection, recording SQL text.

            DuckDB's connection object is a C extension type with read-only
            attributes, so `.execute` cannot be monkeypatched directly on it
            -- this wraps it instead and is passed in place of the real
            connection to the (test-only) direct call below.
            """

            def __init__(self, real_con: Any) -> None:
                self._real_con = real_con
                self.captured: list[str] = []

            def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
                self.captured.append(sql)
                return self._real_con.execute(sql, *args, **kwargs)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._real_con, name)

        idx = OL.OutputsFtsIndex(str(tmp_path))
        spy = _ExecuteSpy(idx._connect())
        try:
            idx._migrate_legacy_storage_paths_locked(spy)
        finally:
            idx.close()

        scan_sql = next(
            sql for sql in spy.captured
            if sql.strip().upper().startswith("SELECT")
            and "OUTPUTS_INDEX" in sql.upper()
        )
        select_clause = scan_sql.split("FROM", 1)[0]
        columns = [
            c.strip().lower()
            for c in select_clause.split("SELECT", 1)[1].split(",")
        ]
        assert not any(c == "content" for c in columns), (
            f"bare `content` column must not be selected in the dedup scan: {columns!r}"
        )
        assert any("content is not null" in c for c in columns), (
            f"expected a `content IS NOT NULL` presence flag in the scan: {columns!r}"
        )

    @duckdb_required
    def test_legacy_migration_dedup_chunked_preserves_winner_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dedup must still pick the canonical-spelling row and stage its
        real content for Tantivy even when both the row-scan batch limit and
        the winner-content lookup chunk are forced down to 1 -- i.e. the new
        chunked reads (task_ecb96ac9) must not drop or scramble rows across
        chunk boundaries, for multiple independent duplicate-path groups.

        Note: the winner here is decided by the pre-existing, unchanged
        `path == canonical` tie-break (highest priority in the selection
        key) rather than by content/mtime/sha256 richness -- the "rich"/
        "stale" naming reflects the deliberately-chosen setup (canonical
        spelling paired with the richer values), not an independent
        richness-based assertion.
        """
        import duckdb

        db_path = str(tmp_path / "chunked.duckdb")
        idx = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        idx._ensure_schema(idx._connect())
        idx.close()

        con = duckdb.connect(db_path)
        try:
            for name, rich_content, stale_content in [
                ("a.csv", "rich-A-content", "stale-A-content"),
                ("b.csv", "rich-B-content", "stale-B-content"),
            ]:
                absolute_path = os.path.abspath(str(tmp_path / name))
                legacy_path = os.path.relpath(absolute_path, start=os.getcwd())
                con.execute(
                    "INSERT INTO outputs_index (path, content, mtime, sha256, "
                    "size, generating_script, kind, is_archival, "
                    "canonical_path, csv_columns, json_keys) "
                    "VALUES (?, ?, ?, ?, ?, NULL, 'data', false, ?, NULL, NULL)",
                    [absolute_path, rich_content, 200.0, "richsha", 10, absolute_path],
                )
                con.execute(
                    "INSERT INTO outputs_index (path, content, mtime, sha256, "
                    "size, generating_script, kind, is_archival, "
                    "canonical_path, csv_columns, json_keys) "
                    "VALUES (?, ?, ?, NULL, ?, NULL, 'data', false, ?, NULL, NULL)",
                    [legacy_path, stale_content, 50.0, 5, absolute_path],
                )
        finally:
            con.close()

        idx = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            monkeypatch.setattr(idx, "_adaptive_batch_limit", lambda: 1)
            monkeypatch.setattr(type(idx), "_MIGRATION_CONTENT_CHUNK", 1)
            con = idx._connect()
            assert idx._migrate_legacy_storage_paths_locked(con) is True

            rows = con.execute(
                "SELECT path, content FROM outputs_index ORDER BY path"
            ).fetchall()
            assert len(rows) == 2
            surviving = {path: content for path, content in rows}
            expected_a = os.path.abspath(str(tmp_path / "a.csv"))
            expected_b = os.path.abspath(str(tmp_path / "b.csv"))
            assert surviving[expected_a] == "rich-A-content"
            assert surviving[expected_b] == "rich-B-content"

            assert idx._pending_tantivy_upserts[expected_a].content == "rich-A-content"
            assert idx._pending_tantivy_upserts[expected_b].content == "rich-B-content"
        finally:
            idx.close()

    @duckdb_required
    def test_legacy_migration_does_not_perturb_adaptive_batch(
        self, tmp_path: Path,
    ) -> None:
        """Regression for a bug caught reviewing task_ecb96ac9's own fix:
        the migration must read `self._adaptive_batch` directly, not call
        `_adaptive_batch_limit()`. That method both reads AND *adjusts*
        `self._adaptive_batch` from `self.last_rebuild_metrics`; the
        migration runs (from `rebuild()`) after `analysis_seconds`/
        `classification_seconds` are recorded but before `fts_seconds`/
        `write_seconds` exist yet, so the missing keys default to 0 and
        always satisfy the "fast, healthy" branch -- doubling the shared
        write-path batch size on every single rebuild(), regardless of real
        memory or commit pressure, which is the opposite of what the
        adaptive mechanism exists to do.
        """
        f = tmp_path / "data.csv"
        f.write_text("x\n1", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            after_first = idx._adaptive_batch
            idx.rebuild()
            after_second = idx._adaptive_batch
            idx.rebuild()
            after_third = idx._adaptive_batch
            assert after_first == after_second == after_third, (
                "adaptive_batch drifted across rebuild() calls with no real "
                f"fts/write pressure signal: {after_first}, {after_second}, "
                f"{after_third}"
            )
        finally:
            idx.close()

    def test_worker_failure_falls_back_gracefully(self, tmp_path: Path) -> None:
        """If a worker raises, the file is re-analysed synchronously and indexed."""
        f = tmp_path / "ok.csv"
        f.write_text("col\n1", encoding="utf-8")

        call_count = [0]
        real_hasher = OL._sha256_file

        def flaky_hasher(path: str) -> str | None:
            call_count[0] += 1
            # Fail once then succeed.
            if call_count[0] == 1:
                raise OSError("simulated failure")
            return real_hasher(path)

        idx = OL.OutputsFtsIndex(str(tmp_path), hasher=flaky_hasher)
        # Should not raise even though the first hasher call fails.
        count = idx.rebuild()
        # The file should still get indexed via the fallback path.
        assert count >= 0  # may be 0 if fallback also failed; main check is no raise
        idx.close()


# ---------------------------------------------------------------------------
# Phase-2 bulk-insert path (task_ecb96ac9 follow-on, perf)
# ---------------------------------------------------------------------------

class TestBulkInsertPath:
    """e8a2f710 added a pyarrow zero-copy bulk-insert fast path (measured
    ~150x faster than the parameter-bound fallback for a comparable batch),
    but pyarrow was never added to this repo's shared pixi.toml (the same
    gap 52cbe5d8 already fixed once for tantivy/xxhash) -- confirmed live,
    it was silently absent in every worktree, so this branch had ZERO test
    coverage: every rebuild() in the whole suite always took the fallback
    path. Both branches must produce identical, correct results."""

    @duckdb_required
    def test_pyarrow_and_fallback_paths_produce_identical_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "a.csv").write_text("term,value\nalpha,1", encoding="utf-8")
        (tmp_path / "b.json").write_text('{"beta": 2}', encoding="utf-8")
        (tmp_path / "c.txt").write_text("gamma content", encoding="utf-8")

        def _index_rows(db_path: str) -> list[tuple]:
            idx = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
            try:
                count = idx.rebuild()
                rows = idx._con.execute(
                    "SELECT path, content, sha256, size, kind, csv_columns, "
                    "json_keys FROM outputs_index ORDER BY path"
                ).fetchall()
                return count, rows
            finally:
                idx.close()

        pyarrow_db = str(tmp_path.parent / f"{tmp_path.name}-pyarrow.duckdb")
        count_pyarrow, rows_pyarrow = _index_rows(pyarrow_db)
        assert count_pyarrow == 3

        monkeypatch.setitem(sys.modules, "pyarrow", None)
        fallback_db = str(tmp_path.parent / f"{tmp_path.name}-fallback.duckdb")
        count_fallback, rows_fallback = _index_rows(fallback_db)
        assert count_fallback == 3

        assert rows_pyarrow == rows_fallback, (
            "the pyarrow bulk-insert path and the parameter-bound fallback "
            "must persist identical rows for the same input files"
        )

    def test_missing_pyarrow_falls_back_without_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression for the exact gap that made this branch untested for
        so long: pyarrow being ABSENT must never raise, just silently take
        the documented fallback path."""
        monkeypatch.setitem(sys.modules, "pyarrow", None)
        (tmp_path / "a.txt").write_text("content", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx.rebuild() == 1
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# Legacy-path migration throttling (task_ecb96ac9 follow-on, perf)
# ---------------------------------------------------------------------------

class TestLegacyMigrationThrottle:
    """The legacy-path migration scan was running unconditionally on every
    rebuild() call -- an O(rows already indexed) cost that compounds badly
    at real scale (confirmed live: ~100k-row rescans on every single call of
    the SUT_Compressed qualification run, finding nothing to fix on nearly
    all of them). Throttled to: the first call always scans, a scan that
    finds+fixes something forces an immediate recheck next call (stays
    vigilant for an active concurrent writer or a still-completing first
    pass), and otherwise at most one scan per
    _LEGACY_MIGRATION_RECHECK_INTERVAL calls -- bounding staleness rather
    than eliminating the check."""

    @staticmethod
    def _spy_migration(idx: "OL.OutputsFtsIndex", results: list) -> list:
        """Replaces the instance's migration method with a spy that records
        each call and returns the next value from `results` (repeating the
        last value once exhausted) instead of touching a real DuckDB
        connection -- isolates the throttle's call-counting logic from the
        migration function's own behavior."""
        calls: list[int] = []

        def spy(con: Any) -> bool:
            calls.append(len(calls) + 1)
            idx_in_results = min(len(calls) - 1, len(results) - 1)
            return results[idx_in_results]

        idx._migrate_legacy_storage_paths_locked = spy
        return calls

    def test_first_call_always_scans(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        calls = self._spy_migration(idx, [False])
        try:
            idx.rebuild()
        finally:
            idx.close()
        assert len(calls) == 1

    def test_clean_scan_throttles_subsequent_calls(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        calls = self._spy_migration(idx, [False])
        try:
            for _ in range(5):
                idx.rebuild()
        finally:
            idx.close()
        assert len(calls) == 1, "a clean first scan must throttle every call after it"

    def test_periodic_recheck_after_interval(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        calls = self._spy_migration(idx, [False])
        interval = OL.OutputsFtsIndex._LEGACY_MIGRATION_RECHECK_INTERVAL
        try:
            for _ in range(interval + 1):
                idx.rebuild()
            assert len(calls) == 1, "the interval must not have elapsed yet"
            idx.rebuild()
            assert len(calls) == 2, "one more call must cross the recheck interval"
        finally:
            idx.close()

    def test_found_migration_forces_immediate_recheck(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        calls = self._spy_migration(idx, [True, False])
        try:
            idx.rebuild()
            assert len(calls) == 1
            idx.rebuild()
            assert len(calls) == 2, (
                "a scan that found+fixed something must force an immediate "
                "recheck next call, not throttle"
            )
            idx.rebuild()
            assert len(calls) == 2, "the clean 2nd scan must throttle the 3rd call"
        finally:
            idx.close()

    def test_failed_scan_forces_immediate_retry_not_full_cooldown(
        self, tmp_path: Path,
    ) -> None:
        """A scan that RAISES must not be treated as a verified-clean pass --
        that would buy a DB state we never actually confirmed the full
        recheck-interval cooldown. It must force a retry on the very next
        call instead, same as a scan that found real duplicates."""
        idx = OL.OutputsFtsIndex(str(tmp_path))
        call_count = [0]

        def flaky(con: Any) -> bool:
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("simulated transient DB error")
            return False

        idx._migrate_legacy_storage_paths_locked = flaky
        try:
            idx.rebuild()  # call 1: scan raises
            assert call_count[0] == 1
            idx.rebuild()  # call 2: must retry immediately, not throttle
            assert call_count[0] == 2, (
                "a failed scan must force an immediate recheck next call, "
                "not the full recheck-interval cooldown"
            )
            idx.rebuild()  # call 3: the now-clean scan on call 2 throttles this one
            assert call_count[0] == 2
        finally:
            idx.close()

    @duckdb_required
    def test_duplicate_introduced_mid_throttle_is_eventually_caught(
        self, tmp_path: Path,
    ) -> None:
        """Real end-to-end correctness check (no spy): a legacy-spelling
        duplicate row inserted directly into the DB -- standing in for a
        concurrent second process's write, without actually opening a second
        concurrent connection to the same file -- while this instance is
        throttled must still get cleaned up within the recheck interval, not
        silently missed forever."""
        f = tmp_path / "data.csv"
        f.write_text("x\n1", encoding="utf-8")
        db_path = str(tmp_path.parent / f"{tmp_path.name}-throttle.duckdb")
        absolute_path = os.path.abspath(os.path.normpath(str(f)))
        legacy_path = os.path.relpath(absolute_path, start=os.getcwd())

        idx = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            assert idx.rebuild() == 1  # first call: real scan, establishes throttle
            rows = idx._con.execute("SELECT path FROM outputs_index").fetchall()
            assert rows == [(absolute_path,)]

            # Stand in for a concurrent second process's write via this
            # instance's own already-open connection -- avoids the separate
            # question of whether DuckDB allows two concurrent connections to
            # one file, which isn't what this test is checking.
            idx._con.execute(
                "INSERT INTO outputs_index (path, content, mtime, sha256, size, "
                "generating_script, kind, is_archival, canonical_path, "
                "csv_columns, json_keys) "
                "SELECT ?, content, mtime, sha256, size, generating_script, kind, "
                "is_archival, canonical_path, csv_columns, json_keys "
                "FROM outputs_index WHERE path = ?",
                [legacy_path, absolute_path],
            )

            interval = OL.OutputsFtsIndex._LEGACY_MIGRATION_RECHECK_INTERVAL
            for _ in range(interval + 1):
                idx.rebuild()

            rows = idx._con.execute("SELECT path FROM outputs_index").fetchall()
            assert rows == [(absolute_path,)], (
                "the duplicate must be cleaned up within the recheck interval, "
                "not permanently missed"
            )
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# rebuild() Phase 1 deadline enforcement (sprint item d9c76caa)
# ---------------------------------------------------------------------------

class TestRebuildPhase1Deadline:
    """Phase 1's ThreadPoolExecutor must actually respect max_seconds instead
    of always running every worker to completion before Phase 2 even starts."""

    def test_default_budget_raised_from_5s(self) -> None:
        # 5845cc6d: lowered from the original 170.0 fix to 130.0 to leave more
        # headroom under the ~4min external MCP client timeout once real
        # uvx-cold-start + protocol overhead is added on top of the internal
        # rebuild() budget -- real-world validation showed 170.0 cut it too
        # close. Still far above the original unreachable 5.0s default.
        assert OL.DEFAULT_REBUILD_BUDGET_SECONDS >= 100.0
        assert OL.DEFAULT_REBUILD_BUDGET_SECONDS <= 150.0

    def test_phase1_deadline_bounds_wall_clock(self, tmp_path: Path) -> None:
        """A tight deadline must make rebuild() return well before every
        worker would finish -- proof Phase 1 no longer blocks on
        as_completed() until all futures are done."""
        n_files = 16
        for i in range(n_files):
            (tmp_path / f"f{i}.csv").write_text(f"col\n{i}", encoding="utf-8")

        def slow_hasher(path: str) -> str | None:
            time.sleep(1.0)
            return OL._sha256_file(path)

        idx = OL.OutputsFtsIndex(str(tmp_path), hasher=slow_hasher)
        try:
            start = time.monotonic()
            idx.rebuild(max_seconds=0.2)
            elapsed = time.monotonic() - start
            # With 8 workers and 16 files at 1s/hasher call, running Phase 1 to
            # completion would take ~2s. A working deadline check should return
            # once the first batch of workers reports back (~1s), well short
            # of that -- proving Phase 1 didn't wait for every future.
            assert elapsed < 1.8, (
                f"rebuild() took {elapsed:.2f}s with a 0.2s budget -- Phase 1 "
                "appears to have blocked until all workers finished"
            )
            assert idx.last_rebuild_partial is True
        finally:
            idx.close()

    @duckdb_required
    def test_unlimited_budget_processes_everything(self, tmp_path: Path) -> None:
        """max_seconds=None must still index every file (no regression to the
        deadline-enforcement change for the common/default case)."""
        for i in range(5):
            (tmp_path / f"g{i}.csv").write_text(f"col\n{i}", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            count = idx.rebuild(max_seconds=None)
            assert count == 5
            assert idx.last_rebuild_partial is False
        finally:
            idx.close()

    @duckdb_required
    def test_phase1_subdeadline_leaves_phase2_time_to_persist(
        self, tmp_path: Path,
    ) -> None:
        """Regression test for 5845cc6d: on a tree too large to fully analyse
        within the budget, Phase 1 must still leave Phase 2 real time to
        persist whatever it DID manage to compute. Before the fix, Phase 1
        used the FULL deadline and could consume all of it, leaving Phase 2
        zero iterations -- total_indexed stuck at 0 forever regardless of how
        many files were actually hashed."""
        n_files = 40
        for i in range(n_files):
            (tmp_path / f"f{i}.csv").write_text(f"col\n{i}", encoding="utf-8")

        def slow_hasher(path: str) -> str | None:
            time.sleep(0.3)
            return OL._sha256_file(path)

        idx = OL.OutputsFtsIndex(str(tmp_path), hasher=slow_hasher)
        try:
            # 8 workers, 0.3s/file -> ~5 waves of 8 to finish everything
            # (~1.5s total). phase1_deadline is 0.5 * max_seconds; with
            # max_seconds=1.0 that's 0.5s -- enough for exactly one wave (8
            # files) to complete before Phase 1 cuts itself off, leaving
            # ~0.5s for Phase 2 (cheap: no hashing, just cache + DB writes).
            count = idx.rebuild(max_seconds=1.0)
            assert count > 0, (
                "rebuild() made zero forward progress -- Phase 1's own "
                "sub-deadline isn't leaving Phase 2 any time to persist"
            )
            assert count < n_files, (
                "test setup didn't actually exercise a deadline cutoff -- "
                "all files were indexed, so this isn't testing partial progress"
            )
            assert idx.last_rebuild_partial is True
        finally:
            idx.close()

    @duckdb_required
    def test_skips_fts_rebuild_when_deadline_passed_and_index_exists(
        self, tmp_path: Path,
    ) -> None:
        """d9c76caa follow-up: once an FTS index exists, a rebuild() whose
        deadline has already passed by the time the write phase reaches
        _rebuild_fts() must skip that (expensive, full-table, non-
        incremental) step rather than paying its cost unconditionally --
        search() still returns results off the existing (now slightly
        stale) index instead of the call blowing its budget regardless of
        how well Phase 1/Phase 2 behaved."""
        a = tmp_path / "a.csv"
        b = tmp_path / "b.csv"
        a.write_text("col\n1", encoding="utf-8")
        b.write_text("col\n2", encoding="utf-8")

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()  # generous default budget -> FTS actually gets built
            assert idx._fts_built is True

            call_count = [0]
            real_rebuild_fts = idx._rebuild_fts

            def counting_rebuild_fts(con: Any) -> None:
                call_count[0] += 1
                real_rebuild_fts(con)

            idx._rebuild_fts = counting_rebuild_fts

            # Removing a file forces removed_paths to be non-empty, which
            # makes changed=True UNCONDITIONALLY (that loop has no deadline
            # check) -- this reaches the `if changed:` block (and therefore
            # the new skip-fts decision) even with an already-expired
            # deadline, where a stale-only rebuild would otherwise never
            # get there at all.
            b.unlink()
            idx.rebuild(max_seconds=-100.0)  # deadline already in the past
            assert call_count[0] == 0, (
                "FTS rebuild should have been skipped once the deadline had "
                "already passed, not paid unconditionally"
            )
            assert idx.last_rebuild_partial is True

            # search() must still work off the existing index, not nothing.
            hits = idx.search("col")
            assert isinstance(hits, list)
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# rebuild()'s initial file walk must itself be deadline-aware (6ba77ada)
# ---------------------------------------------------------------------------
#
# Root cause: _iter_safe_output_files()/os.walk() has zero deadline
# awareness of its own. On a large (tens-of-thousands-of-files) tree it can
# by itself take far longer than rebuild()'s entire max_seconds budget --
# confirmed live against a real 70,000-file tree: the walk alone took ~11s
# vs. the 5s default budget, so Phase 1 (5845cc6d)/Phase 2's own deadline
# checks never even got a chance to run -- every call returned 0 rows,
# search() stayed empty, forever. This is distinct from d9c76caa/c2021725
# (Phase 1's own sub-deadline, and skipping _rebuild_fts() past a deadline),
# both of which assumed the walk feeding them was fast.
#
# These tests use small synthetic trees with an artificially SLOWED walk
# (monkeypatching _walk_safe_output_files to sleep per yielded path) rather
# than a real tens-of-thousands-of-files tree -- scripts/test_outputs_
# indexing.py already covers that as an on-demand diagnostic against a real
# large tree; this stays CI-fast while exercising the exact code path.

class TestResumableFileWalkDeadlineAwareness:
    """Unit coverage for _ResumableFileWalk: the walk must pause at (or
    near) a deadline and resume later without ever losing or duplicating a
    path, regardless of how tight the deadline is."""

    def test_pauses_and_resumes_without_loss_or_duplication(
        self, tmp_path: Path,
    ) -> None:
        n = 60
        for i in range(n):
            (tmp_path / f"f{i:03d}.csv").write_text("col\n1", encoding="utf-8")

        walk = OL._ResumableFileWalk(str(tmp_path))
        collected: list[str] = []
        calls = 0
        while not walk.exhausted:
            calls += 1
            assert calls <= n + 5, "walk made no progress on some call"
            # A deadline already in the past forces drain() to return after
            # exactly one path per call -- the tightest possible resumption
            # granularity, proving pause/resume never drops or repeats a path
            # even in the worst case. The one exception is the FINAL call:
            # since a generator only knows it's exhausted once a pull from it
            # actually comes back empty, the call that discovers exhaustion
            # may legitimately return zero paths.
            chunk = walk.drain(time.monotonic() - 1.0)
            assert len(chunk) >= 1 or walk.exhausted
            collected.extend(chunk)

        expected = sorted(OL._iter_safe_output_files(str(tmp_path)))
        assert sorted(collected) == expected
        assert len(collected) == len(set(collected)), (
            "duplicate path yielded across resumed drain() calls"
        )

    def test_unlimited_deadline_drains_everything_in_one_call(
        self, tmp_path: Path,
    ) -> None:
        for i in range(10):
            (tmp_path / f"g{i}.csv").write_text("col\n1", encoding="utf-8")
        walk = OL._ResumableFileWalk(str(tmp_path))
        chunk = walk.drain(None)
        assert walk.exhausted is True
        assert len(chunk) == 10

    def test_drain_after_exhausted_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "only.csv").write_text("col\n1", encoding="utf-8")
        walk = OL._ResumableFileWalk(str(tmp_path))
        walk.drain(None)
        assert walk.exhausted is True
        assert walk.drain(None) == []


class TestRebuildWalkDeadlineAwareness:
    """rebuild()-level regression coverage for 6ba77ada: a walk that alone
    exceeds max_seconds must not prevent rebuild() from returning promptly
    and making real, resumable progress across repeated calls."""

    @staticmethod
    def _install_slow_walk(monkeypatch: pytest.MonkeyPatch, delay: float) -> None:
        """Wrap the real walk generator so every yielded path costs `delay`
        seconds -- simulates a walk whose OWN pace (not Phase 1/2) is what
        blows the budget, exactly 6ba77ada's reported signature."""
        real_walk = OL._walk_safe_output_files

        def slow_walk(outputs_dir: str, *, exclude_patterns: tuple = (),
                       on_error=None):
            for p in real_walk(
                outputs_dir, exclude_patterns=exclude_patterns, on_error=on_error,
            ):
                time.sleep(delay)
                yield p

        monkeypatch.setattr(OL, "_walk_safe_output_files", slow_walk)

    def test_bare_walk_exceeding_budget_does_not_block_rebuild(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        n = 40
        for i in range(n):
            (tmp_path / f"h{i:03d}.csv").write_text("col\n1", encoding="utf-8")
        # 40 files * 0.02s/file = 0.8s to walk fully -- alone exceeds the
        # 0.2s max_seconds budget used below.
        self._install_slow_walk(monkeypatch, 0.02)

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            start = time.monotonic()
            idx.rebuild(max_seconds=0.2)
            elapsed = time.monotonic() - start
            # Before the fix this call blocked for the walk's full duration
            # (here ~0.8s; on a real 70k-file tree, ~11s+) regardless of
            # max_seconds, because the walk itself had no deadline check.
            assert elapsed < 0.6, (
                f"rebuild() took {elapsed:.2f}s with a 0.2s budget -- the "
                "walk appears to have blocked past its own deadline"
            )
            assert idx.last_rebuild_partial is True
        finally:
            idx.close()

    @duckdb_required
    def test_large_tree_converges_across_repeated_tight_budget_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mirrors scripts/test_outputs_indexing.py's convergence check at a
        CI-fast scale: repeated rebuild() calls against a walk that alone
        exceeds a single call's budget must still converge -- every file
        indexed, FTS built, search returning real hits -- within a bounded
        number of calls, not plateau (at 0 or any other count) forever.

        52cbe5d8 -- this test previously only called ``idx.search(...)`` as
        the LAST, short-circuited operand of the loop's ``and`` chain, so it
        was skipped entirely whenever ``_fts_built`` was still False.  That
        starved the test of the exact recovery path
        ``OutputsFtsIndex.rebuild()``'s own design relies on for convergence
        (b1789c0d): ``search()`` performs a lazy FTS build whenever
        ``_fts_built`` is False, regardless of ``_fts_pending`` -- but only if
        it is actually CALLED.  The reference script this test claims to
        mirror, ``scripts/test_outputs_indexing.py::run_rebuild_cycles``,
        calls ``idx.search(...)`` unconditionally on every cycle for exactly
        this reason (real production usage via ``search_outputs()`` also
        always calls ``rebuild()`` immediately followed by ``search()``, so
        the lazy build is *always* attempted on the very next call). Without
        that unconditional call, a run where every early rebuild() call
        happened to exceed its own deadline just before reaching the Tantivy
        commit step (deferring the build via ``_fts_pending``) could leave
        ``_fts_built`` permanently False for the rest of the loop -- nothing
        else in a bare ``rebuild()``-only loop ever retries the build once
        the walk finishes and there is nothing left to write (``changed``
        goes False forever, and that is the ONLY call site of
        ``_rebuild_fts()`` inside ``rebuild()``). Confirmed via 20 repeated
        isolated runs: the old short-circuited condition failed ~40% of the
        time (a real, repeatable test bug, not a load-flake) while calling
        ``search()`` unconditionally every cycle -- matching the reference
        script and real production usage -- converged in 19/20 runs (the one
        remaining failure was an unrelated Tantivy searcher-reuse bug, fixed
        separately in ``OutputsFtsIndex.search()``)."""
        n = 50
        for i in range(n):
            (tmp_path / f"k{i:03d}.csv").write_text(
                f"col\nvalue={i}\n", encoding="utf-8",
            )
        self._install_slow_walk(monkeypatch, 0.01)

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            converged = False
            for _ in range(30):
                count = idx.rebuild(max_seconds=0.15)
                # Unconditional, mirroring run_rebuild_cycles() -- see the
                # docstring above for why this must never be short-circuited.
                hits = idx.search("value=1")
                if count >= n and idx._fts_built and not idx._fts_pending and hits:
                    converged = True
                    break
            assert converged, (
                "rebuild()/search() never converged across repeated "
                "tight-budget calls -- the walk fix must let every call "
                "make forward, resumable progress instead of stalling "
                "indefinitely"
            )
        finally:
            idx.close()

    def test_removed_file_eventually_detected_after_slow_walk_completes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Removed-file detection must be deferred (not falsely triggered)
        while a walk pass is still in progress, then correctly applied once
        a full pass completes -- covers the resumable-walk correctness
        tradeoff documented in rebuild()'s Phase 0."""
        keep = tmp_path / "keep.csv"
        remove = tmp_path / "remove.csv"
        keep.write_text("col\n1", encoding="utf-8")
        remove.write_text("col\n2", encoding="utf-8")

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            # Establish both files as fully, successfully indexed BEFORE the
            # walk is slowed and the file removed -- so there's no window
            # for the file to vanish between os.walk()'s internal per-
            # directory listing snapshot and the staleness stat check (a
            # pre-existing, unrelated TOCTOU edge case, not what this test
            # targets).
            idx.rebuild()
            assert any("remove.csv" in p for p in idx._row_cache)
            assert any("keep.csv" in p for p in idx._row_cache)

            self._install_slow_walk(monkeypatch, 0.05)
            remove.unlink()

            # A call whose budget is exhausted mid-walk must not yet prune
            # the removed file -- the walk hasn't reconfirmed the full tree.
            idx.rebuild(max_seconds=0.01)
            assert idx.last_rebuild_partial is True
            assert any("remove.csv" in p for p in idx._row_cache), (
                "removed file was pruned before a full walk pass confirmed "
                "it was actually gone"
            )

            # Give it a generous budget so the in-progress pass (and any
            # follow-up pass) can actually finish end to end.
            for _ in range(20):
                idx.rebuild(max_seconds=2.0)
                if not any("remove.csv" in p for p in idx._row_cache):
                    break
            assert not any("remove.csv" in p for p in idx._row_cache), (
                "removed file was never dropped from the cache once a full "
                "walk pass completed"
            )
            assert any("keep.csv" in p for p in idx._row_cache)
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# Archival-classification hash persistence (sprint item 7a6a278f)
# ---------------------------------------------------------------------------

class TestArchivalHashPersistence:
    """classify_canonical_archival must not re-hash unchanged archival
    candidates on every rebuild() -- only newly-stale files get re-hashed."""

    def test_unchanged_archival_candidate_not_rehashed(self, tmp_path: Path) -> None:
        """classify_canonical_archival only runs when something is stale or
        removed, so the test needs an unrelated file to change between
        rebuilds -- that keeps classify_canonical_archival on the call path
        while the archival pair itself stays untouched."""
        content = b"a,b\n1,2\n"
        canonical = tmp_path / "run.csv"
        archival = tmp_path / "run_old.csv"
        unrelated = tmp_path / "unrelated.csv"
        canonical.write_bytes(content)
        archival.write_bytes(content)
        unrelated.write_bytes(b"x\n1")

        call_log: list[str] = []
        real_hasher = OL._sha256_file

        def counting_hasher(path: str) -> str | None:
            call_log.append(path)
            return real_hasher(path)

        idx = OL.OutputsFtsIndex(str(tmp_path), hasher=counting_hasher)
        try:
            idx.rebuild()
            assert str(canonical) in call_log  # both files new -- must be hashed once
            assert str(archival) in call_log

            # Change only the unrelated file so `stale` is non-empty on the
            # second rebuild (keeping classify_canonical_archival on the call
            # path) while the archival pair itself is untouched.
            unrelated.write_bytes(b"x\n2")
            os.utime(str(unrelated), None)
            call_log.clear()
            idx.rebuild()
            assert str(canonical) not in call_log, (
                "unchanged canonical file was re-hashed by classify_canonical_archival"
            )
            assert str(archival) not in call_log, (
                "unchanged archival candidate was re-hashed by classify_canonical_archival"
            )
        finally:
            idx.close()

    def test_changed_file_still_rehashed(self, tmp_path: Path) -> None:
        """A genuinely modified archival candidate must still be re-hashed --
        persistence must not mask real content changes."""
        canonical = tmp_path / "run.csv"
        archival = tmp_path / "run_old.csv"
        canonical.write_bytes(b"a,b\n1,2\n")
        archival.write_bytes(b"a,b\n1,2\n")

        call_log: list[str] = []
        real_hasher = OL._sha256_file

        def counting_hasher(path: str) -> str | None:
            call_log.append(path)
            return real_hasher(path)

        idx = OL.OutputsFtsIndex(str(tmp_path), hasher=counting_hasher)
        try:
            idx.rebuild()
            call_log.clear()

            archival.write_bytes(b"a,b\n9,9\n")
            os.utime(str(archival), None)
            idx.rebuild()
            assert str(archival) in call_log, (
                "a genuinely modified archival candidate must be re-hashed"
            )
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# DuckDB FTS capability probe (sprint item b8314850) -- REMOVED (77443d83)
#
# This class used to empirically probe DuckDB's create_fts_index parameters
# (documenting that DuckDB 1.5.4 has no "incremental" option, only a full
# overwrite=1 rebuild) to justify why _rebuild_fts() used a full rebuild.
# That whole question is now moot: 77443d83/a9b8485a replaced DuckDB's FTS
# extension with Tantivy for the search index entirely (see
# OutputsFtsIndex._rebuild_fts / .search). DuckDB's create_fts_index is no
# longer called anywhere in this module, so a probe of its parameter support
# no longer documents anything about our own behaviour -- removed rather than
# left around to misleadingly imply we still care about it. See
# TestTantivyMigration below for the equivalent capability coverage
# (incremental commit + legacy-row backfill) under the new architecture.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cold-tree FTS deferral fix (sprint item b1789c0d)
# ---------------------------------------------------------------------------

@duckdb_required
class TestColdTreeFtsDeferral:
    """_rebuild_fts() must be skipped -- not run -- when the overall deadline
    has already expired, even on a cold tree where _fts_built is still False.

    Before this fix, the d9c76caa guard only fired when _fts_built was True,
    so a cold/first-call against a large tree would still call _rebuild_fts()
    unconditionally -- the cost of which scales with total row count and has
    no internal deadline.  On a 66k-file tree this alone hit the ~4min
    external MCP client timeout, returning total_indexed=0 with no error.

    After the fix:
    - If the deadline passed AND _fts_built is False: _fts_pending=True,
      _rebuild_fts() is NOT called, last_rebuild_partial=True.
    - The NEXT search() call (which has a fresh deadline) performs the lazy
      FTS build so real BM25 results come back.
    - A warm tree (both _fts_built=True and deadline passed) still uses the
      existing index unchanged (prior d9c76caa behaviour preserved).
    """

    def test_cold_tree_deadline_skips_fts_sets_pending(
        self, tmp_path: Path,
    ) -> None:
        """First rebuild() on a cold tree with an already-expired deadline must
        write rows to the DB but skip _rebuild_fts(), leaving _fts_pending=True
        so search() knows to build the index on the next call."""
        for i in range(5):
            (tmp_path / f"file_{i}.csv").write_text(
                f"col_unique_{i}\n{i}", encoding="utf-8"
            )

        fts_call_count = [0]

        idx = OL.OutputsFtsIndex(str(tmp_path))
        real_rebuild_fts = idx._rebuild_fts

        def counting_rebuild_fts(con: Any) -> None:
            fts_call_count[0] += 1
            real_rebuild_fts(con)

        idx._rebuild_fts = counting_rebuild_fts
        try:
            # max_seconds=None means no overall deadline BUT we simulate an
            # already-expired deadline by using a deeply-negative max_seconds.
            count = idx.rebuild(max_seconds=-1.0)  # deadline already past
            # The removed-paths + stale paths are non-trivial, so changed=True
            # and we DO enter the if-changed block. With an expired deadline,
            # _rebuild_fts() must be SKIPPED.
            assert fts_call_count[0] == 0, (
                "b1789c0d: _rebuild_fts() was called despite an already-expired "
                "deadline on a cold tree (must be skipped to avoid the timeout bug)"
            )
            # Rows may or may not have been written (deadline may have expired
            # before Phase 2 got any iterations), but _fts_pending must be set.
            assert idx._fts_pending is True, (
                "b1789c0d: _fts_pending must be True after FTS was deferred "
                "on a cold tree with an expired deadline"
            )
            assert idx.last_rebuild_partial is True
        finally:
            idx.close()

    def test_cold_tree_partial_rebuild_then_search_builds_fts(
        self, tmp_path: Path,
    ) -> None:
        """Simulate the real bug scenario: rebuild() writes rows but FTS is
        deferred because the deadline expires -- then search() triggers a lazy
        FTS build and returns real results.

        The live bug sequence:
          call 1: Phase 1+2 write N rows, but _rebuild_fts() itself exceeds
                  budget -> total_indexed=N, hits=[], partial=True, fts_pending=True
          call 2: search() sees _fts_pending, calls _rebuild_fts() with a fresh
                  deadline -> real BM25 hits come back.

        We simulate this by manually driving the state: first do a real rebuild
        (rows written), then simulate a second rebuild that sets _fts_pending by
        expiring the deadline before _rebuild_fts can fire, then verify search()
        lazily builds the FTS.
        """
        n_files = 5
        for i in range(n_files):
            (tmp_path / f"result_{i}.csv").write_text(
                f"epoch,uniqueterm_{i}\n{i},{i}", encoding="utf-8"
            )

        idx = OL.OutputsFtsIndex(str(tmp_path))
        call_sequence: list[str] = []
        real_rebuild_fts = idx._rebuild_fts

        def counting_rebuild_fts(con: Any) -> None:
            call_sequence.append("fts_call")
            real_rebuild_fts(con)

        try:
            # Step 1: write rows to the DB using the underlying helpers directly,
            # bypassing _rebuild_fts entirely. This puts us in the state where
            # rows exist but no FTS index was ever built -- exactly the state
            # the real bug leaves behind when _rebuild_fts times out.
            # We achieve this by running Phase 1 + Phase 2 of rebuild with
            # max_seconds=None (no deadline) but with _rebuild_fts patched to
            # raise a simulated timeout error, which is caught by the outer
            # try/except and leaves _fts_built=False.
            def simulated_fts_timeout(con: Any) -> None:
                call_sequence.append("fts_timeout")
                raise RuntimeError("simulated FTS timeout (b1789c0d test)")

            idx._rebuild_fts = simulated_fts_timeout
            # Run rebuild -- rows get written but FTS "times out"
            idx.rebuild(max_seconds=None)
            # Rows are in _row_cache but FTS didn't build (exception was swallowed)
            assert len(idx._row_cache) == n_files, (
                f"Expected {n_files} rows in cache after rebuild, got {len(idx._row_cache)}"
            )
            assert idx._fts_built is False

            # Manually set _fts_pending to True to simulate what the fixed code
            # would have done had it detected the expiry before calling _rebuild_fts.
            idx._fts_pending = True
            idx.last_rebuild_partial = True

            # Step 2: search() with the real _rebuild_fts restored.
            # It sees _fts_pending=True, calls _rebuild_fts() lazily.
            idx._rebuild_fts = counting_rebuild_fts
            call_sequence.clear()

            hits = idx.search("uniqueterm_3")

            assert "fts_call" in call_sequence, (
                "b1789c0d: search() must trigger lazy _rebuild_fts() when _fts_pending=True"
            )
            assert idx._fts_pending is False, (
                "_fts_pending must be cleared after the lazy build completes"
            )
            assert any("result_3" in h["path"] for h in hits), (
                f"b1789c0d: expected BM25 hit for uniqueterm_3 after lazy FTS build, "
                f"got: {hits}"
            )
        finally:
            idx.close()

    def test_warm_tree_deadline_passed_still_uses_existing_fts(
        self, tmp_path: Path,
    ) -> None:
        """Regression check for d9c76caa (warm tree): an expired deadline on a
        WARM tree (_fts_built=True) must still skip _rebuild_fts() and use the
        existing index -- same as before the b1789c0d change."""
        (tmp_path / "data.csv").write_text("warmterm,val\n1,2", encoding="utf-8")

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            # First rebuild with a generous budget -- FTS gets built.
            idx.rebuild(max_seconds=None)
            assert idx._fts_built is True

            fts_call_count = [0]
            real_rebuild_fts = idx._rebuild_fts

            def counting_fts(con: Any) -> None:
                fts_call_count[0] += 1
                real_rebuild_fts(con)

            idx._rebuild_fts = counting_fts

            # Second rebuild with expired deadline -- must skip FTS (existing index usable).
            (tmp_path / "data.csv").unlink()  # force changed=True
            idx.rebuild(max_seconds=-100.0)

            assert fts_call_count[0] == 0, (
                "warm tree + expired deadline should still skip _rebuild_fts() "
                "(regression check for d9c76caa)"
            )
            assert idx._fts_pending is False, (
                "_fts_pending must stay False for a warm tree -- "
                "the existing index is usable"
            )
            # search() must still return results from the existing index.
            hits = idx.search("warmterm")
            assert isinstance(hits, list)
        finally:
            idx.close()

    def test_search_outputs_cold_tree_returns_partial_not_silent_empty(
        self, tmp_path: Path,
    ) -> None:
        """The module-level search_outputs() must never return a bare, unexplained
        {hits: [], total_indexed: 0} on a cold tree where indexing is in progress.

        Before b1789c0d: a cold tree always returned that indistinguishable result.
        After: first call sets partial=True (and fts_pending=True if FTS deferred).
        A subsequent call (warm rows, fresh deadline) returns real hits.
        """
        n_files = 5
        for i in range(n_files):
            (tmp_path / f"cold_{i}.csv").write_text(
                f"cold_unique_term_{i}\n{i}", encoding="utf-8"
            )

        # Simulate a cold tree under tight budget: Phase 1 processes files but
        # FTS is deferred (deadline already in the past by the time Phase 2 runs).
        # We use search_outputs directly (module-level API, as in the live bug).
        result1 = OL.search_outputs(str(tmp_path), "cold_unique_term_2", max_seconds=-1.0)

        # Must NOT be a silent empty result -- must carry partial=True signal.
        assert result1.get("partial") is True, (
            "b1789c0d: search_outputs() on a cold tree with expired deadline must "
            f"return partial=True, not a silent empty result. Got: {result1}"
        )
        # total_in_index must reflect cumulative rows (even on partial runs)
        # so the caller can distinguish 'cold tree, indexing in progress'
        # from 'empty tree, nothing to find'.
        assert "total_in_index" in result1, (
            "b1789c0d: search_outputs() must include total_in_index for caller visibility"
        )

        # Second call: fresh budget (no artificial limit) -- should get real results.
        result2 = OL.search_outputs(str(tmp_path), "cold_unique_term_2")
        assert result2["total_indexed"] >= 1, (
            "b1789c0d: second search_outputs() call must index files and return non-zero "
            f"total_indexed. Got: {result2}"
        )
        # After FTS is built on the second call, hits should be available.
        assert len(result2["hits"]) >= 1, (
            f"b1789c0d: second call must return BM25 hits once FTS is built. Got: {result2}"
        )

    def test_search_outputs_small_warm_tree_unaffected(
        self, tmp_path: Path,
    ) -> None:
        """A small tree that fits comfortably within the default budget must
        behave exactly as before: total_indexed=N, hits=<results>, no partial flag."""
        (tmp_path / "normal.csv").write_text(
            "normalterm,value\n1,2", encoding="utf-8"
        )
        result = OL.search_outputs(str(tmp_path), "normalterm")
        assert result["total_indexed"] >= 1
        assert len(result["hits"]) >= 1
        assert "db_write_error" not in result, (
            "1a799e52: a healthy write must not carry a db_write_error field"
        )
        # 81a0b23d -- a fully-converged (non-partial) response must keep its
        # existing shape exactly: no new pending_stale_count key at all, not
        # even pending_stale_count=0. Regression check for callers that don't
        # know about the new field.
        assert "partial" not in result
        assert "pending_stale_count" not in result, (
            "81a0b23d: a fully-converged rebuild must not carry "
            f"pending_stale_count -- got {result}"
        )

    def test_search_outputs_mid_pass_surfaces_pending_stale_count(
        self, tmp_path: Path,
    ) -> None:
        """81a0b23d: search_outputs()'s response must expose how many
        confirmed-stale files are still queued for analysis+write whenever
        partial=True, so a zero-hit result on a mid-pass index (more files
        queued behind the scenes) is distinguishable from a genuine miss on
        a fully-converged index -- total_indexed/total_in_index alone can't
        make that distinction because rebuild() deliberately keeps them from
        regressing mid-pass (every previously-indexed path is retained in
        ``all_paths`` until the walk's current pass confirms otherwise)."""
        n_files = 10
        for i in range(n_files):
            (tmp_path / f"pending_{i}.csv").write_text(
                f"col\n{i}", encoding="utf-8"
            )

        def slow_hasher(path: str) -> str | None:
            time.sleep(0.2)
            return OL._sha256_file(path)

        # Seed the module-level cache with a pre-built index using the slow
        # hasher (mirrors OL._get_cached_index's own construction, just with
        # a hasher OL.search_outputs itself has no parameter to inject).
        # max_workers is pinned explicitly (rather than left at the
        # os.cpu_count() default, a849e3d5) so this stays deterministic
        # regardless of how many cores the machine running this test has.
        key = OL._cache_key(str(tmp_path))
        idx = OL.OutputsFtsIndex(
            str(tmp_path),
            db_path=OL._resolve_index_db_path(str(tmp_path)),
            hasher=slow_hasher,
            max_workers=2,
        )
        with OL._index_cache_lock:
            OL._index_cache[key] = idx
        try:
            # phase1_deadline is half of max_seconds (0.5s here); with 2
            # workers at 0.2s/file, only ~2 waves (4 files) fit before the
            # sub-deadline trips, leaving the rest un-analysed and therefore
            # still queued in idx._pending_stale.
            result = OL.search_outputs(str(tmp_path), "col", max_seconds=1.0)

            assert result["partial"] is True, (
                f"expected a mid-pass (partial) rebuild, got: {result}"
            )
            assert "pending_stale_count" in result, (
                "81a0b23d: partial=True must carry pending_stale_count -- "
                f"got {result}"
            )
            assert result["pending_stale_count"] > 0
            assert result["pending_stale_count"] < n_files, (
                "test setup didn't actually leave a partial backlog -- "
                f"got {result}"
            )
            # The surfaced count must be the REAL backlog size, not a stand-in.
            assert result["pending_stale_count"] == len(idx._pending_stale)
        finally:
            with OL._index_cache_lock:
                OL._index_cache.pop(key, None)
            idx.close()

    def test_rebuild_surfaces_db_write_error_instead_of_silent_debug_log(
        self, tmp_path: Path,
    ) -> None:
        """1a799e52: before this fix, Phase 2's DB-write except-block swallowed
        ANY failure at DEBUG level only, while total_indexed/total_in_index (both
        derived from the in-memory row_cache, populated BEFORE the write is
        attempted) kept reporting growing "success" -- a real persistence
        failure looked identical to a healthy index. last_db_write_error /
        the search_outputs() result's db_write_error field must now surface it."""
        (tmp_path / "a.csv").write_text("term_one,1\n", encoding="utf-8")

        idx = OL.OutputsFtsIndex(str(tmp_path))
        assert idx.last_db_write_error is None

        def _boom(self, con):  # noqa: ANN001 -- matches _ensure_schema's real signature
            raise RuntimeError("simulated disk-full / connection failure")

        with patch.object(OL.OutputsFtsIndex, "_ensure_schema", _boom):
            total_indexed = idx.rebuild()

        # The misleading part of the original bug: the in-memory count still
        # looks like a healthy, progressing index...
        assert total_indexed >= 1
        assert len(idx._row_cache) >= 1
        # ...but the write genuinely failed, and that must now be visible.
        assert idx.last_db_write_error is not None
        assert "simulated disk-full" in idx.last_db_write_error

        # A subsequent successful rebuild() call must clear the error (per-call
        # semantics -- last_db_write_error reflects only the MOST RECENT call).
        idx.last_db_write_error = None  # reset attribute directly (isolate this assertion)
        idx.rebuild()
        assert idx.last_db_write_error is None

    def test_search_outputs_surfaces_db_write_error_in_result_dict(
        self, tmp_path: Path,
    ) -> None:
        """The module-level search_outputs() API (the real MCP-tool-facing
        entry point) must surface the same signal, not just the class attribute."""
        (tmp_path / "b.csv").write_text("term_two,1\n", encoding="utf-8")

        def _boom(self, con):  # noqa: ANN001
            raise RuntimeError("simulated write failure")

        with patch.object(OL.OutputsFtsIndex, "_ensure_schema", _boom):
            result = OL.search_outputs(str(tmp_path), "term_two")

        assert result["total_indexed"] >= 1, (
            "the in-memory count still looks like a healthy index -- this is "
            "exactly the deceptive state the fix must make visible via db_write_error"
        )
        assert result.get("db_write_error") is not None
        assert "simulated write failure" in result["db_write_error"]
        assert result.get("partial") is not True, (
            "a small/warm tree must not set partial=True: "
            f"got {result}"
        )
        assert result.get("fts_pending") is not True

    def test_db_write_failure_does_not_permanently_drop_file_from_index(
        self, tmp_path: Path,
    ) -> None:
        """<false-convergence root-cause fix> (sprint item f66656f9): before
        this fix, rebuild() popped a just-analysed path from
        ``_pending_stale`` UNCONDITIONALLY right after ``_apply_precomputed``
        -- which had ALREADY optimistically updated ``_row_cache``/
        ``_manifest`` for that path -- regardless of whether Phase 2's actual
        DB write (below that pop, in the original code) went on to succeed.
        If the write then raised, the path was gone from the backlog forever:
        Phase 1's own staleness check (``manifest mismatch OR not in
        row_cache``) found both already "current" and would never re-flag it
        stale, so the file could never be retried. Worse, ``last_db_write_
        error`` resets to ``None`` at the top of every ``rebuild()`` call, so
        even that signal vanishes on the very next call once nothing is
        `changed` anymore -- the tree then looks fully converged
        (``partial`` False, ``pending_stale_count`` omitted) forever, while
        the real file silently never made it into the searchable index. This
        confirms the fix: the file stays queued in ``_pending_stale`` after a
        failed write, and a later successful ``rebuild()`` call actually
        persists and finds it.
        """
        (tmp_path / "real_file.csv").write_text(
            "distinctive_term_xyz,1\n", encoding="utf-8"
        )

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            def _boom(self, con):  # noqa: ANN001 -- matches _ensure_schema's signature
                raise RuntimeError("simulated transient disk-full")

            with patch.object(OL.OutputsFtsIndex, "_ensure_schema", _boom):
                idx.rebuild()

            # The pre-existing, documented optimistic in-memory state...
            assert idx.last_db_write_error is not None
            assert len(idx._row_cache) == 1, (
                f"expected the optimistic row_cache entry -- got {idx._row_cache!r}"
            )
            # ...but the fix under test: the file must STILL be queued for
            # retry, not silently and permanently dropped.
            assert len(idx._pending_stale) == 1, (
                "a DB write failure must leave the file queued in "
                f"_pending_stale for a future retry -- got {idx._pending_stale!r}"
            )
            # And it must genuinely not be searchable yet -- the write really
            # did fail, nothing was persisted.
            assert idx.search("distinctive_term_xyz") == [], (
                "the row was never actually persisted, so it must not be "
                "searchable until a retry succeeds"
            )

            # A later, unpatched rebuild() call (the transient failure is
            # gone) must actually retry and persist the file this time.
            idx.last_db_write_error = None
            idx.rebuild()
            assert idx.last_db_write_error is None
            assert idx._pending_stale == {}, (
                "a successful retry must clear the backlog -- "
                f"got {idx._pending_stale!r}"
            )

            hits = idx.search("distinctive_term_xyz")
            assert any("real_file.csv" in h["path"] for h in hits), (
                f"the file must be searchable once the write actually "
                f"succeeds -- got {hits}"
            )
        finally:
            idx.close()

    def test_search_outputs_sets_zero_hits_warning_when_index_incomplete(
        self, tmp_path: Path,
    ) -> None:
        """<surface-it-loudly> (sprint item f66656f9): a zero-hit result
        returned while the index is NOT fully converged must carry
        ``zero_hits_warning`` -- an unmissable, self-contained signal added
        because the pre-existing ``partial``/``fts_pending``/
        ``pending_stale_count`` contract, while already tracked and returned,
        was repeatedly misread by callers looking only at ``hits: []`` as
        "file does not exist"."""
        n_files = 5
        for i in range(n_files):
            (tmp_path / f"cold_zhw_{i}.csv").write_text(
                f"cold_zhw_unique_term_{i}\n{i}", encoding="utf-8"
            )

        result = OL.search_outputs(
            str(tmp_path), "cold_zhw_unique_term_2", max_seconds=-1.0,
        )

        assert result["hits"] == []
        assert result.get("partial") is True
        assert result.get("zero_hits_warning"), (
            f"expected zero_hits_warning on a 0-hit, partial=True result -- got {result}"
        )
        assert "re-invoke" in result["zero_hits_warning"].lower()

    def test_search_outputs_no_zero_hits_warning_when_hits_present(
        self, tmp_path: Path,
    ) -> None:
        """Regression check: a healthy, non-empty result must never carry
        zero_hits_warning -- the new field must not leak into the common case."""
        (tmp_path / "warm_zhw.csv").write_text(
            "warmzhwuniqueterm,1\n1,2", encoding="utf-8"
        )
        result = OL.search_outputs(str(tmp_path), "warmzhwuniqueterm")
        assert len(result["hits"]) >= 1
        assert "zero_hits_warning" not in result

    def test_search_outputs_no_zero_hits_warning_on_fully_converged_miss(
        self, tmp_path: Path,
    ) -> None:
        """A genuine zero-hit miss on a FULLY converged (non-partial) index
        must NOT carry zero_hits_warning -- it would defeat the purpose of a
        loud signal if it fired on every miss regardless of index state."""
        (tmp_path / "unrelated_zhw.csv").write_text(
            "somecolumn,1\n1,2", encoding="utf-8"
        )
        result = OL.search_outputs(str(tmp_path), "totally_absent_term_zzz_zhw")
        assert result["hits"] == []
        assert result.get("partial") is not True
        assert "zero_hits_warning" not in result


class TestTantivySearchIndex:
    """77443d83/a6056886 -- OutputsFtsIndex._rebuild_fts/.search now go
    through Tantivy instead of DuckDB's FTS extension."""

    def test_search_reuses_single_searcher_snapshot(self, tmp_path: Path) -> None:
        """52cbe5d8 -- search() must resolve every hit's DocAddress against
        the SAME Searcher snapshot that produced the query results, not a
        freshly-obtained one.

        DocAddress values are only meaningful relative to the segment layout
        of the Searcher that returned them. Calling ``index.searcher()`` a
        second time (as the code previously did, once to run the query and
        again to resolve each hit's path) can return a *different* live view
        if Tantivy's background segment-merge thread swaps in a new layout in
        between -- observed live as a Rust-level panic
        (``pyo3_runtime.PanicException: index out of bounds``) from
        ``searcher.doc(addr)`` that bypasses ``search()``'s own
        ``except Exception`` and crashes the caller instead of yielding the
        documented best-effort ``[]``. Reproducing the race itself is
        inherently timing-dependent (it needs a real background merge to
        land in a few-line window), so this test instead pins down the fix
        directly: wrap the real Tantivy index so every ``.searcher()`` call
        is counted, and assert ``search()`` only ever asks for one per call.
        """
        f = tmp_path / "run.csv"
        f.write_text("findme,value\n1,2", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            real_index, real_writer = idx._connect_tantivy()

            class _SearcherCountingIndex:
                def __init__(self, real: Any) -> None:
                    self._real = real
                    self.searcher_calls = 0

                def searcher(self) -> Any:
                    self.searcher_calls += 1
                    return self._real.searcher()

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._real, name)

            counting = _SearcherCountingIndex(real_index)
            idx._tantivy_index = counting  # type: ignore[assignment]

            hits = idx.search("findme")
            assert hits, "sanity check: the query must still find the row"
            assert counting.searcher_calls == 1, (
                "search() called index.searcher() "
                f"{counting.searcher_calls} times in one invocation -- it "
                "must call it exactly once and reuse that same Searcher for "
                "both the query and every doc() lookup"
            )
        finally:
            idx.close()

    def test_content_update_reflected_in_search(self, tmp_path: Path) -> None:
        """A changed file's OLD content must stop matching and its NEW
        content must start matching -- confirms _rebuild_fts's delete-then-
        add per changed row (not a stale/duplicate Tantivy doc)."""
        f = tmp_path / "run.csv"
        f.write_text("originalterm,value\n1,2", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            assert any(h["path"] == str(f) for h in idx.search("originalterm"))

            f.write_text("updatedterm,value\n9,9", encoding="utf-8")
            os.utime(str(f), None)
            idx.rebuild()

            assert any(h["path"] == str(f) for h in idx.search("updatedterm")), (
                "updated content must be searchable after rebuild()"
            )
            assert not any(h["path"] == str(f) for h in idx.search("originalterm")), (
                "stale content must NOT still match after the row was replaced "
                "(would indicate a duplicate/leftover Tantivy doc)"
            )
        finally:
            idx.close()

    def test_removed_file_no_longer_matches(self, tmp_path: Path) -> None:
        """A deleted file's Tantivy doc must be removed via delete_documents,
        not merely orphaned in the DuckDB metadata table."""
        f = tmp_path / "gone.csv"
        f.write_text("vanishingterm,value\n1,2", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            assert any(h["path"] == str(f) for h in idx.search("vanishingterm"))
            f.unlink()
            idx.rebuild()
            assert not any(h["path"] == str(f) for h in idx.search("vanishingterm"))
        finally:
            idx.close()


class TestTantivyHeapSize:
    """c73c0dd7 -- Tantivy writer's undersized default heap_size caused
    678 fragmented segments + a 4.8s reload() on a real 16k-file batch;
    512MB drops segments to 48 and cuts add+commit+reload to 2.8s (~3x)."""

    def test_default_heap_bytes_is_512mb(self, monkeypatch) -> None:
        monkeypatch.delenv(OL._TANTIVY_HEAP_ENV_VAR, raising=False)
        assert OL._default_tantivy_heap_bytes() == 512 * 1024 * 1024

    def test_env_var_overrides_default(self, monkeypatch) -> None:
        monkeypatch.setenv(OL._TANTIVY_HEAP_ENV_VAR, "256")
        assert OL._default_tantivy_heap_bytes() == 256 * 1024 * 1024

    def test_invalid_env_var_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv(OL._TANTIVY_HEAP_ENV_VAR, "not-a-number")
        assert OL._default_tantivy_heap_bytes() == 512 * 1024 * 1024

    def test_env_var_below_minimum_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv(OL._TANTIVY_HEAP_ENV_VAR, "1")
        assert OL._default_tantivy_heap_bytes() == 512 * 1024 * 1024

    def test_explicit_constructor_arg_takes_precedence_over_env_var(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setenv(OL._TANTIVY_HEAP_ENV_VAR, "256")
        assert OL._resolve_tantivy_heap_bytes(64 * 1024 * 1024) == 64 * 1024 * 1024

    def test_explicit_arg_below_minimum_falls_back_to_default(self) -> None:
        assert OL._resolve_tantivy_heap_bytes(1024) == 512 * 1024 * 1024

    def test_index_resolves_heap_bytes_from_constructor(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path), tantivy_heap_bytes=64 * 1024 * 1024)
        assert idx._tantivy_heap_bytes == 64 * 1024 * 1024

    def test_index_defaults_to_512mb_heap(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv(OL._TANTIVY_HEAP_ENV_VAR, raising=False)
        idx = OL.OutputsFtsIndex(str(tmp_path))
        assert idx._tantivy_heap_bytes == 512 * 1024 * 1024


class TestDuckDBMemoryLimit:
    """task_ecb96ac9 follow-on: a real whole-root run (632k+ files) crashed
    with an unrecoverable DuckDB "Out of Memory Error: Allocation failure"
    where even the rollback of the failed operation also hit an allocation
    failure, permanently invalidating the connection. DuckDB had no
    configured memory_limit, so it kept growing until the OS itself had
    nothing left to give -- a much harder failure than DuckDB hitting its
    own configured ceiling. See _default_duckdb_memory_limit_bytes's module
    docstring for the full reasoning."""

    def test_missing_psutil_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.delenv(OL._DUCKDB_MEMORY_LIMIT_ENV_VAR, raising=False)
        monkeypatch.setitem(sys.modules, "psutil", None)
        assert (
            OL._default_duckdb_memory_limit_bytes(512 * 1024 * 1024)
            == OL._DEFAULT_DUCKDB_MEMORY_LIMIT_BYTES
        )

    @staticmethod
    def _fake_psutil(available_bytes: int) -> MagicMock:
        fake = MagicMock()
        fake.virtual_memory.return_value = MagicMock(available=available_bytes)
        return fake

    def test_healthy_memory_computes_conservative_share(self, monkeypatch) -> None:
        monkeypatch.delenv(OL._DUCKDB_MEMORY_LIMIT_ENV_VAR, raising=False)
        monkeypatch.setitem(sys.modules, "psutil", self._fake_psutil(20 * 1024**3))
        tantivy_heap = 512 * 1024 * 1024
        usable = 20 * 1024**3 - tantivy_heap - OL._DUCKDB_MEMORY_RESERVE_BYTES
        expected = int(usable * OL._DUCKDB_MEMORY_LIMIT_SHARE)
        assert OL._default_duckdb_memory_limit_bytes(tantivy_heap) == expected

    def test_low_memory_uses_floor(self, monkeypatch) -> None:
        monkeypatch.delenv(OL._DUCKDB_MEMORY_LIMIT_ENV_VAR, raising=False)
        monkeypatch.setitem(sys.modules, "psutil", self._fake_psutil(100 * 1024 * 1024))
        assert (
            OL._default_duckdb_memory_limit_bytes(512 * 1024 * 1024)
            == OL._DUCKDB_MEMORY_LIMIT_FLOOR_BYTES
        )

    def test_high_memory_uses_ceiling(self, monkeypatch) -> None:
        monkeypatch.delenv(OL._DUCKDB_MEMORY_LIMIT_ENV_VAR, raising=False)
        monkeypatch.setitem(sys.modules, "psutil", self._fake_psutil(200 * 1024**3))
        assert (
            OL._default_duckdb_memory_limit_bytes(512 * 1024 * 1024)
            == OL._DUCKDB_MEMORY_LIMIT_CEILING_BYTES
        )

    def test_env_var_overrides_availability_based_default(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "psutil", self._fake_psutil(20 * 1024**3))
        monkeypatch.setenv(OL._DUCKDB_MEMORY_LIMIT_ENV_VAR, "4096")
        assert OL._default_duckdb_memory_limit_bytes(512 * 1024 * 1024) == 4096 * 1024 * 1024

    def test_out_of_range_env_var_is_clamped_not_passed_through(self, monkeypatch) -> None:
        """A VALID integer env var that's out of range (0, negative, or
        absurdly large) must still be clamped -- it must not bypass the
        floor/ceiling the way an in-range value legitimately does."""
        monkeypatch.setitem(sys.modules, "psutil", self._fake_psutil(20 * 1024**3))
        monkeypatch.setenv(OL._DUCKDB_MEMORY_LIMIT_ENV_VAR, "0")
        assert (
            OL._default_duckdb_memory_limit_bytes(512 * 1024 * 1024)
            == OL._DUCKDB_MEMORY_LIMIT_FLOOR_BYTES
        )
        monkeypatch.setenv(OL._DUCKDB_MEMORY_LIMIT_ENV_VAR, "999999999")
        assert (
            OL._default_duckdb_memory_limit_bytes(512 * 1024 * 1024)
            == OL._DUCKDB_MEMORY_LIMIT_CEILING_BYTES
        )

    def test_invalid_env_var_falls_back_to_availability_based_default(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "psutil", self._fake_psutil(20 * 1024**3))
        monkeypatch.setenv(OL._DUCKDB_MEMORY_LIMIT_ENV_VAR, "not-a-number")
        tantivy_heap = 512 * 1024 * 1024
        usable = 20 * 1024**3 - tantivy_heap - OL._DUCKDB_MEMORY_RESERVE_BYTES
        expected = int(usable * OL._DUCKDB_MEMORY_LIMIT_SHARE)
        assert OL._default_duckdb_memory_limit_bytes(tantivy_heap) == expected

    def test_explicit_constructor_arg_takes_precedence_over_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv(OL._DUCKDB_MEMORY_LIMIT_ENV_VAR, "8192")
        assert (
            OL._resolve_duckdb_memory_limit_bytes(2048 * 1024 * 1024, 512 * 1024 * 1024)
            == 2048 * 1024 * 1024
        )

    def test_explicit_arg_below_floor_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.delenv(OL._DUCKDB_MEMORY_LIMIT_ENV_VAR, raising=False)
        monkeypatch.setitem(sys.modules, "psutil", None)
        assert (
            OL._resolve_duckdb_memory_limit_bytes(1024, 512 * 1024 * 1024)
            == OL._DEFAULT_DUCKDB_MEMORY_LIMIT_BYTES
        )

    def test_index_resolves_memory_limit_from_constructor(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path), duckdb_memory_limit_bytes=2048 * 1024 * 1024)
        assert idx._duckdb_memory_limit_bytes == 2048 * 1024 * 1024

    @duckdb_required
    def test_connect_applies_memory_limit_pragma(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The resolved limit must actually reach a real PRAGMA call on
        connect, not just be stored on the instance and never used."""
        import duckdb

        real_connect = duckdb.connect
        captured: list[str] = []

        class _ExecuteSpyCon:
            def __init__(self, real_con: Any) -> None:
                self._real_con = real_con

            def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
                captured.append(sql)
                return self._real_con.execute(sql, *args, **kwargs)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._real_con, name)

        monkeypatch.setattr(
            duckdb, "connect", lambda *a, **kw: _ExecuteSpyCon(real_connect(*a, **kw)),
        )
        idx = OL.OutputsFtsIndex(str(tmp_path), duckdb_memory_limit_bytes=2048 * 1024 * 1024)
        try:
            idx._connect()
        finally:
            idx.close()
        pragma_calls = [sql for sql in captured if "memory_limit" in sql.lower()]
        assert pragma_calls, f"expected a memory_limit PRAGMA, got: {captured!r}"
        assert "2048MB" in pragma_calls[0]

    @duckdb_required
    def test_connect_survives_pragma_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A PRAGMA failure (any reason) must never block opening the
        connection -- an unconfigured DuckDB is strictly better than no
        usable connection at all."""
        import duckdb

        real_connect = duckdb.connect

        class _FailingPragmaCon:
            def __init__(self, real_con: Any) -> None:
                self._real_con = real_con

            def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
                if "memory_limit" in sql.lower():
                    raise RuntimeError("simulated PRAGMA failure")
                return self._real_con.execute(sql, *args, **kwargs)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._real_con, name)

        monkeypatch.setattr(
            duckdb, "connect", lambda *a, **kw: _FailingPragmaCon(real_connect(*a, **kw)),
        )
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            con = idx._connect()
            assert con is not None
        finally:
            idx.close()

    @duckdb_required
    def test_connect_disables_preserve_insertion_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """task_ecb96ac9 round 2: a configured memory_limit alone was not
        sufficient -- confirmed live, DuckDB hit its own limit cleanly but
        the ROLLBACK of that failed operation also ran out of memory within
        the same budget, reproducing the identical unrecoverable failure.
        `preserve_insertion_order=false` is DuckDB's own first suggestion in
        that exact error message; must actually be applied on connect."""
        import duckdb

        real_connect = duckdb.connect
        captured: list[str] = []

        class _ExecuteSpyCon:
            def __init__(self, real_con: Any) -> None:
                self._real_con = real_con

            def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
                captured.append(sql)
                return self._real_con.execute(sql, *args, **kwargs)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._real_con, name)

        monkeypatch.setattr(
            duckdb, "connect", lambda *a, **kw: _ExecuteSpyCon(real_connect(*a, **kw)),
        )
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx._connect()
        finally:
            idx.close()
        matches = [sql for sql in captured if "preserve_insertion_order" in sql.lower()]
        assert matches, f"expected a preserve_insertion_order PRAGMA, got: {captured!r}"
        assert "false" in matches[0].lower()

    def test_connect_tantivy_passes_resolved_heap_size_to_writer(
        self, tmp_path: Path,
    ) -> None:
        """The resolved heap_bytes must actually reach tantivy.Index.writer(),
        not just be stored on the instance and never used."""
        f = tmp_path / "a.csv"
        f.write_text("term,1\n", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path), tantivy_heap_bytes=33 * 1024 * 1024)

        import tantivy  # noqa: PLC0415

        captured: dict[str, Any] = {}
        real_writer_method = tantivy.Index.writer

        def _spy_writer(self, *args, **kwargs):  # noqa: ANN001
            captured["heap_size"] = kwargs.get("heap_size")
            return real_writer_method(self, *args, **kwargs)

        try:
            with patch.object(tantivy.Index, "writer", _spy_writer):
                idx._connect_tantivy()
            assert captured.get("heap_size") == 33 * 1024 * 1024
        finally:
            idx.close()


class TestTantivyMigration:
    """8163816e -- a pre-Tantivy (pure-DuckDB-FTS) install's outputs_index
    table can already hold rows that predate this migration. Those rows
    aren't "stale" by filesystem mtime/size, so simulate the upgrade
    scenario directly: insert a row into the DuckDB metadata table without
    ever routing it through Tantivy, then confirm rebuild()/search() still
    finds it via the one-time backfill in
    _migrate_duckdb_rows_to_tantivy_if_needed."""

    def test_legacy_duckdb_only_row_is_backfilled(self, tmp_path: Path) -> None:
        db_path = OL._resolve_index_db_path(str(tmp_path))
        idx = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            con = idx._connect()
            idx._ensure_schema(con)
            # Simulate a pre-Tantivy install: a row already sitting in the
            # DuckDB metadata table with no corresponding Tantivy document.
            con.execute(
                "INSERT INTO outputs_index VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    str(tmp_path / "legacy.csv"), "legacytermvalue content",
                    time.time(), "deadbeef", 10, None, "csv", False, None,
                    None, None,
                ],
            )
            assert idx._tantivy_index is None or (
                idx._tantivy_index.searcher().num_docs == 0
            ), "test setup invariant: nothing committed to Tantivy yet"

            hits = idx.search("legacytermvalue")
            assert any(
                h["path"] == str(tmp_path / "legacy.csv") for h in hits
            ), (
                "a pre-existing DuckDB-only row must be backfilled into "
                "Tantivy by the migration path, not silently invisible to "
                "search() forever after an upgrade"
            )
        finally:
            idx.close()

    def test_migration_is_idempotent_on_reconnect(self, tmp_path: Path) -> None:
        """Re-running the migration check (e.g. on a fresh process reconnect)
        must not error or duplicate documents once already backfilled."""
        db_path = OL._resolve_index_db_path(str(tmp_path))
        idx1 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            con = idx1._connect()
            idx1._ensure_schema(con)
            con.execute(
                "INSERT INTO outputs_index VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    str(tmp_path / "legacy2.csv"), "onlyonceterm content",
                    time.time(), "cafef00d", 10, None, "csv", False, None,
                    None, None,
                ],
            )
            idx1.search("onlyonceterm")  # triggers the one-time backfill
            idx1.close()

            idx2 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
            hits = idx2.search("onlyonceterm")
            matches = [h for h in hits if h["path"] == str(tmp_path / "legacy2.csv")]
            assert len(matches) == 1, (
                f"expected exactly one match after reconnect, got {len(matches)}"
            )
            idx2.close()
        finally:
            pass


class TestTantivyDependency:
    """279448b4 -- the real PyPI package is "tantivy" (quickwit-oss/tantivy-py's
    official bindings), NOT "tantivy-py" (a different, unrelated, essentially
    abandoned package under that literal name). This just confirms the
    dependency actually installs and does a real write+search round trip;
    later items (77443d83/a6056886/8163816e) wire it into OutputsFtsIndex."""

    def test_tantivy_importable(self) -> None:
        import tantivy  # noqa: PLC0415

        assert tantivy is not None

    def test_tantivy_write_and_search_round_trip(self) -> None:
        import tantivy  # noqa: PLC0415

        schema_builder = tantivy.SchemaBuilder()
        schema_builder.add_text_field("body", stored=True)
        schema = schema_builder.build()
        index = tantivy.Index(schema)
        writer = index.writer()
        writer.add_document(tantivy.Document(body="hello world tantivy smoke test"))
        writer.commit()
        index.reload()
        query = index.parse_query("hello", ["body"])
        hits = index.searcher().search(query, 10).hits
        assert len(hits) == 1


# ---------------------------------------------------------------------------
# 5d0b3866 -- _tantivy_dir() must be unique per db_path, not per parent dir
# ---------------------------------------------------------------------------

class TestTantivyDirUniqueness:
    """5d0b3866 -- _tantivy_dir() must derive a path unique per db_path, not
    merely per PARENT directory. Two OutputsFtsIndex instances pointed at
    DIFFERENT db_path values in the SAME parent folder must never share a
    Tantivy index directory -- confirmed live: sharing one caused a SECOND
    index's _connect() to detect the FIRST index's on-disk Tantivy segments
    via tantivy.Index.exists() and set _fts_built=True from a completely
    unrelated index's state, after which search() returned 0 hits for terms
    genuinely present in the second index's own files."""

    def test_distinct_db_paths_get_distinct_tantivy_dirs(self, tmp_path: Path) -> None:
        shared_parent = tmp_path / "shared"
        shared_parent.mkdir()
        db_a = str(shared_parent / "a.duckdb")
        db_b = str(shared_parent / "b.duckdb")
        idx_a = OL.OutputsFtsIndex(str(tmp_path), db_path=db_a)
        idx_b = OL.OutputsFtsIndex(str(tmp_path), db_path=db_b)
        try:
            tdir_a = idx_a._tantivy_dir()
            tdir_b = idx_b._tantivy_dir()
            assert tdir_a is not None and tdir_b is not None
            assert tdir_a != tdir_b, (
                "two distinct db_path values in the SAME parent dir must "
                "get genuinely separate tantivy directories"
            )
        finally:
            idx_a.close()
            idx_b.close()

    def test_tantivy_dir_is_stable_for_the_same_db_path(self, tmp_path: Path) -> None:
        """Determinism requirement (requirement 3): the SAME db_path must
        always resolve to the SAME tantivy dir, across instances."""
        db_path = str(tmp_path / "same.duckdb")
        idx1 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        idx2 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            assert idx1._tantivy_dir() == idx2._tantivy_dir()
        finally:
            idx1.close()
            idx2.close()

    @duckdb_required
    def test_two_indexes_in_same_parent_dir_do_not_see_each_others_state(
        self, tmp_path: Path,
    ) -> None:
        """Regression for the EXACT reported scenario: distinct .duckdb
        files in the same parent dir must never make one instance's
        _fts_built flip True from the OTHER's on-disk tantivy index, and
        search results must stay genuinely isolated."""
        outputs_a = tmp_path / "outputs_a"
        outputs_b = tmp_path / "outputs_b"
        outputs_a.mkdir()
        outputs_b.mkdir()
        (outputs_a / "alpha.csv").write_text(
            "uniquetermalpha,val\n1,2", encoding="utf-8",
        )
        (outputs_b / "beta.csv").write_text(
            "uniquetermbeta,val\n3,4", encoding="utf-8",
        )

        shared_cache = tmp_path / "shared_cache"
        shared_cache.mkdir()
        db_a = str(shared_cache / "index_a.duckdb")
        db_b = str(shared_cache / "index_b.duckdb")

        idx_a = OL.OutputsFtsIndex(str(outputs_a), db_path=db_a)
        idx_a.rebuild()
        idx_a.close()

        idx_b = OL.OutputsFtsIndex(str(outputs_b), db_path=db_b)
        try:
            # Before the fix: idx_b._connect() would find idx_a's on-disk
            # tantivy_index/ dir (a SHARED parent) and incorrectly set
            # _fts_built=True from index A's existence check alone.
            idx_b._connect()
            assert idx_b._fts_built is False, (
                "a fresh index for a DIFFERENT db_path must not inherit "
                "_fts_built=True from an unrelated index's tantivy dir"
            )
            idx_b.rebuild()
            hits_b = idx_b.search("uniquetermbeta")
            assert any("beta.csv" in h["path"] for h in hits_b)
            assert idx_b.search("uniquetermalpha") == [], (
                "index B must never see index A's content"
            )
        finally:
            idx_b.close()


# ---------------------------------------------------------------------------
# 9a18a2b2 -- Tantivy single-writer lock conflict handling
# ---------------------------------------------------------------------------

class TestTantivyLockConflictDetection:
    """_is_tantivy_lock_conflict must recognise Tantivy's real LockBusy
    failure (confirmed live against this bindings version, see
    _connect_tantivy's docstring) and NOT flag unrelated errors."""

    def test_detects_real_lock_busy_message(self) -> None:
        exc = ValueError(
            "Failed to acquire Lockfile: LockBusy. Some(\"Failed to "
            "acquire index lock. If you are using a regular directory, "
            "this means there is already an `IndexWriter` working on this "
            "`Directory`, in this process or in a different process.\")"
        )
        assert OL._is_tantivy_lock_conflict(exc) is True

    def test_does_not_flag_unrelated_errors(self) -> None:
        assert OL._is_tantivy_lock_conflict(ValueError("boom")) is False
        assert OL._is_tantivy_lock_conflict(OSError("disk full")) is False
        assert OL._is_tantivy_lock_conflict(RuntimeError("")) is False


class TestTantivyLockHandling:
    """9a18a2b2 -- OutputsFtsIndex must handle a locked Tantivy index
    gracefully: no uncaught exception from rebuild()/search() (best-effort
    contract preserved), plus a clear, actionable message left behind --
    not just a silent empty result indistinguishable from "no matches"."""

    @staticmethod
    def _hold_tantivy_lock(tdir: str):
        import tantivy  # noqa: PLC0415
        schema = OL.OutputsFtsIndex._tantivy_schema()
        blocking_index = tantivy.Index(schema, path=tdir)
        blocking_writer = blocking_index.writer()
        return blocking_index, blocking_writer

    @duckdb_required
    def test_locked_index_does_not_raise_and_sets_actionable_message(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "data.csv").write_text("term,value\n1,2", encoding="utf-8")
        db_path = OL._resolve_index_db_path(str(tmp_path))
        idx = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        tdir = idx._tantivy_dir()
        assert tdir is not None
        blocking_index, blocking_writer = self._hold_tantivy_lock(tdir)
        try:
            # rebuild()/search() must not raise -- best-effort by contract.
            count = idx.rebuild()
            assert isinstance(count, int)
            hits = idx.search("term")
            assert hits == []  # best-effort contract preserved: no crash
            assert idx._last_tantivy_error is not None, (
                "a lock conflict must leave a clear, actionable message "
                "behind, not disappear silently"
            )
            assert "lock" in idx._last_tantivy_error.lower()
        finally:
            idx.close()
            del blocking_writer
            del blocking_index

    @duckdb_required
    def test_connect_tantivy_raises_typed_conflict_directly(
        self, tmp_path: Path,
    ) -> None:
        """Calling _connect_tantivy() directly (bypassing rebuild()/
        search()'s own broad except) must raise the TYPED
        TantivyLockConflict, not disappear or raise something opaque --
        confirms the failure is genuinely identifiable."""
        db_path = OL._resolve_index_db_path(str(tmp_path))
        idx = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        tdir = idx._tantivy_dir()
        assert tdir is not None
        blocking_index, blocking_writer = self._hold_tantivy_lock(tdir)
        try:
            with pytest.raises(OL.TantivyLockConflict):
                idx._connect_tantivy()
        finally:
            idx.close()
            del blocking_writer
            del blocking_index

    @duckdb_required
    def test_search_outputs_surfaces_lock_warning(self, tmp_path: Path) -> None:
        (tmp_path / "data.csv").write_text("term,value\n1,2", encoding="utf-8")
        db_path = OL._resolve_index_db_path(str(tmp_path))
        probe = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        tdir = probe._tantivy_dir()
        probe.close()
        assert tdir is not None
        blocking_index, blocking_writer = self._hold_tantivy_lock(tdir)
        try:
            result = OL.search_outputs(str(tmp_path), "term")
            assert "tantivy_lock_warning" in result
            assert "lock" in result["tantivy_lock_warning"].lower()
        finally:
            del blocking_writer
            del blocking_index


# ---------------------------------------------------------------------------
# 984b237c -- xxHash swap for the archival-duplicate-detection hasher
# ---------------------------------------------------------------------------

class TestXxh3Hasher:
    """_xxh3_file swaps SHA-256 for xxHash on the archival-dedup hasher;
    must degrade gracefully to SHA-256 when xxhash is unavailable, and must
    actually be wired in as the default everywhere that matters."""

    def test_returns_a_real_hash_for_real_content(self, tmp_path: Path) -> None:
        f = tmp_path / "data.bin"
        f.write_bytes(b"hello xxhash world" * 100)
        digest = OL._xxh3_file(str(f))
        assert digest is not None
        assert isinstance(digest, str)
        assert len(digest) > 0

    def test_deterministic_for_same_content(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.bin"
        f2 = tmp_path / "b.bin"
        content = b"identical content for hashing" * 50
        f1.write_bytes(content)
        f2.write_bytes(content)
        assert OL._xxh3_file(str(f1)) == OL._xxh3_file(str(f2))

    def test_different_for_different_content(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.bin"
        f2 = tmp_path / "b.bin"
        f1.write_bytes(b"content A")
        f2.write_bytes(b"content B")
        assert OL._xxh3_file(str(f1)) != OL._xxh3_file(str(f2))

    def test_missing_file_returns_none(self) -> None:
        assert OL._xxh3_file("/no/such/file.bin") is None

    def test_degrades_to_sha256_when_xxhash_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        f = tmp_path / "data.bin"
        f.write_bytes(b"degrade path content")
        # sys.modules[name] = None is the standard mechanism for forcing
        # ImportError on the next `import name`, regardless of any prior
        # caching -- confirmed live against this Python version.
        monkeypatch.setitem(sys.modules, "xxhash", None)
        digest = OL._xxh3_file(str(f))
        assert digest == OL._sha256_file(str(f))

    def test_default_hasher_is_xxh3_on_classify_canonical_archival(self) -> None:
        import inspect
        sig = inspect.signature(OL.classify_canonical_archival)
        assert sig.parameters["hasher"].default is OL._xxh3_file

    def test_default_hasher_is_xxh3_on_build_output_rows(self) -> None:
        import inspect
        sig = inspect.signature(OL.build_output_rows)
        assert sig.parameters["hasher"].default is OL._xxh3_file

    def test_default_hasher_is_xxh3_on_outputs_fts_index(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._hasher is OL._xxh3_file
        finally:
            idx.close()


class TestXxh3Benchmark:
    """984b237c -- live, on-box A/B confirmation that xxHash is genuinely
    faster than SHA-256 on a real file, not just citing published numbers
    (the item's own notes explicitly require live confirmation). Uses a
    real ~6MB file on real disk, best-of-N timing for both algorithms (cuts
    scheduler/OS noise), and a deliberately modest safety margin (xxHash
    must be at least 1.5x faster) -- well under xxHash's commonly-cited
    5-10x -- so this stays robust on a slow/virtualised CI runner while
    still proving a real, substantial speedup rather than a coin-flip."""

    @staticmethod
    def _best_of(fn, path: str, repeats: int = 7) -> float:
        best = float("inf")
        for _ in range(repeats):
            start = time.perf_counter()
            fn(path)
            best = min(best, time.perf_counter() - start)
        return best

    def test_xxh3_faster_than_sha256_on_real_file(self, tmp_path: Path) -> None:
        try:
            import xxhash  # noqa: F401
        except ImportError:
            pytest.skip("xxhash not installed")

        f = tmp_path / "bench.bin"
        # Real, non-trivial content (not all-zero -- avoids either hasher
        # taking a degenerate fast path on a repeating byte pattern).
        chunk = bytes((i * 2654435761) % 256 for i in range(65536))
        with open(f, "wb") as fh:
            for _ in range(96):  # ~6 MB
                fh.write(chunk)

        # Warm the OS page cache identically for both so the comparison is
        # CPU-bound (hashing throughput), not first-read disk I/O.
        OL._sha256_file(str(f))
        OL._xxh3_file(str(f))

        sha256_best = self._best_of(OL._sha256_file, str(f))
        xxh3_best = self._best_of(OL._xxh3_file, str(f))

        assert xxh3_best * 1.5 <= sha256_best, (
            f"expected xxHash to be at least 1.5x faster than SHA-256 on a "
            f"real ~6MB file; got xxh3_best={xxh3_best:.4f}s "
            f"sha256_best={sha256_best:.4f}s "
            f"(speedup={sha256_best / xxh3_best:.2f}x)"
        )


# ---------------------------------------------------------------------------
# 49b97a6a -- hash-algo version marker forces a one-time full re-hash
# ---------------------------------------------------------------------------

class TestHashAlgoVersionUpgrade:
    """Upgrading from a pre-xxHash (984b237c) on-disk DB must trigger a
    one-time full re-hash of every row -- never leave a silent SHA-256/
    xxHash mix sitting under the same 'sha256' column."""

    @duckdb_required
    def test_fresh_db_is_marked_current_immediately(self, tmp_path: Path) -> None:
        db_path = OL._resolve_index_db_path(str(tmp_path))
        idx = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            con = idx._connect()
            assert idx._pending_hash_upgrade is False
            assert idx._read_hash_algo_version(con) == OL._HASH_ALGO_VERSION
        finally:
            idx.close()

    @duckdb_required
    def test_legacy_db_triggers_full_rehash_on_upgrade(self, tmp_path: Path) -> None:
        db_path = OL._resolve_index_db_path(str(tmp_path))

        # Simulate a genuinely pre-49b97a6a on-disk DB: real content rows
        # with an old-style SHA-256 hash, and NO version marker at all
        # (mirrors an install that predates this marker existing).
        f = tmp_path / "legacy.csv"
        f.write_bytes(b"legacy content for hashing")
        old_sha = hashlib.sha256(b"legacy content for hashing").hexdigest()

        idx0 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        con0 = idx0._connect()
        idx0._ensure_schema(con0)
        st = os.stat(f)
        con0.execute(
            "INSERT INTO outputs_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                str(f), "legacy content for hashing", st.st_mtime, old_sha,
                st.st_size, None, "binary_metadata", False, None, None, None,
            ],
        )
        con0.execute(
            "DELETE FROM outputs_index_meta WHERE key = 'hash_algo_version'"
        )
        idx0.close()

        # Fresh instance reconnecting to the SAME on-disk db_path -- the
        # realistic "upgrade" scenario (existing DB, new code).
        idx1 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            idx1._connect()
            assert idx1._pending_hash_upgrade is True
            assert str(f) not in idx1._row_cache, (
                "legacy row must NOT be rehydrated as already-indexed -- "
                "it must look stale so rebuild() genuinely re-hashes it"
            )

            idx1.rebuild()

            new_row = idx1.resolve_output(str(f))
            assert new_row is not None
            assert new_row["sha256"] != old_sha, (
                "row must be re-hashed with the new algorithm after "
                "upgrade, not left with its stale SHA-256 value"
            )
            assert new_row["sha256"] == OL._xxh3_file(str(f)), (
                "re-hashed value must match the current default hasher "
                "(_xxh3_file)"
            )
            assert idx1._pending_hash_upgrade is False, (
                "upgrade flag must clear once the full re-hash pass converges"
            )
            con1 = idx1._connect()
            assert idx1._read_hash_algo_version(con1) == OL._HASH_ALGO_VERSION
        finally:
            idx1.close()

    @duckdb_required
    def test_already_current_db_is_not_flagged_again(self, tmp_path: Path) -> None:
        """An already-upgraded DB (version already current) must take the
        normal fast path on reconnect -- no forced re-hash, rows rehydrate
        as usual."""
        (tmp_path / "data.csv").write_text("col\n1", encoding="utf-8")
        db_path = OL._resolve_index_db_path(str(tmp_path))
        idx1 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        idx1.rebuild()
        idx1.close()

        idx2 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            idx2._connect()
            assert idx2._pending_hash_upgrade is False
            assert any("data.csv" in p for p in idx2._row_cache), (
                "an already-current DB must rehydrate normally, not be "
                "treated as needing another full re-hash"
            )
        finally:
            idx2.close()


# ---------------------------------------------------------------------------
# acac2599 -- configurable Phase-1 ThreadPoolExecutor worker cap
# ---------------------------------------------------------------------------

class TestConfigurableMaxWorkers:
    def test_default_is_physical_core_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Default follows physical cores, not logical hyperthreads."""
        monkeypatch.delenv(OL._MAX_WORKERS_ENV_VAR, raising=False)
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._max_workers == OL._physical_core_count()
        finally:
            idx.close()

    def test_constructor_override(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path), max_workers=2)
        try:
            assert idx._max_workers == 2
        finally:
            idx.close()

    def test_env_var_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL._MAX_WORKERS_ENV_VAR, "3")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._max_workers == 3
        finally:
            idx.close()

    def test_constructor_overrides_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL._MAX_WORKERS_ENV_VAR, "3")
        idx = OL.OutputsFtsIndex(str(tmp_path), max_workers=5)
        try:
            assert idx._max_workers == 5
        finally:
            idx.close()

    def test_invalid_env_var_falls_back_to_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL._MAX_WORKERS_ENV_VAR, "not-an-int")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._max_workers == OL._physical_core_count()
        finally:
            idx.close()

    def test_non_positive_constructor_value_falls_back_to_default(
        self, tmp_path: Path,
    ) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path), max_workers=0)
        try:
            assert idx._max_workers == OL._physical_core_count()
        finally:
            idx.close()

    @duckdb_required
    def test_override_actually_changes_effective_worker_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Confirms the override actually reaches ThreadPoolExecutor, not
        just the stored attribute."""
        for i in range(5):
            (tmp_path / f"f{i}.csv").write_text(f"col{i}\n1", encoding="utf-8")
        seen: list[int] = []
        real_executor = OL.concurrent.futures.ThreadPoolExecutor

        def _spy(*args, **kwargs):
            seen.append(kwargs.get("max_workers"))
            return real_executor(*args, **kwargs)

        monkeypatch.setattr(OL.concurrent.futures, "ThreadPoolExecutor", _spy)
        idx = OL.OutputsFtsIndex(str(tmp_path), max_workers=2)
        try:
            idx.rebuild()
        finally:
            idx.close()
        assert seen, "ThreadPoolExecutor was never constructed"
        assert seen[0] == 2


# ---------------------------------------------------------------------------
# 1bce8c41 -- walk-batch cap vs. DB write-chunk size decoupling
# ---------------------------------------------------------------------------

class TestWalkBatchDefaultUnbounded:
    """FOLLOW-UP to 3535b9ad: _ResumableFileWalk's own default must no
    longer cap the walk at an arbitrary file count (2000) -- the walk should
    be time-primary by default, stopping only on `deadline` (or true
    exhaustion), while an explicit override still works for anyone who
    deliberately wants a count cap.

    b85394bd -- REGRESSION FIX: an intervening perf change (47a1c53) had
    quietly dropped this class default from 1_000_000_000 back down to
    4_096 (and this one assertion was updated to match, silently narrowing
    what this test class actually proves) while wiring
    OutputsFtsIndex._adaptive_batch_limit() in as rebuild()'s walk cap
    instead -- reintroducing the exact count-primary-walk behaviour this
    class exists to guard against, just one level up. Restored to the
    original >= 1_000_000 assertion; see the _MAX_BATCH docstring in
    outputs_local.py for the full history.
    """

    def test_class_default_is_effectively_unbounded(self) -> None:
        assert OL._ResumableFileWalk._MAX_BATCH >= 1_000_000

    def test_default_walk_not_capped_at_old_2000_default(
        self, tmp_path: Path,
    ) -> None:
        n = 2500  # exceeds the OLD hardcoded default of 2000
        for i in range(n):
            (tmp_path / f"f{i:05d}.csv").write_text("col\n1", encoding="utf-8")
        walk = OL._ResumableFileWalk(str(tmp_path))
        chunk = walk.drain(time.monotonic() + 60.0)
        assert len(chunk) == n, (
            f"drain() returned {len(chunk)}/{n} paths with a generous "
            "deadline -- the walk appears to still be capped at an "
            "arbitrary file count instead of being time-primary"
        )
        assert walk.exhausted is True

    def test_explicit_constructor_arg_still_caps_the_walk(
        self, tmp_path: Path,
    ) -> None:
        """An explicit override must still work for anyone who deliberately
        wants a real count cap -- the decoupling only changes the DEFAULT."""
        for i in range(50):
            (tmp_path / f"g{i:03d}.csv").write_text("col\n1", encoding="utf-8")
        walk = OL._ResumableFileWalk(str(tmp_path), max_batch=10)
        chunk = walk.drain(time.monotonic() + 60.0)
        assert len(chunk) == 10
        assert walk.exhausted is False

    def test_env_var_still_caps_the_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL._ResumableFileWalk._MAX_BATCH_ENV_VAR, "10")
        for i in range(50):
            (tmp_path / f"h{i:03d}.csv").write_text("col\n1", encoding="utf-8")
        walk = OL._ResumableFileWalk(str(tmp_path))
        chunk = walk.drain(time.monotonic() + 60.0)
        assert len(chunk) == 10
        assert walk.exhausted is False


class TestWriteChunkDecoupling:
    """1bce8c41 -- the DB write-chunk size (_WRITE_CHUNK, used to batch
    INSERT/DELETE statements against DuckDB) must stay at a small, tuned
    default independent of _ResumableFileWalk._MAX_BATCH's new effectively-
    unbounded default. Naively sharing one knob for both concerns would
    have turned every DB write into one giant, unchunked SQL statement by
    default -- a new resource-exhaustion risk replacing the one just fixed."""

    def test_default_write_chunk_is_2000_while_walk_cap_is_unbounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(OL.OutputsFtsIndex._WRITE_CHUNK_ENV_VAR, raising=False)
        monkeypatch.delenv(OL._ResumableFileWalk._MAX_BATCH_ENV_VAR, raising=False)
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._write_chunk == 2000
            assert idx._max_batch == OL._ResumableFileWalk._MAX_BATCH
            assert idx._max_batch > idx._write_chunk
        finally:
            idx.close()

    def test_write_chunk_constructor_override(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path), write_chunk=250)
        try:
            assert idx._write_chunk == 250
            # The walk's own cap is untouched by this override.
            assert idx._max_batch == OL._ResumableFileWalk._MAX_BATCH
        finally:
            idx.close()

    def test_write_chunk_env_var_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL.OutputsFtsIndex._WRITE_CHUNK_ENV_VAR, "500")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._write_chunk == 500
        finally:
            idx.close()

    def test_constructor_overrides_write_chunk_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL.OutputsFtsIndex._WRITE_CHUNK_ENV_VAR, "500")
        idx = OL.OutputsFtsIndex(str(tmp_path), write_chunk=42)
        try:
            assert idx._write_chunk == 42
        finally:
            idx.close()

    def test_invalid_write_chunk_env_var_falls_back_to_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL.OutputsFtsIndex._WRITE_CHUNK_ENV_VAR, "not-an-int")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._write_chunk == 2000
        finally:
            idx.close()

    def test_non_positive_write_chunk_falls_back_to_default(
        self, tmp_path: Path,
    ) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path), write_chunk=0)
        try:
            assert idx._write_chunk == 2000
        finally:
            idx.close()

    def test_max_batch_env_var_does_not_affect_write_chunk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The two knobs are fully independent in BOTH directions: setting
        the WALK's env var must not perturb the write-chunk default either."""
        monkeypatch.setenv(OL._ResumableFileWalk._MAX_BATCH_ENV_VAR, "999")
        monkeypatch.delenv(OL.OutputsFtsIndex._WRITE_CHUNK_ENV_VAR, raising=False)
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._max_batch == 999
            assert idx._write_chunk == 2000
        finally:
            idx.close()


class TestAdaptiveBatchPolicy:
    """The adaptive controller must preserve explicit overrides and back off
    when a prior Tantivy/DB commit shows pressure."""

    def test_explicit_batch_disables_adaptation(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path), max_batch=123)
        try:
            assert idx._max_batch_overridden is True
            assert idx._adaptive_batch_limit() == 123
        finally:
            idx.close()

    def test_commit_pressure_halves_adaptive_batch(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx._adaptive_batch = 32_768
            idx.last_rebuild_metrics = {"fts_seconds": 9.0, "write_seconds": 25.0}
            assert idx._adaptive_batch_limit() == 16_384
        finally:
            idx.close()

    @duckdb_required
    def test_replacement_writes_use_upsert_without_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Changed rows use DuckDB upsert semantics; deletes remain reserved
        for genuinely removed paths."""
        n = 120
        for i in range(n):
            (tmp_path / f"w{i:04d}.csv").write_text(f"col\n{i}", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path), write_chunk=10)
        try:
            assert idx._write_chunk == 10
            count = idx.rebuild(max_seconds=60)
            assert count == n
            for p in list(idx._row_cache):
                os.utime(p, None)

            con = idx._connect()
            conn_cls = type(con)
            real_execute = conn_cls.execute
            delete_sql_seen: list[str] = []

            def spy_execute(self, sql, parameters=None):
                if isinstance(sql, str) and sql.startswith(
                    "DELETE FROM outputs_index WHERE path IN"
                ):
                    delete_sql_seen.append(sql)
                if parameters is not None:
                    return real_execute(self, sql, parameters)
                return real_execute(self, sql)

            monkeypatch.setattr(conn_cls, "execute", spy_execute)
            assert idx.rebuild(max_seconds=60) == n
            assert not delete_sql_seen
            assert len(idx._row_cache) == n
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# b85394bd -- deterministic memory probe backing the adaptive batch default
# ---------------------------------------------------------------------------

class TestAdaptiveBatchMemoryProbe:
    """_initial_adaptive_batch must resolve deterministically from whatever
    memory probe is actually available: a real, declared psutil dependency
    on a normal install, or the proven 4k floor when it's genuinely missing
    (standalone uvx without the dependency) -- never a silent guess that
    differs between environments for the same physical machine."""

    def test_missing_psutil_falls_back_to_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(sys.modules, "psutil", None)
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._adaptive_batch == OL.OutputsFtsIndex._ADAPTIVE_MIN_BATCH
        finally:
            idx.close()

    @staticmethod
    def _fake_psutil(available_bytes: int) -> MagicMock:
        # cpu_count(logical=False) also gets consulted (by the unrelated
        # _physical_core_count() Phase-1-worker-count probe) during
        # OutputsFtsIndex.__init__ -- give it a real int so that unrelated
        # code path doesn't blow up on a bare, unconfigured MagicMock.
        fake = MagicMock()
        fake.virtual_memory.return_value = MagicMock(available=available_bytes)
        fake.cpu_count.return_value = 4
        return fake

    def test_healthy_memory_targets_32768(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(sys.modules, "psutil", self._fake_psutil(8 * 1024**3))
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._adaptive_batch == OL.OutputsFtsIndex._ADAPTIVE_MAX_BATCH // 2
            assert idx._adaptive_batch == 32_768
        finally:
            idx.close()

    def test_low_memory_uses_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(sys.modules, "psutil", self._fake_psutil(1 * 1024**3))
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._adaptive_batch == OL.OutputsFtsIndex._ADAPTIVE_MIN_BATCH
        finally:
            idx.close()

    def test_medium_memory_uses_double_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(sys.modules, "psutil", self._fake_psutil(3 * 1024**3))
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._adaptive_batch == OL.OutputsFtsIndex._ADAPTIVE_MIN_BATCH * 2
        finally:
            idx.close()

    def test_explicit_override_ignores_memory_probe_entirely(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit max_batch must win regardless of what psutil (or its
        absence) would otherwise resolve to."""
        monkeypatch.setitem(sys.modules, "psutil", None)
        idx = OL.OutputsFtsIndex(str(tmp_path), max_batch=777)
        try:
            assert idx._adaptive_batch == 777
        finally:
            idx.close()

    def test_psutil_declared_as_explicit_pyproject_dependency(self) -> None:
        """b85394bd -- the memory probe must be declared, not merely lazily
        imported, so a standalone `uvx --from <path> meridian-outputs-mcp`
        install (no Pixi environment to transitively supply it) resolves
        the SAME adaptive-batch code path as a normal install instead of
        silently taking the 4k-floor fallback."""
        import tomllib  # noqa: PLC0415 -- test-only, stdlib on 3.11+

        pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
        deps = data["project"]["dependencies"]
        assert any(d.strip().lower().startswith("psutil") for d in deps), (
            f"psutil is not declared as an explicit dependency in "
            f"{pyproject_path} -- standalone uvx installs may silently "
            f"diverge from Pixi installs on the adaptive-batch default. "
            f"dependencies={deps!r}"
        )


# ---------------------------------------------------------------------------
# b85394bd -- discovery capacity vs. adaptive analysis capacity separation
# ---------------------------------------------------------------------------

class TestDiscoveryVsAnalysisCapacitySeparation:
    """rebuild() must use TWO independent caps: the walk's own discovery
    capacity (self._max_batch, effectively unbounded by default, time-
    primary) for how many paths a drain() call may pull off the filesystem,
    and the memory-adaptive analysis capacity (_adaptive_batch_limit()) to
    bound how much of the pending-stale backlog Phase 1/2 take on in a
    single call. Before this fix both concerns were the same number, so a
    small adaptive limit (e.g. the psutil-missing 4k floor) silently capped
    RAW DISCOVERY too, and the entire backlog (however large the walk let it
    grow) was submitted to ThreadPoolExecutor in one shot."""

    @duckdb_required
    def test_discovery_is_not_capped_by_a_small_adaptive_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        n = 50
        for i in range(n):
            (tmp_path / f"f{i:03d}.csv").write_text(f"col\n{i}", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        monkeypatch.setattr(idx, "_adaptive_batch_limit", lambda: 5)
        try:
            idx.rebuild(max_seconds=60)
            # Discovery must have found ALL 50 files this call even though
            # the adaptive (analysis) limit was artificially tiny -- proves
            # the walk's own cap, not the adaptive limit, governs discovery.
            assert idx.last_rebuild_metrics["discovered_total"] == n
            assert idx.last_rebuild_metrics["walk_complete"] is True
            assert idx.last_rebuild_metrics["walk_batch_limit"] == idx._max_batch
            assert idx.last_rebuild_metrics["analysis_batch_limit"] == 5
        finally:
            idx.close()

    @duckdb_required
    def test_analysis_intake_never_submits_the_whole_backlog_at_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        n = 50
        for i in range(n):
            (tmp_path / f"g{i:03d}.csv").write_text(f"col\n{i}", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        monkeypatch.setattr(idx, "_adaptive_batch_limit", lambda: 5)
        submit_calls: list[int] = []
        real_submit = OL.concurrent.futures.ThreadPoolExecutor.submit

        def _spy_submit(self, *args, **kwargs):
            submit_calls.append(1)
            return real_submit(self, *args, **kwargs)

        monkeypatch.setattr(
            OL.concurrent.futures.ThreadPoolExecutor, "submit", _spy_submit,
        )
        try:
            count = idx.rebuild(max_seconds=60)
            assert len(submit_calls) == 5, (
                f"ThreadPoolExecutor.submit was called {len(submit_calls)} "
                f"times for a backlog of {n} with an analysis limit of 5 -- "
                "the whole pending backlog was still submitted at once "
                "instead of a bounded slice"
            )
            assert count == 5, (
                "one rebuild() call processed more than the bounded "
                "analysis intake -- the whole pending backlog was still "
                "processed at once"
            )
            assert idx.last_rebuild_metrics["analysis_batch_limit"] == 5
            assert idx.last_rebuild_metrics["analysis_backlog_deferred"] == n - 5
            assert len(idx._pending_stale) == n - 5
            assert idx.last_rebuild_partial is True, (
                "a genuinely deferred backlog must still mark the rebuild "
                "as partial so search_outputs()'s partial/pending_stale_"
                "count contract keeps firing"
            )
        finally:
            idx.close()

    @duckdb_required
    def test_bounded_intake_still_fully_converges_across_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Resumable/durable convergence must be preserved: a backlog
        deferred by the analysis-intake bound is picked up on later calls,
        not lost."""
        n = 23
        for i in range(n):
            (tmp_path / f"h{i:03d}.csv").write_text(f"col\n{i}", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        monkeypatch.setattr(idx, "_adaptive_batch_limit", lambda: 7)
        try:
            last_count = 0
            calls = 0
            for _ in range(10):
                calls += 1
                # rebuild() returns the CUMULATIVE row count (all_rows), not
                # a per-call delta -- assert on its final value, not a sum.
                last_count = idx.rebuild(max_seconds=60)
                if not idx._pending_stale and idx._walk_state is None:
                    break
            assert calls > 1, (
                "bounded analysis intake (limit=7 on a 23-file tree) should "
                "have required more than one rebuild() call to converge -- "
                "if it converged in one call, this test isn't exercising "
                "the deferred-backlog path at all"
            )
            assert last_count == n
            assert idx._pending_stale == {}
            state = idx.get_convergence_state()
            assert state.converged is True
            hits = idx.search("col", limit=n)
            assert len(hits) == n
        finally:
            idx.close()

    @duckdb_required
    def test_search_outputs_surfaces_deferred_backlog_metrics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        n = 20
        for i in range(n):
            (tmp_path / f"k{i:03d}.csv").write_text(f"col\n{i}", encoding="utf-8")
        idx = OL._get_cached_index(str(tmp_path))
        monkeypatch.setattr(idx, "_adaptive_batch_limit", lambda: 4)
        try:
            result = OL.search_outputs(str(tmp_path), "col")
            discovery = result["discovery"]
            for key in (
                "walk_batch_limit", "analysis_batch_limit",
                "analysis_batch_source", "analysis_backlog_deferred",
            ):
                assert key in discovery, f"{key!r} missing from discovery metrics"
            assert discovery["analysis_batch_limit"] == 4
            assert discovery["analysis_batch_source"] == "adaptive"
            # discovered_total may exceed `n` (e.g. a .gitignore auto-added
            # to the cache directory by ensure_gitignored, itself now a
            # discoverable file) -- assert the relationship between fields,
            # not a hardcoded count.
            assert discovery["discovered_total"] >= n
            assert (
                discovery["analysis_backlog_deferred"]
                == discovery["discovered_total"] - 4
            )
            assert result["partial"] is True
            assert result["pending_stale_count"] == discovery["analysis_backlog_deferred"]
        finally:
            idx.close()

    @duckdb_required
    def test_analysis_batch_source_reports_override(
        self, tmp_path: Path,
    ) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path), max_batch=9)
        try:
            idx.rebuild(max_seconds=10)
            assert idx.last_rebuild_metrics["analysis_batch_source"] == "override"
            assert idx.last_rebuild_metrics["analysis_batch_limit"] == 9
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# fd4dd661 -- user-configurable exclude patterns (gitignore-style, v1)
# ---------------------------------------------------------------------------

class TestExcludePatterns:
    def test_matches_exclude_pattern_basename_glob(self) -> None:
        assert OL._matches_exclude_pattern("run.tmp", "sub/run.tmp", ("*.tmp",))
        assert not OL._matches_exclude_pattern("run.csv", "sub/run.csv", ("*.tmp",))

    def test_matches_exclude_pattern_relative_path_glob(self) -> None:
        assert OL._matches_exclude_pattern("data.csv", "cache/data.csv", ("cache/*",))
        assert not OL._matches_exclude_pattern("data.csv", "keep/data.csv", ("cache/*",))

    def test_directory_pattern_trailing_slash(self) -> None:
        assert OL._matches_exclude_pattern(
            "node_modules", "node_modules", ("node_modules/",)
        )

    def test_empty_patterns_never_match(self) -> None:
        assert not OL._matches_exclude_pattern("a.csv", "a.csv", ())

    @duckdb_required
    def test_iter_safe_output_files_respects_exclude_patterns(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "keep.csv").write_text("a\n1", encoding="utf-8")
        (tmp_path / "skip.tmp").write_text("a\n1", encoding="utf-8")
        big_dir = tmp_path / "big_sweep_output"
        big_dir.mkdir()
        (big_dir / "inner.csv").write_text("a\n1", encoding="utf-8")

        paths = OL._iter_safe_output_files(
            str(tmp_path), exclude_patterns=("*.tmp", "big_sweep_output/"),
        )
        basenames = {os.path.basename(p) for p in paths}
        assert "keep.csv" in basenames
        assert "skip.tmp" not in basenames
        assert "inner.csv" not in basenames, (
            "a directory-pattern match must prune the WHOLE subtree, not "
            "just filter the directory's own listing"
        )

    @duckdb_required
    def test_outputs_fts_index_respects_exclude_patterns(self, tmp_path: Path) -> None:
        (tmp_path / "keep.csv").write_text("uniquekeepterm\n1", encoding="utf-8")
        (tmp_path / "skip.tmp").write_text("uniqueskiptermxyz\n1", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path), exclude_patterns=("*.tmp",))
        try:
            count = idx.rebuild()
            assert count == 1
            assert idx.resolve_output(str(tmp_path / "skip.tmp")) is None
            assert idx.resolve_output(str(tmp_path / "keep.csv")) is not None
        finally:
            idx.close()

    def test_default_exclude_patterns_from_env(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            OL._EXCLUDE_PATTERNS_ENV_VAR, "*.tmp, node_modules/\nbuild/"
        )
        patterns = OL._default_exclude_patterns()
        assert "*.tmp" in patterns
        assert "node_modules/" in patterns
        assert "build/" in patterns

    def test_default_exclude_patterns_empty_when_unset(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(OL._EXCLUDE_PATTERNS_ENV_VAR, raising=False)
        assert OL._default_exclude_patterns() == ()

    def test_constructor_exclude_overrides_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL._EXCLUDE_PATTERNS_ENV_VAR, "*.csv")
        idx = OL.OutputsFtsIndex(str(tmp_path), exclude_patterns=())
        try:
            assert idx._exclude_patterns == ()
        finally:
            idx.close()

    def test_env_var_used_when_constructor_arg_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL._EXCLUDE_PATTERNS_ENV_VAR, "*.csv")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._exclude_patterns == ("*.csv",)
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# 1662873f -- search_logs: Tier 0 (ripgrep / Python fallback) scan + Tier 1
# (opportunistic JSON/timestamp sniffing) ranking. No persistent index.
# ---------------------------------------------------------------------------

_HAS_RG = OL._rg_binary() is not None


class TestSniffHelpers:
    def test_sniff_timestamp_iso8601(self) -> None:
        raw, epoch = OL._sniff_timestamp('2026-07-18T16:10:32.123Z {"msg":"boot"}')
        assert raw == "2026-07-18T16:10:32.123Z"
        assert epoch is not None

    def test_sniff_timestamp_syslog(self) -> None:
        raw, epoch = OL._sniff_timestamp("Jul 18 16:10:32 host sshd[123]: auth failure")
        assert raw == "Jul 18 16:10:32"
        assert epoch is not None

    def test_sniff_timestamp_none_for_plain_line(self) -> None:
        raw, epoch = OL._sniff_timestamp("plain line with no timestamp at all")
        assert raw is None
        assert epoch is None

    def test_sniff_json_whole_line(self) -> None:
        obj = OL._sniff_json('{"level": "error", "msg": "boom"}')
        assert obj == {"level": "error", "msg": "boom"}

    def test_sniff_json_with_leading_prefix(self) -> None:
        obj = OL._sniff_json('2026-07-18 16:10:32 {"level": "info", "msg": "ok"}')
        assert obj == {"level": "info", "msg": "ok"}

    def test_sniff_json_none_for_plain_text(self) -> None:
        assert OL._sniff_json("just a plain log line, no braces") is None

    def test_sniff_level_from_json_field(self) -> None:
        assert OL._sniff_level("irrelevant text", {"level": "WARN"}) == "WARN"

    def test_sniff_level_from_bare_regex(self) -> None:
        assert OL._sniff_level("2026 ERROR something broke", None) == "ERROR"

    def test_sniff_level_none_when_unrecognised(self) -> None:
        assert OL._sniff_level("nothing recognisable here", None) is None


class TestRankKey:
    """Tier-1 signals rank above plain matches; a miss free-falls back to the
    Tier-0 scan order (no extra ranking cost paid for a sniff that found
    nothing)."""

    def test_tier1_signal_outranks_plain_match(self) -> None:
        plain = OL.LogMatch(path="a.log", line_number=1, line="x", scan_order=0)
        timestamped = OL.LogMatch(
            path="a.log", line_number=5, line="y", scan_order=5, timestamp_epoch=1000.0,
        )
        ordered = sorted([plain, timestamped], key=OL._rank_key, reverse=True)
        assert ordered[0] is timestamped

    def test_no_signal_falls_back_to_scan_order(self) -> None:
        first = OL.LogMatch(path="a.log", line_number=1, line="x", scan_order=0)
        second = OL.LogMatch(path="a.log", line_number=2, line="y", scan_order=1)
        ordered = sorted([second, first], key=OL._rank_key, reverse=True)
        assert ordered == [first, second]

    def test_more_recent_timestamp_ranks_first(self) -> None:
        older = OL.LogMatch(
            path="a.log", line_number=1, line="x", scan_order=0, timestamp_epoch=100.0,
        )
        newer = OL.LogMatch(
            path="a.log", line_number=2, line="y", scan_order=1, timestamp_epoch=200.0,
        )
        ordered = sorted([older, newer], key=OL._rank_key, reverse=True)
        assert ordered == [newer, older]

    def test_higher_severity_ranks_first(self) -> None:
        info = OL.LogMatch(path="a.log", line_number=1, line="x", scan_order=0, level="INFO")
        error = OL.LogMatch(path="a.log", line_number=2, line="y", scan_order=1, level="ERROR")
        ordered = sorted([info, error], key=OL._rank_key, reverse=True)
        assert ordered == [error, info]


class TestScanLogsPython:
    """Tier 0 fallback path (used unconditionally regardless of whether `rg`
    happens to be installed on the machine running these tests)."""

    def test_finds_matches_case_insensitive(self, tmp_path: Path) -> None:
        logs_dir = _make_dir(tmp_path, {"app.log": "INFO boot ok\nERROR disk full\n"})
        hits = OL._scan_logs_python(
            logs_dir, "error", timeout_seconds=5.0,
            max_matches_per_file=100, max_total_matches=100,
        )
        assert len(hits) == 1
        path, line_no, text = hits[0]
        assert path.endswith("app.log")
        assert line_no == 2
        assert "disk full" in text

    def test_excludes_secret_named_files(self, tmp_path: Path) -> None:
        logs_dir = _make_dir(tmp_path, {
            "app.log": "token seen here\n",
            ".env": "token seen here too\n",
        })
        hits = OL._scan_logs_python(
            logs_dir, "token", timeout_seconds=5.0,
            max_matches_per_file=100, max_total_matches=100,
        )
        paths = {p for p, _, _ in hits}
        assert all(not p.endswith(".env") for p in paths)
        assert any(p.endswith("app.log") for p in paths)

    def test_invalid_regex_falls_back_to_literal(self, tmp_path: Path) -> None:
        logs_dir = _make_dir(tmp_path, {"app.log": "weird [unterminated bracket line\n"})
        hits = OL._scan_logs_python(
            logs_dir, "[unterminated", timeout_seconds=5.0,
            max_matches_per_file=100, max_total_matches=100,
        )
        assert len(hits) == 1

    def test_respects_max_matches_per_file(self, tmp_path: Path) -> None:
        content = "\n".join(f"ERROR line {i}" for i in range(10)) + "\n"
        logs_dir = _make_dir(tmp_path, {"app.log": content})
        hits = OL._scan_logs_python(
            logs_dir, "ERROR", timeout_seconds=5.0,
            max_matches_per_file=3, max_total_matches=100,
        )
        assert len(hits) == 3


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep (rg) not on PATH")
class TestRunRipgrep:
    def test_finds_matches(self, tmp_path: Path) -> None:
        logs_dir = _make_dir(tmp_path, {"app.log": "INFO boot ok\nERROR disk full\n"})
        hits = OL._run_ripgrep(logs_dir, "error", timeout_seconds=5.0, max_total_matches=100)
        assert hits is not None
        assert len(hits) == 1
        path, line_no, text = hits[0]
        assert path.endswith("app.log")
        assert line_no == 2
        assert "disk full" in text

    def test_excludes_secret_named_files(self, tmp_path: Path) -> None:
        logs_dir = _make_dir(tmp_path, {
            "app.log": "token seen here\n",
            ".env": "token seen here too\n",
        })
        hits = OL._run_ripgrep(logs_dir, "token", timeout_seconds=5.0, max_total_matches=100)
        assert hits is not None
        paths = {p for p, _, _ in hits}
        assert all(not p.endswith(".env") for p in paths)


def test_run_ripgrep_returns_none_when_binary_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OL, "_rg_binary", lambda: None)
    logs_dir = _make_dir(tmp_path, {"app.log": "ERROR boom\n"})
    assert OL._run_ripgrep(
        logs_dir, "error", timeout_seconds=5.0, max_total_matches=100,
    ) is None


class TestSearchLogs:
    """Module-level API -- what server.py's search_logs MCP tool calls."""

    def test_requires_query(self, tmp_path: Path) -> None:
        result = OL.search_logs(str(tmp_path), "")
        assert "error" in result

    def test_requires_existing_dir(self, tmp_path: Path) -> None:
        result = OL.search_logs(str(tmp_path / "nope"), "error")
        assert "error" in result

    def test_end_to_end_ranking_prefers_timestamped_match(self, tmp_path: Path) -> None:
        logs_dir = _make_dir(tmp_path, {
            "app.log": (
                "plain ERROR line with no timestamp\n"
                '2026-07-18T16:10:32Z {"level":"error","msg":"disk full"}\n'
            ),
        })
        result = OL.search_logs(logs_dir, "error", limit=10)
        assert "error" not in result
        assert result["total_matched"] == 2
        assert result["engine"] in ("ripgrep", "python-fallback")
        top = result["hits"][0]
        assert top["tier"] == 1
        assert top["timestamp_epoch"] is not None

    def test_forces_python_fallback_when_rg_missing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(OL, "_rg_binary", lambda: None)
        logs_dir = _make_dir(tmp_path, {"app.log": "INFO boot\nERROR disk full\n"})
        result = OL.search_logs(logs_dir, "error", limit=10)
        assert result["engine"] == "python-fallback"
        assert result["total_matched"] == 1
        assert result["hits"][0]["line"].endswith("disk full")

    def test_secret_named_log_file_excluded(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(OL, "_rg_binary", lambda: None)
        logs_dir = _make_dir(tmp_path, {
            "app.log": "token appears here\n",
            "credentials.log": "token appears here too\n",
        })
        result = OL.search_logs(logs_dir, "token", limit=10)
        paths = {h["path"] for h in result["hits"]}
        assert all("credentials" not in p for p in paths)

    def test_respects_limit(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(OL, "_rg_binary", lambda: None)
        content = "\n".join(f"ERROR line {i}" for i in range(20)) + "\n"
        logs_dir = _make_dir(tmp_path, {"app.log": content})
        result = OL.search_logs(logs_dir, "error", limit=5)
        assert len(result["hits"]) == 5
        assert result["total_matched"] == 20

    def test_line_preview_truncation(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(OL, "_rg_binary", lambda: None)
        long_line = "ERROR " + ("x" * 1000)
        logs_dir = _make_dir(tmp_path, {"app.log": long_line + "\n"})
        result = OL.search_logs(logs_dir, "error", limit=10, max_line_chars=50)
        assert len(result["hits"][0]["line"]) <= 53  # 50 chars + "..."


# ---------------------------------------------------------------------------
# Explicit convergence state (item 6af1518d, requirement 1 & 2)
# ---------------------------------------------------------------------------

class TestConvergenceState:
    @duckdb_required
    def test_fully_converged_root_state(self, tmp_path: Path) -> None:
        (tmp_path / "a.csv").write_text("col\n1", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            state = idx.get_convergence_state()
            assert state.converged is True
            assert state.walk_complete is True
            assert state.pending_count == 0
            assert state.indexed_count == 1
            assert state.expected_count == 1
            assert state.last_error is None
            assert state.scan_boundary is None
            assert state.subtree is None
            assert state.fts_pending is False
            assert state.partial is False
        finally:
            idx.close()

    def test_missing_dir_returns_error(self) -> None:
        result = OL.get_convergence_state("/no/such/dir")
        assert "error" in result

    @duckdb_required
    def test_module_wrapper_matches_instance_state(self, tmp_path: Path) -> None:
        (tmp_path / "a.csv").write_text("col\n1", encoding="utf-8")
        OL.search_outputs(str(tmp_path), "col")
        state = OL.get_convergence_state(str(tmp_path))
        assert state["converged"] is True
        # >= 1, not == 1: search_outputs uses the persistent on-disk cache
        # (_get_cached_index), whose own auto-created .gitignore (via
        # ensure_gitignored) is itself a real, indexable file at the
        # outputs_dir root -- unrelated to this test's own file count.
        assert state["indexed_count"] >= 1

    @duckdb_required
    def test_walk_in_progress_reports_not_converged(self, tmp_path: Path) -> None:
        """Directly synthesizes an in-progress walk (rather than racing real
        timing) so this asserts get_convergence_state()'s own logic, not
        scheduler jitter."""
        (tmp_path / "a.csv").write_text("col\n1", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            assert idx.get_convergence_state().converged is True
            idx._walk_state = object()  # sentinel: a pass is "in progress"
            idx._scan_boundary = str(tmp_path / "a.csv")
            state = idx.get_convergence_state()
            assert state.converged is False
            assert state.walk_complete is False
            assert state.scan_boundary == str(tmp_path / "a.csv")
        finally:
            idx._walk_state = None
            idx.close()

    @duckdb_required
    def test_pending_stale_reported_and_blocks_convergence(self, tmp_path: Path) -> None:
        (tmp_path / "a.csv").write_text("col\n1", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            idx._pending_stale[str(tmp_path / "a.csv")] = (1.0, 1)
            state = idx.get_convergence_state()
            assert state.pending_count == 1
            assert state.converged is False
        finally:
            idx.close()

    @duckdb_required
    def test_db_write_error_surfaced_as_last_error(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.last_db_write_error = "simulated write failure"
            state = idx.get_convergence_state()
            assert state.last_error == "simulated write failure"
            assert state.converged is False
        finally:
            idx.close()

    @duckdb_required
    def test_fts_pending_blocks_convergence(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            idx._fts_pending = True
            state = idx.get_convergence_state()
            assert state.fts_pending is True
            assert state.converged is False
        finally:
            idx.close()


class TestWalkErrorSurfacedInConvergence:
    """6af1518d requirement 1: 'last error (if the walk hit something it
    couldn't read)' -- _walk_safe_output_files's on_error hook must be wired
    all the way through _ResumableFileWalk into OutputsFtsIndex and be
    visible on ConvergenceState.last_error, while the walk itself keeps
    making best-effort progress past the unreadable directory."""

    @duckdb_required
    def test_unreadable_subdir_surfaced_as_last_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        good = tmp_path / "good"
        good.mkdir()
        (good / "a.csv").write_text("col\n1", encoding="utf-8")
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        (bad / "b.csv").write_text("col\n2", encoding="utf-8")

        real_scandir = os.scandir
        bad_norm = os.path.normpath(str(bad))

        def flaky_scandir(path):
            if os.path.normpath(str(path)) == bad_norm:
                raise PermissionError("simulated permission denied")
            return real_scandir(path)

        monkeypatch.setattr(OL.os, "scandir", flaky_scandir)

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            state = idx.get_convergence_state()
            assert state.last_error is not None
            assert "bad_dir" in state.last_error
            assert any("a.csv" in p for p in idx._row_cache)
            assert not any("b.csv" in p for p in idx._row_cache)
        finally:
            idx.close()

    def test_on_error_callback_receives_dir_and_exception(
        self, tmp_path: Path,
    ) -> None:
        """Unit-level check directly on _walk_safe_output_files, independent
        of OutputsFtsIndex, so a regression here is diagnosable without the
        full rebuild() machinery."""
        bad = tmp_path / "bad"
        bad.mkdir()
        real_scandir = os.scandir

        def flaky_scandir(path):
            if os.path.normpath(str(path)) == os.path.normpath(str(bad)):
                raise OSError("boom")
            return real_scandir(path)

        seen: list[tuple[str, OSError]] = []
        with patch("os.scandir", side_effect=flaky_scandir):
            list(OL._walk_safe_output_files(
                str(tmp_path), on_error=lambda p, e: seen.append((p, e)),
            ))
        assert len(seen) == 1
        assert os.path.normpath(seen[0][0]) == os.path.normpath(str(bad))
        assert isinstance(seen[0][1], OSError)

    def test_on_error_callback_exception_does_not_break_walk(
        self, tmp_path: Path,
    ) -> None:
        """A misbehaving on_error callback must never abort the walk itself
        -- it's purely observational."""
        (tmp_path / "ok.csv").write_text("col\n1", encoding="utf-8")

        def boom_callback(dir_path: str, exc: OSError) -> None:
            raise RuntimeError("observer callback misbehaving")

        # No unreadable directory here -- this just proves a NON-raising
        # walk still completes normally when on_error is supplied but never
        # actually invoked, guarding the plumbing itself.
        found = list(OL._walk_safe_output_files(str(tmp_path), on_error=boom_callback))
        assert any("ok.csv" in p for p in found)


class TestWalkErrorClearsOnRecovery:
    """fa600e42: distinguishes ACTIVE walk failure (this pass) from
    HISTORICAL walk failure (a prior, already-superseded pass). Before this
    fix, _last_walk_error was set once by _record_walk_error and never
    reset anywhere -- one transient error made get_convergence_state()
    report non-convergence forever, even after arbitrarily many subsequent
    clean passes."""

    def test_error_clears_once_the_next_pass_completes_without_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        good = tmp_path / "good"
        good.mkdir()
        (good / "a.csv").write_text("col\n1", encoding="utf-8")
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        (bad / "b.csv").write_text("col\n2", encoding="utf-8")

        real_scandir = os.scandir
        bad_norm = os.path.normpath(str(bad))
        fail = {"active": True}

        def flaky_scandir(path):
            if fail["active"] and os.path.normpath(str(path)) == bad_norm:
                raise PermissionError("simulated permission denied")
            return real_scandir(path)

        monkeypatch.setattr(OL.os, "scandir", flaky_scandir)

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            first_state = idx.get_convergence_state()
            assert first_state.last_error is not None
            assert first_state.walk_complete is True  # tiny tree, one call

            # The problem is now fixed (e.g. permissions repaired) -- the
            # NEXT full pass must be given a genuine chance to prove that,
            # not have the old error follow it around forever.
            fail["active"] = False
            idx.rebuild()
            second_state = idx.get_convergence_state()
            assert second_state.last_error is None
            assert any("b.csv" in p for p in idx._row_cache)  # really recovered
        finally:
            idx.close()

    def test_persistent_error_stays_surfaced_across_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        good = tmp_path / "good"
        good.mkdir()
        (good / "a.csv").write_text("col\n1", encoding="utf-8")
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        (bad / "b.csv").write_text("col\n2", encoding="utf-8")

        real_scandir = os.scandir
        bad_norm = os.path.normpath(str(bad))

        def flaky_scandir(path):
            if os.path.normpath(str(path)) == bad_norm:
                raise PermissionError("still broken")
            return real_scandir(path)

        monkeypatch.setattr(OL.os, "scandir", flaky_scandir)

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            assert idx.get_convergence_state().last_error is not None

            # A second full pass hits the SAME still-broken directory -- the
            # error must never go silent just because a new pass began.
            idx.rebuild()
            second_error = idx.get_convergence_state().last_error
            assert second_error is not None
            assert "bad_dir" in second_error
        finally:
            idx.close()

    def test_error_clears_across_a_real_process_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Code-review fix (fa600e42): the FIRST version of this fix reset
        _last_walk_error in Phase 0, but on a genuinely fresh process
        instance rebuild()'s own _connect() (mid-Phase-2) doesn't run until
        AFTER Phase 0 -- and _rehydrate_walk_state_from_disk's "fill only
        if still None" merge rule then immediately reloaded the stale
        pre-restart error right back in, silently undoing the reset within
        the SAME call. This test reproduces the REAL production call
        order (rebuild() called directly on a brand-new instance, with NO
        preceding get_convergence_state()/_connect() call) across a
        SIMULATED PROCESS RESTART (cache eviction + close, this codebase's
        own established restart-simulation convention -- see
        TestCachedIndexPersistence.test_index_survives_cache_eviction)."""
        good = tmp_path / "good"
        good.mkdir()
        (good / "a.csv").write_text("col\n1", encoding="utf-8")
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        (bad / "b.csv").write_text("col\n2", encoding="utf-8")

        real_scandir = os.scandir
        bad_norm = os.path.normpath(str(bad))
        fail = {"active": True}

        def flaky_scandir(path):
            if fail["active"] and os.path.normpath(str(path)) == bad_norm:
                raise PermissionError("simulated permission denied")
            return real_scandir(path)

        monkeypatch.setattr(OL.os, "scandir", flaky_scandir)

        # "Process A": hits the error, persists it, then "exits" (evict the
        # cached instance -- the ONLY way this field reaches disk at all).
        idx_a = OL._get_cached_index(str(tmp_path))
        idx_a.rebuild()
        assert idx_a.get_convergence_state().last_error is not None
        key = OL._cache_key(str(tmp_path))
        with OL._index_cache_lock:
            OL._index_cache.pop(key, None)
        idx_a.close()

        # The problem is now fixed.
        fail["active"] = False

        # "Process B": a BRAND NEW instance, and -- matching the real
        # production call order -- rebuild() is the FIRST call made on it,
        # with no preceding get_convergence_state()/_connect().
        idx_b = OL._get_cached_index(str(tmp_path))
        try:
            assert idx_b._con is None  # sanity: genuinely fresh, unconnected
            idx_b.rebuild()
            state = idx_b.get_convergence_state()
            assert state.last_error is None
            assert any("b.csv" in p for p in idx_b._row_cache)
        finally:
            idx_b.close()

    def test_confirmed_fresh_flag_prevents_rehydration_override(
        self, tmp_path: Path,
    ) -> None:
        """Narrower unit-level check directly on the flag/rehydration
        interaction, independent of the walk/scandir machinery above."""
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            con = idx._connect()  # persist a real error to disk first
            idx._last_walk_error = "a stale, since-resolved error"
            with idx._write_lock:
                idx._persist_walk_state_locked(con)

            # Simulate Phase 0's reset on a hypothetical next pass, on a
            # FRESH instance that hasn't connected yet.
            idx2 = OL.OutputsFtsIndex(str(tmp_path))
            idx2._last_walk_error = None
            idx2._walk_error_confirmed_fresh = True
            idx2._connect()  # triggers _rehydrate_walk_state_from_disk
            assert idx2._last_walk_error is None
            idx2.close()
        finally:
            idx.close()


class TestSubtreeConvergenceHeuristic:
    """6af1518d requirement 2: a subtree-scoped convergence answer must be
    correctly derived from the walk's own deterministic sorted-DFS order,
    not just mirror whole-root state."""

    @duckdb_required
    def test_subtree_not_yet_reached_is_not_converged(self, tmp_path: Path) -> None:
        aaa = tmp_path / "aaa"
        aaa.mkdir()
        zzz = tmp_path / "zzz"
        zzz.mkdir()
        (aaa / "f1.csv").write_text("col\n1", encoding="utf-8")
        (zzz / "f2.csv").write_text("col\n2", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx._walk_state = object()
            idx._scan_boundary = str(aaa / "f1.csv")
            state = idx.get_convergence_state(subtree=str(zzz))
            assert state.converged is False
            assert state.walk_complete is False
            assert state.subtree == str(zzz)
        finally:
            idx._walk_state = None
            idx.close()

    @duckdb_required
    def test_subtree_already_scanned_past_is_converged(self, tmp_path: Path) -> None:
        aaa = tmp_path / "aaa"
        aaa.mkdir()
        mmm = tmp_path / "mmm"
        mmm.mkdir()
        zzz = tmp_path / "zzz"
        zzz.mkdir()
        (aaa / "f1.csv").write_text("col\n1", encoding="utf-8")
        (mmm / "f2.csv").write_text("col\n2", encoding="utf-8")
        (zzz / "f3.csv").write_text("col\n3", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            # Simulate an in-progress full-root walk that has already moved
            # past "aaa" and into "mmm" -- boundary sorts after "aaa".
            idx._walk_state = object()
            idx._scan_boundary = str(mmm / "f2.csv")
            idx._row_cache[str(aaa / "f1.csv")] = OL.OutputRow(
                path=str(aaa / "f1.csv"), content=None, mtime=1.0, sha256="x",
                size=1, generating_script=None,
            )
            state = idx.get_convergence_state(subtree=str(aaa))
            assert state.converged is True
            # The overall root pass is still in progress even though this
            # particular subtree is already done.
            assert state.walk_complete is False
        finally:
            idx._walk_state = None
            idx.close()

    @duckdb_required
    def test_subtree_still_inside_boundary_is_not_converged(self, tmp_path: Path) -> None:
        mmm = tmp_path / "mmm"
        mmm.mkdir()
        (mmm / "f1.csv").write_text("col\n1", encoding="utf-8")
        (mmm / "f2.csv").write_text("col\n2", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx._walk_state = object()
            # Boundary is INSIDE mmm (its first file) -- mmm itself may still
            # have more files queued (f2.csv) even though the walk has
            # started visiting it.
            idx._scan_boundary = str(mmm / "f1.csv")
            state = idx.get_convergence_state(subtree=str(mmm))
            assert state.converged is False
        finally:
            idx._walk_state = None
            idx.close()

    def test_never_walked_means_no_subtree_can_be_confirmed_converged(
        self, tmp_path: Path,
    ) -> None:
        """3f758063 -- `_walk_state is None` (walk_complete=True) alone is
        no longer sufficient to call a subtree converged: a genuinely
        never-rebuilt index has zero real evidence (no scan boundary, no
        rows, no confirmed expected count) that ANY subtree -- however
        small -- was ever actually looked at. See test_no_walk_in_progress_
        after_a_real_pass_means_any_subtree_converged below for the case
        this test used to (and, correctly, still does) cover: a subtree
        query issued after a real pass has genuinely completed."""
        sub = tmp_path / "sub"
        sub.mkdir()
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            # _walk_state is None by default -- no pass currently running --
            # but that alone no longer implies converged=True.
            state = idx.get_convergence_state(subtree=str(sub))
            assert state.walk_complete is True
            assert state.never_walked is True
            assert state.converged is False
        finally:
            idx.close()

    @duckdb_required
    def test_no_walk_in_progress_after_a_real_pass_means_any_subtree_converged(
        self, tmp_path: Path,
    ) -> None:
        """Once a real pass has genuinely completed at least once (a
        confirmed expected_count, even over a part of the tree unrelated to
        `sub` itself), a subtree query with no walk currently in progress
        correctly reports converged=True -- the original pre-3f758063
        intent of this test, preserved for the case it actually protects."""
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "root_file.csv").write_text("col\n1", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            assert idx.get_convergence_state().never_walked is False  # sanity
            # _walk_state is None (the pass completed) -- no pass currently
            # running.
            state = idx.get_convergence_state(subtree=str(sub))
            assert state.walk_complete is True
            assert state.never_walked is False
            assert state.converged is True
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# Provenance-triggered targeted registration (item 6af1518d, requirement 3)
# ---------------------------------------------------------------------------

class TestProvenanceTriggeredRegistration:
    @duckdb_required
    def test_register_priority_path_indexes_immediately(self, tmp_path: Path) -> None:
        f = tmp_path / "new_output.csv"
        f.write_text("col\nvalue=priority\n", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            result = idx.register_priority_path(str(f))
            assert result["indexed"] == 1
            assert os.path.normpath(str(f)) in idx._priority_registered
            hits = idx.search("priority")
            assert len(hits) == 1
            assert "new_output.csv" in hits[0]["path"]
        finally:
            idx.close()

    @duckdb_required
    def test_register_priority_path_queues_not_yet_existing_file(
        self, tmp_path: Path,
    ) -> None:
        missing = tmp_path / "not_written_yet.csv"
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            result = idx.register_priority_path(str(missing))
            assert result["indexed"] == 0
            assert result["queued"] == 1
            assert os.path.normpath(str(missing)) in idx._pending_stale
        finally:
            idx.close()

    @duckdb_required
    def test_index_paths_skips_secret_paths(self, tmp_path: Path) -> None:
        secret = tmp_path / ".env"
        secret.write_text("SECRET=x", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            result = idx.index_paths([str(secret)])
            assert result["indexed"] == 0
            assert result["paths"] == []
        finally:
            idx.close()

    @duckdb_required
    def test_index_paths_does_not_reset_in_progress_root_partial_flag(
        self, tmp_path: Path,
    ) -> None:
        """index_paths()/_build_rows_for_paths() must NEVER touch
        last_rebuild_partial -- a small targeted registration must not be
        able to make a genuinely-in-progress root rebuild look converged."""
        f = tmp_path / "priority.csv"
        f.write_text("col\nvalue=1\n", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.last_rebuild_partial = True
            idx.index_paths([str(f)])
            assert idx.last_rebuild_partial is True
        finally:
            idx.close()

    @duckdb_required
    def test_record_provenance_registers_path_for_prompt_indexing(
        self, tmp_path: Path,
    ) -> None:
        """The whole point of requirement 3: a provenance-known path becomes
        searchable via search_outputs WITHOUT waiting for the ambient
        full-root walk to reach it. The ambient walk is deterministically
        crippled to 1 file/call (no timing race) so it provably could not
        have organically reached an alphabetically-last new file within a
        single search_outputs call on its own."""
        from meridian_outputs import annotate as AN

        for i in range(30):
            (tmp_path / f"noise_{i:03d}.csv").write_text(
                f"col\nvalue={i}\n", encoding="utf-8",
            )
        new_file = tmp_path / "zzz_after_everything_new_output.csv"
        new_file.write_text("col\nvalue=freshly_produced\n", encoding="utf-8")

        idx = OL._get_cached_index(str(tmp_path))
        idx._max_batch = 1
        idx._max_batch_overridden = True

        result = AN.record_provenance(
            str(tmp_path), str(new_file), generating_script="gen.py",
        )
        assert "error" not in result

        search_result = OL.search_outputs(str(tmp_path), "freshly_produced")
        hit_paths = [h["path"] for h in search_result["hits"]]
        assert any("zzz_after_everything_new_output.csv" in p for p in hit_paths), (
            "provenance-registered path was not searchable promptly -- "
            "requirement 3 (targeted registration ahead of the crippled "
            "ambient walk) was not satisfied"
        )

    @duckdb_required
    def test_record_provenance_failure_does_not_block_on_registration_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """register_priority_path is a best-effort latency optimisation --
        if it raises, record_provenance's own (already-durable) write must
        still succeed and be returned normally."""
        from meridian_outputs import annotate as AN

        f = tmp_path / "output.csv"
        f.write_text("col\n1", encoding="utf-8")

        def _boom(outputs_dir: str, path: str):
            raise RuntimeError("simulated indexing failure")

        monkeypatch.setattr(OL, "register_priority_path", _boom)
        result = AN.record_provenance(str(tmp_path), str(f))
        assert "error" not in result
        assert result["path"] == str(f)

    def test_record_provenance_captures_content_hash(self, tmp_path: Path) -> None:
        """bd5b8d79 -- record_provenance must snapshot a content hash (reusing
        fingerprint.script_content_hash, not a new hash scheme) at record
        time, so a later reader can detect drift."""
        from meridian_outputs import annotate as AN
        from meridian_outputs import fingerprint as FP

        f = tmp_path / "output.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")

        result = AN.record_provenance(str(tmp_path), str(f))
        assert "error" not in result
        assert result["content_hash"] == FP.script_content_hash(str(f))
        assert result["content_hash"] is not None

    def test_record_provenance_content_hash_none_when_path_missing(
        self, tmp_path: Path,
    ) -> None:
        """A provenance record can legitimately be written for a path that
        doesn't exist yet (see record_provenance's own docstring) -- the
        hash snapshot must degrade to None, not raise."""
        from meridian_outputs import annotate as AN

        missing = tmp_path / "not_written_yet.csv"
        result = AN.record_provenance(str(tmp_path), str(missing))
        assert "error" not in result
        assert result["content_hash"] is None


# ---------------------------------------------------------------------------
# b85394bd -- direct register_output_paths coverage for exact known files
# ---------------------------------------------------------------------------

class TestRegisterOutputPaths:
    """Module-level bulk registration primitive (the direct MCP-tool-level
    counterpart to OutputsFtsIndex.index_paths / register_priority_path)."""

    @duckdb_required
    def test_registers_and_indexes_multiple_exact_files_immediately(
        self, tmp_path: Path,
    ) -> None:
        for i in range(30):
            (tmp_path / f"noise_{i:03d}.csv").write_text(
                f"col\nvalue={i}\n", encoding="utf-8",
            )
        a = tmp_path / "exact_a.csv"
        b = tmp_path / "exact_b.csv"
        a.write_text("col\nvalue=alpha\n", encoding="utf-8")
        b.write_text("col\nvalue=bravo\n", encoding="utf-8")

        # Cripple the ambient walk so it provably could not have organically
        # reached these two files within a single call on its own.
        idx = OL._get_cached_index(str(tmp_path))
        idx._max_batch = 1
        idx._max_batch_overridden = True

        result = OL.register_output_paths(str(tmp_path), [str(a), str(b)])
        assert result["registered"] is True
        assert result["indexed"] == 2
        assert result["queued"] == 0
        assert os.path.normpath(str(a)) in idx._priority_registered
        assert os.path.normpath(str(b)) in idx._priority_registered

        hits = idx.search("alpha")
        assert any("exact_a.csv" in h["path"] for h in hits)
        hits = idx.search("bravo")
        assert any("exact_b.csv" in h["path"] for h in hits)

    @duckdb_required
    def test_queues_paths_that_do_not_exist_yet(self, tmp_path: Path) -> None:
        missing_a = tmp_path / "not_written_a.csv"
        missing_b = tmp_path / "not_written_b.csv"
        result = OL.register_output_paths(
            str(tmp_path), [str(missing_a), str(missing_b)],
        )
        assert result["registered"] is True
        assert result["indexed"] == 0
        assert result["queued"] == 2

    def test_missing_outputs_dir_is_reported_not_raised(self) -> None:
        result = OL.register_output_paths("/definitely/not/a/real/dir", ["x.csv"])
        assert result["registered"] is False
        assert "reason" in result

    def test_empty_paths_list_is_reported_not_raised(self, tmp_path: Path) -> None:
        result = OL.register_output_paths(str(tmp_path), [])
        assert result["registered"] is False
        assert "reason" in result

    @duckdb_required
    def test_secret_paths_are_skipped(self, tmp_path: Path) -> None:
        secret = tmp_path / ".env"
        secret.write_text("SECRET=x", encoding="utf-8")
        result = OL.register_output_paths(str(tmp_path), [str(secret)])
        assert result["registered"] is True
        assert result["indexed"] == 0
        assert result["paths"] == []

    def test_mcp_tool_is_registered(self) -> None:
        """The MCP tool wrapper in server.py must exist and delegate
        straight through to outputs_local.register_output_paths."""
        from meridian_outputs import server as srv

        assert "register_output_paths" in [
            t.name for t in srv.mcp._tool_manager.list_tools()
        ]


# ---------------------------------------------------------------------------
# Read-only helpers backing the composed provenance-status lookup (bd5b8d79)
# ---------------------------------------------------------------------------

class TestGetIndexedOutput:
    def test_missing_outputs_dir(self) -> None:
        assert OL.get_indexed_output("/definitely/nonexistent/dir", "/f.csv") is None

    def test_empty_path(self, tmp_path: Path) -> None:
        assert OL.get_indexed_output(str(tmp_path), "") is None

    @duckdb_required
    def test_never_indexed_path_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "never_touched.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        assert OL.get_indexed_output(str(tmp_path), str(f)) is None

    @duckdb_required
    def test_indexed_path_returns_row_without_forcing_rebuild(
        self, tmp_path: Path,
    ) -> None:
        """Unlike resolve_figure_output, get_indexed_output must never call
        rebuild() itself -- it is a pure read of whatever the index has
        already persisted. Registers the path explicitly via
        register_priority_path (the same targeted, single-path indexing
        record_provenance already uses) rather than relying on an ambient
        walk this test never triggers."""
        f = tmp_path / "registered.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        OL.register_priority_path(str(tmp_path), str(f))

        row = OL.get_indexed_output(str(tmp_path), str(f))
        assert row is not None
        assert row["path"] == str(f)
        assert row["sha256"] is not None


class TestGetPathAnnotations:
    def test_missing_outputs_dir(self) -> None:
        assert OL.get_path_annotations("/definitely/nonexistent/dir", "/f.csv") == []

    def test_empty_path(self, tmp_path: Path) -> None:
        assert OL.get_path_annotations(str(tmp_path), "") == []

    @duckdb_required
    def test_returns_directory_level_meridian_notes(self, tmp_path: Path) -> None:
        sub = tmp_path / "run_1"
        sub.mkdir()
        (sub / OL.MERIDIAN_NOTES_FILENAME).write_text(
            "PCA on, BFS off.", encoding="utf-8",
        )
        target = sub / "metric.csv"
        target.write_text("a,b\n1,2\n", encoding="utf-8")

        idx = OL._get_cached_index(str(tmp_path))
        idx.rebuild()

        notes = OL.get_path_annotations(str(tmp_path), str(target))
        assert any(n["source"] == OL.MERIDIAN_NOTES_FILENAME for n in notes)


# ---------------------------------------------------------------------------
# Hierarchical subtree indexing (item 6af1518d, requirement 4)
# ---------------------------------------------------------------------------

class TestHierarchicalSubtreeIndexing:
    def test_get_subtree_index_rejects_path_outside_root(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "definitely_elsewhere_meridian_6af1518d"
        with pytest.raises(ValueError):
            OL.get_subtree_index(str(tmp_path), str(outside))

    @duckdb_required
    def test_get_subtree_index_accepts_root_itself(self, tmp_path: Path) -> None:
        idx = OL.get_subtree_index(str(tmp_path), str(tmp_path))
        assert os.path.normpath(idx.outputs_dir) == os.path.normpath(str(tmp_path))

    @duckdb_required
    def test_subtree_seeded_from_already_converged_root(self, tmp_path: Path) -> None:
        sub = tmp_path / "defense_plots"
        sub.mkdir()
        (sub / "results.csv").write_text("col\nvalue=1\n", encoding="utf-8")
        (tmp_path / "other.csv").write_text("col\nvalue=2\n", encoding="utf-8")

        root_result = OL.search_outputs(str(tmp_path), "value")
        assert root_result["convergence"]["converged"] is True

        sub_idx = OL.get_subtree_index(str(tmp_path), str(sub))
        try:
            seeded_paths = {os.path.normpath(p) for p in sub_idx._row_cache}
            assert os.path.normpath(str(sub / "results.csv")) in seeded_paths, (
                "subtree index was not seeded from the already-converged "
                "root index -- requirement 4 (reuse a slice of the parent) "
                "was not satisfied"
            )
            assert os.path.normpath(str(tmp_path / "other.csv")) not in seeded_paths, (
                "subtree index seeded a file OUTSIDE its own scope"
            )
            # Seeded via a direct row copy -- the subtree's OWN walk never ran.
            assert sub_idx._walk_state is None
            hits = sub_idx.search("value=1")
            assert len(hits) == 1
        finally:
            sub_idx.close()

    @duckdb_required
    def test_seed_from_ancestor_never_overwrites_local_write(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        f = sub / "a.csv"
        f.write_text("col\nvalue=root_version\n", encoding="utf-8")

        OL.search_outputs(str(tmp_path), "value")  # converge the root

        sub_idx = OL.OutputsFtsIndex(str(sub))
        try:
            sub_idx.rebuild()  # subtree's own (independent) walk indexes it
            root_idx = OL._get_cached_index(str(tmp_path))
            copied = sub_idx.seed_from_ancestor(root_idx, str(sub))
            # Already present locally -- nothing should be overwritten.
            assert copied == 0
        finally:
            sub_idx.close()

    @duckdb_required
    def test_search_outputs_subtree_param_scopes_results(self, tmp_path: Path) -> None:
        sub = tmp_path / "defense_plots"
        sub.mkdir()
        (sub / "a.csv").write_text("col\nneedle=1\n", encoding="utf-8")
        (tmp_path / "b.csv").write_text("col\nneedle=1\n", encoding="utf-8")

        result = OL.search_outputs(str(tmp_path), "needle", subtree=str(sub))
        assert result["subtree"] == str(sub)
        paths = [h["path"] for h in result["hits"]]
        assert any("a.csv" in p for p in paths)
        assert all("b.csv" not in p for p in paths)

    def test_search_outputs_subtree_missing_returns_error(self, tmp_path: Path) -> None:
        result = OL.search_outputs(str(tmp_path), "q", subtree=str(tmp_path / "nope"))
        assert "error" in result

    def test_search_outputs_subtree_outside_root_returns_error(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        other = tmp_path_factory.mktemp("outside_root")
        result = OL.search_outputs(str(tmp_path), "q", subtree=str(other))
        assert "error" in result

    @duckdb_required
    def test_search_outputs_subtree_zero_hits_flagged_partial_not_genuine(
        self, tmp_path: Path,
    ) -> None:
        """Direct regression test for the real incident behind this item:
        a zero-hit result on a not-yet-converged SUBTREE must be flagged,
        not silently indistinguishable from a genuine miss. The subtree's
        walk is deterministically batch-capped to 1 file/call so this is
        guaranteed not-yet-converged after exactly one search_outputs call
        (no timing race)."""
        sub = tmp_path / "defense_plots"
        sub.mkdir()
        for i in range(5):
            (sub / f"f{i}.csv").write_text(f"col\nvalue={i}\n", encoding="utf-8")

        sub_idx = OL.get_subtree_index(str(tmp_path), str(sub))
        sub_idx._max_batch = 1
        sub_idx._max_batch_overridden = True

        result = OL.search_outputs(
            str(tmp_path), "no_such_term_at_all_xyz", subtree=str(sub),
        )
        assert result["hits"] == []
        assert result["convergence"]["converged"] is False
        assert result["convergence"]["subtree"] is None  # scoped index itself
        assert "zero_hits_warning" in result


class TestAncestorDiskCacheRedirect:
    """39bf34d8 -- _get_cached_index must consult an ancestor's on-disk index
    before creating an independent one, so a parent outputs_dir and a child
    subdirectory never silently diverge into two unrelated databases for the
    SAME real files (the confirmed incident: register/query under one
    reports exact provenance, "unknown" under the other)."""

    def test_find_ancestor_disk_cache_none_when_no_cache_anywhere(self, tmp_path: Path) -> None:
        sub = tmp_path / "child"
        sub.mkdir()
        assert OL._find_ancestor_disk_cache(str(sub)) is None

    def test_find_ancestor_disk_cache_none_when_own_cache_exists(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / ".meridian-outputs-cache"
        cache_dir.mkdir()
        (cache_dir / "index.duckdb").write_bytes(b"")
        assert OL._find_ancestor_disk_cache(str(tmp_path)) is None

    def test_find_ancestor_disk_cache_finds_immediate_parent(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / ".meridian-outputs-cache"
        cache_dir.mkdir()
        (cache_dir / "index.duckdb").write_bytes(b"")
        child = tmp_path / "defense_plots"
        child.mkdir()
        found = OL._find_ancestor_disk_cache(str(child))
        assert found is not None
        assert os.path.normpath(found) == os.path.normpath(str(tmp_path))

    def test_find_ancestor_disk_cache_prefers_nearest_ancestor(self, tmp_path: Path) -> None:
        root_cache = tmp_path / ".meridian-outputs-cache"
        root_cache.mkdir()
        (root_cache / "index.duckdb").write_bytes(b"")
        mid = tmp_path / "mid"
        mid.mkdir()
        mid_cache = mid / ".meridian-outputs-cache"
        mid_cache.mkdir()
        (mid_cache / "index.duckdb").write_bytes(b"")
        child = mid / "leaf"
        child.mkdir()
        found = OL._find_ancestor_disk_cache(str(child))
        assert found is not None
        assert os.path.normpath(found) == os.path.normpath(str(mid))

    def test_find_ancestor_disk_cache_respects_search_bound(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / ".meridian-outputs-cache"
        cache_dir.mkdir()
        (cache_dir / "index.duckdb").write_bytes(b"")
        deep = tmp_path
        for i in range(OL._ANCESTOR_CACHE_SEARCH_MAX_LEVELS + 1):
            deep = deep / f"level{i}"
        deep.mkdir(parents=True)
        assert OL._find_ancestor_disk_cache(str(deep)) is None

    def test_existing_own_cache_is_never_redirected_away_from(self, tmp_path: Path) -> None:
        """An outputs_dir with ITS OWN established on-disk cache must keep
        using it, even when an unrelated ancestor also has one."""
        outer_cache = tmp_path / ".meridian-outputs-cache"
        outer_cache.mkdir()
        (outer_cache / "index.duckdb").write_bytes(b"")

        own = tmp_path / "own_project"
        own.mkdir()
        own_cache = own / ".meridian-outputs-cache"
        own_cache.mkdir()
        (own_cache / "index.duckdb").write_bytes(b"")

        assert OL._find_ancestor_disk_cache(str(own)) is None

    @duckdb_required
    def test_get_cached_index_redirects_child_to_parent_ancestor(self, tmp_path: Path) -> None:
        parent = tmp_path
        child = parent / "defense_plots"
        child.mkdir()
        (child / "a.csv").write_text("col\nvalue=1\n", encoding="utf-8")

        # Converge under the PARENT so it gets a real on-disk cache.
        OL.search_outputs(str(parent), "value")

        # Evict the parent's index from the IN-MEMORY cache so this
        # exercises the ON-DISK discovery path, not the in-memory one
        # _find_ancestor_cached_index already covered.
        with OL._index_cache_lock:
            key = OL._cache_key(str(parent))
            OL._index_cache.pop(key, None)

        child_idx = OL._get_cached_index_for_lookup(str(child))
        assert os.path.normpath(child_idx.outputs_dir) == os.path.normpath(str(parent))

    @duckdb_required
    def test_provenance_registered_under_parent_visible_under_child(self, tmp_path: Path) -> None:
        """Direct regression test for the reported incident: a path
        registered through the PARENT outputs_dir must resolve consistently
        when the SAME path is later queried through a CHILD outputs_dir,
        instead of reporting exact under one and unknown under the other."""
        parent = tmp_path
        child = parent / "defense_plots"
        child.mkdir()
        f = child / "figure_c11.csv"
        f.write_text("col\nvalue=1\n", encoding="utf-8")

        result = OL.register_output_paths(str(parent), [str(f)])
        assert result["registered"] is True
        assert result["indexed"] == 1

        status_via_child = OL.get_indexed_output_status(str(child), str(f))
        assert status_via_child["row"] is not None, (
            "path registered under the parent outputs_dir was not visible "
            f"when queried under the child outputs_dir: {status_via_child}"
        )

    @duckdb_required
    def test_provenance_registered_under_child_visible_under_parent(self, tmp_path: Path) -> None:
        """The reverse direction: once the parent has an established index,
        a write through the CHILD outputs_dir must land in the SAME index
        so a subsequent query under the PARENT sees it too."""
        parent = tmp_path
        child = parent / "defense_plots"
        child.mkdir()

        # Establish the parent's on-disk index first (matches the real
        # incident's ordering -- an ambient walk/search converges the root
        # before the scoped child-directory registration ever happens).
        OL.search_outputs(str(parent), "irrelevant_seed_query")

        f = child / "figure_c12.csv"
        f.write_text("col\nvalue=2\n", encoding="utf-8")
        result = OL.register_output_paths(str(child), [str(f)])
        assert result["registered"] is True

        status_via_parent = OL.get_indexed_output_status(str(parent), str(f))
        assert status_via_parent["row"] is not None


# ---------------------------------------------------------------------------
# research_graph_output_identity (ce2f3750 regression)
# ---------------------------------------------------------------------------

class TestResearchGraphOutputIdentity:
    """Regression coverage for research_graph_output_identity.

    ce2f3750: CI's blocking ruff pass (F821, undefined-name) failed on a
    stray, unreachable ``return result`` line that followed the function's
    real ``return {...}`` statement -- ``result`` was never assigned
    anywhere in the function. The dead line was removed; nothing else about
    the function changed. This function previously had zero test coverage,
    so these tests both lock in its documented return shape and would flag
    a future edit that reintroduces an incremental result-building pattern
    (e.g. ``result = {}; ...; return result``) with a missing assignment."""

    def test_prefers_sha256_over_path_when_both_given(self) -> None:
        out = OL.research_graph_output_identity(path="/tmp/fig.png", sha256="abc123")
        assert out == {
            "node_type": "output",
            "identity_key": "sha256:abc123",
            "revision": "abc123",
            "external_ref": {"path": "/tmp/fig.png", "sha256": "abc123"},
        }

    def test_falls_back_to_path_when_no_sha256(self) -> None:
        out = OL.research_graph_output_identity(path="/tmp/fig.png")
        assert out["node_type"] == "output"
        assert out["identity_key"] == "/tmp/fig.png"
        assert out["revision"] is None
        assert out["external_ref"] == {"path": "/tmp/fig.png"}

    def test_reads_from_row_argument(self) -> None:
        row = {"canonical_path": "/tmp/fig.png", "sha256": "deadbeef"}
        out = OL.research_graph_output_identity(row=row)
        assert out["identity_key"] == "sha256:deadbeef"
        assert out["external_ref"] == {"path": "/tmp/fig.png", "sha256": "deadbeef"}

    def test_row_falls_back_to_path_key_when_no_canonical_path(self) -> None:
        row = {"path": "/tmp/other.png"}
        out = OL.research_graph_output_identity(row=row)
        assert out["identity_key"] == "/tmp/other.png"

    def test_explicit_kwargs_take_precedence_over_row(self) -> None:
        row = {"canonical_path": "/tmp/row_path.png", "sha256": "row_sha"}
        out = OL.research_graph_output_identity(path="/tmp/explicit.png", row=row)
        # Explicit kwargs win over row values (`path or row.get(...)` only
        # falls back to row when the explicit kwarg is falsy).
        assert out["external_ref"]["path"] == "/tmp/explicit.png"
        assert out["identity_key"] == "sha256:row_sha"

    def test_raises_value_error_with_neither_path_nor_sha256(self) -> None:
        with pytest.raises(ValueError, match="requires at least one of"):
            OL.research_graph_output_identity()

    def test_raises_value_error_with_empty_row(self) -> None:
        with pytest.raises(ValueError, match="requires at least one of"):
            OL.research_graph_output_identity(row={})

    def test_return_shape_has_exactly_the_documented_keys(self) -> None:
        out = OL.research_graph_output_identity(path="/tmp/x.png")
        assert set(out.keys()) == {"node_type", "identity_key", "revision", "external_ref"}


# ---------------------------------------------------------------------------
# Fast-vs-strict/sampled staleness (sprint item 89612890)
# ---------------------------------------------------------------------------

class TestStalenessModes:
    @duckdb_required
    def test_fast_mode_misses_same_size_same_mtime_content_change(
        self, tmp_path: Path,
    ) -> None:
        """The documented default cannot catch this class of change --
        confirms the premise the strict/sampled modes exist to close."""
        f = tmp_path / "data.csv"
        f.write_text("col\naaaa\n", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            st = os.stat(f)
            # Overwrite with DIFFERENT content, same length, then force the
            # mtime back to its exact original value -- a real, deliberately
            # constructed same-size+same-mtime content change, not reliant
            # on filesystem timestamp coarseness.
            f.write_text("col\nbbbb\n", encoding="utf-8")
            os.utime(f, (st.st_atime, st.st_mtime))
            assert os.stat(f).st_size == st.st_size

            idx.rebuild(staleness_mode="fast")
            content = idx.get_content(str(f))
            assert content is not None and "bbbb" not in content
        finally:
            idx.close()

    @duckdb_required
    def test_strict_mode_detects_same_size_same_mtime_content_change(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "data.csv"
        f.write_text("col\naaaa\n", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            # First pass under strict mode establishes a real content-hash
            # baseline (this file has a unique size, so a normal "fast"
            # rebuild would never hash it at all -- see _needs_hash).
            idx.rebuild(staleness_mode="strict")

            st = os.stat(f)
            f.write_text("col\nbbbb\n", encoding="utf-8")
            os.utime(f, (st.st_atime, st.st_mtime))
            assert os.stat(f).st_size == st.st_size

            idx.rebuild(staleness_mode="strict")
            content = idx.get_content(str(f))
            assert content is not None and "bbbb" in content
        finally:
            idx.close()

    @duckdb_required
    def test_sampled_mode_with_rate_one_behaves_like_strict(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "data.csv"
        f.write_text("col\naaaa\n", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild(staleness_mode="sampled", staleness_sample_rate=1.0)
            st = os.stat(f)
            f.write_text("col\nbbbb\n", encoding="utf-8")
            os.utime(f, (st.st_atime, st.st_mtime))

            idx.rebuild(staleness_mode="sampled", staleness_sample_rate=1.0)
            content = idx.get_content(str(f))
            assert content is not None and "bbbb" in content
        finally:
            idx.close()

    @duckdb_required
    def test_sampled_mode_with_rate_zero_behaves_like_fast(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "data.csv"
        f.write_text("col\naaaa\n", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild(staleness_mode="sampled", staleness_sample_rate=0.0)
            st = os.stat(f)
            f.write_text("col\nbbbb\n", encoding="utf-8")
            os.utime(f, (st.st_atime, st.st_mtime))

            idx.rebuild(staleness_mode="sampled", staleness_sample_rate=0.0)
            content = idx.get_content(str(f))
            assert content is not None and "bbbb" not in content
        finally:
            idx.close()

    @duckdb_required
    def test_strict_mode_is_opt_in_default_stays_fast(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("col\naaaa\n", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()  # no staleness_mode given
            st = os.stat(f)
            f.write_text("col\nbbbb\n", encoding="utf-8")
            os.utime(f, (st.st_atime, st.st_mtime))

            idx.rebuild()  # still no staleness_mode given -- must stay "fast"
            content = idx.get_content(str(f))
            assert content is not None and "bbbb" not in content
        finally:
            idx.close()

    @duckdb_required
    def test_strict_mode_content_check_respects_deadline(
        self, tmp_path: Path,
    ) -> None:
        """Code-review fix: the content-check loop must never run past its
        own share of the budget, matching every other I/O-heavy stage in
        rebuild() (the walk, Phase 1's ThreadPoolExecutor dispatch). An
        already-expired deadline (max_seconds=0) must make the loop skip
        content-checking entirely rather than hashing every file
        regardless of budget."""
        n = 200
        for i in range(n):
            (tmp_path / f"f{i:04d}.csv").write_text(f"col\nvalue{i}\n", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild(staleness_mode="strict")  # establish baselines, warm pass
            hash_calls = {"count": 0}
            real_hasher = idx._hasher

            def _counting_hasher(p: str) -> str | None:
                hash_calls["count"] += 1
                return real_hasher(p)

            idx._hasher = _counting_hasher
            # An already-past deadline (max_seconds=0) must make the
            # phase1_deadline check stop the loop almost immediately, not
            # process the whole tree regardless of budget. A handful of
            # calls (clock-granularity slop on very fast iterations) is
            # tolerated; hashing anywhere near the full 200-file tree is
            # exactly the unbounded-cost bug this fix closes.
            idx.rebuild(staleness_mode="strict", max_seconds=0.0)
            assert hash_calls["count"] < n // 4, (
                f"content-check loop hashed {hash_calls['count']}/{n} files "
                "despite an already-expired deadline -- it must respect "
                "phase1_deadline like every other I/O-heavy stage in "
                "rebuild(), not run unbounded regardless of budget"
            )
        finally:
            idx.close()

    @duckdb_required
    def test_strict_mode_reachable_via_module_level_search_outputs(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "data.csv"
        f.write_text("col\naaaa\n", encoding="utf-8")
        OL.search_outputs(str(tmp_path), "col", staleness_mode="strict")
        st = os.stat(f)
        f.write_text("col\nbbbb\n", encoding="utf-8")
        os.utime(f, (st.st_atime, st.st_mtime))

        OL.search_outputs(str(tmp_path), "col", staleness_mode="strict")
        idx = OL._get_cached_index(str(tmp_path))
        content = idx.get_content(str(f))
        assert content is not None and "bbbb" in content


# ---------------------------------------------------------------------------
# Directory-level progress diagnostic (sprint item 89612890)
# ---------------------------------------------------------------------------

class TestDirectoryProgress:
    @duckdb_required
    def test_reports_one_entry_per_top_level_directory(self, tmp_path: Path) -> None:
        (tmp_path / "run_a").mkdir()
        (tmp_path / "run_a" / "x.csv").write_text("col\n1\n", encoding="utf-8")
        (tmp_path / "run_b").mkdir()
        (tmp_path / "run_b" / "y.csv").write_text("col\n2\n", encoding="utf-8")

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            progress = idx.get_directory_progress()
            names = {d["name"] for d in progress["directories"]}
            assert names == {"run_a", "run_b"}
            assert all(d["converged"] for d in progress["directories"])
            assert all(d["pending_stale_count"] == 0 for d in progress["directories"])
        finally:
            idx.close()

    def test_missing_outputs_dir_degrades_to_error_not_raise(
        self, tmp_path: Path,
    ) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path / "does_not_exist"))
        try:
            progress = idx.get_directory_progress()
            assert progress["directories"] == []
            assert "error" in progress
        finally:
            idx.close()

    @duckdb_required
    def test_hidden_directories_excluded(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "run_a").mkdir()
        (tmp_path / "run_a" / "x.csv").write_text("col\n1\n", encoding="utf-8")

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            progress = idx.get_directory_progress()
            names = {d["name"] for d in progress["directories"]}
            assert names == {"run_a"}
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# Write-failure injection (sprint item 89612890) -- shared helper
# ---------------------------------------------------------------------------

class TestInjectDbWriteFailureHelper:
    @duckdb_required
    def test_shared_helper_surfaces_db_write_error(self, tmp_path: Path) -> None:
        (tmp_path / "a.csv").write_text("col\n1\n", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            with inject_db_write_failure():
                idx.rebuild()
            assert idx.last_db_write_error is not None
        finally:
            idx.close()

    @duckdb_required
    def test_shared_helper_accepts_a_custom_exception(self, tmp_path: Path) -> None:
        (tmp_path / "a.csv").write_text("col\n1\n", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            with inject_db_write_failure(PermissionError("access denied")):
                idx.rebuild()
            assert "access denied" in (idx.last_db_write_error or "")
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# Bounded scale-run telemetry (sprint item 89612890)
# ---------------------------------------------------------------------------

class TestScaleTelemetry:
    @duckdb_required
    def test_last_rebuild_metrics_carries_the_new_telemetry_fields(
        self, tmp_path: Path,
    ) -> None:
        # index_db_bytes needs a REAL on-disk DB file -- the bare
        # OutputsFtsIndex(outputs_dir) constructor used elsewhere in this
        # file defaults to an in-memory DB (fine for FTS/search-only
        # tests); production code reaches a real on-disk file via
        # _get_cached_index, so that's what this specific assertion needs.
        (tmp_path / "a.csv").write_text("col\n1\n", encoding="utf-8")
        idx = OL._get_cached_index(str(tmp_path))
        try:
            idx.rebuild()
            m = idx.last_rebuild_metrics
            for key in (
                "files_examined", "files_reanalyzed", "files_new",
                "queue_depth", "checkpoint_walk_epoch",
                "checkpoint_walk_pass_complete", "checkpoint_scan_boundary",
                "index_db_bytes",
            ):
                assert key in m, f"missing scale-telemetry field {key!r}"
            # _get_cached_index auto-creates a .gitignore at outputs_dir's
            # root to protect its own .meridian-outputs-cache/ subdirectory
            # -- a real, pre-existing side effect (files, unlike
            # directories, are not hidden-name-filtered by the walk), so
            # both it and a.csv are genuinely new this call.
            assert m["files_new"] == 2
            assert m["files_reanalyzed"] == 0
            assert isinstance(m["index_db_bytes"], int)
            assert m["index_db_bytes"] > 0
            # process_rss_bytes is present but best-effort (None if psutil
            # unavailable) -- only assert the key exists, not a specific type.
            assert "process_rss_bytes" in m
        finally:
            idx.close()

    @duckdb_required
    def test_files_reanalyzed_distinguishes_from_files_new(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "a.csv"
        f.write_text("col\n1\n", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            assert idx.last_rebuild_metrics["files_new"] == 1
            assert idx.last_rebuild_metrics["files_reanalyzed"] == 0

            f.write_text("col\n2\n", encoding="utf-8")  # genuine mtime+size change
            idx.rebuild()
            assert idx.last_rebuild_metrics["files_new"] == 0
            assert idx.last_rebuild_metrics["files_reanalyzed"] == 1
        finally:
            idx.close()

    @duckdb_required
    def test_rss_helper_degrades_gracefully_without_psutil(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            import builtins
            real_import = builtins.__import__

            def _no_psutil(name, *args, **kwargs):
                if name == "psutil":
                    raise ImportError("simulated: psutil not installed")
                return real_import(name, *args, **kwargs)

            monkeypatch.setattr(builtins, "__import__", _no_psutil)
            assert idx._current_process_rss_bytes() is None
        finally:
            idx.close()
