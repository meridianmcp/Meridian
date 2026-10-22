"""4d6e87dd -- pytest wrote into the user's REAL ~/.meridian/tunnel.log.

``tunnel_client.run_tunnel`` calls ``_install_tunnel_log_tee()``, which mirrors
stdout/stderr to ``~/.meridian/tunnel.log``. Dozens of tests drive
``run_tunnel`` (early-exit and happy paths alike), so every test run appended to
the developer's actual tunnel log: 22.1 MB / 11,003 ``meridian tunnel: serving``
lines from pytest, none of them timestamped -- burying the real
indexing-worker crash lines a live tunnel wrote to the same file.

Fix under test:

* ``_tunnel_log_path()`` -- one place that decides where the log lives, with a
  ``MERIDIAN_TUNNEL_LOG`` override (same shape as ``MERIDIAN_MD_ROOT``);
* an autouse fixture in ``tests/conftest.py`` (``_isolate_tunnel_log``, same
  pattern as ``_isolate_md_root``) that points that override at a per-test temp
  file for EVERY test;
* every log line carries an ISO-8601 timestamp (log file only -- the console
  stream is passed through untouched);
* minimal size-based rotation at tunnel start (one ``.1`` backup).
"""
from __future__ import annotations

import io
import itertools
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from meridian import tunnel_client as tc

_REPO_ROOT = Path(__file__).resolve().parent.parent

_ISO_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}(?:Z|[+-]\d{2}:\d{2})) ")


@pytest.fixture
def install_tee(request, capsys):
    """Return a callable that runs ``_install_tunnel_log_tee()`` against pytest's
    ``capsys`` streams (so the tee wraps THOSE, and ``capsys.readouterr()`` shows
    what reached the console side) and closes the tee'd log handle afterwards.

    Streams are captured via ``capsys`` rather than swapped by hand in a fixture:
    pytest's global capture re-binds ``sys.stdout`` at the start of each test
    phase, which would silently undo a fixture-time ``monkeypatch``.
    """

    def _install():
        path = tc._install_tunnel_log_tee()
        if isinstance(sys.stdout, tc._TeeStream):
            request.addfinalizer(sys.stdout._log_file.close)
        return path

    return _install


# ---------------------------------------------------------------------------
# Isolation: the log path is redirected for every test, never the real home.
# ---------------------------------------------------------------------------


def test_autouse_fixture_redirects_tunnel_log_path():
    real = Path.home() / ".meridian" / "tunnel.log"
    active = tc._tunnel_log_path()
    assert active != real
    assert os.environ.get("MERIDIAN_TUNNEL_LOG"), "autouse _isolate_tunnel_log must set the override"
    assert Path(os.environ["MERIDIAN_TUNNEL_LOG"]) == active


def test_tee_install_under_the_fixture_never_touches_home(monkeypatch, tmp_path, install_tee):
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(tc.Path, "home", classmethod(lambda cls: fake_home))

    installed = install_tee()
    print("meridian tunnel: serving /somewhere", flush=True)

    assert not (fake_home / ".meridian").exists(), (
        "the tee wrote under the (fake) home directory -- i.e. it would have written to "
        "the real ~/.meridian/tunnel.log"
    )
    assert installed == tc._tunnel_log_path()
    assert installed.exists()
    assert "serving /somewhere" in installed.read_text(encoding="utf-8")


