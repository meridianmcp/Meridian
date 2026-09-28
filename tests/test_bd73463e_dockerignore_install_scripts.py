"""bd73463e -- prod install endpoints 404 because .dockerignore excluded all of
scripts/.

meridian/routes/hooks.py serves several installer scripts by reading them off
disk at request time (``_watcher_script_path``: ``<repo_root>/scripts/<name>``).
The Dockerfile does ``COPY . .`` (respecting .dockerignore) into WORKDIR
``/app``, so any file .dockerignore excludes from ``scripts/`` genuinely does
not exist in the running container -- the route 404s for everyone, always,
regardless of auth.

The original ``.dockerignore`` had a bare ``scripts/`` line: a DIRECTORY-level
exclusion. Per Docker's own documented behaviour (matching gitignore: "It is
not possible to re-include a file if a parent directory of that file is
excluded"), no later ``!scripts/whatever`` pattern can undo that -- the same
trap this file already documents and avoids for ``extensions/*``. The fix
mirrors that existing pattern: ``scripts/*`` (an ENTRY-level exclusion of each
child individually) plus explicit ``!`` negations for exactly the files the
routes need.

This test does not require Docker: it implements the small, order-dependent
subset of dockerignore/gitignore pattern semantics this repo's .dockerignore
actually uses (bare-name matches anywhere, ``dir/`` sticky directory excludes,
``dir/*`` entry-level excludes, ``!`` negation, and the parent-exclusion-blocks
-child-negation rule) and applies it directly to the real, current
``.dockerignore`` file plus the real files on disk.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERIGNORE = _REPO_ROOT / ".dockerignore"


def _parse_patterns(text: str) -> list[tuple[str, bool]]:
    """Return [(pattern, negate)] in file order, comments/blanks dropped."""
    out: list[tuple[str, bool]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        out.append((line, negate))
    return out


def _segment_glob_match(pattern_seg: str, name: str) -> bool:
    """One path segment against one glob segment (``*``/``?`` stay within
    the segment; this file's patterns never use bare ``**``)."""
    return fnmatch.fnmatchcase(name, pattern_seg)


def _pattern_reaches_node(pattern: str, prefix_parts: tuple[str, ...]) -> bool:
    """True if ``pattern`` matches the filesystem NODE located at exactly
    this ancestor path (``prefix_parts`` -- may be an intermediate directory,
    not necessarily the full candidate path).

    - A pattern with no ``/`` (e.g. ``*.pyc``, or a bare dir name like
      ``tests``) matches by the node's OWN basename, at any depth.
    - A pattern ending in ``/**`` matches any node STRICTLY under the literal
      path before the ``/**`` (never the node at that path itself).
    - Any other pattern containing ``/`` is anchored at the ignore file's
      root and matches only the node at exactly that literal depth.
    """
    if pattern.endswith("/**"):
        base_parts = tuple(pattern[: -len("/**")].split("/"))
        if len(prefix_parts) <= len(base_parts):
            return False
        return all(_segment_glob_match(b, p) for b, p in zip(base_parts, prefix_parts))

    stem = pattern.rstrip("/")
    if "/" not in stem:
        return _segment_glob_match(stem, prefix_parts[-1])

    stem_parts = tuple(stem.split("/"))
    if len(prefix_parts) != len(stem_parts):
        return False
    return all(_segment_glob_match(s, p) for s, p in zip(stem_parts, prefix_parts))


def docker_build_would_include(dockerignore_text: str, rel_path: str) -> bool:
    """True if a Docker build using this .dockerignore would ship ``rel_path``
    (a repo-relative path using ``/`` separators) into the image via ``COPY . .``.

    Mirrors real gitignore/dockerignore semantics: walk the path from the
    root inward, one ancestor at a time. At each ancestor depth, the LAST
    pattern (in file order) that matches the node AT THAT EXACT DEPTH decides
    whether that node is excluded or included -- a pattern that only reaches
    a deeper descendant (e.g. ``extensions/*`` at depth 2 while checking depth
    1) never counts. If any ancestor ends up excluded with nothing at that
    same depth negating it, the whole path is excluded -- deeper patterns
    (like a specific file's own name) can never resurrect a path buried
    inside an excluded directory. This is exactly the "cannot re-include a
    file if a parent directory is excluded" rule both tools document, and
    exactly why this repo's real .dockerignore needs two separate negations
    for ``extensions/meridian-codeindex`` -- one for the directory entry
    itself (``!extensions/meridian-codeindex``, matched at depth 2) and one
    for everything inside it (``!extensions/meridian-codeindex/**``, matched
    at depth 3+).
    """
    patterns = _parse_patterns(dockerignore_text)
    parts = rel_path.split("/")

    for depth in range(1, len(parts) + 1):
        prefix_parts = tuple(parts[:depth])
        state: bool | None = None  # None = untouched (default include) at this depth
        for pattern, negate in patterns:
            if _pattern_reaches_node(pattern, prefix_parts):
                state = not negate
        if state is True:
            return False
    return True


# ---------------------------------------------------------------------------
# The actual regression: these routes 404'd in production before bd73463e.
# ---------------------------------------------------------------------------

_ROUTE_SERVED_SCRIPTS = [
    "scripts/install-windows.ps1",
    "scripts/install_tunnel.ps1",
    "scripts/install_tunnel.sh",
    "scripts/install_watcher.ps1",
    "scripts/install_watcher.sh",
    "scripts/hooks_install.ps1",
]


@pytest.fixture(scope="module")
def dockerignore_text() -> str:
    assert _DOCKERIGNORE.exists(), ".dockerignore must exist at repo root"
    return _DOCKERIGNORE.read_text(encoding="utf-8")


@pytest.mark.parametrize("rel_path", _ROUTE_SERVED_SCRIPTS)
def test_installer_scripts_survive_into_the_docker_image(dockerignore_text, rel_path):
    on_disk = _REPO_ROOT / rel_path
    assert on_disk.exists(), f"{rel_path} must exist on disk for this test to mean anything"
    assert docker_build_would_include(dockerignore_text, rel_path), (
        f"{rel_path} would be EXCLUDED from the Docker image -- "
        f"meridian/routes/hooks.py would 404 serving it, exactly like the original bd73463e bug"
    )


@pytest.mark.parametrize("rel_path", [
    "scripts/deploy.ps1",
    "scripts/test_hooks.sh",
    "scripts/wsl-linux-check.sh",
    "scripts/install_linux_launcher.sh",  # not yet route-served; stays out until it is
])
def test_dev_only_scripts_stay_out_of_the_image(dockerignore_text, rel_path):
    on_disk = _REPO_ROOT / rel_path
    assert on_disk.exists(), f"{rel_path} must exist on disk for this test to mean anything"
    assert not docker_build_would_include(dockerignore_text, rel_path), (
        f"{rel_path} is dev/release-only tooling and should not ship in the image; "
        f"the fix for bd73463e must not widen scripts/* into a blanket re-include"
    )


def test_root_level_install_scripts_were_never_affected(dockerignore_text):
    # These are a *different* file from scripts/install.sh -- served by
    # _hook_script_path, which checks the repo root first. They were never
    # excluded (only scripts/ was), so this pins that they stay that way.
    for rel_path in ("install.ps1", "install.sh"):
        assert (_REPO_ROOT / rel_path).exists()
        assert docker_build_would_include(dockerignore_text, rel_path)


def test_meridian_codeindex_extension_still_ships(dockerignore_text):
    # Sanity check that the matcher correctly models the *existing*,
    # already-working extensions/* negation this fix's comment refers to.
    assert docker_build_would_include(dockerignore_text, "extensions/meridian-codeindex/pyproject.toml")


@pytest.mark.parametrize("rel_path", [
    "extensions/meridian-outputs/server.py",
    ".env",
    "meridian.toml",
    "tests/test_core.py",
    "data/meridian.db",
])
def test_unrelated_exclusions_are_unaffected(dockerignore_text, rel_path):
    assert not docker_build_would_include(dockerignore_text, rel_path)


def test_ordinary_source_files_still_ship(dockerignore_text):
    assert docker_build_would_include(dockerignore_text, "meridian/server.py")
    assert docker_build_would_include(dockerignore_text, "pyproject.toml")


# ---------------------------------------------------------------------------
# Matcher self-test: prove it reproduces the ORIGINAL bug against the
# original (bare "scripts/") form, so a future edit can't silently regress
# the matcher itself into always passing.
# ---------------------------------------------------------------------------

_ORIGINAL_BROKEN_FORM = "scripts/\n"


@pytest.mark.parametrize("rel_path", _ROUTE_SERVED_SCRIPTS)
def test_matcher_reproduces_the_original_404_bug(rel_path):
    assert not docker_build_would_include(_ORIGINAL_BROKEN_FORM, rel_path), (
        "matcher sanity check failed: it must classify these as EXCLUDED "
        "under the original bare 'scripts/' pattern, or this test file "
        "isn't actually proving anything about the fix"
    )
