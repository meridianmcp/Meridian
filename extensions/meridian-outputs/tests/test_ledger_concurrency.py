"""Ledger safety under multiple writers (meridian_outputs.ledger_io).

The provenance ledger (annotate.record_provenance) and the fingerprint ledger
(fingerprint.tag_output) used to be guarded by an in-process lock only, shared
one fixed ``.tmp`` name, never fsynced, and read a corrupt file as ``{}``.
These tests pin the fixed behaviour:

  - four REAL processes writing 10 entries each lose none of the 40;
  - a failed ``os.replace`` leaves the prior ledger byte-for-byte intact and
    no temporary file behind;
  - ``os.fsync`` runs before ``os.replace``;
  - corrupt bytes are quarantined (copied aside, warning logged), never
    silently wiped, and lock-free readers raise instead of reporting empty;
  - a ledger lock held by another process fails closed with
    ``IndexLockAcquireError`` inside the timeout;
  - a read nested inside a transaction does not deadlock on the
    non-reentrant lock;
  - a same-size, same-mtime replacement is seen by the next read.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import annotate as AN
from meridian_outputs import artifact_registry as AR
from meridian_outputs import fingerprint as FP
from meridian_outputs import ledger_io as LIO
from meridian_outputs import outputs_local as OL
from meridian_outputs import run_manifest as RM

_EXT_ROOT = str(Path(__file__).parent.parent)
_LOCK_TIMEOUT_ENV = "MERIDIAN_OUTPUTS_LOCK_TIMEOUT_SECONDS"


@pytest.fixture(autouse=True)
def _no_fast_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """record_provenance's best-effort DuckDB priority registration is not
    under test here; stubbing it keeps these tests on the JSON ledgers and
    fast."""
    monkeypatch.setattr(OL, "register_priority_path", lambda *a, **k: None)


def _outputs(tmp_path: Path) -> Path:
    out = tmp_path / "outputs"
    out.mkdir(exist_ok=True)
    return out


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _cache_files(outputs: Path) -> list[str]:
    cache = outputs / ".meridian-outputs-cache"
    return sorted(os.listdir(cache)) if cache.is_dir() else []


def _wait_for_file(path: str, *, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise TimeoutError(f"{path!r} was never created within {timeout}s")
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# Cross-process writers
# ---------------------------------------------------------------------------

_WRITER_SCRIPT = r"""
import os, sys, time
ext_dir, outputs_dir, script_path, worker_id, count, ready_path, gate_path = sys.argv[1:8]
count = int(count)
sys.path.insert(0, ext_dir)
from meridian_outputs import annotate, fingerprint, outputs_local

# Keep this worker on the two JSON ledgers; the DuckDB index has its own
# multiprocess tests.
outputs_local.register_priority_path = lambda *a, **k: None

paths = []
for i in range(count):
    p = os.path.join(outputs_dir, "w%s_%02d.csv" % (worker_id, i))
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("worker,i\n%s,%d\n" % (worker_id, i))
    paths.append(p)

with open(ready_path, "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))
deadline = time.monotonic() + 120.0
while not os.path.exists(gate_path):
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.005)

for i, p in enumerate(paths):
    rec = annotate.record_provenance(
        outputs_dir, p, generating_script=script_path,
        params={"worker": worker_id, "i": i},
    )
    if "error" in rec:
        sys.stderr.write(rec["error"] + "\n")
        sys.exit(4)
    fingerprint.tag_output(p, outputs_dir, script_path=script_path)
"""


class TestCrossProcessWriters:
    def test_four_processes_ten_writes_each_all_forty_entries_survive(
        self, tmp_path: Path,
    ) -> None:
        outputs = _outputs(tmp_path)
        script = _write(outputs / "gen.py", "print('gen')\n")
        gate = tmp_path / "gate"
        workers, per_worker = 4, 10
        env = dict(os.environ)
        env[_LOCK_TIMEOUT_ENV] = "120"  # contention must wait, never time out
        procs = []
        ready_paths = []
        try:
            for w in range(workers):
                ready = tmp_path / f"ready_{w}"
                ready_paths.append(ready)
                procs.append(subprocess.Popen(
                    [
                        sys.executable, "-c", _WRITER_SCRIPT, _EXT_ROOT,
                        str(outputs), str(script), str(w), str(per_worker),
                        str(ready), str(gate),
                    ],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                ))
            for ready in ready_paths:
                _wait_for_file(str(ready), timeout=120.0)
            gate.write_text("go", encoding="utf-8")  # all four start together
            for proc in procs:
                _, err = proc.communicate(timeout=240)
                assert proc.returncode == 0, err.decode("utf-8", "replace")
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()

        expected = {f"w{w}_{i:02d}.csv" for w in range(workers) for i in range(per_worker)}
        cache = outputs / ".meridian-outputs-cache"
        provenance = json.loads((cache / "provenance_ledger.json").read_text(encoding="utf-8"))
        fingerprints = json.loads((cache / "fingerprint_ledger.json").read_text(encoding="utf-8"))
        assert set(provenance) == expected
        assert set(fingerprints) == expected
        for key, rec in provenance.items():
            worker, index = key[1:-4].split("_")
            assert rec["params"] == {"worker": worker, "i": int(index)}
        leftovers = [n for n in _cache_files(outputs) if n.endswith((".tmp", ".lock"))]
        assert leftovers == []


# The run-manifest ledger and the artifact registry (F6): their writers held
# only an in-process lock, so a write from another process between one
# writer's read and its replace was silently dropped.

_REGISTRY_RUN_WRITER_SCRIPT = r"""
import os, sys, time
ext_dir, outputs_dir, worker_id, count, ready_path, gate_path = sys.argv[1:7]
count = int(count)
sys.path.insert(0, ext_dir)
from meridian_outputs import artifact_registry, run_manifest

# The convergence snapshot opens the outputs index; it is not under test.
run_manifest._safe_convergence = lambda outputs_dir: None

with open(ready_path, "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))
deadline = time.monotonic() + 120.0
while not os.path.exists(gate_path):
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.005)

for i in range(count):
    name = "w%s_%02d" % (worker_id, i)
    art = artifact_registry.register_artifact(outputs_dir, "output", generator=name + ".py")
    run_manifest.start_run_manifest(outputs_dir, run_id=name, command_name="build")
    run_manifest.finalize_run_manifest(
        outputs_dir, name, artifact_ids=[art["artifact_id"]], status="complete",
    )
"""

# One writer (the holder) has read the ledger and stops just before its
# write; the other (the racer) runs its whole write meanwhile. Without a
# cross-process lock the racer's write lands inside that window and the
# holder's write then drops it. Under the lock the racer waits for the
# holder's write, so the holder only waits ``grace`` seconds for a racer that
# never finishes first.
_PAUSED_WRITER_SCRIPT = r"""
import os, sys, time
ext_dir, outputs_dir, op, name, held_path, ready_path, done_path, grace = sys.argv[1:9]
sys.path.insert(0, ext_dir)
from meridian_outputs import artifact_registry, ledger_io, run_manifest

