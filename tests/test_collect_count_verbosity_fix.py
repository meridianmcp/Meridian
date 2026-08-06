"""Regression test for the collect_count double-verbosity deploy-pipeline outage.

Live incident: every caller in this repo already passes its own -q, but
collect_count() unconditionally appended a second -q on top when building
its collect-only preflight args. Pytest verbosity -2 (two stacked -q flags)
drops --collect-only into a per-file "path: count" summary with NO final
"N tests collected" line at all -- confirmed reproduced both locally
(Windows) and on GitHub Actions (ubuntu-latest), silently breaking every
`pixi run test`/`test-cov` invocation with "Could not determine collected
test count; refusing to guess scheduling." (exit 2), which blocked the
dev -> main auto-promote deploy pipeline outright (GitHub Actions runs
31061313505 and 31061345000, both "Deploy to Fly.io" / "Supplemental
preview/release test gate" failures, 2026-08-06).
"""

from scripts.run_tests import _without_verbosity_args, collect_count


def test_without_verbosity_args_strips_quiet_and_verbose():
    assert _without_verbosity_args(["tests/", "-q", "--tb=short"]) == ["tests/", "--tb=short"]
    assert _without_verbosity_args(["-v", "tests/", "--quiet"]) == ["tests/"]
    assert _without_verbosity_args(["tests/"]) == ["tests/"]


def test_collect_count_does_not_double_stack_quiet(monkeypatch):
    """The constructed subprocess argv must contain exactly one -q, however
    many -q/-v flags the caller passed in -- this is what keeps pytest at
    verbosity -1 (the format parse_collected_count actually understands)
    instead of silently sliding to -2 or lower.
    """
    captured_argv = {}

    class _FakeCompleted:
        returncode = 0
        stdout = "3 tests collected in 0.01s\n"
        stderr = ""

    def _fake_run(argv, **kwargs):
        captured_argv["argv"] = argv
        return _FakeCompleted()

    monkeypatch.setattr("scripts.run_tests.subprocess.run", _fake_run)

    collected, code = collect_count(["tests/test_x.py", "-q"])

    assert code == 0
    assert collected == 3
    argv = captured_argv["argv"]
    assert argv.count("-q") == 1
    assert argv.count("--quiet") == 0


def test_collect_count_handles_multiple_caller_quiet_flags(monkeypatch):
    """Even a caller passing -q twice must still only see one -q reach pytest."""
    captured_argv = {}

    class _FakeCompleted:
        returncode = 0
        stdout = "5 tests collected in 0.01s\n"
        stderr = ""

    def _fake_run(argv, **kwargs):
        captured_argv["argv"] = argv
        return _FakeCompleted()

    monkeypatch.setattr("scripts.run_tests.subprocess.run", _fake_run)

    collected, code = collect_count(["tests/", "-q", "-q", "--tb=short"])

    assert code == 0
    assert collected == 5
    assert captured_argv["argv"].count("-q") == 1
