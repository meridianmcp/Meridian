"""Regression contract: the test workflow gates pull requests into dev, not only into main.

Before this change, test.yml declared ``pull_request`` for ``main`` only, so a
pull request into ``dev`` got no check run. Dev was then gated only after a
merge, and a green ``Test (dev branch)`` run auto-promotes dev to main.
"""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"


def _load(path: Path) -> dict:
    # BaseLoader keeps the top-level ``on`` key as the string "on" instead of
    # YAML 1.1's boolean True, matching tests/test_f4ab2787_ci_workflow_contract.py.
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(data, dict)
    return data


def test_test_workflow_runs_on_pull_requests_into_dev_and_main():
    triggers = _load(TEST_WORKFLOW)["on"]
    assert sorted(triggers["pull_request"]["branches"]) == ["dev", "main"]


def test_test_workflow_push_trigger_still_covers_dev_only():
    triggers = _load(TEST_WORKFLOW)["on"]
    assert triggers["push"]["branches"] == ["dev"]


def test_auto_promote_still_requires_a_push_event():
    # A pull_request run into dev must never satisfy the promotion gate, so the
    # auto-promote job has to keep requiring a push-originated workflow_run.
    promote_if = _load(DEPLOY_WORKFLOW)["jobs"]["auto-promote"]["if"]
    assert "github.event.workflow_run.event == 'push'" in promote_if
    assert "github.event.workflow_run.head_branch == 'dev'" in promote_if