run_manifest._safe_convergence = lambda outputs_dir: None


def _touch(path):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))


def _wait(path, seconds):
    deadline = time.monotonic() + seconds
    while not os.path.exists(path) and time.monotonic() < deadline:
        time.sleep(0.01)


holder = held_path != "-"
if holder:
    real_write = ledger_io.write_ledger_atomic

    def _held_write(path, data):
        _touch(held_path)
        _wait(ready_path, 120.0)
        _wait(done_path, float(grace))
        real_write(path, data)

    ledger_io.write_ledger_atomic = _held_write
else:
    _touch(ready_path)

if op == "start":
    run_manifest.start_run_manifest(outputs_dir, run_id=name, command_name="build")
elif op == "finalize":
    run_manifest.finalize_run_manifest(outputs_dir, name, status="complete")
else:
    artifact_registry.register_artifact(outputs_dir, "output", generator=name)

if not holder:
    _touch(done_path)
"""


def _finish(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        _, err = proc.communicate(timeout=240)
        assert proc.returncode == 0, err.decode("utf-8", "replace")


class TestRegistryAndRunManifestCrossProcess:
    def test_four_processes_register_start_and_finalize_lose_nothing(
        self, tmp_path: Path,
    ) -> None:
        outputs = _outputs(tmp_path)
        gate = tmp_path / "gate"
        workers, per_worker = 4, 8
        env = dict(os.environ)
        env[_LOCK_TIMEOUT_ENV] = "120"  # contention must wait, never time out
        procs: list[subprocess.Popen] = []
        try:
            ready_paths = []
            for w in range(workers):
                ready = tmp_path / f"ready_{w}"
                ready_paths.append(ready)
                procs.append(subprocess.Popen(
                    [
                        sys.executable, "-c", _REGISTRY_RUN_WRITER_SCRIPT, _EXT_ROOT,
                        str(outputs), str(w), str(per_worker), str(ready), str(gate),
                    ],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                ))
            for ready in ready_paths:
                _wait_for_file(str(ready), timeout=120.0)
            gate.write_text("go", encoding="utf-8")  # all four start together
            _finish(procs)
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()

        od = str(outputs)
        expected = {f"w{w}_{i:02d}" for w in range(workers) for i in range(per_worker)}
        artifacts = AR._read_registry_strict(od)["artifacts"]
        ids_by_generator = {rec["generator"]: aid for aid, rec in artifacts.items()}
        assert set(ids_by_generator) == {name + ".py" for name in expected}
        runs = RM._read_ledger_strict(od)
        assert set(runs) == expected
        for name, run in runs.items():
            # A finalize lost to a concurrent start would still read in_progress.
            assert run["phase"] == "complete", (name, run["phase"], run["status_reason"])
            assert run["artifact_ids"] == [ids_by_generator[name + ".py"]]
        leftovers = [n for n in _cache_files(outputs) if n.endswith((".tmp", ".lock"))]
        assert leftovers == []

    @pytest.mark.parametrize(
        "holder, racer",
        [
            (("start", "r1"), ("finalize", "r0")),  # a finalize lost to a start
            (("finalize", "r0"), ("start", "r1")),  # a start lost to a finalize
            (("register", "a.py"), ("register", "b.py")),
        ],
        ids=["start-holds-finalize-races", "finalize-holds-start-races", "two-registrations"],
    )
    def test_write_between_another_process_read_and_write_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        holder: tuple[str, str], racer: tuple[str, str],
    ) -> None:
        outputs = _outputs(tmp_path)
        od = str(outputs)
        monkeypatch.setattr(RM, "_safe_convergence", lambda outputs_dir: None)
        if holder[0] != "register":
            RM.start_run_manifest(od, run_id="r0", command_name="build")
        held, ready, done = (tmp_path / n for n in ("held", "ready", "done"))
        env = dict(os.environ)
        env[_LOCK_TIMEOUT_ENV] = "120"

        def _spawn(op: str, name: str, held_path: str) -> subprocess.Popen:
            return subprocess.Popen(
                [
                    sys.executable, "-c", _PAUSED_WRITER_SCRIPT, _EXT_ROOT, od, op, name,
                    held_path, str(ready), str(done), "4",
                ],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )

        procs: list[subprocess.Popen] = []
        try:
            procs.append(_spawn(*holder, str(held)))
            _wait_for_file(str(held), timeout=120.0)  # read done, write pending
            procs.append(_spawn(*racer, "-"))
            _finish(procs)
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()

        if holder[0] == "register":
            generators = {r["generator"] for r in AR._read_registry_strict(od)["artifacts"].values()}
            assert generators == {"a.py", "b.py"}
        else:
            runs = RM._read_ledger_strict(od)
            assert set(runs) == {"r0", "r1"}
            assert runs["r0"]["phase"] == "complete"
            assert runs["r1"]["phase"] == "in_progress"
        assert [n for n in _cache_files(outputs) if n.endswith((".tmp", ".lock"))] == []


# ---------------------------------------------------------------------------
# Atomic write: failure leaves the prior ledger intact, fsync before replace
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_replace_failure_keeps_prior_provenance_ledger_and_no_tmp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outputs = _outputs(tmp_path)
        a = _write(outputs / "a.csv", "x\n1\n")
        b = _write(outputs / "b.csv", "x\n2\n")
        assert "error" not in AN.record_provenance(str(outputs), str(a))
        ledger = Path(AN._ledger_path(str(outputs)))
        before = ledger.read_bytes()

        def _boom(src: str, dst: str) -> None:
            raise OSError("simulated replace failure")

        with monkeypatch.context() as m:
            m.setattr(os, "replace", _boom)
            result = AN.record_provenance(str(outputs), str(b))

        assert "error" in result
        assert ledger.read_bytes() == before
        assert not [n for n in _cache_files(outputs) if n.endswith(".tmp")]
        assert [r["path"] for r in AN.list_provenance(str(outputs))] == [str(a.resolve())]

    def test_replace_failure_keeps_prior_fingerprint_ledger_and_no_tmp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outputs = _outputs(tmp_path)
        script = _write(outputs / "gen.py", "print(1)\n")
        a = _write(outputs / "a.csv", "x\n1\n")
        b = _write(outputs / "b.csv", "x\n2\n")
        FP.tag_output(str(a), str(outputs), script_path=str(script))
        ledger = Path(FP._ledger_path(str(outputs)))
        before = ledger.read_bytes()

        def _boom(src: str, dst: str) -> None:
            raise OSError("simulated replace failure")

        with monkeypatch.context() as m:
            m.setattr(os, "replace", _boom)
            with pytest.raises(OSError, match="simulated"):
                FP.tag_output(str(b), str(outputs), script_path=str(script))

        assert ledger.read_bytes() == before
        assert not [n for n in _cache_files(outputs) if n.endswith(".tmp")]

    def test_fsync_runs_before_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outputs = _outputs(tmp_path)
        a = _write(outputs / "a.csv", "x\n1\n")
        events: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace

        def _fsync(fd: int) -> None:
            events.append("fsync")
            real_fsync(fd)

        def _replace(src: str, dst: str) -> None:
            events.append("replace:" + os.path.basename(dst))
            real_replace(src, dst)

        with monkeypatch.context() as m:
            m.setattr(os, "fsync", _fsync)
            m.setattr(os, "replace", _replace)
            assert "error" not in AN.record_provenance(str(outputs), str(a))

        replace_at = events.index("replace:provenance_ledger.json")
        assert "fsync" in events[:replace_at]

    def test_unserialisable_value_raises_before_any_rename(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.json"
        LIO.write_ledger_atomic(str(ledger), {"a": 1})
        before = ledger.read_bytes()
        with pytest.raises(TypeError):
            LIO.write_ledger_atomic(str(ledger), {"a": object()})
        assert ledger.read_bytes() == before
        assert sorted(os.listdir(tmp_path)) == ["ledger.json"]

    def test_transient_sharing_violation_on_replace_is_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ledger = tmp_path / "ledger.json"
        real_replace = os.replace
        failures = {"left": 2}

        def _flaky(src: str, dst: str) -> None:
            if failures["left"]:
                failures["left"] -= 1
                raise PermissionError("sharing violation")
            real_replace(src, dst)

        monkeypatch.setattr(LIO, "_SHARING_RETRY_SLEEP_SECONDS", 0.0)
        with monkeypatch.context() as m:
            m.setattr(os, "replace", _flaky)
            LIO.write_ledger_atomic(str(ledger), {"k": "v"})
        assert json.loads(ledger.read_text(encoding="utf-8")) == {"k": "v"}
        assert failures["left"] == 0

    def test_persistent_sharing_violation_on_read_is_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ledger = tmp_path / "ledger.json"
        ledger.write_text("{}", encoding="utf-8")
        calls = {"n": 0}

        def _always(*_a, **_k):
            calls["n"] += 1
            raise PermissionError("sharing violation")

        monkeypatch.setattr(LIO, "_SHARING_RETRY_SLEEP_SECONDS", 0.0)
        # A module-level ``open`` shadows the builtin for ledger_io only.
        monkeypatch.setattr(LIO, "open", _always, raising=False)
        with pytest.raises(PermissionError):
            LIO.read_ledger_strict(str(ledger))
        assert calls["n"] == LIO._SHARING_RETRIES

    def test_posix_directory_fsync_is_best_effort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        opened: list[str] = []

        def _open(path: str, flags: int, *a, **k) -> int:
            opened.append(path)
            raise OSError("directories cannot be opened here")

        with monkeypatch.context() as m:
            m.setattr(LIO.os, "name", "posix")
            m.setattr(LIO.os, "open", _open)
            LIO._fsync_directory(str(tmp_path))  # must not raise
        assert opened == [str(tmp_path)]
        LIO._fsync_directory(str(tmp_path))  # real platform: never raises


# ---------------------------------------------------------------------------
# Temporary files: default mode (F7)
# ---------------------------------------------------------------------------

class TestLedgerTempFileMode:
    """F7: the temporary file came from tempfile.mkstemp, which creates it
    0600 whatever the umask, and os.replace keeps that mode on the ledger.
    Another account or container reading a bind-mounted outputs directory
    then got PermissionError, and the lenient readers reported empty."""

    def test_temp_file_is_created_exclusively_with_mode_0666(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ledger = tmp_path / "ledger.json"
        real_open = os.open
        calls: list[tuple[str, int, int]] = []

        def _spy(path, flags, mode=0o777, *args, **kwargs):
            if str(path).endswith(".tmp"):
                calls.append((os.path.basename(path), flags, mode))
            return real_open(path, flags, mode, *args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(os, "open", _spy)
            LIO.write_ledger_atomic(str(ledger), {"k": [1, 2]})

        assert len(calls) == 1, calls
        name, flags, mode = calls[0]
        assert mode == 0o666  # the umask applies, as for open(path, "w")
        assert flags & os.O_CREAT and flags & os.O_EXCL
        binary = getattr(os, "O_BINARY", 0)
        assert flags & binary == binary
        assert re.fullmatch(r"ledger\.json\.[0-9a-f]{8}\.tmp", name), name
        raw = ledger.read_bytes()
        assert b"\r\r\n" not in raw  # no CRT text-mode translation on Windows
        assert json.loads(raw.decode("utf-8")) == {"k": [1, 2]}
        assert sorted(os.listdir(tmp_path)) == ["ledger.json"]

    def test_temp_name_is_no_longer_than_the_mkstemp_name_it_replaced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R6-C: the 16-hex token made the temporary name 8 characters longer
        than tempfile.mkstemp's ``<ledger>.<8 chars>.tmp``, so on Windows
        without long-path support a cache directory 216-223 characters deep
        pushed every ledger write past MAX_PATH."""
        ledger = tmp_path / "run_manifest_ledger.json"
        real_open = os.open
        names: list[str] = []

        def _spy(path, flags, mode=0o777, *args, **kwargs):
            if str(path).endswith(".tmp"):
                names.append(os.path.basename(path))
            return real_open(path, flags, mode, *args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(os, "open", _spy)
            LIO.write_ledger_atomic(str(ledger), {"k": 1})

        fd, reference = tempfile.mkstemp(prefix=ledger.name + ".", suffix=".tmp", dir=tmp_path)
        os.close(fd)
        os.remove(reference)
        assert len(names) == 1, names
        assert len(names[0]) <= len(os.path.basename(reference)), (names[0], reference)
        assert re.fullmatch(r"run_manifest_ledger\.json\.[0-9a-f]{8}\.tmp", names[0]), names[0]

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
    @pytest.mark.parametrize("umask", [0o022, 0o002])
    def test_ledger_mode_follows_the_umask_like_a_plain_open(
        self, tmp_path: Path, umask: int,
    ) -> None:
        ledger = tmp_path / "ledger.json"
        reference = tmp_path / "reference.json"
        previous = os.umask(umask)
        try:
            LIO.write_ledger_atomic(str(ledger), {"k": 1})
            with open(reference, "w", encoding="utf-8") as fh:
                fh.write("{}")
        finally:
            os.umask(previous)
        mode = stat.S_IMODE(os.stat(ledger).st_mode)
        assert mode == 0o666 & ~umask
        assert mode == stat.S_IMODE(os.stat(reference).st_mode)


# ---------------------------------------------------------------------------
# Temporary files: leftovers from a crash are swept (F8)
# ---------------------------------------------------------------------------

def _aged(path: Path, seconds: float) -> Path:
    path.write_bytes(b'{"leftover": "' + b"x" * 4096 + b'"}')
    when = time.time() - seconds
    os.utime(path, (when, when))
    return path


class TestLedgerTempFileSweep:
    """F8: a crash between creating the temporary file and os.replace left a
    full-size copy of the ledger in the cache, and nothing ever removed it."""

    def test_next_write_removes_this_ledgers_temp_files_older_than_an_hour(
        self, tmp_path: Path,
    ) -> None:
        ledger = tmp_path / "ledger.json"
        stale = _aged(tmp_path / f"ledger.json.{'a' * 8}.tmp", 2 * 3600)
        fresh = _aged(tmp_path / f"ledger.json.{'b' * 8}.tmp", 60)
        # Old, but not this ledger's temporary files: never touched.
        others = [
            _aged(tmp_path / f"other.json.{'c' * 8}.tmp", 2 * 3600),
            _aged(tmp_path / "ledger.json.corrupt-0123456789abcdef", 2 * 3600),
            _aged(tmp_path / "ledger.json.lock", 2 * 3600),
            _aged(tmp_path / "ledger.json.notatoken.tmp", 2 * 3600),
        ]

        LIO.write_ledger_atomic(str(ledger), {"k": 1})

        assert not stale.exists()
        assert fresh.exists()
        assert all(p.exists() for p in others)
        assert json.loads(ledger.read_text(encoding="utf-8")) == {"k": 1}

    def test_sweep_matches_only_this_ledgers_eight_hex_temp_names(self, tmp_path: Path) -> None:
        """R6-C: the sweep pattern follows the 8-hex token exactly. Another
        ledger's temporary file, or a name whose token has another length,
        is never this writer's to remove, however old."""
        ledger = tmp_path / "ledger.json"
        stale = _aged(tmp_path / "ledger.json.0a1b2c3d.tmp", 2 * 3600)
        others = [
            _aged(tmp_path / "other.json.0a1b2c3d.tmp", 2 * 3600),
            _aged(tmp_path / "ledger.json.x.0a1b2c3d.tmp", 2 * 3600),
            _aged(tmp_path / f"ledger.json.{'0a1b2c3d' * 2}.tmp", 2 * 3600),
            _aged(tmp_path / "ledger.json.0a1b2c3.tmp", 2 * 3600),
            _aged(tmp_path / "ledger.json.0a1b2c3d4.tmp", 2 * 3600),
            _aged(tmp_path / "ledger.json.0a1b2c3g.tmp", 2 * 3600),
        ]

        LIO.write_ledger_atomic(str(ledger), {"k": 1})

        assert not stale.exists()
        assert [p.name for p in others if not p.exists()] == []
        assert json.loads(ledger.read_text(encoding="utf-8")) == {"k": 1}

    def test_a_writer_sweeps_through_its_ledger_transaction(self, tmp_path: Path) -> None:
        outputs = _outputs(tmp_path)
        a = _write(outputs / "a.csv", "x\n1\n")
        assert "error" not in AN.record_provenance(str(outputs), str(a))
        cache = outputs / ".meridian-outputs-cache"
        stale = _aged(cache / f"provenance_ledger.json.{'d' * 8}.tmp", 2 * 3600)
        b = _write(outputs / "b.csv", "x\n2\n")
        assert "error" not in AN.record_provenance(str(outputs), str(b))
        assert not stale.exists()
        assert [n for n in _cache_files(outputs) if n.endswith(".tmp")] == []

    def test_a_temp_file_that_cannot_be_removed_does_not_block_the_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ledger = tmp_path / "ledger.json"
        stale = _aged(tmp_path / f"ledger.json.{'e' * 8}.tmp", 2 * 3600)
        real_remove = os.remove

        def _held_open(path: str) -> None:
            if os.path.basename(path) == stale.name:
                raise PermissionError("sharing violation")
            real_remove(path)

        with monkeypatch.context() as m:
            m.setattr(os, "remove", _held_open)
            LIO.write_ledger_atomic(str(ledger), {"k": 2})
        assert stale.exists()
        assert json.loads(ledger.read_text(encoding="utf-8")) == {"k": 2}

    @pytest.mark.parametrize("failing", ["stat", "is_file"])
    def test_an_entry_that_cannot_be_stat_ed_is_skipped_and_the_sweep_goes_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str,
    ) -> None:
        """R6-D: one entry whose is_file() or stat() raised OSError (removed
        or locked between the listing and the stat) aborted the whole sweep,
        so every other stale temporary file stayed behind."""
        ledger = tmp_path / "ledger.json"
        bad = _aged(tmp_path / f"ledger.json.{'1' * 8}.tmp", 2 * 3600)
        stale = _aged(tmp_path / f"ledger.json.{'2' * 8}.tmp", 2 * 3600)
        real_scandir = os.scandir

        class _Entry:
            def __init__(self, real: os.DirEntry) -> None:
                self._real, self.name, self.path = real, real.name, real.path

            def is_file(self, *, follow_symlinks: bool = True) -> bool:
                if failing == "is_file" and self.name == bad.name:
                    raise PermissionError("is_file failed")
                return self._real.is_file(follow_symlinks=follow_symlinks)

            def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
                if failing == "stat" and self.name == bad.name:
                    raise FileNotFoundError("stat failed")
                return self._real.stat(follow_symlinks=follow_symlinks)

        @contextmanager
        def _scandir(path: str):
            with real_scandir(path) as entries:
                # The failing entry first, so the sweep must get past it.
                yield sorted((_Entry(e) for e in entries), key=lambda e: e.name != bad.name)

        with monkeypatch.context() as m:
            m.setattr(os, "scandir", _scandir)
            LIO.write_ledger_atomic(str(ledger), {"k": 3})
        assert bad.exists()  # skipped, left for a later write
        assert not stale.exists()
        assert json.loads(ledger.read_text(encoding="utf-8")) == {"k": 3}


