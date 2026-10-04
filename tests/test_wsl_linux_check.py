"""Regression tests for scripts/wsl-linux-check.sh (sprint item a70e6a33).

Unlike tests/test_install_linux_launcher.py (which tests a Linux-only
installer via static source inspection because it writes real systemd/
.desktop files), this script's core logic -- widening a shallow clone's
tracked-branch set and landing on a target branch's HEAD -- is plain git
plumbing that runs identically on any platform with `bash` + `git`
available (both are present on CI's ubuntu-latest AND on this repo's own
Windows dev machines via Git for Windows' bundled Git Bash). So these tests
actually EXECUTE the real script against a throwaway local git remote that
reproduces the exact shallow-clone shape confirmed live against the real
`~/meridian-test` checkout on 2026-09-24 (`--depth 1`, only
`+refs/heads/main:refs/remotes/origin/main` in remote.origin.fetch),
rather than only asserting on script source text.

`MERIDIAN_WSL_SKIP_INSTALL=1` is used throughout so these tests never need
a real pixi.toml in the throwaway fixture repos.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_GIT = shutil.which("git")


def _find_git_bash() -> str | None:
    """Find Git for Windows' Bash without ever selecting the WSL launcher."""
    git_executable = shutil.which("git")
    if git_executable is None:
        return None

    roots: list[Path] = []
    try:
        result = subprocess.run(
            [git_executable, "--exec-path"], capture_output=True, text=True, check=True
        )
        # Git for Windows reports <root>/mingw64/libexec/git-core.
        exec_path = Path(result.stdout.strip())
        if len(exec_path.parents) >= 3:
            roots.append(exec_path.parents[2])
    except (OSError, subprocess.CalledProcessError):
        pass

    # Also support Git for Windows layouts where --exec-path is unavailable or
    # customized, deriving the installation root from cmd/git.exe or
    # mingw64/bin/git.exe.
    git_path = Path(git_executable)
    for parent in git_path.parents:
        if parent.name.casefold() in {"cmd", "mingw64", "bin"}:
            roots.append(parent.parent)

    seen: set[str] = set()
    for root in roots:
        root_key = str(root).casefold()
        if root_key in seen:
            continue
        seen.add(root_key)
        for relative in (Path("bin") / "bash.exe", Path("usr") / "bin" / "bash.exe"):
            candidate = root / relative
            if candidate.is_file():
                return str(candidate)
    return None


if os.name == "nt":
    # A bare bash.exe may resolve to %SystemRoot%\\System32\\bash.exe, which
    # starts WSL and cannot consume the Windows script path used below. Locate
    # Bash relative to the Git for Windows installation instead.
    _BASH = _find_git_bash()
else:
    _BASH = shutil.which("bash")

if _GIT is None:
    _SKIP_REASON = "requires git on PATH"
elif _BASH is None and os.name == "nt":
    _SKIP_REASON = (
        "requires Git for Windows Bash on Windows; the System32 bash.exe is "
        "the WSL launcher and cannot run this Windows script path"
    )
elif _BASH is None:
    _SKIP_REASON = "requires native bash on PATH"
else:
    _SKIP_REASON = None

pytestmark = pytest.mark.skipif(
    _SKIP_REASON is not None,
    reason=_SKIP_REASON or "required shell tools are unavailable",
)

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "wsl-linux-check.sh"
# MSYS/Git-for-Windows bash's argv handling treats a backslash as an escape
# character even in a plain filename argument, so a raw Windows-style path
# (C:\Users\...) gets silently mangled into "C:Users..." (backslashes eaten)
# before bash ever sees it. Forward slashes round-trip fine on Git Bash and
# native Linux bash, so use those for every path handed to `bash`.
_SCRIPT_POSIX = _SCRIPT.as_posix()

