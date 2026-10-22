"""c11c5117 -- hosted ``get_latex_structure`` fails with "No module named 'docparse'".

``meridian/docs_intel.py`` and ``meridian/latex_intel.py`` are thin shims over the
detachable ``packages/docparse`` sub-package (``from docparse import ...``).
Locally ``import docparse`` resolves only because ``pixi.toml`` declares an
editable path install (``meridian-docparse = { path = "packages/docparse",
editable = true }``). The hosted image is built from the ``Dockerfile`` via
``uv pip install --system .`` (which installs ``pyproject.toml``'s dependency
list -- and docparse is not on it, it is a separate package) plus one explicit
``uv pip install --system ./extensions/meridian-codeindex``. Nothing ever
installed ``packages/docparse``, so every hosted call that reaches the shims
(``get_latex_structure``, the docx structure tools, ...) died with
``ModuleNotFoundError``.

This test does not require Docker. It statically checks that:

* the Dockerfile installs ``./packages/docparse`` in a ``RUN`` that runs AFTER
  the ``COPY . .`` that brings the source into the image (a path install before
  the copy would fail the build),
* the Dockerfile fails the build if ``import docparse`` does not work after that
  install (a build-time smoke check, so a regression cannot ship silently),
* ``.dockerignore`` -- evaluated with the same order-dependent matcher
  ``test_bd73463e_dockerignore_install_scripts`` uses -- keeps every file
  ``packages/docparse`` needs to build and import, and
* the subset of files a Docker build would ship is by itself sufficient to
  ``import docparse`` from a clean directory (hermetic simulation of the image's
  install, without needing network access or hatchling).
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE = _REPO_ROOT / "Dockerfile"
_DOCKERIGNORE = _REPO_ROOT / ".dockerignore"
_DOCPARSE_DIR = _REPO_ROOT / "packages" / "docparse"

# Files the hosted image must contain for `uv pip install ./packages/docparse`
# to build (pyproject.toml + the README it references + the wheel's package dir)
# and for `import docparse` to work afterwards.
_REQUIRED_DOCPARSE_FILES = [
    "packages/docparse/pyproject.toml",
    "packages/docparse/README.md",
    "packages/docparse/docparse/__init__.py",
    "packages/docparse/docparse/docs_intel.py",
    "packages/docparse/docparse/latex_intel.py",
    "packages/docparse/docparse/structural_parser.py",
]


def _load_sibling_matcher():
    """Reuse the real dockerignore matcher from the bd73463e test module rather
    than re-implementing it. Loaded by file path so it does not depend on
    ``tests/`` being importable as a package or on sys.path ordering."""
    path = Path(__file__).resolve().parent / "test_bd73463e_dockerignore_install_scripts.py"
    spec = importlib.util.spec_from_file_location("_bd73463e_matcher", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.docker_build_would_include


docker_build_would_include = _load_sibling_matcher()


def _instructions(text: str) -> list[tuple[str, str]]:
    """Parse a Dockerfile into ``[(INSTRUCTION, args)]``: comment lines dropped,
    backslash line continuations joined."""
    logical: list[str] = []
    buf = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not buf and (not stripped or stripped.startswith("#")):
            continue
        if buf and stripped.startswith("#"):
            # comment lines inside a continued instruction are ignored by Docker
            continue
        if stripped.endswith("\\"):
            buf += " " + stripped[:-1].strip()
            continue
        logical.append((buf + " " + stripped).strip())
        buf = ""
    if buf:
        logical.append(buf.strip())
    out: list[tuple[str, str]] = []
    for line in logical:
        head, _, rest = line.partition(" ")
        out.append((head.upper(), rest.strip()))
    return out


@pytest.fixture(scope="module")
def instructions() -> list[tuple[str, str]]:
    assert _DOCKERFILE.exists(), "Dockerfile must exist at repo root"
    return _instructions(_DOCKERFILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dockerignore_text() -> str:
    assert _DOCKERIGNORE.exists(), ".dockerignore must exist at repo root"
    return _DOCKERIGNORE.read_text(encoding="utf-8")


def _is_pip_install_of_docparse(instr: str, args: str) -> bool:
    return (
        instr == "RUN"
        and re.search(r"\bpip\s+install\b", args) is not None
        and re.search(r"(^|[\s'\"])(\./)?packages/docparse/?([\s'\"]|$)", args) is not None
    )


def _index_of_copy_all(instructions: list[tuple[str, str]]) -> int:
    for i, (instr, args) in enumerate(instructions):
        if instr != "COPY":
            continue
        tokens = [t for t in args.split() if not t.startswith("--")]
        if tokens and tokens[0] == "." and tokens[-1] in (".", "./"):
            return i
    raise AssertionError("Dockerfile has no `COPY . .`; premise of this test changed")


# ---------------------------------------------------------------------------
# Premise: the hosted server really does import docparse at runtime.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shim", ["meridian/docs_intel.py", "meridian/latex_intel.py"])
def test_server_shims_import_docparse(shim):
    src = (_REPO_ROOT / shim).read_text(encoding="utf-8")
    assert re.search(r"^from docparse import ", src, re.MULTILINE), (
        f"{shim} no longer imports docparse; re-derive whether the Dockerfile "
        f"still needs to install packages/docparse"
    )


def test_pyproject_does_not_already_pull_docparse_in():
    # If the root pyproject ever starts depending on docparse the explicit
    # Dockerfile step becomes redundant -- but until then it is required, since
    # `uv pip install --system .` is the only other install of project deps.
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = [d.lower() for d in data["project"]["dependencies"]]
    assert not any("docparse" in d for d in deps), (
        "pyproject.toml now depends on docparse; revisit the explicit Dockerfile install"
    )


# ---------------------------------------------------------------------------
# The fix: the Dockerfile installs docparse, after the source is copied in.
# ---------------------------------------------------------------------------


def test_dockerfile_installs_docparse(instructions):
    installs = [i for i, (ins, args) in enumerate(instructions) if _is_pip_install_of_docparse(ins, args)]
    assert installs, (
        "Dockerfile never installs ./packages/docparse -- the hosted image cannot "
        "`import docparse`, so get_latex_structure fails with "
        "\"No module named 'docparse'\" (c11c5117)"
    )


def test_docparse_install_runs_after_source_copy(instructions):
    copy_idx = _index_of_copy_all(instructions)
    installs = [i for i, (ins, args) in enumerate(instructions) if _is_pip_install_of_docparse(ins, args)]
    assert installs, "no docparse install found (see test_dockerfile_installs_docparse)"
    assert all(i > copy_idx for i in installs), (
        "the docparse path-install must come after `COPY . .`; before it "
        "./packages/docparse does not exist in the build context layer yet"
    )


def test_dockerfile_smoke_checks_import_after_install(instructions):
    installs = [i for i, (ins, args) in enumerate(instructions) if _is_pip_install_of_docparse(ins, args)]
    assert installs, "no docparse install found (see test_dockerfile_installs_docparse)"
    smoke = [
        i
        for i, (ins, args) in enumerate(instructions)
        if ins == "RUN" and re.search(r"python\d*(\.\d+)?\s+-c\s+.*\bimport\s+docparse\b", args)
    ]
    assert smoke, (
        "Dockerfile has no build-time `RUN python -c \"import docparse ...\"` "
        "smoke check, so a broken/missing install would ship silently"
    )
    assert min(smoke) > min(installs), "the import smoke check must run after the install"


def test_docparse_install_uses_system_site_packages_like_the_rest_of_the_image(instructions):
    # Every other install in this image goes into the system interpreter
    # (`uv pip install --system`); a venv/user install would not be on the
    # interpreter uvicorn runs under.
    for ins, args in instructions:
        if _is_pip_install_of_docparse(ins, args) and re.search(r"\buv\s+pip\s+install\b", args):
            assert "--system" in args


# ---------------------------------------------------------------------------
# .dockerignore keeps packages/docparse in the image.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel_path", _REQUIRED_DOCPARSE_FILES)
def test_required_docparse_files_exist_on_disk(rel_path):
    assert (_REPO_ROOT / rel_path).is_file(), f"{rel_path} must exist for this test to mean anything"


@pytest.mark.parametrize("rel_path", _REQUIRED_DOCPARSE_FILES)
def test_dockerignore_keeps_required_docparse_files(dockerignore_text, rel_path):
    assert docker_build_would_include(dockerignore_text, rel_path), (
        f"{rel_path} would be EXCLUDED from the Docker image; "
        f"`uv pip install ./packages/docparse` cannot build/import without it (c11c5117)"
    )


def _shippable_docparse_files() -> list[str]:
    out = []
    for p in sorted(_DOCPARSE_DIR.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.suffix == ".pyc":
            continue
        out.append(p.relative_to(_REPO_ROOT).as_posix())
    return out


def test_dockerignore_keeps_every_tracked_docparse_source_file(dockerignore_text):
    files = _shippable_docparse_files()
    assert files, "packages/docparse has no files on disk?"
    dropped = [f for f in files if not docker_build_would_include(dockerignore_text, f)]
    assert not dropped, f".dockerignore would drop docparse files from the image: {dropped}"


@pytest.mark.parametrize(
    "broken_ignore",
    [
        "packages/\n",
        "packages/*\n",
        "packages/docparse/\n",
        "packages/docparse\n",
    ],
)
def test_matcher_flags_docparse_exclusions(broken_ignore):
    # Matcher self-test: a future edit that excludes docparse in any of the
    # natural ways must be classified as EXCLUDED, or the tests above prove nothing.
    assert not docker_build_would_include(broken_ignore, "packages/docparse/pyproject.toml")
    assert not docker_build_would_include(broken_ignore, "packages/docparse/docparse/__init__.py")


def test_matcher_accepts_the_entry_level_negation_form():
    ignore = "packages/*\n!packages/docparse\n!packages/docparse/**\n"
    assert docker_build_would_include(ignore, "packages/docparse/pyproject.toml")
    assert docker_build_would_include(ignore, "packages/docparse/docparse/__init__.py")
    assert not docker_build_would_include(ignore, "packages/meridian-plugin-base/pyproject.toml")


# ---------------------------------------------------------------------------
# Packaging sanity + hermetic simulation of the image's install.
# ---------------------------------------------------------------------------


def test_docparse_pyproject_is_self_consistent():
    data = tomllib.loads((_DOCPARSE_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "meridian-docparse"
    assert data["project"]["dependencies"] == [], (
        "docparse is documented as pure stdlib; a runtime dependency would also need "
        "to be installed in the hosted image"
    )
    readme = data["project"].get("readme")
    if readme:
        assert (_DOCPARSE_DIR / readme).is_file(), "hatchling fails the build if the readme is missing"
    for pkg in data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]:
        assert (_DOCPARSE_DIR / pkg / "__init__.py").is_file()


@pytest.mark.subprocess_isolated
def test_files_a_docker_build_ships_are_enough_to_import_docparse(tmp_path, dockerignore_text):
    """Copy ONLY the docparse files the real .dockerignore lets through into an
    empty directory and import the package from there in a fresh interpreter --
    the closest hermetic stand-in for the image's ``uv pip install`` +
    ``import docparse`` smoke check."""
    site = tmp_path / "site"
    for rel in _shippable_docparse_files():
        if not docker_build_would_include(dockerignore_text, rel):
            continue
        # the importable package dir is packages/docparse/docparse -> site/docparse
        prefix = "packages/docparse/docparse/"
        if rel.startswith(prefix):
            dest = site / "docparse" / rel[len(prefix):]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((_REPO_ROOT / rel).read_bytes())

    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(site)!r})
        import docparse
        from docparse import docs_intel, latex_intel, structural_parser
        assert docparse.__file__.startswith({str(site)!r}), docparse.__file__
        assert hasattr(docparse, "StructuralParser")
        print("ok")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=str(tmp_path)
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