# ---------------------------------------------------------------------------
# Strict reads and quarantine
# ---------------------------------------------------------------------------

_GARBAGE = b'{"results.csv": {"path": "results.csv", "note": "trunc'


class TestStrictReadAndQuarantine:
    def test_missing_ledger_reads_empty(self, tmp_path: Path) -> None:
        assert LIO.read_ledger_strict(str(tmp_path / "nope.json")) == {}

    @pytest.mark.parametrize(
        "raw, reason",
        [
            (b"", "empty"),
            (b"   \n", "empty"),
            (b"\xff\xfe\x00not utf8", "UTF-8"),
            (_GARBAGE, "not valid JSON"),
            (b"[1, 2, 3]", "list, not an object"),
        ],
    )
    def test_damaged_bytes_raise_never_read_as_empty(
        self, tmp_path: Path, raw: bytes, reason: str,
    ) -> None:
        ledger = tmp_path / "ledger.json"
        ledger.write_bytes(raw)
        with pytest.raises(LIO.LedgerCorruptError) as exc_info:
            LIO.read_ledger_strict(str(ledger))
        assert reason in str(exc_info.value)
        assert str(tmp_path) not in str(exc_info.value)  # basename only

    def test_provenance_write_quarantines_corrupt_bytes_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        outputs = _outputs(tmp_path)
        a = _write(outputs / "a.csv", "x\n1\n")
        ledger = Path(AN._ledger_path(str(outputs)))
        ledger.write_bytes(_GARBAGE)

        with caplog.at_level(logging.WARNING, logger="meridian_outputs.ledger_io"):
            result = AN.record_provenance(str(outputs), str(a))

        assert "error" not in result
        quarantined = [n for n in _cache_files(outputs) if ".corrupt-" in n]
        assert len(quarantined) == 1
        assert result["ledger_quarantined"] == quarantined[0]
        cache = outputs / ".meridian-outputs-cache"
        assert (cache / quarantined[0]).read_bytes() == _GARBAGE
        assert any("corrupt" in r.getMessage() for r in caplog.records)
        # The live ledger is valid again and holds the new entry.
        assert list(json.loads(ledger.read_text(encoding="utf-8"))) == ["a.csv"]

    def test_fingerprint_write_quarantines_corrupt_bytes(self, tmp_path: Path) -> None:
        outputs = _outputs(tmp_path)
        script = _write(outputs / "gen.py", "print(1)\n")
        a = _write(outputs / "a.csv", "x\n1\n")
        ledger = Path(FP._ledger_path(str(outputs)))
        ledger.write_bytes(b"")  # what a crash after create leaves behind

        FP.tag_output(str(a), str(outputs), script_path=str(script))

        quarantined = [n for n in _cache_files(outputs) if ".corrupt-" in n]
        assert len(quarantined) == 1
        assert (outputs / ".meridian-outputs-cache" / quarantined[0]).read_bytes() == b""
        assert list(json.loads(ledger.read_text(encoding="utf-8"))) == ["a.csv"]

    def test_lock_free_readers_raise_on_corrupt_ledgers_and_never_quarantine(
        self, tmp_path: Path,
    ) -> None:
        outputs = _outputs(tmp_path)
        Path(AN._ledger_path(str(outputs))).write_bytes(_GARBAGE)
        Path(FP._ledger_path(str(outputs))).write_bytes(_GARBAGE)

        with pytest.raises(LIO.LedgerCorruptError):
            AN.list_provenance(str(outputs))
        with pytest.raises(LIO.LedgerCorruptError):
            AN.get_provenance(str(outputs), str(outputs / "results.csv"))
        with pytest.raises(LIO.LedgerCorruptError):
            FP.check_staleness(str(outputs))
        with pytest.raises(LIO.LedgerCorruptError):
            FP.find_stale_by_script(str(outputs), str(outputs / "gen.py"))
        assert not [n for n in _cache_files(outputs) if ".corrupt-" in n]

    def test_failed_quarantine_copy_leaves_corrupt_ledger_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outputs = _outputs(tmp_path)
        script = _write(outputs / "gen.py", "print(1)\n")
        a = _write(outputs / "a.csv", "x\n1\n")
        prov = Path(AN._ledger_path(str(outputs)))
        fing = Path(FP._ledger_path(str(outputs)))
        prov.write_bytes(_GARBAGE)
        fing.write_bytes(_GARBAGE)

        def _no_space(path: str, raw: bytes | None = None) -> str:
            raise OSError("disk full")

        monkeypatch.setattr(LIO, "quarantine_ledger", _no_space)
        result = AN.record_provenance(str(outputs), str(a))
        assert "error" in result and "quarantine copy failed" in result["error"]
        with pytest.raises(LIO.LedgerCorruptError, match="quarantine copy failed"):
            FP.tag_output(str(a), str(outputs), script_path=str(script))
        assert prov.read_bytes() == _GARBAGE
        assert fing.read_bytes() == _GARBAGE

    def test_quarantine_never_overwrites_an_existing_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ledger = tmp_path / "ledger.json"
        ledger.write_bytes(b"second")
        fixed = str(ledger) + ".corrupt-FIXED"
        Path(fixed).write_bytes(b"first")
        monkeypatch.setattr(LIO, "_quarantine_name", lambda path, raw: fixed)

        copy = LIO.quarantine_ledger(str(ledger))

        assert copy == fixed + "-1"
        assert Path(fixed).read_bytes() == b"first"
        assert Path(copy).read_bytes() == b"second"

    def test_repeated_failing_commits_keep_one_quarantine_copy(self, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.json"
        ledger.write_bytes(_GARBAGE)
        for _ in range(3):
            with pytest.raises(TypeError):
                with LIO.ledger_transaction(str(ledger)) as txn:
                    assert txn.load() == {}
                    txn.commit({"bad": object()})  # fails after the quarantine
        copies = [n for n in os.listdir(tmp_path) if ".corrupt-" in n]
        assert len(copies) == 1, copies
        assert (tmp_path / copies[0]).read_bytes() == _GARBAGE
        assert ledger.read_bytes() == _GARBAGE  # never replaced

        # Different corrupt bytes are a different quarantine copy.
        ledger.write_bytes(b"[1, 2]")
        second = LIO.quarantine_ledger(str(ledger))
        assert second is not None and os.path.basename(second) not in copies
        assert Path(second).read_bytes() == b"[1, 2]"

    def test_quarantine_of_missing_ledger_is_none(self, tmp_path: Path) -> None:
        assert LIO.quarantine_ledger(str(tmp_path / "gone.json")) is None

    def test_raise_quarantined_copies_the_rejected_bytes_not_the_current_file(
        self, tmp_path: Path,
    ) -> None:
        """S-R4-9: raise_quarantined re-read the file, so bytes written after
        the strict parse failed were quarantined instead of the bad ones."""
        ledger = tmp_path / "ledger.json"
        ledger.write_bytes(b'{"rewritten": true}')  # replaced after the parse failed
        exc = LIO.LedgerCorruptError(str(ledger), "file is not valid JSON")
        with pytest.raises(LIO.LedgerCorruptError) as info:
            LIO.raise_quarantined(exc, _GARBAGE)
        copy = Path(info.value.quarantine_path)
        assert copy.read_bytes() == _GARBAGE
        digest = hashlib.sha256(_GARBAGE).hexdigest()[:LIO._QUARANTINE_DIGEST_CHARS]
        assert copy.name == f"ledger.json.corrupt-{digest}"
        assert ledger.read_bytes() == b'{"rewritten": true}'
        # The same rejected bytes reuse that copy, even once the file is gone.
        ledger.unlink()
        assert LIO.quarantine_ledger(str(ledger), _GARBAGE) == str(copy)
        assert sorted(os.listdir(tmp_path)) == [copy.name]

    def test_transaction_creates_directory_and_commits(self, tmp_path: Path) -> None:
        ledger = tmp_path / "new" / "dir" / "ledger.json"
        with LIO.ledger_transaction(str(ledger)) as txn:
            assert txn.load() == {}
            txn.commit({"k": 1})
        assert txn.committed is True
        assert txn.quarantined_path is None
        assert json.loads(ledger.read_text(encoding="utf-8")) == {"k": 1}
        assert not os.path.exists(str(ledger) + ".lock")


# ---------------------------------------------------------------------------
# Lock contention with another process: fail closed inside the timeout
# ---------------------------------------------------------------------------

_HOLDER_SCRIPT = r"""
import os, sys, time
ext_dir, ready_path, release_path = sys.argv[1:4]
ledger_paths = sys.argv[4:]
sys.path.insert(0, ext_dir)
from meridian_outputs import outputs_local
locks = [outputs_local.IndexFileLock(p, session_id="holder") for p in ledger_paths]
for lock in locks:
    lock.acquire()
with open(ready_path, "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))
deadline = time.monotonic() + 60.0
while not os.path.exists(release_path) and time.monotonic() < deadline:
    time.sleep(0.02)
for lock in locks:
    lock.release()
"""


class TestLockContention:
    def test_ledger_locked_by_another_process_raises_within_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outputs = _outputs(tmp_path)
        script = _write(outputs / "gen.py", "print(1)\n")
        a = _write(outputs / "a.csv", "x\n1\n")
        b = _write(outputs / "b.csv", "x\n2\n")
        assert "error" not in AN.record_provenance(str(outputs), str(a))
        FP.tag_output(str(a), str(outputs), script_path=str(script))
        prov = Path(AN._ledger_path(str(outputs)))
        fing = Path(FP._ledger_path(str(outputs)))
        prov_before, fing_before = prov.read_bytes(), fing.read_bytes()

        ready = tmp_path / "ready"
        release = tmp_path / "release"
        holder = subprocess.Popen([
            sys.executable, "-c", _HOLDER_SCRIPT, _EXT_ROOT,
            str(ready), str(release), str(prov), str(fing),
        ])
        try:
            _wait_for_file(str(ready))
            monkeypatch.setenv(_LOCK_TIMEOUT_ENV, "0.5")

            start = time.monotonic()
            with pytest.raises(OL.IndexLockAcquireError):
                AN.record_provenance(str(outputs), str(b))
            with pytest.raises(OL.IndexLockAcquireError):
                FP.tag_output(str(b), str(outputs), script_path=str(script))
            elapsed = time.monotonic() - start
            assert elapsed < 10.0, f"fail-closed path took {elapsed:.1f}s"
            assert prov.read_bytes() == prov_before  # nothing silently written
            assert fing.read_bytes() == fing_before
            assert holder.poll() is None  # the owner was never disturbed
        finally:
            release.write_text("release", encoding="utf-8")
            assert holder.wait(timeout=30) == 0

        # Once released, the same writes succeed.
        assert "error" not in AN.record_provenance(str(outputs), str(b))
        assert len(AN.list_provenance(str(outputs))) == 2


class TestIndexFileLockSharingViolations:
    """Two Windows failure modes the multi-writer test above exposed in
    IndexFileLock's atomic-create fallback: a release whose os.remove hits a
    sharing violation (a waiter is reading the lease) and an O_EXCL create
    that is denied while the previous lock file is delete-pending."""

    def test_release_retries_a_transient_sharing_violation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(OL, "_LOCK_REMOVE_RETRY_SLEEP_SECONDS", 0.0)
        db_path = str(tmp_path / "ledger.json")
        lock = OL.IndexFileLock(db_path)
        lock.acquire()
        real_remove = os.remove
        failures = {"left": 3}

        def _flaky_remove(path: str) -> None:
            if path.endswith(".lock") and failures["left"]:
                failures["left"] -= 1
                raise PermissionError("sharing violation")
            real_remove(path)

        with monkeypatch.context() as m:
            m.setattr(os, "remove", _flaky_remove)
            lock.release()
        assert failures["left"] == 0
        assert not os.path.exists(db_path + ".lock")
        # The same process can take the lock again immediately.
        again = OL.IndexFileLock(db_path, timeout=1.0)
        again.acquire()
        again.release()

    def test_release_gives_up_with_a_warning_and_never_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(OL, "_LOCK_REMOVE_RETRY_SLEEP_SECONDS", 0.0)
        monkeypatch.setattr(OL, "_LOCK_REMOVE_RETRIES", 3)
        lock_file = tmp_path / "x.lock"
        lock_file.write_text("{}", encoding="utf-8")

        def _denied(path: str) -> None:
            raise PermissionError("sharing violation")

        with caplog.at_level(logging.WARNING, logger=OL.__name__):
            with monkeypatch.context() as m:
                m.setattr(os, "remove", _denied)
                OL._remove_released_lock_file(str(lock_file))
        assert any("still held open" in r.getMessage() for r in caplog.records)
        OL._remove_released_lock_file(str(tmp_path / "missing.lock"))  # no error

        def _other(path: str) -> None:
            raise OSError("read-only filesystem")

        with monkeypatch.context() as m:
            m.setattr(os, "remove", _other)
            OL._remove_released_lock_file(str(lock_file))  # logged, not raised

    def test_acquire_retries_a_transient_access_denied_create(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path = str(tmp_path / "ledger.json")
        real_open = os.open
        failures = {"left": 2}

        def _flaky_open(path, flags, *a, **k):
            if str(path).endswith(".lock") and failures["left"]:
                failures["left"] -= 1
                raise PermissionError("delete pending")
            return real_open(path, flags, *a, **k)

        lock = OL.IndexFileLock(db_path, timeout=5.0)
        with monkeypatch.context() as m:
            m.setattr(os, "open", _flaky_open)
            lock.acquire()
        try:
            assert failures["left"] == 0
            assert lock._lock_mode == "atomic_create"
        finally:
            lock.release()

    def test_acquire_fails_closed_when_create_stays_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(OL, "_LOCK_CREATE_DENIED_GRACE_SECONDS", 0.2)
        db_path = str(tmp_path / "ledger.json")
        real_open = os.open

        def _denied(path, flags, *a, **k):
            if str(path).endswith(".lock"):
                raise PermissionError("access denied")
            return real_open(path, flags, *a, **k)

        lock = OL.IndexFileLock(db_path, timeout=30.0)
        start = time.monotonic()
        with monkeypatch.context() as m:
            m.setattr(os, "open", _denied)
            with pytest.raises(OL.IndexLockAcquireError, match="failed to create lock file"):
                lock.acquire()
        assert time.monotonic() - start < 10.0
        # The in-process lock was released on failure.
        assert lock._thread_lock.acquire(blocking=False)
        lock._thread_lock.release()


def _write_lease_file(lock_path: str, **overrides: object) -> dict:
    """Write a lock-file lease; the default is a stale owner on another host
    (pid liveness unknown, heartbeat far past any threshold)."""
    lease: dict = {
        "pid": 4, "hostname": "some-other-host-entirely", "session_id": "gone",
        "started_at": time.time() - 10_000, "heartbeat_at": time.time() - 10_000,
        "lock_mode": "atomic_create", "token": "stale-token",
    }
    lease.update(overrides)
    Path(lock_path).write_text(json.dumps(lease), encoding="utf-8")
    return lease


class TestIndexFileLockStaleReclaim:
    """Stale-lock reclaim in IndexFileLock's atomic-create fallback: bounded
    by the timeout, never removes a lock file that changed hands, and never
    lets an abandoned lock file block writers forever."""

    def test_unremovable_stale_lock_raises_near_the_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path = str(tmp_path / "ledger.json")
        lock_path = db_path + ".lock"
        _write_lease_file(lock_path)
        assert OL.read_index_lock_owner(db_path).is_stale is True
        real_remove = os.remove
        attempts = {"n": 0}
        start = time.monotonic()

        def _held_open(path: str) -> None:
            if str(path).endswith(".lock"):
                attempts["n"] += 1
                # The stale holder keeps the file open (Windows sharing
                # violation). Let the removal through after 5 s so the old
                # unbounded busy-spin fails this test instead of hanging it.
                if time.monotonic() - start < 5.0:
                    raise PermissionError("sharing violation")
            real_remove(path)

        lock = OL.IndexFileLock(db_path, timeout=0.5)
        with monkeypatch.context() as m:
            m.setattr(os, "remove", _held_open)
            with pytest.raises(OL.IndexLockAcquireError) as exc_info:
                lock.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 3.0, f"acquire ran {elapsed:.1f}s past a 0.5s timeout"
        assert attempts["n"] < 50, f"{attempts['n']} removal attempts: busy-spin"
        assert exc_info.value.owner is not None and exc_info.value.owner.is_stale
        assert os.path.exists(lock_path)
        assert lock._thread_lock.acquire(blocking=False)
        lock._thread_lock.release()

    def test_reclaim_skips_a_lock_file_that_changed_hands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path = str(tmp_path / "ledger.json")
        lock_path = db_path + ".lock"
        _write_lease_file(lock_path)
        successor: dict = {}
        real_read = OL._read_lease_from_path
        calls = {"n": 0}

        def _successor_takes_over(path: str):
            calls["n"] += 1
            lease = real_read(path)
            if calls["n"] == 1:
                # Between this waiter's stale verdict and its removal, another
                # waiter reclaims the file and a live successor's lease lands.
                successor.update(_write_lease_file(
                    lock_path, pid=os.getpid(), hostname=socket.gethostname(),
                    session_id="successor", started_at=time.time(),
                    heartbeat_at=time.time(), token="successor-token",
                ))
            return lease

        monkeypatch.setattr(OL, "_read_lease_from_path", _successor_takes_over)
        lock = OL.IndexFileLock(db_path, timeout=0.3)
        with pytest.raises(OL.IndexLockAcquireError):
            lock.acquire()
        assert json.loads(Path(lock_path).read_text(encoding="utf-8")) == successor

    @pytest.mark.parametrize("with_lease", [True, False])
    def test_reclaim_of_an_already_removed_stale_lock_reports_free(
        self, tmp_path: Path, with_lease: bool,
    ) -> None:
        """A stale lock file that is gone by the time of the reclaim (another
        waiter removed it) leaves the path free, which the reclaim reports."""
        db_path = str(tmp_path / "ledger.json")
        lock_path = Path(db_path + ".lock")
        if with_lease:
            _write_lease_file(str(lock_path))
        else:
            lock_path.write_bytes(b"")
            old = time.time() - 10_000
            os.utime(lock_path, (old, old))
        owner, snapshot = OL._inspect_index_lock(db_path)
        assert owner.is_stale is True
        lock_path.unlink()
        assert OL._reclaim_stale_lock_file(str(lock_path), snapshot) is True

    def test_acquire_retries_at_once_when_the_stale_lock_is_already_gone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Another waiter removes the stale lock file between this waiter's
        stale verdict and its reclaim. The path is free, so the create must
        be retried at once: with a zero timeout, any trip through the poll
        branch instead raises IndexLockAcquireError naming a stale owner."""
        db_path = str(tmp_path / "ledger.json")
        lock_path = db_path + ".lock"
        _write_lease_file(lock_path)
        real_inspect = OL._inspect_index_lock
        calls = {"n": 0}

        def _reclaimed_by_another_waiter(*args: object, **kwargs: object):
            calls["n"] += 1
            verdict = real_inspect(*args, **kwargs)
            if calls["n"] == 1:
                os.remove(lock_path)
            return verdict

        monkeypatch.setattr(OL, "_inspect_index_lock", _reclaimed_by_another_waiter)
        lock = OL.IndexFileLock(db_path, session_id="next", timeout=0.0)
        lock.acquire()
        try:
            assert calls["n"] == 1
            lease = json.loads(Path(lock_path).read_text(encoding="utf-8"))
            assert lease["session_id"] == "next"
        finally:
            lock.release()
        assert not os.path.exists(lock_path)

    def test_release_keeps_a_lock_file_that_names_another_token(
        self, tmp_path: Path,
    ) -> None:
        db_path = str(tmp_path / "ledger.json")
        lock_path = db_path + ".lock"
        lock = OL.IndexFileLock(db_path, session_id="first")
        lock.acquire()
        # This holder's lease was reclaimed as stale and a successor now owns
        # the path: the lease on disk names the successor's token.
        successor = _write_lease_file(
            lock_path, pid=os.getpid(), hostname=socket.gethostname(),
            session_id="successor", started_at=time.time(),
            heartbeat_at=time.time(), token="successor-token",
        )
        lock.release()
        assert json.loads(Path(lock_path).read_text(encoding="utf-8")) == successor
        assert lock._thread_lock.acquire(blocking=False)
        lock._thread_lock.release()

    def test_release_removes_its_own_file_when_the_lease_write_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path = str(tmp_path / "ledger.json")
        lock_path = db_path + ".lock"

        def _disk_full(*_a, **_k) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(OL, "_write_lease_to_handle", _disk_full)
        lock = OL.IndexFileLock(db_path)
        lock.acquire()
        assert Path(lock_path).read_bytes() == b""  # no lease, no token on disk
        lock.release()
        assert not os.path.exists(lock_path)

    @pytest.mark.parametrize("content", [b"", b'{"pid": 12, "trunc'])
    def test_old_lock_file_without_a_lease_is_reclaimed(
        self, tmp_path: Path, content: bytes,
    ) -> None:
        """A holder whose lease write failed and which then crashed leaves a
        lock file with no parseable lease; once it is older than the stale
        threshold it must not block writers forever."""
        db_path = str(tmp_path / "ledger.json")
        lock_path = Path(db_path + ".lock")
        lock_path.write_bytes(content)
        old = time.time() - 10_000
        os.utime(lock_path, (old, old))

        owner = OL.read_index_lock_owner(db_path)
        assert owner.held is True
        assert owner.is_stale is True
        assert owner.stale_reason is not None and "no readable lease" in owner.stale_reason

        lock = OL.IndexFileLock(db_path, timeout=5.0)
        start = time.monotonic()
        lock.acquire()
        try:
            assert time.monotonic() - start < 2.0
            assert json.loads(lock_path.read_text(encoding="utf-8"))["pid"] == os.getpid()
        finally:
            lock.release()

    def test_fresh_lock_file_without_a_lease_is_not_reclaimed(
        self, tmp_path: Path,
    ) -> None:
        """A just-created lock file whose owner has not written its lease yet
        is a live owner, not an abandoned one."""
        db_path = str(tmp_path / "ledger.json")
        lock_path = Path(db_path + ".lock")
        lock_path.write_bytes(b"")

        owner = OL.read_index_lock_owner(db_path)
        assert owner.held is True
        assert owner.is_stale is False

        lock = OL.IndexFileLock(db_path, timeout=0.3)
        with pytest.raises(OL.IndexLockAcquireError):
            lock.acquire()
        assert lock_path.read_bytes() == b""


class TestTransactionHeartbeat:
    def test_commit_heartbeat_keeps_a_long_transaction_from_being_reclaimed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Work between load and commit outlasts the stale threshold; a
        waiter that inspects the lease during the (slow) commit must see a
        live owner, not a stale one it may reclaim."""
        monkeypatch.setenv("MERIDIAN_OUTPUTS_LOCK_STALE_SECONDS", "0.3")
        ledger = str(tmp_path / "ledger.json")
        seen: dict[str, OL.IndexLockOwner] = {}
        real_write = LIO.write_ledger_atomic

        def _slow_commit(path: str, data: dict) -> None:
            seen["owner"] = OL.read_index_lock_owner(path)  # the waiter's view
            real_write(path, data)

        monkeypatch.setattr(LIO, "write_ledger_atomic", _slow_commit)
        with LIO.ledger_transaction(ledger) as txn:
            txn.load()
            time.sleep(0.6)
            txn.commit({"k": 1})
        owner = seen["owner"]
        assert owner.held is True
        assert owner.is_stale is False, owner.stale_reason
        assert json.loads(Path(ledger).read_text(encoding="utf-8")) == {"k": 1}


# ---------------------------------------------------------------------------
# Non-reentrant lock: nested reads inside a transaction
# ---------------------------------------------------------------------------

class TestNestedReadInsideTransaction:
    def test_reads_inside_a_provenance_transaction_do_not_deadlock(
        self, tmp_path: Path,
    ) -> None:
        outputs = _outputs(tmp_path)
        a = _write(outputs / "a.csv", "x\n1\n")
        assert "error" not in AN.record_provenance(str(outputs), str(a), note="n1")
        ledger_path = AN._ledger_path(str(outputs))
        seen: dict[str, object] = {}

        def _run() -> None:
            with LIO.ledger_transaction(ledger_path) as txn:
                loaded = txn.load(AN._load_ledger_unlocked)
                seen["read"] = AN._read_ledger(str(outputs))
                seen["list"] = AN.list_provenance(str(outputs))
                seen["get"] = AN.get_provenance(str(outputs), str(a))
                txn.commit(loaded)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout=30)
        assert not worker.is_alive(), "nested read deadlocked inside the transaction"
        assert list(seen["read"]) == ["a.csv"]  # type: ignore[arg-type]
        assert seen["get"]["note"] == "n1"  # type: ignore[index]
        # The lock was released: an ordinary write still goes through.
        b = _write(outputs / "b.csv", "x\n2\n")
        assert "error" not in AN.record_provenance(str(outputs), str(b))


# ---------------------------------------------------------------------------
# Read cache: same size, same mtime, different file
# ---------------------------------------------------------------------------

class TestReadCacheSignature:
    def test_same_size_same_mtime_replacement_is_visible(self, tmp_path: Path) -> None:
        outputs = _outputs(tmp_path)
        a = _write(outputs / "a.csv", "x\n1\n")
        assert "error" not in AN.record_provenance(str(outputs), str(a), note="aaaa")
        assert AN.list_provenance(str(outputs))[0]["note"] == "aaaa"  # cache filled

        ledger = AN._ledger_path(str(outputs))
        old = os.stat(ledger)
        raw = Path(ledger).read_bytes()
        swapped = raw.replace(b'"aaaa"', b'"bbbb"')
        assert len(swapped) == len(raw)
        staging = ledger + ".swap"
        Path(staging).write_bytes(swapped)
        os.utime(staging, ns=(old.st_atime_ns, old.st_mtime_ns))
        os.replace(staging, ledger)
        new = os.stat(ledger)
        assert (new.st_size, new.st_mtime_ns) == (old.st_size, old.st_mtime_ns)
        if new.st_ino == old.st_ino:
            pytest.skip("filesystem does not report distinct inode numbers")

        assert AN.list_provenance(str(outputs))[0]["note"] == "bbbb"
        assert AN.get_provenance(str(outputs), str(a))["note"] == "bbbb"

    def test_unchanged_ledger_is_served_from_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outputs = _outputs(tmp_path)
        a = _write(outputs / "a.csv", "x\n1\n")
        assert "error" not in AN.record_provenance(str(outputs), str(a))
        AN.list_provenance(str(outputs))
        parses = {"n": 0}
        real = LIO.read_ledger_strict

        def _counting(path: str):
            parses["n"] += 1
            return real(path)

        monkeypatch.setattr(LIO, "read_ledger_strict", _counting)
        for _ in range(5):
            assert len(AN.list_provenance(str(outputs))) == 1
        assert parses["n"] == 0
