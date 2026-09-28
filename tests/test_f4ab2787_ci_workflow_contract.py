"""Regression contract between canonical dev CI and supplemental release checks."""

from pathlib import Path

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