def test_default_path_is_home_dot_meridian_tunnel_log(monkeypatch, tmp_path):
    fake_home = tmp_path / "h"
    monkeypatch.setattr(tc.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("MERIDIAN_TUNNEL_LOG", raising=False)
    assert tc._tunnel_log_path() == fake_home / ".meridian" / "tunnel.log"
    monkeypatch.setenv("MERIDIAN_TUNNEL_LOG", "   ")
    assert tc._tunnel_log_path() == fake_home / ".meridian" / "tunnel.log"


def test_env_override_wins(monkeypatch, tmp_path):
    target = tmp_path / "elsewhere" / "t.log"
    monkeypatch.setenv("MERIDIAN_TUNNEL_LOG", str(target))
    assert tc._tunnel_log_path() == target


@pytest.mark.subprocess_isolated
def test_running_tunnel_tests_leaves_the_real_log_path_untouched(tmp_path):
    """End-to-end: run real run_tunnel tests in a child pytest whose HOME is an
    empty temp dir and whose own environment carries NO override -- the child's
    conftest autouse fixture alone must keep tunnel.log out of that home."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(fake_home)
    env["USERPROFILE"] = str(fake_home)
    for k in ("HOMEDRIVE", "HOMEPATH", "MERIDIAN_TUNNEL_LOG", "PYTEST_ADDOPTS"):
        env.pop(k, None)

    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "tests/test_cov_tunnel_client.py",
            "-k", "test_run_tunnel_returns_2_when_no_token or test_run_tunnel_me_failure_returns_1",
            "-p", "no:xdist", "-p", "no:cacheprovider", "-q", "--timeout=60",
        ],
        cwd=str(_REPO_ROOT), env=env, capture_output=True, text=True, timeout=240,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "2 passed" in proc.stdout, proc.stdout[-2000:]
    assert not (fake_home / ".meridian" / "tunnel.log").exists(), (
        "a run_tunnel test wrote tunnel.log under the user's home (4d6e87dd)"
    )


# ---------------------------------------------------------------------------
# Timestamps: every line in the log carries an ISO-8601 timestamp.
# ---------------------------------------------------------------------------


def test_every_logged_line_carries_an_iso_timestamp(install_tee, capsys):
    install_tee()

    print("meridian tunnel: serving /repo", flush=True)
    print("  filesystem: WARNING configured root will NOT be served", file=sys.stderr, flush=True)
    sys.stdout.write("part-a")  # print() emits text and "\n" as separate writes
    sys.stdout.write("-part-b\n")
    sys.stdout.write("line1\nline2\n")  # one chunk, two lines

    lines = tc._tunnel_log_path().read_text(encoding="utf-8").splitlines()
    assert lines, "nothing was logged"
    stamps = []
    for line in lines:
        m = _ISO_PREFIX.match(line)
        assert m, f"log line without an ISO timestamp: {line!r}"
        stamps.append(m.group(1))

    now = datetime.now(timezone.utc)
    for s in stamps:
        dt = datetime.fromisoformat(s)
        assert dt.tzinfo is not None, "timestamps must carry a UTC offset"
        assert abs((now - dt).total_seconds()) < 120

    bodies = [_ISO_PREFIX.sub("", line, count=1) for line in lines]
    assert re.fullmatch(r"=== tunnel started \(pid=%d\) ===" % os.getpid(), bodies[0])
    assert bodies[1:] == [
        "meridian tunnel: serving /repo",
        "  filesystem: WARNING configured root will NOT be served",
        "part-a-part-b",  # a line split across writes gets ONE timestamp
        "line1",
        "line2",
    ]

    # The console/original streams are passed through byte-for-byte, no prefix.
    captured = capsys.readouterr()
    assert captured.out == "meridian tunnel: serving /repo\npart-a-part-b\nline1\nline2\n"
    assert captured.err == "  filesystem: WARNING configured root will NOT be served\n"


def test_timestamped_log_file_prefixes_each_line_deterministically():
    buf = io.StringIO()
    ticks = itertools.count(1)
    sink = tc._TimestampedLogFile(buf, clock=lambda: f"T{next(ticks)}")

    assert sink.write("a\nb\n") == 4
    assert buf.getvalue() == "T1 a\nT2 b\n"

    sink.write("c")
    sink.write("d")
    sink.write("e\n")
    assert buf.getvalue().endswith("T3 cde\n")

    sink.write("\n")  # an intentionally blank line is still a stamped line
    assert buf.getvalue().endswith("T3 cde\nT4 \n")

    sink.write("x\ny")
    sink.write("z\n")
    assert buf.getvalue().endswith("T4 \nT5 x\nT6 yz\n")

    assert sink.write("") == 0  # empty write is a no-op, not a stray timestamp
    assert buf.getvalue().endswith("T6 yz\n")


def test_timestamped_log_file_never_interleaves_lines_across_threads():
    buf = io.StringIO()
    ticks = itertools.count(1)
    sink = tc._TimestampedLogFile(buf, clock=lambda: f"T{next(ticks)}")

    def worker(n: int) -> None:
        for i in range(150):
            sink.write(f"w{n}-{i}\n")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = buf.getvalue().splitlines()
    assert len(lines) == 6 * 150
    assert all(re.fullmatch(r"T\d+ w\d-\d+", line) for line in lines), "a line was torn or double-stamped"


def test_tee_write_survives_a_broken_log_file():
    class BrokenLog:
        def write(self, data):
            raise OSError("disk full")

        def flush(self):
            raise OSError("disk full")

    original = io.StringIO()
    tee = tc._TeeStream(original, BrokenLog())
    assert tee.write("hello\n") == 6
    tee.flush()
    assert original.getvalue() == "hello\n"


# ---------------------------------------------------------------------------
# Rotation: minimal, best-effort, one backup.
# ---------------------------------------------------------------------------


def test_rotate_moves_an_oversized_log_to_dot_1_replacing_the_old_backup(tmp_path):
    log = tmp_path / "tunnel.log"
    log.write_bytes(b"x" * 100)
    backup = tmp_path / "tunnel.log.1"
    backup.write_bytes(b"old")

    tc._rotate_tunnel_log_if_large(log, max_bytes=50)

    assert not log.exists()
    assert backup.read_bytes() == b"x" * 100


def test_rotate_leaves_a_small_or_missing_log_alone(tmp_path):
    log = tmp_path / "tunnel.log"
    tc._rotate_tunnel_log_if_large(log, max_bytes=50)  # missing: no-op, no raise
    assert not log.exists()

    log.write_bytes(b"x" * 50)  # exactly at the limit is not "over"
    tc._rotate_tunnel_log_if_large(log, max_bytes=50)
    assert log.read_bytes() == b"x" * 50
    assert not (tmp_path / "tunnel.log.1").exists()


def test_rotate_tolerates_a_failing_replace(monkeypatch, tmp_path):
    log = tmp_path / "tunnel.log"
    log.write_bytes(b"x" * 100)

    def boom(src, dst):
        raise PermissionError("held open by another tunnel process")

    monkeypatch.setattr(tc.os, "replace", boom)
    tc._rotate_tunnel_log_if_large(log, max_bytes=50)  # must not raise
    assert log.read_bytes() == b"x" * 100


def test_install_rotates_an_oversized_log_before_opening_it(monkeypatch, install_tee):
    log = tc._tunnel_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("x" * 200, encoding="utf-8")
    monkeypatch.setattr(tc, "_TUNNEL_LOG_ROTATE_BYTES", 100)

    install_tee()

    assert Path(str(log) + ".1").read_text(encoding="utf-8") == "x" * 200
    fresh = log.read_text(encoding="utf-8").splitlines()
    assert len(fresh) == 1 and "=== tunnel started" in fresh[0]


# ---------------------------------------------------------------------------
# Best-effort contract is unchanged: a bad log location never blocks startup.
# ---------------------------------------------------------------------------


def test_install_failure_is_swallowed_and_leaves_streams_alone(monkeypatch, tmp_path, install_tee):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_TUNNEL_LOG", str(blocker / "sub" / "tunnel.log"))
    before_out, before_err = sys.stdout, sys.stderr

    assert install_tee() is None

    assert sys.stdout is before_out and sys.stderr is before_err
