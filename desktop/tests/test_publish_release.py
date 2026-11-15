"""Tests for packaging/publish_release.py: the version check, the release gate, the signing key, the git checks, build
and verify, the release zip, SHA256SUMS and SHA256SUMS.sig, the dry run, the --publish path with its duplicate-tag
check, and the paths that the output shows. Also the secret-name rule that the zip uses (make_release.is_secret_name).

Every subprocess call is faked. The tests that pass run= and gh= use a FakeRunner directly. The tests that use the
real default runners and the real default gate fake only subprocess.run, behind the same FakeRunner. Every file
lives under tmp_path, and %LOCALAPPDATA% points at a temp folder, so the real release seed is never read. The seed
and the public key used here are temporary test values. An autouse guard makes any real subprocess call fail the
test, so no pixi, git, gh or network call can run here.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
PASS_OUTPUT = "bundle ready\nRESULT PASS\n"
REMOTE_LINE = "  origin/dev\n"
RELEASE_URL = "https://github.com/meridianmcp/Meridian/releases/tag/desktop-v0.1.0"
TEST_SEED = bytes(range(1, 33))  # a temporary seed for these tests, never the release seed
TEST_SEED_B64 = base64.b64encode(TEST_SEED).decode("ascii")


def _load():
    name = "publish_release_under_test"
    spec = importlib.util.spec_from_file_location(name, REPO / "packaging" / "publish_release.py")
    module = importlib.util.module_from_spec(spec)
    # Registered first: a dataclass looks its module up in sys.modules while the module runs.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pr = _load()
RunResult = pr.RunResult
ed25519 = pr.make_release_key.ed25519
TEST_PUBLIC = ed25519.public_key(TEST_SEED)
TEST_PUBLIC_B64 = base64.b64encode(TEST_PUBLIC).decode("ascii")


def _load_verify_bundle():
    name = "verify_bundle_parity"
    spec = importlib.util.spec_from_file_location(name, REPO / "packaging" / "verify_bundle.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_real_subprocess(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"real subprocess call in a test: {args[0]!r}")

    monkeypatch.setattr(subprocess, "run", refuse)


@pytest.fixture(autouse=True)
def local_appdata(tmp_path, monkeypatch):
    """Point %LOCALAPPDATA% at a temp folder, so no test can read or write the real release seed."""
    folder = tmp_path / "localappdata"
    folder.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(folder))
    return folder


class FakeRunner:
    """Records each argument list and answers by its leading words.

    A route may hold a list of results, used in order with the last one repeated. Unmatched calls succeed
    silently with no output.
    """

    def __init__(self, routes=None):
        self.routes = [(tuple(prefix), result) for prefix, result in (routes or {}).items()]
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        for prefix, result in self.routes:
            if tuple(args[: len(prefix)]) == prefix:
                if isinstance(result, list):
                    return result.pop(0) if len(result) > 1 else result[0]
                return result
        return RunResult(0, "")

    def subprocess_stand_in(self, args, **kwargs):
        """The same answers, shaped as subprocess.run's return value, for the real default runners."""
        self.kwargs.append(kwargs)
        result = self(args)
        return subprocess.CompletedProcess(list(args), result.returncode, stdout=result.stdout, stderr=result.stderr)


def pass_routes(verify_output: str = PASS_OUTPUT, status: str = "", remote: str = REMOTE_LINE) -> dict:
    return {
        ("pixi", "run", "build"): RunResult(0, "bundle ready"),
        ("pixi", "run", "verify"): RunResult(0, verify_output),
        ("git", "rev-parse", "HEAD"): RunResult(0, SHA + "\n"),
        ("git", "status", "--porcelain"): RunResult(0, status),
        ("git", "branch", "-r", "--contains"): RunResult(0, remote),
    }


def gh_routes(tag_check: RunResult, create: RunResult | None = None) -> dict:
    routes = {("gh", "api"): tag_check}
    if create is not None:
        routes[("gh", "release", "create")] = create
    return routes