def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def _run_script(cwd: Path, env_overrides: dict, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_overrides}
    return subprocess.run(
        [_BASH, _SCRIPT_POSIX, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def fake_remote(tmp_path):
    """A tiny bare 'remote' repo with main + dev branches diverging, so a
    test can assert the script lands on dev's real HEAD (and not main's)."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "-b", "main", cwd=remote)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-b", "main", cwd=seed)
    _git("config", "user.email", "test@example.com", cwd=seed)
    _git("config", "user.name", "Test", cwd=seed)
    (seed / "README.md").write_text("main\n")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-m", "main commit", cwd=seed)
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "origin", "main", cwd=seed)

    _git("checkout", "-b", "dev", cwd=seed)
    (seed / "README.md").write_text("dev\n")
    _git("commit", "-am", "dev commit", cwd=seed)
    _git("push", "origin", "dev", cwd=seed)
    dev_head = _git("rev-parse", "HEAD", cwd=seed).stdout.strip()

    return remote, dev_head


@pytest.fixture
def fake_shallow_clone(fake_remote, tmp_path):
    """A --depth 1 clone of fake_remote that only tracks main -- the exact
    clone shape confirmed live against the real ~/meridian-test checkout."""
    remote, dev_head = fake_remote
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", "--depth", "1", str(remote), str(checkout)],
        check=True,
        capture_output=True,
        text=True,
    )
    return checkout, dev_head


class TestFixtureReproducesTheRealBrokenShape:
    def test_shallow_clone_only_tracks_main(self, fake_shallow_clone):
        checkout, _ = fake_shallow_clone
        refspecs = _git(
            "config", "--get-all", "remote.origin.fetch", cwd=checkout
        ).stdout
        assert "heads/main" in refspecs
        assert "heads/dev" not in refspecs

    def test_plain_checkout_of_origin_dev_fails_on_this_clone_shape(self, fake_shallow_clone):
        """Sanity check on the fixture: this is the exact failure the real
        session hit and this script exists to fix."""
        checkout, _ = fake_shallow_clone
        subprocess.run(
            ["git", "fetch", "origin", "dev", "--depth", "1"],
            cwd=checkout, check=True, capture_output=True, text=True,
        )
        result = subprocess.run(
            ["git", "checkout", "-b", "should-fail", "origin/dev"],
            cwd=checkout, capture_output=True, text=True,
        )
        assert result.returncode != 0


class TestWslLinuxCheckScript:
    def test_lands_on_dev_head_from_a_shallow_main_only_clone(self, fake_shallow_clone):
        checkout, dev_head = fake_shallow_clone
        result = _run_script(
            checkout,
            {"MERIDIAN_WSL_CHECKOUT": str(checkout), "MERIDIAN_WSL_SKIP_INSTALL": "1"},
        )
        assert result.returncode == 0, result.stderr
        head = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
        assert head == dev_head

    def test_creates_a_fresh_checkout_when_none_exists_yet(self, fake_remote, tmp_path):
        remote, dev_head = fake_remote
        checkout = tmp_path / "does-not-exist-yet"
        result = _run_script(
            tmp_path,
            {
                "MERIDIAN_WSL_CHECKOUT": str(checkout),
                "MERIDIAN_WSL_REPO_URL": str(remote),
                "MERIDIAN_WSL_SKIP_INSTALL": "1",
            },
        )
        assert result.returncode == 0, result.stderr
        assert checkout.is_dir()
        head = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
        assert head == dev_head

    def test_rerunning_does_not_duplicate_the_fetch_refspec(self, fake_shallow_clone):
        """Regression guard: `git remote set-branches --add origin dev` is
        NOT idempotent on its own (confirmed live) -- calling it twice
        appends two identical refspec lines. The script must guard this."""
        checkout, _ = fake_shallow_clone
        env = {"MERIDIAN_WSL_CHECKOUT": str(checkout), "MERIDIAN_WSL_SKIP_INSTALL": "1"}
        for _ in range(2):
            result = _run_script(checkout, env)
            assert result.returncode == 0, result.stderr
        refspecs = _git(
            "config", "--get-all", "remote.origin.fetch", cwd=checkout
        ).stdout.splitlines()
        dev_lines = [line for line in refspecs if "heads/dev" in line]
        assert len(dev_lines) == 1, f"expected exactly one dev refspec, got: {dev_lines}"

    def test_passes_positional_args_through_as_a_test_subset(self, fake_shallow_clone):
        # The fake fixture repo has no scripts/run_tests.py (it's just a
        # throwaway README), so the actual `pixi run python
        # scripts/run_tests.py ...` invocation is expected to fail here --
        # this test only asserts the script correctly announces (and would
        # therefore pass through) the exact args given, not that a real test
        # run succeeds against a repo that doesn't have a test runner.
        checkout, _ = fake_shallow_clone
        result = _run_script(
            checkout,
            {"MERIDIAN_WSL_CHECKOUT": str(checkout), "MERIDIAN_WSL_SKIP_INSTALL": "1"},
            "tests/test_whatever.py",
            "-k",
            "smoke",
        )
        assert "running targeted test subset: tests/test_whatever.py -k smoke" in result.stdout

    def test_refuses_to_discard_uncommitted_changes_without_force(self, fake_shallow_clone):
        checkout, _ = fake_shallow_clone
        (checkout / "README.md").write_text("local uncommitted edit\n")
        result = _run_script(
            checkout,
            {"MERIDIAN_WSL_CHECKOUT": str(checkout), "MERIDIAN_WSL_SKIP_INSTALL": "1"},
        )
        assert result.returncode != 0
        assert "uncommitted" in (result.stdout + result.stderr).lower()
        # The refusal must be real -- the edit must survive untouched.
        assert (checkout / "README.md").read_text() == "local uncommitted edit\n"

    def test_force_discards_uncommitted_changes_and_syncs(self, fake_shallow_clone):
        checkout, dev_head = fake_shallow_clone
        (checkout / "README.md").write_text("local uncommitted edit\n")
        result = _run_script(
            checkout,
            {
                "MERIDIAN_WSL_CHECKOUT": str(checkout),
                "MERIDIAN_WSL_SKIP_INSTALL": "1",
                "MERIDIAN_WSL_FORCE": "1",
            },
        )
        assert result.returncode == 0, result.stderr
        head = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
        assert head == dev_head

    def test_falls_back_to_a_different_target_branch_when_configured(self, fake_remote, tmp_path):
        """MERIDIAN_WSL_BRANCH lets the same script sync to a non-dev ref
        (e.g. a feature branch) -- exercised here with 'main' since the
        fixture already has it, proving the branch is genuinely configurable
        and not hardcoded."""
        remote, _ = fake_remote
        checkout = tmp_path / "checkout-main"
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", "main", str(remote), str(checkout)],
            check=True, capture_output=True, text=True,
        )
        main_head = _git("rev-parse", "origin/main", cwd=checkout).stdout.strip()
        result = _run_script(
            checkout,
            {
                "MERIDIAN_WSL_CHECKOUT": str(checkout),
                "MERIDIAN_WSL_BRANCH": "main",
                "MERIDIAN_WSL_SKIP_INSTALL": "1",
            },
        )
        assert result.returncode == 0, result.stderr
        head = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
        assert head == main_head
