#!/usr/bin/env python3
"""Build, verify, sign and package a Meridian Desktop release. Publish it to GitHub only with --publish.

    python packaging/publish_release.py            dry run: builds, verifies and signs locally, prints the plan
                                                   and the exact gh command, and calls no gh command
    python packaging/publish_release.py --publish  the same steps, then creates the GitHub prerelease desktop-v<version>

Steps, in order. A failed step stops the run before anything reaches GitHub:
1. Version. The [workspace] version in pixi.toml is the release version: plain dotted integers, as the updater
   accepts. pyproject.toml [project] version, APP_VERSION in meridian_desktop/version.py (when that file exists)
   and __version__ in meridian_desktop/__init__.py must all equal it. Refused on any disagreement. A file that
   assigns one of these constants more than once is refused. This script never creates any of these files.
2. Release gate. The same checks as build.py release_problems: no legal-review marker and no TBD text in
   EULA.txt or OFFER-SOURCE.md. Read-only.
3. Signing key. The seed in %LOCALAPPDATA%\\meridian-release-keys\\release-ed25519.key must exist and be 32 bytes.
   The public key it gives must equal RELEASE_PUBLIC_KEY_B64 in meridian_desktop/release_key.py. The seed is read
   here, never printed, and refused when missing.
4. Git state. HEAD is read and remembered. The working tree must be clean (git status --porcelain prints
   nothing) and HEAD must be contained in a remote-tracking branch (git branch -r --contains HEAD). --publish
   refuses otherwise, before anything is built. The dry run prints the same status and what would block it.
5. Build: pixi run build.
6. Verify: pixi run verify. Its output must hold a line RESULT PASS and it must exit 0. HEAD must still be the
   commit remembered in step 4, or the run stops.
7. Package: dist/release/MeridianDesktop-<version>.zip (make_release.build_release, which leaves out .env,
   .env.*, *.env, meridian.toml, *.pem, *.key and .netrc), dist/release/SHA256SUMS with one
   "<sha256>  <zip name>" line, and dist/release/RELEASE_NOTES-<version>.txt.
8. Sign: dist/release/SHA256SUMS.sig holds the base64 of the 64-byte Ed25519 signature over the exact bytes of
   SHA256SUMS. It is written only after the signature verifies against RELEASE_PUBLIC_KEY_B64.
9. Publish, only with --publish: refuse if tag desktop-v<version> already exists on GITHUB_REPO (a tag check that
   does not answer "not found" also refuses), then gh release create --prerelease with the zip, SHA256SUMS and
   SHA256SUMS.sig as assets. The release targets the commit that was built (git HEAD), so the tag cannot drift from
   the artifacts. It is a prerelease so it never takes the shared repo's "latest" slot from core.

Every subprocess call goes through an injectable runner with an argument list, never a shell string. Each runner
runs in the repository root of the paths it was given. Git and gh results are parsed from stdout only; stderr is
shown only on failure. Paths in the output are relative to the repository root.
"""

from __future__ import annotations

import argparse
import ast
import base64
import binascii
import functools
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[1]
# The shared repo (core releases live here too). Must match meridian_desktop/updater.py REPO.
GITHUB_REPO = "meridianmcp/Meridian"
# Desktop release tags. Must match meridian_desktop/updater.py DESKTOP_TAG_PREFIX. Core's own triggers match only
# v<version>, so a desktop-v tag never starts a server deploy, a core release or a package publish.
TAG_PREFIX = "desktop-v"
TAG_NOT_FOUND_MARKERS = ("HTTP 404", "Not Found")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
SUMS_NAME = "SHA256SUMS"
SIG_NAME = "SHA256SUMS.sig"


def _load_sibling(stem: str) -> ModuleType:
    """Load packaging/<stem>.py by file path under a private name, as tests/test_release_zip.py does."""
    name = f"_packaging_{stem}"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Registered first: a dataclass looks its module up in sys.modules while the module runs.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


make_release = _load_sibling("make_release")
make_release_key = _load_sibling("make_release_key")
build = _load_sibling("build")


class PublishError(RuntimeError):
    """The release is stopped or refused. The message says why."""


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def output(self) -> str:
        """stdout and stderr together, for showing on failure. Results are parsed from stdout, never from this."""
        return "\n".join(part for part in (self.stdout.rstrip("\n"), self.stderr.rstrip("\n")) if part)