def _bundle(dist: Path) -> Path:
    bundle = dist / "MeridianDesktop"
    (bundle / "_internal" / "nested").mkdir(parents=True)
    (bundle / "MeridianDesktop.exe").write_bytes(b"MZ-fake")
    (bundle / "_internal" / "base.dll").write_bytes(b"dll")
    # Secret-named files that must never reach the zip, at the top and nested.
    for rel in (".env", ".env.local", "meridian.toml", "_internal/nested/.env"):
        (bundle / rel).write_text("placeholder", encoding="utf-8")
    return bundle


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pixi.toml").write_text('[workspace]\nname = "meridian-desktop"\nversion = "0.1.0"\n', encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "meridian-desktop"\nversion = "0.1.0"\n', encoding="utf-8")
    scripts = root / "packaging" / "release"
    scripts.mkdir(parents=True)
    (scripts / "install.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (scripts / "uninstall.cmd").write_text("@echo off\r\n", encoding="utf-8")
    init = root / "meridian_desktop" / "__init__.py"
    init.parent.mkdir(parents=True)
    init.write_text('"""Meridian desktop frontend."""\n\n__version__ = "0.1.0"\n', encoding="utf-8")
    (root / "meridian_desktop" / "release_key.py").write_text(
        f'RELEASE_PUBLIC_KEY_B64 = "{TEST_PUBLIC_B64}"\n', encoding="utf-8"
    )
    for name in ("EULA.txt", "OFFER-SOURCE.md"):
        (root / name).write_text("Licence text. Nothing pending.\n", encoding="utf-8")
    _bundle(root / "dist")
    return root


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    root = _make_repo(tmp_path)
    # The real release gate reads EULA.txt and OFFER-SOURCE.md from build.REPO. Point it at this fake repo.
    monkeypatch.setattr(pr.build, "REPO", root)
    resolved = pr.default_paths(root)
    # The test seed goes where the publisher looks: under the temp %LOCALAPPDATA%.
    _write(resolved.signing_seed, TEST_SEED_B64 + "\n")
    return resolved


@pytest.fixture
def subprocess_fake(monkeypatch):
    """Fake only the subprocess boundary. run_command and the default runners reach the returned FakeRunner."""

    def install(routes=None) -> FakeRunner:
        fake = FakeRunner(routes if routes is not None else pass_routes())
        monkeypatch.setattr(subprocess, "run", fake.subprocess_stand_in)
        return fake

    return install


def _run(paths, *, publish=False, run=None, gh=None, gate=None):
    lines: list[str] = []
    outcome = pr.run_release(
        publish=publish,
        paths=paths,
        run=run if run is not None else FakeRunner(pass_routes()),
        gh=gh if gh is not None else FakeRunner(),
        gate=gate if gate is not None else (lambda: []),
        log=lines.append,
    )
    return outcome, lines


def _snapshot(folder: Path) -> dict[str, bytes]:
    return {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob("*") if path.is_file()}


# ---------------------------------------------------------------- version (P1, P8)


def test_version_py_mismatch_refuses_before_any_build(paths):
    _write(paths.version_py, 'APP_VERSION = "0.2.0"\n')
    run = FakeRunner(pass_routes())
    with pytest.raises(pr.PublishError, match="version mismatch"):
        _run(paths, run=run)
    assert run.calls == []


@pytest.mark.parametrize("text", ['APP_VERSION = "0.1.0"\n', 'APP_VERSION: str = "0.1.0"\n'])
def test_matching_version_py_is_accepted(paths, text):
    _write(paths.version_py, text)
    outcome, _ = _run(paths)
    assert outcome.version == "0.1.0"


def test_version_py_without_app_version_refuses(paths):
    _write(paths.version_py, "OTHER = 1\n")
    with pytest.raises(pr.PublishError, match="has no APP_VERSION"):
        _run(paths)


def test_unparsable_version_py_refuses(paths):
    _write(paths.version_py, "APP_VERSION = (\n")
    with pytest.raises(pr.PublishError, match="could not read"):
        _run(paths)


def test_missing_version_py_checks_init_and_creates_nothing(paths):
    outcome, _ = _run(paths)
    assert outcome.version == "0.1.0"
    assert not paths.version_py.exists()


def test_version_py_and_init_must_both_match(paths):
    """P1: __version__ in __init__.py is checked too, not only APP_VERSION."""
    _write(paths.version_py, 'APP_VERSION = "0.1.0"\n')
    _write(paths.init_py, '__version__ = "0.2.0"\n')
    run = FakeRunner(pass_routes())
    with pytest.raises(pr.PublishError, match="version mismatch") as caught:
        _run(paths, run=run)
    assert "meridian_desktop/__init__.py says 0.2.0" in str(caught.value)
    assert run.calls == []


def test_version_py_is_checked_even_when_init_matches(paths):
    _write(paths.version_py, 'APP_VERSION = "0.2.0"\n')
    _write(paths.init_py, '__version__ = "0.1.0"\n')
    run = FakeRunner(pass_routes())
    with pytest.raises(pr.PublishError, match="version mismatch"):
        _run(paths, run=run)
    assert run.calls == []


def test_init_fallback_mismatch_refuses_before_any_build(paths):
    _write(paths.init_py, '__version__ = "0.2.0"\n')
    run = FakeRunner(pass_routes())
    with pytest.raises(pr.PublishError, match="version mismatch") as caught:
        _run(paths, run=run)
    assert "meridian_desktop/__init__.py" in str(caught.value)
    assert run.calls == []


def test_init_without_dunder_version_refuses(paths):
    _write(paths.init_py, "OTHER = 1\n")
    with pytest.raises(pr.PublishError, match="has no __version__"):
        _run(paths)


def test_init_that_imports_app_version_is_accepted(paths):
    """The real __init__.py: __version__ = APP_VERSION, imported from meridian_desktop.version."""
    _write(paths.version_py, 'APP_VERSION = "0.1.0"\n')
    _write(paths.init_py, 'from meridian_desktop.version import APP_VERSION\n\n__version__ = APP_VERSION\n')
    outcome, _ = _run(paths)
    assert outcome.version == "0.1.0"


def test_init_that_imports_app_version_relatively_is_accepted(paths):
    _write(paths.version_py, 'APP_VERSION = "0.1.0"\n')
    _write(paths.init_py, "from .version import APP_VERSION\n\n__version__ = APP_VERSION\n")
    outcome, _ = _run(paths)
    assert outcome.version == "0.1.0"


def test_init_app_version_that_disagrees_refuses(paths):
    _write(paths.version_py, 'APP_VERSION = "0.2.0"\n')
    _write(paths.init_py, "from meridian_desktop.version import APP_VERSION\n\n__version__ = APP_VERSION\n")
    run = FakeRunner(pass_routes())
    with pytest.raises(pr.PublishError, match="version mismatch"):
        _run(paths, run=run)
    assert run.calls == []


def test_init_name_without_an_import_refuses(paths):
    _write(paths.version_py, 'APP_VERSION = "0.1.0"\n')
    _write(paths.init_py, "__version__ = APP_VERSION\n")
    with pytest.raises(pr.PublishError, match="string literal or to APP_VERSION imported"):
        _run(paths)


def test_init_app_version_without_version_py_refuses(paths):
    _write(paths.init_py, "from meridian_desktop.version import APP_VERSION\n\n__version__ = APP_VERSION\n")
    with pytest.raises(pr.PublishError, match="does not exist"):
        _run(paths)


def test_pyproject_mismatch_refuses_before_any_build(paths):
    paths.pyproject_toml.write_text('[project]\nname = "x"\nversion = "0.2.0"\n', encoding="utf-8")
    run = FakeRunner(pass_routes())
    with pytest.raises(pr.PublishError, match="version mismatch") as caught:
        _run(paths, run=run)
    assert "pyproject.toml says 0.2.0" in str(caught.value)
    assert run.calls == []


def test_pyproject_without_a_version_refuses(paths):
    paths.pyproject_toml.write_text('[project]\nname = "x"\ndynamic = ["version"]\n', encoding="utf-8")
    with pytest.raises(pr.PublishError, match="must be a plain number"):
        _run(paths)


def test_unparsable_pyproject_refuses_without_an_absolute_path(paths):
    paths.pyproject_toml.write_text("[project\n", encoding="utf-8")
    with pytest.raises(pr.PublishError, match="could not read the version from pyproject.toml") as caught:
        _run(paths)
    assert str(paths.root) not in str(caught.value)


def test_duplicate_app_version_refuses_even_when_the_first_matches(paths):
    _write(paths.version_py, 'APP_VERSION = "0.1.0"\nAPP_VERSION = "0.2.0"\n')
    run = FakeRunner(pass_routes())
    with pytest.raises(pr.PublishError, match="assigns APP_VERSION 2 times"):
        _run(paths, run=run)
    assert run.calls == []


def test_duplicate_dunder_version_refuses_in_init(paths):
    _write(paths.init_py, '__version__ = "0.1.0"\n__version__ = "0.1.0"\n')
    with pytest.raises(pr.PublishError, match="assigns __version__ 2 times"):
        _run(paths)


def test_non_literal_app_version_refuses(paths):
    _write(paths.version_py, 'APP_VERSION = "0." + "1.0"\n')
    with pytest.raises(pr.PublishError, match="must assign APP_VERSION once at module level"):
        _run(paths)


def test_version_refusal_makes_no_gh_call(paths):
    _write(paths.init_py, '__version__ = "0.9.9"\n')
    gh = FakeRunner()
    with pytest.raises(pr.PublishError, match="version mismatch"):
        _run(paths, gh=gh, publish=True)
    assert gh.calls == []


@pytest.mark.parametrize("version", ["0.1.0-rc1", "0.1.0+build", "v0.1.0", "0.1.0 beta", ""])
def test_a_pixi_version_that_is_not_plain_dotted_integers_refuses(paths, version):
    paths.pixi_toml.write_text(f'[workspace]\nname = "x"\nversion = "{version}"\n', encoding="utf-8")
    run = FakeRunner(pass_routes())
    with pytest.raises(pr.PublishError, match="plain number"):
        _run(paths, run=run)
    assert run.calls == []


# ---------------------------------------------------------------- gate, build, verify


def test_gate_problem_stops_before_build(paths):
    run = FakeRunner(pass_routes())
    with pytest.raises(pr.PublishError, match="release gate"):
        _run(paths, run=run, gate=lambda: ["EULA.txt carries the legal-review marker 'DRAFT - NOT FOR RELEASE'"])
    assert run.calls == []


def test_build_failure_stops_before_verify(paths):
    routes = pass_routes()
    routes[("pixi", "run", "build")] = RunResult(2, "PyInstaller failed")
    run = FakeRunner(routes)
    with pytest.raises(pr.PublishError, match="pixi run build failed"):
        _run(paths, run=run)
    assert ["pixi", "run", "verify"] not in run.calls
    assert not (paths.out_dir / "MeridianDesktop-0.1.0.zip").exists()


@pytest.mark.parametrize(
    "result",
    [
        RunResult(1, "RESULT FAIL (2 problems)"),
        RunResult(0, "all checks ran, no pass line"),
        RunResult(0, "NOT RESULT PASS"),
    ],
)
def test_verify_without_result_pass_stops_the_release(paths, result):
    routes = pass_routes()
    routes[("pixi", "run", "verify")] = result
    gh = FakeRunner()
    with pytest.raises(pr.PublishError, match="verify"):
        _run(paths, run=FakeRunner(routes), gh=gh, publish=True)
    assert gh.calls == []
    assert not (paths.out_dir / "MeridianDesktop-0.1.0.zip").exists()
    assert not (paths.out_dir / "SHA256SUMS").exists()
    assert not (paths.out_dir / "SHA256SUMS.sig").exists()


def test_head_commit_must_be_a_full_sha(paths):
    routes = pass_routes()
    routes[("git", "rev-parse", "HEAD")] = RunResult(0, "abc123\n")
    with pytest.raises(pr.PublishError, match="HEAD commit"):
        _run(paths, run=FakeRunner(routes))
    assert not (paths.out_dir / "MeridianDesktop-0.1.0.zip").exists()


# ---------------------------------------------------------------- git state (F1, P3)


def test_dry_run_reports_git_state_and_what_would_block_publish(paths):
    routes = pass_routes(status=" M packaging/build.py\n", remote="")
    gh = FakeRunner()
    outcome, lines = _run(paths, run=FakeRunner(routes), gh=gh, publish=False)
    assert outcome.published is False
    assert gh.calls == []
    text = "\n".join(lines)
    assert "warning: working tree is not clean" in text
    assert "warning: HEAD is not on any remote branch" in text
    assert "--publish would refuse:" in text
    assert "the working tree is not clean" in text


def test_dry_run_says_publish_would_pass_when_clean_and_pushed(paths):
    _, lines = _run(paths)
    text = "\n".join(lines)
    assert "git: working tree is clean" in text
    assert "git: HEAD is on a remote branch" in text
    assert "--publish would pass the git checks" in text


def test_publish_refuses_a_dirty_tree_before_anything_is_built(paths):
    run = FakeRunner(pass_routes(status=" M packaging/build.py\n"))
    gh = FakeRunner()
    with pytest.raises(pr.PublishError, match="the working tree is not clean") as caught:
        _run(paths, run=run, gh=gh, publish=True)
    assert ["pixi", "run", "build"] not in run.calls
    assert gh.calls == []
    assert "Nothing was built or published" in str(caught.value)


def test_publish_refuses_a_head_that_is_not_on_a_remote_branch(paths):
    run = FakeRunner(pass_routes(remote=""))
    gh = FakeRunner()
    with pytest.raises(pr.PublishError, match="is not on any remote branch"):
        _run(paths, run=run, gh=gh, publish=True)
    assert ["pixi", "run", "build"] not in run.calls
    assert gh.calls == []


def test_failed_git_status_counts_as_not_clean(paths):
    run = FakeRunner(pass_routes())
    run.routes.insert(0, (("git", "status", "--porcelain"), RunResult(128, "", "fatal: not a git repository")))
    with pytest.raises(pr.PublishError, match="not clean"):
        _run(paths, run=run, publish=True)
    assert ["pixi", "run", "build"] not in run.calls


def test_failed_remote_check_counts_as_not_pushed(paths):
    run = FakeRunner(pass_routes())
    run.routes.insert(0, (("git", "branch", "-r", "--contains"), RunResult(128, "", "fatal")))
    with pytest.raises(pr.PublishError, match="is not on any remote branch"):
        _run(paths, run=run, publish=True)


def test_git_warnings_on_stderr_do_not_make_a_clean_tree_dirty(paths):
    """P3: status is read from stdout. A warning on stderr is not a change in the tree."""
    routes = pass_routes()
    routes[("git", "status", "--porcelain")] = RunResult(0, "", "warning: LF will be replaced by CRLF in a file\n")
    _, lines = _run(paths, run=FakeRunner(routes))
    assert "git: working tree is clean" in "\n".join(lines)


def test_head_is_read_from_stdout_only(paths):
    """P3: a SHA that appears only on stderr is not the HEAD commit."""
    routes = pass_routes()
    routes[("git", "rev-parse", "HEAD")] = RunResult(0, "", SHA + "\n")
    with pytest.raises(pr.PublishError, match="HEAD commit"):
        _run(paths, run=FakeRunner(routes))


@pytest.mark.parametrize("publish", [True, False])
def test_head_moved_during_the_build_refuses(paths, publish):
    routes = pass_routes()
    routes[("git", "rev-parse", "HEAD")] = [RunResult(0, SHA + "\n"), RunResult(0, OTHER_SHA + "\n")]
    gh = FakeRunner()
    with pytest.raises(pr.PublishError, match="HEAD moved during the build"):
        _run(paths, run=FakeRunner(routes), gh=gh, publish=publish)
    assert gh.calls == []
    assert not (paths.out_dir / "MeridianDesktop-0.1.0.zip").exists()


# ---------------------------------------------------------------- signing key (signing)


def test_a_missing_seed_refuses_before_any_build(paths):
    paths.signing_seed.unlink()
    run = FakeRunner(pass_routes())
    gh = FakeRunner()
    with pytest.raises(pr.PublishError, match="signing key is missing") as caught:
        _run(paths, run=run, gh=gh, publish=True)
    assert "Nothing was built or published" in str(caught.value)
    assert run.calls == []
    assert gh.calls == []


def test_a_missing_seed_also_refuses_the_dry_run(paths):
    paths.signing_seed.unlink()
    run = FakeRunner(pass_routes())
    with pytest.raises(pr.PublishError, match="signing key is missing"):
        _run(paths, run=run, publish=False)
    assert run.calls == []
    assert not paths.out_dir.exists() or not any(paths.out_dir.iterdir())


def test_a_seed_that_is_not_base64_is_refused_without_printing_it(paths):
    paths.signing_seed.write_text("NOT-A-KEY-VALUE!!\n", encoding="ascii")
    run = FakeRunner(pass_routes())
    with pytest.raises(pr.PublishError, match="not base64 of 32 bytes") as caught:
        _run(paths, run=run)
    assert "NOT-A-KEY-VALUE" not in str(caught.value)
    assert run.calls == []


def test_a_seed_for_another_key_refuses_before_any_build(paths):
    other_seed = bytes(range(33, 65))
    paths.signing_seed.write_text(base64.b64encode(other_seed).decode("ascii") + "\n", encoding="ascii")
    run = FakeRunner(pass_routes())
    gh = FakeRunner()
    with pytest.raises(pr.PublishError, match="does not match RELEASE_PUBLIC_KEY_B64") as caught:
        _run(paths, run=run, gh=gh, publish=True)
    assert "Nothing was built or published" in str(caught.value)
    assert run.calls == []
    assert gh.calls == []


def test_an_empty_release_key_refuses_and_says_how_to_fix_it(paths):
    _write(paths.release_key_py, 'RELEASE_PUBLIC_KEY_B64 = ""\n')
    run = FakeRunner(pass_routes())
    with pytest.raises(pr.PublishError, match="no valid RELEASE_PUBLIC_KEY_B64") as caught:
        _run(paths, run=run)
    assert "make_release_key.py" in str(caught.value)
    assert run.calls == []


def test_a_release_key_that_is_not_base64_refuses(paths):
    _write(paths.release_key_py, 'RELEASE_PUBLIC_KEY_B64 = "not-a-key!!"\n')
    with pytest.raises(pr.PublishError, match="no valid RELEASE_PUBLIC_KEY_B64"):
        _run(paths)


def test_a_missing_release_key_file_refuses(paths):
    paths.release_key_py.unlink()
    with pytest.raises(pr.PublishError, match="could not read meridian_desktop/release_key.py") as caught:
        _run(paths)
    assert str(paths.root) not in str(caught.value)


def test_a_release_key_with_two_assignments_refuses(paths):
    _write(paths.release_key_py, f'RELEASE_PUBLIC_KEY_B64 = "{TEST_PUBLIC_B64}"\nRELEASE_PUBLIC_KEY_B64 = "x"\n')
    with pytest.raises(pr.PublishError, match="assigns RELEASE_PUBLIC_KEY_B64 2 times"):
        _run(paths)


def test_the_signature_must_verify_or_no_sig_is_written(paths, monkeypatch):
    monkeypatch.setattr(pr.make_release_key.ed25519, "sign", lambda seed, data: bytes(64))
    gh = FakeRunner()
    with pytest.raises(pr.PublishError, match="does not verify against RELEASE_PUBLIC_KEY_B64"):
        _run(paths, gh=gh, publish=True)
    assert not (paths.out_dir / "SHA256SUMS.sig").exists()
    assert gh.calls == []


# ---------------------------------------------------------------- signing (SHA256SUMS.sig)


def test_the_signature_verifies_over_the_exact_bytes_of_sha256sums(paths):
    outcome, lines = _run(paths)
    sums = outcome.sums_path.read_bytes()
    assert outcome.sig_path == paths.out_dir / "SHA256SUMS.sig"
    text = outcome.sig_path.read_text(encoding="ascii")
    assert text.endswith("\n")
    signature = base64.b64decode(text.strip(), validate=True)
    assert len(signature) == 64
    assert ed25519.verify(TEST_PUBLIC, sums, signature) is True
    assert "signed SHA256SUMS; wrote dist/release/SHA256SUMS.sig" in lines


def test_a_tampered_sha256sums_fails_verification(paths):
    outcome, _ = _run(paths)
    signature = base64.b64decode(outcome.sig_path.read_text(encoding="ascii").strip(), validate=True)
    tampered = outcome.sums_path.read_bytes().replace(b"  MeridianDesktop", b"  MeridianDesktopX")
    assert tampered != outcome.sums_path.read_bytes()
    assert ed25519.verify(TEST_PUBLIC, tampered, signature) is False
    assert ed25519.verify(TEST_PUBLIC, outcome.sums_path.read_bytes(), signature) is True


def test_the_updater_accepts_the_signature_file_the_publisher_writes(paths):
    """The updater reads SHA256SUMS.sig with its own check: one line of base64 and one trailing newline."""
    from meridian_desktop import updater

    outcome, _ = _run(paths)
    sums = outcome.sums_path.read_bytes()
    sig_bytes = outcome.sig_path.read_bytes()
    assert sig_bytes.endswith(b"\n") and sig_bytes.count(b"\n") == 1
    updater.verify_sums_signature(sums, sig_bytes, TEST_PUBLIC)  # raises Refused when it does not verify
    with pytest.raises(updater.Refused, match="not signed by the Meridian release key"):
        updater.verify_sums_signature(sums + b" ", sig_bytes, TEST_PUBLIC)


def test_a_stale_signature_from_an_earlier_run_is_replaced(paths):
    _write(paths.out_dir / "SHA256SUMS.sig", base64.b64encode(bytes(64)).decode("ascii") + "\n")
    outcome, _ = _run(paths)
    signature = base64.b64decode(outcome.sig_path.read_text(encoding="ascii").strip(), validate=True)
    assert ed25519.verify(TEST_PUBLIC, outcome.sums_path.read_bytes(), signature) is True


def test_dry_run_writes_nothing_outside_dist_release(paths):
    """The dry run signs, but writes only into dist/release and never reads a seed into a written file."""
    before = _snapshot(paths.root)
    seed_before = paths.signing_seed.read_bytes()
    _run(paths, publish=False)
    after = _snapshot(paths.root)
    changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
    assert changed, "the dry run should write the release files"
    assert all(name.startswith("dist/release/") for name in changed), sorted(changed)
    assert paths.signing_seed.read_bytes() == seed_before


# ---------------------------------------------------------------- zip and SHA256SUMS


def test_zip_holds_bundle_and_scripts_and_no_secret_files(paths):
    outcome, lines = _run(paths)
    with zipfile.ZipFile(outcome.zip_path) as archive:
        names = set(archive.namelist())
    assert {"MeridianDesktop.exe", "_internal/base.dll", "install.cmd", "uninstall.cmd"} <= names
    assert not any(name.split("/")[-1] in {".env", ".env.local", "meridian.toml"} for name in names)
    assert "_internal/nested/.env" not in names
    assert outcome.zip_path.name == "MeridianDesktop-0.1.0.zip"
    assert "left out (secret-named): meridian.toml" in lines


def test_sha256sums_matches_the_zip(paths):
    outcome, _ = _run(paths)
    text = outcome.sums_path.read_bytes().decode("utf-8")
    assert "\r" not in text and text.endswith("\n")
    lines = text.splitlines()
    assert len(lines) == 1
    digest, name = lines[0].split("  ", 1)
    assert name == outcome.zip_path.name
    assert digest == hashlib.sha256(outcome.zip_path.read_bytes()).hexdigest()


def test_release_notes_hold_only_the_version_zip_name_and_digest(paths):
    outcome, _ = _run(paths)
    notes = (paths.out_dir / "RELEASE_NOTES-0.1.0.txt").read_text(encoding="utf-8")
    digest = hashlib.sha256(outcome.zip_path.read_bytes()).hexdigest()
    assert notes == f"Meridian Desktop 0.1.0\n\nSHA-256 of MeridianDesktop-0.1.0.zip: {digest}\n"


# ---------------------------------------------------------------- dry run and publish


def test_dry_run_calls_no_gh_command(paths):
    gh = FakeRunner()
    outcome, lines = _run(paths, gh=gh, publish=False)
    assert gh.calls == []
    assert outcome.published is False
    assert outcome.release_output == ""
    assert outcome.gh_command[:4] == ("gh", "release", "create", "desktop-v0.1.0")
    assert any("gh release create desktop-v0.1.0" in line for line in lines)
    assert any("dist/release/SHA256SUMS.sig" in line for line in lines)


def test_publish_checks_the_tag_before_creating_the_release(paths):
    gh = FakeRunner(gh_routes(RunResult(1, "gh: Not Found (HTTP 404)"), RunResult(0, RELEASE_URL + "\n")))
    outcome, _ = _run(paths, gh=gh, publish=True)

    assert len(gh.calls) == 2
    assert gh.calls[0][:2] == ["gh", "api"]
    assert gh.calls[0][2] == "repos/meridianmcp/Meridian/git/ref/tags/desktop-v0.1.0"
    create = gh.calls[1]
    assert create[:4] == ["gh", "release", "create", "desktop-v0.1.0"]
    assert "dist/release/MeridianDesktop-0.1.0.zip" in create
    assert "dist/release/SHA256SUMS" in create
    assert create[create.index("--repo") + 1] == "meridianmcp/Meridian"
    assert create[create.index("--target") + 1] == SHA
    assert create[create.index("--title") + 1] == "Meridian Desktop 0.1.0"
    assert create[create.index("--notes-file") + 1] == "dist/release/RELEASE_NOTES-0.1.0.txt"
    assert outcome.published is True
    assert outcome.release_output == RELEASE_URL


def test_publish_uploads_the_zip_sums_and_sig_together(paths):
    """Signing: the three assets go up in one gh release create call, in this order."""
    gh = FakeRunner(gh_routes(RunResult(1, "gh: Not Found (HTTP 404)"), RunResult(0, RELEASE_URL + "\n")))
    _run(paths, gh=gh, publish=True)
    create = gh.calls[1]
    assert create[4:7] == [
        "dist/release/MeridianDesktop-0.1.0.zip",
        "dist/release/SHA256SUMS",
        "dist/release/SHA256SUMS.sig",
    ]


def test_publish_refuses_an_existing_tag(paths):
    gh = FakeRunner(gh_routes(RunResult(0, '{"ref": "refs/tags/desktop-v0.1.0"}')))
    with pytest.raises(pr.PublishError, match="already exists"):
        _run(paths, gh=gh, publish=True)
    assert len(gh.calls) == 1
    assert gh.calls[0][:2] == ["gh", "api"]


def test_a_prefix_match_does_not_block_the_release(paths):
    """F3: a v0.1.0-rc1 response must not block v0.1.0."""
    gh = FakeRunner(gh_routes(RunResult(0, '{"ref": "refs/tags/desktop-v0.1.0-rc1"}'), RunResult(0, RELEASE_URL + "\n")))
    outcome, _ = _run(paths, gh=gh, publish=True)
    assert outcome.published is True
    assert gh.calls[1][:4] == ["gh", "release", "create", "desktop-v0.1.0"]


def test_a_list_of_only_other_matching_refs_does_not_block(paths):
    answer = '[{"ref": "refs/tags/desktop-v0.1.0-rc1"}, {"ref": "refs/tags/desktop-v0.1.0.1"}]'
    gh = FakeRunner(gh_routes(RunResult(0, answer), RunResult(0, RELEASE_URL + "\n")))
    outcome, _ = _run(paths, gh=gh, publish=True)
    assert outcome.published is True


def test_an_empty_list_of_matching_refs_means_the_tag_is_absent(paths):
    gh = FakeRunner(gh_routes(RunResult(0, "[]"), RunResult(0, RELEASE_URL + "\n")))
    outcome, _ = _run(paths, gh=gh, publish=True)
    assert outcome.published is True


def test_an_exact_ref_in_a_list_still_blocks(paths):
    answer = '[{"ref": "refs/tags/desktop-v0.1.0-rc1"}, {"ref": "refs/tags/desktop-v0.1.0"}]'
    gh = FakeRunner(gh_routes(RunResult(0, answer)))
    with pytest.raises(pr.PublishError, match="already exists"):
        _run(paths, gh=gh, publish=True)
    assert len(gh.calls) == 1


@pytest.mark.parametrize("output", ["gh: HTTP 500 Internal Server Error", "gh: authentication required"])
def test_publish_refuses_when_the_tag_check_is_inconclusive(paths, output):
    gh = FakeRunner(gh_routes(RunResult(1, output)))
    with pytest.raises(pr.PublishError, match="could not check"):
        _run(paths, gh=gh, publish=True)
    assert len(gh.calls) == 1


@pytest.mark.parametrize("output", ["<html>not json</html>", "", "42"])
def test_an_unreadable_answer_with_exit_zero_is_inconclusive(paths, output):
    gh = FakeRunner(gh_routes(RunResult(0, output)))
    with pytest.raises(pr.PublishError, match="could not check"):
        _run(paths, gh=gh, publish=True)
    assert len(gh.calls) == 1


@pytest.mark.parametrize(
    "answer",
    ['{"sha": "0123456789abcdef"}', '[{"ref": "refs/tags/desktop-v0.1.0-rc1"}, {"sha": "x"}]', "[1, 2]", '{"ref": 7}'],
)
def test_an_answer_that_does_not_name_its_ref_is_inconclusive(paths, answer):
    """P2: an object with no ref field must not read as 'tag absent'."""
    gh = FakeRunner(gh_routes(RunResult(0, answer)))
    with pytest.raises(pr.PublishError, match="could not check"):
        _run(paths, gh=gh, publish=True)
    assert len(gh.calls) == 1


def test_a_ref_on_stderr_only_is_not_read_as_the_answer(paths):
    """P3: the tag answer is parsed from stdout. A JSON answer that appears only on stderr is inconclusive."""
    gh = FakeRunner(gh_routes(RunResult(0, "", '{"ref": "refs/tags/desktop-v0.1.0"}')))
    with pytest.raises(pr.PublishError, match="could not check"):
        _run(paths, gh=gh, publish=True)
    assert len(gh.calls) == 1


def test_publish_stops_when_gh_release_create_fails(paths):
    gh = FakeRunner(gh_routes(RunResult(1, "gh: Not Found (HTTP 404)"), RunResult(1, "gh: validation failed")))
    with pytest.raises(pr.PublishError, match="gh release create failed"):
        _run(paths, gh=gh, publish=True)


def test_failed_release_create_prints_tag_recovery_steps(paths):
    """F6 and P6: after a failed create, say that a tag may exist, how to confirm it and how to remove it safely."""
    gh = FakeRunner(gh_routes(RunResult(1, "gh: Not Found (HTTP 404)"), RunResult(1, "gh: validation failed")))
    with pytest.raises(pr.PublishError) as caught:
        _run(paths, gh=gh, publish=True)
    message = str(caught.value)
    assert "A tag may now exist on meridianmcp/Meridian for desktop-v0.1.0" in message
    assert "gh api repos/meridianmcp/Meridian/git/ref/tags/desktop-v0.1.0" in message
    assert "gh release delete desktop-v0.1.0 --cleanup-tag" in message
    assert "points there may be this run's" in message
    assert SHA[:12] in message
    assert "gh release view desktop-v0.1.0" in message
    assert "then run again" in message


def test_the_recovery_text_does_not_offer_to_delete_a_tag_at_another_commit(paths):
    gh = FakeRunner(gh_routes(RunResult(1, "gh: Not Found (HTTP 404)"), RunResult(1, "gh: validation failed")))
    with pytest.raises(pr.PublishError) as caught:
        _run(paths, gh=gh, publish=True)
    assert "If it points anywhere else, stop" in str(caught.value)


# ---------------------------------------------------------------- output paths (F4, P4)


def test_run_output_has_no_absolute_path_prefix(paths):
    _, lines = _run(paths)
    text = "\n".join(lines)
    assert str(paths.root) not in text
    assert paths.root.as_posix() not in text
    assert "wrote dist/release/MeridianDesktop-0.1.0.zip" in text
    assert "wrote dist/release/SHA256SUMS" in text


def test_publish_output_has_no_absolute_path_prefix(paths):
    gh = FakeRunner(gh_routes(RunResult(1, "gh: Not Found (HTTP 404)"), RunResult(0, RELEASE_URL + "\n")))
    _, lines = _run(paths, gh=gh, publish=True)
    text = "\n".join(lines) + "\n" + "\n".join(" ".join(call) for call in gh.calls)
    assert str(paths.root) not in text
    assert paths.root.as_posix() not in text


def test_refusal_messages_have_no_absolute_path_prefix(paths):
    (paths.bundle / "MeridianDesktop.exe").unlink()
    with pytest.raises(pr.PublishError) as caught:
        _run(paths)
    message = str(caught.value)
    assert str(paths.root) not in message
    assert paths.root.as_posix() not in message
    assert "has no MeridianDesktop.exe" in message


def test_version_refusal_shows_the_path_relative_to_the_root(paths):
    _write(paths.version_py, 'APP_VERSION = "0.2.0"\n')
    with pytest.raises(pr.PublishError) as caught:
        _run(paths)
    message = str(caught.value)
    assert "meridian_desktop/version.py" in message
    assert str(paths.root) not in message


def test_an_unexpected_error_is_refused_without_a_traceback_or_absolute_path(paths):
    """P4: an unexpected exception goes through the relative-path display, not a traceback."""

    def boom():
        raise OSError(f"cannot read {paths.root / 'EULA.txt'}")

    with pytest.raises(pr.PublishError) as caught:
        _run(paths, gate=boom)
    message = str(caught.value)
    assert message.startswith("unexpected OSError")
    assert "EULA.txt" in message
    assert str(paths.root) not in message
    assert caught.value.__cause__ is None


def test_main_prints_no_traceback_for_an_unexpected_error(monkeypatch, capsys):
    def explode(**kwargs):
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(pr, "run_release", explode)
    assert pr.main([]) == 1
    err = capsys.readouterr().err
    assert "release stopped: unexpected RuntimeError" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------- real defaults (F7, P5)


def test_default_paths_point_into_the_root(paths):
    root = paths.root
    assert paths.pixi_toml == root / "pixi.toml"
    assert paths.pyproject_toml == root / "pyproject.toml"
    assert paths.version_py == root / "meridian_desktop" / "version.py"
    assert paths.init_py == root / "meridian_desktop" / "__init__.py"
    assert paths.release_key_py == root / "meridian_desktop" / "release_key.py"
    assert paths.bundle == root / "dist" / "MeridianDesktop"
    assert paths.scripts == root / "packaging" / "release"
    assert paths.out_dir == root / "dist" / "release"


def test_default_seed_is_under_localappdata_not_the_repository(paths, local_appdata):
    assert paths.signing_seed == local_appdata / "meridian-release-keys" / "release-ed25519.key"
    assert paths.root not in paths.signing_seed.parents


def test_default_paths_default_root_is_the_repository():
    assert pr.default_paths().root == pr.REPO


def test_default_gate_refuses_the_legal_review_marker(paths, subprocess_fake):
    fake = subprocess_fake()
    _write(paths.root / "EULA.txt", f"Licence text {pr.build.RELEASE_MARKER}\n")
    with pytest.raises(pr.PublishError, match="legal-review marker"):
        pr.run_release(publish=True, paths=paths, log=[].append)
    assert fake.calls == []


def test_default_gate_refuses_tbd_text(paths, subprocess_fake):
    fake = subprocess_fake()
    _write(paths.root / "OFFER-SOURCE.md", "Source offer. TBD\n")
    with pytest.raises(pr.PublishError, match="TBD text"):
        pr.run_release(publish=False, paths=paths, log=[].append)
    assert fake.calls == []


def test_default_dry_run_uses_the_real_runners_and_no_shell(paths, subprocess_fake):
    fake = subprocess_fake()
    lines: list[str] = []
    outcome = pr.run_release(publish=False, paths=paths, log=lines.append)
    assert outcome.published is False
    assert ["pixi", "run", "build"] in fake.calls
    assert ["pixi", "run", "verify"] in fake.calls
    assert not any(call[0] == "gh" for call in fake.calls)
    assert fake.kwargs and all(kw.get("cwd") == paths.root for kw in fake.kwargs)
    assert not any(kw.get("shell") for kw in fake.kwargs)
    assert "--publish would pass the git checks" in "\n".join(lines)


def test_default_publish_runs_gh_in_the_injected_root(paths, subprocess_fake):
    """P5: git and gh run in the root they were given, not in the directory the script lives in."""
    routes = pass_routes()
    routes.update(gh_routes(RunResult(1, "gh: Not Found (HTTP 404)"), RunResult(0, RELEASE_URL + "\n")))
    fake = subprocess_fake(routes)
    pr.run_release(publish=True, paths=paths, log=[].append)
    gh_kwargs = [kw for call, kw in zip(fake.calls, fake.kwargs) if call[0] == "gh"]
    assert gh_kwargs and all(kw.get("cwd") == paths.root for kw in gh_kwargs)


def test_default_publish_refuses_a_dirty_tree(paths, subprocess_fake):
    fake = subprocess_fake(pass_routes(status=" M packaging/build.py\n"))
    with pytest.raises(pr.PublishError, match="not clean"):
        pr.run_release(publish=True, paths=paths, log=[].append)
    assert ["pixi", "run", "build"] not in fake.calls
    assert not any(call[0] == "gh" for call in fake.calls)


def test_default_publish_refuses_an_unpushed_head(paths, subprocess_fake):
    fake = subprocess_fake(pass_routes(remote=""))
    with pytest.raises(pr.PublishError, match="is not on any remote branch"):
        pr.run_release(publish=True, paths=paths, log=[].append)
    assert ["pixi", "run", "build"] not in fake.calls


def test_default_publish_refuses_a_version_py_that_disagrees(paths, subprocess_fake):
    fake = subprocess_fake()
    _write(paths.version_py, 'APP_VERSION = "0.2.0"\n')
    with pytest.raises(pr.PublishError, match="version mismatch"):
        pr.run_release(publish=True, paths=paths, log=[].append)
    assert fake.calls == []


def test_default_publish_refuses_an_init_fallback_that_disagrees(paths, subprocess_fake):
    fake = subprocess_fake()
    _write(paths.init_py, '__version__ = "0.2.0"\n')
    with pytest.raises(pr.PublishError, match="version mismatch"):
        pr.run_release(publish=True, paths=paths, log=[].append)
    assert fake.calls == []


def test_default_publish_is_not_blocked_by_a_prefix_tag(paths, subprocess_fake):
    routes = pass_routes()
    routes.update(gh_routes(RunResult(0, '{"ref": "refs/tags/desktop-v0.1.0-rc1"}'), RunResult(0, RELEASE_URL + "\n")))
    fake = subprocess_fake(routes)
    outcome = pr.run_release(publish=True, paths=paths, log=[].append)
    assert outcome.published is True
    create = next(call for call in fake.calls if call[:3] == ["gh", "release", "create"])
    assert create[create.index("--target") + 1] == SHA
    assert "dist/release/MeridianDesktop-0.1.0.zip" in create
    assert "dist/release/SHA256SUMS.sig" in create


def test_default_publish_refuses_an_existing_exact_tag(paths, subprocess_fake):
    routes = pass_routes()
    routes.update(gh_routes(RunResult(0, '{"ref": "refs/tags/desktop-v0.1.0"}')))
    fake = subprocess_fake(routes)
    with pytest.raises(pr.PublishError, match="already exists"):
        pr.run_release(publish=True, paths=paths, log=[].append)
    assert not any(call[:3] == ["gh", "release", "create"] for call in fake.calls)


def test_default_signing_uses_the_real_ed25519_module(paths, subprocess_fake):
    subprocess_fake()
    outcome = pr.run_release(publish=False, paths=paths, log=[].append)
    signature = base64.b64decode(outcome.sig_path.read_text(encoding="ascii").strip(), validate=True)
    assert ed25519.verify(TEST_PUBLIC, outcome.sums_path.read_bytes(), signature) is True


# ---------------------------------------------------------------- secret names (F5)


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".ENV",
        ".Env.Local",
        ".env.PRODUCTION",
        "prod.ENV",
        "Prod.Env",
        "Meridian.TOML",
        "meridian.toml",
        "Server.PEM",
        "server.pem",
        "Private.KEY",
        "id.Key",
        ".NETRC",
        ".NetRC",
    ],
)
def test_is_secret_name_ignores_letter_case(name):
    assert pr.make_release.is_secret_name(name)


