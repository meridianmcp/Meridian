"""Regression contract: docs deploy must fire after a bot-driven dev->main promotion.

Root cause (audit note e5810bf7, 2026-07-30): docs.yml only listens for `push` to
`main`, but both promotion paths in deploy.yml (`merge-to-main` and `auto-promote`)
push to `main` using the default GITHUB_TOKEN. GitHub's anti-recursion rule
suppresses new `push`-triggered workflow runs caused by GITHUB_TOKEN activity, so
docs.yml silently never fired after a bot promotion even though docs source kept
merging cleanly to main -- the public site went stale for weeks.

Fix: both promotion jobs now explicitly dispatch docs.yml via `workflow_dispatch`
(exempt from the anti-recursion suppression, same mechanism already used to
re-trigger deploy.yml's own prod deploy), gated on an actual docs-path diff and
pinned to the exact merged SHA. docs.yml accepts that SHA and verifies it checked
out the exact pinned commit before building.
"""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
DOCS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs.yml"

DOCS_PATH_TOKENS = ("docs", "mkdocs.yml", ".github/workflows/docs.yml")


def _load(path: Path) -> dict:
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(data, dict)
    return data


def _steps(job: dict) -> list[dict]:
    return job["steps"]


def _find_step(job: dict, name_substring: str) -> dict:
    for step in _steps(job):
        if name_substring in step.get("name", ""):
            return step
    raise AssertionError(f"no step containing {name_substring!r} in job {job}")


def test_docs_workflow_accepts_a_pinned_sha_dispatch_input():
    docs = _load(DOCS_WORKFLOW)
    inputs = docs["on"]["workflow_dispatch"]["inputs"]
    assert "sha" in inputs
    assert inputs["sha"]["required"] in (False, "false")


def test_docs_workflow_checks_out_the_pinned_sha_when_provided():
    docs = _load(DOCS_WORKFLOW)
    checkout = _find_step(docs["jobs"]["deploy"], "Checkout")
    assert checkout["uses"].startswith("actions/checkout@")
    ref_expr = checkout["with"]["ref"]
    assert "inputs.sha" in ref_expr
    assert "github.ref" in ref_expr


def test_docs_workflow_fails_closed_if_checked_out_sha_does_not_match_pin():
    docs = _load(DOCS_WORKFLOW)
    verify = _find_step(docs["jobs"]["deploy"], "Confirm exact commit")
    assert verify["if"] == "inputs.sha != ''"
    assert "exit 1" in verify["run"]
    assert "git rev-parse HEAD" in verify["run"]


def _assert_dispatches_docs_for_merged_sha(job: dict) -> None:
    dispatch = _find_step(job, "Trigger docs deploy")
    run = dispatch["run"]
    assert "gh workflow run docs.yml --ref main" in run
    assert "-f sha=" in run
    assert "steps.merge.outputs.post_sha" in run
    # Gated like docs.yml's own `paths:` filter, since push-vs-dispatch bypasses it.
    for token in DOCS_PATH_TOKENS:
        assert token in run
    assert "git diff --name-only" in run
    assert dispatch["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"


def test_merge_to_main_dispatches_docs_deploy_for_the_merged_sha():
    deploy = _load(DEPLOY_WORKFLOW)
    job = deploy["jobs"]["merge-to-main"]

    merge_step = _find_step(job, "Merge dev")
    assert merge_step["id"] == "merge"
    assert "pre_sha" in merge_step["run"]
    assert "post_sha" in merge_step["run"]

    _assert_dispatches_docs_for_merged_sha(job)


def test_auto_promote_dispatches_docs_deploy_for_the_merged_sha():
    deploy = _load(DEPLOY_WORKFLOW)
    job = deploy["jobs"]["auto-promote"]

    merge_step = _find_step(job, "Merge the CI-green")
    assert merge_step["id"] == "merge"
    assert "pre_sha" in merge_step["run"]
    assert "post_sha" in merge_step["run"]
    assert "merged=true" in merge_step["run"]
    assert "merged=false" in merge_step["run"]

    docs_step = _find_step(job, "Trigger docs deploy")
    # Must be skipped when the merge step short-circuited (idempotent re-run).
    assert docs_step["if"] == "steps.merge.outputs.merged == 'true'"

    _assert_dispatches_docs_for_merged_sha(job)
