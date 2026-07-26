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