@pytest.mark.parametrize(
    "name",
    ["README.md", "meridian.toml.bak", "keyboard.txt", "pem.txt", "environment.txt", "MeridianDesktop.exe", "netrc"],
)
def test_is_secret_name_leaves_other_names_alone(name):
    assert not pr.make_release.is_secret_name(name)


def test_is_secret_name_covers_the_same_names_as_verify_bundle():
    verify = _load_verify_bundle()
    names = [
        ".env",
        ".ENV",
        ".env.local",
        ".ENV.Production",
        ".env_temp",
        ".envrc",
        "prod.env",
        "PROD.ENV",
        "meridian.toml",
        "MERIDIAN.TOML",
        "meridian.toml.bak",
        "server.pem",
        "Server.PEM",
        "private.key",
        "PRIVATE.KEY",
        ".netrc",
        ".NETRC",
        "netrc",
        "keys.txt",
        "keyboard.key.txt",
        "environment.txt",
        "README.md",
        "MeridianDesktop.exe",
    ]
    for name in names:
        assert pr.make_release.is_secret_name(name) == verify._secret_named(name), name


def test_zip_leaves_out_mixed_case_secret_names(tmp_path):
    bundle = tmp_path / "dist" / "MeridianDesktop"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / "MeridianDesktop.exe").write_bytes(b"MZ-fake")
    (bundle / "_internal" / "base.dll").write_bytes(b"dll")
    secrets = [".ENV", "Meridian.TOML", "Server.PEM", "Private.Key", ".NetRC", "_internal/.Env.Local"]
    for rel in secrets:
        (bundle / rel).write_text("placeholder", encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "install.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (scripts / "uninstall.cmd").write_text("@echo off\r\n", encoding="utf-8")

    result = pr.make_release.build_release(bundle, scripts, tmp_path / "out", "0.1.0")

    with zipfile.ZipFile(result.path) as archive:
        names = set(archive.namelist())
    assert names == {"MeridianDesktop.exe", "_internal/base.dll", "install.cmd", "uninstall.cmd"}
    assert result.excluded == tuple(sorted(secrets))


# ---------------------------------------------------------------- runner and CLI (P5)


def test_run_command_passes_an_argument_list_and_no_shell(monkeypatch, tmp_path):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, stdout="RESULT PASS\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = pr.run_command(["pixi", "run", "verify"], cwd=tmp_path)
    assert seen["args"] == ["pixi", "run", "verify"]
    assert not seen["kwargs"].get("shell", False)
    assert seen["kwargs"]["cwd"] == tmp_path
    assert result == RunResult(0, "RESULT PASS\n")


def test_run_command_keeps_stdout_and_stderr_apart(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="out\n", stderr="err\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = pr.run_command(["git", "status"])
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    assert result.output == "out\nerr"


def test_run_command_reports_a_missing_program_without_raising(monkeypatch):
    def missing(args, **kwargs):
        raise FileNotFoundError(2, "no such file")

    monkeypatch.setattr(subprocess, "run", missing)
    assert pr.run_command(["gh", "api", "x"]).returncode == 127


def test_parser_defaults_to_dry_run():
    assert pr.build_parser().parse_args([]).publish is False
    assert pr.build_parser().parse_args(["--publish"]).publish is True


def test_main_passes_the_flag_and_returns_1_on_refusal(monkeypatch, capsys):
    seen = {}

    def refuse(**kwargs):
        seen.update(kwargs)
        raise pr.PublishError("tag desktop-v0.1.0 already exists on meridianmcp/Meridian; refusing to publish.")

    monkeypatch.setattr(pr, "run_release", refuse)
    assert pr.main(["--publish"]) == 1
    assert seen["publish"] is True
    assert "already exists" in capsys.readouterr().err


def test_the_release_command_is_a_prerelease_with_a_desktop_tag(tmp_path):
    from types import SimpleNamespace

    packaged = SimpleNamespace(
        zip_path=tmp_path / "MeridianDesktop-0.2.1.zip",
        sums_path=tmp_path / "SHA256SUMS",
        sig_path=tmp_path / "SHA256SUMS.sig",
        notes_path=tmp_path / "RELEASE_NOTES-0.2.1.txt",
        root=tmp_path,
    )
    command = pr.release_command("0.2.1", packaged, "0123abc")
    assert command[:4] == ["gh", "release", "create", "desktop-v0.2.1"]
    # Without --prerelease the repo's "latest" slot would move to the desktop and break core's installers.
    assert "--prerelease" in command


def test_the_publisher_and_the_updater_agree_on_repo_and_tag_prefix():
    # The publisher writes the release and the updater reads it. A mismatch would publish releases nobody can find.
    from meridian_desktop import updater

    assert pr.GITHUB_REPO == updater.REPO
    assert pr.TAG_PREFIX == updater.DESKTOP_TAG_PREFIX
