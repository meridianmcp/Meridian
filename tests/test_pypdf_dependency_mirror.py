"""aa423c7e -- extensions/meridian-outputs declares pypdf as a real,
non-optional runtime dependency backing outputs_local.py's PDF body-content
extraction (_extract_pdf_text / the "pdf_content" _classify_suffix kind),
but that extension's own test suite imports the module straight off
sys.path (see extensions/meridian-outputs/tests/conftest.py) rather than via
a pip install of the package -- so nothing pulls pypdf into the shared pixi
test environment unless pixi.toml's [pypi-dependencies] ALSO declares it.

This is the exact class of gap already hit (and fixed) for
tantivy/xxhash/pyarrow/psutil -- see 52cbe5d8 and its task_ecb96ac9
follow-ons, and the matching comments in pixi.toml right next to the pypdf
entry this test locks in. Scoped narrowly (like
tests/test_tunnel_extension_dependencies.py) to the one manifest pair this
item actually touches.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]

MERIDIAN_OUTPUTS_PYPROJECT = "extensions/meridian-outputs/pyproject.toml"


def _pyproject_dependencies(manifest_path: str) -> list[str]:
    with (REPO_ROOT / manifest_path).open("rb") as handle:
        data = tomllib.load(handle)
    return list(data.get("project", {}).get("dependencies", []))


def _pixi_pypi_dependencies() -> dict[str, object]:
    with (REPO_ROOT / "pixi.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return dict(data.get("pypi-dependencies", {}))


def test_meridian_outputs_declares_pypdf_dependency() -> None:
    deps = _pyproject_dependencies(MERIDIAN_OUTPUTS_PYPROJECT)
    assert any(d.replace(" ", "").startswith("pypdf") for d in deps), (
        f"{MERIDIAN_OUTPUTS_PYPROJECT} must declare a pypdf dependency for "
        f"PDF body-content extraction; found dependencies={deps!r}"
    )


def test_pixi_toml_mirrors_pypdf_dependency() -> None:
    pixi_deps = _pixi_pypi_dependencies()
    assert "pypdf" in pixi_deps, (
        "pixi.toml's [pypi-dependencies] must mirror "
        f"{MERIDIAN_OUTPUTS_PYPROJECT}'s pypdf dependency -- otherwise a fresh "
        "clone/worktree's shared pixi env never gets pypdf, and "
        "outputs_local.py's PDF extraction silently falls back to the "
        "(also-correct-but-untested-by-default) missing-dependency degrade "
        f"path instead of exercising real extraction. found pypi-dependencies "
        f"keys={sorted(pixi_deps.keys())!r}"
    )


def test_pypdf_actually_importable_in_this_environment() -> None:
    # Regression guard for the actual failure mode: the manifests can agree
    # on paper while the installed env still lacks the package (a stale
    # lock, or a skipped `pixi install`). Import it directly -- this is the
    # same real import the main test env needs for
    # extensions/meridian-outputs/tests/test_outputs_local.py's
    # TestPdfBodyIndexing suite to exercise real (not degraded) extraction.
    import pypdf  # noqa: F401
