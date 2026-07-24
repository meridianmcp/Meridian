"""Coverage for the ``meridian-codeindex`` CLI entry point (extraction 2b2433ca).

Proves the package is genuinely runnable standalone, with zero Meridian
involvement: one test drives :func:`meridian_codeindex.cli.run` in-process
(fast, exercises argument parsing + formatting), and one launches it as a real
subprocess via ``python -m meridian_codeindex.cli`` — the shape a user with
zero Meridian installed would actually invoke.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from meridian_codeindex import cli


def _write(path, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


_FIXTURE_PY = """def parse_token(raw):
    return raw.strip()


def unrelated_helper(x):
    return x * 2
"""


def test_run_prints_ranked_hits(tmp_path, capsys):
    _write(tmp_path / "svc.py", _FIXTURE_PY)
    db_path = str(tmp_path / "idx.duckdb")
    rc = cli.run([str(tmp_path), "parse_token", "--db-path", db_path])
    captured = capsys.readouterr()
    assert rc == 0
    assert "query: 'parse_token'" in captured.out
    assert "parse_token" in captured.out
    # Ranked hit lines start with a right-aligned rank + a bracketed score.
    assert "1. [" in captured.out


def test_run_respects_limit_and_kind(tmp_path, capsys):
    _write(tmp_path / "svc.py", _FIXTURE_PY)
    db_path = str(tmp_path / "idx.duckdb")
    rc = cli.run([
        str(tmp_path), "token", "--limit", "1", "--kind", "function",
        "--db-path", db_path,
    ])
    captured = capsys.readouterr()
    assert rc == 0
    # Exactly one ranked hit line (rank "1.") given --limit 1.
    hit_lines = [ln for ln in captured.out.splitlines() if ln.strip().startswith("1.")]
    assert len(hit_lines) == 1


def test_run_missing_dir_reports_error_and_nonzero_exit(tmp_path, capsys):
    missing = str(tmp_path / "does_not_exist_zzz")
    rc = cli.run([missing, "anything"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "error:" in captured.err
    assert "does not exist" in captured.err


def test_run_no_hits_still_exits_zero(tmp_path, capsys):
    _write(tmp_path / "svc.py", _FIXTURE_PY)
    db_path = str(tmp_path / "idx.duckdb")
    rc = cli.run([str(tmp_path), "zzz_nonexistent_term_zzz", "--db-path", db_path])
    captured = capsys.readouterr()
    assert rc == 0
    assert "no hits" in captured.out


def test_cli_runnable_as_subprocess(tmp_path):
    """A real subprocess invocation — the exact shape a user with zero
    Meridian installed would run: ``python -m meridian_codeindex.cli <repo> <query>``.
    """
    _write(tmp_path / "svc.py", _FIXTURE_PY)
    db_path = str(tmp_path / "idx.duckdb")
    pkg_root = str(Path(__file__).parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [
            sys.executable, "-m", "meridian_codeindex.cli",
            str(tmp_path), "parse_token", "--db-path", db_path,
        ],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "parse_token" in proc.stdout