Runner = Callable[[Sequence[str]], RunResult]


def run_command(args: Sequence[str], cwd: Path = REPO) -> RunResult:
    """Run one program from an argument list, in cwd. No shell is used. stdout and stderr are kept apart."""
    try:
        done = subprocess.run(
            list(args),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return RunResult(returncode=127, stderr=f"could not start {args[0]}: {exc.__class__.__name__}")
    return RunResult(returncode=done.returncode, stdout=done.stdout or "", stderr=done.stderr or "")


def _tail(output: str, lines: int = 25) -> str:
    kept = output.strip().splitlines()[-lines:]
    return "\n".join(kept) if kept else "(no output)"


def _rel(path: Path, root: Path) -> str:
    """path as a forward-slash path relative to the repository root, for printing."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _display(text: str, root: Path) -> str:
    """text with every absolute path under root made relative to it, and the home folder shown as ~.

    Applied to all printed output and to every refusal message.
    """
    for prefix in (f"{root}{os.sep}", f"{root.as_posix()}/"):
        text = text.replace(prefix, "")
    try:
        home = str(Path.home())
    except RuntimeError:
        return text
    for prefix in (f"{home}{os.sep}", f"{Path(home).as_posix()}/"):
        text = text.replace(prefix, f"~{os.sep}")
    return text


@dataclass(frozen=True)
class ReleasePaths:
    root: Path
    pixi_toml: Path
    pyproject_toml: Path
    version_py: Path
    init_py: Path
    release_key_py: Path
    signing_seed: Path
    bundle: Path
    scripts: Path
    out_dir: Path


def default_paths(root: Path = REPO) -> ReleasePaths:
    return ReleasePaths(
        root=root,
        pixi_toml=root / "pixi.toml",
        pyproject_toml=root / "pyproject.toml",
        version_py=root / "meridian_desktop" / "version.py",
        init_py=root / "meridian_desktop" / "__init__.py",
        release_key_py=root / "meridian_desktop" / "release_key.py",
        signing_seed=make_release_key.default_seed_path(),
        bundle=root / "dist" / make_release.APP_NAME,
        scripts=root / "packaging" / "release",
        out_dir=root / "dist" / "release",
    )


# ---------------------------------------------------------------- version and constants (read from source)


def _module_value(path: Path, name: str, root: Path) -> tuple[ast.Module, ast.expr]:
    """The parsed module, and the value of the one module-level assignment to name in path.

    Read from the source, never imported. The file must assign the name exactly once. A file that assigns it more
    than once is refused, not read: the first assignment is not necessarily the one that ships.
    """
    shown = _rel(path, root)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise PublishError(f"could not read {shown} ({exc.__class__.__name__})") from None
    stores = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store)
    ]
    if not stores:
        raise PublishError(f"{shown} has no {name} string")
    if len(stores) > 1:
        raise PublishError(f"{shown} assigns {name} {len(stores)} times; keep one assignment and run again")
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        else:
            continue
        if value is not None and any(target is stores[0] for target in targets):
            return tree, value
    raise PublishError(f"{shown} must assign {name} once at module level, to a string literal")


def _string_constant(path: Path, name: str, root: Path) -> str:
    _, value = _module_value(path, name, root)
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    raise PublishError(f"{_rel(path, root)} must assign {name} once at module level, to a string literal")


def _imports_app_version(tree: ast.Module) -> bool:
    """True when the module imports APP_VERSION from meridian_desktop.version, absolutely or relatively."""
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        imports_it = any(alias.name == "APP_VERSION" and alias.asname in (None, "APP_VERSION") for alias in node.names)
        if imports_it and node.level == 0 and node.module == "meridian_desktop.version":
            return True
        if imports_it and node.level == 1 and node.module == "version":
            return True
    return False


def _init_version(paths: ReleasePaths, app_version: str | None) -> str:
    """__version__ in meridian_desktop/__init__.py: a string literal, or APP_VERSION imported from version.py."""
    tree, value = _module_value(paths.init_py, "__version__", paths.root)
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    shown = _rel(paths.init_py, paths.root)
    if isinstance(value, ast.Name) and value.id == "APP_VERSION" and _imports_app_version(tree):
        if app_version is None:
            raise PublishError(
                f"{shown} takes __version__ from APP_VERSION, but meridian_desktop/version.py does not exist"
            )
        return app_version
    raise PublishError(
        f"{shown} must assign __version__ once at module level, to a string literal or to APP_VERSION "
        "imported from meridian_desktop.version"
    )


def _pyproject_version(path: Path, root: Path) -> str:
    shown = _rel(path, root)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PublishError(f"could not read the version from {shown} ({exc.__class__.__name__})") from None
    project = data.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not make_release.VERSION_PATTERN.fullmatch(version):
        raise PublishError(f"[project] version in {shown} must be a plain number such as 0.1.0")
    return version


def check_versions(paths: ReleasePaths) -> str:
    """The release version. Every other declared version must equal it, or the run is refused before any build."""
    try:
        version = make_release.read_version(paths.pixi_toml)
    except make_release.ReleaseError as exc:
        raise PublishError(str(exc)) from None
    root = paths.root
    declared = [(_rel(paths.pyproject_toml, root), _pyproject_version(paths.pyproject_toml, root))]
    app_version: str | None = None
    if paths.version_py.is_file():
        app_version = _string_constant(paths.version_py, "APP_VERSION", root)
        declared.append((_rel(paths.version_py, root), app_version))
    declared.append((_rel(paths.init_py, root), _init_version(paths, app_version)))
    wrong = [f"{shown} says {value}" for shown, value in declared if value != version]
    if wrong:
        raise PublishError(
            f"version mismatch: pixi.toml says {version}; " + "; ".join(wrong)
            + ". Set every version file to the same version, then run again."
        )
    return version


def gate_problems() -> list[str]:
    """build.py release_problems(): legal-review marker and TBD text. Read-only."""
    return build.release_problems()


# ---------------------------------------------------------------- signing key


def release_public_key(paths: ReleasePaths) -> bytes:
    """The 32-byte key in RELEASE_PUBLIC_KEY_B64 of meridian_desktop/release_key.py. Refuses an empty or bad value."""
    value = _string_constant(paths.release_key_py, "RELEASE_PUBLIC_KEY_B64", paths.root)
    try:
        key = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != 32:
        raise PublishError(
            f"{_rel(paths.release_key_py, paths.root)} has no valid RELEASE_PUBLIC_KEY_B64 (base64 of 32 bytes). "
            "Run packaging/make_release_key.py, paste the public key it prints there, then run again."
        )
    return key


@dataclass(frozen=True)
class SigningKeys:
    seed: bytes
    public: bytes


def signing_keys(paths: ReleasePaths) -> SigningKeys:
    """The seed and the configured public key. Refuses when the seed is missing or does not give that key.

    Nothing about the seed is printed or put in a message.
    """
    try:
        seed = make_release_key.read_seed(paths.signing_seed)
    except make_release_key.SeedError as exc:
        raise PublishError(f"{exc} Nothing was built or published.") from None
    public = release_public_key(paths)
    if make_release_key.ed25519.public_key(seed) != public:
        raise PublishError(
            "the release signing key does not match RELEASE_PUBLIC_KEY_B64 in meridian_desktop/release_key.py. "
            "Nothing was built or published."
        )
    return SigningKeys(seed=seed, public=public)


def sign_sums(sums_path: Path, sig_path: Path, keys: SigningKeys) -> None:
    """Write SHA256SUMS.sig, the base64 of the signature over the exact bytes of SHA256SUMS.

    The signature is checked against the configured public key before it is written, and the file is read back and
    checked again. A failed check leaves no signature file behind.
    """
    sig_path.unlink(missing_ok=True)  # a signature from an earlier run must not outlive its SHA256SUMS
    data = sums_path.read_bytes()
    signature = make_release_key.ed25519.sign(keys.seed, data)
    if not make_release_key.ed25519.verify(keys.public, data, signature):
        raise PublishError(
            f"the signature over {SUMS_NAME} does not verify against RELEASE_PUBLIC_KEY_B64. Nothing was published."
        )
    sig_path.write_bytes(base64.b64encode(signature) + b"\n")
    try:
        written = base64.b64decode(sig_path.read_bytes().strip(), validate=True)
    except (binascii.Error, ValueError, OSError):
        written = b""
    if not make_release_key.ed25519.verify(keys.public, data, written):
        sig_path.unlink(missing_ok=True)
        raise PublishError(
            f"the written {SIG_NAME} does not verify against RELEASE_PUBLIC_KEY_B64. Nothing was published."
        )


# ---------------------------------------------------------------- build, verify


def run_build(run: Runner) -> None:
    result = run(["pixi", "run", "build"])
    if result.returncode != 0:
        raise PublishError(
            f"pixi run build failed (exit {result.returncode}); nothing was packaged or published.\n"
            f"{_tail(result.output)}"
        )


def run_verify(run: Runner) -> None:
    result = run(["pixi", "run", "verify"])
    printed_pass = any(line.strip() == "RESULT PASS" for line in result.output.splitlines())
    if result.returncode != 0 or not printed_pass:
        raise PublishError(
            f"pixi run verify did not pass (exit {result.returncode}, no RESULT PASS line); the release is stopped.\n"
            f"{_tail(result.output)}"
        )


def _failed(message: str, result: RunResult) -> PublishError:
    detail = result.output.strip()
    return PublishError(f"{message}\n{_tail(detail)}" if detail else message)


def head_commit(run: Runner) -> str:
    """The full SHA of HEAD, read from stdout only."""
    result = run(["git", "rev-parse", "HEAD"])
    sha = result.stdout.strip()
    if result.returncode != 0 or not COMMIT_PATTERN.fullmatch(sha):
        raise _failed("could not read the git HEAD commit to tag the release; nothing was published", result)
    return sha


def working_tree_clean(run: Runner) -> bool:
    """True only when git status --porcelain prints nothing on stdout. A failed git call counts as not clean."""
    result = run(["git", "status", "--porcelain"])
    return result.returncode == 0 and not result.stdout.strip()


def head_on_remote(run: Runner) -> bool:
    """True when HEAD is contained in at least one remote-tracking branch, read from stdout.

    A failed git call counts as not.
    """
    result = run(["git", "branch", "-r", "--contains", "HEAD"])
    return result.returncode == 0 and bool(result.stdout.strip())


@dataclass(frozen=True)
class GitState:
    head: str
    clean: bool
    on_remote: bool

    def blockers(self) -> list[str]:
        """Why --publish refuses this state. Empty when the git checks pass."""
        problems = []
        if not self.clean:
            problems.append("the working tree is not clean (git status --porcelain must print nothing)")
        if not self.on_remote:
            problems.append(f"HEAD {self.head[:12]} is not on any remote branch (git branch -r --contains HEAD)")
        return problems


def git_state(run: Runner) -> GitState:
    head = head_commit(run)
    return GitState(head=head, clean=working_tree_clean(run), on_remote=head_on_remote(run))


# ---------------------------------------------------------------- package


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Packaged:
    root: Path
    zip_path: Path
    sums_path: Path
    sig_path: Path
    notes_path: Path
    digest: str
    excluded: tuple[str, ...]


def package(version: str, paths: ReleasePaths) -> Packaged:
    """Write the release zip, SHA256SUMS and the release notes file, all in paths.out_dir."""
    try:
        result = make_release.build_release(paths.bundle, paths.scripts, paths.out_dir, version)
    except make_release.ReleaseError as exc:
        raise PublishError(str(exc)) from None
    digest = sha256_file(result.path)
    sums_path = paths.out_dir / SUMS_NAME
    sums_path.write_bytes(f"{digest}  {result.path.name}\n".encode("utf-8"))
    notes_path = paths.out_dir / f"RELEASE_NOTES-{version}.txt"
    notes_path.write_bytes(
        f"Meridian Desktop {version}\n\nSHA-256 of {result.path.name}: {digest}\n".encode("utf-8")
    )
    return Packaged(
        root=paths.root,
        zip_path=result.path,
        sums_path=sums_path,
        sig_path=paths.out_dir / SIG_NAME,
        notes_path=notes_path,
        digest=digest,
        excluded=result.excluded,
    )


def release_command(version: str, packaged: Packaged, target: str) -> list[str]:
    """The exact gh command that creates the release. Argument list only, with the three assets.

    File paths are relative to the repository root. gh runs there (the runner sets that as its working directory),
    so the printed command shows no absolute path.
    """
    return [
        "gh",
        "release",
        "create",
        f"{TAG_PREFIX}{version}",
        _rel(packaged.zip_path, packaged.root),
        _rel(packaged.sums_path, packaged.root),
        _rel(packaged.sig_path, packaged.root),
        "--repo",
        GITHUB_REPO,
        "--target",
        target,
        "--title",
        f"Meridian Desktop {version}",
        "--notes-file",
        _rel(packaged.notes_path, packaged.root),
        # A prerelease never takes the repo's "latest" slot, so the core release stays the latest one here.
        "--prerelease",
    ]


# ---------------------------------------------------------------- GitHub


def _tag_check_error(tag: str, result: RunResult) -> PublishError:
    return PublishError(
        f"could not check whether tag {tag} exists on {GITHUB_REPO}; refusing to publish (fail closed).\n"
        f"{_tail(result.output)}"
    )


def tag_exists(version: str, gh: Runner) -> bool:
    """True only if the tag desktop-v<version> exists on GITHUB_REPO. The answer is parsed from stdout only.

    gh api answers with the ref it found, as one object or as a list. Only a ref named refs/tags/desktop-v<version>
    counts, so desktop-v0.1.0-rc1 does not block desktop-v0.1.0. A 404 means absent. Any other failure, and any answer
    that does not name each ref it holds, is inconclusive and raises, so the release is refused (fail closed).
    """
    tag = f"{TAG_PREFIX}{version}"
    wanted = f"refs/tags/{tag}"
    result = gh(["gh", "api", f"repos/{GITHUB_REPO}/git/ref/tags/{tag}"])
    if result.returncode != 0:
        if any(marker in result.output for marker in TAG_NOT_FOUND_MARKERS):
            return False
        raise _tag_check_error(tag, result)
    try:
        answer = json.loads(result.stdout)
    except ValueError:
        raise _tag_check_error(tag, result) from None
    if isinstance(answer, dict):
        found = [answer]
    elif isinstance(answer, list):
        found = answer
    else:
        raise _tag_check_error(tag, result)
    refs: list[str] = []
    for item in found:
        ref = item.get("ref") if isinstance(item, dict) else None
        if not isinstance(ref, str):
            raise _tag_check_error(tag, result)
        refs.append(ref)
    return wanted in refs


def create_github_release(version: str, packaged: Packaged, target: str, gh: Runner) -> str:
    """Create the GitHub release. The tag check runs first, and gh release create runs only if it passes."""
    tag = f"{TAG_PREFIX}{version}"
    if tag_exists(version, gh):
        raise PublishError(
            f"tag {tag} already exists on {GITHUB_REPO}; refusing to publish. Bump the version in every version file "
            "(pixi.toml, pyproject.toml, meridian_desktop/version.py and meridian_desktop/__init__.py), then run again."
        )
    result = gh(release_command(version, packaged, target))
    if result.returncode != 0:
        short = target[:12]
        raise PublishError(
            f"gh release create failed (exit {result.returncode}). A tag may now exist on {GITHUB_REPO} for {tag}.\n"
            "To recover safely, change nothing until you have checked:\n"
            f"  1. gh api repos/{GITHUB_REPO}/git/ref/tags/{tag}  this run tags the commit it built, {short}, so a tag "
            f"that points there may be this run's. If it points anywhere else, stop: it is not from this run.\n"
            f"  2. gh release view {tag} --repo {GITHUB_REPO}  shows whether the release was made.\n"
            f"  3. If the tag points at {short}, remove the release and tag together with "
            f"gh release delete {tag} --cleanup-tag --repo {GITHUB_REPO}. If there is no release, remove only the tag "
            f"with gh api -X DELETE repos/{GITHUB_REPO}/git/refs/tags/{tag}, and then run again.\n"
            f"{_tail(result.output)}"
        )
    return result.stdout.strip()


# ---------------------------------------------------------------- the run


@dataclass(frozen=True)
class ReleaseOutcome:
    version: str
    published: bool
    gh_command: tuple[str, ...]
    zip_path: Path
    sums_path: Path
    sig_path: Path
    release_output: str  # gh's output when published, else empty


def _release(
    *,
    publish: bool,
    paths: ReleasePaths,
    run: Runner,
    gh: Runner,
    gate: Callable[[], list[str]],
    say: Callable[[str], None],
) -> ReleaseOutcome:
    version = check_versions(paths)
    say(f"version {version}: every version file agrees")

    problems = gate()
    if problems:
        raise PublishError(
            "release gate: " + "; ".join(problems) + ". Remove the legal-review marker and TBD text, then run again."
        )
    say("gate: no legal-review marker or TBD text")

    keys = signing_keys(paths)
    say("signing: the release key matches meridian_desktop/release_key.py")

    state = git_state(run)
    say(f"git: HEAD {state.head}")
    say("git: working tree is clean" if state.clean else "warning: working tree is not clean")
    say("git: HEAD is on a remote branch" if state.on_remote else "warning: HEAD is not on any remote branch")
    blockers = state.blockers()
    if publish and blockers:
        raise PublishError("cannot publish: " + "; ".join(blockers) + ". Nothing was built or published.")
    if not publish:
        if blockers:
            say("--publish would refuse: " + "; ".join(blockers))
        else:
            say("--publish would pass the git checks")

    say("build: pixi run build")
    run_build(run)
    say("verify: pixi run verify")
    run_verify(run)
    say("verify: RESULT PASS")

    if head_commit(run) != state.head:
        raise PublishError(
            f"HEAD moved during the build (it was {state.head[:12]} before the build); "
            "nothing was published. Run again from a fixed commit."
        )

    packaged = package(version, paths)
    for relative in packaged.excluded:
        say(f"left out (secret-named): {relative}")
    say(f"wrote {_rel(packaged.zip_path, paths.root)}")
    say(f"sha256 {packaged.digest}  {packaged.zip_path.name}")
    say(f"wrote {_rel(packaged.sums_path, paths.root)}")

    sign_sums(packaged.sums_path, packaged.sig_path, keys)
    say(f"signed {SUMS_NAME}; wrote {_rel(packaged.sig_path, paths.root)}")

    command = release_command(version, packaged, state.head)
    if not publish:
        say("dry run: nothing was changed on GitHub. With --publish, the tag check runs and then this command:")
        say("  " + subprocess.list2cmdline(command))
        return ReleaseOutcome(
            version=version,
            published=False,
            gh_command=tuple(command),
            zip_path=packaged.zip_path,
            sums_path=packaged.sums_path,
            sig_path=packaged.sig_path,
            release_output="",
        )

    output = create_github_release(version, packaged, state.head, gh)
    say(f"published {TAG_PREFIX}{version} to {GITHUB_REPO}")
    if output:
        say(output)
    return ReleaseOutcome(
        version=version,
        published=True,
        gh_command=tuple(command),
        zip_path=packaged.zip_path,
        sums_path=packaged.sums_path,
        sig_path=packaged.sig_path,
        release_output=output,
    )


def run_release(
    *,
    publish: bool,
    paths: ReleasePaths | None = None,
    run: Runner | None = None,
    gh: Runner | None = None,
    gate: Callable[[], list[str]] = gate_problems,
    log: Callable[[str], None] = print,
) -> ReleaseOutcome:
    """Run the release steps. Every line of output and every refusal shows paths relative to the repository root.

    Without run= or gh=, git, pixi and gh run in the root of paths.
    """
    root = REPO if paths is None else paths.root

    def say(message: str) -> None:
        log(_display(message, root))

    try:
        resolved = default_paths() if paths is None else paths
        runner = run if run is not None else functools.partial(run_command, cwd=resolved.root)
        gh_runner = gh if gh is not None else functools.partial(run_command, cwd=resolved.root)
        return _release(publish=publish, paths=resolved, run=runner, gh=gh_runner, gate=gate, say=say)
    except PublishError as exc:
        raise PublishError(_display(str(exc), root)) from None
    except Exception as exc:  # never a traceback: the message goes through the same relative-path display
        raise PublishError(f"unexpected {exc.__class__.__name__}: {_display(str(exc), root)}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build, verify, sign and package the Meridian Desktop release. Dry run unless --publish is given."
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="create the GitHub prerelease desktop-v<version> after packaging (default: dry run, no GitHub change)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_release(publish=args.publish)
    except PublishError as exc:
        print(f"release stopped: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # belt and braces: no traceback escapes the publisher
        print(f"release stopped: unexpected {exc.__class__.__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
