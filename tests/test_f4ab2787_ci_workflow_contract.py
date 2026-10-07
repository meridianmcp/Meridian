"""Regression contract between canonical dev CI and supplemental release checks."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"


def _load(path: Path) -> dict:
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(data, dict)
    return data


def test_deploy_auto_promote_consumes_the_canonical_workflow():
    deploy = _load(DEPLOY_WORKFLOW)
    canonical = _load(TEST_WORKFLOW)

    workflow_run = deploy["on"]["workflow_run"]
    assert workflow_run["workflows"] == [canonical["name"]]
    assert workflow_run["types"] == ["completed"]

    promote_if = deploy["jobs"]["auto-promote"]["if"]
    assert "github.event_name == 'workflow_run'" in promote_if
    assert "github.event.workflow_run.conclusion == 'success'" in promote_if
    assert "github.event.workflow_run.head_branch == 'dev'" in promote_if


def test_supplemental_checks_skip_canonical_workflow_runs():
    jobs = _load(DEPLOY_WORKFLOW)["jobs"]

    for job_id in ("test", "playwright-tests"):
        job = jobs[job_id]
        assert job["name"].startswith("Supplemental")
        assert "github.event_name != 'workflow_run'" in job["if"]


def test_both_workflows_document_the_non_equivalent_release_path():
    deploy_text = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    test_text = TEST_WORKFLOW.read_text(encoding="utf-8")

    for text in (deploy_text, test_text):
        assert "f4ab2787" in text
        assert "canonical" in text.lower()
        assert "supplemental" in text.lower()
        assert "30182367824" in text
        assert "30182376358" in text


def test_test_postgres_is_a_blocking_gate_for_auto_promote():
    """Regression for sprint item 3fc08c7e.

    auto-promote's ENTIRE gate is `github.event.workflow_run.conclusion ==
    'success'` for the canonical "Test (dev branch)" run (see
    test_deploy_auto_promote_consumes_the_canonical_workflow above) — it has
    no separate, per-job check. That means every job in test.yml that can run
    on a routine dev push must NOT carry `continue-on-error: true`, or a real
    failure in it silently stops mattering to the promotion gate while still
    reporting green.

    Confirmed live via the GitHub API (2026-09-24): commit 0cbef4c9's
    `test-postgres` check-run concluded `failure`, yet the "Test (dev
    branch)" workflow run it belonged to still concluded `success` (because
    test-postgres carried `continue-on-error: true` at the time) — and main
    was auto-promoted past it minutes later. This test would have failed
    against that historical config and must keep failing if the flag is
    ever reintroduced on a routine (non-continue-on-error-exempted) job.
    """
    jobs = _load(TEST_WORKFLOW)["jobs"]

    # test-postgres is the historical offender (KEYSTONE 98aa7eb7) and the
    # specific subject of this regression -- assert it directly by name so a
    # reintroduction of the flag on this exact job fails loudly and
    # unambiguously, not just as a side effect of the sweep below.
    assert "continue-on-error" not in jobs["test-postgres"], (
        "test-postgres must not carry continue-on-error: true -- doing so "
        "makes the whole 'Test (dev branch)' run report success even when "
        "Postgres-path tests genuinely fail, which silently defeats "
        "deploy.yml's auto-promote gate (see sprint item 3fc08c7e)."
    )

    # Sweep every job that actually runs on the routine dev-push path (i.e.
    # everything except meridian-docs' own preflight dependency, which is
    # legitimately gated by `needs:` rather than continue-on-error). None of
    # them may silently no-op out of the promotion gate's conclusion either.
    for job_id, job in jobs.items():
        assert job.get("continue-on-error") not in (True, "true"), (
            f"job '{job_id}' in test.yml carries continue-on-error: true -- "
            "a real failure there would stop affecting this workflow's "
            "overall conclusion, which is auto-promote's entire gate."
        )


# ---------------------------------------------------------------------------
# CI speed (2026-10-07): the supplemental suite is skipped on a dev PUSH (test.yml
# runs that commit) and on a tree the `attest` job found green under test.yml.
# These tests pin that the skip can only ever be a POSITIVE attestation: any
# doubt runs the suite, and a broken attest step blocks the deploy.
# ---------------------------------------------------------------------------


def _norm(expr: str) -> str:
    return " ".join(str(expr).split())


def test_supplemental_suite_is_skipped_only_for_dev_pushes_and_attested_trees():
    jobs = _load(DEPLOY_WORKFLOW)["jobs"]

    for job_id in ("test", "playwright-tests"):
        job = jobs[job_id]
        assert job["needs"] == "attest"
        cond = _norm(job["if"])
        assert "!(github.event_name == 'push' && github.ref == 'refs/heads/dev')" in cond
        assert "needs.attest.outputs.attested != 'true'" in cond

    # The hotfix path is still the only way to skip the suite without attestation.
    assert "inputs.hotfix != 'yes'" in _norm(jobs["test"]["if"])


def test_deploy_requires_a_successful_attest_job_unless_hotfix():
    deploy = _load(DEPLOY_WORKFLOW)["jobs"]["deploy"]

    assert "attest" in deploy["needs"]
    cond = _norm(deploy["if"])
    # attest is `skipped` on workflow_run events and hotfix dispatches; a FAILED or
    # skipped attest must never read as "tests already passed".
    assert "(inputs.hotfix == 'yes' || needs.attest.result == 'success')" in cond
    assert "(needs.test.result == 'success' || needs.test.result == 'skipped')" in cond


def test_preview_and_operator_promote_survive_a_skipped_suite():
    jobs = _load(DEPLOY_WORKFLOW)["jobs"]

    preview = _norm(jobs["deploy-preview"]["if"])
    assert preview.startswith("always()")
    assert "needs.test.result == 'skipped'" in preview
    assert "github.ref == 'refs/heads/dev'" in preview

    promote = jobs["merge-to-main"]
    assert "attest" in promote["needs"]
    cond = _norm(promote["if"])
    assert cond.startswith("always()")
    assert "needs.attest.result == 'success'" in cond
    assert "needs.smoke-preview.result == 'success'" in cond


def test_attest_job_is_read_only_and_cannot_fail_open():
    attest = _load(DEPLOY_WORKFLOW)["jobs"]["attest"]

    assert attest["permissions"] == {"contents": "read", "actions": "read"}
    assert "github.event_name != 'workflow_run'" in _norm(attest["if"])

    script = _attest_script()
    assert "set -e" not in script
    assert "exit 1" not in script
    assert script.rstrip().endswith("exit 0")
    # Only a canonical push-to-dev run counts: never a PR or a fork branch called dev.
    assert "workflows/test.yml/runs" in script
    for fragment in ("event=push", "branch=dev", "status=success", "head_sha=${c}"):
        assert fragment in script


def _attest_script() -> str:
    steps = _load(DEPLOY_WORKFLOW)["jobs"]["attest"]["steps"]
    run = [s["run"] for s in steps if s.get("id") == "check"]
    assert len(run) == 1
    return run[0]


_BASH = shutil.which("bash")

# A shell function shadows the real `gh`: it answers the one query the script makes.
_FAKE_GH = """
gh() {
  echo "$2" >> "$FAKE_LOG"
  [ -n "$FAKE_GH_FAIL" ] && return 1
  sha="${2#*head_sha=}"; sha="${sha%%&*}"
  for g in $FAKE_GREEN; do [ "$g" = "$sha" ] && { echo 1; return 0; }; done
  echo 0
}
"""


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


def _commit(repo: Path, **files: str) -> str:
    for name, body in files.items():
        (repo / name.replace("__", ".")).write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    return _git(repo, "rev-parse", "HEAD")


def _run_attest(repo: Path, tmp_path: Path, green: list[str], gh_fails: bool = False):
    out = tmp_path / "gh_output"
    log = tmp_path / "gh_log"
    out.write_text("", encoding="utf-8")
    env = dict(
        os.environ,
        GITHUB_REPOSITORY="owner/repo",
        GITHUB_OUTPUT=out.as_posix(),
        GH_TOKEN="x",
        FAKE_GREEN=" ".join(green),
        FAKE_LOG=log.as_posix(),
        FAKE_GH_FAIL="1" if gh_fails else "",
    )
    proc = subprocess.run(
        [_BASH, "-c", _FAKE_GH + _attest_script()],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    values = dict(
        line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    return proc, values.get("attested"), log.read_text(encoding="utf-8") if log.exists() else ""


@pytest.fixture
def merge_repo(tmp_path):
    """main has M0; dev adds the green commit G; `merge` is the auto-promote merge."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    base = _commit(repo, a__txt="1", CHANGELOG__md="log 1")
    _git(repo, "checkout", "-q", "-b", "dev")
    green = _commit(repo, a__txt="2")
    _git(repo, "checkout", "-q", "main")
    return repo, base, green


@pytest.mark.skipif(_BASH is None or shutil.which("git") is None, reason="needs bash and git")
class TestAttestScript:
    def test_a_commit_that_is_itself_green_is_attested(self, merge_repo, tmp_path):
        repo, _base, green = merge_repo
        _git(repo, "checkout", "-q", "dev")
        proc, attested, log = _run_attest(repo, tmp_path, [green])
        assert proc.returncode == 0, proc.stderr
        assert attested == "true"
        assert "event=push&branch=dev&status=success" in log

    def test_the_auto_promote_merge_of_a_green_commit_is_attested(self, merge_repo, tmp_path):
        repo, _base, green = merge_repo
        _git(repo, "merge", "-q", "--no-ff", "--no-edit", "dev")
        proc, attested, _log = _run_attest(repo, tmp_path, [green])
        assert proc.returncode == 0, proc.stderr
        assert attested == "true"

    def test_a_changelog_only_difference_on_main_is_still_attested(self, merge_repo, tmp_path):
        repo, _base, green = merge_repo
        _commit(repo, CHANGELOG__md="log 2")
        _git(repo, "merge", "-q", "--no-ff", "--no-edit", "dev")
        proc, attested, _log = _run_attest(repo, tmp_path, [green])
        assert proc.returncode == 0, proc.stderr
        assert attested == "true"

    def test_code_on_main_that_canonical_ci_never_saw_is_not_attested(self, merge_repo, tmp_path):
        repo, _base, green = merge_repo
        _commit(repo, b__txt="direct commit on main")
        _git(repo, "merge", "-q", "--no-ff", "--no-edit", "dev")
        proc, attested, _log = _run_attest(repo, tmp_path, [green])
        assert proc.returncode == 0, proc.stderr
        assert attested == "false"

    def test_a_tree_with_no_green_canonical_run_is_not_attested(self, merge_repo, tmp_path):
        repo, base, _green = merge_repo
        _git(repo, "merge", "-q", "--no-ff", "--no-edit", "dev")
        proc, attested, _log = _run_attest(repo, tmp_path, [base])
        assert proc.returncode == 0, proc.stderr
        assert attested == "false"

    def test_a_failed_lookup_is_not_attested_and_does_not_fail_the_job(self, merge_repo, tmp_path):
        repo, _base, green = merge_repo
        _git(repo, "merge", "-q", "--no-ff", "--no-edit", "dev")
        proc, attested, _log = _run_attest(repo, tmp_path, [green], gh_fails=True)
        assert proc.returncode == 0, proc.stderr
        assert attested == "false"
