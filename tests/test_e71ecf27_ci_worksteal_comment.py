"""
e71ecf27 -- Verify that test.yml documents real CI timing data for --dist=worksteal.

This test ensures:
1. The workflow file is valid YAML.
2. Both test-core and test-postgres jobs still get worksteal scheduling for the
   full suite (the change is kept -- see note below on how this is checked now).
3. The real CI timing data comment block is present in both jobs (not speculative language).

06d7835 (adaptive local/CI test runner) replaced the literal
``pytest ... --dist=worksteal ...`` invocation in both jobs' run steps with
``pixi run python scripts/run_tests.py ...`` -- ``--dist=worksteal`` is no
longer a static flag in this YAML file at all; it is a runtime decision
``scripts/run_tests.py::build_run_args`` makes (worksteal for a collected
count above the serial threshold, serial otherwise -- the full test-core/
test-postgres suite is always well above that threshold, so the real-world
scheduling behavior these tests originally guarded is unchanged). The two
worksteal-presence tests below were updated to assert the NEW invariant --
the job routes through the adaptive runner -- rather than grep for a flag
that no longer appears in this file by design. The worksteal-for-large-
selections guarantee itself is covered directly, at the unit level, by
tests/test_run_tests_policy.py::test_large_selection_uses_worksteal_with_cap
and tests/test_collect_count_verbosity_fix.py.
"""

import pathlib
import re

import yaml

WORKFLOW_PATH = pathlib.Path(__file__).parent.parent / ".github" / "workflows" / "test.yml"


def _load_yaml():
    with WORKFLOW_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _raw_text():
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_workflow_yaml_is_valid():
    """The workflow file must parse as valid YAML without exceptions."""
    data = _load_yaml()
    assert isinstance(data, dict), "test.yml must parse to a dict"
    assert "jobs" in data, "test.yml must have a 'jobs' key"


def test_test_core_job_uses_worksteal():
    """test-core job must route the full suite through the adaptive runner,
    which is what now decides worksteal vs. serial at run time (06d7835) --
    see this module's docstring for why a literal --dist=worksteal grep is
    no longer the right check."""
    data = _load_yaml()
    steps = data["jobs"]["test-core"]["steps"]
    run_steps = [s["run"] for s in steps if "run" in s]
    combined = "\n".join(run_steps)
    assert "scripts/run_tests.py" in combined, (
        "test-core must route the full suite through scripts/run_tests.py "
        "(the adaptive runner that applies --dist=worksteal for large selections)"
    )


def test_test_postgres_job_uses_worksteal():
    """test-postgres job must route the full suite through the adaptive
    runner, same reasoning as test_test_core_job_uses_worksteal above."""
    data = _load_yaml()
    steps = data["jobs"]["test-postgres"]["steps"]
    run_steps = [s["run"] for s in steps if "run" in s]
    combined = "\n".join(run_steps)
    assert "scripts/run_tests.py" in combined, (
        "test-postgres must route the full suite through scripts/run_tests.py "
        "(the adaptive runner that applies --dist=worksteal for large selections)"
    )


def test_real_timing_data_documented_in_test_core():
    """test-core section must contain real CI timing numbers (not just theoretical rationale)."""
    text = _raw_text()
    # Look for the e71ecf27 marker and real data pattern like "median=402s"
    assert "e71ecf27" in text, "Sprint item e71ecf27 marker must be in test.yml"
    assert "median=402s" in text, (
        "Real pre-change test-core median (402s) must be documented in test.yml"
    )
    assert "437s" in text, (
        "Real post-change test-core run (437s) must be documented in test.yml"
    )


def test_real_timing_data_documented_in_test_postgres():
    """test-postgres section must contain real CI timing numbers."""
    text = _raw_text()
    assert "median=485s" in text, (
        "Real pre-change test-postgres median (485s) must be documented in test.yml"
    )


def test_sample_size_caveat_present():
    """The comment must honestly state that n=1 post-change runs is too small for a verdict."""
    text = _raw_text()
    # Should have language about sample size being too small
    assert re.search(r"[Ss]ample size", text), (
        "test.yml must contain an honest caveat about small post-change sample size"
    )


def test_workflow_has_expected_jobs():
    """Sanity check: all expected jobs are still present."""
    data = _load_yaml()
    jobs = set(data["jobs"].keys())
    expected = {"test-core", "test-postgres", "test-ux", "lint", "frontend", "docs-check"}
    assert expected.issubset(jobs), f"Missing expected jobs: {expected - jobs}"
