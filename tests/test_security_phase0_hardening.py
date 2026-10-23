"""Phase 0 repository hardening contract: public-repo hygiene and CI supply chain.

Pins the repo-level security controls that no runtime test exercises:

* secret-bearing / machine-local files stay out of version control
* SECURITY.md gives reporters a private channel
* Dependabot watches every ecosystem this repo ships, always targeting `dev`
* every third-party GitHub Action is pinned to a full commit SHA
* deploy.yml's auto-promote job only fires for a `push` run of THIS repository
  (a fork PR from a branch named `dev` must not be able to promote to main)

All checks read files in the checkout; nothing here touches the network.
"""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
GITHUB_DIR = REPO_ROOT / ".github"
DEPLOY_WORKFLOW = GITHUB_DIR / "workflows" / "deploy.yml"
DEPENDABOT = GITHUB_DIR / "dependabot.yml"

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<target>[^\s#@]+)@(?P<ref>[^\s#]+)(?P<tail>.*)$")


def _git_tracked(*pathspecs: str) -> list[str]:
    git = shutil.which("git")
    if git is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("git checkout not available")
    proc = subprocess.run(
        [git, "ls-files", "--", *pathspecs],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        pytest.skip(f"git ls-files failed: {proc.stderr.strip()[:120]}")
    return [ln for ln in proc.stdout.splitlines() if ln]


def _gitignore_lines() -> list[str]:
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8-sig")
    return [ln.strip() for ln in text.splitlines()]


# --- .gitignore / tracked files ---------------------------------------------


@pytest.mark.parametrize("pattern", [".env_*", "*.stackdump", ".claude/settings.local.json"])
def test_gitignore_has_pattern_exactly_once(pattern):
    assert _gitignore_lines().count(pattern) == 1


def test_env_suffix_pattern_spares_the_tracked_examples():
    for example in (".env.example", ".env.local.example"):
        assert not fnmatch.fnmatch(example, ".env_*")
        assert example not in _gitignore_lines()


def test_local_only_files_are_not_tracked():
    assert _git_tracked(".claude/settings.local.json") == []
    assert _git_tracked("commit.bat") == []
    # The shared guard registration must stay tracked.
    assert _git_tracked(".claude/settings.json") == [".claude/settings.json"]


# --- SECURITY.md --------------------------------------------------------------


def test_security_policy_points_to_private_reporting():
    text = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "https://github.com/meridianmcp/Meridian/security/advisories/new" in text
    assert "usemeridian.us" in text
    lowered = text.lower()
    assert "latest release" in lowered
    assert "production" in lowered and "never" in lowered


# --- dependabot -----------------------------------------------------------------


def _dependabot_updates() -> list[dict]:
    data = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    assert data["version"] == 2
    return data["updates"]


def test_dependabot_entries_target_dev_weekly_and_group_minor_patch():
    updates = _dependabot_updates()
    assert updates
    for entry in updates:
        label = f"{entry['package-ecosystem']}:{entry['directory']}"
        assert entry.get("target-branch") == "dev", label
        assert entry["schedule"]["interval"] == "weekly", label
        assert entry.get("open-pull-requests-limit") == 5, label
        grouped = {t for g in entry["groups"].values() for t in g["update-types"]}
        assert grouped == {"minor", "patch"}, label


def test_dependabot_covers_every_ecosystem_and_package_json_directory():
    updates = _dependabot_updates()
    by_eco: dict[str, set[str]] = {}
    for entry in updates:
        by_eco.setdefault(entry["package-ecosystem"], set()).add(entry["directory"])

    for ecosystem in ("github-actions", "pip", "docker"):
        assert "/" in by_eco.get(ecosystem, set()), ecosystem

    manifests = [m for m in _git_tracked("*package.json") if PurePosixPath(m).name == "package.json"]
    expected = {
        "/" if str(PurePosixPath(m).parent) == "." else "/" + str(PurePosixPath(m).parent)
        for m in manifests
    }
    assert expected, "expected at least one tracked package.json"
    assert by_eco.get("npm", set()) == expected


# --- workflow action pinning ----------------------------------------------------


def _workflow_files() -> list[Path]:
    return sorted(list(GITHUB_DIR.rglob("*.yml")) + list(GITHUB_DIR.rglob("*.yaml")))


def test_every_workflow_file_parses():
    for path in _workflow_files():
        assert yaml.safe_load(path.read_text(encoding="utf-8")) is not None, path.name


def test_third_party_actions_are_pinned_to_a_full_commit_sha():
    offenders: list[str] = []
    checked = 0
    for path in _workflow_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = _USES.match(line)
            if not m:
                continue
            target = m["target"]
            if target.startswith("./") or target.startswith("docker://"):
                continue
            checked += 1
            if not _SHA40.match(m["ref"]) or not m["tail"].strip().startswith("#"):
                offenders.append(f"{path.name}:{lineno}: {target}@{m['ref']}")
    assert checked > 0
    assert not offenders, "unpinned or comment-less action refs:\n" + "\n".join(offenders)


# --- deploy.yml auto-promote provenance guard ------------------------------------

_THIS_REPO = "meridianmcp/Meridian"


def _auto_promote_if() -> str:
    deploy = yaml.load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    return " ".join(deploy["jobs"]["auto-promote"]["if"].split())


def _lookup(ctx: dict, dotted: str):
    return ctx.get(dotted)


def _eval_and_of_equalities(expr: str, ctx: dict) -> bool:
    """Evaluate `a == 'lit' && b == c.d && ...` against a flat dotted-key context.

    Deliberately tiny: it only understands the shape the auto-promote gate uses, and
    raises on anything else so a future rewrite of the gate has to update this test.
    """
    for term in expr.split("&&"):
        m = re.fullmatch(r"\s*([\w.]+)\s*==\s*('[^']*'|[\w.]+)\s*", term)
        assert m, f"unsupported term in auto-promote gate: {term!r}"
        lhs = _lookup(ctx, m.group(1))
        rhs_raw = m.group(2)
        rhs = rhs_raw[1:-1] if rhs_raw.startswith("'") else _lookup(ctx, rhs_raw)
        if lhs != rhs:
            return False
    return True


def _run_ctx(*, event="push", repo=_THIS_REPO, branch="dev", conclusion="success") -> dict:
    return {
        "github.repository": _THIS_REPO,
        "github.event_name": "workflow_run",
        "github.event.workflow_run.conclusion": conclusion,
        "github.event.workflow_run.head_branch": branch,
        "github.event.workflow_run.event": event,
        "github.event.workflow_run.head_repository.full_name": repo,
    }


def test_auto_promote_guard_terms_are_present():
    gate = _auto_promote_if()
    for term in (
        "github.event_name == 'workflow_run'",
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.head_branch == 'dev'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    ):
        assert term in gate


def test_auto_promote_accepts_only_a_green_push_run_from_this_repo():
    gate = _auto_promote_if()
    # The legitimate path: a push to this repo's dev branch whose test.yml run succeeded.
    assert _eval_and_of_equalities(gate, _run_ctx()) is True
    # Fork PR from a branch literally named `dev`.
    assert _eval_and_of_equalities(gate, _run_ctx(event="pull_request", repo="someone/Meridian")) is False
    # Same-repo pull_request run (not a push) must not promote either.
    assert _eval_and_of_equalities(gate, _run_ctx(event="pull_request")) is False
    # A push event whose head repository is a fork.
    assert _eval_and_of_equalities(gate, _run_ctx(repo="someone/Meridian")) is False
    # Pre-existing conditions are preserved.
    assert _eval_and_of_equalities(gate, _run_ctx(conclusion="failure")) is False
    assert _eval_and_of_equalities(gate, _run_ctx(branch="main")) is False
