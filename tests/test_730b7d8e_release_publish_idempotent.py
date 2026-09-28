"""Regression test for 730b7d8e: release.yml's npm/docker publish jobs must
tolerate a re-run of an already-published tag instead of failing the whole
"Build & Release Binaries" run.

Root causes confirmed live via ``gh run view --log-failed`` before this fix:

- v0.1.9 tag run 28767217852: ``npm-publish`` failed with a real npm registry
  error ("403 Forbidden ... You cannot publish over the previously published
  versions: 0.1.9") -- every job downstream of it (build-*, docker-ghcr,
  pypi-publish, release) succeeded, so this single job was the only thing
  standing between the pipeline and a real green tagged release.
- v0.1.8 tag run 28740098906: ``docker-publish``'s "Log in to Docker Hub" step
  failed with "Username and password required" even though the job's own
  "Check Docker Hub credentials" guard reported ``has=true`` -- the guard only
  checked ``DOCKER_USERNAME``, not ``DOCKER_TOKEN``.

This test does not (and cannot, without live registry credentials) exercise
the actual publish calls -- it asserts the release.yml *contract* that keeps
both jobs tolerant of re-runs, the same way test_f4ab2787_ci_workflow_contract.py
asserts deploy.yml/test.yml structure.
"""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


def _load() -> dict:
    data = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _step_runs(job: dict) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def test_release_workflow_is_valid_yaml():
    _load()


def test_npm_publish_skips_cleanly_when_version_already_published():
    """npm-publish must check the registry for the tag's version and exit 0
    (skip) rather than call `npm publish` again when it already exists --
    otherwise a re-run of an already-published tag fails the whole workflow
    run with npm's "cannot publish over the previously published versions"
    403, even though nothing is actually broken (confirmed on v0.1.9)."""
    jobs = _load()["jobs"]
    npm_job = jobs["npm-publish"]
    assert npm_job["if"] == "startsWith(github.ref, 'refs/tags/')"

    runs = "\n".join(_step_runs(npm_job))
    assert "npm view" in runs, (
        "npm-publish must query the registry for the version before publishing"
    )
    assert "exit 0" in runs, (
        "npm-publish must skip (exit 0), not fail, when the version already exists"
    )
    # The guard has to run BEFORE the actual `npm publish` call, not after.
    view_idx = runs.index("npm view")
    publish_idx = runs.index("npm publish")
    assert view_idx < publish_idx, "the already-published check must precede npm publish"


def test_docker_publish_credential_guard_requires_both_secrets():
    """The Docker Hub credential guard must require BOTH DOCKER_USERNAME and
    DOCKER_TOKEN before attempting docker/login-action -- checking only the
    username let a repo with just one secret configured reach a real login
    failure ("Username and password required", confirmed on v0.1.8) instead
    of skipping cleanly, defeating the guard's whole documented purpose."""
    jobs = _load()["jobs"]
    docker_job = jobs["docker-publish"]
    guard_step = next(
        s for s in docker_job["steps"] if s.get("name") == "Check Docker Hub credentials"
    )
    guard_run = guard_step["run"]
    assert "secrets.DOCKER_USERNAME" in guard_run
    assert "secrets.DOCKER_TOKEN" in guard_run
    assert "&&" in guard_run, (
        "the guard must require both secrets (AND), not just DOCKER_USERNAME alone"
    )

    login_step = next(s for s in docker_job["steps"] if s.get("name") == "Log in to Docker Hub")
    assert login_step["if"] == "steps.dockercreds.outputs.has == 'true'"
